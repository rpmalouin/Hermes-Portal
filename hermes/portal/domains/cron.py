"""Cron domain: the scheduled jobs, their runs and their output.

Three read-only sources, and the domain keeps them distinct:

``cron/jobs.json``                 the definitions (``{"jobs": [...], ...}``)
``cron/executions.db``             one row per run, plus ``cron_incidents``
``cron/output/<job_id>/<date>.md`` the report each run wrote

"10 jobs" and "1000 runs" answer different questions, so the definition count and
the execution count are published side by side with their own definitions rather
than collapsed into one number a reader has to guess at.

Per-profile execution stores exist (``profiles/*/cron/executions.db``) and are
*not* aggregated here; that is called out as a note on the collection instead of
being silently ignored.
"""

from __future__ import annotations

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
    detail_url,
)
from ..sources import (
    as_of,
    cron_dir,
    fmt_ago,
    fmt_duration,
    fmt_time,
    human_size,
    open_sqlite,
    path_source,
    query,
    read_json,
    read_text,
    select_columns,
    snippet,
    to_datetime,
    truncate,
)
from .base import SnapshotDomain

JOBS_CAP = 50
RUNS_CAP = 40
JOB_RUNS_CAP = 25
OUTPUT_CAP = 20
OUTPUT_BODY_CAP = 1200
JOB_BODY_CAP = 3000

EXECUTION_COLUMNS = (
    "id",
    "job_id",
    "source",
    "process_id",
    "pid",
    "status",
    "claimed_at",
    "started_at",
    "finished_at",
    "error",
    "delivery_outcome",
    "scheduled_instant",
)
INCIDENT_COLUMNS = (
    "id",
    "job_id",
    "error_sig",
    "state",
    "failure_type",
    "first_seen_at",
    "last_seen_at",
    "acked_at",
    "closed_at",
    "error",
    "output_file",
)


def _close(con: Any) -> None:
    """Close a connection if one was opened."""
    if con is not None:
        con.close()


def _get(row: Any, key: str, default: Any = "\u2014") -> Any:
    """Read *key* from a sqlite3.Row, tolerating a column the schema lacks."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _load_jobs(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Read the job definitions as a list, whatever shape they were stored in.

    ``jobs.json`` is an object with a ``jobs`` array today; older or hand-edited
    files may be a bare list or a map keyed by job id, so all three are accepted.
    """
    data, error = read_json(path)
    if error:
        return [], error
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    if not isinstance(jobs, list):
        return [], f"unexpected jobs.json shape: {type(jobs).__name__}"
    return [job for job in jobs if isinstance(job, dict)], ""


def _job_title(job: dict[str, Any]) -> str:
    """Human name for a job, falling back to its id."""
    return str(job.get("name") or job.get("id") or "(unnamed job)")


def _job_record(job: dict[str, Any]) -> Record:
    """One job as a list row / detail record."""
    job_id = str(job.get("id", "?"))
    enabled = bool(job.get("enabled"))
    script = str(job.get("script") or "")
    skills = job.get("skills") or ([] if not job.get("skill") else [job["skill"]])
    schedule = str(job.get("schedule_display") or job.get("schedule") or "?")
    return Record(
        id=job_id,
        title=_job_title(job),
        subtitle=f"{schedule} · {'enabled' if enabled else 'disabled'} · "
        f"last {job.get('last_status') or 'never'}",
        badges=(
            schedule,
            "enabled" if enabled else "disabled",
            str(job.get("state") or "?"),
            f"last {job.get('last_status') or 'never'}",
        ),
        links=((detail_url("cron", job_id), "Open job"),),
        group=str(job.get("deliver") or "local"),
        fields=(
            ("id", job_id),
            ("schedule", schedule),
            ("enabled", "yes" if enabled else "no"),
            ("state", str(job.get("state") or "\u2014")),
            ("deliver", str(job.get("deliver") or "\u2014")),
            ("model", str(job.get("model") or "\u2014")),
            ("provider", str(job.get("provider") or "\u2014")),
            ("script", script or "\u2014"),
            ("skills", ", ".join(str(s) for s in skills) or "\u2014"),
            ("no_agent", str(job.get("no_agent"))),
            ("created", fmt_time(job.get("created_at"))),
            (
                "last run",
                f"{fmt_time(job.get('last_run_at'))} "
                f"({fmt_ago(job.get('last_run_at'))})",
            ),
            (
                "next run",
                f"{fmt_time(job.get('next_run_at'))} "
                f"({fmt_ago(job.get('next_run_at'))})",
            ),
            ("last status", str(job.get("last_status") or "\u2014")),
            ("last error", truncate(str(job.get("last_error") or "\u2014"), 300)),
            ("failure streak", str(job.get("failure_streak", 0))),
            (
                "repeat completed",
                str((job.get("repeat") or {}).get("completed", "\u2014")),
            ),
            ("paused", str(job.get("paused_at") or "no")),
            ("paused reason", truncate(str(job.get("paused_reason") or "\u2014"), 200)),
        ),
    )


