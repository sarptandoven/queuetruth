"""Synthetic Bitstamp public capture with the user's own orders in it, plus the matching harness log (tests and the
offline demo of the pipeline; no network, deterministic for a seed).

A small price-time FIFO engine produces the feed the way Bitstamp's public channels show it (observed on real captures):
every message stamped in whole milliseconds; a marketable order prints its trades on `live_trades` first, then appears
on `live_orders` as `order_created` + `order_deleted` (amount 0), then each maker it hit gets `order_changed` (its
remaining amount) or `order_deleted` (amount 0), with `amount_traded` = the amount that message executed; cancels are
`order_deleted` with the remaining amount and `amount_traded` 0; `detail_order_book` snapshots (best 100 orders per
side) follow book changes. `live_orders` messages carry an `event_id` / `pre_event_id` chain. The own orders are placed
at the touch (or behind it), some fill and the rest are cancelled by the harness after a hold; the harness log records
the submits, cancels and final statuses with the local clocks of `harness.py`.

`drop` removes the given `live_orders` message numbers from the capture (a gap the chain reveals); `reconnect_at` closes
and reopens the connection before the given step (a `reconnect` gap; a fresh snapshot follows)."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from livefill.capture import CaptureConfig, CaptureSession
from livefill.harness import LOG_SCHEMA, HarnessLog
from livefill.units import from_fixed
from queuetruth.errors import QtError

PX_EXP = -2  # synthetic market tick 0.01
SZ_EXP = -8
T0_US = 1_792_000_000_000_000  # 2026-10-14 UTC
LATENCY_NS = 40_000_000


def _px(v: int) -> str:
    return f"{Decimal(v).scaleb(PX_EXP):.2f}"


def _amt(v: int) -> str:
    return f"{Decimal(v).scaleb(SZ_EXP):.8f}"


@dataclass
class _Order:
    oid: int
    side: int  # 0 buy, 1 sell
    px: int  # ticks
    amt: int  # 1e-8 units


@dataclass
class SynthResult:
    capture: Path
    log: Path
    own: list[dict[str, Any]] = field(default_factory=list)
    messages: int = 0
    dropped: list[int] = field(default_factory=list)


class _Engine:
    def __init__(self, rng: random.Random, market: str, session: CaptureSession, clock: list[int]) -> None:
        self.rng = rng
        self.market = market
        self.s = session
        self.clock = clock
        self.us = T0_US
        self.next_id = 1_900_000_000_000_000
        self.next_trade = 700_000_000
        self.levels: dict[int, dict[int, dict[int, _Order]]] = {0: {}, 1: {}}
        self.orders: dict[int, _Order] = {}
        self.eid = 0
        self.n_live = 0
        self.drop: set[int] = set()
        self.dropped: list[int] = []
        self.history: dict[int, list[tuple[str, int, int, int]]] = {}  # oid -> (event, us, amt, recv)

    # -- emission ---------------------------------------------------------------------------------------------------
    def _emit(self, obj: dict[str, Any]) -> int:
        recv = self.us * 1000 + LATENCY_NS + self.rng.randint(0, 999)
        recv = max(recv, self.clock[0] + 1)
        self.clock[0] = recv
        self.s.handle(json.dumps(obj, separators=(",", ":")), recv_ns=recv, mono_ns=recv)
        return recv

    def _order_msg(self, ev: str, o: _Order, amt: int, traded: int = 0) -> None:
        self.n_live += 1
        pre = f"e{self.eid:08d}"
        self.eid += 1
        data = {
            "id": o.oid,
            "id_str": str(o.oid),
            "order_type": o.side,
            "datetime": str(self.us // 1_000_000),
            "microtimestamp": str(self.us),
            "amount": float(Decimal(amt).scaleb(SZ_EXP)),
            "amount_str": _amt(amt),
            "amount_traded": _amt(traded) if traded else "0",
            "price": float(Decimal(o.px).scaleb(PX_EXP)),
            "price_str": _px(o.px),
        }
        msg = {
            "data": data,
            "channel": f"live_orders_{self.market}",
            "event": ev,
            "event_id": f"e{self.eid:08d}",
            "pre_event_id": pre,
        }
        if self.n_live in self.drop:
            self.dropped.append(self.n_live)
            self.history.setdefault(o.oid, []).append((ev, self.us, amt, -1))
            return
        recv = self._emit(msg)
        self.history.setdefault(o.oid, []).append((ev, self.us, amt, recv))

    def _trade_msg(self, maker: _Order, taker_id: int, taker_side: int, amt: int) -> None:
        self.next_trade += 1
        buy, sell = (taker_id, maker.oid) if taker_side == 0 else (maker.oid, taker_id)
        data = {
            "id": self.next_trade,
            "timestamp": str(self.us // 1_000_000),
            "amount": float(Decimal(amt).scaleb(SZ_EXP)),
            "amount_str": _amt(amt),
            "price": float(Decimal(maker.px).scaleb(PX_EXP)),
            "price_str": _px(maker.px),
            "type": taker_side,
            "microtimestamp": str(self.us),
            "buy_order_id": buy,
            "sell_order_id": sell,
        }
        self._emit({"data": data, "channel": f"live_trades_{self.market}", "event": "trade"})

    def snapshot(self) -> None:
        bids = [[_px(o.px), _amt(o.amt), str(o.oid)] for o in self._side_orders(0)][:100]
        asks = [[_px(o.px), _amt(o.amt), str(o.oid)] for o in self._side_orders(1)][:100]
        data = {
            "timestamp": str(self.us // 1_000_000),
            "microtimestamp": str(self.us + 500),
            "bids": bids,
            "asks": asks,
        }
        self._emit({"data": data, "channel": f"detail_order_book_{self.market}", "event": "data"})

    # -- book -------------------------------------------------------------------------------------------------------
    def _side_orders(self, side: int) -> list[_Order]:
        out = []
        for px in sorted(self.levels[side], reverse=(side == 0)):
            out.extend(self.levels[side][px].values())
        return out

    def best(self, side: int) -> int | None:
        lv = self.levels[side]
        return (max(lv) if side == 0 else min(lv)) if lv else None

    def new_id(self) -> int:
        self.next_id += self.rng.randint(1, 4096)
        return self.next_id

    def tick(self) -> None:
        self.us += self.rng.randint(1, 40) * 1000

    def add(self, side: int, px: int, amt: int, oid: int | None = None) -> _Order:
        o = _Order(oid or self.new_id(), side, px, amt)
        self.orders[o.oid] = o
        self.levels[side].setdefault(px, {})[o.oid] = o
        self._order_msg("order_created", o, amt)
        return o

    def cancel(self, oid: int) -> None:
        o = self.orders.pop(oid)
        lv = self.levels[o.side][o.px]
        del lv[oid]
        if not lv:
            del self.levels[o.side][o.px]
        self._order_msg("order_deleted", o, o.amt)

    def market_order(self, taker_side: int, amt: int) -> None:
        """A marketable order: trades first, then its created/deleted pair, then the makers' updates."""
        taker = _Order(self.new_id(), taker_side, 0, amt)
        left = amt
        hits: list[tuple[_Order, int]] = []
        for o in self._side_orders(1 - taker_side):
            if left == 0:
                break
            q = min(o.amt, left)
            hits.append((o, q))
            left -= q
        if not hits:
            return
        for o, q in hits:
            self._trade_msg(o, taker.oid, taker_side, q)
        taker.px = hits[-1][0].px
        self._order_msg("order_created", taker, amt)
        self._order_msg("order_deleted", taker, left, amt - left)
        for o, q in hits:
            o.amt -= q
            if o.amt == 0:
                self.orders.pop(o.oid, None)
                lv = self.levels[o.side].get(o.px)
                if lv is not None:
                    lv.pop(o.oid, None)
                    if not lv:
                        del self.levels[o.side][o.px]
                self._order_msg("order_deleted", o, 0, q)
            else:
                self._order_msg("order_changed", o, o.amt, q)


