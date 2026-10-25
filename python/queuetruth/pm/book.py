"""Price-level book on the canonical YES ladder (bids `B`, asks `A`; prices and sizes in fixed-point units).

Kalshi publishes YES bids and NO bids; a NO bid at NO price `q` is a YES ask at `ONE - q` (no-leg pricing, the
`orderbook_delta` default) or at `q` itself when the subscription set `use_yes_price`. Polymarket publishes one
book per outcome token; the YES token's book is the canonical ladder and a NO-token book maps the same way (a NO
bid at `q` is a YES ask at `ONE - q`, a NO ask a YES bid). Everything downstream sees one ladder.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections.abc import Iterator

from queuetruth.errors import QtError

BID, ASK, NONE = ord("B"), ord("A"), ord("N")


def opposite(side: int) -> int:
    """`A` for `B` and back (`N` stays `N`)."""
    return ASK if side == BID else BID if side == ASK else NONE


class Ladder:
    """Aggregated sizes per price on both sides with ordered access (sizes > 0 only)."""

    __slots__ = ("_prices", "_qty")

    def __init__(self) -> None:
        self._qty: dict[int, dict[int, int]] = {BID: {}, ASK: {}}
        self._prices: dict[int, list[int]] = {BID: [], ASK: []}

    def clear(self) -> None:
        """Empties both sides."""
        for s in (BID, ASK):
            self._qty[s].clear()
            self._prices[s].clear()

    def qty(self, side: int, price: int) -> int:
        """Size at a level (0 when empty)."""
        return self._qty[side].get(price, 0)

    def set(self, side: int, price: int, qty: int) -> int:
        """Sets a level's size (0 removes it); returns the previous size."""
        if qty < 0:
            raise QtError("QT-ENGINE-001", f"negative level size {qty} at {chr(side)} {price}")
        book = self._qty[side]
        prev = book.get(price, 0)
        if qty == prev:
            return prev
        prices = self._prices[side]
        if qty == 0:
            del book[price]
            del prices[bisect_left(prices, price)]
        else:
            if prev == 0:
                insort(prices, price)
            book[price] = qty
        return prev

    def best(self, side: int) -> int | None:
        """Best price of a side (highest bid, lowest ask) or None."""
        prices = self._prices[side]
        if not prices:
            return None
        return prices[-1] if side == BID else prices[0]

    def rank(self, side: int, price: int) -> int:
        """Number of levels strictly better than `price` on `side` (0 = best)."""
        prices = self._prices[side]
        if side == BID:
            return len(prices) - bisect_right(prices, price)
        return bisect_left(prices, price)

    def levels(self, side: int) -> Iterator[tuple[int, int]]:
        """`(price, size)` best first."""
        prices = self._prices[side]
        book = self._qty[side]
        seq = reversed(prices) if side == BID else iter(prices)
        for p in seq:
            yield p, book[p]

    def depth(self, side: int, n: int | None = None) -> list[tuple[int, int]]:
        """Up to `n` levels best first (all with None)."""
        out = []
        for i, lv in enumerate(self.levels(side)):
            if n is not None and i >= n:
                break
            out.append(lv)
        return out

    def crossed(self) -> bool:
        """Best bid at or above best ask."""
        b, a = self.best(BID), self.best(ASK)
        return b is not None and a is not None and b >= a

    def snapshot(self) -> dict[int, dict[int, int]]:
        """A copy of both sides."""
        return {BID: dict(self._qty[BID]), ASK: dict(self._qty[ASK])}

    def __len__(self) -> int:
        return len(self._qty[BID]) + len(self._qty[ASK])


__all__ = ["ASK", "BID", "NONE", "Ladder", "opposite"]
