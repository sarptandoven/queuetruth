"""Writes crates/cli/src/python_commands.rs: grammar rows for the command groups that run in Python only.

`queuetruth demo`, `forecast`, `ledger`, `pm` and the certificate commands (`certify`, `verify-cert`, `commit`,
`reveal`, `cert`) are parsed by their own argparse parsers, which `queuetruth.cli.main` runs before the clap grammar.
The rows written here put them into the clap grammar too (Implementation::Python), so `queuetruth --help`, the
command table, `queuetruth-core` and docs/reference/cli.md list them with the same flags, value names and help text.
The argparse parsers stay the source of truth; this script copies them, and `--check` fails when the copy is stale.

    uv run --frozen --no-sync python crates/cli/gen_python_commands.py           # write the file
    uv run --frozen --no-sync python crates/cli/gen_python_commands.py --check   # exit 1 if it would change

Global flags (`--format`, `--log-level`, `--threads`, `--seed`, `--quiet`, `--verbose`) are left out: the clap
grammar defines them for every command. Mutually exclusive argparse options are listed without the exclusion
(argparse enforces it when the command runs).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT = HERE / "src" / "python_commands.rs"
REPO = HERE.parent.parent

#: Flags of the clap root command (`global(true)`), never repeated on a leaf.
GLOBAL_OPTIONS = frozenset(
    {"--format", "--output-format", "--log-level", "--threads", "--seed", "--quiet", "--verbose"}
)

#: One-line descriptions of the top-level words whose argparse parser has only a long description.
ABOUT = {
    "demo": "fetch a public order-level sample and compare a default fill model with exact replay",
    "forecast": "forecast referee: hash-chained forecast ledger, proper scores, calibration and executable PnL",
    "ledger": "trial ledger: every run and variant tried, hash-chained",
    "pm": "prediction-market kit (Kalshi, Polymarket) on your own captures",
}


def parsers() -> list[tuple[list[str], argparse.ArgumentParser]]:
    """(path prefix, parser) of every Python-only group, in the order they are listed in `--help`."""
    sys.path.insert(0, str(REPO / "python"))
    from queuetruth.cert.cli import parser as cert_parser
    from queuetruth.demo.cli import parser as demo_parser
    from queuetruth.forecast.cli import build_parser as forecast_parser
    from queuetruth.ledger.cli import parser as ledger_parser
    from queuetruth.live.reconcile_cli import parser as reconcile_parser
    from queuetruth.pm.cli import parser as pm_parser

    # the certificate parser is a root parser whose subcommands are top-level words
    return [
        (["demo"], demo_parser()),
        (["forecast"], forecast_parser()),
        (["ledger"], ledger_parser()),
        (["pm"], pm_parser()),
        ([], cert_parser()),
        ([], reconcile_parser()),
    ]


def rs(s: str) -> str:
    """A Rust string literal."""
    bad = [c for c in s if ord(c) < 0x20 and c != "\n"]
    if bad:
        raise ValueError(f"control character in {s!r}")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def subparsers(p: argparse.ArgumentParser) -> argparse._SubParsersAction[Any] | None:
    acts = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
    return acts[0] if acts else None


def help_text(p: argparse.ArgumentParser, a: argparse.Action) -> str:
    """The option's help with `%(default)s`-style references expanded, whitespace collapsed."""
    text = p._get_formatter()._expand_help(a) if a.help else ""
    return " ".join(text.split())


def kind(a: argparse.Action, where: str) -> str:
    choices = list(a.choices) if a.choices is not None else None
    if choices is not None and not all(isinstance(c, str) for c in choices):
        raise ValueError(f"{where}: only string choices are supported, got {choices!r}")
    enum = "&[" + ", ".join(rs(c) for c in choices) + "]" if choices else None
    if isinstance(a, argparse._StoreTrueAction):
        return "Kind::Flag"
    if isinstance(a, argparse._StoreFalseAction):
        return "Kind::NoFlag"
    if a.nargs is not None:
        raise ValueError(f"{where}: nargs={a.nargs!r} on an option is not supported")
    if isinstance(a, argparse._AppendAction):
        if a.type not in (None, str):
            raise ValueError(f"{where}: only string values are supported on a repeatable option")
        return f"Kind::EnumAppend({enum})" if enum else "Kind::StrAppend"
    if type(a) is not argparse._StoreAction:
        raise ValueError(f"{where}: unsupported argparse action {type(a).__name__}")
    if enum:
        return f"Kind::Enum({enum})"
    return {int: "Kind::Int", float: "Kind::Num"}.get(a.type, "Kind::Str")  # type: ignore[arg-type]


def flag_rows(p: argparse.ArgumentParser, path: list[str]) -> Iterator[str]:
    where = "queuetruth " + " ".join(path)
    for a in p._actions:
        if isinstance(a, argparse._HelpAction) or not a.option_strings or a.help == argparse.SUPPRESS:
            continue
        if set(a.option_strings) & GLOBAL_OPTIONS:
            continue
        longs = [o for o in a.option_strings if o.startswith("--")]
        shorts = [o for o in a.option_strings if not o.startswith("--")]
        if len(longs) != 1 or len(shorts) > 1 or any(len(s) != 2 for s in shorts):
            raise ValueError(f"{where}: unsupported option strings {a.option_strings}")
        k = kind(a, f"{where} {longs[0]}")
        takes_value = k not in ("Kind::Flag", "Kind::NoFlag")
        value_name = (a.metavar or a.dest.upper()) if takes_value and a.choices is None else None
        if isinstance(value_name, tuple):
            raise ValueError(f"{where} {longs[0]}: tuple metavar")
        short = "Some('" + shorts[0][1] + "')" if shorts else "None"
        meta = "Some(" + rs(value_name) + ")" if value_name else "None"
        yield (
            "Flag { "
            f"long: {rs(longs[0][2:])}, kind: {k}, to: {rs('_cli.' + a.dest)}, required: {str(a.required).lower()}, "
            f"help: {rs(help_text(p, a))}, short: {short}, value_name: {meta} }}"
        )


