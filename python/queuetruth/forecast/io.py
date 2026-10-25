"""Input readers of the forecast referee: timestamps, record files (CSV, JSON Lines, JSON, Parquet) and the quote
file of `forecast score --market`.

Timestamps are integer ns since the Unix epoch (UTC). Inputs may give an integer (ns), a decimal string of digits
(ns) or an ISO 8601 date-time with seconds, up to 9 fractional digits and an explicit offset (`Z` or `+hh:mm`);
a naive date-time or a bare date is refused, because its instant is ambiguous. An integer outside
[`MIN_INT_NS`, int64 max] (1990-01-01 to 2262) is refused too: it is almost certainly seconds, milliseconds or
microseconds, and read as ns it would put every quote or resolution in January 1970.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from queuetruth.errors import QtError

_ISO = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?(Z|z|[+-]\d{2}:?\d{2})$",
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
#: Smallest integer timestamp accepted as ns (1990-01-01T00:00:00Z); smaller integers are another unit.
MIN_INT_NS = 631_152_000_000_000_000
_MAX_INT_NS = 2**63 - 1


def _int_ns(v: int, where: str) -> int:
    if not MIN_INT_NS <= v <= _MAX_INT_NS:
        unit = next((u for u, k in (("s", 9), ("ms", 6), ("us", 3)) if MIN_INT_NS <= v * 10**k <= _MAX_INT_NS), None)
        guess = f" (it looks like {unit} since the epoch)" if unit else ""
        raise QtError(
            "QT-INPUT-002",
            f"{where}: integer timestamp {v} is not ns since the epoch between 1990 and 2262{guess}",
            "give integer ns, or ISO 8601 with an offset such as 2026-11-03T23:59:00Z",
        )
    return v


def parse_ts(value: Any, where: str) -> int:
    """Timestamp as integer ns UTC; `where` names the field for the `QT-INPUT-002` message."""
    if isinstance(value, bool) or value is None:
        raise QtError("QT-INPUT-002", f"{where}: expected a timestamp, got {value!r}")
    if isinstance(value, (int, np.integer)):
        return _int_ns(int(value), where)
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise QtError("QT-INPUT-002", f"{where}: a float timestamp must be a whole number of ns, got {value!r}")
        return _int_ns(int(value), where)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise QtError("QT-INPUT-002", f"{where}: naive datetime {value!r} (give a time zone)")
        delta = value - _EPOCH
        return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000
    if isinstance(value, np.datetime64):
        return int(value.astype("datetime64[ns]").astype(np.int64))
    if not isinstance(value, str):
        raise QtError("QT-INPUT-002", f"{where}: expected a timestamp, got {type(value).__name__}")
    s = value.strip()
    if re.fullmatch(r"-?\d+", s):
        return _int_ns(int(s), where)
    m = _ISO.match(s)
    if m is None:
        raise QtError(
            "QT-INPUT-002",
            f"{where}: {value!r} is not ns or an ISO 8601 date-time with an offset",
            "write e.g. 2026-11-03T23:59:00Z",
        )
    date, hh, mm, ss, frac, off = m.groups()
    try:
        base = datetime.fromisoformat(f"{date}T{hh}:{mm}:{ss}").replace(tzinfo=UTC)
    except ValueError as e:
        raise QtError("QT-INPUT-002", f"{where}: {value!r}: {e}") from None
    if off not in ("Z", "z"):
        sign = 1 if off[0] == "+" else -1
        digits = off[1:].replace(":", "")
        base -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    delta = base - _EPOCH
    ns = (delta.days * 86_400 + delta.seconds) * 1_000_000_000
    if frac:
        ns += int(frac.ljust(9, "0"))
    return ns


def format_ts(ns: int) -> str:
    """ISO 8601 UTC with nanoseconds (`2026-11-03T23:59:00.000000000Z`)."""
    sec, rem = divmod(int(ns), 1_000_000_000)
    dt = _EPOCH + timedelta(seconds=sec)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{rem:09d}Z"


def _parquet_rows(path: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(path)
    except Exception as e:
        raise QtError("QT-INPUT-002", f"{path}: not a readable Parquet file: {e}") from None
    return table.to_pylist()


def _csv_value(v: str) -> Any:
    """CSV cell: empty -> None; a JSON object or array decoded; anything else kept as the stripped text (so
    identifiers such as `0001` keep their leading zeros; the readers convert numbers and timestamps themselves)."""
    t = v.strip()
    if t == "":
        return None
    if t[0] in "[{":
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            return t
    return t


def read_records(path: str | Path) -> list[dict[str, Any]]:
    """Rows of a `.csv`, `.jsonl` / `.ndjson`, `.json` (an array of objects) or `.parquet` file."""
    p = Path(path)
    if not p.is_file():
        raise QtError("QT-INPUT-001", f"{p}: no such file")
    suffix = p.suffix.lower()
    if suffix == ".parquet":
        return _parquet_rows(p)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise QtError("QT-INPUT-001", f"{p}: {e}") from None
    if suffix in (".jsonl", ".ndjson"):
        rows = []
        for i, line in enumerate(text.split("\n"), start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise QtError("QT-INPUT-002", f"{p}:{i}: invalid JSON: {e.msg}") from None
            if not isinstance(obj, dict):
                raise QtError("QT-INPUT-002", f"{p}:{i}: expected a JSON object")
            rows.append(obj)
        return rows
    if suffix == ".json":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise QtError("QT-INPUT-002", f"{p}: invalid JSON: {e.msg}") from None
        if isinstance(obj, dict):
            obj = [obj]
        if not isinstance(obj, list) or not all(isinstance(r, dict) for r in obj):
            raise QtError("QT-INPUT-002", f"{p}: expected a JSON object or an array of objects")
        return obj
    if suffix in (".csv", ".tsv"):
        delim = "\t" if suffix == ".tsv" else ","
        # newline="" keeps line breaks inside quoted fields (and U+2028, \x85 and the like) as data
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delim)
        if reader.fieldnames is None:
            return []
        return [{k: _csv_value(v or "") for k, v in row.items() if k is not None} for row in reader]
    raise QtError("QT-INPUT-002", f"{p}: unknown file type {suffix!r} (use .csv, .tsv, .jsonl, .json or .parquet)")


# ---- quotes -------------------------------------------------------------------------------------------------------

QUOTE_COLUMNS = ("question_id", "ts", "best_bid", "best_ask", "bid_depth", "ask_depth")


@dataclass(frozen=True)
class Quotes:
    """Top-of-book series of one question's YES contract (prices in [0, 1], payout 1), sorted by `ts`."""

    ts: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    bid_depth: np.ndarray
    ask_depth: np.ndarray

    def asof(self, t_ns: int) -> int | None:
        """Index of the last quote with `ts <= t_ns` (None before the first quote)."""
        i = int(np.searchsorted(self.ts, t_ns, side="right")) - 1
        return i if i >= 0 else None

    def mid(self, i: int) -> float | None:
        """Mid of quote `i` when both sides are quoted."""
        b, a = self.bid[i], self.ask[i]
        if not (np.isfinite(b) and np.isfinite(a)):
            return None
        return float((b + a) / 2.0)


