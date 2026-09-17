"""Interactive ``hermes>`` REPL for the skill framework.

Commands::

    skills                      list all skills (name + title + box)
    run <skill> [--k v ...]     run a skill, forwarding flags
    help                        print command reference
    exit | quit                 leave the shell

Entry point::

    python -m hermes.cli.shell                 # this package's own skills/
    python -m hermes.cli.shell --root <dir>    # a directory containing skills/
    python -m hermes.cli.shell --profile <p>   # a profile JSON (see profiles/)

The input line is tokenised with :func:`shlex.split`, so quoted arguments work
(``run example_skill --input "hello there"``).

Flag rules mirror ``hermes/core/executor.py``: ``--key value`` for a scalar,
``--flag`` alone for boolean ``True``, a repeated ``--key`` for a list, and
``--key=value`` when the value itself starts with ``-``.  Omitting a flag is how
you get ``False`` or the ``skill.json`` default.

Assumption (spec ambiguity): ``main()`` accepts an optional argv list and the
two optional path flags so the shell can be pointed at another tree and tested
without a subprocess; with no arguments it loads the ``hermes`` package's own
``skills`` directory.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import Any, TextIO

from ..core.models import SkillResult
from ..core.runtime import Runtime

PROMPT = "hermes> "
PACKAGE_DIR = Path(__file__).resolve().parents[1]

BANNER = "Hermes Portal skill shell -- type 'help' for commands, 'exit' to leave."

HELP_TEXT = """\
Commands
  skills                      list all skills (name, title, box)
  run <skill> [--k v ...]     run a skill, forwarding flags
  help                        print this reference
  exit | quit                 leave the shell

Flags
  --key value                 scalar argument (string)
  --flag                      boolean True
  --key v1 --key v2           list argument
  --key=value                 use when the value starts with '-'
  omitted                     boolean False (or the skill.json default)
"""


def parse_flags(tokens: list[str]) -> dict[str, Any]:
    """Turn REPL flag tokens into keyword arguments.

    Args:
        tokens: Tokens after the skill name, e.g. ``["--input", "hello"]``.

    Returns:
        A dict of flag name (without leading dashes) to value.  A flag repeated
        more than once collapses into a list.

    Raises:
        ValueError: A token does not look like a flag, or a flag name is empty.
    """
    kwargs: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            raise ValueError(f"unexpected token {token!r} (expected --flag)")

        name, separator, inline = token.lstrip("-").partition("=")
        if not name:
            raise ValueError("empty flag name")

        value: Any
        if separator:
            value = inline
            index += 1
        elif index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
            value = tokens[index + 1]
            index += 2
        else:
            value = True
            index += 1

        if name in kwargs:
            existing = kwargs[name]
            if isinstance(existing, list):
                existing.append(value)
            else:
                kwargs[name] = [existing, value]
        else:
            kwargs[name] = value

    return kwargs


def format_skills(runtime: Runtime) -> str:
    """Render the skill table used by the ``skills`` command."""
    skills = runtime.skills.list()
    if not skills:
        return "no skills loaded"

    name_width = max([len("NAME"), *(len(skill.name) for skill in skills)])
    box_width = max([len("BOX"), *(len(skill.primary_box) for skill in skills)])
    lines = [
        f"{'NAME':<{name_width}}  {'BOX':<{box_width}}  TITLE",
        f"{'-' * name_width}  {'-' * box_width}  {'-' * 5}",
    ]
    for skill in skills:
        lines.append(
            f"{skill.name:<{name_width}}  "
            f"{skill.primary_box:<{box_width}}  "
            f"{skill.title}"
        )
    return "\n".join(lines)


def report(result: SkillResult, out: TextIO) -> None:
    """Print a :class:`SkillResult`: stdout on success, stderr on failure, code."""
    if result.ok:
        if result.stdout:
            _write_block(out, result.stdout)
    elif result.stderr:
        _write_block(out, result.stderr)
    out.write(f"return code: {result.returncode}\n")


def execute_command(runtime: Runtime, line: str, out: TextIO | None = None) -> bool:
    """Handle one REPL line.

    Args:
        runtime: The runtime whose skills are addressable.
        line: Raw input line (quotes are honoured via :func:`shlex.split`).
        out: Stream to print to; defaults to ``sys.stdout``.

    Returns:
        ``False`` when the user asked to leave (``exit``/``quit`` or EOF), else
        ``True``.  Errors are reported on *out* and never raised.
    """
    stream = out if out is not None else sys.stdout
    stripped = line.strip()
    if not stripped:
        return True

    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        print(f"error: cannot parse input ({exc})", file=stream)
        return True
    if not tokens:
        return True

    command, rest = tokens[0], tokens[1:]

    if command in {"exit", "quit"}:
        return False
    if command == "help":
        _write_block(stream, HELP_TEXT)
        return True
    if command == "skills":
        print(format_skills(runtime), file=stream)
        return True
    if command == "run":
        if not rest:
            print("usage: run <skill> [--k v ...]", file=stream)
            return True
        skill_name, flag_tokens = rest[0], rest[1:]
        try:
            kwargs = parse_flags(flag_tokens)
        except ValueError as exc:
            print(f"error: {exc}", file=stream)
            return True
        try:
            result = runtime.run(skill_name, **kwargs)
        except KeyError as exc:
            print(f"error: {_message(exc)} (try 'skills')", file=stream)
            return True
        report(result, stream)
        return True

    print(f"error: unknown command {command!r} (try 'help')", file=stream)
    return True


def run_shell(
    runtime: Runtime,
    stream: TextIO | None = None,
    out: TextIO | None = None,
) -> int:
    """Read commands until ``exit``/``quit``/EOF.

    Args:
        runtime: Runtime to dispatch against.
        stream: Input stream; defaults to ``sys.stdin``.
        out: Output stream; defaults to ``sys.stdout``.

    Returns:
        ``0`` -- the shell always exits successfully.
    """
    instream = stream if stream is not None else sys.stdin
    outstream = out if out is not None else sys.stdout
    print(BANNER, file=outstream)

    while True:
        print(PROMPT, end="", file=outstream)
        outstream.flush()
        try:
            line = instream.readline()
        except KeyboardInterrupt:
            print("", file=outstream)
            continue
        if not line:
            print("", file=outstream)
            break
        try:
            if not execute_command(runtime, line, outstream):
                break
        except KeyboardInterrupt:
            print("\n(interrupted -- type 'exit' to leave)", file=outstream)

    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the shell's argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m hermes.cli.shell",
        description="Interactive shell for the Hermes Portal skill framework.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="directory containing skills/ (default: this package directory)",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="profile JSON to load; takes precedence over --root",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m hermes.cli.shell``.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` on a clean exit, ``1`` when the root or profile cannot be loaded.
    """
    args = build_parser().parse_args(argv)

    try:
        if args.profile is not None:
            runtime = Runtime.from_profile(args.profile)
        else:
            runtime = Runtime(args.root if args.root is not None else PACKAGE_DIR)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return run_shell(runtime)


def _write_block(out: TextIO, text: str) -> None:
    """Write *text* followed by exactly one trailing newline."""
    out.write(text if text.endswith("\n") else text + "\n")


def _message(exc: BaseException) -> str:
    """Best-effort human message for an exception (KeyError unwraps cleanly)."""
    if exc.args:
        return str(exc.args[0])
    return str(exc)


if __name__ == "__main__":
    raise SystemExit(main())
