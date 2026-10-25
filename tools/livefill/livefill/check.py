"""Capture validation: replay each segment's `live_orders` on its seed snapshot and compare the replayed book with
every later `detail_order_book` snapshot the venue sent (its best 100 orders per side: ids, amounts and queue
order).

Seeding. The seed is applied, then every `live_orders` message stamped at or after the seed's `microtimestamp`, in
arrival order (the rule `queuetruth ingest --format bitstamp` uses; messages stamped earlier are already in the seed).
This also aligns a REST seed, whose body can be older than its arrival.

Alignment. The snapshot channel and `live_orders` are not delivered in lockstep: a snapshot often already reflects
order messages that arrive tens of messages later, and `live_orders` stamps are whole milliseconds. So a snapshot is
compared with the book replayed up to its own arrival position `p`, and every difference must be *explained* by a
message about the same order that arrived within `window` messages of `p` (before or after). A difference with no
such message is *unexplained*: a lost or wrong message, or a wrong queue rule. Differences:

- `missing`: an order on the snapshot that the book does not hold; `extra`: the reverse;
- `amount` / `price`: the same order with another amount or price;
- `queue_order`: the same orders at a price level in another priority order (explained when any order of that
  level has a message within the window).

Compared region. A WebSocket seed (`detail_order_book`) shows only the best 100 orders per side, so when a side is
cut, only prices strictly better than the seed's worst price are known order by order; and a snapshot side with 100
entries may cut its own worst level. Each side is compared over the prices strictly better than both limits. A
REST seed (`order_book?group=2`) is the whole book.

Lost orders between snapshots. Inside that known region every order is on the replayed book, so an `order_deleted`
of an order that the book does not hold, that is not a taker and whose `order_created` the stream never carried is
direct evidence of a lost `order_created` (`unknown_deletes`; with a REST seed at any price). An order whose create
did arrive is not counted even when the seed missed it: on the RD-12 capture one order stamped 2.4 ms before the
seed was not yet in it (`test_rd12_capture_round_trip_and_check`). Unknown `order_changed` messages are not counted
either: a change can move an order into the region from a price the seed did not show.

Taker orders: Bitstamp shows an incoming order that trades on arrival as `order_created` then `order_deleted`; an
order created and deleted within `taker_window_us` (default 100, the ingest's default) with nothing in between never
rested and is not added to the book (`takers_skipped`), as the ingest does, unless a trade shows it as the resting
(maker) side: an order that rests and is filled within the window is kept (the ingest drops it; see `matcher.py`,
`ingest_suppressed`).

The `livefill.capture_check` document holds, per (segment, market), the seed, `compared`, `exact` (no difference),
`explained`, `unexplained` snapshots, the differences by kind, `max_skew_messages` (largest distance between a
snapshot and the message explaining one of its differences), `unknown_deletes`, examples of unexplained differences
and the replay counters, plus the capture's gaps. `verdict`, the first that applies: `inconsistent` (an unexplained
difference or an unknown delete), `unchecked` (nothing could be compared), `partial` (some (segment, market) stream
had no seed or no later snapshot: `unchecked_streams` names them), `gaps` (consistent within segments, but the
capture has gaps), `consistent`.

Memory: streams are loaded and checked one at a time (`stream.iter_streams`), and each snapshot is parsed only when
it is compared.
"""

from __future__ import annotations

import bisect
from pathlib import Path
from typing import Any

from livefill.book import BUY, SELL, L3Book
from livefill.store import read_meta
from livefill.stream import Book, OrderEvent, SegmentStream, Snapshot, TradeEvent, iter_streams
from queuetruth.errors import QtError

DEFAULT_TAKER_WINDOW_US = 100
#: live_orders messages between a snapshot and a message that explains one of its differences.
DEFAULT_WINDOW = 256
MAX_EXAMPLES = 5
DIFF_KINDS = ("missing", "extra", "amount", "price", "queue_order")
SEED_CHOICES = ("ws", "rest", "auto")


