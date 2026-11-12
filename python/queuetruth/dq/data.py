"""Dataset access for `queuetruth dq`: the manifest, the instrument rows, the ingest-time `qt.data_quality`, and one
partition at a time as numpy columns (a partition is read once and handed to every check, then released).

Only the files of SPEC 3.3 are read (`manifest.json`, `instruments.parquet`, `data_quality.json`,
`mbo|l2/date=.../instrument_id=.../part-*.parquet`, `trades_source/...`); nothing is written.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.dataset import MANIFEST_FILE, date_str, open_dataset
from queuetruth.dataset import read_parquet_bounded as read_parquet
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
#: Schema 1.2: the NYSE TAQ Integrated Feed runs on the Nasdaq record semantics, but its record `sequence` (the
#: channel number) has holes for the control messages the TAQ files omit, so it is not gap-checked
#: (`qt_types::rules_for(RuleSet::Nyse)`; the reader checks Symbol Sequence Numbers at ingest).
RULES["nyse"] = {**RULES["nasdaq"], "sequence_meaningful": False}

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


def _no_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _bad(where: str, msg: str) -> QtError:
    return QtError("QT-INPUT-002", f"{where}: {msg}")


def _list_of_dicts(v: Any, where: str) -> list[dict[str, Any]]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
        raise _bad(where, "expected an array of objects")
    return v


def _normalized(m: dict[str, Any]) -> dict[str, Any]:
    """The checked manifest as the checks read it: `inputs`, `source_vintage` and `partitions` as lists, each
    partition's `files` as a list (copies; the file is not modified)."""
    out = dict(m)
    out["partitions"] = [{**p, "files": list(p.get("files") or [])} for p in m.get("partitions") or []]
    for k in ("rows", "ts_min", "ts_max"):
        for q in out["partitions"]:
            q.setdefault(k, None)
    out["inputs"] = list(m.get("inputs") or [])
    out["source_vintage"] = list(m.get("source_vintage") or [])
    return out


def _read_parquet(path: Path, what: str, columns: list[str] | None = None) -> pa.Table:
    """A Parquet file's table (only `columns`, when given), `QT-INPUT-002` for every way a damaged file fails (Arrow
    errors, invalid UTF-8 in a column name) and for a file that decodes past the expansion limit
    (`read_parquet_bounded`: a few KB of dictionary- or RLE-encoded Parquet can hold millions of rows)."""
    return read_parquet(path, columns=columns, what=what)


