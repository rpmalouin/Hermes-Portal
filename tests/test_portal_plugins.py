"""Tests for the plugins domain: manifests, kinds, and never running any of it.

Hermetic: a fake agent install with a bundled plugins tree (two kind containers, a
top-level plugin, a directory with no manifest, a manifest nested too deep) and a user
plugins directory that shadows one bundled plugin.

The test that matters most is :meth:`test_reading_a_plugin_never_runs_it`.  A plugin is
arbitrary Python, and a page that renders a plugin inventory must not import one: the
fixture ships plugins whose module writes a marker file on import, and the marker must
not exist afterwards.  Hermes' own loader documents the same discipline ("enumerates
without importing"), and this domain is written to keep it.

The second theme is the recurring one: a count describes the set, not the sample.  The
overview's display cap is 5, so the regression test builds 8 plugins and asserts the
headline matches the metrics -- a pre-sliced record list shipped once and read "2" while
its own metrics read "105".
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
from hermes.portal.domains import plugins as plugins_domain  # noqa: E402
from hermes.portal.model import DomainRegistry  # noqa: E402

MARKER_SOURCE = """\
# A plugin that does something on import: the domain must never import this.
from pathlib import Path

Path(__file__).with_name("IMPORTED").write_text("the portal ran plugin code")
"""

MANIFEST = """\
name: {name}
label: {label}
kind: {kind}
version: {version}
author: Test Author
description: >
  A fixture plugin used by the suite, described across
  several folded lines.
requires_env:
  - {env}
optional_env: [OPTIONAL_ONE, OPTIONAL_TWO]
pip_dependencies:
  - some-package>=1,<2
hooks:
  - pre_tool_call
unknown_block:
  nested: value
