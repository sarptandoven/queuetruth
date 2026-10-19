"""Worker maintenance passes. Each pass iterates tenants (`qt_tenant_ids()`, worker role only) and works inside that
tenant's bound transaction (`TenantBinding`): upload verification, expiry of abandoned pending uploads, idempotency
pruning (24 h), usage push through the biller, the daily retention purge and the daily `storage_byte_days` meter.

Security review M1: verification compares the stored size first and fails a mismatched upload without hashing it;
a failed upload's objects are deleted at once; the storage meter bills the bytes the store actually holds (objects
under the tenant prefix plus the parts of pending uploads), never the declared sizes."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from queuetruth_service.billing import Biller, NoopBiller, UsageEvent
from queuetruth_service.db import TenantBinding, tenant_tx_async
from queuetruth_service.ids import new_id
from queuetruth_service.retention import PurgeReport, purge_tenant
from queuetruth_service.store import NotFoundError, ObjectStore, tenant_prefix, upload_prefix

log = logging.getLogger("queuetruth_service.maintenance")


async def tenant_ids(pool: AsyncConnectionPool) -> list[str]:
    """Every tenant id."""
    async with pool.connection() as conn:
        rows = await (await conn.execute("select id from qt_tenant_ids()")).fetchall()
    return [r[0] for r in rows]


async def verify_uploads(pool: AsyncConnectionPool, store: ObjectStore) -> dict[str, str]:
    """Checks completed uploads: `verified` when the stored size and sha256 match the declaration, else `failed`
    (objects deleted). A size mismatch is decided from `head` alone. Returns id -> status."""
    out: dict[str, str] = {}
    for tenant in await tenant_ids(pool):
        async with tenant_tx_async(pool, TenantBinding(tenant)) as conn:
            rows = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "select * from uploads where tenant_id = %s and status = 'complete' order by id "
                    "for update skip locked",
                    (tenant,),
                )
            ).fetchall()
            for u in rows:
                size: int | None = None
                try:
                    size = int((await asyncio.to_thread(store.head, u["object_key"]))["bytes"])
                    ok = size == int(u["size_bytes"])
                    if ok:
                        ok = await asyncio.to_thread(store.sha256, u["object_key"]) == u["sha256"]
                except NotFoundError:
                    ok = False
                status = "verified" if ok else "failed"
                if not ok:
                    await asyncio.to_thread(store.delete_prefix, upload_prefix(tenant, u["id"]))
                await conn.execute(
                    "update uploads set status = %s, stored_bytes = %s where id = %s and tenant_id = %s",
                    (status, size if ok else 0, u["id"], tenant),
                )
                out[u["id"]] = status
    return out


async def expire_pending_uploads(pool: AsyncConnectionPool, store: ObjectStore, ttl_s: int) -> list[str]:
    """Aborts uploads still pending after `ttl_s` (their parts are deleted) and marks them `failed`."""
    expired: list[str] = []
    for tenant in await tenant_ids(pool):
        async with tenant_tx_async(pool, TenantBinding(tenant)) as conn:
            rows = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "select * from uploads where tenant_id = %s and status = 'pending' "
                    "and created_at < now() - make_interval(secs => %s) order by id for update skip locked",
                    (tenant, ttl_s),
                )
            ).fetchall()
            for u in rows:
                if u.get("multipart_id"):
                    await asyncio.to_thread(store.abort_multipart, u["object_key"], u["multipart_id"])
                await asyncio.to_thread(store.delete_prefix, upload_prefix(tenant, u["id"]))
                await conn.execute(
                    "update uploads set status = 'failed', stored_bytes = 0 where id = %s and tenant_id = %s",
                    (u["id"], tenant),
                )
                expired.append(u["id"])
    return expired


async def prune_idempotency(pool: AsyncConnectionPool, max_age_h: int = 24) -> int:
    """Deletes idempotency records older than `max_age_h`."""
    n = 0
    for tenant in await tenant_ids(pool):
        async with tenant_tx_async(pool, TenantBinding(tenant)) as conn:
            cur = await conn.execute(
                "delete from idempotency_keys where tenant_id = %s and created_at < now() - make_interval(hours => %s)",
                (tenant, max_age_h),
            )
            n += cur.rowcount
    return n


async def push_usage(pool: AsyncConnectionPool, biller: Biller, *, batch: int = 500) -> list[str]:
    """Pushes unpushed usage events; acknowledged rows get `pushed_at` and are never sent again."""
    pushed: list[str] = []
    for tenant in await tenant_ids(pool):
        async with tenant_tx_async(pool, TenantBinding(tenant)) as conn:
            cur = conn.cursor(row_factory=dict_row)
            t = await (await cur.execute("select * from tenants where id = %s", (tenant,))).fetchone()
            rows = await (
                await cur.execute(
                    "select * from usage_events where tenant_id = %s and pushed_at is null "
                    "order by created_at, id limit %s for update skip locked",
                    (tenant, batch),
                )
            ).fetchall()
            if not rows or t is None:
                continue
            events = [
                UsageEvent(
                    identifier=r["identifier"],
                    tenant_id=tenant,
                    meter=r["meter"],
                    quantity=float(r["quantity"]),
                    unit=r["unit"],
                    created_at=r["created_at"],
                    customer_id=t.get("stripe_customer_id"),
                )
                for r in rows
            ]
            b: Biller = NoopBiller() if t["zero_egress"] else biller
            results = await asyncio.to_thread(b.push, events)
            for res in results:
                if res.ok:
                    await conn.execute(
                        "update usage_events set pushed_at = now(), push_error = null "
                        "where identifier = %s and tenant_id = %s",
                        (res.identifier, tenant),
                    )
                    pushed.append(res.identifier)
                else:
                    await conn.execute(
                        "update usage_events set push_error = %s where identifier = %s and tenant_id = %s",
                        ((res.error or "error")[:500], res.identifier, tenant),
                    )
    return pushed


async def run_retention(pool: AsyncConnectionPool, store: ObjectStore) -> dict[str, PurgeReport]:
    """Retention purge for every tenant."""
    out = {}
    for tenant in await tenant_ids(pool):
        async with tenant_tx_async(pool, TenantBinding(tenant)) as conn:
            out[tenant] = await purge_tenant(conn, store, tenant)
    return out


async def measured_bytes(conn: Any, store: ObjectStore, tenant: str) -> int:
    """Bytes the store holds for a tenant: every object under `t/{tenant}/` plus staged parts of pending uploads."""
    total = sum(b for _, b in await asyncio.to_thread(store.list_prefix, tenant_prefix(tenant)))
    pending = await (
        await conn.execute(
            "select object_key, multipart_id from uploads where tenant_id = %s and status = 'pending' "
            "and multipart_id is not null",
            (tenant,),
        )
    ).fetchall()
    for key, mp_id in pending:
        total += await asyncio.to_thread(store.staged_bytes, key, mp_id)
    return int(total)


async def storage_meter(pool: AsyncConnectionPool, store: ObjectStore, day: str | None = None) -> dict[str, float]:
    """Inserts one `storage_byte_days` event per tenant for `day` with the measured bytes (idempotent per tenant and
    day)."""
    day = day or datetime.now(UTC).strftime("%Y-%m-%d")
    out = {}
    for tenant in await tenant_ids(pool):
        async with tenant_tx_async(pool, TenantBinding(tenant)) as conn:
            total = float(await measured_bytes(conn, store, tenant))
            await conn.execute(
                "insert into usage_events (id, tenant_id, job_id, meter, quantity, unit, identifier) "
                "values (%s, %s, null, 'storage_byte_days', %s, 'byte_days', %s) on conflict (identifier) do nothing",
                (new_id("use"), tenant, total, f"storage:{tenant}:{day}:storage_byte_days"),
            )
            out[tenant] = total
    return out
