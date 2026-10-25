"""Venue book messages and trade prints -> batches on the canonical YES ladder (one instrument at a time).

Input per instrument, in capture order: `BookMsg`s (`snapshot` = full replacement, `delta` = signed size changes,
`set` = absolute sizes, `gap` = a sequence gap or a new subscription session upstream, `check` = a book the venue
returned out of band, compared and never applied) and `TradeMsg`s. Output: `Batch`es in time order, each one exchange
action: `resync` (the book is replaced: emitted as `clear` + `snapshot` rows), `depth` (level changes `prev -> new`,
with the trades matched to it first) or `trade` (prints matched to no book change).

- Batching: consecutive book messages with the same non-null `group` form one batch (Kalshi: one exchange `clock`,
  "one exchange action can make several deltas"); a message without a group is a batch of its own.
- Snapshots: the first snapshot, and the first after a gap, resync the book. A later snapshot is compared with the
  book built so far and becomes a `depth` batch of the levels that differ; when the venue sends snapshots only as
  restatements (`restate_expected`, Kalshi), every differing level is counted in `snapshot_mismatch_levels`.
- Gaps: a `gap` marker (Kalshi `seq` gap, sequence restart or resubscription; `reason` says which) opens a feed gap;
  deltas are dropped (`deltas_dropped_in_gap`) until the next snapshot closes it. Deltas before the first snapshot
  are skipped (`pre_snapshot_rows_skipped`). A delta that would make a level negative is clamped to 0
  (`negative_level`).
- Checks: a book fetched out of band (Kalshi REST orderbook while the WebSocket stream is read) comes as a `check`
  pair, `group = ("open", n)` at the request's send time and `("close", n)` at its receive time, with the levels on
  the close. It passes when the book equals it at the open or after any batch up to the close (the venue took it at
  some moment of the round trip); otherwise it is counted in `rest_book_mismatches` with its differing levels in
  `rest_book_mismatch_levels`. A check that is open while the book is not synced is `rest_book_unchecked`.
- Trades: each print is attached to the batch, within `match_window_ns` of its time, that decreased the hit level
  (bids for a sell aggressor, asks for a buy) by at least the print's size not yet explained by earlier prints
  (exact explanations first, then the nearest in time, then the earliest); otherwise to the batch that decreased
  the level at all (counted in `trades_partially_explained`); otherwise it becomes a `trade` batch of its own
  (`trades_unmatched`; those that a decrease of the other side at that price within the window would explain are
  also counted in `trades_other_side_explained`: many of them mean the venue's side convention is the opposite of
  the reader's). A decrease the venue attributed to the user (`own_tags`: Kalshi `client_order_id`) is the user's
  cancel, or the user's own taker order consuming the level: it explains the user's own taker prints (`TradeMsg.own`)
  first and other prints only when no other change can, so a cancel and a sweep of the same size in the same
  millisecond do not swap their prints. Block trades (`block`) never touch the book and are only counted.
- After every batch the book is checked for a crossed state (`crossed_books_after_last`) and, where the venue
  states the best bid / ask after a message (Polymarket `price_change`), compared with it (`bba_mismatch`).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import accumulate
from typing import Any, Literal

from queuetruth.pm.book import ASK, BID, NONE, Ladder, opposite

BookKind = Literal["snapshot", "delta", "set", "gap", "check"]


@dataclass
class BookMsg:
    """One venue book message for one instrument (canonical YES ladder, fixed-point units)."""

    kind: BookKind
    ts_event: int
    ts_recv: int
    levels: list[tuple[int, int, int]] = field(default_factory=list)  # (side, price, qty or signed delta)
    group: Any = None
    own_tag: str | None = None
    location: str = ""
    bba: tuple[int | None, int | None] | None = None
    restate_expected: bool = False
    #: `gap`: why the feed has a hole (`sequence gap`, `sequence regression`, `resubscription`).
    reason: str = ""


@dataclass
class TradeMsg:
    """One print on the canonical ladder: `aggressor` B (buy YES) or A (sell YES), `price` YES price."""

    ts_event: int
    ts_recv: int
    price: int
    size: int
    aggressor: int
    trade_id: str | None = None
    block: bool = False
    tx_hash: str | None = None
    fee_rate_bps: str | None = None
    location: str = ""
    #: The user's own taker print (Kalshi: its trade id is on one of the user's taker fills).
    own: bool = False

    @property
    def hit_side(self) -> int:
        """The resting side the print consumed."""
        return ASK if self.aggressor == BID else BID if self.aggressor == ASK else NONE


@dataclass
class Batch:
    """One exchange action on the canonical ladder."""

    kind: Literal["resync", "depth", "trade"]
    ts_event: int
    ts_recv: int
    seq: int
    changes: list[tuple[int, int, int, int]] = field(default_factory=list)  # (side, price, prev, new)
    levels: list[tuple[int, int, int]] = field(default_factory=list)  # resync: (side, price, qty) after
    trades: list[TradeMsg] = field(default_factory=list)
    own_tags: list[tuple[int, int, int, str]] = field(default_factory=list)  # (side, price, delta, tag)
    n_msgs: int = 0

    def decrease(self, side: int, price: int) -> int:
        """Size removed from a level by this batch (0 if none)."""
        if self.kind == "resync":
            return 0
        return sum(max(p - n, 0) for s, px, p, n in self.changes if s == side and px == price)


@dataclass
class BuildStats:
    """Counters of one instrument's build (anomalies feed `qt.data_quality`, the rest the ingest summary)."""

    messages: int = 0
    snapshots: int = 0
    resyncs: int = 0
    pre_snapshot_rows_skipped: int = 0
    deltas_dropped_in_gap: int = 0
    negative_level: int = 0
    snapshot_mismatch_levels: int = 0
    crossed_books_after_last: int = 0
    out_of_order_ts_event: int = 0
    bba_mismatch: int = 0
    trades: int = 0
    trades_matched: int = 0
    trades_partially_explained: int = 0
    trades_unmatched: int = 0
    trades_other_side_explained: int = 0
    block_trades: int = 0
    rest_book_checks: int = 0
    rest_book_mismatches: int = 0
    rest_book_mismatch_levels: int = 0
    rest_book_unchecked: int = 0
    feed_gaps: list[tuple[int, int, str]] = field(default_factory=list)

    def anomalies(self) -> dict[str, int]:
        """Non-zero anomaly counters for `qt.data_quality.source_specific`."""
        out = {
            "deltas_dropped_in_gap": self.deltas_dropped_in_gap,
            "negative_level": self.negative_level,
            "snapshot_mismatch_levels": self.snapshot_mismatch_levels,
            "bba_mismatch": self.bba_mismatch,
            "trades_unmatched": self.trades_unmatched,
            "trades_other_side_explained": self.trades_other_side_explained,
            "rest_book_mismatches": self.rest_book_mismatches,
            "rest_book_mismatch_levels": self.rest_book_mismatch_levels,
        }
        return {k: v for k, v in out.items() if v}


