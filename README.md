# Hermes-Dashboard

A modular, standard-library-only Python framework for discovering, registering
and running *skills* — small self-contained Python programs described by a
`skill.json` manifest. The layout mirrors the Hermes Skill Deck system:

```
hermes/
  __init__.py
  core/
    __init__.py
    models.py        Skill / SkillResult dataclasses + manifest validation
    loader.py        skills directory -> [Skill], one bad skill never kills startup
    registry.py      SkillRegistry: name -> Skill, de-duplication, by_box()
    executor.py      argv construction + subprocess.run (no shell, never shell=True)
    runtime.py       Runtime: load once, run by name; profile loading
    skill_trees.py   skill discovery: roots, SKILL.md frontmatter, dedupe, filter
  skills/
    example_skill/
      skill.json     manifest
      main.py        the skill itself (argparse, --input, exit 0 / 2)
  profiles/
    default.json     {"name": "default", "skills_dir": "skills", "timeout": 60}
  cli/
    __init__.py
    shell.py         interactive `hermes>` REPL
  portal/            drill-down over everything Hermes keeps (one write path)
    __init__.py
    model.py         Domain / Collection / Record / Count (counts carry rules)
    sources.py       read-only SQLite, root resolution, formatting, a TTL cache
    fts.py           message search: FTS5 queries, excerpts around the hit, fallbacks
    state.py         favourites: the portal's own state file and the only writer
    taxonomy.py      the curated grouping behind the box tiles (presentation only)
    render.py        one generic page shape for every domain
    server.py        routes, JSON endpoints, handler
    __main__.py      python -m hermes.portal
    domains/         one adapter per domain
      __init__.py    default_registry()
      skills.py      4 roots, frontmatter, references
      sessions.py    state.db sessions and messages
      cron.py        jobs.json, executions.db, output reports
      usage.py       cost and token rollups, by day/model/provider
      health.py      launchd services, heartbeats, ports, tickers, storage
      logs.py        log tails, error signatures, credential scrubbing
      memory.py      MEMORY.md and USER.md per profile, against their budgets
      plugins.py     plugin manifests, kinds and requirements -- never imported
      vault.py       Obsidian notes: backlinks, tags, tasks, the Kanban board
      graph.py       the code graph: communities, risk, callers, flows
tests/
  __init__.py
  test_smoke.py      43 tests, the framework (manifest, loader, registry, executor)
  test_skill_trees.py 23 tests, the skill data layer on a fake Hermes tree
  test_portal.py     59 tests, the portal incl. the skills gallery
  test_portal_domains.py 31 tests, usage/health/logs + whole-registry invariants
  test_portal_heavy.py 35 tests, the vault and the code graph
  test_portal_ui.py  62 tests, favourites, theme, palette and the box tiles
  test_portal_memory.py 26 tests, the memory files and their budgets
  test_portal_plugins.py 26 tests, plugin discovery (and never running one)
  test_portal_fts.py 31 tests, message search: queries, excerpts, fallbacks
  test_portal_memory_history.py 22 tests, older copies of the memory files
README.md
pyproject.toml      packaging: setuptools, `hermes` console script, ruff config
LICENSE             MIT
.gitignore          .venv/, __pycache__/, build artifacts, caches, .DS_Store
```

## Requirements

* Python 3.11 or newer (developed and verified on 3.12.1)
* No third-party dependencies — the framework is standard library only. It runs
  straight from the checkout (no build) or can be installed as a package.

## Install (optional)

Running from the checkout needs nothing. To get the `hermes` console script:

```sh
cd <project-root>
python3 -m venv .venv
.venv/bin/pip install -e .        # editable install
.venv/bin/hermes                  # the same shell as `python -m hermes.cli.shell`
```

A non-editable wheel carries the skills and profiles as well
(`hermes/profiles/*.json` and `hermes/skills/*/*` are declared as package data in
`pyproject.toml`), so an installed copy lists and runs skills with no project
directory present.

## Quick start

`python -m hermes.cli.shell` resolves the package from the current directory, so
run it from the project root — or use the installed `hermes` script, which works
from any directory:

```sh
cd <project-root>
python3 -m hermes.cli.shell
```

To run it from anywhere, put the project root on `PYTHONPATH`:

```sh
PYTHONPATH=<project-root> python3 -m hermes.cli.shell
```

Optional flags:

