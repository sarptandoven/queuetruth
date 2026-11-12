"""Synthetic event-driven backtests of the synthetic live log in tests/fixtures/synth_small (no market data).

Each writer replays the live log's orders as a simulator on the live schedule would report them (hftbacktest order
snapshots, NautilusTrader orders and fills reports), keyed by the live order keys, with two deliberate
differences so the comparison has something to find: every third order live did not fill is filled by the
"backtest" 1 s after its ack, and every fifth order live filled is not filled by it.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

S = 1_000_000_000


@dataclass(frozen=True)
class SimOrder:
    key: str
    symbol: str
    side: int  # 1 buy, -1 sell
    px: str
    px_int: int
    qty: int
    ack: int
    end: int
    filled: bool
    fill_ts: int | None
    live_filled: bool


def _ns(t: object) -> int | None:
    return None if t is None else int(t.value)  # type: ignore[attr-defined]


def sim_orders(live: str | Path) -> list[SimOrder]:
    """The live log's orders as the synthetic backtest fills them."""
    orders = pq.read_table(Path(live) / "orders.parquet").to_pylist()
    events = pq.read_table(Path(live) / "events.parquet").to_pylist()
    by: dict[str, list[dict]] = {}
    for e in events:
        by.setdefault(e["order_key"], []).append(e)
    out = []
    for i, o in enumerate(sorted(orders, key=lambda r: r["order_key"])):
        ev = sorted(by[o["order_key"]], key=lambda e: e["event_seq"])
        ack = _ns(next(e for e in ev if e["event_type"] == "ack")["ts_exchange"])
        assert ack is not None
        end = max(_ns(e["ts_exchange"]) or 0 for e in ev)
        fills = [_ns(e["ts_exchange"]) for e in ev if e["event_type"] == "fill"]
        live_filled = bool(fills)
        if live_filled:
            filled, ts = i % 5 != 0, fills[-1]
        else:
            filled, ts = i % 3 == 0, ack + S
        end = max(end, (ts or 0) + 1)
        out.append(
            SimOrder(
                o["order_key"],
                o["instrument"],
                1 if o["side"] == "buy" else -1,
                o["px_str"],
                o["limit_px"],
                int(o["orig_qty"]),
                ack,
                end,
                filled,
                ts if filled else None,
                live_filled,
            )
        )
    return out


def write_nautilus(d: Path, os_: list[SimOrder]) -> list[Path]:
    """NautilusTrader `generate_orders_report` and `generate_fills_report` rows (CSV); `client_order_id` is the
    live order key."""
    d.mkdir(parents=True, exist_ok=True)
    po, pf = d / "orders_report.csv", d / "fills_report.csv"
    with po.open("w", newline="") as fo, pf.open("w", newline="") as ff:
        wo, wf = csv.writer(fo), csv.writer(ff)
        wo.writerow(["client_order_id", "venue_order_id", "instrument_id", "side", "type", "quantity", "price",
                     "time_in_force", "post_only", "reduce_only", "status", "filled_qty", "avg_px", "ts_init",
                     "ts_last"])  # fmt: skip
        wf.writerow(["client_order_id", "trade_id", "order_side", "last_qty", "last_px", "liquidity_side",
                     "commission", "ts_event", "instrument_id"])  # fmt: skip
        for n, o in enumerate(os_):
            side = "BUY" if o.side == 1 else "SELL"
            inst = o.symbol + ".SIM"
            status, fq, avg = ("FILLED", o.qty, o.px) if o.filled else ("CANCELED", 0, "")
            wo.writerow([o.key, f"V{n}", inst, side, "LIMIT", o.qty, o.px, "GTC", False, False, status, fq, avg,
                         o.ack, o.end])  # fmt: skip
            if o.filled:
                wf.writerow([o.key, f"T{n}", side, o.qty, o.px, "MAKER", "", o.fill_ts, inst])
    return [po, pf]


def write_hftbacktest(path: Path, os_: list[SimOrder], tick: float = 0.25) -> Path:
    """hftbacktest order snapshots (JSON lines) with the live order key in the `order_key` column."""
    lines = []
    for n, o in enumerate(os_):
        pt = round(float(o.px) / tick)
        base = {"order_id": n + 1, "order_key": o.key, "instrument": o.symbol, "side": o.side, "price_tick": pt,
                "tick_size": tick, "qty": o.qty, "order_type": 0, "time_in_force": 0, "maker": 1}  # fmt: skip
        lines.append({**base, "leaves_qty": o.qty, "exec_price_tick": 0, "status": 1, "local_timestamp": o.ack,
                      "exch_timestamp": o.ack})  # fmt: skip
        if o.filled:
            end = {"leaves_qty": 0, "exec_price_tick": pt, "status": 3, "exch_timestamp": o.fill_ts}
        else:
            end = {"leaves_qty": o.qty, "exec_price_tick": 0, "status": 4, "exch_timestamp": o.end}
        lines.append({**base, **end, "local_timestamp": end["exch_timestamp"]})
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path
