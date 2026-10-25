"""Finds the user's own orders in the public `live_orders` / `live_trades` feed and recovers their exact queue
position.

Bitstamp's public `live_orders` channel carries every order of the market with its venue order id, and a placed
order's REST response is expected to return that same id. So each order of a harness log is found in the feed by id
(`matched_by = "id"`). When the harness never got an id (the submit's outcome was unknown), the order is searched
for by signature: an `order_created` with the same side, price and amount received within `[sent - 1 s,
sent + 30 s]` of the submit, unique among unmatched candidates (`matched_by = "signature"`; several candidates:
`ambiguous`, none: `not_in_feed`). When the REST id is not in the feed at all, the signature search runs too
(`signature_after_id_miss`) and `id_misses` counts it with a warning: every placed order should be found by id, so
any miss on a live run means the REST id and the feed id differ and must be investigated before the run is trusted.
Dry-run ids (`dry-NNNNNN`) are never in a real feed.

Orders are keyed by (log, order index), never by client order id; the same client order id in two submits (two
logs of one run, or a rerun that reused ids) fails with `QT-INPUT-003`.

The feed is replayed per (segment, market) on the segment's seed (`check.choose_seed`) in arrival order with the
book rules of `book.L3Book` (takers skipped as in `check.py`). For each own order this gives:

- the queue position at entry: orders and quantity ahead at its price, the level's rank from the touch, and the
  quantity at better prices (`entry_*`); `entry_exact` is true when the price is strictly inside the part of the
  book the seed showed order by order (a WebSocket seed shows only 100 orders per side);
- a queue trace: one row each time the quantity ahead changes, with the cause;
- every fill (trade id, venue time, price, quantity, maker or taker, quantity ahead just before);
- the end: `filled` (deleted with amount 0), `cancelled` (deleted with a remainder after a cancel request),
  `removed` (deleted with a remainder and no cancel request seen), `open_at_end`.

`ingest_suppressed`: the order's only messages are `order_created` and `order_deleted` within the ingest's taker
window and no trade names it as the maker; `queuetruth ingest --format bitstamp` drops such an order as a taker, so
the scorer cannot match it to an MBO record (it lowers `twin_match_rate`). An order a trade names as the maker is kept
by the ingest (`maker_in_taker_window`) and is not flagged.

Fees: for each fill, the fee of the final `order_status` transaction with the same trade id (`tid`), in the market's
counter currency; null when the log has no such transaction (dry runs, stand-ins, synthetic logs). A live run's fill
without one is counted in `live_fills_without_fee` with a warning.

Memory: only the streams that contain an own order id are loaded, one at a time, and signature candidates are read
with a time-window filter.

Consistency checks per order: `fills_consistent` (the trades' quantity equals the amount the order lost on
`live_orders`), `amount_traded_consistent` (equals the sum of the feed's per-message `amount_traded`, where present),
`venue_consistent` (equals the executed amount of the harness's final `order_status`, live runs only) and
`fifo_violations` (fills received while orders were still ahead at the same price).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from livefill.book import BUY, L3Book
from livefill.check import DEFAULT_TAKER_WINDOW_US, apply, better, choose_seed, maker_ids, seed_limits, taker_ids
from livefill.harness import read_log
from livefill.store import read_meta
from livefill.stream import (
    OrderEvent,
    SegmentStream,
    TradeEvent,
    created_ids,
    load_stream,
    read_creates,
    stream_index,
    streams_with_ids,
)
from livefill.units import PRICE_EXP, SIZE_EXP, to_fixed
from queuetruth.errors import QtError

SIGNATURE_BEFORE_NS = 1_000_000_000
SIGNATURE_AFTER_NS = 30_000_000_000


@dataclass
class HarnessOrder:
    """One submitted order of a harness log."""

    run: str
    mode: str
    #: position of the log in the list given to `harness_orders` (with `index`, the order's key)
    log_index: int
    index: int
    client_order_id: str
    market: str
    side: str
    price_str: str
    amount_str: str
    submit_sent_ns: int
    submit_recv_ns: int | None
    submit_outcome: str
    order_id: str | None
    reject_reason: str | None = None
    cancel_sent_ns: int | None = None
    cancel_recv_ns: int | None = None
    cancel_outcome: str | None = None
    status: dict[str, Any] | None = None
    post_only: bool = False
    #: base and counter currency codes from the log's `market_info` record ("" when the log has none)
    base_currency: str = ""
    counter_currency: str = ""
    raw: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return (self.log_index, self.index)

    @property
    def fee_currency(self) -> str:
        return self.counter_currency or (self.market[-3:] if len(self.market) > 3 else "")

    def fees(self) -> dict[int, float]:
        """Fee per trade id (`tid`) from the final `order_status` transactions."""
        out: dict[int, float] = {}
        if not isinstance(self.status, dict):
            return out
        for t in self.status.get("transactions") or []:
            if not isinstance(t, dict) or t.get("fee") is None:
                continue
            try:
                out[int(t["tid"])] = float(Decimal(str(t["fee"])))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
        return out

    @property
    def venue_executed(self) -> int | None:
        """Executed amount from the final `order_status` transactions (None without one): the sum of each
        transaction's base-currency amount (`base_currency`, else the market symbol without its last three
        letters)."""
        if not isinstance(self.status, dict) or "transactions" not in self.status:
            return None
        total = 0
        base = self.base_currency or (self.market[:-3] if len(self.market) > 3 else "")
        for t in self.status.get("transactions") or []:
            amt = t.get(base) if base else None
            if amt is None:
                continue
            total += abs(to_fixed(amt, SIZE_EXP, "order_status transaction amount"))
        return total


def _bad(path: str | Path, line: int, msg: str) -> QtError:
    return QtError("QT-INPUT-002", f"{path} record {line}: {msg}")


def harness_orders(logs: list[str | Path]) -> list[HarnessOrder]:
    """Submitted orders of one or more harness logs, in log order. Malformed submit records fail with
    `QT-INPUT-002`; a client order id used by two submits fails with `QT-INPUT-003`."""
    out: list[HarnessOrder] = []
    seen: dict[str, tuple[str, int]] = {}
    for li, path in enumerate(logs):
        recs = read_log(path)
        start = recs[0]
        run, mode, market = start.get("run_tag", ""), start.get("mode", ""), start.get("market", "")
        by_i: dict[int, HarnessOrder] = {}
        base = counter = ""
        for n, r in enumerate(recs[1:], start=2):
            t = r.get("type")
            i = r.get("i")
            if t == "market_info":
                base = str(r.get("base_currency") or "").lower()
                counter = str(r.get("counter_currency") or "").lower()
                continue
            if t == "submit":
                params = r.get("params")
                if not isinstance(i, int) or isinstance(i, bool) or i in by_i:
                    raise _bad(path, n, f"submit needs a unique integer order index i, got {i!r}")
                if not isinstance(params, dict) or not all(isinstance(params.get(k), str) for k in ("price", "amount")):
                    raise _bad(path, n, "submit params need price and amount strings")
                sent = r.get("sent_ns")
                if not isinstance(sent, int) or isinstance(sent, bool):
                    raise _bad(path, n, f"submit sent_ns must be an integer, got {sent!r}")
                coid = r.get("client_order_id") or params.get("client_order_id") or ""
                if not isinstance(coid, str) or not coid:
                    raise _bad(path, n, "submit without a client_order_id")
                if coid in seen:
                    first = seen[coid]
                    raise QtError(
                        "QT-INPUT-003",
                        f"client order id {coid} is submitted twice ({first[0]} order {first[1]} and {path} order {i})",
                    )
                seen[coid] = (str(path), i)
                body = r.get("body") if isinstance(r.get("body"), dict) else {}
                oid = body.get("id") if r.get("outcome") == "ok" else None
                o = HarnessOrder(
                    run=run,
                    mode=mode,
                    log_index=li,
                    index=i,
                    client_order_id=coid,
                    market=market,
                    side=r.get("side") or ("buy" if "/buy/" in f"/{r.get('endpoint', '')}/" else "sell"),
                    price_str=params["price"],
                    amount_str=params["amount"],
                    submit_sent_ns=sent,
                    submit_recv_ns=r.get("recv_ns"),
                    submit_outcome=str(r.get("outcome")),
                    order_id=str(oid) if oid is not None else None,
                    post_only=str(params.get("moc_order", "")).lower() == "true",
                    base_currency=base,
                    counter_currency=counter,
                    reject_reason=(body.get("reason") if isinstance(body.get("reason"), str) else None)
                    if r.get("outcome") == "error"
                    else None,
                    raw=[r],
                )
                if o.reject_reason is None and r.get("outcome") == "error":
                    o.reject_reason = str(body.get("reason") or r.get("body"))
                by_i[o.index] = o
                out.append(o)
                continue
            o2 = by_i.get(i) if isinstance(i, int) else None
            if o2 is None:
                continue
            o2.raw.append(r)
            if t == "cancel":
                if o2.cancel_sent_ns is None and isinstance(r.get("sent_ns"), int):
                    o2.cancel_sent_ns, o2.cancel_recv_ns, o2.cancel_outcome = (
                        r["sent_ns"],
                        r.get("recv_ns"),
                        r.get("outcome"),
                    )
            elif t == "status" and r.get("outcome") == "ok" and isinstance(r.get("body"), dict):
                o2.status = r["body"]
                if o2.order_id is None and r["body"].get("id") is not None:
                    o2.order_id = str(r["body"]["id"])
    return out


@dataclass
class OwnTrack:
    """Feed-side facts about one own order id."""

    oid: int
    segment: int
    market: str
    side: str | None = None
    px: int | None = None
    created: OrderEvent | None = None
    amt_created: int | None = None
    last_amt: int | None = None
    deleted: OrderEvent | None = None
    #: sum of `data.amount_traded` over the order's messages (Bitstamp reports the amount executed by each change,
    #: not a running total; a cancel's delete carries 0)
    traded_sum: int | None = None
    entry: dict[str, Any] | None = None
    fills: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    fifo_violations: int = 0
    #: the id also has messages in a later segment (its life spans a gap)
    crosses_gap: bool = False
    #: only `order_created` + `order_deleted` within the ingest's taker window and no trade naming it as the maker:
    #: `queuetruth ingest` drops the order as a taker, so the dataset has no record of it
    ingest_suppressed: bool = False
    #: trades that name this order as the passive (maker) side
    maker_trades: int = 0


def _signature_match(capture_dir: str | Path, o: HarnessOrder, used: set[int]) -> list[int]:
    """Ids of unmatched `order_created` events with the order's side, price and amount in its time window."""
    try:
        px = to_fixed(o.price_str, PRICE_EXP, "price")
        amt = to_fixed(o.amount_str, SIZE_EXP, "amount")
    except QtError:
        return []
    lo, hi = o.submit_sent_ns - SIGNATURE_BEFORE_NS, o.submit_sent_ns + SIGNATURE_AFTER_NS
    return sorted(
        {
            e.oid
            for e in read_creates(capture_dir, o.market, lo, hi)
            if e.side == o.side and e.px == px and e.amt == amt and e.oid not in used
        }
    )