def build(
    out: str | Path,
    seed: int = 7,
    steps: int = 1500,
    n_own: int = 24,
    market: str = "synusd",
    drop: tuple[int, ...] = (),
    reconnect_at: tuple[int, ...] = (),
    snapshot_every: int = 5,
) -> SynthResult:
    """Writes `out/capture/` (a livefill capture) and `out/harness_log.jsonl`; returns their paths and the own
    orders' expected outcomes (`order_id`, `filled_qty`, `end_state`)."""
    if n_own < 1 or steps - 250 < n_own:
        raise QtError("QT-USAGE-001", f"synth: need 1 <= n_own <= steps - 250 (steps={steps}, n_own={n_own})")
    out = Path(out)
    rng = random.Random(seed)
    clock = [T0_US * 1000]
    cfg = CaptureConfig(out=out / "capture", markets=[market], rest_snapshot=False)
    session = CaptureSession(cfg, clock=lambda: clock[0], mono=lambda: clock[0])
    eng = _Engine(rng, market, session, clock)
    eng.drop = set(drop)
    session.connected()
    for ch in ("live_orders", "live_trades", "detail_order_book"):
        eng._emit({"event": "bts:subscription_succeeded", "channel": f"{ch}_{market}", "data": {}})
    # initial book (before the capture's first snapshot, so no messages)
    for k in range(6):
        for _ in range(rng.randint(2, 5)):
            for side, px in ((0, 10_000 - k), (1, 10_001 + k)):
                o = _Order(eng.new_id(), side, px, rng.randint(1, 40) * 10_000_000)
                eng.orders[o.oid] = o
                eng.levels[side].setdefault(px, {})[o.oid] = o
    eng.tick()
    eng.snapshot()
    own_steps = sorted(rng.sample(range(50, steps - 200), n_own))
    own_open: dict[int, dict[str, Any]] = {}
    own: list[dict[str, Any]] = []
    log = HarnessLog(out / "harness_log.jsonl")
    log.write(
        {
            "type": "run_start",
            "schema": LOG_SCHEMA,
            "schema_version": 1,
            "mode": "synthetic",
            "venue": "bitstamp",
            "market": market,
            "config": {"seed": seed, "steps": steps, "n_own": n_own},
            "run_tag": f"syn{seed}",
            "local_ns": clock[0],
        }
    )
    reconnects = set(reconnect_at)
    for step in range(steps):
        eng.tick()
        if step in reconnects:
            session.disconnected("synthetic reconnect")
            eng.us += 2_000_000
            session.connected()
            eng.snapshot()
        if step in own_steps:
            i = len(own)
            side = rng.choice((0, 1))
            if eng.best(side) is None:
                side = 1 - side
            best = eng.best(side)
            if best is None:
                continue
            behind = rng.choice((0, 0, 1))
            px = (best - behind) if side == 0 else (best + behind)
            amt = rng.randint(5, 30) * 10_000_000 + rng.randint(1, 9_999)  # odd lot: a distinctive signature
            coid = f"qtlf-syn{seed}-{i:05d}"
            sent = eng.us * 1000 - 3_000_000
            level = eng.levels[side].get(px, {})
            ahead = (len(level), sum(x.amt for x in level.values()))
            o = eng.add(side, px, amt)
            resp_recv = clock[0] + 1_000_000
            params = {"amount": _amt(amt), "price": _px(px), "client_order_id": coid, "moc_order": "True"}
            log.write(
                {
                    "type": "submit",
                    "i": i,
                    "client_order_id": coid,
                    "endpoint": f"{'buy' if side == 0 else 'sell'}/{market}",
                    "params": params,
                    "sent_ns": sent,
                    "recv_ns": resp_recv,
                    "outcome": "ok",
                    "http_status": 200,
                    "body": {"id": str(o.oid), "client_order_id": coid, "price": params["price"]},
                    "side": "buy" if side == 0 else "sell",
                }
            )
            rec = {
                "i": i,
                "order_id": o.oid,
                "client_order_id": coid,
                "hold": rng.randint(10, 120),
                "orig": amt,
                "entry_ahead_orders": ahead[0],
                "entry_ahead_qty": ahead[1],
            }
            own_open[o.oid] = rec
            own.append(rec)
        for oid, rec in list(own_open.items()):
            rec["hold"] -= 1
            if oid not in eng.orders:
                rec.setdefault("end_state", "filled")
                del own_open[oid]
            elif rec["hold"] <= 0:
                sent = eng.us * 1000 - 2_000_000
                eng.cancel(oid)
                log.write(
                    {
                        "type": "cancel",
                        "i": rec["i"],
                        "client_order_id": rec["client_order_id"],
                        "endpoint": "cancel_order",
                        "params": {"id": str(oid)},
                        "sent_ns": sent,
                        "recv_ns": clock[0] + 1_000_000,
                        "outcome": "ok",
                        "http_status": 200,
                        "body": {"id": str(oid), "status": "Canceled"},
                        "order_id": str(oid),
                    }
                )
                rec["end_state"] = "cancelled"
                del own_open[oid]
        r = rng.random()
        if r < 0.5 or len(eng.orders) < 30:
            side = rng.choice((0, 1))
            best = eng.best(side) or (10_000 if side == 0 else 10_001)
            off = rng.choice((0, 0, 1, 2))
            eng.add(side, best - off if side == 0 else best + off, rng.randint(1, 30) * 10_000_000)
        elif r < 0.82:
            cands = [oid for oid in eng.orders if oid not in own_open]
            eng.cancel(rng.choice(cands))
        else:
            side = rng.choice((0, 1))
            opp = eng._side_orders(1 - side)
            if len(opp) >= 5:
                cap = sum(o.amt for o in opp) * 7 // 10
                eng.market_order(side, min(rng.randint(1, 60) * 10_000_000, cap))
        if step % snapshot_every == 0:
            eng.snapshot()
    for rec in own_open.values():
        rec["end_state"] = "open_at_end"
    for rec in own:
        hist = eng.history.get(rec["order_id"], [])
        rec["filled_qty"] = rec["orig"] - (hist[-1][2] if hist else rec["orig"])
    log.write({"type": "run_end", "summary": {"placed": len(own)}, "local_ns": clock[0]})
    log.close()
    session.close()
    return SynthResult(
        capture=out / "capture",
        log=out / "harness_log.jsonl",
        own=own,
        messages=session.seq,
        dropped=list(eng.dropped),
    )


def price_text(v: int) -> str:
    """Synthetic tick count -> price text at 1e-9 units (helper for tests)."""
    return from_fixed(v * 10**7, -9)
