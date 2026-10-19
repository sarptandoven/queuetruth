//! Synthetic MBO generator with an independent reference engine and shadow ground truth, scenario DSL, synthetic DBN
//! writer, dataset writer and referee bug-class fixtures (SPEC 4.1 `qt_testkit`, 5.2, 5.8, 6.2). Does not depend on
//! `qt_book`, `qt_replay`, `qt_ingest` or `qt_l2`: the generator keeps its own positional book (`synth`), and the
//! shadow ground truth (`shadows`) runs on a separate Vec-based book (`refbook`) written from the SPEC text.

#![forbid(unsafe_code)]

use std::path::{Path, PathBuf};

use qt_schema::rows::{BboRow, ShadowAmend, ShadowFillRow, ShadowResultRow, ShadowSpec, TradeRow, TrajectoryRow};
use qt_types::{BookSide, Dur, Instrument, LatencyModel, MboEvent, Px, QtResult, Qty, Ts, Venue, VenueRules};

pub mod audit;
pub mod bugclasses;
pub mod calendar;
pub mod cases;
mod dataset;
mod dbnio;
pub mod dist;
pub mod fixtures;
pub mod inject;
pub mod refbook;
pub mod scenario;
pub mod shadows;
mod synth;

pub use dataset::{SOURCE_FORMAT, expectations, write_dataset_with, write_manifest_sha256};
pub use shadows::{RefConfig, TrajectoryDetail, try_reference_shadows};

/// One shadow's reference output: result row, fill timeline, trajectory.
pub type ShadowTruth = (ShadowResultRow, Vec<ShadowFillRow>, Vec<TrajectoryRow>);

/// Generator settings; `p_*` and `hidden_print_rate` are relative weights per event (see `synth` module doc).
#[derive(Clone, Debug, PartialEq)]
pub struct SynthConfig {
    /// Seed.
    pub seed: u64,
    /// Venue rules to respect.
    pub venue: Venue,
    /// Instruments (ids 1..=n).
    pub instruments: u32,
    /// Events (F_LAST batches) per instrument and session.
    pub events_per_instrument: u64,
    /// Tick.
    pub tick: Px,
    /// Base price.
    pub base_px: Px,
    /// Maximum levels per side.
    pub max_levels: u32,
    /// Mean order size.
    pub mean_size: Qty,
    /// Weight of adds.
    pub p_add: f64,
    /// Weight of full cancels.
    pub p_cancel: f64,
    /// Weight of partial reductions.
    pub p_reduce: f64,
    /// Weight of modifies (Nasdaq: replace = C + A).
    pub p_modify: f64,
    /// Weight of aggressor sweeps.
    pub p_trade: f64,
    /// Weight of snapshot clears (ignored on Nasdaq).
    pub p_clear: f64,
    /// Weight of hidden prints (venues with hidden orders only).
    pub hidden_print_rate: f64,
    /// Start with a snapshot (`R` + flagged adds; ignored on Nasdaq, which builds its book with adds).
    pub snapshot_at_start: bool,
    /// First timestamp (its local date is the session date).
    pub ts_start: Ts,
    /// Mean gap between events.
    pub mean_gap_ns: Dur,
}

impl SynthConfig {
    /// Realistic defaults per venue (2 instruments, 2,000 events, session start 2026-06-15).
    pub fn for_venue(venue: Venue, seed: u64) -> Self {
        let d = calendar::days_from_civil(2026, 6, 15);
        let sr = calendar::session_rules(venue);
        let ts_start = calendar::local_midnight(venue, d) + i64::from(sr.open_s) * calendar::NS_PER_S;
        let (tick, base_px, mean_size) = match venue {
            Venue::Cme => (250_000_000, 5_000_000_000_000, 4),
            Venue::Nasdaq => (10_000_000, 150_000_000_000, 150),
            Venue::Generic | Venue::Coinbase => (10_000_000, 30_000_000_000_000, 20),
        };
        Self {
            seed,
            venue,
            instruments: 2,
            events_per_instrument: 2_000,
            tick,
            base_px,
            max_levels: 10,
            mean_size,
            p_add: 0.42,
            p_cancel: 0.28,
            p_reduce: 0.06,
            p_modify: 0.08,
            p_trade: 0.13,
            p_clear: if venue == Venue::Nasdaq { 0.0 } else { 0.0002 },
            hidden_print_rate: if venue == Venue::Cme { 0.0 } else { 0.02 },
            snapshot_at_start: venue != Venue::Nasdaq,
            ts_start,
            mean_gap_ns: 50_000_000,
        }
    }
}

