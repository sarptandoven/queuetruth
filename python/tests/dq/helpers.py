"""Helpers for the `queuetruth dq` tests: copies of the committed synthetic datasets, Python-side corruption of
their partitions (the manifest is refreshed so only the injected defect shows), and a builder of tiny hand-made
datasets."""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import write_table

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "fixtures"
Cols = dict[str, np.ndarray]


def copy_fixture(name: str, dest: Path) -> Path:
    shutil.copytree(FIXTURES / name, dest, ignore=shutil.ignore_patterns("truth", "own_orders", "MANIFEST.sha256"))
    return dest


def read_cols(path: Path) -> tuple[Cols, pa.Schema]:
    t = pq.read_table(path)
    cols: Cols = {}
    for name in t.column_names:
        c = t.column(name)
        if pa.types.is_timestamp(c.type):
            c = c.cast(pa.int64())
        if pa.types.is_dictionary(c.type):
            cols[name] = np.asarray(c.cast(pa.utf8()).to_pylist(), dtype=object)
        else:
            cols[name] = np.asarray(c.to_pylist(), dtype=object) if c.null_count else c.to_numpy().copy()
    return cols, t.schema


def write_cols(cols: Cols, schema: pa.Schema, path: Path) -> None:
    arrays = []
    for f in schema:
        v = cols[f.name]
        if pa.types.is_timestamp(f.type):
            arrays.append(pa.array(np.asarray(v, dtype=np.int64)).cast(f.type))
        elif pa.types.is_dictionary(f.type):
            arrays.append(pa.array(list(v), pa.utf8()).dictionary_encode().cast(f.type))
        else:
            arrays.append(pa.array(list(v) if v.dtype == object else v, f.type))
    write_table(pa.Table.from_arrays(arrays, schema=schema), path)


def part_path(ds: Path, date: str, iid: int, table: str = "mbo") -> Path:
    return ds / table / f"date={date}" / f"instrument_id={iid}" / "part-00000.parquet"


def edit_partition(ds: Path, date: str, iid: int, fn: Callable[[Cols], Cols], *, table: str = "mbo") -> None:
    """Rewrites one partition (or its `trades_source` with `table="trades_source"`) through `fn` and refreshes
    the manifest."""
    p = part_path(ds, date, iid, table)
    cols, schema = read_cols(p)
    write_cols(fn(cols), schema, p)
    refresh_manifest(ds)


def take(cols: Cols, idx: np.ndarray | list[int]) -> Cols:
    idx = np.asarray(idx, dtype=np.int64)
    return {k: v[idx] for k, v in cols.items()}


def insert_rows(cols: Cols, at: int, rows: list[dict[str, Any]]) -> Cols:
    """Inserts `rows` (dicts of field overrides on a copy of record `at - 1`) before record `at`."""
    out = {}
    for k, v in cols.items():
        new = []
        for r in rows:
            new.append(r.get(k, v[at - 1]))
        out[k] = np.concatenate([v[:at], np.asarray(new, dtype=v.dtype), v[at:]])
    return out


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def refresh_manifest(ds: Path) -> None:
    """Recomputes partition rows, hashes, ts range and `dataset_id` after an edit."""
    mp = ds / "manifest.json"
    m = json.loads(mp.read_text())
    shas = []
    for part in m["partitions"]:
        rows = 0
        lo = hi = None
        for f in part["files"]:
            fp = ds / f["path"]
            t = pq.read_table(fp, columns=["ts_event"])
            f["rows"] = t.num_rows
            f["sha256"] = sha(fp)
            shas.append(f["sha256"])
            rows += t.num_rows
            if t.num_rows:
                ts = t.column("ts_event").cast(pa.int64()).to_numpy()
                lo = int(ts.min()) if lo is None else min(lo, int(ts.min()))
                hi = int(ts.max()) if hi is None else max(hi, int(ts.max()))
        part["rows"] = rows
        part["ts_min"], part["ts_max"] = lo, hi
    m["dataset_id"] = hashlib.sha256(("\n".join(sorted(shas)) + "\n" + m["source_format"]).encode()).hexdigest()
    mp.write_text(json.dumps(m, indent=2) + "\n")


