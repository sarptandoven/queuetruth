"""Arrow <-> numpy helpers for the evaluation tables (null masks kept explicit)."""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from queuetruth.errors import QtError
from queuetruth.schemas import DICT_VALUES, SCHEMAS


def require(table: pa.Table, names: list[str], what: str) -> None:
    """Raises `QT-INPUT-002` naming the missing columns."""
    missing = [n for n in names if n not in table.column_names]
    if missing:
        raise QtError("QT-INPUT-002", f"{what}: missing column(s) {', '.join(missing)}")


def has(table: pa.Table, name: str) -> bool:
    """True when the column exists."""
    return name in table.column_names


def strings(table: pa.Table, name: str) -> np.ndarray:
    """utf8 or dictionary column as an object array of str (nulls -> None)."""
    col = table[name]
    if pa.types.is_dictionary(col.type):
        col = col.cast(pa.utf8())
    return np.array(col.to_pylist(), dtype=object)


def floats(table: pa.Table, name: str) -> np.ndarray:
    """Numeric column as float64 with NaN for null."""
    col = table[name]
    if pa.types.is_timestamp(col.type) or pa.types.is_date(col.type):
        col = col.cast(pa.int64()) if pa.types.is_timestamp(col.type) else col.cast(pa.int32())
    return np.asarray(pc.fill_null(col.cast(pa.float64()), np.nan).to_numpy(), dtype=float)


def ints(table: pa.Table, name: str) -> tuple[np.ndarray, np.ndarray]:
    """Integer, timestamp (ns) or date32 (days) column as int64 values and a validity mask (nulls -> 0, False)."""
    col = table[name]
    if pa.types.is_timestamp(col.type):
        col = col.cast(pa.int64())
    elif pa.types.is_date(col.type):
        col = col.cast(pa.int32())
    valid = np.asarray(pc.is_valid(col).to_numpy(zero_copy_only=False), dtype=bool)
    vals = np.asarray(pc.fill_null(col.cast(pa.int64()), 0).to_numpy(), dtype=np.int64)
    return vals, valid


def bools(table: pa.Table, name: str) -> tuple[np.ndarray, np.ndarray]:
    """Boolean column as values and a validity mask (nulls -> False, False)."""
    col = table[name]
    valid = np.asarray(pc.is_valid(col).to_numpy(zero_copy_only=False), dtype=bool)
    vals = np.asarray(pc.fill_null(col, False).to_numpy(zero_copy_only=False), dtype=bool)
    return vals, valid


def dict_array(values: list[str], table: str, column: str) -> pa.DictionaryArray:
    """Dictionary column with the schema's value list as dictionary (stable indices)."""
    allowed = list(DICT_VALUES[table][column])
    index = {v: i for i, v in enumerate(allowed)}
    return pa.DictionaryArray.from_arrays(
        pa.array([index[v] for v in values], type=pa.int8()), pa.array(allowed, type=pa.utf8())
    )


def build(table: str, columns: dict[str, pa.Array | list]) -> pa.Table:
    """Table with the exact schema (types, nullability, metadata) of `SCHEMAS[table]`."""
    schema = SCHEMAS[table]
    arrays = []
    for field in schema:
        v = columns[field.name]
        arrays.append(v if isinstance(v, pa.Array) else pa.array(v, type=field.type))
    return pa.Table.from_arrays(arrays, schema=schema)
