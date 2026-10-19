//! Serde mirrors of the spec objects of SPEC 4.2 (source of truth: `python/queuetruth/schemas/specs.py`).
//!
//! Unknown fields are rejected; defaults equal the Pydantic defaults (tested against `schemas/samples/*.json`).
//! Range constraints are validated by the Python models and by the consuming engine (`QT-CONFIG-001`).
//!
//! Schema 1.1 (SPEC 3.11, E38): fields marked "1.1" are additive and default to the v0.1 behaviour; a 1.1
//! field holding its default does not enter `run_id` (`crate::V1_1_DEFAULTS`).

use indexmap::IndexMap;
use qt_types::LatencyModel;
use serde::{Deserialize, Serialize};
use serde_json::Value;

// ---- references -------------------------------------------------------------------------------------------------

/// `{"path": ...}`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PathRef {
    /// Local path.
    pub path: String,
}

/// `{"dataset_id": ...}`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DatasetIdRef {
    /// Service dataset id.
    pub dataset_id: String,
}

/// `{"upload_id": ...}`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UploadRef {
    /// Service upload id.
    pub upload_id: String,
}

/// One output file of a service job.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JobFile {
    /// Job id.
    pub job_id: String,
    /// File name.
    pub name: String,
}

/// `{"job_file": {...}}`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JobFileRef {
    /// The job file.
    pub job_file: JobFile,
}

/// `DatasetRef = {"path"} | {"dataset_id"}`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum DatasetRef {
    /// Local path.
    Path(PathRef),
    /// Service dataset id.
    DatasetId(DatasetIdRef),
}

impl DatasetRef {
    /// `{"path": p}`.
    pub fn path(p: impl Into<String>) -> Self {
        Self::Path(PathRef { path: p.into() })
    }

    /// The local path, if this is a path reference.
    pub fn as_path(&self) -> Option<&str> {
        match self {
            Self::Path(p) => Some(&p.path),
            Self::DatasetId(_) => None,
        }
    }
}

/// `FileRef = {"path"} | {"upload_id"} | {"job_file": {job_id, name}}`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum FileRef {
    /// Local path.
    Path(PathRef),
    /// Service upload.
    Upload(UploadRef),
    /// Service job output.
    JobFile(JobFileRef),
}

impl FileRef {
    /// `{"path": p}`.
    pub fn path(p: impl Into<String>) -> Self {
        Self::Path(PathRef { path: p.into() })
    }

    /// The local path, if this is a path reference.
    pub fn as_path(&self) -> Option<&str> {
        match self {
            Self::Path(p) => Some(&p.path),
            _ => None,
        }
    }
}

// ---- building blocks --------------------------------------------------------------------------------------------

/// Trade rows before or after depth rows within an L2 batch.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TradePositionName {
    /// Trades first.
    #[default]
    Before,
    /// Depth first.
    After,
}

/// L2 view synthesis settings.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct L2Config {
    /// Top-N levels per side (0 = all).
    pub depth_levels: u16,
    /// Conflation cadence (0 = per event).
    pub cadence_ns: i64,
    /// Trade-vs-depth ordering.
    pub trade_position: TradePositionName,
    /// Emit order counts.
    pub include_counts: bool,
    /// Emit hidden trades.
    pub emit_hidden_trades: bool,
}

impl Default for L2Config {
    fn default() -> Self {
        Self {
            depth_levels: 10,
            cadence_ns: 0,
            trade_position: TradePositionName::Before,
            include_counts: true,
            emit_hidden_trades: true,
        }
    }
}

/// Block selection.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlockWindow {
    /// Block indices.
    pub index: Vec<u32>,
    /// Blocks per date.
    #[serde(default = "five")]
    pub n_blocks: u32,
}

fn five() -> u32 {
    5
}

/// Train/test window: dates or blocks; both null = all data.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct WindowSpec {
    /// Dates (`YYYY-MM-DD`).
    pub dates: Option<Vec<String>>,
    /// Blocks.
    pub blocks: Option<BlockWindow>,
}

/// Evenly spaced grid.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GridRange {
    /// Lower end.
    pub lo: f64,
    /// Upper end.
    pub hi: f64,
    /// Points.
    pub n: u32,
}

/// Optimizer grids.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct CalibrationGrid {
    /// ln n grid.
    pub alloc: GridRange,
    /// Databento k grid.
    pub databento: GridRange,
    /// Survival k grid.
    pub survival: GridRange,
}

impl Default for CalibrationGrid {
    fn default() -> Self {
        Self {
            alloc: GridRange {
                lo: -3.0,
                hi: 4.0,
                n: 141,
            },
            databento: GridRange {
                lo: -0.99,
                hi: 0.99,
                n: 199,
            },
            survival: GridRange {
                lo: -0.98,
                hi: 0.9,
                n: 25,
            },
        }
    }
}

