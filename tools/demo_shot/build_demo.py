"""Build a demo Hermes home and vault for a screenshot: no real data in it anywhere.

Schema is replayed from *your* live stores (DDL only, never rows), then filled with
synthetic values; skills are generated from the portal's own curated taxonomy so the
tiles light up the way they do on a real machine.

    python3 tools/demo_shot/build_demo.py [--live-home ~/.hermes] [--out /tmp/portal-demo]

No row is read from the live stores and no real path is written into the fixture.  See
README.md beside this file for the whole shoot: build, serve, scan, capture.
"""

import argparse
import contextlib
import json
import plistlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from hermes.portal import taxonomy  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--live-home",
    default="~/.hermes",
    type=Path,
    help="the real Hermes home whose schema is replayed (default: ~/.hermes)",
)
parser.add_argument(
    "--out",
    default="/tmp/portal-demo",
    type=Path,
    help="where the fixture is written (default: /tmp/portal-demo)",
)
ARGS = parser.parse_args()
LIVE_HOME = ARGS.live_home.expanduser()

LIVE_DB = LIVE_HOME / "state.db"
LIVE_CRON = LIVE_HOME / "cron" / "executions.db"
LIVE_GRAPH = LIVE_HOME / ".code-review-graph" / "graph.db"
ROOT_OUT = ARGS.out.expanduser()
HOME = ROOT_OUT / "home"
VAULT = ROOT_OUT / "vault"
for d in (
    HOME,
    VAULT,
    HOME / "cron" / "output",
    HOME / "logs",
    HOME / "library-logs",
    HOME / "memories",
    HOME / "skills",
    HOME / "portal",
):
    d.mkdir(parents=True, exist_ok=True)


def ddl_from(db: Path, names: tuple[str, ...]) -> list[str]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select sql from sqlite_master where type='table' and sql is not null and name in "
        f"({','.join('?' * len(names))})",
        names,
    ).fetchall()
    indexes = con.execute(
        "select sql from sqlite_master where type='index' and sql is not null and tbl_name in "
        f"({','.join('?' * len(names))})",
        names,
    ).fetchall()
    con.close()
    return [r[0] for r in rows] + [r[0] for r in indexes]


def columns_of(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'pragma table_info("{table}")')]


def insert(con: sqlite3.Connection, table: str, values: dict) -> None:
    """Insert *values* against the table's real columns, filling what it must."""
    info = con.execute(f'pragma table_info("{table}")').fetchall()
    known = {row[1] for row in info}
    # the live schema is replayed as-is: it may require columns this fixture has no
    # opinion about (sessions.source) and lack ones the fixture sets
    # (messages.created_at).  A demo row satisfies it in both directions.
    values = {k: v for k, v in values.items() if k in known}
    # an INTEGER PRIMARY KEY is assigned by SQLite, and feeding it a string is the
    # one thing it refuses with "datatype mismatch"
    for _cid, name, ctype, _nn, _d, pk in info:
        if (
            pk
            and "INT" in (ctype or "").upper()
            and not isinstance(values.get(name), int)
        ):
            values.pop(name, None)
    for _cid, name, ctype, notnull, default, _pk in info:
        if notnull and name not in values and default is None:
            kind = (ctype or "").upper()
            values[name] = 0 if "INT" in kind else (0.0 if "REAL" in kind else "")
    cols = list(values)
    con.execute(
        f"insert into {table} ({','.join(cols)}) values ({','.join('?' * len(cols))})",
        [values[c] for c in cols],
    )


# ---------------------------------------------------------------- state.db
STATE = HOME / "state.db"
STATE.unlink(missing_ok=True)
con = sqlite3.connect(STATE)
TABLES = (
    "sessions",
    "messages",
    "session_model_usage",
    "gateway_heartbeats",
    "messages_fts",
    "messages_fts_trigram",
)
for statement in ddl_from(LIVE_DB, TABLES):
    try:
        con.execute(statement)
    except sqlite3.Error as exc:
        print(f"  skipped DDL ({exc}): {statement[:60]}")

