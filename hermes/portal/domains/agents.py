"""The agents: every profile Hermes has, and what each one's store says it has done.

The other nine domains each read one store, and nine of them read the *root's* slice:
the sessions under ``~/.hermes``, the cron under ``~/.hermes``, the default profile's
memories.  A Hermes home is usually several agents at once -- a root plus a directory
per profile, each with its own ``state.db``, its own ``cron/``, its own memories and
skills -- and nothing on the portal named them.  This is that page.

An agent is the root plus every directory :func:`hermes.core.skill_trees.profile_dirs`
returns: the shared answer to "is this a root or a profile", which is also why a hidden
directory never becomes an agent.  Hermes keeps retired profiles in ``.deleted``, and a
row for that would be a row answering nothing.

**What it leaves to the boxes that own it.**  Memory entries and skills are counted by
the memory and skills pages, per profile; a second number for the same set is how two
pages start disagreeing, so this one links to those views instead (``?profile=`` is a
filter both of them read).  ``config.yaml`` is not read at all: its platform block holds
a token per channel, and a credentialed file does not belong on a page -- an agent's
*channels* are therefore not here, only its identity and its activity.

**A job link is drawn only where a page exists.**  The cron domain reads the *root's*
``jobs.json``, so ``/cron/<id>`` resolves for a root job and 404s for a profile's.
Profile jobs are rendered by the cron adapter's own record builder with its links
removed: the row says what the cron page would say without promising a page that is not
there.

Every read keeps the portal's discipline: SQLite ``mode=ro``, narrow queries, and a
store that could not be read reports *that* rather than a zero -- an agent with no
``state.db`` has no session count, it does not have none.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core import skill_trees
from ..model import (
    Collection,
    Count,
    Domain,
    Record,
    Source,
    build_collection,
    detail_url,
    filter_url,
)
from ..sources import (
    as_of,
    fmt_ago,
    hermes_root,
    human_size,
    open_sqlite,
    path_source,
    query,
    scalar,
    select_columns,
    truncate,
    unreadable,
)
from . import cron as cron_domain
from .base import SnapshotDomain
from .memory import (
    _profile_label,  # noqa: SLF001 - one rule for naming a profile, not a second copy
)

#: How many agents a page lists, and how many rows one of an agent's sections shows.
AGENTS_CAP = 50
SESSION_CAP = 25

#: The columns this adapter reads out of an agent's store.  Declared here rather than
#: borrowed: a narrower read than the sessions page wants, and the doctor's contract
#: for an agent's store is built from this tuple.
AGENT_COLUMNS = ("title", "started_at", "message_count")

#: What says a backend last checked in, from the table the health page also reads.
HEARTBEAT_COLUMNS = ("backend_id", "last_heartbeat")


@dataclass(frozen=True)
class Agent:
    """One agent: where it lives, what its store holds, and how fresh that store is.

    The counts are ``0`` only when the store was read and is empty.  A store that could
    not be read leaves :attr:`store_readable` ``False`` and puts the reason in
    :attr:`store_error`, which is what the page shows in place of a number.
    """

    label: str
    base: Path
    is_root: bool
    store: Path
    store_readable: bool
    store_error: str
    store_size: int
    store_modified: float | None
    sessions: int = 0
    messages: int = 0
    latest_session: str = ""
    latest_at: str = ""
    heartbeats: int = 0
    last_heartbeat: str = ""
    jobs: tuple[dict[str, Any], ...] = ()
    jobs_error: str = ""


@dataclass(frozen=True)
class AgentIndex:
    """Every agent this machine has, and the moment the scan was taken."""

    root: Path
    agents: tuple[Agent, ...] = ()
    as_of: str = ""


def agent_homes(hermes_home: Path | None = None) -> list[tuple[str, Path, bool]]:
    """Every agent's home, the root first, as ``(label, home, is_root)``.

    ``$HERMES_HOME`` points at the root in some launches and at the running profile in
    others, so the list comes from ``skill_trees.profile_dirs`` rather than a local
    guess: the same resolution every other reader uses, and the one that skips hidden
    directories.
    """
    home = (
        Path(hermes_home)
        if hermes_home is not None
        else skill_trees.default_hermes_home()
    )
    root = hermes_root(hermes_home)
    homes: list[tuple[str, Path, bool]] = [(_profile_label(root), root, True)]
    seen = {root.resolve()}
    for profile_dir in skill_trees.profile_dirs(home):
        if profile_dir.resolve() in seen:
            continue  # $HERMES_HOME pointed at a profile: it is already the root
        seen.add(profile_dir.resolve())
        homes.append((profile_dir.name, profile_dir, False))
    return homes


def probe_store(store: Path) -> tuple[bool, str]:
    """``(readable, why not)`` for one store, proved rather than assumed.

    A ``state.db`` that is not a database is the failure this project has met most:
    SQLite connects lazily, so only a real read tells the difference.
    :func:`open_sqlite` probes it, and this passes that verdict on.
    """
    if not store.is_file():
        return False, f"no state.db at {store}"
    con, error = open_sqlite(store)
    if con is None:
        return False, error
    con.close()
    return True, ""


def read_activity(store: Path) -> tuple[int, int, str, str]:
    """``(sessions, messages, newest title, newest start)`` from one agent's store.

    Reads only the columns this module declares, and only the ones the store actually
    has, the way every other adapter asks for what it knows: a store that gained or lost
    a column costs a field rather than the page.
    """
    con, _error = open_sqlite(store)
    if con is None:
        return 0, 0, "", ""
    available = select_columns(con, "sessions", AGENT_COLUMNS)
    sessions = int(scalar(con, "select count(*) from sessions", default=0) or 0)
    messages = 0
    if "message_count" in available:
        messages = int(
            scalar(
                con, "select coalesce(sum(message_count), 0) from sessions", default=0
            )
            or 0
        )
    title = started = ""
    wanted = [name for name in ("title", "started_at") if name in available]
    if "started_at" in available:
        rows, _sql_error = query(
            con,
            f"select {', '.join(wanted)} from sessions "
            "where started_at is not null order by started_at desc limit 1",
        )
        if rows:
            if "title" in wanted:
                title = truncate(str(rows[0]["title"] or ""), 80)
            started = str(rows[0]["started_at"])
    con.close()
    return sessions, messages, title, started


def read_heartbeats(store: Path) -> tuple[int, str]:
    """``(backends seen, newest heartbeat)`` from one agent's store."""
    con, _error = open_sqlite(store)
    if con is None:
        return 0, ""
    available = select_columns(con, "gateway_heartbeats", HEARTBEAT_COLUMNS)
    if "last_heartbeat" not in available:
        con.close()
        return 0, ""
    count = int(scalar(con, "select count(*) from gateway_heartbeats", default=0) or 0)
    newest = scalar(
        con, "select max(last_heartbeat) from gateway_heartbeats", default=""
    )
    con.close()
    return count, str(newest or "")


