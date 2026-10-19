"""Multiple-testing adjustments (SPEC 5.7.5), equal to `statsmodels.stats.multitest.multipletests`.

Adjusted p-values do not depend on `q`; a hypothesis is rejected when `p_adj <= q`. NaN p-values are left NaN and
excluded from the family size.
"""

from __future__ import annotations

import numpy as np


def _prepare(p: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = np.asarray(p, dtype=float).ravel()
    ok = ~np.isnan(x)
    if ((x[ok] < 0) | (x[ok] > 1)).any():
        raise ValueError("p-values must lie in [0, 1]")
    return x, ok, x[ok]


def _step_up(p: np.ndarray, scale: float) -> np.ndarray:
    x, ok, v = _prepare(p)
    out = np.full(x.size, np.nan)
    m = v.size
    if m:
        order = np.argsort(v, kind="stable")
        adj = v[order] * m * scale / np.arange(1, m + 1)
        adj = np.minimum(np.minimum.accumulate(adj[::-1])[::-1], 1.0)
        res = np.empty(m)
        res[order] = adj
        out[ok] = res
    return out


def benjamini_hochberg(p: np.ndarray, q: float = 0.05) -> np.ndarray:
    """BH-adjusted p-values."""
    return _step_up(p, 1.0)


def benjamini_yekutieli(p: np.ndarray, q: float = 0.05) -> np.ndarray:
    """BY-adjusted p-values (BH times `sum_{i <= m} 1/i`)."""
    m = int(np.sum(~np.isnan(np.asarray(p, dtype=float))))
    return _step_up(p, float(np.sum(1.0 / np.arange(1, m + 1))) if m else 1.0)


def holm(p: np.ndarray) -> np.ndarray:
    """Holm-adjusted p-values."""
    x, ok, v = _prepare(p)
    out = np.full(x.size, np.nan)
    m = v.size
    if m:
        order = np.argsort(v, kind="stable")
        adj = np.minimum(np.maximum.accumulate(v[order] * np.arange(m, 0, -1)), 1.0)
        res = np.empty(m)
        res[order] = adj
        out[ok] = res
    return out


def adjust(p: np.ndarray, method: str, q: float = 0.05) -> np.ndarray:
    """Adjusted p-values for `method` in {none, holm, bh, by} (the `adj_method` values of `qt.score_tests`)."""
    if method == "none":
        return np.asarray(p, dtype=float).ravel().copy()
    if method == "holm":
        return holm(p)
    if method == "bh":
        return benjamini_hochberg(p, q)
    if method == "by":
        return benjamini_yekutieli(p, q)
    raise ValueError(f"unknown adjustment method {method!r}")
