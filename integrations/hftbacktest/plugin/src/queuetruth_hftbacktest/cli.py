"""`queuetruth-hftbacktest`: score hftbacktest against exact-queue truth and calibrate its queue model.

  queuetruth-hftbacktest prepare --data DATASET --orders ORDERS.parquet --out DIR [--step-ns N] [--window-ns N]
      exact truth for the orders, one hftbacktest event file per instrument-day and the replay script
  queuetruth-hftbacktest run DIR --python HBT_PYTHON --snippet ASSET.py [--out FILE] [--allow-stop]
      run the replay in the venv that has hftbacktest (or run the command `prepare` prints yourself)
  queuetruth-hftbacktest score DIR [--hbt-orders FILE] [--out DIR] [--horizons 1s,10s,60s,5m] [--format text|json]
      hftbacktest's fills against the exact queue, order by order and per horizon
  queuetruth-hftbacktest calibrate --data DATASET --train WINDOW [--test WINDOW] --out DIR [--orders ORDERS.parquet]
      [--families power3,power2,power] [--exchange partial|no_partial] [--force] [--format text|json]
      fit the ProbQueue n, check it out of sample, write the code to paste (`DIR/hbt_queue_model.py`)

Windows are `YYYY-MM-DD[,YYYY-MM-DD...]` or `blocks:LO-HI/N`. `--json` is an alias of `--format json`. Exit codes
as `queuetruth`: 0 success, 1 no calibrated model passes the trust check (`QT-CALIB-001`; the summary is still
printed and written), 2 any other error (an unexpected exception is `QT-ENGINE-001`). An error prints
`error: <code>: <message>` and `hint: ...` on stderr, or with `--format json` one `qt.error` document on stdout.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import warnings
from typing import Any

from queuetruth_hftbacktest import __version__
from queuetruth_hftbacktest.calibrate import FAMILIES, calibrate
from queuetruth_hftbacktest.errors import EXIT_ERROR, EXIT_OK, EXIT_REFUSED, QtHbtError
from queuetruth_hftbacktest.replay import HORIZONS_NS, prepare, run, score

_UNITS = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000, "m": 60_000_000_000, "min": 60_000_000_000}


def duration_ns(text: str) -> int:
    """`250ms`, `1s`, `5m`, `5min` or a bare number of ns."""
    t = text.strip().lower()
    for unit in sorted(_UNITS, key=len, reverse=True):
        if t.endswith(unit) and t[: -len(unit)].strip().replace(".", "", 1).isdigit():
            return round(float(t[: -len(unit)]) * _UNITS[unit])
    if t.isdigit():
        return int(t)
    raise QtHbtError("QT-CONFIG-001", f"duration {text!r}: expected e.g. 250ms, 1s, 5m or ns")


def _format_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=("text", "json"), default="text", help="output format (default: text)")
    p.add_argument("--json", dest="format", action="store_const", const="json", help="alias of --format json")


def cmd_prepare(a: argparse.Namespace) -> int:
    p = prepare(a.data, a.orders, a.out, step_ns=a.step_ns, window_ns=a.window_ns)
    doc = json.loads((p.out / "prepared.json").read_text(encoding="utf-8"))
    if a.format == "json":
        print(json.dumps(doc, sort_keys=True))
        return EXIT_OK
    print(f"{p.n_orders} orders ready for hftbacktest in {p.out}" + (f"; skipped {p.skipped}" if p.skipped else ""))
    print("run in the hftbacktest venv (your asset settings in a file defining `asset`):")
    print("  " + shlex.join(p.command("HBT_PYTHON", snippet="ASSET.py")))
    print(f"then: queuetruth-hftbacktest score {shlex.quote(str(p.out))}")
    return EXIT_OK


def cmd_run(a: argparse.Namespace) -> int:
    path = run(a.prepared, python=a.python, snippet=a.snippet, out=a.out, allow_stop=a.allow_stop)
    print(json.dumps({"out": str(path)}) if a.format == "json" else f"hftbacktest order states: {path}")
    return EXIT_OK


def cmd_score(a: argparse.Namespace) -> int:
    hs = tuple(duration_ns(x) for x in a.horizons.split(",")) if a.horizons else HORIZONS_NS
    res = score(a.prepared, a.hbt_orders, out=a.out, horizons_ns=hs, seed=a.seed)
    print(
        json.dumps(res.to_document(), sort_keys=True) if a.format == "json" else res.text(),
        end="\n" if a.format == "json" else "",
    )
    return EXIT_OK


def cmd_calibrate(a: argparse.Namespace) -> int:
    fams = [f.strip() for f in a.families.split(",") if f.strip()]
    res = calibrate(
        a.data,
        a.train,
        a.test,
        out=a.out,
        orders=a.orders,
        families=fams,
        exchange=a.exchange.replace("-", "_"),
        horizon_ns=duration_ns(a.horizon),
        n_orders=a.n_orders,
        seed=a.seed,
        threads=a.threads,
        force=a.force,
    )
    if a.format == "json":
        print(json.dumps(res.to_document(), sort_keys=True))
    else:
        print(res.text(), end="")
    return EXIT_OK if res.best is not None else EXIT_REFUSED


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="queuetruth-hftbacktest", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=f"queuetruth-hftbacktest {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="exact truth, hftbacktest event files and the replay script for your orders")
    p.add_argument("--data", required=True, help="queuetruth dataset directory (order-level data)")
    p.add_argument("--orders", required=True, help="the orders (qt.shadow_spec Parquet)")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--step-ns", type=duration_ns, default=10_000_000, help="replay step while an order is open")
    p.add_argument("--window-ns", type=duration_ns, default=None, help="cancel orders open this long (default 5m)")
    _format_args(p)
    p.set_defaults(fn=cmd_prepare)

    p = sub.add_parser("run", help="run the replay in the hftbacktest venv")
    p.add_argument("prepared", help="the prepare output directory")
    p.add_argument("--python", required=True, help="python of the venv that has hftbacktest 2.4.4")
    p.add_argument("--snippet", required=True, help="file defining `asset` (e.g. hbt_queue_model.py)")
    p.add_argument("--out", default=None, help="order states file (default DIR/hbt_orders.jsonl)")
    p.add_argument("--allow-stop", action="store_true", help="exit 0 when hftbacktest stops with an error code")
    _format_args(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("score", help="hftbacktest's fills against the exact queue")
    p.add_argument("prepared", help="the prepare output directory")
    p.add_argument("--hbt-orders", default=None, help="hftbacktest order states (default DIR/hbt_orders.jsonl)")
    p.add_argument("--out", default=None, help="output directory (default: the prepare directory)")
    p.add_argument("--horizons", default=None, help="comma-separated, e.g. 1s,10s,60s,5m (the default)")
    p.add_argument("--seed", type=int, default=0, help="bootstrap seed")
    _format_args(p)
    p.set_defaults(fn=cmd_score)

    p = sub.add_parser("calibrate", help="fit the ProbQueue n and write the code to paste")
    p.add_argument("--data", required=True, help="queuetruth dataset directory (order-level data)")
    p.add_argument("--train", required=True, help="fit window: dates or blocks:LO-HI/N")
    p.add_argument("--test", default=None, help="held-out window (default: all data, with a warning)")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--orders", default=None, help="orders to fit on (qt.shadow_spec); default: sampled")
    p.add_argument("--families", default=",".join(FAMILIES), help="power families to fit (default: all three)")
    p.add_argument("--exchange", choices=("partial", "no_partial", "no-partial"), default="partial")
    p.add_argument("--horizon", default="60s", help="horizon whose held-out log loss picks the winner")
    p.add_argument("--n-orders", type=int, default=2000, help="orders to sample without --orders")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--threads", type=int, default=1)
    p.add_argument("--force", action="store_true", help="write the best refused fit under NOT VALIDATED")
    _format_args(p)
    p.set_defaults(fn=cmd_calibrate)
    return ap


def _emit_error(e: QtHbtError, json_out: bool) -> int:
    """As `queuetruth.cli`: the `qt.error` document on stdout, or `error: <code>: <message>` on stderr."""
    if json_out:
        print(json.dumps(e.to_document(), ensure_ascii=False))
    else:
        print(f"error: {e}", file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return e.exit_code


def _as_hbt_error(e: BaseException) -> QtHbtError | None:
    """A queuetruth `QtError` (same code, message and hint), or None for anything else."""
    try:
        from queuetruth.errors import QtError
    except ImportError:  # pragma: no cover - queuetruth is a dependency
        return None
    if isinstance(e, QtError):
        details: dict[str, Any] = dict(getattr(e, "details", {}) or {})
        return QtHbtError(e.code, e.message, e.hint, details)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    json_out = getattr(a, "format", "text") == "json"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            code = int(a.fn(a))
        except QtHbtError as e:
            code = _emit_error(e, json_out)
        except KeyboardInterrupt:
            code = _emit_error(QtHbtError("QT-USAGE-001", "interrupted"), json_out)
        except Exception as e:  # an unexpected failure is a bug: QT-ENGINE-001, exit 2 (never 1, which means refused)
            qe = _as_hbt_error(e)
            code = _emit_error(qe or QtHbtError("QT-ENGINE-001", f"{type(e).__name__}: {e}"), json_out)
        sys.stdout.flush()
        seen: set[str] = set()
        for w in caught:
            msg = str(w.message)
            if msg not in seen and not issubclass(w.category, DeprecationWarning):
                seen.add(msg)
                print(f"warning: {msg}", file=sys.stderr)
    return code if code in (EXIT_OK, EXIT_REFUSED, EXIT_ERROR) else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
