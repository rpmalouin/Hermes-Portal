"""Health domain: is everything Hermes depends on alive, and what is running.

Six read-only sources, each named on the page:

* ``launchctl list`` plus ``~/Library/LaunchAgents/*.plist`` -- the services, what
  they run, whether they are loaded and what their last exit was.
* ``state.db``'s ``gateway_heartbeats`` -- the gateway's own heartbeat rows.
* ``lsof -nP -iTCP -sTCP:LISTEN`` -- who holds a TCP port right now.
* ``cron/ticker_*`` -- the cron scheduler's liveness stamps (plain Unix seconds).
* file sizes -- state.db, its WAL, snapshots, backups, the code graph and
  per-profile stores.
* a TCP probe of each service's declared ``--port``, to answer "declared vs
  actually listening" rather than assuming the plist is running.

Two rules hold: subprocesses are always argv lists through
:func:`hermes.portal.sources.run_argv` (never a shell), and a missing tool or
unreadable file becomes a note on its own collection instead of a broken page.
"""

from __future__ import annotations

import plistlib
import socket
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..model import (
    Collection,
    Count,
    Domain,
    Record,
    Source,
    build_collection,
    count_map,
)
from ..sources import (
    age_seconds,
    as_of,
    cron_dir,
    fmt_ago,
    fmt_time,
    hermes_root,
    human_size,
    open_sqlite,
    path_source,
    query,
    run_argv,
    scalar,
    scrub,
    snippet,
    tail_text,
    unreadable,
)
from .base import SnapshotDomain

STALE_MINUTES = 10
LISTENER_TIMEOUT = 0.4
SERVICE_MATCH = ("hermes", "nousresearch")
LOG_TAIL_LINES = 40
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
NOISE = (
    "lsof also lists unrelated system processes; the addresses are shown unedited "
    "so the list can be checked rather than trusted"
)


def _close(con: Any) -> None:
    """Close a connection if one was opened."""
    if con is not None:
        con.close()


def _stale_badge(minutes: float | None) -> tuple[str, ...]:
    """Return a badge tuple describing how stale a stamp is."""
    if minutes is None:
        return ("unknown age",)
    if minutes <= STALE_MINUTES:
        return (f"fresh ({minutes:.1f} min)",)
    return (f"STALE ({minutes:.0f} min)",)


def _probe(port: int) -> bool | None:
    """Return ``True`` if something accepts a TCP connection on 127.0.0.1:*port*.

    ``None`` means the probe could not be made at all.  A connect attempt is
    read-only and cheap, and it answers the question a plist cannot: is it up now.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(LISTENER_TIMEOUT)
            return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return None


def _declared_port(arguments: Sequence[str]) -> int | None:
    """Pull ``--port N`` out of a plist's ProgramArguments."""
    for index, argument in enumerate(arguments):
        if argument == "--port" and index + 1 < len(arguments):
            try:
                return int(str(arguments[index + 1]))
            except ValueError:
                return None
        if str(argument).startswith("--port="):
            try:
                return int(str(argument).split("=", 1)[1])
            except ValueError:
                return None
    return None


def _launchctl_state() -> tuple[dict[str, tuple[int | None, int | None]], str]:
    """Return ``{label: (pid, last_exit)}`` from ``launchctl list``."""
    output, error = run_argv(["launchctl", "list"], timeout=10.0)
    state: dict[str, tuple[int | None, int | None]] = {}
    for line in output.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid, status, label = parts[0].strip(), parts[1].strip(), parts[2].strip()
        try:
            pid_value = int(pid) if pid and pid != "-" else None
        except ValueError:
            pid_value = None
        try:
            exit_value = int(status)
        except ValueError:
            exit_value = None
        state[label] = (pid_value, exit_value)
    return state, error


