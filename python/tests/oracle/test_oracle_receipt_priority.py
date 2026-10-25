"""Receipt-order priority in the oracle's fork (docs/design/extensions/oracle-disagreements.md, O1 and D1).

On Nasdaq data from TotalView-ITCH the order ids are ITCH order references, increasing in receipt order within an
instrument, and orders the venue held until the open keep their receipt priority: they are released by `A` records
carrying their original, smaller references and execute ahead of orders displayed earlier but received later. Before
this fix the fork kept each level in arrival order, matched taker volume to the wrong orders and kept the released
ones as phantom liquidity. The `real_*` tests replay reduced real sequences (2019-12-30 / 2020-01-30 ITCH, real
timestamps, references, prices and sizes).
"""

from __future__ import annotations

import pytest
from qt_oracle.engine import FOrder, ForkBook, OracleConfig, PartitionRunner, ShadowState
from qt_oracle.scenarios import build_tape, shadow
from qt_oracle.tape import InstrumentMeta, TapeBook, group_events
from qt_oracle.venue import venue_facts, venue_key

MID_D1 = 1_577_682_000_000_000_000
TICK = 10_000_000


def meta(source: str, venue: str = "nasdaq", instrument_id: int = 13) -> InstrumentMeta:
    return InstrumentMeta(instrument_id, "2019-12-30", venue, TICK, MID_D1, 34_200, 57_600, source)


def run(m: InstrumentMeta, events, shadows):
    recs = build_tape(events, instrument_id=m.instrument_id, seq_per_event=True)
    facts = venue_facts(m.venue, open_s=m.open_s, close_s=m.close_s, cross_window_s=0)
    runner = PartitionRunner(m, shadows, {}, OracleConfig(), facts)
    return runner.run(recs), runner


def fills(res: dict) -> list[tuple[int, int, str]]:
    return [(f["ts_event"], f["fill_qty"], f["reason"]) for f in res["fills"]]


def ahead(res: dict) -> list[tuple[int, int]]:
    return [(t["ts_event"], t["queue_ahead_after"]) for t in res["trajectory"] if t["kind"] in ("live", "event")]


@pytest.mark.parametrize(
    ("venue", "source", "on"),
    [
        ("nasdaq", "itch", True),
        ("XNAS.ITCH", "dbn", True),
        ("itch", "etcsv", True),
        ("nasdaq", "synthetic", False),
        ("nasdaq", "", False),
        ("generic", "itch", False),
        ("cme", "dbn", False),
    ],
)
def test_order_ref_priority_needs_nasdaq_and_an_itch_source(venue: str, source: str, on: bool) -> None:
    assert meta(source, venue).order_ref_priority is on
    assert venue_key("XNAS.ITCH") == "nasdaq" and venue_key("Unknown") == "unknown"


# 2019-12-30 AAPL, mirror shadow r4768961: bid 289.21 x 100 behind order 4768961 (live 08:32:31). Order 3957993
# (received before it, held until the open) is released at 09:30:02.846 and executed first.
P1, P0 = 289_210_000_000, 289_200_000_000
R4768961 = [
    (1_577_712_700_000_000_000, [f"A 4742249 B {P0} 2"]),
    (1_577_712_749_945_170_290, [f"A 4768961 B {P1} 100"]),
    (1_577_712_751_801_629_738, ["A 4769813 B 289140000000 100"]),
    (1_577_716_202_846_219_230, [f"A 3957993 B {P1} 2"]),
    (1_577_716_213_864_334_161, [f"T A {P1} 2", "F 3957993 2", "C 3957993 2"]),
    (1_577_716_213_864_887_599, [f"T A {P1} 100", "F 4768961 100", "C 4768961 100"]),
    (1_577_716_213_868_560_963, [f"T A {P0} 2", "F 4742249 2", "C 4742249 2"]),
]


def test_real_r4768961_released_order_ranks_ahead_of_the_shadow() -> None:
    sh = shadow("r4768961", "B", P1, qty=100, ts_submit=1_577_712_749_945_170_290)
    (res,), _ = run(meta("itch"), R4768961, [sh])
    assert fills(res) == [(1_577_716_213_868_560_963, 2, "trade_through")]
    assert ahead(res) == [
        (1_577_712_749_945_170_290, 100),
        (1_577_716_202_846_219_230, 102),
        (1_577_716_213_864_334_161, 100),
        (1_577_716_213_864_887_599, 0),
        (1_577_716_213_868_560_963, 0),
    ]
    assert not [n for n in res["notes"] if n["category"] == "oracle_divergence"]


def test_arrival_order_is_kept_off_itch_data() -> None:
    """The same tape from a source with freely assigned ids: the released order is at the tail, the fork gives the
    first 2 executed shares to 4768961 and diverges from the tape (the pre-fix behaviour, now only off ITCH)."""
    sh = shadow("x", "B", P1, qty=100, ts_submit=1_577_712_749_945_170_290)
    (res,), _ = run(meta("synthetic"), R4768961, [sh])
    assert fills(res)[0] == (1_577_716_213_864_887_599, 2, "queue_depleted")
    assert any(n["detail"] == "orders_ahead_differ_from_tape" for n in res["notes"])


