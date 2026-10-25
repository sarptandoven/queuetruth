"""Retention and deletion (SPEC 8.8). Uploads and job outputs expire after the tenant's retention (default 30 days,
1..365): the daily purge deletes objects and rows, sets jobs `expired` and writes tombstones (ids, object counts,
sha256s, time; no data). `DELETE` endpoints hard-delete through the same helpers. `audit_log` records deletions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from queuetruth_service.ids import TOMBSTONE_PREFIX, new_id
from queuetruth_service.store import ObjectStore, dataset_prefix, job_prefix, upload_prefix

RUN_TABLES = ("replays", "calibrations", "scorecards", "reports", "zoos")


@dataclass
class PurgeReport:
    """What one purge removed."""

    uploads: list[str] = field(default_factory=list)
    jobs: list[str] = field(default_factory=list)
    objects: int = 0


def tombstone_sync(conn: psycopg.Connection, tenant: str, rtype: str, rid: str, count: int, sha: list[str]) -> str:
    """Writes a tombstone (sync connection)."""
    tid = new_id(TOMBSTONE_PREFIX)
    conn.execute(
        "insert into tombstones (id, tenant_id, resource_type, resource_id, object_count, sha256s) "
        "values (%s, %s, %s, %s, %s, %s)",
        (tid, tenant, rtype, rid, count, sha),
    )
    return tid


def audit_sync(conn: psycopg.Connection, tenant: str, actor: str | None, action: str, rtype: str, rid: str) -> None:
    """Appends to `audit_log` (sync connection)."""
    conn.execute(
        "insert into audit_log (id, tenant_id, actor_key_id, action, resource_type, resource_id) "
        "values (%s, %s, %s, %s, %s, %s)",
        (new_id("aud"), tenant, actor, action, rtype, rid),
    )


async def tombstone(
    conn: psycopg.AsyncConnection, tenant: str, rtype: str, rid: str, count: int, sha: list[str]
) -> None:
    """Writes a tombstone (async connection)."""
    await conn.execute(
        "insert into tombstones (id, tenant_id, resource_type, resource_id, object_count, sha256s) "
        "values (%s, %s, %s, %s, %s, %s)",
        (new_id(TOMBSTONE_PREFIX), tenant, rtype, rid, count, sha),
    )


async def audit(conn: psycopg.AsyncConnection, tenant: str, action: str, rtype: str, rid: str) -> None:
    """Appends to `audit_log` (async connection; actor = system)."""
    await conn.execute(
        "insert into audit_log (id, tenant_id, actor_key_id, action, resource_type, resource_id) "
        "values (%s, %s, null, %s, %s, %s)",
        (new_id("aud"), tenant, action, rtype, rid),
    )


def delete_upload_sync(
    conn: psycopg.Connection, store: ObjectStore, tenant: str, row: dict[str, Any], actor: str | None
) -> None:
    """Hard-deletes one upload (objects, staged parts, row) and writes a tombstone."""
    if row.get("multipart_id") and row["status"] == "pending":
        store.abort_multipart(row["object_key"], row["multipart_id"])
    n = store.delete_prefix(upload_prefix(tenant, row["id"]))
    conn.execute("delete from uploads where id = %s and tenant_id = %s", (row["id"], tenant))
    sha = [row["sha256"]] if row["status"] in ("complete", "verified") else []
    tombstone_sync(conn, tenant, "upload", row["id"], n, sha)
    audit_sync(conn, tenant, actor, "upload.delete", "upload", row["id"])


def _manifest_sha256s(manifest: Any) -> list[str]:
    out: list[str] = []
    if isinstance(manifest, dict):
        for part in manifest.get("partitions") or []:
            for f in part.get("files") or []:
                if isinstance(f, dict) and isinstance(f.get("sha256"), str):
                    out.append(f["sha256"])
    return sorted(out)


async def delete_dataset(conn: psycopg.AsyncConnection, store: ObjectStore, tenant: str, dataset_id: str) -> int:
    """Deletion job body: objects under the dataset prefix, the row, a tombstone. Returns the object count."""
    row = await (
        await conn.cursor(row_factory=dict_row).execute(
            "select * from datasets where id = %s and tenant_id = %s", (dataset_id, tenant)
        )
    ).fetchone()
    n = await asyncio.to_thread(store.delete_prefix, dataset_prefix(tenant, dataset_id))
    if row is not None:
        await conn.execute("delete from datasets where id = %s and tenant_id = %s", (dataset_id, tenant))
    await tombstone(conn, tenant, "dataset", dataset_id, n, _manifest_sha256s(row["manifest"] if row else None))
    await audit(conn, tenant, "dataset.delete", "dataset", dataset_id)
    return n


async def purge_tenant(conn: psycopg.AsyncConnection, store: ObjectStore, tenant: str) -> PurgeReport:
    """Applies the tenant's retention (runs inside the tenant transaction)."""
    rep = PurgeReport()
    cur = conn.cursor(row_factory=dict_row)
    t = await (await cur.execute("select * from tenants where id = %s", (tenant,))).fetchone()
    if t is None:
        return rep
    uploads = await (
        await cur.execute(
            "select * from uploads where tenant_id = %s and created_at < now() - make_interval(days => %s) order by id",
            (tenant, t["retention_uploads_days"]),
        )
    ).fetchall()
    for u in uploads:
        if u.get("multipart_id") and u["status"] == "pending":
            await asyncio.to_thread(store.abort_multipart, u["object_key"], u["multipart_id"])
        n = await asyncio.to_thread(store.delete_prefix, upload_prefix(tenant, u["id"]))
        await conn.execute("delete from uploads where id = %s and tenant_id = %s", (u["id"], tenant))
        sha = [u["sha256"]] if u["status"] in ("complete", "verified") else []
        await tombstone(conn, tenant, "upload", u["id"], n, sha)
        await audit(conn, tenant, "retention.purge", "upload", u["id"])
        rep.uploads.append(u["id"])
        rep.objects += n
    jobs = await (
        await cur.execute(
            "select id from jobs where tenant_id = %s and state in ('succeeded', 'failed', 'canceled') "
            "and finished_at < now() - make_interval(days => %s) order by id",
            (tenant, t["retention_outputs_days"]),
        )
    ).fetchall()
    for j in jobs:
        files = await (
            await cur.execute(
                "select sha256 from job_files where job_id = %s and tenant_id = %s order by name", (j["id"], tenant)
            )
        ).fetchall()
        n = await asyncio.to_thread(store.delete_prefix, job_prefix(tenant, j["id"]))
        await conn.execute("delete from job_files where job_id = %s and tenant_id = %s", (j["id"], tenant))
        for table in RUN_TABLES:
            await conn.execute(
                f"update {table} set summary = null where job_id = %s and tenant_id = %s", (j["id"], tenant)
            )
        await conn.execute(
            "update jobs set state = 'expired', progress = jsonb_build_object('fraction', 1.0, 'stage', 'expired') "
            "where id = %s and tenant_id = %s",
            (j["id"], tenant),
        )
        await tombstone(conn, tenant, "job_outputs", j["id"], n, [f["sha256"] for f in files])
        await audit(conn, tenant, "retention.purge", "job", j["id"])
        rep.jobs.append(j["id"])
        rep.objects += n
    return rep
