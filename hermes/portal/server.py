"""HTTP surface for the portal: routes, JSON, and the read-only handler.

Routes::

    /                    portal index (one card per domain)
    /<domain>            domain page; ?box=, ?model=, ?provider= narrow it
    /<domain>/<id>       one record, plus the collections behind it
    /search?q=...        cross-domain search
    /index.json          the index as JSON
    /<domain>.json       a domain's collections as JSON (filters apply)
    /<domain>/<id>.json  one record plus its sections
    /search.json?q=...   search results as JSON

The handler answers GET only.  There is deliberately no POST: a client that tries
to change something gets the standard library's 501 rather than a surprise write,
and no code path here opens a writable handle on anything Hermes owns.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import render
from .domains import default_registry
from .model import Domain, DomainRegistry
from .sources import as_of

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8087
FILTER_KEYS = ("box", "model", "provider")


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
        "counts": {domain.key: len(groups[domain.key]) for domain in registry.all()},
        "groups": jsonable(groups),
    }


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
    built_at: str = ""
    render_limit: int = 20

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        """Route one GET request."""
        registry = self.registry
        if registry is None:
            self.send_error(500, "Portal registry not configured")
            return

        parsed = urllib.parse.urlsplit(self.path)
        segments = [part for part in parsed.path.split("/") if part]
        filters = filters_from(parsed.query)
        domains = registry.all()

        if not segments:
            self._send_html(
                render.render_index(domains, registry.overviews(), self.built_at)
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
        """Send one response."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_text: str) -> None:
        """Send an HTML page."""
        self._send(200, "text/html; charset=utf-8", html_text.encode("utf-8"))

    def _send_json(self, payload: Any) -> None:
        """Send a JSON document."""
        body = json.dumps(payload, indent=2, sort_keys=False) + "\n"
        self._send(200, "application/json; charset=utf-8", body.encode("utf-8"))

    def _not_found(self, what: str) -> None:
        """Send a 404, rendered like every other page."""
        registry = self.registry
        domains = registry.all() if registry else []
        self._send(
            404,
            "text/html; charset=utf-8",
            render.render_not_found(domains, self.built_at, what).encode("utf-8"),
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ARG002
        """Quiet default request logging; comment out to re-enable."""
        return


def serve(
    hermes_home: Path | None = None,
    profile: str | None = None,
    all_profiles: bool = True,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    registry: DomainRegistry | None = None,
    vault_root: Path | None = None,
    graph_db: Path | None = None,
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
    PortalHandler.registry = registry
    PortalHandler.built_at = built_at

    server = ThreadingHTTPServer((host, port), PortalHandler)
    bound_host, bound_port = server.server_address[:2]
    print(describe(registry))
    print(
        f"Hermes Portal running at http://{bound_host}:{bound_port}  (built {built_at})"
    )
    print("Read-only. Press Ctrl+C to stop.")
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
        description="Read-only drill-down portal over everything Hermes keeps.",
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
        "--graph-db",
        default=None,
        help="code graph database (default: <hermes home>/.code-review-graph/graph.db)",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="Obsidian vault to index (default: /Volumes/Data/MyObsidian)",
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
    )
