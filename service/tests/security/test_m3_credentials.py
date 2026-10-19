"""M3 (security review, inverted): one leaked `qt_app` password is no longer every tenant. A session cannot choose
its tenant (the binding needs a key's HMAC, a held job attempt, or the worker role), bindings cannot be replayed,
`qt_tenant_ids()` and the job functions are worker-only, `qt_finish_job` checks the bound tenant, job children get a
minimal environment without any service secret, and compose gives the api and the worker only their app-role DSN."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import psycopg
import pytest
import yaml
from svc_support import insert_job

from queuetruth_service import keys
from queuetruth_service.executor import CHILD_ENV_ALLOW, run_child

COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "compose" / "docker-compose.yml"


def _denied(conn: psycopg.Connection, sql: str, params: tuple = ()) -> bool:
    try:
        with conn.transaction():
            conn.execute(sql, params).fetchall()
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedFunction):
        return True
    return False


@pytest.mark.requires_postgres
def test_qt_app_session_cannot_choose_or_enumerate_tenants(svc):
    a, b = svc.tenants["A"], svc.tenants["B"]
    job_b = insert_job(svc, "B")
    with psycopg.connect(svc.db.app) as c:
        assert _denied(c, "select id from qt_tenant_ids()")
        assert _denied(c, "select * from auth_lookup(%s)", ("x",))  # the hash-returning lookup is gone
        assert _denied(c, "select qt_bind_tenant(%s)", (b,))
        assert _denied(c, "select qt_bind_job(%s, 1)", (job_b,))
        assert _denied(c, "select qt_binding_mac(%s)", (b,))
        assert _denied(c, "select * from qt_binding_key")
        assert _denied(c, "select * from qt_claim_job(%s, 60, 0)", (job_b,))
        assert _denied(c, "select qt_finish_job(%s, 1, 'failed', null, null, null)", (job_b,))
        assert _denied(c, "select qt_mark_enqueued(%s)", ([job_b],))
        with c.transaction():
            c.execute("select set_config('app.tenant_id', %s, true)", (b,))  # self-chosen tenant
            assert c.execute("select count(*) from api_keys").fetchone()[0] == 0
            assert c.execute("select count(*) from jobs").fetchone()[0] == 0
        with c.transaction():  # a valid key binds exactly its tenant
            row = c.execute(
                "select tenant_id from qt_authenticate(%s, %s)",
                (keys.parse(svc.keys["A"]), keys.key_hash(svc.settings.pepper, svc.keys["A"])),
            ).fetchone()
            assert row == (a,)
            assert {r[0] for r in c.execute("select tenant_id from api_keys").fetchall()} == {a}
            assert c.execute("select count(*) from jobs where id = %s", (job_b,)).fetchone()[0] == 0
    assert svc.db.q("select state from jobs where id = %s", (job_b,))[0][0] == "queued"


@pytest.mark.requires_postgres
def test_a_binding_cannot_be_replayed_or_retargeted(svc):
    a, b = svc.tenants["A"], svc.tenants["B"]
    proof = (keys.parse(svc.keys["A"]), keys.key_hash(svc.settings.pepper, svc.keys["A"]))
    with psycopg.connect(svc.db.app) as c:
        with c.transaction():
            c.execute("select * from qt_authenticate(%s, %s)", proof)
            sig = c.execute("select current_setting('app.tenant_sig')").fetchone()[0]
            c.execute("select set_config('app.tenant_id', %s, true)", (b,))  # keep A's signature, claim B
            assert c.execute("select qt_current_tenant()").fetchone()[0] is None
        with c.transaction():  # the same signature in a later transaction
            c.execute("select set_config('app.tenant_id', %s, true), set_config('app.tenant_sig', %s, true)", (a, sig))
            assert c.execute("select qt_current_tenant()").fetchone()[0] is None
    with psycopg.connect(svc.db.app) as other, other.transaction():  # ... or another session
        other.execute("select set_config('app.tenant_id', %s, true), set_config('app.tenant_sig', %s, true)", (a, sig))
        assert other.execute("select count(*) from api_keys").fetchone()[0] == 0
    with psycopg.connect(svc.db.app) as c, c.transaction():  # a revoked key binds nothing
        svc.db.q("update api_keys set revoked_at = now() where tenant_id = %s", (a,))
        assert c.execute("select * from qt_authenticate(%s, %s)", proof).fetchall() == []


@pytest.mark.requires_postgres
def test_worker_functions_check_the_bound_tenant(svc):
    a = svc.tenants["A"]
    job_b = insert_job(svc, "B")
    with psycopg.connect(svc.db.worker, autocommit=True) as w:
        row = w.execute("select tenant_id, attempts, deferred from qt_claim_job(%s, 60, 0)", (job_b,)).fetchone()
        assert row == (svc.tenants["B"], 1, False)
        with w.transaction():
            w.execute("select qt_bind_tenant(%s)", (a,))  # bound to another tenant than the job's
            assert w.execute("select qt_finish_job(%s, 1, 'failed', null, null, null)", (job_b,)).fetchone()[0] is False
        with w.transaction():
            assert w.execute("select qt_bind_job(%s, 2)", (job_b,)).fetchone()[0] is None  # not the held attempt
        with w.transaction():
            assert w.execute("select qt_bind_job(%s, 1)", (job_b,)).fetchone()[0] == svc.tenants["B"]
            assert w.execute("select qt_finish_job(%s, 1, 'failed', null, null, null)", (job_b,)).fetchone()[0]
    assert svc.db.q("select state from jobs where id = %s", (job_b,))[0][0] == "failed"


def test_job_child_gets_a_minimal_environment(tmp_path, monkeypatch):
    fake = {
        "QT_DATABASE_URL": "postgresql://postgres:SUPERUSER-PW@postgres:5432/queuetruth",
        "QT_DATABASE_URL_APP": "postgresql://qt_app:APP-PW@postgres:5432/queuetruth",
        "QT_DATABASE_URL_WORKER": "postgresql://qt_worker:WORKER-PW@postgres:5432/queuetruth",
        "QT_KEY_PEPPER": "PEPPER-VALUE",
        "QT_SIGNING_KEY": "SIGNING-VALUE",
        "QT_S3_SECRET_ACCESS_KEY": "S3-SECRET",
        "QT_STRIPE_API_KEY": "sk_test_STRIPE",
        "AWS_SECRET_ACCESS_KEY": "AWS-SECRET",
        "PGPASSWORD": "PG-SECRET",
    }
    for k, v in fake.items():
        monkeypatch.setenv(k, v)
    spec = {"needles": list(fake.values())}
    res = asyncio.run(run_child("queuetruth_service.testing:fake_env", spec, str(tmp_path), timeout_s=60, allow=[]))
    assert res.status == "ok", res.error
    env = res.summary["env"]
    assert not any(k.startswith(("QT_", "AWS_", "PG")) for k in env), sorted(env)
    assert not set(fake.values()) & set(env.values())
    own = {k for k in env if not k.startswith("__CF_")}  # macOS CoreFoundation adds its own at process start
    assert own <= {*CHILD_ENV_ALLOW, "HOME", "TMPDIR", "TMP", "TEMP", "MPLCONFIGDIR", "PYTHONDONTWRITEBYTECODE"}
    real = os.path.realpath(tmp_path)
    assert os.path.realpath(env["HOME"]) == real and os.path.realpath(env["TMPDIR"]) == os.path.join(real, "tmp")
    assert res.summary["leaked_in_proc_environ"] == []  # Linux: not even in the initial environment block


def test_compose_gives_each_service_only_its_role():
    doc = yaml.safe_load(COMPOSE.read_text())
    svcs = doc["services"]
    api, worker, migrate = (svcs[n]["environment"] for n in ("api", "worker", "migrate"))
    assert api["QT_DATABASE_URL_APP"].startswith("postgresql://qt_app:")
    assert worker["QT_DATABASE_URL_WORKER"].startswith("postgresql://qt_worker:")
    for env in (api, worker):
        assert "QT_DATABASE_URL" not in env and "postgresql://postgres:" not in str(env)
    assert "QT_DATABASE_URL_WORKER" not in api and "QT_DATABASE_URL_APP" not in worker
    assert "QT_KEY_PEPPER" not in worker and "QT_SIGNING_KEY" not in worker and "QT_STRIPE_API_KEY" not in worker
    assert migrate["QT_DATABASE_URL"].startswith("postgresql://postgres:")  # the migration runner alone
    owners = [n for n, s in svcs.items() if "postgresql://postgres:" in str(s.get("environment", {}))]
    assert owners == ["migrate"]
