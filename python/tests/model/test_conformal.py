"""`queuetruth.model.conformal`: finite-sample coverage of split and Mondrian conformal fill sets on synthetic
exchangeable data, exact p-value laws, Venn-Abers intervals against brute-force isotonic regression, conformal
fill-time bounds, the exchangeability martingale (Ville bound under exchangeability, alarm under drift), the
table-level evaluation (validity refusals, determinism, schemas), `fit-hazard --conformal-alpha`, and the real-data
coverage table of `docs/concepts/conformal-fill-probability.md` (marker `realdata`)."""

from __future__ import annotations

import dataclasses
import json
import math
import os
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pac
import pyarrow.parquet as pq
import pytest
from scipy.stats import kstest
from sklearn.isotonic import isotonic_regression

from model.hazard_synth import make_features
from queuetruth.errors import QtError
from queuetruth.model import HazardModel
from queuetruth.model import conformal as cf
from queuetruth.schemas.arrow import SCHEMAS

ALPHA = 0.1
NS = 1_000_000_000
FAST = {"max_iter": 30, "min_samples_leaf": 40, "max_leaf_nodes": 15}


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _draw(rng: np.random.Generator, n: int, *, shift: float = 0.0, temp: float = 2.0) -> tuple[np.ndarray, ...]:
    """Exchangeable rows: covariate x, true fill probability, a miscalibrated model probability (overconfident by
    `temp`, biased by -0.7 on the logit) and the label."""
    x = rng.normal(shift, 1.0, n)
    p_true = _sigmoid(1.2 * x - 1.0)
    p_hat = _sigmoid(temp * (1.2 * x - 1.0) - 0.7)
    y = (rng.random(n) < p_true).astype(np.int8)
    return x, p_true, p_hat, y


def _covered(sets: np.ndarray, y: np.ndarray) -> np.ndarray:
    return sets[np.arange(len(y)), y]


# ---- split conformal: coverage and p-value laws ---------------------------------------------------------------


def test_split_coverage_hits_nominal_finite_sample_bounds() -> None:
    """Average coverage over 400 calibration draws lies in [1 - alpha, 1 - alpha + 1 / (n + 1)] (continuous scores,
    no ties) up to Monte Carlo error (sd ~0.0015)."""
    rng = np.random.default_rng(1)
    n_cal, n_test, reps = 200, 200, 400
    cov = []
    for _ in range(reps):
        _, _, pc, yc = _draw(rng, n_cal)
        _, _, pt, yt = _draw(rng, n_test)
        sets = cf.BinaryConformal().fit(pc, yc).predict_sets(pt, ALPHA)
        cov.append(_covered(sets, yt).mean())
    mean = float(np.mean(cov))
    assert 1 - ALPHA - 0.006 <= mean <= 1 - ALPHA + 1 / (n_cal + 1) + 0.006, mean
    # validity does not depend on the model: an anti-calibrated model (1 - p_true) still gets nominal coverage
    # (with larger sets), while its own "90% sets" {y : p_hat(y) >= 0.1} do not
    anti, naive = [], []
    for _ in range(reps):
        _, ptc, _, yc = _draw(rng, n_cal)
        _, ptt, _, yt = _draw(rng, n_test)
        sets = cf.BinaryConformal().fit(1 - ptc, yc).predict_sets(1 - ptt, ALPHA)
        anti.append(_covered(sets, yt).mean())
        naive.append(_covered(np.column_stack([ptt >= ALPHA, 1 - ptt >= ALPHA]), yt).mean())
    assert 1 - ALPHA - 0.006 <= np.mean(anti) <= 1 - ALPHA + 1 / (n_cal + 1) + 0.006
    assert np.mean(naive) < 1 - ALPHA - 0.05


def test_smoothed_pvalues_are_exactly_uniform_and_coverage_is_exact() -> None:
    """One test row per calibration draw: the smoothed true-label p-values are i.i.d. U(0, 1), so coverage is
    exactly 1 - alpha; conservative p-values are super-uniform."""
    rng = np.random.default_rng(2)
    reps, n_cal = 3000, 40
    smooth, cons = [], []
    for _ in range(reps):
        _, _, pc, yc = _draw(rng, n_cal)
        _, _, pt, yt = _draw(rng, 1)
        bc = cf.BinaryConformal().fit(pc, yc)
        theta = 1.0 - rng.random(1)
        smooth.append(bc.pvalues(pt, theta=theta)[0, yt[0]])
        cons.append(bc.pvalues(pt)[0, yt[0]])
    smooth, cons = np.array(smooth), np.array(cons)
    assert kstest(smooth, "uniform").pvalue > 0.01
    assert abs((smooth > ALPHA).mean() - (1 - ALPHA)) < 0.017  # 3 sd of a Bernoulli(0.9) mean over 3000
    for a in (0.05, 0.1, 0.25, 0.5):
        assert (cons <= a).mean() <= a + 0.017, a
    assert np.all(cons >= smooth - 1e-12)  # conservative >= smoothed, row by row


def test_pvalues_hand_computed_with_ties() -> None:
    # calibration scores (1 - p_hat(y)): y=1 rows give 1 - p, y=0 rows give p (binary fractions: exact ties)
    p = np.array([0.25, 0.25, 0.5, 0.875, 0.375])
    y = np.array([0, 0, 1, 1, 0])  # scores 0.25, 0.25, 0.5, 0.125, 0.375
    bc = cf.BinaryConformal().fit(p, y)
    pv = bc.pvalues(np.array([0.75]))  # candidate 0 -> score 0.75; candidate 1 -> score 0.25
    assert pv[0, 0] == pytest.approx(1 / 6)  # #{s >= 0.75} = 0: (0 + 1) / 6
    assert pv[0, 1] == pytest.approx(5 / 6)  # #{s >= 0.25} = 4: (4 + 1) / 6
    sm = bc.pvalues(np.array([0.75]), theta=np.array([0.5]))
    assert sm[0, 1] == pytest.approx((2 + 0.5 * 3) / 6)  # #{s > 0.25} = 2, ties 2 (+ itself)
    assert sm[0, 0] == pytest.approx((0 + 0.5 * 1) / 6)
    assert bc.predict_sets(np.array([0.75]), 0.2).tolist() == [[False, True]]
    assert bc.predict_sets(np.array([0.75]), 0.15).tolist() == [[True, True]]


def test_min_informative_n_and_tiny_calibration() -> None:
    assert cf.min_informative_n(0.1) == 9
    assert cf.min_informative_n(0.05) == 19
    assert cf.min_informative_n(0.5) == 1
    test = np.array([0.0625, 0.9375])
    sets = cf.BinaryConformal().fit(np.full(8, 0.0625), np.zeros(8, dtype=int)).predict_sets(test, 0.1)
    assert sets.all()  # 8 < 9 rows: the smallest p-value is 1 / 9 > 0.1, every set holds both labels
    sets = cf.BinaryConformal().fit(np.full(9, 0.0625), np.zeros(9, dtype=int)).predict_sets(test, 0.1)
    # 9 rows: a label scored above every calibration score has p = 1 / 10 and is excluded (a fill at p = 0.0625,
    # no fill at p = 0.9375); the other label ties every calibration score and has p = 1
    assert sets.tolist() == [[True, False], [False, True]]


def test_mondrian_gives_per_stratum_coverage_where_marginal_fails() -> None:
    """Two strata with opposite miscalibration: marginal split conformal under-covers one of them, Mondrian covers
    both at the nominal level (averaged over calibration draws)."""
    rng = np.random.default_rng(3)

    def draw(n: int) -> tuple[np.ndarray, ...]:
        g = rng.integers(0, 2, n)
        x = rng.normal(0, 1, n)
        p_true = np.where(g == 0, _sigmoid(x - 2.0), _sigmoid(x + 0.5))
        p_hat = np.where(g == 0, _sigmoid(x - 0.5), _sigmoid(x - 1.0))  # stratum 0 overstated, 1 understated
        y = (rng.random(n) < p_true).astype(np.int8)
        return np.where(g == 0, "a", "b"), p_hat, y

    marg = {"a": [], "b": []}
    mond = {"a": [], "b": []}
    for _ in range(150):
        gc, pc, yc = draw(400)
        gt, pt, yt = draw(400)
        sm = cf.BinaryConformal().fit(pc, yc).predict_sets(pt, ALPHA)
        sg = cf.BinaryConformal().fit(pc, yc, gc).predict_sets(pt, ALPHA, gt)
        for name in ("a", "b"):
            m = gt == name
            marg[name].append(_covered(sm[m], yt[m]).mean())
            mond[name].append(_covered(sg[m], yt[m]).mean())
    for name in ("a", "b"):
        assert np.mean(mond[name]) >= 1 - ALPHA - 0.008, (name, np.mean(mond[name]))
    assert min(np.mean(marg["a"]), np.mean(marg["b"])) < 1 - ALPHA - 0.03


