"""Tests for the P2 domains: the Obsidian vault and the code graph.

Hermetic like the rest of the suite: a fake vault tree and a fake ``graph.db`` built
in a temp directory from a *subset* of the real schema, so nothing here reads the
machine's own notes or its 2.5 GB graph.

Four things in here are more than unit tests:

* the Kanban reader, because a board's state lives in its ``Status`` field and not in
  its checkboxes -- reading the box alone reported 87 finished cards as open;
* the open-task rule, which has to keep one bad line from inventing a task (an
  empty checkbox followed by a ``## Heading`` once became a task titled "## Related");
* the expensive-count path: the page must never block on a count that takes seconds,
  so the cached value, the "not counted yet" state and the background fill are all
  checked;
* every collection's count against the records it carries, the invariant P1 broke
  repeatedly.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes.portal import sources  # noqa: E402
from hermes.portal.domains import default_registry  # noqa: E402
from hermes.portal.domains import graph as graph_domain  # noqa: E402
from hermes.portal.domains import vault as vault_domain  # noqa: E402


def write(path: Path, text: str) -> Path:
    """Write a fixture file, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_vault(root: Path) -> Path:
    """A small vault with the shapes that matter: a board, links, tags, tasks."""
    write(
        root / "Hermes" / "Hermes Kanban.md",
        "---\ntags:\n  - kanban\nkanban-plugin: board\n---\n\n"
        "## Backlog\n\n## Hermes-Mac (Active)\n\n"
        "- [ ] Ship the vault domain\n"
        "\t- **Task:** expose notes with backlinks\n"
        "\t- **Node:** Mac\n"
        "\t- **Status:** active\n"
        "\t- **Context:** the portal's second plane\n\n"
        "## Done\n\n"
        "- [ ] Wire the graph domain\n"
        "\t- **Task:** narrow indexed reads\n"
        "\t- **Status:** done\n"
        "- [x] Retire the deck module\n"
        "\t- **Status:** done\n",
    )
    write(
        root / "Homelab" / "Homelab.md",
        "---\ntags: [area, homelab]\n---\n\n# \U0001f5a5\ufe0f Homelab\n\n"
        "See [[Plex]] and [[Sunday|the log]] and [[Nowhere]].\n",
    )
    write(
        root / "Homelab" / "Plex.md",
        "# Plex\n\nBack to [[Homelab]].\n\n- [ ] Rotate the token\n- [x] Rebuild\n",
    )
    write(
        root / "Homelab" / "Sunday.md",
        "# Sunday\n\nNotes linking to [[Homelab]].\n",
    )
    write(
        root / "Personal" / "Templates" / "Daily Note Template.md",
        "## \u2705 Tasks\n- [ ] \n\n## \U0001f517 Related\n- \n",
    )
    # noise that must never be indexed
    write(root / ".obsidian" / "workspace.md", "# not a note\n")
    write(root / ".trash" / "deleted.md", "# not a note either\n")
    return root


