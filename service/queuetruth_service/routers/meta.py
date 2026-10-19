"""`/v1/health`, `/v1/meta`, `/v1/tenant`."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from queuetruth._version import __version__ as engine_version
from queuetruth.schemas import models as m
from queuetruth.schemas.models import DOC_MODELS
from queuetruth_service import __version__ as service_version
from queuetruth_service.api_models import INGEST_FORMATS
from queuetruth_service.auth import ReadAuth
from queuetruth_service.context import ctx
from queuetruth_service.db import tenant_tx
from queuetruth_service.documents import fetch_one, tenant_doc
from queuetruth_service.problems import not_found

router = APIRouter(prefix="/v1")


@router.get(
    "/health",
    response_model=m.Health,
    responses={503: {"model": m.Health, "description": "A dependency is down"}},
    summary="Service health (no auth)",
)
def health(request: Request) -> Any:
    """`qt.health`: database, store and queue checks; 503 when any is down."""
    c = ctx(request)
    db = queue = "down"
    if c.pool is not None:
        try:
            with c.pool.connection(timeout=3) as conn:
                conn.execute("select 1")
                db = "ok"
                conn.execute("select count(*) from pgqueuer where status = 'queued'")
                queue = "ok"
        except Exception:
            pass
    try:
        store = "ok" if c.store.check() else "down"
    except Exception:
        store = "down"
    ok = db == store == queue == "ok"
    doc = {
        "schema": "qt.health",
        "schema_version": 1,
        "status": "ok" if ok else "degraded",
        "db": db,
        "store": store,
        "queue": queue,
        "version": service_version,
    }
    return JSONResponse(doc, status_code=200 if ok else 503)


@router.get("/meta", response_model=m.Meta, summary="Versions, limits, formats")
def meta(request: Request, auth: ReadAuth) -> Any:
    """`qt.meta`."""
    c = ctx(request)
    with tenant_tx(c.db(), auth) as conn:
        t = fetch_one(conn, "select zero_egress from tenants where id = %s", (auth.tenant_id,))
    s = c.settings
    return {
        "schema": "qt.meta",
        "schema_version": 1,
        "api_version": "v1",
        "engine_version": engine_version,
        "schema_versions": dict.fromkeys(DOC_MODELS, 1),
        "limits": {
            "max_upload_bytes": s.max_upload_bytes,
            "part_size_bytes": s.part_size_bytes,
            "max_jobs_running": s.tenant_max_running or s.worker_concurrency,
            "rate_limits": {b: {"per_minute": v[0], "burst": v[1]} for b, v in sorted(s.rate_limits.items())},
        },
        "formats": list(INGEST_FORMATS),
        "zero_egress": bool(s.zero_egress or (t or {}).get("zero_egress")),
    }


@router.get("/tenant", response_model=m.Tenant, summary="The caller's tenant")
def tenant(request: Request, auth: ReadAuth) -> Any:
    """`qt.tenant`."""
    with tenant_tx(ctx(request).db(), auth) as conn:
        row = fetch_one(conn, "select * from tenants where id = %s", (auth.tenant_id,))
    if row is None:
        raise not_found("tenant")
    return tenant_doc(row)