/// Bootstrap block rule.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum BootstrapBlock {
    /// Day blocks with >= 5 dates, else 30-minute blocks.
    #[default]
    #[serde(rename = "auto")]
    Auto,
    /// Session dates.
    #[serde(rename = "day")]
    Day,
    /// 30-minute local blocks.
    #[serde(rename = "30min")]
    Min30,
}

/// Block bootstrap settings.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BootstrapSpec {
    /// Resamples.
    pub b: u32,
    /// Block rule.
    #[serde(default)]
    pub block: BootstrapBlock,
    /// Seed.
    #[serde(default)]
    pub seed: u64,
}

fn calib_bootstrap() -> BootstrapSpec {
    BootstrapSpec {
        b: 200,
        block: BootstrapBlock::Auto,
        seed: 0,
    }
}

fn score_bootstrap() -> BootstrapSpec {
    BootstrapSpec {
        b: 1000,
        block: BootstrapBlock::Auto,
        seed: 0,
    }
}

/// Placement with a weight.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Placement {
    /// Ticks relative to the touch.
    pub offset_ticks: i32,
    /// Weight.
    pub weight: f64,
}

/// Quantity rule kind.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QtyKind {
    /// Fixed lots.
    Lots,
    /// Median order size of the instrument-day.
    MedianOrderSize,
}

/// Quantity rule with a weight.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QtyRule {
    /// Kind.
    pub kind: QtyKind,
    /// Lots (kind lots).
    #[serde(default)]
    pub lots: Option<u64>,
    /// Weight.
    pub weight: f64,
}

/// Default zoo / calibration / scorer horizons.
pub const DEFAULT_HORIZONS_NS: [i64; 4] = [100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000];

fn horizons() -> Vec<i64> {
    DEFAULT_HORIZONS_NS.to_vec()
}
fn one() -> u32 {
    1
}
fn t() -> bool {
    true
}
fn h_max_300s() -> i64 {
    300_000_000_000
}

// ---- top-level specs --------------------------------------------------------------------------------------------

/// Ingest format.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IngestFormat {
    /// Nasdaq TotalView-ITCH 5.0.
    Itch,
    /// Databento DBN MBO.
    Dbn,
    /// et MBO CSV layout.
    Etcsv,
    /// Our normalized Parquet.
    Parquet,
    /// Bitstamp capture.
    Bitstamp,
    /// Bitfinex capture.
    Bitfinex,
    /// Coinbase full channel capture.
    Coinbase,
    /// Tardis L2 CSV.
    TardisL2,
}

/// Session time zone.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SessionTz {
    /// US Eastern.
    #[serde(rename = "America/New_York")]
    AmericaNewYork,
    /// UTC.
    #[serde(rename = "UTC")]
    Utc,
}

/// `ingest`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IngestSpec {
    /// Format.
    pub format: IngestFormat,
    /// Inputs.
    pub inputs: Vec<FileRef>,
    /// Venue rule set name.
    pub venue: String,
    /// Source venue (null = from format).
    #[serde(default)]
    pub source_venue: Option<String>,
    /// Output directory.
    #[serde(default)]
    pub out: Option<String>,
    /// Symbol filter.
    #[serde(default)]
    pub symbols: Option<Vec<String>>,
    /// Session date.
    #[serde(default)]
    pub session_date: Option<String>,
    /// Local midnight as UTC ns.
    #[serde(default)]
    pub midnight_utc_ns: Option<i64>,
    /// Time zone.
    #[serde(default)]
    pub tz: Option<SessionTz>,
    /// etcsv instruments.csv.
    #[serde(default)]
    pub instruments: Option<FileRef>,
    /// Price exponent.
    #[serde(default = "price_exp_default")]
    pub price_exp: i8,
    /// Size exponent (null = format default).
    #[serde(default)]
    pub size_exp: Option<i8>,
    /// Tick size (decimal string).
    #[serde(default)]
    pub tick_size: Option<String>,
    /// Lot size (decimal string).
    #[serde(default)]
    pub lot_size: Option<String>,
    /// Format options.
    #[serde(default)]
    pub options: IndexMap<String, Value>,
    /// Fail on a non-clean verdict.
    #[serde(default)]
    pub strict: bool,
    /// 1.1 (3.11 #18): continuous-session open, seconds after local midnight (null = venue default / options).
    #[serde(default)]
    pub open_s: Option<u32>,
    /// 1.1 (3.11 #18): continuous-session close, seconds after local midnight (null = venue default / options).
    #[serde(default)]
    pub close_s: Option<u32>,
}

fn price_exp_default() -> i8 {
    -9
}

/// BBO output mode.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BboModeName {
    /// No BBO output.
    None,
    /// Only rows whose values changed.
    #[default]
    Change,
    /// Every event.
    Full,
}

/// Trajectory output mode.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TrajectoryModeName {
    /// None.
    None,
    /// Rows where queue ahead, rank or filled changed.
    #[default]
    Ahead,
    /// Every event touching the level.
    Full,
}

/// Shadow fill rule (1.1, 3.11 #1).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FillRuleName {
    /// Exact FIFO rule of SPEC 5.2 (default).
    #[default]
    Exact,
    /// Rule of the author's earlier et prototype (RD-5 parity).
    Et,
}

/// Side-`N` print policy at the shadow price (1.1, 3.11 #1; SPEC 5.2.4a).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HiddenPrintPolicyName {
    /// Hidden prints never fill a shadow.
    Ignore,
    /// A hidden print fills a shadow behind displayed interest.
    Behind,
    /// As `behind`, plus hidden trade-throughs.
    BehindAndThrough,
}

/// One real order removed from the tape before shadows are injected (1.1, 3.11 #1; ruling A1).
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExcludeOrderId {
    /// Instrument.
    pub instrument_id: u32,
    /// Venue order id.
    pub order_id: u64,
    /// 1.1: session date (`YYYY-MM-DD`) of the order (venue ids can repeat across days); `None` = every date.
    #[serde(default)]
    pub session_date: Option<String>,
}

/// `replay`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReplaySpec {
    /// Dataset.
    pub dataset: DatasetRef,
    /// Output directory.
    #[serde(default)]
    pub out: Option<String>,
    /// Venue override.
    #[serde(default)]
    pub venue: Option<String>,
    /// Shadow specs.
    #[serde(default)]
    pub shadows: Option<FileRef>,
    /// Shadow amends.
    #[serde(default)]
    pub amends: Option<FileRef>,
    /// Latency.
    #[serde(default)]
    pub latency: LatencyModel,
    /// Rank oracle.
    #[serde(default)]
    pub rank_oracle: bool,
    /// Sweep gap.
    #[serde(default = "sweep_gap_default")]
    pub sweep_gap_ns: i64,
    /// BBO mode.
    #[serde(default)]
    pub bbo: BboModeName,
    /// Depth levels.
    #[serde(default)]
    pub depth_levels: u16,
    /// Trajectory mode.
    #[serde(default)]
    pub trajectory: TrajectoryModeName,
    /// Race window.
    #[serde(default)]
    pub race_window_ns: i64,
    /// Extract real orders.
    #[serde(default)]
    pub real_orders: bool,
    /// Write L2 (null = do not).
    #[serde(default)]
    pub l2: Option<L2Config>,
    /// Trade parity.
    #[serde(default)]
    pub parity_trades: bool,
    /// BBO parity reference.
    #[serde(default)]
    pub parity_bbo: Option<FileRef>,
    /// Strict mode.
    #[serde(default)]
    pub strict: bool,
    /// Threads (execution-only).
    #[serde(default = "one")]
    pub threads: u32,
    /// Seed.
    #[serde(default)]
    pub seed: u64,
    /// 1.1 (3.11 #1): shadow fill rule.
    #[serde(default)]
    pub fill_rule: FillRuleName,
    /// 1.1 (3.11 #1): hidden-print policy (null = rule-set default).
    #[serde(default)]
    pub hidden_print_policy: Option<HiddenPrintPolicyName>,
    /// 1.1 (3.11 #1): real orders removed from the tape (A1).
    #[serde(default)]
    pub exclude_order_ids: Vec<ExcludeOrderId>,
}

fn sweep_gap_default() -> i64 {
    2_000_000
}

/// Shadow allocation across instrument-days.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Allocation {
    /// Equal per instrument-day.
    #[default]
    Equal,
    /// Proportional to records.
    Proportional,
}

/// Side letter in specs.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SideName {
    /// Bid.
    #[serde(rename = "B")]
    B,
    /// Ask.
    #[serde(rename = "A")]
    A,
}

/// `shadows sample`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShadowSampleSpec {
    /// Dataset.
    pub dataset: DatasetRef,
    /// Output file.
    #[serde(default)]
    pub out: Option<String>,
    /// Shadows.
    pub n: u64,
    /// Seed.
    #[serde(default)]
    pub seed: u64,
    /// Allocation.
    #[serde(default)]
    pub allocation: Allocation,
    /// Sides.
    #[serde(default = "sides_default")]
    pub sides: Vec<SideName>,
    /// Placements.
    #[serde(default = "placements_default")]
    pub placements: Vec<Placement>,
    /// Quantity rules.
    #[serde(default = "qty_default")]
    pub qty: Vec<QtyRule>,
    /// Seconds after the open excluded.
    #[serde(default = "open_default")]
    pub window_open_s: u32,
    /// Seconds before the close excluded.
    #[serde(default = "close_default")]
    pub window_close_s: u32,
    /// Shadow lifetime.
    #[serde(default = "h_max_300s")]
    pub lifetime_ns: i64,
    /// Latency.
    #[serde(default)]
    pub latency_ns: i64,
}

