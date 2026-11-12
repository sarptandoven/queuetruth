"""`queuetruth compare`: my backtest against my live run, order by order.

    queuetruth compare --backtest PATH [--backtest PATH ...] --live PATH [--live PATH ...] [--data DIR]
        [--backtester NAME] [--venue V] [--out DIR] [--instrument-map FILE] [--window DURATION] [--account NAME]
        [--option KEY=VALUE ...] [--max-rows N] [--json] [--html]
        [--watch [--interval DURATION] | --once] [--alert-pp PP] [--alert-ticks TICKS] [--alert-pnl AMOUNT]

Runs `queuetruth.compare.compare` and prints the answer first, then the orders that differ. `--json` (or `--format
json`) prints the `qt.compare` document instead; `--html` also writes `<out>/compare.html`. Notes (importer and
scorer caveats) go to stderr with `--verbose`. A Python-only command: `queuetruth.cli.main` runs it before the clap
grammar, which lists it (crates/cli/gen_python_commands.py).

`--watch` runs the comparison again whenever new live records arrive (`queuetruth.compare.watch`) and prints one
line per check with the drift since the baseline window; `--once` runs a single check (cron, CI).

Exit codes (SPEC 0.2): 0 success, 2 any error. With `--watch` or `--once` (as `fit`): 0 no alert, 1 alert, 2 error.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import NoReturn

from queuetruth import backtesters
from queuetruth.compare.detect import BOTS, VENUES
from queuetruth.errors import EXIT_ERROR, EXIT_OK, QtError
from queuetruth.live.reconcile_cli import _options, command_word


class _Parser(argparse.ArgumentParser):
    """argparse that raises `QT-USAGE-001` instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise QtError("QT-USAGE-001", f"{self.prog}: {message}", "queuetruth compare --help")


def parser() -> argparse.ArgumentParser:
    """The argument parser of `queuetruth compare`."""
    root = _Parser(prog="queuetruth", description="backtest against live")
    root.add_argument("--format", "--output-format", dest="format", choices=["human", "json"], default="human")
    root.add_argument("--quiet", action="store_true")
    root.add_argument("--verbose", action="store_true")
    root.add_argument("--log-level", choices=["error", "warn", "info", "debug"], default="warn")
    root.add_argument("--threads", type=int, metavar="N")
    root.add_argument("--seed", type=int, metavar="N")
    sub = root.add_subparsers(dest="command", required=True, parser_class=_Parser)
    p = sub.add_parser(
        "compare",
        help="my backtest against my live run, order by order",
        description="My backtest against my live run, order by order: how many of the backtest's fills happened "
        "live, the fills only one side made and why, the P&L gap, time to fill, markouts and latency. Reads the "
        "backtester and the kind of live run from the files.",
    )
    p.add_argument(
        "--backtest",
        action="append",
        required=True,
        metavar="PATH",
        help="the backtester's output file(s) of one run (repeatable)",
    )
    p.add_argument(
        "--live",
        action="append",
        required=True,
        metavar="PATH",
        help="the live run: a canonical live-log directory, the bot's database and log, or a venue export (repeatable)",
    )
    p.add_argument(
        "--data", metavar="DIR", help="order-level dataset of the same days: markouts, P&L per order, exact queue"
    )
    p.add_argument(
        "--backtester", choices=backtesters.Names(), help="the backtester (default: read from the backtest file)"
    )
    p.add_argument(
        "--venue", choices=(*VENUES, *BOTS), help="the live run's venue or bot (default: read from the files)"
    )
    p.add_argument("--out", default="qt-compare", metavar="DIR", help="output directory (default qt-compare)")
    p.add_argument(
        "--instrument-map",
        metavar="FILE",
        help="instrument_map.csv (default: none needed; with --data built from the dataset's symbols)",
    )
    p.add_argument(
        "--window",
        metavar="DURATION",
        help="how long after entry a fill counts, e.g. 1h (default: 1 day for bots and bar backtesters, 60s else)",
    )
    p.add_argument("--account", metavar="NAME", help="account label of the logs")
    p.add_argument(
        "--option",
        action="append",
        metavar="KEY=VALUE",
        help="backtest importer option, e.g. timeframe=1h, strategy=NAME (repeatable)",
    )
    p.add_argument("--max-rows", type=int, default=10, metavar="N", help="differing orders printed (default 10)")
    p.add_argument("--json", action="store_true", help="print the full summary as JSON (as --format json)")
    p.add_argument("--html", action="store_true", help="also write a self-contained report to <out>/compare.html")
    w = p.add_argument_group("watch for drift (no network, no notifications: lines and the exit code)")
    w.add_argument(
        "--watch",
        action="store_true",
        help="compare again whenever new live records arrive; print the drift since the baseline window (state in "
        "<out>/watch.json); exit 1 after an alert",
    )
    w.add_argument("--once", action="store_true", help="one watch check, then exit 0 (no alert), 1 (alert) or 2")
    w.add_argument("--interval", metavar="DURATION", default="60s", help="how often --watch checks (default 60s)")
    w.add_argument(
        "--alert-pp",
        type=float,
        default=2.0,
        metavar="PP",
        help="alert when the fill-share gap moved at least this many percentage points (default 2)",
    )
    w.add_argument(
        "--alert-ticks",
        type=float,
        default=0.25,
        metavar="TICKS",
        help="alert when the 1s markout gap moved at least this many ticks (default 0.25)",
    )
    w.add_argument(
        "--alert-pnl",
        type=float,
        metavar="AMOUNT",
        help="alert when the P&L gap per order moved at least this much, in the P&L currency (default: no alert)",
    )
    return root


