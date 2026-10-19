//! Serde mirrors of the JSON documents of SPEC 3.10 (source of truth: `python/queuetruth/schemas/models.py`).
//!
//! Field names and order equal the Pydantic models; `schema` / `schema_version` serialize first and are checked on
//! deserialization. Optional (`?`) fields serialize as `null`. Maps keep insertion order (`IndexMap`); producers
//! insert horizon keys in ascending numeric order. The round trip of every `schemas/samples/*.json` document through
//! these types is tested. Service-only documents of SPEC 4.5 (tenant, keys, uploads, ...) have no Rust mirror.
//!
//! Schema 1.1 (SPEC 3.11): fields marked "1.1" are additive `Option`s (null = not reported, the v0.1 behaviour) and
//! are deserialized as `None` when absent, so v0.1 documents stay readable; `schema_version` stays 1.

use std::marker::PhantomData;

use indexmap::IndexMap;
use qt_types::{ValidationStatus, Venue};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Map, Value};

pub use crate::io::FileEntry;
pub use crate::specs::WindowSpec;

/// A top-level document type with its `qt.<name>` schema tag.
pub trait QtDoc {
    /// The `schema` value.
    const SCHEMA: &'static str;
}

/// Serializes as the document's schema name; deserialization rejects any other name.
pub struct Tag<T>(PhantomData<fn() -> T>);

impl<T> Default for Tag<T> {
    fn default() -> Self {
        Self(PhantomData)
    }
}
impl<T> Clone for Tag<T> {
    fn clone(&self) -> Self {
        *self
    }
}
impl<T> Copy for Tag<T> {}
impl<T> PartialEq for Tag<T> {
    fn eq(&self, _: &Self) -> bool {
        true
    }
}
impl<T> Eq for Tag<T> {}
impl<T: QtDoc> std::fmt::Debug for Tag<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(T::SCHEMA)
    }
}
impl<T: QtDoc> Serialize for Tag<T> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(T::SCHEMA)
    }
}
impl<'de, T: QtDoc> Deserialize<'de> for Tag<T> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let s = String::deserialize(d)?;
        if s == T::SCHEMA {
            Ok(Self(PhantomData))
        } else {
            Err(serde::de::Error::custom(format!(
                "expected schema {:?}, found {s:?}",
                T::SCHEMA
            )))
        }
    }
}

/// `schema_version`: serializes as `1`; deserialization rejects other values.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct V1;

impl Serialize for V1 {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_u32(crate::SCHEMA_VERSION)
    }
}
impl<'de> Deserialize<'de> for V1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = u32::deserialize(d)?;
        if v == crate::SCHEMA_VERSION {
            Ok(V1)
        } else {
            Err(serde::de::Error::custom(format!("unsupported schema_version {v}")))
        }
    }
}

/// `{h: v}` map keyed by the horizon in ns as a decimal string; `None` = undefined.
pub type HMap = IndexMap<String, Option<f64>>;
/// Free-form JSON object.
pub type Object = Map<String, Value>;
/// Output files.
pub type Files = Vec<FileEntry>;

macro_rules! doc {
    ($t:ident, $name:literal) => {
        impl QtDoc for $t {
            const SCHEMA: &'static str = $name;
        }
    };
}

/// Horizon map key for a duration in ns.
pub fn hkey(h_ns: i64) -> String {
    h_ns.to_string()
}

// ---- qt.error ---------------------------------------------------------------------------------------------------

/// CLI / API error object.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrorDoc {
    /// `qt.error`.
    pub schema: Tag<ErrorDoc>,
    /// 1.
    pub schema_version: V1,
    /// `QT-<AREA>-<NNN>`.
    pub code: String,
    /// Message without the code.
    pub message: String,
    /// Optional hint.
    pub hint: Option<String>,
    /// Structured details.
    pub details: Object,
}
doc!(ErrorDoc, "qt.error");

impl ErrorDoc {
    /// The error document for a `QtError`.
    pub fn from_error(e: &qt_types::QtError) -> Self {
        Self {
            code: e.code().to_owned(),
            message: e.message(),
            ..Default::default()
        }
    }
}

// ---- datasets ---------------------------------------------------------------------------------------------------

/// Provenance of one ingest input.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InputFile {
    /// Path as given.
    pub path: String,
    /// Size.
    pub bytes: u64,
    /// sha256 hex.
    pub sha256: String,
}

/// One Parquet file of a partition.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PartitionFile {
    /// Path relative to the dataset root (forward slashes).
    pub path: String,
    /// Rows.
    pub rows: u64,
    /// sha256 hex.
    pub sha256: String,
}

/// One (instrument, session date) partition.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PartitionEntry {
    /// Instrument.
    pub instrument_id: u32,
    /// Session date, days since 1970-01-01.
    pub session_date: i32,
    /// Files in order.
    pub files: Vec<PartitionFile>,
    /// Total rows.
    pub rows: u64,
    /// Minimum ts_event.
    pub ts_min: i64,
    /// Maximum ts_event.
    pub ts_max: i64,
}

/// Dataset kind.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DatasetKindName {
    /// Order-level.
    #[default]
    Mbo,
    /// L2 only.
    L2,
}

/// `manifest.json` of a dataset directory.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DatasetManifest {
    /// `qt.dataset_manifest`.
    pub schema: Tag<DatasetManifest>,
    /// 1.
    pub schema_version: V1,
    /// sha256 of the sorted partition-file sha256 list + `\n` + source_format.
    pub dataset_id: String,
    /// mbo or l2.
    pub kind: DatasetKindName,
    /// Ingest format.
    pub source_format: String,
    /// Rule set.
    pub venue: Venue,
    /// Source venue.
    pub source_venue: String,
    /// Inputs (provenance only).
    pub inputs: Vec<InputFile>,
    /// Partitions sorted by (session_date, instrument_id).
    pub partitions: Vec<PartitionEntry>,
    /// Price exponent per instrument id (decimal string keys).
    pub price_exp_by_instrument: IndexMap<String, i8>,
    /// Producer, e.g. `queuetruth 0.1.0`.
    pub created_by: String,
    /// Validation label.
    pub validation_status: ValidationStatus,
    /// 1.1: rule set the engines apply (`crypto_l3`, ...); `None` = derived from `venue`.
    #[serde(default)]
    pub rule_set: Option<qt_types::RuleSet>,
    /// 1.1: data vintage per source file; `None` = not recorded.
    #[serde(default)]
    pub source_vintage: Option<Vec<SourceVintage>>,
}
doc!(DatasetManifest, "qt.dataset_manifest");

/// 1.1: data vintage of one source file (DBN metadata).
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceVintage {
    /// sha256 of the input file.
    pub sha256: String,
    /// DBN version.
    #[serde(default)]
    pub dbn_version: Option<u8>,
    /// Databento dataset code.
    #[serde(default)]
    pub dataset: Option<String>,
    /// DBN schema name.
    #[serde(default)]
    pub dbn_schema: Option<String>,
    /// Metadata query start, UTC ns.
    #[serde(default)]
    pub start_ns: Option<u64>,
    /// Metadata query end, UTC ns (exclusive).
    #[serde(default)]
    pub end_ns: Option<u64>,
    /// Publisher id of the records, when the file holds exactly one.
    #[serde(default)]
    pub publisher_id: Option<u16>,
}