fn sides_default() -> Vec<SideName> {
    vec![SideName::B, SideName::A]
}
fn placements_default() -> Vec<Placement> {
    vec![
        Placement {
            offset_ticks: 0,
            weight: 0.6,
        },
        Placement {
            offset_ticks: -1,
            weight: 0.2,
        },
        Placement {
            offset_ticks: -2,
            weight: 0.1,
        },
        Placement {
            offset_ticks: 1,
            weight: 0.1,
        },
    ]
}
fn qty_default() -> Vec<QtyRule> {
    vec![
        QtyRule {
            kind: QtyKind::Lots,
            lots: Some(1),
            weight: 0.5,
        },
        QtyRule {
            kind: QtyKind::MedianOrderSize,
            lots: None,
            weight: 0.5,
        },
    ]
}
fn open_default() -> u32 {
    300
}
fn close_default() -> u32 {
    600
}

/// Mirror placement rule.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MirrorPlacement {
    /// Submit at the real add time, latency 0.
    #[default]
    BehindReal,
}

/// `shadows mirror-real`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MirrorSpec {
    /// Dataset.
    pub dataset: DatasetRef,
    /// Output file.
    #[serde(default)]
    pub out: Option<String>,
    /// Shadows.
    #[serde(default = "n3000")]
    pub n: u64,
    /// Seed.
    #[serde(default)]
    pub seed: u64,
    /// Stratify by instrument x outcome.
    #[serde(default = "t")]
    pub stratify: bool,
    /// Placement.
    #[serde(default)]
    pub placement: MirrorPlacement,
}

fn n3000() -> u64 {
    3000
}

/// Model mode.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ModeName {
    /// hbt semantics including quirks.
    #[default]
    Faithful,
    /// Quirks fixed.
    Corrected,
}

/// Exchange fill rule.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExchangeName {
    /// Partial fills.
    #[default]
    Partial,
    /// Whole-order fills.
    NoPartial,
}

/// `zoo`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ZooSpec {
    /// Dataset.
    pub dataset: DatasetRef,
    /// Shadows.
    pub shadows: FileRef,
    /// Output directory.
    #[serde(default)]
    pub out: Option<String>,
    /// Models (`all` or model ids).
    #[serde(default = "models_default")]
    pub models: Vec<String>,
    /// Mode.
    #[serde(default)]
    pub mode: ModeName,
    /// Exchange.
    #[serde(default)]
    pub exchange: ExchangeName,
    /// Horizons.
    #[serde(default = "horizons")]
    pub horizons_ns: Vec<i64>,
    /// Maximum horizon.
    #[serde(default = "h_max_300s")]
    pub h_max_ns: i64,
    /// L2 view.
    #[serde(default)]
    pub l2: L2Config,
    /// Particles.
    #[serde(default = "particles_default")]
    pub particles: u32,
    /// Seed.
    #[serde(default)]
    pub seed: u64,
    /// Latency.
    #[serde(default)]
    pub latency: LatencyModel,
    /// Write zoo_fills.
    #[serde(default = "t")]
    pub write_fills: bool,
    /// Write zoo_trajectory.
    #[serde(default)]
    pub write_trajectory: bool,
    /// Threads (execution-only).
    #[serde(default = "one")]
    pub threads: u32,
    /// Bootstrap resamples (Python evaluation).
    #[serde(default = "b1000")]
    pub bootstrap_b: u32,
    /// 1.1 (3.11 #7): shadow amends (null = none).
    #[serde(default)]
    pub amends: Option<FileRef>,
    /// 1.1: real orders removed from the tape (A1; as `ReplaySpec.exclude_order_ids`).
    #[serde(default)]
    pub exclude_order_ids: Vec<ExcludeOrderId>,
}

fn models_default() -> Vec<String> {
    vec!["all".into()]
}
fn particles_default() -> u32 {
    100
}
fn b1000() -> u32 {
    1000
}

/// Calibration family.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CalibrationFamily {
    /// hbt power.
    Power,
    /// hbt power2.
    Power2,
    /// hbt power3.
    Power3,
    /// Databento MC.
    Databento,
    /// NT prob_fill_on_limit.
    NtProbTouch,
    /// NT queue position + prob touch.
    NtQueueL2ProbTouch,
    /// Latency.
    Latency,
}

/// Calibration kind.
pub use crate::json::CalibrationKind;

fn spec_calibration_kind<'de, D: serde::Deserializer<'de>>(d: D) -> Result<CalibrationKind, D::Error> {
    match CalibrationKind::deserialize(d)? {
        CalibrationKind::Live => Err(serde::de::Error::custom(
            "kind: `live` is a calibration_result kind; use LiveCalibrationSpec (calibrate-live)",
        )),
        k => Ok(k),
    }
}

