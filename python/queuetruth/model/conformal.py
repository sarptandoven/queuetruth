"""Conformal fill probabilities: split and Mondrian conformal prediction for fill-by-horizon outcomes.

Extension `conformal-fill` (`docs/design/extensions/conformal-fill.md`; concept page
`docs/concepts/conformal-fill-probability.md`).

Inputs are the fill probabilities of the learned hazard model (`qt.hazard_predictions`: `cif_fill_raw` or
`cif_fill`) or of a zoo model (`qt.zoo_predictions`: `p_fill`), with the exact labels next to them. For every horizon
`tau` the label is `Y = 1{fill within tau}` (the hazard's label: a fill before the policy cancel; the zoo's: the
exact never-cancel truth) and the nonconformity score of a candidate label `y` is `s(x, y) = 1 - p_hat(y | x)`
(`p_hat(1 | x) = p`, `p_hat(0 | x) = 1 - p`).

- `BinaryConformal`: Mondrian split conformal. A taxonomy `kappa(x, y)` (a stratum label, optionally with the label
  itself: `label_conditional`) assigns calibration rows to categories; the p-value of candidate `y` for a test row
  ranks its score among the calibration scores of its category: conservative `(#{s_i >= s} + 1) / (n + 1)` or
  smoothed `(#{s_i > s} + theta (#{s_i = s} + 1)) / (n + 1)`, `theta ~ U(0, 1]`. The prediction set is
  `{y : p_y > alpha}`: `P(Y in C) >= 1 - alpha` within every category (exactly `1 - alpha` when smoothed; below
  `1 - alpha + 1 / (n + 1)` for conservative p-values without ties).
- `VennAbers`: inductive Venn-Abers probability intervals `[p0, p1]` (isotonic regression of the calibration labels
  on `p`, with the test row added as label 0 and as label 1). The probability computed with the true label is
  calibrated, so `E[p0] <= P(Y = 1) <= E[p1]` within every stratum.
- `FillTimeConformal`: one-sided conformal bounds on the grid interval `K` of the first fill (`K = J + 1` when there
  is no fill within the last grid point `tau_J`) from a nondecreasing predicted CIF `F`. Lower bound: scores
  `F(tau_K)` (`+inf` without a fill), threshold the `floor(alpha (n + 1))`-th smallest, `lo = first j with
  F(tau_j) >= threshold, minus 1`: "no fill within `tau_lo`" holds with probability `>= 1 - alpha` (no claim when
  `lo = 0`). Upper bound: scores `F(tau_{K-1})` (`F(tau_0) = 0`, `+inf` without a fill), threshold the
  `ceil((1 - alpha)(n + 1))`-th smallest, `hi = min(J, 1 + max j with F(tau_j) <= threshold)`: "fills within
  `tau_hi`" with probability `>= 1 - alpha` (no claim when the threshold is infinite). `interval` uses `alpha / 2`
  on each side.
- `ConformalCalibrator`: the three calibrations above fitted once on calibration rows, saved to and loaded from
  `conformal_calibrator.parquet` (`qt.conformal_calibrator`), and applied to any rows, including rows without a
  label (tomorrow's orders): per row and horizon the p-values, the set and the Venn-Abers interval, per row the
  fill-time bounds.
- `exchangeability_martingale`: a conformal test martingale on the sequence of true-label scores. `evaluate` orders
  it by role, then session date: every calibration row first, then every test row, each (role, date) group
  shuffled by default, so the test targets a difference between the calibration and the test rows (and between
  days) rather than intraday order, also when both share a date. Online smoothed p-values
  `p_n = (#{i < n : s_i > s_n} + theta_n (#{i < n : s_i = s_n} + 1)) / n`, which are
  i.i.d. uniform under exchangeability (Vovk), are bet on by the Simple Jumper (`jumper`, three betting functions
  `1 + eps (p - 1/2)`, `eps in {-1, 0, 1}`, jump rate `J`) or a uniform mixture of power martingales (`mixture`,
  `eps p^(eps - 1)` on the grid `(k - 1/2) / 100`). By Ville's inequality the probability that the martingale ever
  reaches `threshold` under exchangeability is at most `1 / threshold`; reaching it is the alarm. That level is per
  horizon x landmark cell; across `m` cells the report also states the Bonferroni verdict (some cell reached
  `m x threshold`), whose family-wise false-alarm probability is at most `1 / threshold`.

Validity (the guarantee holds exactly when all of these hold):

1. The probabilities were produced without the calibration rows and without the test rows: the hazard GBDT is
   fitted on `train`, so `cif_fill_raw` may be calibrated and tested on `calib` or `test` rows; the isotonic layer
   behind `cif_fill` is fitted on `calib`, so `cif_fill` may not be calibrated or tested there (`QT-CONFIG-001`).
   Train rows are always refused, as calibration and as test rows. Zoo models with preset parameters may be
   calibrated on any rows; parameters fitted on data (`queuetruth calibrate ...`) follow the same rule as the GBDT:
   neither the calibration nor the test rows may come from the fit's window.
2. The label is observed: a row enters at horizon `tau` only when its outcome is known at `tau`. Shadows never
   affect the market and are followed to their administrative end (session close, `h_max`), which is known when the
   order goes live, so excluding rows whose end comes before `tau` is a function of the covariates and keeps
   exchangeability (zoo: the planned life of `qt.zoo_shadows`, label at `tau` iff `ts_admin_end >= ts_live + tau`);
   the policy cancel of the hazard's labels is a deterministic rule on observed data, a competing event and not
   censoring. Exclusions by feed gaps (`censored_gap`) are not known in advance; they are assumed
   outcome-independent, the guarantee then holds conditional on no gap, and they are counted in the report.
3. Calibration and test rows are exchangeable within each category. Mondrian categories always include the
   landmark, so each order contributes at most one row per category (rows of one order at different landmarks are
   dependent). Rows of different days are exchangeable only if the days are; the martingale tests exactly that,
   and a random order-level split within one day is exchangeable by construction.

Determinism: the smoothing variables and the within-group shuffles come from `numpy.random.default_rng` seeded with
`stable_seed(seed, [b"conformal", <label>])` (labels `sets:<h>:<l>`, `order:<h>:<l>`, `martingale:<h>:<l>`,
`halves`), so equal inputs and spec give equal outputs; `run_id` is the SPEC 7.3 hash of the spec and the input
digests.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pyarrow as pa
from pydantic import ValidationError
from scipy.stats import beta as beta_dist

from queuetruth.model._common import (
    CENSOR,
    FILL,
    check_grid,
    config_error,
    float_matrix,
    input_error,
    int64_column,
    outcome_arrays,
    require_columns,
)
from queuetruth.schemas.arrow import ENGINE_VERSION
from queuetruth.schemas.conformal import (
    REPORT_SCHEMA,
    ConformalReport,
    ConformalSpec,
    CoverageCell,
    DriftCell,
    DriftGroup,
    FillTimeCell,
    StratumName,
)
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.schemas.runid import run_id, spec_echo
from queuetruth.stats._seed import stable_seed

SETS_SCHEMA_NAME = "qt.conformal_sets"
REPORT_NAME = "conformal.json"
SETS_NAME = "conformal_sets.parquet"
CALIBRATOR_SCHEMA_NAME = "qt.conformal_calibrator"
CALIBRATOR_NAME = "conformal_calibrator.parquet"
ALL = "all"
#: Rows per chunk of the streamed mixture martingale (memory O(N_EPS x chunk)).
_MIX_CHUNK = 16_384
#: Mixture-martingale grid size (`eps_k = (k - 1/2) / N_EPS`).
N_EPS = 100
_P_TOL = 1e-9
_MONO_TOL = 1e-9
_LOG10 = math.log(10.0)


# ---- validation helpers ---------------------------------------------------------------------------------------


def _check_alpha(alpha: float) -> float:
    a = float(alpha)
    if not (0.0 < a < 1.0) or not math.isfinite(a):
        raise config_error(f"alpha must be in (0, 1), got {alpha!r}", field="alpha")
    return a


def _check_prob(p: Any, what: str = "p") -> np.ndarray:
    a = np.asarray(p, dtype=np.float64)
    if a.size and (not np.all(np.isfinite(a)) or a.min() < -_P_TOL or a.max() > 1.0 + _P_TOL):
        raise input_error(f"{what}: probabilities must be finite and in [0, 1]")
    return np.clip(a, 0.0, 1.0)


def _check_label(y: Any, n: int) -> np.ndarray:
    a = np.asarray(y)
    if a.shape != (n,):
        raise input_error(f"y: expected shape ({n},), got {a.shape}")
    if a.dtype == bool:
        return a.astype(np.int8)
    if a.size and not np.all(np.isin(a, (0, 1))):
        raise input_error("y: labels must be 0 or 1")
    return a.astype(np.int8)


def _strata(strata: Any, n: int) -> np.ndarray:
    if strata is None:
        return np.full(n, ALL, dtype=object)
    a = np.asarray(strata).astype(str).astype(object)
    if a.shape != (n,):
        raise input_error(f"strata: expected shape ({n},), got {a.shape}")
    return a


def nonconformity(p: Any, y: int | np.ndarray) -> np.ndarray:
    """`1 - p_hat(y | x)`: `1 - p` for label 1, `p` for label 0."""
    pa_ = _check_prob(p)
    yy = np.asarray(y)
    return np.where(yy == 1, 1.0 - pa_, pa_)


def _theta(rng: np.random.Generator, n: int) -> np.ndarray:
    """Smoothing variables in (0, 1] (never 0, so smoothed p-values are > 0)."""
    return 1.0 - rng.random(n)


def _rng(seed: int, label: str) -> np.random.Generator:
    return np.random.default_rng(stable_seed(int(seed), [b"conformal", label]))


def _rank_pvalues(cal_sorted: np.ndarray, s: np.ndarray, theta: np.ndarray | None) -> np.ndarray:
    n = len(cal_sorted)
    ge = n - np.searchsorted(cal_sorted, s, side="left")
    if theta is None:
        return (ge + 1.0) / (n + 1.0)
    gt = n - np.searchsorted(cal_sorted, s, side="right")
    return (gt + theta * (ge - gt + 1.0)) / (n + 1.0)


def min_informative_n(alpha: float) -> int:
    """Smallest calibration size whose conservative sets can exclude a label: `(n + 1) alpha >= 1`."""
    return max(0, math.ceil(1.0 / _check_alpha(alpha) - 1.0 - 1e-12))


# ---- split / Mondrian conformal classifier --------------------------------------------------------------------


class BinaryConformal:
    """Mondrian split-conformal prediction sets and p-values for a binary fill label (module docstring)."""

    def __init__(self, *, label_conditional: bool = False) -> None:
        self.label_conditional = bool(label_conditional)
        self._cal: dict[tuple[str, int], np.ndarray] = {}
        self._fitted = False

    def _key(self, stratum: str, y: int) -> tuple[str, int]:
        return (stratum, y if self.label_conditional else -1)

    def fit(self, p: Any, y: Any, strata: Any = None) -> BinaryConformal:
        """Stores the sorted calibration scores of every category."""
        pp = _check_prob(p)
        if pp.ndim != 1:
            raise input_error("p: expected a 1-D array")
        yy = _check_label(y, len(pp))
        st = _strata(strata, len(pp))
        s = nonconformity(pp, yy)
        self._cal = {}
        lab = yy if self.label_conditional else np.full(len(yy), -1, dtype=np.int8)
        uniq, inv = np.unique(st.astype(str), return_inverse=True)
        for g, name in enumerate(uniq):
            for y in np.unique(lab[inv == g]).tolist():
                self._cal[(str(name), int(y))] = np.sort(s[(inv == g) & (lab == y)])
        self._fitted = True
        return self

    def pvalues(self, p: Any, strata: Any = None, *, theta: np.ndarray | None = None) -> np.ndarray:
        """`(n, 2)` conformal p-values of the candidate labels 0 (no fill) and 1 (fill); smoothed when `theta`
        (one value in (0, 1] per row, shared by both labels) is given, else conservative."""
        if not self._fitted:
            raise input_error("BinaryConformal is not fitted")
        pp = _check_prob(p)
        if pp.ndim != 1:
            raise input_error("p: expected a 1-D array")
        st = _strata(strata, len(pp))
        if theta is not None:
            theta = np.asarray(theta, dtype=np.float64)
            if theta.shape != pp.shape or (theta.size and (theta.min() <= 0.0 or theta.max() > 1.0)):
                raise input_error("theta: one value in (0, 1] per row is required")
        out = np.empty((len(pp), 2))
        uniq, inv = np.unique(st.astype(str), return_inverse=True)
        for g, name in enumerate(uniq):
            rows = np.flatnonzero(inv == g)
            for y in (0, 1):
                cal = self._cal.get(self._key(str(name), y), np.empty(0))
                s = nonconformity(pp[rows], y)
                out[rows, y] = _rank_pvalues(cal, s, None if theta is None else theta[rows])
        return out

    def predict_sets(self, p: Any, alpha: float, strata: Any = None, *, theta: np.ndarray | None = None) -> np.ndarray:
        """`(n, 2)` boolean membership of labels 0 and 1 in the prediction set `{y : p_y > alpha}`."""
        a = _check_alpha(alpha)
        return self.pvalues(p, strata, theta=theta) > a


# ---- Venn-Abers probability intervals -------------------------------------------------------------------------


class _VaStratum:
    """Calibration data of one stratum: sorted unique probabilities `u` with counts `w` and label sums `sy`, and the
    pool-adjacent-violators stacks of every prefix and suffix as persistent linked lists.

    `pre[i]` is the top node of the left-to-right PAVA stack after `u[:i]` (the isotonic fit of that prefix; -1 =
    empty), `suf[i]` the top (leftmost) node of the right-to-left stack of `u[i:]`. Nodes hold the block weight and
    label sum and point to the next block away from the junction. Adding one test point at a junction, the isotonic
    fit of the augmented data pools only blocks adjacent to it: every pooling order of adjacent violators reaches
    the same fit, and prefix / suffix blocks are reachable by poolings within the prefix / suffix."""

    def __init__(self, u: np.ndarray, w: np.ndarray, sy: np.ndarray) -> None:
        self.u = u
        m = len(u)
        wl, sl = w.tolist(), sy.tolist()
        self.lw: list[float] = []
        self.ls: list[float] = []
        self.lp: list[int] = []
        self.pre = [-1] * (m + 1)
        top = -1
        for i in range(m):
            cw, cs = wl[i], sl[i]
            while top >= 0 and self.ls[top] * cw >= cs * self.lw[top]:  # top mean >= new mean: pool
                cw += self.lw[top]
                cs += self.ls[top]
                top = self.lp[top]
            self.lw.append(cw)
            self.ls.append(cs)
            self.lp.append(top)
            top = len(self.lw) - 1
            self.pre[i + 1] = top
        self.rw: list[float] = []
        self.rs: list[float] = []
        self.rp: list[int] = []
        self.suf = [-1] * (m + 1)
        top = -1
        for i in range(m - 1, -1, -1):
            cw, cs = wl[i], sl[i]
            while top >= 0 and cs * self.rw[top] >= self.rs[top] * cw:  # new mean >= top mean: pool
                cw += self.rw[top]
                cs += self.rs[top]
                top = self.rp[top]
            self.rw.append(cw)
            self.rs.append(cs)
            self.rp.append(top)
            top = len(self.rw) - 1
            self.suf[i] = top
        self.w = wl
        self.sy = sl

    def value(self, pos: int, exact: bool, y_new: int) -> float:
        """Isotonic fit at the test point with label `y_new`, inserted before `u[pos]` or (exact) tied with it."""
        if exact:
            cw, cs = self.w[pos] + 1.0, self.sy[pos] + y_new
            left, right = self.pre[pos], self.suf[pos + 1]
        else:
            cw, cs = 1.0, float(y_new)
            left, right = self.pre[pos], self.suf[pos]
        changed = True
        while changed:
            changed = False
            while left >= 0 and self.ls[left] * cw > cs * self.lw[left]:  # left block mean > current mean
                cw += self.lw[left]
                cs += self.ls[left]
                left = self.lp[left]
                changed = True
            while right >= 0 and cs * self.rw[right] > self.rs[right] * cw:  # current mean > right block mean
                cw += self.rw[right]
                cs += self.rs[right]
                right = self.rp[right]
                changed = True
        return cs / cw


class VennAbers:
    """Inductive Venn-Abers predictor per stratum: `predict` returns `(p0, p1)` (module docstring)."""

    def __init__(self) -> None:
        self._st: dict[str, _VaStratum] = {}
        self._fitted = False

    def fit(self, p: Any, y: Any, strata: Any = None) -> VennAbers:
        """Groups the calibration probabilities by stratum and unique value and builds the PAVA stacks."""
        pp = _check_prob(p)
        yy = _check_label(y, len(pp))
        st = _strata(strata, len(pp))
        self._st = {}
        uniq, inv = np.unique(st.astype(str), return_inverse=True)
        for g, name in enumerate(uniq):
            m = inv == g
            u, ui = np.unique(pp[m], return_inverse=True)
            w = np.bincount(ui, minlength=len(u)).astype(np.float64)
            sy = np.bincount(ui, weights=yy[m].astype(np.float64), minlength=len(u))
            self._st[str(name)] = _VaStratum(u, w, sy)
        self._fitted = True
        return self

    def predict(self, p: Any, strata: Any = None) -> tuple[np.ndarray, np.ndarray]:
        """`(p0, p1)`: the isotonic calibrated probability at `p` with the test row added as label 0 / label 1. A
        stratum without calibration rows gives the uninformative `(0, 1)`."""
        if not self._fitted:
            raise input_error("VennAbers is not fitted")
        pp = _check_prob(p)
        st = _strata(strata, len(pp))
        p0 = np.zeros(len(pp))
        p1 = np.ones(len(pp))
        uniq, inv = np.unique(st.astype(str), return_inverse=True)
        for g, name in enumerate(uniq):
            vs = self._st.get(str(name))
            if vs is None:
                continue
            rows = np.flatnonzero(inv == g)
            v = pp[rows]
            pos = np.searchsorted(vs.u, v, side="left")
            exact = (pos < len(vs.u)) & (vs.u[np.minimum(pos, len(vs.u) - 1)] == v)
            key = pos * 2 + exact.astype(np.int64)
            ukeys, kinv = np.unique(key, return_inverse=True)
            v0 = np.array([vs.value(int(k // 2), bool(k % 2), 0) for k in ukeys.tolist()])
            v1 = np.array([vs.value(int(k // 2), bool(k % 2), 1) for k in ukeys.tolist()])
            p0[rows] = v0[kinv]
            p1[rows] = v1[kinv]
        return p0, p1


def venn_abers_merge(p0: np.ndarray, p1: np.ndarray) -> np.ndarray:
    """One probability from a Venn-Abers interval, `p1 / (1 - p0 + p1)` (minimax for log loss)."""
    return np.asarray(p1) / (1.0 - np.asarray(p0) + np.asarray(p1))


# ---- conformal fill-time bounds -------------------------------------------------------------------------------


def _check_cif(cif: Any, j_count: int) -> np.ndarray:
    c = np.asarray(cif, dtype=np.float64)
    if c.ndim != 2 or c.shape[1] != j_count:
        raise input_error(f"cif: expected shape (rows, {j_count}), got {c.shape}")
    c = _check_prob(c, "cif")
    if c.size and np.any(np.diff(c, axis=1) < -_MONO_TOL):
        raise input_error("cif: each row must be nondecreasing across the grid")
    return np.maximum.accumulate(c, axis=1)


def fill_interval(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """`(K, observed)` from per-horizon labels `y` (rows, J) coded 1 / 0 / -1 (unknown): `K` = first grid index
    (1-based) with a fill, `J + 1` without one; `observed` = every horizon's label is known."""
    yy = np.asarray(y)
    j_count = yy.shape[1]
    observed = np.all(yy >= 0, axis=1)
    filled = yy == 1
    first = np.where(filled.any(axis=1), filled.argmax(axis=1) + 1, j_count + 1)
    return first.astype(np.int64), observed


