"""H1 (security review, inverted): tenant isolation no longer fails open. The api and the worker refuse to start
unless they connect as their unprivileged app role (no owner-DSN fallback in any ENV; superuser, BYPASSRLS, owner and
wrong-role connections are refused), tenant-scoped queries filter by `tenant_id` explicitly (isolation holds with RLS
switched off), and the dev local-store signing key is random per install."""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import stat
import urllib.parse
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from svc_support import UNLIMITED, insert_job, seed_dataset, succeed_job, upload_bytes, verify_uploads

from queuetruth_service.admin import create_tenant
from queuetruth_service.app import create_app
from queuetruth_service.db.pool import RoleError, make_async_pool, make_pool
from queuetruth_service.settings import ConfigError, Settings
from queuetruth_service.store import job_key
from queuetruth_service.store.local import LocalStore

SERVICE = Path(__file__).resolve().parents[2] / "queuetruth_service"
OLD_DEV_SIGNING_KEY = b"qt-dev-signing-key-not-for-production"  # the public constant of the reviewed version
TENANT_TABLES = (
    "api_keys", "idempotency_keys", "rate_limit_buckets", "uploads", "datasets", "jobs", "job_files", "replays",
    "calibrations", "scorecards", "reports", "usage_events", "tombstones", "audit_log",
)  # fmt: skip


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("QT_ENV", "QT_DATABASE_URL_APP", "QT_DATABASE_URL_WORKER", "QT_SIGNING_KEY", "QT_KEY_PEPPER"):
        monkeypatch.delenv(var, raising=False)
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])


def _startup(s: Settings) -> None:
    with TestClient(create_app(s), raise_server_exceptions=True):
        pass


@pytest.mark.requires_postgres
def test_api_refuses_to_start_without_or_with_a_privileged_app_role(db, tmp_path, clean_env):
    base = {"database_url": db.owner, "store": "local", "store_root": str(tmp_path / "s"), "rate_limits": UNLIMITED}
    s = Settings(**base)
    assert s.env == "dev" and s.database_url_app is None
    with pytest.raises(ConfigError, match="QT_DATABASE_URL_APP"):
        s.app_dsn  # noqa: B018  (no fallback to the owner DSN, dev included)
    with pytest.raises(ConfigError, match="QT_DATABASE_URL_APP"):
        _startup(s)
    with psycopg.connect(db.owner) as c:  # the owner is a superuser (the migration runner requires it)
        assert c.execute("select rolsuper or rolbypassrls from pg_roles where rolname = current_user").fetchone()[0]
    with pytest.raises(RoleError, match="superuser or has BYPASSRLS"):
        _startup(Settings(**base, database_url_app=db.owner))
    with pytest.raises(RoleError, match="the api must use qt_app"):
        _startup(Settings(**base, database_url_app=db.worker))
    _startup(Settings(**base, database_url_app=db.app))  # the real app role starts


@pytest.mark.requires_postgres
def test_pools_verify_rolsuper_bypassrls_and_ownership(db):
    pw = psycopg.conninfo.conninfo_to_dict(db.app).get("password")  # QT_PG_DSN servers authenticate TCP logins
    password = sql.SQL(" password {}").format(sql.Literal(str(pw))) if pw else sql.SQL("")
    with psycopg.connect(db.owner, autocommit=True) as c:
        c.execute(sql.SQL("create role qt_sneaky login nosuperuser bypassrls{}").format(password))
        c.execute("grant qt_app to qt_sneaky")
        c.execute(sql.SQL("create role qt_heir login nosuperuser nobypassrls{}").format(password))
        owner = c.execute("select current_user").fetchone()[0]
        c.execute(sql.SQL("grant {} to qt_heir").format(sql.Identifier(owner)))  # could SET ROLE to the owner
    try:
        for user, match in (("qt_sneaky", "BYPASSRLS"), ("qt_heir", "superuser")):
            dsn = psycopg.conninfo.make_conninfo(db.app, user=user)
            with pytest.raises(RoleError, match=match):
                make_pool(dsn, purpose="api").close()
        with pytest.raises(RoleError):
            make_pool(db.owner, purpose="api").close()

        async def worker_pools() -> None:
            with pytest.raises(RoleError, match="superuser"):
                await make_async_pool(db.owner, purpose="worker")
            with pytest.raises(RoleError, match="cannot claim jobs"):
                await make_async_pool(db.app, purpose="worker")
            pool = await make_async_pool(db.worker, purpose="worker")
            await pool.close()

        asyncio.run(worker_pools())
    finally:
        with psycopg.connect(db.owner, autocommit=True) as c:
            c.execute("drop role qt_sneaky")
            c.execute("drop role qt_heir")


