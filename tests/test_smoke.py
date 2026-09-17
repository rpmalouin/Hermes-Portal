"""Smoke tests for the Hermes-Dashboard framework (standard library only).

Run from the project root::

    python -m unittest discover -s tests -t .
    python -m unittest tests.test_smoke

Everything here is hermetic: the only subprocesses launched are Python
interpreters, and the only writes go to ``tempfile`` sandboxes.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes.cli import shell  # noqa: E402
from hermes.core.executor import build_argv, run_skill  # noqa: E402
from hermes.core.loader import load_skills  # noqa: E402
from hermes.core.models import Skill, SkillResult  # noqa: E402
from hermes.core.registry import SkillRegistry  # noqa: E402
from hermes.core.runtime import Runtime, load_profile  # noqa: E402
from hermes.web import skill_deck  # noqa: E402

PACKAGE_DIR = PROJECT_ROOT / "hermes"
SKILLS_DIR = PACKAGE_DIR / "skills"
EXAMPLE_SKILLS_DIR = SKILLS_DIR
DEFAULT_PROFILE = PACKAGE_DIR / "profiles" / "default.json"


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


def valid_manifest(name: str, **overrides: object) -> dict:
    """Return a complete manifest dict for *name*, with optional overrides."""
    manifest = {
        "name": name,
        "title": f"{name} (title)",
        "description": "test skill",
        "primary_box": "test",
        "raw_category": "test",
        "tags": [],
        "entrypoint": "main.py",
        "args": {},
        "related": [],
    }
    manifest.update(overrides)
    return manifest


def write_manifest(root: Path, dirname: str, manifest: dict | str) -> Path:
    """Create ``root/dirname/{skill.json,main.py}`` and return the directory."""
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True, exist_ok=True)
    text = manifest if isinstance(manifest, str) else json.dumps(manifest)
    (skill_dir / "skill.json").write_text(text, encoding="utf-8")
    (skill_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    return skill_dir


class TestModels(unittest.TestCase):
    """Skill.from_dict validation and SkillResult.ok."""

    def test_from_dict_round_trips_every_field(self) -> None:
        manifest = valid_manifest("demo", tags=["a"], args={"n": 3}, related=["other"])
        skill = Skill.from_dict(manifest)
        self.assertEqual(skill.name, "demo")
        self.assertEqual(skill.primary_box, "test")
        self.assertEqual(skill.tags, ["a"])
        self.assertEqual(skill.args, {"n": 3})
        self.assertEqual(skill.related, ["other"])
        self.assertEqual(skill.entrypoint, "main.py")

    def test_missing_field_raises_value_error(self) -> None:
        manifest = valid_manifest("demo")
        del manifest["description"]
        with self.assertRaises(ValueError) as caught:
            Skill.from_dict(manifest)
        self.assertIn("missing required field", str(caught.exception))
        self.assertIn("description", str(caught.exception))

    def test_wrong_scalar_type_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as caught:
            Skill.from_dict(valid_manifest("demo", title=42))
        self.assertIn("must be a string", str(caught.exception))

    def test_wrong_list_type_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as caught:
            Skill.from_dict(valid_manifest("demo", tags="not-a-list"))
        self.assertIn("list of strings", str(caught.exception))

    def test_args_rejects_unsupported_value(self) -> None:
        with self.assertRaises(ValueError) as caught:
            Skill.from_dict(valid_manifest("demo", args={"nested": {"a": 1}}))
        self.assertIn("args['nested']", str(caught.exception))

    def test_args_accepts_scalars_and_lists(self) -> None:
        skill = Skill.from_dict(
            valid_manifest("demo", args={"s": "x", "i": 1, "b": True, "l": ["a", 2]})
        )
        self.assertEqual(skill.args["l"], ["a", 2])

    def test_non_object_manifest_raises(self) -> None:
        with self.assertRaises(ValueError):
            Skill.from_dict(["not", "an", "object"])  # type: ignore[arg-type]

    def test_skill_result_ok(self) -> None:
        self.assertTrue(SkillResult("demo", "out", "", 0).ok)
        self.assertFalse(SkillResult("demo", "", "boom", 3).ok)


class TestLoader(unittest.TestCase):
    """load_skills discovery and its never-raise contract."""

    def test_loads_shipped_example_skill(self) -> None:
        skills = load_skills(SKILLS_DIR)
        names = [skill.name for skill in skills]
        self.assertIn("example_skill", names)
        example = next(skill for skill in skills if skill.name == "example_skill")
        self.assertEqual(example.primary_box, "dev")
        self.assertEqual(example.entrypoint, "main.py")

    def test_missing_directory_returns_empty_with_warning(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            skills = load_skills(PROJECT_ROOT / "does_not_exist")
        self.assertEqual(skills, [])
        self.assertIn("skills directory not found", stderr.getvalue())

    def test_project_root_gets_a_hint_naming_the_fix(self) -> None:
        # Runtime(project_root) requests <root>/skills while the real directory
        # sits one level deeper, inside the package. The warning must name that
        # fix instead of just reporting an empty registry.
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            package_dir = project_root / "pkg"
            package_skills = package_dir / "skills"
            (package_skills / "demo").mkdir(parents=True)
            (package_dir / "__init__.py").write_text("", encoding="utf-8")

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                skills = load_skills(project_root / "skills")

            self.assertEqual(skills, [])
            warning = stderr.getvalue()
            self.assertIn("did you pass a project root?", warning)
            self.assertIn(f"pass {package_dir} instead", warning)
            self.assertIn(f"skills_dir={package_skills}", warning)

    def test_hint_prefers_a_candidate_that_is_a_python_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            (project_root / "aaa_sibling" / "skills").mkdir(parents=True)
            package_dir = project_root / "zzz_package"
            (package_dir / "skills").mkdir(parents=True)
            (package_dir / "__init__.py").write_text("", encoding="utf-8")

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                load_skills(project_root / "skills")

            warning = stderr.getvalue()
            self.assertIn(f"skills_dir={package_dir / 'skills'}", warning)

    def test_warning_without_a_candidate_stays_plain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                load_skills(Path(tmp) / "nothing" / "here")
            self.assertIn("skills directory not found", stderr.getvalue())
            self.assertNotIn("did you pass a project root?", stderr.getvalue())

    def test_bad_skills_are_skipped_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_manifest(root, "bad_json", "{not json")
            write_manifest(root, "missing_field", {"name": "missing_field"})
            write_manifest(root, "wrong_type", valid_manifest("wrong_type", tags=1))
            write_manifest(root, "good_skill", valid_manifest("good_skill"))
            (root / "not_a_skill").mkdir()
            (root / "loose_file.txt").write_text("ignore me", encoding="utf-8")

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                skills = load_skills(root)

            self.assertEqual([skill.name for skill in skills], ["good_skill"])
            warnings = stderr.getvalue()
            self.assertIn("malformed JSON", warnings)
            self.assertIn("missing required field", warnings)
            self.assertIn("list of strings", warnings)

    def test_directory_without_manifest_is_ignored_silently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "empty_dir").mkdir()
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                skills = load_skills(root)
            self.assertEqual(skills, [])
            self.assertEqual(stderr.getvalue(), "")

    def test_name_directory_mismatch_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_manifest(root, "folder_name", valid_manifest("manifest_name"))
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                skills = load_skills(root)
            self.assertEqual([skill.name for skill in skills], ["manifest_name"])
            self.assertIn("does not match directory", stderr.getvalue())


class TestRegistry(unittest.TestCase):
    """SkillRegistry lookups, de-duplication and grouping."""

    def setUp(self) -> None:
        self.skill = Skill.from_dict(valid_manifest("alpha", primary_box="dev"))
        self.other = Skill.from_dict(valid_manifest("beta", primary_box="media"))

    def test_register_list_get_names(self) -> None:
        registry = SkillRegistry()
        registry.register(self.other)
        registry.register(self.skill)
        self.assertEqual(registry.names(), ["alpha", "beta"])
        self.assertEqual([s.name for s in registry.list()], ["alpha", "beta"])
        self.assertIs(registry.get("alpha"), self.skill)
        self.assertEqual(len(registry), 2)
        self.assertIn("beta", registry)

    def test_duplicate_name_raises(self) -> None:
        registry = SkillRegistry()
        registry.register(self.skill)
        with self.assertRaises(ValueError) as caught:
            registry.register(Skill.from_dict(valid_manifest("alpha")))
        self.assertIn("duplicate skill name", str(caught.exception))

    def test_get_unknown_raises_key_error(self) -> None:
        registry = SkillRegistry()
        with self.assertRaises(KeyError) as caught:
            registry.get("nope")
        self.assertIn("unknown skill", str(caught.exception))

    def test_by_box(self) -> None:
        registry = SkillRegistry()
        registry.register(self.skill)
        registry.register(self.other)
        self.assertEqual([s.name for s in registry.by_box("dev")], ["alpha"])
        self.assertEqual(registry.by_box("nothing"), [])


class TestExecutor(unittest.TestCase):
    """argv construction and skill execution."""

    def setUp(self) -> None:
        self.skill = Skill.from_dict(valid_manifest("example_skill"))
        self.path = Path("/tmp/main.py")

    def test_build_argv_renders_flag_shapes(self) -> None:
        skill = Skill.from_dict(
            valid_manifest(
                "shapes",
                args={
                    "text": "hello",
                    "count": 3,
                    "verbose": True,
                    "quiet": False,
                    "tags": ["a", "b"],
                },
            )
        )
        argv = build_argv(skill, self.path)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], str(self.path))
        self.assertEqual(
            argv[2:],
            [
                "--text",
                "hello",
                "--count",
                "3",
                "--verbose",
                "--tags",
                "a",
                "--tags",
                "b",
            ],
        )

    def test_build_argv_overrides_defaults(self) -> None:
        skill = Skill.from_dict(valid_manifest("shapes", args={"text": "default"}))
        argv = build_argv(skill, self.path, {"text": "override", "extra": "1"})
        self.assertEqual(argv[2:], ["--text", "override", "--extra", "1"])

    def test_run_example_skill_with_input_hello(self) -> None:
        registry = SkillRegistry()
        registry.register(self.skill)
        resolved = registry.get("example_skill")
        result = run_skill(resolved, EXAMPLE_SKILLS_DIR, input="hello")
        self.assertEqual(result.returncode, 0)
        self.assertIn("HELLO", result.stdout)
        self.assertEqual(result.skill_name, "example_skill")
        self.assertTrue(result.ok)

    def test_run_uses_manifest_default_when_no_kwargs(self) -> None:
        # Use the shipped manifest, not the test fixture: only it carries
        # args={"input": "hello world"}, so this proves the manifest default
        # really reaches the child process.
        shipped = next(
            skill
            for skill in load_skills(EXAMPLE_SKILLS_DIR)
            if skill.name == "example_skill"
        )
        self.assertEqual(shipped.args, {"input": "hello world"})
        result = run_skill(shipped, EXAMPLE_SKILLS_DIR, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertIn("HELLO WORLD", result.stdout)

    def test_missing_entrypoint_returns_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_skill(self.skill, Path(tmp))
            self.assertEqual(result.returncode, 127)
            self.assertIn("entrypoint not found", result.stderr)

    def test_timeout_is_reported_as_minus_one(self) -> None:
        sleeper = Skill.from_dict(
            valid_manifest("slow_skill", entrypoint="main.py", args={})
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "slow_skill"
            skill_dir.mkdir()
            (skill_dir / "main.py").write_text(
                "import time\nprint('started', flush=True)\ntime.sleep(20)\n",
                encoding="utf-8",
            )
            result = run_skill(sleeper, root, timeout=1)
            self.assertEqual(result.returncode, -1)
            self.assertEqual(result.stderr, "timeout")

    def test_failing_skill_reports_its_own_returncode(self) -> None:
        failing = Skill.from_dict(valid_manifest("failing_skill"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "failing_skill"
            skill_dir.mkdir()
            (skill_dir / "main.py").write_text(
                "import sys\nprint('to stderr', file=sys.stderr)\nsys.exit(3)\n",
                encoding="utf-8",
            )
            result = run_skill(failing, root)
            self.assertEqual(result.returncode, 3)
            self.assertIn("to stderr", result.stderr)
            self.assertFalse(result.ok)


class TestRuntime(unittest.TestCase):
    """Runtime loading, dispatch and profile support."""

    def test_runtime_loads_package_skills(self) -> None:
        runtime = Runtime(PACKAGE_DIR)
        self.assertIn("example_skill", runtime.skills.names())
        self.assertEqual(runtime.skills_dir, PACKAGE_DIR / "skills")
        self.assertEqual(runtime.timeout, 60)
        self.assertEqual(runtime.root, PACKAGE_DIR)

    def test_runtime_run_dispatches(self) -> None:
        runtime = Runtime(PACKAGE_DIR)
        result = runtime.run("example_skill", input="hello")
        self.assertEqual(result.returncode, 0)
        self.assertIn("HELLO", result.stdout)

    def test_runtime_unknown_skill_raises_key_error(self) -> None:
        runtime = Runtime(PACKAGE_DIR)
        with self.assertRaises(KeyError):
            runtime.run("no_such_skill")

    def test_duplicate_skill_name_warns_but_keeps_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills_dir = root / "skills"
            write_manifest(skills_dir, "dupe_one", valid_manifest("dupe"))
            write_manifest(skills_dir, "dupe_two", valid_manifest("dupe"))
            # Directory names differ from the manifest name, so silence that
            # warning too and assert only on the duplicate message.
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                runtime = Runtime(root)
            self.assertEqual(runtime.skills.names(), ["dupe"])
            self.assertIn("duplicate skill name", stderr.getvalue())

    def test_load_profile_reads_shipped_default(self) -> None:
        profile = load_profile(DEFAULT_PROFILE)
        self.assertEqual(profile["name"], "default")
        self.assertEqual(profile["skills_dir"], "skills")
        self.assertEqual(profile["timeout"], 60)

    def test_load_profile_rejects_bad_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(
                json.dumps({"name": "x", "skills_dir": "skills", "timeout": "soon"}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_profile(bad)

    def test_from_profile_resolves_relative_to_package_dir(self) -> None:
        runtime = Runtime.from_profile(DEFAULT_PROFILE)
        self.assertEqual(runtime.skills_dir, PACKAGE_DIR / "skills")
        self.assertEqual(runtime.root, PACKAGE_DIR)
        self.assertIn("example_skill", runtime.skills.names())


class TestShell(unittest.TestCase):
    """REPL command handling, without touching the real terminal."""

    def setUp(self) -> None:
        self.runtime = Runtime(PACKAGE_DIR)

    def test_parse_flags_shapes(self) -> None:
        self.assertEqual(shell.parse_flags([]), {})
        self.assertEqual(shell.parse_flags(["--input", "hello"]), {"input": "hello"})
        self.assertEqual(shell.parse_flags(["--verbose"]), {"verbose": True})
        self.assertEqual(
            shell.parse_flags(["--tag", "a", "--tag", "b"]), {"tag": ["a", "b"]}
        )
        self.assertEqual(shell.parse_flags(["--input=-5"]), {"input": "-5"})

    def test_parse_flags_rejects_bare_word(self) -> None:
        with self.assertRaises(ValueError):
            shell.parse_flags(["hello"])

    def test_skills_command_lists_name_box_title(self) -> None:
        out = io.StringIO()
        self.assertTrue(shell.execute_command(self.runtime, "skills", out))
        listing = out.getvalue()
        self.assertIn("NAME", listing)
        self.assertIn("example_skill", listing)
        self.assertIn("dev", listing)
        self.assertIn("Example Skill", listing)

    def test_run_command_prints_stdout_and_returncode(self) -> None:
        out = io.StringIO()
        shell.execute_command(self.runtime, 'run example_skill --input "hello"', out)
        printed = out.getvalue()
        self.assertIn("HELLO", printed)
        self.assertIn("return code: 0", printed)

    def test_run_command_reports_failure_stream(self) -> None:
        out = io.StringIO()
        shell.execute_command(self.runtime, "run nonexistent_skill --input hello", out)
        self.assertIn("unknown skill", out.getvalue())

    def test_help_and_empty_and_exit(self) -> None:
        out = io.StringIO()
        self.assertTrue(shell.execute_command(self.runtime, "help", out))
        self.assertIn("Commands", out.getvalue())
        self.assertTrue(shell.execute_command(self.runtime, "   ", out))
        self.assertTrue(shell.execute_command(self.runtime, "wat", out))
        self.assertIn("unknown command", out.getvalue())
        self.assertFalse(shell.execute_command(self.runtime, "exit", out))
        self.assertFalse(shell.execute_command(self.runtime, "quit", out))

    def test_bad_quoting_is_reported_not_raised(self) -> None:
        out = io.StringIO()
        shell.execute_command(self.runtime, 'run example_skill --input "oops', out)
        self.assertIn("cannot parse input", out.getvalue())

    def test_run_without_skill_name_shows_usage(self) -> None:
        out = io.StringIO()
        shell.execute_command(self.runtime, "run", out)
        self.assertIn("usage: run", out.getvalue())

    def test_run_shell_loop_stops_at_eof(self) -> None:
        out = io.StringIO()
        stdin = io.StringIO("skills\nrun example_skill --input hi\nexit\n")
        self.assertEqual(shell.run_shell(self.runtime, stdin, out), 0)
        printed = out.getvalue()
        self.assertIn(shell.PROMPT, printed)
        self.assertIn("HI", printed)


class TestDeckFrontmatter(unittest.TestCase):
    """SKILL.md frontmatter parsing: a documented subset, never a crash."""

    def test_simple_scalars(self) -> None:
        fields, body = skill_deck.split_frontmatter(
            "---\nname: ascii-art\ndescription: Draw with characters.\n---\n\n# Title\n"
        )
        self.assertEqual(fields["name"], "ascii-art")
        self.assertEqual(fields["description"], "Draw with characters.")
        self.assertIn("# Title", body)

    def test_quoted_values_are_unquoted(self) -> None:
        fields = skill_deck.parse_frontmatter(
            "---\nname: \"ask-matt\"\nlicense: 'MIT'\n---\n"
        )
        self.assertEqual(fields["name"], "ask-matt")
        self.assertEqual(fields["license"], "MIT")

    def test_nested_blocks_and_comments_are_ignored(self) -> None:
        fields = skill_deck.parse_frontmatter(
            "---\nname: demo\ndescription: d\nmetadata:\n  hermes:\n"
            "    tags: [a, b]\n# comment\nversion: 1.0.0\n---\nbody\n"
        )
        self.assertEqual(fields["name"], "demo")
        self.assertEqual(fields["version"], "1.0.0")
        self.assertNotIn("hermes", fields)
        self.assertNotIn("tags", fields)

    def test_no_frontmatter_returns_empty_fields(self) -> None:
        fields, body = skill_deck.split_frontmatter("# Just a heading\n")
        self.assertEqual(fields, {})
        self.assertEqual(body, "# Just a heading\n")

    def test_unterminated_fence_is_not_frontmatter(self) -> None:
        self.assertEqual(skill_deck.parse_frontmatter("---\nname: demo\n"), {})


class TestDeckDiscovery(unittest.TestCase):
    """Card building, the symlink-following walk, and de-duplication."""

    def test_card_uses_heading_as_title_and_path_for_box(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            cards = skill_deck.discover_skills(home / "skills", "test")
            by_name = {card.name: card for card in cards}
            self.assertEqual(
                sorted(by_name),
                ["ascii-art", "linked", "standalone"],
            )
            nested = by_name["ascii-art"]
            self.assertEqual(nested.box, "creative")
            self.assertEqual(nested.category, "creative/ascii-art")
            self.assertEqual(nested.title, "Ascii Art")
            self.assertEqual(nested.origin, skill_deck.HERMES_ORIGIN)
            self.assertTrue(nested.path.endswith("SKILL.md"))

    def test_symlinked_skill_directory_is_followed(self) -> None:
        # Path.rglob would miss this one entirely.
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            names = [
                c.name for c in skill_deck.discover_skills(home / "skills", "test")
            ]
            self.assertIn("linked", names)

    def test_nested_skill_inside_a_skill_is_not_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            names = [
                c.name for c in skill_deck.discover_skills(home / "skills", "test")
            ]
            self.assertNotIn("bogus", names)

    def test_missing_root_is_empty_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(skill_deck.discover_skills(Path(tmp) / "nope", "test"), [])

    def test_card_falls_back_to_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill_dir(root, "no-frontmatter", title=None, with_frontmatter=False)
            cards = skill_deck.discover_skills(root, "test")
            self.assertEqual([card.name for card in cards], ["no-frontmatter"])
            self.assertEqual(cards[0].description, "(no description)")
            self.assertEqual(cards[0].title, "No Frontmatter")

    def test_duplicates_by_name_and_path_are_dropped(self) -> None:
        def card(name: str, origin: str, path: str, source: str) -> skill_deck.Card:
            return skill_deck.Card(
                name, name.upper(), "d", "box", "box", origin, source, path
            )

        first = card("a", "hermes", "/x/SKILL.md", "s1")
        same_name = card("a", "hermes", "/y/SKILL.md", "s2")
        same_path = card("b", "hermes", "/x/SKILL.md", "s1")
        other_origin = card("a", "framework", "/z", "s")
        kept, dropped = skill_deck._dedupe([first, same_name, same_path, other_origin])

        # same_name is dropped by name, same_path by path; the framework
        # card shares the name but a different origin, so it survives.
        self.assertEqual([card.name for card in kept], ["a", "a"])
        self.assertEqual(kept[1].origin, "framework")
        self.assertEqual(dropped, 2)


class TestDeckRoots(unittest.TestCase):
    """Which Hermes skill directories get read, and why."""

    def test_default_home_follows_hermes_home_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                self.assertEqual(skill_deck.default_hermes_home(), home)
                roots = skill_deck.resolve_hermes_roots()
            self.assertEqual([r.path for r in roots], [home / "skills"])

    def test_all_profiles_finds_siblings_when_home_is_a_profile(self) -> None:
        # $HERMES_HOME points at a profile directory in a live session.
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            profile_dir = home / "profiles" / "other"
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(profile_dir)}):
                roots = skill_deck.resolve_hermes_roots(all_profiles=True)
            self.assertEqual([r.path for r in roots], [profile_dir / "skills"])

    def test_all_profiles_skips_hidden_and_keeps_priority_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            roots = skill_deck.resolve_hermes_roots(home, all_profiles=True)
            self.assertEqual(
                [r.path for r in roots],
                [home / "skills", home / "profiles" / "other" / "skills"],
            )
            self.assertTrue(all(".deleted" not in str(r.path) for r in roots))

    def test_named_profile_reads_that_profile_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            roots = skill_deck.resolve_hermes_roots(
                home, profile="other", all_profiles=True
            )
            self.assertEqual(
                [r.path for r in roots], [home / "profiles" / "other" / "skills"]
            )


class TestDeckBuildAndRender(unittest.TestCase):
    """build_deck, HTML escaping and the JSON payload."""

    def test_build_deck_counts_and_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            data = skill_deck.build_deck(
                PROJECT_ROOT,
                hermes_home=home,
                all_profiles=True,
                include_framework=False,
            )
            self.assertEqual(len(data.cards), 4)
            self.assertEqual(data.dropped, 0)
            labels = [status.label for status in data.sources]
            self.assertEqual(labels, ["running hermes", "profile other"])
            self.assertTrue(all(status.present for status in data.sources))

    def test_build_deck_reports_a_missing_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = skill_deck.build_deck(
                PROJECT_ROOT,
                hermes_home=Path(tmp) / "absent",
                include_framework=False,
            )
            self.assertEqual(data.cards, [])
            self.assertFalse(data.sources[0].present)
            self.assertIn("[MISSING]", skill_deck.describe(data))

    def test_unreadable_skill_file_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / "broken"
            bad.mkdir()
            (bad / "SKILL.md").symlink_to(root / "gone" / "SKILL.md")
            good = write_skill_dir(root, "good", title="Good")
            cards = skill_deck.discover_skills(root, "test")
            self.assertEqual([card.name for card in cards], ["good"])
            self.assertTrue(good.is_dir())

    def test_render_escapes_skill_text(self) -> None:
        card = skill_deck.Card(
            name="evil",
            title="<script>alert(1)</script>",
            description="uses <html> & `--flags` **bold**",
            box="dev",
            category="dev",
            origin="hermes",
            source="running hermes",
            path="/tmp/SKILL.md",
        )
        page = skill_deck.render_page(skill_deck.DeckData(cards=[card], sources=[]))
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&amp;", page)
        self.assertIn("<code>--flags</code>", page)
        self.assertIn("<strong>bold</strong>", page)

    def test_render_reports_counts_origins_and_boxes(self) -> None:
        cards = [
            skill_deck.Card(
                "a", "A", "d", "box-one", "box-one", "framework", "s", "/a"
            ),
            skill_deck.Card("b", "B", "d", "box-two", "box-two", "hermes", "s", "/b"),
            skill_deck.Card("c", "C", "d", "box-two", "box-two", "hermes", "s", "/c"),
        ]
        sources = [skill_deck.SourceStatus("running hermes", Path("/nowhere"))]
        page = skill_deck.render_page(
            skill_deck.DeckData(cards=cards, sources=sources, dropped=7)
        )
        self.assertIn("<strong>3</strong> skills", page)
        self.assertIn("1 framework, 2 hermes", page)
        self.assertIn("<strong>2</strong> boxes", page)
        self.assertIn("7 duplicate card(s) skipped", page)
        self.assertIn('class="missing"', page)
        self.assertIn("0/1 present", page)
        self.assertIn('class="card framework"', page)
        self.assertIn('class="card hermes"', page)

    def test_render_empty_deck_says_so(self) -> None:
        page = skill_deck.render_page(skill_deck.DeckData())
        self.assertIn("No skills found", page)

    def test_json_payload_shape(self) -> None:
        card = skill_deck.Card("a", "A", "d", "box", "box", "hermes", "s", "/a")
        payload = skill_deck.json_payload(
            skill_deck.DeckData(
                cards=[card],
                sources=[
                    skill_deck.SourceStatus("running hermes", Path("/nowhere"), 1)
                ],
                dropped=2,
            )
        )
        self.assertEqual(payload["counts"]["total"], 1)
        self.assertEqual(payload["counts"]["duplicates_dropped"], 2)
        self.assertFalse(payload["sources"][0]["present"])
        self.assertEqual(payload["cards"][0]["name"], "a")

    def test_describe_mentions_every_source(self) -> None:
        data = skill_deck.build_deck(
            PROJECT_ROOT,
            hermes_home=Path("/definitely/absent"),
            include_framework=False,
        )
        self.assertIn("0 skills", skill_deck.describe(data))
        self.assertIn("/definitely/absent", skill_deck.describe(data))


class TestDeckServer(unittest.TestCase):
    """The HTTP surface, exercised for real over a socket."""

    def setUp(self) -> None:
        self._saved = skill_deck.DeckHandler.data

    def tearDown(self) -> None:
        skill_deck.DeckHandler.data = self._saved

    def test_serves_html_json_and_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            data = skill_deck.build_deck(
                PROJECT_ROOT,
                hermes_home=home,
                all_profiles=True,
                include_framework=False,
            )
            skill_deck.DeckHandler.data = data

            server = ThreadingHTTPServer(("127.0.0.1", 0), skill_deck.DeckHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base}/", timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("text/html", response.headers.get("Content-Type", ""))
                    page = response.read().decode("utf-8")
                with urllib.request.urlopen(f"{base}/skills.json", timeout=10) as res:
                    payload = json.loads(res.read().decode("utf-8"))
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"{base}/nope", timeout=10)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertIn("Hermes Skill Deck", page)
        self.assertIn("standalone", page)
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(payload["counts"]["total"], len(data.cards))
        self.assertEqual(payload["counts"]["hermes"], 4)
        self.assertEqual(
            sorted(card["name"] for card in payload["cards"]),
            ["ascii-art", "beta", "linked", "standalone"],
        )


class TestDeckBoxFilter(unittest.TestCase):
    """The --box filter: box names, category paths, repeats, and reporting."""

    @staticmethod
    def card(name: str, box: str, category: str | None = None) -> skill_deck.Card:
        return skill_deck.Card(
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
        kept, hidden = skill_deck.filter_cards(self.cards, [])
        self.assertEqual(len(kept), 5)
        self.assertEqual(hidden, 0)

    def test_blank_values_are_ignored(self) -> None:
        kept, hidden = skill_deck.filter_cards(self.cards, ["", "   "])
        self.assertEqual(len(kept), 5)
        self.assertEqual(hidden, 0)

    def test_box_name_matches_case_insensitively(self) -> None:
        kept, hidden = skill_deck.filter_cards(self.cards, ["CREATIVE"])
        self.assertEqual([card.name for card in kept], ["ascii-art", "p5js"])
        self.assertEqual(hidden, 3)

    def test_category_path_narrows_within_a_box(self) -> None:
        kept, _ = skill_deck.filter_cards(self.cards, ["mlops/inference"])
        self.assertEqual([card.name for card in kept], ["vllm"])

    def test_category_path_selects_only_its_own_branch(self) -> None:
        kept, _ = skill_deck.filter_cards(self.cards, ["mlops/evaluation"])
        self.assertEqual([card.name for card in kept], ["harness"])
        self.assertEqual(
            skill_deck.filter_cards(self.cards, ["mlops/evaluation/deeper"])[0], []
        )

    def test_repeated_boxes_are_a_union(self) -> None:
        kept, hidden = skill_deck.filter_cards(self.cards, ["creative", "dev"])
        self.assertEqual(
            [card.name for card in kept], ["ascii-art", "p5js", "example_skill"]
        )
        self.assertEqual(hidden, 2)

    def test_no_match_hides_everything(self) -> None:
        kept, hidden = skill_deck.filter_cards(self.cards, ["nope"])
        self.assertEqual(kept, [])
        self.assertEqual(hidden, 5)

    def test_build_deck_filters_but_sources_still_report_full_scans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            data = skill_deck.build_deck(
                PROJECT_ROOT,
                hermes_home=home,
                all_profiles=True,
                include_framework=False,
                boxes=["creative"],
            )
            self.assertEqual([card.name for card in data.cards], ["ascii-art"])
            self.assertEqual(data.hidden, 3)
            self.assertEqual(data.filters, ("creative",))
            self.assertEqual([status.found for status in data.sources], [3, 1])

    def test_render_shows_the_active_filter(self) -> None:
        page = skill_deck.render_page(
            skill_deck.DeckData(
                cards=[self.card("ascii-art", "creative")],
                sources=[],
                filters=("creative",),
                hidden=7,
            )
        )
        self.assertIn("in box creative", page)
        self.assertIn("(7 hidden)", page)

    def test_render_pluralises_multiple_boxes(self) -> None:
        page = skill_deck.render_page(
            skill_deck.DeckData(
                cards=[self.card("ascii-art", "creative")],
                sources=[],
                filters=("creative", "apple"),
                hidden=3,
            )
        )
        self.assertIn("in boxes creative, apple", page)

    def test_render_explains_an_empty_filtered_deck(self) -> None:
        page = skill_deck.render_page(skill_deck.DeckData(filters=("nope",), hidden=5))
        self.assertIn("No skills in --box nope", page)
        self.assertNotIn("or point --hermes-home", page)

    def test_render_escapes_the_filter_value(self) -> None:
        page = skill_deck.render_page(
            skill_deck.DeckData(filters=("<script>",), hidden=1)
        )
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_json_reports_filter_state_and_box_breakdown(self) -> None:
        payload = skill_deck.json_payload(
            skill_deck.DeckData(
                cards=[self.card("ascii-art", "creative")],
                sources=[],
                filters=("creative",),
                hidden=4,
            )
        )
        self.assertEqual(payload["counts"]["box_filter"], ["creative"])
        self.assertEqual(payload["counts"]["hidden_by_filter"], 4)
        self.assertEqual(payload["counts"]["by_box"], {"creative": 1})

    def test_describe_reports_filter_hidden_and_boxes(self) -> None:
        data = skill_deck.DeckData(
            cards=[self.card("ascii-art", "creative")],
            sources=[],
            filters=("creative",),
            hidden=9,
        )
        summary = skill_deck.describe(data)
        self.assertIn("filter: --box creative (9 hidden)", summary)
        self.assertIn("   1  creative", summary)

    def test_cli_box_is_repeatable_and_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            out = io.StringIO()
            with redirect_stdout(out):
                code = skill_deck.main(
                    [
                        "--list",
                        "--no-framework",
                        "--all-profiles",
                        "--hermes-home",
                        str(home),
                        "--box",
                        "CREATIVE",
                        "--box",
                        "beta",
                    ]
                )
            printed = out.getvalue()

        self.assertEqual(code, 0)
        self.assertIn("2 skills", printed)
        self.assertIn("filter: --box CREATIVE --box beta (2 hidden)", printed)
        self.assertIn("   1  creative", printed)
        self.assertIn("   1  beta", printed)

    def test_cli_without_box_lists_every_box(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            out = io.StringIO()
            with redirect_stdout(out):
                skill_deck.main(
                    ["--list", "--no-framework", "--hermes-home", str(home)]
                )
            printed = out.getvalue()

        self.assertIn("3 skills", printed)
        self.assertIn("boxes:", printed)
        for box in ("standalone", "creative", "linked-skill"):
            self.assertIn(box, printed)
        self.assertNotIn("filter:", printed)

    def test_cli_box_with_no_matches_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_hermes_home(Path(tmp))
            out = io.StringIO()
            with redirect_stdout(out):
                skill_deck.main(
                    [
                        "--list",
                        "--no-framework",
                        "--hermes-home",
                        str(home),
                        "--box",
                        "absent",
                    ]
                )
        printed = out.getvalue()
        self.assertIn("0 skills", printed)
        self.assertIn("filter: --box absent (3 hidden)", printed)


@contextmanager
def run_deck_server(data: skill_deck.DeckData, default_boxes: tuple[str, ...] = ()):
    """Yield the base URL of a deck server on an ephemeral port.

    Restores the handler's class-level state on exit, so tests cannot leak a
    deck or a default filter into each other.
    """
    saved = (skill_deck.DeckHandler.data, skill_deck.DeckHandler.default_boxes)
    skill_deck.DeckHandler.data = data
    skill_deck.DeckHandler.default_boxes = default_boxes
    server = ThreadingHTTPServer(("127.0.0.1", 0), skill_deck.DeckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        skill_deck.DeckHandler.data, skill_deck.DeckHandler.default_boxes = saved


def fetch(url: str) -> str:
    """GET *url* and return the decoded body."""
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


class TestDeckBoxPicker(unittest.TestCase):
    """The box dropdown and the ?box= query that backs it."""

    def cards(self) -> list[skill_deck.Card]:
        return [
            skill_deck.Card(
                "ascii-art",
                "A",
                "d",
                "creative",
                "creative/ascii-art",
                "hermes",
                "s",
                "/a",
            ),
            skill_deck.Card(
                "p5js", "P", "d", "creative", "creative/p5js", "hermes", "s", "/b"
            ),
            skill_deck.Card("beta", "B", "d", "beta", "beta", "hermes", "s", "/c"),
        ]

    def options(self, page: str) -> list[tuple[str, str, str]]:
        import re

        return re.findall(r'<option value="([^"]*)"( selected)?>([^<]+)</option>', page)

    def test_picker_is_a_get_form_that_submits_on_change(self) -> None:
        page = skill_deck.render_page(skill_deck.DeckData(cards=self.cards()))
        self.assertIn('<form class="picker" method="get" action="/"', page)
        self.assertIn(
            '<select id="box" name="box" onchange="this.form.submit()">', page
        )
        self.assertIn("<noscript>", page)

    def test_picker_lists_every_box_with_counts(self) -> None:
        page = skill_deck.render_page(skill_deck.DeckData(cards=self.cards()))
        options = self.options(page)
        self.assertEqual([value for value, _, _ in options], ["", "beta", "creative"])
        labels = {value: label for value, _, label in options}
        self.assertEqual(labels["creative"], "creative (2)")
        self.assertEqual(labels["beta"], "beta (1)")
        self.assertEqual(labels[""], "All boxes (3)")

    def test_all_boxes_is_selected_without_a_filter(self) -> None:
        page = skill_deck.render_page(skill_deck.DeckData(cards=self.cards()))
        self.assertIn('<option value="" selected>All boxes (3)</option>', page)

    def test_active_box_is_selected(self) -> None:
        page = skill_deck.render_page(
            skill_deck.DeckData(cards=self.cards(), filters=("creative",), hidden=1)
        )
        self.assertIn('value="creative" selected', page)
        self.assertNotIn('<option value="" selected>', page)

    def test_several_active_boxes_are_stated_not_faked(self) -> None:
        page = skill_deck.render_page(
            skill_deck.DeckData(
                cards=self.cards(), filters=("creative", "beta"), hidden=0
            )
        )
        self.assertIn('value="" disabled selected>boxes: creative, beta', page)
        self.assertNotIn('value="creative" selected', page)

    def test_picker_offers_every_box_even_when_filtered(self) -> None:
        data = skill_deck.DeckData(
            cards=self.cards(), inventory={"creative": 2, "beta": 1}
        )
        filtered = skill_deck.apply_box_filter(data, ["creative"])
        self.assertEqual(len(filtered.cards), 2)
        self.assertEqual(filtered.hidden, 1)
        self.assertEqual(filtered.inventory, {"creative": 2, "beta": 1})
        page = skill_deck.render_page(filtered)
        values = [value for value, _, _ in self.options(page)]
        self.assertEqual(values, ["", "beta", "creative"])
        self.assertIn("All boxes (3)", page)

    def test_apply_box_filter_leaves_the_original_alone(self) -> None:
        data = skill_deck.DeckData(cards=self.cards(), inventory={"creative": 2})
        skill_deck.apply_box_filter(data, ["beta"])
        self.assertEqual(len(data.cards), 3)
        self.assertEqual(data.filters, ())
        self.assertEqual(data.hidden, 0)

    def test_picker_escapes_box_names(self) -> None:
        page = skill_deck.render_page(
            skill_deck.DeckData(cards=[], inventory={"<script>x</script>": 2})
        )
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", page)

    def test_json_advertises_the_full_inventory(self) -> None:
        data = skill_deck.DeckData(
            cards=self.cards()[:1], inventory={"creative": 2, "beta": 1}
        )
        payload = skill_deck.json_payload(
            skill_deck.apply_box_filter(data, ["creative"])
        )
        self.assertEqual(payload["counts"]["inventory"], {"creative": 2, "beta": 1})
        self.assertEqual(payload["counts"]["by_box"], {"creative": 1})

    # --- over HTTP ---------------------------------------------------------

    def fake_home_deck(self, tmp: str) -> skill_deck.DeckData:
        return skill_deck.build_deck(
            PROJECT_ROOT,
            hermes_home=make_hermes_home(Path(tmp)),
            all_profiles=True,
            include_framework=False,
        )

    def test_query_box_filters_the_page(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_deck_server(self.fake_home_deck(tmp)) as base,
        ):
            page = fetch(f"{base}/?box=creative")
        self.assertEqual(page.count('class="card '), 1)
        self.assertIn("in box creative (3 hidden)", page)
        self.assertIn('value="creative" selected', page)
        self.assertIn("All boxes (4)", page)

    def test_query_box_repeats_and_hits_the_json_endpoint(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_deck_server(self.fake_home_deck(tmp)) as base,
        ):
            payload = json.loads(fetch(f"{base}/skills.json?box=creative&box=beta"))
        counts = payload["counts"]
        self.assertEqual(counts["box_filter"], ["creative", "beta"])
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["hidden_by_filter"], 2)
        self.assertEqual(counts["by_box"], {"beta": 1, "creative": 1})
        self.assertEqual(
            counts["inventory"],
            {"beta": 1, "creative": 1, "linked-skill": 1, "standalone": 1},
        )

    def test_empty_box_param_means_no_filter(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_deck_server(self.fake_home_deck(tmp)) as base,
        ):
            payload = json.loads(fetch(f"{base}/skills.json?box="))
        self.assertEqual(payload["counts"]["total"], 4)
        self.assertEqual(payload["counts"]["box_filter"], [])

    def test_cli_default_applies_until_the_url_overrides_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = self.fake_home_deck(tmp)
            with run_deck_server(data, default_boxes=("creative",)) as base:
                default = json.loads(fetch(f"{base}/skills.json"))
                cleared = json.loads(fetch(f"{base}/skills.json?box="))
                switched = json.loads(fetch(f"{base}/skills.json?box=beta"))
        self.assertEqual(default["counts"]["total"], 1)
        self.assertEqual(default["counts"]["box_filter"], ["creative"])
        self.assertEqual(cleared["counts"]["total"], 4)
        self.assertEqual(switched["counts"]["box_filter"], ["beta"])

    def test_unknown_path_still_404s_with_a_query(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_deck_server(self.fake_home_deck(tmp)) as base,
            self.assertRaises(urllib.error.HTTPError) as caught,
        ):
            fetch(f"{base}/nope?box=creative")
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
