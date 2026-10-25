"""Executable PnL: fee schedule, sizing caps, quote timing, the frictionless identity and recovery of a known edge
after costs."""

from __future__ import annotations

import math

import numpy as np
import pytest

from queuetruth.forecast.io import Quotes
from queuetruth.forecast.pnl import Candidate, decide, order_fee, run_pnl, unit_fee
from queuetruth.forecast.schemas import FeeSchedule, PnlSpec
from queuetruth.forecast.simulate import binary_world, expected_kelly_growth

H = 3_600_000_000_000


def _quotes(ts: list[int], bid: list[float], ask: list[float], depth: float = 1e12) -> Quotes:
    n = len(ts)
    return Quotes(
        ts=np.array(ts, dtype=np.int64),
        bid=np.array(bid, dtype=float),
        ask=np.array(ask, dtype=float),
        bid_depth=np.full(n, depth),
        ask_depth=np.full(n, depth),
    )


def _cand(p: float, y: float, qid: str = "q", t: int = 10 * H, cluster: int = 0) -> Candidate:
    return Candidate(
        forecaster="f",
        question_id=qid,
        p=p,
        decided_at_ns=t,
        cutoff_ns=t + H,
        resolved_at_ns=t + 2 * H,
        outcome=y,
        cluster=cluster,
    )


def test_fee_schedule() -> None:
    kalshi = FeeSchedule(quadratic=0.07, round_up_to=0.01)
    assert unit_fee(0.5, kalshi) == pytest.approx(0.0175)
    assert order_fee(1, 0.5, kalshi) == pytest.approx(0.02)  # 0.0175 rounded up to the cent
    assert order_fee(100, 0.5, kalshi) == pytest.approx(1.75)
    assert order_fee(10, 0.3, kalshi) == pytest.approx(0.15)  # 0.147 -> 0.15
    lin = FeeSchedule(per_contract=0.01, notional_rate=0.02)
    assert order_fee(10, 0.4, lin) == pytest.approx(10 * (0.01 + 0.008))
    assert order_fee(0, 0.4, kalshi) == 0.0
    assert order_fee(5, 0.4, FeeSchedule()) == 0.0


def test_decide_sides_and_kelly() -> None:
    qs = {"q": _quotes([0], [0.40], [0.42])}
    spec = PnlSpec(kelly_fraction=1.0, lot=0.0)
    yes = decide(_cand(0.6, 1.0), qs, spec, 100.0, 100.0)
    assert (yes.status, yes.side, yes.price) == ("bet", "yes", 0.42)
    assert yes.kelly == pytest.approx((0.6 - 0.42) / (1 - 0.42))
    assert yes.contracts == pytest.approx(yes.kelly * 100 / 0.42)
    no = decide(_cand(0.2, 0.0), qs, spec, 100.0, 100.0)
    assert (no.status, no.side, no.price) == ("bet", "no", pytest.approx(0.60))
    assert no.kelly == pytest.approx((0.8 - 0.6) / 0.4)
    inside = decide(_cand(0.41, 1.0), qs, spec, 100.0, 100.0)
    assert inside.status == "no_edge"
    assert decide(_cand(0.43, 1.0), qs, PnlSpec(min_edge=0.02), 100.0, 100.0).status == "no_edge"


