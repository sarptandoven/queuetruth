"""Capture directory layout and the `livefill.bitstamp_capture` Parquet table.

A capture directory holds:

- `messages/part-NNNNN.parquet`: every received frame and every REST snapshot, one row each, in arrival order
  (`seq` 0, 1, 2, ... over the whole capture). Parts are written atomically (temporary file, then rename), so a
  killed daemon loses at most the rows buffered since the last flush.
- `gaps.jsonl`: one JSON object per detected gap, appended and flushed when the gap is seen.
- `capture_meta.json`: markets, channels, counters, gaps and segments; rewritten at every flush (`complete` is
  false until the capture closes). Its `markets` list fixes the instrument ids of `queuetruth ingest` (1, 2, ...).

Columns (`raw` keeps the frame text exactly as received; the other columns index it):
`seq`, `recv_ns` (wall clock), `mono_ns` (monotonic clock), `conn` (connection number), `segment`, `kind` (`ws`,
`rest_book`, `stale_rest_book`, `marker`, `duplicate`), `channel`, `market`, `event`, `event_id`, `pre_event_id`,
`microtimestamp`, `order_id`, `trade_id`, `buy_order_id`, `sell_order_id`, `raw`.

Reading. `iter_capture` streams rows in `seq` order, batch by batch, reading only the requested columns and keeping
only the rows a filter selects, so readers never hold a whole capture; `read_capture` collects the same rows into one
table for small selections.
"""

from __future__ import annotations

import functools
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.errors import QtError

SCHEMA_NAME = "livefill.bitstamp_capture"
SCHEMA_VERSION = "1"
META_FILE = "capture_meta.json"
GAPS_FILE = "gaps.jsonl"
PARTS_DIR = "messages"
BATCH_ROWS = 8192

CAPTURE_SCHEMA = pa.schema(
    [
        pa.field("seq", pa.int64(), nullable=False),
        pa.field("recv_ns", pa.int64(), nullable=False),
        pa.field("mono_ns", pa.int64(), nullable=False),
        pa.field("conn", pa.int32(), nullable=False),
        pa.field("segment", pa.int32(), nullable=False),
        pa.field("kind", pa.string(), nullable=False),
        pa.field("channel", pa.string()),
        pa.field("market", pa.string()),
        pa.field("event", pa.string()),
        pa.field("event_id", pa.string()),
        pa.field("pre_event_id", pa.string()),
        pa.field("microtimestamp", pa.int64()),
        pa.field("order_id", pa.int64()),
        pa.field("trade_id", pa.int64()),
        pa.field("buy_order_id", pa.int64()),
        pa.field("sell_order_id", pa.int64()),
        pa.field("raw", pa.large_string(), nullable=False),
    ],
    metadata={b"livefill.schema": SCHEMA_NAME.encode(), b"livefill.schema_version": SCHEMA_VERSION.encode()},
)
COLUMNS = [f.name for f in CAPTURE_SCHEMA]


