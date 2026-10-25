"""Typed events of one market and segment of a capture, in the replay order of `queuetruth ingest --format bitstamp`:
by (`microtimestamp`, `recv_ns`), order events before trades on ties, then arrival order.

Memory. A capture is read one (segment, market) stream at a time (`stream_index`, `load_stream`, `iter_streams`),
batch by batch: order and trade messages become compact slotted records, and of each book snapshot only its position
and stamp are kept. Snapshot entries are read again from the capture and parsed one at a time, in arrival order, when
they are needed (`SegmentStream.books`), so a stream costs its order and trade records, not its snapshots.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from livefill import feed
from livefill.book import side_of
from livefill.store import iter_capture, read_capture
from livefill.units import PRICE_EXP, SIZE_EXP, to_fixed
from queuetruth.errors import QtError

#: row kinds that carry market data (`duplicate`, `marker` and `stale_rest_book` rows never do)
DATA_KINDS = ("ws", "rest_book")
STREAM_COLUMNS = ["recv_ns", "segment", "kind", "market", "channel", "event", "microtimestamp", "raw"]


@dataclass(frozen=True, slots=True)
class OrderEvent:
    seq: int
    recv_ns: int
    us: int
    event: str  # order_created | order_changed | order_deleted
    oid: int
    side: str
    px: int
    amt: int
    #: `data.amount_traded` (the amount this message executed) when the message carries it
    amount_traded: int | None = None


@dataclass(frozen=True, slots=True)
class TradeEvent:
    seq: int
    recv_ns: int
    us: int
    trade_id: int
    buy_id: int
    sell_id: int
    aggressor: str  # side of the taker: buy when `type` = 0
    px: int
    amt: int
    price_str: str
    amount_str: str


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A book snapshot's position and stamp; its entries are read and parsed on demand (`SegmentStream.books`)."""

    seq: int
    recv_ns: int
    us: int
    source: str  # ws (detail_order_book) | rest (order_book?group=2)


@dataclass(frozen=True)
class Book:
    """A parsed snapshot: `(side, px, amt, oid)` in priority order, bids then asks."""

    entries: tuple[tuple[str, int, int, int], ...]
    n_bids: int
    n_asks: int


@dataclass
class SegmentStream:
    """Events of one (segment, market). Snapshot texts are read from `capture_dir` when needed, or from `raws`
    (seq -> text) for a stream built in memory."""

    segment: int
    market: str
    orders: list[OrderEvent] = field(default_factory=list)
    trades: list[TradeEvent] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    first_recv_ns: int | None = None
    last_recv_ns: int | None = None
    #: receive time of the segment's first row of any market (its connect marker or the frame that opened it)
    segment_start_ns: int | None = None
    capture_dir: Path | None = None
    raws: dict[int, str] | None = None

    def iter_raw(self, seqs: list[int]) -> Iterator[tuple[int, str]]:
        """`(seq, raw text)` of the given snapshot rows, in `seq` order."""
        want = sorted(set(seqs))
        if not want:
            return
        if self.raws is not None:
            for q in want:
                yield q, self.raws[q]
            return
        if self.capture_dir is None:
            raise QtError("QT-ENGINE-001", "snapshot text not available: the stream has no capture directory")
        filters = [("segment", "=", self.segment), ("market", "=", self.market), ("seq", "in", want)]
        for b in iter_capture(self.capture_dir, ["raw"], filters, segment=self.segment):
            yield from zip(b.column("seq").to_pylist(), b.column("raw").to_pylist(), strict=True)

    def raw(self, snap: Snapshot) -> str:
        """One snapshot's raw text (a `detail_order_book` frame or a REST body)."""
        for _, text in self.iter_raw([snap.seq]):
            return text
        raise QtError("QT-INPUT-002", f"capture row seq={snap.seq}: snapshot not found")

    def book(self, snap: Snapshot) -> Book:
        """One snapshot's entries (parsed now; nothing is cached)."""
        return parse_book(self.raw(snap), snap.seq, snap.source)

    def books(self, snaps: list[Snapshot]) -> Iterator[tuple[Snapshot, Book]]:
        """The given snapshots with their entries, in `seq` order, one parsed at a time."""
        by_seq = {x.seq: x for x in snaps}
        for q, text in self.iter_raw(list(by_seq)):
            sn = by_seq[q]
            yield sn, parse_book(text, q, sn.source)

    def merged(self) -> list[OrderEvent | TradeEvent]:
        """Order and trade events in replay order."""
        items: list[tuple[tuple[int, int, int, int], OrderEvent | TradeEvent]] = [
            ((e.us, e.recv_ns, 0, e.seq), e) for e in self.orders
        ]
        items += [((t.us, t.recv_ns, 1, t.seq), t) for t in self.trades]
        items.sort(key=lambda x: x[0])
        return [e for _, e in items]


