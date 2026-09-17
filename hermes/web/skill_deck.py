"""Web-based Skill Deck UI for Hermes-Dashboard (standard library only).

``hermes/web`` holds one module per web view; this one serves the Skill Deck.

The page shows cards from two sources:

* **framework** -- the ``skill.json`` skills this project registers through
  :class:`~hermes.core.runtime.Runtime`.
* **hermes** -- every skill the *running* Hermes agent has: the ``SKILL.md``
  files under ``$HERMES_HOME/skills`` (and, with ``--all-profiles``, under every
  ``$HERMES_HOME/profiles/*/skills``).

Endpoints: ``/`` (or ``/index.html``) renders the card grid, ``/skills.json``
returns the same cards as JSON so a script can check what is loaded instead of
counting divs.  Both accept ``?box=NAME`` (repeatable), and the page carries a
box dropdown that re-filters over the URL, so the matching rule still lives in
exactly one place: :func:`filter_cards`.  The dropdown is a plain GET form whose
``onchange`` submits it, with a ``<noscript>`` button for browsers without JS.

Run with::

    python -m hermes.web.skill_deck                 # this package + hermes
    python -m hermes.web.skill_deck --all-profiles   # every profile too
    python -m hermes.web.skill_deck --list           # print what would show
    python -m hermes.web.skill_deck --port 9000 --no-framework

Four discovery traps are handled here deliberately, because each one silently
under-reports the deck:

* **Symlinks.** ``~/.hermes/skills`` links dozens of skills in from
  ``/Volumes/Data/.agents/skills``.  ``Path.rglob`` does not follow directory
  symlinks (116 of 151 files found); ``os.walk(followlinks=True)`` finds all.
* **Duplicates.** One skill is reachable through several roots -- the whole home
  holds 381 reachable ``SKILL.md`` paths for 153 distinct names -- so cards are
  de-duplicated by frontmatter ``name`` and by real path.
* **Nesting.** A directory containing ``SKILL.md`` *is* a skill and is never
  descended into, so a skill's own reference files cannot register as skills.
* **Frontmatter.** Only top-level ``key: value`` scalars are read (a documented
  subset: nested blocks such as ``metadata:`` are ignored, never guessed at).
  Text is HTML-escaped before any markup is added.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from typing import Any

from ..core.runtime import Runtime

PACKAGE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
SKILL_FILENAME = "SKILL.md"
FRAMEWORK_ORIGIN = "framework"
HERMES_ORIGIN = "hermes"
DESCRIPTION_LIMIT = 280
PRUNED_DIRS = frozenset(
    {"__pycache__", ".git", ".venv", "venv", ".ruff_cache", "node_modules"}
)

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<block>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)
_SCALAR_RE = re.compile(r"^(?P<key>[A-Za-z_][\w-]*):[ \t]*(?P<value>.*)$")
_HEADING_RE = re.compile(r"^#[ \t]+(?P<title>\S.*?)[ \t]*$", re.MULTILINE)
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


@dataclass(frozen=True)
class Card:
    """One skill as shown on the deck."""

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


@dataclass
class SourceStatus:
    """What one root contributed, so an empty deck can be explained."""

    label: str
    path: Path
    found: int = 0

    @property
    def present(self) -> bool:
        """``True`` when the directory exists on disk."""
        return self.path.is_dir()


@dataclass
class DeckData:
    """Everything the page needs: cards plus provenance."""

    cards: list[Card] = field(default_factory=list)
    sources: list[SourceStatus] = field(default_factory=list)
    dropped: int = 0
    filters: tuple[str, ...] = ()
    hidden: int = 0
    inventory: dict[str, int] = field(default_factory=dict)


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


def _dedupe(cards: Iterable[Card]) -> tuple[list[Card], int]:
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


def _matches_box(card: Card, wanted: str) -> bool:
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
        card for card in cards if any(_matches_box(card, target) for target in wanted)
    ]
    return kept, len(cards) - len(kept)


def apply_box_filter(data: DeckData, boxes: Sequence[str]) -> DeckData:
    """Return *data* with *boxes* applied, keeping the full inventory.

    The HTTP handler keeps one unfiltered deck and calls this per request, so a
    filtered view costs no extra filesystem scan.

    Args:
        data: A deck, usually the unfiltered one.
        boxes: Box or category names to keep; blank values are ignored.

    Returns:
        A copy with the filtered cards, the active filters and the hidden count
        updated; sources and inventory are carried over untouched.
    """
    kept, hidden = filter_cards(data.cards, boxes)
    return replace(data, cards=kept, filters=tuple(boxes), hidden=hidden)


def framework_cards(root: Path) -> list[Card]:
    """Cards for the ``skill.json`` skills registered under *root*."""
    runtime = Runtime(Path(root))
    cards: list[Card] = []
    for skill in runtime.skills.list():
        cards.append(
            Card(
                name=skill.name,
                title=skill.title,
                description=skill.description,
                box=skill.primary_box,
                category=skill.raw_category,
                origin=FRAMEWORK_ORIGIN,
                source="skill.json",
                path=str(Path(root) / "skills" / skill.name / "skill.json"),
            )
        )
    return cards


def build_deck(
    root: Path,
    hermes_home: Path | None = None,
    profile: str | None = None,
    all_profiles: bool = False,
    include_framework: bool = True,
    boxes: Sequence[str] = (),
) -> DeckData:
    """Collect the cards and source provenance for the deck.

    Args:
        root: Directory holding this project's ``skills/`` (the package dir).
        hermes_home: Hermes home or profile directory; ``None`` uses
            ``$HERMES_HOME`` then ``~/.hermes``.
        profile: Named Hermes profile to read.
        all_profiles: Also read every profile under the Hermes home.
        include_framework: Include this project's ``skill.json`` skills.
        boxes: Keep only cards in these boxes or category paths; the
            source counts still report what every root contained.

    Returns:
        A :class:`DeckData` with de-duplicated, filtered cards, one
        :class:`SourceStatus` per scanned root, the number of dropped
        duplicates and the number of cards the filter hid.
    """
    cards: list[Card] = []
    sources: list[SourceStatus] = []

    if include_framework:
        project_cards = framework_cards(root)
        cards.extend(project_cards)
        sources.append(
            SourceStatus(
                "framework skill.json", Path(root) / "skills", len(project_cards)
            )
        )

    for skill_root in resolve_hermes_roots(hermes_home, profile, all_profiles):
        found = discover_skills(skill_root.path, skill_root.label)
        cards.extend(found)
        sources.append(SourceStatus(skill_root.label, skill_root.path, len(found)))

    kept, dropped = _dedupe(cards)

    def sort_key(card: Card) -> tuple[bool, str, str]:
        return (card.origin != FRAMEWORK_ORIGIN, card.box, card.name.lower())

    kept.sort(key=sort_key)

    inventory = _count_by_box(kept)
    filtered, hidden = filter_cards(kept, boxes)
    return DeckData(
        cards=filtered,
        sources=sources,
        dropped=dropped,
        filters=tuple(boxes),
        hidden=hidden,
        inventory=inventory,
    )


HTML_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Skill Deck</title>
<style>
    :root { color-scheme: dark; }
    body {
        background: #1a1a2e; color: #eee; margin: 0; padding: 2rem;
        font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    }
    h1 { font-size: 2.25rem; margin: 0 0 1rem; }
    .stats {
        margin-bottom: 1rem; padding: 1rem 1.25rem; border-radius: 8px;
        background: #16213e; font-size: 0.95rem; line-height: 1.7;
    }
    .stats strong { color: #e94560; }
    form.picker {
        align-items: center; display: flex; flex-wrap: wrap; gap: 0.6rem;
        margin: 0 0 1rem;
    }
    form.picker label { color: #8a8fa3; font-size: 0.85rem; }
    form.picker select {
        background: #16213e; color: #eee; border: 1px solid #0f3460;
        border-radius: 8px; font-size: 0.9rem; min-width: 18rem;
        padding: 0.4rem 0.5rem;
    }
    form.picker button {
        background: #16213e; color: #eee; border: 1px solid #0f3460;
        border-radius: 8px; font-size: 0.9rem; padding: 0.4rem 0.7rem;
    }
    details.sources { margin-bottom: 1.5rem; padding: 0 0.25rem; }
    details.sources summary { color: #8a8fa3; cursor: pointer; font-size: 0.85rem; }
    details.sources ul { margin: 0.6rem 0 0; padding-left: 1.4rem; }
    details.sources li {
        color: #8a8fa3; font-family: ui-monospace, monospace; font-size: 0.78rem;
    }
    details.sources li.missing { color: #e94560; }
    .grid {
        display: grid; gap: 1.25rem;
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    }
    .card {
        background: #16213e; border: 1px solid #0f3460; border-radius: 12px;
        padding: 1.25rem; transition: transform 0.15s ease;
    }
    .card:hover { transform: translateY(-2px); border-color: #e94560; }
    .card.framework { border-left: 3px solid #e94560; }
    .card.hermes { border-left: 3px solid #2f6fed; }
    .card .box {
        color: #8a8fa3; font-size: 0.7rem; letter-spacing: 0.05em;
        text-transform: uppercase;
    }
    .card h3 { color: #e94560; font-size: 1.1rem; margin: 0.4rem 0 0.5rem; }
    .card .desc { color: #c8cad3; font-size: 0.85rem; line-height: 1.45; }
    .card .desc code {
        background: #0f3460; border-radius: 4px; padding: 0 0.25rem;
    }
    .card .src {
        color: #6c7186; font-family: ui-monospace, monospace; font-size: 0.7rem;
        margin-top: 0.85rem; word-break: break-all;
    }
    .empty { color: #8a8fa3; font-style: italic; }
</style>
</head>
<body>
    <h1>Hermes Skill Deck</h1>
    <form class="picker" method="get" action="/">
        <label for="box">Box</label>
        <select id="box" name="box" onchange="this.form.submit()">
$box_options
        </select>
        <noscript><button type="submit">Apply</button></noscript>
    </form>
    <div class="stats">
        <strong>$total</strong> skills ($framework framework, $hermes hermes)
        across <strong>$boxes</strong> boxes$dropped$filtered
    </div>
    <details class="sources">
        <summary>Sources ($present/$searched present)</summary>
        <ul>
        $source_items
        </ul>
    </details>
    <div class="grid">
    $cards
    </div>
</body>
</html>
"""
)


