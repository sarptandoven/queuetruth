"""Book priority rules and the capture check (replay vs the venue's snapshots)."""

from __future__ import annotations

import json
from pathlib import Path

from livefill import check
from livefill.book import BUY, SELL, L3Book, side_of
from livefill.capture import CaptureConfig, CaptureSession
from livefill.stream import segment_streams


def test_priority_rules_and_queue_position() -> None:
    b = L3Book()
    b.seed([(BUY, 100, 5, 1), (BUY, 100, 7, 2), (BUY, 99, 4, 3), (SELL, 101, 6, 4)])
    b.created(10, BUY, 100, 3)
    a = b.ahead(10)
    assert a is not None and (a.orders, a.qty, a.level_rank, a.better_qty) == (2, 12, 0, 0)
    b.changed(1, BUY, 100, 2)  # size down: keeps its place
    assert list(b.levels[BUY][100]) == [1, 2, 10]
    b.changed(1, BUY, 100, 9)  # size up: back of the level
    assert list(b.levels[BUY][100]) == [2, 10, 1] and b.counters["requeued"] == 1
    b.changed(2, BUY, 99, 7)  # price change: back of the new level
    assert list(b.levels[BUY][99]) == [3, 2]
    a3 = b.ahead(3)
    assert a3 is not None and (a3.level_rank, a3.better_qty) == (1, 3 + 9)
    assert b.deleted(10) is not None and b.deleted(10) is None and b.counters["deleted_unknown"] == 1
    b.changed(77, SELL, 102, 1)
    assert b.counters["changed_unknown"] == 1 and b.top(SELL, 5) == [(101, 6, 4), (102, 1, 77)]
    b.created(4, SELL, 101, 6)
    assert b.counters["created_duplicate"] == 1
    assert b.best(BUY) == 100 and b.best(SELL) == 101 and b.ahead(999) is None
    assert side_of(0) == BUY and side_of("1") == SELL and side_of(2) is None


def test_same_timestamp_creates_rank_by_order_id() -> None:
    b = L3Book()
    b.created(5, BUY, 100, 1, us=1000)
    b.created(9, BUY, 100, 1, us=2000)
    b.created(8, BUY, 100, 1, us=2000)  # arrives after 9 in the same ms, smaller id: ahead of 9
    b.created(7, BUY, 100, 1, us=3000)  # later ms: back of the queue whatever its id
    assert list(b.levels[BUY][100]) == [5, 8, 9, 7]
    arrival = L3Book(id_ties=False)
    for oid, us in ((5, 1000), (9, 2000), (8, 2000)):
        arrival.created(oid, BUY, 100, 1, us=us)
    assert list(arrival.levels[BUY][100]) == [5, 9, 8]


def _order(ev: str, oid: int, side: int, px: str, amt: str, us: int, eid: int) -> str:
    return json.dumps(
        {
            "data": {
                "id": oid,
                "order_type": side,
                "price_str": px,
                "amount_str": amt,
                "microtimestamp": str(us),
            },
            "channel": "live_orders_tstusd",
            "event": ev,
            "event_id": f"e{eid}",
            "pre_event_id": f"e{eid - 1}",
        }
    )


def _snap(us: int, bids: list[list[str]], asks: list[list[str]]) -> str:
    data = {"microtimestamp": str(us), "bids": bids, "asks": asks}
    return json.dumps({"event": "data", "channel": "detail_order_book_tstusd", "data": data})


def _capture(tmp: Path, frames: list[str]) -> Path:
    t = [0]

    def clock() -> int:
        t[0] += 1000
        return t[0]

    s = CaptureSession(CaptureConfig(out=tmp / "cap", markets=["tstusd"], rest_snapshot=False), clock, clock)
    s.connected()
    for f in frames:
        s.handle(f)
    s.close()
    return tmp / "cap"


SEED = _snap(1000, [["1.00", "1.0", "1"], ["1.00", "2.0", "2"]], [["1.01", "1.0", "3"]])


