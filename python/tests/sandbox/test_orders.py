"""Order semantics of the sandbox on hand-made order-level data whose outcome is known exactly (SPEC 5.2): queue
position, fills, arrival (taker) fills, IOC and market orders, pegged prices, cancels and the cancel race, partial
cancels, censoring, validation and risk checks, idempotency, latency."""

from __future__ import annotations

from pathlib import Path

import pytest

from queuetruth.errors import QtError
from queuetruth.sandbox import Sandbox
from sandbox.sbx_helpers import MS, Tape, at, base_spec, px, write_dataset

pytestmark = pytest.mark.requires_core


def queue_scenario(tmp_path: Path, venue: str = "cme") -> Path:
    """Bids 5 + 3 at 100.00 before the episode; a third bid joins at 11 s, the first cancels at 12 s, the second is
    executed at 13 s, the third is executed (2 of 4) at 14 s; the ask is 100.05 throughout."""
    t = Tape()
    a1 = t.add(at(1), "B", px(0), 5)
    t.add(at(1.5), "A", px(5), 5)
    a2 = t.add(at(2), "B", px(0), 3)
    a3 = t.add(at(11), "B", px(0), 4)
    t.cancel(at(12), a1)
    t.trade(at(13), "A", [(a2, 3)])
    t.trade(at(14), "A", [(a3, 2)])
    t.noop(at(30))
    return write_dataset(tmp_path / "ds", t, venue=venue)


def test_passive_order_queue_position_and_fill(tmp_path: Path) -> None:
    ds = queue_scenario(tmp_path)
    with Sandbox.open(base_spec(ds, start=at(10), end=at(20), latency="1ms")) as sb:
        ack = sb.submit("buy", 2, price="100.00")
        assert ack.order.status == "pending" and ack.order.ts_submit == at(10) and ack.order.queue_ahead is None
        obs = sb.advance(to=at(11.5))
        (o,) = obs.orders
        # activated at the 11 s event against the book the 2 s event left: 5 + 3 ahead, the 11 s bid behind it
        assert (o.status, o.ts_activated, o.queue_ahead, o.rank, o.level_qty) == ("working", at(11), 8, 2, 12)
        o = sb.advance(to=at(12.5)).orders[0]
        assert (o.queue_ahead, o.rank, o.filled_qty) == (3, 1, 0)
        obs = sb.advance(to=at(13.5))
        o = obs.orders[0]
        # the order ahead executed: queue ahead 0, but executions ahead never fill the shadow
        assert (o.queue_ahead, o.rank, o.filled_qty) == (0, 0, 0) and obs.fills == []
        obs = sb.advance(to=at(14.5))
        (f,) = obs.fills
        assert (f.order_id, f.qty, f.price, f.reason, f.liquidity, f.ts_event, f.queue_ahead_before) == (
            "o1",
            2,
            px(0),
            "queue_depleted",
            "maker",
            at(14),
            0,
        )
        assert f.ts_known == at(14) and f.step == obs.step
        assert obs.orders[0].status == "filled" and obs.n_open == 0
        assert obs.position.qty == 2 and obs.position.bought_qty == 2
        card = sb.finish()
    assert card.consistency.equal and card.consistency.fills_reported == 1


def sweep_scenario(tmp_path: Path) -> Path:
    t = Tape()
    t.add(at(1), "B", px(0), 5)
    t.add(at(1), "A", px(5), 2)
    t.add(at(1), "A", px(6), 3)
    t.add(at(1), "A", px(8), 10)
    t.noop(at(11))
    t.add(at(12), "A", px(5), 50)
    t.noop(at(13))
    return write_dataset(tmp_path / "ds", t)


