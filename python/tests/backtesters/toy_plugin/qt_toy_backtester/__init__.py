"""A toy third-party backtester plugin for queuetruth's tests: it is registered only through the
`queuetruth.backtesters` entry point (the test writes its dist-info), with no change to queuetruth.

Its output is a CSV with one row per order: `toy_ref, toy_symbol, toy_side (B/S), toy_price, toy_qty, toy_tick,
toy_placed_ns, toy_done_ns, toy_filled (0/1)`. The importer turns each row into hftbacktest order snapshots and
reuses queuetruth's hftbacktest importer, as a thin plugin over an existing format would."""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path
from typing import Any

from queuetruth.backtesters import BacktesterEntry, Probe

COLUMNS = ("toy_ref", "toy_symbol", "toy_side", "toy_price", "toy_qty", "toy_tick", "toy_placed_ns", "toy_done_ns")


def detect(probe: Probe) -> bool:
    return set(COLUMNS) <= probe.columns


def import_toy(paths: list[str], options: Any) -> Any:
    from queuetruth.live.importers.sim import import_hftbacktest

    lines = []
    for p in paths:
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                tick = float(r["toy_tick"])
                pt = round(float(r["toy_price"]) / tick)
                qty = int(r["toy_qty"])
                base = {
                    "order_id": int(r["toy_ref"]),
                    "side": 1 if r["toy_side"] == "B" else -1,
                    "price_tick": pt,
                    "tick_size": tick,
                    "qty": qty,
                    "order_type": 0,
                    "time_in_force": 0,
                    "maker": 1,
                    "instrument": r["toy_symbol"],
                }
                placed, done = int(r["toy_placed_ns"]), int(r["toy_done_ns"])
                lines.append(
                    {
                        **base,
                        "leaves_qty": qty,
                        "exec_price_tick": 0,
                        "status": 1,
                        "local_timestamp": placed,
                        "exch_timestamp": placed + 1_000,
                    }
                )
                end = (
                    {"leaves_qty": 0, "exec_price_tick": pt, "status": 3}
                    if r["toy_filled"] == "1"
                    else {"leaves_qty": qty, "exec_price_tick": 0, "status": 4}
                )
                lines.append({**base, **end, "local_timestamp": done + 1_000, "exch_timestamp": done})
    tmp = Path(tempfile.mkdtemp(prefix="qt-toy-")) / "snapshots.jsonl"
    tmp.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return import_hftbacktest([str(tmp)], options)


BACKTESTER = BacktesterEntry(name="toy", label="Toy backtester", import_sim=import_toy, detect=detect)