def taker_ids(orders: list[OrderEvent], window_us: int | None, makers: set[int] | None = None) -> set[int]:
    """Ids created and deleted within `window_us` with no other event in between (they never rested), except the
    ids in `makers` (orders a trade shows resting: a maker filled within the window is not a taker)."""
    if window_us is None:
        return set()
    makers = makers or set()
    first: dict[int, OrderEvent] = {}
    n_events: dict[int, int] = {}
    out: set[int] = set()
    for e in sorted(orders, key=lambda e: (e.us, e.recv_ns, e.seq)):
        n_events[e.oid] = n_events.get(e.oid, 0) + 1
        if e.event == "order_created" and e.oid not in first:
            first[e.oid] = e
        elif e.event == "order_deleted":
            c = first.get(e.oid)
            if c is not None and n_events[e.oid] == 2 and e.us - c.us <= window_us and e.oid not in makers:
                out.add(e.oid)
    return out


def maker_ids(trades: list[TradeEvent]) -> set[int]:
    """Order ids on the passive side of a trade."""
    return {t.sell_id if t.aggressor == BUY else t.buy_id for t in trades}


def apply(book: L3Book, e: OrderEvent, skip: set[int]) -> None:
    """Applies one `live_orders` event (ids in `skip` are takers and never rest)."""
    if e.oid in skip:
        return
    if e.event == "order_created":
        book.created(e.oid, e.side, e.px, e.amt, e.us)
    elif e.event == "order_changed":
        book.changed(e.oid, e.side, e.px, e.amt, e.us)
    else:
        book.deleted(e.oid)


def _clean(st: SegmentStream, snaps: list[Snapshot]) -> list[Snapshot]:
    """The snapshots after which no order or trade message arrives stamped earlier than the snapshot."""
    stamps = sorted([(e.seq, e.us) for e in st.orders] + [(t.seq, t.us) for t in st.trades])
    suffix = [0] * (len(stamps) + 1)
    suffix[-1] = 2**63
    for k in range(len(stamps) - 1, -1, -1):
        suffix[k] = min(stamps[k][1], suffix[k + 1])
    seqs = [x[0] for x in stamps]
    return [s for s in snaps if suffix[bisect.bisect_right(seqs, s.seq)] >= s.us]


def choose_seed(st: SegmentStream, prefer: str = "auto", clean: bool = True) -> Snapshot | None:
    """The seed snapshot of a segment: `ws` = the first `detail_order_book`, `rest` = the first REST book, `auto` = the
    first REST book when the segment has one (the whole book), else the first `detail_order_book`; in each case the
    first *unambiguous* one: no message arriving after it may be stamped before it.

    Why: `live_orders` stamps are whole milliseconds while snapshots carry microseconds, and the ingest applies after
    the seed exactly the messages stamped at or after the seed's stamp. A message stamped in the seed's millisecond
    but arriving after the seed may or may not be reflected in it, so such a seed is skipped for a later one
    (`clean=False` takes the first snapshot regardless, as the RD-12 ingest did).

    A REST book of a segment after the first must also be stamped at or after the segment's first frame: a book
    requested before a gap describes the market before the gap (the capture stores answers that arrive on a later
    segment as `stale_rest_book`; this is the second line of defence). The venue stamp is compared with the local
    receive clock, which assumes the two agree to within the subscription round trip."""
    if prefer not in SEED_CHOICES:
        raise QtError("QT-USAGE-001", f"book seed: expected one of {list(SEED_CHOICES)}, got {prefer!r}")
    ws = [s for s in st.snapshots if s.source == "ws"]
    rest = [s for s in st.snapshots if s.source == "rest"]
    if st.segment > 0 and st.segment_start_ns is not None:
        rest = [s for s in rest if s.us * 1000 >= st.segment_start_ns]
    if clean:
        ws, rest = _clean(st, ws), _clean(st, rest)
    first_ws = ws[0] if ws else None
    first_rest = rest[0] if rest else None
    if prefer == "ws":
        return first_ws
    if prefer == "rest":
        return first_rest
    return first_rest or first_ws


def better(side: str, px: int, limit: int | None) -> bool:
    """True when `px` is strictly better than `limit` on `side` (always true without a limit)."""
    return limit is None or (px > limit if side == BUY else px < limit)


def seed_limits(seed: Book, source: str) -> dict[str, int | None]:
    """Per side, the price from which the seed may not show every order: the seed's worst price when a WebSocket
    side was cut at 100 entries, else None (the whole side is known)."""
    out: dict[str, int | None] = {}
    for side, n in ((BUY, seed.n_bids), (SELL, seed.n_asks)):
        pxs = [px for s, px, _, _ in seed.entries if s == side]
        out[side] = (min(pxs) if side == BUY else max(pxs)) if source == "ws" and n >= 100 and pxs else None
    return out


