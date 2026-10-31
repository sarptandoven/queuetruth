//! `L2Builder`: an MBP aggregate built from MBO with the record semantics of SPEC 5.1.1 (priority irrelevant,
//! independent of `qt_book`) and the batching, view and snapshot rules of SPEC 5.3.1.
//!
//! - Aggregate: order map `id -> (side, price, size)` and level map `(side, price) -> (qty, count)`, updated per
//!   record; `aggregate_level` reads it at any time.
//! - Batches: `cadence_ns = 0` emits at every `LAST` record. `cadence_ns > 0` emits at the first `LAST` record with
//!   `ts_event >= bucket_end` (`bucket_end = (floor(ts_prev_emit / cadence) + 1) * cadence`), depth as the net change
//!   since the previous emission (`CONFLATED`); trade rows keep their own `ts_event` and come out in the batch that
//!   covers them. The first emission of a partition is immediate.
//! - View: top `depth_levels` levels per side (0 = all). Depth rows for levels whose qty (and count, with
//!   `include_counts`) differ from what the view last showed, levels that entered the view (`INTO_VIEW`,
//!   `prev_qty = 0`), and levels that left it (`new_qty = 0`; `OUT_OF_VIEW` if still present below N). `level` is the
//!   rank in the new view (255 if out; with `depth_levels = 0` ranks above 254 saturate at 254).
//! - Order in a batch: `before` = trades (record order) then depth (bid then ask, best first); `after` = depth then
//!   trades. Snapshot batches (the first batch of a partition and every batch containing an `R`) emit a `clear` row
//!   then `snapshot` rows (`SNAPSHOT`) for the whole view after the batch instead of depth rows; their trade rows
//!   follow the snapshot. The last row carries `LAST_IN_BATCH`; `event_id` counts emitted batches from 1.

use std::collections::{BTreeMap, HashMap};
use std::hash::{BuildHasherDefault, Hasher};

use qt_schema::rows::{L2Kind, L2Row, l2flags};
use qt_types::{BookSide, MboEvent, Px, Qty, Side, Ts, UNDEF_PRICE, VenueRules};

use crate::{L2Config, TradePosition};

#[derive(Default, Clone, Copy)]
struct IdHasher(u64);

impl Hasher for IdHasher {
    #[inline]
    fn finish(&self) -> u64 {
        self.0
    }
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.0 = (self.0.rotate_left(8) ^ u64::from(b)).wrapping_mul(0x9e37_79b9_7f4a_7c15);
        }
    }
    #[inline]
    fn write_u64(&mut self, v: u64) {
        let x = (self.0 ^ v).wrapping_mul(0x9e37_79b9_7f4a_7c15);
        self.0 = x ^ (x >> 29);
    }
}

type IdMap<V> = HashMap<u64, V, BuildHasherDefault<IdHasher>>;

/// One side of the aggregate: price -> (qty, count).
#[derive(Default, Clone)]
pub(crate) struct SideLevels {
    pub(crate) levels: BTreeMap<Px, (Qty, u32)>,
}

impl SideLevels {
    fn add(&mut self, px: Px, sz: Qty) {
        let l = self.levels.entry(px).or_insert((0, 0));
        l.0 = l.0.saturating_add(sz);
        l.1 = l.1.saturating_add(1);
    }
    fn reduce(&mut self, px: Px, by: Qty) {
        if let Some(l) = self.levels.get_mut(&px) {
            l.0 = l.0.saturating_sub(by);
        }
    }
    fn remove(&mut self, px: Px, sz: Qty) {
        if let Some(l) = self.levels.get_mut(&px) {
            l.0 = l.0.saturating_sub(sz);
            l.1 = l.1.saturating_sub(1);
            if l.1 == 0 {
                self.levels.remove(&px);
            }
        }
    }
}

fn better(side: BookSide, a: Px, b: Px) -> bool {
    match side {
        BookSide::Bid => a > b,
        BookSide::Ask => a < b,
    }
}

/// Number of levels strictly better than `px` (saturating at 254).
fn rank_in(side: BookSide, m: &BTreeMap<Px, (Qty, u32)>, px: Px) -> u8 {
    use std::ops::Bound::{Excluded, Unbounded};
    let n = match side {
        BookSide::Bid => m.range((Excluded(px), Unbounded)).take(254).count(),
        BookSide::Ask => m.range(..px).take(254).count(),
    };
    n as u8
}

