"""`/v1/uploads`: presigned multipart uploads (SPEC 8.4). Completion checks every part against the declared layout and
assembles the parts; the worker verifies size and sha256 asynchronously (`complete` -> `verified` | `failed`).

Security review: part URLs are write credentials, so `GET` returns them only to keys with the write scope (L1); a
completion whose parts do not match the declared size marks the upload `failed` and deletes its parts (422
`upload-size-mismatch`, M1)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Response

from queuetruth.schemas import models as m
from queuetruth_service import idempotency
from queuetruth_service.api_models import UploadComplete, UploadCreate
from queuetruth_service.auth import ReadAuth, WriteAuth
from queuetruth_service.context import ctx
from queuetruth_service.db import tenant_tx
from queuetruth_service.documents import fetch_one, upload_doc
from queuetruth_service.ids import new_id
from queuetruth_service.problems import ProblemError, not_found, problem_body, request_id, validation
from queuetruth_service.retention import delete_upload_sync
from queuetruth_service.store import SizeMismatch, StoreError, part_count, upload_key, upload_prefix

router = APIRouter(prefix="/v1/uploads")
UploadId = Annotated[str, Path(max_length=64)]


@router.post("", status_code=201, response_model=m.Upload, summary="Start a multipart upload")
async def create_upload(request: Request, body: UploadCreate, idem: idempotency.IdemKey, auth: WriteAuth) -> Any:
    """`qt.upload` with presigned part URLs (TTL <= 15 min; refresh with GET while pending)."""
    c = ctx(request)
    s = c.settings
    if body.size_bytes > s.max_upload_bytes:
        raise ProblemError(
            413,
            "payload-too-large",
            "Payload too large",
            "QT-USAGE-001",
            f"uploads are limited to {s.max_upload_bytes} bytes",
        )
    if part_count(body.size_bytes, s.part_size_bytes) > 10_000:
        raise validation(["body", "size_bytes"], "more than 10,000 parts at the configured part size")

    def execute(conn: Any) -> idempotency.Outcome:
        up_id = new_id("up")
        key = upload_key(auth.tenant_id, up_id, body.filename)
        mp_id, _ = c.store.create_multipart(key, body.size_bytes, s.part_size_bytes)
        row = fetch_one(
            conn,
            "insert into uploads (id, tenant_id, purpose, filename, content_type, size_bytes, sha256, status, "
            "object_key, multipart_id, part_size) values (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s) "
            "returning *",
            (
                up_id,
                auth.tenant_id,
                body.purpose,
                body.filename,
                body.content_type,
                body.size_bytes,
                body.sha256,
                key,
                mp_id,
                s.part_size_bytes,
            ),
        )
        return 201, upload_doc(row or {}, c.store), {}, None

    return await idempotency.run(request, auth, idem, execute)


@router.get("/{upload_id}", response_model=m.Upload, summary="Get an upload")
def get_upload(request: Request, upload_id: UploadId, auth: ReadAuth) -> Any:
    """`qt.upload` (fresh part URLs while pending)."""
    c = ctx(request)
    with tenant_tx(c.db(), auth) as conn:
        row = fetch_one(conn, "select * from uploads where id = %s and tenant_id = %s", (upload_id, auth.tenant_id))
    if row is None:
        raise not_found("upload")
    return upload_doc(row, c.store, part_urls=auth.has("write"))


@router.post("/{upload_id}/complete", response_model=m.Upload, summary="Complete a multipart upload")
async def complete_upload(
    request: Request, upload_id: UploadId, body: UploadComplete, idem: idempotency.IdemKey, auth: WriteAuth
) -> Any:
    """Assembles the parts; status `complete` (sha256 verification follows asynchronously)."""
    c = ctx(request)

    def execute(conn: Any) -> idempotency.Outcome:
        row = fetch_one(
            conn, "select * from uploads where id = %s and tenant_id = %s for update", (upload_id, auth.tenant_id)
        )
        if row is None:
            raise not_found("upload")
        if row["status"] != "pending":
            raise ProblemError(
                409, "upload-not-pending", "Upload not pending", "QT-USAGE-001", f"upload is {row['status']}"
            )
        size, part_size = int(row["size_bytes"]), int(row["part_size"])
        n = part_count(size, part_size)
        numbers = sorted(p.part_number for p in body.parts)
        if numbers != list(range(1, n + 1)):
            raise validation(["body", "parts"], f"parts must list part numbers 1..{n} exactly once")
        try:
            c.store.complete_multipart(
                row["object_key"], row["multipart_id"], [p.model_dump() for p in body.parts], size, part_size
            )
        except SizeMismatch as e:
            # the bytes do not match what was declared (and billed, and capped): drop them and fail the upload
            c.store.abort_multipart(row["object_key"], row["multipart_id"])
            c.store.delete_prefix(upload_prefix(auth.tenant_id, upload_id))
            conn.execute(
                "update uploads set status = 'failed', completed_at = now(), stored_bytes = 0 "
                "where id = %s and tenant_id = %s",
                (upload_id, auth.tenant_id),
            )
            doc = problem_body(
                request_id(request),
                422,
                "upload-size-mismatch",
                "Upload size mismatch",
                "QT-INPUT-001",
                detail=f"{e}; the upload is failed and its parts were deleted",
                instance=request.url.path,
            )
            return 422, doc, {}, None
        except StoreError as e:
            raise validation(["body", "parts"], f"multipart completion failed: {e}") from e
        done = fetch_one(
            conn,
            "update uploads set status = 'complete', completed_at = now() where id = %s and tenant_id = %s returning *",
            (upload_id, auth.tenant_id),
        )
        return 200, upload_doc(done or {}, c.store), {}, None

    return await idempotency.run(request, auth, idem, execute)


@router.delete("/{upload_id}", status_code=204, response_class=Response, summary="Delete an upload")
def delete_upload(request: Request, upload_id: UploadId, auth: WriteAuth) -> Response:
    """Hard delete (objects and row) with a tombstone."""
    c = ctx(request)
    with tenant_tx(c.db(), auth) as conn:
        row = fetch_one(
            conn, "select * from uploads where id = %s and tenant_id = %s for update", (upload_id, auth.tenant_id)
        )
        if row is None:
            raise not_found("upload")
        delete_upload_sync(conn, c.store, auth.tenant_id, row, auth.key_id)
    return Response(status_code=204)
