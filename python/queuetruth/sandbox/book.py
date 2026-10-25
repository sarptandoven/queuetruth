"""The agent's market view: the displayed book, best quotes over time and prints, built from exactly the records the
tape released (the same records every replay of the step reads).

`BookView` keeps an order map (`id -> side, price, size`) and price levels (`total`, `count`) with the record
semantics of SPEC 5.1.1 that matter for displayed quantities (priority is irrelevant here, as in `qt_l2`'s
`L2Builder`): `A` adds (an existing id is replaced; undefined price or zero size ignored; side `N` ignored), `C`
reduces or removes, `M` (only when the rule set has modify records) moves the order to its new price and size
(unknown id: an add; size 0: a removal), `R` clears, `F`/`T`/`N` change nothing. At the end of every event (the
`F_LAST` record) it appends the best bid and ask to its history when any of the six values changed, exactly like a
replay's `bbo = change` output; prints are the `T` records with the engine's kind rule (side `N` is `cross` when the
source marks that sequence a cross print, else `hidden` on rule sets with hidden orders, else `displayed`).
python/tests/sandbox/test_book.py checks the history, the depth and the prints against the engine's `bbo.parquet`,
`depth.parquet` and `trades.parquet` on every fixture (and, with the day-one data, on the TSLA partition).

With `starts_after`, it also keeps the time of every event start after that time (`event_starts`, the first record's
`ts_event`, where the engine runs cancels, amends and feed gaps that became due): the finish-time status check needs
the first event after a given time.
"""

from __future__ import annotations

import bisect
import heapq
from array import array
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pyarrow as pa

from queuetruth.sandbox.tape import LAST

UNDEF_PRICE = 2**63 - 1
A, C, M, R, T = (ord(x) for x in "ACMRT")
SIDE_B, SIDE_A, SIDE_N = ord("B"), ord("A"), ord("N")
#: Records converted to Python values at a time (a large first cut is applied in slices of this size).
APPLY_CHUNK_ROWS = 1 << 18


@dataclass
class TradeStatsAcc:
    """Running print statistics (`TradeStats`)."""

    n: int = 0
    volume: int = 0
    notional: int = 0
    high: int | None = None
    low: int | None = None
    last: int | None = None
    first_ts: int | None = None
    last_ts: int | None = None

    def add(self, ts: int, price: int, size: int) -> None:
        """Adds one print."""
        self.n += 1
        self.volume += size
        self.notional += price * size
        self.high = price if self.high is None else max(self.high, price)
        self.low = price if self.low is None else min(self.low, price)
        self.last = price
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts

    def doc(self, price_exp: int) -> dict:
        """`TradeStats` document part (VWAP in real price units)."""
        vwap = None
        if self.volume > 0:
            vwap = float((Decimal(self.notional) / Decimal(self.volume)).scaleb(price_exp))
        return {
            "n": self.n,
            "volume": self.volume,
            "vwap_price": vwap,
            "high": self.high,
            "low": self.low,
            "last": self.last,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }


class BookView:
    """Displayed book, best-quote history and prints of one instrument (module docstring)."""

    def __init__(
        self,
        *,
        instrument_id: int,
        modify_records_present: bool,
        supports_hidden_orders: bool,
        starts_after: int | None = None,
    ) -> None:
        self.instrument_id = instrument_id
        self._starts_after = starts_after
        #: Event start times after `starts_after`, in feed order.
        self.event_starts: array[int] = array("q")
        self.modify_records_present = modify_records_present
        self.supports_hidden_orders = supports_hidden_orders
        self._orders: dict[int, tuple[int, int, int]] = {}
        self._levels: tuple[dict[int, list[int]], dict[int, list[int]]] = ({}, {})
        self._heaps: tuple[list[int], list[int]] = ([], [])
        self._cross: set[int] = set()
        self._key: tuple | None = None
        self._open = False
        self._ev_ts = 0
        self._ev_seq = 0
        # best-quote history (`qt.bbo`, change mode)
        self.h_ts: list[int] = []
        self.h_seq: list[int] = []
        self.h_bid: list[int | None] = []
        self.h_bsz: list[int] = []
        self.h_bct: list[int] = []
        self.h_ask: list[int | None] = []
        self.h_asz: list[int] = []
        self.h_act: list[int] = []
        self.last_event_ts: int | None = None
        self.records = 0
        self.events = 0

    # ---- levels -------------------------------------------------------------------------------------------------

    def _push(self, s: int, price: int, size: int, oid: int) -> None:
        self._orders[oid] = (s, price, size)
        lv = self._levels[s].get(price)
        if lv is None:
            self._levels[s][price] = [size, 1]
            heap = self._heaps[s]
            heapq.heappush(heap, -price if s == 0 else price)
            if len(heap) > 4 * len(self._levels[s]) + 1024:
                self._rebuild(s)
        else:
            lv[0] += size
            lv[1] += 1

    def _rebuild(self, s: int) -> None:
        keys = list(self._levels[s])
        heap = [-p for p in keys] if s == 0 else keys
        heapq.heapify(heap)
        self._heaps[s][:] = heap

    def _remove(self, oid: int) -> None:
        s, price, size = self._orders.pop(oid)
        lv = self._levels[s][price]
        lv[0] -= size
        lv[1] -= 1
        if lv[1] == 0:
            del self._levels[s][price]

    def _best(self, s: int) -> int | None:
        heap, levels = self._heaps[s], self._levels[s]
        while heap:
            p = -heap[0] if s == 0 else heap[0]
            if p in levels:
                return p
            heapq.heappop(heap)
        return None

    def _add(self, side: int, price: int, size: int, oid: int) -> None:
        if price == UNDEF_PRICE or size == 0:
            return
        if side == SIDE_B:
            s = 0
        elif side == SIDE_A:
            s = 1
        else:
            return
        if oid in self._orders:
            self._remove(oid)
        self._push(s, price, size, oid)

    def _cancel(self, oid: int, size: int) -> None:
        o = self._orders.get(oid)
        if o is None:
            return
        s, price, resting = o
        if size < resting:
            self._orders[oid] = (s, price, resting - size)
            self._levels[s][price][0] -= size
        else:
            self._remove(oid)

    def _modify(self, oid: int, side: int, price: int, size: int) -> None:
        o = self._orders.get(oid)
        if o is None:
            self._add(side, price, size, oid)
            return
        s, old_px, old_sz = o
        if size == 0:
            self._remove(oid)
        elif price == old_px and size <= old_sz:
            if size < old_sz:
                self._orders[oid] = (s, price, size)
                self._levels[s][price][0] -= old_sz - size
        else:
            self._remove(oid)
            self._push(s, price, size, oid)

    def _clear(self) -> None:
        self._orders.clear()
        for s in (0, 1):
            self._levels[s].clear()
            self._heaps[s].clear()

    # ---- events -------------------------------------------------------------------------------------------------

    def _level(self, s: int) -> tuple[int | None, int, int]:
        p = self._best(s)
        if p is None:
            return None, 0, 0
        total, count = self._levels[s][p]
        return p, total, count

    def _end_event(self) -> None:
        bp, bs, bc = self._level(0)
        ap, asz, ac = self._level(1)
        key = (bp, bs, bc, ap, asz, ac)
        if key != self._key:
            self._key = key
            self.h_ts.append(self._ev_ts)
            self.h_seq.append(self._ev_seq)
            self.h_bid.append(bp)
            self.h_bsz.append(bs)
            self.h_bct.append(bc)
            self.h_ask.append(ap)
            self.h_asz.append(asz)
            self.h_act.append(ac)
        self.last_event_ts = self._ev_ts
        self.events += 1
        self._open = False

    def add_cross_prints(self, prints: pa.Table | None) -> None:
        """Registers the sequences of the source prints marked `cross` (ITCH Q, OC-28)."""
        if prints is None or prints.num_rows == 0:
            return
        kinds = prints.column("kind").cast(pa.utf8()).to_pylist()
        seqs = prints.column("sequence").to_pylist()
        iids = prints.column("instrument_id").to_pylist()
        for k, q, i in zip(kinds, seqs, iids, strict=True):
            if k == "cross" and int(i) == self.instrument_id:
                self._cross.add(int(q))

    def apply(self, records: pa.Table | None) -> list[tuple[int, int, int, str, str]]:
        """Applies released `qt.mbo` records; returns the new prints `(ts, price, size, aggressor, kind)`."""
        if records is None or records.num_rows == 0:
            return []
        prints: list[tuple[int, int, int, str, str]] = []
        for k in range(0, records.num_rows, APPLY_CHUNK_ROWS):
            self._apply(records.slice(k, APPLY_CHUNK_ROWS), prints)
        return prints

    def _apply(self, records: pa.Table, prints: list[tuple[int, int, int, str, str]]) -> None:
        cols = {
            name: records.column(name).to_numpy() for name in ("action", "side", "price", "size", "order_id", "flags")
        }
        ts = records.column("ts_event").cast(pa.int64()).to_numpy()
        seq = records.column("sequence").to_numpy()
        rows = zip(
            cols["action"].tolist(),
            cols["side"].tolist(),
            cols["price"].tolist(),
            cols["size"].tolist(),
            cols["order_id"].tolist(),
            cols["flags"].tolist(),
            np.asarray(ts).tolist(),
            np.asarray(seq).tolist(),
            strict=True,
        )
        modify = self.modify_records_present
        hidden_ok = self.supports_hidden_orders
        after = self._starts_after
        starts = self.event_starts
        for act, side, price, size, oid, flags, t, q in rows:
            self.records += 1
            if not self._open and after is not None and t > after:
                starts.append(t)
            self._open = True
            self._ev_ts = t
            self._ev_seq = q
            if act == A:
                self._add(side, price, size, oid)
            elif act == C:
                self._cancel(oid, size)
            elif act == M:
                if modify:
                    self._modify(oid, side, price, size)
            elif act == R:
                self._clear()
            elif act == T:
                if side == SIDE_N:
                    kind = "cross" if q in self._cross else ("hidden" if hidden_ok else "displayed")
                else:
                    kind = "displayed"
                agg = "B" if side == SIDE_B else ("A" if side == SIDE_A else "N")
                prints.append((t, price, size, agg, kind))
            if flags & LAST:
                self._end_event()

    def end_partial_event(self) -> None:
        """Ends a trailing event that has no `F_LAST` record (end of data; the engine ends it synthetically)."""
        if self._open:
            self._end_event()

    # ---- queries ------------------------------------------------------------------------------------------------

    def best(self) -> tuple[tuple[int, int, int] | None, tuple[int, int, int] | None]:
        """Best bid and ask as `(price, size, count)`."""
        out = []
        for s in (0, 1):
            p, total, count = self._level(s)
            out.append(None if p is None else (p, total, count))
        return out[0], out[1]

    def depth(self, n: int) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
        """Top `n` levels per side, best first."""
        bids = heapq.nlargest(n, self._levels[0])
        asks = heapq.nsmallest(n, self._levels[1])
        return (
            [(p, self._levels[0][p][0], self._levels[0][p][1]) for p in bids],
            [(p, self._levels[1][p][0], self._levels[1][p][1]) for p in asks],
        )

    def level_qty(self, side: str, price: int) -> int:
        """Displayed quantity at a price (`side` `buy`/`sell` = bid/ask)."""
        lv = self._levels[0 if side == "buy" else 1].get(price)
        return 0 if lv is None else lv[0]

    def quote_at(self, t: int) -> int:
        """Index of the last best-quote row with `ts <= t` (-1 if none)."""
        return bisect.bisect_right(self.h_ts, t) - 1

    def first_event_after(self, t: int) -> int | None:
        """Start time of the first recorded event starting after `t` (None: none in view). Exact when the data's
        timestamps never decrease (the tape counts decreases)."""
        i = bisect.bisect_right(self.event_starts, t)
        return self.event_starts[i] if i < len(self.event_starts) else None

    def quote_before(self, t: int) -> int:
        """Index of the last best-quote row with `ts < t` (-1 if none)."""
        return bisect.bisect_left(self.h_ts, t) - 1

    def mid_x2(self, i: int) -> int | None:
        """`bid + ask` of history row `i` (None when a side is empty or `i < 0`)."""
        if i < 0:
            return None
        b, a = self.h_bid[i], self.h_ask[i]
        if b is None or a is None:
            return None
        return int(b) + int(a)

    def bbo_table(self) -> pa.Table:
        """The best-quote history as a `qt.bbo` table (change mode)."""
        from queuetruth.score._io import make_table

        n = len(self.h_ts)
        return make_table(
            "qt.bbo",
            {
                "ts_event": self.h_ts,
                "instrument_id": [self.instrument_id] * n,
                "sequence": self.h_seq,
                "bid_px": self.h_bid,
                "bid_sz": self.h_bsz,
                "bid_ct": self.h_bct,
                "ask_px": self.h_ask,
                "ask_sz": self.h_asz,
                "ask_ct": self.h_act,
            },
        )


__all__ = ["BookView", "TradeStatsAcc"]
