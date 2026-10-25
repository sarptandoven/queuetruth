"""Batches -> a queuetruth L2-only dataset (SPEC 3.3, 3.4 `qt.l2`), and the reader the queue engine uses.

The layout is the one `queuetruth ingest --format tardis-l2` writes, so `queuetruth zoo`, `calibrate-live`,
`score` and `dq` read it: `manifest.json` (`qt.dataset_manifest`, `kind = l2`, venue `generic`, `source_venue`
`kalshi` or `polymarket`, `validation_status = assumed`), `instruments.parquet` (one row per instrument and UTC
session date, `price_exp = -4`, session 00:00-24:00 UTC), `l2/date=YYYY-MM-DD/instrument_id=N/part-00000.parquet`
and `data_quality.json` (`qt.data_quality`, verdict by the rule of SPEC 5.1.5).

Rows per batch (the Tardis conventions of `qt_crypto::tardis`): a `resync` batch is a `clear` row then one
`snapshot` row per level (bids best first, then asks; flag `SNAPSHOT`); a `depth` batch is its trade rows first
(`trade_position = before`: `side` = the hit side, `aggressor` = the taker side, `level` 255) then one `depth` row per
changed level (bids best first, then asks; `prev_qty` = size before the batch, `level` = rank after it, 255 when
the level emptied); the last row carries `LAST_IN_BATCH`. Every row of a batch shares `event_id` (1-based per
partition), `ts_event`, `ts_recv` and `sequence` (the batch's `seq`, monotone per instrument). Counts are not
published by either venue (`prev_ct` / `new_ct` null). Partitions are UTC dates (by the running maximum of
`ts_event`); each partition after an instrument's first starts with a restatement of the book (`clear` +
`snapshot` rows, counted as `day_boundary_snapshots`) so it replays on its own.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth._version import __version__
from queuetruth.errors import QtError
from queuetruth.pm.batches import Batch, BuildStats
from queuetruth.pm.book import ASK, BID, NONE, Ladder
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.schemas.models import DataQuality, DatasetManifest

DAY_NS = 86_400_000_000_000
LAST_IN_BATCH, SNAPSHOT = 1, 8
KINDS = ("depth", "trade", "snapshot", "clear")
MAX_LEVEL = 254


@dataclass(frozen=True)
class Instrument:
    """One instrument of a prediction-market dataset (a binary market on the canonical YES ladder)."""

    instrument_id: int
    symbol: str
    tick_size: int
    size_exp: int
    lot_size: int = 1
    currency: str = "USD"
    price_exp: int = -4


@dataclass
class Series:
    """The batches of one instrument with its build counters."""

    instrument: Instrument
    batches: list[Batch]
    stats: BuildStats = field(default_factory=BuildStats)


def _date_str(days: int) -> str:
    return datetime.fromtimestamp(days * 86_400, UTC).strftime("%Y-%m-%d")


class _Rows:
    def __init__(self) -> None:
        self.cols: dict[str, list[Any]] = {f.name: [] for f in SCHEMAS["qt.l2"]}

    def add(
        self,
        ts: int,
        recv: int,
        iid: int,
        eid: int,
        seq: int,
        kind: str,
        side: int,
        price: int | None,
        prev: int,
        new: int,
        tq: int,
        aggr: int,
        level: int,
        flags: int,
    ) -> None:
        c = self.cols
        c["ts_event"].append(ts)
        c["ts_recv"].append(recv)
        c["instrument_id"].append(iid)
        c["event_id"].append(eid)
        c["sequence"].append(seq)
        c["kind"].append(KINDS.index(kind))
        c["side"].append(side)
        c["price"].append(price)
        c["prev_qty"].append(prev)
        c["new_qty"].append(new)
        c["prev_ct"].append(None)
        c["new_ct"].append(None)
        c["trade_qty"].append(tq)
        c["aggressor"].append(aggr)
        c["level"].append(level)
        c["flags"].append(flags)

    def mark_last(self) -> None:
        if self.cols["flags"]:
            self.cols["flags"][-1] |= LAST_IN_BATCH

    def __len__(self) -> int:
        return len(self.cols["ts_event"])

    def table(self) -> pa.Table:
        schema = SCHEMAS["qt.l2"]
        arrays = []
        for f in schema:
            vals = self.cols[f.name]
            if f.name == "kind":
                codes = pa.array(vals, type=pa.int8())
                arrays.append(pa.DictionaryArray.from_arrays(codes, pa.array(list(KINDS), type=pa.utf8())))
            elif pa.types.is_timestamp(f.type):
                arrays.append(pa.array(np.asarray(vals, dtype=np.int64), type=pa.int64()).cast(f.type))
            else:
                arrays.append(pa.array(vals, type=f.type))
        return pa.Table.from_arrays(arrays, schema=schema)


def _rank(book: Ladder, side: int, price: int) -> int:
    if book.qty(side, price) == 0:
        return 255
    return min(book.rank(side, price), MAX_LEVEL)


def _emit_resync(rows: _Rows, book: Ladder, iid: int, eid: int, b_ts: int, b_recv: int, seq: int) -> int:
    rows.add(b_ts, b_recv, iid, eid, seq, "clear", NONE, None, 0, 0, 0, NONE, 255, 0)
    n = 1
    for side in (BID, ASK):
        for r, (p, q) in enumerate(book.levels(side)):
            rows.add(b_ts, b_recv, iid, eid, seq, "snapshot", side, p, 0, q, 0, NONE, min(r, MAX_LEVEL), SNAPSHOT)
            n += 1
    rows.mark_last()
    return n


def series_rows(series: Series) -> dict[int, tuple[pa.Table, int, int, int]]:
    """{session day: (qt.l2 table, ts_min, ts_max, day-boundary restatements)} of one instrument."""
    iid = series.instrument.instrument_id
    book = Ladder()
    out: dict[int, tuple[pa.Table, int, int, int]] = {}
    rows = _Rows()
    day: int | None = None
    eid = 0
    run_max: int | None = None
    ts_lo = ts_hi = 0
    boundary = 0
    restated_here = 0
    last_seq = 0
    for b in series.batches:
        run_max = b.ts_event if run_max is None else max(run_max, b.ts_event)
        d = run_max // DAY_NS
        if day is None:
            day, ts_lo, ts_hi = d, b.ts_event, b.ts_event
        elif d != day:
            if len(rows):
                out[day] = (rows.table(), ts_lo, ts_hi, restated_here)
            rows, eid, day, restated_here = _Rows(), 0, d, 0
            ts_lo = ts_hi = b.ts_event
            if len(book):
                eid += 1
                _emit_resync(rows, book, iid, eid, b.ts_event, b.ts_recv, last_seq)
                boundary += 1
                restated_here = 1
        ts_lo, ts_hi = min(ts_lo, b.ts_event), max(ts_hi, b.ts_event)
        eid += 1
        if b.kind == "resync":
            book.clear()
            for s, p, q in b.levels:
                book.set(s, p, q)
            _emit_resync(rows, book, iid, eid, b.ts_event, b.ts_recv, b.seq)
            last_seq = b.seq
            continue
        for t in b.trades:
            rows.add(
                b.ts_event, b.ts_recv, iid, eid, b.seq, "trade", t.hit_side, t.price, 0, 0, t.size, t.aggressor, 255, 0
            )
        for s, p, prev, new in b.changes:
            have = book.qty(s, p)
            if have != prev:
                raise QtError(
                    "QT-ENGINE-001",
                    f"instrument {iid} batch {b.seq}: level {chr(s)} {p} is {have} in the book, the batch says {prev}",
                )
            book.set(s, p, new)
        for s, p, prev, new in b.changes:
            rows.add(b.ts_event, b.ts_recv, iid, eid, b.seq, "depth", s, p, prev, new, 0, NONE, _rank(book, s, p), 0)
        rows.mark_last()
        last_seq = b.seq
    if day is not None and len(rows):
        out[day] = (rows.table(), ts_lo, ts_hi, restated_here)
    return out


def _verdict(dq: dict[str, Any]) -> tuple[str, list[str]]:
    """SPEC 5.1.5 verdict and reasons."""
    events = max(int(dq["records"]), 1)
    unusable = []
    if dq["maybe_bad_book_records"] > 0:
        unusable.append(f"maybe_bad_book_records = {dq['maybe_bad_book_records']}")
    if dq["crossed_books_after_last"] / events > 1e-3:
        unusable.append(f"crossed_books_after_last / events = {dq['crossed_books_after_last'] / events:.6g} > 0.001")
    if dq["unknown_order_refs"] / events > 1e-3:
        unusable.append("unknown_order_refs / records > 0.001")
    if unusable:
        return "unusable", unusable
    anomalies = [
        k
        for k in (
            "bad_ts_recv_records",
            "pre_snapshot_rows_skipped",
            "unknown_order_refs",
            "duplicate_adds",
            "unexpected_modifies",
            "sequence_gaps",
            "sequence_regressions",
            "out_of_order_ts_event",
            "crossed_books_after_last",
        )
        if dq[k]
    ]
    reasons = [f"{k} = {dq[k]}" for k in anomalies]
    reasons += [f"{k} = {v}" for k, v in sorted(dq["source_specific"].items()) if v]
    if dq["feed_gaps"]:
        reasons.append(f"feed_gaps = {len(dq['feed_gaps'])}")
    return ("warnings", reasons) if reasons else ("clean", [])


def write_dataset(
    root: str | Path,
    series: list[Series],
    *,
    source_format: str,
    source_venue: str,
    inputs: list[str | Path],
    sequence_gaps: int = 0,
    sequence_regressions: int = 0,
    extra_anomalies: dict[str, int] | None = None,
) -> tuple[DatasetManifest, DataQuality, list[dict[str, Any]]]:
    """Writes the dataset under `root` (created; existing files of the same names are replaced). Returns the
    manifest, the data-quality document and the `Files` entries."""
    out = Path(root)
    out.mkdir(parents=True, exist_ok=True)
    partitions = []
    files: list[dict[str, Any]] = []
    shas: list[str] = []
    inst_rows: list[dict[str, Any]] = []
    records = snapshot_rows = boundary = 0
    dq_counts = {
        "pre_snapshot_rows_skipped": 0,
        "out_of_order_ts_event": 0,
        "crossed_books_after_last": 0,
    }
    source_specific: dict[str, int] = dict(extra_anomalies or {})
    feed_gaps: list[dict[str, Any]] = []
    for sr in sorted(series, key=lambda x: x.instrument.instrument_id):
        ins = sr.instrument
        st = sr.stats
        dq_counts["pre_snapshot_rows_skipped"] += st.pre_snapshot_rows_skipped
        dq_counts["out_of_order_ts_event"] += st.out_of_order_ts_event
        dq_counts["crossed_books_after_last"] += st.crossed_books_after_last
        for k, v in st.anomalies().items():
            source_specific[k] = source_specific.get(k, 0) + v
        for a, b, why in st.feed_gaps:
            feed_gaps.append({"instrument_id": ins.instrument_id, "ts_start": int(a), "ts_end": int(b), "reason": why})
        for day, (table, ts_lo, ts_hi, restated) in sorted(series_rows(sr).items()):
            rel = f"l2/date={_date_str(day)}/instrument_id={ins.instrument_id}/part-00000.parquet"
            entry = write_table(table, out / rel, SCHEMAS["qt.l2"])
            entry["name"] = rel
            files.append(entry)
            shas.append(entry["sha256"])
            records += table.num_rows
            kinds = table.column("kind").cast(pa.utf8()).to_pylist()
            snapshot_rows += sum(1 for k in kinds if k == "snapshot")
            boundary += restated
            partitions.append(
                {
                    "instrument_id": ins.instrument_id,
                    "session_date": day,
                    "files": [{"path": rel, "rows": table.num_rows, "sha256": entry["sha256"]}],
                    "rows": table.num_rows,
                    "ts_min": int(ts_lo),
                    "ts_max": int(ts_hi),
                }
            )
            inst_rows.append(
                {
                    "instrument_id": ins.instrument_id,
                    "session_date": day,
                    "symbol": ins.symbol,
                    "venue": "generic",
                    "source_venue": source_venue,
                    "price_exp": ins.price_exp,
                    "tick_size": ins.tick_size,
                    "lot_size": ins.lot_size,
                    "size_exp": ins.size_exp,
                    "contract_multiplier": 1.0,
                    "currency": ins.currency,
                    "session_tz": "UTC",
                    "midnight_utc_ns": day * DAY_NS,
                    "open_s": 0,
                    "close_s": 86_400,
                    "source": source_format,
                }
            )
    inst_table = _instruments_table(inst_rows)
    ient = write_table(inst_table, out / "instruments.parquet", SCHEMAS["qt.instruments"])
    files.append(ient)
    dq = {
        "records": records,
        "maybe_bad_book_records": 0,
        "bad_ts_recv_records": 0,
        "snapshot_records": snapshot_rows,
        "pre_snapshot_rows_skipped": dq_counts["pre_snapshot_rows_skipped"],
        "unknown_order_refs": 0,
        "duplicate_adds": 0,
        "unexpected_modifies": 0,
        "sequence_gaps": sequence_gaps,
        "sequence_regressions": sequence_regressions,
        "out_of_order_ts_event": dq_counts["out_of_order_ts_event"],
        "crossed_books_after_last": dq_counts["crossed_books_after_last"],
        "hidden_prints": 0,
        "off_price_executions": 0,
        "feed_gaps": feed_gaps,
        "source_specific": {k: v for k, v in sorted(source_specific.items()) if v},
    }
    verdict, reasons = _verdict(dq)
    dq_doc = DataQuality.model_validate({**dq, "verdict": verdict, "reasons": reasons})
    dq_path = out / "data_quality.json"
    dq_path.write_text(json.dumps(dq_doc.model_dump(mode="json", by_alias=True), indent=2) + "\n", encoding="utf-8")
    files.append(
        {"name": "data_quality.json", "rows": None, "bytes": dq_path.stat().st_size, "sha256": sha256_file(dq_path)}
    )
    ident = "\n".join(sorted(shas)) + "\n" + source_format
    manifest = DatasetManifest.model_validate(
        {
            "dataset_id": hashlib.sha256(ident.encode()).hexdigest(),
            "kind": "l2",
            "source_format": source_format,
            "venue": "generic",
            "source_venue": source_venue,
            "inputs": [{"path": str(p), "bytes": Path(p).stat().st_size, "sha256": sha256_file(p)} for p in inputs],
            "partitions": partitions,
            "price_exp_by_instrument": {str(s.instrument.instrument_id): s.instrument.price_exp for s in series},
            "created_by": f"queuetruth {__version__}",
            "validation_status": "assumed",
        }
    )
    mpath = out / "manifest.json"
    mpath.write_text(json.dumps(manifest.model_dump(mode="json", by_alias=True), indent=2) + "\n", encoding="utf-8")
    files.append({"name": "manifest.json", "rows": None, "bytes": mpath.stat().st_size, "sha256": sha256_file(mpath)})
    return manifest, dq_doc, files


def _instruments_table(rows: list[dict[str, Any]]) -> pa.Table:
    schema = SCHEMAS["qt.instruments"]
    venues = ["nasdaq", "cme", "coinbase", "generic"]
    arrays = []
    for f in schema:
        vals = [r[f.name] for r in rows]
        if f.name == "venue":
            arrays.append(
                pa.DictionaryArray.from_arrays(
                    pa.array([venues.index(v) for v in vals], type=pa.int8()), pa.array(venues, type=pa.utf8())
                )
            )
        elif f.name == "session_date":
            arrays.append(pa.array(vals, type=pa.int32()).cast(pa.date32()))
        else:
            arrays.append(pa.array(vals, type=f.type))
    return pa.Table.from_arrays(arrays, schema=schema)


# ---- reading back -------------------------------------------------------------------------------------------------


@dataclass
class L2Arrays:
    """One instrument's `qt.l2` rows as NumPy columns, in file order (partitions in date order)."""

    instrument_id: int
    ts_event: np.ndarray
    ts_recv: np.ndarray
    event_id: np.ndarray
    sequence: np.ndarray
    kind: np.ndarray  # int8 codes into KINDS
    side: np.ndarray
    price: np.ndarray  # -1 for null
    prev_qty: np.ndarray
    new_qty: np.ndarray
    trade_qty: np.ndarray
    aggressor: np.ndarray
    flags: np.ndarray
    part: np.ndarray  # partition ordinal of each row

    def __len__(self) -> int:
        return int(self.ts_event.size)


