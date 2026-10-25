"""Diebold-Mariano (HAC, HLN) and the question-clustered bootstrap: known values, a statsmodels cross-check, size
under the null and cluster awareness."""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats as sps

from queuetruth.forecast.inference import (
    cluster_index,
    design_effect,
    dm_test,
    long_run_variance,
    mean_cluster_ci,
    newey_west_lag,
    paired_cluster_bootstrap,
)


def test_dm_lag0_is_the_one_sample_t_test() -> None:
    d = np.array([0.3, -0.1, 0.25, 0.4, 0.05, -0.2, 0.15])
    r = dm_test(d, lag=0)
    t = sps.ttest_1samp(d, 0.0)
    assert r.statistic == pytest.approx(t.statistic, rel=1e-12)
    assert r.p_value == pytest.approx(t.pvalue, rel=1e-10)
    assert r.lag == 0


def test_dm_known_value_with_lag() -> None:
    d = np.array([1.0, 2.0, 0.0, 3.0, 1.0, 2.0])
    n, dbar = 6, 1.5
    e = d - dbar
    g0 = float(e @ e) / n
    g1 = float(e[1:] @ e[:-1]) / n
    lrv = g0 + 2 * 0.5 * g1  # Bartlett weight 1 - 1/2
    assert long_run_variance(d, 1) == pytest.approx(lrv)
    h = 2
    k = math.sqrt((n + 1 - 2 * h + h * (h - 1) / n) / n)
    want = k * dbar / math.sqrt(lrv / n)
    r = dm_test(d, lag=1)
    assert r.statistic == pytest.approx(want, rel=1e-12)
    assert r.p_value == pytest.approx(2 * sps.t.sf(abs(want), n - 1), rel=1e-12)


def test_dm_variance_matches_statsmodels_hac() -> None:
    sm = pytest.importorskip("statsmodels.api")
    rng = np.random.default_rng(11)
    e = rng.normal(size=300)
    d = 0.1 + e + 0.6 * np.concatenate([[0.0], e[:-1]])  # MA(1): serially correlated
    for lag in (1, 3, 6):
        fit = sm.OLS(d, np.ones_like(d)).fit(cov_type="HAC", cov_kwds={"maxlags": lag, "use_correction": False})
        var_mean = long_run_variance(d, lag) / d.size
        assert float(fit.bse[0]) ** 2 == pytest.approx(var_mean, rel=1e-10)


def test_dm_edge_cases() -> None:
    assert math.isnan(dm_test(np.array([1.0])).p_value)
    assert math.isnan(dm_test(np.array([1.0, np.nan, 2.0])).statistic)
    zero = dm_test(np.zeros(10), lag=0)
    assert (zero.statistic, zero.p_value) == (0.0, 1.0)
    const = dm_test(np.full(10, 0.2), lag=0)  # constant non-zero differences: no spread, no p-value
    assert math.isnan(const.statistic) and math.isnan(const.p_value)
    assert math.isnan(dm_test(np.full(10, -0.2), lag=0).p_value)
    with pytest.raises(ValueError):
        dm_test(np.ones(5), lag=-1)
    assert dm_test(np.arange(5.0), lag=50).lag == 4  # capped at n - 1


@pytest.mark.parametrize(("n", "lag"), [(0, 0), (1, 1), (99, 3), (100, 4), (1000, 6), (10_000, 11)])
def test_newey_west_rule(n: int, lag: int) -> None:
    assert newey_west_lag(n) == lag
    if n >= 2:
        assert dm_test(np.arange(float(n)) % 3).lag == lag


def test_dm_size_under_the_null() -> None:
    """Rejection rate at 5% over 600 null samples (serially correlated, n = 120) stays within binomial bounds."""
    rng = np.random.default_rng(2026)
    rejections = 0
    sims = 600
    for _ in range(sims):
        e = rng.normal(size=121)
        d = e[1:] + 0.5 * e[:-1]
        rejections += dm_test(d).p_value < 0.05
    rate = rejections / sims
    assert 0.025 <= rate <= 0.085, rate


def test_cluster_index() -> None:
    inv, g = cluster_index(["b", "a", "b", "c"])
    assert g == 3 and inv.tolist() == [1, 0, 1, 2]


def test_paired_bootstrap_point_estimates_and_determinism() -> None:
    ls = np.array([0.1, 0.2, 0.3, 0.4])
    lr = np.array([0.2, 0.2, 0.5, 0.4])
    cl = np.array([0, 0, 1, 2])
    a = paired_cluster_bootstrap(ls, lr, cl, 500, 7, "t")
    assert a.mean_diff == pytest.approx(0.3 / 4)
    assert a.skill == pytest.approx(1 - 1.0 / 1.3)
    assert (a.n, a.n_clusters) == (4, 3)
    b = paired_cluster_bootstrap(ls, lr, cl, 500, 7, "t")
    assert a == b
    c = paired_cluster_bootstrap(ls, lr, cl, 500, 7, "other test id")
    assert c.se != a.se  # its own random stream
    assert a.mean_diff_ci[0] <= a.mean_diff <= a.mean_diff_ci[1]
    one = paired_cluster_bootstrap(ls, lr, np.zeros(4, dtype=np.int64), 500, 7, "t")
    assert math.isnan(one.mean_diff_ci[0]) and math.isnan(one.p_value)
    empty = paired_cluster_bootstrap(np.array([]), np.array([]), np.array([], dtype=np.int64), 10, 0, "t")
    assert empty.n == 0 and math.isnan(empty.mean_diff)


