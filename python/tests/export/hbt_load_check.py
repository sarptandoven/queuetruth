"""Runs inside a separate hftbacktest 2.4.4 venv (never imported by pytest): executes a generated
`hftbacktest_snippet` file, completes the asset with a tiny synthetic event array, runs a backtest that posts one
resting buy order, and prints a JSON line with the outcome. Usage: python hbt_load_check.py SNIPPET.py WORKDIR."""

import json
import sys
from pathlib import Path

import numpy as np
from hftbacktest import (  # noqa: F401  (BacktestAsset used by exec)
    GTX,
    LIMIT,
    BacktestAsset,
    HashMapMarketDepthBacktest,
)

EVENT_DTYPE = np.dtype(
    [
        ("ev", "<u8"),
        ("exch_ts", "<i8"),
        ("local_ts", "<i8"),
        ("px", "<f8"),
        ("qty", "<f8"),
        ("order_id", "<u8"),
        ("ival", "<i8"),
        ("fval", "<f8"),
    ],
    align=True,
)
EXCH, LOCAL, BUY, SELL = 1 << 31, 1 << 30, 1 << 29, 1 << 28
DEPTH, TRADE = 1, 2


def main() -> None:
    snippet = Path(sys.argv[1]).read_text()
    Path(sys.argv[2])
    ns: dict = {}
    exec(compile(snippet, "snippet", "exec"), ns)
    asset = ns["asset"]
    rows = []
    t = 1_000_000_000
    for i in range(200):
        ts = t + i * 10_000_000
        rows.append((DEPTH | BUY | EXCH | LOCAL, ts, ts + 1000, 100.0, 50.0, 0, 0, 0.0))
        rows.append((DEPTH | SELL | EXCH | LOCAL, ts, ts + 1000, 100.01, 50.0, 0, 0, 0.0))
        if i % 5 == 4:
            rows.append((TRADE | SELL | EXCH | LOCAL, ts + 1, ts + 1001, 100.0, 20.0, 0, 0, 0.0))
    data = np.array(rows, dtype=EVENT_DTYPE)
    asset = asset.data([data]).linear_asset(1.0).tick_size(0.01).lot_size(1.0)
    if ns.get("NEEDS_LATENCY", True) and "intp_order_latency" not in snippet:
        asset = asset.constant_latency(0, 0)
    hbt = HashMapMarketDepthBacktest([asset])
    hbt.elapse(100_000_000)
    hbt.submit_buy_order(0, 1, 100.0, 5.0, GTX, LIMIT, False)
    for _ in range(150):
        if hbt.elapse(10_000_000) != 0:
            break
    pos = hbt.position(0)
    hbt.close()
    print(json.dumps({"ok": True, "position": pos}))


if __name__ == "__main__":
    main()
