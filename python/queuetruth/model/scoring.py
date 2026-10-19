"""Scoring rules of the learned hazard model (SPEC 5.5.7).

Status at `tau` follows `queuetruth.model._common`: fills and cancels with `e <= tau` are events by `tau`; a
censoring with `e >= tau` is a survivor at `tau`; a censoring with `e < tau` is unknown (weight 0).
`G` is the Kaplan-Meier survival of censoring (censorings are the events; at tied times fills and cancels stay in the
risk set) evaluated as a left limit, `G(t-) = P(C >= t)`, floored at 0.05: an event at `e` is observed iff `C >= e`
and a survivor at `tau` iff `C >= tau` (this keeps the mass of censorings at the `h_max` cap out of the weights at
`tau = h_max`).

- `rcll`: mean of `-ln P(observed)` with raw hazards; `P = h_c(K) S(tau_{K-1})` for an event of cause `c` in
  interval `K`, `S(tau_J)` when it survived every interval, `S(tau_m)` for a censoring after `m` complete intervals.
- `ipcw_logloss(tau)`: Alberge et al. 2024 eq. 2 with the minus sign (a loss).
- `ipcw_brier(tau)`: Graf-style IPCW Brier of the fill cause; `ibs` integrates it over the grid.
- `c_index_uno`, `auc_at`: discrimination only (improper), cause-specific IPCW forms (Wolbers / Uno).
- `ipcw_calibration(p, y, w)`: the calibration diagnostics of SPEC 5.5.7 at one horizon, every sum weighted by the
  IPCW weights `w` (`ipcw_weights`; rows censored before `tau` have weight 0 and drop out). Unweighted on rows with
  known status they over-represent events (an event at `e` is kept with probability `G(e-) >= G(tau-)`, a survivor
  with `G(tau-)`), so a calibrated model looks like an underpredictor (review F2). With `x = logit(clip(p, 1e-6))`:
  `calib_slope` = slope of the weighted logistic regression `logit P(y) = a + b x` (IRLS); `calib_intercept` =
  calibration-in-the-large, `a` in `logit P(y) = a + x` (slope fixed at 1, weighted Newton); Spiegelhalter
  `z = sum w (y - p)(1 - 2p) / sqrt(sum w^2 (1 - 2p)^2 p (1 - p))` (two-sided normal p); Hosmer-Lemeshow over the 10
  equal-count bins of `queuetruth.stats.bins` (by rank of `p` among the rows with `w > 0`):
  `sum_k (O_k - E_k)^2 / V_k`, `O_k = sum w y`, `E_k = sum w p`, `V_k = E_k (1 - E_k / W_k) sum w^2 / W_k`,
  `W_k = sum w`, over bins with `0 < E_k < W_k`; `hl_p` with `K` df (the held-out set fits nothing; `K` = bins
  used, 10 without ties) and `hl_p_et` with `max(n_bins - 2, 1)` = 8 df (et parity, as
  `queuetruth.stats.hosmer_lemeshow`). The variances are model-based with squared weights, so every diagnostic
  equals its unweighted textbook form when no row is censored before `tau`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import special
from scipy import stats as sps

from queuetruth.model._common import CANCEL, CENSOR, FILL, G_FLOOR, P_FLOOR, cause_codes, check_grid, known_status
from queuetruth.model.person_period import interval_counts


@dataclass(frozen=True)
class CensoringKM:
    """Kaplan-Meier survival of censoring: `value[i] = G(times[i])` (right-continuous)."""

    times: np.ndarray
    value: np.ndarray

    def left(self, t: np.ndarray | int | float) -> np.ndarray:
        """`G(t-)`: the product over censoring times strictly before `t` (1 before the first)."""
        t_arr = np.asarray(t, dtype=np.float64)
        idx = np.searchsorted(self.times, t_arr, side="left")
        padded = np.concatenate([[1.0], self.value])
        return padded[idx]

    def weight_left(self, t: np.ndarray | int | float) -> np.ndarray:
        """`1 / max(G(t-), 0.05)`."""
        return 1.0 / np.maximum(self.left(t), G_FLOOR)


def censoring_km(event_ns: np.ndarray, cause: np.ndarray) -> CensoringKM:
    """KM of the censoring distribution from `(event_ns, cause)`; `cause` as strings or codes (0 = censor)."""
    e = np.asarray(event_ns, dtype=np.float64)
    c = cause_codes(cause)
    if e.size == 0:
        return CensoringKM(np.zeros(0), np.zeros(0))
    order = np.argsort(e, kind="stable")
    es, cs = e[order], c[order]
    times, first = np.unique(es, return_index=True)
    n_at_risk = len(es) - first
    d_cens = np.add.reduceat((cs == CENSOR).astype(np.float64), first)
    value = np.cumprod(1.0 - d_cens / n_at_risk)
    return CensoringKM(times, value)


def ipcw_weights(event_ns: np.ndarray, cause: np.ndarray, tau: int, g: CensoringKM) -> np.ndarray:
    """Per row: `1/G(e-)` for an event by `tau`, `1/G(tau-)` for a survivor, 0 if censored before `tau`."""
    e = np.asarray(event_ns)
    c = cause_codes(cause)
    event_by, survivor, _ = known_status(e, c, tau)
    w = np.zeros(len(e), dtype=np.float64)
    w[event_by] = g.weight_left(e[event_by])
    w[survivor] = float(g.weight_left(tau))
    return w


def cif_from_hazards(h_fill: np.ndarray, h_cancel: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`(CIF_fill, CIF_cancel, S)` per grid point from per-interval hazards (shape (n, J)), SPEC 5.5.4."""
    surv = np.cumprod(np.clip(1.0 - h_fill - h_cancel, 0.0, 1.0), axis=1)
    s_prev = np.concatenate([np.ones((surv.shape[0], 1)), surv[:, :-1]], axis=1)
    return np.cumsum(h_fill * s_prev, axis=1), np.cumsum(h_cancel * s_prev, axis=1), surv


