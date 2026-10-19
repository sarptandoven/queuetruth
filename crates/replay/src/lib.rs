//! Replay engine (SPEC 4.1 `qt_replay`, 5.1, 5.2): event atomicity at `F_LAST`, sinks and observers, shadow orders
//! with per-fill timelines, trajectories, amends and truth decomposition, rank oracle, real-order extraction, data
//! quality, Parquet sinks, `run_dataset` (parallel by partition), sampling, mirrors, `why` and `Session`.
//!
//! An event is the maximal run of records of one instrument ending with `flags::LAST`; books are consistent only at
//! event end, where BBO/depth rows, shadow fill decisions and invariant checks happen. Shadow activation, cancels and
//! amends happen at event start. Records are applied in feed order, never re-sorted.

#![forbid(unsafe_code)]

mod engine;
mod oracle;
mod pairing;
mod real_orders;
mod run;
mod sample;
mod session;
mod shadow;
mod util;
mod why;

pub mod dq;
pub mod parity;
pub mod sinks;

use qt_book::{Book, Effect, Invariants};
use qt_schema::json;
use qt_schema::rows::{
    BboRow, DepthRow, EndState, RankTraceRow, RealOrderRow, ShadowFillRow, ShadowResultRow, TradeRow, TrajectoryRow,
};
use qt_types::{Dur, LatencyModel, MboEvent, Px, QtResult, Qty, Ts, VenueRules};

pub use engine::Engine;
pub use run::{
    ReplayOptions, for_each_partition, ingest_dq, partition_gaps, run_dataset, run_dataset_with, run_partition,
    split_shadows, strict_violations,
};
pub use sample::{mirror_real_orders, sample_shadows};
pub use session::Session;
pub use why::{trace_shadow, trace_shadow_excluding, why};

/// BBO output mode.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum BboMode {
    /// No BBO rows.
    None,
    /// Rows only when any of the 6 values changed.
    #[default]
    Change,
    /// A row after every event.
    Full,
}

/// Trajectory output mode.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum TrajectoryMode {
    /// No trajectory rows.
    None,
    /// Rows where queue ahead, rank or filled changed, plus live/end rows.
    #[default]
    Ahead,
    /// Additionally every event touching the level.
    Full,
}

/// Engine configuration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EngineConfig {
    /// Venue rules.
    pub rules: VenueRules,
    /// Shadow latency.
    pub latency: LatencyModel,
    /// Run seed (latency jitter).
    pub seed: u64,
    /// Rank oracle on.
    pub rank_oracle: bool,
    /// Sweep grouping gap.
    pub sweep_gap_ns: Dur,
    /// BBO mode.
    pub bbo: BboMode,
    /// Depth levels (0 = none).
    pub depth_levels: u16,
    /// Trajectory mode.
    pub trajectory: TrajectoryMode,
    /// Race window.
    pub race_window_ns: Dur,
    /// Extract real orders.
    pub real_orders: bool,
    /// Strict mode.
    pub strict: bool,
}

impl EngineConfig {
    /// Defaults of `ReplaySpec` for the given rules (no latency, seed 0, oracle off, 2 ms sweep gap, change-only BBO,
    /// no depth, `ahead` trajectory, no race window, no real orders, not strict).
    pub fn new(rules: VenueRules) -> Self {
        Self {
            rules,
            latency: LatencyModel::default(),
            seed: 0,
            rank_oracle: false,
            sweep_gap_ns: 2_000_000,
            bbo: BboMode::Change,
            depth_levels: 0,
            trajectory: TrajectoryMode::Ahead,
            race_window_ns: 0,
            real_orders: false,
            strict: false,
        }
    }
}

/// Shadow fill rule.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum FillRule {
    /// Audit rulings A3-A5 with E36 and oracle rulings A3d / A4b (default): queue ahead is the
    /// exact displayed size of the real orders ahead, maintained from their own records (a duplicate add outside a
    /// flagged snapshot re-queues the order behind the shadow); fills come from executions of real orders behind
    /// the shadow at its price executed at that displayed price, executions of orders resting strictly through its
    /// price (and executed through it), hidden prints per `HiddenPrintPolicy`, and taker fills walking the opposite
    /// book when it crosses the shadow's price, after the real same-side orders with priority. Prints never shrink
    /// queue ahead.
    #[default]
    Exact,
    /// et / SPEC 5.2.5 as written: at-price prints deplete `ahead - traded`, prints through the price fill, a crossed
    /// book fills the remainder at the shadow price (et parity runs, RD-5).
    Et,
}

