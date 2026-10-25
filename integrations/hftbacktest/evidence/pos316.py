#!/usr/bin/env python3
"""hftbacktest #316 measurement (hftbacktest venv): a resting GTX buy of 2 at the touch with 1 lot ahead, filled 1
then 1 by two sell trades, under `risk_adverse_queue_model` and `partial_fill_exchange`. Prints, after each step, the
order's status, executed and leaves quantity and `position(0)`, then a JSON summary line. With correct accounting the
position is 2.0 after both fills; released 2.4.4 and the #312-patched build give 1.0 (#316: the first, partial fill
never reaches the position).

Usage: <hbt venv>/bin/python -I pos316.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from hftbacktest.order import GTX, LIMIT
from hftbacktest.types import BUY_EVENT, DEPTH_EVENT, EXCH_EVENT, LOCAL_EVENT, SELL_EVENT, TRADE_EVENT, event_dtype

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hbt_driver import engine_identity

ROWS = [
    (DEPTH_EVENT | BUY_EVENT, 1, 100.0, 1.0),
    (DEPTH_EVENT | SELL_EVENT, 1, 100.01, 5.0),
    (TRADE_EVENT | SELL_EVENT, 3, 100.0, 2.0),
    (TRADE_EVENT | SELL_EVENT, 4, 100.0, 1.0),
    (DEPTH_EVENT | BUY_EVENT, 6, 100.0, 3.0),
]


def main() -> int:
    d = np.zeros(len(ROWS), event_dtype)
    for i, (e, t, p, q) in enumerate(ROWS):
        d[i]["ev"] = EXCH_EVENT | LOCAL_EVENT | e
        d[i]["exch_ts"] = d[i]["local_ts"] = t
        d[i]["px"] = p
        d[i]["qty"] = q
    a = (
        BacktestAsset().data(d).linear_asset(1.0).constant_order_latency(0, 0).risk_adverse_queue_model()
        .partial_fill_exchange().trading_value_fee_model(0.0, 0.0).tick_size(0.01).lot_size(1.0)
    )  # fmt: skip
    h = HashMapMarketDepthBacktest([a])
    h.elapse(0)
    h.submit_buy_order(0, 1, 100.0, 2.0, GTX, LIMIT, False)
    steps = []
    while True:
        rc = h.elapse(1)
        o = h.orders(0).get(1)
        steps.append([int(h.current_timestamp), int(rc), int(o.status), float(o.exec_qty), float(o.leaves_qty),
                      float(h.position(0))])  # fmt: skip
        print("ts {} rc {} status {} exec_qty {} leaves_qty {} position {}".format(*steps[-1]))
        if rc:
            break
    filled = 2.0 - steps[-1][4]
    ident = engine_identity()
    print(json.dumps({"engine": ident, "filled_qty": filled, "position": steps[-1][5]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
