"""Kalshi Liquidity Incentive Program and Polymarket liquidity-reward formulas (`queuetruth.pm.rewards`), checked
by hand against the published rules (Kalshi LIP terms filed with the CFTC 2026-10-14; docs.polymarket.com
/programs/liquidity-rewards, read 2026-10-24)."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from queuetruth.errors import QtError
from queuetruth.pm.rewards import (
    LipProgram,
    PolyRewardParams,
    PriceGrid,
    adjusted_mid,
    competition_bounds,
    kalshi_expected_payout,
    kalshi_paid,
    lip_side,
    lip_snapshot,
    poly_expected_reward,
    poly_order_score,
    poly_q_min,
    poly_sample,
)

CENT = PriceGrid.uniform(100)


def prog(**kw) -> LipProgram:
    base = {
        "market_ticker": "T",
        "start_ns": 0,
        "end_ns": 3_600_000_000_000,
        "period_reward": Decimal(100),
        "target_size": 100,
        "discount_factor": Decimal("0.5"),
    }
    return LipProgram(**{**base, **kw})


# ---- price grid ---------------------------------------------------------------------------------------------------


def test_price_grid_ticks_across_bands() -> None:
    tails = PriceGrid(((0, 1000, 10), (1000, 9000, 100), (9000, 10_000, 10)))  # deci-cent tails
    assert tails.finest == 10
    assert tails.index(8900) == 100 + 79 and tails.index(9100) == 100 + 80 + 10
    assert tails.ticks(9100, 8900) == 11 and tails.ticks(8900, 9100) == 0
    assert CENT.ticks(5000, 4700) == 3 and CENT.on_grid(4550) is False
    with pytest.raises(QtError, match="not on the market's price grid"):
        CENT.index(4550)
    with pytest.raises(QtError, match="above"):
        PriceGrid(((0, 5000, 100),)).index(6000)
    for bad in [((0, 1000, 30),), ((0, 1000, 10), (2000, 3000, 10)), ()]:
        with pytest.raises(QtError):
            PriceGrid(bad)
    with pytest.raises(QtError):
        PriceGrid.uniform(300)


# ---- Kalshi LIP ---------------------------------------------------------------------------------------------------


def test_lip_side_reference_price_discount_and_target() -> None:
    # target 100: totals 10, 30 (>= 20 = one fifth: Reference Price 0.49), 80, 180 (>= 100: stop at 0.47)
    levels = [(5000, 10), (4900, 20), (4800, 50), (4700, 100), (4600, 999)]
    side = lip_side(levels, {4800: 10}, 100, 0.5, CENT)
    assert side.qualified and side.reference == 4900
    assert side.total == pytest.approx(10 + 20 + 0.5 * 50 + 0.25 * 100)  # 0.46 is past the target: not scored
    assert side.user == pytest.approx(0.5 * 10) and side.share == pytest.approx(5 / 80)
    short = lip_side([(5000, 10), (4900, 20)], {}, 100, 0.5, CENT)  # the levels run out before the target
    assert not short.qualified and short.share == 0.0 and short.reference is None
    first = lip_side([(5000, 100)], {5000: 40}, 100, 0.5, CENT)  # one level reaches both thresholds
    assert first.reference == 5000 and first.share == pytest.approx(0.4)
    with pytest.raises(QtError, match="exceeds the level size"):
        lip_side([(5000, 100)], {5000: 101}, 100, 0.5, CENT)


def test_lip_snapshot_counts_yes_asks_as_no_bids_and_needs_both_sides() -> None:
    p = prog()
    bids = [(4900, 100)]
    asks = [(5100, 60), (5200, 60)]  # NO bids at 0.49 (60) and 0.48 (60): reference 0.49, target at 0.48
    score, included = lip_snapshot(bids, asks, {4900: 50}, {5200: 30}, p, CENT)
    no_total = 60 + 0.5 * 60
    assert included and score == pytest.approx(0.5 + 0.5 * 30 / no_total)
    score, included = lip_snapshot(bids, [(5100, 60)], {4900: 50}, {}, p, CENT)
    assert (score, included) == (0.0, False)  # the NO side never reaches the target: snapshot excluded


def test_lip_payout_is_linear_in_the_summed_snapshot_scores() -> None:
    p = prog()  # one hour: 3,600 snapshots, $100
    assert p.total_snapshots == 3600
    # a user holding the whole of both sides (score 2) for the whole hour earns the whole reward
    assert kalshi_expected_payout(2.0 * 3600, p) == Decimal(100)
    assert kalshi_expected_payout(0.5 * 1800, p) == Decimal("12.5")
    assert prog(end_ns=1_500_000_000).total_snapshots == 2  # a partial second still has a snapshot
    with pytest.raises(QtError):
        kalshi_expected_payout(-1.0, p)


def test_lip_paid_floors_to_the_cent_caps_and_needs_a_dollar() -> None:
    p = prog(max_reward_per_account=Decimal(20))
    assert kalshi_paid(Decimal("12.349"), p) == Decimal("12.34")
    assert kalshi_paid(Decimal("25"), p) == Decimal(20)
    assert kalshi_paid(Decimal("0.999"), p) == 0


@pytest.mark.parametrize(
    "kw", [{"end_ns": 0}, {"target_size": 0}, {"discount_factor": Decimal("1.5")}, {"period_reward": Decimal(-1)}]
)
def test_lip_program_validation(kw: dict) -> None:
    with pytest.raises(QtError) as e:
        prog(**kw)
    assert e.value.code == "QT-CONFIG-001"


# ---- Polymarket ---------------------------------------------------------------------------------------------------


def test_order_score_function() -> None:
    assert poly_order_score(3, 1) == pytest.approx(4 / 9)
    assert poly_order_score(3, 0) == 1.0 and poly_order_score(3, 3) == 0.0 and poly_order_score(3, 4) == 0.0
    assert poly_order_score(3, 1, b=2.0) == pytest.approx(8 / 9)
    with pytest.raises(QtError):
        poly_order_score(3, -0.5)


def test_worked_example_side_scores_and_q_min() -> None:
    """The docs' worked example (adjusted midpoint 0.50, max spread 3 cents): Q_one 111.1, Q_two 175."""
    q_one = poly_order_score(3, 1) * 100 + poly_order_score(3, 2) * 200 + poly_order_score(3, 1) * 100
    q_two = poly_order_score(3, 1.5) * 100 + poly_order_score(3, 2) * 100 + poly_order_score(3, 0.5) * 200
    assert q_one == pytest.approx(1000 / 9) and q_two == pytest.approx(175.0)
    assert poly_q_min(q_one, q_two, 5000) == pytest.approx(1000 / 9)  # two-sided: the minimum
    assert poly_q_min(90.0, 0.0, 5000) == pytest.approx(30.0)  # one-sided in [0.10, 0.90]: divided by c = 3
    assert poly_q_min(90.0, 0.0, 950) == 0.0  # outside: must be two-sided
    assert poly_q_min(90.0, 0.0, 1000) == pytest.approx(30.0)  # 0.10 is inside the range