CARD_TEMPLATE = Template(
    """    <div class="card $origin">
        <div class="box">$box / $category</div>
        <h3>$title</h3>
        <div class="desc">$description</div>
        <div class="src" title="$path">$origin / $source</div>
    </div>"""
)


def _rich(text: str, limit: int = DESCRIPTION_LIMIT) -> str:
    """Escape *text* for HTML, then apply a minimal, injection-safe markdown pass."""
    flat = " ".join(text.split())
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "\u2026"
    escaped = html.escape(flat, quote=True)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    return _BOLD_RE.sub(r"<strong>\1</strong>", escaped)


def render_box_picker(data: DeckData) -> str:
    """Render the option list of the box dropdown.

    Options come from the *pre-filter* inventory, so every box stays reachable
    after a filter is applied, and the active one is marked selected.  With more
    than one box active (reachable only via --box) a disabled first option states
    the real filter instead of misrepresenting it as a single selection.
    """
    inventory = data.inventory or _count_by_box(data.cards)
    active = [value.strip().lower() for value in data.filters if value.strip()]
    sole = active[0] if len(active) == 1 else None

    options: list[str] = []
    if len(active) > 1:
        label = html.escape(", ".join(data.filters), quote=True)
        options.append(
            f'            <option value="" disabled selected>boxes: {label}</option>'
        )
    total = sum(inventory.values())
    options.append(
        f'<option value=""{" selected" if not active else ""}>'
        f"All boxes ({total})</option>"
    )
    for box, count in sorted(inventory.items()):
        selected = " selected" if sole is not None and box.lower() == sole else ""
        escaped_box = html.escape(box, quote=True)
        label = html.escape(box)
        options.append(
            f'<option value="{escaped_box}"{selected}>{label} ({count})</option>'
        )
    return "\n".join(options)