def test_market_order_walks_the_book_as_taker(tmp_path: Path) -> None:
    ds = sweep_scenario(tmp_path)
    with Sandbox.open(base_spec(ds, start=at(10), end=at(20), latency="1ms")) as sb:
        ack = sb.submit("buy", 4, type="market")
        # priced at the collar: 100 bps through the ask seen at decision time, on the tick grid
        assert ack.order.price == px(5) + 101 * 10_000_000 and ack.order.tif == "ioc"
        assert sb.advance(to=at(10.5)).orders[0].status == "pending"  # no event after arrival yet
        obs = sb.advance(to=at(11.5))
        assert [(f.price, f.qty, f.liquidity, f.reason) for f in obs.fills] == [
            (px(5), 2, "taker", "crossed_at_live"),
            (px(6), 2, "taker", "crossed_at_live"),
        ]
        # arrival fills carry the arrival time; they are knowable at the first event after it
        assert all(f.ts_event == at(10) + MS and f.ts_known == at(11) for f in obs.fills)
        assert obs.orders[0].status == "filled"
        card = sb.finish()
    assert card.pnl.position_end == 4 and card.consistency.equal


def test_ioc_remainder_is_cancelled_and_never_fills_later(tmp_path: Path) -> None:
    ds = sweep_scenario(tmp_path)
    with Sandbox.open(base_spec(ds, start=at(10), end=at(20), latency="1ms")) as sb:
        sb.submit("buy", 20, type="market")
        sb.submit("buy", 7, price="100.07", tif="ioc")
        obs = sb.advance(to=at(11.5))
        o1, o2 = obs.orders
        assert (o1.status, o1.reason, o1.filled_qty, o1.leaves_qty) == ("cancelled", "ioc_remainder", 15, 0)
        assert (o2.status, o2.reason, o2.filled_qty) == ("cancelled", "ioc_remainder", 5)  # 100.08 is above its limit
        # 50 more at 100.05 arrive at 12 s: an IOC remainder must not fill on it
        obs = sb.advance(to=at(15))
        assert obs.fills == []
        card = sb.finish()
    by = {o.order_id: o for o in card.orders}
    assert by["o1"].filled_qty == 15 and by["o2"].filled_qty == 5
    assert all(f.reason == "crossed_at_live" for f in card.fills)
    assert card.consistency.equal


def test_ioc_that_cannot_trade_is_cancelled_with_no_fill(tmp_path: Path) -> None:
    ds = sweep_scenario(tmp_path)
    with Sandbox.open(base_spec(ds, start=at(10), end=at(20), latency="1ms")) as sb:
        sb.submit("buy", 3, price="100.01", tif="ioc")
        obs = sb.advance(to=at(11.5))
        assert (obs.orders[0].status, obs.orders[0].reason, obs.orders[0].filled_qty) == (
            "cancelled",
            "no_liquidity",
            0,
        )
        with pytest.raises(QtError) as e:
            sb.cancel("o1")
        assert e.value.code == "QT-USAGE-001"


def test_pegged_order_prices_at_arrival_not_at_decision(tmp_path: Path) -> None:
    t = Tape()
    t.add(at(1), "B", px(0), 5)
    t.add(at(1), "A", px(5), 5)
    t.add(at(10.002), "B", px(1), 7)  # the bid improves while the order is in flight
    t.noop(at(11))
    t.noop(at(12))
    ds = write_dataset(tmp_path / "ds", t)
    with Sandbox.open(base_spec(ds, start=at(10), end=at(20), latency="5ms")) as sb:
        ack = sb.submit("buy", 2, peg_offset_ticks=0)
        assert ack.order.price is None and ack.order.peg_offset_ticks == 0
        sb.submit("buy", 1, peg_offset_ticks=-2)
        sb.submit("buy", 1, peg_offset_ticks=10)  # would cross: rejected at arrival by the venue rules
        obs = sb.advance(to=at(11.5))
        a, b, c = obs.orders
        assert (a.price, a.queue_ahead, a.rank) == (px(1), 7, 1)
        assert (b.price, b.queue_ahead) == (px(-1), 0)
        assert (c.status, c.reason) == ("rejected", "rejected_would_cross") and c.ts_done is not None
        card = sb.finish()
    assert card.consistency.equal


