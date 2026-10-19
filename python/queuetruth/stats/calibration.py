"""Calibration metrics for predicted probabilities `p` against outcomes `y` in {0, 1} (SPEC 4.3, 5.5.7, 5.6).

Bins (et `analytics/calibration.py` semantics): K equal-count bins by stable rank of `p`
(`bin = rank * K // N`); tied forecasts share the lowest bin of their tie group, so bins are equal-count only up
to ties and a constant forecast is one bin. Bin labels keep their rank-derived value.
- `brier = mean((p - y)^2)`; `log_loss = -mean(y ln pc + (1 - y) ln(1 - pc))`, `pc = clip(p, eps, 1 - eps)`.
- `reliability_table`: per bin count, mean_p, observed_rate, p_min, p_max and a Wilson 95% interval
  (`z = Phi^-1(0.975)`) for observed_rate.
- `murphy`: `reliability = sum_k n_k/N (pbar_k - obar_k)^2`, `resolution = sum_k n_k/N (obar_k - obar)^2`,
  `uncertainty = obar (1 - obar)`, `within_bin = brier - (reliability - resolution + uncertainty)`.
- `hosmer_lemeshow`: `sum_k (o_k - e_k)^2 / (e_k (1 - e_k / n_k))` over bins with `0 < e_k < n_k`, chi-square with
  `max(K - 2, 1)` df (8 for 10 bins).
- `spiegelhalter_z = sum (y - p)(1 - 2p) / sqrt(sum (1 - 2p)^2 p (1 - p))`, two-sided normal p-value.
- `calibration_slope_intercept`: logistic regression `logit P(y) = intercept + slope * logit(pc)` fitted by
  Newton-Raphson (IRLS) to `|step| < 1e-10` (at most 100 iterations); NaN when the fit does not exist
  (constant `p`, a single outcome class, or no convergence).
Empty inputs give NaN.
"""

from __future__ import annotations

import numpy as np
from scipy import special
from scipy import stats as sps

from queuetruth.stats._types import TestResult

Z975 = float(sps.norm.ppf(0.975))


