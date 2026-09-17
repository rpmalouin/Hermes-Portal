"""Tests for the memory domain: MEMORY.md and USER.md, and their budgets.

Hermetic: a fake Hermes root with a ``memories/`` pair, a second profile, lock files,
a config naming the two budgets, and a profile with no memories at all.  Nothing here
reads the machine's own memory.

Two things get their own tests because they were wrong first:

* the **profile filter** -- a profile with no memories must show nothing and say so,
  and the filter has to survive the server's query-string whitelist;
* **search offsets** -- the snippet has to contain the match.  Scanning "title + text"
  and then slicing the text shifted every hit by the length of the title, so a search
  for ``dockerVM`` returned a fragment of some other sentence.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes.portal import server  # noqa: E402
from hermes.portal.domains import memory as memory_domain  # noqa: E402
from hermes.portal.domains import memory_files  # noqa: E402
from hermes.portal.model import DomainRegistry  # noqa: E402

SEPARATOR = "\u00a7"
MEMORY_TEXT = (
    "First note about the portal. It has detail.\n\n"
    f"{SEPARATOR}\n\n"
    "Second note mentioning dockerVM once, then other words.\n\n"
    f"{SEPARATOR}\n\n"
    "Third note, shorter.\n"
)
USER_TEXT = (
    f"The user is Ada. Prefers short answers.\n\n{SEPARATOR}\n\nSecond user fact.\n"
)


def make_memory_root(root: Path, *, config: str | None = None):
    """Build a fake Hermes home with memories, a profile and a config."""
    default = root / "memories"
    default.mkdir(parents=True, exist_ok=True)
    (default / "MEMORY.md").write_text(MEMORY_TEXT, encoding="utf-8")
    (default / "USER.md").write_text(USER_TEXT, encoding="utf-8")
    (default / "MEMORY.md.lock").write_text("", encoding="utf-8")
    other = root / "profiles" / "other" / "memories"
    other.mkdir(parents=True, exist_ok=True)
    (other / "MEMORY.md").write_text("Only one entry here.\n", encoding="utf-8")
    (root / "profiles" / "empty" / "memories").mkdir(parents=True, exist_ok=True)
    # a real Hermes root is identified by cron/jobs.json (a profile has its own), so
    # the fixture needs the marker or hermes_root() treats a profile dir as the root
    (root / "cron").mkdir(parents=True, exist_ok=True)
    (root / "cron" / "jobs.json").write_text('{"jobs": []}', encoding="utf-8")
    if config is not None:
        (root / "config.yaml").write_text(config, encoding="utf-8")
    return root


DEFAULT_CONFIG = "memory:\n  memory_char_limit: 2200\n  user_char_limit: 1375\n"


class MemoryDomainTestCase(unittest.TestCase):
    """The domain over a fixture root."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_memory_root(Path(self._tmp.name), config=DEFAULT_CONFIG)
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

    def test_it_finds_the_root_and_the_profiles(self) -> None:
        files = self.collections()["files"]
        titles = sorted(record.title for record in files.records)
        self.assertEqual(
            titles,
            ["MEMORY.md · default", "MEMORY.md · other", "USER.md · default"],
        )

    def test_a_profile_home_still_sees_the_root_store(self) -> None:
        """$HERMES_HOME points at a profile in a session; the root is a level up."""
        domain = memory_domain.build_domain(
            hermes_home=self.root / "profiles" / "other"
        )
        registry = DomainRegistry()
        registry.register(domain)
        titles = [
            record.title
            for collection in registry.safe_collections(domain)
            if collection.key == "files"
            for record in collection.records
        ]
        self.assertIn("MEMORY.md · default", titles)
        self.assertIn("MEMORY.md · other", titles)

    def test_entries_are_split_on_the_separator(self) -> None:
        entries = self.collections()["entries"]
        self.assertEqual(entries.count.value, 6)
        self.assertEqual(
            entries.records[0].title, "First note about the portal. It has detail."
        )
        self.assertIn("entry 3", entries.records[2].subtitle)

    def test_file_characters_are_what_the_limit_counts(self) -> None:
        files = {
            record.title: dict(record.fields)
            for record in self.collections()["files"].records
        }
        self.assertEqual(
            files["MEMORY.md · default"]["characters"], f"{len(MEMORY_TEXT):,}"
        )
        self.assertEqual(files["MEMORY.md · default"]["limit"], "2,200")
        self.assertEqual(
            files["USER.md · default"]["limit"],
            "1,375",
            "the two budgets are read separately",
        )

    def test_usage_is_measured_against_the_limit(self) -> None:
        files = {record.title: record for record in self.collections()["files"].records}
        record = files["MEMORY.md · default"]
        percent = 100.0 * len(MEMORY_TEXT) / 2200
        self.assertTrue(any(f"{percent:.0f}%" in badge for badge in record.badges))

    def test_a_file_at_its_limit_is_flagged(self) -> None:
        root = Path(self._tmp.name) / "tight"
        make_memory_root(root, config="memory_char_limit: 30\nuser_char_limit: 1375\n")
        domain = memory_domain.build_domain(hermes_home=root)
        registry = DomainRegistry()
        registry.register(domain)
        files = next(
            collection
            for collection in registry.safe_collections(domain)
            if collection.key == "files"
        )
        capped = [record for record in files.records if "at cap" in record.badges]
        self.assertTrue(capped, "a 30-char budget against a longer file is at cap")
        overview = registry.safe_overview(domain)
        self.assertEqual(dict(overview.metrics)["At cap"], str(len(capped)))

    def test_without_a_limit_it_reports_characters_and_says_so(self) -> None:
        root = Path(self._tmp.name) / "nolimits"
        make_memory_root(root, config="memory:\n  memory_enabled: true\n")
        domain = memory_domain.build_domain(hermes_home=root)
        registry = DomainRegistry()
        registry.register(domain)
        overview = registry.safe_overview(domain)
        self.assertTrue(
            any("does not name" in note for note in overview.notes), overview.notes
        )
        files = next(
            collection
            for collection in registry.safe_collections(domain)
            if collection.key == "files"
        )
        self.assertTrue(
            all("no limit configured" in record.badges for record in files.records)
        )
        self.assertNotIn("%", files.records[0].subtitle)

    def test_lock_files_are_counted_and_not_listed(self) -> None:
        overview = self.registry.safe_overview(self.domain)
        counts = {count.definition: count.value for count in overview.extra_counts}
        self.assertEqual(counts["lock files skipped"], 1)
        self.assertEqual(
            [record.title for record in self.collections()["files"].records].count(
                "MEMORY.md.lock"
            ),
            0,
        )

    def test_files_are_sorted_fullest_first(self) -> None:
        records = self.collections()["files"].records
        percents = []
        for record in records:
            usage = next((badge for badge in record.badges if "% of " in badge), "")
            percents.append(int(usage.split("%")[0]) if usage else -1)
        self.assertEqual(percents, sorted(percents, reverse=True))

    def test_overview_counts_match_the_files(self) -> None:
        overview = self.registry.safe_overview(self.domain)
        files = self.collections()["files"]
        self.assertEqual(overview.count.value, files.count.value)
        counts = {count.definition: count.value for count in overview.extra_counts}
        self.assertEqual(counts["files read"], files.count.value)
        self.assertEqual(counts["entries"], self.collections()["entries"].count.value)

    def test_the_overview_counts_every_file_even_when_it_shows_five(self) -> None:
        """The count must describe the set, not the sample the page happens to show.

        The fixture this suite shares has three files, which is *under* the overview's
        display cap, so it cannot catch a pre-sliced list being passed as the records
        (that bug shipped: the headline read 5 while the metrics read 6).  This builds
        enough profiles to cross the cap.
        """
        root = Path(self._tmp.name) / "many"
        make_memory_root(root, config=DEFAULT_CONFIG)
        for index in range(5):
            extra = root / "profiles" / f"p{index}" / "memories"
            extra.mkdir(parents=True, exist_ok=True)
            (extra / "MEMORY.md").write_text(
                f"Profile {index} memory.\n", encoding="utf-8"
            )
        domain = memory_domain.build_domain(hermes_home=root)
        registry = DomainRegistry()
        registry.register(domain)
        overview = registry.safe_overview(domain)
        files = next(
            collection
            for collection in registry.safe_collections(domain)
            if collection.key == "files"
        )
        self.assertGreater(files.count.value, 5, "the fixture must cross the cap")
        self.assertEqual(overview.count.value, files.count.value)
        self.assertEqual(dict(overview.metrics)["Files"], str(files.count.value))
        self.assertEqual(overview.shown, 5)
        self.assertTrue(overview.truncated)

    def test_collection_counts_match_their_records(self) -> None:
        for collection in self.registry.safe_collections(self.domain):
            if not collection.truncated:
                self.assertEqual(
                    collection.count.value,
                    len(collection.records),
                    f"{collection.key}: count {collection.count.value} != "
                    f"{len(collection.records)} records",
                )

    def test_the_profile_filter_narrows_and_is_visible(self) -> None:
        collections = self.collections({"profile": "default"})
        files = collections["files"]
        self.assertEqual(files.count.value, 2)
        self.assertEqual(files.picker.selected, "default")
        self.assertEqual(collections["entries"].count.value, 5)
        self.assertTrue(
            all(record.title.endswith("· default") for record in files.records)
        )

    def test_an_unknown_profile_shows_nothing_and_says_so(self) -> None:
        files = self.collections({"profile": "nope"})["files"]
        self.assertEqual(files.count.value, 0)
        self.assertFalse(files.records)
        self.assertTrue(any("no memories directory" in note for note in files.notes))

    def test_the_picker_offers_the_profiles_that_have_memories(self) -> None:
        picker = self.collections()["files"].picker
        labels = [option[1] for option in picker.options]
        self.assertEqual(labels, ["default (2)", "other (1)"])
        self.assertIn("All profiles", picker.all_label)
        self.assertEqual(picker.query_key, "profile")

    def test_the_server_passes_the_profile_filter_through(self) -> None:
        self.assertIn("profile", server.FILTER_KEYS)

    def test_file_detail_has_the_text_and_both_sections(self) -> None:
        detail = self.registry.safe_detail(self.domain, "default/memory")
        self.assertEqual(detail.title, "MEMORY.md · default")
        self.assertIn("dockerVM", detail.body)
        sections = {
            section.key
            for section in self.registry.safe_sections(self.domain, "default/memory")
        }
        self.assertEqual(sections, {"entries", "profiles"})

    def test_entry_detail_has_the_text_and_neighbours(self) -> None:
        detail = self.registry.safe_detail(self.domain, "default/memory/2")
        self.assertIn("dockerVM", detail.body)
        self.assertEqual(dict(detail.fields)["position"], "2 of 3")
        sections = self.registry.safe_sections(self.domain, "default/memory/2")
        self.assertEqual([section.key for section in sections], ["neighbours"])
        self.assertEqual(sections[0].count.value, 2)

    def test_unknown_ids_return_nothing(self) -> None:
        self.assertIsNone(self.registry.safe_detail(self.domain, "default/knowledge"))
        self.assertIsNone(self.registry.safe_detail(self.domain, "default/memory/99"))
        self.assertIsNone(self.registry.safe_detail(self.domain, "nope/memory"))
        self.assertEqual(self.registry.safe_sections(self.domain, "nope/memory"), [])

    def test_search_finds_an_entry_and_the_snippet_contains_the_hit(self) -> None:
        hits = self.domain.search("dockerVM", 5)
        self.assertEqual(len(hits), 1)
        self.assertIn("dockerVM", hits[0].subtitle)
        self.assertEqual(hits[0].id, "default/memory/2")

    def test_search_is_case_insensitive_and_ignores_an_empty_query(self) -> None:
        self.assertEqual(len(self.domain.search("DOCKERvm", 5)), 1)
        self.assertEqual(self.domain.search("   ", 5), [])

    def test_search_matches_a_title_whose_text_differs(self) -> None:
        hits = self.domain.search("Second user fact", 5)
        self.assertEqual([hit.id for hit in hits], ["default/user/2"])

    def test_an_unreadable_file_is_reported_not_fatal(self) -> None:
        root = Path(self._tmp.name) / "binary"
        make_memory_root(root)
        (root / "memories" / "MEMORY.md").write_bytes(b"\xff\xfe\x00bad utf-8")
        domain = memory_domain.build_domain(hermes_home=root)
        registry = DomainRegistry()
        registry.register(domain)
        files = next(
            collection
            for collection in registry.safe_collections(domain)
            if collection.key == "files"
        )
        broken = next(
            record for record in files.records if record.title == "MEMORY.md · default"
        )
        self.assertIn("unreadable", broken.badges)
        self.assertEqual(dict(broken.fields)["entries"], "0")

    def test_a_profile_with_no_memories_is_simply_absent(self) -> None:
        profiles = {
            record.title.split(" · ")[-1]
            for record in self.collections()["files"].records
        }
        self.assertNotIn("empty", profiles)

    def test_no_memories_anywhere_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            domain = memory_domain.build_domain(hermes_home=Path(tmp))
            registry = DomainRegistry()
            registry.register(domain)
            overview = registry.safe_overview(domain)
            self.assertEqual(overview.count.value, 0)
            self.assertTrue(
                any("no memories directory" in note for note in overview.notes)
            )
            for collection in registry.safe_collections(domain):
                self.assertEqual(collection.count.value, 0)

    def test_cross_domain_search_includes_memory(self) -> None:
        groups = self.registry.search("dockerVM", 5)
        self.assertIn("memory", groups)
        self.assertTrue(groups["memory"])


