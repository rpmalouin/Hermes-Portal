"""Tests for message search: the query, the excerpt, and the fallback chain.

These are the three parts that were wrong or missing before this phase:

* the **query** was a quoted phrase, so `dashboard parser` matched nothing -- a search
  box is not FTS5 syntax, and every operator-ish character has to be neutralised
  without losing the reader's meaning;
* the **excerpt** was the message's first 200 characters, so a hit could show a
  paragraph that never mentions the search term (the same bug class as the memory
  domain's shifted snippet);
* the **fallback** did not exist: with no word match there was nothing, even though
  `state.db` also ships a trigram index that answers `ashboa` for `dashboard`.

Hermetic: a fixture database with real FTS5 tables, plus the shared fake Hermes root for
the domain integration.  The nastiest strings are thrown at the query builder on
purpose:
nothing a reader can type may raise, and nothing may be read as an operator.
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

from hermes.portal import fts, render, sources  # noqa: E402
from hermes.portal.domains import default_registry  # noqa: E402
from tests.test_portal import make_hermes_root  # noqa: E402

MESSAGES = (
    (1, "s-one", "assistant", None, 100.0, "The dashboard parser lives in hermes_cli."),
    (2, "s-one", "user", None, 101.0, "Where is the dashboard again? Dashboard!"),
    (3, "s-two", "tool", "terminal", 102.0, "launchctl list | grep dashboard"),
    (4, "s-two", "assistant", None, 103.0, "Nothing about that here."),
)


def make_db(path: Path, *, word: bool = True, trigram: bool = True) -> Path:
    """A fixture state.db with messages and whichever FTS tables are asked for."""
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table messages (
            id integer primary key, session_id text, role text, tool_name text,
            timestamp real, content text
        );
        """
    )
    con.executemany("insert into messages values (?,?,?,?,?,?)", MESSAGES)
    for name, enabled in (("messages_fts", word), ("messages_fts_trigram", trigram)):
        if not enabled:
            continue
        if name.endswith("trigram"):
            con.executescript(
                f"create virtual table {name} using fts5(content, tokenize='trigram');"
            )
        else:
            con.executescript(f"create virtual table {name} using fts5(content);")
        con.execute(
            f"insert into {name}(rowid, content) select id, content from messages"
        )
    con.commit()
    con.close()
    return path


