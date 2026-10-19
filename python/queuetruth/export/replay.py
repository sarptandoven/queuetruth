"""Schedule-replay adapters (trader review F11): a canonical live order schedule -> a program that replays it in the
user's own simulator and writes output that `queuetruth.live.import_sim` reads, so "score my backtest against my
fills" is: export the replay, run it, import, `queuetruth score --sim-log`.

Input: the schedule `qt.shadow_spec` (absolute prices; one row per leg, first leg `shadow_id` = `order_key`, later
legs `<order_key>.leg<k>`) plus its `qt.shadow_amend` rows, as written by `queuetruth shadows from-live` or by
`score --out` (`schedule.parquet`, instruction-sized legs, audit A8). With `live` and no schedule, the
instruction-sized schedule is built from the live log as the scorer does (`to_schedule` + `instruction_legs`).
Prices and sizes are in the live log's fixed-point units; the exponents come from `units`, else the live log's
orders, else the dataset's instruments (tick and lot size from `units` or the dataset).

Replay actions, per live order (one simulator order per live order, so the importers link every leg to the same
`order_key`): submit leg 1 at its `ts_submit` (limit; post-only when the live order was post-only/GTX); each later
leg is a MODIFY of that order to the leg's price and quantity at the leg's `ts_submit` (the live amend); each amend
row is a quantity modify at `ts_send`; the last leg's `ts_cancel` is a cancel. Instructions on an order that is no
longer open in the simulator are skipped and counted (a venue would reject them). A leg that starts after the
previous leg ended (a gap) cannot be one order and is refused (`QT-INPUT-002`). `ts_submit` is sent as is: it is the
live exchange ack time (`entry = exchange_ack`, then set the simulator's entry latency to 0) or the local send time
(`send_plus_latency`, then let the simulator's latency model act); the schedule's `latency_ns` is not added.

Targets:
- **hftbacktest 2.4.4** (`hftbacktest_replay_script`): a standalone script. It builds one `BacktestAsset` per
  instrument from `--data` npz files (e.g. `queuetruth export l2-npz`), the model from `--snippet` (a
  `queuetruth export hftbacktest` snippet) or from its `configure_asset` function, steps time with `elapse`, and
  writes one JSON line per order state change (`order_id, order_key, instrument, asset_no, side, price_tick,
  tick_size, qty, leaves_qty, exec_qty, exec_price_tick, status, exch_timestamp, local_timestamp, maker, order_type,
  time_in_force`), the snapshot format of `import_sim("hftbacktest")`, plus a `<out>.summary.json`.
- **NautilusTrader** (`nautilus_replay_strategy`): a module with `ScheduleReplayConfig` / `ScheduleReplayStrategy`
  (generated text; written against NT's documented Strategy API, no NT code copied, LGPL) that replays the actions
  with clock alerts and writes the order events it receives (with `client_order_id` = the live `order_key`) as JSON
  lines for `import_sim("nautilus")`.
"""

from __future__ import annotations

import pprint
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa

from queuetruth._version import __version__
from queuetruth.errors import QtError

Target = Literal["hftbacktest", "nautilus"]
SIDE_BID, SIDE_ASK = 66, 65
#: Default step of the hftbacktest replay while an order is open (fill-time resolution).
DEFAULT_STEP_NS = 10_000_000
#: Default life of a simulator order after its entry (300 s = the calibration h_max default); 0 = unlimited.
DEFAULT_WINDOW_NS = 300_000_000_000
POST_ONLY_TIFS = ("post_only", "gtx")


@dataclass(frozen=True)
class Units:
    """Fixed-point units of one instrument (prices `price * 10^price_exp`, sizes `qty * 10^size_exp`)."""

    price_exp: int
    size_exp: int
    tick_size: str | None = None
    lot_size: str | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class ReplayAction:
    """One instruction of the replay."""

    ts: int
    kind: Literal["submit", "modify", "cancel"]
    order_key: str
    instrument_id: int
    side: Literal["B", "A"]
    price: int
    qty: int
    post_only: bool
    leg: str


@dataclass
class ReplayExport:
    """Result of `replay_schedule`."""

    target: Target
    path: str
    n_orders: int
    n_actions: int
    notes: list[str] = field(default_factory=list)


# ---- schedule -----------------------------------------------------------------------------------------------------


def _rows(src: Any) -> list[dict[str, Any]]:
    """Rows of a Parquet path / Arrow table (timestamps as int ns, dictionaries decoded) or of row dicts."""
    from queuetruth.score._io import read_parquet, rows

    if src is None:
        return []
    if isinstance(src, pa.Table):
        return rows(src)
    if isinstance(src, (str, Path)):
        return rows(read_parquet(src))
    return [dict(r) for r in src]


def _amend_sibling(p: Path) -> Path | None:
    for name in (f"{p.stem}_amends.parquet", "amends.parquet", "schedule_amends.parquet"):
        q = p.with_name(name)
        if q.exists() and q != p:
            return q
    return None


