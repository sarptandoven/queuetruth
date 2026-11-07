"""API keys (SPEC 8.5), auth, scopes and RFC 9457 problems for 401/403/404/409/413/422/429 (O-API-4)."""

from __future__ import annotations

import base64
import json

import pytest

from queuetruth_service import keys

PROBLEM = "application/problem+json"


def assert_problem(r, status: int, slug: str) -> dict:
    assert r.status_code == status, r.text
    assert r.headers["content-type"] == PROBLEM
    p = r.json()
    assert p["schema"] == "qt.problem" and p["schema_version"] == 1
    assert p["type"] == f"https://queuetruth.dev/problems/{slug}"
    assert p["status"] == status and p["code"].startswith("QT-") and p["title"]
    assert p["request_id"] == r.headers["x-request-id"]
    assert "Traceback" not in r.text
    from queuetruth.schemas.models import Problem

    Problem.model_validate(p)
    return p


def test_key_format_checksum_and_hash():
    key, prefix = keys.generate("prod")
    assert key.startswith("qt_live_") and len(key) == len("qt_live_") + 32 + 1 + 6
    assert keys.parse(key) == prefix and len(prefix) == 12
    assert keys.generate("dev")[0].startswith("qt_test_")
    tampered = key[:-1] + ("A" if key[-1] != "A" else "B")
    assert keys.parse(tampered) is None
    flipped = key[:12] + ("x" if key[12] != "x" else "y") + key[13:]
    assert keys.parse(flipped) is None
    h = keys.key_hash(b"pepper", key)
    assert keys.verify(b"pepper", key, h) and not keys.verify(b"other", key, h)
    assert key not in h and len(h) == 64
    assert "qt_<redacted>" in keys.redact(f"auth {key} failed") and key not in keys.redact(key)


@pytest.mark.requires_postgres
def test_o_api_4_problem_bodies(svc):
    c = svc.client
    # 401: no key, malformed key, bad checksum, unknown key
    p = assert_problem(c.get("/v1/jobs"), 401, "unauthorized")
    assert c.get("/v1/jobs").headers["www-authenticate"].startswith("Bearer")
    good = svc.keys["A"]
    bad_checksum = good[:-1] + ("Z" if good[-1] != "Z" else "Y")  # always another key (1 in 62 keys ends in Z)
    for bad in ("qt_test_garbage", bad_checksum, keys.generate("dev")[0]):
        assert_problem(c.get("/v1/jobs", headers={"Authorization": f"Bearer {bad}"}), 401, "unauthorized")
    assert svc.keys["A"] not in json.dumps(p)
    # 404: unknown id, unknown route
    assert_problem(c.get("/v1/jobs/job_00000000000000000000000000", headers=svc.h()), 404, "not-found")
    assert_problem(c.get("/v1/nope", headers=svc.h()), 404, "not-found")
    # 403: read-only key on a write endpoint, write key on admin
    r = c.post("/v1/keys", json={"scopes": ["read"], "name": "ro"}, headers=svc.h(Idempotency_Key="k-ro"))
    svc.keys["RO"] = r.json()["secret"]
    assert_problem(c.post("/v1/uploads", json={}, headers=svc.h("RO", Idempotency_Key="x")), 403, "forbidden")
    r = c.post("/v1/keys", json={"scopes": ["write"], "name": "w"}, headers=svc.h(Idempotency_Key="k-w"))
    svc.keys["W"] = r.json()["secret"]
    assert_problem(c.get("/v1/keys", headers=svc.h("W")), 403, "forbidden")
    # 422: validation with errors[] locations
    p = assert_problem(
        c.post("/v1/replays", json={"dataset": {"dataset_id": "ds_x"}, "bogus": 1}, headers=svc.h(Idempotency_Key="v")),
        422,
        "validation-error",
    )
    assert ["body", "bogus"] in [e["loc"] for e in p["errors"]]
    p = assert_problem(
        c.post("/v1/replays", json={"dataset": {"dataset_id": "ds_x"}}, headers=svc.h()), 422, "validation-error"
    )
    assert p["errors"][0]["loc"] == ["header", "Idempotency-Key"]
    p = assert_problem(
        c.post("/v1/replays", json={"dataset": {"path": "/etc"}}, headers=svc.h(Idempotency_Key="p")),
        422,
        "validation-error",
    )
    assert p["errors"][0]["loc"] == ["body", "dataset"]
    p = assert_problem(
        c.post(
            "/v1/replays", json={"dataset": {"dataset_id": "d"}, "out": "/tmp/x"}, headers=svc.h(Idempotency_Key="o")
        ),
        422,
        "validation-error",
    )
    assert p["errors"][0]["loc"] == ["body", "out"]
    # 409: cancel of a terminal job
    svc.db.q(
        "insert into jobs (id, tenant_id, kind, state, resource_type, resource_id) "
        "values ('job_T0000000000000000000000000', %s, 'replay', 'succeeded', 'replay', 'rp_x')",
        (svc.tenants["A"],),
    )
    assert_problem(
        c.post("/v1/jobs/job_T0000000000000000000000000/cancel", headers=svc.h(Idempotency_Key="c")),
        409,
        "job-terminal",
    )
    # 413: JSON body over 1 MiB, upload over the limit
    big = b'{"x": "' + b"a" * (1 << 20) + b'"}'
    assert_problem(
        c.post("/v1/replays", content=big, headers=svc.h(Idempotency_Key="b") | {"content-type": "application/json"}),
        413,
        "payload-too-large",
    )
    body = {"purpose": "other", "filename": "f", "size_bytes": svc.settings.max_upload_bytes + 1, "sha256": "0" * 64}
    assert_problem(c.post("/v1/uploads", json=body, headers=svc.h(Idempotency_Key="u")), 413, "payload-too-large")


