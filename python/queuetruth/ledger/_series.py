"""Series files: a record's performance series lives out of line in `<ledger dir>/series/<sha256>.qts`, named by the
sha256 of its bytes, and the record keeps that sha256 (`SeriesRef`), so the hash chain covers the values while
counting, listing and statistics never read them.

Layout (little-endian)::

    b"QTS1"  u64 n  u8 indexed  f64[n] values  [i64[n] index, period ends in UTC ns, when indexed]

The encoding is a function of the values and index only, so the same series always has the same name.
"""

from __future__ import annotations

import hashlib
import os
import struct
import sys
import tempfile
from array import array
from pathlib import Path

from queuetruth.errors import QtError
from queuetruth.ledger.models import Series, SeriesData, SeriesRef

MAGIC = b"QTS1"
SERIES_DIR = "series"
SUFFIX = ".qts"
_HEAD = struct.Struct("<4sQB")
_BIG = sys.byteorder == "big"


def _pack(code: str, xs: list[float] | list[int]) -> bytes:
    a = array(code, xs)
    if _BIG:  # pragma: no cover - big-endian hosts
        a.byteswap()
    return a.tobytes()


def _unpack(code: str, data: bytes) -> list[float] | list[int]:
    a = array(code)
    a.frombytes(data)
    if _BIG:  # pragma: no cover - big-endian hosts
        a.byteswap()
    return a.tolist()


def encode(series: Series) -> tuple[SeriesRef, bytes]:
    """(the record's reference, the file bytes) of a validated series."""
    n = len(series.values)
    blob = _HEAD.pack(MAGIC, n, 1 if series.index is not None else 0) + _pack("d", series.values)
    if series.index is not None:
        blob += _pack("q", series.index)
    ref = SeriesRef(
        kind=series.kind,
        sha256=hashlib.sha256(blob).hexdigest(),
        n_obs=n,
        indexed=series.index is not None,
        periods_per_year=series.periods_per_year,
        higher_is_better=series.higher_is_better,
    )
    return ref, blob


def decode(blob: bytes) -> SeriesData:
    """The values and index of a series file; ValueError when the bytes are not one."""
    if len(blob) < _HEAD.size:
        raise ValueError("shorter than the series header")
    magic, n, indexed = _HEAD.unpack_from(blob)
    if magic != MAGIC or indexed not in (0, 1):
        raise ValueError("not a queuetruth series file")
    want = _HEAD.size + 8 * n * (2 if indexed else 1)
    if len(blob) != want:
        raise ValueError(f"{len(blob)} bytes, the header implies {want}")
    end = _HEAD.size + 8 * n
    values = _unpack("d", blob[_HEAD.size : end])
    index = _unpack("q", blob[end:]) if indexed else None
    return SeriesData(values=values, index=index)  # type: ignore[arg-type]


def path_of(directory: Path, sha256: str) -> Path:
    return directory / SERIES_DIR / f"{sha256}{SUFFIX}"


def write(directory: Path, sha256: str, blob: bytes) -> None:
    """Stores a series file (atomically; an existing file of the right size is kept, since its name is its
    hash and a damaged one is caught when it is read). `QT-LEDGER-002` when it cannot be written."""
    if hashlib.sha256(blob).hexdigest() != sha256:
        raise QtError("QT-CONFIG-001", f"series {sha256[:12]}: the bytes do not hash to their name")
    p = path_of(directory, sha256)
    try:
        if p.is_file() and p.stat().st_size == len(blob):
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        raise QtError("QT-LEDGER-002", f"{p}: cannot store the series: {e.strerror or e}") from None


def _read(directory: Path, ref: SeriesRef) -> tuple[SeriesData | None, tuple[str, str] | None]:
    p = path_of(directory, ref.sha256)
    try:
        blob = p.read_bytes()
    except FileNotFoundError:
        return None, ("series_missing", f"series file {p.name} is missing")
    except OSError as e:
        return None, ("series_missing", f"series file {p.name} cannot be read: {e.strerror or e}")
    if hashlib.sha256(blob).hexdigest() != ref.sha256:
        return None, ("series_mismatch", f"series file {p.name} does not hash to its name (altered)")
    try:
        data = decode(blob)
    except ValueError as e:
        return None, ("series_mismatch", f"series file {p.name}: {e}")
    if len(data.values) != ref.n_obs or (data.index is not None) != ref.indexed:
        return None, ("series_mismatch", f"series file {p.name} does not match the record (n_obs, index)")
    return data, None


def check(directory: Path, ref: SeriesRef) -> tuple[str, str] | None:
    """None when the series file of `ref` is present and intact, else (`series_missing` | `series_mismatch`,
    message)."""
    return _read(directory, ref)[1]


def load(directory: Path, ref: SeriesRef) -> SeriesData:
    """The checked values and index of `ref`; `QT-LEDGER-001` when the file is missing or altered."""
    data, bad = _read(directory, ref)
    if bad is not None or data is None:
        raise QtError(
            "QT-LEDGER-001",
            f"{directory}: {bad[1] if bad else 'series unreadable'}",
            "restore the series/ directory next to ledger.jsonl; `queuetruth ledger verify` lists every problem",
        )
    return data
