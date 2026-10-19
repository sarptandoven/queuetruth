//! Local test helpers (not qt_testkit): a scenario builder for hand-written events, a random but venue-consistent
//! market generator with its own FIFO book, random shadows, and an in-memory engine runner.

#![allow(dead_code)]

use std::collections::HashMap;

use qt_replay::sinks::VecSink;
use qt_replay::{Engine, EngineConfig, EngineReport};
use qt_schema::rows::{PriceSpec, ShadowAmend, ShadowSource, ShadowSpec};
use qt_types::{BookSide, Instrument, MboEvent, Venue, Xoshiro256StarStar, flags, rules};

pub const INST: u32 = 1;

/// Instrument 1: tick 1, midnight 0, open 0 s, close 86,400 s.
pub fn instrument(venue: Venue) -> Instrument {
    Instrument {
        instrument_id: INST,
        session_date: 0,
        symbol: "TEST".into(),
        venue,
        source_venue: venue.name().into(),
        price_exp: -9,
        tick_size: 1,
        lot_size: 1,
        size_exp: 0,
        contract_multiplier: 1.0,
        currency: "USD".into(),
        session_tz: "UTC".into(),
        midnight_utc_ns: 0,
        open_s: 0,
        close_s: 86_400,
        source: "test".into(),
    }
}

/// Hand-built events: every record's `sequence` is its 1-based index in the stream, `ts_recv = ts_event`,
/// `LAST` on the last record of each event; `C`/`F` take side and price from the order they reference.
pub struct Scn {
    pub recs: Vec<MboEvent>,
    orders: HashMap<u64, (u8, i64)>,
}

pub struct Ev<'a> {
    s: &'a mut Scn,
    ts: i64,
}

impl Scn {
    pub fn new() -> Self {
        Self {
            recs: Vec::new(),
            orders: HashMap::new(),
        }
    }

    pub fn ev(mut self, ts: i64, f: impl FnOnce(&mut Ev<'_>)) -> Self {
        let start = self.recs.len();
        f(&mut Ev { s: &mut self, ts });
        if self.recs.len() > start {
            if let Some(last) = self.recs.last_mut() {
                last.flags |= flags::LAST;
            }
        }
        self
    }

    fn push(&mut self, ts: i64, action: u8, side: u8, price: i64, size: u64, order_id: u64) {
        let seq = self.recs.len() as u64 + 1;
        self.recs.push(MboEvent {
            ts_event: ts,
            ts_recv: ts,
            instrument_id: INST,
            action,
            side,
            price,
            size,
            order_id,
            sequence: seq,
            ..MboEvent::default()
        });
    }
}

impl Ev<'_> {
    pub fn add(&mut self, id: u64, side: u8, px: i64, qty: u64) -> &mut Self {
        self.s.orders.insert(id, (side, px));
        self.s.push(self.ts, b'A', side, px, qty, id);
        self
    }
    pub fn cancel(&mut self, id: u64, qty: u64) -> &mut Self {
        let (side, px) = self.s.orders.get(&id).copied().unwrap_or((b'N', qt_types::UNDEF_PRICE));
        self.s.push(self.ts, b'C', side, px, qty, id);
        self
    }
    pub fn modify(&mut self, id: u64, side: u8, px: i64, qty: u64) -> &mut Self {
        self.s.orders.insert(id, (side, px));
        self.s.push(self.ts, b'M', side, px, qty, id);
        self
    }
    pub fn trade(&mut self, aggr: u8, px: i64, qty: u64) -> &mut Self {
        self.s.push(self.ts, b'T', aggr, px, qty, 0);
        self
    }
    pub fn fill(&mut self, id: u64, qty: u64) -> &mut Self {
        let (side, px) = self.s.orders.get(&id).copied().unwrap_or((b'N', qt_types::UNDEF_PRICE));
        self.s.push(self.ts, b'F', side, px, qty, id);
        self
    }
    /// `F id q` + `C id q`.
    pub fn exec(&mut self, id: u64, qty: u64) -> &mut Self {
        self.fill(id, qty).cancel(id, qty)
    }
    pub fn clear(&mut self) -> &mut Self {
        self.s.push(self.ts, b'R', b'N', qt_types::UNDEF_PRICE, 0, 0);
        self
    }
}

