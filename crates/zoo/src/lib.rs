//! Queue-model zoo (SPEC 4.1 `qt_zoo`, 5.3): hftbacktest RiskAdverse / ProbQueue / L3FIFO, NautilusTrader-style
//! queue position and touch models, Databento Monte Carlo, bounds; faithful and corrected modes; the `ModelId`
//! grammar; the zoo runner (exact engine + L2Builder + models in one pass) and the particle simulator.
//!
//! Modules: `prob` (probability functions), `model_id` (grammar, `default_models`), `models` (L2 families),
//! `l3` (`hbt_l3_fifo`), `view` (visible book, hbt depth mirror), `pair` (per-pair dispatch rules), `sim`
//! (per-partition simulation), `runner` (`run_zoo`, `particle_simulator`).

#![forbid(unsafe_code)]

pub mod l3;
pub mod model_id;
pub mod models;
pub mod pair;
pub mod prob;
pub mod runner;
pub mod sim;
pub mod view;

#[cfg(test)]
mod tests;

pub use model_id::{default_models, expand_models};
pub use runner::{particle_simulator, run_zoo};

use std::collections::BTreeMap;

use qt_book::{Book, Effect};
use qt_types::QtResult;
use qt_types::{BookSide, Dur, MboEvent, Px, Side, Ts, Xoshiro256StarStar};

/// Fill decided by a model callback.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum FillDelta {
    /// No fill.
    None,
    /// Fill `min(q, leaves)`.
    Qty(f64),
    /// Fill the leaves.
    Full,
}

/// What a model consumes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ModelInput {
    /// The synthesized L2 view.
    L2,
    /// The MBO stream (hbt L3FIFO).
    Mbo,
}

/// Faithful (hbt semantics including quirks) or corrected.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Mode {
    /// hbt `5f3ec40` bit for bit.
    Faithful,
    /// Quirks fixed.
    Corrected,
}

/// Exchange fill rule.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Exchange {
    /// Partial fills.
    Partial,
    /// Whole-order fills.
    NoPartial,
}

/// Model family (canonical names in SPEC 5.3.2).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Family {
    /// `hbt_risk_adverse`.
    HbtRiskAdverse,
    /// `hbt_power`.
    HbtPower,
    /// `hbt_power2`.
    HbtPower2,
    /// `hbt_power3`.
    HbtPower3,
    /// `hbt_log`.
    HbtLog,
    /// `hbt_log2`.
    HbtLog2,
    /// `hbt_l3_fifo`.
    HbtL3Fifo,
    /// `nt_queue_l2`.
    NtQueueL2,
    /// `nt_prob_touch`.
    NtProbTouch,
    /// `nt_queue_l2_prob_touch`.
    NtQueueL2ProbTouch,
    /// `databento_mc`.
    DatabentoMc,
    /// `front_of_queue`.
    FrontOfQueue,
    /// `trade_through_only`.
    TradeThroughOnly,
}

/// A model with its parameters (`<family>[<k>=<v>;...]`, SPEC 5.3.2).
#[derive(Clone, Debug, PartialEq)]
pub struct ModelId {
    /// Family.
    pub family: Family,
    /// Numeric parameters (`n`, `k`, `p`, `particles`).
    pub params: BTreeMap<String, f64>,
    /// Mode.
    pub mode: Mode,
    /// Exchange.
    pub exchange: Exchange,
}

/// A shadow as seen by an L2 model.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ShadowL2 {
    /// Shadow index.
    pub idx: u32,
    /// Side.
    pub side: BookSide,
    /// Price.
    pub price: Px,
    /// Quantity (size units).
    pub qty: f64,
    /// Lot size (size units).
    pub lot: f64,
}

/// One Monte Carlo particle.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Particle {
    /// Queue ahead.
    pub front: f64,
    /// Trades since the last depth update.
    pub cum_trade: f64,
    /// Filled quantity.
    pub filled: f64,
    /// First fill time.
    pub first_fill: Option<Ts>,
}

/// Per (model, shadow) state.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct QState {
    /// Queue ahead estimate.
    pub front: f64,
    /// Trades since the last depth update.
    pub cum_trade: f64,
    /// Remaining quantity.
    pub leaves: f64,
    /// Filled quantity.
    pub filled: f64,
    /// Pair done.
    pub done: bool,
    /// Touches seen.
    pub touches: u32,
    /// Particles (particle models).
    pub particles: Vec<Particle>,
    /// Quirk bitset: 1 hbt_312, 2 hbt_nan_reset.
    pub quirk: u8,
}