/// Hidden (side-`N`) prints on venues with hidden orders (audit ruling A3; `Exact` rule only).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum HiddenPrintPolicy {
    /// Hidden prints never fill shadows.
    Ignore,
    /// A hidden print at the shadow's price fills it when the book at the print shows no displayed interest at or
    /// through that price on either side (displayed beats hidden at a price; a locked or crossed price is not
    /// continuous matching; oracle ruling A3d, superseding the quote-rule reading of E36(c)).
    Behind,
    /// `Behind`, plus a usable hidden print (same condition) strictly through the shadow's price is a
    /// trade-through. Default on Nasdaq.
    BehindAndThrough,
}

impl HiddenPrintPolicy {
    /// The default for a venue: `BehindAndThrough` on Nasdaq, `Ignore` elsewhere.
    pub fn default_for(rules: &VenueRules) -> Self {
        if rules.venue == qt_types::Venue::Nasdaq {
            Self::BehindAndThrough
        } else {
            Self::Ignore
        }
    }
}

/// Shadow diagnostics that have no column in the frozen schemas (reported in `ReplaySummary.warnings`).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ShadowDiagnostics {
    /// Fill events that used hidden-print volume at the shadow's price (A3).
    pub hidden_print_fills: u64,
    /// Hidden prints at a shadow's price declined because displayed interest rested at or through the print price
    /// (A3d), counted per shadow.
    pub hidden_at_px_with_display_ahead: u64,
    /// Taker fill rows from a crossed book (A5; reason `crossed` / `crossed_at_live`, liquidity taker).
    pub taker_fills: u64,
    /// Events in which a shadow was crossed but no opposite size was left after the orders with priority (A5).
    pub crossed_unfilled: u64,
    /// Fill events from executions behind the shadow while displayed queue ahead remained (priority anomaly).
    pub behind_exec_with_ahead: u64,
    /// `exclude_order_ids` requested (A1).
    pub excluded_requested: u64,
    /// Requested ids found in the stream (distinct over partitions after `add`).
    pub excluded_matched: u64,
    /// The (instrument_id, order_id) behind `excluded_matched`, sorted.
    pub excluded_matched_ids: Vec<(u32, u64)>,
    /// Records dropped from the tape for excluded orders.
    pub excluded_records: u64,
    /// Excluded orders replaced by their own shadow and kept on the tape (A1 twin linking,
    /// `Engine::set_twin_linking`).
    pub twins_linked: u64,
    /// Twin executions at the shadow's displayed price credited while the shadow's displayed queue ahead was still
    /// positive after the event (the queue accounting missed the twin's real priority; 0 when it reproduces it).
    pub twin_fills_with_ahead: u64,
    /// Twin executions at a price other than the twin's displayed price (display-slid orders that execute at their
    /// limit), credited to the twin's shadow at that price.
    pub twin_fills_off_display: u64,
}

impl ShadowDiagnostics {
    /// Adds another partition's counters (`excluded_requested` is per run: the maximum is kept).
    pub fn add(&mut self, o: &Self) {
        self.hidden_print_fills += o.hidden_print_fills;
        self.hidden_at_px_with_display_ahead += o.hidden_at_px_with_display_ahead;
        self.taker_fills += o.taker_fills;
        self.crossed_unfilled += o.crossed_unfilled;
        self.behind_exec_with_ahead += o.behind_exec_with_ahead;
        self.excluded_requested = self.excluded_requested.max(o.excluded_requested);
        self.excluded_matched_ids.extend(o.excluded_matched_ids.iter().copied());
        self.excluded_matched_ids.sort_unstable();
        self.excluded_matched_ids.dedup();
        self.excluded_matched = self.excluded_matched_ids.len() as u64;
        self.excluded_records += o.excluded_records;
        self.twins_linked += o.twins_linked;
        self.twin_fills_with_ahead += o.twin_fills_with_ahead;
        self.twin_fills_off_display += o.twin_fills_off_display;
    }
}

/// Planned (exogenous) end of a shadow (audit ruling A2): the earlier of its cancel effective time and its session
/// close, known before replay and independent of the outcome; truth labels at horizon h exist iff
/// `planned_end >= ts_live + h`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlannedEnd {
    /// Shadow id.
    pub shadow_id: String,
    /// Planned end (`None`: no cancel and no known session close).
    pub planned_end: Option<Ts>,
    /// `cancel`, `session_close` or `none`.
    pub kind: &'static str,
}

