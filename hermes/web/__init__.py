"""Web UI package for Hermes-Dashboard.

One module per web view.  :mod:`hermes.web.skill_deck` serves the Skill Deck --
this project's ``skill.json`` skills plus every skill the running Hermes agent
has -- as a dark-themed card grid at ``/`` and as JSON at ``/skills.json``.

Run it with::

    python -m hermes.web.skill_deck

New views belong in their own module here (``hermes/web/<view>.py``) and should
be listed in ``__all__`` below and, if they deserve a command, as an entry point
in ``pyproject.toml``.
"""

from __future__ import annotations

__all__ = ["skill_deck"]
