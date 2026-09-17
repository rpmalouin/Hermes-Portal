"""Data models for the Hermes-Dashboard skill framework.

A :class:`Skill` mirrors the on-disk ``skill.json`` manifest field for field.
:class:`SkillResult` is the outcome of running one skill.  Validation in
:meth:`Skill.from_dict` covers exactly what the specification requires -- the
presence and the type of every documented field -- so unknown keys are ignored
and manifests stay forward compatible.

Assumption (spec ambiguity): ``args`` values may be ``str``, ``bool``, ``int``,
``float`` or a ``list`` of those scalars, because boolean and list defaults have
to survive the round trip to command-line flags.  ``bool`` is checked before
``int`` everywhere: ``bool`` is a subclass of ``int``, so an unchecked
``isinstance(value, int)`` would render ``True`` as the string ``"1"``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

REQUIRED_STR_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "title",
    "description",
    "primary_box",
    "raw_category",
    "entrypoint",
)
REQUIRED_LIST_FIELDS: Final[tuple[str, ...]] = ("tags", "related")
ARG_SCALARS: Final[tuple[type, ...]] = (str, bool, int, float)


def _validate_arg_value(key: str, value: Any) -> None:
    """Validate one ``args`` entry, raising ``ValueError`` when unsupported."""
    if isinstance(value, ARG_SCALARS):
        return
    if isinstance(value, list) and all(isinstance(item, ARG_SCALARS) for item in value):
        return
    raise ValueError(
        f"args[{key!r}] must be a string, boolean, number or list of those, "
        f"got {type(value).__name__}"
    )


@dataclass
class Skill:
    """One skill manifest, as described by ``skill.json``.

    Fields map one-to-one onto the manifest schema; see the project README for
    the field reference.  Instances are normally built by
    :meth:`Skill.from_dict` (via ``hermes.core.loader.load_skills``) rather than
    by hand.
    """

    name: str
    title: str
    description: str
    primary_box: str
    raw_category: str
    tags: list[str]
    entrypoint: str
    args: dict[str, Any]
    related: list[str]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Skill:
        """Build a :class:`Skill` from a decoded ``skill.json`` document.

        Args:
            data: Decoded manifest mapping.

        Returns:
            A validated :class:`Skill`.

        Raises:
            ValueError: A required field is missing, a field has the wrong type
                (including unsupported ``args`` values), or ``name``/``entrypoint``
                would take the executor outside the skill's own directory.
        """
        if not isinstance(data, Mapping):
            raise ValueError(
                f"manifest must be a JSON object, got {type(data).__name__}"
            )

        for field_name in REQUIRED_STR_FIELDS + REQUIRED_LIST_FIELDS + ("args",):
            if field_name not in data:
                raise ValueError(f"missing required field: {field_name!r}")

        for field_name in REQUIRED_STR_FIELDS:
            value = data[field_name]
            if not isinstance(value, str):
                raise ValueError(
                    f"field {field_name!r} must be a string, got {type(value).__name__}"
                )

        for field_name in REQUIRED_LIST_FIELDS:
            value = data[field_name]
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError(f"field {field_name!r} must be a list of strings")

        # The executor resolves the entrypoint as ``<skills_dir>/<name>/<entrypoint>``,
        # so both fields are used as path segments.  A name like ``../..`` or an
        # absolute entrypoint would aim the executor outside the skills tree, so they
        # are rejected here: the loader turns a ValueError into a warning and skips
        # the skill, which is exactly what should happen to one that cannot be run
        # from where it lives.
        name = data["name"]
        if name in {"", ".", ".."} or "/" in name or "\\" in name or "\0" in name:
            raise ValueError(
                f"field 'name' must be a single directory name, got {name!r}"
            )
        entrypoint = data["entrypoint"]
        if (
            not entrypoint
            or entrypoint.startswith(("/", "\\"))
            or "\\" in entrypoint
            or ":" in entrypoint
            or ".." in entrypoint.split("/")
        ):
            raise ValueError(
                "field 'entrypoint' must be a relative path inside the skill's own "
                f"directory, got {entrypoint!r}"
            )

        args = data["args"]
        if not isinstance(args, Mapping):
            raise ValueError(
                f"field 'args' must be an object, got {type(args).__name__}"
            )
        for key, value in args.items():
            if not isinstance(key, str):
                raise ValueError(f"args key {key!r} must be a string")
            _validate_arg_value(key, value)

        return cls(
            name=data["name"],
            title=data["title"],
            description=data["description"],
            primary_box=data["primary_box"],
            raw_category=data["raw_category"],
            tags=list(data["tags"]),
            entrypoint=data["entrypoint"],
            args=dict(args),
            related=list(data["related"]),
        )


@dataclass
class SkillResult:
    """Outcome of running a single skill."""

    skill_name: str
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        """``True`` when the skill exited cleanly (return code ``0``)."""
        return self.returncode == 0
