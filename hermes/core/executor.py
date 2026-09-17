"""Run a skill as a subprocess and report the outcome as data.

Argument rendering (spec section 5): the caller's keyword arguments are merged
over ``skill.args`` defaults, then each pair becomes argv tokens.

===============  ==========================================
value            argv
===============  ==========================================
``str``          ``["--k", value]``
``int``/``float``  ``["--k", str(value)]``
``True``         ``["--k"]``
``False``        omitted
``None``         omitted
``list``         ``["--k", item]`` repeated once per item
===============  ==========================================

Assumptions (spec silent): ``None`` is treated like ``False`` and omits the
flag; list items are rendered with ``str()``; a key already prefixed with ``-``
is used verbatim, otherwise ``--`` is prepended.

Failures are returned, not raised, so the runtime can keep going: ``-1`` for a
timeout (as specified), ``127`` for a missing entrypoint and ``126`` when the
process cannot be launched at all (both mirroring shell conventions).  Flag
names are never passed through a shell: argv is always a list and ``shell=True``
is never used.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from .models import Skill, SkillResult

TIMEOUT_RETURNCODE: Final[int] = -1
LAUNCH_FAILURE_RETURNCODE: Final[int] = 126
MISSING_ENTRYPOINT_RETURNCODE: Final[int] = 127


def _flag(name: str) -> str:
    """Render an argument name as a long option (``input`` -> ``--input``)."""
    stripped = name.lstrip("-")
    return f"--{stripped}" if stripped else name


def build_argv(
    skill: Skill,
    entrypoint_path: Path,
    overrides: Mapping[str, Any] | None = None,
) -> list[str]:
    """Build the argv list used to run *skill* (useful for dry runs and tests).

    Args:
        skill: The skill being run; ``skill.args`` supplies the defaults.
        entrypoint_path: Absolute or relative path to the skill's Python file.
        overrides: Keyword arguments merged over ``skill.args`` (they win).

    Returns:
        ``[sys.executable, str(entrypoint_path), *flag_tokens]``.
    """
    merged: dict[str, Any] = dict(skill.args)
    if overrides:
        merged.update(overrides)

    argv: list[str] = [sys.executable, str(entrypoint_path)]
    for name, value in merged.items():
        flag = _flag(name)
        if value is None:
            continue
        if isinstance(value, bool):
            if value:
                argv.append(flag)
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                argv.extend([flag, str(item)])
            continue
        argv.extend([flag, str(value)])
    return argv


def run_skill(
    skill: Skill,
    skills_root: Path,
    timeout: int = 60,
    **kwargs: Any,
) -> SkillResult:
    """Execute *skill* and capture its output.

    The entrypoint is resolved as ``skills_root / skill.name / skill.entrypoint``
    (so a skill must live in a directory named after it), then launched with
    ``sys.executable``.  Nothing is run through a shell.

    Args:
        skill: Skill to run.
        skills_root: Directory holding per-skill subdirectories.
        timeout: Seconds before the child is killed and reported as a timeout.
        **kwargs: Extra or overriding flags; merged over ``skill.args``.

    Returns:
        A :class:`SkillResult`.  ``returncode`` is the child's exit status, or
        ``-1`` (timeout), ``127`` (entrypoint missing) or ``126`` (cannot
        launch) when the framework itself could not complete the run.
    """
    entrypoint_path = Path(skills_root) / skill.name / skill.entrypoint
    if not entrypoint_path.is_file():
        return SkillResult(
            skill_name=skill.name,
            stdout="",
            stderr=f"entrypoint not found: {entrypoint_path}",
            returncode=MISSING_ENTRYPOINT_RETURNCODE,
        )

    argv = build_argv(skill, entrypoint_path, kwargs)
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SkillResult(
            skill_name=skill.name,
            stdout="",
            stderr="timeout",
            returncode=TIMEOUT_RETURNCODE,
        )
    except OSError as exc:
        return SkillResult(
            skill_name=skill.name,
            stdout="",
            stderr=f"cannot launch {entrypoint_path}: {exc}",
            returncode=LAUNCH_FAILURE_RETURNCODE,
        )

    return SkillResult(
        skill_name=skill.name,
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )
