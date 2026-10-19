//! L2 market state for feature synthesis (SPEC 5.5.2): the `VisibleBook` plus rolling windows built only from
//! emitted L2 rows. Hidden prints (`HIDDEN_TRADE`) are excluded from every trade feature (displayed volume only).
//!
//! Window conventions: a window of length `W` at time `t` is `(t - W, t]`. L1 changes are recorded after each batch
//! whose best bid/ask price or quantity changed; OFI (Cont-Kukanov-Stoikov) uses only changes where both sides exist
//! before and after, bid-positive `e_n`, divided by the mean of `(Qb1 + Qa1) / 2` over those changes. Mid changes
//! (both sides present) feed `vol_ticks_60s`. Unexplained decreases are `max(0, prev - new - at-price displayed
//! opposite-aggressor trades)` per level per batch.

use std::collections::{BTreeMap, VecDeque};

use qt_l2::VisibleBook;
use qt_schema::rows::{L2Kind, L2Row, l2flags};
use qt_types::{BookSide, Px, Qty, Ts};

/// 1 s in ns.
pub const S: i64 = 1_000_000_000;

/// Visible (price, qty) of a best level.
type Level = (Px, Qty);

/// `0` bid, `1` ask.
pub const fn si(s: BookSide) -> usize {
    match s {
        BookSide::Bid => 0,
        BookSide::Ask => 1,
    }
}

/// `+1` bid, `-1` ask.
pub const fn sign(s: BookSide) -> f64 {
    match s {
        BookSide::Bid => 1.0,
        BookSide::Ask => -1.0,
    }
}

#[derive(Clone, Copy, Debug)]
struct Trade {
    ts: Ts,
    px: Px,
    hit: Option<BookSide>,
    qty: f64,
}

#[derive(Clone, Copy, Debug)]
struct OfiObs {
    ts: Ts,
    e: f64,
    depth: f64,
}

/// What one batch did at one level (for per-order accumulators).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct LevelBatch {
    /// Displayed at-price volume with the aggressor opposite to the level's side.
    pub traded: f64,
    /// `max(0, prev - new - traded)` when the level had a depth row, else 0.
    pub unexplained: f64,
    /// Visible quantity before the batch.
    pub prev: Option<f64>,
    /// Visible quantity after the batch.
    pub new: Option<f64>,
}

/// Rolling L2 state of one instrument.
#[derive(Clone, Debug, Default)]
pub struct Market {
    /// The visible book.
    pub vb: VisibleBook,
    l1: Option<[(Px, Qty); 2]>,
    l1_any: (Option<Level>, Option<Level>),
    bbo_changes: VecDeque<Ts>,
    ofi: VecDeque<OfiObs>,
    last_mid: Option<f64>,
    mid_moves: VecDeque<(Ts, f64)>,
    trades: VecDeque<Trade>,
    dec: BTreeMap<(usize, Px), VecDeque<(Ts, f64)>>,
    tick: f64,
}

fn prune<T>(q: &mut VecDeque<T>, ts: impl Fn(&T) -> Ts, keep_after: Ts) {
    while q.front().is_some_and(|x| ts(x) <= keep_after) {
        q.pop_front();
    }
}

impl Market {
    /// Empty state for an instrument with tick `tick` (price units).
    pub fn new(tick: Px) -> Self {
        Self {
            tick: tick.max(1) as f64,
            ..Self::default()
        }
    }

    /// Tick in price units.
    pub fn tick(&self) -> f64 {
        self.tick
    }

    /// Applies one emitted batch (rows in batch order); returns per-level summaries of the levels it touched.
    pub fn apply_batch(&mut self, rows: &[L2Row]) -> BTreeMap<(usize, Px), LevelBatch> {
        let mut lv: BTreeMap<(usize, Px), LevelBatch> = BTreeMap::new();
        let mut batch_ts = Ts::MIN;
        for r in rows {
            batch_ts = batch_ts.max(r.ts_event);
            match r.kind {
                L2Kind::Trade => {
                    if r.flags & l2flags::HIDDEN_TRADE != 0 {
                        continue;
                    }
                    let hit = BookSide::from_side(r.side);
                    let qty = r.trade_qty as f64;
                    if let (Some(h), Some(px)) = (hit, r.price)
                        && r.aggressor == h.opposite().to_side()
                    {
                        lv.entry((si(h), px)).or_default().traded += qty;
                    }
                    if let Some(px) = r.price {
                        self.trades.push_back(Trade {
                            ts: r.ts_event,
                            px,
                            hit,
                            qty,
                        });
                    }
                }
                L2Kind::Depth | L2Kind::Snapshot => {
                    if let (Some(s), Some(px)) = (BookSide::from_side(r.side), r.price) {
                        let e = lv.entry((si(s), px)).or_default();
                        if e.prev.is_none() {
                            e.prev = Some(r.prev_qty as f64);
                        }
                        e.new = Some(r.new_qty as f64);
                        if r.kind == L2Kind::Snapshot {
                            // A snapshot rebuilds the view; it explains nothing.
                            e.prev = Some(r.new_qty as f64);
                        }
                    }
                }
                L2Kind::Clear => {}
            }
            self.vb.apply(r);
        }
        if batch_ts == Ts::MIN {
            return lv;
        }
        for (&(s, px), e) in lv.iter_mut() {
            if let (Some(p), Some(n)) = (e.prev, e.new) {
                e.unexplained = (p - n - e.traded).max(0.0);
                if e.unexplained > 0.0 {
                    self.dec
                        .entry((s, px))
                        .or_default()
                        .push_back((batch_ts, e.unexplained));
                }
            }
        }
        self.after_batch(batch_ts);
        lv
    }

