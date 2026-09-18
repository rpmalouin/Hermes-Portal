"""HTTP surface for the portal: routes, JSON, and the read-only handler.

Routes::

    /                    portal index (one card per domain)
    /<domain>            domain page; ?box=, ?model=, ?provider= narrow it
    /<domain>/<id>       one record, plus the collections behind it
    /favorites           the starred records
    /search?q=...        cross-domain search
    /index.json          the index as JSON
    /<domain>.json       a domain's collections as JSON (filters apply)
    /<domain>/<id>.json  one record plus its sections
    /favorites.json      the starred records as JSON
    /search.json?q=...   search results as JSON
    /app.js              the portal's script (theme, palette, star toggles)

GET is the only method that reads anything, and every source is opened read-only.
The single POST is ``/favorites.json``, which stars or unstars one record, and it
writes exactly one file: the portal's own state document (:mod:`hermes.portal.state`),
outside every source the portal reads.  Nothing here opens ``state.db``, ``cron/``, a
skill tree or a vault note for writing, and ``--no-state`` turns even that off.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import render
from .domains import default_registry
from .model import Domain, DomainRegistry
from .sources import as_of, hermes_root
from .state import (
    PortalState,
    StateError,
    StateWriteError,
    default_state_path,
    favorite_from_payload,
)
from .taxonomy import coverage

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8087
# A filter must exist in three places to work on the wire: this whitelist, the domain
# that reads it, and the picker that offers it.  `folder` was missing here while the
# vault domain supported it, so `?folder=` was stripped before the domain saw it.
FILTER_KEYS = ("box", "folder", "kind", "model", "provider", "profile")
MAX_BODY_BYTES = 8192
JSON_TYPES = ("application/json", "")

#: Refreshes closer together than this are refused.  The effect is a re-read of every
#: source -- cheap, but not free, and no page needs it twice in a moment.
REFRESH_FLOOR_SECONDS = 2.0

#: Names that mean "this machine".  Binding to one of these does *not* mean only this
#: machine can reach the server: any name that resolves to 127.0.0.1 reaches it, which
#: is how a page the user visits can end up same-origin with the portal (DNS rebinding).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: Sent with every response.  The portal renders untrusted text -- vault notes, log
#: lines, session messages -- and uses its own inline script and styles, so the policy
#: allows exactly those and nothing else: no external loads, no framing, no referrers.
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    (
        "Content-Security-Policy",
        # 'self' as well as 'unsafe-inline'.  The page has an inline theme script and
        # also loads /app.js; 'unsafe-inline' alone does not cover an external
        # <script src>, which silently killed every control on the page except the
        # search form.  A policy that breaks the page is not a hardening.
        "default-src 'none'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src data:; connect-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    ),
)


def allowed_hosts(host: str) -> tuple[str, ...]:
    """The names this server answers to, or ``()`` when it is exposed on purpose.

    A loopback-bound portal answers to *any* name that resolves to loopback, so a page
    the user visits can re-resolve its own hostname to 127.0.0.1 and read the agent's
    memory, sessions and vault same-origin.  Checking ``Host`` against the names we
    were bound for closes that.  When the operator names any other interface they have
    already chosen to expose the portal: nothing is imposed, but the banner says so and
    requests are logged.
    """
    if host not in LOOPBACK_HOSTS:
        return ()
    return tuple(sorted({*LOOPBACK_HOSTS, host}))


def jsonable(value: Any) -> Any:
    """Convert portal dataclasses into JSON-serialisable values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def filters_from(query: str) -> dict[str, str]:
    """Extract the portal's supported filters from a query string."""
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    return {
        key: params[key][0].strip()
        for key in FILTER_KEYS
        if params.get(key) and params[key][0].strip()
    }


def index_payload(registry: DomainRegistry, built_at: str) -> dict[str, Any]:
    """The JSON index: every domain with its overview."""
    domains = registry.all()
    return {
        "built_at": built_at,
        "counts": {
            domain.key: registry.safe_overview(domain).count.value for domain in domains
        },
        "domains": [
            {
                "key": domain.key,
                "title": domain.title,
                "summary": domain.summary,
                "overview": jsonable(registry.safe_overview(domain)),
            }
            for domain in domains
        ],
    }