```sh
python3 -m hermes.cli.shell --root <dir-containing-skills>
python3 -m hermes.cli.shell --profile hermes/profiles/default.json
```

A real session (copied verbatim from a run):

```
$ python3 -m hermes.cli.shell
Hermes-Dashboard skill shell -- type 'help' for commands, 'exit' to leave.
hermes> skills
NAME           BOX  TITLE
-------------  ---  -----
example_skill  dev  Example Skill
hermes> run example_skill --input "hello there"
input    : hello there
upper    : HELLO THERE
reversed : ereht olleh
length   : 11
return code: 0
hermes> run example_skill --input=world
input    : world
upper    : WORLD
reversed : dlrow
length   : 5
return code: 0
hermes> exit
```

## REPL commands

| Command | Effect |
| --- | --- |
| `skills` | list every loaded skill: name, primary box, title |
| `run <skill> [--k v ...]` | run a skill, forwarding flags to its argv |
| `help` | print the command reference |
| `exit` / `quit` | leave the shell (Ctrl-D also works) |

Input is tokenised with `shlex.split`, so quoted arguments work. On success the
shell prints the skill's stdout; on failure it prints stderr; it always prints
the return code. Failures never kill the shell: an unknown skill, a bad flag or
unbalanced quotes are reported and the prompt returns.

## Flag rules

The same rules apply whether you call `Runtime.run()` or type into the shell.

| Value | argv produced |
| --- | --- |
| `--key value` | `["--key", "value"]` |
| `--key=value` | `["--key", "value"]` (use when the value starts with `-`) |
| `--flag` | `["--flag"]` — boolean `True` |
| repeated `--key` | `["--key", "a", "--key", "b"]` — list argument |
| flag omitted | boolean `False`, or the `skill.json` default |

`True`, `False` and `None` are never stringified: `True` becomes a bare flag,
`False`/`None` omit it entirely. `int`, `float` and `str` are rendered with
`str()`. List items repeat the flag once each, in order.

`timeout` is the one keyword the framework consumes rather than forwards:
`runtime.run("skill", timeout=30)` sets that call's timeout.

## Adding a new skill

1. Create the directory `hermes/skills/<skill_name>/`. **The directory name must
   match the manifest's `name` field** — the executor resolves entrypoints as
   `<skills_dir>/<name>/<entrypoint>`, and the loader warns when they disagree.

2. Add `hermes/skills/<skill_name>/skill.json` with every required field:

   ```json
   {
     "name": "word_count",
     "title": "Word Count",
     "description": "Counts words, lines and characters in a text input.",
     "primary_box": "dev",
     "raw_category": "text",
     "tags": ["text", "stats"],
     "entrypoint": "main.py",
     "args": { "input": "hello world" },
     "related": ["example_skill"]
   }
   ```

3. Add `hermes/skills/<skill_name>/main.py`. It is launched as a plain script
   with `sys.executable`, so it must be standard library only and must not
   import the `hermes` package (the skill directory, not the project root, is on
   `sys.path` when it runs). Use `argparse`, accept the flags declared in
   `args`, exit `0` on success:

   ```python
   """Count words, lines and characters in a text input."""

   from __future__ import annotations

   import argparse


   def main(argv: list[str] | None = None) -> int:
       """Print the word, line and character counts of --input."""
       parser = argparse.ArgumentParser(prog="word_count")
       parser.add_argument("--input", required=True, help="text to measure")
       args = parser.parse_args(argv)
       print(f"words : {len(args.input.split())}")
       print(f"lines : {len(args.input.splitlines())}")
       print(f"chars : {len(args.input)}")
       return 0


   if __name__ == "__main__":
       raise SystemExit(main())
   ```

4. That is the whole registration step — `load_skills` discovers the directory
   automatically. Verify with `skills` and
   `run word_count --input "one two three"`.

## Running it from Python

`Runtime(root)` and the shell's `--root` take the directory that **contains**
`skills/`. For this repository that is `hermes/`, not the project root:

```python
from pathlib import Path

from hermes.core import Runtime

runtime = Runtime(Path("<project-root>/hermes"))   # loads hermes/skills
print(runtime.skills.names())                      # ['example_skill']

result = runtime.run("example_skill", input="hello")
print(result.stdout, end="")                       # the skill's report
print(result.returncode)                           # 0

# a project whose skills live somewhere else:
other = Runtime(Path("<some-project>"), skills_dir=Path("<some-project>/skills"))
```