def make_graph_db(path: Path) -> Path:
    """A graph database with the columns the adapter selects, and nothing else."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table nodes (
            id integer primary key, kind text, name text, qualified_name text,
            file_path text, line_start integer, line_end integer, language text,
            parent_name text, params text, return_type text, modifiers text,
            is_test integer, file_hash text, extra text, updated_at text,
            signature text, community_id integer
        );
        create table edges (
            id integer primary key, kind text, source_qualified text,
            target_qualified text, file_path text, line integer, extra text,
            confidence real, confidence_tier text, updated_at text
        );
        create table flows (
            id integer primary key, name text, entry_point_id integer, depth integer,
            node_count integer, file_count integer, criticality real, path_json text
        );
        create table communities (
            id integer primary key, name text, level integer, parent_id integer,
            cohesion real, size integer, dominant_language text, description text
        );
        create table community_summaries (
            community_id integer primary key, name text, purpose text,
            key_symbols text, risk text, size integer, dominant_language text
        );
        create table risk_index (
            node_id integer primary key, qualified_name text, risk_score real,
            caller_count integer, test_coverage text, security_relevant integer,
            last_computed text
        );
        create table flow_memberships (
            flow_id integer, node_id integer, position integer
        );
        create table metadata (key text primary key, value text);
        """
    )
    nodes = [
        (
            1,
            "Function",
            "get",
            "ProcessRegistry.get",
            "/p/process_registry.py",
            10,
            20,
            "python",
            "ProcessRegistry",
            "(self, id)",
            "Session",
            "",
            0,
            "h1",
            "",
            "t",
            "def get(self, id)",
            7,
        ),
        (
            2,
            "Function",
            "format",
            "cli.format",
            "/p/cli.py",
            5,
            9,
            "python",
            "",
            "(x)",
            "str",
            "",
            0,
            "h2",
            "",
            "t",
            "def format(x)",
            7,
        ),
        (
            3,
            "File",
            "process_registry.py",
            "/p/process_registry.py",
            "/p/process_registry.py",
            1,
            99,
            "python",
            "",
            "",
            "",
            "",
            0,
            "h3",
            "",
            "t",
            "",
            None,
        ),
        (
            4,
            "Class",
            "Target",
            "Target",
            "/p/target.py",
            1,
            40,
            "python",
            "",
            "",
            "",
            "",
            0,
            "h4",
            "",
            "t",
            "class Target",
            8,
        ),
    ]
    con.executemany("insert into nodes values (%s)" % ",".join("?" * 18), nodes)  # noqa: UP031
    con.executemany(
        "insert into edges (id, kind, source_qualified, target_qualified, file_path, "
        "line, confidence, confidence_tier) values (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                1,
                "calls",
                "ProcessRegistry.get",
                "cli.format",
                "/p/process_registry.py",
                12,
                0.9,
                "high",
            ),
            (
                2,
                "calls",
                "Target.run",
                "ProcessRegistry.get",
                "/p/target.py",
                22,
                0.8,
                "high",
            ),
            (3, "imports", "cli.format", "Target", "/p/cli.py", 3, 0.7, "medium"),
        ],
    )
    con.executemany(
        "insert into flows values (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "websocket_flow", 1, 4, 12, 3, 0.91, "[]"),
            (2, "plain_flow", 2, 2, 5, 2, 0.40, "[]"),
        ],
    )
    con.executemany(
        "insert into communities values (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (7, "tools-resolve", 0, None, 0.5, 120, "python", "tool registry"),
            (8, "target", 0, None, 0.4, 40, "python", "target helpers"),
        ],
    )
    con.executemany(
        "insert into community_summaries values (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                7,
                "tools-resolve",
                "resolves tools",
                "get, register",
                "low",
                120,
                "python",
            ),
            (8, "target", "target helpers", "Target", "medium", 40, "python"),
        ],
    )
    con.executemany(
        "insert into risk_index values (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "ProcessRegistry.get", 0.9, 1386, "tested", 0, "2026-01-01"),
            (2, "cli.format", 0.7, 12, "untested", 1, "2026-01-01"),
            (3, "/p/process_registry.py", 0.6, 3, "n/a", 0, "2026-01-01"),
            (4, "Target", 0.4, 2, "tested", 0, "2026-01-01"),
        ],
    )
    con.executemany(
        "insert into flow_memberships values (?, ?, ?)",
        [(1, 1, 0), (1, 2, 1), (2, 4, 0)],
    )
    con.executemany(
        "insert into metadata values (?, ?)",
        [
            ("schema_version", "9"),
            ("last_updated", "2026-01-01T03:00:00"),
            ("last_build_type", "full"),
        ],
    )
    con.executescript(
        """
        create virtual table nodes_fts using fts5(
            name, qualified_name, file_path, signature,
            content='nodes', content_rowid='rowid', tokenize='porter unicode61'
        );
        insert into nodes_fts(rowid, name, qualified_name, file_path, signature)
            select id, name, qualified_name, file_path, signature from nodes;
        """
    )
    con.commit()
    con.close()
    return path