def _replay_stream(st: SegmentStream, own_ids: set[int], ids_here: set[int], book_seed: str) -> dict[int, OwnTrack]:
    """Tracks the own ids of one (segment, market) stream."""
    seg, market = st.segment, st.market
    s = choose_seed(st, book_seed)
    book = L3Book()
    seed_book = st.book(s) if s else None
    limits = seed_limits(seed_book, s.source) if s and seed_book else {}
    seed_ids = {x[3] for x in seed_book.entries} if seed_book else set()
    if seed_book:
        book.seed(list(seed_book.entries))
    del seed_book
    skip = taker_ids(st.orders, DEFAULT_TAKER_WINDOW_US, maker_ids(st.trades)) - own_ids
    n_msgs: dict[int, int] = {}
    for e in st.orders:
        if e.oid in ids_here:
            n_msgs[e.oid] = n_msgs.get(e.oid, 0) + 1
    events: list[OrderEvent | TradeEvent] = sorted(
        [e for e in st.orders if s is None or e.us >= s.us] + [t for t in st.trades if s is None or t.us >= s.us],
        key=lambda e: e.seq,
    )
    local: dict[int, OwnTrack] = {}
    for oid in ids_here:
        local[oid] = OwnTrack(oid=oid, segment=seg, market=market)
        if oid in seed_ids:
            local[oid].entry = {"error": "order already resting in the seed snapshot (entered before the segment)"}
    last_ahead: dict[int, tuple[int, int]] = {}

    def trace(oid: int, cause: str, e: OrderEvent | TradeEvent) -> None:
        a = book.ahead(oid)
        if a is None:
            return
        key = (a.orders, a.qty)
        if last_ahead.get(oid) == key and cause != "order_created":
            return
        last_ahead[oid] = key
        r = book.orders[oid]
        local[oid].trace.append(
            {
                "order_id": oid,
                "seq": e.seq,
                "us": e.us,
                "recv_ns": e.recv_ns,
                "cause": cause,
                "ahead_orders": a.orders,
                "ahead_qty": a.qty,
                "level_qty": book.level_qty(r.side, r.px),
                "level_rank": a.level_rank,
            }
        )

    # executions already printed on live_trades whose order message has not arrived yet (Bitstamp sends the
    # trade first): maker order id -> quantity
    pending: dict[int, int] = {}

    def ahead_now(oid: int) -> int | None:
        r = book.orders.get(oid)
        if r is None:
            return None
        q = 0
        for o in book.levels[r.side][r.px]:
            if o == oid:
                break
            q += max(book.orders[o].amt - pending.get(o, 0), 0)
        return q

    for e in events:
        if isinstance(e, TradeEvent):
            maker = e.sell_id if e.aggressor == BUY else e.buy_id
            for oid in (e.buy_id, e.sell_id):
                if oid not in local:
                    continue
                tr = local[oid]
                if oid == maker:
                    tr.maker_trades += 1
                own_side = BUY if oid == e.buy_id else "sell"
                q = ahead_now(oid)
                if q is not None and q > 0 and own_side != e.aggressor:
                    tr.fifo_violations += 1
                tr.fills.append(
                    {
                        "order_id": oid,
                        "trade_id": e.trade_id,
                        "seq": e.seq,
                        "us": e.us,
                        "recv_ns": e.recv_ns,
                        "px": e.px,
                        "price_str": e.price_str,
                        "qty": e.amt,
                        "amount_str": e.amount_str,
                        "liquidity": "taker" if own_side == e.aggressor else "maker",
                        "ahead_qty_before": q,
                    }
                )
            pending[maker] = pending.get(maker, 0) + e.amt
            continue
        pending.pop(e.oid, None)
        # order event: own orders at the event's level, and at the level the order leaves (a re-price away from an
        # own order's level changes the quantity ahead of it too), noted before applying
        prev = book.orders.get(e.oid)
        levels = {(e.side, e.px)} | ({(prev.side, prev.px)} if prev is not None else set())
        touched = [
            oid for oid in local if oid in book.orders and (book.orders[oid].side, book.orders[oid].px) in levels
        ]
        apply(book, e, skip)
        if e.oid in local:
            tr = local[e.oid]
            tr.side = tr.side or e.side
            if e.amount_traded is not None:
                tr.traded_sum = (tr.traded_sum or 0) + e.amount_traded
            if e.event == "order_created" and tr.created is None:
                tr.created, tr.px, tr.amt_created, tr.last_amt = e, e.px, e.amt, e.amt
                a = book.ahead(e.oid)
                if tr.entry is None and a is not None:
                    tr.entry = {
                        "ahead_orders": a.orders,
                        "ahead_qty": a.qty,
                        "level_rank": a.level_rank,
                        "better_qty": a.better_qty,
                        "exact": bool(s) and better(e.side, e.px, limits.get(e.side)),
                        "seed_source": s.source if s else None,
                    }
            elif e.event == "order_changed":
                tr.px, tr.last_amt = e.px, e.amt
            elif e.event == "order_deleted":
                tr.deleted, tr.last_amt = e, e.amt
            trace(e.oid, e.event, e)
        for oid in touched:
            if oid != e.oid:
                trace(oid, e.event, e)
    for oid, tr in local.items():
        tr.ingest_suppressed = (
            tr.created is not None
            and tr.deleted is not None
            and n_msgs.get(oid) == 2
            and tr.deleted.us - tr.created.us <= DEFAULT_TAKER_WINDOW_US
            # the ingest keeps a maker filled within the window (`maker_in_taker_window`, qt_crypto::bitstamp)
            and tr.maker_trades == 0
        )
    return local


