"""Memory domain: what the agent has written down about itself and its user.

Hermes keeps a pair of files per profile -- ``MEMORY.md`` (the agent's own notes) and
``USER.md`` (who it is working with).  Entries are separated by a section sign, and each
file is capped by a character budget that ``config.yaml`` names: ``memory_char_limit``
and ``user_char_limit``.

The budget is why this domain exists.  A file sitting on its limit cannot take another
entry, so the useful question is not only "what does memory say" but "which file is
full".  Usage is computed against the real limit read from the config, and when the
config does not name one this reports characters and says so rather than inventing a
percentage.

Read-only, like every other source: these files belong to the agent, and they are the
one thing here that *is* routinely edited -- by Hermes, not by the portal.  Three
details that would otherwise mislead:

* ``*.lock`` files sit beside the memories and are skipped, then counted, so a reader
  knows they were seen and not forgotten;
* a running session loads its copy of memory when it starts, so the file on disk can be
  newer than what an agent is thinking with, and the page says so;
* history lives in :mod:`hermes.portal.domains.memory_files` -- this module serves the
  files as they are, that one knows what they were.

This is the first domain written as a class (:class:`MemoryDomain` over
:class:`~hermes.portal.domains.base.SnapshotDomain`) rather than as a factory full of
closures, because the graph measured the old shape at 793 lines with helpers that
nothing could call by name.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from ...core import skill_trees
from ..model import (
    Collection,
    Count,
    Domain,
    Picker,
    Record,
    Source,
    build_collection,
    detail_url,
)
from ..sources import (
    as_of,
    fmt_ago,
    hermes_root,
    path_source,
    read_text,
    snippet,
    truncate,
)
from .base import SnapshotDomain
from .memory_files import (
    ENTRY_SEPARATOR,
    KIND_LABELS,
    KINDS,
    ArchiveCopy,
    MemoryEntry,
    collect_history,
    delta_against,
)

LIMIT_KEYS = {"memory": "memory_char_limit", "user": "user_char_limit"}
ENTRIES_CAP = 400
BODY_CAP = 8000
TITLE_CHARS = 92
NEIGHBOURS = 12


@dataclass(frozen=True)
class MemoryFile:
    """One ``MEMORY.md`` or ``USER.md``, with its budget."""

    profile: str
    kind: str
    path: Path
    text: str
    entries: tuple[MemoryEntry, ...] = ()
    limit: int | None = None
    mtime: float | None = None
    error: str = ""

    @property
    def key(self) -> str:
        """Stable id for this file, used in URLs."""
        return f"{self.profile}/{self.kind}"

    @property
    def label(self) -> str:
        """``MEMORY.md``-style name for the file."""
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def chars(self) -> int:
        """Characters in the file, which is the unit the limit counts."""
        return len(self.text)

    @property
    def percent(self) -> float | None:
        """Share of the configured limit, or ``None`` when there is no limit."""
        if not self.limit:
            return None
        return 100.0 * self.chars / self.limit

    @property
    def free(self) -> int | None:
        """Characters left before the limit, or ``None`` without a limit."""
        if self.limit is None:
            return None
        return self.limit - self.chars

    @property
    def at_cap(self) -> bool:
        """``True`` when the file has reached its limit."""
        return self.percent is not None and self.percent >= 100.0


@dataclass(frozen=True)
class _Snapshot:
    """Everything the domain read, once per process."""

    files: tuple[MemoryFile, ...] = ()
    locks: int = 0
    limits: Mapping[str, int] = field(default_factory=dict)
    config_note: str = ""
    as_of: str = ""
    history: tuple[ArchiveCopy, ...] = ()
    history_notes: tuple[str, ...] = ()
    history_scanned: tuple[str, ...] = ()

    def file_for(self, profile: str, kind: str) -> MemoryFile | None:
        """The live file a profile/kind pair names, if it is still there."""
        return self.by_key().get(f"{profile}/{kind}")

    def narrowed(self, files: tuple[MemoryFile, ...]) -> _Snapshot:
        """A snapshot holding only *files*, with every other field carried over.

        Rebuilding this dataclass by hand lost the history fields once already: a
        ``?profile=`` view quietly showed no archived copies because the narrowing had
        defaulted them away.
        """
        return replace(self, files=files)

    def by_history_key(self) -> dict[str, ArchiveCopy]:
        """Archived copy key -> copy."""
        return {copy.key: copy for copy in self.history}

    def by_key(self) -> dict[str, MemoryFile]:
        """File key -> file."""
        return {memory_file.key: memory_file for memory_file in self.files}

    def entries(self) -> list[MemoryEntry]:
        """Every entry, in file order."""
        return [entry for memory_file in self.files for entry in memory_file.entries]


def _profile_label(home: Path) -> str:
    """Name the profile a memories directory belongs to."""
    return home.name if home.parent.name == "profiles" else "default"


def _memory_dirs(hermes_home: Path | None) -> list[tuple[str, Path]]:
    """Find every ``memories/`` directory, the running profile's first.

    Both shapes of ``$HERMES_HOME`` are handled: a Hermes root (``~/.hermes``) and a
    profile directory (``~/.hermes/profiles/<name>``), which is what a session sees.
    """
    home = (
        Path(hermes_home)
        if hermes_home is not None
        else skill_trees.default_hermes_home()
    )
    root = hermes_root(hermes_home)
    found: dict[Path, str] = {}
    # The root keeps the default profile's memories.  When $HERMES_HOME points at a
    # profile that store is a level up, and missing it would hide the default profile
    # entirely in a session -- which is exactly when this runs.
    for label, directory in (
        (_profile_label(root), root / "memories"),
        (_profile_label(home), home / "memories"),
    ):
        if directory.is_dir():
            found.setdefault(directory.resolve(), label)
    for profile_dir in skill_trees.profile_dirs(home):
        candidate = profile_dir / "memories"
        if candidate.is_dir():
            found.setdefault(candidate.resolve(), profile_dir.name)
    return sorted((label, path) for path, label in found.items())


def _limits(root: Path) -> tuple[dict[str, int], str]:
    """Read the character budgets from ``config.yaml``.

    A documented subset, not a YAML parser: the config names each budget once, as an
    integer, and that is all this needs.  Anything else is ignored rather than
    guessed, so a missing key means "no percentage" instead of a wrong one.
    """
    path = root / "config.yaml"
    text, _truncated, error = read_text(path, limit=400_000)
    if error:
        return {}, f"no limits read: {error}"
    limits: dict[str, int] = {}
    for kind in KINDS:
        key = LIMIT_KEYS[kind]
        match = re.search(rf"^\s*{re.escape(key)}\s*:\s*(\d+)\s*$", text, re.MULTILINE)
        if match:
            limits[kind] = int(match.group(1))
    missing = [LIMIT_KEYS[kind] for kind in KINDS if kind not in limits]
    if missing:
        return limits, f"config.yaml does not name {', '.join(missing)}"
    return limits, ""


def _read_file(
    label: str,
    kind: str,
    path: Path,
    limit: int | None,
) -> MemoryFile:
    """Read one memory file and split it into entries."""
    text, error = "", ""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raw, error = "", f"{type(exc).__name__}: {exc}"
    text = raw
    entries: list[MemoryEntry] = []
    file_key = f"{label}/{kind}"
    for position, chunk in enumerate(raw.split(ENTRY_SEPARATOR), start=1):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        entries.append(
            MemoryEntry(
                profile=label,
                kind=kind,
                file_key=file_key,
                index=position,
                text=cleaned,
            )
        )
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    return MemoryFile(
        profile=label,
        kind=kind,
        path=path,
        text=text,
        entries=tuple(entries),
        limit=limit,
        mtime=mtime,
        error=error,
    )


class MemoryDomain(SnapshotDomain["_Snapshot"]):
    """The memory files, their budgets, and the archives that hold older copies.

    ``read`` produces the snapshot every page is served from; the rest of the class is
    the callables the portal asks for plus the collection builders and record shapers
    they use -- named methods now, so a test can call one directly.
    """

    key = "memory"
    title = "Memory"
    summary = (
        "What the agent has written down about itself and its user, and how full each "
        "file is."
    )

    def read(self) -> _Snapshot:
        limits, config_note = _limits(hermes_root(self.hermes_home))
        files: list[MemoryFile] = []
        locks = 0
        for label, directory in _memory_dirs(self.hermes_home):
            locks += len(list(directory.glob("*.lock")))
            for kind in KINDS:
                path = directory / KIND_LABELS[kind]
                if not path.is_file():
                    continue
                files.append(_read_file(label, kind, path, limits.get(kind)))
        history, history_notes, history_scanned = collect_history(
            hermes_root(self.hermes_home)
        )
        return _Snapshot(
            files=tuple(files),
            locks=locks,
            limits=limits,
            config_note=config_note,
            as_of=as_of(),
            history=tuple(history),
            history_notes=tuple(history_notes),
            history_scanned=tuple(history_scanned),
        )

    def _sources(self, current: _Snapshot) -> tuple[Source, ...]:
        """The directories this domain looked in, whether or not they exist.

        Naming a missing directory is the point: every other collection in the portal
        says where it read from and marks an absent source, so a memory page on a
        machine with no memories says *which* directory it wanted rather than showing
        no sources at all.
        """
        home = (
            Path(self.hermes_home)
            if self.hermes_home is not None
            else skill_trees.default_hermes_home()
        )
        root = hermes_root(self.hermes_home)
        looked: list[tuple[str, Path]] = [(_profile_label(root), root / "memories")]
        if home != root:
            looked.append((_profile_label(home), home / "memories"))
        for profile_dir in skill_trees.profile_dirs(home):
            directory = profile_dir / "memories"
            if directory.is_dir():
                looked.append((profile_dir.name, directory))
        seen: set[Path] = set()
        sources_found: list[Source] = []
        for label, directory in looked:
            resolved = directory.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            here = sum(
                1 for item in current.files if item.path.parent.resolve() == resolved
            )
            sources_found.append(
                path_source(
                    f"{label} memories",
                    directory,
                    note=f"{here} file(s) read" if here else "no memory files here",
                )
            )
        return tuple(sources_found[:4])

    def _picker(self, selected: str) -> Picker:
        """A dropdown over the profiles that have memories."""
        current = self.snapshot()
        counter: Counter[str] = Counter(item.profile for item in current.files)
        return Picker(
            query_key="profile",
            label="Profile",
            options=tuple(
                (name, f"{name} ({count})") for name, count in sorted(counter.items())
            ),
            all_label=f"All profiles ({len(current.files)} files)",
            selected=selected,
        )

    def _files_collection(self, current: _Snapshot, selected: str = "") -> Collection:
        """One record per memory file, fullest first."""
        rows = sorted(
            current.files,
            key=lambda item: (
                -(item.percent if item.percent is not None else -1),
                item.key,
            ),
        )
        records = []
        for memory_file in rows:
            usage = (
                f"{memory_file.percent:.0f}% of {memory_file.limit:,}"
                if memory_file.percent is not None
                else "no limit configured"
            )
            badges = [memory_file.label, f"{len(memory_file.entries)} entry(s)", usage]
            if memory_file.at_cap:
                badges.append("at cap")
            if memory_file.error:
                badges.append("unreadable")
            subtitle = f"{memory_file.chars:,} characters"
            if memory_file.free is not None:
                subtitle += f" · {memory_file.free:,} free"
            subtitle += f" · {fmt_ago(memory_file.mtime)}"
            records.append(
                Record(
                    id=memory_file.key,
                    title=f"{memory_file.label} · {memory_file.profile}",
                    subtitle=subtitle,
                    badges=tuple(badges),
                    fields=(
                        ("profile", memory_file.profile),
                        ("file", str(memory_file.path)),
                        ("characters", f"{memory_file.chars:,}"),
                        (
                            "limit",
                            f"{memory_file.limit:,}" if memory_file.limit else "\u2014",
                        ),
                        (
                            "free",
                            f"{memory_file.free:,}"
                            if memory_file.free is not None
                            else "\u2014",
                        ),
                        ("entries", str(len(memory_file.entries))),
                        ("modified", fmt_ago(memory_file.mtime)),
                    ),
                )
            )
        at_cap = sum(1 for item in current.files if item.at_cap)
        notes = []
        if current.config_note:
            notes.append(current.config_note)
        if at_cap:
            notes.append(
                f"{at_cap} file(s) are at their limit: a new entry replaces another"
            )
        if current.locks:
            notes.append(
                f"{current.locks} *.lock file(s) sit beside the memories and are not"
            )
        if current.files:
            notes.append(
                "the limit applies to each file on its own, not to the totals below"
            )
        if selected and not current.files:
            notes.append(f"no memories directory found for profile {selected!r}")
        return build_collection(
            "files",
            "Memory files",
            "Every MEMORY.md and USER.md, fullest first, with usage against the "
            "budget config.yaml sets for it.",
            "memory files across the profiles found",
            records,
            sources=self._sources(current),
            picker=self._picker(selected),
            extra_counts=(
                Count(len(current.entries()), "entries across those files"),
                Count(at_cap, "files at or over their limit"),
                Count(current.locks, "lock files skipped"),
            ),
            notes=tuple(notes),
            as_of=current.as_of,
        )

    def _history_collection(self, current: _Snapshot) -> Collection:
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
                        ("entries now", str(len(live.entries)) if live else "\u2014"),
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
            sources=self._sources(current),
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

    def _entries_collection(self, current: _Snapshot) -> Collection:
        """One record per entry: the unit the agent writes."""
        records = [
            Record(
                id=entry.key,
                title=entry.title,
                subtitle=f"{entry.profile} · {KIND_LABELS[entry.kind]} · entry "
                f"{entry.index} · {entry.chars:,} chars",
                badges=(entry.profile, KIND_LABELS[entry.kind]),
                fields=(
                    ("profile", entry.profile),
                    ("file", KIND_LABELS[entry.kind]),
                    ("position", str(entry.index)),
                    ("characters", f"{entry.chars:,}"),
                ),
            )
            for entry in current.entries()
        ]
        return build_collection(
            "entries",
            "Entries",
            "Every entry, in file order: what Hermes has written down, in its own "
            "words.",
            f"entries separated by {ENTRY_SEPARATOR} in {len(current.files)} file(s)",
            records,
            cap=ENTRIES_CAP,
            sources=self._sources(current),
            notes=(
                "a session loads its copy of memory when it starts, so a file can be "
                "newer than what a running agent is thinking with",
            ),
            as_of=current.as_of,
        )

    def _kinds_collection(self, current: _Snapshot) -> Collection:
        """MEMORY.md versus USER.md, across profiles."""
        records = []
        for kind in KINDS:
            group = [item for item in current.files if item.kind == kind]
            if not group:
                continue
            limits = {item.limit for item in group}
            limit = limits.pop() if len(limits) == 1 else None
            entries = sum(len(item.entries) for item in group)
            fullest = max(
                (item.percent or 0.0 for item in group),
                default=0.0,
            )
            records.append(
                Record(
                    id=kind,
                    title=KIND_LABELS[kind],
                    subtitle=f"{len(group)} file(s) · {entries} entry(s) · fullest "
                    f"{fullest:.0f}% of its limit",
                    badges=(f"{len(group)} profiles", f"{entries} entries"),
                    fields=(
                        ("kind", kind),
                        ("profiles", str(len(group))),
                        ("entries", str(entries)),
                        ("limit", f"{limit:,}" if limit else "\u2014"),
                        ("fullest", f"{fullest:.0f}%"),
                    ),
                )
            )
        return build_collection(
            "kinds",
            "What kinds of memory",
            "The two files Hermes keeps, compared across profiles.",
            "kinds of memory file (MEMORY.md and USER.md)",
            records,
            sources=self._sources(current),
            as_of=current.as_of,
        )

    def collections(
        self, filters: Mapping[str, str] | None = None
    ) -> Sequence[Collection]:
        """Drill-down collections.  ``?profile=`` narrows to one profile.

        The filter is honoured strictly: a profile with no memories shows nothing and
        says so, rather than quietly showing every profile's.
        """
        current = self.snapshot()
        wanted = ((filters or {}).get("profile") or "").strip()
        if wanted:
            current = current.narrowed(
                tuple(item for item in current.files if item.profile == wanted)
            )
        files_collection = self._files_collection(current, wanted)
        return [
            files_collection,
            self._entries_collection(current),
            self._history_collection(current),
            self._kinds_collection(current),
        ]

    def overview(self) -> Collection:
        """Headline: how much is written down, and how full it is."""
        current = self.snapshot()
        entries = current.entries()
        at_cap = sum(1 for item in current.files if item.at_cap)
        fullest = max((item.percent or 0.0 for item in current.files), default=0.0)
        counts = [
            Count(len(current.files), "files read"),
            Count(len(entries), "entries"),
            Count(sum(item.chars for item in current.files), "characters in total"),
            Count(current.locks, "lock files skipped"),
            Count(len(current.history), "archived copies of a memory file"),
            Count(len(current.history_scanned), "archives and snapshots looked in"),
        ]
        notes = []
        if current.config_note:
            notes.append(current.config_note)
        if not current.files:
            notes.append("no memories directory found under the Hermes home")
        return build_collection(
            "overview",
            "Memory",
            "What the agent has written down about itself and its user: MEMORY.md "
            "and USER.md per profile, with the budget each is measured against.",
            "memory files (MEMORY.md and USER.md, one pair per profile)",
            [
                Record(
                    id=item.key,
                    title=f"{item.label} · {item.profile}",
                    subtitle=f"{item.chars:,} chars · {len(item.entries)} entry(s)",
                    badges=(item.label, f"{len(item.entries)} entries"),
                )
                for item in current.files
            ],
            cap=5,
            sources=self._sources(current),
            extra_counts=tuple(counts),
            metrics=(
                ("Files", str(len(current.files))),
                ("Entries", str(len(entries))),
                ("Fullest", f"{fullest:.0f}%"),
                ("At cap", str(at_cap)),
            ),
            notes=tuple(notes),
            as_of=current.as_of,
        )

    def detail(self, record_id: str) -> Record | None:
        """One file, one entry, or one archived copy."""
        current = self.snapshot()
        if record_id.startswith("history/"):
            return self._archive_record(current, record_id)
        parts = record_id.split("/")
        if len(parts) >= 3 and parts[-1].isdigit():
            return self._entry_record(current, "/".join(parts[:-1]), int(parts[-1]))
        memory_file = current.by_key().get(record_id)
        if memory_file is None:
            return None
        return self._file_record(memory_file)

    def _file_record(self, memory_file: MemoryFile) -> Record:
        """The record for a whole memory file."""
        return Record(
            id=memory_file.key,
            title=f"{memory_file.label} · {memory_file.profile}",
            subtitle=f"{memory_file.chars:,} characters in "
            f"{len(memory_file.entries)} entry(s)",
            badges=(
                memory_file.label,
                f"{len(memory_file.entries)} entries",
                (
                    f"{memory_file.percent:.0f}% of limit"
                    if memory_file.percent is not None
                    else "no limit configured"
                ),
            )
            + (("at cap",) if memory_file.at_cap else ())
            + (("unreadable",) if memory_file.error else ()),
            fields=(
                ("profile", memory_file.profile),
                ("path", str(memory_file.path)),
                ("characters", f"{memory_file.chars:,}"),
                (
                    "limit",
                    f"{memory_file.limit:,}" if memory_file.limit else "\u2014",
                ),
                (
                    "free",
                    f"{memory_file.free:,}"
                    if memory_file.free is not None
                    else "\u2014",
                ),
                ("entries", str(len(memory_file.entries))),
                ("modified", fmt_ago(memory_file.mtime)),
            ),
            links=(("/memory", "All memory"),),
            body=truncate(memory_file.text, BODY_CAP),
        )

    def _archive_record(self, current: _Snapshot, record_id: str) -> Record | None:
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
                ("member", copy.member or "\u2014"),
                ("characters then", f"{copy.chars:,}"),
                ("entries then", str(len(copy.entries))),
                ("characters now", f"{live.chars:,}" if live is not None else "\u2014"),
                (
                    "entries now",
                    str(len(live.entries)) if live is not None else "\u2014",
                ),
                ("added since", str(len(delta["added"]))),
                ("reworded since", str(len(delta["changed"]))),
                ("removed since", str(len(delta["removed"]))),
            ),
            links=(
                (detail_url("memory", f"{copy.profile}/{copy.kind}"), "Current file"),
            )
            if live is not None
            else (),
            body=truncate(copy.text, BODY_CAP) or "(nothing was archived)",
        )

    def _archived_rows(
        self,
        entries: Sequence[MemoryEntry],
        archive: ArchiveCopy,
        *,
        live_link: str,
    ) -> list[Record]:
        """Rows for archived entries: they link to the current file, never to a
        history id, because an archived entry has no page of its own.

        An archived entry's key looks like a live one (``profile/kind/index``) but its
        index refers to the archive, so linking it would open the wrong entry -- or 404.
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

    def _entry_record(
        self, current: _Snapshot, file_key: str, index: int
    ) -> Record | None:
        """The record for one entry."""
        memory_file = current.by_key().get(file_key)
        if memory_file is None:
            return None
        entry = next(
            (item for item in memory_file.entries if item.index == index),
            None,
        )
        if entry is None:
            return None
        other_profiles = [
            item
            for item in current.files
            if item.kind == entry.kind and item.profile != entry.profile
        ]
        return Record(
            id=entry.key,
            title=entry.title,
            subtitle=f"{entry.profile} · {KIND_LABELS[entry.kind]} · entry "
            f"{entry.index}",
            badges=(entry.profile, KIND_LABELS[entry.kind], f"{entry.chars:,} chars"),
            fields=(
                ("profile", entry.profile),
                ("file", KIND_LABELS[entry.kind]),
                ("position", f"{entry.index} of {len(memory_file.entries)}"),
                ("characters", f"{entry.chars:,}"),
                (
                    "memory file usage",
                    (
                        f"{memory_file.percent:.0f}% of {memory_file.limit:,}"
                        if memory_file.percent is not None
                        else "no limit configured"
                    ),
                ),
                ("file modified", fmt_ago(memory_file.mtime)),
                ("same file in profiles", str(len(other_profiles) + 1)),
            ),
            links=(
                (detail_url("memory", file_key), f"Open {KIND_LABELS[entry.kind]}"),
                ("/memory", "All memory"),
            ),
            body=truncate(entry.text, BODY_CAP),
        )

    def _archive_sections(
        self, current: _Snapshot, copy: ArchiveCopy
    ) -> Sequence[Collection]:
        """What the copy held, and the three ways entries have moved since."""
        live = current.file_for(copy.profile, copy.kind)
        live_link = (
            detail_url("memory", f"{copy.profile}/{copy.kind}")
            if live is not None
            else copy.key
        )
        # Collection is frozen, so the note has to be decided before it is built
        then_notes = (
            ("this file no longer exists, so nothing is compared",)
            if live is None
            else ()
        )
        sections = [
            build_collection(
                "then",
                f"Entries as they were ({copy.when})",
                "Everything this archived copy held, in order.",
                f"entries separated by {ENTRY_SEPARATOR} in the archived file",
                self._archived_rows(copy.entries, copy, live_link=live_link),
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
                self._archived_rows(delta["changed"], copy, live_link=live_link),
            ),
            (
                "removed",
                "Removed since",
                "Entries the copy had that the file has lost.",
                self._archived_rows(delta["removed"], copy, live_link=live_link),
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

    def detail_sections(self, record_id: str) -> Sequence[Collection]:
        """Behind a record: a file's entries, an entry's neighbours, or an
        archive's diff."""
        current = self.snapshot()
        if record_id.startswith("history/"):
            copy = current.by_history_key().get(record_id)
            return self._archive_sections(current, copy) if copy else []
        parts = record_id.split("/")
        if len(parts) >= 3 and parts[-1].isdigit():
            file_key = "/".join(parts[:-1])
            index = int(parts[-1])
            memory_file = current.by_key().get(file_key)
            if memory_file is None:
                return []
            neighbours = [
                item
                for item in memory_file.entries
                if abs(item.index - index) <= NEIGHBOURS and item.index != index
            ]
            if not neighbours:
                return []
            return [
                build_collection(
                    "neighbours",
                    f"Other entries in {memory_file.label}",
                    "The rest of the same file, either side of this one.",
                    f"entries in {memory_file.label} near entry {index}",
                    [self._entry_row(item) for item in neighbours],
                    notes=("a memory entry is written and pruned as a whole",),
                    as_of=current.as_of,
                )
            ]
        memory_file = current.by_key().get(record_id)
        if memory_file is None:
            return []
        sections = [
            build_collection(
                "entries",
                f"Entries in {memory_file.label}",
                "Every entry this file holds, in order.",
                f"entries separated by {ENTRY_SEPARATOR}",
                [self._entry_row(item) for item in memory_file.entries],
                as_of=current.as_of,
            )
        ]
        copies = [
            copy
            for copy in current.history
            if copy.profile == memory_file.profile and copy.kind == memory_file.kind
        ]
        if copies:
            sections.append(
                build_collection(
                    "history",
                    "Older copies of this file",
                    "Archived versions of this same file, newest first, each compared "
                    "with it as it stands today.",
                    "archived copies of this memory file",
                    [
                        Record(
                            id=copy.key,
                            title=f"{copy.when} · {copy.label_text}",
                            subtitle=f"from {copy.source} · {copy.chars:,} characters "
                            f"in {len(copy.entries)} entry(s)",
                            badges=(copy.source, f"{len(copy.entries)} entries then"),
                        )
                        for copy in copies
                    ],
                    notes=(
                        "entries are matched by their title line; the copy's page "
                        "shows what was added, reworded and removed since",
                        "what was added, reworded and removed since",
                    ),
                    as_of=current.as_of,
                )
            )
        siblings = [
            item
            for item in current.files
            if item.kind == memory_file.kind and item.profile != memory_file.profile
        ]
        if siblings:
            sections.append(
                build_collection(
                    "profiles",
                    f"{memory_file.label} in other profiles",
                    "The same file, kept separately by each profile.",
                    "memory files of the same kind in other profiles",
                    [
                        Record(
                            id=item.key,
                            title=f"{item.label} · {item.profile}",
                            subtitle=f"{item.chars:,} chars · "
                            f"{len(item.entries)} entry(s)",
                            badges=(f"{len(item.entries)} entries",),
                        )
                        for item in siblings
                    ],
                    as_of=current.as_of,
                )
            )
        return sections

    def _entry_row(self, entry: MemoryEntry) -> Record:
        """One entry as a list row (its own detail page is the full text)."""
        return Record(
            id=entry.key,
            title=entry.title,
            subtitle=f"entry {entry.index} · {entry.chars:,} chars",
            badges=(KIND_LABELS[entry.kind], entry.profile),
        )

    def search(self, query: str, limit: int) -> Sequence[Record]:
        """Find entries by their text; the file and profile name match too."""
        wanted = query.strip().lower()
        if not wanted:
            return []
        hits: list[Record] = []
        for entry in self.snapshot().entries():
            # offsets are taken from the entry text alone: searching a "title + text"
            # string shifts every match by the length of the title
            position = entry.text.lower().find(wanted)
            if position < 0:
                if wanted not in entry.title.lower():
                    continue
                position = 0
            excerpt = entry.text[max(0, position - 40) : position + 120]
            hits.append(
                Record(
                    id=entry.key,
                    title=entry.title,
                    subtitle=snippet(" ".join(excerpt.split()), 140),
                    badges=(entry.profile, KIND_LABELS[entry.kind]),
                )
            )
            if len(hits) >= limit:
                break
        return hits


def build_domain(hermes_home: Path | None = None) -> Domain:
    """Build the memory domain.

    Args:
        hermes_home: Hermes home or profile directory.

    Returns:
        A :class:`~hermes.portal.model.Domain`.  Files, archives and snapshots are read
        once, on first use, and reused afterwards, so a page reports the moment it read
        them.
    """
    return MemoryDomain(hermes_home).domain()