def render_card(card: Card) -> str:
    """Render one skill card as HTML."""
    return CARD_TEMPLATE.substitute(
        origin=html.escape(card.origin, quote=True),
        box=html.escape(card.box, quote=True),
        category=html.escape(card.category, quote=True),
        title=html.escape(card.title, quote=True),
        description=_rich(card.description),
        path=html.escape(card.path, quote=True),
        source=html.escape(card.source, quote=True),
    )


def render_page(data: DeckData) -> str:
    """Render the whole deck page as HTML (escaped at every interpolation)."""
    if data.cards:
        cards = "\n".join(render_card(card) for card in data.cards)
    elif data.filters:
        wanted = html.escape(", ".join(data.filters), quote=True)
        cards = (
            '    <p class="empty">No skills in '
            f"--box {wanted}. Drop the filter, or use --list to see the boxes.</p>"
        )
    else:
        cards = (
            '    <p class="empty">No skills found. Run this from the project root, '
            "or point --hermes-home at your Hermes directory.</p>"
        )

    source_items = "\n".join(
        "        <li{}>{}: {} skill(s) [{}]</li>".format(
            "" if status.present else ' class="missing"',
            html.escape(status.label, quote=True),
            status.found,
            html.escape(str(status.path), quote=True),
        )
        for status in data.sources
    )

    boxes = len({card.box for card in data.cards})
    return HTML_TEMPLATE.substitute(
        total=len(data.cards),
        framework=sum(1 for card in data.cards if card.origin == FRAMEWORK_ORIGIN),
        hermes=sum(1 for card in data.cards if card.origin == HERMES_ORIGIN),
        boxes=boxes,
        dropped=(
            f" \u2014 {data.dropped} duplicate card(s) skipped" if data.dropped else ""
        ),
        filtered=(
            f" \u2014 in {'boxes' if len(data.filters) > 1 else 'box'} "
            f"{html.escape(', '.join(data.filters), quote=True)}"
            f" ({data.hidden} hidden)"
            if data.filters
            else ""
        ),
        present=sum(1 for status in data.sources if status.present),
        searched=len(data.sources),
        source_items=source_items,
        box_options=render_box_picker(data),
        cards=cards,
    )


