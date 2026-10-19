"""Cluster-robust paired discordance test (audit ruling A10; SPEC 5.7.5 primary test).

Orders are paired (live arm, sim arm) binary outcomes grouped in clusters (session dates, or 30-minute blocks with
fewer than 5 dates; see `block_labels`). Per cluster `k`: `n_k` orders, `b_k` live-only, `c_k` sim-only;
`N = sum n_k`, `d = (c - b) / N` (sim fill rate minus live fill rate, the signed `fill_rate_bias`).
- Obuchowski (1998) cluster-adjusted McNemar: `var(d) = K / (K - 1) * sum_k ((c_k - b_k) - n_k d)^2 / N^2` (ratio
  estimator over the K non-empty clusters), `z = d / sqrt(var(d))`, `p = 2 * t.sf(|z|, K - 1)` (Student t with
  K - 1 df as the small-sample reference; it tends to Obuchowski's chi2(1) as K grows and keeps the size at the
  nominal level for K = 5..20, where chi2(1) rejects 8-12% under a day-clustered null); computed when `K >= 5`,
  else null.
- Block bootstrap of `d` (`block_counts` draws, seed used as given; callers pass
  `bootstrap_seed(seed, test_id)`): percentile CI and the two-sided bootstrap p (`bootstrap_p_two_sided`).
- Primary p-value: Obuchowski when `K >= 5`, else the block-bootstrap p. Plain exact McNemar is kept as the
  secondary test (it assumes independent orders).
- Effective sample size: `design_effect = var(d) / var_iid(d)` with `var_iid(d) = ((b + c) / N - d^2) / (N - 1)`
  (variance of the mean of the per-order differences in {-1, 0, 1}); `n_effective = N / design_effect`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats as sps

from queuetruth.stats._types import McNemarResult
from queuetruth.stats.paired import mcnemar
from queuetruth.stats.resampling import block_counts, bootstrap_p_two_sided, percentile_ci

MIN_CLUSTERS_OBUCHOWSKI = 5


@dataclass(frozen=True)
class ClusteredDiscordance:
    """Cluster-robust discordance test result (primary) with plain McNemar as secondary."""

    n: int
    b: int
    c: int
    estimate: float
    n_clusters: int
    n_clusters_discordant: int
    obuchowski_z: float | None
    p_obuchowski: float | None
    ci_lo: float
    ci_hi: float
    p_bootstrap: float
    p_primary: float
    primary_method: str
    design_effect: float | None
    n_effective: float | None
    mcnemar: McNemarResult


def obuchowski(b_k: np.ndarray, c_k: np.ndarray, n_k: np.ndarray) -> tuple[float, float] | None:
    """`(z, p)` of the Obuchowski cluster-adjusted McNemar test from per-cluster counts; None if undefined."""
    b_k, c_k, n_k = (np.asarray(v, dtype=float) for v in (b_k, c_k, n_k))
    keep = n_k > 0
    b_k, c_k, n_k = b_k[keep], c_k[keep], n_k[keep]
    k = n_k.size
    total = float(n_k.sum())
    if k < 2 or total == 0:
        return None
    d = float((c_k.sum() - b_k.sum()) / total)
    var = k / (k - 1) * float(np.sum(((c_k - b_k) - n_k * d) ** 2)) / total**2
    if var <= 0:
        return None
    z = d / np.sqrt(var)
    return float(z), float(2.0 * sps.t.sf(abs(z), k - 1))


def clustered_mcnemar(
    live_filled: np.ndarray,
    sim_filled: np.ndarray,
    clusters: np.ndarray,
    *,
    b: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> ClusteredDiscordance:
    """Primary cluster-robust discordance test of paired fill outcomes (see module docstring)."""
    live = np.asarray(live_filled, dtype=bool).ravel()
    sim = np.asarray(sim_filled, dtype=bool).ravel()
    cl = np.asarray(clusters).ravel()
    if not live.size == sim.size == cl.size:
        raise ValueError("live_filled, sim_filled and clusters must have the same length")
    n = live.size
    b_cnt = int(np.sum(live & ~sim))
    c_cnt = int(np.sum(~live & sim))
    secondary = mcnemar(b_cnt, c_cnt)
    nan = float("nan")
    if n == 0:
        return ClusteredDiscordance(0, 0, 0, nan, 0, 0, None, None, nan, nan, nan, nan, "none", None, None, secondary)
    _, inv = np.unique(cl, return_inverse=True)
    inv = np.asarray(inv).reshape(-1)
    k = int(inv.max()) + 1
    n_k = np.bincount(inv, minlength=k).astype(float)
    b_k = np.bincount(inv, weights=(live & ~sim).astype(float), minlength=k)
    c_k = np.bincount(inv, weights=(~live & sim).astype(float), minlength=k)
    d = (c_cnt - b_cnt) / n
    counts = block_counts(k, b, seed).astype(float)
    num = c_k - b_k
    rn = (counts * num[None, :]).sum(axis=1)
    rd = (counts * n_k[None, :]).sum(axis=1)
    reps = np.where(rd > 0, rn / np.where(rd > 0, rd, 1.0), np.nan)
    lo, hi = percentile_ci(reps, alpha)
    p_boot = bootstrap_p_two_sided(reps)
    ob = obuchowski(b_k, c_k, n_k) if k >= MIN_CLUSTERS_OBUCHOWSKI else None
    z, p_ob = ob if ob is not None else (None, None)
    deff = n_eff = None
    if ob is not None and n > 1:
        var_iid = ((b_cnt + c_cnt) / n - d * d) / (n - 1)
        var_cl = (d / z) ** 2 if z else None
        if var_iid > 0 and var_cl:
            deff = float(var_cl / var_iid)
            n_eff = float(n / deff)
    if p_ob is not None:
        primary, method = p_ob, "obuchowski"
    else:
        primary, method = p_boot, "block_bootstrap"
    return ClusteredDiscordance(
        n=n,
        b=b_cnt,
        c=c_cnt,
        estimate=float(d),
        n_clusters=k,
        n_clusters_discordant=int(np.sum((b_k + c_k) > 0)),
        obuchowski_z=z,
        p_obuchowski=p_ob,
        ci_lo=lo,
        ci_hi=hi,
        p_bootstrap=p_boot,
        p_primary=float(primary),
        primary_method=method,
        design_effect=deff,
        n_effective=n_eff,
        mcnemar=secondary,
    )
