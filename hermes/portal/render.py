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
from .state import Favorite

BODY_PREVIEW = 400
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

PAGE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#16162a">
<title>$title</title>
<script>
    // Theme before first paint: an explicit choice wins, otherwise the system's.
    (function () {
        try {
            var saved = localStorage.getItem("portal-theme");
            var system = window.matchMedia("(prefers-color-scheme: light)").matches
                ? "light" : "dark";
            document.documentElement.dataset.theme = saved || system;
        } catch (err) { document.documentElement.dataset.theme = "dark"; }
    })();
</script>
<style>
    :root {
        color-scheme: dark;
        --bg: #16162a;
        --fg: #e9eaf0;
        --panel: #1b1d33;
        --panel-2: #1e2140;
        --border: #272a45;
        --border-2: #2c3055;
        --border-3: #23264a;
        --muted: #8d92ad;
        --muted-2: #7d829c;
        --muted-3: #6b7089;
        --soft: #b6bad0;
        --sub: #a9aec6;
        --link: #7fb2ff;
        --pill-bg: #23264a;
        --pill-fg: #c3c8e0;
        --badge-bg: #1f3a5f;
        --badge-fg: #bcd6ff;
        --warn: #e7c07b;
        --code-bg: #14152a;
        --nav-bg: #1e2140;
        --here-bg: #2b3a63;
        --here-fg: #dce7ff;
        --here-border: #4a6bb0;
        --btn-fg: #dce7ff;
        --accent: #b8763f;
        --shadow: 0 18px 40px rgba(0, 0, 0, 0.45);
    }
    :root[data-theme="light"] {
        color-scheme: light;
        --bg: #f6f4f1;
        --fg: #23211f;
        --panel: #ffffff;
        --panel-2: #faf7f4;
        --border: #e2ddd7;
        --border-2: #d6cfc7;
        --border-3: #eae5df;
        --muted: #6f6a64;
        --muted-2: #7a746d;
        --muted-3: #8b857d;
        --soft: #4a453f;
        --sub: #5b554e;
        --link: #a2501e;
        --pill-bg: #f1ece6;
        --pill-fg: #5b554e;
        --badge-bg: #f6e6d8;
        --badge-fg: #8a4a15;
        --warn: #8a5a10;
        --code-bg: #f4f1ed;
        --nav-bg: #ffffff;
        --here-bg: #f0e2d4;
        --here-fg: #7c3f10;
        --here-border: #c98f52;
        --btn-fg: #7c3f10;
        --accent: #b8763f;
        --shadow: 0 18px 40px rgba(90, 70, 50, 0.16);
    }
    * { box-sizing: border-box; }
    body {
        background: var(--bg); color: var(--fg); margin: 0; padding: 1.75rem 2rem 4rem;
        font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
        line-height: 1.5;
    }
    a { color: var(--link); text-decoration: none; }
    a:hover { text-decoration: underline; }
    header.top {
        align-items: center; border-bottom: 1px solid var(--border); display: flex;
        flex-wrap: wrap; gap: 0.75rem; margin: 0 0 1.5rem; padding: 0 0 1rem;
    }
    header.top .brand { font-size: 1.15rem; font-weight: 700; letter-spacing: 0.01em; }
    nav.domains { display: flex; flex-wrap: wrap; gap: 0.85rem; }
    nav.domains a {
        background: var(--nav-bg); border: 1px solid var(--border-2);
        border-radius: 999px; font-size: 0.85rem; padding: 0.25rem 0.75rem;
    }
    nav.domains a.here {
        background: var(--here-bg); border-color: var(--here-border);
        color: var(--here-fg);
    }
    form.search { display: flex; gap: 0.5rem; margin-left: auto; }
    form.search input {
        background: var(--nav-bg); border: 1px solid var(--border-2);
        border-radius: 8px;
        color: var(--fg); min-width: 14rem; padding: 0.4rem 0.6rem;
    }
    form.search button, .ghost {
        background: var(--here-bg); border: 1px solid var(--here-border);
        border-radius: 8px; color: var(--btn-fg); cursor: pointer;
        font-size: 0.85rem; padding: 0.4rem 0.8rem;
    }
    .ghost {
        background: var(--nav-bg); border-color: var(--border-2);
        color: var(--fg);
    }
    h1 { font-size: 1.9rem; margin: 0 0 0.35rem; }
    h2 { font-size: 1.25rem; margin: 2rem 0 0.5rem; }
    .crumbs { color: var(--muted); font-size: 0.85rem; margin: 0 0 0.6rem; }
    .lede { color: var(--soft); margin: 0 0 1.25rem; max-width: 70ch; }
    .panel {
        background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
        margin: 0 0 1.1rem; padding: 1.1rem 1.25rem;
    }
    .layout {
        align-items: start; display: grid; gap: 1.25rem;
        grid-template-columns: minmax(0, 1fr);
    }
    @media (min-width: 1200px) {
        .layout.with-rail { grid-template-columns: minmax(0, 1fr) 21rem; }
    }
    .rail {
        display: flex; flex-direction: column; gap: 1.1rem;
        position: sticky; top: 1rem;
    }
    .rail .panel { margin: 0; }
    .rail h3 {
        font-size: 0.78rem; letter-spacing: 0.09em; margin: 0 0 0.6rem;
        text-transform: uppercase; color: var(--muted);
    }
    .rail .stat {
        display: flex; justify-content: space-between; gap: 1rem;
        font-size: 0.88rem;
    }
    .rail .stat + .stat {
        border-top: 1px solid var(--border-3); margin-top: 0.4rem;
        padding-top: 0.4rem;
    }
    .rail .stat b { font-variant-numeric: tabular-nums; }
    .grid {
        display: grid; gap: 1.1rem;
        grid-template-columns: repeat(auto-fill, minmax(21rem, 1fr));
    }
    .counts {
        align-items: baseline; display: flex; flex-wrap: wrap; gap: 0.6rem;
        margin: 0 0 0.6rem;
    }
    .counts .headline { font-size: 1.9rem; font-weight: 700; }
    .counts .def { color: var(--muted); font-size: 0.82rem; }
    .pill {
        background: var(--pill-bg); border: 1px solid var(--border-3);
        border-radius: 999px;
        color: var(--pill-fg); font-size: 0.75rem; padding: 0.15rem 0.6rem;
    }
    .row {
        border-top: 1px solid var(--border-3); display: flex; flex-wrap: wrap;
        gap: 0.5rem 0.9rem; padding: 0.6rem 0; align-items: flex-start;
    }
    .row:first-of-type { border-top: none; }
    .row .title { font-weight: 600; }
    .row .sub { color: var(--sub); flex: 1 1 22rem; font-size: 0.88rem; }
    .badges { display: flex; flex-wrap: wrap; gap: 0.35rem; }
    .badge {
        background: var(--badge-bg); border-radius: 6px; color: var(--badge-fg);
        font-size: 0.72rem; padding: 0.1rem 0.45rem;
    }
    .meta { color: var(--muted-2); font-size: 0.78rem; }
    .notes {
        color: var(--warn); font-size: 0.82rem; margin: 0.5rem 0 0;
        padding-left: 1.1rem;
    }
    .sources { color: var(--muted-2); font-size: 0.78rem; margin: 0.55rem 0 0; }
    table.fields { border-collapse: collapse; margin: 0.4rem 0 0; width: 100%; }
    table.fields th, table.fields td {
        border-top: 1px solid var(--border-3); font-size: 0.86rem;
        padding: 0.35rem 0.6rem 0.35rem 0; text-align: left;
        vertical-align: top; word-break: break-word;
    }
    table.fields th {
        color: var(--muted); font-weight: 500; white-space: nowrap; width: 12rem;
    }
    pre.body {
        background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px;
        color: var(--fg); font-size: 0.8rem; margin: 0.5rem 0 0; max-height: 26rem;
        overflow: auto; padding: 0.75rem; white-space: pre-wrap; word-break: break-word;
    }
    details.body summary { color: var(--muted); cursor: pointer; font-size: 0.82rem; }
    .empty { color: var(--muted); font-style: italic; }
    footer { color: var(--muted-3); font-size: 0.78rem; margin-top: 2.5rem; }
    form.picker {
        align-items: center; display: flex; flex-wrap: wrap; gap: 0.6rem;
        margin: 0.4rem 0 1rem;
    }
    form.picker label { color: var(--muted); font-size: 0.85rem; }
    form.picker select {
        background: var(--nav-bg); border: 1px solid var(--border-2);
        border-radius: 8px;
        color: var(--fg); font-size: 0.9rem; min-width: 18rem;
        padding: 0.4rem 0.5rem;
    }
    form.picker button {
        background: var(--here-bg); border: 1px solid var(--here-border);
        border-radius: 8px; color: var(--btn-fg); padding: 0.4rem 0.7rem;
    }
    .grid .card {
        background: var(--panel-2); border: 1px solid var(--border-2);
        border-radius: 12px; padding: 1rem 1.1rem; transition: transform 0.15s ease;
    }
    .grid .card:hover { transform: translateY(-2px); border-color: var(--here-border); }
    .grid .card .box {
        color: var(--muted); font-size: 0.7rem; letter-spacing: 0.05em;
        text-transform: uppercase;
    }
    .grid .card h3 { font-size: 1.05rem; margin: 0.35rem 0 0.45rem; }
    .grid .card .desc { color: var(--soft); font-size: 0.85rem; line-height: 1.45; }
    .grid .card .desc code {
        background: var(--pill-bg); border-radius: 4px;
        padding: 0 0.25rem;
    }
    .grid .card .src {
        color: var(--muted-3); font-family: ui-monospace, monospace; font-size: 0.68rem;
        margin-top: 0.7rem; word-break: break-all;
    }
    button.star {
        background: none; border: none; color: var(--muted-2); cursor: pointer;
        font-size: 1.05rem; line-height: 1; padding: 0.1rem 0.3rem;
    }
    button.star::after { content: "\u2606"; }
    button.star[aria-pressed="true"] { color: var(--accent); }
    button.star[aria-pressed="true"]::after { content: "\u2605"; }
    button.star:hover { color: var(--accent); }
    .star-head { display: flex; align-items: center; gap: 0.5rem; }
    #palette {
        align-items: flex-start; background: rgba(8, 8, 18, 0.55);
        display: flex; inset: 0; justify-content: center; padding-top: 12vh;
        position: fixed; z-index: 20;
    }
    #palette[hidden] { display: none; }
    #palette .box {
        background: var(--panel); border: 1px solid var(--border-2);
        border-radius: 14px;
        box-shadow: var(--shadow); max-height: 70vh; overflow: hidden;
        width: min(46rem, 92vw); display: flex; flex-direction: column;
    }
    #palette input {
        background: transparent; border: none; border-bottom: 1px solid var(--border);
        color: var(--fg); font-size: 1rem; padding: 0.9rem 1.1rem; width: 100%;
    }
    #palette input:focus { outline: none; }
    #palette .results { overflow: auto; padding: 0.4rem 0.5rem 0.7rem; }
    #palette .hit {
        border-radius: 8px; cursor: pointer; display: flex; gap: 0.7rem;
        padding: 0.5rem 0.6rem; align-items: baseline;
    }
    #palette .hit.active { background: var(--here-bg); }
    #palette .hit .dom {
        color: var(--muted); font-size: 0.72rem; min-width: 5.5rem;
        text-transform: uppercase; letter-spacing: 0.05em;
    }
    #palette .hit .what { flex: 1; font-size: 0.92rem; }
    #palette .hit .why { color: var(--muted-2); font-size: 0.78rem; }
    #palette .hint {
        border-top: 1px solid var(--border); color: var(--muted-3);
        font-size: 0.75rem; padding: 0.5rem 1.1rem;
    }
    #toast {
        background: var(--panel); border: 1px solid var(--border-2);
        border-radius: 10px;
        bottom: 1.25rem; box-shadow: var(--shadow); color: var(--fg);
        font-size: 0.85rem;
        left: 50%; max-width: 90vw; opacity: 0; padding: 0.5rem 0.9rem;
        position: fixed; transform: translate(-50%, 1rem);
        transition: opacity 0.2s ease;
        pointer-events: none; z-index: 30;
    }
    #toast.show { opacity: 1; transform: translate(-50%, 0); }
