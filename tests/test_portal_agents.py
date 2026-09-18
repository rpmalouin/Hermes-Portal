"""Tests for the agents domain: every profile Hermes has, reported as a box.

Hermetic: the fixture is a Hermes home built in a temp directory, never ``~/.hermes``.
It holds a root with its own store and cron, two profiles (one with sessions, cron and
heartbeats of its own, one whose ``state.db`` is not a database), a profile with no
store at all, and a hidden ``.deleted`` directory -- because that is the shape this
machine has, and Hermes' own trash must not appear as an agent.

Four things here are more than unit tests:

* the **set** (root plus every non-hidden profile, and nothing else) -- a page that
lists the wrong agents is wrong no matter how right its numbers are; * a store that
could not be read reads as *unread*, not as zero sessions: the same rule the rest of the
portal follows for an unavailable read; * a job link is drawn only where a page exists
-- the cron domain serves the *root's* jobs.json, so a profile's job row must not link
to a page that would 404; * every collection's count against the records it carries.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes.portal import server  # noqa: E402
from hermes.portal.domains import agents  # noqa: E402
from hermes.portal.model import detail_url, filter_url  # noqa: E402

STATE_SQL = """
create table sessions (
    id text primary key, title text, started_at real, message_count integer
);
create table gateway_heartbeats (backend_id text, last_heartbeat real);
"""


def write_store(base: Path, sessions: list, heartbeats: list) -> None:
    """A state.db with the columns this adapter reads, and nothing else."""
    base.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(base / "state.db")
    con.executescript(STATE_SQL)
    con.executemany("insert into sessions values (?, ?, ?, ?)", sessions)
    con.executemany("insert into gateway_heartbeats values (?, ?)", heartbeats)
    con.commit()
    con.close()


def write_jobs(base: Path, jobs: list) -> None:
    """A cron/jobs.json in the shape the cron adapter loads."""
    (base / "cron").mkdir(parents=True, exist_ok=True)
    (base / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": jobs}), encoding="utf-8"
    )


class AgentsTestCase(unittest.TestCase):
    """A Hermes home with a root, three profiles and a hidden directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "hermes"
        profiles = self.home / "profiles"

        # the root: a store, heartbeats, and two jobs of its own
        write_store(
            self.home,
            [("s1", "root session", 1_700_000_000.0, 12)],
            [("desktop", 1_700_000_100.0)],
        )
        write_jobs(
            self.home,
            [
                {
                    "id": "root-job-1",
                    "name": "nightly",
                    "schedule_display": "0 3 * * *",
                },
                {"id": "root-job-2", "name": "ticker", "schedule_display": "every 5m"},
            ],
        )
        # a profile with its own store, sessions and cron
        write_store(
            profiles / "alpha",
            [
                ("a1", "alpha one", 1_700_000_200.0, 5),
                ("a2", "alpha two", 1_700_000_300.0, 7),
            ],
            [("desktop", 1_700_000_400.0)],
        )
        write_jobs(
            profiles / "alpha",
            [
                {
                    "id": "alpha-job",
                    "name": "alpha sync",
                    "schedule_display": "every 60m",
                }
            ],
        )
        # a profile whose store is not a database at all
        (profiles / "broken").mkdir(parents=True)
        (profiles / "broken" / "state.db").write_bytes(b"not a database" * 20)
        # a profile that has never run: no store
        (profiles / "fresh").mkdir(parents=True)
        # Hermes' own trash: never an agent
        (profiles / ".deleted").mkdir(parents=True)

        self.domain = agents.build_domain(hermes_home=self.home)
        self.index = agents.read_agents(self.home)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def labels(self) -> list[str]:
        return [agent.label for agent in self.index.agents]

    def collections(self) -> dict:
        return {c.key: c for c in self.domain.collections()}

    def record(self, label: str):
        """One agent's row, from the overview."""
        return next(r for r in self.domain.overview().records if r.id == label)


class TestSet(AgentsTestCase):
    """Who is an agent, and who is not."""

    def test_the_set_is_the_root_plus_every_profile(self) -> None:
        self.assertEqual(sorted(self.labels()), ["alpha", "broken", "default", "fresh"])

    def test_hidden_directories_are_not_agents(self) -> None:
        """Hermes keeps retired profiles in ``.deleted``; a row for that answers
        nothing."""
        self.assertNotIn(".deleted", self.labels())

    def test_the_root_is_labelled_the_way_the_memory_page_labels_it(self) -> None:
        self.assertIn("default", self.labels())
        root = next(a for a in self.index.agents if a.is_root)
        self.assertEqual(root.base, self.home)
        fields = dict(self.record("default").fields)
        self.assertEqual(fields["home"], str(self.home))
        # A row's meta line takes the leading fields (render.py: fields[:4]), so the
        # activity leads and the paths trail -- pinned rather than left to drift.
        self.assertEqual(
            [key for key, _value in self.record("default").fields[:2]],
            ["sessions", "messages"],
        )