pub fn abs_shadow(id: &str, side: BookSide, px: i64, qty: u64, ts_submit: i64, latency_ns: i64) -> ShadowSpec {
    ShadowSpec {
        shadow_id: id.into(),
        instrument_id: INST,
        side,
        price: PriceSpec::Abs(px),
        qty,
        ts_submit,
        latency_ns,
        ts_cancel: None,
        group: None,
        source: ShadowSource::User,
        order_key: None,
    }
}

/// Replays records through one engine into a `VecSink`.
pub fn run(
    cfg: EngineConfig,
    instruments: &[Instrument],
    events: &[MboEvent],
    shadows: Vec<ShadowSpec>,
    amends: Vec<ShadowAmend>,
) -> (VecSink, EngineReport) {
    let mut eng = Engine::new(cfg, instruments, shadows, amends).expect("engine");
    let mut sink = VecSink::default();
    for e in events {
        eng.apply(e, &mut sink).expect("apply");
    }
    let rep = eng.finish(&mut sink).expect("finish");
    (sink, rep)
}

/// Random but venue-consistent market: its own FIFO book keeps the book uncrossed, executions sweep FIFO from the
/// best level with venue-correct records (CME: `T` per level then `F`/`C` per maker in one event; Nasdaq: one
/// `T,F,C` message per maker; generic: like CME without aggressor sides), plus partial cancels, modifies (not
/// Nasdaq), hidden prints (Nasdaq/generic) and, when `clears`, an occasional `R` with a fresh book.
pub struct Market {
    r: Xoshiro256StarStar,
    venue: Venue,
    bids: Vec<(i64, Vec<(u64, u64)>)>,
    asks: Vec<(i64, Vec<(u64, u64)>)>,
    next_id: u64,
    seq: u64,
    ts: i64,
    pub out: Vec<MboEvent>,
    pub clears: bool,
}

impl Market {
    pub fn new(seed: u64, venue: Venue) -> Self {
        Self {
            r: Xoshiro256StarStar::new(seed),
            venue,
            bids: Vec::new(),
            asks: Vec::new(),
            next_id: 1,
            seq: 1,
            ts: 1_000_000_000,
            out: Vec::new(),
            clears: false,
        }
    }

    fn rnd(&mut self, n: u64) -> u64 {
        self.r.next_u64() % n.max(1)
    }

    fn rec(&mut self, action: u8, side: u8, price: i64, size: u64, order_id: u64) {
        self.out.push(MboEvent {
            ts_event: self.ts,
            ts_recv: self.ts + 7,
            instrument_id: INST,
            action,
            side,
            price,
            size,
            order_id,
            sequence: self.seq,
            ..MboEvent::default()
        });
    }

    fn end_event(&mut self) {
        if let Some(l) = self.out.last_mut() {
            if l.flags & flags::LAST == 0 {
                l.flags |= flags::LAST;
                self.seq += 1;
            }
        }
    }

    fn levels(&mut self, side: BookSide) -> &mut Vec<(i64, Vec<(u64, u64)>)> {
        match side {
            BookSide::Bid => &mut self.bids,
            BookSide::Ask => &mut self.asks,
        }
    }

    fn best(&self, side: BookSide) -> Option<i64> {
        match side {
            BookSide::Bid => self.bids.iter().map(|l| l.0).max(),
            BookSide::Ask => self.asks.iter().map(|l| l.0).min(),
        }
    }

    fn rest(&mut self, side: BookSide, px: i64, id: u64, q: u64) {
        let lv = self.levels(side);
        match lv.iter_mut().find(|l| l.0 == px) {
            Some(l) => l.1.push((id, q)),
            None => lv.push((px, vec![(id, q)])),
        }
    }

    fn random_order(&mut self) -> Option<(BookSide, i64, usize)> {
        let n: usize = self.bids.iter().chain(&self.asks).map(|l| l.1.len()).sum();
        if n == 0 {
            return None;
        }
        let mut k = self.rnd(n as u64) as usize;
        for side in [BookSide::Bid, BookSide::Ask] {
            let lv = self.levels(side).clone();
            for l in lv {
                if k < l.1.len() {
                    return Some((side, l.0, k));
                }
                k -= l.1.len();
            }
        }
        None
    }