def _plists() -> list[dict[str, Any]]:
    """Load every plist in the user's LaunchAgents directory."""
    loaded: list[dict[str, Any]] = []
    if not LAUNCH_AGENTS.is_dir():
        return loaded
    for path in sorted(LAUNCH_AGENTS.glob("*.plist")):
        try:
            with path.open("rb") as handle:
                data = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        if isinstance(data, dict):
            data["_path"] = str(path)
            loaded.append(data)
    return loaded


class HealthDomain(SnapshotDomain[None]):
    """Host facts: launchd services, heartbeat files, listening ports and disk.

    A class rather than a factory of closures, so every helper below can be called by
    name -- by a test, by a reader, and by the code graph.
    """

    key = "health"
    title = "Health"
    summary = "Services, gateway heartbeats, listening ports, tickers and storage."

    def __init__(self, hermes_home: Path | None = None) -> None:
        """Point at the sources; nothing is read until a page asks."""
        super().__init__(hermes_home)
        self.root = hermes_root(self.hermes_home)
        self.state_db = self.root / "state.db"
        self.cron_root = cron_dir(self.hermes_home)

    def _services_collection(self) -> Collection:
        """Hermes' launchd services: declared, loaded, and listening (or not)."""
        state, error = _launchctl_state()
        plists = [
            plist
            for plist in _plists()
            if any(
                token in str(plist.get("Label", "")).lower() for token in SERVICE_MATCH
            )
        ]
        notes: list[str] = []
        if error:
            notes.append(error)
        if not plists:
            notes.append(f"no matching plists in {LAUNCH_AGENTS}")
        records = []
        for plist in plists:
            label = str(plist.get("Label", "(unlabelled)"))
            arguments = [str(item) for item in plist.get("ProgramArguments", [])]
            pid, last_exit = state.get(label, (None, None))
            port = _declared_port(arguments)
            listening = _probe(port) if port else None
            running = pid is not None
            badges = ["running" if running else "not running"]
            if port:
                badges.append(
                    f"port {port}: "
                    + ("listening" if listening else "nothing listening")
                )
            if last_exit not in (None, 0):
                badges.append(f"last exit {last_exit}")
            records.append(
                Record(
                    id=label,
                    title=label,
                    subtitle=" ".join(arguments[:3]) or str(plist.get("_path", "")),
                    badges=tuple(badges),
                    fields=(
                        ("label", label),
                        (
                            "loaded",
                            "yes" if label in state else "not in launchctl list",
                        ),
                        ("pid", str(pid) if pid else "\u2014"),
                        (
                            "last exit",
                            str(last_exit) if last_exit is not None else "\u2014",
                        ),
                        ("command", " ".join(arguments)),
                        ("declared port", str(port) if port else "\u2014"),
                        (
                            "listening now",
                            "yes"
                            if listening
                            else "no"
                            if listening is False
                            else "unknown",
                        ),
                        ("RunAtLoad", str(plist.get("RunAtLoad"))),
                        ("KeepAlive", str(plist.get("KeepAlive"))),
                        ("throttle (s)", str(plist.get("ThrottleInterval"))),
                        ("working dir", str(plist.get("WorkingDirectory"))),
                        ("stdout", str(plist.get("StandardOutPath"))),
                        ("stderr", str(plist.get("StandardErrorPath"))),
                        ("plist", str(plist.get("_path"))),
                    ),
                )
            )
        if not records and state:
            notes.append(
                f"launchctl knows {len(state)} services but none match "
                f"{', '.join(SERVICE_MATCH)}"
            )
        return build_collection(
            "services",
            "Services",
            "The launchd jobs that keep Hermes running, with their live state.",
            "launchd jobs whose label mentions hermes or nousresearch",
            records,
            sources=(
                path_source("LaunchAgents", LAUNCH_AGENTS, note="plist definitions"),
                Source("launchctl list", "launchctl list", True, "live state"),
            ),
            extra_counts=(
                Count(
                    sum(1 for record in records if "running" in record.badges),
                    "running",
                ),
                Count(
                    sum(1 for record in records if "not running" in record.badges),
                    "not running",
                ),
                Count(len(state), "services known to launchctl in total"),
            ),
            notes=tuple(notes),
            as_of=as_of(),
        )

    def _heartbeats_collection(self) -> Collection:
        """The gateway's own heartbeat rows, newest first."""
        con, error = open_sqlite(self.state_db)
        total = scalar(con, "select count(*) from gateway_heartbeats", default=0)
        rows, sql_error = query(
            con,
            "select backend_id, pid, profile, host, started_at, last_heartbeat "
            "from gateway_heartbeats order by last_heartbeat desc",
        )
        _close(con)
        ages = [age_seconds(row["last_heartbeat"]) for row in rows]
        stale = [age for age in ages if age is not None and age > STALE_MINUTES * 60]
        return build_collection(
            "heartbeats",
            "Gateway heartbeats",
            "One row per backend the gateway has advertised, newest heartbeat first.",
            "rows in state.db gateway_heartbeats",
            [
                Record(
                    id=str(row["backend_id"]),
                    title=str(row["backend_id"]),
                    subtitle=f"pid {row['pid']} on {row['host']} · "
                    f"profile {row['profile']}",
                    badges=_stale_badge(
                        (age_seconds(row["last_heartbeat"]) or 0) / 60
                        if age_seconds(row["last_heartbeat"]) is not None
                        else None
                    ),
                    fields=(
                        ("backend", str(row["backend_id"])),
                        ("pid", str(row["pid"])),
                        ("profile", str(row["profile"])),
                        ("host", str(row["host"])),
                        ("started", fmt_time(row["started_at"])),
                        (
                            "last heartbeat",
                            f"{fmt_time(row['last_heartbeat'])} "
                            f"({fmt_ago(row['last_heartbeat'])})",
                        ),
                    ),
                )
                for row in rows
            ],
            cap=20,
            sources=(path_source("state.db", self.state_db, note="read-only"),),
            extra_counts=(
                Count(total, "heartbeat rows in total"),
                Count(
                    sum(
                        1
                        for age in ages
                        if age is not None and age <= STALE_MINUTES * 60
                    ),
                    f"heard from in the last {STALE_MINUTES} min",
                ),
                Count(len(stale), f"stale (over {STALE_MINUTES} min)"),
            ),
            notes=tuple(note for note in (error, sql_error) if note)
            + (
                "the heartbeats table accumulates a row per backend start, so many "
                "are historical",
            ),
            as_of=as_of(),
            unavailable=unreadable(error, self.state_db),
        )

    def _ports_collection(self) -> Collection:
        """TCP listeners, via lsof."""
        output, error = run_argv(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], timeout=20.0)
        rows = [line for line in output.splitlines() if line.strip()][1:]
        records = []
        for line in rows:
            parts = line.split()
            if len(parts) < 9:
                continue
            command, pid, user = parts[0], parts[1], parts[2]
            address = parts[-2] if parts[-1] == "(LISTEN)" else parts[-1]
            records.append(
                Record(
                    id=f"{pid}-{address}",
                    title=f"{command} on {address}",
                    subtitle=f"pid {pid}, user {user}",
                    badges=(command, address.rsplit(":", 1)[-1]),
                    fields=(
                        ("command", command),
                        ("pid", pid),
                        ("user", user),
                        ("address", address),
                    ),
                )
            )
        notes = [note for note in (error,) if note]
        notes.append(NOISE)
        return build_collection(
            "ports",
            "Listening ports",
            "Every TCP socket in listen state on this machine.",
            "listeners reported by lsof -nP -iTCP -sTCP:LISTEN",
            records,
            sources=(
                Source("lsof", "lsof -nP -iTCP -sTCP:LISTEN", not error, "live state"),
            ),
            notes=tuple(notes),
            as_of=as_of(),
        )

    def _storage_collection(self) -> Collection:
        """The stores Hermes grows: state, snapshots, backups, graph, cron."""
        targets: list[tuple[str, Path, str]] = [
            ("state.db", self.state_db, "sessions, messages, usage, heartbeats"),
            (
                "state.db-wal",
                self.state_db.with_name(self.state_db.name + "-wal"),
                "write-ahead log",
            ),
            (
                "cron/executions.db",
                self.cron_root / "executions.db",
                "cron run history",
            ),
            ("snapshots", self.root / "state-snapshots", "pre-update state snapshots"),
            ("backups", self.root / "backups", "config and vault backups"),
            (
                "code graph",
                self.root / ".code-review-graph" / "graph.db",
                "code-review-graph",
            ),
            ("logs", self.root / "logs", "agent and gateway logs"),
        ]
        records = []
        for label, path, note in targets:
            if not path.exists():
                continue
            if path.is_file():
                size = path.stat().st_size
            else:
                size = sum(
                    entry.stat().st_size for entry in path.rglob("*") if entry.is_file()
                )
            records.append(
                Record(
                    id=label,
                    title=label,
                    subtitle=f"{human_size(size)} · {note}",
                    badges=(human_size(size),),
                    fields=(
                        ("path", str(path)),
                        ("size", human_size(size)),
                        ("what", note),
                        ("exists", "yes"),
                    ),
                )
            )
        for profile in sorted((self.root / "profiles").glob("*/state.db")):
            records.append(
                Record(
                    id=f"profile {profile.parent.name}",
                    title=f"profile {profile.parent.name}",
                    subtitle=f"{human_size(profile.stat().st_size)} · "
                    "its own session store",
                    badges=(human_size(profile.stat().st_size),),
                    fields=(
                        ("path", str(profile)),
                        ("size", human_size(profile.stat().st_size)),
                        ("what", "per-profile state store"),
                        ("exists", "yes"),
                    ),
                )
            )
        total = sum(
            entry.stat().st_size
            for label, path, _note in targets
            if path.exists() and path.is_file()
            for entry in [path]
        )
        return build_collection(
            "storage",
            "Storage",
            "What Hermes keeps on disk, so growth is visible before it is a problem.",
            "stores present under the Hermes self.root",
            records,
            sources=(path_source("Hermes self.root", self.root),),
            metrics=(("Databases measured here", human_size(total)),),
            notes=(
                "directory sizes are summed recursively and can take a moment on the "
                "bigger trees",
            ),
            as_of=as_of(),
        )

    def _tickers_collection(self) -> Collection:
        """The cron scheduler's liveness stamps."""
        records = []
        for label, path in (
            ("ticker heartbeat", self.cron_root / "ticker_heartbeat"),
            ("last successful tick", self.cron_root / "ticker_last_success"),
        ):
            age = None
            if path.is_file():
                try:
                    age = age_seconds(path.read_text().strip())
                except OSError:
                    age = None
            records.append(
                Record(
                    id=label,
                    title=label,
                    subtitle=fmt_ago(path.stat().st_mtime)
                    if path.is_file()
                    else "missing",
                    badges=_stale_badge(age / 60 if age is not None else None),
                    fields=(
                        ("file", str(path)),
                        ("age", f"{age:.0f}s" if age is not None else "unknown"),
                        (
                            "modified",
                            fmt_ago(path.stat().st_mtime)
                            if path.is_file()
                            else "\u2014",
                        ),
                    ),
                )
            )
        return build_collection(
            "tickers",
            "Cron ticker",
            "The scheduler writes these stamps as it runs; a stale one means "
            "cron is stuck.",
            "ticker stamp files under cron/",
            records,
            sources=(path_source("cron store", self.cron_root),),
            as_of=as_of(),
        )

    def overview(self) -> Collection:
        """Headline health: what is running, what is stale."""
        services = self._services_collection()
        heartbeats = self._heartbeats_collection()
        tickers = self._tickers_collection()
        running = count_map(services.extra_counts).get("running", 0)
        stale = count_map(heartbeats.extra_counts).get(
            f"stale (over {STALE_MINUTES} min)", 0
        )
        heartbeat = next(
            (record for record in tickers.records if record.id == "ticker heartbeat"),
            None,
        )
        last_success = next(
            (
                record
                for record in tickers.records
                if record.id == "last successful tick"
            ),
            None,
        )
        # liveness is the heartbeat; last_success is a last-run record, so it is
        # allowed to be hours old without meaning anything is wrong
        beat_stale = heartbeat is not None and "STALE" in " ".join(heartbeat.badges)
        success_age = ""
        if last_success is not None:
            success_age = dict(last_success.fields).get("age", "")
        return build_collection(
            "overview",
            "Health",
            "Whether the services, the gateway and the scheduler are alive right now.",
            "Hermes services found on this machine",
            services.records,
            cap=3,
            sources=services.sources,
            extra_counts=(
                Count(running, "services running"),
                Count(stale, f"heartbeats staler than {STALE_MINUTES} min"),
            ),
            metrics=(
                ("Services running", str(running)),
                ("Stale heartbeats", str(stale)),
                ("Cron ticker", "stale" if beat_stale else "fresh"),
                ("Last successful tick", success_age or "\u2014"),
            ),
            notes=tuple(services.notes[:1]) + tuple(heartbeats.notes[:1]),
            as_of=as_of(),
        )

    def collections(
        self, _filters: Mapping[str, str] | None = None
    ) -> Sequence[Collection]:
        """Drill-down collections for health."""
        return [
            self._services_collection(),
            self._heartbeats_collection(),
            self._ports_collection(),
            self._tickers_collection(),
            self._storage_collection(),
        ]

    def detail(self, record_id: str) -> Record | None:
        """A service, a backend or a store."""
        for collection in self.collections():
            for record in collection.records:
                if record.id == record_id and record.fields:
                    return record
        return None

    def detail_sections(self, record_id: str) -> Sequence[Collection]:
        """Behind a service: the tail of the log its plist points at."""
        plists = [plist for plist in _plists() if str(plist.get("Label")) == record_id]
        if not plists:
            return []
        plist = plists[0]
        sections = []
        for label, key in (
            ("stdout", "StandardOutPath"),
            ("stderr", "StandardErrorPath"),
        ):
            raw = plist.get(key)
            if not raw:
                continue
            path = Path(str(raw))
            text, truncated, error = tail_text(path, 60_000)
            lines = text.splitlines()[-LOG_TAIL_LINES:]
            sections.append(
                build_collection(
                    label,
                    f"{label} tail",
                    f"The last {LOG_TAIL_LINES} lines of this service's {label} log.",
                    f"most recent lines of {key}",
                    [
                        Record(
                            id=f"{label}-{index}",
                            title=snippet(scrub(line.strip()), 150) or "(blank)",
                            body=scrub(line),
                        )
                        for index, line in enumerate(lines)
                    ],
                    sources=(path_source(label, path, note="log file"),),
                    notes=tuple(note for note in (error,) if note)
                    + (
                        "only the tail of the file is read"
                        + ("; it was longer than the window" if truncated else ""),
                    ),
                    as_of=as_of(),
                )
            )
        return sections

    def search(self, needle: str, limit: int) -> Sequence[Record]:
        """Find a service, backend or listener by name."""
        term = needle.strip().lower()
        if not term:
            return []
        hits: list[Record] = []
        for collection in (
            self._services_collection(),
            self._heartbeats_collection(),
            self._ports_collection(),
        ):
            for record in collection.records:
                haystack = f"{record.title} {record.subtitle}".lower()
                if term in haystack:
                    hits.append(
                        Record(
                            id=record.id,
                            title=record.title,
                            subtitle=snippet(record.subtitle, 120),
                            badges=("health",) + record.badges[:1],
                        )
                    )
                if len(hits) >= limit:
                    return hits
        return hits


def build_domain(hermes_home: Path | None = None) -> Domain:
    """Build the 'health' domain.

    Args:
        hermes_home: Hermes home or profile directory.

    Returns:
        A :class:`~hermes.portal.model.Domain`.
    """
    return HealthDomain(hermes_home).domain()
