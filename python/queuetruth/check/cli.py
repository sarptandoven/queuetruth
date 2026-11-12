"""`queuetruth check`: how wrong is a backtest, in one command.

    queuetruth check BACKTEST [BACKTEST ...] --data DATASET [--backtester NAME] [--instrument-map FILE]
        [--out DIR] [--window DURATION] [--markout DURATION] [--option KEY=VALUE ...] [--json] [--html]
    queuetruth check BACKTEST [BACKTEST ...] [--tier T] [--bar 1m|5m|1h] [--atlas DIR_OR_URL] [--yes] [--refresh]

Runs `queuetruth.check.check` and prints the answer first, then the order-by-order table, the most overfilled
symbols and `queuetruth why` commands for a few phantom fills. Without `--data` it estimates from the atlas touch
tables how many of a bar backtest's fills the real queue would give (`queuetruth.check.estimate`); the atlas is
downloaded only with consent (a prompt on a terminal, or `--yes`), as for `queuetruth atlas`. `--json` (or
`--format json`) prints the `qt.check` (or `qt.check_estimate`) document instead; `--html` also writes
`<out>/check.html`. Notes (importer and scorer caveats) go to stderr with
`--verbose`. A Python-only command: `queuetruth.cli.main` runs it before the clap grammar, which lists it
(crates/cli/gen_python_commands.py).

Exit codes (SPEC 0.2): 0 success, 2 any error.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import NoReturn

from queuetruth.check.detect import BACKTESTERS
from queuetruth.check.run import DEFAULT_MARKOUT, DEFAULT_WINDOW
from queuetruth.errors import EXIT_ERROR, EXIT_OK, QtError
from queuetruth.live.reconcile_cli import _options, command_word


class _Parser(argparse.ArgumentParser):
    """argparse that raises `QT-USAGE-001` instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise QtError("QT-USAGE-001", f"{self.prog}: {message}", "queuetruth check --help")


def parser() -> argparse.ArgumentParser:
    """The argument parser of `queuetruth check`."""
    root = _Parser(prog="queuetruth", description="backtest check")
    root.add_argument("--format", "--output-format", dest="format", choices=["human", "json"], default="human")
    root.add_argument("--quiet", action="store_true")
    root.add_argument("--verbose", action="store_true")
    root.add_argument("--log-level", choices=["error", "warn", "info", "debug"], default="warn")
    root.add_argument("--threads", type=int, metavar="N")
    root.add_argument("--seed", type=int, metavar="N")
    sub = root.add_subparsers(dest="command", required=True, parser_class=_Parser)
    p = sub.add_parser(
        "check",
        help="how wrong a backtest is: its own limit orders replayed in the real queue",
        description="How wrong is my backtest: replays the backtest's own resting limit orders (hftbacktest, "
        "NautilusTrader, Backtrader, VectorBT, LEAN or Freqtrade output) in the exact queue of an order-level "
        "dataset and compares them order by order: fills, phantom fills, time to fill and the P&L gap. Without "
        "--data, estimates from the Fill-Realism Atlas touch tables how many of a bar backtest's fills the real "
        "queue would give, and how much they overstate P&L.",
    )
    p.add_argument("backtest", nargs="+", metavar="BACKTEST", help="the backtester's output file(s) of one run")
    p.add_argument(
        "--data",
        metavar="DIR",
        help="order-level (MBO) dataset covering the backtest's days (without it: the atlas estimate)",
    )
    p.add_argument("--backtester", choices=BACKTESTERS, help="the backtester (default: read from the file)")
    p.add_argument(
        "--instrument-map",
        metavar="FILE",
        help="instrument_map.csv (default: built from the dataset's symbols; needed only when they do not match)",
    )
    p.add_argument("--out", default="qt-check", metavar="DIR", help="output directory (default qt-check)")
    p.add_argument(
        "--window",
        default=DEFAULT_WINDOW,
        metavar="DURATION",
        help=f"how long after entry a fill counts, e.g. 10min (default {DEFAULT_WINDOW})",
    )
    p.add_argument(
        "--markout",
        default=DEFAULT_MARKOUT,
        metavar="DURATION",
        help=f"P&L horizon: fills marked to the mid this long after (default {DEFAULT_MARKOUT})",
    )
    p.add_argument(
        "--option",
        action="append",
        metavar="KEY=VALUE",
        help="importer option, e.g. timeframe=5m, data_tz=America/New_York, column=LABEL (repeatable)",
    )
    p.add_argument(
        "--tier",
        metavar="TIER",
        help="without --data: liquidity tier of symbols the atlas does not hold, T1 to T5 or ALL",
    )
    p.add_argument("--bar", metavar="BAR", help="without --data: the backtest's bar length, 1m, 5m or 1h")
    p.add_argument(
        "--atlas",
        metavar="DIR_OR_URL",
        help="without --data: a site or atlas/v1 directory, or a data API URL (default: the public atlas, cached)",
    )
    p.add_argument("--yes", "-y", action="store_true", help="without --data: download the atlas without asking")
    p.add_argument("--refresh", action="store_true", help="without --data: download the newest atlas manifest")
    p.add_argument("--json", action="store_true", help="print the full summary as JSON (as --format json)")
    p.add_argument("--html", action="store_true", help="also write a self-contained report to <out>/check.html")
    return root