def domain_payload(
    registry: DomainRegistry, domain: Domain, filters: Mapping[str, str], built_at: str
) -> dict[str, Any]:
    """One domain's collections as JSON."""
    return {
        "built_at": built_at,
        "domain": domain.key,
        "filters": dict(filters),
        "collections": jsonable(registry.safe_collections(domain, filters)),
    }


def detail_payload(
    registry: DomainRegistry, domain: Domain, record_id: str, built_at: str
) -> dict[str, Any] | None:
    """One record plus its sections as JSON, or ``None`` when it is unknown."""
    record = registry.safe_detail(domain, record_id)
    if record is None:
        return None
    return {
        "built_at": built_at,
        "domain": domain.key,
        "record": jsonable(record),
        "sections": jsonable(registry.safe_sections(domain, record_id)),
    }


def search_payload(
    registry: DomainRegistry, query: str, limit: int, built_at: str
) -> dict[str, Any]:
    """Search results as JSON, with per-domain totals."""
    groups = registry.search(query, limit)
    return {
        "built_at": built_at,
        "query": query,
        "limit": limit,
        "order": [domain.key for domain in registry.all()],
        "counts": {domain.key: len(groups[domain.key]) for domain in registry.all()},
        "groups": jsonable(groups),
    }


def box_counts(registry: DomainRegistry) -> dict[str, int]:
    """Box name -> skill count, read from the skills domain's own boxes collection.

    The tiles are arranged from this and nothing else, so a tile's number cannot
    disagree with the skills page: there is one measurement, made by the domain.
    """
    if "skills" not in registry:
        return {}
    for collection in registry.safe_collections(registry.get("skills")):
        if collection.key != "boxes":
            continue
        counts: dict[str, int] = {}
        for record in collection.records:
            try:
                counts[record.title] = int(dict(record.fields).get("skills", "0"))
            except (TypeError, ValueError):
                continue
        return counts
    return {}


def describe(registry: DomainRegistry) -> str:
    """Printable summary of what the portal will serve."""
    lines = []
    for domain in registry.all():
        overview = registry.safe_overview(domain)
        lines.append(
            f"{overview.count.value:>6}  {domain.key:<9} [{overview.count.definition}]"
        )
        for extra in overview.extra_counts:
            lines.append(f"{'':>6}    +{extra.value:<6} {extra.definition}")
        for source in overview.sources:
            marker = "" if source.present else "  [MISSING]"
            lines.append(f"{'':>6}    src {source.label}: {source.location}{marker}")
        for note in overview.notes:
            lines.append(f"{'':>6}    note {note}")
    return "\n".join(lines)


