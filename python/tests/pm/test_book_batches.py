"""The canonical ladder and batch building (`queuetruth.pm.book`, `queuetruth.pm.batches`)."""

from __future__ import annotations

import pytest

from queuetruth.errors import QtError
from queuetruth.pm.batches import BookMsg, TradeMsg, build_batches
from queuetruth.pm.book import ASK, BID, NONE, Ladder, opposite

W = 1_000_000_000


def test_ladder_order_rank_and_cross() -> None:
    b = Ladder()
    for p, q in [(4800, 5), (5000, 7), (4900, 1)]:
        b.set(BID, p, q)
    b.set(ASK, 5200, 3)
    b.set(ASK, 5100, 2)
    assert b.best(BID) == 5000 and b.best(ASK) == 5100
    assert list(b.levels(BID)) == [(5000, 7), (4900, 1), (4800, 5)]
    assert b.depth(ASK, 1) == [(5100, 2)] and b.depth(BID) == [(5000, 7), (4900, 1), (4800, 5)]
    assert b.rank(BID, 4900) == 1 and b.rank(BID, 4950) == 1 and b.rank(ASK, 5200) == 1
    assert b.set(BID, 4900, 0) == 1 and b.qty(BID, 4900) == 0 and len(b) == 4
    assert not b.crossed()
    b.set(BID, 5100, 1)
    assert b.crossed()
    assert b.snapshot()[ASK] == {5100: 2, 5200: 3}
    b.clear()
    assert len(b) == 0 and b.best(ASK) is None
    assert opposite(BID) == ASK and opposite(ASK) == BID and opposite(NONE) == NONE
    with pytest.raises(QtError):
        b.set(BID, 1, -1)


def snap(ts: int, levels: list[tuple[int, int, int]], **kw) -> BookMsg:
    return BookMsg("snapshot", ts, ts, levels, **kw)


def delta(ts: int, side: int, price: int, d: int, group=None, tag=None, bba=None) -> BookMsg:
    return BookMsg("delta", ts, ts, [(side, price, d)], group=group, own_tag=tag, bba=bba)


def test_batches_group_by_clock_and_record_changes() -> None:
    msgs = [
        delta(1, BID, 4900, 5),  # before any snapshot: skipped
        snap(10, [(BID, 4900, 10), (ASK, 5100, 10)]),
        delta(20, BID, 4900, -4, group=("clock", 7), tag="mm-1"),
        delta(20, ASK, 5100, 3, group=("clock", 7)),
        delta(30, BID, 5000, 6, group=("clock", 8)),
    ]
    out, st = build_batches(msgs, [], match_window_ns=W)
    assert [b.kind for b in out] == ["resync", "depth", "depth"] and [b.seq for b in out] == [1, 2, 3]
    assert out[0].levels == [(BID, 4900, 10), (ASK, 5100, 10)]
    assert out[1].changes == [(BID, 4900, 10, 6), (ASK, 5100, 10, 13)] and out[1].n_msgs == 2
    assert out[1].own_tags == [(BID, 4900, -4, "mm-1")]
    assert out[1].decrease(BID, 4900) == 4 and out[1].decrease(ASK, 5100) == 0
    assert st.pre_snapshot_rows_skipped == 1 and st.resyncs == 1 and st.messages == 5


def test_restating_snapshots_count_mismatches_and_gaps_drop_deltas() -> None:
    msgs = [
        snap(10, [(BID, 4900, 10), (ASK, 5100, 10)]),
        snap(20, [(BID, 4900, 10), (ASK, 5100, 10)], restate_expected=True),  # identical: no batch content
        snap(30, [(BID, 4900, 12), (ASK, 5100, 10)], restate_expected=True),  # differs at one level
        BookMsg("gap", 40, 40),
        delta(50, BID, 4900, 1),  # dropped inside the gap
        snap(60, [(BID, 4800, 3), (ASK, 5100, 9)]),  # closes the gap with a resync
        delta(70, ASK, 5100, -20),  # would go negative: clamped
    ]
    out, st = build_batches(msgs, [], match_window_ns=W)
    assert [b.kind for b in out] == ["resync", "depth", "depth", "resync", "depth"]
    assert out[2].changes == [(BID, 4900, 10, 12)]
    assert st.snapshot_mismatch_levels == 1 and st.deltas_dropped_in_gap == 1
    assert st.feed_gaps == [(30, 60, "sequence gap until the next snapshot")]
    assert st.negative_level == 1 and out[-1].changes == [(ASK, 5100, 9, 0)]
    assert st.anomalies() == {"deltas_dropped_in_gap": 1, "negative_level": 1, "snapshot_mismatch_levels": 1}


