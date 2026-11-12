"""Merge single-day dataset directories into one multi-date dataset (SPEC 3.3 layout).

`IngestSpec` takes one `session_date`, and many sources are one file per day, so the days are ingested one by one.
Partitions are disjoint by `date=`, so merging is a directory union plus a rebuilt `manifest.json`,
`instruments.parquet` and `data_quality.json`. Partition files are hard-linked when the file system allows it (no
extra disk) and copied otherwise.

`queuetruth data add` merges its days with `merge`; `scripts/realdata/merge_datasets.py` is a thin command line over
it, kept for the scripts that call it.
"""

from __future__ import annotations

import datetime as dt
import json
import warnings
from pathlib import Path
from typing import Any

from queuetruth.dataset import DatasetWriter, open_dataset
from queuetruth.dataset import dataset_id as _dataset_id
from queuetruth.errors import QtError

VERDICT_RANK = {"clean": 0, "warnings": 1, "unusable": 2}
STATUS_RANK = {"real_data": 0, "assumed": 1, "fixture_only": 2, "synthetic_only": 3}
#: `qt.data_quality` counters that add up over days.
DQ_SUM = [
    "records",
    "maybe_bad_book_records",
    "bad_ts_recv_records",
    "snapshot_records",
    "pre_snapshot_rows_skipped",
    "unknown_order_refs",
    "duplicate_adds",
    "unexpected_modifies",
    "sequence_gaps",
    "sequence_regressions",
    "out_of_order_ts_event",
    "crossed_books_after_last",
    "hidden_prints",
    "off_price_executions",
]
#: Partition trees next to `partitions` that the manifest does not list.
SIBLING_TREES = ("trades_source", "l2")


def dataset_id(partition_shas: list[str], source_format: str) -> str:
    """Deprecated: use `queuetruth.dataset.dataset_id`."""
    warnings.warn(
        "queuetruth.data.merge.dataset_id is deprecated; use queuetruth.dataset.dataset_id",
        DeprecationWarning,
        stacklevel=2,
    )
    return _dataset_id(partition_shas, source_format)


def _cannot(msg: str, hint: str | None = None) -> QtError:
    return QtError("QT-INPUT-002", f"cannot merge: {msg}", hint)


def merge(inputs: list[Path], out: Path, *, created_by: str = "merge_datasets.py") -> dict[str, Any]:
    """Merges the datasets `inputs` into the new directory `out` and returns its manifest. `QT-INPUT-002` when they
    differ in kind, format, venue or schema, hold the same (date, instrument) partition, or give one instrument id
    two price exponents; `created_by` is appended to the first manifest's `created_by`."""
    inputs = [Path(d) for d in inputs]
    mans = [open_dataset(d).manifest.to_dict() for d in inputs]
    for k in ("kind", "source_format", "venue", "source_venue", "schema_version"):
        vals = {str(m.get(k)) for m in mans}
        if len(vals) != 1:
            raise _cannot(f"{k} differs: {sorted(vals)}", "one dataset holds one format and one venue")
    seen: set[tuple[int, int]] = set()
    for m in mans:
        for p in m["partitions"]:
            key = (p["session_date"], p["instrument_id"])
            if key in seen:
                day = (dt.date(1970, 1, 1) + dt.timedelta(days=int(key[0]))).isoformat()
                raise _cannot(
                    f"{day} (instrument_id {key[1]}) is in two inputs",
                    "a day is in two inputs, or already in the dataset; add each day once",
                )
            seen.add(key)
    pe: dict[str, int] = {}
    for m in mans:
        for k, v in m["price_exp_by_instrument"].items():
            if pe.setdefault(k, v) != v:
                raise _cannot(f"instrument {k} has price_exp {pe[k]} and {v} (locate-code clash)")
    w = DatasetWriter(out, exist_ok=False)
    partitions, shas = [], []
    for d, m in zip(inputs, mans, strict=True):
        for p in m["partitions"]:
            for f in p["files"]:
                w.copy(d / f["path"], f["path"], link=True)
                shas.append(f["sha256"])
            partitions.append(p)
        for sub in SIBLING_TREES:
            if (d / sub).is_dir():
                for src in sorted((d / sub).rglob("*.parquet")):
                    rel = src.relative_to(d)
                    if not (out / rel).exists():
                        w.copy(src, rel, link=True)
    partitions.sort(key=lambda p: (p["session_date"], p["instrument_id"]))
    man = dict(mans[0])
    man["inputs"] = [i for m in mans for i in m["inputs"]]
    man["partitions"] = partitions
    man["price_exp_by_instrument"] = dict(sorted(pe.items(), key=lambda kv: int(kv[0])))
    man["validation_status"] = max((m["validation_status"] for m in mans), key=lambda s: STATUS_RANK.get(s, 9))
    first = str(mans[0]["created_by"])
    man["created_by"] = first if first.endswith(f" + {created_by}") else f"{first} + {created_by}"
    man["dataset_id"] = _dataset_id(shas, man["source_format"])
    vintage = [v for m in mans for v in (m.get("source_vintage") or [])]
    if vintage:
        man["source_vintage"] = vintage
    attributions = [a for m in mans for a in (m.get("attribution") or [])]
    if attributions:
        man["attribution"] = [dict(t) for t in dict.fromkeys(tuple(sorted(a.items())) for a in attributions)]

    import pyarrow as pa
    import pyarrow.parquet as pq

    tabs = [pq.read_table(d / "instruments.parquet") for d in inputs]
    inst = pa.concat_tables(tabs).combine_chunks()
    order = sorted(
        range(inst.num_rows), key=lambda i: (inst["session_date"][i].as_py(), inst["instrument_id"][i].as_py())
    )
    inst = inst.take(pa.array(order, type=pa.int64())).replace_schema_metadata(tabs[0].schema.metadata)
    pq.write_table(inst, out / "instruments.parquet", compression="zstd", compression_level=3)
    w.record("instruments.parquet")

    dqs = [
        json.loads((d / "data_quality.json").read_text(encoding="utf-8"))
        for d in inputs
        if (d / "data_quality.json").exists()
    ]
    if dqs:
        dq = dict(dqs[0])
        for k in DQ_SUM:
            dq[k] = sum(x.get(k, 0) for x in dqs)
        dq["feed_gaps"] = [g for x in dqs for g in x.get("feed_gaps", [])]
        halts = [h for x in dqs for h in x.get("trading_halts", [])]
        if halts:
            dq["trading_halts"] = halts
        ss: dict[str, int] = {}
        for x in dqs:
            for k, v in x.get("source_specific", {}).items():
                ss[k] = ss.get(k, 0) + v
        dq["source_specific"] = dict(sorted(ss.items()))
        dq["verdict"] = max((x["verdict"] for x in dqs), key=VERDICT_RANK.__getitem__)
        dq["reasons"] = sorted({r for x in dqs for r in x.get("reasons", [])})
        w.write_json("data_quality.json", dq, indent=1)
    w.finalize(man, indent=1)
    return man


__all__ = ["DQ_SUM", "STATUS_RANK", "VERDICT_RANK", "dataset_id", "merge"]