def read_schedule(schedule: Any, amends: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(leg rows, amend rows) of a `qt.shadow_spec` path / table / rows; amends default to the sibling
    `<stem>_amends.parquet` (`shadows from-live`) or `amends.parquet` / `schedule_amends.parquet` next to it."""
    legs = _rows(schedule)
    if amends is None and isinstance(schedule, (str, Path)):
        amends = _amend_sibling(Path(schedule))
    return legs, _rows(amends)


def _side(v: Any, where: str) -> Literal["B", "A"]:
    s = v if isinstance(v, str) else chr(int(v)) if v is not None else ""
    if s in ("B", "b", "buy", "bid"):
        return "B"
    if s in ("A", "a", "S", "sell", "ask"):
        return "A"
    raise QtError("QT-INPUT-002", f"{where}: side {v!r} is not B/A")


def schedule_actions(
    legs: Sequence[Mapping[str, Any]],
    amends: Sequence[Mapping[str, Any]] = (),
    *,
    post_only: Mapping[str, bool] | bool = False,
) -> list[ReplayAction]:
    """Replay actions of a schedule (see the module doc), sorted by (ts, submit < modify < cancel, order_key)."""
    by_key: dict[str, list[Mapping[str, Any]]] = {}
    leg_order: dict[str, str] = {}
    for r in legs:
        sid = str(r["shadow_id"])
        mode = r.get("price_mode") or ("abs" if r.get("price") is not None else "touch")
        if mode != "abs" or r.get("price") is None:
            raise QtError(
                "QT-INPUT-002",
                f"schedule leg {sid}: price_mode {mode!r}; the replay needs absolute prices",
                "replay live schedules (`shadows from-live` or `score --out`); touch-mode shadows need the book at "
                "submit time, use `queuetruth zoo` or `referee` for them",
            )
        key = str(r.get("order_key") or sid)
        by_key.setdefault(key, []).append(r)
        leg_order[sid] = key

    def leg_no(r: Mapping[str, Any]) -> int:
        sid = str(r["shadow_id"])
        tail = sid.rsplit(".leg", 1)
        return int(tail[1]) if len(tail) == 2 and tail[1].isdigit() else 1

    out: list[ReplayAction] = []
    for key, ls in by_key.items():
        ls = sorted(ls, key=lambda r: (int(r["ts_submit"]), leg_no(r)))
        po = post_only if isinstance(post_only, bool) else bool(post_only.get(key, False))
        first = ls[0]
        iid, side = int(first["instrument_id"]), _side(first["side"], str(first["shadow_id"]))
        for k, r in enumerate(ls):
            sid = str(r["shadow_id"])
            if int(r["instrument_id"]) != iid or _side(r["side"], sid) != side:
                raise QtError("QT-INPUT-002", f"schedule leg {sid}: instrument or side differs from {key}'s first leg")
            if k > 0:
                prev_end = ls[k - 1].get("ts_cancel")
                if prev_end is None or int(prev_end) != int(r["ts_submit"]):
                    raise QtError(
                        "QT-INPUT-002",
                        f"schedule leg {sid} starts at {r['ts_submit']} but the previous leg "
                        f"{ls[k - 1]['shadow_id']} ends at {prev_end}: the replay keeps one simulator order per live "
                        "order and needs contiguous legs",
                        "regenerate the schedule with `queuetruth shadows from-live` or take `schedule.parquet` from "
                        "`score --out`",
                    )
            kind: Literal["submit", "modify"] = "submit" if k == 0 else "modify"
            out.append(ReplayAction(int(r["ts_submit"]), kind, key, iid, side, int(r["price"]), int(r["qty"]), po, sid))
        last = ls[-1]
        if last.get("ts_cancel") is not None:
            out.append(
                ReplayAction(
                    int(last["ts_cancel"]),
                    "cancel",
                    key,
                    iid,
                    side,
                    int(last["price"]),
                    int(last["qty"]),
                    po,
                    str(last["shadow_id"]),
                )
            )
    legs_by_id = {str(r["shadow_id"]): r for r in legs}
    for a in amends:
        sid = str(a["shadow_id"])
        leg = legs_by_id.get(sid)
        if leg is None:
            raise QtError("QT-INPUT-002", f"amend row for unknown schedule leg {sid}")
        key = leg_order[sid]
        po = post_only if isinstance(post_only, bool) else bool(post_only.get(key, False))
        out.append(
            ReplayAction(
                int(a["ts_send"]),
                "modify",
                key,
                int(leg["instrument_id"]),
                _side(leg["side"], sid),
                int(leg["price"]),
                int(a["new_qty"]),
                po,
                sid,
            )
        )
    rank = {"submit": 0, "modify": 1, "cancel": 2}
    out.sort(key=lambda x: (x.ts, rank[x.kind], x.order_key))
    return out


def _from_live(live: str, entry: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Instruction-sized legs (A8) of a live log, as the scorer builds them."""
    import warnings

    from queuetruth.live import to_schedule
    from queuetruth.score._io import read_live_log, rows
    from queuetruth.score.outcomes import log_outcomes
    from queuetruth.score.schedule import instruction_legs

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sched, am = to_schedule(live, entry=entry, latency_ns=None)
    orders, events = read_live_log(live)
    oc, _ = log_outcomes(orders, events)
    legs, amends, _ = instruction_legs(rows(sched), rows(am), events, oc, entry)
    return legs, amends


# ---- units ----------------------------------------------------------------------------------------------------------


def _dec(x: Any) -> str:
    d = Decimal(str(x)).normalize()
    return format(d, "f")


def scaled(v: int, exp: int) -> str:
    """Exact decimal text of `v * 10^exp`."""
    return _dec(Decimal(int(v)).scaleb(int(exp)))


def resolve_units(
    instrument_ids: Sequence[int],
    *,
    units: Mapping[int, Units | Mapping[str, Any]] | None = None,
    live: str | None = None,
    dataset: str | None = None,
) -> dict[int, Units]:
    """Units per instrument: `units` > the live log's orders (exponents, symbol) > the dataset's instruments
    (exponents, symbol, tick and lot size). Raises `QT-CONFIG-001` when an instrument's exponents are unknown."""
    found: dict[int, dict[str, Any]] = {int(i): {} for i in instrument_ids}
    if dataset is not None:
        p = Path(dataset) / "instruments.parquet"
        if not p.exists():
            raise QtError("QT-INPUT-001", f"{p}: not found")
        for r in _rows(p):
            iid = int(r["instrument_id"])
            if iid not in found:
                continue
            d = found[iid]
            pe, se = int(r.get("price_exp") or 0), int(r.get("size_exp") or 0)
            d.setdefault("price_exp", pe)
            d.setdefault("size_exp", se)
            if r.get("tick_size") is not None:
                d.setdefault("tick_size", scaled(int(r["tick_size"]), pe))
            if r.get("lot_size") is not None:
                d.setdefault("lot_size", scaled(int(r["lot_size"]), se))
            if r.get("symbol"):
                d.setdefault("symbol", str(r["symbol"]))
    if live is not None:
        from queuetruth.score._io import read_live_log

        orders, _ = read_live_log(live)
        for o in orders:
            iid = o.get("instrument_id")
            if iid is None or int(iid) not in found:
                continue
            d = found[int(iid)]
            d["price_exp"], d["size_exp"] = int(o["price_exp"]), int(o["size_exp"])
            if o.get("instrument"):
                d["symbol"] = str(o["instrument"])
    for iid, u in (units or {}).items():
        if int(iid) in found:
            found[int(iid)].update(u.__dict__ if isinstance(u, Units) else dict(u))
    out: dict[int, Units] = {}
    for iid, d in sorted(found.items()):
        if "price_exp" not in d or "size_exp" not in d:
            raise QtError(
                "QT-CONFIG-001",
                f"instrument {iid}: price_exp/size_exp unknown",
                "pass live= (the live log the schedule came from), dataset= or units={id: Units(...)}",
            )
        out[iid] = Units(
            int(d["price_exp"]),
            int(d["size_exp"]),
            None if d.get("tick_size") is None else _dec(d["tick_size"]),
            None if d.get("lot_size") is None else _dec(d["lot_size"]),
            d.get("symbol"),
        )
    return out


def _post_only_of(live: str | None) -> dict[str, bool]:
    if live is None:
        return {}
    from queuetruth.score._io import read_live_log

    orders, _ = read_live_log(live)
    return {str(o["order_key"]): bool(o.get("post_only")) or str(o.get("tif") or "") in POST_ONLY_TIFS for o in orders}


def _prepare(
    schedule: Any, amends: Any, live: str | None, dataset: str | None, units: Any, post_only: Any, entry: str
) -> tuple[list[ReplayAction], dict[int, Units], str]:
    if schedule is None and live is None:
        raise QtError("QT-CONFIG-001", "replay-schedule: a schedule or a live log is required", "--schedule FILE")
    if schedule is None:
        assert live is not None
        legs, am = _from_live(live, entry)
        source = f"live log {live} (instruction-sized legs, entry {entry})"
    else:
        legs, am = read_schedule(schedule, amends)
        source = f"schedule {schedule}" if isinstance(schedule, (str, Path)) else "schedule rows"
    if not legs:
        raise QtError("QT-INPUT-002", f"{source}: no schedulable order")
    po = post_only if post_only is not None else _post_only_of(live)
    actions = schedule_actions(legs, am, post_only=po)
    u = resolve_units(sorted({a.instrument_id for a in actions}), units=units, live=live, dataset=dataset)
    return actions, u, source


def _action_tuples(actions: Sequence[ReplayAction], u: Mapping[int, Units]) -> list[tuple[Any, ...]]:
    return [
        (
            a.ts,
            a.kind,
            a.order_key,
            a.instrument_id,
            a.side,
            scaled(a.price, u[a.instrument_id].price_exp),
            scaled(a.qty, u[a.instrument_id].size_exp),
            a.post_only,
        )
        for a in actions
    ]


def _literal(x: Any) -> str:
    return pprint.pformat(x, width=118, sort_dicts=True)


# ---- hftbacktest -----------------------------------------------------------------------------------------------------

_HBT_TEMPLATE = '''#!/usr/bin/env python3
"""queuetruth {version} | hftbacktest 2.4.4 schedule replay | {n_orders} orders, {n_actions} actions
Source: {source}

Replays a FIXED live order schedule in hftbacktest (no market signals) and writes one JSON line per order state
change in the snapshot format that `queuetruth.live.import_sim("hftbacktest", ...)` reads. Each live order is one
hftbacktest order (`order_key` column = the live order key): leg 1 is submitted at its time, later legs and
priority-keeping amends are `modify` calls, the last leg's cancel is a `cancel`. Instructions for an order that is no
longer open are skipped and counted, as a venue would reject them. Orders still open {window_text} after their entry
are cancelled by the replay (outside the scorer's window W = min(live cancel, entry + H) for H <= that).

Fill times: time advances in steps of --step-ns while any order is open; a snapshot carries the order's exchange
timestamp, exact for the last fill inside a step (several fills inside one step are merged at the later time).
An hftbacktest error code from `elapse` stops the run: the rows so far are kept, the summary names the code and the
script exits with status 3.

Usage (run inside the venv that has hftbacktest 2.4.4):
  python {script} --data ID=PATH[,PATH...] [--data ...] (--snippet hbt_snippet.py | edit configure_asset) \\
      [--tick-size ID=DEC] [--lot-size ID=DEC] [--order-latency ENTRY_NS,RESP_NS] [--step-ns N] [--window-ns N] \\
      --out sim_orders.jsonl
then, in the queuetruth environment:
  python -c "from queuetruth import live; live.import_sim('hftbacktest', ['sim_orders.jsonl'], 'hbt-sim', \\
      venue='<live venue>', instrument_map='instrument_map.csv')"
  queuetruth score --live <live dir> --sim-log hbt-sim --data <dataset> --out sc-hbt
"""

import argparse
import json
import sys
from pathlib import Path

#: instrument_id -> units and asset settings (prices and sizes below are exact decimal strings).
INSTRUMENTS = {instruments}

#: (ts_ns, action, order_key, instrument_id, side B/A, price, qty, post_only), in time order.
ACTIONS = {actions}

OPEN, TERMINAL = (0, 1, 5), (2, 3, 4, 6)  # hftbacktest status: 0 none, 1 new, 5 partially filled; 2-4, 6 final
STEP_NS = {step_ns}
WINDOW_NS = {window_ns}


def configure_asset(asset):
    """EDIT ME (or pass --snippet): add your queue model, exchange model, latency and fees to `asset` (data, tick
    and lot size are already set) and return it, e.g.

        return asset.power_prob_queue_model3(2.0).partial_fill_exchange().constant_order_latency(0, 0)

    Returning None makes the script require --snippet, so a forgotten edit cannot silently pick a model."""
    return None


def _kv(items, what):
    out = {{}}
    for item in items or []:
        k, sep, v = item.partition("=")
        if not sep or not k.strip().isdigit():
            sys.exit(f"{{what}}: expected INSTRUMENT_ID=VALUE, got {{item!r}}")
        out[int(k)] = v
    return out


def _assets(args, hbt_mod):
    data = _kv(args.data, "--data")
    ticks = _kv(args.tick_size, "--tick-size")
    lots = _kv(args.lot_size, "--lot-size")
    snippet = Path(args.snippet).read_text() if args.snippet else None
    assets, asset_no = [], {{}}
    for iid in sorted(INSTRUMENTS):
        info = INSTRUMENTS[iid]
        if iid not in data:
            sys.exit(f"--data {{iid}}=PATH is required (instrument {{iid}}, {{info.get('symbol')}})")
        tick = ticks.get(iid, info.get("tick_size"))
        lot = lots.get(iid, info.get("lot_size"))
        if tick is None or lot is None:
            sys.exit(f"instrument {{iid}}: --tick-size {{iid}}=DEC and --lot-size {{iid}}=DEC are required")
        if snippet is not None:
            ns = {{}}
            exec(compile(snippet, args.snippet, "exec"), ns)
            asset = ns["asset"]
        else:
            asset = hbt_mod.BacktestAsset()
        asset = asset.data(data[iid].split(",")).linear_asset(1.0).tick_size(float(tick)).lot_size(float(lot))
        if snippet is None:
            asset = configure_asset(asset)
            if asset is None:
                sys.exit("choose the fill model: pass --snippet (a `queuetruth export hftbacktest` snippet) or edit "
                         "configure_asset() in this script")
        elif "_latency(" not in snippet:
            entry, resp = (int(x) for x in args.order_latency.split(","))
            asset = asset.constant_order_latency(entry, resp)
        asset_no[iid] = len(assets)
        assets.append(asset)
        info["tick_used"] = str(tick)
    return assets, asset_no


def main(argv=None):
    p = argparse.ArgumentParser(description="queuetruth schedule replay for hftbacktest 2.4.4")
    p.add_argument("--data", action="append", required=True, help="INSTRUMENT_ID=PATH[,PATH...] (npz event data)")
    p.add_argument("--snippet", help="file defining `asset` (a `queuetruth export hftbacktest` snippet)")
    p.add_argument("--tick-size", action="append", help="INSTRUMENT_ID=DECIMAL (overrides the embedded value)")
    p.add_argument("--lot-size", action="append", help="INSTRUMENT_ID=DECIMAL (overrides the embedded value)")
    p.add_argument("--order-latency", default="0,0", help="ENTRY_NS,RESP_NS when the snippet sets no latency")
    p.add_argument("--step-ns", type=int, default=STEP_NS, help="time step while an order is open")
    p.add_argument("--window-ns", type=int, default=WINDOW_NS, help="cancel orders still open this long (0 = never)")
    p.add_argument("--out", required=True, help="output JSON lines (order snapshots)")
    args = p.parse_args(argv)
    import hftbacktest as hbt_mod

    assets, asset_no = _assets(args, hbt_mod)
    hbt = hbt_mod.HashMapMarketDepthBacktest(assets)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    counts = {{"submitted": 0, "modified": 0, "cancelled": 0, "window_cancels": 0, "rows": 0, "late_actions": 0,
              "max_late_ns": 0, "skipped": {{}}, "errors": {{}}, "end_of_data": False}}
    ids, key_of, entry_ts, last = {{}}, {{}}, {{}}, {{}}
    next_id = [1]
    state = {{"end": False}}

    def skip(reason):
        counts["skipped"][reason] = counts["skipped"].get(reason, 0) + 1

    def err(what, rc):
        k = f"{{what}} rc={{rc}}"
        counts["errors"][k] = counts["errors"].get(k, 0) + 1

    def orders_of(a):
        vals = hbt.orders(a).values()
        while vals.has_next():
            yield vals.get()

    def maker_of(o):
        try:
            return bool(o.arr[0]["maker"])
        except Exception:  # noqa: BLE001 - hftbacktest builds without the field
            return None

    def snapshot(fh):
        any_open = False
        for iid, a in asset_no.items():
            info = INSTRUMENTS[iid]
            for o in orders_of(a):
                oid = int(o.order_id)
                st = int(o.status)
                if st in OPEN:
                    any_open = True
                if int(o.req) != 0:
                    continue  # request in flight: record the exchange-confirmed state only
                sig = (st, float(o.qty), float(o.leaves_qty), int(o.price_tick), int(o.exch_timestamp))
                if last.get((a, oid)) == sig:
                    continue
                last[(a, oid)] = sig
                row = {{
                    "order_id": oid, "order_key": key_of.get((a, oid)),
                    "instrument": info.get("symbol") or f"asset{{a}}",
                    "asset_no": a, "side": int(o.side), "price_tick": int(o.price_tick), "tick_size": info["tick_used"],
                    "qty": float(o.qty), "leaves_qty": float(o.leaves_qty), "exec_qty": float(o.exec_qty),
                    "exec_price_tick": int(o.exec_price_tick), "status": st, "exch_timestamp": int(o.exch_timestamp),
                    "local_timestamp": int(o.local_timestamp), "maker": maker_of(o), "order_type": int(o.order_type),
                    "time_in_force": int(o.time_in_force),
                }}
                fh.write(json.dumps(row, sort_keys=True) + "\\n")
                counts["rows"] += 1
            hbt.clear_inactive_orders(a)
        return any_open

    def is_open(a, oid):
        o = hbt.orders(a).get(oid)
        return o is not None and int(o.status) in (1, 5), o

    def wait_idle(a, oid, o):
        if o is not None and int(o.req) != 0:
            hbt.wait_order_response(a, oid, 5_000_000_000)

    def expire_old(fh):
        if args.window_ns <= 0:
            return
        now = hbt.current_timestamp
        for (a, oid), t0 in list(entry_ts.items()):
            if now - t0 >= args.window_ns:
                ok, o = is_open(a, oid)
                if ok and int(o.req) == 0:
                    rc = hbt.cancel(a, oid, False)
                    if rc == 0:
                        counts["window_cancels"] += 1
                    else:
                        err("window_cancel", rc)
                entry_ts.pop((a, oid), None)

    def fatal(rc):
        state["end"] = True
        counts["fatal"] = {{
            "rc": int(rc), "at_ns": int(hbt.current_timestamp),
            "hint": "hftbacktest stopped with this error code; in 2.4.4 a partial fill equal to the order's leaves can "
                    "raise InvalidOrderStatus (#312/#316): try no_partial_fill_exchange",
        }}

    def advance(fh, t):
        while not state["end"] and hbt.current_timestamp < t:
            any_open = snapshot(fh)
            d = t - hbt.current_timestamp
            if any_open:
                d = min(d, args.step_ns)
                if args.window_ns > 0 and entry_ts:
                    d = min(d, max(1, min(entry_ts.values()) + args.window_ns - hbt.current_timestamp))
            rc = hbt.elapse(int(max(d, 1)))
            if rc == 1:
                state["end"] = True
                counts["end_of_data"] = True
            elif rc != 0:
                fatal(rc)
            expire_old(fh)
        snapshot(fh)

    with out.open("w", encoding="utf-8") as fh:
        rc0 = hbt.elapse(0)
        if rc0 == 1:
            state["end"] = True
        elif rc0 != 0:
            fatal(rc0)
        for ts, kind, key, iid, side, price, qty, post_only in ACTIONS:
            advance(fh, ts)
            if state["end"]:
                skip(f"{{kind}}_after_end_of_data")
                continue
            late = hbt.current_timestamp - ts
            if late > 0:
                counts["late_actions"] += 1
                counts["max_late_ns"] = max(counts["max_late_ns"], late)
            a = asset_no[iid]
            if kind == "submit":
                oid = next_id[0]
                next_id[0] += 1
                ids[key] = (a, oid)
                key_of[(a, oid)] = key
                tif = hbt_mod.GTX if post_only else hbt_mod.GTC
                fn = hbt.submit_buy_order if side == "B" else hbt.submit_sell_order
                rc = fn(a, oid, float(price), float(qty), tif, hbt_mod.LIMIT, False)
                if rc != 0:
                    err("submit", rc)
                    continue
                counts["submitted"] += 1
                entry_ts[(a, oid)] = hbt.current_timestamp
                continue
            if key not in ids:
                skip(f"{{kind}}_unknown_order")
                continue
            a, oid = ids[key]
            ok, o = is_open(a, oid)
            wait_idle(a, oid, o)
            snapshot(fh)
            ok, o = is_open(a, oid)
            if not ok:
                skip(f"{{kind}}_order_not_open")
                continue
            if kind == "modify":
                rc = hbt.modify(a, oid, float(price), float(qty), False)
                if rc != 0:
                    err("modify", rc)
                else:
                    counts["modified"] += 1
            else:
                rc = hbt.cancel(a, oid, False)
                if rc != 0:
                    err("cancel", rc)
                else:
                    counts["cancelled"] += 1
                    entry_ts.pop((a, oid), None)
        if args.window_ns > 0:
            horizon = max([t for t in entry_ts.values()] + [hbt.current_timestamp]) + args.window_ns + 1
        else:
            horizon = (1 << 63) - 1  # no window: run open orders to the end of the data
        while not state["end"] and entry_ts and hbt.current_timestamp < horizon:
            advance(fh, min(horizon, hbt.current_timestamp + 1_000_000_000))
            for k in [k for k in entry_ts if not is_open(*k)[0]]:
                entry_ts.pop(k, None)
        for _ in range(1000):  # let requests in flight (e.g. window cancels) reach the exchange and come back
            pending = [1 for a in asset_no.values() for o in orders_of(a) if int(o.req) != 0]
            if state["end"] or not pending:
                break
            advance(fh, hbt.current_timestamp + args.step_ns)
        snapshot(fh)
    hbt.close()
    Path(str(out) + ".summary.json").write_text(json.dumps(counts, indent=2, sort_keys=True) + "\\n")
    print(json.dumps({{"out": str(out), **{{k: counts[k] for k in ("submitted", "modified", "cancelled", "rows")}}}}))
    if "fatal" in counts:
        print(f"hftbacktest error {{counts['fatal']['rc']}}: {{counts['fatal']['hint']}}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _render_hbt(
    actions: Sequence[ReplayAction], u: Mapping[int, Units], source: str, step_ns: int, window_ns: int, script: str
) -> str:
    inst = {
        iid: {
            "price_exp": x.price_exp,
            "size_exp": x.size_exp,
            "tick_size": x.tick_size,
            "lot_size": x.lot_size,
            "symbol": x.symbol,
        }
        for iid, x in u.items()
    }
    window_text = "never (window disabled)" if window_ns <= 0 else f"{window_ns} ns"
    return _HBT_TEMPLATE.format(
        version=__version__,
        n_orders=len({a.order_key for a in actions}),
        n_actions=len(actions),
        source=source,
        window_text=window_text,
        script=script,
        instruments=_literal(inst),
        actions=_literal(_action_tuples(actions, u)),
        step_ns=int(step_ns),
        window_ns=int(window_ns),
    )


def hftbacktest_replay_script(
    schedule: Any = None,
    *,
    amends: Any = None,
    live: str | None = None,
    dataset: str | None = None,
    units: Mapping[int, Units | Mapping[str, Any]] | None = None,
    post_only: Mapping[str, bool] | bool | None = None,
    entry: str = "exchange_ack",
    step_ns: int = DEFAULT_STEP_NS,
    window_ns: int = DEFAULT_WINDOW_NS,
    script_name: str = "hbt_replay.py",
) -> str:
    """Standalone hftbacktest 2.4.4 replay script for a schedule (see the module doc)."""
    actions, u, source = _prepare(schedule, amends, live, dataset, units, post_only, entry)
    return _render_hbt(actions, u, source, step_ns, window_ns, script_name)


# ---- NautilusTrader --------------------------------------------------------------------------------------------------

_NT_TEMPLATE = '''"""queuetruth {version} | NautilusTrader schedule replay | {n_orders} orders, {n_actions} actions
Source: {source}

Generated configuration/code for NautilusTrader, written against its documented Strategy API (develop 5247ad5:
Strategy, StrategyConfig, clock.set_time_alert_ns, order_factory.limit, submit_order / modify_order / cancel_order,
on_order_event). Verify the names for your installed version. No NautilusTrader code is copied (LGPL).

`ScheduleReplayStrategy` ignores market data and replays a FIXED live order schedule: leg 1 of each live order is a
limit order submitted at its time (post-only when the live order was), later legs and priority-keeping amends are
`modify_order` calls, the last leg's cancel is `cancel_order`; instructions for an order that is no longer open are
skipped and counted. It records the order events it receives as JSON lines with `client_order_id` = the LIVE order
key (Nautilus's own client order id stays internal, so no id-format constraint applies), in the event-row format of
`queuetruth.live.import_sim("nautilus", ...)`.

Use (with your venue, data, fill model and latency model already on the engine):

    from qt_replay_strategy import ScheduleReplayConfig, ScheduleReplayStrategy
    engine.add_strategy(ScheduleReplayStrategy(ScheduleReplayConfig(events_path="nt_events.jsonl")))
    engine.run()      # the strategy writes nt_events.jsonl when it stops

then, in the queuetruth environment:

    from queuetruth import live
    live.import_sim("nautilus", ["nt_events.jsonl"], "nt-sim", venue="<live venue>",
                    instrument_map="instrument_map.csv")
    # queuetruth score --live <live dir> --sim-log nt-sim --data <dataset> --out sc-nautilus

Instrument ids: INSTRUMENT_IDS maps the schedule's instrument_id to a Nautilus InstrumentId string; override it with
`ScheduleReplayConfig(instrument_ids={{"1": "BTCUSDT-PERP.BINANCE"}})`.
"""

import json
from decimal import Decimal
from pathlib import Path

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

#: schedule instrument_id -> Nautilus InstrumentId string.{iid_note}
INSTRUMENT_IDS = {instrument_ids}

#: (ts_ns, action, order_key, instrument_id, side B/A, price, qty, post_only), in time order.
ACTIONS = {actions}

#: Nautilus order events written for queuetruth (others are not needed by the importer).
RECORDED = (
    "OrderSubmitted", "OrderAccepted", "OrderRejected", "OrderDenied", "OrderCanceled", "OrderExpired",
    "OrderUpdated", "OrderFilled", "OrderCancelRejected", "OrderModifyRejected",
)


class ScheduleReplayConfig(StrategyConfig, frozen=True):
    """Where to write the events; optional instrument id overrides (`{{"<instrument_id>": "SYMBOL.VENUE"}}`)."""

    events_path: str = "qt_nautilus_events.jsonl"
    instrument_ids: dict[str, str] | None = None


class ScheduleReplayStrategy(Strategy):
    """Replays ACTIONS; records order events for queuetruth."""

    def __init__(self, config: ScheduleReplayConfig) -> None:
        super().__init__(config)
        self._ids = {{**{{str(k): v for k, v in INSTRUMENT_IDS.items()}}, **(config.instrument_ids or {{}})}}
        self._next = 0
        self._cid = {{}}  # live order_key -> ClientOrderId
        self._key = {{}}  # ClientOrderId value -> live order_key
        self._rows = []
        self.skipped = {{}}

    # -- schedule ------------------------------------------------------------------------------------------
    def on_start(self) -> None:
        self._run_due()

    def _run_due(self, event=None) -> None:
        now = self.clock.timestamp_ns()
        while self._next < len(ACTIONS) and ACTIONS[self._next][0] <= now:
            self._do(ACTIONS[self._next], now)
            self._next += 1
        if self._next < len(ACTIONS):
            when = max(ACTIONS[self._next][0], now + 1)
            self.clock.set_time_alert_ns(name=f"qt-replay-{{self._next}}", alert_time_ns=when, callback=self._run_due)

    def _skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def _do(self, action, now: int) -> None:
        ts, kind, key, iid, side, price, qty, post_only = action
        instrument_id = InstrumentId.from_str(self._ids[str(iid)])
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self._skip(f"{{kind}}_unknown_instrument")
            return
        if kind == "submit":
            order = self.order_factory.limit(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY if side == "B" else OrderSide.SELL,
                quantity=instrument.make_qty(Decimal(qty)),
                price=instrument.make_price(Decimal(price)),
                time_in_force=TimeInForce.GTC,
                post_only=bool(post_only),
            )
            self._cid[key] = order.client_order_id
            self._key[order.client_order_id.value] = key
            self._rows.append(
                {{
                    "event_type": "OrderInitialized", "client_order_id": key, "instrument_id": str(instrument_id),
                    "side": "BUY" if side == "B" else "SELL", "order_type": "LIMIT", "quantity": qty,
                    "price": price, "time_in_force": "GTC", "post_only": bool(post_only), "reduce_only": False,
                    "ts_event": now,
                }}
            )
            self.submit_order(order)
            return
        cid = self._cid.get(key)
        order = self.cache.order(cid) if cid is not None else None
        if order is None or not order.is_open:
            self._skip(f"{{kind}}_order_not_open")
            return
        if kind == "modify":
            self.modify_order(
                order, quantity=instrument.make_qty(Decimal(qty)), price=instrument.make_price(Decimal(price))
            )
        else:
            self.cancel_order(order)

    # -- events --------------------------------------------------------------------------------------------
    def on_order_event(self, event) -> None:
        name = type(event).__name__
        if name not in RECORDED:
            return
        cid = event.client_order_id.value
        row = {{"event_type": name, "client_order_id": self._key.get(cid, cid), "ts_event": int(event.ts_event)}}
        voi = getattr(event, "venue_order_id", None)
        if voi is not None:
            row["venue_order_id"] = str(voi)
        if name == "OrderFilled":
            row.update(
                trade_id=str(event.trade_id), order_side=event.order_side.name, last_qty=str(event.last_qty),
                last_px=str(event.last_px), liquidity_side=event.liquidity_side.name,
                commission=str(event.commission) if getattr(event, "commission", None) is not None else None,
            )
        elif name == "OrderUpdated":
            row.update(quantity=str(event.quantity), price=None if event.price is None else str(event.price))
        elif name in ("OrderRejected", "OrderDenied", "OrderCancelRejected", "OrderModifyRejected"):
            row.update(
                reason=str(getattr(event, "reason", "")), due_post_only=bool(getattr(event, "due_post_only", False))
            )
        self._rows.append(row)

    def on_stop(self) -> None:
        p = Path(self.config.events_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            for r in self._rows:
                fh.write(json.dumps(r, sort_keys=True) + "\\n")
        Path(str(p) + ".summary.json").write_text(
            json.dumps({{"actions_done": self._next, "actions": len(ACTIONS), "skipped": self.skipped}}, indent=2)
            + "\\n"
        )
'''


def _render_nt(
    actions: Sequence[ReplayAction],
    u: Mapping[int, Units],
    source: str,
    instrument_ids: Mapping[int, str] | None,
    nautilus_venue: str,
) -> str:
    ids: dict[int, str] = {}
    guessed = []
    for iid, x in u.items():
        if instrument_ids and iid in instrument_ids:
            ids[iid] = str(instrument_ids[iid])
        else:
            ids[iid] = f"{x.symbol or f'INSTRUMENT{iid}'}.{nautilus_venue}"
            guessed.append(iid)
    note = f" Guessed from the symbol for instrument(s) {', '.join(map(str, guessed))}: check them." if guessed else ""
    return _NT_TEMPLATE.format(
        version=__version__,
        n_orders=len({a.order_key for a in actions}),
        n_actions=len(actions),
        source=source,
        iid_note=note,
        instrument_ids=_literal({str(k): v for k, v in ids.items()}),
        actions=_literal(_action_tuples(actions, u)),
    )


def nautilus_replay_strategy(
    schedule: Any = None,
    *,
    amends: Any = None,
    live: str | None = None,
    dataset: str | None = None,
    units: Mapping[int, Units | Mapping[str, Any]] | None = None,
    post_only: Mapping[str, bool] | bool | None = None,
    entry: str = "exchange_ack",
    instrument_ids: Mapping[int, str] | None = None,
    nautilus_venue: str = "SIM",
) -> str:
    """NautilusTrader strategy module text replaying a schedule (see the module doc). Instrument ids default to
    `<symbol>.<nautilus_venue>`."""
    actions, u, source = _prepare(schedule, amends, live, dataset, units, post_only, entry)
    return _render_nt(actions, u, source, instrument_ids, nautilus_venue)


def replay_schedule(
    target: Target,
    out: str | Path,
    *,
    schedule: Any = None,
    amends: Any = None,
    live: str | None = None,
    dataset: str | None = None,
    units: Mapping[int, Units | Mapping[str, Any]] | None = None,
    post_only: Mapping[str, bool] | bool | None = None,
    entry: str = "exchange_ack",
    step_ns: int = DEFAULT_STEP_NS,
    window_ns: int = DEFAULT_WINDOW_NS,
    instrument_ids: Mapping[int, str] | None = None,
    nautilus_venue: str = "SIM",
) -> ReplayExport:
    """Writes the replay program for `target` to `out` (CLI request: `queuetruth export replay-schedule --target
    hftbacktest|nautilus --schedule FILE [--live DIR] [--data DIR] --out FILE`)."""
    if target not in ("hftbacktest", "nautilus"):
        raise QtError("QT-CONFIG-001", f"replay-schedule target must be hftbacktest or nautilus, got {target!r}")
    p = Path(out)
    actions, u, source = _prepare(schedule, amends, live, dataset, units, post_only, entry)
    if target == "hftbacktest":
        text = _render_hbt(actions, u, source, step_ns, window_ns, p.name)
        notes = [
            f"run (hftbacktest 2.4.4 venv): python {p.name} --data ID=PATH --snippet hbt_snippet.py --out "
            "sim_orders.jsonl",
            "import: queuetruth.live.import_sim('hftbacktest', ['sim_orders.jsonl'], OUT, venue=<live venue>)",
        ]
    else:
        text = _render_nt(actions, u, source, instrument_ids, nautilus_venue)
        notes = [
            "add ScheduleReplayStrategy to your BacktestEngine; it writes the events file when it stops",
            "import: queuetruth.live.import_sim('nautilus', [events.jsonl], OUT, venue=<live venue>)",
            "generated against NautilusTrader's documented API (develop 5247ad5); verify names for your version",
        ]
    missing = [iid for iid, x in u.items() if target == "hftbacktest" and (x.tick_size is None or x.lot_size is None)]
    if missing:
        notes.append(f"tick/lot size unknown for instrument(s) {missing}: pass --tick-size/--lot-size to the script")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return ReplayExport(target, str(p), len({a.order_key for a in actions}), len(actions), notes)


__all__ = [
    "DEFAULT_STEP_NS",
    "DEFAULT_WINDOW_NS",
    "ReplayAction",
    "ReplayExport",
    "Units",
    "hftbacktest_replay_script",
    "nautilus_replay_strategy",
    "read_schedule",
    "replay_schedule",
    "resolve_units",
    "scaled",
    "schedule_actions",
]
