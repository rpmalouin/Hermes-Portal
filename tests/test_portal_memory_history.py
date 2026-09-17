"""Tests for memory history: older copies of MEMORY.md and USER.md.

Two sources, and only one of them usually has anything:

* **archives** -- ``<hermes root>/backups/*.zip``, where a full backup does contain
  ``memories/MEMORY.md`` and ``memories/USER.md``.  Only those members are read, in
  memory; the archive's ``.env`` sits right beside them and is never touched, which is
  asserted here with a secret in a fixture archive.
* **snapshot directories** -- the pre-update snapshots ship a ``manifest.json`` listing
  exactly what they hold, and they deliberately do not hold memories.  That is reported
  as a note rather than as an empty result, and the code still reads them if a future
  manifest ever lists a memory file.

Nothing is extracted to disk, which is also asserted: a page about old copies must not
unpack a 116 MB archive to show two small text files.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes.portal.domains import memory as memory_domain  # noqa: E402
from hermes.portal.domains import memory_files  # noqa: E402
from hermes.portal.model import DomainRegistry  # noqa: E402

SEPARATOR = "\u00a7"
SECRET = "sk-ARCHIVE-SECRET-9999"

OLD_MEMORY = (
    "Hermes model: Ron uses free Nemotron 3 via OpenRouter.\n\n"
    f"{SEPARATOR}\n\n"
    "Google Drive Hermes: Imports/Exports live in the Drive folder.\n\n"
    f"{SEPARATOR}\n\n"
    "An entry that gets reworded in place later on.\n"
)
OLD_USER = 'Ron Malouin (prefers "Ron").\n'

NEW_MEMORY = (
    "Model: Nemotron 3 via OpenRouter, and cron jobs store a model snapshot.\n\n"
    f"{SEPARATOR}\n\n"
    "An entry that was reworded in place later on, with more words added to it now.\n"
)
NEW_USER = f'Ron Malouin (prefers "Ron").\n\n{SEPARATOR}\n\nSecond user fact.\n'


def make_archive(path: Path, members: dict[str, bytes]) -> Path:
    """Write a zip with the given members."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def make_home(root: Path, *, archive: dict[str, bytes] | None = None) -> Path:
    """A Hermes home with live memories, a cron marker and an optional archive."""
    (root / "cron").mkdir(parents=True, exist_ok=True)
    (root / "cron" / "jobs.json").write_text('{"jobs": []}', encoding="utf-8")
    memories = root / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    (memories / "MEMORY.md").write_text(NEW_MEMORY, encoding="utf-8")
    (memories / "USER.md").write_text(NEW_USER, encoding="utf-8")
    if archive is not None:
        make_archive(
            root / "backups" / "hermes-backup-2026-08-25-000000-abcd.zip", archive
        )
    return root


def tree(path: Path) -> set[str]:
    """Every file under *path*, for proving nothing was extracted."""
    return {str(item.relative_to(path)) for item in path.rglob("*") if item.is_file()}


