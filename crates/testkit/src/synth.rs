//! Seeded synthetic MBO generator with its own positional reference book (BTreeMap levels of FIFO deques), written
//! independently of `qt_book`. Each instrument gets its own random stream (`stable_seed(seed, ["synth",
//! instrument_id LE, session LE])`), its own `channel_id` (instrument_id - 1) and its own contiguous `sequence`
//! counter, so every partition and the merged stream are gap-free. Events of all instruments are merged by
//! `ts_event` (ties by instrument, then event order).
//!
//! Venue encodings (one `F_LAST` batch = one event):
//! - `cme`: `R` + `A`s flagged `SNAPSHOT` at the start (and on recovery clears), one `sequence` per event; cancels
//!   are full `C`; size reductions, size increases, price changes and same-price same-size refreshes (priority lost,
//!   audit ruling A6) are `M`; an aggressor sweep is one event with a
//!   `T` per price level followed by `F`/`C` per resting order (interleaved or all `F` then all `C`), optionally the
//!   aggressor's remainder `A` resting at the last swept price.
//! - `nasdaq`: one ITCH-like message per event with its own `sequence`: `A`; `C` partial (X) or full (D); replace
//!   (U) = `C` old + `A` new id; each execution is its own event `T`, `F`, `C` at one timestamp (non-printable
//!   executions omit `T`; rare off-price executions put the `F` one tick better than the resting price); hidden
//!   prints are `T` side `N` without `F`/`C`; the book is built by adds (no snapshot, no `R`, no `M`).
//! - `generic` (and `coinbase`): partial `C`, `M` of every kind (incl. size 0 = delete); a sweep is one event with
//!   `T`, `F`, `C` per fill sharing a `sequence` (fills pair by sequence); `T` side may be `N` (no aggressor
//!   guarantee); snapshot clears are `R` + flagged adds or flagged re-adds without `R`.
//!
//! Prices: adds join the touch or rest 1..max_levels-1 ticks behind it; when the spread is two ticks or wider an
//! add improves it with probability rising with the spread. Sweeps: 45% part of the touch level, 17% exactly the
//! front order, 23% exactly the touch level, 15% through several levels.
//!
//! Queue positions: without a `QueueMix` a cancel or partial cancel picks a uniformly random resting order; with one
//! (the committed fixtures use `QueueMix::REALISTIC`) the pick moves to the front / front half of its level and a
//! share of modifies become queue-jumping re-quotes (see `QueueMix`). Without a mix no extra random draw happens, so
//! streams are identical to the original generator.
//!
//! Action mix: `p_*` are relative weights per event; adds and cancels are rescaled by the book size so the book
//! hovers near `max_levels * 2 * 4` orders; a side that empties is refilled first. Event gaps: 8% zero (bursts at one
//! timestamp), 30% 1..50 us, the rest approximately exponential so that the mean is `mean_gap_ns`.

use std::collections::{BTreeMap, HashMap, VecDeque};

use qt_schema::rows::{BboRow, TradeKind, TradeRow};
use qt_types::{
    Action, BookSide, Dur, Instrument, MboEvent, Px, Qty, Side, Ts, UNDEF_PRICE, Venue, VenueRules, flags, rules,
    stable_seed,
};

use crate::calendar::{self, local_date, local_midnight};
use crate::dist::Rng;
use crate::{QueueMix, SynthConfig, SynthOutput};

/// Positional book of the generator.
#[derive(Clone, Debug, Default)]
pub(crate) struct GBook {
    bids: BTreeMap<Px, VecDeque<(u64, Qty)>>,
    asks: BTreeMap<Px, VecDeque<(u64, Qty)>>,
    loc: HashMap<u64, (BookSide, Px)>,
    ids: Vec<u64>,
    pos: HashMap<u64, usize>,
}

impl GBook {
    fn side(&self, s: BookSide) -> &BTreeMap<Px, VecDeque<(u64, Qty)>> {
        match s {
            BookSide::Bid => &self.bids,
            BookSide::Ask => &self.asks,
        }
    }

    fn side_mut(&mut self, s: BookSide) -> &mut BTreeMap<Px, VecDeque<(u64, Qty)>> {
        match s {
            BookSide::Bid => &mut self.bids,
            BookSide::Ask => &mut self.asks,
        }
    }

    pub(crate) fn len(&self) -> usize {
        self.ids.len()
    }

    pub(crate) fn orders_on(&self, s: BookSide) -> usize {
        self.side(s).values().map(VecDeque::len).sum()
    }

    pub(crate) fn add(&mut self, side: BookSide, px: Px, id: u64, size: Qty) {
        self.side_mut(side).entry(px).or_default().push_back((id, size));
        self.loc.insert(id, (side, px));
        self.pos.insert(id, self.ids.len());
        self.ids.push(id);
    }