/// `calibrate`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CalibrationSpec {
    /// Kind (`live` is a result kind only and is rejected here, as in `specs.py`).
    #[serde(deserialize_with = "spec_calibration_kind")]
    pub kind: CalibrationKind,
    /// Family.
    pub family: CalibrationFamily,
    /// Dataset.
    #[serde(default)]
    pub dataset: Option<DatasetRef>,
    /// Shadows.
    #[serde(default)]
    pub shadows: Option<FileRef>,
    /// Live log (latency).
    #[serde(default)]
    pub live: Option<FileRef>,
    /// Train window.
    #[serde(default)]
    pub train: WindowSpec,
    /// Test window.
    #[serde(default)]
    pub test: WindowSpec,
    /// Output directory.
    #[serde(default)]
    pub out: Option<String>,
    /// Mode.
    #[serde(default)]
    pub mode: ModeName,
    /// Exchange.
    #[serde(default)]
    pub exchange: ExchangeName,
    /// L2 view.
    #[serde(default)]
    pub l2: L2Config,
    /// Horizons.
    #[serde(default = "horizons")]
    pub horizons_ns: Vec<i64>,
    /// Maximum horizon.
    #[serde(default = "h_max_300s")]
    pub h_max_ns: i64,
    /// Grids.
    #[serde(default)]
    pub grid: CalibrationGrid,
    /// Bootstrap.
    #[serde(default = "calib_bootstrap")]
    pub bootstrap: BootstrapSpec,
    /// Particles.
    #[serde(default = "particles_default")]
    pub particles: u32,
    /// Seed.
    #[serde(default)]
    pub seed: u64,
    /// Threads (execution-only).
    #[serde(default = "one")]
    pub threads: u32,
}

/// Feature population.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PopulationName {
    /// Shadows.
    #[default]
    Shadows,
    /// Real orders.
    RealOrders,
}

/// `features`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeatureSpec {
    /// Dataset.
    pub dataset: DatasetRef,
    /// Output directory.
    #[serde(default)]
    pub out: Option<String>,
    /// Population.
    #[serde(default)]
    pub population: PopulationName,
    /// Shadows.
    #[serde(default)]
    pub shadows: Option<FileRef>,
    /// Real orders to sample (null = all).
    #[serde(default)]
    pub n_real_orders: Option<u64>,
    /// Landmarks.
    #[serde(default = "landmarks_default")]
    pub landmarks_ns: Vec<i64>,
    /// Policy-cancel distance in ticks (shadows); 1.1: `None` (null) or 0 = never cancel (labels without the policy
    /// cancel, for the zoo-vs-hazard comparison of review F6).
    #[serde(default = "two")]
    pub cancel_away_ticks: Option<u32>,
    /// Maximum horizon.
    #[serde(default = "h_max_60s")]
    pub h_max_ns: i64,
    /// L2 view.
    #[serde(default)]
    pub l2: L2Config,
    /// Tracker model ids.
    #[serde(default = "trackers_default")]
    pub trackers: Vec<String>,
    /// Privileged columns.
    #[serde(default = "t")]
    pub privileged: bool,
    /// Seed.
    #[serde(default)]
    pub seed: u64,
    /// Threads (execution-only).
    #[serde(default = "one")]
    pub threads: u32,
}

fn landmarks_default() -> Vec<i64> {
    vec![0, 1_000_000_000, 5_000_000_000]
}
fn two() -> Option<u32> {
    Some(2)
}
fn h_max_60s() -> i64 {
    60_000_000_000
}
fn trackers_default() -> Vec<String> {
    ["hbt_risk_adverse", "hbt_power3[n=1]", "hbt_power3[n=3]", "hbt_log2"]
        .iter()
        .map(|s| (*s).to_owned())
        .collect()
}

/// Split scheme.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SplitScheme {
    /// By number of dates.
    #[default]
    Auto,
    /// By dates.
    Days,
    /// By blocks.
    Blocks,
}

/// Split settings.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct SplitSpec {
    /// Scheme.
    pub scheme: SplitScheme,
    /// Blocks.
    pub n_blocks: u32,
    /// Embargo.
    pub embargo_ns: i64,
}

impl Default for SplitSpec {
    fn default() -> Self {
        Self {
            scheme: SplitScheme::Auto,
            n_blocks: 5,
            embargo_ns: 60_000_000_000,
        }
    }
}

/// GBDT settings.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct GbdtSpec {
    /// Iterations.
    pub max_iter: u32,
    /// Learning rate.
    pub learning_rate: f64,
    /// Leaves.
    pub max_leaf_nodes: u32,
    /// Minimum samples per leaf.
    pub min_samples_leaf: u32,
    /// L2 regularization.
    pub l2_regularization: f64,
}

impl Default for GbdtSpec {
    fn default() -> Self {
        Self {
            max_iter: 300,
            learning_rate: 0.05,
            max_leaf_nodes: 31,
            min_samples_leaf: 100,
            l2_regularization: 1.0,
        }
    }
}

/// Recalibration.
pub use crate::json::Recalibration;

