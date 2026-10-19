"""Test helpers: run an importer on a fixture scenario and compare with its golden canonical rows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from queuetruth.live import IMPORTERS, ImportOptions, build_log, load_instrument_map, table_rows

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def scenarios() -> list[Path]:
    """Every importer scenario directory (has `expected.json`)."""
    return sorted(p.parent for p in FIXTURES.glob("*/*/expected.json"))


def run_scenario(scen: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], Any]:
    """(expected doc, got orders, got events, ImportResult) for one scenario."""
    exp = json.loads((scen / "expected.json").read_text())
    spec = exp["import_spec"]
    imap = load_instrument_map(scen / spec["instrument_map"]) if spec.get("instrument_map") else None
    extra = {k: v for k, v in spec.items() if k not in ("venue", "account", "instrument_map", "inputs")}
    res = IMPORTERS[spec["venue"]](
        [str(scen / p) for p in spec["inputs"]], ImportOptions(account=spec.get("account"), imap=imap, extra=extra)
    )
    orders, events = build_log(res.orders, res.events)
    return exp, table_rows(orders), table_rows(events), res


def diff_rows(kind: str, got: list[dict[str, Any]], want: list[dict[str, Any]], ignore: set[str]) -> list[str]:
    """Human-readable differences between row lists (floats compared to 1e-12)."""
    out: list[str] = []
    if len(got) != len(want):
        out.append(f"{kind}: {len(got)} rows, expected {len(want)}")
    for g, w in zip(got, want, strict=False):
        for k, v in w.items():
            if k in ignore:
                continue
            gv = g.get(k)
            if isinstance(v, float) and isinstance(gv, float) and abs(gv - v) <= 1e-12:
                continue
            if gv != v:
                out.append(f"{kind} {w.get('order_key')}#{w.get('event_seq')} {k}: got {gv!r}, expected {v!r}")
    return out