/// Top `n` levels of `m` (best first) into `buf` (`n = 0`: all).
fn top_into(side: BookSide, m: &BTreeMap<Px, (Qty, u32)>, n: usize, buf: &mut Vec<(Px, Qty, u32)>) {
    buf.clear();
    let n = if n == 0 { usize::MAX } else { n };
    match side {
        BookSide::Bid => buf.extend(m.iter().rev().take(n).map(|(p, (q, c))| (*p, *q, *c))),
        BookSide::Ask => buf.extend(m.iter().take(n).map(|(p, (q, c))| (*p, *q, *c))),
    }
}

#[derive(Clone, Copy)]
struct Rec {
    ts_recv: Ts,
    sequence: u64,
}

/// MBO -> L2 view builder for one instrument partition.
pub struct L2Builder {
    cfg: L2Config,
    rules: VenueRules,
    instrument_id: u32,
    orders: IdMap<(BookSide, Px, Qty)>,
    sides: [SideLevels; 2],
    /// What the view last showed: full map when `depth_levels = 0`, else a best-first vector of at most N.
    shown_map: [BTreeMap<Px, (Qty, u32)>; 2],
    shown_vec: [Vec<(Px, Qty, u32)>; 2],
    dirty: [Vec<Px>; 2],
    view_buf: Vec<(Px, Qty, u32)>,
    trades: Vec<L2Row>,
    had_clear: bool,
    first_batch: bool,
    pending: bool,
    last_rec: Option<Rec>,
    last_emit: Option<Ts>,
    event_id: u64,
    unresolved_aggressor: u64,
}

const fn idx(s: BookSide) -> usize {
    match s {
        BookSide::Bid => 0,
        BookSide::Ask => 1,
    }
}

const SIDES: [BookSide; 2] = [BookSide::Bid, BookSide::Ask];

impl L2Builder {
    /// New builder.
    pub fn new(cfg: L2Config, rules: VenueRules, instrument_id: u32) -> Self {
        Self {
            cfg,
            rules,
            instrument_id,
            orders: IdMap::default(),
            sides: [SideLevels::default(), SideLevels::default()],
            shown_map: [BTreeMap::new(), BTreeMap::new()],
            shown_vec: [Vec::new(), Vec::new()],
            dirty: [Vec::new(), Vec::new()],
            view_buf: Vec::new(),
            trades: Vec::new(),
            had_clear: false,
            first_batch: true,
            pending: false,
            last_rec: None,
            last_emit: None,
            event_id: 0,
            unresolved_aggressor: 0,
        }
    }

    /// Non-hidden trades without an aggressor whose side could not be inferred from the prevailing BBO.
    pub fn unresolved_aggressor_trades(&self) -> u64 {
        self.unresolved_aggressor
    }

    /// Batches emitted so far (the last `event_id`).
    pub fn batches(&self) -> u64 {
        self.event_id
    }

    #[inline]
    fn touch(&mut self, s: BookSide, px: Px) {
        self.dirty[idx(s)].push(px);
    }

    fn add_order(&mut self, id: u64, s: BookSide, px: Px, sz: Qty) {
        if let Some((os, opx, osz)) = self.orders.remove(&id) {
            self.sides[idx(os)].remove(opx, osz);
            self.touch(os, opx);
        }
        self.orders.insert(id, (s, px, sz));
        self.sides[idx(s)].add(px, sz);
        self.touch(s, px);
    }

    fn remove_order(&mut self, id: u64) {
        if let Some((s, px, sz)) = self.orders.remove(&id) {
            self.sides[idx(s)].remove(px, sz);
            self.touch(s, px);
        }
    }

