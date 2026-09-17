"""Tests for the P1 domains: usage, health and logs.

Hermetic like the rest: a fake Hermes root with its own ``state.db``, cron store,
log files and LaunchAgents directory.  External state is never read -- ``launchctl``,
``lsof`` and the TCP probe are patched -- so the suite says the same thing on any
machine.

Two things here are deliberately more than unit tests:

* the credential scrubber is exercised on every path that renders log text, because
  a page that leaks a pasted key is worse than a page that shows nothing;
* every collection's count is checked against the records it actually carries, the
  invariant that three of the P1 overviews broke on first run (a headline counting
  a sample instead of its set).
"""

from __future__ import annotations

import datetime as dt
import plistlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes.portal import sources  # noqa: E402
from hermes.portal.domains import cron as cron_domain  # noqa: E402
from hermes.portal.domains import default_registry  # noqa: E402
from hermes.portal.domains import health as health_domain  # noqa: E402
from hermes.portal.domains import logs as logs_domain  # noqa: E402
from hermes.portal.domains import sessions as sessions_domain  # noqa: E402
from hermes.portal.domains import usage as usage_domain  # noqa: E402
from hermes.portal.model import (  # noqa: E402
    Domain,
    count_map,
    failed_collection,  # noqa: E402
)

FAKE_KEY = "sk-ABCDEF1234567890"
FAKE_BEARER = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6"
FAKE_TOKEN = "api_key=SUPERSECRET123"
# a *live* clock: every stamp below is relative to it, so "30s ago is fresh" holds
# whenever the suite runs.  Frozen at a literal, the fresh row aged past the 10-minute
# staleness threshold and the health tests flipped from 1 stale to 2 on their own.
NOW = time.time()
# The usage rollups group sessions by their *local date*, so the fixture anchors its
# sessions to local midnight rather than to "now minus N hours": with hour offsets the
# day a session lands on depends on the hour the suite runs, and this file's by-day test
# read three days at 17:00 and two in the morning.  Midnight and midnight-minus-a-day
# are the same two days whenever the suite runs.
MIDNIGHT = (
    dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
)
TODAY = MIDNIGHT
YESTERDAY = MIDNIGHT - 86_400