/// Liquidity of a shadow fill (A5 as refined by E48): `taker` only for
/// `crossed_at_live` (the shadow arrives into a crossing book and walks the opposite levels); a resting shadow
/// crossed later by an opposite arrival (`crossed`) is the `maker` at its own price, like `queue_depleted` and
/// `trade_through`. Returns the schema value name.
pub fn fill_liquidity(reason: qt_schema::rows::FillReason) -> &'static str {
    match shadow_fill_liquidity(reason) {
        qt_schema::rows::Liquidity::Taker => "taker",
        qt_schema::rows::Liquidity::Maker => "maker",
    }
}

/// `fill_liquidity` as the schema enum (the value written to `qt.shadow_fills.liquidity`).
pub fn shadow_fill_liquidity(reason: qt_schema::rows::FillReason) -> qt_schema::rows::Liquidity {
    use qt_schema::rows::{FillReason, Liquidity};
    match reason {
        FillReason::CrossedAtLive => Liquidity::Taker,
        FillReason::Crossed | FillReason::QueueDepleted | FillReason::TradeThrough => Liquidity::Maker,
    }
}

/// Shadow lifecycle status.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ShadowStatus {
    /// Not live yet.
    Pending,
    /// Live.
    Live,
    /// Ended.
    Done,
}

/// Snapshot of one shadow's state.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ShadowState {
    /// Status.
    pub status: ShadowStatus,
    /// Resolved price.
    pub price: Option<Px>,
    /// Target activation time.
    pub ts_live: Ts,
    /// Queue ahead.
    pub queue_ahead: Option<Qty>,
    /// Rank.
    pub rank: Option<u32>,
    /// Level quantity.
    pub level_qty: Option<Qty>,
    /// Filled quantity.
    pub filled: Qty,
    /// Current quantity.
    pub qty: Qty,
    /// End state once done.
    pub end_state: Option<EndState>,
}

/// Replay counters (et `Stats`).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    /// Records.
    pub records: u64,
    /// Events.
    pub events: u64,
    /// Trades.
    pub trades: u64,
    /// Instruments.
    pub instruments: u64,
    /// Partitions.
    pub partitions: u64,
    /// Snapshot records.
    pub snapshot_records: u64,
    /// Records with ts_event going backwards.
    pub out_of_order_ts_event: u64,
    /// Sequence gaps.
    pub sequence_gaps: u64,
    /// Sequence regressions.
    pub sequence_regressions: u64,
    /// Partitions ending inside an event.
    pub unterminated_events: u64,
    /// Files without a leading snapshot (CME).
    pub files_without_snapshot: u64,
    /// Hidden prints.
    pub hidden_prints: u64,
    /// Off-price executions.
    pub off_price_executions: u64,
    /// `C`/`M` of an order whose `F` was left unpaired shortly before (audit minor 24; `pairing` module).
    pub removal_after_unpaired_fill: u64,
    /// ITCH cross prints (OC-28; `Engine::set_cross_prints`).
    pub cross_prints: u64,
}

impl Stats {
    /// Adds another partition's counters.
    pub fn add(&mut self, o: &Self) {
        self.records += o.records;
        self.events += o.events;
        self.trades += o.trades;
        self.instruments += o.instruments;
        self.partitions += o.partitions;
        self.snapshot_records += o.snapshot_records;
        self.out_of_order_ts_event += o.out_of_order_ts_event;
        self.sequence_gaps += o.sequence_gaps;
        self.sequence_regressions += o.sequence_regressions;
        self.unterminated_events += o.unterminated_events;
        self.files_without_snapshot += o.files_without_snapshot;
        self.hidden_prints += o.hidden_prints;
        self.off_price_executions += o.off_price_executions;
        self.removal_after_unpaired_fill += o.removal_after_unpaired_fill;
        self.cross_prints += o.cross_prints;
    }
}

