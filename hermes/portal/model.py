"""The generic drill-down vocabulary shared by every domain.

An adapter (see :mod:`hermes.portal.domains`) answers four questions, and this
module defines the types it answers them with:

* what are my collections?          -> :class:`Collection`
* what is one record?               -> :class:`Record`
* what is behind one record?        -> more :class:`Collection` sections
* what matches a search string?     -> :class:`Record`

Design notes that are load-bearing:

* :class:`Count` pairs every number with its definition, and
  :class:`Collection.extra_counts` exists so a domain can publish *competing*
  counts side by side (e.g. 157 unique skill names vs 385 files) instead of
  quietly picking one.
* A collection whose records are capped for display sets ``truncated`` and the
  renderer prints "showing N of M" -- the count stays truthful about the whole.
* :class:`DomainRegistry` never lets one broken adapter take the portal down:
  failures are converted into a zero-count collection carrying the error text.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

DETAIL_ROW = tuple[str, str]
DETAIL_LINK = tuple[str, str]


@dataclass(frozen=True)
class Count:
    """A number together with the rule that produced it."""

    value: int
    definition: str

    def __str__(self) -> str:
        """Render as ``value (definition)``."""
        return f"{self.value} ({self.definition})"


@dataclass(frozen=True)
class Source:
    """One place a collection read from, so a page can be audited."""

    label: str
    location: str
    present: bool
    detail: str = ""


@dataclass(frozen=True)
class Picker:
    """A dropdown that narrows a collection over the query string.

    Domains build these; the renderer only draws them.  Keeping the URL shape in
    the adapter means a new filter is a new picker, not a change to the page code.
    """

    query_key: str
    options: tuple[tuple[str, str], ...] = ()
    all_label: str = "All"
    selected: str = ""
    label: str = "Filter"


@dataclass(frozen=True)
class Record:
    """One row of a collection, and the detail page behind it.

    ``fields`` are the ordered key/value rows of the detail page, ``links`` are
    ``(href, label)`` pairs into other views -- href first, as every domain builds
    them -- and ``body`` is optional long text (already plain text; the renderer
    escapes it).
    """

    id: str
    title: str
    subtitle: str = ""
    badges: tuple[str, ...] = ()
    fields: tuple[DETAIL_ROW, ...] = ()
    links: tuple[DETAIL_LINK, ...] = ()
    body: str = ""
    group: str = ""
    href: str = ""

    @property
    def detail_href(self) -> str:
        """Where the title should point, when the renderer cannot know better."""
        return self.href


@dataclass(frozen=True)
class Collection:
    """A named set of records, with its count, sources and provenance."""

    key: str
    title: str
    description: str
    count: Count
    records: tuple[Record, ...] = ()
    sources: tuple[Source, ...] = ()
    extra_counts: tuple[Count, ...] = ()
    notes: tuple[str, ...] = ()
    as_of: str = ""
    display: str = "rows"
    picker: Picker | None = None
    metrics: tuple[DETAIL_ROW, ...] = ()

    @property
    def shown(self) -> int:
        """How many records this collection actually carries (may be capped)."""
        return len(self.records)

    @property
    def truncated(self) -> bool:
        """``True`` when ``records`` is a display subset of ``count.value``."""
        return self.shown < self.count.value


def build_collection(
    key: str,
    title: str,
    description: str,
    definition: str,
    records: Sequence[Record],
    *,
    cap: int | None = None,
    sources: Sequence[Source] = (),
    extra_counts: Sequence[Count] = (),
    notes: Sequence[str] = (),
    as_of: str = "",
    display: str = "rows",
    picker: Picker | None = None,
    metrics: Sequence[DETAIL_ROW] = (),
) -> Collection:
    """Assemble a :class:`Collection`, applying an optional display *cap*.

    Args:
        key: Stable identifier, also the anchor in the page.
        title: Human title.
        description: What this collection is.
        definition: The rule behind ``count`` (never optional).
        records: All collected records, in display order.
        cap: Maximum records to keep for display; the count stays the full size.
        sources: Where the data came from.
        extra_counts: Additional competing counts worth publishing.
        notes: Per-adapter warnings, errors and caveats.
        as_of: ISO-8601 UTC stamp for the read.
        display: How the records read best: ``"rows"`` (default) or ``"cards"``.
        picker: Optional dropdown for narrowing this collection.
        metrics: Labelled headline values (``("Estimated cost", "$1.2345")``).
            Counts stay integers; money, durations and sizes belong here.

    Returns:
        A :class:`Collection`; ``truncated`` is derived, never passed in.
    """
    kept = tuple(records[:cap]) if cap is not None else tuple(records)
    return Collection(
        key=key,
        title=title,
        description=description,
        count=Count(len(records), definition),
        records=kept,
        sources=tuple(sources),
        extra_counts=tuple(extra_counts),
        notes=tuple(notes),
        as_of=as_of,
        display=display,
        picker=picker,
        metrics=tuple(metrics),
    )


@dataclass(frozen=True)
class Domain:
    """A top-level area of the portal, backed by one adapter.

    The four callables are the whole adapter contract; nothing else is
    required.  ``detail_sections`` is what makes the drill-down real: it returns
    the collections behind one record (a session's messages, a cron job's runs).
    """

    key: str
    title: str
    summary: str
    overview: Callable[[], Collection]
    collections: Callable[..., Sequence[Collection]]
    detail: Callable[[str], Record | None]
    search: Callable[[str, int], Sequence[Record]]
    detail_sections: Callable[[str], Sequence[Collection]] = lambda _record_id: ()


def failed_collection(key: str, title: str, error: str, as_of: str = "") -> Collection:
    """Return a zero-count collection that reports an adapter failure."""
    return Collection(
        key=key,
        title=title,
        description="This adapter failed; the portal is still up.",
        count=Count(0, "unavailable -- the adapter raised"),
        notes=(error,),
        as_of=as_of,
    )


@dataclass
class DomainRegistry:
    """Holds the domains and keeps a broken one from taking the portal down."""

    _domains: dict[str, Domain] = field(default_factory=dict)

    def register(self, domain: Domain) -> None:
        """Add *domain*.

        Raises:
            ValueError: A domain with the same key is already registered.
        """
        if domain.key in self._domains:
            raise ValueError(f"duplicate domain key: {domain.key!r}")
        self._domains[domain.key] = domain

    def get(self, key: str) -> Domain:
        """Return the domain called *key*.

        Raises:
            KeyError: No such domain.
        """
        try:
            return self._domains[key]
        except KeyError:
            raise KeyError(f"unknown domain: {key!r}") from None

    def keys(self) -> list[str]:
        """Registered domain keys, sorted."""
        return sorted(self._domains)

    def __contains__(self, key: object) -> bool:
        """Support ``"skills" in registry``."""
        return key in self._domains

    def all(self) -> list[Domain]:
        """Registered domains, sorted by key."""
        return [self._domains[key] for key in self.keys()]

    def overviews(self) -> list[Collection]:
        """One overview collection per domain, failures included as notes."""
        return [self.safe_overview(domain) for domain in self.all()]

    def safe_overview(self, domain: Domain) -> Collection:
        """Call ``domain.overview()``, converting any exception into a note."""
        try:
            return domain.overview()
        except Exception as exc:  # noqa: BLE001 - a portal must survive an adapter
            return failed_collection(
                domain.key, domain.title, f"{type(exc).__name__}: {exc}"
            )

    def safe_collections(
        self, domain: Domain, filters: Mapping[str, str] | None = None
    ) -> list[Collection]:
        """Call ``domain.collections(filters)``, converting errors into a note.

        Args:
            domain: The domain to ask.
            filters: Query-string values the domain may use to filter itself
                (``?box=creative``), so a link from one page lands on a real
                filtered view rather than an unfiltered one.
        """
        try:
            return list(domain.collections(filters or {}))
        except Exception as exc:  # noqa: BLE001 - see above
            return [
                failed_collection(
                    domain.key, domain.title, f"{type(exc).__name__}: {exc}"
                )
            ]

    def safe_detail(self, domain: Domain, record_id: str) -> Record | None:
        """Look up one record, never raising for a bad id."""
        try:
            return domain.detail(record_id)
        except Exception:  # noqa: BLE001 - treat an adapter error as "not found"
            return None

    def safe_sections(self, domain: Domain, record_id: str) -> list[Collection]:
        """Return the collections behind one record, failures included."""
        try:
            return list(domain.detail_sections(record_id))
        except Exception as exc:  # noqa: BLE001 - see above
            return [
                failed_collection(
                    domain.key, "sections", f"{type(exc).__name__}: {exc}"
                )
            ]

    def search(self, query: str, limit: int = 20) -> dict[str, tuple[Record, ...]]:
        """Search every domain, returning hits grouped by domain key.

        A domain that raises contributes an empty group rather than an error.
        """
        results: dict[str, tuple[Record, ...]] = {}
        for domain in self.all():
            try:
                results[domain.key] = tuple(domain.search(query, limit))
            except Exception:  # noqa: BLE001 - search must never 500 the page
                results[domain.key] = ()
        return results


def clone_collection(collection: Collection, **changes: Any) -> Collection:
    """Return a copy of *collection* with fields replaced (thin dataclass sugar)."""
    return replace(collection, **changes)


def count_map(counts: Iterable[Count]) -> dict[str, int]:
    """Turn labelled counts into ``{definition: value}``, for tests and JSON."""
    return {count.definition: count.value for count in counts}