def test_trades_attach_to_the_batch_that_explains_them() -> None:
    msgs = [
        snap(0, [(BID, 4900, 10), (ASK, 5100, 10), (ASK, 5200, 5)]),
        delta(100, ASK, 5100, -4),
        delta(300, ASK, 5100, -6),
        delta(500, ASK, 5200, -1),
    ]
    trades = [
        TradeMsg(290, 290, 5100, 6, BID, trade_id="t1"),  # exactly the second decrease (nearest exact first)
        TradeMsg(110, 110, 5100, 4, BID, trade_id="t2"),
        TradeMsg(505, 505, 5200, 3, BID, trade_id="t3"),  # explains only 1 of 3: partially explained
        TradeMsg(900 * W, 900 * W, 4900, 2, ASK, trade_id="t4"),  # no batch in the window: a trade batch
        TradeMsg(10, 10, 4900, 9, ASK, block=True),  # block trades never touch the book
    ]
    out, st = build_batches(msgs, trades, match_window_ns=W)
    by_id = {t.trade_id: b.seq for b in out for t in b.trades}
    assert by_id["t2"] == 2 and by_id["t1"] == 3 and by_id["t3"] == 4
    assert out[-1].kind == "trade" and out[-1].trades[0].trade_id == "t4"
    assert (st.trades, st.trades_matched, st.trades_partially_explained, st.trades_unmatched, st.block_trades) == (
        5,
        3,
        1,
        1,
        1,
    )
    assert TradeMsg(0, 0, 1, 1, BID).hit_side == ASK and TradeMsg(0, 0, 1, 1, NONE).hit_side == NONE


def test_prints_explained_only_by_the_other_side_are_counted() -> None:
    """A print whose side is the maker's rather than the taker's matches no decrease on the side it names but one
    on the other side at its price: counted, so a flipped side convention shows up in the data quality."""
    msgs = [snap(0, [(BID, 5000, 10), (ASK, 5100, 10)]), delta(100, BID, 5000, -4), delta(200, ASK, 5100, -2)]
    trades = [
        TradeMsg(100, 100, 5000, 4, BID, trade_id="flipped"),  # names a buy, but the bid at 0.50 shrank
        TradeMsg(5 * W, 5 * W, 5100, 1, BID, trade_id="late"),  # no change near it on either side
    ]
    out, st = build_batches(msgs, trades, match_window_ns=W)
    assert (st.trades_unmatched, st.trades_other_side_explained) == (2, 1)
    assert st.anomalies()["trades_other_side_explained"] == 1
    assert [b.kind for b in out].count("trade") == 2


def test_crossed_out_of_order_and_bba_checks() -> None:
    msgs = [
        snap(10, [(BID, 4900, 10), (ASK, 5100, 10)]),
        delta(20, BID, 5100, 1, bba=(5100, 5100)),
        delta(15, BID, 5100, -1, bba=(4900, 5000)),  # earlier than the last batch, and the stated ask is wrong
    ]
    out, st = build_batches(msgs, [], match_window_ns=W)
    assert st.crossed_books_after_last == 1 and st.out_of_order_ts_event == 1 and st.bba_mismatch == 1
    assert len(out) == 3


def test_a_cancel_attributed_to_the_user_never_takes_another_traders_prints() -> None:
    """The user's cancel (client_order_id on the delta) and a taker's sweep remove 10 contracts each from one level
    in the same millisecond: the sweep's two prints belong to the sweep. Size and time alone tie, and the earlier
    batch, the cancel, used to take them."""
    msgs = [
        snap(0, [(BID, 4900, 10), (ASK, 5800, 2500)]),
        delta(5 * W, ASK, 5800, -1000, group=("clock", 1), tag="mm-1"),  # the user's cancel
        delta(5 * W, ASK, 5800, -1000, group=("clock", 2)),  # the taker
    ]
    prints = [
        TradeMsg(5 * W, 5 * W, 5800, 200, BID, trade_id="a"),
        TradeMsg(5 * W, 5 * W, 5800, 800, BID, trade_id="b"),
    ]
    out, st = build_batches(msgs, prints, match_window_ns=W)
    assert [len(b.trades) for b in out] == [0, 0, 2] and st.trades_matched == 2
    # the user's own taker order consuming a level is tagged too: its prints take its change first
    msgs = [
        snap(0, [(BID, 4900, 10), (ASK, 5800, 2500)]),
        delta(5 * W, ASK, 5800, -300, group=("clock", 1)),  # another trader's cancel of the same size
        delta(5 * W, ASK, 5800, -300, group=("clock", 2), tag="mm-2"),  # the user's buy taking 3
    ]
    own = TradeMsg(5 * W, 5 * W, 5800, 300, BID, trade_id="mine", own=True)
    out, _st = build_batches(msgs, [own], match_window_ns=W)
    assert [[t.trade_id for t in b.trades] for b in out] == [[], [], ["mine"]]
