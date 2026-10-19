"""Null-safe column extraction from pyarrow tables into numpy arrays and Python lists."""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc


def _col(t: pa.Table, name: str) -> pa.ChunkedArray:
    c = t.column(name)
    if pa.types.is_dictionary(c.type):
        c = pc.cast(c, pa.string())
    return c


def f64(t: pa.Table, name: str) -> np.ndarray:
    """Float64 values, NaN for nulls."""
    c = _col(t, name)
    if pa.types.is_timestamp(c.type):
        c = pc.cast(c, pa.int64())
    if pa.types.is_boolean(c.type):
        c = pc.cast(c, pa.int8())
    return np.asarray(pc.cast(c, pa.float64()).to_numpy(zero_copy_only=False), dtype=np.float64)


def i64(t: pa.Table, name: str) -> tuple[np.ndarray, np.ndarray]:
    """Int64 values (0 for nulls) and the validity mask; timestamps as ns."""
    c = _col(t, name)
    if pa.types.is_date32(c.type):
        c = pc.cast(c, pa.int32())
    if pa.types.is_timestamp(c.type) or not pa.types.is_integer(c.type):
        c = pc.cast(c, pa.int64())
    valid = np.asarray(pc.is_valid(c).to_numpy(zero_copy_only=False), dtype=bool)
    vals = np.asarray(pc.cast(pc.fill_null(c, 0), pa.int64()).to_numpy(zero_copy_only=False), dtype=np.int64)
    return vals, valid


def boolean(t: pa.Table, name: str) -> tuple[np.ndarray, np.ndarray]:
    """Bool values (False for nulls) and the validity mask."""
    c = _col(t, name)
    valid = np.asarray(pc.is_valid(c).to_numpy(zero_copy_only=False), dtype=bool)
    vals = np.asarray(pc.fill_null(c, False).to_numpy(zero_copy_only=False), dtype=bool)
    return vals, valid


def strs(t: pa.Table, name: str) -> np.ndarray:
    """String values as a numpy object array (None for nulls)."""
    return np.asarray(_col(t, name).to_pylist(), dtype=object)


def pylist(t: pa.Table) -> list[dict]:
    """Rows as dicts with timestamp columns as int ns (no datetime precision loss) and dictionaries as strings."""
    arrays, names = [], []
    for f, c in zip(t.schema, t.columns, strict=True):
        if pa.types.is_timestamp(f.type):
            c = pc.cast(c, pa.int64())
        elif pa.types.is_dictionary(f.type):
            c = pc.cast(c, pa.string())
        arrays.append(c)
        names.append(f.name)
    return pa.Table.from_arrays(arrays, names=names).to_pylist()
