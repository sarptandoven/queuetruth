"""Time-to-fill survival curves of a scorecard (hftbacktest discussion #332): Kaplan-Meier per arm on the same
orders, an interval at each horizon, and a log-rank test that respects the pairing and the clustering.

Event: the order's first maker fill inside its evaluation window W (the scorer's `{arm}_event_ns` with cause 1).
Censored: a cancel or expiry, a first fill as taker (cause 2), and the end of W (cause 0), each at its own time. So
`S(t)` is the share of orders still unfilled at `t` had none been cancelled; the scorer's `ttf.cif_fill` is the
other view, with cancels as a competing risk.

- Curves: `stats.kaplan_meier` per arm. At most `MAX_POINTS` points are stored; a longer curve is evaluated at
  `MAX_POINTS` log-spaced times (`curve_exact` false).
- Intervals: the scorer's block bootstrap (blocks = session dates, or 30-minute blocks with fewer than 5 dates),
  resampling orders so both arms move together. 95% percentile intervals for `S(h)` per arm at each `ttf_horizons_ns`
  value, for `S_sim(h) - S_live(h)` and for the median time to fill (null when `S` never reaches 0.5; the upper end
  is null when more than 2.5% of the replicates never reach it).
- Test: log-rank on the fill hazard, stratified by instrument, with a cluster-robust variance. Each order-arm `i`
  has the score residual `u_i = delta_i (z_i - zbar(T_i)) - sum_{t_k <= T_i} (z_i - zbar_k) d_k / n_k` (z = 1 for
  sim; `zbar_k`, `d_k`, `n_k` within its stratum), so `U = sum u_i` is the stratified log-rank score. The variance
  is `C / (C - 1) sum_c (sum_{i in c} u_i)^2` over clusters `c` = (instrument, bootstrap block): both arms of an order
  share a cluster, so the pairing is respected (Lee, Wei and Amato 1992). With fewer than 5 such clusters, each
  order is its own cluster and `method` says so. `U^2 / V` is chi-square with 1 df; with C clusters it is at most
  C - 1, so few clusters cannot give a very small p-value.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from queuetruth import stats

MAX_POINTS = 300
MIN_CLUSTERS = 5
Row = dict[str, Any]


def km_curve(time: np.ndarray, event: np.ndarray) -> Any:
    """Kaplan-Meier survival of time to fill (`stats.kaplan_meier`)."""
    return stats.kaplan_meier(np.asarray(time, dtype=float), np.asarray(event, dtype=bool))


def median_time(sf: Any) -> float | None:
    """First time with `S(t) <= 0.5`, or None when the curve stays above one half."""
    hit = np.nonzero(np.asarray(sf.value) <= 0.5)[0]
    return float(sf.time[hit[0]]) if hit.size else None


def curve_points(sf: Any, max_points: int = MAX_POINTS) -> tuple[list[list[float]], bool]:
    """`[[t_ns, S(t)], ...]` at each step, or at `max_points` log-spaced times for a longer curve."""
    t, v = np.asarray(sf.time, dtype=float), np.asarray(sf.value, dtype=float)
    if t.size <= max_points:
        return [[float(a), float(b)] for a, b in zip(t, v, strict=True)], True
    pos = t[t > 0]
    grid = np.unique(np.concatenate([[0.0], np.geomspace(max(pos.min(), 1.0), t.max(), max_points - 1)]))
    return [[float(a), float(sf.at(a))] for a in grid], False


def logrank_residuals(time: np.ndarray, event: np.ndarray, z: np.ndarray, strata: np.ndarray) -> np.ndarray:
    """Score residual `u_i` of each subject in the stratified log-rank (module doc); `sum(u)` is the score `U`."""
    t = np.asarray(time, dtype=float)
    e = np.asarray(event, dtype=bool)
    g = np.asarray(z, dtype=float)
    u = np.zeros(t.size)
    for s in np.unique(strata):
        m = np.nonzero(strata == s)[0]
        ts, es, zs = t[m], e[m], g[m]
        ut, inv = np.unique(ts, return_inverse=True)
        exits = np.bincount(inv, minlength=ut.size).astype(float)
        n_k = ts.size - np.concatenate([[0.0], np.cumsum(exits)[:-1]])
        z_exits = np.bincount(inv, weights=zs, minlength=ut.size)
        nz_k = zs.sum() - np.concatenate([[0.0], np.cumsum(z_exits)[:-1]])
        d_k = np.bincount(inv, weights=es.astype(float), minlength=ut.size)
        zbar = nz_k / n_k
        haz = d_k / n_k
        c_haz = np.cumsum(haz)
        c_zhaz = np.cumsum(zbar * haz)
        u[m] = es * (zs - zbar[inv]) - (zs * c_haz[inv] - c_zhaz[inv])
    return u


def clustered_logrank(
    time: np.ndarray, event: np.ndarray, z: np.ndarray, strata: np.ndarray, clusters: np.ndarray
) -> tuple[float | None, float | None]:
    """(chi2, p) of the stratified log-rank with the cluster-robust variance of the module doc."""
    u = logrank_residuals(time, event, z, strata)
    big_u = float(u.sum())
    _, ci = np.unique(clusters, return_inverse=True)
    per = np.bincount(ci, weights=u)
    k = per.size
    if k < 2:
        return None, None
    v = float(k / (k - 1) * np.sum(per**2))
    if v <= 0:
        return None, None
    chi2 = big_u * big_u / v
    return chi2, float(math.erfc(math.sqrt(chi2 / 2.0)))  # chi-square survival function, 1 df


def _fin(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _at(h: int, v: Any, ci: tuple[float | None, float | None], key: str) -> dict[str, Any]:
    return {"horizon_ns": int(h), key: _fin(v), "ci_lo": ci[0], "ci_hi": ci[1]}


def survival_part(
    orders: list[Row],
    events: dict[str, np.ndarray],
    horizons: list[int],
    blocks: np.ndarray,
    b: int,
    seed: int,
) -> dict[str, Any]:
    """The summary part `ttf_survival` from per-order (time, cause) arrays `events[arm]` (shape n x 2)."""
    n = len(orders)
    hs = sorted({int(h) for h in horizons})
    t_l, e_l = events["live"][:, 0], events["live"][:, 1] == 1
    t_s, e_s = events["sim"][:, 0], events["sim"][:, 1] == 1

    def vector(idx: np.ndarray) -> np.ndarray:
        out = []
        meds = []
        for t, e in ((t_l, e_l), (t_s, e_s)):
            sf = km_curve(t[idx], e[idx])
            out.append(np.asarray(sf.at(np.asarray(hs, dtype=float)), dtype=float).reshape(-1))
            md = median_time(sf)
            meds.append(np.nan if md is None else md)
        return np.concatenate([out[0], out[1], out[1] - out[0], np.asarray(meds)])

    acc: list[np.ndarray] = []

    def first(idx: np.ndarray) -> float:
        acc.append(vector(idx))
        return float(acc[-1][0]) if acc[-1].size else 0.0

    with np.errstate(all="ignore"):
        stats.block_replicates(blocks, first, b, stats.bootstrap_seed(seed, "ttf_survival"))
    est, reps = acc[0], np.vstack(acc[1:]) if len(acc) > 1 else np.empty((0, acc[0].size))

    def ci(j: int) -> tuple[float | None, float | None]:
        if reps.shape[0] == 0:
            return None, None
        lo, hi = stats.percentile_ci(reps[:, j])
        return _fin(lo), _fin(hi)

    k = len(hs)
    arms: dict[str, Any] = {}
    for idx_arm, (name, tt, ee) in enumerate((("live", t_l, e_l), ("sim", t_s, e_s))):
        sf = km_curve(tt, ee)
        pts, exact = curve_points(sf)
        mlo, mhi = ci(3 * k + idx_arm)
        if reps.shape[0] and np.mean(~np.isfinite(reps[:, 3 * k + idx_arm])) > 0.025:
            mhi = None  # more than 2.5% of the replicates never reach S = 0.5: the upper end is not bounded
        arms[name] = {
            "n_filled": int(ee.sum()),
            "n_censored": int((~ee).sum()),
            "median_ns": _fin(median_time(sf)),
            "median_ci_lo": mlo,
            "median_ci_hi": mhi,
            "at": [_at(h, est[idx_arm * k + j], ci(idx_arm * k + j), "survival") for j, h in enumerate(hs)],
            "curve": pts,
            "curve_exact": exact,
        }
    diff = [_at(h, est[2 * k + j], ci(2 * k + j), "diff") for j, h in enumerate(hs)]
    iid = np.array([int(o.get("instrument_id") or 0) for o in orders], dtype=np.int64)
    cl_pair = np.unique(np.stack([iid, np.asarray(blocks, dtype=np.int64)], axis=1), axis=0, return_inverse=True)[1]
    cl_pair = np.asarray(cl_pair).reshape(-1)
    if np.unique(cl_pair).size >= MIN_CLUSTERS:
        cl, method = cl_pair, "stratified by instrument; clusters = instrument x bootstrap block"
    else:
        cl, method = np.arange(n), "stratified by instrument; clusters = orders (fewer than 5 instrument blocks)"
    chi2, p = clustered_logrank(
        np.concatenate([t_l, t_s]),
        np.concatenate([e_l, e_s]),
        np.concatenate([np.zeros(n), np.ones(n)]),
        np.concatenate([iid, iid]),
        np.concatenate([cl, cl]),
    )
    return {
        "n": n,
        "horizons_ns": hs,
        "arms": arms,
        "diff": diff,
        "logrank": {
            "statistic": _fin(chi2),
            "p_value": _fin(p),
            "method": method,
            "n_clusters": int(np.unique(cl).size),
            "n_strata": int(np.unique(iid).size),
        },
    }
