"""Tests for ``hermes-portal doctor``.

The fixture is a **recorded schema snapshot**: the DDL the six tables actually have
in a live Hermes, pasted verbatim, plus rows of the shapes those stores write
(``state.db`` holds epoch seconds as REAL, ``cron/executions.db`` holds ISO-8601
strings as TEXT).  That makes the healthy case a real assertion -- the declared
contract still matches a true shape -- instead of a fixture built to agree with the
code it is testing.
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

from hermes.portal import doctor, server  # noqa: E402

STATE_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    compression_ineffective_count INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0, "system_prompt_hash" TEXT,
        "title_source" TEXT, "last_activity_at" REAL,
        "last_activity_description" TEXT, "last_activity_provenance" TEXT,
        "last_read_at" REAL, "git_metadata_generation" INTEGER NOT NULL DEFAULT 0,
        "hidden" INTEGER NOT NULL DEFAULT 0, "compression_recovery_deadline" REAL,
        "tool_names" TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    effect_disposition TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    api_content TEXT,
    display_kind TEXT,
    display_metadata TEXT
, "_compressed_summary" INTEGER NOT NULL DEFAULT 0, "display_identity" BLOB,
        "display_order" INTEGER);
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT,
    cost_source TEXT,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url,
        billing_mode, task)
);
CREATE TABLE gateway_heartbeats (
    backend_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    started_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT ''
);
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version (version) VALUES (30);
INSERT INTO sessions (id, source, model, started_at, ended_at, title, end_reason,
                      billing_provider)
    VALUES ('s-1', 'desktop', 'deepseek-flash', 1789657135.0, 1789657200.0,
            'a session', NULL, 'deepseek'),
           ('s-2', 'gateway', 'gemini-3.6-flash', 1789650000.0, NULL,
            'another', NULL, NULL);
INSERT INTO messages (session_id, role, content, timestamp)
    VALUES ('s-1', 'user', 'hello', 1789657136.0),
           ('s-1', 'assistant', 'hi', 1789657137.0);
INSERT INTO gateway_heartbeats (backend_id, pid, started_at, last_heartbeat)
    VALUES ('backend-1', 4242, 1789650000.0, 1789657200.0);
"""

CRON_DDL = """
CREATE TABLE executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           , handoff_pending INTEGER NOT NULL DEFAULT 0, handoff_started_at REAL,
        delivery_outcome TEXT, scheduled_instant TEXT);
CREATE TABLE cron_incidents (
             id            TEXT PRIMARY KEY,
             job_id        TEXT NOT NULL,
             error_sig     TEXT NOT NULL,
             state         TEXT NOT NULL,
             failure_type  TEXT NOT NULL DEFAULT 'unknown',
             first_seen_at TEXT NOT NULL,
             last_seen_at  TEXT NOT NULL,
             acked_at      TEXT,
             closed_at     TEXT,
             error         TEXT NOT NULL,
             output_file   TEXT
           );
INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at,
                        started_at, finished_at)
    VALUES ('run-1', 'job-1', 'scheduler', 'p-1', 100, 'completed',
            '2026-09-18T09:00:00+00:00', '2026-09-18T09:00:00.500000+00:00',
            '2026-09-18T09:00:02.000000+00:00');
INSERT INTO cron_incidents (id, job_id, error_sig, state, first_seen_at,
                            last_seen_at, error)
    VALUES ('inc-1', 'job-1', 'boom', 'open', '2026-09-18T08:00:00+00:00',
            '2026-09-18T08:00:00+00:00', 'boom');
"""

GRAPH_TABLES = (
    "metadata",
    "nodes",
    "edges",
    "communities",
    "community_summaries",
    "flows",
    "flow_memberships",
    "risk_index",
    "nodes_fts",
)


def build_root(tmp: Path) -> Path:
    """A Hermes root shaped like the real one, including the cron store marker."""
    root = tmp / "hermes"
    (root / "cron").mkdir(parents=True)
    state = sqlite3.connect(root / "state.db")
    state.executescript(STATE_DDL)
    state.commit()
    state.close()
    cron = sqlite3.connect(root / "cron" / "executions.db")
    cron.executescript(CRON_DDL)
    cron.commit()
    cron.close()
    (root / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "job-1", "name": "nightly", "enabled": True}]}),
        encoding="utf-8",
    )
    return root


def make_graph_db(root: Path) -> Path:
    """A minimal code graph store, enough for the presence check and the hint."""
    path = root / ".code-review-graph" / "graph.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    for table in GRAPH_TABLES:
        con.execute(
            f"create table {table} (id integer primary key, key text, value text)"
        )
    con.execute("insert into metadata (key, value) values ('schema_version', '9')")
    con.commit()
    con.close()
    return path


class DoctorTestCase(unittest.TestCase):
    """A temp Hermes root, rebuilt fresh for every test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def check(self) -> doctor.Report:
        """Run the doctor against this test's root."""
        return doctor.inspect(home=self.root)

    def sql(self, store: str, statement: str) -> None:
        """Run one statement against a store under this root."""
        con = sqlite3.connect(self.root / store)
        con.execute(statement)
        con.commit()
        con.close()


class TestHealthyFixture(DoctorTestCase):
    """The recorded snapshot must satisfy the declared contract, untouched."""

    def test_a_healthy_store_reports_no_drift(self) -> None:
        """The point of the snapshot: real DDL, and nothing declared is missing."""
        report = self.check()
        self.assertTrue(report.ok, [f.subject for f in report.drift])
        self.assertEqual(report.drift, ())

    def test_the_version_is_a_hint_and_the_hint_is_read(self) -> None:
        report = self.check()
        self.assertEqual(report.hints["state.db schema_version"], "30")

    def test_additions_are_reported_as_notes_not_drift(self) -> None:
        """Hermes adds columns; a reader that names its columns keeps working."""
        notes = [f for f in self.check().findings if f.severity == "info"]
        self.assertTrue(any("does not read" in note.detail for note in notes))
        self.assertTrue(any("safe by design" in note.detail for note in notes))

    def test_stamps_of_both_shapes_parse(self) -> None:
        """state.db writes REAL epoch seconds, executions.db writes ISO TEXT."""
        findings = [
            f
            for f in self.check().findings
            if f.severity in ("drift", "warn") and "do not parse" in f.detail
        ]
        self.assertEqual(findings, [])

    def test_a_missing_graph_database_is_a_warning_not_drift(self) -> None:
        """The graph is optional by design: every other page still serves."""
        report = self.check()
        self.assertTrue(report.ok, [f.subject for f in report.drift])
        self.assertTrue(any("graph.db" in w.subject for w in report.warnings))

    def test_a_present_graph_database_reports_its_own_version(self) -> None:
        make_graph_db(self.root)
        report = self.check()
        self.assertEqual(report.hints["graph.db schema_version"], "9")
        self.assertTrue(report.ok, [f.subject for f in report.drift])