def race_scenario(tmp_path: Path) -> Path:
    """A lone shadow bid at 100.00 above a real bid at 99.99; at 20.005 s a sell executes 3 at 99.99 (trade
    through)."""
    t = Tape()
    b1 = t.add(at(1), "B", px(-1), 5)
    t.add(at(1), "A", px(5), 5)
    t.noop(at(11))
    t.trade(at(20.005), "A", [(b1, 3)])
    t.noop(at(25))
    return write_dataset(tmp_path / "ds", t)


@pytest.mark.parametrize(("latency", "filled"), [("10ms", True), ("1ms", False)])
def test_cancel_race_fills_during_cancel_latency_stand(tmp_path: Path, latency: str, filled: bool) -> None:
    ds = race_scenario(tmp_path)
    with Sandbox.open(base_spec(ds, start=at(10), end=at(30), latency=latency)) as sb:
        sb.submit("buy", 2, price="100.00")
        assert sb.advance(to=at(20)).orders[0].status == "working"
        ack = sb.cancel("o1")
        assert ack.order.cancel_sent_ns == at(20) and ack.order.status == "working"
        o = sb.advance(to=at(20.003)).orders[0]
        assert o.status == "working" and o.cancel_sent_ns == at(20)  # the cancel is in flight
        obs = sb.advance(to=at(26))
        o = obs.orders[0]
        if filled:
            assert (o.status, o.filled_qty) == ("filled", 2)
            assert [(f.reason, f.price, f.qty) for f in obs.fills] == [("trade_through", px(0), 2)]
        else:
            assert (o.status, o.reason, o.filled_qty) == ("cancelled", "agent_cancel", 0)
            assert o.ts_done == at(20) + MS
        card = sb.finish()
    assert card.consistency.equal


def test_partial_cancel_keeps_priority_and_can_reduce_to_filled(tmp_path: Path) -> None:
    t = Tape()
    a1 = t.add(at(1), "B", px(0), 5)
    t.add(at(1), "A", px(5), 5)
    a2 = t.add(at(11), "B", px(0), 10)  # behind the shadow
    t.cancel(at(12), a1)
    t.trade(at(13), "A", [(a2, 1)])
    t.noop(at(15))
    t.trade(at(16), "A", [(a2, 5)])
    t.noop(at(18))
    ds = write_dataset(tmp_path / "ds", t)
    with Sandbox.open(base_spec(ds, start=at(10), end=at(20), latency="1ms")) as sb:
        sb.submit("buy", 5, price="100.00")
        sb.submit("buy", 4, price="100.00", client_order_id="second")
        o = sb.advance(to=at(11.5)).orders[0]
        assert (o.queue_ahead, o.qty) == (5, 5)
        ack = sb.cancel("o1", qty=3)
        assert ack.order.qty == 2
        o = sb.advance(to=at(11.8)).orders[0]
        assert (o.queue_ahead, o.rank, o.qty) == (5, 1, 2)  # priority kept
        obs = sb.advance(to=at(13.5))
        assert [(f.order_id, f.qty) for f in obs.fills] == [("o1", 1), ("o2", 1)]
        sb.cancel("o2", qty=3)  # 4 - 3 = 1 <= 1 filled: reduced to what has filled
        obs = sb.advance(to=at(17))
        by = {o.order_id: o for o in obs.orders}
        assert (by["o1"].status, by["o1"].filled_qty) == ("filled", 2)
        assert (by["o2"].status, by["o2"].reason, by["o2"].filled_qty) == ("filled", "reduced_to_filled", 1)
        with pytest.raises(QtError) as e:
            sb.cancel("o1", qty=1)
        assert e.value.code == "QT-USAGE-001"
        card = sb.finish()
    assert card.consistency.equal