def _num(row: dict[str, Any], key: str, where: str, *, allow_none: bool = True) -> float:
    v = row.get(key)
    if v is None or (isinstance(v, str) and v.strip() == ""):
        if allow_none:
            return float("nan")
        raise QtError("QT-INPUT-002", f"{where}: {key} is required")
    if isinstance(v, bool):
        raise QtError("QT-INPUT-002", f"{where}: {key} must be a number, got {v!r}")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise QtError("QT-INPUT-002", f"{where}: {key} must be a number, got {v!r}") from None
    if math.isnan(x) and not allow_none:
        raise QtError("QT-INPUT-002", f"{where}: {key} is required")
    return x


def read_quotes(path: str | Path) -> dict[str, Quotes]:
    """The market file: one row per quote update, columns `question_id, ts, best_bid, best_ask, bid_depth,
    ask_depth` (prices of the YES contract in [0, 1]; depth = contracts displayed at the touch; an empty bid or ask
    is a one-sided book). Rows are sorted by `ts` per question (stable, so the last of equal timestamps wins)."""
    rows = read_records(path)
    if rows:
        missing = [c for c in QUOTE_COLUMNS if c not in rows[0]]
        if missing:
            raise QtError("QT-INPUT-002", f"{path}: missing column(s) {', '.join(missing)}")
    by_q: dict[str, list[tuple[int, float, float, float, float]]] = {}
    for i, row in enumerate(rows, start=1):
        where = f"{path}: row {i}"
        qid = row.get("question_id")
        if qid is None or str(qid) == "":
            raise QtError("QT-INPUT-002", f"{where}: question_id is required")
        ts = parse_ts(row.get("ts"), f"{where}: ts")
        bid = _num(row, "best_bid", where)
        ask = _num(row, "best_ask", where)
        bd = _num(row, "bid_depth", where)
        ad = _num(row, "ask_depth", where)
        for name, x in (("best_bid", bid), ("best_ask", ask)):
            if np.isfinite(x) and not 0.0 <= x <= 1.0:
                raise QtError("QT-INPUT-002", f"{where}: {name} {x} is outside [0, 1]")
        for name, x in (("bid_depth", bd), ("ask_depth", ad)):
            if not math.isnan(x) and x < 0:
                raise QtError("QT-INPUT-002", f"{where}: {name} {x} is negative")
        if np.isfinite(bid) and np.isfinite(ask) and bid > ask:
            raise QtError("QT-INPUT-002", f"{where}: crossed quote (best_bid {bid} > best_ask {ask})")
        by_q.setdefault(str(qid), []).append((ts, bid, ask, bd, ad))
    out: dict[str, Quotes] = {}
    for qid, items in by_q.items():
        arr = np.array(items, dtype=float)
        ts = np.array([x[0] for x in items], dtype=np.int64)
        order = np.argsort(ts, kind="stable")
        out[qid] = Quotes(
            ts=ts[order],
            bid=arr[order, 1],
            ask=arr[order, 2],
            bid_depth=arr[order, 3],
            ask_depth=arr[order, 4],
        )
    return out


__all__ = ["MIN_INT_NS", "QUOTE_COLUMNS", "Quotes", "format_ts", "parse_ts", "read_quotes", "read_records"]
