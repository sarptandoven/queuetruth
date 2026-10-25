"""Selection-aware statistics (`queuetruth.stats.selection`) against closed forms, published worked examples and the
reference implementation `arch` 8.0.0 run on the same bootstrap draws.

References:
- Bailey, D. H. and Lopez de Prado, M. (2012), "The Sharpe ratio efficient frontier", Journal of Risk 15(2) (PSR,
  MinTRL); (2014) "The deflated Sharpe ratio", Journal of Portfolio Management 40(5), section "A numerical example"
  (SR 2.5 annualized, T = 1250 daily returns, N = 100 trials, V = 1/2 annualized, skewness -3, kurtosis 10 ->
  SR0 = 0.1132 per day, DSR = 0.9004).
- Lo, A. W. (2002), "The statistics of Sharpe ratios", Financial Analysts Journal 58(4): iid normal standard error
  `sqrt((1 + SR^2 / 2) / n)`; Mertens (2002) for the skewness / kurtosis terms.
- Bailey, Borwein, Lopez de Prado and Zhu (2017), "The probability of backtest overfitting", Journal of
  Computational Finance 20(4): CSCV, logits, PBO = share of logits <= 0.
- Hansen, P. R. (2005), "A test for superior predictive ability", JBES 23(4); Romano, J. P. and Wolf, M. (2005),
  "Stepwise multiple testing as formalized data snooping", Econometrica 73(4); Hansen, Lunde and Nason (2011),
  "The model confidence set", Econometrica 79(2). `ARCH_FIXTURE` holds `arch.bootstrap.MCS` / `SPA` / `StepM`
  outputs computed in a throwaway venv (arch 8.0.0) with its bootstrap replaced by `bootstrap_indices(120, 200,
  block_size=5, seed=3, test_id="arch_fixture")` on `_arch_case()` (docs/concepts/selection-bias.md).
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import integrate
from scipy import stats as sps

from queuetruth.stats import block_counts, bootstrap_seed
from queuetruth.stats import selection as sel

ARCH_FIXTURE = {
    "mcs_R": [1.0, 0.225, 0.045, 0.0, 0.0],
    "mcs_max": [1.0, 0.225, 0.05, 0.0, 0.0],
    "spa": {"lower": 0.125, "consistent": 0.195, "upper": 0.325},
    "spa_var": [0.4145779518762353, 0.5125948839831911, 0.521250621557897, 0.4901933728422736],
    "stepm_0.05": [],
    "stepm_0.2": [0],
}


def _arch_case() -> np.ndarray:
    rng = np.random.default_rng(42)
    n, m = 120, 5
    e = rng.standard_normal((n, m))
    for t in range(1, n):
        e[t] += 0.4 * e[t - 1]
    return 1.0 + np.array([0.0, 0.03, 0.08, 0.25, 0.5]) + 0.4 * e


def _arch_indices() -> np.ndarray:
    return sel.bootstrap_indices(120, 200, block_size=5, kind="stationary", seed=3, test_id="arch_fixture")


# ---- Sharpe ratio family ----------------------------------------------------------------------------------------


def test_dsr_published_worked_example() -> None:
    # Bailey and Lopez de Prado (2014), numerical example: daily data, 250 days a year
    sr = 2.5 / math.sqrt(250)
    sr0 = sel.expected_max_sharpe(100, 0.5 / 250)
    assert sr0 == pytest.approx(0.1132, abs=5e-5)
    dsr = sel.deflated_sharpe_ratio(sr, 1250, n_trials=100, var_sr=0.5 / 250, skew=-3.0, kurtosis=10.0)
    assert dsr == pytest.approx(0.9004, abs=5e-5)
    # DSR is the PSR against SR0
    assert dsr == pytest.approx(sel.probabilistic_sharpe_ratio(sr, 1250, skew=-3.0, kurtosis=10.0, sr_benchmark=sr0))


def test_sharpe_std_reduces_to_lo_2002_for_normal_returns() -> None:
    for sr, n in ((0.0, 10), (0.1, 250), (0.5, 60), (-0.3, 1000)):
        assert sel.sharpe_ratio_std(sr, n) == pytest.approx(math.sqrt((1 + sr * sr / 2) / (n - 1)), rel=1e-12)


def test_sharpe_std_matches_monte_carlo_for_skewed_fat_tailed_returns() -> None:
    # centred exponential + drift: skewness 2, kurtosis 9 (Pearson); empirical sd of the estimator vs Mertens
    rng = np.random.default_rng(11)
    n, sims, mu = 400, 4000, 0.15
    x = rng.exponential(1.0, size=(sims, n)) - 1.0 + mu
    est = x.mean(axis=1) / x.std(axis=1, ddof=1)
    true_sr = mu / 1.0
    expected = sel.sharpe_ratio_std(true_sr, n, skew=2.0, kurtosis=9.0)
    assert float(np.std(est)) == pytest.approx(expected, rel=0.05)
    # the normal formula is visibly wrong here, so the test can tell them apart
    assert abs(float(np.std(est)) - sel.sharpe_ratio_std(true_sr, n)) / expected > 0.1


def test_psr_basic_properties() -> None:
    assert sel.probabilistic_sharpe_ratio(0.1, 100, sr_benchmark=0.1) == pytest.approx(0.5)
    a = sel.probabilistic_sharpe_ratio(0.1, 250)
    assert a == pytest.approx(sps.norm.cdf(0.1 / math.sqrt((1 + 0.005) / 249)))
    assert sel.probabilistic_sharpe_ratio(0.1, 250, sr_benchmark=0.05) < a
    assert sel.probabilistic_sharpe_ratio(0.1, 500) > a  # longer track record, more confidence
    assert sel.probabilistic_sharpe_ratio(0.1, 250, skew=-2.0) < a  # negative skew hurts a positive Sharpe
    assert sel.probabilistic_sharpe_ratio(0.1, 250, kurtosis=12.0) < a  # fat tails hurt
    # symmetric about the benchmark
    assert sel.probabilistic_sharpe_ratio(-0.1, 250) == pytest.approx(1 - a)


def test_min_track_record_length_inverts_psr() -> None:
    for sr, g3, g4, alpha in ((0.1, 0.0, 3.0, 0.05), (0.2, -1.0, 6.0, 0.05), (0.08, 0.5, 4.0, 0.01)):
        n = sel.min_track_record_length(sr, skew=g3, kurtosis=g4, alpha=alpha)
        assert sel.probabilistic_sharpe_ratio(sr, n, skew=g3, kurtosis=g4) == pytest.approx(1 - alpha, abs=1e-12)
    assert sel.min_track_record_length(0.1, sr_benchmark=0.1) == math.inf
    assert sel.min_track_record_length(-0.1) == math.inf


def test_expected_max_sharpe_matches_the_exact_normal_maximum() -> None:
    # E[max of N iid N(0, 1)] by quadrature; the extreme-value approximation overstates it slightly, by a share that
    # shrinks with N (measured: +2.33% at N = 10, +0.92% at 100, +0.42% at 1000, +0.23% at 10000)
    prev = math.inf
    for n, bound in ((10, 0.025), (100, 0.01), (1000, 0.005), (10000, 0.0025)):

        def dens(x: float, n: int = n) -> float:
            return x * n * sps.norm.pdf(x) * sps.norm.cdf(x) ** (n - 1)

        exact = integrate.quad(dens, -10, 10, limit=200)[0]
        rel = sel.expected_max_sharpe(n, 1.0) / exact - 1.0
        assert 0.0 < rel < bound and rel < prev
        prev = rel
    assert sel.expected_max_sharpe(100, 4.0) == pytest.approx(2.0 * sel.expected_max_sharpe(100, 1.0))
    assert sel.expected_max_sharpe(100, 1.0, mean_sr=0.5) == pytest.approx(0.5 + sel.expected_max_sharpe(100, 1.0))
    assert sel.expected_max_sharpe(1, 3.0, mean_sr=0.2) == 0.2  # no selection
    assert sel.expected_max_sharpe(50, 0.0) == 0.0


def test_dsr_one_trial_is_psr_and_more_trials_deflate() -> None:
    assert sel.deflated_sharpe_ratio(0.1, 250, n_trials=1, var_sr=0.01) == pytest.approx(
        sel.probabilistic_sharpe_ratio(0.1, 250)
    )
    vals = [sel.deflated_sharpe_ratio(0.1, 250, n_trials=k, var_sr=0.001) for k in (1, 10, 100, 1000)]
    assert vals == sorted(vals, reverse=True)


def test_from_returns_use_sample_moments() -> None:
    rng = np.random.default_rng(5)
    r = rng.standard_t(5, size=500) * 0.01 + 0.001
    sr, n, g3, g4 = sel.sharpe_moments(r)
    assert sr == pytest.approx(r.mean() / r.std(ddof=1))
    assert (n, g3, g4) == (500, pytest.approx(sps.skew(r)), pytest.approx(sps.kurtosis(r, fisher=False)))
    assert sel.psr_from_returns(r) == pytest.approx(sel.probabilistic_sharpe_ratio(sr, n, skew=g3, kurtosis=g4))
    trials = np.array([sr, 0.01, -0.02, 0.03, 0.0])
    assert sel.dsr_from_returns(r, trials) == pytest.approx(
        sel.deflated_sharpe_ratio(sr, n, n_trials=5, var_sr=float(np.var(trials, ddof=1)), skew=g3, kurtosis=g4)
    )
    assert sel.dsr_from_returns(r, [sr]) == pytest.approx(sel.psr_from_returns(r))
    # trials without a series (a ledger's n_trials) deflate further; V still comes from the Sharpes given
    assert sel.dsr_from_returns(r, trials, n_trials=40) == pytest.approx(
        sel.deflated_sharpe_ratio(sr, n, n_trials=40, var_sr=float(np.var(trials, ddof=1)), skew=g3, kurtosis=g4)
    )
    assert sel.dsr_from_returns(r, trials, n_trials=40) < sel.dsr_from_returns(r, trials)
    assert sel.dsr_from_returns(r, trials, n_trials=5) == sel.dsr_from_returns(r, trials)
    with pytest.raises(ValueError, match="n_trials = 4 is below the 5 trial Sharpes"):
        sel.dsr_from_returns(r, trials, n_trials=4)


def test_constant_series() -> None:
    assert sel.sharpe_ratio(np.zeros(5)) == 0.0
    assert sel.sharpe_ratio(np.full(5, 0.01)) == math.inf
    assert sel.sharpe_ratio(np.full(5, -0.01)) == -math.inf
    assert sel.sharpe_moments(np.zeros(4)) == (0.0, 4, 0.0, 3.0)
    assert sel.psr_from_returns(np.full(5, 0.01)) == 1.0 and sel.psr_from_returns(np.full(5, -0.01)) == 0.0
    assert sel.psr_from_returns(np.zeros(5)) == 0.5
    with pytest.raises(ValueError, match="NaN"):
        sel.probabilistic_sharpe_ratio(math.nan, 10)


def test_sharpe_input_errors() -> None:
    with pytest.raises(ValueError, match="two returns"):
        sel.sharpe_ratio(np.array([0.1]))
    with pytest.raises(ValueError, match="finite"):
        sel.sharpe_ratio(np.array([0.1, np.nan]))
    with pytest.raises(ValueError, match="n >= 2"):
        sel.sharpe_ratio_std(0.1, 1)
    with pytest.raises(ValueError, match="inconsistent moments"):
        sel.probabilistic_sharpe_ratio(1.0, 100, skew=5.0, kurtosis=1.0)
    with pytest.raises(ValueError, match="inconsistent moments"):
        sel.min_track_record_length(1.0, skew=5.0, kurtosis=1.0)
    with pytest.raises(ValueError, match="n_trials"):
        sel.expected_max_sharpe(0, 1.0)
    with pytest.raises(ValueError, match="var_sr"):
        sel.expected_max_sharpe(5, -1.0)
    with pytest.raises(ValueError, match="alpha"):
        sel.min_track_record_length(0.1, alpha=1.0)
    with pytest.raises(ValueError, match="trial_sharpes"):
        sel.dsr_from_returns(np.array([0.1, 0.2]), [])


# ---- PBO / CSCV -------------------------------------------------------------------------------------------------


def _pbo_reference(x: np.ndarray, s: int, metric: str) -> np.ndarray:
    """Direct re-implementation of the paper's algorithm (no sufficient statistics) for comparison."""
    from itertools import combinations

    parts = np.array_split(np.arange(x.shape[0]), s)
    out = []
    for ins in combinations(range(s), s // 2):
        outs = [i for i in range(s) if i not in ins]
        a = x[np.concatenate([parts[i] for i in ins])]
        b = x[np.concatenate([parts[i] for i in outs])]
        if metric == "mean":
            pa_, pb = a.mean(0), b.mean(0)
        else:
            pa_, pb = a.mean(0) / a.std(0, ddof=1), b.mean(0) / b.std(0, ddof=1)
        best = int(np.argmax(pa_))
        rank = sps.rankdata(pb)[best]
        w = rank / (x.shape[1] + 1)
        out.append(math.log(w / (1 - w)))
    return np.array(out)


@pytest.mark.parametrize("metric", ["sharpe", "mean"])
def test_pbo_logits_equal_direct_algorithm(metric: str) -> None:
    rng = np.random.default_rng(3)
    x = rng.standard_normal((203, 7)) * 0.01 + rng.normal(0, 0.002, size=7)
    res = sel.probability_of_backtest_overfitting(x, n_splits=8, metric=metric)
    ref = _pbo_reference(x, 8, metric)
    assert res.n_combinations == math.comb(8, 4) == ref.size
    np.testing.assert_allclose(res.logits, ref, rtol=1e-12, atol=1e-12)
    assert res.pbo == pytest.approx(float(np.mean(ref <= 0)))


def test_pbo_callable_metric_equals_builtin() -> None:
    rng = np.random.default_rng(4)
    x = rng.standard_normal((100, 5))
    a = sel.probability_of_backtest_overfitting(x, n_splits=6, metric="mean")
    b = sel.probability_of_backtest_overfitting(x, n_splits=6, metric=lambda rows: rows.mean(axis=0))
    np.testing.assert_allclose(a.logits, b.logits)
    assert a.pbo == b.pbo
    with pytest.raises(ValueError, match="one performance per trial"):
        sel.probability_of_backtest_overfitting(x, n_splits=6, metric=lambda rows: rows.mean())


def test_pbo_rejects_a_nan_performance() -> None:
    # a user Sharpe of a trial that never trades is 0/0: argmax would select it and its NaN logit would count as
    # "not overfit" (PBO 0, the most favourable verdict); it is an error instead
    x = np.random.default_rng(4).standard_normal((100, 3))
    x[:, 1] = 0.0

    def sharpe(rows: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            return rows.mean(axis=0) / rows.std(axis=0, ddof=1)

    with pytest.raises(ValueError, match="NaN performance"):
        sel.probability_of_backtest_overfitting(x, n_splits=6, metric=sharpe)
    # +-inf is a legitimate performance (a constant non-zero series): no error
    y = x.copy()
    y[:, 1] = 1.0
    assert sel.probability_of_backtest_overfitting(y, n_splits=6, metric=sharpe).pbo == 0.0


def test_pbo_reversal_is_one_and_persistence_is_zero() -> None:
    # two halves: block 1 means (3, 2, 1), block 2 means (1, 2, 3): the IS winner is always the OOS loser
    x = np.vstack([np.tile([3.0, 2.0, 1.0], (4, 1)), np.tile([1.0, 2.0, 3.0], (4, 1))])
    r = sel.probability_of_backtest_overfitting(x, n_splits=2, metric="mean")
    assert r.pbo == 1.0
    np.testing.assert_allclose(r.logits, [math.log(1 / 3)] * 2)  # rank 1 of 3 -> w = 1/4
    assert list(r.selected) == [0, 2]
    assert r.prob_oos_loss == 0.0
    assert math.isnan(r.degradation_slope)  # both splits select with IS performance 3: no line to fit
    # one dominant trial: logit ln(N) on every split, PBO 0
    rng = np.random.default_rng(9)
    y = rng.standard_normal((160, 6)) * 0.01
    y[:, 2] += 1.0
    q = sel.probability_of_backtest_overfitting(y, n_splits=8)
    assert q.pbo == 0.0 and (q.selected == 2).all()
    np.testing.assert_allclose(q.logits, math.log(6))
    assert q.prob_oos_loss == 0.0


def test_pbo_of_pure_noise_is_about_one_half() -> None:
    # skill-less trials: the IS winner's OOS rank is uniform, so E[PBO] = 1/2 for even N
    vals = [
        sel.probability_of_backtest_overfitting(np.random.default_rng(s).standard_normal((240, 10)), n_splits=8).pbo
        for s in range(40)
    ]
    assert float(np.mean(vals)) == pytest.approx(0.5, abs=0.08)


def test_pbo_degradation_line_and_ties() -> None:
    rng = np.random.default_rng(12)
    x = rng.standard_normal((120, 8))
    r = sel.probability_of_backtest_overfitting(x, n_splits=6, metric="mean")
    slope, intercept = np.polyfit(r.is_performance, r.oos_performance, 1)
    assert (r.degradation_slope, r.degradation_intercept) == (pytest.approx(slope), pytest.approx(intercept))
    assert r.prob_oos_loss == float(np.mean(r.oos_performance < 0))
    # identical trials tie in-sample: the first is selected, its OOS rank is the average rank
    z = np.repeat(rng.standard_normal((40, 1)), 3, axis=1)
    t = sel.probability_of_backtest_overfitting(z, n_splits=4, metric="mean")
    assert (t.selected == 0).all()
    np.testing.assert_allclose(t.logits, 0.0)  # average rank 2 of 3 -> w = 1/2
    assert t.pbo == 1.0


def test_pbo_input_errors() -> None:
    x = np.zeros((40, 3))
    with pytest.raises(ValueError, match="even"):
        sel.probability_of_backtest_overfitting(x, n_splits=5)
    with pytest.raises(ValueError, match="two trials"):
        sel.probability_of_backtest_overfitting(np.zeros((40, 1)))
    with pytest.raises(ValueError, match="too few"):
        sel.probability_of_backtest_overfitting(np.zeros((3, 3)), n_splits=2)
    with pytest.raises(ValueError, match="T x N"):
        sel.probability_of_backtest_overfitting(np.zeros(40))
    with pytest.raises(ValueError, match="finite"):
        sel.probability_of_backtest_overfitting(np.full((40, 3), np.inf), n_splits=4)
    with pytest.raises(ValueError, match="unknown metric"):
        sel.probability_of_backtest_overfitting(x, n_splits=4, metric="sortino")  # type: ignore[arg-type]


# ---- bootstrap draws --------------------------------------------------------------------------------------------


def test_bootstrap_indices_are_deterministic_and_well_formed() -> None:
    a = sel.bootstrap_indices(50, 30, block_size=4, seed=1, test_id="x")
    assert np.array_equal(a, sel.bootstrap_indices(50, 30, block_size=4, seed=1, test_id="x"))
    assert not np.array_equal(a, sel.bootstrap_indices(50, 30, block_size=4, seed=1, test_id="y"))
    assert a.shape == (30, 50) and a.min() >= 0 and a.max() < 50
    for kind in ("circular", "moving"):
        b = sel.bootstrap_indices(50, 30, block_size=4, kind=kind, seed=1)  # type: ignore[arg-type]
        assert b.shape == (30, 50) and b.min() >= 0 and b.max() < 50
        for row in b:  # blocks of 4 consecutive (wrapping for circular)
            for k in range(0, 48, 4):
                step = np.diff(row[k : k + 4]) % 50 if kind == "circular" else np.diff(row[k : k + 4])
                assert (step == 1).all()


def test_stationary_bootstrap_block_lengths_are_geometric() -> None:
    idx = sel.bootstrap_indices(2000, 50, block_size=8, seed=2)
    breaks = (np.diff(idx, axis=1) % 2000) != 1
    # a new block starts with probability 1/8 (a restart can also land on the next index, probability 1/n)
    assert float(breaks.mean()) == pytest.approx(1 / 8 * (1 - 1 / 2000), abs=0.01)
    iid = sel.bootstrap_indices(500, 20, block_size=1, seed=2)
    assert float(((np.diff(iid, axis=1) % 500) != 1).mean()) > 0.99


def test_bootstrap_draws_are_chunked_deterministically(monkeypatch: pytest.MonkeyPatch) -> None:
    # one chunk (b n <= CHUNK_ELEMENTS) is the single-call draw; smaller chunks are still deterministic and
    # well formed, and the MCS on them equals the MCS on the same indices passed explicitly
    full = sel.bootstrap_indices(60, 40, block_size=4, seed=2, test_id="c")
    assert sel._chunk_rows(40, 60) == 40
    monkeypatch.setattr(sel, "CHUNK_ELEMENTS", 600)  # 10 replicates of 60 rows per chunk
    assert sel._chunk_rows(40, 60) == 10 and sel._chunk_rows(40, 10_000) == 1
    small = sel.bootstrap_indices(60, 40, block_size=4, seed=2, test_id="c")
    assert small.shape == full.shape
    assert np.array_equal(small, sel.bootstrap_indices(60, 40, block_size=4, seed=2, test_id="c"))
    assert small.min() >= 0 and small.max() < 60
    x = np.random.default_rng(1).standard_normal((60, 3)) + np.array([0.0, 0.1, 0.5])
    a = sel.model_confidence_set(x, b=40, block_size=4, seed=2, test_id="c")
    b = sel.model_confidence_set(x, indices=small)
    np.testing.assert_array_equal(a.p_values, b.p_values)


def test_bootstrap_indices_errors() -> None:
    with pytest.raises(ValueError, match="unknown bootstrap"):
        sel.bootstrap_indices(10, 5, kind="wild")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="n >= 1"):
        sel.bootstrap_indices(0, 5)
    with pytest.raises(ValueError, match="block_size"):
        sel.bootstrap_indices(10, 5, block_size=0)


# ---- SPA and StepM ----------------------------------------------------------------------------------------------


def test_spa_matches_arch_on_the_same_draws() -> None:
    x = _arch_case()
    bench, models = x[:, 1], np.delete(x, 1, axis=1)
    r = sel.spa_test(bench, models, indices=_arch_indices(), block_size=5, studentize=False)
    assert (r.p_lower, r.p_consistent, r.p_upper) == (
        ARCH_FIXTURE["spa"]["lower"],
        ARCH_FIXTURE["spa"]["consistent"],
        ARCH_FIXTURE["spa"]["upper"],
    )
    np.testing.assert_allclose(r.omega**2, ARCH_FIXTURE["spa_var"], rtol=1e-12)


def test_spa_hand_computed_studentized() -> None:
    bench = np.array([1.0, 2.0, 0.5, 1.5, 1.0])
    models = np.array([[0.5, 2.5], [1.0, 2.0], [0.7, 0.1], [1.0, 1.0], [0.9, 1.5]])
    idx = np.array([[0, 1, 2, 3, 4], [4, 4, 0, 1, 1], [2, 3, 3, 3, 0]])
    r = sel.spa_test(bench, models, indices=idx, block_size=2)
    d = bench[:, None] - models
    n = 5
    dbar = d.mean(0)
    dm = d - dbar
    var = (dm**2).sum(0) / n
    q = 0.5
    for i in range(1, n):  # direct sum, independent of the FFT path
        kappa = (1 - i / n) * (1 - q) ** i + (i / n) * (1 - q) ** (n - i)
        var += 2 * kappa * (dm[: n - i] * dm[i:]).sum(0) / n
    omega = np.sqrt(var)
    t = math.sqrt(n) * dbar / omega
    np.testing.assert_allclose(r.omega, omega, rtol=1e-12)
    np.testing.assert_allclose(r.t, t, rtol=1e-12)
    assert r.statistic == pytest.approx(max(0.0, t.max()))
    star = np.stack([d[i].mean(0) for i in idx])
    thr = -np.sqrt(var / n * 2 * math.log(math.log(n)))
    for mu, p in (
        (np.maximum(dbar, 0), r.p_lower),
        (np.where(dbar >= thr, dbar, 0.0), r.p_consistent),
        (dbar, r.p_upper),
    ):
        t_star = np.maximum(0, (math.sqrt(n) * (star - mu) / omega).max(axis=1))
        assert p == pytest.approx(float(np.mean(t_star >= r.statistic)))


def test_spa_p_value_ordering_power_and_degenerate_cases() -> None:
    rng = np.random.default_rng(8)
    bench = rng.standard_normal(300) + 1.0
    models = rng.standard_normal((300, 6)) + 1.0
    models[:, 3] -= 0.4  # one genuinely better model
    r = sel.spa_test(bench, models, b=500, block_size=3, seed=1)
    assert r.p_lower <= r.p_consistent <= r.p_upper
    assert r.p_consistent < 0.01 and int(np.argmax(r.t)) == 3
    # every model identical to the benchmark: no evidence, p = 1 (zero variance reads t = 0)
    same = sel.spa_test(bench, np.repeat(bench[:, None], 3, axis=1), b=50)
    assert same.statistic == 0.0 and (same.p_lower, same.p_consistent, same.p_upper) == (1.0, 1.0, 1.0)
    assert (same.omega == 0).all()
    # a constant improvement has zero variance (up to rounding of bench - (bench - 0.1)) and an unbounded t:
    # rejected with p = 0; an exactly constant one reads t = inf
    better = sel.spa_test(bench, (bench - 0.1)[:, None], b=50)
    assert better.statistic > 1e12 and better.p_consistent == 0.0
    exact = sel.spa_test(np.full(300, 1.0), np.full((300, 1), 0.5), b=50)
    assert exact.statistic == math.inf and (exact.p_lower, exact.p_consistent, exact.p_upper) == (0.0, 0.0, 0.0)
    # deterministic in (seed, test_id)
    again = sel.spa_test(bench, models, b=500, block_size=3, seed=1)
    assert (again.p_lower, again.p_consistent, again.p_upper) == (r.p_lower, r.p_consistent, r.p_upper)


def test_spa_size_under_the_null() -> None:
    # all models as good as the benchmark (iid losses): rejection rate at 5% stays near 5%
    rej = 0
    sims = 200
    for s in range(sims):
        rng = np.random.default_rng(1000 + s)
        x = rng.standard_normal((100, 5))
        r = sel.spa_test(x[:, 0], x[:, 1:], b=199, block_size=1, seed=s)
        rej += r.p_consistent <= 0.05
    assert rej / sims <= 0.1


def test_spa_cluster_bootstrap_uses_block_counts() -> None:
    rng = np.random.default_rng(6)
    bench = rng.standard_normal(60) + 0.2
    models = rng.standard_normal((60, 2))
    blocks = np.repeat(np.arange(12), 5)
    r = sel.spa_test(bench, models, b=300, blocks=blocks, seed=4, test_id="spa_blocks")
    d = bench[:, None] - models
    counts = block_counts(12, 300, bootstrap_seed(4, "spa_blocks")).astype(float)
    sums = np.stack([d[blocks == k].sum(0) for k in range(12)])
    reps = counts @ sums / (counts @ np.full(12, 5.0))[:, None]
    omega = np.sqrt(60 * ((reps - d.mean(0)) ** 2).mean(0))
    np.testing.assert_allclose(r.omega, omega, rtol=1e-12)


def test_spa_input_errors() -> None:
    x = np.zeros((10, 2))
    with pytest.raises(ValueError, match="n x k"):
        sel.spa_test(np.zeros(9), x)
    with pytest.raises(ValueError, match="at least 3 rows"):
        sel.spa_test(np.zeros(2), np.zeros((2, 1)))
    with pytest.raises(ValueError, match="finite"):
        sel.spa_test(np.full(10, np.nan), x)
    with pytest.raises(ValueError, match="indices"):
        sel.spa_test(np.zeros(10), x, indices=np.zeros((3, 9), dtype=int))
    with pytest.raises(ValueError, match="out of range"):
        sel.spa_test(np.zeros(10), x, indices=np.full((3, 10), 10))
    with pytest.raises(ValueError, match="blocks or indices"):
        sel.spa_test(np.zeros(10), x, blocks=np.zeros(10), indices=np.zeros((3, 10), dtype=int))
    with pytest.raises(ValueError, match="one label per row"):
        sel.spa_test(np.zeros(10), x, blocks=np.zeros(9))
    with pytest.raises(ValueError, match="b >= 1"):
        sel.spa_test(np.zeros(10), x, b=0)


def test_stepm_matches_arch_on_the_same_draws() -> None:
    x = _arch_case()
    bench, models = x[:, 1], np.delete(x, 1, axis=1)
    for alpha, key in ((0.05, "stepm_0.05"), (0.2, "stepm_0.2")):
        r = sel.stepm(
            bench, models, alpha=alpha, indices=_arch_indices(), block_size=5, studentize=False, recenter="consistent"
        )
        assert list(r.superior) == ARCH_FIXTURE[key]


def test_stepm_steps_down() -> None:
    # model 0 far better, model 1 with t = 1.8: above the one-model 95% quantile (~1.645) but below the two-model
    # one (~1.95): rejected only at the second step, after model 0 leaves the max
    rng = np.random.default_rng(21)
    n = 2000
    z = rng.standard_normal((n, 2))
    z = (z - z.mean(0)) / z.std(0)
    d = np.column_stack([z[:, 0] + 0.5, z[:, 1] + 1.8 / math.sqrt(n)])
    bench = np.zeros(n)
    r = sel.stepm(bench, -d, alpha=0.05, b=2000, block_size=1, seed=3)
    assert r.t[1] == pytest.approx(1.8, rel=1e-9)
    assert r.steps == ((0,), (1,)) and r.superior == (0, 1)
    assert r.critical_values[1] < 1.8 < r.critical_values[0]
    # FWER: with no superior model nothing is rejected
    none = sel.stepm(bench, rng.standard_normal((n, 4)), alpha=0.05, b=500, block_size=1)
    assert none.superior == () and len(none.critical_values) == 1


def test_stepm_errors() -> None:
    with pytest.raises(ValueError, match="alpha"):
        sel.stepm(np.zeros(5), np.zeros((5, 1)), alpha=0.0)
    with pytest.raises(ValueError, match="recenter"):
        sel.stepm(np.zeros(5), np.zeros((5, 1)), recenter="middle")  # type: ignore[arg-type]


# ---- model confidence set ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["R", "max"])
def test_mcs_matches_arch_on_the_same_draws(method: str) -> None:
    r = sel.model_confidence_set(_arch_case(), alpha=0.10, method=method, indices=_arch_indices())  # type: ignore[arg-type]
    np.testing.assert_allclose(r.p_values, ARCH_FIXTURE[f"mcs_{method}"], atol=1e-12)
    assert r.included == tuple(i for i, p in enumerate(ARCH_FIXTURE[f"mcs_{method}"]) if p > 0.10)
    assert r.eliminated[-1] == 0 and r.step_p[-1] == 1.0


def test_mcs_hand_computed_two_models() -> None:
    losses = np.array([[1.0, 2.0], [2.0, 2.5], [1.5, 1.0], [0.5, 2.0]])
    idx = np.array([[0, 1, 2, 3], [0, 0, 2, 2], [1, 3, 3, 1], [2, 2, 2, 0]])
    r = sel.model_confidence_set(losses, alpha=0.2, method="R", indices=idx)
    mean = losses.mean(0)
    reps = np.stack([losses[i].mean(0) for i in idx])
    diff = mean[1] - mean[0]
    cen = (reps[:, 1] - reps[:, 0]) - diff
    se = math.sqrt(float(np.mean(cen**2)))
    stat = diff / se
    sim = np.abs(cen) / se  # max over the ordered pairs (1, 0) and (0, 1)
    p = float(np.mean(sim >= stat))
    assert r.eliminated == (1, 0)
    assert r.p_values[1] == pytest.approx(p) and r.p_values[0] == 1.0
    assert r.included == ((0, 1) if p > 0.2 else (0,))
    # with two models T_max and T_R agree
    rm = sel.model_confidence_set(losses, alpha=0.2, method="max", indices=idx)
    np.testing.assert_allclose(rm.p_values, r.p_values)


@pytest.mark.parametrize("method", ["R", "max"])
def test_mcs_identical_and_dominated_models(method: str) -> None:
    rng = np.random.default_rng(13)
    base = rng.standard_normal(200)
    same = np.repeat(base[:, None], 4, axis=1)
    r = sel.model_confidence_set(same, method=method, b=100)  # type: ignore[arg-type]
    assert r.included == (0, 1, 2, 3) and (r.p_values == 1.0).all()
    # a constant extra loss has zero spread around its difference: eliminated with p = 0
    shifted = np.column_stack([base, base + 0.1, base])
    q = sel.model_confidence_set(shifted, method=method, b=100)  # type: ignore[arg-type]
    assert q.included == (0, 2) and q.p_values[1] == 0.0 and q.eliminated[0] == 1
    # clearly ranked models: only the best survives
    ranked = rng.standard_normal((300, 4)) * 0.2 + np.array([1.0, 2.0, 3.0, 4.0])
    s = sel.model_confidence_set(ranked, method=method, b=200)  # type: ignore[arg-type]
    assert s.included == (0,) and s.eliminated == (3, 2, 1, 0)
    np.testing.assert_allclose(s.mean_loss, ranked.mean(0))


def test_mcs_p_values_are_monotone_along_the_elimination_order() -> None:
    rng = np.random.default_rng(14)
    x = rng.standard_normal((150, 8)) + np.linspace(0, 0.4, 8)
    r = sel.model_confidence_set(x, b=300, block_size=3)
    ps = [r.p_values[i] for i in r.eliminated]
    assert ps == sorted(ps) and ps[-1] == 1.0
    assert r.step_p[-1] == 1.0 and len(r.step_p) == 8
    assert r.included == tuple(sorted(i for i in range(8) if r.p_values[i] > 0.10))


def test_mcs_coverage_when_all_models_are_equal() -> None:
    # equal expected losses: the whole set is retained with probability about 1 - alpha
    kept = 0
    sims = 120
    for s in range(sims):
        x = np.random.default_rng(500 + s).standard_normal((100, 4))
        kept += len(sel.model_confidence_set(x, alpha=0.10, b=199, block_size=1, seed=s).included) == 4
    assert kept / sims >= 0.82


def test_mcs_cluster_bootstrap_and_single_model() -> None:
    rng = np.random.default_rng(15)
    x = rng.standard_normal((80, 3)) + np.array([0.0, 0.05, 0.6])
    blocks = np.repeat(np.arange(16), 5)[::-1]  # labels need not be sorted or contiguous in value
    a = sel.model_confidence_set(x, blocks=blocks, b=400, seed=2, test_id="t")
    b = sel.model_confidence_set(x, blocks=blocks, b=400, seed=2, test_id="t")
    np.testing.assert_array_equal(a.p_values, b.p_values)
    assert 2 not in a.included and 0 in a.included
    one = sel.model_confidence_set(x[:, :1], b=10)
    assert one.included == (0,) and one.p_values.tolist() == [1.0] and one.eliminated == (0,)


def test_mcs_input_errors() -> None:
    with pytest.raises(ValueError, match="n x m"):
        sel.model_confidence_set(np.zeros(5))
    with pytest.raises(ValueError, match="n x m"):
        sel.model_confidence_set(np.zeros((1, 3)))
    with pytest.raises(ValueError, match="finite"):
        sel.model_confidence_set(np.full((5, 2), np.nan))
    with pytest.raises(ValueError, match="alpha"):
        sel.model_confidence_set(np.zeros((5, 2)), alpha=1.0)
    with pytest.raises(ValueError, match="method"):
        sel.model_confidence_set(np.zeros((5, 2)), method="T")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="B >= 1"):
        sel.model_confidence_set(np.zeros((5, 2)), indices=np.zeros((0, 5), dtype=int))


