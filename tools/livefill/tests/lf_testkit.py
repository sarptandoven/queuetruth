"""Shared helpers of the livefill tests (imported by name; `conftest` puts this directory on sys.path)."""

from __future__ import annotations

import importlib.util

import pytest


def core_available() -> bool:
    """True when the compiled extension `queuetruth._core` is importable."""
    try:
        return importlib.util.find_spec("queuetruth._core") is not None
    except (ImportError, ValueError):
        return False


needs_core = pytest.mark.skipif(not core_available(), reason="needs the compiled queuetruth._core extension")