def test_decide_caps_and_skips() -> None:
    qs = {"q": _quotes([0], [0.40], [0.42], depth=7)}
    d = decide(_cand(0.9, 1.0), qs, PnlSpec(kelly_fraction=1.0), 1000.0, 1000.0)
    assert d.status == "bet" and d.contracts == 7 and d.depth_limited
    half = decide(_cand(0.9, 1.0), qs, PnlSpec(kelly_fraction=1.0, max_participation=0.5), 1000.0, 1000.0)
    assert half.contracts == 3  # floor(3.5) whole lots
    qs_big = {"q": _quotes([0], [0.40], [0.42])}
    c = decide(_cand(0.9, 1.0), qs_big, PnlSpec(kelly_fraction=1.0), 1000.0, 5.0)
    assert c.status == "bet" and c.cash_limited and c.contracts * 0.42 + c.fee <= 5.0
    assert decide(_cand(0.9, 1.0), qs_big, PnlSpec(kelly_fraction=1.0), 1000.0, 0.1).status == "insufficient_cash"
    tiny = decide(_cand(0.43, 1.0), qs_big, PnlSpec(kelly_fraction=0.01), 10.0, 10.0)
    assert tiny.status == "size_zero"
    fees = PnlSpec(kelly_fraction=1.0, fees=FeeSchedule(per_contract=0.001, round_up_to=1.0))
    assert decide(_cand(0.43, 1.0), qs_big, fees, 100.0, 100.0).status == "fees_exceed_edge"
    empty_depth = {"q": _quotes([0], [0.40], [0.42], depth=0)}
    assert decide(_cand(0.9, 1.0), empty_depth, PnlSpec(), 100.0, 100.0).status == "no_depth"
    one_sided = {"q": _quotes([0], [math.nan], [0.42])}
    assert decide(_cand(0.1, 0.0), one_sided, PnlSpec(), 100.0, 100.0).status == "no_edge"


def test_quote_timing() -> None:
    qs = {"q": _quotes([5 * H, 10 * H + 100, 12 * H], [0.10, 0.70, 0.95], [0.12, 0.72, 0.97])}
    c = _cand(0.5, 1.0, t=10 * H)
    at = decide(c, qs, PnlSpec(), 100.0, 100.0)
    assert (at.quote_ts_ns, at.side) == (5 * H, "yes")  # the quote after the decision is not read
    # priced at arrival: a bettor with no market-data delay sees the newer quote and flips side
    late = decide(c, qs, PnlSpec(latency_ns=200, price_at="arrival"), 100.0, 100.0)
    assert (late.quote_ts_ns, late.side) == (10 * H + 100, "no")
    # priced at the decision (default): a YES limit at 0.12 meets an ask of 0.72 on arrival and does not fill
    moved = decide(c, qs, PnlSpec(latency_ns=200), 100.0, 100.0)
    assert (moved.status, moved.side, moved.decision_ts_ns, moved.quote_ts_ns) == (
        "price_moved",
        "yes",
        5 * H,
        10 * H + 100,
    )
    # a better price on arrival fills at that price (a marketable limit), capped by the arrival depth
    better = {
        "q": Quotes(
            ts=np.array([5 * H, 10 * H + 100], dtype=np.int64),
            bid=np.array([0.10, 0.08]),
            ask=np.array([0.12, 0.11]),
            bid_depth=np.array([1e9, 1e9]),
            ask_depth=np.array([1e9, 3.0]),
        )
    }
    fill = decide(c, better, PnlSpec(latency_ns=200, kelly_fraction=1.0), 100.0, 100.0)
    assert (fill.status, fill.price, fill.contracts, fill.depth_limited) == ("bet", 0.11, 3.0, True)
    assert decide(c, qs, PnlSpec(latency_ns=H), 100.0, 100.0).status == "after_cutoff"
    assert decide(c, qs, PnlSpec(max_quote_age_ns=H), 100.0, 100.0).status == "stale_quote"
    assert decide(_cand(0.5, 1.0, t=H), qs, PnlSpec(), 100.0, 100.0).status == "no_quote_yet"
    assert decide(_cand(0.5, 1.0, qid="zz"), qs, PnlSpec(), 100.0, 100.0).status == "no_quotes"


def test_frictionless_identity() -> None:
    """Zero spread, fees, latency and depth limits, full Kelly, continuous lots: the isolated growth equals the
    log-score skill against the mid, question by question."""
    w = binary_world(500, 0.7, seed=21)
    qs = {f"q{i}": _quotes([0], [w.q[i]], [w.q[i]]) for i in range(500)}
    cands = [_cand(float(w.p[i]), float(w.y[i]), qid=f"q{i}", cluster=i) for i in range(500)]
    spec = PnlSpec(kelly_fraction=1.0, lot=0.0, bankroll=1.0)
    run = run_pnl(cands, qs, spec, b=0)
    skill = np.where(w.y == 1, np.log(w.p / w.q), np.log((1 - w.p) / (1 - w.q)))
    assert run.result.frictionless_growth == pytest.approx(float(skill.mean()), rel=1e-12)
    assert run.result.isolated_growth.value == pytest.approx(float(skill.mean()), rel=1e-9)
    assert run.result.fees_paid == 0 and run.result.spread_paid == pytest.approx(0.0, abs=1e-12)


