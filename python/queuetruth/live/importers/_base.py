"""Shared importer plumbing: the importer signature, input-kind detection and small field mappers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from queuetruth.live._common import Record, dump_raw, input_error, read_json_records
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.imap import InstrumentMap


@dataclass
class ImportOptions:
    """Importer options: account override, instrument map and venue-specific settings."""

    account: str | None = None
    imap: InstrumentMap | None = None
    extra: dict[str, Any] = field(default_factory=dict)


Importer = Callable[[list[str], ImportOptions], ImportResult]


def json_records(paths: list[str]) -> list[Record]:
    """Records of every JSON / JSON-lines input in order (file index = position in `paths`)."""
    out: list[Record] = []
    for i, p in enumerate(paths):
        out.extend(read_json_records(Path(p), i))
    return out


def unwrap_envelope(obj: Any, *keys: str) -> list[Any]:
    """Follows the first matching envelope keys (e.g. `result` -> `list`) and returns a list of items."""
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
    if isinstance(cur, list):
        return cur
    return [cur]


def sub_records(rec: Record, *keys: str) -> list[Record]:
    """Items of an enveloped / array record as records of their own (raw = the item's JSON)."""
    items = unwrap_envelope(rec.obj, *keys)
    if len(items) == 1 and items[0] is rec.obj:
        return [rec]
    out = []
    for i, item in enumerate(items):
        out.append(
            Record(
                item,
                dump_raw(item),
                f"{rec.location}/{i}",
                rec.file_idx,
                rec.line_idx * 100_000 + i,
                recv_ns=rec.recv_ns,
                send_ns=rec.send_ns,
                direction=rec.direction,
                wrapped=rec.wrapped,
            )
        )
    return out


def _kind(v: Any) -> str:
    return {dict: "an object", list: "an array", str: "a string", bool: "a boolean"}.get(type(v), "a number")


def obj_field(v: Any, what: str, location: str) -> dict[str, Any]:
    """A nested JSON object of a record: `v`, `{}` for null, `QT-INPUT-002` for any other JSON type (an importer
    must never call `.get` on an array or a string a hostile file put where an object belongs)."""
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    raise input_error(location, f"{what} must be an object, got {_kind(v)}")


def list_field(v: Any, what: str, location: str) -> list[Any]:
    """A nested JSON array of a record: `v`, `[]` for null, `QT-INPUT-002` for any other JSON type."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    raise input_error(location, f"{what} must be an array, got {_kind(v)}")


def side_of(v: Any) -> str | None:
    """Venue side text -> `buy` / `sell`."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("buy", "b", "bid", "1", "long"):
        return "buy"
    if s in ("sell", "s", "a", "ask", "2", "5", "short"):
        return "sell"
    return None


def new_builder(venue: str, opts: ImportOptions, *, size_exp: int = -8) -> LogBuilder:
    """A `LogBuilder` with the SPEC 3.9 defaults for the venue."""
    return LogBuilder(venue, account=opts.account, imap=opts.imap, price_exp=-9, size_exp=size_exp)
