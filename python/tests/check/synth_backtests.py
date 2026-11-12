"""Synthetic backtest outputs over tests/fixtures/synth_small for `queuetruth check` (no market data).

Each writer places the same seeded set of resting limit orders at the best bid or ask of the synthetic tape and
says, like a touch-fill backtest, that every other order filled 5 s after it was placed; the rest are cancelled
after 30 s. The files follow the formats the importers read (`queuetruth.live.importers.sim`, `backtrader`,
`vectorbt`).
"""

from __future__ import annotations

import datetime as dt
import json
import random
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

TICK = 250_000_000  # 0.25 at price_exp -9
PX = 1e-9
FILL_AFTER = 5_000_000_000
CANCEL_AFTER = 30_000_000_000
SYMBOLS = {1: "QTAU6", 2: "QTBU6"}


@dataclass(frozen=True)
class Order:
    oid: int
    instrument_id: int
    side: int  # 1 buy, -1 sell
    px: int  # price_exp -9
    qty: int
    ts: int  # placement, epoch ns
    filled: bool

    @property
    def symbol(self) -> str:
        return SYMBOLS[self.instrument_id]

    @property
    def price(self) -> float:
        return self.px * PX

    @property
    def ts_end(self) -> int:
        return self.ts + (FILL_AFTER if self.filled else CANCEL_AFTER)


def orders(dataset: str | Path, n: int = 40, seed: int = 3, instruments: tuple[int, ...] = (1, 2)) -> list[Order]:
    """`n` seeded orders at the touch of the synthetic tape, at whole seconds (bar-friendly times)."""
    t = pq.read_table(
        Path(dataset) / "truth" / "bbo.parquet", columns=["ts_event", "instrument_id", "bid_px", "ask_px"]
    )
    rows = [r for r in t.to_pylist() if r["bid_px"] and r["ask_px"] and r["instrument_id"] in instruments]
    rng = random.Random(seed)
    out = []
    for i in range(n):
        r = rows[rng.randrange(len(rows) // 20, len(rows) - len(rows) // 20)]
        ts = (int(r["ts_event"].value) // 1_000_000_000 + 1) * 1_000_000_000
        side = rng.choice([1, -1])
        px = int(r["bid_px"] if side == 1 else r["ask_px"])
        out.append(Order(i + 1, int(r["instrument_id"]), side, px, 1 + i % 3, ts, filled=i % 2 == 0))
    return out


def _iso(ns: int) -> str:
    return dt.datetime.fromtimestamp(ns / 1e9, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")


def write_hftbacktest(path: Path, os_: list[Order], *, named: bool = True) -> Path:
    """hftbacktest order snapshots (JSON lines): new at placement, then filled or canceled."""
    lines = []
    for o in os_:
        pt = o.px // TICK
        base = {
            "order_id": o.oid,
            "side": o.side,
            "price_tick": pt,
            "tick_size": 0.25,
            "qty": o.qty,
            "order_type": 0,
            "time_in_force": 0,
            "maker": 1,
        }
        if named:
            base["instrument"] = o.symbol
        lines.append(
            {
                **base,
                "leaves_qty": o.qty,
                "exec_price_tick": 0,
                "status": 1,
                "local_timestamp": o.ts,
                "exch_timestamp": o.ts + 1_000,
            }
        )
        if o.filled:
            end = {"leaves_qty": 0, "exec_price_tick": pt, "status": 3}
        else:
            end = {"leaves_qty": o.qty, "exec_price_tick": 0, "status": 4}
        lines.append({**base, **end, "local_timestamp": o.ts_end + 1_000, "exch_timestamp": o.ts_end})
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


def write_nautilus(path: Path, os_: list[Order]) -> Path:
    """NautilusTrader `generate_orders_report` rows (CSV)."""
    head = "client_order_id,venue_order_id,instrument_id,side,type,quantity,price,time_in_force,post_only,"
    head += "reduce_only,status,filled_qty,avg_px,ts_init,ts_last\n"
    rows = []
    for o in os_:
        side = "BUY" if o.side == 1 else "SELL"
        status, fq, avg = ("FILLED", o.qty, f"{o.price:.2f}") if o.filled else ("CANCELED", 0, "")
        rows.append(
            f"O-{o.oid},V-{o.oid},{o.symbol}.XCME,{side},LIMIT,{o.qty},{o.price:.2f},GTC,False,False,{status},{fq},"
            f"{avg},{o.ts},{o.ts_end}\n"
        )
    path.write_text(head + "".join(rows))
    return path


def write_backtrader(path: Path, os_: list[Order]) -> Path:
    """Backtrader `QtOrderLog` notification lines (JSON lines)."""
    lines = []
    for o in os_:
        size = o.qty if o.side == 1 else -o.qty
        base = {
            "event": "order",
            "ref": o.oid,
            "data": o.symbol,
            "ordtype": "Buy" if o.side == 1 else "Sell",
            "exectype": "Limit",
            "size": size,
            "price": o.price,
            "pricelimit": o.price,
            "created": _iso(o.ts),
            "valid": None,
            "order_key": None,
        }
        lines.append({**base, "status": "Submitted", "dt": _iso(o.ts), "exbits": []})
        lines.append({**base, "status": "Accepted", "dt": _iso(o.ts), "exbits": []})
        if o.filled:
            bit = {"dt": _iso(o.ts_end), "size": size, "price": o.price, "comm": 0}
            lines.append({**base, "status": "Completed", "dt": _iso(o.ts_end), "exbits": [bit]})
        else:
            lines.append({**base, "status": "Canceled", "dt": _iso(o.ts_end), "exbits": []})
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


def write_vectorbt(path: Path, os_: list[Order]) -> Path:
    """VectorBT order records (`pf.orders.records_readable.to_csv()`): the filled orders only."""
    rows = [",Order Id,Column,Timestamp,Size,Price,Fees,Side\n"]
    for k, o in enumerate(x for x in os_ if x.filled):
        ts = dt.datetime.fromtimestamp(o.ts / 1e9, tz=dt.UTC).strftime("%Y-%m-%d %H:%M:%S+00:00")
        rows.append(f"{k},{k},{o.symbol},{ts},{o.qty},{o.price:.2f},0.0,{'Buy' if o.side == 1 else 'Sell'}\n")
    path.write_text("".join(rows))
    return path
