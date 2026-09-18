"""Code graph domain: the code-review-graph store Hermes rebuilds nightly.

A narrow, structured adapter over a 2.5 GB SQLite database, shaped by two measurements:

* **Prefer what the builders already computed.**  ``risk_index``, ``flows`` and
  ``community_summaries`` carry the graph's own analysis and answer in
  milliseconds.  Recomputing degrees or a language histogram with group-bys over
  1.5M edges costs 2-4 seconds per query, so those are simply not on the page.
* **Count what is cheap, cache what is not.**  ``count(*)`` on nodes and flows is
  instant; on edges it is 3.3 seconds, so it is computed once and returned with the
  moment it was taken.

The graph's 34 MCP tools remain its interactive interface: semantic search, refactor
previews and impact analysis are theirs, not this page's.  What the portal adds is a
stable, linkable read of what the store contains, from the same read-only discipline
as every other domain.
"""

from __future__ import annotations

import threading
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
    Cache,
    age_seconds,
    as_of,
    fmt_ago,
    hermes_root,
    human_size,
    open_sqlite,
    path_source,
    query,
    scalar,
    snippet,
    truncate,
    unreadable,
)
from .base import SnapshotDomain

EDGE_COUNT_TTL = 900.0
STALE_AFTER_HOURS = 48
COMMUNITY_CAP = 40
RISK_CAP = 25
CALLER_CAP = 25
FLOW_CAP = 25
EDGE_CAP = 40
NODE_CAP = 100


def _close(con: Any) -> None:
    """Close a connection if one was opened."""
    if con is not None:
        con.close()


