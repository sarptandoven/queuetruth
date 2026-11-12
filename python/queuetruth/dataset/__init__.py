"""One way to open, read, check and write a queuetruth dataset (plan section 5.3 step 3).

A dataset is a directory with a `manifest.json` (SPEC 3.3, 3.8): an order-level (MBO) or price-level (L2) dataset,
a prediction-market dataset, or a canonical live or simulator log.

```python
from queuetruth.dataset import open_dataset

ds = open_dataset("synth")
ds.kind                      # DatasetKind.MBO
ds.manifest.dataset_id       # the SPEC 3.10 identity
[i.symbol for i in ds.instruments]
ds.days                      # ["2024-01-02", ...]
ds.table("mbo", columns=["ts_event", "price"], filter={"instrument_id": 1})
ds.verify().raise_for_problems()
```

Writers use `DatasetWriter`: it writes the tables, records their hashes and writes the manifest last, atomically.
Errors are `QtError`s: `QT-INPUT-001` for a missing directory or file, `QT-INPUT-002` for a file that does not
parse or does not belong, each with the path and a hint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from queuetruth.dataset._bounded import MAX_RATIO, SLACK, expansion_limit
from queuetruth.dataset._bounded import read_parquet as read_parquet_bounded
from queuetruth.dataset._dataset import (
    DATA_QUALITY_FILE,
    EVENTS_FILE,
    ORDERS_FILE,
    PREDICTION_MARKET_VENUES,
    SIM_PREFIX,
    TABLES,
    Dataset,
    DatasetKind,
    TableDef,
    Verification,
    check_table_schema,
    open_dataset,
    table_schema_name,
)
from queuetruth.dataset._instruments import INSTRUMENT_TYPES, INSTRUMENTS_FILE, Instrument, Session, instrument_rows
from queuetruth.dataset._manifest import (
    DATASET_SCHEMA,
    LIVE_SCHEMA,
    MANIFEST_FILE,
    PRICE_EXP_MAX,
    PRICE_EXP_MIN,
    Manifest,
    Partition,
    PartitionFile,
    check_rel_path,
    dataset_id,
    date_str,
    day_number,
    read_manifest_doc,
)
from queuetruth.dataset._writer import DatasetWriter, json_text, write_text_atomic


def read_manifest(path: str | Path, *, missing_ok: bool = True) -> dict[str, Any]:
    """The `manifest.json` of a directory as a dict (`{}` when absent and `missing_ok`); `QT-INPUT-002` when it does
    not parse."""
    return read_manifest_doc(path, missing_ok=missing_ok) or {}


__all__ = [
    "DATASET_SCHEMA",
    "DATA_QUALITY_FILE",
    "EVENTS_FILE",
    "INSTRUMENTS_FILE",
    "INSTRUMENT_TYPES",
    "LIVE_SCHEMA",
    "MANIFEST_FILE",
    "MAX_RATIO",
    "ORDERS_FILE",
    "PREDICTION_MARKET_VENUES",
    "PRICE_EXP_MAX",
    "PRICE_EXP_MIN",
    "SIM_PREFIX",
    "SLACK",
    "TABLES",
    "Dataset",
    "DatasetKind",
    "DatasetWriter",
    "Instrument",
    "Manifest",
    "Partition",
    "PartitionFile",
    "Session",
    "TableDef",
    "Verification",
    "check_rel_path",
    "check_table_schema",
    "dataset_id",
    "date_str",
    "day_number",
    "expansion_limit",
    "instrument_rows",
    "json_text",
    "open_dataset",
    "read_manifest",
    "read_manifest_doc",
    "read_parquet_bounded",
    "table_schema_name",
    "write_text_atomic",
]
