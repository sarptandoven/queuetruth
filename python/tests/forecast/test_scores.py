"""Proper scoring rules against hand-computed values, closed forms and numerical integration."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from scipy import integrate
from scipy import stats as sps

from queuetruth.forecast.scores import (
    binary_scores,
    categorical_scores,
    crps_normal,
    crps_quantiles,
    crps_samples,
    quantile_weights,
)


def test_binary_known_values() -> None:
    s, clipped = binary_scores(np.array([0.7, 0.7, 0.5, 1.0]), np.array([1.0, 0.0, 1.0, 0.0]))
    assert s["brier"] == pytest.approx([0.09, 0.49, 0.25, 1.0])
    assert s["log"][:3] == pytest.approx([-math.log(0.7), -math.log(0.3), math.log(2.0)])
    assert s["log"][3] == pytest.approx(-math.log(1e-6))  # p_y = 0 is clipped at eps
    assert clipped.tolist() == [False, False, False, True]
    norm = math.sqrt(0.7**2 + 0.3**2)
    assert s["spherical"][:2] == pytest.approx([1 - 0.7 / norm, 1 - 0.3 / norm])
    assert s["spherical"][2] == pytest.approx(1 - 0.5 / math.sqrt(0.5))
    assert s["spherical"][3] == pytest.approx(1.0)


def test_binary_perfect_forecast_scores_zero() -> None:
    s, clipped = binary_scores(np.array([1.0, 0.0]), np.array([1.0, 0.0]))
    for m in ("brier", "log", "spherical"):
        assert s[m] == pytest.approx([0.0, 0.0], abs=1e-15)
    assert not clipped.any()


@pytest.mark.parametrize(
    ("p", "y", "msg"),
    [
        ([0.5, 1.2], [1, 0], "p must lie"),
        ([0.5, np.nan], [1, 0], "p must lie"),
        ([0.5, 0.5], [1, 2], "y must be"),
        ([0.5], [1, 0], "same shape"),
    ],
)
def test_binary_validation(p: list[float], y: list[float], msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        binary_scores(np.array(p), np.array(y, dtype=float))


@pytest.mark.parametrize("p", [0.1, 0.35, 0.8])
def test_binary_rules_are_proper(p: float) -> None:
    """The expected loss under the truth p is minimised by reporting p (grid search)."""
    grid = np.linspace(0.001, 0.999, 999)
    for m in ("brier", "log", "spherical"):
        s1, _ = binary_scores(grid, np.ones_like(grid))
        s0, _ = binary_scores(grid, np.zeros_like(grid))
        expected = p * s1[m] + (1 - p) * s0[m]
        assert grid[int(np.argmin(expected))] == pytest.approx(p, abs=1.5e-3), m


def test_categorical_known_values() -> None:
    probs = np.array([[0.2, 0.5, 0.3], [0.2, 0.5, 0.3]])
    s, _ = categorical_scores(probs, np.array([1, 2]), ordered=True)
    assert s["brier"] == pytest.approx([0.04 + 0.25 + 0.09, 0.04 + 0.25 + 0.49])
    assert s["log"] == pytest.approx([-math.log(0.5), -math.log(0.3)])
    norm = math.sqrt(0.04 + 0.25 + 0.09)
    assert s["spherical"] == pytest.approx([1 - 0.5 / norm, 1 - 0.3 / norm])
    # RPS: cumulative (0.2, 0.7) vs (0, 1) and (0, 0); divided by K - 1 = 2
    assert s["rps"] == pytest.approx([(0.04 + 0.09) / 2, (0.04 + 0.49) / 2])


def test_categorical_two_outcomes_match_binary() -> None:
    p = np.array([0.3, 0.9, 0.6])
    y = np.array([1.0, 0.0, 1.0])
    b, _ = binary_scores(p, y)
    c, _ = categorical_scores(np.column_stack([1 - p, p]), y.astype(int), ordered=True)
    assert c["brier"] == pytest.approx(2 * b["brier"])
    assert c["rps"] == pytest.approx(b["brier"])
    assert c["log"] == pytest.approx(b["log"])
    assert c["spherical"] == pytest.approx(b["spherical"])


def test_categorical_rps_only_when_ordered_and_validation() -> None:
    s, _ = categorical_scores(np.array([[0.5, 0.5]]), np.array([0]))
    assert "rps" not in s
    with pytest.raises(ValueError, match="out of range"):
        categorical_scores(np.array([[0.5, 0.5]]), np.array([2]))
    with pytest.raises(ValueError, match=r"\(n, K"):
        categorical_scores(np.array([0.5, 0.5]), np.array([0]))


def test_crps_normal_known_value() -> None:
    # CRPS(N(0, 1), 0) = 2 phi(0) - 1 / sqrt(pi) = sqrt(2 / pi) - 1 / sqrt(pi)
    assert float(crps_normal(0.0, 1.0, 0.0)) == pytest.approx(math.sqrt(2 / math.pi) - 1 / math.sqrt(math.pi))
    assert float(crps_normal(0.0, 1.0, 0.0)) == pytest.approx(0.23369497725510913)
    # location-scale: CRPS(N(m, s), y) = s CRPS(N(0, 1), (y - m) / s)
    assert float(crps_normal(3.0, 2.0, 4.0)) == pytest.approx(2 * float(crps_normal(0.0, 1.0, 0.5)))
    with pytest.raises(ValueError):
        crps_normal(0.0, 0.0, 1.0)


def _crps_integral(cdf, y: float, lo: float, hi: float) -> float:  # type: ignore[no-untyped-def]
    left = integrate.quad(lambda x: cdf(x) ** 2, lo, y, limit=400)[0]
    right = integrate.quad(lambda x: (1 - cdf(x)) ** 2, y, hi, limit=400)[0]
    return left + right


@pytest.mark.parametrize("y", [-1.3, 0.0, 0.4, 2.5])
def test_crps_normal_matches_integral(y: float) -> None:
    want = _crps_integral(sps.norm(0.2, 0.7).cdf, y, -12, 12)
    assert float(crps_normal(0.2, 0.7, y)) == pytest.approx(want, rel=1e-7)


def test_crps_samples_known_values() -> None:
    assert crps_samples(np.array([0.0]), 1.0) == pytest.approx(1.0)  # point forecast: |x - y|
    assert crps_samples(np.array([0.0, 1.0]), 0.0) == pytest.approx(0.25)
    assert crps_samples(np.array([0.0, 1.0]), 0.5) == pytest.approx(0.25)
    with pytest.raises(ValueError):
        crps_samples(np.array([]), 0.0)


def test_crps_samples_matches_brute_force() -> None:
    rng = np.random.default_rng(7)
    x = rng.normal(size=57)
    for y in (-0.8, 0.1, 3.0):
        brute = np.mean(np.abs(x - y)) - 0.5 * np.mean(np.abs(x[:, None] - x[None, :]))
        assert crps_samples(x, y) == pytest.approx(brute, rel=1e-12)

        def ecdf(t: float) -> float:
            return float(np.mean(x <= t))

        pts = np.sort(np.concatenate([x, [y]]))
        integral = 0.0
        for a, b in itertools.pairwise(pts):
            mid = (a + b) / 2
            integral += (ecdf(mid) - (1.0 if mid >= y else 0.0)) ** 2 * (b - a)
        assert crps_samples(x, y) == pytest.approx(integral, rel=1e-10)


def test_crps_quantiles() -> None:
    assert crps_quantiles(np.array([0.5]), np.array([2.0]), 5.0) == pytest.approx(3.0)  # median: |q - y|
    assert quantile_weights(np.array([0.1, 0.5, 0.9])) == pytest.approx([0.3, 0.4, 0.3])
    levels = (np.arange(999) + 0.5) / 999
    q = sps.norm.ppf(levels)
    for y in (-1.0, 0.0, 0.7):
        assert crps_quantiles(levels, q, y) == pytest.approx(float(crps_normal(0.0, 1.0, y)), rel=2e-3)
    with pytest.raises(ValueError):
        crps_quantiles(np.array([0.5]), np.array([1.0, 2.0]), 0.0)


def test_crps_is_proper_for_samples() -> None:
    """Averaged over outcomes from N(0, 1), the true distribution's CRPS beats a shifted or a too-narrow one."""
    rng = np.random.default_rng(3)
    ys = rng.normal(size=4000)
    true = float(np.mean(crps_normal(0.0, 1.0, ys)))
    assert true < float(np.mean(crps_normal(0.3, 1.0, ys)))
    assert true < float(np.mean(crps_normal(0.0, 0.6, ys)))
