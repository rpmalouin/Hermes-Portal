"""Hermes Portal: a read-only, drill-down view over everything Hermes keeps.

The system is deliberately generic, because the endgame is many domains rather
than one page per feature.  Everything it shows is one of four things:

``Domain``      a top-level area (skills, sessions, cron, ... more to come)
``Collection``  a named set inside a domain, carrying its own count and sources
``Record``      one row of a collection, and the page behind it
``Source``      where a collection's data came from

Two rules hold everywhere:

* **A count is never a bare integer.**  :class:`~hermes.portal.model.Count`
  carries the rule that produced it.  This project has already shown "383", "385"
  and "157" for the same skills tree depending on how you dedupe, so every number
  is labelled and every collection reports its sources and an ``as_of`` stamp.
* **The portal never writes.**  Adapters read files and open SQLite read-only;
  anything that changes state belongs to the owning tool's command line.

Entry point: ``python -m hermes.portal`` (or the ``hermes-portal`` script).
"""

from __future__ import annotations

__all__ = ["model", "render", "server", "sources"]