class CronDomain(SnapshotDomain[None]):
    """Scheduled jobs, their recent runs, and the failures worth seeing.

    A class rather than a factory of closures, so every helper below can be called by
    name -- by a test, by a reader, and by the code graph.
    """

    key = "cron"
    title = "Cron"
    summary = "Scheduled jobs, execution history and run output, read-only."

    def __init__(self, hermes_home: Path | None = None) -> None:
        """Point at the sources; nothing is read until a page asks."""
        super().__init__(hermes_home)
        self.cron_root = cron_dir(self.hermes_home)
        self.jobs_path = self.cron_root / "jobs.json"
        self.exec_path = self.cron_root / "executions.db"
        self.output_dir = self.cron_root / "output"

    def _sources(self) -> tuple[Source, ...]:
        return (
            path_source("jobs.json", self.jobs_path, note="job definitions"),
            path_source(
                "executions.db", self.exec_path, note="one row per run, read-only"
            ),
            path_source("output/", self.output_dir, note="per-run reports"),
        )

    def _jobs_and_notes(self) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
        jobs, error = _load_jobs(self.jobs_path)
        notes: list[str] = []
        if error:
            notes.append(error)
        profile_stores = (
            sorted(
                p
                for p in (self.cron_root.parent / "profiles").glob(
                    "*/cron/executions.db"
                )
            )
            if (self.cron_root.parent / "profiles").is_dir()
            else []
        )
        if profile_stores:
            notes.append(
                f"{len(profile_stores)} per-profile execution store(s) exist and are "
                "not aggregated here yet"
            )
        return jobs, tuple(notes)

    def overview(self) -> Collection:
        """Headline numbers for the cron domain."""
        jobs, notes = self._jobs_and_notes()
        con, error = open_sqlite(self.exec_path)
        runs = 0
        if con is not None:
            rows, _sql_error = query(con, "select count(*) from executions")
            runs = int(rows[0][0]) if rows else 0
        _close(con)
        return build_collection(
            "overview",
            "Cron",
            "Scheduled jobs, how often they ran, and whether they are healthy.",
            "entries in cron/jobs.json",
            [_job_record(job) for job in jobs],
            cap=5,
            sources=self._sources(),
            extra_counts=(
                Count(runs, "rows in cron/executions.db"),
                Count(sum(1 for job in jobs if job.get("enabled")), "enabled jobs"),
                Count(sum(1 for job in jobs if job.get("script")), "run a script"),
            ),
            notes=tuple(notes) + ((error,) if error else ()),
            as_of=as_of(),
        )

    def _jobs_collection(self) -> Collection:
        """Every defined job."""
        jobs, notes = self._jobs_and_notes()
        return build_collection(
            "jobs",
            "Jobs",
            "Every scheduled job, with its schedule, last status and next run.",
            "entries in cron/jobs.json",
            [_job_record(job) for job in jobs],
            cap=JOBS_CAP,
            sources=self._sources(),
            extra_counts=(
                Count(sum(1 for job in jobs if job.get("enabled")), "enabled"),
                Count(sum(1 for job in jobs if job.get("script")), "run a script"),
                Count(sum(1 for job in jobs if job.get("prompt")), "carry a prompt"),
            ),
            notes=notes,
            as_of=as_of(),
        )

    def _runs_collection(self, job_id: str | None = None) -> Collection:
        """Execution history, optionally for one job."""
        jobs, _notes = self._jobs_and_notes()
        names = {str(job.get("id")): _job_title(job) for job in jobs}
        con, error = open_sqlite(self.exec_path)
        columns = select_columns(con, "executions", EXECUTION_COLUMNS)
        where = " where job_id = ?" if job_id else ""
        params = (job_id,) if job_id else ()
        order = "started_at" if "started_at" in columns else "id"
        rows, sql_error = query(
            con,
            f"select {', '.join(columns)} from executions{where} order by {order} desc",
            params,
        )
        total = len(rows)
        completed = sum(1 for row in rows if str(_get(row, "status")) == "completed")
        _close(con)

        records = []
        for row in rows:
            started = _get(row, "started_at", None)
            finished = _get(row, "finished_at", None)
            duration = None
            began, ended = to_datetime(started), to_datetime(finished)
            if began is not None and ended is not None:
                duration = (ended - began).total_seconds()
            job_key = str(_get(row, "job_id", "?"))
            records.append(
                Record(
                    id=str(_get(row, "id")),
                    title=names.get(job_key, job_key),
                    href=detail_url("cron", job_key) if job_key != "?" else "",
                    subtitle=f"{fmt_time(started)} · {_get(row, 'status')}"
                    + (f" · {fmt_duration(duration)}" if duration is not None else ""),
                    badges=(str(_get(row, "status")), str(_get(row, "source")))
                    + ((fmt_duration(duration),) if duration is not None else ()),
                    links=((detail_url("cron", job_key), "Job"),),
                    fields=(
                        ("job", names.get(job_key, job_key)),
                        ("job id", job_key),
                        ("status", str(_get(row, "status"))),
                        ("source", str(_get(row, "source"))),
                        ("scheduled", fmt_time(_get(row, "scheduled_instant", None))),
                        ("started", fmt_time(started)),
                        ("finished", fmt_time(finished)),
                        ("duration", fmt_duration(duration)),
                        ("pid", str(_get(row, "pid", None))),
                        ("error", truncate(str(_get(row, "error", "")), 200)),
                        ("delivery", str(_get(row, "delivery_outcome", None))),
                    ),
                )
            )
        notes = tuple(n for n in (error, sql_error) if n)
        definition = (
            f"rows in cron/executions.db for job {job_id}"
            if job_id
            else "rows in cron/executions.db"
        )
        return build_collection(
            "runs" if not job_id else f"runs-{job_id}",
            "Recent runs" if not job_id else "Runs",
            "One row per execution, newest first.",
            definition,
            records,
            cap=RUNS_CAP if not job_id else JOB_RUNS_CAP,
            sources=self._sources(),
            extra_counts=(
                Count(total, "runs counted here"),
                Count(completed, "of those, completed"),
                Count(total - completed, "of those, not completed"),
            ),
            notes=notes,
            as_of=as_of(),
        )

    def _incidents_collection(self) -> Collection:
        """Failures the cron scheduler recorded."""
        con, error = open_sqlite(self.exec_path)
        columns = select_columns(con, "cron_incidents", INCIDENT_COLUMNS)
        if not columns:
            _close(con)
            return build_collection(
                "incidents",
                "Incidents",
                "Failures recorded by the scheduler.",
                "rows in cron_incidents",
                [],
                sources=self._sources(),
                notes=(error or "no cron_incidents table in this database",),
                as_of=as_of(),
            )
        order = "first_seen_at" if "first_seen_at" in columns else "id"
        rows, sql_error = query(
            con,
            f"select {', '.join(columns)} from cron_incidents order by {order} desc",
        )
        states: dict[str, int] = {}
        for row in rows:
            state = str(_get(row, "state", "?"))
            states[state] = states.get(state, 0) + 1
        _close(con)
        return build_collection(
            "incidents",
            "Incidents",
            "Failures recorded by the scheduler, with their error signature.",
            "rows in cron_incidents",
            [
                Record(
                    id=str(_get(row, "id")),
                    title=truncate(str(_get(row, "error_sig", "incident")), 80),
                    href=detail_url("cron", str(_get(row, "job_id")))
                    if _get(row, "job_id")
                    else "",
                    subtitle=f"{truncate(str(_get(row, 'error', '')), 120)}",
                    badges=(
                        str(_get(row, "state")),
                        str(_get(row, "failure_type")),
                        f"job {_get(row, 'job_id')}",
                    ),
                    links=((f"/cron/{_get(row, 'job_id', '')}", "Job"),),
                    fields=(
                        ("job id", str(_get(row, "job_id"))),
                        ("state", str(_get(row, "state"))),
                        ("failure type", str(_get(row, "failure_type"))),
                        ("first seen", fmt_time(_get(row, "first_seen_at", None))),
                        ("last seen", fmt_time(_get(row, "last_seen_at", None))),
                        ("acked", fmt_time(_get(row, "acked_at", None))),
                        ("closed", fmt_time(_get(row, "closed_at", None))),
                        ("error", truncate(str(_get(row, "error", "")), 300)),
                        ("output file", str(_get(row, "output_file"))),
                    ),
                )
                for row in rows
            ],
            cap=JOBS_CAP,
            sources=self._sources(),
            extra_counts=tuple(
                Count(count, f"incidents in state {state}")
                for state, count in sorted(states.items())
            ),
            notes=tuple(n for n in (error, sql_error) if n),
            as_of=as_of(),
        )

    def collections(
        self, _filters: Mapping[str, str] | None = None
    ) -> Sequence[Collection]:
        """Drill-down collections for the cron domain.

        No query filters yet: a job's own runs and output live behind the job's
        detail page, which keeps the collection count honest.
        """
        return [
            self._jobs_collection(),
            self._runs_collection(),
            self._incidents_collection(),
        ]

    def detail(self, record_id: str) -> Record | None:
        """One job, with its prompt and script text."""
        jobs, _notes = self._jobs_and_notes()
        for job in jobs:
            if str(job.get("id")) == record_id:
                record = _job_record(job)
                body_parts = []
                if job.get("prompt"):
                    body_parts.append("PROMPT\n" + str(job["prompt"]))
                if job.get("script"):
                    body_parts.append("SCRIPT\n" + str(job["script"]))
                if job.get("enabled_toolsets"):
                    body_parts.append(
                        "TOOLSETS\n"
                        + ", ".join(str(t) for t in job["enabled_toolsets"])
                    )
                return Record(
                    id=record.id,
                    title=record.title,
                    subtitle=record.subtitle,
                    badges=record.badges,
                    fields=record.fields,
                    links=record.links,
                    body=truncate("\n\n".join(body_parts), JOB_BODY_CAP),
                    group=record.group,
                )
        return None

    def detail_sections(self, record_id: str) -> Sequence[Collection]:
        """Behind one job: its runs and the reports those runs wrote."""
        job_dir = self.output_dir / record_id
        outputs = (
            sorted((p for p in job_dir.iterdir() if p.is_file()), reverse=True)
            if job_dir.is_dir()
            else []
        )
        output_records = []
        for path in outputs:
            text, truncated, error = read_text(path, OUTPUT_BODY_CAP)
            try:
                size = human_size(path.stat().st_size)
            except OSError:
                size = "unknown"
            output_records.append(
                Record(
                    id=path.name,
                    title=path.name,
                    subtitle=snippet(text, 140) or "(empty report)",
                    badges=(size,) + (("truncated",) if truncated else ()),
                    fields=(
                        ("file", str(path)),
                        ("size", size),
                        ("body", "truncated for the page" if truncated else "complete"),
                        ("read error", error or "\u2014"),
                    ),
                    body=text,
                )
            )
        return [
            self._runs_collection(record_id),
            build_collection(
                "output",
                "Output files",
                "Reports written by this job's runs, newest first.",
                "files under cron/output/<job id>",
                output_records,
                cap=OUTPUT_CAP,
                sources=(path_source("output directory", job_dir),),
                as_of=as_of(),
                notes=() if outputs else ("no output files for this job yet",),
            ),
        ]

    def search(self, needle: str, limit: int) -> Sequence[Record]:
        """Case-insensitive substring search over job definitions."""
        term = needle.strip().lower()
        if not term:
            return []
        jobs, _notes = self._jobs_and_notes()
        hits = []
        for job in jobs:
            haystack = " ".join(
                str(job.get(key) or "")
                for key in (
                    "name",
                    "id",
                    "script",
                    "prompt",
                    "schedule_display",
                    "model",
                )
            ).lower()
            if term in haystack:
                record = _job_record(job)
                hits.append(
                    Record(
                        id=record.id,
                        title=record.title,
                        subtitle=record.subtitle,
                        badges=("cron job",) + record.badges[:1],
                        links=record.links,
                    )
                )
            if len(hits) >= limit:
                break
        return hits


def build_domain(hermes_home: Path | None = None) -> Domain:
    """Build the 'cron' domain.

    Args:
        hermes_home: Hermes home or profile directory.

    Returns:
        A :class:`~hermes.portal.model.Domain`.
    """
    return CronDomain(hermes_home).domain()
