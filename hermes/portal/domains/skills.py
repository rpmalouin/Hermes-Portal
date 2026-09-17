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

from ...core import skill_trees
from ..model import (
    Collection,
    Count,
    Domain,
    Picker,
    Record,
    Source,
    build_collection,
)
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
from .base import SnapshotDomain

SKILLS_CAP = 100
REFERENCES_CAP = 50
BODY_CAP = 4000


@dataclass
class _Snapshot:
    """What one walk of every skill root found, deduped by name."""

    roots: list[skill_trees.SkillRoot]
    by_name: dict[str, skill_trees.Card]
    paths_total: int
    duplicates_dropped: int
    per_root: list[tuple[str, Path, int, bool]]  # label, path, found, present


def _snapshot(
    hermes_home: Path | None = None,
    profile: str | None = None,
    all_profiles: bool = False,
) -> _Snapshot:
    """Discover every skill once, keeping the counts needed to be honest."""
    shared_root = hermes_root(hermes_home) / "skills"
    roots = [
        skill_trees.SkillRoot(
            "shared skills root"
            if skill_root.path == shared_root
            else skill_root.label,
            skill_root.path,
        )
        for skill_root in skill_trees.resolve_hermes_roots(
            hermes_home=hermes_home, profile=profile, all_profiles=all_profiles
        )
    ]
    found: list[skill_trees.Card] = []
    per_root: list[tuple[str, Path, int, bool]] = []
    for root in roots:
        cards = skill_trees.discover_skills(root.path, root.label)
        per_root.append((root.label, root.path, len(cards), root.path.is_dir()))
        found.extend(cards)

    kept, dropped = skill_trees.dedupe_cards(found)
    return _Snapshot(
        roots=roots,
        by_name={card.name: card for card in kept},
        paths_total=len(found),
        duplicates_dropped=dropped,
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
    """Every unique skill as a record, optionally filtered to a box or category.

    The match uses :func:`hermes.core.skill_trees.filter_cards`, the same rule the
    Skill Deck used, so ``?box=creative`` takes the whole box and
    ``?box=mlops/evaluation`` narrows to one branch -- one filter, one meaning.
    """
    cards = list(snapshot.by_name.values())
    if box:
        cards, _hidden = skill_trees.filter_cards(cards, [box])
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


def skills_picker(snapshot: _Snapshot, selected: str = "") -> Picker:
    """The box dropdown: every box with its count, whatever is filtered now.

    Options come from the *unfiltered* inventory, so a box stays reachable after
    another one has been applied -- the trap that made the old deck need a
    separate ``inventory`` field in its page data.
    """
    return Picker(
        query_key="box",
        label="Box",
        options=tuple((name, f"{name} ({count})") for name, count in _boxes(snapshot)),
        all_label=f"All boxes ({len(snapshot.by_name)})",
        selected=selected,
    )


def _skills_collection(snapshot: _Snapshot, box: str | None = None) -> Collection:
    """Every unique skill, optionally filtered to a box or category path.

    This is the gallery: ``display="cards"`` asks the renderer for the card grid,
    and the picker travels with the collection so the page can re-filter itself.
    """
    definition = (
        "unique frontmatter names across all roots"
        if box is None
        else f"unique frontmatter names matching {box!r}"
    )
    records = _skill_records(snapshot, box)
    notes: tuple[str, ...] = ()
    if box and not records:
        notes = (
            f"nothing matches {box!r}: it is neither a box nor a category path. "
            "The Boxes collection lists what exists.",
        )
    extra = (
        Count(len(snapshot.by_name), "unique names across all roots (unfiltered)"),
        Count(
            snapshot.paths_total,
            "SKILL.md files on disk (symlinks followed, same skill once per root)",
        ),
        Count(len(_boxes(snapshot)), "boxes (first path component)"),
        Count(snapshot.duplicates_dropped, "duplicates dropped (same name or path)"),
    )
    if box:
        extra = extra[:2]
    return build_collection(
        "skills" if box is None else f"box-{box}",
        "Skills" if box is None else f"Skills matching {box}",
        "Every skill the running Hermes has, deduped by frontmatter name. "
        "Open one for its frontmatter and reference files.",
        definition,
        records,
        cap=SKILLS_CAP,
        sources=_sources(snapshot),
        extra_counts=extra,
        notes=notes,
        as_of=as_of(),
        display="cards",
        picker=skills_picker(snapshot, selected=box or ""),
    )


class SkillsDomain(SnapshotDomain[None]):
    """Every skill document the running agent can invoke.

    A class rather than a factory of closures, so every helper below can be called by
    name -- by a test, by a reader, and by the code graph.
    """

    key = "skills"
    title = "Skills"
    summary = "Every SKILL.md the running agent has: boxes, files, frontmatter."

    def __init__(
        self,
        hermes_home: Path | None = None,
        profile: str | None = None,
        all_profiles: bool = False,
    ) -> None:
        """Point at the sources; nothing is read until a page asks.

        Args:
            hermes_home: Hermes home or profile directory.
            profile: Named profile to read skills from.
            all_profiles: Also read every profile under the Hermes home.
        """
        super().__init__(hermes_home)
        self.profile = profile
        self.all_profiles = all_profiles
        # the factory built this eagerly; the name is ``index`` because ``snapshot`` is
        # the base class's method for the same thing
        self.index = _snapshot(
            hermes_home=self.hermes_home,
            profile=self.profile,
            all_profiles=self.all_profiles,
        )

    def overview(self) -> Collection:
        """Headline collection: the skill count, and the counts that differ from it."""
        return build_collection(
            "overview",
            "Skills",
            "Everything the running Hermes agent can invoke, as documents.",
            "unique frontmatter names across all roots",
            _skill_records(self.index),
            cap=5,
            sources=_sources(self.index),
            extra_counts=(
                Count(
                    self.index.paths_total, "SKILL.md files on disk (symlinks followed)"
                ),
                Count(len(_boxes(self.index)), "boxes (first path component)"),
                Count(len(self.index.per_root), "skill roots scanned"),
            ),
            as_of=as_of(),
            notes=(
                f"{len(self.index.by_name)} unique skills across "
                f"{len(self.index.per_root)} roots; the Skills collection lists them "
                "all.",
            ),
        )

    def collections(
        self, filters: Mapping[str, str] | None = None
    ) -> Sequence[Collection]:
        """Drill-down collections: roots, boxes, and every skill.

        ``?box=<name>`` narrows the skill list to one box; the box records link
        straight into it.
        """
        box = ((filters or {}).get("box") or "").strip()
        narrow = box or None
        return [
            _roots_collection(self.index),
            _boxes_collection(self.index),
            _skills_collection(self.index, narrow),
        ]

    def detail(self, record_id: str) -> Record | None:
        """One skill or one root: manifest fields, file facts and the document."""
        for label, path, found, present in self.index.per_root:
            if label == record_id and record_id not in self.index.by_name:
                return Record(
                    id=label,
                    title=label,
                    subtitle=str(path),
                    badges=("present" if present else "MISSING", f"{found} file(s)"),
                    fields=(
                        ("root", str(path)),
                        ("SKILL.md files", str(found)),
                        ("present", "yes" if present else "no"),
                        ("roots scanned", str(len(self.index.per_root))),
                    ),
                    links=(("/skills", "All skills"),),
                )
        card = self.index.by_name.get(record_id)
        if card is None:
            return None
        path = Path(card.path)
        text, truncated, error = read_text(path, BODY_CAP)
        fields, body = skill_trees.split_frontmatter(text) if text else ({}, "")
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

    def detail_sections(self, record_id: str) -> Sequence[Collection]:
        """Behind one skill: the frontmatter that was parsed, and its references."""
        card = self.index.by_name.get(record_id)
        if card is None:
            return []
        path = Path(card.path)
        text, _truncated, _error = read_text(path, BODY_CAP * 4)
        fields, _body = skill_trees.split_frontmatter(text)

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

    def search(self, query: str, limit: int) -> Sequence[Record]:
        """Case-insensitive substring search over skill metadata."""
        needle = query.strip().lower()
        if not needle:
            return []
        hits = []
        for card in sorted(self.index.by_name.values(), key=lambda c: c.name):
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


def build_domain(
    hermes_home: Path | None = None,
    profile: str | None = None,
    all_profiles: bool = False,
) -> Domain:
    """Build the skills domain.

    Returns:
        A :class:`~hermes.portal.model.Domain`.
    """
    return SkillsDomain(
        hermes_home=hermes_home, profile=profile, all_profiles=all_profiles
    ).domain()
