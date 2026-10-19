"""Scorer fixes from the trader UX review (F1, F3, F4, F6, F8, F10, B8) and the Python analytics review (F3, F4, F7,
F10, F11, F13); each `exp_*.py` reproducer of the review is ported here.

- units: live price/size exponents converted to the dataset's (QT-INPUT-004 when not exact);
- markouts in bps and net of fees (`qt.score_tests` rows);
- a scorecard that scores nothing is an error with the reasons counted ("NOT SCORED: ...");
- the headline states H and "no gates configured", without double negatives;
- referee aggregates legs per order for order-keyed simulator logs;
- self-exclusion is scoped to the order's instrument-day and checks side/price on by-id matches;
- a self-trade keeps both fills; a fill without a price keeps its quantity;
- a re-priced leg that `to_schedule` drops (live fills reached the new total) is restored."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from queuetruth import live as qlive
from queuetruth.errors import QtError
from queuetruth.schemas.io import write_table
from queuetruth.score import build_scorecard
from queuetruth.score._io import make_table, read_live_log, read_parquet, rows
from queuetruth.score.outcomes import log_outcomes, merge_legs
from queuetruth.score.referee import compare_referee
from queuetruth.score.schedule import instruction_legs
from queuetruth.score.scorecard import exclusion_items
from queuetruth.score.selfexclude import filtered_dataset, match_twins
from queuetruth.score.units import rescale
from score.qt_score_helpers import (
    DAY,
    DAY0,
    MIDNIGHT,
    MS,
    T0,
    TICK,
    FakeRunner,
    S,
    log_from,
    make_dataset,
    order_events,
)

PX9 = 100_000_000_000  # 100.00 at price_exp -9 (dataset)
PX8 = 10_000_000_000  # 100.00 at price_exp -8
BBO = [{"ts_event": T0 - S, "bid_px": PX9, "bid_sz": 5, "ask_px": PX9 + TICK, "ask_sz": 5}]


def _end_mbo():
    return [{"ts_event": T0 + 3600 * S, "action": ord("N"), "side": ord("N"), "price": 2**63 - 1}]


def _spec(live, ds=None, out=None, **kw):
    d = {"live": {"path": str(live)}, "sim": {"kind": "exact"}, "bootstrap": {"b": 30}, "perm_b": 30}
    if ds is not None:
        d["dataset"] = {"path": str(ds)}
    if out is not None:
        d["out"] = str(out)
    d.update(kw)
    return d


def _set_size_exp(ds: Path, size_exp: int) -> None:
    t = read_parquet(ds / "instruments.parquet")
    r = rows(t)
    for x in r:
        x["size_exp"] = size_exp
    write_table(make_table("qt.instruments", r), ds / "instruments.parquet")


# ---- units (trader F4 / analytics F3) -------------------------------------------------------------------------------


def test_price_exp_mismatch_is_converted(tmp_path):
    """Port of `exp_price_exp_mismatch.py`: live log at price_exp -8, dataset at -9 (both 100.00)."""
    o, ev = order_events("o1", "buy", PX8, 10, T0, fills=[(T0 + 2 * S, 10)])
    o["price_exp"] = -8
    for e in ev:
        if e.get("px") is not None:
            e["px"] = PX8
    live = log_from([(o, ev)], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(sim_fills={"o1": [(T0 + 3 * S, 10, "queue_depleted")]}, bbo=BBO)
    s = build_scorecard(_spec(live, ds, tmp_path / "out"), runner=fr)
    assert fr.schedules[0][0]["price"] == PX9  # dataset scale reaches the engine
    m = {r.arm: r.mean_ticks for r in s.markouts if r.horizon_ns == S and r.ref == "mid"}
    assert m == {"live": 0.5, "sim": 0.5}  # was +9000.5 ticks before the fix
    assert any(w.startswith("units: live log instrument 1 price_exp -8 -> -9") for w in s.warnings)
    so = rows(read_parquet(tmp_path / "out" / "score_orders.parquet"))[0]
    assert so["limit_px"] == PX9


def test_size_exp_mismatch_queue_and_currency_markouts(tmp_path):
    """Trader F4: live size_exp 0 (1 lot) vs dataset size_exp -8 -> qty 1e8 dataset units; mo_ccy in currency."""
    o, ev = order_events("o1", "buy", PX9, 1, T0, fills=[(T0 + 2 * S, 1)])
    live = log_from([(o, ev)], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    _set_size_exp(ds, -8)
    fr = FakeRunner(
        sim_fills={"o1": [(T0 + 3 * S, 100_000_000, "queue_depleted")]}, bbo=BBO, queue_ahead={"o1": 500_000_000}
    )
    build_scorecard(_spec(live, ds, tmp_path / "out"), runner=fr)
    assert fr.schedules[0][0]["qty"] == 100_000_000
    so = rows(read_parquet(tmp_path / "out" / "score_orders.parquet"))[0]
    assert so["qty"] == 100_000_000 and so["queue_ahead_at_entry"] == 500_000_000
    assert so["live_filled_qty"] == so["sim_filled_qty"] == 100_000_000 and so["class"] == "both"
    mk = rows(read_parquet(tmp_path / "out" / "score_markouts.parquet"))
    live_1s = next(r for r in mk if r["arm"] == "live" and r["horizon_ns"] == S)
    assert live_1s["mo_ccy"] == pytest.approx(0.005)  # half a 0.01 tick on 1 unit (was 5e-11)


def test_unit_conversion_not_exact_is_qt_input_004(tmp_path):
    o, ev = order_events("o1", "buy", 1_000_000_000_001, 10, T0)
    o["price_exp"] = -10  # 100.0000000001 is not representable at the dataset's price_exp -9
    live = log_from([(o, ev)], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    with pytest.raises(QtError) as e:
        build_scorecard(_spec(live, ds), runner=FakeRunner(bbo=BBO))
    assert e.value.code == "QT-INPUT-004" and "instrument 1" in str(e.value) and "exponent -10" in str(e.value)
    assert rescale(1234, -8, -9, "x") == 12340 and rescale(12340, -9, -8, "x") == 1234
    with pytest.raises(QtError):
        rescale(12345, -9, -8, "x")


# ---- markouts in bps and net of fees (trader F10) -------------------------------------------------------------------


def _fee_case(tmp_path, fee_ccy: str):
    o, ev = order_events("o1", "buy", PX9, 10, T0, fills=[(T0 + 2 * S, 10)])
    for e in ev:
        if e["event_type"] == "fill":
            e["fee"], e["fee_ccy"] = 0.25, fee_ccy
    live = log_from([(o, ev)], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(sim_fills={"o1": [(T0 + 3 * S, 10, "queue_depleted")]}, bbo=BBO)
    return build_scorecard(_spec(live, ds, tmp_path / "out", markout_horizons_ns=[S]), runner=fr)


def test_markouts_bps_and_net_of_fees(tmp_path):
    _fee_case(tmp_path, "USD")
    tests = rows(read_parquet(tmp_path / "out" / "score_tests.parquet"))
    bps = {t["stratum_kind"]: t["value"] for t in tests if t["test_id"] == "markout_mean_bps"}
    net = {t["stratum_kind"]: t["value"] for t in tests if t["test_id"] == "markout_mean_net_ccy"}
    # mid = 100.005 vs fill 100.00: 0.5 bps; currency 0.005 x 10 = 0.05, minus the 0.25 fee
    assert bps == {"live": pytest.approx(0.5), "sim": pytest.approx(0.5)}
    assert net == {"live": pytest.approx(0.05 - 0.25), "sim": pytest.approx(0.05 - 0.25)}  # sim at the live rate
    row = next(t for t in tests if t["test_id"] == "markout_mean_bps")
    assert row["statistic"] == "mean_bps" and row["stratum"] == "mid:1000000000" and row["n"] == 1


def test_fees_in_another_currency_are_not_netted(tmp_path):
    s = _fee_case(tmp_path, "BNB")
    tests = rows(read_parquet(tmp_path / "out" / "score_tests.parquet"))
    assert not [t for t in tests if t["test_id"] == "markout_mean_net_ccy"]
    assert [t["stratum_kind"] for t in tests if t["test_id"] == "markout_mean_bps"] == ["live", "sim"]
    assert any(w.startswith("fees: 1 live fill(s) charged in BNB") for w in s.warnings)


def test_score_tables_have_readable_sides(tmp_path):
    """Trader B8: `side` is a dictionary string in qt.score_orders and qt.score_fills (1.1 layout)."""
    live = log_from([order_events("o1", "sell", PX9, 3, T0, fills=[(T0 + S, 3)])], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    build_scorecard(_spec(live, ds, tmp_path / "out"), runner=FakeRunner(bbo=BBO))
    for name in ("score_orders", "score_fills"):
        t = pq.read_table(tmp_path / "out" / f"{name}.parquet")
        assert pa.types.is_dictionary(t.schema.field("side").type)
        assert t.column("side").to_pylist() == ["sell"]


# ---- nothing scored (trader F1) -------------------------------------------------------------------------------------


def test_zero_scored_orders_is_an_error_with_reasons(tmp_path):
    a, ea = order_events("a", "buy", PX9, 1, T0)
    a["instrument_id"] = None
    b, eb = order_events("b", "buy", PX9, 1, T0, tif="ioc")
    c, ec = order_events("c", "buy", PX9, 1, T0 + S)
    c["instrument_id"] = None
    live = log_from([(a, ea), (b, eb), (c, ec)], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    with pytest.raises(QtError) as e:
        build_scorecard(_spec(live, ds, tmp_path / "out"), runner=FakeRunner(bbo=BBO))
    msg = str(e.value)
    assert e.value.code == "QT-INPUT-002"
    assert "NOT SCORED: 0 of 3 live orders scored (2 missing instrument_id: pass --instrument-map; 1 IOC" in msg
    assert not (tmp_path / "out" / "summary.json").exists()


def test_zero_scored_external_arm_names_the_sim_log(tmp_path):
    live = log_from([order_events("o1", "buy", PX9, 1, T0)], tmp_path / "live")
    sim = log_from([order_events("other", "buy", PX9, 1, T0, venue="sim:hbt")], tmp_path / "sim")
    with pytest.raises(QtError) as e:
        build_scorecard(_spec(live, sim={"kind": "external", "log": {"path": str(sim)}}), runner=FakeRunner())
    assert "0 of 1 live orders scored (1 missing from the simulator log)" in str(e.value)


def test_not_scheduled_warning_names_reasons(tmp_path):
    a, ea = order_events("a", "buy", PX9, 1, T0)
    a["instrument_id"] = None
    live = log_from([(a, ea), order_events("b", "buy", PX9, 1, T0)], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    s = build_scorecard(_spec(live, ds), runner=FakeRunner(bbo=BBO))
    assert s.n_matched == 1
    assert "not_scheduled: 1 live order(s) not schedulable (1 missing instrument_id: pass --instrument-map)" in (
        s.warnings
    )


# ---- headline (trader F3, F6, F8) -----------------------------------------------------------------------------------


def test_headline_window_and_gates(tmp_path):
    oe = [order_events(f"o{i}", "buy", PX9, 1, T0 + i * S, fills=[(T0 + i * S + MS, 1)]) for i in range(4)]
    live = log_from(oe, tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(bbo=BBO)  # the sim never fills: underfills
    s = build_scorecard(_spec(live, ds), runner=fr)
    h = s.verdict.headline
    assert "fill rate within 60 s" in h and h.endswith("; no gates configured") and s.verdict.gates_passed is True
    if s.bias.direction == "sim_underfills":
        assert "underfills live by 100.0 pp" in h and "-100.0 pp" not in h
    g = build_scorecard(_spec(live, ds, gates={"max_abs_fill_rate_bias": 0.5}), runner=fr)
    assert "no gates configured" not in g.verdict.headline and g.verdict.gates_passed is False


# ---- referee legs (analytics F4) ------------------------------------------------------------------------------------


def test_referee_aggregates_legs_for_order_keyed_logs():
    """Port of `exp_referee_legs.py`: identical 5 + 5 fills on two legs of order K pass, one order compared."""
    t = 1_767_261_600_000_000_000
    results = pa.table(
        {
            "shadow_id": ["K", "K.leg2"],
            "filled_qty": [5, 5],
            "ts_first_fill": [t + S, t + 3 * S],
            "ts_submit": [t, t + 2 * S],
            "end_state": ["cancelled", "filled"],
            "first_fill_reason": ["queue_depleted", "queue_depleted"],
        }
    )
    fills = pa.table({"shadow_id": ["K", "K.leg2"], "fill_qty": [5, 5], "ts_event": [t + S, t + 3 * S]})
    schedule = pa.table({"shadow_id": ["K", "K.leg2"], "order_key": ["K", "K"]})
    orders = pa.table({"order_key": ["K"], "venue": ["sim:hftbacktest"], "instrument": ["X"], "orig_qty": [10]})
    events = pa.table(
        {
            "order_key": ["K", "K"],
            "event_seq": [1, 2],
            "event_type": ["fill", "fill"],
            "qty": [5, 5],
            "px": [100, 101],
            "fill_id": ["a", "b"],
            "ts_exchange": [t + S, t + 3 * S],
        }
    )
    tol = {"max_discordant": 0, "max_qty_mismatch": 0, "max_abs_ttf_err_ns": 0}
    rep = compare_referee(results, fills, (orders, events), tol, schedule=schedule)
    assert rep.pass_ is True and rep.qty_mismatch == 0 and rep.n_orders == 1 and rep.max_abs_ttf_err_ns == 0
    # a shadow_id-keyed simulator (fills table) is still compared leg by leg
    per_leg = compare_referee(results, fills, fills, tol, schedule=schedule)
    assert per_leg.pass_ is True and per_leg.n_orders == 2
    # the exact arm fills only the first leg: one order, qty mismatch (not one sim_only per leg)
    short = results.set_column(1, "filled_qty", pa.array([5, 0])).set_column(
        2, "ts_first_fill", pa.array([t + S, None], pa.int64())
    )
    rep2 = compare_referee(short, fills.slice(0, 1), (orders, events), tol, schedule=schedule)
    assert rep2.n_orders == 1 and rep2.qty_mismatch == 1 and rep2.discordant.sim_only == 0


# ---- self-exclusion across days (analytics F7) ----------------------------------------------------------------------


def _days_tape():
    px = 100 * TICK
    day1 = MIDNIGHT + 10 * 3600 * S
    day2 = MIDNIGHT + DAY + 10 * 3600 * S

    def rec(ts, side, size, oid):
        return {"ts_event": ts, "action": ord("A"), "side": ord(side), "price": px, "size": size, "order_id": oid}

    return px, day1, day2, [rec(day1, "B", 500, 42), rec(day1 + S, "B", 100, 7), rec(day2, "B", 100, 42)]


def test_self_exclusion_scoped_to_session_date(tmp_path):
    """Port of `exp_selfexclude_days.py`: order ids restart daily; day 1's 500-lot with id 42 is not the twin."""
    px, _, day2, mbo = _days_tape()
    ds = make_dataset(tmp_path / "ds", mbo, n_days=2)
    tw = match_twins(
        ds,
        [
            {
                "order_key": "L1",
                "instrument_id": 1,
                "side": "buy",
                "price": px,
                "qty": 100,
                "ts_entry": day2,
                "venue_order_id": "42",
            }
        ],
    )
    assert tw.by_id == 1 and tw.days == {(1, DAY0 + 1): {42}}
    assert exclusion_items(tw.ids) == [{"instrument_id": 1, "order_id": 42}]
    assert tw.collisions == {1: {42}} and "also occur on other session dates" in (tw.collision_warning() or "")
    out = filtered_dataset(ds, tmp_path / "filt", tw.ids, by_day=tw.days)
    d0 = pq.read_table(out / "mbo/date=day0/instrument_id=1/part-00000.parquet")
    d1 = pq.read_table(out / "mbo/date=day1/instrument_id=1/part-00000.parquet")
    assert d0["order_id"].to_pylist() == [42, 7] and d0["size"].to_pylist() == [500, 100]
    assert 42 not in d1["order_id"].to_pylist()
    # a sell at another price whose venue id equals day 1's bid 7 is not matched by id
    tw2 = match_twins(
        ds,
        [
            {
                "order_key": "L2",
                "instrument_id": 1,
                "side": "sell",
                "price": px + 50 * TICK,
                "qty": 3,
                "ts_entry": day2 + 5 * S,
                "venue_order_id": "7",
            }
        ],
    )
    assert tw2.by_id == 0 and tw2.ids == {}


