//! Per-order L2 tracking and the feature vector of SPEC 5.5.2 at a landmark.
//!
//! Trackers (`qt_zoo` queue models) arrive at the order's activation batch with the visible quantity before the
//! batch and then see the batch rows of their level like the zoo runner (at-price opposite-aggressor trades ->
//! `on_trade`, depth/snapshot rows -> `on_depth`); fills are never applied, the features are their queue-ahead
//! estimates clamped at 0 (the visible `level_qty` before arrival). For real orders the order's own displayed size is
//! removed from the level quantities a tracker and `level_qty` see (an L2 user knows its own size).

use qt_schema::rows::{L2Kind, L2Row, l2flags};
use qt_types::{BookSide, Instrument, Px, Ts, Xoshiro256StarStar, stable_seed};
use qt_zoo::{QState, QueueModel, ShadowL2};

use crate::market::{LevelBatch, Market, S, sign};

/// Tracker of one order by one queue model.
pub struct Tracker {
    state: QState,
    rng: Xoshiro256StarStar,
}

/// L2-side state of one order.
pub struct OrderTrack {
    /// Side.
    pub side: BookSide,
    /// Price (resolved).
    pub price: Px,
    /// Quantity (size units).
    pub qty: f64,
    /// Lot (size units).
    pub lot: f64,
    /// Life start (`ts_live` or `ts_add`).
    pub start: Ts,
    /// Arrived in the L2 arm.
    pub arrived: bool,
    /// Trackers, in tracker order.
    pub trackers: Vec<Tracker>,
    /// Own displayed size currently included in the visible level (real orders).
    pub own_in_view: f64,
    /// At-price opposite-aggressor displayed volume since arrival.
    pub traded_since: f64,
    /// Unexplained decreases at the level since arrival.
    pub unexplained_since: f64,
}

impl OrderTrack {
    /// New order (trackers seeded with `stable_seed(seed, [model_id, order_ref])`).
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        side: BookSide,
        price: Px,
        qty: f64,
        lot: f64,
        start: Ts,
        models: &[Box<dyn QueueModel>],
        seed: u64,
        order_ref: &str,
    ) -> Self {
        let trackers = models
            .iter()
            .map(|m| Tracker {
                state: QState::default(),
                rng: Xoshiro256StarStar::new(stable_seed(
                    seed,
                    &[m.id().to_string().as_bytes(), order_ref.as_bytes()],
                )),
            })
            .collect();
        Self {
            side,
            price,
            qty,
            lot,
            start,
            arrived: false,
            trackers,
            own_in_view: 0.0,
            traded_since: 0.0,
            unexplained_since: 0.0,
        }
    }

    fn shadow(&self, idx: u32) -> ShadowL2 {
        ShadowL2 {
            idx,
            side: self.side,
            price: self.price,
            qty: self.qty,
            lot: self.lot,
        }
    }

    /// Arrival with the visible quantity at the price before the batch.
    pub fn arrive(&mut self, models: &[Box<dyn QueueModel>], visible: f64) {
        let o = self.shadow(0);
        for (m, t) in models.iter().zip(self.trackers.iter_mut()) {
            t.state = QState {
                leaves: self.qty,
                ..QState::default()
            };
            m.on_arrive(&mut t.state, &o, visible, &mut t.rng);
        }
        self.arrived = true;
    }

    /// Feeds one batch row of the order's level (row already applied to the market). `own_after` is the order's own
    /// displayed size after the batch (0 for shadows).
    pub fn on_row(&mut self, models: &[Box<dyn QueueModel>], r: &L2Row, own_after: f64) {
        if !self.arrived || r.price != Some(self.price) {
            return;
        }
        let o = self.shadow(0);
        match r.kind {
            L2Kind::Trade => {
                if r.flags & l2flags::HIDDEN_TRADE != 0
                    || BookSide::from_side(r.side) != Some(self.side)
                    || r.aggressor != self.side.opposite().to_side()
                {
                    return;
                }
                for (m, t) in models.iter().zip(self.trackers.iter_mut()) {
                    let _ = m.on_trade(
                        &mut t.state,
                        &o,
                        r.ts_event,
                        r.trade_qty as f64,
                        r.aggressor,
                        &mut t.rng,
                    );
                }
            }
            L2Kind::Depth | L2Kind::Snapshot => {
                if BookSide::from_side(r.side) != Some(self.side) {
                    return;
                }
                let own_after = if r.new_qty == 0 { 0.0 } else { own_after };
                let prev = (r.prev_qty as f64 - self.own_in_view).max(0.0);
                let new = (r.new_qty as f64 - own_after).max(0.0);
                self.own_in_view = own_after;
                for (m, t) in models.iter().zip(self.trackers.iter_mut()) {
                    let _ = m.on_depth(&mut t.state, &o, r.ts_event, prev, new, r.flags, &mut t.rng);
                }
            }
            L2Kind::Clear => self.own_in_view = 0.0,
        }
    }

    /// Accumulates the batch summary of the order's level.
    pub fn on_level_batch(&mut self, b: &LevelBatch) {
        if self.arrived {
            self.traded_since += b.traded;
            self.unexplained_since += b.unexplained;
        }
    }

    /// Queue-ahead estimate of tracker `i` (clamped >= 0; `level_qty` before arrival).
    pub fn tracker_value(&self, models: &[Box<dyn QueueModel>], i: usize, level_qty: f64) -> f64 {
        if !self.arrived {
            return level_qty;
        }
        models[i].queue_ahead(&self.trackers[i].state).max(0.0)
    }
}

