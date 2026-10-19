//! L2 queue models of SPEC 5.3.3 (a, b, d-i). hbt RiskAdverse / ProbQueueModel follow hftbacktest `queue.rs` and the
//! `PartialFillExchange` / `NoPartialFillExchange` fill rules at commit 5f3ec40b2afb764e0fea112f941ed85523ef4e88
//! (MIT, see NOTICE and third_party/licenses/hftbacktest-MIT.txt). The NautilusTrader-style models are
//! re-implemented from NT's documented behaviour only (no NT code). The Databento estimator follows the documented
//! behaviour of its tutorial code (not the displayed formula).
//!
//! State lives in [`QState`]; `leaves` and `filled` are maintained by the caller (the zoo runner), the models only
//! move `front`, `cum_trade`, `touches`, `particles` and the quirk bits.

use qt_schema::rows::l2flags;
use qt_types::{Side, Ts, Xoshiro256StarStar};

use crate::prob;
use crate::{Exchange, Family, FillDelta, Mode, ModelId, ModelInput, QState, QueueModel, ShadowL2};

/// Quirk bit: hbt #312 zombie (an exact or sub-half-lot remainder leaves a resting `Filled` order).
pub const QUIRK_HBT_312: u8 = 1;
/// Quirk bit: NaN queue estimate reset to the new level size.
pub const QUIRK_HBT_NAN_RESET: u8 = 2;

/// Probability function of the hbt ProbQueueModel family.
#[derive(Clone, Copy, Debug, PartialEq)]
enum ProbFn {
    Power(f64),
    Power2(f64),
    Power3(f64),
    Log,
    Log2,
}

impl ProbFn {
    fn eval(self, front: f64, back: f64) -> f64 {
        match self {
            ProbFn::Power(n) => prob::power(n, front, back),
            ProbFn::Power2(n) => prob::power2(n, front, back),
            ProbFn::Power3(n) => prob::power3(n, front, back),
            ProbFn::Log => prob::log(front, back),
            ProbFn::Log2 => prob::log2(front, back),
        }
    }
}

/// hbt `RiskAdverseQueueModel` (`prob = None`) or `ProbQueueModel<P>` with the exchange fill rule.
pub struct HbtModel {
    id: ModelId,
    prob: Option<ProbFn>,
}

impl HbtModel {
    /// Builds the model of an hbt L2 family id.
    pub fn new(id: ModelId) -> Self {
        let n = id.param("n").unwrap_or(f64::NAN);
        let prob = match id.family {
            Family::HbtPower => Some(ProbFn::Power(n)),
            Family::HbtPower2 => Some(ProbFn::Power2(n)),
            Family::HbtPower3 => Some(ProbFn::Power3(n)),
            Family::HbtLog => Some(ProbFn::Log),
            Family::HbtLog2 => Some(ProbFn::Log2),
            _ => None,
        };
        Self { id, prob }
    }

    fn faithful(&self) -> bool {
        self.id.mode == Mode::Faithful
    }

    /// `is_filled` followed by the exchange rule (SPEC 5.3.3 a; the zombie condition as hbt's own code behaves).
    fn exec(&self, s: &mut QState, o: &ShadowL2) -> FillDelta {
        let lot = o.lot;
        let q = if self.faithful() {
            let lots = prob::hbt_exec_lots(s.front, lot);
            if lots <= 0 {
                return FillDelta::None;
            }
            s.front = 0.0;
            lots as f64 * lot
        } else {
            let lots = ((-s.front).max(0.0) / lot + 1e-9).floor() as i64;
            if lots <= 0 {
                return FillDelta::None;
            }
            s.front = 0.0;
            lots as f64 * lot
        };
        match self.id.exchange {
            Exchange::NoPartial => FillDelta::Full,
            Exchange::Partial => {
                if self.faithful() {
                    if q > s.leaves {
                        FillDelta::Full
                    } else {
                        // hbt PartialFillExchange: the order is removed only when q > leaves; status becomes
                        // Filled when round((leaves - q) / lot) == 0, leaving a resting zombie (#312).
                        if ((s.leaves - q) / lot).round() == 0.0 {
                            s.quirk |= QUIRK_HBT_312;
                        }
                        FillDelta::Qty(q)
                    }
                } else if q >= s.leaves {
                    FillDelta::Full
                } else {
                    FillDelta::Qty(q)
                }
            }
        }
    }