# ---- MCS: wild cluster bootstrap-t ----------------------------------------------------------------------------------


def _mcs_wild_reference(
    x: np.ndarray, blocks: np.ndarray, b: int, seed: int, test_id: str, method: str
) -> tuple[np.ndarray, tuple[int, ...]]:
    """Direct loop implementation of the wild cluster bootstrap-t MCS (module docstring of `selection`)."""
    from queuetruth.stats import bootstrap_rng

    labs, g = np.unique(blocks, return_inverse=True)
    n_g, m = labs.size, x.shape[1]
    sums = np.stack([x[g == k].sum(0) for k in range(n_g)])
    sizes = np.array([(g == k).sum() for k in range(n_g)], dtype=float)
    total = sizes.sum()
    w = sel.WEBB_WEIGHTS[bootstrap_rng(seed, test_id).integers(0, 6, size=(b, n_g))]
    mean = sums.sum(0) / total
    e = sums - sizes[:, None] * mean

    def ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(den > 0, num / np.where(den > 0, den, 1), np.where(num == 0, 0.0, np.sign(num) * np.inf))

    def se_pairs(res: np.ndarray, idx: list[int]) -> np.ndarray:
        out = np.empty((len(idx), len(idx)))
        for a, i in enumerate(idx):
            for c, j in enumerate(idx):
                out[a, c] = math.sqrt(float(((res[:, i] - res[:, j]) ** 2).sum())) / total
        return out

    def se_dev(res: np.ndarray, idx: list[int]) -> np.ndarray:
        r = res[:, idx] - res[:, idx].mean(1, keepdims=True)
        return np.sqrt((r**2).sum(0)) / total

    active = list(range(m))
    order, step_p = [], []
    while len(active) > 1:
        sims = []
        if method == "R":
            diff = mean[active][:, None] - mean[active][None, :]
            t = ratio(diff, se_pairs(e, active))
            stat, worst = t.max(), active[int(np.argmax(t.max(1)))]
        else:
            d = mean[active] - mean[active].mean()
            t = ratio(d, se_dev(e, active))
            stat, worst = t.max(), active[int(np.argmax(t))]
        for r in range(b):
            delta = w[r] @ e / total
            es = w[r][:, None] * e - sizes[:, None] * delta
            if method == "R":
                num = delta[active][:, None] - delta[active][None, :]
                sims.append(ratio(num, se_pairs(es, active)).max())
            else:
                dd = delta[active] - delta[active].mean()
                sims.append(ratio(dd, se_dev(es, active)).max())
        step_p.append(float(np.mean(np.array(sims) >= stat)))
        order.append(worst)
        active.remove(worst)
    order.append(active[0])
    step_p.append(1.0)
    p = np.empty(m)
    run = 0.0
    for i, q in zip(order, step_p, strict=True):
        run = max(run, q)
        p[i] = run
    return p, tuple(order)