def test_prod_refuses_the_owner_dsn_as_app_dsn():
    pepper, signing = "N3w-pepper-" + "a1b2c3d4e5f6g7h8i9j0", "N3w-signing-" + "z9y8x7w6v5u4t3s2r1q0"
    dsn = "postgresql://postgres:Xq7-Lm2-Rt9-Vb4-Kp8@db/queuetruth"
    with pytest.raises(ValueError, match="must not be the owner DSN"):
        Settings(env="prod", key_pepper=pepper, signing_key=signing, database_url=dsn, database_url_app=dsn)
    with pytest.raises(ValueError, match="must not be the owner DSN"):
        Settings(env="prod", key_pepper=pepper, signing_key=signing, database_url=dsn, database_url_worker=dsn)


@pytest.mark.requires_postgres
def test_production_path_isolates_tenants_and_dev_urls_cannot_be_forged(db, tmp_path, clean_env):
    s = Settings(
        database_url=db.owner,
        database_url_app=db.app,
        store="local",
        store_root=str(tmp_path / "store"),
        rate_limits=UNLIMITED,
        auth_failure_limit=(1e6, 1e6),
    )
    a = create_tenant(db.owner, s, "tenant A")
    b = create_tenant(db.owner, s, "tenant B")
    secret = b"TENANT-B-PRIVATE-FILLS"
    job_b = "job_01JB0000000000000000000000"
    key_b = job_key(b["tenant_id"], job_b, "fills.parquet")
    p = Path(s.store_root) / "objects" / key_b
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(secret)
    with psycopg.connect(db.owner, autocommit=True) as c:
        c.execute(
            "insert into jobs (id, tenant_id, kind, state, spec, resource_type, resource_id, finished_at) "
            "values (%s, %s, 'replay', 'succeeded', '{}', 'replay', 'rp_x', now())",
            (job_b, b["tenant_id"]),
        )
        c.execute(
            "insert into job_files (job_id, tenant_id, name, bytes, sha256, content_type, object_key) "
            "values (%s, %s, 'fills.parquet', %s, %s, 'application/octet-stream', %s)",
            (job_b, b["tenant_id"], len(secret), "0" * 64, key_b),
        )
    with TestClient(create_app(s), raise_server_exceptions=False) as client:  # lifespan opens make_pool(app_dsn)
        ha = {"Authorization": f"Bearer {a['key']}"}
        assert client.get(f"/v1/jobs/{job_b}", headers=ha).status_code == 404
        assert job_b not in {j["id"] for j in client.get("/v1/jobs", headers=ha).json()["data"]}
        assert b["key_id"] not in {k["id"] for k in client.get("/v1/keys", headers=ha).json()["data"]}
        assert client.get(f"/v1/jobs/{job_b}/files/fills.parquet", headers=ha).status_code == 404
        hb = {"Authorization": f"Bearer {b['key']}"}
        assert client.get(f"/v1/jobs/{job_b}/files/fills.parquet", headers=hb).content == secret
        # the old public dev constant no longer signs anything
        forged = LocalStore(Path(s.store_root), OLD_DEV_SIGNING_KEY).presign_get(key_b, 900)
        u = urllib.parse.urlsplit(forged)
        assert client.get(f"{u.path}?{u.query}").status_code == 403


def test_dev_signing_key_is_random_per_install(tmp_path, clean_env):
    one = Settings(store_root=str(tmp_path / "one"))
    two = Settings(store_root=str(tmp_path / "two"))
    k1, k2 = one.signing_secret, two.signing_secret
    assert k1 != k2 and len(k1) >= 64 and OLD_DEV_SIGNING_KEY not in (k1, k2)
    assert Settings(store_root=str(tmp_path / "one")).signing_secret == k1  # stable for one install
    mode = stat.S_IMODE(os.stat(tmp_path / "one" / ".signing-key").st_mode)
    assert mode == 0o600
    import queuetruth_service.settings as settings_mod

    assert not hasattr(settings_mod, "DEV_SIGNING_KEY")
    with pytest.raises(ConfigError, match="QT_SIGNING_KEY"):
        Settings(env="prod", store_root=str(tmp_path / "p")).signing_secret  # noqa: B018