def _tighter(side: str, a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b) if side == BUY else min(a, b)


def differences(book: L3Book, snap: Book, limits: dict[str, int | None]) -> list[tuple[str, str, int, int]]:
    """Differences `(kind, side, order_id, price)` between the book and a snapshot over the compared region."""
    out: list[tuple[str, str, int, int]] = []
    for side, n in ((BUY, snap.n_bids), (SELL, snap.n_asks)):
        want_all = [(px, amt, oid) for s, px, amt, oid in snap.entries if s == side]
        cut = want_all[-1][0] if n >= 100 and want_all else None
        limit = _tighter(side, limits[side], cut)
        want = {oid: (px, amt) for px, amt, oid in want_all if better(side, px, limit)}
        want_levels: dict[int, list[int]] = {}
        for px, _, oid in want_all:
            if oid in want:
                want_levels.setdefault(px, []).append(oid)
        got: dict[int, tuple[int, int]] = {}
        for px in book.prices(side):
            if not better(side, px, limit):
                break
            for o in book.levels[side][px]:
                got[o] = (px, book.orders[o].amt)
        for oid, (px, amt) in want.items():
            g = got.get(oid)
            if g is None:
                out.append(("missing", side, oid, px))
            elif g[0] != px:
                out.append(("price", side, oid, px))
            elif g[1] != amt:
                out.append(("amount", side, oid, px))
        out.extend(("extra", side, oid, px) for oid, (px, _) in got.items() if oid not in want)
        for px, ids in want_levels.items():
            level = book.levels[side].get(px, {})
            common = set(ids)
            book_ids = [o for o in level if o in common]
            snap_ids = [o for o in ids if o in level]
            if book_ids != snap_ids:
                out.append(("queue_order", side, 0, px))
    return out


class _Positions:
    """Arrival positions of every order's messages, for window lookups."""

    def __init__(self, events: list[OrderEvent]) -> None:
        self.by_oid: dict[int, list[int]] = {}
        for k, e in enumerate(events):
            self.by_oid.setdefault(e.oid, []).append(k)

    def distance(self, oid: int, p: int) -> int | None:
        """Smallest |k - p| over the order's message positions `k` (None when the order has no message)."""
        ks = self.by_oid.get(oid)
        if not ks:
            return None
        i = bisect.bisect_left(ks, p)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(ks):
                d = abs(ks[j] - p)
                best = d if best is None else min(best, d)
        return best


