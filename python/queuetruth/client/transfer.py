"""Presigned multipart uploads and resumable, sha256-verified job file downloads (SPEC 4.5, 8.4).

Uploads: `POST /v1/uploads` returns one presigned URL per part; part `k` (1-based) covers bytes
`[(k-1) * part_size, min(k * part_size, size))` with `part_size = meta.limits.part_size_bytes` (a single part covers
the whole file). Each part is PUT without the API key; its `ETag` is reported to `/complete`. A 401/403 on a part URL
(expired) refreshes the URLs once through `GET /v1/uploads/{id}`.

Downloads: `GET /v1/jobs/{id}/files/{name}` (with the API key) either streams the file (local store, `Range`
supported) or redirects to a presigned URL (fetched without the API key). Bytes go to `<dest>.part`; after an
interruption the next attempt asks for `Range: bytes=<have>-` and appends on 206 (restarts on 200). The finished file
must match the manifest's `bytes` and `sha256`, else it is deleted and `QtError("QT-REMOTE-001")` is raised; only a
verified file is renamed to `dest`.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin

import httpx

from queuetruth.client.problems import api_error
from queuetruth.errors import QtError
from queuetruth.schemas import models as m

if TYPE_CHECKING:
    from queuetruth.client._client import Client

CHUNK = 1 << 20
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class _Interrupted(Exception):
    """A download attempt ended early; the next attempt resumes."""


def file_digest(path: Path) -> tuple[int, str]:
    """Size in bytes and lower-case hex sha256 of a file."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def safe_join(root: Path, name: str) -> Path:
    """`root / name` for a manifest file name; absolute names and `..` components are rejected."""
    pure = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or pure.is_absolute()
        or any(p in ("..", "") for p in pure.parts)
        or ":" in pure.parts[0]
    ):
        raise QtError("QT-REMOTE-001", f"unsafe file name in job manifest: {name!r}")
    return root.joinpath(*pure.parts)


# ---- uploads ----------------------------------------------------------------------------------------------------


def _put_part(client: Client, upload_id: str, part: m.UploadPart, data: bytes) -> str:
    url = client.url(part.url)
    refreshed = False
    while True:
        r = client.send("PUT", url, content=data)
        if r.status_code in (401, 403) and not refreshed:
            fresh = {p.part_number: p for p in client.get_upload(upload_id).parts}
            if part.part_number not in fresh:
                raise api_error(r)
            url = client.url(fresh[part.part_number].url)
            refreshed = True
            continue
        if r.status_code >= 400:
            raise api_error(r)
        etag = r.headers.get("ETag")
        if not etag:
            raise QtError("QT-REMOTE-001", f"upload {upload_id} part {part.part_number}: store returned no ETag")
        return etag


def upload_file(
    client: Client,
    path: Path,
    purpose: str,
    *,
    filename: str | None = None,
    content_type: str | None = None,
    idempotency_key: str | None = None,
) -> m.Upload:
    """Create -> PUT parts -> complete for one local file; returns the completed upload."""
    if not path.is_file():
        raise QtError("QT-INPUT-001", f"{path}: not a readable file")
    size, digest = file_digest(path)
    if size == 0:
        raise QtError("QT-INPUT-002", f"{path}: empty file cannot be uploaded")
    up = client.create_upload(
        purpose, filename or path.name, size, digest, content_type=content_type, idempotency_key=idempotency_key
    )
    parts = sorted(up.parts, key=lambda p: p.part_number)
    n = len(parts)
    if n == 0 or [p.part_number for p in parts] != list(range(1, n + 1)):
        raise QtError("QT-REMOTE-001", f"upload {up.id}: part numbers are not 1..{n}")
    part_size = size if n == 1 else client.meta().limits.part_size_bytes
    if part_size <= 0 or math.ceil(size / part_size) != n:
        raise QtError("QT-REMOTE-001", f"upload {up.id}: {n} parts do not match part size {part_size} for {size} bytes")
    done = []
    with open(path, "rb") as f:
        for p in parts:
            f.seek((p.part_number - 1) * part_size)
            data = f.read(part_size)
            done.append({"part_number": p.part_number, "etag": _put_part(client, up.id, p, data)})
    complete_key = f"{idempotency_key[:240]}.complete" if idempotency_key else None
    return client.complete_upload(up.id, done, idempotency_key=complete_key)


