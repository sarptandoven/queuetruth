"""O-STAT-1 (McNemar), O-STAT-2 (Wilcoxon) and the paired permutation test."""

from __future__ import annotations

import numpy as np
import pytest
from scipy import stats as sps
from statsmodels.stats.contingency_tables import mcnemar as sm_mcnemar

from queuetruth import stats


def test_o_stat_1_golden_b3_c12() -> None:
    r = stats.mcnemar(3, 12)
    assert r.p_exact == 0.03515625
    assert r.chi2_cc == 4.266666666666667
    assert r.p_chi2 == pytest.approx(0.038867103812417245, rel=1e-14)
    assert r.odds_ratio == 4.0
    assert r.or_ci == pytest.approx((1.1287601312649644, 14.174845086058559), rel=1e-12)


def test_o_stat_1_reference_tables(reference) -> None:
    doc = reference("mcnemar.json")
    assert len(doc["cases"]) >= 50
    for case in doc["cases"]:
        r = stats.mcnemar(case["b"], case["c"])
        spec = case["spec"]
        assert r.p_exact == pytest.approx(spec["p_exact"], rel=1e-13), case["name"]
        assert r.chi2_cc == pytest.approx(spec["chi2_cc"], rel=1e-14, abs=1e-300), case["name"]
        assert r.p_chi2 == pytest.approx(spec["p_chi2"], rel=1e-12), case["name"]
        if spec["odds_ratio"] is None:
            assert r.odds_ratio is None
        else:
            assert r.odds_ratio == pytest.approx(spec["odds_ratio"], rel=1e-15)
        if spec["or_ci_lo"] is None:
            assert r.or_ci is None
        else:
            assert r.or_ci == pytest.approx((spec["or_ci_lo"], spec["or_ci_hi"]), rel=1e-12)


def test_o_stat_1_statsmodels_50_random_tables() -> None:
    rng = np.random.default_rng(stats.stable_seed(0, [b"test", b"mcnemar"]))
    for _ in range(50):
        b, c = (int(v) for v in rng.integers(0, 60, size=2))
        n11, n00 = (int(v) for v in rng.integers(0, 100, size=2))
        table = [[n11, b], [c, n00]]
        r = stats.mcnemar(b, c)
        exact = sm_mcnemar(table, exact=True)
        approx = sm_mcnemar(table, exact=False, correction=True)
        assert r.p_exact == pytest.approx(exact.pvalue, rel=1e-12), (b, c)
        if b + c > 0:
            assert r.chi2_cc == pytest.approx(approx.statistic, rel=1e-14), (b, c)
            assert r.p_chi2 == pytest.approx(approx.pvalue, rel=1e-12), (b, c)


def test_o_stat_1_edge_cases() -> None:
    zero = stats.mcnemar(0, 0)
    assert (zero.chi2_cc, zero.p_chi2, zero.p_exact, zero.odds_ratio, zero.or_ci) == (0.0, 1.0, 1.0, None, None)
    only_c = stats.mcnemar(0, 7)
    assert only_c.odds_ratio is None and only_c.or_ci is None
    assert only_c.p_exact == pytest.approx(2 / 2**7)
    only_b = stats.mcnemar(5, 0)
    assert only_b.odds_ratio is None and only_b.or_ci is None
    big = stats.mcnemar(2400, 2600)  # scipy path above the exact-rational limit
    assert big.p_exact == pytest.approx(sps.binomtest(2400, 5000, 0.5).pvalue, rel=1e-9)
    with pytest.raises(ValueError):
        stats.mcnemar(-1, 2)


def test_o_stat_1_binom_exact_two_sided() -> None:
    assert stats.binom_exact_two_sided(3, 15) == 0.03515625
    assert stats.binom_exact_two_sided(5, 10) == 1.0
    assert stats.binom_exact_two_sided(0, 0) == 1.0
    for k, n, p in [(2, 20, 0.3), (17, 40, 0.25), (9, 9, 0.9)]:
        assert stats.binom_exact_two_sided(k, n, p) == pytest.approx(sps.binomtest(k, n, p).pvalue, rel=1e-12)


def test_o_stat_2_wilcoxon_reference(reference) -> None:
    doc = reference("wilcoxon.json")
    assert len(doc["cases"]) >= 20
    for case in doc["cases"]:
        d = np.array(case["d"], dtype=float)
        for zm, ref in case["scipy"].items():
            r = stats.wilcoxon_signed_rank(d, zero_method=zm)
            if ref["statistic"] is None:
                assert np.isnan(r.statistic), (case["name"], zm)
                continue
            assert r.statistic == pytest.approx(ref["statistic"], abs=1e-12), (case["name"], zm)
            assert r.pvalue == pytest.approx(ref["pvalue"], rel=1e-12), (case["name"], zm)


def test_o_stat_2_wilcoxon_20_seeded_samples_with_zeros_and_ties() -> None:
    rng = np.random.default_rng(stats.stable_seed(0, [b"test", b"wilcoxon"]))
    for i in range(20):
        n = int(rng.integers(6, 90))
        d = np.round(rng.normal(0.2 * (i % 3), 1.0, n) * 2) / 2  # half-integer grid: zeros and ties
        r = stats.wilcoxon_signed_rank(d)
        ref = sps.wilcoxon(d, zero_method="wilcox", method="auto")
        assert r.statistic == ref.statistic
        assert r.pvalue == pytest.approx(ref.pvalue, rel=1e-12)


def test_wilcoxon_no_data_is_nan() -> None:
    assert np.isnan(stats.wilcoxon_signed_rank(np.zeros(4)).pvalue)
    assert np.isnan(stats.wilcoxon_signed_rank(np.array([])).statistic)


def _cif_distance(live: np.ndarray, sim: np.ndarray) -> float:
    horizons = np.array([1.0, 2.0, 4.0])
    out = 0.0
    for h in horizons:
        out += abs(np.mean((sim[:, 0] <= h) & (sim[:, 1] == 1)) - np.mean((live[:, 0] <= h) & (live[:, 1] == 1)))
    return float(out)


def test_paired_permutation_deterministic_and_calibrated() -> None:
    rng = np.random.default_rng(5)
    live = np.column_stack([rng.exponential(2.0, 80), rng.integers(0, 2, 80)])
    sim_same = live.copy()
    r0 = stats.paired_permutation(live, sim_same, _cif_distance, 199, seed=11)
    assert r0.statistic == 0.0 and r0.p_value == 1.0
    sim_fast = np.column_stack([live[:, 0] * 0.3, np.ones(80)])
    r1 = stats.paired_permutation(live, sim_fast, _cif_distance, 199, seed=11)
    r2 = stats.paired_permutation(live, sim_fast, _cif_distance, 199, seed=11)
    assert r1 == r2
    assert r1.p_value == pytest.approx(1 / 200)
    one_d = stats.paired_permutation(np.zeros(30), np.ones(30), lambda a, b: float(np.mean(b - a)), 99, seed=3)
    assert one_d.statistic == 1.0 and one_d.p_value == pytest.approx(1 / 100)