class FillTimeConformal:
    """Mondrian split-conformal lower and upper bounds on the grid interval of the first fill (module docstring)."""

    def __init__(self, grid_ns: Any) -> None:
        self.grid_ns = check_grid(grid_ns)
        self._lo: dict[str, np.ndarray] = {}
        self._hi: dict[str, np.ndarray] = {}
        self._fitted = False

    def fit(self, cif: Any, k: Any, strata: Any = None) -> FillTimeConformal:
        """Calibration CIFs (rows, J) and fill intervals `K` in 1..J+1 (rows observed through the last grid point)."""
        j_count = len(self.grid_ns)
        c = _check_cif(cif, j_count)
        kk = np.asarray(k, dtype=np.int64)
        if kk.shape != (len(c),) or (kk.size and (kk.min() < 1 or kk.max() > j_count + 1)):
            raise input_error(f"k: one fill interval in 1..{j_count + 1} per row is required")
        st = _strata(strata, len(c))
        filled = kk <= j_count
        idx = np.arange(len(c))
        s_lo = np.full(len(c), np.inf)
        s_lo[filled] = c[idx[filled], kk[filled] - 1]
        s_hi = np.full(len(c), np.inf)
        s_hi[filled] = np.where(kk[filled] >= 2, c[idx[filled], np.maximum(kk[filled] - 2, 0)], 0.0)
        self._lo, self._hi = {}, {}
        uniq, inv = np.unique(st.astype(str), return_inverse=True)
        for g, name in enumerate(uniq):
            m = inv == g
            self._lo[str(name)] = np.sort(s_lo[m])
            self._hi[str(name)] = np.sort(s_hi[m])
        self._fitted = True
        return self

    def _groups(self, n: int, strata: Any) -> list[tuple[str, np.ndarray]]:
        if not self._fitted:
            raise input_error("FillTimeConformal is not fitted")
        st = _strata(strata, n)
        uniq, inv = np.unique(st.astype(str), return_inverse=True)
        return [(str(name), np.flatnonzero(inv == g)) for g, name in enumerate(uniq)]

    def lower(self, cif: Any, alpha: float, strata: Any = None) -> np.ndarray:
        """Grid index `lo` in 0..J per row: "no fill within `grid_ns[lo - 1]`" with probability >= 1 - alpha
        (no claim when 0)."""
        a = _check_alpha(alpha)
        j_count = len(self.grid_ns)
        c = _check_cif(cif, j_count)
        out = np.zeros(len(c), dtype=np.int64)
        for name, rows in self._groups(len(c), strata):
            cal = self._lo.get(name, np.empty(0))
            k = math.floor(a * (len(cal) + 1) + 1e-12)
            if k < 1:
                continue
            t = cal[k - 1]
            hit = c[rows] >= t
            out[rows] = np.where(hit.any(axis=1), hit.argmax(axis=1), j_count)
        return out

    def upper(self, cif: Any, alpha: float, strata: Any = None) -> np.ndarray:
        """Grid index `hi` in 1..J per row: "fills within `grid_ns[hi - 1]`" with probability >= 1 - alpha;
        `-1` = no claim."""
        a = _check_alpha(alpha)
        j_count = len(self.grid_ns)
        c = _check_cif(cif, j_count)
        out = np.full(len(c), -1, dtype=np.int64)
        for name, rows in self._groups(len(c), strata):
            cal = self._hi.get(name, np.empty(0))
            k = math.ceil((1.0 - a) * (len(cal) + 1) - 1e-12)
            if k > len(cal) or not np.isfinite(cal[k - 1]):
                continue
            t = cal[k - 1]
            j_prime = (c[rows] <= t).sum(axis=1)
            out[rows] = np.minimum(j_prime + 1, j_count)
        return out

    def interval(self, cif: Any, alpha: float, strata: Any = None) -> tuple[np.ndarray, np.ndarray]:
        """`(lower(alpha / 2), upper(alpha / 2))`: both statements hold jointly with probability >= 1 - alpha."""
        a = _check_alpha(alpha)
        return self.lower(cif, a / 2.0, strata), self.upper(cif, a / 2.0, strata)


