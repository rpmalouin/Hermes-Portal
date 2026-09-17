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
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes.cli import shell  # noqa: E402
from hermes.core.executor import build_argv, run_skill  # noqa: E402
from hermes.core.loader import load_skills  # noqa: E402
from hermes.core.models import Skill, SkillResult  # noqa: E402
from hermes.core.registry import SkillRegistry  # noqa: E402
from hermes.core.runtime import Runtime, load_profile  # noqa: E402

PACKAGE_DIR = PROJECT_ROOT / "hermes"
SKILLS_DIR = PACKAGE_DIR / "skills"
EXAMPLE_SKILLS_DIR = SKILLS_DIR
DEFAULT_PROFILE = PACKAGE_DIR / "profiles" / "default.json"


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