    /// hbt `depth` (faithful) or the corrected update (SPEC 5.3.3 a, b).
    pub fn depth_update(&self, s: &mut QState, prev: f64, new: f64) {
        let Some(p) = self.prob else {
            if self.faithful() {
                s.front = s.front.min(new);
            } else {
                s.front = s.front.max(0.0).min(new);
            }
            return;
        };
        let mut chg = prev - new;
        chg -= s.cum_trade;
        s.cum_trade = 0.0;
        if self.faithful() {
            if chg < 0.0 {
                s.front = s.front.min(new);
                return;
            }
            let front = s.front;
            let back = prev - front;
            let mut pi = p.eval(front, back);
            if pi.is_infinite() {
                pi = 1.0;
            }
            let est = front - (1.0 - pi) * chg + (back - pi * chg).min(0.0);
            if est.is_nan() {
                s.quirk |= QUIRK_HBT_NAN_RESET;
            }
            s.front = est.min(new);
        } else {
            if chg < 0.0 {
                s.front = s.front.max(0.0).min(new);
                return;
            }
            let front = s.front.max(0.0);
            if front == 0.0 {
                s.front = front.min(new);
                return;
            }
            let back = (prev - front).max(0.0);
            let pi = if back == 0.0 {
                0.0
            } else {
                let x = p.eval(front, back);
                let x = if x.is_finite() { x } else { back / (front + back) };
                x.clamp(0.0, 1.0)
            };
            let est = front - (1.0 - pi) * chg + (back - pi * chg).min(0.0);
            s.front = est.max(0.0).min(new);
        }
    }
}

impl QueueModel for HbtModel {
    fn id(&self) -> &ModelId {
        &self.id
    }

    fn input(&self) -> ModelInput {
        ModelInput::L2
    }

    fn on_arrive(&self, s: &mut QState, _o: &ShadowL2, visible_level_qty: f64, _rng: &mut Xoshiro256StarStar) {
        s.front = visible_level_qty;
        s.cum_trade = 0.0;
    }

    fn on_trade(
        &self,
        s: &mut QState,
        o: &ShadowL2,
        _ts: Ts,
        trade_qty: f64,
        _aggressor: Side,
        _rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        s.front -= trade_qty;
        if self.prob.is_some() {
            s.cum_trade += trade_qty;
        }
        self.exec(s, o)
    }

    fn on_depth(
        &self,
        s: &mut QState,
        _o: &ShadowL2,
        _ts: Ts,
        prev_qty: f64,
        new_qty: f64,
        _flags: u8,
        _rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        self.depth_update(s, prev_qty, new_qty);
        FillDelta::None
    }

    fn queue_ahead(&self, s: &QState) -> f64 {
        s.front
    }

    fn pi_behind(&self, front: f64, back: f64) -> Option<f64> {
        self.prob.map(|p| p.eval(front, back))
    }
}

/// NT L2 queue-position step (SPEC 5.3.3 d) on a trade: returns the excess volume beyond the queue (0 = none).
pub fn nt_trade_step(front: &mut f64, v: f64, leaves: f64) -> f64 {
    let consumed = v.min(*front);
    let remaining = v - consumed;
    if remaining <= 0.0 {
        *front = (*front - v).max(0.0);
        0.0
    } else {
        *front = 0.0;
        remaining.min(leaves)
    }
}

