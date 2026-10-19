"""Statistics assembly on constructed per-order tables (must-pass: 2x2, bias CIs, deciles, Holm/BH
families, TOST, headline; audit A10 clustered primary; fills-only sources)."""

from __future__ import annotations

import numpy as np
import pytest

from queuetruth import stats
from queuetruth.schemas.specs import ScorecardSpec
from queuetruth.score.statistics import NOT_COMPUTABLE, assemble, assign_strata, block_labels, headline

DAY = 86_400_000_000_000
D0 = 20_454


def spec(**kw):
    base = {"live": {"path": "x"}, "sim": {"kind": "exact"}, "bootstrap": {"b": 200, "seed": 3}, "perm_b": 100}
    base.update(kw)
    return ScorecardSpec.model_validate(base)


def order(i, live, sim, *, day=0, qa=None, tv=None, qty=10, ttf_l=None, ttf_s=None):
    return {
        "order_key": f"o{i:04d}",
        "instrument_id": 1,
        "session_date": D0 + day,
        "ts_entry": (D0 + day) * DAY + 36_000_000_000_000 + i * 1_000_000_000,
        "qty": qty,
        "live_filled": live,
        "sim_filled": sim,
        "live_filled_qty": qty if live else 0,
        "sim_filled_qty": qty if sim else 0,
        "queue_ahead_at_entry": qa,
        "trade_vol_60s": tv,
        "ttf_live_ns": ttf_l if live else None,
        "ttf_sim_ns": ttf_s if sim else None,
    }


def table(n11, n00, b, c, days=1):
    out = []
    i = 0
    for live, sim, n in ((True, True, n11), (False, False, n00), (True, False, b), (False, True, c)):
        for _ in range(n):
            out.append(order(i, live, sim, day=i % days, ttf_l=10**9 + i, ttf_s=2 * 10**9 + i))
            i += 1
    return out


def test_two_by_two_bias_and_mcnemar_golden():
    a = assemble(table(20, 65, 3, 12), [], spec())
    p = a.parts
    assert p["two_by_two"] == {"both": 20, "neither": 65, "live_only": 3, "sim_only": 12}
    assert p["bias"]["fill_rate_bias"] == pytest.approx(9 / 100)
    assert p["bias"]["relative_overfill"] == pytest.approx(32 / 23 - 1)
    assert p["bias"]["qty_bias"] == pytest.approx(90 / 1000)
    m = p["mcnemar"]
    assert (m["b"], m["c"]) == (3, 12)
    assert m["p_exact"] == 0.03515625
    assert m["chi2_cc"] == 4.266666666666667
    assert m["p_chi2"] == pytest.approx(0.038867103812417245, rel=1e-12)


def test_bias_ci_matches_day_block_bootstrap_and_is_deterministic():
    orders = table(30, 100, 10, 25, days=6)
    s = spec()
    a1 = assemble(orders, [], s)
    a2 = assemble(orders, [], s)
    assert a1.parts == a2.parts and a1.tests == a2.tests
    live = np.array([o["live_filled"] for o in orders])
    sim = np.array([o["sim_filled"] for o in orders])
    diff = (sim & ~live).astype(float) - (live & ~sim).astype(float)
    blocks, kind = block_labels(orders, "auto")
    assert kind == "day"
    ci = stats.day_block_bootstrap(
        blocks, lambda idx: float(diff[idx].mean()), 200, stats.bootstrap_seed(3, "fill_rate_bias")
    )
    assert a1.parts["bias"]["ci_lo"] == pytest.approx(ci.lo, abs=1e-15)
    assert a1.parts["bias"]["ci_hi"] == pytest.approx(ci.hi, abs=1e-15)
    assert a1.parts["bias"]["direction"] == ("sim_overfills" if ci.lo > 0 else "no_significant_bias")


def test_direction_underfills_and_30min_warning():
    a = assemble(table(10, 10, 30, 0), [], spec())
    assert a.parts["bias"]["direction"] == "sim_underfills"
    assert "ci_block=30min" in a.warnings


