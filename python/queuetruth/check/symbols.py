"""Backtest instrument names -> the dataset's instruments, from the dataset's own symbology.

A name matches a symbol when they are equal, or equal after upper-casing and dropping a venue suffix
(`AAPL.XNAS` -> `AAPL`) and the separators `-`, `/`, `_`, `:` and spaces (`BTC/USDT` -> `BTCUSDT`). A placeholder
name (hftbacktest's `asset0` when the snapshots carry no instrument) maps to the dataset's symbol when the dataset
has exactly one. Every other name that matches no symbol, or more than one, is reported with the reason; it never
maps to a guess.

The map is written in the instrument-map CSV layout (`queuetruth.live.imap`), with the dataset's exponents, tick and
lot sizes, so the importers convert the backtest's prices and quantities into the dataset's units. A symbol whose
instrument id changes from one session date to the next gets one row per date.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from queuetruth.errors import QtError

_SEP = re.compile(r"[-/_:\s]")
_PLACEHOLDER = re.compile(r"asset\d+")
_EPOCH = dt.date(1970, 1, 1)


def norm(name: str) -> str:
    """The comparison form of an instrument name."""
    s = name.strip().upper()
    if "." in s:
        s = s.rsplit(".", 1)[0]
    return _SEP.sub("", s)


@dataclass
class Instrument:
    """One dataset symbol: its id per session date and its units."""

    symbol: str
    venue: str
    price_exp: int
    size_exp: int
    tick_size: int
    lot_size: int
    currency: str | None
    tz: str
    ids: dict[int, int] = field(default_factory=dict)  # session date (days) -> instrument_id


def dataset_symbols(dataset: str | Path) -> dict[str, Instrument]:
    """Symbol -> `Instrument` of a dataset's `instruments.parquet`."""
    p = Path(dataset) / "instruments.parquet"
    if not p.is_file():
        raise QtError(
            "QT-INPUT-001",
            f"{dataset}: no instruments.parquet, so its symbols are unknown",
            "pass --instrument-map FILE (columns venue,instrument,instrument_id,session_date,price_exp,size_exp,"
            "tick_size,lot_size)",
        )
    out: dict[str, Instrument] = {}
    for r in pq.read_table(p).to_pylist():
        sym = str(r.get("symbol") or "").strip()
        if not sym:
            continue
        d = r["session_date"]
        days = (d - _EPOCH).days if isinstance(d, dt.date) else int(d)
        inst = out.get(sym)
        if inst is None:
            inst = out[sym] = Instrument(
                symbol=sym,
                venue=str(r.get("venue") or ""),
                price_exp=int(r["price_exp"]),
                size_exp=int(r["size_exp"]),
                tick_size=int(r["tick_size"]),
                lot_size=int(r["lot_size"]),
                currency=r.get("currency"),
                tz=str(r.get("session_tz") or "UTC"),
            )
        inst.ids[days] = int(r["instrument_id"])
    return out


@dataclass
class Mapping:
    """Names that map (name -> symbol) and names that do not (name -> reason)."""

    mapped: dict[str, str]
    unmapped: dict[str, str]
    notes: list[str]


def match(names: list[str], symbols: dict[str, Instrument]) -> Mapping:
    """Maps each backtest instrument name to one dataset symbol (see the module docstring)."""
    by_norm: dict[str, list[str]] = {}
    for s in symbols:
        by_norm.setdefault(norm(s), []).append(s)
    mapped: dict[str, str] = {}
    unmapped: dict[str, str] = {}
    notes: list[str] = []
    shown = ", ".join(sorted(symbols)[:12]) + (", ..." if len(symbols) > 12 else "")
    for name in sorted(set(names)):
        if name in symbols:
            mapped[name] = name
            continue
        hits = by_norm.get(norm(name), [])
        if len(hits) == 1:
            mapped[name] = hits[0]
            notes.append(f"backtest instrument {name} is the dataset's {hits[0]}")
        elif len(hits) > 1:
            unmapped[name] = f"matches several dataset symbols ({', '.join(sorted(hits))})"
        elif _PLACEHOLDER.fullmatch(name) and len(symbols) == 1:
            only = next(iter(symbols))
            mapped[name] = only
            notes.append(f"the backtest names no instrument ({name}); the dataset has one symbol, {only}")
        elif _PLACEHOLDER.fullmatch(name):
            unmapped[name] = f"the backtest names no instrument and the dataset has {len(symbols)} symbols ({shown})"
        else:
            unmapped[name] = f"is not a symbol of the dataset (it has {shown})"
    return Mapping(mapped, unmapped, notes)


def _dec(v: int, exp: int) -> str:
    return format(Decimal(v).scaleb(exp).normalize(), "f")


def write_map(path: Path, venue: str, mapped: dict[str, str], symbols: dict[str, Instrument]) -> Path:
    """Writes the instrument-map CSV of `mapped` (backtest name -> symbol) for importer venue `venue`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["venue", "instrument", "instrument_id", "session_date", "price_exp", "size_exp", "tick_size",
                    "lot_size"])  # fmt: skip
        for name, sym in sorted(mapped.items()):
            i = symbols[sym]
            units = [i.price_exp, i.size_exp, _dec(i.tick_size, i.price_exp), _dec(i.lot_size, i.size_exp)]
            if len(set(i.ids.values())) == 1:
                w.writerow([venue, name, next(iter(i.ids.values())), "", *units])
            else:
                for days, iid in sorted(i.ids.items()):
                    w.writerow([venue, name, iid, (_EPOCH + dt.timedelta(days=days)).isoformat(), *units])
    return path


def unmapped_error(m: Mapping, n_orders: dict[str, int]) -> QtError:
    """`QT-INPUT-002` naming every backtest instrument that maps to no dataset symbol, and why."""
    parts = [f"{name} ({n_orders.get(name, 0)} orders) {why}" for name, why in sorted(m.unmapped.items())]
    return QtError(
        "QT-INPUT-002",
        "check: no backtest instrument maps to a dataset symbol: " + "; ".join(parts),
        "pass --instrument-map FILE (columns venue,instrument,instrument_id,session_date,price_exp,size_exp,"
        "tick_size,lot_size; venue is the dataset's venue), or check against a dataset that holds these symbols",
    )


def order_counts(orders: list[dict[str, Any]]) -> dict[str, int]:
    """Orders per backtest instrument name."""
    out: dict[str, int] = {}
    for o in orders:
        out[str(o.get("instrument"))] = out.get(str(o.get("instrument")), 0) + 1
    return out
