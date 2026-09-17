"""The memory file format, and the older copies of it.

Two concerns live here because both are about the files themselves rather than about
serving a page: the primitives the format is made of -- an entry, the section sign
between entries, the ``MEMORY``/``USER`` names, the archive limits -- and the readers
for older copies, including the comparison between an old copy and today's file.

Splitting this out of ``memory.py`` is what the code graph suggested: that file had
grown past 1,300 lines with a 793-line factory in it, and the history code had no home
of its own.  Everything here is pure -- paths and text in, dataclasses out -- so it is
testable without a portal, a registry or a server.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..sources import read_text, truncate

KINDS = ("memory", "user")
KIND_LABELS = {"memory": "MEMORY.md", "user": "USER.md"}
LIMIT_KEYS = {"memory": "memory_char_limit", "user": "user_char_limit"}
ENTRY_SEPARATOR = "\u00a7"
OVERLAP_FLOOR = 0.4
MEMBER_RE = re.compile(r"(?:^|/)(MEMORY|USER)\.md$")
MEMBER_KIND = {"MEMORY": "memory", "USER": "user"}
MAX_MEMBER_BYTES = 1_048_576
SNAPSHOT_MANIFEST = "manifest.json"
ENTRIES_CAP = 400
BODY_CAP = 8000
TITLE_CHARS = 92
NEIGHBOURS = 12


@dataclass(frozen=True)
class MemoryEntry:
    """One entry: the unit the agent writes and the limit counts."""

    profile: str
    kind: str
    file_key: str
    index: int
    text: str

    @property
    def key(self) -> str:
        """Stable id for this entry, used in URLs."""
        return f"{self.file_key}/{self.index}"

    @property
    def chars(self) -> int:
        """Characters in the entry, separators excluded."""
        return len(self.text)

    @property
    def title(self) -> str:
        """The entry's first line, as the list shows it."""
        for line in self.text.splitlines():
            cleaned = line.strip()
            if cleaned:
                return truncate(cleaned, TITLE_CHARS)
        return "(empty entry)"


@dataclass(frozen=True)
class ArchiveCopy:
    """One older copy of a memory file, from an archive or a snapshot directory."""

    key: str
    label: str
    when: str
    profile: str
    kind: str
    source: str
    path: Path
    member: str
    text: str
    entries: tuple[MemoryEntry, ...] = ()
    error: str = ""

    @property
    def label_text(self) -> str:
        """``MEMORY.md``-style name for the archived file."""
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def chars(self) -> int:
        """Characters in the archived copy."""
        return len(self.text)


def _profile_in(member: str) -> str:
    """The profile an archived member belongs to, from its path."""
    parts = [part for part in member.split("/") if part]
    if "memories" in parts:
        index = parts.index("memories")
        if index >= 2 and parts[index - 2] == "profiles":
            return parts[index - 1]
    return "default"


def _entries_for(profile: str, kind: str, text: str) -> tuple[MemoryEntry, ...]:
    """Split archived text into entries the same way the live files are split."""
    file_key = f"{profile}/{kind}"
    entries: list[MemoryEntry] = []
    for position, chunk in enumerate(text.split(ENTRY_SEPARATOR), start=1):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        entries.append(
            MemoryEntry(
                profile=profile,
                kind=kind,
                file_key=file_key,
                index=position,
                text=cleaned,
            )
        )
    return tuple(entries)


def _read_zip(path: Path, root: Path) -> tuple[list[ArchiveCopy], str]:
    """Read only the memory members of an archive; nothing is extracted to disk."""
    copies: list[ArchiveCopy] = []
    label = path.stem
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                match = MEMBER_RE.search(member)
                if not match:
                    continue
                kind = MEMBER_KIND[match.group(1)]
                profile = _profile_in(member)
                info = archive.getinfo(member)
                if info.file_size > MAX_MEMBER_BYTES:
                    copies.append(
                        ArchiveCopy(
                            key=f"history/{label}/{profile}/{kind}",
                            label=label,
                            when=f"{info.date_time[0]:04d}-{info.date_time[1]:02d}-"
                            f"{info.date_time[2]:02d}",
                            profile=profile,
                            kind=kind,
                            source=f"archive {path.name}",
                            path=path,
                            member=member,
                            text="",
                            error=f"member is {info.file_size:,} bytes, not read",
                        )
                    )
                    continue
                text = archive.read(member).decode("utf-8", "replace")
                copies.append(
                    ArchiveCopy(
                        key=f"history/{label}/{profile}/{kind}",
                        label=label,
                        when=f"{info.date_time[0]:04d}-{info.date_time[1]:02d}-"
                        f"{info.date_time[2]:02d}",
                        profile=profile,
                        kind=kind,
                        source=f"archive {path.name}",
                        path=path,
                        member=member,
                        text=text,
                        entries=_entries_for(profile, kind, text),
                    )
                )
    except (OSError, zipfile.BadZipFile, NotImplementedError) as exc:
        return [], f"{path.name}: {type(exc).__name__}: {exc}"
    _ = root
    return copies, ""


