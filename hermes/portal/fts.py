"""Full-text search over ``state.db``'s message index, with excerpts that show the hit.

``state.db`` ships FTS5 tables over ``messages`` -- ``messages_fts`` (word tokens) and
usually ``messages_fts_trigram`` (substrings) -- and the sessions domain has always
queried them.  But the preview it showed was the message's *first* 200 characters, so a
hit could display a paragraph that never mentions what you searched for; and the query
was a quoted phrase, so ``dashboard parser`` found nothing while ``"dashboard parser"``
found the one message that says it.

This module owns the three parts that are easy to get wrong:

* **the query.**  A search box is not FTS5 syntax.  User text becomes a sanitised MATCH
  expression -- terms AND-ed, each a prefix match -- with every operator-ish character
  neutralised, so ``-``, ``"``, ``*``, ``:`` or ``NEAR(`` can neither raise a syntax
  error nor quietly mean something else.
* **the excerpt.**  ``snippet()`` around the match, with private markers around the
  matched terms; the renderer escapes the whole string and only then turns the markers
  into markup, so highlighting cannot inject anything into a page.
* **the fallback chain.**  Word index -> trigram index (substrings like ``EADDRIN``) ->
  ``LIKE`` when neither index exists, each reported by name so a reader knows which one
  answered.

Read-only throughout: the connection is handed in already opened ``mode=ro`` with
``query_only`` set, and nothing here writes.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

WORD_INDEX = "messages_fts"
SUBSTRING_INDEX = "messages_fts_trigram"
INDEXES = (WORD_INDEX, SUBSTRING_INDEX)
HIT_OPEN = "\x02"
HIT_CLOSE = "\x03"
SNIPPET_TOKENS = 14
TERM_LIMIT = 8
TERM_CHARS = 40
# Everything unprintable goes, *except* the two hit markers this module inserts.  The
# class has to skip both: a range like \x03-\x08 still starts at the closing marker, so
# an earlier version stripped every closing marker and left the opening one, which
# highlighted to the end of the excerpt.
CONTROL_CHARS = re.compile(r"[\x00-\x01\x04-\x08\x0b-\x1f\x7f]")
WORD_CHARS = re.compile(r"[^\w]+", re.UNICODE)


class SearchUnavailable(RuntimeError):
    """No index and no ``messages`` table: the caller reports it rather than raising."""


def terms_in(text: str) -> tuple[str, ...]:
    """Split user input into search terms, dropping anything that is not a word.

    Operator-ish characters are the point: ``-extra``, ``"phrase"``, ``a*``, ``NEAR(x``
    and ``col:value`` must all be read as ordinary text, so punctuation becomes a
    separator and only word characters survive.
    """
    cleaned = CONTROL_CHARS.sub(" ", text or "")
    parts = [part for part in WORD_CHARS.split(cleaned) if part]
    trimmed = tuple(part[:TERM_CHARS] for part in parts[:TERM_LIMIT])
    return tuple(part for part in trimmed if part)


def build_match(text: str) -> tuple[str, tuple[str, ...]]:
    """Turn user input into ``(match_expression, terms)``.

    Each term is quoted and prefix-matched, and the terms are AND-ed: searching
    ``dashboard parser`` asks for messages containing both, which is what a search box
    means.  Returns ``("", ())`` when there is nothing searchable.
    """
    terms = terms_in(text)
    if not terms:
        return "", ()
    expression = " AND ".join(f'"{term}"*' for term in terms)
    return expression, terms


def substring_expression(text: str) -> str:
    """A quoted expression for the trigram index, or ``""`` when it is too short.

    The trigram tokenizer needs at least three characters, and it is the only index that
    finds a substring inside a word.
    """
    cleaned = CONTROL_CHARS.sub(" ", text or "").strip()
    if len(cleaned) < 3:
        return ""
    return '"' + cleaned.replace('"', '""') + '"'


def available_indexes(con: sqlite3.Connection | None) -> tuple[str, ...]:
    """Which FTS tables exist, word index first."""
    if con is None:
        return ()
    found: list[str] = []
    for name in INDEXES:
        try:
            con.execute(f"select count(*) from {name} limit 1").fetchone()
        except sqlite3.Error:
            continue
        found.append(name)
    return tuple(found)


def _excerpt(raw: str) -> str:
    """Strip unprintable characters and collapse whitespace, keeping the hit markers."""
    return " ".join(CONTROL_CHARS.sub("", raw or "").split())


def as_dict(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    """A row as a dict, whether or not the connection set a row factory."""
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}  # noqa: SIM118 - sqlite3.Row
    return dict(zip(columns, row, strict=False))


def _message_row(row: Any, index_used: str) -> dict[str, Any]:
    """Shape one search hit."""
    snippet_text = _excerpt(str(row["excerpt"] or ""))
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "role": row["role"],
        "tool_name": row["tool_name"],
        "timestamp": row["timestamp"],
        "excerpt": snippet_text,
        "index_used": index_used,
        "score": float(row["score"]) if "score" in keys else None,
    }


def search_messages(
    con: sqlite3.Connection | None,
    text: str,
    limit: int,
    *,
    indexes: tuple[str, ...] | None = None,
) -> tuple[list[dict[str, Any]], str, str]:
    """Search messages, returning ``(rows, index_used, note)``.

    The word index answers first, ranked by relevance (``bm25``).  When it finds
    nothing, the trigram index is asked for the input as a substring, because a
    reader typing ``EADDRIN`` means "find this text", not "find this word".  With no
    index at all the query degrades to ``LIKE``, and ``note`` says which answered.

    """
    if con is None:
        return [], "", "no state.db to search"
    present = indexes if indexes is not None else available_indexes(con)
    wanted = text.strip()
    if not wanted or not limit:
        return [], "", ""
    columns = _message_columns(con)

    if WORD_INDEX in present:
        expression, _terms = build_match(wanted)
        if expression:
            rows = _match(con, WORD_INDEX, expression, limit)
            if rows:
                return (
                    [_message_row(row, WORD_INDEX) for row in rows],
                    WORD_INDEX,
                    "",
                )

    if SUBSTRING_INDEX in present:
        expression = substring_expression(wanted)
        if expression:
            rows = _match(con, SUBSTRING_INDEX, expression, limit)
            if rows:
                return (
                    [_message_row(row, SUBSTRING_INDEX) for row in rows],
                    SUBSTRING_INDEX,
                    f"{SUBSTRING_INDEX} answered: the word index had no match for "
                    f"{wanted!r}, so this is a substring search",
                )

    if columns:
        rows = _like(con, wanted, limit, columns)
        if rows:
            return (
                [_message_row(row, "like") for row in rows],
                "like",
                "no FTS index answered, so this fell back to a LIKE scan of messages",
            )
    return [], "", f"nothing matched {wanted!r}"


def _match(
    con: sqlite3.Connection, index: str, expression: str, limit: int
) -> list[Any]:
    """Run one MATCH query with an excerpt around the hit; never raises."""
    if not _has_message_join(con):
        return []
    sql = (
        "select m.id as id, m.session_id as session_id, m.role as role, "
        "m.tool_name as tool_name, m.timestamp as timestamp, "
        f"snippet({index}, 0, ?, ?, ' \u2026 ', {SNIPPET_TOKENS}) as excerpt, "
        f"bm25({index}) as score "
        f"from {index} f join messages m on m.id = f.rowid "
        f"where {index} match ? order by rank limit ?"
    )
    try:
        return con.execute(sql, (HIT_OPEN, HIT_CLOSE, expression, limit)).fetchall()
    except sqlite3.Error:
        return []


def _like(
    con: sqlite3.Connection, text: str, limit: int, columns: tuple[str, ...]
) -> list[Any]:
    """Last resort: a substring scan when no index exists."""
    select = ", ".join(f"m.{name} as {name}" for name in columns if name != "content")
    sql = (
        f"select {select}, m.content as excerpt from messages m "
        f"where m.content like ? order by m.timestamp desc limit ?"
    )
    try:
        return con.execute(sql, (f"%{text}%", limit)).fetchall()
    except sqlite3.Error:
        return []


def _message_columns(con: sqlite3.Connection) -> tuple[str, ...]:
    """The message columns that exist, so a narrow fixture still works."""
    wanted = ("id", "session_id", "role", "tool_name", "content", "timestamp")
    try:
        present = {str(row[1]) for row in con.execute("pragma table_info(messages)")}
    except sqlite3.Error:
        return ()
    return tuple(name for name in wanted if name in present)


def _has_message_join(con: sqlite3.Connection) -> bool:
    """Whether the messages table can be joined to the index by rowid."""
    columns = _message_columns(con)
    return "id" in columns and "content" in columns


def recent_messages(
    con: sqlite3.Connection | None, limit: int
) -> tuple[list[dict[str, Any]], str]:
    """The newest messages, for browsing rather than searching.

    No markers here: an excerpt is only highlighted when it was produced by a query.
    """
    if con is None:
        return [], "no state.db to read"
    columns = _message_columns(con)
    if not columns:
        return [], "no messages table"
    select = ", ".join(f"m.{name} as {name}" for name in columns)
    sql = f"select {select} from messages m order by m.timestamp desc limit ?"
    try:
        rows = con.execute(sql, (limit,)).fetchall()
    except sqlite3.Error as exc:
        return [], f"{type(exc).__name__}: {exc}"
    out: list[dict[str, Any]] = []
    for row in rows:
        row_dict = as_dict(row, columns)
        content = str(row_dict.get("content") or "")
        out.append(
            {
                "id": row_dict.get("id"),
                "session_id": row_dict.get("session_id"),
                "role": row_dict.get("role"),
                "tool_name": row_dict.get("tool_name"),
                "timestamp": row_dict.get("timestamp"),
                "excerpt": _excerpt(content[:400]),
                "index_used": "",
            }
        )
    return out, ""


def count_messages(con: sqlite3.Connection | None) -> int:
    """How many messages are in the table (0 when it cannot be read)."""
    if con is None:
        return 0
    try:
        return int(con.execute("select count(*) from messages").fetchone()[0])
    except sqlite3.Error:
        return 0
