"""Shared pieces of the W1 scale pipeline (`scripts/scale/`): run defaults, canonical hashing and the Merkle tree.

Hashing rules (the manifest contract, `docs/scale.md`):
- `canonical_json(obj)`: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` as UTF-8; floats
  are Python's shortest round-trip repr (no NaN / inf allowed).
- `table_digest(table, keys)`: sha256 over the rows sorted by `keys` (then by every column), column by column:
  name and Arrow type string (0x00-terminated), one validity byte per row, then the values with nulls as 0 / "":
  integers, timestamps and dates as int64 little-endian, floats as float64 IEEE bits, booleans as one byte, strings
  as UTF-8 with a 0x00 terminator. Independent of the Parquet writer and of row order, so a re-run on another
  machine compares contents, not file bytes.
- Merkle tree: leaves `sha256(b"\\x00" + canonical_json(leaf_doc))` in unit-id order; inner nodes
  `sha256(b"\\x01" + left + right)`; an odd node is carried up unchanged; the root of no leaves is sha256(b"").
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (REPO / "scripts" / "realdata", REPO / "scripts" / "repro", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from breadth import HORIZONS, POPULATIONS, STD_PLACEMENTS, STD_QTY, TIERS  # noqa: E402
from reproduce import build_info as _build_info  # noqa: E402
from reproduce import free_gib, sha256_file  # noqa: E402

S1 = 1_000_000_000
H_MAX_NS = 300 * S1
#: The W1 design (07_scale_validation.md 1.5): the breadth populations scaled down, both modes.
SCALE_POPULATIONS: dict[str, dict[str, Any]] = {
    "standard": {"n": 1000, "placements": STD_PLACEMENTS, "qty": STD_QTY},
    "large": {"n": 500, "placements": POPULATIONS["large"]["placements"], "qty": POPULATIONS["large"]["qty"]},
    "deep": {"n": 500, "placements": POPULATIONS["deep"]["placements"], "qty": STD_QTY},
}
#: Nasdaq test symbols (never real securities): excluded from every universe with this reason.
TEST_SYMBOL_PREFIXES = ("ZAZZT", "ZBZZT", "ZCZZT", "ZJZZT", "ZVZZT", "ZWZZT", "ZXZZT", "ZYZZT")
INGEST_DEFAULTS = {"venue": "nasdaq", "price_exp": -9, "tick_size": "0.01", "lot_size": "1"}

__all__ = [
    "HORIZONS",
    "H_MAX_NS",
    "INGEST_DEFAULTS",
    "POPULATIONS",
    "SCALE_POPULATIONS",
    "TEST_SYMBOL_PREFIXES",
    "TIERS",
    "build_info",
    "canonical_json",
    "free_gib",
    "leaf_hash",
    "merkle_proof",
    "merkle_root",
    "sha256_file",
    "sha256_hex",
    "table_digest",
    "verify_proof",
]


def build_info() -> dict[str, Any]:
    """`reproduce.build_info()`; in a container without `.git` the commit comes from `QT_GIT_HEAD`."""
    info = _build_info()
    if not info.get("git_head"):
        info["git_head"] = os.environ.get("QT_GIT_HEAD") or None
    return info


def canonical_json(obj: Any) -> bytes:
    def check(x: Any) -> None:
        if isinstance(x, float) and not math.isfinite(x):
            raise ValueError("canonical_json: NaN / inf are not allowed")
        if isinstance(x, dict):
            for v in x.values():
                check(v)
        elif isinstance(x, list | tuple):
            for v in x:
                check(v)

    check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def table_digest(table: Any, keys: Sequence[str] = ()) -> str:
    """Row-order and writer independent sha256 of an Arrow table's contents (module docstring)."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc

    names = list(table.column_names)
    order_cols = [k for k in keys if k in names] + [n for n in names if n not in keys]
    t = table.sort_by([(n, "ascending") for n in order_cols]) if table.num_rows and order_cols else table
    h = hashlib.sha256()
    for name in names:
        col = t.column(name)
        typ = col.type
        h.update(name.encode() + b"\x00" + str(typ).encode() + b"\x00")
        if pa.types.is_dictionary(typ):
            col = col.cast(typ.value_type)
            typ = typ.value_type
        valid = np.asarray(pc.is_valid(col).to_numpy(zero_copy_only=False), dtype=np.uint8)
        h.update(valid.tobytes())
        if pa.types.is_string(typ) or pa.types.is_large_string(typ):
            h.update(b"".join(v.encode("utf-8") + b"\x00" for v in pc.fill_null(col, "").to_pylist()))
        elif pa.types.is_floating(typ):
            h.update(np.asarray(pc.fill_null(col.cast(pa.float64()), 0.0).to_numpy(), dtype="<f8").tobytes())
        elif pa.types.is_boolean(typ):
            h.update(np.asarray(pc.fill_null(col, False).to_numpy(zero_copy_only=False), dtype=np.uint8).tobytes())
        else:
            if pa.types.is_date(typ):
                col = col.cast(pa.int32() if pa.types.is_date32(typ) else pa.int64())
            h.update(np.asarray(pc.fill_null(col.cast(pa.int64()), 0).to_numpy(), dtype="<i8").tobytes())
    return h.hexdigest()


def leaf_hash(leaf_doc: dict[str, Any]) -> str:
    return sha256_hex(b"\x00" + canonical_json(leaf_doc))


def _level_up(level: list[bytes]) -> list[bytes]:
    nxt = []
    for i in range(0, len(level), 2):
        if i + 1 < len(level):
            nxt.append(hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest())
        else:
            nxt.append(level[i])
    return nxt


def merkle_root(leaves: Sequence[str]) -> str:
    if not leaves:
        return sha256_hex(b"")
    level = [bytes.fromhex(x) for x in leaves]
    while len(level) > 1:
        level = _level_up(level)
    return level[0].hex()


def merkle_proof(leaves: Sequence[str], index: int) -> list[list[str]]:
    """Sibling path of leaf `index`: `[["L" or "R", sibling hex], ...]` from the leaf up (carried nodes skipped)."""
    level = [bytes.fromhex(x) for x in leaves]
    path: list[list[str]] = []
    i = index
    while len(level) > 1:
        sib = i ^ 1
        if sib < len(level):
            path.append(["L" if sib < i else "R", level[sib].hex()])
        level = _level_up(level)
        i //= 2
    return path


def verify_proof(leaf: str, proof: Sequence[Sequence[str]], root: str) -> bool:
    h = bytes.fromhex(leaf)
    for side, sib in proof:
        s = bytes.fromhex(sib)
        h = hashlib.sha256(b"\x01" + (s + h if side == "L" else h + s)).digest()
    return h.hex() == root
