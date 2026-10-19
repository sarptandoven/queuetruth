"""Exact replay with shadow orders on the synthetic dataset.

Shows: `qt.sample_shadows` (a seeded schedule of hypothetical resting orders), `qt.replay` with those shadows
(the replay summary: record counters, the 8 book invariants, the shadow outcome block, the data quality verdict),
reading `qt.shadow_results` / `qt.shadow_fills` with `qt.read_table` (pyarrow), and the interactive `qt.Session`
(`advance_to`, `bbo`, `depth`, `queue_ahead`) on one instrument.

Needs the compiled extension (`make develop` or the wheel). Synthetic data only: the numbers mean nothing about
real venues.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq
from _data import NS, dataset_path, out_dir, print_rows, sample_example_shadows, table_by_schema

import queuetruth as qt


def show_replay(dataset: Path, out: Path, shadows: Path) -> None:
    """Runs the exact replay with shadows and prints its summary and a few output rows."""
    r = qt.replay({"dataset": {"path": str(dataset)}, "out": str(out / "replay"), "shadows": {"path": str(shadows)}})
    print(f"\nreplay run_id {r.run_id} (engine {r.engine_version})")
    st = r.stats
    print(
        f"  records {st.records}, events {st.events}, trades {st.trades}, instruments {st.instruments}, "
        f"partitions {st.partitions}, snapshot records {st.snapshot_records}"
    )
    inv = r.invariants.model_dump()
    bad = {k: v for k, v in inv.items() if v}
    print(f"  invariants: {len(inv)} counters, {'all zero' if not bad else f'non-zero: {bad}'}")
    if r.shadows is not None:
        sh = r.shadows
        print(
            f"  shadows: n {sh.n}, live {sh.n_live}, filled {sh.n_filled}, partial only {sh.n_partial_only}, "
            f"cancelled {sh.n_cancelled}, censored {sh.n_censored}, rejected {sh.n_rejected}; "
            f"fill rows {sh.fills_rows}, trajectory rows {sh.trajectory_rows}"
        )
    dq = r.data_quality
    print(f"  data quality verdict: {dq.verdict} {dq.reasons or ''}".rstrip())
    for w in r.warnings:
        print(f"  warning: {w}")
    print("  files: " + ", ".join(f"{f.name} ({f.rows} rows)" if f.rows is not None else f.name for f in r.files))

    replay_out = out / "replay"
    results = qt.read_table(str(table_by_schema(replay_out, r.files, "qt.shadow_results")), expect="qt.shadow_results")
    print("\nqt.shadow_results (first 5 rows)")
    print_rows(
        results,
        ["shadow_id", "instrument_id", "price", "qty", "ts_live", "queue_ahead_at_live", "filled_qty", "end_state"],
    )
    ends = pc.value_counts(pc.cast(results.column("end_state"), "string")).to_pylist()
    print("  end_state counts: " + ", ".join(f"{e['values']} {e['counts']}" for e in ends))

    fills = qt.read_table(str(table_by_schema(replay_out, r.files, "qt.shadow_fills")), expect="qt.shadow_fills")
    print("\nqt.shadow_fills (first 5 rows)")
    print_rows(fills, ["shadow_id", "fill_seq", "ts_event", "fill_qty", "leaves_qty", "reason", "queue_ahead_before"])


def show_session(dataset: Path) -> None:
    """Steps a `qt.Session` through instrument 1 of the first session date and prints the top of book."""
    inst = pq.read_table(dataset / "instruments.parquet").to_pylist()
    first = min(inst, key=lambda r: (r["session_date"], r["instrument_id"]))
    iid, exp = int(first["instrument_id"]), int(first["price_exp"])
    open_ns = int(first["midnight_utc_ns"]) + int(first["open_s"]) * NS
    s = qt.Session(str(dataset))
    print(f"\nqt.Session on instrument {iid} ({first['symbol']}, {first['session_date']}); prices x 10^{exp}")
    for offset_s in (60, 600, 3600):
        s.advance_to(open_ns + offset_s * NS)
        bbo = s.bbo(iid)
        if not bbo or bbo["bid"] is None or bbo["ask"] is None:
            print(f"  open + {offset_s}s: one side empty: {bbo}")
            continue
        (bpx, bqty, bcnt), (apx, aqty, acnt) = bbo["bid"], bbo["ask"]
        print(
            f"  open + {offset_s}s: bid {bpx * 10.0**exp:.2f} x {bqty} ({bcnt} orders), "
            f"ask {apx * 10.0**exp:.2f} x {aqty} ({acnt} orders)"
        )
        bids, asks = s.depth(iid, 3)
        levels = {side: [(p * 10.0**exp, q) for p, q, _ in book] for side, book in (("bids", bids), ("asks", asks))}
        print(f"    depth 3 (price, qty): bids {levels['bids']}, asks {levels['asks']}")
        print(
            f"    queue_ahead (displayed qty, orders) at best bid {s.queue_ahead(iid, 'B', bpx)}, "
            f"at best ask {s.queue_ahead(iid, 'A', apx)}"
        )


def main() -> None:
    """Samples shadows, replays them exactly and steps an interactive session."""
    dataset = dataset_path()
    out = out_dir(__file__)
    print(f"queuetruth {qt.__version__}; dataset {dataset}; outputs {out}")
    shadows = sample_example_shadows(dataset, out)
    show_replay(dataset, out, shadows)
    show_session(dataset)


if __name__ == "__main__":
    main()
