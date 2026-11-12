"""Bounded reading of compressed inputs: gzip (QuickFIX logs and stores) and Parquet (simulator outputs, `dq`
datasets) expand to at most `MAX_RATIO` times their size on disk plus a fixed slack.

Parquet needs more than a page-size check. Dictionary and RLE encodings let a few KB of file decode to millions
of rows (12 KB of zstd Parquet hold 2 000 000 rows of two string columns), and a dictionary entry of megabytes can
repeat on every row. `read_parquet` therefore checks three things before it decodes anything it has not paid for: the
footer's stored size (bounds every page, including dictionary pages), the row count times the narrowest decoded row
(fails fast on RLE bombs), and, batch by batch, the decoded size of each column with string and binary columns
kept dictionary-encoded until their decoded length (the sum of the dictionary entries the indices name) is known.
Nested columns (lists, structs, maps) are refused: no input read here has them.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from queuetruth.errors import QtError

#: A compressed input may expand to at most this many times its size on disk, plus a slack. Real data sits far
#: below: QuickFIX logs compress about 10-12x with gzip, MBO partitions about 3-8x with Parquet; deflate's own
#: ceiling is about 1032x, which a decompression bomb uses.
MAX_RATIO = 200
SLACK = 64 * 2**20
_BATCH_ROWS = 4096


def expansion_limit(size: int, slack: int = SLACK) -> int:
    """Largest decoded size accepted for an input of `size` bytes on disk."""
    return MAX_RATIO * size + slack


def _binary_like(t: pa.DataType) -> bool:
    return (
        pa.types.is_string(t)
        or pa.types.is_large_string(t)
        or pa.types.is_binary(t)
        or pa.types.is_large_binary(t)
        or pa.types.is_fixed_size_binary(t)
    )


def _nested(t: pa.DataType) -> bool:
    if pa.types.is_dictionary(t):
        return _nested(t.value_type)
    return pa.types.is_nested(t)


def _min_width(t: pa.DataType) -> int:
    """Bytes one value of type `t` takes at least once decoded (validity bits ignored)."""
    if pa.types.is_dictionary(t):
        return max(1, t.index_type.bit_width // 8)
    if pa.types.is_fixed_size_binary(t):
        return max(1, t.byte_width)
    if pa.types.is_large_string(t) or pa.types.is_large_binary(t):
        return 8
    if _binary_like(t):
        return 4
    try:
        return max(1, t.bit_width // 8)
    except ValueError:  # null type
        return 1


def _decoded_bytes(col: pa.Array) -> int:
    """Bytes of `col` once its dictionary encoding (if any) is decoded, computed without decoding it."""
    if not pa.types.is_dictionary(col.type):
        return col.nbytes
    vt = col.type.value_type
    n = len(col) - col.null_count
    if pa.types.is_fixed_size_binary(vt):
        return n * vt.byte_width
    if _binary_like(vt):
        lengths = pc.binary_length(col.dictionary)
        total = pc.sum(pc.take(lengths, col.indices)).as_py()
        return (total or 0) + 8 * len(col)
    return n * _min_width(vt)


def _refused(path: Path, what: str, decoded: int, limit: int, size: int) -> QtError:
    return QtError(
        "QT-INPUT-002",
        f"{path}: {what}: Parquet input decodes to more than {limit} bytes ({decoded}; {MAX_RATIO}x its {size} bytes "
        "plus slack): refused",
    )


def read_parquet(
    path: Path,
    *,
    columns: list[str] | None = None,
    code: str = "QT-INPUT-002",
    what: str = "unreadable",
    slack: int = SLACK,
) -> pa.Table:
    """The Parquet file's table (only `columns` that exist when given), its decoded size bounded by
    `expansion_limit(file size, slack)`. `code` for a damaged file (any Arrow error, invalid UTF-8 in a column name),
    `QT-INPUT-002` for a file that decodes past the limit or has a nested column."""
    try:
        size = path.stat().st_size
        with pq.ParquetFile(path) as meta:
            schema = meta.schema_arrow
            names = list(schema.names)  # Arrow decodes names lazily: invalid UTF-8 fails here, inside the guard
            md = meta.metadata
            stored = sum(md.row_group(i).total_byte_size for i in range(md.num_row_groups))
            num_rows = md.num_rows
    except (OSError, ValueError, pa.ArrowException) as e:  # UnicodeDecodeError is a ValueError
        raise QtError(code, f"{path}: {what}: {e}") from None
    limit = expansion_limit(size, slack)
    keep = [i for i, n in enumerate(names) if columns is None or n in columns]
    fields = [schema.field(i) for i in keep]
    for f in fields:
        if _nested(f.type):
            raise QtError("QT-INPUT-002", f"{path}: {what}: column {f.name!r} has nested type {f.type}")
    if stored > limit:
        raise _refused(path, what, stored, limit, size)
    row_min = sum(_min_width(f.type) for f in fields) or 1
    if num_rows * row_min > limit:
        raise _refused(path, what, num_rows * row_min, limit, size)
    # Columns stored as plain strings are read dictionary-encoded, so a repeated megabyte entry is counted before it
    # is copied onto every row; they are decoded back once the batch is within the limit.
    as_dict = {f.name for f in fields if _binary_like(f.type)}
    batches: list[pa.RecordBatch] = []
    decoded = 0
    try:
        with pq.ParquetFile(path, read_dictionary=sorted(as_dict)) as pf:
            sel = None if columns is None else [names[i] for i in keep]
            for batch in pf.iter_batches(batch_size=_BATCH_ROWS, columns=sel):
                decoded += sum(_decoded_bytes(c) for c in batch.columns)
                if decoded > limit:
                    raise _refused(path, what, decoded, limit, size)
                cols = [
                    c.dictionary_decode() if n in as_dict and pa.types.is_dictionary(c.type) else c
                    for n, c in zip(batch.schema.names, batch.columns, strict=True)
                ]
                batches.append(pa.RecordBatch.from_arrays(cols, names=batch.schema.names))
        return pa.Table.from_batches(batches) if batches else pa.schema(fields).empty_table()
    except (OSError, ValueError, pa.ArrowException) as e:
        raise QtError(code, f"{path}: {what}: {e}") from None


__all__ = ["MAX_RATIO", "SLACK", "expansion_limit", "read_parquet"]