    fn apply_book(&mut self, e: &MboEvent) {
        match e.action {
            b'A' => {
                let Some(s) = BookSide::from_byte(e.side) else { return };
                if e.price == UNDEF_PRICE || e.size == 0 {
                    return;
                }
                self.add_order(e.order_id, s, e.price, e.size);
            }
            b'C' => {
                let Some(&(s, px, sz)) = self.orders.get(&e.order_id) else {
                    return;
                };
                if e.size < sz {
                    self.sides[idx(s)].reduce(px, e.size);
                    if let Some(o) = self.orders.get_mut(&e.order_id) {
                        o.2 = sz - e.size;
                    }
                    self.touch(s, px);
                } else {
                    self.remove_order(e.order_id);
                }
            }
            b'M' => {
                if !self.rules.modify_records_present {
                    return;
                }
                match self.orders.get(&e.order_id).copied() {
                    None => {
                        let Some(s) = BookSide::from_byte(e.side) else { return };
                        if e.price != UNDEF_PRICE && e.size > 0 {
                            self.add_order(e.order_id, s, e.price, e.size);
                        }
                    }
                    Some((s, px, sz)) => {
                        if e.size == 0 {
                            self.remove_order(e.order_id);
                        } else if e.price == px {
                            let l = &mut self.sides[idx(s)];
                            if e.size <= sz {
                                l.reduce(px, sz - e.size);
                            } else if let Some(lv) = l.levels.get_mut(&px) {
                                lv.0 = lv.0.saturating_add(e.size - sz);
                            }
                            if let Some(o) = self.orders.get_mut(&e.order_id) {
                                o.2 = e.size;
                            }
                            self.touch(s, px);
                        } else if e.price != UNDEF_PRICE {
                            // Moves to the new price (on the order's side); the record side is ignored as in 5.1.1.
                            self.add_order(e.order_id, s, e.price, e.size);
                        } else {
                            self.remove_order(e.order_id);
                        }
                    }
                }
            }
            b'R' => {
                for s in SIDES {
                    self.sides[idx(s)].levels.clear();
                }
                self.orders.clear();
                self.had_clear = true;
            }
            _ => {}
        }
    }

    /// Aggressor of a non-hidden trade without one (e.g. CME implied): at the prevailing best bid a sell hit the
    /// bid, at the best ask a buy hit the ask; anything else stays `N` (counted).
    fn infer_aggressor(&mut self, px: Px) -> Side {
        let bid = self.sides[0].levels.keys().next_back().copied();
        let ask = self.sides[1].levels.keys().next().copied();
        match (bid == Some(px), ask == Some(px)) {
            (true, false) => Side::Ask,
            (false, true) => Side::Bid,
            _ => {
                self.unresolved_aggressor += 1;
                Side::None
            }
        }
    }

    fn trade_row(&mut self, e: &MboEvent) -> Option<L2Row> {
        let mut aggr = Side::from_byte(e.side);
        let hidden = aggr == Side::None && self.rules.supports_hidden_orders;
        if hidden && !self.cfg.emit_hidden_trades {
            return None;
        }
        if aggr == Side::None && !hidden && e.price != UNDEF_PRICE {
            aggr = self.infer_aggressor(e.price);
        }
        Some(L2Row {
            ts_event: e.ts_event,
            ts_recv: e.ts_recv,
            instrument_id: self.instrument_id,
            event_id: 0,
            sequence: e.sequence,
            kind: L2Kind::Trade,
            side: aggr.opposite(),
            price: (e.price != UNDEF_PRICE).then_some(e.price),
            prev_qty: 0,
            new_qty: 0,
            prev_ct: None,
            new_ct: None,
            trade_qty: e.size,
            aggressor: aggr,
            level: 255,
            flags: if hidden { l2flags::HIDDEN_TRADE } else { 0 },
        })
    }

    /// Feeds one record; appends rows when a batch is emitted.
    pub fn push(&mut self, e: &MboEvent, out: &mut Vec<L2Row>) {
        self.apply_book(e);
        if e.action == b'T'
            && let Some(r) = self.trade_row(e)
        {
            self.trades.push(r);
        }
        self.pending = true;
        self.last_rec = Some(Rec {
            ts_recv: e.ts_recv,
            sequence: e.sequence,
        });
        if !e.is_last() {
            return;
        }
        let due = match (self.cfg.cadence_ns, self.last_emit) {
            (c, Some(prev)) if c > 0 => e.ts_event >= bucket_end(prev, c),
            _ => true,
        };
        if due {
            self.emit(e.ts_event, e.ts_recv, e.sequence, out);
        }
    }

    /// End of input or forced cadence boundary: emits what is pending as a batch at `ts`.
    pub fn flush(&mut self, ts: Ts, out: &mut Vec<L2Row>) {
        if !self.pending {
            return;
        }
        let (ts_recv, seq) = self.last_rec.map_or((ts, 0), |r| (r.ts_recv, r.sequence));
        self.emit(ts, ts_recv, seq, out);
    }

    /// Own MBP aggregate of a level (oracle use).
    pub fn aggregate_level(&self, side: BookSide, px: Px) -> Option<(Qty, u32)> {
        self.sides[idx(side)].levels.get(&px).copied()
    }