impl DatasetManifest {
    /// The effective rule set: `rule_set`, else the venue's own.
    pub fn effective_rule_set(&self) -> qt_types::RuleSet {
        self.rule_set.unwrap_or_else(|| qt_types::RuleSet::from(self.venue))
    }
}

impl Default for DatasetManifest {
    fn default() -> Self {
        Self {
            schema: Tag::default(),
            schema_version: V1,
            dataset_id: String::new(),
            kind: DatasetKindName::Mbo,
            source_format: String::new(),
            venue: Venue::Generic,
            source_venue: String::new(),
            inputs: Vec::new(),
            partitions: Vec::new(),
            price_exp_by_instrument: IndexMap::new(),
            created_by: String::new(),
            validation_status: ValidationStatus::SyntheticOnly,
            rule_set: None,
            source_vintage: None,
        }
    }
}

/// `dataset_id` rule (SPEC 3.10): sha256 of the sorted partition-file sha256s joined by `\n`, then `\n` and
/// `source_format`.
pub fn dataset_id(file_sha256s: &[String], source_format: &str) -> String {
    let mut v: Vec<&str> = file_sha256s.iter().map(String::as_str).collect();
    v.sort_unstable();
    let text = format!("{}\n{source_format}", v.join("\n"));
    crate::io::sha256_bytes(text.as_bytes())
}

/// A gap in the feed of one instrument.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeedGap {
    /// Instrument.
    pub instrument_id: u32,
    /// Gap start.
    pub ts_start: i64,
    /// Gap end.
    pub ts_end: i64,
    /// Reason.
    pub reason: String,
}

/// Data-quality verdict.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    /// No counter non-zero.
    #[default]
    Clean,
    /// Some counter non-zero.
    Warnings,
    /// Thresholds of SPEC 5.1.5 exceeded.
    Unusable,
}

/// Data-quality counters and verdict.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DataQuality {
    /// `qt.data_quality`.
    pub schema: Tag<DataQuality>,
    /// 1.
    pub schema_version: V1,
    /// Records.
    pub records: u64,
    /// Records flagged MAYBE_BAD_BOOK.
    pub maybe_bad_book_records: u64,
    /// Records flagged BAD_TS_RECV.
    pub bad_ts_recv_records: u64,
    /// Snapshot records.
    pub snapshot_records: u64,
    /// Rows skipped before the first snapshot.
    pub pre_snapshot_rows_skipped: u64,
    /// C/M/F for unknown orders.
    pub unknown_order_refs: u64,
    /// Duplicate adds outside snapshots.
    pub duplicate_adds: u64,
    /// M on venues without modifies.
    pub unexpected_modifies: u64,
    /// Sequence gaps.
    pub sequence_gaps: u64,
    /// Sequence regressions.
    pub sequence_regressions: u64,
    /// ts_event going backwards.
    pub out_of_order_ts_event: u64,
    /// Crossed books at event end.
    pub crossed_books_after_last: u64,
    /// Hidden prints.
    pub hidden_prints: u64,
    /// Off-price executions.
    pub off_price_executions: u64,
    /// Feed gaps.
    pub feed_gaps: Vec<FeedGap>,
    /// Source-specific counters.
    pub source_specific: IndexMap<String, u64>,
    /// Verdict.
    pub verdict: Verdict,
    /// Reasons for the verdict.
    pub reasons: Vec<String>,
}
doc!(DataQuality, "qt.data_quality");

/// Summary of `ingest`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IngestSummary {
    /// `qt.ingest_summary`.
    pub schema: Tag<IngestSummary>,
    /// 1.
    pub schema_version: V1,
    /// Spec echo.
    pub spec: Object,
    /// Dataset manifest.
    pub manifest: DatasetManifest,
    /// Data quality.
    pub data_quality: DataQuality,
    /// Files.
    pub files: Files,
    /// Wall-clock timings.
    pub timings: Option<IndexMap<String, f64>>,
    /// 1.1: ingest warnings.
    #[serde(default)]
    pub warnings: Vec<String>,
}
doc!(IngestSummary, "qt.ingest_summary");

// ---- replay -----------------------------------------------------------------------------------------------------

/// Replay counters.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReplayStats {
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
}

/// The 8 book invariant counters.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Invariants {
    /// Cancels exceeding the resting size.
    pub cancels_exceeding_resting: u64,
    /// Fills exceeding the resting size.
    pub fills_exceeding_resting: u64,
    /// Fills of unknown orders.
    pub fills_unknown_order: u64,
    /// References to unknown orders.
    pub unknown_order_refs: u64,
    /// Duplicate adds.
    pub duplicate_adds: u64,
    /// Unexpected modifies.
    pub unexpected_modifies: u64,
    /// Crossed books at event end.
    pub crossed_books_after_last: u64,
    /// Negative spreads at event end.
    pub negative_spreads: u64,
}

/// Rank-oracle statistics over the first 60 s after the open.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RankWindow {
    /// Executions checked.
    pub checked: u64,
    /// Rank violations.
    pub rank_violations: u64,
    /// Rank violations, sweep adjusted.
    pub rank_violations_sweep_adjusted: u64,
    /// Share with rank 0.
    pub rank0_share: Option<f64>,
    /// Share with rank 0, sweep adjusted.
    pub rank0_share_sweep_adjusted: Option<f64>,
}

/// Rank-oracle statistics of one 30-minute local bucket.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RankBucket {
    /// Executions checked.
    pub checked: u64,
    /// Rank-0 executions.
    pub rank0: u64,
    /// Rank-0 executions, sweep adjusted.
    pub rank0_sweep_adjusted: u64,
    /// Share with rank 0.
    pub rank0_share: Option<f64>,
    /// Share with rank 0, sweep adjusted.
    pub rank0_share_sweep_adjusted: Option<f64>,
}

/// Rank-oracle summary.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RankOracle {
    /// Executions checked.
    pub executions_checked: u64,
    /// Executions of unknown orders.
    pub executions_unknown_order: u64,
    /// Price-priority violations.
    pub price_priority_violations: u64,
    /// Rank histogram, bins 0..4 and 5+.
    pub fifo_rank_histogram: [u64; 6],
    /// Sweep-adjusted rank histogram.
    pub fifo_rank_histogram_sweep_adjusted: [u64; 6],
    /// Share with rank 0.
    pub rank0_share: Option<f64>,
    /// Share with rank 0, sweep adjusted.
    pub rank0_share_sweep_adjusted: Option<f64>,
    /// Sweeps.
    pub sweeps: u64,
    /// Sweeps with more than one fill.
    pub sweeps_multi_fill: u64,
    /// Sweep gap.
    pub sweep_gap_ns: i64,
    /// First 60 s after the open.
    pub first_60s_after_open: RankWindow,
    /// Per 30-minute bucket keyed `HH:MM`.
    pub rank0_share_by_bucket: IndexMap<String, RankBucket>,
}

/// Trade and BBO parity.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Parity {
    /// Trades compared.
    pub trades_compared: u64,
    /// Trade mismatches.
    pub trades_mismatches: u64,
    /// Source trades without a derived trade.
    pub trades_missing_derived: u64,
    /// Derived trades without a source trade.
    pub trades_extra_derived: u64,
    /// BBO rows compared.
    pub bbo_compared: u64,
    /// BBO mismatches.
    pub bbo_mismatches: u64,
    /// First 20 mismatches.
    pub first_mismatches: Vec<Object>,
    /// Zero mismatches.
    pub pass: bool,
}