/// `fit-hazard`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HazardFitSpec {
    /// Features.
    pub features: FileRef,
    /// Output directory.
    #[serde(default)]
    pub out: Option<String>,
    /// Grid.
    #[serde(default = "grid_default")]
    pub grid_ns: Vec<i64>,
    /// Split.
    #[serde(default)]
    pub split: SplitSpec,
    /// GBDT.
    #[serde(default)]
    pub gbdt: GbdtSpec,
    /// Recalibration.
    #[serde(default)]
    pub recalibration: Recalibration,
    /// Privileged model.
    #[serde(default)]
    pub privileged: bool,
    /// Seed.
    #[serde(default)]
    pub seed: u64,
    /// Bootstrap resamples.
    #[serde(default = "b1000")]
    pub bootstrap_b: u32,
    /// Zoo predictions for comparison.
    #[serde(default)]
    pub zoo_predictions: Option<FileRef>,
    /// 1.1: `qt.zoo_shadows` of the zoo predictions (null = `zoo_shadows.parquet` next to `zoo_predictions`).
    #[serde(default)]
    pub zoo_shadows: Option<FileRef>,
}

fn grid_default() -> Vec<i64> {
    vec![
        100_000_000,
        250_000_000,
        500_000_000,
        1_000_000_000,
        2_000_000_000,
        5_000_000_000,
        10_000_000_000,
        30_000_000_000,
        60_000_000_000,
    ]
}

/// Live venue.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LiveVenue {
    /// Binance.
    Binance,
    /// Bybit.
    Bybit,
    /// OKX.
    Okx,
    /// Coinbase Advanced Trade.
    Coinbase,
    /// Kraken.
    Kraken,
    /// Deribit.
    Deribit,
    /// Hyperliquid.
    Hyperliquid,
    /// IBKR Flex.
    IbkrFlex,
    /// FIX 4.4 drop copy.
    Fix44,
    /// 1.1 (3.11 #13): hftbacktest simulator output.
    SimHftbacktest,
    /// 1.1 (3.11 #13): NautilusTrader simulator output.
    SimNautilus,
}

/// FIX options.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct FixOptions {
    /// Delimiter (`auto`, SOH, `|`).
    pub delimiter: String,
    /// Regex for a log-line local timestamp.
    pub local_ts_regex: Option<String>,
    /// 1.1 (3.11 #13): venue name of the canonical log (null = `fix44`).
    pub venue_name: Option<String>,
    /// 1.1: messages failing CheckSum / BodyLength: exclude or keep.
    pub checksum: FixChecksum,
    /// 1.1: QuickFIX data dictionary XML path(s).
    pub data_dictionary: Option<OneOrMany>,
    /// 1.1: session (file stem or counterparty CompID) -> venue.
    pub session_venues: IndexMap<String, String>,
    /// 1.1: tag holding the instrument instead of Symbol(55).
    pub instrument_tag: Option<u32>,
}

impl Default for FixOptions {
    fn default() -> Self {
        Self {
            delimiter: "auto".into(),
            local_ts_regex: None,
            venue_name: None,
            checksum: FixChecksum::Exclude,
            data_dictionary: None,
            session_venues: IndexMap::new(),
            instrument_tag: None,
        }
    }
}

/// Messages failing CheckSum(10) / BodyLength(9) (1.1).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FixChecksum {
    /// Dropped with a warning (default).
    #[default]
    Exclude,
    /// Imported anyway.
    Keep,
}

/// One string or a list of strings (`str | list[str]` in `specs.py`).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum OneOrMany {
    /// One value.
    One(String),
    /// Several values.
    Many(Vec<String>),
}

impl OneOrMany {
    /// The values as a list.
    pub fn to_vec(&self) -> Vec<String> {
        match self {
            Self::One(s) => vec![s.clone()],
            Self::Many(v) => v.clone(),
        }
    }
}

/// `live import`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiveImportSpec {
    /// Venue.
    pub venue: LiveVenue,
    /// Inputs.
    pub inputs: Vec<FileRef>,
    /// Output directory.
    #[serde(default)]
    pub out: Option<String>,
    /// Account.
    #[serde(default)]
    pub account: Option<String>,
    /// Instrument map.
    #[serde(default)]
    pub instrument_map: Option<FileRef>,
    /// Append to an existing log.
    #[serde(default)]
    pub append: bool,
    /// FIX options.
    #[serde(default)]
    pub fix: FixOptions,
    /// 1.1 (3.11 #13): account time zone (IANA) of venue-local times (null = UTC).
    #[serde(default)]
    pub tz: Option<String>,
}

/// Simulator arm.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum SimSpec {
    /// External simulator log.
    External {
        /// Canonical live-log directory.
        log: FileRef,
    },
    /// Exact replay.
    Exact {},
    /// Zoo model.
    Zoo {
        /// Model id.
        model: String,
        /// L2 view.
        #[serde(default)]
        l2: L2Config,
    },
}

/// Entry time rule.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EntryName {
    /// Exchange ack time.
    #[default]
    ExchangeAck,
    /// Local send time plus latency.
    SendPlusLatency,
}

/// FDR settings.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct FdrSpec {
    /// Method.
    pub method: crate::json::FdrMethod,
    /// Level.
    pub q: f64,
}

