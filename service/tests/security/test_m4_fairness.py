"""M4 (security review, inverted): a tenant's compute budget no longer scales with its keys or its parameters. Rate
buckets are per tenant, key minting is capped, compute knobs are bounded at the API (422 with every offending field)
and again in the worker, and the claim caps each tenant's running jobs across workers (the excess is deferred)."""

from __future__ import annotations

import asyncio
import json

import pytest
from svc_support import insert_job, make_svc, seed_dataset

from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.settings import DEFAULT_COMPUTE_LIMITS, DEFAULT_RATE_LIMITS
from queuetruth_service.worker import Worker


def _submit(svc, key: str, spec: dict, n: int, tag: str) -> int:
    ok = 0
    for i in range(n):
        r = svc.client.post(
            "/v1/replays", json=spec, headers={"Authorization": f"Bearer {key}", "Idempotency-Key": f"{tag}{i}"}
        )
        ok += r.status_code == 202
    return ok


@pytest.mark.requires_postgres
def test_minting_keys_adds_no_submit_budget(db, tmp_path):
    gen = make_svc(db, tmp_path, rate_limits=dict(DEFAULT_RATE_LIMITS))  # production limits (submit 10/min burst 5)
    svc = next(gen)
    try:
        spec = {"dataset": {"dataset_id": seed_dataset(svc)}}
        assert _submit(svc, svc.keys["A"], spec, 8, "a") == 5  # burst 5, then 429
        minted = []
        for i in range(10):
            r = svc.client.post(
                "/v1/keys", json={"scopes": ["write"], "name": f"k{i}"}, headers=svc.h(Idempotency_Key=f"mint{i}")
            )
            assert r.status_code == 201
            minted.append(r.json()["secret"])
        assert sum(_submit(svc, k, spec, 5, f"m{n}") for n, k in enumerate(minted)) == 0  # one bucket per tenant
        spec_b = {"dataset": {"dataset_id": seed_dataset(svc, "B")}}
        assert _submit(svc, svc.keys["B"], spec_b, 5, "b") == 5  # other tenants keep their own budget
    finally:
        gen.close()


@pytest.mark.requires_postgres
def test_active_keys_per_tenant_are_capped(db, tmp_path):
    gen = make_svc(db, tmp_path, max_active_keys=3)
    svc = next(gen)
    try:
        ids = []
        for i in range(2):  # bootstrap key + 2 = 3
            r = svc.client.post("/v1/keys", json={"scopes": ["read"]}, headers=svc.h(Idempotency_Key=f"k{i}"))
            assert r.status_code == 201
            ids.append(r.json()["id"])
        r = svc.client.post("/v1/keys", json={"scopes": ["read"]}, headers=svc.h(Idempotency_Key="k3"))
        assert r.status_code == 409 and r.json()["type"].endswith("/key-limit")
        assert svc.client.delete(f"/v1/keys/{ids[0]}", headers=svc.h()).status_code == 204
        r = svc.client.post("/v1/keys", json={"scopes": ["read"]}, headers=svc.h(Idempotency_Key="k4"))
        assert r.status_code == 201
    finally:
        gen.close()


@pytest.mark.requires_postgres
def test_compute_knobs_are_bounded_at_the_api(svc):
    ds = seed_dataset(svc)
    r = svc.client.post(
        "/v1/replays", json={"dataset": {"dataset_id": ds}, "threads": 10**9}, headers=svc.h(Idempotency_Key="thr")
    )
    assert r.status_code == 422 and r.json()["errors"] == [
        {"loc": ["body", "threads"], "msg": f"must be <= {DEFAULT_COMPUTE_LIMITS['threads']} on this service"}
    ]
    cal = {
        "kind": "alloc",
        "family": "power",
        "dataset": {"dataset_id": ds},
        "threads": 10**6,
        "particles": 10**9,
        "bootstrap": {"b": 10**9},
    }
    r2 = svc.client.post("/v1/calibrations", json=cal, headers=svc.h(Idempotency_Key="cal"))
    assert r2.status_code == 422 and r2.headers["content-type"] == "application/problem+json"
    assert sorted(tuple(e["loc"]) for e in r2.json()["errors"]) == [
        ("body", "bootstrap", "b"),
        ("body", "particles"),
        ("body", "threads"),
    ]
    est = svc.client.post("/v1/calibrations/estimate", json=cal, headers=svc.h())
    assert est.status_code == 422
    assert svc.db.q("select count(*) from jobs") == [(0,)]  # nothing was queued
    ok = svc.client.post(
        "/v1/replays",
        json={"dataset": {"dataset_id": ds}, "threads": DEFAULT_COMPUTE_LIMITS["threads"]},
        headers=svc.h(Idempotency_Key="thr-ok"),
    )
    assert ok.status_code == 202


@pytest.mark.requires_postgres
def test_worker_refuses_out_of_bound_specs_that_bypassed_the_api(svc, tmp_path):
    job = insert_job(svc)
    marker = tmp_path / "child-ran"
    svc.db.q("update jobs set spec = %s where id = %s", (json.dumps({"threads": 10**9, "marker": str(marker)}), job))

    async def go() -> str | None:
        pool = await make_async_pool(svc.db.worker)
        try:
            w = Worker(
                svc.settings, pool, svc.store, None, targets={"replay": "queuetruth_service.testing:fake_marker"}
            )
            return await w.handle(job)
        finally:
            await pool.close()

    assert asyncio.run(go()) == "failed"
    err = svc.db.q("select error from jobs where id = %s", (job,))[0][0]
    assert err["code"] == "QT-CONFIG-001" and "spec.threads" in err["detail"]
    assert not marker.exists()


class RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    async def enqueue(self, job_id: str, delay_s: float = 0) -> None:
        self.calls.append((job_id, delay_s))


@pytest.mark.requires_postgres
def test_claim_caps_running_jobs_per_tenant(svc):
    a1, a2, a3 = (insert_job(svc, "A") for _ in range(3))
    b1 = insert_job(svc, "B")

    async def go() -> None:
        pool = await make_async_pool(svc.db.worker)
        q = RecordingQueue()
        try:
            s = svc.settings.model_copy(update={"tenant_max_running": 2, "tenant_defer_s": 7})
            w = Worker(s, pool, svc.store, q)  # type: ignore[arg-type]
            first, second = await w.claim(a1), await w.claim(a2)
            assert first is not None and second is not None
            assert await w.claim(a3) is None  # tenant A is at its cap: deferred, still queued
            assert q.calls == [(a3, 7)]
            assert svc.db.q("select state, attempts from jobs where id = %s", (a3,)) == [("queued", 0)]
            assert await w.claim(b1) is not None  # tenant B is not blocked by A
            svc.db.q("update jobs set state = 'succeeded' where id = %s", (a1,))
            assert await w.claim(a3) is not None
        finally:
            await pool.close()

    asyncio.run(go())
    assert svc.settings.tenant_max_running == 1  # secure default: one running job per tenant
