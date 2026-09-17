"""High-level entry point: load a skills directory once, run skills by name.

``Runtime(root)`` loads ``root / "skills"`` exactly as specified.  Two optional
keyword arguments and one helper are extensions that make the shipped
``hermes/profiles/default.json`` functional rather than decorative:

* ``Runtime(root, skills_dir=..., timeout=...)`` -- point at a non-default skills
  directory and set the default execution timeout.  With no keyword arguments the
  specified behaviour is unchanged (``root/skills``, 60 second timeout).
* :func:`load_profile` / :meth:`Runtime.from_profile` -- read
  ``{"name", "skills_dir", "timeout"}`` from a profile JSON file.  ``skills_dir``
  is resolved relative to the *package directory*, i.e. the parent of the
  ``profiles`` directory, so ``profiles/default.json`` with ``"skills_dir":
  "skills"`` resolves to ``hermes/skills``.

A duplicate skill name is a warning, not a crash: the first registration wins.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

from .executor import run_skill
from .loader import load_skills
from .models import SkillResult
from .registry import SkillRegistry

DEFAULT_TIMEOUT: Final[int] = 60
PROFILE_FIELDS: Final[tuple[str, ...]] = ("name", "skills_dir", "timeout")


def load_profile(path: Path) -> dict[str, Any]:
    """Read and validate a profile manifest.

    Args:
        path: Path to a profile JSON file such as ``profiles/default.json``.

    Returns:
        ``{"name": str, "skills_dir": str, "timeout": int}`` with ``timeout``
        defaulting to :data:`DEFAULT_TIMEOUT` when the file omits it.

    Raises:
        OSError: The file cannot be read.
        ValueError: The JSON is malformed, or ``name``/``skills_dir`` are not
            strings, or ``timeout`` is not a positive integer.
    """
    profile_path = Path(path)
    try:
        raw = profile_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError(f"cannot read profile {profile_path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{profile_path}: malformed JSON ({exc})") from exc

    if not isinstance(data, dict):
        raise ValueError(f"{profile_path}: profile must be a JSON object")

    for field_name in ("name", "skills_dir"):
        if not isinstance(data.get(field_name), str):
            raise ValueError(f"{profile_path}: field {field_name!r} must be a string")

    timeout = data.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError(f"{profile_path}: field 'timeout' must be a positive integer")

    return {
        "name": data["name"],
        "skills_dir": data["skills_dir"],
        "timeout": timeout,
    }


class Runtime:
    """Owns a skills directory, its :class:`SkillRegistry` and the timeout."""

    def __init__(
        self,
        root: Path,
        skills_dir: Path | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Load every skill under ``skills_dir`` (default ``root / "skills"``).

        Args:
            root: Project or package root, used for the default skills location.
            skills_dir: Explicit skills directory; defaults to ``root/skills``.
            timeout: Default per-run timeout in seconds, forwarded to
                :func:`hermes.core.executor.run_skill`.
        """
        self._root = Path(root)
        self._skills_dir = (
            Path(skills_dir) if skills_dir is not None else self._root / "skills"
        )
        self._timeout = timeout
        self._registry = SkillRegistry()

        for skill in load_skills(self._skills_dir):
            try:
                self._registry.register(skill)
            except ValueError as exc:
                print(
                    f"warning: {self._skills_dir}: {exc} (keeping first)",
                    file=sys.stderr,
                )

    @property
    def root(self) -> Path:
        """The directory this runtime was built for."""
        return self._root

    @property
    def skills_dir(self) -> Path:
        """The directory skills are loaded from and run out of."""
        return self._skills_dir

    @property
    def timeout(self) -> int:
        """Default per-run timeout in seconds."""
        return self._timeout

    @property
    def skills(self) -> SkillRegistry:
        """The populated :class:`~hermes.core.registry.SkillRegistry`."""
        return self._registry

    def run(self, skill_name: str, **kwargs: Any) -> SkillResult:
        """Look up *skill_name* and execute it.

        Args:
            skill_name: Registered skill name.
            **kwargs: Extra or overriding flags for the skill.  A ``timeout``
                keyword sets the per-run timeout for this call only (the
                framework consumes it; the skill never sees the flag) and
                defaults to the runtime's timeout.

        Returns:
            A :class:`SkillResult`.

        Raises:
            KeyError: No skill is registered under *skill_name*.
        """
        skill = self._registry.get(skill_name)
        kwargs.setdefault("timeout", self._timeout)
        return run_skill(skill, self._skills_dir, **kwargs)

    @classmethod
    def from_profile(cls, profile_path: Path) -> Runtime:
        """Build a runtime from a profile file.

        ``skills_dir`` is resolved relative to the package directory (the parent
        of the directory holding the profile), so ``hermes/profiles/default.json``
        with ``"skills_dir": "skills"`` loads ``hermes/skills``.

        Args:
            profile_path: Path to the profile JSON file.

        Returns:
            A ready :class:`Runtime`.

        Raises:
            OSError: The profile cannot be read.
            ValueError: The profile is malformed or incomplete.
        """
        profile = load_profile(profile_path)
        package_dir = Path(profile_path).resolve().parent.parent
        return cls(
            package_dir,
            skills_dir=package_dir / profile["skills_dir"],
            timeout=profile["timeout"],
        )