/// Shadow outcome counters.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowCounts {
    /// Shadows.
    pub n: u64,
    /// Shadows that went live.
    pub n_live: u64,
    /// Fully filled.
    pub n_filled: u64,
    /// Partially filled only.
    pub n_partial_only: u64,
    /// Cancelled.
    pub n_cancelled: u64,
    /// Censored.
    pub n_censored: u64,
    /// Rejected.
    pub n_rejected: u64,
    /// Rows in shadow_fills.
    pub fills_rows: u64,
    /// Rows in queue_trajectory.
    pub trajectory_rows: u64,
}

/// 1.1 (3.11 #20): shadow fill-rule diagnostics of a replay (A1, A3, A5).
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowDiagnostics {
    /// Fill events that used hidden-print volume at the shadow price (A3).
    pub hidden_print_fills: u64,
    /// Hidden prints at a shadow price declined because displayed interest rested there (A3d).
    pub hidden_at_px_with_display_ahead: u64,
    /// Taker fill rows (A5).
    pub taker_fills: u64,
    /// Crossed events without opposite size left for the shadow (A5).
    pub crossed_unfilled: u64,
    /// Fill events from executions behind the shadow while displayed queue ahead remained.
    pub behind_exec_with_ahead: u64,
    /// `exclude_order_ids` requested (A1).
    pub excluded_requested: u64,
    /// Requested ids found in the stream.
    pub excluded_matched: u64,
    /// Records dropped for excluded orders.
    pub excluded_records: u64,
    /// Excluded orders replaced by their own shadow and kept on the tape (A1 twin linking; additive, default 0).
    #[serde(default)]
    pub twins_linked: u64,
    /// Twin executions credited while the shadow's displayed queue ahead was still positive (additive, default 0).
    #[serde(default)]
    pub twin_fills_with_queue_ahead: u64,
    /// Twin executions at a price other than the twin's displayed price (additive, default 0).
    #[serde(default)]
    pub twin_fills_off_display: u64,
}

/// Wall-clock timings of a replay.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReplayTimings {
    /// Wall seconds.
    pub wall_s: f64,
    /// Records per second.
    pub records_per_s: Option<f64>,
}

/// Summary of `replay`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReplaySummary {
    /// `qt.replay_summary`.
    pub schema: Tag<ReplaySummary>,
    /// 1.
    pub schema_version: V1,
    /// Run id (hex16).
    pub run_id: String,
    /// Engine version.
    pub engine_version: String,
    /// Spec echo.
    pub spec: Object,
    /// Counters.
    pub stats: ReplayStats,
    /// Invariants.
    pub invariants: Invariants,
    /// Rank oracle.
    pub rank_oracle: Option<RankOracle>,
    /// Parity.
    pub parity: Option<Parity>,
    /// Shadow counters.
    pub shadows: Option<ShadowCounts>,
    /// Data quality.
    pub data_quality: DataQuality,
    /// Files.
    pub files: Files,
    /// Warnings.
    pub warnings: Vec<String>,
    /// Timings.
    pub timings: Option<ReplayTimings>,
    /// 1.1 (3.11 #20): shadow diagnostics.
    #[serde(default)]
    pub shadow_diagnostics: Option<ShadowDiagnostics>,
}
doc!(ReplaySummary, "qt.replay_summary");

/// Summary of `shadows sample` / `mirror-real` / `from-live`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowSampleSummary {
    /// `qt.shadow_sample_summary`.
    pub schema: Tag<ShadowSampleSummary>,
    /// 1.
    pub schema_version: V1,
    /// Spec echo.
    pub spec: Object,
    /// Shadows written.
    pub n: u64,
    /// Count by instrument id.
    pub by_instrument: IndexMap<String, u64>,
    /// Count by placement offset.
    pub by_placement: IndexMap<String, u64>,
    /// Files.
    pub files: Files,
}
doc!(ShadowSampleSummary, "qt.shadow_sample_summary");

// ---- zoo --------------------------------------------------------------------------------------------------------

/// Point estimate with a confidence interval.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CiValue {
    /// Estimate.
    pub value: Option<f64>,
    /// Lower bound.
    pub ci_lo: Option<f64>,
    /// Upper bound.
    pub ci_hi: Option<f64>,
}

/// Discordant counts and exact McNemar p.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct McNemarSmall {
    /// Truth only.
    pub b: u64,
    /// Model only.
    pub c: u64,
    /// Exact p.
    pub p_exact: Option<f64>,
}

/// One model's metrics vs exact truth.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LeaderboardEntry {
    /// Model id.
    pub model_id: String,
    /// Brier by horizon.
    pub brier: HMap,
    /// Log loss by horizon.
    pub logloss: HMap,
    /// Fill-rate bias by horizon.
    pub fill_rate_bias: IndexMap<String, CiValue>,
    /// Overfill bias at h_max.
    pub overfill_bias: CiValue,
    /// Quantity bias.
    pub qty_bias: Option<f64>,
    /// Median log ttf ratio.
    pub ttf_log_ratio_median: Option<f64>,
    /// Integrated absolute difference of fill curves.
    pub ttf_iad: Option<f64>,
    /// Queue-tracking MAE.
    pub qtrack_mae: Option<f64>,
    /// McNemar at h_max.
    pub mcnemar: McNemarSmall,
}

/// Summary of `zoo`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ZooSummary {
    /// `qt.zoo_summary`.
    pub schema: Tag<ZooSummary>,
    /// 1.
    pub schema_version: V1,
    /// Run id.
    pub run_id: String,
    /// Spec echo.
    pub spec: Object,
    /// Shadows.
    pub n_shadows: u64,
    /// Model ids.
    pub models: Vec<String>,
    /// Leaderboard (empty from the Rust runner).
    pub leaderboard: Vec<LeaderboardEntry>,
    /// Files.
    pub files: Files,
    /// Warnings.
    pub warnings: Vec<String>,
    /// 1.1 (additive): faithful hbt #312 aborts per model id, the number of (model, shadow) pairs whose
    /// `InvalidOrderStatus` event stopped them (their prediction rows from the abort on carry status
    /// `hbt_312_abort`); models without an abort are absent. The `hbt_312_abort: ` warning lines stay.
    #[serde(default)]
    pub hbt_312_aborts: IndexMap<String, u64>,
}
doc!(ZooSummary, "qt.zoo_summary");

// ---- calibration ------------------------------------------------------------------------------------------------

/// Held-out metrics of one model.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeldOut {
    /// Brier by horizon.
    pub brier: HMap,
    /// Log loss by horizon.
    pub logloss: HMap,
    /// Fill-rate bias by horizon.
    pub fill_rate_bias: HMap,
}

/// Parameter estimate in one stratum.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StratumEstimate {
    /// Stratum kind.
    pub stratum_kind: String,
    /// Stratum.
    pub stratum: String,
    /// Estimate.
    pub estimate: Option<f64>,
    /// Lower bound.
    pub ci_lo: Option<f64>,
    /// Upper bound.
    pub ci_hi: Option<f64>,
    /// Events.
    pub n: u64,
}

