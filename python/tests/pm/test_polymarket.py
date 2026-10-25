"""The Polymarket reader on hand-made captures in the documented message formats (`queuetruth.pm.polymarket`)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pm._helpers import COND, MS, NO, T0, YES, p_book, p_change, p_gamma, p_order, p_print, wrap, write_jsonl

from queuetruth.errors import QtError
from queuetruth.pm.book import ASK, BID
from queuetruth.pm.polymarket import read_polymarket

BIDS = [("0.40", "100"), ("0.41", "50")]
ASKS = [("0.45", "30"), ("0.44", "20")]


def read(tmp: Path, records: list[Any], **kw: Any):
    return read_polymarket([write_jsonl(tmp / "p.jsonl", records)], **kw)


def test_yes_book_is_the_canonical_ladder_and_the_no_book_its_mirror(tmp_path: Path) -> None:
    recs = [
        p_gamma(),
        p_book(T0, YES, BIDS, ASKS),
        p_book(T0, NO, [("0.55", "30"), ("0.56", "20")], [("0.60", "100"), ("0.59", "50")]),  # the same book
        p_change(T0 + 10 * MS, [(YES, "0.41", "70", "BUY"), (NO, "0.59", "70", "SELL")], bba=("0.41", "0.44")),
        p_change(T0 + 20 * MS, [(NO, "0.57", "5", "BUY")]),  # only the NO book moves: the tops disagree
    ]
    cap, _tx = read(tmp_path, recs)
    (sym,) = cap.markets
    assert sym == "test-market"
    snap, chg = cap.book[sym]
    assert sorted(snap.levels) == sorted(
        [(BID, 4000, 100_000_000), (BID, 4100, 50_000_000), (ASK, 4500, 30_000_000), (ASK, 4400, 20_000_000)]
    )
    assert chg.kind == "set" and chg.levels == [(BID, 4100, 70_000_000)] and chg.bba == (4100, 4400)
    assert cap.counters["complement_book_messages"] == 3 and cap.counters["complement_top_mismatch"] == 1
    info = cap.markets[sym]
    assert (info.yes_asset, info.no_asset, info.condition_id, info.grid.finest) == (YES, NO, COND, 100)
    assert (
        info.poly_fee is not None
        and info.poly_fee.rate == Decimal("0.04")
        and info.poly_fee.rebate_rate == Decimal("0.25")
    )
    assert info.poly_rewards == [
        {"daily_rate": "20", "start_date": "2026-10-01", "end_date": "2026-10-31", "asset_address": None}
    ]
    assert (info.meta["rewards_min_size"], info.meta["rewards_max_spread"]) == ("10", "3")


def test_a_no_token_capture_alone_is_mirrored(tmp_path: Path) -> None:
    recs = [p_gamma(), p_book(T0, NO, [("0.55", "30")], [("0.60", "100")])]
    cap, _ = read(tmp_path, recs)
    (snap,) = cap.book["test-market"]
    assert sorted(snap.levels) == [(ASK, 4500, 30_000_000), (BID, 4000, 100_000_000)]
    bba = p_change(T0 + 5, [(NO, "0.55", "10", "BUY")], bba=("0.55", "0.60"))
    cap, _ = read(tmp_path, [*recs, bba])
    assert cap.book["test-market"][1].bba == (4000, 4500)  # NO best bid / ask mirrored to YES ask / bid


def test_prints_map_to_yes_and_dedupe_across_tokens(tmp_path: Path) -> None:
    tx = "0x" + "aa" * 32
    recs = [
        p_gamma(),
        p_book(T0, YES, BIDS, ASKS),
        p_print(T0 + 1, YES, "0.44", "5", "BUY", tx),
        p_print(T0 + 1, NO, "0.56", "5", "SELL", tx),  # the complement's copy of the same trade
        p_print(T0 + 2, NO, "0.59", "1", "BUY", ""),  # no hash on the complement token: skipped
        p_print(T0 + 3, YES, "0.41", "2", "SELL", "0x" + "bb" * 32),
    ]
    cap, tx_times = read(tmp_path, recs)
    t1, t2 = cap.trades["test-market"]
    assert (t1.price, t1.size, t1.aggressor, t1.tx_hash) == (4400, 5_000_000, BID, tx)
    assert (t2.price, t2.aggressor, t2.hit_side) == (4100, ASK, BID)
    assert cap.counters["duplicate_prints"] == 1 and cap.counters["complement_prints_skipped"] == 1
    assert tx_times[tx] == T0 // MS * MS


def test_equal_prints_of_one_transaction_stay_separate(tmp_path: Path) -> None:
    """Deduplication is across tokens only, one for one: two genuinely separate 10-share prints at 0.45 in one
    transaction on the book token both stay, their two mirrors on the other token go, and a mirror with no print
    left to match is kept."""
    tx = "0x" + "cc" * 32
    recs = [
        p_gamma(),
        p_book(T0, YES, BIDS, ASKS),
        p_print(T0 + 1, YES, "0.45", "10", "BUY", tx),
        p_print(T0 + 1, YES, "0.45", "10", "BUY", tx),
        p_print(T0 + 2, NO, "0.55", "10", "SELL", tx),
        p_print(T0 + 2, NO, "0.55", "10", "SELL", tx),
        p_print(T0 + 2, NO, "0.55", "10", "SELL", tx),  # a third print the book token did not report
    ]
    cap, _ = read(tmp_path, recs)
    prints = cap.trades["test-market"]
    assert [(t.price, t.size, t.aggressor) for t in prints] == [(4500, 10_000_000, BID)] * 3
    assert cap.counters["duplicate_prints"] == 2 and cap.counters["complement_prints_kept"] == 1


def test_sdk_events_metadata_pages_and_resolution(tmp_path: Path) -> None:
    sdk_book = {
        "topic": "market",
        "type": "book",
        "payload": {
            "conditionId": COND,
            "assetId": YES,
            "timestamp": str(T0 // MS),
            "bids": [{"price": "0.4", "size": "1"}],
            "asks": [{"price": "0.6", "size": "1"}],
        },
    }
    resolved = {"event_type": "market_resolved", "market": COND, "winning_asset_id": NO, "timestamp": str(T0 // MS + 9)}
    tick = {
        "event_type": "tick_size_change",
        "market": COND,
        "asset_id": YES,
        "new_tick_size": "0.001",
        "timestamp": str(T0 // MS + 5),
    }
    cap, _ = read(tmp_path, [{"data": [p_gamma()]}, wrap(T0, sdk_book), wrap(T0, tick), wrap(T0, resolved)])
    info = cap.markets["test-market"]
    assert info.settlement == 0 and info.settled_ns == T0 + 9 * MS  # NO won
    assert info.grid.finest == 10 and cap.counters["tick_size_changes"] == 1
    assert cap.book["test-market"][0].levels == [(BID, 4000, 1_000_000), (ASK, 6000, 1_000_000)]


def test_markets_without_metadata(tmp_path: Path) -> None:
    one = [p_book(T0, YES, BIDS, ASKS)]
    cap, _ = read(tmp_path, one)
    assert list(cap.markets) == [COND] and any("as its own instrument" in w for w in cap.warnings)
    with pytest.raises(QtError, match="no YES / NO mapping"):
        read(tmp_path, [*one, p_book(T0, NO, [], [])])
    cap, _ = read(tmp_path, [*one, p_book(T0, NO, [], [])], markets={COND: (YES, NO)})
    assert cap.markets[COND].no_asset == NO
    with pytest.raises(QtError, match="are not \\[Yes, No\\]"):
        read(tmp_path, [p_gamma(outcomes='["Up", "Down"]')])


def maker_trade(t: int, status: str, oid: str, amount: str, price: str, owner: str = "key-1") -> dict:
    return wrap(
        t,
        {
            "event_type": "trade",
            "type": "TRADE",
            "id": "tr-1",
            "taker_order_id": "0xtaker",
            "market": COND,
            "asset_id": YES,
            "side": "SELL",
            "size": amount,
            "price": price,
            "status": status,
            "owner": owner,
            "trader_side": "MAKER",
            "timestamp": str(t // MS),
            "transaction_hash": "0x" + "cc" * 32,
            "maker_orders": [
                {
                    "order_id": oid,
                    "owner": owner,
                    "matched_amount": amount,
                    "price": price,
                    "asset_id": YES,
                    "side": "BUY",
                },
                {
                    "order_id": "0xother",
                    "owner": "key-2",
                    "matched_amount": "9",
                    "price": price,
                    "asset_id": YES,
                    "side": "BUY",
                },
            ],
        },
    )


def test_own_orders_and_trades_become_the_canonical_log(tmp_path: Path) -> None:
    oid = "0x" + "01" * 32
    recs = [
        p_gamma(),
        p_book(T0, YES, BIDS, ASKS),
        p_order(T0 + 10 * MS, oid, "BUY", "0.41", "25", "0", "PLACEMENT"),
        maker_trade(T0 + 20 * MS, "MATCHED", oid, "10", "0.41"),
        maker_trade(T0 + 22 * MS, "MINED", oid, "10", "0.41"),
        maker_trade(T0 + 24 * MS, "CONFIRMED", oid, "10", "0.41"),
        p_order(T0 + 30 * MS, oid, "BUY", "0.41", "25", "10", "CANCELLATION"),
        # a NO-token order: BUY NO at 0.57 rests as a YES ask at 0.43
        p_order(T0 + 40 * MS, "0x02", "BUY", "0.57", "5", "0", "PLACEMENT", asset=NO),
    ]
    cap, _ = read(tmp_path, recs)
    res = cap.builder.finish()
    orders = {o["venue_order_id"]: o for o in res.orders}
    assert orders[oid]["side"] == "buy" and orders["0x02"]["side"] == "sell"
    assert {e["event_type"] for e in res.events} == {"ack", "fill", "cancel_ack"}
    (fill,) = [e for e in res.events if e["event_type"] == "fill"]
    assert (fill["qty"], fill["px"], fill["liquidity"], fill["fee"]) == (10_000_000, 4100, "maker", 0.0)
    assert cap.counters["trade_status_updates"] == 2 and cap.counters["own_fills"] == 1
    no_ack = [e for e in res.events if e["order_key"].endswith("0x02")]
    assert no_ack[0]["px"] == 4300


def test_failed_trade_busts_the_fill_and_restated_orders(tmp_path: Path) -> None:
    oid = "0x" + "03" * 32
    recs = [
        p_gamma(),
        p_book(T0 + 3_600_000 * MS, YES, BIDS, ASKS),
        # first seen as an UPDATE, created an hour before the capture: restated
        p_order(T0 + 3_600_010 * MS, oid, "BUY", "0.41", "25", "5", "UPDATE"),
        maker_trade(T0 + 3_600_020 * MS, "MATCHED", oid, "10", "0.41"),
        maker_trade(T0 + 3_600_030 * MS, "FAILED", oid, "10", "0.41"),
    ]
    recs[2]["msg"]["created_at"] = str(T0 // 1_000_000_000)
    cap, _ = read(tmp_path, recs)
    res = cap.builder.finish()
    kinds = [e["event_type"] for e in res.events]
    assert kinds[0] == "restate" and "fill" in kinds and kinds[-1] == "fill_bust"


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (p_change(T0, [(YES, "0.41", "1", "HOLD")]), "price_change side"),
        (p_print(T0, YES, "0.41", "1", "HOLD", "0x1"), "last_trade_price side"),
        (wrap(T0, {"event_type": "book", "market": COND}), "without asset_id"),
        (wrap(T0, {"event_type": "order", "market": COND}), "order event without id"),
        (p_book(T0, YES, [("0.40005", "1")], []), "not representable"),
    ],
)
def test_malformed_polymarket_records(tmp_path: Path, record: dict, message: str) -> None:
    with pytest.raises(QtError) as e:
        read(tmp_path, [p_gamma(), record])
    assert message in e.value.message


def test_maker_orders_without_an_event_owner_are_only_the_users_known_orders(tmp_path: Path) -> None:
    oid = "0x" + "04" * 32
    trade = maker_trade(T0 + 20 * MS, "MATCHED", oid, "10", "0.41")
    del trade["msg"]["owner"]
    for mo in trade["msg"]["maker_orders"]:
        del mo["owner"]
    recs = [p_gamma(), p_book(T0, YES, BIDS, ASKS), p_order(T0 + 10 * MS, oid, "BUY", "0.41", "25", "0", "PLACEMENT")]
    cap, _ = read(tmp_path, [*recs, trade])
    res = cap.builder.finish()
    fills = [e for e in res.events if e["event_type"] == "fill"]
    assert [f["order_key"] for f in fills] == [f"polymarket:default:{oid}"]  # not the other maker "0xother"
    assert {o["venue_order_id"] for o in res.orders} == {oid}
