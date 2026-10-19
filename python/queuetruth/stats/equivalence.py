"""Equivalence testing (TOST, SPEC 5.7.5).

`tost_from_ci`: equivalent iff the 90% CI lies inside the closed interval `[-margin, margin]`.
`tost_p_bootstrap` (audit minor M22, used by the scorecard): the TOST p-value from the same bootstrap replicates as
the CI, `max(frac(rep <= -margin), frac(rep >= margin))` over the finite replicates, so the flag and the p-value use
one method. `tost_p` (normal approximation, kept for API compatibility) =
`max(1 - Phi((est + margin) / se), Phi((est - margin) / se))`; with `se = 0` the p-value is 0 when `|est| < margin`
and 1 otherwise; NaN inputs give NaN.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import stats as sps


def tost_from_ci(ci90: tuple[float, float], margin: float) -> bool:
    """Equivalence iff the 90% CI lies inside `[-margin, margin]`."""
    lo, hi = float(ci90[0]), float(ci90[1])
    if math.isnan(lo) or math.isnan(hi) or math.isnan(margin):
        return False
    return -margin <= lo and hi <= margin


def tost_p(estimate: float, se: float, margin: float) -> float:
    """TOST p-value `max(1 - Phi((est + m) / se), Phi((est - m) / se))`."""
    est, s, m = float(estimate), float(se), float(margin)
    if math.isnan(est) or math.isnan(s) or math.isnan(m) or s < 0:
        return float("nan")
    if s == 0:
        return 0.0 if abs(est) < m else 1.0
    return float(max(sps.norm.sf((est + m) / s), sps.norm.cdf((est - m) / s)))


def tost_p_bootstrap(replicates: np.ndarray, margin: float) -> float:
    """TOST p-value from bootstrap replicates (SPEC 5.7.5, M22): `max(frac(rep <= -margin), frac(rep >= margin))`
    over the finite replicates; NaN when there is none or the margin is NaN."""
    r = np.asarray(replicates, dtype=float)
    r = r[np.isfinite(r)]
    m = float(margin)
    if r.size == 0 or math.isnan(m):
        return float("nan")
    return float(max(np.mean(r <= -m), np.mean(r >= m)))
