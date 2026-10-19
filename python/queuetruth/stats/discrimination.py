"""Discrimination measures, reported only as "discrimination only (improper)" (SPEC 5.5.7). Never used for selection.

`risk` is a score where larger means earlier/more likely event of interest. Censoring survival `G` is the
Kaplan-Meier of the censoring indicator (`censor`, default `~event`), evaluated as a left limit `G(t-)`.
- `uno_c_index` (Uno et al. 2011): `sum_{i,j} D_i G(T_i-)^-2 1{T_i < T_j, T_i < tau} (1{r_i > r_j} + 1/2 1{r_i = r_j})
  / sum_{i,j} D_i G(T_i-)^-2 1{T_i < T_j, T_i < tau}`, `D_i` = event of interest observed.
- `auc_at` (cumulative/dynamic AUC at `tau`, Blanche et al. 2013 definition 2 for competing risks): cases have the
  event of interest by `tau` (weight `1/G(T_i-)`); controls are still event-free at `tau` (weight `1/G(tau)`) or had
  a competing event (neither event nor censored) by `tau` (weight `1/G(T_j-)`); ties in risk count 1/2.
NaN when there are no comparable pairs.
"""

from __future__ import annotations

import numpy as np

from queuetruth.stats.survival import kaplan_meier


def _inputs(
    time: np.ndarray, event: np.ndarray, risk: np.ndarray, censor: np.ndarray | None
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    t = np.asarray(time, dtype=float).ravel()
    e = np.asarray(event, dtype=bool).ravel()
    r = np.asarray(risk, dtype=float).ravel()
    c = ~e if censor is None else np.asarray(censor, dtype=bool).ravel()
    if not t.size == e.size == r.size == c.size:
        raise ValueError("time, event, risk and censor must have the same length")
    if (e & c).any():
        raise ValueError("a row cannot be both an event and censored")
    return t, e, r, c


def _censor_left(t: np.ndarray, c: np.ndarray, at: np.ndarray) -> np.ndarray:
    g = kaplan_meier(t, c)
    return np.asarray(g.left(at, before=1.0), dtype=float)


def uno_c_index(
    time: np.ndarray, event: np.ndarray, risk: np.ndarray, tau: float | None = None, censor: np.ndarray | None = None
) -> float:
    """Uno's concordance index (improper; discrimination only)."""
    t, e, r, c = _inputs(time, event, risk, censor)
    if t.size == 0:
        return float("nan")
    limit = np.inf if tau is None else float(tau)
    g = _censor_left(t, c, t)
    w = np.where(e & (t < limit) & (g > 0), 1.0 / np.where(g > 0, g, 1.0) ** 2, 0.0)
    ranks = np.unique(r, return_inverse=True)[1] + 1
    size = int(ranks.max())
    tree = np.zeros(size + 1)

    def add(i: int) -> None:
        while i <= size:
            tree[i] += 1.0
            i += i & -i

    def prefix(i: int) -> float:
        s = 0.0
        while i > 0:
            s += tree[i]
            i -= i & -i
        return s

    order = np.argsort(-t, kind="stable")
    num = den = 0.0
    inserted = 0.0
    k = 0
    while k < order.size:
        j = k
        while j < order.size and t[order[j]] == t[order[k]]:
            j += 1
        group = order[k:j]
        for i in group:
            if w[i] > 0 and inserted > 0:
                less = prefix(int(ranks[i]) - 1)
                equal = prefix(int(ranks[i])) - less
                num += w[i] * (less + 0.5 * equal)
                den += w[i] * inserted
        for i in group:
            add(int(ranks[i]))
            inserted += 1.0
        k = j
    return float(num / den) if den > 0 else float("nan")


def auc_at(
    time: np.ndarray, event: np.ndarray, risk: np.ndarray, tau: float, censor: np.ndarray | None = None
) -> float:
    """Time-dependent cumulative/dynamic AUC at `tau` (improper; discrimination only)."""
    t, e, r, c = _inputs(time, event, risk, censor)
    case = e & (t <= tau)
    late = t > tau
    competing = ~e & ~c & (t <= tau)
    g_left = _censor_left(t, c, t)
    g_tau = float(np.asarray(kaplan_meier(t, c).at(tau, before=1.0)))
    wc = np.where(case & (g_left > 0), 1.0 / np.where(g_left > 0, g_left, 1.0), 0.0)
    wk = np.where(late, 1.0 / g_tau if g_tau > 0 else 0.0, 0.0) + np.where(
        competing & (g_left > 0), 1.0 / np.where(g_left > 0, g_left, 1.0), 0.0
    )
    ci = np.flatnonzero(wc > 0)
    ki = np.flatnonzero(wk > 0)
    if ci.size == 0 or ki.size == 0:
        return float("nan")
    order = np.argsort(r[ki], kind="stable")
    kr = r[ki][order]
    kw = np.concatenate([[0.0], np.cumsum(wk[ki][order])])
    lo = np.searchsorted(kr, r[ci], side="left")
    hi = np.searchsorted(kr, r[ci], side="right")
    below = kw[lo]
    tied = kw[hi] - kw[lo]
    num = float(np.sum(wc[ci] * (below + 0.5 * tied)))
    return num / (float(np.sum(wc[ci])) * float(kw[-1]))
