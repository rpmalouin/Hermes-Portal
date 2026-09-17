"""Sessions and messages: the structured spine inside ``state.db``.

Hermes keeps its session index, every message and the per-model usage rollup in
one SQLite store with FTS5 indexes already built, so this is a *structured*
adapter: it queries read-only and uses the existing ``messages_fts`` index for
search instead of building a second one.

Two disciplines worth knowing:

* Schema drift across Hermes versions is normal, so every query selects only the
  columns that exist (:func:`hermes.portal.sources.select_columns`); a missing
  table degrades to a note on the collection.
* Message bodies are carried as snippets in listings and capped when a section
  shows them, so a 50-message section cannot ship megabytes of transcript into
  one page.
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
    hermes_root,
    open_sqlite,
    path_source,
    query,
    scalar,
    select_columns,
    snippet,
    state_db,
    table_columns,
    truncate,
)

SESSION_COLUMNS = (
    "id",
    "title",
    "display_name",
    "model",
    "source",
    "profile_name",
    "started_at",
    "ended_at",
    "end_reason",
    "message_count",
    "tool_call_count",
    "api_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
    "actual_cost_usd",
    "cost_status",
    "billing_provider",
    "cwd",
    "git_branch",
    "git_repo_root",
    "archived",
    "hidden",
    "pinned",
    "tool_names",
    "last_activity_description",
)
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
    "first_seen",
    "last_seen",
)
MESSAGE_COLUMNS = (
    "id",
    "session_id",
    "role",
    "content",
    "tool_name",
    "timestamp",
    "token_count",
    "finish_reason",
    "compacted",
    "active",
)
SESSION_CAP = 50
MESSAGES_CAP = 50
MESSAGE_BODY_CAP = 1200
EMPTY = "\u2014"  # f-strings on 3.11 cannot contain escapes inside expressions
USAGE_CAP = 40


def _get(row: Any, key: str, default: Any = "\u2014") -> Any:
    """Read *key* from a sqlite3.Row, tolerating a column the schema lacks."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


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


def _session_record(row: Any) -> Record:
    """One session as a list row / detail record."""
    title = (
        _get(row, "title", "") or _get(row, "display_name", "") or "(untitled session)"
    )
    session_id = str(_get(row, "id", "?"))
    started = _get(row, "started_at", None)
    messages = _get(row, "message_count", None)
    tools = _get(row, "tool_call_count", None)
    cost = _get(row, "estimated_cost_usd", None)
    model = _get(row, "model", None)
    return Record(
        id=session_id,
        title=str(truncate(str(title), 90)),
        subtitle=f"{_get(row, 'source', '?')} · {model} · {fmt_ago(started)}",
        badges=(
            f"{_number(messages)} msgs",
            f"{_number(tools)} tools",
            _money(cost),
        ),
        links=((f"/sessions/{session_id}", "Open session"),),
        group=str(_get(row, "billing_provider", "?")),
        fields=(
            ("id", session_id),
            ("model", str(model)),
            ("provider", str(_get(row, "billing_provider", None))),
            ("source", str(_get(row, "source", None))),
            ("profile", str(_get(row, "profile_name", None))),
            ("started", fmt_time(started)),
            ("ended", fmt_time(_get(row, "ended_at", None))),
            ("end reason", str(_get(row, "end_reason", None))),
            ("messages", _number(messages)),
            ("tool calls", _number(tools)),
            ("api calls", _number(_get(row, "api_call_count", None))),
            ("input tokens", _number(_get(row, "input_tokens", None))),
            ("output tokens", _number(_get(row, "output_tokens", None))),
            ("cache read", _number(_get(row, "cache_read_tokens", None))),
            ("cache write", _number(_get(row, "cache_write_tokens", None))),
            ("reasoning tokens", _number(_get(row, "reasoning_tokens", None))),
            ("cost (estimated)", _money(_get(row, "estimated_cost_usd", None))),
            ("cost (actual)", _money(_get(row, "actual_cost_usd", None))),
            ("cost status", str(_get(row, "cost_status", None))),
            ("cwd", str(_get(row, "cwd", None))),
            ("git branch", str(_get(row, "git_branch", None))),
            (
                "archived / hidden / pinned",
                f"{_get(row, 'archived', '?')} / "
                f"{_get(row, 'hidden', '?')} / "
                f"{_get(row, 'pinned', '?')}",
            ),
            ("tools used", truncate(str(_get(row, "tool_names", "")), 300)),
            (
                "last activity",
                truncate(str(_get(row, "last_activity_description", "")), 200),
            ),
        ),
    )