def test_check_consistent_explained_and_unexplained(tmp_path: Path) -> None:
    frames = [
        SEED,
        _order("order_created", 10, 0, "1.00", "0.5", 2000, 1),
        _snap(2500, [["1.00", "1.0", "1"], ["1.00", "2.0", "2"], ["1.00", "0.5", "10"]], [["1.01", "1.0", "3"]]),
        # snapshot ahead of live_orders: order 2 is already gone, its delete arrives next (explained, skew 0)
        _snap(3500, [["1.00", "1.0", "1"], ["1.00", "0.5", "10"]], [["1.01", "1.0", "3"]]),
        _order("order_deleted", 2, 0, "1.00", "2.0", 3000, 2),
        _snap(4500, [["1.00", "1.0", "1"], ["1.00", "0.5", "10"]], [["1.01", "1.0", "3"]]),
    ]
    doc = check.check_capture(_capture(tmp_path / "a", frames))
    st = doc["streams"][0]
    assert doc["verdict"] == "consistent"
    assert (st["compared"], st["exact"], st["explained"], st["unexplained"]) == (3, 2, 1, 0)
    assert st["differences"]["extra"] == 1 and st["max_skew_messages"] == 0
    # an order the snapshots show but live_orders never mentions: a lost message
    lost = [
        *frames,
        _snap(5500, [["1.00", "1.0", "1"], ["1.00", "0.5", "10"]], [["1.01", "1.0", "3"], ["1.02", "1", "99"]]),
    ]
    doc2 = check.check_capture(_capture(tmp_path / "b", lost))
    st2 = doc2["streams"][0]
    assert doc2["verdict"] == "inconsistent" and st2["unexplained"] == 1
    assert st2["examples"][0]["differences"] == [
        {"kind": "missing", "side": "sell", "order_id": 99, "px": 1_020_000_000}
    ]


def test_check_window_and_queue_order(tmp_path: Path) -> None:
    frames = [
        SEED,
        _snap(1500, [["1.00", "1.0", "1"], ["1.00", "2.0", "2"], ["1.00", "1", "11"]], [["1.01", "1.0", "3"]]),
        *[_order("order_created", 100 + k, 1, "1.05", "1", 1200, 1 + k) for k in range(3)],
        _order("order_created", 11, 0, "1.00", "1", 1200, 4),
    ]
    cap = _capture(tmp_path / "w", frames)
    assert check.check_capture(cap, window=3)["verdict"] == "consistent"
    assert check.check_capture(cap, window=2)["verdict"] == "inconsistent"
    # the venue ranks same-millisecond orders by id even when the messages arrive the other way round
    frames_q = [
        SEED,
        _order("order_created", 21, 0, "1.00", "1", 2000, 1),
        _order("order_created", 20, 0, "1.00", "1", 2000, 2),
        *[_order("order_created", 200 + k, 1, "1.09", "1", 2100, 3 + k) for k in range(300)],
        _snap(
            3000,
            [["1.00", "1.0", "1"], ["1.00", "2.0", "2"], ["1.00", "1", "20"], ["1.00", "1", "21"]],
            [["1.01", "1.0", "3"]] + [["1.09", "1", str(200 + k)] for k in range(99)],
        ),
    ]
    doc = check.check_capture(_capture(tmp_path / "q", frames_q))
    assert doc["verdict"] == "consistent" and doc["streams"][0]["exact"] == 1


def test_seed_limits_ignore_orders_beyond_a_cut_seed(tmp_path: Path) -> None:
    bids = [["1.00", "1", str(1000 + k)] for k in range(50)] + [["0.99", "1", str(2000 + k)] for k in range(50)]
    asks = [["1.01", "1", "3"]]
    frames = [
        _snap(1000, bids, asks),
        # a deep order the seed could not show is deleted: unknown to the book, not a difference
        _order("order_deleted", 5555, 0, "0.90", "1", 1500, 1),
        _order("order_deleted", 1000, 0, "1.00", "1", 1600, 2),
        _snap(2000, [*bids[1:], ["0.98", "1", "3000"]], asks),
    ]
    doc = check.check_capture(_capture(tmp_path, frames))
    st = doc["streams"][0]
    assert st["seed"]["limits"] == {"buy": 990_000_000, "sell": None}
    assert doc["verdict"] == "consistent" and st["exact"] == 1 and st["counters"]["deleted_unknown"] == 1


def test_takers_are_skipped(tmp_path: Path) -> None:
    frames = [
        SEED,
        _order("order_created", 50, 0, "1.01", "0.4", 2000, 1),
        _order("order_deleted", 50, 0, "1.01", "0", 2000, 2),
        _snap(2500, [["1.00", "1.0", "1"], ["1.00", "2.0", "2"]], [["1.01", "1.0", "3"]]),
    ]
    cap = _capture(tmp_path, frames)
    st = segment_streams(cap)[(0, "tstusd")]
    assert check.taker_ids(st.orders, 100) == {50} and check.taker_ids(st.orders, None) == set()
    assert check.check_capture(cap)["streams"][0]["takers_skipped"] == 1