def _fail(seq: int, msg: str) -> QtError:
    return QtError("QT-INPUT-002", f"capture row seq={seq}: {msg}")


def _num(d: dict[str, Any], key: str, seq: int) -> str:
    v = d.get(key)
    if isinstance(v, bool) or v is None:
        raise _fail(seq, f"missing {key}")
    return v if isinstance(v, str) else repr(v) if isinstance(v, float) else str(v)


def _int(d: dict[str, Any], key: str, seq: int) -> int:
    v = d.get(key)
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v)
    raise _fail(seq, f"missing or non-integer {key}")


def parse_book(raw: str, seq: int, source: str) -> Book:
    """Entries of a `detail_order_book` frame (source ws) or a REST `order_book?group=2` body (source rest)."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise _fail(seq, f"book snapshot is not JSON ({e.msg})") from e
    d = doc.get("data") if source == "ws" and isinstance(doc, dict) else doc
    if not isinstance(d, dict):
        raise _fail(seq, "book snapshot without data")
    entries: list[tuple[str, int, int, int]] = []
    counts = {}
    for key, side in (("bids", "buy"), ("asks", "sell")):
        lst = d.get(key)
        if not isinstance(lst, list):
            raise _fail(seq, f"book snapshot without {key}")
        for e in lst:
            if not (isinstance(e, list) and len(e) == 3):
                raise _fail(seq, f"{key} entry is not [price, amount, order_id]")
            px = to_fixed(e[0], PRICE_EXP, f"seq={seq} {key} price")
            amt = to_fixed(e[1], SIZE_EXP, f"seq={seq} {key} amount")
            oid = feed.int_or_none(e[2])
            if oid is None:
                raise _fail(seq, f"{key} order_id {e[2]!r} is not an integer")
            entries.append((side, px, amt, oid))
        counts[key] = len(lst)
    return Book(entries=tuple(entries), n_bids=counts["bids"], n_asks=counts["asks"])


@dataclass(frozen=True)
class StreamIndex:
    """The (segment, market) streams of a capture and each segment's first receive time (index columns only)."""

    keys: list[tuple[int, str]]
    segment_start_ns: dict[int, int]


def stream_index(capture_dir: str | Path) -> StreamIndex:
    """Reads only the `seq`, `recv_ns`, `segment`, `kind` and `market` columns."""
    t = read_capture(capture_dir, columns=["recv_ns", "segment", "kind", "market"])
    starts = t.group_by("segment").aggregate([("recv_ns", "min")])
    seg_start = dict(zip(starts.column("segment").to_pylist(), starts.column("recv_ns_min").to_pylist(), strict=True))
    data = t.filter(pc.and_(pc.is_in(t.column("kind"), pa.array(DATA_KINDS)), pc.is_valid(t.column("market"))))
    data = data.filter(pc.not_equal(data.column("market"), ""))
    pairs = data.group_by(["segment", "market"]).aggregate([])
    keys = sorted(zip(pairs.column("segment").to_pylist(), pairs.column("market").to_pylist(), strict=True))
    return StreamIndex(keys=keys, segment_start_ns=seg_start)


def load_stream(
    capture_dir: str | Path, segment: int, market: str, segment_start_ns: int | None = None
) -> SegmentStream:
    """One (segment, market) stream; malformed order or trade frames fail with `QT-INPUT-002` (`QT-INPUT-004` for an
    inexact decimal)."""
    filters = [("segment", "=", segment), ("market", "=", market), ("kind", "in", list(DATA_KINDS))]
    st = SegmentStream(segment=segment, market=market, segment_start_ns=segment_start_ns, capture_dir=Path(capture_dir))
    for batch in iter_capture(capture_dir, STREAM_COLUMNS, filters, segment=segment):
        names = ("seq", "recv_ns", "kind", "channel", "event", "microtimestamp")
        cols = {c: batch.column(c).to_pylist() for c in names}
        raws = batch.column("raw")
        for i in range(batch.num_rows):
            seq, recv, kind, event = cols["seq"][i], cols["recv_ns"][i], cols["kind"][i], cols["event"][i]
            prefix = feed.split_channel(cols["channel"][i] or "")[0]
            is_order = event in feed.ORDER_EVENTS and prefix == "live_orders"
            is_trade = event == "trade" and prefix == "live_trades"
            is_snap = kind == "rest_book" or (event == "data" and prefix == "detail_order_book")
            if not (is_order or is_trade or is_snap):
                continue
            st.first_recv_ns = recv if st.first_recv_ns is None else min(st.first_recv_ns, recv)
            st.last_recv_ns = recv if st.last_recv_ns is None else max(st.last_recv_ns, recv)
            if is_snap:
                us = cols["microtimestamp"][i]
                if us is None:
                    raise _fail(seq, "book snapshot without microtimestamp")
                st.snapshots.append(Snapshot(seq, recv, us, "rest" if kind == "rest_book" else "ws"))
                continue
            _event(st, raws[i].as_py(), seq, recv, is_order)
    return st


