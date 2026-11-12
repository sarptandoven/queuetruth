"""`queuetruth.schemas.schedule`: the validated schedule row types and the one table builder (the callers' outputs
are pinned in `live/test_schedule_pins.py`)."""

from __future__ import annotations

import pyarrow as pa
import pytest

from queuetruth.errors import QtError
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.schedule import (
    ScheduleAmend,
    ScheduleLeg,
    amend_rows,
    amend_table,
    schedule_rows,
    schedule_table,
)
from queuetruth.score._io import make_table

T0 = 1_800_000_000_000_000_000


def _leg(**kw: object) -> dict:
    row = {
        "shadow_id": "k1",
        "instrument_id": 7,
        "side": ord("B"),
        "price_mode": "abs",
        "price": 100,
        "offset_ticks": 0,
        "qty": 10,
        "ts_submit": T0,
        "latency_ns": 0,
        "ts_cancel": T0 + 5,
        "group": None,
        "source": "live_log",
        "order_key": "k1",
    }
    row.update(kw)
    return row


def test_tables_equal_the_generic_builder() -> None:
    legs = [
        _leg(),
        _leg(shadow_id="k1.leg2", ts_submit=T0 + 5, ts_cancel=None, price=101),
        _leg(shadow_id="t", price_mode="touch", price=None, offset_ticks=-2, source="user", order_key=None, group="g"),
    ]
    amends = [{"shadow_id": "k1", "ts_send": T0 + 2, "new_qty": 4}]
    t, a = schedule_table(legs), amend_table(amends)
    assert t.equals(make_table("qt.shadow_spec", legs), check_metadata=True)
    assert a.equals(make_table("qt.shadow_amend", amends), check_metadata=True)
    assert t.schema.equals(SCHEMAS["qt.shadow_spec"], check_metadata=True)
    assert schedule_rows(t) == [ScheduleLeg.of(r).row() for r in legs] == schedule_rows(legs)
    assert amend_rows(a) == amends and schedule_rows(None) == [] and amend_rows(None) == []
    assert schedule_table([]).num_rows == 0 and amend_table([]).num_rows == 0


def test_text_sides_defaults_and_rows_in_table_order() -> None:
    leg = ScheduleLeg.of({"shadow_id": "x", "instrument_id": 1, "side": "sell", "qty": 2, "ts_submit": 5, "price": 9})
    assert (leg.side, leg.price_mode, leg.latency_ns, leg.offset_ticks, leg.source) == (ord("A"), "abs", 0, 0, "user")
    assert list(leg.row()) == [f.name for f in SCHEMAS["qt.shadow_spec"]]
    touch = ScheduleLeg.of({"shadow_id": "y", "instrument_id": 1, "side": "B", "qty": 2, "ts_submit": 5})
    assert touch.price_mode == "touch" and touch.price is None
    assert ScheduleLeg.of(leg) is leg and ScheduleAmend.of(a := ScheduleAmend("x", 1, 0)) is a


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"side": "X"}, "side"),
        ({"side": 1}, "side"),
        ({"price": None}, "price is required"),
        ({"price_mode": "mid"}, "price_mode"),
        ({"source": "agent"}, "source"),
        ({"qty": -1}, "qty -1 is out of range"),
        ({"instrument_id": 2**32}, "instrument_id"),
        ({"ts_submit": 1.5}, "ts_submit must be an integer"),
        ({"ts_submit": None}, "ts_submit is required"),
        ({"shadow_id": ""}, "shadow_id"),
        ({"order_key": 3}, "order_key"),
    ],
)
def test_invalid_legs_are_refused(change: dict, message: str) -> None:
    with pytest.raises(QtError, match=message) as e:
        schedule_table([_leg(**change)])
    assert e.value.code == "QT-INPUT-002"


def test_missing_columns_and_bad_amends() -> None:
    with pytest.raises(QtError, match=r"schedule leg k1: missing \['qty'\]"):
        ScheduleLeg.of({k: v for k, v in _leg().items() if k != "qty"})
    with pytest.raises(QtError, match="new_qty"):
        amend_table([{"shadow_id": "k1", "ts_send": T0, "new_qty": -3}])
    with pytest.raises(QtError, match="missing"):
        amend_rows([{"shadow_id": "k1", "ts_send": T0}])


def test_numpy_and_arrow_integers_are_accepted() -> None:
    import numpy as np

    leg = ScheduleLeg.of(_leg(qty=np.uint64(3), ts_submit=np.int64(T0), instrument_id=pa.scalar(7).as_py()))
    assert (leg.qty, leg.ts_submit) == (3, T0) and type(leg.qty) is int
