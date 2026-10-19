"""Write a synthetic canonical live log (SPEC 3.8) and a perturbed "sim" copy for the scorer benchmark.

live = queuetruth.live.synthetic_log(seed, n_orders); sim = same orders with ~10% of fills dropped and fill
timestamps shifted by +1 ms, so the scorer sees both arms' discordance, TTF differences and markouts (no dataset).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from queuetruth import live  # API[py] queuetruth.live


def write_live(orders: pa.Table, events: pa.Table, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(orders, out / "orders.parquet", compression="zstd")
    pq.write_table(events, out / "events.parquet", compression="zstd")
    manifest = {
        "schema": "qt.live_manifest",
        "schema_version": 1,
        "venues": sorted(set(orders.column("venue").to_pylist())),
        "accounts": sorted(set(orders.column("account").to_pylist())),
        "n_orders": orders.num_rows,
        "n_events": events.num_rows,
        "instrument_map_sha256": None,
        "created_by": "benches/make_live_pair.py",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")


def renumber_event_seq(events: pa.Table) -> pa.Table:
    """Repair per-order bookkeeping after fill events were dropped: event_seq contiguous from 1, cum_qty equal to
    the running sum of the remaining fills, and leaves_qty shifted by the same amount (order of events kept)."""
    rows = events.to_pylist()
    by_order: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        by_order.setdefault(r["order_key"], []).append(i)
    for idx in by_order.values():
        idx.sort(key=lambda i: rows[i]["event_seq"])
        cum = 0
        for j, i in enumerate(idx, start=1):
            r = rows[i]
            r["event_seq"] = j
            if r["event_type"] == "fill" and r["qty"] is not None:
                cum += r["qty"]
            if r["cum_qty"] is not None:
                if r["leaves_qty"] is not None:
                    r["leaves_qty"] = r["leaves_qty"] + (r["cum_qty"] - cum)
                r["cum_qty"] = cum
    return pa.Table.from_pylist(rows, schema=events.schema)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--live", type=Path, required=True)
    ap.add_argument("--sim", type=Path, required=True)
    a = ap.parse_args()
    orders, events = live.synthetic_log(a.seed, a.n)  # API[py] -> (qt.live_orders, qt.live_events)
    write_live(orders, events, a.live)
    # Perturb: drop every 10th fill event; the sim arm then goes through live.build_log, which re-derives
    # consistent cum_qty / leaves_qty / event_seq.
    is_fill = pc.equal(events.column("event_type").cast(pa.string()), "fill").to_pylist()
    keep = [not (f and i % 10 == 0) for i, f in enumerate(is_fill)]
    sim_events = events.filter(pa.array(keep))
    shift = pa.scalar(1_000_000, pa.duration("ns"))
    if "ts_exchange" in sim_events.column_names:
        col = sim_events.column("ts_exchange")
        sim_events = sim_events.set_column(
            sim_events.column_names.index("ts_exchange"), "ts_exchange", pc.add(col, shift)
        )
    # API[py] live.build_log(orders: list[dict], events: list[dict]) -> (orders, events), re-derives bookkeeping
    o2, e2 = live.build_log(orders.to_pylist(), sim_events.to_pylist())
    e2 = renumber_event_seq(e2)
    write_live(o2, e2, a.sim)
    warnings = live.validate(str(a.sim))  # API[py] raises QtError on errors
    print(json.dumps({"live_orders": orders.num_rows, "sim_events": e2.num_rows, "sim_warnings": len(warnings)}))


if __name__ == "__main__":
    main()