</style>
</head>
<body>
<script src="/app.js"></script>
$nav
$body
<footer>
    Counts carry their definitions; sources and read time are shown per collection.
    Favourites are the portal's only write, kept in its own state file. Built $built_at.
</footer>
<div id="palette" hidden>
    <div class="box">
        <input type="search" placeholder="Search every domain&hellip;"
               aria-label="Search">
        <div class="results"><p class="empty">Type to search.</p></div>
        <div class="hint">&uarr;&darr; to move &middot; Enter to open
        &middot; Esc to close</div>
    </div>
</div>
<div id="toast" role="status" aria-live="polite"></div>
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
    placeholder = "Search every domain&hellip;"
    return (
        '<header class="top">\n'
        '  <div class="brand"><a href="/">Hermes Portal</a></div>\n'
        f'  <nav class="domains">{joined}</nav>\n'
        '  <form class="search" method="get" action="/search">\n'
        f'    <input name="q" value="{html.escape(query, quote=True)}"'
        f' placeholder="{placeholder}">\n'
        '    <button type="submit">Search</button>\n'
        "  </form>\n"
        '  <button class="ghost" type="button" id="palette-open" '
        'title="Search (Ctrl+K)">\u2318K</button>\n'
        '  <button class="ghost" type="button" id="theme-toggle" '
        'title="Light or dark">\u25d1</button>\n'
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
    """Wrap *body* in the shell.

    The shell renders no state of its own: the rail is part of *body*, and the star
    buttons hydrate from ``/favorites.json`` client-side.
    """
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


def star_button(domain_key: str, record_id: str, title: str) -> str:
    """Render a star toggle for one record.

    The button always renders unstarred: a small script reads ``/favorites.json``
    once and marks the starred ones, which keeps the renderer free of state and the
    page cacheable.  Without JavaScript the button does nothing -- the write path is
    a nicety, not the only way to use the portal.
    """
    target = html.escape(f"{domain_key}|{record_id}", quote=True)
    label = html.escape(title, quote=True)
    return (
        f'<button class="star" type="button" data-fav="{target}" data-title="{label}" '
        f'aria-pressed="false" aria-label="Star {label}" title="Star"></button>'
    )


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
        f'<div><div class="title">{star_button(domain_key, record.id, record.title)}'
        f"{title}</div>{meta}</div>"
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
    favorites: Sequence[Favorite] = (),
) -> str:
    """Render the portal index: one card per domain, plus the rail."""
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
    rail = _rail(domains, overviews, favorites)
    body = (
        '<div class="layout with-rail">'
        "<div>"
        "<h1>Hermes Portal</h1>"
        '<p class="lede">A drill-down view over everything Hermes keeps: skills, '
        "sessions, cron, usage, health, logs, the Obsidian vault and the code graph. "
        "Every count is labelled with the rule that produced it, every collection "
        "names the sources it read, and the only thing written is your favourites.</p>"
        f'<div class="grid">{"".join(cards)}</div>'
        "</div>"
        f"{rail}"
        "</div>"
    )
    return _page("Hermes Portal", body, domains, built_at)