def _count_by_box(cards: Sequence[Card]) -> dict[str, int]:
    """Count cards per box, for the JSON payload and ``--list``."""
    counts: dict[str, int] = {}
    for card in cards:
        counts[card.box] = counts.get(card.box, 0) + 1
    return dict(sorted(counts.items()))


def json_payload(data: DeckData) -> dict[str, Any]:
    """Serialise the deck for the ``/skills.json`` endpoint."""
    return {
        "counts": {
            "total": len(data.cards),
            "framework": sum(
                1 for card in data.cards if card.origin == FRAMEWORK_ORIGIN
            ),
            "hermes": sum(1 for card in data.cards if card.origin == HERMES_ORIGIN),
            "boxes": len({card.box for card in data.cards}),
            "duplicates_dropped": data.dropped,
            "box_filter": list(data.filters),
            "hidden_by_filter": data.hidden,
            "by_box": _count_by_box(data.cards),
            "inventory": data.inventory or _count_by_box(data.cards),
        },
        "sources": [
            {
                "label": status.label,
                "path": str(status.path),
                "present": status.present,
                "found": status.found,
            }
            for status in data.sources
        ],
        "cards": [
            {
                "name": card.name,
                "title": card.title,
                "description": card.description,
                "box": card.box,
                "category": card.category,
                "origin": card.origin,
                "source": card.source,
                "path": card.path,
            }
            for card in data.cards
        ],
    }


