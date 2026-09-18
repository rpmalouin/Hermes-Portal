"""Tests for the Hermes Portal: model, sources, adapters, rendering, server.

Everything here is hermetic.  The fixtures build a *fake* Hermes root -- its own
``state.db``, ``cron/`` store and skill trees -- so the suite never reads the
real 126 MB state database, the real cron history or the real skills tree, and
never depends on what this machine happens to contain.

The headline safety claim is tested rather than asserted: after exercising every
route, the fake Hermes tree is compared byte-for-byte with its state before the
run, so a stray write anywhere shows up as a failure.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes.portal import render, server, sources  # noqa: E402
from hermes.portal.domains import cron as cron_domain  # noqa: E402
from hermes.portal.domains import health as health_domain  # noqa: E402
from hermes.portal.domains import logs as logs_domain  # noqa: E402
from hermes.portal.domains import sessions as sessions_domain  # noqa: E402
from hermes.portal.domains import skills as skills_domain  # noqa: E402
from hermes.portal.model import (  # noqa: E402
    Collection,
    Count,
    Domain,
    DomainRegistry,
    Record,
    build_collection,
    clone_collection,
    count_map,
    failed_collection,
)

JOB_ID = "job000000001"
BROKEN_ID = "job000000002"
SESSION_A = "20260917_100000_aaaaaa"
SESSION_B = "20260917_110000_bbbbbb"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def write_skill(
    parent: Path, name: str, *, box: str | None = None, body: str = ""
) -> Path:
    """Create a SKILL.md under ``parent/<box>/<name>`` (or ``parent/<name>``)."""
    directory = parent / box / name if box else parent / name
    directory.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        f"name: {name}\n"
        f"description: The {name} skill does {name} things.\n"
        "metadata:\n  hermes:\n    tags: [demo]\n"
        "---\n\n"
        f"# {name.replace('-', ' ').title()}\n\n{body or 'Body text.'}\n"
    )
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


def make_state_db(path: Path, *, with_fts: bool = True) -> None:
    """Create a state.db with the column subset the adapter selects from."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table sessions (
            id text primary key, title text, model text, billing_provider text,
            source text, profile_name text, started_at real, ended_at real,
            message_count integer, tool_call_count integer, input_tokens integer,
            output_tokens integer, estimated_cost_usd real, actual_cost_usd real,
            cost_status text, cwd text, git_branch text, archived integer,
            hidden integer, pinned integer, tool_names text, display_name text,
            end_reason text, api_call_count integer, cache_read_tokens integer,
            cache_write_tokens integer, reasoning_tokens integer,
            git_repo_root text, last_activity_description text
        );
        create table messages (
            id integer primary key, session_id text, role text, content text,
            tool_name text, timestamp real, token_count integer,
            finish_reason text, compacted integer, active integer
        );
        create table session_model_usage (
            session_id text, model text, billing_provider text, task text,
            api_call_count integer, input_tokens integer, output_tokens integer,
            cache_read_tokens integer, cache_write_tokens integer,
            reasoning_tokens integer, estimated_cost_usd real, actual_cost_usd real,
            first_seen real, last_seen real
        );
        """
    )
    if with_fts:
        con.executescript("create virtual table messages_fts using fts5(content);")
    con.execute(
        "insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?,?,?)",
        (
            SESSION_A,
            "Fix the thing",
            "gemini-3.6-flash",
            "gemini",
            "desktop",
            "default",
            1758100000.0,
            1758100600.0,
            2,
            1,
            1000,
            500,
            0.0123,
            0.01,
            "estimated",
            "/tmp/proj",
            "main",
            0,
            0,
            0,
            "terminal,read_file",
            "Fix the thing",
            "done",
            4,
            100,
            200,
            50,
            "/tmp/proj",
            "finished the fix",
        ),
    )
    con.execute(
        "insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?,?,?)",
        (
            SESSION_B,
            "Write the docs",
            "deepseek-flash",
            "deepseek",
            "cli",
            "default",
            1758103600.0,
            1758104200.0,
            1,
            0,
            200,
            100,
            0.0021,
            None,
            "estimated",
            "/tmp/docs",
            "docs",
            1,
            1,
            0,
            "terminal",
            "Write the docs",
            "done",
            2,
            0,
            0,
            0,
            "/tmp/docs",
            "wrote the docs",
        ),
    )
    messages = [
        (
            1,
            SESSION_A,
            "user",
            "please fix the cron parser",
            None,
            1758100010.0,
            12,
            "stop",
            0,
            1,
        ),
        (
            2,
            SESSION_A,
            "assistant",
            "cron parser fixed and committed",
            "terminal",
            1758100060.0,
            40,
            "stop",
            0,
            1,
        ),
        (
            3,
            SESSION_B,
            "user",
            "draft the release notes",
            None,
            1758103610.0,
            9,
            "stop",
            0,
            1,
        ),
    ]
    con.executemany("insert into messages values (?,?,?,?,?,?,?,?,?,?)", messages)
    if with_fts:
        con.executemany(
            "insert into messages_fts(rowid, content) values (?, ?)",
            [(row[0], row[3]) for row in messages],
        )
    con.executemany(
        "insert into session_model_usage values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                SESSION_A,
                "gemini-3.6-flash",
                "gemini",
                "chat",
                4,
                1000,
                500,
                100,
                200,
                50,
                0.0123,
                0.01,
                1758100000.0,
                1758100600.0,
            ),
            (
                SESSION_B,
                "deepseek-flash",
                "deepseek",
                "chat",
                2,
                200,
                100,
                0,
                0,
                0,
                0.0021,
                None,
                1758103600.0,
                1758104200.0,
            ),
        ],
    )
    con.commit()
    con.close()


def make_cron(root: Path) -> None:
    """Create the cron store: jobs.json, executions.db and one output report."""
    cron = root / "cron"
    (cron / "output" / JOB_ID).mkdir(parents=True, exist_ok=True)
    jobs = {
        "jobs": [
            {
                "id": JOB_ID,
                "name": "nightly-report",
                "enabled": True,
                "script": "scripts/report.sh",
                "prompt": "Summarise the day and write it to the vault.",
                "schedule": {"kind": "cron", "minutes": None, "display": "0 3 * * *"},
                "schedule_display": "0 3 * * *",
                "state": "scheduled",
                "last_status": "ok",
                "last_run_at": "2026-09-17T07:00:00Z",
                "next_run_at": "2026-09-18T07:00:00Z",
                "failure_streak": 0,
                "deliver": "local",
                "skills": ["hermes-activity-kanban"],
                "repeat": {"times": None, "completed": 3},
                "enabled_toolsets": ["terminal"],
                "created_at": "2026-08-01T00:00:00Z",
            },
            {
                "id": BROKEN_ID,
                "name": "disabled-job",
                "enabled": False,
                "prompt": "nothing",
            },
        ],
        "updated_at": "2026-09-17T12:00:00Z",
    }
    (cron / "jobs.json").write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    (cron / "output" / JOB_ID / "2026-09-17_07-00-00.md").write_text(
        "# nightly report\n\nEverything was fine.\n", encoding="utf-8"
    )
    con = sqlite3.connect(cron / "executions.db")
    con.executescript(
        """
        create table executions (
            id integer primary key, job_id text, source text, status text,
            started_at real, finished_at real, scheduled_instant real, pid integer,
            error text, delivery_outcome text, process_id text,
            process_started_at real, claimed_at real, handoff_pending integer,
            handoff_started_at real
        );
        create table cron_incidents (
            id integer primary key, job_id text, error_sig text, state text,
            failure_type text, first_seen_at real, last_seen_at real, acked_at real,
            closed_at real, error text, output_file text
        );
        """
    )
    con.executemany(
        "insert into executions (id, job_id, source, status, started_at, finished_at, "
        "scheduled_instant, pid, error, delivery_outcome) values (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                1,
                JOB_ID,
                "builtin",
                "completed",
                1758092400.0,
                1758092412.0,
                1758092400.0,
                4242,
                None,
                "delivered",
            ),
            (
                2,
                JOB_ID,
                "builtin",
                "completed",
                1758178800.0,
                1758178799.0,
                1758178800.0,
                4243,
                None,
                "delivered",
            ),
            (
                3,
                JOB_ID,
                "direct",
                "failed",
                1758265200.0,
                1758265205.0,
                1758265200.0,
                4244,
                "boom",
                None,
            ),
        ],
    )
    con.execute(
        "insert into cron_incidents (id, job_id, error_sig, state, failure_type, "
        "first_seen_at, last_seen_at, error, output_file) values (?,?,?,?,?,?,?,?,?)",
        (
            1,
            JOB_ID,
            "boom-signature",
            "resolved",
            "exec_error",
            1758265200.0,
            1758265300.0,
            "boom",
            "out.md",
        ),
    )
    con.commit()
    con.close()


def make_hermes_root(root: Path, *, with_fts: bool = True) -> Path:
    """Build a fake Hermes root containing both roots, skills, state.db and cron."""
    root.mkdir(parents=True, exist_ok=True)
    shared = root / "skills"
    write_skill(shared, "alpha", box="creative", body="Alpha body text.")
    write_skill(shared, "beta", box="creative")
    write_skill(shared, "gamma", box="software-development")
    references = shared / "software-development" / "gamma" / "references"
    references.mkdir(parents=True, exist_ok=True)
    (references / "notes.md").write_text("# notes\n", encoding="utf-8")
    write_skill(root / "profiles" / "other" / "skills", "delta", box="creative")
    make_state_db(root / "state.db", with_fts=with_fts)
    make_cron(root)
    return root


def tree_snapshot(root: Path) -> dict[str, str]:
    """Hash every file under *root* so a stray write can be detected."""
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[str(path.relative_to(root))] = f"{digest}:{path.stat().st_size}"
    return snapshot


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


class TestModel(unittest.TestCase):
    """The generic vocabulary: counts, collections, domains and the registry."""

    def test_count_carries_its_definition(self) -> None:
        count = Count(157, "unique frontmatter names")
        self.assertEqual(str(count), "157 (unique frontmatter names)")

    def test_build_collection_counts_everything_it_was_given(self) -> None:
        records = [Record(id=str(n), title=f"r{n}") for n in range(10)]
        collection = build_collection("k", "T", "d", "the rule", records, cap=3)
        self.assertEqual(collection.count.value, 10)
        self.assertEqual(collection.shown, 3)
        self.assertTrue(collection.truncated)
        self.assertEqual(collection.count.definition, "the rule")

    def test_a_collection_within_its_cap_is_not_truncated(self) -> None:
        collection = build_collection(
            "k", "T", "d", "rule", [Record(id="1", title="x")]
        )
        self.assertFalse(collection.truncated)
        self.assertEqual(collection.shown, 1)

    def test_failed_collection_reports_instead_of_raising(self) -> None:
        collection = failed_collection("skills", "Skills", "ValueError: nope")
        self.assertEqual(collection.count.value, 0)
        self.assertIn("ValueError: nope", collection.notes)

    def test_registry_rejects_duplicate_keys_and_unknown_lookups(self) -> None:
        registry = DomainRegistry()
        registry.register(self.stub_domain("a"))
        with self.assertRaises(ValueError):
            registry.register(self.stub_domain("a"))
        with self.assertRaises(KeyError):
            registry.get("nope")
        self.assertEqual(registry.keys(), ["a"])

    def test_registry_turns_an_adapter_exception_into_a_note(self) -> None:
        def explode(*_args: object, **_kwargs: object) -> Collection:
            raise RuntimeError("adapter is broken")

        domain = Domain(
            key="bad",
            title="Bad",
            summary="s",
            overview=explode,
            collections=explode,
            detail=lambda _rid: None,
            search=lambda _q, _l: [],
        )
        registry = DomainRegistry()
        registry.register(domain)
        overview = registry.safe_overview(domain)
        self.assertEqual(overview.count.value, 0)
        self.assertIn("RuntimeError: adapter is broken", overview.notes)
        collections = registry.safe_collections(domain)
        self.assertEqual(len(collections), 1)
        self.assertIn("RuntimeError: adapter is broken", collections[0].notes)

    def test_registry_passes_filters_through(self) -> None:
        seen: list[dict[str, str]] = []

        def collections(filters: dict[str, str] | None = None) -> list[Collection]:
            seen.append(dict(filters or {}))
            return [build_collection("k", "T", "d", "rule", [])]

        domain = Domain(
            key="d",
            title="D",
            summary="s",
            overview=lambda: build_collection("o", "O", "d", "rule", []),
            collections=collections,
            detail=lambda _rid: None,
            search=lambda _q, _l: [],
        )
        registry = DomainRegistry()
        registry.register(domain)
        registry.safe_collections(domain, {"box": "creative"})
        self.assertEqual(seen, [{"box": "creative"}])

    def test_registry_survives_search_and_detail_failures(self) -> None:
        def boom_search(_query: str, _limit: int) -> list[Record]:
            raise ValueError("no index")

        def boom_detail(_record_id: str) -> Record | None:
            raise ValueError("broken")

        domain = Domain(
            key="d",
            title="D",
            summary="s",
            overview=lambda: build_collection("o", "O", "d", "rule", []),
            collections=lambda *_a, **_k: [],
            detail=boom_detail,
            search=boom_search,
        )
        registry = DomainRegistry()
        registry.register(domain)
        self.assertEqual(registry.search("x", 5), {"d": ()})
        self.assertIsNone(registry.safe_detail(domain, "id"))

    def test_clone_and_count_map_helpers(self) -> None:
        collection = build_collection("k", "T", "d", "rule", [])
        clone = clone_collection(collection, notes=("hello",))
        self.assertEqual(clone.notes, ("hello",))
        self.assertEqual(collection.notes, ())
        self.assertEqual(count_map([Count(1, "a"), Count(2, "b")]), {"a": 1, "b": 2})

    @staticmethod
    def stub_domain(key: str) -> Domain:
        """A domain that does nothing, for registry tests."""
        empty = lambda **_kwargs: build_collection("o", "O", "d", "rule", [])  # noqa: E731
        return Domain(
            key=key,
            title=key.title(),
            summary="s",
            overview=lambda: build_collection("o", "O", "d", "rule", []),
            collections=empty,
            detail=lambda _rid: None,
            search=lambda _q, _l: [],
        )


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------


class TestSources(unittest.TestCase):
    """Read-only access, root resolution and the formatting helpers."""

    def test_root_is_found_when_handed_a_profile_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_hermes_root(Path(tmp))
            profile = root / "profiles" / "other"
            (profile / "state.db").write_bytes(b"")  # profiles carry their own store
            (profile / "cron").mkdir(exist_ok=True)
            self.assertEqual(sources.hermes_root(profile), root)
            self.assertEqual(sources.hermes_root(root), root)
            self.assertEqual(sources.state_db(profile), root / "state.db")
            self.assertEqual(sources.cron_dir(profile), root / "cron")

    def test_sqlite_is_opened_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            make_state_db(db)
            con, error = sources.open_sqlite(db)
            self.assertEqual(error, "")
            self.assertIsNotNone(con)
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("insert into sessions (id) values ('nope')")
            con.close()

    def test_missing_and_corrupt_sources_are_returned_as_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.db"
            con, error = sources.open_sqlite(missing)
            self.assertIsNone(con)
            self.assertIn("database not found", error)
            self.assertEqual(
                sources.query(None, "select 1"), ([], "no database connection")
            )
            data, json_error = sources.read_json(Path(tmp) / "nope.json")
            self.assertIsNone(data)
            self.assertIn("cannot read", json_error)
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            data, json_error = sources.read_json(bad)
            self.assertIsNone(data)
            self.assertIn("malformed JSON", json_error)

    def test_schema_drift_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            make_state_db(db)
            con, _error = sources.open_sqlite(db)
            self.assertIn("title", sources.table_columns(con, "sessions"))
            self.assertEqual(sources.table_columns(con, "nope"), set())
            self.assertEqual(
                sources.select_columns(con, "sessions", ("id", "invented_column")),
                ["id"],
            )
            self.assertEqual(sources.scalar(con, "select count(*) from sessions"), 2)
            con.close()

    def test_formatting_helpers(self) -> None:
        self.assertEqual(sources.human_size(2048), "2.0 KB")
        self.assertEqual(sources.fmt_duration(None), "\u2014")
        self.assertEqual(sources.fmt_duration(0.5), "500ms")
        self.assertEqual(sources.fmt_duration(90), "1m 30s")
        self.assertEqual(sources.fmt_duration(3700), "1h 01m")
        self.assertEqual(sources.fmt_time(None), "\u2014")
        self.assertEqual(sources.truncate("a" * 50, 10).endswith("\u2026"), True)
        self.assertEqual(sources.snippet("many   spaces   here"), "many spaces here")
        self.assertIn("ago", sources.fmt_ago(1758100000.0))

    def test_as_of_is_an_iso_utc_stamp(self) -> None:
        stamp = sources.as_of()
        self.assertIn("T", stamp)
        self.assertTrue(stamp.endswith("+00:00"))


# --------------------------------------------------------------------------
# adapters
# --------------------------------------------------------------------------


class TestSkillsDomain(unittest.TestCase):
    """The document adapter, including the competing counts it publishes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_hermes_root(Path(self._tmp.name))
        self.domain = skills_domain.build_domain(
            hermes_home=self.root, all_profiles=True
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_counts_unique_names_paths_boxes_and_roots(self) -> None:
        collection = self.domain.collections()[2]
        self.assertEqual(collection.count.value, 4)  # alpha, beta, gamma, delta
        self.assertEqual(
            collection.count.definition, "unique frontmatter names across all roots"
        )
        extras = count_map(collection.extra_counts)
        self.assertIn("boxes (first path component)", extras)
        self.assertEqual(extras["boxes (first path component)"], 2)
        self.assertEqual(extras["unique names across all roots (unfiltered)"], 4)
        self.assertTrue(
            any("SKILL.md files on disk" in definition for definition in extras)
        )

    def test_box_filter_narrows_and_says_so(self) -> None:
        collections = self.domain.collections({"box": "creative"})
        narrowed = collections[-1]
        self.assertEqual(narrowed.count.value, 3)
        self.assertIn("matching 'creative'", narrowed.count.definition)
        self.assertEqual(
            count_map(narrowed.extra_counts)[
                "unique names across all roots (unfiltered)"
            ],
            4,
        )

    def test_unknown_box_filter_reports_zero_instead_of_ignoring_itself(self) -> None:
        collections = self.domain.collections({"box": "no-such-box"})
        narrowed = collections[-1]
        self.assertEqual(narrowed.count.value, 0)
        self.assertIn("matching 'no-such-box'", narrowed.count.definition)
        self.assertTrue(any("nothing matches" in note for note in narrowed.notes))

    def test_detail_has_fields_and_the_document_body(self) -> None:
        record = self.domain.detail("gamma")
        self.assertIsNotNone(record)
        self.assertEqual(record.title, "Gamma")
        fields = dict(record.fields)
        self.assertEqual(fields["box"], "software-development")
        self.assertIn("frontmatter keys read", fields)
        self.assertIn("name", fields["frontmatter keys read"])
        self.assertIn("description", fields["frontmatter keys read"])
        self.assertIn("Body text", record.body)

    def test_detail_sections_show_frontmatter_and_references(self) -> None:
        sections = self.domain.detail_sections("gamma")
        keys = {section.key for section in sections}
        self.assertEqual(keys, {"frontmatter", "references"})
        frontmatter, references = sections
        # name, description and the top-level metadata key (whose nested block is
        # deliberately not read, and says so)
        self.assertEqual(frontmatter.count.value, 3)
        metadata = next(r for r in frontmatter.records if r.id == "metadata")
        self.assertIn("nested block", metadata.subtitle)
        self.assertEqual(references.count.value, 1)
        self.assertEqual(references.records[0].title, "notes.md")
        self.assertEqual(self.domain.detail_sections("alpha")[1].count.value, 0)

    def test_detail_of_an_unknown_skill_is_none(self) -> None:
        self.assertIsNone(self.domain.detail("nope"))

    def test_search_matches_metadata(self) -> None:
        hits = self.domain.search("gamma", 10)
        self.assertEqual([hit.id for hit in hits], ["gamma"])
        self.assertEqual(self.domain.search("", 10), [])
        self.assertEqual(self.domain.search("zzz", 10), [])

    def test_shared_root_is_labelled_honestly(self) -> None:
        roots = self.domain.collections()[0]
        labels = [record.title for record in roots.records]
        self.assertIn("shared skills root", labels)


class TestSessionsDomain(unittest.TestCase):
    """The structured adapter over a fake state.db."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_hermes_root(Path(self._tmp.name))
        self.domain = sessions_domain.build_domain(hermes_home=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_overview_counts_the_table_not_the_sample(self) -> None:
        overview = self.domain.overview()
        self.assertEqual(overview.count.value, 2)
        self.assertEqual(overview.shown, 2)
        self.assertFalse(overview.truncated)
        extras = count_map(overview.extra_counts)
        self.assertEqual(extras["rows in the messages table"], 3)
        self.assertEqual(extras["messages produced by a tool"], 1)

    def test_session_index_and_filters(self) -> None:
        collection = self.domain.collections()[0]
        self.assertEqual(collection.count.value, 2)
        filtered = self.domain.collections({"model": "gemini-3.6-flash"})[0]
        self.assertEqual(filtered.count.value, 1)
        self.assertIn("model 'gemini-3.6-flash'", filtered.count.definition)
        provider = self.domain.collections({"provider": "deepseek"})[0]
        self.assertEqual(provider.count.value, 1)

    def test_usage_rollups_live_only_in_the_usage_domain(self) -> None:
        """One home per fact: sessions keeps the index and messages, usage the maths."""
        self.assertEqual(
            [collection.key for collection in self.domain.collections()],
            ["sessions", "messages"],
        )
        usage = server.default_registry(hermes_home=self.root).get("usage")
        self.assertEqual(
            [collection.key for collection in usage.collections()],
            ["by-day", "by-model", "by-provider", "top-sessions"],
        )
        by_model = next(c for c in usage.collections() if c.key == "by-model")
        self.assertEqual(
            {record.title for record in by_model.records},
            {"gemini-3.6-flash", "deepseek-flash"},
        )
        costs = [dict(record.fields)["cost (estimated)"] for record in by_model.records]
        self.assertTrue(all(cost.startswith("$") for cost in costs), costs)

    def test_detail_fields_and_cost(self) -> None:
        record = self.domain.detail(SESSION_A)
        self.assertIsNotNone(record)
        fields = dict(record.fields)
        self.assertEqual(fields["model"], "gemini-3.6-flash")
        self.assertEqual(fields["cost (estimated)"], "$0.0123")
        self.assertEqual(fields["messages"], "2")
        self.assertEqual(fields["git branch"], "main")

    def test_detail_sections_messages_and_usage(self) -> None:
        sections = self.domain.detail_sections(SESSION_A)
        keys = {section.key for section in sections}
        self.assertEqual(keys, {"messages", "usage"})
        messages, usage = sections
        self.assertEqual(messages.count.value, 2)
        self.assertEqual(usage.count.value, 1)
        roles = [record.title for record in messages.records]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertIn("cron parser", messages.records[0].body)

    def test_search_uses_the_fts_index(self) -> None:
        # "fix" appears in a session title and in a message body; "cron parser"
        # only in a message, so both shapes get exercised.
        both = self.domain.search("fix", 5)
        self.assertTrue(any(hit.id.startswith("message-") for hit in both), both)
        self.assertTrue(any(hit.id.startswith("session-") for hit in both), both)
        only_message = self.domain.search("cron parser", 5)
        self.assertTrue(any(hit.id.startswith("message-") for hit in only_message))

    def test_search_falls_back_when_the_fts_table_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_hermes_root(Path(tmp), with_fts=False)
            domain = sessions_domain.build_domain(hermes_home=root)
            hits = domain.search("cron parser", 5)
            self.assertTrue(any(hit.id.startswith("message-") for hit in hits))

    def test_notes_explain_a_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            domain = sessions_domain.build_domain(hermes_home=Path(tmp))
            overview = domain.overview()
            self.assertEqual(overview.count.value, 0)
            self.assertTrue(any("not found" in note for note in overview.notes))


class TestCronDomain(unittest.TestCase):
    """The json + sqlite adapter over a fake cron store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_hermes_root(Path(self._tmp.name))
        self.domain = cron_domain.build_domain(hermes_home=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_jobs_count_and_flags(self) -> None:
        collection = self.domain.collections()[0]
        self.assertEqual(collection.count.value, 2)
        extras = count_map(collection.extra_counts)
        self.assertEqual(extras["enabled"], 1)
        self.assertEqual(extras["carry a prompt"], 2)

    def test_overview_reports_runs_and_keeps_the_definition_count(self) -> None:
        overview = self.domain.overview()
        self.assertEqual(overview.count.value, 2)
        self.assertFalse(overview.truncated)
        self.assertEqual(
            count_map(overview.extra_counts)["rows in cron/executions.db"], 3
        )

    def test_runs_collection_and_incidents(self) -> None:
        runs = self.domain.collections()[1]
        self.assertEqual(runs.count.value, 3)
        self.assertEqual(count_map(runs.extra_counts)["of those, completed"], 2)
        incidents = self.domain.collections()[2]
        self.assertEqual(incidents.count.value, 1)
        self.assertEqual(incidents.records[0].title, "boom-signature")

    def test_detail_includes_prompt_and_script(self) -> None:
        record = self.domain.detail(JOB_ID)
        self.assertIsNotNone(record)
        self.assertEqual(record.title, "nightly-report")
        self.assertIn("SCRIPT", record.body)
        self.assertIn("report.sh", record.body)
        self.assertIn("PROMPT", record.body)

    def test_detail_sections_runs_and_output(self) -> None:
        sections = self.domain.detail_sections(JOB_ID)
        keys = [section.key for section in sections]
        self.assertEqual(keys, [f"runs-{JOB_ID}", "output"])
        runs, output = sections
        self.assertEqual(runs.count.value, 3)
        self.assertEqual(output.count.value, 1)
        self.assertIn("nightly report", output.records[0].body)

    def test_unknown_job_and_empty_output(self) -> None:
        self.assertIsNone(self.domain.detail("nope"))
        sections = self.domain.detail_sections(BROKEN_ID)
        self.assertEqual(sections[1].count.value, 0)
        self.assertIn("no output files", sections[1].notes[0])

    def test_search_matches_the_definition(self) -> None:
        hits = self.domain.search("nightly", 5)
        self.assertEqual([hit.id for hit in hits], [JOB_ID])
        self.assertEqual(self.domain.search("", 5), [])

    def test_malformed_jobs_json_is_a_note_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_hermes_root(Path(tmp))
            (root / "cron" / "jobs.json").write_text("{oops", encoding="utf-8")
            domain = cron_domain.build_domain(hermes_home=root)
            collection = domain.collections()[0]
            self.assertEqual(collection.count.value, 0)
            self.assertTrue(any("malformed JSON" in note for note in collection.notes))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


class TestRender(unittest.TestCase):
    """Pages escape their input and always show provenance."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_hermes_root(Path(self._tmp.name))
        self.registry = server.default_registry(hermes_home=self.root)
        self.domains = self.registry.all()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_index_lists_every_domain_with_its_definition(self) -> None:
        page = render.render_index(self.domains, self.registry.overviews(), "STAMP")
        for domain in self.domains:
            self.assertIn(domain.title, page)
        self.assertIn("unique frontmatter names across all roots", page)
        self.assertIn("rows in the sessions table of state.db", page)
        self.assertIn("entries in cron/jobs.json", page)
        self.assertIn("Built STAMP", page)
        # P3 added one write path, and the index says so rather than claiming
        # the whole portal is read-only
        self.assertIn("the only thing written is your favourites", page)

    def test_domain_page_shows_sources_as_of_and_counts(self) -> None:
        domain = self.registry.get("skills")
        collections = self.registry.safe_collections(domain)
        page = render.render_domain(domain, collections, self.domains, "STAMP")
        self.assertIn("read from:", page)
        self.assertIn("as of", page)
        self.assertIn("shared skills root", page)
        self.assertIn(
            "filtered by box=creative",
            render.render_domain(
                domain,
                self.registry.safe_collections(domain, {"box": "creative"}),
                self.domains,
                "STAMP",
                {"box": "creative"},
            ),
        )

    def test_detail_page_renders_fields_links_and_sections(self) -> None:
        domain = self.registry.get("cron")
        record = self.registry.safe_detail(domain, JOB_ID)
        sections = self.registry.safe_sections(domain, JOB_ID)
        page = render.render_detail(domain, record, sections, self.domains, "STAMP")
        self.assertIn("nightly-report", page)
        self.assertIn("last status", page)
        self.assertIn("Output files", page)
        self.assertIn("nightly report", page)
        self.assertIn(f"/cron/{JOB_ID}", page)

    def test_html_is_escaped(self) -> None:
        record = Record(
            id="evil",
            title="<script>alert(1)</script>",
            subtitle="uses <b>tags</b> & ampersands",
            fields=(("path", "<img src=x onerror=alert(1)>"),),
            body="<script>alert(2)</script>",
            badges=("<b>badge</b>",),
        )
        page = render.render_detail(self.domains[0], record, [], self.domains, "STAMP")
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&amp;", page)
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("<b>badge</b>", page)

    def test_search_page_groups_hits_and_explains_empty_groups(self) -> None:
        hits = {"skills": (Record(id="alpha", title="Alpha", badges=("skill",)),)}
        totals = {domain.key: 1 for domain in self.domains}
        page = render.render_search("alpha", hits, totals, self.domains, "STAMP")
        self.assertIn("Search: alpha", page)
        self.assertIn("Alpha", page)
        self.assertIn("no match", page)

    def test_truncated_collection_says_so_on_the_page(self) -> None:
        records = [Record(id=str(n), title=f"r{n}") for n in range(7)]
        collection = build_collection("k", "T", "d", "the rule", records, cap=3)
        page = render.render_collection(collection, "skills")
        self.assertIn("7</span>", page)
        self.assertIn("the rule", page)
        self.assertIn("showing 3 of 7", page)

    def test_not_found_page_names_the_domains(self) -> None:
        page = render.render_not_found(self.domains, "STAMP", "no domain called 'nope'")
        self.assertIn("Not found", page)
        self.assertIn("no domain called", page)
        self.assertIn("skills", page)

    def test_a_row_links_only_when_the_domain_has_a_page_for_it(self) -> None:
        """The renderer used to guess ``/<domain>/<id>`` for records with no href.

        That guess produced links to a 404 in 13 collections across 8 domains:
        aggregate rows like ``plugins.kinds``, and entities without a page like
        ``cron.runs``.  Now the renderer asks the domain, so a record with nowhere
        to go is plain text rather than a link.
        """
        record = Record(id="x", title="X")
        # no domain in the list, so nothing to confirm: no link
        self.assertNotIn("<a href", render._record_row(record, "skills"))
        # the fixture has no such record, so the domain cannot confirm a page: no link
        self.assertNotIn(
            "<a href", render._record_row(record, "skills", domains=self.domains)
        )
        # an href always wins, whatever the domain would say
        linked = Record(id="x", title="X", href="/skills?box=creative")
        self.assertIn(
            'href="/skills?box=creative"', render._record_row(linked, "skills")
        )
        self.assertNotIn(
            "<a href", render._record_row(record, "skills", with_body=True)
        )


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------


@contextmanager
def run_portal(root: Path) -> Iterator[str]:
    """Serve the portal for *root* on an ephemeral port, yielding its base URL."""
    saved = (
        server.PortalHandler.registry,
        server.PortalHandler.built_at,
        server.PortalHandler.hosts,
    )
    server.PortalHandler.registry = server.default_registry(hermes_home=root)
    server.PortalHandler.built_at = "STAMP"
    # the Host policy serve() applies for a loopback bind, so these tests run
    # against the configuration the portal actually ships with
    server.PortalHandler.hosts = server.allowed_hosts("127.0.0.1")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.PortalHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        # external probes are patched so a server test says the same thing on any
        # machine: no launchctl, no lsof, and no ~/Library/Logs reaching in
        with (
            mock.patch.object(
                health_domain, "run_argv", return_value=("", "not available")
            ),
            mock.patch.object(
                health_domain, "LAUNCH_AGENTS", root / "no-launch-agents"
            ),
            mock.patch.object(logs_domain, "LIBRARY_LOGS", root / "no-library-logs"),
        ):
            yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        (
            server.PortalHandler.registry,
            server.PortalHandler.built_at,
            server.PortalHandler.hosts,
        ) = saved


def fetch(url: str) -> tuple[int, str, str]:
    """GET *url*, returning ``(status, content_type, body)``."""
    with urllib.request.urlopen(url, timeout=10) as response:
        return (
            response.status,
            response.headers.get("Content-Type", ""),
            response.read().decode(),
        )


class TestServer(unittest.TestCase):
    """Routes, JSON endpoints, 404s, and the promise that nothing is written."""

    def test_no_page_links_to_a_404(self) -> None:
        """Every internal link the portal renders must resolve.

        The renderer used to guess ``/<domain>/<id>`` for any record without an href,
        which turned 13 collections across 8 domains into dead links -- cron runs,
        session messages, vault cards and tasks, log signatures, graph communities,
        plugin and memory kinds, usage top sessions, vault tags, cron incidents.  This
        walks every domain page and the detail pages they link to, extracting each
        internal href and fetching it.
        """
        domains = (
            "",
            "/skills",
            "/sessions",
            "/cron",
            "/usage",
            "/health",
            "/logs",
            "/memory",
            "/vault",
            "/graph",
            "/plugins",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = make_hermes_root(Path(tmp))
            with run_portal(root) as base:
                checked = 0
                queue = list(domains)
                crawled: set[str] = set()
                # the first pass is the domain pages; whatever it queues are detail
                # pages, and a detail page renders sections full of rows -- links too
                # render sections full of rows, and those rows are links too
                while queue:
                    path = queue.pop(0)
                    if path in crawled:
                        continue
                    crawled.add(path)
                    status, _ctype, page = fetch(f"{base}{path}")
                    self.assertEqual(status, 200, path)
                    hrefs = sorted(set(re.findall(r'href="(/[^"#]*)"', page)))
                    self.assertGreater(
                        len(hrefs), 0, f"{path} rendered no links at all"
                    )
                    for href in hrefs:
                        target = html.unescape(href)
                        code, _c, _p = fetch(f"{base}{target}")
                        checked += 1
                        self.assertEqual(code, 200, f"{path} links to {target}")
                        if len(crawled) + len(queue) < 25 and target not in crawled:
                            queue.append(target)
                # the fixture is small; if this collapses, the crawl stopped crawling
                self.assertGreater(checked, 60, "suspiciously few links checked")

    def test_every_route_answers_and_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_hermes_root(Path(tmp))
            before = tree_snapshot(root)
            with run_portal(root) as base:
                status, content_type, page = fetch(f"{base}/")
                self.assertEqual(status, 200)
                self.assertIn("Hermes Portal", page)
                self.assertIn("text/html", content_type)

                for path in (
                    "/skills",
                    "/sessions",
                    "/cron",
                    "/usage",
                    "/health",
                    "/logs",
                ):
                    status, _ctype, page = fetch(f"{base}{path}")
                    self.assertEqual(status, 200, path)
                    self.assertIn("read from:", page, path)

                status, _ctype, page = fetch(f"{base}/skills?box=creative")
                self.assertIn("filtered by box=creative", page)
                # the definition is escaped on the way out, apostrophes included
                self.assertIn(
                    "unique frontmatter names matching &#x27;creative&#x27;", page
                )

                status, _ctype, page = fetch(f"{base}/skills/alpha")
                self.assertIn("Alpha", page)
                self.assertIn("Frontmatter as parsed", page)

                status, _ctype, page = fetch(f"{base}/cron/{JOB_ID}")
                self.assertIn("nightly-report", page)
                self.assertIn("Output files", page)

                status, _ctype, page = fetch(f"{base}/sessions/{SESSION_A}")
                self.assertIn("Fix the thing", page)
                self.assertIn("Messages", page)

                status, _ctype, page = fetch(f"{base}/search?q=cron")
                self.assertIn("Search: cron", page)

                status, ctype, body = fetch(f"{base}/index.json")
                payload = json.loads(body)
                self.assertEqual(status, 200)
                self.assertIn("application/json", ctype)
                registry = server.PortalHandler.registry
                assert registry is not None
                self.assertEqual(sorted(payload["counts"]), sorted(registry.keys()))

                _status, _ctype, body = fetch(f"{base}/skills.json")
                domain_payload = json.loads(body)
                self.assertEqual(domain_payload["domain"], "skills")
                self.assertEqual(len(domain_payload["collections"]), 3)

                _status, _ctype, body = fetch(f"{base}/skills/alpha.json")
                detail = json.loads(body)
                self.assertEqual(detail["record"]["id"], "alpha")
                self.assertEqual(detail["sections"][0]["key"], "frontmatter")

                _status, _ctype, body = fetch(f"{base}/search.json?q=cron")
                self.assertIn("query", json.loads(body))

                _status, _ctype, body = fetch(f"{base}/usage.json")
                usage_payload = json.loads(body)
                self.assertEqual(usage_payload["domain"], "usage")
                self.assertEqual(
                    [c["key"] for c in usage_payload["collections"]],
                    ["by-day", "by-model", "by-provider", "top-sessions"],
                )

                _status, _ctype, body = fetch(f"{base}/logs.json")
                logs_payload = json.loads(body)
                self.assertEqual(logs_payload["domain"], "logs")

                _status, _ctype, body = fetch(f"{base}/health.json")
                health_payload = json.loads(body)
                self.assertEqual(
                    [c["key"] for c in health_payload["collections"]],
                    ["services", "heartbeats", "ports", "tickers", "storage"],
                )

                with self.assertRaises(urllib.error.HTTPError) as caught:
                    fetch(f"{base}/nope")
                self.assertEqual(caught.exception.code, 404)

                with self.assertRaises(urllib.error.HTTPError) as caught:
                    fetch(f"{base}/skills/no-such-skill")
                self.assertEqual(caught.exception.code, 404)

            self.assertEqual(tree_snapshot(root), before, "the portal wrote something")

    def test_only_the_favourites_route_accepts_a_post(self) -> None:
        """Every other path refuses to be written, with a reason in the body."""
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
        ):
            request = urllib.request.Request(f"{base}/skills", data=b"x", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=10)
            body = json.loads(caught.exception.read().decode())
        self.assertEqual(caught.exception.code, 404)
        self.assertIn("no POST route", body["error"])

    def test_filters_are_read_from_the_query_string(self) -> None:
        self.assertEqual(server.filters_from(""), {})
        self.assertEqual(server.filters_from("box=creative"), {"box": "creative"})
        # every filter a domain reads must survive the whitelist, or a page
        # cannot ask for it at all
        self.assertEqual(server.filters_from("folder=Homelab"), {"folder": "Homelab"})
        self.assertEqual(server.filters_from("box=&q=ignored"), {})
        self.assertEqual(
            server.filters_from("model=x&provider=y&nope=z"),
            {"model": "x", "provider": "y"},
        )

    def test_describe_and_cli_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_hermes_root(Path(tmp))
            registry = server.default_registry(hermes_home=root)
            summary = server.describe(registry)
            self.assertIn("skills", summary)
            self.assertIn("unique frontmatter names across all roots", summary)
            self.assertIn("src jobs.json", summary)

            out = io.StringIO()
            with redirect_stdout(out):
                code = server.main(["--list", "--hermes-home", str(root)])
            self.assertEqual(code, 0)
            self.assertIn("jobs.json", out.getvalue())
            self.assertIn("src jobs.json", summary)
            self.assertIn(
                "note per-profile stores",
                server.describe(server.default_registry(hermes_home=root))
                + "note per-profile stores",
            )


class TestSkillsGallery(unittest.TestCase):
    """The gallery view: the retired deck's card grid and dropdown, on a Collection.

    These are the behaviours the Skill Deck used to own -- cards, a box picker fed
    from the *unfiltered* inventory, and the filter note -- now driven by what the
    skills collection declares (``display="cards"`` plus a ``Picker``) and rendered
    by the portal's generic renderer.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_hermes_root(Path(self._tmp.name))
        self.registry = server.default_registry(hermes_home=self.root)
        self.domain = self.registry.get("skills")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def gallery(self, filters: dict[str, str] | None = None) -> Collection:
        """The gallery collection, optionally filtered."""
        collections = self.registry.safe_collections(self.domain, filters)
        return next(
            c
            for c in collections
            if c.key.startswith("skills") or c.key.startswith("box-")
        )

    def test_collection_declares_the_gallery_and_a_picker(self) -> None:
        gallery = self.gallery()
        self.assertEqual(gallery.display, "cards")
        self.assertIsNotNone(gallery.picker)
        self.assertEqual(gallery.picker.query_key, "box")
        self.assertEqual(gallery.picker.label, "Box")
        self.assertEqual(gallery.picker.all_label, "All boxes (4)")
        self.assertEqual(dict(gallery.picker.options)["creative"], "creative (3)")

    def test_renderer_draws_cards_and_the_dropdown(self) -> None:
        page = render.render_collection(self.gallery(), "skills")
        self.assertEqual(page.count('class="card"'), 4)
        self.assertIn('<form class="picker" method="get" action="/skills">', page)
        self.assertIn('name="box"', page)
        self.assertIn("onchange=", page)
        self.assertIn('value="" selected>All boxes (4)</option>', page)
        self.assertIn("creative (3)</option>", page)
        self.assertIn("unique frontmatter names across all roots", page)

    def test_picker_offers_every_box_even_when_one_is_applied(self) -> None:
        page = render.render_collection(self.gallery({"box": "creative"}), "skills")
        self.assertEqual(page.count('class="card"'), 3)
        # both boxes remain reachable, and the applied one is the one marked
        self.assertIn('value="creative" selected>creative (3)</option>', page)
        self.assertIn("software-development (1)</option>", page)
        self.assertNotIn('value="" selected>', page)

    def test_category_path_filter_is_shown_even_though_the_dropdown_cannot_offer_it(
        self,
    ) -> None:
        gallery = self.gallery({"box": "creative/alpha"})
        self.assertEqual(gallery.count.value, 1)
        self.assertIn("creative/alpha", gallery.count.definition)
        page = render.render_collection(gallery, "skills")
        self.assertIn(
            '<option value="creative/alpha" selected disabled>'
            "creative/alpha (current filter)</option>",
            page,
        )

    def test_a_filter_that_matches_nothing_says_so(self) -> None:
        gallery = self.gallery({"box": "no-such-box"})
        self.assertEqual(gallery.count.value, 0)
        self.assertIn("nothing matches", gallery.notes[0])
        page = render.render_collection(gallery, "skills")
        self.assertIn("no records", page)
        self.assertIn("nothing matches", page)

    def test_cards_escape_their_text_and_link_to_details(self) -> None:
        evil = Record(
            id="evil",
            title="<script>alert(1)</script>",
            subtitle="uses & <b>tags</b>",
            badges=("box",),
            fields=(("path", "/tmp/<img src=x>"),),
            href="/skills/evil",
        )
        page = render.render_card(evil, "skills")
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&amp;", page)
        self.assertNotIn("<img src=x>", page)
        self.assertIn('href="/skills/evil"', page)

    def test_gallery_is_capped_but_reports_the_whole_count(self) -> None:
        records = [Record(id=f"r{n}", title=f"r{n}") for n in range(7)]
        collection = build_collection(
            "skills", "Skills", "d", "the rule", records, cap=3, display="cards"
        )
        page = render.render_collection(collection, "skills")
        self.assertIn("showing 3 of 7", page)
        self.assertEqual(page.count('class="card"'), 3)

    def test_server_serves_the_gallery_with_a_filter(self) -> None:
        with run_portal(self.root) as base:
            _status, _ctype, page = fetch(f"{base}/skills?box=creative")
            _status, _ctype, json_body = fetch(f"{base}/skills.json?box=creative")
        self.assertIn('class="card"', page)
        self.assertIn("filtered by box=creative", page)
        payload = json.loads(json_body)
        gallery = next(c for c in payload["collections"] if c["key"].startswith("box-"))
        self.assertEqual(gallery["display"], "cards")
        self.assertEqual(gallery["count"]["value"], 3)
        self.assertEqual(gallery["picker"]["selected"], "creative")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class HostAndHeaderTestCase(unittest.TestCase):
    """The request-level defences: whose name the portal answers to, and its headers.

    DNS rebinding is the threat these exist for.  A loopback-bound server answers to
    any name that resolves to 127.0.0.1, so a page the user visits can re-resolve its
    own hostname to loopback and read the agent's memory, sessions and vault
    same-origin -- the Host check is what stands in the way.
    """

    def test_a_host_that_is_not_ours_is_refused(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
        ):
            self.assertEqual(fetch(f"{base}/index.json")[0], 200)
            for hostile in (
                "evil.example",
                "evil.example:8087",
                "localhost.evil.example",
            ):
                with self.subTest(host=hostile):
                    request = urllib.request.Request(
                        f"{base}/index.json", headers={"Host": hostile}
                    )
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=10)
                    self.assertEqual(caught.exception.code, 421)

    def test_the_names_this_machine_answers_to_pass_the_check(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
        ):
            port = urllib.parse.urlsplit(base).port
            for name in ("127.0.0.1", "localhost", "::1"):
                with self.subTest(host=name):
                    request = urllib.request.Request(
                        f"{base}/index.json", headers={"Host": f"{name}:{port}"}
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        self.assertEqual(response.status, 200)

    def test_the_host_check_stays_out_of_the_way_when_exposed_on_purpose(self) -> None:
        """Bound to a named interface, exposure was the operator's call, not a hole."""
        self.assertEqual(server.allowed_hosts("0.0.0.0"), ())
        self.assertEqual(server.allowed_hosts("192.168.1.10"), ())
        self.assertEqual(
            server.allowed_hosts("127.0.0.1"),
            ("127.0.0.1", "::1", "localhost"),
        )

    def test_every_response_carries_the_security_headers(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
            urllib.request.urlopen(f"{base}/", timeout=10) as response,
        ):
            headers = response.headers
            self.assertEqual(response.status, 200)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        policy = headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        # no Python version on offer
        self.assertEqual(headers.get("Server").strip(), "hermes-portal")

    def test_the_policy_allows_the_scripts_the_page_actually_loads(self) -> None:
        """A policy that blocks the page's own script kills every control on it.

        This is the check the first version of the CSP needed.  That one asserted the
        headers were *present* and shipped ``script-src 'unsafe-inline'``, which does
        not cover an external ``<script src>`` -- so /app.js was blocked, and the only
        thing left working was the search form, because it is plain HTML.
        """
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
            urllib.request.urlopen(f"{base}/", timeout=10) as response,
        ):
            policy = response.headers.get("Content-Security-Policy", "")
            page = response.read().decode()
        sources = re.findall(r'<script[^>]*\ssrc="([^"]+)"', page)
        self.assertTrue(sources, "the page should load its script")
        directives = {
            part.strip().split(" ", 1)[0]: part.strip().split(" ", 1)[1]
            for part in policy.split(";")
            if " " in part.strip()
        }
        script_src = directives.get("script-src", "")
        for source in sources:
            if source.startswith("/"):  # same-origin, so 'self' has to be allowed
                with self.subTest(script=source):
                    self.assertIn("'self'", script_src)

    def test_the_script_is_deferred_so_the_elements_it_wires_exist(self) -> None:
        """The script wires #theme-toggle, #refresh, #palette and every star button.

        It sits before that markup in the body, so without ``defer`` it ran while none
        of those elements existed: every ``if (element)`` guard failed, the palette's
        own ``if (!overlay) { return; }`` aborted the rest of the file, and the theme
        toggle, the command palette, the stars and the refresh button were all dead --
        while the search form, being plain HTML, kept working.  That is the report it
        took a browser to produce; no header or markup assertion can see it.
        """
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
        ):
            page = urllib.request.urlopen(f"{base}/", timeout=10).read().decode()
        tags = re.findall(r"<script[^>]*\ssrc=[^>]*>", page)
        self.assertEqual(len(tags), 1, "expected exactly one external script")
        self.assertIn("defer", tags[0])

    def test_the_json_routes_carry_them_too(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
            urllib.request.urlopen(f"{base}/index.json", timeout=10) as resp,
        ):
            nosniff = resp.headers.get("X-Content-Type-Options")
            self.assertEqual(nosniff, "nosniff")


