#!/usr/bin/env python3
"""Minimal reproduction: shadows are filled during a trading halt, which the v0.1 contract censors (finding F2).

    uv run --frozen --no-sync python scripts/scale/findings/repro_halt_shadow_fill.py [--keep DIR]

Exit 0 when the engine follows the contract, 1 while it fills shadows during the halt (the current behaviour), 2 on
a setup error. See `README.md` in this directory (finding F2) for the 2020-01-30 evidence.

The contract: `docs/design/spec.md` 1.2 (non-goals for v0.1) says "No auction modelling: shadows resting through an
auction or halt are censored (`end_state = censored_gap`)", and `docs/concepts/exact-truth.md` ("Where exactness
stops") and `docs/venue-semantics.md` ("Out of scope") say the same to users.

The ITCH 5.0 day (one symbol `HLTX`, locate 1, 2020-01-02; prices in dollars, times New York):

| # | time | message | displayed book after it |
|---|---|---|---|
| 1 | 04:00:00 | `S` O, `R` HLTX, `H` T (trading) | empty |
| 2 | 09:30:00 | `S` Q (market open) | |
| 3 | 10:00:00.000 | `A` ref 1, buy 100 @ 9.98 | 9.98 / - |
| 4 | 10:00:00.001 | `A` ref 2, sell 100 @ 10.02 | 9.98 / 10.02 |
| 5 | 10:01:00 | `H` H, reason T1 (halted: no matching until a halt cross) | 9.98 / 10.02 |
| 6 | 10:02:00 | `A` ref 3, sell 100 @ 9.99 (displayed, not matched: halted) | 9.98 / 9.99 |
| 7 | 10:03:00 | `D` ref 3 (cancelled during the halt) | 9.98 / 10.02 |
| 8 | 10:05:00 | `H` T (resumed; nothing crossed, so no halt cross) | 9.98 / 10.02 |
| 9 | 10:10:00 | `D` ref 1, `D` ref 2 | empty |

Shadows (100 shares, latency 0, cancelled at 10:06:00):
- `S1` buy @ 10.00 at 10:00:10 (trading: the best bid, not marketable). It rests into the halt at 10:01:00.
  **Expected (spec 1.2): no fill, `end_state = censored_gap` at the halt.** Under Nasdaq rules nothing could
  execute either: the ask at 9.99 arrives during the halt and is cancelled during it.
- `S2` buy @ 10.03 at 10:02:30 (halted). It arrives on a book whose asks (9.99, 10.02) are at or below its price,
  but the venue is halted. **Expected: never live, no fill, `end_state = censored_gap`** (a shadow cannot rest
  through a halt it arrives in). Taking part in the halt cross at the resumption is not part of the v0.1 contract;
  it is a possible v0.2 extension.

Actual (queuetruth 0.1.1, `_core` sha256 004ecd3b...): `S1` filled 100 @ 10.00 at 10:02:00 (reason `crossed`,
maker) and `S2` filled 100 @ 9.99 at 10:02:30 (reason `crossed_at_live`, taker against the halted 9.99 ask), both
`end_state = filled`. The exact fill rule assumes continuous matching. The book replay itself is exact (no invariant
flag: the displayed book never crosses here), so the replay checks cannot see this; what is wrong is the shadows'
fill truth while the symbol is not trading. The ITCH mapper drops `H` messages (`crates/ingest/src/itch/mapper.rs`,
`Body::TradingAction => {}`), so the engine cannot know.

The report prints prices in dollars and times as seconds since midnight (New York) in every section.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import struct
import sys
import tempfile
from pathlib import Path

NS = 1_000_000_000
H = 3600 * NS
M = 60 * NS
MIDNIGHT_UTC = 1_577_941_200 * NS  # 2020-01-02 00:00 America/New_York (EST, UTC-5)
SYM = "HLTX"


def _hdr(t: str, loc: int, ts: int) -> bytes:
    return t.encode() + struct.pack(">HH", loc, 0) + ts.to_bytes(6, "big")


def _frame(m: bytes) -> bytes:
    return struct.pack(">H", len(m)) + m


def itch_day() -> bytes:
    stock = SYM.encode().ljust(8)
    directory = stock + b"QN" + struct.pack(">I", 100) + b"NCZ PNN1N" + struct.pack(">I", 0) + b"N"

    def add(ts: int, ref: int, side: bytes, shares: int, px: int) -> bytes:
        return _frame(
            _hdr("A", 1, ts) + struct.pack(">Q", ref) + side + struct.pack(">I", shares) + stock + struct.pack(">I", px)
        )

    def delete(ts: int, ref: int) -> bytes:
        return _frame(_hdr("D", 1, ts) + struct.pack(">Q", ref))

    def action(ts: int, state: bytes, reason: bytes) -> bytes:
        return _frame(_hdr("H", 1, ts) + stock + state + b" " + reason.ljust(4))

    msgs = [
        _frame(_hdr("S", 0, 4 * H) + b"O"),
        _frame(_hdr("R", 1, 4 * H + 1) + directory),
        action(4 * H + 2, b"T", b""),
        _frame(_hdr("S", 0, 9 * H + 30 * M) + b"Q"),
        add(10 * H, 1, b"B", 100, 99_800),
        add(10 * H + 1_000_000, 2, b"S", 100, 100_200),
        action(10 * H + 1 * M, b"H", b"T1"),
        add(10 * H + 2 * M, 3, b"S", 100, 99_900),
        delete(10 * H + 3 * M, 3),
        action(10 * H + 5 * M, b"T", b""),
        delete(10 * H + 10 * M, 1),
        delete(10 * H + 10 * M + 1, 2),
        _frame(_hdr("S", 0, 16 * H) + b"M"),
        _frame(_hdr("S", 0, 20 * H) + b"E"),
        _frame(_hdr("S", 0, 20 * H + 5 * M) + b"C"),
    ]
    return b"".join(msgs)


def shadows_csv() -> str:
    cancel = MIDNIGHT_UTC + 10 * H + 6 * M
    rows = [  # prices at price_exp -9
        ("S1", 10_000_000_000, MIDNIGHT_UTC + 10 * H + 10 * NS),
        ("S2", 10_030_000_000, MIDNIGHT_UTC + 10 * H + 2 * M + 30 * NS),
    ]
    head = "shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns,ts_cancel\n"
    return head + "".join(f"{sid},1,B,{p},100,{ts},0,{cancel}\n" for sid, p, ts in rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", type=Path, help="write the inputs and outputs here instead of a temporary directory")
    a = ap.parse_args(argv)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        import queuetruth as qt
    except ImportError as e:
        print(f"QT-USAGE: needs the queuetruth package with its compiled core ({e})", file=sys.stderr)
        return 2
    wd = a.keep or Path(tempfile.mkdtemp(prefix="qt-repro-halt-"))
    wd.mkdir(parents=True, exist_ok=True)
    try:
        src = wd / "01022020.NASDAQ_ITCH50.gz"
        src.write_bytes(gzip.compress(itch_day(), mtime=0))
        (wd / "shadows.csv").write_text(shadows_csv())
        ds = wd / "ds"
        shutil.rmtree(ds, ignore_errors=True)
        qt.ingest(
            {
                "format": "itch",
                "inputs": [{"path": str(src)}],
                "out": str(ds),
                "symbols": [SYM],
                "venue": "nasdaq",
                "price_exp": -9,
                "tick_size": "0.01",
                "lot_size": "1",
            }
        )
        out = wd / "replay"
        shutil.rmtree(out, ignore_errors=True)
        rs = qt.replay({"dataset": {"path": str(ds)}, "out": str(out), "shadows": {"path": str(wd / "shadows.csv")}})
        inv = rs.model_dump(by_alias=True)["invariants"]
        fills = pq.read_table(out / "shadow_fills.parquet")
        fills = fills.select(["shadow_id", "ts_event", "fill_qty", "fill_px", "reason", "liquidity"])
        fills = fills.set_column(1, "ts_event", fills.column("ts_event").cast(pa.int64()))
        res = pq.read_table(out / "shadow_results.parquet", columns=["shadow_id", "filled_qty", "end_state"])
        actual_fills = [
            {
                "shadow_id": r["shadow_id"],
                "time_s": (r["ts_event"] - MIDNIGHT_UTC) / NS,
                "qty": r["fill_qty"],
                "price": r["fill_px"] / 1e9,
                "reason": r["reason"],
                "liquidity": r["liquidity"],
            }
            for r in fills.to_pylist()
        ]
        halt, resume = (10 * H + 1 * M) / NS, (10 * H + 5 * M) / NS
        # the contract (spec 1.2): no fill in the halt, both shadows end censored_gap
        bad = [{"violation": "fill while halted", **f} for f in actual_fills if halt <= f["time_s"] < resume]
        bad += [{"violation": "filled at all", **f} for f in actual_fills if f["shadow_id"] == "S1"]
        results = {r["shadow_id"]: r for r in res.to_pylist()}
        for sid in ("S1", "S2"):
            got = results.get(sid, {})
            if got.get("end_state") != "censored_gap" or got.get("filled_qty"):
                bad.append(
                    {"violation": "end_state", "shadow_id": sid, **{k: got.get(k) for k in ("end_state", "filled_qty")}}
                )
        report = {
            "units": "prices in dollars; times in seconds since midnight New York (halt 36060, resumption 36300)",
            "book_invariants": {k: v for k, v in inv.items() if v},
            "expected": {
                "S1": "no fill; end_state censored_gap at the halt (spec 1.2)",
                "S2": "never live, no fill; end_state censored_gap (spec 1.2)",
            },
            "actual_fills": actual_fills,
            "actual_end_states": {
                k: {"end_state": v["end_state"], "filled_qty": v["filled_qty"]} for k, v in results.items()
            },
            "violations": bad,
        }
        print(json.dumps(report, indent=1, default=str))
        return 1 if bad else 0
    finally:
        if a.keep is None:
            shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
