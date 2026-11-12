"""Which backtester wrote a file: read from the file's own columns or keys, never from its name alone.

Backtesters and what marks their output (the importers in `queuetruth.live.importers` hold the full formats):

- `hftbacktest`: order snapshots with `price_tick`, `tick_size` and `leaves_qty`.
- `nautilus`: report or event rows with `client_order_id`, `ts_init`, `last_qty` or `ts_event`; or an order event
  file named after the event (`OrderFilled.csv`).
- `backtrader`: `QtOrderLog` lines (`"event": "order"` or `"trade"`), or a `Transactions` analyzer dump (an object
  of bar times to lists).
- `vectorbt`: `records_readable` columns (`Order Id` and `Column`, or a trade id column).
- `lean`: a result JSON with an `orders` member, or order events (`orderEventId`, `algorithmId`, `OrderId`).
- `freqtrade`: a backtest result (`.zip`, or JSON with `strategy` and its trades) or a trades database.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError

#: Backtesters `check` reads, with the importer kind of each (`queuetruth.live.import_sim`).
BACKTESTERS: tuple[str, ...] = ("hftbacktest", "nautilus", "backtrader", "vectorbt", "lean", "freqtrade")
_HEAD = 256 * 1024
_NT_EVENTS = ("orderinitialized", "ordersubmitted", "orderaccepted", "orderrejected", "ordercanceled")
_NT_EVENTS += ("orderexpired", "orderupdated", "orderfilled", "orderdenied")


def _keys_of_json(text: str) -> tuple[set[str], Any]:
    """Keys of the first JSON object of `text` (a JSON document or JSON lines) and the parsed document if any."""
    s = text.lstrip()
    doc: Any = None
    try:
        doc = json.loads(s)
    except ValueError:
        line = s.split("\n", 1)[0]
        try:
            doc = json.loads(line)
        except ValueError:
            return set(), None
    first = doc[0] if isinstance(doc, list) and doc else doc
    return (set(first) if isinstance(first, dict) else set()), doc


def _columns(path: Path) -> tuple[set[str], Any]:
    """(column or key names, parsed JSON document or None) of a backtest file."""
    suffix = path.suffix.lower()
    if suffix in (".parquet", ".pq"):
        import pyarrow.parquet as pq

        try:
            return set(pq.read_schema(path).names), None
        except Exception as e:  # unreadable Parquet
            raise QtError("QT-INPUT-001", f"{path}: unreadable Parquet ({e})") from e
    try:
        with path.open("rb") as f:
            raw = f.read(_HEAD)
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{path}: {e}") from e
    text = raw.decode("utf-8", errors="replace")
    if text.lstrip()[:1] in ("{", "["):
        if len(raw) == _HEAD:  # too large to parse a prefix as one document: read it whole
            text = path.read_text(encoding="utf-8", errors="replace")
        return _keys_of_json(text)
    header = text.split("\n", 1)[0]
    return {c.strip().strip('"') for c in header.split(",")}, None


def detect(path: str | Path) -> str | None:
    """The backtester that wrote `path`, or None when the file does not say."""
    p = Path(path)
    if not p.is_file():
        raise QtError("QT-INPUT-001", f"{p}: no such file")
    with p.open("rb") as f:
        magic = f.read(16)
    if magic.startswith(b"PK\x03\x04"):
        try:
            names = zipfile.ZipFile(p).namelist()
        except zipfile.BadZipFile:
            return None
        return "freqtrade" if any(n.endswith(".json") for n in names) else None
    if magic.startswith(b"SQLite format 3\x00"):
        return "freqtrade"
    cols, doc = _columns(p)
    if {"price_tick", "tick_size", "leaves_qty"} <= cols:
        return "hftbacktest"
    if {"Order Id", "Column"} <= cols or (
        "Column" in cols and any("Trade Id" in c or c == "Position Id" for c in cols)
    ):
        return "vectorbt"
    if "event" in cols and "ref" in cols:
        return "backtrader"
    if isinstance(doc, dict) and doc and all(isinstance(v, list) for k, v in doc.items() if k != "date"):
        if all(isinstance(r, list) for v in doc.values() if isinstance(v, list) for r in v):
            return "backtrader"
    if isinstance(doc, dict) and isinstance(doc.get("strategy"), dict):
        return "freqtrade"
    if isinstance(doc, dict) and "orders" in doc:
        return "lean"
    if cols & {"orderEventId", "algorithmId", "order-event-id", "OrderId", "orderId"}:
        return "lean"
    if cols & {"client_order_id", "ts_init", "last_qty"}:
        return "nautilus"
    if p.stem.replace("_", "").lower().startswith(_NT_EVENTS):
        return "nautilus"
    return None


def detect_all(paths: list[str]) -> str:
    """The one backtester of all `paths` (`QT-USAGE-001` when a file does not say or two files disagree)."""
    kinds: dict[str, list[str]] = {}
    unknown: list[str] = []
    for p in paths:
        k = detect(p)
        if k is None:
            unknown.append(p)
        else:
            kinds.setdefault(k, []).append(p)
    if len(kinds) > 1:
        seen = "; ".join(f"{k}: {', '.join(v)}" for k, v in sorted(kinds.items()))
        raise QtError(
            "QT-USAGE-001",
            f"check: the files look like output of different backtesters ({seen})",
            "check one backtest at a time, or name it with --backtester",
        )
    if unknown and not kinds:
        raise QtError(
            "QT-USAGE-001",
            f"check: cannot tell which backtester wrote {', '.join(unknown)} (no column or key of a known format)",
            f"name it with --backtester ({', '.join(BACKTESTERS)})",
        )
    return next(iter(kinds))