# ---- downloads --------------------------------------------------------------------------------------------------


def _range_start(response: httpx.Response) -> int | None:
    match = _CONTENT_RANGE.match(response.headers.get("Content-Range", "").strip())
    return int(match.group(1)) if match else None


def _fetch(client: Client, api_url: str, part: Path, offset: int) -> None:
    """One attempt: request from `offset` and write what arrives to `part`."""
    range_header = {"Range": f"bytes={offset}-"} if offset > 0 else {}
    r = client.send("GET", api_url, headers={**client._auth_headers(), **range_header}, stream=True)
    try:
        if r.status_code in _REDIRECTS:
            location = r.headers.get("Location")
            r.close()
            if not location:
                raise QtError("QT-REMOTE-001", f"redirect without Location from {api_url}")
            r = client.send("GET", urljoin(api_url, location), headers=range_header, stream=True)
            if r.status_code in (401, 403):
                raise _Interrupted(f"presigned URL rejected with {r.status_code} (expired?)")
        if r.status_code == 416:
            part.unlink(missing_ok=True)
            raise _Interrupted("range not satisfiable; restarting from byte 0")
        if r.status_code >= 400:
            raise api_error(r)
        if r.status_code == 206:
            start = _range_start(r)
            if start != offset:
                part.unlink(missing_ok=True)
                raise _Interrupted(f"unexpected Content-Range (start {start}, wanted {offset}); restarting")
            mode = "ab"
        elif r.status_code == 200:
            mode = "wb"
        else:
            raise QtError("QT-REMOTE-001", f"unexpected HTTP {r.status_code} for {api_url}")
        with open(part, mode) as f:
            try:
                for chunk in r.iter_bytes():
                    f.write(chunk)
            except httpx.TransportError as e:
                raise _Interrupted(f"{type(e).__name__}: {e}") from e
    finally:
        r.close()


def download_job_file(client: Client, job_id: str, entry: m.ManifestFile, dest: Path) -> Path:
    """Downloads `entry` of `job_id` to `dest` (resume + verification, see module docstring)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    api_url = client.url(f"/v1/jobs/{quote(job_id, safe='')}/files/{quote(entry.name, safe='/')}")
    failures = 0
    while True:
        have = part.stat().st_size if part.exists() else 0
        if have > entry.bytes:
            part.unlink()
            have = 0
        if have == entry.bytes and have > 0:
            break
        try:
            _fetch(client, api_url, part, have)
            got = part.stat().st_size if part.exists() else 0
            if got == entry.bytes:
                break
            if got > entry.bytes:
                part.unlink()
            raise _Interrupted(f"received {got} of {entry.bytes} bytes")
        except _Interrupted as e:
            delay = client._delay(failures, None)
            failures += 1
            if delay is None:
                raise QtError(
                    "QT-REMOTE-001",
                    f"download of {entry.name} (job {job_id}) failed after {failures} attempts: {e}",
                    "run the download again; completed bytes are kept in the .part file",
                    {"job_id": job_id, "name": entry.name},
                ) from e
            client._sleep(delay)
    if not part.exists():
        part.write_bytes(b"")
    size, digest = file_digest(part)
    if size != entry.bytes or digest != entry.sha256:
        part.unlink()
        raise QtError(
            "QT-REMOTE-001",
            f"sha256 mismatch for {entry.name} (job {job_id}): expected {entry.sha256} ({entry.bytes} bytes), "
            f"got {digest} ({size} bytes)",
            "the partial file was deleted; download again",
            {"job_id": job_id, "name": entry.name, "expected_sha256": entry.sha256, "sha256": digest},
        )
    os.replace(part, dest)
    return dest


__all__ = ["download_job_file", "file_digest", "safe_join", "upload_file"]