@pytest.mark.parametrize("method", ["R", "max"])
def test_mcs_wild_cluster_bootstrap_equals_a_direct_implementation(method: str) -> None:
    rng = np.random.default_rng(31)
    blocks = np.repeat([4, 9, 1, 7, 3, 8, 2], [9, 14, 6, 11, 8, 12, 10])  # 7 clusters of unequal size, unsorted
    x = rng.standard_normal((blocks.size, 4)) + np.array([0.0, 0.1, 0.3, 0.9])
    x[:, 3] += 0.5 * rng.standard_normal(7)[np.unique(blocks, return_inverse=True)[1]]
    r = sel.model_confidence_set(x, method=method, b=120, seed=4, test_id="wild", blocks=blocks)  # type: ignore[arg-type]
    p, order = _mcs_wild_reference(x, blocks, 120, 4, "wild", method)
    assert r.studentize == "wild" and r.eliminated == order
    np.testing.assert_allclose(r.p_values, p, atol=1e-12)
    # an explicit "fixed" is the Hansen-Lunde-Nason statistic on the pairs cluster bootstrap (block_counts)
    f = sel.model_confidence_set(
        x,
        method=method,  # type: ignore[arg-type]
        b=120,
        seed=4,
        test_id="wild",
        blocks=blocks,
        studentize="fixed",
    )
    assert f.studentize == "fixed" and not np.array_equal(f.p_values, r.p_values)