class DeltaTestCase(unittest.TestCase):
    """The comparison rule, on hand-made entries."""

    def entries(self, *texts: str, profile: str = "default", kind: str = "memory"):
        return memory_files._entries_for(profile, kind, SEPARATOR.join(texts))

    def test_identical_copies_report_no_change(self) -> None:
        delta = memory_files.delta_against(self.entries("one"), self.entries("one"))
        self.assertEqual(
            [len(delta[key]) for key in ("added", "removed", "changed")], [0, 0, 0]
        )

    def test_a_reworded_entry_is_changed_not_replaced(self) -> None:
        old = self.entries("Ship the portal, carefully.")
        new = self.entries("Ship the portal, carefully and slowly.")
        delta = memory_files.delta_against(old, new)
        self.assertEqual(len(delta["changed"]), 1)
        self.assertEqual(delta["added"], ())
        self.assertEqual(delta["removed"], ())

    def test_an_entry_that_lost_its_opening_line_pairs_by_overlap(self) -> None:
        """Title matching alone would call this a removal plus an addition."""
        old = self.entries(
            "Hermes macOS web-UI when Desktop mode is off: local patch and watchdog."
        )
        new = self.entries(
            "Hermes macOS web-UI with Desktop mode off: a local patch plus a watchdog."
        )
        delta = memory_files.delta_against(old, new)
        self.assertEqual(len(delta["changed"]), 1, delta)
        self.assertEqual(delta["added"], ())
        self.assertEqual(delta["removed"], ())

    def test_genuinely_different_entries_are_added_and_removed(self) -> None:
        old = self.entries("Google Drive Hermes: imports and exports live in Drive.")
        new = self.entries("Obsidian vault is at /Volumes/Data/MyObsidian now.")
        delta = memory_files.delta_against(old, new)
        self.assertEqual(len(delta["added"]), 1)
        self.assertEqual(len(delta["removed"]), 1)
        self.assertEqual(delta["changed"], ())

    def test_one_side_empty_is_all_added_or_all_removed(self) -> None:
        self.assertEqual(
            len(memory_files.delta_against((), self.entries("x"))["added"]), 1
        )
        self.assertEqual(
            len(memory_files.delta_against(self.entries("x"), ())["removed"]), 1
        )

    def test_overlap_is_a_word_set_ratio(self) -> None:
        self.assertEqual(memory_files._overlap("a b c", "a b c"), 1.0)
        self.assertEqual(memory_files._overlap("a b", "c d"), 0.0)
        self.assertEqual(memory_files._overlap("", "a"), 0.0)


