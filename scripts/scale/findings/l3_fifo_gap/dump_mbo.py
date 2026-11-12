"""Write a queuetruth dataset's MBO partitions as the harness's 48-byte records, one file per instrument.

Usage (repository venv): dump_mbo.py <dataset_dir> <out_dir>

Prints one JSON line per instrument: its id, its `instruments.parquet` row, the record count and the file written.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

ds, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
inst = pq.read_table(ds / "instruments.parquet").to_pylist()
man = json.loads((ds / "manifest.json").read_text())
dt = np.dtype(
    [
        ("ts", "<i8"),
        ("px", "<i8"),
        ("size", "<u8"),
        ("oid", "<u8"),
        ("seq", "<u8"),
        ("action", "u1"),
        ("side", "u1"),
        ("flags", "u1"),
        ("pad", "V5"),
    ]
)
for p in man["partitions"]:
    iid = p["instrument_id"]
    t = pa.concat_tables([pq.read_table(ds / f["path"]) for f in p["files"]])
    c = {k: t.column(k).to_numpy(zero_copy_only=False) for k in t.column_names}
    a = np.zeros(t.num_rows, dt)
    a["ts"] = c["ts_event"]
    a["px"] = c["price"]
    a["size"] = c["size"]
    a["oid"] = c["order_id"]
    a["seq"] = c["sequence"]
    a["action"] = c["action"]
    a["side"] = c["side"]
    a["flags"] = c["flags"]
    f = out / f"{iid}.bin"
    a.tofile(f)
    meta = next((i for i in inst if i["instrument_id"] == iid), {})
    print(
        json.dumps(
            {"instrument_id": iid, "meta": {k: str(v) for k, v in meta.items()}, "rows": t.num_rows, "path": str(f)}
        )
    )
