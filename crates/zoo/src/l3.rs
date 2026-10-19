//! `hbt_l3_fifo` (SPEC 5.3.3 c): MBO-input model. Faithful = hftbacktest `L3FIFOQueueModel` +
//! `L3NoPartialFillExchange` at 5f3ec40 (whole-order fills when a feed order behind the shadow, or at a worse price
//! on its side, executes; full fill when the book crosses it). Corrected = the exact engine's default fill rule
//! (audit rulings A3-A5: executions behind / through the shadow, hidden prints on Nasdaq, taker walk on a cross), so
//! fill quantities equal the exact engine (oracle O-ZOO-4).
//!
//! The faithful mode is ported from hftbacktest at commit 5f3ec40b2afb764e0fea112f941ed85523ef4e88 (MIT,
//! Copyright (c) 2022 nkaz001; see NOTICE and third_party/licenses/hftbacktest-MIT.txt).

use std::collections::{BTreeMap, HashSet};
use std::sync::Mutex;

use qt_book::{Book, Effect};
use qt_types::{Action, BookSide, MboEvent, Px, Qty, Side, Ts, Xoshiro256StarStar};

use crate::{FillDelta, Mode, ModelId, ModelInput, QState, QueueModel, ShadowL2};

/// Reason of an L3 fill.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum L3Reason {
    /// Queue depleted (corrected) or a feed order behind the shadow executed (faithful).
    Queue,
    /// Execution at a worse price on the shadow's side.
    TradeThrough,
    /// Opposite best at or through the price after the event.
    Crossed,
}

/// Per-shadow state of the L3 FIFO model.
#[derive(Clone, Debug)]
pub struct L3Pair {
    side: BookSide,
    price: Px,
    faithful: bool,
    tick: Px,
    hidden_policy: Hidden,
    qty: Qty,
    filled: Qty,
    ahead_set: HashSet<u64>,
    taken: BTreeMap<u64, Qty>,
    ev_exec_behind: Qty,
    ev_exec_through: Qty,
    ev_hidden_at: Qty,
    ev_hidden_through: Qty,
    ev_own_at: Qty,
    pending: Option<L3Reason>,
}

/// Hidden-print handling of the exact rule (audit A3): ignored on venues without hidden orders' policy, behind
/// the displayed queue and through the price on Nasdaq.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Hidden {
    Displayed,
    Ignore,
    BehindAndThrough,
}

impl L3Pair {
    /// State at activation from the book as left by the previous event (SPEC 5.2.3).
    pub fn new(side: BookSide, price: Px, qty: Qty, mode: Mode, book: &Book) -> Self {
        let ahead_set: HashSet<u64> = book.orders_at(side, price).map(|o| o.order_id).collect();
        let rules = book.rules();
        let hidden_policy = if !rules.supports_hidden_orders {
            Hidden::Displayed
        } else if rules.venue == qt_types::Venue::Nasdaq {
            Hidden::BehindAndThrough
        } else {
            Hidden::Ignore
        };
        Self {
            side,
            price,
            faithful: mode == Mode::Faithful,
            tick: 1,
            hidden_policy,
            qty,
            filled: 0,
            ahead_set,
            taken: BTreeMap::new(),
            ev_exec_behind: 0,
            ev_exec_through: 0,
            ev_hidden_at: 0,
            ev_hidden_through: 0,
            ev_own_at: 0,
            pending: None,
        }
    }

    /// Instrument tick size (hbt tick arithmetic of the faithful mode).
    pub fn with_tick(mut self, tick: Px) -> Self {
        self.tick = tick.max(1);
        self
    }

    /// Remaining quantity.
    pub fn leaves(&self) -> Qty {
        self.qty - self.filled
    }

    /// Opposite best at or through the price.
    pub fn crossed(&self, book: &Book) -> bool {
        match self.side {
            BookSide::Bid => book.best(BookSide::Ask).is_some_and(|l| l.price <= self.price),
            BookSide::Ask => book.best(BookSide::Bid).is_some_and(|l| l.price >= self.price),
        }
    }

    /// Displayed size of the tracked orders ahead (exact queue ahead).
    pub fn queue_ahead(&self, book: &Book) -> f64 {
        self.qa(book) as f64
    }