/// NT L2 queue-position step on a depth / snapshot row at the shadow's level.
///
/// Faithful (as NautilusTrader 1.231.0 behaves on a delta feed, cross-checked by `scripts/nt_crosscheck`): a size
/// update never moves the queue position (only at-price trades shrink it, [`nt_trade_step`]); a deleted level or a
/// snapshot row resets it to 0 (front of the queue). Corrected (queuetruth's own semantics): `F = min(F, new)`, a
/// delete resets to 0 unless the level only scrolled out of view.
pub fn nt_depth_step(front: &mut f64, new: f64, flags: u8, mode: Mode) {
    if mode == Mode::Faithful {
        if new == 0.0 || flags & l2flags::SNAPSHOT != 0 {
            *front = 0.0;
        }
        return;
    }
    if flags & l2flags::SNAPSHOT != 0 {
        *front = front.min(new);
    } else if new == 0.0 {
        if !(mode == Mode::Corrected && flags & l2flags::OUT_OF_VIEW != 0) {
            *front = 0.0;
        }
    } else {
        *front = front.min(new);
    }
}

/// Faithful NT queue gate: a queue-tracking NT family does not fill on a trade-through or a cross while queue ahead
/// remains at its price (NautilusTrader 1.231.0 checks the queue before any fill at the order's level).
pub fn nt_gate_blocks(queue: bool, mode: Mode, front: f64) -> bool {
    queue && mode == Mode::Faithful && front > 0.0
}

/// NautilusTrader-style models: `nt_queue_l2`, `nt_prob_touch[p]`, `nt_queue_l2_prob_touch[p]`.
///
/// Faithful mode follows what NautilusTrader 1.231.0 does on an `OrderBookDelta` feed (behaviour re-implemented from
/// observation and documentation, no NT code; agreement checked by `scripts/nt_crosscheck`):
/// - a hidden print (`NO_AGGRESSOR`) at the order's price is a touch for both sides (NT moves both book sides to the
///   trade price) and, in the queue families, decrements the queue like a displayed print;
/// - book size updates never shrink the queue ahead; only at-price trades do, and a level delete or snapshot resets
///   it to 0 ([`nt_depth_step`]);
/// - while queue ahead is positive, trade-throughs and crosses do not fill the queue families ([`nt_gate_blocks`]).
///
/// Corrected mode keeps queuetruth's semantics (hidden prints ignored, `F = min(F, new)`, no gate).
///
/// Feed note: NautilusTrader resets `queue_position` (queue ahead to 0) whenever an `OrderBookDepth10` arrives, so on
/// a depth-10 feed its queue families fill like `nt_prob_touch` (the `d10_*` rows of the cross-check report). These
/// models, and the cross-check agreement, describe NT on an `OrderBookDelta` feed only.
pub struct NtModel {
    id: ModelId,
    queue: bool,
    p: Option<f64>,
}

impl NtModel {
    /// Builds an NT-family model.
    pub fn new(id: ModelId) -> Self {
        let (queue, p) = match id.family {
            Family::NtQueueL2 => (true, None),
            Family::NtProbTouch => (false, Some(id.param("p").unwrap_or(1.0))),
            _ => (true, Some(id.param("p").unwrap_or(1.0))),
        };
        Self { id, queue, p }
    }

    fn draw(p: f64, rng: &mut Xoshiro256StarStar) -> bool {
        let u = rng.next_f64();
        p >= 1.0 || (p > 0.0 && u < p)
    }
}

impl QueueModel for NtModel {
    fn id(&self) -> &ModelId {
        &self.id
    }

    fn input(&self) -> ModelInput {
        ModelInput::L2
    }

    fn on_arrive(&self, s: &mut QState, _o: &ShadowL2, visible_level_qty: f64, _rng: &mut Xoshiro256StarStar) {
        s.front = visible_level_qty;
        s.touches = 0;
    }

    fn on_trade(
        &self,
        s: &mut QState,
        _o: &ShadowL2,
        _ts: Ts,
        trade_qty: f64,
        aggressor: Side,
        rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        let displayed = aggressor != Side::None;
        let excess = if self.queue {
            nt_trade_step(&mut s.front, trade_qty, s.leaves)
        } else {
            0.0
        };
        match self.p {
            None => {
                if excess > 0.0 {
                    FillDelta::Qty(excess)
                } else {
                    FillDelta::None
                }
            }
            Some(p) => {
                // corrected mode never receives hidden prints; faithful NT counts them as touches
                if !displayed && self.id.mode == Mode::Corrected {
                    return FillDelta::None;
                }
                s.touches += 1;
                let ok = Self::draw(p, rng);
                if !ok {
                    return FillDelta::None;
                }
                if self.queue {
                    if excess > 0.0 {
                        FillDelta::Qty(excess.min(s.leaves))
                    } else {
                        FillDelta::None
                    }
                } else {
                    FillDelta::Qty(trade_qty.min(s.leaves))
                }
            }
        }
    }