MODELS = [
    ("claude-sonnet-4", "anthropic"),
    ("gpt-5-mini", "openai"),
    ("llama-3.3-70b", "local"),
    ("claude-haiku-4", "anthropic"),
]
SESSIONS = [
    ("s-001", "Render the vault note graph", 3.42, 412_000, 38_500, 61, 24),
    ("s-002", "Tidy the cron job descriptions", 1.10, 96_400, 11_200, 14, 9),
    ("s-003", "Write the release notes", 2.05, 233_800, 27_600, 33, 18),
    ("s-004", "Investigate a failing health probe", 0.87, 88_100, 9_400, 21, 12),
    ("s-005", "Summarise last week's sessions", 1.66, 178_000, 19_800, 27, 15),
    ("s-006", "Add a filter to the skills gallery", 4.21, 505_300, 52_100, 74, 31),
    ("s-007", "Check the log signatures", 0.42, 41_200, 5_100, 9, 6),
    ("s-008", "Plan the plugin inventory page", 2.98, 341_700, 36_900, 48, 22),
    ("s-009", "Fix a reversed detail link", 0.61, 58_900, 7_300, 11, 7),
    ("s-010", "Draft the why-this-exists section", 1.31, 142_600, 16_200, 19, 11),
    ("s-011", "Trace a slow count query", 0.95, 104_500, 12_800, 17, 10),
    ("s-012", "Document the security posture", 2.44, 288_000, 31_400, 39, 20),
]
session_cols = columns_of(con, "sessions")
for sid, title, cost, tin, tout, calls, tools in SESSIONS:
    values = {
        "id": sid,
        "title": title,
        "started_at": f"2026-09-{10 + (int(sid[-3:]) % 8):02d} "
        f"{8 + (int(sid[-3:]) % 11):02d}:15:00",
        "ended_at": f"2026-09-{10 + (int(sid[-3:]) % 8):02d} "
        f"{8 + (int(sid[-3:]) % 11):02d}:52:00",
        "billing_provider": MODELS[int(sid[-3:]) % len(MODELS)][1],
        "model": MODELS[int(sid[-3:]) % len(MODELS)][0],
        "estimated_cost_usd": cost,
        "actual_cost_usd": cost * 0.97,
        "input_tokens": tin,
        "output_tokens": tout,
        "api_call_count": calls,
        "tool_call_count": tools,
        "tool_names": "read_file,patch,terminal",
        "git_branch": "main",
        "cwd": "/srv/portal",
        "message_count": tools * 3,
        "profile_name": "demo",
    }
    insert(con, "sessions", values)

message_cols = columns_of(con, "messages")
LINES = [
    ("user", None, "Have a look at the vault and tell me what is in there."),
    ("assistant", None, "The vault has 18 notes, 24 links and 3 open tasks."),
    ("tool", "read_file", "read vault/Hermes Kanban.md"),
    ("assistant", None, "The board has three columns and four cards."),
    ("user", None, "Which notes are hubs?"),
    ("assistant", None, "Three: the index, the log and the map."),
    ("tool", "terminal", "grep -c '\\[\\[' vault/*.md"),
    ("assistant", None, "Counted 24 wiki links across the notes."),
]
for n, (role, tool, text) in enumerate(LINES * 14):  # ~112 messages
    values = {
        "id": f"m-{n:04d}",
        "session_id": SESSIONS[n % len(SESSIONS)][0],
        "role": role,
        "content": text,
        "tool_name": tool,
        "timestamp": f"2026-09-15 1{n % 9}:{n % 60:02d}:00",
        "created_at": f"2026-09-15 1{n % 9}:{n % 60:02d}:00",
    }
    insert(con, "messages", values)
try:
    con.execute(
        "insert into messages_fts(rowid, content) select rowid, content from messages"
    )
except sqlite3.Error as exc:
    print(f"  FTS fill skipped: {exc}")

usage_cols = columns_of(con, "session_model_usage")
for sid, _t, cost, tin, tout, calls, _tool in SESSIONS:
    for model, provider in MODELS[: 1 + int(sid[-1]) % 3]:
        values = {
            "session_id": sid,
            "model": model,
            "billing_provider": provider,
            "input_tokens": tin // 3,
            "output_tokens": tout // 3,
            "api_call_count": calls // 2,
            "estimated_cost_usd": cost / 3,
        }
        insert(con, "session_model_usage", values)

