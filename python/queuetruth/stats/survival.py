"""Survival and competing-risk estimators and tests (SPEC 4.3, 5.7.5).

Times are non-negative floats (any unit); ties are handled exactly (events and censorings at the same time: the
censored subjects are at risk at that time). Returned step functions start at time 0 (value 1 for survival, 0 for
incidence) followed by every distinct observed time.

- `kaplan_meier`: `S(t) = prod_{t_k <= t} (1 - d_k / n_k)`.
- `aalen_johansen`: `CIF_c(t) = sum_{t_k <= t} S(t_k-) d_{c,k} / n_k`, with `S` the all-cause Kaplan-Meier; cause 0
  is censoring and every non-zero cause counts as an event for `S`.
- `logrank`: two-sample log-rank `U^2 / V`, `U = sum (d_a - n_a d / n)`, `V = sum n_a n_b d (n - d) / (n^2 (n - 1))`,
  chi-square with 1 df.
- `gray_test`: Gray (1988) K-sample test of equal cumulative incidence for one cause, weight rho = 0. Causes other
  than `cause_of_interest` and 0 are pooled as the competing cause. Score per group
  `z_k = sum_t [d1_k(t) - d1(t) R_k(t) / R(t)]`, `R_k = Y_k (1 - F_k(t-)) / S_k(t-)` (group CIF `F_k`, all-cause KM
  `S_k`, at-risk `Y_k`). Covariance from the first-order expansion of `z` under H0 with the pooled incidence
  `F0` (`dF0 = d1 / sum_r Y_r / S_r(t-)`): `a_kr = delta_kr - t1_k / tr` with `t1_r = Y_r / S_r(t-)`,
  `tr = sum_r t1_r`; `G_kr(t) = sum_{s > t} a_kr(s) t1_r(s) dF0(s) / (1 - F0(s-))`;
  `A_kr = a_kr t1_r + G_kr (1 - q_r)`, `B_kr = -G_kr q_r`, `q_r = (1 - F0(t)) / S_r(t)`;
  `V_kj = sum_t sum_r A_kr A_jr (S_r(t-) / Y_r) dF0 c1_r + B_kr B_jr (S_r(t-) / Y_r)^2 d2_r c2_r` with tie
  corrections `c1_r = 1 - (d1 - 1) / (tr S_r(t-) - 1)` and `c2_r = 1 - (d2_r - 1) / (Y_r - 1)` (1 without ties).
  Statistic `z' V^-1 z` on the first K-1 groups (sorted labels), chi-square with K-1 df. Derived from the paper;
  agrees with R `cmprsk::cuminc` to ~1e-14 (O-STAT-3).
"""

from __future__ import annotations

import numpy as np
from scipy import stats as sps

from queuetruth.stats._types import StepFunction, TestResult


def _times(time: np.ndarray) -> np.ndarray:
    t = np.asarray(time, dtype=float).ravel()
    if np.isnan(t).any():
        raise ValueError("time contains NaN")
    if (t < 0).any():
        raise ValueError("time must be >= 0")
    return t


def _prepend(time: np.ndarray, value: np.ndarray, start: float) -> StepFunction:
    if time.size == 0:
        return StepFunction(np.array([0.0]), np.array([start]))
    if time[0] > 0:
        return StepFunction(np.concatenate([[0.0], time]), np.concatenate([[start], value]))
    return StepFunction(time.copy(), value)