def lower_covered(k: np.ndarray, lo: np.ndarray) -> np.ndarray:
    """The lower bound holds: no fill within `tau_lo` (`K > lo`; always true for `lo = 0`)."""
    return np.asarray(k) > np.asarray(lo)


def upper_covered(k: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """The upper bound holds: a fill within `tau_hi` (`K <= hi`; always true without a claim, `hi = -1`)."""
    kk, hh = np.asarray(k), np.asarray(hi)
    return (hh < 0) | (kk <= hh)


# ---- exchangeability martingale -------------------------------------------------------------------------------


def online_pvalues(scores: Any, theta: np.ndarray) -> np.ndarray:
    """Smoothed online conformal p-values of a score sequence (each score ranked among itself and its past)."""
    s = np.asarray(scores, dtype=np.float64)
    n = len(s)
    th = np.asarray(theta, dtype=np.float64)
    if th.shape != (n,):
        raise input_error("theta: one value per score is required")
    if n == 0:
        return np.empty(0)
    if not np.all(np.isfinite(s)):
        raise input_error("scores must be finite")
    _, inv = np.unique(s, return_inverse=True)
    m = int(inv.max()) + 1
    tree = [0] * (m + 1)
    out = np.empty(n)
    ranks = inv.tolist()
    thl = th.tolist()
    for i, r in enumerate(ranks):
        le = 0
        j = r + 1
        while j > 0:
            le += tree[j]
            j -= j & -j
        lt = 0
        j = r
        while j > 0:
            lt += tree[j]
            j -= j & -j
        out[i] = (i - le + thl[i] * (le - lt + 1)) / (i + 1)
        j = r + 1
        while j <= m:
            tree[j] += 1
            j += j & -j
    return out


def jumper_log10(pvalues: Any, rate: float = 0.01) -> np.ndarray:
    """log10 of the Simple Jumper test martingale after each p-value (Vovk et al. 2021)."""
    if not (0.0 < rate <= 1.0):
        raise config_error(f"jumper_rate must be in (0, 1], got {rate!r}", field="jumper_rate")
    c = [1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0]
    log_s = 0.0
    out = np.empty(len(pvalues))
    for i, p in enumerate(np.asarray(pvalues, dtype=np.float64).tolist()):
        mix = rate / 3.0
        c = [(1.0 - rate) * x + mix for x in c]  # sum(c) == 1 before the jump, so the jump keeps it at 1
        d = p - 0.5
        c = [c[0] * (1.0 - d), c[1], c[2] * (1.0 + d)]
        tot = c[0] + c[1] + c[2]
        log_s += math.log10(tot)
        c = [x / tot for x in c]
        out[i] = log_s
    return out


def mixture_log10(pvalues: Any, n_eps: int = N_EPS, *, chunk: int = _MIX_CHUNK) -> np.ndarray:
    """log10 of the uniform mixture of power martingales `prod eps p^(eps - 1)` on `eps = (k - 1/2) / n_eps`.
    Streamed in chunks of `chunk` p-values with a running log-product per `eps`, so memory is `O(n_eps x chunk)`."""
    p = np.asarray(pvalues, dtype=np.float64)
    if p.size == 0:
        return np.empty(0)
    if p.min() <= 0.0 or p.max() > 1.0:
        raise input_error("pvalues must be in (0, 1]")
    eps = (np.arange(n_eps) + 0.5) / n_eps
    slope = (eps - 1.0)[:, None]
    log_eps = np.log(eps)
    carry = np.zeros(n_eps)  # sum of the log factors so far, per eps
    out = np.empty(len(p))
    for start in range(0, len(p), max(1, int(chunk))):
        lp = np.log(p[start : start + chunk])
        cum = np.cumsum(log_eps[:, None] + slope * lp[None, :], axis=1)
        cum += carry[:, None]
        carry = cum[:, -1].copy()
        top = cum.max(axis=0)
        out[start : start + len(lp)] = top + np.log(np.exp(cum - top).sum(axis=0)) - math.log(n_eps)
    return out / _LOG10


@dataclass(frozen=True)
class GroupDrift:
    """Martingale statistics of one group (e.g. `calib:<date>`) in processing order."""

    group: str
    n: int
    first_index: int
    log10_end: float
    log10_max: float
    mean_pvalue: float
    ks_uniform: float


@dataclass(frozen=True)
class MartingaleResult:
    """A conformal test martingale run (`exchangeability_martingale`)."""

    method: str
    threshold: float
    order: np.ndarray  # input indices in processing order
    pvalues: np.ndarray  # online p-values in processing order
    log10_path: np.ndarray  # log10 martingale after each p-value
    alarm_index: int | None  # first processing position with martingale >= threshold
    alarm_group: str | None
    groups: list[GroupDrift] = field(default_factory=list)

    @property
    def log10_max(self) -> float:
        """Largest log10 value reached (0 for an empty run)."""
        return float(self.log10_path.max()) if self.log10_path.size else 0.0

    @property
    def alarm(self) -> bool:
        """The martingale reached the threshold (exchangeability rejected at level `1 / threshold`)."""
        return self.alarm_index is not None


def _ks_uniform(p: np.ndarray) -> float:
    if p.size == 0:
        return 0.0
    x = np.sort(p)
    n = len(x)
    i = np.arange(1, n + 1)
    return float(max(np.max(i / n - x), np.max(x - (i - 1) / n)))


def exchangeability_martingale(
    scores: Any,
    groups: Any = None,
    *,
    method: Literal["jumper", "mixture"] = "jumper",
    jumper_rate: float = 0.01,
    threshold: float = 100.0,
    shuffle_within_group: bool = True,
    time_ns: Any = None,
    seed: int = 0,
    label: str = "",
) -> MartingaleResult:
    """Conformal test martingale over `scores` processed group by group (groups in sorted order; within a group
    shuffled with the seed, or in `time_ns` order when `shuffle_within_group` is false). `label` names the seed
    stream (`martingale:<label>` for the smoothing, `order:<label>` for the shuffle)."""
    s = np.asarray(scores, dtype=np.float64)
    n = len(s)
    if not (threshold > 1.0) or not math.isfinite(threshold):
        raise config_error(f"alarm_threshold must be a finite number > 1, got {threshold!r}", field="alarm_threshold")
    if method not in ("jumper", "mixture"):
        raise config_error(f"martingale must be jumper or mixture, got {method!r}", field="martingale")
    g = np.zeros(n, dtype=np.int64) if groups is None else np.asarray(groups)
    if g.shape != (n,):
        raise input_error(f"groups: expected shape ({n},), got {g.shape}")
    t = None if time_ns is None else np.asarray(time_ns, dtype=np.int64)
    if t is not None and t.shape != (n,):
        raise input_error(f"time_ns: expected shape ({n},), got {t.shape}")
    if not shuffle_within_group and t is None:
        raise config_error("exchangeability_martingale: time_ns is required when shuffle_within_group is false")
    order_rng = _rng(seed, f"order:{label}")
    names = np.unique(g).tolist()  # numeric groups (session dates as days) sort numerically
    order_parts = []
    for name in names:
        idx = np.flatnonzero(g == name)
        if shuffle_within_group:
            idx = idx[order_rng.permutation(len(idx))]
        else:
            idx = idx[np.argsort(t[idx], kind="stable")]
        order_parts.append(idx)
    order = np.concatenate(order_parts) if order_parts else np.empty(0, dtype=np.int64)
    theta = _theta(_rng(seed, f"martingale:{label}"), n)
    pv = online_pvalues(s[order], theta)
    path = jumper_log10(pv, jumper_rate) if method == "jumper" else mixture_log10(pv)
    lim = math.log10(threshold)
    hits = np.flatnonzero(path >= lim - 1e-12)
    alarm_index = int(hits[0]) if hits.size else None
    stats = []
    pos = 0
    alarm_group = None
    for name, part in zip(names, order_parts, strict=True):
        m = len(part)
        seg = path[pos : pos + m]
        pseg = pv[pos : pos + m]
        if alarm_index is not None and alarm_group is None and pos <= alarm_index < pos + m:
            alarm_group = str(name)
        stats.append(
            GroupDrift(
                group=str(name),
                n=m,
                first_index=pos,
                log10_end=float(seg[-1]),
                log10_max=float(seg.max()),
                mean_pvalue=float(pseg.mean()),
                ks_uniform=_ks_uniform(pseg),
            )
        )
        pos += m
    return MartingaleResult(method, float(threshold), order, pv, path, alarm_index, alarm_group, stats)


# ---- prepared outcomes from hazard / zoo predictions ----------------------------------------------------------


@dataclass
class FillOutcomes:
    """Per (order, landmark) row: probabilities `p` (rows, J) on `grid_ns`, labels `y` (1 / 0 / -1 unknown),
    session date (days since epoch; `dates_known` false when no source gave one and `date` is all 0), landmark
    time, split (hazard only) and named stratum columns ((rows,) or (rows, J) string arrays). `notes` are caveats of
    the preparation that `evaluate` copies into the report's warnings."""

    source: Literal["hazard", "zoo"]
    model_id: str
    score: str
    grid_ns: np.ndarray
    order_ref: np.ndarray
    landmark_ns: np.ndarray
    date: np.ndarray
    time_ns: np.ndarray
    split: np.ndarray | None
    p: np.ndarray
    y: np.ndarray
    strata: dict[str, np.ndarray]
    excluded_unobserved: dict[int, int] = field(default_factory=dict)
    dates_known: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        """Number of rows."""
        return len(self.order_ref)


def _strings(col: pa.ChunkedArray | pa.Array) -> np.ndarray:
    arr = col.combine_chunks() if isinstance(col, pa.ChunkedArray) else col
    return np.asarray(arr.cast(pa.utf8()).to_pylist(), dtype=object)


def _pivot(
    unit: np.ndarray, horizon: np.ndarray, values: dict[str, np.ndarray], what: str
) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray]]:
    """Long (unit, horizon) rows -> unit index (first occurrence order), grid, {name: (units, J)}; every unit must
    have exactly one row per grid horizon."""
    grid = np.unique(horizon)
    check_grid(grid)
    _, first, uinv = np.unique(unit, return_index=True, return_inverse=True)
    rank = np.empty(len(first), dtype=np.int64)
    rank[np.argsort(first, kind="stable")] = np.arange(len(first))
    u = rank[uinv]
    j = np.searchsorted(grid, horizon)
    n_u, j_count = len(first), len(grid)
    count = np.zeros((n_u, j_count), dtype=np.int64)
    np.add.at(count, (u, j), 1)
    if np.any(count != 1):
        raise input_error(f"{what}: every row must have exactly one value per horizon {list(map(int, grid))}")
    out = {}
    for name, v in values.items():
        m = np.empty((n_u, j_count), dtype=v.dtype)
        m[u, j] = v
        out[name] = m
    unit_first = np.empty(n_u, dtype=np.int64)
    unit_first[u] = np.arange(len(u))
    return unit_first, grid, out


