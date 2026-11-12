"""`queuetruth.stats.intervals`: the named variants agree with the formulas they replaced, bit for bit."""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats as sps

from queuetruth import stats
from queuetruth.stats.intervals import Z95


def test_z95_is_the_normal_quantile() -> None:
    assert float(sps.norm.ppf(0.975)) == Z95


@pytest.mark.parametrize(("k", "n"), [(0, 10), (10, 10), (3, 7), (0, 1), (1, 1), (999, 1000), (2.5, 4.0)])
def test_wilson_edges(k: float, n: float) -> None:
    raw = stats.wilson(k, n, edges="none")
    assert raw is not None
    lo, hi = stats.wilson_arrays(np.float64(k / n), np.float64(n))
    assert raw == (float(lo), float(hi))
    assert stats.wilson(k, n, edges="clip") == (max(0.0, raw[0]), min(1.0, raw[1]))
    exact = stats.wilson(k, n)
    assert exact is not None
    assert exact[0] == (0.0 if k <= 0 else max(0.0, raw[0]))
    assert exact[1] == (1.0 if k >= n else min(1.0, raw[1]))
    assert 0.0 <= exact[0] <= k / n <= exact[1] <= 1.0


def test_wilson_without_trials() -> None:
    assert stats.wilson(0, 0) == (0.0, 1.0)
    assert stats.wilson(0, 0, edges="clip") is None
    assert stats.clopper_pearson(0, 0) == (None, None)


def test_clopper_pearson_contains_wilson_centre() -> None:
    lo, hi = stats.clopper_pearson(3, 7)
    assert lo is not None and hi is not None
    assert lo < 3 / 7 < hi
    assert stats.clopper_pearson(0, 5)[0] == 0.0
    assert stats.clopper_pearson(5, 5)[1] == 1.0


def test_ratio_replicates_match_the_inline_forms() -> None:
    rng = np.random.default_rng(3)
    for nb in (1, 2, 9, 40):
        counts = stats.block_counts(nb, 500, int(rng.integers(1 << 30))).astype(float)
        num = rng.random(nb)
        den = rng.random(nb) * (rng.random(nb) < 0.7)
        rn, rd = counts @ num, counts @ den
        want = np.where(rd > 0, rn / np.where(rd > 0, rd, 1.0), np.nan)
        assert np.array_equal(stats.ratio_replicates(counts, num, den), want, equal_nan=True)
        rn = (counts * num[None, :]).sum(axis=1)
        rd = (counts * den[None, :]).sum(axis=1)
        want = np.where(rd > 0, rn / np.where(rd > 0, rd, 1.0), np.nan)
        got = stats.ratio_replicates(counts, num, den, summation="elementwise")
        assert np.array_equal(got, want, equal_nan=True)


def test_percentile_ci_is_numpy_percentile() -> None:
    r = np.random.default_rng(5).random(2001)
    r[::7] = np.nan
    lo, hi = stats.percentile_ci(r, 0.05)
    want = np.percentile(r[np.isfinite(r)], [2.5, 97.5])
    assert (lo, hi) == (float(want[0]), float(want[1]))
    assert all(math.isnan(x) for x in stats.percentile_ci(np.full(3, np.nan)))


@pytest.mark.parametrize(("nb", "b", "cells"), [(6, 300, 4_000_000), (6, 301, 25), (1, 10, 1), (50, 7, 60)])
def test_block_counts_chunks_concatenate_to_block_counts(nb: int, b: int, cells: int) -> None:
    got = np.concatenate(list(stats.block_counts_chunks(nb, b, 13, cells)))
    assert np.array_equal(got, stats.block_counts(nb, b, 13))
    assert list(stats.block_counts_chunks(nb, 0, 13)) == []
