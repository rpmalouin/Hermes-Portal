"""A recorded snapshot of the Hermes stores the portal reads.

The DDL below is pasted verbatim from a live Hermes: the six tables the adapters
declare columns in, plus the rows' shapes -- ``state.db`` holds epoch seconds as REAL,
``cron/executions.db`` holds ISO-8601 strings as TEXT.  It is the one copy of that
shape in the repository, shared by the doctor's tests (a fixture that must satisfy the
declared contract) and by ``tools/drift_check.py`` (fixtures that break it on purpose).

Keeping it recorded rather than generated from a running Hermes is what makes both
hermetic: neither a test nor CI needs a real install, and a drift check can run on a
machine whose Hermes is the thing being suspected.

This is a *snapshot*, so it goes stale as Hermes moves.  That is a feature of the
doctor's tests (they fail loudly when a declared column no longer exists here) and a
thing to remember when reading it: it describes one known-good shape, not the contract.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

STATE_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    compression_ineffective_count INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0, "system_prompt_hash" TEXT,
        "title_source" TEXT, "last_activity_at" REAL,
        "last_activity_description" TEXT, "last_activity_provenance" TEXT,
        "last_read_at" REAL, "git_metadata_generation" INTEGER NOT NULL DEFAULT 0,
        "hidden" INTEGER NOT NULL DEFAULT 0, "compression_recovery_deadline" REAL,
        "tool_names" TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    effect_disposition TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    api_content TEXT,
    display_kind TEXT,
    display_metadata TEXT
, "_compressed_summary" INTEGER NOT NULL DEFAULT 0, "display_identity" BLOB,
        "display_order" INTEGER);
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT,
    cost_source TEXT,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url,
        billing_mode, task)
);
CREATE TABLE gateway_heartbeats (
    backend_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    started_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT ''
);
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version (version) VALUES (30);
INSERT INTO sessions (id, source, model, started_at, ended_at, title, end_reason,
                      billing_provider)
    VALUES ('s-1', 'desktop', 'deepseek-flash', 1789657135.0, 1789657200.0,
            'a session', NULL, 'deepseek'),
           ('s-2', 'gateway', 'gemini-3.6-flash', 1789650000.0, NULL,
            'another', NULL, NULL);
INSERT INTO messages (session_id, role, content, timestamp)
    VALUES ('s-1', 'user', 'hello', 1789657136.0),
           ('s-1', 'assistant', 'hi', 1789657137.0);
INSERT INTO session_model_usage (session_id, model, billing_provider, task,
                                 api_call_count, input_tokens, output_tokens,
                                 estimated_cost_usd, actual_cost_usd)
    VALUES ('s-1', 'deepseek-flash', 'deepseek', '', 3, 1200, 400, 0.0123, 0.0121);
INSERT INTO gateway_heartbeats (backend_id, pid, started_at, last_heartbeat)
    VALUES ('backend-1', 4242, 1789650000.0, 1789657200.0);
"""

CRON_DDL = """
CREATE TABLE executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           , handoff_pending INTEGER NOT NULL DEFAULT 0, handoff_started_at REAL,
        delivery_outcome TEXT, scheduled_instant TEXT);
CREATE TABLE cron_incidents (
             id            TEXT PRIMARY KEY,
             job_id        TEXT NOT NULL,
             error_sig     TEXT NOT NULL,
             state         TEXT NOT NULL,
             failure_type  TEXT NOT NULL DEFAULT 'unknown',
             first_seen_at TEXT NOT NULL,
             last_seen_at  TEXT NOT NULL,
             acked_at      TEXT,
             closed_at     TEXT,
             error         TEXT NOT NULL,
             output_file   TEXT
           );
INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at,
                        started_at, finished_at)
    VALUES ('run-1', 'job-1', 'scheduler', 'p-1', 100, 'completed',
            '2026-09-18T09:00:00+00:00', '2026-09-18T09:00:00.500000+00:00',
            '2026-09-18T09:00:02.000000+00:00');
INSERT INTO cron_incidents (id, job_id, error_sig, state, first_seen_at,
                            last_seen_at, error)
    VALUES ('inc-1', 'job-1', 'boom', 'open', '2026-09-18T08:00:00+00:00',
            '2026-09-18T08:00:00+00:00', 'boom');
"""

JOBS_JSON = json.dumps({"jobs": [{"id": "job-1", "name": "nightly", "enabled": True}]})

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


def build_root(tmp: Path) -> Path:
    """Build a Hermes root from the snapshot and return it.

    Args:
        tmp: A directory this may create ``hermes/`` inside.

    Returns:
        The root, carrying ``state.db``, ``cron/executions.db`` and ``cron/jobs.json``
        -- including that file, which is what marks a directory as a root rather than
        a profile.
    """
    root = tmp / "hermes"
    (root / "cron").mkdir(parents=True)
    state = sqlite3.connect(root / "state.db")
    state.executescript(STATE_DDL)
    state.commit()
    state.close()
    cron = sqlite3.connect(root / "cron" / "executions.db")
    cron.executescript(CRON_DDL)
    cron.commit()
    cron.close()
    (root / "cron" / "jobs.json").write_text(JOBS_JSON, encoding="utf-8")
    return root


def make_graph_db(root: Path) -> Path:
    """Add the code graph store: enough for a presence check and a version hint."""
    path = root / ".code-review-graph" / "graph.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    for table in GRAPH_TABLES:
        con.execute(
            f"create table {table} (id integer primary key, key text, value text)"
        )
    con.execute("insert into metadata (key, value) values ('schema_version', '9')")
    con.commit()
    con.close()
    return path