def write_json_atomic(path: Path, doc: Any) -> None:
    """Writes `doc` as indented JSON through a temporary file and a rename."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n")
    os.replace(tmp, path)


class PartWriter:
    """Buffers capture rows and writes them as numbered Parquet parts (zstd), atomically."""

    def __init__(self, out_dir: Path, flush_rows: int = 20_000, flush_s: float = 10.0) -> None:
        if flush_rows < 1 or flush_s <= 0:
            raise QtError("QT-CONFIG-001", "flush_rows must be >= 1 and flush_s > 0")
        self.dir = Path(out_dir) / PARTS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(self.dir.glob("part-*.parquet"))
        if existing:
            raise QtError("QT-USAGE-001", f"{self.dir} already holds capture parts; capture into a new directory")
        self.flush_rows = flush_rows
        self.flush_s = flush_s
        self.cols: dict[str, list[Any]] = {c: [] for c in COLUMNS}
        self.n_parts = 0
        self.rows_written = 0
        self.last_flush = time.monotonic()

    @property
    def buffered(self) -> int:
        return len(self.cols["seq"])

    def append(self, row: dict[str, Any]) -> None:
        for c in COLUMNS:
            self.cols[c].append(row.get(c))

    def due(self) -> bool:
        return self.buffered >= self.flush_rows or (
            self.buffered > 0 and time.monotonic() - self.last_flush >= self.flush_s
        )

    def flush(self) -> Path | None:
        self.last_flush = time.monotonic()
        if not self.buffered:
            return None
        table = pa.Table.from_pydict(self.cols, schema=CAPTURE_SCHEMA)
        path = self.dir / f"part-{self.n_parts:05d}.parquet"
        tmp = path.with_name(path.name + ".tmp")
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, path)
        self.n_parts += 1
        self.rows_written += table.num_rows
        self.cols = {c: [] for c in COLUMNS}
        return path


def read_meta(capture_dir: str | Path) -> dict[str, Any]:
    """`capture_meta.json` of a capture directory."""
    p = Path(capture_dir) / META_FILE
    try:
        doc = json.loads(p.read_text())
    except FileNotFoundError as e:
        raise QtError("QT-INPUT-001", f"{p}: not found (not a livefill capture directory)") from e
    except json.JSONDecodeError as e:
        raise QtError("QT-INPUT-002", f"{p}: not JSON ({e.msg})") from e
    if doc.get("schema") != "livefill.capture_meta":
        raise QtError("QT-INPUT-002", f"{p}: schema is {doc.get('schema')!r}, expected 'livefill.capture_meta'")
    return doc


def capture_parts(capture_dir: str | Path) -> list[Path]:
    """The capture's Parquet parts in order; parts with another schema fail with `QT-INPUT-002` (only the footers are
    read)."""
    d = Path(capture_dir) / PARTS_DIR
    parts = sorted(d.glob("part-*.parquet"))
    if not parts:
        raise QtError("QT-INPUT-001", f"{d}: no capture parts (part-*.parquet)")
    for p in parts:
        sch = pq.read_schema(p)
        md = sch.metadata or {}
        if md.get(b"livefill.schema") != SCHEMA_NAME.encode() or sch.names != COLUMNS:
            raise QtError("QT-INPUT-002", f"{p}: not a {SCHEMA_NAME} table")
    return parts


@functools.lru_cache(maxsize=4096)
def _segment_range(path: str, mtime_ns: int) -> tuple[int, int] | None:
    """(min, max) `segment` of a part from its row-group statistics (None when absent)."""
    md = pq.ParquetFile(path).metadata
    col = COLUMNS.index("segment")
    lo = hi = None
    for g in range(md.num_row_groups):
        st = md.row_group(g).column(col).statistics
        if st is None or not st.has_min_max:
            return None
        lo = st.min if lo is None else min(lo, st.min)
        hi = st.max if hi is None else max(hi, st.max)
    return None if lo is None else (int(lo), int(hi))


def _filter_columns(filters: Any) -> list[str]:
    """Column names a DNF filter (a list of tuples, or a list of lists of tuples) refers to."""
    if not filters:
        return []
    groups = filters if isinstance(filters[0], list) else [filters]
    return [t[0] for g in groups for t in g]


def iter_capture(
    capture_dir: str | Path,
    columns: list[str] | None = None,
    filters: Any = None,
    segment: int | None = None,
    batch_rows: int = BATCH_ROWS,
) -> Iterator[pa.RecordBatch]:
    """Capture rows in `seq` order as record batches: all columns or only `columns` (`seq` is always read), only the
    rows matching the pyarrow `filters` (DNF tuples, as `pyarrow.parquet.read_table` takes them). With `segment`,
    parts whose row-group statistics exclude it are skipped unread. At most one batch of `batch_rows` unfiltered rows
    is decoded at a time, so memory does not grow with the capture."""
    cols = COLUMNS if columns is None else list(dict.fromkeys(["seq", *columns]))
    schema = pa.schema([CAPTURE_SCHEMA.field(c) for c in cols], CAPTURE_SCHEMA.metadata)
    expr = pq.filters_to_expression(filters) if filters else None
    read_cols = list(dict.fromkeys([*cols, *_filter_columns(filters)]))
    read_schema = pa.schema([CAPTURE_SCHEMA.field(c) for c in read_cols], CAPTURE_SCHEMA.metadata)
    last = -1
    for p in capture_parts(capture_dir):
        if segment is not None:
            r = _segment_range(str(p), p.stat().st_mtime_ns)
            if r is not None and not (r[0] <= segment <= r[1]):
                continue
        for b in pq.ParquetFile(p).iter_batches(batch_size=batch_rows, columns=read_cols, use_threads=False):
            t = pa.Table.from_batches([b]).cast(read_schema)
            if expr is not None:
                t = t.filter(expr)
            if read_cols != cols:
                t = t.select(cols).cast(schema)
            if t.num_rows == 0:
                continue
            seqs = t.column("seq")
            first = seqs[0].as_py()
            if first <= last:
                raise QtError("QT-INPUT-002", f"{p}: rows are not in seq order (seq {first} after {last})")
            last = seqs[-1].as_py()
            yield from t.combine_chunks().to_batches()


def read_capture(
    capture_dir: str | Path,
    columns: list[str] | None = None,
    filters: Any = None,
    segment: int | None = None,
) -> pa.Table:
    """Capture rows in `seq` order as one table (see `iter_capture`; fine for small captures and selections)."""
    cols = COLUMNS if columns is None else list(dict.fromkeys(["seq", *columns]))
    schema = pa.schema([CAPTURE_SCHEMA.field(c) for c in cols], CAPTURE_SCHEMA.metadata)
    return pa.Table.from_batches(list(iter_capture(capture_dir, columns, filters, segment)), schema=schema)
