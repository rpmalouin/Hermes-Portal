"""Serve the demo home: patch the real-machine readers, then start the portal.

    python3 tools/demo_shot/shim.py [--out /tmp/portal-demo] [--port 8089]

Run this from the repo root, in a terminal of its own.  Nothing about *this* machine
may reach the page, so the readers that would fall back to a real path are pointed at
the fixture instead.
"""

import argparse
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--out",
    default="/tmp/portal-demo",
    type=Path,
    help="the fixture the builder wrote (default: /tmp/portal-demo)",
)
parser.add_argument(
    "--port", default=8089, type=int, help="the port to serve it on (default: 8089)"
)
ARGS = parser.parse_args()
DEMO = ARGS.out.expanduser()
ROOT = Path(__file__).resolve().parents[2]

# $HERMES_HOME is what several readers fall back to when a path is not passed, and in a
# real session it points at the running profile.  Point it at the demo, or the screenshot
# would carry this machine's profile names.
os.environ["HERMES_HOME"] = str(DEMO / "home")
sys.path.insert(0, str(ROOT))

from hermes.portal.domains import health, logs  # noqa: E402

logs.LIBRARY_LOGS = DEMO / "home" / "library-logs"
health.LAUNCH_AGENTS = DEMO / "home" / "demo-agents"


def fake_run(argv, timeout=15.0):  # noqa: ANN001, ARG001
    """Fixed output for the two probes, so no real service or port is described."""
    if argv and argv[0] == "launchctl":
        return (
            "PID\tStatus\tLabel\n"
            "41822\t0\tai.demo.gateway\n"
            "41960\t0\tai.demo.ticker\n"
            "-\t0\tai.demo.retired\n",
            "",
        )
    if argv and argv[0] == "lsof":
        return (
            "COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
            "python  41822 demo    7u  IPv4  0x01      0t0  TCP 127.0.0.1:8087 (LISTEN)\n"
            "python  41960 demo    9u  IPv4  0x02      0t0  TCP 127.0.0.1:9119 (LISTEN)\n",
            "",
        )
    return "", "not available in the demo"


health.run_argv = fake_run
logs.run_argv = fake_run

from hermes.portal.server import main  # noqa: E402

raise SystemExit(
    main(
        [
            "--hermes-home",
            str(DEMO / "home"),
            "--vault",
            str(DEMO / "vault"),
            "--graph-db",
            str(DEMO / "home" / ".code-review-graph" / "graph.db"),
            "--state",
            str(DEMO / "home" / "portal" / "state.json"),
            "--no-all-profiles",
            "--port",
            str(ARGS.port),
        ]
    )
)
