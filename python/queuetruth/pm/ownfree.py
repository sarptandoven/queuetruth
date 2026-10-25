"""The book without the user's own resting orders, and where each own order joined the queue.

Own orders come from the canonical live log as `queuetruth.score.l2_own.OwnOrder` records (resting-quantity steps
in dataset units, maker fills). Each change of an order's resting quantity is placed inside one batch of the
published series:

1. exact: a Kalshi book change the venue attributed to the order (`client_order_id` on `orderbook_delta`, carried
   as `Batch.own_tags`) with the same level and size; the nearest such change in time is used;
2. aligned: the batch within `window_ns` that changed the level in the same direction by at least the order's
   change not yet explained by other own changes, else by less, nearest in time, earliest first (the rule of
   `queuetruth.score.l2_own`);
3. unaligned: before the first batch at or after the change's time that touches the level (counted).

A restated order (open when the capture started) rests from before the first batch. The own-free series replays
the published batches on a second ladder: every published level size loses the own resting size at that level
just after the batch (`prev` is the own-free ladder's value), negative results are clamped to 0 and counted, and a
batch that only moved own orders becomes a no-op (dropped, its trades kept). Trade prints are kept (the twin of a
live order takes its place in the queue and needs the volume that filled it).

`OwnEntry` records, per own order (leg), where it joined its level, by the published batch `seq`: `inside` = during
that batch (the order queues behind every other size at the level after the batch), else just before it; a
restated order has `seq = 0`. With an attributed placement this is the exact queue-entry point. A later size
increase at the same price (an amend up, which loses priority) is a re-entry at the back of the queue: it gets its
own row, keyed `<order key>#up<k>`, with the resting size after it. Each row also places where that stretch left
the queue (`end_seq`, `end_inside`): the change that took its resting size to 0 (a cancel, or the last fill) or the
next increase; None while it still rests at the end of the data. The replay stops a cancelled order there, so a
print in a later batch with the cancel's timestamp never fills it.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, replace

from queuetruth.pm.batches import Batch
from queuetruth.pm.book import BID, Ladder
from queuetruth.score.l2_own import OwnOrder


@dataclass
class OwnFreeStats:
    """Counters of one own-order removal (summed over instruments)."""

    orders: int = 0
    restated: int = 0
    changes: int = 0
    exact: int = 0
    aligned: int = 0
    unaligned: int = 0
    outside: int = 0
    clamps: int = 0
    noop_batches: int = 0
    own_tags: int = 0
    own_tags_unused: int = 0

    def add(self, o: OwnFreeStats) -> None:
        """Adds another instrument's counters."""
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + getattr(o, k))


@dataclass(frozen=True)
class OwnEntry:
    """Where one own order (leg) joined its level: the published batch `seq` and whether it joined inside that
    batch or just before it, how the placement was found, its exchange time and its resting size at entry."""

    key: str
    side: int
    price: int
    seq: int
    inside: bool
    ts_event: int
    how: str  # exact | aligned | unaligned | restated | outside
    qty: int
    end_seq: int | None = None
    end_inside: bool | None = None


@dataclass
class OwnFreeResult:
    """Own-free batches, own resting size per level after each published batch, and entries."""

    batches: list[Batch]
    entries: list[OwnEntry]
    stats: OwnFreeStats
    #: (side, price) -> sorted positions and cumulative own size: position 2k = before batch k, 2k+1 = inside it.
    own_levels: dict[tuple[int, int], tuple[list[int], list[int]]] = field(default_factory=dict)

    def own_after(self, side: int, price: int, k: int) -> int:
        """Own resting size at a level just after published batch `k`."""
        pos, cum = self.own_levels.get((side, price), ([], []))
        i = bisect_right(pos, 2 * k + 1)
        return cum[i - 1] if i else 0


def _level_index(batches: list[Batch]) -> dict[tuple[int, int], tuple[list[int], list[int]]]:
    """(side, price) -> (batch indices, ts_event) of batches that change that level (resyncs count as changes)."""
    idx: dict[tuple[int, int], tuple[list[int], list[int]]] = {}
    for k, b in enumerate(batches):
        keys = {(s, p) for s, p, _a, _b in b.changes} | {(s, p) for s, p, _q in b.levels}
        for key in keys:
            ks, ts = idx.setdefault(key, ([], []))
            ks.append(k)
            ts.append(b.ts_event)
    return idx


