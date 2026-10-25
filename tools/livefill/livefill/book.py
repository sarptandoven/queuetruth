"""Order-by-order book of one Bitstamp market, replayed from a seed snapshot and `live_orders` events.

Priority rules (the `crypto_l3` rule set that `queuetruth ingest --format bitstamp` applies): an order joins the
back of its price level; a change keeps its place only when the price is unchanged and the amount does not grow;
any other change re-queues it at the back of its (new) level. Prices and amounts are fixed-point integers
(`units.PRICE_EXP`, `units.SIZE_EXP`).

This is an independent Python replay (it shares no code with the Rust converter), used to check a capture against
the venue's own `detail_order_book` snapshots (`check.py`) and to read exact queue positions of the user's orders
(`matcher.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

BUY = "buy"
SELL = "sell"


def side_of(order_type: object) -> str | None:
    """Bitstamp `order_type` / trade `type`: 0 buy, 1 sell."""
    if order_type in (0, "0"):
        return BUY
    if order_type in (1, "1"):
        return SELL
    return None


@dataclass
class Resting:
    side: str
    px: int
    amt: int
    #: venue microtimestamp of the event that queued the order (None for seeded orders)
    us: int | None = None


@dataclass
class Ahead:
    """Queue position of a resting order: orders and quantity ahead of it at its price, the level's rank from the
    touch (0 = best price) and the quantity resting at strictly better prices of the same side."""

    orders: int
    qty: int
    level_rank: int
    better_qty: int


class L3Book:
    """One market's book: `orders` by id and, per side and price, the ids in priority order (dict order).

    `id_ties`: orders queued at the same price in the same venue microtimestamp are ranked by order id, not by
    message arrival (Bitstamp's ids increase with entry time, and same-millisecond messages can arrive out of
    priority order)."""

    def __init__(self, id_ties: bool = True) -> None:
        self.id_ties = id_ties
        self.orders: dict[int, Resting] = {}
        self.levels: dict[str, dict[int, dict[int, None]]] = {BUY: {}, SELL: {}}
        self.counters: dict[str, int] = {
            "created_duplicate": 0,
            "changed_unknown": 0,
            "deleted_unknown": 0,
            "requeued": 0,
        }

    def _insert(self, oid: int, side: str, px: int, amt: int, us: int | None = None) -> None:
        self.orders[oid] = Resting(side, px, amt, us)
        lv = self.levels[side].setdefault(px, {})
        if us is None or not self.id_ties:
            lv[oid] = None
            return
        tail: list[int] = []
        for o in reversed(lv):
            r = self.orders[o]
            if r.us != us or o < oid:
                break
            tail.append(o)
        if not tail:
            lv[oid] = None
            return
        for o in tail:
            del lv[o]
        lv[oid] = None
        for o in reversed(tail):
            lv[o] = None

    def _remove(self, oid: int) -> Resting | None:
        r = self.orders.pop(oid, None)
        if r is None:
            return None
        lv = self.levels[r.side][r.px]
        del lv[oid]
        if not lv:
            del self.levels[r.side][r.px]
        return r

    def seed(self, entries: list[tuple[str, int, int, int]]) -> None:
        """Replaces the book with snapshot entries `(side, px, amt, oid)` given in priority order."""
        self.orders.clear()
        self.levels = {BUY: {}, SELL: {}}
        for side, px, amt, oid in entries:
            if oid in self.orders:
                self._remove(oid)
            self._insert(oid, side, px, amt)

    def created(self, oid: int, side: str, px: int, amt: int, us: int | None = None) -> None:
        if oid in self.orders:
            self.counters["created_duplicate"] += 1
            self.changed(oid, side, px, amt, us)
            return
        self._insert(oid, side, px, amt, us)

    def changed(self, oid: int, side: str, px: int, amt: int, us: int | None = None) -> None:
        r = self.orders.get(oid)
        if r is None:
            self.counters["changed_unknown"] += 1
            self._insert(oid, side, px, amt, us)
            return
        if r.side == side and r.px == px and amt <= r.amt:
            r.amt = amt
            return
        self.counters["requeued"] += 1
        self._remove(oid)
        self._insert(oid, side, px, amt, us)

    def deleted(self, oid: int) -> Resting | None:
        r = self._remove(oid)
        if r is None:
            self.counters["deleted_unknown"] += 1
        return r

    def prices(self, side: str) -> list[int]:
        """Prices of a side from the touch outwards."""
        return sorted(self.levels[side], reverse=(side == BUY))

    def best(self, side: str) -> int | None:
        lv = self.levels[side]
        if not lv:
            return None
        return max(lv) if side == BUY else min(lv)

    def top(self, side: str, n: int) -> list[tuple[int, int, int]]:
        """The best `n` orders of a side as `(px, amt, oid)` in priority order."""
        out: list[tuple[int, int, int]] = []
        for px in self.prices(side):
            for oid in self.levels[side][px]:
                out.append((px, self.orders[oid].amt, oid))
                if len(out) == n:
                    return out
        return out

    def ahead(self, oid: int) -> Ahead | None:
        """Queue position of a resting order (None when it is not on the book)."""
        r = self.orders.get(oid)
        if r is None:
            return None
        n = q = 0
        for o in self.levels[r.side][r.px]:
            if o == oid:
                break
            n += 1
            q += self.orders[o].amt
        prices = self.prices(r.side)
        rank = prices.index(r.px)
        better = sum(self.orders[o].amt for p in prices[:rank] for o in self.levels[r.side][p])
        return Ahead(orders=n, qty=q, level_rank=rank, better_qty=better)

    def level_qty(self, side: str, px: int) -> int:
        return sum(self.orders[o].amt for o in self.levels[side].get(px, {}))