def _profile_store_notes() -> list[str]:
    """Describe per-profile state stores, which hold sessions of their own.

    Every profile keeps its own ``state.db`` (the running one has 2 sessions while
    the root holds 91), so a portal that quietly read one of them would be wrong
    about how much history exists.
    """
    root = hermes_root(None)
    stores: list[str] = []
    for profile in sorted((root / "profiles").glob("*/state.db")):
        con, error = open_sqlite(profile)
        if error:
            continue
        sessions = scalar(con, "select count(*) from sessions", default=0)
        _close(con)
        if sessions:
            stores.append(f"{profile.parent.name} ({sessions})")
    if not stores:
        return []
    return [
        "per-profile stores are not aggregated into these counts: "
        + ", ".join(stores[:6])
    ]


def build_domain(hermes_home: Path | None = None) -> Domain:
    """Build the sessions domain.

    Args:
        hermes_home: Hermes home or profile directory; the root holding
            ``state.db`` is resolved from it.

    Returns:
        A :class:`~hermes.portal.model.Domain`; ``state.db`` is opened read-only
        per call, so pages reflect the live database.
    """
    db_path = state_db(hermes_home)
    source = path_source("state.db", db_path, note="opened read-only, per request")

    def _open() -> tuple[Any, str]:
        """Open the state database read-only, returning ``(connection, error)``."""
        return open_sqlite(db_path)

    def _sources() -> tuple[Source, ...]:
        return (source,)

    def _notes(con: Any, error: str) -> tuple[str, ...]:
        """Caveats for this read; must be called *before* the connection closes."""
        notes = []
        if error:
            notes.append(error)
        missing = [
            table
            for table in ("sessions", "messages", "session_model_usage")
            if not table_columns(con, table)
        ]
        if missing:
            notes.append("tables absent from this database: " + ", ".join(missing))
        if db_path.with_name(db_path.name + "-wal").exists():
            notes.append(
                "a WAL file is present: the agent is live, reads may lag by a moment"
            )
        notes.extend(_profile_store_notes())
        return tuple(notes)

    def overview() -> Collection:
        """Headline numbers for the sessions domain."""
        con, error = _open()
        messages = scalar(con, "select count(*) from messages", default=0)
        tool_messages = scalar(
            con, "select count(*) from messages where tool_name is not null", default=0
        )
        providers = scalar(
            con, "select count(distinct billing_provider) from sessions", default=0
        )
        columns = select_columns(con, "sessions", SESSION_COLUMNS)
        order = (
            "started_at"
            if "started_at" in columns
            else (columns[0] if columns else "id")
        )
        rows, sql_error = query(
            con,
            f"select {', '.join(columns) or 'id'} from sessions order by {order} desc",
        )
        notes = list(_notes(con, error))  # queries, so it must run before the close
        if sql_error:
            notes.append(sql_error)
        _close(con)
        return build_collection(
            "overview",
            "Sessions",
            "Every conversation Hermes has run, with cost, tokens and tools.",
            "rows in the sessions table of state.db",
            [_session_record(row) for row in rows],
            cap=5,
            sources=_sources(),
            extra_counts=(
                Count(messages, "rows in the messages table"),
                Count(tool_messages, "messages produced by a tool"),
                Count(providers, "distinct billing providers"),
            ),
            notes=tuple(notes),
            as_of=as_of(),
        )

    def _sessions_collection(
        model: str | None = None, provider: str | None = None
    ) -> Collection:
        """The session index, newest first, optionally filtered."""
        con, error = _open()
        columns = select_columns(con, "sessions", SESSION_COLUMNS)
        order = "started_at" if "started_at" in columns else columns[0]
        where: list[str] = []
        params: list[Any] = []
        if model and "model" in columns:
            where.append("model = ?")
            params.append(model)
        elif model:
            where.append("0")
        if provider and "billing_provider" in columns:
            where.append("billing_provider = ?")
            params.append(provider)
        elif provider:
            where.append("0")
        clause = f" where {' and '.join(where)}" if where else ""
        rows, sql_error = query(
            con,
            f"select {', '.join(columns)} from sessions{clause} order by {order} desc",
            tuple(params),
        )
        everything = scalar(con, "select count(*) from sessions", default=0)
        archived = sum(1 for row in rows if _get(row, "archived", 0))
        hidden = sum(1 for row in rows if _get(row, "hidden", 0))
        notes = list(_notes(con, error))
        if sql_error:
            notes.append(sql_error)
        _close(con)

        active = " and ".join(
            part
            for part in (
                f"model {model!r}" if model else "",
                f"provider {provider!r}" if provider else "",
            )
            if part
        )
        definition = "rows in the sessions table of state.db"
        extra = [Count(everything, "sessions in the database (unfiltered)")]
        title = "Session index"
        description = "Newest first; open one to see its messages and per-model usage."
        if active:
            definition = f"rows in sessions where {active}"
            title = f"Session index ({active})"
            description = f"Filtered to {active}; drop the filter to see all sessions."
            extra.append(Count(archived, "of those, archived"))
            extra.append(Count(hidden, "of those, hidden"))
        else:
            extra.append(Count(archived, "archived"))
            extra.append(Count(hidden, "hidden"))
        return build_collection(
            "sessions",
            title,
            description,
            definition,
            [_session_record(row) for row in rows],
            cap=SESSION_CAP,
            sources=_sources(),
            extra_counts=tuple(extra),
            notes=tuple(notes),
            as_of=as_of(),
        )

    def collections(filters: Mapping[str, str] | None = None) -> Sequence[Collection]:
        """Drill-down collections for the sessions domain.

        ``?model=<name>`` and ``?provider=<name>`` narrow the session index; the
        usage domain's rollups link straight into those filters.

        The by-model, by-provider and usage rollups used to live here too.  They
        now live only in the usage domain, so the same number is never computed in
        two places; this page keeps the index and each session's own detail.
        """
        active = filters or {}
        return [
            _sessions_collection(
                model=(active.get("model") or "").strip() or None,
                provider=(active.get("provider") or "").strip() or None,
            ),
        ]

    def detail(record_id: str) -> Record | None:
        """One session, as a detail page."""
        con, error = _open()
        columns = select_columns(con, "sessions", SESSION_COLUMNS)
        rows, sql_error = query(
            con, f"select {', '.join(columns)} from sessions where id = ?", (record_id,)
        )
        _close(con)
        if not rows:
            return None
        record = _session_record(rows[0])
        notes = [error, sql_error] if (error or sql_error) else []
        return Record(
            id=record.id,
            title=record.title,
            subtitle=record.subtitle,
            badges=record.badges,
            fields=record.fields,
            links=((f"/sessions?q={record.id}", "Search this id"),),
            body="\n".join(n for n in notes if n),
        )

    def detail_sections(record_id: str) -> Sequence[Collection]:
        """Behind one session: its messages and its per-model usage."""
        con, error = _open()
        message_columns = select_columns(con, "messages", MESSAGE_COLUMNS)
        order = "timestamp" if "timestamp" in message_columns else "id"
        rows, sql_error = query(
            con,
            f"select {', '.join(message_columns)} from messages where session_id = ? "
            f"order by {order}",
            (record_id,),
        )
        usage_columns = select_columns(con, "session_model_usage", USAGE_COLUMNS)
        usage_rows, usage_error = query(
            con,
            f"select {', '.join(usage_columns)} from session_model_usage "
            f"where session_id = ?",
            (record_id,),
        )
        _close(con)

        messages = build_collection(
            "messages",
            "Messages",
            "The conversation as stored, tool calls included.",
            "rows in messages for this session (active and compacted)",
            [
                Record(
                    id=str(_get(row, "id")),
                    title=str(_get(row, "role", "?")),
                    subtitle=snippet(_get(row, "content", ""), 200),
                    badges=tuple(
                        badge
                        for badge in (
                            _get(row, "tool_name", ""),
                            "compacted" if _get(row, "compacted", 0) else "",
                            "inactive" if not _get(row, "active", 1) else "",
                            f"{_number(_get(row, 'token_count', None))} tok",
                        )
                        if badge and badge != "\u2014"
                    ),
                    fields=(
                        ("role", str(_get(row, "role", "?"))),
                        ("tool", str(_get(row, "tool_name", None))),
                        ("timestamp", fmt_time(_get(row, "timestamp", None))),
                        ("tokens", _number(_get(row, "token_count", None))),
                        ("finish reason", str(_get(row, "finish_reason", None))),
                        ("compacted", str(_get(row, "compacted", None))),
                        ("active", str(_get(row, "active", None))),
                    ),
                    body=truncate(_get(row, "content", ""), MESSAGE_BODY_CAP),
                )
                for row in rows
            ],
            cap=MESSAGES_CAP,
            sources=_sources(),
            notes=tuple(n for n in (error, sql_error) if n)
            + ("bodies are capped for the page; the full text lives in Hermes",),
            as_of=as_of(),
        )

        usage = build_collection(
            "usage",
            "Per-model usage for this session",
            "What each model cost inside this one session.",
            "rows in session_model_usage for this session",
            [
                Record(
                    id=f"{_get(row, 'model')}-{_get(row, 'task', '')}",
                    title=str(_get(row, "model")),
                    subtitle=f"task: {_get(row, 'task', EMPTY)}",
                    badges=(_money(_get(row, "estimated_cost_usd", None)),),
                    fields=(
                        ("model", str(_get(row, "model"))),
                        ("provider", str(_get(row, "billing_provider", None))),
                        ("task", str(_get(row, "task", None))),
                        ("api calls", _number(_get(row, "api_call_count", None))),
                        ("input tokens", _number(_get(row, "input_tokens", None))),
                        ("output tokens", _number(_get(row, "output_tokens", None))),
                        (
                            "cost (estimated)",
                            _money(_get(row, "estimated_cost_usd", None)),
                        ),
                        ("first seen", fmt_time(_get(row, "first_seen", None))),
                        ("last seen", fmt_time(_get(row, "last_seen", None))),
                    ),
                )
                for row in usage_rows
            ],
            cap=USAGE_CAP,
            sources=_sources(),
            notes=tuple(n for n in (error, usage_error) if n),
            as_of=as_of(),
        )
        return [messages, usage]

    def search(needle: str, limit: int) -> Sequence[Record]:
        """Search session titles and message bodies, using the FTS index."""
        term = needle.strip()
        if not term:
            return []
        con, _error = _open()
        records: list[Record] = []

        session_columns = select_columns(con, "sessions", SESSION_COLUMNS)
        if "title" in session_columns:
            rows, _sql_error = query(
                con,
                f"select {', '.join(session_columns)} from sessions "
                f"where coalesce(title,'') || ' ' || coalesce(model,'') like ? "
                f"order by started_at desc limit ?",
                (f"%{term}%", limit),
            )
            for row in rows:
                record = _session_record(row)
                records.append(
                    Record(
                        id=f"session-{record.id}",
                        title=record.title,
                        subtitle=record.subtitle,
                        badges=("session",),
                        links=((f"/sessions/{record.id}", "Open session"),),
                    )
                )

        phrase = '"' + term.replace('"', '""') + '"'
        message_columns = select_columns(con, "messages", MESSAGE_COLUMNS)
        rows, sql_error = query(
            con,
            "select m.id as id, m.session_id as session_id, m.role as role, "
            "m.tool_name as tool_name, m.content as content, m.timestamp as timestamp "
            "from messages_fts f join messages m on m.id = f.rowid "
            "where messages_fts match ? order by m.timestamp desc limit ?",
            (phrase, limit),
        )
        if sql_error:
            rows, _like_error = query(
                con,
                f"select {', '.join(message_columns)} from messages "
                f"where content like ? order by timestamp desc limit ?"
                if "content" in message_columns
                else "select 1 where 0",
                (f"%{term}%", limit),
            )
        for row in rows:
            records.append(
                Record(
                    id=f"message-{_get(row, 'id')}",
                    title=f"{_get(row, 'role', '?')} message",
                    subtitle=snippet(_get(row, "content", ""), 200),
                    badges=("message", str(_get(row, "tool_name", ""))),
                    links=((f"/sessions/{_get(row, 'session_id', '')}", "Session"),),
                )
            )
        _close(con)
        return records[:limit]

    return Domain(
        key="sessions",
        title="Sessions",
        summary="Conversations, messages and cost from state.db, read read-only.",
        overview=overview,
        collections=collections,
        detail=detail,
        search=search,
        detail_sections=detail_sections,
    )
