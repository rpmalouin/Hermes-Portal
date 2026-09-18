#!/usr/bin/env python3
"""Prove the portal degrades by collection, not by page.

Five fixtures, each built from the recorded Hermes shape in ``tools/schema_snapshot.py``
and then broken the way a Hermes update breaks things: a renamed column with a dropped
table, a reshaped cron store with a truncated ``jobs.json``, a valid ``jobs.json`` whose
keys moved, and a ``state.db`` replaced by random bytes.  Each is served by a real
portal
on a spare port and every route is crawled.

The property under test is the one that matters when Hermes moves:

* **no route stops answering** -- nothing 5xx, no connection refused, the process alive;
* **the affected collections say so** -- "unavailable -- state.db could not be read", or
  the name of the column that went missing;
* **everything else keeps working** -- a broken store silences only what reads it, so
  cron's job definitions and health's launchctl/lsof collections still report real
  numbers while the store beside them is unreadable.

This is the check that found the three bugs the unit suite had passed through, promoted
from a scratch script into the repository so it runs on every push.

    python3 tools/drift_check.py            # stdlib only, no install needed
    python3 tools/drift_check.py --json     # machine-readable summary
    python3 tools/drift_check.py --keep     # leave the fixtures on disk to poke at

Nothing here reads ``~/.hermes``: every fixture is built in a temp directory, the portal
is pointed at it with ``--no-state``, and ``--vault`` is redirected to an empty
directory
so a check never indexes a real vault.
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.schema_snapshot import build_root  # noqa: E402

PAGES = (
    "/",
    "/sessions",
    "/cron",
    "/memory",
    "/graph",
    "/vault",
    "/skills",
    "/plugins",
    "/usage",
    "/logs",
    "/health",
    "/agents",
    "/search?q=probe",
)
JSON_ROUTES = (
    "/index.json",
    "/sessions.json",
    "/cron.json",
    "/usage.json",
    "/health.json",
    "/agents.json",
)

# A read that did not happen says so; anything else is a number the page stands behind.
UNAVAILABLE = "unavailable --"


@dataclass(frozen=True)
class Expect:
    """One collection that must behave a particular way in a scenario."""

    domain: str
    collection: str
    checks: tuple[str, ...]
    why: str = ""


@dataclass(frozen=True)
class Scenario:
    """A fixture, the way it is broken, and what must still hold."""

    name: str
    breaks: str
    mutate: Callable[[Path], None]
    expect: tuple[Expect, ...]


@dataclass
class Result:
    """What one scenario actually did."""

    name: str
    routes_ok: bool = True
    failures: list[str] = field(default_factory=list)
    detail: dict[str, str] = field(default_factory=dict)
    alive: bool = True


# -- the breakage ---------------------------------------------------------------


def mutate_renamed_columns(root: Path) -> None:
    """Two columns renamed and a table dropped: the shape the portal asks for moved."""
    _sql(
        root / "state.db",
        "alter table sessions rename column started_at to created_at",
        "alter table messages rename column session_id to session_uuid",
        "drop table session_model_usage",
    )


def mutate_cron_reshaped(root: Path) -> None:
    """jobs.json truncated mid-write, executions reshaped, incidents gone."""
    (root / "cron" / "jobs.json").write_text(
        '{"jobs": [{"id": "job-1", "name": "nightly"', encoding="utf-8"
    )
    _sql(
        root / "cron" / "executions.db",
        "alter table executions rename column started_at to began_at",
        "drop table cron_incidents",
    )


def mutate_renamed_job_keys(root: Path) -> None:
    """Valid JSON, moved keys: the failure a reader cannot tell from an empty list."""
    (root / "cron" / "jobs.json").write_text(
        json.dumps({"schema_version": 2, "job_definitions": [{"job_id": "job-1"}]}),
        encoding="utf-8",
    )


def mutate_corrupt_state_db(root: Path) -> None:
    """The store is not a database at all -- the worst case, and the quietest."""
    (root / "state.db").write_bytes(b"\x00\x01not a database" * 64)


def _sql(path: Path, *statements: str) -> None:
    """Run statements against a fixture store, failing loudly if one cannot apply."""
    import sqlite3

    con = sqlite3.connect(path)
    try:
        for statement in statements:
            con.execute(statement)
        con.commit()
    finally:
        con.close()


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="baseline",
        breaks="nothing -- the recorded shape as it should be",
        mutate=lambda _root: None,
        expect=(
            Expect(
                "sessions", "sessions", ("readable",), "the table is there and reads"
            ),
            Expect("usage", "by-day", ("readable",)),
            Expect("cron", "jobs", ("readable",)),
            Expect("health", "services", ("readable",), "launchctl, not a store"),
            Expect("agents", "stores", ("readable",), "its own store is read"),
        ),
    ),
    Scenario(
        name="renamed columns",
        breaks=(
            "sessions.started_at, messages.session_id renamed; "
            "session_model_usage dropped"
        ),
        mutate=mutate_renamed_columns,
        expect=(
            Expect(
                "sessions", "sessions", ("readable",), "the table itself still reads"
            ),
            Expect("usage", "by-day", ("mentions:started_at",), "the column is named"),
            Expect("usage", "by-model", ("mentions:session_model_usage",)),
        ),
    ),
    Scenario(
        name="cron reshaped",
        breaks=(
            "jobs.json truncated; executions.started_at renamed; cron_incidents dropped"
        ),
        mutate=mutate_cron_reshaped,
        expect=(
            Expect("cron", "jobs", ("unavailable", "mentions:malformed JSON")),
            Expect("cron", "runs", ("readable",), "the renamed column is not fatal"),
            Expect("cron", "incidents", ("mentions:cron_incidents",)),
        ),
    ),
    Scenario(
        name="renamed job keys",
        breaks="valid jobs.json whose keys moved (job_definitions, job_id)",
        mutate=mutate_renamed_job_keys,
        expect=(
            Expect(
                "cron",
                "jobs",
                ("unavailable", "mentions:unrecognized jobs.json shape"),
                "0 entries beside a present file is the reading a reader trusts",
            ),
        ),
    ),
    Scenario(
        name="corrupt state.db",
        breaks="state.db replaced by random bytes",
        mutate=mutate_corrupt_state_db,
        expect=(
            Expect("sessions", "sessions", ("unavailable", "mentions:not a database")),
            Expect("usage", "by-day", ("unavailable",)),
            Expect(
                "health", "heartbeats", ("unavailable",), "the only health read of it"
            ),
            Expect("health", "services", ("readable",), "silenced only what reads it"),
            Expect("health", "ports", ("readable",)),
            Expect(
                "agents",
                "stores",
                ("mentions:not read",),
                "an agent with an unreadable store says so rather than reading 0",
            ),
        ),
    ),
)


# -- serving and crawling -------------------------------------------------------


def free_port() -> int:
    """A port nothing is listening on (small race, retried by the caller)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def serve(root: Path, port: int, vault: Path, log: Path) -> subprocess.Popen[bytes]:
    """Start a portal for *root* on *port*, logging to *log*."""
    handle = log.open("wb")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hermes.portal",
            "--hermes-home",
            str(root),
            "--no-state",
            "--vault",
            str(vault),
            "--port",
            str(port),
        ],
        cwd=str(REPO),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