def test_book_clear_and_feed_gap_censor_orders(tmp_path: Path) -> None:
    t = Tape()
    t.add(at(1), "B", px(0), 5)
    t.add(at(1), "A", px(5), 5)
    t.noop(at(11))
    t.clear(at(12))
    t.add(at(12.5), "B", px(0), 5)
    t.add(at(12.5), "A", px(5), 5)
    t.noop(at(13))
    t.noop(at(16))
    gap = {"instrument_id": 1, "ts_start": at(14), "ts_end": at(15), "reason": "sequence gap"}
    ds = write_dataset(tmp_path / "ds", t, feed_gaps=[gap])
    with Sandbox.open(base_spec(ds, start=at(10), end=at(20), latency="1ms")) as sb:
        sb.submit("buy", 1, price="100.00")
        o = sb.advance(to=at(12.2)).orders[0]
        assert (o.status, o.reason) == ("censored", "book_clear")
        sb.submit("sell", 1, price="100.05")
        # the gap (14 s to 15 s) has started, but no event after its start is visible yet: it acts at the 16 s event
        assert sb.advance(to=at(14.5)).orders[-1].status == "working"
        assert sb.advance(to=at(16.5)).orders[-1].reason == "feed_gap"
        card = sb.finish()
    assert [o.status for o in card.orders] == ["censored", "censored"]
    assert any("censored" in w for w in card.warnings)


def test_hidden_print_policy_and_cross_prints_on_nasdaq(tmp_path: Path) -> None:
    t = Tape()
    a1 = t.add(at(1), "B", px(0), 5)
    t.add(at(1), "A", px(5), 5)
    t.noop(at(11))
    t.cancel(at(12), a1)
    t.hidden_print(at(13), px(0), 3, cross=True)  # an ITCH cross: never fill volume
    t.hidden_print(at(14), px(0), 1)  # a hidden execution at the shadow's price, nothing displayed at or through it
    t.noop(at(15))
    ds = write_dataset(tmp_path / "ds", t, venue="nasdaq")
    with Sandbox.open(base_spec(ds, start=at(10), end=at(20), latency="1ms")) as sb:
        sb.submit("buy", 2, price="100.00")
        obs = sb.advance(to=at(13.5))
        assert obs.fills == [] and [x.kind for x in obs.trades] == ["cross"]
        obs = sb.advance(to=at(14.5))
        assert [(f.qty, f.reason) for f in obs.fills] == [(1, "queue_depleted")]
        assert [x.kind for x in obs.trades] == ["hidden"]
        card = sb.finish()
    assert card.consistency.equal
    with Sandbox.open(base_spec(ds, start=at(10), end=at(20), latency="1ms", hidden_print_policy="ignore")) as sb:
        sb.submit("buy", 2, price="100.00")
        assert sb.advance(to=at(14.5)).fills == []


# ---- validation and risk ----------------------------------------------------------------------------------------


@pytest.fixture
def simple(tmp_path: Path) -> Path:
    t = Tape()
    t.add(at(1), "B", px(0), 5)
    t.add(at(1), "A", px(5), 5)
    for k in range(11, 40):
        t.noop(at(k))
    return write_dataset(tmp_path / "ds", t, lot=1)


