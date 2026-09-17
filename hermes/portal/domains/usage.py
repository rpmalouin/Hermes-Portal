"""Usage and cost domain: what Hermes has spent, over time and by model.

One read-only source, ``state.db``, and one home for the cross-cutting rollups.
The sessions domain still shows *per-session* usage on its detail pages, but the
by-model, by-provider and by-day views live here so the same number is never
computed in two places -- the drift rule this portal keeps.

Two caveats the page states rather than hides:

* Cost is what Hermes *estimated* (``estimated_cost_usd``) unless a provider
  reported an actual amount (``actual_cost_usd``); priced and unpriced rows are
  counted separately rather than quietly summed.
* Token counts are the agent's own accounting, so a provider that does not report
  usage leaves a smaller number, not a wrong one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..model import Collection, Count, Domain, Record, Source, build_collection
from ..sources import (
    as_of,
    fmt_ago,
    fmt_time,
    open_sqlite,
    path_source,
    query,
    scalar,
    select_columns,
    snippet,
    state_db,
    truncate,
)

DAY_CAP = 40
MODEL_CAP = 40
TOP_CAP = 20
SESSION_COLUMNS = ("id", "title", "model", "billing_provider", "started_at")
USAGE_COLUMNS = (
    "session_id",
    "model",
    "billing_provider",
    "task",
    "api_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
    "actual_cost_usd",
    "last_seen",
)


def _close(con: Any) -> None:
    """Close a connection if one was opened."""
    if con is not None:
        con.close()


def _money(value: Any) -> str:
    """Format a dollar amount, tolerating None and odd types."""
    if value in (None, ""):
        return "\u2014"
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _number(value: Any) -> str:
    """Format an integer-ish value with thousands separators."""
    if value in (None, ""):
        return "\u2014"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _get(row: Any, key: str, default: Any = "\u2014") -> Any:
    """Read *key* from a sqlite3.Row, tolerating a column the schema lacks."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def build_domain(hermes_home: Path | None = None) -> Domain:
    """Build the usage domain.

    Args:
        hermes_home: Hermes home or profile directory; the root holding
            ``state.db`` is resolved from it.

    Returns:
        A :class:`~hermes.portal.model.Domain`; ``state.db`` is opened read-only
        per collection, so the numbers are current.
    """
    db_path = state_db(hermes_home)
    source = path_source("state.db", db_path, note="read-only")

    def _open() -> tuple[Any, str]:
        return open_sqlite(db_path)

    def _sources() -> tuple[Source, ...]:
        return (source,)

    def overview() -> Collection:
        """Headline spend: sessions counted, money and tokens as metrics."""
        con, error = _open()
        usage_columns = select_columns(con, "session_model_usage", USAGE_COLUMNS)
        sessions = scalar(con, "select count(*) from sessions", default=0)
        priced = scalar(
            con,
            "select count(*) from sessions where estimated_cost_usd is not null",
            default=0,
        )
        estimated = scalar(
            con, "select sum(estimated_cost_usd) from sessions", default=0
        )
        actual = scalar(con, "select sum(actual_cost_usd) from sessions", default=0)
        tokens_in = scalar(con, "select sum(input_tokens) from sessions", default=0)
        tokens_out = scalar(con, "select sum(output_tokens) from sessions", default=0)
        calls = scalar(
            con, "select sum(api_call_count) from session_model_usage", default=0
        )
        select_list = ", ".join(select_columns(con, "sessions", SESSION_COLUMNS))
        rows, sql_error = query(
            con,
            f"select {select_list} from sessions "
            "where estimated_cost_usd is not null "
            "order by estimated_cost_usd desc",
        )
        notes = [note for note in (error, sql_error) if note]
        if sessions and priced < sessions:
            notes.append(
                f"{sessions - priced} of {sessions} sessions carry no cost estimate; "
                "totals below cover the priced ones"
            )
        if not usage_columns:
            notes.append("session_model_usage is absent, so api_call_count is unknown")
        _close(con)
        records = [
            Record(
                id=str(_get(row, "id")),
                title=truncate(str(_get(row, "title", "(untitled)")), 70),
                subtitle=f"{_get(row, 'model')} · "
                f"{fmt_ago(_get(row, 'started_at', None))}",
                badges=(_money(_get(row, "estimated_cost_usd", None)),),
                links=((f"/sessions/{_get(row, 'id')}", "Open session"),),
            )
            for row in rows
        ]
        return build_collection(
            "overview",
            "Usage",
            "Spend and tokens across every session Hermes has recorded.",
            "sessions with a cost estimate (the ones these totals cover)",
            records,
            cap=5,
            sources=_sources(),
            extra_counts=(
                Count(sessions, "sessions in total"),
                Count(sessions - priced, "sessions without a cost estimate"),
            ),
            metrics=(
                ("Estimated cost", _money(estimated)),
                ("Actual cost reported", _money(actual)),
                ("Input tokens", _number(tokens_in)),
                ("Output tokens", _number(tokens_out)),
                ("API calls", _number(calls)),
            ),
            notes=tuple(notes),
            as_of=as_of(),
        )

    def _by_day() -> Collection:
        """Cost and tokens per calendar day (local time)."""
        con, error = _open()
        columns = select_columns(
            con,
            "sessions",
            ("started_at", "input_tokens", "output_tokens", "estimated_cost_usd"),
        )
        if "started_at" not in columns:
            _close(con)
            return build_collection(
                "by-day",
                "By day",
                "Spend per day.",
                "days with sessions",
                [],
                sources=_sources(),
                notes=(error or "no started_at column",),
                as_of=as_of(),
            )
        sums = ", ".join(
            f"sum({column}) as {column}"
            for column in ("input_tokens", "output_tokens", "estimated_cost_usd")
            if column in columns
        )
        rows, sql_error = query(
            con,
            "select date(started_at, 'unixepoch', 'localtime') as day, "
            "count(*) as sessions "
            f"{', ' + sums if sums else ''} from sessions "
            "group by day order by day desc",
        )
        _close(con)
        return build_collection(
            "by-day",
            "By day",
            "Cost and tokens per calendar day, newest first.",
            "distinct days on which sessions started",
            [
                Record(
                    id=str(_get(row, "day")),
                    title=str(_get(row, "day")),
                    subtitle=f"{_number(_get(row, 'sessions'))} session(s)",
                    badges=(_money(_get(row, "estimated_cost_usd", None)),),
                    fields=(
                        ("day", str(_get(row, "day"))),
                        ("sessions", _number(_get(row, "sessions"))),
                        ("input tokens", _number(_get(row, "input_tokens", None))),
                        ("output tokens", _number(_get(row, "output_tokens", None))),
                        (
                            "cost (estimated)",
                            _money(_get(row, "estimated_cost_usd", None)),
                        ),
                    ),
                )
                for row in rows
            ],
            cap=DAY_CAP,
            sources=_sources(),
            notes=tuple(note for note in (error, sql_error) if note),
            as_of=as_of(),
        )

    def _by_model() -> Collection:
        """Per-model rollup, the fullest picture of where tokens go."""
        con, error = _open()
        columns = select_columns(con, "session_model_usage", USAGE_COLUMNS)
        if "model" not in columns:
            _close(con)
            return build_collection(
                "by-model",
                "By model",
                "Tokens and cost per model.",
                "distinct models in session_model_usage",
                [],
                sources=_sources(),
                notes=(error or "session_model_usage is absent from this database",),
                as_of=as_of(),
            )
        sums = ", ".join(
            f"sum({column}) as {column}"
            for column in (
                "api_call_count",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "estimated_cost_usd",
                "actual_cost_usd",
            )
            if column in columns
        )
        rows, sql_error = query(
            con,
            f"select model, count(*) as rows {', ' + sums if sums else ''} "
            "from session_model_usage group by model "
            "order by sum(estimated_cost_usd) desc",
        )
        _close(con)
        return build_collection(
            "by-model",
            "By model",
            "Tokens, calls and cost per model, biggest spend first.",
            "distinct models in session_model_usage",
            [
                Record(
                    id=str(_get(row, "model")),
                    title=str(_get(row, "model")),
                    subtitle=f"{_number(_get(row, 'api_call_count'))} calls · "
                    f"{_number(_get(row, 'output_tokens'))} output tokens",
                    badges=(_money(_get(row, "estimated_cost_usd", None)),),
                    href=f"/sessions?model={_get(row, 'model', '')}",
                    fields=(
                        ("model", str(_get(row, "model"))),
                        ("usage rows", _number(_get(row, "rows"))),
                        ("API calls", _number(_get(row, "api_call_count", None))),
                        ("input tokens", _number(_get(row, "input_tokens", None))),
                        ("output tokens", _number(_get(row, "output_tokens", None))),
                        ("cache read", _number(_get(row, "cache_read_tokens", None))),
                        ("cache write", _number(_get(row, "cache_write_tokens", None))),
                        (
                            "reasoning tokens",
                            _number(_get(row, "reasoning_tokens", None)),
                        ),
                        (
                            "cost (estimated)",
                            _money(_get(row, "estimated_cost_usd", None)),
                        ),
                        ("cost (actual)", _money(_get(row, "actual_cost_usd", None))),
                    ),
                )
                for row in rows
            ],
            cap=MODEL_CAP,
            sources=_sources(),
            notes=tuple(note for note in (error, sql_error) if note),
            as_of=as_of(),
        )

    def _by_provider() -> Collection:
        """Per-provider rollup, from the session rows themselves."""
        con, error = _open()
        columns = select_columns(
            con,
            "sessions",
            ("billing_provider", "input_tokens", "output_tokens", "estimated_cost_usd"),
        )
        if "billing_provider" not in columns:
            _close(con)
            return build_collection(
                "by-provider",
                "By provider",
                "Spend per provider.",
                "distinct billing providers",
                [],
                sources=_sources(),
                notes=(error or "no billing_provider column",),
                as_of=as_of(),
            )
        sums = ", ".join(
            f"sum({column}) as {column}"
            for column in ("input_tokens", "output_tokens", "estimated_cost_usd")
            if column in columns
        )
        rows, sql_error = query(
            con,
            f"select billing_provider as provider, count(*) as sessions "
            f"{', ' + sums if sums else ''} from sessions group by provider "
            "order by count(*) desc",
        )
        _close(con)
        return build_collection(
            "by-provider",
            "By provider",
            "Which account paid for the tokens.",
            "distinct billing providers",
            [
                Record(
                    id=str(_get(row, "provider")),
                    title=str(_get(row, "provider")),
                    subtitle=f"{_number(_get(row, 'sessions'))} session(s)",
                    badges=(_money(_get(row, "estimated_cost_usd", None)),),
                    href=f"/sessions?provider={_get(row, 'provider', '')}",
                    fields=(
                        ("provider", str(_get(row, "provider"))),
                        ("sessions", _number(_get(row, "sessions"))),
                        ("input tokens", _number(_get(row, "input_tokens", None))),
                        ("output tokens", _number(_get(row, "output_tokens", None))),
                        (
                            "cost (estimated)",
                            _money(_get(row, "estimated_cost_usd", None)),
                        ),
                    ),
                )
                for row in rows
            ],
            cap=MODEL_CAP,
            sources=_sources(),
            notes=tuple(note for note in (error, sql_error) if note),
            as_of=as_of(),
        )

    def _top_sessions() -> Collection:
        """The most expensive sessions, linking into the sessions domain."""
        con, error = _open()
        columns = select_columns(
            con,
            "sessions",
            (
                "id",
                "title",
                "model",
                "started_at",
                "estimated_cost_usd",
                "input_tokens",
                "output_tokens",
                "message_count",
            ),
        )
        if "estimated_cost_usd" not in columns:
            _close(con)
            return build_collection(
                "top-sessions",
                "Most expensive sessions",
                "Ranked by estimated cost.",
                "sessions with a cost estimate",
                [],
                sources=_sources(),
                notes=(error or "no cost column",),
                as_of=as_of(),
            )
        rows, sql_error = query(
            con,
            f"select {', '.join(columns)} from sessions "
            "where estimated_cost_usd is not null "
            "order by estimated_cost_usd desc limit ?",
            (TOP_CAP,),
        )
        _close(con)
        return build_collection(
            "top-sessions",
            "Most expensive sessions",
            "Where the money went; open one to see its messages and per-model usage.",
            "sessions ranked by estimated cost",
            [
                Record(
                    id=str(_get(row, "id")),
                    title=truncate(str(_get(row, "title", "(untitled)")), 70),
                    subtitle=f"{_get(row, 'model')} · "
                    f"{fmt_time(_get(row, 'started_at', None))} · "
                    f"{_number(_get(row, 'message_count'))} msgs",
                    badges=(_money(_get(row, "estimated_cost_usd", None)),),
                    links=((f"/sessions/{_get(row, 'id')}", "Open session"),),
                    fields=(
                        ("session", str(_get(row, "id"))),
                        (
                            "cost (estimated)",
                            _money(_get(row, "estimated_cost_usd", None)),
                        ),
                        ("model", str(_get(row, "model"))),
                        ("input tokens", _number(_get(row, "input_tokens", None))),
                        ("output tokens", _number(_get(row, "output_tokens", None))),
                        ("started", fmt_time(_get(row, "started_at", None))),
                    ),
                )
                for row in rows
            ],
            sources=_sources(),
            notes=tuple(note for note in (error, sql_error) if note),
            as_of=as_of(),
        )

    def collections(_filters: Mapping[str, str] | None = None) -> Sequence[Collection]:
        """Drill-down collections for usage and cost.

        No query filters: the rollups link out to the sessions index, which is where
        filtering by model or provider belongs.
        """
        return [_by_day(), _by_model(), _by_provider(), _top_sessions()]

    def detail(record_id: str) -> Record | None:
        """A day or a model, as a detail page."""
        for collection in (_by_day(), _by_model(), _by_provider()):
            for record in collection.records:
                if record.id == record_id:
                    return record
        return None

    def detail_sections(record_id: str) -> Sequence[Collection]:
        """Behind a model or day: the sessions that contributed to it."""
        con, error = _open()
        columns = select_columns(
            con,
            "sessions",
            (
                "id",
                "title",
                "model",
                "billing_provider",
                "started_at",
                "estimated_cost_usd",
            ),
        )
        rows, sql_error = query(
            con,
            f"select {', '.join(columns)} from sessions "
            "where model = ? or billing_provider = ? or "
            "date(started_at, 'unixepoch', 'localtime') = ? "
            "order by estimated_cost_usd desc",
            (record_id, record_id, record_id),
        )
        _close(con)
        return [
            build_collection(
                "sessions",
                "Sessions behind this record",
                "Every session matching this day, model or provider.",
                "sessions whose day, model or provider equals this id",
                [
                    Record(
                        id=str(_get(row, "id")),
                        title=truncate(str(_get(row, "title", "(untitled)")), 70),
                        subtitle=f"{_get(row, 'model')} · "
                        f"{fmt_time(_get(row, 'started_at', None))}",
                        badges=(_money(_get(row, "estimated_cost_usd", None)),),
                        links=((f"/sessions/{_get(row, 'id')}", "Open session"),),
                    )
                    for row in rows
                ],
                cap=TOP_CAP,
                sources=_sources(),
                notes=tuple(note for note in (error, sql_error) if note),
                as_of=as_of(),
            )
        ]

    def search(needle: str, limit: int) -> Sequence[Record]:
        """Find a model or provider by name."""
        term = needle.strip().lower()
        if not term:
            return []
        hits: list[Record] = []
        for collection in (_by_model(), _by_provider(), _by_day()):
            for record in collection.records:
                if term in record.title.lower():
                    hits.append(
                        Record(
                            id=record.id,
                            title=record.title,
                            subtitle=snippet(record.subtitle, 120),
                            badges=("usage",) + record.badges[:1],
                            links=record.links or (("/usage", "Usage"),),
                        )
                    )
                if len(hits) >= limit:
                    return hits
        return hits

    return Domain(
        key="usage",
        title="Usage",
        summary="Cost and tokens over time, by model and by provider, from state.db.",
        overview=overview,
        collections=collections,
        detail=detail,
        search=search,
        detail_sections=detail_sections,
    )