def _check_ingest_dq(doc: dict[str, Any], dpath: Path) -> dict[str, Any]:
    """`data_quality.json` with the fields the checks read validated."""
    from queuetruth.dq.book import DQ_SCALARS

    for k in DQ_SCALARS:
        v = doc.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
            raise _bad(f"{dpath}: {k}", "expected a number")
    ss = doc.get("source_specific")
    if ss is not None:
        if not isinstance(ss, dict):
            raise _bad(f"{dpath}: source_specific", "expected an object")
        for k, v in ss.items():
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                raise _bad(f"{dpath}: source_specific.{k}", "expected a number")
    _list_of_dicts(doc.get("feed_gaps"), f"{dpath}: feed_gaps")
    reasons = doc.get("reasons")
    if reasons is not None and (not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons)):
        raise _bad(f"{dpath}: reasons", "expected an array of strings")
    return doc


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
    """A queuetruth dataset directory (SPEC 3.3).

    The manifest, `instruments.parquet` and `data_quality.json` are validated on load (every field the checks read
    has its documented JSON / Arrow type; partition files are relative paths inside the dataset directory), so a
    damaged or hand-edited dataset fails `QT-INPUT-002` naming the field instead of crashing a check."""

    def __init__(self, path: str | Path) -> None:
        self.root = Path(path)
        mpath = self.root / MANIFEST_FILE
        if not mpath.is_file():
            raise QtError(
                "QT-INPUT-001",
                f"{self.root}: not a queuetruth dataset (no manifest.json)",
                "pass a directory written by `queuetruth ingest` or `fixtures synth`",
            )
        ds = open_dataset(self.root, bounded=True)
        if not ds.manifest.is_dataset:
            raise QtError("QT-INPUT-002", f"{mpath}: not a qt.dataset_manifest document")
        self.dataset = ds
        self.manifest: dict[str, Any] = _normalized(ds.manifest.to_dict())
        self.kind: str = ds.manifest.kind
        self.source_format: str = ds.manifest.source_format
        self.venue: str = ds.manifest.venue
        self.rule_set_name: str = ds.manifest.rule_set
        self.rules_known = self.rule_set_name in RULES
        self.rules = RULES.get(self.rule_set_name, RULES["generic"])
        self.instruments: list[dict[str, Any]] = ds.instrument_rows()
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
                doc = json.loads(dpath.read_text(encoding="utf-8"), parse_constant=_no_constant)
            except (OSError, ValueError, RecursionError):
                doc = None  # an unreadable data_quality.json counts as absent (the dq_counters check warns)
            self.ingest_dq = _check_ingest_dq(doc, dpath) if isinstance(doc, dict) else None
        self.parts: list[PartRef] = []
        for p in ds.partitions:
            ref = PartRef(
                instrument_id=p.instrument_id,
                session_date=p.session_date,
                files=[self.root / f.path for f in p.files],
                rows=p.rows or 0,
                ts_min=p.ts_min,
                ts_max=p.ts_max,
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
        """The partition's records (all parts concatenated in order). MBO columns are cast to their `qt.mbo` types
        (a column of another type, a missing column or a null fails `QT-INPUT-002`)."""
        missing = [f for f in ref.files if not f.is_file()]
        if missing:
            raise QtError("QT-INPUT-001", f"{missing[0]}: partition file missing (manifest lists it)")
        tables = [_read_parquet(f, "unreadable partition", list(columns)) for f in ref.files]
        try:
            t = (pa.concat_tables(tables) if len(tables) > 1 else tables[0]) if tables else None
        except (ValueError, pa.ArrowException) as e:  # parts with different schemas
            raise QtError("QT-INPUT-002", f"{ref.files[0]}: unreadable partition: {e}") from None
        if t is None:
            return Part(ref, {c: np.zeros(0, dtype=np.int64) for c in columns})
        mbo = self.kind == "mbo"
        cols = {}
        for c in columns:
            if c not in t.column_names:
                if mbo:
                    raise _bad(str(ref.files[0]), f"partition has no {c} column")
                continue
            col = t.column(c)
            want = _MBO_TYPES.get(c) if mbo else None
            if want is not None:
                try:
                    col = col.cast(want, safe=True)
                except (pa.ArrowException, ValueError, TypeError) as e:
                    raise _bad(f"{ref.files[0]}: {c}", f"not castable to {want}: {e}") from None
                if col.null_count:
                    raise _bad(f"{ref.files[0]}: {c}", f"{col.null_count} null value(s) in a non-null column")
            cols[c] = _to_numpy(col)
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
        tables = [_read_parquet(f, "unreadable trades_source") for f in files]
        try:
            return pa.concat_tables(tables)
        except (ValueError, pa.ArrowException) as e:
            raise QtError("QT-INPUT-002", f"{d}: unreadable trades_source: {e}") from None

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


#: `qt.mbo` column types (SPEC 3.1); timestamps as int64 ns.
_MBO_TYPES: dict[str, pa.DataType] = {
    "ts_event": pa.timestamp("ns", tz="UTC"),
    "ts_recv": pa.timestamp("ns", tz="UTC"),
    "instrument_id": pa.uint32(),
    "action": pa.uint8(),
    "side": pa.uint8(),
    "price": pa.int64(),
    "size": pa.uint64(),
    "order_id": pa.uint64(),
    "flags": pa.uint8(),
    "sequence": pa.uint64(),
    "channel_id": pa.uint16(),
    "publisher_id": pa.uint16(),
}


def _l2_columns(ref: PartRef) -> list[str]:
    if not ref.files:
        return ["ts_event", "ts_recv"]
    try:
        names = pq.read_schema(ref.files[0]).names
    except (OSError, ValueError, pa.ArrowException) as e:
        raise QtError("QT-INPUT-002", f"{ref.files[0]}: unreadable partition: {e}") from None
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