def replay_own(capture_dir: str | Path, own_ids: set[int], book_seed: str = "auto") -> dict[int, OwnTrack]:
    """Replays the (segment, market) streams that hold any of the given order ids, one at a time, and tracks them."""
    tracks: dict[int, OwnTrack] = {}
    keys = streams_with_ids(capture_dir, own_ids)
    if not keys:
        return tracks
    starts = stream_index(capture_dir).segment_start_ns
    for seg, market in sorted(keys):
        st = load_stream(capture_dir, seg, market, starts.get(seg))
        ids_here = {e.oid for e in st.orders if e.oid in own_ids} | {
            x for t in st.trades for x in (t.buy_id, t.sell_id) if x in own_ids
        }
        if not ids_here:
            continue
        local = _replay_stream(st, own_ids, ids_here, book_seed)
        del st
        for oid, tr in local.items():
            prev = tracks.get(oid)
            if prev is None or (prev.created is None and tr.created is not None):
                if prev is not None:
                    tr.crosses_gap = True
                tracks[oid] = tr
            else:
                prev.crosses_gap = True
    return tracks


def match(
    capture_dir: str | Path,
    logs: list[str | Path],
    out: str | Path | None = None,
    book_seed: str = "auto",
    select: Callable[[HarnessOrder], bool] | None = None,
) -> dict[str, Any]:
    """Matches harness orders to the feed; with `out`, writes `own_orders.parquet`, `own_fills.parquet`,
    `own_queue.parquet` and `match.json`. `select` keeps only some orders (the pipeline gives each capture the orders
    sent while it was recording). Returns the `livefill.match` document (with `orders` and `fills`)."""
    meta = read_meta(capture_dir)
    markets = list(meta["markets"])
    horders = harness_orders(logs)
    if select is not None:
        horders = [o for o in horders if select(o)]
    by_id: dict[int, HarnessOrder] = {}
    for o in horders:
        if o.order_id is not None and o.order_id.isdigit():
            if int(o.order_id) in by_id:
                raise QtError("QT-INPUT-003", f"order id {o.order_id} appears in two harness submits")
            by_id[int(o.order_id)] = o
    in_feed = created_ids(capture_dir, set(by_id))
    matched_by: dict[tuple[int, int], str] = {}
    oid_of: dict[tuple[int, int], int] = {}
    used = set(in_feed)
    id_misses = 0
    for o in horders:
        k = o.key
        if o.order_id is not None and o.order_id.isdigit() and int(o.order_id) in in_feed:
            matched_by[k], oid_of[k] = "id", int(o.order_id)
            continue
        if o.submit_outcome == "error":
            matched_by[k] = "rejected"
            continue
        if o.order_id is not None and not o.order_id.isdigit():
            matched_by[k] = "not_in_feed"  # a dry-run id: never in a real feed
            continue
        miss = o.order_id is not None
        id_misses += miss
        cands = _signature_match(capture_dir, o, used)
        if len(cands) == 1:
            oid_of[k] = cands[0]
            used.add(cands[0])
            matched_by[k] = "signature_after_id_miss" if miss else "signature"
        else:
            matched_by[k] = "ambiguous" if cands else "not_in_feed"
    matched_oids = set(oid_of.values())
    tracks = replay_own(capture_dir, matched_oids, book_seed)
    rows: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    live_without_fee = 0
    for o in horders:
        mb = matched_by[o.key]
        oid = oid_of.get(o.key)
        if oid is None and o.order_id and o.order_id.isdigit():
            oid = int(o.order_id)
        tr = tracks.get(oid) if oid in matched_oids else None
        if mb in ("id", "signature", "signature_after_id_miss") and (tr is None or tr.created is None):
            mb = "not_in_feed"
        row = _row(o, mb, oid, tr, markets)
        if tr is not None and tr.created is not None:
            fees = o.fees()
            for f in tr.fills:
                fee = fees.get(f["trade_id"])
                if fee is None and o.mode == "live":
                    live_without_fee += 1
                fills.append(
                    {
                        **f,
                        "client_order_id": o.client_order_id,
                        "fee": fee,
                        "fee_ccy": o.fee_currency if fee is not None else None,
                    }
                )
            trace += [{**q, "client_order_id": o.client_order_id} for q in tr.trace]
        rows.append(row)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["matched_by"]] = counts.get(r["matched_by"], 0) + 1
    found = [r for r in rows if r["created_us"] is not None]
    warnings = []
    if id_misses:
        warnings.append(
            f"id_misses: {id_misses} order id(s) returned by the venue are not order ids of the public feed; they were "
            "searched for by signature. On a live run this means the REST and feed ids differ: investigate before "
            "trusting the run."
        )
    if live_without_fee:
        warnings.append(f"live_fills_without_fee: {live_without_fee} live fill(s) have no order_status transaction")
    doc = {
        "schema": "livefill.match",
        "schema_version": 1,
        "capture": str(Path(capture_dir)),
        "logs": [str(Path(x)) for x in logs],
        "markets": markets,
        "book_seed": book_seed,
        "segments": [
            {"segment": x["segment"], "first_recv_ns": x["first_recv_ns"], "last_recv_ns": x["last_recv_ns"]}
            for x in meta.get("segments", [])
        ],
        "n_orders": len(rows),
        "matched_by": dict(sorted(counts.items())),
        "id_misses": id_misses,
        "n_in_feed": len(found),
        "n_filled_any": sum(1 for r in found if r["filled_qty"]),
        "n_entry_exact": sum(1 for r in found if r["entry_exact"]),
        "fills_inconsistent": sum(1 for r in found if r["fills_consistent"] is False),
        "venue_inconsistent": sum(1 for r in found if r["venue_consistent"] is False),
        "fifo_violations": sum(r["fifo_violations"] or 0 for r in found),
        "ingest_suppressed": sum(1 for r in found if r["ingest_suppressed"]),
        "fills_with_fee": sum(1 for f in fills if f["fee"] is not None),
        "live_fills_without_fee": live_without_fee,
        "warnings": warnings,
        "orders": rows,
    }
    if out is not None:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        _write(out / "own_orders.parquet", rows, ORDER_SCHEMA)
        _write(out / "own_fills.parquet", fills, FILL_SCHEMA)
        _write(out / "own_queue.parquet", trace, QUEUE_SCHEMA)
        (out / "match.json").write_text(json.dumps({k: v for k, v in doc.items() if k != "orders"}, indent=1) + "\n")
    doc["fills"] = fills
    return doc