Mistaking the project root for the skills root is not silent: the loader reports
the missing directory, names the `skills_dir=` fix in the warning, and returns an
empty registry instead of raising.

## Manifest field reference

Every field below is required. Validation is strict about *presence* and *type*
only, so extra keys are ignored and manifests stay forward compatible. A
manifest that fails validation is reported on stderr and skipped — it never
prevents the remaining skills from loading.

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | str | Unique identifier, `snake_case`. Must match the directory name. Used by `run` and by the executor's path resolution. |
| `title` | str | Human-readable name, shown by `skills`. |
| `description` | str | One-line summary of what the skill does. |
| `primary_box` | str | Top-level category, e.g. `dev`, `media`, `ops`. Group with `SkillRegistry.by_box()`. |
| `raw_category` | str | Finer-grained category label. |
| `tags` | list[str] | Free-form search/label terms. |
| `entrypoint` | str | Path to the Python file *relative to the skill directory*, normally `main.py`. |
| `args` | dict | Argument name (without dashes) -> default value. Values may be `str`, `bool`, `int`, `float` or a list of those. |
| `related` | list[str] | Names of related skills. |

## Profiles

A profile is a small JSON document describing how to start the framework:

| Key | Type | Meaning |
| --- | --- | --- |
| `name` | str | Profile name. |
| `skills_dir` | str | Skills directory, resolved relative to the *package directory* — the parent of the `profiles` directory. |
| `timeout` | int | Default per-run timeout in seconds (optional, defaults to 60; must be positive). |

`Runtime.from_profile(path)` resolves `hermes/profiles/default.json` with
`"skills_dir": "skills"` to `hermes/skills`, and passes the profile timeout to
every skill it runs.

## Web views

There is now **one web server**: the portal (below). The Skill Deck was folded into
it -- its card grid and box dropdown are the portal's skills gallery -- so the module
`hermes/web/skill_deck.py`, the `python -m hermes.web.skill_deck` entry point and the
separate server are gone. What moved where:

| Was | Now |
| --- | --- |
| the deck's card grid + box dropdown | the portal's skills gallery (`display="cards"` + a `Picker`) |
| `skill_deck.discover_skills` / `resolve_hermes_roots` / `split_frontmatter` | `hermes/core/skill_trees.py` (view-free) |
| `skill_deck.filter_cards` (box **or** category path) | `skill_trees.filter_cards`, now used by the portal too, so `?box=mlops/evaluation` works there as well |
| `python -m hermes.web.skill_deck --box creative` | `python -m hermes.portal` then `/skills?box=creative` |
| `hermes-deck` | still exists as an **alias** that launches the portal |

One behaviour changed on purpose: a box filter that matches nothing used to be
silently ignored, which showed unfiltered skills under a filtered URL. It now
reports zero and says why. Skills are also no longer counted with this project's own
`example_skill` mixed in -- the portal shows what Hermes has, not what this repo ships.

**Skills gallery**

`/skills` is the deck's successor: one card per skill, a **Box** dropdown listing
every box with its count, and the filter in the URL (`?box=creative`,
`?box=mlops/evaluation` for a category path, repeatable). It keeps everything the
portal insists on -- the count's definition, the extra counts that differ from it,
the sources and the read time all stay on the page. The dropdown is drawn from the
*unfiltered* inventory, so every box stays reachable after one is applied, and a
filter the dropdown cannot offer (a category path, or a value that matched nothing)
is shown as a disabled "current filter" entry rather than being hidden.
## Hermes Portal

A second web view, and the start of the system around everything Hermes keeps.
Every **source** is opened read-only -- SQLite with `mode=ro` plus `query_only`, HTTP
probes as GETs, files read from the end -- and there is exactly **one** write in the
project: starring a record, which updates the portal's own state document and nothing
else. Every other POST is refused by name.

```sh
python3 -m hermes.portal                 # http://127.0.0.1:8087
.venv/bin/hermes-portal --port 8087      # installed console script
python3 -m hermes.portal --list          # registry summary, no server
.venv/bin/hermes-portal --no-state       # serve with writing switched off
.venv/bin/hermes-portal --state /tmp/p.json   # put the favourites file elsewhere
```

