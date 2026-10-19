"""Small deterministic computations the report needs on top of the input tables: Wilson intervals, reliability
bins, queue-decile strata (SPEC 5.6 construction), last-value-per-bucket downsampling, and the C1 allocation
quasi-likelihood profile (SPEC 5.4) evaluated on the `qt.alloc_events` table.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from queuetruth.report.svg import RelBin

Z95 = 1.959963984540054
PROFILE_CHI2_95 = 3.841459


def wilson(k: float, n: float, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes out of n (n > 0)."""
    if n <= 0:
        return (0.0, 1.0)
    phat = k / n
    den = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / den
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / den
    lo = 0.0 if k <= 0 else max(0.0, centre - half)
    hi = 1.0 if k >= n else min(1.0, centre + half)
    return (lo, hi)


def reliability_bins(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> list[RelBin]:
    """Equal-width bins on [0, 1] (1.0 falls in the last bin); empty bins are kept with n = 0."""
    p = np.clip(np.asarray(p, dtype=np.float64), 0.0, 1.0)
    y = np.asarray(y, dtype=np.float64)
    idx = np.minimum((p * n_bins).astype(np.int64), n_bins - 1)
    out: list[RelBin] = []
    for b in range(n_bins):
        m = idx == b
        n = int(m.sum())
        lo, hi = b / n_bins, (b + 1) / n_bins
        if n == 0:
            out.append(RelBin(lo, hi, 0, (lo + hi) / 2, 0.0, 0.0, 0.0))
            continue
        k = float(y[m].sum())
        wl, wh = wilson(k, n)
        out.append(RelBin(lo, hi, n, float(np.sort(p[m]).mean()), k / n, wl, wh))
    return out


def strata(values: np.ndarray, groups: np.ndarray, n_q: int) -> np.ndarray:
    """Stratum index per row: `searchsorted(q, v, side="right")` with `q` the 1/n_q .. (n_q-1)/n_q linear quantiles
    of the values within each group, clipped to 0..n_q-1; -1 where the value is missing (NaN)."""
    v = np.asarray(values, dtype=np.float64)
    g = np.asarray(groups)
    out = np.full(v.shape[0], -1, dtype=np.int64)
    probs = np.arange(1, n_q) / n_q
    for key in sorted(set(g.tolist())):
        m = (g == key) & np.isfinite(v)
        if not m.any():
            continue
        q = np.quantile(v[m], probs, method="linear")
        out[m] = np.clip(np.searchsorted(q, v[m], side="right"), 0, n_q - 1)
    return out


def last_per_bucket(t: np.ndarray, v: np.ndarray, t_lo: int, t_hi: int, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Keeps the last point of each of `n` uniform time buckets over [t_lo, t_hi] (points must be time-ordered)."""
    t = np.asarray(t, dtype=np.int64)
    v = np.asarray(v)
    if t.shape[0] <= n:
        return t, v
    span = max(int(t_hi) - int(t_lo), 1)
    b = ((t - t_lo).astype(np.float64) / span * n).astype(np.int64)
    b = np.clip(b, 0, n - 1)
    last = np.ones(t.shape[0], dtype=bool)
    last[:-1] = b[1:] != b[:-1]
    return t[last], v[last]


# ---- C1 allocation quasi-likelihood (SPEC 5.4, prob functions of 5.3.3 b and g) ----------------------------------


def _pi(family: str, theta: float, front: np.ndarray, back: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        if family == "databento":
            x = front / (front + back)
            k = theta
            if not -1.0 < k < 1.0:
                return np.full(np.shape(front), np.nan)
            pf = x ** (1.0 / (1.0 + k)) if k <= 0 else 1.0 - (1.0 - x) ** (1.0 / (1.0 - k))
            return 1.0 - pf
        n = math.exp(theta)
        if family == "power":
            bn, fn = back**n, front**n
            return bn / (bn + fn)
        if family == "power2":
            return back**n / (back + front) ** n
        if family == "power3":
            return 1.0 - (front / (front + back)) ** n
    raise ValueError(family)


def alloc_loglik(family: str, theta: float, front: np.ndarray, back: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    """`sum b ln pi + a ln(1 - pi)` over events with front > 0 and back > 0, pi clipped to [1e-12, 1 - 1e-12];
    non-finite pi skips the event."""
    pi = _pi(family, theta, front, back)
    ok = np.isfinite(pi)
    pi = np.clip(pi[ok], 1e-12, 1 - 1e-12)
    return float(np.sum(b[ok] * np.log(pi)) + np.sum(a[ok] * np.log1p(-pi)))


ALLOC_FAMILIES = ("power", "power2", "power3", "databento")


def default_grid(family: str) -> tuple[float, float, int]:
    """Spec default grid (theta = ln n for power families, k for databento)."""
    return (-0.99, 0.99, 199) if family == "databento" else (-3.0, 4.0, 141)


def alloc_profile(
    family: str,
    front: Sequence[float],
    back: Sequence[float],
    ahead: Sequence[float],
    behind: Sequence[float],
    grid: tuple[float, float, int],
) -> list[tuple[float, float]]:
    """(theta, loglik) on the grid."""
    f = np.asarray(front, dtype=np.float64)
    bk = np.asarray(back, dtype=np.float64)
    a = np.asarray(ahead, dtype=np.float64)
    b = np.asarray(behind, dtype=np.float64)
    keep = (f > 0) & (bk > 0)
    f, bk, a, b = f[keep], bk[keep], a[keep], b[keep]
    lo, hi, n = grid
    thetas = np.linspace(lo, hi, int(n)) if n > 1 else np.array([lo])
    return [(float(t), alloc_loglik(family, float(t), f, bk, a, b)) for t in thetas]
