"""Proper scoring rules, as losses (lower is better), for binary, categorical and continuous forecasts.

Binary (`p` = P(y = 1), `p_y` = probability given to what happened):
- Brier `(p - y)^2` (range [0, 1]); log `-ln max(p_y, eps)`; spherical `1 - p_y / sqrt(p^2 + (1 - p)^2)`.
Categorical (`P` rows sum to 1, `e` = one-hot outcome, K outcomes):
- Brier `sum_k (P_k - e_k)^2` (range [0, 2]; a binary question scored this way gives twice the binary Brier);
  log and spherical as above with `||P||_2`; RPS (ordinal outcomes) `sum_{k<K} (cum P_k - cum e_k)^2 / (K - 1)`,
  which equals the binary Brier for K = 2.
Continuous: CRPS `int (F(x) - 1{x >= y})^2 dx`:
- normal: `sd * (z (2 Phi(z) - 1) + 2 phi(z) - 1 / sqrt(pi))`, `z = (y - mean) / sd` (Gneiting and Raftery 2007);
- samples: the CRPS of the empirical distribution, `mean|X - y| - mean|X - X'| / 2` over all ordered pairs;
- quantiles: `2 sum_k w_k QS_k`, the pinball loss `QS_k = (1{y < q_k} - tau_k)(q_k - y)` weighted by the width of
  each level's cell on [0, 1] (cells split halfway between levels), which tends to the CRPS as the levels get
  dense and is `|q - y|` for a single median.
`eps` (default 1e-6, as `queuetruth.stats.log_loss`) bounds the log score at `-ln(eps)`; scorecards count the
clipped forecasts.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import stats as sps

SQRT_PI = math.sqrt(math.pi)


def binary_scores(p: np.ndarray, y: np.ndarray, eps: float = 1e-6) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Per-forecast `{brier, log, spherical}` losses and the mask of log scores clipped at `eps`."""
    pp = np.asarray(p, dtype=float)
    yy = np.asarray(y, dtype=float)
    if pp.shape != yy.shape:
        raise ValueError("p and y must have the same shape")
    if np.any(~np.isfinite(pp)) or np.any((pp < 0) | (pp > 1)):
        raise ValueError("p must lie in [0, 1]")
    if not np.isin(yy, (0.0, 1.0)).all():
        raise ValueError("y must be 0 or 1")
    py = np.where(yy == 1.0, pp, 1.0 - pp)
    clipped = py < eps
    norm = np.sqrt(pp * pp + (1.0 - pp) ** 2)
    return {
        "brier": (pp - yy) ** 2,
        "log": -np.log(np.maximum(py, eps)),
        "spherical": 1.0 - py / norm,
    }, clipped


def categorical_scores(
    probs: np.ndarray, y: np.ndarray, ordered: bool = False, eps: float = 1e-6
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Per-forecast losses for an `(n, K)` probability matrix and outcome indices `y`; `rps` only when `ordered`."""
    pm = np.asarray(probs, dtype=float)
    yi = np.asarray(y, dtype=np.int64)
    if pm.ndim != 2 or pm.shape[0] != yi.size or pm.shape[1] < 2:
        raise ValueError("probs must be (n, K >= 2) with one outcome index per row")
    if np.any((yi < 0) | (yi >= pm.shape[1])):
        raise ValueError("outcome index out of range")
    if np.any(~np.isfinite(pm)) or np.any((pm < 0) | (pm > 1)):
        raise ValueError("probabilities must lie in [0, 1]")
    n, k = pm.shape
    onehot = np.zeros_like(pm)
    onehot[np.arange(n), yi] = 1.0
    py = pm[np.arange(n), yi]
    clipped = py < eps
    out = {
        "brier": np.sum((pm - onehot) ** 2, axis=1),
        "log": -np.log(np.maximum(py, eps)),
        "spherical": 1.0 - py / np.sqrt(np.sum(pm * pm, axis=1)),
    }
    if ordered:
        cum = np.cumsum(pm, axis=1)[:, :-1]
        cum_e = np.cumsum(onehot, axis=1)[:, :-1]
        out["rps"] = np.sum((cum - cum_e) ** 2, axis=1) / (k - 1)
    return out, clipped


def crps_normal(mean: float | np.ndarray, sd: float | np.ndarray, y: float | np.ndarray) -> np.ndarray:
    """Closed-form CRPS of N(mean, sd^2) at y."""
    m = np.asarray(mean, dtype=float)
    s = np.asarray(sd, dtype=float)
    if np.any(s <= 0):
        raise ValueError("sd must be positive")
    z = (np.asarray(y, dtype=float) - m) / s
    return s * (z * (2.0 * sps.norm.cdf(z) - 1.0) + 2.0 * sps.norm.pdf(z) - 1.0 / SQRT_PI)


def crps_samples(samples: np.ndarray, y: float) -> float:
    """CRPS of the empirical distribution of `samples` at y (O(m log m))."""
    x = np.sort(np.asarray(samples, dtype=float).ravel())
    m = x.size
    if m == 0:
        raise ValueError("no samples")
    term1 = float(np.mean(np.abs(x - float(y))))
    # mean over ordered pairs |X_i - X_j| = (2 / m^2) sum_i (2i - m - 1) x_(i), i = 1..m
    i = np.arange(1, m + 1, dtype=float)
    pair = 2.0 * float(np.sum((2.0 * i - m - 1.0) * x)) / (m * m)
    return term1 - 0.5 * pair


def quantile_weights(levels: np.ndarray) -> np.ndarray:
    """Width of each level's cell on [0, 1], cells split halfway between consecutive levels (sum 1)."""
    t = np.asarray(levels, dtype=float)
    if t.size == 0:
        raise ValueError("no levels")
    edges = np.concatenate([[0.0], (t[1:] + t[:-1]) / 2.0, [1.0]])
    return np.diff(edges)


def crps_quantiles(levels: np.ndarray, values: np.ndarray, y: float) -> float:
    """Quantile-score approximation of the CRPS: `2 sum_k w_k QS_{tau_k}(q_k, y)`."""
    t = np.asarray(levels, dtype=float)
    q = np.asarray(values, dtype=float)
    if t.shape != q.shape:
        raise ValueError("levels and values must have the same length")
    qs = ((float(y) < q).astype(float) - t) * (q - float(y))
    return float(2.0 * np.sum(quantile_weights(t) * qs))


def crps_empirical_climatology(prior: np.ndarray, y: float) -> float:
    """CRPS of the empirical distribution of earlier outcomes (the continuous climatology baseline)."""
    return crps_samples(prior, y)


__all__ = [
    "binary_scores",
    "categorical_scores",
    "crps_empirical_climatology",
    "crps_normal",
    "crps_quantiles",
    "crps_samples",
    "quantile_weights",
]