def test_sequential_path_compounds_non_overlapping_bets() -> None:
    """Questions that resolve before the next decision compound: log(final / bankroll) = sum of the bets' log
    returns under full Kelly at the mid."""
    w = binary_world(50, 0.7, seed=4)
    qs = {f"q{i}": _quotes([0], [w.q[i]], [w.q[i]]) for i in range(50)}
    cands = [_cand(float(w.p[i]), float(w.y[i]), qid=f"q{i}", t=(3 * i + 1) * H, cluster=i) for i in range(50)]
    run = run_pnl(cands, qs, PnlSpec(kelly_fraction=1.0, lot=0.0, bankroll=1.0), b=0)
    skill = np.where(w.y == 1, np.log(w.p / w.q), np.log((1 - w.p) / (1 - w.q)))
    assert run.result.log_growth == pytest.approx(float(skill.sum()), rel=1e-9)
    assert run.result.n_bets == int(np.sum(np.abs(w.p - w.q) > 0))
    assert 0 <= run.result.max_drawdown < 1


def test_known_edge_growth_after_costs_matches_expectation() -> None:
    """A forecaster who knows the truth, quarter Kelly against a 2-cent spread: the mean isolated log return
    converges to the expected Kelly growth computed from the true probabilities."""
    n = 6000
    w = binary_world(n, 0.6, seed=31)
    hs = 0.01
    qs = {f"q{i}": _quotes([0], [w.q[i] - hs], [w.q[i] + hs]) for i in range(n)}
    cands = [_cand(float(w.p[i]), float(w.y[i]), qid=f"q{i}", cluster=i) for i in range(n)]
    lam = 0.25
    run = run_pnl(cands, qs, PnlSpec(kelly_fraction=lam, lot=0.0, bankroll=1.0), b=500, seed=1)
    exp = []
    for i in range(n):
        p, q = float(w.p[i]), float(w.q[i])
        g_yes = expected_kelly_growth(p, q + hs, p, lam)
        g_no = expected_kelly_growth(1 - p, 1 - (q - hs), 1 - p, lam)
        exp.append(max(g_yes, g_no))
    expected = float(np.mean(exp))
    got = run.result.isolated_growth
    assert expected > 0
    assert got.ci_lo <= expected <= got.ci_hi
    assert got.value == pytest.approx(expected, rel=0.2)


def test_no_edge_forecaster_loses_after_costs() -> None:
    """Reporting the market price plus noise (no information) loses money once the spread and fees are paid."""
    n = 4000
    w = binary_world(n, 0.6, seed=32)
    rng = np.random.default_rng(5)
    noisy = 1 / (1 + np.exp(-(np.log(w.q / (1 - w.q)) + 0.4 * rng.standard_normal(n))))
    qs = {f"q{i}": _quotes([0], [w.q[i] - 0.01], [w.q[i] + 0.01]) for i in range(n)}
    # one question at a time (each resolves before the next decision), so the bankroll path places every bet
    cands = [_cand(float(noisy[i]), float(w.y[i]), qid=f"q{i}", t=(3 * i + 1) * H, cluster=i) for i in range(n)]
    fees = FeeSchedule(quadratic=0.07, round_up_to=0.01)
    run = run_pnl(cands, qs, PnlSpec(kelly_fraction=0.25, fees=fees, bankroll=10_000.0), b=500, seed=2)
    assert run.result.n_bets > 1000
    assert run.result.n_skipped.get("insufficient_cash", 0) == 0 and not run.result.path_cash_limited
    assert run.result.isolated_growth.ci_hi < 0
    assert run.result.log_growth < 0 and run.result.total_return < 0
    assert run.result.fees_paid > 0 and run.result.spread_paid > 0
    sharp = [_cand(float(w.p[i]), float(w.y[i]), qid=f"q{i}", t=(3 * i + 1) * H, cluster=i) for i in range(n)]
    run_s = run_pnl(sharp, qs, PnlSpec(kelly_fraction=0.25, fees=fees, bankroll=10_000.0), b=500, seed=2)
    assert run_s.result.isolated_growth.ci_lo > 0
    assert run_s.result.log_growth > 0