# ---- defence in depth: explicit tenant filters -------------------------------------------------------------------

SQL_START = re.compile(r"^\s*(select|update|delete|insert|with)\b", re.I)
RELATION = re.compile(r"\b(?:from|update|into|join)\s+(\{\}|[a-z_][a-z0-9_]*)", re.I)


def _sql_strings(path: Path) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            out.append((node.lineno, text))
    return [(n, t) for n, t in out if SQL_START.match(t)]


def test_every_tenant_scoped_query_filters_by_tenant_explicitly():
    files = (
        sorted(SERVICE.glob("routers/*.py"))
        + [
            SERVICE / f
            for f in ("worker.py", "maintenance.py", "retention.py", "refs.py", "estimate.py", "auth.py", "jobs.py")
        ]
        + [SERVICE / "idempotency.py"]
    )
    checked = 0
    for f in files:
        for line, text in _sql_strings(f):
            rels = {r.lower() for r in RELATION.findall(text)}
            if rels & {"tenants"}:
                assert "id = %s" in text, (f.name, line, text)
                checked += 1
            if rels & ({*TENANT_TABLES, "{}"}):
                assert "tenant_id" in text, (f.name, line, text)
                checked += 1
    assert checked >= 60
    # the shared pager composes its SQL: its first condition is always the tenant
    docs = (SERVICE / "documents.py").read_text()
    assert 'where = [sql.SQL("tenant_id = %s")]' in docs and "params: list[Any] = [tenant_id]" in docs
    src = (SERVICE / "routers" / "jobs.py").read_text()
    assert '"select * from jobs where id = %s"' not in src and "tenant_id = %s" in src


@pytest.mark.requires_postgres
def test_isolation_holds_with_row_level_security_switched_off(svc):
    c = svc.client
    up = upload_bytes(svc, b"tenant-a-data" * 10, "A")
    assert verify_uploads(svc)[up] == "verified"
    ds = seed_dataset(svc, "A")
    r = c.post("/v1/replays", json={"dataset": {"dataset_id": ds}}, headers=svc.h("A", Idempotency_Key="r"))
    rp, job = r.json()["id"], r.json()["job"]["id"]
    succeed_job(svc, job, {"out.bin": b"secret-bytes"})
    other = insert_job(svc, "A", state="queued")
    for t in (*TENANT_TABLES, "tenants"):  # what an owner-role mistake or a policy bug would amount to
        svc.db.q(f"alter table {t} no force row level security")
        svc.db.q(f"alter table {t} disable row level security")
    assert svc.db.q("select count(*) from pg_class where relname = 'jobs' and relrowsecurity") == [(0,)]
    b = svc.h("B")
    for path in (
        f"/v1/uploads/{up}",
        f"/v1/datasets/{ds}",
        f"/v1/replays/{rp}",
        f"/v1/jobs/{job}",
        f"/v1/jobs/{other}",
        f"/v1/jobs/{job}/files",
        f"/v1/jobs/{job}/files/out.bin",
    ):
        assert c.get(path, headers=b).status_code == 404, path
    for path in ("/v1/datasets", "/v1/jobs", "/v1/replays", "/v1/keys", "/v1/tombstones"):
        ids = {x["id"] for x in c.get(path, headers=b).json()["data"]}
        assert not ids & {up, ds, rp, job, other}, path
    assert c.post(f"/v1/jobs/{job}/cancel", headers=svc.h("B", Idempotency_Key="x")).status_code == 404
    r = c.post("/v1/replays", json={"dataset": {"dataset_id": ds}}, headers=svc.h("B", Idempotency_Key="y"))
    assert r.status_code == 404
    usage = c.get("/v1/usage?group_by=job", headers=b).json()
    assert all(row["key"] != job for row in usage["rows"])
    assert c.get("/v1/tenant", headers=b).json()["id"] == svc.tenants["B"]
    assert c.get(f"/v1/jobs/{job}", headers=svc.h("A")).status_code == 200
