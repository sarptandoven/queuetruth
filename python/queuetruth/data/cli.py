"""`queuetruth data`: bring order-level data in.

    queuetruth data add FILE [FILE ...] --out DATASET [--input-format F] [--venue V] [--date YYYY-MM-DD]
        [--symbols S,S] [--json]
    queuetruth data fetch iex --date YYYY-MM-DD --symbols S,S [--out DATASET] [--yes] [--dry-run] [--url URL] [--json]

`data add` prints the answer first (symbols, dates, records, data quality), then one line per session date with
its span in venue-local time, the checks and how each input was identified. `data fetch iex` streams a free IEX
DEEP+ day after the user agrees (a prompt on a terminal, or `--yes`), keeps the symbols asked for and adds them to
a dataset. `--json` (or the global `--format json`) prints the `qt.data_add` document instead. The global
`--format` is the output format; after `data`, `--format` with a source format name (`--format itch`) is read as
`--input-format`, the name `ingest` also accepts.

A Python-only command group: `queuetruth.cli.main` runs it before the clap grammar, which lists it
(crates/cli/gen_python_commands.py). Exit codes (SPEC 0.2): 0 success, 2 any error.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from typing import Any, NoReturn

from queuetruth.data.infer import FORMATS
from queuetruth.errors import EXIT_ERROR, EXIT_OK, QtError
from queuetruth.live.reconcile_cli import command_word

OUTPUT_FORMATS = ("human", "json")


class _Parser(argparse.ArgumentParser):
    """argparse that raises `QT-USAGE-001` instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise QtError("QT-USAGE-001", f"{self.prog}: {message}", "queuetruth data --help")


def parser() -> argparse.ArgumentParser:
    """The argument parser of `queuetruth data`."""
    root = _Parser(prog="queuetruth", description="data")
    root.add_argument("--format", "--output-format", dest="format", choices=list(OUTPUT_FORMATS), default="human")
    root.add_argument("--quiet", action="store_true")
    root.add_argument("--verbose", action="store_true")
    root.add_argument("--log-level", choices=["error", "warn", "info", "debug"], default="warn")
    root.add_argument("--threads", type=int, metavar="N")
    root.add_argument("--seed", type=int, metavar="N")
    sub = root.add_subparsers(dest="command", required=True, parser_class=_Parser)
    data = sub.add_parser(
        "data",
        help="bring order-level data in: add files to a dataset, or fetch a free IEX day",
        description="Bring order-level data in: add files (format, venue and date inferred, days merged, quality "
        "checked) or fetch a free IEX DEEP+ day.",
    )
    verbs = data.add_subparsers(dest="verb", required=True, parser_class=_Parser)
    add = verbs.add_parser(
        "add",
        help="ingest order-level files into one dataset, format, venue and date inferred",
        description="Ingests one or more order-level files (ITCH, IEX DEEP+, DBN, NYSE TAQ Integrated Feed, the crypto "
        "captures, Tardis L2) into one dataset: the format, venue and session date are read from the files, several "
        "days are merged, and the data-quality checks run. An existing dataset at --out gains the new days.",
    )
    add.add_argument("files", nargs="+", metavar="FILE", help="order-level files or capture directories")
    add.add_argument("--out", required=True, metavar="DIR", help="the dataset directory (created, or added to)")
    add.add_argument(
        "--input-format",
        choices=list(FORMATS),
        help="source format, when it cannot be read from the files",
    )
    add.add_argument("--venue", help="venue rule set, when the format does not decide it (nasdaq, cme, generic, ...)")
    add.add_argument("--date", metavar="YYYY-MM-DD", help="session date, when neither the name nor a manifest gives it")
    add.add_argument("--symbols", metavar="S,S", help="keep only these symbols (comma separated)")
    add.add_argument("--json", action="store_true", help="print the full summary as JSON (as --format json)")
    fetch = verbs.add_parser(
        "fetch",
        help="fetch a free public day into a dataset (iex: IEX DEEP+)",
        description="Streams a free public order-level day (iex: IEX DEEP+ from IEX's public bucket) after you agree, "
        "keeps only the symbols you ask for and adds them to a dataset, with IEX's attribution in its manifest. The "
        "day's file (several GB) is never stored.",
    )
    fetch.add_argument("source", choices=["iex"], metavar="SOURCE", help="the public source: iex")
    fetch.add_argument("--date", required=True, metavar="YYYY-MM-DD", help="the trading day")
    fetch.add_argument("--symbols", required=True, metavar="S,S", help="symbols to keep (comma separated)")
    fetch.add_argument(
        "--out", metavar="DIR", help="the dataset directory (default iex-<date>; an existing one gains the day)"
    )
    fetch.add_argument("--yes", "-y", action="store_true", help="download without asking")
    fetch.add_argument("--dry-run", action="store_true", help="show what would be downloaded and where, then stop")
    fetch.add_argument("--url", help="stream this URL instead of IEX's bucket (a mirror of the day's file)")
    fetch.add_argument("--json", action="store_true", help="print the full summary as JSON (as --format json)")
    return root


