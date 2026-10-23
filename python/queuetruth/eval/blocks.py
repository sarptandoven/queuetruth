"""Summable zoo statistics for sharded runs (W1 scale validation) and their evaluation.

A zoo run over thousands of instrument-days cannot keep `qt.zoo_predictions` (shadows x models x horizons rows per
mode). `block_stats` reduces the predictions of one shard to sums per cell and the evaluators work from those sums:

- `block_stats(predictions, planned_life, h_max_ns=...)`: the rows `evaluate_zoo` scores (the same `common_population`,
  `assign_strata`, planned-life eligibility and never-cancel / cancel-bearing populations, horizons `<= h_max_ns`),
  summed per (`model_id`, `population`, `stratum_kind` in all / queue_decile / liquidity_bucket, `stratum`,
  `horizon_ns`, `instrument_id`, `session_date`, `block` = 30-minute bucket of `ts_live` as `block_labels`, or 0
  with `block_ns=None`: whole instrument-days, for runs evaluated by `cluster_bootstrap` only). Fields:
  `n`, `sum_p`, `n_y`, `sum_d` = sum(p - y), `sum_sq` = sum((p - y)^2), `sum_ll` (log loss terms with p clipped to
  [1e-6, 1 - 1e-6]), `qty_n` / `qty_num` / `qty_den` (rows with a finite truth quantity: sum(e_fill_qty -
  truth_filled_qty), sum(qty)), `b` = truth-only and `c` = model-only discordant rows (model filled = p >= 0.5).
  Also `shadows` (distinct shadows per instrument-day and block: the block universe of the run) and `exact` (per
  model, horizon and instrument-day: predictions not `censored` with a truth label, and how many filled; the
  "exact fill rate" of `scripts/repro/breadth.py`). Every table carries the `extra` constant columns and, with
  `symbols`, a `symbol` column (locates are per day; the symbol is the cross-day identity).
- `evaluate_blocks(stats, shadows, h_max_ns=...)`: the `qt.zoo_errors` rows of `evaluate_zoo` for metrics `brier`,
  `logloss`, `fill_rate_bias`, `qty_bias` (every horizon), `overfill_bias`, `mcnemar_b`, `mcnemar_c` and
  `mcnemar_p_exact` (at `h_max_ns`), strata all / queue_decile / liquidity_bucket / instrument, as if the shards'
  shadows had been one zoo run: the blocks are the run's distinct labels (dates when it spans >= 5 dates, else 30-minute
  buckets), the draws `block_counts(n_blocks, bootstrap_b, bootstrap_seed(seed, "zoo_eval"))`, replicate
  `sum_k count_k num_k / sum_k count_k den_k`. Equal to `evaluate_zoo` on the concatenated predictions up to float
  summation order (`scripts/scale/prove_breadth.py` checks this on the 36-symbol breadth run). Not reproduced (they
  need row-level data): `fill_rate_bias_aj`, `ttf_*`, `qtrack_mae` and the cluster-robust discordance p-values.
- `cluster_bootstrap(stats, metric=..., clusters=("session_date", "symbol"))`: a metric per cell with a percentile
  CI from a pigeonhole bootstrap over the crossed clusters (Owen 2007): each replicate draws every cluster dimension
  with replacement (`block_counts`, stream `bootstrap_seed(seed, "scale_cluster:<dimension>")`) and weighs the
  cluster cell (d, s) by the product of its draw counts. One dimension gives the one-way cluster bootstrap. Also a
  two-sided bootstrap p-value against 0 (`bootstrap_p_two_sided`) for multiplicity control. A cell with a single
  cluster in every dimension gets no CI, SE or p-value.
- `shrink(estimate, se)`: DerSimonian-Laird random-effects mean, between-unit variance `tau2` and the empirical-Bayes
  posterior means `mu + tau2 / (tau2 + se^2) (estimate - mu)` (per-symbol estimates toward their tier mean).
- `bh(p)`: Benjamini-Hochberg adjusted p-values (`queuetruth.stats.benjamini_hochberg`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from queuetruth.eval import _cols as C
from queuetruth.eval.zoo import LOGLOSS_EPS, _planned_life, assign_strata, common_population
from queuetruth.stats import (
    benjamini_hochberg,
    block_counts,
    bootstrap_p_two_sided,
    bootstrap_seed,
    mcnemar,
    percentile_ci,
)
from queuetruth.stats.resampling import HALF_HOUR_NS, MIN_DAY_BLOCKS

KINDS = ("all", "queue_decile", "liquidity_bucket")
POPULATIONS = ("", "cancel_bearing/")
KEYS = ("model_id", "population", "stratum_kind", "stratum", "horizon_ns", "instrument_id", "session_date", "block")
SUMS = ("n", "sum_p", "n_y", "sum_d", "sum_sq", "sum_ll", "qty_n", "qty_num", "qty_den", "b", "c")
_INT_SUMS = ("n", "n_y", "qty_n", "b", "c")
#: metric -> (numerator, denominator) as functions of a dict of summed arrays
METRICS: dict[str, tuple[Any, Any]] = {
    "brier": (lambda s: s["sum_sq"], lambda s: s["n"]),
    "logloss": (lambda s: s["sum_ll"], lambda s: s["n"]),
    "fill_rate_bias": (lambda s: s["sum_d"], lambda s: s["n"]),
    "qty_bias": (lambda s: s["qty_num"], lambda s: s["qty_den"]),
    "overfill_bias": (lambda s: s["c"] - s["b"], lambda s: s["n"]),
    "exact_fill_rate": (lambda s: s["n_y"], lambda s: s["n"]),
    "model_fill_rate": (lambda s: s["sum_p"], lambda s: s["n"]),
}
BOOTSTRAP_TEST_ID = "zoo_eval"


def _with_extra(cols: dict[str, Any], n: int, extra: dict[str, Any] | None) -> pa.Table:
    t = pa.table(cols)
    for k, v in (extra or {}).items():
        t = t.append_column(k, pa.array([v] * n))
    return t


def _symbol_col(t: pa.Table, symbols: dict[int, str] | None) -> pa.Table:
    if symbols is None:
        return t
    ids = t.column("instrument_id").to_pylist()
    return t.append_column("symbol", pa.array([symbols.get(int(i), str(i)) for i in ids], pa.utf8()))


def block_stats(
    predictions: pa.Table,
    planned_life: pa.Table,
    *,
    h_max_ns: int,
    symbols: dict[int, str] | None = None,
    extra: dict[str, Any] | None = None,
    block_ns: int | None = HALF_HOUR_NS,
) -> dict[str, pa.Table]:
    """`{"stats", "shadows", "exact"}` of one shard's zoo predictions (module docstring). `block_ns=None` sums
    whole instrument-days (`block` 0): enough for the cluster bootstrap over days and symbols, about 13x fewer rows."""

    def blk(ts: np.ndarray) -> np.ndarray:
        return np.floor_divide(ts, block_ns) if block_ns else np.zeros(ts.size, dtype=np.int64)

    raw = predictions
    t = assign_strata(common_population(predictions))
    model = C.strings(t, "model_id")
    sid = C.strings(t, "shadow_id")
    h, _ = C.ints(t, "horizon_ns")
    p = C.floats(t, "p_fill")
    e_qty = C.floats(t, "e_fill_qty")
    qty = C.floats(t, "qty")
    y, yv = C.bools(t, "truth_filled")
    t_qty = C.floats(t, "truth_filled_qty")
    live, _ = C.ints(t, "ts_live")
    date, _ = C.ints(t, "session_date")
    inst, _ = C.ints(t, "instrument_id")
    dec, decv = C.ints(t, "queue_decile")
    liq, liqv = C.ints(t, "liquidity_bucket")
    out: dict[str, pa.Table] = {}

    # shadows: the block universe (distinct shadows of the evaluated rows)
    if sid.size:
        ids, first, sid_inv = np.unique(sid, return_index=True, return_inverse=True)
        sid_inv = np.asarray(sid_inv).reshape(-1)
        blk_shadow = blk(live[first])
        sk = np.stack([inst[first], date[first], blk_shadow], axis=1)
        su, scount = np.unique(sk, axis=0, return_counts=True)
        cancel_rel, admin_rel = _planned_life(planned_life, ids, live[first])
    else:
        sid_inv = np.zeros(0, dtype=np.int64)
        su, scount = np.zeros((0, 3), dtype=np.int64), np.zeros(0, dtype=np.int64)
        cancel_rel = admin_rel = np.zeros(0)
    shadows = {
        "instrument_id": pa.array(su[:, 0], pa.uint32()),
        "session_date": pa.array(su[:, 1].astype(np.int32), pa.int32()),
        "block": pa.array(su[:, 2], pa.int64()),
        "n_shadows": pa.array(scount, pa.int64()),
    }
    out["shadows"] = _symbol_col(_with_extra(shadows, len(su), extra), symbols)

    # exact fill counts of breadth.py: raw predictions, not censored, truth defined
    rm = C.strings(raw, "model_id")
    rh, _ = C.ints(raw, "horizon_ns")
    ri, _ = C.ints(raw, "instrument_id")
    rd, _ = C.ints(raw, "session_date")
    ry, ryv = C.bools(raw, "truth_filled")
    rc, rcv = C.bools(raw, "censored")
    defined = ryv & ~(rc & rcv)
    mods, mcode = np.unique(rm, return_inverse=True) if rm.size else (np.zeros(0, dtype=object), np.zeros(0, int))
    ek = np.stack([np.asarray(mcode).reshape(-1), rh, ri, rd], axis=1)[defined]
    if ek.size:
        eu, einv = np.unique(ek, axis=0, return_inverse=True)
        einv = np.asarray(einv).reshape(-1)
        nd = np.bincount(einv, minlength=len(eu))
        nf = np.bincount(einv, weights=ry[defined].astype(float), minlength=len(eu)).astype(np.int64)
    else:
        eu, nd, nf = np.zeros((0, 4), dtype=np.int64), np.zeros(0, np.int64), np.zeros(0, np.int64)
    exact = {
        "model_id": pa.array([str(mods[i]) for i in eu[:, 0]], pa.utf8()),
        "horizon_ns": pa.array(eu[:, 1], pa.int64()),
        "instrument_id": pa.array(eu[:, 2], pa.uint32()),
        "session_date": pa.array(eu[:, 3].astype(np.int32), pa.int32()),
        "n_defined": pa.array(nd, pa.int64()),
        "n_filled": pa.array(nf, pa.int64()),
    }
    out["exact"] = _symbol_col(_with_extra(exact, len(eu), extra), symbols)

    # cell sums of the scored rows
    eligible = admin_rel[sid_inv] >= h.astype(float)
    never_cancel = cancel_rel >= float(h_max_ns)
    usable = yv & eligible & (h <= h_max_ns)
    pop_code = np.where(never_cancel[sid_inv], 0, 1) if sid.size else np.zeros(0, int)
    yf = y.astype(float)
    pcl = np.clip(p, LOGLOSS_EPS, 1.0 - LOGLOSS_EPS)
    ll = -(yf * np.log(pcl) + (1.0 - yf) * np.log(1.0 - pcl))
    tq_ok = np.isfinite(t_qty)
    filled = p >= 0.5
    rows_all = np.flatnonzero(usable)
    rows_dec = np.flatnonzero(usable & decv)
    rows_liq = np.flatnonzero(usable & liqv)
    idx = np.concatenate([rows_all, rows_dec, rows_liq])
    kind = np.concatenate([np.zeros(rows_all.size), np.ones(rows_dec.size), np.full(rows_liq.size, 2)]).astype(int)
    stratum = np.concatenate([np.zeros(rows_all.size, int), dec[rows_dec], liq[rows_liq]])
    models, mcode = np.unique(model, return_inverse=True) if model.size else (model, np.zeros(0, int))
    mcode = np.asarray(mcode).reshape(-1)
    key = np.stack(
        [
            mcode[idx],
            pop_code[idx],
            kind,
            stratum,
            h[idx],
            inst[idx],
            date[idx],
            blk(live[idx]),
        ],
        axis=1,
    ).astype(np.int64)
    if key.size:
        # one int64 per row from per-column codes (fast 1-D unique instead of a row-wise one)
        cols = [np.unique(key[:, j], return_inverse=True) for j in range(key.shape[1])]
        flat = np.ravel_multi_index(tuple(np.asarray(c[1]).reshape(-1) for c in cols), [len(c[0]) for c in cols])
        fu, kinv = np.unique(flat, return_inverse=True)
        kinv = np.asarray(kinv).reshape(-1)
        ku = np.stack(
            [c[0][i] for c, i in zip(cols, np.unravel_index(fu, [len(c[0]) for c in cols]), strict=True)], axis=1
        )
    else:
        ku, kinv = np.zeros((0, 8), dtype=np.int64), np.zeros(0, dtype=np.int64)
    m = len(ku)

    def s(w: np.ndarray) -> np.ndarray:
        return np.bincount(kinv, weights=w[idx].astype(float), minlength=m)

    d = p - yf
    sums = {
        "n": np.bincount(kinv, minlength=m),
        "sum_p": s(p),
        "n_y": s(yf),
        "sum_d": s(d),
        "sum_sq": s(d * d),
        "sum_ll": s(ll),
        "qty_n": s(tq_ok.astype(float)),
        "qty_num": s(np.where(tq_ok, e_qty - t_qty, 0.0)),
        "qty_den": s(np.where(tq_ok, qty, 0.0)),
        "b": s(y & ~filled),
        "c": s(filled & ~y),
    }
    cols: dict[str, Any] = {
        "model_id": pa.array([str(models[i]) for i in ku[:, 0]], pa.utf8()),
        "population": pa.array([POPULATIONS[i] for i in ku[:, 1]], pa.utf8()),
        "stratum_kind": pa.array([KINDS[i] for i in ku[:, 2]], pa.utf8()),
        "stratum": pa.array([str(v) if k else "all" for k, v in zip(ku[:, 2], ku[:, 3], strict=True)], pa.utf8()),
        "horizon_ns": pa.array(ku[:, 4], pa.int64()),
        "instrument_id": pa.array(ku[:, 5], pa.uint32()),
        "session_date": pa.array(ku[:, 6].astype(np.int32), pa.int32()),
        "block": pa.array(ku[:, 7], pa.int64()),
    }
    for f in SUMS:
        v = sums[f]
        cols[f] = pa.array(np.rint(v).astype(np.int64) if f in _INT_SUMS else v.astype(float))
    out["stats"] = _symbol_col(_with_extra(cols, m, extra), symbols)
    return out


# ---- evaluation --------------------------------------------------------------------------------------------------


def _np(t: pa.Table, name: str) -> np.ndarray:
    col = t.column(name)
    if pa.types.is_string(col.type) or pa.types.is_large_string(col.type) or pa.types.is_dictionary(col.type):
        return C.strings(t, name)
    return np.asarray(col.to_numpy())


def _codes(t: pa.Table, name: str) -> tuple[np.ndarray, np.ndarray]:
    """(code of every row, sorted distinct values): Arrow dictionary encoding for strings, `np.unique` otherwise."""
    col = t.column(name)
    if pa.types.is_string(col.type) or pa.types.is_large_string(col.type) or pa.types.is_dictionary(col.type):
        enc = pc.dictionary_encode(col.cast(pa.utf8()) if pa.types.is_dictionary(col.type) else col)
        enc = enc.combine_chunks() if isinstance(enc, pa.ChunkedArray) else enc
        values = np.array(enc.dictionary.to_pylist(), dtype=object)
        order = np.argsort(values.astype(str), kind="stable")
        rank = np.empty(order.size, dtype=np.int64)
        rank[order] = np.arange(order.size)
        idx = np.asarray(pc.fill_null(enc.indices, 0).to_numpy(), dtype=np.int64)
        return rank[idx], values[order]
    u, inv = np.unique(np.asarray(col.to_numpy()), return_inverse=True)
    return np.asarray(inv).reshape(-1), u


def _groups(t: pa.Table, names: Sequence[str]) -> tuple[list[tuple[Any, ...]], np.ndarray]:
    """Distinct key tuples (sorted) and the group index of every row."""
    if t.num_rows == 0:
        return [], np.zeros(0, dtype=np.int64)
    codes, uniq = [], []
    for n in names:
        c, u = _codes(t, n)
        codes.append(c)
        uniq.append(u)
    mat = np.stack(codes, axis=1)
    gu, ginv = np.unique(mat, axis=0, return_inverse=True)
    keys = [tuple(_py(uniq[j][row[j]]) for j in range(len(names))) for row in gu]
    return keys, np.asarray(ginv).reshape(-1)


def _py(v: Any) -> Any:
    return v.item() if isinstance(v, np.generic) else v


def _block_index(stats: pa.Table, shadows: pa.Table) -> tuple[np.ndarray, int]:
    """Block index of every stats row among the run's distinct block labels (`block_labels` semantics)."""
    sd = _np(shadows, "session_date").astype(np.int64)
    if np.unique(sd).size >= MIN_DAY_BLOCKS:
        universe = np.unique(sd)
        lab = _np(stats, "session_date").astype(np.int64)
    else:
        universe = np.unique(_np(shadows, "block").astype(np.int64))
        lab = _np(stats, "block").astype(np.int64)
    pos = np.searchsorted(universe, lab)
    if lab.size and (np.any(pos >= universe.size) or np.any(universe[np.minimum(pos, universe.size - 1)] != lab)):
        raise ValueError("stats rows in a block that no shadow of the run occupies")
    return pos, int(universe.size)