def _sorted_changes(changes: dict[tuple[int, int], tuple[int, int]]) -> list[tuple[int, int, int, int]]:
    """Level changes bids (best first) then asks (best first), dropping no-ops."""
    bids = sorted(((p, v) for (s, p), v in changes.items() if s == BID and v[0] != v[1]), key=lambda x: -x[0])
    asks = sorted(((p, v) for (s, p), v in changes.items() if s == ASK and v[0] != v[1]), key=lambda x: x[0])
    return [(BID, p, a, b) for p, (a, b) in bids] + [(ASK, p, a, b) for p, (a, b) in asks]


def _book_levels(book: Ladder) -> list[tuple[int, int, int]]:
    """Every level of the book: bids best first, then asks best first."""
    return [(BID, p, q) for p, q in book.levels(BID)] + [(ASK, p, q) for p, q in book.levels(ASK)]


def _target(levels: list[tuple[int, int, int]]) -> dict[tuple[int, int], int]:
    """`{(side, price): size}` of a full book message (repeated levels add up, empty ones are dropped)."""
    out: dict[tuple[int, int], int] = {}
    for s, p, q in levels:
        if q > 0:
            out[(s, p)] = out.get((s, p), 0) + q
    return out


def _differing(book: Ladder, target: dict[tuple[int, int], int]) -> int:
    """Number of levels where `book` and `target` disagree (0 when they are equal)."""
    n = sum(1 for (s, p), q in target.items() if book.qty(s, p) != q)
    return n + sum(1 for s in (BID, ASK) for p, _q in book.levels(s) if (s, p) not in target)