def rcll(
    h_fill: np.ndarray, h_cancel: np.ndarray, event_ns: np.ndarray, cause: np.ndarray, grid_ns: list[int]
) -> float:
    """Right-censored log likelihood loss (mean `-ln P(observed)`, probabilities floored at 1e-12)."""
    grid = check_grid(grid_ns)
    hf = np.atleast_2d(np.asarray(h_fill, dtype=np.float64))
    hc = np.atleast_2d(np.asarray(h_cancel, dtype=np.float64))
    e = np.asarray(event_ns, dtype=np.int64)
    c = cause_codes(cause)
    n = len(e)
    if n == 0:
        return float("nan")
    _, _, surv = cif_from_hazards(hf, hc)
    s_pad = np.concatenate([np.ones((n, 1)), surv], axis=1)  # s_pad[:, m] = S(tau_m)
    n_surv, ev_int = interval_counts(e, c, grid)
    rows = np.arange(n)
    p = s_pad[rows, n_surv].copy()
    ev = ev_int > 0
    k = ev_int[ev] - 1
    haz = np.where(c[ev] == FILL, hf[rows[ev], k], hc[rows[ev], k])
    p[ev] = haz * s_pad[rows[ev], k]
    return float(np.mean(-np.log(np.maximum(p, P_FLOOR))))


def ipcw_logloss(
    cif_fill: np.ndarray,
    cif_cancel: np.ndarray,
    event_ns: np.ndarray,
    cause: np.ndarray,
    tau_ns: int,
    *,
    censoring: CensoringKM | None = None,
) -> float:
    """IPCW competing-risk log loss at `tau` (Alberge et al. 2024 eq. 2), `S = max(1 - F_fill - F_cancel, 1e-12)`;
    `censoring` defaults to the KM of censoring of the same rows."""
    e = np.asarray(event_ns, dtype=np.int64)
    c = cause_codes(cause)
    if len(e) == 0:
        return float("nan")
    g = censoring if censoring is not None else censoring_km(e, c)
    ff = np.asarray(cif_fill, dtype=np.float64)
    fc = np.asarray(cif_cancel, dtype=np.float64)
    w = ipcw_weights(e, c, tau_ns, g)
    event_by, survivor, _ = known_status(e, c, tau_ns)
    s = np.maximum(1.0 - ff - fc, P_FLOOR)
    ll = np.zeros(len(e))
    fill = event_by & (c == FILL)
    canc = event_by & (c == CANCEL)
    ll[fill] = np.log(np.maximum(ff[fill], P_FLOOR))
    ll[canc] = np.log(np.maximum(fc[canc], P_FLOOR))
    ll[survivor] = np.log(s[survivor])
    return float(-np.mean(w * ll))


def ipcw_brier(
    cif_fill: np.ndarray, event_ns: np.ndarray, cause: np.ndarray, tau_ns: int, *, censoring: CensoringKM | None = None
) -> float:
    """IPCW Brier of the fill cause at `tau`: `mean_i w_i (1{e_i <= tau, fill} - F_fill(tau | x_i))^2`."""
    e = np.asarray(event_ns, dtype=np.int64)
    c = cause_codes(cause)
    if len(e) == 0:
        return float("nan")
    g = censoring if censoring is not None else censoring_km(e, c)
    w = ipcw_weights(e, c, tau_ns, g)
    y = ((c == FILL) & (e <= tau_ns)).astype(np.float64)
    return float(np.mean(w * (y - np.asarray(cif_fill, dtype=np.float64)) ** 2))