/// Output rows of a replay; every method defaults to `Ok(())`.
pub trait Sink {
    /// One BBO row.
    fn bbo(&mut self, _r: &BboRow) -> QtResult<()> {
        Ok(())
    }
    /// One trade row.
    fn trade(&mut self, _r: &TradeRow) -> QtResult<()> {
        Ok(())
    }
    /// One depth row.
    fn depth(&mut self, _r: &DepthRow) -> QtResult<()> {
        Ok(())
    }
    /// One rank-trace row.
    fn rank_trace(&mut self, _r: &RankTraceRow) -> QtResult<()> {
        Ok(())
    }
    /// One shadow fill.
    fn shadow_fill(&mut self, _r: &ShadowFillRow) -> QtResult<()> {
        Ok(())
    }
    /// One shadow fill with its liquidity (schema 1.1 `qt.shadow_fills.liquidity`). The engine emits fills
    /// through this method; the default forwards to `shadow_fill`.
    fn shadow_fill_liquidity(&mut self, r: &ShadowFillRow, _liquidity: qt_schema::rows::Liquidity) -> QtResult<()> {
        self.shadow_fill(r)
    }
    /// One trajectory row.
    fn trajectory(&mut self, _r: &TrajectoryRow) -> QtResult<()> {
        Ok(())
    }
    /// One shadow result.
    fn shadow_result(&mut self, _r: &ShadowResultRow) -> QtResult<()> {
        Ok(())
    }
    /// One shadow result with its planned end (schema 1.1 `ts_planned_end`, `planned_end_kind`; audit ruling A2).
    /// The engine emits results through this method; the default forwards to `shadow_result`.
    fn shadow_result_planned(
        &mut self,
        r: &ShadowResultRow,
        _planned: qt_schema::codec::PlannedEndCols,
    ) -> QtResult<()> {
        self.shadow_result(r)
    }
    /// One real-order life.
    fn real_order(&mut self, _r: &RealOrderRow) -> QtResult<()> {
        Ok(())
    }
}

/// Per-event truth decomposition of one live shadow (SPEC 5.2.5).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ShadowEventTruth {
    /// Shadow index.
    pub shadow_idx: u32,
    /// Activated at this event's start.
    pub activated_now: bool,
    /// Ended at this event.
    pub ended_now: Option<EndState>,
    /// Queue ahead before step 1.
    pub ahead_before: Qty,
    /// Queue ahead after step 2.
    pub ahead_after: Qty,
    /// Rank before.
    pub rank_before: u32,
    /// Rank after.
    pub rank_after: u32,
    /// Level quantity before.
    pub level_qty_before: Qty,
    /// Level quantity after.
    pub level_qty_after: Qty,
    /// Pure cancels ahead.
    pub cancel_ahead: Qty,
    /// Pure cancels behind.
    pub cancel_behind: Qty,
    /// Quantity added behind.
    pub add_behind: Qty,
    /// At-price traded volume.
    pub traded_at_px: Qty,
    /// Trade-through volume.
    pub trade_through: Qty,
    /// Quantity filled this event.
    pub filled_now: Qty,
}

/// Event-end view passed to observers.
pub struct EventEnd<'a> {
    /// Instrument.
    pub instrument_id: u32,
    /// Event time.
    pub ts_event: Ts,
    /// Sequence of the `LAST` record.
    pub sequence: u64,
    /// Book after the event.
    pub book: &'a Book,
    /// Records of the event.
    pub records: &'a [MboEvent],
    /// Effects of the records.
    pub effects: &'a [Effect],
    /// Truth of every shadow live during the event.
    pub shadow_truth: &'a [ShadowEventTruth],
}

/// Observer of the exact replay (zoo runner, calibration, features); defaults are no-ops.
pub trait EventObserver: Send {
    /// Start of an event (book as left by the previous event).
    fn on_event_start(&mut self, _instrument_id: u32, _ts_event: Ts, _book: &Book) {}
    /// After each record is applied.
    fn on_record(&mut self, _e: &MboEvent, _effect: &Effect, _book: &Book) {}
    /// End of an event.
    fn on_event_end(&mut self, _end: &EventEnd<'_>) {}
    /// End of input.
    fn on_finish(&mut self) {}
}

/// Result of one engine run.
#[derive(Clone, Debug, PartialEq)]
pub struct EngineReport {
    /// Counters.
    pub stats: Stats,
    /// Invariants.
    pub invariants: Invariants,
    /// Rank oracle summary.
    pub rank_oracle: Option<json::RankOracle>,
    /// Data quality.
    pub data_quality: json::DataQuality,
    /// Shadow counters.
    pub shadows: Option<json::ShadowCounts>,
    /// Shadow diagnostics without a schema column (extension; see `ShadowDiagnostics`).
    pub diagnostics: ShadowDiagnostics,
}