def read_manifest(root: str | Path) -> dict[str, Any]:
    """`manifest.json` of a dataset (`QT-INPUT-001` if missing)."""
    p = Path(root) / "manifest.json"
    if not p.is_file():
        raise QtError("QT-INPUT-001", f"{p}: dataset manifest not found")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise QtError("QT-INPUT-002", f"{p}: invalid JSON: {e}") from None


def read_l2(root: str | Path, instrument_id: int) -> L2Arrays:
    """Every `qt.l2` row of one instrument (`QT-INPUT-002` for an MBO dataset or missing columns)."""
    man = read_manifest(root)
    if man.get("kind") != "l2":
        raise QtError("QT-CONFIG-001", f"{root}: an L2 dataset is required (kind {man.get('kind')!r})")
    parts = sorted(
        (p for p in man.get("partitions", []) if int(p["instrument_id"]) == instrument_id),
        key=lambda p: int(p["session_date"]),
    )
    tables, ords = [], []
    for k, p in enumerate(parts):
        for f in p["files"]:
            t = pq.read_table(Path(root) / f["path"])
            tables.append(t)
            ords.append(np.full(t.num_rows, k, dtype=np.int32))
    if not tables:
        empty = np.zeros(0, dtype=np.int64)
        return L2Arrays(instrument_id, *(empty.copy() for _ in range(12)), np.zeros(0, dtype=np.int32))
    t = pa.concat_tables(tables)

    def ints(name: str, fill: int = 0) -> np.ndarray:
        col = t.column(name)
        if pa.types.is_timestamp(col.type):
            col = col.cast(pa.int64())
        return np.asarray(col.fill_null(fill).to_numpy(zero_copy_only=False), dtype=np.int64)

    kinds = t.column("kind").cast(pa.utf8()).to_pylist()
    code = np.array([KINDS.index(k) for k in kinds], dtype=np.int8)
    return L2Arrays(
        instrument_id,
        ints("ts_event"),
        ints("ts_recv"),
        ints("event_id"),
        ints("sequence"),
        code,
        ints("side"),
        ints("price", -1),
        ints("prev_qty"),
        ints("new_qty"),
        ints("trade_qty"),
        ints("aggressor"),
        ints("flags"),
        np.concatenate(ords),
    )