def test_queue_deciles_and_liquidity_buckets():
    rng = np.random.default_rng(1)
    orders = []
    for i in range(200):
        o = order(i, bool(i % 3 == 0), bool(i % 4 == 0), qa=int(rng.integers(0, 1000)), tv=int(rng.integers(0, 10**5)))
        o["instrument_id"] = 1 + i % 2
        orders.append(o)
    assign_strata(orders)
    for iid in (1, 2):
        sub = [o for o in orders if o["instrument_id"] == iid]
        x = np.array([o["queue_ahead_at_entry"] for o in sub], dtype=float)
        q = np.quantile(x, [k / 10 for k in range(1, 10)], method="linear")
        assert [o["queue_decile"] for o in sub] == list(np.clip(np.searchsorted(q, x, side="right"), 0, 9))
        y = np.array([o["trade_vol_60s"] for o in sub], dtype=float)
        t = np.quantile(y, [1 / 3, 2 / 3], method="linear")
        assert [o["liquidity_bucket"] for o in sub] == list(np.clip(np.searchsorted(t, y, side="right"), 0, 2))
    a = assemble(orders, [], spec())
    rows = a.parts["by_queue_decile"]
    assert sum(r["both"] + r["neither"] + r["live_only"] + r["sim_only"] for r in rows) == 200
    for r in rows:
        assert r["p_exact"] == stats.mcnemar(r["live_only"], r["sim_only"]).p_exact
    assert {r["bucket"] for r in a.parts["by_liquidity"]} == {0, 1, 2}


def test_holm_primary_and_bh_secondary_families():
    orders = table(30, 100, 10, 25, days=6)
    for o in orders:
        o["queue_ahead_at_entry"] = int(o["order_key"][1:]) % 37
    assign_strata(orders)
    mk = []
    for o in orders:
        for arm in ("live", "sim"):
            if o[f"{arm}_filled"]:
                for h in (1_000_000_000, 10_000_000_000):
                    v = (int(o["order_key"][1:]) % 7 - 3) / 4 + (0.3 if arm == "sim" else 0.0)
                    mk.append(
                        {
                            "order_key": o["order_key"],
                            "arm": arm,
                            "fill_seq": 1,
                            "horizon_ns": h,
                            "ref": "mid",
                            "mo_ticks": v,
                        }
                    )
    a = assemble(orders, mk, spec(markout_horizons_ns=[1_000_000_000, 10_000_000_000], fdr={"method": "bh", "q": 0.1}))
    prim = [t for t in a.tests if t["family"] == "primary"]
    assert {t["test_id"] for t in prim} == {"discordance_clustered", "markout_diff"}
    p = np.array([t["p_value"] for t in prim])
    assert [t["p_adj"] for t in prim] == pytest.approx(list(stats.holm(p)))
    assert all(t["adj_method"] == "holm" for t in prim)
    sec = [t for t in a.tests if t["adj_method"] == "bh"]
    ps = np.array([t["p_value"] for t in sec])
    assert [t["p_adj"] for t in sec] == pytest.approx(list(stats.benjamini_hochberg(ps, 0.1)))
    assert a.parts["fdr"]["n_tests"] == len(sec)
    assert a.parts["fdr"]["n_rejected"] == int(np.sum(stats.benjamini_hochberg(ps, 0.1) <= 0.1))
    plain = next(t for t in a.tests if t["test_id"] == "mcnemar")
    assert plain["family"] == "secondary" and "independent" in plain["note"]
    d1 = next(r for r in a.parts["markout_diff"] if r["horizon_ns"] == 1_000_000_000)
    assert d1["mean_diff_ticks"] == pytest.approx(0.3)
    assert d1["p_adj"] == next(t["p_adj"] for t in prim if t["test_id"] == "markout_diff")
    clus = next(t for t in prim if t["test_id"] == "discordance_clustered")
    assert "clusters=6" in clus["note"]