def _py(p: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    pp = np.asarray(p, dtype=float).ravel()
    yy = np.asarray(y, dtype=float).ravel()
    if pp.size != yy.size:
        raise ValueError("p and y must have the same length")
    if np.isnan(pp).any() or ((pp < 0) | (pp > 1)).any():
        raise ValueError("p must lie in [0, 1]")
    if not np.isin(yy, (0.0, 1.0)).all():
        raise ValueError("y must be 0 or 1")
    return pp, yy


def bins(p: np.ndarray, n_bins: int = 10) -> np.ndarray:
    """Bin label per forecast (equal-count by stable rank; ties share the lowest bin of their group)."""
    pp = np.asarray(p, dtype=float).ravel()
    n = pp.size
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    order = np.argsort(pp, kind="stable")
    raw = np.empty(n, dtype=np.int64)
    raw[order] = np.arange(n, dtype=np.int64) * n_bins // n
    uniq, inv = np.unique(pp, return_inverse=True)
    low = np.full(uniq.size, np.iinfo(np.int64).max, dtype=np.int64)
    np.minimum.at(low, inv, raw)
    return low[inv]


def brier(p: np.ndarray, y: np.ndarray) -> float:
    """Brier score."""
    pp, yy = _py(p, y)
    return float(np.mean((pp - yy) ** 2)) if pp.size else float("nan")


def log_loss(p: np.ndarray, y: np.ndarray, eps: float = 1e-6) -> float:
    """Log loss with clipping."""
    pp, yy = _py(p, y)
    if pp.size == 0:
        return float("nan")
    pc = np.clip(pp, eps, 1.0 - eps)
    return float(-np.mean(yy * np.log(pc) + (1.0 - yy) * np.log(1.0 - pc)))


def reliability_table(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> dict[str, np.ndarray]:
    """Reliability table with Wilson CIs: columns bin, count, mean_p, observed_rate, p_min, p_max, rate_lo, rate_hi."""
    pp, yy = _py(p, y)
    b = bins(pp, n_bins)
    labels, inv = np.unique(b, return_inverse=True)
    count = np.bincount(inv, minlength=labels.size).astype(float)
    mean_p = np.bincount(inv, weights=pp, minlength=labels.size) / np.where(count > 0, count, 1.0)
    rate = np.bincount(inv, weights=yy, minlength=labels.size) / np.where(count > 0, count, 1.0)
    p_min = np.full(labels.size, np.inf)
    p_max = np.full(labels.size, -np.inf)
    np.minimum.at(p_min, inv, pp)
    np.maximum.at(p_max, inv, pp)
    z2 = Z975 * Z975
    centre = (rate + z2 / (2 * count)) / (1 + z2 / count)
    half = Z975 * np.sqrt(rate * (1 - rate) / count + z2 / (4 * count * count)) / (1 + z2 / count)
    return {
        "bin": labels.astype(np.int64),
        "count": count.astype(np.int64),
        "mean_p": mean_p,
        "observed_rate": rate,
        "p_min": p_min,
        "p_max": p_max,
        "rate_lo": centre - half,
        "rate_hi": centre + half,
    }


def murphy(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> dict[str, float]:
    """Murphy decomposition: brier, reliability, resolution, uncertainty, within_bin (they sum exactly to brier)."""
    pp, yy = _py(p, y)
    if pp.size == 0:
        nan = float("nan")
        return {"brier": nan, "reliability": nan, "resolution": nan, "uncertainty": nan, "within_bin": nan}
    t = reliability_table(pp, yy, n_bins)
    w = t["count"] / pp.size
    obar = float(yy.mean())
    b = float(np.mean((pp - yy) ** 2))
    rel = float(np.sum(w * (t["mean_p"] - t["observed_rate"]) ** 2))
    res = float(np.sum(w * (t["observed_rate"] - obar) ** 2))
    unc = obar * (1.0 - obar)
    return {"brier": b, "reliability": rel, "resolution": res, "uncertainty": unc, "within_bin": b - (rel - res + unc)}


def hosmer_lemeshow(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> TestResult:
    """Hosmer-Lemeshow test on the equal-count bins, `max(n_bins - 2, 1)` df."""
    pp, yy = _py(p, y)
    if pp.size == 0:
        return TestResult(float("nan"), float("nan"))
    t = reliability_table(pp, yy, n_bins)
    n_k = t["count"].astype(float)
    o_k = t["observed_rate"] * n_k
    e_k = t["mean_p"] * n_k
    ok = (e_k > 0) & (e_k < n_k)
    stat = float(np.sum((o_k[ok] - e_k[ok]) ** 2 / (e_k[ok] * (1 - e_k[ok] / n_k[ok]))))
    return TestResult(stat, float(sps.chi2.sf(stat, max(n_bins - 2, 1))))


def spiegelhalter_z(p: np.ndarray, y: np.ndarray) -> TestResult:
    """Spiegelhalter z test (two-sided normal p-value)."""
    pp, yy = _py(p, y)
    den = float(np.sum((1 - 2 * pp) ** 2 * pp * (1 - pp)))
    if pp.size == 0 or den <= 0:
        return TestResult(float("nan"), float("nan"))
    z = float(np.sum((yy - pp) * (1 - 2 * pp)) / np.sqrt(den))
    return TestResult(z, float(2 * sps.norm.sf(abs(z))))


def calibration_slope_intercept(p: np.ndarray, y: np.ndarray, eps: float = 1e-6) -> tuple[float, float]:
    """Logistic recalibration `(slope, intercept)` of `y` on `logit(clip(p, eps, 1 - eps))`."""
    pp, yy = _py(p, y)
    nan = (float("nan"), float("nan"))
    if pp.size < 2 or yy.min() == yy.max():
        return nan
    x = special.logit(np.clip(pp, eps, 1.0 - eps))
    if np.ptp(x) == 0:
        return nan
    design = np.column_stack([np.ones_like(x), x])
    beta = np.zeros(2)
    for _ in range(100):
        mu = special.expit(design @ beta)
        w = mu * (1 - mu)
        hess = design.T @ (design * w[:, None])
        grad = design.T @ (yy - mu)
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            return nan
        beta = beta + step
        if not np.all(np.isfinite(beta)):
            return nan
        if np.max(np.abs(step)) < 1e-10:
            return float(beta[1]), float(beta[0])
    return nan
