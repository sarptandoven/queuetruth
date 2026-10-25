"""Zoo and calibration jobs end to end on `tests/fixtures/synth_small`: the API, the PgQueuer worker and the child
process calling `queuetruth.run_zoo` / `queuetruth.calibrate`. The stored summaries equal a direct local run (spec
echo aside), every Parquet output of a zoo job is byte-identical to the local run's, the outputs download with their
sha256, the usage meters count shadows and (model, shadow) pairs, an unknown model family is refused at submit, a
model parameter the engine rejects fails the job with the engine's `QT-CONFIG-001`, and the per-kind timeout applies
to `zoo`."""

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
from svc_support import make_svc, upload_bytes, verify_uploads

from queuetruth.schemas.models import CalibrationResult, Job, ZooSummary
from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.ids import new_id
from queuetruth_service.queue import PgQueuerJobQueue, enqueue_sync
from queuetruth_service.routers.runs import ZOO_ALL_MODELS
from queuetruth_service.store import dataset_prefix
from queuetruth_service.worker import Worker

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "synth_small"
pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres, pytest.mark.requires_core, pytest.mark.asyncio]
N_SHADOWS = 40


@pytest.fixture(scope="module")
def shadows_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Seeded shadows on the fixture (`qt.shadow_spec` Parquet)."""
    if not (FIXTURE / "manifest.json").exists():
        pytest.skip("tests/fixtures/synth_small is missing (make fixtures)")
    import queuetruth as qt

    out = tmp_path_factory.mktemp("shadows") / "shadows.parquet"
    qt.sample_shadows({"dataset": {"path": str(FIXTURE)}, "n": N_SHADOWS, "seed": 7, "out": str(out)})
    return out


async def _live(db, tmp_path, **settings):
    gen = make_svc(db, tmp_path, **settings)
    svc = next(gen)
    svc.client.app.state.ctx.enqueue = lambda job_id: enqueue_sync(svc.pool, job_id)
    pool = await make_async_pool(db.worker, max_size=8)
    queue = PgQueuerJobQueue(db.worker, heartbeat_timeout=timedelta(seconds=5), dequeue_timeout=timedelta(seconds=1))
    worker = Worker(svc.settings.model_copy(update={"heartbeat_s": 1.0}), pool, svc.store, queue)
    stop = asyncio.Event()
    task = asyncio.create_task(queue.run(worker.handle, concurrency=1, stop=stop))

    async def close() -> None:
        stop.set()
        await asyncio.wait_for(task, 30)
        await queue.close()
        await pool.close()
        gen.close()

    return svc, close


@pytest_asyncio.fixture
async def live(db, tmp_path):
    svc, close = await _live(db, tmp_path)
    yield svc
    await close()


def seed_fixture(svc, who: str = "A") -> str:
    tenant = svc.tenants[who]
    ds = new_id("ds")
    for p in sorted(FIXTURE.rglob("*")):
        if p.is_file():
            svc.store.put_file(dataset_prefix(tenant, ds) + p.relative_to(FIXTURE).as_posix(), p)
    svc.db.q(
        "insert into datasets (id, tenant_id, name, kind, format, venue, status, upload_ids, manifest, object_prefix) "
        "values (%s, %s, 'synth_small', 'mbo', 'parquet', 'cme', 'ready', '{}', %s, %s)",
        (ds, tenant, (FIXTURE / "manifest.json").read_text(), dataset_prefix(tenant, ds)),
    )
    return ds


async def upload_shadows(svc, path: Path) -> str:
    up = upload_bytes(svc, path.read_bytes(), filename="shadows.parquet", purpose="shadow_spec")
    assert (await asyncio.to_thread(verify_uploads, svc))[up] == "verified"  # its own event loop
    return up


async def wait_terminal(svc, job: str, seconds: float = 300) -> dict:
    end = time.monotonic() + seconds
    state = "queued"
    while time.monotonic() < end and state not in ("succeeded", "failed", "canceled"):
        await asyncio.sleep(0.2)
        state = svc.db.q("select state from jobs where id = %s", (job,))[0][0]
    doc = svc.client.get(f"/v1/jobs/{job}", headers=svc.h()).json()
    Job.model_validate(doc)
    return doc


def downloads_verify(svc, job: str) -> dict[str, bytes]:
    man = svc.client.get(f"/v1/jobs/{job}/files", headers=svc.h()).json()["files"]
    out = {}
    for f in man:
        data = svc.client.get(f"/v1/jobs/{job}/files/{f['name']}", headers=svc.h()).content
        assert hashlib.sha256(data).hexdigest() == f["sha256"] and len(data) == f["bytes"], f["name"]
        out[f["name"]] = data
    return out


def comparable(doc: dict) -> str:
    d = dict(doc)
    d.pop("spec", None)
    d.pop("timings", None)
    return json.dumps(d, sort_keys=True)


async def test_real_zoo_job_equals_a_local_run(live, shadows_file):
    svc = live
    ds, up = seed_fixture(svc), await upload_shadows(svc, shadows_file)
    spec = {"dataset": {"dataset_id": ds}, "shadows": {"upload_id": up}, "bootstrap_b": 50, "particles": 10}
    r = svc.client.post("/v1/zoos", json=spec, headers=svc.h(Idempotency_Key="zoo"))
    assert r.status_code == 202, r.text
    zid, job = r.json()["id"], r.json()["job"]["id"]
    doc = await wait_terminal(svc, job)
    assert doc["state"] == "succeeded", (doc.get("error") or {}).get("detail")
    rec = svc.client.get(f"/v1/zoos/{zid}", headers=svc.h()).json()
    summary = ZooSummary.model_validate(rec["summary"])
    assert summary.n_shadows == N_SHADOWS and len(summary.models) == ZOO_ALL_MODELS  # pins the estimate constant
    assert summary.leaderboard and rec["summary"]["spec"]["shadows"] == {"upload_id": up}
    assert "out" not in rec["summary"]["spec"] and "threads" not in rec["summary"]["spec"]
    pairs = float(N_SHADOWS * ZOO_ALL_MODELS)
    assert doc["usage"]["shadow_orders"] == N_SHADOWS and doc["usage"]["zoo_pairs"] == pairs
    events = svc.db.q("select meter, quantity, unit from usage_events where job_id = %s order by meter", (job,))
    assert [(m, float(q), u) for m, q, u in events if m != "cpu_ms"] == [
        ("shadow_orders", float(N_SHADOWS), "orders"),
        ("zoo_pairs", pairs, "pairs"),
    ]
    assert svc.db.q("select run_id from jobs where id = %s", (job,)) == [(summary.run_id,)]
    files = downloads_verify(svc, job)
    assert {"summary.json", "zoo_predictions.parquet", "zoo_errors.parquet", "zoo_shadows.parquet"} <= set(files)
    import queuetruth as qt

    with tempfile.TemporaryDirectory() as out:
        local = qt.run_zoo(
            {
                "dataset": {"path": str(FIXTURE)},
                "shadows": {"path": str(shadows_file)},
                "bootstrap_b": 50,
                "particles": 10,
                "out": out,
            }
        )
        local_files = {p.relative_to(out).as_posix(): p.read_bytes() for p in Path(out).rglob("*") if p.is_file()}
    assert comparable(local.model_dump(mode="json")) == comparable(summary.model_dump(mode="json"))
    parquet = sorted(n for n in local_files if n.endswith(".parquet"))
    assert parquet == sorted(n for n in files if n.endswith(".parquet"))
    assert {"zoo_fills.parquet", "zoo_shadows.parquet", "zoo_errors.parquet"} <= set(parquet), parquet
    for name in parquet:  # every Parquet output byte for byte
        assert hashlib.sha256(local_files[name]).digest() == hashlib.sha256(files[name]).digest(), name
    # summary.json differs from the local one only in the spec echo (tenant ids instead of paths, SPEC 7.3)
    assert comparable(json.loads(files["summary.json"])) == comparable(json.loads(local_files["summary.json"]))
    assert json.loads(files["summary.json"])["run_id"] == summary.run_id


async def test_zoo_job_with_a_bad_model_parameter_fails_with_the_engine_code(live, shadows_file):
    svc = live
    ds, up = seed_fixture(svc), await upload_shadows(svc, shadows_file)
    spec = {"dataset": {"dataset_id": ds}, "shadows": {"upload_id": up}, "models": ["nosuchfamily[n=1]"]}
    r = svc.client.post("/v1/zoos", json=spec, headers=svc.h(Idempotency_Key="family"))
    assert r.status_code == 422 and r.json()["errors"][0]["loc"] == ["body", "models", 0]  # family: at submit
    spec["models"] = ["databento_mc[k=5]"]  # a known family; the engine checks parameter values
    r = svc.client.post("/v1/zoos", json=spec, headers=svc.h(Idempotency_Key="bad"))
    assert r.status_code == 202, r.text
    doc = await wait_terminal(svc, r.json()["job"]["id"])
    assert doc["state"] == "failed"
    err = doc["error"]
    assert err["code"] == "QT-CONFIG-001" and err["status"] == 422 and "k must be in (-1, 1)" in err["detail"]
    assert "qtjob-" not in err["detail"]  # no worker paths
    assert svc.client.get(f"/v1/zoos/{r.json()['id']}", headers=svc.h()).json()["summary"] is None
    assert svc.db.q("select count(*) from usage_events where job_id = %s", (doc["id"],)) == [(0,)]


async def test_real_calibration_job_equals_a_local_run(live, shadows_file):
    svc = live
    ds, up = seed_fixture(svc), await upload_shadows(svc, shadows_file)
    body = {"kind": "alloc", "family": "power3", "bootstrap": {"b": 20}, "particles": 10}
    spec = body | {"dataset": {"dataset_id": ds}, "shadows": {"upload_id": up}}
    r = svc.client.post("/v1/calibrations", json=spec, headers=svc.h(Idempotency_Key="cal"))
    assert r.status_code == 202, r.text
    cid, job = r.json()["id"], r.json()["job"]["id"]
    doc = await wait_terminal(svc, job)
    assert doc["state"] == "succeeded", (doc.get("error") or {}).get("detail")
    rec = svc.client.get(f"/v1/calibrations/{cid}", headers=svc.h()).json()
    result = CalibrationResult.model_validate(rec["summary"])
    assert result.kind == "alloc" and result.family == "power3" and result.n_shadows > 0
    assert doc["usage"]["shadow_orders"] == result.n_shadows
    models = len(rec["summary"].get("held_out") or {}) + len(rec["summary"].get("baseline_held_out") or {})
    assert models > 0 and doc["usage"]["zoo_pairs"] == result.n_shadows * models
    files = downloads_verify(svc, job)
    assert files
    import queuetruth as qt

    with tempfile.TemporaryDirectory() as out:
        local = qt.calibrate(
            body | {"dataset": {"path": str(FIXTURE)}, "shadows": {"path": str(shadows_file)}, "out": out}
        )
    assert comparable(local.model_dump(mode="json")) == comparable(result.model_dump(mode="json"))


async def test_zoo_per_kind_timeout(db, tmp_path, shadows_file):
    svc, close = await _live(db, tmp_path, job_timeouts_s={"zoo": 0.2})
    try:
        ds, up = seed_fixture(svc), await upload_shadows(svc, shadows_file)
        r = svc.client.post(
            "/v1/zoos",
            json={"dataset": {"dataset_id": ds}, "shadows": {"upload_id": up}},
            headers=svc.h(Idempotency_Key="t"),
        )
        doc = await wait_terminal(svc, r.json()["job"]["id"], 120)
        assert doc["state"] == "failed" and doc["error"]["code"] == "QT-ENGINE-001"
        assert doc["error"]["detail"] == "job timed out"
    finally:
        await close()
