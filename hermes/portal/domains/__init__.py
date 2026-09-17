"""Domain adapters, and the registry that assembles them.

One module per domain.  A new domain is a new module here plus a line in
:func:`default_registry`; nothing else in the portal changes, because everything
downstream talks to :class:`~hermes.portal.model.Domain`.

The data layer each adapter needs already exists elsewhere and is imported rather
than copied: :mod:`hermes.core.skill_trees` for the skill trees (shared with
nothing now that the Skill Deck is retired, but still view-free on principle), and
:mod:`hermes.portal.sources` for read-only databases, files and formatting.
"""

from __future__ import annotations

from pathlib import Path

from ..model import DomainRegistry
from ..sources import hermes_root
from . import cron, sessions, skills

__all__ = ["cron", "default_registry", "sessions", "skills"]


def default_registry(
    hermes_home: Path | None = None,
    profile: str | None = None,
    all_profiles: bool = True,
) -> DomainRegistry:
    """Build the P0 registry: skills, sessions and cron.

    Args:
        hermes_home: Hermes home or profile directory; ``None`` resolves
            ``$HERMES_HOME`` then ``~/.hermes``.  The directory holding
            ``state.db`` and ``cron/`` is found from it either way.
        profile: Named profile to read skills from.
        all_profiles: Read every profile's skills too (default).  The portal is
            meant to show *everything* Hermes has, so per-profile trees are in
            by default and each one is listed as its own source.

    Returns:
        A registry with one domain per adapter.  The skills domain snapshots the
        filesystem once here (one walk per process); sessions and cron query their
        stores per request, so those pages are live.
    """
    root = hermes_root(hermes_home)
    registry = DomainRegistry()
    registry.register(
        skills.build_domain(
            hermes_home=root, profile=profile, all_profiles=all_profiles
        )
    )
    registry.register(sessions.build_domain(hermes_home=hermes_home))
    registry.register(cron.build_domain(hermes_home=hermes_home))
    return registry
