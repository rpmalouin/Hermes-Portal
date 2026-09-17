"""Tests for :mod:`hermes.core.skill_trees`, the shared skill data layer.

This is the view-free half that used to live in the Skill Deck module: walking the
skill roots, reading ``SKILL.md`` frontmatter, de-duplicating and filtering.  The
page that consumed it (the deck) has been retired into the portal's skills gallery,
so these tests follow the logic rather than the page, and use a fake Hermes tree of
their own rather than reading the real one.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes.core import skill_trees  # noqa: E402


def write_skill_dir(
    parent: Path,
    name: str,
    *,
    title: str | None = "Test Skill",
    description: str = "A test skill.",
    frontmatter_name: str | None = None,
    with_frontmatter: bool = True,
) -> Path:
    """Write a SKILL.md skill directory under *parent* and return it.

    ``title=None`` omits the ``# heading`` so the name-fallback path can be
    exercised.
    """
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest_name = frontmatter_name if frontmatter_name is not None else name
    body = f"# {title}\n\nBody text.\n" if title else "Body text.\n"
    text = (
        f"---\nname: {manifest_name}\ndescription: {description}\n---\n\n{body}"
        if with_frontmatter
        else body
    )
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


def make_hermes_home(root: Path) -> Path:
    """Build a fake Hermes home with the shapes that matter and return it.

    Reproduces the three traps the real tree has: a top-level skill, a nested
    category skill, a skill reachable only through a directory symlink, a
    second profile, a hidden profile directory, and a SKILL.md nested *inside*
    a skill directory (which must not register as a skill).
    """
    home = root / "hermes-home"
    write_skill_dir(home / "skills", "standalone", title="Standalone")
    write_skill_dir(home / "skills" / "creative", "ascii-art", title="Ascii Art")
    write_skill_dir(home / "profiles" / "other" / "skills", "beta", title="Beta")
    (home / "profiles" / ".deleted" / "skills").mkdir(parents=True, exist_ok=True)

    outside = root / "outside"
    write_skill_dir(outside, "linked", title="Linked")
    (home / "skills").mkdir(parents=True, exist_ok=True)
    (home / "skills" / "linked-skill").symlink_to(
        outside / "linked", target_is_directory=True
    )

    nested = home / "skills" / "standalone" / "references"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "SKILL.md").write_text("---\nname: bogus\n---\n", encoding="utf-8")
    return home


class TestFrontmatter(unittest.TestCase):
    """SKILL.md frontmatter parsing: a documented subset, never a crash."""

    def test_simple_scalars(self) -> None:
        fields, body = skill_trees.split_frontmatter(
            "---\nname: ascii-art\ndescription: Draw with characters.\n---\n\n# Title\n"
        )
        self.assertEqual(fields["name"], "ascii-art")
        self.assertEqual(fields["description"], "Draw with characters.")
        self.assertIn("# Title", body)

    def test_quoted_values_are_unquoted(self) -> None:
        fields = skill_trees.parse_frontmatter(
            "---\nname: \"ask-matt\"\nlicense: 'MIT'\n---\n"
        )
        self.assertEqual(fields["name"], "ask-matt")
        self.assertEqual(fields["license"], "MIT")

    def test_nested_blocks_and_comments_are_ignored(self) -> None:
        fields = skill_trees.parse_frontmatter(
            "---\nname: demo\ndescription: d\nmetadata:\n  hermes:\n"
            "    tags: [a, b]\n# comment\nversion: 1.0.0\n---\nbody\n"
        )
        self.assertEqual(fields["name"], "demo")
        self.assertEqual(fields["version"], "1.0.0")
        self.assertNotIn("hermes", fields)
        self.assertNotIn("tags", fields)

    def test_no_frontmatter_returns_empty_fields(self) -> None:
        fields, body = skill_trees.split_frontmatter("# Just a heading\n")
        self.assertEqual(fields, {})
        self.assertEqual(body, "# Just a heading\n")

    def test_unterminated_fence_is_not_frontmatter(self) -> None:
        self.assertEqual(skill_trees.parse_frontmatter("---\nname: demo\n"), {})


class TestSkillDiscovery(unittest.TestCase):
    """Card building, the symlink-following walk, and de-duplication."""

    def test_card_uses_heading_as_title_and_path_for_box(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            cards = skill_trees.discover_skills(home / "skills", "test")
            by_name = {card.name: card for card in cards}
            self.assertEqual(
                sorted(by_name),
                ["ascii-art", "linked", "standalone"],
            )
            nested = by_name["ascii-art"]
            self.assertEqual(nested.box, "creative")
            self.assertEqual(nested.category, "creative/ascii-art")
            self.assertEqual(nested.title, "Ascii Art")
            self.assertEqual(nested.origin, skill_trees.HERMES_ORIGIN)
            self.assertTrue(nested.path.endswith("SKILL.md"))

    def test_symlinked_skill_directory_is_followed(self) -> None:
        # Path.rglob would miss this one entirely.
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            names = [
                c.name for c in skill_trees.discover_skills(home / "skills", "test")
            ]
            self.assertIn("linked", names)

    def test_nested_skill_inside_a_skill_is_not_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            names = [
                c.name for c in skill_trees.discover_skills(home / "skills", "test")
            ]
            self.assertNotIn("bogus", names)

    def test_missing_root_is_empty_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                skill_trees.discover_skills(Path(tmp) / "nope", "test"), []
            )

    def test_card_falls_back_to_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill_dir(root, "no-frontmatter", title=None, with_frontmatter=False)
            cards = skill_trees.discover_skills(root, "test")
            self.assertEqual([card.name for card in cards], ["no-frontmatter"])
            self.assertEqual(cards[0].description, "(no description)")
            self.assertEqual(cards[0].title, "No Frontmatter")

    def test_duplicates_by_name_and_path_are_dropped(self) -> None:
        def card(name: str, origin: str, path: str, source: str) -> skill_trees.Card:
            return skill_trees.Card(
                name, name.upper(), "d", "box", "box", origin, source, path
            )

        first = card("a", "hermes", "/x/SKILL.md", "s1")
        same_name = card("a", "hermes", "/y/SKILL.md", "s2")
        same_path = card("b", "hermes", "/x/SKILL.md", "s1")
        other_origin = card("a", "framework", "/z", "s")
        kept, dropped = skill_trees.dedupe_cards(
            [first, same_name, same_path, other_origin]
        )

        # same_name is dropped by name, same_path by path; the framework
        # card shares the name but a different origin, so it survives.
        self.assertEqual([card.name for card in kept], ["a", "a"])
        self.assertEqual(kept[1].origin, "framework")
        self.assertEqual(dropped, 2)


class TestSkillRoots(unittest.TestCase):
    """Which Hermes skill directories get read, and why."""

    def test_default_home_follows_hermes_home_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                self.assertEqual(skill_trees.default_hermes_home(), home)
                roots = skill_trees.resolve_hermes_roots()
            self.assertEqual([r.path for r in roots], [home / "skills"])

    def test_all_profiles_finds_siblings_when_home_is_a_profile(self) -> None:
        # $HERMES_HOME points at a profile directory in a live session.
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            profile_dir = home / "profiles" / "other"
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(profile_dir)}):
                roots = skill_trees.resolve_hermes_roots(all_profiles=True)
            self.assertEqual([r.path for r in roots], [profile_dir / "skills"])

    def test_all_profiles_skips_hidden_and_keeps_priority_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            roots = skill_trees.resolve_hermes_roots(home, all_profiles=True)
            self.assertEqual(
                [r.path for r in roots],
                [home / "skills", home / "profiles" / "other" / "skills"],
            )
            self.assertTrue(all(".deleted" not in str(r.path) for r in roots))

    def test_named_profile_reads_that_profile_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            roots = skill_trees.resolve_hermes_roots(
                home, profile="other", all_profiles=True
            )
            self.assertEqual(
                [r.path for r in roots], [home / "profiles" / "other" / "skills"]
            )


class TestCardFilter(unittest.TestCase):
    """The --box filter: box names, category paths, repeats, and reporting."""

    @staticmethod
    def card(name: str, box: str, category: str | None = None) -> skill_trees.Card:
        return skill_trees.Card(
            name=name,
            title=name,
            description="d",
            box=box,
            category=category if category is not None else box,
            origin="hermes",
            source="running hermes",
            path=f"/tmp/{name}/SKILL.md",
        )

    def setUp(self) -> None:
        self.cards = [
            self.card("ascii-art", "creative", "creative/ascii-art"),
            self.card("p5js", "creative", "creative/p5js"),
            self.card("harness", "mlops", "mlops/evaluation/evaluating-llms-harness"),
            self.card("vllm", "mlops", "mlops/inference/serving-llms-vllm"),
            self.card("example_skill", "dev", "text"),
        ]

    def test_no_filter_keeps_everything(self) -> None:
        kept, hidden = skill_trees.filter_cards(self.cards, [])
        self.assertEqual(len(kept), 5)
        self.assertEqual(hidden, 0)

    def test_blank_values_are_ignored(self) -> None:
        kept, hidden = skill_trees.filter_cards(self.cards, ["", "   "])
        self.assertEqual(len(kept), 5)
        self.assertEqual(hidden, 0)

    def test_box_name_matches_case_insensitively(self) -> None:
        kept, hidden = skill_trees.filter_cards(self.cards, ["CREATIVE"])
        self.assertEqual([card.name for card in kept], ["ascii-art", "p5js"])
        self.assertEqual(hidden, 3)

    def test_category_path_narrows_within_a_box(self) -> None:
        kept, _ = skill_trees.filter_cards(self.cards, ["mlops/inference"])
        self.assertEqual([card.name for card in kept], ["vllm"])

    def test_category_path_selects_only_its_own_branch(self) -> None:
        kept, _ = skill_trees.filter_cards(self.cards, ["mlops/evaluation"])
        self.assertEqual([card.name for card in kept], ["harness"])
        self.assertEqual(
            skill_trees.filter_cards(self.cards, ["mlops/evaluation/deeper"])[0], []
        )

    def test_repeated_boxes_are_a_union(self) -> None:
        kept, hidden = skill_trees.filter_cards(self.cards, ["creative", "dev"])
        self.assertEqual(
            [card.name for card in kept], ["ascii-art", "p5js", "example_skill"]
        )
        self.assertEqual(hidden, 2)

    def test_no_match_hides_everything(self) -> None:
        kept, hidden = skill_trees.filter_cards(self.cards, ["nope"])
        self.assertEqual(kept, [])
        self.assertEqual(hidden, 5)


class TestUnreadableSkill(unittest.TestCase):
    """A skill file that cannot be read is skipped, not fatal."""

    def test_unreadable_skill_file_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / "broken"
            bad.mkdir()
            (bad / "SKILL.md").symlink_to(root / "gone" / "SKILL.md")
            good = write_skill_dir(root, "good", title="Good")
            cards = skill_trees.discover_skills(root, "test")
            self.assertEqual([card.name for card in cards], ["good"])
            self.assertTrue(good.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