impl Default for FdrSpec {
    fn default() -> Self {
        Self {
            method: crate::json::FdrMethod::Bh,
            q: 0.05,
        }
    }
}

/// Equivalence margins.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct EquivalenceSpec {
    /// Fill-rate margin.
    pub fill_rate_margin: f64,
    /// Markout margin in ticks.
    pub markout_margin_ticks: f64,
    /// Markout horizon.
    pub markout_horizon_ns: i64,
}

impl Default for EquivalenceSpec {
    fn default() -> Self {
        Self {
            fill_rate_margin: 0.02,
            markout_margin_ticks: 0.25,
            markout_horizon_ns: 1_000_000_000,
        }
    }
}

/// Scorecard gates.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct GatesSpec {
    /// Fail if |fill-rate bias| exceeds this.
    pub max_abs_fill_rate_bias: Option<f64>,
    /// Fail if not equivalent.
    pub require_equivalence: bool,
}

/// `score`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScorecardSpec {
    /// Live log directory.
    pub live: FileRef,
    /// Simulator arm.
    pub sim: SimSpec,
    /// Dataset.
    #[serde(default)]
    pub dataset: Option<DatasetRef>,
    /// Output directory.
    #[serde(default)]
    pub out: Option<String>,
    /// Entry rule.
    #[serde(default)]
    pub entry: EntryName,
    /// Latency.
    #[serde(default)]
    pub latency: Option<LatencyModel>,
    /// Markout horizons.
    #[serde(default = "markout_horizons_default")]
    pub markout_horizons_ns: Vec<i64>,
    /// Markout references.
    #[serde(default = "markout_refs_default")]
    pub markout_refs: Vec<crate::json::MarkoutRef>,
    /// Time-to-fill horizons.
    #[serde(default = "horizons")]
    pub ttf_horizons_ns: Vec<i64>,
    /// Bootstrap.
    #[serde(default = "score_bootstrap")]
    pub bootstrap: BootstrapSpec,
    /// FDR.
    #[serde(default)]
    pub fdr: FdrSpec,
    /// Equivalence.
    #[serde(default)]
    pub equivalence: EquivalenceSpec,
    /// Gates.
    #[serde(default)]
    pub gates: GatesSpec,
    /// Permutations.
    #[serde(default = "perm_default")]
    pub perm_b: u32,
    /// 1.1 (3.11 #8): evaluation horizon H (ruling A9).
    #[serde(default = "window_60s")]
    pub window_ns: i64,
    /// 1.1 (3.11 #8): twin matching window for self-exclusion.
    #[serde(default = "match_window_1ms")]
    pub match_window_ns: i64,
    /// 1.1 (3.11 #8): self-exclusion (ruling A1).
    #[serde(default = "t")]
    pub self_exclude: bool,
    /// 1.1 (3.11 #21): markouts past the end of data (null = undefined).
    #[serde(default)]
    pub markout_past_end: Option<MarkoutPastEnd>,
}

/// Markout value past the end of data (1.1, 3.11 #21).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MarkoutPastEnd {
    /// The last quote before the end of data.
    LastQuote,
}

fn window_60s() -> i64 {
    60_000_000_000
}
fn match_window_1ms() -> i64 {
    1_000_000
}

fn markout_horizons_default() -> Vec<i64> {
    vec![
        50_000_000,
        100_000_000,
        1_000_000_000,
        10_000_000_000,
        15_000_000_000,
        60_000_000_000,
        300_000_000_000,
    ]
}
fn markout_refs_default() -> Vec<crate::json::MarkoutRef> {
    vec![crate::json::MarkoutRef::Mid]
}
fn perm_default() -> u32 {
    2000
}

/// `report`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportSpec {
    /// Output directories.
    pub inputs: Vec<String>,
    /// HTML path.
    #[serde(default)]
    pub out: Option<String>,
    /// Title.
    #[serde(default = "title_default")]
    pub title: String,
    /// Why traces.
    #[serde(default = "max_why_default")]
    pub max_why: u32,
    /// 1.1: zoo leaderboard sort horizon (absolute fill-rate bias; null = 60 s).
    #[serde(default)]
    pub leaderboard_sort_horizon_ns: Option<i64>,
}

fn title_default() -> String {
    "queuetruth report".into()
}
fn max_why_default() -> u32 {
    20
}

/// Simulator under test.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SimulatorInfo {
    /// Name.
    pub name: String,
    /// Version.
    #[serde(default)]
    pub version: Option<String>,
}

fn simulator_default() -> SimulatorInfo {
    SimulatorInfo {
        name: "unknown".into(),
        version: None,
    }
}

/// Referee tolerance.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct RefereeTolerance {
    /// Discordant orders allowed.
    pub max_discordant: u64,
    /// Quantity mismatches allowed.
    pub max_qty_mismatch: u64,
    /// Max abs first-fill time error.
    pub max_abs_ttf_err_ns: u64,
}

