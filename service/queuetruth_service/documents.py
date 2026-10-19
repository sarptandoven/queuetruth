"""Row -> `qt.*` document conversion and cursor pagination shared by the routers."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from queuetruth_service.ids import rfc3339
from queuetruth_service.store import ObjectStore


def job_doc(r: dict[str, Any]) -> dict[str, Any]:
    """`qt.job` from a `jobs` row."""
    progress = r.get("progress") or {"fraction": 0.0, "stage": r["state"]}
    return {
        "schema": "qt.job",
        "schema_version": 1,
        "id": r["id"],
        "kind": r["kind"],
        "state": r["state"],
        "progress": {"fraction": float(progress.get("fraction", 0.0)), "stage": str(progress.get("stage", ""))},
        "resource": {"type": r["resource_type"], "id": r["resource_id"]},
        "created_at": rfc3339(r["created_at"]),
        "started_at": rfc3339(r.get("started_at")),
        "finished_at": rfc3339(r.get("finished_at")),
        "error": r.get("error"),
        "usage": {k: float(v) for k, v in (r.get("usage") or {}).items()},
        "files_expire_at": rfc3339(r.get("files_expire_at")),
    }


def key_doc(r: dict[str, Any]) -> dict[str, Any]:
    """`qt.api_key` from an `api_keys` row (never the hash)."""
    return {
        "schema": "qt.api_key",
        "schema_version": 1,
        "id": r["id"],
        "prefix": r["prefix"],
        "scopes": list(r["scopes"]),
        "created_at": rfc3339(r["created_at"]),
        "last_used_at": rfc3339(r.get("last_used_at")),
        "revoked_at": rfc3339(r.get("revoked_at")),
    }


def tenant_doc(r: dict[str, Any]) -> dict[str, Any]:
    """`qt.tenant`."""
    return {
        "schema": "qt.tenant",
        "schema_version": 1,
        "id": r["id"],
        "name": r["name"],
        "plan": r["plan"],
        "zero_egress": bool(r["zero_egress"]),
        "retention": {"uploads_days": r["retention_uploads_days"], "outputs_days": r["retention_outputs_days"]},
    }


def upload_doc(r: dict[str, Any], store: ObjectStore | None, *, part_urls: bool = True) -> dict[str, Any]:
    """`qt.upload`; a pending upload carries fresh part URLs when `part_urls` (callers with the write scope only:
    a PUT URL is a write credential, security review L1)."""
    parts: list[dict[str, Any]] = []
    if part_urls and r["status"] == "pending" and store is not None and r.get("multipart_id"):
        n = max(1, -(-int(r["size_bytes"]) // int(r["part_size"])))
        parts = [
            {"part_number": p.part_number, "url": p.url, "expires_at": rfc3339(p.expires_at)}
            for p in store.part_urls(r["object_key"], r["multipart_id"], n)
        ]
    return {
        "schema": "qt.upload",
        "schema_version": 1,
        "id": r["id"],
        "purpose": r["purpose"],
        "filename": r["filename"],
        "size_bytes": int(r["size_bytes"]),
        "sha256": r["sha256"],
        "status": r["status"],
        "parts": parts,
        "created_at": rfc3339(r["created_at"]),
    }


def dataset_doc(r: dict[str, Any]) -> dict[str, Any]:
    """`qt.dataset`."""
    return {
        "schema": "qt.dataset",
        "schema_version": 1,
        "id": r["id"],
        "name": r["name"],
        "kind": r["kind"],
        "format": r["format"],
        "venue": r["venue"],
        "status": r["status"],
        "upload_ids": list(r["upload_ids"]),
        "manifest": r.get("manifest"),
        "data_quality": r.get("data_quality"),
        "created_at": rfc3339(r["created_at"]),
        "job_id": r.get("job_id"),
    }


def run_doc(r: dict[str, Any]) -> dict[str, Any]:
    """`{id, spec, job_id, summary?}` of a replay, calibration or scorecard."""
    return {"id": r["id"], "spec": r["spec"], "job_id": r["job_id"], "summary": r.get("summary")}


def tombstone_doc(r: dict[str, Any]) -> dict[str, Any]:
    """Tombstone item."""
    return {
        "id": r["id"],
        "resource_type": r["resource_type"],
        "resource_id": r["resource_id"],
        "deleted_at": rfc3339(r["deleted_at"]),
        "object_count": int(r["object_count"]),
        "sha256s": list(r["sha256s"]),
    }


def page(
    conn: psycopg.Connection,
    table: str,
    tenant_id: str,
    limit: int,
    starting_after: str | None,
    filters: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Newest-first page of `table` for one tenant (explicit filter, and RLS): rows and `has_more`."""
    where = [sql.SQL("tenant_id = %s")]
    params: list[Any] = [tenant_id]
    if starting_after:
        where.append(sql.SQL("id < %s"))
        params.append(starting_after)
    for col, val in (filters or {}).items():
        if val is not None:
            where.append(sql.SQL("{} = %s").format(sql.Identifier(col)))
            params.append(val)
    q = sql.SQL("select * from {} where {} order by id desc limit %s").format(
        sql.Identifier(table), sql.SQL(" and ").join(where)
    )
    rows = conn.cursor(row_factory=dict_row).execute(q, [*params, limit + 1]).fetchall()
    return rows[:limit], len(rows) > limit


def page_doc(items: list[dict[str, Any]], has_more: bool) -> dict[str, Any]:
    """`qt.page`."""
    return {
        "schema": "qt.page",
        "schema_version": 1,
        "data": items,
        "has_more": has_more,
        "next_cursor": items[-1]["id"] if has_more and items else None,
    }


def fetch_one(conn: psycopg.Connection, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    """One row as a dict."""
    return conn.cursor(row_factory=dict_row).execute(query, params).fetchone()
