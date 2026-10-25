"""Own-order removal and queue entry points (`queuetruth.pm.ownfree`)."""

from __future__ import annotations

from pm._helpers import depth, resync, trade

from queuetruth.pm.book import ASK, BID
from queuetruth.pm.ownfree import remove_own
from queuetruth.score.l2_own import OwnOrder

W = 1_000_000_000


def numbered(batches):
    for k, b in enumerate(batches, start=1):
        b.seq = k
    return batches


def book():
    return numbered(
        [
            resync(100, [(BID, 4900, 10), (ASK, 5100, 10)]),
            depth(200, [(BID, 4900, 10, 15)]),  # own add of 5 (attributed by tag below)
            depth(300, [(BID, 4900, 15, 22)]),  # someone else adds 7
            depth(400, [(BID, 4900, 22, 17)], [trade(400, 4900, 5, ASK)]),  # the own order fills 5 (a print)
            depth(500, [(ASK, 5100, 10, 13)]),  # own ask added (no tag: aligned by time and size)
            depth(600, [(ASK, 5100, 13, 10)]),  # own ask cancelled
        ]
    )


def test_exact_aligned_entries_and_the_own_free_replay() -> None:
    bs = book()
    bs[1].own_tags = [(BID, 4900, 5, "mm-1")]
    own = [
        OwnOrder(1, "B", 4900, ((199, 5), (400, 0)), ((400, 5, 4900),), key="k:bid"),
        OwnOrder(1, "A", 5100, ((505, 3), (590, 0)), (), key="k:ask"),
    ]
    res = remove_own(bs, own, {"k:bid": {"mm-1"}}, W)
    e = {x.key: x for x in res.entries}
    assert (e["k:bid"].how, e["k:bid"].seq, e["k:bid"].inside, e["k:bid"].qty) == ("exact", 2, True, 5)
    assert (e["k:ask"].how, e["k:ask"].seq) == ("aligned", 5)
    st = res.stats
    assert (st.orders, st.exact, st.aligned, st.unaligned, st.changes, st.own_tags, st.own_tags_unused) == (
        2,
        1,
        3,
        0,
        4,
        1,
        0,
    )
    assert res.own_after(BID, 4900, 1) == 5 and res.own_after(BID, 4900, 0) == 0 and res.own_after(ASK, 5100, 4) == 3
    kinds = [(b.seq, b.kind, b.changes, [t.size for t in b.trades]) for b in res.batches]
    # the own add and the own ask's add / cancel become no-ops; the fill's print is kept for the twin
    assert kinds == [
        (1, "resync", [], []),
        (3, "depth", [(BID, 4900, 10, 17)], []),
        (4, "trade", [], [5]),
        # batch 4's level change 22 -> 17 is the own order leaving: the own-free level stays at 17
    ]
    assert st.noop_batches == 3 and st.clamps == 0


def test_unaligned_restated_outside_and_clamps() -> None:
    bs = numbered(
        [
            resync(100, [(BID, 4900, 4), (ASK, 5100, 10)]),
            depth(200, [(BID, 4900, 4, 6)]),
            depth(9 * W, [(ASK, 5100, 10, 2)]),
        ]
    )
    own = [
        # restated: resting since before the first batch; its 8 exceeds the visible 4 -> clamped
        OwnOrder(1, "B", 4900, ((50, 8),), (), key="r", restated=True),
        # an add at 5100 nobody saw within the window: placed before the next batch touching the level
        OwnOrder(1, "A", 5100, ((300, 1),), (), key="u"),
        # after the last batch
        OwnOrder(1, "A", 5200, ((20 * W, 1),), (), key="o"),
    ]
    res = remove_own(bs, own, {}, W)
    e = {x.key: x for x in res.entries}
    assert (e["r"].how, e["r"].seq, e["r"].qty) == ("restated", 0, 8)
    assert (e["u"].how, e["u"].seq, e["u"].inside) == ("unaligned", 3, False)
    assert (e["o"].how, e["o"].seq) == ("outside", 4)
    assert res.stats.clamps >= 1
    first = res.batches[0]
    assert first.levels == [(ASK, 5100, 10)]  # the bid level is all own size: removed
