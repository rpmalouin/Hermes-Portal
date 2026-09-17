"""Vault domain: the Obsidian notes, including what links to what.

The vault is small enough (764 notes, about 3 MB) to index in memory in one pass,
and that is what buys the view a folder listing cannot give: **backlinks**.  The
index is built lazily on first use and kept for the life of the process, so every
page stamps its ``as of`` and the note says so: restart the portal to re-read the
vault after an edit.

Sources: the vault directory (``--vault``, default ``/Volumes/Data/MyObsidian``)
and the Kanban board note inside it, whose ``##`` headings are the columns and
whose checkbox lines are the cards.

Frontmatter is read as a documented subset: top-level ``key: value`` scalars plus
``tags`` in either ``tags: [a, b]`` or indented ``- a`` form.  Anything else is
left alone rather than guessed at.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ...core import skill_trees
from ..model import Collection, Count, Domain, Record, Source, build_collection
from ..sources import (
    as_of,
    fmt_ago,
    human_size,
    path_source,
    snippet,
    truncate,
)

DEFAULT_VAULT = Path("/Volumes/Data/MyObsidian")
SKIPPED_DIRS = frozenset({".obsidian", ".trash", ".git", ".smart-env", "node_modules"})
BODY_CAP = 6000
RECENT_CAP = 20
HUBS_CAP = 20
TAGS_CAP = 40
TASKS_CAP = 60
NOTE_CAP = 200
SEARCH_SNIPPET = 200

_TITLE_RE = re.compile(r"^#[ \t]+(?P<title>\S.*?)[ \t]*$", re.MULTILINE)
_TASK_RE = re.compile(
    r"^[ \t]*[-*][ \t]+\[(?P<state>[ xX])\]"
    r"[ \t]*(?P<text>\S.*?)[ \t]*$",
    re.MULTILINE,
)
# a kanban-plugin card is any list item: the checkbox is optional, and its absence
# means open rather than "not a card"
_CARD_RE = re.compile(
    r"^[-*][ \t]+(?:\[(?P<state>[ xX])\][ \t]*)?"
    r"(?P<text>(?!--|<!--)\S.*?)[ \t]*$"
)
_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_HEADING_RE = re.compile(r"^(?P<level>#{1,6})[ \t]+(?P<title>.*?)[ \t]*$", re.MULTILINE)
_TAG_LIST_ITEM = re.compile(r"^[ \t]+-[ \t]+(?P<tag>\S+)[ \t]*$")
_FIELD_RE = re.compile(
    r"^[ \t]+[-*][ \t]+\*\*(?P<key>[^:*]+):?\*\*[ \t]*(?P<value>.*)$"
)


@dataclass
class Note:
    """One markdown note, as indexed from the vault."""

    rel: str
    path: Path
    title: str
    folder: str
    size: int
    mtime: float
    tags: tuple[str, ...]
    links: tuple[str, ...] = ()
    backlinks: tuple[str, ...] = ()
    open_tasks: tuple[str, ...] = ()
    done_tasks: int = 0
    frontmatter_keys: tuple[str, ...] = ()
    body: str = ""


@dataclass
class VaultIndex:
    """Every note, plus the lookups that need the whole vault to answer."""

    root: Path
    notes: dict[str, Note] = field(default_factory=dict)
    by_title: dict[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    as_of: str = ""

    def folders(self) -> list[tuple[str, int]]:
        """Top-level folder -> note count (recursive), biggest first."""
        counter: Counter[str] = Counter()
        for note in self.notes.values():
            counter[note.folder.split("/")[0] if note.folder else "(root)"] += 1
        return sorted(counter.items(), key=lambda item: (-item[1], item[0]))


def _frontmatter(text: str) -> tuple[dict[str, str], tuple[str, ...], str]:
    """Return ``(fields, tags, body)`` for a note.

    Only top-level scalars and two shapes of ``tags`` are read; a nested block other
    than ``tags`` is ignored rather than guessed at.
    """
    fields, body = skill_trees.split_frontmatter(text)
    tags: list[str] = []
    if text.startswith("---"):
        block = text.split("---", 2)[1] if text.count("---") >= 2 else ""
        lines = block.splitlines()
        for index, line in enumerate(lines):
            if not line.startswith("tags:"):
                continue
            inline = line.split(":", 1)[1].strip()
            if inline.startswith("[") and inline.endswith("]"):
                tags.extend(
                    part.strip().strip("\"'") for part in inline[1:-1].split(",")
                )
            for follow in lines[index + 1 :]:
                match = _TAG_LIST_ITEM.match(follow)
                if not match:
                    break
                tags.append(match.group("tag").strip("\"'"))
    return fields, tuple(tag for tag in tags if tag), body


def _title_of(body: str, fallback: str) -> str:
    """First ``#`` heading, else the file stem."""
    match = _TITLE_RE.search(body)
    return match.group("title").strip() if match else fallback