def feed_gaps(root: str | Path) -> dict[int, list[tuple[int, int]]]:
    """{instrument_id: [(ts_start, ts_end)]} from `data_quality.json` (empty when absent)."""
    p = Path(root) / "data_quality.json"
    if not p.is_file():
        return {}
    doc = json.loads(p.read_text(encoding="utf-8"))
    out: dict[int, list[tuple[int, int]]] = {}
    for g in doc.get("feed_gaps", []) or []:
        out.setdefault(int(g["instrument_id"]), []).append((int(g["ts_start"]), int(g["ts_end"])))
    return out


def instruments(root: str | Path) -> list[dict[str, Any]]:
    """Rows of `instruments.parquet`."""
    p = Path(root) / "instruments.parquet"
    if not p.is_file():
        raise QtError("QT-INPUT-001", f"{p}: instruments table not found")
    rows = pq.read_table(p).to_pylist()
    for r in rows:
        if hasattr(r.get("session_date"), "toordinal"):
            r["session_date"] = (r["session_date"] - datetime(1970, 1, 1).date()).days
    return rows


__all__ = [
    "DAY_NS",
    "KINDS",
    "Instrument",
    "L2Arrays",
    "Series",
    "feed_gaps",
    "instruments",
    "read_l2",
    "read_manifest",
    "series_rows",
    "write_dataset",
]
