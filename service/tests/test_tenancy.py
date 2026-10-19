"""Tenant isolation (SPEC 8.8, O-API-3): tenant B gets 404 for every tenant-A id on every endpoint, cannot download
A's files, cannot reference A's datasets/uploads/jobs in specs, and RLS returns 0 rows to `qt_app` under B. A tenant
is bound only through an ownership check (`qt_authenticate` with the key's HMAC); a hand-set `app.tenant_id` binds
nothing (security review M3)."""

from __future__ import annotations

from urllib.parse import urlsplit

import psycopg
import pytest
from svc_support import seed_dataset, succeed_job, upload_bytes, verify_uploads

from queuetruth_service import keys

pytestmark = pytest.mark.requires_postgres
TABLES = (
    "tenants", "api_keys", "idempotency_keys", "rate_limit_buckets", "uploads", "datasets", "jobs", "job_files",
    "replays", "calibrations", "scorecards", "reports", "usage_events", "tombstones", "audit_log",
)  # fmt: skip


def test_o_api_3_tenant_isolation(svc):
    c = svc.client
    up = upload_bytes(svc, b"tenant-a-data" * 10, "A")
    assert verify_uploads(svc)[up] == "verified"
    ds = seed_dataset(svc, "A")
    r = c.post("/v1/replays", json={"dataset": {"dataset_id": ds}}, headers=svc.h("A", Idempotency_Key="r"))
    rp, job = r.json()["id"], r.json()["job"]["id"]
    succeed_job(svc, job, {"out.bin": b"secret-bytes", "l2/date=2026-01-02/part-00000.parquet": b"pq"})
    r = c.post("/v1/reports", json={"inputs": [job]}, headers=svc.h("A", Idempotency_Key="rep"))
    assert r.status_code == 202, r.text
    rpt = r.json()["id"]
    key_id = c.get("/v1/keys", headers=svc.h("A")).json()["data"][0]["id"]
    svc.db.q(
        "insert into calibrations (id, tenant_id, job_id, spec) values ('cal_A', %s, %s, '{}'), "
        "('cal_X', %s, %s, '{}')",
        (svc.tenants["A"], job, svc.tenants["A"], job),
    )
    svc.db.q(
        "insert into scorecards (id, tenant_id, job_id, spec) values ('sc_A', %s, %s, '{}')", (svc.tenants["A"], job)
    )
    # A sees everything
    assert c.get(f"/v1/jobs/{job}/files", headers=svc.h("A")).status_code == 200
    assert c.get(f"/v1/jobs/{job}/files/out.bin", headers=svc.h("A")).content == b"secret-bytes"
    b = svc.h("B", Idempotency_Key="b-key")
    reads = [
        f"/v1/uploads/{up}",
        f"/v1/datasets/{ds}",
        f"/v1/replays/{rp}",
        "/v1/calibrations/cal_A",
        "/v1/scorecards/sc_A",
        f"/v1/reports/{rpt}",
        f"/v1/jobs/{job}",
        f"/v1/jobs/{job}/files",
        f"/v1/jobs/{job}/files/out.bin",
        f"/v1/jobs/{job}/files/l2/date=2026-01-02/part-00000.parquet",
    ]
    for path in reads:
        r = c.get(path, headers=b)
        assert r.status_code == 404, (path, r.status_code)
        assert r.headers["content-type"] == "application/problem+json"
    writes = [
        ("delete", f"/v1/uploads/{up}", None),
        ("delete", f"/v1/datasets/{ds}", None),
        ("delete", f"/v1/keys/{key_id}", None),
        ("post", f"/v1/jobs/{job}/cancel", None),
        ("post", f"/v1/uploads/{up}/complete", {"parts": [{"part_number": 1, "etag": "x"}]}),
        ("post", "/v1/replays", {"dataset": {"dataset_id": ds}}),
        ("post", "/v1/replays/estimate", {"dataset": {"dataset_id": ds}}),
        ("post", "/v1/replays", {"dataset": {"dataset_id": seed_dataset(svc, "B")}, "shadows": {"upload_id": up}}),
        (
            "post",
            "/v1/replays",
            {
                "dataset": {"dataset_id": seed_dataset(svc, "B")},
                "shadows": {"job_file": {"job_id": job, "name": "out.bin"}},
            },
        ),
        ("post", "/v1/reports", {"inputs": [job]}),
        ("post", "/v1/datasets", {"name": "x", "kind": "mbo", "format": "itch", "venue": "nasdaq", "upload_ids": [up]}),
    ]
    for i, (method, path, body) in enumerate(writes):
        h = svc.h("B", Idempotency_Key=f"bw{i}")
        r = c.request(method.upper(), path, json=body, headers=h)
        assert r.status_code == 404, (path, r.status_code, r.text)
    # lists never show A's resources
    for path in (
        "/v1/datasets",
        "/v1/jobs",
        "/v1/replays",
        "/v1/calibrations",
        "/v1/scorecards",
        "/v1/tombstones",
        "/v1/keys",
    ):
        ids = [x["id"] for x in c.get(path, headers=svc.h("B")).json()["data"]]
        assert not set(ids) & {up, ds, rp, job, "cal_A", "sc_A", key_id}, path
    usage = c.get("/v1/usage?group_by=job", headers=svc.h("B")).json()
    assert all(r["key"] != job for r in usage["rows"])
    assert c.get("/v1/tenant", headers=svc.h("B")).json()["id"] == svc.tenants["B"]
    # A's data is untouched after B's attempts
    assert c.get(f"/v1/uploads/{up}", headers=svc.h("A")).status_code == 200
    assert c.get(f"/v1/datasets/{ds}", headers=svc.h("A")).json()["status"] == "ready"
    # a signed URL for A's object is scoped to that key: B cannot reuse it for another object
    url = c.get(f"/v1/jobs/{job}/files", headers=svc.h("A")).json()["files"][0]["url"]
    u = urlsplit(url)
    assert c.get(f"{u.path}?{u.query}").status_code == 200
    other = u.path.replace("out.bin", "l2/date=2026-01-02/part-00000.parquet") if "out.bin" in u.path else u.path + "x"
    assert c.get(f"{other}?{u.query}").status_code == 403
    # RLS: direct SQL as qt_app with B's app.tenant_id sees none of A's rows
    with psycopg.connect(svc.db.app) as conn:
        with conn.transaction():
            conn.execute("select set_config('app.tenant_id', %s, true)", (svc.tenants["B"],))
            for t in TABLES:
                col = "id" if t == "tenants" else "tenant_id"
                n = conn.execute(f"select count(*) from {t} where {col} = %s", (svc.tenants["A"],)).fetchone()[0]
                assert n == 0, t
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(
                        "insert into jobs (id, tenant_id, kind, resource_type, resource_id) "
                        "values ('job_evil', %s, 'replay', 'replay', 'x')",
                        (svc.tenants["A"],),
                    )
        assert conn.execute("select count(*) from jobs").fetchone()[0] == 0  # no tenant set: nothing visible
    with psycopg.connect(svc.db.app) as conn, conn.transaction():
        conn.execute("select set_config('app.tenant_id', %s, true)", (svc.tenants["A"],))
        assert conn.execute("select count(*) from jobs").fetchone()[0] == 0  # a hand-set GUC binds nothing
    key = svc.keys["A"]
    prefix = keys.parse(key)
    with psycopg.connect(svc.db.app) as conn, conn.transaction():
        bound = conn.execute(
            "select tenant_id from qt_authenticate(%s, %s)", (prefix, keys.key_hash(svc.settings.pepper, key))
        ).fetchone()
        assert bound == (svc.tenants["A"],)
        assert conn.execute("select count(*) from jobs").fetchone()[0] >= 2
