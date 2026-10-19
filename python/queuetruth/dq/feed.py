"""Record-level checks over the MBO partitions (no book needed): sequence numbering, timestamps and feed latency,
sizes and the price grid, session coverage, snapshot and clear handling, orphan references, per-day rate drift."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

import numpy as np

from queuetruth.dq.data import (
    DAY_NS,
    F_BAD_TS_RECV,
    F_LAST,
    F_MAYBE_BAD_BOOK,
    F_SNAPSHOT,
    NS,
    UNDEF_PRICE,
    A,
    C,
    F,
    M,
    Part,
    R,
    T,
    date_str,
)
from queuetruth.dq.framework import FAIL_RATIO, Check, chr_or, level, percentiles, ratio
from queuetruth.dq.model import DqCheck

MASK64 = np.uint64(0xFFFFFFFFFFFFFFFF)


def _not_snapshot(p: Part) -> np.ndarray:
    return (p["flags"] & F_SNAPSHOT) == 0


def _fmt_ns(ns: float | int | None) -> str:
    if ns is None:
        return "n/a"
    a = abs(ns)
    for unit, size in (("s", NS), ("ms", 1_000_000), ("us", 1_000)):
        if a >= size:
            return f"{ns / size:.3g} {unit}"
    return f"{int(ns)} ns"


# ---- sequence ---------------------------------------------------------------------------------------------------

#: A partition whose sequence span is covered below this share by the dataset's records is a filtered feed (the
#: numbering spans instruments the dataset does not hold): its holes are not gaps.
DENSE_SHARE = 0.5
#: Partitions of one channel sharing more than this share of sequence numbers number their records per instrument.
PER_INSTRUMENT_SHARE = 0.5
#: ... when each of them holds at least this many numbers (a few shared numbers are collisions).
PER_INSTRUMENT_MIN = 100


def _row_hash(cols: list[np.ndarray]) -> np.ndarray:
    h = np.zeros(cols[0].shape[0], dtype=np.uint64)
    with np.errstate(over="ignore"):
        for c in cols:
            h ^= c.astype(np.int64, copy=False).view(np.uint64)
            h *= np.uint64(0x9E3779B97F4A7C15)
            h ^= h >> np.uint64(29)
    return h


def duplicate_rows(p: Part, names: tuple[str, ...]) -> np.ndarray:
    """Indices of records identical in every column of `names` to an earlier record of the partition."""
    n = len(p)
    if n < 2:
        return np.zeros(0, dtype=np.int64)
    cols = [np.asarray(p[c]) for c in names if c in p.cols]
    h = _row_hash(cols)
    order = np.argsort(h, kind="stable")
    hs = h[order]
    cand = np.nonzero(hs[1:] == hs[:-1])[0]
    if cand.size == 0:
        return np.zeros(0, dtype=np.int64)
    out = []
    for k in cand:
        i, j = int(order[k]), int(order[k + 1])
        if all(c[i] == c[j] for c in cols):
            out.append(max(i, j))
    return np.unique(np.asarray(out, dtype=np.int64))


class SequenceCheck(Check):
    id = "sequence"
    title = "Sequence gaps and duplicates per channel"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.checkable = self.ds.sequence_checkable
        # (date, channel) -> [(instrument_id, unique seqs, ts of first occurrence, min and max non-snapshot seq)]
        self.chan: dict[tuple[int, int], list[tuple[int, np.ndarray, np.ndarray, int, int]]] = defaultdict(list)
        self.resets: dict[tuple[int, int], set[tuple[int, int]]] = defaultdict(set)
        self.records = 0
        self.ev_dup = self.evidence(
            "duplicate_records",
            ["date", "instrument_id", "row", "first_row", "ts_event", "sequence", "action", "order_id", "size"],
        )
        self.ev_reg = self.evidence(
            "sequence_regressions",
            ["date", "instrument_id", "channel_id", "row", "ts_event", "sequence", "previous_sequence"],
        )
        self.dup_total = 0
        self.reg_total = 0

    def partition(self, p: Part) -> None:
        n = len(p)
        self.records += n
        if n == 0:
            return
        names = (
            "ts_event",
            "ts_recv",
            "action",
            "side",
            "price",
            "size",
            "order_id",
            "flags",
            "sequence",
            "channel_id",
        )
        dups = duplicate_rows(p, names)
        if dups.size:
            self.dup_total += int(dups.size)
            # first copy of each duplicate (for the evidence)
            h = _row_hash([np.asarray(p[c]) for c in names])

            def first_of(i: int) -> int:
                return int(np.nonzero(h == h[i])[0][0])

            self.ev_dup.extend(
                (
                    (
                        p.ref.date,
                        p.ref.instrument_id,
                        int(i),
                        first_of(int(i)),
                        p["ts_event"][i],
                        p["sequence"][i],
                        chr_or(p["action"][i]),
                        p["order_id"][i],
                        p["size"][i],
                    )
                    for i in dups[: self.ev_dup.room]
                ),
                total=int(dups.size),
            )
        if not self.checkable:
            return
        seq = p["sequence"].astype(np.uint64)
        ch = p["channel_id"].astype(np.int64)
        ns = _not_snapshot(p)
        both = ns[1:] & ns[:-1] & (ch[1:] == ch[:-1])
        reg = np.nonzero(both & (seq[1:] < seq[:-1]))[0] + 1
        if reg.size:
            self.reg_total += int(reg.size)
            self.ev_reg.extend(
                (
                    (p.ref.date, p.ref.instrument_id, ch[i], int(i), p["ts_event"][i], seq[i], seq[i - 1])
                    for i in reg[: self.ev_reg.room]
                ),
                total=int(reg.size),
            )
        ts = p["ts_event"]
        snap = ~ns
        for c in np.unique(ch):
            on = ch == c
            sel = ns & on
            if not sel.any():
                continue
            key = (p.ref.session_date, int(c))
            # snapshot records cover their numbers but never start or end a span
            u, first = np.unique(seq[on], return_index=True)
            self.chan[key].append((p.ref.instrument_id, u, ts[on][first], int(seq[sel].min()), int(seq[sel].max())))
            if snap[on].any():
                # a snapshot resets the channel: the jump from the record before it to the one after is no gap
                rows = np.nonzero(on)[0]
                s_on = snap[rows]
                starts = np.nonzero(s_on & ~np.concatenate(([False], s_on[:-1])))[0]
                ends = np.nonzero(s_on & ~np.concatenate((s_on[1:], [False])))[0]
                for a, e in zip(starts, ends, strict=True):
                    if a > 0 and e + 1 < rows.size:
                        self.resets[key].add((int(seq[rows[a - 1]]), int(seq[rows[e + 1]])))

    def _holes(
        self, u: np.ndarray, uts: np.ndarray, spans: list[tuple[int, int, int]]
    ) -> list[tuple[int, int, int, int, int, list[int]]]:
        """Holes of the sorted unique numbers `u` inside the span of a dense partition: (lo, hi, missing,
        ts_lo, ts_hi, instruments)."""
        if u.size < 2:
            return []
        d = np.diff(u)
        idx = np.nonzero(d > 1)[0]
        out = []
        for k in idx:
            lo, hi = int(u[k]), int(u[k + 1])
            iids = [iid for iid, a, b in spans if a <= lo and hi <= b]
            if iids:
                out.append((lo, hi, hi - lo - 1, int(uts[k]), int(uts[k + 1]), iids))
        return out

    def finish(self) -> DqCheck:
        if self.skip_reason:
            return self.skipped()
        notes: list[str] = []
        ev_gap = self.evidence(
            "gaps",
            [
                "date",
                "channel_id",
                "sequence_before",
                "sequence_after",
                "missing",
                "ts_before",
                "ts_after",
                "instruments",
            ],
        )
        ev_col = self.evidence("cross_instrument_collisions", ["date", "channel_id", "sequence", "instruments"])
        gaps = missing = collisions = 0
        sparse: list[str] = []
        per_instrument: list[str] = []
        for (date, chan), items in sorted(self.chan.items()):
            allu = np.concatenate([it[1] for it in items])
            alluts = np.concatenate([it[2] for it in items])
            uniq, first, counts = np.unique(allu, return_index=True, return_counts=True)
            shared = int((counts > 1).sum())
            smallest = min(it[1].size for it in items)
            resets = self.resets.get((date, chan), set())
            if len(items) > 1 and smallest >= PER_INSTRUMENT_MIN and shared > PER_INSTRUMENT_SHARE * smallest:
                # per-instrument numbering on this channel: each partition is its own sequence space
                per_instrument.append(f"{date_str(date)} channel {chan}")
                groups = [(u, t, [(iid, a, b)]) for iid, u, t, a, b in items]
            else:
                if shared:
                    collisions += shared
                    owners: dict[int, list[int]] = defaultdict(list)
                    dup_vals = uniq[counts > 1][: self.cap]
                    for iid, u, *_ in items:
                        for v in np.intersect1d(u, dup_vals):
                            owners[int(v)].append(iid)
                    ev_col.extend(
                        ((date_str(date), chan, v, ",".join(map(str, owners[v]))) for v in sorted(owners)),
                        total=shared,
                    )
                spans = []
                for iid, _, _, a, b in items:
                    covered = int(np.searchsorted(uniq, b, "right") - np.searchsorted(uniq, a, "left"))
                    if covered / max(b - a + 1, 1) >= DENSE_SHARE:
                        spans.append((iid, a, b))
                    else:
                        sparse.append(f"{date_str(date)} channel {chan} instrument {iid}")
                groups = [(uniq, alluts[first], spans)]
            for u, t, spans in groups:
                for lo, hi, miss, t0, t1, iids in self._holes(u, t, spans):
                    if (lo, hi) in resets:
                        continue
                    gaps += 1
                    missing += miss
                    ev_gap.add((date_str(date), chan, lo, hi, miss, t0, t1, ",".join(map(str, iids))))
        dq_gaps = self.ds.ingest_counter("sequence_gaps")
        dq_reg = self.ds.ingest_counter("sequence_regressions")
        feed_gaps = len((self.ds.ingest_dq or {}).get("feed_gaps") or [])
        if not self.checkable:
            why = (
                f"sequence numbers of source format {self.ds.source_format!r} are assigned by the reader"
                if self.ds.rules["sequence_meaningful"]
                else f"rule set {self.ds.rule_set_name!r} marks sequence numbers as not meaningful"
            )
            notes.append(f"gap, regression and collision recount not run: {why}; only exact duplicate records checked")
        if sparse:
            notes.append(
                f"{len(sparse)} partition(s) hold a sparse share of their channel's numbering (a symbol-filtered "
                "feed): holes there are other instruments' messages, not gaps; the ingest counters (computed on the "
                "whole feed before the symbol filter) are authoritative: " + "; ".join(sparse[:5])
            )
        if per_instrument:
            notes.append("per-instrument sequence numbering detected on " + "; ".join(per_instrument[:5]))
        bad = missing + self.dup_total
        status = level(bad, self.records)
        if status == "pass" and (gaps or self.reg_total or collisions or dq_gaps or dq_reg or feed_gaps):
            status = "warn"
        parts = []
        if self.checkable:
            parts.append(f"{gaps} gap(s) ({missing} missing message(s))")
            parts.append(f"{self.reg_total} regression(s)")
            parts.append(f"{collisions} cross-instrument collision(s)")
        parts.append(f"{self.dup_total} exact duplicate record(s)")
        if dq_gaps or dq_reg or feed_gaps:
            parts.append(f"ingest counted {dq_gaps} gap(s), {dq_reg} regression(s), {feed_gaps} feed gap span(s)")
        metrics = {
            "records": self.records,
            "sequence_checked": self.checkable,
            "gaps": gaps,
            "missing_messages": missing,
            "regressions": self.reg_total,
            "cross_instrument_collisions": collisions,
            "duplicate_records": self.dup_total,
            "ingest_sequence_gaps": dq_gaps,
            "ingest_sequence_regressions": dq_reg,
            "ingest_feed_gaps": feed_gaps,
            "channels": len(self.chan),
        }
        return self.result(status, "; ".join(parts), metrics, [ev_gap, self.ev_reg, ev_col, self.ev_dup], notes)


# ---- timestamps -------------------------------------------------------------------------------------------------

#: Latency histogram bucket upper bounds (ns) and labels; negative latencies have their own bucket.
LAT_BUCKETS = [
    (0, "0"),
    (1_000, "< 1 us"),
    (10_000, "1-10 us"),
    (100_000, "10-100 us"),
    (1_000_000, "0.1-1 ms"),
    (10_000_000, "1-10 ms"),
    (100_000_000, "10-100 ms"),
    (1_000_000_000, "0.1-1 s"),
    (10_000_000_000, "1-10 s"),
]
#: Latencies kept per dataset for the distribution; beyond it every partition is sampled with a stride.
LAT_SAMPLE_MAX = 20_000_000


class TimestampCheck(Check):
    id = "timestamps"
    title = "Timestamp order and feed latency"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.records = 0
        self.regress = 0
        self.recv_regress = 0
        self.negative = 0
        self.bad_flag = 0
        self.hist = [0] * (len(LAT_BUCKETS) + 2)  # [negative, buckets..., >= last]
        self.lat: list[np.ndarray] = []
        self.top: list[tuple[int, str, int, int, int, int, int]] = []
        self.lat_n = 0
        self.stride = max(1, -(-self.ds.records // LAT_SAMPLE_MAX))
        self.ev_reg = self.evidence(
            "ts_event_regressions",
            ["date", "instrument_id", "row", "ts_event", "previous_ts_event", "back_ns", "sequence"],
        )
        self.ev_neg = self.evidence(
            "negative_latency", ["date", "instrument_id", "row", "ts_event", "ts_recv", "latency_ns", "sequence"]
        )

    def partition(self, p: Part) -> None:
        n = len(p)
        self.records += n
        if n == 0:
            return
        fl = p["flags"]
        ns = (fl & F_SNAPSHOT) == 0
        te, tr = p["ts_event"], p["ts_recv"]
        idx = np.nonzero(ns)[0]
        tns = te[idx]
        back = np.nonzero(tns[1:] < tns[:-1])[0] + 1
        if back.size:
            self.regress += int(back.size)
            self.ev_reg.extend(
                (
                    (
                        p.ref.date,
                        p.ref.instrument_id,
                        int(idx[k]),
                        tns[k],
                        tns[k - 1],
                        int(tns[k - 1] - tns[k]),
                        p["sequence"][idx[k]],
                    )
                    for k in back[: self.ev_reg.room]
                ),
                total=int(back.size),
            )
        self.recv_regress += int((tr[1:] < tr[:-1]).sum())
        bad = (fl & F_BAD_TS_RECV) != 0
        self.bad_flag += int(bad.sum())
        use = ns & ~bad
        lat = (tr - te)[use]
        rows = np.nonzero(use)[0]
        neg = np.nonzero(lat < 0)[0]
        if neg.size:
            self.negative += int(neg.size)
            self.ev_neg.extend(
                (
                    (
                        p.ref.date,
                        p.ref.instrument_id,
                        int(rows[k]),
                        te[rows[k]],
                        tr[rows[k]],
                        lat[k],
                        p["sequence"][rows[k]],
                    )
                    for k in neg[: self.ev_neg.room]
                ),
                total=int(neg.size),
            )
        self.hist[0] += int(neg.size)
        pos = lat[lat >= 0]
        edges = np.array([b for b, _ in LAT_BUCKETS], dtype=np.int64)
        # bucket 0: exactly 0; bucket k: [edge[k-1], edge[k])
        zero = int((pos == 0).sum())
        self.hist[1] += zero
        nz = pos[pos > 0]
        if nz.size:
            b = np.searchsorted(edges, nz, side="right")  # 1..len(edges)
            cnt = np.bincount(b, minlength=len(edges) + 1)
            for k in range(1, len(edges) + 1):
                self.hist[1 + k] += int(cnt[k]) if k < len(cnt) else 0
        self.lat_n += int(lat.size)
        self.lat.append(lat[:: self.stride].copy())
        if lat.size:
            k = min(self.cap, lat.size)
            top = np.argpartition(lat, lat.size - k)[lat.size - k :]
            for j in top:
                r = int(rows[j])
                self.top.append(
                    (int(lat[j]), p.ref.date, p.ref.instrument_id, r, int(te[r]), int(tr[r]), int(p["sequence"][r]))
                )

    def finish(self) -> DqCheck:
        if self.skip_reason:
            return self.skipped()
        lat = np.concatenate(self.lat) if self.lat else np.zeros(0, dtype=np.int64)
        zero_share = float((lat == 0).mean()) if lat.size else 0.0
        mixed = 0.5 < zero_share < 1.0
        if mixed:
            # most records carry one clock only (ts_recv copied into ts_event): the distribution is the others'
            lat = lat[lat != 0]
        p50, p90, p99, p999 = percentiles(lat, (50, 90, 99, 99.9))
        lo = float(lat.min()) if lat.size else None
        hi = float(lat.max()) if lat.size else None
        thr = self.ctx.spec.latency_outlier_ns
        auto = thr is None
        if auto:
            thr = int(max(10 * (p99 or 0), 1_000_000))
        outliers = int(sum(int((a > thr).sum()) for a in self.lat)) * self.stride
        ev_out = self.evidence(
            "latency_outliers", ["date", "instrument_id", "row", "ts_event", "ts_recv", "latency_ns", "sequence"]
        )
        top = sorted((t for t in self.top if t[0] > thr), key=lambda t: (-t[0], t[1], t[2], t[3]))
        ev_out.extend(((d, i, r, te, tr, la, s) for la, d, i, r, te, tr, s in top), total=outliers)
        ev_hist = self.evidence("latency_histogram", ["bucket", "records"])
        ev_hist.cap = len(self.hist)
        labels = ["negative", *(lab for _, lab in LAT_BUCKETS), ">= 10 s"]
        for lab, c in zip(labels, self.hist, strict=True):
            ev_hist.add((lab, c))
        notes = []
        if self.stride > 1:
            notes.append(
                f"latency percentiles and the outlier count come from every {self.stride}th record "
                f"({self.lat_n} latencies)"
            )
        if mixed:
            notes.append(
                f"{zero_share:.1%} of records have ts_recv equal to ts_event (the source gives them one clock); the "
                f"latency percentiles describe the other {lat.size} record(s)"
            )
        if self.lat_n and hi is not None and hi == 0 and lo == 0:
            notes.append("ts_recv equals ts_event on every record: the source carries no receive clock")
        if self.recv_regress:
            notes.append(
                f"{self.recv_regress} ts_recv regression(s) in feed order (informational): expected when a partition "
                "merges channels or when the receive clock is simulated; a capture written in arrival order has none"
            )
        if auto:
            notes.append(f"outlier threshold = max(10 x p99, 1 ms) = {_fmt_ns(thr)}")
        st_order = level(self.regress, self.records)
        st_neg = level(self.negative, self.lat_n)
        status = max(
            (st_order, st_neg, "warn" if outliers or self.bad_flag else "pass"),
            key=lambda s: {"pass": 0, "warn": 1, "fail": 2}[s],
        )
        summary = (
            f"{self.regress} ts_event regression(s), {self.negative} negative latency, {outliers} outlier(s) above "
            f"{_fmt_ns(thr)}; latency p50 {_fmt_ns(p50)}, p99 {_fmt_ns(p99)}, max {_fmt_ns(hi)}"
        )
        metrics = {
            "records": self.records,
            "ts_event_regressions": self.regress,
            "ts_recv_regressions": self.recv_regress,
            "bad_ts_recv_flagged": self.bad_flag,
            "latencies": self.lat_n,
            "negative_latency": self.negative,
            "latency_min_ns": lo,
            "latency_p50_ns": p50,
            "latency_p90_ns": p90,
            "latency_p99_ns": p99,
            "latency_p999_ns": p999,
            "latency_max_ns": hi,
            "zero_latency_share": zero_share,
            "outlier_threshold_ns": thr,
            "outliers": outliers,
        }
        return self.result(status, summary, metrics, [self.ev_reg, self.ev_neg, ev_out, ev_hist], notes)


# ---- sizes and price grid ---------------------------------------------------------------------------------------


#: Venues quoting in $0.01 above $1.00 and $0.0001 below (Reg NMS Rule 612).
US_EQUITY_VENUES = frozenset({"nasdaq", "iex"})


class SizePriceCheck(Check):
    id = "size_price"
    title = "Sizes and price grid"

    ISSUES = (
        "zero_size",
        "size_overflow",
        "undefined_price",
        "nonpositive_price",
        "off_tick_resting",
    )

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.records = 0
        self.counts: dict[str, int] = defaultdict(int)
        self.ev = self.evidence(
            "records",
            [
                "issue",
                "date",
                "instrument_id",
                "row",
                "ts_event",
                "sequence",
                "action",
                "side",
                "price",
                "size",
                "tick_size",
            ],
        )
        self.no_tick: list[str] = []

    def _flag(self, issue: str, p: Part, idx: np.ndarray, tick: int | None) -> None:
        if idx.size == 0:
            return
        self.counts[issue] += int(idx.size)
        self.ev.extend(
            (
                (
                    issue,
                    p.ref.date,
                    p.ref.instrument_id,
                    int(i),
                    p["ts_event"][i],
                    p["sequence"][i],
                    chr_or(p["action"][i]),
                    chr_or(p["side"][i]),
                    p["price"][i],
                    p["size"][i],
                    tick,
                )
                for i in idx[: self.ev.room]
            ),
            total=int(idx.size),
        )

    def partition(self, p: Part) -> None:
        n = len(p)
        self.records += n
        if n == 0:
            return
        act, px, sz = p["action"], p["price"], p["size"].astype(np.uint64)
        inst = p.ref.instrument or {}
        tick = int(inst.get("tick_size") or 0) or None
        lot = int(inst.get("lot_size") or 0) or None
        priced = np.isin(act, (A, M, F, T))
        self._flag("zero_size", p, np.nonzero(np.isin(act, (A, F, T)) & (sz == 0))[0], tick)
        self._flag("size_overflow", p, np.nonzero(sz >= np.uint64(1 << 63))[0], tick)
        undef = px == UNDEF_PRICE
        self._flag("undefined_price", p, np.nonzero(priced & undef)[0], tick)
        nonpos = np.nonzero(priced & ~undef & (px <= 0))[0]
        if self.ds.rules["negative_prices_allowed"]:
            self.counts["nonpositive_price_allowed"] += int(nonpos.size)
        else:
            self._flag("nonpositive_price", p, nonpos, tick)
        if tick and tick > 1:
            ok = ~undef & (px > 0)
            off = (px % tick) != 0
            exp = inst.get("price_exp")
            us_equity = self.ds.venue in US_EQUITY_VENUES or self.ds.rule_set_name in US_EQUITY_VENUES
            if us_equity and exp is not None and int(exp) <= -4:
                # Reg NMS Rule 612: quotes below $1.00 may use $0.0001 increments
                one, sub = 10 ** (-int(exp)), 10 ** (-4 - int(exp))
                off = np.where(px < one, (px % sub) != 0, off)
            self._flag("off_tick_resting", p, np.nonzero(np.isin(act, (A, M)) & ok & off)[0], tick)
            self.counts["off_tick_executions"] += int((np.isin(act, (F, T)) & ok & off).sum())
        else:
            self.no_tick.append(f"{p.ref.date} {p.ref.symbol}")
        if lot and lot > 1:
            self.counts["odd_lot_adds"] += int(((act == A) & (sz % np.uint64(lot) != 0)).sum())

    def finish(self) -> DqCheck:
        if self.skip_reason:
            return self.skipped()
        bad = sum(self.counts[k] for k in self.ISSUES)
        status = level(bad, self.records)
        if self.counts["size_overflow"]:
            status = "fail"
        notes = []
        if self.no_tick:
            notes.append(
                f"price grid not checked for {len(self.no_tick)} partition(s) without a tick size above one price "
                "unit: " + ", ".join(self.no_tick[:5])
            )
        if self.counts["off_tick_executions"]:
            notes.append(
                "off-tick executions are informational: midpoint and price-improved executions print between ticks"
            )
        if self.ds.rules["negative_prices_allowed"]:
            notes.append(f"rule set {self.ds.rule_set_name!r} allows zero and negative prices (spreads)")
        found = [f"{self.counts[k]} {k.replace('_', ' ')}" for k in self.ISSUES if self.counts[k]]
        summary = ", ".join(found) if found else "every size positive and every resting price on the tick grid"
        metrics: dict[str, Any] = {"records": self.records}
        for k in (*self.ISSUES, "off_tick_executions", "odd_lot_adds", "nonpositive_price_allowed"):
            metrics[k] = self.counts[k]
        return self.result(status, summary, metrics, [self.ev], notes)


# ---- session coverage -------------------------------------------------------------------------------------------

#: Extended-hours close of Nasdaq (20:00 local): data ending at 00:00 UTC before it is a truncated tail.
NASDAQ_EXTENDED_CLOSE_S = 72_000


class SessionCheck(Check):
    id = "session_coverage"
    title = "Session-boundary coverage"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.tol = ctx.spec.session_tolerance_ns
        self.ev = self.evidence(
            "partitions", ["issue", "date", "instrument_id", "symbol", "first_ts", "last_ts", "open_ts", "close_ts"]
        )
        self.counts: dict[str, int] = defaultdict(int)
        self.partitions = 0

    def partition(self, p: Part) -> None:
        self.partitions += 1
        win = self.ds.session_window(p.ref)
        if win is None:
            self.counts["no_instrument_row"] += 1
            return
        mid, open_ns, close_ns = win
        ts = p["ts_event"][_not_snapshot(p)]
        if ts.size == 0:
            self.counts["empty"] += 1
            self.ev.add(("empty", p.ref.date, p.ref.instrument_id, p.ref.symbol, None, None, open_ns, close_ns))
            return
        first, last = int(ts.min()), int(ts.max())
        self.counts["pre_open_records"] += int((ts < open_ns).sum())
        self.counts["post_close_records"] += int((ts >= close_ns).sum())
        n_reg = int(((ts >= open_ns) & (ts < close_ns)).sum())
        row = (p.ref.date, p.ref.instrument_id, p.ref.symbol, first, last, open_ns, close_ns)
        issues = []
        if n_reg == 0:
            issues.append("no_regular_hours_data")
        else:
            if first > open_ns + self.tol:
                snap_start = bool(p["flags"][0] & F_SNAPSHOT)
                issues.append("starts_late" if snap_start else "starts_late_without_snapshot")
            if last < close_ns - self.tol:
                issues.append("ends_early")
        utc_mid = (last // DAY_NS + 1) * DAY_NS
        day_end = mid + (NASDAQ_EXTENDED_CLOSE_S * NS if self.ds.venue == "nasdaq" else DAY_NS)
        if last >= utc_mid - NS and utc_mid + self.tol < day_end:
            issues.append("truncated_at_utc_midnight")
        for issue in issues:
            self.counts[issue] += 1
            self.ev.add((issue, *row))

    def finish(self) -> DqCheck:
        if self.skip_reason:
            return self.skipped()
        issue_keys = (
            "no_regular_hours_data",
            "starts_late",
            "starts_late_without_snapshot",
            "ends_early",
            "truncated_at_utc_midnight",
            "empty",
            "no_instrument_row",
        )
        n_issues = sum(self.counts[k] for k in issue_keys)
        status = "warn" if n_issues else "pass"
        notes = [
            f"tolerance {_fmt_ns(self.tol)} at the open and the close; records outside the session are kept by "
            "ingest and reported here for information"
        ]
        if self.counts["starts_late_without_snapshot"]:
            notes.append(
                "a partition that starts after the open without a snapshot has no record of the orders resting at its "
                "start: expect unknown order references and an incomplete book until they are replaced"
            )
        found = [f"{self.counts[k]} {k.replace('_', ' ')}" for k in issue_keys if self.counts[k]]
        summary = ", ".join(found) if found else f"all {self.partitions} partition(s) cover the session open and close"
        summary += (
            f"; {self.counts['pre_open_records']} record(s) before the open, "
            f"{self.counts['post_close_records']} after the close"
        )
        metrics: dict[str, Any] = {"partitions": self.partitions, "tolerance_ns": self.tol}
        for k in (*issue_keys, "pre_open_records", "post_close_records"):
            metrics[k] = self.counts[k]
        return self.result(status, summary, metrics, [self.ev], notes)


# ---- snapshots and clears ---------------------------------------------------------------------------------------


class SnapshotCheck(Check):
    id = "snapshots"
    title = "Snapshot and clear handling"

    WARN_KEYS = (
        "snapshot_without_clear",
        "partition_without_snapshot",
        "records_before_first_snapshot",
        "unterminated_snapshot",
        "clear_without_snapshot",
    )

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.counts: dict[str, int] = defaultdict(int)
        self.partitions = 0
        self.ev = self.evidence("records", ["issue", "date", "instrument_id", "row", "ts_event", "sequence", "count"])

    def _add(self, issue: str, p: Part, row: int, count: int = 1) -> None:
        self.counts[issue] += count
        ts = p["ts_event"][row] if 0 <= row < len(p) else None
        seq = p["sequence"][row] if 0 <= row < len(p) else None
        self.ev.add((issue, p.ref.date, p.ref.instrument_id, row, ts, seq, count))

    def partition(self, p: Part) -> None:
        self.partitions += 1
        n = len(p)
        if n == 0:
            return
        rules = self.ds.rules
        fl, act = p["flags"], p["action"]
        snap = (fl & F_SNAPSHOT) != 0
        self.counts["snapshot_records"] += int(snap.sum())
        clears = np.nonzero(act == R)[0]
        self.counts["clears"] += int(clears.size)
        bad_book = np.nonzero((fl & F_MAYBE_BAD_BOOK) != 0)[0]
        if bad_book.size:
            self._add("maybe_bad_book", p, int(bad_book[0]), int(bad_book.size))
        starts = np.nonzero(snap & ~np.concatenate(([False], snap[:-1])))[0]
        ends = np.nonzero(snap & ~np.concatenate((snap[1:], [False])))[0]
        self.counts["snapshot_runs"] += int(starts.size)
        for s, e in zip(starts, ends, strict=True):
            prev_clear = s > 0 and act[s - 1] == R
            if rules["snapshot_begins_with_clear"] and not prev_clear and act[s] != R:
                self._add("snapshot_without_clear", p, int(s))
            if not fl[e] & F_LAST:
                self._add("unterminated_snapshot", p, int(e))
        if rules["utc_midnight_snapshot"] and not snap[0]:
            self._add("partition_without_snapshot", p, 0)
        if starts.size and starts[0] > 0:
            k = int(starts[0])
            pre = int((~snap[:k] & (act[:k] != R)).sum())
            if pre:
                self._add("records_before_first_snapshot", p, 0, pre)
        for c in clears:
            nxt = int(c) + 1
            if snap[c]:
                continue
            if (
                rules["snapshot_records_flagged"]
                and rules["snapshot_begins_with_clear"]
                and (nxt >= n or not snap[nxt])
            ):
                self._add("clear_without_snapshot", p, int(c))

    def finish(self) -> DqCheck:
        if self.skip_reason:
            return self.skipped()
        rp = self.ctx.replay.summary.get("data_quality", {}) if self.ctx.replay else {}
        src = rp.get("source_specific") or {}
        extra = {
            "replay_files_without_snapshot": int(src.get("files_without_snapshot", 0) or 0),
            "replay_unterminated_events": int(src.get("unterminated_events", 0) or 0),
            "ingest_pre_snapshot_rows_skipped": self.ds.ingest_counter("pre_snapshot_rows_skipped"),
        }
        n_warn = sum(self.counts[k] for k in self.WARN_KEYS) + sum(extra.values())
        status = "fail" if self.counts["maybe_bad_book"] else ("warn" if n_warn else "pass")
        if self.counts["snapshot_runs"] == 0 and not self.counts["clears"]:
            summary = "no snapshot or clear records"
        else:
            summary = (
                f"{self.counts['snapshot_runs']} snapshot run(s) ({self.counts['snapshot_records']} records), "
                f"{self.counts['clears']} clear(s)"
            )
        found = [
            f"{self.counts[k]} {k.replace('_', ' ')}" for k in ("maybe_bad_book", *self.WARN_KEYS) if self.counts[k]
        ]
        found += [f"{v} {k.replace('_', ' ')}" for k, v in extra.items() if v]
        if found:
            summary += "; " + ", ".join(found)
        notes = []
        r = self.ds.rules
        notes.append(
            f"rule set {self.ds.rule_set_name!r}: snapshots begin with a clear: {r['snapshot_begins_with_clear']}, "
            f"snapshot records flagged: {r['snapshot_records_flagged']}, snapshot at each UTC midnight: "
            f"{r['utc_midnight_snapshot']}"
        )
        metrics: dict[str, Any] = {"partitions": self.partitions}
        for k in ("snapshot_records", "snapshot_runs", "clears", "maybe_bad_book", *self.WARN_KEYS):
            metrics[k] = self.counts[k]
        metrics.update(extra)
        return self.result(status, summary, metrics, [self.ev], notes)


# ---- orphan references ------------------------------------------------------------------------------------------


def orphan_rows(p: Part) -> np.ndarray:
    """Rows of `C`, `F` and `M` records whose order id was never added earlier in the partition (since the last
    clear): `A` records and `M` records (an `M` of an unknown order acts as an add) count as adds, snapshot records
    included. An order referenced again after it was fully removed is not detected here (the replay counts it)."""
    act = p["action"]
    n = len(act)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    oid = p["order_id"].astype(np.uint64)
    seg = np.cumsum(act == R)
    is_add = np.isin(act, (A, M))
    is_ref = np.isin(act, (C, F, M))
    rows = np.nonzero(is_add | is_ref)[0]
    if rows.size == 0:
        return rows
    order = rows[np.lexsort((rows, oid[rows], seg[rows]))]
    s_seg, s_oid = seg[order], oid[order]
    new_grp = np.ones(order.size, dtype=bool)
    new_grp[1:] = (s_seg[1:] != s_seg[:-1]) | (s_oid[1:] != s_oid[:-1])
    adds = is_add[order].astype(np.int64)
    cum = np.cumsum(adds)
    grp_start = np.maximum.accumulate(np.where(new_grp, np.arange(order.size), 0))
    before_grp = np.where(grp_start > 0, cum[grp_start - 1], 0)
    adds_before = cum - adds - before_grp
    orphan = is_ref[order] & (adds_before == 0)
    return np.sort(order[orphan])


class OrphanCheck(Check):
    id = "orphan_refs"
    title = "Orphan executions, cancels and modifies"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.records = 0
        self.by_action: dict[str, int] = defaultdict(int)
        self.ev = self.evidence(
            "records",
            ["date", "instrument_id", "row", "ts_event", "sequence", "action", "side", "price", "size", "order_id"],
        )
        self.mid_start: list[str] = []

    def partition(self, p: Part) -> None:
        self.records += len(p)
        rows = orphan_rows(p)
        if rows.size == 0:
            return
        for a, c in zip(*np.unique(p["action"][rows], return_counts=True), strict=True):
            self.by_action[chr_or(a)] += int(c)
        self.ev.extend(
            (
                (
                    p.ref.date,
                    p.ref.instrument_id,
                    int(i),
                    p["ts_event"][i],
                    p["sequence"][i],
                    chr_or(p["action"][i]),
                    chr_or(p["side"][i]),
                    p["price"][i],
                    p["size"][i],
                    p["order_id"][i],
                )
                for i in rows[: self.ev.room]
            ),
            total=int(rows.size),
        )
        win = self.ds.session_window(p.ref)
        if win and len(p) and int(p["ts_event"][0]) > win[1] and not p["flags"][0] & F_SNAPSHOT:
            self.mid_start.append(f"{p.ref.date} {p.ref.symbol}")

    def finish(self) -> DqCheck:
        if self.skip_reason:
            return self.skipped()
        scan = sum(self.by_action.values())
        inv = (self.ctx.replay.summary.get("invariants") or {}) if self.ctx.replay else {}
        replay_refs = inv.get("unknown_order_refs")
        replay_fills = inv.get("fills_unknown_order")
        notes = [
            "scan: a C, F or M record whose order id has no earlier add in its partition (since the last clear); "
            "the replay counters also catch references to orders already removed"
        ]
        if self.mid_start:
            notes.append(
                f"{len(self.mid_start)} partition(s) start after the open without a snapshot, so orders resting "
                "before their first record are unknown: " + ", ".join(self.mid_start[:5])
            )
        if self.ctx.replay is None:
            notes.append("replay not run: only the scan count is available")
        worst_n = max(scan, int(replay_refs or 0) + int(replay_fills or 0))
        status = level(worst_n, self.records)
        summary = f"{scan} orphan reference(s) by scan"
        if self.by_action:
            summary += " (" + ", ".join(f"{a}: {c}" for a, c in sorted(self.by_action.items())) + ")"
        if replay_refs is not None:
            summary += f"; replay: {replay_refs} unknown order reference(s), {replay_fills} fill(s) of unknown orders"
        metrics: dict[str, Any] = {
            "records": self.records,
            "orphans_scan": scan,
            "orphan_share": ratio(worst_n, self.records),
            "replay_unknown_order_refs": replay_refs,
            "replay_fills_unknown_order": replay_fills,
            "replay_cancels_exceeding_resting": inv.get("cancels_exceeding_resting"),
            "replay_fills_exceeding_resting": inv.get("fills_exceeding_resting"),
            "replay_duplicate_adds": inv.get("duplicate_adds"),
        }
        for a, c in sorted(self.by_action.items()):
            metrics[f"orphans_{a}"] = c
        return self.result(status, summary, metrics, [self.ev], notes)


# ---- per-day rate drift -----------------------------------------------------------------------------------------

DRIFT_LOW, DRIFT_HIGH = 0.25, 4.0


class RateDriftCheck(Check):
    id = "rate_drift"
    title = "Per-day message-rate drift"
    mbo_only = False

    def finish(self) -> DqCheck:
        by_sym: dict[str, dict[str, int]] = defaultdict(dict)
        for ref in self.ds.parts:
            by_sym[ref.symbol][ref.date] = by_sym[ref.symbol].get(ref.date, 0) + ref.rows
        ev = self.evidence("days", ["symbol", "date", "records", "median_records", "ratio"])
        checked = 0
        for sym in sorted(by_sym):
            days = by_sym[sym]
            if len(days) < 3:
                continue
            checked += 1
            for d in sorted(days):
                others = [v for k, v in days.items() if k != d]
                med = statistics.median(others)
                r = days[d] / med if med else None
                if r is None or r < DRIFT_LOW or r > DRIFT_HIGH:
                    ev.add((sym, d, days[d], med, r))
        if not checked:
            return self.skipped("needs three or more session dates of one symbol")
        status = "warn" if ev.total else "pass"
        summary = (
            f"{ev.total} day(s) outside {DRIFT_LOW}x-{DRIFT_HIGH}x of the symbol's median over the other days"
            if ev.total
            else f"every day of {checked} symbol(s) within {DRIFT_LOW}x-{DRIFT_HIGH}x of its median"
        )
        notes = [
            "a day far from its symbol's usual record count often means a partial download, a silent vendor "
            "re-issue or a different feed vintage; holidays and half days also show here"
        ]
        return self.result(status, summary, {"symbols_checked": checked, "days_flagged": ev.total}, [ev], notes)


__all__ = [
    "FAIL_RATIO",
    "OrphanCheck",
    "RateDriftCheck",
    "SequenceCheck",
    "SessionCheck",
    "SizePriceCheck",
    "SnapshotCheck",
    "TimestampCheck",
    "duplicate_rows",
    "orphan_rows",
]