beat_cols = columns_of(con, "gateway_heartbeats")
for n, (backend, pid) in enumerate([("gateway", 41822), ("desktop", 41960)]):
    values = {
        "backend_id": backend,
        "pid": pid,
        "profile": "demo",
        "host": "demo-host",
        "started_at": 1789000000 + n * 600,
        "last_heartbeat": 1789050000 + n * 30,
    }
    insert(con, "gateway_heartbeats", values)
con.commit()
print(
    f"  state.db: {con.execute('select count(*) from sessions').fetchone()[0]} sessions, "
    f"{con.execute('select count(*) from messages').fetchone()[0]} messages"
)
con.close()

# ---------------------------------------------------------------- cron
CRON = HOME / "cron"
(CRON / "jobs.json").write_text(
    json.dumps(
        {
            "jobs": [
                {
                    "id": "a1",
                    "name": "nightly-backup",
                    "enabled": True,
                    "schedule": "0 3 * * *",
                    "prompt": "Back up the notes directory and report the size.",
                    "script": "backup.sh",
                    "deliver": "local",
                },
                {
                    "id": "a2",
                    "name": "feeds-refresh",
                    "enabled": True,
                    "schedule": "*/30 * * * *",
                    "prompt": "Refresh the tracked feeds and summarise anything new.",
                    "script": "feeds.sh",
                    "deliver": "local",
                },
                {
                    "id": "a3",
                    "name": "weekly-digest",
                    "enabled": True,
                    "schedule": "0 8 * * 1",
                    "prompt": "Write the weekly digest from the week's sessions.",
                    "script": "digest.sh",
                    "deliver": "local",
                },
                {
                    "id": "a4",
                    "name": "disk-check",
                    "enabled": False,
                    "schedule": "0 */6 * * *",
                    "prompt": "Check free space and alert under 10%.",
                    "script": "disk.sh",
                    "deliver": "local",
                },
            ],
            "updated_at": "2026-09-17T21:00:00+00:00",
        },
        indent=2,
    ),
    encoding="utf-8",
)

CEXE = CRON / "executions.db"
CEXE.unlink(missing_ok=True)
ccon = sqlite3.connect(CEXE)
for statement in ddl_from(LIVE_CRON, ("executions", "cron_incidents")):
    with contextlib.suppress(sqlite3.Error):
        ccon.execute(statement)
ex_cols = list(columns_of(ccon, "executions"))
for n in range(48):
    values = {
        "job_id": ["a1", "a2", "a3", "a4"][n % 4],
        "started_at": f"2026-09-{1 + n % 16:02d} 03:00:00",
        "finished_at": f"2026-09-{1 + n % 16:02d} 03:0{n % 9}:30",
        "status": "completed",
        "duration_ms": 4000 + n * 137,
        "exit_code": 0,
        "output_path": f"output/{['a1', 'a2', 'a3', 'a4'][n % 4]}/2026-09-{1 + n % 16:02d}.md",
    }
    if n % 11 == 5:
        values.update(status="failed", exit_code=1)
    insert(ccon, "executions", values)
inc_cols = columns_of(ccon, "cron_incidents")
for n in range(3):
    values = {
        "job_id": "a2",
        "first_seen": f"2026-09-0{n + 2} 06:30:00",
        "last_seen": f"2026-09-0{n + 2} 07:00:00",
        "count": 2 + n,
        "error_signature": "feed fetch timed out after 30s",
        "resolved_at": "",
    }
    insert(ccon, "cron_incidents", values)
ccon.commit()
print(f"  cron: {ccon.execute('select count(*) from executions').fetchone()[0]} runs")
ccon.close()
for job in ("a1", "a3"):
    out = CRON / "output" / job
    out.mkdir(parents=True, exist_ok=True)
    (out / "2026-09-16.md").write_text(
        f"# {job} report\n\nRan at 03:00, finished at 03:01.\n\n"
        "- 412 files, 38 MB, no changes worth reporting\n",
        encoding="utf-8",
    )

