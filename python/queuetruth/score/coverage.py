"""Detection of fills-only live logs (imports from fill lists omit never-filled orders).

A log is fills-only when any of: `manifest.json` has `fills_only: true` or `coverage: "fills_only"` (or
`coverage: {"fills_only": true}`); a `coverage.json` sidecar says the same; an orders column `fills_only` /
`coverage` marks any order; or no order has any lifecycle event besides fill / fill_bust / fill_correct.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from queuetruth.score._io import Row, read_manifest

FILL_TYPES = frozenset({"fill", "fill_bust", "fill_correct"})


def _flag(doc: Any) -> bool:
    if not isinstance(doc, dict):
        return False
    if doc.get("fills_only") is True:
        return True
    cov = doc.get("coverage")
    if cov == "fills_only":
        return True
    return isinstance(cov, dict) and cov.get("fills_only") is True


def is_fills_only(live_dir: str | Path, orders: list[Row], events: list[Row]) -> bool:
    """True when the log cannot contain never-filled orders."""
    if _flag(read_manifest(live_dir)):
        return True
    side = Path(live_dir) / "coverage.json"
    if side.exists():
        try:
            if _flag(json.loads(side.read_text(encoding="utf-8"))):
                return True
        except (OSError, json.JSONDecodeError):
            pass
    for o in orders:
        if o.get("fills_only") is True or o.get("coverage") == "fills_only":
            return True
    return bool(events) and all(str(e["event_type"]) in FILL_TYPES for e in events)
