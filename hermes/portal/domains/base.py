"""What every domain is built from: one snapshot, cached, and the wiring to serve it.

Domains used to be built by a single ``build_domain`` function holding a dozen nested
closures over a ``state: dict`` cache.  It worked, but the graph measured the cost: the
factory in ``memory.py`` reached 793 lines, ``graph.py``'s 567, and because the helpers
were closures nobody -- no test, no reader -- could call one by name.  Most of the
code-review graph's "untested hotspots" in this project were that shape.

So a domain is a class now.  Three things change:

* **the snapshot is an attribute**, read once behind a lock
  (:meth:`SnapshotDomain.snapshot`) instead of a dict a closure happens to close
  over, so two threads asking at once read the sources once rather than twice;
* **helpers are methods with names**, which a test can call directly and the graph can
  see referenced;
* **the wiring is written once**, in :meth:`SnapshotDomain.domain`, so a domain module
  ends with ``return MemoryDomain(hermes_home).domain()`` rather than five lines of
  plumbing.

``build_domain(hermes_home=...)`` stays each module's public entry point -- the registry
calls it -- so this is an internal refactor, convertible one domain at a time.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Generic, TypeVar

from ..model import Collection, Domain, Record

Snapshot = TypeVar("Snapshot")


class SnapshotDomain(Generic[Snapshot]):
    """A domain that reads its sources once and serves every page from that result.

    Subclasses set :attr:`key`, :attr:`title` and :attr:`summary`, implement
    :meth:`read` to produce the snapshot, and publish collections by implementing
    :meth:`overview` and, as needed, :meth:`collections`, :meth:`detail`,
    :meth:`detail_sections` and :meth:`search`.
    """

    key: str = ""
    title: str = ""
    summary: str = ""

    def __init__(self, hermes_home: Path | None = None) -> None:
        """Remember where the sources are; nothing is read until a page asks."""
        self.hermes_home = Path(hermes_home) if hermes_home is not None else None
        self._cache: Snapshot | None = None
        self._lock = threading.Lock()

    def snapshot(self) -> Snapshot:
        """The snapshot, reading the sources on first use.

        The lock is held across the read, so a burst of requests walks the tree once;
        the check outside it keeps the common case lock-free.
        """
        if self._cache is not None:
            return self._cache
        with self._lock:
            if self._cache is None:
                self._cache = self.read()
        return self._cache

    def read(self) -> Snapshot:
        """Read the sources and return the snapshot.

        Raises:
            NotImplementedError: A subclass must implement this.
        """
        raise NotImplementedError

    def forget(self) -> None:
        """Drop the snapshot, so the next :meth:`snapshot` call reads again.

        For tests, and for a caller that knows the sources changed.  The portal never
        does this: a page that silently re-read a 3 MB vault mid-request would be a
        surprise.
        """
        with self._lock:
            self._cache = None

    def overview(self) -> Collection:
        """The headline collection.

        Raises:
            NotImplementedError: A subclass must implement this.
        """
        raise NotImplementedError

    def collections(
        self, filters: Mapping[str, str] | None = None
    ) -> Sequence[Collection]:
        """Drill-down collections.  A domain with none returns an empty sequence."""
        return ()

    def detail(self, record_id: str) -> Record | None:
        """One record, or ``None`` when the id is unknown.  A subclass answers."""
        return None

    def detail_sections(self, record_id: str) -> Sequence[Collection]:
        """What sits behind one record.  No sections by default."""
        return ()

    def search(self, query: str, limit: int) -> Sequence[Record]:
        """Find records by text.  Domains that cannot answer return nothing."""
        return ()

    def domain(self) -> Domain:
        """Wire this instance into the :class:`~hermes.portal.model.Domain` a
        registry holds."""
        return Domain(
            key=self.key,
            title=self.title,
            summary=self.summary,
            overview=self.overview,
            collections=self.collections,
            detail=self.detail,
            search=self.search,
            detail_sections=self.detail_sections,
        )

    def __repr__(self) -> str:
        """Show the domain and where it reads from."""
        home = self.hermes_home or "(resolved from $HERMES_HOME)"
        return f"<{type(self).__name__} {self.key!r} home={home}>"
