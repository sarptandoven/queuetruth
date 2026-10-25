"""SPEC 7.2 cross-platform tolerance for re-executed outputs.

Determinism is promised byte for byte for the same inputs, spec and version on one platform. Across platforms SPEC 7.2
allows values computed with `powf`, `ln` or `exp` to differ by at most 4 ulp, and keeps integer outputs identical.
`verify-cert --rerun` therefore compares an output that is not byte-identical once more by value:

- JSON: the same structure, keys, strings, booleans, nulls and integers; floating-point numbers within 4 ulp
  (`timings` dropped and re-executed paths mapped back first, as for the digest).
- Parquet: the same schema (names, types, nullability and key-value metadata, mapped back), the same row count and
  null positions, every non-floating column equal, every `float32` / `float64` column within 4 ulp (NaN equals NaN).
  This also absorbs a different `created_by` of the writing library, which is file metadata, not data.

Any other file (HTML, npz, pickles) must be byte-identical. A file that passes only this comparison is reported as
`equivalent`, never as identical.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any

from queuetruth.cert.hashing import map_bytes, map_strings, strip_volatile

#: Largest distance in units in the last place that SPEC 7.2 allows between platforms.
ULP_TOLERANCE = 4


def _ordered(x: float) -> int:
    """The float64 bit pattern as an integer that is monotonic in the value (-0.0 and 0.0 map to 0)."""
    (i,) = struct.unpack("<q", struct.pack("<d", x))
    return -(2**63) - i if i < 0 else i


def ulp_distance(a: float, b: float) -> int:
    """Distance between two finite float64 values in units in the last place."""
    return abs(_ordered(a) - _ordered(b))


def _float_close(a: float, b: float) -> bool:
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if math.isinf(a) or math.isinf(b):
        return a == b
    return ulp_distance(a, b) <= ULP_TOLERANCE


def json_close(a: Any, b: Any) -> bool:
    """Two JSON values equal up to 4 ulp on floating-point numbers (types must agree otherwise)."""
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return type(a) is type(b) and a == b
    if isinstance(a, float) and isinstance(b, float):
        return _float_close(a, b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return type(a) is type(b) and a == b
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_close(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_close(a[k], b[k]) for k in a)
    return False


def _json_equivalent(certified: Path, rerun: Path, unmap: list[tuple[str, str]]) -> bool:
    try:
        a = json.loads(certified.read_text(encoding="utf-8"))
        b = json.loads(rerun.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return json_close(strip_volatile(a), strip_volatile(map_strings(b, unmap)))


def _floats_close(a: Any, b: Any) -> bool:
    """Element-wise 4-ulp comparison of two numpy float arrays of one dtype (NaN equals NaN)."""
    import numpy as np

    if a.dtype != b.dtype or a.shape != b.shape:
        return False
    nan_a, nan_b = np.isnan(a), np.isnan(b)
    if not np.array_equal(nan_a, nan_b):
        return False
    keep = ~nan_a
    a, b = a[keep], b[keep]
    if np.array_equal(a, b):
        return True
    if not np.array_equal(np.isinf(a), np.isinf(b)) or not np.array_equal(a[np.isinf(a)], b[np.isinf(b)]):
        return False
    ity = {np.dtype(np.float64): np.int64, np.dtype(np.float32): np.int32}.get(a.dtype)
    if ity is None:
        return False
    lo = np.iinfo(ity).min
    ia, ib = a.view(ity), b.view(ity)
    # bit patterns ordered like the values; NaN excluded above, so the +-4 bounds cannot overflow
    oa = np.where(ia < 0, ity(lo) - ia, ia)
    ob = np.where(ib < 0, ity(lo) - ib, ib)
    with np.errstate(over="ignore"):
        return bool(np.all((oa >= ob - ULP_TOLERANCE) & (oa <= ob + ULP_TOLERANCE)))


def _parquet_equivalent(certified: Path, rerun: Path, unmap: list[tuple[str, str]]) -> bool:
    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        ta = pq.read_table(certified)
        tb = pq.read_table(rerun)
    except (OSError, pa.ArrowException):
        return False
    meta_a = ta.schema.metadata or {}
    meta_b = {k: map_bytes(v, unmap) for k, v in (tb.schema.metadata or {}).items()}
    if not ta.schema.equals(tb.schema, check_metadata=False) or meta_a != meta_b or ta.num_rows != tb.num_rows:
        return False
    for name in ta.column_names:
        ca, cb = ta.column(name), tb.column(name)
        if pa.types.is_floating(ca.type) and not pa.types.is_float16(ca.type):
            if not ca.is_null().equals(cb.is_null()):
                return False
            va = ca.drop_null().to_numpy()
            vb = cb.drop_null().to_numpy()
            if not _floats_close(va, vb):
                return False
        elif not ca.equals(cb):
            return False
    return True


def equivalent(certified: Path, rerun: Path, unmap: list[tuple[str, str]] | None = None) -> bool:
    """True when the re-executed file agrees with the certified one within the SPEC 7.2 tolerance."""
    suffix = certified.suffix.lower()
    if suffix != rerun.suffix.lower():
        return False
    if suffix == ".json":
        return _json_equivalent(certified, rerun, unmap or [])
    if suffix == ".parquet":
        return _parquet_equivalent(certified, rerun, unmap or [])
    return False