# ---------------------------------------------------------------- logs
(HOME / "logs" / "gateway.error.log").write_text(
    "2026-09-16 02:11:04 ERROR feeds: fetch timed out after 30s (attempt 2/3)\n"
    "2026-09-16 02:14:19 WARN  gateway: slow upstream, 4.2s for /api/health\n"
    "2026-09-16 03:00:01 INFO  cron: a1 nightly-backup started\n"
    "2026-09-16 04:22:37 ERROR vault: unreadable note 'drafts/scratch.md'\n"
    "2026-09-16 05:00:02 WARN  auth: rejecting request, api_key=demo-only-not-a-real-key\n"
    "2026-09-16 06:30:00 ERROR feeds: fetch timed out after 30s (attempt 3/3)\n"
    "2026-09-16 07:41:12 ERROR gateway: upstream 502, retrying in 5s\n",
    encoding="utf-8",
)
(HOME / "logs" / "agent.log").write_text(
    "".join(
        f"2026-09-16 {8 + n % 12:02d}:{n % 60:02d}:11 INFO  agent: turn {n} "
        f"model=claude-sonnet-4 tools=3 tokens={1200 + n * 17}\n"
        for n in range(60)
    ),
    encoding="utf-8",
)
(HOME / "library-logs" / "demo-app.log").write_text(
    "2026-09-16 09:00:00 INFO  demo-app: started (pid 41960)\n"
    "2026-09-16 09:04:02 WARN  demo-app: retrying render, 2 attempts left\n"
    "2026-09-16 09:41:33 ERROR demo-app: cannot reach http://127.0.0.1:9999/\n",
    encoding="utf-8",
)
(HOME / "logs" / "desktop.log").write_text(
    "2026-09-16 09:00:00 INFO  desktop: window ready\n", encoding="utf-8"
)

# ---------------------------------------------------------------- memory
SEP = "\u00a7"
(HOME / "memories" / "MEMORY.md").write_text(
    f"Prefers plain answers and short reports.{SEP}\n"
    f"Works from a Linux box, keeps notes in an Obsidian vault.{SEP}\n"
    f"Runs the portal on loopback and reads everything read-only.\n",
    encoding="utf-8",
)
(HOME / "memories" / "USER.md").write_text(
    f"Ada (prefers 'Ada'). Retired engineer, meticulous about labels.{SEP}\n"
    f"Likes counts that say what they count.\n",
    encoding="utf-8",
)

# ---------------------------------------------------------------- skills, from the taxonomy
# One skill per box the taxonomy names -- every box, not the first few.  The index page counts
# what each group is missing, so a demo that covers 24 of the 55 boxes inflates all eight tiles
# into full-width bars with a "N box(es) that no longer exist" list and roughly doubles the
# page height.  (The README caption's "8 groups ... covering 55 of them" depends on this loop.)
made = 0
for group in taxonomy.GROUPS:
    for box in group.boxes:
        for n in range(1 + (made % 2)):
            name = f"{box}-{n + 1}" if made % 2 else box
            skill = HOME / "skills" / box / name
            skill.mkdir(parents=True, exist_ok=True)
            (skill / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Demo skill {made + 1} for the {box} box.\n---\n\n"
                f"# {name}\n\nA synthetic skill, here so the demo screenshot has something to\n"
                f"draw.  It belongs to the **{box}** box in the **{group.title}** group.\n",
                encoding="utf-8",
            )
            (skill / "main.py").write_text(
                '"""A synthetic entry point, only so the code graph has something to read."""\n'
                "\n\n"
                "def main() -> int:\n"
                f'    print("demo skill {name}")\n'
                "    return 0\n\n\n"
                'if __name__ == "__main__":\n'
                "    raise SystemExit(main())\n",
                encoding="utf-8",
            )
            made += 1
print(
    f"  skills: {made} demo skills across {sum(len(g.boxes) for g in taxonomy.GROUPS)} boxes"
)


# ---------------------------------------------------------------- the vault
def note(rel: str, text: str) -> None:
    path = VAULT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