def build_batches(
    msgs: Sequence[BookMsg], trades: Sequence[TradeMsg], *, match_window_ns: int
) -> tuple[list[Batch], BuildStats]:
    """Batches of one instrument (see the module docstring)."""
    st = BuildStats()
    book = Ladder()
    synced = False
    gap_open: int | None = None
    gap_why = ""
    last_ts: int | None = None
    out: list[Batch] = []
    # open out-of-band checks: id -> [levels, matched so far]
    checks: dict[Any, list[Any]] = {}

    def recheck() -> None:
        for c in checks.values():
            if not c[1] and synced and _differing(book, c[0]) == 0:
                c[1] = True

    i = 0
    n = len(msgs)
    while i < n:
        m = msgs[i]
        j = i + 1
        if m.kind in ("delta", "set") and m.group is not None:
            while j < n and msgs[j].group == m.group and msgs[j].kind == m.kind:
                j += 1
        group = msgs[i:j]
        i = j
        if m.kind == "check":
            tag, cid = m.group
            if tag == "open":
                want = _target(m.levels)
                checks[cid] = [want, synced and _differing(book, want) == 0]
                continue
            c = checks.pop(cid, None)
            if c is None:
                continue
            if not c[1] and not synced:
                st.rest_book_unchecked += 1
                continue
            st.rest_book_checks += 1
            off = 0 if c[1] else _differing(book, c[0])
            if off:
                st.rest_book_mismatches += 1
                st.rest_book_mismatch_levels += off
            continue
        st.messages += len(group)
        if m.kind == "gap":
            if synced:
                gap_open = m.ts_event if last_ts is None else last_ts
                gap_why = m.reason or "sequence gap"
            synced = False
            continue
        if m.kind == "snapshot":
            st.snapshots += 1
            target = _target(m.levels)
            if not synced:
                book.clear()
                for (s, p), q in target.items():
                    book.set(s, p, q)
                synced = True
                st.resyncs += 1
                if gap_open is not None:
                    st.feed_gaps.append((gap_open, m.ts_event, f"{gap_why} until the next snapshot"))
                    gap_open = None
                b = Batch("resync", m.ts_event, m.ts_recv, 0, levels=_book_levels(book), n_msgs=1)
            else:
                changes: dict[tuple[int, int], tuple[int, int]] = {}
                for s in (BID, ASK):
                    for p, q in list(book.levels(s)):
                        if target.get((s, p), 0) != q:
                            changes[(s, p)] = (q, target.get((s, p), 0))
                for (s, p), q in target.items():
                    if book.qty(s, p) == 0:
                        changes[(s, p)] = (0, q)
                if m.restate_expected:
                    st.snapshot_mismatch_levels += len(changes)
                for (s, p), (_a, q) in changes.items():
                    book.set(s, p, q)
                b = Batch("depth", m.ts_event, m.ts_recv, 0, changes=_sorted_changes(changes), n_msgs=1)
            _finish(b, book, st, out, m, last_ts)
            last_ts = max(last_ts, b.ts_event) if last_ts is not None else b.ts_event
            if checks:
                recheck()
            continue
        if not synced:
            if gap_open is not None:
                st.deltas_dropped_in_gap += len(group)
            else:
                st.pre_snapshot_rows_skipped += len(group)
            continue
        changes = {}
        tags: list[tuple[int, int, int, str]] = []
        for g in group:
            for s, p, q in g.levels:
                prev = book.qty(s, p)
                if g.kind == "delta":
                    new = prev + q
                    if new < 0:
                        st.negative_level += 1
                        new = 0
                    if g.own_tag is not None:
                        tags.append((s, p, q, g.own_tag))
                else:
                    new = q
                first = changes.get((s, p), (prev, prev))[0]
                changes[(s, p)] = (first, new)
                book.set(s, p, new)
        b = Batch(
            "depth",
            max(g.ts_event for g in group),
            max(g.ts_recv for g in group),
            0,
            changes=_sorted_changes(changes),
            own_tags=tags,
            n_msgs=len(group),
        )
        _finish(b, book, st, out, group[-1], last_ts)
        last_ts = max(last_ts, b.ts_event) if last_ts is not None else b.ts_event
        if checks:
            recheck()
    st.rest_book_unchecked += len(checks)  # opened and never closed
    if gap_open is not None:
        end = last_ts if last_ts is not None else gap_open
        st.feed_gaps.append((gap_open, max(end, gap_open), f"{gap_why} without a later snapshot"))
    out = _attach_trades(out, trades, st, match_window_ns)
    for k, b in enumerate(out, start=1):
        b.seq = k
    return out, st


