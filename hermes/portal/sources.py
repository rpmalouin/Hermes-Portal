"""Read-only access to the places Hermes keeps its truth.

Every adapter goes through here, so the safety properties live in one place:

* SQLite is opened with ``mode=ro`` (plus ``query_only``), so the portal can
  never take a write lock on a database the running agent is using -- including
  ``state.db``, which is live and 126 MB with a WAL beside it.
* Missing files, unreadable files and SQL errors are returned as *data*
  (``(None, "message")`` or a note), never raised, so one absent source degrades
  a single collection instead of the whole portal.
* Timestamps are stamped once per read (:func:`as_of`) in UTC, because a page
  that cannot say when it looked is a page nobody should trust.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .model import Source, scrub  # noqa: F401 - re-exported, adapters import it here


def hermes_home() -> Path:
    """Return the running Hermes' home directory.

    ``$HERMES_HOME`` wins when set -- inside a Hermes session it points at the
    running profile directory -- otherwise ``~/.hermes``.  The same rule the
    Skill Deck uses, kept here so adapters do not each reinvent it.
    """
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def as_of() -> str:
    """Return the current UTC time as an ISO-8601 string (seconds precision)."""
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def human_size(num_bytes: int | float) -> str:
    """Format a byte count as B/KB/MB/GB."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def path_source(label: str, path: Path, note: str = "") -> Source:
    """Describe *path* as a source, with its size when it exists."""
    exists = path.exists()
    detail = note
    if exists:
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        detail = f"{human_size(size)}" if path.is_file() else "directory"
        if note:
            detail = f"{detail}; {note}"
    else:
        detail = "missing"
    return Source(label=label, location=str(path), present=exists, detail=detail)


def open_sqlite(path: Path) -> tuple[sqlite3.Connection | None, str]:
    """Open *path* read-only.

    Args:
        path: SQLite database file.

    Returns:
        ``(connection, "")`` on success, or ``(None, message)`` when the file is
        absent or cannot be opened.  Never raises.
    """
    db_path = Path(path)
    if not db_path.is_file():
        return None, f"database not found: {db_path}"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        con.execute("pragma query_only = 1")
    except sqlite3.Error as exc:
        return None, f"cannot open {db_path} read-only: {exc}"
    return con, ""


def query(
    con: sqlite3.Connection | None,
    sql: str,
    params: Sequence[Any] = (),
) -> tuple[list[sqlite3.Row], str]:
    """Run *sql*, returning ``(rows, "")`` or ``([], message)``.  Never raises."""
    if con is None:
        return [], "no database connection"
    try:
        return list(con.execute(sql, tuple(params))), ""
    except sqlite3.Error as exc:
        return [], f"query failed: {exc}"


def scalar(
    con: sqlite3.Connection | None,
    sql: str,
    params: Sequence[Any] = (),
    default: Any = None,
) -> Any:
    """Return the first column of the first row of *sql*, or *default*."""
    rows, _ = query(con, sql, params)
    if not rows:
        return default
    return rows[0][0]


def hermes_root(home: Path | None = None) -> Path:
    """Return the Hermes *root*, even when ``$HERMES_HOME`` is a profile.

    ``state.db`` and ``cron/`` live at the root (``~/.hermes``) while a live
    session's ``$HERMES_HOME`` points at ``~/.hermes/profiles/<name>``, so
    adapters ask here instead of guessing which one they were handed.
    """
    base = Path(home).expanduser() if home is not None else hermes_home()
    for candidate in (base, base.parent.parent, base.parent):
        # A profile carries its own state.db and cron/ dir, so `cron/jobs.json`
        # is the marker that actually separates the root from a profile.
        if (candidate / "cron" / "jobs.json").is_file():
            return candidate
    for candidate in (base, base.parent.parent, base.parent):
        if (candidate / "state.db").exists():
            return candidate
    return base


def state_db(home: Path | None = None) -> Path:
    """Path to the SQLite state store (sessions, messages, usage, heartbeats)."""
    return hermes_root(home) / "state.db"


