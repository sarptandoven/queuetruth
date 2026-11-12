#!/usr/bin/env python3
"""Touch tables for bar-based backtests (docs/concepts/touch-tables.md): what a bar rule says against the real queue.

A candle backtester fills a resting limit order when a bar's low (buy) or high (sell) reaches the limit (`touch`), or
goes `k` ticks through it (`penetrate_k`). For the atlas's shadow orders resting as day orders, the exact replay says
whether and when the real queue filled them. This module measures both and carries the result through the atlas:

- `unit_touch` (called by `unit.py` when the unit spec has `touch`): one exact replay of every population's shadows
  with each order resting until the regular-session close, bars from the replayed day's prints, and per (shadow
  population, symbol-day, side, bar length, rule) the sums `SUMS` (`touch_stats.parquet`, not per order);
- `reduce_rows` (`atlas.py reduce-day`): the units' sums with the day's `tier` and exactness labels
  (`touch_cells.parquet`);
- `combine` (`atlas.py combine`): per (shadow population, tier incl. `ALL`, side incl. `all`, bar length, rule) the
  `METRICS` with 95% intervals from the atlas's two-way bootstrap over dates x symbols
  (`queuetruth.eval.blocks.cluster_bootstrap`), `touch_by_tier.parquet`; days reduced before the tables existed
  are left out and the days used are returned (`touch_days` in `atlas_manifest.json`);
- `section` (`atlas_site.py`): a short section of the site and `atlas/v1/touch.json`
  (`queuetruth.atlas.touch.TouchTables`).

The engine is imported inside the functions, as in `atlas.py`.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
from pathlib import Path
from typing import Any

#: what `plan.py` puts in the run spec (and so in every unit spec) unless `--no-touch`
DEFAULT_SPEC: dict[str, Any] = {"bars_s": [60, 300, 3600], "penetrate_ticks": [1, 2]}
KEYS = ["shadow_pop", "symbol", "session_date", "side", "bar_s", "rule"]
#: per cell: orders; real fills and rule fills before the close; rule fills the real queue had filled by the end of
#: the rule's bar; real fills the rule had filled by the end of the real fill's bar; markout sums in ticks
SUMS = ("n", "n_real", "n_rule", "n_rule_real", "n_real_rule", "mk_real", "mk_rule")
#: metric -> (numerator, denominator) over the summed fields; the numerator may be a difference of two sums
METRICS: dict[str, tuple[tuple[str, ...], str]] = {
    "real_fill_rate": (("n_real",), "n"),
    "rule_fill_rate": (("n_rule",), "n"),
    "p_real_given_rule": (("n_rule_real",), "n_rule"),
    "p_rule_given_real": (("n_real_rule",), "n_real"),
    "real_markout_ticks": (("mk_real",), "n_real"),
    "rule_markout_ticks": (("mk_rule",), "n_rule"),
    "pnl_overstated_ticks": (("mk_rule", "-mk_real"), "n"),
}
SIDES = {66: "buy", 65: "sell"}
#: end states of a shadow that never rested in the book (`queuetruth.eval.impact.REJECTED_END_STATES`)
REJECTED = ("rejected_no_touch", "rejected_would_cross")  # shadow `side`: ASCII B (bid) and A (ask)
CELLS = "touch_cells.parquet"
TABLE = "touch_by_tier.parquet"
DOC = "docs/concepts/touch-tables.md"
NS = 1_000_000_000
_BLOCK = 512


def rule_names(spec: dict[str, Any]) -> list[tuple[str, int]]:
    """(rule, ticks through the limit) of a `touch` spec: `touch` (0) and `penetrate_<k>`."""
    return [("touch", 0), *((f"penetrate_{k}", int(k)) for k in spec.get("penetrate_ticks", []))]


def bar_label(bar_s: int) -> str:
    return f"{bar_s // 3600}h" if bar_s % 3600 == 0 else f"{bar_s // 60}m" if bar_s % 60 == 0 else f"{bar_s}s"


def first_reach(s: Any, start: Any, thr: Any) -> Any:
    """Per query q, the first index j >= start[q] with s[j] <= thr[q], or -1. Block minima make a query cost about
    len(s) / 512 + 512 comparisons instead of len(s)."""
    import numpy as np

    n = len(s)
    out = np.full(len(start), -1, dtype=np.int64)
    if not n:
        return out
    nb = -(-n // _BLOCK)
    pad = np.full(nb * _BLOCK, np.iinfo(np.int64).max, dtype=np.int64)
    pad[:n] = s
    bmin = pad.reshape(nb, _BLOCK).min(axis=1)
    for q, (i, t) in enumerate(zip(start.tolist(), thr.tolist(), strict=True)):
        if i >= n:
            continue
        b = i // _BLOCK
        hit = np.flatnonzero(pad[i : (b + 1) * _BLOCK] <= t)
        if hit.size:
            out[q] = i + hit[0]
            continue
        rest = np.flatnonzero(bmin[b + 1 :] <= t)
        if rest.size:
            bb = b + 1 + int(rest[0])
            out[q] = bb * _BLOCK + int(np.flatnonzero(pad[bb * _BLOCK : (bb + 1) * _BLOCK] <= t)[0])
    return out


def markout(ts: Any, px: Any, lim: Any, sign: Any, tick: int, t: Any, bar_ns: int, have: Any) -> Any:
    """Per order with `have`: `sign x (P(t + bar) - L) / tick`, P the last print (`ts`, `px`, sorted) at or before
    one bar after `t`; 0 where `have` is false or no print came before."""
    import numpy as np

    j = np.searchsorted(ts, np.where(have, t, 0) + bar_ns, side="right") - 1
    ref = px[np.clip(j, 0, None)] if len(px) else lim
    ref = np.where(j >= 0, ref, lim)
    return np.where(have, sign * (ref - lim) / tick, 0.0)


def outcomes(results: Any, trades: Any, instruments: Any, spec: dict[str, Any]) -> Any:
    """Per order, bar length and rule: whether the real queue and the rule filled it before the close, whether each
    is real by the end of the other's bar, and the markouts (docs/concepts/touch-tables.md). `results`: the exact
    replay's `shadow_results` with a `shadow_pop` column; `trades`: its `trades`; `instruments`: the dataset's.
    Orders that never rested in the book (not live or without a price, rejected as crossing or without a touch,
    out of session) or went live at or after the close are left out."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc

    inst = {r["instrument_id"]: r for r in instruments.to_pylist()}
    rules = rule_names(spec)
    bars = [int(b) for b in spec["bars_s"]]
    cols: dict[str, list[Any]] = {k: [] for k in ("shadow_pop", "instrument_id", "side", "bar_s", "rule")}
    vals: dict[str, list[Any]] = {k: [] for k in ("real", "rule_fill", "rule_real", "real_rule", "mk_real", "mk_rule")}
    t_iid = trades.column("instrument_id").to_numpy()
    for iid in sorted(set(results.column("instrument_id").to_pylist())):
        meta = inst[iid]
        mid, tick = int(meta["midnight_utc_ns"]), int(meta["tick_size"])
        lo, hi = mid + int(meta["open_s"]) * NS, mid + int(meta["close_s"]) * NS
        rs = results.filter(pc.equal(results["instrument_id"], iid))
        t0 = rs.column("ts_live").cast(pa.int64())
        rested = pc.invert(pc.is_in(rs["end_state"].cast(pa.utf8()), value_set=pa.array(sorted(REJECTED))))
        ok = pc.and_(pc.and_(pc.is_valid(t0), pc.is_valid(rs["price"])), pc.less(pc.fill_null(t0, hi), hi))
        ok = pc.and_(ok, pc.and_(pc.fill_null(rested, True), pc.invert(pc.fill_null(rs["out_of_session"], False))))
        ok = ok.to_numpy(zero_copy_only=False)
        rs = rs.filter(pa.array(ok))
        if not rs.num_rows:
            continue
        tr = trades.filter(pa.array(t_iid == iid))
        ts = tr.column("ts_event").cast(pa.int64()).to_numpy()
        keep = (ts >= lo) & (ts < hi)
        order = np.lexsort((tr.column("sequence").to_numpy()[keep], ts[keep]))
        ts, px = ts[keep][order], tr.column("price").to_numpy()[keep][order].astype(np.int64)
        t0 = rs.column("ts_live").cast(pa.int64()).to_numpy()
        lim = rs.column("price").to_numpy().astype(np.int64)
        sign = np.where(rs.column("side").to_numpy() == 66, 1, -1)
        tf = pc.fill_null(rs.column("ts_first_fill").cast(pa.int64()), -1).to_numpy()
        real = (tf >= 0) & (tf < hi)
        start = np.searchsorted(ts, t0, side="left")
        pops = rs.column("shadow_pop").to_pylist()
        sides = [SIDES[int(x)] for x in rs.column("side").to_pylist()]

        # buy: first print at or below L - k tick; sell: first print at or above L + k tick (negated: at or below)
        for rule, k in rules:
            hit_buy = first_reach(px, start, lim - k * tick)
            hit_sell = first_reach(-px, start, -(lim + k * tick))
            hit = np.where(sign > 0, hit_buy, hit_sell)
            fills = hit >= 0
            tr_ = np.where(fills, ts[np.clip(hit, 0, None)] if len(ts) else 0, -1)
            for bar_s in bars:
                bar_ns = bar_s * NS
                b_rule = (tr_ - mid) // bar_ns
                b_real = (tf - mid) // bar_ns
                n = len(t0)
                cols["shadow_pop"] += pops
                cols["instrument_id"] += [iid] * n
                cols["side"] += sides
                cols["bar_s"] += [bar_s] * n
                cols["rule"] += [rule] * n
                vals["real"].append(real)
                vals["rule_fill"].append(fills)
                vals["rule_real"].append(fills & real & (b_real <= b_rule))
                vals["real_rule"].append(real & fills & (b_rule <= b_real))
                vals["mk_real"].append(markout(ts, px, lim, sign, tick, tf, bar_ns, real))
                vals["mk_rule"].append(markout(ts, px, lim, sign, tick, tr_, bar_ns, fills))
    out = {
        "shadow_pop": pa.array(cols["shadow_pop"], pa.utf8()),
        "instrument_id": pa.array(cols["instrument_id"], pa.uint32()),
        "side": pa.array(cols["side"], pa.utf8()),
        "bar_s": pa.array(cols["bar_s"], pa.int64()),
        "rule": pa.array(cols["rule"], pa.utf8()),
    }
    for k, v in vals.items():
        arr = np.concatenate(v) if v else np.zeros(0)
        out[k] = pa.array(arr.astype(np.float64 if k.startswith("mk_") else np.int64))
    return pa.table(out)


