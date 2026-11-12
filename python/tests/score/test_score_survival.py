"""Time-to-fill survival curves (`queuetruth.score.survival`, summary part `ttf_survival`): Kaplan-Meier and the
stratified log-rank score against a hand calculation on a tiny case, the cluster-robust variance, and the part as
`assemble` builds it (intervals, censoring at cancels, fills-only sources)."""

from __future__ import annotations

import numpy as np
import pytest

from queuetruth import stats
from queuetruth.score.statistics import assemble
from queuetruth.score.survival import (
    clustered_logrank,
    curve_points,
    km_curve,
    logrank_residuals,
    median_time,
    survival_part,
)
from score.test_score_statistics import D0, DAY, spec

S = 1_000_000_000

# Tiny case (times in seconds; 1 = fill, 0 = censored at a cancel or the window end).
LIVE_T, LIVE_E = [1.0, 2.0, 3.0, 4.0], [1, 0, 1, 1]
SIM_T, SIM_E = [1.0, 1.0, 2.0, 5.0], [1, 1, 1, 0]


def test_kaplan_meier_matches_hand_calculation():
    # live: t=1 1 of 4 fill -> 3/4; t=2 censored; t=3 1 of 2 -> 3/8; t=4 1 of 1 -> 0
    sf = km_curve(np.array(LIVE_T), np.array(LIVE_E))
    assert sf.at(0.5) == 1.0 and sf.at(1.0) == 0.75 and sf.at(2.5) == 0.75 and sf.at(3.0) == 0.375 and sf.at(4.0) == 0
    assert median_time(sf) == 3.0
    # sim: t=1 2 of 4 -> 1/2; t=2 1 of 2 -> 1/4; t=5 censored
    sf = km_curve(np.array(SIM_T), np.array(SIM_E))
    assert sf.at(1.0) == 0.5 and sf.at(2.0) == 0.25 and sf.at(10.0) == 0.25
    assert median_time(sf) == 1.0
    assert median_time(km_curve(np.array([1.0, 2.0]), np.array([0, 0]))) is None


def test_logrank_score_matches_hand_calculation():
    # U = sum over fill times of (d_sim - n_sim d / n):
    # t=1: 2 - 4*3/8 = 0.5; t=2: 1 - 2*1/5 = 0.6; t=3: 0 - 1*1/3; t=4: 0 - 1*1/2  -> U = 4/15
    t = np.array(LIVE_T + SIM_T)
    e = np.array(LIVE_E + SIM_E, dtype=bool)
    z = np.array([0] * 4 + [1] * 4)
    u = logrank_residuals(t, e, z, np.zeros(8))
    assert u.sum() == pytest.approx(0.5 + 0.6 - 1 / 3 - 1 / 2)
    # robust variance, one cluster per pair (live order i with sim order i): C/(C-1) sum_c (u_c)^2
    pairs = np.array([0, 1, 2, 3, 0, 1, 2, 3])
    chi2, p = clustered_logrank(t, e, z, np.zeros(8), pairs)
    per = np.bincount(pairs, weights=u)
    assert chi2 == pytest.approx(u.sum() ** 2 / (4 / 3 * np.sum(per**2)))
    assert 0 < p < 1


def test_stratified_score_is_the_sum_of_the_strata():
    rng = np.random.default_rng(1)
    t = rng.exponential(1.0, 40)
    e = rng.random(40) < 0.7
    z = np.tile([0, 1], 20)
    st = np.repeat([0, 1], 20)
    u = logrank_residuals(t, e, z, st)
    a = logrank_residuals(t[:20], e[:20], z[:20], np.zeros(20)).sum()
    b = logrank_residuals(t[20:], e[20:], z[20:], np.zeros(20)).sum()
    assert u.sum() == pytest.approx(a + b)


def test_independent_clusters_agree_with_the_plain_logrank_on_large_samples():
    rng = np.random.default_rng(7)
    n = 4000
    ta, tb = rng.exponential(1.0, n), rng.exponential(1.25, n)
    ea, eb = rng.random(n) < 0.8, rng.random(n) < 0.8
    plain = stats.logrank(ta, ea, tb, eb)
    chi2, _ = clustered_logrank(
        np.concatenate([ta, tb]),
        np.concatenate([ea, eb]),
        np.concatenate([np.zeros(n), np.ones(n)]),
        np.zeros(2 * n),
        np.arange(2 * n),
    )
    assert chi2 == pytest.approx(plain.statistic, rel=0.05)


def test_long_curves_are_sampled():
    sf = km_curve(np.arange(1, 1001, dtype=float), np.ones(1000, dtype=bool))
    pts, exact = curve_points(sf, max_points=50)
    assert not exact and len(pts) <= 50 and pts[0] == [0.0, 1.0]
    assert all(v == pytest.approx(float(sf.at(t))) for t, v in pts)
    pts, exact = curve_points(km_curve(np.array(LIVE_T), np.array(LIVE_E)))
    assert exact and pts == [[0.0, 1.0], [1.0, 0.75], [2.0, 0.75], [3.0, 0.375], [4.0, 0.0]]