def normalize(argv: list[str]) -> list[str]:
    """`argv` with the global flags given after `data` (output `--format human|json`, `--quiet`, `--verbose`,
    `--log-level`, `--threads`, `--seed`) moved before it, and a source-format `--format X` after `data` turned into
    `--input-format X` (module docstring)."""
    front: list[str] = []
    rest: list[str] = []
    after = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if not after and a == "data":
            after = True
        name, eq, val = a.partition("=")
        if after and a in ("--quiet", "--verbose"):
            front.append(a)
            i += 1
            continue
        if after and name in ("--log-level", "--threads", "--seed"):
            front += argv[i : i + (1 if eq else 2)]
            i += 1 if eq else 2
            continue
        if after and name in ("--format", "--output-format"):
            if not eq:
                if i + 1 >= len(argv):
                    rest.append(a)
                    break
                val = argv[i + 1]
                i += 1
            if val in OUTPUT_FORMATS:
                front += ["--format", val]
            else:
                rest += ["--input-format", val.lower().replace("_", "-")]
            i += 1
            continue
        rest.append(a)
        i += 1
    return front + rest


def maybe_main(argv: list[str]) -> int | None:
    """The exit code when `argv` is `data ...` (or `help data`), else None (the main grammar handles it)."""
    word = command_word(argv)
    if word == "help":
        rest = [a for a in argv[argv.index("help") + 1 :] if not a.startswith("-")]
        return main(["data", *rest[1:], "--help"]) if rest[:1] == ["data"] else None
    if word != "data":
        return None
    return main(argv)


def _wants_json(argv: list[str]) -> bool:
    if "--json" in argv or any(a in ("--format=json", "--output-format=json") for a in argv):
        return True
    return any(a in ("--format", "--output-format") and b == "json" for a, b in itertools.pairwise(argv))


def _mb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1e6:,.1f} MB"


def _progress(quiet: bool) -> Any:
    if quiet:
        return None
    tty = sys.stderr.isatty()

    def show(d: dict[str, Any]) -> None:
        m = d.get("market_s")
        at = "-" if m is None else f"{int(m) // 3600:02d}:{int(m) % 3600 // 60:02d}:{int(m) % 60:02d}"
        rate = d["bytes"] / 1e6 / d["elapsed_s"] if d["elapsed_s"] > 0 else 0.0
        of = f" of {_mb(d['total'])}" if d.get("total") else ""
        msg = f"  {_mb(d['bytes'])}{of} ({rate:5.1f} MB/s), market time {at} ET, kept {d['kept']:,} messages"
        print(("\r" + msg) if tty else msg, end="" if tty else "\n", file=sys.stderr, flush=True)

    return show


def _confirm(yes: bool, interactive: bool) -> Any:
    from queuetruth.data.iex import plan_text

    def confirm(pl: Any) -> bool:
        if yes:
            return True
        if not interactive:
            raise QtError(
                "QT-USAGE-001",
                f"data fetch iex: needs to stream IEX's whole DEEP+ file for {pl.date} (several GB, never stored)",
                "rerun with --yes to confirm the download, or --dry-run to see the plan",
            )
        print(plan_text(pl), file=sys.stderr)
        print("Download now? [y/N] ", end="", file=sys.stderr, flush=True)
        return sys.stdin.readline().strip().lower() in ("y", "yes")

    return confirm


def main(argv: list[str]) -> int:
    """Runs `queuetruth data ...`; returns the exit code."""
    argv = normalize(argv)
    json_out = _wants_json(argv)
    try:
        args = parser().parse_args(argv)
    except QtError as e:
        return _emit_error(e, json_out)
    except SystemExit as e:  # --help
        return int(e.code or 0) if isinstance(e.code, int) or e.code is None else EXIT_ERROR
    json_out = args.format == "json" or args.json
    say = None if args.quiet else (lambda s: print(f"data: {s}", file=sys.stderr, flush=True))
    try:
        if args.verb == "add":
            from queuetruth.data.add import add

            res = add(
                args.files,
                args.out,
                format=args.input_format,
                venue=args.venue,
                date=args.date,
                symbols=args.symbols.split(",") if args.symbols else None,
                say=say,
            )
        else:
            from queuetruth.data.iex import fetch_iex, plan_fetch, plan_text

            if args.dry_run:
                pl = plan_fetch(args.date, args.symbols, args.out, url=args.url)
                print(json.dumps(pl.to_dict()) if json_out else plan_text(pl))
                return EXIT_OK
            interactive = sys.stdin.isatty() and sys.stderr.isatty() and not json_out
            prog = _progress(args.quiet)
            res = fetch_iex(
                args.date,
                args.symbols,
                args.out,
                confirm=_confirm(args.yes, interactive),
                progress=prog,
                say=say,
                url=args.url,
            )
            if prog is not None and sys.stderr.isatty():
                print(file=sys.stderr)
    except QtError as e:
        return _emit_error(e, json_out)
    except KeyboardInterrupt:
        return _emit_error(QtError("QT-USAGE-001", "interrupted"), json_out)
    except Exception as e:  # every unexpected failure is QT-ENGINE-001 with exit code 2
        return _emit_error(QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}"), json_out)
    if json_out:
        print(json.dumps(res.doc, ensure_ascii=False, allow_nan=False))
    else:
        print(res.text())
    return EXIT_OK


def _emit_error(e: QtError, json_out: bool) -> int:
    if json_out:
        print(json.dumps(e.to_document(), ensure_ascii=False, default=str))
    else:
        print(str(e), file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return EXIT_ERROR


__all__ = ["main", "maybe_main", "normalize", "parser"]