def test_cluster_bootstrap_widens_for_correlated_clusters() -> None:
    """20 questions x 10 near-identical rows: the cluster CI reflects 20 units, the row CI 200."""
    rng = np.random.default_rng(5)
    base = rng.normal(size=20)
    d = np.repeat(base, 10) + 0.01 * rng.normal(size=200)
    by_question = np.repeat(np.arange(20), 10)
    by_row = np.arange(200)
    cl = paired_cluster_bootstrap(np.zeros(200), d, by_question, 2000, 1, "t")
    rw = paired_cluster_bootstrap(np.zeros(200), d, by_row, 2000, 1, "t")
    width_cl = cl.mean_diff_ci[1] - cl.mean_diff_ci[0]
    width_rw = rw.mean_diff_ci[1] - rw.mean_diff_ci[0]
    assert width_cl / width_rw == pytest.approx(math.sqrt(10), rel=0.25)
    assert design_effect(d, by_question) == pytest.approx(10, rel=0.1)
    assert design_effect(d, by_row) == 1.0


def test_mean_cluster_ci() -> None:
    est, lo, hi = mean_cluster_ci(np.array([1.0, 2.0, 3.0, 4.0]), np.array([0, 1, 2, 3]), 1000, 0, "m")
    assert est == 2.5 and lo < est < hi
    assert all(math.isnan(x) for x in mean_cluster_ci(np.array([]), np.array([], dtype=np.int64), 10, 0, "m"))
    est1, lo1, _ = mean_cluster_ci(np.array([1.0, 3.0]), np.array([0, 0]), 10, 0, "m")
    assert est1 == 2.0 and math.isnan(lo1)


def test_bootstrap_ci_coverage_on_known_mean() -> None:
    """Percentile CI of a clustered mean covers the true mean in roughly 95% of 200 repetitions."""
    rng = np.random.default_rng(9)
    cover = 0
    for _ in range(200):
        cl = np.repeat(np.arange(60), 3)
        x = np.repeat(rng.normal(size=60), 3) + rng.normal(size=180)
        _, lo, hi = mean_cluster_ci(x, cl, 400, int(rng.integers(1 << 30)), "c")
        cover += lo <= 0.0 <= hi
    assert 0.88 <= cover / 200 <= 0.99


@pytest.mark.parametrize(("g", "b"), [(1, 5), (7, 13), (999, 101), (3001, 40)])
def test_chunked_counts_equal_block_counts(g: int, b: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from queuetruth.forecast import inference
    from queuetruth.stats import block_counts

    monkeypatch.setattr(inference, "CHUNK_CELLS", 5000)  # force several chunks
    got = np.vstack(list(inference.cluster_count_chunks(g, b, 42)))
    want = block_counts(g, b, 42)
    assert got.shape == (b, g)
    assert np.array_equal(got, want.astype(float))
    assert list(inference.cluster_count_chunks(g, 0, 42)) == []


def test_dm_with_singleton_clusters_is_the_row_test() -> None:
    rng = np.random.default_rng(3)
    d = rng.normal(0.1, 1.0, size=50)
    row = dm_test(d, lag=2)
    cl = dm_test(d, lag=2, clusters=np.arange(50)[::-1].copy())
    assert (cl.statistic, cl.p_value, cl.lag, cl.units) == (row.statistic, row.p_value, row.lag, 50)


def test_dm_on_clusters_at_lag0_is_cr1() -> None:
    """Several rows per cluster: the test runs on cluster sums of the deviations, which at lag 0 is the CR1
    cluster-robust t test with G - 1 df."""
    d = np.array([0.3, 0.1, -0.2, 0.5, 0.4, 0.0, -0.1, 0.25, 0.6, -0.3])
    cl = np.array([0, 0, 1, 1, 1, 2, 3, 3, 4, 4])
    n, g = d.size, 5
    u = np.bincount(cl, weights=d - d.mean(), minlength=g)
    var = g / (g - 1) * float(u @ u) / n**2
    t = d.mean() / math.sqrt(var)
    r = dm_test(d, lag=0, clusters=cl)
    assert r.units == g and r.lag == 0
    assert r.statistic == pytest.approx(t, rel=1e-12)
    assert r.p_value == pytest.approx(2 * sps.t.sf(abs(t), g - 1), rel=1e-12)
    assert dm_test(d, clusters=cl).lag == newey_west_lag(g)  # the default lag counts clusters, not rows
    with pytest.raises(ValueError):
        dm_test(d, lag=0, clusters=cl[:-1])


def test_dm_size_under_a_grouped_null() -> None:
    """12 groups of 8 rows that share one shock (questions about one event): the row-level test treats 96 rows as
    independent and rejects far too often; the test on cluster sums keeps its level."""
    rng = np.random.default_rng(12)
    sims, g, k = 2000, 12, 8
    cl = np.repeat(np.arange(g), k)
    rej_row = rej_cl = 0
    for _ in range(sims):
        d = np.repeat(rng.normal(size=g), k) + 0.1 * rng.normal(size=g * k)
        rej_row += dm_test(d).p_value < 0.05
        rej_cl += dm_test(d, clusters=cl).p_value < 0.05
    assert rej_row / sims > 0.15, rej_row / sims  # 23% here, even with the HAC lags spanning neighbours
    assert 0.01 <= rej_cl / sims <= 0.08, rej_cl / sims