def stats(per_order: Any, instruments: Any, symbols: dict[int, str] | None = None) -> Any:
    """`outcomes` summed per `KEYS` (the unit's `touch_stats` table): `session_date` as the other unit tables carry
    it (int32 days since 1970-01-01), `symbol` from `symbols` (locate -> symbol) or the instrument's symbol."""
    import pyarrow as pa

    ren = {"real": "n_real", "rule_fill": "n_rule", "rule_real": "n_rule_real", "real_rule": "n_real_rule"}
    t = per_order.rename_columns([ren.get(c, c) for c in per_order.column_names])
    t = t.append_column("n", pa.array([1] * t.num_rows, pa.int64()))
    # one thread: the markout sums are added in row order, so a re-run of the unit gives the same leaf digest
    g = t.group_by(["shadow_pop", "instrument_id", "side", "bar_s", "rule"], use_threads=False).aggregate(
        [(f, "sum") for f in SUMS]
    )
    g = g.rename_columns([c.removesuffix("_sum") for c in g.column_names])
    inst = {r["instrument_id"]: r for r in instruments.to_pylist()}
    iids = g.column("instrument_id").to_pylist()
    sym = [(symbols or {}).get(i) or inst[i]["symbol"] for i in iids]
    day = [(inst[i]["session_date"] - dt.date(1970, 1, 1)).days for i in iids]
    g = g.append_column("symbol", pa.array(sym, pa.utf8()))
    g = g.append_column("session_date", pa.array(day, pa.int32()))
    return g.select([*KEYS, *SUMS])