# 2019-12-30 AAPL, mirror shadow r143222381: ask 292.15 x 500 behind order 143222381 (live 12:30:25). Ahead of it:
# 5610853 (displayed before the open) and 4162377, 4667329 (older references, released at the open). Nasdaq
# executes 4162377 at 13:38:11.394, then 4667329 and 5610853 at 13:38:11.513: reference order, not arrival order.
P2 = 292_150_000_000
R143222381 = [
    (1_577_716_190_000_000_000, [f"A 5610853 A {P2} 5"]),
    (1_577_716_201_000_000_000, [f"A 4162377 A {P2} 55"]),
    (1_577_716_202_000_000_000, [f"A 4667329 A {P2} 1"]),
    (1_577_726_000_000_000_000, [f"A 138106201 A {P2} 2"]),
    (1_577_727_025_261_104_879, [f"A 143222381 A {P2} 500"]),
    (1_577_727_025_371_395_377, ["A 143223000 A 292200000000 100"]),
    (1_577_727_631_503_465_825, ["C 138106201 2"]),
    (1_577_731_091_394_365_704, [f"T B {P2} 42", "F 4162377 42", "C 4162377 42"]),
    (1_577_731_091_464_513_813, ["C 4162377 13"]),
    (1_577_731_091_513_052_645, [f"T B {P2} 1", "F 4667329 1", "C 4667329 1"]),
    (1_577_731_091_513_052_645, [f"T B {P2} 5", "F 5610853 5", "C 5610853 5"]),
    (1_577_731_091_513_052_645, [f"T B {P2} 194", "F 143222381 194", "C 143222381 194"]),
]


def test_real_r143222381_fork_follows_reference_order() -> None:
    sh = shadow("r143222381", "A", P2, qty=500, ts_submit=1_577_727_025_261_104_879)
    (res,), _ = run(meta("itch"), R143222381, [sh])
    assert res["queue_ahead_at_live"] == 563 and res["rank_at_live"] == 5
    t = 1_577_731_091_513_052_645
    assert ahead(res)[1:] == [
        (1_577_727_631_503_465_825, 561),
        (1_577_731_091_394_365_704, 519),
        (1_577_731_091_464_513_813, 506),
        (t, 505),
        (t, 500),
        (t, 306),
    ]
    assert res["filled_qty"] == 0
    assert not [n for n in res["notes"] if n["category"] == "oracle_divergence"]
    # Arrival order matches the 42 to 5610853 (5) and 4162377 (37): the fork no longer follows the tape.
    (old,), _ = run(meta("synthetic"), R143222381, [sh])
    assert any(n["detail"] == "orders_ahead_differ_from_tape" for n in old["notes"])


def test_arrival_order_leaves_phantom_liquidity_that_takes_the_shadows_fills() -> None:
    """The mechanism behind r15271952 (2020-01-30 TSLA): a level above the shadow holds o5 (displayed first) and o3
    (released later, older reference). The tape executes o3 first, then o5 is cancelled. In arrival order the fork
    matched o5, so o3 stayed as a phantom bid; an ask that the tape shows resting at the shadow's price then traded
    with the phantom instead of the shadow."""
    events = [
        (10, ["A 1 B 99 5", "A 5 B 101 10", "A 9 A 105 5"]),
        (20, ["A 3 B 101 10"]),
        (30, ["T A 101 10", "F 3 10", "C 3 10"]),
        (40, ["C 5 10"]),
        (50, ["A 11 A 100 10"]),
    ]
    sh = shadow("x", "B", 100, qty=10, ts_submit=15)
    m = InstrumentMeta(1, "2019-12-30", "nasdaq", 1, 0, 34_200, 57_600, "itch")
    (res,), _ = run(m, events, [sh])
    assert fills(res) == [(50, 10, "crossed")]
    m_old = InstrumentMeta(1, "2019-12-30", "nasdaq", 1, 0, 34_200, 57_600, "synthetic")
    (old,), _ = run(m_old, events, [sh])
    assert fills(old) == []


def _fork(ref_priority: bool) -> tuple[ForkBook, TapeBook]:
    tape = TapeBook(venue_facts("nasdaq"), ref_priority)
    for oid, px in ((7, 100), (5, 100), (6, 99)):
        tape.add(oid, "B", px, 10)
    state = ShadowState(shadow("x", "B", 100, qty=1, ts_submit=0), 0)
    return ForkBook(tape, state, venue_facts("nasdaq"), OracleConfig()), tape


