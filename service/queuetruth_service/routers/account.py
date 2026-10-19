"""`/v1/usage`, `/v1/retention`, `/v1/tombstones`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request

from queuetruth.schemas import models as m
from queuetruth_service import idempotency
from queuetruth_service.api_models import TombstonePage
from queuetruth_service.auth import AdminAuth, AdminReadAuth, ReadAuth
from queuetruth_service.context import ctx
from queuetruth_service.db import tenant_tx
from queuetruth_service.documents import fetch_one, page, page_doc, tombstone_doc
from queuetruth_service.ids import rfc3339
from queuetruth_service.problems import not_found, validation
from queuetruth_service.retention import audit_sync

router = APIRouter(prefix="/v1")
Limit = Annotated[int, Query(ge=1, le=200)]
UNITS = {
    "replay_records": "records",
    "shadow_orders": "orders",
    "zoo_pairs": "pairs",
    "orders_scored": "orders",
    "cpu_ms": "ms",
    "storage_byte_days": "byte_days",
}


def _parse_ts(value: str | None, loc: str, default: datetime) -> datetime:
    if value is None:
        return default
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise validation(["query", loc], "must be an RFC 3339 timestamp or a YYYY-MM-DD date") from e
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


@router.get("/usage", response_model=m.UsageReport, summary="Usage report")
def usage(
    request: Request,
    auth: ReadAuth,
    from_: Annotated[str | None, Query(alias="from", max_length=40)] = None,
    to: Annotated[str | None, Query(max_length=40)] = None,
    group_by: Literal["meter", "day", "job"] = "meter",
) -> Any:
    """`qt.usage_report` over `[from, to)` (default: the last 30 days)."""
    end = _parse_ts(to, "to", datetime.now(UTC))
    try:
        start = _parse_ts(from_, "from", end - timedelta(days=30))
    except OverflowError as e:
        raise validation(["query", "to"], "out of range") from e
    key = {
        "meter": "meter",
        "day": "to_char(created_at at time zone 'UTC', 'YYYY-MM-DD')",
        "job": "coalesce(job_id, 'none')",
    }[group_by]
    with tenant_tx(ctx(request).db(), auth) as conn:
        rows = conn.execute(
            f"select {key} as k, meter, sum(quantity) from usage_events "
            "where tenant_id = %s and created_at >= %s and created_at < %s group by 1, 2 order by 1, 2",
            (auth.tenant_id, start, end),
        ).fetchall()
    return {
        "schema": "qt.usage_report",
        "schema_version": 1,
        "from": rfc3339(start),
        "to": rfc3339(end),
        "group_by": group_by,
        "rows": [
            {"key": str(k), "meter": meter, "quantity": float(q), "unit": UNITS.get(meter, "units")}
            for k, meter, q in rows
        ],
    }


@router.get("/retention", response_model=m.Retention, summary="Retention settings")
def get_retention(request: Request, auth: ReadAuth) -> Any:
    """`qt.tenant.retention`."""
    with tenant_tx(ctx(request).db(), auth) as conn:
        row = fetch_one(conn, "select * from tenants where id = %s", (auth.tenant_id,))
    if row is None:
        raise not_found("tenant")
    return {"uploads_days": row["retention_uploads_days"], "outputs_days": row["retention_outputs_days"]}


@router.put("/retention", response_model=m.Retention, summary="Change retention")
async def put_retention(
    request: Request, body: m.Retention, auth: AdminAuth, idem: idempotency.OptIdemKey = None
) -> Any:
    """Sets the tenant's retention (1..365 days each); audited."""

    def execute(conn: Any) -> idempotency.Outcome:
        row = fetch_one(
            conn,
            "update tenants set retention_uploads_days = %s, retention_outputs_days = %s where id = %s returning *",
            (body.uploads_days, body.outputs_days, auth.tenant_id),
        )
        if row is None:
            raise not_found("tenant")
        audit_sync(conn, auth.tenant_id, auth.key_id, "retention.update", "tenant", auth.tenant_id)
        doc = {"uploads_days": row["retention_uploads_days"], "outputs_days": row["retention_outputs_days"]}
        return 200, doc, {}, None

    return await idempotency.run(request, auth, idem, execute)


@router.get("/tombstones", response_model=TombstonePage, summary="Deletion tombstones")
def tombstones(request: Request, auth: AdminReadAuth, limit: Limit = 50, starting_after: str | None = None) -> Any:
    """`qt.page` of tombstones (ids, counts, sha256s, time; no data)."""
    with tenant_tx(ctx(request).db(), auth) as conn:
        rows, more = page(conn, "tombstones", auth.tenant_id, limit, starting_after)
    return page_doc([tombstone_doc(r) for r in rows], more)