def maybe_main(argv: list[str]) -> int | None:
    """The exit code when `argv` is `check` (or `help check`), else None (the main grammar handles it)."""
    word = command_word(argv)
    if word == "help":
        rest = [a for a in argv[argv.index("help") + 1 :] if not a.startswith("-")]
        return main(["check", "--help"]) if rest[:1] == ["check"] else None
    if word != "check":
        return None
    return main(argv)


def main(argv: list[str]) -> int:
    """Runs `queuetruth check`; returns the exit code."""
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
    from queuetruth.atlas import confirm_on_terminal

    interactive = sys.stdin.isatty() and sys.stderr.isatty() and not json_out
    if args.data is not None:
        given = [f for f, v in (("--tier", args.tier), ("--bar", args.bar), ("--atlas", args.atlas)) if v]
        if given:
            return _emit_error(
                QtError("QT-USAGE-001", f"check: {', '.join(given)} apply only without --data (the atlas estimate)"),
                json_out,
            )
    try:
        from queuetruth.check.run import check

        res = check(
            args.backtest,
            args.data,
            backtester=args.backtester,
            instrument_map=args.instrument_map,
            out=args.out,
            window=args.window,
            markout=args.markout,
            options=_options(args.option),
            tier=args.tier,
            bar=args.bar,
            atlas=args.atlas,
            yes=args.yes,
            confirm=confirm_on_terminal(interactive, "check", "--atlas DIR"),
            refresh=args.refresh,
            seed=args.seed,
        )
        if args.html:
            path = Path(args.out) / "check.html"
            path.write_text(res.to_html(), encoding="utf-8")
            res.doc["files"]["html"] = str(path)
            (Path(args.out) / "check.json").write_text(
                json.dumps(res.doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            )
    except QtError as e:
        return _emit_error(e, json_out)
    except KeyboardInterrupt:
        return _emit_error(QtError("QT-USAGE-001", "interrupted"), json_out)
    except Exception as e:  # every unexpected failure is QT-ENGINE-001 with exit code 2
        return _emit_error(QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}"), json_out)
    if json_out:
        print(json.dumps(res.doc, ensure_ascii=False, allow_nan=False))
    else:
        text = res.text()
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
            print(f"{len(notes)} note(s) in {res.doc['files']['check']} (--verbose prints them)", file=sys.stderr)
    return EXIT_OK


def _emit_error(e: QtError, json_out: bool) -> int:
    if json_out:
        print(json.dumps(e.to_document(), ensure_ascii=False, default=str))
    else:
        print(str(e), file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return EXIT_ERROR
