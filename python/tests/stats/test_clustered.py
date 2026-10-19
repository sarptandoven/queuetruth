"""Audit ruling A10: cluster-robust primary discordance test (O-STAT-1 clustered-null size check)."""

from __future__ import annotations

import numpy as np
import pytest
from scipy import stats as sps

from queuetruth import stats


def _paired(b_k: np.ndarray, c_k: np.ndarray, n_k: np.ndarray, both_k: np.ndarray | None = None):
    live, sim, cl = [], [], []
    for k, (b, c, n) in enumerate(zip(b_k, c_k, n_k, strict=True)):
        both = 0 if both_k is None else both_k[k]
        x = np.zeros(n, bool)
        y = np.zeros(n, bool)
        y[:c] = True
        x[c : c + b] = True
        x[c + b : c + b + both] = True
        y[c + b : c + b + both] = True
        live.append(x)
        sim.append(y)
        cl.append(np.full(n, k))
    return np.concatenate(live), np.concatenate(sim), np.concatenate(cl)


def test_o_stat_1_clustered_hand_formula() -> None:
    b_k = np.array([3, 0, 5, 2, 1, 4])
    c_k = np.array([6, 2, 4, 7, 3, 9])
    n_k = np.array([40, 25, 50, 30, 20, 45])
    live, sim, cl = _paired(b_k, c_k, n_k, both_k=np.array([10, 5, 8, 6, 4, 9]))
    r = stats.clustered_mcnemar(live, sim, cl, b=500, seed=stats.bootstrap_seed(0, "disc"))
    total = n_k.sum()
    d = (c_k.sum() - b_k.sum()) / total
    var = 6 / 5 * np.sum(((c_k - b_k) - n_k * d) ** 2) / total**2
    z = d / np.sqrt(var)
    assert (r.b, r.c, r.n, r.n_clusters, r.n_clusters_discordant) == (15, 31, 210, 6, 6)
    assert r.estimate == pytest.approx(d)
    assert r.obuchowski_z == pytest.approx(z, rel=1e-12)
    assert r.p_obuchowski == pytest.approx(2 * sps.t.sf(abs(z), 5), rel=1e-12)
    assert r.primary_method == "obuchowski" and r.p_primary == r.p_obuchowski
    assert r.mcnemar == stats.mcnemar(15, 31)  # secondary
    assert r.ci_lo < d < r.ci_hi
    var_iid = ((15 + 31) / 210 - d * d) / 209
    assert r.design_effect == pytest.approx(var / var_iid, rel=1e-12)
    assert r.n_effective == pytest.approx(210 / r.design_effect)
    again = stats.clustered_mcnemar(live, sim, cl, b=500, seed=stats.bootstrap_seed(0, "disc"))
    assert again == r


def test_o_stat_1_few_clusters_use_block_bootstrap() -> None:
    live, sim, cl = _paired(np.array([2, 1, 3]), np.array([9, 8, 7]), np.array([30, 30, 30]))
    r = stats.clustered_mcnemar(live, sim, cl, b=400, seed=1)
    assert r.n_clusters == 3 and r.p_obuchowski is None and r.design_effect is None
    assert r.primary_method == "block_bootstrap" and r.p_primary == r.p_bootstrap
    assert 0 < r.p_bootstrap <= 1


def test_o_stat_1_clustered_null_size_at_most_0_07() -> None:
    # audit check mcn.py: 10 days x 200 orders, discordance prob 0.1, per-day tilt U(-0.2, 0.2) (marginally null)
    rng = np.random.default_rng(stats.stable_seed(0, [b"test", b"clustered_null"]))
    reps, k = 2000, 10
    rej_primary = rej_exact = 0
    for _ in range(reps):
        nd = rng.binomial(200, 0.1, size=k)
        c_k = rng.binomial(nd, 0.5 + rng.uniform(-0.2, 0.2, size=k))
        b_k = nd - c_k
        _, p = stats.obuchowski(b_k, c_k, np.full(k, 200))
        rej_primary += p < 0.05
        rej_exact += stats.mcnemar(int(b_k.sum()), int(c_k.sum())).p_exact < 0.05
    assert rej_primary / reps <= 0.07
    assert rej_exact / reps > 0.10  # the plain test is anti-conservative here, hence secondary only


def test_independent_orders_have_design_effect_near_one() -> None:
    rng = np.random.default_rng(7)
    n = 6000
    live = rng.random(n) < 0.4
    sim = np.where(rng.random(n) < 0.1, ~live, live)
    cl = rng.integers(0, 30, n)
    r = stats.clustered_mcnemar(live, sim, cl, b=200, seed=2)
    assert 0.6 < r.design_effect < 1.6
    assert r.n_effective == pytest.approx(n / r.design_effect)
