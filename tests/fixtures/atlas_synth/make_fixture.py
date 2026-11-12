"""Writes `atlas/v1/` of this directory: a synthetic Fill-Realism Atlas data API with invented numbers (no market
data), for the docs blocks and tests of the one-line framework settings (`qt.atlas.calibration`,
`qt.backtrader.QueueFiller(tier=...)`, `qt.lean.QueueFillModel(tier=...)`). Deterministic; run from anywhere:

    uv run --frozen --no-sync python tests/fixtures/atlas_synth/make_fixture.py

Files (each validated by its `queuetruth.atlas` model, listed with its sha256 in `manifest.json`):

- `configs.json`: the closest setting per library and tier. NautilusTrader: touch fill `p = 1` for `ALL`, queue
  position with `p = 0.5` for `T1_top100`. hftbacktest: `power_prob_queue_model3(3)` for `ALL`,
  `log_prob_queue_model2()` for `T1_top100`, and for `T5_rest` a corrected-mode model no released setting reproduces.
- `symbols/Z.json`: one symbol, `ZZZA`, in tier `T1_top100`.
- `touch.json`: touch tables for tiers `ALL` and `T1_top100`, both sides and 1m, 5m and 1h bars.

The venue name, window and attribution are invented too; nothing here describes a real market.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = "cd" * 32
#: (library, tier, model id, bias pp, lo, hi, config text)
CONFIGS = (
    ("nautilus", "ALL", "nt_prob_touch[mode=faithful;p=1]", 0.1, -0.2, 0.4,
     "fill_model=FillModel(prob_fill_on_limit=1), book_type=BookType.L2_MBP, trade_execution=True, "
     "queue_position=False"),
    ("nautilus", "T1_top100", "nt_queue_l2_prob_touch[mode=faithful;p=0.5]", -0.6, -0.9, -0.3,
     "fill_model=FillModel(prob_fill_on_limit=0.5), book_type=BookType.L2_MBP, trade_execution=True, "
     "queue_position=True"),
    ("hftbacktest", "ALL", "hbt_power3[exch=partial;mode=faithful;n=3]", 0.2, -0.1, 0.5,
     ".power_prob_queue_model3(3).no_partial_fill_exchange()"),
    ("hftbacktest", "T1_top100", "hbt_log2[exch=partial;mode=faithful]", -0.7, -1.0, -0.4,
     ".log_prob_queue_model2().no_partial_fill_exchange()"),
    ("hftbacktest", "T5_rest", "hbt_power3[exch=partial;mode=corrected;n=2]", 0.0, -0.3, 0.3, None),
)  # fmt: skip
#: P(real | touch) per (tier, side, bar)
TOUCH = {("ALL", "1m"): 0.45, ("ALL", "5m"): 0.55, ("ALL", "1h"): 0.7, ("T1_top100", "1m"): 0.35,
         ("T1_top100", "5m"): 0.5, ("T1_top100", "1h"): 0.65}  # fmt: skip


def _iv(v: float, spread: float = 0.05) -> dict[str, float]:
    return {"value": v, "ci_lo": round(v - spread, 4), "ci_hi": round(v + spread, 4)}


def documents() -> dict[str, dict[str, Any]]:
    from queuetruth.atlas import _common as C

    configs = []
    for lib, tier, mid, v, lo, hi, text in CONFIGS:
        iv = {"value": v, "lo": lo, "hi": hi}
        key = C.verdict_key(iv)
        configs.append({
            "library": lib, "library_name": C.LIBRARY_NAMES[lib], "tier": tier, "tier_name": C.TIER_WORDS[tier],
            "label": C.label(mid), "model_id": mid, "n_settings": 3, "fill_rate_bias_pp": iv, "verdict": key,
            "verdict_text": C.VERDICT_TEXT[key], "overlapping": [], "overlapping_labels": [], "pnl_bias_ticks": None,
            "config": text, "config_kind": None if text is None else (
                "NautilusTrader BacktestEngine.add_venue keywords" if lib == "nautilus"
                else "hftbacktest BacktestAsset chain"),
        })  # fmt: skip
    bars = {"1m": 60, "5m": 300, "1h": 3600}
    rows = []
    for (tier, bar), p in TOUCH.items():
        for side, d in (("buy", 0.0), ("sell", 0.02), ("all", 0.01)):
            rows.append({
                "tier": tier, "side": side, "bar": bar, "bar_s": bars[bar], "rule": "touch", "orders": 4000,
                "dates": 3, "symbols": 40, "real_fill_rate": _iv(0.3), "rule_fill_rate": _iv(0.6),
                "p_real_given_rule": _iv(round(p + d, 4)), "p_rule_given_real": _iv(0.9),
                "real_markout_ticks": _iv(-1.0, 0.2), "rule_markout_ticks": _iv(0.5, 0.2),
                "pnl_overstated_ticks": _iv(0.4, 0.1), "flagged_share": 0.0,
            })  # fmt: skip
    head = {"venue": "synthetic", "atlas_root": ROOT}
    return {
        "configs.json": {"schema": "qt.atlas_configs", "schema_version": 1, **head, "mode": "faithful",
                         "shadow_population": "standard", "horizon": "1min", "notes": {}, "configs": configs},
        "symbols/Z.json": {"schema": "qt.atlas_symbols", "schema_version": 1, **head, "letter": "Z",
                           "mode": "faithful", "shadow_population": "standard", "horizons": ["1min"],
                           "columns": ["bias_pp", "lo_pp", "hi_pp", "shrunk_pp", "n", "n_days"], "settings": [],
                           "symbols": [{"symbol": "ZZZA", "tier": "T1_top100", "days": 3, "cells": []}]},
        "touch.json": {"schema": "qt.atlas.touch", "schema_version": 1, "atlas_root": ROOT, "source": "synthetic",
                       "population": "standard", "days": ["2026-11-06", "2026-11-09", "2026-11-10"],
                       "flag_policy": "exclude", "definition": "docs/concepts/touch-tables.md",
                       "attribution": "Synthetic numbers for tests and docs.", "rows": rows},
    }  # fmt: skip


def main() -> None:
    from queuetruth.atlas._registry import doc_models

    base = HERE / "atlas" / "v1"
    (base / "symbols").mkdir(parents=True, exist_ok=True)
    models = doc_models()
    entries = []
    for path, doc in documents().items():
        models[doc["schema"]].model_validate(doc)
        data = (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode()
        (base / path).write_bytes(data)
        entries.append({"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {
        "schema": "qt.atlas_api_manifest", "schema_version": 1, "api_version": 1, "built_at": "2026-11-10",
        "atlas_root": ROOT, "venue": "synthetic", "venue_name": "Synthetic venue",
        "window": {"from": "2026-11-06", "to": "2026-11-10"}, "days": ["2026-11-06", "2026-11-09", "2026-11-10"],
        "symbol_days": 3, "symbols": 1, "mode": "faithful", "shadow_population": "standard",
        "headline_horizon": "1min", "attribution": "Synthetic numbers for tests and docs.",
        "verify": "https://example.org", "files": entries,
    }  # fmt: skip
    models[manifest["schema"]].model_validate(manifest)
    (base / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(f"wrote {base}")


if __name__ == "__main__":
    main()
