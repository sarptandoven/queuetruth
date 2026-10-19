"""Sanity of the O-REF-1 test-only mini-simulator on a hand-built tape (expected values computed by hand from SPEC
5.2.4/5.2.5)."""

from __future__ import annotations

from score.minisim import simulate
from score.qt_score_helpers import T0, make_dataset

B, A, T, C, M = ord("B"), ord("A"), ord("T"), ord("C"), ord("M")
PX = 100


def _tape():
    F = ord("F")
    return [
        {"ts_event": T0, "action": ord("A"), "side": B, "price": PX, "size": 5, "order_id": 1, "flags": 0},
        {"ts_event": T0, "action": ord("A"), "side": B, "price": PX, "size": 5, "order_id": 2, "flags": 128},
        {"ts_event": T0 + 10, "action": ord("A"), "side": A, "price": PX + 1, "size": 9, "order_id": 9, "flags": 128},
        {"ts_event": T0 + 15, "action": ord("A"), "side": B, "price": PX, "size": 5, "order_id": 3, "flags": 128},
        {"ts_event": T0 + 20, "action": C, "side": B, "price": PX, "size": 5, "order_id": 1, "flags": 128},
        {"ts_event": T0 + 30, "action": T, "side": A, "price": PX, "size": 4, "order_id": 0, "flags": 0},
        {"ts_event": T0 + 30, "action": F, "side": B, "price": PX, "size": 4, "order_id": 2, "flags": 0},
        {"ts_event": T0 + 30, "action": C, "side": B, "price": PX, "size": 4, "order_id": 2, "flags": 0},
        {"ts_event": T0 + 30, "action": T, "side": A, "price": PX, "size": 3, "order_id": 0, "flags": 0},
        {"ts_event": T0 + 30, "action": F, "side": B, "price": PX, "size": 1, "order_id": 2, "flags": 0},
        {"ts_event": T0 + 30, "action": C, "side": B, "price": PX, "size": 1, "order_id": 2, "flags": 0},
        {"ts_event": T0 + 30, "action": F, "side": B, "price": PX, "size": 2, "order_id": 3, "flags": 0},
        {"ts_event": T0 + 30, "action": C, "side": B, "price": PX, "size": 2, "order_id": 3, "flags": 128},
    ]


SCHED = [
    {
        "shadow_id": "s",
        "instrument_id": 1,
        "side": B,
        "price": PX,
        "qty": 3,
        "ts_submit": T0 + 5,
        "latency_ns": 0,
        "ts_cancel": None,
    }
]


def test_minisim_clean_and_bugs(tmp_path):
    ds = make_dataset(tmp_path / "ds", _tape())
    clean = simulate(ds, SCHED).to_pydict()
    assert clean["fill_qty"] == [2] and clean["ts_event"] == [T0 + 30]  # ahead 10 -> 5 by cancel, 7 executed
    assert simulate(ds, SCHED, {"no_cap_on_shrink"}).num_rows == 0  # ahead stays 10 > 7 executed
    assert simulate(ds, SCHED, {"one_trade_per_event"}).num_rows == 0  # only the first trade's 4 counted
    assert simulate(ds, SCHED, {"print_driven_queue"}).to_pydict()["fill_qty"] == [2]  # prints match executions here
    assert simulate(ds, SCHED, {"unrelated_delete_to_front"}).to_pydict()["fill_qty"] == [2]  # deleted order was ahead
