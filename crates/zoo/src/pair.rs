//! One (model, shadow) pair of the L2 arm: the dispatch rules of SPEC 5.3.2 per family and mode, fill
//! bookkeeping, the faithful hbt quirks (as recorded from hbt itself: GTX expiry on arrival, clear keeps the order, per-row cross,
//! #312 zombie with its run-level `InvalidOrderStatus`), the NT touch analytic tracker and particle fill records.
//!
//! The faithful hbt rules are ported from hftbacktest at commit 5f3ec40b2afb764e0fea112f941ed85523ef4e88 (MIT,
//! Copyright (c) 2022 nkaz001; see NOTICE and third_party/licenses/hftbacktest-MIT.txt).

use qt_schema::rows::{Quirk, ZooFillReason};
use qt_types::{BookSide, Side, Ts, Xoshiro256StarStar};

use crate::models::{QUIRK_HBT_312, QUIRK_HBT_NAN_RESET, nt_depth_step, nt_gate_blocks, nt_trade_step};
use crate::{Family, FillDelta, Mode, ModelId, QState, QueueModel, ShadowL2};

/// One model-independent input of a pair, generated from the L2 batches (see `sim`).
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Op {
    /// Activation: quantities before the batch in the view and in the hbt depth mirror, and whether the
    /// opposite best is at or through the price (view: corrected / non-hbt; mirror: hbt GTX check).
    Arrive {
        /// Activation time (`ts_live`, used for a `crossed_at_live` fill).
        ts: Ts,
        /// View quantity at the price.
        q_view: f64,
        /// Mirror quantity at the price.
        q_hbt: f64,
        /// View opposite best at or through.
        cross_view: bool,
        /// Mirror opposite best at or through.
        cross_hbt: bool,
        /// Visible opposite quantity at prices at or through the shadow's price (taker size, audit A5).
        opp_view: f64,
    },
    /// Depth or snapshot row at the shadow's level.
    Depth {
        /// Row time.
        ts: Ts,
        /// What the view showed before.
        prev_view: f64,
        /// What the hbt mirror held before.
        prev_hbt: f64,
        /// New quantity.
        new: f64,
        /// `qt.l2` flags of the row.
        flags: u8,
    },
    /// At-price trade hitting the shadow's side (`hidden`: a hidden print, aggressor unknown).
    Trade {
        /// Row time.
        ts: Ts,
        /// Trade quantity.
        qty: f64,
        /// Aggressor side.
        aggressor: Side,
        /// Hidden print.
        hidden: bool,
    },
    /// Trade through the price.
    Through {
        /// Row time.
        ts: Ts,
        /// Hidden print.
        hidden: bool,
    },
    /// hbt mirror: the opposite best moved toward the order and reached its price on this row.
    CrossRow {
        /// Row time.
        ts: Ts,
    },
    /// `clear` row.
    Clear {
        /// Row time.
        ts: Ts,
    },
    /// Priority-keeping size reduction of the shadow (schema 1.1 `ZooSpec.amends`, `qt.shadow_amend`): the total
    /// quantity becomes `qty` (the `ShadowL2` of this and later ops carries it); the queue estimate is kept.
    Amend {
        /// Effective time.
        ts: Ts,
        /// New total quantity.
        qty: f64,
    },
    /// End of a batch that touched the shadow (or crossed it in the view).
    BatchEnd {
        /// Batch time.
        ts: Ts,
        /// View opposite best at or through the price.
        cross_view: bool,
        /// Visible opposite quantity at prices at or through the shadow's price (taker size, audit A5).
        opp_view: f64,
        /// `cadence_ns == 0` (corrected models reset their trade netting per batch).
        cadence0: bool,
    },
}

/// A model plus its dispatch traits.
pub struct ModelSlot {
    /// Canonical id.
    pub id: ModelId,
    /// Canonical id string.
    pub id_str: String,
    /// The model.
    pub model: Box<dyn QueueModel>,
    /// Faithful hbt L2 family (mirror quantities, GTX expiry, per-row cross, clear keeps the order).
    pub hbt_faithful: bool,
    /// Receives hidden prints (NT families in faithful mode, and `front_of_queue` in both modes).
    pub hidden: bool,
    /// Particle family.
    pub particles: bool,
    /// Touch-analytic family (`Some(p)`), with `gated` = queue-gated variant.
    pub touch: Option<(f64, bool)>,
    /// Corrected mode.
    pub corrected: bool,
}