def _finish(b: Batch, book: Ladder, st: BuildStats, out: list[Batch], last: BookMsg, last_ts: int | None) -> None:
    if last_ts is not None and b.ts_event < last_ts:
        st.out_of_order_ts_event += 1
    if book.crossed():
        st.crossed_books_after_last += 1
    if last.bba is not None:
        if (book.best(BID), book.best(ASK)) != last.bba:
            st.bba_mismatch += 1
    out.append(b)


def _attach_trades(batches: list[Batch], trades: Sequence[TradeMsg], st: BuildStats, window: int) -> list[Batch]:
    st.trades = len(trades)
    live = [t for t in trades if not t.block]
    st.block_trades = len(trades) - len(live)
    # candidate batches per hit level (sorted by time) with their unexplained decrease, split into the part the venue
    # attributed to the user (`own`: a cancel, or the user's own taker order) and the rest (`cap`)
    cap: dict[tuple[int, int, int], int] = {}
    own: dict[tuple[int, int, int], int] = {}
    by_level: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for k, b in enumerate(batches):
        if b.kind != "depth":
            continue
        tagged: dict[tuple[int, int], int] = {}
        for s, p, d, _tag in b.own_tags:
            if d < 0:
                tagged[(s, p)] = tagged.get((s, p), 0) - d
        for s, p, prev, new in b.changes:
            if new < prev:
                mine = min(tagged.get((s, p), 0), prev - new)
                own[(k, s, p)] = mine
                cap[(k, s, p)] = prev - new - mine
                by_level.setdefault((s, p), []).append((b.ts_event, k))
    times: dict[tuple[int, int], list[int]] = {}
    for key, lst in by_level.items():
        lst.sort()
        times[key] = [x[0] for x in lst]
    extra: list[TradeMsg] = []
    for t in sorted(live, key=lambda x: (x.ts_event, x.ts_recv)):
        key = (t.hit_side, t.price)
        lst = by_level.get(key)
        best = None
        if lst:
            ts = times[key]
            lo, hi = bisect_left(ts, t.ts_event - window), bisect_right(ts, t.ts_event + window)
            near = [k for _, k in lst[lo:hi]]
            for pot in (own, cap) if t.own else (cap, own):
                cands = [k for k in near if pot[(k, *key)] > 0]
                full = [k for k in cands if pot[(k, *key)] >= t.size]
                pool = full or cands
                if not pool:
                    continue
                best = min(
                    pool,
                    key=lambda k, pot=pot: (
                        pot[(k, *key)] != t.size,
                        abs(batches[k].ts_event - t.ts_event),
                        batches[k].ts_event,
                        k,
                    ),
                )
                if not full:
                    st.trades_partially_explained += 1
                pot[(best, *key)] = max(pot[(best, *key)] - t.size, 0)
                break
        if best is None:
            st.trades_unmatched += 1
            other = (opposite(t.hit_side), t.price)
            if other in times:
                ts = times[other]
                lo, hi = bisect_left(ts, t.ts_event - window), bisect_right(ts, t.ts_event + window)
                if any(cap[(k, *other)] + own[(k, *other)] > 0 for _, k in by_level[other][lo:hi]):
                    st.trades_other_side_explained += 1
            extra.append(t)
            continue
        batches[best].trades.append(t)
        st.trades_matched += 1
    for b in batches:
        b.trades.sort(key=lambda x: (x.ts_event, x.ts_recv))
    if not extra:
        return batches
    run = list(accumulate((b.ts_event for b in batches), max))
    ins = sorted(((bisect_right(run, t.ts_event), t) for t in extra), key=lambda x: (x[0], x[1].ts_event, x[1].ts_recv))
    merged: list[Batch] = []
    j = 0
    for k, b in enumerate(batches):
        while j < len(ins) and ins[j][0] <= k:
            t = ins[j][1]
            merged.append(Batch("trade", t.ts_event, t.ts_recv, 0, trades=[t], n_msgs=1))
            j += 1
        merged.append(b)
    for _, t in ins[j:]:
        merged.append(Batch("trade", t.ts_event, t.ts_recv, 0, trades=[t], n_msgs=1))
    return merged


__all__ = ["Batch", "BookMsg", "BuildStats", "TradeMsg", "build_batches"]
