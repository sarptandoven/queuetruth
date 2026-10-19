"""Merge single-day queuetruth dataset directories into one multi-date dataset (SPEC 3.3 layout).

Why: `IngestSpec` takes one `session_date` / `midnight_utc_ns`, and the et MBO CSVs are one file per day, so the two
Nasdaq days are ingested separately. RD-9 (train day one, test day two) and RD-10 (`split auto` across dates) need
one dataset holding both dates. Partitions are disjoint by `date=`, so merging is a directory union plus a rebuilt
`manifest.json`, `instruments.parquet` and `data_quality.json`. Partition files are hard-linked (no extra disk).

If ingest gains a native multi-date path (several `--date`s, `--append`, or a `dataset merge` command), prefer it
and delete this helper.

    python merge_datasets.py --out .realdata/ds/nasdaq-2d .realdata/ds/nasdaq-2019-12-30 .realdata/ds/nasdaq-2020-01-30
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

VERDICT_RANK = {"clean": 0, "warnings": 1, "unusable": 2}
STATUS_RANK = {"real_data": 0, "assumed": 1, "fixture_only": 2, "synthetic_only": 3}
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


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 22):
            h.update(b)
    return h.hexdigest()


def dataset_id(partition_shas: list[str], source_format: str) -> str:
    """SPEC 3.10 qt.dataset_manifest: sha256 of the sorted partition-file sha256 list, newline-joined, then a newline
    and source_format. Cross-check against `qt_schema` if `qt_ingest` exposes its implementation (one-line test:
    rebuild the id of a single-day manifest and compare)."""
    return hashlib.sha256(("\n".join(sorted(partition_shas)) + "\n" + source_format).encode()).hexdigest()


def merge(inputs: list[Path], out: Path) -> dict:
    mans = [json.loads((d / "manifest.json").read_text()) for d in inputs]
    for k in ("kind", "source_format", "venue", "source_venue", "schema_version"):
        vals = {m[k] for m in mans}
        if len(vals) != 1:
            raise SystemExit(f"cannot merge: {k} differs: {sorted(map(str, vals))}")
    seen: set[tuple[int, int]] = set()
    for m in mans:
        for p in m["partitions"]:
            key = (p["session_date"], p["instrument_id"])
            if key in seen:
                raise SystemExit(f"cannot merge: duplicate partition {key}")
            seen.add(key)
    pe: dict[str, int] = {}
    for m in mans:
        for k, v in m["price_exp_by_instrument"].items():
            if pe.setdefault(k, v) != v:
                raise SystemExit(f"cannot merge: instrument {k} has price_exp {pe[k]} and {v} (locate-code clash)")
    out.mkdir(parents=True, exist_ok=False)
    partitions, shas = [], []
    for d, m in zip(inputs, mans, strict=False):
        for p in m["partitions"]:
            for f in p["files"]:
                src, dst = d / f["path"], out / f["path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.link(src, dst)
                shas.append(f["sha256"])
            partitions.append(p)
        for sub in ("trades_source", "l2"):  # sibling partition trees not listed in `partitions`
            if (d / sub).is_dir():
                for src in (d / sub).rglob("*.parquet"):
                    dst = out / src.relative_to(d)
                    if dst.exists():
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.link(src, dst)
    partitions.sort(key=lambda p: (p["session_date"], p["instrument_id"]))
    man = dict(mans[0])
    man["inputs"] = [i for m in mans for i in m["inputs"]]
    man["partitions"] = partitions
    man["price_exp_by_instrument"] = dict(sorted(pe.items(), key=lambda kv: int(kv[0])))
    man["validation_status"] = max((m["validation_status"] for m in mans), key=lambda s: STATUS_RANK.get(s, 9))
    man["created_by"] = mans[0]["created_by"] + " + merge_datasets.py"
    man["dataset_id"] = dataset_id(shas, man["source_format"])
    (out / "manifest.json").write_text(json.dumps(man, indent=1) + "\n")

    tabs = [pq.read_table(d / "instruments.parquet") for d in inputs]
    inst = pa.concat_tables(tabs).combine_chunks()
    order = sorted(
        range(inst.num_rows), key=lambda i: (inst["session_date"][i].as_py(), inst["instrument_id"][i].as_py())
    )
    inst = inst.take(pa.array(order)).replace_schema_metadata(tabs[0].schema.metadata)
    pq.write_table(inst, out / "instruments.parquet", compression="zstd", compression_level=3)

    dqs = [json.loads((d / "data_quality.json").read_text()) for d in inputs if (d / "data_quality.json").exists()]
    if dqs:
        dq = dict(dqs[0])
        for k in DQ_SUM:
            dq[k] = sum(x.get(k, 0) for x in dqs)
        dq["feed_gaps"] = [g for x in dqs for g in x.get("feed_gaps", [])]
        ss: dict[str, int] = {}
        for x in dqs:
            for k, v in x.get("source_specific", {}).items():
                ss[k] = ss.get(k, 0) + v
        dq["source_specific"] = dict(sorted(ss.items()))
        dq["verdict"] = max((x["verdict"] for x in dqs), key=VERDICT_RANK.__getitem__)
        dq["reasons"] = sorted({r for x in dqs for r in x.get("reasons", [])})
        (out / "data_quality.json").write_text(json.dumps(dq, indent=1) + "\n")
    return man


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("inputs", type=Path, nargs="+")
    a = ap.parse_args(argv)
    man = merge(a.inputs, a.out)
    print(json.dumps({"out": str(a.out), "dataset_id": man["dataset_id"], "partitions": len(man["partitions"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
