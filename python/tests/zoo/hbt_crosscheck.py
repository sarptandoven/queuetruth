#!/usr/bin/env python3
"""O-ZOO-3 style driver: run the released hftbacktest Python wheel (2.4.4) on SPEC 5.3.5 npz feeds.

Input directory layout (written by gen_scenarios.py, or by queuetruth `write_hbt_npz` plus an orders sidecar):
  <dir>/<name>.npz           np.savez_compressed(path, data=arr), arr dtype = SPEC 5.3.5 / hftbacktest event_dtype
  <dir>/<name>.orders.json   {"tick_size": f, "lot_size": f, "actions": [{"ts", "kind": "submit"|"cancel",
                              "order_id", "side": "B"|"A", "px", "qty", "tif": "GTX"|"GTC"}]}

For every npz x queue model x exchange this script replays the feed with zero latency and zero fees, issuing the
actions at their timestamps (after all feed rows with ts <= action ts, before later rows), and records:
  fills         one per local order response that reduced leaves_qty (ts = exchange fill timestamp)
  order_events  status changes without a fill (expired GTX, canceled)
  q_ack         queue state copied into the local order at the ack response (front qty [, cum_trade qty]), read
                through the order's `_q1` box pointer: the only queue estimate the Python API exposes
  error         first non-zero elapse/submit/cancel code (14 = InvalidOrderStatus, hbt #312) and where; the run
                stops there because the exchange row that errored would be re-processed
  first_fill    order_id -> first fill ts (the O-ZOO-3 comparison quantity)

Run with the separate venv only (`make hbt-crosscheck`):
  .scratch/hbt-venv/bin/python python/tests/zoo/hbt_crosscheck.py <npz dir> --out out.json
Adopted from the hbt cross-check harness used during development (validated: identical fills to a Rust build of
hftbacktest 5f3ec40 on 2,988 runs). hftbacktest is MIT (third_party/licenses/hftbacktest-MIT.txt); this driver only
calls its public API.
Needs only hftbacktest==2.4.4 (+ its numba/numpy).  Plain-Python calls into the jitclass wrappers are used, which is
slow-ish but fine for synthetic sizes.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
from pathlib import Path

import hftbacktest
import numpy as np
from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from hftbacktest.order import EXPIRED, GTC, GTX, LIMIT, NONE, REJECTED

STATUS = {0: "none", 1: "new", 2: "expired", 3: "filled", 4: "canceled", 5: "partially_filled", 6: "rejected"}

MODELS: list[tuple[str, str, float | None]] = [("hbt_risk_adverse", "risk_adverse_queue_model", None)]
for fam, meth in (
    ("hbt_power", "power_prob_queue_model"),
    ("hbt_power2", "power_prob_queue_model2"),
    ("hbt_power3", "power_prob_queue_model3"),
):
    for n in (1.0, 2.0, 3.0, 0.5, 1.5):
        MODELS.append((fam, meth, n))
MODELS += [("hbt_log", "log_prob_queue_model", None), ("hbt_log2", "log_prob_queue_model2", None)]
EXCHS = [("no_partial", "no_partial_fill_exchange"), ("partial", "partial_fill_exchange")]


def fmt_f(x: float) -> str:
    return np.format_float_positional(x, unique=True, trim="-")


def model_id(fam: str, n: float | None, exch: str) -> str:
    keys = {"exch": exch, "mode": "faithful"}
    if n is not None:
        keys["n"] = fmt_f(n)
    return fam + "[" + ";".join(f"{k}={v}" for k, v in sorted(keys.items())) + "]"


def jnum(x: float):
    return float(x) if np.isfinite(x) else None


def read_q(order, is_prob: bool):
    st = int(order.status)
    if st in (NONE, EXPIRED, REJECTED):
        return None
    ptr = int(order.arr[0]["_q1"])
    if ptr < 4096:
        return None
    vals = (ctypes.c_double * (2 if is_prob else 1)).from_address(ptr)
    return [float(v) for v in vals]


def run_one(
    data: np.ndarray, meta: dict, meth: str, n: float | None, exch_meth: str, is_prob: bool, coarse: bool = False
) -> dict:
    asset = (
        BacktestAsset()
        .data(data)
        .linear_asset(1.0)
        .constant_order_latency(0, 0)
        .trading_value_fee_model(0.0, 0.0)
        .tick_size(meta["tick_size"])
        .lot_size(meta["lot_size"])
        .last_trades_capacity(0)
    )
    asset = getattr(asset, meth)(n) if n is not None else getattr(asset, meth)()
    asset = getattr(asset, exch_meth)()
    hbt = HashMapMarketDepthBacktest([asset])

    actions = meta["actions"]
    if coarse:
        # step only at action timestamps, then run to the end: fill ts are still exact (exchange timestamps) as long
        # as an order fills at most once between two steps, which always holds on no_partial.
        timeline = [(int(data[0]["exch_ts"]), 0, 0)]
        timeline += [(int(a["ts"]), 1, j) for j, a in enumerate(actions)]
        timeline += [(int(data[-1]["exch_ts"]), 0, len(data) - 1)]
    else:
        timeline = [(int(data[i]["exch_ts"]), 0, i) for i in range(len(data))]
        timeline += [(int(a["ts"]), 1, j) for j, a in enumerate(actions)]
    timeline.sort()
    oids = sorted({int(a["order_id"]) for a in actions})

    prev = {}
    fills, order_events, q_ack = [], [], {}
    error = None
    started = False
    for step, (ts, kind, idx) in enumerate(timeline):
        if not started:
            assert kind == 0 and idx == 0
            rc = hbt.elapse(0)
            started = True
        else:
            rc = hbt.elapse(max(ts - hbt.current_timestamp, 0))
        if rc not in (0, 1):
            error = {"step": step, "ts": ts, "error_code": int(rc)}
            break
        if kind == 1:
            a = actions[idx]
            oid = int(a["order_id"])
            if a["kind"] == "submit":
                tif = {"GTX": GTX, "GTC": GTC}[a["tif"]]
                f = hbt.submit_buy_order if a["side"] == "B" else hbt.submit_sell_order
                rc = f(0, oid, float(a["px"]), float(a["qty"]), tif, LIMIT, False)
            else:
                rc = hbt.cancel(0, oid, False)
            if rc != 0:
                error = {"step": step, "ts": ts, "error_code": int(rc), "in": "action"}
                break
            rc = hbt.elapse(0)
            if rc not in (0, 1):
                error = {"step": step, "ts": ts, "error_code": int(rc)}
                break
        orders = hbt.orders(0)
        pos = float(hbt.position(0))
        for oid in oids:
            o = orders.get(oid)
            if o is None:
                continue
            leaves, st = float(o.leaves_qty), int(o.status)
            pl, ps = prev.get(oid, (float(o.qty), NONE))
            if leaves < pl:
                fills.append(
                    {
                        "step": step,
                        "order_id": oid,
                        "ts": int(o.exch_timestamp),
                        "fill_qty": pl - leaves,
                        "fill_qty_hex": float(pl - leaves).hex(),
                        "exec_qty": float(o.exec_qty),
                        "leaves_qty": leaves,
                        "status": STATUS[st],
                        "position_after": pos,
                    }
                )
            elif st != ps:
                order_events.append(
                    {
                        "step": step,
                        "order_id": oid,
                        "ts": int(o.exch_timestamp),
                        "status": STATUS[st],
                        "leaves_qty": leaves,
                    }
                )
            if str(oid) not in q_ack and st not in (NONE,):
                q = read_q(o, is_prob)
                if q is not None:
                    q_ack[str(oid)] = {"step": step, "q": [jnum(v) for v in q], "q_hex": [v.hex() for v in q]}
            prev[oid] = (leaves, st)
    first_fill = {}
    for f in fills:
        first_fill.setdefault(str(f["order_id"]), f["ts"])
    out = {
        "fills": fills,
        "order_events": order_events,
        "q_ack": q_ack,
        "error": error,
        "first_fill": first_fill,
        "position_end": float(hbt.position(0)),
    }
    hbt.close()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("npz_dir", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--only", default=None, help="comma list of npz stems")
    ap.add_argument(
        "--coarse",
        action="store_true",
        help="step only at actions (fast; exact first-fill ts on no_partial; step ids differ)",
    )
    ap.add_argument(
        "--models",
        default=None,
        help="comma list of model ids without exch, e.g. 'hbt_risk_adverse,hbt_power3[n=2]' (default: all 18)",
    )
    ap.add_argument("--exch", default="no_partial,partial")
    args = ap.parse_args(argv)
    files = sorted(args.npz_dir.glob("*.npz"))
    if args.only:
        keep = set(args.only.split(","))
        files = [f for f in files if f.stem in keep]
    runs = []
    for f in files:
        data = np.load(f)["data"]
        meta = (
            json.loads(f.with_suffix(".orders.json").read_text())
            if f.with_suffix(".orders.json").exists()
            else json.loads((f.parent / (f.stem + ".orders.json")).read_text())
        )
        data = np.ascontiguousarray(data.astype(hftbacktest.event_dtype))
        for fam, meth, n in MODELS:
            short = fam if n is None else f"{fam}[n={fmt_f(n)}]"
            if args.models and short not in args.models.split(","):
                continue
            for exname, exmeth in EXCHS:
                if exname not in args.exch.split(","):
                    continue
                r = run_one(data, meta, meth, n, exmeth, fam != "hbt_risk_adverse", coarse=args.coarse)
                r.update(
                    {"scenario": f.stem, "model_id": model_id(fam, n, exname), "family": fam, "n": n, "exch": exname}
                )
                runs.append(r)
        print(f"{f.stem}: done", file=sys.stderr)
    doc = {
        "schema": "qt.hbt_crosscheck.py_runs",
        "schema_version": 1,
        "engine": "hftbacktest 2.4.4 (PyPI wheel)",
        "coarse": args.coarse,
        "runs": runs,
    }
    if args.out:
        args.out.write_text(json.dumps(doc) + "\n")
    else:
        json.dump(doc, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
