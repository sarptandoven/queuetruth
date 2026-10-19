"""Shared conventions of the learned hazard model (SPEC 5.5).

Cause codes: 0 = censor, 1 = fill, 2 = cancel (the classifier's classes are 0 = no event in the interval, 1 = fill,
2 = cancel). Status at a horizon `tau` for an order-landmark with `(e, c)`: an event of cause fill/cancel is known
iff `e <= tau`; `c = censor` is known event-free through `tau` iff `e >= tau` (a censoring exactly at `tau`, e.g. the
`h_max` cap, means the order survived to `tau`), and unknown ("censored before tau") iff `e < tau`.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa

from queuetruth.errors import QtError
from queuetruth.schemas.arrow import TABLES

CENSOR, FILL, CANCEL = 0, 1, 2
CAUSE_CODES = {"censor": CENSOR, "fill": FILL, "cancel": CANCEL}
#: Fixed (non-feature) columns of `qt.features`.
FIXED_FEATURE_COLUMNS = tuple(c[0] for c in TABLES["qt.features"])
#: Columns that are never model inputs even when present (`midnight_utc_ns`: optional local-midnight column for the
#: climatology's time-of-day strata).
NON_FEATURE_COLUMNS = frozenset(FIXED_FEATURE_COLUMNS) | {"split", "midnight_utc_ns"}
PRIVILEGED_PREFIX = "p_"
NS_PER_S = 1_000_000_000
NS_PER_DAY = 86_400 * NS_PER_S
HALF_HOUR_NS = 1_800 * NS_PER_S
G_FLOOR = 0.05
P_FLOOR = 1e-12


def input_error(message: str, **details: Any) -> QtError:
    """`QT-INPUT-002` (input format or content mismatch)."""
    return QtError("QT-INPUT-002", message, details=details or None)


def config_error(message: str, **details: Any) -> QtError:
    """`QT-CONFIG-001` (invalid spec or config value)."""
    return QtError("QT-CONFIG-001", message, details=details or None)


def require_columns(table: pa.Table, names: list[str] | tuple[str, ...], what: str) -> None:
    """Raises `QT-INPUT-002` naming the first missing column."""
    for n in names:
        if n not in table.column_names:
            raise input_error(f"{what}: missing column {n!r}", column=n)


def cause_codes(values: Any) -> np.ndarray:
    """Cause column (dictionary/utf8 Arrow column, strings or integer codes) -> int8 codes 0/1/2."""
    if isinstance(values, (pa.ChunkedArray, pa.Array)):
        arr = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
        if arr.null_count:
            raise input_error("cause: null values")
        if pa.types.is_dictionary(arr.type):
            dict_vals = arr.dictionary.to_pylist()
            lut = np.array([_code(v) for v in dict_vals] or [0], dtype=np.int8)
            idx = arr.indices.to_numpy(zero_copy_only=False).astype(np.int64)
            return lut[idx]
        if pa.types.is_integer(arr.type):
            return _int_codes(arr.to_numpy(zero_copy_only=False))
        values = arr.to_pylist()
    a = np.asarray(values)
    if a.dtype.kind in "iu":
        return _int_codes(a)
    uniq, inv = np.unique(a.astype(str), return_inverse=True)
    lut = np.array([_code(v) for v in uniq] or [0], dtype=np.int8)
    return lut[inv.reshape(-1)] if len(a) else np.zeros(0, dtype=np.int8)


def _code(v: Any) -> int:
    try:
        return CAUSE_CODES[str(v)]
    except KeyError:
        raise input_error(f"cause: unknown value {v!r} (expected fill, cancel or censor)") from None


def _int_codes(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.int64)
    if a.size and (a.min() < 0 or a.max() > 2):
        raise input_error("cause codes must be 0 (censor), 1 (fill) or 2 (cancel)")
    return a.astype(np.int8)


def int64_column(table: pa.Table, name: str) -> np.ndarray:
    """A non-null integer / timestamp / date column as int64 numpy (timestamps in ns, dates in days)."""
    col = table.column(name).combine_chunks()
    if col.null_count:
        raise input_error(f"column {name!r} has null values", column=name)
    t = col.type
    if pa.types.is_timestamp(t) or pa.types.is_date32(t):
        col = col.cast(pa.int64() if pa.types.is_timestamp(t) else pa.int32())
    return np.asarray(col.to_numpy(zero_copy_only=False), dtype=np.int64)


def float_matrix(table: pa.Table, names: list[str]) -> np.ndarray:
    """Columns as a float64 matrix (nulls -> NaN), shape (rows, len(names))."""
    out = np.empty((table.num_rows, len(names)), dtype=np.float64)
    for j, n in enumerate(names):
        col = table.column(n).combine_chunks()
        if not (pa.types.is_integer(col.type) or pa.types.is_floating(col.type) or pa.types.is_boolean(col.type)):
            raise input_error(f"feature column {n!r} must be numeric, got {col.type}", column=n)
        out[:, j] = col.cast(pa.float64()).to_numpy(zero_copy_only=False)
    return out


def check_grid(grid_ns: list[int] | tuple[int, ...] | np.ndarray) -> np.ndarray:
    """Validates a horizon grid: non-empty, integer, strictly increasing, all > 0."""
    g = np.asarray(list(grid_ns), dtype=np.int64)
    if g.size == 0:
        raise config_error("grid_ns must not be empty")
    if g[0] <= 0 or np.any(np.diff(g) <= 0):
        raise config_error("grid_ns must be strictly increasing and > 0", grid_ns=[int(x) for x in g])
    return g


def outcome_arrays(table: pa.Table) -> tuple[np.ndarray, np.ndarray]:
    """`(event_ns int64, cause int8)` of a features-like table; negative `event_ns` is an input error."""
    require_columns(table, ("event_ns", "cause"), "features")
    e = int64_column(table, "event_ns")
    if e.size and e.min() < 0:
        raise input_error("event_ns must be >= 0")
    return e, cause_codes(table.column("cause"))


def model_feature_names(table: pa.Table | pa.Schema, privileged: bool) -> list[str]:
    """Model inputs in table order: `side_sign`, then every non-fixed column; `p_*` columns only if `privileged`."""
    names = table.column_names if isinstance(table, pa.Table) else table.names
    out = ["side_sign"] if "side_sign" in names else []
    for n in names:
        if n in NON_FEATURE_COLUMNS:
            continue
        if n.startswith(PRIVILEGED_PREFIX) and not privileged:
            continue
        out.append(n)
    if not out:
        raise input_error("features: no feature columns")
    return out


def known_status(event_ns: np.ndarray, cause: np.ndarray, tau: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """At horizon `tau`: `(event_by_tau, survivor, censored_before)` boolean masks (module docstring rules)."""
    is_cens = cause == CENSOR
    event_by = (~is_cens) & (event_ns <= tau)
    censored_before = is_cens & (event_ns < tau)
    survivor = ~(event_by | censored_before)
    return event_by, survivor, censored_before
