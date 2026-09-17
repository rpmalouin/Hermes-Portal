"""Tests for P3: favourites (the write path), the theme, and the palette.

The portal was read-only until this phase, so most of what is here exists to pin
down the *new* promise rather than the old one:

* **the write path has a blast radius of one file.**  ``tree_snapshot`` is taken
  before and after a POST and the diff must be exactly the state document, inside
  the portal's own directory.  A star must never touch ``state.db``, ``cron/``, a
  skill tree or a vault note.
* **a rejected request never reaches the filesystem.**  Media type, length, JSON,
  domain and record are checked in that order, so a malformed body cannot create or
  modify anything.
* **the store is atomic and forgiving.**  No temporary files are left behind, the
  document is written 0600, and a corrupt file is reported rather than raised.
* **nothing is rendered unescaped.**  A favourite's title comes from a record, and
  records carry HTML from vault notes and skill descriptions; it is escaped on the
  way out and checked here.

The server harness reuses ``tests.test_portal``'s fixture and probes, so this file
still says the same thing on any machine (no launchctl, no lsof, no ~/Library/Logs).
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes.portal import model as registry_module  # noqa: E402
from hermes.portal import render, server, taxonomy  # noqa: E402
from hermes.portal.domains import health as health_domain  # noqa: E402
from hermes.portal.domains import logs as logs_domain  # noqa: E402
from hermes.portal.state import (  # noqa: E402
    MAX_FAVORITES,
    Favorite,
    PortalState,
    StateError,
    favorite_from_payload,
)
from tests.test_portal import fetch, make_hermes_root, tree_snapshot  # noqa: E402


def post(
    url: str,
    payload: object = None,
    *,
    content_type: str = "application/json",
    raw: bytes | None = None,
) -> tuple[int, dict]:
    """POST to *url*, returning ``(status, json)`` for success and error alike."""
    body = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


@contextlib.contextmanager
def run_portal(
    root: Path,
    *,
    state_path: Path | None = None,
    no_state: bool = False,
) -> Iterator[str]:
    """Serve the portal for *root* with a state store, yielding its base URL."""
    saved = (
        server.PortalHandler.registry,
        server.PortalHandler.built_at,
        server.PortalHandler.state,
    )
    server.PortalHandler.registry = server.default_registry(hermes_home=root)
    server.PortalHandler.built_at = "STAMP"
    server.PortalHandler.state = None if no_state else PortalState(state_path)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.PortalHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
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
            server.PortalHandler.state,
        ) = saved


class StateStoreTestCase(unittest.TestCase):
    """The favourites store on its own, no server involved."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "portal" / "state.json"
        self.store = PortalState(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_constructing_creates_nothing(self) -> None:
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_reading_a_missing_file_is_not_an_error(self) -> None:
        snapshot = self.store.read()
        self.assertEqual(snapshot.favorites, ())
        self.assertFalse(snapshot.exists)
        self.assertEqual(snapshot.error, "")

    def test_add_writes_the_document_with_owner_only_permissions(self) -> None:
        snapshot = self.store.toggle("skills", "alpha", "Alpha", add=True)
        self.assertEqual([f.key for f in snapshot.favorites], ["skills|alpha"])
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        self.assertEqual(raw["favorites"][0]["domain"], "skills")
        self.assertTrue(raw["updated_at"])
        mode = self.path.stat().st_mode % 0o1000
        self.assertEqual(mode, 0o600)

    def test_no_temporary_files_are_left_behind(self) -> None:
        for index in range(4):
            self.store.toggle("vault", f"note-{index}", add=True)
        leftovers = [
            p.name for p in self.path.parent.iterdir() if p.name != "state.json"
        ]
        self.assertEqual(leftovers, [])

    def test_toggle_flips_and_adding_twice_is_idempotent(self) -> None:
        self.store.toggle("cron", "job", "job", add=True)
        self.store.toggle("cron", "job", "job", add=True)
        self.assertEqual(len(self.store.read().favorites), 1)
        self.store.toggle("cron", "job")
        self.assertEqual(len(self.store.read().favorites), 0)
        self.store.toggle("cron", "job")
        self.assertEqual(len(self.store.read().favorites), 1)

    def test_removing_something_absent_is_quiet(self) -> None:
        snapshot = self.store.toggle("graph", "absent", add=False)
        self.assertEqual(snapshot.favorites, ())

    def test_the_list_is_capped(self) -> None:
        for index in range(MAX_FAVORITES):
            self.store.toggle("skills", f"skill-{index}", add=True)
        with self.assertRaises(StateError) as caught:
            self.store.toggle("skills", "one-too-many", add=True)
        self.assertIn("full", str(caught.exception))
        self.assertEqual(len(self.store.read().favorites), MAX_FAVORITES)

    def test_input_is_validated(self) -> None:
        with self.assertRaises(StateError):
            self.store.toggle("", "id")
        with self.assertRaises(StateError):
            self.store.toggle("skills", "   ")
        with self.assertRaises(StateError):
            self.store.toggle("skills", "x" * 513)
        with self.assertRaises(StateError):
            self.store.toggle("skills", "bad\nnewline")

    def test_a_corrupt_file_is_reported_not_raised(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ this is not json", encoding="utf-8")
        snapshot = self.store.read()
        self.assertTrue(snapshot.error)
        self.assertEqual(snapshot.favorites, ())

    def test_a_write_repairs_a_corrupt_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("[]", encoding="utf-8")
        self.store.toggle("skills", "alpha", add=True)
        self.assertEqual(self.store.read().error, "")
        self.assertEqual(len(self.store.read().favorites), 1)

    def test_entries_missing_fields_are_skipped(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "favorites": [
                        {"domain": "skills", "id": "kept"},
                        {"domain": "", "id": "dropped"},
                        {"id": "dropped-too"},
                        "not-an-object",
                    ],
                }
            ),
            encoding="utf-8",
        )
        favorites = self.store.read().favorites
        self.assertEqual([f.id for f in favorites], ["kept"])
        self.assertEqual(favorites[0].title, "kept")

    def test_a_store_with_no_path_refuses_to_write_but_reads_empty(self) -> None:
        store = PortalState(None)
        self.assertEqual(store.read().favorites, ())
        with self.assertRaises(StateError):
            store.toggle("skills", "alpha")

    def test_favorites_come_back_in_the_order_they_were_added(self) -> None:
        for name in ("one", "two", "three"):
            self.store.toggle("skills", name, add=True)
        self.assertEqual(
            [f.id for f in self.store.read().favorites], ["one", "two", "three"]
        )


