"""`queuetruth reconcile`: a trading bot's simulator arm against its live (or dry-run) run, order by order.

    queuetruth reconcile --kind freqtrade|hummingbot --sim PATH [--sim PATH ...] --live PATH [--live PATH ...]
        --out DIR [--account NAME] [--instrument-map FILE] [--window DURATION] [--price-tol-bps X]
        [--time-tol DURATION] [--no-score] [--arms-as-given] [--option KEY=VALUE ...] [--max-rows N]

Runs `reconcile_freqtrade` / `reconcile_hummingbot` (`queuetruth.live.importers`): writes `<out>/live` and `<out>/sim`
(canonical live logs), `<out>/score` (`queuetruth score` with the simulator arm) and `<out>/reconcile.json`
(`qt.bot_reconcile`) with `reconcile_orders.csv`, and prints the report answer first (`--format json`: the
`qt.bot_reconcile` document). Options are the importer options of the guide
(docs/how-to/reconcile-freqtrade-hummingbot.md), e.g. `--option timeframe=5m`, `--option dry_run=true`. A Python-only
group: `queuetruth.cli.main` runs it before the clap grammar, which lists it (crates/cli/gen_python_commands.py).

Exit codes (SPEC 0.2): 0 success, 2 any error.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from typing import Any, NoReturn

from queuetruth.errors import EXIT_ERROR, EXIT_OK, QtError

#: Global flags of the main grammar that take a value (skipped when looking for the command word).
_VALUE_FLAGS = ("--format", "--output-format", "--log-level", "--threads", "--seed")


class _Parser(argparse.ArgumentParser):
    """argparse that raises `QT-USAGE-001` instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise QtError("QT-USAGE-001", f"{self.prog}: {message}", "queuetruth reconcile --help")


def parser() -> argparse.ArgumentParser:
    """The argument parser of `queuetruth reconcile`."""
    root = _Parser(prog="queuetruth", description="trading-bot reconciliation")
    root.add_argument("--format", "--output-format", dest="format", choices=["human", "json"], default="human")
    root.add_argument("--quiet", action="store_true")
    root.add_argument("--verbose", action="store_true")
    root.add_argument("--log-level", choices=["error", "warn", "info", "debug"], default="warn")
    root.add_argument("--threads", type=int, metavar="N")
    root.add_argument("--seed", type=int, metavar="N")
    sub = root.add_subparsers(dest="command", required=True, parser_class=_Parser)
    p = sub.add_parser(
        "reconcile",
        help="a Freqtrade or Hummingbot simulator arm against its live run, order by order",
        description="A trading bot's simulator arm (Freqtrade backtest or dry run, Hummingbot paper bot) against its "
        "live run: paired orders, why each differs, and the sim-vs-live scorecard.",
    )
    p.add_argument("--kind", required=True, choices=["freqtrade", "hummingbot"], help="the bot")
    p.add_argument(
        "--sim",
        action="append",
        required=True,
        metavar="PATH",
        help="simulator arm: backtest result, dry-run database and logs, or paper-bot database (repeatable)",
    )
    p.add_argument(
        "--live",
        action="append",
        required=True,
        metavar="PATH",
        help="live arm: the bot's database, logs or trades CSV (repeatable)",
    )
    p.add_argument("--out", required=True, metavar="DIR", help="output directory")
    p.add_argument("--account", default="main", help="account label of both logs (default main)")
    p.add_argument("--instrument-map", metavar="FILE", help="instrument_map.csv (default: generated, with a warning)")
    p.add_argument(
        "--window",
        metavar="DURATION",
        help="scorer evaluation horizon, e.g. 1h (default 1 day freqtrade, 60 s hummingbot)",
    )
    p.add_argument(
        "--price-tol-bps", type=float, default=0.5, metavar="X", help="price difference that counts (default 0.5 bps)"
    )
    p.add_argument("--time-tol", metavar="DURATION", help="timing difference that counts, e.g. 5s")
    p.add_argument("--no-score", action="store_true", help="pair and explain only; do not run the scorer")
    p.add_argument("--arms-as-given", action="store_true", help="do not refuse a bot simulator arm against a dry run")
    p.add_argument(
        "--option",
        action="append",
        metavar="KEY=VALUE",
        help="importer option, e.g. timeframe=5m, strategy=NAME, dry_run=true, log_tz=Europe/Berlin (repeatable)",
    )
    p.add_argument("--max-rows", type=int, default=50, metavar="N", help="order lines printed (default 50)")
    return root