def evaluate_blocks(
    stats: pa.Table, shadows: pa.Table, *, h_max_ns: int, bootstrap_b: int = 1000, seed: int = 0
) -> list[dict[str, Any]]:
    """`qt.zoo_errors`-shaped rows (dicts) of one run from its shards' summed cells (module docstring)."""
    if stats.num_rows == 0:
        return []
    blocks, nb = _block_index(stats, shadows)
    counts = block_counts(nb, bootstrap_b, bootstrap_seed(seed, BOOTSTRAP_TEST_ID)).astype(float)
    vals = {f: _np(stats, f).astype(float) for f in SUMS}
    kind, kinds = _codes(stats, "stratum_kind")
    inst_rows = kinds[kind] == "all"
    # the instrument stratum is the `all` cells split by instrument
    t_main = stats
    t_inst = stats.filter(pa.array(inst_rows))
    inst_keys = ["model_id", "population", "instrument_id", "horizon_ns"]
    out: list[dict[str, Any]] = []
    for table, names, is_inst in (
        (t_main, ["model_id", "population", "stratum_kind", "stratum", "horizon_ns"], False),
        (t_inst, inst_keys, True),
    ):
        sel = inst_rows if is_inst else np.ones(stats.num_rows, dtype=bool)
        keys, ginv = _groups(table, names)
        bsel = blocks[sel]
        v = {f: x[sel] for f, x in vals.items()}
        order = np.argsort(ginv, kind="stable")
        bounds = np.cumsum(np.bincount(ginv, minlength=len(keys)))
        starts = np.concatenate([[0], bounds[:-1]])
        for g, key in enumerate(keys):
            rows = order[starts[g] : bounds[g]]
            if is_inst:
                model_id, pop, inst, hz = key
                skind, stratum = "instrument", str(inst)
            else:
                model_id, pop, skind, stratum, hz = key
            bs = bsel[rows]
            per_block = {f: np.bincount(bs, weights=v[f][rows], minlength=nb) for f in SUMS}
            n = round(per_block["n"].sum())
            if n == 0:
                continue
            emit = ["brier", "logloss", "fill_rate_bias"]
            if per_block["qty_n"].sum() > 0:
                emit.append("qty_bias")
            if int(hz) == h_max_ns:
                emit.append("overfill_bias")
            for metric in emit:
                num_f, den_f = METRICS[metric]
                num, den = num_f(per_block), den_f(per_block)
                total = float(den.sum())
                value = float(num.sum()) / total if total > 0 else float("nan")
                rn, rd = counts @ num, counts @ den
                reps = np.where(rd > 0, rn / np.where(rd > 0, rd, 1.0), np.nan)
                lo, hi = percentile_ci(reps, 0.05)
                out.append(_row(model_id, pop + metric, int(hz), skind, stratum, value, lo, hi, n))
            if int(hz) == h_max_ns:
                b, c = round(per_block["b"].sum()), round(per_block["c"].sum())
                mc = mcnemar(b, c)
                nan = float("nan")
                out.append(_row(model_id, pop + "mcnemar_b", int(hz), skind, stratum, float(b), nan, nan, n))
                out.append(_row(model_id, pop + "mcnemar_c", int(hz), skind, stratum, float(c), nan, nan, n))
                out.append(_row(model_id, pop + "mcnemar_p_exact", int(hz), skind, stratum, mc.p_exact, nan, nan, n))
    return out