impl ModelSlot {
    /// Wraps a built model.
    pub fn new(model: Box<dyn QueueModel>) -> Self {
        let id = model.id().clone();
        let fam = id.family;
        let faithful = id.mode == Mode::Faithful;
        let touch = if fam.is_touch() {
            Some((id.param("p").unwrap_or(1.0), fam == Family::NtQueueL2ProbTouch))
        } else {
            None
        };
        Self {
            id_str: id.to_string(),
            hbt_faithful: faithful && fam.is_hbt_l2(),
            // the optimistic bound sees every print the exact engine may fill from (A3d hidden prints, O-ZOO-6)
            hidden: (faithful && fam.is_nt()) || fam == Family::FrontOfQueue,
            particles: fam.is_particle(),
            touch,
            corrected: !faithful,
            model,
            id,
        }
    }
}

/// One model fill.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct FillRec {
    /// Fill time.
    pub ts: Ts,
    /// Quantity.
    pub qty: f64,
    /// Cumulative quantity.
    pub cum: f64,
    /// Model queue-ahead estimate before the callback.
    pub qa_before: f64,
    /// Reason.
    pub reason: ZooFillReason,
    /// Quirk that fired.
    pub quirk: Quirk,
}

/// Deterministic F path of NT (d) and the touch / trade-through times for the analytic predictions (5.3.3 e, f).
/// Faithful mode counts hidden at-price prints as touches and, for the queue-gated family, records a
/// trade-through or cross only while the deterministic `F` is 0 (NautilusTrader's queue gate, [`nt_gate_blocks`]).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TouchTracker {
    front: f64,
    /// Times of counted touches.
    pub touches: Vec<Ts>,
    /// First trade-through or cross.
    pub through: Option<Ts>,
}

/// How a pair ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PairEnd {
    /// Leaves exhausted.
    Filled,
    /// hbt GTX order crossing on arrival: expired unfilled (faithful).
    Expired,
    /// Cancel took effect.
    Cancelled,
    /// Book cleared.
    CensoredClear,
    /// Copied from the exact engine (gap / bad book).
    CensoredGap,
    /// Past `ts_live + h_max`, or end of data.
    Stopped,
    /// hbt would have raised `InvalidOrderStatus` on a zombie (faithful #312).
    HbtError,
    /// An amend reduced the total quantity to the filled quantity (exact engine: `amended_to_filled`).
    AmendedToFilled,
}

/// State of one (model, shadow) pair.
#[derive(Clone, Debug)]
pub struct Pair {
    /// Model state.
    pub st: QState,
    /// Per-pair RNG stream.
    pub rng: Xoshiro256StarStar,
    /// Activated.
    pub live: bool,
    /// Fills stopped (done or ended); trackers may still run.
    pub done: bool,
    /// End (once nothing runs any more).
    pub end: Option<PairEnd>,
    /// Faithful hbt #312 zombie: done, still receives hooks to detect hbt's run-fatal error.
    pub zombie: bool,
    /// Time of the hbt `InvalidOrderStatus` error.
    pub hbt_error: Option<Ts>,
    /// Fills (deterministic and touch families).
    pub fills: Vec<FillRec>,
    /// Aggregated particle fills `(ts, sum over particles)`.
    pub pfills: Vec<(Ts, f64)>,
    /// Touch analytic tracker.
    pub tracker: Option<TouchTracker>,
    /// Optional `(ts, front bits, cum_trade bits)` after every op (tests).
    pub trace: Option<Vec<(Ts, f64, f64)>>,
    /// Model estimate changed in the current batch (trajectory).
    pub touched: bool,
    /// Corrected mode (audit A5): visible opposite size this pair already took on a cross (`crossed_at_live` or
    /// `crossed`), capped at what is still visible at or through its price, so the same displayed size is never
    /// taken twice (review M6).
    pub taker_taken: f64,
}

const EPS: f64 = 1e-9;

impl Pair {
    /// New pair for a shadow of quantity `qty`.
    pub fn new(slot: &ModelSlot, qty: f64, rng: Xoshiro256StarStar) -> Self {
        Self {
            st: QState {
                leaves: qty,
                ..QState::default()
            },
            rng,
            live: false,
            done: false,
            end: None,
            zombie: false,
            hbt_error: None,
            fills: Vec::new(),
            pfills: Vec::new(),
            tracker: slot.touch.map(|_| TouchTracker::default()),
            trace: None,
            touched: false,
            taker_taken: 0.0,
        }
    }

