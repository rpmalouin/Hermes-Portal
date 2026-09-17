"""HTML rendering for the portal: one generic page shape, four views.

Every view renders the same vocabulary -- collections of records, each with its
count, the definition behind that count and the sources it read -- so a new
domain gets a complete page without touching this file.

Two rules:

* Nothing is trusted: every interpolated value is escaped before it reaches the
  template, including record bodies and the query string.
* Record bodies render inside ``<details>``, so a 50-message section cannot
  dominate a page while still being reachable in one click.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from string import Template

from .model import Collection, Domain, Picker, Record

BODY_PREVIEW = 400
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

PAGE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
    :root { color-scheme: dark; }
    body {
        background: #16162a; color: #e9eaf0; margin: 0; padding: 1.75rem 2rem 4rem;
        font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
        line-height: 1.5;
    }
    a { color: #7fb2ff; text-decoration: none; }
    a:hover { text-decoration: underline; }
    header.top {
        align-items: center; border-bottom: 1px solid #272a45; display: flex;
        flex-wrap: wrap; gap: 1rem; margin: 0 0 1.5rem; padding: 0 0 1rem;
    }
    header.top .brand { font-size: 1.15rem; font-weight: 700; letter-spacing: 0.01em; }
    nav.domains { display: flex; flex-wrap: wrap; gap: 0.85rem; }
    nav.domains a {
        background: #1e2140; border: 1px solid #2c3055; border-radius: 999px;
        font-size: 0.85rem; padding: 0.25rem 0.75rem;
    }
    nav.domains a.here { background: #2b3a63; border-color: #4a6bb0; color: #dce7ff; }
    form.search { display: flex; gap: 0.5rem; margin-left: auto; }
    form.search input {
        background: #1e2140; border: 1px solid #2c3055; border-radius: 8px;
        color: #e9eaf0; min-width: 14rem; padding: 0.4rem 0.6rem;
    }
    form.search button {
        background: #2b3a63; border: 1px solid #4a6bb0; border-radius: 8px;
        color: #dce7ff; padding: 0.4rem 0.8rem;
    }
    h1 { font-size: 1.9rem; margin: 0 0 0.35rem; }
    h2 { font-size: 1.25rem; margin: 2rem 0 0.5rem; }
    .crumbs { color: #8d92ad; font-size: 0.85rem; margin: 0 0 0.6rem; }
    .lede { color: #b6bad0; margin: 0 0 1.25rem; max-width: 70ch; }
    .panel {
        background: #1b1d33; border: 1px solid #272a45; border-radius: 12px;
        margin: 0 0 1.1rem; padding: 1.1rem 1.25rem;
    }
    .grid {
        display: grid; gap: 1.1rem;
        grid-template-columns: repeat(auto-fill, minmax(21rem, 1fr));
    }
    .counts {
        align-items: baseline; display: flex; flex-wrap: wrap; gap: 0.6rem;
        margin: 0 0 0.6rem;
    }
    .counts .headline { font-size: 1.9rem; font-weight: 700; }
    .counts .def { color: #8d92ad; font-size: 0.82rem; }
    .pill {
        background: #23264a; border: 1px solid #2f3363; border-radius: 999px;
        color: #c3c8e0; font-size: 0.75rem; padding: 0.15rem 0.6rem;
    }
    .row {
        border-top: 1px solid #23264a; display: flex; flex-wrap: wrap;
        gap: 0.5rem 0.9rem; padding: 0.6rem 0;
    }
    .row:first-of-type { border-top: none; }
    .row .title { font-weight: 600; }
    .row .sub { color: #a9aec6; flex: 1 1 22rem; font-size: 0.88rem; }
    .badges { display: flex; flex-wrap: wrap; gap: 0.35rem; }
    .badge {
        background: #1f3a5f; border-radius: 6px; color: #bcd6ff;
        font-size: 0.72rem; padding: 0.1rem 0.45rem;
    }
    .meta { color: #7d829c; font-size: 0.78rem; }
    .notes {
        color: #e7c07b; font-size: 0.82rem; margin: 0.5rem 0 0;
        padding-left: 1.1rem;
    }
    .sources { color: #7d829c; font-size: 0.78rem; margin: 0.55rem 0 0; }
    table.fields { border-collapse: collapse; margin: 0.4rem 0 0; width: 100%; }
    table.fields th, table.fields td {
        border-top: 1px solid #23264a; font-size: 0.86rem;
        padding: 0.35rem 0.6rem 0.35rem 0; text-align: left;
        vertical-align: top;
    }
    table.fields th {
        color: #8d92ad; font-weight: 500; white-space: nowrap; width: 12rem;
    }
    pre.body {
        background: #14152a; border: 1px solid #272a45; border-radius: 8px;
        color: #cfd3e6; font-size: 0.8rem; margin: 0.5rem 0 0; max-height: 26rem;
        overflow: auto; padding: 0.75rem; white-space: pre-wrap; word-break: break-word;
    }
    details.body summary { color: #8d92ad; cursor: pointer; font-size: 0.82rem; }
    .empty { color: #8d92ad; font-style: italic; }
    footer { color: #6b7089; font-size: 0.78rem; margin-top: 2.5rem; }
    form.picker {
        align-items: center; display: flex; flex-wrap: wrap; gap: 0.6rem;
        margin: 0.4rem 0 1rem;
    }
    form.picker label { color: #8d92ad; font-size: 0.85rem; }
    form.picker select {
        background: #1e2140; border: 1px solid #2c3055; border-radius: 8px;
        color: #e9eaf0; font-size: 0.9rem; min-width: 18rem;
        padding: 0.4rem 0.5rem;
    }
    form.picker button {
        background: #2b3a63; border: 1px solid #4a6bb0; border-radius: 8px;
        color: #dce7ff; padding: 0.4rem 0.7rem;
    }
    .grid .card {
        background: #1e2140; border: 1px solid #2c3055; border-radius: 12px;
        padding: 1rem 1.1rem; transition: transform 0.15s ease;
    }
    .grid .card:hover { transform: translateY(-2px); border-color: #4a6bb0; }
    .grid .card .box {
        color: #8d92ad; font-size: 0.7rem; letter-spacing: 0.05em;
        text-transform: uppercase;
    }
    .grid .card h3 { font-size: 1.05rem; margin: 0.35rem 0 0.45rem; }
    .grid .card .desc { color: #b6bad0; font-size: 0.85rem; line-height: 1.45; }
    .grid .card .desc code {
        background: #23264a; border-radius: 4px; padding: 0 0.25rem;
    }
    .grid .card .src {
        color: #6b7089; font-family: ui-monospace, monospace; font-size: 0.68rem;
        margin-top: 0.7rem; word-break: break-all;
    }
</style>
</head>
<body>
$nav
$body
<footer>
    Read-only: the portal opens every source in read-only mode and never writes.
    Counts carry their definitions; sources and read time are shown per collection.
    Built $built_at.
</footer>
</body>
</html>
"""
)