def test_scorer_warns_on_cross_day_id_collision(tmp_path):
    px, _, day2, mbo = _days_tape()
    ds = make_dataset(tmp_path / "ds", mbo, n_days=2)
    live = log_from([order_events("L1", "buy", px, 100, day2, venue_order_id="42")], tmp_path / "live")
    s = build_scorecard(_spec(live, ds), runner=FakeRunner(native_replay_options=True))
    assert any("also occur on other session dates" in w for w in s.warnings)
    assert s.twin_match_rate == 1.0


# ---- fills (analytics F10, F13) -------------------------------------------------------------------------------------


def test_self_trade_keeps_both_fills():
    """Port of `exp_selftrade_dedupe.py`: both sides of one venue trade id are the user's orders."""
    t = 1_767_261_600_000_000_000
    orders = [
        {"order_key": "binance:acct:100", "venue": "binance", "instrument": "BTCUSDT", "side": "sell", "orig_qty": 5},
        {"order_key": "binance:acct:200", "venue": "binance", "instrument": "BTCUSDT", "side": "buy", "orig_qty": 5},
    ]

    def ev(k, s, liq):
        return {
            "order_key": k,
            "event_seq": s,
            "event_type": "fill",
            "qty": 5,
            "px": 100,
            "fill_id": "777",
            "ts_exchange": t,
            "liquidity": liq,
        }

    oc, w = log_outcomes(orders, [ev("binance:acct:100", 1, "taker"), ev("binance:acct:200", 1, "maker")])
    assert {k: (o.filled_qty, o.end) for k, o in oc.items()} == {
        "binance:acct:100": (5, "filled"),
        "binance:acct:200": (5, "filled"),
    }
    assert not any(x.startswith("fill_dedupe") for x in w)
    dup, w2 = log_outcomes(orders[1:], [ev("binance:acct:200", 1, "maker"), ev("binance:acct:200", 2, "maker")])
    assert dup["binance:acct:200"].filled_qty == 5 and any(x.startswith("fill_dedupe: 1") for x in w2)


