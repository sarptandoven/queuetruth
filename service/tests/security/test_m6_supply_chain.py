"""M6 (security review, inverted): the image installs exactly the locked, hash-pinned dependency set (exported from
uv.lock, pip --require-hashes --no-deps) and every base image is pinned by digest; CI runs cargo-audit and pip-audit
(`.github/workflows/ci.yml`)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "deploy" / "docker" / "Dockerfile"
COMPOSE = REPO / "deploy" / "compose" / "docker-compose.yml"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _run_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.replace("\\\n", " ").splitlines() if ln.strip()]


def test_runtime_installs_only_the_locked_hashed_set():
    text = DOCKERFILE.read_text()
    assert "uv export --frozen --no-dev --no-emit-workspace --package queuetruth-service" in " ".join(text.split())
    installs = [ln for ln in _run_lines(text) if "pip install" in ln]
    assert installs and all("--no-deps" in seg for ln in installs for seg in ln.split("&&") if "pip install" in seg)
    assert any("--require-hashes -r /wheels/requirements.txt" in ln for ln in installs)
    assert "pip check" in text
    assert "pip install --no-cache-dir /wheels/*.whl" not in text  # no resolution of transitive ranges


def test_base_images_pinned_by_digest():
    text = DOCKERFILE.read_text()
    images = re.findall(r"^FROM\s+(\S+)", text, flags=re.M) + re.findall(r"COPY --from=(\S+?\S*:\S+)", text)
    images = [i for i in images if ":" in i]
    assert len(images) == 3 and all(DIGEST.search(i) for i in images), images
    svcs = yaml.safe_load(COMPOSE.read_text())["services"]
    pinned = [s["image"] for s in svcs.values() if "build" not in s]
    assert pinned and all(DIGEST.search(i) for i in pinned), pinned


def test_the_export_covers_every_runtime_dependency_with_hashes(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv not installed")
    out = tmp_path / "req.txt"
    r = subprocess.run(
        [
            "uv", "export", "--frozen", "--no-dev", "--no-emit-workspace", "--package", "queuetruth-service",
            "--format", "requirements-txt", "--output-file", str(out),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    reqs = [b for b in re.split(r"\n(?=[a-z0-9])", out.read_text()) if "==" in b and not b.startswith("#")]
    names = {b.split("==")[0] for b in reqs}
    assert {"fastapi", "uvicorn", "psycopg", "pgqueuer", "boto3", "pydantic-settings", "numpy", "pyarrow"} <= names
    assert all("--hash=sha256:" in b for b in reqs)
    assert not any(b.startswith(("queuetruth==", "queuetruth-service==")) for b in reqs)  # local wheels


def test_ci_audits_dependencies():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text().lower()
    assert "cargo audit" in ci and "pip-audit" in ci