def test_label_conditional_coverage_for_rare_fills() -> None:
    rng = np.random.default_rng(4)
    cov_marg, cov_lc = [], []
    for _ in range(150):
        _, _, pc, yc = _draw(rng, 600, shift=-1.0)
        _, _, pt, yt = _draw(rng, 600, shift=-1.0)
        filled = yt == 1
        sm = cf.BinaryConformal().fit(pc, yc).predict_sets(pt, ALPHA)
        sl = cf.BinaryConformal(label_conditional=True).fit(pc, yc).predict_sets(pt, ALPHA)
        cov_marg.append(_covered(sm[filled], yt[filled]).mean())
        cov_lc.append(_covered(sl[filled], yt[filled]).mean())
    assert np.mean(cov_lc) >= 1 - ALPHA - 0.01
    assert np.mean(cov_marg) < np.mean(cov_lc) - 0.05


def test_binary_conformal_input_errors() -> None:
    bc = cf.BinaryConformal()
    with pytest.raises(QtError, match="not fitted"):
        bc.pvalues(np.array([0.5]))
    for bad in (0.0, 1.0, float("nan"), -0.1):
        with pytest.raises(QtError) as e:
            cf.BinaryConformal().fit([0.5], [1]).predict_sets([0.5], bad)
        assert e.value.code == "QT-CONFIG-001"
    for p, y in (([0.5, 1.2], [0, 1]), ([0.5, float("nan")], [0, 1]), ([0.5, 0.5], [0, 2]), ([0.5], [0, 1])):
        with pytest.raises(QtError) as e:
            cf.BinaryConformal().fit(p, y)
        assert e.value.code == "QT-INPUT-002"
    fitted = cf.BinaryConformal().fit([0.5, 0.4], [0, 1])
    with pytest.raises(QtError, match="theta"):
        fitted.pvalues([0.5], theta=np.array([0.0]))
    with pytest.raises(QtError, match="strata"):
        fitted.pvalues([0.5], strata=["a", "b"])
    # a stratum without calibration rows: conservative p = 1, the set holds both labels
    assert cf.BinaryConformal().fit([0.5], [1], ["a"]).predict_sets([0.9], 0.1, ["b"]).all()


# ---- Venn-Abers -----------------------------------------------------------------------------------------------


def _va_brute(pc: np.ndarray, yc: np.ndarray, v: float, y_new: int) -> float:
    x = np.append(pc, v)
    yy = np.append(yc, y_new).astype(np.float64)
    u, inv = np.unique(x, return_inverse=True)
    w = np.bincount(inv).astype(np.float64)
    sy = np.bincount(inv, weights=yy)
    return float(isotonic_regression(sy / w, sample_weight=w)[np.searchsorted(u, v)])


def test_venn_abers_matches_brute_force_isotonic() -> None:
    rng = np.random.default_rng(5)
    for _ in range(200):
        n = int(rng.integers(1, 40))
        pc = np.round(rng.random(n), int(rng.integers(1, 3)))  # ties
        yc = (rng.random(n) < pc).astype(np.int8)
        pt = np.concatenate([np.round(rng.random(15), 1), pc[:3], [0.0, 1.0]])
        p0, p1 = cf.VennAbers().fit(pc, yc).predict(pt)
        for i, v in enumerate(pt):
            assert p0[i] == pytest.approx(_va_brute(pc, yc, v, 0), abs=1e-12)
            assert p1[i] == pytest.approx(_va_brute(pc, yc, v, 1), abs=1e-12)
        assert np.all(p0 <= p1 + 1e-12)


def test_venn_abers_validity_on_exchangeable_data() -> None:
    """With one test row per calibration draw, the probability computed with the true label is calibrated:
    `E[p_Y] = P(Y = 1)`, so `E[p0] <= P(Y = 1) <= E[p1]`; the miscalibrated input is not."""
    rng = np.random.default_rng(6)
    p_true_label, ys, p0s, p1s, raw = [], [], [], [], []
    for _ in range(3000):
        _, _, pc, yc = _draw(rng, 60)
        _, _, pt, yt = _draw(rng, 1)
        p0, p1 = cf.VennAbers().fit(pc, yc).predict(pt)
        p_true_label.append(p1[0] if yt[0] == 1 else p0[0])
        ys.append(yt[0])
        p0s.append(p0[0])
        p1s.append(p1[0])
        raw.append(pt[0])
    ys = np.array(ys, dtype=float)
    se = math.sqrt(ys.mean() * (1 - ys.mean()) / len(ys))
    assert abs(np.mean(p_true_label) - ys.mean()) < 3 * se
    assert np.mean(p0s) <= ys.mean() + 3 * se and ys.mean() - 3 * se <= np.mean(p1s)
    assert abs(np.mean(raw) - ys.mean()) > 5 * se  # the input probabilities are biased
    merged = cf.venn_abers_merge(np.array(p0s), np.array(p1s))
    assert np.all((merged >= np.array(p0s) - 1e-12) & (merged <= np.array(p1s) + 1e-12))


def test_venn_abers_empty_stratum_and_unfitted() -> None:
    with pytest.raises(QtError, match="not fitted"):
        cf.VennAbers().predict([0.5])
    p0, p1 = cf.VennAbers().fit([0.2, 0.8], [0, 1], ["a", "a"]).predict([0.5, 0.5], ["a", "b"])
    assert p0[1] == 0.0 and p1[1] == 1.0
    assert 0.0 <= p0[0] <= p1[0] <= 1.0


# ---- fill-time bounds -----------------------------------------------------------------------------------------


GRID = np.array([1, 2, 5, 10, 30]) * NS


