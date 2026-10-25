#!/usr/bin/env python3
"""Minimal reproduction of hftbacktest #312 (run it with the hftbacktest venv; numpy + hftbacktest only).

PartialFillExchange marks an order `Filled` when a maker fill leaves less than half a lot, but removes it from the
book only when the queue model's fill is strictly larger than the leaves. The completed order stays in the book; the
next trade at (or through) its price calls `fill()` on it again, which returns `InvalidOrderStatus` and makes
`elapse` return 14, stopping the whole backtest.

Cases (one GTX order, zero latency, risk-adverse queue model, `partial_fill_exchange`):
  equal_leaves        lot 1, buy 2 with 1 ahead: fills of 1 then exactly the remaining 1, then another trade
  float_residual      lot 0.01, buy 0.1 with 0.01 ahead (the report's sizes): fills of 0.01 then 0.09, where the
                      leaves are 0.1 - 0.01 = 0.09000000000000001 > 0.09, so the reporter's `>=` fix misses it
  non_lot_quantity    lot 1, buy 1.4 with 1 ahead: after one lot the 0.4 left rounds to zero lots
  trade_through       lot 1, sell 2: completion by an at-price fill, then a buy trade through the price

Exit status: 0 when no case aborts (fixed build), 3 when at least one case reproduces the abort, 2 on a usage error.
`--json` prints the per-case results as JSON.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import sys

try:
    import numpy as np
    from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
    from hftbacktest.order import GTX, LIMIT
    from hftbacktest.types import BUY_EVENT, DEPTH_EVENT, EXCH_EVENT, LOCAL_EVENT, SELL_EVENT, TRADE_EVENT, event_dtype
except ImportError as e:  # pragma: no cover - depends on the interpreter
    print(f"QT-USAGE-001: run this with a Python that has hftbacktest installed ({e})", file=sys.stderr)
    raise SystemExit(2) from e

STATUS = {0: "none", 1: "new", 2: "expired", 3: "filled", 4: "canceled", 5: "partially_filled", 6: "rejected"}
BID, ASK = DEPTH_EVENT | BUY_EVENT, DEPTH_EVENT | SELL_EVENT
SELL_TRADE, BUY_TRADE = TRADE_EVENT | SELL_EVENT, TRADE_EVENT | BUY_EVENT

CASES = {
    "equal_leaves": dict(
        lot=1.0,
        buy=True,
        px=100.0,
        qty=2.0,
        rows=[
            (BID, 1, 100.0, 1.0),
            (ASK, 1, 100.01, 5.0),
            (SELL_TRADE, 3, 100.0, 2.0),
            (SELL_TRADE, 4, 100.0, 1.0),
            (SELL_TRADE, 5, 100.0, 1.0),
        ],
    ),
    "float_residual": dict(
        lot=0.01,
        buy=True,
        px=100.0,
        qty=0.1,
        rows=[
            (BID, 1, 100.0, 0.01),
            (ASK, 1, 100.01, 5.0),
            (SELL_TRADE, 3, 100.0, 0.02),
            (SELL_TRADE, 4, 100.0, 0.09),
            (SELL_TRADE, 5, 100.0, 0.01),
        ],
    ),
    "non_lot_quantity": dict(
        lot=1.0,
        buy=True,
        px=100.0,
        qty=1.4,
        rows=[
            (BID, 1, 100.0, 1.0),
            (ASK, 1, 100.01, 5.0),
            (SELL_TRADE, 3, 100.0, 2.0),
            (SELL_TRADE, 4, 100.0, 2.0),
        ],
    ),
    "trade_through": dict(
        lot=1.0,
        buy=False,
        px=100.0,
        qty=2.0,
        rows=[
            (BID, 1, 99.99, 5.0),
            (ASK, 1, 100.0, 1.0),
            (BUY_TRADE, 3, 100.0, 2.0),
            (BUY_TRADE, 4, 100.0, 1.0),
            (BUY_TRADE, 5, 100.02, 4.0),
        ],
    ),
}


def run_case(lot: float, buy: bool, px: float, qty: float, rows: list) -> dict:
    """Replay `rows` with one GTX order submitted after the first row; return codes and the order's states."""
    data = np.zeros(len(rows), event_dtype)
    for i, (ev, ts, p, q) in enumerate(rows):
        data[i]["ev"] = EXCH_EVENT | LOCAL_EVENT | ev
        data[i]["exch_ts"] = ts
        data[i]["local_ts"] = ts
        data[i]["px"] = p
        data[i]["qty"] = q
    asset = (
        BacktestAsset()
        .data(data)
        .linear_asset(1.0)
        .constant_order_latency(0, 0)
        .risk_adverse_queue_model()
        .partial_fill_exchange()
        .trading_value_fee_model(0.0, 0.0)
        .tick_size(0.01)
        .lot_size(lot)
    )
    hbt = HashMapMarketDepthBacktest([asset])
    hbt.elapse(0)
    submit = hbt.submit_buy_order if buy else hbt.submit_sell_order
    submit(0, 1, px, qty, GTX, LIMIT, False)
    states, codes = [], []
    while True:
        rc = int(hbt.elapse(1))
        codes.append(rc)
        if rc not in (0, 1):
            break
        o = hbt.orders(0).get(1)
        state = (STATUS[int(o.status)], float(o.exec_qty), float(o.leaves_qty))
        if not states or states[-1][1:] != state:
            states.append((int(hbt.current_timestamp), *state))
        if rc == 1:
            break
    hbt.close()
    keys = ("ts", "status", "exec_qty", "leaves_qty")
    return {
        "final_code": codes[-1],
        "aborted": codes[-1] == 14,
        "states": [dict(zip(keys, s, strict=True)) for s in states],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="print the results as JSON")
    ap.add_argument("--case", choices=sorted(CASES), action="append", help="run only these cases")
    args = ap.parse_args(argv)
    names = args.case or list(CASES)
    results = {name: run_case(**CASES[name]) for name in names}
    if args.json:
        print(json.dumps({"hftbacktest": md.version("hftbacktest"), "cases": results}, sort_keys=True))
    else:
        print(f"hftbacktest {md.version('hftbacktest')}")
        for name, r in results.items():
            last = r["states"][-1] if r["states"] else {}
            verdict = "ABORT: elapse returned 14 (InvalidOrderStatus)" if r["aborted"] else "ok (ran to end of data)"
            print(f"  {name:<17} {verdict}; last order state {last}")
    return 3 if any(r["aborted"] for r in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