def test_fill_without_price_keeps_quantity(tmp_path):
    """Analytics F13: a fill with a null px (unrepresentable VWAP) still fills the order."""
    oc, w = log_outcomes(
        [{"order_key": "k", "orig_qty": 5}],
        [{"order_key": "k", "event_type": "fill", "qty": 5, "px": None, "fill_id": "f", "ts_exchange": T0}],
    )
    assert oc["k"].filled_qty == 5 and oc["k"].filled and oc["k"].fills[0].px is None
    assert any(x.startswith("fills_without_px: 1") for x in w)
    o, ev = order_events("o1", "buy", PX9, 5, T0, fills=[(T0 + S, 5)])
    for e in ev:
        if e["event_type"] == "fill":
            e["px"] = None
    live = log_from([(o, ev)], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(sim_fills={"o1": [(T0 + 2 * S, 5, "queue_depleted")]}, bbo=BBO)
    s = build_scorecard(_spec(live, ds, tmp_path / "out"), runner=fr)
    assert s.two_by_two.both == 1 and s.ttf.n_joint == 1
    fills = rows(read_parquet(tmp_path / "out" / "score_fills.parquet"))
    assert [f["arm"] for f in fills] == ["sim"]  # no price: not in score_fills / markouts
    assert any(x.startswith("live: fills_without_px: 1") for x in s.warnings)


# ---- A8 dropped leg (analytics F11) ---------------------------------------------------------------------------------


def _repriced_log(tmp_path):
    o, ev = order_events("K", "buy", PX9, 10, T0, fills=[(T0 + 2 * S, 6)])
    ev.append(
        {"order_key": "K", "event_type": "amend_sent", "ts_local_send": T0 + 5 * S - MS, "qty": 6, "px": PX9 - TICK}
    )
    ev.append({"order_key": "K", "event_type": "amend_ack", "ts_exchange": T0 + 5 * S, "qty": 6, "px": PX9 - TICK})
    for i, e in enumerate(ev, start=1):
        e["event_seq"] = i
        for k, v in (("ts_exchange_kind", "match_time"), ("ts_precision", "ns"), ("source", "synthetic")):
            e.setdefault(k, v)
        e.setdefault("derived", False)
    return log_from([(o, ev)], tmp_path / "live")


def test_repriced_leg_restored_when_live_fills_reach_new_total(tmp_path):
    """Port of `exp_a8_leg_dropped.py`: order 10 @ PX, live fill 6, re-price to 6 @ PX - 1 tick."""
    d = _repriced_log(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sched, am = qlive.to_schedule(str(d), entry="exchange_ack", latency_ns=None)
    orders, events = read_live_log(d)
    oc, _ = log_outcomes(orders, events)
    want = [("K", 10, PX9, T0), ("K.leg2", 6, PX9 - TICK, T0 + 5 * S)]
    # current to_schedule emits the leg at the instruction total; an older one dropped it (the scorer restores it)
    for legs in (rows(sched), [r for r in rows(sched) if r["shadow_id"] == "K"]):
        s2, _, tot = instruction_legs(legs, rows(am), events, oc, "exchange_ack")
        assert [(r["shadow_id"], r["qty"], r["price"], r["ts_submit"]) for r in s2] == want
        assert tot == {"K": 10, "K.leg2": 6}
    # per arm: a sim that filled 2 on leg 1 gets up to 4 more on the restored leg; the live arm gets 0 more
    from queuetruth.score.outcomes import Fill

    sim = merge_legs({"K": [Fill(T0 + S, 2, PX9)], "K.leg2": [Fill(T0 + 6 * S, 6, PX9 - TICK)]}, ["K", "K.leg2"], tot)
    assert [f.qty for f in sim] == [2, 4]


def test_repriced_leg_restored_in_send_mode(tmp_path):
    d = _repriced_log(tmp_path)
    orders, events = read_live_log(d)
    oc, _ = log_outcomes(orders, events)
    sched = [
        {
            "shadow_id": "K",
            "order_key": "K",
            "instrument_id": 1,
            "side": ord("B"),
            "price_mode": "abs",
            "price": PX9,
            "offset_ticks": 0,
            "qty": 10,
            "ts_submit": T0 - MS,
            "latency_ns": 0,
            "ts_cancel": T0 + 5 * S - MS,
            "source": "live_log",
        }
    ]
    s2, _, tot = instruction_legs(sched, [], events, oc, "send_plus_latency")
    assert tot["K.leg2"] == 6
    assert [(r["shadow_id"], r["qty"], r["price"], r["ts_submit"]) for r in s2][1] == (
        "K.leg2",
        6,
        PX9 - TICK,
        T0 + 5 * S - MS,
    )


@pytest.mark.filterwarnings("ignore:.*re-priced leg sized at the instruction total")
def test_scorer_restores_repriced_leg_end_to_end(tmp_path):
    d = _repriced_log(tmp_path)
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(
        sim_fills={"K": [(T0 + S, 2, "queue_depleted")], "K.leg2": [(T0 + 7 * S, 6, "queue_depleted")]},
        bbo=BBO,
        schedule_fn=qlive.to_schedule,
    )
    s = build_scorecard(_spec(d, ds, tmp_path / "out"), runner=fr)
    assert [r["shadow_id"] for r in fr.schedules[0]] == ["K", "K.leg2"]
    so = rows(read_parquet(tmp_path / "out" / "score_orders.parquet"))[0]
    assert so["legs"] == 2 and so["sim_filled_qty"] == 6 and so["live_filled_qty"] == 6
    assert s.n_matched == 1


def test_manifest_and_json_untouched(tmp_path):
    """The scorer reads but never rewrites the live log (units are converted in memory)."""
    o, ev = order_events("o1", "buy", PX8, 10, T0)
    o["price_exp"] = -8
    live = log_from([(o, ev)], tmp_path / "live")
    before = {p.name: p.read_bytes() for p in live.iterdir()}
    build_scorecard(_spec(live, make_dataset(tmp_path / "ds", _end_mbo())), runner=FakeRunner(bbo=BBO))
    assert {p.name: p.read_bytes() for p in live.iterdir()} == before
    assert json.loads((live / "manifest.json").read_text())["n_orders"] == 1
