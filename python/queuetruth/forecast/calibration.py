"""Reliability diagrams and the expected calibration error (ECE) of probability forecasts.

`bins`: K equal-width bins on [0, 1], `k = min(floor(p K), K - 1)` (so p = 1 falls in the last bin).
`ece = sum_k (n_k / N) |obar_k - pbar_k| = |sum over bin k of (y - p)| summed over bins / N`.

ECE is biased upward in small samples: a perfectly calibrated forecaster has ECE > 0. The CI is therefore the
basic (bias-reflected) cluster-bootstrap interval `[2 ece - q_hi, 2 ece - q_lo]` (clipped at 0), not the percentile
interval of the plug-in, which would sit almost entirely above the estimate. Next to it the diagram reports, from `B`
draws of outcomes under perfect calibration (consistency resampling, Brocker and Smith 2007): the mean ECE of a
calibrated forecaster with the same forecasts (`ece_if_calibrated`), the p-value `(1 + #{ECE* >= ECE}) / (1 + B)` of
the hypothesis of perfect calibration, and per bin the 2.5% and 97.5% quantiles of the observed rate under that
hypothesis (the consistency band). The draws respect clusters: one uniform `u_g` per cluster and `y*_i = 1[u_g <
p_i]`, so every row keeps its marginal `Bernoulli(p_i)` and rows of one cluster move together. That is exact when a
cluster's rows share one outcome (several forecasts of one question, or copies of one event) and conservative when
they are less correlated; with one row per cluster it is the usual independent draw. Each bin also has the Wilson
95% interval of its observed rate. The Murphy decomposition comes from `queuetruth.stats.murphy` (equal-count
bins).

For categorical forecasts the same diagram is drawn for the top-label confidence: `p` = the largest probability,
`y` = 1 when that label happened.
"""

from __future__ import annotations

import numpy as np

from queuetruth.forecast.inference import cluster_count_chunks
from queuetruth.forecast.schemas import Calibration, ReliabilityBin
from queuetruth.schemas.models import CiValue
from queuetruth.stats import bootstrap_seed, murphy, percentile_ci, stable_seed
from queuetruth.stats.calibration import Z975


def bins(p: np.ndarray, n_bins: int) -> np.ndarray:
    """Equal-width bin index per forecast."""
    pp = np.asarray(p, dtype=float)
    return np.minimum((pp * n_bins).astype(np.int64), n_bins - 1)


def ece(p: np.ndarray, y: np.ndarray, n_bins: int = 10, weights: np.ndarray | None = None) -> float:
    """Expected calibration error with equal-width bins (optionally row-weighted)."""
    pp = np.asarray(p, dtype=float)
    yy = np.asarray(y, dtype=float)
    w = np.ones_like(pp) if weights is None else np.asarray(weights, dtype=float)
    tot = float(w.sum())
    if pp.size == 0 or tot <= 0:
        return float("nan")
    gap = np.bincount(bins(pp, n_bins), weights=w * (yy - pp), minlength=n_bins)
    return float(np.abs(gap).sum() / tot)


def _wilson(rate: float, n: int) -> tuple[float, float]:
    z2 = Z975 * Z975
    centre = (rate + z2 / (2 * n)) / (1 + z2 / n)
    half = Z975 * np.sqrt(rate * (1 - rate) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return float(centre - half), float(centre + half)


def _f(x: float) -> float | None:
    return float(x) if np.isfinite(x) else None


def calibration(
    p: np.ndarray,
    y: np.ndarray,
    clusters: np.ndarray,
    *,
    target: str,
    n_bins: int = 10,
    b: int = 1000,
    seed: int = 0,
    test_id: str = "calibration",
    alpha: float = 0.05,
) -> Calibration:
    """The `Calibration` block for forecasts `p` of binary events `y`, rows clustered by `clusters` (int index)."""
    pp = np.asarray(p, dtype=float)
    yy = np.asarray(y, dtype=float)
    cl = np.asarray(clusters, dtype=np.int64)
    n = int(pp.size)
    k_idx = bins(pp, n_bins)
    count = np.bincount(k_idx, minlength=n_bins)
    sum_p = np.bincount(k_idx, weights=pp, minlength=n_bins)
    sum_y = np.bincount(k_idx, weights=yy, minlength=n_bins)
    est = ece(pp, yy, n_bins)
    lo = hi = float("nan")
    null_mean = null_p = float("nan")
    band_lo = np.full(n_bins, np.nan)
    band_hi = np.full(n_bins, np.nan)
    if n and b > 0:
        g = int(cl.max()) + 1
        if g >= 2:
            reps = np.array(
                [
                    ece(pp, yy, n_bins, weights=row[cl])
                    for chunk in cluster_count_chunks(g, b, bootstrap_seed(seed, test_id + "/ece"))
                    for row in chunk
                ]
            )
            q_lo, q_hi = percentile_ci(reps, alpha)
            lo, hi = max(0.0, 2.0 * est - q_hi), max(0.0, 2.0 * est - q_lo)
        rng = np.random.default_rng(stable_seed(seed, [b"consistency", test_id]))
        g_all = int(cl.max()) + 1
        null = np.empty(b)
        rates = np.full((b, n_bins), np.nan)
        for i in range(b):
            ys = (rng.random(g_all)[cl] < pp).astype(float)
            null[i] = ece(pp, ys, n_bins)
            with np.errstate(invalid="ignore", divide="ignore"):
                rates[i] = np.bincount(k_idx, weights=ys, minlength=n_bins) / count
        null_mean = float(null.mean())
        null_p = float((1 + int(np.sum(null >= est - 1e-15))) / (1 + b))
        occupied = count > 0
        band_lo[occupied] = np.quantile(rates[:, occupied], 0.025, axis=0)
        band_hi[occupied] = np.quantile(rates[:, occupied], 0.975, axis=0)
    rows = []
    for k in range(n_bins):
        c = int(count[k])
        if c:
            rate = float(sum_y[k] / c)
            wl, wh = _wilson(rate, c)
            rows.append(
                ReliabilityBin(
                    lo=k / n_bins,
                    hi=(k + 1) / n_bins,
                    count=c,
                    mean_p=float(sum_p[k] / c),
                    observed_rate=rate,
                    rate_lo=wl,
                    rate_hi=wh,
                    consistency_lo=_f(band_lo[k]),
                    consistency_hi=_f(band_hi[k]),
                )
            )
        else:
            rows.append(
                ReliabilityBin(
                    lo=k / n_bins,
                    hi=(k + 1) / n_bins,
                    count=0,
                    mean_p=None,
                    observed_rate=None,
                    rate_lo=None,
                    rate_hi=None,
                    consistency_lo=None,
                    consistency_hi=None,
                )
            )
    mur = murphy(pp, yy, n_bins) if n else {}
    return Calibration(
        target=target,  # type: ignore[arg-type]
        n=n,
        n_bins=n_bins,
        ece=CiValue(value=_f(est), ci_lo=_f(lo), ci_hi=_f(hi)),
        ece_if_calibrated=_f(null_mean),
        ece_p_value=_f(null_p),
        murphy={k: _f(v) for k, v in mur.items()},
        bins=rows,
    )


__all__ = ["bins", "calibration", "ece"]