def _seq_of(pos: int, batches: list[Batch]) -> tuple[int, bool]:
    """(published batch seq, inside it) of a placement position (2k = before batch k, 2k + 1 = inside it)."""
    if pos < 0:
        return 0, False
    if pos >= 2 * len(batches):
        return (batches[-1].seq + 1 if batches else 1), False
    return batches[pos // 2].seq, bool(pos % 2)


def _delta_at(b: Batch, side: int, price: int) -> int:
    for s, p, a, n in b.changes:
        if s == side and p == price:
            return n - a
    return 0


def remove_own(batches: list[Batch], own: list[OwnOrder], cids: dict[str, set[str]], window_ns: int) -> OwnFreeResult:
    """Own-free batches of one instrument (see the module docstring). `cids` maps an order key to its client order
    ids (Kalshi attribution)."""
    st = OwnFreeStats(orders=len(own))
    lvl = _level_index(batches)
    first_ts = batches[0].ts_event if batches else 0
    # exact attributions: (side, price, delta, tag) -> list of batch indices
    tagged: dict[tuple[int, int, int, str], list[int]] = {}
    for k, b in enumerate(batches):
        for s, p, d, tag in b.own_tags:
            tagged.setdefault((s, p, d, tag), []).append(k)
            st.own_tags += 1
    used_tags: set[tuple[tuple[int, int, int, str], int]] = set()
    capacity: dict[tuple[int, int, int], int] = {}
    placed: dict[tuple[int, int], list[tuple[int, int]]] = {}
    entries: list[OwnEntry] = []
    for o in own:
        side, price = o.side, o.price
        key = (side, price)
        ks, ts = lvl.get(key, ([], []))
        changes = o.changes()
        floor = -1
        resting = 0
        n_up = 0
        for n_ch, (t, d, _dn) in enumerate(changes):
            resting += d
            st.changes += 1
            how = ""
            pos = None
            if o.restated and n_ch == 0 and t <= first_ts:
                pos, how = -1, "restated"
                st.restated += 1
            if pos is None:
                for cid in sorted(cids.get(o.key.split("#leg")[0], ())):
                    tkey = (side, price, d, cid)
                    cand = [k for k in tagged.get(tkey, []) if (tkey, k) not in used_tags and 2 * k + 1 >= floor]
                    if cand:
                        k = min(cand, key=lambda k: (abs(batches[k].ts_event - t), k))
                        used_tags.add((tkey, k))
                        pos, how = 2 * k + 1, "exact"
                        st.exact += 1
                        break
            if pos is None and ks:
                lo, hi = bisect_left(ts, t - window_ns), bisect_right(ts, t + window_ns)
                cands = []
                for k in ks[lo:hi]:
                    if 2 * k + 1 < floor:
                        continue
                    delta = _delta_at(batches[k], side, price)
                    if (d > 0 and delta > 0) or (d < 0 and delta < 0):
                        left = capacity.get((k, side, price), abs(delta))
                        if left > 0:
                            cands.append((k, left))
                if cands:
                    k, left = min(
                        cands, key=lambda x: (x[1] < abs(d), abs(batches[x[0]].ts_event - t), batches[x[0]].ts_event)
                    )
                    capacity[(k, side, price)] = max(left - abs(d), 0)
                    pos, how = 2 * k + 1, "aligned"
                    st.aligned += 1
            if pos is None:
                nxt = bisect_left(ts, t) if ks else 0
                while nxt < len(ks) and 2 * ks[nxt] < floor:
                    nxt += 1
                if nxt < len(ks):
                    pos, how = 2 * ks[nxt], "unaligned"
                    st.unaligned += 1
                elif t > (batches[-1].ts_event if batches else t):
                    pos, how = 2 * len(batches), "outside"
                    st.outside += 1
                else:
                    pos, how = 2 * len(batches), "unaligned"
                    st.unaligned += 1
            floor = max(floor, pos)
            placed.setdefault(key, []).append((pos, d))
            seq, inside = _seq_of(pos, batches)
            if n_ch > 0 and (d > 0 or resting <= 0):  # the stretch ends here: an increase, or nothing left
                entries[-1] = replace(entries[-1], end_seq=seq, end_inside=inside)
            if n_ch == 0:
                entries.append(OwnEntry(o.key, side, price, seq, inside, t, how, max(o.resting[0][1], 0)))
            elif d > 0:
                n_up += 1
                entries.append(OwnEntry(f"{o.key}#up{n_up}", side, price, seq, inside, t, how, max(resting, 0)))
    own_levels: dict[tuple[int, int], tuple[list[int], list[int]]] = {}
    for key, lst in placed.items():
        lst.sort()
        pos_list, cum, run = [], [], 0
        for pos, d in lst:
            run += d
            pos_list.append(pos)
            cum.append(run)
        own_levels[key] = (pos_list, cum)
    res = OwnFreeResult([], entries, st, own_levels)
    res.batches = _replay(batches, res, st)
    st.own_tags_unused = st.own_tags - len(used_tags)
    return res


def _replay(batches: list[Batch], res: OwnFreeResult, st: OwnFreeStats) -> list[Batch]:
    book = Ladder()
    out: list[Batch] = []
    for k, b in enumerate(batches):
        if b.kind == "resync":
            book.clear()
            levels = []
            for s, p, q in b.levels:
                v = q - res.own_after(s, p, k)
                if v < 0:
                    st.clamps += 1
                    v = 0
                if v > 0:
                    book.set(s, p, v)
                    levels.append((s, p, v))
            out.append(
                Batch("resync", b.ts_event, b.ts_recv, b.seq, levels=levels, trades=list(b.trades), n_msgs=b.n_msgs)
            )
            continue
        changes = []
        for s, p, _a, new in b.changes:
            v = new - res.own_after(s, p, k)
            if v < 0:
                st.clamps += 1
                v = 0
            prev = book.qty(s, p)
            if v != prev:
                book.set(s, p, v)
                changes.append((s, p, prev, v))
        changes.sort(key=lambda c: (c[0] != BID, -c[1] if c[0] == BID else c[1]))
        if b.kind == "depth" and not changes and not b.trades:
            st.noop_batches += 1
            continue
        kind = b.kind if changes or b.kind != "depth" else "trade"
        out.append(Batch(kind, b.ts_event, b.ts_recv, b.seq, changes=changes, trades=list(b.trades), n_msgs=b.n_msgs))
    return out


__all__ = ["OwnEntry", "OwnFreeResult", "OwnFreeStats", "remove_own"]