def test_wild_weights_are_webbs_six_points() -> None:
    w = sel.WEBB_WEIGHTS
    assert w.size == 6 and float(w.mean()) == pytest.approx(0.0, abs=1e-15)
    assert float((w**2).mean()) == pytest.approx(1.0) and float((w**4).mean()) == pytest.approx(7 / 6)


@pytest.mark.parametrize("studentize", ["fixed", "wild"])
def test_mcs_chunked_t_star_equals_the_cached_one(studentize: str, monkeypatch: pytest.MonkeyPatch) -> None:
    rng = np.random.default_rng(32)
    blocks = np.repeat(np.arange(10), 12)
    x = rng.standard_normal((120, 6)) + np.linspace(0.0, 0.5, 6)
    for method in ("R", "max"):
        kw = {"b": 200, "seed": 1, "blocks": blocks, "studentize": studentize, "method": method}
        cached = sel.model_confidence_set(x, **kw)  # type: ignore[arg-type]
        monkeypatch.setattr(sel, "CHUNK_ELEMENTS", 500)  # no cache, chunks of a few replicates, recomputed per step
        chunked = sel.model_confidence_set(x, **kw)  # type: ignore[arg-type]
        monkeypatch.undo()
        np.testing.assert_array_equal(cached.p_values, chunked.p_values)
        assert cached.eliminated == chunked.eliminated


