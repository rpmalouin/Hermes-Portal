"""Discovering the skills a Hermes agent has, as documents.

This is the view-free half of what used to live inside the Skill Deck module.
Walking the skill roots, reading ``SKILL.md`` frontmatter, de-duplicating and
filtering belong next to the framework rather than inside a page, so both the
portal's skills domain and anything else needing the same answers can share one
implementation.

Four behaviours here are deliberate and load-bearing:

* **The walk follows directory symlinks.** Hermes skill trees link many skills in
  from other checkouts, and ``Path.rglob`` silently skips every one of them.
* **A directory holding ``SKILL.md`` is a skill** and is never descended into, so
  a skill's own reference files cannot register as skills.
* **Only top-level frontmatter scalars are read.** Nested blocks such as
  ``metadata:`` and block scalars are ignored rather than guessed at, and a
  missing ``name`` falls back to the directory name.
* **Nothing is deduped silently.** :func:`dedupe_cards` reports how many cards it
  dropped by name and by path, because the same tree is honestly "157 unique
  names", "421 files" or "4 roots" depending on how you count, and callers are
  expected to publish those definitions side by side instead of picking one.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
SKILL_FILENAME = "SKILL.md"
HERMES_ORIGIN = "hermes"
PRUNED_DIRS = frozenset(
    {"__pycache__", ".git", ".venv", "venv", ".ruff_cache", "node_modules"}
)

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<block>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)
_SCALAR_RE = re.compile(r"^(?P<key>[A-Za-z_][\w-]*):[ \t]*(?P<value>.*)$")
_HEADING_RE = re.compile(r"^#[ \t]+(?P<title>\S.*?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Card:
    """One skill found on disk."""

    name: str
    title: str
    description: str
    box: str
    category: str
    origin: str
    source: str
    path: str


@dataclass(frozen=True)
class SkillRoot:
    """A directory scanned for ``SKILL.md`` files, with a human label."""

    label: str
    path: Path


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes from a frontmatter value."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a ``SKILL.md`` document into frontmatter fields and body text.

    Documented subset: only top-level ``key: value`` pairs are read.  Nested
    mappings (``metadata:``), block scalars and list values are ignored rather
    than guessed at, and indented lines, comments and unknown keys are skipped.
    A file with no ``---`` fence yields ``({}, text)``.

    Args:
        text: Full contents of a SKILL.md file.

    Returns:
        ``(fields, body)`` where *fields* maps top-level keys to unquoted
        scalars and *body* is everything after the closing fence.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text

    fields: dict[str, str] = {}
    for line in match.group("block").splitlines():
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        scalar = _SCALAR_RE.match(line)
        if scalar is not None:
            fields[scalar.group("key")] = _unquote(scalar.group("value"))
    return fields, text[match.end() :]


def parse_frontmatter(text: str) -> dict[str, str]:
    """Return just the frontmatter fields of a ``SKILL.md`` document."""
    return split_frontmatter(text)[0]


def _humanize(name: str) -> str:
    """Turn a skill identifier into a display title."""
    return name.replace("_", " ").replace("-", " ").strip().title()


def card_from_skill_file(path: Path, source: str, root: Path) -> Card | None:
    """Build a :class:`Card` from one ``SKILL.md`` file.

    Args:
        path: Path to the ``SKILL.md`` file.
        source: Label of the root it was found under.
        root: The scan root, used to derive the box/category.

    Returns:
        A card, or ``None`` when the file cannot be read.  A missing ``name``
        falls back to the directory name rather than dropping the skill.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    fields, body = split_frontmatter(text)
    name = fields.get("name", "").strip() or path.parent.name
    description = fields.get("description", "").strip() or "(no description)"

    heading = _HEADING_RE.search(body)
    title = heading.group("title") if heading is not None else _humanize(name)

    try:
        parts = path.parent.relative_to(root).parts
    except ValueError:
        parts = (path.parent.name,)
    parts = tuple(part for part in parts if part not in (".", ""))

    try:
        real_path = str(path.resolve())
    except OSError:
        real_path = str(path)

    return Card(
        name=name,
        title=title,
        description=description,
        box=parts[0] if parts else "root",
        category="/".join(parts) if parts else "root",
        origin=HERMES_ORIGIN,
        source=source,
        path=real_path,
    )


