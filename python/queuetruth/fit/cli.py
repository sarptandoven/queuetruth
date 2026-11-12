"""`queuetruth fit`: the parameter that fixes a backtester's fill model, with its verdict.

    queuetruth fit --backtester hftbacktest|nautilus|databento (--data DATASET [--shadows FILE] | --live LOG
        --data DATASET) [--family F] [--split 70% | --test-after HH:MM | --train W --test W] [--out DIR]
        [--n-shadows N] [--json]

Runs `queuetruth.fit.fit` and prints the answer first: the parameter, its interval, how much it improves on the
library default on held-out data and the verdict. The snippet is written to `<out>/<backtester>.py` only when the
fit passes. `--json` (or `--format json`) prints the `qt.fit` document. A Python-only command:
`queuetruth.cli.main` runs it before the clap grammar, which lists it (crates/cli/gen_python_commands.py).

Exit codes (SPEC 0.2): 0 the fit passes and the snippet is written, 1 the fit is refused (the answer says why and
what would make it pass), 2 any error.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from typing import NoReturn

from queuetruth.errors import EXIT_ERROR, EXIT_GATE, EXIT_OK, QtError
from queuetruth.fit.run import BACKTESTERS, DEFAULT_N_SHADOWS, FAMILIES
from queuetruth.live.reconcile_cli import command_word

#: Family names as the CLI spells them (kebab case; `_` is accepted too, as in the clap grammar).
_ALL_FAMILIES = [f.replace("_", "-") for fams in FAMILIES.values() for f in fams]


def _kebab(s: str) -> str:
    return s.strip().replace("_", "-")


class _Parser(argparse.ArgumentParser):
    """argparse that raises `QT-USAGE-001` instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise QtError("QT-USAGE-001", f"{self.prog}: {message}", "queuetruth fit --help")


def parser() -> argparse.ArgumentParser:
    """The argument parser of `queuetruth fit`."""
    root = _Parser(prog="queuetruth", description="fit a backtester's fill model")
    root.add_argument("--format", "--output-format", dest="format", choices=["human", "json"], default="human")
    root.add_argument("--quiet", action="store_true")
    root.add_argument("--verbose", action="store_true")
    root.add_argument("--log-level", choices=["error", "warn", "info", "debug"], default="warn")
    root.add_argument("--threads", type=int, metavar="N")
    root.add_argument("--seed", type=int, metavar="N")
    sub = root.add_subparsers(dest="command", required=True, parser_class=_Parser)
    p = sub.add_parser(
        "fit",
        help="the parameter that fixes your backtester's fill model, with its verdict",
        description="The parameter that fixes your backtester's fill model: fitted on order-level data (exact "
        "queue) or on your live fills, compared with the library default on held-out data. The paste-ready "
        "snippet is written only when the fit beats the default; otherwise it says why and what would make it "
        "pass.",
    )
    p.add_argument("--backtester", required=True, choices=list(BACKTESTERS), help="the library whose model to fit")
    p.add_argument("--data", metavar="DIR", help="dataset: order-level (MBO), or the L2 or MBO tape of a live log")
    p.add_argument("--shadows", metavar="FILE", help="shadow orders to fit on (default: sampled from --data)")
    p.add_argument("--live", metavar="DIR", help="fit to your own fills: a canonical live log (with --data)")
    p.add_argument(
        "--family",
        type=_kebab,
        choices=_ALL_FAMILIES,
        help="the library's fill model (default: power3, nt_prob_touch or databento)",
    )
    p.add_argument(
        "--split",
        metavar="SHARE",
        help="train on the first SHARE of the data in time, hold out the rest (default 70%%)",
    )
    p.add_argument("--test-after", metavar="HH:MM", help="hold out the data after this venue-local time")
    p.add_argument("--train", metavar="WINDOW", help="expert: train window, dates or blocks:I-J/N (with --test)")
    p.add_argument("--test", metavar="WINDOW", help="expert: held-out window, dates or blocks:I-J/N (with --train)")
    p.add_argument("--out", default="qt-fit", metavar="DIR", help="output directory (default qt-fit)")
    p.add_argument(
        "--n-shadows",
        type=int,
        default=DEFAULT_N_SHADOWS,
        metavar="N",
        help=f"shadows to sample when --shadows is not given (default {DEFAULT_N_SHADOWS})",
    )
    p.add_argument("--json", action="store_true", help="print the qt.fit document as JSON (as --format json)")
    return root


def maybe_main(argv: list[str]) -> int | None:
    """The exit code when `argv` is `fit` (or `help fit`), else None (the main grammar handles it)."""
    word = command_word(argv)
    if word == "help":
        rest = [a for a in argv[argv.index("help") + 1 :] if not a.startswith("-")]
        return main(["fit", "--help"]) if rest[:1] == ["fit"] else None
    if word != "fit":
        return None
    return main(argv)


def main(argv: list[str]) -> int:
    """Runs `queuetruth fit`; returns the exit code."""
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
    try:
        from queuetruth.fit.run import fit

        res = fit(
            args.backtester,
            data=args.data,
            shadows=args.shadows,
            live=args.live,
            family=args.family,
            split=args.split,
            test_after=args.test_after,
            train=args.train,
            test=args.test,
            out=args.out,
            n_shadows=args.n_shadows,
            seed=args.seed or 0,
            threads=args.threads or 1,
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
        print(res.text())
    sys.stdout.flush()
    return EXIT_OK if res.passed else EXIT_GATE


def _emit_error(e: QtError, json_out: bool) -> int:
    if json_out:
        print(json.dumps(e.to_document(), ensure_ascii=False, default=str))
    else:
        print(str(e), file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return EXIT_ERROR