    /// Top `n` aggregate levels of a side, best first (0 = all).
    pub fn aggregate_levels(&self, side: BookSide, n: usize) -> Vec<(Px, Qty, u32)> {
        let mut v = Vec::new();
        top_into(side, &self.sides[idx(side)].levels, n, &mut v);
        v
    }

    fn view_n(&self) -> usize {
        usize::from(self.cfg.depth_levels)
    }

    fn ct(&self, c: u32) -> Option<u32> {
        self.cfg.include_counts.then_some(c)
    }

    fn differs(&self, a: (Qty, u32), b: (Qty, u32)) -> bool {
        a.0 != b.0 || (self.cfg.include_counts && a.1 != b.1)
    }

    fn base_row(&self, ts: Ts, ts_recv: Ts, seq: u64, kind: L2Kind) -> L2Row {
        L2Row {
            ts_event: ts,
            ts_recv,
            instrument_id: self.instrument_id,
            event_id: self.event_id,
            sequence: seq,
            kind,
            side: Side::None,
            price: None,
            prev_qty: 0,
            new_qty: 0,
            prev_ct: None,
            new_ct: None,
            trade_qty: 0,
            aggressor: Side::None,
            level: 255,
            flags: 0,
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn depth_row(
        &self,
        b: (Ts, Ts, u64),
        side: BookSide,
        px: Px,
        prev: Option<(Qty, u32)>,
        new: Option<(Qty, u32)>,
        flags: u8,
        level: u8,
    ) -> L2Row {
        let mut r = self.base_row(b.0, b.1, b.2, L2Kind::Depth);
        r.side = side.to_side();
        r.price = Some(px);
        r.prev_qty = prev.map_or(0, |p| p.0);
        r.prev_ct = self.ct(prev.map_or(0, |p| p.1));
        r.new_qty = new.map_or(0, |p| p.0);
        r.new_ct = self.ct(new.map_or(0, |p| p.1));
        r.level = level;
        r.flags = flags;
        r
    }

    fn depth_rows(&mut self, side: BookSide, b: (Ts, Ts, u64), extra: u8, rows: &mut Vec<L2Row>) {
        let si = idx(side);
        if self.dirty[si].is_empty() {
            return;
        }
        let n = self.view_n();
        let start = rows.len();
        if n == 0 {
            let mut dirty = std::mem::take(&mut self.dirty[si]);
            dirty.sort_unstable();
            dirty.dedup();
            for &px in &dirty {
                let cur = self.sides[si].levels.get(&px).copied();
                let prev = self.shown_map[si].get(&px).copied();
                match (prev, cur) {
                    (None, None) => {}
                    (None, Some(c)) => {
                        let lv = rank_in(side, &self.sides[si].levels, px);
                        rows.push(self.depth_row(b, side, px, None, Some(c), l2flags::INTO_VIEW | extra, lv));
                        self.shown_map[si].insert(px, c);
                    }
                    (Some(p), None) => {
                        rows.push(self.depth_row(b, side, px, Some(p), None, extra, 255));
                        self.shown_map[si].remove(&px);
                    }
                    (Some(p), Some(c)) => {
                        if self.differs(p, c) {
                            let lv = rank_in(side, &self.sides[si].levels, px);
                            rows.push(self.depth_row(b, side, px, Some(p), Some(c), extra, lv));
                        }
                        self.shown_map[si].insert(px, c);
                    }
                }
            }
            dirty.clear();
            self.dirty[si] = dirty;
        } else {
            // Changes strictly below a full view cannot change the top N.
            let full_and_below = self.shown_vec[si].len() == n
                && self.shown_vec[si]
                    .last()
                    .is_some_and(|w| self.dirty[si].iter().all(|p| better(side, w.0, *p)));
            self.dirty[si].clear();
            if full_and_below {
                return;
            }
            let mut view = std::mem::take(&mut self.view_buf);
            top_into(side, &self.sides[si].levels, n, &mut view);
            for (rank, &(px, q, c)) in view.iter().enumerate() {
                match self.shown_vec[si].iter().find(|s| s.0 == px) {
                    None => rows.push(self.depth_row(
                        b,
                        side,
                        px,
                        None,
                        Some((q, c)),
                        l2flags::INTO_VIEW | extra,
                        rank as u8,
                    )),
                    Some(&(_, pq, pc)) => {
                        if self.differs((pq, pc), (q, c)) {
                            rows.push(self.depth_row(b, side, px, Some((pq, pc)), Some((q, c)), extra, rank as u8));
                        }
                    }
                }
            }
            for &(px, pq, pc) in &self.shown_vec[si] {
                if !view.iter().any(|v| v.0 == px) {
                    let still = self.sides[si].levels.contains_key(&px);
                    let f = if still { l2flags::OUT_OF_VIEW } else { 0 };
                    rows.push(self.depth_row(b, side, px, Some((pq, pc)), None, f | extra, 255));
                }
            }
            std::mem::swap(&mut self.shown_vec[si], &mut view);
            self.view_buf = view;
        }
        // Best first.
        match side {
            BookSide::Bid => rows[start..].sort_unstable_by_key(|r| std::cmp::Reverse(r.price)),
            BookSide::Ask => rows[start..].sort_unstable_by_key(|r| r.price),
        }
    }

    fn snapshot_rows(&mut self, b: (Ts, Ts, u64), rows: &mut Vec<L2Row>) {
        rows.push(self.base_row(b.0, b.1, b.2, L2Kind::Clear));
        let n = self.view_n();
        for side in SIDES {
            let si = idx(side);
            let mut view = std::mem::take(&mut self.view_buf);
            top_into(side, &self.sides[si].levels, n, &mut view);
            for (rank, &(px, q, c)) in view.iter().enumerate() {
                let mut r = self.base_row(b.0, b.1, b.2, L2Kind::Snapshot);
                r.side = side.to_side();
                r.price = Some(px);
                r.prev_ct = self.ct(0);
                r.new_qty = q;
                r.new_ct = self.ct(c);
                r.level = rank.min(254) as u8;
                r.flags = l2flags::SNAPSHOT;
                rows.push(r);
            }
            if n == 0 {
                self.shown_map[si] = self.sides[si].levels.clone();
                self.view_buf = view;
            } else {
                std::mem::swap(&mut self.shown_vec[si], &mut view);
                self.view_buf = view;
            }
            self.dirty[si].clear();
        }
    }

    fn trade_level(&self, bs: BookSide, px: Px) -> u8 {
        let si = idx(bs);
        if self.view_n() == 0 {
            if self.shown_map[si].contains_key(&px) {
                rank_in(bs, &self.shown_map[si], px)
            } else {
                255
            }
        } else {
            self.shown_vec[si]
                .iter()
                .position(|s| s.0 == px)
                .map_or(255, |i| i as u8)
        }
    }

    fn emit(&mut self, ts: Ts, ts_recv: Ts, seq: u64, out: &mut Vec<L2Row>) {
        self.event_id += 1;
        let start = out.len();
        let b = (ts, ts_recv, seq);
        let snapshot = self.first_batch || self.had_clear;
        let conflated = if self.cfg.cadence_ns > 0 { l2flags::CONFLATED } else { 0 };
        let eid = self.event_id;
        let trades_first = !snapshot && self.cfg.trade_position == TradePosition::Before;
        if trades_first {
            out.extend(self.trades.drain(..).map(|mut t| {
                t.event_id = eid;
                t
            }));
        }
        let depth_start = out.len();
        if snapshot {
            self.snapshot_rows(b, out);
        } else {
            for side in SIDES {
                self.depth_rows(side, b, conflated, out);
            }
        }
        let depth_end = out.len();
        if !trades_first {
            out.extend(self.trades.drain(..).map(|mut t| {
                t.event_id = eid;
                t
            }));
        }
        // Trade-row levels: rank of the hit price in the new view.
        let trade_ranges = [start..depth_start, depth_end..out.len()];
        for range in trade_ranges {
            for k in range {
                let r = out[k];
                if let (L2Kind::Trade, Some(bs), Some(px)) = (r.kind, BookSide::from_side(r.side), r.price) {
                    out[k].level = self.trade_level(bs, px);
                }
            }
        }
        if out.len() > start {
            if let Some(last) = out.last_mut() {
                last.flags |= l2flags::LAST_IN_BATCH;
            }
        } else {
            self.event_id -= 1;
        }
        self.first_batch = false;
        self.had_clear = false;
        self.pending = false;
        self.last_emit = Some(ts);
    }
}

/// End of the cadence bucket that contains `prev` (`(floor(prev / cadence) + 1) * cadence`).
pub fn bucket_end(prev: Ts, cadence: i64) -> Ts {
    prev.div_euclid(cadence).saturating_add(1).saturating_mul(cadence)
}
