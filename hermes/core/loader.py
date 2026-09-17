"""Skill discovery: turn a ``skills`` directory into :class:`Skill` objects.

Robustness rule: a single bad skill never aborts discovery.  Malformed JSON,
missing fields, wrong field types and unreadable manifests are reported as
one-line ``warning:`` messages on stderr and the offending directory is skipped;
:func:`load_skills` never raises for an individual bad skill.

A directory without a ``skill.json`` is not a skill and is skipped silently.
A manifest whose ``name`` does not match its directory name is loaded but warned
about, because the executor resolves entrypoints as
``<skills_dir>/<name>/<entrypoint>`` and such a skill would not be runnable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .models import Skill

MANIFEST_NAME = "skill.json"


def _warn(manifest_path: Path, message: str) -> None:
    """Write a single-line warning to stderr."""
    print(f"warning: {manifest_path}: {message}", file=sys.stderr)


def _missing_dir_hint(path: Path) -> str:
    """Explain a missing skills directory and, when possible, name the fix.

    Passing a project root instead of the directory that *contains* ``skills/``
    is the usual cause: the requested directory is then ``<project>/skills``
    while the real one is one level deeper.  Look inside *path* and its parent
    for a ``*/skills`` candidate; a candidate inside a Python package (a sibling
    of ``__init__.py``) wins.
    """
    search_roots = [path, path.parent] if path.parent != path else [path]
    candidates: set[Path] = set()
    for search_root in search_roots:
        if search_root.is_dir():
            candidates.update(
                candidate
                for candidate in search_root.glob("*/skills")
                if candidate.is_dir()
            )
    if not candidates:
        return "skills directory not found"

    fix = min(
        candidates,
        key=lambda candidate: (
            not (candidate.parent / "__init__.py").is_file(),
            str(candidate),
        ),
    )
    return (
        "skills directory not found (did you pass a project root? "
        f"pass {fix.parent} instead of {path}, or use skills_dir={fix})"
    )


def load_skills(skills_dir: Path) -> list[Skill]:
    """Load every valid skill manifest below *skills_dir*.

    Args:
        skills_dir: Directory whose immediate subdirectories hold skills.  It is
            a directory of *skill directories*, not a project root.

    Returns:
        The successfully loaded skills sorted by directory name.  A missing or
        unreadable *skills_dir* yields ``[]`` (with a warning naming the likely
        fix), never an exception.
    """
    root = Path(skills_dir)
    if not root.is_dir():
        _warn(root, _missing_dir_hint(root))
        return []

    skills: list[Skill] = []
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir():
            continue

        manifest_path = candidate / MANIFEST_NAME
        if not manifest_path.is_file():
            continue

        try:
            raw = manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            _warn(manifest_path, f"cannot read manifest ({exc})")
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _warn(manifest_path, f"malformed JSON ({exc})")
            continue

        try:
            skill = Skill.from_dict(data)
        except ValueError as exc:
            _warn(manifest_path, str(exc))
            continue

        if skill.name != candidate.name:
            _warn(
                manifest_path,
                f"manifest name {skill.name!r} does not match directory "
                f"{candidate.name!r}; run it via its manifest name",
            )

        skills.append(skill)

    return skills
