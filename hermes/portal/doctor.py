"""``hermes-portal doctor`` -- does this Hermes still match what the adapters read?

The portal reads a small surface: the columns its adapters declare, six tables in
``state.db`` and ``cron/executions.db``, the tables the code graph keeps, and one
JSON file (``cron/jobs.json``).  Hermes evolves that surface, and the evolution is
*additive by design*: ``SCHEMA_SQL`` is its single source of truth and a startup
reconcile ADDs any column that is missing, so a reader asking only for the columns it
knows keeps working as tables grow.  That is why no version matrix is needed here --
and why what this checks for is the other direction:

* a table or column that **disappeared or was renamed**, reported next to the columns
  that do exist, ranked by name similarity, because the candidate is the useful part;
* a **JSON shape that moved**, reported by running the adapter's own loader, so the
  message is the one the page itself would show;
* values whose **shape survived and whose meaning changed** -- the cron store writes
  ISO-8601 stamps where ``state.db`` holds epoch seconds -- so every declared stamp
  column is sampled and put through the same parser the pages use, and a number too
  large to be seconds is called out as milliseconds.

It reports; it does not repair.  A rename is a decision about what a number *means*,
so the fix belongs to whoever reads this: a person, or their agent, with the suite as
the net.  Version stamps are printed as hints and never as contracts -- ``state.db``
advances ``schema_version`` for *data* migrations only, so a shape change can leave it
unmoved, and ``cron/executions.db`` carries no stamp at all.
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domains import cron as cron_domain
from .domains import graph as graph_domain
from .domains import sessions as sessions_domain
from .domains import usage as usage_domain
from .domains import vault as vault_domain
from .sources import (
    cron_dir,
    hermes_root,
    open_sqlite,
    query,
    state_db,
    table_columns,
    to_datetime,
)

# Directories never worth walking when counting sources: they are not content, and a
# vault or a plugin tree can hold a copy of one big enough to matter.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".obsidian",
        ".trash",
        ".smart-env",
    }
)

# A bound on the walk, so a check never turns into a survey of a 100k-file tree.
MAX_SOURCES = 20000


@dataclass(frozen=True)
class FileSurface:
    """A page whose items come from files rather than from a database.

    ``diagnostic`` says whether an empty read is evidence.  It is not, everywhere:

    * **memory**: ``entries separated by §`` comes from parsing each file, so files on
      disk and no entries is a format that moved.
    * **skills**: the item count is the frontmatter names, and a name falls back to the
      directory -- so zero items means the tree could not be walked at all.
    * **vault** and **plugins** count filesystem facts (notes, manifests), so zero means
      the files are gone, which the ``sources`` number already says.
    * **logs**: ``distinct error signatures`` is legitimately 0 on a healthy machine --
      firing on that would train a reader to ignore the report -- so it is reported
      without a verdict.
    """

    domain: str
    collection: str
    sources: str
    count: Callable[[Path, Path], int]
    diagnostic: bool = True


FILE_SURFACES = (
    FileSurface(
        "memory",
        "entries",
        "memory file(s) under the profiles",
        lambda root, _vault: _walk_sources(
            _memory_roots(root), lambda p: p.suffix == ".md"
        ),
    ),
    FileSurface(
        "skills",
        "skills",
        "SKILL.md file(s) reachable from the skills roots",
        lambda root, _vault: _walk_sources(
            _skills_roots(root), lambda p: p.name == "SKILL.md"
        ),
    ),
    FileSurface(
        "plugins",
        "plugins",
        "director(y/ies) holding a plugin.yaml",
        lambda root, _vault: _walk_sources(
            [root / "plugins", root / "hermes-agent" / "plugins"],
            lambda p: p.name == "plugin.yaml",
        ),
        False,
    ),
    FileSurface(
        "vault",
        "notes",
        "markdown note(s) under the vault root",
        lambda _root, vault: _walk_sources([vault], lambda p: p.suffix == ".md"),
        False,
    ),
    FileSurface(
        "logs",
        "signatures",
        "log file(s) in scope",
        lambda root, _vault: _walk_sources(
            [root / "logs"], lambda p: p.suffix == ".log"
        ),
        False,
    ),
)

# Columns the health adapter names inline (it queries them without a constant).
HEARTBEAT_COLUMNS = (
    "backend_id",
    "pid",
    "started_at",
    "last_heartbeat",
    "profile",
    "host",
)

# Tables the code graph reads.  This adapter writes its column lists inside its
# queries rather than as constants, so the doctor checks presence for these and
# leaves the column surface to the adapters that declare one -- saying less is
# better than a second, drifting copy of the same names.
GRAPH_TABLES = (
    "metadata",
    "nodes",
    "edges",
    "communities",
    "community_summaries",
    "flows",
    "flow_memberships",
    "risk_index",
    "nodes_fts",
)

# A column whose name says "this holds a stamp".
TIME_COLUMN = re.compile(r"(_at|_ts|timestamp|_heartbeat)$")

# Epoch seconds this side of the year 5138; anything larger is a different unit.
MAX_PLAUSIBLE_SECONDS = 1e11

SAMPLE_ROWS = 25
CANDIDATE_FLOOR = 0.4

# What a green run does not mean.  Printed with every report so a clean bill of
# health is not read as "everything the portal touches was verified".
NOT_COVERED = (
    "what is inside the files: a note whose links moved, a log line's shape",
    "value plausibility beyond parseability (units, scales, offsets)",
    "whether a number still means what its label says",
    "Hermes' own version, which does not describe this shape (see the stamps above)",
)


@dataclass(frozen=True)
class TableNeed:
    """One table an adapter reads, and the columns it declares it needs."""

    source: str
    path: Path
    table: str
    columns: tuple[str, ...] = ()
    required: bool = True


@dataclass(frozen=True)
class Finding:
    """One thing the doctor noticed, with the weight it should carry."""

    severity: str
    source: str
    subject: str
    detail: str


@dataclass(frozen=True)
class StoreReport:
    """What one database looked like, including its version stamp if it has one."""

    source: str
    path: Path
    readable: bool
    tables: tuple[str, ...]
    version: str = ""


@dataclass(frozen=True)
class Report:
    """The whole inspection: stores, findings, hints, and the limits of the check."""

    hermes_root: Path
    stores: tuple[StoreReport, ...]
    findings: tuple[Finding, ...]
    hints: dict[str, str]
    not_covered: tuple[str, ...] = NOT_COVERED

    @property
    def drift(self) -> tuple[Finding, ...]:
        """Findings that mean a declared read is broken right now."""
        return tuple(f for f in self.findings if f.severity == "drift")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        """Findings worth knowing that do not break a page."""
        return tuple(f for f in self.findings if f.severity == "warn")

    @property
    def ok(self) -> bool:
        """``True`` when nothing declared is missing or unreadable."""
        return not self.drift


def contract(home: Path | None = None) -> tuple[TableNeed, ...]:
    """Return the read surface, paired with the store each table lives in.

    The columns come from the adapters' own constants wherever one exists, so this
    module cannot quietly disagree with what the pages ask for.

    Args:
        home: Hermes home or profile directory; defaults to ``$HERMES_HOME``.

    Returns:
        One :class:`TableNeed` per table, state store first.
    """
    root = hermes_root(home)
    db = state_db(root)
    exec_db = cron_dir(root) / "executions.db"
    graph_db = graph_domain.graph_path(root)

    def merged(*groups: Sequence[str]) -> tuple[str, ...]:
        """Union of column lists, order preserved, duplicates dropped."""
        return tuple(dict.fromkeys(name for group in groups for name in group))

    needs = [
        TableNeed(
            "state.db",
            db,
            "sessions",
            merged(sessions_domain.SESSION_COLUMNS, usage_domain.SESSION_COLUMNS),
        ),
        TableNeed("state.db", db, "messages", sessions_domain.MESSAGE_COLUMNS),
        TableNeed(
            "state.db",
            db,
            "session_model_usage",
            merged(sessions_domain.USAGE_COLUMNS, usage_domain.USAGE_COLUMNS),
        ),
        TableNeed("state.db", db, "gateway_heartbeats", HEARTBEAT_COLUMNS),
        TableNeed(
            "cron/executions.db", exec_db, "executions", cron_domain.EXECUTION_COLUMNS
        ),
        TableNeed(
            "cron/executions.db",
            exec_db,
            "cron_incidents",
            cron_domain.INCIDENT_COLUMNS,
        ),
    ]
    # The code graph is optional by design: the portal serves every other page when
    # it is absent, so a missing graph database is a warning, not breakage.
    needs.extend(
        TableNeed("graph.db", graph_db, table, (), required=False)
        for table in GRAPH_TABLES
    )
    return tuple(needs)


def candidates(name: str, live: Iterable[str], limit: int = 3) -> list[str]:
    """Return the live names closest to *name*, best first.

    Similarity is plain string distance (``difflib``), which is enough to put
    ``created_at`` in front of ``closed_at`` for a missing ``started_at`` -- and the
    point is to hand a reader the likely rename, not to guess on their behalf.

    Args:
        name: The declared name that is no longer there.
        live: Names that do exist.
        limit: How many to return.

    Returns:
        Similar names, above :data:`CANDIDATE_FLOOR`.
    """
    scored = sorted(
        ((difflib.SequenceMatcher(None, name, other).ratio(), other) for other in live),
        reverse=True,
    )
    return [other for ratio, other in scored[:limit] if ratio >= CANDIDATE_FLOOR]


def parse_stamp(value: Any) -> Any:
    """Parse *value* with the pages' own parser; ``None`` when it is not a stamp."""
    try:
        return to_datetime(value)
    except (TypeError, ValueError, OverflowError, sqlite3.Error):
        return None


