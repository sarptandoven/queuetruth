"""`queuetruth pm fixtures`: invented Kalshi and Polymarket captures to walk through the kit with (no venue data).

An exact price-time FIFO book (individual orders) is simulated: other traders add orders near the touch, cancel
uniformly random resting orders (so a cancel lands ahead of an order in proportion to the size ahead of it) and send
takers that sweep the best levels; a small market maker (the "own" account) joins the best bid and best ask, rests
for a few seconds and cancels. Every event is then written in the documented message formats of Kalshi
(docs.kalshi.com: `orderbook_snapshot`, `orderbook_delta`, `trade`, `fill`, `user_order`, REST responses) and
Polymarket (docs.polymarket.com: market channel, user channel, Gamma market object, and `eth_getLogs` entries of
CTF Exchange V2 `OrderFilled` / `OrdersMatched` / `FeeCharged`). Because the book is exact, `truth.json` holds what no
venue publishes: each own order's true queue ahead at every sample and fill, and the operator's maker order in every
match. Markets, accounts, order ids and values are invented.

The generation is deterministic: `write_fixtures` writes the same bytes every time, and the repository's
`tests/fixtures/pm/` is one generation (its tests compare it with a fresh one; `MANIFEST.sha256` lists every file).
`kalshi(seed, n_events)` writes longer captures of the same process for tests of scale.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.pm.keccak import keccak256, topic

B, A = "B", "A"
T0_MS = 1_792_764_000_000  # 2026-10-23T14:00:00Z
KALSHI_TICKER = "KXDEMO-26OCT23-T50"
POLY_CONDITION = "0x" + "5e" * 32
POLY_YES = "48230119864057241947752938604731125541089712355090447216983571920448812350017"
POLY_NO = "91326675520183942265113350876220495101937735418702334465180987442155209960443"
POLY_EXCHANGE = "0xe111180000d2663c0091e4f400237545b87b996b"
FEE_RECEIVER = "0x" + "fe" * 20
OWN_WALLET = "0x" + "0a" * 20
OWN_KEY = "11111111-2222-4333-8444-555555555555"


@dataclass
class Order:
    oid: str
    side: str
    price: int  # price units (1e-4 dollars)
    qty: int  # size units
    owner: str  # "own" or a trader id
    cid: str | None = None
    created_ms: int = 0
    filled: int = 0
    initial: int = 0
    no_token: bool = False  # Polymarket: posted on the NO token (mirrored on the YES ladder)


@dataclass
class Fill:
    maker: Order
    price: int
    qty: int
    ahead_before: int  # exact size ahead of the maker at its level before the taker hit it


@dataclass
class Event:
    kind: str  # add | cancel | take | snapshot
    t_ms: int
    clock: int
    order: Order | None = None
    qty: int = 0
    side: str = ""  # taker aggressor side for take
    fills: list[Fill] = field(default_factory=list)
    deltas: list[tuple[str, int, int]] = field(default_factory=list)  # (side, price, signed size)
    taker_id: str = ""


class Book:
    """Exact FIFO book."""

    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.levels: dict[str, dict[int, deque[str]]] = {B: {}, A: {}}

    def qty(self, side: str, price: int) -> int:
        return sum(self.orders[o].qty for o in self.levels[side].get(price, ()))

    def best(self, side: str) -> int | None:
        lv = [p for p, q in self.levels[side].items() if q]
        if not lv:
            return None
        return max(lv) if side == B else min(lv)

    def prices(self, side: str) -> list[int]:
        lv = [p for p, q in self.levels[side].items() if q]
        return sorted(lv, reverse=side == B)

    def add(self, o: Order) -> None:
        self.orders[o.oid] = o
        self.levels[o.side].setdefault(o.price, deque()).append(o.oid)

    def remove(self, oid: str) -> Order:
        o = self.orders.pop(oid)
        self.levels[o.side][o.price].remove(oid)
        return o

    def ahead(self, oid: str) -> int:
        o = self.orders[oid]
        tot = 0
        for x in self.levels[o.side][o.price]:
            if x == oid:
                return tot
            tot += self.orders[x].qty
        raise AssertionError(oid)

    def take(self, aggr: str, qty: int, levels: int) -> list[Fill]:
        hit = A if aggr == B else B
        fills: list[Fill] = []
        left = qty
        for p in self.prices(hit)[:levels]:
            q = self.levels[hit][p]
            while q and left > 0:
                m = self.orders[q[0]]
                ahead = 0
                x = min(left, m.qty)
                fills.append(Fill(m, p, x, ahead))
                m.qty -= x
                m.filled += x
                left -= x
                if m.qty == 0:
                    self.remove(m.oid)
            if left <= 0:
                break
        return fills


def _uuid(rng: random.Random) -> str:
    h = f"{rng.getrandbits(128):032x}"
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-8{h[17:20]}-{h[20:32]}"


def simulate(
    seed: int, n_events: int, *, size_unit: int, decimals: bool, no_token_share: float = 0.0, own_lots: int = 10
) -> dict[str, Any]:
    """Runs the book; returns the event list, the own orders (`own_lots` size units each), queue samples and the
    initial book."""
    rng = random.Random(seed)
    book = Book()
    tick = 100
    events: list[Event] = []
    clock = 1000
    t = T0_MS
    n_other = 0

    def new_other(side: str, price: int) -> Order:
        nonlocal n_other
        n_other += 1
        base = rng.randint(2, 60)
        frac = rng.choice([0, 0, 0, 25, 50]) if decimals else 0
        qty = base * size_unit + frac * size_unit // 100
        return Order(
            f"o{n_other:05d}",
            side,
            price,
            qty,
            f"trader{rng.randint(1, 9)}",
            created_ms=t,
            initial=qty,
            no_token=rng.random() < no_token_share,
        )

    for k in range(5):
        for side, p in ((B, 4400 - k * tick), (A, 4600 + k * tick)):
            for _ in range(rng.randint(2, 5)):
                book.add(new_other(side, p))
    initial = {s: {p: book.qty(s, p) for p in book.prices(s)} for s in (B, A)}
    initial_orders = [book.orders[o] for s in (B, A) for p in book.prices(s) for o in book.levels[s][p]]
    own: list[dict[str, Any]] = []
    own_live: dict[str, Order] = {}
    own_cancel_at: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    next_own = {B: T0_MS + 1500, A: T0_MS + 2500}
    next_sample = T0_MS + 1000
    n_own = 0

    # one market event at time t; an event that cannot happen returns early and the caller still refills thin
    # books, so a book that thinned out always recovers
    def market_event(t: int) -> None:
        nonlocal clock
        u = rng.random()
        clock += rng.randint(1, 3)
        if u < 0.45:
            side = rng.choice((B, A))
            best = book.best(side)
            opp = book.best(A if side == B else B)
            if best is None or opp is None:
                return
            step = rng.choice((0, 0, 0, 1, 1, 2, 3, -1))
            p = best - step * tick if side == B else best + step * tick
            if (side == B and p >= opp) or (side == A and p <= opp):
                p = best
            if not 100 <= p <= 9900:
                return
            o = new_other(side, p)
            book.add(o)
            events.append(Event("add", t, clock, o, o.qty, deltas=[(side, p, o.qty)]))
        elif u < 0.80:
            top = {s: set(book.prices(s)[:4]) for s in (B, A)}
            cands = [o for o in book.orders.values() if o.owner != "own" and o.price in top[o.side]]
            if not cands:
                return
            o = rng.choice(sorted(cands, key=lambda x: x.oid))
            if sum(1 for x in book.levels[o.side][o.price] if x != o.oid) == 0 and len(book.prices(o.side)) < 4:
                return
            book.remove(o.oid)
            events.append(Event("cancel", t, clock, o, o.qty, deltas=[(o.side, o.price, -o.qty)]))
        else:
            aggr = rng.choice((B, A))
            hit = A if aggr == B else B
            if len(book.prices(hit)) < 3:
                return
            want = rng.randint(1, 70) * size_unit
            pre = {oid: book.ahead(oid) for oid in own_live}
            fills = book.take(aggr, want, rng.choice((1, 1, 1, 2)))
            if not fills:
                return
            done: dict[int, int] = {}
            for f in fills:
                done[f.price] = done.get(f.price, 0) + f.qty
                if f.maker.owner == "own":
                    f.ahead_before = pre[f.maker.oid]
                    rec = own[_own_index(own, f.maker.oid)]
                    rec["fills"].append({"t_ms": t, "qty": f.qty, "price": f.price, "ahead_before": pre[f.maker.oid]})
                    if f.maker.qty == 0:
                        own_live.pop(f.maker.oid, None)
                        own_cancel_at.pop(f.maker.oid, None)
                        next_own[f.maker.side] = t + rng.randint(800, 2500)
            deltas = [(hit, p, -q) for p, q in sorted(done.items(), key=lambda x: -x[0] if hit == B else x[0])]
            events.append(Event("take", t, clock, None, want, aggr, fills, deltas, taker_id=f"tk{len(events):05d}"))

    while len(events) < n_events:
        t += max(1, int(rng.expovariate(1 / 140.0)))
        # own strategy: cancels, placements, queue samples
        for oid in [o for o, at in own_cancel_at.items() if at <= t and o in own_live]:
            o = own_live.pop(oid)
            del own_cancel_at[oid]
            book.remove(oid)
            clock += rng.randint(1, 3)
            events.append(Event("cancel", t, clock, o, o.qty, deltas=[(o.side, o.price, -o.qty)]))
            own[_own_index(own, oid)]["cancel_ms"] = t
            next_own[o.side] = t + rng.randint(800, 2500)
        for side in (B, A):
            if t >= next_own[side] and not any(o.side == side for o in own_live.values()):
                p = book.best(side)
                if p is None:
                    continue
                n_own += 1
                qty = own_lots * size_unit
                o = Order(f"own{n_own:03d}", side, p, qty, "own", cid=f"mm-{n_own}", created_ms=t, initial=qty)
                book.add(o)
                own_live[o.oid] = o
                own_cancel_at[o.oid] = t + rng.randint(4000, 12000)
                clock += rng.randint(1, 3)
                events.append(Event("add", t, clock, o, qty, deltas=[(side, p, qty)]))
                own.append(
                    {
                        "order_id": o.oid,
                        "cid": o.cid,
                        "side": side,
                        "price": p,
                        "qty": qty,
                        "created_ms": t,
                        "clock": clock,
                        "ahead_at_entry": book.ahead(o.oid),
                        "fills": [],
                        "samples": [],
                    }
                )
        if t >= next_sample:
            for oid in sorted(own_live):
                ahead = book.ahead(oid)
                rec = {"t_ms": t, "queue_ahead": ahead}
                own[_own_index(own, oid)]["samples"].append(rec)
                samples.append({"order_id": oid, "t_ms": t, "queue_ahead": ahead})
            next_sample = t + rng.randint(700, 1300)
        market_event(t)
        # refill thin books
        for side in (B, A):
            if len(book.prices(side)) < 4:
                worst = book.prices(side)[-1] if book.prices(side) else (4400 if side == B else 4600)
                p = worst - tick if side == B else worst + tick
                if 100 <= p <= 9900:
                    o = new_other(side, p)
                    book.add(o)
                    clock += 1
                    events.append(Event("add", t, clock, o, o.qty, deltas=[(side, p, o.qty)]))
    return {
        "events": events,
        "own": own,
        "samples": samples,
        "initial": initial,
        "initial_orders": initial_orders,
        "end_ms": t,
        "final": {s: {p: book.qty(s, p) for p in book.prices(s)} for s in (B, A)},
    }


def _own_index(own: list[dict[str, Any]], oid: str) -> int:
    for i, r in enumerate(own):
        if r["order_id"] == oid:
            return i
    raise KeyError(oid)


def _d(units: int, exp: int, places: int) -> str:
    """Fixed-point units -> decimal text with `places` decimals."""
    return format(Decimal(units).scaleb(exp).quantize(Decimal(1).scaleb(-places)), "f")


def _dump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=False)


# ---- Kalshi ---------------------------------------------------------------------------------------------------------


def _kalshi_fee(q_units: int, p_units: int) -> str:
    """The fixture's maker fee (quadratic_with_maker_fees): 0.0175 x C x P x (1 - P), ceiled to $0.000001 and then
    to the direct-member $0.0001 precision (one fill per order in the fixture, so the accumulator never rebates)."""
    c = Decimal(q_units).scaleb(-2)
    p = Decimal(p_units).scaleb(-4)
    fee = (Decimal("0.0175") * c * p * (1 - p) / Decimal("0.000001")).to_integral_value(ROUND_CEILING) * Decimal(
        "0.000001"
    )
    gross = -c * p - fee
    aligned = (gross / Decimal("0.0001")).to_integral_value(ROUND_FLOOR) * Decimal("0.0001")
    return format(fee + (gross - aligned), ".6f")


def kalshi(seed: int = 7, n_events: int = 520) -> dict[str, bytes]:
    """`kalshi/ws.jsonl`, `kalshi/rest.jsonl`, `kalshi/truth.json`."""
    sim = simulate(seed, n_events, size_unit=100, decimals=False)
    rng = random.Random(seed + 1)
    ws: list[tuple[int, int, str]] = []  # (recv_ns, order, line)
    seq = {1: 0, 2: 0}
    n = 0

    def put(recv_ns: int, msg: Any, out: bool = False) -> None:
        nonlocal n
        n += 1
        wrap = {"dir": "out", "local_ns": recv_ns, "msg": msg} if out else {"recv_ns": recv_ns, "msg": msg}
        ws.append((recv_ns, n, _dump(wrap)))

    t0 = T0_MS * 1_000_000
    put(
        t0 - 50_000_000,
        {
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_ticker": KALSHI_TICKER, "use_yes_price": False},
        },
        out=True,
    )
    put(t0 - 40_000_000, {"id": 1, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}})
    put(
        t0 - 30_000_000,
        {"id": 2, "cmd": "subscribe", "params": {"channels": ["trade"], "market_ticker": KALSHI_TICKER}},
        out=True,
    )
    put(t0 - 20_000_000, {"id": 2, "type": "subscribed", "msg": {"channel": "trade", "sid": 2}})
    mid = "9b0f6b43-5b68-4f9f-9f02-0000000000d1"
    seq[1] += 1
    snap = {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": seq[1],
        "sending_ts_ms": T0_MS,
        "msg": {
            "market_ticker": KALSHI_TICKER,
            "market_id": mid,
            "clock": 999,
            "yes_dollars_fp": [[_d(p, -4, 4), _d(q, -2, 2)] for p, q in sorted(sim["initial"][B].items())],
            "no_dollars_fp": [
                [_d(10_000 - p, -4, 4), _d(q, -2, 2)] for p, q in sorted(sim["initial"][A].items(), reverse=True)
            ],
        },
    }
    put(t0 + 3_000_000, snap)
    own_state: dict[str, dict[str, Any]] = {}
    oid_uuid: dict[str, str] = {}
    fill_ids: dict[tuple[str, int], str] = {}
    last_recv = 0
    trade_n = 0
    for ev in sim["events"]:
        lat = (4 + rng.randint(0, 3)) * 1_000_000
        recv = max(last_recv + 10_000, ev.t_ms * 1_000_000 + lat)  # one connection delivers in order
        last_recv = recv
        cid = ev.order.cid if ev.order is not None and ev.order.owner == "own" else None
        if ev.kind == "take":
            for f in ev.fills:
                trade_n += 1
                seq[2] += 1
                tid = _uuid(rng)
                taker_yes = ev.side == B
                put(
                    recv,
                    {
                        "type": "trade",
                        "sid": 2,
                        "seq": seq[2],
                        "msg": {
                            "trade_id": tid,
                            "market_ticker": KALSHI_TICKER,
                            "yes_price_dollars": _d(f.price, -4, 4),
                            "no_price_dollars": _d(10_000 - f.price, -4, 4),
                            "count_fp": _d(f.qty, -2, 2),
                            "taker_side": "yes" if taker_yes else "no",
                            "taker_outcome_side": "yes" if taker_yes else "no",
                            "taker_book_side": "bid" if taker_yes else "ask",
                            "is_block_trade": False,
                            "ts": ev.t_ms // 1000,
                            "ts_ms": ev.t_ms,
                        },
                    },
                )
                if f.maker.owner == "own":
                    fill_ids[(f.maker.oid, ev.t_ms)] = tid
                    st = own_state[f.maker.oid]
                    st["fill"] += f.qty
                    st["rem"] -= f.qty
                    cost = Decimal(f.qty).scaleb(-2) * (
                        Decimal(f.price).scaleb(-4) if f.maker.side == B else 1 - Decimal(f.price).scaleb(-4)
                    )
                    st["maker_cost"] += cost
                    fee = _kalshi_fee(f.qty, f.price)
                    st["maker_fees"] += Decimal(fee)
                    put(
                        recv + 2_000_000,
                        {
                            "type": "fill",
                            "sid": 4,
                            "msg": {
                                "trade_id": tid,
                                "order_id": oid_uuid[f.maker.oid],
                                "client_order_id": f.maker.cid,
                                "market_ticker": KALSHI_TICKER,
                                "exchange_index": 0,
                                "is_taker": False,
                                "side": "yes" if f.maker.side == B else "no",
                                "yes_price_dollars": _d(f.price, -4, 4),
                                "count_fp": _d(f.qty, -2, 2),
                                "fee_cost": fee,
                                "action": "buy",
                                "ts": ev.t_ms // 1000,
                                "ts_ms": ev.t_ms,
                                "post_position_fp": "0.00",
                                "purchased_side": "yes" if f.maker.side == B else "no",
                                "outcome_side": "yes" if f.maker.side == B else "no",
                                "book_side": "bid" if f.maker.side == B else "ask",
                            },
                        },
                    )
                    put(
                        recv + 3_000_000,
                        _user_order(
                            oid_uuid[f.maker.oid],
                            f.maker,
                            st,
                            ev.t_ms,
                            "executed" if st["rem"] == 0 else "resting",
                            "Trade",
                        ),
                    )
        for side, p, d in ev.deltas:
            seq[1] += 1
            msg = {
                "market_ticker": KALSHI_TICKER,
                "market_id": mid,
                "clock": ev.clock,
                "price_dollars": _d(p if side == B else 10_000 - p, -4, 4),
                "delta_fp": _d(d, -2, 2),
                "side": "yes" if side == B else "no",
                "ts_ms": ev.t_ms,
            }
            if cid is not None:
                msg["client_order_id"] = cid
            put(recv + 1_000_000, {"type": "orderbook_delta", "sid": 1, "seq": seq[1], "msg": msg})
        if ev.order is not None and ev.order.owner == "own":
            o = ev.order
            if ev.kind == "add":
                oid_uuid[o.oid] = _uuid(rng)
                own_state[o.oid] = {"fill": 0, "rem": ev.qty, "maker_cost": Decimal(0), "maker_fees": Decimal(0)}
                put(recv + 2_500_000, _user_order(oid_uuid[o.oid], o, own_state[o.oid], ev.t_ms, "resting", None))
            elif ev.kind == "cancel":
                st = own_state[o.oid]
                st["rem"] = 0
                put(recv + 2_500_000, _user_order(oid_uuid[o.oid], o, st, ev.t_ms, "canceled", ""))
    end = sim["end_ms"] + 5
    put(
        end * 1_000_000 + 9_000_000,
        {
            "type": "market_lifecycle_v2",
            "sid": 9,
            "seq": 1,
            "msg": {
                "event_type": "determined",
                "market_ticker": KALSHI_TICKER,
                "result": "yes",
                "settlement_value": "1.0000",
                "determination_ts": end // 1000,
            },
        },
    )
    ws.sort(key=lambda x: (x[0], x[1]))
    # REST: market, series, incentives, fills and orders exports, queue positions
    rest: list[str] = []
    rest.append(
        _dump(
            {
                "recv_ns": t0 - 100_000_000,
                "path": f"/trade-api/v2/markets/{KALSHI_TICKER}",
                "msg": {
                    "market": {
                        "ticker": KALSHI_TICKER,
                        "event_ticker": "KXDEMO-26OCT23",
                        "status": "active",
                        "price_level_structure": "linear_cent",
                        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
                    }
                },
            }
        )
    )
    rest.append(
        _dump(
            {
                "recv_ns": t0 - 90_000_000,
                "msg": {
                    "series": {
                        "ticker": "KXDEMO",
                        "frequency": "daily",
                        "title": "Demo series",
                        "fee_type": "quadratic_with_maker_fees",
                        "fee_multiplier": 1,
                    }
                },
            }
        )
    )
    rest.append(
        _dump(
            {
                "recv_ns": t0 - 80_000_000,
                "msg": {
                    "incentive_programs": [
                        {
                            "id": "lip-demo-1",
                            "market_id": mid,
                            "market_ticker": KALSHI_TICKER,
                            "incentive_type": "liquidity",
                            "incentive_description": "Demo liquidity incentive",
                            "start_date": "2026-10-23T00:00:00Z",
                            "end_date": "2026-10-24T00:00:00Z",
                            "period_reward": 500000,
                            "paid_out": False,
                            "discount_factor_bps": 5000,
                            "target_size_fp": "300.00",
                        }
                    ]
                },
            }
        )
    )
    for s in sim["samples"]:
        t_ns = s["t_ms"] * 1_000_000
        send = t_ns - 18_000_000
        recv = t_ns + 22_000_000
        rest.append(
            _dump(
                {
                    "dir": "out",
                    "local_ns": send,
                    "msg": {
                        "method": "GET",
                        "path": f"/trade-api/v2/portfolio/orders/queue_positions?market_tickers={KALSHI_TICKER}",
                    },
                }
            )
        )
        rest.append(
            _dump(
                {
                    "dir": "in",
                    "local_ns": recv,
                    "msg": {
                        "queue_positions": [
                            {
                                "order_id": oid_uuid[s["order_id"]],
                                "market_ticker": KALSHI_TICKER,
                                "queue_position_fp": _d(s["queue_ahead"], -2, 2),
                            }
                        ]
                    },
                }
            )
        )
    fills_export = []
    for o in sim["own"]:
        for f in o["fills"]:
            fid = fill_ids[(o["order_id"], f["t_ms"])]
            fills_export.append(
                {
                    "fill_id": fid,
                    "trade_id": fid,
                    "order_id": oid_uuid[o["order_id"]],
                    "ticker": KALSHI_TICKER,
                    "market_ticker": KALSHI_TICKER,
                    "outcome_side": "yes" if o["side"] == B else "no",
                    "book_side": "bid" if o["side"] == B else "ask",
                    "count_fp": _d(f["qty"], -2, 2),
                    "yes_price_dollars": _d(f["price"], -4, 4),
                    "no_price_dollars": _d(10_000 - f["price"], -4, 4),
                    "is_taker": False,
                    "fee_cost": _kalshi_fee(f["qty"], f["price"]),
                    "created_time": _iso(f["t_ms"]),
                }
            )
    rest.append(_dump({"recv_ns": end * 1_000_000 + 50_000_000, "msg": {"fills": fills_export, "cursor": ""}}))
    truth = {
        "venue": "kalshi",
        "ticker": KALSHI_TICKER,
        "settlement": "1.0000",
        "events": len(sim["events"]),
        "own": [{**o, "order_id": oid_uuid[o["order_id"]], "sim_id": o["order_id"]} for o in sim["own"]],
        "final_book": {s: {str(p): q for p, q in lv.items()} for s, lv in sim["final"].items()},
    }
    return {
        "kalshi/ws.jsonl": ("\n".join(x[2] for x in ws) + "\n").encode(),
        "kalshi/rest.jsonl": ("\n".join(rest) + "\n").encode(),
        "kalshi/truth.json": (json.dumps(truth, indent=1, sort_keys=True) + "\n").encode(),
    }


def _iso(t_ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(t_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t_ms % 1000:03d}Z"


def _user_order(oid: str, o: Order, st: dict[str, Any], t_ms: int, status: str, reason: str | None) -> dict[str, Any]:
    msg = {
        "order_id": oid,
        "user_id": "a1b2c3d4-e5f6-4890-abcd-ef1234567890",
        "ticker": KALSHI_TICKER,
        "exchange_index": 0,
        "status": status,
        "side": "yes" if o.side == B else "no",
        "is_yes": o.side == B,
        "outcome_side": "yes" if o.side == B else "no",
        "book_side": "bid" if o.side == B else "ask",
        "yes_price_dollars": _d(o.price, -4, 4),
        "fill_count_fp": _d(st["fill"], -2, 2),
        "remaining_count_fp": _d(st["rem"], -2, 2),
        "initial_count_fp": _d(o.initial, -2, 2),
        "taker_fill_cost_dollars": "0.000000",
        "maker_fill_cost_dollars": format(st["maker_cost"], ".6f"),
        "taker_fees_dollars": "0.000000",
        "maker_fees_dollars": format(st["maker_fees"], ".6f"),
        "client_order_id": o.cid,
        "created_time": _iso(o.created_ms),
        "created_ts_ms": o.created_ms,
        "last_updated_ts_ms": t_ms,
    }
    if reason is not None:
        msg["last_update_reason"] = reason
    return {"type": "user_order", "sid": 3, "msg": msg}


# ---- Polymarket -----------------------------------------------------------------------------------------------------


def _word(v: int) -> str:
    return f"{v:064x}"


def _addr_topic(a: str) -> str:
    return "0x" + "0" * 24 + a[2:]


def polymarket(seed: int = 11, n_events: int = 420) -> dict[str, bytes]:
    """`polymarket/market.jsonl`, `polymarket/user.jsonl`, `polymarket/gamma.json`, `polymarket/logs.json`,
    `polymarket/truth.json`."""
    sim = simulate(seed, n_events, size_unit=1_000_000, decimals=True, no_token_share=0.15, own_lots=25)
    rng = random.Random(seed + 1)
    t_filled = topic("OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)")
    t_matched = topic("OrdersMatched(bytes32,address,uint8,uint256,uint256,uint256)")
    t_fee = topic("FeeCharged(address,uint256)")
    market: list[tuple[int, int, str]] = []
    user: list[tuple[int, int, str]] = []
    n = 0

    def put(dst: list[tuple[int, int, str]], recv_ns: int, msg: Any) -> None:
        nonlocal n
        n += 1
        dst.append((recv_ns, n, _dump({"recv_ns": recv_ns, "msg": msg})))

    def h(oid: str) -> str:
        return "0x" + keccak256(f"demo-order:{oid}".encode()).hex()

    wallets: dict[str, str] = {}

    def wallet(owner: str) -> str:
        if owner == "own":
            return OWN_WALLET
        if owner not in wallets:
            wallets[owner] = "0x" + keccak256(owner.encode()).hex()[:40]
        return wallets[owner]

    t0 = T0_MS * 1_000_000
    for asset, mirror in ((POLY_YES, False), (POLY_NO, True)):
        bids = sim["initial"][B] if not mirror else {10_000 - p: q for p, q in sim["initial"][A].items()}
        asks = sim["initial"][A] if not mirror else {10_000 - p: q for p, q in sim["initial"][B].items()}
        put(
            market,
            t0 + 2_000_000,
            [
                {
                    "event_type": "book",
                    "market": POLY_CONDITION,
                    "asset_id": asset,
                    "timestamp": str(T0_MS),
                    "hash": "0x" + f"{rng.getrandbits(160):040x}",
                    "bids": [{"price": _d(p, -4, 2), "size": _d(q, -6, 2)} for p, q in sorted(bids.items())],
                    "asks": [
                        {"price": _d(p, -4, 2), "size": _d(q, -6, 2)} for p, q in sorted(asks.items(), reverse=True)
                    ],
                }
            ],
        )
    book = {s: dict(v) for s, v in sim["initial"].items()}
    block = 78_400_000
    logs: list[dict[str, Any]] = []
    matches_truth: list[dict[str, Any]] = []
    own_orders: dict[str, dict[str, Any]] = {}
    log_index = 0
    last_recv = 0
    for ev in sim["events"]:
        recv = max(last_recv + 10_000, ev.t_ms * 1_000_000 + (5 + rng.randint(0, 4)) * 1_000_000)
        last_recv = recv
        changes = []
        for side, p, d in ev.deltas:
            book[side][p] = book[side].get(p, 0) + d
            if book[side][p] == 0:
                del book[side][p]
            bb = max(book[B]) if book[B] else None
            ba = min(book[A]) if book[A] else None
            changes.append(
                {
                    "asset_id": POLY_YES,
                    "price": _d(p, -4, 2),
                    "size": _d(book[side].get(p, 0), -6, 2),
                    "side": "BUY" if side == B else "SELL",
                    "hash": f"{rng.getrandbits(160):040x}",
                    "best_bid": None if bb is None else _d(bb, -4, 2),
                    "best_ask": None if ba is None else _d(ba, -4, 2),
                }
            )
            changes.append(
                {
                    "asset_id": POLY_NO,
                    "price": _d(10_000 - p, -4, 2),
                    "size": _d(book[side].get(p, 0), -6, 2),
                    "side": "SELL" if side == B else "BUY",
                    "hash": f"{rng.getrandbits(160):040x}",
                    "best_bid": None if ba is None else _d(10_000 - ba, -4, 2),
                    "best_ask": None if bb is None else _d(10_000 - bb, -4, 2),
                }
            )
        if ev.kind == "take":
            block += rng.randint(1, 2)
            tx = "0x" + keccak256(f"demo-tx:{ev.taker_id}".encode()).hex()
            taker_hash = h(ev.taker_id)
            taker_wallet = wallet(f"taker{rng.randint(1, 5)}")
            bts = (ev.t_ms // 1000) + 2
            makers = []
            coll_tot = tok_tot = 0
            for f in ev.fills:
                m = f.maker
                coll = f.qty * (f.price if not m.no_token else 10_000 - f.price) // 10_000
                tokens = f.qty
                # maker side on its own token: a YES bid is BUY YES, a YES ask is SELL YES; a NO-token order that
                # rests as a YES ask is BUY NO, as a YES bid SELL NO
                if not m.no_token:
                    side_w = 0 if m.side == B else 1
                    token = POLY_YES
                else:
                    side_w = 0 if m.side == A else 1
                    token = POLY_NO
                making, taking = (coll, tokens) if side_w == 0 else (tokens, coll)
                coll_tot += f.qty * f.price // 10_000
                tok_tot += tokens
                logs.append(
                    {
                        "address": POLY_EXCHANGE,
                        "topics": [t_filled, h(m.oid), _addr_topic(wallet(m.owner)), _addr_topic(taker_wallet)],
                        "data": "0x" + "".join(_word(x) for x in (side_w, int(token), making, taking, 0, 0, 0)),
                        "blockNumber": hex(block),
                        "transactionHash": tx,
                        "transactionIndex": "0x1",
                        "blockHash": "0x" + keccak256(f"block:{block}".encode()).hex(),
                        "logIndex": hex(log_index),
                        "removed": False,
                        "blockTimestamp": hex(bts),
                    }
                )
                log_index += 1
                makers.append(
                    {
                        "order_hash": h(m.oid),
                        "price": f.price,
                        "qty": f.qty,
                        "own": m.owner == "own",
                        "no_token": m.no_token,
                        "ahead_before": f.ahead_before,
                    }
                )
            p_last = ev.fills[-1].price
            fee = (
                Decimal(tok_tot).scaleb(-6)
                * Decimal("0.04")
                * Decimal(p_last).scaleb(-4)
                * (1 - Decimal(p_last).scaleb(-4))
            ).quantize(Decimal("0.00001"))
            fee_units = int(fee.scaleb(6))
            taker_side = 0 if ev.side == B else 1
            t_making, t_taking = (coll_tot, tok_tot) if taker_side == 0 else (tok_tot, coll_tot)
            logs.append(
                {
                    "address": POLY_EXCHANGE,
                    "topics": [t_fee, _addr_topic(FEE_RECEIVER)],
                    "data": "0x" + _word(fee_units),
                    "blockNumber": hex(block),
                    "transactionHash": tx,
                    "transactionIndex": "0x1",
                    "blockHash": "0x" + keccak256(f"block:{block}".encode()).hex(),
                    "logIndex": hex(log_index),
                    "removed": False,
                    "blockTimestamp": hex(bts),
                }
            )
            log_index += 1
            for topics, data in (
                (
                    [t_filled, taker_hash, _addr_topic(taker_wallet), _addr_topic(POLY_EXCHANGE)],
                    (taker_side, int(POLY_YES), t_making, t_taking, fee_units, 0, 0),
                ),
                ([t_matched, taker_hash, _addr_topic(taker_wallet)], (taker_side, int(POLY_YES), t_making, t_taking)),
            ):
                logs.append(
                    {
                        "address": POLY_EXCHANGE,
                        "topics": topics,
                        "data": "0x" + "".join(_word(x) for x in data),
                        "blockNumber": hex(block),
                        "transactionHash": tx,
                        "transactionIndex": "0x1",
                        "blockHash": "0x" + keccak256(f"block:{block}".encode()).hex(),
                        "logIndex": hex(log_index),
                        "removed": False,
                        "blockTimestamp": hex(bts),
                    }
                )
                log_index += 1
            matches_truth.append({"tx": tx, "taker_order_hash": taker_hash, "makers": makers})
            by_price: dict[int, int] = {}
            for f in ev.fills:
                by_price[f.price] = by_price.get(f.price, 0) + f.qty
            for p, q in by_price.items():
                put(
                    market,
                    recv,
                    {
                        "event_type": "last_trade_price",
                        "market": POLY_CONDITION,
                        "asset_id": POLY_YES,
                        "price": _d(p, -4, 2),
                        "size": _d(q, -6, 6),
                        "fee_rate_bps": "400",
                        "side": "BUY" if ev.side == B else "SELL",
                        "timestamp": str(ev.t_ms),
                        "transaction_hash": tx,
                    },
                )
                put(
                    market,
                    recv + 1_000,
                    {
                        "event_type": "last_trade_price",
                        "market": POLY_CONDITION,
                        "asset_id": POLY_NO,
                        "price": _d(10_000 - p, -4, 2),
                        "size": _d(q, -6, 6),
                        "fee_rate_bps": "400",
                        "side": "SELL" if ev.side == B else "BUY",
                        "timestamp": str(ev.t_ms),
                        "transaction_hash": tx,
                    },
                )
            own_fills = [f for f in ev.fills if f.maker.owner == "own"]
            for status, dt in (("MATCHED", 0), ("MINED", 2000), ("CONFIRMED", 4000)):
                if not own_fills:
                    break
                put(
                    user,
                    recv + dt * 1_000_000 + 1_000_000,
                    {
                        "event_type": "trade",
                        "type": "TRADE",
                        "id": "trade-" + ev.taker_id,
                        "taker_order_id": taker_hash,
                        "market": POLY_CONDITION,
                        "asset_id": POLY_YES,
                        "side": "BUY" if ev.side == B else "SELL",
                        "size": _d(sum(f.qty for f in ev.fills), -6, 2),
                        "fee_rate_bps": "400",
                        "price": _d(p_last, -4, 2),
                        "status": status,
                        "match_time": str(ev.t_ms // 1000),
                        "last_update": str((ev.t_ms + dt) // 1000),
                        "outcome": "Yes",
                        "owner": OWN_KEY,
                        "trade_owner": OWN_KEY,
                        "maker_address": OWN_WALLET,
                        "transaction_hash": tx if status != "MATCHED" else None,
                        "bucket_index": 0,
                        "maker_orders": [
                            {
                                "order_id": h(f.maker.oid),
                                "owner": OWN_KEY if f.maker.owner == "own" else f"key-{f.maker.owner}",
                                "maker_address": wallet(f.maker.owner),
                                "matched_amount": _d(f.qty, -6, 2),
                                "price": _d(f.price, -4, 2),
                                "fee_rate_bps": "0",
                                "asset_id": POLY_YES,
                                "outcome": "Yes",
                                "outcome_index": 0,
                                "side": "BUY" if f.maker.side == B else "SELL",
                            }
                            for f in ev.fills
                        ],
                        "trader_side": "MAKER",
                        "timestamp": str(ev.t_ms + dt),
                    },
                )
            for f in own_fills:
                st = own_orders[f.maker.oid]
                st["matched"] += f.qty
                put(user, recv + 1_500_000, _poly_order(h(f.maker.oid), f.maker, st, ev.t_ms, "UPDATE"))
        if changes:
            put(
                market,
                recv + 500_000,
                {
                    "event_type": "price_change",
                    "market": POLY_CONDITION,
                    "price_changes": changes,
                    "timestamp": str(ev.t_ms),
                },
            )
        if ev.order is not None and ev.order.owner == "own":
            o = ev.order
            if ev.kind == "add":
                own_orders[o.oid] = {"matched": 0}
                put(user, recv + 1_000_000, _poly_order(h(o.oid), o, own_orders[o.oid], ev.t_ms, "PLACEMENT"))
            elif ev.kind == "cancel":
                put(user, recv + 1_000_000, _poly_order(h(o.oid), o, own_orders[o.oid], ev.t_ms, "CANCELLATION"))
    end = sim["end_ms"]
    put(
        market,
        end * 1_000_000 + 20_000_000,
        {
            "event_type": "market_resolved",
            "id": "123456",
            "market": POLY_CONDITION,
            "assets_ids": [POLY_YES, POLY_NO],
            "winning_asset_id": POLY_NO,
            "winning_outcome": "No",
            "timestamp": str(end + 10),
        },
    )
    market.sort(key=lambda x: (x[0], x[1]))
    user.sort(key=lambda x: (x[0], x[1]))
    # a reorged log and an unrelated Transfer event (both must be skipped)
    logs.append({**logs[0], "logIndex": hex(log_index + 5), "removed": True})
    logs.append(
        {
            "address": "0x" + "c0" * 20,
            "topics": [topic("Transfer(address,address,uint256)"), _addr_topic(OWN_WALLET), _addr_topic(FEE_RECEIVER)],
            "data": "0x" + _word(5),
            "blockNumber": hex(block),
            "transactionHash": "0x" + "ab" * 32,
            "transactionIndex": "0x2",
            "blockHash": "0x" + "cd" * 32,
            "logIndex": hex(log_index + 6),
            "removed": False,
        }
    )
    gamma = {
        "id": "123456",
        "question": "Will the demo event happen?",
        "conditionId": POLY_CONDITION,
        "slug": "will-the-demo-event-happen",
        "version": "v1",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.45", "0.55"]',
        "clobTokenIds": json.dumps([POLY_YES, POLY_NO]),
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
        "negRisk": False,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.04, "exponent": 1, "takerOnly": True, "rebateRate": 0.25},
        "rewardsMinSize": 20,
        "rewardsMaxSpread": 3,
        "clobRewards": [
            {
                "id": "77",
                "conditionId": POLY_CONDITION,
                "assetAddress": "0x" + "9c" * 20,
                "rewardsAmount": 1550,
                "rewardsDailyRate": 50,
                "startDate": "2026-10-01",
                "endDate": "2026-10-31",
            }
        ],
    }
    truth = {
        "venue": "polymarket",
        "condition": POLY_CONDITION,
        "yes": POLY_YES,
        "no": POLY_NO,
        "events": len(sim["events"]),
        "winner": "no",
        "matches": matches_truth,
        "own": sim["own"],
        "final_book": {s: {str(p): q for p, q in lv.items()} for s, lv in sim["final"].items()},
    }
    return {
        "polymarket/market.jsonl": ("\n".join(x[2] for x in market) + "\n").encode(),
        "polymarket/user.jsonl": ("\n".join(x[2] for x in user) + "\n").encode(),
        "polymarket/gamma.json": (json.dumps(gamma, indent=1) + "\n").encode(),
        "polymarket/logs.json": (json.dumps({"jsonrpc": "2.0", "id": 1, "result": logs}, indent=1) + "\n").encode(),
        "polymarket/truth.json": (json.dumps(truth, indent=1, sort_keys=True) + "\n").encode(),
    }


def _poly_order(oid: str, o: Order, st: dict[str, Any], t_ms: int, typ: str) -> dict[str, Any]:
    return {
        "event_type": "order",
        "id": oid,
        "owner": OWN_KEY,
        "market": POLY_CONDITION,
        "asset_id": POLY_YES,
        "side": "BUY" if o.side == B else "SELL",
        "order_owner": OWN_KEY,
        "original_size": _d(o.initial, -6, 2),
        "size_matched": _d(st["matched"], -6, 2),
        "price": _d(o.price, -4, 2),
        "associate_trades": None,
        "outcome": "Yes",
        "type": typ,
        "created_at": str(o.created_ms // 1000),
        "expiration": "0",
        "order_type": "GTC",
        "status": "LIVE" if typ != "CANCELLATION" else "CANCELED",
        "maker_address": OWN_WALLET,
        "timestamp": str(t_ms),
    }


def quotes_csv() -> bytes:
    """Hypothetical quotes for `pm score` on the Kalshi fixture (one bid and one ask every 5 s, 4 s life)."""
    rows = ["quote_id,instrument,side,price,qty,ts_submit,latency_ns,ts_cancel,post_only"]
    t = (T0_MS + 3_000) * 1_000_000
    for k in range(10):
        rows.append(f"q{k:02d}b,{KALSHI_TICKER},buy,0.4400,15,{t},2000000,{t + 4_000_000_000},true")
        rows.append(f"q{k:02d}a,{KALSHI_TICKER},sell,0.4600,15,{t + 1_000_000},2000000,{t + 4_000_000_000},true")
        t += 5_000_000_000
    return ("\n".join(rows) + "\n").encode()


README = """Prediction-market fixtures of `queuetruth pm`. Invented markets, accounts, order ids and values: no venue
data.