def _event(st: SegmentStream, raw: str, seq: int, recv: int, is_order: bool) -> None:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise _fail(seq, f"message is not JSON ({e.msg})") from e
    d = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(d, dict):
        raise _fail(seq, "message without data")
    us = _int(d, "microtimestamp", seq)
    ps, as_ = _num(d, "price_str", seq), _num(d, "amount_str", seq)
    px = to_fixed(ps, PRICE_EXP, f"seq={seq} price_str")
    amt = to_fixed(as_, SIZE_EXP, f"seq={seq} amount_str")
    if is_order:
        side = side_of(d.get("order_type"))
        if side is None:
            raise _fail(seq, "order_type must be 0 (buy) or 1 (sell)")
        at = d.get("amount_traded")
        traded = None if at is None or isinstance(at, bool) else to_fixed(at, SIZE_EXP, f"seq={seq} amount_traded")
        st.orders.append(OrderEvent(seq, recv, us, doc["event"], _int(d, "id", seq), side, px, amt, traded))
        return
    agg = side_of(d.get("type"))
    if agg is None:
        raise _fail(seq, "trade type must be 0 (buy) or 1 (sell)")
    st.trades.append(
        TradeEvent(
            seq,
            recv,
            us,
            _int(d, "id", seq),
            _int(d, "buy_order_id", seq),
            _int(d, "sell_order_id", seq),
            agg,
            px,
            amt,
            ps,
            as_,
        )
    )


def read_creates(capture_dir: str | Path, market: str, lo_ns: int, hi_ns: int) -> list[OrderEvent]:
    """`order_created` events of `market` received in `[lo_ns, hi_ns]` (any segment; repeated frames excluded)."""
    filters = [
        ("market", "=", market),
        ("kind", "=", "ws"),
        ("event", "=", "order_created"),
        ("recv_ns", ">=", lo_ns),
        ("recv_ns", "<=", hi_ns),
    ]
    st = SegmentStream(segment=-1, market=market)
    for b in iter_capture(capture_dir, ["recv_ns", "market", "kind", "event", "channel", "raw"], filters):
        names = ("seq", "recv_ns", "channel")
        c = {k: b.column(k).to_pylist() for k in names}
        raws = b.column("raw")
        for i in range(b.num_rows):
            if feed.split_channel(c["channel"][i] or "")[0] == "live_orders":
                _event(st, raws[i].as_py(), c["seq"][i], c["recv_ns"][i], True)
    return st.orders


def streams_with_ids(capture_dir: str | Path, ids: set[int]) -> set[tuple[int, str]]:
    """The (segment, market) streams in which any of `ids` appears as an order id or a trade's buy or sell id."""
    if not ids:
        return set()
    v = sorted(ids)
    filters = [[("kind", "=", "ws"), (c, "in", v)] for c in ("order_id", "buy_order_id", "sell_order_id")]
    out: set[tuple[int, str]] = set()
    for b in iter_capture(
        capture_dir, ["segment", "market", "kind", "order_id", "buy_order_id", "sell_order_id"], filters
    ):
        out |= set(zip(b.column("segment").to_pylist(), b.column("market").to_pylist(), strict=True))
    return {k for k in out if k[1]}


def created_ids(capture_dir: str | Path, ids: set[int]) -> set[int]:
    """The ids among `ids` that have an `order_created` in the capture."""
    if not ids:
        return set()
    filters = [("kind", "=", "ws"), ("event", "=", "order_created"), ("order_id", "in", sorted(ids))]
    out: set[int] = set()
    for b in iter_capture(capture_dir, ["kind", "event", "order_id"], filters):
        out |= set(b.column("order_id").to_pylist())
    return out


def iter_streams(capture_dir: str | Path, index: StreamIndex | None = None) -> Iterator[SegmentStream]:
    """Every (segment, market) stream of a capture, loaded one at a time in (segment, market) order."""
    ix = index or stream_index(capture_dir)
    for seg, market in ix.keys:
        yield load_stream(capture_dir, seg, market, ix.segment_start_ns.get(seg))


def segment_streams(capture_dir: str | Path) -> dict[tuple[int, str], SegmentStream]:
    """All streams at once (small captures and tests; the commands use `iter_streams`)."""
    return {(st.segment, st.market): st for st in iter_streams(capture_dir)}