def wait_ready(port: int, seconds: float = 25.0) -> bool:
    """Poll until the server answers, or give up."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2):
                return True
        except urllib.error.HTTPError:
            return True  # answering, just not with 200 -- the crawl will judge it
        except Exception:
            time.sleep(0.3)
    return False


def get(port: int, path: str) -> tuple[int, str]:
    """GET one route; status 0 means no response at all."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def collections_of(port: int, domain: str) -> dict[str, dict]:
    """The collections a domain's JSON route serves, keyed by collection key."""
    status, body = get(port, f"/{domain}.json")
    if status != 200:
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return {c["key"]: c for c in payload.get("collections") or []}


def judge(port: int, scenario: Scenario) -> Result:
    """Crawl one served fixture and check both the routes and the collections."""
    result = Result(scenario.name)
    for path in (*PAGES, *JSON_ROUTES):
        status, body = get(port, path)
        result.detail[path] = str(status)
        if status == 0:
            result.routes_ok = False
            result.failures.append(f"{path}: no response ({body[:60]})")
        elif status >= 500:
            result.routes_ok = False
            result.failures.append(f"{path}: HTTP {status}")

    for expect in scenario.expect:
        found = collections_of(port, expect.domain)
        collection = found.get(expect.collection)
        if collection is None:
            result.failures.append(
                f"{expect.domain}.{expect.collection}: not served "
                f"(keys: {sorted(found) or 'none'})"
            )
            continue
        definition = str((collection.get("count") or {}).get("definition", ""))
        notes = " ".join(collection.get("notes") or [])
        for check in expect.checks:
            if check == "unavailable" and not definition.startswith(UNAVAILABLE):
                result.failures.append(
                    f"{expect.domain}.{expect.collection}: expected an unavailable "
                    f"read, got {definition!r}"
                )
            elif check == "readable" and definition.startswith(UNAVAILABLE):
                result.failures.append(
                    f"{expect.domain}.{expect.collection}: went unavailable "
                    f"({definition!r}) but its store was fine"
                )
            elif check.startswith("mentions:"):
                needle = check.split(":", 1)[1]
                haystack = f"{definition} {notes}"
                if needle not in haystack:
                    result.failures.append(
                        f"{expect.domain}.{expect.collection}: nothing said {needle!r} "
                        f"(definition {definition!r}, notes {notes[:80]!r})"
                    )
    return result