    fn qa(&self, book: &Book) -> Qty {
        self.ahead_set
            .iter()
            .filter_map(|id| book.order(*id))
            .map(|(_, _, o)| o.size)
            .fold(0, Qty::saturating_add)
    }

    fn worse(&self, px: Px) -> bool {
        match self.side {
            BookSide::Bid => px < self.price,
            BookSide::Ask => px > self.price,
        }
    }

    /// One record after the book applied it (`_start_best` = (bid, ask) best prices at the start of the event;
    /// kept for callers, the hidden-print rule reads the book at the print).
    pub fn on_record(&mut self, e: &MboEvent, effect: &Effect, book: &Book, _start_best: (Option<Px>, Option<Px>)) {
        match effect {
            Effect::Remove { order_id, .. } | Effect::Replace { order_id, .. } => {
                self.ahead_set.remove(order_id);
            }
            // E36(d) (as the exact engine): outside a flagged snapshot a duplicate add follows the book (removed,
            // then appended at the tail), so an order ahead leaves the queue ahead; a flagged snapshot re-add at the
            // same level keeps its place
            Effect::Add {
                order_id, side, price, ..
            } if self.ahead_set.contains(order_id) => {
                let in_snapshot = book.rules().snapshot_records_flagged && e.flags & qt_types::flags::SNAPSHOT != 0;
                if !(in_snapshot && *side == self.side && *price == self.price) {
                    self.ahead_set.remove(order_id);
                }
            }
            _ => {}
        }
        match e.action() {
            Some(Action::Fill) => {
                let Some((side, px, _)) = book.order(e.order_id) else {
                    return;
                };
                if side != self.side {
                    return;
                }
                if self.faithful {
                    let behind = px == self.price && !self.ahead_set.contains(&e.order_id);
                    self.faithful_exec(e.price, behind, book);
                } else if px == self.price {
                    // ruling A4b: an execution of an order behind the shadow away from its displayed price never
                    // fills the shadow
                    if !self.ahead_set.contains(&e.order_id) && e.price == px {
                        self.ev_exec_behind = self.ev_exec_behind.saturating_add(e.size);
                    }
                } else if self.worse(px) && self.worse(e.price) {
                    // strictly better than both the resting and the execution price (A4)
                    self.ev_exec_through = self.ev_exec_through.saturating_add(e.size);
                }
            }
            Some(Action::Trade) if !self.faithful => {
                if e.side() != Side::None || matches!(self.hidden_policy, Hidden::Displayed | Hidden::Ignore) {
                    return;
                }
                // ruling A3d (as the exact engine): a hidden print is usable only when the book at the print shows
                // no displayed interest at or through its price on either side, and the price is on the tick grid
                let p = e.price;
                let usable = book.best(BookSide::Bid).is_none_or(|l| l.price < p)
                    && book.best(BookSide::Ask).is_none_or(|l| l.price > p);
                if !usable || p.rem_euclid(self.tick) != 0 {
                    return;
                }
                if p == self.price {
                    self.ev_hidden_at = self.ev_hidden_at.saturating_add(e.size);
                } else if self.worse(p) {
                    self.ev_hidden_through = self.ev_hidden_through.saturating_add(e.size);
                }
            }
            _ => {}
        }
    }

    /// hbt `L3FIFOQueueModel::fill_market_feed_order` + `L3NoPartialFillExchange` (faithful): whole-order fills. An
    /// execution at `exec_px` fills the shadow by trade-through when hbt's `fill_bid_between(best_bid, exec + 1)` /
    /// `fill_ask_between(best_ask, exec - 1)` reaches it, else by queue when the executed order rests behind the
    /// shadow at its price (`behind_at_price`).
    fn faithful_exec(&mut self, exec_px: Px, behind_at_price: bool, book: &Book) {
        if self.pending.is_some() {
            return;
        }
        // with one backtest order: when the tick range is wider than 1 every backtest order at or beyond exec +- 1
        // fills, else only those inside [exec + 1, best] (resp. [best, exec - 1])
        let t = |x: Px| x.div_euclid(self.tick);
        let (p, x) = (t(self.price), t(exec_px));
        let better = match (self.side, book.best(self.side).map(|l| t(l.price))) {
            (BookSide::Bid, Some(b)) if x < b => p > x && (1 < b - (x + 1) || p <= b),
            (BookSide::Ask, Some(a)) if x > a => p < x && (1 < (x - 1) - a || p >= a),
            _ => false,
        };
        if better {
            self.pending = Some(L3Reason::TradeThrough);
        } else if behind_at_price {
            self.pending = Some(L3Reason::Queue);
        }
    }

