"""Records, event grouping, the tape book and tape readers.

The tape book is the book exactly as the feed describes it (no shadow, no counterfactual). It keeps each price
level in time-priority order (arrival order, or receipt order by order reference on Nasdaq ITCH data: `prio`),
applies the venue's modify-priority facts and pairs execution removals with their `F` records. The oracle uses it
as the source of every fork (the book at shadow activation) and as the reference for "what the market really
showed" (crossed books, displayed interest ahead of a hidden print).
"""

from __future__ import annotations

import bisect
import csv
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .venue import VenueFacts, modify_keeps_priority, venue_key

UNDEF_PRICE = (1 << 63) - 1
FLAG_LAST = 1 << 7
FLAG_SNAPSHOT = 1 << 5
FLAG_MAYBE_BAD_BOOK = 1 << 2


class Rec:
    """One MBO record (qt.mbo fields the oracle needs). `action` and `side` are one-character strings."""

    __slots__ = ("action", "flags", "instrument_id", "order_id", "price", "sequence", "side", "size", "ts_event")

    def __init__(
        self,
        ts_event: int,
        instrument_id: int,
        action: str,
        side: str,
        price: int,
        size: int,
        order_id: int,
        flags: int,
        sequence: int,
    ) -> None:
        self.ts_event = ts_event
        self.instrument_id = instrument_id
        self.action = action
        self.side = side
        self.price = price
        self.size = size
        self.order_id = order_id
        self.flags = flags
        self.sequence = sequence

    def __repr__(self) -> str:
        return (
            f"Rec({self.ts_event} {self.action} {self.side} px={self.price} sz={self.size} id={self.order_id} "
            f"fl={self.flags} seq={self.sequence})"
        )


def _ch(v) -> str:
    if isinstance(v, str):
        return v
    return chr(int(v))


def rec_from_mapping(m: dict) -> Rec:
    """Builds a record from a qt.mbo row or an et CSV row (dict of str or native values)."""
    ts = m["ts_event"]
    if hasattr(ts, "value"):
        ts = ts.value
    price = m.get("price")
    price = UNDEF_PRICE if price is None or price == "" else int(price)
    return Rec(
        int(ts),
        int(m["instrument_id"]),
        _ch(m["action"]),
        _ch(m["side"]),
        price,
        int(m["size"]),
        int(m["order_id"] or 0),
        int(m["flags"] or 0),
        int(m["sequence"] or 0),
    )


def group_events(records: Iterable[Rec]) -> Iterator[list[Rec]]:
    """Maximal runs of consecutive records of one partition ending with a LAST record (SPEC 5.1.2). A trailing
    unterminated run is yielded as a final event."""
    buf: list[Rec] = []
    for r in records:
        buf.append(r)
        if r.flags & FLAG_LAST:
            yield buf
            buf = []
    if buf:
        yield buf


def insert_by_prio(level: dict, o) -> None:
    """Inserts `o` (with a `prio` key) into a level dict kept in priority order: appended when it ranks at or after
    the tail (the usual case), else the level is rebuilt in key order (stable for equal keys)."""
    if level and next(reversed(level.values())).prio > o.prio:
        items = sorted([*level.values(), o], key=lambda x: x.prio)
        level.clear()
        level.update((x.oid, x) for x in items)
    else:
        level[o.oid] = o


def opposite(side: str) -> str:
    return "A" if side == "B" else "B"


def at_or_better(side: str, a: int, b: int) -> bool:
    """`a` is at or better than `b` for a resting order on `side` (bid: higher, ask: lower)."""
    return a >= b if side == "B" else a <= b


def strictly_better(side: str, a: int, b: int) -> bool:
    return a > b if side == "B" else a < b


class TapeOrder:
    __slots__ = ("oid", "price", "prio", "side", "size")

    def __init__(self, oid: int, side: str, price: int, size: int, prio: int = 0) -> None:
        self.oid = oid
        self.side = side
        self.price = price
        self.size = size
        self.prio = prio  # priority key within its level: smaller ranks first (`TapeBook.key`)