@pytest.mark.parametrize(
    ("kwargs", "code", "fragment"),
    [
        ({"side": "hold", "qty": 1, "price": "100"}, "QT-CONFIG-001", "side"),
        ({"side": "buy", "qty": 0, "price": "100"}, "QT-CONFIG-001", "qty"),
        ({"side": "buy", "qty": -2, "price": "100"}, "QT-CONFIG-001", "qty"),
        ({"side": "buy", "qty": 1.5, "price": "100"}, "QT-CONFIG-001", "qty"),
        ({"side": "buy", "qty": 1}, "QT-CONFIG-001", "needs a price"),
        ({"side": "buy", "qty": 1, "price": "100.005"}, "QT-CONFIG-001", "tick grid"),
        ({"side": "buy", "qty": 1, "price": "100.0000000001"}, "QT-INPUT-004", "not representable"),
        ({"side": "buy", "qty": 1, "price": "abc"}, "QT-CONFIG-001", "decimal"),
        ({"side": "buy", "qty": 1, "price": 100.0}, "QT-CONFIG-001", "decimal string"),
        ({"side": "buy", "qty": 1, "price": "100", "peg_offset_ticks": 0}, "QT-CONFIG-001", "not both"),
        ({"side": "buy", "qty": 1, "type": "stop", "price": "100"}, "QT-CONFIG-001", "type"),
        ({"side": "buy", "qty": 1, "price": "100", "tif": "gtc"}, "QT-CONFIG-001", "tif"),
        ({"side": "buy", "qty": 1, "type": "market", "price": "100"}, "QT-CONFIG-001", "market order"),
        ({"side": "buy", "qty": 1, "peg_offset_ticks": 0, "tif": "ioc"}, "QT-CONFIG-001", "day order"),
        ({"side": "buy", "qty": 1, "price": "100", "client_order_id": "has space"}, "QT-CONFIG-001", "client_order_id"),
    ],
)
def test_invalid_orders_are_rejected_and_logged(simple: Path, kwargs: dict, code: str, fragment: str) -> None:
    with Sandbox.open(base_spec(simple, start=at(10), end=at(30))) as sb:
        side, qty = kwargs.pop("side"), kwargs.pop("qty")
        with pytest.raises(QtError) as e:
            sb.submit(side, qty, **kwargs)
        assert e.value.code == code and fragment in e.value.message
        last = sb.order_log()["entries"][-1]
        assert last["kind"] == "reject" and last["data"]["code"] == code
        assert sb.observe().n_orders == 0


def test_lot_size_and_risk_limits(tmp_path: Path) -> None:
    t = Tape()
    t.add(at(1), "B", px(0), 500)
    t.add(at(1), "A", px(5), 500)
    t.noop(at(11))
    ds = write_dataset(tmp_path / "ds", t, lot=100)
    spec = base_spec(ds, start=at(10), end=at(20), max_order_qty=300, max_position=400, max_open_orders=3)
    with Sandbox.open(spec) as sb:
        for kw, reason in (
            ({"qty": 150}, None),
            ({"qty": 400}, "max_order_qty"),
        ):
            with pytest.raises(QtError) as e:
                sb.submit("buy", kw["qty"], price="100.00")
            assert e.value.details.get("reason") == reason
        sb.submit("buy", 300, price="99.00")
        with pytest.raises(QtError) as e:
            sb.submit("buy", 200, price="99.00")
        assert e.value.details["reason"] == "max_position"
        sb.submit("sell", 300, price="101.00")
        sb.submit("buy", 100, price="98.00")
        with pytest.raises(QtError) as e:
            sb.submit("sell", 100, price="102.00")
        assert e.value.details["reason"] == "max_open_orders"
        assert sb.observe().n_orders == 3


def test_self_trade_prevention(simple: Path) -> None:
    with Sandbox.open(base_spec(simple, start=at(10), end=at(30))) as sb:
        sb.submit("sell", 1, price="100.05")
        with pytest.raises(QtError) as e:
            sb.submit("buy", 1, price="100.05")
        assert e.value.details == {"reason": "self_trade", "order_id": "o1"}
        with pytest.raises(QtError):
            sb.submit("buy", 1, type="market")
        sb.submit("buy", 1, price="100.04")
        sb.cancel("o1")
        sb.submit("buy", 1, price="100.06")  # a cancelled order no longer blocks
    with Sandbox.open(base_spec(simple, start=at(10), end=at(30), self_trade_prevention="off")) as sb:
        sb.submit("sell", 1, price="100.05")
        sb.submit("buy", 1, price="100.06")
        sb.advance(to=at(15))
        card = sb.finish()
    assert card.diagnostics.self_cross_pairs == 1