def brier_terms(
    cif_fill: np.ndarray, event_ns: np.ndarray, cause: np.ndarray, tau_ns: int, censoring: CensoringKM
) -> np.ndarray:
    """Per-row terms `w_i (y_i - F_i)^2` whose mean is `ipcw_brier`."""
    e = np.asarray(event_ns, dtype=np.int64)
    c = cause_codes(cause)
    w = ipcw_weights(e, c, tau_ns, censoring)
    y = ((c == FILL) & (e <= tau_ns)).astype(np.float64)
    return w * (y - np.asarray(cif_fill, dtype=np.float64)) ** 2


def ibs(brier_by_tau: list[float] | np.ndarray, grid_ns: list[int]) -> float:
    """Integrated Brier `sum_j BS(tau_j) (tau_j - tau_{j-1}) / tau_J` (`tau_0 = 0`)."""
    grid = check_grid(grid_ns).astype(np.float64)
    bs = np.asarray(brier_by_tau, dtype=np.float64)
    if bs.shape != grid.shape:
        raise ValueError("ibs: one Brier value per grid point is required")
    widths = np.diff(np.concatenate([[0.0], grid]))
    return float(np.sum(bs * widths) / grid[-1])


def auc_at(
    event_ns: np.ndarray, cause: np.ndarray, risk: np.ndarray, tau_ns: int, *, censoring: CensoringKM | None = None
) -> float | None:
    """Cumulative/dynamic IPCW AUC of the fill cause at `tau` (improper): cases fill by `tau` (weight `1/G(e-)`),
    controls survivors (`1/G(tau-)`) and cancels by `tau` (`1/G(e-)`); ties count 1/2. None without both classes.
    Arguments follow `queuetruth.stats.auc_at(time, event, risk, tau, ...)` (times, outcome, risk, tau; SPEC 4.3)."""
    e = np.asarray(event_ns, dtype=np.int64)
    c = cause_codes(cause)
    r = np.asarray(risk, dtype=np.float64)
    g = censoring if censoring is not None else censoring_km(e, c)
    w = ipcw_weights(e, c, tau_ns, g)
    event_by, survivor, _ = known_status(e, c, tau_ns)
    case = event_by & (c == FILL)
    ctrl = survivor | (event_by & (c == CANCEL))
    if not case.any() or not ctrl.any():
        return None
    rc, wc = r[ctrl], w[ctrl]
    order = np.argsort(rc, kind="stable")
    rc, wc = rc[order], wc[order]
    cum = np.concatenate([[0.0], np.cumsum(wc)])
    lo = np.searchsorted(rc, r[case], side="left")
    hi = np.searchsorted(rc, r[case], side="right")
    num = np.sum(w[case] * (cum[lo] + 0.5 * (cum[hi] - cum[lo])))
    den = np.sum(w[case]) * cum[-1]
    return float(num / den) if den > 0 else None


def c_index_uno(
    risk: np.ndarray,
    event_ns: np.ndarray,
    cause: np.ndarray,
    tau_ns: int,
    *,
    censoring: CensoringKM | None = None,
    max_rows: int = 20_000,
    seed: int = 0,
) -> float | None:
    """Uno-type IPCW concordance of the fill cause truncated at `tau` (improper): for a fill `i` with `e_i <= tau`
    the comparable `j` are `e_j > e_i` or cancels with `e_j <= e_i`; pair weight `1/G(e_i-)^2`; concordant if
    `risk_i > risk_j` (ties 1/2). Above `max_rows` rows a seeded subsample of `max_rows` rows is used."""
    e = np.asarray(event_ns, dtype=np.int64)
    c = cause_codes(cause)
    r = np.asarray(risk, dtype=np.float64)
    g = censoring if censoring is not None else censoring_km(e, c)
    if len(e) > max_rows:
        from queuetruth.stats import stable_seed

        rng = np.random.default_rng(stable_seed(seed, [b"c_index_uno"]))
        sel = np.sort(rng.choice(len(e), size=max_rows, replace=False))
        e, c, r = e[sel], c[sel], r[sel]
    cases = np.flatnonzero((c == FILL) & (e <= tau_ns))
    if cases.size == 0:
        return None
    wi = g.weight_left(e[cases]) ** 2
    is_cancel = c == CANCEL
    num = den = 0.0
    for start in range(0, cases.size, 512):
        idx = cases[start : start + 512]
        ei = e[idx][:, None]
        comp = (e[None, :] > ei) | (is_cancel[None, :] & (e[None, :] <= ei))
        comp[np.arange(idx.size), idx] = False
        ri = r[idx][:, None]
        conc = (r[None, :] < ri).astype(np.float64) + 0.5 * (r[None, :] == ri)
        w = wi[start : start + 512]
        num += float(np.sum(w * np.sum(comp * conc, axis=1)))
        den += float(np.sum(w * np.sum(comp, axis=1)))
    return num / den if den > 0 else None


