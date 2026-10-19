"""The counterfactual matching engine, shadow lifecycle and the per-partition runner.

How a shadow is evaluated (static counterfactual):
1. The tape book (`tape.TapeBook`) follows the feed. At the start of the first event with `ts_event > ts_live` the
   oracle copies it into a private `ForkBook` and inserts the shadow there as a real displayed limit order at the
   tail of its price level (if it is marketable on arrival it first trades as a taker against the copied opposite
   side at the resting prices).
2. Every later record is replayed into the fork:
   - `A` / `C` / `M` of real orders are applied as the feed says, with the venue's priority facts. An `A` (or a
     price-changing `M`) that would trade only because the shadow is there (it does not lock or cross the tape book)
     is matched against the fork as an incoming marketable order. One that locks or crosses the tape book itself
     shows a non-continuous market (auction, pre-open, feed artefact) and rests unmatched, as on the tape.
   - Every execution on the tape becomes taker flow: an `F` of `q` on a resting order at price `r` delivers `q`
     units of incoming volume to that side with a limit of `r` (extended to the worst price of the current sweep).
     The fork matches that volume in price / display / time priority, so the shadow receives exactly the volume
     that would have reached it. Prints are used only to find the execution price and hidden executions.
   - A side-N print without `F` on a venue with hidden orders is a non-displayed execution. When its resting side
     is known (print side, or the open sweep) and the tape shows no displayed interest at or better than its price,
     a just-in-time hidden order is placed there and the print's volume is matched: displayed orders (the shadow
     included) at that price trade first. Otherwise the print is ambiguous and is not used (noted).
3. The shadow's fills, its exact queue ahead after every event and notes about audit-relevant tape situations are
   recorded. Taker volume that the fork cannot place (because it already diverged from the tape) is counted.

Own orders (ruling A1, `exclude_order_ids`). An excluded order is the user's own live order on the tape and the
shadow that stands for it is the user's order itself. The shadow that enters at the order's `A` / `M` record (same
side and abs price, `ts_live` equal to the record's `ts_event`, or 1 ns before it when the shadow went live at the
start of that record's event; equal quantity first, then input order) is its twin's shadow. The counterfactual for
that shadow is "the user's order, placed exactly where it was": its fork is copied from the tape just before the
record, the shadow joins the tail there, the order's own `A` / `C` / `M` records never reach that fork, and every
execution of the order is the shadow's own execution at the execution price. For every other shadow the order is
a real resting order and stays on its tape. An excluded order no shadow enters at is removed from the tape for
everyone (its executions remain taker flow), as is any order excluded with `twin_linking` off.

The taker flow is fixed: the engine cannot model takers that would have behaved differently because the shadow
was there (a static counterfactual).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Any

from .tape import (
    FLAG_MAYBE_BAD_BOOK,
    UNDEF_PRICE,
    InstrumentMeta,
    Rec,
    TapeBook,
    at_or_better,
    group_events,
    opposite,
    strictly_better,
)
from .venue import VenueFacts, venue_facts

SHADOW = -1
JIT_HIDDEN = -2
NS = 1_000_000_000

CATEGORIES = (
    "hidden_prints",
    "off_price_executions",
    "crosses",
    "cme_modify",
    "own_order_exclusion",
    "oracle_divergence",
)


@dataclass
class ShadowSpec:
    shadow_id: str
    instrument_id: int
    side: str  # "B" | "A"
    qty: int
    ts_submit: int
    price: int | None = None
    price_mode: str = "abs"
    offset_ticks: int = 0
    latency_ns: int = 0
    ts_cancel: int | None = None


@dataclass
class Amend:
    shadow_id: str
    ts_send: int
    new_qty: int


@dataclass
class OracleConfig:
    match_mode: str = "fifo"  # "fifo" (pure price/display/time) | "directed" (tape's maker choice kept, shadow first)
    sweep_gap_ns: int = 0  # executions on one resting side within this gap (and no other book change) form a sweep
    exclude_order_ids: frozenset[int] = frozenset()  # ruling A1: the user's own orders (see the module docstring)
    # Ruling A1 per-shadow twins: an excluded order stays on the tape for every shadow except the one that enters at
    # its record. False removes every excluded order from the tape for everyone.
    twin_linking: bool = True
    # Side-N prints strictly inside the tape spread on the tick grid have no observable resting side. With
    # "infer_from_shadow" the print's volume is offered to the shadow's side when the shadow rests at or through
    # the print price (a non-displayed bid hit there, or a non-displayed ask that crossed the shadow, both trade
    # with the shadow first). "strict" uses only the print side or an open sweep.
    hidden_side_policy: str = "infer_from_shadow"
    max_notes_per_shadow: int = 400


class FOrder:
    __slots__ = ("hidden", "oid", "price", "side", "size", "stamp")

    def __init__(self, oid: int, side: str, price: int, size: int, stamp: int, hidden: bool = False) -> None:
        self.oid = oid
        self.side = side
        self.price = price
        self.size = size
        self.stamp = stamp
        self.hidden = hidden


class ShadowState:
    """Everything the oracle records for one shadow."""

    def __init__(self, spec: ShadowSpec, ts_live: int, index: int = 0) -> None:
        self.spec = spec
        self.ts_live = ts_live
        self.index = index  # input order (twin tie-break)
        self.started = False  # left the pending list (activated, or ended before going live)
        self.twin: int | None = None  # the excluded order this shadow stands for (ruling A1)
        self.qty = spec.qty
        self.price: int | None = spec.price if spec.price_mode == "abs" else None
        self.filled = 0
        self.fills: list[dict[str, Any]] = []
        self.trajectory: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []
        self._note_keys: set = set()
        self.end_state: str | None = None
        self.ts_end: int | None = None
        self.ts_activated_event: int | None = None
        self.ts_cancel_effective: int | None = None
        self.queue_ahead_at_live: int | None = None
        self.rank_at_live: int | None = None
        self.level_qty_at_live: int | None = None
        self.diag = {"unmatched_exec_volume": 0, "unmatched_exec_volume_other_side": 0, "ahead_divergence_events": 0}
        self.max_notes = 400

    @property
    def remaining(self) -> int:
        return self.qty - self.filled

    def note(self, ts: int, category: str, detail: str) -> None:
        key = (ts, category, detail)
        if key in self._note_keys:
            return
        self._note_keys.add(key)
        if len(self.notes) < self.max_notes:
            self.notes.append({"ts_event": ts, "category": category, "detail": detail})

    def result(self) -> dict[str, Any]:
        s = self.spec
        fills = self.fills
        first = fills[0] if fills else None
        return {
            "shadow_id": s.shadow_id,
            "instrument_id": s.instrument_id,
            "side": s.side,
            "price": self.price,
            "qty": s.qty,
            "qty_final": self.qty,
            "ts_submit": s.ts_submit,
            "ts_live": self.ts_live,
            "ts_activated_event": self.ts_activated_event,
            "ts_cancel_effective": self.ts_cancel_effective,
            "queue_ahead_at_live": self.queue_ahead_at_live,
            "rank_at_live": self.rank_at_live,
            "level_qty_at_live": self.level_qty_at_live,
            "filled_qty": self.filled,
            "n_fills": len(fills),
            "ts_first_fill": first["ts_event"] if first else None,
            "ts_last_fill": fills[-1]["ts_event"] if fills else None,
            "ts_full_fill": fills[-1]["ts_event"] if fills and self.filled >= self.qty else None,
            "first_fill_reason": first["reason"] if first else "never",
            "first_fill_liquidity": first["liquidity"] if first else None,
            "queue_ahead_at_first_fill": first["queue_ahead_before"] if first else None,
            "end_state": self.end_state,
            "ts_end": self.ts_end,
            "fills": fills,
            "trajectory": self.trajectory,
            "notes": self.notes,
            "diag": self.diag,
        }


class ForkBook:
    """A private copy of the tape book with one shadow inserted as a real order; a minimal price/display/time
    priority matching engine."""

    def __init__(self, tape: TapeBook, st: ShadowState, facts: VenueFacts, cfg: OracleConfig) -> None:
        self.st = st
        self.facts = facts
        self.cfg = cfg
        self.orders: dict[int, FOrder] = {}
        # side -> price -> [displayed {oid: FOrder}, hidden {oid: FOrder}]
        self.levels: dict[str, dict[int, list[dict[int, FOrder]]]] = {"B": {}, "A": {}}
        self.prices: dict[str, list[int]] = {"B": [], "A": []}
        self.stamp = 0
        for side in ("B", "A"):
            for px in tape.prices[side]:
                for o in tape.levels[side][px].values():
                    self._insert(FOrder(o.oid, side, px, o.size, self._next()))
        self.shadow: FOrder | None = None
        self.done = False
        self.ev_ts = 0
        self.ev_seq = 0
        self.qa_last = 0
        self.rank_last = 0
        self.filled_last = 0
        self.dirty = False
        self.diverged = False
        self.act_ev = 0  # event number (PartitionRunner) the shadow went live in
        self.twins: set[int] = set()  # own excluded orders: their records never reach this fork

    def _next(self) -> int:
        self.stamp += 1
        return self.stamp

    # ----- structure
    def _insert(self, o: FOrder) -> None:
        side_lv = self.levels[o.side]
        lv = side_lv.get(o.price)
        if lv is None:
            lv = side_lv[o.price] = [{}, {}]
            bisect.insort(self.prices[o.side], o.price)
        lv[1 if o.hidden else 0][o.oid] = o
        self.orders[o.oid] = o

    def _unlink(self, o: FOrder) -> None:
        lv = self.levels[o.side][o.price]
        lv[1 if o.hidden else 0].pop(o.oid, None)
        if not lv[0] and not lv[1]:
            del self.levels[o.side][o.price]
            p = self.prices[o.side]
            del p[bisect.bisect_left(p, o.price)]
        if self.orders.get(o.oid) is o:
            del self.orders[o.oid]

    def best(self, side: str) -> int | None:
        p = self.prices[side]
        if not p:
            return None
        return p[-1] if side == "B" else p[0]

    def _walk_prices(self, side: str, limit: int) -> list[int]:
        """Prices on resting `side` reachable by a taker with `limit`, best first."""
        p = self.prices[side]
        if side == "B":
            i = bisect.bisect_left(p, limit)
            return p[i:][::-1]
        i = bisect.bisect_right(p, limit)
        return p[:i]

    # ----- matching
    def match(self, side: str, qty: int, limit: int, on_take) -> int:
        """Incoming taker volume `qty` with `limit` against resting `side`; `on_take(order, take, price)` per
        execution. Returns the volume left unmatched."""
        for px in self._walk_prices(side, limit):
            lv = self.levels[side].get(px)
            if lv is None:
                continue
            for d in (lv[0], lv[1]):
                emptied = []
                for o in d.values():
                    take = o.size if o.size < qty else qty
                    if take <= 0:
                        continue
                    o.size -= take
                    qty -= take
                    on_take(o, take, px)
                    if o.size == 0:
                        emptied.append(o)
                    if qty == 0:
                        break
                for o in emptied:
                    self._unlink(o)
                if qty == 0:
                    return 0
        return qty

    # ----- shadow
    def shadow_live(self) -> bool:
        return self.shadow is not None and not self.done

    def _shadow_fill(
        self, take: int, px: int, liquidity: str, reason: str, source: str, ts: int, seq: int | None
    ) -> None:
        st = self.st
        st.filled += take
        st.fills.append(
            {
                "ts_event": ts,
                "sequence": seq,
                "fill_qty": take,
                "cum_qty": st.filled,
                "leaves_qty": st.qty - st.filled,
                "fill_px": px,
                "liquidity": liquidity,
                "reason": reason,
                "source": source,
                "queue_ahead_before": self.qa_last,
            }
        )
        self.dirty = True

    def _maker_cb(self, ref_px: int | None, source: str):
        """Callback for taker volume hitting resting orders: records the shadow's maker fills."""
        st = self.st

        def cb(o: FOrder, take: int, px: int) -> None:
            if o.oid != SHADOW:
                return
            if source == "cross_add":
                reason = "crossed"
            elif ref_px is None or ref_px == px:
                reason = "queue_depleted"
            elif strictly_better(o.side, px, ref_px):
                reason = "trade_through"
            else:
                reason = "queue_depleted"
                st.note(self.ev_ts, "oracle_divergence", "shadow_matched_by_volume_named_at_better_price")
            self._shadow_fill(take, px, "maker", reason, source, self.ev_ts, self.ev_seq)

        return cb

    def activate(self, tape: TapeBook, meta: InstrumentMeta, ts_event: int) -> None:
        st = self.st
        sp = st.spec
        side = sp.side
        st.ts_activated_event = ts_event
        if sp.price_mode == "touch":
            b = tape.best(side)
            if b is None:
                self._end("rejected_no_touch", ts_event)
                return
            step = sp.offset_ticks * meta.tick_size
            px = b + step if side == "B" else b - step
            st.price = px
            if sp.offset_ticks > 0 and tape.crosses(side, px):
                self._end("rejected_would_cross", ts_event)
                return
        px = st.price
        st.level_qty_at_live = tape.level_total(side, px)
        st.queue_ahead_at_live = st.level_qty_at_live
        st.rank_at_live = len(tape.levels[side].get(px, {}))
        # Marketable on arrival: the shadow is the taker, at the resting prices.
        opp = opposite(side)
        ob = self.best(opp)
        if ob is not None and at_or_better(side, px, ob):
            st.note(st.ts_live, "crosses", "crossed_at_live" + ("_tape_crossed" if tape.is_crossed() else ""))

            def taker_cb(o: FOrder, take: int, lpx: int) -> None:
                self._shadow_fill(take, lpx, "taker", "crossed_at_live", "at_live", st.ts_live, None)

            self.qa_last = 0
            self.match(opp, st.qty, px, taker_cb)
        if st.remaining <= 0:
            self._end("filled", st.ts_live)
            st.trajectory.append(self._traj_row(st.ts_live, None, "live", 0, 0, 0, 0))
            return
        self.shadow = FOrder(SHADOW, side, px, st.remaining, self._next())
        self._insert(self.shadow)
        qa, rank = self.queue_ahead()
        st.queue_ahead_at_live = qa
        st.rank_at_live = rank
        self.qa_last, self.rank_last, self.filled_last = qa, rank, st.filled
        st.trajectory.append(self._traj_row(st.ts_live, None, "live", qa, qa, rank, rank))

    def _traj_row(self, ts: int, seq: int | None, kind: str, qa_b: int, qa_a: int, rk_b: int, rk_a: int) -> dict:
        return {
            "ts_event": ts,
            "sequence": seq,
            "kind": kind,
            "queue_ahead_before": qa_b,
            "queue_ahead_after": qa_a,
            "rank_before": rk_b,
            "rank_after": rk_a,
            "filled_qty": self.st.filled,
        }

    def queue_ahead(self) -> tuple[int, int]:
        sh = self.shadow
        lv = self.levels[sh.side].get(sh.price)
        qa = rank = 0
        if lv is None:
            return 0, 0
        for o in lv[0].values():
            if o is sh:
                break
            if o.size > 0:
                qa += o.size
                rank += 1
        return qa, rank

    def _end(self, state: str, ts: int) -> None:
        if self.done:
            return
        self.done = True
        self.st.end_state = state
        self.st.ts_end = ts
        if self.shadow is not None and self.orders.get(SHADOW) is self.shadow:
            self._unlink(self.shadow)

    def end(self, state: str, ts: int) -> None:
        if self.shadow is not None and not self.done:
            self.st.trajectory.append(
                self._traj_row(ts, None, "end", self.qa_last, self.qa_last, self.rank_last, self.rank_last)
            )
        self._end(state, ts)

    def _check_filled(self) -> None:
        if self.shadow is not None and not self.done and self.st.remaining <= 0:
            self.event_end_rows(force=True)
            self.st.trajectory.append(self._traj_row(self.ev_ts, self.ev_seq, "end", 0, 0, 0, 0))
            self._end("filled", self.ev_ts)

    # ----- record application
    def _place(self, oid: int, side: str, px: int, size: int, crosses_tape: bool, tape: TapeBook) -> None:
        """An order arriving on the book (A, or M that lost priority)."""
        old = self.orders.get(oid)
        if old is not None:
            self._unlink(old)
        st = self.st
        opp = opposite(side)
        ob = self.best(opp)
        if ob is not None and at_or_better(side, px, ob):
            if crosses_tape:
                if self.shadow_live() and st.spec.side == opp and at_or_better(side, px, self.shadow.price):
                    st.note(self.ev_ts, "crosses", "tape_crossing_add_rests_unmatched")
            else:
                size = self.match(opp, size, px, self._maker_cb(None, "cross_add"))
                if self.shadow_live() and st.spec.side == opp:
                    st.note(self.ev_ts, "crosses", "add_marketable_only_against_shadow")
                self._check_filled()
        if size > 0:
            self._insert(FOrder(oid, side, px, size, self._next()))
        if self.shadow_live() and side == st.spec.side and px == self.shadow.price:
            self.dirty = True

    def on_add(self, r: Rec, crosses_tape: bool, tape: TapeBook) -> None:
        self._place(r.order_id, r.side, r.price, r.size, crosses_tape, tape)

    def on_cancel(self, oid: int, cancel_part: int, full_remove: bool) -> None:
        o = self.orders.get(oid)
        if o is None:
            return
        if self.shadow_live() and o.side == self.shadow.side and o.price == self.shadow.price:
            self.dirty = True
        if full_remove:
            self._unlink(o)
        elif cancel_part > 0:
            o.size -= min(cancel_part, o.size)
            if o.size == 0:
                self._unlink(o)

    def on_modify(
        self,
        oid: int,
        side: str,
        new_px: int,
        new_size: int,
        keeps: bool,
        tape_px: int,
        tape_size: int,
        pending: int,
        crosses_tape: bool,
        tape: TapeBook,
    ) -> None:
        o = self.orders.get(oid)
        fork_size = o.size if o is not None else 0
        new_fork = max(0, new_size + (fork_size - (tape_size - pending)))
        st = self.st
        if self.shadow_live() and side == st.spec.side and self.shadow.price in (tape_px, new_px):
            self.dirty = True
            base = tape_size - pending
            if keeps:
                detail = "priority_kept"
            elif new_px != tape_px:
                detail = "price_change_lost_priority"
            elif new_size == tape_size and pending == 0:
                detail = "same_size_lost_priority"
            elif pending > 0 and new_size > base:
                detail = "size_above_post_fill_remaining_lost_priority"
            else:
                detail = "size_increase_lost_priority"
            ahead = o is not None and o.price == self.shadow.price and o.side == side and o.stamp < self.shadow.stamp
            st.note(self.ev_ts, "cme_modify", detail + ("_ahead" if ahead else "_behind_or_new"))
        if keeps:
            if o is not None:
                if new_fork == 0:
                    self._unlink(o)
                else:
                    o.size = new_fork
            return
        if o is not None:
            self._unlink(o)
        if new_fork > 0:
            self._place(oid, side, new_px, new_fork, crosses_tape, tape)

    def on_exec(
        self, side: str, resting_px: int, exec_px: int, oid: int, qty: int, limit: int, printed: bool, excluded: bool
    ) -> None:
        st = self.st
        live = self.shadow_live()
        on_my_side = live and side == st.spec.side
        if on_my_side:
            p = self.shadow.price
            relevant = resting_px == p or at_or_better(side, p, exec_px) or at_or_better(side, p, resting_px)
            if relevant and (not printed or exec_px != resting_px):
                st.note(
                    self.ev_ts,
                    "off_price_executions",
                    "non_printable_execution" if not printed else "execution_price_differs_from_resting",
                )
            if excluded:
                st.note(self.ev_ts, "own_order_exclusion", "own_order_execution_reaches_book")
            self.dirty = True
        cb = self._maker_cb(resting_px, "exec")
        left = qty
        if strictly_better(side, exec_px, resting_px):  # printed or not (ITCH C non-printable too, A4b)
            # Price-improved execution (ITCH "executed with price" better than the display price): the order
            # really rested at the execution price (e.g. a price-slid order displayed one tick away), so it ranked
            # ahead of everything displayed at its display price. Only a shadow priced strictly better than the
            # execution price would have traded first.
            cb = self._maker_cb(exec_px, "exec")
            sh = self.shadow
            if on_my_side and strictly_better(side, sh.price, exec_px):
                take = min(left, sh.size)
                sh.size -= take
                left -= take
                cb(sh, take, sh.price)
                if sh.size == 0:
                    self._unlink(sh)
            o = self.orders.get(oid)
            if left > 0 and o is not None and o.side == side and not o.hidden:
                take = min(left, o.size)
                o.size -= take
                left -= take
                if o.size == 0:
                    self._unlink(o)
            limit = exec_px
        elif self.cfg.match_mode == "directed":
            left = self._directed(side, oid, qty, cb)
        if left > 0:
            left = self.match(side, left, limit, cb)
        if left > 0:
            if on_my_side:
                st.diag["unmatched_exec_volume"] += left
                st.note(self.ev_ts, "oracle_divergence", "taker_volume_unmatched_in_fork")
            else:  # e.g. opposite orders the shadow took as a taker on arrival: irrelevant to the shadow
                st.diag["unmatched_exec_volume_other_side"] += left
        self._check_filled()

    def _directed(self, side: str, oid: int, qty: int, cb) -> int:
        o = self.orders.get(oid)
        if o is None or o.side != side or o.hidden:
            return qty
        sh = self.shadow
        if (
            self.shadow_live()
            and sh.side == side
            and (strictly_better(side, sh.price, o.price) or (sh.price == o.price and sh.stamp < o.stamp))
        ):
            take = min(qty, sh.size)
            if take > 0:
                sh.size -= take
                qty -= take
                cb(sh, take, sh.price)
                if sh.size == 0:
                    self._unlink(sh)
        take = min(qty, o.size)
        if take > 0:
            o.size -= take
            qty -= take
            if o.size == 0:
                self._unlink(o)
        return qty

    def drop_twin(self, oid: int) -> None:
        """The order `oid` is this shadow's own (ruling A1): it is never part of this fork."""
        self.twins.add(oid)
        o = self.orders.get(oid)
        if o is not None:
            self._unlink(o)
            if self.shadow_live() and o.side == self.shadow.side and o.price == self.shadow.price:
                self.dirty = True

    def on_twin_exec(self, px: int, qty: int) -> None:
        """An execution of the shadow's own order: it is the shadow's execution, at the execution price, whatever
        the fork shows ahead of it (the user's order really was there)."""
        if not self.shadow_live():
            return
        sh = self.shadow
        take = min(qty, sh.size)
        if take <= 0:
            return
        sh.size -= take
        if sh.size == 0:
            self._unlink(sh)
        self.st.note(self.ev_ts, "own_order_exclusion", "twin_execution_fills_shadow")
        self._shadow_fill(take, px, "maker", "queue_depleted", "twin_exec", self.ev_ts, self.ev_seq)
        self._check_filled()

    def on_hidden(self, side: str | None, px: int, qty: int, limit: int, status: str) -> None:
        st = self.st
        live = self.shadow_live()
        relevant = False
        if live:
            p = self.shadow.price
            if side is None:
                relevant = at_or_better(st.spec.side, p, px)
            else:
                relevant = side == st.spec.side and at_or_better(side, p, px)
        if status == "ambiguous_side" and relevant and self.cfg.hidden_side_policy == "infer_from_shadow":
            side = st.spec.side
            limit = px
            status = "clean"
            st.note(self.ev_ts, "hidden_prints", "resting_side_inferred_from_shadow")
        if status != "clean":
            if relevant:
                st.note(self.ev_ts, "hidden_prints", status)
            return
        jit = FOrder(JIT_HIDDEN, side, px, qty, self._next(), hidden=True)
        self._insert(jit)
        before = st.filled
        self.match(side, qty, limit, self._maker_cb(px, "hidden_print"))
        if self.orders.get(JIT_HIDDEN) is jit:
            self._unlink(jit)
        if relevant:
            self.dirty = True
            st.note(
                self.ev_ts,
                "hidden_prints",
                "hidden_print_reached_shadow" if st.filled > before else "hidden_print_after_shadow_no_fill",
            )
        self._check_filled()

    def on_clear(self, ts: int) -> None:
        if self.shadow_live():
            self.end("censored_clear", ts)
        self.done = True

    # ----- event end
    def event_end_rows(self, tape: TapeBook | None = None, force: bool = False) -> None:
        if self.shadow is None:
            return
        st = self.st
        if not (self.dirty or force or st.filled != self.filled_last):
            return
        self.dirty = False
        qa, rank = self.queue_ahead() if not (self.done or st.remaining <= 0) else (0, 0)
        if (qa, rank, st.filled) != (self.qa_last, self.rank_last, self.filled_last):
            st.trajectory.append(
                self._traj_row(self.ev_ts, self.ev_seq, "event", self.qa_last, qa, self.rank_last, rank)
            )
        self.qa_last, self.rank_last, self.filled_last = qa, rank, st.filled
        if tape is not None and not self.done:
            # Orders ahead of the shadow must look exactly as on the tape (FIFO tape); otherwise the fork diverged.
            div = 0
            sh = self.shadow
            lv = self.levels[sh.side].get(sh.price)
            if lv is not None:
                for o in lv[0].values():
                    if o is sh:
                        break
                    to = tape.orders.get(o.oid)
                    tsz = to.size if to is not None and to.price == o.price else 0
                    div += abs(tsz - o.size)
            if div:
                st.diag["ahead_divergence_events"] += 1
                st.note(self.ev_ts, "oracle_divergence", "orders_ahead_differ_from_tape")

    def event_end(self, tape: TapeBook) -> None:
        if self.done or self.shadow is None:
            return
        self.event_end_rows(tape)
        sh = self.shadow
        ob = tape.best(opposite(sh.side))
        if ob is not None and at_or_better(sh.side, sh.price, ob):
            self.st.note(self.ev_ts, "crosses", "tape_opposite_best_at_or_through_shadow_at_event_end")