def test_synthetic_capture_is_consistent(synth_default) -> None:
    doc = check.check_capture(synth_default.capture)
    assert doc["verdict"] == "consistent"
    assert doc["compared"] > 100 and doc["exact"] == doc["compared"]


def test_unknown_deletes_in_the_known_region_are_lost_creates(tmp_path: Path) -> None:
    """A delete of an order the book never held, at a price the seed showed order by order, means its create was
    lost; a delete of an order whose create did arrive (the seed simply had not caught up) is not counted."""
    base = [
        _order("order_created", 30, 0, "0.99", "1", 900, 1),  # stamped before the seed, yet not in it
        SEED,
        _order("order_deleted", 30, 0, "0.99", "1", 1500, 2),
        _snap(2500, [["1.00", "1.0", "1"], ["1.00", "2.0", "2"]], [["1.01", "1.0", "3"]]),
    ]
    doc = check.check_capture(_capture(tmp_path / "ok", base))
    assert doc["verdict"] == "consistent" and doc["unknown_deletes"] == 0
    lost = [*base[:3], _order("order_deleted", 31, 1, "1.02", "1", 1600, 3), base[3]]
    doc2 = check.check_capture(_capture(tmp_path / "lost", lost))
    assert doc2["verdict"] == "inconsistent" and doc2["unknown_deletes"] == 1 and doc2["unexplained"] == 0
    assert doc2["streams"][0]["examples"][0]["order_id"] == 31


def test_partial_verdict_when_a_stream_has_no_seed(tmp_path: Path) -> None:
    other = json.loads(_order("order_created", 9, 0, "1.00", "1", 2000, 1))
    other["channel"] = "live_orders_othusd"
    frames = [SEED, _snap(1500, [["1.00", "1.0", "1"], ["1.00", "2.0", "2"]], [["1.01", "1.0", "3"]])]
    t = [0]

    def clock() -> int:
        t[0] += 1000
        return t[0]

    cfg = CaptureConfig(out=tmp_path / "cap", markets=["tstusd", "othusd"], rest_snapshot=False)
    s = CaptureSession(cfg, clock, clock)
    s.connected()
    for f in [*frames, json.dumps(other)]:
        s.handle(f)
    s.close()
    doc = check.check_capture(tmp_path / "cap")
    assert doc["verdict"] == "partial" and doc["unchecked_streams"] == [{"segment": 0, "market": "othusd"}]


def test_arrival_ties_option_measures_the_ingest_rule(tmp_path: Path) -> None:
    frames = [
        SEED,
        _order("order_created", 21, 0, "1.00", "1", 2000, 1),
        _order("order_created", 20, 0, "1.00", "1", 2000, 2),
        *[_order("order_created", 200 + k, 1, "1.09", "1", 2100, 3 + k) for k in range(300)],
        _snap(
            3000,
            [["1.00", "1.0", "1"], ["1.00", "2.0", "2"], ["1.00", "1", "20"], ["1.00", "1", "21"]],
            [["1.01", "1.0", "3"]] + [["1.09", "1", str(200 + k)] for k in range(99)],
        ),
    ]
    cap = _capture(tmp_path, frames)
    assert check.check_capture(cap)["verdict"] == "consistent"
    arrival = check.check_capture(cap, id_ties=False)
    assert arrival["verdict"] == "inconsistent" and arrival["streams"][0]["unexplained_differences"]["queue_order"] == 1


def test_check_memory_is_one_stream_at_a_time(tmp_path: Path) -> None:
    """Checking a capture of eight segments peaks at about the cost of its largest segment (measured: 1.46 times),
    not of all eight (holding every stream at once: 4.3 times)."""
    import gc
    import tracemalloc

    from livefill import synth
    from livefill.stream import load_stream, stream_index

    r = synth.build(tmp_path / "m", seed=2, steps=4000, n_own=4, reconnect_at=tuple(range(500, 4000, 500)))
    ix = stream_index(r.capture)
    assert len(ix.keys) == 8

    def peak(fn) -> int:
        gc.collect()
        tracemalloc.start()
        fn()
        _, top = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return top

    def one_stream() -> None:
        seg, m = max(ix.keys, key=lambda k: len(load_stream(r.capture, *k).orders))
        check.check_stream(load_stream(r.capture, seg, m, ix.segment_start_ns[seg]))

    whole = peak(lambda: check.check_capture(r.capture))
    single = peak(one_stream)
    assert whole < 2 * single, (whole, single)
