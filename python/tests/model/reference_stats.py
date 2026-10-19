"""Small reference implementations used to inject `queuetruth.stats` functions into non-gate model tests (the real
functions live in `queuetruth.stats`; the gate tests marked `integration` use those)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
from scipy import stats as sps


def aalen_johansen(time, cause, causes=(1, 2)):
    """Aalen-Johansen CIF per cause as step functions on the distinct event times (events before censorings)."""
    t = np.asarray(time, dtype=np.float64)
    c = np.asarray(cause, dtype=np.int64)
    order = np.argsort(t, kind="stable")
    t, c = t[order], c[order]
    times = np.unique(t)
    out = {k: [] for k in causes}
    s_prev, cif = 1.0, dict.fromkeys(causes, 0.0)
    for u in times:
        at = t == u
        n = int(np.sum(t >= u))
        d_all = int(np.sum(at & (c != 0)))
        for k in causes:
            cif[k] += s_prev * np.sum(at & (c == k)) / n
            out[k].append(cif[k])
        s_prev *= 1.0 - d_all / n
    return {k: SimpleNamespace(time=times, value=np.asarray(out[k])) for k in causes}


def calibration_slope_intercept(p, y):
    """Logistic regression of y on logit(p) by Newton steps: (slope, intercept)."""
    q = np.clip(np.asarray(p, dtype=np.float64), 1e-6, 1 - 1e-6)
    x = np.column_stack([np.ones(len(q)), np.log(q / (1 - q))])
    yy = np.asarray(y, dtype=np.float64)
    beta = np.zeros(2)
    for _ in range(50):
        mu = 1 / (1 + np.exp(-x @ beta))
        g = x.T @ (yy - mu)
        h = (x * (mu * (1 - mu))[:, None]).T @ x + 1e-9 * np.eye(2)
        step = np.linalg.solve(h, g)
        beta += step
        if np.max(np.abs(step)) < 1e-10:
            break
    return float(beta[1]), float(beta[0])


def spiegelhalter_z(p, y):
    """Spiegelhalter z."""
    p = np.asarray(p, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    z = np.sum((y - p) * (1 - 2 * p)) / np.sqrt(np.sum((1 - 2 * p) ** 2 * p * (1 - p)))
    return SimpleNamespace(statistic=float(z), p_value=float(2 * sps.norm.sf(abs(z))))


def hosmer_lemeshow(p, y, n_bins=10):
    """Hosmer-Lemeshow over equal-count bins, n_bins - 2 df."""
    p = np.asarray(p, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    order = np.argsort(p, kind="stable")
    stat = 0.0
    for b in np.array_split(order, n_bins):
        if b.size == 0:
            continue
        o, e, n = y[b].sum(), p[b].sum(), b.size
        stat += (o - e) ** 2 / max(e * (1 - e / n), 1e-12)
    return SimpleNamespace(statistic=float(stat), p_value=float(sps.chi2.sf(stat, n_bins - 2)))


def install(monkeypatch) -> None:
    """Replaces the `queuetruth.stats` functions the model card uses."""
    import queuetruth.stats as qs

    for name in ("aalen_johansen", "calibration_slope_intercept", "spiegelhalter_z", "hosmer_lemeshow"):
        monkeypatch.setattr(qs, name, globals()[name])