    fn on_depth(
        &self,
        s: &mut QState,
        _o: &ShadowL2,
        _ts: Ts,
        _prev_qty: f64,
        new_qty: f64,
        flags: u8,
        _rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        if self.queue {
            nt_depth_step(&mut s.front, new_qty, flags, self.id.mode);
        }
        FillDelta::None
    }

    fn on_trade_through(&self, s: &mut QState, _o: &ShadowL2) -> FillDelta {
        if nt_gate_blocks(self.queue, self.id.mode, s.front) {
            FillDelta::None
        } else {
            FillDelta::Full
        }
    }

    fn on_cross(&self, s: &mut QState, _o: &ShadowL2) -> FillDelta {
        if nt_gate_blocks(self.queue, self.id.mode, s.front) {
            FillDelta::None
        } else {
            FillDelta::Full
        }
    }

    fn queue_ahead(&self, s: &QState) -> f64 {
        s.front
    }
}

/// `databento_mc[k, particles]` (SPEC 5.3.3 g).
pub struct DatabentoModel {
    id: ModelId,
    k: f64,
    n: usize,
}

impl DatabentoModel {
    /// Builds the Monte Carlo model.
    pub fn new(id: ModelId) -> Self {
        let k = id.param("k").unwrap_or(-0.8);
        let n = id.particles().max(1);
        Self { id, k, n }
    }

    fn bias(&self, u: f64) -> f64 {
        if self.k <= 0.0 {
            u.powf(1.0 + self.k)
        } else {
            1.0 - (1.0 - u).powf(1.0 - self.k)
        }
    }

    fn fill_particle(p: &mut crate::Particle, q: f64, qty: f64, ts: Ts) {
        let leaves = qty - p.filled;
        let q = q.min(leaves);
        if q > 0.0 {
            p.filled += q;
            if p.first_fill.is_none() {
                p.first_fill = Some(ts);
            }
        }
    }
}

impl QueueModel for DatabentoModel {
    fn id(&self) -> &ModelId {
        &self.id
    }

    fn input(&self) -> ModelInput {
        ModelInput::L2
    }

    fn on_arrive(&self, s: &mut QState, _o: &ShadowL2, visible_level_qty: f64, _rng: &mut Xoshiro256StarStar) {
        s.front = visible_level_qty;
        s.particles = vec![
            crate::Particle {
                front: visible_level_qty,
                cum_trade: 0.0,
                filled: 0.0,
                first_fill: None
            };
            self.n
        ];
    }

    fn on_trade(
        &self,
        s: &mut QState,
        o: &ShadowL2,
        ts: Ts,
        trade_qty: f64,
        _aggressor: Side,
        _rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        let lot = o.lot;
        let corrected = self.id.mode == Mode::Corrected;
        for p in &mut s.particles {
            if corrected {
                p.front -= trade_qty;
                p.cum_trade += trade_qty;
                let q = ((-p.front).max(0.0) / lot).floor() * lot;
                Self::fill_particle(p, q, o.qty, ts);
                p.front = p.front.max(0.0);
            } else {
                let q = ((trade_qty - p.front).max(0.0) / lot).floor() * lot;
                Self::fill_particle(p, q, o.qty, ts);
            }
        }
        FillDelta::None
    }

