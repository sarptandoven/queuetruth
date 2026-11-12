"""sha256 of bytes, text, files, streams and canonical JSON, as lower-case hex.

Files and streams are read in 1 MiB chunks. Errors are the caller's to translate: `sha256_file` raises `OSError`
as `open` does (certificates turn it into `QT-INPUT-001`, see `queuetruth.cert.hashing`).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, BinaryIO

from queuetruth.integrity.canonical import Form, canonical_bytes

CHUNK = 1 << 20


def sha256_bytes(data: bytes) -> str:
    """Hex sha256 of `data`."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """Hex sha256 of the UTF-8 bytes of `text`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_stream(stream: BinaryIO, chunk: int = CHUNK) -> str:
    """Hex sha256 of everything left in a binary stream."""
    h = hashlib.sha256()
    while block := stream.read(chunk):
        h.update(block)
    return h.hexdigest()


def sha256_file(path: str | Path, chunk: int = CHUNK) -> str:
    """Hex sha256 of a file's bytes."""
    with open(path, "rb") as f:
        return sha256_stream(f, chunk)


def canonical_sha256(value: Any, form: Form = "jcs") -> str:
    """Hex sha256 of the canonical JSON of `value` in `form` (`queuetruth.integrity.canonical`)."""
    return hashlib.sha256(canonical_bytes(value, form)).hexdigest()


__all__ = ["CHUNK", "canonical_sha256", "sha256_bytes", "sha256_file", "sha256_stream", "sha256_text"]