def build_index(vault: Path | None = None) -> VaultIndex:
    """Walk the vault once and return the index.

    Args:
        vault: Vault root; defaults to :data:`DEFAULT_VAULT`.

    Returns:
        A :class:`VaultIndex`.  Unreadable notes are recorded as ``errors`` rather
        than raising, so one locked file cannot empty the page.
    """
    root = Path(vault) if vault is not None else DEFAULT_VAULT
    index = VaultIndex(root=root, as_of=as_of())
    if not root.is_dir():
        index.errors = (f"vault not found: {root}",)
        return index

    errors: list[str] = []
    for path in sorted(root.rglob("*.md")):
        parts = set(path.relative_to(root).parts[:-1])
        if parts & SKIPPED_DIRS:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        fields, tags, body = _frontmatter(text)
        rel = str(path.relative_to(root))
        folder = str(Path(rel).parent) if str(Path(rel).parent) != "." else ""
        stat = path.stat()
        note = Note(
            rel=rel,
            path=path,
            title=_title_of(body, path.stem),
            folder=folder,
            size=stat.st_size,
            mtime=stat.st_mtime,
            tags=tags,
            links=tuple(target.strip().lower() for target in _LINK_RE.findall(text)),
            frontmatter_keys=tuple(sorted(fields)),
            body=body,
        )
        for raw in _TASK_RE.finditer(body):
            text_of = raw.group("text").strip()
            if raw.group("state") == " ":
                note.open_tasks = (*note.open_tasks, text_of)
            else:
                note.done_tasks += 1
        index.notes[rel] = note
        index.by_title.setdefault(path.stem.lower(), rel)
        index.by_title.setdefault(note.title.lower(), rel)

    # backlinks need the whole vault, so they are resolved after the walk
    resolved: dict[str, list[str]] = {}
    for rel, note in index.notes.items():
        for target in note.links:
            destination = index.by_title.get(target)
            if destination and destination != rel:
                resolved.setdefault(destination, []).append(rel)
    for rel, sources in resolved.items():
        index.notes[rel].backlinks = tuple(sorted(set(sources)))

    index.errors = tuple(errors)
    return index


def kanban_cards(index: VaultIndex) -> list[KanbanCard]:
    """Parse the vault's Kanban board into cards.

    A board note's ``##`` headings are its columns and each top-level list item is a
    card; the tab-indented items under it are the card's fields (``**Task:**``,
    ``**Node:**``, ``**Context:**``, ``**Status:**``, ``**Notes:**``), which is the
    structure the board's own ``card_template`` declares.

    State comes from the card's ``Status`` field when it has one, because that is
    what the template asks for: the checkbox itself is often left unchecked on cards
    whose status says done, and reading the box alone would report every finished
    card as open.
    """
    for rel, note in index.notes.items():
        if "kanban" not in note.tags and "kanban-plugin" not in note.frontmatter_keys:
            continue
        cards: list[KanbanCard] = []
        column = "(no column)"
        current: KanbanCard | None = None
        for line in note.body.splitlines():
            heading = _HEADING_RE.match(line)
            if heading and len(heading.group("level")) <= 3:
                column = heading.group("title").strip()
                continue
            top = _CARD_RE.match(line)
            if top:
                state = top.group("state")
                current = KanbanCard(
                    column=column,
                    text=top.group("text").strip(),
                    note=rel,
                    checkbox_open=state == " ",
                )
                cards.append(current)
                continue
            field = _FIELD_RE.match(line)
            if field and current is not None:
                current.fields[field.group("key").strip().lower()] = field.group(
                    "value"
                ).strip()
        return cards
    return []


class KanbanCard:
    """One card on the board, with the fields its template defines."""

    def __init__(
        self,
        column: str,
        text: str,
        note: str,
        checkbox_open: bool,
    ) -> None:
        """Create a card; fields are filled in as the parser reads them."""
        self.column = column
        self.text = text
        self.note = note
        self.checkbox_open = checkbox_open
        self.fields: dict[str, str] = {}

    @property
    def status(self) -> str:
        """The card's status: its ``Status`` field, else the checkbox."""
        value = self.fields.get("status", "").strip().lower()
        if value:
            return value
        return "open" if self.checkbox_open else "done"

    @property
    def is_done(self) -> bool:
        """``True`` when the card reads as finished."""
        return self.status in {"done", "complete", "completed", "closed"}


