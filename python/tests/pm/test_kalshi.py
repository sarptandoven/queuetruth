"""The Kalshi reader on hand-made captures in the documented message formats (`queuetruth.pm.kalshi`)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pm._helpers import (
    MS,
    T0,
    TICKER,
    S,
    k_delta,
    k_fill,
    k_snapshot,
    k_subscribe,
    k_trade,
    k_user_order,
    wrap,
    write_jsonl,
)

from queuetruth.errors import QtError
from queuetruth.pm.batches import build_batches
from queuetruth.pm.book import ASK, BID
from queuetruth.pm.kalshi import read_kalshi

SNAP_YES = [("0.4800", "10.00"), ("0.4900", "5.00")]
SNAP_NO = [("0.5000", "7.00")]


def read(tmp: Path, records: list[Any], **kw: Any):
    return read_kalshi([write_jsonl(tmp / "k.jsonl", records)], **kw)


@pytest.mark.parametrize(
    ("use_yes", "opt", "ask_price", "warned"),
    [
        (False, "auto", 5000, False),  # no-leg pricing: NO bid 0.50 = YES ask 0.50
        (None, "auto", 5000, False),  # subscribed without the flag: the documented default
        (True, "auto", 5000, False),
        ("nosub", "auto", 5000, True),  # no subscribe captured: default assumed, with a warning
        ("nosub", "true", 5000, False),
    ],
)
def test_no_levels_map_to_yes_asks(tmp_path: Path, use_yes: Any, opt: str, ask_price: int, warned: bool) -> None:
    recs = [] if use_yes == "nosub" else k_subscribe(1, use_yes)
    recs.append(k_snapshot(1, T0, SNAP_YES, [("0.5000", "7.00")]))
    cap = read(tmp_path, recs, yes_price=opt)
    (snap,) = cap.book[TICKER]
    asks = [lv for lv in snap.levels if lv[0] == ASK]
    assert asks == [(ASK, ask_price, 700)]
    assert sorted(lv for lv in snap.levels if lv[0] == BID) == [(BID, 4800, 1000), (BID, 4900, 500)]
    assert any("use_yes_price unknown" in w for w in cap.warnings) == warned


def test_yes_price_pricing_reads_no_levels_as_yes_prices(tmp_path: Path) -> None:
    cap = read(tmp_path, [*k_subscribe(1, True), k_snapshot(1, T0, [], [("0.5300", "2.00")])])
    assert cap.book[TICKER][0].levels == [(ASK, 5300, 200)]
    cap = read(tmp_path, [*k_subscribe(1, False), k_snapshot(1, T0, [], [("0.5300", "2.00")])])
    assert cap.book[TICKER][0].levels == [(ASK, 4700, 200)]


def test_deltas_clock_groups_own_tags_gaps_and_regressions(tmp_path: Path) -> None:
    recs = [
        *k_subscribe(1, False),
        k_snapshot(1, T0, SNAP_YES, SNAP_NO),
        k_delta(2, T0 + 10 * MS, "yes", "0.4900", "3.00", clock=5, cid="mm-1"),
        k_delta(3, T0 + 10 * MS, "no", "0.5000", "-2.00", clock=5),
        k_delta(5, T0 + 20 * MS, "yes", "0.4800", "1.00", clock=6),  # seq 4 missing: a gap
        k_delta(4, T0 + 21 * MS, "yes", "0.4800", "1.00", clock=7),  # backwards, new content: a new session
        k_delta(9, T0 + 30 * MS, "yes", "0.4800", "1.00", clock=8, sid=2),  # the same market on a second sid
    ]
    cap = read(tmp_path, recs)
    msgs = cap.book[TICKER]
    assert [m.kind for m in msgs] == ["snapshot", "delta", "delta", "gap", "delta", "gap", "delta"]
    assert [m.reason for m in msgs if m.kind == "gap"] == ["sequence gap", "sequence regression"]
    assert msgs[1].group == msgs[2].group == ("clock", 5) and msgs[1].own_tag == "mm-1"
    assert msgs[2].levels == [(ASK, 5000, -200)]
    assert msgs[1].ts_event == T0 + 10 * MS and msgs[1].ts_recv == T0 + 12 * MS
    assert (cap.sequence_gaps, cap.sequence_regressions) == (1, 1)
    assert cap.counters["duplicate_subscription_messages"] == 1 and cap.counters["own_attributed_deltas"] == 1
    assert any("seq gap" in w for w in cap.warnings) and any("seq regression" in w for w in cap.warnings)
    assert any("another subscription delivers were dropped" in w for w in cap.warnings)


def book_after(msgs: list[Any]) -> tuple[list[Any], Any]:
    return build_batches(msgs, [], match_window_ns=10**9)


def session(t: int, sid: int, first_seq: int = 1, yes: str = "13.00") -> list[dict]:
    """A snapshot then two deltas on one subscription."""
    return [
        k_snapshot(first_seq, t, [("0.4800", yes)], SNAP_NO, sid=sid),
        k_delta(first_seq + 1, t + 10 * MS, "yes", "0.4800", "1.00", clock=t // MS, sid=sid),
        k_delta(first_seq + 2, t + 20 * MS, "yes", "0.4800", "-2.00", clock=t // MS + 1, sid=sid),
    ]


@pytest.mark.parametrize("subscribed", [True, False])
def test_a_reconnect_on_the_same_sid_gaps_the_book_until_the_new_snapshot(tmp_path: Path, subscribed: bool) -> None:
    """Kalshi numbers `seq` per subscription and connection: after a reconnect the sid can come back with seq 1.
    The outage must open a feed gap (orders resting through it are censored) and the new snapshot must resync the
    book, not be diffed into an ordinary depth batch."""
    t1 = T0 + 30 * S
    again = k_subscribe(1, False, t=t1 - 50 * MS) if subscribed else []
    recs = [*k_subscribe(1, False), *session(T0, 1), *again, *session(t1, 1, yes="40.00")]
    cap = read(tmp_path, recs)
    assert cap.counters["orderbook_resubscriptions"] == 1 and cap.sequence_regressions == 0
    gaps = [m for m in cap.book[TICKER] if m.kind == "gap"]
    assert [g.reason for g in gaps] == ["resubscription"]
    batches, st = book_after(cap.book[TICKER])
    assert [b.kind for b in batches] == ["resync", "depth", "depth", "resync", "depth", "depth"]
    assert batches[3].levels[0] == (BID, 4800, 4000)  # the new session's snapshot, not a 1200 -> 4000 change
    assert st.feed_gaps == [(T0 + 20 * MS, t1, "resubscription until the next snapshot")]
    assert any("resubscription" in w for w in cap.warnings)


def test_a_reconnect_with_a_new_sid_moves_the_market_to_it(tmp_path: Path) -> None:
    """The old code bound a market to its first sid and silently dropped every later message from another one."""
    t1 = T0 + 30 * S
    stale = k_delta(4, t1 + 15 * MS, "yes", "0.4800", "5.00", clock=99, sid=1)  # late frame of the old subscription
    recs = [*k_subscribe(1, False), *session(T0, 1), *k_subscribe(7, False, t=t1 - 50 * MS), *session(t1, 7), stale]
    cap = read(tmp_path, recs)
    msgs = cap.book[TICKER]
    assert [m.kind for m in msgs].count("delta") == 4 and [m.kind for m in msgs].count("snapshot") == 2
    assert cap.counters["orderbook_resubscriptions"] == 1 and cap.counters["duplicate_subscription_messages"] == 1
    batches, st = book_after(msgs)
    assert [b.kind for b in batches] == ["resync", "depth", "depth", "resync", "depth", "depth"]
    assert len(st.feed_gaps) == 1 and st.feed_gaps[0][2] == "resubscription until the next snapshot"


def test_duplicated_frames_are_dropped_once(tmp_path: Path) -> None:
    first = session(T0, 1)
    recs = [*k_subscribe(1, False), first[0], first[1], first[1], first[2], first[2]]
    cap = read(tmp_path, recs)
    assert [m.kind for m in cap.book[TICKER]] == ["snapshot", "delta", "delta"]
    assert cap.counters["duplicate_book_frames"] == 2 and cap.sequence_regressions == 0
    batches, _st = book_after(cap.book[TICKER])
    assert batches[-1].changes == [(BID, 4800, 1400, 1200)]  # each delta applied once


def test_rotated_captures_are_read_in_time_order(tmp_path: Path) -> None:
    recs = [*k_subscribe(1, False), *session(T0, 1)]
    whole = read(tmp_path, recs)
    a = write_jsonl(tmp_path / "part1.jsonl", recs[:3])
    b = write_jsonl(tmp_path / "part2.jsonl", recs[3:])
    for paths in ([a, b], [b, a]):
        cap = read_kalshi(paths)
        assert [(m.kind, m.levels) for m in cap.book[TICKER]] == [(m.kind, m.levels) for m in whole.book[TICKER]]
        assert cap.sequence_gaps == cap.sequence_regressions == 0


def test_trades_are_deduplicated_by_trade_id_across_websocket_and_rest(tmp_path: Path) -> None:
    ws = k_trade(T0, "0.4900", "4.00", "no", "t-1")
    rest = {"trades": [{**ws["msg"]["msg"], "created_time": "2026-10-23T14:00:00Z"}]}
    rest["trades"][0].pop("ts_ms")
    cap = read_kalshi(
        [write_jsonl(tmp_path / "ws.jsonl", [ws]), write_jsonl(tmp_path / "rest.jsonl", [wrap(T0 + 60 * S, rest)])]
    )
    (t,) = cap.trades[TICKER]
    assert (t.trade_id, t.size, t.ts_recv) == ("t-1", 400, T0 + MS)  # the WebSocket copy is kept
    assert cap.counters["duplicate_trades"] == 1
    # the REST export read first (its file starts earlier): still one print, and the WebSocket copy wins
    cap = read_kalshi(
        [write_jsonl(tmp_path / "ws2.jsonl", [ws]), write_jsonl(tmp_path / "rest2.jsonl", [wrap(T0 - S, rest)])]
    )
    (t,) = cap.trades[TICKER]
    assert t.ts_recv == T0 + MS and cap.counters["duplicate_trades"] == 1
    bad = {"trades": [{**rest["trades"][0], "count_fp": "5.00"}]}
    with pytest.raises(QtError, match=r"trade t-1 .* conflicts with"):
        read_kalshi([write_jsonl(tmp_path / "ws3.jsonl", [ws, wrap(T0 + 60 * S, bad)])])


def rest_book(t: int, yes: str, *, send: int | None = None) -> list[dict]:
    path = f"/trade-api/v2/markets/{TICKER}/orderbook"
    book = {"orderbook_fp": {"yes_dollars": [["0.4800", yes]], "no_dollars": [["0.5000", "7.00"]]}}
    if send is None:
        return [{"recv_ns": t, "path": path, "msg": book}]
    return [
        {"dir": "out", "local_ns": send, "msg": {"method": "GET", "path": path}},
        {"dir": "in", "local_ns": t, "msg": book},
    ]


def test_rest_orderbooks_check_the_websocket_book_instead_of_rewriting_it(tmp_path: Path) -> None:
    """A REST snapshot saved in a second file used to be appended after the whole WebSocket stream, whatever its
    time, and diffed into the book as a depth batch."""
    ws = write_jsonl(tmp_path / "ws.jsonl", [*k_subscribe(1, False), *session(T0, 1)])
    # 0.48 holds 13 contracts until +10 ms, 14 until +20 ms and 12 after
    rest = write_jsonl(
        tmp_path / "rest.jsonl",
        [
            *rest_book(T0 + 5 * MS, "13.00"),  # agrees with the book at its receive time
            *rest_book(T0 + 12 * MS, "13.00", send=T0 + 8 * MS),  # taken during the round trip (13 at the send)
            *rest_book(T0 + 30 * MS, "15.00"),  # agrees with no state: a mismatch
        ],
    )
    cap = read_kalshi([ws, rest])
    msgs = cap.book[TICKER]
    assert [m.kind for m in msgs if m.kind != "check"] == ["snapshot", "delta", "delta"]
    batches, st = book_after(msgs)
    assert [b.kind for b in batches] == ["resync", "depth", "depth"]  # the book is the WebSocket's alone
    assert (st.rest_book_checks, st.rest_book_mismatches, st.rest_book_mismatch_levels) == (3, 1, 1)
    assert st.anomalies() == {"rest_book_mismatches": 1, "rest_book_mismatch_levels": 1}


def test_rest_orderbooks_are_the_book_of_markets_without_a_websocket_stream(tmp_path: Path) -> None:
    late = write_jsonl(tmp_path / "late.jsonl", rest_book(T0 + 9 * S, "20.00"))
    early = write_jsonl(tmp_path / "early.jsonl", rest_book(T0 + 1 * S, "13.00"))
    cap = read_kalshi([late, early])
    assert [(m.kind, m.ts_recv) for m in cap.book[TICKER]] == [("snapshot", T0 + S), ("snapshot", T0 + 9 * S)]
    batches, _st = book_after(cap.book[TICKER])
    assert [b.kind for b in batches] == ["resync", "depth"] and batches[1].changes == [(BID, 4800, 1300, 2000)]


def test_lifecycle_messages_give_the_grid_and_the_settlement(tmp_path: Path) -> None:
    bands = [
        {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
        {"start": "0.1000", "end": "1.0000", "step": "0.0100"},
    ]
    created = {"market_ticker": TICKER, "event_type": "created", "open_ts": T0 // 10**9, "price_ranges": bands}
    determined = {
        "market_ticker": TICKER,
        "event_type": "determined",
        "result": "no",
        "settlement_value": "0.0000",
        "determination_ts": T0 // 10**9 + 60,
    }
    recs = [
        wrap(T0, {"type": "market_lifecycle_v2", "sid": 9, "seq": 1, "msg": created}),
        wrap(T0 + MS, {"type": "market_lifecycle_v2", "sid": 9, "seq": 2, "msg": determined}),
    ]
    m = read(tmp_path, recs).markets[TICKER]
    assert m.grid is not None and m.grid.bands == ((0, 1000, 10), (1000, 10_000, 100))
    assert (m.settlement, m.settled_ns, m.meta["result"]) == (Decimal(0), (T0 // 10**9 + 60) * 10**9, "no")


def test_trades_taker_side_and_block_trades(tmp_path: Path) -> None:
    cap = read(
        tmp_path,
        [
            k_trade(T0, "0.4900", "3.00", "no", "t1"),
            k_trade(T0 + MS, "0.5000", "1.50", "yes", "t2", seq=2, block=True),
        ],
    )
    t1, t2 = cap.trades[TICKER]
    assert (t1.price, t1.size, t1.aggressor, t1.hit_side, t1.trade_id, t1.block) == (4900, 300, ASK, BID, "t1", False)
    assert (t2.aggressor, t2.block, t2.size) == (BID, True, 150)


def own_life(t: int, oid: str = "o-1") -> list[dict]:
    created = t // MS
    return [
        k_user_order(t, oid, "mm-1", "bid", "0.4900", "10.00", "0.00", "10.00", "resting", created),
        k_fill(t + 50 * MS, "t9", oid, "mm-1", "bid", "0.4900", "4.00", "0.0123"),
        k_user_order(
            t + 50 * MS,
            oid,
            "mm-1",
            "bid",
            "0.4900",
            "10.00",
            "4.00",
            "6.00",
            "resting",
            created,
            "Trade",
            maker_cost="1.960000",
        ),
        k_user_order(
            t + 90 * MS,
            oid,
            "mm-1",
            "bid",
            "0.4900",
            "8.00",
            "4.00",
            "4.00",
            "resting",
            created,
            "Decrease",
            maker_cost="1.960000",
        ),
        k_user_order(
            t + 99 * MS,
            oid,
            "mm-1",
            "bid",
            "0.4900",
            "8.00",
            "4.00",
            "0.00",
            "canceled",
            created,
            maker_cost="1.960000",
        ),
    ]


def test_own_order_life_becomes_the_canonical_log(tmp_path: Path) -> None:
    cap = read(tmp_path, [k_snapshot(1, T0, SNAP_YES, SNAP_NO), *own_life(T0 + 100 * MS)])
    res = cap.builder.finish()
    (o,) = res.orders
    assert (o["venue_order_id"], o["instrument"], o["side"], o["client_order_id"]) == ("o-1", TICKER, "buy", "mm-1")
    ev = [(e["event_type"], e.get("amend_keeps_priority")) for e in res.events]
    assert [k for k, _ in ev] == ["ack", "fill", "amend_ack", "cancel_ack"]
    assert ev[2][1] is True  # a decrease keeps priority
    fill = res.events[1]
    assert (fill["qty"], fill["px"], fill["liquidity"], fill["fee"], fill["fill_id"]) == (
        400,
        4900,
        "maker",
        0.0123,
        "t9",
    )
    assert res.events[0]["ts_exchange"] == T0 + 100 * MS  # the ack at created_ts_ms
    assert cap.cids["o-1"] == {"mm-1"} and res.dedupe_dropped == 1  # the fill_count copy of the fill


@pytest.mark.parametrize(
    ("status", "reason", "event"),
    [
        ("canceled", "PostOnlyCrossCancel", "post_only_reject"),
        ("canceled", "ExpiryCancel", "expire"),
        ("canceled", "", "cancel_ack"),
        ("executed", None, "done"),
    ],
)
def test_order_end_reasons(tmp_path: Path, status: str, reason: str | None, event: str) -> None:
    t = T0 + 10 * MS
    recs = [
        k_user_order(t, "o-2", "c", "ask", "0.5100", "5.00", "0.00", "5.00", "resting", t // MS),
        k_user_order(t + MS, "o-2", "c", "ask", "0.5100", "5.00", "0.00", "0.00", status, t // MS, reason),
    ]
    res = read(tmp_path, recs).builder.finish()
    assert res.events[-1]["event_type"] == event and res.orders[0]["side"] == "sell"


def test_price_amend_loses_priority_and_derived_fills(tmp_path: Path) -> None:
    t = T0 + 10 * MS
    recs = [
        k_user_order(t, "o-3", "c", "bid", "0.4900", "5.00", "0.00", "5.00", "resting", t // MS),
        k_user_order(t + MS, "o-3", "c", "bid", "0.4800", "5.00", "0.00", "5.00", "resting", t // MS, "Amend"),
        k_user_order(
            t + 2 * MS,
            "o-3",
            "c",
            "bid",
            "0.4800",
            "5.00",
            "2.00",
            "3.00",
            "resting",
            t // MS,
            "Trade",
            maker_cost="0.960000",
        ),
    ]
    cap = read(tmp_path, recs)
    res = cap.builder.finish()
    kinds = [(e["event_type"], e.get("amend_keeps_priority")) for e in res.events]
    assert kinds[1] == ("amend_ack", False)
    derived = [e for e in res.events if e["event_type"] == "fill"]
    assert len(derived) == 1 and derived[0]["qty"] == 200 and derived[0]["liquidity"] == "maker"
    assert derived[0]["fee"] is None  # the updates reported no fees
    assert any("derived from fill_count" in w for w in cap.warnings)


def test_derived_fills_take_their_fee_from_the_reported_fee_totals(tmp_path: Path) -> None:
    t = T0 + 10 * MS
    common = ("o-5", "c", "bid", "0.4800", "5.00")
    recs = [
        k_user_order(t, *common, "0.00", "5.00", "resting", t // MS, maker_fees="0.000000"),
        k_user_order(t + MS, *common, "2.00", "3.00", "resting", t // MS, "Trade", "0.960000", "0.004400"),
        k_user_order(t + 2 * MS, *common, "5.00", "0.00", "executed", t // MS, "Trade", "2.400000", "0.011000"),
    ]
    fills = [e for e in read(tmp_path, recs).builder.finish().events if e["event_type"] == "fill"]
    assert [(e["qty"], e["fee"], e["fee_ccy"]) for e in fills] == [(200, 0.0044, "USD"), (300, 0.0066, "USD")]


def test_restated_order_open_before_the_capture(tmp_path: Path) -> None:
    t = T0 + 10_000 * MS
    recs = [
        k_snapshot(1, t, SNAP_YES, SNAP_NO),
        k_user_order(t, "o-4", "c", "bid", "0.4900", "5.00", "1.00", "4.00", "resting", (t - 600_000 * MS) // MS),
    ]
    res = read(tmp_path, recs).builder.finish()
    assert [e["event_type"] for e in res.events] == ["restate"]


def test_rest_exports_queue_positions_markets_series_and_programs(tmp_path: Path) -> None:
    qp_path = "/trade-api/v2/portfolio/orders/o-1/queue_position"
    bands = [("0.0000", "0.1000", "0.0010"), ("0.1000", "0.9000", "0.0100"), ("0.9000", "1.0000", "0.0010")]
    market = {
        "ticker": TICKER,
        "price_ranges": [{"start": a, "end": b, "step": c} for a, b, c in bands],
        "result": "yes",
        "settlement_value_dollars": "1.0000",
        "fee_waiver_expiration_time": "2026-10-23T15:00:00Z",
    }
    lip = {
        "id": "p1",
        "market_ticker": TICKER,
        "incentive_type": "liquidity",
        "start_date": "2026-10-23T00:00:00Z",
        "end_date": "2026-10-24T00:00:00Z",
        "period_reward": 500000,  # centi-cents: $50
        "discount_factor_bps": 5000,
        "target_size_fp": "300.00",
    }
    book = {"yes_dollars": [["0.4900", "5.00"]], "no_dollars": [["0.5000", "2.00"]]}
    recs = [
        {"dir": "out", "local_ns": T0, "msg": {"method": "GET", "path": qp_path}},
        {"dir": "in", "local_ns": T0 + 40 * MS, "msg": {"queue_position_fp": "12.00"}},
        wrap(
            T0 + 50 * MS,
            {"queue_positions": [{"order_id": "o-2", "market_ticker": TICKER, "queue_position_fp": "3.50"}]},
        ),
        {"recv_ns": T0, "path": f"/trade-api/v2/markets/{TICKER}/orderbook", "msg": {"orderbook_fp": book}},
        wrap(T0, {"market": market}),
        wrap(T0, {"series": {"ticker": "KXTEST", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}}),
        wrap(T0, {"incentive_programs": [lip, {"id": "v1", "market_ticker": TICKER, "incentive_type": "volume"}]}),
        wrap(T0, {"something_else": True}),
    ]
    cap = read(tmp_path, recs)
    s1, s2 = cap.samples
    assert (s1.order_id, s1.queue_position, s1.recv_ns, s1.send_ns) == ("o-1", 1200, T0 + 40 * MS, T0)
    assert (s2.order_id, s2.queue_position, s2.market) == ("o-2", 350, TICKER)
    assert cap.book[TICKER][0].levels == [(BID, 4900, 500), (ASK, 5000, 200)]
    info = cap.markets[TICKER]
    assert info.grid is not None and info.grid.bands == ((0, 1000, 10), (1000, 9000, 100), (9000, 10000, 10))
    assert info.settlement == Decimal("1.0000") and info.meta["result"] == "yes"
    assert info.kalshi_fee is not None and info.kalshi_fee.fee_type == "quadratic_with_maker_fees"
    assert info.kalshi_fee.fee_waiver_until_ns == 1_792_767_600_000_000_000
    (lip,) = info.lip
    assert (lip.period_reward, lip.target_size, lip.discount_factor) == (Decimal(50), 30_000, Decimal("0.5"))
    assert lip.total_snapshots == 86_400
    assert cap.counters["incentive_programs_not_liquidity"] == 1 and cap.counters["unrecognised_rest_payloads"] == 1


def test_flat_series_and_missing_series_are_left_to_the_scorer(tmp_path: Path) -> None:
    cap = read(
        tmp_path, [wrap(T0, {"series": {"ticker": "KXTEST", "fee_type": "flat"}}), k_trade(T0, "0.5", "1", "yes", "t")]
    )
    assert cap.markets[TICKER].kalshi_fee is None and cap.markets[TICKER].meta["kalshi_fee_type"] == "flat"
    assert any("fee_type flat" in w for w in cap.warnings)
    cap = read(tmp_path, [k_trade(T0, "0.5", "1", "yes", "t")])
    assert cap.markets[TICKER].meta["kalshi_fee_type"] is None


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (wrap(T0, {"type": "orderbook_delta", "sid": 1, "seq": 1, "msg": {"side": "yes"}}), "without market_ticker"),
        (k_delta(1, T0, "maybe", "0.5", "1", clock=1), "orderbook_delta side"),
        (k_delta(1, T0, "yes", "0.12345", "1", clock=1), "not representable"),
        (
            wrap(T0, {"type": "trade", "msg": {"market_ticker": TICKER, "yes_price_dollars": "0.5", "count_fp": "1"}}),
            "without taker side",
        ),
        (wrap(T0, {"type": "fill", "msg": {"market_ticker": TICKER}}), "fill without order_id"),
        ({"queue_position_fp": "1.00"}, "without an order id"),
        ({"order_id": "x", "queue_position_fp": "1.00"}, "without an order id"),
        (wrap(T0, {"series": {"ticker": "S", "fee_type": "cubic"}}), "fee_type"),
    ],
)
def test_malformed_kalshi_records(tmp_path: Path, record: dict, message: str) -> None:
    with pytest.raises(QtError) as e:
        read(tmp_path, [record])
    assert e.value.code in ("QT-INPUT-002", "QT-INPUT-004") and message in e.value.message


def test_queue_position_needs_a_receive_time(tmp_path: Path) -> None:
    with pytest.raises(QtError, match="local receive time"):
        read(tmp_path, [{"queue_positions": [{"order_id": "o-1", "queue_position_fp": "1.00"}]}])
    with pytest.raises(QtError, match="yes_price"):
        read_kalshi([], yes_price="maybe")