class ArchiveDiscoveryTestCase(unittest.TestCase):
    """Reading archives without running, extracting or leaking anything."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.archive = {
            "memories/MEMORY.md": OLD_MEMORY.encode(),
            "memories/USER.md": OLD_USER.encode(),
            ".env": f"OPENAI_API_KEY={SECRET}\n".encode(),
            "config.yaml": b"memory_enabled: true\n",
            "hermes-backup/README.md": b"not a memory file at all\n",
        }
        make_home(self.root, archive=self.archive)
        self.registry = DomainRegistry()
        self.domain = memory_domain.build_domain(hermes_home=self.root)
        self.registry.register(self.domain)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def collections(self, filters=None):
        """Collection key -> collection."""
        return {
            collection.key: collection
            for collection in self.registry.safe_collections(self.domain, filters)
        }

    def test_the_archived_memory_files_are_found(self) -> None:
        snapshots = self.collections()["snapshots"]
        self.assertEqual(snapshots.count.value, 2)
        kinds = {record.id.split("/")[-1] for record in snapshots.records}
        self.assertEqual(kinds, {"memory", "user"})
        profile = {record.id.split("/")[-2] for record in snapshots.records}
        self.assertEqual(profile, {"default"})

    def test_only_memory_members_are_read(self) -> None:
        """The archive's .env is one directory away from the files this reads."""
        rendered = repr(self.registry.safe_collections(self.domain))
        rendered += repr(self.registry.safe_overview(self.domain))
        self.assertNotIn(SECRET, rendered)
        for record in self.collections()["snapshots"].records:
            detail = self.registry.safe_detail(self.domain, record.id)
            self.assertNotIn(SECRET, detail.body)
            for section in self.registry.safe_sections(self.domain, record.id):
                self.assertNotIn(SECRET, repr(section))

    def test_nothing_is_extracted_to_disk(self) -> None:
        before = tree(self.root)
        domain = memory_domain.build_domain(hermes_home=self.root)
        registry = DomainRegistry()
        registry.register(domain)
        registry.safe_collections(domain)
        self.assertEqual(tree(self.root), before)

    def test_the_delta_against_today_is_reported(self) -> None:
        snapshots = self.collections()["snapshots"]
        memory_row = next(
            record for record in snapshots.records if record.id.endswith("/memory")
        )
        fields = dict(memory_row.fields)
        # three archived entries against two today: the reworded one pairs by overlap
        # (changed), "Google Drive" is gone, and "Hermes model: Ron uses..." shares only
        # 33% of its words with today's model entry, which the 40% floor correctly reads
        # as two different entries rather than as one edit
        self.assertEqual(fields["entries then"], "3")
        self.assertEqual(fields["entries now"], "2")
        self.assertEqual(fields["removed since"], "2")
        self.assertEqual(fields["reworded since"], "1")
        self.assertEqual(fields["characters then"], f"{len(OLD_MEMORY):,}")

    def test_the_arcaded_text_is_the_body(self) -> None:
        detail = self.registry.safe_detail(
            self.domain, "history/hermes-backup-2026-08-25-000000-abcd/default/memory"
        )
        self.assertIn("Google Drive Hermes", detail.body)
        self.assertNotIn("cron jobs store a model snapshot", detail.body)

    def test_the_diff_sections_are_named_for_what_moved(self) -> None:
        sections = {
            section.key: section
            for section in self.registry.safe_sections(
                self.domain,
                "history/hermes-backup-2026-08-25-000000-abcd/default/memory",
            )
        }
        self.assertIn("then", sections)
        self.assertEqual(sections["then"].count.value, 3)
        self.assertEqual(sections["removed"].count.value, 2)
        self.assertEqual(sections["reworded"].count.value, 1)
        self.assertEqual(sections["added"].count.value, 1)

    def test_archived_entries_link_to_the_current_file(self) -> None:
        """An archived entry has no page of its own, so its row must not 404."""
        sections = self.registry.safe_sections(
            self.domain, "history/hermes-backup-2026-08-25-000000-abcd/default/memory"
        )
        for section in sections:
            for record in section.records:
                if record.href:
                    self.assertEqual(record.href, "/memory/default/memory")

    def test_the_current_file_links_to_its_past(self) -> None:
        sections = {
            section.key: section
            for section in self.registry.safe_sections(self.domain, "default/memory")
        }
        self.assertIn("history", sections)
        self.assertEqual(sections["history"].count.value, 1)
        self.assertEqual(
            sections["history"].records[0].id,
            "history/hermes-backup-2026-08-25-000000-abcd/default/memory",
        )

    def test_a_file_with_no_archived_copy_has_no_history_section(self) -> None:
        sections = {
            section.key
            for section in self.registry.safe_sections(self.domain, "default/user")
        }
        # the fixture archives USER.md too, so this asserts the mechanism rather than
        # the absence: a file that was never archived must not grow a section
        self.assertIn("history", sections)
        (self.root / "backups" / "hermes-backup-2026-08-25-000000-abcd.zip").unlink()
        fresh = memory_domain.build_domain(hermes_home=self.root)
        registry = DomainRegistry()
        registry.register(fresh)
        sections = {s.key for s in registry.safe_sections(fresh, "default/user")}
        self.assertNotIn("history", sections)

    def test_an_unknown_history_id_returns_nothing(self) -> None:
        self.assertIsNone(
            self.registry.safe_detail(self.domain, "history/nope/default/memory")
        )
        self.assertEqual(
            self.registry.safe_sections(self.domain, "history/nope/default/memory"), []
        )

    def test_collection_counts_match_their_records(self) -> None:
        for collection in self.registry.safe_collections(self.domain):
            if not collection.truncated:
                self.assertEqual(
                    collection.count.value,
                    len(collection.records),
                    f"{collection.key}: count {collection.count.value} != "
                    f"{len(collection.records)} records",
                )