def dist_bucket(dist_ticks: np.ndarray) -> np.ndarray:
    """`better` (< 0, inside the same-side best), `touch` (0), `behind1`, `behind2+`, `na` (missing)."""
    d = np.rint(np.asarray(dist_ticks, dtype=np.float64))
    out = np.full(len(d), "na", dtype=object)
    out[d < 0] = "better"
    out[d == 0] = "touch"
    out[d == 1] = "behind1"
    out[d >= 2] = "behind2+"
    return out


def predicted_bucket(p: np.ndarray) -> np.ndarray:
    """Probability bin of width 0.2 (`p[0.0,0.2)` ... `p[0.8,1.0]`); per horizon when `p` is (rows, J)."""
    pp = np.asarray(p, dtype=np.float64)
    b = np.minimum((pp * 5.0).astype(np.int64), 4)
    names = np.array([f"p[{k / 5:.1f},{(k + 1) / 5:.1f}{']' if k == 4 else ')'}" for k in range(5)], dtype=object)
    return names[b]


def _feature_strata(
    features: pa.Table | None, order_ref: np.ndarray, landmark_ns: np.ndarray, what: str, *, missing_ok: bool = False
) -> tuple[dict[str, np.ndarray], np.ndarray | None, np.ndarray | None, np.ndarray | None]:
    """Joins the features on (order_ref, landmark_ns): strata `dist_bucket`, `side`, `instrument`, plus session date
    (days), landmark time (ns) and the outcome rows (for the hazard's label checks). A row without a features row is
    an input error, or with `missing_ok` gets stratum `na` (and no date, time or outcome row)."""
    if features is None:
        return {}, None, None, None
    require_columns(features, ("order_ref", "landmark_ns"), "features")
    f_ref = _strings(features.column("order_ref"))
    f_lm = int64_column(features, "landmark_ns")
    index = {(r, int(lm)): i for i, (r, lm) in enumerate(zip(f_ref.tolist(), f_lm.tolist(), strict=True))}
    keys = zip(order_ref.tolist(), landmark_ns.tolist(), strict=True)
    rows = np.array([index.get((r, int(lm)), -1) for r, lm in keys], dtype=np.int64)
    found = rows >= 0
    if not found.all() and not missing_ok:
        i = int(np.flatnonzero(~found)[0])
        raise input_error(f"{what}: row ({order_ref[i]!r}, {int(landmark_ns[i])}) has no features row")
    safe = np.where(found, rows, 0)

    def col(values: np.ndarray) -> np.ndarray:
        return np.where(found, values, "na").astype(object)

    strata: dict[str, np.ndarray] = {}
    if features.num_rows == 0:
        return strata, None, None, None
    if "dist_ticks" in features.column_names:
        strata["dist_bucket"] = col(dist_bucket(float_matrix(features, ["dist_ticks"])[safe, 0]))
    if "side_sign" in features.column_names:
        s = int64_column(features, "side_sign")[safe]
        strata["side"] = col(np.where(s > 0, "bid", "ask"))
    if "instrument_id" in features.column_names:
        strata["instrument"] = col(int64_column(features, "instrument_id")[safe].astype(str))
    if not found.all():
        return strata, None, None, None
    date = int64_column(features, "session_date")[rows] if "session_date" in features.column_names else None
    tl = int64_column(features, "ts_landmark")[rows] if "ts_landmark" in features.column_names else None
    return strata, date, tl, rows


def prepare_hazard(
    predictions: pa.Table,
    features: pa.Table | None = None,
    *,
    score: str = "cif_fill_raw",
    model_id: str = "",
    labels: bool = True,
) -> FillOutcomes:
    """`qt.hazard_predictions` (+ the `qt.features` it was predicted from, for strata, dates and times) ->
    `FillOutcomes`. Labels: `y_fill` (null = censored before the horizon: unknown), checked against the features'
    `event_ns` / `cause`. With `labels=False` the rows are unlabeled (orders whose outcome is not known yet, for
    `ConformalCalibrator.predict`): `y_fill`, `split` and the features' outcome columns are not needed and every
    label is unknown."""
    if score not in ("cif_fill_raw", "cif_fill"):
        raise config_error(f"score for hazard predictions must be cif_fill_raw or cif_fill, got {score!r}")
    need = ("order_ref", "landmark_ns", "horizon_ns", score) + (("y_fill", "split") if labels else ())
    require_columns(predictions, need, "predictions")
    ref = _strings(predictions.column("order_ref"))
    lm = int64_column(predictions, "landmark_ns")
    hz = int64_column(predictions, "horizon_ns")
    # unit = (order_ref, landmark_ns) as one integer key (no string concatenation: refs are arbitrary text)
    _, ref_id = np.unique(ref.astype(str), return_inverse=True)
    lm_u, lm_id = np.unique(lm, return_inverse=True)
    unit = np.asarray(ref_id, dtype=np.int64).reshape(-1) * len(lm_u) + np.asarray(lm_id).reshape(-1)
    if labels:
        yc = predictions.column("y_fill").combine_chunks()
        nulls = yc.is_null().to_numpy(zero_copy_only=False)
        y = np.where(nulls, -1, yc.fill_null(False).to_numpy(zero_copy_only=False))
    else:
        y = np.full(predictions.num_rows, -1)
    p = float_matrix(predictions, [score])[:, 0]
    has_split = "split" in predictions.column_names
    split = _strings(predictions.column("split")) if has_split else np.zeros(predictions.num_rows, dtype=object)
    first, grid, piv = _pivot(unit, hz, {"p": p, "y": y.astype(np.int8)}, "predictions")
    o_ref, o_lm, o_split = ref[first], lm[first], split[first]
    strata, date, tl, frows = _feature_strata(features, o_ref, o_lm, "predictions")
    if labels and features is not None and frows is not None:
        e, c = outcome_arrays(features)
        e, c = e[frows], c[frows]
        want = np.where((c[:, None] == FILL) & (e[:, None] <= grid[None, :]), 1, 0)
        unknown = (c[:, None] == CENSOR) & (e[:, None] < grid[None, :])
        want = np.where(unknown, -1, want)
        if np.any(want != piv["y"]):
            raise input_error("predictions: y_fill disagrees with the features' event_ns / cause")
    n = len(first)
    notes = []
    if date is None:
        notes.append(
            "session dates unknown (no features table with session_date): the martingale orders the calibration "
            "rows, then the test rows, without date groups"
        )
    return FillOutcomes(
        source="hazard",
        model_id=model_id,
        score=score,
        grid_ns=grid,
        order_ref=o_ref,
        landmark_ns=o_lm,
        date=date if date is not None else np.zeros(n, dtype=np.int64),
        time_ns=tl if tl is not None else np.zeros(n, dtype=np.int64),
        split=o_split if has_split else None,
        p=piv["p"],
        y=piv["y"],
        strata=strata,
        excluded_unobserved={int(h): int((piv["y"][:, j] < 0).sum()) for j, h in enumerate(grid)} if labels else {},
        dates_known=date is not None,
        notes=notes,
    )


ZooPopulation = Literal["never_cancel", "cancel_bearing"]


def prepare_zoo(
    zoo_predictions: pa.Table,
    model_id: str,
    planned_life: pa.Table | None,
    features: pa.Table | None = None,
    *,
    population: ZooPopulation = "never_cancel",
) -> FillOutcomes:
    """`qt.zoo_predictions` of one model + its planned-life table (`qt.zoo_shadows`: `shadow_id`,
    `ts_cancel_planned`, `ts_admin_end`) (+ optional `qt.features` for strata of the same shadows at landmark 0) ->
    `FillOutcomes` at landmark 0 with labels `truth_filled`, under the rules of `evaluate_zoo` (SPEC 5.6, audit
    ruling A2, E38 OC-7):

    - `planned_life` is required (`QT-INPUT-001`) and must cover every shadow (`QT-INPUT-002`); the label at
      horizon `h` exists iff `ts_admin_end` is null or `>= ts_live + h` (and `truth_filled` is not null), never from
      the outcome-flavoured `censored` column.
    - Populations are evaluated separately: `never_cancel` (no planned cancel before `ts_live` + the largest
      horizon; `p_fill` and `truth_filled` are then the same never-cancel estimand) or `cancel_bearing` (e.g.
      real-order mirrors: the label is a fill before the shadow's own cancel, a competing event, which conformal
      validity allows since `p_fill` is only the score).
    - Every row of a shadow that any faithful hbt pair aborted on at #312 is left out (`common_population`), so all
      models share one population."""
    from queuetruth.errors import QtError
    from queuetruth.eval.zoo import _planned_life, aborted_shadows, common_population

    if planned_life is None:
        raise QtError(
            "QT-INPUT-001",
            "prepare_zoo: planned_life (qt.zoo_shadows: shadow_id, ts_cancel_planned, ts_admin_end) is required; "
            "label eligibility is never inferred from the censored flag (audit ruling A2, E38 OC-7)",
            "pass pyarrow.parquet.read_table('<zoo out>/zoo_shadows.parquet')",
        )
    if population not in ("never_cancel", "cancel_bearing"):
        raise config_error(f"population must be never_cancel or cancel_bearing, got {population!r}")
    require_columns(
        zoo_predictions,
        ("model_id", "shadow_id", "horizon_ns", "p_fill", "truth_filled", "session_date", "ts_live", "instrument_id"),
        "zoo_predictions",
    )
    mid_all = _strings(zoo_predictions.column("model_id"))
    if not (mid_all == model_id).any():
        known = sorted(set(mid_all.tolist()))
        raise input_error(f"zoo_predictions: no rows for model {model_id!r} (models: {known})", model_id=model_id)
    n_aborted = len(aborted_shadows(zoo_predictions))
    common = common_population(zoo_predictions)
    t = common.filter(pa.array(_strings(common.column("model_id")) == model_id))
    sid = _strings(t.column("shadow_id"))
    hz = int64_column(t, "horizon_ns")
    tf = t.column("truth_filled").combine_chunks()
    tf_null = tf.is_null().to_numpy(zero_copy_only=False)
    truth = np.where(tf_null, -1, tf.fill_null(False).to_numpy(zero_copy_only=False))
    p = float_matrix(t, ["p_fill"])[:, 0]
    if t.num_rows == 0:
        raise input_error(f"zoo_predictions: every shadow of model {model_id!r} is outside the common population")
    first, grid, piv = _pivot(sid, hz, {"p": p, "y": truth.astype(np.int8)}, "zoo_predictions")
    ids = sid[first]
    live = int64_column(t, "ts_live")[first]
    cancel_rel, admin_rel = _planned_life(planned_life, ids, live)
    never = cancel_rel >= float(grid[-1])
    keep = never if population == "never_cancel" else ~never
    if not keep.any():
        raise input_error(f"zoo_predictions: model {model_id!r} has no {population} shadow")
    eligible = admin_rel[:, None] >= grid[None, :].astype(np.float64)
    y = np.where(eligible, piv["y"], -1).astype(np.int8)[keep]
    pp = piv["p"][keep]
    o_ref = ids[keep]
    date = int64_column(t, "session_date")[first][keep]
    tl = live[keep]
    lm = np.zeros(len(o_ref), dtype=np.int64)
    notes = []
    if n_aborted:
        notes.append(f"zoo: {n_aborted} shadow(s) aborted by an hbt pair at #312 left out (common population)")
    if (~keep).any():
        other = "cancel_bearing" if population == "never_cancel" else "never_cancel"
        notes.append(f"zoo: {int((~keep).sum())} {other} shadow(s) left out (population {population})")
    strata, _, _, _ = _feature_strata(features, o_ref, lm, "zoo_predictions", missing_ok=True)
    if features is not None and strata:
        n_na = int((next(iter(strata.values())) == "na").sum())
        if n_na:
            notes.append(f"zoo: {n_na} of {len(o_ref)} shadow(s) have no features row at landmark 0: stratum na")
    strata["instrument"] = int64_column(t, "instrument_id")[first][keep].astype(str).astype(object)
    strata["predicted"] = predicted_bucket(pp)
    return FillOutcomes(
        source="zoo",
        model_id=model_id,
        score="p_fill",
        grid_ns=grid,
        order_ref=o_ref,
        landmark_ns=lm,
        date=date,
        time_ns=tl,
        split=None,
        p=pp,
        y=y,
        strata=strata,
        excluded_unobserved={int(h): int((y[:, j] < 0).sum()) for j, h in enumerate(grid)},
        notes=notes,
    )


