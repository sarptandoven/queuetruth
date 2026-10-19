"""Shared pytest setup: repository paths and automatic skips for optional environments (markers in pyproject.toml)."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PG_BIN_DEFAULT = "/opt/homebrew/opt/postgresql@16/bin"


def core_available() -> bool:
    """True when the compiled extension `queuetruth._core` is importable."""
    try:
        return importlib.util.find_spec("queuetruth._core") is not None
    except (ImportError, ValueError):
        return False


def postgres_available() -> bool:
    """True with `QT_PG_DSN` set or `initdb` / `pg_ctl` present in `QT_PG_BIN`."""
    if os.environ.get("QT_PG_DSN"):
        return True
    pg_bin = Path(os.environ.get("QT_PG_BIN", PG_BIN_DEFAULT))
    return (pg_bin / "initdb").exists() and (pg_bin / "pg_ctl").exists()


def docker_available() -> bool:
    """True when `docker info` succeeds."""
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def hftbacktest_available() -> bool:
    """True when the separate hftbacktest venv of `make hbt-crosscheck` exists."""
    return (REPO_ROOT / ".scratch" / "hbt-venv" / "bin" / "python").exists()


GRAY_REFERENCE = REPO_ROOT / "tests" / "fixtures" / "reference" / "gray_cmprsk.json"

_CHECKS = {
    "requires_core": (core_available, "queuetruth._core is not built (run `make develop`)"),
    "requires_postgres": (postgres_available, "PostgreSQL binaries not found (set QT_PG_BIN or QT_PG_DSN)"),
    "requires_docker": (docker_available, "Docker daemon not running (`docker info` failed)"),
    "requires_hftbacktest": (hftbacktest_available, "hftbacktest venv missing (run `make hbt-crosscheck`)"),
    "requires_gray_reference": (GRAY_REFERENCE.exists, f"missing {GRAY_REFERENCE.relative_to(REPO_ROOT)}"),
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skips tests whose optional environment is unavailable, with the reason."""
    cache: dict[str, bool] = {}
    for item in items:
        for marker, (check, reason) in _CHECKS.items():
            if item.get_closest_marker(marker) is None:
                continue
            if marker not in cache:
                cache[marker] = bool(check())
            if not cache[marker]:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Repository root."""
    return REPO_ROOT