class PrimitivesTestCase(unittest.TestCase):
    """The memory primitives, called by name.

    These exist because the code-review graph flagged them as untested: the domain
    tests exercise them *through* a registry, which leaves no edge from the symbol to a
    test in the graph.  Calling each one directly is both a real test and the reference
    the graph needs to see.
    """

    def test_entry_key_chars_and_title(self) -> None:
        entry = memory_files.MemoryEntry(
            profile="p",
            kind="memory",
            file_key="p/memory",
            index=3,
            text="First line here.\nSecond line.",
        )
        self.assertEqual(entry.key, "p/memory/3")
        self.assertEqual(entry.chars, len(entry.text))
        self.assertEqual(entry.title, "First line here.")
        empty = memory_files.MemoryEntry(
            profile="p", kind="memory", file_key="p/memory", index=1, text="   \n\n"
        )
        self.assertEqual(empty.title, "(empty entry)")

    def test_file_budget_properties_without_a_limit(self) -> None:
        path = Path("/tmp/does-not-matter/MEMORY.md")
        memory_file = memory_domain.MemoryFile(
            profile="p", kind="memory", path=path, text="abc", limit=None
        )
        self.assertEqual(memory_file.label, "MEMORY.md")
        self.assertEqual(memory_file.chars, 3)
        self.assertIsNone(memory_file.percent)
        self.assertIsNone(memory_file.free)
        self.assertFalse(memory_file.at_cap)
        self.assertEqual(memory_file.key, "p/memory")

    def test_file_budget_properties_with_a_limit(self) -> None:
        path = Path("/tmp/does-not-matter/MEMORY.md")
        memory_file = memory_domain.MemoryFile(
            profile="p", kind="user", path=path, text="abcde", limit=10
        )
        self.assertEqual(memory_file.percent, 50.0)
        self.assertEqual(memory_file.free, 5)
        self.assertFalse(memory_file.at_cap)
        over = memory_domain.MemoryFile(
            profile="p", kind="user", path=path, text="abcdefghijk", limit=10
        )
        self.assertTrue(over.at_cap)
        self.assertEqual(over.free, -1)

    def test_archive_copy_label_and_chars(self) -> None:
        copy = memory_files.ArchiveCopy(
            key="history/x/p/user",
            label="x",
            when="2026-08-25",
            profile="p",
            kind="user",
            source="archive x.zip",
            path=Path("/tmp/x.zip"),
            member="memories/USER.md",
            text="1234",
        )
        self.assertEqual(copy.label_text, "USER.md")
        self.assertEqual(copy.chars, 4)

    def test_profile_is_derived_from_the_member_path(self) -> None:
        self.assertEqual(memory_files._profile_in("memories/MEMORY.md"), "default")
        self.assertEqual(
            memory_files._profile_in("profiles/purechat/memories/MEMORY.md"), "purechat"
        )
        self.assertEqual(memory_files._profile_in("some/prefix/USER.md"), "default")

    def test_entries_are_split_with_positions(self) -> None:
        text = f"one{memory_files.ENTRY_SEPARATOR}two{memory_files.ENTRY_SEPARATOR}\n\n"
        entries = memory_files._entries_for("p", "memory", text)
        self.assertEqual([entry.text for entry in entries], ["one", "two"])
        self.assertEqual([entry.index for entry in entries], [1, 2])
        self.assertEqual(memory_files._entries_for("p", "memory", ""), ())