# ---- calibration / test selections ----------------------------------------------------------------------------


def mask_split(out: FillOutcomes, name: str) -> np.ndarray:
    """Rows of one hazard split (`train`, `calib`, `test`)."""
    if out.split is None:
        raise config_error(f"{out.source} outcomes have no split column: select rows by date")
    return out.split == name


def _date_days(d: str) -> int:
    import datetime as dt

    try:
        return (dt.date.fromisoformat(d) - dt.date(1970, 1, 1)).days
    except ValueError:
        raise config_error(f"invalid date {d!r} (expected YYYY-MM-DD)") from None


def mask_dates(out: FillOutcomes, dates: list[str] | tuple[str, ...]) -> np.ndarray:
    """Rows whose session date is one of `dates` (`YYYY-MM-DD`)."""
    return np.isin(out.date, [_date_days(d) for d in dates])


def random_order_halves(out: FillOutcomes, mask: np.ndarray, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Splits the masked rows into two halves by order (every landmark of an order on the same side), uniformly at
    random with the seed: the halves are exchangeable by construction."""
    refs = np.unique(out.order_ref[mask].astype(str))
    perm = _rng(seed, "halves").permutation(len(refs))
    first = set(refs[perm[: len(refs) // 2]].tolist())
    in_first = np.fromiter((r in first for r in out.order_ref.tolist()), dtype=bool, count=out.n)
    return mask & in_first, mask & ~in_first


# ---- report ---------------------------------------------------------------------------------------------------


def conformal_spec(spec: ConformalSpec | dict[str, Any] | None) -> ConformalSpec:
    """Validates a spec: `QT-CONFIG-001` naming the first invalid field."""
    if isinstance(spec, ConformalSpec):
        return spec
    try:
        return ConformalSpec.model_validate(spec or {})
    except ValidationError as err:
        first = err.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise config_error(f"conformal spec: {loc}: {first.get('msg')}", field=loc) from None


def _ratio(num: float, den: float) -> float | None:
    return float(num / den) if den > 0 else None


def clopper_pearson(k: int, n: int, level: float = 0.95) -> tuple[float | None, float | None]:
    """Exact binomial interval for `k` successes in `n` (rows treated as independent)."""
    if n <= 0:
        return None, None
    a = (1.0 - level) / 2.0
    lo = 0.0 if k == 0 else float(beta_dist.ppf(a, k, n - k + 1))
    hi = 1.0 if k == n else float(beta_dist.ppf(1.0 - a, k + 1, n - k))
    return lo, hi


def _iso(days: int) -> str:
    import datetime as dt

    return (dt.date(1970, 1, 1) + dt.timedelta(days=int(days))).isoformat()


def _stratum_labels(out: FillOutcomes, names: list[str], j: int) -> np.ndarray:
    if not names:
        return np.full(out.n, ALL, dtype=object)
    parts = []
    for name in names:
        col = out.strata.get(name)
        if col is None:
            raise input_error(
                f"stratum {name!r} is not available for these outcomes (available: {sorted(out.strata)}); pass the "
                "features table for dist_bucket / side"
            )
        col = col[:, j] if col.ndim == 2 else col
        parts.append(np.char.add(f"{name}=", col.astype(str)))
    lab = parts[0]
    for x in parts[1:]:
        lab = np.char.add(np.char.add(lab, "|"), x)
    return lab.astype(object)


def _check_rows(out: FillOutcomes, mask: np.ndarray, role: str) -> None:
    """The probabilities must not have been fitted on calibration or test rows (validity condition 1)."""
    if out.source != "hazard" or out.split is None:
        return
    splits = set(out.split[mask].tolist())
    if "train" in splits:
        raise config_error(
            f"{role} rows include the hazard's train split: the GBDT was fitted on them, so conformal scores there "
            "are not exchangeable with scores of new orders"
        )
    if out.score == "cif_fill" and "calib" in splits:
        raise config_error(
            f"score cif_fill with {role} rows from the hazard's calib split: the isotonic layer was fitted on those "
            "rows; use cif_fill_raw, or rows the isotonic layer did not see"
        )


def _check_selection(out: FillOutcomes, calib: np.ndarray, test: np.ndarray) -> None:
    if calib.shape != (out.n,) or test.shape != (out.n,):
        raise input_error("calib / test masks must have one entry per row")
    if np.any(calib & test):
        raise config_error("calibration and test rows overlap")
    if not calib.any():
        raise config_error("no calibration rows selected")
    if not test.any():
        raise config_error("no test rows selected")
    _check_rows(out, calib, "calibration")
    _check_rows(out, test, "test")


GUARANTEE = (
    "Within every category (landmark x stratum, and label when label_conditional), if the calibration and test rows "
    "are exchangeable and the probabilities were produced without the calibration or test rows, then P(Y in C(X)) "
    ">= 1 - alpha (= 1 - alpha with smoothed p-values), the no-fill-within lower bound and the fill-within upper "
    "bound each hold with probability >= 1 - alpha, and the two-sided fill-time interval (alpha / 2 per side) with "
    "probability >= 1 - alpha. Labels are uncensored: a row enters a horizon only when its outcome is observed "
    "there, and shadows are followed to an administrative end known when they go live (feed-gap exclusions are "
    "assumed outcome-independent). Exchangeability of the calibration and test rows is tested, not assumed: see "
    "drift (alarm at level 1 / alarm_threshold per cell; drift_familywise_alarm over all cells)."
)


# ---- calibrator -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Predicted:
    """Calibrator output on selected rows (`rows`, ascending): per row and horizon the stratum label (without the
    landmark), p-values `(m, J, 2)`, set membership, Venn-Abers `(p0, p1)`; per row the fill-time bounds as grid
    indices (`lo1`, `hi1` one-sided at alpha; `lo2`, `hi2` the two-sided interval)."""

    rows: np.ndarray
    strata: np.ndarray
    pv: np.ndarray
    inset: np.ndarray
    va0: np.ndarray
    va1: np.ndarray
    lo1: np.ndarray
    hi1: np.ndarray
    lo2: np.ndarray
    hi2: np.ndarray


def _calibrator_schema() -> pa.Schema:
    fields = [
        pa.field("part", pa.utf8(), nullable=False),
        pa.field("horizon_ns", pa.int64(), nullable=True),
        pa.field("landmark_ns", pa.int64(), nullable=False),
        pa.field("category", pa.utf8(), nullable=False),
        pa.field("label", pa.int8(), nullable=False),
        pa.field("value", pa.float64(), nullable=False),
        pa.field("weight", pa.float64(), nullable=True),
        pa.field("label_sum", pa.float64(), nullable=True),
    ]
    meta = {
        b"qt.schema": CALIBRATOR_SCHEMA_NAME.encode(),
        b"qt.schema_version": b"1",
        b"qt.engine_version": ENGINE_VERSION.encode(),
    }
    return pa.schema(fields, metadata=meta)


class ConformalCalibrator:
    """The calibration of `evaluate`, fitted once and applied to any rows (module docstring).

    Per horizon x landmark a `BinaryConformal` and a `VennAbers` over the spec's strata, per landmark a
    `FillTimeConformal` (strata without `predicted`). `predict` needs no labels: it scores tomorrow's orders
    (`prepare_hazard(..., labels=False)`). A landmark or horizon without calibration rows gives uninformative
    output (p-value 1 or `theta`, interval `(0, 1)`, no fill-time claim). `save` / `load` round-trip it exactly
    through `conformal_calibrator.parquet` (`qt.conformal_calibrator`: one row per stored score, the settings in
    the schema metadata)."""

    def __init__(
        self,
        *,
        spec: ConformalSpec,
        grid_ns: np.ndarray,
        source: str,
        score: str,
        model_id: str,
        n_calib_rows: int,
        sets: dict[tuple[int, int], BinaryConformal],
        va: dict[tuple[int, int], VennAbers],
        time: dict[int, FillTimeConformal],
    ) -> None:
        self.spec = spec
        self.grid_ns = check_grid(grid_ns)
        self.source = source
        self.score = score
        self.model_id = model_id
        self.n_calib_rows = int(n_calib_rows)
        self.sets = sets
        self.va = va
        self.time = time

    @property
    def time_strata(self) -> list[str]:
        """Strata of the fill-time categories (the spec's, without the per-horizon `predicted`)."""
        return [s for s in self.spec.strata if s != "predicted"]

    @classmethod
    def fit(
        cls, out: FillOutcomes, calib: np.ndarray, spec: ConformalSpec | dict[str, Any] | None = None
    ) -> ConformalCalibrator:
        """Calibrates on the `calib` rows (with a known label at the horizon; rows known at every horizon for the
        fill-time bounds). Refuses rows the probabilities were fitted on (`QT-CONFIG-001`)."""
        sp = conformal_spec(spec)
        calib = np.asarray(calib, dtype=bool)
        if calib.shape != (out.n,):
            raise input_error("calib mask must have one entry per row")
        if not calib.any():
            raise config_error("no calibration rows selected")
        _check_rows(out, calib, "calibration")
        grid = out.grid_ns
        landmarks = sorted(set(out.landmark_ns[calib].tolist()))
        sets: dict[tuple[int, int], BinaryConformal] = {}
        va: dict[tuple[int, int], VennAbers] = {}
        for j, h in enumerate(grid.tolist()):
            strata_j = _stratum_labels(out, list(sp.strata), j)
            known = out.y[:, j] >= 0
            for lm in landmarks:
                ci = np.flatnonzero(calib & (out.landmark_ns == lm) & known)
                if len(ci) == 0:
                    continue
                yc, pc, st = out.y[ci, j].astype(np.int8), out.p[ci, j], strata_j[ci]
                sets[(h, lm)] = BinaryConformal(label_conditional=sp.label_conditional).fit(pc, yc, st)
                va[(h, lm)] = VennAbers().fit(pc, yc, st)
        k_all, observed = fill_interval(out.y)
        tstrata = _stratum_labels(out, [s for s in sp.strata if s != "predicted"], len(grid) - 1)
        time: dict[int, FillTimeConformal] = {}
        for lm in landmarks:
            ci = np.flatnonzero(calib & (out.landmark_ns == lm) & observed)
            if len(ci):
                time[lm] = FillTimeConformal(grid).fit(out.p[ci], k_all[ci], tstrata[ci])
        return cls(
            spec=sp,
            grid_ns=grid,
            source=out.source,
            score=out.score,
            model_id=out.model_id,
            n_calib_rows=int(calib.sum()),
            sets=sets,
            va=va,
            time=time,
        )

    def _predict(self, out: FillOutcomes, rows: np.ndarray | None = None) -> _Predicted:
        if not np.array_equal(out.grid_ns, self.grid_ns):
            raise input_error(
                f"outcomes grid {out.grid_ns.tolist()} differs from the calibrator grid {self.grid_ns.tolist()}"
            )
        if out.score != self.score:
            raise config_error(f"calibrator was fitted on {self.score!r} scores, these outcomes carry {out.score!r}")
        sp = self.spec
        mask = np.ones(out.n, dtype=bool) if rows is None else np.asarray(rows, dtype=bool)
        if mask.shape != (out.n,):
            raise input_error("rows mask must have one entry per row")
        idx = np.flatnonzero(mask)
        m, j_count = len(idx), len(self.grid_ns)
        strata = np.empty((m, j_count), dtype=object)
        pv = np.ones((m, j_count, 2))
        va0 = np.zeros((m, j_count))
        va1 = np.ones((m, j_count))
        lms = out.landmark_ns[idx]
        empty = BinaryConformal(label_conditional=sp.label_conditional)
        empty._fitted = True
        for j, h in enumerate(self.grid_ns.tolist()):
            st = _stratum_labels(out, list(sp.strata), j)[idx]
            strata[:, j] = st
            for lm in sorted(set(lms.tolist())):
                sel = np.flatnonzero(lms == lm)
                p = out.p[idx[sel], j]
                theta = _theta(_rng(sp.seed, f"sets:{h}:{lm}"), len(sel)) if sp.smoothed else None
                pv[sel, j] = self.sets.get((h, lm), empty).pvalues(p, st[sel], theta=theta)
                vam = self.va.get((h, lm))
                if vam is not None:
                    va0[sel, j], va1[sel, j] = vam.predict(p, st[sel])
        lo1 = np.zeros(m, dtype=np.int64)
        lo2 = np.zeros(m, dtype=np.int64)
        hi1 = np.full(m, -1, dtype=np.int64)
        hi2 = np.full(m, -1, dtype=np.int64)
        tst = _stratum_labels(out, self.time_strata, j_count - 1)[idx]
        for lm, ft in self.time.items():
            sel = np.flatnonzero(lms == lm)
            if len(sel) == 0:
                continue
            c = out.p[idx[sel]]
            lo1[sel], hi1[sel] = ft.lower(c, sp.alpha, tst[sel]), ft.upper(c, sp.alpha, tst[sel])
            lo2[sel], hi2[sel] = ft.interval(c, sp.alpha, tst[sel])
        return _Predicted(idx, strata, pv, pv > sp.alpha, va0, va1, lo1, hi1, lo2, hi2)

    def predict(self, out: FillOutcomes, rows: np.ndarray | None = None) -> pa.Table:
        """`qt.conformal_sets` rows for every selected row (default: all) and horizon, labelled or not (`y_fill`
        null when the label is unknown). Smoothing variables are drawn per horizon x landmark over the selected rows
        in row order, so predicting the test rows of `evaluate` reproduces its sets."""
        return _sets_table(out, self._predict(out, rows), known_only=False)

    def save(self, path: str | Path) -> dict[str, Any]:
        """Writes `conformal_calibrator.parquet`; returns its `Files` entry."""
        cols: dict[str, list[Any]] = {f.name: [] for f in _calibrator_schema()}

        def add(part: str, h: int | None, lm: int, cat: str, label: int, values, weights=None, sums=None) -> None:
            n = len(values)
            cols["part"] += [part] * n
            cols["horizon_ns"] += [h] * n
            cols["landmark_ns"] += [lm] * n
            cols["category"] += [cat] * n
            cols["label"] += [label] * n
            cols["value"] += np.asarray(values, dtype=np.float64).tolist()
            cols["weight"] += [None] * n if weights is None else np.asarray(weights, dtype=np.float64).tolist()
            cols["label_sum"] += [None] * n if sums is None else np.asarray(sums, dtype=np.float64).tolist()

        for (h, lm), bc in sorted(self.sets.items()):
            for (cat, label), s in sorted(bc._cal.items()):
                add("set", h, lm, cat, label, s)
        for (h, lm), vam in sorted(self.va.items()):
            for cat, vs in sorted(vam._st.items()):
                add("va", h, lm, cat, -1, vs.u, vs.w, vs.sy)
        for lm, ft in sorted(self.time.items()):
            for cat in sorted(ft._lo):
                add("time_lo", None, lm, cat, -1, ft._lo[cat])
                add("time_hi", None, lm, cat, -1, ft._hi[cat])
        schema = _calibrator_schema()
        settings = {
            "spec": self.spec.model_dump(mode="json"),
            "grid_ns": [int(g) for g in self.grid_ns],
            "source": self.source,
            "score": self.score,
            "model_id": self.model_id,
            "n_calib_rows": self.n_calib_rows,
        }
        meta = {**(schema.metadata or {}), b"qt.conformal_calibrator": json.dumps(settings, sort_keys=True).encode()}
        schema = schema.with_metadata(meta)
        table = pa.Table.from_pydict(cols, schema=schema)
        return write_table(table, path, schema)

    @classmethod
    def load(cls, path: str | Path) -> ConformalCalibrator:
        """Reads a calibrator written by `save` (`QT-INPUT-001` when missing, `QT-INPUT-002` when it is not a
        `qt.conformal_calibrator` table)."""
        import pyarrow.parquet as pq

        from queuetruth.errors import QtError

        p = Path(path)
        if not p.is_file():
            raise QtError("QT-INPUT-001", f"{p}: not found")
        t = pq.read_table(p)
        meta = t.schema.metadata or {}
        if meta.get(b"qt.schema") != CALIBRATOR_SCHEMA_NAME.encode() or meta.get(b"qt.schema_version") != b"1":
            raise input_error(f"{p}: not a {CALIBRATOR_SCHEMA_NAME} table (schema_version 1)")
        require_columns(t, [f.name for f in _calibrator_schema()], CALIBRATOR_SCHEMA_NAME)
        settings = json.loads(meta[b"qt.conformal_calibrator"])
        sp = conformal_spec(settings["spec"])
        grid = np.asarray(settings["grid_ns"], dtype=np.int64)
        part = np.asarray(t.column("part").to_pylist(), dtype=object)
        h = t.column("horizon_ns").to_pylist()
        lm = t.column("landmark_ns").to_pylist()
        cat = t.column("category").to_pylist()
        lab = t.column("label").to_pylist()
        val = t.column("value").to_numpy()
        wt = t.column("weight").to_numpy(zero_copy_only=False)
        sy = t.column("label_sum").to_numpy(zero_copy_only=False)
        groups: dict[tuple[Any, ...], list[int]] = {}
        for i, key in enumerate(zip(part.tolist(), h, lm, cat, lab, strict=True)):
            groups.setdefault(key, []).append(i)
        sets: dict[tuple[int, int], BinaryConformal] = {}
        va: dict[tuple[int, int], VennAbers] = {}
        time: dict[int, FillTimeConformal] = {}
        for (pt, hh, ll, cc, yy), ix in groups.items():
            ii = np.asarray(ix)
            if pt == "set":
                bc = sets.setdefault((int(hh), int(ll)), BinaryConformal(label_conditional=sp.label_conditional))
                bc._cal[(str(cc), int(yy))] = np.sort(val[ii])
                bc._fitted = True
            elif pt == "va":
                vam = va.setdefault((int(hh), int(ll)), VennAbers())
                order = np.argsort(val[ii], kind="stable")
                vam._st[str(cc)] = _VaStratum(val[ii][order], wt[ii][order], sy[ii][order])
                vam._fitted = True
            elif pt in ("time_lo", "time_hi"):
                ft = time.setdefault(int(ll), FillTimeConformal(grid))
                (ft._lo if pt == "time_lo" else ft._hi)[str(cc)] = np.sort(val[ii])
                ft._fitted = True
            else:
                raise input_error(f"{p}: unknown part {pt!r}")
        return cls(
            spec=sp,
            grid_ns=grid,
            source=str(settings["source"]),
            score=str(settings["score"]),
            model_id=str(settings["model_id"]),
            n_calib_rows=int(settings["n_calib_rows"]),
            sets=sets,
            va=va,
            time=time,
        )


# ---- evaluation -----------------------------------------------------------------------------------------------


def evaluate(
    out: FillOutcomes, calib: np.ndarray, test: np.ndarray, spec: ConformalSpec | dict[str, Any] | None = None
) -> tuple[ConformalReport, pa.Table]:
    """Calibrates on the `calib` rows (`ConformalCalibrator.fit`) and evaluates on the `test` rows: the report and
    the `qt.conformal_sets` table (one row per test row x horizon with a known label)."""
    sp = conformal_spec(spec)
    calib = np.asarray(calib, dtype=bool)
    test = np.asarray(test, dtype=bool)
    _check_selection(out, calib, test)
    warnings: list[str] = list(out.notes)
    alpha = sp.alpha
    grid = out.grid_ns
    landmarks = sorted(set(out.landmark_ns[calib | test].tolist()))
    n_inf = min_informative_n(alpha)
    cal = ConformalCalibrator.fit(out, calib, sp)
    pred = cal._predict(out, test)
    pos = np.full(out.n, -1, dtype=np.int64)
    pos[pred.rows] = np.arange(len(pred.rows))
    cells: list[CoverageCell] = []
    low_cal_strata: set[str] = set()

    def cell(h: int, lm: int | None, name: str, ncal: int, yt, inset, ycal, pt, va0, va1) -> CoverageCell:
        n_t = len(yt)
        hit = inset[np.arange(n_t), yt] if n_t else np.zeros(0, dtype=bool)
        covered = int(hit.sum())
        size = inset.sum(axis=1) if n_t else np.zeros(0)
        lo, hi = clopper_pearson(covered, n_t)
        filled = yt == 1
        return CoverageCell(
            horizon_ns=h,
            landmark_ns=lm,
            stratum=name,
            n_calib=ncal,
            n_test=n_t,
            n_test_fill=int(filled.sum()),
            coverage=_ratio(covered, n_t),
            coverage_ci_lo=lo,
            coverage_ci_hi=hi,
            frac_empty=_ratio(float((size == 0).sum()), n_t),
            frac_singleton=_ratio(float((size == 1).sum()), n_t),
            frac_both=_ratio(float((size == 2).sum()), n_t),
            fill_rate_calib=float(ycal.mean()) if len(ycal) else None,
            fill_rate_test=float(yt.mean()) if n_t else None,
            mean_p_test=float(pt.mean()) if n_t else None,
            va_p0_mean=float(va0.mean()) if n_t else None,
            va_p1_mean=float(va1.mean()) if n_t else None,
            va_true_label_mean=float(np.where(filled, va1, va0).mean()) if n_t else None,
            coverage_fill=_ratio(float(hit[filled].sum()), float(filled.sum())),
            coverage_nofill=_ratio(float(hit[~filled].sum()), float((~filled).sum())),
            informative=bool(ncal >= n_inf),
        )

    drift: list[DriftCell] = []
    for j, h in enumerate(grid.tolist()):
        strata_j = _stratum_labels(out, list(sp.strata), j)
        pooled: dict[str, list[np.ndarray]] = {k: [] for k in ("y", "in", "ycal", "p", "va0", "va1")}
        for lm in landmarks:
            at = out.landmark_ns == lm
            known = out.y[:, j] >= 0
            ci = np.flatnonzero(calib & at & known)
            ti = np.flatnonzero(test & at & known)
            if len(ci) == 0 or len(ti) == 0:
                warnings.append(f"horizon {h} ns landmark {lm} ns: {len(ci)} calibration / {len(ti)} test rows")
                continue
            q = pos[ti]
            yc, yt = out.y[ci, j].astype(np.int8), out.y[ti, j].astype(np.int8)
            pt = out.p[ti, j]
            inset, va0, va1 = pred.inset[q, j], pred.va0[q, j], pred.va1[q, j]
            if sp.strata:  # without strata the only category is the landmark: the `all` cell below
                for name in sorted(set(strata_j[ci].tolist()) | set(strata_j[ti].tolist())):
                    mc = strata_j[ci] == name
                    mt = strata_j[ti] == name
                    ycm = yc[mc]
                    ncal = int(mc.sum()) if not sp.label_conditional else int(min((ycm == 0).sum(), (ycm == 1).sum()))
                    if ncal < n_inf:
                        low_cal_strata.add(f"landmark={lm}|{name}")
                    cells.append(cell(h, lm, name, ncal, yt[mt], inset[mt], ycm, pt[mt], va0[mt], va1[mt]))
            elif (len(ci) if not sp.label_conditional else min((yc == 0).sum(), (yc == 1).sum())) < n_inf:
                low_cal_strata.add(f"landmark={lm}")
            cells.append(cell(h, lm, ALL, len(ci), yt, inset, yc, pt, va0, va1))
            for k, v in (("y", yt), ("in", inset), ("ycal", yc), ("p", pt), ("va0", va0), ("va1", va1)):
                pooled[k].append(v)
            drift.append(_drift_cell(out, sp, j, h, lm, ci, ti))
        if len(landmarks) > 1 and pooled["y"]:
            c = {k: np.concatenate(v) for k, v in pooled.items()}
            ncal = len(c["ycal"])
            cells.append(cell(h, None, ALL, ncal, c["y"], c["in"], c["ycal"], c["p"], c["va0"], c["va1"]))
    if low_cal_strata:
        what = (
            "their smoothed sets are randomized and uninformative (a label is dropped at random)"
            if sp.smoothed
            else "their sets always contain both labels"
        )
        warnings.append(
            f"{len(low_cal_strata)} categories have fewer than {n_inf} calibration rows: {what} (alpha = {alpha}): "
            f"{sorted(low_cal_strata)[:5]}"
        )
    unobserved = {h: c for h, c in out.excluded_unobserved.items() if c}
    if unobserved:
        warnings.append(
            f"rows with an unknown label (outcome not observed by the horizon), excluded per horizon (ns): {unobserved}"
        )
    fw_threshold: float | None = None
    fw_alarm = False
    if drift:
        fw_threshold = sp.alarm_threshold * len(drift)
        fw_alarm = max(d.log10_max for d in drift) >= math.log10(fw_threshold) - 1e-12
    calib_dates = sorted({_iso(int(d)) for d in out.date[calib].tolist()}) if out.dates_known else []
    test_dates = sorted({_iso(int(d)) for d in out.date[test].tolist()}) if out.dates_known else []
    report = ConformalReport(
        run_id=_run_id(out, sp, calib, test),
        spec=spec_echo(sp.model_dump(mode="json")),
        source=out.source,
        model_id=out.model_id,
        score=out.score,
        alpha=alpha,
        grid_ns=[int(g) for g in grid],
        n_calib_rows=int(calib.sum()),
        n_test_rows=int(test.sum()),
        calib_dates=calib_dates,
        test_dates=test_dates,
        guarantee=GUARANTEE,
        cells=cells,
        fill_time=_fill_time_cells(out, calib, test, sp, landmarks, pred, pos),
        drift=drift,
        drift_familywise_threshold=fw_threshold,
        drift_familywise_alarm=fw_alarm,
        warnings=warnings,
    )
    return report, _sets_table(out, pred, known_only=True)


def _drift_cell(
    out: FillOutcomes, sp: ConformalSpec, j: int, h: int, lm: int, ci: np.ndarray, ti: np.ndarray
) -> DriftCell:
    """The martingale over the calibration rows (by date), then the test rows (by date): a shift between the
    calibration and the test rows is visible also when they share a session date."""
    rows = np.concatenate([ci, ti])
    role = np.array(["calib"] * len(ci) + ["test"] * len(ti), dtype=object)
    if out.dates_known:
        dates = np.array([_iso(int(d)) for d in out.date[rows].tolist()], dtype=object)
        groups = np.char.add(np.char.add(role.astype(str), ":"), dates.astype(str)).astype(object)
    else:
        groups = role
    mres = exchangeability_martingale(
        nonconformity(out.p[rows, j], out.y[rows, j]),
        groups,  # "calib:..." sorts before "test:..."; ISO dates sort chronologically
        method=sp.martingale,
        jumper_rate=sp.jumper_rate,
        threshold=sp.alarm_threshold,
        shuffle_within_group=sp.shuffle_within_day,
        time_ns=out.time_ns[rows],
        seed=sp.seed,
        label=f"{h}:{lm}",
    )

    def split(g: str) -> tuple[Literal["calib", "test"], str | None]:
        r, _, d = g.partition(":")
        return ("calib" if r == "calib" else "test"), (d or None)

    return DriftCell(
        horizon_ns=h,
        landmark_ns=lm,
        method=mres.method,
        n=len(rows),
        log10_final=float(mres.log10_path[-1]),
        log10_max=mres.log10_max,
        alarm=mres.alarm,
        alarm_group=mres.alarm_group,
        groups=[
            DriftGroup(
                group=g.group,
                role=split(g.group)[0],
                date=split(g.group)[1],
                n=g.n,
                log10_end=g.log10_end,
                log10_max=g.log10_max,
                mean_pvalue=g.mean_pvalue,
                ks_uniform=g.ks_uniform,
            )
            for g in mres.groups
        ],
    )


def _fill_time_cells(
    out: FillOutcomes,
    calib: np.ndarray,
    test: np.ndarray,
    sp: ConformalSpec,
    landmarks: list[int],
    pred: _Predicted,
    pos: np.ndarray,
) -> list[FillTimeCell]:
    grid = out.grid_ns
    j_count = len(grid)
    k_all, observed = fill_interval(out.y)
    names = [s for s in sp.strata if s != "predicted"]
    strata = _stratum_labels(out, names, j_count - 1)
    cells: list[FillTimeCell] = []
    pooled: dict[str, list[np.ndarray]] = {"k": [], "lo1": [], "hi1": [], "lo2": [], "hi2": []}
    ncal_all = 0

    def ftcell(lm: int | None, name: str, ncal: int, k, lo1, hi1, lo2, hi2) -> FillTimeCell:
        n_t = len(k)
        lc, uc = lower_covered(k, lo1), upper_covered(k, hi1)
        both = lower_covered(k, lo2) & upper_covered(k, hi2)
        claim_lo = lo1 > 0
        claim_hi = hi1 > 0
        return FillTimeCell(
            landmark_ns=lm,
            stratum=name,
            n_calib=ncal,
            n_test=n_t,
            lower_coverage=_ratio(float(lc.sum()), n_t),
            upper_coverage=_ratio(float(uc.sum()), n_t),
            interval_coverage=_ratio(float(both.sum()), n_t),
            frac_lower_claim=_ratio(float(claim_lo.sum()), n_t),
            frac_upper_claim=_ratio(float(claim_hi.sum()), n_t),
            median_lower_ns=int(np.median(grid[lo1[claim_lo] - 1])) if claim_lo.any() else None,
            median_upper_ns=int(np.median(grid[hi1[claim_hi] - 1])) if claim_hi.any() else None,
        )

    for lm in landmarks:
        at = out.landmark_ns == lm
        ci = np.flatnonzero(calib & at & observed)
        ti = np.flatnonzero(test & at & observed)
        if len(ci) == 0 or len(ti) == 0:
            continue
        q = pos[ti]
        st_t, kt = strata[ti], k_all[ti]
        lo1, hi1, lo2, hi2 = pred.lo1[q], pred.hi1[q], pred.lo2[q], pred.hi2[q]
        if names:
            for name in sorted(set(strata[ci].tolist()) | set(st_t.tolist())):
                m = st_t == name
                ncal = int((strata[ci] == name).sum())
                cells.append(ftcell(lm, name, ncal, kt[m], lo1[m], hi1[m], lo2[m], hi2[m]))
        cells.append(ftcell(lm, ALL, len(ci), kt, lo1, hi1, lo2, hi2))
        for key, v in (("k", kt), ("lo1", lo1), ("hi1", hi1), ("lo2", lo2), ("hi2", hi2)):
            pooled[key].append(v)
        ncal_all += len(ci)
    if len(landmarks) > 1 and pooled["k"]:
        cat = {k: np.concatenate(v) for k, v in pooled.items()}
        cells.append(ftcell(None, ALL, ncal_all, cat["k"], cat["lo1"], cat["hi1"], cat["lo2"], cat["hi2"]))
    return cells


def sets_schema() -> pa.Schema:
    """Arrow schema of `conformal_sets.parquet` (`qt.conformal_sets`). The fill-time columns are per row:
    `no_fill_within_ns` / `fill_within_ns` are the one-sided bounds at `alpha` (each holds with probability
    `>= 1 - alpha`), the `interval_*` pair is the two-sided interval (`alpha / 2` per side, jointly `>= 1 - alpha`);
    null = no claim."""
    fields = [
        pa.field("order_ref", pa.utf8(), nullable=False),
        pa.field("landmark_ns", pa.int64(), nullable=False),
        pa.field("horizon_ns", pa.int64(), nullable=False),
        pa.field("stratum", pa.dictionary(pa.int16(), pa.utf8()), nullable=False),
        pa.field("p", pa.float64(), nullable=False),
        pa.field("y_fill", pa.bool_(), nullable=True),
        pa.field("pvalue_nofill", pa.float64(), nullable=False),
        pa.field("pvalue_fill", pa.float64(), nullable=False),
        pa.field("set_nofill", pa.bool_(), nullable=False),
        pa.field("set_fill", pa.bool_(), nullable=False),
        pa.field("va_p0", pa.float64(), nullable=False),
        pa.field("va_p1", pa.float64(), nullable=False),
        pa.field("no_fill_within_ns", pa.int64(), nullable=True),
        pa.field("fill_within_ns", pa.int64(), nullable=True),
        pa.field("interval_no_fill_within_ns", pa.int64(), nullable=True),
        pa.field("interval_fill_within_ns", pa.int64(), nullable=True),
    ]
    meta = {
        b"qt.schema": SETS_SCHEMA_NAME.encode(),
        b"qt.schema_version": b"1",
        b"qt.engine_version": ENGINE_VERSION.encode(),
    }
    return pa.schema(fields, metadata=meta)


def _sets_table(out: FillOutcomes, pred: _Predicted, *, known_only: bool) -> pa.Table:
    """Rows of `pred` x horizons, ordered by row then horizon; `known_only` keeps the rows with a known label."""
    schema = sets_schema()
    m, j_count = pred.va0.shape
    if m == 0:
        return schema.empty_table()
    grid = out.grid_ns
    row = np.repeat(pred.rows, j_count)
    jj = np.tile(np.arange(j_count), m)
    y = out.y[pred.rows].reshape(-1)
    keep = y >= 0 if known_only else np.ones(len(y), dtype=bool)
    row, jj, y = row[keep], jj[keep], y[keep]
    r = np.repeat(np.arange(m), j_count)[keep]

    def bound(ix: np.ndarray) -> pa.Array:
        v = ix[r]
        return pa.array(np.where(v > 0, grid[np.maximum(v - 1, 0)], 0), pa.int64(), mask=v <= 0)

    flat = {k: getattr(pred, k).reshape(-1)[keep] for k in ("va0", "va1")}
    pv = pred.pv.reshape(-1, 2)[keep]
    inset = pred.inset.reshape(-1, 2)[keep]
    arrays = {
        "order_ref": pa.array(out.order_ref[row].astype(str).tolist(), pa.utf8()),
        "landmark_ns": pa.array(out.landmark_ns[row], pa.int64()),
        "horizon_ns": pa.array(grid[jj], pa.int64()),
        "stratum": pa.array(pred.strata.reshape(-1)[keep].astype(str).tolist(), pa.utf8()).dictionary_encode(),
        "p": pa.array(out.p[row, jj], pa.float64()),
        "y_fill": pa.array(y == 1, pa.bool_(), mask=y < 0),
        "pvalue_nofill": pa.array(pv[:, 0], pa.float64()),
        "pvalue_fill": pa.array(pv[:, 1], pa.float64()),
        "set_nofill": pa.array(inset[:, 0]),
        "set_fill": pa.array(inset[:, 1]),
        "va_p0": pa.array(flat["va0"], pa.float64()),
        "va_p1": pa.array(flat["va1"], pa.float64()),
        "no_fill_within_ns": bound(pred.lo1),
        "fill_within_ns": bound(pred.hi1),
        "interval_no_fill_within_ns": bound(pred.lo2),
        "interval_fill_within_ns": bound(pred.hi2),
    }
    return pa.Table.from_arrays([arrays[f.name].cast(f.type) for f in schema], schema=schema)


def _digest(*arrays: np.ndarray) -> str:
    import hashlib

    h = hashlib.sha256()
    for a in arrays:
        b = np.ascontiguousarray(a)
        h.update(str(b.dtype).encode() + str(b.shape).encode())
        h.update(b.tobytes() if b.dtype != object else "\x1f".join(map(str, b.tolist())).encode())
    return h.hexdigest()


def _run_id(out: FillOutcomes, sp: ConformalSpec, calib: np.ndarray, test: np.ndarray) -> str:
    strata = {k: _digest(v) for k, v in sorted(out.strata.items())}
    ident = {
        "conformal": sp.model_dump(mode="json"),
        "source": out.source,
        "model_id": out.model_id,
        "score": out.score,
        # time_ns orders the martingale when shuffle_within_day is false; dates_known changes its groups
        "inputs": _digest(out.grid_ns, out.order_ref, out.landmark_ns, out.date, out.time_ns, out.p, out.y),
        "dates_known": out.dates_known,
        "strata": strata,
        "calib": _digest(calib),
        "test": _digest(test),
    }

    def no_refs(ref: dict[str, Any]) -> str:  # the identity document holds digests, never references
        raise config_error(f"unexpected reference in the conformal identity document: {ref}")

    return run_id(ident, ENGINE_VERSION, no_refs)


# ---- fit-hazard integration (`fit-hazard --conformal-alpha`) --------------------------------------------------

#: Features columns `fit-hazard --conformal-alpha` needs (join keys, the default `dist_bucket` stratum, dates and
#: times for the martingale, outcomes for the label check); checked before the fit.
_CLI_FEATURE_COLUMNS = ("order_ref", "landmark_ns", "dist_ticks", "session_date", "ts_landmark", "event_ns", "cause")


def _features_path(ref: Any) -> Path:
    path = ref.get("path") if isinstance(ref, dict) else getattr(ref, "path", None)
    if path is None:
        raise config_error("fit-hazard --conformal-alpha needs a local features path")
    p = Path(path)
    return p / "features.parquet" if p.is_dir() else p


def write_report(
    report: ConformalReport, sets: pa.Table, out_dir: str | Path, calibrator: ConformalCalibrator | None = None
) -> list[dict[str, Any]]:
    """Writes `conformal_sets.parquet`, `conformal.json` (through a temporary file and a rename) and, when given,
    `conformal_calibrator.parquet` under `out_dir`; returns the `Files` entries."""
    import os

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    entries = [write_table(sets, out / SETS_NAME, sets_schema())]
    if calibrator is not None:
        entries.append(calibrator.save(out / CALIBRATOR_NAME))
    text = json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    p = out / REPORT_NAME
    tmp = out / f".{REPORT_NAME}.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    entries.append({"name": REPORT_NAME, "rows": None, "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    return entries


def conformal_step(
    args: dict[str, Any], alpha: Any
) -> Callable[[dict[str, Any]], tuple[list[dict[str, Any]], list[str]]]:
    """`fit-hazard --conformal-alpha A`: validates `A`, `--out` and the features table's columns before the fit and
    returns the step to run after it (`conformal_after_fit`)."""
    import pyarrow.parquet as pq

    from queuetruth.errors import QtError

    try:
        a = _check_alpha(float(alpha))
    except (TypeError, ValueError):
        raise config_error(f"--conformal-alpha must be a number in (0, 1), got {alpha!r}", field="alpha") from None
    if not args.get("out"):
        raise config_error("fit-hazard --conformal-alpha needs --out")
    path = _features_path(args.get("features"))
    if not path.is_file():
        raise QtError("QT-INPUT-001", f"{path}: not found (fit-hazard --conformal-alpha reads the features table)")
    names = set(pq.read_schema(path).names)
    missing = [c for c in _CLI_FEATURE_COLUMNS if c not in names]
    if missing:
        raise input_error(f"{path}: fit-hazard --conformal-alpha needs features columns {missing}")
    return lambda card: conformal_after_fit(args, card, a)


def conformal_after_fit(
    args: dict[str, Any], card: dict[str, Any], alpha: float
) -> tuple[list[dict[str, Any]], list[str]]:
    """Calibrates `cif_fill_raw` on the calib split and evaluates on the test split of the `fit-hazard` run just
    written to `args["out"]`; writes `conformal.json`, `conformal_sets.parquet` and `conformal_calibrator.parquet`
    there and returns their `Files` entries and the lines the CLI prints (`summary_lines`)."""
    import pyarrow.parquet as pq

    a = _check_alpha(alpha)
    out_dir = args.get("out")
    if not out_dir:
        raise config_error("fit-hazard --conformal-alpha needs --out")
    preds = pq.read_table(Path(out_dir) / "predictions.parquet")
    feats = pq.read_table(_features_path(args.get("features")))
    outcomes = prepare_hazard(preds, feats, score="cif_fill_raw", model_id=str(card.get("model_id", "")))
    spec = ConformalSpec(alpha=a, seed=int(args.get("seed") or 0))
    calib = mask_split(outcomes, "calib")
    report, sets = evaluate(outcomes, calib, mask_split(outcomes, "test"), spec)
    entries = write_report(report, sets, out_dir, ConformalCalibrator.fit(outcomes, calib, spec))
    return entries, summary_lines(report)


def _pooled_cells(report: ConformalReport) -> list[CoverageCell]:
    n_lm = len({c.landmark_ns for c in report.cells if c.landmark_ns is not None})
    return [c for c in report.cells if c.stratum == ALL and (c.landmark_ns is None) == (n_lm > 1)]


def summary_lines(report: ConformalReport) -> list[str]:
    """The pooled coverage at 1 s and 10 s (the first and last horizons when those are not on the grid), then
    `summary_warnings`."""
    pooled = {c.horizon_ns: c for c in _pooled_cells(report)}
    hs = [h for h in (1_000_000_000, 10_000_000_000) if h in pooled] or sorted(pooled)[:: max(1, len(pooled) - 1)]
    cov = ", ".join(f"{_fmt_ns(h)} {pooled[h].coverage:.3f}" for h in hs if pooled[h].coverage is not None)
    head = f"conformal ({REPORT_NAME}): pooled test coverage at alpha {report.alpha:g}: {cov}" if cov else None
    return ([head] if head else []) + summary_warnings(report)


def summary_warnings(report: ConformalReport) -> list[str]:
    """Warning lines of a report: horizons whose pooled test coverage, or coverage among filled orders, is
    significantly below `1 - alpha` (the 95% Clopper-Pearson upper limit is below it), and martingale alarms with
    their level (per cell, and the Bonferroni family-wise verdict)."""
    lines = []
    pooled = _pooled_cells(report)
    nominal = 1.0 - report.alpha
    low = [c for c in pooled if c.coverage_ci_hi is not None and c.coverage_ci_hi < nominal]
    if low:
        cov = ", ".join(f"{_fmt_ns(c.horizon_ns)} {c.coverage:.3f}" for c in low)
        lines.append(f"conformal ({REPORT_NAME}): test coverage below the nominal {nominal:.3f} at {cov}")
    low_fill = []
    for c in pooled:
        if c.coverage_fill is None or c.n_test_fill == 0:
            continue
        hi = clopper_pearson(round(c.coverage_fill * c.n_test_fill), c.n_test_fill)[1]
        if hi is not None and hi < nominal:
            low_fill.append(f"{_fmt_ns(c.horizon_ns)} {c.coverage_fill:.3f}")
    if low_fill:
        hint = "" if report.spec.get("label_conditional") else "; label_conditional sets cover each label"
        lines.append(
            f"conformal ({REPORT_NAME}): coverage among filled orders below the nominal {nominal:.3f} at "
            f"{', '.join(low_fill)}{hint}"
        )
    alarms = [d for d in report.drift if d.alarm]
    if alarms:
        thr = float(report.spec.get("alarm_threshold", 100.0))
        n = len(report.drift)
        if report.drift_familywise_alarm:
            verdict = (
                f"exchangeability of the calibration and test rows is rejected at family-wise level {1 / thr:.3g} "
                "(Bonferroni over the cells), so coverage across them is not guaranteed"
            )
        else:
            verdict = (
                f"no cell reached the family-wise (Bonferroni) threshold {thr * n:g}; per-cell alarms at level "
                f"{1 / thr:.3g} can be false alarms (family-wise up to {min(1.0, n / thr):.3g})"
            )
        lines.append(
            f"conformal ({REPORT_NAME}): exchangeability martingale alarm in {len(alarms)} of {n} horizon x "
            f"landmark cells: {verdict}"
        )
    return lines


def _fmt_ns(ns: int) -> str:
    if ns % 1_000_000_000 == 0:
        return f"{ns // 1_000_000_000}s"
    return f"{ns // 1_000_000}ms" if ns % 1_000_000 == 0 else f"{ns}ns"


__all__ = [
    "ALL",
    "CALIBRATOR_NAME",
    "GUARANTEE",
    "REPORT_NAME",
    "REPORT_SCHEMA",
    "SETS_NAME",
    "BinaryConformal",
    "ConformalCalibrator",
    "ConformalReport",
    "ConformalSpec",
    "CoverageCell",
    "DriftCell",
    "DriftGroup",
    "FillOutcomes",
    "FillTimeCell",
    "FillTimeConformal",
    "GroupDrift",
    "MartingaleResult",
    "StratumName",
    "VennAbers",
    "clopper_pearson",
    "conformal_after_fit",
    "conformal_spec",
    "conformal_step",
    "dist_bucket",
    "evaluate",
    "exchangeability_martingale",
    "fill_interval",
    "jumper_log10",
    "lower_covered",
    "mask_dates",
    "mask_split",
    "min_informative_n",
    "mixture_log10",
    "nonconformity",
    "online_pvalues",
    "predicted_bucket",
    "prepare_hazard",
    "prepare_zoo",
    "random_order_halves",
    "sets_schema",
    "summary_lines",
    "summary_warnings",
    "upper_covered",
    "venn_abers_merge",
    "write_report",
]