def make_state_db(path: Path) -> None:
    """A minimal but honest state.db: a *subset* schema, which is the point.

    Adapters select only the columns they find, so a narrow fixture exercises the
    drift tolerance the real 58-column table would hide.
    """
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table sessions (
            id text primary key, title text, model text, billing_provider text,
            started_at real, message_count integer, input_tokens integer,
            output_tokens integer, estimated_cost_usd real, actual_cost_usd real
        );
        create table session_model_usage (
            session_id text, model text, billing_provider text, task text,
            api_call_count integer, input_tokens integer, output_tokens integer,
            estimated_cost_usd real
        );
        create table gateway_heartbeats (
            backend_id text, pid integer, started_at real, last_heartbeat real,
            profile text, host text
        );
        """
    )
    con.executemany(
        "insert into sessions values (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "s1",
                "Cheap session",
                "small-model",
                "deepseek",
                YESTERDAY + 3_600,
                4,
                100,
                50,
                0.01,
                None,
            ),
            (
                "s2",
                "Expensive session",
                "big-model",
                "openrouter",
                TODAY,
                40,
                9000,
                4000,
                1.25,
                1.20,
            ),
            (
                "s3",
                "Same day, other model",
                "big-model",
                "openrouter",
                TODAY + 3_600,
                12,
                2000,
                900,
                0.40,
                None,
            ),
            (
                "s4",
                "Unpriced session",
                "small-model",
                "custom",
                TODAY + 7_200,
                2,
                10,
                5,
                None,
                None,
            ),
            (
                "s5",
                "Yesterday",
                "small-model",
                "deepseek",
                YESTERDAY,
                3,
                80,
                40,
                0.02,
                None,
            ),
        ],
    )
    con.executemany(
        "insert into session_model_usage values (?,?,?,?,?,?,?,?)",
        [
            ("s1", "small-model", "deepseek", "chat", 2, 100, 50, 0.01),
            ("s2", "big-model", "openrouter", "chat", 30, 9000, 4000, 1.25),
            ("s2", "big-model", "openrouter", "aux", 4, 0, 0, 0.0),
        ],
    )
    con.executemany(
        "insert into gateway_heartbeats values (?,?,?,?,?,?)",
        [
            ("default@host-a", 4242, NOW - 600, NOW - 30, "default", "host-a"),
            ("default@host-b", 4242, NOW - 4000, NOW - 3600, "default", "host-b"),
        ],
    )
    con.commit()
    con.close()


def make_logs(root: Path) -> Path:
    """Create a logs directory with a recurring error and credential-shaped lines."""
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "gateway.error.log").write_text(
        "2026-09-17 10:00:01,123 WARNING tools.mcp_tool: MCP server 'obsidian' "
        "failed after 5 reconnection attempts, parking\n"
        "2026-09-17 10:05:02,456 WARNING tools.mcp_tool: MCP server 'obsidian' "
        "failed after 5 reconnection attempts, parking\n"
        f"2026-09-17 10:06:00,000 INFO agent.provider: using {FAKE_KEY} for requests\n"
        f"2026-09-17 10:07:00,000 ERROR tools.browser: header {FAKE_BEARER}\n"
        f"2026-09-17 10:08:00,000 ERROR agent.config: bad config {FAKE_TOKEN}\n"
        "2026-09-17 10:09:00,000 INFO agent.turn: finished cleanly\n",
        encoding="utf-8",
    )
    (logs / "agent.log.1").write_text(
        "2026-09-01 09:00:00,000 ERROR agent.tools: read_file returned error\n"
        "2026-09-01 09:00:01,000 INFO agent.turn: recovered\n",
        encoding="utf-8",
    )
    return logs


def make_launch_agents(root: Path) -> Path:
    """Create a LaunchAgents directory with one Hermes service."""
    agents = root / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    with (agents / "com.hermes.dashboard.plist").open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.hermes.dashboard",
                "ProgramArguments": [
                    "/tmp/hermes",
                    "dashboard",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9119",
                ],
                "RunAtLoad": True,
                "KeepAlive": True,
                "StandardOutPath": str(root / "logs" / "dashboard.out.log"),
                "StandardErrorPath": str(root / "logs" / "dashboard.err.log"),
                "WorkingDirectory": "/tmp",
                "ThrottleInterval": 5,
            },
            handle,
        )
    with (agents / "com.something.else.plist").open("wb") as handle:
        plistlib.dump(
            {"Label": "com.something.else", "ProgramArguments": ["/bin/true"]}, handle
        )
    return agents


def make_cron_store(root: Path) -> None:
    """A cron store with one job and fresh ticker stamps."""
    cron = root / "cron"
    (cron / "output" / "job000000001").mkdir(parents=True, exist_ok=True)
    (cron / "jobs.json").write_text(
        '{"jobs": [{"id": "job000000001", "name": "job"}]}', encoding="utf-8"
    )
    con = sqlite3.connect(cron / "executions.db")
    con.executescript("create table executions (id integer primary key, job_id text);")
    con.execute("insert into executions (id, job_id) values (1, 'job000000001')")
    con.commit()
    con.close()
    # derived from the real clock so "fresh" stays fresh as time passes
    now = time.time()
    (cron / "ticker_heartbeat").write_text(str(now), encoding="utf-8")
    (cron / "ticker_last_success").write_text(str(now - 7200), encoding="utf-8")


def build_root(root: Path) -> Path:
    """A Hermes root with everything the P1 domains read."""
    root.mkdir(parents=True, exist_ok=True)
    make_state_db(root / "state.db")
    make_cron_store(root)
    make_logs(root)
    (root / "skills" / "creative" / "alpha").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "creative" / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: d\n---\n\n# Alpha\n", encoding="utf-8"
    )
    (root / "profiles" / "other" / "skills").mkdir(parents=True, exist_ok=True)
    return root


class BaseP1(unittest.TestCase):
    """Shared temporary root."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestUsageDomain(BaseP1):
    """Cost and token rollups from state.db."""

    def setUp(self) -> None:
        super().setUp()
        self.domain = usage_domain.build_domain(hermes_home=self.root)

    def test_overview_counts_the_sessions_its_totals_cover(self) -> None:
        overview = self.domain.overview()
        self.assertEqual(overview.count.value, 4)  # five sessions, one unpriced
        self.assertIn("cost estimate", overview.count.definition)
        metrics = dict(overview.metrics)
        self.assertEqual(metrics["Estimated cost"], "$1.6800")
        self.assertEqual(metrics["Input tokens"], "11,190")
        self.assertEqual(metrics["Output tokens"], "4,995")
        self.assertEqual(metrics["API calls"], "36")
        self.assertTrue(
            any("carry no cost estimate" in note for note in overview.notes)
        )

    def test_by_day_groups_on_the_local_date(self) -> None:
        by_day = next(c for c in self.domain.collections() if c.key == "by-day")
        self.assertEqual(by_day.count.value, 2)  # today and yesterday
        days = [record.id for record in by_day.records]
        self.assertEqual(len(days), 2)
        self.assertEqual(by_day.records[0].id, max(days))

    def test_by_model_rolls_up_and_links_to_sessions(self) -> None:
        by_model = next(c for c in self.domain.collections() if c.key == "by-model")
        self.assertEqual(by_model.count.value, 2)
        titles = [record.title for record in by_model.records]
        self.assertEqual(titles[0], "big-model")  # biggest spend first
        big = by_model.records[0]
        self.assertEqual(dict(big.fields)["API calls"], "34")
        self.assertEqual(big.href, "/sessions?model=big-model")

    def test_by_provider_rolls_up_and_links(self) -> None:
        by_provider = next(
            c for c in self.domain.collections() if c.key == "by-provider"
        )
        self.assertEqual(by_provider.count.value, 3)
        providers = {record.title for record in by_provider.records}
        self.assertEqual(providers, {"deepseek", "openrouter", "custom"})

    def test_top_sessions_ranked_and_linked(self) -> None:
        top = next(c for c in self.domain.collections() if c.key == "top-sessions")
        self.assertEqual(top.count.value, 4)
        self.assertEqual(top.records[0].id, "s2")
        self.assertEqual(top.records[0].links[0][0], "/sessions/s2")

    def test_detail_and_sections_reach_the_contributing_sessions(self) -> None:
        record = self.domain.detail("big-model")
        self.assertIsNotNone(record)
        sections = self.domain.detail_sections("big-model")
        self.assertEqual(sections[0].count.value, 2)
        self.assertEqual({r.id for r in sections[0].records}, {"s2", "s3"})

    def test_search_finds_a_model(self) -> None:
        hits = self.domain.search("big", 5)
        self.assertTrue(any(hit.title == "big-model" for hit in hits))
        self.assertEqual(self.domain.search("", 5), [])