def command_word(argv: list[str]) -> str | None:
    """The first positional word of `argv` (skipping global flags and their values)."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUE_FLAGS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def _options(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        k, v = k.strip(), v.strip()
        if not sep or not k:
            raise QtError("QT-USAGE-001", f"--option {it!r}: expected KEY=VALUE")
        if k in out:
            raise QtError("QT-USAGE-001", f"--option {k}: given twice")
        out[k] = {"true": True, "false": False}.get(v.lower(), v)
    return out


def run(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """(the `qt.bot_reconcile` document, human text)."""
    from queuetruth.pm.cli import duration_ns

    opts = _options(args.option)
    kw: dict[str, Any] = {
        "account": args.account,
        "instrument_map": args.instrument_map,
        "price_tol_bps": args.price_tol_bps,
        "score": not args.no_score,
        "arms_as_given": args.arms_as_given,
        **opts,
    }
    if args.window:
        kw["window_ns"] = duration_ns(args.window)
    if args.time_tol:
        kw["time_tol_ns"] = duration_ns(args.time_tol)
    if args.kind == "freqtrade":
        from queuetruth.live.importers.freqtrade import reconcile_freqtrade as reconcile
    else:
        from queuetruth.live.importers.hummingbot import reconcile_hummingbot as reconcile
    from queuetruth.live.importers.freqtrade import render_reconcile

    report = reconcile(args.sim, args.live, args.out, **kw)
    doc = report.model_dump(mode="json", by_alias=True)
    text = render_reconcile(report, max_rows=args.max_rows)
    if len(report.orders) > args.max_rows:
        text += f"\n... {len(report.orders) - args.max_rows} more orders in {args.out}/reconcile_orders.csv"
    return doc, text


def maybe_main(argv: list[str]) -> int | None:
    """The exit code when `argv` is `reconcile` (or `help reconcile`), else None (the main grammar handles it)."""
    word = command_word(argv)
    if word == "help":
        rest = [a for a in argv[argv.index("help") + 1 :] if not a.startswith("-")]
        return main(["reconcile", "--help"]) if rest[:1] == ["reconcile"] else None
    if word != "reconcile":
        return None
    return main(argv)


def main(argv: list[str]) -> int:
    """Runs `queuetruth reconcile`; returns the exit code."""
    json_out = any(a in ("--format=json", "--output-format=json") for a in argv) or any(
        a in ("--format", "--output-format") and b == "json" for a, b in itertools.pairwise(argv)
    )
    try:
        args = parser().parse_args(argv)
    except QtError as e:
        return _emit_error(e, json_out)
    except SystemExit as e:  # --help
        return int(e.code or 0)
    json_out = args.format == "json"
    try:
        doc, text = run(args)
    except QtError as e:
        return _emit_error(e, json_out)
    except KeyboardInterrupt:
        return _emit_error(QtError("QT-USAGE-001", "interrupted"), json_out)
    except Exception as e:  # every unexpected failure is QT-ENGINE-001 with exit code 2
        return _emit_error(QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}"), json_out)
    if json_out:
        print(json.dumps(doc, ensure_ascii=False, allow_nan=False))
    else:
        print(text)
    sys.stdout.flush()
    if not args.quiet:
        for w in doc.get("warnings") or []:
            print(f"warning: {w}", file=sys.stderr)
    return EXIT_OK


def _emit_error(e: QtError, json_out: bool) -> int:
    if json_out:
        print(json.dumps(e.to_document(), ensure_ascii=False, default=str))
    else:
        print(str(e), file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return EXIT_ERROR