/// Generated stream with its independent expectations.
#[derive(Clone, Debug, PartialEq)]
pub struct SynthOutput {
    /// Records.
    pub events: Vec<MboEvent>,
    /// Instruments.
    pub instruments: Vec<Instrument>,
    /// Expected `bbo = full` rows.
    pub expected_bbo: Vec<BboRow>,
    /// Expected derived trades.
    pub expected_trades: Vec<TradeRow>,
    /// Final depth (instrument, side, price, total, count).
    pub expected_depth_final: Vec<(u32, BookSide, Px, Qty, u32)>,
}

/// Generates one session valid for the venue's rules (F_LAST batching; see the `synth` module doc).
pub fn generate(cfg: &SynthConfig) -> SynthOutput {
    synth::generate_session(cfg, None, 0, cfg.ts_start)
}

/// Where in a queue the generator's cancels and amends happen (review item 10: a regime in which L2 queue models
/// show visible, explainable optimism). `None` everywhere = the original generator (a cancel or size reduction
/// picks a uniformly random resting order of the whole book, so within a level its position is uniform).
/// With a mix, the order a cancel or partial cancel picks is moved within its own level: the front order with
/// probability `front`, an order of the front half with probability `middle`, else a uniform position; and a
/// share `jump` of modifies become queue-jumping re-quotes: an order from the back half of a best level moves one
/// tick better inside a spread of two ticks or more, leaving the queue from behind (L2 sees the level shrink as
/// if a queue-ahead order had left). All draws come from the instrument's seeded stream.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct QueueMix {
    /// Share of cancels / partial cancels taken from the front of the level.
    pub front: f64,
    /// Share taken uniformly from the front half of the level.
    pub middle: f64,
    /// Share of modifies that are queue-jumping re-quotes.
    pub jump: f64,
}

impl QueueMix {
    /// The default of the committed `synth_small` fixtures: 30% of cancels and partial cancels from the front of
    /// the queue, 25% from its front half, the rest uniform, and 25% of modifies queue-jumping. These are plausible
    /// round numbers (empirical order-book studies report cancellations concentrated away from the back of the
    /// queue, with front-of-queue cancels common at the touch), not values fitted to a venue.
    pub const REALISTIC: Self = Self {
        front: 0.30,
        middle: 0.25,
        jump: 0.25,
    };
}

/// Generates `sessions` consecutive local days (session `k` starts `k` days after `ts_start` at the same local
/// time, with its own seeds and order-id range); outputs are concatenated in session order and
/// `expected_depth_final` is the last session's.
pub fn generate_sessions(cfg: &SynthConfig, sessions: u32) -> SynthOutput {
    generate_sessions_with(cfg, sessions, None)
}

/// `generate_sessions` with a queue-position mix of cancels and amends (`QueueMix`; `None` = `generate_sessions`).
pub fn generate_sessions_with(cfg: &SynthConfig, sessions: u32, mix: Option<QueueMix>) -> SynthOutput {
    let mut out = SynthOutput {
        events: Vec::new(),
        instruments: Vec::new(),
        expected_bbo: Vec::new(),
        expected_trades: Vec::new(),
        expected_depth_final: Vec::new(),
    };
    for k in 0..sessions {
        let s = synth::generate_session(cfg, mix, k, synth::session_start(cfg.venue, cfg.ts_start, k));
        out.events.extend(s.events);
        out.instruments.extend(s.instruments);
        out.expected_bbo.extend(s.expected_bbo);
        out.expected_trades.extend(s.expected_trades);
        out.expected_depth_final = s.expected_depth_final;
    }
    out
}

/// Independent naive implementation of SPEC 5.2 (results, fill timelines, trajectories per shadow, in input order)
/// with the defaults of `RefConfig::new(rules)` (seed 0, no race window, trajectory `ahead`, UTC-day sessions, tick
/// 1). Inputs the engine rejects (`QT-INPUT-002/003`, `QT-CONFIG-001`) give an empty vector; use
/// `try_reference_shadows` for the error.
pub fn reference_shadows(
    events: &[MboEvent],
    rules: VenueRules,
    latency: LatencyModel,
    shadows: &[ShadowSpec],
    amends: &[ShadowAmend],
) -> Vec<(ShadowResultRow, Vec<ShadowFillRow>, Vec<TrajectoryRow>)> {
    let mut cfg = RefConfig::new(rules);
    cfg.latency = latency;
    try_reference_shadows(events, &cfg, shadows, amends).unwrap_or_default()
}

/// Writes a dataset directory (SPEC 3.3 layout via `qt_schema::io`): `manifest.json`, `instruments.parquet`,
/// `mbo/` and `trades_source/` partitions, `data_quality.json`. Returns `root`.
pub fn write_dataset(out: &SynthOutput, root: &Path) -> QtResult<PathBuf> {
    dataset::write_dataset_with(out, root, None)
}

/// Writes a synthetic DBN (v3) MBO file; a path ending in `.zst` is Zstandard-compressed.
pub fn encode_dbn(events: &[MboEvent], path: &Path) -> QtResult<()> {
    dbnio::encode_dbn(events, path)
}