class PartitionRunner:
    """Runs every shadow of one (instrument, session) partition through the oracle in one pass over the tape.
    Each shadow has its own fork, so shadows never interact."""

    def __init__(
        self,
        meta: InstrumentMeta,
        shadows: list[ShadowSpec],
        amends: dict[str, list[Amend]],
        cfg: OracleConfig | None = None,
        facts: VenueFacts | None = None,
        cross_sequences: frozenset[int] = frozenset(),
    ) -> None:
        self.meta = meta
        self.cfg = cfg or OracleConfig()
        self.facts = facts or venue_facts(meta.venue, open_s=meta.open_s, close_s=meta.close_s)
        self.cross_sequences = cross_sequences
        self.tape = TapeBook(self.facts)
        self.excluded = TapeBook(self.facts)
        self.states: list[ShadowState] = []
        pend = []
        for sp in shadows:
            st = ShadowState(sp, sp.ts_submit + sp.latency_ns, len(pend))
            st.max_notes = self.cfg.max_notes_per_shadow
            self.states.append(st)
            ams = sorted(amends.get(sp.shadow_id, []), key=lambda a: a.ts_send)
            pend.append((st.ts_live, len(pend), st, ams))
        pend.sort(key=lambda x: (x[0], x[1]))
        self.pending = pend
        self.pi = 0
        # Ruling A1: pending shadows by ts_live (twin candidates) and the state of each excluded order seen so far
        # ("twin": stays on the tape, its own shadow's fork never sees it; "dropped": removed for everyone).
        self.pending_at: dict[int, list] = {}
        for item in pend:
            self.pending_at.setdefault(item[0], []).append(item)
        self.excl_state: dict[int, str] = {}
        self.ev_no = 0
        self.ev_last: tuple[int, int] = (0, 0)
        self.live: list[tuple[ForkBook, list[Amend]]] = []
        self.pending_fill: dict[int, int] = {}
        self.cur_seq: int | None = None
        self.sweep: dict[str, tuple[int, int]] = {}
        self.last_ts: int | None = None
        self.stats = {
            "records": 0,
            "events": 0,
            "exec_chunks": 0,
            "hidden_prints": 0,
            "hidden_clean": 0,
            "hidden_display_ahead": 0,
            "hidden_ambiguous": 0,
            "hidden_off_tick": 0,
            "cross_prints": 0,
            "bare_prints": 0,
            "tape_crossing_adds": 0,
            "nonprintable_execs": 0,
            "off_price_execs": 0,
            "excluded_records_dropped": 0,
            "twins_linked": 0,
        }

    # ----- lifecycle
    def _cancel_eff(self, sp: ShadowSpec) -> int | None:
        return None if sp.ts_cancel is None else sp.ts_cancel + sp.latency_ns

    def _lifecycle(self, ts: int) -> None:
        """Shadow activation, amends and cancels at the start of the event with first `ts_event = ts`.

        Conventions shared with SPEC 5.2.1/5.2.6 (they define the experiment, not the fill rules): the shadow
        arrives at `ts_live` but is first seen at the start of the first event with `ts_event > ts_live`; a
        marketable shadow trades at `ts_live`; cancels (`c = ts_cancel + latency_ns`) and amends
        (`a = ts_send + latency_ns`) take effect at the start of the first event with `ts_event > c` (or `a`); a
        cancel or amend-to-zero effective at or before `ts_live` means the shadow never goes live."""
        while self.pi < len(self.pending) and ts > self.pending[self.pi][0]:
            _, _, st, ams = self.pending[self.pi]
            self.pi += 1
            if not st.started:
                self._start(st, ams, ts)
        still = []
        for fork, ams in self.live:
            st = fork.st
            if fork.done:
                continue
            while ams and ts > ams[0].ts_send + st.spec.latency_ns:
                a = ams.pop(0)
                if a.new_qty <= st.filled:
                    fork.end("amended_to_filled", a.ts_send + st.spec.latency_ns)
                    break
                st.qty = a.new_qty
                if fork.shadow is not None:
                    fork.shadow.size = st.remaining
            c = self._cancel_eff(st.spec)
            if not fork.done and c is not None and ts > c:
                st.ts_cancel_effective = c
                fork.end("cancelled", c)
            if not fork.done:
                still.append((fork, ams))
        self.live = still

    def _start(self, st: ShadowState, ams: list[Amend], ts: int, twin: int | None = None) -> ForkBook | None:
        """Takes a shadow out of the pending list: never live (a cancel or an amend to zero effective at or before
        `ts_live`), or a fork activated on the tape as it is now (at the start of an event, or just before its
        twin's record). Returns the fork, or None when the shadow never goes live."""
        st.started = True
        sp = st.spec
        c = self._cancel_eff(sp)
        never = False
        while ams and ams[0].ts_send + sp.latency_ns <= st.ts_live:
            a = ams.pop(0)
            st.qty = a.new_qty
            if a.new_qty == 0:
                never = True
        if never or (c is not None and c <= st.ts_live):
            st.end_state = "cancelled"
            st.ts_cancel_effective = c
            st.ts_end = c
            return None
        fork = ForkBook(self.tape, st, self.facts, self.cfg)
        # At an event start the fork is stamped with the event's first ts; mid-event (a twin) with the event's end.
        fork.ev_ts, fork.ev_seq = self.ev_last if twin is not None else (ts, 0)
        fork.act_ev = self.ev_no
        if twin is not None:
            st.twin = twin
            fork.drop_twin(twin)
            st.note(ts, "own_order_exclusion", "twin_linked")
        fork.activate(self.tape, self.meta, ts)
        if self.cfg.exclude_order_ids and st.price is not None and self.excluded.level_total(sp.side, st.price) > 0:
            st.note(ts, "own_order_exclusion", "own_order_removed_from_shadow_level")
        if not fork.done:
            self.live.append((fork, ams))
        return fork

    def _link_twin(self, r: Rec) -> bool:
        """Ruling A1: the shadow that enters at the excluded order's `A` / `M` record `r` (same side and abs price;
        pending with `ts_live = ts_event`, or live since the start of this event with `ts_live = ts_event - 1`;
        not yet linked; equal quantity first, then input order) becomes that order's own shadow."""
        best = None
        for _, _, st, ams in self.pending_at.get(r.ts_event, ()):
            sp = st.spec
            if st.started or sp.price_mode != "abs" or sp.side != r.side or sp.price != r.price:
                continue
            key = (st.qty != r.size, st.index)
            if best is None or key < best[0]:
                best = (key, st, ams, None)
        for fork, _ in self.live:
            st = fork.st
            if fork.done or fork.act_ev != self.ev_no or st.ts_live != r.ts_event - 1 or st.twin is not None:
                continue
            if st.spec.side != r.side or st.price != r.price:
                continue
            key = (st.qty != r.size, st.index)
            if best is None or key < best[0]:
                best = (key, st, None, fork)
        if best is None:
            return False
        _, st, ams, fork = best
        if fork is None:
            self._start(st, ams, r.ts_event, r.order_id)
        else:
            st.twin = r.order_id
            fork.drop_twin(r.order_id)
            st.note(r.ts_event, "own_order_exclusion", "twin_linked")
        self.stats["twins_linked"] += 1
        return True

    # ----- one event
    def push_event(self, ev: list[Rec]) -> None:
        self.stats["events"] += 1
        self.ev_no += 1
        self.ev_last = (ev[-1].ts_event, ev[-1].sequence)
        ts0 = ev[0].ts_event
        self._lifecycle(ts0)
        ev_ts, ev_seq = ev[-1].ts_event, ev[-1].sequence
        for fork, _ in self.live:
            fork.ev_ts, fork.ev_seq = ev_ts, ev_seq
        facts = self.facts
        # Print / execution association inside the event.
        t_of: dict[int, Rec | None] = {}
        t_has_fill: set[int] = set()
        cur_t: Rec | None = None
        cur_t_i = -1
        prev_seq = None
        for i, r in enumerate(ev):
            if facts.pair_by_sequence and r.sequence != prev_seq:
                cur_t, cur_t_i = None, -1
            prev_seq = r.sequence
            if r.action == "T":
                cur_t, cur_t_i = r, i
            elif r.action == "F":
                t_of[i] = cur_t
                if cur_t is not None:
                    t_has_fill.add(cur_t_i)
        for i, r in enumerate(ev):
            self.stats["records"] += 1
            self._record(r, i, t_of, t_has_fill)
            if r.flags & FLAG_MAYBE_BAD_BOOK:
                for fork, _ in self.live:
                    if not fork.done:
                        fork.end("censored_gap", r.ts_event)
        self.pending_fill.clear()
        for fork, _ in self.live:
            if not fork.done:
                fork.event_end(self.tape)
        self.live = [(f, a) for f, a in self.live if not f.done]
        self.last_ts = ev_ts

    def _forks(self):
        return [f for f, _ in self.live if not f.done]

    def _close_sweep(self, side: str | None = None) -> None:
        if side is None:
            self.sweep.clear()
        else:
            self.sweep.pop(side, None)

    def _sweep_limit(self, side: str, px: int, ts: int) -> int:
        cur = self.sweep.get(side)
        if cur is not None and ts - cur[0] <= self.cfg.sweep_gap_ns:
            lim = min(cur[1], px) if side == "B" else max(cur[1], px)
        else:
            lim = px
        self.sweep[side] = (ts, lim)
        return lim

    def _record(self, r: Rec, i: int, t_of: dict, t_has_fill: set) -> None:
        tape = self.tape
        facts = self.facts
        if facts.pair_by_sequence and r.sequence != self.cur_seq:
            self.pending_fill.clear()
        self.cur_seq = r.sequence
        a = r.action
        excl = self.cfg.exclude_order_ids
        state = None
        if excl and r.order_id in excl and a in ("A", "C", "M", "F"):
            state = self.excl_state.get(r.order_id)
            if a in ("A", "M") and state != "dropped" and self.cfg.twin_linking and not (r.flags & FLAG_MAYBE_BAD_BOOK):
                # The order's entry record (or a later leg's): the shadow entering here stands for it.
                linked = self._link_twin(r)
                state = "twin" if state == "twin" or linked else "dropped"
            elif state is None:
                state = "dropped"
            self.excl_state[r.order_id] = state
        if state == "dropped" and a in ("A", "C", "M"):
            self.stats["excluded_records_dropped"] += 1
            if a == "A":
                self.excluded.add(r.order_id, r.side, r.price, r.size)
            elif a == "C":
                self.excluded.cancel(r.order_id, r.size)
            else:
                self.excluded.modify(r.order_id, r.side, r.price, r.size, False)
            return
        forks = self._forks()
        own: list[ForkBook] = []
        if state == "twin":
            # A twin's records are the real order's for every other shadow; its own shadow's fork never sees them.
            own = [f for f in forks if r.order_id in f.twins]
            forks = [f for f in forks if r.order_id not in f.twins]
        if a == "A":
            if r.price == UNDEF_PRICE or r.size == 0 or r.side not in ("B", "A"):
                return
            self._close_sweep(r.side)
            crosses = tape.crosses(r.side, r.price)
            if crosses:
                self.stats["tape_crossing_adds"] += 1
            for f in forks:
                f.on_add(r, crosses, tape)
            tape.add(r.order_id, r.side, r.price, r.size)
        elif a == "C":
            o = tape.orders.get(r.order_id)
            if o is None:
                return
            pend = self.pending_fill.get(r.order_id, 0)
            paired = min(r.size, pend)
            if paired:
                self.pending_fill[r.order_id] = pend - paired
            cancel_part = r.size - paired
            full = r.size >= o.size
            if cancel_part > 0:
                self._close_sweep(o.side)
            for f in forks:
                f.on_cancel(r.order_id, cancel_part, full and cancel_part > 0)
            tape.cancel(r.order_id, r.size)
        elif a == "M":
            if not facts.modify_present:
                return
            o = tape.orders.get(r.order_id)
            if o is None:
                if r.size > 0 and r.price != UNDEF_PRICE and r.side in ("B", "A"):
                    self._close_sweep(r.side)
                    crosses = tape.crosses(r.side, r.price)
                    for f in forks:
                        f.on_add(r, crosses, tape)
                    tape.add(r.order_id, r.side, r.price, r.size)
                return
            pend = self.pending_fill.pop(r.order_id, 0)
            pend = min(pend, o.size)
            if r.size == 0:
                # Full removal: the executed part (if any) was already matched; the rest is a cancel.
                cancel_part = o.size - pend
                if cancel_part > 0:
                    self._close_sweep(o.side)
                for f in forks:
                    f.on_cancel(r.order_id, cancel_part, cancel_part > 0)
                tape.cancel(r.order_id, o.size)
                return
            keeps = tape.keeps_priority(r.order_id, r.price, r.size, pend)
            if not (keeps and r.size == o.size - pend):
                self._close_sweep(o.side)
            crosses = (not keeps) and tape.crosses(o.side, r.price)
            for f in forks:
                f.on_modify(r.order_id, o.side, r.price, r.size, keeps, o.price, o.size, pend, crosses, tape)
            tape.modify(r.order_id, o.side, r.price, r.size, keeps)
        elif a == "R":
            for f in forks:
                f.on_clear(r.ts_event)
            tape.clear()
            self.excluded.clear()
            self._close_sweep()
            self.pending_fill.clear()
        elif a == "F":
            self.stats["exec_chunks"] += 1
            o = tape.orders.get(r.order_id)
            side = o.side if o is not None else r.side
            if side not in ("B", "A"):
                return
            resting_px = o.price if o is not None else r.price
            t = t_of.get(i)
            printed = t is not None
            exec_px = t.price if printed and t.price != UNDEF_PRICE else r.price
            if not printed:
                self.stats["nonprintable_execs"] += 1
            elif exec_px != resting_px:
                self.stats["off_price_execs"] += 1
            limit = self._sweep_limit(side, resting_px, r.ts_event)
            excluded = state == "dropped"
            for f in own:
                f.on_twin_exec(exec_px, r.size)
            for f in forks:
                f.on_exec(side, resting_px, exec_px, r.order_id, r.size, limit, printed, excluded)
            if o is not None:
                self.pending_fill[r.order_id] = self.pending_fill.get(r.order_id, 0) + r.size
        elif a == "T":
            if i in t_has_fill or r.size == 0 or r.price == UNDEF_PRICE:
                return
            self._bare_print(r)

    def _bare_print(self, r: Rec) -> None:
        facts = self.facts
        tape = self.tape
        if not facts.hidden_orders:
            self.stats["bare_prints"] += 1
            return
        self.stats["hidden_prints"] += 1
        q = r.price
        meta = self.meta
        tod_s = (r.ts_event - meta.midnight_utc_ns) / NS
        in_cross = r.sequence in self.cross_sequences or any(lo <= tod_s < hi for lo, hi in facts.cross_windows_s)
        side: str | None = None
        if r.side in ("B", "A"):
            side = opposite(r.side)  # resting side = opposite of the aggressor
        else:
            open_sides = [s for s, (ts, _) in self.sweep.items() if r.ts_event - ts <= self.cfg.sweep_gap_ns]
            if len(open_sides) == 1:
                side = open_sides[0]
        tick = meta.tick_size or 1
        if in_cross:
            status = "cross_print"
            self.stats["cross_prints"] += 1
        elif side is None and (tape.displayed_at_or_better("B", q) > 0 or tape.displayed_at_or_better("A", q) > 0):
            # At or through a displayed best: a non-displayed order there would rank behind displayed interest
            # that the tape shows was not executed (or it is a cross / mis-sided print). Not usable.
            status = "display_ahead_on_tape"
            self.stats["hidden_display_ahead"] += 1
        elif side is None and q % tick != 0:
            status = "off_tick_midpoint"
            self.stats["hidden_off_tick"] += 1
        elif side is None:
            status = "ambiguous_side"
            self.stats["hidden_ambiguous"] += 1
        elif tape.displayed_at_or_better(side, q) > 0:
            status = "display_ahead_on_tape"
            self.stats["hidden_display_ahead"] += 1
        else:
            status = "clean"
            self.stats["hidden_clean"] += 1
        limit = q
        if status == "clean":
            limit = self._sweep_limit(side, q, r.ts_event)
        for f in self._forks():
            f.on_hidden(side, q, r.size, limit, status)

    def finish(self) -> list[dict[str, Any]]:
        close_ns = self.meta.midnight_utc_ns + self.meta.close_s * NS
        last = self.last_ts if self.last_ts is not None else 0
        state = "censored_session_end" if last >= close_ns else "open_at_end"
        for fork, _ in self.live:
            if not fork.done:
                fork.end(state, last)
        while self.pi < len(self.pending):
            st = self.pending[self.pi][2]
            self.pi += 1
            if st.started:
                continue
            st.end_state = state
            st.ts_end = last
        return [st.result() for st in self.states]

    def run(self, records) -> list[dict[str, Any]]:
        for ev in group_events(records):
            self.push_event(ev)
        return self.finish()


def run_partition(
    meta: InstrumentMeta,
    records,
    shadows: list[ShadowSpec],
    amends: dict[str, list[Amend]] | None = None,
    cfg: OracleConfig | None = None,
    cross_sequences: frozenset[int] = frozenset(),
) -> dict[str, Any]:
    """Convenience wrapper: returns {"results": [...], "stats": {...}}."""
    runner = PartitionRunner(meta, shadows, amends or {}, cfg, cross_sequences=cross_sequences)
    res = runner.run(records)
    return {"results": res, "stats": runner.stats}
