//! `run_zoo` (SPEC 5.3.2) and the particle simulator for the C2 survival calibration (SPEC 5.4). Per partition, one
//! pass feeds every MBO record to the exact `qt_replay::Engine` (shadow truth, observer callbacks for the MBO arm)
//! and to `qt_l2::L2Builder`; every emitted L2 batch drives the L2 arm (`sim::PartitionSim`). L2-only datasets run
//! the L2 arm on the ingested rows without an exact arm (truth columns null).
//!
//! Output directory: `zoo_predictions.parquet`, `zoo_fills.parquet` (`write_fills`), `zoo_trajectory.parquet`
//! (`write_trajectory`), `zoo_shadows.parquet` (per-shadow planned end and administrative censoring for the
//! outcome-independent truth labels of audit ruling A2; schema `qt.zoo_shadows`, not yet in SPEC 3.4) and
//! `summary.json` (`qt.zoo_summary`, empty leaderboard).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use arrow_array::builder::{BooleanBuilder, Date32Builder, StringBuilder, TimestampNanosecondBuilder, UInt32Builder};
use arrow_array::{ArrayRef, RecordBatch};
use qt_book::{Book, Effect};
use qt_ingest::{Dataset, DatasetKind, Partition};
use qt_replay::{BboMode, Engine, EngineConfig, EventEnd, EventObserver, Sink, TrajectoryMode};
use qt_schema::io::{FileEntry, TableWriter};
use qt_schema::json::FeedGap;
use qt_schema::rows::{
    EndState, L2Row, ShadowAmend, ShadowFillRow, ShadowResultRow, ShadowSpec, TrajectoryKind, TrajectoryRow,
    ZooPredictionRow,
};
use qt_schema::specs::{CalibrationSpec, ExchangeName, ExcludeOrderId, ModeName, WindowSpec, ZooSpec};
use qt_schema::{codec, json};
use qt_types::{
    BookSide, Dur, Instrument, LatencyModel, MboEvent, Px, QtError, QtResult, Qty, Ts, UNDEF_PRICE, VenueRules,
    Xoshiro256StarStar, stable_seed,
};

use crate::model_id::expand_models;
use crate::pair::{ModelSlot, Op, Pair};
use crate::sim::{PartitionOut, PartitionSim, ShadowInput, SimConfig, Truth, split_batches};
use crate::{Exchange, Family, Mode, ModelId, ParticleSimulator, ShadowCurve};

/// `(ts_live, cancel effective time)` of a shadow under SPEC 3.1 latency semantics (no amends).
pub fn lifecycle(s: &ShadowSpec, lat: &LatencyModel, seed: u64) -> QtResult<(Ts, Option<Ts>)> {
    lifecycle_with_amends(s, &[], lat, seed).map(|(live, cancel, _)| (live, cancel))
}

/// `(ts_live, cancel effective time, [(amend effective time, new total qty)])` of a shadow.
pub type Lifecycle = (Ts, Option<Ts>, Vec<(Ts, Qty)>);

/// Lifecycle of a shadow with its amends (schema 1.1 `ZooSpec.amends`), as the exact engine computes it: `ts_live`,
/// the cancel effective time and `(effective time, new total qty)` per amend in row order (amend delay =
/// `latency_ns + gateway + J_amend`; jitter stream `stable_seed(seed, ["latency", shadow_id])`, draws J_submit,
/// J_cancel, then one per amend). Amends are validated as by the engine (`ts_send >= ts_submit`, strictly
/// decreasing quantities: priority-keeping reductions only) and must be in time order (review m11), else
/// `QT-INPUT-002`.
pub fn lifecycle_with_amends(
    s: &ShadowSpec,
    amends: &[ShadowAmend],
    lat: &LatencyModel,
    seed: u64,
) -> QtResult<Lifecycle> {
    if s.latency_ns < 0 || lat.feed_ns < 0 || lat.decision_ns < 0 || lat.gateway_ns < 0 || lat.jitter_ns < 0 {
        return Err(QtError::Config(format!("shadow {}: negative latency", s.shadow_id)));
    }
    let bad = |msg: String| QtError::InputFormat {
        location: format!("shadow {}", s.shadow_id),
        msg,
    };
    let mut prev_qty = s.qty;
    let mut prev_send = s.ts_submit;
    for a in amends {
        if a.ts_send < s.ts_submit {
            return Err(bad(format!("amend ts_send {} < ts_submit {}", a.ts_send, s.ts_submit)));
        }
        // rows are applied in order; an earlier-sent amend after a later one is ambiguous (review m11)
        if a.ts_send < prev_send {
            return Err(bad(format!(
                "amend ts_send {} is before the previous amend's {prev_send} (amends must be in time order)",
                a.ts_send
            )));
        }
        prev_send = a.ts_send;
        if a.new_qty >= prev_qty {
            return Err(bad(format!(
                "amend new_qty {} must be < the current qty {prev_qty} (priority-keeping reductions only)",
                a.new_qty
            )));
        }
        prev_qty = a.new_qty;
    }
    let mut rng =
        (lat.jitter_ns > 0).then(|| Xoshiro256StarStar::new(stable_seed(seed, &[b"latency", s.shadow_id.as_bytes()])));
    let mut jitter = || -> i64 {
        rng.as_mut()
            .map_or(0, |r| r.range_inclusive(0, lat.jitter_ns as u64) as i64)
    };
    let (js, jc) = (jitter(), jitter());
    let ovf = || QtError::Config(format!("shadow {}: latency overflow", s.shadow_id));
    let submit = [lat.feed_ns, lat.decision_ns, lat.gateway_ns, js]
        .iter()
        .try_fold(s.latency_ns, |a, b| a.checked_add(*b))
        .ok_or_else(ovf)?;
    let ts_live = s.ts_submit.checked_add(submit).ok_or_else(ovf)?;
    let cancel = match s.ts_cancel {
        Some(c) => {
            let d = [lat.gateway_ns, jc]
                .iter()
                .try_fold(s.latency_ns, |a, b| a.checked_add(*b))
                .ok_or_else(ovf)?;
            Some(c.checked_add(d).ok_or_else(ovf)?)
        }
        None => None,
    };
    let mut times = Vec::with_capacity(amends.len());
    for a in amends {
        let d = [lat.gateway_ns, jitter()]
            .iter()
            .try_fold(s.latency_ns, |acc, b| acc.checked_add(*b))
            .ok_or_else(ovf)?;
        times.push((a.ts_send.checked_add(d).ok_or_else(ovf)?, a.new_qty));
    }
    Ok((ts_live, cancel, times))
}

fn mode_of(m: ModeName) -> Mode {
    match m {
        ModeName::Faithful => Mode::Faithful,
        ModeName::Corrected => Mode::Corrected,
    }
}

fn exch_of(e: ExchangeName) -> Exchange {
    match e {
        ExchangeName::Partial => Exchange::Partial,
        ExchangeName::NoPartial => Exchange::NoPartial,
    }
}

fn horizons(hs: &[Dur], h_max: Dur) -> QtResult<Vec<Dur>> {
    if h_max <= 0 {
        return Err(QtError::Config("h_max_ns must be > 0".into()));
    }
    let mut v: Vec<Dur> = hs.to_vec();
    if v.iter().any(|h| *h <= 0 || *h > h_max) {
        return Err(QtError::Config("horizons_ns must be in (0, h_max_ns]".into()));
    }
    v.push(h_max);
    v.sort_unstable();
    v.dedup();
    Ok(v)
}

/// Runs `f` over items on `threads` threads (std scoped threads); results in item order.
pub fn par_map<T: Sync, R: Send>(
    items: &[T],
    threads: usize,
    f: impl Fn(&T) -> QtResult<R> + Sync,
) -> QtResult<Vec<R>> {
    let threads = threads.max(1).min(items.len().max(1));
    if threads <= 1 {
        return items.iter().map(&f).collect();
    }
    let next = std::sync::atomic::AtomicUsize::new(0);
    let slots: Vec<Mutex<Option<QtResult<R>>>> = items.iter().map(|_| Mutex::new(None)).collect();
    std::thread::scope(|sc| {
        for _ in 0..threads {
            sc.spawn(|| {
                loop {
                    let i = next.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    let Some(item) = items.get(i) else { break };
                    let r = f(item);
                    if let Ok(mut s) = slots[i].lock() {
                        *s = Some(r);
                    }
                }
            });
        }
    });
    slots
        .into_iter()
        .map(|m| {
            m.into_inner()
                .ok()
                .flatten()
                .unwrap_or_else(|| Err(QtError::Engine("partition worker failed".into())))
        })
        .collect()
}

/// Captures the exact engine's shadow outputs.
#[derive(Default)]
struct TruthSink {
    results: Vec<ShadowResultRow>,
    fills: BTreeMap<String, Vec<(Ts, u64)>>,
    ends: BTreeMap<String, Ts>,
}

impl Sink for TruthSink {
    fn shadow_fill(&mut self, r: &ShadowFillRow) -> QtResult<()> {
        self.fills
            .entry(r.shadow_id.clone())
            .or_default()
            .push((r.ts_event, r.fill_qty));
        Ok(())
    }
    fn trajectory(&mut self, r: &TrajectoryRow) -> QtResult<()> {
        if r.kind == TrajectoryKind::End {
            self.ends.insert(r.shadow_id.clone(), r.ts_event);
        }
        Ok(())
    }
    fn shadow_result(&mut self, r: &ShadowResultRow) -> QtResult<()> {
        self.results.push(r.clone());
        Ok(())
    }
}

/// Forwards exact-engine callbacks to the shared partition simulation.
/// Engine observer: forwards to the partition's zoo state and counts the records the engine applied (an excluded
/// order's record is applied only when that order is a twin, `Engine::set_twin_linking`; a dropped one never is).
struct Obs(Arc<Mutex<PartitionSim>>, Arc<AtomicU64>);

impl EventObserver for Obs {
    fn on_event_start(&mut self, _instrument_id: u32, ts_event: Ts, book: &Book) {
        if let Ok(mut s) = self.0.lock() {
            s.mbo_event_start(ts_event, book);
        }
    }
    fn on_record(&mut self, e: &MboEvent, effect: &Effect, book: &Book) {
        self.1.fetch_add(1, Ordering::Relaxed);
        if let Ok(mut s) = self.0.lock() {
            s.mbo_record(e, effect, book);
        }
    }
    fn on_event_end(&mut self, end: &EventEnd<'_>) {
        if let Ok(mut s) = self.0.lock() {
            s.mbo_event_end(end.ts_event, end.sequence, end.book, end.shadow_truth);
        }
    }
}

/// One partition's inputs.
pub struct PartJob<'a> {
    /// Partition.
    pub part: &'a Partition,
    /// Instrument row.
    pub inst: Instrument,
    /// Shadow specs (engine input).
    pub specs: Vec<ShadowSpec>,
    /// Shadows with lifecycle times (same order).
    pub shadows: Vec<ShadowInput>,
    /// Schema 1.1 stream options (amends of these shadows, excluded real orders).
    pub opts: StreamOptions,
}

/// Schema 1.1 inputs of one instrument-day stream.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct StreamOptions {
    /// Amends of the stream's shadows (`qt.shadow_amend` rows, for the exact engine; the zoo arms read the effective
    /// times from `ShadowInput::amends`).
    pub amends: Vec<ShadowAmend>,
    /// Real order ids of the stream's instrument removed from the tape (audit A1, `ZooSpec.exclude_order_ids`): the
    /// exact engine excludes them natively, the L2 view is built without their `A`/`C`/`M`/`F` records (their `T`
    /// prints stay, so L2 models see the executions at the price) and their executions are credited to the
    /// `hbt_l3_fifo` shadows in their place. MBO streams only.
    pub exclude_order_ids: Vec<u64>,
    /// Ingest-recorded feed gaps of the stream's partition (`qt_replay::partition_gaps` of the dataset's
    /// `data_quality.json`), set on the exact engine exactly as `replay` does: shadows live across a gap end
    /// `censored_gap`. Record-level sequence-gap detection stays off (multi-instrument feeds). MBO streams only.
    pub feed_gaps: Vec<FeedGap>,
    /// Ingest-recorded trading halts of the stream's partition (`qt_replay::partition_halts` of the dataset's
    /// `data_quality.json`, SPEC 1.2), set on the exact engine exactly as `replay` does: a shadow live at a halt
    /// ends `censored_gap` there and one going live in a halt never goes live; the engine's censoring ends the
    /// model arms too. MBO streams only.
    pub trading_halts: Vec<qt_replay::TradingHalt>,
}

/// Exclusion counters of a stream (audit A1).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ExclusionStats {
    /// Ids requested for the stream's instrument.
    pub requested: u64,
    /// Ids seen on the tape.
    pub matched: u64,
    /// Records removed from the tape.
    pub records: u64,
}

/// Per-shadow facts for `zoo_shadows.parquet` and the simulator.
#[derive(Clone, Debug, Default)]
pub struct ShadowFacts {
    /// Shadow id.
    pub shadow_id: String,
    /// Instrument.
    pub instrument_id: u32,
    /// Session date.
    pub session_date: i32,
    /// ts_live.
    pub ts_live: Ts,
    /// Planned (exogenous) cancel effective time.
    pub planned_end: Option<Ts>,
    /// Session close of the partition.
    pub session_close: Ts,
    /// Administrative end (audit A2): first clear / bad-book / gap after `ts_live`, session close or end of data.
    pub admin_end: Ts,
    /// Quantity.
    pub qty: u64,
    /// Truth (exact arm only).
    pub truth: Option<Truth>,
}

/// Output of one partition run.
pub struct PartResult {
    /// Rows.
    pub out: PartitionOut,
    /// Facts per shadow (partition order).
    pub facts: Vec<ShadowFacts>,
    /// Recorded op tapes (record mode).
    pub tapes: Vec<Vec<Op>>,
    /// Exclusion counters (zero without `exclude_order_ids`).
    pub excluded: ExclusionStats,
}

fn sim_lock(sim: &Arc<Mutex<PartitionSim>>) -> QtResult<std::sync::MutexGuard<'_, PartitionSim>> {
    sim.lock()
        .map_err(|_| QtError::Engine("zoo partition state poisoned".into()))
}

fn dispatch_rows(sim: &Arc<Mutex<PartitionSim>>, rows: &[L2Row]) -> QtResult<()> {
    if rows.is_empty() {
        return Ok(());
    }
    let mut s = sim_lock(sim)?;
    for r in split_batches(rows) {
        s.on_l2_batch(&rows[r]);
    }
    Ok(())
}

/// Record input of one partition stream.
pub enum Input<'a> {
    /// MBO records (exact arm + L2 synthesized by `L2Builder`).
    Mbo(Box<dyn Iterator<Item = QtResult<MboEvent>> + 'a>),
    /// Ingested L2 rows (L2-only dataset: no exact arm).
    L2(Box<dyn Iterator<Item = QtResult<L2Row>> + 'a>),
}

/// Runs one partition of a dataset.
#[allow(clippy::too_many_arguments)]
pub fn run_partition(
    ds: &Dataset,
    job: PartJob<'_>,
    models: &[ModelId],
    cfg: &SimConfig,
    l2cfg: &qt_schema::specs::L2Config,
    latency: LatencyModel,
    seed: u64,
    record: bool,
) -> QtResult<PartResult> {
    let PartJob {
        part,
        inst,
        specs,
        shadows,
        opts,
    } = job;
    let input = match part.kind {
        DatasetKind::Mbo => Input::Mbo(part.mbo()?),
        DatasetKind::L2 => Input::L2(part.l2()?),
    };
    run_stream(
        ds.rules(),
        part.session_date,
        input,
        inst,
        specs,
        shadows,
        models,
        cfg,
        l2cfg,
        latency,
        seed,
        record,
        opts,
    )
}

/// Runs one instrument-day stream: exact engine + L2 arm (MBO) or the L2 arm alone (L2 rows). `opts` carries the
/// schema 1.1 amends and excluded real orders (`exclude_order_ids` on an L2 stream is `QT-CONFIG-001`).
#[allow(clippy::too_many_arguments)]
pub fn run_stream(
    rules: VenueRules,
    session_date: i32,
    input: Input<'_>,
    inst: Instrument,
    specs: Vec<ShadowSpec>,
    shadows: Vec<ShadowInput>,
    models: &[ModelId],
    cfg: &SimConfig,
    l2cfg: &qt_schema::specs::L2Config,
    latency: LatencyModel,
    seed: u64,
    record: bool,
    opts: StreamOptions,
) -> QtResult<PartResult> {
    let StreamOptions {
        amends,
        exclude_order_ids,
        feed_gaps,
        trading_halts,
    } = opts;
    let mut excluded = ExclusionStats {
        requested: exclude_order_ids.len() as u64,
        ..ExclusionStats::default()
    };
    let mut sim = PartitionSim::new(cfg.clone(), models, shadows.clone())?;
    if record {
        sim.set_record(true);
    }
    let spans: Vec<(Ts, Ts)> = trading_halts.iter().map(|h| (h.ts_start, h.ts_end)).collect();
    sim.set_trading_halts(&spans);
    let close = inst
        .midnight_utc_ns
        .saturating_add(i64::from(inst.close_s).saturating_mul(1_000_000_000));
    let mut facts: Vec<ShadowFacts> = shadows
        .iter()
        .map(|s| ShadowFacts {
            shadow_id: s.shadow_id.clone(),
            instrument_id: s.instrument_id,
            session_date,
            ts_live: s.ts_live,
            planned_end: s.cancel_at,
            session_close: close,
            admin_end: Ts::MAX,
            qty: s.qty,
            truth: None,
        })
        .collect();
    let truths: Option<Vec<Truth>>;
    let sim = Arc::new(Mutex::new(sim));
    match input {
        Input::Mbo(records) => {
            let ecfg = EngineConfig {
                rules,
                latency,
                seed,
                rank_oracle: false,
                sweep_gap_ns: 2_000_000,
                bbo: BboMode::None,
                depth_levels: 0,
                trajectory: TrajectoryMode::Ahead,
                race_window_ns: 0,
                real_orders: false,
                strict: false,
            };
            let mut engine = Engine::new(ecfg, std::slice::from_ref(&inst), specs.clone(), amends)?;
            // as qt_replay::run_dataset: sequence gaps are an ingest-time property (partitions of a multi-instrument
            // feed see the feed's global sequence jump); live detection would censor every shadow.
            // The ingest-recorded gaps of the partition censor the shadows live across them, as in replay.
            engine.set_sequence_gap_detection(false);
            engine.set_feed_gaps(&feed_gaps);
            engine.set_trading_halts(&trading_halts)?;
            // audit A1: excluded orders and the level (side, price) each rests at (from its `A`/`M` records, as the
            // engine tracks it) for crediting its executions to the `hbt_l3_fifo` shadows
            let iid = inst.instrument_id;
            let mut excl: HashMap<u64, Option<(BookSide, Px)>> = exclude_order_ids.iter().map(|o| (*o, None)).collect();
            // twin linking (as replay): an excluded order that a shadow enters at stays on the tape for
            // every other shadow and is replaced by its own shadow only; `twin[o]` = decided at its first record
            let mut twin: HashMap<u64, bool> = HashMap::new();
            if !excl.is_empty() {
                engine.set_excluded_orders(exclude_order_ids.iter().map(|o| (iid, *o)));
                engine.set_twin_linking(true);
            }
            let applied = Arc::new(AtomicU64::new(0));
            {
                let mut s = sim_lock(&sim)?;
                let mut map = Vec::new();
                for (i, sp) in specs.iter().enumerate() {
                    if let Some(ei) = engine.shadow_index(&sp.shadow_id) {
                        let ei = ei as usize;
                        if map.len() <= ei {
                            map.resize(ei + 1, None);
                        }
                        map[ei] = Some(i as u32);
                    }
                }
                s.engine_map = map;
            }
            engine.add_observer(Box::new(Obs(sim.clone(), applied.clone())));
            let mut sink = TruthSink::default();
            let mut l2b = qt_l2::L2Builder::new(qt_l2::L2Config::from(l2cfg), rules, inst.instrument_id);
            let mut rows: Vec<L2Row> = Vec::new();
            let mut last_ts = Ts::MIN;
            for e in records {
                let e = e?;
                last_ts = last_ts.max(e.ts_event);
                let removed = !excl.is_empty()
                    && e.instrument_id == iid
                    && matches!(e.action, b'A' | b'C' | b'M' | b'F')
                    && excl.contains_key(&e.order_id);
                if removed {
                    let decided = twin.get(&e.order_id).copied();
                    if decided != Some(true)
                        && e.action == b'F'
                        && let Some(Some((side, px))) = excl.get(&e.order_id).copied()
                    {
                        // a dropped order's execution, reported before the engine applies (and drops) the record:
                        // credited after the event start the record may open
                        sim_lock(&sim)?.mbo_excluded_exec(side, px, e.size);
                    }
                    let before = applied.load(Ordering::Relaxed);
                    engine.apply(&e, &mut sink)?;
                    let is_twin = match decided {
                        Some(t) => t,
                        None => {
                            let t = applied.load(Ordering::Relaxed) > before;
                            twin.insert(e.order_id, t);
                            t
                        }
                    };
                    if is_twin {
                        // the twin stays a real order for every other shadow: the L2 view keeps its displayed size
                        l2b.push(&e, &mut rows);
                        if !rows.is_empty() {
                            dispatch_rows(&sim, &rows)?;
                            rows.clear();
                        }
                        continue;
                    }
                    if matches!(e.action, b'A' | b'M') {
                        excl.insert(
                            e.order_id,
                            BookSide::from_byte(e.side)
                                .filter(|_| e.price != UNDEF_PRICE)
                                .map(|s| (s, e.price)),
                        );
                    }
                    if e.is_last() {
                        // the L2 view never sees the record; a no-op record keeps the event boundary it carried
                        let mut n = e;
                        n.action = b'N';
                        l2b.push(&n, &mut rows);
                    }
                } else {
                    engine.apply(&e, &mut sink)?;
                    l2b.push(&e, &mut rows);
                }
                if !rows.is_empty() {
                    dispatch_rows(&sim, &rows)?;
                    rows.clear();
                }
            }
            l2b.flush(last_ts, &mut rows);
            dispatch_rows(&sim, &rows)?;
            let report = engine.finish(&mut sink)?;
            excluded.matched = report.diagnostics.excluded_matched;
            excluded.records = report.diagnostics.excluded_records;
            let mut by_id: BTreeMap<&str, &ShadowResultRow> = BTreeMap::new();
            for r in &sink.results {
                by_id.insert(r.shadow_id.as_str(), r);
            }
            let mut tv = Vec::with_capacity(shadows.len());
            let mut warn = Vec::new();
            for s in &shadows {
                let mut t = Truth {
                    ts_live: s.ts_live,
                    ..Truth::default()
                };
                if let Some(r) = by_id.get(s.shadow_id.as_str()) {
                    if r.ts_live != s.ts_live {
                        warn.push(format!(
                            "shadow {}: exact ts_live {} differs from the zoo's {}",
                            s.shadow_id, r.ts_live, s.ts_live
                        ));
                    }
                    t.ts_live = r.ts_live;
                    t.price = r.price;
                    t.ts_first_fill = r.ts_first_fill;
                    t.fills = sink.fills.get(&s.shadow_id).cloned().unwrap_or_default();
                    t.queue_ahead_at_live = r.queue_ahead_at_live;
                    t.level_qty_at_live = r.level_qty_at_live;
                    t.end_state = Some(r.end_state);
                    t.ts_activated = r.ts_activated_event;
                    t.censor_time = match r.end_state {
                        EndState::CensoredSessionEnd
                        | EndState::CensoredClear
                        | EndState::CensoredGap
                        | EndState::OpenAtEnd => Some(sink.ends.get(&s.shadow_id).copied().unwrap_or(last_ts)),
                        _ => None,
                    };
                } else {
                    warn.push(format!("shadow {}: no exact result", s.shadow_id));
                }
                tv.push(t);
            }
            truths = Some(tv);
            let mut s = sim_lock(&sim)?;
            for (i, sh) in shadows.iter().enumerate() {
                if let (Some(tp), Some(zp)) = (truths.as_ref().and_then(|t| t[i].price), s.price_of(i as u32))
                    && tp != zp
                {
                    warn.push(format!(
                        "shadow {}: exact price {tp} differs from the zoo's {zp}",
                        sh.shadow_id
                    ));
                }
            }
            s.push_warnings(warn);
        }
        Input::L2(rows) => {
            if !exclude_order_ids.is_empty() {
                return Err(QtError::Config(
                    "exclude_order_ids: needs an MBO dataset (an L2 tape has no order ids to remove)".into(),
                ));
            }
            let mut buf: Vec<L2Row> = Vec::new();
            for r in rows {
                let r = r?;
                let boundary = buf.last().is_some_and(|p: &L2Row| p.event_id != r.event_id);
                if boundary {
                    dispatch_rows(&sim, &buf)?;
                    buf.clear();
                }
                let last = r.flags & qt_schema::rows::l2flags::LAST_IN_BATCH != 0;
                buf.push(r);
                if last {
                    dispatch_rows(&sim, &buf)?;
                    buf.clear();
                }
            }
            dispatch_rows(&sim, &buf)?;
            truths = None;
        }
    }
    let sim = Arc::try_unwrap(sim)
        .map_err(|_| QtError::Engine("zoo partition state still shared".into()))?
        .into_inner()
        .map_err(|_| QtError::Engine("zoo partition state poisoned".into()))?;
    let tapes = if record { sim.tapes() } else { Vec::new() };
    if let Some(t) = &truths {
        for (f, tr) in facts.iter_mut().zip(t) {
            f.truth = Some(tr.clone());
        }
    }
    let out = sim.finish(truths.as_deref(), session_date, true, close);
    for (f, a) in facts.iter_mut().zip(&out.admin_end) {
        f.admin_end = *a;
    }
    Ok(PartResult {
        out,
        facts,
        tapes,
        excluded,
    })
}

/// Shadows of one partition: engine specs, zoo inputs (same order) and the amends of those shadows.
#[derive(Clone, Debug, Default)]
pub struct ShadowGroup {
    /// Engine specs.
    pub specs: Vec<ShadowSpec>,
    /// Zoo inputs (same order as `specs`).
    pub inputs: Vec<ShadowInput>,
    /// Amends of these shadows (`qt.shadow_amend` rows, row order kept).
    pub amends: Vec<ShadowAmend>,
}

fn input_of(s: &ShadowSpec, amends: &[ShadowAmend], lat: &LatencyModel, seed: u64) -> QtResult<ShadowInput> {
    let (ts_live, cancel_at, times) = lifecycle_with_amends(s, amends, lat, seed)?;
    Ok(ShadowInput {
        shadow_id: s.shadow_id.clone(),
        instrument_id: s.instrument_id,
        side: s.side,
        price: s.price,
        qty: s.qty,
        ts_live,
        cancel_at,
        amends: times,
    })
}

/// Shadow inputs (lifecycle times under `lat`) of specs, in order (no amends).
pub fn shadow_inputs(specs: &[ShadowSpec], lat: &LatencyModel, seed: u64) -> QtResult<Vec<ShadowInput>> {
    specs.iter().map(|s| input_of(s, &[], lat, seed)).collect()
}

/// Shadow inputs of specs with their amends (`qt.shadow_amend` rows; see `group_amends`), in order.
pub fn shadow_inputs_with_amends(
    specs: &[ShadowSpec],
    amends: &[ShadowAmend],
    lat: &LatencyModel,
    seed: u64,
) -> QtResult<Vec<ShadowInput>> {
    let by = group_amends(specs, amends)?;
    specs
        .iter()
        .map(|s| input_of(s, by.get(&s.shadow_id).map_or(&[][..], Vec::as_slice), lat, seed))
        .collect()
}

/// Amends grouped by shadow id (row order kept); an amend of an unknown shadow is `QT-INPUT-002` (as the engine).
pub fn group_amends(specs: &[ShadowSpec], amends: &[ShadowAmend]) -> QtResult<BTreeMap<String, Vec<ShadowAmend>>> {
    let known: HashSet<&str> = specs.iter().map(|s| s.shadow_id.as_str()).collect();
    let mut by: BTreeMap<String, Vec<ShadowAmend>> = BTreeMap::new();
    for a in amends {
        if !known.contains(a.shadow_id.as_str()) {
            return Err(QtError::InputFormat {
                location: format!("amend for shadow {}", a.shadow_id),
                msg: "amend references an unknown shadow_id".into(),
            });
        }
        by.entry(a.shadow_id.clone()).or_default().push(a.clone());
    }
    Ok(by)
}

/// Splits shadows (and their amends, grouped by `group_amends`) by partition (`partition_for(instrument,
/// ts_submit)`), computing lifecycle times.
pub fn split(
    ds: &Dataset,
    specs: Vec<ShadowSpec>,
    amends: &BTreeMap<String, Vec<ShadowAmend>>,
    lat: &LatencyModel,
    seed: u64,
    warnings: &mut Vec<String>,
) -> QtResult<BTreeMap<(i32, u32), ShadowGroup>> {
    let mut m: BTreeMap<(i32, u32), ShadowGroup> = BTreeMap::new();
    let mut missing = 0u64;
    for s in specs {
        let Some(p) = ds.partition_for(s.instrument_id, s.ts_submit) else {
            missing += 1;
            continue;
        };
        let am = amends.get(&s.shadow_id).map_or(&[][..], Vec::as_slice);
        let input = input_of(&s, am, lat, seed)?;
        let e = m.entry((p.session_date, p.instrument_id)).or_default();
        e.amends.extend_from_slice(am);
        e.specs.push(s);
        e.inputs.push(input);
    }
    if missing > 0 {
        warnings.push(format!(
            "{missing} shadows have no partition (censored_gap; no predictions written)"
        ));
    }
    Ok(m)
}

/// `qt.zoo_shadows` (registered in schema 1.1, 3.11 #5): per-shadow planned life for the outcome-independent truth
/// labels of audit ruling A2; the first three columns are exactly `queuetruth.eval.zoo.planned_life_table`.
fn zoo_shadows_batch(facts: &[ShadowFacts], h_max: Dur) -> QtResult<RecordBatch> {
    let schema = qt_schema::schema("qt.zoo_shadows")
        .ok_or_else(|| QtError::Engine("qt.zoo_shadows is not a registered table".into()))?;
    let mut id = StringBuilder::new();
    let mut planned = TimestampNanosecondBuilder::new().with_timezone("UTC");
    let mut admin = TimestampNanosecondBuilder::new().with_timezone("UTC");
    let mut inst = UInt32Builder::new();
    let mut date = Date32Builder::new();
    let mut live = TimestampNanosecondBuilder::new().with_timezone("UTC");
    let mut close = TimestampNanosecondBuilder::new().with_timezone("UTC");
    let mut end = StringBuilder::new();
    let mut never_cancel = BooleanBuilder::new();
    for f in facts {
        id.append_value(&f.shadow_id);
        planned.append_option(f.planned_end);
        admin.append_option((f.admin_end != Ts::MAX).then_some(f.admin_end));
        inst.append_value(f.instrument_id);
        date.append_value(f.session_date);
        live.append_value(f.ts_live);
        close.append_value(f.session_close);
        end.append_option(f.truth.as_ref().and_then(|t| t.end_state).map(|e| e.to_string()));
        never_cancel.append_value(f.planned_end.is_none_or(|p| p >= f.ts_live.saturating_add(h_max)));
    }
    let cols: Vec<ArrayRef> = vec![
        Arc::new(id.finish()),
        Arc::new(planned.finish()),
        Arc::new(admin.finish()),
        Arc::new(inst.finish()),
        Arc::new(date.finish()),
        Arc::new(live.finish()),
        Arc::new(close.finish()),
        Arc::new(end.finish()),
        Arc::new(never_cancel.finish()),
    ];
    RecordBatch::try_new(schema, cols).map_err(|e| QtError::Engine(format!("zoo_shadows: {e}")))
}

fn write_table(path: &Path, batch: RecordBatch) -> QtResult<FileEntry> {
    let mut w = TableWriter::create_with_schema(path, batch.schema())?;
    if batch.num_rows() > 0 {
        w.write(&batch)?;
    }
    w.finish()
}

/// Shadows whose model trajectories `run_zoo` writes by default (`write_trajectory = false` on an MBO dataset): the
/// why-trace selection of the report (`ReportSpec.max_why` default).
pub const TRACED_SHADOWS: usize = 20;

/// The shadows the report's zoo why traces pick (SPEC 5.9, same ranking as `queuetruth.report.why`): rows at the
/// largest horizon with a truth label that are not censored; per shadow the lexicographic maximum over models of
/// (|p_fill - truth|, |model - exact first fill time| when both filled, else 0); shadows ranked by that error, the
/// number of models wrong (error >= 0.5), the time gap, then the id; only error or gap > 0; the first `k`.
pub fn traced_shadows(preds: &[ZooPredictionRow], k: usize) -> Vec<String> {
    let Some(hmax) = preds.iter().map(|p| p.horizon_ns).max() else {
        return Vec::new();
    };
    let mut best: BTreeMap<&str, (f64, i64)> = BTreeMap::new();
    let mut wrong: BTreeMap<&str, u32> = BTreeMap::new();
    for p in preds {
        let Some(y) = p.truth_filled.filter(|_| p.horizon_ns == hmax && !p.censored) else {
            continue;
        };
        let e = (p.p_fill - if y { 1.0 } else { 0.0 }).abs();
        let e = if e.is_finite() { e } else { 0.0 };
        let gap = match (p.model_ts_first_fill, p.truth_ts_first_fill) {
            (Some(a), Some(b)) => a.saturating_sub(b).saturating_abs(),
            _ => 0,
        };
        if e >= 0.5 {
            *wrong.entry(p.shadow_id.as_str()).or_default() += 1;
        }
        let cur = best.entry(p.shadow_id.as_str()).or_insert((e, gap));
        if e.total_cmp(&cur.0).then(gap.cmp(&cur.1)).is_gt() {
            *cur = (e, gap);
        }
    }
    let mut ranked: Vec<(&str, (f64, i64))> = best.into_iter().filter(|(_, (e, g))| *e > 0.0 || *g > 0).collect();
    let nw = |s: &str| wrong.get(s).copied().unwrap_or(0);
    ranked.sort_by(|a, b| {
        b.1.0
            .total_cmp(&a.1.0)
            .then(nw(b.0).cmp(&nw(a.0)))
            .then(b.1.1.cmp(&a.1.1))
            .then(a.0.cmp(b.0))
    });
    ranked.into_iter().take(k).map(|(s, _)| s.to_owned()).collect()
}

/// The partition jobs restricted to the shadows `ids` (their amends kept).
fn subset_jobs<'a>(jobs: &[(&'a Partition, ShadowGroup)], ids: &HashSet<&str>) -> Vec<(&'a Partition, ShadowGroup)> {
    jobs.iter()
        .filter_map(|(p, g)| {
            let keep: Vec<usize> = (0..g.specs.len())
                .filter(|&i| ids.contains(g.specs[i].shadow_id.as_str()))
                .collect();
            (!keep.is_empty()).then(|| {
                (
                    *p,
                    ShadowGroup {
                        specs: keep.iter().map(|&i| g.specs[i].clone()).collect(),
                        inputs: keep.iter().map(|&i| g.inputs[i].clone()).collect(),
                        amends: g
                            .amends
                            .iter()
                            .filter(|a| ids.contains(a.shadow_id.as_str()))
                            .cloned()
                            .collect(),
                    },
                )
            })
        })
        .collect()
}

/// Runs the partition jobs of a zoo spec (`write_trajectory` as given).
/// `ZooSpec.exclude_order_ids` by (instrument_id, session date as days since 1970-01-01, `None` = every date).
pub type ExcludeMap = BTreeMap<(u32, Option<i32>), Vec<u64>>;

/// Groups `exclude_order_ids` items by instrument and session date (`ExcludeOrderId.session_date`: that date only,
/// as `ReplaySpec.exclude_order_ids`; a malformed date is `QT-CONFIG-001`). Ids sorted and deduplicated.
pub fn exclude_map(items: &[ExcludeOrderId]) -> QtResult<ExcludeMap> {
    let mut m: ExcludeMap = BTreeMap::new();
    for x in items {
        let date = match x.session_date.as_deref() {
            None => None,
            Some(d) => Some(
                qt_ingest::time::parse_date(d)
                    .map_err(|e| QtError::Config(format!("exclude_order_ids: session_date {d:?}: {e}")))?,
            ),
        };
        m.entry((x.instrument_id, date)).or_default().push(x.order_id);
    }
    for v in m.values_mut() {
        v.sort_unstable();
        v.dedup();
    }
    Ok(m)
}

/// The excluded order ids of one partition: the instrument's every-date items plus those dated that session.
pub fn excluded_on(m: &ExcludeMap, instrument_id: u32, session_date: i32) -> Vec<u64> {
    let mut v: Vec<u64> = [None, Some(session_date)]
        .iter()
        .filter_map(|d| m.get(&(instrument_id, *d)))
        .flatten()
        .copied()
        .collect();
    v.sort_unstable();
    v.dedup();
    v
}

#[allow(clippy::too_many_arguments)]
fn run_jobs(
    ds: &Dataset,
    jobs: &[(&Partition, ShadowGroup)],
    spec: &ZooSpec,
    models: &[ModelId],
    hs: &[Dur],
    exclude: &ExcludeMap,
    write_trajectory: bool,
) -> QtResult<Vec<PartResult>> {
    // Ingest-recorded feed gaps and trading halts, read once (as qt_replay::run_dataset).
    let dq = qt_replay::ingest_dq(ds);
    let gaps = dq.as_ref().map(|d| d.feed_gaps.clone()).unwrap_or_default();
    let halts = dq.as_ref().map(qt_replay::halts_of).unwrap_or_default();
    par_map(jobs, spec.threads as usize, |(part, g)| {
        let inst = ds
            .instrument(part.instrument_id, part.session_date)
            .cloned()
            .ok_or_else(|| QtError::Config(format!("no instrument row for partition {}", part.instrument_id)))?;
        let cfg = SimConfig {
            horizons: hs.to_vec(),
            h_max: spec.h_max_ns,
            tick: inst.tick_size.max(1),
            lot: (inst.lot_size.max(1)) as f64,
            cadence0: spec.l2.cadence_ns == 0,
            seed: spec.seed,
            write_trajectory,
            trace: false,
        };
        let midnight = inst.midnight_utc_ns;
        let job = PartJob {
            part,
            inst,
            specs: g.specs.clone(),
            shadows: g.inputs.clone(),
            opts: StreamOptions {
                amends: g.amends.clone(),
                exclude_order_ids: excluded_on(exclude, part.instrument_id, part.session_date),
                feed_gaps: qt_replay::partition_gaps(&gaps, part),
                trading_halts: qt_replay::partition_halts(&halts, part, Some(midnight)),
            },
        };
        run_partition(ds, job, models, &cfg, &spec.l2, spec.latency, spec.seed, false)
    })
}

/// Schema 1.1 contract on an L2-only dataset: options the L2 arm cannot honour are `QT-CONFIG-001`, never ignored.
fn check_l2_only(spec: &ZooSpec, models: &[ModelId]) -> QtResult<()> {
    if models.iter().any(|m| m.family == Family::HbtL3Fifo) {
        return Err(QtError::Config("hbt_l3_fifo needs an MBO dataset".into()));
    }
    if !spec.exclude_order_ids.is_empty() {
        return Err(QtError::Config(
            "exclude_order_ids: needs an MBO dataset (an L2 tape has no order ids; remove own orders from the L2 rows \
             instead)"
                .into(),
        ));
    }
    if spec.write_trajectory {
        return Err(QtError::Config(
            "write_trajectory: needs an MBO dataset (the trajectory compares the model with the exact queue)".into(),
        ));
    }
    if spec.l2 != qt_schema::specs::L2Config::default() {
        return Err(QtError::Config(
            "l2: an L2-only dataset is read as ingested; L2 view settings (depth_levels, cadence_ns, trade_position, \
             include_counts, emit_hidden_trades) need an MBO dataset (leave l2 at its default)"
                .into(),
        ));
    }
    Ok(())
}

/// `zoo`: writes the tables and `summary.json`; the leaderboard is left empty (filled by `queuetruth.eval`).
///
/// Schema 1.1: `amends` (`qt.shadow_amend`) apply to the exact arm and to every model (priority-keeping size
/// reductions at the exact engine's effective times); `exclude_order_ids` removes those real orders from the tape of
/// MBO datasets (exact arm natively, L2 view without their records, `hbt_l3_fifo` credited with their executions);
/// `qt.zoo_fills` and `qt.zoo_predictions` are written in the full 1.1 layout (`liquidity`, `crate::fill_liquidity`:
/// taker only for `crossed_at_live`).
pub fn run_zoo(spec: &ZooSpec) -> QtResult<json::ZooSummary> {
    let root = spec.dataset.as_path().ok_or_else(|| {
        QtError::Config("dataset: a local path is required (dataset_id is resolved by the service)".into())
    })?;
    let shadows_path = spec
        .shadows
        .as_path()
        .ok_or_else(|| QtError::Config("shadows: a local path is required".into()))?;
    let out = PathBuf::from(
        spec.out
            .as_deref()
            .ok_or_else(|| QtError::Config("out: required".into()))?,
    );
    let mode = mode_of(spec.mode);
    let exchange = exch_of(spec.exchange);
    if spec.particles == 0 {
        return Err(QtError::Config("particles must be >= 1".into()));
    }
    // model ids first: a typo fails (with its suggestion) before any data is read
    let models = expand_models(&spec.models, mode, exchange, spec.particles)?;
    let ds = Dataset::open(Path::new(root))?;
    let specs = codec::read_shadow_specs(Path::new(shadows_path))?;
    let n_shadows = specs.len() as u64;
    let hs = horizons(&spec.horizons_ns, spec.h_max_ns)?;
    let mut warnings = Vec::new();
    if ds.manifest.kind == json::DatasetKindName::L2 {
        check_l2_only(spec, &models)?;
    }
    let amends = match &spec.amends {
        Some(r) => {
            let p = r
                .as_path()
                .ok_or_else(|| QtError::Config("amends: a local path is required".into()))?;
            codec::read_shadow_amends(Path::new(p))?
        }
        None => Vec::new(),
    };
    let by_shadow = group_amends(&specs, &amends)?;
    let exclude = exclude_map(&spec.exclude_order_ids)?;
    let n_excl: u64 = exclude.values().map(|v| v.len() as u64).sum();
    let groups = split(&ds, specs, &by_shadow, &spec.latency, spec.seed, &mut warnings)?;
    let jobs: Vec<(&Partition, ShadowGroup)> = ds
        .partitions()
        .iter()
        .filter_map(|p| groups.get(&(p.session_date, p.instrument_id)).map(|g| (p, g.clone())))
        .collect();
    let results = run_jobs(&ds, &jobs, spec, &models, &hs, &exclude, spec.write_trajectory)?;
    let mut preds = Vec::new();
    let mut pred_liq = Vec::new();
    let mut pred_aborted = Vec::new();
    let mut fills = Vec::new();
    let mut traj = Vec::new();
    let mut facts = Vec::new();
    let mut excl = ExclusionStats::default();
    let mut errs: BTreeMap<String, (u64, String, Ts)> = BTreeMap::new();
    for r in results {
        preds.extend(r.out.predictions);
        pred_liq.extend(r.out.pred_liquidity);
        pred_aborted.extend(r.out.pred_aborted);
        fills.extend(r.out.fills);
        traj.extend(r.out.trajectory);
        facts.extend(r.facts);
        warnings.extend(r.out.warnings);
        excl.matched += r.excluded.matched;
        excl.records += r.excluded.records;
        for (k, v) in r.out.hbt_errors {
            let e = errs.entry(k).or_insert((0, v.1.clone(), v.2));
            e.0 += v.0;
        }
    }
    // Faithful hbt #312 (E8; SPEC 5.3.2): hftbacktest stops the whole backtest at the zombie's InvalidOrderStatus,
    // but one order's quirk must not void a model's evaluation on every other shadow, so the event aborts only that
    // (model, shadow) pair from that point on; the model stays, its rows from the abort on carry status
    // `hbt_312_abort`, and one warning per model gives the count (stable prefix `hbt_312_abort: `); the summary
    // field `hbt_312_aborts` carries the same counts by model id.
    let hbt_312_aborts = errs.iter().map(|(m, (n, _, _))| (m.clone(), *n)).collect();
    for (m, (n, sid, ts)) in &errs {
        warnings.push(format!(
            "hbt_312_abort: {m}: {n} of {n_shadows} shadow pair(s) aborted at the #312 InvalidOrderStatus event \
             (faithful hftbacktest stops there), first at shadow {sid} ts {ts}; their predictions from the abort on \
             carry status hbt_312_abort and are left out of the metrics"
        ));
    }
    if n_excl > 0 {
        // same counters as the replay warning (partitions with shadows only)
        warnings.push(format!(
            "exclude_order_ids: requested={n_excl} matched={} unmatched={} records_removed={}",
            excl.matched,
            n_excl.saturating_sub(excl.matched),
            excl.records
        ));
    }
    if pred_liq.len() != preds.len() || pred_aborted.len() != preds.len() {
        return Err(QtError::Engine("zoo_predictions: liquidity column misaligned".into()));
    }
    // 0.1.1: a shadow any faithful hbt pair aborted on is left out of every model's metrics (one common population)
    let n_excluded_hbt_312 = preds
        .iter()
        .zip(&pred_aborted)
        .filter(|(_, a)| **a)
        .map(|(p, _)| p.shadow_id.as_str())
        .collect::<HashSet<&str>>()
        .len() as u64;
    std::fs::create_dir_all(&out).map_err(QtError::Io)?;
    let pred_batch = codec::with_added_columns(
        "qt.zoo_predictions",
        &codec::zoo_predictions_to_batch(&preds),
        vec![
            codec::liquidity_column(pred_liq),
            codec::zoo_prediction_status_column(pred_aborted),
        ],
    )?;
    let mut files = vec![write_table(&out.join("zoo_predictions.parquet"), pred_batch)?];
    if spec.write_fills {
        let liq = codec::liquidity_column(fills.iter().map(|f| Some(crate::fill_liquidity(f.reason))));
        files.push(write_table(
            &out.join("zoo_fills.parquet"),
            codec::with_added_columns("qt.zoo_fills", &codec::zoo_fills_to_batch(&fills), vec![liq])?,
        )?);
    }
    // trajectories: every shadow with `write_trajectory`; else, on an MBO dataset, the shadows the report's why
    // traces pick (all models), from a second pass over their partitions (shadows are independent, so their rows
    // equal those of a full run); the table metadata `qt.zoo_trajectory_scope` says which (`all` / `traced:<n>`)
    let mut scope = "all".to_owned();
    if !spec.write_trajectory && ds.manifest.kind != json::DatasetKindName::L2 {
        let traced = traced_shadows(&preds, TRACED_SHADOWS);
        if !traced.is_empty() {
            let ids: HashSet<&str> = traced.iter().map(String::as_str).collect();
            let sub = subset_jobs(&jobs, &ids);
            traj = run_jobs(&ds, &sub, spec, &models, &hs, &exclude, true)?
                .into_iter()
                .flat_map(|r| r.out.trajectory)
                .collect();
            scope = format!("traced:{}", traced.len());
        }
    }
    if spec.write_trajectory || scope != "all" {
        let batch = codec::zoo_trajectory_to_batch(&traj);
        let mut md = batch.schema().metadata().clone();
        md.insert("qt.zoo_trajectory_scope".into(), scope);
        let schema = Arc::new(batch.schema().as_ref().clone().with_metadata(md));
        let batch = RecordBatch::try_new(schema, batch.columns().to_vec())
            .map_err(|e| QtError::Engine(format!("zoo_trajectory: {e}")))?;
        files.push(write_table(&out.join("zoo_trajectory.parquet"), batch)?);
    }
    files.push(write_table(
        &out.join("zoo_shadows.parquet"),
        zoo_shadows_batch(&facts, spec.h_max_ns)?,
    )?);
    let spec_v = serde_json::to_value(spec).map_err(|e| QtError::Engine(format!("spec: {e}")))?;
    let manifest_id = ds.manifest.dataset_id.clone();
    let resolve = |v: &serde_json::Value| -> QtResult<String> {
        let o = v.as_object().ok_or_else(|| QtError::Config("bad ref".into()))?;
        if let Some(p) = o.get("path").and_then(|p| p.as_str()) {
            if p == root {
                return Ok(manifest_id.clone());
            }
            return qt_schema::io::sha256_file(Path::new(p));
        }
        Err(QtError::Config("non-path refs are resolved by the service".into()))
    };
    let run_id = qt_schema::run_id(&spec_v, qt_schema::ENGINE_VERSION, &resolve)?;
    let echo = match qt_schema::spec_echo(&spec_v) {
        serde_json::Value::Object(o) => o,
        _ => serde_json::Map::new(),
    };
    let summary = json::ZooSummary {
        schema: Default::default(),
        schema_version: Default::default(),
        run_id,
        spec: echo,
        n_shadows,
        models: models.iter().map(ToString::to_string).collect(),
        leaderboard: Vec::new(),
        files: files.clone(),
        warnings,
        hbt_312_aborts,
        n_excluded_hbt_312,
        n_leaderboard: None,
    };
    let text = serde_json::to_string_pretty(&summary).map_err(|e| QtError::Engine(format!("summary: {e}")))?;
    std::fs::write(out.join("summary.json"), text + "\n").map_err(QtError::Io)?;
    Ok(summary)
}

// ---- particle simulator (C2) -------------------------------------------------------------------------------------

struct TapeShadow {
    shadow_id: String,
    qty: f64,
    lot: f64,
    ts_live: Ts,
    ops: Vec<Op>,
    truth_interval: Option<usize>,
    censored_from: Option<usize>,
}

struct DbSimulator {
    horizons: Vec<Dur>,
    mode: Mode,
    particles: u32,
    threads: usize,
    shadows: Vec<TapeShadow>,
}

impl ParticleSimulator for DbSimulator {
    fn horizons(&self) -> &[Dur] {
        &self.horizons
    }

    fn simulate(&self, theta: f64, seed: u64) -> QtResult<Vec<ShadowCurve>> {
        let id = ModelId::new(
            Family::DatabentoMc,
            &[("k", theta), ("particles", f64::from(self.particles))],
            self.mode,
            Exchange::Partial,
        )?;
        let idx: Vec<usize> = (0..self.shadows.len()).collect();
        par_map(&idx, self.threads, |&i| {
            let t = &self.shadows[i];
            let slot = ModelSlot::new(crate::build(&id)?);
            // common random numbers: the stream does not depend on theta
            let rng = Xoshiro256StarStar::new(stable_seed(seed, &[b"databento_mc", t.shadow_id.as_bytes()]));
            let mut pair = Pair::new(&slot, t.qty, rng);
            let o = crate::ShadowL2 {
                idx: i as u32,
                side: qt_types::BookSide::Bid,
                price: 0,
                qty: t.qty,
                lot: t.lot,
            };
            for op in &t.ops {
                pair.apply(&slot, &o, op);
                if pair.finished() {
                    break;
                }
            }
            let n = pair.st.particles.len().max(1) as f64;
            let survival = self
                .horizons
                .iter()
                .map(|h| {
                    let dl = t.ts_live.saturating_add(*h);
                    let filled = pair
                        .st
                        .particles
                        .iter()
                        .filter(|p| p.first_fill.is_some_and(|f| f <= dl))
                        .count();
                    let alive = pair.st.particles.len().saturating_sub(filled) as f64;
                    (alive + 0.5) / (n + 1.0)
                })
                .collect();
            Ok(ShadowCurve {
                shadow_idx: i as u32,
                survival,
                truth_interval: t.truth_interval,
                censored_from: t.censored_from,
            })
        })
    }
}