class PortalHandler(BaseHTTPRequestHandler):
    """Serve the portal: HTML at the page routes, JSON at the ``.json`` routes."""

    registry: DomainRegistry | None = None
    state: PortalState | None = None
    built_at: str = ""
    render_limit: int = 20
    #: Names this server answers to; empty means no check (bound where the operator
    #: asked, so exposure was their call).  Set by :func:`serve`.
    hosts: tuple[str, ...] = ()
    #: When the last refresh was accepted, so two in a row can be refused politely.
    last_refresh: float = 0.0
    #: Quiet on loopback, where every request is the user's own; requests are logged
    #: when the portal was bound to a named interface, because then they are not.
    quiet: bool = True
    # no Python version in the Server header
    server_version = "hermes-portal"
    sys_version = ""

    def _host_ok(self) -> bool:
        """True when the request's ``Host`` names a name this portal was bound for.

        A request with no ``Host`` at all passes: browsers always send one, so an
        absent header means a non-browser client, which is not what rebinding fools.
        """
        if not self.hosts:
            return True
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return True
        name = raw
        if name.startswith("["):  # an IPv6 literal: [::1]:8087
            name = name.split("]", 1)[0].lstrip("[")
        elif ":" in name:
            name = name.rsplit(":", 1)[0]
        if name.lower() in self.hosts:
            return True
        self._send(
            421,
            "text/plain; charset=utf-8",
            (
                f"misdirected request: Host {raw!r} is not a name this portal answers "
                "to. Start it with --host <name> if it should.\n"
            ).encode(),
        )
        return False

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        """Route one GET request."""
        if not self._host_ok():
            return
        registry = self.registry
        if registry is None:
            self.send_error(500, "Portal registry not configured")
            return

        parsed = urllib.parse.urlsplit(self.path)
        segments = [part for part in parsed.path.split("/") if part]
        filters = filters_from(parsed.query)
        domains = registry.all()

        if not segments:
            snapshot = self.state.read() if self.state else None
            counts = box_counts(registry)
            self._send_html(
                render.render_index(
                    domains,
                    registry.overviews(),
                    self.built_at,
                    favorites=snapshot.favorites if snapshot else (),
                    tiles=render.render_tiles(coverage(counts)) if counts else "",
                )
            )
            return
        if segments == ["app.js"]:
            self._send(200, "text/javascript; charset=utf-8", render.APP_JS.encode())
            return
        if segments[0] in ("favorites", "favorites.json"):
            if segments[0].endswith(".json"):
                self._send_json(self._favorites_payload())
                return
            snapshot = self.state.read() if self.state else None
            note = ""
            if self.state is None:
                note = "the portal is running with --no-state: starring is disabled"
            elif snapshot is not None and snapshot.error:
                note = f"the state file could not be read: {snapshot.error}"
            self._send_html(
                render.render_favorites(
                    snapshot.favorites if snapshot else (),
                    domains,
                    self.built_at,
                    note,
                )
            )
            return
        if segments == ["index.json"]:
            self._send_json(index_payload(registry, self.built_at))
            return
        if segments[0] in ("search", "search.json"):
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
            if segments[0] == "search.json":
                self._send_json(
                    search_payload(registry, query, self.render_limit, self.built_at)
                )
                return
            groups = registry.search(query, self.render_limit) if query else {}
            totals = {
                domain.key: registry.safe_overview(domain).count.value
                for domain in domains
            }
            self._send_html(
                render.render_search(query, groups, totals, domains, self.built_at)
            )
            return

        domain_key = segments[0]
        wants_json = domain_key.endswith(".json")
        key = domain_key[: -len(".json")] if wants_json else domain_key
        if key not in registry:
            self._not_found(f"no domain called {key!r}")
            return
        domain = registry.get(key)

        if len(segments) == 1:
            if wants_json:
                self._send_json(
                    domain_payload(registry, domain, filters, self.built_at)
                )
                return
            collections = registry.safe_collections(domain, filters)
            self._send_html(
                render.render_domain(
                    domain, collections, domains, self.built_at, filters
                )
            )
            return

        record_id = urllib.parse.unquote(segments[1])
        wants_json = record_id.endswith(".json")
        if wants_json:
            record_id = record_id[: -len(".json")]
        record = registry.safe_detail(domain, record_id)
        if record is None:
            self._not_found(f"no record {record_id!r} in domain {key!r}")
            return
        if wants_json:
            self._send_json(detail_payload(registry, domain, record_id, self.built_at))
            return
        sections = registry.safe_sections(domain, record_id)
        self._send_html(
            render.render_detail(domain, record, sections, domains, self.built_at)
        )

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        """Send one response, with the headers every response carries."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for header, value in SECURITY_HEADERS:
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_text: str) -> None:
        """Send an HTML page."""
        self._send(200, "text/html; charset=utf-8", html_text.encode("utf-8"))

    def _send_json(self, payload: Any, status: int = 200) -> None:
        """Send a JSON document."""
        body = json.dumps(payload, indent=2, sort_keys=False) + "\n"
        self._send(status, "application/json; charset=utf-8", body.encode("utf-8"))

    def _favorites_payload(self) -> dict[str, Any]:
        """The favourites as JSON.  Reading never fails; problems ride in ``error``."""
        snapshot = self.state.read() if self.state else None
        favorites = snapshot.favorites if snapshot else ()
        return {
            "built_at": self.built_at,
            "writable": self.state is not None,
            "path": str(snapshot.path) if snapshot and snapshot.path else "",
            "error": snapshot.error if snapshot else "",
            "count": len(favorites),
            "favorites": [
                {
                    "domain": favorite.domain,
                    "id": favorite.id,
                    "title": favorite.title,
                    "added_at": favorite.added_at,
                }
                for favorite in favorites
            ],
        }

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        """Star or unstar one record: the project's only write.

        The validation order is deliberate -- route, then state, then media type,
        then size, then body, then the record itself -- so a bad request never
        reaches the filesystem and the response says which check failed.
        """
        if not self._host_ok():
            return
        registry = self.registry
        if registry is None:
            self._send_json(
                {"ok": False, "error": "portal registry not configured"}, 500
            )
            return

        parsed = urllib.parse.urlsplit(self.path)
        segments = [part for part in parsed.path.split("/") if part]
        key = segments[0] if segments else ""
        if key.endswith(".json"):
            key = key[: -len(".json")]
        if key == "refresh":
            self._refresh()
            return
        if key != "favorites":
            self._send_json(
                {"ok": False, "error": f"no POST route for {parsed.path!r}"}, 404
            )
            return
        if self.state is None:
            self._send_json(
                {"ok": False, "error": "favourites are disabled (--no-state)"}, 409
            )
            return

        content_type = (
            (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        )
        if content_type not in JSON_TYPES:
            self._send_json(
                {"ok": False, "error": f"send application/json, not {content_type!r}"},
                415,
            )
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json({"ok": False, "error": "empty body"}, 400)
            return
        if length > MAX_BODY_BYTES:
            self._send_json(
                {"ok": False, "error": f"body is larger than {MAX_BODY_BYTES} bytes"},
                413,
            )
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json({"ok": False, "error": f"body is not JSON: {exc}"}, 400)
            return
        try:
            domain_key, record_id, _title, add = favorite_from_payload(payload)
        except StateError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return

        domain_key = domain_key.strip()
        if domain_key not in registry:
            self._send_json(
                {"ok": False, "error": f"no domain called {domain_key!r}"}, 400
            )
            return

        domain = registry.get(domain_key)
        title = record_id
        if add is not False:
            # the title comes from the record, not the client, so the star file
            # cannot be used to store arbitrary text
            record = registry.safe_detail(domain, record_id)
            if record is None:
                self._send_json(
                    {
                        "ok": False,
                        "error": f"no record {record_id!r} in domain {domain_key!r}",
                    },
                    404,
                )
                return
            title = record.title

        try:
            snapshot = self.state.toggle(domain_key, record_id, title, add=add)
        except StateWriteError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        except StateError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return

        starred = f"{domain_key}|{record_id}" in snapshot.keys()  # noqa: SIM118
        self._send_json(
            {
                "ok": True,
                "starred": starred,
                "count": len(snapshot.favorites),
                "path": str(snapshot.path) if snapshot.path else "",
                "favorites": [
                    {"domain": favorite.domain, "id": favorite.id}
                    for favorite in snapshot.favorites
                ],
            }
        )

    def _refresh(self) -> None:
        """Drop every domain's cached snapshot: the explicit way to re-read.

        The cache is per process on purpose -- a page that silently re-read a 3 MB vault
        mid-request would be a surprise -- so the way to make the portal fresh is to say
        so, which is what the header's refresh button does.  Nothing is written:
        the only file this server ever touches is the favourites document.
        """
        registry = self.registry
        if registry is None:
            self._send_json(
                {"ok": False, "error": "portal registry not configured"}, 500
            )
            return

        now = time.monotonic()
        if now - PortalHandler.last_refresh < REFRESH_FLOOR_SECONDS:
            self._send_json(
                {"ok": False, "error": "refreshed a moment ago; give it a second"}, 429
            )
            return

        forgotten: list[str] = []
        for domain in registry.all():
            if domain.forget is None:
                continue
            domain.forget()
            forgotten.append(domain.key)
        PortalHandler.last_refresh = now
        self._send_json(
            {
                "ok": True,
                "forgotten": forgotten,
                "as_of": as_of(),
                "note": "nothing written; the next page reads every source again",
            }
        )

    def _not_found(self, what: str) -> None:
        """Send a 404, rendered like every other page."""
        registry = self.registry
        domains = registry.all() if registry else []
        self._send(
            404,
            "text/html; charset=utf-8",
            render.render_not_found(domains, self.built_at, what).encode("utf-8"),
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Quiet on loopback; logged when the portal was deliberately exposed.

        The stdlib calls this for every request, including errors.  Silence is right
        when the only caller is the user on the same machine; if the portal was bound
        to a named interface, an access log is the least it should leave behind.
        """
        if self.quiet:
            return
        super().log_message(format, *args)