    pub(crate) fn get(&self, id: u64) -> Option<(BookSide, Px, Qty)> {
        let &(side, px) = self.loc.get(&id)?;
        let size = self.side(side).get(&px)?.iter().find(|(o, _)| *o == id)?.1;
        Some((side, px, size))
    }

    pub(crate) fn remove(&mut self, id: u64) -> Option<(BookSide, Px, Qty)> {
        let (side, px) = self.loc.remove(&id)?;
        let levels = self.side_mut(side);
        let level = levels.get_mut(&px)?;
        let i = level.iter().position(|(o, _)| *o == id)?;
        let (_, size) = level.remove(i)?;
        if level.is_empty() {
            levels.remove(&px);
        }
        if let Some(p) = self.pos.remove(&id) {
            self.ids.swap_remove(p);
            if let Some(&moved) = self.ids.get(p) {
                self.pos.insert(moved, p);
            }
        }
        Some((side, px, size))
    }

    pub(crate) fn set_size(&mut self, id: u64, size: Qty) {
        if let Some(&(side, px)) = self.loc.get(&id)
            && let Some(level) = self.side_mut(side).get_mut(&px)
            && let Some(o) = level.iter_mut().find(|(o, _)| *o == id)
        {
            o.1 = size;
        }
    }

    pub(crate) fn best_px(&self, s: BookSide) -> Option<Px> {
        match s {
            BookSide::Bid => self.bids.keys().next_back().copied(),
            BookSide::Ask => self.asks.keys().next().copied(),
        }
    }

    pub(crate) fn level_total(&self, s: BookSide, px: Px) -> (Qty, u32) {
        self.side(s)
            .get(&px)
            .map_or((0, 0), |l| (l.iter().map(|o| o.1).sum(), l.len() as u32))
    }

    pub(crate) fn best(&self, s: BookSide) -> Option<(Px, Qty, u32)> {
        self.best_px(s).map(|p| {
            let (t, c) = self.level_total(s, p);
            (p, t, c)
        })
    }

    pub(crate) fn prices_best_first(&self, s: BookSide) -> Vec<Px> {
        match s {
            BookSide::Bid => self.bids.keys().rev().copied().collect(),
            BookSide::Ask => self.asks.keys().copied().collect(),
        }
    }

    pub(crate) fn level_orders(&self, s: BookSide, px: Px) -> Vec<(u64, Qty)> {
        self.side(s)
            .get(&px)
            .map(|l| l.iter().copied().collect())
            .unwrap_or_default()
    }

    pub(crate) fn n_levels(&self, s: BookSide) -> usize {
        self.side(s).len()
    }

    pub(crate) fn has_level(&self, s: BookSide, px: Px) -> bool {
        self.side(s).contains_key(&px)
    }

    pub(crate) fn random_id(&self, rng: &mut Rng) -> Option<u64> {
        if self.ids.is_empty() {
            None
        } else {
            Some(self.ids[rng.index(self.ids.len())])
        }
    }

    /// Every order in snapshot order: bids best first, then asks best first, FIFO inside a level.
    pub(crate) fn snapshot_orders(&self) -> Vec<(BookSide, Px, u64, Qty)> {
        let mut out = Vec::with_capacity(self.len());
        for s in [BookSide::Bid, BookSide::Ask] {
            for p in self.prices_best_first(s) {
                for (id, sz) in self.level_orders(s, p) {
                    out.push((s, p, id, sz));
                }
            }
        }
        out
    }

    /// `(instrument, side, price, total, count)` rows, bids best first then asks best first.
    pub(crate) fn depth_rows(&self, inst: u32) -> Vec<(u32, BookSide, Px, Qty, u32)> {
        let mut out = Vec::new();
        for s in [BookSide::Bid, BookSide::Ask] {
            for p in self.prices_best_first(s) {
                let (t, c) = self.level_total(s, p);
                out.push((inst, s, p, t, c));
            }
        }
        out
    }
}

/// One generated event with its expectations.
struct GenEvent {
    recs: Vec<MboEvent>,
    bbo: BboRow,
    trades: Vec<TradeRow>,
}

struct InstGen<'a> {
    cfg: &'a SynthConfig,
    venue: Venue,
    rules: VenueRules,
    inst: u32,
    chan: u16,
    publisher: u16,
    rng: Rng,
    book: GBook,
    ts: Ts,
    ts_recv: Ts,
    seq: u64,
    next_id: u64,
    tick: Px,
    ref_px: Px,
    out: Vec<GenEvent>,
    target: u64,
    mix: Option<QueueMix>,
}