    /// Execution of `size` of an excluded real order (the user's own MBO twin removed from the tape, audit A1)
    /// resting on `side` at `px`. The record never reaches the book (so `on_record` cannot see it) and the shadow
    /// takes the twin's place: at the shadow's price the execution is volume behind the shadow, at a worse price on
    /// the shadow's side it is trade-through volume (as the exact engine credits it).
    pub fn on_excluded_exec(&mut self, side: BookSide, px: Px, size: Qty, book: &Book) {
        if side != self.side {
            return;
        }
        if self.faithful {
            self.faithful_exec(px, px == self.price, book);
        } else if px == self.price {
            // the exact engine's `ev_own_at`: used once the displayed queue ahead is exhausted
            self.ev_own_at = self.ev_own_at.saturating_add(size);
        } else if self.worse(px) {
            self.ev_exec_through = self.ev_exec_through.saturating_add(size);
        }
    }

    /// Priority-keeping size reduction (shadow amend) to the total quantity `new_qty`. Returns false when the order
    /// is done (`new_qty` at or below the filled quantity: the exact engine's `amended_to_filled`).
    pub fn amend(&mut self, new_qty: Qty) -> bool {
        if new_qty <= self.filled {
            self.qty = self.filled;
            return false;
        }
        self.qty = new_qty;
        true
    }

    /// Displayed volume of the real same-side orders with priority over the shadow in a locked or crossed book
    /// (E36(a)): every level strictly better than its price plus the exact queue ahead at its price.
    pub fn priority_volume(&self, book: &Book) -> Qty {
        let better = book
            .levels(self.side)
            .take_while(|l| match self.side {
                BookSide::Bid => l.price > self.price,
                BookSide::Ask => l.price < self.price,
            })
            .fold(0, |a: Qty, l| a.saturating_add(l.total));
        better.saturating_add(self.qa(book))
    }

    /// Taker fills (audit A5): walk the opposite displayed orders at or through the price, best level first and FIFO
    /// within a level, after the same-side volume with priority (E36(a), `priority_volume`) and net of what this
    /// shadow already took from each order; one fill per level.
    pub fn taker_walk(&mut self, book: &Book) -> Vec<Qty> {
        let opp = self.side.opposite();
        let mut skip = self.priority_volume(book);
        let mut want = self.leaves();
        let mut out = Vec::new();
        for lv in book.levels(opp) {
            let reach = match self.side {
                BookSide::Bid => lv.price <= self.price,
                BookSide::Ask => lv.price >= self.price,
            };
            if !reach || want == 0 {
                break;
            }
            let mut at_level: Qty = 0;
            for o in book.orders_at(opp, lv.price) {
                let sk = skip.min(o.size);
                skip -= sk;
                let avail = (o.size - sk).saturating_sub(self.taken.get(&o.order_id).copied().unwrap_or(0));
                let q = avail.min(want);
                if q > 0 {
                    *self.taken.entry(o.order_id).or_insert(0) += q;
                    at_level += q;
                    want -= q;
                }
                if want == 0 {
                    break;
                }
            }
            if at_level > 0 {
                out.push(at_level);
            }
        }
        for q in &out {
            self.filled += q;
        }
        out
    }

