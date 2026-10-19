"""O-STAT-5: Brier, Murphy (with within_bin), Hosmer-Lemeshow and reliability tables equal et's calibration.py;
scoring rules, Spiegelhalter and calibration slope/intercept on the shared scoring reference."""

from __future__ import annotations

import numpy as np
import pytest
import statsmodels.api as sm

from queuetruth import stats

REL = 1e-12
ABS = 1e-14


def test_o_stat_5_et_calibration(reference) -> None:
    doc = reference("et_calibration.json")
    assert len(doc["cases"]) >= 5
    for case in doc["cases"]:
        p = np.array(case["input"]["p"], float)
        y = np.array(case["input"]["y"], float)
        k = case["n_bins"]
        ref = case["calibrate"]
        assert stats.brier(p, y) == pytest.approx(ref["brier"], rel=REL, abs=ABS), case["name"]
        m = stats.murphy(p, y, k)
        for key in ("reliability", "resolution", "uncertainty", "within_bin"):
            assert m[key] == pytest.approx(ref[key], rel=REL, abs=ABS), (case["name"], key)
        assert m["reliability"] - m["resolution"] + m["uncertainty"] + m["within_bin"] == pytest.approx(m["brier"])
        hl = stats.hosmer_lemeshow(p, y, k)
        assert hl.statistic == pytest.approx(ref["hosmer_lemeshow"], rel=REL, abs=ABS), case["name"]
        assert hl.p_value == pytest.approx(ref["hl_pvalue"], rel=REL, abs=ABS), case["name"]
        t = stats.reliability_table(p, y, k)
        for col, values in case["reliability_table"].items():
            got = np.asarray(t[col], float)
            np.testing.assert_allclose(
                got, np.array(values, float), rtol=REL, atol=ABS, err_msg=f"{case['name']}:{col}"
            )


def test_o_stat_5_scoring_rules_reference(reference) -> None:
    cal = reference("scoring_rules.json")["calibration"]
    p = np.array(cal["input"]["p"], float)
    y = np.array(cal["input"]["y"], float)
    assert stats.brier(p, y) == pytest.approx(cal["brier"], rel=1e-12)
    assert stats.log_loss(p, y, eps=1e-6) == pytest.approx(cal["log_loss_eps1e-6"], rel=1e-12)
    z = stats.spiegelhalter_z(p, y)
    assert z.statistic == pytest.approx(cal["spiegelhalter_z"], rel=1e-12)
    assert z.p_value == pytest.approx(cal["spiegelhalter_p_two_sided"], rel=1e-12)
    slope, intercept = stats.calibration_slope_intercept(p, y)
    assert slope == pytest.approx(cal["calibration_slope"], abs=1e-8)
    assert intercept == pytest.approx(cal["calibration_intercept"], abs=1e-8)


def test_calibration_slope_intercept_vs_statsmodels_and_degenerate() -> None:
    rng = np.random.default_rng(4)
    p = rng.uniform(0.02, 0.98, 400)
    y = (rng.random(400) < np.clip(p * 0.8 + 0.1, 0, 1)).astype(float)
    x = np.log(p / (1 - p))
    fit = sm.Logit(y, sm.add_constant(x)).fit(disp=0, tol=1e-12)
    slope, intercept = stats.calibration_slope_intercept(p, y)
    assert slope == pytest.approx(fit.params[1], abs=1e-8) and intercept == pytest.approx(fit.params[0], abs=1e-8)
    assert all(np.isnan(stats.calibration_slope_intercept(np.full(10, 0.3), y[:10])))
    assert all(np.isnan(stats.calibration_slope_intercept(p[:10], np.ones(10))))


def test_hand_values_and_input_checks() -> None:
    p = np.array([0.1, 0.9, 0.5, 0.5])
    y = np.array([0, 1, 1, 0])
    assert stats.brier(p, y) == pytest.approx((0.01 + 0.01 + 0.25 + 0.25) / 4)
    ll = -(np.log(0.9) + np.log(0.9) + np.log(0.5) + np.log(0.5)) / 4
    assert stats.log_loss(p, y) == pytest.approx(ll)
    assert stats.log_loss(np.array([0.0]), np.array([1])) == pytest.approx(-np.log(1e-6))
    assert stats.bins(np.array([0.3, 0.1, 0.3, 0.2]), 4).tolist() == [2, 0, 2, 1]
    with pytest.raises(ValueError):
        stats.brier(np.array([1.2]), np.array([1]))
    with pytest.raises(ValueError):
        stats.brier(np.array([0.2]), np.array([2]))
