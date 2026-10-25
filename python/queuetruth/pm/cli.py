"""`queuetruth pm fixtures | ingest | calibrate | score`: the prediction-market kit's command group.

The group is parsed here (argparse) rather than by the Rust command table, and `queuetruth.cli.main` hands it every
argument list whose first command word is `pm`. It honours the global flags of the main CLI (`--output-format` or
`--format` `human|json`, `--quiet`, `--verbose`, `--log-level`, `--seed`) before or after `pm`, prints exactly one JSON
document with `--format json` (the summary, or a `qt.error` document on exit code 2) and uses the same exit codes:
0 success, 2 any error (an interrupt is `QT-USAGE-001`). Logging and Python warnings are handled as in the main CLI
(`LEVEL message` on stderr; each warning printed as `warning: ...` after the answer). `--threads` is refused: the
kit runs in one thread. Durations take a unit (`500ms`, `1s`, `60s`, `5min`); a bare number is refused.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import warnings
from typing import Any

from queuetruth.errors import EXIT_OK, QtError

log = logging.getLogger("queuetruth.pm")

_GLOBAL_VALUE = {"--output-format", "--format", "--log-level", "--seed", "--threads"}
_GLOBAL_FLAG = {"--quiet", "--verbose"}
_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ns|us|ms|s|min|h)\s*$")
_UNIT = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000, "min": 60_000_000_000, "h": 3_600_000_000_000}


def duration_ns(text: str) -> int:
    """`500ms` -> 500000000; a number without a unit is `QT-USAGE-001`."""
    m = _DUR.match(text)
    if not m:
        raise QtError("QT-USAGE-001", f"duration {text!r} needs a unit: ns, us, ms, s, min or h (e.g. 500ms)")
    v = float(m.group(1)) * _UNIT[m.group(2)]
    if v != math.floor(v):
        raise QtError("QT-USAGE-001", f"duration {text!r} is not a whole number of nanoseconds")
    return int(v)


def _pairs(items: list[str] | None, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep or not k.strip() or not v.strip():
            raise QtError("QT-USAGE-001", f"--{what} {it!r}: expected KEY=VALUE")
        if k.strip() in out:
            raise QtError("QT-USAGE-001", f"--{what}: {k.strip()} given twice")
        out[k.strip()] = v.strip()
    return out


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # type: ignore[override]
        raise QtError("QT-USAGE-001", message)


def parser() -> argparse.ArgumentParser:
    """The `queuetruth pm` parser."""
    p = _Parser(
        prog="queuetruth pm",
        description=(
            "Prediction-market kit (Kalshi, Polymarket): your own captures -> books, queue calibration and "
            "reward-aware market-making PnL. Self-hosted use on your own data only; see `queuetruth.pm.terms`."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    f = sub.add_parser("fixtures", help="write the invented example captures of the walkthrough (no venue data)")
    f.add_argument("--out", required=True, metavar="DIR")
    i = sub.add_parser("ingest", help="captures -> L2 datasets, live log and tables")
    i.add_argument("--venue", required=True, choices=["kalshi", "polymarket"])
    i.add_argument("--in", dest="inputs", action="append", required=True, metavar="PATH", help="capture (repeat)")
    i.add_argument("--chain", action="append", metavar="PATH", help="Polymarket: Polygon log export (repeat)")
    i.add_argument("--out", required=True, metavar="DIR")
    i.add_argument("--account", default="default", help="account name in order keys (default: default)")
    i.add_argument(
        "--kalshi-yes-price",
        choices=["auto", "true", "false"],
        default="auto",
        help="Kalshi NO-level pricing of the orderbook channel (default: auto, from the captured subscribe)",
    )
    i.add_argument("--market", action="append", metavar="COND=YES,NO", help="Polymarket: YES and NO token of a market")
    i.add_argument("--instrument-id", action="append", metavar="SYMBOL=ID", help="instrument id of a symbol")
    i.add_argument("--match-window", metavar="DURATION", help="print-to-book-change window (default 1s / 2s)")
    i.add_argument("--own-match-window", metavar="DURATION", help="own-order-to-book-change window")
    i.add_argument("--exchange", action="append", metavar="ADDRESS", help="Polymarket: decode only this contract")
    c = sub.add_parser("calibrate", help="queue models against your queue positions and fills")
    c.add_argument("--pm", required=True, metavar="DIR", help="`pm ingest` output")
    c.add_argument("--out", required=True, metavar="DIR")
    c.add_argument("--families", default="power3,power2,power,log,log2", help="model families (comma separated)")
    c.add_argument("--grid", default="-4:4:33", metavar="LO:HI:N", help="grid of ln n (default -4:4:33)")
    c.add_argument("--train-fraction", type=float, default=0.5, help="share of orders (by entry time) that trains")
    c.add_argument("--loss", choices=["rmse", "mae"], default="rmse", help="fit and rank by this loss (default rmse)")
    c.add_argument("--sample-time", choices=["recv", "mid"], default="mid", help="queue sample clock (default mid)")
    c.add_argument(
        "--no-atomic",
        dest="atomic",
        action="store_false",
        help="do not fit the one-order-per-change (atomic) variants of the families",
    )
    c.add_argument("--bootstrap", type=int, default=500, metavar="B", help="bootstrap replicates (default 500)")
    s = sub.add_parser("score", help="reward-aware PnL band and sim versus live")
    s.add_argument("--pm", required=True, metavar="DIR", help="`pm ingest` output")
    s.add_argument("--out", required=True, metavar="DIR")
    s.add_argument("--quotes", metavar="PATH", help="hypothetical quotes (CSV / Parquet); default: your live orders")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--calibration", metavar="PATH", help="`pm calibrate` output (its fitted model)")
    g.add_argument("--model", metavar="ID", help="queue model id, e.g. power3[n=1.5] or risk_adverse")
    s.add_argument("--settle", action="append", metavar="SYMBOL=VALUE", help="YES settlement value in dollars")
    s.add_argument("--member", choices=["direct", "non-direct"], default="direct", help="Kalshi fee rounding")
    s.add_argument("--window", default="60s", metavar="DURATION", help="own orders without a cancel (default 60s)")
    s.add_argument("--markout-horizons", default="1s,10s,60s,5min", metavar="D,...")
    s.add_argument("--flat-taker", metavar="DOLLARS", help="Kalshi flat series: taker fee per contract")
    s.add_argument("--flat-maker", metavar="DOLLARS", help="Kalshi flat series: maker fee per contract")
    s.add_argument("--poly-multiplier", type=float, default=1.0, help="Polymarket in-game reward multiplier b")
    s.add_argument("--poly-daily-reward", metavar="PUSD", help="Polymarket daily liquidity reward of the market")
    s.add_argument("--poly-category", metavar="NAME", help="Polymarket fee category when metadata has no fees")
    s.add_argument("--actual-rewards", metavar="DOLLARS", help="your actual reward payout for the window")
    s.add_argument("--bootstrap", type=int, default=500, metavar="B", help="bootstrap replicates (default 500)")
    return p


def split_globals(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    """(global options, the remaining arguments) of a `queuetruth ...` argument list."""
    g: dict[str, Any] = {"format": "human", "quiet": False, "verbose": False, "log_level": "warn", "seed": None}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        name, eq, val = a.partition("=")
        if name in _GLOBAL_VALUE:
            if not eq:
                if i + 1 >= len(argv):
                    raise QtError("QT-USAGE-001", f"{name} needs a value")
                val = argv[i + 1]
                i += 1
            key = {"--output-format": "format", "--format": "format", "--log-level": "log_level"}.get(name, name[2:])
            g[key] = val
        elif a in _GLOBAL_FLAG:
            g[a[2:]] = True
        else:
            rest.append(a)
        i += 1
    if g["format"] not in ("human", "json"):
        raise QtError("QT-USAGE-001", f"--output-format {g['format']!r}: expected human or json")
    return g, rest


def is_pm(argv: list[str]) -> bool:
    """True when the first command word of a `queuetruth` argument list is `pm`."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.partition("=")[0] in _GLOBAL_VALUE:
            i += 1 if "=" in a else 2
            continue
        if a in _GLOBAL_FLAG:
            i += 1
            continue
        return a == "pm"
    return False