def _rail(
    domains: Sequence[Domain],
    overviews: Sequence[Collection],
    favorites: Sequence[Favorite] = (),
) -> str:
    """The right rail: usage totals, favourites, and every domain at a glance."""
    by_key: dict[str, Collection] = {
        domain.key: overview
        for domain, overview in zip(domains, overviews, strict=False)
    }
    blocks: list[str] = []

    usage = by_key.get("usage")
    if usage is not None and usage.metrics:
        rows = "".join(
            f'<div class="stat"><span>{html.escape(str(label))}</span>'
            f"<b>{html.escape(str(value))}</b></div>"
            for label, value in usage.metrics[:5]
        )
        blocks.append(
            '<section class="panel"><h3>Usage overview</h3>'
            f'{rows}<div class="meta">as of {html.escape(usage.as_of)}</div></section>'
        )

    blocks.append(_favorites_panel(favorites))

    glance = []
    for domain in domains:
        overview = by_key.get(domain.key)
        if overview is None:
            continue
        glance.append(
            f'<div class="stat"><span><a href="/{html.escape(domain.key)}">'
            f"{html.escape(domain.title)}</a></span>"
            f"<b>{overview.count.value:,}</b></div>"
        )
    blocks.append(
        '<section class="panel"><h3>At a glance</h3>' + "".join(glance) + "</section>"
    )
    return f'<aside class="rail">{"".join(blocks)}</aside>'