class QueryTestCase(unittest.TestCase):
    """The query builder: user text in, FTS5 expression out, no surprises."""

    def test_plain_terms_become_and_ed_prefix_matches(self) -> None:
        expression, terms = fts.build_match("dashboard parser")
        self.assertEqual(terms, ("dashboard", "parser"))
        self.assertEqual(expression, '"dashboard"* AND "parser"*')

    def test_operators_are_read_as_ordinary_text(self) -> None:
        cases = {
            "-extra": ("extra",),
            '"quoted"': ("quoted",),
            "a*": ("a",),
            "NEAR(a b)": ("NEAR", "a", "b"),
            "col:value": ("col", "value"),
            "AND OR NOT": ("AND", "OR", "NOT"),
            "foo-bar": ("foo", "bar"),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                expression, terms = fts.build_match(query)
                self.assertEqual(terms, expected)
                # every term is quoted with a prefix star, and nothing else is syntax
                self.assertNotIn("-", expression.replace('"', ""))
                self.assertEqual(expression.count('"'), 2 * len(expected))

    def test_only_word_characters_survive(self) -> None:
        _expression, terms = fts.build_match("**((dash===board))&&&")
        self.assertEqual(terms, ("dash", "board"))

    def test_unicode_terms_are_kept(self) -> None:
        _expression, terms = fts.build_match("caf\u00e9 na\u00efve")
        self.assertEqual(terms, ("caf\u00e9", "na\u00efve"))

    def test_nothing_searchable_yields_an_empty_expression(self) -> None:
        for query in ("", "   ", "-", "***", "()", "\x00\x01", None):
            with self.subTest(query=query):
                self.assertEqual(fts.build_match(query or ""), ("", ()))

    def test_terms_and_length_are_capped(self) -> None:
        _expression, terms = fts.build_match(
            " ".join(f"t{index}" for index in range(40))
        )
        self.assertEqual(len(terms), fts.TERM_LIMIT)
        _expression, terms = fts.build_match("x" * 200)
        self.assertEqual(len(terms[0]), fts.TERM_CHARS)

    def test_control_characters_cannot_reach_the_expression(self) -> None:
        expression, _terms = fts.build_match("dash\x02board\x03")
        self.assertNotIn(fts.HIT_OPEN, expression)
        self.assertNotIn(fts.HIT_CLOSE, expression)

    def test_nothing_a_reader_can_type_raises(self) -> None:
        nasty = (
            '"',
            '""',
            "*",
            "NEAR(",
            "a NEAR/b",
            "AND AND",
            "' OR 1=1 --",
            "col:val AND (x OR y)",
            "\U0001f600",
            "a" * 5000,
            "\t\n",
        )
        for query in nasty:
            with self.subTest(query=query[:20]):
                expression, terms = fts.build_match(query)
                self.assertIsInstance(expression, str)
                self.assertIsInstance(terms, tuple)

    def test_substring_expression_needs_three_characters(self) -> None:
        self.assertEqual(fts.substring_expression("ab"), "")
        self.assertEqual(fts.substring_expression("  ab  "), "")
        self.assertEqual(fts.substring_expression("abc"), '"abc"')
        self.assertEqual(fts.substring_expression('a"b'), '"a""b"')


class SearchTestCase(unittest.TestCase):
    """Searching a fixture database."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def open(self, **kwargs):
        """Open a fixture database read-only, like the portal does."""
        make_db(self.path, **kwargs)
        con, error = sources.open_sqlite(self.path)
        self.assertFalse(error)
        assert con is not None
        return con

    def test_the_excerpt_contains_the_hit_and_marks_it(self) -> None:
        con = self.open()
        rows, index_used, _note = fts.search_messages(con, "dashboard", 5)
        self.assertEqual(index_used, fts.WORD_INDEX)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertIn("dashboard", row["excerpt"].lower())
            self.assertIn(fts.HIT_OPEN, row["excerpt"])
            self.assertIn(fts.HIT_CLOSE, row["excerpt"])
        con.close()

    def test_results_are_ranked_by_relevance(self) -> None:
        """``order by rank`` is bm25 order: the message that says it twice wins."""
        con = self.open()
        rows, _index, _note = fts.search_messages(con, "dashboard", 5)
        self.assertEqual(rows[0]["id"], 2, "the doubled mention should rank first")
        scores = [row["score"] for row in rows]
        self.assertEqual(scores, sorted(scores), "rank order must be monotonic")
        con.close()

    def test_a_substring_falls_through_to_the_trigram_index(self) -> None:
        con = self.open()
        rows, index_used, note = fts.search_messages(con, "ashboa", 5)
        self.assertEqual(index_used, fts.SUBSTRING_INDEX)
        self.assertIn("substring search", note)
        # the trigram match sits inside the word, so the markers split it: compare the
        # text with the markers removed, which is what a reader sees highlighted
        plain = [
            row["excerpt"].replace(fts.HIT_OPEN, "").replace(fts.HIT_CLOSE, "").lower()
            for row in rows
        ]
        self.assertTrue(all("dashboard" in text for text in plain), plain)
        con.close()

    def test_without_the_trigram_index_a_substring_falls_through_to_like(self) -> None:
        """The word index cannot find a substring, so the last resort answers."""
        con = self.open(trigram=False)
        rows, index_used, note = fts.search_messages(con, "ashboa", 5)
        self.assertEqual(index_used, "like")
        self.assertIn("LIKE scan", note)
        self.assertEqual(len(rows), 3)
        con.close()

    def test_with_no_index_at_all_it_falls_back_to_like(self) -> None:
        con = self.open(word=False, trigram=False)
        rows, index_used, note = fts.search_messages(con, "dashboard", 5)
        self.assertEqual(index_used, "like")
        self.assertIn("LIKE scan", note)
        self.assertEqual(len(rows), 3)
        # the LIKE path selects the content itself, so the excerpt is the message start
        self.assertIn("dashboard", rows[0]["excerpt"].lower())
        con.close()

    def test_an_empty_query_returns_nothing(self) -> None:
        con = self.open()
        for query in ("", "   ", "---"):
            self.assertEqual(fts.search_messages(con, query, 5)[0], [])
        self.assertEqual(fts.search_messages(con, "dashboard", 0)[0], [])
        con.close()

    def test_no_match_says_so(self) -> None:
        con = self.open()
        rows, index_used, note = fts.search_messages(con, "zzznope", 5)
        self.assertEqual(rows, [])
        self.assertEqual(index_used, "")
        self.assertIn("nothing matched", note)
        con.close()

    def test_nothing_a_reader_can_type_raises(self) -> None:
        con = self.open()
        for query in ('"', "*", "NEAR(", "-", "' OR 1=1 --", "a AND", "\U0001f600"):
            with self.subTest(query=query[:12]):
                rows, _index, _note = fts.search_messages(con, query, 5)
                self.assertIsInstance(rows, list)
        con.close()

    def test_the_limit_is_respected(self) -> None:
        con = self.open()
        rows, _index, _note = fts.search_messages(con, "dashboard", 1)
        self.assertEqual(len(rows), 1)
        con.close()

    def test_a_missing_database_is_reported_not_raised(self) -> None:
        rows, index_used, note = fts.search_messages(None, "dashboard", 5)
        self.assertEqual(rows, [])
        self.assertEqual(index_used, "")
        self.assertIn("no state.db", note)

    def test_recent_messages_are_newest_first_and_not_marked(self) -> None:
        con = self.open()
        rows, note = fts.recent_messages(con, 3)
        self.assertEqual(note, "")
        self.assertEqual([row["id"] for row in rows], [4, 3, 2])
        self.assertTrue(all(fts.HIT_OPEN not in row["excerpt"] for row in rows))
        self.assertEqual(fts.count_messages(con), 4)
        con.close()

    def test_available_indexes_names_what_exists(self) -> None:
        con = self.open()
        self.assertEqual(
            fts.available_indexes(con), (fts.WORD_INDEX, fts.SUBSTRING_INDEX)
        )
        con.close()
        con = self.open(word=False, trigram=False)
        self.assertEqual(fts.available_indexes(con), ())
        con.close()


class HighlightTestCase(unittest.TestCase):
    """The renderer's half of the contract: escape first, mark second."""

    def test_markers_become_mark_tags(self) -> None:
        html = render.rich(f"before {fts.HIT_OPEN}hit{fts.HIT_CLOSE} after")
        self.assertEqual(html, "before <mark>hit</mark> after")

    def test_a_hostile_message_cannot_inject_through_a_marker(self) -> None:
        html = render.rich(f"{fts.HIT_OPEN}<script>alert(1)</script>{fts.HIT_CLOSE}")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertTrue(html.startswith("<mark>"))

    def test_plain_text_is_unchanged(self) -> None:
        self.assertEqual(render.rich("no markers here"), "no markers here")


class SessionsIntegrationTestCase(unittest.TestCase):
    """The domain: message hits first, then titles, and a browsable collection."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_hermes_root(Path(self._tmp.name))
        self.registry = default_registry(hermes_home=self.root)
        self.domain = self.registry.get("sessions")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_search_returns_message_hits_with_a_marked_excerpt(self) -> None:
        hits = self.registry.search("cron parser")["sessions"]
        messages = [hit for hit in hits if hit.id.startswith("message-")]
        self.assertTrue(messages)
        for hit in messages:
            self.assertTrue(
                any(badge.startswith("via ") for badge in hit.badges), hit.badges
            )
            self.assertIn(fts.HIT_OPEN, hit.subtitle)
            self.assertIn(fts.HIT_CLOSE, hit.subtitle)
            self.assertTrue(
                any(href.startswith("/sessions/") for href, _label in hit.links),
                f"{hit.id}: links={hit.links!r} badges={hit.badges!r}",
            )

    def test_message_hits_come_before_title_hits(self) -> None:
        # "fix" is in a message body ("please fix the cron parser") *and* in a session
        # title ("Fix the thing"), so one query exercises both halves
        hits = self.registry.search("fix")["sessions"]
        self.assertTrue(
            [hit for hit in hits if hit.id.startswith("message-")], "no message hits"
        )
        self.assertTrue(
            [hit for hit in hits if hit.id.startswith("session-")], "no title hits"
        )
        kinds = [
            "message" if hit.id.startswith("message-") else "session" for hit in hits
        ]
        self.assertEqual(kinds, sorted(kinds, key=lambda kind: kind != "message"))
        title_hits = [hit for hit in hits if hit.id.startswith("session-")]
        for hit in title_hits:
            self.assertIn("title match", hit.badges)

    def test_the_messages_collection_is_browsable(self) -> None:
        collections = {
            collection.key: collection
            for collection in self.registry.safe_collections(self.domain)
        }
        self.assertIn("messages", collections)
        messages = collections["messages"]
        counts = {count.definition: count.value for count in messages.extra_counts}
        self.assertIn("rows in the messages table", counts)
        self.assertTrue(
            any("searchable through" in note for note in messages.notes), messages.notes
        )
        self.assertTrue(messages.records)
        self.assertIn("sessions", collections, "the session index is still there")

    def test_the_overview_reports_the_search_index(self) -> None:
        overview = self.registry.safe_overview(self.domain)
        counts = {count.definition: count.value for count in overview.extra_counts}
        self.assertIn(f"rows in the {fts.WORD_INDEX} search index", counts)
        self.assertTrue(
            any(fts.WORD_INDEX in note for note in overview.notes), overview.notes
        )

    def test_collection_counts_match_their_records(self) -> None:
        for collection in self.registry.safe_collections(self.domain):
            if not collection.truncated:
                self.assertEqual(
                    collection.count.value,
                    len(collection.records),
                    f"{collection.key}: {collection.count.value} != "
                    f"{len(collection.records)}",
                )

    def test_a_session_without_a_messages_table_still_serves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_hermes_root(Path(tmp))
            con = sqlite3.connect(root / "state.db")
            con.executescript("drop table messages_fts; drop table messages;")
            con.commit()
            con.close()
            registry = default_registry(hermes_home=root)
            domain = registry.get("sessions")
            self.assertFalse(registry.search("anything")["sessions"])
            messages = next(
                collection
                for collection in registry.safe_collections(domain)
                if collection.key == "messages"
            )
            self.assertEqual(messages.count.value, 0)
            self.assertTrue(any("no messages" in note for note in messages.notes))


if __name__ == "__main__":
    unittest.main()