def test_client_order_id_is_idempotent(simple: Path) -> None:
    with Sandbox.open(base_spec(simple, start=at(10), end=at(30))) as sb:
        a = sb.submit("buy", 2, price="100.00", client_order_id="c-1")
        b = sb.submit("buy", 2, price="100.00", client_order_id="c-1")
        assert b.duplicate and not a.duplicate and b.order.order_id == a.order.order_id == "o1"
        assert b.log_hash == a.log_hash
        with pytest.raises(QtError) as e:
            sb.submit("buy", 3, price="100.00", client_order_id="c-1")
        assert e.value.code == "QT-INPUT-003"
        assert sb.observe().n_orders == 1


def test_cancel_errors(simple: Path) -> None:
    with Sandbox.open(base_spec(simple, start=at(10), end=at(30))) as sb:
        with pytest.raises(QtError) as e:
            sb.cancel("o9")
        assert e.value.code == "QT-USAGE-001"
        sb.submit("buy", 2, price="99.00")
        for qty, code in ((0, "QT-CONFIG-001"), (2, "QT-CONFIG-001"), (5, "QT-CONFIG-001"), ("x", "QT-CONFIG-001")):
            with pytest.raises(QtError) as e:
                sb.cancel("o1", qty=qty)
            assert e.value.code == code
        sb.cancel("o1")
        with pytest.raises(QtError) as e:
            sb.cancel("o1")
        assert e.value.code == "QT-USAGE-001" and "already sent" in e.value.message


def test_latency_and_jitter_are_deterministic_per_order(simple: Path) -> None:
    spec = base_spec(simple, start=at(10), end=at(30), latency="2ms", jitter="1ms", seed=7)
    lat = []
    for _ in range(2):
        with Sandbox.open(spec) as sb:
            for _ in range(6):
                sb.submit("buy", 1, price="99.00")
            lat.append([e["data"]["latency_ns"] for e in sb.order_log()["entries"] if e["kind"] == "submit"])
    assert lat[0] == lat[1]
    assert all(2 * MS <= v <= 3 * MS for v in lat[0]) and len(set(lat[0])) > 1
    with Sandbox.open({**spec, "seed": 8}) as sb:
        for _ in range(6):
            sb.submit("buy", 1, price="99.00")
        other = [e["data"]["latency_ns"] for e in sb.order_log()["entries"] if e["kind"] == "submit"]
    assert other != lat[0]


def test_advance_rules(simple: Path) -> None:
    with Sandbox.open(base_spec(simple, start=at(10), end=at(20), max_steps=3)) as sb:
        for kwargs in ({}, {"by": "1s", "to": at(12)}):
            with pytest.raises(QtError) as e:
                sb.advance(**kwargs)
            assert e.value.code == "QT-USAGE-001"
        for kwargs, code in (
            ({"to": at(10)}, "QT-USAGE-001"),
            ({"to": at(5)}, "QT-USAGE-001"),
            ({"by": 0}, "QT-USAGE-001"),
            ({"by": "5"}, "QT-CONFIG-001"),
            ({"by": "-1s"}, "QT-CONFIG-001"),
            ({"to": "25:99"}, "QT-CONFIG-001"),
        ):
            with pytest.raises(QtError) as e:
                sb.advance(**kwargs)
            assert e.value.code == code, kwargs
        assert sb.advance(by="1s").clock_ns == at(11)
        assert sb.advance(to="00:00:12.5").clock_ns == at(12.5)
        obs = sb.advance(by="1m")
        assert obs.clock_ns == at(20) and obs.done and obs.warnings
        with pytest.raises(QtError) as e:
            sb.advance(by="1s")
        assert "over" in e.value.message
    with Sandbox.open(base_spec(simple, start=at(10), end=at(20), max_steps=1)) as sb:
        sb.advance(by="1s")
        with pytest.raises(QtError) as e:
            sb.advance(by="1s")
        assert "max_steps" in e.value.message