class TestHealthDomain(BaseP1):
    """Services, heartbeats, ports, tickers, storage -- outside world patched."""

    def setUp(self) -> None:
        super().setUp()
        self.agents = make_launch_agents(self.root / "fakehome")
        self._patches = [
            mock.patch.object(health_domain, "LAUNCH_AGENTS", self.agents),
            mock.patch.object(health_domain, "_probe", return_value=True),
            mock.patch.object(
                health_domain,
                "run_argv",
                side_effect=self._fake_command,
            ),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.domain = health_domain.build_domain(hermes_home=self.root)

    @staticmethod
    def _fake_command(argv: list[str], timeout: float = 0.0) -> tuple[str, str]:
        """Stand in for launchctl and lsof."""
        del timeout
        if argv[0] == "launchctl":
            return (
                "PID\tStatus\tLabel\n"
                "4242\t0\tcom.hermes.dashboard\n"
                "9999\t-15\tcom.something.else\n",
                "",
            )
        if argv[0] == "lsof":
            return (
                "COMMAND   PID USER   FD   TYPE  DEVICE SIZE/OFF NODE NAME\n"
                "Python  4242  ron    3u  IPv4  0x1234      0t0  TCP "
                "127.0.0.1:9119 (LISTEN)\n",
                "",
            )
        return "", f"unexpected command {argv[0]}"

    def test_services_are_joined_with_their_live_state(self) -> None:
        services = next(c for c in self.domain.collections() if c.key == "services")
        self.assertEqual(services.count.value, 1)  # only the hermes-labelled plist
        record = services.records[0]
        fields = dict(record.fields)
        self.assertEqual(fields["loaded"], "yes")
        self.assertEqual(fields["pid"], "4242")
        self.assertEqual(fields["declared port"], "9119")
        self.assertEqual(fields["listening now"], "yes")
        self.assertIn("running", record.badges)
        self.assertTrue(any("port 9119: listening" in badge for badge in record.badges))

    def test_heartbeats_separate_fresh_from_stale(self) -> None:
        beats = next(c for c in self.domain.collections() if c.key == "heartbeats")
        self.assertEqual(beats.count.value, 2)
        extras = count_map(beats.extra_counts)
        self.assertEqual(extras["heartbeat rows in total"], 2)
        self.assertEqual(extras["stale (over 10 min)"], 1)
        self.assertTrue(any("STALE" in " ".join(r.badges) for r in beats.records))

    def test_ports_are_parsed_from_lsof(self) -> None:
        ports = next(c for c in self.domain.collections() if c.key == "ports")
        self.assertEqual(ports.count.value, 1)
        self.assertIn("127.0.0.1:9119", ports.records[0].title)
        self.assertEqual(dict(ports.records[0].fields)["pid"], "4242")

    def test_tickers_report_age(self) -> None:
        tickers = next(c for c in self.domain.collections() if c.key == "tickers")
        self.assertEqual(tickers.count.value, 2)
        badges = {record.id: " ".join(record.badges) for record in tickers.records}
        self.assertIn("fresh", badges["ticker heartbeat"])
        self.assertIn("STALE", badges["last successful tick"])

    def test_storage_lists_the_stores_that_exist(self) -> None:
        storage = next(c for c in self.domain.collections() if c.key == "storage")
        labels = {record.id for record in storage.records}
        self.assertIn("state.db", labels)
        self.assertNotIn("code graph", labels)  # absent here, so not claimed
        self.assertEqual(storage.count.value, len(labels))

    def test_service_detail_sections_scrub_their_log_tail(self) -> None:
        err_log = self.root / "fakehome" / "logs" / "dashboard.err.log"
        err_log.parent.mkdir(parents=True, exist_ok=True)
        err_log.write_text(
            f"2026-09-17 10:00:00,000 ERROR boot failed {FAKE_KEY}\n", encoding="utf-8"
        )
        sections = self.domain.detail_sections("com.hermes.dashboard")
        keys = [section.key for section in sections]
        self.assertIn("stderr", keys)
        stderr = next(section for section in sections if section.key == "stderr")
        self.assertEqual(stderr.count.value, 1)
        body = stderr.records[0].body
        self.assertNotIn(FAKE_KEY, body)
        self.assertIn("<redacted>", body)

    def test_overview_counts_the_services_it_samples(self) -> None:
        overview = self.domain.overview()
        services = next(c for c in self.domain.collections() if c.key == "services")
        self.assertEqual(overview.count.value, services.count.value)
        metrics = dict(overview.metrics)
        self.assertEqual(metrics["Services running"], "1")
        self.assertEqual(metrics["Stale heartbeats"], "1")
        self.assertEqual(metrics["Cron ticker"], "fresh")

    def test_search_finds_a_service(self) -> None:
        hits = self.domain.search("dashboard", 5)
        self.assertTrue(any(hit.title == "com.hermes.dashboard" for hit in hits))


class TestLogsDomain(BaseP1):
    """Log tails, grouped signatures, and the scrubber on every rendering path."""

    def setUp(self) -> None:
        super().setUp()
        self.library = self.root / "library-logs"
        self.library.mkdir(exist_ok=True)
        self._patcher = mock.patch.object(logs_domain, "LIBRARY_LOGS", self.library)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.domain = logs_domain.build_domain(self.root)

    def test_files_are_summarised_from_the_tail_window(self) -> None:
        files = next(c for c in self.domain.collections() if c.key == "files")
        self.assertEqual(files.count.value, 2)
        gateway = next(
            record for record in files.records if record.id == "gateway.error.log"
        )
        fields = dict(gateway.fields)
        # two WARNINGs about the MCP server, one bearer-header ERROR, one config ERROR
        self.assertEqual(fields["error-ish lines"], "4")
        self.assertIn("rotated", " ".join(files.records[1].badges))

    def test_signatures_merge_timestamps_and_numbers(self) -> None:
        signatures = next(c for c in self.domain.collections() if c.key == "signatures")
        titles = [record.title for record in signatures.records]
        mcp = next(title for title in titles if "obsidian" in title)
        record = next(r for r in signatures.records if r.title == mcp)
        self.assertIn("x2", record.badges)
        self.assertIn("reconnection attempts", record.title)
        self.assertNotIn("2026-09-17", record.title)  # timestamp stripped
        self.assertNotIn("5", record.title)  # counts stripped, so variants merge
        self.assertNotIn(FAKE_KEY, record.title)
        self.assertGreaterEqual(signatures.count.value, 3)

    def test_every_rendering_path_scrubs_credentials(self) -> None:
        signatures = next(c for c in self.domain.collections() if c.key == "signatures")
        rendered = " ".join(
            f"{record.title} {record.subtitle} {record.body} "
            f"{dict(record.fields)['example']}"
            for record in signatures.records
        )
        self.assertNotIn(FAKE_KEY, rendered)
        self.assertNotIn(FAKE_TOKEN, rendered)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6", rendered)
        self.assertIn("<redacted>", rendered)

    def test_detail_shows_only_the_tail_and_scrubs_it(self) -> None:
        record = self.domain.detail("gateway.error.log")
        self.assertIsNotNone(record)
        self.assertIn("gateway.error.log", record.title)
        self.assertNotIn(FAKE_KEY, record.body)
        self.assertNotIn(FAKE_TOKEN, record.body)
        self.assertIn("<redacted>", record.body)
        self.assertIn("finished cleanly", record.body)

    def test_detail_sections_list_the_error_lines_scrubbed(self) -> None:
        sections = self.domain.detail_sections("gateway.error.log")
        self.assertEqual([section.key for section in sections], ["errors"])
        errors = sections[0]
        self.assertEqual(errors.count.value, 4)
        joined = " ".join(record.body for record in errors.records)
        self.assertNotIn(FAKE_TOKEN, joined)

    def test_search_greps_the_windows_and_scrubs_bodies(self) -> None:
        hits = self.domain.search("obsidian", 5)
        self.assertTrue(hits)
        self.assertIn("gateway.error.log", hits[0].subtitle)
        keys = self.domain.search("sk-ABCDEF", 5)
        self.assertTrue(keys)
        self.assertNotIn(FAKE_KEY, keys[0].body)
        self.assertEqual(self.domain.search("", 5), [])

    def test_a_missing_log_root_is_a_source_that_says_missing(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            domain = logs_domain.build_domain(Path(empty))
            overview = domain.overview()
            self.assertEqual(overview.count.value, 0)
            sources_seen = {source.label: source.present for source in overview.sources}
            self.assertEqual(len(sources_seen), 2)
            # the hermes logs dir does not exist; the library root does, but holds
            # nothing that matches -- two different kinds of empty, both reported
            self.assertFalse(sources_seen["hermes logs"])
            self.assertTrue(sources_seen["library logs"])
            self.assertEqual(
                overview.count.definition, "log files in scope (read from the end)"
            )


# The one place the portal's promised planes are enumerated.  Everything else asserts
# a property (membership, or that the index agrees with the registry), so adding a
# domain does not mean editing four literal lists -- and a test that fails when data
# expected to change is updated is not a test worth keeping.
EXPECTED_DOMAINS = (
    "cron",
    "graph",
    "health",
    "logs",
    "memory",
    "plugins",
    "sessions",
    "skills",
    "usage",
    "vault",
)


class TestRegistryInvariants(BaseP1):
    """Whole-system checks that hold for every domain and collection."""

    def setUp(self) -> None:
        super().setUp()
        self._patches = [
            mock.patch.object(
                health_domain, "LAUNCH_AGENTS", self.root / "nothing-here"
            ),
            mock.patch.object(health_domain, "run_argv", return_value=("", "no tool")),
            mock.patch.object(
                logs_domain, "LIBRARY_LOGS", self.root / "no-library-logs"
            ),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.registry = default_registry(hermes_home=self.root)

    def test_all_domains_are_registered(self) -> None:
        """The promised planes, in one place (see EXPECTED_DOMAINS)."""
        self.assertEqual(
            self.registry.keys(),
            list(EXPECTED_DOMAINS),
        )

    def test_every_collection_count_matches_its_records(self) -> None:
        """A count is the size of its set: equal when nothing is capped, never less."""
        checked = 0
        for domain in self.registry.all():
            for collection in self.registry.safe_collections(domain):
                self.assertGreaterEqual(
                    collection.count.value,
                    collection.shown,
                    f"{domain.key}/{collection.key}: count below the records shown",
                )
                if not collection.truncated:
                    self.assertEqual(
                        collection.count.value,
                        collection.shown,
                        f"{domain.key}/{collection.key}: untruncated count mismatch",
                    )
                self.assertTrue(
                    collection.count.definition.strip(),
                    f"{domain.key}/{collection.key}: count has no definition",
                )
                checked += 1
        self.assertGreater(checked, 10)

    def test_every_collection_reports_sources_and_a_stamp(self) -> None:
        for domain in self.registry.all():
            overview = self.registry.safe_overview(domain)
            self.assertTrue(overview.sources, f"{domain.key}: no sources")
            self.assertTrue(overview.as_of, f"{domain.key}: no as-of stamp")

    def test_a_broken_domain_still_leaves_the_portal_usable(self) -> None:
        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("adapter blew up")

        broken = Domain(
            key="broken",
            title="Broken",
            summary="s",
            overview=explode,  # type: ignore[arg-type]
            collections=explode,  # type: ignore[arg-type]
            detail=lambda _rid: None,
            search=lambda _q, _l: [],
        )
        self.registry.register(broken)
        overview = self.registry.safe_overview(broken)
        self.assertIn("RuntimeError: adapter blew up", overview.notes)
        for domain in self.registry.all():
            if domain.key != "broken":
                self.registry.safe_overview(domain)

    def test_search_reaches_every_domain(self) -> None:
        groups = self.registry.search("model", 5)
        self.assertEqual(sorted(groups), self.registry.keys())
        self.assertTrue(groups["usage"], "usage search found nothing")


class TestScrubber(unittest.TestCase):
    """The scrubber itself: shapes masked, ordinary text untouched."""

    def test_masks_keys_tokens_and_bearers(self) -> None:
        text = sources.scrub(
            f"key {FAKE_KEY} header {FAKE_BEARER} config {FAKE_TOKEN} "
            "password=hunter2 token: abc123"
        )
        for secret in (
            FAKE_KEY,
            FAKE_TOKEN,
            "eyJhbGciOiJIUzI1NiIsInR5cCI6",
            "hunter2",
            "abc123",
        ):
            self.assertNotIn(secret, text)
        self.assertIn("<redacted>", text)

    def test_leaves_normal_log_text_alone(self) -> None:
        text = "2026-09-17 10:00:00,000 INFO agent.turn: finished cleanly"
        self.assertEqual(sources.scrub(text), text)

    def test_age_and_tail_helpers(self) -> None:
        self.assertIsNone(sources.age_seconds(None))
        self.assertIsNone(sources.age_seconds("not a time"))
        self.assertGreater(sources.age_seconds(NOW - 60) or 0, 60)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log"
            path.write_text("x" * 500, encoding="utf-8")
            text, truncated, error = sources.tail_text(path, 100)
            self.assertEqual(len(text), 100)
            self.assertTrue(truncated)
            self.assertEqual(error, "")
            _text, _truncated, error = sources.tail_text(Path(tmp) / "nope", 10)
            self.assertIn("cannot stat", error)

    def test_run_argv_never_uses_a_shell_and_reports_failure(self) -> None:
        output, error = sources.run_argv(["/bin/echo", "hello world"])
        self.assertEqual(output.strip(), "hello world")
        self.assertEqual(error, "")
        _output, error = sources.run_argv(["/nonexistent/binary"])
        self.assertIn("failed", error)


class ConvertedDomainsPrimitivesTestCase(BaseP1):
    """The primitives of the three domains whose factories became classes.

    ``usage``, ``health`` and ``sessions`` were closures inside 458-, 486- and 443-line
    factories, so no test could name one.  They are methods now, and this calls them --
    ``_open``/``_sources``/``_notes`` and every collection builder.
    """

    def setUp(self) -> None:
        super().setUp()
        make_launch_agents(self.root)
        self.usage = usage_domain.UsageDomain(hermes_home=self.root)
        self.health = health_domain.HealthDomain(hermes_home=self.root)
        self.sessions = sessions_domain.SessionsDomain(hermes_home=self.root)

    def test_usage_open_is_read_only_and_sources_names_the_store(self) -> None:
        con, error = self.usage._open()
        self.assertFalse(error)
        assert con is not None
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("delete from sessions")
        con.close()
        sources = self.usage._sources()
        self.assertEqual(len(sources), 1)
        self.assertIn("state.db", sources[0].label)

    def test_usage_preamble_locals_are_attributes_now(self) -> None:
        self.assertEqual(self.usage.db_path, self.root / "state.db")
        self.assertEqual(self.usage.source.label, "state.db")

    def test_health_preamble_locals_are_attributes_now(self) -> None:
        self.assertEqual(self.health.root, self.root)
        self.assertEqual(self.health.state_db, self.root / "state.db")
        self.assertTrue(str(self.health.cron_root).endswith("cron"))

    def test_every_health_builder_counts_what_it_carries(self) -> None:
        for collection in (
            self.health._services_collection(),
            self.health._heartbeats_collection(),
            self.health._ports_collection(),
            self.health._storage_collection(),
            self.health._tickers_collection(),
        ):
            with self.subTest(collection=collection.key):
                if not collection.truncated:
                    self.assertEqual(collection.count.value, len(collection.records))
        # the fixture writes a LaunchAgent, so the services collection is not empty
        self.assertTrue(self.health._services_collection().records)

    def test_every_usage_builder_counts_what_it_carries(self) -> None:
        for collection in (
            self.usage._by_day(),
            self.usage._by_model(),
            self.usage._by_provider(),
            self.usage._top_sessions(),
        ):
            with self.subTest(collection=collection.key):
                self.assertTrue(collection.records)
                if not collection.truncated:
                    self.assertEqual(collection.count.value, len(collection.records))

    def test_sessions_open_and_sources_name_the_store(self) -> None:
        con, error = self.sessions._open()
        self.assertFalse(error)
        assert con is not None
        con.close()
        self.assertEqual(self.sessions.db_path, self.root / "state.db")
        self.assertEqual(len(self.sessions._sources()), 1)

    def test_sessions_notes_explain_a_missing_store(self) -> None:
        """A page with no store must say why rather than render an empty table."""
        notes = self.sessions._notes(None, "unable to open database file")
        self.assertTrue(notes)
        self.assertTrue(any("unable to open" in note for note in notes))

    def test_the_sessions_filter_narrows_and_the_builders_count_it(self) -> None:
        everything = self.sessions._sessions_collection()
        narrowed = self.sessions._sessions_collection(model="nothing-like-this")
        self.assertTrue(everything.records)
        self.assertEqual(narrowed.records, ())
        self.assertEqual(narrowed.count.value, 0)
        messages = self.sessions._messages_collection()
        self.assertEqual(messages.count.value, len(messages.records))

    def test_each_converted_domain_still_builds_a_wired_domain(self) -> None:
        for module, kwargs in (
            (usage_domain, {"hermes_home": self.root}),
            (health_domain, {"hermes_home": self.root}),
            (sessions_domain, {"hermes_home": self.root}),
        ):
            with self.subTest(domain=module.__name__):
                domain = module.build_domain(**kwargs)
                self.assertTrue(domain.key)
                self.assertEqual(domain.overview().key, "overview")


class CronPrimitivesTestCase(BaseP1):
    """The cron domain's primitives by name -- the last factory to become a class."""

    def setUp(self) -> None:
        super().setUp()
        self.cron = cron_domain.CronDomain(hermes_home=self.root)

    def test_preamble_locals_are_attributes_now(self) -> None:
        self.assertTrue(str(self.cron.cron_root).endswith("cron"))
        self.assertEqual(self.cron.jobs_path, self.cron.cron_root / "jobs.json")
        self.assertEqual(self.cron.exec_path, self.cron.cron_root / "executions.db")
        self.assertEqual(self.cron.output_dir, self.cron.cron_root / "output")

    def test_sources_name_the_store_it_reads(self) -> None:
        sources = self.cron._sources()
        self.assertTrue(sources)
        self.assertTrue(any("jobs.json" in source.label for source in sources))

    def test_jobs_and_notes_returns_both_halves(self) -> None:
        jobs, notes = self.cron._jobs_and_notes()
        self.assertTrue(jobs)
        self.assertIsInstance(notes, tuple)

    def test_every_collection_builder_counts_what_it_carries(self) -> None:
        for collection in (
            self.cron._jobs_collection(),
            self.cron._runs_collection(),
            self.cron._incidents_collection(),
        ):
            with self.subTest(collection=collection.key):
                if not collection.truncated:
                    self.assertEqual(collection.count.value, len(collection.records))
        self.assertTrue(self.cron._jobs_collection().records)

    def test_a_runs_collection_can_be_narrowed_to_one_job(self) -> None:
        job = self.cron._jobs_collection().records[0]
        narrowed = self.cron._runs_collection(job_id=job.id)
        self.assertLessEqual(
            narrowed.count.value, self.cron._runs_collection().count.value
        )

    def test_build_domain_still_returns_a_wired_domain(self) -> None:
        domain = cron_domain.build_domain(hermes_home=self.root)
        self.assertEqual(domain.key, "cron")
        self.assertEqual(domain.overview().key, "overview")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FailureAndSignatureTestCase(BaseP1):
    """The two things no test named: scrubbed adapter failures, and the signature
    grouping that the code graph listed as the one method nothing called."""

    def test_a_failed_collection_is_scrubbed(self) -> None:
        """Adapter errors reach the page, so they go through the same guard as logs."""
        collection = failed_collection(
            "logs", "Logs", "connect failed: api_key=abcdef123456", as_of="STAMP"
        )
        note = collection.notes[0]
        self.assertIn("<redacted>", note)
        self.assertNotIn("abcdef123456", note)
        self.assertIn("connect failed", note)
        self.assertEqual(collection.count.value, 0)

    def test_the_signature_collection_groups_by_shape(self) -> None:
        library = self.root / "library-logs"
        library.mkdir(exist_ok=True)
        with mock.patch.object(logs_domain, "LIBRARY_LOGS", library):
            domain = logs_domain.LogsDomain(hermes_home_override=self.root)
            scans = domain._scans()
            collection = domain._signatures_collection(scans)
        self.assertEqual(collection.key, "signatures")
        self.assertTrue(collection.records)
        self.assertTrue(all(record.title for record in collection.records))
        # the count is the size of the set the label describes, truncated or not
        if collection.truncated:
            self.assertGreaterEqual(collection.count.value, len(collection.records))
        if not collection.truncated:
            self.assertEqual(collection.count.value, len(collection.records))
