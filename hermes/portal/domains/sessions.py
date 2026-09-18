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

from .. import fts
from ..model import (
    Collection,
    Count,
    Domain,
    Record,
    Source,
    build_collection,
    detail_url,
    search_url,
)
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
from .base import SnapshotDomain

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
        links=((detail_url("sessions", session_id), "Open session"),),
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


class SessionsDomain(SnapshotDomain[None]):
    """Sessions and their messages, read from the session store.

    A class rather than a factory of closures, so every helper below can be called by
    name -- by a test, by a reader, and by the code graph.
    """

    key = "sessions"
    title = "Sessions"
    summary = "Conversations, messages and cost from state.db, read read-only."

    def __init__(self, hermes_home: Path | None = None) -> None:
        """Point at the sources; nothing is read until a page asks."""
        super().__init__(hermes_home)
        self.db_path = state_db(self.hermes_home)
        self.source = path_source(
            "state.db", self.db_path, note="opened read-only, per request"
        )

    def _open(self) -> tuple[Any, str]:
        """Open the state database read-only, returning ``(connection, error)``."""
        return open_sqlite(self.db_path)

    def _sources(self) -> tuple[Source, ...]:
        return (self.source,)

    def _notes(self, con: Any, error: str) -> tuple[str, ...]:
        """Caveats for this read; must be called *before* the connection closes."""
        notes = []
        if error:
            notes.append(error)
        # Only a connection that opened can report absent tables.  With no
        # connection every table looks absent, and that list would sit beside the
        # real error implying Hermes moved the schema.
        missing = (
            [
                table
                for table in ("sessions", "messages", "session_model_usage")
                if not table_columns(con, table)
            ]
            if con is not None
            else []
        )
        if missing:
            notes.append("tables absent from this database: " + ", ".join(missing))
        if self.db_path.with_name(self.db_path.name + "-wal").exists():
            notes.append(
                "a WAL file is present: the agent is live, reads may lag by a moment"
            )
        notes.extend(_profile_store_notes())
        return tuple(notes)

    def overview(self) -> Collection:
        """Headline numbers for the sessions domain."""
        con, error = self._open()
        messages = scalar(con, "select count(*) from messages", default=0)
        tool_messages = scalar(
            con, "select count(*) from messages where tool_name is not null", default=0
        )
        indexes = fts.available_indexes(con)
        indexed = (
            scalar(con, f"select count(*) from {fts.WORD_INDEX}", default=0)
            if fts.WORD_INDEX in indexes
            else 0
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
        notes = list(
            self._notes(con, error)
        )  # queries, so it must run before the close
        if sql_error:
            notes.append(sql_error)
        notes.append(
            f"message search runs through {fts.WORD_INDEX} (relevance ranked, excerpt "
            "around the hit)"
            if fts.WORD_INDEX in indexes
            else "no FTS index over messages: search falls back to a LIKE scan"
        )
        if fts.SUBSTRING_INDEX in indexes:
            notes.append(
                f"{fts.SUBSTRING_INDEX} is present too, and answers substring queries "
                "when the word index finds nothing"
            )
        _close(con)
        return build_collection(
            "overview",
            "Sessions",
            "Every conversation Hermes has run, with cost, tokens and tools.",
            "rows in the sessions table of state.db",
            [_session_record(row) for row in rows],
            cap=5,
            sources=self._sources(),
            extra_counts=(
                Count(messages, "rows in the messages table"),
                Count(indexed, f"rows in the {fts.WORD_INDEX} search index"),
                Count(tool_messages, "messages produced by a tool"),
                Count(providers, "distinct billing providers"),
            ),
            notes=tuple(notes),
            as_of=as_of(),
        )

    def _sessions_collection(
        self, model: str | None = None, provider: str | None = None
    ) -> Collection:
        """The session index, newest first, optionally filtered."""
        con, error = self._open()
        columns = select_columns(con, "sessions", SESSION_COLUMNS)
        order = (
            "started_at"
            if "started_at" in columns
            else (columns[0] if columns else "id")
        )
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
        notes = list(self._notes(con, error))
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
            sources=self._sources(),
            extra_counts=tuple(extra),
            notes=tuple(notes),
            as_of=as_of(),
        )

    def _messages_collection(self) -> Collection:
        """The newest messages: the corpus, browsable and not only searchable.

        These excerpts are the *start* of each message, not a search excerpt -- nothing
        is highlighted here, and the title says so by comparing with a search result,
        which is an excerpt around the match.
        """
        con, error = self._open()
        total = fts.count_messages(con)
        rows, note = fts.recent_messages(con, MESSAGES_CAP)
        by_role, _role_error = query(
            con,
            "select role as role, count(*) as n from messages "
            "group by role order by n desc limit 3",
        )
        tool_messages = scalar(
            con, "select count(*) from messages where tool_name is not null", default=0
        )
        indexes = fts.available_indexes(con)
        _close(con)
        records = []
        for row in rows:
            role = str(row.get("role") or "?")
            tool = str(row.get("tool_name") or "").strip()
            session_id = str(row.get("session_id") or "")
            records.append(
                Record(
                    id=f"message-{row.get('id')}",
                    title=f"{role} message" + (f" · {tool}" if tool else ""),
                    subtitle=row.get("excerpt") or "(empty)",
                    href=detail_url("sessions", session_id) if session_id else "",
                    badges=(role, "tool call" if tool else "text"),
                    fields=(
                        ("session", session_id),
                        ("role", role),
                        ("tool", tool or "—"),
                        ("at", fmt_time(row.get("timestamp"))),
                    ),
                    links=((detail_url("sessions", session_id), "Open session"),)
                    if session_id
                    else (),
                )
            )
        notes: list[str] = []
        if error:
            notes.append(error)
        if note:
            notes.append(note)
        if not records:
            notes.append("no messages table in state.db")
        notes.append(
            f"searchable through {fts.WORD_INDEX}"
            if fts.WORD_INDEX in indexes
            else "no FTS index: message search falls back to a LIKE scan"
        )
        return build_collection(
            "messages",
            "Recent messages",
            "The newest messages across every session, newest first: what was actually "
            "said, not just which sessions exist.",
            f"messages from the messages table, newest {MESSAGES_CAP} shown",
            records,
            cap=MESSAGES_CAP,
            sources=self._sources(),
            extra_counts=(
                Count(total, "rows in the messages table"),
                Count(tool_messages, "messages produced by a tool"),
            )
            + tuple(Count(int(row["n"]), f"{row['role']} messages") for row in by_role),
            notes=tuple(notes),
            as_of=as_of(),
        )

    def collections(
        self, filters: Mapping[str, str] | None = None
    ) -> Sequence[Collection]:
        """Drill-down collections for the sessions domain.

        ``?model=<name>`` and ``?provider=<name>`` narrow the session index; the
        usage domain's rollups link straight into those filters.

        The by-model, by-provider and usage rollups used to live here too.  They
        now live only in the usage domain, so the same number is never computed in
        two places; this page keeps the index and each session's own detail.
        """
        active = filters or {}
        return [
            self._sessions_collection(
                model=(active.get("model") or "").strip() or None,
                provider=(active.get("provider") or "").strip() or None,
            ),
            self._messages_collection(),
        ]

    def detail(self, record_id: str) -> Record | None:
        """One session, as a detail page."""
        con, error = self._open()
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
            links=((search_url(record.id), "Search this id"),),
            body="\n".join(n for n in notes if n),
        )

    def detail_sections(self, record_id: str) -> Sequence[Collection]:
        """Behind one session: its messages and its per-model usage."""
        con, error = self._open()
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
            sources=self._sources(),
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
            sources=self._sources(),
            notes=tuple(n for n in (error, usage_error) if n),
            as_of=as_of(),
        )
        return [messages, usage]

    def search(self, needle: str, limit: int) -> Sequence[Record]:
        """Search message text through the FTS index, then session titles.

        Messages come first because they are the content: a hit is an excerpt *around*
        the match with the terms marked, ranked by relevance (``bm25``) rather than by
        recency, and each row names the index that answered -- ``messages_fts``, the
        trigram index when a substring was asked for, or ``like`` when neither exists.
        """
        term = needle.strip()
        if not term:
            return []
        con, _error = self._open()
        records: list[Record] = []

        hits, index_used, _note = fts.search_messages(con, term, limit)
        for hit in hits:
            role = str(hit.get("role") or "?")
            tool = str(hit.get("tool_name") or "").strip()
            session_id = str(hit.get("session_id") or "")
            records.append(
                Record(
                    id=f"message-{hit.get('id')}",
                    title=f"{role} message" + (f" · {tool}" if tool else ""),
                    subtitle=hit["excerpt"],
                    badges=(
                        "message",
                        f"via {index_used}" if index_used else "message",
                    )
                    + (("tool call",) if tool else ()),
                    fields=(("session", session_id), ("role", role)),
                    links=((detail_url("sessions", session_id), "Open session"),)
                    if session_id
                    else (),
                )
            )

        session_columns = select_columns(con, "sessions", SESSION_COLUMNS)
        if "title" in session_columns and len(records) < limit:
            rows, _sql_error = query(
                con,
                f"select {', '.join(session_columns)} from sessions "
                f"where coalesce(title,'') || ' ' || coalesce(model,'') like ? "
                f"order by started_at desc limit ?",
                (f"%{term}%", limit - len(records)),
            )
            for row in rows:
                record = _session_record(row)
                records.append(
                    Record(
                        id=f"session-{record.id}",
                        title=record.title,
                        subtitle=record.subtitle,
                        badges=("session", "title match"),
                        links=((detail_url("sessions", record.id), "Open session"),),
                    )
                )
        _close(con)
        return records[:limit]


def build_domain(hermes_home: Path | None = None) -> Domain:
    """Build the 'sessions' domain.

    Args:
        hermes_home: Hermes home or profile directory.

    Returns:
        A :class:`~hermes.portal.model.Domain`.
    """
    return SessionsDomain(hermes_home).domain()