fn bside(s: BookSide) -> u8 {
    s.as_byte()
}

impl<'a> InstGen<'a> {
    fn new(cfg: &'a SynthConfig, mix: Option<QueueMix>, inst_idx: u32, session: u32, ts_start: Ts) -> Self {
        let inst = inst_idx + 1;
        let venue = cfg.venue;
        let seed = stable_seed(cfg.seed, &[b"synth", &inst.to_le_bytes(), &session.to_le_bytes()]);
        let mut rng = Rng::new(seed);
        let tick = cfg.tick.max(1);
        let seq = 1 + rng.range(0, 999_999);
        let ref_px = cfg.base_px.max(1_000 * tick) + i64::from(inst_idx) * 40 * tick;
        Self {
            cfg,
            venue,
            rules: rules(venue),
            inst,
            chan: u16::try_from(inst_idx).unwrap_or(u16::MAX),
            publisher: calendar::session_rules(venue).publisher_id,
            rng,
            book: GBook::default(),
            ts: ts_start,
            ts_recv: ts_start + 10_000,
            seq,
            next_id: (u64::from(session) + 1) * 1_000_000_000_000 + u64::from(inst) * 1_000_000_000,
            tick,
            ref_px,
            out: Vec::new(),
            target: u64::from(cfg.max_levels.max(1)) * 8,
            mix,
        }
    }

    fn remaining(&self) -> u64 {
        self.cfg.events_per_instrument.saturating_sub(self.out.len() as u64)
    }

    fn advance(&mut self) {
        let mean = self.cfg.mean_gap_ns.max(0) as f64;
        let u = self.rng.f64();
        let gap: Dur = if u < 0.08 {
            0
        } else if u < 0.38 {
            self.rng.range(1, 50_000) as i64
        } else {
            let m2 = ((mean - 0.3 * 25_000.0) / 0.62).max(0.0);
            self.rng.exp_ns(m2)
        };
        self.ts = self.ts.saturating_add(gap);
        self.ts_recv = self.ts.saturating_add(5_000 + self.rng.range(0, 20_000) as i64);
    }

    fn next_seq(&mut self) -> u64 {
        self.seq += 1;
        self.seq
    }

