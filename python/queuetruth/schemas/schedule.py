"""Order schedules: the one builder of shadow schedule tables (SPEC 3.4, 5.7.2).

A schedule is what an order did, as instructions at given times: each leg is placed at `ts_submit` (with
`latency_ns`), may be reduced in size with priority kept (`qt.shadow_amend`: `new_qty` at `ts_send`) and is
cancelled at `ts_cancel` (null: it rests until it fills or the data ends). A live order whose price or size changed
in a way that loses priority is several legs: the first leg's `shadow_id` is the `order_key`, later legs are
`<order_key>.leg<k>`, and `order_key` is set on every leg.

`ScheduleLeg` is the validated `qt.shadow_spec` row and `ScheduleAmend` the `qt.shadow_amend` row: every value has
the table's type and range, `side` is the byte `B` (66) or `A` (65), `price_mode` and `source` are values of the
table's dictionaries, and an `abs` leg has a price. `schedule_table` and `amend_table` build the tables (the Arrow
schemas of `queuetruth.schemas.arrow`); `schedule_rows` and `amend_rows` read them back as validated row dicts.

Who builds schedules here: the live log (`queuetruth.live.build_schedule`), the sandbox (its episode's orders), the
scorer (its schedule file and the per-leg runs of live calibration) and the replay export (`read_schedule`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any

import pyarrow as pa

from queuetruth.errors import QtError
from queuetruth.schemas.arrow import DICT_VALUES, SCHEMAS

SPEC = "qt.shadow_spec"
AMEND = "qt.shadow_amend"
BID, ASK = ord("B"), ord("A")
_SIDES: dict[Any, int] = {
    BID: BID,
    ASK: ASK,
    "B": BID,
    "b": BID,
    "buy": BID,
    "bid": BID,
    "A": ASK,
    "a": ASK,
    "S": ASK,
    "sell": ASK,
    "ask": ASK,
}
_U32 = 2**32 - 1
_U64 = 2**64 - 1
_I32 = 2**31
_I64 = 2**63


def _bad(where: str, msg: str) -> QtError:
    return QtError("QT-INPUT-002", f"{where}: {msg}")


def _int(v: Any, where: str, name: str, lo: int, hi: int, *, null: bool = False) -> int | None:
    if v is None:
        if null:
            return None
        raise _bad(where, f"{name} is required")
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            iv = int(v)
        except (TypeError, ValueError):
            raise _bad(where, f"{name} must be an integer, got {v!r}") from None
        if iv != v:
            raise _bad(where, f"{name} must be an integer, got {v!r}")
        v = iv
    if not lo <= v <= hi:
        raise _bad(where, f"{name} {v} is out of range [{lo}, {hi}]")
    return v


def _text(v: Any, where: str, name: str, *, null: bool = False) -> str | None:
    if v is None:
        if null:
            return None
        raise _bad(where, f"{name} is required")
    if not isinstance(v, str) or (not null and not v):
        raise _bad(where, f"{name} must be a non-empty string, got {v!r}")
    return v


def _choice(v: Any, where: str, table: str, name: str) -> str:
    allowed = DICT_VALUES[table][name]
    if v not in allowed:
        raise _bad(where, f"{name} {v!r} is not one of {list(allowed)}")
    return str(v)


@dataclass(frozen=True)
class ScheduleLeg:
    """One `qt.shadow_spec` row, validated (module docstring). Times are integer ns since the epoch, prices and
    quantities integers in the instrument's units."""

    shadow_id: str
    instrument_id: int
    side: int
    qty: int
    ts_submit: int
    price_mode: str = "abs"
    price: int | None = None
    offset_ticks: int = 0
    latency_ns: int = 0
    ts_cancel: int | None = None
    group: str | None = None
    source: str = "user"
    order_key: str | None = None

    def __post_init__(self) -> None:
        sid = _text(self.shadow_id, "schedule leg", "shadow_id")
        where = f"schedule leg {sid}"
        side = _SIDES.get(self.side) if isinstance(self.side, (int, str)) else None
        if side is None:
            raise _bad(where, f"side {self.side!r} is not B or A")
        set_ = object.__setattr__
        set_(self, "side", side)
        set_(self, "instrument_id", _int(self.instrument_id, where, "instrument_id", 0, _U32))
        set_(self, "qty", _int(self.qty, where, "qty", 0, _U64))
        set_(self, "ts_submit", _int(self.ts_submit, where, "ts_submit", -_I64, _I64 - 1))
        set_(self, "price_mode", _choice(self.price_mode, where, SPEC, "price_mode"))
        set_(self, "price", _int(self.price, where, "price", -_I64, _I64 - 1, null=self.price_mode != "abs"))
        set_(self, "offset_ticks", _int(self.offset_ticks, where, "offset_ticks", -_I32, _I32 - 1))
        set_(self, "latency_ns", _int(self.latency_ns, where, "latency_ns", -_I64, _I64 - 1))
        set_(self, "ts_cancel", _int(self.ts_cancel, where, "ts_cancel", -_I64, _I64 - 1, null=True))
        set_(self, "group", _text(self.group, where, "group", null=True))
        set_(self, "source", _choice(self.source, where, SPEC, "source"))
        set_(self, "order_key", _text(self.order_key, where, "order_key", null=True))

    @classmethod
    def of(cls, row: Mapping[str, Any] | ScheduleLeg) -> ScheduleLeg:
        """A leg from a row dict: the table's columns, with `side` also as text (`B`/`A`, `buy`/`sell`); missing
        optional columns take their defaults, and a missing `price_mode` is `abs` when there is a price, else
        `touch`. Raises `QT-INPUT-002` naming the leg."""
        if isinstance(row, ScheduleLeg):
            return row
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in row.items() if k in known}
        for k in ("offset_ticks", "latency_ns"):
            if kw.get(k) is None:
                kw.pop(k, None)
        if kw.get("source") is None:
            kw.pop("source", None)
        if kw.get("price_mode") is None:
            kw["price_mode"] = "abs" if kw.get("price") is not None else "touch"
        missing = [k for k in ("shadow_id", "instrument_id", "side", "qty", "ts_submit") if k not in kw]
        if missing:
            raise _bad(f"schedule leg {row.get('shadow_id')}", f"missing {missing}")
        return cls(**kw)

    def row(self) -> dict[str, Any]:
        """The `qt.shadow_spec` row as a dict (columns in table order)."""
        return {f.name: getattr(self, f.name) for f in SCHEMAS[SPEC]}


