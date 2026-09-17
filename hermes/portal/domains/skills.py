"""Skills domain: every ``SKILL.md`` the running Hermes has, as documents.

The discovery itself is not reimplemented: this adapter calls the Skill Deck's
``discover_skills`` / ``resolve_hermes_roots`` / ``split_frontmatter``, which
already handle the traps that matter here (following directory symlinks, not
descending into a skill that contains a ``SKILL.md``, reading frontmatter as a
documented subset).

What it adds is the vocabulary the portal insists on: **competing counts**.  The
same tree can honestly be described as 157 unique names, 385 files or 421 paths
depending on how you dedupe, so this domain publishes the definitions next to
each other and lets the reader see the difference instead of trusting one number.
Skills are snapshotted once when the domain is built (one filesystem walk per
process), so a page view never re-walks 385 files.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ...web import skill_deck
from ..model import Collection, Count, Domain, Record, Source, build_collection
from ..sources import (
    as_of,
    fmt_ago,
    hermes_root,
    human_size,
    path_source,
    read_text,
    snippet,
    truncate,
)

SKILLS_CAP = 100
REFERENCES_CAP = 50
BODY_CAP = 4000


@dataclass
class _Snapshot:
    """What one walk of every skill root found, deduped by name."""

    roots: list[skill_deck.SkillRoot]
    by_name: dict[str, skill_deck.Card]
    paths_total: int
    duplicates_by_name: int
    duplicates_by_path: int
    per_root: list[tuple[str, Path, int, bool]]  # label, path, found, present


def _snapshot(
    hermes_home: Path | None = None,
    profile: str | None = None,
    all_profiles: bool = False,
) -> _Snapshot:
    """Discover every skill once, keeping the counts needed to be honest."""
    shared_root = hermes_root(hermes_home) / "skills"
    roots = [
        skill_deck.SkillRoot(
            "shared skills root"
            if skill_root.path == shared_root
            else skill_root.label,
            skill_root.path,
        )
        for skill_root in skill_deck.resolve_hermes_roots(
            hermes_home=hermes_home, profile=profile, all_profiles=all_profiles
        )
    ]
    by_name: dict[str, skill_deck.Card] = {}
    seen_paths: set[str] = set()
    paths_total = 0
    dup_name = dup_path = 0
    per_root: list[tuple[str, int, bool]] = []

    for root in roots:
        cards = skill_deck.discover_skills(root.path, root.label)
        per_root.append((root.label, root.path, len(cards), root.path.is_dir()))
        for card in cards:
            paths_total += 1
            if card.path in seen_paths:
                dup_path += 1
                continue
            seen_paths.add(card.path)
            if card.name in by_name:
                dup_name += 1
                continue
            by_name[card.name] = card

    return _Snapshot(
        roots=roots,
        by_name=by_name,
        paths_total=paths_total,
        duplicates_by_name=dup_name,
        duplicates_by_path=dup_path,
        per_root=per_root,
    )


def _sources(snapshot: _Snapshot) -> tuple[Source, ...]:
    """Describe every root that was walked."""
    sources: list[Source] = []
    for label, path, found, _present in snapshot.per_root:
        sources.append(path_source(label, path, note=f"{found} skill file(s)"))
    return tuple(sources)


def _boxes(snapshot: _Snapshot) -> list[tuple[str, int]]:
    """Box name -> skill count, biggest first."""
    counter = Counter(card.box for card in snapshot.by_name.values())
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))


def _roots_collection(snapshot: _Snapshot) -> Collection:
    """One record per skill root: where the skills came from."""
    records = []
    for label, path, found, present in snapshot.per_root:
        records.append(
            Record(
                id=label,
                title=label,
                subtitle=str(path),
                badges=("present" if present else "MISSING", f"{found} file(s)"),
                fields=(
                    ("root", str(path)),
                    ("SKILL.md files", str(found)),
                    ("present", "yes" if present else "no"),
                ),
            )
        )
    return build_collection(
        "roots",
        "Skill roots",
        "Every directory walked for SKILL.md files, in priority order.",
        "directories scanned for SKILL.md",
        records,
        sources=_sources(snapshot),
        as_of=as_of(),
        notes=(
            "The same skill is reachable through several roots; the name-deduped "
            "count below is what the portal treats as 'the skills'.",
        ),
    )


def _boxes_collection(snapshot: _Snapshot) -> Collection:
    """One record per box (first path component)."""
    boxes = _boxes(snapshot)
    singles = sum(1 for _name, count in boxes if count == 1)
    records = [
        Record(
            id=name,
            title=name,
            subtitle=f"{count} skill(s)",
            badges=(f"{count} skills",),
            href=f"/skills?box={name}",
            fields=(("box", name), ("skills", str(count))),
        )
        for name, count in boxes
    ]
    return build_collection(
        "boxes",
        "Boxes",
        "The first path component of each skill directory, biggest first.",
        "distinct first path components",
        records,
        sources=_sources(snapshot),
        extra_counts=(
            Count(singles, "boxes holding exactly one skill"),
            Count(len(boxes) - singles, "boxes holding more than one"),
        ),
        as_of=as_of(),
        notes=(
            "Boxes mix real categories (creative, productivity) with skills that "
            "sit at the root of a tree (tdd, triage), so a small box usually means "
            "an ungrouped skill rather than a thin category.",
        ),
    )


def _skill_records(snapshot: _Snapshot, box: str | None = None) -> list[Record]:
    """Every unique skill as a record, optionally filtered to one box."""
    cards = [
        card for card in snapshot.by_name.values() if box is None or card.box == box
    ]
    cards.sort(key=lambda card: (card.box, card.name))
    return [
        Record(
            id=card.name,
            title=card.title,
            subtitle=truncate(card.description, 150),
            badges=(card.box, card.category)
            if card.category != card.box
            else (card.box,),
            links=((f"/skills/{card.name}", "Details"),),
            group=card.box,
            fields=(
                ("name", card.name),
                ("box", card.box),
                ("category", card.category),
                ("source root", card.source),
                ("path", card.path),
            ),
        )
        for card in cards
    ]


def _skills_collection(snapshot: _Snapshot, box: str | None = None) -> Collection:
    """Every unique skill, optionally filtered to one box."""
    definition = (
        "unique frontmatter names across all roots"
        if box is None
        else f"unique frontmatter names in box {box!r}"
    )
    return build_collection(
        "skills" if box is None else f"box-{box}",
        "Skills" if box is None else f"Skills in {box}",
        "Every skill the running Hermes has, deduped by frontmatter name.",
        definition,
        _skill_records(snapshot, box),
        cap=SKILLS_CAP,
        sources=_sources(snapshot),
        extra_counts=(
            Count(len(snapshot.by_name), "unique names across all roots (unfiltered)"),
            Count(
                snapshot.paths_total,
                "SKILL.md files on disk (symlinks followed, same skill once per root)",
            ),
            Count(len(_boxes(snapshot)), "boxes (first path component)"),
        )
        if box is None
        else (
            Count(len(snapshot.by_name), "unique names across all roots (unfiltered)"),
            Count(snapshot.paths_total, "SKILL.md files on disk (symlinks followed)"),
        ),
        as_of=as_of(),
    )


def build_domain(
    hermes_home: Path | None = None,
    profile: str | None = None,
    all_profiles: bool = False,
) -> Domain:
    """Build the skills domain, snapshotting the filesystem once.

    Args:
        hermes_home: Hermes home or profile directory; ``None`` uses
            ``$HERMES_HOME`` then ``~/.hermes``.
        profile: Named profile to read skills from.
        all_profiles: Also read every profile under the Hermes home.

    Returns:
        A :class:`~hermes.portal.model.Domain` ready to register.
    """
    snapshot = _snapshot(
        hermes_home=hermes_home, profile=profile, all_profiles=all_profiles
    )

    def overview() -> Collection:
        """Headline collection: the skill count, and the counts that differ from it."""
        return build_collection(
            "overview",
            "Skills",
            "Everything the running Hermes agent can invoke, as documents.",
            "unique frontmatter names across all roots",
            _skill_records(snapshot),
            cap=5,
            sources=_sources(snapshot),
            extra_counts=(
                Count(
                    snapshot.paths_total, "SKILL.md files on disk (symlinks followed)"
                ),
                Count(len(_boxes(snapshot)), "boxes (first path component)"),
                Count(len(snapshot.per_root), "skill roots scanned"),
            ),
            as_of=as_of(),
            notes=(
                f"{len(snapshot.by_name)} unique skills across "
                f"{len(snapshot.per_root)} roots; the Skills collection lists them "
                "all.",
            ),
        )

    def collections(filters: Mapping[str, str] | None = None) -> Sequence[Collection]:
        """Drill-down collections: roots, boxes, and every skill.

        ``?box=<name>`` narrows the skill list to one box; the box records link
        straight into it.
        """
        box = ((filters or {}).get("box") or "").strip()
        known = {name for name, _count in _boxes(snapshot)}
        narrow = box if box in known else None
        return [
            _roots_collection(snapshot),
            _boxes_collection(snapshot),
            _skills_collection(snapshot, narrow),
        ]

    def detail(record_id: str) -> Record | None:
        """One skill or one root: manifest fields, file facts and the document."""
        for label, path, found, present in snapshot.per_root:
            if label == record_id and record_id not in snapshot.by_name:
                return Record(
                    id=label,
                    title=label,
                    subtitle=str(path),
                    badges=("present" if present else "MISSING", f"{found} file(s)"),
                    fields=(
                        ("root", str(path)),
                        ("SKILL.md files", str(found)),
                        ("present", "yes" if present else "no"),
                        ("roots scanned", str(len(snapshot.per_root))),
                    ),
                    links=(("/skills", "All skills"),),
                )
        card = snapshot.by_name.get(record_id)
        if card is None:
            return None
        path = Path(card.path)
        text, truncated, error = read_text(path, BODY_CAP)
        fields, body = skill_deck.split_frontmatter(text) if text else ({}, "")
        try:
            stat = path.stat()
            size, modified = human_size(stat.st_size), fmt_ago(stat.st_mtime)
        except OSError:
            size, modified = "unknown", "unknown"
        skill_dir = path.parent
        references = sorted(
            p for p in (skill_dir / "references").rglob("*") if p.is_file()
        )
        detail_fields = [
            ("name", card.name),
            ("title", card.title),
            ("box", card.box),
            ("category", card.category),
            ("source root", card.source),
            ("path", str(path)),
            ("size", size),
            ("modified", modified),
            ("frontmatter keys read", ", ".join(sorted(fields)) or "none"),
            ("references files", str(len(references))),
            ("body truncated", "yes" if truncated else "no"),
        ]
        notes = []
        if error:
            notes.append(error)
        if truncated:
            notes.append(f"showing the first {BODY_CAP} characters of SKILL.md")
        if not fields:
            notes.append(
                "no frontmatter fence found; the name falls back to the directory"
            )
        return Record(
            id=card.name,
            title=card.title,
            subtitle=truncate(card.description, 200),
            badges=(card.box, card.source),
            fields=tuple(detail_fields),
            links=(
                (f"/skills?box={card.box}", f"Other skills in {card.box}"),
                (f"/search?q={card.name}", "Search for this name"),
            ),
            body=body or text,
            group=card.box,
        )

    def detail_sections(record_id: str) -> Sequence[Collection]:
        """Behind one skill: the frontmatter that was parsed, and its references."""
        card = snapshot.by_name.get(record_id)
        if card is None:
            return []
        path = Path(card.path)
        text, _truncated, _error = read_text(path, BODY_CAP * 4)
        fields, _body = skill_deck.split_frontmatter(text)

        front = build_collection(
            "frontmatter",
            "Frontmatter as parsed",
            "Top-level scalars the loader reads; nested blocks are ignored by design.",
            "top-level scalar keys in SKILL.md frontmatter",
            [
                Record(
                    id=key,
                    title=key,
                    subtitle=snippet(value, 200) or "(nested block, not read)",
                )
                for key, value in sorted(fields.items())
            ],
            as_of=as_of(),
            notes=(
                "Nested mappings (metadata:), block scalars and lists are not read; "
                "unknown keys are skipped rather than guessed at.",
            ),
        )

        skill_dir = path.parent
        refs = sorted(p for p in (skill_dir / "references").rglob("*") if p.is_file())
        ref_records = []
        for ref in refs:
            try:
                size = human_size(ref.stat().st_size)
                modified = fmt_ago(ref.stat().st_mtime)
            except OSError:
                size, modified = "unknown", "unknown"
            ref_records.append(
                Record(
                    id=str(ref.relative_to(skill_dir)),
                    title=ref.name,
                    subtitle=str(ref.relative_to(skill_dir)),
                    badges=(size,),
                    fields=(("path", str(ref)), ("size", size), ("modified", modified)),
                )
            )
        references = build_collection(
            "references",
            "Reference files",
            "Documents shipped beside SKILL.md.",
            "files under the skill's references/ directory",
            ref_records,
            cap=REFERENCES_CAP,
            sources=(path_source("skill directory", skill_dir),),
            as_of=as_of(),
        )
        return [front, references]

    def search(query: str, limit: int) -> Sequence[Record]:
        """Case-insensitive substring search over skill metadata."""
        needle = query.strip().lower()
        if not needle:
            return []
        hits = []
        for card in sorted(snapshot.by_name.values(), key=lambda c: c.name):
            haystack = " ".join(
                (card.name, card.title, card.description, card.box, card.category)
            ).lower()
            if needle in haystack:
                hits.append(
                    Record(
                        id=card.name,
                        title=card.title,
                        subtitle=truncate(card.description, 120),
                        badges=("skill", card.box),
                        links=((f"/skills/{card.name}", "Open"),),
                    )
                )
            if len(hits) >= limit:
                break
        return hits

    return Domain(
        key="skills",
        title="Skills",
        summary="Every SKILL.md the running agent has: boxes, files, frontmatter.",
        overview=overview,
        collections=collections,
        detail=detail,
        search=search,
        detail_sections=detail_sections,
    )