def _get(row: Any, key: str, default: str = "\u2014") -> Any:
    """Read *key* from a sqlite3.Row, tolerating a column the schema lacks."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def graph_path(home: Path | None = None) -> Path:
    """Return the code graph database path for *home*."""
    return hermes_root(home) / ".code-review-graph" / "graph.db"


class GraphDomain(SnapshotDomain[None]):
    """The code-review-graph store, served from narrow indexed queries.

    This domain does not read a snapshot: every page queries the database, because each
    query is already narrow and indexed.  What it *does* cache is the one thing that is
    slow -- ``count(*)`` over 1.5M edges takes seconds, so it is taken once per
    :data:`EDGE_COUNT_TTL` and returned with the moment it was taken, in the background
    when a page would otherwise wait.  (The base class's snapshot machinery is optional
    for exactly this reason.)
    """

    key = "graph"
    title = "Code graph"
    summary = (
        "Communities, risky nodes and callers from the code-review-graph store Hermes "
        "rebuilds nightly."
    )

    def __init__(
        self, hermes_home: Path | None = None, graph_db: Path | None = None
    ) -> None:
        """Point at the store; nothing is queried until a page asks.

        Args:
            hermes_home: Hermes home or profile directory.
            graph_db: Explicit database path; defaults to
                ``<root>/.code-review-graph/graph.db``.
        """
        super().__init__(hermes_home)
        self.db_path = (
            Path(graph_db) if graph_db is not None else graph_path(hermes_home)
        )
        self.cache = Cache()

    def _open(self) -> tuple[Any, str]:
        return open_sqlite(self.db_path)

    def _sources(self) -> tuple[Source, ...]:
        return (
            path_source("graph.db", self.db_path, note="read-only, narrow queries"),
        )

    def _metadata(self, con: Any) -> dict[str, str]:
        rows, _error = query(con, "select key, value from metadata")
        return {str(row[0]): str(row[1]) for row in rows}

    def _edge_count(self) -> int:
        """Count the edge table once: 1.5M rows takes seconds, so it is cached."""
        con, _error = self._open()
        value = int(scalar(con, "select count(*) from edges", default=0) or 0)
        _close(con)
        return value

    def _cheap_counts(self) -> dict[str, int]:
        """The counts that are instant on this database."""
        con, _error = self._open()
        counts = {
            "nodes": int(scalar(con, "select count(*) from nodes", default=0) or 0),
            "flows": int(scalar(con, "select count(*) from flows", default=0) or 0),
            "communities": int(
                scalar(con, "select count(*) from communities", default=0) or 0
            ),
        }
        _close(con)
        return counts

    def _counts(self, *, block: bool = False) -> dict[str, Any]:
        """Counts, with the expensive edge count fetched in the background.

        A page must not wait seconds for one number: when the cache is cold the
        metric says so and a daemon thread fills it in, stamped with when it was
        taken.  ``block=True`` is for that thread and for a warm-up.
        """
        counts: dict[str, Any] = self._cheap_counts()
        if block:
            edges, stamp = self.cache.get("edges", EDGE_COUNT_TTL, self._edge_count)
        else:
            edges, stamp = self.cache.peek("edges", EDGE_COUNT_TTL)
            if edges is None:
                threading.Thread(
                    target=self.cache.get,
                    args=("edges", EDGE_COUNT_TTL, self._edge_count),
                    name="graph-edge-count",
                    daemon=True,
                ).start()
        counts["edges"] = edges
        counts["_stamp"] = stamp
        return counts

    def _build_collection(self) -> Collection:
        """The store's own build facts, one record per metadata row."""
        con, error = self._open()
        metadata = self._metadata(con)
        _close(con)
        built = metadata.get("last_updated", "")
        age_hours = (age_seconds(built) or 0) / 3600 if built else None
        notes = [note for note in (error,) if note]
        if not self.db_path.is_file():
            notes.append(f"no graph database at {self.db_path}")
        if age_hours is not None and age_hours > STALE_AFTER_HOURS:
            notes.append(
                f"the graph was last built {age_hours:.0f}h ago; the nightly rebuild "
                "should be keeping it fresher"
            )
        records = [
            Record(id=key, title=key, subtitle=str(value))
            for key, value in sorted(metadata.items())
        ]
        if self.db_path.is_file():
            records.append(
                Record(
                    id="size",
                    title="size on disk",
                    subtitle=f"{human_size(self.db_path.stat().st_size)} · "
                    f"modified {fmt_ago(self.db_path.stat().st_mtime)}",
                )
            )
        return build_collection(
            "build",
            "Build",
            "When the graph was built, at which schema version, and how big it is.",
            "rows in the graph's metadata table",
            records,
            sources=self._sources(),
            notes=tuple(notes),
            as_of=as_of(),
            unavailable=unreadable(error, self.db_path),
        )

    def overview(self) -> Collection:
        """What is in the graph, and how fresh the build is."""
        con, error = self._open()
        metadata = self._metadata(con)
        _close(con)
        counts = self._counts(block=False)
        stamp = str(counts.get("_stamp", ""))
        nodes = int(counts.get("nodes", 0))
        raw_edges = counts.get("edges")
        edges = int(raw_edges) if raw_edges is not None else None
        flows = int(counts.get("flows", 0))
        communities = int(counts.get("communities", 0))
        built = metadata.get("last_updated", "")
        age_hours = (age_seconds(built) or 0) / 3600 if built else None
        stale = age_hours is not None and age_hours > STALE_AFTER_HOURS
        notes = [note for note in (error,) if note]
        if not self.db_path.is_file():
            notes.append(f"no graph database at {self.db_path}")
        if stale:
            notes.append(
                f"the graph was last built {age_hours:.0f}h ago; a nightly rebuild "
                "should be keeping it fresher"
            )
        notes.append(
            f"the edge count takes seconds over 1.5M rows, so it is computed in the "
            f"background once per {int(EDGE_COUNT_TTL / 60)} min and cached"
            + (f" (taken {stamp})" if stamp else " (not counted yet)")
        )
        return build_collection(
            "overview",
            "Code graph",
            "The builder's own tables: communities, risky nodes and flows.",
            "communities detected by the builder",
            self._communities_collection().records,
            cap=5,
            sources=self._sources(),
            extra_counts=(
                Count(communities, "communities"),
                Count(flows, "execution flows"),
                Count(edges or 0, "edges (cached count; 0 while it is being computed)"),
            ),
            metrics=(
                ("Nodes", f"{nodes:,}"),
                ("Edges", f"{edges:,}" if edges is not None else "counting…"),
                ("Communities", f"{communities:,}"),
                ("Flows", f"{flows:,}"),
                ("Last build", fmt_ago(built) if built else "\u2014"),
                (
                    "Size",
                    human_size(self.db_path.stat().st_size)
                    if self.db_path.is_file()
                    else "\u2014",
                ),
            ),
            notes=tuple(notes),
            as_of=as_of(),
            unavailable=unreadable(error, self.db_path),
        )

    def _communities_collection(self) -> Collection:
        """Communities with the summary the builder wrote for each."""
        con, error = self._open()
        rows, sql_error = query(
            con,
            "select c.id as id, c.name as name, c.level as level, c.size as size, "
            "c.dominant_language as language, c.cohesion as cohesion, "
            "s.purpose as purpose, s.risk as risk, s.key_symbols as key_symbols "
            "from communities c "
            "left join community_summaries s on s.community_id = c.id "
            "order by c.size desc",
        )
        _close(con)
        return build_collection(
            "communities",
            "Communities",
            "Clusters the builder detected, with its own summary of each.",
            "rows in the communities table",
            [
                Record(
                    id=str(_get(row, "id")),
                    title=str(_get(row, "name")),
                    subtitle=snippet(_get(row, "purpose", ""), 180)
                    or f"{_get(row, 'language')} cluster",
                    badges=(
                        f"{_get(row, 'size')} nodes",
                        str(_get(row, "language")),
                        f"risk {_get(row, 'risk')}",
                    ),
                    fields=(
                        ("id", str(_get(row, "id"))),
                        ("name", str(_get(row, "name"))),
                        ("size", str(_get(row, "size"))),
                        ("level", str(_get(row, "level"))),
                        ("language", str(_get(row, "language"))),
                        ("cohesion", str(_get(row, "cohesion"))),
                        ("risk", str(_get(row, "risk"))),
                        ("purpose", truncate(str(_get(row, "purpose", "")), 400)),
                        (
                            "key symbols",
                            truncate(str(_get(row, "key_symbols", "")), 400),
                        ),
                    ),
                )
                for row in rows
            ],
            cap=COMMUNITY_CAP,
            sources=self._sources(),
            notes=tuple(note for note in (error, sql_error) if note),
            as_of=as_of(),
            unavailable=unreadable(error, self.db_path),
        )

    def _risky_collection(self) -> Collection:
        """The riskiest nodes, straight from risk_index."""
        con, error = self._open()
        rows, sql_error = query(
            con,
            "select node_id, qualified_name, risk_score, caller_count, test_coverage, "
            "security_relevant from risk_index order by risk_score desc limit ?",
            (RISK_CAP,),
        )
        scored = int(scalar(con, "select count(*) from risk_index", default=0) or 0)
        _close(con)
        return build_collection(
            "risky",
            "Risky nodes",
            "Highest risk scores, with caller count and test coverage as the "
            "builder saw them.",
            f"top {RISK_CAP} of the risk_index population, by score",
            [
                Record(
                    id=str(_get(row, "qualified_name")),
                    title=truncate(str(_get(row, "qualified_name")), 90),
                    subtitle=f"{_get(row, 'caller_count')} callers · "
                    f"coverage {_get(row, 'test_coverage')}",
                    badges=(
                        f"risk {_get(row, 'risk_score')}",
                        "security-relevant"
                        if _get(row, "security_relevant")
                        else "not flagged",
                    ),
                    links=(
                        (detail_url("graph", _get(row, "qualified_name")), "Open node"),
                    ),
                    fields=(
                        ("qualified name", str(_get(row, "qualified_name"))),
                        ("risk score", str(_get(row, "risk_score"))),
                        ("callers", str(_get(row, "caller_count"))),
                        ("test coverage", str(_get(row, "test_coverage"))),
                        ("security relevant", str(_get(row, "security_relevant"))),
                    ),
                )
                for row in rows
            ],
            sources=self._sources(),
            extra_counts=(Count(scored, "nodes with a risk score"),),
            notes=tuple(note for note in (error, sql_error) if note),
            as_of=as_of(),
            unavailable=unreadable(error, self.db_path),
        )

    def _callers_collection(self) -> Collection:
        """Most-called nodes, from the precomputed caller count."""
        con, error = self._open()
        rows, sql_error = query(
            con,
            "select qualified_name, caller_count, risk_score from risk_index "
            "order by caller_count desc limit ?",
            (CALLER_CAP,),
        )
        _close(con)
        return build_collection(
            "callers",
            "Most-called nodes",
            "The busiest functions by caller count -- a degree ranking the builder "
            "already computed.",
            "nodes ranked by precomputed caller count",
            [
                Record(
                    id=str(_get(row, "qualified_name")),
                    title=truncate(str(_get(row, "qualified_name")), 90),
                    subtitle=f"risk {_get(row, 'risk_score')}",
                    badges=(f"{_get(row, 'caller_count')} callers",),
                    links=(
                        (detail_url("graph", _get(row, "qualified_name")), "Open node"),
                    ),
                    fields=(
                        ("qualified name", str(_get(row, "qualified_name"))),
                        ("callers", str(_get(row, "caller_count"))),
                        ("risk score", str(_get(row, "risk_score"))),
                    ),
                )
                for row in rows
            ],
            sources=self._sources(),
            notes=tuple(note for note in (error, sql_error) if note)
            + (
                "a degree count over the 1.5M-row edge table takes ~4s per query, so "
                "the builder's precomputed count is used instead",
            ),
            as_of=as_of(),
            unavailable=unreadable(error, self.db_path),
        )

    def _flows_collection(self) -> Collection:
        """Execution flows by criticality."""
        con, error = self._open()
        rows, sql_error = query(
            con,
            "select id, name, criticality, depth, node_count, file_count from flows "
            "order by criticality desc limit ?",
            (FLOW_CAP,),
        )
        _close(con)
        return build_collection(
            "flows",
            "Execution flows",
            "Entry-point paths the builder traced, most critical first.",
            "flows ranked by criticality",
            [
                Record(
                    id=str(_get(row, "id")),
                    title=truncate(str(_get(row, "name")), 90),
                    subtitle=f"{_get(row, 'node_count')} nodes in "
                    f"{_get(row, 'file_count')} files · depth {_get(row, 'depth')}",
                    badges=(f"criticality {_get(row, 'criticality')}",),
                    fields=(
                        ("id", str(_get(row, "id"))),
                        ("name", str(_get(row, "name"))),
                        ("criticality", str(_get(row, "criticality"))),
                        ("depth", str(_get(row, "depth"))),
                        ("nodes", str(_get(row, "node_count"))),
                        ("files", str(_get(row, "file_count"))),
                    ),
                )
                for row in rows
            ],
            sources=self._sources(),
            notes=tuple(note for note in (error, sql_error) if note),
            as_of=as_of(),
            unavailable=unreadable(error, self.db_path),
        )

    def collections(
        self, _filters: Mapping[str, str] | None = None
    ) -> Sequence[Collection]:
        """Drill-down collections for the code graph."""
        return [
            self._build_collection(),
            self._communities_collection(),
            self._risky_collection(),
            self._callers_collection(),
            self._flows_collection(),
        ]

    def _node(self, con: Any, qualified_name: str) -> Any | None:
        rows, _error = query(
            con,
            "select id, name, qualified_name, kind, language, file_path, line_start, "
            "line_end, parent_name, params, return_type, is_test, signature "
            "from nodes where qualified_name = ? limit 1",
            (qualified_name,),
        )
        return rows[0] if rows else None

    def detail(self, record_id: str) -> Record | None:
        """One node: where it lives and what the builder says about it."""
        con, error = self._open()
        row = self._node(con, record_id)
        if row is None:
            _close(con)
            return None
        risk_rows, _risk_error = query(
            con,
            "select risk_score, caller_count, test_coverage, security_relevant "
            "from risk_index where qualified_name = ? limit 1",
            (record_id,),
        )
        _close(con)
        risk = risk_rows[0] if risk_rows else None
        return Record(
            id=str(_get(row, "qualified_name")),
            title=str(_get(row, "name")),
            subtitle=f"{_get(row, 'qualified_name')}",
            badges=(
                str(_get(row, "kind")),
                str(_get(row, "language")),
                "test" if _get(row, "is_test") else "source",
            ),
            fields=(
                ("qualified name", str(_get(row, "qualified_name"))),
                ("kind", str(_get(row, "kind"))),
                ("language", str(_get(row, "language"))),
                ("file", f"{_get(row, 'file_path')}:{_get(row, 'line_start')}"),
                ("lines", f"{_get(row, 'line_start')}-{_get(row, 'line_end')}"),
                ("parent", str(_get(row, "parent_name"))),
                ("return type", str(_get(row, "return_type"))),
                ("is test", str(_get(row, "is_test"))),
                ("signature", truncate(str(_get(row, "signature", "")), 300)),
                ("risk score", str(_get(risk, "risk_score")) if risk else "\u2014"),
                ("callers", str(_get(risk, "caller_count")) if risk else "\u2014"),
                (
                    "test coverage",
                    str(_get(risk, "test_coverage")) if risk else "\u2014",
                ),
            ),
            links=(("/graph", "All communities"),),
            body=truncate(str(_get(row, "params", "")), 1000),
        )

    def detail_sections(self, record_id: str) -> Sequence[Collection]:
        """Behind a node: its edges, its flows and its community."""
        con, error = self._open()
        node = self._node(con, record_id)
        if node is None:
            _close(con)
            return []
        node_id = _get(node, "id")
        out_rows, out_error = query(
            con,
            "select kind, target_qualified, file_path, line, confidence from edges "
            "where source_qualified = ? limit ?",
            (record_id, EDGE_CAP),
        )
        in_rows, in_error = query(
            con,
            "select kind, source_qualified, file_path, line, confidence from edges "
            "where target_qualified = ? limit ?",
            (record_id, EDGE_CAP),
        )
        flow_rows, flow_error = query(
            con,
            "select f.id as id, f.name as name, f.criticality as criticality "
            "from flow_memberships m join flows f on f.id = m.flow_id "
            "where m.node_id = ? limit ?",
            (node_id, EDGE_CAP),
        )
        community_rows, community_error = query(
            con,
            "select c.name as name, c.size as size, s.purpose as purpose "
            "from nodes n join communities c on c.id = n.community_id "
            "left join community_summaries s on s.community_id = c.id "
            "where n.id = ? limit 1",
            (node_id,),
        )
        _close(con)

        def edge_collection(key: str, title: str, rows: Any, other: str) -> Collection:
            return build_collection(
                key,
                title,
                f"Edges where this node is the {other}.",
                f"edges with this node as {other} (capped at {EDGE_CAP})",
                [
                    Record(
                        id=f"{key}-{index}",
                        title=truncate(str(_get(row, other)), 90),
                        subtitle=f"{_get(row, 'kind')} · {_get(row, 'file_path')}:"
                        f"{_get(row, 'line')}",
                        badges=(
                            str(_get(row, "kind")),
                            f"confidence {_get(row, 'confidence')}",
                        ),
                    )
                    for index, row in enumerate(rows)
                ],
                sources=self._sources(),
                as_of=as_of(),
                unavailable=unreadable(error, self.db_path),
            )

        sections = [
            edge_collection("edges-out", "Edges out", out_rows, "target_qualified"),
            edge_collection("edges-in", "Edges in", in_rows, "source_qualified"),
            build_collection(
                "flows",
                "Flows through this node",
                "Execution flows this node takes part in.",
                "flow memberships for this node",
                [
                    Record(
                        id=str(_get(row, "id")),
                        title=truncate(str(_get(row, "name")), 90),
                        badges=(f"criticality {_get(row, 'criticality')}",),
                    )
                    for row in flow_rows
                ],
                sources=self._sources(),
                as_of=as_of(),
                unavailable=unreadable(error, self.db_path),
            ),
            build_collection(
                "community",
                "Community",
                "The cluster the builder filed this node under.",
                "communities containing this node",
                [
                    Record(
                        id=str(_get(row, "name")),
                        title=str(_get(row, "name")),
                        subtitle=snippet(_get(row, "purpose", ""), 160),
                        badges=(f"{_get(row, 'size')} nodes",),
                    )
                    for row in community_rows
                ],
                sources=self._sources(),
                as_of=as_of(),
                unavailable=unreadable(error, self.db_path),
            ),
        ]
        notes = tuple(
            note
            for note in (error, out_error, in_error, flow_error, community_error)
            if note
        )
        if notes and sections:
            sections[0] = build_collection(
                sections[0].key,
                sections[0].title,
                sections[0].description,
                sections[0].count.definition,
                list(sections[0].records),
                sources=self._sources(),
                notes=notes,
                as_of=as_of(),
            )
        return sections

    def search(self, needle: str, limit: int) -> Sequence[Record]:
        """Full-text search over node names, paths and signatures via nodes_fts."""
        term = needle.strip()
        if not term:
            return []
        con, _error = self._open()
        phrase = '"' + term.replace('"', '""') + '"'
        rows, sql_error = query(
            con,
            "select o.qualified_name as qualified_name, o.kind as kind, "
            "o.language as language, o.file_path as file_path "
            "from nodes_fts f join nodes o on o.id = f.rowid "
            "where nodes_fts match ? limit ?",
            (phrase, limit),
        )
        if sql_error:
            rows, _like_error = query(
                con,
                "select qualified_name, kind, language, file_path from nodes "
                "where qualified_name like ? limit ?",
                (f"%{term}%", limit),
            )
        _close(con)
        return [
            Record(
                id=str(_get(row, "qualified_name")),
                title=truncate(str(_get(row, "qualified_name")), 90),
                subtitle=f"{_get(row, 'kind')} · {_get(row, 'file_path')}",
                badges=("node", str(_get(row, "language"))),
                links=(
                    (detail_url("graph", _get(row, "qualified_name")), "Open node"),
                ),
            )
            for row in rows
        ]


def build_domain(
    hermes_home: Path | None = None, graph_db: Path | None = None
) -> Domain:
    """Build the code graph domain.

    Args:
        hermes_home: Hermes home or profile directory.
        graph_db: Explicit database path; defaults to
            ``<root>/.code-review-graph/graph.db``.

    Returns:
        A :class:`~hermes.portal.model.Domain`.  Reads are narrow and indexed, and the
        one expensive count is cached with the moment it was taken.
    """
    return GraphDomain(hermes_home, graph_db).domain()