def _favorites_panel(favorites: Sequence[Favorite]) -> str:
    """The favourites block, with a link to the full page when there are any."""
    if not favorites:
        return (
            '<section class="panel"><h3>Favourites</h3>'
            '<p class="empty">Star anything with the \u2606 beside it.</p></section>'
        )
    rows = "".join(
        '<div class="stat"><span><a href="/'
        f'{html.escape(favorite.domain)}/{html.escape(favorite.id, quote=True)}">'
        f"{rich(favorite.title)}</a></span>"
        f'<span class="meta">{html.escape(favorite.domain)}</span></div>'
        for favorite in favorites[:8]
    )
    more = (
        f'<div class="meta"><a href="/favorites">all {len(favorites)}</a></div>'
        if len(favorites) > 8
        else ""
    )
    return f'<section class="panel"><h3>Favourites</h3>{rows}{more}</section>'


def render_favorites(
    favorites: Sequence[Favorite],
    domains: Sequence[Domain],
    built_at: str,
    state_note: str = "",
) -> str:
    """Render the favourites page: everything starred, grouped by domain."""
    if not favorites:
        inner = '<p class="empty">Nothing is starred yet.</p>'
    else:
        rows = "".join(
            '<div class="row">'
            f'<div><div class="title">'
            f"{star_button(favorite.domain, favorite.id, favorite.title)}"
            f'<a href="/{html.escape(favorite.domain)}/'
            f'{html.escape(favorite.id, quote=True)}">{rich(favorite.title)}</a>'
            f'</div><div class="meta">{html.escape(favorite.id)}</div></div>'
            f'<div class="sub"><a href="/{html.escape(favorite.domain)}">'
            f"{html.escape(favorite.domain)}</a></div>"
            f'<div class="meta">starred {html.escape(favorite.added_at)}</div>'
            "</div>"
            for favorite in favorites
        )
        inner = rows
    note = (
        f'<ul class="notes"><li>{html.escape(state_note)}</li></ul>'
        if state_note
        else ""
    )
    body = (
        '<div class="crumbs"><a href="/">Hermes Portal</a> / Favourites</div>'
        f"<h1>Favourites</h1>"
        f'<p class="lede">{len(favorites)} starred record(s). The portal keeps '
        f"these in its own state file and never writes to anything it reads.</p>"
        f'<section class="panel">{inner}</section>'
        f"{note}"
    )
    return _page("Favourites", body, domains, built_at)


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
        f'<div class="star-head"><h1>{html.escape(record.title)}</h1>'
        f"{star_button(domain.key, record.id, record.title)}</div>"
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


