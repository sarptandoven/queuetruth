"""O-STAT-4: BH/BY/Holm vs statsmodels, deterministic block bootstrap, TOST on constructed estimates."""

from __future__ import annotations

import numpy as np
import pytest
from statsmodels.stats.multitest import multipletests

from queuetruth import stats

_METHODS = {"fdr_bh": stats.benjamini_hochberg, "fdr_by": stats.benjamini_yekutieli}


def test_o_stat_4_fdr_holm_reference(reference) -> None:
    doc = reference("multitest.json")
    for case in doc["cases"]:
        p = np.array(case["p"], float)
        for method, ref in case["statsmodels"].items():
            got = stats.holm(p) if method == "holm" else _METHODS[method](p, 0.05)
            np.testing.assert_allclose(got, ref["p_adj"], rtol=0, atol=1e-15, err_msg=f"{case['name']}:{method}")


def test_o_stat_4_fdr_holm_statsmodels_live() -> None:
    rng = np.random.default_rng(stats.stable_seed(0, [b"test", b"multitest"]))
    for _ in range(20):
        m = int(rng.integers(1, 60))
        p = np.where(rng.random(m) < 0.3, rng.random(m) * 0.01, rng.random(m))
        p[rng.integers(0, m, size=m // 4)] = p[0]  # ties
        for name, fn in (("fdr_bh", stats.benjamini_hochberg), ("fdr_by", stats.benjamini_yekutieli)):
            np.testing.assert_allclose(fn(p, 0.05), multipletests(p, 0.05, method=name)[1], atol=1e-15)
        np.testing.assert_allclose(stats.holm(p), multipletests(p, 0.05, method="holm")[1], atol=1e-15)
        np.testing.assert_allclose(stats.adjust(p, "bh"), stats.benjamini_hochberg(p))


def test_o_stat_4_fdr_nan_left_out() -> None:
    p = np.array([0.01, np.nan, 0.04, 0.03])
    got = stats.benjamini_hochberg(p)
    assert np.isnan(got[1])
    np.testing.assert_allclose(got[[0, 2, 3]], multipletests([0.01, 0.04, 0.03], method="fdr_bh")[1])


def test_o_stat_4_bootstrap_reference(reference) -> None:
    doc = reference("bootstrap.json")
    for case in doc["day_block_bootstrap"]:
        blocks = np.array(case["input"]["blocks"], np.int64)
        live = np.array(case["input"]["live_filled"], float)
        sim = np.array(case["input"]["sim_filled"], float)
        seed = stats.bootstrap_seed(case["seed"], case["test_id"])
        assert seed == case["derived_seed"]

        def bias(idx: np.ndarray, live: np.ndarray = live, sim: np.ndarray = sim) -> float:
            return float(np.mean(sim[idx] - live[idx]))

        ci = stats.day_block_bootstrap(blocks, bias, case["b"], seed, alpha=0.05)
        assert ci.estimate == case["estimate"]
        assert (ci.lo, ci.hi) == pytest.approx(tuple(case["ci95"]), abs=1e-15)
        ci90 = stats.day_block_bootstrap(blocks, bias, case["b"], seed, alpha=0.10)
        assert (ci90.lo, ci90.hi) == pytest.approx(tuple(case["ci90"]), abs=1e-15)
        _est, reps = stats.block_replicates(blocks, bias, case["b"], seed)
        np.testing.assert_allclose(reps[:10], case["replicates_first_10"], atol=1e-15)
        assert stats.bootstrap_se(reps) == pytest.approx(case["boot_sd_ddof1"], abs=1e-15)
        assert stats.bootstrap_p_two_sided(reps) == pytest.approx(case["p_bootstrap_test"], abs=1e-15)
        # block multiplicities are the same draws: ratio-of-sums replicates equal the index replicates
        labels, inv = np.unique(blocks, return_inverse=True)
        counts = stats.block_counts(labels.size, case["b"], seed)
        assert counts.sum(axis=1).tolist() == [labels.size] * case["b"]
        num = np.bincount(inv, weights=sim - live)
        den = np.bincount(inv).astype(float)
        np.testing.assert_allclose((counts * num).sum(1) / (counts * den).sum(1), reps, rtol=1e-12, atol=1e-15)


def test_o_stat_4_bootstrap_deterministic_for_a_seed() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=300)
    blocks = np.repeat(np.arange(10), 30)
    a = stats.day_block_bootstrap(blocks, lambda i: float(np.mean(x[i])), 500, stats.bootstrap_seed(3, "t"))
    b = stats.day_block_bootstrap(blocks, lambda i: float(np.mean(x[i])), 500, stats.bootstrap_seed(3, "t"))
    c = stats.day_block_bootstrap(blocks, lambda i: float(np.mean(x[i])), 500, stats.bootstrap_seed(3, "u"))
    assert a == b and a != c and a.lo < a.estimate < a.hi


def test_bootstrap_p_never_zero_and_capped() -> None:
    assert stats.bootstrap_p_two_sided(np.zeros(100)) == 1.0
    assert stats.bootstrap_p_two_sided(np.ones(99)) == pytest.approx(2 / 100)
    assert np.isnan(stats.bootstrap_p_two_sided(np.array([np.nan])))


def test_block_labels_day_vs_30min() -> None:
    dates = np.array(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"], dtype="datetime64[D]")
    ts = np.arange(5) * 86_400_000_000_000
    labels, kind = stats.block_labels(dates, ts)
    assert kind == "day" and labels.tolist() == dates.astype(np.int64).tolist()
    labels, kind = stats.block_labels(dates[:2], np.array([0, 1_800_000_000_000 + 5]))
    assert kind == "30min" and labels.tolist() == [0, 1]


def test_o_stat_4_tost_reference(reference) -> None:
    doc = reference("tost.json")
    for case in doc["tost_p"]:
        got = stats.tost_p(case["estimate"], case["se"], case["margin"])
        assert got == pytest.approx(case["tost_p"], abs=1e-15), case
    for case in doc["tost_from_ci"]:
        assert stats.tost_from_ci(tuple(case["ci90"]), case["margin"]) is case["equivalent"], case


def test_o_stat_4_tost_constructed() -> None:
    assert stats.tost_from_ci((-0.01, 0.015), 0.02) is True
    assert stats.tost_from_ci((-0.01, 0.025), 0.02) is False
    assert stats.tost_from_ci((float("nan"), 0.0), 0.02) is False
    assert stats.tost_p(0.0, 0.0, 0.02) == 0.0 and stats.tost_p(0.05, 0.0, 0.02) == 1.0
    assert stats.tost_p(0.0, 0.01, 0.02) < 0.05 < stats.tost_p(0.015, 0.01, 0.02)


def test_m22_tost_p_bootstrap() -> None:
    """Audit minor M22: the bootstrap TOST p-value is the larger tail fraction at the margins (closed tails, finite
    replicates only), and it agrees with the 90% percentile-CI flag."""
    reps = np.array([-0.03, -0.01, 0.0, 0.01, 0.02, 0.025, np.nan])
    assert stats.tost_p_bootstrap(reps, 0.02) == pytest.approx(max(1 / 6, 2 / 6))
    assert stats.tost_p_bootstrap(np.zeros(10), 0.02) == 0.0
    assert stats.tost_p_bootstrap(np.full(10, 0.02), 0.02) == 1.0  # on the margin counts as outside
    assert np.isnan(stats.tost_p_bootstrap(np.array([np.nan]), 0.02))
    assert np.isnan(stats.tost_p_bootstrap(reps, float("nan")))
    rng = np.random.default_rng(7)
    for centre in (0.0, 0.005, 0.012, 0.03):
        r = rng.normal(centre, 0.005, 4000)
        flag = stats.tost_from_ci(stats.percentile_ci(r, 0.10), 0.02)
        p = stats.tost_p_bootstrap(r, 0.02)
        assert flag is (p < 0.05), (centre, p)
