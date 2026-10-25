"""Implementation-shortfall decomposition of the sim-versus-live gap (`queuetruth.score.shortfall`,
`qt.score_shortfall`): every bucket in isolation, FIFO unit matching, the additive identity (exact in integer units,
to rounding in currency, randomized), unvalued orders, fees, totals with the clustered bootstrap, the scorer hook,
the synthetic live-pair benchmark and (opt-in) the RD-11 real orders."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from queuetruth import stats
from queuetruth.errors import QtError
from queuetruth.score import build_scorecard
from queuetruth.score._io import make_table, read_parquet, rows
from queuetruth.score.shortfall import (
    COMPONENTS,
    MARKET,
    NAME,
    SCHEMA,
    Marks,
    ShortfallFill,
    decompose_order,
    shortfall_rows,
    shortfall_table,
)
from queuetruth.score.statistics import block_labels
from score.qt_score_helpers import DAY0, MIDNIGHT, MS, T0, TICK, FakeRunner, S, log_from, make_dataset, order_events

PX = 100_000_000_000


def F(ts: int, qty: int, px: int | None = 100, liq: str = "maker", fee: float | None = None, ccy: str | None = None):
    return ShortfallFill(ts, qty, px, liq, fee, ccy)


def run(side, live, sim, pre, post, currency=None):
    return decompose_order(side, live, sim, lambda t: pre.get(t), lambda t: post.get(t), currency)


def only(res, **want):
    """Every market bucket is 0 except the ones named."""
    assert res.valued, res.note
    got = {k: v for k, v in res.units.items() if k != "total"}
    assert got == {**dict.fromkeys(MARKET, 0), **want}
    assert sum(got.values()) == res.units["total"]


# ---- each bucket in isolation -------------------------------------------------------------------------------------


def test_delay_only_late_taker():
    # buy 10: the sim takes the ask 101 at t=1 (mid 100.5); live arrives late, after the mid rose by 1, and takes
    # the ask 102 (mid 101.5); both are marked at 101.5. Same effective half-spread, same mark: all delay (Perold)
    res = run(1, [F(2, 10, 102, "taker")], [F(1, 10, 101, "taker")], pre={1: 201, 2: 203}, post={1: 203, 2: 203})
    only(res, delay=20)
    assert res.units["total"] == 10 * (203 - 202) - 10 * (203 - 204) == 20
    assert (res.live_qty, res.sim_qty, res.matched_qty) == (10, 10, 10)
    # with momentum (live's mark 102.5) the gap is 0: live paid the delay and the later mark gave it back
    res = run(1, [F(2, 10, 102, "taker")], [F(1, 10, 101, "taker")], pre={1: 201, 2: 203}, post={1: 203, 2: 205})
    only(res, delay=20, adverse_selection=-20)
    assert res.units["total"] == 0


def test_passive_same_price_fills_land_in_adverse_selection():
    # both arms rest at the bid 100; the sim fills at t=1 (mid 100.5), live at t=2 after the mid fell to 99.5. The
    # mid move between the fills is delay for live (-) and an equal effective-spread loss (+): they cancel, and the
    # whole difference is in the marks
    res = run(1, [F(2, 10)], [F(1, 10)], pre={1: 201, 2: 199}, post={1: 199, 2: 197})
    only(res, delay=-20, spread=20, adverse_selection=20)
    assert res.units["total"] == 10 * (199 - 200) - 10 * (197 - 200) == 20


def test_spread_only_taker_against_maker():
    # same time, live crossed the spread (paid the ask 101), the sim rested at the bid 100: the full spread per unit
    res = run(1, [F(1, 10, 101, "taker")], [F(1, 10, 100)], pre={1: 201}, post={1: 203})
    only(res, spread=20)  # 10 units x 2 x (101 - 100) half-price units = 10 x 1 price unit
    res_sell = run(-1, [F(1, 10, 100, "taker")], [F(1, 10, 101)], pre={1: 201}, post={1: 199})
    only(res_sell, spread=20)


def test_adverse_selection_only():
    # same price and same pre-fill mid, different post-fill drift: live's fill was followed by a drop
    res = run(1, [F(2, 10)], [F(1, 10)], pre={1: 201, 2: 201}, post={1: 205, 2: 199})
    only(res, adverse_selection=60)


def test_missed_fill_sim_only_and_live_only():
    # the sim claims a fill live never got: its markout is live's opportunity cost; no M(t-) needed
    res = run(1, [], [F(1, 10)], pre={}, post={1: 204})
    only(res, missed_fill=40)
    assert res.matched_qty == 0
    # live got a toxic fill the sim missed: the sim looks better by its loss
    res = run(1, [F(1, 10)], [], pre={}, post={1: 196})
    only(res, missed_fill=40)
    # a sell the sim missed that earned +3 per unit: the sim understates live
    res = run(-1, [F(1, 5)], [], pre={}, post={1: 197})
    only(res, missed_fill=-5 * 3)


def test_partial_fill_both_directions():
    res = run(1, [F(1, 4)], [F(1, 10)], pre={1: 201}, post={1: 203})
    only(res, partial_fill=6 * 3)
    assert res.matched_qty == 4
    res = run(1, [F(1, 10)], [F(1, 4)], pre={1: 201}, post={1: 203})
    only(res, partial_fill=-6 * 3)


def test_fees_only_and_rebates():
    res = run(1, [F(1, 10, fee=0.5)], [F(1, 10, fee=0.2)], pre={1: 201}, post={1: 203})
    only(res)
    assert res.fees == pytest.approx(0.3, abs=1e-15)
    # maker rebate in live (negative fee), taker-rate fee in the sim
    res = run(1, [F(1, 10, fee=-0.1)], [F(1, 10, fee=0.3)], pre={1: 201}, post={1: 203})
    assert res.fees == pytest.approx(-0.4, abs=1e-15)
    # null fees count as zero
    assert run(1, [F(1, 10)], [F(1, 10, fee=0.25)], pre={1: 201}, post={1: 203}).fees == -0.25


def test_fees_in_another_currency_are_left_out():
    res = run(1, [F(1, 10, fee=0.5, ccy="EUR")], [F(1, 10, fee=0.2, ccy="usd")], {1: 201}, {1: 203}, "USD")
    assert res.fees == -0.2 and res.foreign_fees == 1


def test_neither_filled_is_all_zero():
    res = run(1, [], [], pre={}, post={})
    only(res)
    assert res.units["total"] == 0 and res.fees == 0.0


# ---- FIFO matching -------------------------------------------------------------------------------------------------


def test_fifo_unit_matching_across_fill_boundaries():
    # live 3 @ t1 + 7 @ t3; sim 5 @ t2 + 5 @ t4: segments (L0,S0) 3, (L1,S0) 2, (L1,S1) 5
    pre = {1: 200, 2: 204, 3: 198, 4: 210}
    post = {1: 202, 2: 200, 3: 196, 4: 214}
    res = run(1, [F(3, 7), F(1, 3)], [F(4, 5), F(2, 5)], pre, post)  # input order is irrelevant: sorted by time
    delay = 3 * (200 - 204) + 2 * (198 - 204) + 5 * (198 - 210)
    adv = 3 * (200 - 202) + 2 * (200 - 196) + 5 * (214 - 196)
    only(res, delay=delay, spread=-delay, adverse_selection=adv)  # same price everywhere: spread = -delay
    total = 5 * (200 - 200) + 5 * (214 - 200) - (3 * (202 - 200) + 7 * (196 - 200))
    assert res.units["total"] == total == adv


def test_fifo_partial_remainder_inside_a_fill():
    # live 4; sim 3 @ t1 + 6 @ t2: matched 3 (S0) + 1 (S1); the partial bucket is the other 5 units of S1
    pre, post = {1: 200, 2: 202, 5: 201}, {1: 204, 2: 206, 5: 203}
    res = run(1, [F(5, 4)], [F(1, 3), F(2, 6)], pre, post)
    assert res.matched_qty == 4
    assert res.units["partial_fill"] == 5 * (206 - 200)
    assert res.units["delay"] == 3 * (201 - 200) + 1 * (201 - 202)
    assert sum(res.units[k] for k in MARKET) == res.units["total"]


def test_same_time_ties_keep_input_order():
    a = run(1, [F(1, 2, 100), F(1, 2, 99)], [F(1, 4, 100)], {1: 201}, {1: 203})
    # live's first two units (at 100) pair with the sim's at 100, the next two (at 99) pay 1 less
    assert a.units["spread"] == 2 * 2 * (99 - 100)


def test_sell_is_the_mirror_of_buy():
    pre = {1: 200, 2: 204, 3: 198}
    post = {1: 202, 2: 200, 3: 196}
    live, sim = [F(1, 3, 99), F(3, 7, 101, "taker")], [F(2, 6, 100)]
    b, s = run(1, live, sim, pre, post), run(-1, live, sim, pre, post)
    assert {k: -v for k, v in b.units.items()} == s.units


def test_zero_qty_fills_are_ignored_and_bad_side_raises():
    res = run(1, [F(1, 0), F(2, 10, 102)], [F(1, 10, 101)], {1: 201, 2: 203}, {1: 203, 2: 203})
    only(res, delay=20)
    with pytest.raises(QtError) as e:
        run(0, [], [], {}, {})
    assert e.value.code == "QT-INPUT-002"


# ---- the identity, randomized ----------------------------------------------------------------------------------------


def _independent_total(side, live, sim, post):
    def r(fs):
        return sum(f.qty * side * (post[f.ts] - 2 * f.px) for f in fs if f.qty > 0)

    return r(sim) - r(live)


def test_identity_holds_exactly_on_random_orders():
    rng = np.random.default_rng(20261024)
    n_valued = n_unvalued = 0
    for _ in range(3000):
        side = int(rng.choice([1, -1]))
        times = rng.choice(np.arange(1, 60), size=int(rng.integers(0, 9)), replace=False).tolist()
        cut = int(rng.integers(0, len(times) + 1))
        mk = lambda ts: [  # noqa: E731
            F(int(t), int(rng.integers(1, 9)), int(rng.integers(990, 1010)), str(rng.choice(["maker", "taker"])))
            for t in ts
        ]
        live, sim = mk(times[:cut]), mk(times[cut:])
        pre = {int(t): int(rng.integers(1970, 2030)) for t in times if rng.random() > 0.03}
        post = {int(t): int(rng.integers(1970, 2030)) for t in times if rng.random() > 0.03}
        res = run(side, live, sim, pre, post)
        if not res.valued:
            n_unvalued += 1
            assert res.reason in ("no mark at fill + horizon", "no quote before the fill")
            continue
        n_valued += 1
        assert sum(res.units[k] for k in MARKET) == res.units["total"] == _independent_total(side, live, sim, post)
        assert res.matched_qty == min(res.live_qty, res.sim_qty)
        if res.live_qty == 0 or res.sim_qty == 0:
            assert res.units["missed_fill"] == res.units["total"]
        else:
            assert res.units["missed_fill"] == 0
        if res.live_qty == res.sim_qty:
            assert res.units["partial_fill"] == 0
    assert n_valued > 2000 and n_unvalued > 50


def _reference(side, live, sim, pre, post):
    """Every bucket from single units: expand the fills into units (time order, ties in input order), pair the k-th
    units of the two arms and value each pair and each unpaired unit on its own."""

    def units(fs):
        return [f for f in sorted((f for f in fs if f.qty > 0), key=lambda f: f.ts) for _ in range(f.qty)]

    def val(f):
        return side * (post[f.ts] - 2 * f.px)

    ul, us = units(live), units(sim)
    u = dict.fromkeys(MARKET, 0)
    if not ul or not us:
        u["missed_fill"] = sum(map(val, us)) - sum(map(val, ul))
        return u
    m = min(len(ul), len(us))
    for a, b in zip(ul[:m], us[:m], strict=True):
        u["delay"] += side * (pre[a.ts] - pre[b.ts])
        u["spread"] += side * ((2 * a.px - pre[a.ts]) - (2 * b.px - pre[b.ts]))
        u["adverse_selection"] += side * (post[b.ts] - post[a.ts])
    u["partial_fill"] = sum(map(val, us[m:])) - sum(map(val, ul[m:]))
    return u


def test_every_bucket_matches_a_unit_by_unit_reference():
    rng = np.random.default_rng(424242)
    times = np.arange(1, 8)  # few times: ties within and across arms

    def mk(n):
        return [
            F(int(rng.choice(times)), int(rng.integers(0, 6)), int(rng.integers(995, 1005)),
              str(rng.choice(["maker", "taker"])))
            for _ in range(n)
        ]  # fmt: skip

    for _ in range(20_000):
        side = int(rng.choice([1, -1]))
        live, sim = mk(int(rng.integers(0, 5))), mk(int(rng.integers(0, 5)))
        pre = {int(t): int(rng.integers(1985, 2015)) for t in times}
        post = {int(t): int(rng.integers(1985, 2015)) for t in times}
        res = run(side, live, sim, pre, post)
        assert res.valued
        want = _reference(side, live, sim, pre, post)
        assert {k: res.units[k] for k in MARKET} == want, (side, live, sim, pre, post)
        assert res.units["total"] == _independent_total(side, live, sim, post)


def test_fifo_pairing_reads_unit_k_of_the_order():
    # live fills 5 @ t1 and 5 @ t2, the sim only 5 @ t2: FIFO pairs live's early fill with the sim's late one, so the
    # sim's fill reads as late (delay, spread) and differently marked (adverse selection), and live's t2 fill is the
    # partial; a time-aligned pairing would put all -20 in partial fills. The total is the same either way
    pre, post = {1: 200, 2: 210}, {1: 204, 2: 230}
    res = run(1, [F(1, 5), F(2, 5)], [F(2, 5)], pre, post)
    only(res, delay=5 * (200 - 210), spread=5 * (210 - 200), adverse_selection=5 * (230 - 204),
         partial_fill=-5 * (230 - 200))  # fmt: skip
    assert res.units["total"] == -5 * (204 - 200) == -20


# ---- unvalued orders -----------------------------------------------------------------------------------------------


def test_unvalued_reasons():
    res = run(1, [F(1, 10)], [F(2, 10)], pre={1: 201, 2: 201}, post={1: 203})
    assert not res.valued and res.reason == "no mark at fill + horizon" and "fill at 2" in res.note
    assert res.units == {} and res.matched_qty == 10
    # M(t-) is needed only for fills with matched units
    res = run(1, [F(1, 10)], [F(2, 10)], pre={2: 201}, post={1: 203, 2: 205})
    assert not res.valued and res.reason == "no quote before the fill"
    assert run(1, [], [F(2, 10)], pre={}, post={2: 205}).valued
    res = run(1, [F(1, 10, None)], [F(2, 10)], pre={1: 201, 2: 201}, post={1: 203, 2: 205})
    assert not res.valued and res.reason == "fill without price"


# ---- marks ---------------------------------------------------------------------------------------------------------


def _instruments(currency="USD", iid=1, pexp=-9, sexp=0, mult=1.0):
    return {
        "instrument_id": iid,
        "session_date": DAY0,
        "symbol": f"S{iid}",
        "venue": "generic",
        "source_venue": "x",
        "price_exp": pexp,
        "tick_size": TICK,
        "lot_size": 1,
        "size_exp": sexp,
        "contract_multiplier": mult,
        "currency": currency,
        "session_tz": "UTC",
        "midnight_utc_ns": MIDNIGHT,
        "open_s": 0,
        "close_s": 86_400,
        "source": "test",
    }


def _bbo(quotes, iid=1):
    return make_table(
        "qt.bbo",
        [
            {
                "ts_event": t,
                "instrument_id": iid,
                "sequence": i,
                "bid_px": b,
                "bid_sz": 1,
                "bid_ct": 1,
                "ask_px": a,
                "ask_sz": 1,
                "ask_ct": 1,
            }
            for i, (t, b, a) in enumerate(quotes)
        ],
    )


def test_marks_prevailing_quote_strictly_before_and_past_end():
    inst = make_table("qt.instruments", [_instruments()])
    bbo = _bbo([(T0, PX, PX + TICK), (T0 + S, PX + TICK, PX + 2 * TICK), (T0 + 2 * S, None, PX + TICK)])
    m = Marks(bbo, inst)
    assert m.pre(1, T0) is None  # nothing strictly before T0
    assert m.pre(1, T0 + S) == 2 * PX + TICK  # the T0 quote, not the one at T0 + S
    assert m.post(1, T0, S) == 2 * PX + 3 * TICK  # prevailing at T0 + S (rows at t <= T0 + S)
    assert m.post(1, T0, 2 * S) is None  # one side empty
    assert m.post(1, T0, 3 * S) is None  # past the last event (the last BBO row by default)
    m2 = Marks(bbo, inst, last_event_ts={(1, DAY0): T0 + 10 * S})
    assert m2.post(1, T0, 3 * S) is None  # the empty-sided quote prevails
    m3 = Marks(_bbo([(T0, PX, PX + TICK)]), inst, past_end="last_quote")
    assert m3.post(1, T0, 10 * S) == 2 * PX + TICK
    with pytest.raises(QtError) as e:
        Marks(bbo, inst, past_end="nope")
    assert e.value.code == "QT-CONFIG-001"


def test_marks_stay_exact_beyond_int64_mids():
    inst = make_table("qt.instruments", [_instruments()])
    big = 2**62 + 1
    m = Marks(_bbo([(T0, big, big + 2), (T0 + S, big + 2, big + 4)]), inst)
    assert m.pre(1, T0 + 1) == 2 * big + 2 and m.post(1, T0, S) == 2 * big + 6  # bid + ask > 2^63 - 1


def test_quote_groups_equal_the_markout_quotes():
    from queuetruth.score.markouts import InstrumentDays, _quotes
    from queuetruth.score.shortfall import quote_groups

    day = 86_400 * S
    inst = make_table(
        "qt.instruments",
        [
            _instruments("USD", 1),
            {**_instruments("USD", 1), "session_date": DAY0 + 1, "midnight_utc_ns": MIDNIGHT + day},
            _instruments("EUR", 2),
        ],
    )
    rng = np.random.default_rng(5)
    recs = []
    for i in range(400):
        t = MIDNIGHT - S + int(rng.choice([0, 3600 * S, day + 3600 * S])) + int(rng.integers(0, 50)) * MS  # ties
        b = None if rng.random() < 0.1 else PX + int(rng.integers(-3, 3)) * TICK
        recs.append(
            {"ts_event": t, "instrument_id": int(rng.choice([1, 2, 3])), "sequence": i, "bid_px": b,
             "bid_sz": int(rng.integers(0, 9)), "bid_ct": 1, "ask_px": PX + 3 * TICK, "ask_sz": 2, "ask_ct": 1}
        )  # fmt: skip
    bbo = make_table("qt.bbo", recs)
    days = InstrumentDays(inst)
    want, got = _quotes(bbo, days, None), quote_groups(bbo, inst, None)
    assert sorted(want) == sorted(got) and len(got) >= 3  # instrument 3 has no instrument rows: dropped
    for key, w in want.items():
        g = got[key]
        assert np.array_equal(g.ts, w.ts) and np.array_equal(g.ok, w.ok), key
        assert np.array_equal(g.mid2[g.ok], (w.bid + w.ask)[w.ok]), key
    one = make_table("qt.instruments", [_instruments()])
    no_iid = bbo.drop_columns(["instrument_id"])
    d1 = InstrumentDays(one)
    assert {k: v.ts.tolist() for k, v in _quotes(no_iid, d1, 1).items()} == {
        k: v.ts.tolist() for k, v in quote_groups(no_iid, one, 1).items()
    }
    with pytest.raises(QtError):
        quote_groups(no_iid, inst, None)


# ---- table level ---------------------------------------------------------------------------------------------------


def _order(key, side="buy", qty=10, t0=T0, iid=1, cls="both", lq=0, sq=0, limit=PX):
    return {
        "order_key": key,
        "instrument_id": iid,
        "session_date": DAY0,
        "side": side,
        "limit_px": limit,
        "qty": qty,
        "ts_entry": t0,
        "class": cls,
        "live_filled_qty": lq,
        "sim_filled_qty": sq,
        "local_ns": t0 - MIDNIGHT,
    }


def _sf(key, arm, ts, qty, px=PX, liq="maker", fee=None, ccy=None, iid=1, side="buy", seq=1):
    return {
        "order_key": key,
        "arm": arm,
        "fill_seq": seq,
        "ts": ts,
        "qty": qty,
        "px": px,
        "liquidity": liq,
        "side": side,
        "instrument_id": iid,
        "fee": fee,
        "fee_ccy": ccy,
    }


@pytest.fixture
def market():
    """A quote path every 100 ms around 100.00 with a 1-tick spread (mid moves by half ticks)."""
    rng = np.random.default_rng(7)
    q, b = [], PX
    for i in range(400):
        b += int(rng.integers(-1, 2)) * TICK
        q.append((T0 - S + i * 100 * MS, b, b + TICK))
    return _bbo(q), make_table("qt.instruments", [_instruments()])


def test_table_schema_rows_and_totals(market):
    bbo, inst = market
    orders = [
        _order("a", lq=10, sq=10),
        _order("b", side="sell", cls="sim_only", sq=5, t0=T0 + S),
        _order("c", cls="live_only", lq=4, t0=T0 + 2 * S),
        _order("d", cls="neither", t0=T0 + 3 * S),
    ]
    fills = [
        _sf("a", "live", T0 + 2 * S, 10, fee=0.01, ccy="USD"),
        _sf("a", "sim", T0 + S, 6, fee=0.002),
        _sf("a", "sim", T0 + 3 * S, 4, fee=0.001, seq=2),
        _sf("b", "sim", T0 + 4 * S, 5, px=PX + TICK, side="sell"),
        _sf("c", "live", T0 + 5 * S, 4),
        _sf("c", "sim", T0 + 6 * S, 2, liq="taker", px=PX + TICK),  # a taker fill counts as a fill
    ]
    t = shortfall_table(orders, fills, bbo, inst, [S, 10 * S], bootstrap={"b": 200, "seed": 3})
    assert t.schema.equals(SCHEMA, check_metadata=True)
    assert t.schema.metadata[b"qt.schema"] == NAME.encode()
    r = rows(t)
    tot = [x for x in r if x["scope"] == "total"]
    ords = [x for x in r if x["scope"] == "order"]
    assert [(x["horizon_ns"], x["statistic"]) for x in tot] == [
        (h, s) for h in (S, 10 * S) for s in ("sum", "mean", "ci_lo", "ci_hi")
    ]
    assert [(x["horizon_ns"], x["order_key"]) for x in ords] == [(h, k) for h in (S, 10 * S) for k in "abcd"]
    k = Fraction(10) ** -9 / 2
    for x in ords:
        assert x["valued"] and x["n"] == 1 and x["currency"] == "USD"
        comps = [x[f"{c}_ccy"] for c in COMPONENTS]
        assert math.fsum(comps) == pytest.approx(x["total_ccy"], rel=1e-12, abs=1e-12)
        units = [Fraction(v) / k for v in comps[:-1]]
        # every market bucket is an integer number of half-price x size units, rounded once to a float
        assert all(abs(u - round(u)) < Fraction(1, 10**6) for u in units)
        assert x["notional_ccy"] == pytest.approx(10 * 100.0)
    by = {(x["horizon_ns"], x["order_key"]): x for x in ords}
    assert by[(S, "c")]["live_qty"] == 4 and by[(S, "c")]["sim_qty"] == 2 and by[(S, "c")]["matched_qty"] == 2
    assert by[(S, "b")]["missed_fill_ccy"] == by[(S, "b")]["total_ccy"]
    assert by[(S, "d")]["total_ccy"] == 0.0
    assert by[(S, "a")]["fees_ccy"] == pytest.approx(0.01 - 0.003)
    for h in (S, 10 * S):
        s = next(x for x in tot if x["horizon_ns"] == h and x["statistic"] == "sum")
        mean = next(x for x in tot if x["horizon_ns"] == h and x["statistic"] == "mean")
        lo = next(x for x in tot if x["horizon_ns"] == h and x["statistic"] == "ci_lo")
        hi = next(x for x in tot if x["horizon_ns"] == h and x["statistic"] == "ci_hi")
        assert s["n"] == 4 and s["qty"] == 40 and s["live_qty"] == 14 and s["sim_qty"] == 17
        for c in [*COMPONENTS, "total"]:
            col = f"{c}_ccy"
            assert s[col] == math.fsum(by[(h, key)][col] for key in "abcd")
            assert mean[col] == pytest.approx(s[col] / 4)
            assert lo[col] <= hi[col]
        assert "block bootstrap" in s["note"] and "score_shortfall:USD:" in s["note"]


def test_ci_uses_the_scorer_block_bootstrap_draws(market):
    bbo, inst = market
    orders, fills = [], []
    for i in range(30):
        t0 = T0 + i * 300 * MS
        key = f"o{i:02d}"
        orders.append(_order(key, t0=t0, lq=5, sq=5))
        fills.append(_sf(key, "live", t0 + 200 * MS, 5))
        fills.append(_sf(key, "sim", t0 + (100 + 10 * (i % 7)) * MS, 5))
    # spread the orders over 3 half-hour blocks so the bootstrap has clusters
    for i, o in enumerate(orders):
        o["local_ns"] += (i % 3) * 1_800_000_000_000
    b, seed, h = 300, 11, S
    r = shortfall_rows(orders, fills, bbo, inst, [h], bootstrap={"b": b, "seed": seed, "block": "auto"})
    lo = next(x for x in r if x["statistic"] == "ci_lo")
    hi = next(x for x in r if x["statistic"] == "ci_hi")
    vals = {k: np.array([x[f"{k}_ccy"] for x in r if x["scope"] == "order"]) for k in ("delay", "total")}
    labels, kind = block_labels(orders, "auto")
    assert kind == "30min" and np.unique(labels).size == 3
    for k, x in vals.items():
        _, reps = stats.block_replicates(
            labels, lambda idx, x=x: float(x[idx].mean()), b, stats.bootstrap_seed(seed, f"score_shortfall:USD:{h}")
        )
        want = stats.percentile_ci(reps, 0.05)
        assert (lo[f"{k}_ccy"], hi[f"{k}_ccy"]) == pytest.approx(want, rel=1e-9, abs=1e-15)


def test_unpriced_live_fill_detected_from_score_orders(market):
    bbo, inst = market
    orders = [_order("a", lq=10, sq=10)]
    fills = [_sf("a", "live", T0 + S, 5), _sf("a", "sim", T0 + S, 10)]  # 5 of live's 10 have no price
    r = shortfall_rows(orders, fills, bbo, inst, [S], bootstrap={"b": 10})
    o = next(x for x in r if x["scope"] == "order")
    assert not o["valued"] and o["n"] == 0 and o["total_ccy"] is None
    assert o["note"] == "fill without price (live arm)"
    s = next(x for x in r if x["statistic"] == "sum")
    assert s["n"] == 0 and s["total_ccy"] is None and s["note"].startswith("1 of 1 order(s) unvalued")


def test_no_dataset_quotes_every_order_unvalued():
    r = shortfall_rows([_order("a", lq=1)], [_sf("a", "live", T0 + S, 1)], None, None, [S], bootstrap={"b": 5})
    o = next(x for x in r if x["scope"] == "order")
    assert not o["valued"] and o["note"] == "no dataset quotes" and o["currency"] is None
    assert o["live_qty"] == 1 and o["sim_qty"] == 0
    s = next(x for x in r if x["statistic"] == "sum")
    assert s["n"] == 0 and "no dataset quotes: 1" in s["note"]


def test_currencies_are_totalled_separately():
    inst = make_table("qt.instruments", [_instruments("USD", 1), _instruments("EUR", 2, pexp=-4, sexp=-2, mult=5.0)])
    q = [(T0 + i * S, PX, PX + TICK) for i in range(10)]
    bbo = pa.concat_tables([_bbo(q, 1), _bbo([(t, 10_000, 10_002) for t, _, _ in q], 2)])
    orders = [_order("u", lq=1), _order("e", iid=2, limit=10_000, sq=3)]
    fills = [_sf("u", "live", T0 + 2 * S, 1), _sf("e", "sim", T0 + 2 * S, 3, px=10_000, iid=2)]
    r = shortfall_rows(orders, fills, bbo, inst, [S], bootstrap={"b": 20})
    sums = {x["currency"]: x for x in r if x["statistic"] == "sum"}
    assert set(sums) == {"EUR", "USD"} and sums["EUR"]["n"] == sums["USD"]["n"] == 1
    # EUR: 3 units x (mid 10001 - 10000) x 10^-4 x 10^-2 x 5
    assert sums["EUR"]["missed_fill_ccy"] == pytest.approx(3 * 1 * 1e-6 * 5)
    assert sums["USD"]["missed_fill_ccy"] == pytest.approx(-(TICK / 2) * 1e-9)


def test_deterministic_and_bad_horizon(market):
    bbo, inst = market
    orders = [_order("a", lq=10, sq=10)]
    fills = [_sf("a", "live", T0 + 2 * S, 10), _sf("a", "sim", T0 + S, 10)]
    a = shortfall_table(orders, fills, bbo, inst, [S, S, 50 * MS], bootstrap={"b": 50})
    b = shortfall_table(list(reversed(orders)), list(reversed(fills)), bbo, inst, [50 * MS, S], bootstrap={"b": 50})
    assert a.equals(b) and a.num_rows == 2 * 4 + 2
    with pytest.raises(QtError) as e:
        shortfall_rows(orders, fills, bbo, inst, [-1])
    assert e.value.code == "QT-CONFIG-001"


def test_worked_example_of_the_how_to_page():
    """docs/how-to/decompose-the-sim-live-gap.md: buy 1,000 at 100.00 (cents, 1 cent tick), horizon 1 s."""
    inst = make_table("qt.instruments", [{**_instruments(), "price_exp": -2, "tick_size": 1}])
    bbo = _bbo([(T0, 10000, 10002), (T0 + 1500 * MS, 10000, 10001), (T0 + 2500 * MS, 10000, 10001),
                (T0 + 3200 * MS, 9999, 10000)])  # fmt: skip
    orders = [_order("ex", qty=1000, limit=10000, lq=400, sq=1000)]
    fills = [
        _sf("ex", "live", T0 + 3 * S, 400, px=10000, fee=-0.80, ccy="USD"),
        _sf("ex", "sim", T0 + 1 * S, 600, px=10000, fee=-1.20, ccy="USD"),
        _sf("ex", "sim", T0 + 2 * S, 400, px=10000, fee=-0.80, ccy="USD", seq=2),
    ]
    r = shortfall_rows(orders, fills, bbo, inst, [S], last_event_ts={(1, DAY0): T0 + 60 * S}, bootstrap={"b": 10})
    o = next(x for x in r if x["scope"] == "order")
    got = {c: o[f"{c}_ccy"] for c in (*COMPONENTS, "total")}
    want = {"delay": -2.0, "spread": 2.0, "missed_fill": 0.0, "adverse_selection": 4.0, "partial_fill": 3.0,
            "fees": 1.2, "total": 8.2}  # fmt: skip
    assert got == pytest.approx(want, abs=1e-12)
    assert (o["live_qty"], o["sim_qty"], o["matched_qty"], o["notional_ccy"]) == (400, 1000, 400, 100_000.0)
    assert (o["live_notional_ccy"], o["sim_notional_ccy"]) == (40_000.0, 100_000.0)
    # the page's per-arm results: R_sim = 5.00 gross + 2.00 rebates, R_live = -2.00 gross + 0.80 rebate
    gross = {}
    for arm, sign in (("live", -1), ("sim", 1)):  # one arm alone: missed_fill = +R_sim or -R_live, gross of fees
        one = [f for f in fills if f["arm"] == arm]
        r1 = shortfall_rows(orders, one, bbo, inst, [S], last_event_ts={(1, DAY0): T0 + 60 * S}, bootstrap={"b": 10},
                            unpriced_qty={})  # fmt: skip
        gross[arm] = sign * next(y for y in r1 if y["scope"] == "order")["missed_fill_ccy"]
    assert gross == pytest.approx({"sim": 5.0, "live": -2.0}, abs=1e-12)
    assert (gross["sim"] + 2.0) - (gross["live"] + 0.8) == pytest.approx(8.2, abs=1e-12)


def _one(r, key=None, h=None):
    return next(x for x in r if x["scope"] == "order" and (key is None or x["order_key"] == key)
                and (h is None or x["horizon_ns"] == h))  # fmt: skip


def _sum(r, h=None):
    return next(x for x in r if x["statistic"] == "sum" and (h is None or x["horizon_ns"] == h))


def test_instrument_not_in_the_dataset(market):
    bbo, inst = market
    r = shortfall_rows([_order("x", iid=9, lq=1)], [_sf("x", "live", T0 + S, 1, iid=9)], bbo, inst, [S],
                       bootstrap={"b": 5})  # fmt: skip
    o = _one(r)
    assert not o["valued"] and o["note"] == "instrument not in the dataset" and o["currency"] is None
    assert o["notional_ccy"] is None and o["live_qty"] == 1
    assert "instrument not in the dataset: 1" in _sum(r)["note"]


def test_fee_notes_foreign_and_unknown(market):
    bbo, inst = market
    orders = [_order("f", lq=10, sq=10), _order("u", lq=10, t0=T0 + S), _order("z", lq=10, sq=10)]
    fills = [
        _sf("f", "live", T0 + 2 * S, 10, fee=0.5, ccy="EUR"),
        _sf("f", "sim", T0 + 2 * S, 10, fee=0.2, ccy="USD"),
        # live paid a maker fee, the sim filled as taker where live has no taker rate: its fee is unknown
        _sf("u", "live", T0 + 2 * S, 10, fee=0.5, ccy="USD"),
        _sf("u", "sim", T0 + 2 * S, 10, liq="taker", px=PX + TICK),
        _sf("z", "live", T0 + 2 * S, 10),  # no fee anywhere on the order: nothing to flag
        _sf("z", "sim", T0 + 2 * S, 10),
    ]
    r = shortfall_rows(orders, fills, bbo, inst, [S], bootstrap={"b": 5})
    f, u, z = _one(r, "f"), _one(r, "u"), _one(r, "z")
    assert f["valued"] and f["fees_ccy"] == -0.2 and f["note"] == "1 fee(s) in another currency left out"
    assert u["valued"] and u["fees_ccy"] == 0.5 and u["note"] == "1 sim fill(s) without a fee counted as 0"
    assert z["valued"] and z["fees_ccy"] == 0.0 and z["note"] is None
    note = _sum(r)["note"]
    assert "1 fee(s) in another currency left out" in note and "fills without a fee counted as 0: sim 1" in note


def test_past_end_last_quote_through_shortfall_rows():
    inst = make_table("qt.instruments", [_instruments()])
    bbo = _bbo([(T0, PX, PX + TICK), (T0 + S, PX + TICK, PX + 2 * TICK)])
    orders, fills = [_order("a", sq=10)], [_sf("a", "sim", T0 + S // 2, 10)]
    r = shortfall_rows(orders, fills, bbo, inst, [10 * S], bootstrap={"b": 5})
    assert not _one(r)["valued"] and _one(r)["note"].startswith("no mark at fill + horizon")
    r = shortfall_rows(orders, fills, bbo, inst, [10 * S], past_end="last_quote", bootstrap={"b": 5})
    o = _one(r)
    assert o["valued"] and o["missed_fill_ccy"] == pytest.approx(10 * 1.5 * TICK * 1e-9)


def test_block_kind_is_chosen_once_over_all_orders():
    # 6 session dates (day blocks for the scorer); at 10 s only the orders of 2 dates can be marked, and the block
    # kind must stay `day` there instead of switching to 30-minute blocks
    inst = make_table("qt.instruments", [_instruments()])
    bbo = _bbo([(T0 - S + i * 100 * MS, PX + (i % 3) * TICK, PX + (i % 3 + 1) * TICK) for i in range(200)])
    orders, fills = [], []
    for i in range(24):
        key, t0 = f"o{i:02d}", T0 + (i % 4) * 100 * MS
        late = i % 6 >= 2  # dates 2..5 fill near the end of the quotes: no mark at +10 s
        orders.append({**_order(key, lq=2, sq=2, t0=t0), "session_date": DAY0 + i % 6})
        ft = T0 + (15 * S if late else S)
        fills += [_sf(key, "live", ft + 300 * MS, 2), _sf(key, "sim", ft + (i % 5) * 100 * MS, 2)]
    b, seed = 200, 4
    r = shortfall_rows(orders, fills, bbo, inst, [S, 10 * S], bootstrap={"b": b, "seed": seed})
    assert _sum(r, S)["n"] == 24 and _sum(r, 10 * S)["n"] == 8
    for h in (S, 10 * S):
        assert "day block(s)" in _sum(r, h)["note"]
    valued = [o for o in orders if o["session_date"] - DAY0 < 2]
    x = np.array([_one(r, o["order_key"], 10 * S)["total_ccy"] for o in valued])
    labels = np.array([o["session_date"] for o in valued])
    _, reps = stats.block_replicates(
        labels, lambda idx: float(x[idx].mean()), b, stats.bootstrap_seed(seed, f"score_shortfall:USD:{10 * S}")
    )
    lo = next(y for y in r if y["statistic"] == "ci_lo" and y["horizon_ns"] == 10 * S)
    hi = next(y for y in r if y["statistic"] == "ci_hi" and y["horizon_ns"] == 10 * S)
    assert (lo["total_ccy"], hi["total_ccy"]) == pytest.approx(stats.percentile_ci(reps, 0.05), rel=1e-9, abs=1e-15)


def test_notional_columns(market):
    bbo, inst = market
    orders = [_order("m", limit=0, lq=3, sq=2), _order("n", limit=None, lq=0, sq=0)]
    fills = [_sf("m", "live", T0 + S, 3, px=PX), _sf("m", "sim", T0 + S, 2, px=PX + TICK)]
    r = shortfall_rows(orders, fills, bbo, inst, [S], bootstrap={"b": 5})
    m, n = _one(r, "m"), _one(r, "n")
    assert m["notional_ccy"] is None and n["notional_ccy"] is None  # market order / no price: no bps denominator
    assert m["live_notional_ccy"] == pytest.approx(3 * 100.0) and m["sim_notional_ccy"] == pytest.approx(2 * 100.01)
    assert n["live_notional_ccy"] == n["sim_notional_ccy"] == 0.0
    s = _sum(r)
    assert s["notional_ccy"] is None and s["live_notional_ccy"] == pytest.approx(300.0)


def test_unpriced_taker_fill_from_the_scorer_and_scope_notes(market):
    bbo, inst = market
    orders = [_order("a", lq=0, sq=10), _order("b", lq=0, sq=0)]
    fills = [_sf("a", "sim", T0 + S, 10), _sf("zz", "live", T0 + S, 3), _sf("zz", "live", T0 + 2 * S, 1, seq=2)]
    # live's taker fill on `a` had no price: the scorer dropped it from the fills and passes its quantity
    r = shortfall_rows(orders, fills, bbo, inst, [S], bootstrap={"b": 5}, unpriced_qty={("a", "live"): 4},
                       n_unmatched_live=2)  # fmt: skip
    a = _one(r, "a")
    assert not a["valued"] and a["note"] == "fill without price (live arm)"
    note = _sum(r)["note"]
    assert "not decomposed: 2 live fill(s) of 1 order(s) outside the scored orders" in note
    assert "not decomposed: 2 live order(s) without a sim outcome" in note
    # without the scorer's counts, the taker fill is invisible (score_orders counts maker quantity only)
    assert _one(shortfall_rows(orders, fills, bbo, inst, [S], bootstrap={"b": 5}), "a")["valued"]


# ---- the scorer hook -------------------------------------------------------------------------------------------------


def _end_mbo(n_days=1):
    return [
        {
            "ts_event": MIDNIGHT + d * 86_400 * S + 23 * 3600 * S,
            "action": ord("N"),
            "side": ord("N"),
            "price": 2**63 - 1,
        }
        for d in range(n_days)
    ]


def _check_scorecard_dir(out: Path, expect_valued: bool = True) -> list[dict]:
    """Identity per row and agreement with qt.score_markouts (mid) and qt.score_fills fees."""
    sf = rows(read_parquet(out / "score_shortfall.parquet"))
    mk = rows(read_parquet(out / "score_markouts.parquet"))
    fl = rows(read_parquet(out / "score_fills.parquet"))
    mo: dict = {}
    for m in mk:
        if m["ref"] == "mid" and m["mo_ccy"] is not None:
            key = (m["order_key"], m["horizon_ns"], m["arm"])
            mo[key] = mo.get(key, 0.0) + m["mo_ccy"]
    fee: dict = {}
    for f in fl:
        if f["fee"] is not None:
            fee[(f["order_key"], f["arm"])] = fee.get((f["order_key"], f["arm"]), 0.0) + f["fee"]
    orders = [x for x in sf if x["scope"] == "order"]
    for x in orders:
        if not x["valued"]:
            assert not expect_valued, x
            continue
        comps = [x[f"{c}_ccy"] for c in COMPONENTS]
        assert math.fsum(comps) == pytest.approx(x["total_ccy"], rel=1e-12, abs=1e-12)
        k, h = x["order_key"], x["horizon_ns"]
        want = (mo.get((k, h, "sim"), 0.0) - fee.get((k, "sim"), 0.0)) - (
            mo.get((k, h, "live"), 0.0) - fee.get((k, "live"), 0.0)
        )
        assert x["total_ccy"] == pytest.approx(want, rel=1e-9, abs=1e-9)
    return sf


def test_scorer_writes_score_shortfall(tmp_path):
    oe = [
        order_events("o1", "buy", PX, 10, T0, fills=[(T0 + 2 * S, 10)]),
        order_events("o2", "buy", PX, 10, T0 + S, cancel_at=T0 + 30 * S),
        order_events("o3", "sell", PX + TICK, 5, T0 + 2 * S, fills=[(T0 + 5 * S, 5)]),
        order_events("o4", "buy", PX, 3, T0 + 3 * S, fills=[(T0 + 7 * S, 1)]),
    ]
    live = log_from(oe, tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    quotes = [{"ts_event": T0 - S + i * 500 * MS, "bid_px": PX + (i % 3 - 1) * TICK, "bid_sz": 5,
               "ask_px": PX + (i % 3) * TICK, "ask_sz": 5} for i in range(200)]  # fmt: skip
    fr = FakeRunner(
        sim_fills={"o1": [(T0 + 1 * S, 10, "queue_depleted")], "o4": [(T0 + 4 * S, 3, "queue_depleted")]},
        bbo=quotes,
    )
    out = tmp_path / "out"
    spec = {"live": {"path": str(live)}, "sim": {"kind": "exact"}, "dataset": {"path": str(ds)}, "out": str(out),
            "bootstrap": {"b": 50}, "perm_b": 50, "markout_horizons_ns": [S, 10 * S]}  # fmt: skip
    s = build_scorecard(spec, runner=fr)
    entry = next(f for f in s.files if f.name == "score_shortfall.parquet")
    t = pq.read_table(out / "score_shortfall.parquet")
    assert t.schema.equals(SCHEMA, check_metadata=True) and entry.rows == t.num_rows
    sf = _check_scorecard_dir(out)
    by = {(x["order_key"], x["horizon_ns"]): x for x in sf if x["scope"] == "order"}
    assert by[("o3", S)]["class"] == "live_only" and by[("o3", S)]["missed_fill_ccy"] == by[("o3", S)]["total_ccy"]
    assert by[("o4", S)]["partial_fill_ccy"] != 0 and by[("o4", S)]["matched_qty"] == 1
    assert by[("o1", S)]["delay_ccy"] != 0 and by[("o1", S)]["missed_fill_ccy"] == 0
    assert by[("o2", S)]["total_ccy"] == 0
    # outputs stay deterministic
    s2 = build_scorecard({**spec, "out": str(tmp_path / "out2")}, runner=fr)
    assert [f.sha256 for f in s.files] == [f.sha256 for f in s2.files]


def test_scorer_without_dataset_marks_orders_unvalued(tmp_path):
    oe = [order_events("o1", "buy", PX, 10, T0, fills=[(T0 + 2 * S, 10)])]
    live = log_from(oe, tmp_path / "live")
    sim = log_from([order_events("o1", "buy", PX, 10, T0, fills=[(T0 + S, 10)])], tmp_path / "sim")
    spec = {"live": {"path": str(live)}, "sim": {"kind": "external", "log": {"path": str(sim)}},
            "out": str(tmp_path / "out"), "bootstrap": {"b": 20}, "perm_b": 20}  # fmt: skip
    build_scorecard(spec, runner=FakeRunner())
    sf = rows(read_parquet(tmp_path / "out" / "score_shortfall.parquet"))
    assert all(not x["valued"] and x["note"] == "no dataset quotes" for x in sf if x["scope"] == "order")
    assert {x["n"] for x in sf if x["scope"] == "total"} == {0}


def test_scorer_passes_unpriced_taker_fills(tmp_path):
    o1 = order_events("o1", "buy", PX, 10, T0, fills=[(T0 + 2 * S, 10)], liquidity="taker")
    o1[1][-1]["px"] = None  # a taker fill without a price: dropped from qt.score_fills, not in score_orders
    o2 = order_events("o2", "buy", PX, 10, T0 + S, fills=[(T0 + 3 * S, 10)])
    live = log_from([o1, o2], tmp_path / "live")
    sim = log_from([order_events(k, "buy", PX, 10, t, fills=[(t + S, 10)]) for k, t in (("o1", T0), ("o2", T0 + S))],
                   tmp_path / "sim")  # fmt: skip
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    quotes = [{"ts_event": T0 - S + i * 500 * MS, "bid_px": PX, "bid_sz": 5, "ask_px": PX + TICK, "ask_sz": 5}
              for i in range(40)]  # fmt: skip
    spec = {"live": {"path": str(live)}, "sim": {"kind": "external", "log": {"path": str(sim)}},
            "dataset": {"path": str(ds)}, "out": str(tmp_path / "out"), "bootstrap": {"b": 20}, "perm_b": 20,
            "markout_horizons_ns": [S]}  # fmt: skip
    build_scorecard(spec, runner=FakeRunner(bbo=quotes))
    sf = rows(read_parquet(tmp_path / "out" / "score_shortfall.parquet"))
    by = {x["order_key"]: x for x in sf if x["scope"] == "order"}
    assert not by["o1"]["valued"] and by["o1"]["note"] == "fill without price (live arm)"
    assert by["o2"]["valued"]


# ---- the synthetic live-pair benchmark -----------------------------------------------------------------------------


def _live_pair(tmp_path: Path, n: int, monkeypatch) -> tuple[Path, Path]:
    """benches/make_live_pair.py as the benchmark runs it (sim = live with every 10th fill dropped, +1 ms)."""
    path = Path(__file__).resolve().parents[3] / "benches" / "make_live_pair.py"
    spec = importlib.util.spec_from_file_location("qt_make_live_pair", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    live, sim = tmp_path / "live", tmp_path / "sim"
    monkeypatch.setattr(sys, "argv", ["make_live_pair", "--n", str(n), "--live", str(live), "--sim", str(sim)])
    mod.main()
    return live, sim


def test_live_pair_benchmark_identity_and_buckets(tmp_path, monkeypatch, capsys):
    n = 300
    live, sim = _live_pair(tmp_path, n, monkeypatch)
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["live_orders"] == n
    t0 = 1_767_623_400_000_000_000  # synthetic_log's default first order (2026-01-05 14:30 UTC)
    ds = make_dataset(tmp_path / "ds", _end_mbo(5), n_days=5)
    rng = np.random.default_rng(1)
    quotes, b = [], PX
    for i in range((n + 400) * 20):
        b += int(rng.choice([-1, 0, 0, 0, 1])) * TICK
        quotes.append(
            {"ts_event": t0 - 10 * S + i * 50 * MS, "bid_px": b, "bid_sz": 3, "ask_px": b + TICK, "ask_sz": 3}
        )
    out = tmp_path / "out"
    spec = {"live": {"path": str(live)}, "sim": {"kind": "external", "log": {"path": str(sim)}},
            "dataset": {"path": str(ds)}, "out": str(out), "bootstrap": {"b": 100}, "perm_b": 50}  # fmt: skip
    s = build_scorecard(spec, runner=FakeRunner(bbo=quotes))
    assert s.n_matched > 200
    sf = _check_scorecard_dir(out)
    sums = {x["horizon_ns"]: x for x in sf if x["scope"] == "total" and x["statistic"] == "sum"}
    one = sums[1_000_000_000]
    assert one["n"] == s.n_matched
    # dropped fills: some orders lost all their fills (missed) and some lost one of several (partial); the 1 ms
    # shift moves fill times (delay / adverse selection); prices are equal (no spread); the dropped fills' fees
    assert one["missed_fill_ccy"] != 0 and one["partial_fill_ccy"] != 0 and one["delay_ccy"] != 0
    assert one["fees_ccy"] != 0
    # every matched pair is two passive fills at the same price: delay and spread cancel order by order
    for x in sf:
        if x["scope"] == "order" and x["valued"]:
            assert x["spread_ccy"] == -x["delay_ccy"], x
    assert one["sim_qty"] < one["live_qty"]


# ---- RD-11 real orders (opt-in) --------------------------------------------------------------------------------------


def _realdata() -> Path | None:
    root = Path(os.environ.get("QT_REALDATA", Path(__file__).resolve().parents[3] / ".realdata"))
    ok = (root / "live" / "d1-et3000").is_dir() and (root / "ds" / "nasdaq-2019-12-30").is_dir()
    return root if ok else None


@pytest.mark.integration
@pytest.mark.realdata
@pytest.mark.requires_core
@pytest.mark.skipif(_realdata() is None, reason="RD-11 data not found (set QT_REALDATA)")
@pytest.mark.parametrize("self_exclude", [True, False])
def test_rd11_real_orders_identity(tmp_path, self_exclude):
    root = _realdata()
    out = tmp_path / "out"
    spec = {"live": {"path": str(root / "live" / "d1-et3000")}, "sim": {"kind": "exact"},  # type: ignore[operator]
            "dataset": {"path": str(root / "ds" / "nasdaq-2019-12-30")}, "out": str(out),  # type: ignore[operator]
            "self_exclude": self_exclude, "bootstrap": {"b": 200}}  # fmt: skip
    s = build_scorecard(spec)
    sf = _check_scorecard_dir(out)
    orders = [x for x in sf if x["scope"] == "order"]
    assert len(orders) == 7 * s.n_matched and all(x["valued"] for x in orders)
    if self_exclude:  # the exact arm reproduces every live fill: every bucket is exactly 0
        assert all(x[f"{c}_ccy"] == 0 for x in orders for c in [*COMPONENTS, "total"])
    else:  # the sim leg queues behind its own twin: live-only orders and short sim fills
        assert any(x["missed_fill_ccy"] for x in orders) and any(x["partial_fill_ccy"] for x in orders)
        # every matched pair is two passive fills at the same price: delay and spread cancel order by order
        assert any(x["delay_ccy"] for x in orders) and all(x["spread_ccy"] == -x["delay_ccy"] for x in orders)
