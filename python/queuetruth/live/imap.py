"""Instrument map for live logs (SPEC 3.9): CSV
`venue,instrument,instrument_id,session_date(optional),price_exp,size_exp,tick_size,lot_size`.

`tick_size` and `lot_size` are decimal strings in real units and must be exact at `price_exp` / `size_exp`
(`QT-INPUT-004`). A row with a `session_date` applies to that UTC date only and wins over a dateless row for the
same (venue, instrument). Venue names compare case-insensitively (stored and looked up in lower case, E53).
Without a matching row importers use the venue defaults (`price_exp = -9`;
`size_exp = -8` for crypto venues and FIX, `0` for IBKR) and `instrument_id = null`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from queuetruth.errors import QtError
from queuetruth.live._common import date_days, dec, input_error, read_csv_rows, read_text, to_fixed
from queuetruth.schemas.io import sha256_file

REQUIRED = ("venue", "instrument", "instrument_id", "price_exp", "size_exp", "tick_size", "lot_size")


@dataclass(frozen=True)
class InstrumentEntry:
    """One instrument-map row; `tick_size` / `lot_size` in fixed-point units at the row's exponents."""

    venue: str
    instrument: str
    instrument_id: int
    session_date: int | None
    price_exp: int
    size_exp: int
    tick_size: int
    lot_size: int


class InstrumentMap:
    """Lookup of (venue, instrument, session date) -> `InstrumentEntry`."""

    def __init__(self, entries: list[InstrumentEntry], sha256: str | None = None) -> None:
        self.entries = list(entries)
        self.sha256 = sha256
        self._by: dict[tuple[str, str, int | None], InstrumentEntry] = {}
        for e in self.entries:
            key = (e.venue.lower(), e.instrument, e.session_date)
            if key in self._by:
                raise QtError("QT-INPUT-003", f"duplicate instrument map row: {key}")
            self._by[key] = e

    def lookup(self, venue: str, instrument: str, session_date: int | None) -> InstrumentEntry | None:
        """Row for the date, else the dateless row, else None."""
        v = venue.lower()
        if session_date is not None:
            hit = self._by.get((v, instrument, session_date))
            if hit is not None:
                return hit
        return self._by.get((v, instrument, None))


def _int(v: str, what: str, loc: str, lo: int, hi: int) -> int:
    try:
        x = int(v.strip())
    except ValueError as e:
        raise input_error(loc, f"{what}: {v!r} is not an integer") from e
    if not lo <= x <= hi:
        raise input_error(loc, f"{what}: {x} outside [{lo}, {hi}]")
    return x


def load_instrument_map(path: str | Path) -> InstrumentMap:
    """Reads an instrument-map CSV (SPEC 3.9)."""
    p = Path(path)
    header, rows = read_csv_rows(read_text(p))
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise input_error(str(p), f"instrument map lacks columns {missing}")
    col = {c: header.index(c) for c in header}
    entries: list[InstrumentEntry] = []
    for n, r in enumerate(rows, start=2):
        loc = f"{p}:{n}"
        if len(r) < len(header):
            r = r + [""] * (len(header) - len(r))

        def g(c: str, r: list[str] = r) -> str:
            return r[col[c]].strip() if c in col else ""

        price_exp = _int(g("price_exp"), "price_exp", loc, -12, 0)
        size_exp = _int(g("size_exp"), "size_exp", loc, -18, 18)
        tick = dec(g("tick_size"), "tick_size", loc)
        lot = dec(g("lot_size"), "lot_size", loc)
        if tick is None or lot is None or tick <= 0 or lot <= 0:
            raise input_error(loc, "tick_size and lot_size must be positive decimals")
        sd = g("session_date")
        entries.append(
            InstrumentEntry(
                venue=g("venue").lower(),
                instrument=g("instrument"),
                instrument_id=_int(g("instrument_id"), "instrument_id", loc, 0, 2**32 - 1),
                session_date=date_days(sd) if sd else None,
                price_exp=price_exp,
                size_exp=size_exp,
                tick_size=to_fixed(tick, price_exp, f"{loc} tick_size"),
                lot_size=to_fixed(lot, size_exp, f"{loc} lot_size", unsigned=True),
            )
        )
    return InstrumentMap(entries, sha256_file(p))