def discover_skills(root: Path, source: str) -> list[Card]:
    """Find every ``SKILL.md`` below *root*, following symlinks.

    Args:
        root: Directory to walk (may itself be a symlink).
        source: Label recorded on each card.

    Returns:
        One card per skill directory.  Directories holding a ``SKILL.md`` are
        not descended into, and noise directories are pruned.
    """
    directory_root = Path(root)
    if not directory_root.is_dir():
        return []

    cards: list[Card] = []
    for dirpath, dirnames, filenames in os.walk(directory_root, followlinks=True):
        dirnames[:] = sorted(name for name in dirnames if name not in PRUNED_DIRS)
        current = Path(dirpath)
        if SKILL_FILENAME in filenames:
            card = card_from_skill_file(
                current / SKILL_FILENAME, source, directory_root
            )
            if card is not None:
                cards.append(card)
            dirnames[:] = []  # a skill directory contains no nested skills
    return cards


def default_hermes_home() -> Path:
    """Return the running Hermes' home directory.

    ``$HERMES_HOME`` wins when set -- inside a Hermes session it points at the
    running profile (``~/.hermes/profiles/<name>``), whose ``skills/`` directory
    is exactly what the agent has.  Otherwise ``~/.hermes``.
    """
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _profile_dirs(home: Path) -> list[Path]:
    """Profile directories for *home*, whether it is a hermes root or a profile.

    ``$HERMES_HOME`` points at a hermes *root* in some launches (``~/.hermes``)
    and at the running *profile* in others (``~/.hermes/profiles/<name>``), so
    the profiles container is either ``home/profiles`` or ``home.parent``.
    """
    container: Path | None = None
    if (home / "profiles").is_dir():
        container = home / "profiles"
    elif (home.parent / "profiles").is_dir():
        container = home.parent / "profiles"
    elif home.parent.name == "profiles":
        container = home.parent
    if container is None:
        return []
    return sorted(
        entry
        for entry in container.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    )


def resolve_hermes_roots(
    hermes_home: Path | None = None,
    profile: str | None = None,
    all_profiles: bool = False,
) -> list[SkillRoot]:
    """Decide which Hermes skill directories to read.

    Args:
        hermes_home: Hermes home or profile directory; defaults to
            :func:`default_hermes_home`.
        profile: Named profile; reads ``<home>/profiles/<profile>/skills``.
        all_profiles: Also read every ``<home>/profiles/*/skills``.

    Returns:
        Roots in priority order, de-duplicated by real path so the running
        profile is never scanned twice.
    """
    home = (
        Path(hermes_home).expanduser()
        if hermes_home is not None
        else default_hermes_home()
    )

    roots: list[SkillRoot] = []
    if profile:
        profile_skills = home / "profiles" / profile / "skills"
        roots.append(SkillRoot(f"profile {profile}", profile_skills))
    else:
        roots.append(SkillRoot("running hermes", home / "skills"))

    if all_profiles:
        for profile_dir in _profile_dirs(home):
            if profile and profile_dir.name == profile:
                continue  # already covered by the named-profile root
            label = f"profile {profile_dir.name}"
            roots.append(SkillRoot(label, profile_dir / "skills"))

    unique: list[SkillRoot] = []
    seen: set[str] = set()
    for skill_root in roots:
        key = str(skill_root.path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(skill_root)
    return unique


def dedupe_cards(cards: Iterable[Card]) -> tuple[list[Card], int]:
    """Drop cards sharing a real path or an (origin, name); first one wins."""
    kept: list[Card] = []
    seen_paths: set[str] = set()
    seen_names: set[tuple[str, str]] = set()
    dropped = 0
    for card in cards:
        name_key = (card.origin, card.name.lower())
        if card.path in seen_paths or name_key in seen_names:
            dropped += 1
            continue
        seen_paths.add(card.path)
        seen_names.add(name_key)
        kept.append(card)
    return kept, dropped


def matches_box(card: Card, wanted: str) -> bool:
    """``True`` when *card* sits inside the box or category path *wanted*."""
    if card.box.lower() == wanted:
        return True
    category = card.category.lower()
    return category == wanted or category.startswith(f"{wanted}/")


def filter_cards(cards: Sequence[Card], boxes: Sequence[str]) -> tuple[list[Card], int]:
    """Keep the cards inside any of *boxes*.

    A value matches a card's ``box`` (``creative``) or a ``category`` path
    (``creative/ascii-art``), case-insensitively; a category path also
    selects everything below it.  An empty *boxes* keeps every card.

    Args:
        cards: Cards to filter.
        boxes: Box or category names to keep.

    Returns:
        ``(kept, hidden)`` where *hidden* is how many cards the filter
        removed.
    """
    wanted = [value.strip().lower() for value in boxes if value.strip()]
    if not wanted:
        return list(cards), 0
    kept = [
        card for card in cards if any(matches_box(card, target) for target in wanted)
    ]
    return kept, len(cards) - len(kept)
