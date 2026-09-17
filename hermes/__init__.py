"""Hermes Portal: a read-only drill-down over everything Hermes keeps, plus the

Layout (mirrors the Hermes Skill Deck system):

``hermes/core``      internals: models, loader, registry, executor, runtime
``hermes/skills``    drop-in skills: one directory per skill (skill.json + main.py)
``hermes/profiles``  JSON profiles: which skills directory to load, default timeout
``hermes/cli``       interactive ``hermes>`` REPL

Quick start::

    python -m hermes.cli.shell
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
