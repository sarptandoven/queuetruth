"""Run the released hftbacktest (L3FIFOQueueModel, L3 no-partial-fill exchange) on the harness's orders.

Usage (in the hftbacktest venv of integrations/hftbacktest/setup_venvs.sh):

    .scratch/hbt-venv/bin/python -I hbt_l3_run.py <records.bin> <tick> <shadows.jsonl> <out.json>

The MBO records (`dump_mbo.py`) become hftbacktest L3 events: `A` an add, `C` a cancel when it removes the rest of
the order and else a modify to the smaller size (hftbacktest keeps the priority of a size decrease), `F` a fill with
the resting order's side at the execution price, `R` a depth clear. Hidden prints (`T`, side `N`) carry no order and
are left out, which is what hftbacktest does with a fill without a side. Prices are fed in ticks (tick size 1). The
orders are the harness's shadows: GTX, 100 shares, zero latency, issued after every row with `exch_ts <= ts_live`
and cancelled at their end, with the loop of `integrations/hftbacktest/hbt_driver.py`. Output: the first fill time
and the final status of every order, keyed by the harness's shadow id.
"""

import json
import sys
from pathlib import Path

import numpy as np

rec_path, tick, sh_path, out_path = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "integrations" / "hftbacktest"))
import hbt_driver  # noqa: E402
import hftbacktest as H  # noqa: E402
from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest  # noqa: E402
from numba import njit  # noqa: E402

dt = np.dtype(
    [
        ("ts", "<i8"),
        ("px", "<i8"),
        ("size", "<u8"),
        ("oid", "<u8"),
        ("seq", "<u8"),
        ("action", "u1"),
        ("side", "u1"),
        ("flags", "u1"),
        ("pad", "V5"),
    ]
)
r = np.fromfile(rec_path, dt)
E = H.EXCH_EVENT | H.LOCAL_EVENT
rows = []
live = {}  # oid -> [side_flag, px_ticks, remaining]
n_skip = {"T": 0, "other": 0}
for x in r:
    a, oid, ts = chr(x["action"]), int(x["oid"]), int(x["ts"])
    sideflag = H.BUY_EVENT if x["side"] == ord("B") else H.SELL_EVENT
    if a == "A":
        p = int(x["px"]) // tick
        live[oid] = [sideflag, p, int(x["size"])]
        rows.append((E | sideflag | H.ADD_ORDER_EVENT, ts, ts, float(p), float(x["size"]), oid, 0, 0.0))
    elif a == "C":
        o = live.get(oid)
        if o is None:
            n_skip["other"] += 1
            continue
        o[2] -= int(x["size"])
        if o[2] <= 0:
            del live[oid]
            rows.append((E | o[0] | H.CANCEL_ORDER_EVENT, ts, ts, float(o[1]), 0.0, oid, 0, 0.0))
        else:
            rows.append((E | o[0] | H.MODIFY_ORDER_EVENT, ts, ts, float(o[1]), float(o[2]), oid, 0, 0.0))
    elif a == "F":
        o = live.get(oid)
        if o is None:
            n_skip["other"] += 1
            continue
        rows.append((E | o[0] | H.FILL_EVENT, ts, ts, float(int(x["px"]) / tick), float(x["size"]), oid, 0, 0.0))
    elif a == "R":
        live.clear()
        rows.append((E | H.DEPTH_CLEAR_EVENT, ts, ts, 0.0, 0.0, 0, 0, 0.0))
    elif a == "T":
        n_skip["T"] += 1
    else:
        n_skip["other"] += 1
data = np.array(rows, dtype=H.event_dtype)

shadows = [json.loads(line) for line in open(sh_path)]
actions = []
for i, s in enumerate(shadows):
    actions.append(
        {
            "ts": s["ts_live"],
            "kind": "submit",
            "order_id": i + 1,
            "side": s["side"],
            "px": float(s["px"] // tick),
            "qty": 100.0,
            "tif": "GTX",
        }
    )
    actions.append({"ts": s["deadline"], "kind": "cancel", "order_id": i + 1})
acts = hbt_driver.load_actions({"actions": actions})
loop = hbt_driver.make_loop(njit)


def backtest():
    asset = (
        BacktestAsset()
        .data(data)
        .linear_asset(1.0)
        .constant_order_latency(0, 0)
        .trading_value_fee_model(0.0, 0.0)
        .tick_size(1.0)
        .lot_size(1.0)
        .last_trades_capacity(0)
        .l3_fifo_queue_model()
        .no_partial_fill_exchange()
    )
    return HashMapMarketDepthBacktest([asset])


n = len(shadows)
cap = 4 * n + 4
f_oid, f_ts = np.zeros(cap, np.int64), np.zeros(cap, np.int64)
f_qty, f_left, f_st = np.zeros(cap), np.zeros(cap), np.zeros(cap, np.int64)
step_ts = np.unique(data["exch_ts"].astype(np.int64))
hbt = backtest()
res = loop(
    hbt,
    step_ts,
    acts["ts"],
    acts["kind"],
    acts["oid"],
    acts["side"],
    acts["px"],
    acts["qty"],
    acts["tif"],
    n,
    np.iinfo(np.int64).max,
    f_oid,
    f_ts,
    f_qty,
    f_left,
    f_st,
)
n_fills, n_steps, err, err_ts = res[0], res[1], res[2], res[3]
orders = hbt.orders(0)
first = {}
for k in range(n_fills):
    first.setdefault(int(f_oid[k]), int(f_ts[k]))
status = {}
for i in range(1, n + 1):
    o = orders.get(i)
    status[i] = int(o.status) if o is not None else -1
Path(out_path).write_text(
    json.dumps(
        {
            "rows": len(data),
            "skipped": n_skip,
            "error": int(err),
            "err_ts": int(err_ts),
            "n_fills": int(n_fills),
            "first_fill": {str(shadows[i - 1]["id"]): first.get(i) for i in range(1, n + 1)},
            "status": {str(shadows[i - 1]["id"]): status[i] for i in range(1, n + 1)},
        }
    )
)
print(f"rows {len(data)} fills {n_fills} err {err} skipped {n_skip}")