def rich(text: str) -> str:
    """Escape *text*, then apply a minimal markdown pass (code, bold)."""
    escaped = html.escape(" ".join(str(text).split()), quote=True)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    return _BOLD_RE.sub(r"<strong>\1</strong>", escaped)


def source_state(present: bool) -> str:
    """Render whether a source was found, without nesting quotes in an f-string."""
    if present:
        return '<span title="present">ok</span>'
    return '<span class="warn">MISSING</span>'


def _nav(domains: Sequence[Domain], current: str = "", query: str = "") -> str:
    """Render the header: brand, domain links and the search form."""
    links = []
    for domain in domains:
        key = html.escape(domain.key, quote=True)
        here = ' class="here"' if domain.key == current else ""
        links.append(f'<a href="/{key}"{here}>{html.escape(domain.title)}</a>')
    joined = "".join(links)
    placeholder = "Search every domain\u2026"
    return (
        '<header class="top">\n'
        '  <div class="brand"><a href="/">Hermes Portal</a></div>\n'
        f'  <nav class="domains">{joined}</nav>\n'
        '  <form class="search" method="get" action="/search">\n'
        f'    <input name="q" value="{html.escape(query, quote=True)}"'
        f' placeholder="{placeholder}">\n'
        '    <button type="submit">Search</button>\n'
        "  </form>\n"
        "</header>"
    )