/// Latency quantiles in ns.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Quantiles {
    /// 10th percentile.
    pub p10: Option<f64>,
    /// Median.
    pub p50: Option<f64>,
    /// 90th percentile.
    pub p90: Option<f64>,
    /// 99th percentile.
    pub p99: Option<f64>,
    /// Observations.
    pub n: u64,
}

/// Latency distributions from a live log.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LatencyFit {
    /// Send -> exchange ack.
    pub entry_ns: Quantiles,
    /// Exchange -> local receive.
    pub response_ns: Quantiles,
    /// Cancel latency.
    pub cancel_ns: Quantiles,
    /// Amend latency.
    pub amend_ns: Quantiles,
    /// Always true.
    pub clock_sync_assumed: bool,
}

/// Paste-ready config text with notes.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExportSnippet {
    /// Python text.
    pub python: String,
    /// Notes.
    pub notes: Vec<String>,
}

/// Config exports (all null in the Rust-only `fit.json`).
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Exports {
    /// hftbacktest snippet.
    pub hftbacktest: Option<ExportSnippet>,
    /// NautilusTrader snippet.
    pub nautilus: Option<ExportSnippet>,
    /// Latency npz file name.
    pub latency_npz: Option<String>,
}

/// Provenance of the data a calibration used.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DataSource {
    /// Dataset id.
    pub dataset_id: String,
    /// Rule set.
    pub venue: String,
    /// Source venue.
    pub source_venue: String,
    /// Validation label.
    pub validation_status: ValidationStatus,
}

/// Calibration kind.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CalibrationKind {
    /// Allocation quasi-likelihood.
    #[default]
    Alloc,
    /// Survival likelihood.
    Survival,
    /// NT touch MLE.
    NtTouch,
    /// Latency from live logs.
    Latency,
    /// 1.1 (3.11 #10): `calibrate-live` result (not a `CalibrationSpec` kind).
    Live,
}

/// 1.1 (3.11 #10): held-out discordance of `calibrate-live`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeldOutDiscordance {
    /// Test orders.
    pub n: u64,
    /// Live filled, model not.
    pub b: u64,
    /// Model filled, live not.
    pub c: u64,
    /// Fill-rate bias (model minus live).
    pub fill_rate_bias: Option<f64>,
    /// CI low.
    pub ci_lo: Option<f64>,
    /// CI high.
    pub ci_hi: Option<f64>,
    /// Exact McNemar p.
    pub p_exact: Option<f64>,
    /// Cluster-robust p.
    pub p_clustered: Option<f64>,
    /// Cluster-robust method.
    pub clustered_method: Option<String>,
    /// Clusters.
    pub n_clusters: u64,
}

/// Result of `calibrate`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CalibrationResult {
    /// `qt.calibration_result`.
    pub schema: Tag<CalibrationResult>,
    /// 1.
    pub schema_version: V1,
    /// Run id.
    pub run_id: String,
    /// Spec echo.
    pub spec: Object,
    /// Kind.
    pub kind: CalibrationKind,
    /// Family.
    pub family: String,
    /// Mode.
    pub mode: String,
    /// Parameter name.
    pub param_name: Option<String>,
    /// Estimate.
    pub estimate: Option<f64>,
    /// Profile CI (quasi).
    pub ci_profile: Option<(Option<f64>, Option<f64>)>,
    /// Bootstrap CI.
    pub ci_bootstrap: Option<(Option<f64>, Option<f64>)>,
    /// Log likelihood at the estimate.
    pub loglik: Option<f64>,
    /// Events used.
    pub n_events: u64,
    /// Shadows used.
    pub n_shadows: u64,
    /// Train window.
    pub train: WindowSpec,
    /// Test window.
    pub test: WindowSpec,
    /// Held-out metrics of the fitted model.
    pub held_out: IndexMap<String, HeldOut>,
    /// Held-out metrics of the default parameters.
    pub baseline_held_out: IndexMap<String, HeldOut>,
    /// Stratified estimates.
    pub stratified: Vec<StratumEstimate>,
    /// Latency fit.
    pub latency: Option<LatencyFit>,
    /// Exports.
    pub exports: Exports,
    /// Data sources.
    pub data_sources: Vec<DataSource>,
    /// Warnings.
    pub warnings: Vec<String>,
    /// 1.1 (3.11 #10): held-out discordance (live calibrations).
    #[serde(default)]
    pub held_out_discordance: Option<HeldOutDiscordance>,
    /// 1.1: export trust assessment (structured `trust:` lines).
    #[serde(default)]
    pub trust: Option<CalibrationTrust>,
}
doc!(CalibrationResult, "qt.calibration_result");

/// Export verdict of a calibration (1.1).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TrustVerdict {
    /// Exportable.
    Exportable,
    /// Refused (`QT-CALIB-001` unless forced).
    #[default]
    Refused,
}

/// Held-out comparison of the fitted and the default model at one horizon (1.1).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum BeatsDefault {
    /// Better on a score, not worse on the other.
    #[serde(rename = "better")]
    Better,
    /// Ties on both scores.
    #[serde(rename = "tie")]
    Tie,
    /// Worse on a score, not better on the other.
    #[serde(rename = "worse")]
    Worse,
    /// Better on one score, worse on the other.
    #[serde(rename = "mixed")]
    Mixed,
    /// Not scored.
    #[serde(rename = "n/a")]
    NotAvailable,
}

/// 1.1: structured form of the `trust:` warning lines.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CalibrationTrust {
    /// Verdict.
    pub verdict: TrustVerdict,
    /// Refusing tokens.
    pub reasons: Vec<String>,
    /// `trust:` diagnostic tokens present.
    pub flags: Vec<String>,
    /// Held-out comparison per horizon (ns, decimal string).
    pub beats_default: IndexMap<String, BeatsDefault>,
}

/// Train log-likelihood at one `calibrate-live` grid value.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiveCalibrationGridPoint {
    /// Grid coordinate.
    pub theta: f64,
    /// Parameter in natural units.
    pub value: f64,
    /// Train log-likelihood.
    pub loglik: f64,
}

/// 1.1 (3.11 #11): `live_calibration_test.json` of `calibrate-live`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiveCalibrationTest {
    /// `qt.live_calibration_test`.
    pub schema: Tag<LiveCalibrationTest>,
    /// 1.
    pub schema_version: V1,
    /// Fitted model id.
    pub model_id: String,
    /// Grid profile.
    pub grid: Vec<LiveCalibrationGridPoint>,
    /// Test orders.
    pub n: u64,
    /// Live filled, model not.
    pub b: u64,
    /// Model filled, live not.
    pub c: u64,
    /// Fill-rate bias.
    pub fill_rate_bias: Option<f64>,
    /// CI low.
    pub ci_lo: Option<f64>,
    /// CI high.
    pub ci_hi: Option<f64>,
    /// Exact McNemar p.
    pub p_exact: Option<f64>,
    /// Cluster-robust p.
    pub p_clustered: Option<f64>,
    /// Cluster-robust method.
    pub clustered_method: Option<String>,
    /// Clusters.
    pub n_clusters: u64,
}
doc!(LiveCalibrationTest, "qt.live_calibration_test");

// ---- features and learned model ---------------------------------------------------------------------------------

/// Rows by cause.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CauseCounts {
    /// Fill.
    pub fill: u64,
    /// Cancel.
    pub cancel: u64,
    /// Censor.
    pub censor: u64,
}