/// One queue model (SPEC 5.3.3); callbacks receive the pair state and the shadow.
pub trait QueueModel: Send + Sync {
    /// Canonical id.
    fn id(&self) -> &ModelId;
    /// Input kind.
    fn input(&self) -> ModelInput;
    /// Activation with the visible level quantity before the batch.
    fn on_arrive(&self, s: &mut QState, o: &ShadowL2, visible_level_qty: f64, rng: &mut Xoshiro256StarStar);
    /// At-price trade with an aggressor opposite to the shadow.
    fn on_trade(
        &self,
        s: &mut QState,
        o: &ShadowL2,
        ts: Ts,
        trade_qty: f64,
        aggressor: Side,
        rng: &mut Xoshiro256StarStar,
    ) -> FillDelta;
    /// Depth or snapshot row at the shadow's level.
    #[allow(clippy::too_many_arguments)]
    fn on_depth(
        &self,
        s: &mut QState,
        o: &ShadowL2,
        ts: Ts,
        prev_qty: f64,
        new_qty: f64,
        flags: u8,
        rng: &mut Xoshiro256StarStar,
    ) -> FillDelta;
    /// Trade through the price.
    fn on_trade_through(&self, _s: &mut QState, _o: &ShadowL2) -> FillDelta {
        FillDelta::Full
    }
    /// Opposite best at or through the price.
    fn on_cross(&self, _s: &mut QState, _o: &ShadowL2) -> FillDelta {
        FillDelta::Full
    }
    /// Opposite best at or through the price when the shadow goes live (`crossed_at_live`, before the corrected
    /// mode's taker cap).
    fn on_cross_at_live(&self, _s: &mut QState, _o: &ShadowL2) -> FillDelta {
        FillDelta::Full
    }
    /// MBO record (MBO-input families).
    fn on_mbo(&self, _s: &mut QState, _o: &ShadowL2, _e: &MboEvent, _effect: &Effect, _book: &Book) -> FillDelta {
        FillDelta::None
    }
    /// Queue-ahead estimate.
    fn queue_ahead(&self, s: &QState) -> f64;
    /// Particle fraction with a fill.
    fn p_filled(&self, _s: &QState) -> Option<f64> {
        None
    }
    /// Probability that a decrease is behind the order.
    fn pi_behind(&self, _front: f64, _back: f64) -> Option<f64> {
        None
    }
}

/// Liquidity of a zoo fill (`qt.zoo_fills.liquidity`, `qt.zoo_predictions.liquidity`; audit ruling A5 as refined by
/// E48): only an order that goes live into an already-crossed book takes liquidity
/// (`crossed_at_live` = taker); a resting order later crossed by an opposite-side arrival is filled at its own
/// resting price as a maker (`crossed` = maker), as are queue, trade-through and touch fills (the same mapping as
/// `qt_schema::rows::ZooFillReason::liquidity`).
pub const fn fill_liquidity(reason: qt_schema::rows::ZooFillReason) -> qt_schema::rows::Liquidity {
    match reason {
        qt_schema::rows::ZooFillReason::CrossedAtLive => qt_schema::rows::Liquidity::Taker,
        _ => qt_schema::rows::Liquidity::Maker,
    }
}

/// Instantiates a model (validated id: parameters and ranges as `ModelId::parse_with`).
pub fn build(id: &ModelId) -> QtResult<Box<dyn QueueModel>> {
    let id = ModelId::parse_with(&id.to_string(), id.mode, id.exchange, model_id::DEFAULT_PARTICLES)?;
    Ok(match id.family {
        Family::HbtRiskAdverse
        | Family::HbtPower
        | Family::HbtPower2
        | Family::HbtPower3
        | Family::HbtLog
        | Family::HbtLog2 => Box::new(models::HbtModel::new(id)),
        Family::HbtL3Fifo => Box::new(l3::L3Model::new(id)),
        Family::NtQueueL2 | Family::NtProbTouch | Family::NtQueueL2ProbTouch => Box::new(models::NtModel::new(id)),
        Family::DatabentoMc => Box::new(models::DatabentoModel::new(id)),
        Family::FrontOfQueue | Family::TradeThroughOnly => Box::new(models::BoundModel::new(id)),
    })
}

/// Survival curve of one shadow on the simulator horizons.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ShadowCurve {
    /// Shadow index.
    pub shadow_idx: u32,
    /// Survival on each horizon.
    pub survival: Vec<f64>,
    /// Interval of the exact first fill.
    pub truth_interval: Option<usize>,
    /// First interval not fully observed.
    pub censored_from: Option<usize>,
}

/// Particle simulator used by the survival calibration (C2).
pub trait ParticleSimulator: Sync {
    /// Horizons.
    fn horizons(&self) -> &[Dur];
    /// Simulates every train shadow for one parameter value with common random numbers.
    fn simulate(&self, theta: f64, seed: u64) -> QtResult<Vec<ShadowCurve>>;
}