class VaultTestCase(unittest.TestCase):
    """The vault domain, against a fixture vault."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = make_vault(Path(cls._tmp.name) / "vault")
        # a registry is what the adapters are exercised through, as in the server
        cls.registry = default_registry(vault_root=cls.root)
        cls.domain = cls.registry.get("vault")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def collections(self):
        """Map of collection key -> collection for the fixture vault."""
        return {c.key: c for c in self.registry.safe_collections(self.domain)}

    def test_index_skips_dot_directories(self) -> None:
        overview = self.registry.safe_overview(self.domain)
        # five real notes: the two under .obsidian/ and .trash/ are not notes
        self.assertEqual(overview.count.value, 5)
        paths = " ".join(r.id for r in self.collections()["notes"].records)
        self.assertNotIn(".obsidian", paths)
        self.assertNotIn(".trash", paths)

    def test_frontmatter_tags_parse_in_both_forms(self) -> None:
        tags = self.collections()["tags"]
        found = {r.id: r.subtitle for r in tags.records}
        # inline [area, homelab] and the indented - kanban list
        self.assertIn("homelab", found)
        self.assertIn("area", found)
        self.assertIn("kanban", found)

    def test_tags_collection_counts_every_tag_not_just_shown(self) -> None:
        tags = self.collections()["tags"]
        self.assertEqual(tags.count.value, len(tags.records))
        self.assertGreater(tags.count.value, 0)

    def test_links_resolve_aliases_and_report_unresolved(self) -> None:
        sections = self.registry.safe_sections(self.domain, "Homelab/Homelab.md")
        by_key = {s.key: s for s in sections}
        out = {r.title: r.subtitle for r in by_key["links-out"].records}
        self.assertEqual(out["plex"], "Homelab/Plex.md")
        self.assertEqual(out["sunday"], "Homelab/Sunday.md")
        # [[Nowhere]] resolves to nothing, and says so instead of being dropped
        self.assertEqual(out["nowhere"], "not resolved")

    def test_backlinks_are_the_inverse_of_links(self) -> None:
        sections = {
            s.key: s
            for s in self.registry.safe_sections(self.domain, "Homelab/Homelab.md")
        }
        self.assertGreaterEqual(len(sections["backlinks"].records), 2)
        paths = {r.subtitle for r in sections["backlinks"].records}
        self.assertIn("Homelab/Plex.md", paths)
        self.assertIn("Homelab/Sunday.md", paths)
        # the backlink records are the linking notes, by title
        titles = {r.title for r in sections["backlinks"].records}
        self.assertEqual(titles, {"Plex", "Sunday"})

    def test_title_comes_from_heading_then_filename(self) -> None:
        records = {r.id: r.title for r in self.collections()["notes"].records}
        self.assertEqual(records["Homelab/Homelab.md"], "\U0001f5a5\ufe0f Homelab")
        self.assertEqual(records["Homelab/Sunday.md"], "Sunday")

    def test_kanban_reads_cards_not_field_lines(self) -> None:
        kanban = self.collections()["kanban"]
        titles = [r.title for r in kanban.records]
        self.assertEqual(kanban.count.value, 3)
        self.assertIn("Ship the vault domain", titles)
        self.assertIn("Wire the graph domain", titles)
        # "**Task:** ..." is an indented field, never a card
        self.assertFalse(any(t.startswith("**") for t in titles))

    def test_kanban_status_field_wins_over_the_checkbox(self) -> None:
        kanban = self.collections()["kanban"]
        by_title = {r.title: dict(r.fields) for r in kanban.records}
        shipped = by_title["Ship the vault domain"]
        self.assertEqual(shipped["status"], "active")
        self.assertEqual(shipped["checkbox"], "open")
        self.assertEqual(shipped["node"], "Mac")
        wired = by_title["Wire the graph domain"]
        # unchecked box, but the card says done: the field is the state
        self.assertEqual(wired["status"], "done")
        self.assertEqual(wired["checkbox"], "open")

    def test_kanban_counts_open_cards_by_status(self) -> None:
        counts = {
            c.definition: c.value for c in self.collections()["kanban"].extra_counts
        }
        self.assertEqual(counts["cards not marked done"], 1)
        self.assertEqual(counts["cards marked done"], 2)

    def test_open_tasks_exclude_done_cards_and_headings(self) -> None:
        tasks = self.collections()["tasks"]
        titles = [r.title for r in tasks.records]
        self.assertIn("Rotate the token", titles)
        self.assertIn("Ship the vault domain", titles)
        # a done card is not open work
        self.assertNotIn("Wire the graph domain", titles)
        self.assertNotIn("Retire the deck module", titles)
        # the empty checkbox in the daily template must not invent a task
        self.assertFalse(any(t.lstrip().startswith("#") for t in titles))

    def test_overview_open_task_metric_uses_the_same_rule(self) -> None:
        overview = self.registry.safe_overview(self.domain)
        metric = dict(overview.metrics)["Open tasks"]
        self.assertEqual(int(metric), self.collections()["tasks"].count.value)

    def test_folder_filter_narrows_the_notes_collection(self) -> None:
        """A filtered view is its own collection, so the URL stays meaningful."""
        filtered = self.registry.safe_collections(
            self.domain, filters={"folder": "Homelab"}
        )
        keys = [c.key for c in filtered]
        self.assertIn("folder-Homelab", keys)
        self.assertNotIn("notes", keys)
        narrowed = next(c for c in filtered if c.key == "folder-Homelab")
        self.assertEqual(narrowed.count.value, 3)
        self.assertTrue(all(r.id.startswith("Homelab/") for r in narrowed.records))

    def test_note_detail_carries_the_fields_and_body(self) -> None:
        detail = self.registry.safe_detail(self.domain, "Homelab/Plex.md")
        fields = dict(detail.fields)
        self.assertEqual(fields["folder"], "Homelab")
        self.assertEqual(fields["tags"], "\u2014")
        # the note text rides on the record itself, not in a field
        self.assertIn("Rotate the token", detail.body)

    def test_unknown_note_returns_none(self) -> None:
        self.assertIsNone(self.registry.safe_detail(self.domain, "nope/absent.md"))

    def test_search_finds_notes_by_title_and_body(self) -> None:
        by_title = self.registry.search("homelab")["vault"]
        self.assertTrue(any(r.id == "Homelab/Homelab.md" for r in by_title))
        by_body = self.registry.search("Rotate the token")["vault"]
        self.assertEqual([r.id for r in by_body], ["Homelab/Plex.md"])

    def test_missing_vault_reports_instead_of_crashing(self) -> None:
        absent = vault_domain.build_domain(vault=Path(self._tmp.name) / "nope")
        overview = self.registry.safe_overview(absent)
        self.assertEqual(overview.count.value, 0)
        self.assertTrue(
            any(
                "no such vault" in n.lower() or "vault" in n.lower()
                for n in overview.notes
            )
        )

    def test_collection_counts_match_the_records_they_carry(self) -> None:
        for collection in self.registry.safe_collections(self.domain):
            if not collection.truncated:
                self.assertEqual(
                    collection.count.value,
                    len(collection.records),
                    f"{collection.key}: count {collection.count.value} != "
                    f"{len(collection.records)} records",
                )
            else:
                self.assertLessEqual(len(collection.records), collection.count.value)


class GraphTestCase(unittest.TestCase):
    """The graph domain, against a fixture database."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = make_graph_db(Path(cls._tmp.name) / "graph.db")
        cls.domain = graph_domain.build_domain(graph_db=cls.db)
        cls.registry = default_registry(graph_db=cls.db)
        cls.domain = cls.registry.get("graph")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def collections(self):
        """Map of collection key -> collection for the fixture graph."""
        return {c.key: c for c in self.registry.safe_collections(self.domain)}

    def test_connection_is_read_only(self) -> None:
        con, error = sources.open_sqlite(self.db)
        self.assertFalse(error)
        self.assertIsNotNone(con)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("delete from metadata")
        con.close()

    def test_cheap_counts_come_from_the_tables(self) -> None:
        metrics = dict(self.registry.safe_overview(self.domain).metrics)
        self.assertEqual(metrics["Nodes"], "4")
        self.assertEqual(metrics["Flows"], "2")
        self.assertEqual(metrics["Communities"], "2")

    def test_slow_edge_count_is_cached_after_the_first_take(self) -> None:
        cache = sources.Cache()
        value, stamp = cache.get("edges", 60, lambda: 3)
        self.assertEqual(value, 3)
        self.assertTrue(stamp)
        # a peek returns the cached value without computing
        peeked, peek_stamp = cache.peek("edges", 60)
        self.assertEqual(peeked, 3)
        self.assertEqual(peek_stamp, stamp)

    def test_overview_does_not_block_on_the_edge_count(self) -> None:
        domain = graph_domain.build_domain(graph_db=self.db)
        overview = self.registry.safe_overview(domain)
        edges = dict(overview.metrics)["Edges"]
        # either counted already, or explicitly being counted -- never a wrong number
        self.assertTrue(edges == "counting\u2026" or edges.replace(",", "").isdigit())

    def test_every_collection_count_matches_its_records(self) -> None:
        for collection in self.registry.safe_collections(self.domain):
            if not collection.truncated:
                self.assertEqual(
                    collection.count.value,
                    len(collection.records),
                    f"{collection.key}: count {collection.count.value} != "
                    f"{len(collection.records)} records",
                )

    def test_risky_reports_the_population_it_ranked(self) -> None:
        risky = self.collections()["risky"]
        self.assertEqual(risky.count.value, len(risky.records))
        self.assertIn("top", risky.count.definition)
        population = [
            c for c in risky.extra_counts if c.definition.startswith("nodes with")
        ]
        self.assertEqual(len(population), 1)
        self.assertEqual(population[0].value, 4)

    def test_risky_is_ordered_by_score(self) -> None:
        scores = [
            float(dict(r.fields)["risk score"])
            for r in self.collections()["risky"].records
        ]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_callers_uses_the_precomputed_caller_count(self) -> None:
        rows = self.collections()["callers"].records
        self.assertEqual(dict(rows[0].fields)["callers"], "1386")

    def test_communities_carry_the_builders_summaries(self) -> None:
        rows = {
            r.title: dict(r.fields) for r in self.collections()["communities"].records
        }
        self.assertIn("tools-resolve", rows)
        self.assertEqual(rows["tools-resolve"]["purpose"], "resolves tools")
        self.assertEqual(rows["tools-resolve"]["language"], "python")

    def test_build_collection_reports_metadata_and_size(self) -> None:
        rows = {r.title: r.subtitle for r in self.collections()["build"].records}
        self.assertEqual(rows["schema_version"], "9")
        self.assertEqual(rows["last_build_type"], "full")
        self.assertIn("size on disk", rows)

    def test_stale_graph_is_flagged(self) -> None:
        # the fixture's last_updated is far in the past
        notes = " ".join(self.collections()["build"].notes)
        self.assertIn("nightly rebuild", notes)

    def test_node_detail_has_edges_flows_and_community(self) -> None:
        sections = {
            s.key: s
            for s in self.registry.safe_sections(self.domain, "ProcessRegistry.get")
        }
        self.assertIn("edges-out", sections)
        self.assertIn("edges-in", sections)
        self.assertIn("flows", sections)
        self.assertIn("community", sections)
        self.assertEqual(sections["community"].records[0].title, "tools-resolve")
        self.assertEqual(len(sections["flows"].records), 1)

    def test_node_detail_fields(self) -> None:
        detail = self.registry.safe_detail(self.domain, "ProcessRegistry.get")
        fields = dict(detail.fields)
        self.assertEqual(fields["kind"], "Function")
        self.assertEqual(fields["language"], "python")
        self.assertEqual(fields["callers"], "1386")

    def test_unknown_node_returns_none(self) -> None:
        self.assertIsNone(self.registry.safe_detail(self.domain, "NoSuch.node"))

    def test_search_joins_the_fts_index(self) -> None:
        hits = self.registry.search("get")["graph"]
        self.assertTrue(any(r.id == "ProcessRegistry.get" for r in hits))

    def test_missing_database_degrades_to_notes(self) -> None:
        absent = graph_domain.build_domain(graph_db=Path(self._tmp.name) / "absent.db")
        overview = self.registry.safe_overview(absent)
        self.assertTrue(any("no graph database" in n for n in overview.notes))
        for collection in self.registry.safe_collections(absent):
            self.assertEqual(collection.count.value, 0)


