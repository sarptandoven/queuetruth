"""The instrument table of a dataset (`instruments.parquet`, `qt.instruments`): one row per instrument and session
date, with the units (`price_exp`, `size_exp`, `tick_size`) and the session times."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from queuetruth.dataset._manifest import MAX_DAY, MIN_DAY, PRICE_EXP_MAX, PRICE_EXP_MIN, bad, date_str, day_number

INSTRUMENTS_FILE = "instruments.parquet"
NS = 1_000_000_000
DAY_NS = 86_400 * NS

#: Instrument columns readers use, with the type each must cast to (SPEC 3.3 `qt.instruments`).
INSTRUMENT_TYPES = {
    "instrument_id": pa.uint32(),
    "session_date": pa.date32(),
    "price_exp": pa.int8(),
    "tick_size": pa.int64(),
    "lot_size": pa.uint64(),
    "size_exp": pa.int8(),
    "midnight_utc_ns": pa.int64(),
    "open_s": pa.uint32(),
    "close_s": pa.uint32(),
}


def instrument_rows(t: pa.Table, where: str | Path) -> list[dict[str, Any]]:
    """Rows of a `qt.instruments` table with the known columns cast to their types and `session_date` as a day
    number (`QT-INPUT-002` naming the column when a value does not fit)."""
    for c in ("instrument_id", "session_date"):
        if c not in t.column_names:
            raise bad(str(where), f"no {c} column")
    cols = {}
    for name in t.column_names:
        col = t.column(name)
        want = INSTRUMENT_TYPES.get(name)
        if want is not None:
            try:
                col = col.cast(want, safe=True)
            except (pa.ArrowException, ValueError, TypeError) as e:
                raise bad(f"{where}: {name}", f"not castable to {want}: {e}") from None
            if name in ("instrument_id", "session_date") and col.null_count:
                raise bad(f"{where}: {name}", "null values")
        cols[name] = col
    for name, lo, hi in (("session_date", MIN_DAY, MAX_DAY), ("price_exp", PRICE_EXP_MIN, PRICE_EXP_MAX)):
        if name in cols and len(cols[name]):
            mm = pc.min_max(cols[name].cast(pa.int32()))
            lo_v, hi_v = mm["min"].as_py(), mm["max"].as_py()
            if lo_v is not None and (lo_v < lo or hi_v > hi):
                raise bad(f"{where}: {name}", f"value {lo_v if lo_v < lo else hi_v} outside [{lo}, {hi}]")
    try:
        rows = pa.table(cols).to_pylist()
    except (pa.ArrowException, ValueError, ArithmeticError) as e:  # a date or time outside Python's range
        raise bad(where, f"unreadable: {e}") from None
    for row in rows:
        row["session_date"] = day_number(row["session_date"])
    return rows


@dataclass(frozen=True)
class Instrument:
    """An instrument's identity and units on one session date. Prices are integers scaled by `10**price_exp`, sizes
    by `10**size_exp`; `tick_size` is in price units."""

    instrument_id: int
    symbol: str | None
    price_exp: int | None
    size_exp: int | None
    tick_size: int | None
    lot_size: int | None = None
    currency: str | None = None
    #: IANA zone of the venue's session (`America/New_York`), for local times.
    session_tz: str | None = None
    #: The full `qt.instruments` row (`session_date` as a day number).
    row: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def label(self) -> str:
        """The symbol, else `#<instrument_id>`."""
        return str(self.symbol) if self.symbol else f"#{self.instrument_id}"

    def units(self) -> dict[str, Any]:
        """`{symbol, price_exp, size_exp, tick_size, session_tz}`: what turns the instrument's integers into human
        units (`queuetruth.units`)."""
        return {
            "symbol": self.symbol,
            "price_exp": self.price_exp,
            "size_exp": self.size_exp,
            "tick_size": self.tick_size,
            "session_tz": self.session_tz,
        }

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> Instrument:
        def opt(k: str) -> Any:
            v = r.get(k)
            return None if v is None else int(v)

        sym = r.get("symbol")
        cur = r.get("currency")
        tz = r.get("session_tz")
        return cls(
            int(r["instrument_id"]),
            None if sym is None else str(sym),
            opt("price_exp"),
            opt("size_exp"),
            opt("tick_size"),
            opt("lot_size"),
            None if cur is None else str(cur),
            None if tz is None else str(tz),
            row=r,
        )


@dataclass(frozen=True)
class Session:
    """One instrument-day: the instrument's units that day and the session times. `midnight_utc_ns` is the local
    midnight in UTC ns; `open_s` and `close_s` are seconds after it (continuous trading)."""

    instrument: Instrument
    session_date: int
    midnight_utc_ns: int | None
    open_s: int | None
    close_s: int | None

    @property
    def instrument_id(self) -> int:
        return self.instrument.instrument_id

    @property
    def date(self) -> str:
        """The session date, `YYYY-MM-DD`."""
        return date_str(self.session_date)

    @property
    def key(self) -> tuple[int, int]:
        """`(instrument_id, session_date)`."""
        return (self.instrument_id, self.session_date)

    @property
    def open_ns(self) -> int | None:
        """Start of continuous trading, UTC ns."""
        if self.midnight_utc_ns is None or self.open_s is None:
            return None
        return self.midnight_utc_ns + self.open_s * NS

    @property
    def close_ns(self) -> int | None:
        """End of continuous trading, UTC ns."""
        if self.midnight_utc_ns is None or self.close_s is None:
            return None
        return self.midnight_utc_ns + self.close_s * NS

    def contains(self, ts: int) -> bool:
        """Whether `ts` falls on this session's local day."""
        return self.midnight_utc_ns is not None and self.midnight_utc_ns <= ts < self.midnight_utc_ns + DAY_NS

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> Session:
        def opt(k: str) -> int | None:
            v = r.get(k)
            return None if v is None else int(v)

        return cls(
            Instrument.from_row(r), int(r["session_date"]), opt("midnight_utc_ns"), opt("open_s"), opt("close_s")
        )