    fn take(&mut self, side: BookSide, px: i64, pos: usize, q: u64) -> (u64, u64) {
        let lv = self.levels(side);
        let li = lv.iter().position(|l| l.0 == px).expect("level");
        let (id, size) = lv[li].1[pos];
        if q >= size {
            lv[li].1.remove(pos);
            if lv[li].1.is_empty() {
                lv.remove(li);
            }
            (id, size)
        } else {
            lv[li].1[pos].1 -= q;
            (id, q)
        }
    }

    fn add_price(&mut self, side: BookSide) -> i64 {
        let mid = 1_000;
        let off = self.rnd(6) as i64;
        match side {
            BookSide::Bid => {
                let cap = self.best(BookSide::Ask).map_or(mid, |a| a - 1);
                (mid - 1 - off).min(cap)
            }
            BookSide::Ask => {
                let floor = self.best(BookSide::Bid).map_or(mid, |b| b + 1);
                (mid + 1 + off).max(floor)
            }
        }
    }

    fn sb(side: BookSide) -> u8 {
        side.as_byte()
    }

    /// One event.
    pub fn step(&mut self) {
        self.ts += 1 + self.rnd(2_000_000) as i64;
        let k = self.rnd(100);
        let side = if self.rnd(2) == 0 { BookSide::Bid } else { BookSide::Ask };
        match k {
            0..=44 => {
                let px = self.add_price(side);
                let q = 1 + self.rnd(50);
                let id = self.next_id;
                self.next_id += 1;
                self.rest(side, px, id, q);
                self.rec(b'A', Self::sb(side), px, q, id);
                self.end_event();
            }
            45..=59 => {
                if let Some((s, px, pos)) = self.random_order() {
                    let (id, q) = self.take(s, px, pos, u64::MAX);
                    self.rec(b'C', Self::sb(s), px, q, id);
                    self.end_event();
                }
            }
            60..=69 => {
                if let Some((s, px, pos)) = self.random_order() {
                    let lv = self.levels(s).iter().find(|l| l.0 == px).expect("level").1[pos];
                    if lv.1 > 1 {
                        let by = 1 + self.rnd(lv.1 - 1);
                        let (id, _) = self.take(s, px, pos, by);
                        self.rec(b'C', Self::sb(s), px, by, id);
                        self.end_event();
                    }
                }
            }
            70..=74 if self.venue != Venue::Nasdaq => {
                if let Some((s, px, pos)) = self.random_order() {
                    let (id, size) = self.levels(s).iter().find(|l| l.0 == px).expect("level").1[pos];
                    let up = self.rnd(3);
                    let (npx, nq) = match up {
                        0 => (px, (size / 2).max(1)),
                        1 => (px, size + 1 + self.rnd(10)),
                        _ => (self.add_price(s), size),
                    };
                    // CME (audit ruling A6): only a strict decrease keeps priority.
                    let keeps = npx == px && (nq < size || (nq == size && self.venue != Venue::Cme));
                    if keeps {
                        let li = self.levels(s).iter().position(|l| l.0 == px).expect("level");
                        self.levels(s)[li].1[pos].1 = nq;
                    } else {
                        self.take(s, px, pos, u64::MAX);
                        self.rest(s, npx, id, nq);
                    }
                    self.rec(b'M', Self::sb(s), npx, nq, id);
                    self.end_event();
                }
            }
            75..=93 => self.execute(side),
            94..=96 if self.venue != Venue::Cme => {
                let px = 1_000 + self.rnd(5) as i64 - 2;
                let q = 1 + self.rnd(30);
                self.rec(b'T', b'N', px, q, 0);
                self.end_event();
            }
            97 if self.clears => {
                self.bids.clear();
                self.asks.clear();
                self.rec(b'R', b'N', qt_types::UNDEF_PRICE, 0, 0);
                for s in [BookSide::Bid, BookSide::Ask] {
                    for _ in 0..3 {
                        let px = self.add_price(s);
                        let id = self.next_id;
                        self.next_id += 1;
                        let q = 1 + self.rnd(40);
                        self.rest(s, px, id, q);
                        self.rec(b'A', Self::sb(s), px, q, id);
                    }
                }
                self.end_event();
            }
            _ => {
                let px = self.add_price(side);
                let id = self.next_id;
                self.next_id += 1;
                self.rest(side, px, id, 5);
                self.rec(b'A', Self::sb(side), px, 5, id);
                self.end_event();
            }
        }
    }