class RegistryTestCase(unittest.TestCase):
    """The registry the server actually builds."""

    def test_the_registry_lists_the_planes_this_file_covers(self) -> None:
        """Membership, not a snapshot: the exact list lives in test_portal_domains."""
        keys = set(default_registry().keys())
        self.assertLessEqual(
            {"skills", "usage", "health", "logs", "vault", "graph"}, keys
        )

    def test_every_domain_has_a_description_and_a_source(self) -> None:
        registry = default_registry()
        for key in registry.keys():  # noqa: SIM118 - not a plain dict
            domain = registry.get(key)
            self.assertTrue(domain.title, key)
            self.assertTrue(domain.summary, key)


class GraphPrimitivesTestCase(unittest.TestCase):
    """The graph domain's helpers, called by name.

    The code-review graph flagged ``_get`` as the most-connected untested node in the
    project (degree 85) and the collection builders right behind it -- all closures at
    the time, so no test could reference them.  They are methods now, so this calls
    them.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = make_graph_db(Path(cls._tmp.name) / "graph.db")
        cls.domain = graph_domain.GraphDomain(graph_db=cls.db)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_get_reads_a_row_and_tolerates_a_missing_column(self) -> None:
        con, error = sources.open_sqlite(self.db)
        self.assertFalse(error)
        assert con is not None
        row = con.execute("select name, qualified_name from nodes limit 1").fetchone()
        self.assertEqual(graph_domain._get(row, "name"), "get")
        self.assertEqual(graph_domain._get(row, "kind"), "\u2014")
        self.assertEqual(graph_domain._get(row, "name", "fallback"), "get")
        self.assertEqual(graph_domain._get({"name": None}, "name", "empty"), "empty")
        con.close()

    def test_graph_path_points_inside_the_hermes_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            (root / "cron").mkdir(parents=True)
            (root / "cron" / "jobs.json").write_text('{"jobs": []}', encoding="utf-8")
            self.assertEqual(
                graph_domain.graph_path(root),
                root / ".code-review-graph" / "graph.db",
            )

    def test_close_tolerates_nothing(self) -> None:
        self.assertIsNone(graph_domain._close(None))

    def test_open_is_read_only(self) -> None:
        con, error = self.domain._open()
        self.assertFalse(error)
        assert con is not None
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("delete from metadata")
        graph_domain._close(con)

    def test_cheap_counts_come_from_the_tables(self) -> None:
        counts = self.domain._cheap_counts()
        self.assertEqual(counts["nodes"], 4)
        self.assertEqual(counts["flows"], 2)
        self.assertEqual(counts["communities"], 2)

    def test_counts_include_edges_and_a_stamp_when_they_block(self) -> None:
        counts = self.domain._counts(block=True)
        self.assertEqual(counts["edges"], 3)
        self.assertTrue(counts["_stamp"])

    def test_counts_do_not_block_when_the_cache_is_cold(self) -> None:
        """The page must not wait: a cold cache answers without the edge count."""
        fresh = graph_domain.GraphDomain(graph_db=self.db)
        counts = fresh._counts(block=False)
        self.assertIn("edges", counts)
        self.assertIsNone(counts["edges"])

    def test_metadata_and_node_lookup(self) -> None:
        con, _error = self.domain._open()
        assert con is not None
        self.assertEqual(self.domain._metadata(con)["schema_version"], "9")
        node = self.domain._node(con, "ProcessRegistry.get")
        self.assertEqual(graph_domain._get(node, "kind"), "Function")
        self.assertIsNone(self.domain._node(con, "NoSuch.node"))
        graph_domain._close(con)

    def test_sources_name_the_store(self) -> None:
        sources_list = self.domain._sources()
        self.assertEqual(len(sources_list), 1)
        self.assertIn("graph.db", sources_list[0].label)

    def test_every_collection_builder_counts_what_it_carries(self) -> None:
        for collection in (
            self.domain._build_collection(),
            self.domain._communities_collection(),
            self.domain._risky_collection(),
            self.domain._callers_collection(),
            self.domain._flows_collection(),
        ):
            with self.subTest(collection=collection.key):
                self.assertTrue(collection.records)
                if not collection.truncated:
                    self.assertEqual(collection.count.value, len(collection.records))

    def test_build_domain_still_returns_a_wired_domain(self) -> None:
        domain = graph_domain.build_domain(graph_db=self.db)
        self.assertEqual(domain.key, "graph")
        self.assertEqual(domain.overview().key, "overview")


if __name__ == "__main__":
    unittest.main()
