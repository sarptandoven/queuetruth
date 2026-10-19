"""IPCW-weighted calibration diagnostics of the hazard model card (SPEC 5.5.7 / M21; review F2).

Unweighted on the rows with known status, a perfectly calibrated model looks like an underpredictor whenever rows are
censored before the horizon (review reproducer `exp_calib_unweighted.py`: intercept 0.55, Spiegelhalter z 53, HL p 0).
"""

from __future__ import annotations

import math

import numpy as np
import pyarrow as pa
import pytest
from scipy import special

from model.hazard_synth import NS, make_features
from queuetruth import stats
from queuetruth.model._common import CENSOR, FILL, float_matrix, outcome_arrays
from queuetruth.model.scoring import censoring_km, ipcw_calibration, ipcw_weights

TAU = 10 * NS


def _censored_sample(seed: int, n: int, mis: float = 0.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The reviewer's sample: p ~ U(0.05, 0.6) calibrated (or shifted by `mis`), fill time uniform in (0, tau] when
    it happens, independent exponential censoring with mean 10 s. Returns (p, y by tau, IPCW weights at tau)."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.05, 0.6, n)
    fills = rng.random(n) < np.clip(p + mis, 0.0, 1.0)
    t_fill = np.where(fills, rng.uniform(0, TAU, n), np.inf)
    t_event = np.minimum(t_fill, 60 * NS)
    c_time = rng.exponential(10e9, n)
    e = np.minimum(t_event, c_time).astype(np.int64)
    cause = np.where((t_event <= c_time) & np.isfinite(t_fill), FILL, CENSOR).astype(np.int8)
    w = ipcw_weights(e, cause, TAU, censoring_km(e, cause))
    y = ((cause == FILL) & (e <= TAU)).astype(np.float64)
    return p, y, w


def test_f2_reviewer_repro_calibrated_model_is_calibrated() -> None:
    p, y, w = _censored_sample(1, 200_000)
    known = w > 0
    # as built (unweighted, censored-before rows dropped): biased toward "model underfills"
    _, intercept_unweighted = stats.calibration_slope_intercept(p[known], y[known])
    assert intercept_unweighted > 0.4
    assert stats.spiegelhalter_z(p[known], y[known]).statistic > 20
    r = ipcw_calibration(p, y, w)
    assert abs(r["calib_intercept"]) < 0.03
    assert abs(r["calib_slope"] - 1.0) < 0.05
    assert abs(r["spiegelhalter_z"]) < 3.0
    assert r["hl_p"] > 0.01 and r["hl_p_et"] > 0.01


def test_f2_null_distribution_and_power() -> None:
    zs, hl = [], []
    for s in range(60):
        r = ipcw_calibration(*_censored_sample(100 + s, 3000))
        zs.append(r["spiegelhalter_z"])
        hl.append(r["hl_p"])
    assert np.mean(np.abs(np.asarray(zs)) > 1.96) <= 0.1
    assert np.mean(np.asarray(hl) < 0.05) <= 0.15
    bad = ipcw_calibration(*_censored_sample(7, 20_000, mis=0.05))
    assert bad["calib_intercept"] > 0.1 and bad["spiegelhalter_z"] > 3.0 and bad["hl_p"] < 1e-3


def _true_cif(tbl: pa.Table, tau_s: float) -> np.ndarray:
    """CIF of fill by `tau` of `hazard_synth`'s process (defaults): fill rate `0.04 exp(1.6 imb + 2.2 (1 - q) - 0.7
    dist)` per second against a constant cancel rate 0.03; censoring is independent of both."""
    x = float_matrix(tbl, ["imb_l1", "queue_share_ra", "dist_ticks"])
    lam_f = 0.04 * np.exp(1.6 * x[:, 0] + 2.2 * (1.0 - x[:, 1]) - 0.7 * x[:, 2])
    lam = lam_f + 0.03
    return lam_f / lam * (1.0 - np.exp(-lam * tau_s))


def test_f2_true_model_of_the_generator_is_calibrated() -> None:
    """The generator's own CIF on its own rows (landmark 0) with heavy independent censoring (mean 10 s)."""
    tbl = make_features(5, orders_per_day=2000, random_censor_rate=0.1)
    tbl = tbl.filter(pa.compute.equal(tbl.column("landmark_ns"), 0))
    e, c = outcome_arrays(tbl)
    p = _true_cif(tbl, TAU / NS)
    w = ipcw_weights(e, c, TAU, censoring_km(e, c))
    y = ((c == FILL) & (e <= TAU)).astype(np.float64)
    assert np.mean(w == 0) > 0.2  # a fifth of the rows are censored before 10 s
    known = w > 0
    assert stats.calibration_slope_intercept(p[known], y[known])[1] > 0.2  # the unweighted bias
    r = ipcw_calibration(p, y, w)
    assert abs(r["calib_intercept"]) < 0.1
    assert abs(r["calib_slope"] - 1.0) < 0.15
    assert abs(r["spiegelhalter_z"]) < 3.0
    assert r["hl_p"] > 1e-3


def test_unweighted_limit_matches_textbook_forms() -> None:
    rng = np.random.default_rng(3)
    p = rng.uniform(0.02, 0.9, 4000)
    y = (rng.random(4000) < np.clip(p * 1.1, 0, 1)).astype(np.float64)
    r = ipcw_calibration(p, y, np.ones_like(p))
    assert r["spiegelhalter_z"] == pytest.approx(stats.spiegelhalter_z(p, y).statistic, rel=1e-12)
    assert r["hl_p_et"] == pytest.approx(stats.hosmer_lemeshow(p, y).p_value, rel=1e-9)
    assert r["calib_slope"] == pytest.approx(stats.calibration_slope_intercept(p, y)[0], rel=1e-9)
    # calibration-in-the-large: slope fixed at 1 (score equation of the offset model is zero)
    x = special.logit(np.clip(p, 1e-6, 1 - 1e-6))
    assert np.sum(y - special.expit(r["calib_intercept"] + x)) == pytest.approx(0.0, abs=1e-6)
    # a constant weight changes nothing
    r2 = ipcw_calibration(p, y, np.full_like(p, 2.5))
    for k in ("calib_slope", "calib_intercept", "spiegelhalter_z", "hl_p", "hl_p_et"):
        assert r2[k] == pytest.approx(r[k], rel=1e-9), k


def test_undefined_cases_are_nan() -> None:
    p = np.array([0.2, 0.4, 0.6])
    one_class = ipcw_calibration(p, np.zeros(3), np.ones(3))
    assert math.isnan(one_class["calib_slope"]) and math.isnan(one_class["calib_intercept"])
    tiny = ipcw_calibration(p, np.array([1.0, 0.0, 1.0]), np.array([0.0, 0.0, 1.0]))
    assert all(math.isnan(v) for v in tiny.values())
    with pytest.raises(ValueError):
        ipcw_calibration(p, np.array([0.0, 0.5, 1.0]), np.ones(3))