def test_finish_cancels_open_orders_and_closes(simple: Path, tmp_path: Path) -> None:
    sb = Sandbox.open(base_spec(simple, start=at(10), end=at(30), latency="1ms"))
    wd = sb.workdir
    sb.submit("buy", 2, price="99.00")
    sb.advance(to=at(12))
    card = sb.finish(out=tmp_path / "out")
    assert sb.closed and not wd.exists()
    (o,) = card.orders
    assert (o.status, o.reason, o.cancel_sent_ns) == ("cancelled", "finish_cancel", at(12))
    log = sb.order_log()
    assert [e["kind"] for e in log["entries"]] == ["open", "submit", "advance", "cancel", "finish"]
    assert log["entries"][3]["data"]["by"] == "finish"
    for call in (sb.observe, lambda: sb.submit("buy", 1, price="99.00"), lambda: sb.advance(by="1s"), sb.finish):
        with pytest.raises(QtError) as e:
            call()
        assert e.value.code == "QT-USAGE-001"
    names = {f.name for f in card.files}
    assert {"order_log.json", "replay/shadow_results.parquet", "replay/shadow_fills.parquet"} <= names
    assert (tmp_path / "out" / "scorecard.json").exists()


def test_open_errors(tmp_path: Path, simple: Path) -> None:
    for spec, code in (
        ({"dataset": {"path": str(tmp_path / "missing")}}, "QT-INPUT-001"),
        (base_spec(simple, bogus=1), "QT-CONFIG-001"),
        (base_spec(simple, instrument_id=9), "QT-CONFIG-001"),
        (base_spec(simple, symbol="NOPE", instrument_id=None), "QT-CONFIG-001"),
        (base_spec(simple, start=at(20), end=at(10)), "QT-CONFIG-001"),
        (base_spec(simple, start="10:00", end="09:00"), "QT-CONFIG-001"),
        (base_spec(simple, latency="5"), "QT-CONFIG-001"),
        (base_spec(simple, latency="1ms", latency_ns=5), "QT-CONFIG-001"),
        (base_spec(simple, markout_horizons=["10s", "1s"]), "QT-CONFIG-001"),
        (base_spec(simple, fees={"maker_bps": float("nan")}), "QT-CONFIG-001"),
    ):
        with pytest.raises(QtError) as e:
            Sandbox.open(spec)
        assert e.value.code == code, spec
    with Sandbox.open({"dataset": {"path": str(simple)}, "symbol": "tst"}) as sb:
        assert sb.instrument["symbol"] == "TST"  # case-insensitive fallback


def test_l2_dataset_is_refused(tmp_path: Path, simple: Path) -> None:
    import json
    import shutil

    l2 = tmp_path / "l2"
    shutil.copytree(simple, l2)
    man = json.loads((l2 / "manifest.json").read_text())
    man["kind"] = "l2"
    (l2 / "manifest.json").write_text(json.dumps(man))
    with pytest.raises(QtError) as e:
        Sandbox.open(base_spec(l2))
    assert e.value.code == "QT-CONFIG-001" and "MBO" in e.value.message


def test_self_trade_prevention_compares_arrival_times(simple: Path) -> None:
    """An IOC or a cancelled order that is gone by the time a new order arrives cannot trade with it; with jitter the
    arrivals decide (both are known exactly: per-order latency is deterministic)."""
    with Sandbox.open(base_spec(simple, start=at(10), end=at(30), latency="1ms")) as sb:
        sb.submit("buy", 1, type="market")  # arrives at 10.001 and is done on arrival
        sb.submit("sell", 1, price="100.00", tif="ioc")  # sent after it, same latency: processed after it
        sb.submit("sell", 1, price="100.10")
        sb.cancel("o3")
        sb.submit("buy", 1, price="100.10")  # the cancel takes effect when this order arrives: no cross
        assert sb.observe().n_orders == 4
    decided = {"accept": False, "reject": False}
    for seed in range(60):
        with Sandbox.open(base_spec(simple, start=at(10), end=at(30), latency="1ms", jitter="1ms", seed=seed)) as sb:
            sb.submit("buy", 1, type="market")
            ioc_arrival = at(10) + sb.orders[0].latency_ns
            new_arrival = at(10) + sb._order_latency("o2")
            try:
                sb.submit("sell", 1, price="100.00")
                outcome = "accept"
            except QtError as e:
                assert e.details == {"reason": "self_trade", "order_id": "o1"}
                outcome = "reject"
            assert outcome == ("accept" if ioc_arrival <= new_arrival else "reject")
            decided[outcome] = True
        if all(decided.values()):
            break
    assert all(decided.values())


