"""`/v1/jobs`: list, get, cancel, file manifest and file download (302 to a presigned URL on S3, or a direct
stream with `Range` support on the local store)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import RedirectResponse, Response

from queuetruth.schemas import models as m
from queuetruth_service import idempotency
from queuetruth_service.api_models import JobPage
from queuetruth_service.auth import ReadAuth, WriteAuth
from queuetruth_service.context import ctx
from queuetruth_service.db import tenant_tx
from queuetruth_service.documents import fetch_one, job_doc, page, page_doc
from queuetruth_service.ids import rfc3339
from queuetruth_service.jobs import TERMINAL
from queuetruth_service.problems import ProblemError, not_found
from queuetruth_service.routers.store import absolute, serve_file

router = APIRouter(prefix="/v1/jobs")
JobId = Annotated[str, Path(max_length=64)]
Limit = Annotated[int, Query(ge=1, le=200)]
Kind = Literal["ingest", "replay", "calibration", "scorecard", "report", "dataset_validate", "deletion"]
State = Literal["queued", "running", "succeeded", "failed", "canceled", "expired"]
#: Longest a cancel waits for the job row (held briefly by the worker's claim, progress, heartbeat and finish
#: transactions). A holder stuck longer (a stalled or partitioned worker mid-transaction) must not pin an API thread and
#: a pool connection forever: the cancel answers 503 with Retry-After and, the idempotency record being dropped with the
#: failed attempt, the client retries with the same key.
CANCEL_LOCK_TIMEOUT_MS = 5000
FILE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "File bytes",
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    },
    206: {
        "description": "Partial content",
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    },
    302: {"description": "Redirect to a presigned URL"},
    410: {"model": m.Problem, "description": "Outputs expired"},
    416: {"model": m.Problem, "description": "Range not satisfiable"},
}


@router.get("", response_model=JobPage, summary="List jobs")
def list_jobs(
    request: Request,
    auth: ReadAuth,
    limit: Limit = 50,
    starting_after: str | None = None,
    kind: Kind | None = None,
    state: State | None = None,
) -> Any:
    """`qt.page[qt.job]`, newest first, optionally filtered."""
    with tenant_tx(ctx(request).db(), auth) as conn:
        rows, more = page(conn, "jobs", auth.tenant_id, limit, starting_after, {"kind": kind, "state": state})
    return page_doc([job_doc(r) for r in rows], more)


@router.get("/{job_id}", response_model=m.Job, summary="Get a job")
def get_job(request: Request, job_id: JobId, auth: ReadAuth) -> Any:
    """`qt.job`."""
    with tenant_tx(ctx(request).db(), auth) as conn:
        row = fetch_one(conn, "select * from jobs where id = %s and tenant_id = %s", (job_id, auth.tenant_id))
    if row is None:
        raise not_found("job")
    return job_doc(row)


@router.post("/{job_id}/cancel", response_model=m.Job, summary="Cancel a job")
async def cancel_job(request: Request, job_id: JobId, idem: idempotency.IdemKey, auth: WriteAuth) -> Any:
    """Queued jobs become `canceled`; running jobs get `cancel_requested` (the worker kills the child); 409 when
    the job is terminal."""

    def execute(conn: Any) -> idempotency.Outcome:
        # bounded wait for the row; `for no key update` (not `for update`) because only non-key columns change, so
        # foreign-key checks of rows referencing the job (job_files, run tables) neither block nor are blocked
        conn.execute("select set_config('lock_timeout', %s, true)", (f"{CANCEL_LOCK_TIMEOUT_MS}ms",))
        try:
            row = fetch_one(
                conn,
                "select * from jobs where id = %s and tenant_id = %s for no key update",
                (job_id, auth.tenant_id),
            )
        except psycopg.errors.LockNotAvailable as e:
            raise ProblemError(
                503,
                "unavailable",
                "Service unavailable",
                "QT-ENGINE-001",
                "the job is locked by a worker transaction; retry with the same Idempotency-Key",
                headers={"Retry-After": "1"},
            ) from e
        if row is None:
            raise not_found("job")
        if row["state"] in TERMINAL:
            raise ProblemError(409, "job-terminal", "Job is terminal", "QT-USAGE-001", f"job is {row['state']}")
        if row["state"] == "queued":
            row = fetch_one(
                conn,
                "update jobs set state = 'canceled', cancel_requested = true, finished_at = now(), "
                "progress = jsonb_build_object('fraction', 1.0, 'stage', 'canceled') "
                "where id = %s and tenant_id = %s returning *",
                (job_id, auth.tenant_id),
            )
        else:
            row = fetch_one(
                conn,
                "update jobs set cancel_requested = true where id = %s and tenant_id = %s returning *",
                (job_id, auth.tenant_id),
            )
        return 200, job_doc(row or {}), {}, None

    return await idempotency.run(request, auth, idem, execute)


def _ready_job(conn: Any, tenant_id: str, job_id: str) -> dict[str, Any]:
    row = fetch_one(conn, "select * from jobs where id = %s and tenant_id = %s", (job_id, tenant_id))
    if row is None:
        raise not_found("job")
    if row["state"] == "expired":
        raise ProblemError(
            410, "gone", "Outputs expired", "QT-USAGE-001", "the job's outputs were deleted by retention"
        )
    if row["state"] != "succeeded":
        raise ProblemError(409, "job-not-succeeded", "Job not succeeded", "QT-USAGE-001", f"job is {row['state']}")
    return row


@router.get("/{job_id}/files", response_model=m.FileManifest, summary="List a job's files")
def job_files(request: Request, job_id: JobId, auth: ReadAuth) -> Any:
    """`qt.file_manifest` with presigned URLs (TTL <= 15 min)."""
    c = ctx(request)
    with tenant_tx(c.db(), auth) as conn:
        _ready_job(conn, auth.tenant_id, job_id)
        rows = conn.execute(
            "select name, bytes, sha256, content_type, object_key from job_files "
            "where job_id = %s and tenant_id = %s order by name",
            (job_id, auth.tenant_id),
        ).fetchall()
    ttl = c.settings.presign_ttl_s
    expires = rfc3339(datetime.fromtimestamp(datetime.now(UTC).timestamp() + ttl, UTC))
    files = [
        {
            "name": name,
            "bytes": int(size),
            "sha256": sha,
            "content_type": ctype,
            "url": absolute(c, c.store.presign_get(key, ttl)),
            "expires_at": expires,
        }
        for name, size, sha, ctype, key in rows
    ]
    return {"schema": "qt.file_manifest", "schema_version": 1, "job_id": job_id, "files": files}


@router.get(
    "/{job_id}/files/{name:path}", responses=FILE_RESPONSES, response_class=Response, summary="Download a job file"
)
def job_file(request: Request, job_id: JobId, name: str, auth: ReadAuth) -> Response:
    """302 to a presigned URL (S3) or the bytes with `Range` support (local store)."""
    c = ctx(request)
    with tenant_tx(c.db(), auth) as conn:
        _ready_job(conn, auth.tenant_id, job_id)
        row = fetch_one(
            conn,
            "select object_key, content_type from job_files where job_id = %s and tenant_id = %s and name = %s",
            (job_id, auth.tenant_id, name),
        )
    if row is None:
        raise not_found("file")
    if c.store.kind == "s3":
        return RedirectResponse(c.store.presign_get(row["object_key"], c.settings.presign_ttl_s), status_code=302)
    return serve_file(request, c, row["object_key"], row["content_type"])