def _draw_times(rng: np.random.Generator, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-interval fill hazards from a random rate; the model's CIF uses a misspecified rate. Returns (model CIF,
    first-fill interval K in 1..J+1)."""
    j = len(GRID)
    rate = np.exp(rng.normal(-0.5, 1.0, n))
    width = np.diff(np.concatenate([[0], GRID])) / NS
    h = 1.0 - np.exp(-rate[:, None] * width[None, :] * 0.4)  # true per-interval fill probability
    u = rng.random((n, j))
    fills = u < h
    k = np.where(fills.any(axis=1), fills.argmax(axis=1) + 1, j + 1)
    model_rate = rate * 0.2  # the model understates the fill rate: its own early quantiles come too late
    cif = 1.0 - np.exp(-model_rate[:, None] * (GRID / NS)[None, :] * 0.4)
    return cif, k


def test_fill_time_bounds_coverage() -> None:
    rng = np.random.default_rng(7)
    lo_cov, hi_cov, both_cov, lo_claims, hi_claims = [], [], [], [], []
    for _ in range(200):
        cc, kc = _draw_times(rng, 300)
        ct, kt = _draw_times(rng, 300)
        ft = cf.FillTimeConformal(GRID).fit(cc, kc)
        lo, hi = ft.lower(ct, ALPHA), ft.upper(ct, ALPHA)
        lo2, hi2 = ft.interval(ct, ALPHA)
        lo_cov.append(cf.lower_covered(kt, lo).mean())
        hi_cov.append(cf.upper_covered(kt, hi).mean())
        both_cov.append((cf.lower_covered(kt, lo2) & cf.upper_covered(kt, hi2)).mean())
        lo_claims.append((lo > 0).mean())
        hi_claims.append((hi > 0).mean())
    assert np.mean(lo_cov) >= 1 - ALPHA - 0.006
    assert np.mean(hi_cov) >= 1 - ALPHA - 0.006
    assert np.mean(both_cov) >= 1 - ALPHA - 0.006
    assert np.mean(lo_claims) > 0.5 and np.mean(hi_claims) > 0.05  # informative bounds, not vacuous ones
    # the model's own 10% quantile of the fill time (no conformal step) under-covers: its rate is too high
    cc, kc = _draw_times(rng, 50_000)
    naive_lo = np.where((cc >= ALPHA).any(axis=1), (cc >= ALPHA).argmax(axis=1), len(GRID))
    assert cf.lower_covered(kc, naive_lo).mean() < 1 - ALPHA - 0.02


def test_fill_time_hand_example_and_edges() -> None:
    grid = np.array([1, 2, 3]) * NS
    cif_c = np.array([[0.1, 0.5, 0.9]] * 9)
    k_c = np.array([1, 2, 2, 3, 3, 3, 4, 4, 4])
    ft = cf.FillTimeConformal(grid).fit(cif_c, k_c)
    # lower scores F(tau_K): 0.1, 0.5, 0.5, 0.9 x3, inf x3; alpha = 0.2 -> k = floor(0.2 * 10) = 2 -> t = 0.5
    assert ft.lower(np.array([[0.1, 0.5, 0.9], [0.6, 0.7, 0.8], [0.0, 0.0, 0.0]]), 0.2).tolist() == [1, 0, 3]
    # upper scores F(tau_{K-1}): 0, 0.1, 0.1, 0.5 x3, inf x3; alpha = 0.5 -> k = ceil(0.5 * 10) = 5 -> t = 0.5
    assert ft.upper(np.array([[0.1, 0.5, 0.9], [0.6, 0.7, 0.8]]), 0.5).tolist() == [3, 1]
    # alpha = 0.2 -> k = 8 -> t = inf: no upper claim
    assert ft.upper(np.array([[0.1, 0.5, 0.9]]), 0.2).tolist() == [-1]
    # too few rows for any lower claim: floor(0.05 * 10) = 0
    assert ft.lower(np.array([[0.9, 0.9, 0.9]]), 0.05).tolist() == [0]
    # every calibration row without a fill: the lower threshold is inf -> "no fill within tau_J"
    ft2 = cf.FillTimeConformal(grid).fit(np.array([[0.1, 0.2, 0.3]] * 19), np.full(19, 4))
    assert ft2.lower(np.array([[0.9, 0.95, 1.0]]), 0.1).tolist() == [3]
    assert cf.lower_covered(np.array([4, 1]), np.array([3, 0])).tolist() == [True, True]
    assert cf.upper_covered(np.array([4, 2, 3]), np.array([-1, 2, 2])).tolist() == [True, True, False]
    with pytest.raises(QtError, match="nondecreasing"):
        ft.lower(np.array([[0.5, 0.4, 0.9]]), 0.1)
    with pytest.raises(QtError, match="fill interval"):
        cf.FillTimeConformal(grid).fit(cif_c, np.full(9, 5))
    with pytest.raises(QtError, match="shape"):
        ft.lower(np.array([[0.1, 0.2]]), 0.1)
    with pytest.raises(QtError, match="not fitted"):
        cf.FillTimeConformal(grid).lower(cif_c, 0.1)


def test_fill_interval_from_labels() -> None:
    y = np.array([[0, 0, 1], [1, 1, 1], [0, 0, 0], [0, -1, -1]])
    k, observed = cf.fill_interval(y)
    assert k.tolist() == [3, 1, 4, 4]
    assert observed.tolist() == [True, True, True, False]


# ---- exchangeability martingale -------------------------------------------------------------------------------


def _pvalues_brute(s: np.ndarray, theta: np.ndarray) -> np.ndarray:
    out = []
    for n in range(len(s)):
        past = s[:n]
        out.append(((past > s[n]).sum() + theta[n] * ((past == s[n]).sum() + 1)) / (n + 1))
    return np.array(out)


def test_online_pvalues_match_brute_force_with_ties() -> None:
    rng = np.random.default_rng(8)
    s = np.round(rng.random(300), 1)
    th = 1.0 - rng.random(300)
    assert np.allclose(cf.online_pvalues(s, th), _pvalues_brute(s, th))
    assert cf.online_pvalues(np.empty(0), np.empty(0)).size == 0
    with pytest.raises(QtError):
        cf.online_pvalues(np.array([np.inf]), np.array([0.5]))


def test_martingale_closed_forms() -> None:
    p = np.full(50, 0.25)
    assert np.allclose(cf.jumper_log10(p, rate=1.0), 0.0)  # rate 1: the bets average to 1 at every step
    assert np.allclose(cf.jumper_log10(np.full(10, 0.5)), 0.0)  # p = 1/2: every betting function is 1
    # mixture of power martingales equals its definition on the eps grid
    q = np.array([0.3, 0.05, 0.9, 0.2])
    eps = (np.arange(cf.N_EPS) + 0.5) / cf.N_EPS
    want = [np.log10(np.mean(np.prod(eps[:, None] * q[None, : i + 1] ** (eps[:, None] - 1), axis=1))) for i in range(4)]
    assert np.allclose(cf.mixture_log10(q), want)
    # streamed in chunks: the running log-product gives the same path as one pass
    r = np.random.default_rng(0).random(1000) * 0.999 + 0.001
    assert np.allclose(cf.mixture_log10(r, chunk=7), cf.mixture_log10(r), rtol=0, atol=1e-9)
    with pytest.raises(QtError):
        cf.jumper_log10(p, rate=0.0)
    with pytest.raises(QtError):
        cf.mixture_log10(np.array([0.0]))


def test_martingale_false_alarm_rate_under_exchangeability() -> None:
    """Ville: P(sup M >= 20) <= 1/20 under exchangeability, for both martingales: over 400 runs each, at most
    400 / 20 = 20 alarms in expectation; the assertion allows 3 binomial sd of Monte Carlo error (33)."""
    for method in ("jumper", "mixture"):
        alarms = 0
        for r in range(400):
            rng = np.random.default_rng(100 + r)
            s = np.round(rng.random(400), 2)  # ties
            days = np.repeat([18000, 18001], 200)
            res = cf.exchangeability_martingale(s, days, method=method, threshold=20.0, seed=r)
            alarms += res.alarm
        assert alarms <= 20 + 3 * math.sqrt(400 * 0.05 * 0.95), (method, alarms)


def test_martingale_alarms_on_day_drift() -> None:
    rng = np.random.default_rng(9)
    s = np.concatenate([rng.random(1500), rng.random(1500) ** 0.5])  # day 2 scores larger (worse calibrated)
    days = np.repeat([18300, 18301], 1500)
    for method in ("jumper", "mixture"):
        res = cf.exchangeability_martingale(s, days, method=method, threshold=100.0, seed=0)
        assert res.alarm and res.alarm_group == "18301", method
        assert res.log10_max > 2
        g1, g2 = res.groups
        assert (g1.group, g1.n, g2.first_index) == ("18300", 1500, 1500)
        assert g2.mean_pvalue < 0.45 < g1.mean_pvalue
        assert res.order[:1500].max() < 1500  # day 1 first
    placebo = cf.exchangeability_martingale(rng.random(3000), days, threshold=100.0, seed=0)
    assert not placebo.alarm


def test_martingale_ordering_and_errors() -> None:
    s = np.arange(6, dtype=float)
    days = np.array([10, 9, 10, 9, 10, 9])
    t = np.array([5, 4, 3, 2, 1, 0])
    res = cf.exchangeability_martingale(s, days, shuffle_within_group=False, time_ns=t)
    assert res.order.tolist() == [5, 3, 1, 4, 2, 0]  # day 9 then day 10, each by time
    days_mixed_width = np.array([9, 10, 9, 10, 9, 10])  # numeric order, not "10" < "9"
    res = cf.exchangeability_martingale(s, days_mixed_width, shuffle_within_group=False, time_ns=t)
    assert [g.group for g in res.groups] == ["9", "10"]
    a = cf.exchangeability_martingale(s, days, seed=3)
    b = cf.exchangeability_martingale(s, days, seed=3)
    assert np.array_equal(a.order, b.order) and np.array_equal(a.log10_path, b.log10_path)
    with pytest.raises(QtError, match="time_ns"):
        cf.exchangeability_martingale(s, days, shuffle_within_group=False)
    with pytest.raises(QtError, match="threshold"):
        cf.exchangeability_martingale(s, days, threshold=1.0)
    with pytest.raises(QtError, match="jumper or mixture"):
        cf.exchangeability_martingale(s, days, method="cusum")  # type: ignore[arg-type]
    with pytest.raises(QtError, match="groups"):
        cf.exchangeability_martingale(s, days[:3])
    empty = cf.exchangeability_martingale(np.empty(0))
    assert not empty.alarm and empty.log10_max == 0.0


# ---- table level: hazard predictions --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hazard_run() -> tuple[pa.Table, pa.Table, HazardModel]:
    features = make_features(21, orders_per_day=300)
    model = HazardModel({"features": {"path": "features.parquet"}, "gbdt": FAST}).fit(features)
    assert model.split_features is not None
    return features, model.predict(model.split_features), model


def test_prepare_hazard_pivots_and_checks_labels(hazard_run) -> None:
    features, preds, model = hazard_run
    out = cf.prepare_hazard(preds, features)
    assert out.n == preds.num_rows // len(model.grid_ns)
    assert np.array_equal(out.grid_ns, model.grid_ns)
    assert set(out.split.tolist()) == {"train", "calib", "test"}
    assert {"dist_bucket", "side", "instrument"} <= set(out.strata)
    assert "na" not in set(out.strata["dist_bucket"].tolist())
    assert np.all(np.diff(out.p, axis=1) >= -1e-15)
    # censoring before the horizon is unknown (-1), never a 0 label
    assert sum(out.excluded_unobserved.values()) == int((out.y < 0).sum()) > 0
    tampered = preds.set_column(
        preds.schema.get_field_index("y_fill"),
        "y_fill",
        pa.array([not v if v is not None else None for v in preds.column("y_fill").to_pylist()], pa.bool_()),
    )
    with pytest.raises(QtError, match="disagrees"):
        cf.prepare_hazard(tampered, features)
    with pytest.raises(QtError, match="exactly one value per horizon"):
        cf.prepare_hazard(preds.slice(1), features)
    with pytest.raises(QtError, match="no features row"):
        cf.prepare_hazard(preds, features.slice(0, 10))
    with pytest.raises(QtError, match="score"):
        cf.prepare_hazard(preds, features, score="survival")


def test_evaluate_hazard_report_and_sets(hazard_run) -> None:
    features, preds, model = hazard_run
    out = cf.prepare_hazard(preds, features)
    calib, test = cf.mask_split(out, "calib"), cf.mask_split(out, "test")
    report, sets = cf.evaluate(out, calib, test, {"alpha": 0.2, "strata": ["dist_bucket", "side"]})
    doc = json.loads(json.dumps(report.model_dump(mode="json")))
    assert cf.ConformalReport.model_validate(doc) == report
    assert doc["schema"] == "qt.conformal_report" and doc["schema_version"] == 1
    assert report.n_calib_rows == int(calib.sum()) and report.n_test_rows == int(test.sum())
    assert report.calib_dates != report.test_dates
    landmarks = sorted(set(out.landmark_ns.tolist()))
    pooled = [c for c in report.cells if c.landmark_ns is None]
    assert len(pooled) == len(model.grid_ns) and all(c.stratum == cf.ALL for c in pooled)
    for c in report.cells:
        assert c.frac_empty + c.frac_singleton + c.frac_both == pytest.approx(1.0)
        assert c.coverage_ci_lo <= c.coverage <= c.coverage_ci_hi
        assert c.va_p0_mean <= c.va_true_label_mean <= c.va_p1_mean + 1e-12
        if c.coverage_fill is not None and c.coverage_nofill is not None:
            mix = c.fill_rate_test * c.coverage_fill + (1 - c.fill_rate_test) * c.coverage_nofill
            assert mix == pytest.approx(c.coverage)
    assert {d.landmark_ns for d in report.drift} == set(landmarks)
    assert len(report.drift) == len(landmarks) * len(model.grid_ns)
    for d in report.drift:  # the calibration rows by date, then the test rows by date
        assert [g.role for g in d.groups] == ["calib"] * len(report.calib_dates) + ["test"] * len(report.test_dates)
        assert [g.date for g in d.groups] == report.calib_dates + report.test_dates
        assert d.groups[0].group == f"calib:{report.calib_dates[0]}"
    assert report.drift_familywise_threshold == pytest.approx(100.0 * len(report.drift))
    keys = [(c.horizon_ns, c.landmark_ns, c.stratum) for c in report.cells]
    assert len(keys) == len(set(keys))
    # the sets table: one row per known test row x horizon, consistent with the p-values
    assert sets.schema.equals(cf.sets_schema())
    assert sets.schema.metadata[b"qt.schema"] == b"qt.conformal_sets"
    known = int(sum((out.y[test, j] >= 0).sum() for j in range(len(model.grid_ns))))
    assert sets.num_rows == known
    sn = sets.column("set_nofill").to_numpy(zero_copy_only=False)
    pn = sets.column("pvalue_nofill").to_numpy()
    assert np.array_equal(sn, pn > 0.2)
    lo = sets.column("no_fill_within_ns").to_pylist()
    assert all(v is None or v in set(model.grid_ns.tolist()) for v in lo)
    # pooled coverage reported = coverage recomputed from the sets table
    y = sets.column("y_fill").to_numpy(zero_copy_only=False)
    covered = np.where(y, sets.column("set_fill").to_numpy(zero_copy_only=False), sn)
    h = sets.column("horizon_ns").to_numpy()
    for c in pooled:
        assert c.coverage == pytest.approx(covered[h == c.horizon_ns].mean())


def test_evaluate_is_deterministic_and_run_id_tracks_inputs(hazard_run, tmp_path: Path) -> None:
    features, preds, _ = hazard_run
    out = cf.prepare_hazard(preds, features)
    calib, test = cf.mask_split(out, "calib"), cf.mask_split(out, "test")
    spec = {"alpha": 0.1, "smoothed": True, "seed": 4}
    r1, s1 = cf.evaluate(out, calib, test, spec)
    r2, s2 = cf.evaluate(out, calib, test, spec)
    assert r1.model_dump_json() == r2.model_dump_json() and s1.equals(s2)
    files1 = cf.write_report(r1, s1, tmp_path / "a")
    files2 = cf.write_report(r2, s2, tmp_path / "b")
    assert [f["sha256"] for f in files1] == [f["sha256"] for f in files2]
    assert cf.evaluate(out, calib, test, {**spec, "seed": 5})[0].run_id != r1.run_id
    assert cf.evaluate(out, calib, test, {**spec, "alpha": 0.2})[0].run_id != r1.run_id
    assert cf.evaluate(out, test, calib, spec)[0].run_id != r1.run_id
    # with shuffle_within_day false the martingale runs in time order: time_ns is part of the identity
    timed = {**spec, "shuffle_within_day": False}
    r_t, _ = cf.evaluate(out, calib, test, timed)
    rev = dataclasses.replace(out, time_ns=out.time_ns[::-1].copy())
    r_rev, _ = cf.evaluate(rev, calib, test, timed)
    assert r_rev.run_id != r_t.run_id
    assert pq.read_table(tmp_path / "a" / cf.SETS_NAME).equals(s1)


def test_evaluate_refuses_invalid_calibration(hazard_run) -> None:
    features, preds, _ = hazard_run
    out = cf.prepare_hazard(preds, features)
    calib, test, train = (cf.mask_split(out, s) for s in ("calib", "test", "train"))
    with pytest.raises(QtError, match="calibration rows include the hazard's train split") as e:
        cf.evaluate(out, train, test)
    assert e.value.code == "QT-CONFIG-001"
    # the test rows must not be fitted on either: in-sample test rows would report in-sample coverage
    with pytest.raises(QtError, match="test rows include the hazard's train split") as e:
        cf.evaluate(out, calib, train)
    assert e.value.code == "QT-CONFIG-001"
    iso = cf.prepare_hazard(preds, features, score="cif_fill")
    with pytest.raises(QtError, match="calibration rows from the hazard's calib split"):
        cf.evaluate(iso, calib, test)
    with pytest.raises(QtError, match="test rows from the hazard's calib split") as e:
        cf.evaluate(iso, test, calib)
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError, match="train split"):
        cf.ConformalCalibrator.fit(out, train)
    a, b = cf.random_order_halves(iso, test, seed=1)  # cif_fill on rows the isotonic layer never saw: allowed
    report, _ = cf.evaluate(iso, a, b)
    assert report.score == "cif_fill"
    with pytest.raises(QtError, match="overlap"):
        cf.evaluate(out, test, test)
    with pytest.raises(QtError, match="no calibration rows"):
        cf.evaluate(out, np.zeros(out.n, dtype=bool), test)
    with pytest.raises(QtError, match="no test rows"):
        cf.evaluate(out, calib, np.zeros(out.n, dtype=bool))
    with pytest.raises(QtError):
        cf.evaluate(out, calib, test, {"alpha": 1.5})
    with pytest.raises(QtError, match="not available"):
        cf.evaluate(cf.prepare_hazard(preds, None), calib, test, {"strata": ["dist_bucket"]})


def test_random_order_halves_keep_orders_together(hazard_run) -> None:
    features, preds, _ = hazard_run
    out = cf.prepare_hazard(preds, features)
    test = cf.mask_split(out, "test")
    a, b = cf.random_order_halves(out, test, seed=2)
    assert not np.any(a & b) and np.array_equal(a | b, test)
    assert not set(out.order_ref[a].tolist()) & set(out.order_ref[b].tolist())
    a2, _ = cf.random_order_halves(out, test, seed=2)
    assert np.array_equal(a, a2)


def test_within_day_random_halves_cover_at_nominal(hazard_run) -> None:
    """A random order-level split of one day is exchangeable by construction: pooled coverage is >= 1 - alpha up
    to sampling error, at every horizon."""
    features, preds, _ = hazard_run
    out = cf.prepare_hazard(preds, features)
    test = cf.mask_split(out, "test")
    covs = []
    for seed in range(8):
        a, b = cf.random_order_halves(out, test, seed=seed)
        report, _ = cf.evaluate(out, a, b, {"alpha": ALPHA, "strata": [], "seed": seed})
        covs.append([c.coverage for c in report.cells if c.landmark_ns is None])
    assert np.mean(covs) >= 1 - ALPHA - 0.01


def _outcomes(p: np.ndarray, y: np.ndarray, date: np.ndarray, *, dates_known: bool = True) -> cf.FillOutcomes:
    """One-horizon (1 s), one-landmark outcomes built directly."""
    n = len(p)
    return cf.FillOutcomes(
        source="zoo",
        model_id="m",
        score="p_fill",
        grid_ns=np.array([NS]),
        order_ref=np.array([f"o{i}" for i in range(n)], dtype=object),
        landmark_ns=np.zeros(n, dtype=np.int64),
        date=date.astype(np.int64),
        time_ns=np.arange(n, dtype=np.int64),
        split=None,
        p=p[:, None],
        y=y[:, None].astype(np.int8),
        strata={},
        dates_known=dates_known,
    )


def _shifted(rng: np.random.Generator, n: int, rate: float) -> tuple[np.ndarray, np.ndarray]:
    """Model probability `p` and labels with fill probability `rate x p` (calibrated when `rate = 1`)."""
    p = _sigmoid(rng.normal(-2.5, 1.0, n))
    return p, (rng.random(n) < np.minimum(rate * p, 1.0)).astype(np.int8)


def test_martingale_sees_shift_between_calibration_and_test_rows_on_one_date() -> None:
    """Regression: calibration and test rows of one session date with a 4x fill-rate shift between them. The
    martingale runs over the calibration rows, then the test rows, so it alarms whether they share a date, sit on
    two dates or have no date at all; without a shift it stays quiet."""
    rng = np.random.default_rng(12)
    pc, yc = _shifted(rng, 2000, 1.0)
    pt, yt = _shifted(rng, 2000, 4.0)
    p, y = np.concatenate([pc, pt]), np.concatenate([yc, yt])
    calib = np.arange(4000) < 2000
    spec = {"alpha": ALPHA, "strata": []}
    one_day = np.full(4000, 18300)
    for date, known in ((one_day, True), (np.where(calib, 18300, 18301), True), (one_day, False)):
        report, _ = cf.evaluate(_outcomes(p, y, date, dates_known=known), calib, ~calib, spec)
        (d,) = report.drift
        assert d.alarm and d.alarm_group is not None and d.alarm_group.startswith("test"), (date[-1], known)
        assert [g.role for g in d.groups] == ["calib", "test"]
        assert report.cells[0].coverage < 1 - ALPHA - 0.05
        if not known:
            assert [g.group for g in d.groups] == ["calib", "test"] and d.groups[0].date is None
            assert report.calib_dates == [] and report.test_dates == []
    assert report.drift_familywise_alarm  # one cell: Bonferroni threshold = the per-cell threshold
    p2, y2 = _shifted(rng, 4000, 1.0)
    quiet, _ = cf.evaluate(_outcomes(p2, y2, one_day), calib, ~calib, spec)
    assert not quiet.drift[0].alarm and not quiet.drift_familywise_alarm
    mix, _ = cf.evaluate(_outcomes(p, y, one_day), calib, ~calib, {**spec, "martingale": "mixture"})
    assert mix.drift[0].method == "mixture" and mix.drift[0].alarm


def test_summary_lines_and_warnings() -> None:
    rng = np.random.default_rng(13)
    pc, yc = _shifted(rng, 2000, 1.0)
    pt, yt = _shifted(rng, 2000, 4.0)
    out = _outcomes(np.concatenate([pc, pt]), np.concatenate([yc, yt]), np.full(4000, 18300))
    calib = np.arange(4000) < 2000
    report, _ = cf.evaluate(out, calib, ~calib, {"alpha": ALPHA, "strata": []})
    assert len(report.cells) == 1  # no strata: one `all` cell per horizon x landmark, never a duplicate
    lines = cf.summary_lines(report)
    cov = report.cells[0].coverage
    assert lines[0] == f"conformal (conformal.json): pooled test coverage at alpha 0.1: 1s {cov:.3f}"
    assert lines[1] == f"conformal (conformal.json): test coverage below the nominal 0.900 at 1s {cov:.3f}"
    fill = report.cells[0].coverage_fill
    assert lines[2] == (
        f"conformal (conformal.json): coverage among filled orders below the nominal 0.900 at 1s {fill:.3f}; "
        "label_conditional sets cover each label"
    )
    assert "alarm in 1 of 1 horizon x landmark cells" in lines[3] and "family-wise level 0.01" in lines[3]
    assert cf.summary_warnings(report) == lines[1:]
    # a per-cell alarm that misses the Bonferroni threshold is reported as possibly false
    two = report.model_copy(update={"drift": [*report.drift, report.drift[0]], "drift_familywise_alarm": False})
    assert "can be false alarms (family-wise up to 0.02)" in cf.summary_warnings(two)[-1]
    good = report.model_copy(update={"cells": [], "drift": [], "drift_familywise_alarm": False})
    assert cf.summary_lines(good) == []


def test_low_calibration_warning_depends_on_the_p_value_mode() -> None:
    rng = np.random.default_rng(14)
    p, y = _shifted(rng, 400, 1.0)
    calib = np.arange(400) < 5
    out = _outcomes(p, y, np.full(400, 18300))
    cons, _ = cf.evaluate(out, calib, ~calib, {"strata": []})
    smooth, _ = cf.evaluate(out, calib, ~calib, {"strata": [], "smoothed": True})
    assert any("their sets always contain both labels" in w for w in cons.warnings)
    assert any("smoothed sets are randomized and uninformative" in w for w in smooth.warnings)
    assert cons.cells[0].frac_both == 1.0 and not cons.cells[0].informative
    assert smooth.cells[0].frac_both < 1.0  # smoothed: a label is dropped at random, so not "both labels"


def test_prepare_hazard_unit_key_and_unlabeled_rows() -> None:
    """Order refs that collide when the key concatenates ref and landmark as text stay two units."""
    t = pa.table(
        {
            "order_ref": ["13:7", "13:7100000000"],
            "landmark_ns": pa.array([NS, 0], pa.int64()),
            "horizon_ns": pa.array([NS, NS], pa.int64()),
            "cif_fill_raw": [0.25, 0.5],
            "y_fill": [True, None],
            "split": ["test", "test"],
        }
    )
    out = cf.prepare_hazard(t)
    assert out.order_ref.tolist() == ["13:7", "13:7100000000"] and out.y[:, 0].tolist() == [1, -1]
    assert not out.dates_known and any("session dates unknown" in n for n in out.notes)
    bare = cf.prepare_hazard(t.drop_columns(["y_fill", "split"]), labels=False)
    assert bare.split is None and bare.y.tolist() == [[-1], [-1]] and bare.excluded_unobserved == {}
    with pytest.raises(QtError, match="y_fill"):
        cf.prepare_hazard(t.drop_columns(["y_fill"]))


def _plain_strata(t: pa.Table) -> pa.Table:
    i = t.schema.get_field_index("stratum")
    return t.set_column(i, "stratum", t.column("stratum").cast(pa.utf8()))


def test_calibrator_round_trip_and_unlabeled_orders(hazard_run, tmp_path: Path) -> None:
    """The calibrator saved and loaded reproduces evaluate's sets on the test rows, and scores the same orders
    without labels (no y_fill, no split, no outcome columns) identically."""
    features, preds, _ = hazard_run
    out = cf.prepare_hazard(preds, features)
    calib, test = cf.mask_split(out, "calib"), cf.mask_split(out, "test")
    assert (out.y[test] < 0).any()  # some test rows have an unknown label at some horizon
    spec = {"alpha": ALPHA, "smoothed": True, "seed": 3, "strata": ["dist_bucket", "side"]}
    _, sets = cf.evaluate(out, calib, test, spec)
    cal = cf.ConformalCalibrator.fit(out, calib, spec)
    entry = cal.save(tmp_path / cf.CALIBRATOR_NAME)
    assert entry["name"] == cf.CALIBRATOR_NAME and entry["rows"] > 0
    loaded = cf.ConformalCalibrator.load(tmp_path / cf.CALIBRATOR_NAME)
    assert loaded.spec == cal.spec and loaded.n_calib_rows == int(calib.sum())
    pred = loaded.predict(out, test)
    assert pred.num_rows == int(test.sum()) * len(out.grid_ns)
    assert _plain_strata(pred).equals(_plain_strata(cal.predict(out, test)))
    known = pred.filter(pac.is_valid(pred.column("y_fill")))
    assert _plain_strata(known).equals(_plain_strata(sets))
    assert loaded.save(tmp_path / "again.parquet")["sha256"] == entry["sha256"]
    # tomorrow's orders: the same predictions without labels
    new = cf.prepare_hazard(
        preds.drop_columns(["y_fill", "split"]), features.drop_columns(["event_ns", "cause"]), labels=False
    )
    assert np.array_equal(new.order_ref, out.order_ref) and np.array_equal(new.landmark_ns, out.landmark_ns)
    unl = loaded.predict(new, test)
    assert unl.column("y_fill").null_count == unl.num_rows
    cols = [c for c in sets.column_names if c != "y_fill"]
    assert _plain_strata(unl.select(cols)).equals(_plain_strata(pred.select(cols)))
    assert unl.column("interval_no_fill_within_ns").null_count < unl.num_rows  # informative bounds exist
    with pytest.raises(QtError) as e:
        cf.ConformalCalibrator.load(tmp_path / "missing.parquet")
    assert e.value.code == "QT-INPUT-001"
    pq.write_table(sets, tmp_path / "sets.parquet")
    with pytest.raises(QtError, match=r"not a qt\.conformal_calibrator"):
        cf.ConformalCalibrator.load(tmp_path / "sets.parquet")
    with pytest.raises(QtError, match="fitted on 'cif_fill_raw'"):
        loaded.predict(cf.prepare_hazard(preds, features, score="cif_fill"))


# ---- table level: zoo predictions -----------------------------------------------------------------------------


def _zoo_table(rng: np.random.Generator, n_per_day: int = 400) -> pa.Table:
    schema = SCHEMAS["qt.zoo_predictions"]
    horizons = np.array([1, 10, 60]) * NS
    rows: dict[str, list] = {f.name: [] for f in schema}
    for d, day in enumerate((18300, 18301)):
        for i in range(n_per_day):
            t_fill = rng.exponential(20.0 if d == 0 else 12.0) * NS
            pred = rng.exponential(18.0) * NS
            ts_live = day * 86_400 * NS + 15 * 3600 * NS + i * NS
            for h in horizons:
                truth = bool(t_fill <= h)
                rows["model_id"].append("m")
                rows["shadow_id"].append(f"s{d}_{i}")
                rows["qty"].append(1)
                rows["horizon_ns"].append(int(h))
                rows["p_fill"].append(float(pred <= h))
                rows["e_fill_qty"].append(float(pred <= h))
                rows["model_ts_first_fill"].append(None)
                rows["truth_filled"].append(None if (i == 0 and h == horizons[-1]) else truth)
                rows["truth_ts_first_fill"].append(None)
                rows["truth_filled_qty"].append(None)
                rows["censored"].append(bool(i == 0 and h == horizons[-1]))
                rows["queue_ahead_at_live"].append(None)
                rows["level_qty_at_live"].append(None)
                rows["visible_level_qty_at_live"].append(100)
                rows["out_of_view_at_live"].append(False)
                rows["instrument_id"].append(7)
                rows["session_date"].append(day)
                rows["ts_live"].append(ts_live)
                rows["trade_vol_60s"].append(0)
    arrays = []
    for f in schema:
        if pa.types.is_date32(f.type):
            arrays.append(pa.array(rows[f.name], pa.int32()).cast(f.type))
        elif pa.types.is_timestamp(f.type):
            arrays.append(pa.array(rows[f.name], pa.int64()).cast(f.type))
        else:
            arrays.append(pa.array(rows[f.name], f.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def _zoo_shadows(
    zoo: pa.Table, *, admin: dict[str, int] | None = None, cancel: dict[str, int] | None = None
) -> pa.Table:
    """The planned life (`qt.zoo_shadows` columns) of the shadows in `zoo`: administrative end one hour after
    `ts_live` and no planned cancel, unless overridden (shadow_id -> ns after `ts_live`)."""
    admin, cancel = admin or {}, cancel or {}
    live: dict[str, int] = {}
    sid = zoo.column("shadow_id").to_pylist()
    for s, t in zip(sid, zoo.column("ts_live").cast(pa.int64()).to_pylist(), strict=True):
        live.setdefault(s, t)
    ids = sorted(live)
    ts = pa.timestamp("ns", tz="UTC")
    return pa.table(
        {
            "shadow_id": ids,
            "ts_cancel_planned": pa.array([live[i] + cancel[i] if i in cancel else None for i in ids]).cast(ts),
            "ts_admin_end": pa.array([live[i] + admin.get(i, 3600 * NS) for i in ids], pa.int64()).cast(ts),
            "never_cancel": [i not in cancel for i in ids],
        }
    )


def test_prepare_zoo_planned_life_populations_and_evaluate() -> None:
    """Label eligibility comes from the planned life (`ts_admin_end >= ts_live + h`), never from `censored`;
    cancel-bearing shadows are a separate population; strata by predicted probability."""
    rng = np.random.default_rng(10)
    zoo = _zoo_table(rng)
    # s*_0: censored at 60 s in the zoo table and ends at 30 s; s*_2: ends at 5 s although its truth is known;
    # s*_1: a planned cancel at 5 s (cancel-bearing)
    early = {"s0_0": 30 * NS, "s1_0": 30 * NS, "s0_2": 5 * NS, "s1_2": 5 * NS}
    life = _zoo_shadows(zoo, admin=early, cancel={"s0_1": 5 * NS, "s1_1": 5 * NS})
    out = cf.prepare_zoo(zoo, "m", life)
    assert out.n == 798 and out.grid_ns.tolist() == [NS, 10 * NS, 60 * NS]
    assert "s0_1" not in set(out.order_ref.tolist())
    assert out.excluded_unobserved == {NS: 0, 10 * NS: 2, 60 * NS: 4}
    i2 = out.order_ref.tolist().index("s0_2")
    assert out.y[i2, 1:].tolist() == [-1, -1]  # the label exists, but the planned life ends before 10 s
    assert any("2 cancel_bearing shadow(s) left out" in n for n in out.notes)
    assert out.strata["predicted"].shape == (798, 3)
    assert set(out.strata["instrument"].tolist()) == {"7"}
    cb = cf.prepare_zoo(zoo, "m", life, population="cancel_bearing")
    assert sorted(cb.order_ref.tolist()) == ["s0_1", "s1_1"]
    calib, test = cf.mask_dates(out, ["2020-02-08"]), cf.mask_dates(out, ["2020-02-09"])
    assert calib.sum() == 399 and test.sum() == 399
    report, sets = cf.evaluate(out, calib, test, {"alpha": ALPHA, "strata": ["predicted"], "smoothed": True})
    assert report.source == "zoo" and report.score == "p_fill"
    strata = {c.stratum for c in report.cells}
    assert {"predicted=p[0.0,0.2)", "predicted=p[0.8,1.0]", cf.ALL} == strata
    assert any("unknown label" in w for w in report.warnings)
    assert any("cancel_bearing shadow(s) left out" in w for w in report.warnings)
    assert report.fill_time and report.fill_time[0].stratum == cf.ALL  # predicted is per horizon: not a time stratum
    assert sets.num_rows == 399 * 3 - 3
    with pytest.raises(QtError, match="no rows for model"):
        cf.prepare_zoo(zoo, "other", life)
    with pytest.raises(QtError) as e:
        cf.prepare_zoo(zoo, "m", None)
    assert e.value.code == "QT-INPUT-001"
    with pytest.raises(QtError, match="planned_life: no row") as e:
        cf.prepare_zoo(zoo, "m", life.slice(1))
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError, match="population"):
        cf.prepare_zoo(zoo, "m", life, population="mirrors")  # type: ignore[arg-type]
    with pytest.raises(QtError, match="no split column"):
        cf.mask_split(out, "test")
    with pytest.raises(QtError, match="invalid date"):
        cf.mask_dates(out, ["2020-13-01"])


def test_zoo_aborted_shadows_leave_every_model() -> None:
    """A shadow that a faithful hbt pair aborted on at #312 leaves the population of every model
    (`common_population`), not only the aborted rows."""
    rng = np.random.default_rng(11)
    one = _zoo_table(rng, 50)
    i = one.schema.get_field_index("model_id")
    other = one.set_column(i, "model_id", pa.array(["m2"] * one.num_rows).cast(one.schema.field(i).type))
    zoo = pa.concat_tables([one, other.cast(one.schema)])
    status = pa.array(["hbt_312_abort"] + [None] * (zoo.num_rows - 1), pa.dictionary(pa.int8(), pa.utf8()))
    zoo = zoo.append_column("status", status)
    life = _zoo_shadows(zoo)
    for model in ("m", "m2"):
        out = cf.prepare_zoo(zoo, model, life)
        assert out.n == 99 and "s0_0" not in set(out.order_ref.tolist()), model
        assert any("1 shadow(s) aborted" in n for n in out.notes)


def test_prepare_zoo_with_features_strata() -> None:
    rng = np.random.default_rng(15)
    zoo = _zoo_table(rng, 50)
    refs = [f"s0_{i}" for i in range(50)]  # day 1 only: day 2 shadows have no features row
    feats = pa.table(
        {
            "order_ref": refs,
            "landmark_ns": pa.array([0] * 50, pa.int64()),
            "dist_ticks": pa.array([float(i % 3) for i in range(50)]),
        }
    )
    out = cf.prepare_zoo(zoo, "m", _zoo_shadows(zoo), feats)
    db = dict(zip(out.order_ref.tolist(), out.strata["dist_bucket"].tolist(), strict=True))
    assert db["s0_0"] == "touch" and db["s0_1"] == "behind1" and db["s0_2"] == "behind2+" and db["s1_0"] == "na"
    assert any("50 of 100 shadow(s) have no features row at landmark 0" in n for n in out.notes)


def test_dist_and_predicted_buckets() -> None:
    assert cf.dist_bucket(np.array([-2.0, -0.0, 0.0, 1.0, 2.0, 7.0, np.nan])).tolist() == [
        "better",
        "touch",
        "touch",
        "behind1",
        "behind2+",
        "behind2+",
        "na",
    ]
    assert cf.predicted_bucket(np.array([0.0, 0.2, 0.99, 1.0])).tolist() == [
        "p[0.0,0.2)",
        "p[0.2,0.4)",
        "p[0.8,1.0]",
        "p[0.8,1.0]",
    ]
    assert cf.clopper_pearson(0, 0) == (None, None)
    lo, hi = cf.clopper_pearson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(1 - 0.025 ** (1 / 10))
    assert cf.clopper_pearson(10, 10)[1] == 1.0


# ---- fit-hazard --conformal-alpha -----------------------------------------------------------------------------


@pytest.mark.requires_core
def test_cli_fit_hazard_conformal_alpha(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """One session date: the calib and test blocks share it, and the martingale still runs calibration rows, then
    test rows. The printed card lists the conformal files; stderr carries the coverage line."""
    from queuetruth.cli import main

    feats = make_features(31, dates=("2026-03-02",), orders_per_day=600)
    pq.write_table(feats, tmp_path / "features.parquet")
    argv = ["fit-hazard", "--features", str(tmp_path), "--out", str(tmp_path / "m"), "--bootstrap-b", "20"]
    assert main([*argv, "--conformal-alpha", "0.1", "--format", "json"]) == 0
    io = capsys.readouterr()
    printed = json.loads(io.out)
    names = [f["name"] for f in printed["files"]]
    assert {cf.REPORT_NAME, cf.SETS_NAME, cf.CALIBRATOR_NAME} <= set(names)
    assert "pooled test coverage at alpha 0.1: 1s" in io.err
    doc = json.loads((tmp_path / "m" / cf.REPORT_NAME).read_text())
    report = cf.ConformalReport.model_validate(doc)
    assert report.source == "hazard" and report.score == "cif_fill_raw" and report.alpha == 0.1
    assert report.calib_dates == report.test_dates == ["2026-03-02"]
    assert all([g.group for g in d.groups] == ["calib:2026-03-02", "test:2026-03-02"] for d in report.drift)
    card = json.loads((tmp_path / "m" / "model.json").read_text())
    assert report.model_id == card["model_id"]
    sets = pq.read_table(tmp_path / "m" / cf.SETS_NAME)
    assert sets.schema.metadata[b"qt.schema"] == b"qt.conformal_sets" and sets.num_rows > 0
    cal = cf.ConformalCalibrator.load(tmp_path / "m" / cf.CALIBRATOR_NAME)
    assert cal.spec.alpha == 0.1 and cal.score == "cif_fill_raw"
    assert not list((tmp_path / "m").glob(".*.tmp"))
    bad = ["fit-hazard", "--features", str(tmp_path), "--out", str(tmp_path / "bad"), "--conformal-alpha", "1.5"]
    assert main(bad) == 2
    assert "alpha" in capsys.readouterr().err
    assert not (tmp_path / "bad").exists()  # refused before fitting: nothing written
    # a features table without the default stratum's column is refused before the fit too
    (tmp_path / "nodist").mkdir()
    pq.write_table(feats.drop_columns(["dist_ticks"]), tmp_path / "nodist" / "features.parquet")
    nodist = ["fit-hazard", "--features", str(tmp_path / "nodist"), "--out", str(tmp_path / "nd")]
    assert main([*nodist, "--conformal-alpha", "0.1"]) != 0
    assert "dist_ticks" in capsys.readouterr().err
    assert not (tmp_path / "nd").exists()


def test_conformal_step_validates_before_fitting(tmp_path: Path) -> None:
    feats = {"path": str(tmp_path)}
    pq.write_table(make_features(3, orders_per_day=20), tmp_path / "features.parquet")
    with pytest.raises(QtError, match="needs --out"):
        cf.conformal_step({"out": None, "features": feats}, 0.1)
    with pytest.raises(QtError, match="conformal-alpha"):
        cf.conformal_step({"out": "x", "features": feats}, "abc")
    with pytest.raises(QtError):
        cf.conformal_step({"out": "x", "features": feats}, 0.0)
    with pytest.raises(QtError) as e:
        cf.conformal_step({"out": "x", "features": {"path": str(tmp_path / "none")}}, 0.1)
    assert e.value.code == "QT-INPUT-001"
    assert callable(cf.conformal_step({"out": "x", "features": feats}, 0.1))


# ---- real data (docs/concepts/conformal-fill-probability.md) ---------------------------------------------------

REALDATA = Path(os.environ.get("QT_REALDATA_DIR", Path(__file__).resolve().parents[3] / ".realdata"))
HAZ = REALDATA / "hazard" / "2d-40000-l2" / "predictions.parquet"
FEAT = REALDATA / "features" / "2d-40000" / "features.parquet"
ZOO = REALDATA / "zoo" / "2d-40000-subset" / "zoo_predictions.parquet"
ZOO_LIFE = REALDATA / "zoo" / "2d-40000-subset" / "zoo_shadows.parquet"


@pytest.mark.realdata
@pytest.mark.skipif(not all(p.exists() for p in (HAZ, FEAT, ZOO, ZOO_LIFE)), reason="needs .realdata hazard/zoo runs")
def test_realdata_coverage_table() -> None:
    """Every number on the concept page: hazard (L2-only, `cif_fill_raw`) calibrated on 2019-12-30 (calib split) and
    tested on 2020-01-30, the same day split at random by order, and the zoo models day 1 -> day 2."""
    feats = pq.read_table(FEAT)
    out = cf.prepare_hazard(pq.read_table(HAZ), feats)
    assert sum(out.excluded_unobserved.values()) == 0  # no label censored before any horizon on these days
    calib, test = cf.mask_split(out, "calib"), cf.mask_split(out, "test")
    assert (len(set(out.order_ref[calib].tolist())), int(calib.sum())) == (3676, 7552)
    assert (len(set(out.order_ref[test].tolist())), int(test.sum())) == (19464, 29840)
    spec = {"alpha": ALPHA, "strata": ["dist_bucket"]}
    cross, _ = cf.evaluate(out, calib, test, spec)
    a, b = cf.random_order_halves(out, test, seed=0)
    assert (int(a.sum()), int(b.sum())) == (14865, 14975)
    halves, _ = cf.evaluate(out, a, b, spec)
    lc, _ = cf.evaluate(out, a, b, {**spec, "label_conditional": True})

    def pooled(r: cf.ConformalReport) -> dict[int, cf.CoverageCell]:
        return {c.horizon_ns: c for c in r.cells if c.landmark_ns is None}

    def r3(x: float | None) -> float:
        assert x is not None
        return round(x, 3)

    ms = [100_000_000, 250_000_000, 500_000_000, NS, 2 * NS, 5 * NS, 10 * NS, 30 * NS, 60 * NS]
    pc, ph, pl = pooled(cross), pooled(halves), pooled(lc)
    assert [r3(pc[h].coverage) for h in ms] == [0.770, 0.775, 0.798, 0.795, 0.812, 0.874, 0.903, 0.906, 0.904]
    assert [(r3(pc[h].coverage_ci_lo), r3(pc[h].coverage_ci_hi)) for h in ms] == [
        (0.765, 0.774),
        (0.770, 0.780),
        (0.793, 0.803),
        (0.791, 0.800),
        (0.807, 0.816),
        (0.870, 0.878),
        (0.899, 0.906),
        (0.903, 0.909),
        (0.901, 0.907),
    ]
    assert [(r3(pc[h].fill_rate_calib), r3(pc[h].fill_rate_test)) for h in ms] == [
        (0.009, 0.024),
        (0.024, 0.054),
        (0.044, 0.100),
        (0.077, 0.170),
        (0.138, 0.257),
        (0.261, 0.355),
        (0.353, 0.388),
        (0.447, 0.398),
        (0.460, 0.398),
    ]
    assert [r3(ph[h].coverage) for h in ms] == [0.902, 0.900, 0.901, 0.896, 0.894, 0.897, 0.897, 0.897, 0.897]
    d0 = {d.horizon_ns: d for d in cross.drift if d.landmark_ns == 0}
    assert [round(d0[h].log10_max, 2) for h in ms] == [9.67, 2.71, 0.81, 0.37, 0.20, 0.05, 0.03, 0.05, 0.37]
    alarms = {(d.horizon_ns, d.landmark_ns) for d in cross.drift if d.alarm}
    assert alarms == {
        (100_000_000, 0),
        (100_000_000, NS),
        (100_000_000, 5 * NS),
        (250_000_000, 0),
        (250_000_000, 5 * NS),
        (NS, 5 * NS),
    }
    assert len(cross.drift) == 27 and not any(d.alarm for d in halves.drift)
    # Bonferroni over the 27 cells (threshold 2,700): three cells reach it
    assert cross.drift_familywise_threshold == 2700.0 and cross.drift_familywise_alarm
    fw = {(d.horizon_ns, d.landmark_ns): round(d.log10_max, 2) for d in cross.drift if d.log10_max >= math.log10(2700)}
    assert fw == {(100_000_000, 0): 9.67, (100_000_000, 5 * NS): 3.76, (NS, 5 * NS): 3.98}
    # same-day halves: calibration half, then test half, in one martingale; the largest value is far from 100
    assert round(max(d.log10_max for d in halves.drift), 2) == 1.24 and not halves.drift_familywise_alarm
    # time-of-day control: day 2 test rows restricted to the clock window of the calibration block
    tod = out.time_ns - out.date * 86_400 * NS
    window = test & (tod >= tod[calib].min()) & (tod <= tod[calib].max())
    assert (int(window.sum()), round(tod[calib].min() / 3600 / NS, 2), round(tod[calib].max() / 3600 / NS, 2)) == (
        5493,
        19.6,
        20.83,
    )
    ctl, _ = cf.evaluate(out, calib, window, spec)
    pt = pooled(ctl)
    assert [r3(pt[h].coverage) for h in ms] == [0.667, 0.675, 0.712, 0.754, 0.824, 0.873, 0.889, 0.892, 0.890]
    assert sum(d.alarm for d in ctl.drift) == 9
    by = {
        c.stratum: (c.n_calib, c.n_test, r3(c.coverage))
        for c in cross.cells
        if c.horizon_ns == NS and c.landmark_ns == 0
    }
    hb = {c.stratum: r3(c.coverage) for c in halves.cells if c.horizon_ns == NS and c.landmark_ns == 0}
    assert by["dist_bucket=behind1"] == (753, 4092, 0.704) and hb["dist_bucket=behind1"] == 0.889
    assert by["dist_bucket=touch"] == (2330, 11897, 0.798) and hb["dist_bucket=touch"] == 0.901
    assert by["dist_bucket=better"] == (196, 1465, 0.869) and hb["dist_bucket=better"] == 0.902
    assert by["dist_bucket=behind2+"] == (397, 2010, 0.951) and hb["dist_bucket=behind2+"] == 0.894
    assert r3(ph[100_000_000].coverage_fill) == 0.000 and r3(ph[30 * NS].coverage_fill) == 0.824
    assert min(r3(pl[h].coverage_fill) for h in ms) == 0.899 and max(r3(pl[h].coverage_fill) for h in ms) == 0.912
    assert min(r3(pl[h].coverage_nofill) for h in ms) == 0.895
    assert max(r3(pl[h].coverage_nofill) for h in ms) == 0.902
    assert min(r3(pl[h].frac_both) for h in ms) == 0.645 and max(r3(pl[h].frac_both) for h in ms) == 0.663
    ft_h = {c.landmark_ns: c for c in halves.fill_time if c.stratum == cf.ALL}[None]
    ft_c = {c.landmark_ns: c for c in cross.fill_time if c.stratum == cf.ALL}[None]
    assert (r3(ft_h.lower_coverage), r3(ft_c.lower_coverage)) == (0.903, 0.839)
    assert (r3(ft_h.interval_coverage), r3(ft_c.interval_coverage)) == (0.954, 0.905)
    assert all(c.frac_upper_claim in (0.0, None) for c in [*halves.fill_time, *cross.fill_time])

    zoo = pq.read_table(ZOO)
    life = pq.read_table(ZOO_LIFE)
    want = {
        "front_of_queue[mode=faithful]": (0.996, 0.906, 0.975, 0.914),
        "hbt_power3[exch=partial;mode=faithful;n=2]": (0.990, 0.898, 0.986, 0.900),
        "hbt_risk_adverse[exch=partial;mode=faithful]": (0.992, 0.896, 0.987, 0.898),
        "nt_queue_l2[mode=faithful]": (0.999, 0.899, 0.997, 0.900),
        "trade_through_only[mode=faithful]": (0.981, 0.893, 0.965, 0.895),
    }
    for model, nums in want.items():
        z = cf.prepare_zoo(zoo, model, life, feats)
        zc, zt = cf.mask_dates(z, ["2019-12-30"]), cf.mask_dates(z, ["2020-01-30"])
        assert (int(zc.sum()), int(zt.sum()), sum(z.excluded_unobserved.values())) == (20000, 20000, 0)
        assert set(np.unique(z.p).tolist()) == {0.0, 1.0}  # every zoo model here predicts 0 or 1
        assert z.notes == ["zoo: 1288 of 40000 shadow(s) have no features row at landmark 0: stratum na"]
        zspec = {"alpha": ALPHA, "strata": ["predicted"]}
        cons, _ = cf.evaluate(z, zc, zt, zspec)
        smooth, _ = cf.evaluate(z, zc, zt, {**zspec, "smoothed": True})

        def at(r: cf.ConformalReport, h: int) -> float:
            return r3(next(c.coverage for c in r.cells if c.horizon_ns == h and c.stratum == cf.ALL))

        assert (at(cons, NS), at(smooth, NS), at(cons, 10 * NS), at(smooth, 10 * NS)) == nums, model
        assert len(smooth.drift) == 10 and not any(d.alarm for d in smooth.drift), model
        if model.startswith("front_of_queue"):
            hit = next(c for c in smooth.cells if c.horizon_ns == NS and c.stratum == "predicted=p[0.8,1.0]")
            assert (r3(hit.va_p0_mean), r3(hit.va_p1_mean), r3(hit.fill_rate_test)) == (0.786, 0.786, 0.870)
