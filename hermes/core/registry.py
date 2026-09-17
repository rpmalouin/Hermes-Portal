"""In-memory registry of loaded skills, keyed by ``Skill.name``.

The registry is deliberately dumb storage: it never touches the filesystem and
never runs anything.  It is the single place that enforces uniqueness of skill
names (``register`` raises ``ValueError`` on a duplicate, and the runtime turns
that into a warning so one duplicated skill cannot kill startup).
"""

from __future__ import annotations

from .models import Skill


class SkillRegistry:
    """A name -> :class:`Skill` map with de-duplication on ``Skill.name``."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """Add *skill* to the registry.

        Args:
            skill: The skill to store.

        Raises:
            ValueError: Another skill with the same ``name`` is already
                registered.
        """
        if skill.name in self._skills:
            raise ValueError(f"duplicate skill name: {skill.name!r}")
        self._skills[skill.name] = skill

    def list(self) -> list[Skill]:
        """Return every registered skill, sorted by name."""
        return [self._skills[name] for name in sorted(self._skills)]

    def get(self, name: str) -> Skill:
        """Return the skill called *name*.

        Raises:
            KeyError: No skill is registered under that name.
        """
        try:
            return self._skills[name]
        except KeyError:
            raise KeyError(f"unknown skill: {name!r}") from None

    def by_box(self, box: str) -> list[Skill]:
        """Return the skills whose ``primary_box`` equals *box*, sorted by name."""
        return [skill for skill in self.list() if skill.primary_box == box]

    def names(self) -> list[str]:
        """Return the registered skill names, sorted (convenience)."""
        return sorted(self._skills)

    def __len__(self) -> int:
        """Return the number of registered skills."""
        return len(self._skills)

    def __contains__(self, name: object) -> bool:
        """Support ``"some_skill" in registry``."""
        return name in self._skills