def check_stream(
    st: SegmentStream,
    book_seed: str = "auto",
    taker_window_us: int | None = DEFAULT_TAKER_WINDOW_US,
    window: int = DEFAULT_WINDOW,
    id_ties: bool = True,
) -> dict[str, Any]:
    """Replays one (segment, market) stream and compares it with its later WebSocket snapshots. `id_ties=False` ranks
    same-millisecond orders by arrival instead of order id (the ingest's rule; for measuring the difference)."""
    seed = choose_seed(st, book_seed)
    res: dict[str, Any] = {
        "segment": st.segment,
        "market": st.market,
        "seed": None,
        "order_events": len(st.orders),
        "trades": len(st.trades),
        "snapshots": len(st.snapshots),
        "compared": 0,
        "exact": 0,
        "explained": 0,
        "unexplained": 0,
        "differences": dict.fromkeys(DIFF_KINDS, 0),
        "unexplained_differences": dict.fromkeys(DIFF_KINDS, 0),
        "max_skew_messages": 0,
        "unknown_deletes": 0,
        "examples": [],
        "takers_skipped": 0,
        "counters": {},
    }
    if seed is None:
        return res
    seed_book = st.book(seed)
    limits = seed_limits(seed_book, seed.source)
    res["seed"] = {
        "source": seed.source,
        "seq": seed.seq,
        "us": seed.us,
        "bids": seed_book.n_bids,
        "asks": seed_book.n_asks,
        "limits": dict(limits),
    }
    book = L3Book(id_ties=id_ties)
    book.seed(list(seed_book.entries))
    del seed_book
    skip = taker_ids(st.orders, taker_window_us, maker_ids(st.trades))
    res["takers_skipped"] = len(skip)
    events = sorted((e for e in st.orders if e.us >= seed.us), key=lambda e: e.seq)
    pos = _Positions(events)
    snaps = [s for s in st.snapshots if s.source == "ws" and s.seq > seed.seq]
    i = 0
    created = {e.oid for e in st.orders if e.event == "order_created"}

    def step(e: OrderEvent) -> None:
        if (
            e.event == "order_deleted"
            and e.oid not in skip
            and e.oid not in book.orders
            and e.oid not in created
            and better(e.side, e.px, limits[e.side])
        ):
            res["unknown_deletes"] += 1
            if len(res["examples"]) < MAX_EXAMPLES:
                res["examples"].append(
                    {"unknown_delete_seq": e.seq, "us": e.us, "order_id": e.oid, "side": e.side, "px": e.px}
                )
        apply(book, e, skip)

    for snap, snap_book in st.books(snaps):
        while i < len(events) and events[i].seq < snap.seq:
            step(events[i])
            i += 1
        res["compared"] += 1
        diffs = differences(book, snap_book, limits)
        if not diffs:
            res["exact"] += 1
            continue
        bad = []
        for kind, side, oid, px in diffs:
            res["differences"][kind] += 1
            if kind == "queue_order":
                ds = [pos.distance(o, i) for o in book.levels[side].get(px, {})]
                ds += [pos.distance(o, i) for s2, p2, _, o in snap_book.entries if s2 == side and p2 == px]
                d = min((x for x in ds if x is not None), default=None)
            else:
                d = pos.distance(oid, i)
            if d is not None and d <= window:
                res["max_skew_messages"] = max(res["max_skew_messages"], d)
            else:
                bad.append((kind, side, oid, px))
                res["unexplained_differences"][kind] += 1
        if bad:
            res["unexplained"] += 1
            if len(res["examples"]) < MAX_EXAMPLES:
                res["examples"].append(
                    {
                        "snapshot_seq": snap.seq,
                        "us": snap.us,
                        "differences": [{"kind": k, "side": s, "order_id": o, "px": p} for k, s, o, p in bad[:5]],
                    }
                )
        else:
            res["explained"] += 1
    while i < len(events):
        step(events[i])
        i += 1
    res["counters"] = dict(book.counters)
    return res


def _dup_trades(trades: list[TradeEvent]) -> int:
    return len(trades) - len({t.trade_id for t in trades})


def check_capture(
    capture_dir: str | Path,
    book_seed: str = "auto",
    taker_window_us: int | None = DEFAULT_TAKER_WINDOW_US,
    window: int = DEFAULT_WINDOW,
    id_ties: bool = True,
) -> dict[str, Any]:
    """The `livefill.capture_check` document of a capture directory (one stream in memory at a time)."""
    meta = read_meta(capture_dir)
    rows = []
    dup_trades = 0
    for st in iter_streams(capture_dir):
        rows.append(check_stream(st, book_seed, taker_window_us, window, id_ties))
        dup_trades += _dup_trades(st.trades)
    compared = sum(r["compared"] for r in rows)
    unexplained = sum(r["unexplained"] for r in rows)
    unknown_deletes = sum(r["unknown_deletes"] for r in rows)
    unchecked = [{"segment": r["segment"], "market": r["market"]} for r in rows if r["compared"] == 0]
    if unexplained or unknown_deletes:
        verdict = "inconsistent"
    elif compared == 0:
        verdict = "unchecked"
    elif unchecked:
        verdict = "partial"
    elif meta.get("gaps"):
        verdict = "gaps"
    else:
        verdict = "consistent"
    return {
        "schema": "livefill.capture_check",
        "schema_version": 1,
        "capture": str(Path(capture_dir)),
        "verdict": verdict,
        "book_seed": book_seed,
        "id_ties": id_ties,
        "window": window,
        "rows": int(meta.get("rows", 0)),
        "gaps": meta.get("gaps", []),
        "segments": len(meta.get("segments", [])),
        "anomalies": meta.get("anomalies", {}),
        "duplicate_trade_ids": dup_trades,
        "compared": compared,
        "exact": sum(r["exact"] for r in rows),
        "explained": sum(r["explained"] for r in rows),
        "unexplained": unexplained,
        "unknown_deletes": unknown_deletes,
        "max_skew_messages": max((r["max_skew_messages"] for r in rows), default=0),
        "unchecked_streams": unchecked,
        "streams": rows,
    }