note(
    "index.md",
    "---\ntags: [index, hub]\n---\n\n# Index\n\nStart at [[map]]. Tasks live in "
    "[[Hermes/Hermes Kanban]]. Notes: [[notes/alpha]], [[notes/beta]].\n",
)
note(
    "map.md",
    "---\ntags: [hub]\n---\n\n# Map\n\n[[index]] · [[notes/alpha]] · [[notes/beta]]"
    " · [[log]]\n",
)
note(
    "log.md",
    "---\ntags: [log]\n---\n\n# Log\n\n- 2026-09-10 wrote [[notes/alpha]]\n"
    "- 2026-09-12 reviewed [[notes/beta]]\n",
)
note(
    "Hermes/Hermes Kanban.md",
    "---\ntags:\n  - kanban\nkanban-plugin: board\n---\n\n"
    "## Backlog\n\n- [ ] Add a folder filter\n\t- **Task:** narrow the note list\n"
    "\t- **Status:** backlog\n\n"
    "## Active\n\n- [ ] Write the demo notes\n\t- **Task:** fill the vault\n"
    "\t- **Status:** active\n\n"
    "## Done\n\n- [x] Sketch the index\n\t- **Status:** done\n",
)
for name, tags, body in (
    (
        "alpha",
        "notes",
        "Alpha outlines how the pages fit together.\n\n- [ ] Add a diagram\n",
    ),
    ("beta", "notes", "Beta is the second note; it links back to [[notes/alpha]].\n"),
    (
        "gamma",
        "drafts",
        "Gamma is a draft and mentions [[notes/beta]].\n- [ ] Finish it\n",
    ),
    (
        "delta",
        "drafts",
        "Delta lists open questions.\n- [ ] Ask about [[notes/gamma]]\n",
    ),
    ("epsilon", "ideas", "Epsilon: a list of small ideas.\n"),
    ("zeta", "ideas", "Zeta: another list, longer than [[notes/epsilon]].\n"),
):
    note(f"notes/{name}.md", f"---\ntags: [{tags}]\n---\n\n# {name.title()}\n\n{body}")
for n in range(6):
    note(
        f"archive/old-{n + 1}.md",
        f"---\ntags: [archive]\n---\n\n# Old {n + 1}\n\nAn archived note, kept for the record.\n",
    )

# ---------------------------------------------------------------- favourites, so the rail is not empty
(HOME / "portal").mkdir(parents=True, exist_ok=True)
(HOME / "portal" / "state.json").write_text(
    json.dumps(
        {
            "version": 1,
            "updated_at": "2026-09-17T21:05:00+00:00",
            "favorites": [
                {
                    "domain": "vault",
                    "id": "index.md",
                    "title": "Index",
                    "added_at": "2026-09-17T20:58:00+00:00",
                },
                {
                    "domain": "skills",
                    "id": "tdd/tdd/SKILL.md",
                    "title": "tdd",
                    "added_at": "2026-09-17T21:01:00+00:00",
                },
            ],
        },
        indent=2,
    ),
    encoding="utf-8",
)

print(f"\n  demo home: {HOME}")
print(
    f"  files: {sum(1 for _ in HOME.rglob('*') if _.is_file())} in the home, "
    f"{sum(1 for _ in VAULT.rglob('*.md'))} notes in the vault"
)

# ---------------------------------------------------------------- profiles, so the agents box has agents
# The eleventh domain reads each agent's own store, its own cron and its heartbeats.  A demo
# home with only the root renders that box as a single row -- the least interesting shape of a
# page whose whole point is that one home holds several agents.  Synthetic names only, and the
# schema is still replayed from the live store (DDL, never rows).
DEMO_PROFILES = (
    ("demo-research", 4, 2, False),
    ("demo-ops", 2, 1, True),
    ("demo-preview", 1, 1, False),
)
PROFILE_JOBS = [
    {
        "id": "p1",
        "name": "notes-index",
        "enabled": True,
        "schedule": "*/20 * * * *",
        "prompt": "Index new notes and report what changed.",
        "script": "index.sh",
        "deliver": "local",
    },
    {
        "id": "p2",
        "name": "link-check",
        "enabled": True,
        "schedule": "0 6 * * *",
        "prompt": "Check the notes for broken wiki links.",
        "script": "links.sh",
        "deliver": "local",
    },
]
for profile, session_count, beat_count, has_cron in DEMO_PROFILES:
    base = HOME / "profiles" / profile
    (base / "memories").mkdir(parents=True, exist_ok=True)
    store = base / "state.db"
    if store.exists():
        store.unlink()
    pcon = sqlite3.connect(store)
    for statement in ddl_from(LIVE_DB, ("sessions", "gateway_heartbeats")):
        pcon.execute(statement)
    for n in range(session_count):
        insert(
            pcon,
            "sessions",
            {
                "id": f"{profile}-{n + 1}",
                "title": f"{profile}: task {n + 1}",
                "source": "demo",
                "profile_name": profile,
                "model": MODELS[n % len(MODELS)][0],
                "started_at": 1789000000 + n * 900,
                "ended_at": 1789000600 + n * 900,
                "message_count": 8 * (n + 1),
                "tool_call_count": 3 * (n + 1),
                "api_call_count": 6 * (n + 1),
                "input_tokens": 12000 * (n + 1),
                "output_tokens": 2200 * (n + 1),
                "estimated_cost_usd": 0.42 * (n + 1),
            },
        )
    for n in range(beat_count):
        insert(
            pcon,
            "gateway_heartbeats",
            {
                "backend_id": f"{profile}-gateway-{n + 1}",
                "pid": 43000 + n,
                "profile": profile,
                "host": "demo-host",
                "started_at": 1789000000 + n * 600,
                "last_heartbeat": 1789050000 + n * 30,
            },
        )
    pcon.commit()
    pcon.close()
    (base / "memories" / "MEMORY.md").write_text(
        f"---\ntags: [memory]\n---\n\n- {profile}: a demo entry, kept short.\n",
        encoding="utf-8",
    )
    if has_cron:
        (base / "cron").mkdir(parents=True, exist_ok=True)
        (base / "cron" / "jobs.json").write_text(
            json.dumps(
                {"jobs": PROFILE_JOBS, "updated_at": "2026-09-17T21:05:00+00:00"},
                indent=2,
            ),
            encoding="utf-8",
        )
