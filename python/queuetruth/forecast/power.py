"""Power analysis: how many resolved questions a paired comparison needs to detect an edge.

The edge is the expected per-question loss difference `delta = E[loss_reference - loss_subject]` with standard
deviation `sd` of that difference. The test is the two-sided paired t test at level `alpha` (the Diebold-Mariano
test with lag 0), so the power at `n` independent questions is `P(|T| > t_{1-alpha/2, n-1})` with `T` noncentral t
(`n - 1` df, noncentrality `delta sqrt(n) / sd`), exact for normal differences and accurate for others once `n` is
in the tens. Clustered questions inflate the variance of the mean by the design effect `deff`, so `n` questions
carry the information of `n / deff` independent ones.

`brier_edge_moments` and `log_edge_moments` give `delta` and `sd` when the reference forecasts `q` and the truth
(and the subject's forecast) is `p`: for the Brier score `E[d | p, q] = (q - p)^2` and
`Var[d | p, q] = 4 (q - p)^2 p (1 - p)`; for the log score `E[d | p, q] = KL(Bern(p) || Bern(q))`.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import stats as sps

MAX_N = 10**9


def power_at(n: float, delta: float, sd: float, alpha: float = 0.05, deff: float = 1.0) -> float:
    """Power of the two-sided paired t test with `n` questions (NaN when undefined)."""
    if not (n >= 2 and sd > 0 and deff >= 1 and math.isfinite(delta)):
        return float("nan")
    n_eff = n / deff
    if n_eff < 2:
        return float("nan")
    df = n_eff - 1.0
    tc = float(sps.t.ppf(1.0 - alpha / 2.0, df))
    nc = abs(delta) * math.sqrt(n_eff) / sd
    return float(sps.nct.sf(tc, df, nc) + sps.nct.cdf(-tc, df, nc))


def required_n(delta: float, sd: float, alpha: float = 0.05, power: float = 0.8, deff: float = 1.0) -> int | None:
    """Smallest number of resolved questions whose power reaches `power` (None for no edge or no spread)."""
    if not (math.isfinite(delta) and math.isfinite(sd)) or delta == 0 or sd <= 0 or deff < 1:
        return None
    z = float(sps.norm.ppf(1.0 - alpha / 2.0) + sps.norm.ppf(power))
    n0 = (z * sd / abs(delta)) ** 2 * deff
    if n0 > MAX_N:
        return None
    if n0 > 1e6:  # t and normal agree to well under one question here
        return math.ceil(n0)
    n = max(2, math.ceil(2.0 * deff), math.ceil(n0))
    while power_at(n, delta, sd, alpha, deff) < power:
        n += 1
    while n > 2 and power_at(n - 1, delta, sd, alpha, deff) >= power:
        n -= 1
    return n


def brier_edge_moments(q: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """`(delta, sd)` of the Brier difference of reference `q` against a subject who forecasts the truth `p`."""
    qq = np.asarray(q, dtype=float)
    pp = np.asarray(p, dtype=float)
    m = (qq - pp) ** 2
    v = 4.0 * m * pp * (1.0 - pp)
    delta = float(m.mean())
    var = float(v.mean() + m.var())
    return delta, math.sqrt(var)


def log_edge_moments(q: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """`(delta, sd)` of the log-score difference of reference `q` against a subject who forecasts the truth `p`."""
    qq = np.clip(np.asarray(q, dtype=float), 1e-12, 1 - 1e-12)
    pp = np.clip(np.asarray(p, dtype=float), 1e-12, 1 - 1e-12)
    a = np.log(pp / qq)  # y = 1
    b = np.log((1 - pp) / (1 - qq))  # y = 0
    m = pp * a + (1 - pp) * b
    second = pp * a * a + (1 - pp) * b * b
    delta = float(m.mean())
    var = float((second - m * m).mean() + m.var())
    return delta, math.sqrt(var)


__all__ = ["MAX_N", "brier_edge_moments", "log_edge_moments", "power_at", "required_n"]
