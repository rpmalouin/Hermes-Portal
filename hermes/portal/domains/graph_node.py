"""Behind one node: its own row, its risk, and the collections that hang off it.

``graph.py`` serves the graph's index pages -- the build facts, the communities, the
risky nodes, the most-called nodes and the flows.  This module is the page for a
*single* node: the record behind ``/graph/<qualified name>`` and the sections under it
(edges out, edges in, the flows through it, the community it was filed under).

It is separate because that concern had grown into the largest method in the file --
117 lines of `detail_sections` -- holding its only nested closure, and because a helper
nobody can call by name is a helper no test can reach *on its own*.  The page's
**results** were covered through the registry all along: the record's fields, the four
section keys, the community it was filed under, and a node the graph does not have.
What the move gave names to is the machinery underneath -- the lookup by itself, each
section's definition and badges, the branch that hangs a failed query's notes off the
first section, a zero kept beside a real count, and a store that will not open.

The seam is a typed one: :class:`NodeStore` names exactly the three things a node page
needs from the domain, so this module cannot quietly grow a dependency on the rest of
it.  The arrow points one way (``graph`` -> here), so there is no cycle to work around.
Two details are deliberate: a connection **per call**, because every query here is
narrow and indexed, and a local ``_get``/``_close``, because the adapters in this
package are self-contained -- ``sessions``, ``usage``, ``cron`` and ``health`` each
carry their own rather than importing one.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from ..model import Collection, Record, Source, build_collection, filter_url
from ..sources import as_of, query, snippet, truncate

#: How many edges a node's page shows in each direction.
EDGE_CAP = 40


class NodeStore(Protocol):
    """What the node page needs from the graph domain, and nothing more."""

    #: The store being read, for the sections' source footer.
    db_path: Path

    def _open(self) -> tuple[Any, str]:
        """A read-only connection, or ``(None, why it could not be opened)``."""
        ...

    def _sources(self) -> tuple[Source, ...]:
        """The store this page reads, described for the page's footer."""
        ...


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


def find_node(con: Any, qualified_name: str) -> Any | None:
    """The row for one node, or ``None`` when the graph has no node by that name.

    Args:
        con: Open connection to the graph store.
        qualified_name: The node's qualified name, which is also its page id.

    Returns:
        The ``nodes`` row, or ``None``.
    """
    rows, _error = query(
        con,
        "select id, name, qualified_name, kind, language, file_path, line_start, "
        "line_end, parent_name, params, return_type, is_test, signature "
        "from nodes where qualified_name = ? limit 1",
        (qualified_name,),
    )
    return rows[0] if rows else None


def edge_collection(
    store: NodeStore, key: str, title: str, rows: Any, other: str
) -> Collection:
    """One direction of a node's edges.

    Args:
        store: The domain the page reads through.
        key: Collection key (``edges-out`` or ``edges-in``).
        title: Collection title.
        rows: Edge rows, already capped.
        other: The column naming the node at the far end of each edge.

    Returns:
        The collection, with the source footer a page needs.
    """
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
        sources=store._sources(),
        as_of=as_of(),
    )


def flow_collection(store: NodeStore, rows: Any) -> Collection:
    """The flows this node takes part in."""
    return build_collection(
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
            for row in rows
        ],
        sources=store._sources(),
        as_of=as_of(),
    )


def community_collection(store: NodeStore, rows: Any) -> Collection:
    """The community the builder filed this node under."""
    return build_collection(
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
            for row in rows
        ],
        sources=store._sources(),
        as_of=as_of(),
    )


def node_detail(store: NodeStore, record_id: str) -> Record | None:
    """One node: where it lives and what the builder says about it.

    Args:
        store: The domain the page reads through.
        record_id: The node's qualified name.

    Returns:
        The record, or ``None`` when the graph has no such node.
    """
    con, error = store._open()
    row = find_node(con, record_id)
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
        links=((filter_url("graph"), "All communities"),),
        body=truncate(str(_get(row, "params", "")), 1000),
    )


def node_sections(store: NodeStore, record_id: str) -> Sequence[Collection]:
    """Behind a node: its edges, its flows and its community.

    The sections are built from one connection, and a query that fails does not empty
    the page: the notes for the failed reads are attached to the *first* section, which
    is the one a reader sees first, so a partly-read page says so instead of looking
    narrow.

    The sections carry no ``unavailable=``, and that is not an omission.  The three
    builders used to pass ``unreadable(error, db_path)``, which cannot fire here:
    ``open_sqlite`` returns a connection **only** alongside an empty error, and a store
    that did not open leaves ``find_node`` with no rows and no node, returning before
    any of these are built.  Carrying a parameter that is always empty into three new
    signatures would be noise; the reachable half (a store that will not open yields no
    sections and no exception) is what a reader should check, and a test pins it.
    """
    con, error = store._open()
    node = find_node(con, record_id)
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

    sections = [
        edge_collection(store, "edges-out", "Edges out", out_rows, "target_qualified"),
        edge_collection(store, "edges-in", "Edges in", in_rows, "source_qualified"),
        flow_collection(store, flow_rows),
        community_collection(store, community_rows),
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
            sources=store._sources(),
            notes=notes,
            as_of=as_of(),
        )
    return sections