Written by `queuetruth pm fixtures` (queuetruth.pm.fixtures) from an exact simulated price-time FIFO book
(individual orders: other traders add near the touch, cancel random resting orders and send takers that sweep the
best levels; a small market maker, the "own" account, joins the best bid and ask, rests a few seconds and cancels),
in the message formats of the public API documentation (docs.kalshi.com and docs.polymarket.com, read 2026-10-24).
Every generation is byte for byte the same (MANIFEST.sha256). Write them with

    queuetruth pm fixtures --out DIR

kalshi/ws.jsonl          WebSocket capture (capture-wrapped): subscribe commands, orderbook_snapshot, orderbook_delta
                         (own changes carry client_order_id), trade, fill, user_order, market_lifecycle_v2
kalshi/rest.jsonl        REST responses: market (price_ranges), series (fee_type), incentive_programs, the
                         queue_positions request and response around every sample (local send and receive times),
                         the fills export
kalshi/truth.json        what no venue publishes: each own order's true queue ahead at entry, at every queue
                         sample and at every fill, and the final book
polymarket/market.jsonl  market channel: book (both tokens), price_change, last_trade_price, market_resolved
polymarket/user.jsonl    user channel: order (PLACEMENT, UPDATE, CANCELLATION) and trade (MATCHED, MINED, CONFIRMED)
polymarket/gamma.json    Gamma market object: clobTokenIds, feeSchedule, rewards settings and clobRewards
polymarket/logs.json     eth_getLogs result of the CTF Exchange V2: OrderFilled per maker in operator order,
                         FeeCharged, the taker's OrderFilled and OrdersMatched; plus one reorged (removed) log and
                         one unrelated Transfer