/// Summary of `features`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeatureSummary {
    /// `qt.feature_summary`.
    pub schema: Tag<FeatureSummary>,
    /// 1.
    pub schema_version: V1,
    /// Spec echo.
    pub spec: Object,
    /// Orders.
    pub n_orders: u64,
    /// Rows.
    pub n_rows: u64,
    /// Rows by cause.
    pub by_cause: CauseCounts,
    /// Feature columns.
    pub feature_names: Vec<String>,
    /// Privileged columns.
    pub privileged_names: Vec<String>,
    /// Files.
    pub files: Files,
}
doc!(FeatureSummary, "qt.feature_summary");

/// Split scheme echo.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SplitEcho {
    /// Scheme.
    pub scheme: String,
    /// Train part.
    pub train: Value,
    /// Calib part.
    pub calib: Value,
    /// Test part.
    pub test: Value,
}

/// Orders per split.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SplitCounts {
    /// Train.
    pub train: u64,
    /// Calib.
    pub calib: u64,
    /// Test.
    pub test: u64,
}

/// Test-split scores of the learned model.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HazardScores {
    /// RCLL.
    pub rcll: Option<f64>,
    /// IPCW log loss.
    pub ipcw_logloss: HMap,
    /// IPCW Brier.
    pub brier: HMap,
    /// BSS vs climatology.
    pub bss_vs_climatology: IndexMap<String, CiValue>,
    /// Integrated Brier score.
    pub ibs: Option<f64>,
    /// Calibration slope.
    pub calib_slope: HMap,
    /// Calibration intercept.
    pub calib_intercept: HMap,
    /// Spiegelhalter z.
    pub spiegelhalter_z: HMap,
    /// Hosmer-Lemeshow p.
    pub hl_p: HMap,
    /// Uno's C (improper).
    pub c_index_uno: Option<f64>,
    /// AUC (improper).
    pub auc: HMap,
    /// 1.1 (3.11 #22): Hosmer-Lemeshow p with 8 df (et parity).
    #[serde(default)]
    pub hl_p_et: Option<HMap>,
}

/// Brier of a zoo model on the same test rows.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ZooBrier {
    /// Brier by horizon.
    pub brier: HMap,
}

/// Hazard model cause.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HazardCause {
    /// Fill.
    Fill,
    /// Cancel.
    Cancel,
}

/// Recalibration method.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Recalibration {
    /// Isotonic.
    #[default]
    Isotonic,
    /// None.
    None,
}

/// `model.json` of `fit-hazard`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HazardModelCard {
    /// `qt.hazard_model_card`.
    pub schema: Tag<HazardModelCard>,
    /// 1.
    pub schema_version: V1,
    /// Model id.
    pub model_id: String,
    /// Format version (1).
    pub format_version: u32,
    /// Spec echo.
    pub spec: Object,
    /// Grid.
    pub grid_ns: Vec<i64>,
    /// Causes.
    pub causes: Vec<HazardCause>,
    /// Features.
    pub feature_names: Vec<String>,
    /// Privileged model.
    pub privileged: bool,
    /// Split.
    pub split: SplitEcho,
    /// Orders per split.
    pub n_orders: SplitCounts,
    /// Person-period rows in train.
    pub n_rows_train: u64,
    /// Scores.
    pub scores_test: HazardScores,
    /// Zoo comparison.
    pub zoo_comparison: Option<IndexMap<String, ZooBrier>>,
    /// Recalibration.
    pub recalibration: Recalibration,
    /// Files.
    pub files: Files,
    /// Warnings.
    pub warnings: Vec<String>,
}
doc!(HazardModelCard, "qt.hazard_model_card");

// ---- live logs --------------------------------------------------------------------------------------------------

/// Provenance of one importer input.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiveInput {
    /// Path.
    pub path: String,
    /// sha256.
    pub sha256: String,
}

/// Validation label of live logs.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LiveValidationStatus {
    /// Real account data.
    Real,
    /// Fixture-validated importer.
    #[default]
    FixtureOnly,
    /// 1.1 (3.11 #15): simulator output.
    Simulated,
}

/// 1.1 (3.11 #14): order-source coverage of a live log.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiveCoverage {
    /// Every order is known only from fills.
    pub fills_only: bool,
    /// Orders known only from fills.
    pub n_fills_only_orders: u64,
}

/// Summary of `live import`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiveImportSummary {
    /// `qt.live_import_summary`.
    pub schema: Tag<LiveImportSummary>,
    /// 1.
    pub schema_version: V1,
    /// Venue.
    pub venue: String,
    /// Inputs.
    pub inputs: Vec<LiveInput>,
    /// Orders.
    pub n_orders: u64,
    /// Events.
    pub n_events: u64,
    /// Fills.
    pub n_fills: u64,
    /// Rows dropped by dedupe.
    pub dedupe_dropped: u64,
    /// Fills without an order.
    pub unmatched_fills: u64,
    /// Warnings.
    pub warnings: Vec<String>,
    /// Validation label.
    pub validation_status: LiveValidationStatus,
    /// Files.
    pub files: Files,
    /// 1.1 (3.11 #14): coverage.
    #[serde(default)]
    pub coverage: Option<LiveCoverage>,
    /// 1.1 (3.11 #14): entry-time fallback share.
    #[serde(default)]
    pub entry_fallback_share: Option<f64>,
    /// 1.1: instruments without an instrument-map entry.
    #[serde(default)]
    pub unmapped_instruments: Vec<UnmappedInstrument>,
    /// 1.1: per-instrument scales of the written log.
    #[serde(default)]
    pub instrument_scales: Option<Vec<InstrumentScale>>,
}
doc!(LiveImportSummary, "qt.live_import_summary");

/// 1.1: an instrument without an instrument-map entry.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UnmappedInstrument {
    /// Venue.
    pub venue: String,
    /// Venue instrument name.
    pub instrument: String,
    /// Orders.
    pub n_orders: u64,
}

/// Where an instrument's scales came from (1.1).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScaleSource {
    /// The instrument map.
    InstrumentMap,
    /// Importer defaults (unmapped).
    #[default]
    Default,
}

/// 1.1: price / size exponents of one instrument of a live log.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstrumentScale {
    /// Venue.
    pub venue: String,
    /// Venue instrument name.
    pub instrument: String,
    /// Instrument id (null = unmapped).
    pub instrument_id: Option<u32>,
    /// Price exponent.
    pub price_exp: i8,
    /// Size exponent.
    pub size_exp: i8,
    /// Scale source.
    pub scale_source: ScaleSource,
    /// Orders.
    pub n_orders: u64,
}

/// `manifest.json` of a canonical live-log directory.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiveManifest {
    /// `qt.live_manifest`.
    pub schema: Tag<LiveManifest>,
    /// 1.
    pub schema_version: V1,
    /// Venues.
    pub venues: Vec<String>,
    /// Accounts.
    pub accounts: Vec<String>,
    /// Orders.
    pub n_orders: u64,
    /// Events.
    pub n_events: u64,
    /// Instrument map sha256.
    pub instrument_map_sha256: Option<String>,
    /// Producer.
    pub created_by: String,
    /// 1.1 (3.11 #14): coverage.
    #[serde(default)]
    pub coverage: Option<LiveCoverage>,
    /// 1.1 (3.11 #14): entry-time fallback share.
    #[serde(default)]
    pub entry_fallback_share: Option<f64>,
}
doc!(LiveManifest, "qt.live_manifest");

