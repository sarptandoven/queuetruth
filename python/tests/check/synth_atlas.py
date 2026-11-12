"""A synthetic atlas data API with touch tables (invented numbers, no market data) for `check` without data."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = "cd" * 32
#: (tier, side) -> (P(real | touch) value, lo, hi), (P&L overstated ticks per order: value, lo, hi)
CELLS: dict[tuple[str, str], tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    ("T1_top100", "buy"): ((0.40, 0.35, 0.45), (0.50, 0.40, 0.60)),
    ("T1_top100", "sell"): ((0.50, 0.44, 0.56), (0.30, 0.20, 0.40)),
    ("ALL", "buy"): ((0.60, 0.55, 0.65), (0.20, 0.10, 0.30)),
    ("ALL", "sell"): ((0.60, 0.55, 0.65), (0.20, 0.10, 0.30)),
    ("T5_rest", "buy"): ((0.70, 0.60, 0.80), (-0.05, -0.20, 0.10)),
}


def _est(v: float | None, lo: float | None = None, hi: float | None = None) -> dict[str, Any]:
    return {"value": v, "ci_lo": lo, "ci_hi": hi}


def touch_doc(cells: dict[tuple[str, str], Any] | None = None, bars: tuple[str, ...] = ("1m", "5m")) -> dict:
    rows = []
    for (tier, side), (p, mk) in (cells or CELLS).items():
        for bar in bars:
            rows.append(
                {
                    "tier": tier, "side": side, "bar": bar, "bar_s": {"1m": 60, "5m": 300, "1h": 3600}[bar],
                    "rule": "touch", "orders": 4000, "dates": 3, "symbols": 50,
                    "real_fill_rate": _est(0.3, 0.28, 0.32), "rule_fill_rate": _est(0.5, 0.48, 0.52),
                    "p_real_given_rule": _est(*p), "p_rule_given_real": _est(0.95, 0.93, 0.97),
                    "real_markout_ticks": _est(-1.0, -1.2, -0.8), "rule_markout_ticks": _est(-0.4, -0.6, -0.2),
                    "pnl_overstated_ticks": _est(*mk), "flagged_share": 0.0,
                }
            )  # fmt: skip
    return {
        "schema": "qt.atlas.touch", "schema_version": 1, "atlas_root": ROOT, "source": "iex",
        "population": "standard", "days": ["2026-11-06", "2026-11-09", "2026-11-10"], "flag_policy": "exclude",
        "definition": "docs/concepts/touch-tables.md", "attribution": "Data provided for free by IEX.", "rows": rows,
    }  # fmt: skip


DAYS = ["2026-11-06", "2026-11-09", "2026-11-10"]


def write_atlas(
    root: Path,
    *,
    touch: dict | bool | None = True,
    symbols: dict[str, str] | None = None,
    day_roots: list[str] | None = None,
) -> Path:
    """A site directory with `atlas/v1/` (manifest, summary, a symbol shard per letter of `symbols` and, unless
    `touch` is False or None, `touch.json`) and `data/atlas_manifest.json` with the roots of the three `DAYS`
    (`day_roots`, default invented)."""
    from queuetruth.atlas.verify import merkle_root

    base = root / "atlas" / "v1"
    (base / "symbols").mkdir(parents=True, exist_ok=True)
    day_roots = day_roots or [hashlib.sha256(d.encode()).hexdigest() for d in ("d1", "d2", "d3")]
    atlas_root = merkle_root(day_roots)
    summary = {"schema": "qt.atlas_summary", "schema_version": 1, "venue": "iex", "atlas_root": atlas_root,
               "headline_horizon": "1min", "rows": []}  # fmt: skip
    files: dict[str, dict[str, Any]] = {"summary.json": summary}
    if touch is True:
        touch = touch_doc()
    if touch:
        files["touch.json"] = {**touch, "atlas_root": atlas_root}
    by_letter: dict[str, list[dict[str, Any]]] = {}
    for sym, tier in sorted((symbols or {}).items()):
        by_letter.setdefault(sym[0], []).append({"symbol": sym, "tier": tier, "days": 3, "cells": []})
    for letter, entries in by_letter.items():
        files[f"symbols/{letter}.json"] = {
            "schema": "qt.atlas_symbols", "schema_version": 1, "venue": "iex", "atlas_root": atlas_root,
            "letter": letter, "mode": "faithful", "shadow_population": "standard", "horizons": ["1s", "10s", "1min"],
            "settings": [], "symbols": entries,
        }  # fmt: skip
    entries = []
    for path, doc in sorted(files.items()):
        data = (json.dumps(doc) + "\n").encode()
        (base / path).write_bytes(data)
        entries.append({"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    days = DAYS
    manifest = {
        "schema": "qt.atlas_api_manifest", "schema_version": 1, "api_version": 1, "built_at": "2026-11-10",
        "atlas_root": atlas_root, "venue": "iex", "venue_name": "IEX DEEP+",
        "window": {"from": days[0], "to": days[-1]}, "days": days, "symbol_days": 120, "symbols": 41,
        "mode": "faithful", "shadow_population": "standard", "headline_horizon": "1min",
        "attribution": "Data provided for free by IEX.", "verify": "https://example.org", "files": entries,
    }  # fmt: skip
    (base / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    (root / "data").mkdir(parents=True, exist_ok=True)
    atlas_manifest = {
        "schema": "qt.scale.atlas_manifest", "schema_version": 1, "atlas_root": atlas_root,
        "rule": "Merkle root over the day roots in date order",
        "days": [{"date": d.replace("-", ""), "merkle_root": r, "venue": "iex", "buckets": 32,
                  "source_url": f"https://example.org/{d}.pcap.gz"} for d, r in zip(days, day_roots, strict=True)],
    }  # fmt: skip
    (root / "data" / "atlas_manifest.json").write_text(json.dumps(atlas_manifest, indent=1) + "\n")
    return root