def run_scenario(scenario: Scenario, workspace: Path, verbose: bool) -> Result:
    """Build one fixture, serve it, judge it, and stop the server."""
    root = build_root(workspace / scenario.name.replace(" ", "_"))
    scenario.mutate(root)
    vault = workspace / "empty-vault"
    vault.mkdir(exist_ok=True)

    last: Result | None = None
    for _attempt in range(3):
        port = free_port()
        server = serve(root, port, vault, workspace / f"portal-{port}.log")
        try:
            if not wait_ready(port):
                last = Result(scenario.name)
                last.failures.append(f"the portal never came up on {port}")
                continue
            last = judge(port, scenario)
            last.alive = server.poll() is None
            if not last.alive:
                last.failures.append("the server exited while serving")
            if verbose:
                routes = " ".join(f"{p}:{s}" for p, s in last.detail.items())
                print(f"    {routes}")
            return last
        finally:
            server.send_signal(signal.SIGINT)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
    return last or Result(scenario.name)


def main(argv: list[str] | None = None) -> int:
    """Run every scenario and report.

    Returns:
        ``0`` when every route answered and every expectation held, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        description="Serve deliberately broken Hermes fixtures and prove the portal "
        "degrades by collection rather than by page."
    )
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    parser.add_argument("--keep", action="store_true", help="leave fixtures on disk")
    parser.add_argument(
        "--verbose", action="store_true", help="print every route status"
    )
    args = parser.parse_args(argv)

    workspace = Path(tempfile.mkdtemp(prefix="hermes-portal-drift-"))
    results: list[Result] = []
    try:
        for scenario in SCENARIOS:
            if not args.json:
                print(f"  {scenario.name}: {scenario.breaks}")
            results.append(run_scenario(scenario, workspace, args.verbose))
    finally:
        if args.keep:
            print(f"\n  fixtures kept at {workspace}")
        else:
            shutil.rmtree(workspace, ignore_errors=True)

    failed = [r for r in results if r.failures or not r.alive]
    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failed,
                    "fixtures": len(results),
                    "failures": [
                        {"scenario": r.name, "problems": r.failures} for r in failed
                    ],
                    "routes": {r.name: r.detail for r in results},
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1 if failed else 0

    print()
    for result in results:
        mark = "ok  " if not result.failures and result.alive else "FAIL"
        print(f"  [{mark}] {result.name}")
        for problem in result.failures:
            print(f"         - {problem}")
    print()
    if failed:
        print(f"  {len(failed)} of {len(results)} fixtures broke a rule above.")
        return 1
    print(
        f"  {len(results)} fixtures: every route answered, and a broken store silenced "
        "only the collections that read it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