def test_fork_levels_by_reference_and_tail_for_re_adds() -> None:
    f, tape = _fork(True)
    assert list(tape.levels["B"][100]) == [5, 7]  # the tape ranks 5 (arrived second) ahead of 7
    assert list(f.levels["B"][100][0]) == [5, 7]  # and the fork copies its keys
    f._place(4, "B", 100, 3, False, tape)  # an older reference released later: ahead of 5 and 7
    assert list(f.levels["B"][100][0]) == [4, 5, 7]
    f._place(5, "B", 100, 2, False, tape)  # a duplicate add of 5: received now, after every reference so far
    assert list(f.levels["B"][100][0]) == [4, 7, 5]
    f.activate(TapeBook(venue_facts("nasdaq")), meta("itch"), 1)  # the shadow keys after reference 7, after 5
    f._place(3, "B", 100, 1, False, tape)  # reference 3 < max_ref 7: received before the shadow
    f._place(8, "B", 100, 1, False, tape)  # reference 8: after it
    assert [o.oid for o in f.levels["B"][100][0].values()] == [3, 4, 7, 5, -1, 8]
    assert f.queue_ahead() == (3 + 1 + 10 + 2, 4)


def test_fork_levels_in_arrival_order_without_receipt_priority() -> None:
    f, tape = _fork(False)
    assert list(tape.levels["B"][100]) == [7, 5]
    assert list(f.levels["B"][100][0]) == [7, 5]
    f._place(4, "B", 100, 3, False, tape)
    assert list(f.levels["B"][100][0]) == [7, 5, 4]
    o = FOrder(1, "B", 100, 1, stamp=42)
    assert o.prio == 42 and FOrder(1, "B", 100, 1, stamp=42, prio=3).prio == 3


def test_max_ref_tracks_adds_only() -> None:
    m = meta("itch")
    recs = build_tape(
        [(10, ["A 40 B 100 1"]), (20, ["A 7 B 100 1"]), (30, ["C 40 1"]), (40, ["T B 100 5"])],
        instrument_id=13,
        seq_per_event=True,
    )
    runner = PartitionRunner(m, [], {}, OracleConfig(), venue_facts("nasdaq"))
    seen = []
    for ev in group_events(recs):
        runner.push_event(ev)
        seen.append(runner.tape.max_ref)
    assert seen == [40, 40, 40, 40]
    assert list(runner.tape.levels["B"][100]) == [7]


def test_tape_re_entry_keys_after_every_reference_so_far() -> None:
    tape = TapeBook(venue_facts("generic"), ref_priority=True)
    tape.add(10, "B", 100, 1)
    tape.add(3, "B", 100, 1)
    tape.modify(10, "B", 100, 5, keeps=False)  # loses priority: re-enters after reference 10
    tape.add(9, "B", 100, 1)  # an older reference than 10 still ranks ahead of the re-entered 10
    assert list(tape.levels["B"][100]) == [3, 9, 10]
    tape.modify(11, "B", 100, 2, keeps=False)  # unknown id: a new order with reference 11
    tape.add(12, "B", 100, 1)
    assert list(tape.levels["B"][100]) == [3, 9, 10, 11, 12] and tape.max_ref == 12


def run_twin(m: InstrumentMeta, events, shadows, own: set[int]):
    recs = build_tape(events, instrument_id=m.instrument_id, seq_per_event=True)
    facts = venue_facts(m.venue, open_s=m.open_s, close_s=m.close_s, cross_window_s=0)
    runner = PartitionRunner(m, shadows, {}, OracleConfig(exclude_order_ids=frozenset(own)), facts)
    return runner.run(recs)


def test_twin_released_from_the_hold_ranks_at_its_own_reference() -> None:
    """A twin-linked shadow (ruling A1) whose twin is a released held order (reference 300 below the 500 already
    added) keys at the twin's place: the resting 400 is behind it, a later 350 behind, a later 250 ahead (the Rust
    twin test of crates/replay/tests/oracle_disagreements.rs, same tape)."""
    events = [
        (1_000, [f"A 100 B {P1} 10"]),
        (1_100, [f"A 400 B {P1} 5"]),
        (1_200, ["A 500 A 289300000000 1"]),
        (3_000, [f"A 300 B {P1} 4"]),
        (4_000, [f"A 350 B {P1} 7"]),
        (5_000, [f"A 250 B {P1} 2"]),
    ]
    sh = shadow("x", "B", P1, qty=4, ts_submit=3_000)
    (res,) = run_twin(meta("itch"), events, [sh], {300})
    assert res["queue_ahead_at_live"] == 10
    assert ahead(res) == [(3_000, 10), (5_000, 12)]
    (res,) = run_twin(meta("synthetic"), events, [sh], {300})
    assert res["queue_ahead_at_live"] == 15


def test_twin_bound_covers_references_received_before_it() -> None:
    events = [
        (1_000, [f"A 100 B {P1} 10"]),
        (1_200, ["A 200 A 289300000000 1"]),
        (3_000, [f"A 600 B {P1} 4"]),
        (4_000, [f"A 400 B {P1} 3"]),
        (5_000, [f"A 700 B {P1} 2"]),
    ]
    sh = shadow("x", "B", P1, qty=4, ts_submit=3_000)
    (res,) = run_twin(meta("itch"), events, [sh], {600})
    assert ahead(res) == [(3_000, 10), (4_000, 13)]
