"""`check` without data: how many of a bar backtest's fills the real queue would give, from the atlas touch tables.

A bar backtester (Freqtrade, Backtrader, VectorBT, LEAN, and hftbacktest or NautilusTrader when they run on bars)
fills a resting limit order when a bar's low reaches a buy's limit or its high a sell's (the `touch` rule). The
Fill-Realism Atlas measures, for its own shadow orders, how many such fills the exact FIFO replay of the real queue
also gives (`atlas/v1/touch.json`, docs/concepts/touch-tables.md). This module applies those tables to a backtest:

1. Import the backtest with its simulator importer (the same code as the exact mode) and keep its resting limit
   orders; an order with a fill is a fill the backtest's bar rule gave it.
2. Each order's liquidity tier: the tier of its symbol in the atlas symbol shards, else `tier` (the caller's; the
   atlas has US equities on one venue, so a symbol it does not hold needs one). Its side is the order's.
3. The bar length: `bar`, else the importer option `timeframe`, else a Freqtrade result's `timeframe`, else read from
   the log's timestamps (all of them on a 1m, 5m or 1h clock grid).
4. Per (tier, side) the `touch` row at that bar length. Expected real fills are the backtest's fills times
   P(real | rule). The 95% interval comes from a simulation: each cell's P(real | rule) is drawn from a Beta
   distribution with the cell's value as its mean and a spread matching its 95% interval, the draws of all cells
   moving together (rank-coupled: the tables' errors share one bootstrap, so this is the cautious choice), then the
   real fills of each cell from a binomial over the backtest's fills. The P&L overstatement per order is the
   tables' `pnl_overstated_ticks` averaged over the backtest's resting orders, drawn the same way (normal).

No number is invented: a tier, side or bar length that is not in the tables, or a cell without a value or interval,
stops with the reason. The answer is an estimate for orders like the atlas's shadows (small orders joining the back
of the queue, resting as day orders) on IEX DEEP+, not an exact replay of these orders; `data=` gives that.
"""

from __future__ import annotations

import csv
import json
import math
import re
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError

SCHEMA = "qt.check_estimate"
#: Bar lengths of the touch tables, in seconds, largest first (`queuetruth.atlas.touch.BARS`).
_BARS = (("1h", 3600), ("5m", 300), ("1m", 60))
#: Distinct times needed to read a 5m or 1h bar from a log.
_MIN_TIMES = 5
_RESTING = ("gtc", "day", "post_only", "gtx", "gtd")
_FILLS = ("fill",)
#: Draws of the simulation.
DRAWS = 20_000
_TIER_SHORT = {"T1": "T1_top100", "T2": "T2_101_500", "T3": "T3_501_1500", "T4": "T4_1501_4000", "T5": "T5_rest"}
_NO_TOUCH = (
    "check: the atlas at {where} has no touch tables yet (atlas/v1/touch.json; built {built} from {days} day(s)). "
    "The kept atlas days were reduced before the tables existed and a backfill will add them. Until then check "
    "without --data gives no number"
)


def bar_of(text: str) -> str:
    """`1m`, `5m` or `1h` from `1m`, `1min`, `5min`, `60m`, `1h`, `3600s` (`QT-USAGE-001` otherwise)."""
    m = re.fullmatch(r"\s*(\d+)\s*(s|sec|m|min|h|hr)\s*", str(text).lower())
    if m is None:
        raise QtError("QT-USAGE-001", f"check: --bar {text!r} is not a bar length (1m, 5m or 1h)")
    sec = int(m.group(1)) * {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600}[m.group(2)]
    for name, size in _BARS:
        if sec == size:
            return name
    raise QtError(
        "QT-USAGE-001",
        f"check: the touch tables have bars of 1m, 5m and 1h, not {text}",
        "pass the nearest with --bar, or check with --data for an exact answer at any bar length",
    )


def tier_of(text: str) -> str:
    """A tier name from `T1` ... `T5`, a full name (`T1_top100`) or `ALL`."""
    s = str(text).strip()
    if s.upper() == "ALL":
        return "ALL"
    return _TIER_SHORT.get(s.upper(), s)


