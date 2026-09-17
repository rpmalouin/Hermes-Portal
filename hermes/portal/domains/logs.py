"""Logs domain: what the logs are saying, without reading them whole.

Sources, named with their windows on the page:

* ``$HERMES_HOME/logs/*.log*`` -- agent, gateway, desktop and rotated files.
* ``~/Library/Logs/{hermes,backup,action-backup}*.log`` -- the dashboard service and
  the backup jobs, which log outside the Hermes home.

Everything is read from the **end** of each file (200 KB by default): a 5 MB
gateway log must never be pulled into memory to show today's failure.  Error lines
are normalised into signatures (timestamp, level, ids and numbers stripped) and
counted, which is what turns "834 error lines" into "one recurring MCP failure".

**Every line that reaches a page is scrubbed first** (see
:func:`hermes.portal.sources.scrub`): logs are exactly where a pasted key or a
bearer header ends up.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..model import Collection, Count, Domain, Record, Source, build_collection
from ..sources import (
    as_of,
    fmt_ago,
    glob_files,
    hermes_home,
    human_size,
    path_source,
    scrub,
    snippet,
    tail_text,
    truncate,
)

WINDOW_BYTES = 200_000
FILES_CAP = 40
SIGNATURES_CAP = 30
TAIL_LINES = 40
ERROR_LINES_CAP = 50
HERMES_LOG_PATTERNS = ("*.log", "*.log.*")
LIBRARY_LOG_PATTERNS = ("hermes*.log", "backup*.log", "action-backup*.log")
LIBRARY_LOGS = Path.home() / "Library" / "Logs"

ERRORISH = re.compile(
    r"(?i)\b(error|traceback|exception|failed|failure|fatal|refused)\b"
)
_TIMESTAMP = re.compile(
    r"^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?\]?\s*"
)
_LEVEL = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL|NOTICE)\b")
_NOISE_WORDS = re.compile(r"0x[0-9a-fA-F]+|\b[0-9a-f]{8,}\b|\b\d+\.\d+\b|\b\d+\b")


def log_roots(home: Path | None = None) -> list[tuple[str, Path, tuple[str, ...]]]:
    """Return ``(label, root, patterns)`` for every place logs live.

    Args:
        home: Hermes home or profile directory; defaults to ``$HERMES_HOME`` then
            ``~/.hermes``.  Tests pass a temporary tree here.
    """
    return [
        ("hermes logs", (home or hermes_home()) / "logs", HERMES_LOG_PATTERNS),
        ("library logs", LIBRARY_LOGS, LIBRARY_LOG_PATTERNS),
    ]


def log_files(home: Path | None = None) -> list[Path]:
    """Every log file in scope, biggest first."""
    files: list[Path] = []
    for _label, root, patterns in log_roots(home):
        if root.is_dir():
            files.extend(glob_files(root, patterns))
    unique = {path.resolve(): path for path in files}
    return sorted(unique.values(), key=lambda path: -path.stat().st_size)


def signature(line: str) -> str:
    """Normalise a log line into a stable signature.

    Timestamps, log levels, hex ids and numbers differ between two occurrences of
    the same failure, so they are the parts that are removed; what is left is what
    a reader can act on.
    """
    text = _TIMESTAMP.sub("", line.strip())
    text = _LEVEL.sub("", text)
    text = _NOISE_WORDS.sub("N", text)
    return truncate(text.strip(" -:"), 150)


def scan(path: Path) -> dict[str, Any]:
    """Read the tail of one log file and summarise it."""
    text, truncated, error = tail_text(path, WINDOW_BYTES)
    lines = text.splitlines()
    error_lines = [line for line in lines if ERRORISH.search(line)]
    counts: dict[str, int] = {}
    examples: dict[str, str] = {}
    for line in error_lines:
        # scrub first: the signature becomes a record *title*, so a credential in
        # the raw line would be rendered in the list even with bodies masked
        clean = scrub(line)
        key = signature(clean)
        counts[key] = counts.get(key, 0) + 1
        examples.setdefault(key, clean.strip())
    return {
        "path": path,
        "lines": lines,
        "error_lines": error_lines,
        "counts": counts,
        "examples": examples,
        "truncated": truncated,
        "error": error,
        "size": path.stat().st_size if path.exists() else 0,
        "modified": path.stat().st_mtime if path.exists() else None,
    }


def build_domain(hermes_home_override: Path | None = None) -> Domain:
    """Build the logs domain.

    Args:
        hermes_home_override: Hermes home or profile directory; the default
            resolves ``$HERMES_HOME`` then ``~/.hermes``.

    Returns:
        A :class:`~hermes.portal.model.Domain`.  Files are read (tails only) per
        collection, and every rendered line is scrubbed.
    """

    def _sources() -> tuple[Source, ...]:
        return tuple(
            path_source(label, root, note=", ".join(patterns))
            for label, root, patterns in log_roots(hermes_home_override)
        )

    def _scans() -> list[dict[str, Any]]:
        return [scan(path) for path in log_files(hermes_home_override)]

    def _files_collection(scans: list[dict[str, Any]]) -> Collection:
        """One record per log file."""
        return build_collection(
            "files",
            "Log files",
            "Each file, with its size and how noisy the tail window is.",
            "log files in scope, by size",
            [
                Record(
                    id=str(entry["path"].name),
                    title=entry["path"].name,
                    subtitle=f"{human_size(entry['size'])} · "
                    f"{len(entry['lines'])} lines in the tail window · "
                    f"{len(entry['error_lines'])} look like errors",
                    badges=(
                        human_size(entry["size"]),
                        f"{len(entry['error_lines'])} error-ish",
                        "rotated" if entry["path"].name.count(".") > 1 else "current",
                    ),
                    fields=(
                        ("path", str(entry["path"])),
                        ("size", human_size(entry["size"])),
                        ("modified", fmt_ago(entry["modified"])),
                        ("lines in window", str(len(entry["lines"]))),
                        ("error-ish lines", str(len(entry["error_lines"]))),
                        (
                            "window",
                            f"last {human_size(WINDOW_BYTES)}"
                            + (" (file is longer)" if entry["truncated"] else ""),
                        ),
                        ("read error", entry["error"] or "\u2014"),
                    ),
                )
                for entry in scans
            ],
            cap=FILES_CAP,
            sources=_sources(),
            notes=(
                f"only the last {human_size(WINDOW_BYTES)} of each file is read, so "
                "counts describe the window and not the whole history",
            ),
            as_of=as_of(),
        )

    def _signatures_collection(scans: list[dict[str, Any]]) -> Collection:
        """Recurring error signatures across every file, most frequent first."""
        totals: dict[str, int] = {}
        examples: dict[str, str] = {}
        files: dict[str, set[str]] = {}
        for entry in scans:
            for key, count in entry["counts"].items():
                totals[key] = totals.get(key, 0) + count
                files.setdefault(key, set()).add(entry["path"].name)
                examples.setdefault(key, entry["examples"][key])
        ordered = sorted(totals.items(), key=lambda item: -item[1])
        return build_collection(
            "signatures",
            "Recurring errors",
            "Error lines grouped by what they say, with timestamps and ids removed.",
            "distinct normalised error signatures in the tail windows",
            [
                Record(
                    id=f"sig-{index}",
                    title=snippet(key, 120),
                    subtitle=f"{count} occurrence(s) across {len(files[key])} file(s)",
                    badges=(f"x{count}", f"{len(files[key])} file(s)"),
                    body=examples[key],
                    fields=(
                        ("signature", key),
                        ("occurrences in window", str(count)),
                        ("files", ", ".join(sorted(files[key]))),
                        ("example", examples[key]),
                    ),
                )
                for index, (key, count) in enumerate(ordered[:SIGNATURES_CAP])
            ],
            sources=_sources(),
            extra_counts=(
                Count(len(ordered), "distinct signatures"),
                Count(sum(totals.values()), "error-ish lines in the windows"),
            ),
            notes=(
                "signatures are a heuristic: a number or id that changes between two "
                "occurrences is stripped, so two genuinely different errors can merge",
            ),
            as_of=as_of(),
        )

    def overview() -> Collection:
        """Headline: how many files, how noisy, and the worst offender."""
        scans = _scans()
        files = _files_collection(scans)
        signatures = _signatures_collection(scans)
        top = signatures.records[0] if signatures.records else None
        return build_collection(
            "overview",
            "Logs",
            "What the logs are saying, read from the end and grouped by failure.",
            "log files in scope (read from the end)",
            files.records,
            cap=5,
            sources=_sources(),
            extra_counts=(
                Count(len(scans), "log files scanned"),
                Count(
                    sum(len(entry["error_lines"]) for entry in scans),
                    f"error-ish lines in the last {human_size(WINDOW_BYTES)} "
                    "of each file",
                ),
                Count(len(signatures.records), "distinct signatures"),
            ),
            metrics=(
                ("Files", str(len(scans))),
                (
                    "Total size",
                    human_size(sum(entry["size"] for entry in scans)),
                ),
                ("Most frequent", (top.title if top else "\u2014")),
            ),
            notes=tuple(entry["error"] for entry in scans if entry["error"]),
            as_of=as_of(),
        )

    def collections(_filters: Mapping[str, str] | None = None) -> Sequence[Collection]:
        """Drill-down collections for logs."""
        scans = _scans()
        return [_files_collection(scans), _signatures_collection(scans)]

    def detail(record_id: str) -> Record | None:
        """One log file: its facts plus the tail of the file itself."""
        for path in log_files(hermes_home_override):
            if path.name != record_id:
                continue
            _text, truncated, error = tail_text(path, WINDOW_BYTES)
            lines = _text.splitlines()[-TAIL_LINES:]
            return Record(
                id=path.name,
                title=path.name,
                subtitle=str(path),
                badges=(human_size(path.stat().st_size),),
                fields=(
                    ("path", str(path)),
                    ("size", human_size(path.stat().st_size)),
                    ("modified", fmt_ago(path.stat().st_mtime)),
                    ("tail lines shown", str(len(lines))),
                    (
                        "tail window",
                        f"last {human_size(WINDOW_BYTES)}"
                        + (" (file is longer)" if truncated else ""),
                    ),
                    ("read error", error or "\u2014"),
                ),
                body="\n".join(scrub(line) for line in lines),
                links=(("/logs", "All logs"),),
            )
        return None

    def detail_sections(record_id: str) -> Sequence[Collection]:
        """Behind one log file: the error lines in its window, scrubbed."""
        for path in log_files(hermes_home_override):
            if path.name != record_id:
                continue
            entry = scan(path)
            return [
                build_collection(
                    "errors",
                    "Error-ish lines in this file",
                    "The lines that matched the error heuristic, newest last.",
                    "error-ish lines in this file's tail window",
                    [
                        Record(
                            id=f"line-{index}",
                            title=snippet(scrub(line.strip()), 140),
                            badges=(f"line {index + 1}",),
                            body=scrub(line),
                        )
                        for index, line in enumerate(
                            entry["error_lines"][-ERROR_LINES_CAP:]
                        )
                    ],
                    sources=(path_source("log file", path),),
                    notes=(
                        "matched by keyword, so informational lines that mention an "
                        "error word are included too",
                    ),
                    as_of=as_of(),
                )
            ]
        return []

    def search(needle: str, limit: int) -> Sequence[Record]:
        """Grep the tail windows of every log file."""
        term = needle.strip()
        if not term:
            return []
        lowered = term.lower()
        hits: list[Record] = []
        for entry in _scans():
            for index, line in enumerate(entry["lines"]):
                if lowered in line.lower():
                    hits.append(
                        Record(
                            id=f"{entry['path'].name}-{index}",
                            title=snippet(scrub(line.strip()), 140) or "(blank)",
                            subtitle=str(entry["path"].name),
                            badges=("log", entry["path"].name),
                            body=scrub(line),
                            links=((f"/logs/{entry['path'].name}", "Open file"),),
                        )
                    )
                    break
            if len(hits) >= limit:
                break
        return hits

    return Domain(
        key="logs",
        title="Logs",
        summary="Log tails and grouped error signatures, scrubbed of credentials.",
        overview=overview,
        collections=collections,
        detail=detail,
        search=search,
        detail_sections=detail_sections,
    )