print(
    f"  profiles: {len(DEMO_PROFILES)} demo agents, "
    f"{sum(p[1] for p in DEMO_PROFILES)} sessions between them, "
    f"cron on {sum(1 for p in DEMO_PROFILES if p[3])}"
)

# ---------------------------------------------------------------- the code graph, demo-shaped
# The graph domain reads the builder's own tables, so the demo needs a store with the same shape:
# replayed DDL, synthetic rows.  Without it the card reads "graph.db could not be read" -- an
# honest message and a poor screenshot.  The nodes are the Portal's own modules, which is a
# truthful thing for a portal demo to show and carries no reader's data.
GRAPH = HOME / ".code-review-graph" / "graph.db"
GRAPH.parent.mkdir(parents=True, exist_ok=True)
if GRAPH.exists():
    GRAPH.unlink()
gcon = sqlite3.connect(GRAPH)
for statement in ddl_from(
    LIVE_GRAPH,
    (
        "metadata",
        "nodes",
        "edges",
        "communities",
        "community_summaries",
        "flows",
        "flow_memberships",
        "risk_index",
        "nodes_fts",
    ),
):
    try:
        gcon.execute(statement)
    except sqlite3.Error as exc:  # a shadow table already made
        print(f"  graph DDL skipped: {exc}")

DEMO_COMMUNITIES = (
    (
        1,
        "portal-server",
        24,
        "python",
        0.62,
        "HTTP routes, JSON endpoints and the handler",
    ),
    (2, "portal-render", 19, "python", 0.58, "One generic page shape for every domain"),
    (
        3,
        "portal-domains",
        31,
        "python",
        0.51,
        "An adapter per domain over one snapshot base",
    ),
    (
        4,
        "portal-model",
        12,
        "python",
        0.66,
        "Domain, Collection, Record and the count rules",
    ),
    (5, "skill-framework", 22, "python", 0.47, "Manifests, the loader, the executor"),
    (6, "skill-trees", 9, "python", 0.55, "Frontmatter, boxes and the symlink forest"),
    (
        7,
        "cron-and-ticker",
        7,
        "python",
        0.44,
        "Scheduled jobs, their runs and their output",
    ),
    (8, "docs-and-config", 5, "python", 0.39, "README, packaging and the ruff config"),
)
for cid, name, size, language, cohesion, purpose in DEMO_COMMUNITIES:
    insert(
        gcon,
        "communities",
        {
            "id": cid,
            "name": name,
            "level": 0,
            "cohesion": cohesion,
            "size": size,
            "dominant_language": language,
            "description": purpose,
        },
    )
    insert(
        gcon,
        "community_summaries",
        {
            "community_id": cid,
            "name": name,
            "purpose": purpose,
            "key_symbols": "handle, render, build",
            "risk": "low" if cid % 3 else "medium",
            "size": size,
            "dominant_language": language,
        },
    )