def _row(o: HarnessOrder, mb: str, oid: int | None, tr: OwnTrack | None, markets: list[str]) -> dict[str, Any]:
    r: dict[str, Any] = {
        "client_order_id": o.client_order_id,
        "log_index": o.log_index,
        "run": o.run,
        "mode": o.mode,
        "index": o.index,
        "market": o.market,
        "instrument_id": markets.index(o.market) + 1 if o.market in markets else None,
        "side": o.side,
        "price_str": o.price_str,
        "amount_str": o.amount_str,
        "order_id": oid,
        "matched_by": mb,
        "submit_sent_ns": o.submit_sent_ns,
        "submit_recv_ns": o.submit_recv_ns,
        "cancel_sent_ns": o.cancel_sent_ns,
        "cancel_recv_ns": o.cancel_recv_ns,
        "segment": None,
        "created_seq": None,
        "created_us": None,
        "created_recv_ns": None,
        "entry_ahead_orders": None,
        "entry_ahead_qty": None,
        "entry_level_rank": None,
        "entry_better_qty": None,
        "entry_exact": None,
        "entry_note": None,
        "seed_source": None,
        "n_fills": 0,
        "filled_qty": 0,
        "first_fill_us": None,
        "deleted_us": None,
        "deleted_recv_ns": None,
        "remaining_qty": None,
        "end_state": "rejected" if mb == "rejected" else "not_in_feed",
        "fills_consistent": None,
        "amount_traded_consistent": None,
        "venue_executed_qty": o.venue_executed,
        "venue_consistent": None,
        "fifo_violations": None,
        "crosses_gap": None,
        "ingest_suppressed": None,
        "reject_reason": o.reject_reason,
        "post_only": o.post_only,
    }
    if tr is None or tr.created is None:
        return r
    c = tr.created
    filled = sum(f["qty"] for f in tr.fills)
    r.update(
        segment=tr.segment,
        created_seq=c.seq,
        created_us=c.us,
        created_recv_ns=c.recv_ns,
        n_fills=len(tr.fills),
        filled_qty=filled,
        first_fill_us=min((f["us"] for f in tr.fills), default=None),
        fifo_violations=tr.fifo_violations,
        crosses_gap=tr.crosses_gap,
        ingest_suppressed=tr.ingest_suppressed,
        remaining_qty=tr.last_amt,
    )
    if tr.entry:
        if "error" in tr.entry:
            r["entry_note"] = tr.entry["error"]
            r["entry_exact"] = False
        else:
            r.update(
                entry_ahead_orders=tr.entry["ahead_orders"],
                entry_ahead_qty=tr.entry["ahead_qty"],
                entry_level_rank=tr.entry["level_rank"],
                entry_better_qty=tr.entry["better_qty"],
                entry_exact=tr.entry["exact"],
                seed_source=tr.entry["seed_source"],
            )
    else:
        r["entry_exact"] = False
        r["entry_note"] = "not on the replayed book at entry (taker, or crossed)"
    if tr.deleted is not None:
        r["deleted_us"], r["deleted_recv_ns"] = tr.deleted.us, tr.deleted.recv_ns
        if tr.deleted.amt == 0:
            r["end_state"] = "filled"
        elif o.cancel_sent_ns is not None and o.cancel_sent_ns <= tr.deleted.recv_ns:
            r["end_state"] = "cancelled"
        else:
            r["end_state"] = "removed"
    else:
        r["end_state"] = "open_at_end"
    lost = (tr.amt_created or 0) - (tr.last_amt or 0)
    r["fills_consistent"] = lost == filled
    if tr.traded_sum is not None:
        r["amount_traded_consistent"] = tr.traded_sum == filled
    if o.venue_executed is not None:
        r["venue_consistent"] = o.venue_executed == filled
    return r


