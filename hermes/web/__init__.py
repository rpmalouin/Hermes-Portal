"""Web UI package for Hermes-Dashboard.

:mod:`hermes.web.deck` serves the Skill Deck -- this project's ``skill.json``
skills plus every skill the running Hermes agent has -- as a dark-themed card
grid at ``/`` and as JSON at ``/skills.json``.

Run it with::

    python -m hermes.web.deck
"""

from __future__ import annotations

__all__ = ["deck"]