@dataclass(frozen=True)
class ScheduleAmend:
    """One `qt.shadow_amend` row, validated: the leg `shadow_id` is reduced to `new_qty` at `ts_send`, keeping its
    queue priority."""

    shadow_id: str
    ts_send: int
    new_qty: int

    def __post_init__(self) -> None:
        sid = _text(self.shadow_id, "schedule amend", "shadow_id")
        where = f"schedule amend of {sid}"
        object.__setattr__(self, "ts_send", _int(self.ts_send, where, "ts_send", -_I64, _I64 - 1))
        object.__setattr__(self, "new_qty", _int(self.new_qty, where, "new_qty", 0, _U64))

    @classmethod
    def of(cls, row: Mapping[str, Any] | ScheduleAmend) -> ScheduleAmend:
        """An amend from a row dict (`shadow_id`, `ts_send`, `new_qty`); raises `QT-INPUT-002`."""
        if isinstance(row, ScheduleAmend):
            return row
        missing = [k for k in ("shadow_id", "ts_send", "new_qty") if k not in row]
        if missing:
            raise _bad(f"schedule amend of {row.get('shadow_id')}", f"missing {missing}")
        return cls(row["shadow_id"], row["ts_send"], row["new_qty"])

    def row(self) -> dict[str, Any]:
        """The `qt.shadow_amend` row as a dict."""
        return {"shadow_id": self.shadow_id, "ts_send": self.ts_send, "new_qty": self.new_qty}


def _build(name: str, rows: list[dict[str, Any]]) -> pa.Table:
    schema = SCHEMAS[name]
    arrays = []
    for f in schema:
        vals = [r[f.name] for r in rows]
        if pa.types.is_dictionary(f.type):
            allowed = DICT_VALUES[name][f.name]
            index = {v: i for i, v in enumerate(allowed)}
            arrays.append(
                pa.DictionaryArray.from_arrays(
                    pa.array([None if v is None else index[v] for v in vals], type=pa.int8()),
                    pa.array(list(allowed), type=pa.utf8()),
                )
            )
        elif pa.types.is_timestamp(f.type):
            arrays.append(pa.array(vals, type=pa.int64()).cast(f.type))
        else:
            arrays.append(pa.array(vals, type=f.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def schedule_table(legs: Iterable[ScheduleLeg | Mapping[str, Any]]) -> pa.Table:
    """The `qt.shadow_spec` table of `legs` (validated rows or row dicts, in the given order)."""
    return _build(SPEC, [ScheduleLeg.of(r).row() for r in legs])


def amend_table(amends: Iterable[ScheduleAmend | Mapping[str, Any]]) -> pa.Table:
    """The `qt.shadow_amend` table of `amends` (validated rows or row dicts, in the given order)."""
    return _build(AMEND, [ScheduleAmend.of(r).row() for r in amends])


def _plain_rows(table: pa.Table) -> list[dict[str, Any]]:
    cols = {}
    for f in table.schema:
        col = table.column(f.name)
        if pa.types.is_timestamp(f.type):
            col = col.cast(pa.timestamp("ns", tz=f.type.tz)).cast(pa.int64())
        elif pa.types.is_dictionary(f.type):
            col = col.cast(f.type.value_type)
        cols[f.name] = col.to_pylist()
    return [{k: v[i] for k, v in cols.items()} for i in range(table.num_rows)]


def schedule_rows(src: pa.Table | Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Validated `qt.shadow_spec` row dicts of a table or of row dicts (`ScheduleLeg.of`); [] for None."""
    if src is None:
        return []
    rows = _plain_rows(src) if isinstance(src, pa.Table) else src
    return [ScheduleLeg.of(r).row() for r in rows]


def amend_rows(src: pa.Table | Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Validated `qt.shadow_amend` row dicts of a table or of row dicts; [] for None."""
    if src is None:
        return []
    rows = _plain_rows(src) if isinstance(src, pa.Table) else src
    return [ScheduleAmend.of(r).row() for r in rows]


__all__ = [
    "AMEND",
    "ASK",
    "BID",
    "SPEC",
    "ScheduleAmend",
    "ScheduleLeg",
    "amend_rows",
    "amend_table",
    "schedule_rows",
    "schedule_table",
]
