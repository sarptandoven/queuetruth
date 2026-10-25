#!/usr/bin/env python3
"""Run hftbacktest on a queuetruth L2 npz with a set of own orders and record every fill (hftbacktest venv only).

Input: a SPEC 5.3.5 npz (`queuetruth export l2-npz`), an orders sidecar and a `qtint.hbt_queue_config` file.

  orders sidecar  {"tick_size": f, "lot_size": f, "actions": [{"ts", "kind": "submit"|"cancel", "order_id",
                   "side": "B"|"A", "px", "qty", "tif": "GTX"|"GTC"}], "shadow_ids": {"<order_id>": "<shadow id>"}}
                  (the SPEC 5.3.5 layout; `shadow_ids` is optional).

Replay semantics (identical to the O-ZOO-3 driver `python/tests/zoo/hbt_crosscheck.py` and to queuetruth's zoo
activation rule): zero order latency, zero fees, `HashMapMarketDepthBacktest`; an action at `ts` is issued after
every feed row with `exch_ts <= ts` and before any later row. While an own order is live the loop steps to every
distinct `exch_ts`, so a fill is recorded at its exchange timestamp; two fills of one order at one timestamp are
reported as one fill with their summed quantity. When no own order is live it jumps to the next action, and after
the last action it replays the rest of the feed (a completed #312 order can still abort the run). A cancel is sent
only while the order is still active locally (`NEW` / `PARTIALLY_FILLED`) with no pending request.

The loop is a numba `@njit` function, the way hftbacktest strategies are written, so a full instrument-day runs in
seconds. The first non-zero `elapse` / `submit` / `cancel` code stops the run (hftbacktest cannot continue past
it: 14 = `InvalidOrderStatus`, the #312 abort); the result records the code, the timestamp and the orders still live.
When the failing `elapse` covered several feed timestamps (a jump), the run is repeated stepping every timestamp from
that elapse's start, so the reported timestamp is the failing event's own.

Output (`--out`, JSON with sorted keys): `qtint.hbt_driver_run` v1 with `engine` (the `--engine-label`, default
`hftbacktest <version>`), `engine_identity` (`engine_identity()`: hftbacktest version, sha256 of its native module,
numba / llvmlite / numpy / Python versions), `npz_load_s`, `compile_s` (numba compilation of the loop, done once on a
two-row dummy feed before any model is timed) and one run per model: `fills` [{order_id, shadow_id, ts, qty,
leaves_after, status}], `orders` [{order_id, shadow_id, status, filled_qty, leaves_qty, first_fill_ts}], `error`
{code, ts, live_order_ids} or null, `n_steps`, `wall_s` (the loop alone, compilation excluded).

`--identify` prints `engine_identity()` as JSON and exits (reconcile.py records it in its cache manifest).

Exit codes: 0 done, 2 bad input (`QT-USAGE-001`, `QT-CONFIG-001`, `QT-INPUT-00x` on stderr).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hbt_config

SCHEMA = "qtint.hbt_driver_run"
STATUS = {0: "none", 1: "new", 2: "expired", 3: "filled", 4: "canceled", 5: "partially_filled", 6: "rejected"}
KIND_SUBMIT, KIND_CANCEL = 0, 1


def _import_hbt():
    try:
        import hftbacktest
        from numba import njit
    except ImportError:  # pragma: no cover - exercised only outside the hbt venv
        print(
            "QT-USAGE-001: hbt_driver.py needs hftbacktest and numba; run it with the hftbacktest venv "
            "(integrations/hftbacktest/setup_venvs.sh)",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    return hftbacktest, njit


def engine_identity() -> dict[str, Any]:
    """The hftbacktest build this process runs: versions and the sha256 of the installed native module(s)."""
    import importlib.metadata as md

    import hftbacktest

    pkg = Path(hftbacktest.__file__).resolve().parent
    natives = sorted(p for p in pkg.iterdir() if p.is_file() and p.suffix in (".so", ".pyd", ".dylib"))
    return {
        "hftbacktest": md.version("hftbacktest"),
        "native_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in natives},
        "numba": md.version("numba"),
        "llvmlite": md.version("llvmlite"),
        "numpy": md.version("numpy"),
        "python": platform.python_version(),
    }


def load_actions(meta: dict[str, Any]) -> dict[str, np.ndarray]:
    """Actions as sorted column arrays (stable by ts; order ids must be 1..N, each submitted once)."""
    acts = sorted(enumerate(meta["actions"]), key=lambda t: (int(t[1]["ts"]), t[0]))
    n = len(acts)
    out = {
        "ts": np.empty(n, np.int64),
        "kind": np.empty(n, np.int64),
        "oid": np.empty(n, np.int64),
        "side": np.empty(n, np.int64),
        "px": np.empty(n, np.float64),
        "qty": np.empty(n, np.float64),
        "tif": np.empty(n, np.int64),
    }
    submitted: set[int] = set()
    for i, (_, a) in enumerate(acts):
        kind = {"submit": KIND_SUBMIT, "cancel": KIND_CANCEL}[a["kind"]]
        oid = int(a["order_id"])
        if kind == KIND_SUBMIT:
            if oid in submitted:
                raise ValueError(f"QT-INPUT-003: duplicate order_id: {oid} is submitted twice")
            submitted.add(oid)
        out["ts"][i] = int(a["ts"])
        out["kind"][i] = kind
        out["oid"][i] = oid
        out["side"][i] = 1 if a.get("side", "B") == "B" else -1
        out["px"][i] = float(a.get("px", 0.0))
        out["qty"][i] = float(a.get("qty", 0.0))
        out["tif"][i] = {"GTX": 1, "GTC": 0}[a.get("tif", "GTX")]
    if submitted and (min(submitted) < 1 or max(submitted) != len(submitted)):
        raise ValueError("QT-INPUT-002: submitted order ids must be exactly 1..N")
    return out


def make_loop(njit):
    @njit
    def loop(
        hbt, step_ts, a_ts, a_kind, a_oid, a_side, a_px, a_qty, a_tif, n_orders, precise_after, f_oid, f_ts, f_qty,
        f_left, f_st,
    ):  # fmt: skip
        """Returns (n_fills, n_steps, err_code, err_ts, err_rows, err_from, live, n_live).

        Steps to every distinct feed timestamp while an own order is live or once `current_timestamp >=
        precise_after`; otherwise jumps to the next action (or, at the end, to the last row). `err_rows` is the
        number of distinct feed timestamps in the elapse that failed (1: `err_ts` is the failing event's time) and
        `err_from` the timestamp it started from.
        """
        prev_leaves = np.zeros(n_orders, np.float64)
        live = np.zeros(n_orders, np.int64)  # ids of the live own orders
        is_live = np.zeros(n_orders, np.bool_)
        n_live = 0
        n_fills = 0
        n_steps = 0
        err_code = 0
        err_ts = 0
        err_rows = 0
        err_from = 0
        rc = hbt.elapse(0)
        if rc != 0 and rc != 1:
            return n_fills, n_steps, rc, hbt.current_timestamp, 1, 0, live, n_live
        i = 0
        j = 0
        ns = step_ts.shape[0]
        na = a_ts.shape[0]
        while True:
            cur = hbt.current_timestamp
            while i < ns and step_ts[i] <= cur:
                i += 1
            stepping = n_live > 0 or cur >= precise_after
            is_action = False
            if j < na and (not stepping or i >= ns or a_ts[j] < step_ts[i]):
                target = a_ts[j]
                is_action = True
                if not stepping and cur < precise_after < target:
                    target = precise_after
                    is_action = False
            elif stepping and i < ns:
                target = step_ts[i]
            elif not stepping and i < ns and cur < step_ts[ns - 1]:
                # no live order and no action left: replay the rest of the feed as a full backtest would (a #312
                # zombie aborts on a later trade), stepping once precise_after is reached
                target = step_ts[ns - 1]
                if cur < precise_after < target:
                    target = precise_after
            else:
                break
            if target > cur:
                i0 = i
                rc = hbt.elapse(target - cur)
                n_steps += 1
                while i < ns and step_ts[i] <= target:
                    i += 1
                if rc != 0 and rc != 1:
                    err_code = rc
                    err_ts = target
                    err_rows = i - i0
                    err_from = cur
                    break
                if rc == 1:
                    i = ns  # end of data
            if is_action:
                oid = a_oid[j]
                if a_kind[j] == 0:
                    if a_side[j] > 0:
                        rc = hbt.submit_buy_order(0, oid, a_px[j], a_qty[j], a_tif[j], 0, False)
                    else:
                        rc = hbt.submit_sell_order(0, oid, a_px[j], a_qty[j], a_tif[j], 0, False)
                    if rc != 0:
                        err_code = rc
                        err_ts = a_ts[j]
                        err_rows = 0
                        break
                    prev_leaves[oid - 1] = a_qty[j]
                    if not is_live[oid - 1]:
                        is_live[oid - 1] = True
                        live[n_live] = oid
                        n_live += 1
                else:
                    o = hbt.orders(0).get(oid)
                    if o is not None and (o.status == 1 or o.status == 5) and o.req == 0:
                        rc = hbt.cancel(0, oid, False)
                        if rc != 0:
                            err_code = rc
                            err_ts = a_ts[j]
                            err_rows = 0
                            break
                j += 1
                rc = hbt.elapse(0)
                if rc != 0 and rc != 1:
                    err_code = rc
                    err_ts = target
                    err_rows = 0
                    break
            # record changes of the live orders
            orders = hbt.orders(0)
            k = 0
            while k < n_live:
                oid = live[k]
                o = orders.get(oid)
                done = False
                if o is not None:
                    leaves = o.leaves_qty
                    st = o.status
                    if leaves < prev_leaves[oid - 1]:
                        f_oid[n_fills] = oid
                        f_ts[n_fills] = o.exch_timestamp
                        f_qty[n_fills] = prev_leaves[oid - 1] - leaves
                        f_left[n_fills] = leaves
                        f_st[n_fills] = st
                        n_fills += 1
                        prev_leaves[oid - 1] = leaves
                    done = st == 2 or st == 3 or st == 4 or st == 6
                if done:
                    is_live[oid - 1] = False
                    live[k] = live[n_live - 1]
                    n_live -= 1
                else:
                    k += 1
        return n_fills, n_steps, err_code, err_ts, err_rows, err_from, live, n_live

    return loop


def _backtest(data: np.ndarray, tick: float, lot: float, entry: dict):
    from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest

    asset = (
        BacktestAsset()
        .data(data)
        .linear_asset(1.0)
        .constant_order_latency(0, 0)
        .trading_value_fee_model(0.0, 0.0)
        .tick_size(tick)
        .lot_size(lot)
        .last_trades_capacity(0)
    )
    return HashMapMarketDepthBacktest([hbt_config.apply(asset, entry)])


def compile_loop(hftbacktest, loop, entry: dict) -> float:
    """Compile `loop` (numba compiles on the first call) on a two-row dummy feed; returns the seconds it took.

    Every `HashMapMarketDepthBacktest` has the same numba type whatever its queue model, so one compilation serves
    every model of the process and `run_model`'s `wall_s` is loop time only."""
    rows = np.zeros(2, hftbacktest.event_dtype)
    for i, (side, px) in enumerate(((1 << 29, 100.0), (1 << 28, 100.01))):
        rows[i]["ev"] = (1 << 31) | (1 << 30) | side | 1
        rows[i]["exch_ts"] = rows[i]["local_ts"] = 1
        rows[i]["px"] = px
        rows[i]["qty"] = 1.0
    i64, f64 = np.zeros(0, np.int64), np.zeros(0, np.float64)
    t0 = time.perf_counter()
    hbt = _backtest(rows, 0.01, 1.0, entry)
    loop(hbt, np.array([1], np.int64), i64, i64, i64, i64, f64, f64, i64, 0, np.iinfo(np.int64).max,
         np.zeros(1, np.int64), np.zeros(1, np.int64), np.zeros(1, np.float64), np.zeros(1, np.float64),
         np.zeros(1, np.int64))  # fmt: skip
    hbt.close()
    return time.perf_counter() - t0


def run_model(hftbacktest, loop, data: np.ndarray, meta: dict, acts: dict, entry: dict) -> dict[str, Any]:
    """One hftbacktest run of `entry` over `data` with the sidecar's actions."""

    def backtest():
        return _backtest(data, float(meta["tick_size"]), float(meta["lot_size"]), entry)

    sub = acts["kind"] == KIND_SUBMIT
    n_orders = int(sub.sum())
    # every recorded fill removes at least one lot, except the one that completes a sub-lot residual: an order
    # produces at most ceil(qty / lot) + 1 fill rows
    cap = 1 + int(np.sum(np.ceil(acts["qty"][sub] / float(meta["lot_size"]) - 1e-9) + 1))
    f_oid = np.zeros(cap, np.int64)
    f_ts = np.zeros(cap, np.int64)
    f_qty = np.zeros(cap, np.float64)
    f_left = np.zeros(cap, np.float64)
    f_st = np.zeros(cap, np.int64)
    step_ts = np.unique(data["exch_ts"].astype(np.int64))
    never = np.iinfo(np.int64).max
    precise_after = never
    t0 = time.perf_counter()
    while True:
        hbt = backtest()
        res = loop(
            hbt, step_ts, acts["ts"], acts["kind"], acts["oid"], acts["side"], acts["px"], acts["qty"], acts["tif"],
            n_orders, precise_after, f_oid, f_ts, f_qty, f_left, f_st,
        )  # fmt: skip
        n_fills, n_steps, err_code, err_ts, err_rows, err_from, live, n_live = res
        if err_code == 0 or err_rows <= 1 or precise_after != never:
            break
        # the failing elapse spanned several feed timestamps: replay, stepping every timestamp from its start, so
        # the stop is reported at the failing event's own timestamp
        hbt.close()
        precise_after = int(err_from)
    wall = time.perf_counter() - t0
    orders = hbt.orders(0)
    sid = meta.get("shadow_ids", {})
    fills = [
        {
            "order_id": int(f_oid[k]),
            "shadow_id": sid.get(str(int(f_oid[k]))),
            "ts": int(f_ts[k]),
            "qty": float(f_qty[k]),
            "leaves_after": float(f_left[k]),
            "status": STATUS.get(int(f_st[k]), str(int(f_st[k]))),
        }
        for k in range(n_fills)
    ]
    first: dict[int, int] = {}
    filled: dict[int, float] = {}
    for f in fills:
        first.setdefault(f["order_id"], f["ts"])
        filled[f["order_id"]] = filled.get(f["order_id"], 0.0) + f["qty"]
    out_orders = []
    for oid in range(1, n_orders + 1):
        o = orders.get(oid)
        out_orders.append(
            {
                "order_id": oid,
                "shadow_id": sid.get(str(oid)),
                "status": None if o is None else STATUS.get(int(o.status), str(int(o.status))),
                "leaves_qty": None if o is None else float(o.leaves_qty),
                "filled_qty": filled.get(oid, 0.0),
                "first_fill_ts": first.get(oid),
            }
        )
    error = None
    if err_code != 0:
        error = {"code": int(err_code), "ts": int(err_ts), "live_order_ids": sorted(int(x) for x in live[:n_live])}
    hbt.close()
    return {
        "model_id": entry["model_id"],
        "error": error,
        "fills": fills,
        "orders": out_orders,
        "n_steps": int(n_steps),
        "wall_s": round(wall, 3),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--npz", type=Path)
    ap.add_argument("--orders", type=Path, help="orders sidecar JSON")
    ap.add_argument("--config", type=Path, help="qtint.hbt_queue_config file")
    ap.add_argument("--models", default=None, help="comma list of model ids (default: every model in the config)")
    ap.add_argument("--engine-label", default=None, help="label for the hftbacktest build (default: its version)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--identify", action="store_true", help="print the engine identity as JSON and exit")
    args = ap.parse_args(argv)
    hftbacktest, njit = _import_hbt()
    if args.identify:
        print(json.dumps(engine_identity(), sort_keys=True))
        return 0
    missing = [f"--{k}" for k in ("npz", "orders", "config", "out") if getattr(args, k) is None]
    if missing:
        print(f"QT-USAGE-001: {', '.join(missing)} required (or --identify)", file=sys.stderr)
        return 2
    try:
        cfg = hbt_config.load(args.config)
        entries = hbt_config.select(cfg, args.models.split(",") if args.models else None)
        meta = json.loads(args.orders.read_text())
        acts = load_actions(meta)
    except (hbt_config.ConfigError, ValueError, KeyError, OSError) as e:
        msg = str(e)
        if not msg.startswith("QT-"):
            msg = f"QT-INPUT-002: {args.orders}: {type(e).__name__}: {msg}"
        print(msg, file=sys.stderr)
        return 2
    t0 = time.perf_counter()
    data = np.ascontiguousarray(np.load(args.npz)["data"].astype(hftbacktest.event_dtype))
    npz_load_s = time.perf_counter() - t0
    loop = make_loop(njit)
    compile_s = compile_loop(hftbacktest, loop, entries[0])
    runs = []
    for e in entries:
        r = run_model(hftbacktest, loop, data, meta, acts, e)
        print(
            f"{e['model_id']}: {len(r['fills'])} fills, error {r['error']}, {r['wall_s']} s",
            file=sys.stderr,
        )
        runs.append(r)
    ident = engine_identity()
    doc = {
        "schema": SCHEMA,
        "schema_version": 1,
        "engine": args.engine_label or f"hftbacktest {ident['hftbacktest']}",
        "engine_identity": ident,
        "npz_load_s": round(npz_load_s, 3),
        "compile_s": round(compile_s, 3),
        "npz": args.npz.name,
        "n_rows": len(data),
        "runs": runs,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