def test_rows_and_skip_counts() -> None:
    qs = {"q0": _quotes([0], [0.4], [0.42])}
    cands = [_cand(0.9, 1.0, qid="q0"), _cand(0.9, 1.0, qid="q1"), _cand(0.41, 0.0, qid="q0", t=11 * H)]
    run = run_pnl(cands, qs, PnlSpec(), b=0)
    assert [r["status"] for r in run.rows] == ["bet", "no_quotes", "no_edge"]
    assert run.result.n_skipped == {"no_quotes": 1, "no_edge": 1}
    assert run.rows[0]["payout"] == run.rows[0]["contracts"]
    assert run.result.hit_rate == 1.0
    assert run.result.final_wealth == pytest.approx(1000 + run.rows[0]["contracts"] - run.rows[0]["cost"])


def test_positions_settling_together_make_one_equity_point() -> None:
    """Two full-Kelly bets at mid 0.5 with p 0.7 settle at the same instant, one lost and one won: equity goes from
    1000 to 1000, so the drawdown is 0 whichever question id sorts first."""
    qs = {q: _quotes([0], [0.5], [0.5]) for q in ("a", "b")}
    spec = PnlSpec(kelly_fraction=1.0, lot=0.0)
    for won, lost in (("a", "b"), ("b", "a")):
        run = run_pnl([_cand(0.7, 1.0, qid=won), _cand(0.7, 0.0, qid=lost)], qs, spec, b=0)
        assert run.result.n_bets == 2
        assert run.result.final_wealth == pytest.approx(1000.0)
        assert run.result.max_drawdown == 0.0
    # both lose at one instant: one drop, from 1000 to 200 (each staked 40% of equity at cost)
    both = run_pnl([_cand(0.7, 0.0, qid="a"), _cand(0.7, 0.0, qid="b")], qs, spec, b=0)
    assert both.result.final_wealth == pytest.approx(200.0)
    assert both.result.max_drawdown == pytest.approx(0.8)


def test_cash_limited_path_is_flagged() -> None:
    """Forty questions open at once with a large edge: quarter Kelly runs out of cash after about ten bets, so the
    path describes an arbitrary subset and says so; one question at a time never sets the flag."""
    qs = {f"q{i}": _quotes([0], [0.49], [0.51]) for i in range(40)}
    together = [_cand(0.9, 1.0, qid=f"q{i}") for i in range(40)]
    run = run_pnl(together, qs, PnlSpec(), b=0)
    assert run.result.n_skipped.get("insufficient_cash", 0) + run.result.cash_limited > 4
    assert run.result.path_cash_limited
    apart = [_cand(0.9, 1.0, qid=f"q{i}", t=(3 * i + 1) * H) for i in range(40)]
    assert not run_pnl(apart, qs, PnlSpec(), b=0).result.path_cash_limited


def test_frictionless_growth_uses_the_decision_mid() -> None:
    """With latency, the frictionless bettor still trades at the mid in force at the decision (no latency), so it
    stays equal to the log-score difference against that mid."""
    qs = {"q": _quotes([0, 10 * H + 5], [0.39, 0.79], [0.41, 0.81])}
    c = _cand(0.7, 1.0, t=10 * H)
    run = run_pnl([c], qs, PnlSpec(latency_ns=10), b=0)
    assert run.result.frictionless_growth == pytest.approx(math.log(0.7 / 0.40))
    assert run.rows[0]["status"] == "price_moved" and run.rows[0]["decision_quote_ts_ns"] == 0