    fn new_id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }

    #[allow(clippy::too_many_arguments)]
    fn rec(&self, action: Action, side: u8, price: Px, size: Qty, order_id: u64, fl: u8, seq: u64) -> MboEvent {
        MboEvent {
            ts_event: self.ts,
            ts_recv: self.ts_recv,
            instrument_id: self.inst,
            action: action.as_byte(),
            side,
            price,
            size,
            order_id,
            flags: fl,
            sequence: seq,
            channel_id: self.chan,
            publisher_id: self.publisher,
        }
    }

    /// Records one event; the generator book must already reflect it.
    fn emit(&mut self, mut recs: Vec<MboEvent>) {
        let Some(last) = recs.last_mut() else { return };
        last.flags |= flags::LAST;
        let seq = last.sequence;
        let bid = self.book.best(BookSide::Bid);
        let ask = self.book.best(BookSide::Ask);
        let bbo = BboRow {
            ts_event: self.ts,
            instrument_id: self.inst,
            sequence: seq,
            bid_px: bid.map(|b| b.0),
            bid_sz: bid.map_or(0, |b| b.1),
            bid_ct: bid.map_or(0, |b| b.2),
            ask_px: ask.map(|a| a.0),
            ask_sz: ask.map_or(0, |a| a.1),
            ask_ct: ask.map_or(0, |a| a.2),
        };
        let trades = recs
            .iter()
            .filter(|r| r.action == b'T')
            .map(|r| trade_row(r, &self.rules))
            .collect();
        self.out.push(GenEvent { recs, bbo, trades });
    }

    fn run(&mut self) {
        let n = self.cfg.events_per_instrument;
        if n == 0 {
            return;
        }
        if self.cfg.snapshot_at_start && self.venue != Venue::Nasdaq {
            self.seed_book();
            self.emit_snapshot(true);
        }
        while self.remaining() > 0 {
            let warm = self.book.len() < (self.target / 2) as usize && self.out.len() < (self.target as usize);
            if warm {
                self.gen_add(None);
            } else {
                self.step();
            }
        }
    }

    fn seed_book(&mut self) {
        let levels = self.cfg.max_levels.clamp(1, 8);
        for side in [BookSide::Bid, BookSide::Ask] {
            for k in 0..levels {
                if k > 0 && self.rng.chance(0.15) {
                    continue;
                }
                let off = i64::from(k + 1) * self.tick;
                let px = match side {
                    BookSide::Bid => self.ref_px - off,
                    BookSide::Ask => self.ref_px + off,
                };
                for _ in 0..=self.rng.range(0, 4) {
                    let id = self.new_id();
                    let sz = self.rng.size(self.cfg.mean_size);
                    self.book.add(side, px, id, sz);
                }
            }
        }
    }

    /// `R` (if `with_clear`) + every order as a flagged `A`, all flagged `SNAPSHOT`; the book is unchanged.
    fn emit_snapshot(&mut self, with_clear: bool) {
        let seq = self.next_seq();
        let mut recs = Vec::with_capacity(self.book.len() + 1);
        if with_clear {
            recs.push(self.rec(Action::Clear, b'N', UNDEF_PRICE, 0, 0, flags::SNAPSHOT, seq));
        }
        for (s, p, id, sz) in self.book.snapshot_orders() {
            recs.push(self.rec(Action::Add, bside(s), p, sz, id, flags::SNAPSHOT, seq));
        }
        if recs.is_empty() {
            recs.push(self.rec(Action::Clear, b'N', UNDEF_PRICE, 0, 0, flags::SNAPSHOT, seq));
        }
        self.emit(recs);
    }

    fn step(&mut self) {
        let n = self.book.len();
        let bid_n = self.book.orders_on(BookSide::Bid);
        let ask_n = self.book.orders_on(BookSide::Ask);
        if bid_n == 0 || ask_n == 0 || n < 4 {
            let forced = if bid_n == 0 {
                Some(BookSide::Bid)
            } else if ask_n == 0 {
                Some(BookSide::Ask)
            } else {
                None
            };
            self.gen_add(forced);
            return;
        }
        let c = self.cfg;
        let ratio = n as f64 / self.target as f64;
        let w = [
            c.p_add * (1.0 / ratio).clamp(0.25, 4.0),
            c.p_cancel * ratio.clamp(0.25, 4.0),
            c.p_reduce,
            c.p_modify,
            c.p_trade,
            if self.venue == Venue::Nasdaq { 0.0 } else { c.p_clear },
            if self.rules.supports_hidden_orders {
                c.hidden_print_rate
            } else {
                0.0
            },
        ];
        let done = match self.rng.weighted(&w) {
            1 => self.gen_cancel(),
            2 => self.gen_reduce(),
            3 => self.gen_modify(),
            4 => self.gen_trade(),
            5 => self.gen_clear(),
            6 => self.gen_hidden(),
            _ => false,
        };
        if !done {
            self.gen_add(None);
        }
    }

    fn level_draw(&mut self) -> i64 {
        let u = self.rng.f64();
        let max = i64::from(self.cfg.max_levels.max(1));
        let k = if u < 0.40 {
            0
        } else if u < 0.62 {
            1
        } else if u < 0.76 {
            2
        } else if u < 0.85 {
            3
        } else {
            self.rng.range(4, (max - 1).max(4) as u64) as i64
        };
        k.min(max - 1)
    }

    /// A resting price for `side` that never locks or crosses the opposite best.
    fn passive_price(&mut self, side: BookSide) -> Px {
        let t = self.tick;
        let same = self.book.best_px(side);
        let opp = self.book.best_px(side.opposite());
        let sign = match side {
            BookSide::Bid => 1,
            BookSide::Ask => -1,
        };
        let px = match (same, opp) {
            (Some(b), Some(a)) => {
                let spread_ticks = (a - b).abs() / t;
                // A wide spread attracts improving orders (most often one tick better).
                let p_improve = (0.35 + 0.15 * (spread_ticks - 2) as f64).min(0.9);
                if spread_ticks >= 2 && self.rng.chance(p_improve) {
                    let j = if self.rng.chance(0.7) {
                        1
                    } else {
                        self.rng.range(1, (spread_ticks - 1) as u64) as i64
                    };
                    b + sign * j * t
                } else {
                    let k = self.level_draw();
                    b - sign * k * t
                }
            }
            (Some(b), None) => {
                let k = self.level_draw();
                b - sign * k * t
            }
            (None, Some(a)) => {
                let k = self.rng.range(0, 2) as i64;
                a - sign * (1 + k) * t
            }
            (None, None) => {
                let k = self.rng.range(0, 2) as i64;
                self.ref_px - sign * (1 + k) * t
            }
        };
        let px = px.max(t);
        if !self.book.has_level(side, px) && self.book.n_levels(side) >= self.cfg.max_levels.max(1) as usize {
            return same.unwrap_or(px);
        }
        px
    }

    fn gen_add(&mut self, forced: Option<BookSide>) {
        let side = forced.unwrap_or_else(|| {
            let nb = self.book.orders_on(BookSide::Bid) as f64;
            let na = self.book.orders_on(BookSide::Ask) as f64;
            let p_bid = (0.5 + 0.25 * (na - nb) / (na + nb + 1.0)).clamp(0.2, 0.8);
            if self.rng.chance(p_bid) {
                BookSide::Bid
            } else {
                BookSide::Ask
            }
        });
        let px = self.passive_price(side);
        let size = self.rng.size(self.cfg.mean_size);
        let id = self.new_id();
        self.advance();
        let seq = self.next_seq();
        self.book.add(side, px, id, size);
        let r = self.rec(Action::Add, bside(side), px, size, id, 0, seq);
        self.emit(vec![r]);
    }

    /// With a `QueueMix`: moves a picked order to a front / front-half / uniform position of its own level.
    fn mixed_position(&mut self, id: u64) -> u64 {
        let Some(m) = self.mix else {
            return id;
        };
        let Some((side, px, _)) = self.book.get(id) else {
            return id;
        };
        let q = self.book.level_orders(side, px);
        let u = self.rng.f64();
        let pos = if u < m.front {
            0
        } else if u < m.front + m.middle {
            self.rng.index(q.len().div_ceil(2))
        } else {
            self.rng.index(q.len())
        };
        q.get(pos).map_or(id, |o| o.0)
    }

    /// Queue-jumping re-quote (`QueueMix::jump`): an order from the back half of a best level moves one tick better
    /// inside a spread of at least two ticks (Nasdaq: replace with a new id; elsewhere an `M`, priority lost).
    fn gen_jump(&mut self) -> bool {
        let side = if self.rng.chance(0.5) {
            BookSide::Bid
        } else {
            BookSide::Ask
        };
        let (Some((best, _, n)), Some(opp)) = (self.book.best(side), self.book.best_px(side.opposite())) else {
            return false;
        };
        let t = self.tick;
        let new_px = match side {
            BookSide::Bid => best + t,
            BookSide::Ask => best - t,
        };
        let crosses = match side {
            BookSide::Bid => new_px >= opp,
            BookSide::Ask => new_px <= opp,
        };
        if crosses || n < 2 {
            return false;
        }
        let q = self.book.level_orders(side, best);
        let half = q.len() / 2;
        let (id, size) = q[half + self.rng.index(q.len() - half)];
        self.advance();
        let seq = self.next_seq();
        if self.venue == Venue::Nasdaq {
            let new_id = self.new_id();
            let c = self.rec(Action::Cancel, bside(side), best, size, id, 0, seq);
            let a = self.rec(Action::Add, bside(side), new_px, size, new_id, 0, seq);
            self.book.remove(id);
            self.book.add(side, new_px, new_id, size);
            self.emit(vec![c, a]);
        } else {
            let r = self.rec(Action::Modify, bside(side), new_px, size, id, 0, seq);
            self.book.remove(id);
            self.book.add(side, new_px, id, size);
            self.emit(vec![r]);
        }
        true
    }

    fn gen_cancel(&mut self) -> bool {
        let Some(id) = self.book.random_id(&mut self.rng) else {
            return false;
        };
        let id = self.mixed_position(id);
        let Some((side, px, size)) = self.book.get(id) else {
            return false;
        };
        self.advance();
        let seq = self.next_seq();
        let r = if self.rules.modify_records_present && self.venue != Venue::Cme && self.rng.chance(0.1) {
            self.rec(Action::Modify, bside(side), px, 0, id, 0, seq)
        } else {
            self.rec(Action::Cancel, bside(side), px, size, id, 0, seq)
        };
        self.book.remove(id);
        self.emit(vec![r]);
        true
    }

    fn pick_reducible(&mut self) -> Option<(u64, BookSide, Px, Qty)> {
        for _ in 0..8 {
            let id = self.book.random_id(&mut self.rng)?;
            let id = self.mixed_position(id);
            let (s, p, sz) = self.book.get(id)?;
            if sz >= 2 {
                return Some((id, s, p, sz));
            }
        }
        None
    }

    fn gen_reduce(&mut self) -> bool {
        let Some((id, side, px, size)) = self.pick_reducible() else {
            return false;
        };
        let by = self.rng.range(1, size - 1);
        let new = size - by;
        self.advance();
        let seq = self.next_seq();
        let as_modify = match self.venue {
            Venue::Nasdaq => false,
            Venue::Cme => true,
            Venue::Generic | Venue::Coinbase => self.rng.chance(0.5),
        };
        let r = if as_modify {
            self.rec(Action::Modify, bside(side), px, new, id, 0, seq)
        } else {
            self.rec(Action::Cancel, bside(side), px, by, id, 0, seq)
        };
        self.book.set_size(id, new);
        self.emit(vec![r]);
        true
    }

    /// A new non-crossing price 1-2 ticks away from `px` on the same side.
    fn moved_price(&mut self, side: BookSide, px: Px) -> Px {
        let t = self.tick;
        let d = self.rng.range(1, 2) as i64 * t;
        let mut cand = if self.rng.chance(0.5) { px + d } else { px - d };
        match side {
            BookSide::Bid => {
                if let Some(a) = self.book.best_px(BookSide::Ask)
                    && cand >= a
                {
                    cand = a - t;
                }
                if cand == px {
                    cand = px - t;
                }
            }
            BookSide::Ask => {
                if let Some(b) = self.book.best_px(BookSide::Bid)
                    && cand <= b
                {
                    cand = b + t;
                }
                if cand == px {
                    cand = px + t;
                }
            }
        }
        cand.max(t)
    }

    fn gen_modify(&mut self) -> bool {
        if let Some(m) = self.mix
            && self.rng.chance(m.jump)
            && self.gen_jump()
        {
            return true;
        }
        let Some(id) = self.book.random_id(&mut self.rng) else {
            return false;
        };
        let Some((side, px, size)) = self.book.get(id) else {
            return false;
        };
        if self.venue == Venue::Nasdaq {
            let new_px = if self.rng.chance(0.5) {
                px
            } else {
                self.moved_price(side, px)
            };
            let new_size = self.rng.size(self.cfg.mean_size);
            let new_id = self.new_id();
            self.advance();
            let seq = self.next_seq();
            let c = self.rec(Action::Cancel, bside(side), px, size, id, 0, seq);
            let a = self.rec(Action::Add, bside(side), new_px, new_size, new_id, 0, seq);
            self.book.remove(id);
            self.book.add(side, new_px, new_id, new_size);
            self.emit(vec![c, a]);
            return true;
        }
        let u = self.rng.f64();
        let (new_px, new_size) = if u < 0.40 {
            let p = self.moved_price(side, px);
            let s = if self.rng.chance(0.3) {
                self.rng.size(self.cfg.mean_size)
            } else {
                size
            };
            (p, s)
        } else if u < 0.70 || (u < 0.95 && size < 2) {
            (px, size + self.rng.size(self.cfg.mean_size))
        } else if u < 0.95 {
            (px, self.rng.range(1, size - 1))
        } else {
            (px, size)
        };
        self.advance();
        let seq = self.next_seq();
        let r = self.rec(Action::Modify, bside(side), new_px, new_size, id, 0, seq);
        if crate::refbook::keeps_priority(&self.rules, px, size, new_px, new_size) {
            self.book.set_size(id, new_size);
        } else {
            self.book.remove(id);
            self.book.add(side, new_px, id, new_size);
        }
        self.emit(vec![r]);
        true
    }

    /// Sweep quantity: partial touch, exactly the front order, exactly the touch level, or through several levels.
    fn sweep_qty(&mut self, resting: BookSide) -> Qty {
        let prices = self.book.prices_best_first(resting);
        let Some(&p0) = prices.first() else { return 0 };
        let touch = self.book.level_total(resting, p0).0;
        let front = self.book.level_orders(resting, p0).first().map_or(1, |o| o.1);
        let depth: Qty = prices.iter().map(|&p| self.book.level_total(resting, p).0).sum();
        let u = self.rng.f64();
        let q = if u < 0.45 {
            self.rng.range(1, touch.saturating_sub(1).max(1))
        } else if u < 0.62 {
            front
        } else if u < 0.85 {
            touch
        } else {
            let beyond = depth - touch;
            let extra_cap = beyond.min(3 * self.cfg.mean_size.max(1));
            touch + if extra_cap > 0 { self.rng.range(1, extra_cap) } else { 0 }
        };
        q.clamp(1, depth)
    }

    fn gen_trade(&mut self) -> bool {
        let aggr = if self.rng.chance(0.5) {
            BookSide::Bid
        } else {
            BookSide::Ask
        };
        let resting = aggr.opposite();
        let q = self.sweep_qty(resting);
        if q == 0 {
            return false;
        }
        let max_fills = if self.venue == Venue::Nasdaq {
            self.remaining().saturating_sub(1).max(1) as usize
        } else {
            usize::MAX
        };
        // Walk the FIFO: (price, id, take, size_before).
        let mut fills: Vec<(Px, u64, Qty, Qty)> = Vec::new();
        let mut left = q;
        let mut last_level_emptied = false;
        'outer: for p in self.book.prices_best_first(resting) {
            let orders = self.book.level_orders(resting, p);
            let mut taken_all = true;
            for (id, sz) in orders {
                if left == 0 || fills.len() >= max_fills {
                    taken_all = false;
                    break;
                }
                let take = sz.min(left);
                fills.push((p, id, take, sz));
                left -= take;
                if take < sz {
                    taken_all = false;
                }
            }
            last_level_emptied = taken_all;
            if left == 0 || fills.len() >= max_fills {
                break 'outer;
            }
        }
        if fills.is_empty() {
            return false;
        }
        let last_px = fills[fills.len() - 1].0;
        let budget_for_rem = self.venue != Venue::Nasdaq || self.remaining() > fills.len() as u64;
        let remainder = if last_level_emptied && budget_for_rem && self.rng.chance(0.25) {
            self.rng.size(self.cfg.mean_size)
        } else {
            0
        };
        self.advance();
        match self.venue {
            Venue::Cme => self.sweep_cme(aggr, &fills, last_px, remainder),
            Venue::Nasdaq => self.sweep_nasdaq(aggr, &fills, last_px, remainder),
            Venue::Generic | Venue::Coinbase => self.sweep_generic(aggr, &fills, last_px, remainder),
        }
        true
    }

    fn apply_fill(&mut self, id: u64, take: Qty, size_before: Qty) {
        if take >= size_before {
            self.book.remove(id);
        } else {
            self.book.set_size(id, size_before - take);
        }
    }

    fn sweep_cme(&mut self, aggr: BookSide, fills: &[(Px, u64, Qty, Qty)], last_px: Px, remainder: Qty) {
        let resting = aggr.opposite();
        let seq = self.next_seq();
        let interleave = self.rng.chance(0.5);
        let mut recs = Vec::new();
        let mut i = 0;
        while i < fills.len() {
            let p = fills[i].0;
            let j = fills[i..].iter().position(|f| f.0 != p).map_or(fills.len(), |k| i + k);
            let level_qty: Qty = fills[i..j].iter().map(|f| f.2).sum();
            recs.push(self.rec(Action::Trade, bside(aggr), p, level_qty, 0, 0, seq));
            if interleave {
                for &(p, id, take, _) in &fills[i..j] {
                    recs.push(self.rec(Action::Fill, bside(resting), p, take, id, 0, seq));
                    recs.push(self.rec(Action::Cancel, bside(resting), p, take, id, 0, seq));
                }
            } else {
                for &(p, id, take, _) in &fills[i..j] {
                    recs.push(self.rec(Action::Fill, bside(resting), p, take, id, 0, seq));
                }
                for &(p, id, take, _) in &fills[i..j] {
                    recs.push(self.rec(Action::Cancel, bside(resting), p, take, id, 0, seq));
                }
            }
            i = j;
        }
        for &(_, id, take, before) in fills {
            self.apply_fill(id, take, before);
        }
        if remainder > 0 {
            let id = self.new_id();
            recs.push(self.rec(Action::Add, bside(aggr), last_px, remainder, id, 0, seq));
            self.book.add(aggr, last_px, id, remainder);
        }
        self.emit(recs);
    }

    fn sweep_generic(&mut self, aggr: BookSide, fills: &[(Px, u64, Qty, Qty)], last_px: Px, remainder: Qty) {
        let resting = aggr.opposite();
        let mut recs = Vec::new();
        for &(p, id, take, _) in fills {
            let seq = self.next_seq();
            let tside = if !self.rules.trades_have_aggressor && self.rng.chance(0.05) {
                b'N'
            } else {
                bside(aggr)
            };
            recs.push(self.rec(Action::Trade, tside, p, take, 0, 0, seq));
            recs.push(self.rec(Action::Fill, bside(resting), p, take, id, 0, seq));
            recs.push(self.rec(Action::Cancel, bside(resting), p, take, id, 0, seq));
        }
        for &(_, id, take, before) in fills {
            self.apply_fill(id, take, before);
        }
        if remainder > 0 {
            let id = self.new_id();
            let seq = self.next_seq();
            recs.push(self.rec(Action::Add, bside(aggr), last_px, remainder, id, 0, seq));
            self.book.add(aggr, last_px, id, remainder);
        }
        self.emit(recs);
    }

    fn sweep_nasdaq(&mut self, aggr: BookSide, fills: &[(Px, u64, Qty, Qty)], last_px: Px, remainder: Qty) {
        let resting = aggr.opposite();
        for &(p, id, take, before) in fills {
            let seq = self.next_seq();
            let u = self.rng.f64();
            let mut recs = Vec::with_capacity(3);
            if u < 0.003 {
                let better = match resting {
                    BookSide::Bid => p - self.tick,
                    BookSide::Ask => p + self.tick,
                };
                recs.push(self.rec(Action::Fill, bside(resting), better, take, id, 0, seq));
            } else if u < 0.013 {
                recs.push(self.rec(Action::Fill, bside(resting), p, take, id, 0, seq));
            } else {
                recs.push(self.rec(Action::Trade, bside(aggr), p, take, 0, 0, seq));
                recs.push(self.rec(Action::Fill, bside(resting), p, take, id, 0, seq));
            }
            recs.push(self.rec(Action::Cancel, bside(resting), p, take, id, 0, seq));
            self.apply_fill(id, take, before);
            self.emit(recs);
        }
        if remainder > 0 && self.remaining() > 0 {
            let id = self.new_id();
            let seq = self.next_seq();
            self.book.add(aggr, last_px, id, remainder);
            let r = self.rec(Action::Add, bside(aggr), last_px, remainder, id, 0, seq);
            self.emit(vec![r]);
        }
    }

    fn gen_clear(&mut self) -> bool {
        if self.venue == Venue::Nasdaq {
            return false;
        }
        self.advance();
        let with_clear = !self.rules.snapshot_records_flagged || self.venue == Venue::Cme || self.rng.chance(0.5);
        self.emit_snapshot(with_clear);
        true
    }

    fn gen_hidden(&mut self) -> bool {
        let (Some(b), Some(a)) = (self.book.best_px(BookSide::Bid), self.book.best_px(BookSide::Ask)) else {
            return false;
        };
        let n = ((a - b) / self.tick).max(0) as u64;
        let px = b + self.rng.range(0, n) as i64 * self.tick;
        let size = self.rng.size(self.cfg.mean_size);
        self.advance();
        let seq = self.next_seq();
        let r = self.rec(Action::Trade, b'N', px, size, 0, 0, seq);
        self.emit(vec![r]);
        true
    }
}

