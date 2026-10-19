"""Importer core: collects venue records into canonical orders and events (SPEC 3.8) without dropping or inventing
data silently.

Semantics shared by every importer:

- Order identity: records are joined by venue order id and client order id (a client id never joins two orders
  with different venue order ids). `order_key = <venue>:<account>:<venue_order_id or root client_order_id>`;
  `client_order_id` is the root (first) client id, `client_order_id_chain` lists every client id of the order in
  order of appearance when there is more than one.
- Event order within an order: captured streams (WebSocket, FIX log) keep their capture order; history records
  (REST, CSV, Flex) are placed by their timestamp; ties keep input order. Across inputs an event is placed by its
  local time (receive, else send) when it has one, else by its exchange time, so a local `cancel_sent` and the
  venue's `cancel_ack` from another capture compare on the same clock.
- Fill dedupe: one fill per (instrument, order side, `fill_id`) (venue trade ids can be per symbol, and both sides
  of a self-trade carry the same trade id, so each side keeps its fill); a stream copy wins over a history copy,
  else the first seen; every dropped copy counts in `dedupe_dropped` (conflicting copies also warn).
  Fills derived from cumulative quantities are dropped (and counted) when the same order has per-fill records;
  lifecycle events synthesised from a history order record are dropped (and counted) when a captured event of the
  same type exists.
- Quantities left empty by the venue are completed arithmetically from the order's own events: `cum_qty` = running
  net filled qty (busts subtract the referenced fill, corrections replace it); `leaves_qty` = current total qty minus
  `cum_qty` (0 on terminal events), only when an order-level record gave the total.
- An order seen only through fills (no order-level record) gets `orig_qty` = its summed fill qty, `order_type` and
  `tif` = `other`, `limit_px` = null; its fills count in `unmatched_fills` and a warning names it.
- Records that cannot be mapped (no side or instrument, or no quantity at all) are excluded, counted and named in
  the warnings; messages without a canonical event are counted per reason in the warnings.
- Replacements (schema 1.1, SPEC 3.11 #16): an order created with `replaces=<handle>` (a replace the venue answered
  with a new venue order id as a separate order) gets `replaces_order_key` = the replaced order's key when the two
  stay distinct orders; a replace that stays one order (same client-id chain) leaves it null.
- Schedule legs: orders whose importer gave them the same `key_override` (simulator orders replaying the legs
  `<order_key>`, `<order_key>.leg<k>` of one live order) become one order: leg k > 1 starts as `amend_sent` /
  `amend_ack` (new total = the order's net fills so far + the leg's size, price = the leg's), the re-price cancel
  of the previous leg is dropped (counted), cumulative / leaves quantities are recomputed across legs.
- Client ids can be scoped (`cid_scope`, e.g. one FIX session: ClOrdIDs are unique per session, not per venue), and
  `fresh=True` starts a new order even when the client id is known (a ClOrdID reused on a later trading day); two
  such orders without venue order ids get the keys `<key>` and `<key>.r<k>`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live._common import (
    Record,
    dsub,
    dsum,
    make_order_key,
    to_fixed,
    utc_date_days,
)
from queuetruth.live.imap import InstrumentMap

TERMINAL = ("cancel_ack", "expire", "done", "reject", "post_only_reject")
STREAM_SOURCES = ("ws", "fix_dropcopy")
_SOURCE_RANK = {"ws": 0, "fix_dropcopy": 0, "rest": 1, "csv": 2, "flex": 2, "synthetic": 3}
_STATIC = ("account", "instrument", "side", "order_type", "tif", "post_only", "reduce_only", "strategy_tag")


@dataclass
class _Order:
    handle: int
    venue_order_id: str | None = None
    client_ids: list[str] = field(default_factory=list)
    account: str | None = None
    instrument: str | None = None
    side: str | None = None
    order_type: str | None = None
    tif: str | None = None
    post_only: bool | None = None
    reduce_only: bool | None = None
    strategy_tag: str | None = None
    qty: Decimal | None = None
    qty_text: str | None = None
    px: Decimal | None = None
    px_text: str | None = None
    raw: str | None = None
    key_override: str | None = None
    record: bool = False
    first_pos: tuple[int, int] = (1 << 30, 1 << 30)
    replaces: int | None = None
    reused: bool = False
    flags: set[str] = field(default_factory=set)
    legs: dict[int, tuple[Decimal | None, Decimal | None]] | None = None


@dataclass
class _Event:
    handle: int
    event_type: str
    source: str
    kind: str
    precision: str
    file_idx: int
    pos: int
    stream: bool
    snapshot: bool
    ts_exchange: int | None = None
    ts_local_send: int | None = None
    ts_local_recv: int | None = None
    qty: Decimal | None = None
    px: Decimal | None = None
    leaves: Decimal | None = None
    cum: Decimal | None = None
    fill_id: str | None = None
    ref_fill_id: str | None = None
    liquidity: str | None = None
    fee: Decimal | None = None
    fee_ccy: str | None = None
    keeps: bool | None = None
    reason: str | None = None
    derived: bool = False
    raw: str | None = None
    complete: bool = True
    soft_px: bool = False
    keep_derived: bool = False
    key: int = 0
    leg: int = 0
    leg_qty: Decimal | None = None


@dataclass
class ImportResult:
    """Canonical rows of one import plus its counters (fed into `qt.live_import_summary`)."""

    venue: str
    orders: list[dict[str, Any]]
    events: list[dict[str, Any]]
    n_fills: int
    dedupe_dropped: int
    unmatched_fills: int
    warnings: list[str]
    skipped: dict[str, int]
    instrument_map_sha256: str | None = None
    fills_only_orders: int = 0


class LogBuilder:
    """Accumulates orders and events of one venue import and emits canonical rows (see module docstring)."""

    def __init__(
        self,
        venue: str,
        *,
        account: str | None = None,
        imap: InstrumentMap | None = None,
        price_exp: int = -9,
        size_exp: int = -8,
        validation_status: str = "fixture_only",
        always_chain: bool = False,
        map_venue: str | None = None,
        source_override: str | None = None,
    ) -> None:
        # Venue names are case-insensitive (E53): canonical logs carry them in lower case.
        self.venue = venue.lower()
        self.account = account
        self.imap = imap
        self.price_exp = price_exp
        self.size_exp = size_exp
        self.validation_status = validation_status
        self.always_chain = always_chain
        self.map_venue = (map_venue or venue).lower()
        self.source_override = source_override
        self._orders: list[_Order] = []
        self._parent: list[int] = []
        self._by_oid: dict[str, int] = {}
        self._by_cid: dict[Any, int] = {}
        self._events: list[_Event] = []
        self.warnings: list[str] = []
        self._warned: set[str] = set()
        self.skipped: dict[str, int] = {}
        self.dedupe_dropped = 0

    # ---- bookkeeping ---------------------------------------------------------------------------------------------

    def warn(self, msg: str) -> None:
        """Adds a warning once."""
        if msg not in self._warned:
            self._warned.add(msg)
            self.warnings.append(msg)

    def skip(self, reason: str, n: int = 1) -> None:
        """Counts a record that has no canonical event (reported in the warnings)."""
        self.skipped[reason] = self.skipped.get(reason, 0) + n

    def dropped_duplicate(self, n: int = 1) -> None:
        """Counts a duplicate record dropped by an importer."""
        self.dedupe_dropped += n

    def _find(self, h: int) -> int:
        while self._parent[h] != h:
            self._parent[h] = self._parent[self._parent[h]]
            h = self._parent[h]
        return h

    def _merge(self, a: int, b: int) -> int:
        a, b = self._find(a), self._find(b)
        if a == b:
            return a
        keep, gone = (a, b) if a < b else (b, a)
        self._parent[gone] = keep
        ko, go = self._orders[keep], self._orders[gone]
        if ko.venue_order_id is None:
            ko.venue_order_id = go.venue_order_id
        for c in go.client_ids:
            if c not in ko.client_ids:
                ko.client_ids.append(c)
        for f in (*_STATIC, "qty", "qty_text", "px", "px_text", "raw", "key_override"):
            if getattr(ko, f) is None:
                setattr(ko, f, getattr(go, f))
        ko.record = ko.record or go.record
        ko.first_pos = min(ko.first_pos, go.first_pos)
        if ko.replaces is None:
            ko.replaces = go.replaces
        ko.reused = ko.reused or go.reused
        ko.flags |= go.flags
        return keep

    @staticmethod
    def _ck(cid: str, scope: str | None) -> Any:
        return cid if scope is None else (scope, cid)

    def find_order(self, *, oid: str | None = None, cid: str | None = None, cid_scope: str | None = None) -> int | None:
        """Handle of an already-seen order, or None."""
        if oid is not None and oid in self._by_oid:
            return self._find(self._by_oid[oid])
        ck = self._ck(cid, cid_scope) if cid is not None else None
        if ck is not None and ck in self._by_cid:
            return self._find(self._by_cid[ck])
        return None

    def order(
        self,
        *,
        oid: str | None = None,
        cid: str | None = None,
        orig_cid: str | None = None,
        rec: Record | None = None,
        record: bool = False,
        qty: Decimal | None = None,
        qty_text: str | None = None,
        px: Decimal | None = None,
        px_text: str | None = None,
        raw: str | None = None,
        cid_scope: str | None = None,
        fresh: bool = False,
        replaces: int | None = None,
        **fields: Any,
    ) -> int:
        """Finds or creates the order for these ids and merges the given static fields (first value wins; a
        conflicting later value warns). `record=True` marks an order-level record (gives `orig_qty`). `fresh`
        ignores known client ids (a new order reusing one); `replaces` links a new order to the order it replaces;
        `cid_scope` scopes the client ids (see the module docstring)."""
        cands: list[int] = []
        if oid is not None and oid in self._by_oid:
            cands.append(self._find(self._by_oid[oid]))
        reused = False
        for c in (orig_cid, cid):
            ck = self._ck(c, cid_scope) if c is not None else None
            if ck is None or ck not in self._by_cid:
                continue
            if fresh:
                reused = True
                continue
            h = self._find(self._by_cid[ck])
            known = self._orders[h].venue_order_id
            if oid is not None and known is not None and known != oid:
                continue  # client id reused by a different venue order
            cands.append(h)
        if cands:
            h = cands[0]
            for other in cands[1:]:
                h = self._merge(h, other)
        else:
            h = len(self._orders)
            self._orders.append(_Order(handle=h, reused=reused))
            self._parent.append(h)
        o = self._orders[h]
        if replaces is not None and self._find(replaces) != h and o.replaces is None:
            o.replaces = self._find(replaces)
        if oid is not None:
            if o.venue_order_id is None:
                o.venue_order_id = oid
            self._by_oid[oid] = h
        for c in (orig_cid, cid):
            if c is not None:
                self._by_cid[self._ck(c, cid_scope)] = h
                if c not in o.client_ids:
                    o.client_ids.append(c)
        if rec is not None:
            o.first_pos = min(o.first_pos, (rec.file_idx, rec.line_idx))
        for k, v in fields.items():
            if k not in _STATIC:
                raise TypeError(f"unknown order field {k}")
            if v is None:
                continue
            cur = getattr(o, k)
            if cur is None:
                setattr(o, k, v)
            elif cur != v:
                self.warn(f"{self.venue}: order {oid or cid}: conflicting {k} {cur!r} vs {v!r} (first kept)")
        if record and qty is not None and o.qty is None:
            o.qty, o.qty_text = qty, qty_text
        if px is not None and o.px is None:
            o.px, o.px_text = px, px_text
        if record:
            o.record = True
            if o.raw is None:
                o.raw = raw if raw is not None else (rec.raw if rec is not None else None)
        return h

    def event(
        self,
        h: int,
        event_type: str,
        *,
        rec: Record | None,
        source: str,
        kind: str,
        precision: str,
        stream: bool | None = None,
        snapshot: bool = False,
        raw: str | None = None,
        **values: Any,
    ) -> _Event:
        """Appends an event (canonical field names; quantities as `Decimal`). Capture-wrapper local times fill
        `ts_local_recv` / `ts_local_send` when not given."""
        file_idx = rec.file_idx if rec is not None else 0
        e = _Event(
            handle=h,
            event_type=event_type,
            source=source,
            kind=kind,
            precision=precision,
            file_idx=file_idx,
            pos=len(self._events),
            stream=(source in STREAM_SOURCES) if stream is None else stream,
            snapshot=snapshot,
            raw=raw if raw is not None else (rec.raw if rec is not None else None),
        )
        for k, v in values.items():
            if not hasattr(e, k):
                raise TypeError(f"unknown event field {k}")
            setattr(e, k, v)
        if rec is not None:
            if e.ts_local_recv is None and rec.recv_ns is not None:
                e.ts_local_recv = rec.recv_ns
            if e.ts_local_send is None and rec.send_ns is not None:
                e.ts_local_send = rec.send_ns
        if e.ts_exchange is None and e.ts_local_recv is None and e.ts_local_send is None:
            raise QtError(
                "QT-INPUT-002",
                f"{rec.location if rec else self.venue}: {event_type} event has no timestamp",
            )
        self._events.append(e)
        return e

    def alias_oid(self, h: int, oid: str) -> None:
        """Routes a further venue order id (e.g. a new id assigned on replace) to an existing order."""
        self._by_oid[oid] = self._find(h)

    def root(self, h: int) -> int:
        """Current handle of an order (handles merge when ids join two records)."""
        return self._find(h)

    def flag(self, h: int, name: str) -> None:
        """Marks an order with an importer-defined flag (kept across merges)."""
        self._orders[self._find(h)].flags.add(name)

    def has_flag(self, h: int, name: str) -> bool:
        """True when the order carries the flag."""
        return name in self._orders[self._find(h)].flags

    def default_field(self, name: str, value: Any) -> int:
        """Sets a static field on every order (with an order-level record) still without it; returns the count."""
        if name not in _STATIC:
            raise TypeError(f"unknown order field {name}")
        n = 0
        for o in self._orders:
            if self._find(o.handle) == o.handle and o.record and getattr(o, name) is None:
                setattr(o, name, value)
                n += 1
        return n

    def order_field(self, h: int, name: str) -> Any:
        """Current value of an order field (after merges)."""
        return getattr(self._orders[self._find(h)], name)

    def set_order_field(self, h: int, name: str, value: Any) -> None:
        """Sets an order field when it is still empty."""
        o = self._orders[self._find(h)]
        if getattr(o, name) is None:
            setattr(o, name, value)

    # ---- finalisation --------------------------------------------------------------------------------------------

    def _dedupe(self, by_order: dict[int, list[_Event]]) -> list[_Event]:
        kept: list[_Event] = []
        # fills by (instrument, order side, fill_id) (E50)
        groups: dict[tuple[str, str, str], list[_Event]] = {}
        for h, evs in by_order.items():
            inst = self._orders[h].instrument or ""
            side = self._orders[h].side or ""
            for e in evs:
                if e.event_type == "fill" and e.fill_id is not None:
                    groups.setdefault((inst, side, e.fill_id), []).append(e)
        drop: set[int] = set()
        for (inst, _side, fid), evs in groups.items():
            if len(evs) < 2:
                continue
            evs_sorted = sorted(evs, key=lambda e: (_SOURCE_RANK.get(e.source, 9), e.pos))
            win = evs_sorted[0]
            for loser in evs_sorted[1:]:
                drop.add(loser.pos)
                self.dedupe_dropped += 1
                if (loser.qty, loser.px) != (win.qty, win.px):
                    self.warn(
                        f"{self.venue}: conflicting duplicate fill {inst}/{fid}: qty/px {loser.qty}/{loser.px} vs "
                        f"{win.qty}/{win.px} (kept the {win.source} copy)"
                    )
        for evs in by_order.values():
            live = [e for e in evs if e.pos not in drop]
            has_real_fill = any(e.event_type == "fill" and not e.derived for e in live)
            captured_types = {e.event_type for e in live if not e.snapshot}
            n_derived = 0
            for e in live:
                if e.event_type == "fill" and e.derived and has_real_fill and not e.keep_derived:
                    n_derived += 1
                    self.dedupe_dropped += 1
                    continue
                if e.snapshot and e.event_type in captured_types:
                    self.dedupe_dropped += 1
                    continue
                kept.append(e)
            if n_derived:
                self.warn(
                    f"{self.venue}: {n_derived} fill(s) derived from cumulative quantities dropped where per-fill "
                    "records exist"
                )
        return kept

    def _sort_keys(self) -> None:
        running: dict[int, int] = {}
        last = 0
        for e in self._events:
            t = e.ts_local_recv if e.ts_local_recv is not None else e.ts_local_send
            if t is None:
                t = e.ts_exchange
            t = last if t is None else t
            if e.stream:
                cur = running.get(e.file_idx)
                t = t if cur is None or t > cur else cur
                running[e.file_idx] = t
            e.key = t
            last = t

    def _merge_legs(self) -> None:
        """Orders sharing a `key_override` -> one order with per-event leg numbers (see the module docstring)."""
        groups: dict[str, list[int]] = {}
        for o in self._orders:
            if self._find(o.handle) == o.handle and o.key_override is not None:
                groups.setdefault(o.key_override, []).append(o.handle)
        groups = {k: v for k, v in groups.items() if len(v) > 1}
        if not groups:
            return
        first_key: dict[int, int] = {}
        for e in self._events:
            r = self._find(e.handle)
            first_key[r] = min(first_key.get(r, e.key), e.key)
        n_legs = 0
        for hs in groups.values():
            hs = sorted(hs, key=lambda h: (first_key.get(h, 1 << 62), self._orders[h].first_pos, h))
            lead = self._orders[hs[0]]
            keep = {f: getattr(lead, f) for f in (*_STATIC, "qty", "qty_text", "px", "px_text", "raw")}
            legs = {k: (self._orders[h].qty, self._orders[h].px) for k, h in enumerate(hs)}
            leg_of = {h: k for k, h in enumerate(hs)}
            for e in self._events:
                k = leg_of.get(self._find(e.handle))
                if k is not None:
                    e.leg = k
            root = hs[0]
            for h in hs[1:]:
                root = self._merge(root, h)
            o = self._orders[root]
            for f, v in keep.items():
                setattr(o, f, v)
            o.legs = legs
            n_legs += len(hs)
        self.warn(
            f"{self.venue}: {len(groups)} order(s) merged from {n_legs} schedule legs (.leg<k> client ids); leg k > 1 "
            "starts are amend_sent / amend_ack"
        )

    def _leg_events(self, evs: list[_Event], legs: dict[int, tuple[Decimal | None, Decimal | None]]) -> list[_Event]:
        last = max(e.leg for e in evs) if evs else 0
        out: list[_Event] = []
        dropped = 0
        for e in evs:
            if e.leg < last and e.event_type in ("cancel_sent", "cancel_ack", "expire"):
                dropped += 1
                continue
            e.cum, e.leaves = None, None
            if e.leg > 0 and e.event_type in ("submit_sent", "ack"):
                e.event_type = "amend_sent" if e.event_type == "submit_sent" else "amend_ack"
                qty, px = legs.get(e.leg, (None, None))
                e.leg_qty = qty if qty is not None else e.qty
                e.px = e.px if e.px is not None else px
                e.reason = e.reason or f"schedule leg {e.leg + 1}"
            out.append(e)
        if dropped:
            self.dedupe_dropped += dropped
            self.warn(f"{self.venue}: {dropped} re-price cancel event(s) between schedule legs folded into amends")
        return out

    def finish(self) -> ImportResult:
        """Canonical order and event rows (unsorted dicts ready for `build_log`) and the counters."""
        self._sort_keys()
        self._merge_legs()
        by_order: dict[int, list[_Event]] = {}
        for e in self._events:
            by_order.setdefault(self._find(e.handle), []).append(e)
        kept = self._dedupe(by_order)
        by_order = {}
        for e in kept:
            by_order.setdefault(self._find(e.handle), []).append(e)
        roots = sorted({self._find(o.handle) for o in self._orders}, key=lambda h: self._orders[h].first_pos)
        orders_out: list[dict[str, Any]] = []
        events_out: list[dict[str, Any]] = []
        unmatched = 0
        n_fills = 0
        keys: dict[str, int] = {}
        key_of: dict[int, str] = {}
        excluded: list[str] = []
        for h in roots:
            o = self._orders[h]
            evs = sorted(by_order.get(h, []), key=lambda e: (e.key, e.file_idx, e.pos))
            if o.legs is not None:
                evs = self._leg_events(evs, o.legs)
            label = o.venue_order_id or (o.client_ids[0] if o.client_ids else f"#{h}")
            fills = [e for e in evs if e.event_type == "fill"]
            if not o.record:
                if not fills:
                    if evs:
                        excluded.append(f"{label} ({len(evs)} event(s), no order record or fill)")
                    continue
                if any(e.qty is None for e in fills):
                    excluded.append(f"{label} (fill without quantity)")
                    continue
                o.qty = dsum([e.qty for e in fills if e.qty is not None])
                o.qty_text = format(o.qty, "f")
                o.order_type = o.order_type or "other"
                o.tif = o.tif or "other"
                unmatched += len(fills)
                self.warn(
                    f"{self.venue}: order {label} has fills but no order record: orig_qty = summed fill qty "
                    "(lower bound), order_type/tif unknown"
                )
            if o.side is None or o.instrument is None or o.qty is None:
                missing = [n for n, v in (("side", o.side), ("instrument", o.instrument), ("qty", o.qty)) if v is None]
                excluded.append(f"{label} (missing {', '.join(missing)}; {len(evs)} event(s))")
                continue
            account = self.account or o.account or "unknown"
            ident = o.venue_order_id or (o.client_ids[0] if o.client_ids else None)
            if ident is None:
                excluded.append(f"#{h} (no order id)")
                continue
            key = o.key_override or make_order_key(self.venue, account, ident)
            if key in keys and o.reused and o.venue_order_id is None and o.key_override is None:
                k = 2
                while f"{key}.r{k}" in keys:
                    k += 1
                key = f"{key}.r{k}"
            if key in keys:
                raise QtError("QT-INPUT-003", f"duplicate order_key: {key}")
            keys[key] = h
            key_of[h] = key
            first_ts = next(
                (t for e in evs for t in (e.ts_exchange, e.ts_local_recv, e.ts_local_send) if t is not None), None
            )
            entry = None
            if self.imap is not None:
                entry = self.imap.lookup(self.map_venue, o.instrument, utc_date_days(first_ts) if first_ts else None)
                if entry is None:
                    self.warn(f"{self.venue}: instrument {o.instrument!r} not in the instrument map (defaults used)")
            pexp = entry.price_exp if entry else self.price_exp
            sexp = entry.size_exp if entry else self.size_exp
            where = f"{self.venue} order {label}"
            orig = to_fixed(o.qty, sexp, f"{where} qty", unsigned=True)
            limit_px = to_fixed(o.px, pexp, f"{where} price") if o.px is not None else None
            if entry is not None:
                if orig % entry.lot_size:
                    self.warn(f"{where}: qty {o.qty_text} is not a multiple of lot_size")
                if limit_px is not None and limit_px % entry.tick_size:
                    self.warn(f"{where}: price {o.px_text} is not a multiple of tick_size")
            chain = list(o.client_ids)
            post_only = bool(o.post_only) or o.tif == "post_only"
            orders_out.append(
                {
                    "order_key": key,
                    "venue": self.venue,
                    "account": account,
                    "instrument": o.instrument,
                    "instrument_id": entry.instrument_id if entry else None,
                    "venue_order_id": o.venue_order_id,
                    "client_order_id": chain[0] if chain else None,
                    "client_order_id_chain": chain if len(chain) > 1 or (self.always_chain and chain) else None,
                    "side": o.side,
                    "order_type": o.order_type or "other",
                    "tif": o.tif or "other",
                    "post_only": post_only,
                    "reduce_only": o.reduce_only,
                    "orig_qty": orig,
                    "qty_str": o.qty_text if o.qty_text is not None else format(o.qty, "f"),
                    "limit_px": limit_px,
                    "px_str": o.px_text if o.px is not None else None,
                    "price_exp": pexp,
                    "size_exp": sexp,
                    "strategy_tag": o.strategy_tag,
                    "validation_status": self.validation_status,
                    "raw": o.raw,
                    "replaces_order_key": None,
                    "_fills_only": not o.record,
                    "_replaces": o.replaces,
                }
            )
            events_out.extend(self._order_events(key, o, evs, pexp, sexp, where))
            n_fills += len(fills)
        fills_only = sum(1 for r in orders_out if r["_fills_only"])
        unlinked = 0
        for r in orders_out:
            del r["_fills_only"]
            rep_h = r.pop("_replaces")
            if rep_h is not None:
                target = key_of.get(self._find(rep_h))
                if target is None:
                    unlinked += 1
                elif target != r["order_key"]:  # merged into one order: no link
                    r["replaces_order_key"] = target
        if unlinked:
            self.warn(f"{self.venue}: {unlinked} replacement order(s) whose replaced order was excluded (no link)")
        if fills_only:
            self.warn(
                f"{self.venue}: coverage=fills_only for {fills_only} of {len(orders_out)} order(s): no order records, "
                "so never-filled orders are absent; fill-rate bias and McNemar are not meaningful for them"
            )
        if excluded:
            self.warn(f"{self.venue}: {len(excluded)} order(s) excluded as unmappable: " + "; ".join(excluded[:20]))
        for reason in sorted(self.skipped):
            self.warn(f"{self.venue}: {self.skipped[reason]} record(s) without a canonical event: {reason}")
        return ImportResult(
            venue=self.venue,
            orders=orders_out,
            events=events_out,
            n_fills=n_fills,
            dedupe_dropped=self.dedupe_dropped,
            unmatched_fills=unmatched,
            warnings=list(self.warnings),
            skipped=dict(self.skipped),
            instrument_map_sha256=self.imap.sha256 if self.imap else None,
            fills_only_orders=fills_only,
        )

    def _order_events(
        self, key: str, o: _Order, evs: list[_Event], pexp: int, sexp: int, where: str
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cum = Decimal(0)
        total = o.qty if o.record else None
        fill_qty: dict[str, Decimal] = {}
        for seq, e in enumerate(evs, start=1):
            qty, px, cum_v, leaves = e.qty, e.px, e.cum, e.leaves
            t = e.event_type
            if e.leg_qty is not None and t in ("amend_sent", "amend_ack"):
                qty = dsum([cum, e.leg_qty])
            if t == "fill" and qty is not None:
                cum = dsum([cum, qty])
                if e.fill_id is not None:
                    fill_qty[e.fill_id] = qty
            elif t == "fill_bust" and e.ref_fill_id is not None:
                old = fill_qty.pop(e.ref_fill_id, None)
                if old is not None:
                    cum = dsub(cum, old)
            elif t == "fill_correct" and e.ref_fill_id is not None and qty is not None:
                old = fill_qty.get(e.ref_fill_id)
                if old is not None:
                    cum = dsum([dsub(cum, old), qty])
                    fill_qty[e.ref_fill_id] = qty
            if t in ("amend_ack", "restate") and qty is not None:
                total = qty
            if e.complete:
                if t in ("ack", "submit_sent"):
                    qty = qty if qty is not None else total
                    px = px if px is not None else o.px
                if t in ("ack", "fill", "fill_bust", "fill_correct", "amend_ack", "restate", *TERMINAL):
                    cum_v = cum_v if cum_v is not None else cum
                    if leaves is None and t in TERMINAL:
                        leaves = Decimal(0)
                    elif leaves is None and total is not None and t != "submit_sent":
                        rest = dsub(total, cum_v)
                        if rest >= 0:
                            leaves = rest
                        else:
                            self.warn(f"{where}: cumulative qty exceeds order qty (leaves_qty left null)")
            px_fixed = None
            if px is not None:
                try:
                    px_fixed = to_fixed(px, pexp, f"{where} event px")
                except QtError:
                    if not e.soft_px:
                        raise
                    self.warn(f"{where}: derived price {px} not exact at price_exp {pexp} (px left null)")
            out.append(
                {
                    "order_key": key,
                    "event_seq": seq,
                    "event_type": t,
                    "qty": to_fixed(qty, sexp, f"{where} event qty", unsigned=True) if qty is not None else None,
                    "px": px_fixed,
                    "leaves_qty": to_fixed(leaves, sexp, f"{where} leaves_qty", unsigned=True)
                    if leaves is not None
                    else None,
                    "cum_qty": to_fixed(cum_v, sexp, f"{where} cum_qty", unsigned=True) if cum_v is not None else None,
                    "fill_id": e.fill_id,
                    "ref_fill_id": e.ref_fill_id,
                    "liquidity": e.liquidity,
                    "fee": float(e.fee) if e.fee is not None else None,
                    "fee_ccy": e.fee_ccy,
                    "amend_keeps_priority": e.keeps,
                    "ts_local_send": e.ts_local_send,
                    "ts_exchange": e.ts_exchange,
                    "ts_local_recv": e.ts_local_recv,
                    "ts_exchange_kind": e.kind,
                    "ts_precision": e.precision,
                    "source": self.source_override or e.source,
                    "reason": e.reason,
                    "derived": e.derived,
                    "raw": e.raw,
                }
            )
        return out
