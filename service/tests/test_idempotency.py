"""Idempotency (SPEC 8.6, O-API-2)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from svc_support import seed_dataset

from queuetruth_service.idempotency import request_hash

pytestmark = pytest.mark.requires_postgres


def test_o_api_2_same_key_same_body_replays_without_second_job(svc):
    ds = seed_dataset(svc)
    body = {"dataset": {"dataset_id": ds}, "seed": 1}
    r1 = svc.client.post("/v1/replays", json=body, headers=svc.h(Idempotency_Key="abc"))
    assert r1.status_code == 202, r1.text
    assert r1.headers["location"] == f"/v1/jobs/{r1.json()['job']['id']}"
    assert "idempotent-replayed" not in r1.headers
    raw = json.dumps({"seed": 1, "dataset": {"dataset_id": ds}}, indent=3)
    r2 = svc.client.post(
        "/v1/replays", content=raw, headers=svc.h(Idempotency_Key="abc") | {"content-type": "application/json"}
    )
    assert r2.status_code == 202 and r2.json() == r1.json()
    assert r2.headers["idempotent-replayed"] == "true" and r2.headers["location"] == r1.headers["location"]
    assert svc.db.q("select count(*) from jobs") == [(1,)]
    assert svc.db.q("select count(*) from replays") == [(1,)]
    assert svc.enqueued == [r1.json()["job"]["id"]]


def test_o_api_2_same_key_different_body_422(svc):
    ds = seed_dataset(svc)
    svc.client.post("/v1/replays", json={"dataset": {"dataset_id": ds}}, headers=svc.h(Idempotency_Key="k"))
    r = svc.client.post(
        "/v1/replays", json={"dataset": {"dataset_id": ds}, "seed": 9}, headers=svc.h(Idempotency_Key="k")
    )
    assert r.status_code == 422 and r.json()["type"].endswith("/idempotency-key-reused")
    assert svc.db.q("select count(*) from jobs") == [(1,)]


def test_o_api_2_concurrent_duplicate_409(svc):
    ds = seed_dataset(svc)
    body = {"dataset": {"dataset_id": ds}}
    h = request_hash("POST", "/v1/replays", json.dumps(body).encode())
    with psycopg.connect(svc.db.owner, autocommit=True) as c:  # a request with this key is still executing
        c.execute(
            "insert into idempotency_keys (tenant_id, key, method, path, request_hash, state) "
            "values (%s, 'fly', 'POST', '/v1/replays', %s, 'in_progress')",
            (svc.tenants["A"], h),
        )
    r = svc.client.post("/v1/replays", json=body, headers=svc.h(Idempotency_Key="fly"))
    assert r.status_code == 409 and r.json()["type"].endswith("/idempotency-in-flight")
    with psycopg.connect(svc.db.owner, autocommit=True) as c:  # abandoned (crashed) request: taken over after 60 s
        c.execute("update idempotency_keys set locked_at = now() - interval '2 minutes' where key = 'fly'")
    assert svc.client.post("/v1/replays", json=body, headers=svc.h(Idempotency_Key="fly")).status_code == 202


def test_o_api_2_parallel_requests_one_job(svc):
    ds = seed_dataset(svc)
    body = {"dataset": {"dataset_id": ds}}

    def post(_):
        return svc.client.post("/v1/replays", json=body, headers=svc.h(Idempotency_Key="race"))

    with ThreadPoolExecutor(6) as ex:
        rs = list(ex.map(post, range(6)))
    codes = [r.status_code for r in rs]
    assert set(codes) <= {202, 409} and 202 in codes, codes
    assert len({r.json()["job"]["id"] for r in rs if r.status_code == 202}) == 1
    assert svc.db.q("select count(*) from jobs") == [(1,)]
    assert len(svc.enqueued) == 1


def test_validation_and_handler_errors_are_not_stored(svc):
    """SPEC 8.6 (open conflict 9, ratified): a 4xx problem raised during execution rolls the handler back, so nothing
    is committed and nothing is stored; the same key then works once the cause is fixed. (A 4xx the handler returns
    with committed effects is stored: `test_completion_rejects_a_size_mismatch_fails_the_upload_and_deletes_its_parts`.)
    """
    r = svc.client.post(
        "/v1/replays", json={"dataset": {"dataset_id": "ds_missing"}}, headers=svc.h(Idempotency_Key="nf")
    )
    assert r.status_code == 404 and "idempotent-replayed" not in r.headers
    r = svc.client.post("/v1/replays", json={"bad": 1}, headers=svc.h(Idempotency_Key="nf2"))
    assert r.status_code == 422
    assert svc.db.q("select count(*) from idempotency_keys") == [(0,)]
    assert svc.db.q("select count(*) from jobs") == [(0,)] and svc.db.q("select count(*) from replays") == [(0,)]
    assert svc.enqueued == []
    ds = seed_dataset(svc)
    r = svc.client.post("/v1/replays", json={"dataset": {"dataset_id": ds}}, headers=svc.h(Idempotency_Key="nf"))
    assert r.status_code == 202  # the same key works after a non-stored failure


def test_keys_are_scoped_per_tenant(svc):
    a = seed_dataset(svc, "A")
    b = seed_dataset(svc, "B")
    ra = svc.client.post("/v1/replays", json={"dataset": {"dataset_id": a}}, headers=svc.h("A", Idempotency_Key="same"))
    rb = svc.client.post("/v1/replays", json={"dataset": {"dataset_id": b}}, headers=svc.h("B", Idempotency_Key="same"))
    assert ra.status_code == rb.status_code == 202 and ra.json()["id"] != rb.json()["id"]


def test_prune_after_24h(svc):
    import asyncio

    from queuetruth_service.db.pool import make_async_pool
    from queuetruth_service.maintenance import prune_idempotency

    ds = seed_dataset(svc)
    svc.client.post("/v1/replays", json={"dataset": {"dataset_id": ds}}, headers=svc.h(Idempotency_Key="old"))
    svc.db.q("update idempotency_keys set created_at = now() - interval '25 hours'")

    async def go():
        pool = await make_async_pool(svc.db.worker)
        try:
            return await prune_idempotency(pool)
        finally:
            await pool.close()

    assert asyncio.run(go()) == 1