def build_domain(vault: Path | None = None) -> Domain:
    """Build the vault domain.

    Args:
        vault: Vault root; ``None`` uses :data:`DEFAULT_VAULT`.

    Returns:
        A :class:`~hermes.portal.model.Domain`.  The index is built on first use
        (one pass over the notes) and reused afterwards.
    """
    state: dict[str, VaultIndex] = {}
    lock = threading.Lock()

    def index() -> VaultIndex:
        with lock:
            if "index" not in state:
                state["index"] = build_index(vault)
            return state["index"]

    def _sources(current: VaultIndex) -> tuple[Source, ...]:
        return (path_source("vault", current.root, note=f"{len(current.notes)} notes"),)

    def _notes(current: VaultIndex) -> list[Note]:
        return sorted(current.notes.values(), key=lambda note: note.rel)

    def overview() -> Collection:
        """Headline: how much is in the vault and how tangled it is."""
        current = index()
        open_tasks = len(_open_tasks(current))
        done = sum(note.done_tasks for note in current.notes.values())
        links = sum(len(note.links) for note in current.notes.values())
        notes = _notes(current)
        return build_collection(
            "overview",
            "Vault",
            "The Obsidian notes, with their links, tags and open tasks.",
            "markdown notes under the vault root",
            [
                Record(
                    id=note.rel,
                    title=note.title,
                    subtitle=f"{note.folder or '(root)'} · {human_size(note.size)} · "
                    f"{fmt_ago(note.mtime)}",
                    badges=(
                        f"{len(note.links)} links",
                        f"{len(note.backlinks)} backlinks",
                    )
                    + (("has open tasks",) if note.open_tasks else ()),
                    links=((f"/vault/{note.rel}", "Open note"),),
                )
                for note in sorted(notes, key=lambda n: -n.mtime)
            ],
            cap=RECENT_CAP,
            sources=_sources(current),
            extra_counts=(
                Count(len(current.notes), "notes indexed"),
                Count(links, "wiki links in those notes"),
                Count(open_tasks, "open items (see the Open tasks collection)"),
                Count(done, "checked boxes"),
            ),
            metrics=(
                ("Notes", str(len(current.notes))),
                ("Size", human_size(sum(note.size for note in current.notes.values()))),
                ("Open tasks", str(open_tasks)),
            ),
            notes=tuple(current.errors[:3])
            + (
                "the vault is indexed once per process, so edits need a restart to "
                "show up here",
            ),
            as_of=current.as_of,
        )

    def _kanban_collection(current: VaultIndex) -> Collection:
        """The board note, one record per card, with the card's own fields."""
        cards = kanban_cards(current)
        columns = Counter(card.column for card in cards)
        statuses = Counter(card.status for card in cards)
        open_cards = [card for card in cards if not card.is_done]
        return build_collection(
            "kanban",
            "Kanban board",
            "Every card on the board, with the column and the Status field its "
            "template asks for.",
            "cards (top-level list items) on the Kanban board note",
            [
                Record(
                    id=f"card-{index}",
                    title=card.text,
                    subtitle=f"{card.column} · {card.status}",
                    badges=(card.column, card.status)
                    + (
                        ("checkbox open",)
                        if card.checkbox_open and card.is_done
                        else ()
                    ),
                    links=((f"/vault/{card.note}", "Open board"),),
                    fields=(
                        ("column", card.column),
                        ("status", card.status),
                        ("checkbox", "open" if card.checkbox_open else "ticked"),
                        ("task", card.fields.get("task", "\u2014")),
                        ("node", card.fields.get("node", "\u2014")),
                        ("context", card.fields.get("context", "\u2014")),
                        ("notes", truncate(card.fields.get("notes", "\u2014"), 400)),
                    ),
                )
                for index, card in enumerate(cards)
            ],
            sources=_sources(current),
            extra_counts=(
                Count(len(open_cards), "cards not marked done"),
                Count(len(cards) - len(open_cards), "cards marked done"),
            )
            + tuple(
                Count(count, f"cards in {name}")
                for name, count in columns.most_common(6)
            )
            + tuple(
                Count(count, f"cards {status}")
                for status, count in statuses.most_common(4)
            ),
            notes=()
            if cards
            else ("no Kanban board note found in this vault",)
            + (
                "state comes from each card's Status field: the checkbox is often left "
                "unchecked on cards whose status says done",
            ),
            as_of=current.as_of,
        )

    def _open_tasks(current: VaultIndex) -> list[tuple[str, str, str, str]]:
        """Open work as ``(text, note_rel, badge, detail)`` tuples.

        Two conventions, because the vault has two: a checkbox line in an ordinary
        note is open while it is unchecked, and a Kanban card is open while its
        ``Status`` field is not done -- the board's checkbox is not maintained.
        """
        board = {card.note for card in kanban_cards(current)}
        found: list[tuple[str, str, str, str]] = []
        for note in _notes(current):
            if note.rel in board:
                continue
            for text in note.open_tasks:
                found.append((text, note.rel, "checkbox open", ""))
        for card in kanban_cards(current):
            if not card.is_done:
                found.append(
                    (card.text, card.note, card.status, f"column {card.column}")
                )
        return found

    def _tasks_collection(current: VaultIndex) -> Collection:
        """Open work across the whole vault, wherever it lives."""
        open_items = _open_tasks(current)
        done_boxes = sum(note.done_tasks for note in current.notes.values())
        done_cards = sum(1 for card in kanban_cards(current) if card.is_done)
        return build_collection(
            "tasks",
            "Open tasks",
            "Unchecked boxes in ordinary notes, plus Kanban cards whose Status is "
            "not done.",
            "open checkbox lines plus Kanban cards not marked done",
            [
                Record(
                    id=f"task-{index}",
                    title=text,
                    subtitle=note_rel if not detail else f"{note_rel} · {detail}",
                    badges=(badge, note_rel.split("/")[0] or "(root)"),
                    links=((f"/vault/{note_rel}", "Open note"),),
                )
                for index, (text, note_rel, badge, detail) in enumerate(open_items)
            ],
            cap=TASKS_CAP,
            sources=_sources(current),
            extra_counts=(
                Count(done_boxes, "checked boxes in ordinary notes"),
                Count(done_cards, "Kanban cards marked done"),
            ),
            notes=(
                "a Kanban card's checkbox is often unchecked while its Status field "
                "says done; the Status field wins here",
            ),
            as_of=current.as_of,
        )

    def _folders_collection(current: VaultIndex) -> Collection:
        """Top-level folders by note count."""
        folders = current.folders()
        return build_collection(
            "folders",
            "Folders",
            "Top-level folders, biggest first, counted recursively.",
            "distinct top-level folders",
            [
                Record(
                    id=name,
                    title=name,
                    subtitle=f"{count} note(s)",
                    badges=(f"{count} notes",),
                    href=f"/vault?folder={name}",
                )
                for name, count in folders
            ],
            sources=_sources(current),
            as_of=current.as_of,
        )

    def _hubs_collection(current: VaultIndex) -> Collection:
        """Notes with the most outbound links, and how many point back."""
        ordered = sorted(
            current.notes.values(), key=lambda note: (-len(note.links), note.rel)
        )
        return build_collection(
            "hubs",
            "Hub notes",
            "The notes other notes lean on: most links out, then most back.",
            "notes ranked by outbound wiki links",
            [
                Record(
                    id=note.rel,
                    title=note.title,
                    subtitle=note.rel,
                    badges=(f"{len(note.links)} out", f"{len(note.backlinks)} in"),
                    links=((f"/vault/{note.rel}", "Open note"),),
                )
                for note in ordered
                if note.links
            ],
            cap=HUBS_CAP,
            sources=_sources(current),
            as_of=current.as_of,
        )

    def _tags_collection(current: VaultIndex) -> Collection:
        """Frontmatter tags with their counts."""
        counter: Counter[str] = Counter()
        for note in current.notes.values():
            counter.update(note.tags)
        return build_collection(
            "tags",
            "Tags",
            "Tags declared in frontmatter, most used first.",
            "distinct frontmatter tags",
            [
                Record(
                    id=tag, title=tag, subtitle=f"{count} note(s)", badges=(f"{count}",)
                )
                for tag, count in counter.most_common()
            ],
            cap=TAGS_CAP,
            sources=_sources(current),
            notes=("only frontmatter tags are counted; inline #tags are not parsed",),
            as_of=current.as_of,
        )

    def collections(filters: Mapping[str, str] | None = None) -> Sequence[Collection]:
        """Drill-down collections for the vault.

        ``?folder=<name>`` narrows the note list to one top-level folder; the folder
        records link straight into it.
        """
        current = index()
        folder = ((filters or {}).get("folder") or "").strip() or None
        return [
            _kanban_collection(current),
            _tasks_collection(current),
            _folders_collection(current),
            _hubs_collection(current),
            _tags_collection(current),
            _notes_collection(current, folder),
        ]

    def _notes_collection(current: VaultIndex, folder: str | None = None) -> Collection:
        """Every note, optionally filtered to one top-level folder."""
        notes = [
            note
            for note in _notes(current)
            if folder is None or note.folder.split("/")[0] == folder
        ]
        return build_collection(
            "notes" if folder is None else f"folder-{folder}",
            "Notes" if folder is None else f"Notes in {folder}",
            "Every markdown note in the vault.",
            "markdown notes under the vault root"
            if folder is None
            else f"notes under {folder!r}",
            [
                Record(
                    id=note.rel,
                    title=note.title,
                    subtitle=note.rel,
                    badges=(
                        human_size(note.size),
                        f"{len(note.links)} links",
                        f"{len(note.backlinks)} in",
                    ),
                    links=((f"/vault/{note.rel}", "Open note"),),
                )
                for note in notes
            ],
            cap=NOTE_CAP,
            sources=_sources(current),
            as_of=current.as_of,
        )

    def detail(record_id: str) -> Record | None:
        """One note: its frontmatter, its size, and the note text itself."""
        current = index()
        note = current.notes.get(record_id)
        if note is None:
            return None
        return Record(
            id=note.rel,
            title=note.title,
            subtitle=note.rel,
            badges=(
                human_size(note.size),
                f"{len(note.links)} out",
                f"{len(note.backlinks)} in",
            ),
            fields=(
                ("path", str(note.path)),
                ("folder", note.folder or "(root)"),
                ("size", human_size(note.size)),
                ("modified", fmt_ago(note.mtime)),
                ("tags", ", ".join(note.tags) or "\u2014"),
                ("frontmatter keys", ", ".join(note.frontmatter_keys) or "\u2014"),
                ("links out", str(len(note.links))),
                ("backlinks", str(len(note.backlinks))),
                ("open tasks", str(len(note.open_tasks))),
                ("checked boxes", str(note.done_tasks)),
            ),
            links=(
                (
                    f"/vault?folder={note.folder.split('/')[0] or '(root)'}",
                    "Its folder",
                ),
            ),
            body=truncate(note.body, BODY_CAP),
        )

    def detail_sections(record_id: str) -> Sequence[Collection]:
        """Behind one note: what it links to, and what links back to it."""
        current = index()
        note = current.notes.get(record_id)
        if note is None:
            return []
        out_records = []
        for target in note.links:
            destination = current.by_title.get(target)
            out_records.append(
                Record(
                    id=f"out-{target}",
                    title=target,
                    subtitle=current.notes[destination].rel
                    if destination
                    else "not resolved",
                    badges=("resolved",) if destination else ("broken link",),
                    links=((f"/vault/{destination}", "Open note"),)
                    if destination
                    else (),
                )
            )
        back_records = [
            Record(
                id=f"in-{source}",
                title=current.notes[source].title,
                subtitle=source,
                badges=("links here",),
                links=((f"/vault/{source}", "Open note"),),
            )
            for source in note.backlinks
        ]
        return [
            build_collection(
                "links-out",
                "Links out",
                "Wiki links in this note, resolved against the vault.",
                "wiki links in this note",
                out_records,
                sources=_sources(current),
                as_of=current.as_of,
            ),
            build_collection(
                "backlinks",
                "Backlinks",
                "Notes that link to this one.",
                "notes linking here",
                back_records,
                sources=_sources(current),
                as_of=current.as_of,
            ),
        ]

    def search(needle: str, limit: int) -> Sequence[Record]:
        """Substring search over titles, tags and note text."""
        term = needle.strip()
        if not term:
            return []
        lowered = term.lower()
        hits: list[Record] = []
        for note in _notes(index()):
            where = ""
            excerpt = ""
            if lowered in note.title.lower():
                where = "title"
            elif any(lowered in tag.lower() for tag in note.tags):
                where = "tag"
            elif lowered in note.body.lower():
                where = "body"
                position = note.body.lower().find(lowered)
                excerpt = note.body[max(0, position - 60) : position + SEARCH_SNIPPET]
            if where:
                hits.append(
                    Record(
                        id=note.rel,
                        title=note.title,
                        subtitle=snippet(excerpt or note.rel, 160),
                        badges=(where, note.folder.split("/")[0] or "(root)"),
                        links=((f"/vault/{note.rel}", "Open note"),),
                    )
                )
            if len(hits) >= limit:
                break
        return hits

    return Domain(
        key="vault",
        title="Vault",
        summary="Obsidian notes with their links, backlinks, tags and open tasks.",
        overview=overview,
        collections=collections,
        detail=detail,
        search=search,
        detail_sections=detail_sections,
    )