The default is **127.0.0.1:8087**, deliberately: not 8080 (the retired deck's port,
which is now free and stays free) and not 9119 (Hermes' own dashboard). It binds
localhost only, and `--port 0` picks a free port when something else already holds
it.

Eight domains, each with collections, drill-down and search:

| Domain | Source | Reads | Headline |
| --- | --- | --- | --- |
| skills | 4 skill roots (shared + 3 profiles) | `SKILL.md` trees, frontmatter, `references/` | 158 unique names — plus 422 files, 55 boxes, 4 roots |
| sessions | `state.db`, incl. the `messages_fts` and `messages_fts_trigram` indexes | `sessions`, `messages`; search runs through the indexes | 91 sessions — plus 9,504 messages, all searchable |
| cron | `cron/jobs.json`, `cron/executions.db`, `cron/output/` | jobs, runs, incidents, reports | 10 jobs — plus 1,000 executions |
| usage | `state.db` | `sessions`, `session_model_usage` | 87 priced sessions — $16.68 estimated, 30.0M in / 2.6M out tokens, 4,292 calls |
| health | `launchctl`, LaunchAgents plists, `state.db`, `lsof`, `cron/ticker_*`, file sizes | services, heartbeats, ports, tickers, storage | the services found — plus running/stale counts |
| logs | `$HERMES_HOME/logs`, `~/Library/Logs` | the last 200 KB of each file, error signatures | log files in scope — plus distinct signatures |
| vault | the Obsidian vault (`--vault`, default `/Volumes/Data/MyObsidian`) | all notes indexed in memory, frontmatter, `[[wiki links]]`, checkboxes, the Kanban board | 762 notes — plus 1,116 links, 141 tags, 76 open items |
| graph | `.code-review-graph/graph.db` (`--graph-db`) | precomputed tables: communities, risk, flows; FTS for search | 38 communities — plus 170,971 nodes, 1.5M edges (cached count) |
| memory | `memories/MEMORY.md` and `USER.md`, per profile, plus `config.yaml`, plus the archives under `backups/` | every entry, split on the section sign, measured against its character budget; older copies with what changed since | 6 files over 3 profiles — 42 entries; one at 2,200/2,200; 2 archived copies from 2026-08-25 |
| plugins | `plugin.yaml` manifests under the bundled tree, `~/.hermes/plugins/`, each profile's, and `config.yaml` | names, kinds, versions, file lists, declared env vars and hooks | 105 plugins in 8 kinds; 41 declare requirements |

P0 was skills/sessions/cron; P1 added usage, health and logs; P2 the two heavy planes,
the vault and the code graph; P5 memory; P6 plugins. Every source is read-only; the only
write in the project is a favourite.

**Vault.** 762 notes totalling ~3 MB is small enough to index in one pass and keep in
memory, which buys the thing a folder listing cannot: **backlinks**. Open any note and
you see its frontmatter, its text, what it links to (and whether each target exists) and
what links back to it. Tags come from frontmatter; open work comes from two conventions,
because the vault has two — a checkbox line in an ordinary note is open while unchecked,
and a **Kanban card is open while its `Status` field is not done**, since the board's
checkboxes are not maintained. Reading the box alone reported 87 finished cards as open.

**Searching messages.** The portal's search now reaches what was *said*, not only the
titles of things. `messages_fts` answers first, ranked by relevance (`bm25`), and each hit
is an excerpt **around** the match with the terms marked — where it used to show the
message's first 200 characters, so a hit could display a paragraph that never mentions
your search. A search box is not FTS5 syntax, so user text is sanitised into quoted prefix
terms AND-ed together: `dashboard parser` finds messages containing both, and `-`, `"`,
`*`, `NEAR(` and friends are read as ordinary text rather than raising or meaning
something else. When the word index has nothing, the trigram index answers substrings
(`ashboa` finds `dashboard`), and with no index at all the query degrades to `LIKE` — each
path naming which index answered. The rendering escapes the excerpt and only then turns
the markers into `<mark>`, so a message's own text cannot inject anything. A **Recent
messages** collection on the sessions page makes the same corpus browsable.

That work also uncovered a bug in every domain: `Record.links` were built as
`(href, label)` and unpacked as `(label, href)`, so each detail link rendered with the URL
as its text and the human label as its href — `<a href="Its folder">/vault?folder=Homelab</a>`,
which navigated nowhere. The renderer matches the documented order now, and a test pins the
rendered markup.

**Memory history.** "What did memory say last week" is answerable, and the interesting
part is *which* containers hold the answer. The per-profile `state-snapshots/*-pre-update/`
directories do **not** keep memories — each ships a `manifest.json` listing exactly what it
holds (state.db, config.yaml, cron/, a few databases), so the page reads that manifest and
says so rather than scanning 126 MB for files that were never there. The archives under
`<hermes root>/backups/*.zip` do keep them, so history comes from those: only the
`memories/MEMORY.md` and `memories/USER.md` members are read, **in memory** (nothing is
extracted, asserted by a test), and the `.env` sitting beside them in the same archive is
never opened — asserted with a secret in a fixture archive.

Each older copy gets a page: the archived text, and what moved since, split three ways.
Entries are matched by their title line first, then by word overlap (40%+), because an
entry whose opening line was rewritten otherwise reads as a removal plus an addition. On
this machine the real archive shows the memory was substantially rewritten over three
weeks — `+8 -9 ~1` — which is the honest reading, not a diff artefact: most unmatched
entries share under a third of their words with today's.

**Plugins.** What Hermes is extended with, read from `plugin.yaml` manifests **without
running any of it**: the domain never imports a plugin, never executes its code, and
never evaluates a manifest with a full YAML parser (a documented subset handles the
three shapes these files use). That is not a shortcut — a plugin is arbitrary Python,
and a page that renders an inventory must not import one; the fixture suite ships
plugins that write a marker file on import and asserts the marker never appears. It
reports the bundled tree and every user/profile plugins directory, the kind either
declared in the manifest or inferred from its container (and says which), the files a
plugin ships, and the environment variable **names** it needs — never a value: 41
manifests declare `requires_env`, and a neighbouring `.env` is one thing this domain
does not read. Enablement is deliberately not claimed: this config has no
`plugins.enabled` list, so the page says which plugins `known_plugin_toolsets` names
per surface instead of inventing an on/off boolean, and directories without a manifest
are listed as such rather than counted as plugins.

**Memory.** The two files Hermes keeps per profile -- `MEMORY.md` (its own notes) and
`USER.md` (who it is working with) -- with entries split on the section sign and each
file measured against the budget `config.yaml` sets (`memory_char_limit`,
`user_char_limit`). The budget is the reason the domain exists: a file on its limit
cannot take another entry, so the page leads with usage and says which file is full.
When the config does not name a limit it reports characters and says so rather than
inventing a percentage, `*.lock` files are counted and skipped, and the page notes that
a running session holds the copy of memory it loaded at start, which can be older than
the file. The profile picker reads only the profiles that actually have memories, and a
profile with none shows nothing and says which directory it looked in.

**Code graph.** A deliberately narrow adapter over a 2.5 GB store, shaped by two rules.
Prefer what the builders already computed: `risk_index`, `flows` and
`community_summaries` answer in milliseconds, whereas recomputing degrees or language
histograms with group-bys over 1.5M edges costs 2–4 seconds per query, so those are not
on the page at all. And count what is cheap, cache what is not: `count(*)` on nodes is
instant, on edges it takes 3.3 seconds, so it is computed once in a **background thread**
and the metric says `counting…` until it lands, stamped with when it was taken. The
graph's 34 MCP tools remain its interactive interface — semantic search, refactor
previews and impact analysis are theirs; what the portal adds is a stable, linkable read
of what the store contains. A graph node's page shows its edges out and in, its flow
memberships, its risk row and its community.

### The interface (P3)

The mockup's UI on top of real data, with one new idea: a record can be **starred**.

* **Favourites are the portal's only write.** `POST /favorites.json` takes
  `{domain, id, action}` and updates `<hermes root>/portal/state.json` (override with
  `--state`, disable with `--no-state`). The request is validated in a fixed order --
  route, state, media type, body size, JSON, domain, record -- so a malformed request
  never reaches the filesystem, and writes are atomic (temp file plus `os.replace`,
  mode `0600`) so a crash cannot leave a half-written list. The title stored is the
  **record's** title, never the client's, so the file cannot be used as scratch space.
  Star buttons ship unpressed and hydrate from `GET /favorites.json`, which keeps the
  renderer free of state; with JavaScript off the portal still works unstarred.
* **The theme is a real light mode, not an inverted dark one.** One set of CSS custom
  properties per theme, chosen in `<head>` before first paint (saved choice, else the
  system preference), toggled from the header and remembered in `localStorage`.
* **Ctrl/⌘-K opens a command palette** over whatever is loaded (`/search.json`,
  debounced, `↑`/`↓`/Enter/Esc), and `/` opens it anywhere outside a text field. Every
  page also carries a right rail: usage totals, favourites, and each domain's headline
  count.
* **No app-mode window.** The mockup's final piece was a chrome-less window, which
  means launching a Chromium browser with `--app=`. This machine's default browser is
  not Chromium, so that would be a flag that silently does nothing; the theme toggle
  and the rail get the same effect inside a normal tab.
* **The box tiles are curated, and they say so.** The tree has 55 boxes and 43 of them
  hold exactly one skill, so `taxonomy.py` arranges the real boxes into eight groups
  -- name, blurb, emoji and a CSS gradient, no image assets. The tiles are
  presentation and the counts are measurement: a group's number is the sum of its
  member boxes' counts, read from the skills domain's own `boxes` collection, so a
  tile cannot disagree with the skills page. The section prints its coverage
  ("8 groups over the 55 boxes the tree actually has, covering 55 of them and 158 of
  158 skills") and lists every box the mapping does *not* name, so a new box appears
  as ungrouped rather than vanishing into a total, and a renamed box is flagged as a
  stale mapping entry.

### Running it somewhere else

This portal resolves everything from `$HERMES_HOME`, so a copy on another machine
works with no configuration -- but two domains are macOS-shaped and degrade rather
than pretend:

| Domain | macOS-specific part | On another platform |
| --- | --- | --- |
| health | `launchctl list`, `~/Library/LaunchAgents/*.plist` | finds nothing, reports 0 services with the sources marked `MISSING`; a `systemd` reader is the equivalent work |
| logs | also reads `~/Library/Logs/*.log` | reads `$HERMES_HOME/logs` only |
| vault | defaults to `/Volumes/Data/MyObsidian` | pass `--vault <path>`, or it reports an empty vault |

Everything else -- skills, sessions, cron, usage, the code graph, favourites -- is
resolved from the Hermes home and needs no platform code.

**Usage** is the single home for cost and token rollups: totals, by day, by model,
by provider and the most expensive sessions (each linking into the sessions view that
filters to it). The sessions domain keeps the index and each session's own usage --
one number, one place, so the two cannot drift.

**Health** answers "is it alive right now" rather than "is it configured": each
service's plist is joined with live `launchctl` state and a TCP probe of the port it
declares, so you get `running · port 9119: listening · last exit -15` instead of a
guess. It also carries the gateway's heartbeats, the cron ticker stamps, every
listening port, and the size of the stores Hermes grows (state.db, its WAL,
snapshots, backups, the 2.3 GB code graph).

**Logs** reads only the end of each file (200 KB) and normalises error lines into
signatures -- timestamps, levels, ids and numbers stripped -- which is what turns
"834 error lines" into `MCP server 'obsidian' failed after N reconnection attempts
x2283`. Every line that reaches a page is **scrubbed** first, because logs are where
a pasted key or a bearer header ends up.

Routes: `/` index, `/<domain>` collections, `/<domain>/<id>` a record and the
collections behind it (a session's messages, a cron job's runs and reports), and
`/search?q=...` across domains. `?box=`, `?model=` and `?provider=` narrow a
domain, and every `.json` variant returns the same data — `/index.json`,
`/skills.json`, `/skills/python-project-scaffolding.json`, `/search.json?q=...`.

### The rules the portal runs on

* **A count is never a bare integer.** Every collection publishes its definition
  next to the number, and competing counts appear side by side: the skills domain
  shows 157 unique names *and* 421 files *and* 55 boxes, because each answers a
  different question. Where a page shows a capped list it says "showing N of M".
* **Every collection names its sources and read time**, and a missing source is
  marked `MISSING` rather than silently empty. Adapter failures become notes on
  the page — one broken domain cannot take the portal down.
* **The portal writes one file, and the blast radius is tested.** SQLite is
  opened `mode=ro` with `query_only`; the only writer is the favourites store, and a
  test hashes the whole fake Hermes tree around a POST to prove the change set is
  exactly `portal/state.json`. Reading never creates the file, so a read-only home
  still serves (stars simply do not persist, and the page says so); `--no-state`
  turns writing off entirely.
* **Per-profile stores are disclosed, not merged.** Profiles keep their own
  `state.db` and `cron/executions.db`; the sessions page says so (2 sessions in
  the running profile, 6 in another) instead of quietly reading one of them.
* **Log text is scrubbed before it is rendered.** `sk-…`, `Bearer …`, `api_key=…`,
  `token=` and `password:` shapes become `<redacted>` on every path that shows free
  text, and signatures are built from scrubbed lines because a signature becomes a
  record *title* — masking only the body would still leak.
* **A count is the size of the set it describes.** Three P1 overviews shipped
  counting a five-record sample while meaning "32 files" or "87 sessions"; a test now
  asserts, for every collection in a fake registry, that the count equals the records
  shown when nothing is capped, and never falls below them.
* **Anything outside the process is patched in tests.** `launchctl`, `lsof` and
  `~/Library/Logs` are mocked, so the suite says the same thing on any machine.

Domains and views are separate: `hermes/portal/domains/<domain>.py` is one adapter
(Domain -> Collection -> Record), `hermes/portal/render.py` is one generic page
shape over that vocabulary, and a new domain is a new module plus a line in
`default_registry()`. P1 adds usage/health/logs; P2 the vault and the code graph
through its own MCP tools.

## Tests and checks

```sh
cd <project-root>
python3 -m unittest discover -s tests -t .   # 156 tests, ~3.6s
python3 -m unittest tests.test_smoke         # same suite
python3 tests/test_smoke.py                  # works directly too

# through the installed package, from an unrelated cwd:
.venv/bin/python -m unittest discover -s <project-root>/tests -t <project-root>
.venv/bin/hermes                             # console script, any cwd
```

The suite is hermetic: the only subprocesses are Python interpreters, the only
writes go to `tempfile` sandboxes, and no test reads the network or a real
terminal.

Lint settings ship in `pyproject.toml` (`[tool.ruff]` with
`select = ["E","W","F","I","UP","B","SIM","C4","RET","ARG","PTH"]`). Ruff is
**not** a project dependency — nothing in the tree imports anything outside the
standard library — so run it ephemerally if you do not have it installed:

```sh
uvx ruff check .            # reads the config from pyproject.toml
uvx ruff format --check .
```

## Return codes and error reporting

Skills return their own exit status. The framework uses distinct codes when it
cannot complete the run itself, mirroring shell conventions:

| Code | Meaning |
| --- | --- |
| `0` | Skill succeeded. |
| other `> 0` | The skill's own exit status (the example skill uses `2` for a missing `--input`). |
| `-1` | Timeout; `stderr` is set to `timeout`. |
| `126` | The process could not be launched. |
| `127` | The entrypoint file does not exist. |

Loader problems (malformed JSON, missing field, wrong type, unreadable file,
name/directory mismatch, duplicate skill name) are one-line `warning:` messages
on stderr; the offending skill is skipped and startup continues.

## Specification notes

This tree was generated from a written specification; these are the places where
it had to be interpreted, all of them deliberate:

* **Target directory.** The spec named `/Volumes/Development/Hermes-Dashboard`,
  a volume that does not exist on the generation host (and `/Volumes` is not
  writable without `sudo`). The tree was written to the developer's project root
  as `Hermes-Dashboard`, keeping the spec's own spelling; the spec's other
  spelling, `HermesDashboard`, refers to the same tree.
* **Packaging came later, on request.** The spec listed an exact tree, so the
  first pass shipped without `pyproject.toml`, `LICENSE` or `.gitignore`. They
  were added afterwards; no file from the specified tree changed shape to
  accommodate them, and `python -m hermes.cli.shell` still runs uninstalled.
* **Extensions kept inside the specified files.** `load_profile()` and
  `Runtime.from_profile()` exist so `profiles/default.json` is functional rather
  than decorative; `Runtime` gained optional `skills_dir`/`timeout` keywords
  (with no arguments it behaves exactly as specified); the shell's `main()`
  accepts `--root`/`--profile` so it can be pointed at another tree. None of
  these add files to the tree.
* **Flag rendering for `None`.** The spec listed `str`/`int`/`float`/`bool`/list;
  `None` is treated like `False` and omits the flag.
* **Extra return codes `126`/`127`.** The spec only fixed `-1` for timeouts; the
  launch-failure and missing-entrypoint cases return data instead of raising, so
  the runtime can keep going.
* **Loader warnings beyond the spec.** A manifest whose `name` disagrees with its
  directory name loads but warns, because such a skill could never be executed.
