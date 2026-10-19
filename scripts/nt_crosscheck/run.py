#!/usr/bin/env python3
"""Cross-check queuetruth's NautilusTrader-style zoo models against the real `nautilus_trader` package.

queuetruth's `nt_prob_touch[p]`, `nt_queue_l2` and `nt_queue_l2_prob_touch[p]` re-implement NautilusTrader fill
semantics from its documentation (SPEC 5.3.3 d-f). This harness replays the same L2 tape (queuetruth's own L2 view,
`synthesize_l2`) through NautilusTrader's `BacktestEngine` with the same passive limit orders as the zoo's shadows
(same instrument, side, price, size and live time; GTC; no cancel inside the horizon) and compares per-order fill
outcomes and fill rates by horizon.

Three steps, each in the interpreter that has its dependencies (`all` chains them):

  prepare   (queuetruth venv)  L2 tape (`synthesize_l2`), zoo run of the nt_* models (faithful), instrument
                               table, and the shadows with the price the zoo resolved (from the tape, checked
                               against the zoo's `visible_level_qty_at_live`).
  nautilus  (NautilusTrader venv; imports pyarrow, numpy and nautilus_trader only, never queuetruth)
                               one BacktestEngine run per chunk of shadows, per config:
                                 touch_p1        FillModel(prob_fill_on_limit=1.0)           (NT default)
                                 touch_p0.29     FillModel(prob_fill_on_limit=0.29)
                                 queue_p1        queue_position=True, prob_fill_on_limit=1.0
                                 queue_p0.29     queue_position=True, prob_fill_on_limit=0.29
                               with `--depth10` also the same configs fed OrderBookDepth10 (MBP-10 snapshots)
                               instead of OrderBookDelta.
  compare   (queuetruth venv)  agreement table per model/horizon, discrepancy classes with examples, the
                               headline bias under real NautilusTrader, and replicas of both rule sets that
                               attribute every disagreement to one rule difference.

Isolation: NautilusTrader shares a trade print's volume between the strategy's own resting orders when
`queue_position` (or `liquidity_consumption`) is on, while the zoo treats every shadow independently. Shadows are
therefore assigned to lanes in which windows never overlap; each window starts with a CLEAR + F_SNAPSHOT book of the
L2 view as of the shadow's live time (queuetruth's activation rule: every row with ts <= ts_live applied), the order is
submitted by a time alert at ts_live, and cancelled at ts_live + window + 1 ns.

NautilusTrader is LGPL-3.0: this script only calls its public Python API; no NautilusTrader source is copied.

Usage (paths relative to the repository root):
  .venv/bin/python scripts/nt_crosscheck/run.py all --work .scratch/nt/work-d1 \
      --dataset .realdata/ds/nasdaq-2019-12-30 --shadows .realdata/shadows/d1-sampled-3000.parquet \
      --nt-python .scratch/nt/venv/bin/python
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]

HORIZONS_NS = [100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000]
HNAME = {100_000_000: "0.1s", 1_000_000_000: "1s", 10_000_000_000: "10s", 60_000_000_000: "60s"}
WINDOW_NS = 60_000_000_000
L2_CFG = {
    "depth_levels": 10,
    "cadence_ns": 0,
    "trade_position": "before",
    "include_counts": True,
    "emit_hidden_trades": True,
}
P_LOW = 0.29
CONFIGS = {
    # name: (queue_position, prob_fill_on_limit, depth10 input)
    "touch_p1": (False, 1.0, False),
    f"touch_p{P_LOW:g}": (False, P_LOW, False),
    "queue_p1": (True, 1.0, False),
    f"queue_p{P_LOW:g}": (True, P_LOW, False),
}
DEPTH10_CONFIGS = {
    "d10_touch_p1": (False, 1.0, True),
    "d10_queue_p1": (True, 1.0, True),
}
ZOO_MODELS = [
    "nt_prob_touch[p=1]",
    f"nt_prob_touch[p={P_LOW:g}]",
    "nt_queue_l2",
    "nt_queue_l2_prob_touch[p=1]",
    f"nt_queue_l2_prob_touch[p={P_LOW:g}]",
]
# NT config -> zoo model it is compared with (faithful mode)
PAIRS = {
    "touch_p1": "nt_prob_touch[mode=faithful;p=1]",
    f"touch_p{P_LOW:g}": f"nt_prob_touch[mode=faithful;p={P_LOW:g}]",
    "queue_p1": "nt_queue_l2[mode=faithful]",
    f"queue_p{P_LOW:g}": f"nt_queue_l2_prob_touch[mode=faithful;p={P_LOW:g}]",
    "d10_touch_p1": "nt_prob_touch[mode=faithful;p=1]",
    "d10_queue_p1": "nt_queue_l2[mode=faithful]",
}
# further zoo models compared with an NT config (report["extra_pairs"]); nt_queue_l2_prob_touch at p = 1 is the
# queue model with certain fills, so NautilusTrader's queue_position=True at prob_fill_on_limit = 1 is its counterpart
EXTRA_PAIRS = [("queue_p1", "nt_queue_l2_prob_touch[mode=faithful;p=1]")]

# qt.l2 encodings
K_DEPTH, K_SNAP, K_TRADE, K_CLEAR = 0, 1, 2, 3
KIND_CODE = {"depth": K_DEPTH, "snapshot": K_SNAP, "trade": K_TRADE, "clear": K_CLEAR}
S_BID, S_ASK, S_NONE = 66, 65, 78
F_LAST_IN_BATCH, F_OUT_OF_VIEW, F_HIDDEN = 1, 2, 32


# ---------------------------------------------------------------------------------------------------------------
# tape helpers (pyarrow + numpy only; shared by every step)
# ---------------------------------------------------------------------------------------------------------------


@dataclass
class Tape:
    ts: np.ndarray  # int64 ns (ts_event)
    event: np.ndarray  # uint64 event_id
    kind: np.ndarray  # int8 K_*
    side: np.ndarray  # uint8 level side / hit side
    price: np.ndarray  # int64 (0 where null)
    qty: np.ndarray  # int64 new level qty (depth/snapshot) or trade qty
    aggressor: np.ndarray  # uint8
    flags: np.ndarray  # uint8

    def __len__(self) -> int:
        return len(self.ts)


def tape_path(work: Path, iid: int) -> Path:
    hits = sorted((work / "l2").rglob(f"instrument_id={iid}/*.parquet"))
    if len(hits) != 1:
        raise SystemExit(f"expected one L2 partition for instrument {iid} under {work / 'l2'}, found {hits}")
    return hits[0]


def load_tape(path: Path) -> Tape:
    t = pq.read_table(path)
    kinds = pc.cast(t["kind"], pa.string()).to_pylist()
    kind = np.fromiter((KIND_CODE[k] for k in kinds), dtype=np.int8, count=len(kinds))
    is_trade = kind == K_TRADE
    new_qty = t["new_qty"].to_numpy().astype(np.int64)
    trade_qty = t["trade_qty"].to_numpy().astype(np.int64)
    return Tape(
        ts=pc.cast(t["ts_event"], pa.int64()).to_numpy(),
        event=t["event_id"].to_numpy(),
        kind=kind,
        side=t["side"].to_numpy(),
        price=pc.fill_null(t["price"], 0).to_numpy(),
        qty=np.where(is_trade, trade_qty, new_qty),
        aggressor=t["aggressor"].to_numpy(),
        flags=t["flags"].to_numpy(),
    )


def trade_hit_side(side: int, aggressor: int) -> int | None:
    """Hit (resting) side of a displayed trade: the row side, else the opposite of the aggressor (zoo rule)."""
    if side in (S_BID, S_ASK):
        return side
    if aggressor == S_BID:
        return S_ASK
    if aggressor == S_ASK:
        return S_BID
    return None


class View:
    """The L2 view (what queuetruth's zoo sees): price -> qty per side, top-N rows applied as `set`."""

    def __init__(self) -> None:
        self.bid: dict[int, int] = {}
        self.ask: dict[int, int] = {}

    def apply(self, tp: Tape, i: int) -> None:
        k = tp.kind[i]
        if k == K_CLEAR:
            self.bid.clear()
            self.ask.clear()
        elif k == K_DEPTH or k == K_SNAP:
            book = self.bid if tp.side[i] == S_BID else self.ask if tp.side[i] == S_ASK else None
            if book is None:
                return
            q = int(tp.qty[i])
            p = int(tp.price[i])
            if q == 0:
                book.pop(p, None)
            else:
                book[p] = q

    def best_bid(self) -> int | None:
        return max(self.bid) if self.bid else None

    def best_ask(self) -> int | None:
        return min(self.ask) if self.ask else None

    def snapshot(self) -> tuple[dict[int, int], dict[int, int]]:
        return dict(self.bid), dict(self.ask)


def batch_ends(tp: Tape) -> np.ndarray:
    """Boolean per row: the row closes a zoo batch (LAST_IN_BATCH, or the next row starts another event)."""
    n = len(tp)
    end = (tp.flags & F_LAST_IN_BATCH) != 0
    if n:
        nxt = np.empty(n, dtype=bool)
        nxt[:-1] = tp.event[1:] != tp.event[:-1]
        nxt[-1] = True
        end = end | nxt
    return end


# ---------------------------------------------------------------------------------------------------------------
# prepare (queuetruth venv)
# ---------------------------------------------------------------------------------------------------------------


def cmd_prepare(a: argparse.Namespace) -> None:
    import queuetruth as qt
    from queuetruth import _core

    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if not (work / "l2").exists():
        _core.synthesize_l2(
            json.dumps(
                {"dataset": {"path": str(a.dataset)}, "out": str(work / "l2"), "threads": a.threads, "l2": L2_CFG}
            )
        )
    print(f"[prepare] L2 tape ready ({time.time() - t0:.1f}s)", flush=True)

    shadows_path = Path(a.shadows) if a.shadows else None
    if shadows_path is None:
        shadows_path = work / "shadows_sampled.parquet"
        if not shadows_path.exists():
            qt.sample_shadows(
                {"dataset": {"path": str(a.dataset)}, "out": str(shadows_path), "n": a.n_shadows, "seed": 0}
            )
    zoo_dir = work / "zoo"
    if not (zoo_dir / "zoo_predictions.parquet").exists():
        qt.run_zoo(
            {
                "dataset": {"path": str(a.dataset)},
                "shadows": {"path": str(shadows_path)},
                "out": str(zoo_dir),
                "models": ZOO_MODELS,
                "mode": "faithful",
                "exchange": "partial",
                "horizons_ns": HORIZONS_NS,
                "h_max_ns": 300_000_000_000,
                "l2": L2_CFG,
                "write_fills": True,
                "threads": a.threads,
            }
        )
    print(f"[prepare] zoo done ({time.time() - t0:.1f}s)", flush=True)

    inst = pq.read_table(Path(a.dataset) / "instruments.parquet").to_pylist()
    instruments = {
        str(r["instrument_id"]): {
            "symbol": r["symbol"],
            "price_exp": r["price_exp"],
            "size_exp": r["size_exp"],
            "tick_size": r["tick_size"],
            "lot_size": r["lot_size"],
            "currency": r["currency"],
        }
        for r in inst
    }
    (work / "instruments.json").write_text(json.dumps(instruments, indent=1))

    spec = pq.read_table(shadows_path).to_pylist()
    zs = {r["shadow_id"]: r for r in pq.read_table(zoo_dir / "zoo_shadows.parquet").to_pylist()}
    preds = pq.read_table(zoo_dir / "zoo_predictions.parquet").to_pandas()
    p1 = preds[(preds.model_id == PAIRS["touch_p1"]) & (preds.horizon_ns == HORIZONS_NS[0])].set_index("shadow_id")
    by_inst: dict[int, list[dict]] = {}
    for r in spec:
        z = zs.get(r["shadow_id"])
        if z is None:
            continue
        by_inst.setdefault(r["instrument_id"], []).append(
            {**r, "ts_live_ns": int(z["ts_live"].value), "end": z["exact_end_state"]}
        )

    rows = []
    for iid, items in sorted(by_inst.items()):
        tick = int(instruments[str(iid)]["tick_size"])
        tp = load_tape(tape_path(work, iid))
        items.sort(key=lambda r: r["ts_live_ns"])
        view = View()
        j = 0
        for r in items:
            while j < len(tp) and tp.ts[j] <= r["ts_live_ns"]:
                view.apply(tp, j)
                j += 1
            side = r["side"]
            same = view.best_bid() if side == S_BID else view.best_ask()
            opp = view.best_ask() if side == S_BID else view.best_bid()
            px = None
            if r["price_mode"] == "abs":
                px = int(r["price"])
            elif same is not None:
                off = int(r["offset_ticks"]) * tick
                px = same + off if side == S_BID else same - off
                if (
                    r["offset_ticks"] > 0
                    and opp is not None
                    and ((side == S_BID and px >= opp) or (side == S_ASK and px <= opp))
                ):
                    px = None  # resolve_price: an improving offset that would cross is rejected
            crosses = (
                px is not None and opp is not None and ((side == S_BID and px >= opp) or (side == S_ASK and px <= opp))
            )
            book = view.bid if side == S_BID else view.ask
            qv = book.get(px, 0) if px is not None else None
            zrow = p1.loc[r["shadow_id"]] if r["shadow_id"] in p1.index else None
            rows.append(
                {
                    "shadow_id": r["shadow_id"],
                    "instrument_id": iid,
                    "side": "B" if side == S_BID else "A",
                    "price": px,
                    "qty": int(r["qty"]),
                    "ts_live": r["ts_live_ns"],
                    "crosses_at_live": bool(crosses),
                    "exact_end_state": r["end"],
                    "q_view": qv,
                    "zoo_visible_qty": None if zrow is None else int(zrow["visible_level_qty_at_live"]),
                }
            )
    sh = pa.Table.from_pylist(rows)
    pq.write_table(sh, work / "shadows.parquet")
    ok = [r for r in rows if r["price"] is not None and not r["crosses_at_live"]]
    mism = [r for r in ok if r["zoo_visible_qty"] is not None and r["q_view"] != r["zoo_visible_qty"]]
    print(
        f"[prepare] {len(rows)} shadows, {len(ok)} submitted to NT (rest: no price / would cross), "
        f"visible-qty mismatches vs zoo: {len(mism)} ({time.time() - t0:.1f}s)",
        flush=True,
    )
    if mism:
        print("  e.g.", mism[:3])


# ---------------------------------------------------------------------------------------------------------------
# nautilus (NautilusTrader venv)
# ---------------------------------------------------------------------------------------------------------------


def lanes_of(ts_live: list[int], window_ns: int) -> list[list[int]]:
    """Greedy interval colouring: indices (sorted by ts_live) into lanes whose windows never overlap."""
    lanes: list[list[int]] = []
    ends: list[int] = []
    for i in sorted(range(len(ts_live)), key=lambda k: ts_live[k]):
        t = ts_live[i]
        for li, e in enumerate(ends):
            if t - 1 > e + 1:  # snapshot at ts_live - 1 must follow the previous cancel at e
                lanes[li].append(i)
                ends[li] = t + window_ns + 1
                break
        else:
            lanes.append([i])
            ends.append(t + window_ns + 1)
    return lanes


def _nt_worker(args: tuple) -> list[dict]:
    work, iid, cfg_names, window_ns, chunk, seed = args
    return run_instrument_nt(Path(work), int(iid), cfg_names, window_ns, chunk, seed)


def run_instrument_nt(work: Path, iid: int, cfg_names: list[str], window_ns: int, chunk: int, seed: int) -> list[dict]:
    warnings.filterwarnings("ignore")
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.backtest.models import FillModel
    from nautilus_trader.config import LoggingConfig, RiskEngineConfig, StrategyConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas, OrderBookDepth10, TradeTick
    from nautilus_trader.model.enums import (
        AccountType,
        AggressorSide,
        BookAction,
        BookType,
        OmsType,
        OrderSide,
        TimeInForce,
    )
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import FIXED_PRECISION, Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    meta = json.loads((work / "instruments.json").read_text())[str(iid)]
    pexp, sexp = int(meta["price_exp"]), int(meta["size_exp"])
    from decimal import Decimal

    tick_dec = Decimal(int(meta["tick_size"])).scaleb(pexp).normalize()
    pprec = max(0, -tick_dec.as_tuple().exponent)
    sprec = max(0, -sexp)
    pscale = 10 ** (FIXED_PRECISION + pexp)
    sscale = 10 ** (FIXED_PRECISION + sexp)
    venue = Venue("XNAS")
    inst_id = InstrumentId(Symbol(meta["symbol"]), venue)

    def P(px: int) -> Price:
        return Price.from_raw(px * pscale, pprec)

    def Q(q: int) -> Quantity:
        return Quantity.from_raw(q * sscale, sprec)

    tp = load_tape(tape_path(work, iid))
    ends = batch_ends(tp)
    sh = pq.read_table(work / "shadows.parquet").to_pandas()
    sh = sh[(sh.instrument_id == iid) & sh.price.notna() & ~sh.crosses_at_live].sort_values("ts_live")
    shadows = sh.to_dict("records")
    for s in shadows:
        s["price"] = int(s["price"])
    ts_live = [int(s["ts_live"]) for s in shadows]

    # view snapshots at each ts_live (rows with ts <= ts_live applied), window row ranges
    starts = np.searchsorted(tp.ts, np.array(ts_live, dtype=np.int64), side="right")
    stops = np.searchsorted(tp.ts, np.array(ts_live, dtype=np.int64) + window_ns, side="right")
    snaps = []
    view = View()
    j = 0
    for st in starts:
        while j < st:
            view.apply(tp, j)
            j += 1
        snaps.append(view.snapshot())

    null_order = BookOrder(OrderSide.NO_ORDER_SIDE, Price.from_raw(0, pprec), Quantity.from_raw(0, sprec), 0)

    def depth10_of(v: View, ts: int, seq: int) -> OrderBookDepth10:
        bids = sorted(v.bid.items(), reverse=True)[:10]
        asks = sorted(v.ask.items())[:10]
        bo = [BookOrder(OrderSide.BUY, P(p), Q(q), 0) for p, q in bids] + [null_order] * (10 - len(bids))
        ao = [BookOrder(OrderSide.SELL, P(p), Q(q), 0) for p, q in asks] + [null_order] * (10 - len(asks))
        bc = [1] * len(bids) + [0] * (10 - len(bids))
        ac = [1] * len(asks) + [0] * (10 - len(asks))
        return OrderBookDepth10(inst_id, bo, ao, bc, ac, 128 | 32, seq, ts, ts)

    def window_data(k: int, depth10: bool) -> list:
        """CLEAR + snapshot at ts_live - 1, then the rows of (ts_live, ts_live + window] as NT data."""
        t_snap = ts_live[k] - 1
        bid, ask = snaps[k]
        out: list = []
        if depth10:
            v = View()
            v.bid, v.ask = dict(bid), dict(ask)
            out.append(depth10_of(v, t_snap, 0))
        else:
            ds = [OrderBookDelta.clear(inst_id, 0, t_snap, t_snap)]
            for p, q in sorted(bid.items(), reverse=True):
                ds.append(
                    OrderBookDelta(
                        inst_id, BookAction.ADD, BookOrder(OrderSide.BUY, P(p), Q(q), 0), 32, 0, t_snap, t_snap
                    )
                )
            for p, q in sorted(ask.items()):
                ds.append(
                    OrderBookDelta(
                        inst_id, BookAction.ADD, BookOrder(OrderSide.SELL, P(p), Q(q), 0), 32, 0, t_snap, t_snap
                    )
                )
            last = ds[-1]
            ds[-1] = OrderBookDelta(inst_id, last.action, last.order, 32 | 128, 0, t_snap, t_snap)
            out.append(OrderBookDeltas(inst_id, ds))
            v = None
        pend: list = []
        if depth10:
            v = View()
            v.bid, v.ask = dict(bid), dict(ask)
        lvl_b, lvl_a = dict(bid), dict(ask)  # for ADD vs UPDATE
        changed = False
        for i in range(int(starts[k]), int(stops[k])):
            kd = tp.kind[i]
            t = int(tp.ts[i])
            if kd == K_TRADE:
                if pend:
                    out.append(_deltas(pend))
                    pend = []
                hidden = bool(tp.flags[i] & F_HIDDEN)
                if hidden:
                    ag = AggressorSide.NO_AGGRESSOR
                else:
                    hit = trade_hit_side(int(tp.side[i]), int(tp.aggressor[i]))
                    if hit is None:
                        continue  # unsided displayed print: the zoo drops it too
                    ag = AggressorSide.SELLER if hit == S_BID else AggressorSide.BUYER
                q = int(tp.qty[i])
                if q > 0:
                    out.append(TradeTick(inst_id, P(int(tp.price[i])), Q(q), ag, TradeId(f"T{i}"), t, t))
            elif kd == K_CLEAR:
                if depth10:
                    v.apply(tp, i)
                    changed = True
                else:
                    lvl_b.clear()
                    lvl_a.clear()
                    pend.append(OrderBookDelta.clear(inst_id, 0, t, t))
            else:
                sd = int(tp.side[i])
                if sd not in (S_BID, S_ASK):
                    continue
                if depth10:
                    v.apply(tp, i)
                    changed = True
                else:
                    lv = lvl_b if sd == S_BID else lvl_a
                    p, q = int(tp.price[i]), int(tp.qty[i])
                    fl = 32 if kd == K_SNAP else 0
                    osd = OrderSide.BUY if sd == S_BID else OrderSide.SELL
                    if q == 0:
                        if p in lv:
                            del lv[p]
                            pend.append(
                                OrderBookDelta(inst_id, BookAction.DELETE, BookOrder(osd, P(p), Q(0), 0), fl, 0, t, t)
                            )
                    else:
                        act = BookAction.UPDATE if p in lv else BookAction.ADD
                        lv[p] = q
                        pend.append(OrderBookDelta(inst_id, act, BookOrder(osd, P(p), Q(q), 0), fl, 0, t, t))
            if ends[i]:
                if depth10 and changed:
                    out.append(depth10_of(v, t, 0))
                    changed = False
                if pend:
                    out.append(_deltas(pend))
                    pend = []
        if pend:
            out.append(_deltas(pend))
        return out

    def _deltas(pend: list) -> OrderBookDeltas:
        last = pend[-1]
        pend[-1] = OrderBookDelta(
            inst_id, last.action, last.order, int(last.flags) | 128, 0, last.ts_event, last.ts_init
        )
        return OrderBookDeltas(inst_id, pend)

    class ShadowStrategy(Strategy):
        def __init__(self, items: list[int]) -> None:
            super().__init__(StrategyConfig(strategy_id="QTSHADOW-001", order_id_tag="001"))
            self.items = items
            self.orders: dict[int, object] = {}
            self.coid: dict[str, int] = {}
            self.fills: dict[int, list[tuple[int, int, int, str]]] = {}
            self.events: dict[int, list[str]] = {}

        def on_start(self) -> None:
            for k in self.items:
                self.clock.set_time_alert_ns(f"s{k}", ts_live[k], self._submit)
                self.clock.set_time_alert_ns(f"c{k}", ts_live[k] + window_ns + 1, self._cancel)

        def _submit(self, ev) -> None:
            k = int(ev.name[1:])
            s = shadows[k]
            o = self.order_factory.limit(
                instrument_id=inst_id,
                order_side=OrderSide.BUY if s["side"] == "B" else OrderSide.SELL,
                quantity=Q(int(s["qty"])),
                price=P(s["price"]),
                time_in_force=TimeInForce.GTC,
            )
            self.orders[k] = o
            self.coid[o.client_order_id.value] = k
            self.submit_order(o)

        def _cancel(self, ev) -> None:
            k = int(ev.name[1:])
            o = self.orders.get(k)
            if o is not None and o.is_open:
                self.cancel_order(o)

        def on_order_filled(self, e) -> None:
            k = self.coid[e.client_order_id.value]
            self.fills.setdefault(k, []).append(
                (int(e.ts_event), int(e.last_qty.raw // sscale), int(e.last_px.raw // pscale), e.liquidity_side.name)
            )

        def on_order_rejected(self, e) -> None:
            self.events.setdefault(self.coid[e.client_order_id.value], []).append(f"rejected:{e.reason}")

        def on_order_denied(self, e) -> None:
            self.events.setdefault(self.coid[e.client_order_id.value], []).append(f"denied:{e.reason}")

    lanes = lanes_of(ts_live, window_ns)
    chunks = [ln[i : i + chunk] for ln in lanes for i in range(0, len(ln), chunk)]
    out_rows: list[dict] = []
    allcfg = {**CONFIGS, **DEPTH10_CONFIGS}
    for cname in cfg_names:
        qpos, p, d10 = allcfg[cname]
        t0 = time.time()
        n_items = 0
        for ci, items in enumerate(chunks):
            engine = BacktestEngine(
                config=BacktestEngineConfig(
                    trader_id="QT-001",
                    logging=LoggingConfig(log_level="ERROR", bypass_logging=True),
                    risk_engine=RiskEngineConfig(bypass=True),
                )
            )
            engine.add_venue(
                venue=venue,
                oms_type=OmsType.NETTING,
                account_type=AccountType.MARGIN,
                base_currency=USD,
                starting_balances=[Money(10**12, USD)],
                fill_model=FillModel(prob_fill_on_limit=p, prob_slippage=0.0, random_seed=seed + 7919 * ci + iid),
                book_type=BookType.L2_MBP,
                trade_execution=True,
                queue_position=qpos,
            )
            engine.add_instrument(
                Equity(
                    instrument_id=inst_id,
                    raw_symbol=Symbol(meta["symbol"]),
                    currency=USD,
                    price_precision=pprec,
                    price_increment=P(int(meta["tick_size"])),
                    lot_size=Q(max(1, int(meta["lot_size"]))),
                    ts_event=0,
                    ts_init=0,
                )
            )
            data: list = []
            for k in items:
                data.extend(window_data(k, d10))
            n_items += len(data)
            engine.add_data(data, sort=True)
            strat = ShadowStrategy(items)
            engine.add_strategy(strat)
            engine.run()
            for k in items:
                s = shadows[k]
                fl = strat.fills.get(k, [])
                row = {
                    "config": cname,
                    "shadow_id": s["shadow_id"],
                    "instrument_id": iid,
                    "ts_live": ts_live[k],
                    "first_fill_ts": fl[0][0] if fl else None,
                    "first_fill_liquidity": fl[0][3] if fl else None,
                    "n_fills": len(fl),
                    "fills_json": json.dumps(fl),
                    "events": ";".join(strat.events.get(k, [])),
                }
                for h in HORIZONS_NS:
                    row[f"qty_{HNAME[h]}"] = sum(q for (t, q, _, _) in fl if t <= ts_live[k] + h)
                out_rows.append(row)
            engine.dispose()
        print(
            f"[nautilus] instrument {iid} {cname}: {len(shadows)} orders, {len(chunks)} runs, {n_items} data items, "
            f"{time.time() - t0:.1f}s",
            flush=True,
        )
    return out_rows


def cmd_nautilus(a: argparse.Namespace) -> None:
    import nautilus_trader

    warnings.filterwarnings("ignore")

    work = Path(a.work)
    cfgs = list(CONFIGS) + (list(DEPTH10_CONFIGS) if a.depth10 else [])
    if a.configs:
        cfgs = a.configs.split(",")
    iids = sorted({int(x) for x in pq.read_table(work / "shadows.parquet")["instrument_id"].to_pylist()})
    jobs = [(str(work), iid, cfgs, a.window_ns, a.chunk, a.seed) for iid in iids]
    rows: list[dict] = []
    if a.procs > 1 and len(jobs) > 1:
        import multiprocessing as mp

        with mp.get_context("spawn").Pool(min(a.procs, len(jobs))) as pool:
            for r in pool.imap_unordered(_nt_worker, jobs):
                rows.extend(r)
    else:
        for j in jobs:
            rows.extend(_nt_worker(j))
    out = work / "nt"
    out.mkdir(exist_ok=True)
    t = pa.Table.from_pylist(rows)
    t = t.replace_schema_metadata({"nautilus_trader": nautilus_trader.__version__})
    pq.write_table(t, out / "nt_outcomes.parquet")
    (out / "nt_version.json").write_text(
        json.dumps({"nautilus_trader": nautilus_trader.__version__, "python": sys.version})
    )
    print(f"[nautilus] wrote {len(rows)} rows (nautilus_trader {nautilus_trader.__version__})", flush=True)


# ---------------------------------------------------------------------------------------------------------------
# replicas (rule sets as data; used only to attribute disagreements, never as ground truth)
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rules:
    queue: bool  # track queue ahead (else touch model)
    hidden_at_price: bool  # a hidden (NO_AGGRESSOR) print at the order price is a touch / decrements the queue
    depth_min: bool  # depth/snapshot rows at the order price clamp ahead to the new level size
    gate_through: bool  # trade-throughs and crosses are blocked while ahead > 0 at the order price
    snapshot_zero: bool  # a CLEAR / snapshot sets ahead to 0


ZOO_TOUCH = Rules(queue=False, hidden_at_price=True, depth_min=False, gate_through=False, snapshot_zero=False)
NT_TOUCH = Rules(queue=False, hidden_at_price=True, depth_min=False, gate_through=False, snapshot_zero=False)
ZOO_QUEUE = Rules(queue=True, hidden_at_price=True, depth_min=False, gate_through=True, snapshot_zero=False)
NT_QUEUE = Rules(queue=True, hidden_at_price=True, depth_min=False, gate_through=True, snapshot_zero=True)


def replica_run(
    tp: Tape,
    ends: np.ndarray,
    start: int,
    stop: int,
    side: int,
    px: int,
    q0: int,
    opp0: dict[int, int],
    r: Rules,
) -> tuple[int | None, list[tuple[int, str]]]:
    """First 'certain' fill time (p = 1) and the list of probabilistic touches (ts, cause) before it."""
    ahead = q0
    opp = dict(opp0)
    touches: list[tuple[int, str]] = []
    for i in range(start, stop):
        k = tp.kind[i]
        t = int(tp.ts[i])
        if k == K_TRADE:
            p = int(tp.price[i])
            v = int(tp.qty[i])
            hidden = bool(tp.flags[i] & F_HIDDEN)
            if hidden:
                at = p == px
                through = p < px if side == S_BID else p > px
                if at and r.hidden_at_price:
                    if r.queue:
                        if ahead > 0:
                            excess = v - ahead
                            ahead = max(ahead - v, 0)
                            if excess > 0:
                                touches.append((t, "hidden_at_price"))
                        else:
                            touches.append((t, "hidden_at_price"))
                    else:
                        touches.append((t, "hidden_at_price"))
                elif through:
                    if not (r.gate_through and ahead > 0):
                        return t, touches
            else:
                hit = trade_hit_side(int(tp.side[i]), int(tp.aggressor[i]))
                if hit != side:
                    pass
                elif p == px:
                    if r.queue:
                        if ahead > 0:
                            excess = v - ahead
                            ahead = max(ahead - v, 0)
                            if excess > 0:
                                touches.append((t, "displayed_at_price"))
                        else:
                            touches.append((t, "displayed_at_price"))
                    else:
                        touches.append((t, "displayed_at_price"))
                elif (side == S_BID and p < px) or (side == S_ASK and p > px):
                    if not (r.gate_through and ahead > 0):
                        return t, touches
        elif k == K_CLEAR:
            if not r.snapshot_zero:
                return None, touches  # zoo: a clear ends the pair (censored_clear)
            opp.clear()
            ahead = 0
        elif k in (K_DEPTH, K_SNAP):
            sd = int(tp.side[i])
            p = int(tp.price[i])
            q = int(tp.qty[i])
            if sd == side and p == px and r.queue:
                if k == K_SNAP and r.snapshot_zero:
                    ahead = 0
                elif q == 0:
                    ahead = 0
                elif r.depth_min:
                    ahead = min(ahead, q)
            elif sd != side and sd in (S_BID, S_ASK):
                if q == 0:
                    opp.pop(p, None)
                else:
                    opp[p] = q
        if ends[i] and opp:
            best = min(opp) if side == S_BID else max(opp)
            crossed = best <= px if side == S_BID else best >= px
            if crossed and not (r.gate_through and ahead > 0):
                return t, touches
    return None, touches


def first_fill_p1(certain: int | None, touches: list[tuple[int, str]]) -> int | None:
    t1 = touches[0][0] if touches else None
    if certain is None:
        return t1
    if t1 is None:
        return certain
    return min(certain, t1)


# ---------------------------------------------------------------------------------------------------------------
# compare (queuetruth venv; pyarrow + pandas)
# ---------------------------------------------------------------------------------------------------------------


def _ci(x: np.ndarray, b: int = 1000, seed: int = 0) -> tuple[float, float]:
    if len(x) == 0:
        return (math.nan, math.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(b, len(x)))
    m = x[idx].mean(axis=1)
    return float(np.quantile(m, 0.025)), float(np.quantile(m, 0.975))


def _rule_subsets(zr: Rules, nr: Rules) -> list[tuple[str, ...]]:
    """Subsets of the differing rule fields, smallest first."""
    import itertools

    fields = [
        f for f in ("hidden_at_price", "depth_min", "gate_through", "snapshot_zero") if getattr(zr, f) != getattr(nr, f)
    ]
    return [c for n in range(1, len(fields) + 1) for c in itertools.combinations(fields, n)]


def cmd_compare(a: argparse.Namespace) -> None:
    import dataclasses

    import pandas as pd

    work = Path(a.work)
    sh = pq.read_table(work / "shadows.parquet").to_pandas().set_index("shadow_id")
    nt = pq.read_table(work / "nt" / "nt_outcomes.parquet").to_pandas(types_mapper={pa.int64(): pd.Int64Dtype()}.get)
    ntver = json.loads((work / "nt" / "nt_version.json").read_text())["nautilus_trader"]
    zp = pq.read_table(work / "zoo" / "zoo_predictions.parquet").to_pandas()
    zp = zp[zp.horizon_ns.isin(HORIZONS_NS)]
    all_ids = sorted(sh.index)
    n_all = len(all_ids)
    truth = zp[zp.model_id == PAIRS["touch_p1"]].pivot(index="shadow_id", columns="horizon_ns", values="truth_filled")
    truth = truth.astype(float)
    cfgs = [c for c in list(CONFIGS) + list(DEPTH10_CONFIGS) if c in set(nt.config)]
    ntby = {c: nt[nt.config == c].set_index("shadow_id") for c in cfgs}
    zby = {
        m: zp[zp.model_id == m].pivot(index="shadow_id", columns="horizon_ns", values="p_fill")
        for m in set(PAIRS.values()) | {z for _, z in EXTRA_PAIRS}
    }

    def nt_filled(cname: str, sid: str, h: int) -> bool:
        g = ntby[cname]
        if sid not in g.index:
            return False
        f = g.at[sid, "first_fill_ts"]
        return (not pd.isna(f)) and int(f) <= int(sh.at[sid, "ts_live"]) + h

    def zoo_filled(model: str, sid: str, h: int) -> bool:
        z = zby[model]
        return bool(sid in z.index and z.at[sid, h] > 0.5)

    # replica contexts (one forward pass per instrument)
    tapes: dict[int, tuple[Tape, np.ndarray]] = {}
    ctx: dict[str, tuple] = {}
    reps: dict[str, dict[str, tuple]] = {}
    named = (("zoo_touch", ZOO_TOUCH), ("nt_touch", NT_TOUCH), ("zoo_queue", ZOO_QUEUE), ("nt_queue", NT_QUEUE))
    if not a.no_replica:
        t0 = time.time()
        for iid, g in sh[sh.price.notna() & ~sh.crosses_at_live].groupby("instrument_id"):
            tp = load_tape(tape_path(work, int(iid)))
            ends = batch_ends(tp)
            tapes[int(iid)] = (tp, ends)
            g = g.sort_values("ts_live")
            tl = g.ts_live.to_numpy(dtype=np.int64)
            starts = np.searchsorted(tp.ts, tl, side="right")
            stops = np.searchsorted(tp.ts, tl + WINDOW_NS, side="right")
            view = View()
            j = 0
            for (sid, s), st, sp in zip(g.iterrows(), starts, stops, strict=True):
                while j < st:
                    view.apply(tp, j)
                    j += 1
                side = S_BID if s.side == "B" else S_ASK
                opp0 = dict(view.ask if side == S_BID else view.bid)
                ctx[sid] = (int(iid), int(st), int(sp), side, int(s.price), int(s.q_view), opp0)
                reps[sid] = {
                    n: replica_run(tp, ends, int(st), int(sp), side, int(s.price), int(s.q_view), opp0, r)
                    for n, r in named
                }
        print(f"[compare] replicas done ({time.time() - t0:.1f}s)", flush=True)

    def rep_filled(sid: str, rules: Rules, h: int) -> bool:
        iid, st, sp, side, px, q0, opp0 = ctx[sid]
        tp, ends = tapes[iid]
        t1 = first_fill_p1(*replica_run(tp, ends, st, sp, side, px, q0, opp0, rules))
        return t1 is not None and t1 <= int(sh.at[sid, "ts_live"]) + h

    report: dict = {"nautilus_trader": ntver, "n_shadows": n_all, "configs": {}}
    n_skip = int((sh.price.isna() | sh.crosses_at_live).sum())
    lines = [
        f"nautilus_trader {ntver}; {n_all} shadows ({n_skip} not submitted: rejected or would cross at live, "
        "unfilled on "
        "both sides as in the zoo)",
        "",
        "| NT config | zoo model | h | NT rate | zoo rate | exact | NT bias pp [95% CI] | zoo bias pp | agree "
        "| NT1/zoo0 "
        "| NT0/zoo1 |",
        "|" + "---|" * 11,
    ]
    rows = [(c, PAIRS[c], False) for c in cfgs] + [(c, z, True) for c, z in EXTRA_PAIRS if c in ntby]
    report["extra_pairs"] = {}
    for cname, zmodel, extra in rows:
        g = ntby[cname]
        crep: dict = {"zoo_model": zmodel, "horizons": {}}
        for h in HORIZONS_NS:
            ntf = pd.Series(0.0, index=all_ids)
            ff = g.first_fill_ts.dropna()
            ok = ff[ff.astype("int64") <= g.loc[ff.index, "ts_live"] + h]
            ntf.loc[ok.index] = 1.0
            zf = zby[zmodel][h].reindex(all_ids).fillna(0.0)
            tr = truth[h].reindex(all_ids).fillna(0.0)
            d = (ntf - tr).to_numpy()
            lo, hi = _ci(d)
            det = cname.endswith("p1")
            agree = float(((ntf > 0.5) == (zf > 0.5)).mean()) if det else math.nan
            n10 = int(((ntf > 0.5) & (zf < 0.5)).sum())
            n01 = int(((ntf < 0.5) & (zf > 0.5)).sum())
            hz = {
                "nt_rate": float(ntf.mean()),
                "zoo_rate": float(zf.mean()),
                "exact_rate": float(tr.mean()),
                "nt_bias_pp": 100 * float(d.mean()),
                "nt_bias_ci_pp": [100 * lo, 100 * hi],
                "zoo_bias_pp": 100 * float((zf - tr).mean()),
                "nt_minus_zoo_pp": 100 * float((ntf - zf).mean()),
                "agree": agree,
                "nt1_zoo0": n10 if det else None,
                "nt0_zoo1": n01 if det else None,
            }
            crep["horizons"][HNAME[h]] = hz
            lines.append(
                f"| {cname} | `{zmodel}` | {HNAME[h]} | {100 * hz['nt_rate']:.2f}% | {100 * hz['zoo_rate']:.2f}% | "
                f"{100 * hz['exact_rate']:.2f}% | {hz['nt_bias_pp']:+.2f} [{100 * lo:+.2f}, {100 * hi:+.2f}] | "
                f"{hz['zoo_bias_pp']:+.2f} | {'' if not det else f'{100 * agree:.2f}%'} | "
                f"{'' if not det else n10} | {'' if not det else n01} |"
            )
        if extra:
            report["extra_pairs"][f"{cname} vs {zmodel}"] = crep
        else:
            report["configs"][cname] = crep

    if reps:
        lines += [
            "",
            "Replica validation (fill by each horizon, per submitted shadow; replicas are rule sets, not truth):",
        ]
        val = {}
        checks = [
            ("zoo_touch", PAIRS["touch_p1"], None),
            ("nt_touch", None, "touch_p1"),
            ("zoo_queue", PAIRS["queue_p1"], None),
            ("nt_queue", None, "queue_p1"),
        ]
        for rep_name, zmodel, cname in checks:
            if cname is not None and cname not in ntby:
                continue
            agree = tot = 0
            bad = []
            for sid, rr in reps.items():
                t1 = first_fill_p1(*rr[rep_name])
                tl = int(sh.at[sid, "ts_live"])
                for h in HORIZONS_NS:
                    mine = t1 is not None and t1 <= tl + h
                    theirs = zoo_filled(zmodel, sid, h) if zmodel else nt_filled(cname, sid, h)
                    tot += 1
                    if mine == theirs:
                        agree += 1
                    elif len(bad) < 3:
                        bad.append({"shadow_id": sid, "h": HNAME[h], "replica": mine, "reference": theirs})
            val[rep_name] = {"against": zmodel or f"NT {cname}", "agree": agree, "total": tot, "examples": bad}
            lines.append(f"- {rep_name} vs {zmodel or 'NT ' + cname}: {agree}/{tot}" + (f" e.g. {bad}" if bad else ""))
        report["replica_validation"] = val

        lines += [
            "",
            "Discrepancy classes (p = 1 configs; each differing shadow at its first differing horizon, "
            "attributed to the smallest set of zoo->NT rule flips that reproduces NT on that shadow):",
        ]
        classes: dict = {}
        for fam, zr, nr in (("touch", ZOO_TOUCH, NT_TOUCH), ("queue", ZOO_QUEUE, NT_QUEUE)):
            cname = f"{fam}_p1"
            if cname not in ntby:
                continue
            zmodel = PAIRS[cname]
            subsets = _rule_subsets(zr, nr)
            cls: dict[str, list] = {}
            for sid in reps:
                for h in HORIZONS_NS:
                    ntv = nt_filled(cname, sid, h)
                    zv = zoo_filled(zmodel, sid, h)
                    if ntv == zv:
                        continue
                    cause = "unexplained"
                    for sub in subsets:
                        rr = dataclasses.replace(zr, **{f: getattr(nr, f) for f in sub})
                        if rep_filled(sid, rr, h) == ntv:
                            cause = "+".join(sub)
                            break
                    f = ntby[cname].at[sid, "first_fill_ts"]
                    zf = zby[zmodel]
                    cls.setdefault(cause, []).append(
                        {
                            "shadow_id": sid,
                            "horizon": HNAME[h],
                            "nt_filled": ntv,
                            "zoo_filled": zv,
                            "side": sh.at[sid, "side"],
                            "price": int(sh.at[sid, "price"]),
                            "queue_at_live": int(sh.at[sid, "q_view"]),
                            "nt_first_fill_ms_after_live": None
                            if pd.isna(f)
                            else (int(f) - int(sh.at[sid, "ts_live"])) / 1e6,
                        }
                    )
                    break
            n_diff = sum(len(v) for v in cls.values())
            classes[fam] = {
                k: {"n": len(v), "nt_more_fills": sum(x["nt_filled"] for x in v), "examples": v[:3]}
                for k, v in cls.items()
            }
            lines.append(
                f"- {fam} (NT {cname} vs `{zmodel}`): {n_diff} shadows differ: "
                + ", ".join(
                    f"{k} {len(v)} (NT fills, zoo not: {sum(x['nt_filled'] for x in v)})" for k, v in cls.items()
                )
            )
            for k, v in cls.items():
                lines.append(f"    {k}: e.g. {v[0]}")
        report["discrepancy_classes"] = classes

        lines.append("")
        lines.append(
            f"p = {P_LOW:g}: NT realised fill rate vs the expectation under each rule set "
            "(1 - (1 - p)^touches, 1 once a "
            "certain fill occurred):"
        )
        for cname, nt_rep, zoo_rep in (
            (f"touch_p{P_LOW:g}", "nt_touch", "zoo_touch"),
            (f"queue_p{P_LOW:g}", "nt_queue", "zoo_queue"),
        ):
            if cname not in ntby:
                continue
            for h in HORIZONS_NS:
                ex = {}
                for rn in (nt_rep, zoo_rep):
                    tot = 0.0
                    for sid, rr in reps.items():
                        tl = int(sh.at[sid, "ts_live"])
                        certain, touches = rr[rn]
                        if certain is not None and certain <= tl + h:
                            tot += 1.0
                            continue
                        n = sum(1 for (t, _) in touches if t <= tl + h)
                        tot += 1 - (1 - P_LOW) ** n
                    ex[rn] = tot / n_all
                hz = report["configs"][cname]["horizons"][HNAME[h]]
                hz["expect_nt_rules"] = ex[nt_rep]
                hz["expect_zoo_rules"] = ex[zoo_rep]
                lines.append(
                    f"- {cname} {HNAME[h]}: NT realised {100 * hz['nt_rate']:.2f}%, expected under NT rules "
                    f"{100 * ex[nt_rep]:.2f}%, under zoo rules {100 * ex[zoo_rep]:.2f}%, "
                    f"zoo reported {100 * hz['zoo_rate']:.2f}%"
                )

    (work / "report.json").write_text(json.dumps(report, indent=1, default=str))
    (work / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


# ---------------------------------------------------------------------------------------------------------------


def cmd_all(a: argparse.Namespace) -> None:
    me = str(HERE)
    base = ["--work", a.work]
    prep = [
        sys.executable,
        me,
        "prepare",
        *base,
        "--dataset",
        a.dataset,
        "--threads",
        str(a.threads),
        "--n-shadows",
        str(a.n_shadows),
    ]
    if a.shadows:
        prep += ["--shadows", a.shadows]
    subprocess.run(prep, check=True)
    ntc = [a.nt_python, me, "nautilus", *base, "--procs", str(a.procs), "--chunk", str(a.chunk), "--seed", str(a.seed)]
    if a.depth10:
        ntc.append("--depth10")
    if a.configs:
        ntc += ["--configs", a.configs]
    subprocess.run(ntc, check=True, env={**os.environ, "PYTHONNOUSERSITE": "1"})
    cmp_ = [sys.executable, me, "compare", *base]
    subprocess.run(cmp_, check=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--work", required=True)

    p = sub.add_parser("prepare")
    common(p)
    p.add_argument("--dataset", required=True)
    p.add_argument("--shadows", default=None, help="qt.shadow_spec parquet (default: sample --n-shadows, seed 0)")
    p.add_argument("--n-shadows", type=int, default=400)
    p.add_argument("--threads", type=int, default=4)
    p.set_defaults(fn=cmd_prepare)

    p = sub.add_parser("nautilus")
    common(p)
    p.add_argument("--configs", default=None)
    p.add_argument("--depth10", action="store_true")
    p.add_argument("--window-ns", type=int, default=WINDOW_NS)
    p.add_argument("--chunk", type=int, default=60)
    p.add_argument("--procs", type=int, default=4)
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(fn=cmd_nautilus)

    p = sub.add_parser("compare")
    common(p)
    p.add_argument("--no-replica", action="store_true")
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("all")
    common(p)
    p.add_argument("--dataset", required=True)
    p.add_argument("--shadows", default=None)
    p.add_argument("--n-shadows", type=int, default=400)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--nt-python", default=str(ROOT / ".scratch" / "nt" / "venv" / "bin" / "python"))
    p.add_argument("--configs", default=None)
    p.add_argument("--depth10", action="store_true")
    p.add_argument("--chunk", type=int, default=60)
    p.add_argument("--procs", type=int, default=4)
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(fn=cmd_all)

    a = ap.parse_args(argv)
    a.fn(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