def empty_stats() -> Any:
    import pyarrow as pa

    types = {"session_date": pa.int32(), "bar_s": pa.int64()}
    fields = [(k, types.get(k, pa.utf8())) for k in KEYS]
    fields += [(k, pa.float64() if k.startswith("mk_") else pa.int64()) for k in SUMS]
    return pa.schema(fields).empty_table()


def unit_touch(
    ds: Path,
    shadow_files: dict[str, Path],
    wd: Path,
    spec: dict[str, Any],
    *,
    seed: int,
    threads: int = 1,
    symbols: dict[int, str] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """One unit's `touch_stats` table and its leaf entry. Every population's shadows go into one exact replay
    (shadow ids prefixed `<population>:`; shadows do not change the book, so each one's fills are those of a replay
    of it alone), each resting until its instrument's regular-session close instead of its planned cancel."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    import queuetruth as qt

    instruments = pq.read_table(ds / "instruments.parquet")
    close = {r["instrument_id"]: int(r["midnight_utc_ns"]) + int(r["close_s"]) * NS for r in instruments.to_pylist()}
    if len(close) != instruments.num_rows:
        raise ValueError("touch: one session per instrument expected in a unit's dataset")
    parts, pop_of = [], {}
    for pop, path in sorted(shadow_files.items()):
        t = pq.read_table(path)
        ids = pc.binary_join_element_wise(pop, t["shadow_id"], ":")
        j = t.column_names.index("shadow_id")
        t = t.set_column(j, t.schema.field(j), ids)  # the field keeps the schema's nullability
        ends = pa.array([close[i] for i in t.column("instrument_id").to_pylist()], pa.int64())
        i = t.column_names.index("ts_cancel")
        t = t.set_column(i, t.schema.field(i), ends.cast(t.schema.field(i).type))
        parts.append(t)
        pop_of.update(dict.fromkeys(ids.to_pylist(), pop))
    shadows = pa.concat_tables(parts)
    sh = wd / "touch-shadows.parquet"
    pq.write_table(shadows, sh)
    rp = wd / "touch-replay"
    qt.replay(
        {
            "dataset": {"path": str(ds)},
            "shadows": {"path": str(sh)},
            "out": str(rp),
            "seed": seed,
            "bbo": "none",
            "trajectory": "none",
            "threads": threads,
        }
    )
    res = pq.read_table(rp / "shadow_results.parquet")
    res = res.append_column("shadow_pop", pa.array([pop_of[s] for s in res.column("shadow_id").to_pylist()]))
    trades = pq.read_table(rp / "trades.parquet", columns=["ts_event", "instrument_id", "sequence", "price"])
    per = outcomes(res, trades, instruments, spec)
    table = stats(per, instruments, symbols) if per.num_rows else empty_stats()
    live = int(pc.sum(pc.is_valid(res["ts_live"])).as_py() or 0)
    info = {
        "shadows": shadows.num_rows,
        "live": live,
        "scored": per.num_rows // max(1, len(spec["bars_s"]) * len(rule_names(spec))),
        "trades": trades.num_rows,
    }
    return table, info


def reduce_rows(t: Any) -> Any:
    """A unit's `touch_stats` with the day's labels summed per `KEYS` and the labels (`atlas.py reduce-day`)."""
    flag_cols = ["tier", "replay_ok", "trading_ok", "exact_ok"]
    keys = [*KEYS, *flag_cols]
    g = t.group_by(keys, use_threads=False).aggregate([(f, "sum") for f in SUMS])
    return g.select([*keys, *[f"{f}_sum" for f in SUMS]]).rename_columns([*keys, *SUMS])


def combine(
    days: list[tuple[dict[str, Any], Path]], out: Path, *, flag_policy: str, bootstrap_b: int, seed: int
) -> list[str]:
    """`touch_by_tier.parquet` from the days that have `touch_cells.parquet`; returns those days' dates (none: no
    table is written). Each metric is a ratio of sums with the atlas's two-way bootstrap over dates x symbols: the
    cells go to `cluster_bootstrap` as `n_y` (numerator) and `n` (denominator) under its `exact_fill_rate` ratio, so
    every metric is resampled with the same draws as the fill-model tables."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from queuetruth.eval import blocks as B

    used = [(dm["date"], d / CELLS) for dm, d in days if (d / CELLS).exists()]
    if not used:
        return []
    parts = [t for t in (pq.read_table(p) for _, p in used) if t.num_rows]
    t = pa.concat_tables(parts, promote_options="default") if parts else empty_stats()
    if parts and flag_policy == "exclude":
        t = t.filter(t["exact_ok"])
    rows: list[dict[str, Any]] = []
    for pop in sorted(pc.unique(t["shadow_pop"]).to_pylist()):
        tp = t.filter(pc.equal(t["shadow_pop"], pop))
        for tier in [*sorted(pc.unique(tp["tier"]).to_pylist()), "ALL"]:
            tt = tp if tier == "ALL" else tp.filter(pc.equal(tp["tier"], tier))
            for side in ("buy", "sell", "all"):
                st = tt if side == "all" else tt.filter(pc.equal(tt["side"], side))
                if st.num_rows:
                    rows += _cells(st, {"shadow_pop": pop, "tier": tier, "side": side}, B, bootstrap_b, seed)
    cols = ["shadow_pop", "tier", "side", "bar_s", "rule", "n", "n_dates", "n_symbols"]
    cols += [f"{m}_{f}" for m in METRICS for f in ("value", "ci_lo", "ci_hi", "se")]
    cols += ["flagged_n", "flagged_share"]
    table = pa.Table.from_pylist([{c: r.get(c) for c in cols} for r in rows]) if rows else pa.table({})
    pq.write_table(table, out / TABLE, compression="zstd")
    return [d for d, _ in used]