@pytest.mark.parametrize("c", [1.0, 1.5, 2.0, 3.0, 5.0])
def test_competition_bounds_hold_for_every_split_of_the_others(c: float) -> None:
    """The others' summed Q_min, for any split of their side scores over makers, lies in the bounds, for every c
    (the documented c is 3; under 2, one-sided makers beat any balanced maker)."""
    import random

    rng = random.Random(3)
    for mid in (5000, 500):
        for _ in range(300):
            n = rng.randint(1, 5)
            a = [rng.choice([0.0, rng.uniform(0, 50)]) for _ in range(n)]
            b = [rng.choice([0.0, rng.uniform(0, 50)]) for _ in range(n)]
            total = sum(poly_q_min(x, y, mid, c) for x, y in zip(a, b, strict=True))
            lo, pt, hi = competition_bounds(sum(a), sum(b), mid, c)
            assert lo - 1e-9 <= total <= hi + 1e-9
            assert lo - 1e-9 <= pt <= hi + 1e-9


@pytest.mark.parametrize(("c", "q1", "q2"), [(1.5, 10.0, 10.0), (3.0, 10.0, 10.0), (3.0, 40.0, 5.0), (1.0, 7.0, 2.0)])
def test_competition_bounds_are_reached(c: float, q1: float, q2: float) -> None:
    """Both ends are attained by some split, so the band is as narrow as an L2 book allows."""
    lo, _pt, hi = competition_bounds(q1, q2, 5000, c)
    long_, short = max(q1, q2), min(q1, q2)
    one_sided = poly_q_min(q1, 0.0, 5000, c) + poly_q_min(0.0, q2, 5000, c)
    balanced = poly_q_min(short, short, 5000, c) + poly_q_min(long_ - short, 0.0, 5000, c)
    assert hi == pytest.approx(max(one_sided, balanced))
    if long_ >= c * short:  # one maker holding everything scores the long side over c
        assert lo == pytest.approx(poly_q_min(q1, q2, 5000, c))
    else:  # makers whose sides are c to 1 apart: (c x, x) and (y, c y)
        x = (c * long_ - short) / (c * c - 1) if c > 1 else 0.0
        y = (c * short - long_) / (c * c - 1) if c > 1 else 0.0
        split = poly_q_min(c * x, x, 5000, c) + poly_q_min(y, c * y, 5000, c)
        assert lo == pytest.approx(split if c > 1 else (q1 + q2) / 2)