/// Result of `live validate`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiveValidation {
    /// `qt.live_validation`.
    pub schema: Tag<LiveValidation>,
    /// 1.
    pub schema_version: V1,
    /// Directory.
    pub path: String,
    /// Orders.
    pub n_orders: u64,
    /// Events.
    pub n_events: u64,
    /// Errors.
    pub errors: Vec<String>,
    /// Warnings.
    pub warnings: Vec<String>,
}
doc!(LiveValidation, "qt.live_validation");

// ---- scorecard --------------------------------------------------------------------------------------------------

/// Simulator arm kind.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SimKind {
    /// External simulator log.
    #[default]
    External,
    /// Exact replay.
    Exact,
    /// Zoo model.
    Zoo,
}

/// Which simulator arm was scored.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SimSource {
    /// Kind.
    pub kind: SimKind,
    /// Zoo model id.
    pub model_id: Option<String>,
    /// External log path.
    pub path: Option<String>,
}

/// Per-order fill outcome table.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TwoByTwo {
    /// Both filled.
    pub both: u64,
    /// Neither filled.
    pub neither: u64,
    /// Live only.
    pub live_only: u64,
    /// Sim only.
    pub sim_only: u64,
}

/// Bias direction.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    /// CI lower > 0.
    SimOverfills,
    /// CI upper < 0.
    SimUnderfills,
    /// Otherwise.
    #[default]
    NoSignificantBias,
}

/// Signed bias.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Bias {
    /// (c - b) / N.
    pub fill_rate_bias: Option<f64>,
    /// Lower bound.
    pub ci_lo: Option<f64>,
    /// Upper bound.
    pub ci_hi: Option<f64>,
    /// (n11 + c) / (n11 + b) - 1.
    pub relative_overfill: Option<f64>,
    /// Quantity bias.
    pub qty_bias: Option<f64>,
    /// Lower bound.
    pub qty_bias_ci_lo: Option<f64>,
    /// Upper bound.
    pub qty_bias_ci_hi: Option<f64>,
    /// Direction.
    pub direction: Direction,
}

/// McNemar test.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct McNemar {
    /// Live only.
    pub b: u64,
    /// Sim only.
    pub c: u64,
    /// Continuity-corrected chi-square.
    pub chi2_cc: f64,
    /// Chi-square p.
    pub p_chi2: Option<f64>,
    /// Exact p.
    pub p_exact: Option<f64>,
    /// c / b.
    pub odds_ratio: Option<f64>,
    /// Lower bound.
    pub or_ci_lo: Option<f64>,
    /// Upper bound.
    pub or_ci_hi: Option<f64>,
}

/// 2x2 table within one queue decile.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DecileRow {
    /// Decile 0..9.
    pub decile: u8,
    /// Both.
    pub both: u64,
    /// Neither.
    pub neither: u64,
    /// Live only.
    pub live_only: u64,
    /// Sim only.
    pub sim_only: u64,
    /// Exact p.
    pub p_exact: Option<f64>,
    /// Adjusted p.
    pub p_adj: Option<f64>,
}

/// 2x2 table within one liquidity bucket.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiquidityRow {
    /// Bucket 0..2.
    pub bucket: u8,
    /// Both.
    pub both: u64,
    /// Neither.
    pub neither: u64,
    /// Live only.
    pub live_only: u64,
    /// Sim only.
    pub sim_only: u64,
    /// Exact p.
    pub p_exact: Option<f64>,
    /// Adjusted p.
    pub p_adj: Option<f64>,
}

/// Time-to-fill comparison.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Ttf {
    /// Jointly filled orders.
    pub n_joint: u64,
    /// Median log ratio.
    pub median_log_ratio: Option<f64>,
    /// Lower bound.
    pub ci_lo: Option<f64>,
    /// Upper bound.
    pub ci_hi: Option<f64>,
    /// Wilcoxon p.
    pub wilcoxon_p: Option<f64>,
    /// Paired permutation p.
    pub paired_perm_p: Option<f64>,
    /// Log-rank p.
    pub logrank_p: Option<f64>,
    /// Gray p.
    pub gray_p: Option<f64>,
    /// CIF of fills by arm (`live`, `sim`).
    pub cif_fill: IndexMap<String, HMap>,
}

/// Arm.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Arm {
    /// Live.
    #[default]
    Live,
    /// Simulator.
    Sim,
}

/// Markout reference price.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MarkoutRef {
    /// Mid.
    #[default]
    Mid,
    /// Size-weighted mid.
    Microprice,
}

/// Mean markout of one arm.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MarkoutRow {
    /// Horizon.
    pub horizon_ns: i64,
    /// Reference.
    pub r#ref: MarkoutRef,
    /// Arm.
    pub arm: Arm,
    /// Fills.
    pub n: u64,
    /// Mean in ticks.
    pub mean_ticks: Option<f64>,
    /// Lower bound.
    pub ci_lo: Option<f64>,
    /// Upper bound.
    pub ci_hi: Option<f64>,
}

/// Paired markout difference.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MarkoutDiffRow {
    /// Horizon.
    pub horizon_ns: i64,
    /// Reference.
    pub r#ref: MarkoutRef,
    /// Orders.
    pub n: u64,
    /// Mean difference.
    pub mean_diff_ticks: Option<f64>,
    /// Lower bound.
    pub ci_lo: Option<f64>,
    /// Upper bound.
    pub ci_hi: Option<f64>,
    /// Adjusted p.
    pub p_adj: Option<f64>,
}

/// Sim-only vs live-only markouts.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DiscordantMarkoutRow {
    /// Horizon.
    pub horizon_ns: i64,
    /// Sim-only mean.
    pub sim_only_mean: Option<f64>,
    /// Live-only mean.
    pub live_only_mean: Option<f64>,
    /// Difference.
    pub diff: Option<f64>,
    /// Lower bound.
    pub ci_lo: Option<f64>,
    /// Upper bound.
    pub ci_hi: Option<f64>,
}

/// TOST results.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Equivalence {
    /// Fill-rate margin.
    pub fill_rate_margin: f64,
    /// TOST p.
    pub tost_p: Option<f64>,
    /// Fill rate equivalent.
    pub fill_rate_equivalent: bool,
    /// Markout margin.
    pub markout_margin_ticks: f64,
    /// Markout horizon.
    pub markout_horizon_ns: i64,
    /// Markout TOST p.
    pub markout_tost_p: Option<f64>,
    /// Markout equivalent.
    pub markout_equivalent: bool,
}

/// FDR method.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FdrMethod {
    /// Benjamini-Hochberg.
    #[default]
    Bh,
    /// Benjamini-Yekutieli.
    By,
}

/// Multiple-testing summary.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Fdr {
    /// Method.
    pub method: FdrMethod,
    /// Level.
    pub q: f64,
    /// Tests.
    pub n_tests: u64,
    /// Rejections.
    pub n_rejected: u64,
}

/// Headline and gates.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScoreVerdict {
    /// Headline.
    pub headline: String,
    /// Signed bias.
    pub signed_bias: Option<f64>,
    /// All gates passed.
    pub gates_passed: bool,
    /// Failed gates.
    pub failed_gates: Vec<String>,
}