    /// Visible opposite size at or through the price still available to this pair as a taker (corrected mode):
    /// `opp` minus what it already took (capped at `opp`).
    fn taker_avail(&mut self, slot: &ModelSlot, opp: f64) -> f64 {
        if !slot.corrected {
            return opp;
        }
        self.taker_taken = self.taker_taken.min(opp.max(0.0));
        (opp - self.taker_taken).max(0.0)
    }

    /// Records a taker fill against the visible opposite size (`before` = filled quantity before the fill).
    fn taker_took(&mut self, slot: &ModelSlot, before: f64, cap: f64) {
        if !slot.corrected {
            return;
        }
        let took = if slot.particles {
            // each particle took at most the cap; the largest take bounds what is gone for every particle
            if self.psum() > before { cap } else { 0.0 }
        } else {
            (self.st.filled - before).max(0.0)
        };
        self.taker_taken += took.min(cap);
    }

    /// Nothing more to do for this pair.
    pub fn finished(&self) -> bool {
        self.end.is_some()
    }

    /// Ends the pair (fills and trackers) unless already ended.
    pub fn stop(&mut self, why: PairEnd) {
        if self.end.is_none() {
            self.end = Some(why);
            self.done = true;
        }
    }

    /// First model fill time.
    pub fn first_fill(&self) -> Option<Ts> {
        self.fills.first().map(|f| f.ts)
    }

    fn maybe_finish(&mut self) {
        if self.done && !self.zombie && self.tracker.is_none() && self.end.is_none() {
            self.end = Some(PairEnd::Filled);
        }
    }

    fn apply_delta(
        &mut self,
        slot: &ModelSlot,
        o: &ShadowL2,
        ts: Ts,
        d: FillDelta,
        qa_before: f64,
        reason: ZooFillReason,
    ) {
        if slot.particles {
            let cap = match d {
                FillDelta::None => 0.0,
                FillDelta::Qty(x) => x,
                FillDelta::Full => f64::INFINITY,
            };
            if cap > 0.0 {
                let mut sum = 0.0;
                for p in &mut self.st.particles {
                    let q = (o.qty - p.filled).min(cap);
                    if q > 0.0 {
                        p.filled += q;
                        sum += q;
                        if p.first_fill.is_none() {
                            p.first_fill = Some(ts);
                        }
                    }
                }
                if sum > 0.0 {
                    self.pfills.push((ts, sum));
                }
            }
            if self.st.particles.iter().all(|p| o.qty - p.filled <= EPS * o.lot) {
                self.done = true;
            }
            return;
        }
        let q = match d {
            FillDelta::None => 0.0,
            FillDelta::Qty(x) => x.min(self.st.leaves),
            FillDelta::Full => self.st.leaves,
        };
        let zombie_now = slot.hbt_faithful && self.st.quirk & QUIRK_HBT_312 != 0 && !self.zombie;
        if q > 0.0 {
            self.st.leaves -= q;
            self.st.filled += q;
            let quirk = if zombie_now {
                Quirk::Hbt312
            } else if self.st.quirk & QUIRK_HBT_NAN_RESET != 0 {
                Quirk::HbtNanReset
            } else {
                Quirk::None
            };
            self.fills.push(FillRec {
                ts,
                qty: q,
                cum: self.st.filled,
                qa_before,
                reason,
                quirk,
            });
            if self.st.leaves <= EPS * o.lot {
                self.st.leaves = self.st.leaves.max(0.0);
                self.done = true;
            }
        }
        if zombie_now {
            self.zombie = true;
            self.done = true;
        }
        self.st.done = self.done;
        self.maybe_finish();
    }

    /// A zombie received a fill attempt: hbt aborts with `InvalidOrderStatus`.
    fn zombie_attempt(&mut self, ts: Ts, attempted: bool) {
        if attempted {
            self.hbt_error = Some(ts);
            self.zombie = false;
            self.end = Some(PairEnd::HbtError);
        }
    }

    /// Particle snapshot before a callback (sum of filled).
    fn psum(&self) -> f64 {
        self.st.particles.iter().map(|p| p.filled).fold(0.0, |a, b| a + b)
    }

    fn record_particles(&mut self, ts: Ts, before: f64) {
        let after = self.psum();
        if after > before {
            self.pfills.push((ts, after - before));
        }
    }