class TestRollup(AgentsTestCase):
    """What each agent's store says."""

    def test_a_profile_with_its_own_cron_is_counted(self) -> None:
        self.assertEqual(dict(self.record("alpha").fields)["cron jobs"], "1")
        self.assertEqual(dict(self.record("default").fields)["cron jobs"], "2")

    def test_sessions_and_messages_come_from_that_agent_s_own_store(self) -> None:
        fields = dict(self.record("alpha").fields)
        self.assertEqual(fields["sessions"], "2")
        self.assertEqual(fields["messages"], "12")
        self.assertEqual(fields["newest session"], "alpha two")
        self.assertEqual(dict(self.record("default").fields)["sessions"], "1")

    def test_the_overview_counts_only_the_stores_that_could_be_read(self) -> None:
        """Two of the four agents have a readable store: the root's and alpha's."""
        overview = self.domain.overview()
        metrics = dict(overview.metrics)
        self.assertEqual(metrics["Agents"], "4")
        self.assertEqual(metrics["Stores read"], "2 of 4")
        self.assertEqual(metrics["Cron jobs"], "3")
        counts = {count.definition: count.value for count in overview.extra_counts}
        self.assertEqual(counts["agents"], 4)
        self.assertEqual(counts["sessions across the 2 store(s) that could be read"], 3)

    def test_agents_are_ordered_newest_store_first(self) -> None:
        written = [agent.store_modified or 0 for agent in self.index.agents]
        self.assertEqual(written, sorted(written, reverse=True))


class TestUnreadStores(AgentsTestCase):
    """A store that was not read must not read as an empty one."""

    def test_a_store_that_is_not_a_database_is_unread(self) -> None:
        broken = next(a for a in self.index.agents if a.label == "broken")
        self.assertFalse(broken.store_readable)
        self.assertIn("not a database", broken.store_error)

    def test_an_unread_store_publishes_no_numbers(self) -> None:
        fields = dict(self.record("broken").fields)
        self.assertNotIn("sessions", fields)
        self.assertNotIn("messages", fields)
        self.assertIn("read", fields)
        self.assertIn("store not read", self.record("broken").subtitle)

    def test_a_profile_with_no_store_is_reported_not_guessed(self) -> None:
        fresh = next(a for a in self.index.agents if a.label == "fresh")
        self.assertFalse(fresh.store_readable)
        self.assertIn("no state.db", fresh.store_error)
        self.assertNotIn("sessions", dict(self.record("fresh").fields))

    def test_its_sections_say_unavailable_rather_than_showing_nothing(self) -> None:
        sections = {c.key: c for c in self.domain.detail_sections("broken")}
        self.assertTrue(
            sections["sessions"].count.definition.startswith("unavailable --"),
            sections["sessions"].count.definition,
        )
        self.assertEqual(list(sections["sessions"].records), [])

    def test_the_notes_name_every_store_that_could_not_be_read(self) -> None:
        notes = " ".join(self.domain.overview().notes)
        self.assertIn("stores not read", notes)
        self.assertIn("broken", notes)
        self.assertIn("fresh", notes)


class TestJobs(AgentsTestCase):
    """Cron lives per agent, and a link is only drawn where a page exists."""

    def test_every_agents_jobs_are_listed_in_one_place(self) -> None:
        cron = self.collections()["cron"]
        self.assertEqual(cron.count.value, 3)
        titles = {record.title for record in cron.records}
        self.assertEqual(titles, {"nightly", "ticker", "alpha sync"})

    def test_a_root_job_keeps_the_cron_page_link(self) -> None:
        row = next(
            r for r in self.collections()["cron"].records if r.title == "nightly"
        )
        self.assertEqual(row.links, ((detail_url("cron", "root-job-1"), "Open job"),))

    def test_a_profiles_job_has_no_link_because_that_page_would_404(self) -> None:
        """The cron domain reads the root's jobs.json, so /cron/<id> would not
        answer."""
        row = next(
            r for r in self.collections()["cron"].records if r.title == "alpha sync"
        )
        self.assertEqual(row.links, ())
        self.assertIn("alpha \u00b7", row.subtitle)

    def test_the_note_says_where_cron_really_lives(self) -> None:
        notes = " ".join(self.domain.overview().notes)
        self.assertIn("cron lives per agent", notes)
        self.assertIn("alpha", notes)


class TestPages(AgentsTestCase):
    """The detail page, the search, and the invariant every collection carries."""

    def test_every_agent_has_a_page_and_nothing_else_does(self) -> None:
        for label in self.labels():
            self.assertIsNotNone(self.domain.detail(label), label)
        self.assertIsNone(self.domain.detail("no-such-agent"))
        self.assertEqual(list(self.domain.detail_sections("no-such-agent")), [])

    def test_an_agents_page_carries_its_own_sections(self) -> None:
        keys = [c.key for c in self.domain.detail_sections("alpha")]
        self.assertEqual(keys, ["sessions", "jobs", "store"])
        sessions = self.domain.detail_sections("alpha")[0]
        self.assertEqual(sessions.count.value, 2)
        self.assertIn("alpha two", [r.title for r in sessions.records])

    def test_the_memory_link_is_a_filter_the_memory_page_reads(self) -> None:
        """A control that exists in the picker but not the whitelist is a dead
        control."""
        self.assertIn("profile", server.FILTER_KEYS)
        links = {label: href for href, label in self.record("alpha").links}
        self.assertEqual(links["Memory"], filter_url("memory", profile="alpha"))
        self.assertEqual(links["Open agent"], detail_url("agents", "alpha"))

    def test_search_finds_an_agent_by_name(self) -> None:
        self.assertEqual([r.id for r in self.domain.search("alpha", 5)], ["alpha"])
        self.assertEqual([r.id for r in self.domain.search("  ", 5)], [])

    def test_every_collection_counts_what_it_carries(self) -> None:
        for collection in [self.domain.overview(), *self.collections().values()]:
            self.assertEqual(
                collection.count.value,
                len(collection.records),
                f"{collection.key}: count {collection.count.value} != "
                f"{len(collection.records)} records",
            )

    def test_the_stores_collection_is_one_row_per_agent(self) -> None:
        stores = self.collections()["stores"]
        self.assertEqual(stores.count.value, len(self.labels()))
        self.assertEqual({r.id for r in stores.records}, set(self.labels()))
