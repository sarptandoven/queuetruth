"""End-to-end test setup: skips for optional environments and shared fixtures (helpers in e2e_support.py)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # makes e2e_support importable (unique name)

import pytest
from e2e_support import REPO, SYNTH, core_available, postgres_available

_CHECKS = {
    "requires_core": (core_available, "queuetruth._core is not built (run `make develop`)"),
    "requires_postgres": (postgres_available, "PostgreSQL binaries not found (set QT_PG_BIN or QT_PG_DSN)"),
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skips tests whose optional environment is unavailable, with the reason."""
    cache: dict[str, bool] = {}
    for item in items:
        if Path(str(item.fspath)).resolve().parent != Path(__file__).resolve().parent:
            continue
        for name, (fn, reason) in _CHECKS.items():
            if item.get_closest_marker(name):
                if name not in cache:
                    cache[name] = fn()
                if not cache[name]:
                    item.add_marker(pytest.mark.skip(reason=f"{name}: {reason}"))


@pytest.fixture(scope="session")
def synth() -> Path:
    if not (SYNTH / "manifest.json").exists():
        pytest.skip("tests/fixtures/synth_small is missing (make fixtures)")
    return SYNTH


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO
