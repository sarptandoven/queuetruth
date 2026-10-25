//! Dataset-level replay (SPEC 5.1.6, 7.2): partitions replay independently (one engine each), in parallel on
//! `threads` threads; every output is the concatenation of per-partition outputs in partition order
//! `(session_date, instrument_id)`, each in emission order, so files are byte-identical for any thread count.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;

use qt_ingest::{Dataset, Partition};
use qt_schema::codec::{read_shadow_amends, read_shadow_specs};
use qt_schema::io::{FileEntry, sha256_file};
use qt_schema::json::{self, DataQuality, DatasetKindName, FeedGap, Parity, ReplaySummary, ShadowCounts, Verdict};
use qt_schema::rows::{
    BboRow, DepthRow, L2Row, RankTraceRow, RealOrderRow, ShadowAmend, ShadowFillRow, ShadowResultRow, ShadowSpec,
    TradeRow, TrajectoryRow,
};
use qt_schema::specs::{
    BboModeName, DatasetRef, FillRuleName, HiddenPrintPolicyName, ReplaySpec, TradePositionName, TrajectoryModeName,
};
use qt_schema::{ENGINE_VERSION, run_id, spec_echo};
use qt_types::{QtError, QtResult, Venue, rules};
use serde_json::Value;

use crate::dq::merge as merge_dq;
use crate::engine::Engine;
use crate::oracle::merge as merge_oracle;
use crate::parity::{BboParity, TradeParity, merge as merge_parity, open_bbo_reference};
use crate::sinks::{Chunker, NullSink, TableSet, Tables};
use crate::util::date_string;
use crate::{
    BboMode, EngineConfig, EngineReport, EventObserver, FillRule, HiddenPrintPolicy, PlannedEnd, ShadowDiagnostics,
    Sink, Stats, TradingHalt, TrajectoryMode,
};

/// Overrides of the schema-1.1 `ReplaySpec` fields `exclude_order_ids` (A1), `fill_rule` (A4/A5 rule switch) and
/// `hidden_print_policy` (A3): a non-default field here wins over the spec (`resolve`).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ReplayOptions {
    /// (instrument_id, order_id) of real orders removed from the tape before shadows are injected, on every
    /// session date (A1). An excluded order that a shadow enters at (its twin, `Engine::set_twin_linking`, always on
    /// in `replay` and `why`) stays on the tape for everyone else and is replaced by that shadow only.
    pub exclude_order_ids: Vec<(u32, u64)>,
    /// (instrument_id, order_id, session_date as days since 1970-01-01) removed only on that session date (venue
    /// order ids can repeat across days; `ExcludeOrderId.session_date`).
    pub exclude_order_ids_dated: Vec<(u32, u64, i32)>,
    /// Shadow fill rule (default `Exact`; `Et` reproduces the earlier et prototype).
    pub fill_rule: FillRule,
    /// Hidden-print policy (`None` = the venue default).
    pub hidden_print_policy: Option<HiddenPrintPolicy>,
    /// Spans in which instruments were not in continuous trading (SPEC 1.2; `Engine::set_trading_halts`): each
    /// partition gets those of its instrument.
    pub trading_halts: Vec<TradingHalt>,
}

impl ReplayOptions {
    /// The options a run uses: the spec's `fill_rule`, `hidden_print_policy` and `exclude_order_ids`, each replaced
    /// by the override's when that is not its default (`Exact`, `None`, no exclusions). A spec exclusion with a
    /// `session_date` applies on that date only (`QT-CONFIG-001` for a malformed date).
    pub fn resolve(spec: &ReplaySpec, over: &ReplayOptions) -> QtResult<ReplayOptions> {
        let fill_rule = match (over.fill_rule, spec.fill_rule) {
            (FillRule::Et, _) | (FillRule::Exact, FillRuleName::Et) => FillRule::Et,
            (FillRule::Exact, FillRuleName::Exact) => FillRule::Exact,
        };
        let hidden_print_policy = over.hidden_print_policy.or(spec.hidden_print_policy.map(|p| match p {
            HiddenPrintPolicyName::Ignore => HiddenPrintPolicy::Ignore,
            HiddenPrintPolicyName::Behind => HiddenPrintPolicy::Behind,
            HiddenPrintPolicyName::BehindAndThrough => HiddenPrintPolicy::BehindAndThrough,
        }));
        let (mut exclude_order_ids, mut exclude_order_ids_dated) =
            if over.exclude_order_ids.is_empty() && over.exclude_order_ids_dated.is_empty() {
                let mut all = Vec::new();
                let mut dated = Vec::new();
                for x in &spec.exclude_order_ids {
                    match &x.session_date {
                        None => all.push((x.instrument_id, x.order_id)),
                        Some(d) => {
                            let day = qt_ingest::time::parse_date(d)
                                .map_err(|e| QtError::Config(format!("exclude_order_ids: session_date {d:?}: {e}")))?;
                            dated.push((x.instrument_id, x.order_id, day));
                        }
                    }
                }
                (all, dated)
            } else {
                (over.exclude_order_ids.clone(), over.exclude_order_ids_dated.clone())
            };
        // Requested once each (review m17).
        exclude_order_ids.sort_unstable();
        exclude_order_ids.dedup();
        exclude_order_ids_dated.sort_unstable();
        exclude_order_ids_dated.dedup();
        Ok(ReplayOptions {
            exclude_order_ids,
            exclude_order_ids_dated,
            fill_rule,
            hidden_print_policy,
            trading_halts: over.trading_halts.clone(),
        })
    }

    /// Number of distinct exclusion requests (all-date and dated).
    pub fn excluded_requested(&self) -> u64 {
        (self.exclude_order_ids.len() + self.exclude_order_ids_dated.len()) as u64
    }

    /// Contract rule: a non-default setting the engine would not honour is `QT-CONFIG-001`, never silently ignored.
    /// A hidden-print policy other than `ignore` needs the `exact` fill rule (the et rule never uses side-`N` prints)
    /// and a venue with hidden orders (elsewhere side-`N` prints are implied trades, not hidden executions).
    pub fn check_honoured(&self, rules: &qt_types::VenueRules) -> QtResult<()> {
        let Some(p) = self.hidden_print_policy.filter(|&p| p != HiddenPrintPolicy::Ignore) else {
            return Ok(());
        };
        if self.fill_rule == FillRule::Et {
            return Err(QtError::Config(format!(
                "hidden_print_policy {p:?} is not honoured by fill_rule et (et never fills from hidden prints); use \
                 fill_rule exact or hidden_print_policy ignore"
            )));
        }
        if !rules.supports_hidden_orders {
            return Err(QtError::Config(format!(
                "hidden_print_policy {p:?} is not honoured on venue {} (no hidden orders: side-N prints are implied \
                 trades)",
                rules.venue.name()
            )));
        }
        Ok(())
    }

    fn configure(&self, eng: &mut Engine, part: &Partition) -> QtResult<()> {
        eng.set_fill_rule(self.fill_rule);
        if let Some(p) = self.hidden_print_policy {
            eng.set_hidden_print_policy(p);
        }
        let dated = self
            .exclude_order_ids_dated
            .iter()
            .filter(|&&(i, _, d)| i == part.instrument_id && d == part.session_date)
            .map(|&(i, o, _)| (i, o));
        let all = self
            .exclude_order_ids
            .iter()
            .copied()
            .filter(|&(i, _)| i == part.instrument_id);
        let ids: Vec<(u32, u64)> = all.chain(dated).collect();
        if !ids.is_empty() {
            eng.set_excluded_orders(ids);
            eng.set_twin_linking(true);
        }
        let halts: Vec<TradingHalt> = self
            .trading_halts
            .iter()
            .copied()
            .filter(|h| h.instrument_id == part.instrument_id)
            .collect();
        eng.set_trading_halts(&halts)
    }
}

/// The local path of a dataset reference.
pub(crate) fn dataset_path(r: &DatasetRef) -> QtResult<&str> {
    r.as_path().ok_or_else(|| {
        QtError::Usage(
            "dataset must be a local {\"path\": ...} reference (dataset_id is resolved by the service)".into(),
        )
    })
}

/// Opens a dataset directory.
pub(crate) fn open_dataset(path: &str) -> QtResult<Dataset> {
    Dataset::open(Path::new(path))
}

/// Writes a JSON document (pretty, trailing newline) and returns its file entry.
pub(crate) fn write_json<T: serde::Serialize>(path: &Path, v: &T) -> QtResult<FileEntry> {
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)?;
    }
    let mut bytes = serde_json::to_vec_pretty(v).map_err(|e| QtError::Engine(format!("json: {e}")))?;
    bytes.push(b'\n');
    std::fs::write(path, &bytes)?;
    Ok(FileEntry {
        name: path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
        rows: None,
        bytes: bytes.len() as u64,
        sha256: qt_schema::io::sha256_bytes(&bytes),
    })
}

/// The ingest-time data-quality document of a dataset (`data_quality.json`), when present and readable; the zoo,
/// calib and features engines use it, with `partition_gaps`, to censor shadows on ingest-recorded feed gaps as replay
/// does.
pub fn ingest_dq(ds: &Dataset) -> Option<DataQuality> {
    ingest_dq_checked(ds).ok().flatten()
}

/// `ingest_dq`, with a message when the document exists but cannot be read or parsed (review m20: its feed gaps
/// and verdict would be lost silently).
fn ingest_dq_checked(ds: &Dataset) -> Result<Option<DataQuality>, String> {
    let p = ds.root.join("data_quality.json");
    if !p.exists() {
        return Ok(None);
    }
    let text = std::fs::read_to_string(&p).map_err(|e| format!("{}: {e}", p.display()))?;
    serde_json::from_str(&text)
        .map(Some)
        .map_err(|e| format!("{}: {e}", p.display()))
}

/// Ingest feed gaps (`ingest_dq(ds)?.feed_gaps`) of one partition: those of its instrument overlapping
/// `[ts_min, ts_max]`.
pub fn partition_gaps(gaps: &[FeedGap], part: &Partition) -> Vec<FeedGap> {
    gaps.iter()
        .filter(|g| g.instrument_id == part.instrument_id && g.ts_end >= part.ts_min && g.ts_start <= part.ts_max)
        .cloned()
        .collect()
}

/// `date=YYYY-MM-DD/instrument_id=N` of a partition.
pub(crate) fn partition_label(part: &Partition) -> String {
    format!(
        "date={}/instrument_id={}",
        date_string(part.session_date),
        part.instrument_id
    )
}

/// The engine of one partition, exactly as `replay` configures it (shared by `why`, review M3): the partition's
/// instrument row, record-level sequence-gap detection off (a partition holds one instrument of a feed whose
/// sequence numbers span instruments), the ingest feed gaps of the partition (they censor live shadows), and the
/// resolved options (fill rule, hidden-print policy, exclusions).
pub(crate) fn partition_engine(
    part: &Partition,
    ds: &Dataset,
    cfg: &EngineConfig,
    shadows: Vec<ShadowSpec>,
    amends: Vec<ShadowAmend>,
    gaps: &[FeedGap],
    opts: &ReplayOptions,
) -> QtResult<Engine> {
    let inst: Vec<_> = ds
        .instrument(part.instrument_id, part.session_date)
        .cloned()
        .into_iter()
        .collect();
    let mut eng = Engine::new(cfg.clone(), &inst, shadows, amends)?;
    eng.set_sequence_gap_detection(false);
    opts.configure(&mut eng, part)?;
    eng.set_feed_gaps(&partition_gaps(gaps, part));
    if cfg.rules.supports_hidden_orders {
        // OC-28: side-N prints the source marks as crosses are not hidden prints.
        if let Some(trades) = part.source_trades()? {
            eng.set_cross_prints(
                trades
                    .iter()
                    .filter(|t| t.kind == qt_schema::rows::TradeKind::Cross)
                    .map(|t| (t.instrument_id, t.sequence)),
            );
        }
    }
    Ok(eng)
}

#[allow(clippy::too_many_arguments)]
fn replay_part(
    part: &Partition,
    ds: &Dataset,
    cfg: &EngineConfig,
    shadows: Vec<ShadowSpec>,
    amends: Vec<ShadowAmend>,
    observers: Vec<Box<dyn EventObserver>>,
    gaps: &[FeedGap],
    opts: &ReplayOptions,
    sink: &mut dyn Sink,
    mut l2: Option<&mut L2Out>,
) -> QtResult<(EngineReport, Vec<PlannedEnd>)> {
    let mut eng = partition_engine(part, ds, cfg, shadows, amends, gaps, opts)?;
    let planned = eng.shadow_planned_ends();
    for o in observers {
        eng.add_observer(o);
    }
    for e in part.mbo()? {
        let e = e?;
        if let Some(l) = l2.as_deref_mut() {
            l.push(&e)?;
        }
        eng.apply(&e, sink)?;
    }
    let mut rep = eng.finish(sink)?;
    rep.stats.partitions = 1;
    Ok((rep, planned))
}

/// Replays one partition (sequence gaps come from the dataset's ingest-time feed gaps).
pub fn run_partition(
    part: &Partition,
    ds: &Dataset,
    cfg: &EngineConfig,
    shadows: Vec<ShadowSpec>,
    amends: Vec<ShadowAmend>,
    observers: Vec<Box<dyn EventObserver>>,
    sink: &mut dyn Sink,
) -> QtResult<EngineReport> {
    let gaps = ingest_dq(ds).map(|d| d.feed_gaps).unwrap_or_default();
    replay_part(
        part,
        ds,
        cfg,
        shadows,
        amends,
        observers,
        &gaps,
        &ReplayOptions::default(),
        sink,
        None,
    )
    .map(|r| r.0)
}

/// Runs `f` over partitions on `threads` threads; results in partition order (the first error in that order wins).
pub fn for_each_partition<T: Send>(
    parts: &[Partition],
    threads: usize,
    f: impl Fn(&Partition) -> QtResult<T> + Sync,
) -> QtResult<Vec<T>> {
    let threads = threads.max(1).min(parts.len().max(1));
    if threads == 1 {
        return parts.iter().map(&f).collect();
    }
    let next = AtomicUsize::new(0);
    let slots: Mutex<Vec<Option<QtResult<T>>>> = Mutex::new((0..parts.len()).map(|_| None).collect());
    std::thread::scope(|s| {
        for _ in 0..threads {
            s.spawn(|| {
                loop {
                    let i = next.fetch_add(1, Ordering::SeqCst);
                    let Some(p) = parts.get(i) else {
                        break;
                    };
                    let r = f(p);
                    if let Ok(mut v) = slots.lock() {
                        v[i] = Some(r);
                    }
                }
            });
        }
    });
    let slots = slots
        .into_inner()
        .map_err(|_| QtError::Engine("partition worker panicked".into()))?;
    slots
        .into_iter()
        .map(|r| r.unwrap_or_else(|| Err(QtError::Engine("partition worker panicked".into()))))
        .collect()
}

/// Shadows routed to partitions by `partition_for(instrument_id, ts_submit)`, plus those without a partition.
/// Shadows by (session_date, instrument_id) partition.
type ByPartition = BTreeMap<(i32, u32), Vec<ShadowSpec>>;

fn split(ds: &Dataset, shadows: Vec<ShadowSpec>) -> (ByPartition, Vec<ShadowSpec>) {
    let mut map: BTreeMap<(i32, u32), Vec<ShadowSpec>> = BTreeMap::new();
    let mut none = Vec::new();
    for s in shadows {
        match ds.partition_for(s.instrument_id, s.ts_submit) {
            Some(p) => map.entry((p.session_date, p.instrument_id)).or_default().push(s),
            None => none.push(s),
        }
    }
    (map, none)
}

/// Splits shadows by (session_date, instrument_id) partition; shadows without a partition are left out
/// (`run_dataset` ends them `censored_gap`).
pub fn split_shadows(ds: &Dataset, shadows: Vec<ShadowSpec>) -> QtResult<BTreeMap<(i32, u32), Vec<ShadowSpec>>> {
    Ok(split(ds, shadows).0)
}

/// Gate failures of `--strict`: non-zero invariants, a data-quality verdict other than clean, failed parity.
pub fn strict_violations(s: &ReplaySummary) -> Vec<String> {
    let mut v = Vec::new();
    let i = &s.invariants;
    for (k, n) in [
        ("cancels_exceeding_resting", i.cancels_exceeding_resting),
        ("fills_exceeding_resting", i.fills_exceeding_resting),
        ("fills_unknown_order", i.fills_unknown_order),
        ("unknown_order_refs", i.unknown_order_refs),
        ("duplicate_adds", i.duplicate_adds),
        ("unexpected_modifies", i.unexpected_modifies),
        ("crossed_books_after_last", i.crossed_books_after_last),
        ("negative_spreads", i.negative_spreads),
    ] {
        if n > 0 {
            v.push(format!("{k}={n}"));
        }
    }
    if s.data_quality.verdict != Verdict::Clean {
        v.extend(s.data_quality.reasons.iter().cloned());
    }
    if s.parity.as_ref().is_some_and(|p| !p.pass) {
        v.push("parity failed".into());
    }
    v
}

/// L2 rows of one partition written while replaying.
struct L2Out {
    builder: qt_l2::L2Builder,
    rows: Vec<L2Row>,
    table: Chunker<L2Row>,
    last_ts: i64,
}

impl L2Out {
    fn push(&mut self, e: &qt_types::MboEvent) -> QtResult<()> {
        self.last_ts = e.ts_event;
        self.builder.push(e, &mut self.rows);
        self.drain()
    }

    fn drain(&mut self) -> QtResult<()> {
        for r in self.rows.drain(..) {
            self.table.push(r)?;
        }
        Ok(())
    }

    fn finish(mut self) -> QtResult<FileEntry> {
        self.builder.flush(self.last_ts, &mut self.rows);
        self.drain()?;
        self.table.finish()
    }
}

/// Per-partition sink: parity checks, the user's BBO mode (the engine runs `full` when BBO parity is on).
struct PartSink<'a> {
    out: &'a mut dyn Sink,
    trades: Option<TradeParity>,
    bbo: Option<BboParity>,
    mode: BboMode,
    last: Option<BboRow>,
}

impl Sink for PartSink<'_> {
    fn bbo(&mut self, r: &BboRow) -> QtResult<()> {
        if let Some(p) = self.bbo.as_mut() {
            p.derived(r)?;
        }
        let key = |b: &BboRow| (b.bid_px, b.bid_sz, b.bid_ct, b.ask_px, b.ask_sz, b.ask_ct);
        let emit = match self.mode {
            BboMode::None => false,
            BboMode::Full => true,
            BboMode::Change => self.last.as_ref().is_none_or(|l| key(l) != key(r)),
        };
        self.last = Some(*r);
        if emit { self.out.bbo(r) } else { Ok(()) }
    }
    fn trade(&mut self, r: &TradeRow) -> QtResult<()> {
        if let Some(p) = self.trades.as_mut() {
            p.derived(r);
        }
        self.out.trade(r)
    }
    fn depth(&mut self, r: &DepthRow) -> QtResult<()> {
        self.out.depth(r)
    }
    fn rank_trace(&mut self, r: &RankTraceRow) -> QtResult<()> {
        self.out.rank_trace(r)
    }
    fn shadow_fill(&mut self, r: &ShadowFillRow) -> QtResult<()> {
        self.out.shadow_fill(r)
    }
    fn shadow_fill_liquidity(&mut self, r: &ShadowFillRow, liquidity: qt_schema::rows::Liquidity) -> QtResult<()> {
        self.out.shadow_fill_liquidity(r, liquidity)
    }
    fn trajectory(&mut self, r: &TrajectoryRow) -> QtResult<()> {
        self.out.trajectory(r)
    }
    fn shadow_result_planned(
        &mut self,
        r: &ShadowResultRow,
        planned: qt_schema::codec::PlannedEndCols,
    ) -> QtResult<()> {
        self.out.shadow_result_planned(r, planned)
    }
    fn shadow_result(&mut self, r: &ShadowResultRow) -> QtResult<()> {
        self.out.shadow_result(r)
    }
    fn real_order(&mut self, r: &RealOrderRow) -> QtResult<()> {
        self.out.real_order(r)
    }
}

/// What one partition contributes to the summary.
struct PartOut {
    label: String,
    instrument_id: u32,
    report: EngineReport,
    parity: Option<Parity>,
    l2: Option<FileEntry>,
    warnings: Vec<String>,
    planned: Vec<PlannedEnd>,
}

/// Resolves a spec reference to its identity sha256 (SPEC 7.3).
pub(crate) fn resolve_ref(v: &Value) -> QtResult<String> {
    let Value::Object(o) = v else {
        return Err(QtError::Engine("run_id: reference is not an object".into()));
    };
    if let Some(Value::String(id)) = o.get("dataset_id") {
        return Ok(id.clone());
    }
    let Some(Value::String(p)) = o.get("path") else {
        return Err(QtError::Usage(
            "run_id: upload and job-file references are resolved by the service".into(),
        ));
    };
    let path = Path::new(p);
    if path.is_dir() {
        let m = path.join("manifest.json");
        let text = std::fs::read_to_string(&m).map_err(|e| QtError::InputMissing {
            path: m.display().to_string(),
            msg: e.to_string(),
        })?;
        if let Ok(Value::Object(doc)) = serde_json::from_str::<Value>(&text) {
            if doc.get("schema").and_then(Value::as_str) == Some("qt.dataset_manifest") {
                if let Some(Value::String(id)) = doc.get("dataset_id") {
                    return Ok(id.clone());
                }
            }
        }
        return sha256_file(&m);
    }
    sha256_file(path)
}

/// Venue rules of a dataset: its manifest's `rule_set` when set (e.g. `crypto_l3` for Bitstamp / Bitfinex /
/// Coinbase datasets written by qt_crypto), else its venue's.
pub(crate) fn dataset_rules(ds: &Dataset) -> qt_types::VenueRules {
    qt_types::rules_for(ds.manifest.effective_rule_set())
}

fn engine_config(spec: &ReplaySpec, ds: &Dataset) -> QtResult<EngineConfig> {
    let r = match &spec.venue {
        Some(v) => rules(v.parse::<Venue>()?),
        None => dataset_rules(ds),
    };
    let mut cfg = EngineConfig::new(r);
    cfg.latency = spec.latency;
    cfg.seed = spec.seed;
    cfg.rank_oracle = spec.rank_oracle;
    cfg.sweep_gap_ns = spec.sweep_gap_ns;
    cfg.bbo = match spec.bbo {
        BboModeName::None => BboMode::None,
        BboModeName::Change => BboMode::Change,
        BboModeName::Full => BboMode::Full,
    };
    cfg.depth_levels = spec.depth_levels;
    cfg.trajectory = match spec.trajectory {
        TrajectoryModeName::None => TrajectoryMode::None,
        TrajectoryModeName::Ahead => TrajectoryMode::Ahead,
        TrajectoryModeName::Full => TrajectoryMode::Full,
    };
    cfg.race_window_ns = spec.race_window_ns;
    cfg.real_orders = spec.real_orders;
    cfg.strict = spec.strict;
    Ok(cfg)
}

fn l2_config(c: &qt_schema::specs::L2Config) -> qt_l2::L2Config {
    qt_l2::L2Config {
        depth_levels: c.depth_levels,
        cadence_ns: c.cadence_ns,
        trade_position: match c.trade_position {
            TradePositionName::Before => qt_l2::TradePosition::Before,
            TradePositionName::After => qt_l2::TradePosition::After,
        },
        include_counts: c.include_counts,
        emit_hidden_trades: c.emit_hidden_trades,
    }
}

fn file_path(r: &Option<qt_schema::specs::FileRef>, what: &str) -> QtResult<Option<PathBuf>> {
    match r {
        None => Ok(None),
        Some(f) => f
            .as_path()
            .map(|p| Some(PathBuf::from(p)))
            .ok_or_else(|| QtError::Usage(format!("{what} must be a local {{\"path\": ...}} reference"))),
    }
}

/// `replay`: writes the SPEC 5.1.6 output directory (default `ReplayOptions`).
pub fn run_dataset(spec: &ReplaySpec) -> QtResult<json::ReplaySummary> {
    run_dataset_with(spec, &ReplayOptions::default())
}

/// `replay` with overrides of the spec's fill rule, hidden-print policy and exclusions (`ReplayOptions::resolve`;
/// a combination the engine would not honour is `QT-CONFIG-001`, `ReplayOptions::check_honoured`). With shadows it
/// also writes `shadow_planned_end.parquet` (`qt.x_shadow_planned_end`: shadow_id, planned_end, kind; A2), reports
/// `shadow_diagnostics` in the summary, and the diagnostics and exclusion match counts in `warnings`.
pub fn run_dataset_with(spec: &ReplaySpec, over: &ReplayOptions) -> QtResult<json::ReplaySummary> {
    let started = Instant::now();
    let ds = open_dataset(dataset_path(&spec.dataset)?)?;
    if ds.manifest.kind == DatasetKindName::L2 {
        return Err(QtError::Config(
            "dataset: exact replay needs MBO (this dataset is L2-only)".into(),
        ));
    }
    let cfg = engine_config(spec, &ds)?;
    let resolved = ReplayOptions::resolve(spec, over)?;
    resolved.check_honoured(&cfg.rules)?;
    let opts = &resolved;
    let shadows = match file_path(&spec.shadows, "shadows")? {
        Some(p) => read_shadow_specs(&p)?,
        None => Vec::new(),
    };
    let amends = match file_path(&spec.amends, "amends")? {
        Some(p) => read_shadow_amends(&p)?,
        None => Vec::new(),
    };
    let have_shadows = spec.shadows.is_some();
    let bbo_ref = file_path(&spec.parity_bbo, "parity_bbo")?;
    let mut run_cfg = cfg.clone();
    if bbo_ref.is_some() {
        run_cfg.bbo = BboMode::Full;
    }
    let mut warnings_pre = Vec::new();
    let ingest = match ingest_dq_checked(&ds) {
        Ok(d) => d,
        Err(m) => {
            warnings_pre.push(format!(
                "data_quality.json unreadable, its feed gaps and verdict are not applied: {m}"
            ));
            None
        }
    };
    let gaps = ingest.as_ref().map(|d| d.feed_gaps.clone()).unwrap_or_default();
    let (mut by_part, unmatched) = split(&ds, shadows);
    let mut amends_by_id: BTreeMap<String, Vec<ShadowAmend>> = BTreeMap::new();
    for a in amends {
        amends_by_id.entry(a.shadow_id.clone()).or_default().push(a);
    }
    let known: BTreeSet<&str> = by_part
        .values()
        .flatten()
        .chain(&unmatched)
        .map(|s| s.shadow_id.as_str())
        .collect();
    if let Some(id) = amends_by_id.keys().find(|k| !known.contains(k.as_str())) {
        return Err(QtError::InputFormat {
            location: format!("amend for shadow {id}"),
            msg: "amend references an unknown shadow_id".into(),
        });
    }
    let take_amends = |sh: &[ShadowSpec]| -> Vec<ShadowAmend> {
        sh.iter()
            .flat_map(|s| amends_by_id.get(&s.shadow_id).cloned().unwrap_or_default())
            .collect()
    };
    let parts = ds.partitions().to_vec();
    let part_shadows: Vec<Vec<ShadowSpec>> = parts
        .iter()
        .map(|p| by_part.remove(&(p.session_date, p.instrument_id)).unwrap_or_default())
        .collect();
    let out_root = spec.out.as_ref().map(PathBuf::from);
    if let Some(r) = &out_root {
        std::fs::create_dir_all(r)?;
    }
    let set = TableSet {
        bbo: cfg.bbo != BboMode::None,
        depth: cfg.depth_levels > 0,
        rank_trace: cfg.rank_oracle,
        shadows: have_shadows,
        real_orders: cfg.real_orders,
    };

    let run_one = |i: usize, sink: &mut dyn Sink| -> QtResult<PartOut> {
        let part = &parts[i];
        let label = partition_label(part);
        let mut warnings = Vec::new();
        let sh = part_shadows[i].clone();
        let am = take_amends(&sh);
        let trades = if spec.parity_trades {
            match part.source_trades()? {
                Some(t) => Some(TradeParity::new(part.instrument_id, t)),
                None => {
                    warnings.push(format!("{label}: parity_trades: the dataset has no source trades"));
                    None
                }
            }
        } else {
            None
        };
        let bbo = match &bbo_ref {
            Some(p) => {
                let (it, counts) = open_bbo_reference(p, part.instrument_id, (part.ts_min, part.ts_max))?;
                Some(BboParity::new(part.instrument_id, it, counts))
            }
            None => None,
        };
        let parity_on = trades.is_some() || bbo.is_some();
        let mut l2 = match (&spec.l2, &out_root) {
            (Some(c), Some(root)) => {
                let name = format!("l2/{label}/part-00000.parquet");
                let rules = run_cfg.rules;
                Some(L2Out {
                    builder: qt_l2::L2Builder::new(l2_config(c), rules, part.instrument_id),
                    rows: Vec::new(),
                    table: Chunker::create(root, &name)?,
                    last_ts: part.ts_min,
                })
            }
            _ => None,
        };
        let mut ps = PartSink {
            out: sink,
            trades,
            bbo,
            mode: cfg.bbo,
            last: None,
        };
        let (report, planned) = replay_part(
            part,
            &ds,
            &run_cfg,
            sh,
            am,
            Vec::new(),
            &gaps,
            opts,
            &mut ps,
            l2.as_mut(),
        )?;
        let mut pp = Vec::new();
        if let Some(t) = ps.trades.take() {
            pp.push(t.finish());
        }
        if let Some(b) = ps.bbo.take() {
            pp.push(b.finish()?);
        }
        let l2 = l2.map(L2Out::finish).transpose()?;
        Ok(PartOut {
            label,
            instrument_id: part.instrument_id,
            report,
            parity: parity_on.then(|| merge_parity(&pp)),
            l2,
            warnings,
            planned,
        })
    };

    let threads = (spec.threads as usize).max(1);
    let mut tables = match &out_root {
        Some(r) => Some(Tables::create(r, set)?),
        None => None,
    };
    let mut outs = Vec::with_capacity(parts.len());
    if threads == 1 || parts.len() <= 1 {
        for i in 0..parts.len() {
            let o = match tables.as_mut() {
                Some(t) => run_one(i, t)?,
                None => run_one(i, &mut NullSink)?,
            };
            outs.push(o);
        }
    } else {
        let tmp = out_root.as_ref().map(|r| r.join(".tmp-replay"));
        let index = |p: &Partition| parts.iter().position(|q| std::ptr::eq(p, q)).unwrap_or(0);
        let res = for_each_partition(&parts, threads, |p| {
            let i = index(p);
            match &tmp {
                Some(t) => {
                    let dir = t.join(format!("part-{i:06}"));
                    let mut tt = Tables::create(&dir, set)?;
                    let o = run_one(i, &mut tt)?;
                    tt.finish()?;
                    Ok((o, Some(dir)))
                }
                None => Ok((run_one(i, &mut NullSink)?, None)),
            }
        })?;
        for (o, dir) in res {
            if let (Some(t), Some(d)) = (tables.as_mut(), dir) {
                t.append(&d)?;
                std::fs::remove_dir_all(&d)?;
            }
            outs.push(o);
        }
        if let Some(t) = &tmp {
            if t.exists() {
                std::fs::remove_dir_all(t)?;
            }
        }
    }

    // Shadows without a partition: censored_gap, appended after the partitions.
    let mut extra_counts = None;
    let mut extra_planned = Vec::new();
    let mut warnings: Vec<String> = std::mem::take(&mut warnings_pre);
    warnings.extend(outs.iter().flat_map(|o| o.warnings.clone()));
    if !unmatched.is_empty() {
        warnings.push(format!(
            "{} shadow(s) have no partition for their instrument and date (censored_gap); first: {}",
            unmatched.len(),
            unmatched[0].shadow_id
        ));
        let am = take_amends(&unmatched);
        let mut eng = Engine::new(cfg.clone(), &ds.instruments, unmatched, am)?;
        extra_planned = eng.shadow_planned_ends();
        let rep = match tables.as_mut() {
            Some(t) => {
                eng.censor_unreplayed(t)?;
                eng.finish(t)?
            }
            None => {
                eng.censor_unreplayed(&mut NullSink)?;
                eng.finish(&mut NullSink)?
            }
        };
        extra_counts = rep.shadows;
    }

    // Merge.
    let mut stats = Stats::default();
    let mut inv = qt_book::Invariants::default();
    let mut oracles = Vec::new();
    let mut dqs = Vec::new();
    let mut parities = Vec::new();
    let mut counts: Option<ShadowCounts> = if have_shadows {
        Some(ShadowCounts::default())
    } else {
        None
    };
    let mut instruments = BTreeSet::new();
    let mut l2_files = Vec::new();
    let mut diag = ShadowDiagnostics::default();
    let mut planned: Vec<PlannedEnd> = Vec::new();
    for o in &outs {
        diag.add(&o.report.diagnostics);
        planned.extend(o.planned.iter().cloned());
        stats.add(&o.report.stats);
        inv.add(&o.report.invariants);
        instruments.insert(o.instrument_id);
        if let Some(r) = &o.report.rank_oracle {
            oracles.push(r.clone());
        }
        dqs.push((o.label.clone(), o.report.data_quality.clone()));
        if let Some(p) = &o.parity {
            parities.push(p.clone());
        }
        if let (Some(c), Some(s)) = (counts.as_mut(), &o.report.shadows) {
            add_counts(c, s);
        }
        if let Some(f) = &o.l2 {
            l2_files.push(f.clone());
        }
    }
    if let (Some(c), Some(s)) = (counts.as_mut(), &extra_counts) {
        add_counts(c, s);
    }
    stats.instruments = instruments.len() as u64;
    let mut dq = merge_dq(&dqs);
    // A gap overlapping several partitions is reported once (review m20).
    dq.feed_gaps.sort_by(|a, b| {
        (a.instrument_id, a.ts_start, a.ts_end, &a.reason).cmp(&(b.instrument_id, b.ts_start, b.ts_end, &b.reason))
    });
    dq.feed_gaps.dedup();
    if let Some(ing) = &ingest {
        let mut extra = Vec::new();
        if ing.pre_snapshot_rows_skipped > 0 {
            dq.pre_snapshot_rows_skipped += ing.pre_snapshot_rows_skipped;
            extra.push(format!(
                "ingest: pre_snapshot_rows_skipped = {}",
                ing.pre_snapshot_rows_skipped
            ));
        }
        if ing.sequence_gaps > 0 {
            dq.sequence_gaps += ing.sequence_gaps;
            extra.push(format!("ingest: sequence_gaps = {}", ing.sequence_gaps));
        }
        for (k, v) in &ing.source_specific {
            *dq.source_specific.entry(k.clone()).or_insert(0) += v;
            if *v > 0 && !qt_ingest::dq::is_info_source_counter(k) {
                extra.push(format!("ingest: {k} = {v}"));
            }
        }
        if ing.verdict > dq.verdict {
            extra.push(format!("ingest verdict: {:?}", ing.verdict).to_lowercase());
            dq.verdict = ing.verdict;
        }
        if !extra.is_empty() {
            dq.verdict = dq.verdict.max(Verdict::Warnings);
            dq.reasons.extend(extra);
        }
    }
    let parity = if spec.parity_trades || bbo_ref.is_some() {
        Some(merge_parity(&parities))
    } else {
        None
    };
    let mut files = match tables {
        Some(t) => t.finish()?,
        None => Vec::new(),
    };
    planned.extend(extra_planned);
    if let (Some(r), true) = (&out_root, have_shadows) {
        files.push(write_planned_ends(r, &planned)?);
    }
    l2_files.sort_by(|a, b| a.name.cmp(&b.name));
    files.extend(l2_files);
    if have_shadows {
        warnings.push(format!(
            "shadow diagnostics (fill rule {:?}): hidden_print_fills={} hidden_at_px_with_display_ahead={} \
             taker_fills={} crossed_unfilled={} behind_exec_with_ahead={}",
            opts.fill_rule,
            diag.hidden_print_fills,
            diag.hidden_at_px_with_display_ahead,
            diag.taker_fills,
            diag.crossed_unfilled,
            diag.behind_exec_with_ahead
        ));
    }
    if opts.excluded_requested() > 0 {
        let requested = opts.excluded_requested();
        warnings.push(format!(
            "exclude_order_ids: requested={requested} matched={} unmatched={} records_removed={} \
             replaced_by_own_shadow={} twin_fills_with_queue_ahead={} twin_fills_off_display={}",
            diag.excluded_matched,
            requested.saturating_sub(diag.excluded_matched),
            diag.excluded_records,
            diag.twins_linked,
            diag.twin_fills_with_ahead,
            diag.twin_fills_off_display
        ));
    }
    if let Some(r) = &out_root {
        files.push(write_json(&r.join("data_quality.json"), &dq)?);
    }
    let spec_value = serde_json::to_value(spec).map_err(|e| QtError::Engine(format!("spec: {e}")))?;
    let echo = match spec_echo(&spec_value) {
        Value::Object(o) => o,
        _ => serde_json::Map::new(),
    };
    let wall = started.elapsed().as_secs_f64();
    let mut summary = ReplaySummary {
        run_id: run_id(&spec_value, ENGINE_VERSION, &resolve_ref)?,
        engine_version: ENGINE_VERSION.into(),
        spec: echo,
        stats: json::ReplayStats {
            records: stats.records,
            events: stats.events,
            trades: stats.trades,
            instruments: stats.instruments,
            partitions: stats.partitions,
            snapshot_records: stats.snapshot_records,
        },
        invariants: json::Invariants {
            cancels_exceeding_resting: inv.cancels_exceeding_resting,
            fills_exceeding_resting: inv.fills_exceeding_resting,
            fills_unknown_order: inv.fills_unknown_order,
            unknown_order_refs: inv.unknown_order_refs,
            duplicate_adds: inv.duplicate_adds,
            unexpected_modifies: inv.unexpected_modifies,
            crossed_books_after_last: inv.crossed_books_after_last,
            negative_spreads: inv.negative_spreads,
        },
        rank_oracle: if cfg.rank_oracle {
            Some(merge_oracle(&oracles).unwrap_or_else(|| json::RankOracle {
                sweep_gap_ns: cfg.sweep_gap_ns,
                ..json::RankOracle::default()
            }))
        } else {
            None
        },
        parity,
        shadows: counts,
        data_quality: dq,
        files,
        warnings,
        timings: Some(json::ReplayTimings {
            wall_s: wall,
            records_per_s: (wall > 0.0).then(|| stats.records as f64 / wall),
        }),
        shadow_diagnostics: have_shadows.then_some(json::ShadowDiagnostics {
            hidden_print_fills: diag.hidden_print_fills,
            hidden_at_px_with_display_ahead: diag.hidden_at_px_with_display_ahead,
            taker_fills: diag.taker_fills,
            crossed_unfilled: diag.crossed_unfilled,
            behind_exec_with_ahead: diag.behind_exec_with_ahead,
            excluded_requested: opts.excluded_requested(),
            excluded_matched: diag.excluded_matched,
            excluded_records: diag.excluded_records,
            twins_linked: diag.twins_linked,
            twin_fills_with_queue_ahead: diag.twin_fills_with_ahead,
            twin_fills_off_display: diag.twin_fills_off_display,
        }),
        ..ReplaySummary::default()
    };
    if spec.strict {
        let v = strict_violations(&summary);
        summary.warnings.extend(v.iter().map(|m| format!("strict: {m}")));
    }
    if let Some(r) = &out_root {
        write_json(&r.join("summary.json"), &summary)?;
    }
    if spec.strict && summary.data_quality.verdict == Verdict::Unusable {
        return Err(QtError::DataUnusable(summary.data_quality.reasons.join("; ")));
    }
    Ok(summary)
}

/// `shadow_planned_end.parquet` (A2), one row per shadow in `shadow_results` order.
fn write_planned_ends(root: &Path, rows: &[PlannedEnd]) -> QtResult<FileEntry> {
    use std::sync::Arc;

    use arrow_array::{ArrayRef, RecordBatch, StringArray, TimestampNanosecondArray};
    use arrow_schema::{DataType, Field, Schema};

    let schema = Arc::new(Schema::new_with_metadata(
        vec![
            Field::new("shadow_id", DataType::Utf8, false),
            Field::new("planned_end", qt_schema::ts_type(), true),
            Field::new("kind", DataType::Utf8, false),
        ],
        qt_schema::table_metadata("qt.x_shadow_planned_end"),
    ));
    let name = "shadow_planned_end.parquet";
    let mut w = qt_schema::io::TableWriter::create_with_schema(&root.join(name), schema.clone())?;
    for chunk in rows
        .chunks(crate::sinks::CHUNK_ROWS)
        .chain(rows.is_empty().then_some(&rows[..0]))
    {
        let cols: Vec<ArrayRef> = vec![
            Arc::new(StringArray::from_iter_values(
                chunk.iter().map(|r| r.shadow_id.as_str()),
            )),
            Arc::new(TimestampNanosecondArray::from_iter(chunk.iter().map(|r| r.planned_end)).with_timezone("UTC")),
            Arc::new(StringArray::from_iter_values(chunk.iter().map(|r| r.kind))),
        ];
        let b = RecordBatch::try_new(schema.clone(), cols).map_err(|e| QtError::Engine(format!("{name}: {e}")))?;
        w.write(&b)?;
    }
    let mut f = w.finish()?;
    f.name = name.into();
    Ok(f)
}

fn add_counts(c: &mut ShadowCounts, s: &ShadowCounts) {
    c.n += s.n;
    c.n_live += s.n_live;
    c.n_filled += s.n_filled;
    c.n_partial_only += s.n_partial_only;
    c.n_cancelled += s.n_cancelled;
    c.n_censored += s.n_censored;
    c.n_rejected += s.n_rejected;
    c.fills_rows += s.fills_rows;
    c.trajectory_rows += s.trajectory_rows;
}
