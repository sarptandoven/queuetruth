"""Reliability, ECE and the consistency test; the power analysis against Monte Carlo."""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import special
from scipy import stats as sps

from queuetruth.forecast.calibration import bins, calibration, ece
from queuetruth.forecast.inference import dm_test
from queuetruth.forecast.power import brier_edge_moments, log_edge_moments, power_at, required_n
from queuetruth.forecast.simulate import binary_world


def test_ece_hand_example() -> None:
    p = np.array([0.05, 0.15, 0.15, 0.95, 1.0])
    y = np.array([0.0, 1.0, 0.0, 1.0, 1.0])
    assert bins(p, 10).tolist() == [0, 1, 1, 9, 9]
    # bin 0: |0 - 0.05|; bin 1: |1 - 0.3|; bin 9: |2 - 1.95|; over N = 5
    assert ece(p, y, 10) == pytest.approx((0.05 + 0.7 + 0.05) / 5)
    assert ece(p, y, 10, weights=np.array([0, 0, 0, 1, 1])) == pytest.approx(0.05 / 2)
    assert math.isnan(ece(np.array([]), np.array([])))


def test_calibration_block_known_counts() -> None:
    p = np.array([0.05, 0.15, 0.15, 0.95, 1.0])
    y = np.array([0.0, 1.0, 0.0, 1.0, 1.0])
    c = calibration(p, y, np.arange(5), target="binary", n_bins=10, b=200, seed=1)
    assert c.n == 5 and len(c.bins) == 10
    b1 = c.bins[1]
    assert (b1.count, b1.mean_p, b1.observed_rate) == (2, 0.15, 0.5)
    assert b1.rate_lo < 0.5 < b1.rate_hi
    assert c.bins[2].count == 0 and c.bins[2].observed_rate is None
    assert c.ece.value == pytest.approx(0.16)
    assert c.ece.ci_lo is not None and 0.0 <= c.ece.ci_lo <= c.ece.ci_hi
    assert set(c.murphy) == {"brier", "reliability", "resolution", "uncertainty", "within_bin"}
    assert c.murphy["brier"] == pytest.approx(np.mean((p - y) ** 2))
    assert c.bins[9].consistency_lo is not None


def test_calibrated_forecaster_passes_and_miscalibrated_fails() -> None:
    rng = np.random.default_rng(4)
    n = 3000
    p = rng.uniform(0.02, 0.98, size=n)
    y = (rng.random(n) < p).astype(float)
    good = calibration(p, y, np.arange(n), target="binary", b=300, seed=2)
    assert good.ece_p_value > 0.05
    assert good.ece.value == pytest.approx(good.ece_if_calibrated, abs=0.02)
    # observed rates sit inside their consistency bands (allowing one slip)
    inside = sum(r.consistency_lo <= r.observed_rate <= r.consistency_hi for r in good.bins if r.count)
    assert inside >= 9
    over = special.expit(2.0 * special.logit(p))  # overconfident reports of the same events
    bad = calibration(over, y, np.arange(n), target="binary", b=300, seed=2)
    assert bad.ece_p_value < 0.01
    assert bad.ece.ci_lo > good.ece.ci_hi
    # the basic interval reflects the plug-in's upward bias: for a calibrated forecaster (true ECE 0) it reaches
    # down to about 0 instead of sitting above the estimate
    assert good.ece.ci_lo < 0.005 < good.ece.value


def test_consistency_test_respects_clusters() -> None:
    """A calibrated forecaster on 12 groups x 8 copies of one event: one uniform per cluster keeps the test's size
    near 5%; drawing the 96 outcomes independently rejects nearly every time."""
    rng = np.random.default_rng(8)
    sims, g, k = 150, 12, 8
    cl = np.repeat(np.arange(g), k)
    rej_cl = rej_row = 0
    for s in range(sims):
        pg = rng.uniform(0.1, 0.9, size=g)
        yg = (rng.random(g) < pg).astype(float)
        p, y = np.repeat(pg, k), np.repeat(yg, k)
        rej_cl += calibration(p, y, cl, target="binary", b=200, seed=s).ece_p_value < 0.05
        rej_row += calibration(p, y, np.arange(g * k), target="binary", b=200, seed=s).ece_p_value < 0.05
    assert rej_cl / sims <= 0.10, rej_cl / sims
    assert rej_row / sims > 0.8, rej_row / sims


def test_power_at_and_required_n_closed_forms() -> None:
    # normal approximation: n = ((z_0.975 + z_0.8) sd / delta)^2 = 7.849 (sd/delta)^2; t adds a few
    n = required_n(0.01, 0.1)
    z = (sps.norm.ppf(0.975) + sps.norm.ppf(0.8)) ** 2 * 100
    assert z == pytest.approx(784.89, abs=0.01)
    assert n is not None and z <= n <= z + 3
    assert power_at(n, 0.01, 0.1) >= 0.8 > power_at(n - 1, 0.01, 0.1)
    assert required_n(-0.01, 0.1) == n  # two-sided: the sign does not matter
    assert required_n(0.01, 0.1, deff=2.0) in (2 * n - 2, 2 * n - 1, 2 * n, 2 * n + 1)
    assert required_n(0.0, 0.1) is None
    assert required_n(0.01, 0.0) is None
    assert required_n(1e-9, 1.0) is None  # beyond MAX_N
    assert required_n(1e-4, 1.0) == pytest.approx(7.849e8, rel=1e-3)
    assert math.isnan(power_at(1, 0.1, 1.0))
    assert power_at(10, 0.0, 1.0) == pytest.approx(0.05, abs=1e-9)  # size of the test


def test_power_against_simulation() -> None:
    """At the required n, the simulated rejection rate of the paired test is the target power (normal d)."""
    delta, sd = 0.05, 0.25
    n = required_n(delta, sd)
    assert n is not None
    rng = np.random.default_rng(8)
    sims = 1500
    rej = sum(dm_test(rng.normal(delta, sd, size=n), lag=0).p_value < 0.05 for _ in range(sims))
    assert rej / sims == pytest.approx(0.8, abs=0.035)


def test_edge_moments_match_monte_carlo() -> None:
    w = binary_world(200_000, 0.8, seed=3)
    d_brier = (w.q - w.y) ** 2 - (w.p - w.y) ** 2
    delta, sd = brier_edge_moments(w.q, w.p)
    assert d_brier.mean() == pytest.approx(delta, rel=0.03)
    assert d_brier.std() == pytest.approx(sd, rel=0.02)
    ly = np.where(w.y == 1, np.log(w.p / w.q), np.log((1 - w.p) / (1 - w.q)))
    delta_l, sd_l = log_edge_moments(w.q, w.p)
    assert ly.mean() == pytest.approx(delta_l, rel=0.03)
    assert ly.std() == pytest.approx(sd_l, rel=0.02)


def test_power_for_a_known_edge_world() -> None:
    """The questions the analysis asks for give the promised power for the Brier edge over the market."""
    big = binary_world(100_000, 0.6, seed=12)
    delta, sd = brier_edge_moments(big.q, big.p)
    n = required_n(delta, sd)
    assert n is not None and 300 < n < 3000
    hits = 0
    sims = 300
    for k in range(sims):
        w = binary_world(n, 0.6, seed=1000 + k)
        d = (w.q - w.y) ** 2 - (w.p - w.y) ** 2
        hits += dm_test(d, lag=0).p_value < 0.05
    assert hits / sims == pytest.approx(0.8, abs=0.07)