class TapeBook:
    """The feed's book: per side, price -> {order_id: TapeOrder} in time priority, plus sorted price lists.

    Time priority is arrival order, or with `ref_priority` (Nasdaq ITCH data, `InstrumentMeta.order_ref_priority`)
    receipt order: an order entering the book keys at `2 * reference`, so a reference older than orders resting at
    its level (an order held until the open, released with its original reference) ranks ahead of them; an order
    re-entering the book (a duplicate add, a modify that loses priority) is received now and keys at `2 * max_ref + 1`,
    after every reference added so far."""

    def __init__(self, facts: VenueFacts, ref_priority: bool = False) -> None:
        self.facts = facts
        self.ref_priority = ref_priority
        self.max_ref = 0  # largest order reference added so far
        self.arrivals = 0
        self.orders: dict[int, TapeOrder] = {}
        self.levels: dict[str, dict[int, dict[int, TapeOrder]]] = {"B": {}, "A": {}}
        self.totals: dict[str, dict[int, int]] = {"B": {}, "A": {}}
        self.prices: dict[str, list[int]] = {"B": [], "A": []}  # ascending

    # ----- queries
    def best(self, side: str) -> int | None:
        p = self.prices[side]
        if not p:
            return None
        return p[-1] if side == "B" else p[0]

    def level_total(self, side: str, price: int) -> int:
        return self.totals[side].get(price, 0)

    def displayed_at_or_better(self, side: str, price: int) -> int:
        """Displayed quantity on `side` at prices at or better than `price`."""
        tot = self.totals[side]
        p = self.prices[side]
        if side == "B":
            i = bisect.bisect_left(p, price)
            return sum(tot[x] for x in p[i:])
        i = bisect.bisect_right(p, price)
        return sum(tot[x] for x in p[:i])

    def crosses(self, side: str, price: int) -> bool:
        """An order on `side` at `price` would lock or cross the opposite displayed best."""
        ob = self.best(opposite(side))
        return ob is not None and at_or_better(side, price, ob)

    def is_crossed(self) -> bool:
        b, a = self.best("B"), self.best("A")
        return b is not None and a is not None and b >= a

    # ----- mutations
    def key(self, oid: int, reentry: bool) -> int:
        """Priority key of an order entering the book now (`reentry`: it was on the book before)."""
        self.arrivals += 1
        if not self.ref_priority:
            return self.arrivals
        return 2 * max(self.max_ref, oid) + 1 if reentry else 2 * oid

    def _insert(self, o: TapeOrder) -> None:
        lv = self.levels[o.side].get(o.price)
        if lv is None:
            lv = self.levels[o.side][o.price] = {}
            self.totals[o.side][o.price] = 0
            bisect.insort(self.prices[o.side], o.price)
        insert_by_prio(lv, o)
        self.totals[o.side][o.price] += o.size
        self.orders[o.oid] = o
        self.max_ref = max(self.max_ref, o.oid)

    def _remove(self, o: TapeOrder) -> None:
        lv = self.levels[o.side][o.price]
        del lv[o.oid]
        self.totals[o.side][o.price] -= o.size
        if not lv:
            del self.levels[o.side][o.price]
            del self.totals[o.side][o.price]
            p = self.prices[o.side]
            del p[bisect.bisect_left(p, o.price)]
        del self.orders[o.oid]

    def _reduce(self, o: TapeOrder, by: int) -> None:
        by = min(by, o.size)
        o.size -= by
        self.totals[o.side][o.price] -= by
        if o.size == 0:
            self._remove(o)

    def add(self, oid: int, side: str, price: int, size: int) -> None:
        old = self.orders.get(oid)
        if old is not None:
            self._remove(old)
        self._insert(TapeOrder(oid, side, price, size, self.key(oid, old is not None)))

    def cancel(self, oid: int, size: int) -> None:
        o = self.orders.get(oid)
        if o is None:
            return
        if size >= o.size:
            self._remove(o)
        else:
            self._reduce(o, size)

    def modify(self, oid: int, side: str, price: int, size: int, keeps: bool) -> None:
        o = self.orders.get(oid)
        if o is None:
            if size > 0:
                self._insert(TapeOrder(oid, side, price, size, self.key(oid, False)))
            return
        if size == 0:
            self._remove(o)
            return
        if keeps:
            self._reduce(o, o.size - size)
        else:
            self._remove(o)
            self._insert(TapeOrder(oid, o.side, price, size, self.key(oid, True)))

    def clear(self) -> None:
        self.orders.clear()
        for s in ("B", "A"):
            self.levels[s].clear()
            self.totals[s].clear()
            self.prices[s].clear()

    def keeps_priority(self, oid: int, new_px: int, new_size: int, filled_pending: int) -> bool:
        o = self.orders.get(oid)
        if o is None:
            return False
        return modify_keeps_priority(self.facts, o.price, o.size, filled_pending, new_px, new_size)


# ---------------------------------------------------------------- readers


@dataclass
class InstrumentMeta:
    instrument_id: int
    session_date: str  # YYYY-MM-DD
    venue: str
    tick_size: int
    midnight_utc_ns: int
    open_s: int
    close_s: int
    source: str = ""  # ingest format of the instrument row (`itch`, `etcsv`, `dbn`, `synthetic`, ...)

    @property
    def order_ref_priority(self) -> bool:
        """Time priority follows the order id (an ITCH order reference number, assigned in receipt order across
        the feed) rather than the arrival order of `A` records: Nasdaq data from TotalView-ITCH. Nasdaq releases
        orders it held until the open by `A` records carrying their original, smaller references, and executes them
        ahead of orders displayed earlier but received later. Other sources assign ids freely."""
        return venue_key(self.venue) == "nasdaq" and self.source in ("itch", "etcsv", "dbn")


