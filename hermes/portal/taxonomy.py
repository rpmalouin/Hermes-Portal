"""A curated view of the skills boxes: groups, blurbs and tile colours.

The skills tree has 55 boxes (the first path component of a skill's category), and
**43 of them hold exactly one skill** -- most of those are individual workflow skills
that sit at the top level. A tile per box is therefore a wall of one-skill tiles, so
this module arranges the real boxes into a small number of groups: eight tiles instead
of fifty-five.

Two rules keep this honest, and they are the whole reason it is a module rather than a
list in the template:

* **This is presentation, not measurement.**  The skills domain counts; this only
  arranges.  A group's number is the sum of its member boxes' real counts, and every
  tile lists its members with their own counts, so a reader can see the arithmetic and
  disagree with the grouping without doubting the data.
* **Coverage is reported, never assumed.**  :func:`coverage` partitions the boxes it
  was given against the mapping, so a box that appears in the tree tomorrow -- a new
  skill, a new box -- shows up as *ungrouped* on the page instead of vanishing into a
  total.  Nothing here needs updating for that to be visible; the tile grid simply
  says so.

To regroup, edit :data:`GROUPS`: the keys are real box names, and a key that no box
matches is reported as *stale* rather than silently counting zero.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Group:
    """One tile: a name, a blurb, a colour ramp and the real boxes it collects."""

    key: str
    title: str
    blurb: str
    emoji: str
    boxes: tuple[str, ...]
    gradient: tuple[str, str] = field(default=("#2b3a63", "#4a6bb0"))


# Ordered: the tiles render in this order, most-crowded first where it matters.
GROUPS: tuple[Group, ...] = (
    Group(
        key="build",
        title="Build & ship",
        blurb="Writing code, testing it, packaging it and getting it merged.",
        emoji="\U0001f528",
        boxes=(
            "software-development",
            "github",
            "tdd",
            "implement",
            "prototype",
            "setup-pre-commit",
            "setup-ts-deep-modules",
            "migrate-to-shoehorn",
            "resolving-merge-conflicts",
            "git-guardrails-claude-code",
            "verified-delivery",
            "scaffold-exercises",
            "devops",
        ),
        gradient=("#7c3f10", "#c98f52"),
    ),
    Group(
        key="review",
        title="Review & design",
        blurb="Reading code closely: review, module boundaries, domain models, bugs.",
        emoji="\U0001f50d",
        boxes=(
            "code-review",
            "codebase-design",
            "domain-modeling",
            "diagnosing-bugs",
            "improve-codebase-architecture",
        ),
        gradient=("#1f3a5f", "#4a7bb0"),
    ),
    Group(
        key="agents",
        title="Agents & models",
        blurb="Delegating to coding agents, serving models, wiring MCP servers.",
        emoji="\U0001f916",
        boxes=("autonomous-ai-agents", "mlops", "hermes-mcp-setup"),
        gradient=("#3b2455", "#8a5bc4"),
    ),
    Group(
        key="research",
        title="Research & notes",
        blurb="Finding things out and keeping the result: papers, feeds, the vault.",
        emoji="\U0001f52c",
        boxes=(
            "research",
            "note-taking",
            "obsidian-vault",
            "hermes-skill-inventory",
            "web",
        ),
        gradient=("#0f3f3a", "#2f9e8f"),
    ),
    Group(
        key="creative",
        title="Creative & writing",
        blurb="Prose, diagrams, images and audio: making things, not fixing them.",
        emoji="\U0001f3a8",
        boxes=(
            "creative",
            "media",
            "writing-beats",
            "writing-for-agents",
            "writing-fragments",
            "writing-shape",
        ),
        gradient=("#7a1f3d", "#d0567f"),
    ),
    Group(
        key="communicate",
        title="Communicate",
        blurb="Mail, meetings, summaries and the social platforms.",
        emoji="\u2709\ufe0f",
        boxes=("productivity", "email", "social-media", "daily-summary"),
        gradient=("#1c4a6e", "#3f9fd0"),
    ),
    Group(
        key="home",
        title="Home & hardware",
        blurb="This machine and the ones around it: services, containers, lights.",
        emoji="\U0001f3e0",
        boxes=("homelab", "smart-home", "apple"),
        gradient=("#3d4a1c", "#8aa63f"),
    ),
    Group(
        key="process",
        title="Plan & decide",
        blurb="Grilling a plan, cutting tickets, handing off, agent workflows.",
        emoji="\U0001f9ed",
        boxes=(
            "ask-matt",
            "claude-handoff",
            "grill-me",
            "grill-with-docs",
            "grilling",
            "handoff",
            "loop-me",
            "setup-matt-pocock-skills",
            "teach",
            "to-questionnaire",
            "to-spec",
            "to-tickets",
            "triage",
            "wait-what",
            "wayfinder",
            "wizard",
        ),
        gradient=("#4a2f10", "#a5813f"),
    ),
)


@dataclass(frozen=True)
class GroupRow:
    """One tile's numbers, measured against the boxes that really exist."""

    group: Group
    present: tuple[tuple[str, int], ...]
    missing: tuple[str, ...]
    skills: int


@dataclass(frozen=True)
class Coverage:
    """What the mapping covers, and what it does not."""

    rows: tuple[GroupRow, ...]
    ungrouped: tuple[tuple[str, int], ...]
    boxes: int
    skills: int

    @property
    def covered_boxes(self) -> int:
        """How many boxes the mapping names and the tree has."""
        return self.boxes - len(self.ungrouped)

    @property
    def covered_skills(self) -> int:
        """How many skills sit inside a grouped box."""
        return self.skills - sum(count for _box, count in self.ungrouped)

    @property
    def stale(self) -> tuple[str, ...]:
        """Box names the mapping expects that no box matches."""
        return tuple(name for row in self.rows for name in row.missing)


def coverage(box_counts: Mapping[str, int]) -> Coverage:
    """Partition *box_counts* against :data:`GROUPS`.

    Args:
        box_counts: Real box name -> skill count, as the skills domain measured it.

    Returns:
        The tiles' numbers plus every box the mapping does not name.  Counts are
        summed, never invented: a group with no matching box reports zero skills and
        its missing names, so a rename in the tree is visible instead of quiet.
    """
    claimed: set[str] = set()
    rows: list[GroupRow] = []
    for group in GROUPS:
        present = tuple(
            (name, box_counts[name]) for name in group.boxes if name in box_counts
        )
        missing = tuple(name for name in group.boxes if name not in box_counts)
        claimed.update(name for name, _count in present)
        rows.append(
            GroupRow(
                group=group,
                present=present,
                missing=missing,
                skills=sum(count for _name, count in present),
            )
        )
    ungrouped = tuple(
        sorted(
            (
                (name, count)
                for name, count in box_counts.items()
                if name not in claimed
            ),
            key=lambda row: (-row[1], row[0]),
        )
    )
    return Coverage(
        rows=tuple(rows),
        ungrouped=ungrouped,
        boxes=len(box_counts),
        skills=sum(box_counts.values()),
    )