    fn trace(&mut self, ts: Ts) {
        if let Some(t) = self.trace.as_mut() {
            t.push((ts, self.st.front, self.st.cum_trade));
        }
    }

    /// Applies one op.
    pub fn apply(&mut self, slot: &ModelSlot, o: &ShadowL2, op: &Op) {
        if self.finished() {
            return;
        }
        let m = slot.model.as_ref();
        let fills_on = !self.done;
        match *op {
            Op::Arrive {
                ts,
                q_view,
                q_hbt,
                cross_view,
                cross_hbt,
                opp_view,
            } => {
                self.live = true;
                if slot.hbt_faithful && cross_hbt {
                    self.stop(PairEnd::Expired);
                    return;
                }
                let q = if slot.hbt_faithful { q_hbt } else { q_view };
                m.on_arrive(&mut self.st, o, q, &mut self.rng);
                self.st.leaves = o.qty;
                if let Some(t) = self.tracker.as_mut() {
                    t.front = q_view;
                }
                if !slot.hbt_faithful && cross_view {
                    let avail = self.taker_avail(slot, opp_view);
                    let d = taker_cap(slot, m.on_cross_at_live(&mut self.st, o), avail);
                    if d != FillDelta::None {
                        if let Some(t) = self.tracker.as_mut() {
                            t.through.get_or_insert(ts);
                        }
                        let qa = m.queue_ahead(&self.st);
                        let before = if slot.particles { self.psum() } else { self.st.filled };
                        self.apply_delta(slot, o, ts, d, qa, ZooFillReason::CrossedAtLive);
                        self.taker_took(slot, before, avail);
                    }
                }
                self.trace(ts);
            }
            Op::Depth {
                ts,
                prev_view,
                prev_hbt,
                new,
                flags,
            } => {
                if let (Some(t), Some((_, true))) = (self.tracker.as_mut(), slot.touch) {
                    nt_depth_step(&mut t.front, new, flags, slot.id.mode);
                }
                if fills_on || self.zombie {
                    let prev = if slot.hbt_faithful { prev_hbt } else { prev_view };
                    let qa = m.queue_ahead(&self.st);
                    let before = self.psum();
                    let d = m.on_depth(&mut self.st, o, ts, prev, new, flags, &mut self.rng);
                    self.touched = true;
                    if self.zombie {
                        self.zombie_attempt(ts, d != FillDelta::None);
                    } else if slot.particles {
                        self.record_particles(ts, before);
                        self.apply_delta(slot, o, ts, d, qa, ZooFillReason::Queue);
                    } else {
                        self.apply_delta(slot, o, ts, d, qa, ZooFillReason::Queue);
                    }
                }
                self.trace(ts);
            }
            Op::Trade {
                ts,
                qty,
                aggressor,
                hidden,
            } => {
                if hidden && !slot.hidden {
                    return;
                }
                let aggressor = if hidden { Side::None } else { aggressor };
                if let (Some(t), Some((_, gated))) = (self.tracker.as_mut(), slot.touch) {
                    let excess = if gated {
                        nt_trade_step(&mut t.front, qty, o.qty)
                    } else {
                        1.0
                    };
                    // hidden prints reach here only in faithful mode, where NT counts them as touches
                    if excess > 0.0 {
                        t.touches.push(ts);
                    }
                }
                if fills_on || self.zombie {
                    let qa = m.queue_ahead(&self.st);
                    let before = self.psum();
                    let d = m.on_trade(&mut self.st, o, ts, qty, aggressor, &mut self.rng);
                    self.touched = true;
                    if self.zombie {
                        self.zombie_attempt(ts, d != FillDelta::None);
                    } else {
                        if slot.particles {
                            self.record_particles(ts, before);
                        }
                        let reason = if slot.touch.is_some() {
                            ZooFillReason::TouchDraw
                        } else {
                            ZooFillReason::Queue
                        };
                        self.apply_delta(slot, o, ts, d, qa, reason);
                    }
                }
                self.trace(ts);
            }
            Op::Through { ts, hidden } => {
                if hidden && !slot.hidden {
                    return;
                }
                if let Some(t) = self.tracker.as_mut()
                    && !tracker_gated(slot, t)
                {
                    t.through.get_or_insert(ts);
                }
                if self.zombie {
                    let d = m.on_trade_through(&mut self.st, o);
                    self.zombie_attempt(ts, d != FillDelta::None);
                } else if fills_on {
                    let qa = m.queue_ahead(&self.st);
                    let d = m.on_trade_through(&mut self.st, o);
                    self.apply_delta(slot, o, ts, d, qa, ZooFillReason::TradeThrough);
                }
                self.trace(ts);
            }
            Op::CrossRow { ts } => {
                if !slot.hbt_faithful {
                    return;
                }
                if self.zombie {
                    let d = m.on_cross(&mut self.st, o);
                    self.zombie_attempt(ts, d != FillDelta::None);
                } else if fills_on {
                    let qa = m.queue_ahead(&self.st);
                    let d = m.on_cross(&mut self.st, o);
                    self.apply_delta(slot, o, ts, d, qa, ZooFillReason::Crossed);
                }
                self.trace(ts);
            }
            Op::Amend { ts, qty } => {
                // a priority-keeping reduction: the model's queue estimate is kept, the leaves shrink
                if slot.particles {
                    if self.st.particles.iter().all(|p| qty - p.filled <= EPS * o.lot) {
                        self.done = true;
                        self.st.done = true;
                        self.stop(PairEnd::AmendedToFilled);
                    }
                } else {
                    self.st.leaves = (qty - self.st.filled).max(0.0);
                    if self.st.leaves <= EPS * o.lot {
                        self.st.leaves = 0.0;
                        self.st.done = true;
                        self.stop(PairEnd::AmendedToFilled);
                    }
                }
                self.trace(ts);
            }
            Op::Clear { .. } => {
                if !slot.hbt_faithful {
                    self.stop(PairEnd::CensoredClear);
                }
            }
            Op::BatchEnd {
                ts,
                cross_view,
                opp_view,
                cadence0,
            } => {
                if slot.hbt_faithful {
                    return;
                }
                let avail = if cross_view {
                    self.taker_avail(slot, opp_view)
                } else {
                    0.0
                };
                let model_cross =
                    cross_view && taker_cap(slot, m.on_cross(&mut self.st.clone(), o), avail) != FillDelta::None;
                if let Some(t) = self.tracker.as_mut() {
                    // faithful: the tracker's own F gates (the model's F stops moving once it has filled)
                    let hit = if slot.corrected {
                        model_cross
                    } else {
                        cross_view && !tracker_gated(slot, t)
                    };
                    if hit {
                        t.through.get_or_insert(ts);
                    }
                }
                if model_cross && fills_on {
                    let qa = m.queue_ahead(&self.st);
                    let d = m.on_cross(&mut self.st, o);
                    let d = taker_cap(slot, d, avail);
                    let before = if slot.particles { self.psum() } else { self.st.filled };
                    self.apply_delta(slot, o, ts, d, qa, ZooFillReason::Crossed);
                    self.taker_took(slot, before, avail);
                }
                if slot.corrected && cadence0 {
                    self.st.cum_trade = 0.0;
                    for p in &mut self.st.particles {
                        p.cum_trade = 0.0;
                    }
                }
            }
        }
        self.maybe_finish();
    }
}