class SnapshotContractTestCase(unittest.TestCase):
    """The class-based domain: one snapshot, cached, and builders that honour it.

    The filter regression this pins: after the conversion from closures to methods, the
    collection builders called ``self.snapshot()`` instead of using the snapshot they
    were handed, so a *filtered* view silently rendered the unfiltered one.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_memory_root(Path(self._tmp.name), config=DEFAULT_CONFIG)
        # the class itself, which is the point of the conversion: it can be named
        self.domain = memory_domain.MemoryDomain(hermes_home=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_snapshot_is_read_once(self) -> None:
        self.assertIsInstance(self.domain, memory_domain.MemoryDomain)
        first = self.domain.snapshot()
        self.assertIs(first, self.domain.snapshot())
        self.domain.forget()
        self.assertIsNot(first, self.domain.snapshot())

    def test_read_gathers_files_limits_and_history(self) -> None:
        snapshot = self.domain.read()
        self.assertEqual(len(snapshot.files), 3)
        self.assertEqual(snapshot.limits["memory"], 2200)
        self.assertEqual(snapshot.locks, 1)
        self.assertEqual(snapshot.by_key()["default/memory"].chars, len(MEMORY_TEXT))

    def test_narrowing_a_snapshot_keeps_the_history(self) -> None:
        """The filtered view lost its archived copies when this was rebuilt by hand."""
        snapshot = self.domain.snapshot()
        narrowed = snapshot.narrowed(
            tuple(item for item in snapshot.files if item.profile == "default")
        )
        self.assertEqual(len(narrowed.files), 2)
        self.assertEqual(narrowed.history, snapshot.history)
        self.assertEqual(narrowed.history_notes, snapshot.history_notes)
        self.assertEqual(narrowed.history_scanned, snapshot.history_scanned)

    def test_a_collection_builder_honours_the_snapshot_it_is_given(self) -> None:
        snapshot = self.domain.snapshot()
        everything = self.domain._files_collection(snapshot, "")
        self.assertEqual(everything.count.value, 3)
        narrowed = snapshot.narrowed(
            tuple(item for item in snapshot.files if item.profile == "default")
        )
        only_default = self.domain._files_collection(narrowed, "default")
        self.assertEqual(only_default.count.value, 2)
        self.assertEqual(only_default.picker.selected, "default")
        self.assertEqual(
            [record.title for record in only_default.records],
            sorted(record.title for record in only_default.records),
        )

    def test_build_domain_still_returns_a_wired_domain(self) -> None:
        """The registry's contract is unchanged by the conversion."""
        domain = memory_domain.build_domain(hermes_home=self.root)
        self.assertEqual(domain.key, "memory")
        for name in ("overview", "collections", "detail", "search", "detail_sections"):
            self.assertTrue(callable(getattr(domain, name)), name)
        # "nothing at all here" would match on "all"; use a word that is not there.
        # (A domain may return a list or a tuple; the registry wraps either.)
        self.assertFalse(domain.search("zzzznope", 5))


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