APP_JS = r"""
/* The portal's only script: theme, the command palette, and the star toggles.
   No dependencies, no state of its own beyond the theme in localStorage. */
(function () {
    "use strict";

    // -- theme ------------------------------------------------------------
    var toggle = document.getElementById("theme-toggle");
    function current() {
        return document.documentElement.dataset.theme === "light" ? "light" : "dark";
    }
    if (toggle) {
        toggle.textContent = current() === "light" ? "\u25d0" : "\u25d1";
        toggle.addEventListener("click", function () {
            var next = current() === "light" ? "dark" : "light";
            document.documentElement.dataset.theme = next;
            toggle.textContent = next === "light" ? "\u25d0" : "\u25d1";
            try { localStorage.setItem("portal-theme", next); }
            catch (err) { /* private mode */ }
        });
    }

    // -- toast ------------------------------------------------------------
    var toast = document.getElementById("toast");
    var toastTimer = null;
    function say(message) {
        if (!toast) { return; }
        toast.textContent = message;
        toast.classList.add("show");
        if (toastTimer) { clearTimeout(toastTimer); }
        toastTimer = setTimeout(function () { toast.classList.remove("show"); }, 2600);
    }

    // -- favourites -------------------------------------------------------
    function key(domain, id) { return domain + "|" + id; }

    function hydrate() {
        var buttons = document.querySelectorAll("[data-fav]");
        if (!buttons.length) { return; }
        fetch("/favorites.json").then(function (response) { return response.json(); })
            .then(function (data) {
                var starred = {};
                (data.favorites || []).forEach(function (favorite) {
                    starred[key(favorite.domain, favorite.id)] = true;
                });
                buttons.forEach(function (button) {
                    button.setAttribute(
                        "aria-pressed", starred[button.dataset.fav] ? "true" : "false"
                    );
                });
            })
            .catch(function () { /* the portal still works unstarred */ });
    }

    document.addEventListener("click", function (event) {
        var button = event.target.closest("[data-fav]");
        if (!button) { return; }
        event.preventDefault();
        var parts = button.dataset.fav.split("|");
        var domain = parts.shift();
        var payload = {
            domain: domain,
            id: parts.join("|"),
            title: button.dataset.title || "",
            action: button.getAttribute("aria-pressed") === "true" ? "remove" : "add"
        };
        button.disabled = true;
        fetch("/favorites.json", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        }).then(function (response) {
            return response.json().then(function (data) {
                return { ok: response.ok, data: data };
            });
        }).then(function (result) {
            if (!result.ok) {
                say(result.data.error || "could not save that");
                return;
            }
            button.setAttribute("aria-pressed", result.data.starred ? "true" : "false");
            say(result.data.starred ? "Starred" : "Unstarred");
        }).catch(function () {
            say("could not reach the portal");
        }).then(function () { button.disabled = false; });
    });

    // -- command palette --------------------------------------------------
    var overlay = document.getElementById("palette");
    var opener = document.getElementById("palette-open");
    if (!overlay) { return; }
    var field = overlay.querySelector("input");
    var results = overlay.querySelector(".results");
    var hits = [];
    var active = -1;
    var timer = null;

    function close() {
        overlay.hidden = true;
        active = -1;
    }

    function open() {
        overlay.hidden = false;
        field.value = "";
        results.innerHTML = '<p class="empty">Type to search.</p>';
        hits = [];
        field.focus();
    }

    function mark() {
        hits.forEach(function (hit, index) {
            hit.el.classList.toggle("active", index === active);
        });
        if (hits[active]) { hits[active].el.scrollIntoView({ block: "nearest" }); }
    }

    function render(data) {
        var groups = data.groups || {};
        var order = data.order || Object.keys(groups);
        hits = [];
        results.textContent = "";
        var any = false;
        order.forEach(function (domain) {
            (groups[domain] || []).forEach(function (record) {
                any = true;
                var el = document.createElement("div");
                el.className = "hit";
                var dom = document.createElement("span");
                dom.className = "dom";
                dom.textContent = domain;
                var what = document.createElement("span");
                what.className = "what";
                what.textContent = record.title;
                var why = document.createElement("span");
                why.className = "why";
                why.textContent = record.subtitle || "";
                el.appendChild(dom);
                el.appendChild(what);
                el.appendChild(why);
                var url = "/" + encodeURIComponent(domain) + "/" +
                    encodeURIComponent(record.id);
                el.addEventListener("click", function () { window.location = url; });
                results.appendChild(el);
                hits.push({ el: el, url: url });
            });
        });
        if (!any) {
            results.innerHTML = '<p class="empty">No matches.</p>';
        }
        active = hits.length ? 0 : -1;
        mark();
    }

    function search(query) {
        if (!query) {
            results.innerHTML = '<p class="empty">Type to search.</p>';
            hits = [];
            return;
        }
        fetch("/search.json?limit=6&q=" + encodeURIComponent(query))
            .then(function (response) { return response.json(); })
            .then(render)
            .catch(function () {
                results.innerHTML = '<p class="empty">Search failed.</p>';
            });
    }

    if (opener) { opener.addEventListener("click", open); }
    field.addEventListener("input", function () {
        if (timer) { clearTimeout(timer); }
        var query = field.value.trim();
        timer = setTimeout(function () { search(query); }, 140);
    });
    overlay.addEventListener("click", function (event) {
        if (event.target === overlay) { close(); }
    });
    document.addEventListener("keydown", function (event) {
        var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
            event.preventDefault();
            overlay.hidden ? open() : close();
            return;
        }
        if (event.key === "/" && !typing && overlay.hidden) {
            event.preventDefault();
            open();
            return;
        }
        if (overlay.hidden) { return; }
        if (event.key === "Escape") { close(); }
        else if (event.key === "ArrowDown" && hits.length) {
            event.preventDefault();
            active = (active + 1) % hits.length;
            mark();
        } else if (event.key === "ArrowUp" && hits.length) {
            event.preventDefault();
            active = (active - 1 + hits.length) % hits.length;
            mark();
        } else if (event.key === "Enter" && hits[active]) {
            event.preventDefault();
            window.location = hits[active].url;
        }
    });

    hydrate();
})();
"""