def _page(
    title: str,
    body: str,
    domains: Sequence[Domain],
    built_at: str,
    current: str = "",
    query: str = "",
) -> str:
    """Wrap *body* in the shell."""
    return PAGE.substitute(
        title=html.escape(title),
        nav=_nav(domains, current, query),
        body=body,
        built_at=html.escape(built_at),
    )


def _badges(record: Record) -> str:
    """Render a record's badges."""
    if not record.badges:
        return ""
    pills = "".join(
        f'<span class="badge">{html.escape(str(badge))}</span>'
        for badge in record.badges
        if str(badge).strip()
    )
    return f'<div class="badges">{pills}</div>'


def _record_row(record: Record, domain_key: str, with_body: bool = False) -> str:
    """Render one record as a list row.

    The title links to the record's ``href`` when it declares one (a box points at
    its filtered view), otherwise to its detail page.
    """
    title = html.escape(record.title)
    if not with_body:
        target = record.href or (
            f"/{html.escape(domain_key)}/{html.escape(record.id, quote=True)}"
        )
        title = f'<a href="{html.escape(target, quote=True)}">{title}</a>'
    body = ""
    if with_body and record.body:
        preview = html.escape(record.body[:BODY_PREVIEW])
        body = (
            '<details class="body"><summary>body '
            f"({len(record.body):,} chars)</summary>"
            f'<pre class="body">{preview}'
            + ("\u2026" if len(record.body) > BODY_PREVIEW else "")
            + "</pre></details>"
        )
    sub = f'<div class="sub">{rich(record.subtitle)}</div>' if record.subtitle else ""
    meta = ""
    if record.fields:
        bits = " · ".join(
            f"{html.escape(str(key))}: {html.escape(str(value))}"
            for key, value in record.fields[:4]
            if str(value).strip() and str(value) != "\u2014"
        )
        if bits:
            meta = f'<div class="meta">{bits}</div>'
    return (
        '<div class="row">'
        f'<div><div class="title">{title}</div>{meta}</div>'
        f"{sub}{_badges(record)}{body}"
        "</div>"
    )


def render_card(record: Record, domain_key: str) -> str:
    """Render one record as a gallery card (the deck's card, driven by a Record)."""
    target = record.href or (
        f"/{html.escape(domain_key)}/{html.escape(record.id, quote=True)}"
    )
    box_line = " / ".join(
        html.escape(str(badge)) for badge in record.badges if str(badge).strip()
    )
    desc = rich(record.subtitle) if record.subtitle else ""
    path = next(
        (
            str(value)
            for key, value in record.fields
            if str(key) in ("path", "file") and str(value).strip()
        ),
        "",
    )
    meta = f'<div class="src">{html.escape(path)}</div>' if path else ""
    return (
        '<div class="card">'
        f'<div class="box">{box_line}</div>'
        f'<h3><a href="{html.escape(target, quote=True)}">'
        f"{html.escape(record.title)}</a></h3>"
        f'<div class="desc">{desc}</div>'
        f"{meta}</div>"
    )


def render_picker(picker: Picker, domain_key: str) -> str:
    """Render a collection's dropdown as a plain GET form."""
    options = [
        '<option value=""{}>{}</option>'.format(
            "" if picker.selected else " selected", html.escape(picker.all_label)
        )
    ]
    offered = {value for value, _label in picker.options}
    if picker.selected and picker.selected not in offered:
        # a filter the dropdown cannot offer (a category path, or a value that no
        # longer exists) still has to be visible, or the page lies about its state
        selected_value = html.escape(picker.selected, quote=True)
        options.append(
            f'<option value="{selected_value}" selected disabled>'
            f"{html.escape(picker.selected)} (current filter)</option>"
        )
    for value, label in picker.options:
        selected = " selected" if value == picker.selected else ""
        options.append(
            f'<option value="{html.escape(value, quote=True)}"{selected}>'
            f"{html.escape(label)}</option>"
        )
    key = html.escape(picker.query_key, quote=True)
    return (
        '<form class="picker" method="get" action="/'
        f'{html.escape(domain_key, quote=True)}">'
        f'<label for="{key}">{html.escape(picker.label)}</label>'
        f'<select id="{key}" name="{key}" onchange="this.form.submit()">'
        + "".join(options)
        + "</select>"
        '<noscript><button type="submit">Apply</button></noscript>'
        "</form>"
    )


