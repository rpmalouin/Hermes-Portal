"""Example Hermes Portal skill: transform a text input.

This file is launched as a plain script by
:func:`hermes.core.executor.run_skill`, so it is standard-library only and
imports nothing from the ``hermes`` package -- that also means it runs
standalone::

    python hermes/skills/example_skill/main.py --input "hello world"

Contract: accepts ``--input`` (required), prints the input, its uppercase form,
its reverse and its length, exits ``0`` on success and ``2`` when ``--input`` is
missing (argparse's own exit code).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def transform(text: str) -> dict[str, str]:
    """Return the upper / reversed / length views of *text* as display strings."""
    return {
        "input": text,
        "upper": text.upper(),
        "reversed": text[::-1],
        "length": str(len(text)),
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the skill's argument parser (``--input`` is required)."""
    parser = argparse.ArgumentParser(
        prog="example_skill",
        description="Uppercase, reverse and measure --input.",
    )
    parser.add_argument("--input", required=True, help="text to transform")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the skill.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` on success, ``2`` when ``--input`` is missing.
    """
    args = build_parser().parse_args(argv)
    if args.input is None:
        print("error: --input is required", file=sys.stderr)
        return 2

    values = transform(args.input)
    width = max(len(key) for key in values)
    for key, value in values.items():
        print(f"{key:<{width}} : {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