DEMO_NODES = (
    (
        "Function",
        "do_GET",
        "hermes/portal/server.py::PortalHandler.do_GET",
        "server.py",
        1,
        2,
    ),
    (
        "Class",
        "PortalHandler",
        "hermes/portal/server.py::PortalHandler",
        "server.py",
        3,
        4,
    ),
    (
        "Function",
        "index_payload",
        "hermes/portal/server.py::index_payload",
        "server.py",
        5,
        3,
    ),
    ("Function", "page", "hermes/portal/render.py::page", "render.py", 2, 2),
    ("Function", "rows", "hermes/portal/render.py::rows", "render.py", 1, 2),
    (
        "Function",
        "render_domain",
        "hermes/portal/render.py::render_domain",
        "render.py",
        3,
        2,
    ),
    (
        "Class",
        "SnapshotDomain",
        "hermes/portal/domains/base.py::SnapshotDomain",
        "base.py",
        3,
        3,
    ),
    (
        "Function",
        "build_domain",
        "hermes/portal/domains/vault.py::build_domain",
        "vault.py",
        2,
        3,
    ),
    (
        "Function",
        "build_index",
        "hermes/portal/domains/vault.py::build_index",
        "vault.py",
        3,
        3,
    ),
    (
        "Function",
        "node_sections",
        "hermes/portal/domains/graph_node.py::node_sections",
        "graph_node.py",
        1,
        3,
    ),
    (
        "Function",
        "agent_homes",
        "hermes/portal/domains/agents.py::agent_homes",
        "agents.py",
        1,
        3,
    ),
    ("Class", "Collection", "hermes/portal/model.py::Collection", "model.py", 3, 4),
    (
        "Function",
        "build_collection",
        "hermes/portal/model.py::build_collection",
        "model.py",
        4,
        4,
    ),
    (
        "Function",
        "load_manifest",
        "hermes/core/loader.py::load_manifest",
        "loader.py",
        2,
        5,
    ),
    (
        "Class",
        "SkillRegistry",
        "hermes/core/registry.py::SkillRegistry",
        "registry.py",
        3,
        5,
    ),
    (
        "Function",
        "discover_skills",
        "hermes/core/skill_trees.py::discover_skills",
        "skill_trees.py",
        2,
        6,
    ),
    (
        "Function",
        "load_jobs",
        "hermes/portal/domains/cron.py::_load_jobs",
        "cron.py",
        2,
        7,
    ),
    ("Function", "main", "hermes/portal/server.py::main", "server.py", 5, 1),
)
for nid, (kind, name, qualified, filename, lines, cid) in enumerate(
    DEMO_NODES, start=1
):
    insert(
        gcon,
        "nodes",
        {
            "id": nid,
            "kind": kind,
            "name": name,
            "qualified_name": qualified,
            "file_path": f"/srv/portal/hermes/portal/{filename}",
            "line_start": 10 * nid,
            "line_end": 10 * nid + lines,
            "language": "python",
            "parent_name": name if kind == "Function" else "",
            "return_type": "None",
            "is_test": 0,
            "signature": f"def {name}(...)",
            "community_id": cid,
        },
    )
    try:
        gcon.execute(
            "insert into nodes_fts(rowid, name, qualified_name, file_path, signature) "
            "values (?, ?, ?, ?, ?)",
            (
                nid,
                name,
                qualified,
                f"/srv/portal/hermes/portal/{filename}",
                f"def {name}(...)",
            ),
        )
    except sqlite3.Error as exc:
        print(f"  graph FTS fill skipped: {exc}")

DEMO_EDGES = (
    ("calls", 1, 4),
    ("calls", 1, 5),
    ("calls", 2, 3),
    ("calls", 3, 4),
    ("calls", 6, 4),
    ("calls", 7, 12),
    ("calls", 8, 9),
    ("calls", 9, 12),
    ("calls", 10, 12),
    ("calls", 11, 12),
    ("calls", 13, 12),
    ("calls", 14, 15),
    ("calls", 15, 16),
    ("calls", 17, 12),
    ("imports", 7, 13),
    ("imports", 1, 13),
    ("imports", 10, 4),
    ("imports", 11, 7),
    ("imports", 17, 1),
    ("tested_by", 12, 13),
    ("tested_by", 3, 1),
    ("documents", 16, 8),
)
for eid, (kind, source, target) in enumerate(DEMO_EDGES, start=1):
    insert(
        gcon,
        "edges",
        {
            "id": eid,
            "kind": kind,
            "source_qualified": DEMO_NODES[source - 1][2],
            "target_qualified": DEMO_NODES[target - 1][2],
            "file_path": f"/srv/portal/hermes/portal/{DEMO_NODES[source - 1][3]}",
            "line": 10 * source,
            "confidence": 0.9 if eid % 4 else 0.7,
            "confidence_tier": "high" if eid % 4 else "medium",
        },
    )