/// `referee run`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RefereeSpec {
    /// Dataset.
    pub dataset: DatasetRef,
    /// Schedule (qt.shadow_spec).
    pub schedule: FileRef,
    /// Simulator fills.
    pub sim_fills: FileRef,
    /// Simulator.
    #[serde(default = "simulator_default")]
    pub simulator: SimulatorInfo,
    /// Tolerance.
    #[serde(default)]
    pub tolerance: RefereeTolerance,
    /// Output directory.
    #[serde(default)]
    pub out: Option<String>,
}

/// `why`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WhySpec {
    /// Dataset.
    pub dataset: DatasetRef,
    /// Shadows.
    #[serde(default)]
    pub shadows: Option<FileRef>,
    /// Shadow id.
    #[serde(default)]
    pub shadow_id: Option<String>,
    /// Live log.
    #[serde(default)]
    pub live: Option<FileRef>,
    /// Order key.
    #[serde(default)]
    pub order_key: Option<String>,
    /// Event cap.
    #[serde(default = "max_events_default")]
    pub max_events: u32,
    /// Output file.
    #[serde(default)]
    pub out: Option<String>,
    /// 1.1: real orders removed from the tape (A1; as `ReplaySpec.exclude_order_ids`).
    #[serde(default)]
    pub exclude_order_ids: Vec<ExcludeOrderId>,
}

fn max_events_default() -> u32 {
    5000
}

/// Export target.
pub use crate::json::ExportTarget;

/// `export`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExportSpec {
    /// Target.
    pub target: ExportTarget,
    /// Calibration result.
    #[serde(default)]
    pub calibration: Option<FileRef>,
    /// Dataset.
    #[serde(default)]
    pub dataset: Option<DatasetRef>,
    /// Instrument.
    #[serde(default)]
    pub instrument_id: Option<u32>,
    /// Session date.
    #[serde(default)]
    pub session_date: Option<String>,
    /// Live log.
    #[serde(default)]
    pub live: Option<FileRef>,
    /// L2 view.
    #[serde(default)]
    pub l2: L2Config,
    /// Feed latency added to local_ts (l2_npz).
    #[serde(default)]
    pub feed_latency_ns: i64,
    /// Output file.
    #[serde(default)]
    pub out: Option<String>,
    /// 1.1: write the snippet of a refused calibration (with a warning header).
    #[serde(default)]
    pub force: bool,
}

/// Zoo family of `calibrate-live`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LiveCalibrationFamily {
    /// hbt power.
    Power,
    /// hbt power2.
    Power2,
    /// hbt power3.
    Power3,
    /// Databento MC.
    Databento,
    /// NT prob_fill_on_limit.
    NtProbTouch,
    /// NT queue position + prob touch.
    NtQueueL2ProbTouch,
}

/// 1.1 (3.11 #11): `calibrate-live` (as built in `queuetruth.score.calibrate_live`).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiveCalibrationSpec {
    /// Dataset (L2 tape or MBO).
    pub dataset: DatasetRef,
    /// Canonical live-log directory.
    pub live: FileRef,
    /// Family.
    pub family: LiveCalibrationFamily,
    /// Output directory.
    #[serde(default)]
    pub out: Option<String>,
    /// Entry rule.
    #[serde(default)]
    pub entry: EntryName,
    /// Latency.
    #[serde(default)]
    pub latency: Option<LatencyModel>,
    /// Mode.
    #[serde(default)]
    pub mode: ModeName,
    /// Exchange.
    #[serde(default)]
    pub exchange: ExchangeName,
    /// L2 view.
    #[serde(default)]
    pub l2: L2Config,
    /// Horizons.
    #[serde(default = "live_calibration_horizons")]
    pub horizons_ns: Vec<i64>,
    /// Evaluation window H.
    #[serde(default = "window_60s")]
    pub window_ns: i64,
    /// Grid (null = family default).
    #[serde(default)]
    pub grid: Option<GridRange>,
    /// Train window.
    #[serde(default)]
    pub train: WindowSpec,
    /// Test window.
    #[serde(default)]
    pub test: WindowSpec,
    /// Particles.
    #[serde(default = "particles_default")]
    pub particles: u32,
    /// Bootstrap.
    #[serde(default = "calib_bootstrap")]
    pub bootstrap: BootstrapSpec,
    /// Seed.
    #[serde(default)]
    pub seed: u64,
    /// Threads (execution-only).
    #[serde(default = "one")]
    pub threads: u32,
    /// 1.1: remove the live orders' own records (MBO) or resting size (L2 depth) first (ruling A1).
    #[serde(default = "t")]
    pub self_exclude: bool,
    /// 1.1: live-to-MBO twin matching window for self-exclusion.
    #[serde(default = "match_window_1ms")]
    pub match_window_ns: i64,
}

fn live_calibration_horizons() -> Vec<i64> {
    vec![100_000_000, 1_000_000_000, 10_000_000_000]
}
