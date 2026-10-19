"""Gate-2 integration: a real replay job on `tests/fixtures/synth_small` through the API, the
PgQueuer worker and the child process calling `queuetruth.replay`; outputs download with verified sha256 and the
summary equals a direct `qt.replay` run (excluding `timings` and the spec echo)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from svc_support import make_svc

from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.ids import new_id
from queuetruth_service.queue import PgQueuerJobQueue, enqueue_sync
from queuetruth_service.store import dataset_prefix
from queuetruth_service.worker import Worker

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "synth_small"
pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres, pytest.mark.requires_core, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def live(db, tmp_path):
    if not (FIXTURE / "manifest.json").exists():
        pytest.skip("tests/fixtures/synth_small is missing (make fixtures)")
    gen = make_svc(db, tmp_path)
    svc = next(gen)
    svc.client.app.state.ctx.enqueue = lambda job_id: enqueue_sync(svc.pool, job_id)
    pool = await make_async_pool(db.worker, max_size=8)
    queue = PgQueuerJobQueue(db.worker, heartbeat_timeout=timedelta(seconds=5), dequeue_timeout=timedelta(seconds=1))
    worker = Worker(svc.settings.model_copy(update={"heartbeat_s": 1.0}), pool, svc.store, queue)
    stop = asyncio.Event()
    task = asyncio.create_task(queue.run(worker.handle, concurrency=1, stop=stop))
    yield svc
    stop.set()
    await asyncio.wait_for(task, 30)
    await queue.close()
    await pool.close()
    gen.close()


async def test_real_replay_job_on_synth_small(live):
    svc = live
    tenant = svc.tenants["A"]
    ds = new_id("ds")
    for p in sorted(FIXTURE.rglob("*")):
        if p.is_file():
            svc.store.put_file(dataset_prefix(tenant, ds) + p.relative_to(FIXTURE).as_posix(), p)
    svc.db.q(
        "insert into datasets (id, tenant_id, name, kind, format, venue, status, upload_ids, manifest, object_prefix) "
        "values (%s, %s, 'synth_small', 'mbo', 'parquet', 'cme', 'ready', '{}', %s, %s)",
        (ds, tenant, (FIXTURE / "manifest.json").read_text(), dataset_prefix(tenant, ds)),
    )
    spec = {"dataset": {"dataset_id": ds}, "rank_oracle": True}
    r = svc.client.post("/v1/replays", json=spec, headers=svc.h(Idempotency_Key="int"))
    assert r.status_code == 202, r.text
    job, rp = r.json()["job"]["id"], r.json()["id"]
    end = time.monotonic() + 300
    state = "queued"
    while time.monotonic() < end and state not in ("succeeded", "failed"):
        await asyncio.sleep(0.2)
        state = svc.db.q("select state from jobs where id = %s", (job,))[0][0]
    doc = svc.client.get(f"/v1/jobs/{job}", headers=svc.h()).json()
    assert state == "succeeded", (doc.get("error") or {}).get("detail")
    from queuetruth.schemas.models import ReplaySummary

    summary = ReplaySummary.model_validate(svc.client.get(f"/v1/replays/{rp}", headers=svc.h()).json()["summary"])
    assert summary.stats.records > 0 and doc["usage"]["replay_records"] == summary.stats.records
    man = svc.client.get(f"/v1/jobs/{job}/files", headers=svc.h()).json()["files"]
    assert man
    for f in man:
        data = svc.client.get(f"/v1/jobs/{job}/files/{f['name']}", headers=svc.h()).content
        assert hashlib.sha256(data).hexdigest() == f["sha256"] and len(data) == f["bytes"]
    import queuetruth as qt

    with tempfile.TemporaryDirectory() as out:
        local = qt.replay({"dataset": {"path": str(FIXTURE)}, "rank_oracle": True, "out": out}).model_dump(mode="json")
    remote = summary.model_dump(mode="json")
    for d in (local, remote):
        d.pop("timings", None)
        d.pop("spec", None)
    assert json.dumps(local, sort_keys=True) == json.dumps(remote, sort_keys=True)