def positionals(p: argparse.ArgumentParser) -> Iterator[argparse.Action]:
    return (a for a in p._actions if not a.option_strings and not isinstance(a, argparse._SubParsersAction))


def positional_name(a: argparse.Action) -> str:
    name = a.metavar or a.dest
    if not isinstance(name, str):
        raise ValueError(f"{a.dest}: tuple metavar")
    return name + "..." if a.nargs in ("+", "*") else name


def positional_rows(p: argparse.ArgumentParser, path: list[str]) -> Iterator[str]:
    for a in positionals(p):
        if a.nargs not in (None, "?", "+", "*"):
            raise ValueError(f"queuetruth {' '.join(path)} {a.dest}: nargs={a.nargs!r} is not supported")
        yield f"({rs(positional_name(a))}, {rs(a.dest)}, {str(a.nargs in (None, '+')).lower()})"


def walk(
    p: argparse.ArgumentParser, path: list[str], about: str | None, emit: Callable[[list[str], str, Any], None]
) -> None:
    sub = subparsers(p)
    if path:
        emit(path, about or ABOUT.get(path[0], ""), p)
    if sub is None:
        return
    helps = {c.dest: c.help for c in sub._choices_actions}
    for name, child in sub.choices.items():
        walk(child, [*path, name], helps.get(name), emit)


def generate() -> str:
    groups: list[tuple[str, str]] = []
    long_about: list[tuple[list[str], str]] = []
    pos_help: list[tuple[list[str], str, str]] = []
    cmds: list[str] = []

    def emit(path: list[str], about: str, p: argparse.ArgumentParser) -> None:
        if len(path) > 2:
            raise ValueError(f"queuetruth {' '.join(path)}: the grammar has at most two command words")
        if not about:
            raise ValueError(f"queuetruth {' '.join(path)}: no one-line description (add it to ABOUT)")
        desc = " ".join((p.description or "").split())
        if desc and desc != about:
            long_about.append((path, desc))
        if subparsers(p) is not None:
            groups.append((path[0], about))
            return
        for a in positionals(p):
            if a.help and a.help != argparse.SUPPRESS:
                pos_help.append((path, positional_name(a), help_text(p, a)))
        flags = "".join(f"            {row},\n" for row in flag_rows(p, path))
        pos = ", ".join(positional_rows(p, path))
        cmds.append(
            "    Cmd {\n"
            f"        path: &[{', '.join(rs(w) for w in path)}],\n"
            "        imp: Implementation::Python,\n"
            f"        about: {rs(about)},\n"
            + (f"        flags: &[\n{flags}        ],\n" if flags else "        flags: &[],\n")
            + f"        positional: &[{pos}],\n"
            "        globals: Globals::None,\n"
            "        hidden: false,\n"
            "    },\n"
        )

    for prefix, p in parsers():
        walk(p, prefix, None, emit)
    head = (
        "//! Generated by crates/cli/gen_python_commands.py from the argparse parsers of the Python-only command\n"
        "//! groups (demo, forecast, ledger, pm, certify, verify-cert, commit, reveal, cert). Do not edit by hand;\n"
        "//! rerun the script. `queuetruth.cli.main` runs these commands before the clap grammar; the rows make them\n"
        "//! part of `--help`, the command table and the reference docs. Flags map to `_cli.<dest>`.\n\n"
        "use crate::Implementation;\n"
        "use crate::grammar::{Cmd, Flag, Globals, Kind};\n\n"
    )
    g = "".join(f"    ({rs(n)}, {rs(a)}),\n" for n, a in groups)
    la = "".join(f"    (&[{', '.join(rs(w) for w in path)}], {rs(d)}),\n" for path, d in long_about)
    ph = "".join(f"    (&[{', '.join(rs(w) for w in path)}], {rs(n)}, {rs(h)}),\n" for path, n, h in pos_help)
    return (
        head
        + "/// Group descriptions of the two-level Python-only commands.\n#[rustfmt::skip]\n"
        + f"pub(crate) const PYTHON_GROUPS: &[(&str, &str)] = &[\n{g}];\n\n"
        + "/// Long descriptions (`--help`) by command path.\n#[rustfmt::skip]\n"
        + f"pub(crate) const PYTHON_LONG_ABOUT: &[(&[&str], &str)] = &[\n{la}];\n\n"
        + "/// Help of positional arguments: (command path, argument name, help).\n#[rustfmt::skip]\n"
        + f"pub(crate) const PYTHON_POSITIONAL_HELP: &[(&[&str], &str, &str)] = &[\n{ph}];\n\n"
        + "/// The Python-only commands, after `grammar::COMMANDS` in the command table.\n#[rustfmt::skip]\n"
        + f"pub(crate) const PYTHON_COMMANDS: &[Cmd] = &[\n{''.join(cmds)}];\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the file is out of date (writes nothing)")
    a = ap.parse_args(argv)
    text = generate()
    current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    if a.check:
        if text != current:
            print(f"{OUT.relative_to(REPO)} is stale; run python crates/cli/gen_python_commands.py", file=sys.stderr)
            return 1
        return 0
    if text != current:
        OUT.write_text(text, encoding="utf-8")
        print(f"wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