def _risk_table(t: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Distinct times, inverse index and number at risk (time >= t_k) per distinct time."""
    ut, inv = np.unique(t, return_inverse=True)
    exits = np.bincount(inv, minlength=ut.size).astype(float)
    at_risk = t.size - np.concatenate([[0.0], np.cumsum(exits)[:-1]])
    return ut, inv, at_risk


def kaplan_meier(time: np.ndarray, event: np.ndarray) -> StepFunction:
    """Kaplan-Meier survival (event: bool)."""
    t = _times(time)
    e = np.asarray(event, dtype=bool).ravel()
    if e.size != t.size:
        raise ValueError("time and event must have the same length")
    if t.size == 0:
        return _prepend(t, t, 1.0)
    ut, inv, n = _risk_table(t)
    d = np.bincount(inv, weights=e.astype(float), minlength=ut.size)
    return _prepend(ut, np.cumprod(1.0 - d / n), 1.0)


def aalen_johansen(time: np.ndarray, cause: np.ndarray, causes: tuple[int, ...] = (1, 2)) -> dict[int, StepFunction]:
    """Aalen-Johansen cumulative incidence per cause (cause 0 = censored)."""
    t = _times(time)
    c = np.asarray(cause).ravel().astype(np.int64)
    if c.size != t.size:
        raise ValueError("time and cause must have the same length")
    if t.size == 0:
        return {int(k): _prepend(t, t, 0.0) for k in causes}
    ut, inv, n = _risk_table(t)
    d_all = np.bincount(inv, weights=(c != 0).astype(float), minlength=ut.size)
    s = np.cumprod(1.0 - d_all / n)
    s_left = np.concatenate([[1.0], s[:-1]])
    out: dict[int, StepFunction] = {}
    for k in causes:
        dk = np.bincount(inv, weights=(c == k).astype(float), minlength=ut.size)
        out[int(k)] = _prepend(ut, np.cumsum(s_left * dk / n), 0.0)
    return out


def logrank(time_a: np.ndarray, event_a: np.ndarray, time_b: np.ndarray, event_b: np.ndarray) -> TestResult:
    """Two-sample log-rank test (matches lifelines `logrank_test`)."""
    ta, tb = _times(time_a), _times(time_b)
    ea = np.asarray(event_a, dtype=bool).ravel()
    eb = np.asarray(event_b, dtype=bool).ravel()
    if ea.size != ta.size or eb.size != tb.size:
        raise ValueError("time and event arrays must have matching lengths")
    t = np.concatenate([ta, tb])
    e = np.concatenate([ea, eb]).astype(float)
    in_a = np.concatenate([np.ones(ta.size), np.zeros(tb.size)])
    if t.size == 0:
        return TestResult(float("nan"), float("nan"))
    ut, inv, n = _risk_table(t)
    d = np.bincount(inv, weights=e, minlength=ut.size)
    da = np.bincount(inv, weights=e * in_a, minlength=ut.size)
    exits_a = np.bincount(inv, weights=in_a, minlength=ut.size)
    na = ta.size - np.concatenate([[0.0], np.cumsum(exits_a)[:-1]])
    m = (d > 0) & (n > 1)
    u = float(np.sum(da[d > 0] - na[d > 0] * d[d > 0] / n[d > 0]))
    v = float(np.sum(na[m] * (n[m] - na[m]) * d[m] * (n[m] - d[m]) / (n[m] ** 2 * (n[m] - 1.0))))
    if v <= 0:
        return TestResult(float("nan"), float("nan"))
    stat = u * u / v
    return TestResult(stat, float(sps.chi2.sf(stat, 1)))


def gray_test(time: np.ndarray, cause: np.ndarray, group: np.ndarray, cause_of_interest: int = 1) -> TestResult:
    """Gray's K-sample test on cumulative incidence (Gray 1988, rho = 0)."""
    t = _times(time)
    c = np.asarray(cause).ravel().astype(np.int64)
    g = np.asarray(group).ravel()
    if not t.size == c.size == g.size:
        raise ValueError("time, cause and group must have the same length")
    m = np.where(c == cause_of_interest, 1, np.where(c == 0, 0, 2))
    labels, gi = np.unique(g, return_inverse=True)
    k = labels.size
    if k < 2:
        raise ValueError("gray_test needs at least two groups")
    ut, ti = np.unique(t, return_inverse=True)
    nt = ut.size
    d1 = np.zeros((nt, k))
    d2 = np.zeros((nt, k))
    ex = np.zeros((nt, k))
    np.add.at(d1, (ti, gi), (m == 1).astype(float))
    np.add.at(d2, (ti, gi), (m == 2).astype(float))
    np.add.at(ex, (ti, gi), 1.0)
    y = ex.sum(axis=0) - np.vstack([np.zeros(k), np.cumsum(ex, axis=0)[:-1]])
    alive = y > 0
    y_safe = np.where(alive, y, 1.0)
    s = np.cumprod(np.where(alive, 1.0 - (d1 + d2) / y_safe, 1.0), axis=0)
    s_left = np.vstack([np.ones(k), s[:-1]])
    f = np.cumsum(np.where(alive, s_left * d1 / y_safe, 0.0), axis=0)
    f_left = np.vstack([np.zeros(k), f[:-1]])
    sl_safe = np.where(s_left > 0, s_left, 1.0)
    big_r = np.where(alive, y * (1.0 - f_left) / sl_safe, 0.0)
    d1_all = d1.sum(axis=1)
    has = d1_all > 0
    z = np.sum(d1[has] - d1_all[has, None] * big_r[has] / big_r[has].sum(axis=1)[:, None], axis=0)

    t1 = np.where(alive & (s_left > 0), y / sl_safe, 0.0)
    tr = t1.sum(axis=1)
    tr_safe = np.where(tr > 0, tr, 1.0)
    df0 = np.where(tr > 0, d1_all / tr_safe, 0.0)
    f0 = np.cumsum(df0)
    f0_left = np.concatenate([[0.0], f0[:-1]])
    a = np.eye(k)[None, :, :] - (t1 / tr_safe[:, None])[:, :, None]  # a[t, k, r] = delta_kr - t1_k / tr
    den = np.where(1.0 - f0_left > 0, 1.0 - f0_left, 1.0)
    inc = a * t1[:, None, :] * (df0 / den)[:, None, None]
    big_g = np.cumsum(inc[::-1], axis=0)[::-1] - inc
    s_safe = np.where(s > 0, s, 1.0)
    q = np.where(s > 0, (1.0 - f0)[:, None] / s_safe, 0.0)
    big_a = a * t1[:, None, :] + big_g * (1.0 - q)[:, None, :]
    big_b = -big_g * q[:, None, :]
    with np.errstate(divide="ignore", invalid="ignore"):
        den1 = tr[:, None] * s_left - 1.0
        c1 = np.where((d1_all[:, None] > 1) & (den1 > 0), 1.0 - (d1_all[:, None] - 1.0) / den1, 1.0)
        c2 = np.where((d2 > 1) & (y > 1), 1.0 - (d2 - 1.0) / np.where(y > 1, y - 1.0, 1.0), 1.0)
    w1 = np.where(alive, s_left / y_safe, 0.0) * df0[:, None] * c1
    w2 = np.where(alive, (s_left / y_safe) ** 2 * d2, 0.0) * c2
    v = np.einsum("tkr,tjr,tr->kj", big_a, big_a, w1) + np.einsum("tkr,tjr,tr->kj", big_b, big_b, w2)
    zz = z[: k - 1]
    vv = v[: k - 1, : k - 1]
    try:
        stat = float(zz @ np.linalg.solve(vv, zz))
    except np.linalg.LinAlgError:
        return TestResult(float("nan"), float("nan"))
    if not np.isfinite(stat):
        return TestResult(float("nan"), float("nan"))
    return TestResult(stat, float(sps.chi2.sf(stat, k - 1)))