LOGIT_EPS = 1e-6
HL_BINS = 10


def _weighted_logit_fit(x: np.ndarray, y: np.ndarray, w: np.ndarray, offset: bool) -> np.ndarray | None:
    """Weighted logistic regression by Newton-Raphson (`|step| < 1e-10`, at most 100 steps): `(a, b)` of
    `logit P(y) = a + b x`, or `(a,)` of `logit P(y) = a + x` when `offset`; None when the fit does not exist."""
    design = np.ones((x.size, 1)) if offset else np.column_stack([np.ones_like(x), x])
    off = x if offset else np.zeros_like(x)
    beta = np.zeros(design.shape[1])
    for _ in range(100):
        mu = special.expit(design @ beta + off)
        hess = design.T @ (design * (w * mu * (1.0 - mu))[:, None])
        grad = design.T @ (w * (y - mu))
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            return None
        beta = beta + step
        if not np.all(np.isfinite(beta)):
            return None
        if np.max(np.abs(step)) < 1e-10:
            return beta
    return None


def ipcw_calibration(p: np.ndarray, y: np.ndarray, w: np.ndarray, n_bins: int = HL_BINS) -> dict[str, float]:
    """IPCW-weighted calibration diagnostics (module docstring): `calib_slope`, `calib_intercept`,
    `spiegelhalter_z`, `spiegelhalter_p`, `hl_stat`, `hl_p`, `hl_p_et`; NaN where undefined (fewer than 2 rows with
    `w > 0`, one outcome class only, constant forecasts, no convergence)."""
    from queuetruth.stats.calibration import bins

    nan = float("nan")
    out = dict.fromkeys(
        ("calib_slope", "calib_intercept", "spiegelhalter_z", "spiegelhalter_p", "hl_stat", "hl_p", "hl_p_et"), nan
    )
    pp = np.asarray(p, dtype=np.float64).ravel()
    yy = np.asarray(y, dtype=np.float64).ravel()
    ww = np.asarray(w, dtype=np.float64).ravel()
    if not (pp.size == yy.size == ww.size):
        raise ValueError("p, y and w must have the same length")
    keep = ww > 0
    pp, yy, ww = pp[keep], yy[keep], ww[keep]
    if pp.size < 2 or not np.all(np.isfinite(pp)) or ((pp < 0) | (pp > 1)).any():
        return out
    if not np.isin(yy, (0.0, 1.0)).all():
        raise ValueError("y must be 0 or 1")
    both = bool(yy.min() != yy.max())
    x = special.logit(np.clip(pp, LOGIT_EPS, 1.0 - LOGIT_EPS))
    if both and np.ptp(x) > 0:
        beta = _weighted_logit_fit(x, yy, ww, offset=False)
        if beta is not None:
            out["calib_slope"] = float(beta[1])
    if both:
        a = _weighted_logit_fit(x, yy, ww, offset=True)
        if a is not None:
            out["calib_intercept"] = float(a[0])
    c = 1.0 - 2.0 * pp
    den = float(np.sum(ww**2 * c**2 * pp * (1.0 - pp)))
    if den > 0:
        z = float(np.sum(ww * (yy - pp) * c) / np.sqrt(den))
        out["spiegelhalter_z"], out["spiegelhalter_p"] = z, float(2.0 * sps.norm.sf(abs(z)))
    b = bins(pp, n_bins)
    labels, inv = np.unique(b, return_inverse=True)
    wk = np.bincount(inv, weights=ww, minlength=labels.size)
    w2k = np.bincount(inv, weights=ww**2, minlength=labels.size)
    ok_k = np.bincount(inv, weights=ww * yy, minlength=labels.size)
    ek = np.bincount(inv, weights=ww * pp, minlength=labels.size)
    use = (ek > 0) & (ek < wk)
    if use.any():
        vk = ek[use] * (1.0 - ek[use] / wk[use]) * w2k[use] / wk[use]
        stat = float(np.sum((ok_k[use] - ek[use]) ** 2 / vk))
        k = int(use.sum())
        out["hl_stat"] = stat
        out["hl_p"] = float(sps.chi2.sf(stat, k))
        out["hl_p_et"] = float(sps.chi2.sf(stat, max(n_bins - 2, 1)))
    return out


__all__ = [
    "CensoringKM",
    "auc_at",
    "brier_terms",
    "c_index_uno",
    "censoring_km",
    "cif_from_hazards",
    "ibs",
    "ipcw_brier",
    "ipcw_calibration",
    "ipcw_logloss",
    "ipcw_weights",
    "rcll",
]