def render_collection(collection: Collection, domain_key: str) -> str:
    """Render one collection: counts, provenance, notes and its records."""
    extras = "".join(
        f'<span class="pill">{extra.value:,} {html.escape(extra.definition)}</span>'
        for extra in collection.extra_counts
    )
    shown = (
        f'<span class="def">showing {collection.shown:,} of '
        f"{collection.count.value:,}</span>"
        if collection.truncated
        else ""
    )
    notes = "".join(f"<li>{html.escape(str(note))}</li>" for note in collection.notes)
    notes_block = f'<ul class="notes">{notes}</ul>' if notes else ""
    sources = " · ".join(
        f"{html.escape(source.label)} {source_state(source.present)} "
        f'<span class="meta">{html.escape(source.location)}</span>'
        for source in collection.sources
    )
    sources_block = (
        f'<div class="sources">read from: {sources}</div>' if sources else ""
    )
    if collection.display == "cards":
        cards = "".join(
            render_card(record, domain_key) for record in collection.records
        )
        rows = f'<div class="grid">{cards}</div>' if cards else ""
    else:
        rows = "".join(_record_row(record, domain_key) for record in collection.records)
    if not rows:
        rows = '<p class="empty">no records</p>'
    picker = render_picker(collection.picker, domain_key) if collection.picker else ""
    as_of = (
        f'<span class="meta">as of {html.escape(collection.as_of)}</span>'
        if collection.as_of
        else ""
    )
    return (
        '<section class="panel">'
        f'<h2 id="{html.escape(collection.key, quote=True)}">'
        f"{html.escape(collection.title)}</h2>"
        f'<p class="lede">{html.escape(collection.description)}</p>'
        '<div class="counts">'
        f'<span class="headline">{collection.count.value:,}</span>'
        f'<span class="def">{html.escape(collection.count.definition)}</span>'
        f"{shown}{extras}{as_of}</div>"
        f"{notes_block}{picker}{rows}{sources_block}"
        "</section>"
    )


def render_index(
    domains: Sequence[Domain],
    overviews: Sequence[Collection],
    built_at: str,
) -> str:
    """Render the portal index: one card per domain."""
    cards = []
    for domain, overview in zip(domains, overviews, strict=False):
        extras = "".join(
            f'<span class="pill">{extra.value:,} {html.escape(extra.definition)}</span>'
            for extra in overview.extra_counts
        )
        samples = "".join(
            _record_row(record, domain.key) for record in overview.records[:3]
        )
        sources_ok = sum(1 for source in overview.sources if source.present)
        notes = "".join(f"<li>{html.escape(str(note))}</li>" for note in overview.notes)
        cards.append(
            '<section class="panel">'
            f'<h2><a href="/{html.escape(domain.key)}">'
            f"{html.escape(domain.title)}</a></h2>"
            f'<p class="lede">{html.escape(domain.summary)}</p>'
            '<div class="counts">'
            f'<span class="headline">{overview.count.value:,}</span>'
            f'<span class="def">{html.escape(overview.count.definition)}</span>'
            f"{extras}</div>"
            f'<div class="meta">{sources_ok}/{len(overview.sources)} source(s) present'
            f" · as of {html.escape(overview.as_of)}</div>"
            f"{samples}"
            + (f'<ul class="notes">{notes}</ul>' if notes else "")
            + "</section>"
        )
    body = (
        "<h1>Hermes Portal</h1>"
        '<p class="lede">A read-only, drill-down view over everything Hermes keeps: '
        "skills, sessions and cron today. Every count is labelled with the rule that "
        "produced it, and every collection names the sources it read.</p>"
        f'<div class="grid">{"".join(cards)}</div>'
    )
    return _page("Hermes Portal", body, domains, built_at)


