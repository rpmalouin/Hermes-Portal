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
  portal/            read-only drill-down over everything Hermes keeps
    __init__.py
    model.py         Domain / Collection / Record / Count (counts carry rules)
    sources.py       read-only SQLite, root resolution, formatting
    render.py        one generic page shape for every domain
    server.py        routes, JSON endpoints, handler
    __main__.py      python -m hermes.portal
    domains/         one adapter per domain
      __init__.py    default_registry()
      skills.py      4 roots, frontmatter, references
      sessions.py    state.db sessions, messages, per-model usage
      cron.py        jobs.json, executions.db, output reports
tests/
  __init__.py
  test_smoke.py      43 tests, the framework (manifest, loader, registry, executor)
  test_skill_trees.py 23 tests, the skill data layer on a fake Hermes tree
  test_portal.py     59 tests, the portal incl. the skills gallery
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

A second web view, and the start of the system around everything Hermes keeps. It
is **read-only**: every adapter opens its source read-only, and there is no POST
route, so a client trying to change something gets the standard library's 501.

```sh
python3 -m hermes.portal                 # http://127.0.0.1:8087
.venv/bin/hermes-portal --port 8087      # installed console script
python3 -m hermes.portal --list          # registry summary, no server
```

The default is **127.0.0.1:8087**, deliberately: not 8080 (the retired deck's port,
which is now free and stays free) and not 9119 (Hermes' own dashboard). It binds
localhost only, and `--port 0` picks a free port when something else already holds
it.

P0 ships three domains, each with collections, drill-down and search:

| Domain | Source | Reads | Count shown as |
| --- | --- | --- | --- |
| skills | 4 skill roots (shared + 3 profiles) | `SKILL.md` trees, frontmatter, `references/` | 157 unique names — plus 421 files, 55 boxes, 4 roots alongside |
| sessions | `state.db` | `sessions`, `messages`, `session_model_usage` | 91 sessions — plus 9,504 messages, 4 providers |
| cron | `cron/jobs.json`, `cron/executions.db`, `cron/output/` | jobs, runs, incidents, reports | 10 jobs — plus 1,000 executions |

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
* **The portal never writes.** SQLite is opened `mode=ro` with `query_only`, no
  files are created and no POST is served. A test hashes the whole fake Hermes
  tree before and after exercising every route and fails on any difference.
* **Per-profile stores are disclosed, not merged.** Profiles keep their own
  `state.db` and `cron/executions.db`; the sessions page says so (2 sessions in
  the running profile, 6 in another) instead of quietly reading one of them.

Domains and views are separate: `hermes/portal/domains/<domain>.py` is one adapter
(Domain -> Collection -> Record), `hermes/portal/render.py` is one generic page
shape over that vocabulary, and a new domain is a new module plus a line in
`default_registry()`. P1 adds usage/health/logs; P2 the vault and the code graph
through its own MCP tools.

## Tests and checks

```sh
cd <project-root>
python3 -m unittest discover -s tests -t .   # 125 tests, ~3.5s
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
