"""Endpoint behaviour not covered by the oracle tests: health, meta, uploads, datasets validation, estimates,
pagination, job filters, cancel semantics, reports, run records."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from svc_support import insert_job, seed_dataset, succeed_job, upload_bytes, verify_uploads

from queuetruth.schemas import models as m
from queuetruth_service.app import create_app
from queuetruth_service.settings import Settings
from queuetruth_service.store.local import LocalStore

pytestmark = pytest.mark.requires_postgres


def test_health_ok_and_degraded(svc, tmp_path):
    r = svc.client.get("/v1/health")
    assert r.status_code == 200
    doc = m.Health.model_validate(r.json())
    assert (doc.status, doc.db, doc.store, doc.queue) == ("ok", "ok", "ok", "ok")
    s = Settings(store_root=str(tmp_path / "s"))
    with TestClient(create_app(s, store=LocalStore(tmp_path / "s", b"k"), connect=False)) as c:
        r = c.get("/v1/health")
        assert r.status_code == 503 and r.json()["status"] == "degraded" and r.json()["db"] == "down"
        p = c.get("/v1/tenant", headers={"Authorization": f"Bearer {svc.keys['A']}"})
        assert p.status_code == 503 and p.headers["content-type"] == "application/problem+json"


def test_meta_and_tenant(svc):
    meta = m.Meta.model_validate(svc.client.get("/v1/meta", headers=svc.h()).json())
    assert meta.api_version == "v1" and meta.limits.part_size_bytes == svc.settings.part_size_bytes
    assert "itch" in meta.formats and meta.schema_versions["qt.job"] == 1 and meta.zero_egress is False
    assert set(meta.limits.rate_limits) == {"read", "write", "submit"}
    t = m.Tenant.model_validate(svc.client.get("/v1/tenant", headers=svc.h()).json())
    assert t.id == svc.tenants["A"] and t.retention.uploads_days == 30


def test_upload_lifecycle_and_errors(svc):
    data = b"z" * 2500
    body = {
        "purpose": "live_log",
        "filename": "../../x y.zip",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    r = svc.client.post("/v1/uploads", json=body, headers=svc.h(Idempotency_Key="u1"))
    assert r.status_code == 201
    up = m.Upload.model_validate(r.json())
    assert up.status == "pending" and len(up.parts) == 3 and up.filename == "../../x y.zip"
    key = svc.db.q("select object_key from uploads where id = %s", (up.id,))[0][0]
    assert key == f"t/{svc.tenants['A']}/uploads/{up.id}/x_y.zip"
    again = svc.client.get(f"/v1/uploads/{up.id}", headers=svc.h()).json()
    assert len(again["parts"]) == 3  # fresh URLs while pending
    r = svc.client.post(
        f"/v1/uploads/{up.id}/complete",
        json={"parts": [{"part_number": 1, "etag": "x"}]},
        headers=svc.h(Idempotency_Key="c"),
    )
    assert r.status_code == 422 and "1..3" in str(r.json()["errors"])
    uid = upload_bytes(svc, b"q" * 10, filename="small.bin")
    r = svc.client.post(
        f"/v1/uploads/{uid}/complete",
        json={"parts": [{"part_number": 1, "etag": "x"}]},
        headers=svc.h(Idempotency_Key="c2"),
    )
    assert r.status_code == 409 and r.json()["type"].endswith("/upload-not-pending")
    bad = dict(body, sha256="XYZ")
    assert svc.client.post("/v1/uploads", json=bad, headers=svc.h(Idempotency_Key="u2")).status_code == 422
    assert (
        svc.client.post("/v1/uploads", json=dict(body, size_bytes=0), headers=svc.h(Idempotency_Key="u3")).status_code
        == 422
    )
    assert (
        svc.client.post("/v1/uploads", json=dict(body, purpose="x"), headers=svc.h(Idempotency_Key="u4")).status_code
        == 422
    )


def test_dataset_create_validation(svc):
    uid = upload_bytes(svc, b"x" * 50)
    base = {"name": "d", "kind": "mbo", "format": "itch", "venue": "nasdaq", "upload_ids": [uid]}
    r = svc.client.post("/v1/datasets", json=base, headers=svc.h(Idempotency_Key="d1"))
    assert r.status_code == 409  # not verified yet
    verify_uploads(svc)
    r = svc.client.post("/v1/datasets", json=base | {"ingest": {"format": "dbn"}}, headers=svc.h(Idempotency_Key="d2"))
    assert r.status_code == 422 and r.json()["errors"][0]["loc"] == ["body", "ingest", "format"]
    r = svc.client.post("/v1/datasets", json=base | {"ingest": {"inputs": []}}, headers=svc.h(Idempotency_Key="d3"))
    assert r.status_code == 422
    r = svc.client.post("/v1/datasets", json=base | {"upload_ids": [uid, uid]}, headers=svc.h(Idempotency_Key="d4"))
    assert r.status_code == 422
    r = svc.client.post("/v1/datasets", json=base | {"upload_ids": ["up_nope"]}, headers=svc.h(Idempotency_Key="d5"))
    assert r.status_code == 404
    r = svc.client.post("/v1/datasets", json=base, headers=svc.h(Idempotency_Key="d6"))
    assert r.status_code == 202
    job = svc.db.q("select kind, spec from jobs where id = %s", (r.json()["job_id"],))[0]
    assert job[0] == "dataset_validate" and job[1]["ingest"]["format"] == "itch" and job[1]["upload_ids"] == [uid]
    assert svc.enqueued == [r.json()["job_id"]]


def test_estimates(svc):
    ds = seed_dataset(svc)
    for path, spec in (
        ("/v1/replays/estimate", {"dataset": {"dataset_id": ds}}),
        ("/v1/calibrations/estimate", {"kind": "alloc", "family": "power3", "dataset": {"dataset_id": ds}}),
        (
            "/v1/scorecards/estimate",
            {"live": {"upload_id": upload_and_verify(svc)}, "sim": {"kind": "exact"}, "dataset": {"dataset_id": ds}},
        ),
    ):
        r = svc.client.post(path, json=spec, headers=svc.h())
        assert r.status_code == 200, r.text
        e = m.Estimate.model_validate(r.json())
        assert e.records_est == 1000 and e.cpu_seconds_est >= 0 and e.eta_seconds >= e.cpu_seconds_est
    assert svc.db.q("select count(*) from jobs") == [(0,)]
    r = svc.client.post(
        "/v1/replays/estimate",
        json={"dataset": {"dataset_id": seed_dataset(svc, status="validating")}},
        headers=svc.h(),
    )
    assert r.status_code == 409 and r.json()["type"].endswith("/dataset-not-ready")


def upload_and_verify(svc) -> str:
    uid = upload_bytes(svc, b"live" * 30, filename="live.zip", purpose="live_log")
    verify_uploads(svc)
    return uid


def test_pagination_and_filters(svc):
    ids = [insert_job(svc, kind="replay" if i % 2 else "report") for i in range(7)]
    page1 = svc.client.get("/v1/jobs?limit=3", headers=svc.h()).json()
    assert len(page1["data"]) == 3 and page1["has_more"] and page1["next_cursor"] == page1["data"][-1]["id"]
    page2 = svc.client.get(f"/v1/jobs?limit=3&starting_after={page1['next_cursor']}", headers=svc.h()).json()
    page3 = svc.client.get(f"/v1/jobs?limit=3&starting_after={page2['next_cursor']}", headers=svc.h()).json()
    got = [j["id"] for p in (page1, page2, page3) for j in p["data"]]
    assert got == sorted(ids, reverse=True) and not page3["has_more"] and page3["next_cursor"] is None
    only = svc.client.get("/v1/jobs?kind=replay", headers=svc.h()).json()["data"]
    assert len(only) == 3 and {j["kind"] for j in only} == {"replay"}
    assert svc.client.get("/v1/jobs?state=succeeded", headers=svc.h()).json()["data"] == []
    for q in ("limit=0", "limit=201", "kind=bogus", "state=bogus"):
        assert svc.client.get(f"/v1/jobs?{q}", headers=svc.h()).status_code == 422, q
    m.Page.model_validate(page1)


def test_runs_reports_and_cancel(svc):
    ds = seed_dataset(svc)
    r = svc.client.post(
        "/v1/replays", json={"dataset": {"dataset_id": ds}, "threads": 4}, headers=svc.h(Idempotency_Key="r")
    )
    rp, job = r.json()["id"], r.json()["job"]
    assert rp.startswith("rp_") and job["resource"] == {"type": "replay", "id": rp} and job["kind"] == "replay"
    m.Job.model_validate(job)
    rec = svc.client.get(f"/v1/replays/{rp}", headers=svc.h()).json()
    assert rec["job_id"] == job["id"] and rec["summary"] is None and rec["spec"]["threads"] == 4
    assert rec["spec"]["bbo"] == "change"  # stored with SPEC 4.2 defaults
    r = svc.client.post("/v1/reports", json={"inputs": [job["id"]]}, headers=svc.h(Idempotency_Key="rep1"))
    assert r.status_code == 409 and r.json()["type"].endswith("/job-not-succeeded")
    succeed_job(svc, job["id"], {"x.parquet": b"1"})
    r = svc.client.post("/v1/reports", json={"inputs": [job["id"]]}, headers=svc.h(Idempotency_Key="rep2"))
    assert r.status_code == 202 and r.json()["id"].startswith("rpt_")
    rep = svc.client.get(f"/v1/reports/{r.json()['id']}", headers=svc.h()).json()
    assert rep["files"] == [] and rep["job_id"] == r.json()["job"]["id"]
    succeed_job(svc, rep["job_id"], {"report.html": b"<html></html>"})
    rep = svc.client.get(f"/v1/reports/{r.json()['id']}", headers=svc.h()).json()
    assert rep["files"][0]["name"] == "report.html" and rep["files"][0]["bytes"] == 13
    q = insert_job(svc)
    c = svc.client.post(f"/v1/jobs/{q}/cancel", headers=svc.h(Idempotency_Key="cx"))
    assert c.json()["state"] == "canceled" and c.json()["finished_at"]
    again = svc.client.post(f"/v1/jobs/{q}/cancel", headers=svc.h(Idempotency_Key="cx"))
    assert again.status_code == 200 and again.headers["idempotent-replayed"] == "true"
    assert svc.client.get(f"/v1/jobs/{q}/files", headers=svc.h()).status_code == 409
    r = svc.client.post(
        "/v1/scorecards",
        json={"live": {"path": "/x"}, "sim": {"kind": "exact"}},
        headers=svc.h(Idempotency_Key="sc"),
    )
    assert r.status_code == 422 and r.json()["errors"][0]["loc"] == ["body", "live"]
    r = svc.client.post(
        "/v1/calibrations",
        json={"kind": "latency", "family": "latency", "live": {"upload_id": upload_and_verify(svc)}},
        headers=svc.h(Idempotency_Key="cal"),
    )
    assert r.status_code == 202 and r.json()["id"].startswith("cal_")
    lst = svc.client.get("/v1/calibrations", headers=svc.h()).json()
    assert [x["id"] for x in lst["data"]] == [r.json()["id"]]