/// 1.1 (3.11 #9): cluster-robust primary discordance test (A10).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DiscordanceClustered {
    /// Method.
    pub method: String,
    /// Fill-rate bias estimate.
    pub estimate: Option<f64>,
    /// CI low.
    pub ci_lo: Option<f64>,
    /// CI high.
    pub ci_hi: Option<f64>,
    /// p value.
    pub p_value: Option<f64>,
    /// Clusters.
    pub n_clusters: u64,
    /// Clusters with discordance.
    pub n_clusters_discordant: u64,
    /// Design effect.
    pub design_effect: Option<f64>,
    /// Effective n.
    pub n_effective: Option<f64>,
}

/// A count per scorecard arm.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArmCounts {
    /// Live arm.
    pub live: u64,
    /// Simulator arm.
    pub sim: u64,
}

/// Summary of `score`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScorecardSummary {
    /// `qt.scorecard_summary`.
    pub schema: Tag<ScorecardSummary>,
    /// 1.
    pub schema_version: V1,
    /// Run id.
    pub run_id: String,
    /// Spec echo.
    pub spec: Object,
    /// Live log source.
    pub live_source: String,
    /// Simulator source.
    pub sim_source: SimSource,
    /// Orders.
    pub n_orders: u64,
    /// Matched orders.
    pub n_matched: u64,
    /// Unmatched live orders.
    pub n_unmatched_live: u64,
    /// Unmatched sim orders.
    pub n_unmatched_sim: u64,
    /// Orders overlapping at the same price.
    pub n_overlap_same_px: u64,
    /// 2x2 table.
    pub two_by_two: TwoByTwo,
    /// Bias.
    pub bias: Bias,
    /// McNemar.
    pub mcnemar: McNemar,
    /// By queue decile.
    pub by_queue_decile: Vec<DecileRow>,
    /// By liquidity bucket.
    pub by_liquidity: Vec<LiquidityRow>,
    /// Time to fill.
    pub ttf: Ttf,
    /// Markouts.
    pub markouts: Vec<MarkoutRow>,
    /// Markout differences.
    pub markout_diff: Vec<MarkoutDiffRow>,
    /// Discordant markouts.
    pub sim_only_vs_live_only_markout: Vec<DiscordantMarkoutRow>,
    /// Equivalence.
    pub equivalence: Equivalence,
    /// FDR.
    pub fdr: Fdr,
    /// Verdict.
    pub verdict: ScoreVerdict,
    /// Data quality.
    pub data_quality: Option<DataQuality>,
    /// Files.
    pub files: Files,
    /// Warnings.
    pub warnings: Vec<String>,
    /// 1.1 (3.11 #9): live orders without an MBO twin.
    #[serde(default)]
    pub n_twin_unmatched: Option<u64>,
    /// 1.1 (3.11 #9): twin match rate.
    #[serde(default)]
    pub twin_match_rate: Option<f64>,
    /// 1.1 (3.11 #9): cluster-robust primary test.
    #[serde(default)]
    pub discordance_clustered: Option<DiscordanceClustered>,
    /// 1.1 (3.11 #9): taker fills per arm.
    #[serde(default)]
    pub n_taker_fills: Option<ArmCounts>,
    /// 1.1 (3.11 #9): fills-only live log.
    #[serde(default)]
    pub fills_only: Option<bool>,
}
doc!(ScorecardSummary, "qt.scorecard_summary");

// ---- referee, why, report, export, misc -------------------------------------------------------------------------

/// Simulator under test.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SimulatorEcho {
    /// Name.
    pub name: String,
    /// Version.
    pub version: Option<String>,
}

/// Discordant fill outcomes.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Discordant {
    /// Sim only.
    pub sim_only: u64,
    /// Exact only.
    pub exact_only: u64,
}

/// Referee tolerance echo.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToleranceEcho {
    /// Discordant orders allowed.
    pub max_discordant: u64,
    /// Quantity mismatches allowed.
    pub max_qty_mismatch: u64,
    /// Max abs first-fill time error.
    pub max_abs_ttf_err_ns: u64,
}

/// One disagreeing order.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RefereeExample {
    /// Order key.
    pub order_key: String,
    /// Exact outcome.
    pub exact: Object,
    /// Simulator outcome.
    pub sim: Object,
}

/// Result of `referee run`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RefereeReport {
    /// `qt.referee_report`.
    pub schema: Tag<RefereeReport>,
    /// 1.
    pub schema_version: V1,
    /// Spec echo.
    pub spec: Object,
    /// Simulator.
    pub simulator: SimulatorEcho,
    /// Orders.
    pub n_orders: u64,
    /// Both filled.
    pub n_both_filled: u64,
    /// Discordant outcomes.
    pub discordant: Discordant,
    /// Filled-quantity mismatches.
    pub qty_mismatch: u64,
    /// Max abs first-fill time error.
    pub max_abs_ttf_err_ns: Option<i64>,
    /// Median first-fill time error.
    pub ttf_err_p50_ns: Option<i64>,
    /// Counts by reason.
    pub by_reason: IndexMap<String, u64>,
    /// Tolerance.
    pub tolerance: ToleranceEcho,
    /// Within tolerance.
    pub pass: bool,
    /// First 20 examples.
    pub examples: Vec<RefereeExample>,
}
doc!(RefereeReport, "qt.referee_report");

/// The traced order.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WhySubject {
    /// Shadow id.
    pub shadow_id: Option<String>,
    /// Live order key.
    pub order_key: Option<String>,
}

/// Outcome of the traced order.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WhyOutcome {
    /// Filled quantity.
    pub filled_qty: u64,
    /// First fill time.
    pub ts_first_fill: Option<i64>,
    /// End state.
    pub end_state: String,
}

/// One record at the order's level.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WhyEvent {
    /// Event time.
    pub ts_event: i64,
    /// Sequence.
    pub sequence: u64,
    /// Action letter.
    pub action: String,
    /// Order id.
    pub order_id: u64,
    /// Side letter (B, A, N).
    pub side: String,
    /// Price.
    pub price: Option<i64>,
    /// Size.
    pub size: u64,
    /// Book effect.
    pub effect: String,
    /// The order is ahead of the traced order.
    pub is_ahead: bool,
    /// Queue ahead after.
    pub queue_ahead_after: u64,
    /// Rank after.
    pub rank_after: u32,
    /// Level quantity after.
    pub level_qty_after: u64,
    /// Fill quantity of the traced order.
    pub fill_qty: u64,
    /// Note.
    pub note: Option<String>,
}

/// Event-by-event trace of one order.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WhyTrace {
    /// `qt.why_trace`.
    pub schema: Tag<WhyTrace>,
    /// 1.
    pub schema_version: V1,
    /// Subject.
    pub subject: WhySubject,
    /// Instrument.
    pub instrument_id: u32,
    /// Side letter.
    pub side: String,
    /// Price.
    pub price: Option<i64>,
    /// Quantity.
    pub qty: u64,
    /// Activation time.
    pub ts_live: Option<i64>,
    /// Outcome.
    pub outcome: WhyOutcome,
    /// Events (capped at max_events).
    pub events: Vec<WhyEvent>,
    /// More events existed.
    pub truncated: bool,
    /// 1.1: zoo model compared with the exact trace.
    #[serde(default)]
    pub model_id: Option<String>,
    /// 1.1: the model's queue-ahead estimate per event, aligned with `events`.
    #[serde(default)]
    pub model_queue_ahead: Option<Vec<Option<f64>>>,
    /// 1.1: first divergence of model and exact queue ahead.
    #[serde(default)]
    pub first_divergence: Option<WhyDivergence>,
    /// 1.1: plain-language conclusion.
    #[serde(default)]
    pub conclusion: Option<String>,
    /// 1.1: caveats.
    #[serde(default)]
    pub warnings: Vec<String>,
}
doc!(WhyTrace, "qt.why_trace");