/// Inputs of one landmark snapshot besides the market and order.
pub struct SnapshotCtx<'a> {
    /// Instrument (session clock, tick).
    pub inst: &'a Instrument,
    /// Landmark time.
    pub t: Ts,
    /// Remaining own quantity at the landmark.
    pub own_qty: f64,
    /// Index of the `hbt_risk_adverse` tracker, if any.
    pub ra_index: Option<usize>,
    /// Counts are emitted.
    pub include_counts: bool,
}

fn opt(x: f64) -> Option<f64> {
    x.is_finite().then_some(x)
}

/// Feature values in the column order of `feature_names(trackers)`.
pub fn snapshot(m: &Market, o: &OrderTrack, models: &[Box<dyn QueueModel>], c: &SnapshotCtx<'_>) -> Vec<Option<f64>> {
    let s = sign(o.side);
    let tick = m.tick();
    let side = o.side;
    let best_same = m.vb.best(side);
    let bid = m.vb.best(BookSide::Bid);
    let ask = m.vb.best(BookSide::Ask);
    let level_qty = (m.vb.qty_at(side, o.price) as f64 - o.own_in_view).max(0.0);
    let level_ct = if c.include_counts {
        m.count_at(side, o.price).map(|n| {
            let own = if o.own_in_view > 0.0 { 1.0 } else { 0.0 };
            (f64::from(n) - own).max(0.0)
        })
    } else {
        None
    };
    let mut v: Vec<Option<f64>> = Vec::with_capacity(32 + models.len());
    v.push(best_same.map(|(p, _)| s * (p - o.price) as f64 / tick));
    v.push(match (bid, ask) {
        (Some(b), Some(a)) => Some((a.0 - b.0) as f64 / tick),
        _ => None,
    });
    v.push(Some(level_qty));
    v.push(level_ct);
    v.push(Some(c.own_qty / o.lot));
    let trackers: Vec<f64> = (0..models.len())
        .map(|i| o.tracker_value(models, i, level_qty))
        .collect();
    v.extend(trackers.iter().map(|x| Some(*x)));
    v.push(c.ra_index.map(|i| {
        let d = level_qty + c.own_qty;
        if d > 0.0 { trackers[i] / d } else { 0.0 }
    }));
    v.push(Some((c.t - o.start) as f64));
    v.push(Some(o.traded_since));
    v.push(Some(o.unexplained_since));
    let qb1 = bid.map_or(0.0, |b| b.1 as f64);
    let qa1 = ask.map_or(0.0, |a| a.1 as f64);
    v.push(if qb1 + qa1 > 0.0 {
        Some(s * (qb1 - qa1) / (qb1 + qa1))
    } else {
        None
    });
    let top = |sd: BookSide| -> Vec<f64> {
        let mut l: Vec<f64> = m.vb.levels(sd, 3).iter().map(|x| x.1 as f64).collect();
        l.resize(3, 0.0);
        l
    };
    let (tb, ta) = (top(BookSide::Bid), top(BookSide::Ask));
    let (sb, sa): (f64, f64) = (tb.iter().sum(), ta.iter().sum());
    v.push(if sb + sa > 0.0 {
        Some(s * (sb - sa) / (sb + sa))
    } else {
        None
    });
    v.push(match (bid, ask) {
        (Some(b), Some(a)) if qb1 + qa1 > 0.0 => {
            let (pb, pa) = (b.0 as f64, a.0 as f64);
            let wmid = (qb1 * pa + qa1 * pb) / (qb1 + qa1);
            opt(s * (wmid - (pb + pa) / 2.0) / tick)
        }
        _ => None,
    });
    let (same, oppl) = if side == BookSide::Bid { (&tb, &ta) } else { (&ta, &tb) };
    v.extend(same.iter().map(|x| Some(*x)));
    v.extend(oppl.iter().map(|x| Some(*x)));
    v.push(m.ofi(c.t, S, s));
    v.push(m.ofi(c.t, 10 * S, s));
    v.push(Some(
        m.trade_vol(c.t, 10 * S, |px, hit| px == o.price && hit == Some(side)),
    ));
    v.push(Some(m.trade_vol(c.t, 10 * S, |_, hit| hit == Some(side))));
    v.push(Some(m.trade_vol(c.t, 60 * S, |_, _| true)));
    v.push(Some(m.unexplained(side, o.price, c.t, 10 * S) / 10.0));
    v.push(Some(m.vol_ticks(c.t, 60 * S)));
    v.push(Some(m.bbo_changes(c.t, 10 * S) as f64));
    let open = c.inst.midnight_utc_ns as f64 + f64::from(c.inst.open_s) * 1e9;
    let len = (f64::from(c.inst.close_s) - f64::from(c.inst.open_s)) * 1e9;
    let frac = if len > 0.0 {
        ((c.t as f64 - open) / len).clamp(0.0, 1.0)
    } else {
        0.0
    };
    let ang = 2.0 * std::f64::consts::PI * frac;
    v.push(Some(ang.sin()));
    v.push(Some(ang.cos()));
    v.push(match (bid, ask) {
        (Some(b), Some(a)) => {
            let mid = (b.0 as f64 + a.0 as f64) / 2.0;
            if mid > 0.0 { opt((mid / tick).ln()) } else { None }
        }
        _ => None,
    });
    v
}