def _orders(n_days=5, per_day=20, sim_faster=True):
    """Orders whose live fill comes at 4 s and sim fill at 1 s (or the same) ; every fifth is cancelled at 2 s in
    both arms before it fills (censored)."""
    out = []
    i = 0
    for d in range(n_days):
        for k in range(per_day):
            cancelled = k % 5 == 4
            tl, ts = 4 * S, (1 * S if sim_faster else 4 * S)
            out.append(
                {
                    "order_key": f"o{i:04d}",
                    "instrument_id": 1 + k % 2,
                    "session_date": D0 + d,
                    "ts_entry": (D0 + d) * DAY + 36_000 * S + k * S,
                    "qty": 1,
                    "live_filled": not cancelled,
                    "sim_filled": not cancelled,
                    "live_filled_qty": 0 if cancelled else 1,
                    "sim_filled_qty": 0 if cancelled else 1,
                    "ttf_live_ns": None if cancelled else tl,
                    "ttf_sim_ns": None if cancelled else ts,
                    "live_event_ns": 2 * S if cancelled else tl,
                    "live_cause": 2 if cancelled else 1,
                    "sim_event_ns": 2 * S if cancelled else ts,
                    "sim_cause": 2 if cancelled else 1,
                }
            )
            i += 1
    return out


def test_assemble_survival_part():
    a = assemble(_orders(), [], spec(ttf_horizons_ns=[S, 3 * S, 10 * S]))
    sv = a.parts["ttf_survival"]
    assert sv["n"] == 100 and sv["horizons_ns"] == [S, 3 * S, 10 * S]
    live, sim = sv["arms"]["live"], sv["arms"]["sim"]
    # cancels at 2 s censor (not events): 80 fills, 20 censored per arm
    assert (live["n_filled"], live["n_censored"]) == (80, 20)
    # S at 1 s: live 1, sim 0 (every uncancelled order filled at 1 s; the cancels come later)
    assert [x["survival"] for x in live["at"]] == [1.0, 1.0, 0.0]
    assert [x["survival"] for x in sim["at"]] == [pytest.approx(0.2), pytest.approx(0.2), pytest.approx(0.2)]
    assert live["median_ns"] == 4 * S and sim["median_ns"] == S
    d1 = sv["diff"][0]
    assert d1["diff"] == pytest.approx(-0.8) and d1["ci_lo"] <= -0.8 + 1e-9 and d1["ci_hi"] <= 0
    lr = sv["logrank"]
    assert lr["n_strata"] == 2 and "instrument x bootstrap block" in lr["method"]
    assert lr["n_clusters"] == 10  # 2 instruments x 5 days
    # every cluster says the same: the robust chi2 reaches its ceiling C - 1 = 9 (p = 0.0027), no smaller p is
    # claimed from 10 clusters
    assert lr["statistic"] == pytest.approx(9.0) and lr["p_value"] == pytest.approx(0.0027, abs=1e-4)
    row = next(t for t in a.tests if t["test_id"] == "ttf_logrank_clustered")
    assert row["p_value"] == pytest.approx(lr["p_value"]) and row["adj_method"] in ("bh", "by")


def test_identical_arms_have_no_survival_difference():
    sv = assemble(_orders(sim_faster=False), [], spec()).parts["ttf_survival"]
    assert all(x["diff"] == 0.0 for x in sv["diff"])
    assert sv["logrank"]["statistic"] in (0.0, None)


def test_few_clusters_fall_back_to_orders_and_fills_only_is_null():
    sv = assemble(_orders(n_days=1, per_day=4), [], spec()).parts["ttf_survival"]
    assert "clusters = orders" in sv["logrank"]["method"] and sv["logrank"]["n_clusters"] == 4
    assert assemble(_orders(), [], spec(), fills_only=True).parts["ttf_survival"] is None


def test_median_upper_end_is_null_when_replicates_do_not_reach_half():
    """52 of 100 orders fill: the median exists, but many resamples stay above S = 0.5, so the upper end of its
    interval is not bounded (null), while the lower end is."""
    n = 100
    t = np.arange(1, n + 1, dtype=float) * S
    cause = np.where(np.arange(n) < 52, 1, 0)
    ev = np.stack([t, cause], axis=1)
    orders = [{"instrument_id": 1} for _ in range(n)]
    blocks = np.arange(n) % 20
    sv = survival_part(orders, {"live": ev, "sim": ev}, [S], blocks, 200, 1)
    a = sv["arms"]["live"]
    assert a["median_ns"] is not None and a["median_ci_lo"] is not None and a["median_ci_hi"] is None
