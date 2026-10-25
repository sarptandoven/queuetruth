"""Routing of `queuetruth demo` ahead of the clap grammar (stdlib only).

`queuetruth.cli.main` calls `maybe_run(argv)` before every command. It only looks at the tokens; the demo's own
modules are imported only when the command really is `demo` (or `help demo`), so no other command pays for them or
can be broken by them.
"""

from __future__ import annotations

GLOBAL_VALUE_FLAGS = ("--format", "--output-format", "--log-level", "--threads", "--seed")
GLOBAL_FLAGS = ("--quiet", "--verbose")


def _command_index(argv: list[str]) -> int | None:
    """Index of the subcommand token when it is `demo` (global options may come first), else None."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if a in GLOBAL_FLAGS or any(a.startswith(f + "=") for f in GLOBAL_VALUE_FLAGS):
            i += 1
            continue
        return i if a == "demo" else None
    return None


def _help_index(argv: list[str]) -> int | None:
    """Index of `help` in `help demo` (global options may come first), else None."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if a in GLOBAL_FLAGS or any(a.startswith(f + "=") for f in GLOBAL_VALUE_FLAGS):
            i += 1
            continue
        return i if a == "help" and argv[i + 1 : i + 2] == ["demo"] else None
    return None


def maybe_run(argv: list[str]) -> int | None:
    """The demo's exit code when `argv` (without the program name) is a `demo` or `help demo` command, else None."""
    i = _command_index(argv)
    if i is not None:
        rest = [*argv[:i], *argv[i + 1 :]]
    else:
        j = _help_index(argv)
        if j is None:
            return None
        rest = [*argv[:j], "--help"]
    from queuetruth.demo.cli import main

    return main(rest)


__all__ = ["maybe_run"]