    fn on_depth(
        &self,
        s: &mut QState,
        _o: &ShadowL2,
        _ts: Ts,
        prev_qty: f64,
        new_qty: f64,
        _flags: u8,
        rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        if self.id.mode == Mode::Faithful {
            let d = prev_qty - new_qty;
            for p in &mut s.particles {
                if d > 0.0 {
                    if new_qty == 0.0 {
                        p.front = 0.0;
                    } else {
                        let x = p.front / new_qty;
                        let u = rng.next_f64();
                        if self.bias(u) < x {
                            p.front -= d;
                        }
                    }
                }
                p.front = p.front.max(0.0).min(new_qty);
            }
        } else {
            for p in &mut s.particles {
                let mut chg = prev_qty - new_qty;
                chg -= p.cum_trade;
                p.cum_trade = 0.0;
                if chg > 0.0 {
                    let back = prev_qty - p.front;
                    let x = p.front / (p.front + back);
                    let pf = prob::db_p_front(self.k, x);
                    let u = rng.next_f64();
                    if u < pf {
                        p.front -= chg;
                    }
                }
                p.front = p.front.max(0.0).min(new_qty);
            }
        }
        FillDelta::None
    }

    fn queue_ahead(&self, s: &QState) -> f64 {
        if s.particles.is_empty() {
            return s.front;
        }
        s.particles.iter().map(|p| p.front).fold(0.0, |a, b| a + b) / s.particles.len() as f64
    }

    fn p_filled(&self, s: &QState) -> Option<f64> {
        if s.particles.is_empty() {
            return Some(0.0);
        }
        Some(s.particles.iter().filter(|p| p.filled > 0.0).count() as f64 / s.particles.len() as f64)
    }

    fn pi_behind(&self, front: f64, back: f64) -> Option<f64> {
        Some(1.0 - prob::db_p_front(self.k, front / (front + back)))
    }
}

/// Optimistic (`front_of_queue`) and pessimistic (`trade_through_only`) bounds (SPEC 5.3.3 h, i), built to bracket
/// the exact engine shadow by shadow (O-ZOO-6): `front_of_queue` has no queue ahead and fills on every print that
/// can reach the shadow, displayed or hidden (A3d), at or through its price, and on every cross; the exact fill
/// paths are a subset of these. `trade_through_only` fills only on a displayed trade-through: never on a cross or
/// on arrival into a crossed book (the exact engine gives that size to real orders with priority first, E36(a),
/// so a cross need not reach the shadow) and never on a hidden print (A3d may decline it).
pub struct BoundModel {
    id: ModelId,
    front_of_queue: bool,
}

impl BoundModel {
    /// Builds a bound model.
    pub fn new(id: ModelId) -> Self {
        let front_of_queue = id.family == Family::FrontOfQueue;
        Self { id, front_of_queue }
    }
}

impl QueueModel for BoundModel {
    fn id(&self) -> &ModelId {
        &self.id
    }

    fn input(&self) -> ModelInput {
        ModelInput::L2
    }

    fn on_arrive(&self, s: &mut QState, _o: &ShadowL2, visible_level_qty: f64, _rng: &mut Xoshiro256StarStar) {
        s.front = if self.front_of_queue { 0.0 } else { visible_level_qty };
    }

    fn on_trade(
        &self,
        _s: &mut QState,
        o: &ShadowL2,
        _ts: Ts,
        trade_qty: f64,
        _aggressor: Side,
        _rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        if !self.front_of_queue {
            return FillDelta::None;
        }
        let q = (trade_qty / o.lot).floor() * o.lot;
        if q > 0.0 { FillDelta::Qty(q) } else { FillDelta::None }
    }

    fn on_depth(
        &self,
        s: &mut QState,
        _o: &ShadowL2,
        _ts: Ts,
        _prev_qty: f64,
        new_qty: f64,
        _flags: u8,
        _rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        if !self.front_of_queue {
            s.front = s.front.min(new_qty);
        }
        FillDelta::None
    }

    fn on_cross(&self, _s: &mut QState, _o: &ShadowL2) -> FillDelta {
        if self.front_of_queue {
            FillDelta::Full
        } else {
            FillDelta::None
        }
    }

    fn on_cross_at_live(&self, _s: &mut QState, _o: &ShadowL2) -> FillDelta {
        if self.front_of_queue {
            FillDelta::Full
        } else {
            FillDelta::None
        }
    }

    fn queue_ahead(&self, s: &QState) -> f64 {
        s.front
    }
}