/// The derived-trade row of a `T` record (hidden = side `N` on a venue with hidden orders).
pub(crate) fn trade_row(r: &MboEvent, rules: &VenueRules) -> TradeRow {
    let side = Side::from_byte(r.side);
    TradeRow {
        ts_event: r.ts_event,
        instrument_id: r.instrument_id,
        sequence: r.sequence,
        price: r.price,
        size: r.size,
        aggressor_side: side,
        kind: if side == Side::None && rules.supports_hidden_orders {
            TradeKind::Hidden
        } else {
            TradeKind::Displayed
        },
        maker_order_id: None,
    }
}

/// Symbol of the `k`-th synthetic instrument of a venue.
pub(crate) fn symbol(venue: Venue, k: u32) -> String {
    let letter = char::from(b'A' + (k % 26) as u8);
    match venue {
        Venue::Cme => format!("QT{letter}U6"),
        Venue::Nasdaq => format!("QTS{letter}"),
        Venue::Generic | Venue::Coinbase => format!("QT{letter}-USD"),
    }
}

/// Generates one session starting at `ts_start` (session index for seeds and order ids).
pub(crate) fn generate_session(cfg: &SynthConfig, mix: Option<QueueMix>, session: u32, ts_start: Ts) -> SynthOutput {
    let mut per_inst: Vec<Vec<GenEvent>> = Vec::new();
    let mut depth = Vec::new();
    let mut instruments: Vec<Instrument> = Vec::new();
    for k in 0..cfg.instruments {
        let mut g = InstGen::new(cfg, mix, k, session, ts_start);
        g.run();
        depth.extend(g.book.depth_rows(g.inst));
        instruments.push(calendar::instrument_for(
            cfg.venue,
            g.inst,
            &symbol(cfg.venue, k),
            ts_start,
            g.tick,
            1,
        ));
        per_inst.push(g.out);
    }
    let mut order: Vec<(Ts, usize, usize)> = Vec::new();
    for (k, evs) in per_inst.iter().enumerate() {
        for (i, ev) in evs.iter().enumerate() {
            order.push((ev.recs[0].ts_event, k, i));
        }
    }
    order.sort_unstable();
    let mut out = SynthOutput {
        events: Vec::new(),
        instruments,
        expected_bbo: Vec::with_capacity(order.len()),
        expected_trades: Vec::new(),
        expected_depth_final: depth,
    };
    for (_, k, i) in order {
        let ev = &per_inst[k][i];
        out.events.extend_from_slice(&ev.recs);
        out.expected_bbo.push(ev.bbo);
        out.expected_trades.extend_from_slice(&ev.trades);
    }
    out
}

/// Session `k` start: same offset from local midnight as `ts_start`, `k` local days later.
pub(crate) fn session_start(venue: Venue, ts_start: Ts, k: u32) -> Ts {
    let d0 = local_date(venue, ts_start);
    let off = ts_start - local_midnight(venue, d0);
    local_midnight(venue, d0 + k as i32) + off
}
