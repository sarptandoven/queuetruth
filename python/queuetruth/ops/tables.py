"""Reading queuetruth tables: `read_table` and `fill_liquidity`."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.errors import QtError
from queuetruth.ops._call import _call, _core


def fill_liquidity(fills: str | pa.Table) -> str | pa.Table:
    """Liquidity of shadow fills (audit A5): for a `qt.shadow_fills` reason name, `maker` or `taker`; for a table
    with a `reason` column, the table with a `liquidity` column appended (unchanged when it already has one, as
    schema-1.1 `qt.shadow_fills` do)."""
    if isinstance(fills, str):
        return str(_call("fill_liquidity", fills))
    if "liquidity" in fills.column_names:  # schema 1.1 writes the column itself
        return fills
    reasons = fills.column("reason").to_pylist()
    names = {r: str(_call("fill_liquidity", r)) for r in set(reasons) if r is not None}
    return fills.append_column("liquidity", pa.array([names.get(r) for r in reasons], pa.utf8()))


def read_table(path: str, expect: str | None = None) -> pa.Table:
    """Reads a Parquet table and checks its `qt.schema` metadata (and the columns, for tables known to the core)."""
    p = Path(path)
    if not p.exists():
        raise QtError("QT-INPUT-001", f"{p}: not found")
    try:
        meta = pq.read_schema(p).metadata or {}
    except (OSError, pa.ArrowInvalid) as e:
        raise QtError("QT-INPUT-002", f"{p}: not a Parquet file: {e}") from None
    name = meta.get(b"qt.schema", b"").decode()
    if not name:
        raise QtError("QT-INPUT-002", f"{p}: missing qt.schema metadata (not a queuetruth table)")
    if expect is not None and name != expect:
        raise QtError("QT-INPUT-002", f"{p}: expected table {expect}, found {name}")
    core = _core()
    if name in core.schema_names():
        return _call("read_table", str(p), expect)
    return pq.read_table(p)