def _cells(st: Any, labels: dict[str, str], B: Any, bootstrap_b: int, seed: int) -> list[dict[str, Any]]:
    """The rows of one (population, tier, side) slice: per (bar length, rule) every metric with its interval."""
    import pyarrow as pa
    import pyarrow.compute as pc

    sums = {f: st.column(f).to_numpy().astype(float) for f in SUMS}
    base = st.select(["session_date", "symbol", "bar_s", "rule"])
    cells: dict[tuple, dict[str, Any]] = {}
    for metric, (num, den) in METRICS.items():
        x = sum(-sums[f[1:]] if f.startswith("-") else sums[f] for f in num)
        mt = base.append_column("n_y", pa.array(x)).append_column("n", pa.array(sums[den]))
        for r in B.cluster_bootstrap(
            mt,
            metric="exact_fill_rate",
            clusters=("session_date", "symbol"),
            group=("bar_s", "rule"),
            bootstrap_b=bootstrap_b,
            seed=seed,
        ):
            row = cells.setdefault((r["bar_s"], r["rule"]), {**labels, "bar_s": r["bar_s"], "rule": r["rule"]})
            for f in ("value", "ci_lo", "ci_hi", "se"):
                row[f"{metric}_{f}"] = r[f]
            if metric == "real_fill_rate":
                row |= {"n_dates": r["n_session_date"], "n_symbols": r["n_symbol"]}
    flagged = st.filter(pc.invert(st["exact_ok"]))
    totals = {}
    for name, x in (("n", st), ("flagged_n", flagged)):
        g = x.group_by(["bar_s", "rule"], use_threads=False).aggregate([("n", "sum")]).to_pylist()
        totals[name] = {(r["bar_s"], r["rule"]): int(r["n_sum"]) for r in g}
    for key, row in cells.items():
        n, fn = totals["n"].get(key, 0), totals["flagged_n"].get(key, 0)
        row |= {"n": n, "flagged_n": fn, "flagged_share": fn / n if n else None}
    return [cells[k] for k in sorted(cells, key=lambda k: (k[0], _rule_order(k[1])))]