def test_fractional_sizes(tmp_path: Path) -> None:
    """An instrument with size_exp -8 (crypto units): decimal sizes in, fees and PnL in real units."""
    t = Tape()
    t.add(at(1), "B", px(0), 300_000_000)  # 3.0
    s1 = t.add(at(1), "A", px(5), 200_000_000)  # 2.0
    t.noop(at(11))
    t.trade(at(12), "B", [(s1, 50_000_000)])
    t.noop(at(13))
    ds = write_dataset(tmp_path / "ds", t, venue="generic", lot=1_000_000, size_exp=-8)
    spec = base_spec(ds, start=at(10), end=at(20), latency="1ms", fees={"taker_bps": 10.0, "maker_per_unit": -1.0})
    with Sandbox.open(spec) as sb:
        a = sb.submit("buy", "0.25", type="market")
        assert a.order.qty == 25_000_000
        with pytest.raises(QtError) as e:
            sb.submit("buy", "0.001", price="100.00")  # 100,000 units: not a multiple of the 1,000,000 lot
        assert e.value.code == "QT-CONFIG-001" and "lot" in e.value.message
        with pytest.raises(QtError) as e:
            sb.submit("buy", "0.000000001", price="100.00")
        assert e.value.code == "QT-INPUT-004"
        sb.advance(to=at(11.5))
        card = sb.finish()
    (f,) = card.fills
    assert (f.qty, f.price, f.liquidity) == (25_000_000, px(5), "taker")
    # 0.25 x 100.05 = 25.0125 notional; 10 bp taker fee = 0.0250125
    assert card.pnl.fees == 0.0250125 and card.pnl.notional_traded == 25.0125
    assert card.pnl.position_end == 25_000_000


def test_cancel_sent_with_the_order_never_goes_live(simple: Path) -> None:
    with Sandbox.open(base_spec(simple, start=at(10), end=at(30), latency="2ms")) as sb:
        sb.submit("buy", 3, price="100.05")  # marketable, but cancelled at the same clock: arrives with its cancel
        sb.cancel("o1")
        o = sb.advance(to=at(12)).orders[0]
        assert (o.status, o.filled_qty, o.ts_activated) == ("cancelled", 0, None)
        card = sb.finish()
    assert card.fills == [] and card.consistency.equal


def test_steps_without_new_records_replay_nothing(tmp_path: Path) -> None:
    t = Tape()
    a1 = t.add(at(1), "B", px(0), 5)
    t.add(at(1), "A", px(5), 5)
    t.noop(at(11))
    t.trade(at(30), "A", [(a1, 5)])
    t.trade(at(30.5), "A", [(t.add(at(30.2), "B", px(0), 4), 4)])
    t.noop(at(31))
    ds = write_dataset(tmp_path / "ds", t)
    with Sandbox.open(base_spec(ds, start=at(10), end=at(40), latency="1ms")) as sb:
        sb.submit("buy", 2, price="100.00")
        sb.advance(to=at(12))
        assert sb.window_replays == 1
        for k in range(13, 30):  # no market event between 11 s and 30 s
            sb.advance(to=at(k))
            sb.submit("sell", 1, price="100.10")  # new instructions alone do not need a replay
        assert sb.window_replays == 1 and all(o.status == "pending" for o in sb.orders[1:])
        obs = sb.advance(to=at(31))
        assert sb.window_replays == 2 and [(f.order_id, f.qty) for f in obs.fills] == [("o1", 2)]
        card = sb.finish()
    assert card.consistency.equal and card.consistency.window_replays == 2
