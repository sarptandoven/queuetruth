"""Settings (SPEC 8.1), structured logs without secrets (SPEC 8.10) and the admin console script."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

import pytest

from queuetruth_service import keys, logs
from queuetruth_service.settings import Settings

PEPPER = "Pp0-rand0m-pepper-value-1234567890abcdef"
SIGNING = "Ss9-rand0m-signing-value-0987654321fedcba"


def test_prod_requires_secrets_and_secrets_hidden(monkeypatch):
    with pytest.raises(ValueError, match="QT_KEY_PEPPER"):
        Settings(env="prod").require("api")
    with pytest.raises(ValueError, match="QT_DATABASE_URL_WORKER"):
        Settings(env="prod").require("worker")
    s = Settings(env="prod", key_pepper=PEPPER, signing_key=SIGNING, database_url_app="postgresql://qt_app@x/q")
    s.require("api")
    assert PEPPER not in repr(s) and SIGNING not in repr(s)
    assert s.pepper == PEPPER.encode()
    with pytest.raises(ValueError, match="S3_ENDPOINT"):
        Settings(store="s3")
    monkeypatch.setenv("QT_ZERO_EGRESS", "true")
    monkeypatch.setenv("QT_RATE_LIMITS", json.dumps({"read": [1, 2], "write": [3, 4], "submit": [5, 6]}))
    monkeypatch.setenv("QT_MAX_UPLOAD_BYTES", "123")
    e = Settings()
    assert e.zero_egress and e.rate_limits["submit"] == (5, 6) and e.max_upload_bytes == 123
    assert e.effective_biller == "noop" and e.env == "dev" and e.part_size_bytes == 67108864
    assert Settings().job_timeout_s == 7200 and Settings().worker_concurrency == 2


def test_job_timeout_per_kind(monkeypatch):
    """SPEC 8.1 (open conflict 9): `QT_JOB_TIMEOUTS_S` sets the child timeout per job kind; other kinds use
    `QT_JOB_TIMEOUT_S`. Unknown kinds and non-positive values are configuration errors."""
    assert Settings().job_timeout_for("replay") == 7200 and Settings().job_timeouts_s == {}
    monkeypatch.setenv("QT_JOB_TIMEOUT_S", "600")
    monkeypatch.setenv("QT_JOB_TIMEOUTS_S", json.dumps({"calibration": 14400, "report": 120}))
    s = Settings()
    assert (s.job_timeout_for("calibration"), s.job_timeout_for("report"), s.job_timeout_for("replay")) == (
        14400,
        120,
        600,
    )
    for bad in ({"replays": 10}, {"replay": 0}):
        monkeypatch.setenv("QT_JOB_TIMEOUTS_S", json.dumps(bad))
        with pytest.raises(ValueError, match="QT_JOB_TIMEOUTS_S"):
            Settings()


def test_json_logs_redact_keys():
    key, _ = keys.generate("prod")
    rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "bad key %s", (key,), None)
    token = logs.request_id_var.set("req_1")
    try:
        line = logs.JsonFormatter().format(rec)
    finally:
        logs.request_id_var.reset(token)
    doc = json.loads(line)
    assert key not in line and doc["request_id"] == "req_1" and doc["level"] == "warning"


@pytest.mark.requires_postgres
def test_no_secrets_in_logs_on_errors(svc, caplog, monkeypatch):
    from queuetruth_service.routers import meta

    monkeypatch.setattr(meta, "tenant_doc", lambda *a: 1 / 0)
    with caplog.at_level(logging.DEBUG):
        svc.client.get("/v1/tenant", headers=svc.h())
        svc.client.get("/v1/tenant", headers={"Authorization": f"Bearer {svc.keys['B'][:-2]}xx"})
    text = "\n".join(logs.JsonFormatter().format(r) for r in caplog.records)
    assert svc.keys["A"] not in text and svc.keys["B"][:20] not in text


@pytest.mark.requires_postgres
def test_admin_migrate_and_create_tenant(pg, svc):
    name = pg.create_database()
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "QT_"))}
        env["QT_DATABASE_URL"] = pg.dsn(name)
        run = [sys.executable, "-m", "queuetruth_service.admin"]
        r = subprocess.run([*run, "migrate"], env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["applied"][0] == "0001_core.sql"
        r = subprocess.run([*run, "migrate"], env=env, capture_output=True, text=True, timeout=120)
        assert json.loads(r.stdout)["applied"] == []
        r = subprocess.run(
            [*run, "create-tenant", "--name", "Acme"], env=env, capture_output=True, text=True, timeout=120
        )
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["tenant_id"].startswith("ten_") and keys.parse(out["key"]) and out["key"].startswith("qt_test_")
        assert out["key"] not in r.stderr
        r = subprocess.run([*run, "create-tenant"], env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 2
        # Password-less DSNs keep this check about the missing pepper: CI's Postgres has a weak password, which
        # ENV=prod refuses first (the command exits before connecting either way).
        env_prod = env | {
            "QT_ENV": "prod",
            "QT_DATABASE_URL": "postgresql:///queuetruth",
            "QT_DATABASE_URL_APP": "postgresql://qt_app@localhost/queuetruth",
            "QT_DATABASE_URL_WORKER": "postgresql://qt_worker@localhost/queuetruth",
        }
        r = subprocess.run(
            [*run, "create-tenant", "--name", "x"], env=env_prod, capture_output=True, text=True, timeout=60
        )
        assert r.returncode == 2 and "QT_KEY_PEPPER" in r.stderr
        r = subprocess.run(
            [*run, "migrate"], env=env_prod | {"QT_KEY_PEPPER": "change-me"}, capture_output=True, text=True, timeout=60
        )
        assert r.returncode == 2 and "placeholder" in r.stderr
        for cmd in ("api", "worker"):  # no app-role DSN: refuse to start (security review H1)
            r = subprocess.run([*run, cmd], env=env, capture_output=True, text=True, timeout=60)
            assert r.returncode == 2 and "QT_DATABASE_URL_" in r.stderr, (cmd, r.stderr)
    finally:
        pg.drop_database(name)
