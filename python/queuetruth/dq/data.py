"""Dataset access for `queuetruth dq`: the manifest, the instrument rows, the ingest-time `qt.data_quality`, and one
partition at a time as numpy columns (a partition is read once and handed to every check, then released).

Only the files of SPEC 3.3 are read (`manifest.json`, `instruments.parquet`, `data_quality.json`,
`mbo|l2/date=.../instrument_id=.../part-*.parquet`, `trades_source/...`); nothing is written.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.errors import QtError

# DBN record flags (SPEC 3.1, `qt_types::flags`).
F_LAST = 1 << 7
F_SNAPSHOT = 1 << 5
F_BAD_TS_RECV = 1 << 3
F_MAYBE_BAD_BOOK = 1 << 2
#: `qt.mbo` price of records without one (DBN convention).
UNDEF_PRICE = (1 << 63) - 1

A, C, M, R, T, F, N = (ord(x) for x in "ACMRTFN")
SIDE_B, SIDE_A, SIDE_N = ord("B"), ord("A"), ord("N")
NS = 1_000_000_000
DAY_NS = 86_400 * NS

#: Venue rules used by the checks (SPEC 3.2, mirror of `qt_types::rules`; `crypto_l3` = generic without hidden
#: orders). Unknown rule sets fall back to `generic` with a report warning.
RULES: dict[str, dict[str, bool]] = {
    "nasdaq": {
        "modify_records_present": False,
        "snapshot_begins_with_clear": False,
        "snapshot_records_flagged": False,
        "supports_hidden_orders": True,
        "utc_midnight_snapshot": False,
        "sequence_meaningful": True,
        "negative_prices_allowed": False,
    },
    "cme": {
        "modify_records_present": True,
        "snapshot_begins_with_clear": True,
        "snapshot_records_flagged": True,
        "supports_hidden_orders": False,
        "utc_midnight_snapshot": True,
        "sequence_meaningful": True,
        "negative_prices_allowed": True,
    },
    "coinbase": {
        "modify_records_present": True,
        "snapshot_begins_with_clear": False,
        "snapshot_records_flagged": False,
        "supports_hidden_orders": False,
        "utc_midnight_snapshot": False,
        "sequence_meaningful": True,
        "negative_prices_allowed": False,
    },
    "generic": {
        "modify_records_present": True,
        "snapshot_begins_with_clear": False,
        "snapshot_records_flagged": True,
        "supports_hidden_orders": True,
        "utc_midnight_snapshot": False,
        "sequence_meaningful": False,
        "negative_prices_allowed": False,
    },
}
RULES["crypto_l3"] = {**RULES["generic"], "supports_hidden_orders": False}
#: Schema 1.2: IEX DEEP+ runs on the Nasdaq record semantics (`qt_types` `IEX_RULES`).
RULES["iex"] = dict(RULES["nasdaq"])

#: Formats whose `sequence` is assigned by the reader (one per message, contiguous by construction): no gap check.
SYNTHETIC_SEQUENCE_FORMATS = frozenset({"itch"})

MBO_COLUMNS = (
    "ts_event",
    "ts_recv",
    "instrument_id",
    "action",
    "side",
    "price",
    "size",
    "order_id",
    "flags",
    "sequence",
    "channel_id",
    "publisher_id",
)


def date_str(days: int) -> str:
    """`YYYY-MM-DD` of a date32 value."""
    return (datetime.date(1970, 1, 1) + datetime.timedelta(days=int(days))).isoformat()


def _days(v: Any) -> int:
    if isinstance(v, datetime.date):
        return (v - datetime.date(1970, 1, 1)).days
    return int(v)


@dataclass
class PartRef:
    """One `(session_date, instrument_id)` partition of the manifest."""

    instrument_id: int
    session_date: int
    files: list[Path]
    rows: int
    ts_min: int | None
    ts_max: int | None
    instrument: dict[str, Any] | None = None

    @property
    def date(self) -> str:
        return date_str(self.session_date)

    @property
    def symbol(self) -> str:
        sym = (self.instrument or {}).get("symbol")
        return str(sym) if sym else f"#{self.instrument_id}"

    @property
    def key(self) -> tuple[int, int]:
        return (self.session_date, self.instrument_id)


@dataclass
class Part:
    """A partition's records as numpy arrays (timestamps as int64 ns), in feed order."""

    ref: PartRef
    cols: dict[str, np.ndarray] = field(default_factory=dict)

    def __getitem__(self, name: str) -> np.ndarray:
        return self.cols[name]

    def __len__(self) -> int:
        return len(self.cols["ts_event"]) if "ts_event" in self.cols else 0