def test_mcs_fixed_variance_separates_from_two_clusters_and_wild_does_not() -> None:
    # nearly equal models on two clusters: the fixed variance reads them as separated with p = 0; the wild
    # bootstrap-t keeps them tied (its coverage collapses only at 3 clusters, which callers must avoid)
    rng = np.random.default_rng(33)
    x = rng.standard_normal((300, 3)) + np.array([0.0, 0.001, 0.002])
    two = np.repeat([0, 1], 150)
    f = sel.model_confidence_set(x, b=400, blocks=two, studentize="fixed")
    assert len(f.included) == 1 and sorted(f.p_values)[:2] == [0.0, 0.0]
    r = sel.model_confidence_set(x, b=400, blocks=two)
    assert r.studentize == "wild" and r.included == (0, 1, 2)
    assert sel.model_confidence_set(x, b=400, blocks=np.repeat(np.arange(13), 24)[:300]).included == (0, 1, 2)


def test_mcs_wild_identical_and_dominated_models() -> None:
    rng = np.random.default_rng(35)
    blocks = np.repeat(np.arange(12), 25)
    base = rng.standard_normal(300)
    same = np.repeat(base[:, None], 3, axis=1)
    r = sel.model_confidence_set(same, b=100, blocks=blocks)
    assert r.included == (0, 1, 2) and (r.p_values == 1.0).all()
    ranked = rng.standard_normal((300, 3)) * 0.2 + np.array([1.0, 2.0, 3.0])
    s = sel.model_confidence_set(ranked, b=200, blocks=blocks)
    assert s.included == (0,) and s.eliminated == (2, 1, 0)


def test_cluster_bootstrap_needs_two_clusters() -> None:
    x = np.random.default_rng(34).standard_normal((50, 3))
    one = np.zeros(50)
    for call in (
        lambda: sel.model_confidence_set(x, blocks=one),
        lambda: sel.model_confidence_set(x, blocks=one, studentize="fixed"),
        lambda: sel.spa_test(x[:, 0], x[:, 1:], blocks=one),
        lambda: sel.stepm(x[:, 0], x[:, 1:], blocks=one),
    ):
        with pytest.raises(ValueError, match="at least 2 clusters, got 1"):
            call()
    with pytest.raises(ValueError, match="unknown studentize"):
        sel.model_confidence_set(x, studentize="hac")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="needs blocks="):
        sel.model_confidence_set(x, studentize="wild")
    # row-index bootstraps keep the fixed variance under "auto"
    assert sel.model_confidence_set(x, b=20).studentize == "fixed"
