"""Paired inference for forecast losses: Diebold-Mariano with HAC errors and the question-clustered bootstrap.

Both work on per-question loss pairs of a subject and a reference forecaster, `d = loss_reference -
loss_subject` (positive = the subject is better):

- `dm_test(d, lag, clusters)`: `DM = mean(d) / sqrt(LRV / n)` with the Newey-West (Bartlett) long-run variance
  `LRV = g_0 + 2 sum_{l=1..L} (1 - l / (L + 1)) g_l`, `g_l = sum_{t>=l} (d_t - dbar)(d_{t-l} - dbar) / n`, times the
  Harvey-Leybourne-Newbold factor `sqrt((n + 1 - 2h + h(h - 1) / n) / n)` with `h = L + 1`, against Student t with
  `n - 1` df (two-sided). With `L = 0` this is exactly the one-sample t test of `d`. The default lag is the
  Newey-West rule `floor(4 (n / 100)^(2/9))`. The pairs must be in time order (resolution time) for the lags to
  mean serial correlation. When `clusters` puts more than one row in some cluster, the same formulas run on the
  `G` cluster sums `u_g = sum_{i in g} (d_i - dbar)`, ordered by each cluster's last row:
  `Var(dbar) = G LRV_u / n^2`, the HLN factor and the default lag use `G`, and the t distribution has `G - 1` df.
  With `L = 0` that is the CR1 cluster-robust t test; with one row per cluster it is the row-level test above.
  Rows of one cluster (questions of one group, or several forecasts of one question) are never counted as
  independent evidence.
- `paired_cluster_bootstrap`: resamples whole clusters (questions, or question groups) with replacement, so the
  replicates of ratio statistics are `counts @ sums / counts @ sizes`; percentile CIs and the two-sided bootstrap
  p-value of `queuetruth.stats` (H0: mean_diff = 0). `cluster_count_chunks` yields the multiplicities of
  `queuetruth.stats.block_counts` (same generator, same draws) in row chunks of at most `CHUNK_CELLS` cells, so
  memory stays bounded for any number of clusters.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
from scipy import stats as sps

from queuetruth.stats import bootstrap_p_two_sided, bootstrap_seed, percentile_ci

#: Largest replicate-by-cluster block held in memory at once (float64 cells).
CHUNK_CELLS = 4_000_000


def cluster_count_chunks(n_clusters: int, b: int, seed: int) -> Iterator[np.ndarray]:
    """`(rows, n_clusters)` float multiplicity blocks whose concatenation equals
    `queuetruth.stats.block_counts(n_clusters, b, seed)`."""
    if b <= 0:
        return
    rng = np.random.default_rng(seed)
    step = max(1, CHUNK_CELLS // max(n_clusters, 1))
    done = 0
    while done < b:
        c = min(step, b - done)
        draws = rng.integers(0, n_clusters, size=(c, n_clusters))
        flat = (np.arange(c, dtype=np.int64)[:, None] * n_clusters + draws).ravel()
        yield np.bincount(flat, minlength=c * n_clusters).reshape(c, n_clusters).astype(float)
        done += c


def newey_west_lag(n: int) -> int:
    """`floor(4 (n / 100)^(2/9))`, the Newey-West (1994) bandwidth rule (0 for n < 1)."""
    if n < 1:
        return 0
    return math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))


def long_run_variance(d: np.ndarray, lag: int) -> float:
    """Bartlett-kernel long-run variance of `d` (divisor n, as Newey-West)."""
    x = np.asarray(d, dtype=float)
    n = x.size
    if n == 0:
        return float("nan")
    e = x - x.mean()
    lrv = float(e @ e) / n
    for lg in range(1, min(lag, n - 1) + 1):
        lrv += 2.0 * (1.0 - lg / (lag + 1.0)) * float(e[lg:] @ e[:-lg]) / n
    return lrv


@dataclass(frozen=True)
class DmResult:
    """Diebold-Mariano statistic (HLN-corrected), two-sided p-value, the lag used and the independent units
    (rows, or clusters when a cluster holds more than one row; the t test has `units - 1` df)."""

    statistic: float
    p_value: float
    lag: int
    units: int = 0


def dm_test(d: np.ndarray, lag: int | None = None, clusters: np.ndarray | None = None) -> DmResult:
    """Diebold-Mariano test of `E[d] = 0` (see the module docstring); NaN for fewer than 2 units, and for
    differences with zero variance that are not all zero (constant differences carry no evidence of a spread)."""
    x = np.asarray(d, dtype=float).ravel()
    n = x.size
    if n and not np.all(np.isfinite(x)):
        return DmResult(float("nan"), float("nan"), newey_west_lag(n) if lag is None else int(lag), n)
    if lag is not None and int(lag) < 0:
        raise ValueError("lag must be >= 0")
    dbar = float(x.mean()) if n else float("nan")
    series = x
    if clusters is not None and n:
        cl = np.asarray(clusters, dtype=np.int64).ravel()
        if cl.size != n:
            raise ValueError("clusters must have one entry per difference")
        g = int(cl.max()) + 1
        size = np.bincount(cl, minlength=g)
        if size.max() > 1:
            sums = np.bincount(cl, weights=x - dbar, minlength=g)
            last = np.full(g, -1, dtype=np.int64)
            last[cl] = np.arange(n)  # later rows overwrite: the position of each cluster's last row
            present = np.flatnonzero(size > 0)
            series = sums[present[np.argsort(last[present], kind="stable")]]
    units = int(series.size)
    lg = newey_west_lag(units) if lag is None else int(lag)
    if units < 2:
        return DmResult(float("nan"), float("nan"), lg, units)
    lg = min(lg, units - 1)
    clustered = series is not x
    # the unit series has mean 0 when clustered (sums of deviations), so long_run_variance centres it harmlessly
    lrv = long_run_variance(series, lg)
    h = lg + 1
    k2 = (units + 1.0 - 2.0 * h + h * (h - 1.0) / units) / units
    if k2 <= 0:
        return DmResult(float("nan"), float("nan"), lg, units)
    if not lrv > 0:  # zero variance (or a non-positive HAC estimate)
        if lrv == 0 and dbar == 0:
            return DmResult(0.0, 1.0, lg, units)
        return DmResult(float("nan"), float("nan"), lg, units)
    var_mean = units * lrv / (n * n) if clustered else lrv / n
    stat = math.sqrt(k2) * dbar / math.sqrt(var_mean)
    return DmResult(stat, float(2.0 * sps.t.sf(abs(stat), df=units - 1)), lg, units)


def cluster_index(labels: list[str] | np.ndarray) -> tuple[np.ndarray, int]:
    """Integer cluster index per row (rank of its label among the sorted distinct labels) and the count."""
    arr = np.asarray(labels, dtype=object).astype(str)
    uniq, inv = np.unique(arr, return_inverse=True)
    return inv.astype(np.int64), int(uniq.size)


@dataclass(frozen=True)
class PairedBootstrap:
    """Cluster-bootstrap results of a paired loss comparison."""

    n: int
    n_clusters: int
    mean_diff: float
    mean_diff_ci: tuple[float, float]
    skill: float
    skill_ci: tuple[float, float]
    p_value: float
    se: float


def paired_cluster_bootstrap(
    loss_subject: np.ndarray,
    loss_reference: np.ndarray,
    clusters: np.ndarray,
    b: int,
    seed: int,
    test_id: str,
    alpha: float = 0.05,
) -> PairedBootstrap:
    """Mean loss difference (reference - subject) and skill `1 - mean(subject) / mean(reference)` with CIs by
    resampling clusters; `clusters` is an integer index per row (`cluster_index`)."""
    ls = np.asarray(loss_subject, dtype=float)
    lr = np.asarray(loss_reference, dtype=float)
    cl = np.asarray(clusters, dtype=np.int64)
    n = ls.size
    nan2 = (float("nan"), float("nan"))
    if n == 0:
        return PairedBootstrap(0, 0, float("nan"), nan2, float("nan"), nan2, float("nan"), float("nan"))
    g = int(cl.max()) + 1
    s_sub = np.bincount(cl, weights=ls, minlength=g)
    s_ref = np.bincount(cl, weights=lr, minlength=g)
    size = np.bincount(cl, minlength=g).astype(float)
    keep = size > 0
    s_sub, s_ref, size = s_sub[keep], s_ref[keep], size[keep]
    g = int(size.size)
    mean_diff = float((s_ref.sum() - s_sub.sum()) / size.sum())
    skill = float(1.0 - s_sub.sum() / s_ref.sum()) if s_ref.sum() > 0 else float("nan")
    if b <= 0 or g < 2:
        return PairedBootstrap(n, g, mean_diff, nan2, skill, nan2, float("nan"), float("nan"))
    diffs, skills = [], []
    for counts in cluster_count_chunks(g, b, bootstrap_seed(seed, test_id)):
        diffs.append((counts @ (s_ref - s_sub)) / (counts @ size))
        rs = counts @ s_ref
        with np.errstate(divide="ignore", invalid="ignore"):
            skills.append(np.where(rs > 0, 1.0 - (counts @ s_sub) / rs, np.nan))
    rep_diff, rep_skill = np.concatenate(diffs), np.concatenate(skills)
    finite = rep_diff[np.isfinite(rep_diff)]
    se = float(np.std(finite, ddof=1)) if finite.size > 1 else float("nan")
    return PairedBootstrap(
        n=n,
        n_clusters=g,
        mean_diff=mean_diff,
        mean_diff_ci=percentile_ci(rep_diff, alpha),
        skill=skill,
        skill_ci=percentile_ci(rep_skill, alpha),
        p_value=bootstrap_p_two_sided(rep_diff, 0.0),
        se=se,
    )


def mean_cluster_ci(
    values: np.ndarray, clusters: np.ndarray, b: int, seed: int, test_id: str, alpha: float = 0.05
) -> tuple[float, float, float]:
    """`(mean, lo, hi)` of a per-row value with a cluster-bootstrap percentile CI (NaN CI with < 2 clusters)."""
    v = np.asarray(values, dtype=float)
    cl = np.asarray(clusters, dtype=np.int64)
    if v.size == 0:
        return float("nan"), float("nan"), float("nan")
    g = int(cl.max()) + 1
    s = np.bincount(cl, weights=v, minlength=g)
    size = np.bincount(cl, minlength=g).astype(float)
    keep = size > 0
    s, size = s[keep], size[keep]
    est = float(s.sum() / size.sum())
    if b <= 0 or size.size < 2:
        return est, float("nan"), float("nan")
    reps = [(c @ s) / (c @ size) for c in cluster_count_chunks(int(size.size), b, bootstrap_seed(seed, test_id))]
    lo, hi = percentile_ci(np.concatenate(reps), alpha)
    return est, lo, hi


def design_effect(d: np.ndarray, clusters: np.ndarray) -> float:
    """Variance inflation of a mean from clustering: cluster-robust over i.i.d. variance of `mean(d)`, at least 1
    (exactly 1 when every cluster has one row)."""
    x = np.asarray(d, dtype=float)
    cl = np.asarray(clusters, dtype=np.int64)
    n = x.size
    if n < 2:
        return 1.0
    g = int(cl.max()) + 1
    size = np.bincount(cl, minlength=g)
    if size.max() <= 1:
        return 1.0
    e = x - x.mean()
    v_iid = float(e @ e) / (n - 1) / n
    sums = np.bincount(cl, weights=e, minlength=g)
    v_cl = float(sums @ sums) / (n * n)
    if not v_iid > 0:
        return 1.0
    return max(1.0, v_cl / v_iid)


__all__ = [
    "CHUNK_CELLS",
    "DmResult",
    "PairedBootstrap",
    "cluster_count_chunks",
    "cluster_index",
    "design_effect",
    "dm_test",
    "long_run_variance",
    "mean_cluster_ci",
    "newey_west_lag",
    "paired_cluster_bootstrap",
]