def _column_types(con: sqlite3.Connection, table: str) -> dict[str, str]:
    """Return ``{column: declared type}`` for *table*, from SQLite's own metadata."""
    rows, _error = query(con, f'pragma table_info("{table}")')
    # Lower-cased: the report reads these in a sentence, and Hermes' DDL is upper.
    return {str(row["name"]): str(row["type"] or "").lower() for row in rows}


def _live_tables(con: sqlite3.Connection) -> set[str]:
    """Return every table name in the store."""
    rows, _error = query(con, "select name from sqlite_master where type = 'table'")
    return {str(row["name"]) for row in rows}


def _check_values(
    con: sqlite3.Connection, need: TableNeed, limit: int = SAMPLE_ROWS
) -> list[Finding]:
    """Sample every declared stamp column and put the values through the parser.

    A column that kept its name and its type but changed what it stores is the one
    drift a schema comparison cannot see, and this project has already been bitten
    by it: ``executions.db`` writes ISO-8601 strings where other stores hold epoch
    seconds, which is why one parser accepts both shapes.

    Args:
        con: Open connection to the store.
        need: The table being checked.
        limit: How many non-null values to sample per column.

    Returns:
        One finding per column that does not parse, or holds an implausible number.
    """
    live = table_columns(con, need.table)
    findings: list[Finding] = []
    for column in need.columns:
        if column not in live or not TIME_COLUMN.search(column):
            continue
        rows, _error = query(
            con,
            f'select "{column}" from "{need.table}" '
            f'where "{column}" is not null limit ?',
            (limit,),
        )
        values = [row[0] for row in rows]
        if not values:
            continue
        subject = f"{need.table}.{column}"
        # A unit change is judged first: milliseconds are a number, so they "parse"
        # as a date far in the future, and calling that unparseable would bury the
        # real diagnosis (seconds became milliseconds).
        numeric = [
            value
            for value in values
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        oversized = [
            value for value in numeric if abs(float(value)) > MAX_PLAUSIBLE_SECONDS
        ]
        if oversized:
            findings.append(
                Finding(
                    "warn",
                    need.source,
                    subject,
                    f"{len(oversized)} of {len(values)} values are too large to be "
                    f"epoch seconds (e.g. {oversized[0]}); a unit change such as "
                    "milliseconds still parses, and reads as a date in the year 50000",
                )
            )
            continue
        unparsed = [value for value in values if parse_stamp(value) is None]
        if not unparsed:
            continue
        severity = "drift" if len(unparsed) == len(values) else "warn"
        findings.append(
            Finding(
                severity,
                need.source,
                subject,
                f"{len(unparsed)} of {len(values)} sampled values do not parse as "
                f"a timestamp (e.g. {str(unparsed[0])[:40]!r}); the column kept its "
                "name and changed what it stores",
            )
        )
    return findings


def _check_table(
    con: sqlite3.Connection, need: TableNeed, tables: set[str]
) -> list[Finding]:
    """Check one table's presence and its declared columns."""
    weight = "drift" if need.required else "warn"
    if need.table not in tables:
        near = candidates(need.table, tables)
        hint = f"; nearest table(s): {', '.join(near)}" if near else ""
        return [
            Finding(
                weight,
                need.source,
                need.table,
                f"table is missing from {need.path.name}{hint}",
            )
        ]
    if not need.columns:
        return []
    live = table_columns(con, need.table)
    types = _column_types(con, need.table)
    findings: list[Finding] = []
    for column in need.columns:
        if column in live:
            continue
        near = candidates(column, live)
        detail = ", ".join(f"{name} ({types.get(name, '?')})" for name in near)
        findings.append(
            Finding(
                weight,
                need.source,
                f"{need.table}.{column}",
                f"declared column is gone; closest in this table: {detail}"
                if near
                else f"declared column is gone, and nothing similar is left of "
                f"{len(live)} columns",
            )
        )
    extra = sorted(live - set(need.columns))
    if extra:
        findings.append(
            Finding(
                "info",
                need.source,
                need.table,
                f"{len(extra)} column(s) the portal does not read: "
                f"{', '.join(extra[:6])}{'…' if len(extra) > 6 else ''} "
                "(additions are safe by design)",
            )
        )
    return findings


def _memory_roots(root: Path) -> list[Path]:
    """Everywhere a memory file can sit: the root's, then each profile's."""
    roots = [root / "memories"]
    profiles = root / "profiles"
    if profiles.is_dir():
        roots.extend(sorted(p / "memories" for p in profiles.iterdir()))
    return roots


def _skills_roots(root: Path) -> list[Path]:
    """Everywhere a skill can sit, the root's and each profile's."""
    roots = [root / "skills"]
    profiles = root / "profiles"
    if profiles.is_dir():
        roots.extend(sorted(p / "skills" for p in profiles.iterdir()))
    return roots


def _walk_sources(roots: Sequence[Path], matches: Callable[[Path], bool]) -> int:
    """Count files under *roots* that *matches* accepts, bounded and symlink-aware.

    ``followlinks=True`` is load-bearing: a profile's ``skills/`` is a tree *made* of
    symlinks to a shared root, so a walk that refuses to follow them counts nothing and
    would report every skill as unread.

    Args:
        roots: Directories to walk; missing ones are skipped.
        matches: Applied to each file.

    Returns:
        How many files matched, capped at :data:`MAX_SOURCES`.
    """
    total = 0
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
            for filename in filenames:
                if not matches(Path(dirpath) / filename):
                    continue
                total += 1
                if total >= MAX_SOURCES:
                    return total
    return total


def _check_files(
    root: Path,
    vault_root: Path | None,
    surfaces: Sequence[FileSurface] = FILE_SURFACES,
) -> list[Finding]:
    """Compare each file-backed page's item count with the sources on disk.

    A format that moves does not crash a file adapter -- it makes the page empty.  This
    is the one place that holds what is on disk next to what was read, so an empty page
    over a non-empty tree is visible rather than looking like a quiet machine.

    Args:
        root: Hermes root.
        vault_root: Vault to check; ``None`` uses the vault domain's own default.
        surfaces: What to check.

    Returns:
        One finding per surface: drift when a diagnostic surface read nothing, info
        otherwise (the numbers are worth seeing even when they agree).
    """
    from .server import default_registry  # local: server imports this module's main

    try:
        registry = default_registry(hermes_home=root, vault_root=vault_root)
    except OSError as exc:
        return [Finding("warn", "files", "-", f"could not build the adapters: {exc}")]
    vault = Path(vault_root) if vault_root is not None else vault_domain.DEFAULT_VAULT

    findings: list[Finding] = []
    for surface in surfaces:
        subject = f"{surface.domain}.{surface.collection}"
        try:
            served = {c.key: c for c in registry.get(surface.domain).collections()}
        except Exception as exc:  # noqa: BLE001 - an adapter that raises is the finding
            findings.append(
                Finding(
                    "drift",
                    surface.domain,
                    subject,
                    f"the adapter raised {type(exc).__name__}: {exc}",
                )
            )
            continue
        collection = served.get(surface.collection)
        sources = surface.count(root, vault)
        if collection is None:
            findings.append(
                Finding(
                    "warn",
                    surface.domain,
                    subject,
                    f"the page serves {sorted(served) or 'no collections'}, "
                    f"not {surface.collection!r}",
                )
            )
        elif sources and not collection.count.value and surface.diagnostic:
            findings.append(
                Finding(
                    "drift",
                    surface.domain,
                    subject,
                    f"{sources} {surface.sources}, and the page read none of them: "
                    "the shape it parses is not the shape on disk",
                )
            )
        else:
            findings.append(
                Finding(
                    "info",
                    surface.domain,
                    subject,
                    f"{sources} {surface.sources}; the page reads "
                    f"{collection.count.value}",
                )
            )
    return findings


def _check_jobs(root: Path) -> list[Finding]:
    """Run the cron adapter's own loader over ``jobs.json``.

    Reusing the loader means the doctor's verdict is exactly the one the page would
    reach -- including the legacy shapes it accepts and the ones it names.
    """
    path = cron_dir(root) / "jobs.json"
    if not path.is_file():
        return [
            Finding(
                "drift", "cron/jobs.json", path.name, f"no job definitions at {path}"
            )
        ]
    jobs, error = cron_domain._load_jobs(path)  # noqa: SLF001 - the point is to reuse it
    if error:
        return [Finding("drift", "cron/jobs.json", path.name, error)]
    if not jobs:
        return [
            Finding(
                "warn", "cron/jobs.json", path.name, "it parses, but defines no jobs"
            )
        ]
    return [
        Finding("info", "cron/jobs.json", path.name, f"{len(jobs)} job definition(s)")
    ]


def _version_hint(con: sqlite3.Connection, source: str) -> str:
    """Return the store's own schema version, when it keeps one."""
    if source == "state.db":
        rows, _error = query(con, "select version from schema_version")
    elif source == "graph.db":
        rows, _error = query(
            con, "select value from metadata where key = 'schema_version'"
        )
    else:
        return ""
    return str(rows[0][0]) if rows else ""


def inspect(
    needs: Sequence[TableNeed] | None = None,
    home: Path | None = None,
    vault_root: Path | None = None,
) -> Report:
    """Read every store in the contract and report what no longer matches.

    Args:
        needs: Tables to check; defaults to :func:`contract`.
        home: Hermes home or profile directory.
        vault_root: Vault for the file-format check; ``None`` uses the domain's default.

    Returns:
        A :class:`Report`; ``report.ok`` is ``False`` when something declared is gone.
    """
    needs = tuple(needs) if needs is not None else contract(home)
    root = hermes_root(home)
    findings: list[Finding] = []
    stores: list[StoreReport] = []
    hints: dict[str, str] = {}

    grouped: dict[Path, list[TableNeed]] = {}
    for need in needs:
        grouped.setdefault(need.path, []).append(need)

    for path, group in grouped.items():
        label = group[0].source
        required = any(need.required for need in group)
        con, error = open_sqlite(path)
        if con is None:
            findings.append(
                Finding("drift" if required else "warn", label, path.name, error)
            )
            stores.append(StoreReport(label, path, False, ()))
            continue
        tables = _live_tables(con)
        for need in group:
            findings.extend(_check_table(con, need, tables))
            findings.extend(_check_values(con, need))
        version = _version_hint(con, label)
        stores.append(StoreReport(label, path, True, tuple(sorted(tables)), version))
        if version:
            hints[f"{label} schema_version"] = version
        con.close()

    findings.extend(_check_jobs(root))
    findings.extend(_check_files(root, vault_root))
    hints["Hermes root"] = str(root)
    return Report(root, tuple(stores), tuple(findings), hints)


def render(report: Report) -> str:
    """Render *report* for a person reading a terminal."""
    lines: list[str] = [f"hermes-portal doctor -- {report.hermes_root}", ""]
    for store in report.stores:
        stamp = f"schema {store.version}" if store.version else "no version stamp"
        state = f"{len(store.tables)} tables" if store.readable else "NOT READABLE"
        lines.append(f"  {store.source:<19} {state:<12} {stamp}")
    if report.hints:
        lines.append("")
        lines.append("  hints (a version describes the data, not the shape):")
        for key, value in report.hints.items():
            lines.append(f"    {key}: {value}")
    for heading, group in (
        ("drift", report.drift),
        ("warnings", report.warnings),
        ("notes", tuple(f for f in report.findings if f.severity == "info")),
    ):
        if not group:
            continue
        lines.append("")
        lines.append(f"  {heading}:")
        for finding in group:
            lines.append(
                f"    [{finding.severity}] {finding.source} :: {finding.subject}"
            )
            lines.append(f"        {finding.detail}")
    lines.append("")
    if report.ok:
        lines.append("  ok -- everything the adapters declare is present and readable.")
    else:
        lines.append(
            f"  {len(report.drift)} declared read(s) are broken. Each one names what "
            "was expected; the fix is a decision about meaning, so it belongs to you "
            "(or your agent) rather than to a guess."
        )
    lines.append("")
    lines.append("  not covered by this check:")
    for item in report.not_covered:
        lines.append(f"    - {item}")
    return "\n".join(lines)


def as_json(report: Report) -> str:
    """Render *report* for a machine -- the artifact an agent can act on."""
    payload = {
        "ok": report.ok,
        "hermes_root": str(report.hermes_root),
        "hints": report.hints,
        "stores": [
            {
                "source": store.source,
                "path": str(store.path),
                "readable": store.readable,
                "tables": list(store.tables),
                "version": store.version,
            }
            for store in report.stores
        ],
        "findings": [dataclasses.asdict(finding) for finding in report.findings],
        "drift": [dataclasses.asdict(finding) for finding in report.drift],
        "not_covered": list(report.not_covered),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    """The doctor's own arguments; it never starts a server."""
    parser = argparse.ArgumentParser(
        prog="hermes-portal doctor",
        description=(
            "Check that this machine's Hermes still matches the columns and shapes "
            "the portal's adapters read. Read-only; reports, does not repair."
        ),
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=None,
        help="Hermes home or profile directory (default: $HERMES_HOME, else ~/.hermes)",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="Obsidian vault to check (default: $HERMES_VAULT, else the vault's own)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the report as JSON and nothing else"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing; the exit code carries the verdict",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the check.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` when nothing declared is missing, ``1`` when something is.
    """
    args = build_parser().parse_args(argv)
    report = inspect(home=args.hermes_home, vault_root=args.vault)
    if args.quiet:
        return 0 if report.ok else 1
    print(as_json(report) if args.json else render(report))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    sys.exit(main())