def test_adjusted_mid_skips_levels_under_the_min_size() -> None:
    bids, asks = [(4900, 5), (4800, 50)], [(5100, 50)]
    assert adjusted_mid(bids, asks, 10) == 4950.0
    assert adjusted_mid(bids, asks, 0) == 5000.0
    assert adjusted_mid(bids, [], 10) is None


def test_poly_sample_own_share_and_min_size() -> None:
    params = PolyRewardParams(3.0, 20, Decimal(50))
    bids = [(4900, 100), (4800, 100), (4500, 100)]  # 0.45 is 4.5 cents away: beyond the max spread
    asks = [(5100, 100), (5200, 40)]
    smp = poly_sample(bids, asks, {4900: [30, 10]}, {5100: [25]}, params)
    assert smp.mid == 5000.0
    s1 = poly_order_score(3, 1)
    assert smp.q_user == pytest.approx(min(30 * s1, 25 * s1))  # the 10-share order is under the min size
    lo, pt, hi = smp.share()
    assert 0 < lo <= pt <= hi < 1
    others_bid = (100 - 40) * s1 + 100 * poly_order_score(3, 2)
    others_ask = (100 - 25) * s1 + 40 * poly_order_score(3, 2)
    assert smp.rest_point == pytest.approx(poly_q_min(others_bid, others_ask, 5000))
    nothing = poly_sample(bids, asks, {}, {}, params)
    assert nothing.share() == (0.0, 0.0, 0.0)
    with pytest.raises(QtError, match="exceeds the level size"):
        poly_sample(bids, asks, {4900: [101]}, {}, params)
    assert poly_sample([], asks, {}, {}, params).mid is None


def test_poly_expected_reward_and_params() -> None:
    p = PolyRewardParams(3.0, 20, Decimal(48))
    assert poly_expected_reward(86_400.0, p) == Decimal(48)  # the whole share for a whole day
    assert poly_expected_reward(3600.0, p) == Decimal(2)
    assert poly_expected_reward(60.0, p, samples_per_day=60) == Decimal("0.8")
    for bad in (-1.0, math.inf):
        with pytest.raises(QtError):
            poly_expected_reward(bad, p)
    for kw in ({"max_spread_cents": 0}, {"min_size": -1}, {"daily_reward": Decimal(-1)}, {"c": 0.5}):
        with pytest.raises(QtError):
            PolyRewardParams(**{"max_spread_cents": 3.0, "min_size": 1, "daily_reward": Decimal(1), **kw})