def _rule_order(rule: str) -> tuple[int, str]:
    return (0, rule) if rule == "touch" else (1, rule)


# ---- site ----------------------------------------------------------------------------------------------------------


def _fin(x: Any) -> float | None:
    return None if x is None or not math.isfinite(float(x)) else float(x)


def _est(r: dict[str, Any], m: str) -> dict[str, float | None]:
    return {"value": _fin(r.get(f"{m}_value")), "ci_lo": _fin(r.get(f"{m}_ci_lo")), "ci_hi": _fin(r.get(f"{m}_ci_hi"))}


def _pct(e: dict[str, Any]) -> str:
    return "n/a" if e["value"] is None else f"{100 * e['value']:.1f}%"


def _ticks(e: dict[str, Any]) -> str:
    return "n/a" if e["value"] is None else f"{e['value']:+.2f}"


def _iv(e: dict[str, Any], fmt: Any) -> str:
    if e["ci_lo"] is None or e["ci_hi"] is None:
        return "no interval"
    return f"{fmt({'value': e['ci_lo']})} to {fmt({'value': e['ci_hi']})}"


def _iso(d: str) -> str:
    return f"{d[4:]}-{d[:2]}-{d[2:4]}" if len(d) == 8 else d


def document(atlas: Path, *, population: str, source: str, attribution: str | None) -> dict[str, Any] | None:
    """The `atlas/v1/touch.json` document of `population`, validated by `queuetruth.atlas.touch.TouchTables`; None
    when the atlas has no touch tables."""
    import pyarrow.parquet as pq

    from queuetruth.atlas.touch import TouchTables

    path = atlas / TABLE
    if not path.exists() or not pq.read_metadata(path).num_rows:
        return None
    manifest = json.loads((atlas / "atlas_manifest.json").read_text(encoding="utf-8"))
    rows = [r for r in pq.read_table(path).to_pylist() if r["shadow_pop"] == population]
    if not rows:
        return None
    doc = TouchTables(
        atlas_root=manifest.get("atlas_root", ""),
        source=source,
        population=population,
        days=[_iso(d) for d in manifest.get("touch_days") or []],
        flag_policy=str(manifest.get("flag_policy")),
        definition=DOC,
        attribution=attribution,
        rows=[
            {
                "tier": r["tier"],
                "side": r["side"],
                "bar": bar_label(r["bar_s"]),
                "bar_s": r["bar_s"],
                "rule": r["rule"],
                "orders": r["n"],
                "dates": r["n_dates"] or 0,
                "symbols": r["n_symbols"] or 0,
                **{m: _est(r, m) for m in METRICS},
                "flagged_share": _fin(r["flagged_share"]),
            }
            for r in rows
        ],
    )
    return doc.model_dump(mode="json")