def read_jobs(base: Path) -> tuple[tuple[dict[str, Any], ...], str]:
    """``(jobs, why not)`` from an agent's own ``cron/jobs.json``.

    Through the cron adapter's own loader, so this list is exactly what that adapter
    would serve -- including the shapes it names rather than guesses at.
    """
    path = base / "cron" / "jobs.json"
    if not path.is_file():
        return (), ""
    jobs, error = cron_domain._load_jobs(path)  # noqa: SLF001 - reuse, do not rewrite
    return tuple(jobs), error


def read_agent(label: str, base: Path, is_root: bool) -> Agent:
    """Everything this domain knows about one agent, in one pass."""
    store = base / "state.db"
    size, modified = 0, None
    try:
        stat = store.stat()
    except OSError:  # absent, or gone between the walk and this call
        readable, error = probe_store(store)
    else:
        size, modified = stat.st_size, stat.st_mtime
        readable, error = probe_store(store)
    sessions = messages = heartbeats = 0
    latest_session = latest_at = last_heartbeat = ""
    if readable:
        sessions, messages, latest_session, latest_at = read_activity(store)
        heartbeats, last_heartbeat = read_heartbeats(store)
    jobs, jobs_error = read_jobs(base)
    return Agent(
        label=label,
        base=base,
        is_root=is_root,
        store=store,
        store_readable=readable,
        store_error=error,
        store_size=size,
        store_modified=modified,
        sessions=sessions,
        messages=messages,
        latest_session=latest_session,
        latest_at=latest_at,
        heartbeats=heartbeats,
        last_heartbeat=last_heartbeat,
        jobs=jobs,
        jobs_error=jobs_error,
    )


def read_agents(hermes_home: Path | None = None) -> AgentIndex:
    """Scan every agent once, newest store first."""
    agents = [
        read_agent(label, base, is_root)
        for label, base, is_root in agent_homes(hermes_home)
    ]
    agents.sort(key=lambda agent: (-(agent.store_modified or 0), agent.label))
    return AgentIndex(
        root=hermes_root(hermes_home), agents=tuple(agents), as_of=as_of()
    )


def job_row(agent: Agent, job: dict[str, Any]) -> Record:
    """One cron job, rendered by the cron adapter, wearing the agent it belongs to.

    The row is the cron page's own -- title, schedule, badges, fields -- so the two
    boxes cannot describe one job differently.  Its links are dropped for a profile: the
    cron domain reads the root's ``jobs.json``, so ``/cron/<id>`` answers for a root job
    and 404s for this one, and a link that 404s is worse than no link.
    """
    row = cron_domain._job_record(job)  # noqa: SLF001 - reuse the adapter's own row
    if not agent.is_root:
        row = dataclasses.replace(row, links=())
    return dataclasses.replace(row, subtitle=f"{agent.label} \u00b7 {row.subtitle}")


class AgentsDomain(SnapshotDomain[AgentIndex]):
    """Every profile Hermes has, with what each one's store holds."""

    key = "agents"
    title = "Agents"
    summary = (
        "Every profile Hermes has: its store, what that store holds, when it last "
        "changed, and whether its backend is still checking in."
    )

    def read(self) -> AgentIndex:
        """Scan every agent once per process."""
        return read_agents(self.hermes_home)

    def _find(self, label: str) -> Agent | None:
        """The agent a page id names, or ``None``."""
        for agent in self.snapshot().agents:
            if agent.label == label:
                return agent
        return None

    def _sources(self, index: AgentIndex) -> tuple[Source, ...]:
        """The stores this page read, one per agent that has one."""
        return tuple(
            path_source(
                f"{agent.label}/state.db", agent.store, note="read-only, narrow queries"
            )
            for agent in index.agents
            if agent.store.is_file()
        )

    def _notes(self, index: AgentIndex) -> tuple[str, ...]:
        """What a reader should know before trusting the numbers above."""
        notes: list[str] = []
        unread = [agent for agent in index.agents if not agent.store_readable]
        if unread:
            notes.append(
                "stores not read: "
                + "; ".join(f"{agent.label} ({agent.store_error})" for agent in unread)
            )
        scheduled = [agent for agent in index.agents if agent.jobs]
        if scheduled:
            notes.append(
                "cron lives per agent, so a profile's jobs are not on the cron page: "
                + ", ".join(f"{agent.label} ({len(agent.jobs)})" for agent in scheduled)
            )
        notes.extend(
            f"{agent.label}/cron/jobs.json: {agent.jobs_error}"
            for agent in index.agents
            if agent.jobs_error
        )
        return tuple(notes)

    def _record(self, agent: Agent) -> Record:
        """One agent as a row: identity, its store, and what the store says."""
        badges: tuple[str, ...] = ("root" if agent.is_root else "profile",)
        if agent.store_readable and agent.sessions:
            badges = (*badges, f"{agent.sessions} session(s)")
        if agent.store_readable and agent.heartbeats:
            badges = (*badges, f"beat {fmt_ago(agent.last_heartbeat)}")
        fields: list[tuple[str, str]] = []
        if agent.store_readable:
            fields.extend(
                [
                    ("sessions", str(agent.sessions)),
                    ("messages", f"{agent.messages:,}"),
                    ("newest session", agent.latest_session or "\u2014"),
                    ("backends seen", str(agent.heartbeats)),
                    ("last heartbeat", fmt_ago(agent.last_heartbeat)),
                    # Last written, size and the paths sit after the activity: a row's
                    # meta line leads with them: "12 session(s) · 555 messages" is what
                    # an operator scans for.
                    ("last written", fmt_ago(agent.store_modified)),
                    ("size", human_size(agent.store_size)),
                    ("store", str(agent.store)),
                    ("home", str(agent.base)),
                ]
            )
        else:
            # A store that was not read has no numbers to show.  Saying so is the point:
            # "0 sessions" beside an unreadable file is a claim nobody made.
            fields.extend([("home", str(agent.base)), ("read", agent.store_error)])
        if agent.jobs:
            fields.append(("cron jobs", str(len(agent.jobs))))
        return Record(
            id=agent.label,
            title=agent.label,
            subtitle=(
                f"{agent.sessions} session(s) \u00b7 {agent.messages:,} messages"
                f" \u00b7 last written {fmt_ago(agent.store_modified)}"
                if agent.store_readable
                else f"store not read: {agent.store_error}"
            ),
            badges=badges,
            links=(
                (detail_url("agents", agent.label), "Open agent"),
                (filter_url("memory", profile=agent.label), "Memory"),
            ),
            fields=tuple(fields),
        )

    def overview(self) -> Collection:
        """Every agent, newest store first."""
        index = self.snapshot()
        agents = index.agents
        read = [agent for agent in agents if agent.store_readable]
        sessions = sum(agent.sessions for agent in read)
        jobs = sum(len(agent.jobs) for agent in agents)
        return build_collection(
            "overview",
            "Agents",
            "Every profile Hermes has, with its store and what that store holds.",
            "profiles under the Hermes home, plus the root",
            [self._record(agent) for agent in agents],
            cap=AGENTS_CAP,
            sources=self._sources(index),
            extra_counts=(
                Count(len(agents), "agents"),
                Count(
                    sessions,
                    f"sessions across the {len(read)} store(s) that could be read",
                ),
                Count(jobs, "cron jobs across those agents"),
            ),
            metrics=(
                ("Agents", str(len(agents))),
                ("Sessions", f"{sessions:,}"),
                ("Stores read", f"{len(read)} of {len(agents)}"),
                ("Cron jobs", f"{jobs:,}"),
            ),
            notes=self._notes(index),
            as_of=index.as_of,
        )

    def _stores_collection(self) -> Collection:
        """The storage lens: one row per store, with its size and last write."""
        index = self.snapshot()
        return build_collection(
            "stores",
            "Stores",
            "The state.db each agent keeps, how big it is and when it last changed.",
            "state.db files under the Hermes home",
            [
                Record(
                    id=agent.label,
                    title=f"{agent.label}/state.db",
                    subtitle=(
                        f"{human_size(agent.store_size)} \u00b7 "
                        f"written {fmt_ago(agent.store_modified)} \u00b7 "
                        f"{agent.sessions} session(s)"
                        if agent.store_readable
                        else f"not read: {agent.store_error}"
                    ),
                    badges=("root" if agent.is_root else "profile",),
                    links=((detail_url("agents", agent.label), "Open agent"),),
                    fields=(
                        ("path", str(agent.store)),
                        ("size", human_size(agent.store_size)),
                        ("last written", fmt_ago(agent.store_modified)),
                        ("backends seen", str(agent.heartbeats)),
                        ("last heartbeat", fmt_ago(agent.last_heartbeat)),
                    ),
                )
                for agent in index.agents
            ],
            cap=AGENTS_CAP,
            sources=self._sources(index),
            notes=self._notes(index),
            as_of=index.as_of,
        )

    def _jobs_collection(self) -> Collection:
        """Every agent's jobs in one list, the root's and the profiles' alike."""
        index = self.snapshot()
        scheduled = [agent for agent in index.agents if agent.jobs]
        return build_collection(
            "cron",
            "Cron by agent",
            "The jobs each agent schedules: cron lives per profile, and the cron page "
            "reads the root's file only.",
            "jobs.json files under the Hermes home",
            [job_row(agent, job) for agent in scheduled for job in agent.jobs],
            cap=AGENTS_CAP,
            sources=tuple(
                path_source(
                    f"{agent.label}/cron/jobs.json", agent.base / "cron" / "jobs.json"
                )
                for agent in scheduled
            ),
            notes=self._notes(index),
            as_of=index.as_of,
        )

    def collections(
        self, _filters: Mapping[str, str] | None = None
    ) -> Sequence[Collection]:
        """Drill-down collections: the stores, and cron by agent."""
        return [self._stores_collection(), self._jobs_collection()]

    def detail(self, record_id: str) -> Record | None:
        """One agent: the same roll-up, as its own page."""
        agent = self._find(record_id)
        return None if agent is None else self._record(agent)

    def detail_sections(self, record_id: str) -> Sequence[Collection]:
        """Behind one agent: its newest sessions, its cron, and its store."""
        agent = self._find(record_id)
        if agent is None:
            return []
        stamp = self.snapshot().as_of
        return [
            self._sessions_section(agent, stamp),
            self._jobs_section(agent, stamp),
            self._store_section(agent, stamp),
        ]

    def _sessions_section(self, agent: Agent, stamp: str) -> Collection:
        """The agent's own newest sessions, or why they could not be read."""
        if not agent.store_readable:
            return build_collection(
                "sessions",
                "Sessions",
                "What this agent has been doing, newest first.",
                "rows in this agent's sessions table",
                [],
                sources=(),
                unavailable=agent.store_error,
                as_of=stamp,
            )
        con, error = open_sqlite(agent.store)
        rows: list[Any] = []
        if con is not None:
            available = select_columns(con, "sessions", AGENT_COLUMNS)
            wanted = [name for name in ("title", "started_at") if name in available]
            order = "started_at desc" if "started_at" in available else "rowid desc"
            rows, _sql_error = query(
                con,
                f"select {', '.join(wanted) if wanted else 'id'} from sessions "
                f"order by {order} limit ?",
                (SESSION_CAP,),
            )
            con.close()
        records = []
        for position, row in enumerate(rows):
            keys = set(row.keys())
            title = str(row["title"]) if "title" in keys else ""
            when = str(row["started_at"]) if "started_at" in keys else ""
            records.append(
                Record(
                    id=f"{agent.label}-{position}",
                    title=truncate(title or "(untitled)", 80),
                    subtitle=f"started {fmt_ago(when)}" if when else "",
                    badges=("session",),
                )
            )
        return build_collection(
            "sessions",
            "Sessions",
            "What this agent has been doing, newest first.",
            f"newest {SESSION_CAP} of this agent's sessions",
            records,
            cap=SESSION_CAP,
            sources=(path_source(f"{agent.label}/state.db", agent.store),),
            notes=tuple(note for note in (error,) if note),
            unavailable=unreadable(error, agent.store),
            as_of=stamp,
        )

    def _jobs_section(self, agent: Agent, stamp: str) -> Collection:
        """This agent's own cron jobs, as the cron adapter renders them."""
        return build_collection(
            "jobs",
            "Cron jobs",
            "The jobs this agent schedules from its own cron/jobs.json.",
            f"jobs in {agent.label}/cron/jobs.json",
            [job_row(agent, job) for job in agent.jobs],
            cap=AGENTS_CAP,
            sources=(
                (
                    path_source(
                        f"{agent.label}/cron/jobs.json",
                        agent.base / "cron" / "jobs.json",
                    ),
                )
                if agent.jobs
                else ()
            ),
            notes=tuple(note for note in (agent.jobs_error,) if note),
            as_of=stamp,
        )

    def _store_section(self, agent: Agent, stamp: str) -> Collection:
        """The store itself: where it is, how big, and how fresh."""
        facts = (
            ("path", str(agent.store)),
            ("size", human_size(agent.store_size)),
            ("last written", fmt_ago(agent.store_modified)),
            ("backends seen", str(agent.heartbeats)),
            ("last heartbeat", fmt_ago(agent.last_heartbeat)),
        )
        return build_collection(
            "store",
            "Store",
            "The state.db behind this agent.",
            "facts about this agent's store",
            [
                Record(
                    id=name.replace(" ", "-"),
                    title=name,
                    subtitle=value if agent.store_readable else "not read",
                )
                for name, value in facts
            ],
            sources=(path_source(f"{agent.label}/state.db", agent.store),),
            notes=tuple(note for note in (agent.store_error,) if note),
            unavailable=unreadable(agent.store_error, agent.store),
            as_of=stamp,
        )

    def search(self, needle: str, limit: int) -> Sequence[Record]:
        """Agents matching *needle* by name or path."""
        term = needle.strip().lower()
        if not term:
            return []
        hits = [
            agent
            for agent in self.snapshot().agents
            if term in agent.label.lower() or term in str(agent.base).lower()
        ]
        return [self._record(agent) for agent in hits[:limit]]


def build_domain(hermes_home: Path | None = None) -> Domain:
    """Build the agents domain.

    Args:
        hermes_home: Hermes home or profile directory; ``None`` resolves
        ``$HERMES_HOME`` then ``~/.hermes``.

    Returns:
        A :class:`~hermes.portal.model.Domain`.  The scan happens once per process, so
        the refresh button is what picks up a profile created since the last read.
    """
    return AgentsDomain(hermes_home).domain()