def cron_dir(home: Path | None = None) -> Path:
    """Path to the cron store (``jobs.json``, ``executions.db``, ``output/``)."""
    return hermes_root(home) / "cron"


def table_columns(con: sqlite3.Connection | None, table: str) -> set[str]:
    """Return the column names of *table* (empty when the table is absent)."""
    rows, _error = query(con, f'pragma table_info("{table}")')
    return {row["name"] for row in rows}


def select_columns(
    con: sqlite3.Connection | None, table: str, wanted: Sequence[str]
) -> list[str]:
    """Return the subset of *wanted* that actually exists in *table*.

    Hermes' schema moves between versions, so an adapter selects what it finds
    rather than assuming a shape; a missing column becomes a missing detail row,
    not a stack trace.
    """
    available = table_columns(con, table)
    return [column for column in wanted if column in available]


class Cache:
    """A tiny TTL cache for the few reads too slow to run on every request.

    Values are returned with the moment they were computed, so a page can say
    *when* a number was true instead of implying it is true now.  A miss computes
    once under a lock, so concurrent requests do not each pay the price.
    """

    def __init__(self) -> None:
        """Create an empty cache."""
        self._lock = threading.Lock()
        self._values: dict[str, tuple[float, Any, str]] = {}

    def peek(self, key: str, ttl: float) -> tuple[Any | None, str]:
        """Return ``(value, computed_at)`` without computing, or ``(None, "")``."""
        now = time.monotonic()
        with self._lock:
            cached = self._values.get(key)
            if cached is not None and now - cached[0] < ttl:
                return cached[1], cached[2]
            return None, ""

    def get(
        self,
        key: str,
        ttl: float,
        producer: Callable[[], Any],
    ) -> tuple[Any, str]:
        """Return ``(value, computed_at)`` for *key*, computing when stale.

        Args:
            key: Cache key.
            ttl: Seconds a value stays valid.
            producer: Called with no arguments to compute the value.
        """
        now = time.monotonic()
        with self._lock:
            cached = self._values.get(key)
            if cached is not None and now - cached[0] < ttl:
                return cached[1], cached[2]
            value = producer()
            stamp = as_of()
            self._values[key] = (now, value, stamp)
            return value, stamp


def inside_tree(path: Path, root: Path) -> bool:
    """Return ``True`` when *path*, resolved, still lives under *root*.

    The tree-walking readers use this to refuse a way out of the tree they were
    pointed at: a symlink -- a file, or a directory full of them -- is otherwise a
    door out of the vault (or the log directory) and into whatever it points at.
    A link that stays inside the tree is fine, which matters here because one of
    the trees the portal reads (a profile's ``skills/``) is made of symlinks.
    """
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, RuntimeError):  # a vanished parent, a permission wall, a loop
        return False


def age_seconds(timestamp: float | str | None) -> float | None:
    """Seconds since *timestamp* (Unix seconds or ISO-8601), or ``None``."""
    if timestamp is None or timestamp == "":
        return None
    moment: dt.datetime
    try:
        moment = dt.datetime.fromtimestamp(float(timestamp), tz=dt.UTC)
    except (TypeError, ValueError):
        try:
            moment = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - moment).total_seconds()


def run_argv(argv: Sequence[str], timeout: float = 15.0) -> tuple[str, str]:
    """Run a read-only command and return ``(stdout, error)``.

    argv is always a list: ``shell=True`` is never used anywhere in this project.
    A failure comes back as an error string so a missing tool degrades one
    collection instead of the page.
    """
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"{' '.join(argv)} failed: {type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:200]
        return completed.stdout, f"{argv[0]} exited {completed.returncode}: {detail}"
    return completed.stdout, ""