def _to_numpy(col: pa.ChunkedArray) -> np.ndarray:
    t = col.type
    if pa.types.is_timestamp(t):
        col = col.cast(pa.int64())
    elif pa.types.is_dictionary(t):
        col = col.cast(pa.utf8())
    if col.null_count:
        return np.asarray(col.to_pylist(), dtype=object)
    return col.to_numpy()


class Dataset:
    """A queuetruth dataset directory (SPEC 3.3)."""

    def __init__(self, path: str | Path) -> None:
        self.root = Path(path)
        mpath = self.root / "manifest.json"
        if not mpath.is_file():
            raise QtError(
                "QT-INPUT-001",
                f"{self.root}: not a queuetruth dataset (no manifest.json)",
                "pass a directory written by `queuetruth ingest` or `fixtures synth`",
            )
        try:
            self.manifest: dict[str, Any] = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise QtError("QT-INPUT-002", f"{mpath}: unreadable manifest: {e}") from None
        if self.manifest.get("schema") != "qt.dataset_manifest":
            raise QtError("QT-INPUT-002", f"{mpath}: not a qt.dataset_manifest document")
        self.kind: str = str(self.manifest.get("kind") or "mbo")
        self.source_format: str = str(self.manifest.get("source_format") or "")
        self.venue: str = str(self.manifest.get("venue") or "generic")
        self.rule_set_name: str = str(self.manifest.get("rule_set") or self.venue)
        self.rules_known = self.rule_set_name in RULES
        self.rules = RULES.get(self.rule_set_name, RULES["generic"])
        self.instruments: list[dict[str, Any]] = []
        ipath = self.root / "instruments.parquet"
        if ipath.is_file():
            try:
                self.instruments = pq.read_table(ipath).to_pylist()
            except (OSError, pa.ArrowInvalid) as e:
                raise QtError("QT-INPUT-002", f"{ipath}: unreadable: {e}") from None
            for row in self.instruments:
                row["session_date"] = _days(row["session_date"])
        self.inst_by_key: dict[tuple[int, int], dict[str, Any]] = {}
        self.duplicate_instrument_rows: list[tuple[int, int]] = []
        for row in self.instruments:
            k = (int(row["session_date"]), int(row["instrument_id"]))
            if k in self.inst_by_key:
                self.duplicate_instrument_rows.append(k)
            else:
                self.inst_by_key[k] = row
        self.ingest_dq: dict[str, Any] | None = None
        dpath = self.root / "data_quality.json"
        if dpath.is_file():
            try:
                doc = json.loads(dpath.read_text(encoding="utf-8"))
                self.ingest_dq = doc if isinstance(doc, dict) else None
            except (OSError, ValueError):
                self.ingest_dq = None
        self.parts: list[PartRef] = []
        for p in self.manifest.get("partitions") or []:
            files = [self.root / f["path"] for f in p.get("files") or []]
            ref = PartRef(
                instrument_id=int(p["instrument_id"]),
                session_date=_days(p["session_date"]),
                files=files,
                rows=int(p.get("rows") or 0),
                ts_min=p.get("ts_min"),
                ts_max=p.get("ts_max"),
            )
            ref.instrument = self.inst_by_key.get(ref.key)
            self.parts.append(ref)
        self.parts.sort(key=lambda r: r.key)

    # ---- descriptive --------------------------------------------------------------------------------------------

    @property
    def records(self) -> int:
        return sum(p.rows for p in self.parts)

    @property
    def session_dates(self) -> list[str]:
        return [date_str(d) for d in sorted({p.session_date for p in self.parts})]

    @property
    def symbols(self) -> list[str]:
        return sorted({p.symbol for p in self.parts})

    @property
    def sequence_checkable(self) -> bool:
        """Sequence numbers carry feed order (venue rule `sequence_meaningful`, reader-assigned ITCH numbers
        excluded)."""
        return bool(self.rules["sequence_meaningful"]) and self.source_format not in SYNTHETIC_SEQUENCE_FORMATS

    def ingest_counter(self, name: str) -> int:
        d = self.ingest_dq or {}
        v = d.get(name)
        if v is None:
            v = (d.get("source_specific") or {}).get(name, 0)
        return int(v or 0)

    def session_window(self, ref: PartRef) -> tuple[int, int, int] | None:
        """(local midnight, open, close) in UTC ns of a partition, from its instrument row."""
        inst = ref.instrument
        if not inst or inst.get("midnight_utc_ns") is None:
            return None
        mid = int(inst["midnight_utc_ns"])
        return mid, mid + int(inst.get("open_s") or 0) * NS, mid + int(inst.get("close_s") or 86_400) * NS

    # ---- reading ------------------------------------------------------------------------------------------------

    def read(self, ref: PartRef, columns: tuple[str, ...] = MBO_COLUMNS) -> Part:
        """The partition's records (all parts concatenated in order)."""
        missing = [f for f in ref.files if not f.is_file()]
        if missing:
            raise QtError("QT-INPUT-001", f"{missing[0]}: partition file missing (manifest lists it)")
        try:
            tables = [pq.read_table(f) for f in ref.files]
        except (OSError, pa.ArrowInvalid) as e:
            raise QtError("QT-INPUT-002", f"{ref.files[0]}: unreadable partition: {e}") from None
        if not tables:
            return Part(ref, {c: np.zeros(0, dtype=np.int64) for c in columns})
        t = pa.concat_tables(tables) if len(tables) > 1 else tables[0]
        cols = {c: _to_numpy(t.column(c)) for c in columns if c in t.column_names}
        return Part(ref, cols)

    def iter_parts(self) -> Iterator[Part]:
        for ref in self.parts:
            yield self.read(ref, MBO_COLUMNS if self.kind == "mbo" else tuple(_l2_columns(ref)))

    def trades_source(self, ref: PartRef) -> pa.Table | None:
        """The partition's `trades_source` rows (`qt.trades`), or None when the dataset has none for it."""
        d = self.root / "trades_source" / f"date={ref.date}" / f"instrument_id={ref.instrument_id}"
        files = sorted(d.glob("part-*.parquet")) if d.is_dir() else []
        if not files:
            return None
        return pa.concat_tables([pq.read_table(f) for f in files])

    @property
    def has_trades_source(self) -> bool:
        return (self.root / "trades_source").is_dir()

    def describe(self) -> dict[str, Any]:
        m = self.manifest
        return {
            "path": str(self.root),
            "dataset_id": m.get("dataset_id"),
            "kind": m.get("kind"),
            "source_format": m.get("source_format"),
            "venue": m.get("venue"),
            "source_venue": m.get("source_venue"),
            "rule_set": m.get("rule_set"),
            "validation_status": m.get("validation_status"),
            "partitions": len(self.parts),
            "records": self.records,
            "session_dates": self.session_dates,
            "symbols": self.symbols,
        }


def _l2_columns(ref: PartRef) -> list[str]:
    if not ref.files:
        return ["ts_event", "ts_recv"]
    names = pq.read_schema(ref.files[0]).names
    return [c for c in ("ts_event", "ts_recv", "kind", "side", "price", "new_qty", "sequence", "flags") if c in names]


__all__ = [
    "DAY_NS",
    "F_BAD_TS_RECV",
    "F_LAST",
    "F_MAYBE_BAD_BOOK",
    "F_SNAPSHOT",
    "MBO_COLUMNS",
    "NS",
    "RULES",
    "SIDE_A",
    "SIDE_B",
    "SIDE_N",
    "UNDEF_PRICE",
    "A",
    "C",
    "Dataset",
    "F",
    "M",
    "N",
    "Part",
    "PartRef",
    "R",
    "T",
    "date_str",
]