def _read_snapshot(directory: Path) -> tuple[list[ArchiveCopy], bool, str]:
    """Memory copies inside a snapshot directory, and whether its manifest kept any.

    The pre-update snapshots ship a ``manifest.json`` listing exactly what they hold --
    state.db, config.yaml, cron/ and a few databases, and deliberately **not** memories.
    Reading the manifest is how this reports that honestly instead of scanning a 126 MB
    directory for files that were never meant to be there.
    """
    manifest_path = directory / SNAPSHOT_MANIFEST
    holds_memories = False
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [], False, f"{directory.name}: unreadable manifest ({exc})"
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if isinstance(files, dict):
            holds_memories = any(
                isinstance(name, str) and MEMBER_RE.search(name) for name in files
            )
    copies: list[ArchiveCopy] = []
    for kind in KINDS:
        path = directory / "memories" / KIND_LABELS[kind]
        if not path.is_file():
            continue
        text, _truncated, error = read_text(path, limit=200_000)
        if error:
            continue
        copies.append(
            ArchiveCopy(
                key=f"history/{directory.name}/default/{kind}",
                label=directory.name,
                when=directory.name.split("-")[0],
                profile="default",
                kind=kind,
                source=f"snapshot {directory.name}",
                path=path,
                member=f"memories/{KIND_LABELS[kind]}",
                text=text,
                entries=_entries_for("default", kind, text),
            )
        )
    return copies, holds_memories, ""


def collect_history(root: Path) -> tuple[list[ArchiveCopy], list[str], list[str]]:
    """Every older copy of a memory file, plus notes about what was scanned.

    Returns ``(copies, notes, scanned)``: the archived memory files found, notes about
    containers that hold no memories, and the containers that were looked at.
    """
    copies: list[ArchiveCopy] = []
    notes: list[str] = []
    scanned: list[str] = []
    for archive_path in sorted((root / "backups").glob("*.zip")):
        scanned.append(archive_path.name)
        found, error = _read_zip(archive_path, root)
        copies.extend(found)
        if error:
            notes.append(error)
        elif not found:
            notes.append(f"{archive_path.name} holds no memory files")
    for snapshot_dir in sorted((root / "state-snapshots").glob("*")):
        if not snapshot_dir.is_dir():
            continue
        scanned.append(snapshot_dir.name)
        found, holds, error = _read_snapshot(snapshot_dir)
        copies.extend(found)
        if error:
            notes.append(error)
        elif not found and not holds:
            notes.append(
                f"snapshot {snapshot_dir.name} keeps no memories by design "
                "(its manifest lists state.db, config.yaml and cron/, not memories/)"
            )
    copies.sort(key=lambda copy: copy.when, reverse=True)
    return copies, notes, scanned


def _overlap(left: str, right: str) -> float:
    """How much two entries have in common, as a word-set Jaccard ratio."""
    a = set(re.findall(r"[a-z0-9]+", left.lower()))
    b = set(re.findall(r"[a-z0-9]+", right.lower()))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def delta_against(
    old: tuple[MemoryEntry, ...], new: tuple[MemoryEntry, ...]
) -> dict[str, tuple[MemoryEntry, ...]]:
    """Compare an archived copy against the live file, entry by entry.

    Two passes, because one is not enough on real memory.  Entries are matched first by
    their **title line** (the first non-empty line, which is what the agent writes first
    and what the lists show).  Whatever is left unmatched is then paired by *word
    overlap*, best partner first, above :data:`OVERLAP_FLOOR`.

    The second pass catches an entry that was rewritten enough to lose its opening line:
    under title matching alone it reads as a removal plus an addition.  Measured against
    this machine's own archive, it pairs the one entry edited in place (57% overlap) and
    leaves the rest alone -- which is right, because the rest really are different
    entries: that memory was consolidated in the three weeks since the copy.
    What remains unpaired after both passes is genuinely added or removed.
    """
    old_by_title: dict[str, MemoryEntry] = {}
    for entry in old:
        old_by_title.setdefault(entry.title, entry)
    new_by_title: dict[str, MemoryEntry] = {}
    for entry in new:
        new_by_title.setdefault(entry.title, entry)

    changed: list[MemoryEntry] = [
        new_by_title[title]
        for title in new_by_title
        if title in old_by_title
        and new_by_title[title].text != old_by_title[title].text
    ]
    old_left = [e for title, e in old_by_title.items() if title not in new_by_title]
    new_left = [e for title, e in new_by_title.items() if title not in old_by_title]

    taken: set[str] = set()
    pairs: list[tuple[MemoryEntry, MemoryEntry]] = []
    for older in old_left:
        best: tuple[float, MemoryEntry] | None = None
        for newer in new_left:
            if newer.key in taken:
                continue
            score = _overlap(older.text, newer.text)
            if score >= OVERLAP_FLOOR and (best is None or score > best[0]):
                best = (score, newer)
        if best is not None:
            taken.add(best[1].key)
            pairs.append((older, best[1]))

    paired_old = {older.key for older, _newer in pairs}
    changed.extend(newer for _older, newer in pairs)
    return {
        "added": tuple(entry for entry in new_left if entry.key not in taken),
        "removed": tuple(entry for entry in old_left if entry.key not in paired_old),
        "changed": tuple(changed),
    }
