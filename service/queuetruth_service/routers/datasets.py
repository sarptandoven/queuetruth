"""`/v1/datasets`: register a dataset from verified uploads (a `dataset_validate` job runs `ingest`), list, get,
delete (a `deletion` job hard-deletes the objects and writes a tombstone)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from psycopg.types.json import Jsonb

from queuetruth.schemas import models as m
from queuetruth_service import idempotency
from queuetruth_service.api_models import DatasetCreate, DatasetPage, IngestOptions
from queuetruth_service.auth import ReadAuth, SubmitAuth, WriteAuth
from queuetruth_service.context import ctx
from queuetruth_service.db import tenant_tx
from queuetruth_service.documents import dataset_doc, fetch_one, page, page_doc
from queuetruth_service.ids import new_id
from queuetruth_service.jobs import create_job, enqueue_hook
from queuetruth_service.problems import ProblemError, not_found, validation
from queuetruth_service.refs import check_upload
from queuetruth_service.store import dataset_prefix

router = APIRouter(prefix="/v1/datasets")
DatasetId = Annotated[str, Path(max_length=64)]
Limit = Annotated[int, Query(ge=1, le=200)]


@router.post(
    "",
    status_code=202,
    response_model=m.Dataset,
    summary="Register a dataset (validation job)",
    responses={
        202: {"headers": {"Location": {"description": "URL of the validation job", "schema": {"type": "string"}}}}
    },
)
async def create_dataset(request: Request, body: DatasetCreate, idem: idempotency.IdemKey, auth: SubmitAuth) -> Any:
    """`qt.dataset` (status `validating`) + `Location` of its `dataset_validate` job."""
    c = ctx(request)
    opts = (body.ingest or IngestOptions()).model_dump(mode="json")
    for field in ("format", "venue"):
        if opts.get(field) is not None and opts[field] != getattr(body, field):
            raise validation(["body", "ingest", field], f"must equal the top-level {field} when given")
    if len(set(body.upload_ids)) != len(body.upload_ids):
        raise validation(["body", "upload_ids"], "duplicate upload id")

    def execute(conn: Any) -> idempotency.Outcome:
        for i, up in enumerate(body.upload_ids):
            try:
                check_upload(conn, auth.tenant_id, up)
            except ProblemError as e:
                e.detail = f"upload_ids[{i}]: {e.detail}"
                raise
        if opts.get("instruments"):
            check_upload(conn, auth.tenant_id, opts["instruments"]["upload_id"])
        ds_id = new_id("ds")
        ingest = opts | {"format": body.format, "venue": body.venue}
        job = create_job(
            conn,
            auth.tenant_id,
            "dataset_validate",
            {"ingest": ingest, "upload_ids": body.upload_ids},
            "dataset",
            ds_id,
        )
        row = fetch_one(
            conn,
            "insert into datasets (id, tenant_id, name, kind, format, venue, status, upload_ids, ingest, "
            "object_prefix, job_id) values (%s, %s, %s, %s, %s, %s, 'validating', %s, %s, %s, %s) returning *",
            (
                ds_id,
                auth.tenant_id,
                body.name,
                body.kind,
                body.format,
                body.venue,
                body.upload_ids,
                Jsonb(ingest),
                dataset_prefix(auth.tenant_id, ds_id),
                job["id"],
            ),
        )
        return 202, dataset_doc(row or {}), {"Location": f"/v1/jobs/{job['id']}"}, enqueue_hook(c, job["id"], auth)

    return await idempotency.run(request, auth, idem, execute)


@router.get("", response_model=DatasetPage, summary="List datasets")
def list_datasets(request: Request, auth: ReadAuth, limit: Limit = 50, starting_after: str | None = None) -> Any:
    """`qt.page[qt.dataset]`, newest first."""
    with tenant_tx(ctx(request).db(), auth) as conn:
        rows, more = page(conn, "datasets", auth.tenant_id, limit, starting_after)
    return page_doc([dataset_doc(r) for r in rows], more)


@router.get("/{dataset_id}", response_model=m.Dataset, summary="Get a dataset")
def get_dataset(request: Request, dataset_id: DatasetId, auth: ReadAuth) -> Any:
    """`qt.dataset`."""
    with tenant_tx(ctx(request).db(), auth) as conn:
        row = fetch_one(conn, "select * from datasets where id = %s and tenant_id = %s", (dataset_id, auth.tenant_id))
    if row is None:
        raise not_found("dataset")
    return dataset_doc(row)


@router.delete("/{dataset_id}", status_code=202, response_model=m.Job, summary="Delete a dataset")
async def delete_dataset(
    request: Request, dataset_id: DatasetId, auth: WriteAuth, idem: idempotency.OptIdemKey = None
) -> Any:
    """`qt.job` of the deletion (objects hard-deleted, tombstone written)."""
    c = ctx(request)

    def execute(conn: Any) -> idempotency.Outcome:
        row = fetch_one(
            conn, "select * from datasets where id = %s and tenant_id = %s for update", (dataset_id, auth.tenant_id)
        )
        if row is None or row["status"] == "deleted":
            raise not_found("dataset")
        conn.execute(
            "update datasets set status = 'deleted' where id = %s and tenant_id = %s", (dataset_id, auth.tenant_id)
        )
        job = create_job(conn, auth.tenant_id, "deletion", {"dataset_id": dataset_id}, "dataset", dataset_id)
        return 202, job, {"Location": f"/v1/jobs/{job['id']}"}, enqueue_hook(c, job["id"], auth)

    return await idempotency.run(request, auth, idem, execute)
