"""Local-store signed URLs `/v1/_store/<key>` (SPEC 8.4): `PUT` stores one multipart part, `GET` downloads with
`Range` support. No API key: the HMAC signature over `method|key|part|exp` (expiring, one key) is the credential.
A part body must be exactly the part's expected length: a longer body is refused with 413 as soon as it passes the
limit, a shorter one with 422 (security review M1)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from queuetruth.schemas import models as m
from queuetruth_service.context import ServiceContext, ctx
from queuetruth_service.problems import ProblemError, not_found
from queuetruth_service.store import NotFoundError, PartTooLarge, SizeMismatch, StoreError

router = APIRouter(prefix="/v1/_store")
CHUNK = 1 << 20
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
BINARY = {"schema": {"type": "string", "format": "binary"}}


def forbidden() -> ProblemError:
    """403 for a bad or expired signature."""
    return ProblemError(403, "forbidden", "Forbidden", "QT-USAGE-001", "invalid or expired signature")


def _part_problem(e: SizeMismatch) -> ProblemError:
    if isinstance(e, PartTooLarge):
        return ProblemError(413, "payload-too-large", "Payload too large", "QT-USAGE-001", str(e))
    return ProblemError(422, "part-size-mismatch", "Part size mismatch", "QT-USAGE-001", str(e))


def absolute(c: ServiceContext, url: str) -> str:
    """Prefixes relative store URLs with `PUBLIC_BASE_URL` when set."""
    if url.startswith("/") and c.settings.public_base_url:
        return c.settings.public_base_url.rstrip("/") + url
    return url


def _local(c: ServiceContext) -> Any:
    if c.store.kind != "local":
        raise not_found("store path")
    return c.store


def serve_file(request: Request, c: ServiceContext, key: str, content_type: str) -> Response:
    """Streams an object with single-range `Range` support (200 / 206 / 416)."""
    store = _local(c)
    try:
        path = store.path(key)
    except StoreError as e:
        raise not_found("object") from e
    if not path.is_file():
        raise not_found("object")
    size = path.stat().st_size
    start, end, status = 0, size - 1, 200
    rng = request.headers.get("range")
    if rng:
        mt = RANGE_RE.match(rng.strip())
        if not mt or (not mt.group(1) and not mt.group(2)):
            raise ProblemError(
                416, "range-not-satisfiable", "Range not satisfiable", headers={"Content-Range": f"bytes */{size}"}
            )
        if mt.group(1):
            start = int(mt.group(1))
            end = min(int(mt.group(2)), size - 1) if mt.group(2) else size - 1
        else:
            start = max(0, size - int(mt.group(2)))
        if start >= size or start > end:
            raise ProblemError(
                416, "range-not-satisfiable", "Range not satisfiable", headers={"Content-Range": f"bytes */{size}"}
            )
        status = 206
    length = end - start + 1 if size else 0

    def body() -> Iterator[bytes]:
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        iterate_in_threadpool(body()), status_code=status, media_type=content_type, headers=headers
    )


@router.put(
    "/{key:path}",
    response_class=Response,
    responses={200: {"description": "Part stored; the ETag header carries its md5"}, 403: {"model": m.Problem}},
    openapi_extra={"requestBody": {"content": {"application/octet-stream": BINARY}}},
    summary="Signed part upload (local store)",
)
async def put_part(
    request: Request,
    key: str,
    part: Annotated[int, Query(ge=1, le=10_000)],
    exp: Annotated[str, Query(max_length=20)],
    sig: Annotated[str, Query(max_length=128)],
) -> Response:
    """Stores one part of a pending multipart upload."""
    c = ctx(request)
    store = _local(c)
    if not store.verify("PUT", key, str(part), exp, sig):
        raise forbidden()
    from starlette.concurrency import run_in_threadpool

    try:
        limit = await run_in_threadpool(store.part_limit, key, part)
    except NotFoundError as e:
        raise not_found("multipart upload") from e
    except StoreError as e:
        raise not_found("part") from e
    chunks: list[bytes] = []
    seen = 0
    async for chunk in request.stream():
        seen += len(chunk)
        if seen > limit:
            raise _part_problem(PartTooLarge(f"part {part} is longer than {limit} bytes"))
        chunks.append(chunk)
    try:
        etag = await run_in_threadpool(store.put_part, key, part, chunks)
    except SizeMismatch as e:
        raise _part_problem(e) from e
    except NotFoundError as e:
        raise not_found("multipart upload") from e
    except StoreError as e:
        raise not_found("object") from e
    return Response(status_code=200, headers={"ETag": etag})


@router.get(
    "/{key:path}",
    response_class=Response,
    responses={
        200: {"description": "Object bytes", "content": {"application/octet-stream": BINARY}},
        206: {"description": "Partial content", "content": {"application/octet-stream": BINARY}},
        403: {"model": m.Problem},
        416: {"model": m.Problem},
    },
    summary="Signed download (local store)",
)
def get_object(
    request: Request,
    key: str,
    exp: Annotated[str, Query(max_length=20)],
    sig: Annotated[str, Query(max_length=128)],
) -> Response:
    """Downloads one object (`Range` supported)."""
    c = ctx(request)
    store = _local(c)
    if not store.verify("GET", key, "", exp, sig):
        raise forbidden()
    return serve_file(request, c, key, "application/octet-stream")
