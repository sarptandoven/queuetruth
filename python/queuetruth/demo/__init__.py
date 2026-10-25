"""`queuetruth demo`: a believable real-data answer in about a minute (see `queuetruth.demo.run`).

Python API: `run_demo(DemoOptions(...))` returns a `DemoSummary`; `plan(DemoOptions(...))` says what would be
downloaded. Sources are listed in `queuetruth.demo.sources.SOURCES`.

The names below are loaded on first use (PEP 562): `queuetruth.cli` imports `queuetruth.demo._dispatch` before every
command, and that must not pull in the demo's modules (or numpy, pyarrow, ssl) for commands that are not `demo`.
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY = {
    "DemoOptions": "queuetruth.demo.run",
    "plan": "queuetruth.demo.run",
    "run_demo": "queuetruth.demo.run",
    "SOURCES": "queuetruth.demo.sources",
    "DemoWindow": "queuetruth.demo.sources",
    "Source": "queuetruth.demo.sources",
    "DemoPlan": "queuetruth.demo.summary",
    "DemoSources": "queuetruth.demo.summary",
    "DemoSummary": "queuetruth.demo.summary",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module 'queuetruth.demo' has no attribute {name!r}")
    value = getattr(importlib.import_module(mod), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])
