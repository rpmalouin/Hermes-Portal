"""Tests for ``hermes-portal doctor``.

The fixture comes from ``tools/schema_snapshot.py``: the DDL those six tables really
have in a live Hermes, plus rows of the shapes those stores write (``state.db`` holds
epoch seconds as REAL, ``cron/executions.db`` holds ISO-8601 strings as TEXT).  That
makes the healthy case a real assertion -- the declared contract still matches a true
shape -- instead of a fixture built to agree with the code it is testing.
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
from tools.schema_snapshot import build_root, make_graph_db  # noqa: E402


class DoctorTestCase(unittest.TestCase):
    """A temp Hermes root, rebuilt fresh for every test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(Path(self._tmp.name))
        # An empty vault, so a check never walks a real one (the default is a path on
        # this machine, which would make the tests non-hermetic and slow).
        self.vault = Path(self._tmp.name) / "vault"
        self.vault.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def check(self) -> doctor.Report:
        """Run the doctor against this test's root."""
        return doctor.inspect(home=self.root, vault_root=self.vault)

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


class TestFileSurfaces(DoctorTestCase):
    """The file-backed pages: a format that moves empties a page instead of crashing."""

    def test_a_memory_file_the_page_does_not_recognise_is_drift(self) -> None:
        """The count is parsed out of the files, so a name it does not know reads 0."""
        memories = self.root / "memories"
        memories.mkdir()
        (memories / "NOTES.md").write_text("an entry\n§\nanother\n", encoding="utf-8")
        finding = next(f for f in self.check().drift if f.subject == "memory.entries")
        self.assertIn("read none of them", finding.detail)

    def test_a_memory_file_with_entries_is_reported_as_read(self) -> None:
        memories = self.root / "memories"
        memories.mkdir()
        (memories / "MEMORY.md").write_text("one\n§\ntwo\n", encoding="utf-8")
        findings = {f.subject: f for f in self.check().findings}
        self.assertIn("memory.entries", findings)
        self.assertIn("memory file(s)", findings["memory.entries"].detail)
        self.assertIn("the page reads", findings["memory.entries"].detail)

    def test_a_page_that_may_legitimately_be_empty_is_not_drift(self) -> None:
        """Log signatures are 0 on a healthy machine; firing on that teaches a reader
        to ignore the report, so that surface is counted without a verdict."""
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "agent.log").write_text(
            "2026-09-18 09:00:00,000 INFO nothing to see\n", encoding="utf-8"
        )
        report = self.check()
        self.assertTrue(report.ok, [f.subject for f in report.drift])
        self.assertTrue(any(f.subject == "logs.signatures" for f in report.findings))

    def test_the_empty_vault_is_reported_rather_than_assumed(self) -> None:
        findings = {f.subject: f for f in self.check().findings}
        self.assertIn("vault.notes", findings)
        self.assertIn("0 markdown note(s)", findings["vault.notes"].detail)


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
        base = [
            "--hermes-home",
            str(self.root),
            "--vault",
            str(self.vault),
            "--quiet",
        ]
        self.assertEqual(doctor.main(base), 0)
        (self.root / "cron" / "jobs.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(doctor.main(base), 1)

    def test_the_server_routes_the_doctor_subcommand(self) -> None:
        """`hermes-portal doctor` is the console script's own entry point."""
        base = [
            "doctor",
            "--hermes-home",
            str(self.root),
            "--vault",
            str(self.vault),
            "--quiet",
        ]
        self.assertEqual(server.main(base), 0)
        (self.root / "cron" / "jobs.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(server.main(base), 1)


if __name__ == "__main__":  # pragma: no cover - run through the suite
    unittest.main()
