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
  skills/
    example_skill/
      skill.json     manifest
      main.py        the skill itself (argparse, --input, exit 0 / 2)
  profiles/
    default.json     {"name": "default", "skills_dir": "skills", "timeout": 60}
  cli/
    __init__.py
    shell.py         interactive `hermes>` REPL
  web/
    __init__.py
    deck.py          Skill Deck web UI: this project + the running Hermes agent
tests/
  __init__.py
  test_smoke.py      98 tests, unittest only
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

## Web Skill Deck

```sh
python3 -m hermes.web.deck            # this project + the running Hermes agent
python3 -m hermes.web.deck --list     # print what would be shown, then exit
.venv/bin/hermes-deck --port 9000     # installed console script, any directory
```

`/` renders the card grid, `/skills.json` returns the same cards as JSON so a
script can check the load instead of counting `<div>`s. Flags: `--root`,
`--hermes-home`, `--profile`, `--all-profiles`, `--no-framework`, `--box`,
`--list`, `--host`, `--port`.

### Filtering by box

`--box` keeps one box of the deck and is repeatable; a value with a `/` in it is
a **category path**, which narrows further:

```sh
python3 -m hermes.web.deck --list --box software-development   # 28 skills
python3 -m hermes.web.deck --list --box mlops/evaluation       # 2 skills
python3 -m hermes.web.deck --box creative --box apple          # 21 skills, 133 hidden
python3 -m hermes.web.deck --list                              # lists every box + count
```

Matching is case-insensitive on the card's box (`creative`) or its category path
(`creative/ascii-art`); a category path also selects everything below it. The
page and `/skills.json` both report the active filter and how many cards it hid
(`hidden_by_filter`), and the *source* counts keep reporting what every root
contained, so a filtered deck still says how much of the whole it is showing.
`--list` with no `--box` prints the box breakdown, which is the easiest way to
find a name to pass in.

### The box dropdown

The page header carries a **Box** dropdown listing every box with its count, plus
`All boxes (154)`. It is a plain GET form -- `onchange` submits it, and a
`<noscript>` Apply button covers browsers with JavaScript off -- so the filter
lands in the URL and can be bookmarked or shared:

```
http://127.0.0.1:8765/?box=creative
http://127.0.0.1:8765/?box=creative&box=apple      # repeatable
http://127.0.0.1:8765/skills.json?box=mlops/evaluation
http://127.0.0.1:8765/?box=                         # blank clears the filter
```

`?box=` works on `/` and `/skills.json` alike, and the **URL beats `--box`**:
the flag only decides which view the server starts on, so choosing "All boxes"
in the dropdown really does clear a `--box` you launched with.

Two details keep the control honest. Options come from the *pre-filter*
inventory, so every box stays reachable after a filter is applied rather than
disappearing once selected; and when two or more boxes are active at once
(reachable only from `--box creative --box apple`) the picker shows a disabled
`boxes: creative, apple` entry instead of pretending one of them is the whole
selection. Nothing here duplicates the matching rule -- the dropdown sends
`?box=`, the server calls the same `filter_cards()` the CLI uses, and one
filesystem scan serves every view.

Which skills appear:

| Source | Read from | Count on the machine this was built on |
| --- | --- | --- |
| framework | `<root>/skills/*/skill.json` | 1 (`example_skill`) |
| hermes | `$HERMES_HOME/skills/**/SKILL.md` | the running profile's whole set |
| extra profiles | `$HERMES_HOME/profiles/*/skills`, with `--all-profiles` | further unique names |

Counts move as skills are added or removed — during the session this deck was
built, the running profile went from 152 to 153 skills while the server was up,
and the deck simply reported the new number. Treat any figure here as a
snapshot, not a constant.

Hermes sets `$HERMES_HOME` itself: in a live session it points at the running
profile directory, whose `skills/` directory *is* the skill set the agent has.
With no such variable the deck falls back to `~/.hermes`. `--all-profiles` also
finds that directory's siblings whether `$HERMES_HOME` is a hermes root or a
single profile.

Three details decide whether the count is right:

* **Symlinks are followed** (`os.walk(followlinks=True)`). Skills symlinked in
  from another checkout are invisible to `Path.rglob`; the deck finds them.
* **Cards are de-duplicated** by frontmatter `name` and by real path. The same
  skill is reachable through several profiles: walking everything unreconciled
  yields hundreds of paths for ~150 names.
* **Only top-level frontmatter scalars are read** (`name`, `description`, title
  from the first `#` heading). Nested blocks such as `metadata:` are ignored
  rather than guessed at, and every interpolated value is HTML-escaped before
  any markup is added, so a skill description containing `<`, `&` or backticks
  renders as text.

The response also lists every source it scanned with its count, and marks a
source `MISSING` when the directory is absent, so an empty or short deck is
self-explaining.

## Tests and checks

```sh
cd <project-root>
python3 -m unittest discover -s tests -t .   # 98 tests, ~5.0s
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