def test_tost_equivalence():
    same = assemble(table(40, 60, 0, 0, days=5), [], spec())
    assert same.parts["equivalence"]["fill_rate_equivalent"] is True
    assert same.parts["equivalence"]["tost_p"] == 0.0
    far = assemble(table(10, 50, 0, 40, days=5), [], spec())
    assert far.parts["equivalence"]["fill_rate_equivalent"] is False
    assert far.parts["equivalence"]["tost_p"] > 0.05


def test_tost_p_comes_from_the_ci_replicates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit minor M22 (open conflict 3): `tost_p` is the bootstrap fraction of the replicates behind the 90% CI
    (`max(frac(rep <= -m), frac(rep >= m))`), never the normal approximation, so the flag and the p-value agree."""

    def no_normal(*a, **k):
        raise AssertionError("normal-approximation tost_p used")

    monkeypatch.setattr(stats, "tost_p", no_normal)
    for n11, n00, b, c, margin in ((40, 52, 3, 5, 0.05), (40, 50, 2, 8, 0.05), (40, 52, 3, 5, 0.02)):
        res = assemble(table(n11, n00, b, c, days=6), [], spec(equivalence={"fill_rate_margin": margin}))
        eq = res.parts["equivalence"]
        p = eq["tost_p"]
        assert p is not None and 0.0 <= p <= 1.0
        assert abs(p * 200 - round(p * 200)) < 1e-9, p  # a fraction of the B = 200 replicates
        lo90, hi90 = next((t["ci_lo"], t["ci_hi"]) for t in res.tests if t["test_id"] == "tost_fill_rate")
        assert eq["fill_rate_equivalent"] is (-margin <= lo90 and hi90 <= margin)
        # one method: the flag holds iff fewer than 5% of the replicates fall at or beyond either margin (up to the
        # quantile interpolation at the boundary)
        if eq["fill_rate_equivalent"]:
            assert p <= 0.05 + 1 / 200, (margin, p)
        else:
            assert p >= 0.05 - 1 / 200, (margin, p)
    # markout diff at markout_horizon_ns: every order's sim-minus-live markout is 0.3 ticks, so every replicate is 0.3
    orders = table(30, 100, 10, 25, days=6)
    mk = [
        {"order_key": o["order_key"], "arm": arm, "fill_seq": 1, "horizon_ns": 1_000_000_000, "ref": "mid",
         "mo_ticks": (int(o["order_key"][1:]) % 7 - 3) / 4 + (0.3 if arm == "sim" else 0.0)}
        for o in orders for arm in ("live", "sim") if o[f"{arm}_filled"]
    ]  # fmt: skip
    for margin, p, flag in ((0.25, 1.0, False), (0.5, 0.0, True)):
        eq = assemble(orders, mk, spec(equivalence={"markout_margin_ticks": margin})).parts["equivalence"]
        assert (eq["markout_tost_p"], eq["markout_equivalent"]) == (pytest.approx(p), flag), margin


def test_headline_text():
    bias = {"fill_rate_bias": 0.0912, "ci_lo": 0.031, "ci_hi": 0.1504, "direction": "sim_overfills"}
    assert headline(bias, {"p_exact": 0.03515625}, 100) == (
        "sim overfills live by 9.1 pp fill rate (95% CI [+3.1, +15.0]); McNemar p = 0.0352; 100 orders"
    )
    bias2 = {"fill_rate_bias": -0.02, "ci_lo": -0.05, "ci_hi": 0.01, "direction": "no_significant_bias"}
    assert headline(bias2, {"p_exact": 0.5}, 7, clusters={"p_value": 0.61, "k": 6}) == (
        "no significant fill-rate difference: -2.0 pp (95% CI [-5.0, +1.0]); cluster-robust p = 0.61 (6 clusters); "
        "plain McNemar p = 0.5 (assumes independent orders); 7 orders"
    )


def test_headline_review_f3_f6_f8():
    """Window H in the headline (F6), no double negative and `no gates configured` (F3), equivalence status when
    the difference is not significant (F8)."""
    under = {"fill_rate_bias": -0.115, "ci_lo": -0.15, "ci_hi": -0.08, "direction": "sim_underfills"}
    h = headline(under, {"p_exact": 6.6e-06}, 400, window_ns=60_000_000_000, gates=False)
    assert h == (
        "sim underfills live by 11.5 pp fill rate within 60 s (95% CI [-15.0, -8.0]); McNemar p = 6.6e-06; "
        "400 orders; no gates configured"
    )
    assert "-11.5 pp" not in h
    eq = {"fill_rate_margin": 0.02, "tost_p": 0.038, "fill_rate_equivalent": True}
    same = {"fill_rate_bias": 0.001, "ci_lo": -0.01, "ci_hi": 0.012, "direction": "no_significant_bias"}
    assert headline(same, {"p_exact": 0.9}, 50, window_ns=1_500_000_000, equiv=eq, gates=True) == (
        "no significant fill-rate difference within 1.5 s: +0.1 pp (95% CI [-1.0, +1.2]); equivalent within ±2 pp "
        "(TOST p = 0.038); McNemar p = 0.9; 50 orders"
    )
    neq = {"fill_rate_margin": 0.02, "tost_p": 0.4, "fill_rate_equivalent": False}
    assert "not shown equivalent within ±2 pp (TOST p = 0.4)" in headline(same, {"p_exact": 0.9}, 50, equiv=neq)
    v = assemble(table(10, 50, 0, 40, days=5), [], spec(window_ns=120_000_000_000)).parts["verdict"]
    assert "within 120 s" in v["headline"] and v["headline"].endswith("; no gates configured")
    assert v["gates_passed"] is True  # exit-code semantics unchanged
    g = assemble(table(10, 50, 0, 40, days=5), [], spec(gates={"max_abs_fill_rate_bias": 0.5})).parts["verdict"]
    assert "no gates configured" not in g["headline"] and g["gates_passed"] is True


def test_discordance_clustered_part():
    """Schema 1.1: the clustered primary test is a summary part, null when not computable."""
    a = assemble(table(20, 65, 3, 12, days=6), [], spec())
    d = a.parts["discordance_clustered"]
    row = next(t for t in a.tests if t["test_id"] == "discordance_clustered")
    assert d["method"] == "obuchowski" and d["n_clusters"] == 6
    assert d["p_value"] == row["p_value"] and d["estimate"] == row["value"]
    assert d["ci_lo"] == row["ci_lo"] and d["ci_hi"] == row["ci_hi"]
    assert f"clusters={d['n_clusters']}; discordant clusters={d['n_clusters_discordant']}" in row["note"]
    assert any(w.startswith("clusters: 6") for w in a.warnings)  # v0.1 readers
    assert assemble(table(20, 0, 0, 0), [], spec(), fills_only=True).parts["discordance_clustered"] is None
    assert assemble([], [], spec()).parts["discordance_clustered"] is None


def test_markout_bps_and_net_rows():
    orders = table(4, 0, 0, 0)
    mk = [
        {
            "order_key": o["order_key"],
            "arm": arm,
            "fill_seq": 1,
            "horizon_ns": 1_000_000_000,
            "ref": "mid",
            "mo_ticks": 1.0,
            "mo_bps": 2.5 if arm == "live" else 5.0,
            "mo_net_ccy": -0.5 if arm == "live" else None,
        }
        for o in orders
        for arm in ("live", "sim")
    ]
    a = assemble(orders, mk, spec(markout_horizons_ns=[1_000_000_000]))
    bps = {t["stratum_kind"]: t for t in a.tests if t["test_id"] == "markout_mean_bps"}
    assert bps["live"]["value"] == 2.5 and bps["sim"]["value"] == 5.0 and bps["live"]["statistic"] == "mean_bps"
    assert bps["live"]["stratum"] == "mid:1000000000" and bps["live"]["n"] == 4
    net = [t for t in a.tests if t["test_id"] == "markout_mean_net_ccy"]
    assert [(t["stratum_kind"], t["value"], t["n"]) for t in net] == [("live", -0.5, 4)]
    plain = assemble(
        orders,
        [{k: v for k, v in r.items() if not k.startswith("mo_") or k == "mo_ticks"} for r in mk],
        spec(markout_horizons_ns=[1_000_000_000]),
    )
    assert not [
        t
        for t in plain.tests
        if t["test_id"].startswith("markout_mean_")
        and t["test_id"] != "markout_mean_taker"
        and t["test_id"] != "markout_mean"
    ]
    assert [t["value"] for t in a.tests if t["test_id"] == "markout_mean"] == [
        t["value"] for t in plain.tests if t["test_id"] == "markout_mean"
    ]


def test_gates():
    s = spec(gates={"max_abs_fill_rate_bias": 0.01, "require_equivalence": True})
    a = assemble(table(10, 50, 0, 40, days=5), [], s)
    v = a.parts["verdict"]
    assert v["gates_passed"] is False
    assert v["failed_gates"] == ["max_abs_fill_rate_bias", "require_equivalence"]
    ok = assemble(table(40, 60, 0, 0, days=5), [], s)
    assert ok.parts["verdict"]["gates_passed"] is True


def test_fills_only_not_computable_but_ttf_and_markouts_computed():
    orders = table(20, 0, 0, 0)
    mk = [
        {
            "order_key": o["order_key"],
            "arm": arm,
            "fill_seq": 1,
            "horizon_ns": 1_000_000_000,
            "ref": "mid",
            "mo_ticks": 1.0,
        }
        for o in orders
        for arm in ("live", "sim")
    ]
    a = assemble(orders, mk, spec(gates={"max_abs_fill_rate_bias": 0.05}), fills_only=True)
    p = a.parts
    assert p["bias"]["fill_rate_bias"] is None and p["mcnemar"]["p_exact"] is None
    assert NOT_COMPUTABLE in p["verdict"]["headline"]
    assert p["verdict"]["gates_passed"] is False and NOT_COMPUTABLE in p["verdict"]["failed_gates"][0]
    assert p["ttf"]["n_joint"] == 20 and p["ttf"]["median_log_ratio"] is not None
    assert p["ttf"]["paired_perm_p"] is None
    assert any(r["n"] == 20 and r["mean_ticks"] == 1.0 for r in p["markouts"])
    assert any("fills_only" in w for w in a.warnings)


def test_taker_fills_excluded_from_maker_markouts():
    orders = table(4, 0, 0, 0)
    mk = []
    for o in orders:
        mk.append(
            {
                "order_key": o["order_key"],
                "arm": "sim",
                "fill_seq": 1,
                "horizon_ns": 1_000_000_000,
                "ref": "mid",
                "mo_ticks": -5.0,
            }
        )
        mk.append(
            {
                "order_key": o["order_key"],
                "arm": "sim",
                "fill_seq": 2,
                "horizon_ns": 1_000_000_000,
                "ref": "mid",
                "mo_ticks": 1.0,
            }
        )
    taker = {(o["order_key"], "sim", 1) for o in orders}
    a = assemble(orders, mk, spec(markout_horizons_ns=[1_000_000_000]), taker=taker)
    row = next(r for r in a.parts["markouts"] if r["arm"] == "sim")
    assert row["mean_ticks"] == 1.0 and row["n"] == 4
    tk = next(t for t in a.tests if t["test_id"] == "markout_mean_taker")
    assert tk["value"] == -5.0 and tk["n"] == 4


def test_ttf_cif_and_permutation():
    orders = table(30, 30, 5, 5, days=2)
    a = assemble(orders, [], spec(ttf_horizons_ns=[1_500_000_000, 60_000_000_000]))
    t = a.parts["ttf"]
    assert t["n_joint"] == 30
    assert t["cif_fill"]["live"]["60000000000"] == pytest.approx(35 / 70)
    assert t["cif_fill"]["sim"]["1500000000"] == pytest.approx(0.0)
    assert 0 < t["paired_perm_p"] <= 1 and t["logrank_p"] is not None and t["gray_p"] is not None
    assert t["median_log_ratio"] == pytest.approx(np.median([np.log((2e9 + i) / (1e9 + i)) for i in range(30)]))