class DegradationTestCase(unittest.TestCase):
    """What the domain says when history is thin, broken or absent."""

    def build(self, root: Path):
        """A registry over *root*."""
        registry = DomainRegistry()
        registry.register(memory_domain.build_domain(hermes_home=root))
        return registry.get("memory"), registry

    def test_no_archives_at_all_explains_where_history_comes_from(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_home(Path(tmp))
            domain, registry = self.build(root)
            snapshots = next(
                collection
                for collection in registry.safe_collections(domain)
                if collection.key == "snapshots"
            )
            self.assertEqual(snapshots.count.value, 0)
            notes = " ".join(snapshots.notes)
            self.assertIn("no older copy holds memories", notes)
            self.assertIn("backups", notes)

    def test_a_snapshot_that_keeps_no_memories_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_home(Path(tmp))
            snapshot = root / "state-snapshots" / "20260916-141254-pre-update"
            snapshot.mkdir(parents=True)
            (snapshot / "manifest.json").write_text(
                json.dumps({"id": "x", "files": {"state.db": 1, "config.yaml": 2}}),
                encoding="utf-8",
            )
            domain, registry = self.build(root)
            snapshots = next(
                collection
                for collection in registry.safe_collections(domain)
                if collection.key == "snapshots"
            )
            self.assertEqual(snapshots.count.value, 0)
            self.assertTrue(
                any("keeps no memories by design" in note for note in snapshots.notes),
                snapshots.notes,
            )
            overview = registry.safe_overview(domain)
            counts = {count.definition: count.value for count in overview.extra_counts}
            self.assertEqual(counts["archives and snapshots looked in"], 1)

    def test_a_snapshot_that_does_keep_memories_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_home(Path(tmp))
            snapshot = root / "state-snapshots" / "20260901-000000-pre-update"
            (snapshot / "memories").mkdir(parents=True)
            (snapshot / "memories" / "MEMORY.md").write_text(
                OLD_MEMORY, encoding="utf-8"
            )
            (snapshot / "manifest.json").write_text(
                json.dumps({"files": {"memories/MEMORY.md": len(OLD_MEMORY)}}),
                encoding="utf-8",
            )
            domain, registry = self.build(root)
            snapshots = next(
                collection
                for collection in registry.safe_collections(domain)
                if collection.key == "snapshots"
            )
            self.assertEqual(snapshots.count.value, 1)
            record = snapshots.records[0]
            self.assertIn("snapshot", dict(record.fields)["from"])
            self.assertTrue(
                any(badge.startswith("snapshot") for badge in record.badges),
                record.badges,
            )
            fields = dict(record.fields)
            self.assertIn(
                "snapshot state-snapshots" if False else "snapshot", fields["from"]
            )

    def test_a_corrupt_archive_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_home(Path(tmp))
            broken = root / "backups" / "broken-backup.zip"
            broken.parent.mkdir(parents=True, exist_ok=True)
            broken.write_bytes(b"this is not a zip file at all")
            domain, registry = self.build(root)
            snapshots = next(
                collection
                for collection in registry.safe_collections(domain)
                if collection.key == "snapshots"
            )
            self.assertEqual(snapshots.count.value, 0)
            self.assertTrue(
                any(
                    "BadZipFile" in note or "not a zip" in note
                    for note in snapshots.notes
                ),
                snapshots.notes,
            )

    def test_a_huge_member_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_home(Path(tmp))
            make_archive(
                root / "backups" / "huge-backup.zip",
                {"memories/MEMORY.md": b"A" * (memory_files.MAX_MEMBER_BYTES + 1)},
            )
            domain, registry = self.build(root)
            snapshots = next(
                collection
                for collection in registry.safe_collections(domain)
                if collection.key == "snapshots"
            )
            self.assertEqual(snapshots.count.value, 1)
            record = snapshots.records[0]
            # the member is skipped and said so, rather than read into memory
            self.assertIn("not read", record.badges)
            self.assertIn("not read", record.subtitle)
            detail = registry.safe_detail(domain, record.id)
            self.assertIn("not read", detail.badges)
            self.assertEqual(detail.body, "(nothing was archived)")


if __name__ == "__main__":
    unittest.main()