/// Faithful queue-gated touch tracker with queue ahead left: NautilusTrader does not fill on a trade-through or a
/// cross yet, so the analytic prediction does not jump to 1.
fn tracker_gated(slot: &ModelSlot, t: &TouchTracker) -> bool {
    matches!(slot.touch, Some((_, true))) && nt_gate_blocks(true, slot.id.mode, t.front)
}

/// Corrected mode (audit A5 as refined by E48): a cross fills at most the visible opposite
/// quantity at or through the price not taken before (`Pair::taker_avail`): on arrival into a crossed book as a
/// taker (`crossed_at_live`), a resting order later crossed by an opposite arrival as a maker at its own price for
/// min(leaves, arriving size) (`crossed`; `crate::fill_liquidity`); faithful mode keeps the full fill.
fn taker_cap(slot: &ModelSlot, d: FillDelta, opp: f64) -> FillDelta {
    if !slot.corrected {
        return d;
    }
    if opp <= 0.0 {
        return FillDelta::None;
    }
    match d {
        FillDelta::None => FillDelta::None,
        FillDelta::Full => FillDelta::Qty(opp),
        FillDelta::Qty(x) => FillDelta::Qty(x.min(opp)),
    }
}

/// Builds the `ShadowL2` view of a shadow.
pub fn shadow_l2(idx: u32, side: BookSide, price: i64, qty: f64, lot: f64) -> ShadowL2 {
    ShadowL2 {
        idx,
        side,
        price,
        qty,
        lot,
    }
}