def _freqtrade_timeframe(paths: list[str]) -> str | None:
    """The `timeframe` of a Freqtrade backtest result (`.zip` or `.json`), if it states one."""
    for p in paths:
        try:
            if zipfile.is_zipfile(p):
                with zipfile.ZipFile(p) as z:
                    names = [n for n in z.namelist() if n.endswith(".json") and "_config" not in n]
                    docs = [json.loads(z.read(n)) for n in names]
            elif p.endswith(".json"):
                docs = [json.loads(Path(p).read_text())]
            else:
                continue
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
        for doc in docs:
            strat = doc.get("strategy") if isinstance(doc, dict) else None
            for v in (strat or {}).values():
                if isinstance(v, dict) and v.get("timeframe"):
                    return str(v["timeframe"])
    return None


def bar_from_times(times: Sequence[int]) -> tuple[str | None, str]:
    """(bar, why) from event times in ns: the clock grid every time is on (the greatest common divisor of the
    seconds since midnight) when it is a table bar; None with the reason otherwise. A 5m or 1h grid needs at least
    `_MIN_TIMES` distinct times, so a 1m backtest is not taken for a coarser one by chance."""
    ts = [int(t) for t in times if t is not None]
    if not ts or any(t % 1_000_000_000 for t in ts):
        return None, "its times are not on whole seconds (a tick-driven backtest?)"
    secs = sorted({t // 1_000_000_000 for t in ts})
    g = 0
    for s in secs:
        g = math.gcd(g, s % 86_400)
    g = g or 86_400
    if g % 60:
        return None, "its times are not all on whole minutes (a tick-driven backtest?)"
    name = {sec: n for n, sec in _BARS}.get(g)
    if name is not None and (g == 60 or len(secs) >= _MIN_TIMES):
        return name, f"every order and fill time is on the {name} grid"
    if len(secs) < _MIN_TIMES:
        return None, f"it has {len(secs)} distinct time(s), too few to tell the bar length"
    return None, f"its times are on a {g // 60} minute grid, which is not a bar of the tables (1m, 5m, 1h)"


def _orders(res: Any) -> tuple[list[dict[str, Any]], dict[str, bool], list[int], dict[str, int]]:
    """(resting limit orders, order key -> filled, event times, every order's type counts)."""
    filled: dict[str, bool] = {}
    times: list[int] = []
    for e in res.events:
        k = str(e["order_key"])
        if e["event_type"] in _FILLS:
            filled[k] = True
        for f in ("ts_exchange", "ts_local_send", "ts_local_recv"):
            if e.get(f) is not None:
                times.append(int(e[f]))
                break
    types: dict[str, int] = {}
    for o in res.orders:
        key = f"{o.get('order_type')}/{o.get('tif')}"
        types[key] = types.get(key, 0) + 1
    resting = [o for o in res.orders if o.get("order_type") == "limit" and o.get("tif") in _RESTING]
    return resting, filled, times, types


def _shard_tiers(src: Any, names: list[str]) -> dict[str, tuple[str, str]]:
    """Backtest instrument name -> (atlas symbol, tier) for the names the atlas symbol shards hold."""
    from queuetruth.check.symbols import norm

    out: dict[str, tuple[str, str]] = {}
    shards: dict[str, dict[str, str]] = {}
    for name in names:
        sym = norm(name)
        c = sym[:1]
        key = c if "A" <= c <= "Z" else "_"
        if key not in shards:
            path = f"symbols/{key}.json"
            doc = src.get(path) if src.has(path) else {"symbols": []}
            shards[key] = {e["symbol"]: e["tier"] for e in doc["symbols"]}
        if sym in shards[key]:
            out[name] = (sym, shards[key][sym])
    return out


def _beta_draws(rng: Any, v: float, lo: float, hi: float, n: int) -> Any:
    """`n` sorted draws of a Beta with mean `v` and the spread of the 95% interval [lo, hi]."""
    import numpy as np

    sd = (hi - lo) / (2 * 1.959964)
    if sd <= 0 or v <= 0 or v >= 1:
        return np.full(n, min(max(v, 0.0), 1.0))
    k = max(v * (1 - v) / (sd * sd) - 1, 1.0)
    return np.sort(rng.beta(v * k, (1 - v) * k, n))


def _normal_draws(rng: Any, v: float, lo: float, hi: float, n: int) -> Any:
    import numpy as np

    sd = (hi - lo) / (2 * 1.959964)
    return np.sort(rng.normal(v, max(sd, 0.0), n))


def confirm_on_terminal(interactive: bool) -> Callable[[str], bool]:
    """The consent callback of `check` without data: `queuetruth atlas`'s prompt, naming `check` and `--atlas`."""
    from queuetruth.atlas import confirm_on_terminal as atlas_confirm

    return atlas_confirm(interactive, "check", "--atlas DIR")  # type: ignore[no-any-return]


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _ticks(x: float) -> str:
    return f"{x:.2f}"


def estimate(
    backtest: str | Path | Sequence[str | Path],
    *,
    backtester: str | None = None,
    tier: str | None = None,
    bar: str | None = None,
    atlas: str | Path | None = None,
    yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    refresh: bool = False,
    fetch: Callable[[str], bytes] | None = None,
    out: str | Path | None = None,
    options: dict[str, Any] | None = None,
    seed: int | None = None,
    draws: int = DRAWS,
) -> Any:
    """The atlas estimate of `check` (module docstring); returns a `CheckResult` whose `doc` is `qt.check_estimate`.

    `atlas`: a directory or URL of the atlas data API (default: the public atlas, cached; nothing is downloaded
    unless `yes=True` or `confirm(url)` says yes). `tier` is the tier of symbols the atlas does not hold (`T1` to
    `T5`, a full tier name or `ALL`); `bar` the backtest's bar length (`1m`, `5m`, `1h`) when its log does not say.
    """
    import numpy as np

    from queuetruth.atlas import open_source, tier_name
    from queuetruth.atlas.touch import TouchTables
    from queuetruth.check.detect import BACKTESTERS, detect_all
    from queuetruth.check.run import CheckResult, _import, _paths, _vectorbt_labels

    paths = _paths(backtest)
    if backtester is not None and backtester not in BACKTESTERS:
        raise QtError("QT-USAGE-001", f"check: backtester must be one of {list(BACKTESTERS)}, got {backtester!r}")
    kind = backtester or detect_all(paths)
    fallback = tier_of(tier) if tier else None
    notes: list[str] = []
    extra: dict[str, Any] = dict(options or {})
    if kind == "vectorbt":
        extra.setdefault("order_type", "limit")
        if not any(extra.get(k) for k in ("column", "instrument", "instruments")):
            labels = _vectorbt_labels(paths)
            if len(labels) > 1:
                extra["instruments"] = {lab: lab for lab in labels}
        notes.append("VectorBT records are fills only: each counts as a resting limit order the bar rule filled")
    res = _import(kind, paths, None, extra)
    resting, filled, times, types = _orders(res)
    if not resting:
        shown = ", ".join(f"{n} {k}" for k, n in sorted(types.items())) or "none"
        raise QtError(
            "QT-INPUT-002",
            f"check: the backtest has no resting limit order ({len(res.orders)} order(s): {shown})",
            "only resting limit orders have a queue position; market orders fill the same way everywhere",
        )

    # ---- bar length ----------------------------------------------------------------------------------------------
    if bar is not None:
        bar_name, bar_how = bar_of(bar), "given with --bar"
    elif extra.get("timeframe"):
        bar_name, bar_how = bar_of(str(extra["timeframe"])), "the importer option timeframe"
    elif kind == "freqtrade" and (tf := _freqtrade_timeframe(paths)):
        bar_name, bar_how = bar_of(tf), "the Freqtrade result's timeframe"
    else:
        found, why = bar_from_times(times)
        if found is None:
            raise QtError(
                "QT-USAGE-001",
                f"check: cannot tell the backtest's bar length from its log: {why}",
                "pass --bar 1m, 5m or 1h (the touch tables' bars); a backtest that does not run on bars needs "
                "--data for the exact answer",
            )
        bar_name, bar_how = found, f"read from the log ({why})"

    # ---- the atlas -----------------------------------------------------------------------------------------------
    src = open_source(atlas, yes=yes, confirm=confirm, refresh=refresh, fetch=fetch)
    m = src.manifest
    if not src.has("touch.json"):
        raise QtError(
            "QT-INPUT-001",
            _NO_TOUCH.format(where=src.where, built=m.get("built_at"), days=len(m.get("days") or [])),
            "pass --data DIR (order-level data of the backtest's days) for the exact answer; try again after the "
            "atlas's next build (--refresh)",
        )
    tables = TouchTables.model_validate(src.get("touch.json"))

    # ---- tiers ---------------------------------------------------------------------------------------------------
    names = sorted({str(o.get("instrument")) for o in resting})
    known = _shard_tiers(src, names)
    missing = [n for n in names if n not in known]
    if missing and fallback is None:
        raise QtError(
            "QT-USAGE-001",
            f"check: {', '.join(missing[:8])}{' and more' if len(missing) > 8 else ''} "
            f"{'is' if len(missing) == 1 else 'are'} not in the atlas ({m.get('venue_name')}, US equities seen on "
            f"enough days), so the liquidity tier of {'that symbol' if len(missing) == 1 else 'those symbols'} is "
            "unknown and the touch tables cannot be chosen",
            "pass --tier T1 to T5 (T1: the 100 most traded US stocks ... T5: the less traded) for the symbol's "
            "liquidity, or --tier ALL for the atlas's average",
        )
    tier_by_name = {n: known[n][1] if n in known else str(fallback) for n in names}
    if missing:
        notes.append(f"{len(missing)} symbol(s) not in the atlas use --tier {fallback}: {', '.join(missing)}")
    if known:
        notes.append("tiers from the atlas: " + ", ".join(f"{n} {t}" for n, (_, t) in sorted(known.items())))

    # ---- per (tier, side) ----------------------------------------------------------------------------------------
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for o in resting:
        side = str(o.get("side"))
        g = groups.setdefault((tier_by_name[str(o.get("instrument"))], side), {"orders": 0, "filled": 0})
        g["orders"] += 1
        g["filled"] += bool(filled.get(str(o["order_key"])))
    have = {(r.tier, r.side, r.bar) for r in tables.rows if r.rule == "touch"}
    rows = []
    for (t, side), g in sorted(groups.items()):
        r = tables.row(tier=t, side=side, bar=bar_name, rule="touch")
        if r is None:
            tiers = sorted({x[0] for x in have})
            bars = sorted({x[2] for x in have if x[0] == t})
            sides = sorted({x[1] for x in have if x[0] == t and x[2] == bar_name})
            if t not in tiers:
                why = f"no tier {t} (the tables have {', '.join(tiers)})"
            elif bar_name not in bars:
                why = f"no {bar_name} bars for tier {t} (they have {', '.join(bars) or 'none'})"
            else:
                why = f"no {side} side for tier {t} at {bar_name} (they have {', '.join(sides) or 'none'})"
            raise QtError(
                "QT-USAGE-001",
                f"check: the touch tables have {why}; no number is invented for it",
                "pass another --tier or --bar that the tables hold, or check with --data",
            )
        for metric in ("p_real_given_rule", "pnl_overstated_ticks"):
            e = getattr(r, metric)
            if e.value is None or e.ci_lo is None or e.ci_hi is None:
                raise QtError(
                    "QT-INPUT-002",
                    f"check: the touch table of tier {t}, {side}, {bar_name} has no {metric} with an interval "
                    f"({r.orders} orders, {r.dates} date(s), {r.symbols} symbol(s)); no number is invented for it",
                    "pass another --tier, or check with --data",
                )
        rows.append((t, side, g, r))

    # ---- the simulation ------------------------------------------------------------------------------------------
    rng = np.random.default_rng(0 if seed is None else seed)
    n_filled = sum(g["filled"] for _, _, g, _ in rows)
    n_rest = sum(g["orders"] for _, _, g, _ in rows)
    real = np.zeros(draws, dtype=np.int64)
    pnl = np.zeros(draws)
    point_real = 0.0
    point_pnl = 0.0
    out_rows = []
    for t, side, g, r in rows:
        p, mk = r.p_real_given_rule, r.pnl_overstated_ticks
        pd_ = _beta_draws(rng, float(p.value), float(p.ci_lo), float(p.ci_hi), draws)  # type: ignore[arg-type]
        real += rng.binomial(g["filled"], pd_)
        pnl += g["orders"] * _normal_draws(rng, float(mk.value), float(mk.ci_lo), float(mk.ci_hi), draws)  # type: ignore[arg-type]
        point_real += g["filled"] * float(p.value)  # type: ignore[arg-type]
        point_pnl += g["orders"] * float(mk.value)  # type: ignore[arg-type]
        out_rows.append(
            {
                "tier": t,
                "tier_name": tier_name(t),
                "side": side,
                "bar": bar_name,
                "orders": g["orders"],
                "backtest_filled": g["filled"],
                "p_real_given_touch": p.model_dump(),
                "expected_real_fills": round(g["filled"] * float(p.value), 2),  # type: ignore[arg-type]
                "pnl_overstated_ticks_per_order": mk.model_dump(),
                "atlas_orders": r.orders,
                "atlas_dates": r.dates,
                "atlas_symbols": r.symbols,
            }
        )
    lo, hi = (int(x) for x in np.percentile(real, [2.5, 97.5], method="nearest"))
    per = pnl / n_rest
    plo, phi = (float(x) for x in np.percentile(per, [2.5, 97.5]))
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": 1,
        "answer": "",
        "mode": "atlas",
        "backtester": kind,
        "backtest": paths,
        "n_orders": len(res.orders),
        "n_resting": n_rest,
        "backtest_filled": n_filled,
        "bar": bar_name,
        "bar_from": bar_how,
        "rule": "touch",
        "real_fills": {"expected": round(point_real, 1), "ci_lo": lo, "ci_hi": hi},
        "touch_only": {"expected": round(n_filled - point_real, 1), "ci_lo": n_filled - hi, "ci_hi": n_filled - lo},
        "pnl_overstated_ticks_per_order": {
            "value": round(point_pnl / n_rest, 4),
            "ci_lo": round(plo, 4),
            "ci_hi": round(phi, 4),
        },
        "groups": out_rows,
        "method": (
            f"expected real fills: the backtest's touch fills per (tier, side) times P(real | touch) of the atlas "
            f"touch tables at {bar_name} bars; 95% interval from {draws:,} simulated draws (each cell's P(real | "
            "touch) from a Beta with the cell's mean and 95% interval, all cells moving together, then binomial "
            "real fills); P&L: the tables' P&L overstatement per order placed (rule markout minus real markout, one "
            "bar after the fill, in ticks), averaged over the backtest's resting orders, drawn the same way"
        ),
        "rests_on": [
            f"the Fill-Realism Atlas touch tables, {tables.source.upper()} DEEP+ only (one venue), "
            f"{len(tables.days)} day(s) from {tables.days[0] if tables.days else '?'} to "
            f"{tables.days[-1] if tables.days else '?'}, atlas root {tables.atlas_root}",
            "orders like the atlas's shadows: small, joining the back of the queue, resting as day orders; bars from "
            "one venue's prints (docs/concepts/touch-tables.md, 'What is approximate')",
            "an estimate for orders like these, not an exact replay of this backtest's orders: pass --data with "
            "order-level data of its days for that",
        ],
        "atlas": {
            "source": src.where,
            "atlas_root": m.get("atlas_root"),
            "built_at": m.get("built_at"),
            "touch_days": list(tables.days),
            "attribution": tables.attribution or m.get("attribution"),
        },
        "notes": notes,
    }
    doc["answer"] = _answer(doc)
    outp = Path(out) if out is not None else Path(tempfile.mkdtemp(prefix="qt-check-"))
    outp.mkdir(parents=True, exist_ok=True)
    doc["files"] = {"check": str(outp / "check.json"), "groups": str(outp / "groups.csv")}
    with (outp / "groups.csv").open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["tier", "side", "bar", "orders", "backtest_filled", "p_real_given_touch", "ci_lo", "ci_hi",
                    "expected_real_fills", "pnl_overstated_ticks_per_order", "pnl_ci_lo", "pnl_ci_hi"])  # fmt: skip
        for x in out_rows:
            p, mk = x["p_real_given_touch"], x["pnl_overstated_ticks_per_order"]
            w.writerow([x["tier"], x["side"], x["bar"], x["orders"], x["backtest_filled"], p["value"], p["ci_lo"],
                        p["ci_hi"], x["expected_real_fills"], mk["value"], mk["ci_lo"], mk["ci_hi"]])  # fmt: skip
    (outp / "check.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    return CheckResult(doc=doc, orders=out_rows, out=outp)


def _answer(doc: dict[str, Any]) -> str:
    """The answer in plain words, with what it rests on."""
    nf, nr = doc["backtest_filled"], doc["n_resting"]
    rf, to = doc["real_fills"], doc["touch_only"]
    s = (
        f"Your backtest filled {nf:,} of {nr:,} resting limit order{'' if nr == 1 else 's'} when a {doc['bar']} bar "
        f"touched the limit. In the real queue about {rf['expected']:,.0f} of those fills would have happened "
        f"(95% interval {rf['ci_lo']:,} to {rf['ci_hi']:,}), so about {to['expected']:,.0f}"
    )
    s += f" ({_pct(to['expected'] / nf)}) are touch-only." if nf else " are touch-only."
    p = doc["pnl_overstated_ticks_per_order"]
    if p["ci_lo"] > 0 or p["ci_hi"] < 0:
        way = "too high" if p["value"] > 0 else "too low"
        s += (
            f" Its P&L is about {_ticks(abs(p['value']))} ticks per order {way} (95% interval {_ticks(p['ci_lo'])} "
            f"to {_ticks(p['ci_hi'])})."
        )
    else:
        s += (
            f" Its P&L per order differs by {_ticks(p['value'])} ticks, an interval ({_ticks(p['ci_lo'])} to "
            f"{_ticks(p['ci_hi'])}) that includes 0: no clear P&L difference."
        )
    a = doc["atlas"]
    days = a["touch_days"]
    s += (
        f" An estimate from the Fill-Realism Atlas touch tables (IEX DEEP+, one venue, {len(days)} day"
        f"{'' if len(days) == 1 else 's'}) for orders like the atlas's shadow orders, not an exact replay of yours: "
        "pass --data for that."
    )
    return s


def text(doc: dict[str, Any]) -> str:
    """The terminal report: the answer first, then the table per tier and side, what it rests on, the source."""
    from queuetruth.check.render import _table

    out = [doc["answer"], ""]
    head = ["tier", "side", "orders", "touch fills", "P(real | touch)", "real fills", "P&L too high, ticks/order"]
    body = []
    for g in doc["groups"]:
        p, mk = g["p_real_given_touch"], g["pnl_overstated_ticks_per_order"]
        body.append(
            [
                g["tier"],
                g["side"],
                f"{g['orders']:,}",
                f"{g['backtest_filled']:,}",
                f"{_pct(p['value'])} [{_pct(p['ci_lo'])}, {_pct(p['ci_hi'])}]",
                f"{g['expected_real_fills']:,.1f}",
                f"{_ticks(mk['value'])} [{_ticks(mk['ci_lo'])}, {_ticks(mk['ci_hi'])}]",
            ]
        )
    out += _table(head, body, {2, 3, 4, 5, 6})
    out += ["", f"Bar length {doc['bar']} ({doc['bar_from']}); rule: a bar touching the limit fills the order."]
    out.append("What it rests on:")
    out += [f"  - {x}" for x in doc["rests_on"]]
    out.append(f"Method: {doc['method']}.")
    a = doc["atlas"]
    out.append(f"Source: {a['source']}, built {a['built_at']}.")
    if a.get("attribution"):
        out.append(str(a["attribution"]))
    out.append(f"Details: {doc['files']['check']} (full summary), {doc['files']['groups']} (one row per tier and side)")
    return "\n".join(out)


def html(doc: dict[str, Any]) -> str:
    """The self-contained report page of the estimate."""
    from queuetruth.report.fmt import esc
    from queuetruth.report.page import section, shell, table, ul

    body = [f'<header><h1>How wrong is this backtest?</h1><p class="lead" id="answer">{esc(doc["answer"])}</p>']
    body.append(f"<p class='note'>{esc(doc['backtester'])} backtest {esc(', '.join(doc['backtest']))}.</p></header>")
    head = ["tier", "side", "orders", "touch fills", "P(real | touch)", "real fills", "P&L too high, ticks/order"]
    rows = [
        [
            g["tier"],
            g["side"],
            f"{g['orders']:,}",
            f"{g['backtest_filled']:,}",
            f"{_pct(g['p_real_given_touch']['value'])}",
            f"{g['expected_real_fills']:,.1f}",
            _ticks(g["pnl_overstated_ticks_per_order"]["value"]),
        ]
        for g in doc["groups"]
    ]
    body.append(section("groups", "By tier and side", [table(head, rows, numeric=(2, 3, 4, 5, 6))]))
    body.append(section("rests", "What it rests on", [ul(doc["rests_on"]), f"<p>{esc(doc['method'])}.</p>"]))
    if doc.get("notes"):
        body.append(section("notes", "Notes", [ul(doc["notes"])]))
    return shell("queuetruth check", "\n".join(body), doc["answer"], skip_to="answer")


__all__ = ["DRAWS", "SCHEMA", "bar_from_times", "bar_of", "confirm_on_terminal", "estimate", "html", "text", "tier_of"]