class PayloadTestCase(unittest.TestCase):
    """The POST body reader."""

    def test_actions_are_understood(self) -> None:
        for action, expected in (
            ("add", True),
            ("star", True),
            ("remove", False),
            ("unstar", False),
            ("delete", False),
            ("toggle", None),
            ("", None),
        ):
            with self.subTest(action=action):
                _domain, _id, _title, add = favorite_from_payload(
                    {"domain": "skills", "id": "x", "action": action}
                )
                self.assertIs(add, expected)

    def test_a_missing_action_toggles(self) -> None:
        _d, _i, _t, add = favorite_from_payload({"domain": "skills", "id": "x"})
        self.assertIsNone(add)

    def test_a_boolean_action_is_accepted(self) -> None:
        _d, _i, _t, add = favorite_from_payload(
            {"domain": "skills", "id": "x", "action": False}
        )
        self.assertIs(add, False)

    def test_unknown_actions_and_bodies_are_rejected(self) -> None:
        with self.assertRaises(StateError):
            favorite_from_payload({"domain": "skills", "id": "x", "action": "burn"})
        with self.assertRaises(StateError):
            favorite_from_payload(["not", "an", "object"])


class RenderTestCase(unittest.TestCase):
    """The theme, the palette, the star buttons and the rail."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = make_hermes_root(Path(cls._tmp.name))
        cls.registry = server.default_registry(hermes_home=Path(cls._tmp.name))
        cls.domains = cls.registry.all()
        cls.overviews = cls.registry.overviews()
        cls.store = PortalState(Path(cls._tmp.name) / "portal" / "state.json")
        cls.store.toggle("skills", "alpha", "Alpha", add=True)
        cls.favorites = cls.store.read().favorites

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def index(self) -> str:
        """The rendered home page."""
        return render.render_index(
            self.domains, self.overviews, "STAMP", favorites=self.favorites
        )

    def test_both_themes_are_defined_with_variables(self) -> None:
        page = self.index()
        self.assertIn("--bg:", page)
        self.assertIn(':root[data-theme="light"]', page)
        self.assertIn("color-scheme: dark", page)
        self.assertIn("color-scheme: light", page)

    def test_the_theme_is_chosen_before_the_first_paint(self) -> None:
        page = self.index()
        self.assertIn("portal-theme", page)
        self.assertIn("prefers-color-scheme", page)
        # the decision happens in <head>, before the stylesheet and the body
        self.assertLess(page.index("portal-theme"), page.index("<style>"))
        self.assertLess(page.index("prefers-color-scheme"), page.index("</head>"))

    def test_the_toggle_and_the_palette_are_in_the_header(self) -> None:
        page = self.index()
        header = page[page.index("<header") : page.index("</header>")]
        self.assertIn('id="theme-toggle"', header)
        self.assertIn('id="palette-open"', header)
        self.assertIn('id="palette"', page)
        self.assertIn("/app.js", page)

    def test_the_script_cannot_break_out_of_its_tag(self) -> None:
        self.assertNotIn("</script", render.APP_JS.lower())
        self.assertNotIn("<script", render.APP_JS.lower())

    def test_detail_links_render_their_href_and_label_the_right_way_round(self) -> None:
        """The bug this pins: links were rendered with the label as the href.

        Every domain builds ``((href, label), ...)``; the renderer unpacked them the
        other way, so a "Its folder" link pointed at the literal string ``Its folder``
        and went nowhere.
        """
        record = render.Record(
            id="x", title="X", links=(("/vault?folder=Homelab", "Its folder"),)
        )
        page = render.render_detail(
            self.registry.get("skills"), record, [], self.domains, "STAMP"
        )
        self.assertIn('<a href="/vault?folder=Homelab">Its folder</a>', page)
        self.assertNotIn('href="Its folder"', page)

    def test_star_buttons_render_unpressed_and_are_hydrated(self) -> None:
        page = self.index()
        pressed = re.findall(r'<button class="star"[^>]*aria-pressed="(\w+)"', page)
        self.assertTrue(pressed, "no star buttons were rendered")
        # the state is never baked into the markup: the script reads it back, so
        # every button ships unpressed (the CSS *selector* mentions "true")
        self.assertEqual(set(pressed), {"false"})
        self.assertIn("/favorites.json", render.APP_JS)

    def test_a_star_button_carries_the_domain_and_the_id(self) -> None:
        button = render.star_button(
            "vault", "Hermes/Hermes Kanban.md", 'A "quoted" note'
        )
        self.assertIn('data-fav="vault|Hermes/Hermes Kanban.md"', button)
        self.assertIn("&quot;quoted&quot;", button)

    def test_the_rail_shows_usage_favourites_and_every_domain(self) -> None:
        page = self.index()
        self.assertIn("Usage overview", page)
        self.assertIn("Favourites", page)
        self.assertIn("At a glance", page)
        self.assertIn('class="rail"', page)
        self.assertIn("with-rail", page)

    def test_an_empty_favourites_panel_invites_a_star(self) -> None:
        page = render.render_index(self.domains, self.overviews, "STAMP")
        self.assertIn("Star anything", page)

    def test_the_favourites_panel_lists_the_first_eight(self) -> None:
        many = tuple(
            Favorite(
                domain="skills",
                id=f"skill-{index}",
                title=f"Skill {index}",
                added_at="2026-01-01",
            )
            for index in range(11)
        )
        panel = render._favorites_panel(many)
        self.assertIn("all 11", panel)
        self.assertEqual(panel.count('class="stat"'), 8)

    def test_titles_are_escaped_on_the_favourites_page(self) -> None:
        page = render.render_favorites(
            self.store.read().favorites, self.domains, "STAMP"
        )
        self.assertIn("Alpha", page)
        # exactly the two scripts the shell ships: no script from a record's title
        self.assertEqual(page.count("<script"), 2)
        self.assertIn("/app.js", page)

    def test_a_hostile_title_is_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PortalState(Path(tmp) / "state.json")
            store.toggle("vault", "note", "<script>alert(1)</script>", add=True)
            favorites = store.read().favorites
        page = render.render_favorites(favorites, self.domains, "STAMP")
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_the_empty_favourites_page_says_so(self) -> None:
        page = render.render_favorites((), self.domains, "STAMP")
        self.assertIn("Nothing is starred yet", page)

    def test_a_state_note_is_rendered(self) -> None:
        page = render.render_favorites((), self.domains, "STAMP", "state is broken")
        self.assertIn("state is broken", page)


class BannerTestCase(unittest.TestCase):
    """The startup banner must not claim more safety than the process has."""

    def test_without_a_store_it_says_writing_is_off(self) -> None:
        line = server.write_policy(None)
        self.assertIn("Read-only", line)
        self.assertIn("--no-state", line)

    def test_with_a_store_it_names_the_one_file_a_request_can_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            line = server.write_policy(PortalState(Path(tmp) / "state.json"))
        self.assertIn("Read-only except favourites", line)
        self.assertIn("state.json", line)


class TilesTestCase(unittest.TestCase):
    """The curated box tiles: the mapping, its arithmetic, and its honesty."""

    def test_the_mapping_is_well_formed(self) -> None:
        keys = [group.key for group in taxonomy.GROUPS]
        self.assertEqual(len(keys), len(set(keys)), "group keys must be unique")
        for group in taxonomy.GROUPS:
            with self.subTest(group=group.key):
                self.assertTrue(group.title)
                self.assertTrue(group.blurb)
                self.assertTrue(group.emoji)
                self.assertTrue(group.boxes, "a group with no boxes renders empty")
                self.assertEqual(len(group.gradient), 2)
                for colour in group.gradient:
                    self.assertRegex(colour, r"^#[0-9a-f]{6}$")

    def test_a_box_belongs_to_at_most_one_group(self) -> None:
        seen: dict[str, str] = {}
        for group in taxonomy.GROUPS:
            for box in group.boxes:
                self.assertNotIn(
                    box, seen, f"{box!r} is in both {seen.get(box)!r} and {group.key!r}"
                )
                seen[box] = group.key

    def test_coverage_sums_the_real_counts(self) -> None:
        counts = {"software-development": 31, "github": 6, "creative": 17, "zzz": 2}
        cov = taxonomy.coverage(counts)
        build = next(row for row in cov.rows if row.group.key == "build")
        self.assertEqual(build.skills, 37)
        self.assertEqual(cov.boxes, 4)
        self.assertEqual(cov.skills, 56)
        self.assertEqual([name for name, _c in cov.ungrouped], ["zzz"])
        self.assertEqual(cov.covered_boxes, 3)
        self.assertEqual(cov.covered_skills, 54)

    def test_every_count_is_accounted_for(self) -> None:
        counts = dict(self.fake_counts())
        cov = taxonomy.coverage(counts)
        grouped = sum(row.skills for row in cov.rows)
        ungrouped = sum(count for _name, count in cov.ungrouped)
        self.assertEqual(grouped + ungrouped, cov.skills)
        self.assertEqual(cov.skills, sum(counts.values()))

    def fake_counts(self) -> dict[str, int]:
        """A box map with two known boxes and one nobody has heard of."""
        return {"creative": 4, "software-development": 9, "a-brand-new-box": 3}

    def test_an_unknown_box_is_reported_not_hidden(self) -> None:
        cov = taxonomy.coverage(self.fake_counts())
        self.assertIn("a-brand-new-box", [name for name, _c in cov.ungrouped])
        page = render.render_tiles(cov)
        self.assertIn("Not in a group yet", page)
        self.assertIn("a-brand-new-box", page)

    def test_a_renamed_box_shows_as_a_stale_mapping_entry(self) -> None:
        cov = taxonomy.coverage({"creative": 4})
        review = next(row for row in cov.rows if row.group.key == "review")
        self.assertEqual(review.skills, 0)
        self.assertTrue(review.missing)
        page = render.render_tiles(cov)
        self.assertIn("no longer exist", page)

    def test_an_empty_tree_renders_without_crashing(self) -> None:
        cov = taxonomy.coverage({})
        self.assertEqual(cov.boxes, 0)
        self.assertEqual(cov.covered_boxes, 0)
        self.assertTrue(cov.stale)
        page = render.render_tiles(cov)
        # the groups are declared, so they still render; nothing is covered
        self.assertIn(f"{len(taxonomy.GROUPS)} groups over the 0 boxes", page)
        self.assertIn("covering 0 of them", page)

    def test_box_names_are_escaped_and_encoded(self) -> None:
        """A box name is a directory name, so it is untrusted input."""
        cov = taxonomy.coverage({"<script>alert(1)</script>": 2, "has space": 1})
        page = render.render_tiles(cov)
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("has%20space", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_a_tile_lists_members_and_caps_the_chips(self) -> None:
        many = {box: 1 for group in taxonomy.GROUPS for box in group.boxes}
        page = render.render_tiles(taxonomy.coverage(many))
        build = page[page.index("Build &amp; ship") : page.index("Review &amp; design")]
        self.assertIn("+7 more boxes", build)
        self.assertEqual(build.count('class="chip"'), 6)

    def test_the_coverage_line_states_the_arithmetic(self) -> None:
        page = render.render_tiles(taxonomy.coverage(self.fake_counts()))
        self.assertIn("8 groups over the 3 boxes", page)
        self.assertIn("covering 2 of them", page)
        self.assertIn("the counts are the tree's", page.lower())

    def test_the_index_puts_the_tiles_before_the_domain_cards(self) -> None:
        registry = server.default_registry(hermes_home=Path(self.tmp.name))
        tiles = render.render_tiles(taxonomy.coverage(server.box_counts(registry)))
        page = render.render_index(
            registry.all(), registry.overviews(), "STAMP", tiles=tiles
        )
        self.assertIn('class="tiles"', page)
        self.assertLess(page.index('class="tiles"'), page.index('class="grid"'))

    def test_box_counts_come_from_the_skills_domain(self) -> None:
        """One measurement: the tiles and the skills page cannot disagree."""
        registry = server.default_registry(hermes_home=Path(self.tmp.name))
        counts = server.box_counts(registry)
        boxes = next(
            collection
            for collection in registry.safe_collections(registry.get("skills"))
            if collection.key == "boxes"
        )
        self.assertEqual(
            counts,
            {
                record.title: int(dict(record.fields)["skills"])
                for record in boxes.records
            },
        )
        self.assertEqual(
            sum(counts.values()),
            sum(int(dict(record.fields)["skills"]) for record in boxes.records),
        )

    def test_box_counts_tolerates_a_registry_without_skills(self) -> None:
        registry = registry_module.DomainRegistry()
        self.assertEqual(server.box_counts(registry), {})

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_hermes_root(Path(self.tmp.name))
        self.tmp_root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class FavouritesRouteTestCase(unittest.TestCase):
    """The POST and its failures, against a live handler on an ephemeral port."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_hermes_root(Path(self._tmp.name))
        self.state_path = Path(self._tmp.name) / "portal" / "state.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_starring_writes_one_file_and_nothing_else(self) -> None:
        """The whole write path, measured: a star changes exactly one file.

        The fixture root contains the state file (it stands in for the Hermes home),
        so the diff is the complete list of paths this POST touched -- and it has to
        be the portal's own document, never ``state.db``, ``cron/``, a skill or a note.
        """
        before = tree_snapshot(self.root)
        with run_portal(self.root, state_path=self.state_path) as base:
            status, payload = post(
                f"{base}/favorites.json",
                {"domain": "skills", "id": "alpha", "action": "add"},
            )
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["starred"])
            self.assertEqual(payload["count"], 1)
        after = tree_snapshot(self.root)
        changed = {
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        }
        # the complete list of paths this POST touched
        self.assertEqual(changed, {"portal/state.json"}, sorted(changed))
        self.assertTrue(self.state_path.is_file())

    def test_the_title_comes_from_the_record_not_the_client(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            post(
                f"{base}/favorites.json",
                {"domain": "skills", "id": "alpha", "title": "LIED"},
            )
        stored = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotEqual(stored["favorites"][0]["title"], "LIED")

    def test_unstarring_removes_it(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            post(f"{base}/favorites.json", {"domain": "skills", "id": "alpha"})
            status, payload = post(
                f"{base}/favorites.json",
                {"domain": "skills", "id": "alpha", "action": "remove"},
            )
            self.assertEqual(status, 200)
            self.assertFalse(payload["starred"])
            self.assertEqual(payload["count"], 0)

    def test_unstarring_something_absent_is_still_ok(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            status, payload = post(
                f"{base}/favorites.json",
                {"domain": "skills", "id": "never-starred", "action": "remove"},
            )
            self.assertEqual(status, 200)
            self.assertFalse(payload["starred"])

    def test_an_unknown_domain_is_rejected(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            status, payload = post(
                f"{base}/favorites.json", {"domain": "nope", "id": "x"}
            )
            self.assertEqual(status, 400)
            self.assertIn("no domain", payload["error"])
        self.assertFalse(self.state_path.exists())

    def test_an_unknown_record_is_rejected(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            status, payload = post(
                f"{base}/favorites.json", {"domain": "skills", "id": "no-such-skill"}
            )
            self.assertEqual(status, 404)
            self.assertIn("no record", payload["error"])
        self.assertFalse(self.state_path.exists())

    def test_a_wrong_media_type_is_rejected(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            status, payload = post(
                f"{base}/favorites.json",
                {"domain": "skills", "id": "alpha"},
                content_type="text/plain",
            )
            self.assertEqual(status, 415)
            self.assertIn("application/json", payload["error"])

    def test_a_malformed_body_is_rejected(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            status, payload = post(
                f"{base}/favorites.json",
                raw=b"{not json",
                content_type="application/json",
            )
            self.assertEqual(status, 400)
            self.assertIn("not JSON", payload["error"])
        self.assertFalse(self.state_path.exists())

    def test_an_empty_body_is_rejected(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            status, _payload = post(f"{base}/favorites.json", raw=b"")
            self.assertEqual(status, 400)

    def test_an_oversized_body_is_rejected(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            big = json.dumps({"domain": "skills", "id": "x" * 9000}).encode("utf-8")
            status, payload = post(f"{base}/favorites.json", raw=big)
            self.assertEqual(status, 413)
            self.assertIn("larger than", payload["error"])
        self.assertFalse(self.state_path.exists())

    def test_an_unknown_post_route_is_rejected(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            status, payload = post(
                f"{base}/skills.json", {"domain": "skills", "id": "x"}
            )
            self.assertEqual(status, 404)
            self.assertIn("no POST route", payload["error"])

    def test_no_state_refuses_the_write_and_says_why(self) -> None:
        with run_portal(self.root, no_state=True) as base:
            status, payload = post(
                f"{base}/favorites.json", {"domain": "skills", "id": "alpha"}
            )
            self.assertEqual(status, 409)
            self.assertIn("no-state", payload["error"])
            _status, _ctype, listed = fetch(f"{base}/favorites.json")
            self.assertIn('"writable": false', listed)

    def test_the_favourites_json_lists_what_is_starred(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            post(f"{base}/favorites.json", {"domain": "skills", "id": "alpha"})
            status, _ctype, body = fetch(f"{base}/favorites.json")
            payload = json.loads(body)
            self.assertEqual(status, 200)
            self.assertTrue(payload["writable"])
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["favorites"][0]["id"], "alpha")
            self.assertEqual(payload["path"], str(self.state_path))
            self.assertEqual(payload["error"], "")

    def test_the_favourites_page_renders(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            post(f"{base}/favorites.json", {"domain": "skills", "id": "alpha"})
            status, content_type, page = fetch(f"{base}/favorites")
            self.assertEqual(status, 200)
            self.assertIn("text/html", content_type)
            self.assertIn("Favourites", page)
            self.assertIn("data-fav=", page)

    def test_the_script_is_served_as_javascript(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            status, content_type, body = fetch(f"{base}/app.js")
            self.assertEqual(status, 200)
            self.assertIn("text/javascript", content_type)
            self.assertIn("favorites.json", body)

    def test_search_json_carries_the_domain_order(self) -> None:
        with run_portal(self.root, state_path=self.state_path) as base:
            _status, _ctype, body = fetch(f"{base}/search.json?q=alpha")
            payload = json.loads(body)
            self.assertIn("order", payload)
            self.assertEqual(len(payload["order"]), len(self.registry_keys()))

    def registry_keys(self) -> list[str]:
        """The registry's domain keys, as the server sees them."""
        registry = server.PortalHandler.registry
        assert registry is not None
        return [domain.key for domain in registry.all()]

    def test_a_corrupt_state_file_is_reported_on_the_page_and_the_json(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("---", encoding="utf-8")
        with run_portal(self.root, state_path=self.state_path) as base:
            _status, _ctype, body = fetch(f"{base}/favorites.json")
            self.assertTrue(json.loads(body)["error"])
            _status, _ctype, page = fetch(f"{base}/favorites")
            self.assertIn("could not be read", page)


if __name__ == "__main__":
    unittest.main()