polymarket/truth.json    every match's makers in operator order with their exact queue ahead, and the own orders'
                         truth as for Kalshi
quotes.csv               hypothetical quotes for `pm score --quotes` on the Kalshi fixture
MANIFEST.sha256          sha256 of every file above (sha256sum format)
"""


def all_files() -> dict[str, bytes]:
    """Every generated fixture file (relative path -> bytes), `MANIFEST.sha256` last."""
    out: dict[str, bytes] = {"README.txt": README.encode()}
    out.update(kalshi())
    out.update(polymarket())
    out["quotes.csv"] = quotes_csv()
    out["MANIFEST.sha256"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {rel}\n" for rel, data in sorted(out.items())
    ).encode()
    return out


def _previous(root: Path) -> bool:
    """True when `root` holds an earlier generation (its README is this module's)."""
    p = root / "README.txt"
    try:
        return p.is_file() and p.read_text(encoding="utf-8").startswith(README.splitlines()[0])
    except (OSError, UnicodeDecodeError):
        return False


def write_fixtures(out: str | Path) -> list[dict[str, Any]]:
    """Writes every fixture file under `out` and returns their `FileEntry` dicts (name, bytes, sha256). `out` must
    not exist, be empty or hold an earlier generation (`QT-CONFIG-001`)."""
    root = Path(out)
    if root.exists() and (not root.is_dir() or (any(root.iterdir()) and not _previous(root))):
        raise QtError("QT-CONFIG-001", f"out: {root} exists and is not empty or an earlier pm fixture")
    files = all_files()
    entries = []
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        entries.append({"name": rel, "rows": None, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return sorted(entries, key=lambda e: e["name"])


__all__ = ["README", "all_files", "kalshi", "polymarket", "quotes_csv", "simulate", "write_fixtures"]