DEMO_FLOWS = (
    (1, "request → render → response", 1, 5, 9, 4, 0.93),
    (2, "skill run → executor → output", 2, 4, 6, 3, 0.81),
    (3, "cron tick → job → report", 7, 3, 5, 2, 0.74),
)
for fid, name, entry, depth, nodes_in_flow, files, criticality in DEMO_FLOWS:
    insert(
        gcon,
        "flows",
        {
            "id": fid,
            "name": name,
            "entry_point_id": entry,
            "depth": depth,
            "node_count": nodes_in_flow,
            "file_count": files,
            "criticality": criticality,
            "path_json": "[]",
        },
    )
for fid, member in ((1, 1), (1, 4), (1, 5), (2, 8), (2, 9), (3, 17), (3, 3)):
    insert(gcon, "flow_memberships", {"flow_id": fid, "node_id": member, "position": 0})

for nid, (score, callers, coverage) in enumerate(
    (
        (0.92, 48, "tested"),
        (0.81, 31, "partial"),
        (0.74, 26, "untested"),
        (0.63, 19, "tested"),
        (0.52, 12, "partial"),
        (0.41, 8, "tested"),
    ),
    start=1,
):
    insert(
        gcon,
        "risk_index",
        {
            "node_id": nid,
            "qualified_name": DEMO_NODES[nid - 1][2],
            "risk_score": score,
            "caller_count": callers,
            "test_coverage": coverage,
            "security_relevant": 1 if nid == 1 else 0,
            "last_computed": "2026-09-17T21:00:00",
        },
    )

insert(gcon, "metadata", {"key": "schema_version", "value": "9"})
insert(gcon, "metadata", {"key": "last_updated", "value": "2026-09-17T21:00:00"})
insert(gcon, "metadata", {"key": "last_build_type", "value": "full"})
gcon.commit()
print(
    f"  graph.db: {gcon.execute('select count(*) from nodes').fetchone()[0]} nodes, "
    f"{gcon.execute('select count(*) from communities').fetchone()[0]} communities, "
    f"{gcon.execute('select count(*) from flows').fetchone()[0]} flows"
)
gcon.close()

# ---------------------------------------------------------------- launch agents, so health has services
# The health domain reads the user's LaunchAgents directory and keeps the labels that mention
# Hermes.  Three plists with demo labels give that card something true to describe -- and the
# patched `launchctl list` in the shim deliberately names different labels, so they read as
# configured-but-not-running rather than pretending to be live.
LAUNCH = HOME / "demo-agents"
LAUNCH.mkdir(parents=True, exist_ok=True)
for label, program, run_at_load in (
    (
        "ai.hermes.demo-gateway",
        ["/usr/local/bin/demo-gateway", "run", "--port", "8087"],
        True,
    ),
    ("ai.hermes.demo-ticker", ["/usr/local/bin/demo-ticker", "--interval", "60"], True),
    ("com.hermes.demo-dashboard", ["/usr/local/bin/demo-dashboard", "serve"], False),
):
    with (LAUNCH / f"{label}.plist").open("wb") as handle:
        plistlib.dump(
            {"Label": label, "ProgramArguments": program, "RunAtLoad": run_at_load},
            handle,
        )
print(f"  launch agents: {len(list(LAUNCH.glob('*.plist')))} demo plists")

# ---------------------------------------------------------------- config.yaml, so the memory card is quiet
# The memory domain reads its budgets from this file and reports its absence as a note; a demo
# home that leaves the portal saying "cannot read config.yaml" is a wart in a screenshot.
(HOME / "config.yaml").write_text(
    "# Demo configuration: names only, no credentials of any kind.\n"
    "model:\n"
    "  provider: demo\n"
    "  name: demo-model\n"
    "memory:\n"
    "  memory_char_limit: 2200\n"
    "  user_char_limit: 1375\n"
    "skills:\n"
    "  autoload: true\n",
    encoding="utf-8",
)
print("  config.yaml: demo budgets only")