class TestDrift(DoctorTestCase):
    """Each drift class the portal has actually met, reproduced deliberately."""

    def test_a_renamed_column_names_the_likely_candidate(self) -> None:
        self.sql(
            "state.db", "alter table sessions rename column started_at to created_at"
        )
        finding = next(
            f for f in self.check().drift if f.subject == "sessions.started_at"
        )
        self.assertIn("created_at", finding.detail)
        # A rename keeps the declared type, and the report shows it as the hint.
        self.assertIn("(real)", finding.detail)

    def test_a_dropped_column_with_no_lookalike_says_so(self) -> None:
        self.sql("state.db", "alter table sessions drop column git_repo_root")
        finding = next(
            f for f in self.check().drift if f.subject == "sessions.git_repo_root"
        )
        self.assertTrue(
            "nothing similar" in finding.detail or "closest" in finding.detail,
            finding.detail,
        )

    def test_a_missing_table_is_drift_when_a_page_needs_it(self) -> None:
        self.sql("state.db", "drop table gateway_heartbeats")
        report = self.check()
        self.assertFalse(report.ok)
        self.assertTrue(any(f.subject == "gateway_heartbeats" for f in report.drift))

    def test_an_unreadable_store_is_drift_and_says_why(self) -> None:
        (self.root / "state.db").write_bytes(b"\x00\x01not a database" * 64)
        report = self.check()
        self.assertFalse(report.ok)
        self.assertTrue(any("not a database" in f.detail for f in report.drift))

    def test_a_stamp_column_that_changed_meaning_is_drift(self) -> None:
        """The shape is identical; only the content moved. No schema diff sees it."""
        self.sql(
            "cron/executions.db",
            "update executions set started_at = 'yesterday afternoon'",
        )
        finding = next(
            f for f in self.check().drift if f.subject == "executions.started_at"
        )
        self.assertIn("do not parse", finding.detail)
        self.assertIn("changed what it stores", finding.detail)

    def test_a_millisecond_stamp_is_flagged_as_a_unit_change(self) -> None:
        """A number too large to be seconds parses as a date -- and a wrong one."""
        self.sql("state.db", "update sessions set started_at = started_at * 1000")
        report = self.check()
        self.assertTrue(
            any(
                "milliseconds" in f.detail and f.subject == "sessions.started_at"
                for f in report.warnings
            ),
            [f.detail for f in report.warnings],
        )

    def test_malformed_jobs_json_reports_the_loaders_own_message(self) -> None:
        (self.root / "cron" / "jobs.json").write_text("{oops", encoding="utf-8")
        report = self.check()
        self.assertFalse(report.ok)
        self.assertTrue(any("malformed JSON" in f.detail for f in report.drift))

    def test_a_renamed_jobs_shape_is_drift_not_an_empty_schedule(self) -> None:
        (self.root / "cron" / "jobs.json").write_text(
            json.dumps({"schema_version": 2, "job_definitions": [{"job_id": "x"}]}),
            encoding="utf-8",
        )
        report = self.check()
        self.assertTrue(
            any("unrecognized jobs.json shape" in f.detail for f in report.drift)
        )


class TestOutput(DoctorTestCase):
    """The two surfaces: a person's terminal, and an agent's JSON."""

    def test_the_human_report_names_what_is_wrong(self) -> None:
        (self.root / "cron" / "jobs.json").write_text("{oops", encoding="utf-8")
        text = doctor.render(self.check())
        self.assertIn("hermes-portal doctor", text)
        self.assertIn("drift:", text)
        self.assertIn("not covered by this check", text)

    def test_the_json_report_is_the_artifact_an_agent_can_act_on(self) -> None:
        (self.root / "cron" / "jobs.json").write_text("{oops", encoding="utf-8")
        payload = json.loads(doctor.as_json(self.check()))
        self.assertFalse(payload["ok"])
        self.assertTrue(
            any(f["subject"] == "jobs.json" for f in payload["drift"]),
            payload["drift"],
        )
        self.assertEqual(payload["drift"][0]["severity"], "drift")
        self.assertTrue(payload["not_covered"])
        self.assertIn("state.db schema_version", payload["hints"])

    def test_main_exit_codes_follow_the_verdict(self) -> None:
        self.assertEqual(doctor.main(["--hermes-home", str(self.root), "--quiet"]), 0)
        (self.root / "cron" / "jobs.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(doctor.main(["--hermes-home", str(self.root), "--quiet"]), 1)

    def test_the_server_routes_the_doctor_subcommand(self) -> None:
        """`hermes-portal doctor` is the console script's own entry point."""
        self.assertEqual(
            server.main(["doctor", "--hermes-home", str(self.root), "--quiet"]), 0
        )
        (self.root / "cron" / "jobs.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(
            server.main(["doctor", "--hermes-home", str(self.root), "--quiet"]), 1
        )


if __name__ == "__main__":  # pragma: no cover - run through the suite
    unittest.main()