@pytest.mark.requires_postgres
def test_basic_auth_security_headers_and_revocation(svc):
    c = svc.client
    basic = base64.b64encode(f"{svc.keys['A']}:".encode()).decode()
    r = c.get("/v1/tenant", headers={"Authorization": f"Basic {basic}"})
    assert r.status_code == 200 and r.json()["id"] == svc.tenants["A"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert r.headers["x-request-id"].startswith("req_")
    assert "access-control-allow-origin" not in r.headers
    with_pw = base64.b64encode(f"{svc.keys['A']}:pw".encode()).decode()
    assert c.get("/v1/tenant", headers={"Authorization": f"Basic {with_pw}"}).status_code == 401
    r = c.post("/v1/keys", json={"scopes": ["read"], "name": "tmp"}, headers=svc.h(Idempotency_Key="k1"))
    assert r.status_code == 201
    created = r.json()
    assert created["schema"] == "qt.api_key_created" and keys.parse(created["secret"])
    tmp = {"Authorization": f"Bearer {created['secret']}"}
    assert c.get("/v1/tenant", headers=tmp).status_code == 200
    page = c.get("/v1/keys", headers=svc.h()).json()
    assert created["id"] in [k["id"] for k in page["data"]] and "secret" not in json.dumps(page)
    assert c.delete(f"/v1/keys/{created['id']}", headers=svc.h()).status_code == 204
    assert c.get("/v1/tenant", headers=tmp).status_code == 401  # revocation is immediate
    assert c.delete(f"/v1/keys/{created['id']}", headers=svc.h()).status_code == 404
    # stored: only prefix + HMAC; the secret is nowhere in the database
    dump = json.dumps(svc.db.q("select * from api_keys"), default=str) + json.dumps(
        svc.db.q("select * from idempotency_keys"), default=str
    )
    assert created["secret"] not in dump
    # replay of the create returns secret null
    r2 = c.post("/v1/keys", json={"scopes": ["read"], "name": "tmp"}, headers=svc.h(Idempotency_Key="k1"))
    assert r2.status_code == 201 and r2.json()["secret"] is None and r2.json()["id"] == created["id"]
    actions = [a for (a,) in svc.db.q("select action from audit_log order by at")]
    assert "key.create" in actions and "key.revoke" in actions


@pytest.mark.requires_postgres
def test_last_used_updated_at_most_once_per_minute(svc):
    svc.client.get("/v1/tenant", headers=svc.h())
    (t1,) = svc.db.q("select last_used_at from api_keys where tenant_id = %s", (svc.tenants["A"],))[0]
    svc.client.get("/v1/tenant", headers=svc.h())
    (t2,) = svc.db.q("select last_used_at from api_keys where tenant_id = %s", (svc.tenants["A"],))[0]
    assert t1 is not None and t1 == t2


@pytest.mark.requires_postgres
def test_500_is_problem_without_stack_trace(svc, monkeypatch):
    from queuetruth_service.routers import meta

    def boom(*a, **k):
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr(meta, "tenant_doc", boom)
    r = svc.client.get("/v1/tenant", headers=svc.h())
    p = assert_problem(r, 500, "internal")
    assert "secret-internal-detail" not in r.text and p["code"] == "QT-ENGINE-001"