/// 1.1: first event after which the model's and the exact queue ahead differ.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WhyDivergence {
    /// Index into `events`.
    pub index: u64,
    /// `ts_event` of that event.
    pub ts_event: i64,
}

/// Summary of `report`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportSummary {
    /// `qt.report_summary`.
    pub schema: Tag<ReportSummary>,
    /// 1.
    pub schema_version: V1,
    /// Inputs.
    pub inputs: Vec<String>,
    /// Output path.
    pub out: String,
    /// Sections.
    pub sections: Vec<String>,
    /// Size.
    pub bytes: u64,
    /// sha256.
    pub sha256: String,
}
doc!(ReportSummary, "qt.report_summary");

/// Export target.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExportTarget {
    /// hftbacktest snippet.
    #[default]
    Hftbacktest,
    /// NautilusTrader snippet.
    Nautilus,
    /// L2 npz.
    L2Npz,
    /// Latency npz.
    LatencyNpz,
}

/// Summary of `export`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExportSummary {
    /// `qt.export_summary`.
    pub schema: Tag<ExportSummary>,
    /// 1.
    pub schema_version: V1,
    /// Target.
    pub target: ExportTarget,
    /// Files.
    pub files: Files,
    /// Text.
    pub text: Option<String>,
}
doc!(ExportSummary, "qt.export_summary");

/// One written fixture directory.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureEntry {
    /// Name.
    pub name: String,
    /// Path.
    pub path: String,
    /// Files.
    pub files: Files,
}

/// Summary of `referee fixtures`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixturesSummary {
    /// `qt.fixtures_summary`.
    pub schema: Tag<FixturesSummary>,
    /// 1.
    pub schema_version: V1,
    /// Output directory.
    pub out: String,
    /// Fixtures.
    pub fixtures: Vec<FixtureEntry>,
}
doc!(FixturesSummary, "qt.fixtures_summary");

/// Result of `doctor`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Doctor {
    /// `qt.doctor`.
    pub schema: Tag<Doctor>,
    /// 1.
    pub schema_version: V1,
    /// Python version.
    pub python: String,
    /// Platform.
    pub platform: String,
    /// Machine.
    pub machine: String,
    /// Core version.
    pub core_version: Option<String>,
    /// Schemas consistent.
    pub schemas_ok: bool,
    /// PostgreSQL status.
    pub postgres: Option<String>,
    /// Docker status.
    pub docker: Option<String>,
    /// Problems.
    pub problems: Vec<String>,
}
doc!(Doctor, "qt.doctor");

/// Result of `bench replay`.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Bench {
    /// `qt.bench`.
    pub schema: Tag<Bench>,
    /// 1.
    pub schema_version: V1,
    /// Dataset.
    pub data: String,
    /// Records.
    pub records: u64,
    /// Wall seconds per repeat.
    pub wall_s: Vec<f64>,
    /// Median records per second.
    pub records_per_s_median: Option<f64>,
}
doc!(Bench, "qt.bench");

// ---- service documents mirrored for clients ---------------------------------------------------------------------

/// Job progress.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Progress {
    /// 0..1.
    pub fraction: f64,
    /// Stage.
    pub stage: String,
}

/// Resource a job acts on.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResourceRef {
    /// Resource type.
    pub r#type: String,
    /// Resource id.
    pub id: String,
}

/// One validation error location.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProblemError {
    /// Location (strings and integers).
    pub loc: Vec<Value>,
    /// Message.
    pub msg: String,
}

/// RFC 9457 problem details.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Problem {
    /// `qt.problem`.
    pub schema: Tag<Problem>,
    /// 1.
    pub schema_version: V1,
    /// Type URI.
    pub r#type: String,
    /// Title.
    pub title: String,
    /// HTTP status.
    pub status: u16,
    /// Detail.
    pub detail: Option<String>,
    /// Instance.
    pub instance: Option<String>,
    /// QT code.
    pub code: String,
    /// Request id.
    pub request_id: String,
    /// Validation errors.
    pub errors: Option<Vec<ProblemError>>,
}
doc!(Problem, "qt.problem");

/// Job kind.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JobKind {
    /// Ingest.
    Ingest,
    /// Replay.
    #[default]
    Replay,
    /// Calibration.
    Calibration,
    /// Scorecard.
    Scorecard,
    /// Report.
    Report,
    /// Dataset validation.
    DatasetValidate,
    /// Deletion.
    Deletion,
}

/// Job state.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JobState {
    /// Queued.
    #[default]
    Queued,
    /// Running.
    Running,
    /// Succeeded.
    Succeeded,
    /// Failed.
    Failed,
    /// Canceled.
    Canceled,
    /// Expired.
    Expired,
}

/// A service job.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Job {
    /// `qt.job`.
    pub schema: Tag<Job>,
    /// 1.
    pub schema_version: V1,
    /// Id.
    pub id: String,
    /// Kind.
    pub kind: JobKind,
    /// State.
    pub state: JobState,
    /// Progress.
    pub progress: Progress,
    /// Resource.
    pub resource: ResourceRef,
    /// RFC 3339.
    pub created_at: String,
    /// RFC 3339.
    pub started_at: Option<String>,
    /// RFC 3339.
    pub finished_at: Option<String>,
    /// Problem.
    pub error: Option<Problem>,
    /// Usage by meter.
    pub usage: IndexMap<String, f64>,
    /// RFC 3339.
    pub files_expire_at: Option<String>,
}
doc!(Job, "qt.job");

/// One downloadable job file.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ManifestFile {
    /// Name.
    pub name: String,
    /// Size.
    pub bytes: u64,
    /// sha256.
    pub sha256: String,
    /// Content type.
    pub content_type: String,
    /// Presigned URL.
    pub url: String,
    /// RFC 3339.
    pub expires_at: String,
}

/// Downloadable files of a job.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileManifest {
    /// `qt.file_manifest`.
    pub schema: Tag<FileManifest>,
    /// 1.
    pub schema_version: V1,
    /// Job id.
    pub job_id: String,
    /// Files.
    pub files: Vec<ManifestFile>,
}
doc!(FileManifest, "qt.file_manifest");

/// Cost and time estimate.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Estimate {
    /// `qt.estimate`.
    pub schema: Tag<Estimate>,
    /// 1.
    pub schema_version: V1,
    /// Kind.
    pub kind: String,
    /// Records.
    pub records_est: u64,
    /// Shadows.
    pub shadows_est: u64,
    /// CPU seconds.
    pub cpu_seconds_est: f64,
    /// Output bytes.
    pub bytes_out_est: u64,
    /// Cost units.
    pub cost_units: f64,
    /// ETA seconds.
    pub eta_seconds: f64,
}
doc!(Estimate, "qt.estimate");
