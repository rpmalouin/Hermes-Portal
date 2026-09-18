# demo_shot

The kit behind the screenshot in the README.

`docs/screenshot.png` is a page of a **demo instance** — a fixture Hermes home with synthetic
sessions, profiles, jobs, services and notes — because a screenshot of a real one would carry
its reader's memory, session titles and paths onto a public page.  These four files build that
fixture, serve it and capture it, so the image can be regenerated instead of kept as a blob
nobody can re-shoot.

Needs Python 3.11+, `node` (for the two capture scripts), and a Chrome/Chromium you can start
with a debugging port.

## The shoot

```bash
# 1. build the fixture -- schema replayed from your own stores, DDL only, never rows
rm -rf /tmp/portal-demo                     # the DDL replay creates tables that exist otherwise
python3 tools/demo_shot/build_demo.py

# 2. serve it, in a terminal of its own
python3 tools/demo_shot/shim.py             # http://127.0.0.1:8089/

# 3. scan every route before you capture: nothing real may appear on the pages
for route in / /index.json /agents /sessions /vault; do
  curl -s "http://127.0.0.1:8089$route"
done | grep -Ec "/Users/|/Volumes/|code-scaffolding-agent"   # must print 0

# 4. capture -- Chrome running with --remote-debugging-port=9222
node tools/demo_shot/shot.js http://127.0.0.1:8089/ docs/screenshot.png 2826 1
```

## Two things worth knowing

**The framing is part of the image.**  The committed screenshot is a 1x capture of a **2826 CSS
px** viewport.  The index's card grid is `auto-fill`, so a narrower emulated viewport silently
re-flows it: at 1413 CSS px the same page is two columns and twice as tall, which looks nothing
like the image it is replacing.  `shot.js` takes the width as an argument — keep it at 2826
with dpr 1.  If the height still looks wrong, `heights.js <url>` lists the tallest elements with
their class names rather than leaving you to guess which card grew.

**A card that finds nothing says so.**  The fixture has to be complete or the public page
advertises the gaps — the agents card renders a single row without profile stores, the graph
card reads *"graph.db could not be read"* without a store, health reports *0 services* without
the demo plists, and the memory card notes a missing `config.yaml`.  `build_demo.py` writes all
of them, replaying each store's schema from the real one so the fixture cannot drift from it.

## Files

| file | what it does |
|---|---|
| `build_demo.py` | writes the fixture: stores replayed from your live DDL filled with synthetic rows, skills generated from the portal's own taxonomy, the vault, favourites, three profiles, the code graph, LaunchAgent plists, `config.yaml` |
| `shim.py` | points the readers that would otherwise fall back to a real path at the fixture (`$HERMES_HOME`, `logs.LIBRARY_LOGS`, `health.LAUNCH_AGENTS`, a fixed `launchctl`/`lsof`), then starts the portal |
| `shot.js` | drives Chrome over CDP and writes a full-page PNG: `<url> <out.png> [widthCss] [dpr]` |
| `heights.js` | prints a page's tallest elements: `<url>` |
