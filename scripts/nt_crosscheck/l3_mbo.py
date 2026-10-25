#!/usr/bin/env python3
"""Feed the shadows' MBO book to NautilusTrader as L3 deltas; record each order's first fill (scored by l3_score.py).

Runs in the NautilusTrader venv (numpy, pyarrow, nautilus_trader 1.x or 2.x; never queuetruth) on a work directory
`run.py prepare` made, and reuses run.py's lane assignment. Per window (same isolation as run.py): CLEAR plus every
resting order of the MBO book as of ts_live (rows with ts <= ts_live applied, FIFO per level) stamped ts_live - 1 with
F_SNAPSHOT, then the MBO rows of (ts_live, ts_live + 60 s]: A -> ADD, C -> UPDATE (remaining size) or DELETE,
M -> UPDATE, R -> CLEAR, F skipped (the C that follows removes the size), T -> TradeTick (side = aggressor; N, a
hidden execution, -> NO_AGGRESSOR, as run.py feeds hidden prints). Deltas of one (ts, sequence) group go out as one
OrderBookDeltas with F_LAST on the last. Venue `book_type=L3_MBO`; configs `l3_queue_p1` (queue_position=True) and
`l3_touch_p1`, both prob_fill_on_limit = 1.

Usage (NautilusTrader venv): l3_mbo.py <run.py> <work> <dataset> <out.parquet> [configs]
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import warnings
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

A, C, M, R, T, F = 65, 67, 77, 82, 84, 70
SB, SA, SN = 66, 65, 78
WINDOW = 60_000_000_000
HORIZONS = {"0.1s": 100_000_000, "1s": 1_000_000_000, "10s": 10_000_000_000, "60s": 60_000_000_000}
CONFIGS = {"l3_queue_p1": True, "l3_touch_p1": False}


def load_mbo(ds: Path, iid: int):
    f = sorted(ds.glob(f"mbo/*/instrument_id={iid}/*.parquet"))
    assert len(f) == 1, f
    t = pq.read_table(f[0], columns=["ts_event", "action", "side", "price", "size", "order_id", "sequence"])
    return (
        pc.cast(t["ts_event"], pa.int64()).to_numpy(),
        t["action"].to_numpy(),
        t["side"].to_numpy(),
        t["price"].to_numpy(),
        t["size"].to_numpy().astype(np.int64),
        t["order_id"].to_numpy(),
        t["sequence"].to_numpy(),
    )


class L3:
    def __init__(self) -> None:
        self.orders: dict[int, tuple[int, int]] = {}  # oid -> (side, price)
        self.levels: dict[tuple[int, int], dict[int, int]] = {}  # (side, price) -> {oid: size} in priority order

    def apply(self, act: int, side: int, px: int, sz: int, oid: int) -> None:
        if act == A:
            self.orders[oid] = (side, px)
            self.levels.setdefault((side, px), {})[oid] = sz
        elif act == C:
            k = self.orders.get(oid)
            if k is None:
                return
            lv = self.levels[k]
            rem = lv[oid] - sz
            if rem > 0:
                lv[oid] = rem
            else:
                del lv[oid]
                del self.orders[oid]
                if not lv:
                    del self.levels[k]
        elif act == M:
            k = self.orders.get(oid)
            if k is None:
                self.apply(A, side, px, sz, oid)
                return
            lv = self.levels[k]
            if k == (side, px) and sz <= lv[oid]:
                lv[oid] = sz  # size down keeps priority
            else:
                del lv[oid]
                if not lv:
                    del self.levels[k]
                del self.orders[oid]
                self.apply(A, side, px, sz, oid)
        elif act == R:
            self.orders.clear()
            self.levels.clear()

    def snapshot(self) -> list[tuple[int, int, int, int]]:
        out = []
        for (side, px), lv in self.levels.items():
            for oid, sz in lv.items():
                out.append((side, px, sz, oid))
        # bids high->low, asks low->high; FIFO kept within a level (stable sort)
        out.sort(key=lambda r: (r[0], -r[1] if r[0] == SB else r[1]))
        return out


def main() -> int:
    run_py, work, ds, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
    cfgs = sys.argv[5].split(",") if len(sys.argv) > 5 else list(CONFIGS)
    spec = importlib.util.spec_from_file_location("ntx", run_py)
    ntx = importlib.util.module_from_spec(spec)
    sys.modules["ntx"] = ntx
    spec.loader.exec_module(ntx)
    warnings.filterwarnings("ignore")

    import nautilus_trader

    nt2 = int(nautilus_trader.__version__.split(".")[0]) >= 2
    if nt2:
        from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
        from nautilus_trader.common import LoggerConfig
        from nautilus_trader.execution import DefaultFillModel, FixedFeeModel
        from nautilus_trader.model import (
            FIXED_PRECISION,
            AccountType,
            AggressorSide,
            BookAction,
            BookOrder,
            BookType,
            Currency,
            Equity,
            InstrumentId,
            Money,
            OmsType,
            OrderBookDelta,
            OrderBookDeltas,
            OrderSide,
            Price,
            Quantity,
            StrategyId,
            Symbol,
            TimeInForce,
            TradeId,
            TraderId,
            TradeTick,
            Venue,
        )
        from nautilus_trader.risk import RiskEngineConfig
        from nautilus_trader.trading import Strategy, StrategyConfig

        USD = Currency.from_str("USD")
        AG_BUY, AG_SELL = AggressorSide.BUY, AggressorSide.SELL
    else:
        from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
        from nautilus_trader.backtest.models import FillModel as DefaultFillModel
        from nautilus_trader.config import LoggingConfig, RiskEngineConfig, StrategyConfig
        from nautilus_trader.model.currencies import USD
        from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas, TradeTick
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

        AG_BUY, AG_SELL = AggressorSide.BUYER, AggressorSide.SELLER
    insts = json.loads((work / "instruments.json").read_text())
    sh_all = pq.read_table(work / "shadows.parquet").to_pandas()
    sh_all = sh_all[sh_all.price.notna() & ~sh_all.crosses_at_live]
    rows_out = []

    def run_instrument(iid: int, meta: dict) -> None:
        sh = sh_all[sh_all.instrument_id == iid].sort_values("ts_live")
        if sh.empty:
            return
        shadows = sh.to_dict("records")
        ts_live = [int(s["ts_live"]) for s in shadows]
        pexp, sexp = int(meta["price_exp"]), int(meta["size_exp"])
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

        t0 = time.time()
        ts, act, side, price, size, oid, seq = load_mbo(ds, iid)
        starts = np.searchsorted(ts, np.array(ts_live, dtype=np.int64), side="right")
        stops = np.searchsorted(ts, np.array(ts_live, dtype=np.int64) + WINDOW, side="right")
        book = L3()
        snaps = []
        j = 0
        for st in starts:
            while j < st:
                book.apply(int(act[j]), int(side[j]), int(price[j]), int(size[j]), int(oid[j]))
                j += 1
            snaps.append(book.snapshot())
        print(f"[l3] {meta['symbol']}: book states {time.time() - t0:.1f}s", flush=True)

        def osd(s: int):
            return OrderSide.BUY if s == SB else OrderSide.SELL

        def window_data(k: int) -> list:
            t_snap = ts_live[k] - 1
            ds_ = [OrderBookDelta.clear(inst_id, 0, t_snap, t_snap)]
            for s, px, sz, o in snaps[k]:
                ds_.append(
                    OrderBookDelta(inst_id, BookAction.ADD, BookOrder(osd(s), P(px), Q(sz), o), 32, 0, t_snap, t_snap)
                )
            last = ds_[-1]
            ds_[-1] = OrderBookDelta(inst_id, last.action, last.order, 32 | 128, 0, t_snap, t_snap)
            out_ = [OrderBookDeltas(inst_id, ds_)]
            # live sizes/sides of orders, to turn partial C into UPDATE
            live = {o: [s, px, sz] for s, px, sz, o in snaps[k]}
            pend: list = []
            st, sp = int(starts[k]), int(stops[k])
            for i in range(st, sp):
                a, s, px, sz, o, t = int(act[i]), int(side[i]), int(price[i]), int(size[i]), int(oid[i]), int(ts[i])
                if a == T:
                    if pend:
                        out_.append(flush(pend))
                        pend = []
                    ag = AG_BUY if s == SB else AG_SELL if s == SA else AggressorSide.NO_AGGRESSOR
                    if sz > 0:
                        out_.append(TradeTick(inst_id, P(px), Q(sz), ag, TradeId(f"T{i}"), t, t))
                elif a == A:
                    live[o] = [s, px, sz]
                    pend.append(OrderBookDelta(inst_id, BookAction.ADD, BookOrder(osd(s), P(px), Q(sz), o), 0, 0, t, t))
                elif a == C:
                    lv = live.get(o)
                    if lv is not None:
                        rem = lv[2] - sz
                        if rem > 0:
                            lv[2] = rem
                            pend.append(
                                OrderBookDelta(
                                    inst_id, BookAction.UPDATE, BookOrder(osd(lv[0]), P(lv[1]), Q(rem), o), 0, 0, t, t
                                )
                            )
                        else:
                            del live[o]
                            pend.append(
                                OrderBookDelta(
                                    inst_id, BookAction.DELETE, BookOrder(osd(lv[0]), P(lv[1]), Q(0), o), 0, 0, t, t
                                )
                            )
                elif a == M:
                    act_ = BookAction.UPDATE if o in live else BookAction.ADD
                    live[o] = [s, px, sz]
                    pend.append(OrderBookDelta(inst_id, act_, BookOrder(osd(s), P(px), Q(sz), o), 0, 0, t, t))
                elif a == R:
                    live.clear()
                    pend.append(OrderBookDelta.clear(inst_id, 0, t, t))
                end = i + 1 >= sp or seq[i + 1] != seq[i] or ts[i + 1] != ts[i]
                if end and pend:
                    out_.append(flush(pend))
                    pend = []
            if pend:
                out_.append(flush(pend))
            return out_

        def flush(pend: list) -> OrderBookDeltas:
            last = pend[-1]
            pend[-1] = OrderBookDelta(
                inst_id, last.action, last.order, int(last.flags) | 128, 0, last.ts_event, last.ts_init
            )
            return OrderBookDeltas(inst_id, pend)

        class Shadow(Strategy):
            def __init__(self, items):
                sid = StrategyId("QTSHADOW-001") if nt2 else "QTSHADOW-001"
                super().__init__(StrategyConfig(strategy_id=sid, order_id_tag="001"))
                self.items = items
                self.orders, self.coid, self.fills, self.events = {}, {}, {}, {}
                self.cancels_sent: set[int] = set()

            def on_start(self):
                for k in self.items:
                    self.clock.set_time_alert_ns(f"s{k}", ts_live[k], self._submit)
                    self.clock.set_time_alert_ns(f"c{k}", ts_live[k] + WINDOW + 1, self._cancel)

            def _submit(self, ev):
                k = int(ev.name[1:])
                s = shadows[k]
                o = self.order_factory.limit(
                    instrument_id=inst_id,
                    order_side=OrderSide.BUY if s["side"] == "B" else OrderSide.SELL,
                    quantity=Q(int(s["qty"])),
                    price=P(int(s["price"])),
                    time_in_force=TimeInForce.GTC,
                )
                self.orders[k] = o
                self.coid[o.client_order_id.value] = k
                self.submit_order(o)

            def _cancel(self, ev):
                k = int(ev.name[1:])
                o = self.orders.get(k)
                # 2.x: the object order_factory returned stays INITIALIZED, so read the cache's order; and 2.x
                # cancel_order takes the ClientOrderId (an exception in a timer callback is swallowed)
                c = None if o is None else self.cache.order(o.client_order_id)
                if c is not None and c.is_open:
                    self.cancel_order(c.client_order_id if nt2 else c)
                    self.cancels_sent.add(k)

            def on_order_filled(self, e):
                k = self.coid[e.client_order_id.value]
                self.fills.setdefault(k, []).append((int(e.ts_event), int(e.last_qty.raw // sscale)))

            def on_order_rejected(self, e):
                self.events.setdefault(self.coid[e.client_order_id.value], []).append(f"rejected:{e.reason}")

            def on_order_denied(self, e):
                self.events.setdefault(self.coid[e.client_order_id.value], []).append(f"denied:{e.reason}")

        lanes = ntx.lanes_of(ts_live, WINDOW)
        chunks = [ln[i : i + 60] for ln in lanes for i in range(0, len(ln), 60)]
        for cname in cfgs:
            qpos = CONFIGS[cname]
            t1 = time.time()
            n_items = 0
            for ci, items in enumerate(chunks):
                if nt2:
                    ecfg = BacktestEngineConfig(
                        trader_id=TraderId("QT-001"),
                        logging=LoggerConfig(bypass_logging=True),
                        risk_engine=RiskEngineConfig(bypass=True),
                    )
                else:
                    ecfg = BacktestEngineConfig(
                        trader_id="QT-001",
                        logging=LoggingConfig(log_level="ERROR", bypass_logging=True),
                        risk_engine=RiskEngineConfig(bypass=True),
                    )
                eng = BacktestEngine(config=ecfg)
                eng.add_venue(
                    venue=venue,
                    oms_type=OmsType.NETTING,
                    account_type=AccountType.MARGIN,
                    base_currency=USD,
                    starting_balances=[Money(10**12, USD)],
                    fill_model=DefaultFillModel(prob_fill_on_limit=1.0, prob_slippage=0.0, random_seed=42 + ci),
                    **({"fee_model": FixedFeeModel(Money(0, USD))} if nt2 else {}),
                    book_type=BookType.L3_MBO,
                    trade_execution=True,
                    queue_position=qpos,
                )
                eng.add_instrument(
                    Equity(
                        instrument_id=inst_id,
                        raw_symbol=Symbol(meta["symbol"]),
                        currency=USD,
                        price_precision=pprec,
                        price_increment=P(int(meta["tick_size"])),
                        ts_event=0,
                        ts_init=0,
                        lot_size=Q(max(1, int(meta["lot_size"]))),
                    )
                )
                data = []
                for k in items:
                    data.extend(window_data(k))
                n_items += len(data)
                eng.add_data(data, sort=True)
                strat = Shadow(items)
                eng.add_strategy(strat)
                eng.run()
                for k in items:
                    fl = strat.fills.get(k, [])
                    r = {
                        "config": cname,
                        "shadow_id": shadows[k]["shadow_id"],
                        "instrument_id": iid,
                        "ts_live": ts_live[k],
                        "first_fill_ts": fl[0][0] if fl else None,
                        "n_fills": len(fl),
                        "events": ";".join(strat.events.get(k, [])),
                        "cancel_sent": k in strat.cancels_sent,
                        "q_l3": sum(
                            sz
                            for s_, px_, sz, _ in snaps[k]
                            if s_ == (SB if shadows[k]["side"] == "B" else SA) and px_ == int(shadows[k]["price"])
                        ),
                    }
                    rows_out.append(r)
                eng.dispose()
            print(
                f"[l3] {meta['symbol']} {cname}: {len(shadows)} orders, {len(chunks)} runs, {n_items} items, "
                f"{time.time() - t1:.1f}s",
                flush=True,
            )

    for iid_s, meta in insts.items():
        run_instrument(int(iid_s), meta)
    t = pa.Table.from_pylist(rows_out).replace_schema_metadata({"nautilus_trader": nautilus_trader.__version__})
    pq.write_table(t, out)
    print(f"[l3] wrote {len(rows_out)} rows to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