class DeckHandler(BaseHTTPRequestHandler):
    """HTTP handler serving the deck at ``/`` and its data at ``/skills.json``.

    ``data`` is the unfiltered deck (one filesystem scan per process) and
    ``default_boxes`` is the ``--box`` filter given on the command line.  A
    ``?box=`` in the request overrides it -- that is what the page's dropdown
    sends, so the CLI flag sets the starting view and the URL changes it.
    """

    data: DeckData = DeckData()
    default_boxes: tuple[str, ...] = ()

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        """Answer ``/`` with HTML and ``/skills.json`` with the card payload."""
        parsed = urllib.parse.urlsplit(self.path)
        data = self._filtered(self._requested_boxes(parsed.query))

        if parsed.path in ("/", "/index.html"):
            self._respond("text/html; charset=utf-8", render_page(data).encode())
        elif parsed.path in ("/skills.json", "/api/skills"):
            payload = json.dumps(json_payload(data), indent=2) + "\n"
            self._respond("application/json; charset=utf-8", payload.encode())
        else:
            self.send_error(404, "Not Found")

    def _requested_boxes(self, query: str) -> tuple[str, ...]:
        """Boxes asked for by the query string; the URL beats ``--box``.

        Blank values mean *no* filter, not a filter on the empty string:
        ``?box=`` is what the dropdown's "All boxes" entry submits.
        """
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        if "box" not in params:
            return self.default_boxes
        return tuple(value for value in params["box"] if value.strip())

    def _filtered(self, boxes: tuple[str, ...]) -> DeckData:
        """Return the deck for this request, scanning nothing extra."""
        if not boxes:
            return self.data
        return apply_box_filter(self.data, boxes)

    def _respond(self, content_type: str, body: bytes) -> None:
        """Send a 200 response with *body*."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ARG002
        """Quiet default request logging; comment out to re-enable."""
        return


def describe(data: DeckData) -> str:
    """Return a printable summary of what the deck would show."""
    lines = [
        f"{len(data.cards)} skills "
        f"({sum(1 for c in data.cards if c.origin == FRAMEWORK_ORIGIN)} framework, "
        f"{sum(1 for c in data.cards if c.origin == HERMES_ORIGIN)} hermes), "
        f"{data.dropped} duplicate card(s) skipped",
    ]
    if data.filters:
        selector = " --box ".join(data.filters)
        lines.append(f"filter: --box {selector} ({data.hidden} hidden)")
    lines.append("sources:")
    for status in data.sources:
        marker = "" if status.present else "  [MISSING]"
        lines.append(f"  {status.found:>4}  {status.label}: {status.path}{marker}")
    counts = _count_by_box(data.cards)
    if counts:
        lines.append("boxes:")
        for box, count in counts.items():
            lines.append(f"  {count:>4}  {box}")
    return "\n".join(lines)


def serve(
    root: Path,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    hermes_home: Path | None = None,
    profile: str | None = None,
    all_profiles: bool = False,
    include_framework: bool = True,
    boxes: Sequence[str] = (),
) -> int:
    """Build the deck and serve it until interrupted.

    Args:
        root: Directory containing this project's ``skills/`` directory.
        port: TCP port to bind (0 picks a free port).
        host: Interface to bind; defaults to localhost only.
        hermes_home: Hermes home or profile directory.
        profile: Named Hermes profile to read skills from.
        all_profiles: Also read every profile under the Hermes home.
        include_framework: Include this project's ``skill.json`` skills.
        boxes: Initial box filter.  The page's dropdown overrides it per request
            through ``?box=``, so this only decides the view the server starts on.

    Returns:
        ``0`` on a clean shutdown.
    """
    base = build_deck(
        root,
        hermes_home=hermes_home,
        profile=profile,
        all_profiles=all_profiles,
        include_framework=include_framework,
    )
    DeckHandler.data = base
    DeckHandler.default_boxes = tuple(boxes)

    server = ThreadingHTTPServer((host, port), DeckHandler)
    bound_host, bound_port = server.server_address[:2]
    print(describe(apply_box_filter(base, boxes) if boxes else base))
    print(f"Hermes Skill Deck running at http://{bound_host}:{bound_port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the deck's argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m hermes.web.skill_deck",
        description="Serve the Hermes Skill Deck: this project's skills plus the "
        "running Hermes agent's skills.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="directory containing this project's skills/ (default: this package)",
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=None,
        help="Hermes home or profile directory (default: $HERMES_HOME, else ~/.hermes)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Hermes profile name: reads <home>/profiles/<name>/skills",
    )
    parser.add_argument(
        "--all-profiles",
        action="store_true",
        help="also read every profile under <home>/profiles/*/skills",
    )
    parser.add_argument(
        "--no-framework",
        action="store_true",
        help="show only Hermes skills; skip this project's skill.json registry",
    )
    parser.add_argument(
        "--box",
        action="append",
        default=None,
        metavar="BOX",
        help="only show skills in this box or category path, e.g. creative "
        "or mlops/evaluation (repeatable)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print what would be shown and exit without serving",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="interface to bind")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to bind")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m hermes.web.skill_deck``.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` on success, ``1`` when the deck cannot be built.
    """
    args = build_parser().parse_args(argv)
    root = args.root if args.root is not None else PACKAGE_DIR
    boxes = tuple(args.box or ())

    try:
        data = build_deck(
            root,
            hermes_home=args.hermes_home,
            profile=args.profile,
            all_profiles=args.all_profiles,
            include_framework=not args.no_framework,
            boxes=boxes,
        )
    except OSError as exc:
        print(f"error: cannot build deck: {exc}", file=sys.stderr)
        return 1

    if args.list:
        print(describe(data))
        return 0

    return serve(
        root,
        port=args.port,
        host=args.host,
        hermes_home=args.hermes_home,
        profile=args.profile,
        all_profiles=args.all_profiles,
        include_framework=not args.no_framework,
        boxes=boxes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