ORDER_SCHEMA = pa.schema(
    [
        ("client_order_id", pa.string()),
        ("log_index", pa.int32()),
        ("run", pa.string()),
        ("mode", pa.string()),
        ("index", pa.int32()),
        ("market", pa.string()),
        ("instrument_id", pa.uint32()),
        ("side", pa.string()),
        ("price_str", pa.string()),
        ("amount_str", pa.string()),
        ("order_id", pa.int64()),
        ("matched_by", pa.string()),
        ("submit_sent_ns", pa.int64()),
        ("submit_recv_ns", pa.int64()),
        ("cancel_sent_ns", pa.int64()),
        ("cancel_recv_ns", pa.int64()),
        ("segment", pa.int32()),
        ("created_seq", pa.int64()),
        ("created_us", pa.int64()),
        ("created_recv_ns", pa.int64()),
        ("entry_ahead_orders", pa.int64()),
        ("entry_ahead_qty", pa.int64()),
        ("entry_level_rank", pa.int32()),
        ("entry_better_qty", pa.int64()),
        ("entry_exact", pa.bool_()),
        ("entry_note", pa.string()),
        ("seed_source", pa.string()),
        ("n_fills", pa.int32()),
        ("filled_qty", pa.int64()),
        ("first_fill_us", pa.int64()),
        ("deleted_us", pa.int64()),
        ("deleted_recv_ns", pa.int64()),
        ("remaining_qty", pa.int64()),
        ("end_state", pa.string()),
        ("fills_consistent", pa.bool_()),
        ("amount_traded_consistent", pa.bool_()),
        ("venue_executed_qty", pa.int64()),
        ("venue_consistent", pa.bool_()),
        ("fifo_violations", pa.int32()),
        ("crosses_gap", pa.bool_()),
        ("ingest_suppressed", pa.bool_()),
        ("reject_reason", pa.string()),
        ("post_only", pa.bool_()),
    ],
    metadata={b"livefill.schema": b"livefill.own_orders", b"livefill.schema_version": b"1"},
)
FILL_SCHEMA = pa.schema(
    [
        ("client_order_id", pa.string()),
        ("order_id", pa.int64()),
        ("trade_id", pa.int64()),
        ("seq", pa.int64()),
        ("us", pa.int64()),
        ("recv_ns", pa.int64()),
        ("px", pa.int64()),
        ("price_str", pa.string()),
        ("qty", pa.int64()),
        ("amount_str", pa.string()),
        ("liquidity", pa.string()),
        ("ahead_qty_before", pa.int64()),
        ("fee", pa.float64()),
        ("fee_ccy", pa.string()),
    ],
    metadata={b"livefill.schema": b"livefill.own_fills", b"livefill.schema_version": b"1"},
)
QUEUE_SCHEMA = pa.schema(
    [
        ("client_order_id", pa.string()),
        ("order_id", pa.int64()),
        ("seq", pa.int64()),
        ("us", pa.int64()),
        ("recv_ns", pa.int64()),
        ("cause", pa.string()),
        ("ahead_orders", pa.int64()),
        ("ahead_qty", pa.int64()),
        ("level_qty", pa.int64()),
        ("level_rank", pa.int32()),
    ],
    metadata={b"livefill.schema": b"livefill.own_queue", b"livefill.schema_version": b"1"},
)


def _write(path: Path, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
    cols = {f.name: [r.get(f.name) for r in rows] for f in schema}
    pq.write_table(pa.Table.from_pydict(cols, schema=schema), path, compression="zstd")
