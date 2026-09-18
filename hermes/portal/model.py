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

import re
import urllib.parse
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


def unavailable_definition(reason: str) -> str:
    """The rule for a count whose source could not be read.

    A count names the rule that produced it.  When the rule could not run, the
    number cannot be ``0`` either: 0 asserts an empty set, and the truth is that
    nobody looked.  The value stays 0 -- there are no records to show -- and the
    definition carries the reason, so the page says *unavailable* instead of
    quietly counting nothing.

    Args:
        reason: What could not be read, in a few words (``"state.db"``).

    Returns:
        A definition string for :class:`Count`, e.g.
        ``"unavailable -- state.db could not be read"``.
    """
    return f"unavailable -- {reason}"


def unavailable_count(reason: str) -> Count:
    """A zero count for a source that could not be read.

    The value is 0 because there are no records to show; the definition carries
    the reason, which is the part a reader needs.
    """
    return Count(0, unavailable_definition(reason))


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
    unavailable: str = "",
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
        unavailable: Set when the collection's own source could not be read.
            ``definition``, ``extra_counts`` and ``metrics`` are replaced, because
            each of them is a claim *about that source* -- a number derived from a
            read that never happened is the same bug as a count that says 0.

    Returns:
        A :class:`Collection`; ``truncated`` is derived, never passed in.
    """
    if unavailable:
        definition = unavailable_definition(unavailable)
        extra_counts = ()
        metrics = ()
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
    #: Drop whatever this domain caches, so the next read goes back to the sources.  A
    #: page never calls it -- re-reading a 3 MB vault mid-request would be a surprise --
    #: so it is wired to the explicit refresh (``POST /refresh.json``) and nothing else.
    #: ``None`` means the domain has no cache to drop.
    forget: Callable[[], None] | None = None


#: Credential-shaped text that must never reach a page.  Logs, message bodies, job
#: output and adapter error strings can all contain a pasted key or a bearer header,
#: so every path that carries free text runs through :func:`scrub` first.
_SCRUB_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{6,}"
    r"|bearer\s+\S+"
    r"|(api[_-]?key|token|secret|password|passwd)\s*[=:]\s*\S+)"
)


def scrub(text: str) -> str:
    """Mask credential-shaped substrings before text reaches a page.

    ``sources`` re-exports this so adapters can keep importing it from there; it
    lives here because :func:`failed_collection` needs it and ``sources`` already
    imports this module (importing the other way round would be a cycle).

    This masks the shape, not the value: it is a display guard, never a reason to
    trust a file.
    """
    return _SCRUB_RE.sub("<redacted>", text)


def detail_url(domain_key: str, record_id: str) -> str:
    """The URL of a record's page, with the id encoded as one path segment.

    Every generated link goes through here -- the renderer building a row's target,
    and the domains pointing a run at its job, a message at its session, a card at
    its board.  A vault note id is a path, so an unencoded one would be a link the
    server reads as a file that does not exist.
    """
    key = urllib.parse.quote(domain_key, safe="")
    return f"/{key}/{urllib.parse.quote(str(record_id), safe='')}"


def filter_url(domain_key: str, **params: str) -> str:
    """The URL of a filtered view: a domain page plus its picker's query.

    Values are encoded here, so a model name with a slash or a folder with a space
    cannot split the query or truncate it.  An empty value is dropped rather than
    sent, because a picker offers no empty choice.
    """
    key = urllib.parse.quote(domain_key, safe="")
    query = urllib.parse.urlencode({k: str(v) for k, v in params.items() if v != ""})
    return f"/{key}?{query}" if query else f"/{key}"


def search_url(query: str) -> str:
    """The cross-domain search URL for a query.

    Search is its own route with its own parameter, not a filter: ``q`` is not in
    FILTER_KEYS, and the server strips anything that is not.  A chip pointing at
    ``?q=`` on a domain page therefore looked like a search and did nothing.
    """
    return f"/search?q={urllib.parse.quote(str(query), safe='')}"


def failed_collection(key: str, title: str, error: str, as_of: str = "") -> Collection:
    """Return a zero-count collection that reports an adapter failure.

    The error text is scrubbed on the way in, like every other free-text path: an
    exception message can carry a connection string, a path or a pasted key, and
    this collection is rendered on the page.
    """
    return Collection(
        key=key,
        title=title,
        description="This adapter failed; the portal is still up.",
        count=unavailable_count("the adapter raised"),
        notes=(scrub(error),),
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