"""


def write_plugin(
    directory: Path,
    name: str,
    *,
    kind: str = "backend",
    label: str = "",
    env: str = "FIXTURE_KEY",
    entry: bool = False,
) -> Path:
    """Create one plugin directory with a manifest and a module."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(
        MANIFEST.format(
            name=name, label=label or name.title(), kind=kind, version="1.2.3", env=env
        ),
        encoding="utf-8",
    )
    (directory / "plugin.py").write_text(MARKER_SOURCE, encoding="utf-8")
    (directory / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    if entry:
        (directory / "extra").mkdir(exist_ok=True)
        (directory / "extra" / "notes.md").write_text("# notes\n", encoding="utf-8")
    return directory


def make_agent_tree(root: Path, *, plugin_count: int = 3) -> Path:
    """A fake Hermes home with a bundled agent tree and a user plugins dir."""
    bundled = root / "hermes-agent" / "plugins"
    for index in range(plugin_count):
        write_plugin(bundled / "web" / f"backend{index}", f"backend-{index}")
    write_plugin(bundled / "platforms" / "chat", "chat-platform", kind="platform")
    write_plugin(bundled / "spotify", "spotify", kind="standalone", label="Spotify")
    # a directory the loader would not treat as a plugin
    (bundled / "kanban").mkdir(parents=True, exist_ok=True)
    (bundled / "kanban" / "dashboard").mkdir(parents=True, exist_ok=True)
    # too deep for the documented contract (a manifest must sit in a plugin directory)
    write_plugin(bundled / "web" / "nested" / "deep", "too-deep")
    # a manifest with no kind: the container supplies it
    (bundled / "image_gen" / "painted").mkdir(parents=True, exist_ok=True)
    (bundled / "image_gen" / "painted" / "plugin.yaml").write_text(
        "name: painted\nversion: 0.1.0\ndescription: no kind declared\n",
        encoding="utf-8",
    )
    # user plugins: one shadows a bundled name
    user = root / "plugins"
    write_plugin(user / "spotify", "spotify", kind="standalone", label="Spotify (mine)")
    (root / "config.yaml").write_text(
        "known_plugin_toolsets:\n"
        "  cli:\n"
        "    - spotify\n"
        "    - chat-platform\n"
        "  gateway:\n"
        "    - nothing-here\n",
        encoding="utf-8",
    )
    return root


class PluginDomainTestCase(unittest.TestCase):
    """The domain over the fixture tree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_agent_tree(Path(self._tmp.name))
        self.registry = DomainRegistry()
        self.domain = plugins_domain.build_domain(hermes_home=self.root)
        self.registry.register(self.domain)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def collections(self, filters=None):
        """Collection key -> collection."""
        return {
            collection.key: collection
            for collection in self.registry.safe_collections(self.domain, filters)
        }

    def keys(self) -> set[str]:
        """Every plugin key found."""
        return {record.id for record in self.collections()["plugins"].records}

    # -- the security property ------------------------------------------

    def test_reading_a_plugin_never_runs_it(self) -> None:
        """The whole point: a plugin is arbitrary code and a page is not an import."""
        markers = sorted(Path(self._tmp.name).rglob("IMPORTED"))
        self.assertEqual(markers, [], f"plugin code ran: {markers}")
        # render everything too, in case a lazy path exists behind the renderer
        for collection in self.registry.safe_collections(self.domain):
            for record in collection.records:
                self.registry.safe_detail(self.domain, record.id)
                self.registry.safe_sections(self.domain, record.id)
        self.assertEqual(sorted(Path(self._tmp.name).rglob("IMPORTED")), [])

    def test_no_plugin_module_is_imported(self) -> None:
        before = set(sys.modules)
        plugins_domain.build_domain(hermes_home=self.root)
        leaked = sorted(
            name
            for name in set(sys.modules) - before
            if "fixture" in name or "backend0" in name or "spotify" in name
        )
        self.assertEqual(leaked, [])

    def test_env_values_are_never_read(self) -> None:
        """Only env var *names* are reported; a neighbouring secret stays unread."""
        secret = "sekrit-value-12345"
        (self.root / ".env").write_text(f"FIXTURE_KEY={secret}\n", encoding="utf-8")
        domain = plugins_domain.build_domain(hermes_home=self.root)
        registry = DomainRegistry()
        registry.register(domain)
        needs = next(
            collection
            for collection in registry.safe_collections(domain)
            if collection.key == "needs"
        )
        rendered = repr(needs.records) + repr(needs.notes)
        self.assertIn("FIXTURE_KEY", rendered)
        self.assertNotIn(secret, rendered)
        self.assertIn("Names only", needs.description)

    # -- discovery ------------------------------------------------------

    def test_it_finds_container_and_top_level_plugins(self) -> None:
        self.assertEqual(
            self.keys(),
            {
                "web/backend0",
                "web/backend1",
                "web/backend2",
                "platforms/chat",
                "spotify",
                "image_gen/painted",
                "user/spotify",
            },
        )

    def test_a_manifest_too_deep_is_ignored(self) -> None:
        self.assertNotIn("too-deep", " ".join(self.keys()))

    def test_container_plugins_get_their_container(self) -> None:
        records = {
            record.id: dict(record.fields)
            for record in self.collections()["plugins"].records
        }
        self.assertEqual(records["web/backend0"]["container"], "web")
        self.assertEqual(records["spotify"]["container"], "(top level)")

    def test_a_missing_kind_is_inferred_from_the_container(self) -> None:
        records = {
            record.id: record for record in self.collections()["plugins"].records
        }
        painted = records["image_gen/painted"]
        self.assertEqual(dict(painted.fields)["kind"], "image-gen")
        self.assertIn("kind inferred from its directory", painted.badges)
        declared = records["web/backend0"]
        self.assertNotIn("kind inferred from its directory", declared.badges)

    def test_a_user_plugin_shadows_a_bundled_one_and_both_are_shown(self) -> None:
        records = {
            record.id: record for record in self.collections()["plugins"].records
        }
        self.assertIn("spotify", records)
        self.assertIn("user/spotify", records)
        self.assertIn("Spotify (mine)", [record.title for record in records.values()])
        notes = " ".join(self.collections()["plugins"].notes)
        self.assertIn("shadows the bundled one", notes)

    def test_directories_without_a_manifest_are_listed(self) -> None:
        plugins = self.collections()["plugins"]
        counts = {count.definition: count.value for count in plugins.extra_counts}
        self.assertEqual(counts["directories without a plugin.yaml"], 1)
        sections = {
            s.key: s for s in self.registry.safe_sections(self.domain, "spotify")
        }
        self.assertIn("manifestless", sections)
        self.assertEqual(sections["manifestless"].records[0].title, "kanban")

    # -- the manifest subset -------------------------------------------

    def test_the_manifest_subset_reads_what_it_claims(self) -> None:
        detail = self.registry.safe_detail(self.domain, "web/backend0")
        fields = dict(detail.fields)
        self.assertEqual(fields["manifest name"], "backend-0")
        self.assertEqual(fields["version"], "1.2.3")
        self.assertEqual(fields["author"], "Test Author")
        self.assertEqual(fields["requires env"], "FIXTURE_KEY")
        self.assertEqual(fields["optional env"], "OPTIONAL_ONE, OPTIONAL_TWO")
        self.assertEqual(fields["hooks"], "pre_tool_call")
        self.assertIn("description", fields["declares"])

    def test_a_folded_description_becomes_one_line(self) -> None:
        detail = self.registry.safe_detail(self.domain, "web/backend0")
        self.assertIn("described across several folded lines", detail.subtitle)
        self.assertNotIn("\n", detail.subtitle)

    def test_the_nested_unknown_block_is_ignored(self) -> None:
        detail = self.registry.safe_detail(self.domain, "web/backend0")
        self.assertNotIn("nested", detail.body.split("unknown_block")[0][-40:])

    # -- config signals -------------------------------------------------

    def test_config_names_a_plugin_by_directory_or_manifest_name(self) -> None:
        records = {
            record.id: record for record in self.collections()["plugins"].records
        }
        spotify = [b for b in records["spotify"].badges if "named by config" in b]
        chat = [b for b in records["platforms/chat"].badges if "named by config" in b]
        self.assertTrue(spotify, "matched by directory name")
        self.assertTrue(chat, "matched by manifest name")
        self.assertIn("cli", spotify[0])

    def test_a_plugin_config_does_not_name_says_so_by_omission(self) -> None:
        records = {
            record.id: record for record in self.collections()["plugins"].records
        }
        self.assertFalse(
            [b for b in records["web/backend0"].badges if "named by config" in b]
        )

    # -- counts and filters ---------------------------------------------

    def test_the_overview_counts_every_plugin_even_when_it_shows_five(self) -> None:
        root = Path(self._tmp.name) / "many"
        make_agent_tree(root, plugin_count=7)
        domain = plugins_domain.build_domain(hermes_home=root)
        registry = DomainRegistry()
        registry.register(domain)
        overview = registry.safe_overview(domain)
        plugins = next(
            collection
            for collection in registry.safe_collections(domain)
            if collection.key == "plugins"
        )
        self.assertGreater(plugins.count.value, 5, "the fixture must cross the cap")
        self.assertEqual(overview.count.value, plugins.count.value)
        self.assertEqual(dict(overview.metrics)["Plugins"], str(plugins.count.value))
        self.assertEqual(overview.shown, 5)
        self.assertTrue(overview.truncated)

    def test_collection_counts_match_their_records(self) -> None:
        for collection in self.registry.safe_collections(self.domain):
            if not collection.truncated:
                self.assertEqual(
                    collection.count.value,
                    len(collection.records),
                    f"{collection.key}: {collection.count.value} != "
                    f"{len(collection.records)}",
                )

    def test_the_kind_filter_narrows_and_is_visible(self) -> None:
        collections = self.collections({"kind": "platform"})
        plugins = collections["plugins"]
        self.assertEqual(plugins.count.value, 1)
        self.assertEqual(plugins.picker.selected, "platform")
        self.assertEqual(plugins.records[0].id, "platforms/chat")
        # the count now describes a filtered set, so the rule and the whole are stated
        self.assertIn("in kind 'platform'", plugins.count.definition)
        counts = {count.definition: count.value for count in plugins.extra_counts}
        self.assertEqual(counts["hidden by the kind filter (of 7)"], 6)

    def test_an_unknown_kind_shows_nothing_and_says_so(self) -> None:
        plugins = self.collections({"kind": "nonsense"})["plugins"]
        self.assertEqual(plugins.count.value, 0)
        self.assertTrue(any("no plugin of kind" in note for note in plugins.notes))

    def test_the_server_passes_the_kind_filter_through(self) -> None:
        self.assertIn("kind", server.FILTER_KEYS)

    def test_kinds_are_summarised(self) -> None:
        kinds = {
            record.id: dict(record.fields)
            for record in self.collections()["kinds"].records
        }
        self.assertEqual(kinds["platform"]["plugins"], "1")
        self.assertEqual(kinds["backend"]["plugins"], "3")
        self.assertIn(
            "inferred",
            " ".join(
                record.subtitle for record in self.collections()["kinds"].records
            ).lower()
            + " "
            + " ".join(
                badge
                for record in self.collections()["kinds"].records
                for badge in record.badges
            ).lower(),
        )

    def test_needs_lists_plugins_that_declare_requirements(self) -> None:
        # three backends, the platform, and both spotify manifests declare requires_env;
        # the manifest-less kind fixture (image_gen/painted) declares nothing
        needs = self.collections()["needs"]
        self.assertEqual(needs.count.value, 6)
        self.assertEqual(
            sorted({dict(record.fields)["requires"] for record in needs.records}),
            ["FIXTURE_KEY"],
        )

    # -- detail and search ----------------------------------------------

    def test_detail_sections_are_files_and_neighbours(self) -> None:
        sections = {
            section.key: section
            for section in self.registry.safe_sections(self.domain, "web/backend0")
        }
        self.assertIn("files", sections)
        self.assertIn("same-kind", sections)
        names = [record.title for record in sections["files"].records]
        self.assertIn("plugin.py", names)
        self.assertIn("helper.py", names)

    def test_an_unknown_plugin_returns_nothing(self) -> None:
        self.assertIsNone(self.registry.safe_detail(self.domain, "web/absent"))
        self.assertEqual(self.registry.safe_sections(self.domain, "web/absent"), [])

    def test_search_finds_by_name_kind_description_and_env(self) -> None:
        self.assertEqual(
            [r.id for r in self.domain.search("backend-1", 5)], ["web/backend1"]
        )
        self.assertTrue(self.domain.search("platform", 5))
        self.assertTrue(self.domain.search("folded lines", 5))
        self.assertTrue(self.domain.search("FIXTURE_KEY", 5))
        self.assertEqual(self.domain.search("  ", 5), [])

    def test_no_plugins_anywhere_degrades_with_a_useful_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            domain = plugins_domain.build_domain(hermes_home=Path(tmp))
            registry = DomainRegistry()
            registry.register(domain)
            overview = registry.safe_overview(domain)
            self.assertEqual(overview.count.value, 0)
            self.assertTrue(any("no plugin.yaml found" in n for n in overview.notes))
            self.assertTrue(any("hermes-agent/plugins" in n for n in overview.notes))
            for collection in registry.safe_collections(domain):
                self.assertEqual(collection.count.value, 0)


class RegistryTestCase(unittest.TestCase):
    """This file's own claim: the plugins domain is registered and wired."""

    def test_plugins_is_registered(self) -> None:
        registry = server.default_registry(hermes_home=Path(tempfile.mkdtemp()))
        self.assertIn("plugins", registry.keys())
        self.assertEqual(registry.get("plugins").key, "plugins")


if __name__ == "__main__":
    unittest.main()
