"""Puts `scripts/scale` (the W1 pipeline scripts) and this directory on `sys.path` for the flag-tracing tests, and
skips `requires_core` tests when `queuetruth._core` is not built (as `python/tests/conftest.py` does).

These tests are outside the pytest `testpaths` (python/tests, service/tests, tests/e2e): run them with
`pytest scripts/scale/tests` until they move to `python/tests/scale/` (whose conftest already puts `scripts/scale` on
the path; this file is then not needed).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

for _p in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _core_available() -> bool:
    try:
        return importlib.util.find_spec("queuetruth._core") is not None
    except (ImportError, ValueError):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _core_available():
        return
    skip = pytest.mark.skip(reason="queuetruth._core is not built (run `make develop`)")
    for item in items:
        if item.get_closest_marker("requires_core") is not None:
            item.add_marker(skip)