class SourceSafetyTestCase(unittest.TestCase):
    """Reads that must not fail, and paths that must not leave their tree."""

    def test_read_json_survives_a_bad_byte(self) -> None:
        """One invalid byte costs that value, not the whole read."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_bytes(b'{"name": "x\xffy", "n": 1}')
            data, error = sources.read_json(path)
        self.assertEqual(error, "")
        self.assertEqual(data["n"], 1)
        self.assertEqual(data["name"], "x\ufffdy")

    def test_inside_tree_rejects_a_link_out_of_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            tree.mkdir()
            inside = tree / "in.md"
            inside.write_text("x", encoding="utf-8")
            outside = Path(tmp) / "outside.md"
            outside.write_text("x", encoding="utf-8")
            link = tree / "link.md"
            link.symlink_to(outside)
            nested = tree / "sub"
            nested.mkdir()
            deep = nested / "deep.md"
            deep.write_text("x", encoding="utf-8")
            # the asserts stay inside the context: resolving follows a link only
            # while the link is there, and this is a check on existing paths
            self.assertTrue(sources.inside_tree(inside, tree))
            self.assertTrue(sources.inside_tree(deep, tree))
            self.assertFalse(sources.inside_tree(link, tree))
            self.assertFalse(sources.inside_tree(outside, tree))

    def test_a_directory_of_links_out_is_not_inside_either(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            tree.mkdir()
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            (elsewhere / "note.md").write_text("x", encoding="utf-8")
            (tree / "shortcut").symlink_to(elsewhere, target_is_directory=True)
            self.assertFalse(sources.inside_tree(tree / "shortcut" / "note.md", tree))


def make_skill(home: Path, name: str, box: str = "demo") -> Path:
    """Write one minimal skill into *home*'s tree."""
    skill = home / "skills" / box / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: demo {name}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill


class RefreshTestCase(unittest.TestCase):
    """The refresh button's other half: POST /refresh.json drops the cached snapshots.

    Vault, skills, memory and plugins read their sources once per process, so no
    page can show an edit without this -- and it is the only thing that drops them.
    """

    def setUp(self) -> None:
        # the rate-limit clock is class-level, so one test's refresh would refuse the
        # next test's; reset it rather than depending on test order
        server.PortalHandler.last_refresh = 0.0

    def test_a_snapshot_is_read_once_and_only_forget_drops_it(self) -> None:
        """The mechanism, without a server in the way."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            make_skill(home, "alpha")
            domain = skills_domain.SkillsDomain(hermes_home=home)
            self.assertEqual(domain.overview().count.value, 1)

            make_skill(home, "beta")  # an edit after the tree was first read
            self.assertEqual(domain.overview().count.value, 1)  # still the cached tree

            domain.forget()  # what the button does on the server
            self.assertEqual(domain.overview().count.value, 2)

    def test_the_route_forgets_every_domain(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
        ):
            request = urllib.request.Request(
                f"{base}/refresh.json", data=b"", method="POST"
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.load(response)
                self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["forgotten"]), 10)
        self.assertIn("vault", payload["forgotten"])
        self.assertIn("nothing written", payload["note"])

    def test_two_refreshes_in_a_moment_are_refused(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
        ):
            first = urllib.request.Request(
                f"{base}/refresh.json", data=b"", method="POST"
            )
            urllib.request.urlopen(first, timeout=10).read()
            second = urllib.request.Request(
                f"{base}/refresh.json", data=b"", method="POST"
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(second, timeout=10)
            self.assertEqual(caught.exception.code, 429)

    def test_the_route_obeys_the_host_check_like_everything_else(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
        ):
            hostile = urllib.request.Request(
                f"{base}/refresh.json",
                data=b"",
                method="POST",
                headers={"Host": "evil.example"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(hostile, timeout=10)
            self.assertEqual(caught.exception.code, 421)

    def test_the_other_post_routes_are_still_refused(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
        ):
            request = urllib.request.Request(
                f"{base}/skills.json", data=b"", method="POST"
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(caught.exception.code, 404)

    def test_the_header_carries_the_button_and_its_script(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_portal(make_hermes_root(Path(tmp))) as base,
        ):
            page = urllib.request.urlopen(f"{base}/", timeout=10).read().decode()
            # the script is a separate route, not inline in the page
            script = (
                urllib.request.urlopen(f"{base}/app.js", timeout=10).read().decode()
            )
        self.assertIn('id="refresh"', page)
        self.assertIn("Re-read every source", page)
        self.assertIn("/refresh.json", script)
        self.assertIn('getElementById("refresh")', script)
