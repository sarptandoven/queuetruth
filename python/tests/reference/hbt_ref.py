"""Line-by-line Python transliteration of hftbacktest `queue.rs` and the L2 exchange fill rules (O-ZOO-2).

Source: hftbacktest (https://github.com/nkaz001/hftbacktest) at commit 5f3ec40b2afb764e0fea112f941ed85523ef4e88,
`hftbacktest/src/backtest/models/queue.rs` (`RiskAdverseQueueModel`, `ProbQueueModel` with `PowerProbQueueFunc`,
`PowerProbQueueFunc2`, `PowerProbQueueFunc3`, `LogProbQueueFunc`, `LogProbQueueFunc2`) and
`hftbacktest/src/backtest/proc/{nopartialfillexchange.rs, partialfillexchange.rs}` (`check_if_*_filled`, `fill`).
Copyright (c) 2022 nkaz001@protonmail.com, MIT License (full text: third_party/licenses/hftbacktest-MIT.txt).
The Rust-semantics helpers follow the development-time hbt transliteration that matched 49,857 recorded hbt hooks
bit for bit.

Rust semantics reproduced: `f64::min` (NaN loses), `powf` (NaN for a negative base with a non-integer exponent),
`ln` (-inf at 0, NaN below), IEEE division (no exceptions), `f64::round` (half away from zero) and saturating
`as i64` (NaN -> 0).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

FAMILIES = ("hbt_risk_adverse", "hbt_power", "hbt_power2", "hbt_power3", "hbt_log", "hbt_log2")


def f2b(x: float) -> int:
    """f64 bits."""
    return struct.unpack("<Q", struct.pack("<d", x))[0]


def b2f(b: int) -> float:
    """f64 from bits."""
    return struct.unpack("<d", struct.pack("<Q", b))[0]


def rmin(a: float, b: float) -> float:
    """Rust `f64::min`: a NaN argument loses."""
    if math.isnan(a):
        return b
    if math.isnan(b):
        return a
    if a == b == 0.0:
        return a if math.copysign(1.0, a) < 0 else b
    return a if a < b else b


def powf(x: float, n: float) -> float:
    """Rust `powf`."""
    try:
        return math.pow(x, n)
    except ValueError:
        return math.nan
    except OverflowError:
        return math.copysign(math.inf, x) if (n == int(n) and int(n) % 2 == 1) else math.inf


def ln(x: float) -> float:
    """Rust `ln`."""
    if math.isnan(x) or x < 0:
        return math.nan
    if x == 0:
        return -math.inf
    return math.log(x)


def rdiv(a: float, b: float) -> float:
    """IEEE division."""
    if b == 0.0:
        if a == 0.0 or math.isnan(a):
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def rround(x: float) -> float:
    """Rust `f64::round` (half away from zero)."""
    if math.isnan(x) or math.isinf(x):
        return x
    ax = abs(x)
    r = math.floor(ax)
    if ax - r >= 0.5:
        r += 1.0
    return math.copysign(r, x)


def as_i64(x: float) -> int:
    """Rust saturating `as i64`."""
    if math.isnan(x):
        return 0
    if x >= 9.223372036854775807e18:
        return 2**63 - 1
    if x <= -9.223372036854775808e18:
        return -(2**63)
    return int(x)


def prob(family: str, n: float | None, f: float, b: float) -> float:
    """The five `Probability::prob(front, back)` functions (probability that a decrease is behind the order)."""
    if family == "hbt_power":
        return rdiv(powf(b, n), powf(b, n) + powf(f, n))
    if family == "hbt_power2":
        return rdiv(powf(b, n), powf(b + f, n))
    if family == "hbt_power3":
        return 1.0 - powf(rdiv(f, f + b), n)
    if family == "hbt_log":
        return rdiv(ln(1.0 + b), ln(1.0 + b) + ln(1.0 + f))
    if family == "hbt_log2":
        return rdiv(ln(1.0 + b), ln(1.0 + (b + f)))
    raise ValueError(family)


class QueueModel:
    """`RiskAdverseQueueModel` (family `hbt_risk_adverse`) or `ProbQueueModel<P>`; state `[front, cum_trade]`."""

    def __init__(self, family: str, n: float | None, lot: float) -> None:
        self.family, self.n, self.lot = family, n, lot
        self.ra = family == "hbt_risk_adverse"
        self.nan_reset = False

    def new_order(self, q: float) -> list[float]:
        """`new_order`: front = level qty at the order tick."""
        return [q, 0.0]

    def trade(self, s: list[float], qty: float) -> None:
        """`trade`."""
        s[0] -= qty
        if not self.ra:
            s[1] += qty

    def depth(self, s: list[float], prev: float, new: float) -> None:
        """`depth`."""
        if self.ra:
            s[0] = rmin(s[0], new)
            return
        chg = prev - new
        chg -= s[1]
        s[1] = 0.0
        if chg < 0.0:
            s[0] = rmin(s[0], new)
            return
        front = s[0]
        back = prev - front
        p = prob(self.family, self.n, front, back)
        if math.isinf(p):
            p = 1.0
        est = front - (1.0 - p) * chg + rmin(back - p * chg, 0.0)
        if math.isnan(est):
            self.nan_reset = True
        s[0] = rmin(est, new)

    def is_filled(self, s: list[float]) -> float:
        """`is_filled`: `round(-front / lot) as i64` lots."""
        ex = as_i64(rround(rdiv(-s[0], self.lot)))
        if ex > 0:
            s[0] = 0.0
            return float(ex) * self.lot
        return 0.0


class InvalidOrderStatus(Exception):
    """hbt `BacktestError::InvalidOrderStatus` (fill on a Filled / Canceled / Expired order)."""


@dataclass
class Order:
    """One resting own order in an L2 exchange."""

    qty: float
    leaves: float
    status: str = "new"
    removed: bool = False
    fills: list[float] = field(default_factory=list)


class Exchange:
    """`NoPartialFillExchange` (`partial = False`) or `PartialFillExchange` fill rules for one resting order."""

    def __init__(self, partial: bool, lot: float) -> None:
        self.partial, self.lot = partial, lot

    def _fill(self, o: Order, exec_qty: float) -> None:
        if o.status in ("expired", "canceled", "filled"):
            raise InvalidOrderStatus
        o.leaves -= exec_qty
        o.fills.append(exec_qty)
        if self.partial and as_i64(rround(o.leaves / self.lot)) > 0:
            o.status = "partially_filled"
        else:
            o.status = "filled"

    def at_price_trade(self, model: QueueModel, s: list[float], o: Order, qty: float) -> None:
        """`check_if_*_filled`, `Ordering::Equal` branch."""
        model.trade(s, qty)
        filled = model.is_filled(s)
        if not self.partial:
            if filled > 0.0:
                o.removed = True
                exec_qty = o.leaves
                self._fill(o, exec_qty)
            return
        if filled > 0.0:
            if filled > o.leaves:
                o.removed = True
                exec_qty = o.leaves
            else:
                exec_qty = filled
            self._fill(o, exec_qty)

    def full(self, o: Order) -> None:
        """Trade through the price, or the opposite best crossing it: fill the leaves."""
        o.removed = True
        self._fill(o, o.leaves)


def run_sequence(
    family: str, n: float | None, lot: float, qty: float, partial: bool, ops: list[tuple[str, float, float]]
) -> list[dict]:
    """Drives one own order through model-level ops (`arrive` Q, `depth` prev new, `trade` v, `through`, `cross`).

    Returns one dict per op processed: `f_bits`, `t_bits` (None for RiskAdverse), `fill_qty_bits` (0.0 if none),
    `leaves_bits`, `status`, `error`. Processing stops after an error or once the order left the exchange.
    """
    model = QueueModel(family, n, lot)
    exch = Exchange(partial, lot)
    order = Order(qty=qty, leaves=qty)
    s = [0.0, 0.0]
    out = []
    for kind, a, b in ops:
        if order.removed:
            break
        before = len(order.fills)
        err = False
        try:
            if kind == "arrive":
                s = model.new_order(a)
            elif kind == "depth":
                model.depth(s, a, b)
            elif kind == "trade":
                exch.at_price_trade(model, s, order, a)
            elif kind in ("through", "cross"):
                exch.full(order)
            else:
                raise ValueError(kind)
        except InvalidOrderStatus:
            err = True
        fill = order.fills[-1] if len(order.fills) > before else 0.0
        out.append(
            {
                "f_bits": f2b(s[0]),
                "t_bits": None if model.ra else f2b(s[1]),
                "fill_qty_bits": f2b(fill),
                "leaves_bits": f2b(order.leaves),
                "status": order.status,
                "error": err,
            }
        )
        if err:
            break
    return out


# ---- L3 FIFO (hbt `L3FIFOQueueModel` + `L3NoPartialFillExchange`, O-ZOO-4 faithful) -------------------------------


class L3Fifo:
    """Transliteration of `L3FIFOQueueModel` (feed / backtest orders in per-tick FIFO queues) and the
    `L3NoPartialFillExchange` event handling, on books that are never crossed by feed orders (so the feed best is the
    best feed price; hbt's uncrossing of its tracked best does not arise). Ticks are integers; one backtest order
    per run (`len(backtest_orders) == 1` in the tick-range shortcuts of `fill_*_between`)."""

    def __init__(self) -> None:
        self.queues: dict[tuple[str, int], list[list]] = {}
        self.feed: dict[int, tuple[str, int, float]] = {}
        self.backtest: dict[int, tuple[str, int]] = {}
        self.fills: list[tuple[int, int, str]] = []

    def best(self, side: str) -> int | None:
        ticks = [t for (s, t, _q) in self.feed.values() if s == side]
        if not ticks:
            return None
        return max(ticks) if side == "B" else min(ticks)

    def _q(self, side: str, tick: int) -> list[list]:
        return self.queues.setdefault((side, tick), [])

    def _fill_between(self, side: str, lo: int, hi: int, ts: int, why: str, ranged: bool) -> None:
        """`fill_bid_between` / `fill_ask_between`: backtest orders of `side` in [lo, hi] (bid: `>= lo` when not
        ranged; ask: `<= hi` when not ranged)."""
        for oid, (s, t) in list(self.backtest.items()):
            if s != side:
                continue
            hit = (lo <= t <= hi) if ranged else (t >= lo if side == "B" else t <= hi)
            if hit:
                q = self._q(s, t)
                q[:] = [e for e in q if not (e[0] == "bt" and e[1] == oid)]
                del self.backtest[oid]
                self.fills.append((ts, oid, why))

    def _cross_after_best_move(self, side: str, prev: int | None, new: int | None, ts: int) -> None:
        """`on_best_ask_update` -> fill bids `[new, prev - 1]`; `on_best_bid_update` -> fill asks `[prev + 1, new]`."""
        if new is None:
            return
        if side == "A" and (prev is None or new < prev):
            if prev is None or 1 < (prev - 1) - new:
                self._fill_between("B", new, 0, ts, "cross", ranged=False)
            else:
                self._fill_between("B", new, prev - 1, ts, "cross", ranged=True)
        if side == "B" and (prev is None or new > prev):
            if prev is None or 1 < new - (prev + 1):
                self._fill_between("A", 0, new, ts, "cross", ranged=False)
            else:
                self._fill_between("A", prev + 1, new, ts, "cross", ranged=True)

    def add_feed(self, oid: int, side: str, tick: int, qty: float, ts: int) -> None:
        prev = self.best("A" if side == "A" else "B")
        self._q(side, tick).append(["feed", oid, qty])
        self.feed[oid] = (side, tick, qty)
        self._cross_after_best_move(side, prev, self.best(side), ts)

    def cancel_feed(self, oid: int, qty: float) -> None:
        """A cancel record: a full removal deletes the order; a partial one reduces it in place."""
        side, tick, left = self.feed[oid]
        if qty >= left:
            q = self._q(side, tick)
            q[:] = [e for e in q if not (e[0] == "feed" and e[1] == oid)]
            del self.feed[oid]
        else:
            self.modify_feed(oid, tick, left - qty, 0)

    def modify_feed(self, oid: int, tick: int, qty: float, ts: int) -> None:
        side, old_tick, old_qty = self.feed[oid]
        prev = self.best(side)
        q = self._q(side, old_tick)
        i = next(k for k, e in enumerate(q) if e[0] == "feed" and e[1] == oid)
        if old_tick != tick or old_qty < qty:
            q.pop(i)
            self._q(side, tick).append(["feed", oid, qty])
        else:
            q[i][2] = qty
        self.feed[oid] = (side, tick, qty)
        if old_tick != tick:
            self._cross_after_best_move(side, prev, self.best(side), ts)

    def fill_feed(self, oid: int, exec_tick: int, ts: int) -> None:
        """`fill_market_feed_order::<false>`: better-priced backtest orders, then those queued before the order."""
        side, tick, _ = self.feed[oid]
        best = self.best(side)
        if side == "B" and best is not None and exec_tick < best:
            ranged = not (1 < best - (exec_tick + 1))
            self._fill_between("B", exec_tick + 1, best, ts, "through", ranged=ranged)
        if side == "A" and best is not None and exec_tick > best:
            ranged = not (1 < (exec_tick - 1) - best)
            self._fill_between("A", best, exec_tick - 1, ts, "through", ranged=ranged)
        q = self._q(side, tick)
        kept = []
        stop = False
        for e in q:
            if not stop and e[0] == "feed" and e[1] == oid:
                stop = True
            if not stop and e[0] == "bt":
                self.backtest.pop(e[1], None)
                self.fills.append((ts, e[1], "queue"))
                continue
            kept.append(e)
        q[:] = kept

    def submit(self, oid: int, side: str, tick: int, ts: int) -> None:
        """GTX limit: expires when marketable, else joins the tail of its tick's queue."""
        opp = self.best("A" if side == "B" else "B")
        if opp is not None and ((side == "B" and tick >= opp) or (side == "A" and tick <= opp)):
            self.fills.append((ts, oid, "expired"))
            return
        self._q(side, tick).append(["bt", oid, 0.0])
        self.backtest[oid] = (side, tick)