def _spec(ns: argparse.Namespace, g: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    seed = int(g["seed"]) if g.get("seed") is not None else 0
    if ns.cmd == "fixtures":
        return "fixtures", {"out": ns.out}
    if ns.cmd == "ingest":
        markets: dict[str, list[str]] = {}
        for k, v in _pairs(ns.market, "market").items():
            yes, sep, no = v.partition(",")
            if not sep or not yes or not no:
                raise QtError("QT-USAGE-001", f"--market {k}={v}: expected COND=YES,NO")
            markets[k] = [yes, no]
        ids = {}
        for k, v in _pairs(ns.instrument_id, "instrument-id").items():
            if not v.isdigit():
                raise QtError("QT-USAGE-001", f"--instrument-id {k}={v}: the id must be a non-negative integer")
            ids[k] = int(v)
        return "ingest", {
            "venue": ns.venue,
            "inputs": [{"path": p} for p in ns.inputs],
            "chain": [{"path": p} for p in ns.chain or []],
            "out": ns.out,
            "account": ns.account,
            "kalshi_yes_price": ns.kalshi_yes_price,
            "markets": markets,
            "instrument_ids": ids,
            "match_window_ns": duration_ns(ns.match_window) if ns.match_window else None,
            "own_match_window_ns": duration_ns(ns.own_match_window) if ns.own_match_window else None,
            "exchanges": ns.exchange,
        }
    if ns.cmd == "calibrate":
        parts = ns.grid.split(":")
        if len(parts) != 3:
            raise QtError("QT-USAGE-001", f"--grid {ns.grid!r}: expected LO:HI:N")
        try:
            lo, hi, n = float(parts[0]), float(parts[1]), int(parts[2])
        except ValueError:
            raise QtError("QT-USAGE-001", f"--grid {ns.grid!r}: expected numbers LO:HI:N") from None
        return "calibrate", {
            "pm": {"path": ns.pm},
            "out": ns.out,
            "families": [f.strip() for f in ns.families.split(",") if f.strip()],
            "grid_lo": lo,
            "grid_hi": hi,
            "grid_n": n,
            "train_fraction": ns.train_fraction,
            "sample_time": ns.sample_time,
            "atomic": ns.atomic,
            "loss": ns.loss,
            "bootstrap_b": ns.bootstrap,
            "seed": seed,
        }
    return "score", {
        "pm": {"path": ns.pm},
        "out": ns.out,
        "quotes": {"path": ns.quotes} if ns.quotes else None,
        "calibration": {"path": ns.calibration} if ns.calibration else None,
        "model": ns.model,
        "settle": _pairs(ns.settle, "settle"),
        "member": ns.member.replace("-", "_"),
        "window_ns": duration_ns(ns.window),
        "markout_horizons_ns": [duration_ns(x) for x in ns.markout_horizons.split(",") if x.strip()],
        "flat_taker": ns.flat_taker,
        "flat_maker": ns.flat_maker,
        "poly_multiplier": ns.poly_multiplier,
        "poly_daily_reward": ns.poly_daily_reward,
        "poly_category": ns.poly_category,
        "actual_rewards": ns.actual_rewards,
        "bootstrap_b": ns.bootstrap,
        "seed": seed,
    }


def _money(x: float | None) -> str:
    """`$1,234.50`, `-$3.10`; amounts under half a cent keep four decimals (`$0.0034`)."""
    if x is None:
        return "n/a"
    a = abs(x)
    txt = f"{a:,.{4 if 0.00005 <= a < 0.005 else 2}f}"
    return f"-${txt}" if x < 0 and float(txt.replace(",", "")) != 0 else f"${txt}"


def _svl_text(s: dict[str, Any]) -> str:
    b = s["fill_rate_bias"]
    return (
        f"fill-rate bias {'n/a' if b is None else f'{b * 100:+.1f} pp'}  (live {s['live_only']} only, sim "
        f"{s['sim_only']} only, bracket violations {s['bracket_violations']})"
    )


def render(cmd: str, doc: dict[str, Any], verbose: bool) -> str:
    """Answer-first terminal view of a summary."""
    lines: list[str] = []
    if cmd == "ingest":
        b = doc["book"]
        lines.append(
            f"pm ingest ({doc['venue']}): {len(doc['instruments'])} market(s), book {b['records']} rows, "
            f"verdict {b['verdict']}"
        )
        if doc.get("live"):
            lv = doc["live"]
            lines.append(f"own orders: {lv['n_orders']} orders, {lv['n_fills']} fills")
        of = doc.get("own_free")
        if of:
            lines.append(
                f"queue entry points: {of['exact']} exact, {of['aligned']} aligned, {of['unaligned']} unaligned, "
                f"{of['restated']} restated"
            )
        if doc.get("queue_samples"):
            lines.append(f"official queue positions: {doc['queue_samples']}")
        ch = doc.get("chain")
        if ch:
            lines.append(
                f"chain: {ch['maker_fills']} maker fills in {ch['matches']} matches, "
                f"{ch['filled_orders']} filled orders"
            )
    elif cmd == "fixtures":
        lines.append(
            f"pm fixtures: wrote {len(doc['files'])} files under {doc['out']} (invented captures, no venue data; "
            "README.txt lists them)"
        )
    elif cmd == "calibrate":
        best = doc["best"] or "none (nothing to fit)"
        lines.append(f"pm calibrate ({doc['venue']}, fit on {doc['fit_on']}): best {best}")
        cov = doc.get("bounds_coverage")
        lines.append(
            f"orders {doc['n_orders']} (train {doc['n_train_orders']}, test {doc['n_test_orders']}), "
            f"samples {doc['n_samples']}, bounds coverage {'n/a' if cov is None else f'{cov:.1%}'}"
        )

        def num(x: float | None, sign: str = "") -> str:
            return "n/a" if x is None else f"{x:{sign},.1f}"

        head = f"model (by train {doc['loss']})"
        n_unf = doc.get("n_unfilled_test_orders", 0)
        lines.append(
            f"  {head:<30} {'train':>7} {'test':>7} {'bias':>7} {'at fills':>9} {'unfilled':>9}  "
            f"({doc['units']}; at fills: MAE; unfilled: of {n_unf} unfilled test orders)"
        )
        models = doc["models"]
        shown = models[:6] + [
            m for m in models[6:] if m["family"] in ("cancels_ahead", "risk_adverse", "bounds_midpoint")
        ]
        for m in shown:
            loss = f"{num(m['train_loss']):>7} {num(m['test_loss']):>7}"
            unf = "" if m.get("unfilled_filled") is None else f"{m['unfilled_filled']}/{n_unf}"
            lines.append(
                f"  {m['model_id']:<30} {loss} {num(m['test_bias'], '+'):>7} {num(m['fill_truth_mae']):>9} {unf:>9}"
            )
        beats = doc.get("best_beats_bounds_midpoint")
        if beats is not None:
            lines.append(f"best beats the bounds midpoint on the test orders: {'yes' if beats else 'no'}")
        trust = doc.get("trust") or {}
        if trust.get("verdict") == "refused":
            lines.append(f"verdict: DO NOT USE ({', '.join(trust['reasons'])}); pm score warns when it scores this fit")
        elif trust:
            lines.append("verdict: usable")
        ft = doc["fill_truth"]
        if ft["n_orders"]:
            share = ft["fraction_ahead_true"]
            lines.append(
                f"fill truth: {ft['n_orders']} order(s), cancels ahead {ft['cancel_ahead_true']:,.1f} "
                f"(share {'n/a' if share is None else format(share, '.1%')}), "
                f"bound violations {ft['bound_violations']}"
            )
    else:
        band = doc["band"]["total"]
        trust = doc.get("calibration_trust")
        tag = f", calibration {trust['verdict']}" if trust else ""
        lines.append(
            f"pm score ({doc['venue']}, {doc['source']}, model {doc['model_id']}{tag}): total PnL "
            f"{_money(band['model'])} (scenarios {_money(band['lower'])} to {_money(band['upper'])})"
        )
        tb, nb = doc["band"]["trading_bound"], doc["band"]["net_trading_bound"]
        lines.append(
            f"trading PnL bound over orders: [{_money(tb['lower'])}, {_money(tb['upper'])}], net of fees "
            f"[{_money(nb['lower'])}, {_money(nb['upper'])}]"
        )
        m = doc["scenarios"]["model"]
        lines.append(
            f"model: spread capture {_money(m['spread_capture'])}, adverse selection {_money(m['adverse_selection'])}, "
            f"fees {_money(m['fees'])}, rewards {_money(m['rewards'])}, rebates {_money(m['rebates'])}"
        )
        if doc.get("live"):
            lv = doc["live"]
            lines.append(
                f"live: total {_money(lv['total'])}, trading {_money(lv['trading'])}, fees {_money(lv['fees'])}"
            )
            lo, hi, tot = band["lower"], band["upper"], lv["total"]
            if None not in (lo, hi, tot) and not lo - 1e-9 <= tot <= hi + 1e-9:
                held = tb["lower"] is not None and tb["lower"] - 1e-6 <= lv["trading"] <= tb["upper"] + 1e-6
                lines.append(
                    "  the live total is outside the scenario range (the scenarios replay fills and bound nothing); "
                    f"the trading bound {'holds' if held else 'does not hold'} the live trading PnL"
                )
        if trust and trust["verdict"] == "refused":
            lines.append(
                f"calibration verdict: refused ({', '.join(trust['reasons'])}): the model scenario is not a calibrated "
                "estimate"
            )
        rows = doc.get("sim_vs_live", [])
        test = [s for s in rows if s.get("orders") == "test"]  # the model on the calibration's held-out orders
        for k, s in enumerate(r for r in rows if r.get("orders", "all") == "all"):
            lines.append(f"  sim vs live {s['model_id']:<24} {_svl_text(s)}")
            if k == 0 and test:
                lines.append(f"    on the {test[0]['n_orders']} test orders only   {_svl_text(test[0])}")
    lines = [x.rstrip() for x in lines]
    if verbose:
        lines.append(json.dumps(doc, indent=2))
    return "\n".join(lines)


def run(argv: list[str]) -> int | None:
    """Runs `queuetruth pm ...`; None when `argv` is not a `pm` command (the main CLI handles it)."""
    if not is_pm(argv):
        return None
    json_out = any(
        a in ("--format=json", "--output-format=json") or (a in ("--format", "--output-format") and b == "json")
        for a, b in zip(argv, [*argv[1:], ""], strict=True)
    )
    g: dict[str, Any] = {"quiet": False, "verbose": False}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            g, rest = split_globals(argv)
            json_out = g["format"] == "json"
            if g.get("threads") is not None:
                raise QtError("QT-USAGE-001", "--threads: the pm commands run in one thread (pure Python); drop it")
            level = getattr(logging, str(g["log_level"]).upper().replace("WARN", "WARNING"), logging.WARNING)
            logging.basicConfig(
                stream=sys.stderr, level=logging.ERROR if g["quiet"] else level, format="%(levelname)s %(message)s"
            )
            if rest[:1] != ["pm"]:
                raise QtError("QT-USAGE-001", "expected `pm` as the command")
            if len(rest) == 1 or rest[1] in ("-h", "--help"):
                parser().print_help(sys.stdout)
                return EXIT_OK
            args = rest[1:]
            # values that start with a minus sign (`--grid -2:2:5`) would read as options
            for j in range(len(args) - 1, 0, -1):
                if args[j - 1] == "--grid" and args[j].startswith("-"):
                    args[j - 1 : j + 1] = [f"--grid={args[j]}"]
            try:
                ns = parser().parse_args(args)
            except SystemExit as e:  # --help of a subcommand
                return int(e.code or 0)
            cmd, spec = _spec(ns, g)
            doc = _dispatch(cmd, spec)
        except QtError as e:
            return _emit_error(e, json_out)
        except KeyboardInterrupt:
            return _emit_error(QtError("QT-USAGE-001", "interrupted"), json_out)
        except Exception as e:  # every unexpected failure is QT-ENGINE-001 with exit code 2
            log.debug("unexpected error", exc_info=True)
            return _emit_error(QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}"), json_out)
    if json_out:
        print(json.dumps(doc, ensure_ascii=False, allow_nan=False))
    else:
        print(render(cmd, doc, bool(g["verbose"])))
    if not g["quiet"]:
        sys.stdout.flush()  # the answer first, then the warnings, also when both go to one pipe
        for w in doc.get("warnings", []):
            print(f"warning: {w}", file=sys.stderr)
        for c in caught:
            print(f"warning: {c.message}", file=sys.stderr)
    return EXIT_OK


def _dispatch(cmd: str, spec: dict[str, Any]) -> dict[str, Any]:
    if cmd == "fixtures":
        from queuetruth.pm.fixtures import write_fixtures
        from queuetruth.pm.models import PmFixtures

        files = write_fixtures(spec["out"])
        note = "invented Kalshi and Polymarket captures in the documented formats; no venue data (README.txt)"
        return PmFixtures(out=str(spec["out"]), note=note, files=files).model_dump(mode="json")
    if cmd == "ingest":
        from queuetruth.pm.ingest import ingest

        return ingest(spec).model_dump(mode="json")
    if cmd == "calibrate":
        from queuetruth.pm.calibrate import calibrate

        return calibrate(spec).model_dump(mode="json")
    from queuetruth.pm.score import score

    return score(spec).model_dump(mode="json")


def _emit_error(e: QtError, json_out: bool) -> int:
    if json_out:
        print(json.dumps(e.to_document(), ensure_ascii=False))
    else:
        print(str(e), file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return e.exit_code


__all__ = ["duration_ns", "is_pm", "parser", "render", "run", "split_globals"]