def section(atlas: Path, out: Path, *, population: str, source: str, attribution: str | None, docs: str) -> str:
    """The site's touch-table section (HTML) and `<out>/atlas/v1/touch.json`; "" and no file when the atlas has no
    touch tables, so a site without them is unchanged."""
    doc = document(atlas, population=population, source=source, attribution=attribution)
    if doc is None:
        return ""
    (out / "atlas" / "v1").mkdir(parents=True, exist_ok=True)
    (out / "atlas" / "v1" / "touch.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    at = {(r["tier"], r["side"], r["bar"], r["rule"]): r for r in doc["rows"]}
    bars = sorted({(r["bar_s"], r["bar"]) for r in doc["rows"]})
    rules = sorted({r["rule"] for r in doc["rows"]}, key=_rule_order)
    first = bars[0][1] if bars else "1m"
    e = at.get(("ALL", "all", first, "touch"))
    parts = ['<h2 id="bar-backtests">Bar-based backtests: how many touch fills are real</h2>']
    if e:
        lead = (
            f"With {first} bars, a backtest that fills a resting limit order when the bar's low or high touches its "
            f"price fills {_pct(e['rule_fill_rate'])} of orders by the close; the real queue fills "
            f"{_pct(e['real_fill_rate'])}. Of the backtest's fills, {_pct(e['p_real_given_rule'])} "
            f"({_iv(e['p_real_given_rule'], _pct)}) had really filled by the end of the same bar, and its P&L is "
            f"{_ticks(e['pnl_overstated_ticks'])} ticks per order too high ({_iv(e['pnl_overstated_ticks'], _ticks)})."
        )
        parts.append(f'<p class="lead">{html.escape(lead, quote=False)}</p>')
    days = doc["days"]
    parts.append(
        "<p>For the same orders resting until the close, what a candle backtester's fill rule says against the exact "
        "replay, all symbols and both sides. <em>P(real | rule)</em>: of the rule's fills, the share the real queue "
        "had filled by the end of the same bar. <em>P(rule | real)</em>: of the real fills, the share the rule had "
        "filled by then. Markouts are in ticks one bar after the fill; <em>P&amp;L too high</em> is the rule's "
        "markout P&amp;L minus the real one per order placed. 95% intervals, from the same bootstrap over dates and "
        f"symbols. Over {len(days)} day{'s' if len(days) != 1 else ''}; per tier and side in "
        '<a href="atlas/v1/touch.json">touch.json</a> '
        f'(<a href="{docs}/concepts/touch-tables.md">definitions and what is approximate</a>).</p>'
    )
    head = (
        "<tr><th>bar</th><th>rule</th><th>rule fills</th><th>real fills</th><th>P(real | rule)</th>"
        "<th>P(rule | real)</th><th>markout, rule</th><th>markout, real</th><th>P&amp;L too high</th></tr>"
    )

    def cell(x: dict[str, Any], fmt: Any) -> str:
        return f'<td>{fmt(x)}<br><span class="muted">{_iv(x, fmt)}</span></td>'

    body = []
    for _bar_s, bar in bars:
        for rule in rules:
            r = at.get(("ALL", "all", bar, rule))
            if r is None:
                continue
            body.append(
                f"<tr><td>{bar}</td><td>{html.escape(rule.replace('_', ' by '))}</td>"
                + cell(r["rule_fill_rate"], _pct)
                + cell(r["real_fill_rate"], _pct)
                + cell(r["p_real_given_rule"], _pct)
                + cell(r["p_rule_given_real"], _pct)
                + cell(r["rule_markout_ticks"], _ticks)
                + cell(r["real_markout_ticks"], _ticks)
                + cell(r["pnl_overstated_ticks"], _ticks)
                + "</tr>"
            )
    parts.append(f'<div class="wrap"><table><thead>{head}</thead><tbody>{"".join(body)}</tbody></table></div>')
    return "\n".join(parts)


__all__ = [
    "CELLS",
    "DEFAULT_SPEC",
    "KEYS",
    "METRICS",
    "SUMS",
    "TABLE",
    "bar_label",
    "combine",
    "document",
    "first_reach",
    "outcomes",
    "reduce_rows",
    "rule_names",
    "section",
    "stats",
    "unit_touch",
]
