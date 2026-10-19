"""Store protocol, part URLs, errors and object-key helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class StoreError(Exception):
    """A store operation failed (bad multipart completion, I/O)."""


class NotFoundError(StoreError):
    """The object does not exist."""


class SizeMismatch(StoreError):
    """Uploaded bytes do not match the declared upload size or the part size (security review M1)."""


class PartTooLarge(SizeMismatch):
    """A part body is longer than its expected length (rejected while streaming)."""


def part_count(size_bytes: int, part_size: int) -> int:
    """Number of parts of an upload."""
    return max(1, -(-int(size_bytes) // int(part_size)))


def expected_part_size(size_bytes: int, part_size: int, part_number: int) -> int:
    """Exact length of part `part_number` (1-based): `part_size`, except the last part, which holds the rest."""
    n = part_count(size_bytes, part_size)
    if not 1 <= part_number <= n:
        raise SizeMismatch(f"part {part_number} is outside 1..{n}")
    return int(part_size) if part_number < n else int(size_bytes) - (n - 1) * int(part_size)


def check_part_sizes(size_bytes: int, part_size: int, sizes: dict[int, int]) -> None:
    """Raises `SizeMismatch` unless `sizes` (part number -> bytes) is exactly the declared layout."""
    n = part_count(size_bytes, part_size)
    if sorted(sizes) != list(range(1, n + 1)):
        raise SizeMismatch(f"expected parts 1..{n}, the store has {sorted(sizes)[:20]}")
    for i, got in sorted(sizes.items()):
        want = expected_part_size(size_bytes, part_size, i)
        if got != want:
            raise SizeMismatch(f"part {i} has {got} bytes, expected {want}")
    total = sum(sizes.values())
    if total != size_bytes:
        raise SizeMismatch(f"parts hold {total} bytes, the upload declared {size_bytes}")


@dataclass(frozen=True)
class PartUrl:
    """A presigned URL for one multipart part."""

    part_number: int
    url: str
    expires_at: datetime


class ObjectStore(Protocol):
    """SPEC 8.4 interface, plus listing, hashing and abort helpers used by the worker. `complete_multipart` checks
    every part's size against the declared layout (`check_part_sizes`) and raises `SizeMismatch` otherwise."""

    kind: str
    endpoint: str | None

    def create_multipart(self, key: str, size_bytes: int, part_size: int) -> tuple[str, list[PartUrl]]: ...
    def part_urls(self, key: str, multipart_id: str, n_parts: int) -> list[PartUrl]: ...
    def complete_multipart(
        self, key: str, multipart_id: str, parts: list[dict], size_bytes: int, part_size: int
    ) -> None: ...
    def abort_multipart(self, key: str, multipart_id: str) -> None: ...
    def staged_bytes(self, key: str, multipart_id: str) -> int: ...
    def put_file(self, key: str, path: Path) -> str: ...
    def get_to_file(self, key: str, path: Path) -> None: ...
    def presign_get(self, key: str, ttl_s: int) -> str: ...
    def head(self, key: str) -> dict: ...
    def sha256(self, key: str) -> str: ...
    def list_prefix(self, prefix: str) -> list[tuple[str, int]]: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def check(self) -> bool: ...


def safe_filename(name: str) -> str:
    """A filename usable as one object-key component (other characters become `_`)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    clean = _SAFE.sub("_", base)[:200].strip(".") or "file"
    return clean


def check_key(key: str) -> str:
    """Rejects keys with empty, `.` or `..` components or a leading slash."""
    parts = key.split("/")
    if not key or key.startswith("/") or any(p in ("", ".", "..") for p in parts) or "\\" in key or "\x00" in key:
        raise StoreError(f"invalid object key {key!r}")
    return key


def tenant_prefix(tenant_id: str) -> str:
    """`t/{tenant}/`."""
    return f"t/{tenant_id}/"


def upload_prefix(tenant_id: str, upload_id: str) -> str:
    """`t/{tenant}/uploads/{upload}/`."""
    return f"t/{tenant_id}/uploads/{upload_id}/"


def upload_key(tenant_id: str, upload_id: str, filename: str) -> str:
    """Object key of an upload."""
    return upload_prefix(tenant_id, upload_id) + safe_filename(filename)


def dataset_prefix(tenant_id: str, dataset_id: str) -> str:
    """`t/{tenant}/datasets/{dataset}/`."""
    return f"t/{tenant_id}/datasets/{dataset_id}/"


def job_prefix(tenant_id: str, job_id: str) -> str:
    """`t/{tenant}/jobs/{job}/`."""
    return f"t/{tenant_id}/jobs/{job_id}/"


def job_key(tenant_id: str, job_id: str, name: str) -> str:
    """Object key of a job output file (`name` may contain `/`)."""
    return check_key(job_prefix(tenant_id, job_id) + name)