    /// Event end. Faithful: the pending whole-order fill, then the cross check (full fill). Corrected (the exact
    /// engine's default rule, audit A3-A5 with rulings A3d, A4b, 36a, 36d): executions behind the shadow at its
    /// displayed price (plus, once the displayed queue ahead is exhausted, usable hidden prints at the price and the
    /// executions of an excluded own twin) fill `queue`; executions strictly through both the resting and the
    /// execution price and hidden prints through it fill `trade_through`; a crossed book walks the opposite side as
    /// taker after the same-side volume with priority (`crossed`).
    pub fn on_event_end(&mut self, book: &Book) -> Vec<(Qty, L3Reason)> {
        let mut fills = Vec::new();
        if self.faithful {
            if let Some(r) = self.pending.take()
                && self.leaves() > 0
            {
                fills.push((self.leaves(), r));
                self.filled = self.qty;
            }
            if self.leaves() > 0 && self.crossed(book) {
                fills.push((self.leaves(), L3Reason::Crossed));
                self.filled = self.qty;
            }
            return fills;
        }
        let qa = self.qa(book);
        let mut v = self.ev_exec_behind;
        if qa == 0 {
            // hidden prints at the price and the excluded own twin's executions count once the queue ahead is gone
            v = v.saturating_add(self.ev_hidden_at).saturating_add(self.ev_own_at);
        }
        if v > 0 && self.leaves() > 0 {
            let q = v.min(self.leaves());
            fills.push((q, L3Reason::Queue));
            self.filled += q;
        }
        let through = self.ev_exec_through.saturating_add(self.ev_hidden_through);
        if through > 0 && self.leaves() > 0 {
            let q = through.min(self.leaves());
            fills.push((q, L3Reason::TradeThrough));
            self.filled += q;
        }
        if self.leaves() > 0 && self.crossed(book) {
            for q in self.taker_walk(book) {
                fills.push((q, L3Reason::Crossed));
            }
        }
        self.ev_exec_behind = 0;
        self.ev_exec_through = 0;
        self.ev_hidden_at = 0;
        self.ev_hidden_through = 0;
        self.ev_own_at = 0;
        fills
    }
}

/// `hbt_l3_fifo` as a [`QueueModel`] for callers that only have the trait: state per shadow index is kept inside
/// the model (initialized lazily on the first `on_mbo` after `on_arrive`); `on_mbo` returns the event-end fills on
/// the record carrying `LAST`. The zoo runner drives [`L3Pair`] directly.
pub struct L3Model {
    id: ModelId,
    pairs: Mutex<BTreeMap<u32, L3Pair>>,
}

impl L3Model {
    /// Builds the model.
    pub fn new(id: ModelId) -> Self {
        Self {
            id,
            pairs: Mutex::new(BTreeMap::new()),
        }
    }
}

impl QueueModel for L3Model {
    fn id(&self) -> &ModelId {
        &self.id
    }

    fn input(&self) -> ModelInput {
        ModelInput::Mbo
    }

    fn on_arrive(&self, s: &mut QState, o: &ShadowL2, visible_level_qty: f64, _rng: &mut Xoshiro256StarStar) {
        s.front = visible_level_qty;
        if let Ok(mut m) = self.pairs.lock() {
            m.remove(&o.idx);
        }
    }

    fn on_trade(
        &self,
        _s: &mut QState,
        _o: &ShadowL2,
        _ts: Ts,
        _trade_qty: f64,
        _aggressor: Side,
        _rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        FillDelta::None
    }

    fn on_depth(
        &self,
        _s: &mut QState,
        _o: &ShadowL2,
        _ts: Ts,
        _prev_qty: f64,
        _new_qty: f64,
        _flags: u8,
        _rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        FillDelta::None
    }

    fn on_mbo(&self, s: &mut QState, o: &ShadowL2, e: &MboEvent, effect: &Effect, book: &Book) -> FillDelta {
        let Ok(mut m) = self.pairs.lock() else {
            return FillDelta::None;
        };
        let pair = m.entry(o.idx).or_insert_with(|| {
            let mut p = L3Pair::new(o.side, o.price, o.qty.max(0.0).round() as Qty, self.id.mode, book);
            // the record was already applied: an order it just placed at the level is behind the shadow
            if let Effect::Add { order_id, .. } | Effect::Replace { order_id, .. } = effect {
                p.ahead_set.remove(order_id);
            }
            p
        });
        pair.on_record(e, effect, book, (None, None));
        s.front = pair.queue_ahead(book);
        if !e.is_last() {
            return FillDelta::None;
        }
        let fills = pair.on_event_end(book);
        s.front = pair.queue_ahead(book);
        let q: Qty = fills.iter().map(|f| f.0).sum();
        if q == 0 {
            FillDelta::None
        } else if pair.leaves() == 0 {
            FillDelta::Full
        } else {
            FillDelta::Qty(q as f64)
        }
    }

    fn on_cross(&self, _s: &mut QState, _o: &ShadowL2) -> FillDelta {
        FillDelta::None
    }

    fn queue_ahead(&self, s: &QState) -> f64 {
        s.front
    }
}