    /// An aggressor on `aggr` side sweeping the opposite book FIFO.
    fn execute(&mut self, aggr: BookSide) {
        let maker = aggr.opposite();
        let mut want = 1 + self.rnd(120);
        let aggr_byte = if self.venue == Venue::Generic {
            b'N'
        } else {
            Self::sb(aggr)
        };
        while want > 0 {
            let Some(px) = self.best(maker) else { break };
            let li = self.levels(maker).iter().position(|l| l.0 == px).expect("level");
            let level: Vec<(u64, u64)> = self.levels(maker)[li].1.clone();
            let mut fills = Vec::new();
            for (id, size) in level {
                if want == 0 {
                    break;
                }
                let q = size.min(want);
                want -= q;
                fills.push((id, q));
            }
            for &(_, q) in &fills {
                self.take(maker, px, 0, q);
            }
            let total: u64 = fills.iter().map(|f| f.1).sum();
            if self.venue == Venue::Nasdaq {
                for (id, q) in fills {
                    self.rec(b'T', aggr_byte, px, q, 0);
                    self.rec(b'F', Self::sb(maker), px, q, id);
                    self.rec(b'C', Self::sb(maker), px, q, id);
                    self.end_event();
                }
            } else {
                self.rec(b'T', aggr_byte, px, total, 0);
                for &(id, q) in &fills {
                    self.rec(b'F', Self::sb(maker), px, q, id);
                }
                for (id, q) in fills {
                    self.rec(b'C', Self::sb(maker), px, q, id);
                }
            }
        }
        if self.venue != Venue::Nasdaq {
            self.end_event();
        }
    }

    pub fn generate(seed: u64, venue: Venue, events: usize) -> Vec<MboEvent> {
        let mut m = Self::new(seed, venue);
        m.clears = venue == Venue::Cme;
        for _ in 0..events {
            m.step();
        }
        m.out
    }
}

/// Random shadows over a stream: abs prices near the touch and touch-relative ones, latencies, cancels, amends.
pub fn random_shadows(seed: u64, events: &[MboEvent], n: usize) -> (Vec<ShadowSpec>, Vec<ShadowAmend>) {
    let mut r = Xoshiro256StarStar::new(seed ^ 0x5eed);
    let (t0, t1) = (
        events.first().map_or(0, |e| e.ts_event),
        events.last().map_or(1, |e| e.ts_event),
    );
    let mut shadows = Vec::new();
    let mut amends = Vec::new();
    for i in 0..n {
        let side = if r.next_u64() % 2 == 0 {
            BookSide::Bid
        } else {
            BookSide::Ask
        };
        let ts_submit = t0 + (r.next_u64() % ((t1 - t0).max(1) as u64)) as i64;
        let price = if r.next_u64() % 3 == 0 {
            PriceSpec::Touch {
                offset_ticks: (r.next_u64() % 4) as i32 - 2,
            }
        } else {
            let base = 1_000 + if side == BookSide::Bid { -1 } else { 1 };
            PriceSpec::Abs(base + (r.next_u64() % 7) as i64 - 3)
        };
        let qty = 1 + r.next_u64() % 60;
        let id = format!("x{i}");
        let ts_cancel = (r.next_u64() % 3 == 0).then(|| ts_submit + (r.next_u64() % 200_000_000) as i64);
        if qty > 2 && r.next_u64() % 4 == 0 {
            amends.push(ShadowAmend {
                shadow_id: id.clone(),
                ts_send: ts_submit + (r.next_u64() % 100_000_000) as i64,
                new_qty: qty / 2,
            });
        }
        shadows.push(ShadowSpec {
            shadow_id: id,
            instrument_id: INST,
            side,
            price,
            qty,
            ts_submit,
            latency_ns: (r.next_u64() % 3_000_000) as i64,
            ts_cancel,
            group: None,
            source: ShadowSource::Sampled,
            order_key: None,
        });
    }
    (shadows, amends)
}

pub fn cfg(venue: Venue) -> EngineConfig {
    EngineConfig::new(rules(venue))
}