def write_policy(state: PortalState | None) -> str:
    """One line saying exactly what a request may write, for the startup banner."""
    refresh = "POST /refresh.json writes nothing; it re-reads every source."
    if state is None or state.path is None:
        return f"Read-only: writing is switched off (--no-state). {refresh}"
    return (
        f"Read-only except favourites: POST /favorites.json writes {state.path}. "
        f"{refresh}"
    )


def serve(
    hermes_home: Path | None = None,
    profile: str | None = None,
    all_profiles: bool = True,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    registry: DomainRegistry | None = None,
    vault_root: Path | None = None,
    graph_db: Path | None = None,
    state: PortalState | None = None,
    state_path: Path | None = None,
    no_state: bool = False,
) -> int:
    """Build the registry (if needed) and serve the portal until interrupted.

    Args:
        hermes_home: Hermes home or profile directory; ``None`` resolves
            ``$HERMES_HOME`` then ``~/.hermes``.
        profile: Named profile to read skills from.
        all_profiles: Read every profile's skills too.
        host: Interface to bind; localhost only by default.
        port: TCP port (0 picks a free one).
        registry: Pre-built registry; one is built here when omitted, so a
            caller that already has one does not pay for a second walk.
        vault_root: Obsidian vault to index (default documented in the vault domain).
        graph_db: Code graph database; default ``.code-review-graph/graph.db``.
        state: Pre-built favourites store; built here when omitted.
        state_path: Where to keep favourites; default
            ``<hermes root>/portal/state.json``.
        no_state: Serve without a store, so the star buttons report 409 and nothing
            in the process can write anything.

    Returns:
        ``0`` on a clean shutdown.
    """
    built_at = as_of()
    if registry is None:
        registry = default_registry(
            hermes_home=hermes_home,
            profile=profile,
            all_profiles=all_profiles,
            vault_root=vault_root,
            graph_db=graph_db,
        )
    if state is None and not no_state:
        state = PortalState(state_path or default_state_path(hermes_root(hermes_home)))
    PortalHandler.registry = registry
    PortalHandler.state = state if not no_state else None
    PortalHandler.built_at = built_at
    PortalHandler.hosts = allowed_hosts(host)
    PortalHandler.quiet = bool(PortalHandler.hosts)

    server = ThreadingHTTPServer((host, port), PortalHandler)
    bound_host, bound_port = server.server_address[:2]
    print(describe(registry))
    print(
        f"Hermes Portal running at http://{bound_host}:{bound_port}  (built {built_at})"
    )
    print(write_policy(PortalHandler.state))
    if not PortalHandler.hosts:
        print(
            f"WARNING: bound to {host}, which is not loopback. This portal has no "
            "authentication: anyone who can reach this address can read the agent's "
            "memory, sessions, vault and logs. Requests will be logged."
        )
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the portal's argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m hermes.portal",
        description=(
            "Drill-down portal over everything Hermes keeps. Every source is opened "
            "read-only; favourites are the only write, kept in the portal's own file."
        ),
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=None,
        help="Hermes home or profile directory (default: $HERMES_HOME, else ~/.hermes)",
    )
    parser.add_argument(
        "--profile", default=None, help="Named profile to read skills from"
    )
    parser.add_argument(
        "--all-profiles",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="read every profile's skills too (default: yes)",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="favourites file (default: <hermes root>/portal/state.json)",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="do not keep favourites: the star buttons report that writing is off",
    )
    parser.add_argument(
        "--graph-db",
        default=None,
        help="code graph database (default: <hermes home>/.code-review-graph/graph.db)",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help=(
            "Obsidian vault to index (default: $HERMES_VAULT, else "
            "/Volumes/Data/MyObsidian)"
        ),
    )
    parser.add_argument(
        "--list", action="store_true", help="print the registry summary and exit"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="interface to bind")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to bind")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m hermes.portal``.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` on success, ``1`` when the registry cannot be built.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["doctor"]:
        # A subcommand rather than a second console script: `hermes-portal doctor`
        # is the name people reach for, and the import stays here so a long-running
        # server does not pay for the check's module at startup.
        from .doctor import main as doctor_main

        return doctor_main(argv[1:])
    args = build_parser().parse_args(argv)
    try:
        registry = default_registry(
            hermes_home=args.hermes_home,
            profile=args.profile,
            all_profiles=args.all_profiles,
            vault_root=args.vault,
            graph_db=args.graph_db,
        )
    except OSError as exc:
        print(f"error: cannot build the portal: {exc}", file=sys.stderr)
        return 1

    if args.list:
        print(describe(registry))
        return 0

    return serve(
        hermes_home=args.hermes_home,
        profile=args.profile,
        all_profiles=args.all_profiles,
        host=args.host,
        port=args.port,
        registry=registry,
        vault_root=args.vault,
        graph_db=args.graph_db,
        state_path=Path(args.state) if args.state else None,
        no_state=args.no_state,
    )