@dataclass
class Partition:
    meta: InstrumentMeta
    paths: list[Path]  # parquet files in order (qt dataset) or empty for in-memory
    records: list[Rec] | None = None  # in-memory partitions
    cross_sequences: frozenset[int] = frozenset()  # sequences of cross prints from trades_source (kind = cross)

    def iter_records(self, batch_rows: int = 65_536) -> Iterator[Rec]:
        if self.records is not None:
            yield from self.records
            return
        import pyarrow.parquet as pq

        cols = ["ts_event", "instrument_id", "action", "side", "price", "size", "order_id", "flags", "sequence"]
        for path in self.paths:
            pf = pq.ParquetFile(path)
            for batch in pf.iter_batches(batch_size=batch_rows, columns=cols):
                d = batch.to_pydict()
                ts = batch.column(0).cast("int64").to_pylist()
                for i in range(batch.num_rows):
                    price = d["price"][i]
                    yield Rec(
                        ts[i],
                        d["instrument_id"][i],
                        chr(d["action"][i]),
                        chr(d["side"][i]),
                        UNDEF_PRICE if price is None else price,
                        d["size"][i],
                        d["order_id"][i],
                        d["flags"][i],
                        d["sequence"][i],
                    )


def _date_str(v) -> str:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    import datetime as _dt

    return (_dt.date(1970, 1, 1) + _dt.timedelta(days=int(v))).isoformat()


def read_qt_dataset(root: str | Path) -> list[Partition]:
    """Partitions of a qt dataset directory (SPEC 3.3), in manifest order."""
    import pyarrow.parquet as pq

    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("kind", "mbo") != "mbo":
        raise ValueError("the oracle needs an MBO dataset")
    insts = pq.read_table(root / "instruments.parquet").to_pylist()
    meta = {}
    for r in insts:
        meta[(int(r["instrument_id"]), _date_str(r["session_date"]))] = InstrumentMeta(
            int(r["instrument_id"]),
            _date_str(r["session_date"]),
            str(r["venue"]),
            int(r["tick_size"]),
            int(r["midnight_utc_ns"]),
            int(r["open_s"]),
            int(r["close_s"]),
            str(r.get("source") or ""),
        )
    out = []
    for p in manifest["partitions"]:
        key = (int(p["instrument_id"]), _date_str(p["session_date"]))
        m = meta.get(key) or InstrumentMeta(key[0], key[1], manifest.get("venue", "generic"), 1, 0, 0, 86_400)
        crosses: frozenset[int] = frozenset()
        tdir = root / "trades_source" / f"date={key[1]}" / f"instrument_id={key[0]}"
        if tdir.is_dir():
            seqs = set()
            for f in sorted(tdir.glob("*.parquet")):
                t = pq.read_table(f, columns=["sequence", "kind"]).to_pydict()
                seqs.update(s for s, k in zip(t["sequence"], t["kind"], strict=True) if k == "cross")
            crosses = frozenset(seqs)
        out.append(Partition(m, [root / f["path"] for f in p["files"]], cross_sequences=crosses))
    return out


def read_et_csv(
    mbo_csv: str | Path,
    *,
    venue: str,
    midnight_utc_ns: int,
    open_s: int,
    close_s: int,
    tick_size: int,
    session_date: str = "unknown",
    instrument_ids: set[int] | None = None,
) -> list[Partition]:
    """An et `mbo.csv` (header ts_event,ts_recv,instrument_id,action,side,price,size,order_id,flags,sequence,
    channel_id) split into one in-memory partition per instrument, in file order. Memory grows with the file;
    restrict `instrument_ids` for large days."""
    parts: dict[int, list[Rec]] = {}
    with open(mbo_csv, newline="") as f:
        rd = csv.reader(f)
        header = next(rd)
        ix = {n: i for i, n in enumerate(header)}
        i_ts, i_inst, i_act, i_side = ix["ts_event"], ix["instrument_id"], ix["action"], ix["side"]
        i_px, i_sz, i_id, i_fl, i_seq = ix["price"], ix["size"], ix["order_id"], ix["flags"], ix["sequence"]
        for row in rd:
            inst = int(row[i_inst])
            if instrument_ids is not None and inst not in instrument_ids:
                continue
            px = row[i_px]
            parts.setdefault(inst, []).append(
                Rec(
                    int(row[i_ts]),
                    inst,
                    row[i_act],
                    row[i_side],
                    UNDEF_PRICE if px == "" else int(px),
                    int(row[i_sz]),
                    int(row[i_id] or 0),
                    int(row[i_fl] or 0),
                    int(row[i_seq] or 0),
                )
            )
    return [
        Partition(
            InstrumentMeta(i, session_date, venue, tick_size, midnight_utc_ns, open_s, close_s, "etcsv"), [], recs
        )
        for i, recs in sorted(parts.items())
    ]
