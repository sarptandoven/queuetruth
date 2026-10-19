"""Parquet writing with the settings of SPEC 0.1 (ZSTD level 3, row groups of at most 1,048,576 rows, dictionary
encoding only for utf8 / dictionary columns) and sha256 helpers, for every Python-written table.

pyarrow cannot set the Parquet `created_by` field; files written here carry pyarrow's own value.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

MAX_ROW_GROUP_ROWS = 1_048_576


def sha256_file(path: str | Path) -> str:
    """Lower-case hex sha256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dictionary_columns(schema: pa.Schema) -> list[str]:
    out = []
    for f in schema:
        t = f.type
        if pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_dictionary(t):
            out.append(f.name)
        elif pa.types.is_list(t) and pa.types.is_string(t.value_type):
            out.append(f.name)
    return out


def write_table(table: pa.Table, path: str | Path, schema: pa.Schema | None = None) -> dict[str, Any]:
    """Writes `table` (cast to `schema` when given, which must carry `qt.schema` metadata) and returns the `Files`
    entry `{name, rows, bytes, sha256}` with `name` = the file name."""
    if schema is not None:
        table = table.cast(schema)
    meta = table.schema.metadata or {}
    if b"qt.schema" not in meta:
        raise ValueError("write_table: schema lacks qt.schema metadata")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        p,
        compression="zstd",
        compression_level=3,
        row_group_size=MAX_ROW_GROUP_ROWS,
        use_dictionary=_dictionary_columns(table.schema),
        write_statistics=True,
    )
    return {"name": p.name, "rows": table.num_rows, "bytes": p.stat().st_size, "sha256": sha256_file(p)}
