"""Deployment files: `docker compose config -q` (needs only the docker CLI, not the daemon) and structure checks;
the full compose smoke test (up, tenant, replay, download, down) needs a running daemon and is owned by W3."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "compose" / "docker-compose.yml"
ENV = ROOT / "deploy" / "compose" / ".env.example"
DOCKERFILE = ROOT / "deploy" / "docker" / "Dockerfile"


def test_compose_config_valid():
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    r = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--env-file", str(ENV), "config", "-q"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr


def test_compose_structure():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(COMPOSE.read_text())
    svcs = doc["services"]
    assert set(svcs) == {"postgres", "seaweedfs", "migrate", "api", "worker"}
    assert svcs["postgres"]["image"].startswith("postgres:16")
    assert svcs["seaweedfs"]["image"].startswith("chrislusf/seaweedfs:")
    for name in ("postgres", "seaweedfs", "api", "worker"):
        assert "healthcheck" in svcs[name], name
    for name in ("migrate", "api", "worker"):
        s = svcs[name]
        assert s["read_only"] is True and s["cap_drop"] == ["ALL"] and "no-new-privileges:true" in s["security_opt"]
    assert "ports" not in svcs["postgres"]
    env = svcs["api"]["environment"]
    assert env["QT_ENV"] == "prod" and env["QT_BILLER"] == "noop" and env["QT_STORE"] == "s3"
    assert "QT_STRIPE_API_KEY" not in env


def test_dockerfile_multistage_non_root():
    text = DOCKERFILE.read_text()
    assert text.count("\nFROM ") + text.startswith("FROM ") >= 2
    assert "USER 10001" in text and "useradd" in text


def test_env_example_has_no_real_secrets():
    for line in ENV.read_text().splitlines():
        if line and not line.startswith("#"):
            assert line.split("=", 1)[1].startswith("change-me"), line


@pytest.mark.requires_docker
def test_compose_smoke_needs_daemon():
    pytest.skip("compose smoke test: run scripts/compose_smoke.sh")