def render_domain(
    domain: Domain,
    collections: Sequence[Collection],
    domains: Sequence[Domain],
    built_at: str,
    filters: Mapping[str, str] | None = None,
) -> str:
    """Render a domain page: every collection it publishes."""
    active = {key: value for key, value in (filters or {}).items() if value}
    filter_line = ""
    if active:
        parts = ", ".join(
            f"{html.escape(k)}={html.escape(v)}" for k, v in active.items()
        )
        filter_line = (
            f'<p class="lede">filtered by {parts} · '
            f'<a href="/{html.escape(domain.key)}">clear</a></p>'
        )
    blocks = "".join(
        render_collection(collection, domain.key) for collection in collections
    )
    body = (
        f'<div class="crumbs"><a href="/">Hermes Portal</a> / '
        f"{html.escape(domain.title)}</div>"
        f"<h1>{html.escape(domain.title)}</h1>"
        f'<p class="lede">{html.escape(domain.summary)}</p>'
        f"{filter_line}{blocks}"
    )
    return _page(domain.title, body, domains, built_at, domain.key)


def render_detail(
    domain: Domain,
    record: Record,
    sections: Sequence[Collection],
    domains: Sequence[Domain],
    built_at: str,
) -> str:
    """Render one record, plus the collections behind it."""
    fields = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in record.fields
    )
    table = f'<table class="fields">{fields}</table>' if fields else ""
    links = ""
    if record.links:
        anchors = " ".join(
            f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'
            for label, href in record.links
        )
        links = f'<p class="lede">{anchors}</p>'
    body = ""
    if record.body:
        body = f'<pre class="body">{html.escape(record.body)}</pre>'
    sections_html = "".join(
        render_collection(section, domain.key) for section in sections
    )
    page_body = (
        f'<div class="crumbs"><a href="/">Hermes Portal</a> / '
        f'<a href="/{html.escape(domain.key)}">{html.escape(domain.title)}</a> / '
        f"{html.escape(record.id)}</div>"
        f"<h1>{html.escape(record.title)}</h1>"
        f'<p class="lede">{rich(record.subtitle)}</p>'
        f"{_badges(record)}{links}{table}{body}{sections_html}"
    )
    return _page(record.title, page_body, domains, built_at, domain.key)


def render_search(
    query: str,
    groups: Mapping[str, Sequence[Record]],
    totals: Mapping[str, int],
    domains: Sequence[Domain],
    built_at: str,
) -> str:
    """Render cross-domain search results, grouped by domain."""
    blocks = []
    for domain in domains:
        records = groups.get(domain.key, ())
        definition = (
            f'<span class="def">{len(records)} of '
            f"{totals.get(domain.key, 0):,} match(es) "
            "shown</span>"
        )
        if not records:
            blocks.append(
                '<section class="panel">'
                f"<h2>{html.escape(domain.title)}</h2>"
                '<p class="empty">no match</p></section>'
            )
            continue
        rows = "".join(_record_row(record, domain.key) for record in records)
        blocks.append(
            '<section class="panel">'
            f'<h2><a href="/{html.escape(domain.key)}">'
            f"{html.escape(domain.title)}</a></h2>"
            f'<div class="counts">{definition}</div>{rows}</section>'
        )
    hits = sum(len(records) for records in groups.values())
    body = (
        f'<div class="crumbs"><a href="/">Hermes Portal</a> / search</div>'
        f"<h1>Search: {html.escape(query)}</h1>"
        f'<p class="lede">{hits} hit(s) across {len(domains)} domains. '
        "Each domain searches the field it can: skills match name, title and "
        "description; sessions match titles and use the existing message full-text "
        "index; cron matches job definitions.</p>"
        f'<div class="grid">{"".join(blocks)}</div>'
    )
    return _page(f"Search: {query}", body, domains, built_at, "", query)


def render_not_found(domains: Sequence[Domain], built_at: str, what: str) -> str:
    """Render a 404 page that says what was missing."""
    body = (
        f'<div class="crumbs"><a href="/">Hermes Portal</a> / 404</div>'
        "<h1>Not found</h1>"
        f'<p class="lede">{html.escape(what)}</p>'
        f'<p class="lede">Known domains: '
        + ", ".join(
            f'<a href="/{html.escape(domain.key)}">{html.escape(domain.key)}</a>'
            for domain in domains
        )
        + "</p>"
    )
    return _page("Not found", body, domains, built_at)