def edit_instruments(ds: Path, fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> None:
    p = ds / "instruments.parquet"
    t = pq.read_table(p)
    rows = fn(t.to_pylist())
    write_table(pa.Table.from_pylist(rows, schema=t.schema), p)


# ---- tiny hand-made datasets ------------------------------------------------------------------------------------


def midnight_ny(date: str) -> int:
    """UTC ns of local midnight in New York."""
    d = datetime.date.fromisoformat(date)
    return int(datetime.datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("America/New_York")).timestamp()) * 10**9


def rec(ts: int, action: str, side: str, price: int, size: int, oid: int, seq: int, **kw: Any) -> dict[str, Any]:
    r = {
        "ts_event": ts,
        "ts_recv": ts + 1_000,
        "action": ord(action),
        "side": ord(side),
        "price": price,
        "size": size,
        "order_id": oid,
        "flags": 128,
        "sequence": seq,
        "channel_id": 0,
        "publisher_id": 1,
    }
    r.update(kw)
    return r


def make_dataset(
    root: Path,
    parts: dict[tuple[str, int], list[dict[str, Any]]],
    symbols: dict[int, str],
    *,
    venue: str = "nasdaq",
    source_format: str = "synthetic",
    tick: int = 10_000_000,
) -> Path:
    """Writes a dataset directory: `parts` maps (date, instrument_id) to records (`rec`); instruments get the
    Nasdaq session (09:30-16:00 New York)."""
    schema = SCHEMAS["qt.mbo"]
    inst_rows = []
    manifest_parts = []
    shas = []
    for (date, iid), records in sorted(parts.items()):
        p = part_path(root, date, iid)
        cols = {f.name: np.asarray([r[f.name] if f.name != "instrument_id" else iid for r in records]) for f in schema}
        if not records:
            cols = {f.name: np.zeros(0, dtype=np.int64) for f in schema}
        arrays = []
        for f in schema:
            v = cols[f.name]
            if pa.types.is_timestamp(f.type):
                arrays.append(pa.array(v.astype(np.int64)).cast(f.type))
            else:
                arrays.append(pa.array(v.tolist(), f.type))
        entry = write_table(pa.Table.from_arrays(arrays, schema=schema), p)
        shas.append(entry["sha256"])
        ts = [r["ts_event"] for r in records]
        manifest_parts.append(
            {
                "instrument_id": iid,
                "session_date": (datetime.date.fromisoformat(date) - datetime.date(1970, 1, 1)).days,
                "files": [{"path": str(p.relative_to(root)), "rows": len(records), "sha256": entry["sha256"]}],
                "rows": len(records),
                "ts_min": min(ts) if ts else None,
                "ts_max": max(ts) if ts else None,
            }
        )
        inst_rows.append(
            {
                "instrument_id": iid,
                "session_date": datetime.date.fromisoformat(date),
                "symbol": symbols[iid],
                "venue": venue,
                "source_venue": venue,
                "price_exp": -9,
                "tick_size": tick,
                "lot_size": 1,
                "size_exp": 0,
                "contract_multiplier": 1.0,
                "currency": "USD",
                "session_tz": "America/New_York",
                "midnight_utc_ns": midnight_ny(date),
                "open_s": 34_200,
                "close_s": 57_600,
                "source": source_format,
            }
        )
    write_table(pa.Table.from_pylist(inst_rows, schema=SCHEMAS["qt.instruments"]), root / "instruments.parquet")
    manifest = {
        "schema": "qt.dataset_manifest",
        "schema_version": 1,
        "dataset_id": hashlib.sha256(("\n".join(sorted(shas)) + "\n" + source_format).encode()).hexdigest(),
        "kind": "mbo",
        "source_format": source_format,
        "venue": venue,
        "source_venue": venue,
        "inputs": [],
        "partitions": manifest_parts,
        "price_exp_by_instrument": {str(i): -9 for i in symbols},
        "created_by": "queuetruth tests",
        "validation_status": "fixture_only",
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return root


def check(rep: Any, cid: str) -> Any:
    """The check `cid` of a report."""
    return next(c for c in rep.checks if c.id == cid)


def evidence(c: Any, name: str) -> Any:
    return next((e for e in c.evidence if e.name == name), None)
