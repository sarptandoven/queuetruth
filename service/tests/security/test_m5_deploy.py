"""M5 (security review, inverted): deploy defaults. `ENV=prod` refuses placeholder, empty, short or low-variety
secrets (including DSN passwords) and `QT_FORWARDED_ALLOW_IPS=*`; compose publishes the api and S3 on 127.0.0.1
unless overridden; the api trusts X-Forwarded-* only from configured proxies (default none); the worker has memory
and pids limits and private per-job directories (with their own tmp/) instead of one shared RAM /tmp."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from svc_support import insert_job
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from queuetruth_service.admin import uvicorn_options
from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.settings import Settings
from queuetruth_service.worker import Worker

ROOT = Path(__file__).resolve().parents[3] / "deploy"
COMPOSE = ROOT / "compose" / "docker-compose.yml"
ENV_EXAMPLE = ROOT / "compose" / ".env.example"


def _env_example() -> dict[str, str]:
    out = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _prod(**kw: str) -> Settings:
    good = secrets.token_urlsafe(32)
    base = {"env": "prod", "key_pepper": good, "signing_key": secrets.token_urlsafe(32)}
    return Settings(**(base | kw))


def test_prod_refuses_the_env_example_placeholders():
    e = _env_example()
    with pytest.raises(ValueError, match="placeholder") as info:
        Settings(
            env="prod",
            key_pepper=e["QT_KEY_PEPPER"],
            signing_key=e["QT_SIGNING_KEY"],
            database_url_app=f"postgresql://qt_app:{e['QT_APP_PASSWORD']}@postgres:5432/queuetruth",
            store="s3",
            s3_endpoint="http://seaweedfs:8333",
            s3_access_key_id=e["QT_S3_ACCESS_KEY_ID"],
            s3_secret_access_key=e["QT_S3_SECRET_ACCESS_KEY"],
        )
    text = str(info.value)
    for name in ("QT_KEY_PEPPER", "QT_SIGNING_KEY", "QT_S3_SECRET_ACCESS_KEY", "QT_S3_ACCESS_KEY_ID"):
        assert name in text
    assert "the password in QT_DATABASE_URL_APP" in text


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("", "empty"),
        ("short-but-random-9x", "shorter"),
        ("a" * 40, "distinct"),
        ("CHANGEME" + "x7" * 20, "placeholder"),
    ],
)
def test_prod_refuses_weak_secrets(value, why):
    with pytest.raises(ValueError, match=why):
        _prod(key_pepper=value)


def test_prod_accepts_generated_secrets_and_refuses_trust_in_any_proxy():
    pw = secrets.token_urlsafe(24)
    s = _prod(database_url_app=f"postgresql://qt_app:{pw}@postgres/q", forwarded_allow_ips="10.0.0.0/8")
    assert s.env == "prod"
    with pytest.raises(ValueError, match=r"QT_FORWARDED_ALLOW_IPS=\*"):
        _prod(forwarded_allow_ips="10.0.0.1, *")
    with pytest.raises(ValueError, match="password in QT_DATABASE_URL_WORKER"):
        _prod(database_url_worker="postgresql://qt_worker:hunter2@postgres/q")


def test_compose_publishes_on_loopback_and_trusts_no_proxy():
    doc = yaml.safe_load(COMPOSE.read_text())
    svcs = doc["services"]
    assert svcs["api"]["ports"] == ["${QT_API_BIND:-127.0.0.1}:${QT_API_PORT:-8080}:8080"]
    assert svcs["seaweedfs"]["ports"] == ["${QT_S3_BIND:-127.0.0.1}:${QT_S3_PORT:-8333}:8333"]
    assert [n for n, s in svcs.items() if "ports" in s] == ["seaweedfs", "api"]
    cmd = svcs["api"]["command"]
    assert "--forwarded-allow-ips" not in cmd and "--proxy-headers" not in cmd and "*" not in cmd
    assert svcs["api"]["environment"]["QT_FORWARDED_ALLOW_IPS"] == "${QT_FORWARDED_ALLOW_IPS:-}"
    for name in ("QT_API_BIND", "QT_S3_BIND", "QT_FORWARDED_ALLOW_IPS"):
        assert name in COMPOSE.read_text().split("name: queuetruth")[0]  # documented in the header


def test_compose_worker_limits_and_job_directories():
    svcs = yaml.safe_load(COMPOSE.read_text())["services"]
    w = svcs["worker"]
    assert w["mem_limit"].startswith("${QT_WORKER_MEM_LIMIT:-") and int(w["pids_limit"]) <= 1024
    assert w["ulimits"]["core"] == 0
    assert w["tmpfs"] != ["/tmp:size=4g"] and all("size=4g" not in t for t in w["tmpfs"])
    assert w["environment"]["QT_JOB_ROOT"] == "/var/lib/queuetruth/jobs"
    assert "jobs:/var/lib/queuetruth/jobs" in w["volumes"]
    assert "pids_limit" in svcs["api"]
    docker = (ROOT / "docker" / "Dockerfile").read_text()
    assert "chown 10001:10001 /var/lib/queuetruth/jobs" in docker and "chmod 0700 /var/lib/queuetruth/jobs" in docker


def test_compose_resolves_to_loopback_ports():
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    r = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--env-file", str(ENV_EXAMPLE), "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    svcs = json.loads(r.stdout)["services"]
    for name in ("api", "seaweedfs"):
        assert [p["host_ip"] for p in svcs[name]["ports"]] == ["127.0.0.1"], name


def test_forwarded_headers_are_ignored_unless_the_proxy_is_listed():
    opts = uvicorn_options(Settings())
    assert opts["proxy_headers"] is False and opts["forwarded_allow_ips"] is None and opts["log_config"] is None
    listed = uvicorn_options(Settings(forwarded_allow_ips="10.0.0.5"))
    assert listed["proxy_headers"] is True and listed["forwarded_allow_ips"] == "10.0.0.5"

    def run(client: str) -> dict:
        seen: dict = {}

        async def app(scope, receive, send):
            seen["client"], seen["scheme"] = scope["client"], scope["scheme"]

        mw = ProxyHeadersMiddleware(app, trusted_hosts=listed["forwarded_allow_ips"])
        scope = {
            "type": "http",
            "scheme": "http",
            "client": (client, 5555),
            "headers": [(b"x-forwarded-for", b"10.9.9.9"), (b"x-forwarded-proto", b"https")],
        }
        asyncio.run(mw(scope, None, None))
        return seen

    assert run("203.0.113.9") == {"client": ("203.0.113.9", 5555), "scheme": "http"}  # a direct client: ignored
    assert run("10.0.0.5") == {"client": ("10.9.9.9", 0), "scheme": "https"}  # the listed proxy


@pytest.mark.requires_postgres
def test_each_job_gets_a_private_directory_with_its_own_tmp(svc, tmp_path):
    root = tmp_path / "jobs"
    jobs = [insert_job(svc) for _ in range(2)]

    async def go() -> None:
        pool = await make_async_pool(svc.db.worker)
        try:
            s = svc.settings.model_copy(update={"job_root": str(root)})
            w = Worker(s, pool, svc.store, None, targets={"replay": "queuetruth_service.testing:fake_env"})
            for j in jobs:
                svc.db.q("update jobs set spec = '{}' where id = %s", (j,))
                assert await w.handle(j) == "succeeded"
        finally:
            await pool.close()

    asyncio.run(go())
    seen = []
    for j in jobs:
        key = svc.db.q("select object_key from job_files where job_id = %s and name = 'env.json'", (j,))[0][0]
        seen.append(json.loads(svc.store.path(key).read_text()))
    real_root = os.path.realpath(root)
    cwds = {os.path.realpath(d["cwd"]) for d in seen}
    assert len(cwds) == 2 and all(os.path.dirname(c) == real_root for c in cwds)  # one directory per job
    for d in seen:
        assert d["cwd_mode"] == oct(0o700)
        assert os.path.realpath(d["env"]["TMPDIR"]) == os.path.join(os.path.realpath(d["cwd"]), "tmp")
        assert os.path.realpath(d["env"]["HOME"]) == os.path.realpath(d["cwd"])
    assert list(root.iterdir()) == []  # removed after each job