fn block_of(ts_live: Ts, inst: Option<&Instrument>, n: u32) -> u32 {
    let Some(i) = inst else { return 0 };
    let open = i.midnight_utc_ns.saturating_add(i64::from(i.open_s) * 1_000_000_000);
    let len = (i64::from(i.close_s) - i64::from(i.open_s)).max(1) * 1_000_000_000;
    let pos = (ts_live.saturating_sub(open)) as f64 / len as f64;
    ((pos * f64::from(n)).floor().max(0.0) as u32).min(n.saturating_sub(1))
}

fn in_window(w: &WindowSpec, date: i32, ts_live: Ts, inst: Option<&Instrument>) -> QtResult<bool> {
    if let Some(d) = &w.dates {
        let mut ok = false;
        for s in d {
            if qt_ingest::time::parse_date(s)? == date {
                ok = true;
            }
        }
        if !ok {
            return Ok(false);
        }
    }
    if let Some(b) = &w.blocks {
        return Ok(b.index.contains(&block_of(ts_live, inst, b.n_blocks.max(1))));
    }
    Ok(true)
}

/// Particle simulator of `databento_mc` (faithful or corrected per the spec) over the train shadows: one data pass
/// records each train shadow's op tape and exact truth; `simulate(k, seed)` replays the tapes with common random
/// numbers (stream `stable_seed(seed, ["databento_mc", shadow_id])`, independent of `k`). Survival values are the
/// smoothed `(#no fill by tau_j + 0.5) / (N + 1)`; `censored_from` uses administrative censoring only (audit A2).
pub fn particle_simulator(spec: &CalibrationSpec) -> QtResult<Box<dyn ParticleSimulator>> {
    let ds_ref = spec
        .dataset
        .as_ref()
        .ok_or_else(|| QtError::Config("dataset: required".into()))?;
    let root = ds_ref
        .as_path()
        .ok_or_else(|| QtError::Config("dataset: a local path is required".into()))?;
    let sh_ref = spec
        .shadows
        .as_ref()
        .ok_or_else(|| QtError::Config("shadows: required".into()))?;
    let sh_path = sh_ref
        .as_path()
        .ok_or_else(|| QtError::Config("shadows: a local path is required".into()))?;
    let ds = Dataset::open(Path::new(root))?;
    if ds.manifest.kind == json::DatasetKindName::L2 {
        return Err(QtError::Config(
            "survival calibration needs an MBO dataset (exact truth)".into(),
        ));
    }
    let specs = codec::read_shadow_specs(Path::new(sh_path))?;
    let mode = mode_of(spec.mode);
    if spec.particles == 0 {
        return Err(QtError::Config("particles must be >= 1".into()));
    }
    let hs = horizons(&spec.horizons_ns, spec.h_max_ns)?;
    let latency = LatencyModel::default();
    let mut warnings = Vec::new();
    let groups = split(&ds, specs, &BTreeMap::new(), &latency, spec.seed, &mut warnings)?;
    let mut jobs = Vec::new();
    for p in ds.partitions() {
        let Some(ShadowGroup {
            specs: sp, inputs: sh, ..
        }) = groups.get(&(p.session_date, p.instrument_id))
        else {
            continue;
        };
        let inst = ds.instrument(p.instrument_id, p.session_date);
        let mut ksp = Vec::new();
        let mut ksh = Vec::new();
        for (a, b) in sp.iter().zip(sh) {
            if in_window(&spec.train, p.session_date, b.ts_live, inst)? {
                ksp.push(a.clone());
                ksh.push(b.clone());
            }
        }
        if !ksp.is_empty() {
            jobs.push((p, ksp, ksh));
        }
    }
    let id = ModelId::new(
        Family::DatabentoMc,
        &[("k", -0.8), ("particles", f64::from(spec.particles))],
        mode,
        Exchange::Partial,
    )?;
    // Ingest-recorded feed gaps and trading halts censor the exact truth as in replay.
    let dq = qt_replay::ingest_dq(&ds);
    let gaps = dq.as_ref().map(|d| d.feed_gaps.clone()).unwrap_or_default();
    let halts = dq.as_ref().map(qt_replay::halts_of).unwrap_or_default();
    let results = par_map(&jobs, spec.threads as usize, |(part, sp, sh)| {
        let inst = ds
            .instrument(part.instrument_id, part.session_date)
            .cloned()
            .ok_or_else(|| QtError::Config(format!("no instrument row for partition {}", part.instrument_id)))?;
        let cfg = SimConfig {
            horizons: hs.clone(),
            h_max: spec.h_max_ns,
            tick: inst.tick_size.max(1),
            lot: (inst.lot_size.max(1)) as f64,
            cadence0: spec.l2.cadence_ns == 0,
            seed: spec.seed,
            write_trajectory: false,
            trace: false,
        };
        let lot = cfg.lot;
        let midnight = inst.midnight_utc_ns;
        let job = PartJob {
            part,
            inst,
            specs: sp.clone(),
            shadows: sh.clone(),
            opts: StreamOptions {
                feed_gaps: qt_replay::partition_gaps(&gaps, part),
                trading_halts: qt_replay::partition_halts(&halts, part, Some(midnight)),
                ..StreamOptions::default()
            },
        };
        let r = run_partition(
            &ds,
            job,
            std::slice::from_ref(&id),
            &cfg,
            &spec.l2,
            latency,
            spec.seed,
            true,
        )?;
        Ok((r, lot))
    })?;
    let mut shadows = Vec::new();
    for (r, lot) in results {
        for (f, ops) in r.facts.into_iter().zip(r.tapes) {
            let tr = f.truth.unwrap_or_default();
            let interval = |t: Ts| hs.iter().position(|h| t <= f.ts_live.saturating_add(*h));
            let truth_interval = tr.ts_first_fill.and_then(interval);
            let c = f.admin_end;
            let censored_from = hs.iter().position(|h| f.ts_live.saturating_add(*h) > c);
            let qty = f.qty as f64;
            shadows.push(TapeShadow {
                shadow_id: f.shadow_id,
                qty,
                lot,
                ts_live: f.ts_live,
                ops,
                truth_interval,
                censored_from,
            });
        }
    }
    Ok(Box::new(DbSimulator {
        horizons: hs,
        mode,
        particles: spec.particles,
        threads: spec.threads.max(1) as usize,
        shadows,
    }))
}