def tail_text(path: Path, window_bytes: int = 200_000) -> tuple[str, bool, str]:
    """Return the last *window_bytes* of a file, decoded.

    Logs are read from the end only: a 5 MB gateway log must never be pulled into
    memory in full just to show its most recent failure.

    Returns:
        ``(text, truncated, error)`` -- *truncated* says the file is longer than
        the window that was read.
    """
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return "", False, f"cannot stat {file_path}: {exc}"
    try:
        with file_path.open("rb") as handle:
            truncated = size > window_bytes
            if truncated:
                handle.seek(size - window_bytes)
            raw = handle.read(window_bytes)
    except OSError as exc:
        return "", False, f"cannot read {file_path}: {exc}"
    return raw.decode("utf-8", errors="replace"), truncated, ""


def glob_files(root: Path, patterns: Sequence[str]) -> list[Path]:
    """Return the files under *root* matching any of *patterns*, biggest first."""
    found: list[Path] = []
    for pattern in patterns:
        found.extend(path for path in Path(root).glob(pattern) if path.is_file())
    # a log file outside the log tree (a link into /var/log, say) is not one of ours
    found = [path for path in found if inside_tree(path, root)]
    unique = {path.resolve(): path for path in found}
    return sorted(unique.values(), key=lambda path: -path.stat().st_size)


def read_json(path: Path) -> tuple[Any | None, str]:
    """Read and decode a JSON file, returning ``(data, "")`` or ``(None, error)``."""
    try:
        # errors="replace" like every other read: a manifest with one bad byte
        # should cost that file's value, not the whole collection's
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError as exc:
        return None, f"malformed JSON in {path}: {exc}"


def read_text(path: Path, limit: int = 4000) -> tuple[str, bool, str]:
    """Read a text file, capped at *limit* characters.

    Returns:
        ``(text, truncated, error)``; *text* is empty when *error* is set.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "", False, f"cannot read {path}: {exc}"
    if len(raw) > limit:
        return raw[:limit], True, ""
    return raw, False, ""


def to_datetime(timestamp: float | str | None) -> dt.datetime | None:
    """A stamp as an aware datetime, or None if it is not a stamp at all.

    Both shapes live in these stores, sometimes in the same column: cron's
    ``executions.db`` writes ISO-8601 strings while older rows and other stores hold
    epoch seconds.  Every reader must accept both -- reading one shape with ``float()``
    is what made every cron run report a missing duration, because the ValueError was
    caught and the row silently lost its length instead of the call failing.
    """
    if timestamp is None or timestamp == "":
        return None
    try:
        moment = dt.datetime.fromtimestamp(float(timestamp), tz=dt.UTC)
    except (TypeError, ValueError):
        try:
            moment = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment


def fmt_time(timestamp: float | str | None) -> str:
    """Format a Unix timestamp (or ISO string) for display, in local time."""
    if timestamp is None or timestamp == "":
        return "—"
    moment = to_datetime(timestamp)
    if moment is None:
        return str(timestamp)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def fmt_ago(timestamp: float | str | None) -> str:
    """Format a Unix timestamp or an ISO-8601 string as a coarse age like ``3h ago``."""
    if timestamp is None or timestamp == "":
        return "\u2014"
    moment: dt.datetime
    try:
        moment = dt.datetime.fromtimestamp(float(timestamp), tz=dt.UTC)
    except (TypeError, ValueError):
        try:
            moment = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return "\u2014"
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
    seconds = (dt.datetime.now(dt.UTC) - moment).total_seconds()
    if seconds < 0:
        return "in the future"
    for span, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= span:
            return f"{int(seconds // span)}{unit} ago"
    return f"{int(seconds)}s ago"


def fmt_duration(seconds: float | None) -> str:
    """Format a duration in seconds as ``1h 02m`` / ``3m 04s`` / ``800ms``."""
    if seconds is None:
        return "—"
    total = float(seconds)
    if total < 1:
        return f"{total * 1000:.0f}ms"
    if total < 60:
        return f"{total:.1f}s"
    minutes, secs = divmod(int(total), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def truncate(text: str, limit: int) -> str:
    """Collapse whitespace and cut *text* to *limit* characters with an ellipsis."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "\u2026"


def snippet(text: str, limit: int = 160) -> str:
    """Return a one-line preview of *text*."""
    return truncate(text, limit)