    fn after_batch(&mut self, ts: Ts) {
        let b = self.vb.best(BookSide::Bid);
        let a = self.vb.best(BookSide::Ask);
        if (b, a) != self.l1_any {
            self.bbo_changes.push_back(ts);
            if let (Some(old), Some(nb), Some(na)) = (self.l1, b, a) {
                let [(pb0, qb0), (pa0, qa0)] = old;
                let (pb, qb, pa, qa) = (nb.0, nb.1 as f64, na.0, na.1 as f64);
                let (qb0, qa0) = (qb0 as f64, qa0 as f64);
                let mut e = 0.0;
                if pb >= pb0 {
                    e += qb;
                }
                if pb <= pb0 {
                    e -= qb0;
                }
                if pa <= pa0 {
                    e -= qa;
                }
                if pa >= pa0 {
                    e += qa0;
                }
                self.ofi.push_back(OfiObs {
                    ts,
                    e,
                    depth: (qb + qa) / 2.0,
                });
            }
            self.l1_any = (b, a);
        }
        self.l1 = match (b, a) {
            (Some(b), Some(a)) => Some([b, a]),
            _ => None,
        };
        if let (Some(b), Some(a)) = (b, a) {
            let mid = (b.0 as f64 + a.0 as f64) / 2.0;
            if let Some(m0) = self.last_mid
                && m0 != mid
            {
                let d = (mid - m0) / self.tick;
                self.mid_moves.push_back((ts, d * d));
            }
            self.last_mid = Some(mid);
        }
        let keep = ts.saturating_sub(60 * S);
        prune(&mut self.trades, |t| t.ts, keep);
        prune(&mut self.mid_moves, |t| t.0, keep);
        let keep10 = ts.saturating_sub(10 * S);
        prune(&mut self.bbo_changes, |t| *t, keep10);
        prune(&mut self.ofi, |t| t.ts, keep10);
        self.dec.retain(|_, q| {
            prune(q, |t| t.0, keep10);
            !q.is_empty()
        });
    }

    /// Visible order count at a price (`None` without counts; 0 when absent).
    pub fn count_at(&self, side: BookSide, px: Px) -> Option<u32> {
        if !self.vb.in_view(side, px) {
            return Some(0);
        }
        self.vb
            .levels(side, 0)
            .into_iter()
            .find(|l| l.0 == px)
            .and_then(|l| l.2)
    }

    /// CKS OFI over `(t - w, t]`, signed by `s`, normalized by the mean depth; None without L1 change.
    pub fn ofi(&self, t: Ts, w: i64, s: f64) -> Option<f64> {
        let lo = t.saturating_sub(w);
        let obs: Vec<&OfiObs> = self.ofi.iter().filter(|o| o.ts > lo && o.ts <= t).collect();
        if obs.is_empty() {
            return None;
        }
        let sum: f64 = obs.iter().map(|o| o.e).sum();
        let depth = obs.iter().map(|o| o.depth).sum::<f64>() / obs.len() as f64;
        (depth > 0.0).then(|| s * sum / depth)
    }

    /// Displayed trade volume over `(t - w, t]` matching `pred(px, hit side)`.
    pub fn trade_vol(&self, t: Ts, w: i64, pred: impl Fn(Px, Option<BookSide>) -> bool) -> f64 {
        let lo = t.saturating_sub(w);
        self.trades
            .iter()
            .filter(|x| x.ts > lo && x.ts <= t && pred(x.px, x.hit))
            .map(|x| x.qty)
            .sum()
    }

    /// Unexplained decreases at a level over `(t - w, t]`.
    pub fn unexplained(&self, side: BookSide, px: Px, t: Ts, w: i64) -> f64 {
        let lo = t.saturating_sub(w);
        self.dec
            .get(&(si(side), px))
            .map_or(0.0, |q| q.iter().filter(|x| x.0 > lo && x.0 <= t).map(|x| x.1).sum())
    }

    /// `sqrt(sum (dmid / tick)^2)` over `(t - w, t]`.
    pub fn vol_ticks(&self, t: Ts, w: i64) -> f64 {
        let lo = t.saturating_sub(w);
        self.mid_moves
            .iter()
            .filter(|x| x.0 > lo && x.0 <= t)
            .map(|x| x.1)
            .sum::<f64>()
            .sqrt()
    }

    /// L1 changes over `(t - w, t]`.
    pub fn bbo_changes(&self, t: Ts, w: i64) -> usize {
        let lo = t.saturating_sub(w);
        self.bbo_changes.iter().filter(|x| **x > lo && **x <= t).count()
    }
}
