"""Older copies of the memory files: how they are found, compared and shown.

``memory.py`` serves the memory files as they are; ``memory_files.py`` knows what they
*were* and hands back the archived copies with the diff between then and today.  This
module is the presentation of that second concern: the collection of older copies, the
page for one copy, and the sections behind it.

It is separate because the two concerns had grown one class past 790 lines, and this is
the half that reads no live state -- it is handed a snapshot and returns records.  The
seam is a typed one: :class:`ArchiveView` names exactly the six things the archive needs
from a snapshot, so the module cannot quietly start depending on the rest of it.

A note on what an archived entry's id means: it looks like a live one
(``profile/kind/index``) but its index refers to the archive, so an archived row links
to the *current* file rather than to a history id -- linking the archive key would open
the wrong entry, or 404.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..model import Collection, Count, Record, Source, build_collection, detail_url
from ..sources import path_source, truncate
from .memory_files import (
    ENTRY_SEPARATOR,
    KIND_LABELS,
    ArchiveCopy,
    MemoryEntry,
    delta_against,
)

if TYPE_CHECKING:  # annotations only: importing memory for real would be a cycle
    from .memory import MemoryFile

#: How much of an archived file's text a page carries.
BODY_CAP = 8000


class ArchiveView(Protocol):
    """What the archive needs from a memory snapshot, and nothing more."""

    history: Sequence[ArchiveCopy]
    history_notes: Sequence[str]
    history_scanned: Sequence[Path]
    as_of: str

    def file_for(self, profile: str, kind: str) -> MemoryFile | None:
        """The live file a copy came from, or ``None`` when it is gone."""
        ...

    def by_history_key(self) -> dict[str, ArchiveCopy]:
        """Archived copies by their history id."""
        ...


def history_collection(current: ArchiveView, sources: Sequence[Source]) -> Collection:
    """Older copies of the memory files, newest first, with what changed since."""
    records = []
    for copy in current.history:
        live = current.file_for(copy.profile, copy.kind)
        delta = (
            delta_against(copy.entries, live.entries)
            if live is not None
            else {"added": (), "removed": (), "changed": ()}
        )
        moved = [
            f"+{len(delta['added'])}" if delta["added"] else "",
            f"-{len(delta['removed'])}" if delta["removed"] else "",
            f"~{len(delta['changed'])}" if delta["changed"] else "",
        ]
        badges = [copy.source, f"{len(copy.entries)} entry(s) then"]
        if live is None:
            badges.append("this file is gone")
        else:
            badges.append(
                "unchanged since"
                if not any(delta.values())
                else "changed: " + " ".join(bit for bit in moved if bit)
            )
        if copy.error:
            badges.append("not read")
        records.append(
            Record(
                id=copy.key,
                title=f"{copy.label_text} · {copy.profile} · {copy.when}",
                subtitle=f"{copy.chars:,} characters in "
                f"{len(copy.entries)} entry(s)"
                + (f" · {copy.error}" if copy.error else "")
                + ("" if live is None else f" · now {live.chars:,}"),
                badges=tuple(badges),
                fields=(
                    ("archived file", f"{copy.profile}/{KIND_LABELS[copy.kind]}"),
                    ("from", copy.source),
                    ("when", copy.when),
                    ("characters then", f"{copy.chars:,}"),
                    ("entries then", str(len(copy.entries))),
                    ("entries now", str(len(live.entries)) if live else "—"),
                    (
                        "characters now",
                        f"{live.chars:,}" if live is not None else "—",
                    ),
                    ("added since", str(len(delta["added"]))),
                    ("removed since", str(len(delta["removed"]))),
                    ("reworded since", str(len(delta["changed"]))),
                    ("member", copy.member or "—"),
                ),
                links=(
                    (
                        detail_url("memory", f"{copy.profile}/{copy.kind}"),
                        "Current file",
                    ),
                )
                if live is not None
                else (),
            )
        )
    notes = list(current.history_notes[:4])
    notes.append(
        "entries are matched by title, then by word overlap, so a reworded one "
        "usually reads as changed; the rest counts as added or removed"
    )
    if not current.history:
        notes.append(
            "no older copy holds memories: the pre-update snapshots keep state.db, "
            "config.yaml and cron/ by design, so memory history comes from the "
            "archives under <hermes root>/backups"
        )
    return build_collection(
        "snapshots",
        "Older copies",
        "Memory files as they were, from the archives and snapshots under the "
        "Hermes home, newest first, compared with the file today.",
        "archived copies of a memory file",
        records,
        sources=sources,
        extra_counts=(
            Count(len(current.history), "archived memory files found"),
            Count(len(current.history_scanned), "archives and snapshots looked in"),
            Count(
                sum(1 for copy in current.history if copy.error),
                "copies that could not be read",
            ),
        ),
        notes=tuple(notes),
        as_of=current.as_of,
    )


def archive_record(current: ArchiveView, record_id: str) -> Record | None:
    """One archived copy: what it held, and what has moved since."""
    copy = current.by_history_key().get(record_id)
    if copy is None:
        return None
    live = current.file_for(copy.profile, copy.kind)
    delta = (
        delta_against(copy.entries, live.entries)
        if live is not None
        else {"added": (), "removed": (), "changed": ()}
    )
    return Record(
        id=copy.key,
        title=f"{copy.label_text} · {copy.profile} · {copy.when}",
        subtitle=copy.text.strip().splitlines()[0][:120]
        if copy.text.strip()
        else "(empty)",
        badges=(copy.source, f"{len(copy.entries)} entry(s) then")
        + (("readable",) if not copy.error else ("not read",)),
        fields=(
            ("archived file", f"{copy.profile}/{KIND_LABELS[copy.kind]}"),
            ("from", copy.source),
            ("when", copy.when),
            ("member", copy.member or "—"),
            ("characters then", f"{copy.chars:,}"),
            ("entries then", str(len(copy.entries))),
            ("characters now", f"{live.chars:,}" if live is not None else "—"),
            (
                "entries now",
                str(len(live.entries)) if live is not None else "—",
            ),
            ("added since", str(len(delta["added"]))),
            ("reworded since", str(len(delta["changed"]))),
            ("removed since", str(len(delta["removed"]))),
        ),
        links=((detail_url("memory", f"{copy.profile}/{copy.kind}"), "Current file"),)
        if live is not None
        else (),
        body=truncate(copy.text, BODY_CAP) or "(nothing was archived)",
    )


def archive_sections(current: ArchiveView, copy: ArchiveCopy) -> Sequence[Collection]:
    """What the copy held, and the three ways entries have moved since."""
    live = current.file_for(copy.profile, copy.kind)
    live_link = (
        detail_url("memory", f"{copy.profile}/{copy.kind}")
        if live is not None
        else copy.key
    )
    # Collection is frozen, so the note has to be decided before it is built
    then_notes = (
        ("this file no longer exists, so nothing is compared",) if live is None else ()
    )
    sections = [
        build_collection(
            "then",
            f"Entries as they were ({copy.when})",
            "Everything this archived copy held, in order.",
            f"entries separated by {ENTRY_SEPARATOR} in the archived file",
            archived_rows(copy.entries, copy, live_link=live_link),
            sources=(path_source(f"archive {copy.label}", copy.path),),
            notes=then_notes,
            as_of=current.as_of,
        )
    ]
    if live is None:
        return sections
    delta = delta_against(copy.entries, live.entries)
    for key, title, description, rows in (
        (
            "added",
            "Added since",
            "Entries in the file today that the copy did not have.",
            [
                Record(
                    id=entry.key,
                    title=entry.title,
                    subtitle=f"{entry.chars:,} chars",
                    badges=(KIND_LABELS[entry.kind], "now"),
                )
                for entry in delta["added"]
            ],
        ),
        (
            "reworded",
            "Reworded since",
            "Entries whose opening line survived but whose text changed.",
            archived_rows(delta["changed"], copy, live_link=live_link),
        ),
        (
            "removed",
            "Removed since",
            "Entries the copy had that the file has lost.",
            archived_rows(delta["removed"], copy, live_link=live_link),
        ),
    ):
        if not rows:
            continue
        sections.append(
            build_collection(
                key,
                title,
                description,
                f"entries that changed between the archive and today ({key})",
                rows,
                sources=(path_source(f"archive {copy.label}", copy.path),),
                as_of=current.as_of,
            )
        )
    return sections


def archived_rows(
    entries: Sequence[MemoryEntry],
    archive: ArchiveCopy,
    *,
    live_link: str,
) -> list[Record]:
    """Rows for archived entries: they link to the current file, never to a
    history id, because an archived entry has no page of its own.
    """
    return [
        Record(
            id=f"{archive.key}/{entry.index}",
            title=entry.title,
            subtitle=f"was entry {entry.index} · {entry.chars:,} chars",
            badges=(KIND_LABELS[entry.kind],),
            href=live_link,
        )
        for entry in entries
    ]
