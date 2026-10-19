"""Retention and deletion (SPEC 8.8, O-RET-1): the retention purge and DELETE endpoints remove objects and rows and
write tombstones; expired jobs answer 410 for their files; dataset registration and deletion jobs end to end."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import timedelta

import psycopg
import pytest
import pytest_asyncio
from svc_support import insert_job, make_svc, succeed_job, upload_bytes, verify_uploads

from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.db.runner import MIGRATIONS_DIR
from queuetruth_service.ids import ID_RE, new_id
from queuetruth_service.maintenance import run_retention
from queuetruth_service.queue import PgQueuerJobQueue, enqueue_sync
from queuetruth_service.store import dataset_prefix, job_prefix, upload_prefix
from queuetruth_service.worker import Worker

pytestmark = pytest.mark.requires_postgres

#: SPEC 4.5: tombstone ids are `del_<26-char ULID>` (open conflict 8: they were `tomb_`).
TOMB_ID = re.compile(r"^del_[0-9A-HJKMNP-TV-Z]{26}$")


def test_o_ret_1_retention_purge(svc):
    a = svc.tenants["A"]
    old_up = upload_bytes(svc, b"old-upload" * 50, filename="old.bin")
    new_up = upload_bytes(svc, b"new-upload" * 50, filename="new.bin")
    verify_uploads(svc)
    old_job, new_job = insert_job(svc), insert_job(svc)
    succeed_job(svc, old_job, {"a.parquet": b"aaa", "b/c.json": b"{}"})
    succeed_job(svc, new_job, {"a.parquet": b"bbb"})
    svc.db.q(
        "insert into replays (id, tenant_id, job_id, spec, summary) values ('rp_old', %s, %s, '{}', '{}')", (a, old_job)
    )
    r = svc.client.put("/v1/retention", json={"uploads_days": 7, "outputs_days": 3}, headers=svc.h())
    assert r.status_code == 200 and r.json() == {"uploads_days": 7, "outputs_days": 3}
    assert svc.client.get("/v1/retention", headers=svc.h()).json() == {"uploads_days": 7, "outputs_days": 3}
    assert (
        svc.client.put("/v1/retention", json={"uploads_days": 0, "outputs_days": 3}, headers=svc.h()).status_code == 422
    )
    svc.db.q("update uploads set created_at = now() - interval '8 days' where id = %s", (old_up,))
    svc.db.q("update jobs set finished_at = now() - interval '4 days' where id = %s", (old_job,))

    async def go():
        pool = await make_async_pool(svc.db.worker)
        try:
            return await run_retention(pool, svc.store)
        finally:
            await pool.close()

    rep = asyncio.run(go())[a]
    assert rep.uploads == [old_up] and rep.jobs == [old_job] and rep.objects == 3
    assert svc.store.list_prefix(upload_prefix(a, old_up)) == [] and svc.store.list_prefix(job_prefix(a, old_job)) == []
    assert svc.store.list_prefix(upload_prefix(a, new_up)) and svc.store.list_prefix(job_prefix(a, new_job))
    assert svc.db.q("select count(*) from uploads where id = %s", (old_up,)) == [(0,)]
    assert svc.db.q("select count(*) from job_files where job_id = %s", (old_job,)) == [(0,)]
    assert svc.db.q("select state from jobs where id = %s", (old_job,)) == [("expired",)]
    assert svc.db.q("select summary from replays where id = 'rp_old'") == [(None,)]
    p = svc.client.get(f"/v1/jobs/{old_job}/files", headers=svc.h())
    assert p.status_code == 410 and p.headers["content-type"] == "application/problem+json"
    assert svc.client.get(f"/v1/jobs/{old_job}/files/a.parquet", headers=svc.h()).status_code == 410
    assert svc.client.get(f"/v1/uploads/{old_up}", headers=svc.h()).status_code == 404
    tombs = svc.client.get("/v1/tombstones", headers=svc.h()).json()["data"]
    by_res = {t["resource_id"]: t for t in tombs}
    assert by_res[old_up]["resource_type"] == "upload" and by_res[old_up]["object_count"] == 1
    assert len(by_res[old_up]["sha256s"]) == 1
    assert by_res[old_job]["resource_type"] == "job_outputs" and by_res[old_job]["object_count"] == 2
    assert len(by_res[old_job]["sha256s"]) == 2
    assert "old-upload" not in str(tombs)
    assert all(TOMB_ID.match(t["id"]) for t in tombs), [t["id"] for t in tombs]  # purge path (async writer)
    actions = {a for (a,) in svc.db.q("select action from audit_log")}
    assert {"retention.purge", "retention.update"} <= actions


def test_o_ret_1_delete_upload(svc):
    up = upload_bytes(svc, b"x" * 3000, filename="del.bin")
    pending = svc.client.post(
        "/v1/uploads",
        json={"purpose": "other", "filename": "p.bin", "size_bytes": 5000, "sha256": "1" * 64},
        headers=svc.h(Idempotency_Key="pend"),
    ).json()["id"]
    a = svc.tenants["A"]
    for uid in (up, pending):
        assert svc.client.delete(f"/v1/uploads/{uid}", headers=svc.h()).status_code == 204
        assert svc.client.get(f"/v1/uploads/{uid}", headers=svc.h()).status_code == 404
        assert svc.client.delete(f"/v1/uploads/{uid}", headers=svc.h()).status_code == 404
        assert svc.store.list_prefix(upload_prefix(a, uid)) == []
    assert not (svc.store.staging / upload_prefix(a, pending).rstrip("/")).exists()
    t = {x["resource_id"]: x for x in svc.client.get("/v1/tombstones", headers=svc.h()).json()["data"]}
    assert t[up]["object_count"] == 1 and t[pending]["object_count"] == 0 and t[pending]["sha256s"] == []
    assert all(TOMB_ID.match(x["id"]) for x in t.values()), list(t.values())  # DELETE path (sync writer)


def test_tombstone_ids_follow_the_id_grammar(svc):
    """SPEC 4.5 (open conflict 8): `del_` is the tombstone prefix; `tomb_` is no longer a known prefix, and migration
    0006 renames existing `tomb_` rows keeping their ULID (pagination order unchanged)."""
    assert ID_RE.match(new_id("del")) and TOMB_ID.match(new_id("del"))
    with pytest.raises(ValueError, match="unknown id prefix"):
        new_id("tomb")
    a = svc.tenants["A"]
    ulids = ["01JABCDEFGHJKMNPQRSTVWXYZ0", "01JABCDEFGHJKMNPQRSTVWXYZ1"]
    with psycopg.connect(svc.db.owner, autocommit=True) as c:
        for u in ulids:
            c.execute(
                "insert into tombstones (id, tenant_id, resource_type, resource_id, object_count, sha256s) "
                "values (%s, %s, 'upload', %s, 0, '{}')",
                (f"tomb_{u}", a, f"up_{u}"),
            )
        c.execute((MIGRATIONS_DIR / "0006_tombstone_ids.sql").read_text())
    data = svc.client.get("/v1/tombstones", headers=svc.h()).json()["data"]
    assert [x["id"] for x in data] == [f"del_{u}" for u in reversed(ulids)]  # newest first
    page1 = svc.client.get("/v1/tombstones?limit=1", headers=svc.h()).json()
    assert page1["data"][0]["id"] == f"del_{ulids[1]}" and page1["has_more"]
    nxt = svc.client.get(f"/v1/tombstones?limit=1&starting_after={page1['next_cursor']}", headers=svc.h()).json()
    assert [x["id"] for x in nxt["data"]] == [f"del_{ulids[0]}"]


@pytest_asyncio.fixture
async def wsvc(db, tmp_path):
    gen = make_svc(db, tmp_path)
    svc = next(gen)
    svc.client.app.state.ctx.enqueue = lambda job_id: enqueue_sync(svc.pool, job_id)
    pool = await make_async_pool(db.worker, max_size=8)
    queue = PgQueuerJobQueue(db.worker, heartbeat_timeout=timedelta(seconds=2), dequeue_timeout=timedelta(seconds=1))
    s = svc.settings.model_copy(update={"heartbeat_s": 0.3})
    worker = Worker(
        s,
        pool,
        svc.store,
        queue,
        targets={
            "dataset_validate": "queuetruth_service.testing:fake_ingest",
            "replay": "queuetruth_service.testing:fake_replay",
        },
    )
    stop = asyncio.Event()
    task = asyncio.create_task(queue.run(worker.handle, concurrency=2, stop=stop))
    yield svc
    stop.set()
    await asyncio.wait_for(task, 15)
    await queue.close()
    await pool.close()
    gen.close()


async def wait_state(svc, job: str, state: str, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if svc.db.q("select state from jobs where id = %s", (job,)) == [(state,)]:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(svc.db.q("select state, error from jobs where id = %s", (job,)))


@pytest.mark.asyncio
async def test_dataset_register_use_and_delete(wsvc):
    svc = wsvc
    a = svc.tenants["A"]
    up = await asyncio.to_thread(upload_bytes, svc, b"mbo-bytes" * 200, "A", "day.itch.gz", "market_data")
    body = {"name": "day1", "kind": "mbo", "format": "itch", "venue": "nasdaq", "upload_ids": [up]}
    r = svc.client.post("/v1/datasets", json=body, headers=svc.h(Idempotency_Key="ds"))
    assert r.status_code == 409 and r.json()["type"].endswith("/upload-not-verified")
    await asyncio.to_thread(verify_uploads, svc)
    r = svc.client.post(
        "/v1/datasets", json=body | {"ingest": {"session_date": "2026-01-02"}}, headers=svc.h(Idempotency_Key="ds")
    )
    assert r.status_code == 202, r.text
    ds = r.json()
    assert ds["status"] == "validating" and r.headers["location"] == f"/v1/jobs/{ds['job_id']}"
    await wait_state(svc, ds["job_id"], "succeeded")
    got = svc.client.get(f"/v1/datasets/{ds['id']}", headers=svc.h()).json()
    assert got["status"] == "ready" and got["manifest"]["source_format"] == "itch"
    assert got["data_quality"]["verdict"] == "clean"
    keys = [k for k, _ in svc.store.list_prefix(dataset_prefix(a, ds["id"]))]
    assert keys[-1].endswith("mbo/date=2026-01-02/instrument_id=1/part-00000.parquet")
    files = svc.client.get(f"/v1/jobs/{ds['job_id']}/files", headers=svc.h()).json()["files"]
    assert [f["name"] for f in files] == ["ingest_summary.json"]
    job = svc.client.get(f"/v1/jobs/{ds['job_id']}", headers=svc.h()).json()
    assert job["kind"] == "dataset_validate" and job["resource"] == {"type": "dataset", "id": ds["id"]}
    # use it
    r = svc.client.post("/v1/replays", json={"dataset": {"dataset_id": ds["id"]}}, headers=svc.h(Idempotency_Key="rp"))
    await wait_state(svc, r.json()["job"]["id"], "succeeded")
    # delete it
    r = svc.client.delete(f"/v1/datasets/{ds['id']}", headers=svc.h())
    assert r.status_code == 202 and r.json()["kind"] == "deletion"
    await wait_state(svc, r.json()["id"], "succeeded")
    assert svc.client.get(f"/v1/datasets/{ds['id']}", headers=svc.h()).status_code == 404
    assert svc.store.list_prefix(dataset_prefix(a, ds["id"])) == []
    t = [x for x in svc.client.get("/v1/tombstones", headers=svc.h()).json()["data"] if x["resource_id"] == ds["id"]]
    assert t and t[0]["resource_type"] == "dataset" and t[0]["object_count"] == 3 and len(t[0]["sha256s"]) == 1
    assert svc.client.delete(f"/v1/datasets/{ds['id']}", headers=svc.h()).status_code == 404