def maybe_main(argv: list[str]) -> int | None:
    """The exit code when `argv` is `compare` (or `help compare`), else None (the main grammar handles it)."""
    word = command_word(argv)
    if word == "help":
        rest = [a for a in argv[argv.index("help") + 1 :] if not a.startswith("-")]
        return main(["compare", "--help"]) if rest[:1] == ["compare"] else None
    if word != "compare":
        return None
    return main(argv)


def main(argv: list[str]) -> int:
    """Runs `queuetruth compare`; returns the exit code."""
    json_out = "--json" in argv or any(a in ("--format=json", "--output-format=json") for a in argv)
    json_out = json_out or any(
        a in ("--format", "--output-format") and b == "json" for a, b in itertools.pairwise(argv)
    )
    try:
        args = parser().parse_args(argv)
    except QtError as e:
        return _emit_error(e, json_out)
    except SystemExit as e:  # --help
        return int(e.code or 0)
    json_out = args.format == "json" or args.json
    if args.watch or args.once:
        return _watch(args, json_out)
    try:
        from queuetruth.compare.run import compare, write_doc

        res = compare(
            args.backtest,
            args.live,
            data=args.data,
            backtester=args.backtester,
            venue=args.venue,
            out=args.out,
            instrument_map=args.instrument_map,
            window=args.window,
            account=args.account,
            options=_options(args.option),
        )
        if args.html:
            path = Path(args.out) / "compare.html"
            path.write_text(res.to_html(), encoding="utf-8")
            res.doc["files"]["html"] = str(path)
            write_doc(res.doc, Path(args.out))
    except QtError as e:
        return _emit_error(e, json_out)
    except KeyboardInterrupt:
        return _emit_error(QtError("QT-USAGE-001", "interrupted"), json_out)
    except Exception as e:  # every unexpected failure is QT-ENGINE-001 with exit code 2
        return _emit_error(QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}"), json_out)
    if json_out:
        print(json.dumps(res.doc, ensure_ascii=False, allow_nan=False, default=str))
    else:
        text = res.text(max_rows=args.max_rows)
        if args.html:
            text += f"\nReport: {res.doc['files']['html']}"
        print(text)
    sys.stdout.flush()
    notes = res.doc.get("notes") or []
    if not args.quiet and notes:
        if args.verbose:
            for w in notes:
                print(f"note: {w}", file=sys.stderr)
        else:
            print(f"{len(notes)} note(s) in {res.doc['files']['compare']} (--verbose prints them)", file=sys.stderr)
    return EXIT_OK


def _watch(args: argparse.Namespace, json_out: bool) -> int:
    """`--watch` / `--once`: `queuetruth.compare.watch.watch`, one printed line per check."""
    if json_out:
        return _emit_error(
            QtError("QT-USAGE-001", "compare --watch prints one line per check", "read <out>/watch.json instead"),
            True,
        )
    from queuetruth.compare.watch import watch

    def echo(line: str) -> None:
        print(line, flush=True)

    try:
        return watch(
            args.backtest,
            args.live,
            out=args.out,
            interval=args.interval,
            once=args.once,
            echo=echo,
            alert_pp=args.alert_pp,
            alert_ticks=args.alert_ticks,
            alert_pnl=args.alert_pnl,
            data=args.data,
            backtester=args.backtester,
            venue=args.venue,
            instrument_map=args.instrument_map,
            window=args.window,
            account=args.account,
            options=_options(args.option),
        )
    except QtError as e:
        return _emit_error(e, False)


def _emit_error(e: QtError, json_out: bool) -> int:
    if json_out:
        print(json.dumps(e.to_document(), ensure_ascii=False, default=str))
    else:
        print(str(e), file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return EXIT_ERROR
