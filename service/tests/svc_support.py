"""Seeding helpers for the service tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from fastapi.testclient import TestClient

from queuetruth_service.admin import create_tenant
from queuetruth_service.app import create_app
from queuetruth_service.db.pool import make_pool
from queuetruth_service.settings import Settings
from queuetruth_service.store.local import LocalStore

UNLIMITED = {"read": (1e6, 1e6), "write": (1e6, 1e6), "submit": (1e6, 1e6)}


@dataclass
class Db:
    """One per-test database: owner DSN, `qt_app` (api) DSN and `qt_worker` (worker) DSN."""

    name: str
    owner: str
    app: str
    worker: str

    def q(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """Runs a statement as the owner and returns rows."""
        with psycopg.connect(self.owner, autocommit=True) as c:
            cur = c.execute(sql, params)
            return cur.fetchall() if cur.description else []


def settings_for(tmp: Path, **kw: Any) -> Settings:
    """Test settings (local store under `tmp`, generous rate limits unless overridden)."""
    base: dict[str, Any] = {
        "store": "local",
        "store_root": str(tmp / "store"),
        "rate_limits": UNLIMITED,
        "part_size_bytes": 1024,
        "max_upload_bytes": 10 * 1024 * 1024,
        "auth_failure_limit": (600, 50),  # generous for tests, still finite (security review L4)
    }
    return Settings(**(base | kw))


@dataclass
class Svc:
    """An in-process API with tenants A and B."""

    client: TestClient
    db: Db
    settings: Settings
    store: LocalStore
    pool: Any
    keys: dict[str, str] = field(default_factory=dict)
    tenants: dict[str, str] = field(default_factory=dict)
    enqueued: list[str] = field(default_factory=list)

    def h(self, who: str = "A", **extra: str) -> dict[str, str]:
        """Bearer header for a named key plus extra headers (`Idempotency_Key` -> `Idempotency-Key`)."""
        hdr = {"Authorization": f"Bearer {self.keys[who]}"}
        hdr.update({k.replace("_", "-"): v for k, v in extra.items()})
        return hdr


def make_svc(db: Db, tmp: Path, **settings_kw: Any) -> Iterator[Svc]:
    s = settings_for(tmp, **settings_kw)
    store = LocalStore(Path(s.store_root), s.signing_secret, s.public_base_url)
    pool = make_pool(db.app, max_size=10)
    enq: list[str] = []
    app = create_app(s, pool=pool, store=store, enqueue=enq.append)
    svc = Svc(TestClient(app, raise_server_exceptions=False), db, s, store, pool, enqueued=enq)
    for t in ("A", "B"):
        out = create_tenant(db.owner, s, f"tenant {t}")
        svc.tenants[t] = out["tenant_id"]
        svc.keys[t] = out["key"]
    try:
        yield svc
    finally:
        svc.client.close()
        pool.close()


def seed_dataset(svc: Svc, who: str = "A", status: str = "ready", files: dict[str, bytes] | None = None) -> str:
    """Inserts a dataset row (and its objects) for tenant `who`; returns its id."""
    import json as _json

    from queuetruth_service.ids import new_id
    from queuetruth_service.store import dataset_prefix

    tenant = svc.tenants[who]
    ds = new_id("ds")
    files = files or {"manifest.json": b"{}", "mbo/date=2026-01-02/instrument_id=1/part-00000.parquet": b"x" * 100}
    for name, data in files.items():
        p = svc.store.path(dataset_prefix(tenant, ds) + name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    from queuetruth_service.testing import manifest_doc

    manifest = manifest_doc("itch", "nasdaq", "a" * 64, 1000)
    svc.db.q(
        "insert into datasets (id, tenant_id, name, kind, format, venue, status, upload_ids, manifest, object_prefix) "
        "values (%s, %s, 'd', 'mbo', 'itch', 'nasdaq', %s, '{}', %s, %s)",
        (ds, tenant, status, _json.dumps(manifest), dataset_prefix(tenant, ds)),
    )
    return ds


def upload_bytes(svc: Svc, data: bytes, who: str = "A", filename: str = "data.bin", purpose: str = "other") -> str:
    """Full multipart upload through the API (local store); returns the upload id (status complete)."""
    import hashlib
    import math
    from urllib.parse import urlsplit

    body = {
        "purpose": purpose,
        "filename": filename,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    r = svc.client.post(
        "/v1/uploads", json=body, headers=svc.h(who, Idempotency_Key=f"up-{hashlib.md5(data).hexdigest()}-{filename}")
    )
    assert r.status_code == 201, r.text
    up = r.json()
    part = svc.settings.part_size_bytes
    assert len(up["parts"]) == max(1, math.ceil(len(data) / part))
    done = []
    for p in up["parts"]:
        u = urlsplit(p["url"])
        chunk = data[(p["part_number"] - 1) * part : p["part_number"] * part]
        rr = svc.client.put(f"{u.path}?{u.query}", content=chunk)
        assert rr.status_code == 200, rr.text
        done.append({"part_number": p["part_number"], "etag": rr.headers["etag"]})
    r = svc.client.post(
        f"/v1/uploads/{up['id']}/complete", json={"parts": done}, headers=svc.h(who, Idempotency_Key=f"c-{up['id']}")
    )
    assert r.status_code == 200 and r.json()["status"] == "complete", r.text
    return up["id"]


def succeed_job(svc: Svc, job_id: str, files: dict[str, bytes], who: str = "A") -> None:
    """Marks a job succeeded with output files (rows + objects), as the worker would."""
    import hashlib

    from queuetruth_service.store import job_key

    tenant = svc.tenants[who]
    for name, data in files.items():
        key = job_key(tenant, job_id, name)
        p = svc.store.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        svc.db.q(
            "insert into job_files (job_id, tenant_id, name, bytes, sha256, content_type, object_key) "
            "values (%s, %s, %s, %s, %s, 'application/octet-stream', %s)",
            (job_id, tenant, name, len(data), hashlib.sha256(data).hexdigest(), key),
        )
    svc.db.q(
        "update jobs set state = 'succeeded', started_at = now(), finished_at = now(), "
        "files_expire_at = now() + interval '30 days' where id = %s",
        (job_id,),
    )


def verify_uploads(svc: Svc) -> dict[str, str]:
    """Runs the worker's upload verification pass."""
    import asyncio

    from queuetruth_service.db.pool import make_async_pool
    from queuetruth_service.maintenance import verify_uploads as _verify

    async def go() -> dict[str, str]:
        pool = await make_async_pool(svc.db.worker)
        try:
            return await _verify(pool, svc.store)
        finally:
            await pool.close()

    return asyncio.run(go())


def insert_job(svc: Svc, who: str = "A", kind: str = "replay", state: str = "queued") -> str:
    """Inserts a job row directly; returns its id."""
    from queuetruth_service.ids import new_id

    jid = new_id("job")
    svc.db.q(
        "insert into jobs (id, tenant_id, kind, state, resource_type, resource_id) values (%s, %s, %s, %s, %s, 'rp_x')",
        (jid, svc.tenants[who], kind, state, kind),
    )
    return jid