def _row(m: str, metric: str, hz: int, kind: str, stratum: str, v: float, lo: float, hi: float, n: int) -> dict:
    fin = lambda x: float(x) if x is not None and np.isfinite(x) else None  # noqa: E731
    return {
        "model_id": m,
        "metric": metric,
        "horizon_ns": hz,
        "stratum_kind": kind,
        "stratum": stratum,
        "value": fin(v),
        "ci_lo": fin(lo),
        "ci_hi": fin(hi),
        "n": n,
    }


def cluster_bootstrap(
    stats: pa.Table,
    *,
    metric: str = "fill_rate_bias",
    clusters: Sequence[str] = ("session_date", "symbol"),
    group: Sequence[str] = ("model_id", "population", "stratum_kind", "stratum", "horizon_ns"),
    bootstrap_b: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> list[dict[str, Any]]:
    """`metric` per `group` cell with a (multi-way) pigeonhole cluster bootstrap CI (module docstring)."""
    if metric not in METRICS:
        raise ValueError(f"metric: one of {sorted(METRICS)}")
    if stats.num_rows == 0:
        return []
    num_f, den_f = METRICS[metric]
    vals = {f: _np(stats, f).astype(float) for f in SUMS}
    num_all, den_all = num_f(vals), den_f(vals)
    codes, sizes = [], []
    for c in clusters:
        inv, u = _codes(stats, c)
        codes.append(inv)
        sizes.append(len(u))
    draws = [
        block_counts(k, bootstrap_b, bootstrap_seed(seed, f"scale_cluster:{c}")).astype(float)
        for c, k in zip(clusters, sizes, strict=True)
    ]
    keys, ginv = _groups(stats, group)
    order = np.argsort(ginv, kind="stable")
    bounds = np.cumsum(np.bincount(ginv, minlength=len(keys)))
    starts = np.concatenate([[0], bounds[:-1]])
    out: list[dict[str, Any]] = []
    for g, key in enumerate(keys):
        rows = order[starts[g] : bounds[g]]
        num, den = num_all[rows], den_all[rows]
        total = float(den.sum())
        value = float(num.sum()) / total if total > 0 else float("nan")
        # collapse to the occupied cluster cells, then weigh each by the product of its draw counts
        cell = np.stack([c[rows] for c in codes], axis=1)
        cu, cinv = np.unique(cell, axis=0, return_inverse=True)
        cinv = np.asarray(cinv).reshape(-1)
        cn = np.bincount(cinv, weights=num, minlength=len(cu))
        cd = np.bincount(cinv, weights=den, minlength=len(cu))
        w = np.ones((bootstrap_b, len(cu)))
        for j, dr in enumerate(draws):
            w *= dr[:, cu[:, j]]
        rn, rd = w @ cn, w @ cd
        reps = np.where(rd > 0, rn / np.where(rd > 0, rd, 1.0), np.nan)
        lo, hi = percentile_ci(reps, alpha)
        finite = reps[np.isfinite(reps)]
        if all(np.unique(cu[:, j]).size < 2 for j in range(len(clusters))):
            lo = hi = float("nan")  # one cluster: no between-cluster variation to resample
            finite = finite[:0]
            reps = np.full(reps.size, np.nan)
        row = dict(zip(group, key, strict=True))
        row.update(
            {
                "metric": metric,
                "value": value if np.isfinite(value) else None,
                "ci_lo": lo if np.isfinite(lo) else None,
                "ci_hi": hi if np.isfinite(hi) else None,
                "se": float(np.std(finite, ddof=1)) if finite.size > 1 else None,
                "p_boot": bootstrap_p_two_sided(reps) if metric not in ("exact_fill_rate", "model_fill_rate") else None,
                "n": round(float(_np(stats, "n")[rows].sum())),
                **{f"n_{c}": int(np.unique(cu[:, j]).size) for j, c in enumerate(clusters)},
            }
        )
        out.append(row)
    return out


def shrink(estimate: Sequence[float], se: Sequence[float]) -> dict[str, Any]:
    """DerSimonian-Laird `mu`, `tau2` and empirical-Bayes posterior means (NaN inputs pass through as NaN)."""
    x = np.asarray(estimate, dtype=float)
    s = np.asarray(se, dtype=float)
    ok = np.isfinite(x) & np.isfinite(s) & (s > 0)
    post = np.full(x.size, np.nan)
    if ok.sum() < 2:
        post[ok] = x[ok]
        return {"mu": float(x[ok].mean()) if ok.any() else float("nan"), "tau2": 0.0, "posterior": post}
    w = 1.0 / s[ok] ** 2
    mu_fe = float(np.sum(w * x[ok]) / np.sum(w))
    q = float(np.sum(w * (x[ok] - mu_fe) ** 2))
    denom = float(np.sum(w) - np.sum(w**2) / np.sum(w))
    tau2 = max(0.0, (q - (ok.sum() - 1)) / denom) if denom > 0 else 0.0
    wr = 1.0 / (s[ok] ** 2 + tau2)
    mu = float(np.sum(wr * x[ok]) / np.sum(wr))
    post[ok] = mu + tau2 / (tau2 + s[ok] ** 2) * (x[ok] - mu)
    return {"mu": mu, "tau2": tau2, "posterior": post}


def bh(p: Sequence[float]) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values (NaN stays NaN and is not counted)."""
    return benjamini_hochberg(np.asarray(p, dtype=float))


def concat(tables: Sequence[pa.Table]) -> pa.Table:
    """Concatenates shard tables (schemas unified; missing extra columns become null)."""
    ts = [t for t in tables if t is not None]
    if not ts:
        return pa.table({})
    return pa.concat_tables(ts, promote_options="default")


__all__ = [
    "KEYS",
    "METRICS",
    "SUMS",
    "bh",
    "block_stats",
    "cluster_bootstrap",
    "concat",
    "evaluate_blocks",
    "shrink",
]
