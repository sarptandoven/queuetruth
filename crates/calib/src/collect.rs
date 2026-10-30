//! Calibration input collection (SPEC 5.4): one pass per partition feeds every MBO record to the exact `Engine`
//! (shadows, truth observer) and to the `L2Builder`; each emitted L2 batch is applied to a `VisibleBook` and to an
//! NT `queue_position` tracker per shadow (5.3.3 d, via `qt_zoo`).
//!
//! `qt.alloc_events`: one row per (shadow, exact event) with `cancel_ahead + cancel_behind > 0`; `front =
//! ahead_before`, `back = level_qty_before - ahead_before`; `l2_prev`/`l2_new`/`l2_trades` describe the shadow's level
//! in the L2 batch emitted at that event (null when no batch was emitted there or the level is out of view).
//!
//! `qt.touch_records`: one row per displayed at-price trade row with the aggressor opposite to the shadow while the
//! shadow is live in both arms (L2 activation = first batch with `ts_event > ts_live`). `nt_front_before` is the
//! tracker's queue ahead before the trade, `nt_excess` its excess fill (`min(remaining, leaves)`, leaves = qty; fills
//! are never applied to the tracker so its `F` path is deterministic). Exact truth per touch (cadence 0): with
//! `qa = ahead_before - cancel_ahead` of the event and the at-price volume accumulated over the event's touches, a
//! touch fills when it carries the cumulative volume past `qa` (capped at the remaining quantity);
//! `truth_first_fill_here` marks that touch in the event of the exact first fill when its reason is
//! `queue_depleted`. Rows stop after the exact first-fill event, so a shadow first filled any other way
//! (trade-through, cross, cancel-driven depletion) keeps only the touches of strictly earlier events (the touch rows
//! of the first-fill event itself are dropped: "strictly before" is per event, E28 / E38) and is censored there by
//! C3 (audit ruling A7).

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

use qt_ingest::{Dataset, Partition};
use qt_l2::{L2Builder, VisibleBook};
use qt_replay::{Engine, EngineConfig, EventEnd, EventObserver, ShadowEventTruth, ShadowStatus, Sink};
use qt_schema::json::FeedGap;
use qt_schema::rows::{
    AllocEventRow, EndState, FillReason, L2Kind, L2Row, ShadowFillRow, ShadowSpec, TouchRecordRow, l2flags,
};
use qt_schema::specs;
use qt_types::{
    BookSide, Instrument, MboEvent, Px, QtError, QtResult, Side, Ts, VenueRules, Xoshiro256StarStar, stable_seed,
};
use qt_zoo::{FillDelta, ModelId, QState, QueueModel, ShadowL2};

/// Distance-from-touch bucket of an allocation event (0, 1, 2+ ticks; 255 = no same-side best).
pub type DistBucket = u8;

/// Shadow that went live, with what the window and block rules need.
#[derive(Clone, Debug, PartialEq)]
pub struct LiveShadow {
    /// Shadow id.
    pub shadow_id: String,
    /// Instrument.
    pub instrument_id: u32,
    /// Session date.
    pub session_date: i32,
    /// Target activation time.
    pub ts_live: Ts,
}

/// Collected rows with side information (aligned with the rows).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Collected {
    /// `qt.alloc_events` rows.
    pub alloc: Vec<AllocEventRow>,
    /// Distance bucket per alloc row.
    pub alloc_dist: Vec<DistBucket>,
    /// `qt.touch_records` rows.
    pub touch: Vec<TouchRecordRow>,
    /// Shadows that went live, in shadow order.
    pub shadows: Vec<LiveShadow>,
}

impl Collected {
    fn append(&mut self, o: Collected) {
        self.alloc.extend(o.alloc);
        self.alloc_dist.extend(o.alloc_dist);
        self.touch.extend(o.touch);
        self.shadows.extend(o.shadows);
    }
}

/// Settings of one collection pass.
#[derive(Clone, Debug)]
pub struct CollectConfig {
    /// L2 view.
    pub l2: qt_l2::L2Config,
    /// Engine seed (latency jitter) and tracker RNG seed.
    pub seed: u64,
    /// Faithful mode (hidden trades reach the NT tracker as aggressor N).
    pub faithful: bool,
}

#[derive(Clone)]
struct EvTruth {
    sequence: u64,
    ts_event: Ts,
    best: [Option<Px>; 2],
    truths: Vec<ShadowEventTruth>,
}

#[derive(Clone, Default)]
struct TruthBuf(Arc<Mutex<Vec<EvTruth>>>);

impl TruthBuf {
    fn drain(&self) -> Vec<EvTruth> {
        self.0.lock().map(|mut v| std::mem::take(&mut *v)).unwrap_or_default()
    }
}

struct TruthObserver(TruthBuf);

impl EventObserver for TruthObserver {
    fn on_event_end(&mut self, end: &EventEnd<'_>) {
        if end.shadow_truth.is_empty() {
            return;
        }
        let best = [
            end.book.best(BookSide::Bid).map(|l| l.price),
            end.book.best(BookSide::Ask).map(|l| l.price),
        ];
        if let Ok(mut v) = (self.0).0.lock() {
            v.push(EvTruth {
                sequence: end.sequence,
                ts_event: end.ts_event,
                best,
                truths: end.shadow_truth.to_vec(),
            });
        }
    }
}

/// Keeps the first exact fill of every shadow.
#[derive(Default)]
struct FirstFillSink {
    first: BTreeMap<String, (u64, FillReason)>,
    recent: Vec<String>,
}

impl Sink for FirstFillSink {
    fn shadow_fill(&mut self, r: &ShadowFillRow) -> QtResult<()> {
        if r.fill_seq == 1 && !self.first.contains_key(&r.shadow_id) {
            self.first.insert(r.shadow_id.clone(), (r.sequence, r.reason));
            self.recent.push(r.shadow_id.clone());
        }
        Ok(())
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Pending,
    Live,
    Done,
}

struct Pair {
    idx: u32,
    spec_pos: usize,
    side: BookSide,
    qty: f64,
    ts_live: Ts,
    price: Option<Px>,
    phase: Phase,
    state: QState,
    rng: Xoshiro256StarStar,
    touches: u32,
    rows: Vec<TouchRecordRow>,
    /// Exact event (`sequence`) during which each row of `rows` was recorded.
    row_event: Vec<u64>,
    exact_filled: u64,
    first_fill_seen: bool,
    went_live: bool,
}

const fn si(s: BookSide) -> usize {
    match s {
        BookSide::Bid => 0,
        BookSide::Ask => 1,
    }
}

fn sign(s: BookSide) -> i64 {
    match s {
        BookSide::Bid => 1,
        BookSide::Ask => -1,
    }
}

/// The NT `queue_position` tracker id used for touch records.
pub fn nt_tracker_id(mode: specs::ModeName) -> QtResult<ModelId> {
    let s = match mode {
        specs::ModeName::Faithful => "nt_queue_l2[mode=faithful]",
        specs::ModeName::Corrected => "nt_queue_l2[mode=corrected]",
    };
    s.parse()
}

/// Collects one instrument partition from its MBO records (feed order), without ingest feed gaps (in-memory
/// records; `collect_events_with_gaps` for a dataset partition).
pub fn collect_events(
    events: impl Iterator<Item = QtResult<MboEvent>>,
    inst: &Instrument,
    rules: VenueRules,
    shadows: Vec<ShadowSpec>,
    nt: &dyn QueueModel,
    cfg: &CollectConfig,
) -> QtResult<Collected> {
    collect_events_with_gaps(events, inst, rules, shadows, nt, cfg, &[])
}

/// `collect_events` with the ingest-recorded feed gaps of the partition (`qt_replay::partition_gaps`), set on the
/// exact engine exactly as `replay` does: shadows live across a gap end `censored_gap` and leave the collection there.
#[allow(clippy::too_many_arguments)]
pub fn collect_events_with_gaps(
    events: impl Iterator<Item = QtResult<MboEvent>>,
    inst: &Instrument,
    rules: VenueRules,
    shadows: Vec<ShadowSpec>,
    nt: &dyn QueueModel,
    cfg: &CollectConfig,
    feed_gaps: &[FeedGap],
) -> QtResult<Collected> {
    collect_events_with_spans(events, inst, rules, shadows, nt, cfg, feed_gaps, &[])
}

/// `collect_events_with_gaps` with the ingest-recorded trading halts of the partition too
/// (`qt_replay::partition_halts`, SPEC 1.2), set on the exact engine exactly as `replay` does: a shadow live at a
/// halt ends `censored_gap` there, one going live in a halt never goes live.
#[allow(clippy::too_many_arguments)]
pub fn collect_events_with_spans(
    events: impl Iterator<Item = QtResult<MboEvent>>,
    inst: &Instrument,
    rules: VenueRules,
    shadows: Vec<ShadowSpec>,
    nt: &dyn QueueModel,
    cfg: &CollectConfig,
    feed_gaps: &[FeedGap],
    trading_halts: &[qt_replay::TradingHalt],
) -> QtResult<Collected> {
    let mut ecfg = EngineConfig::new(rules);
    ecfg.seed = cfg.seed;
    ecfg.bbo = qt_replay::BboMode::None;
    ecfg.trajectory = qt_replay::TrajectoryMode::None;
    let ids: Vec<String> = shadows.iter().map(|s| s.shadow_id.clone()).collect();
    let specs_by_pos = shadows.clone();
    let mut engine = Engine::new(ecfg, std::slice::from_ref(inst), shadows, Vec::new())?;
    // as qt_replay::run_dataset: sequence gaps are an ingest-time property, not detected per partition (a multi-instrument
    // feed's global sequence jumps between instruments);
    // the partition's ingest-recorded gaps censor the shadows live across them
    engine.set_sequence_gap_detection(false);
    engine.set_feed_gaps(feed_gaps);
    engine.set_trading_halts(trading_halts)?;
    let buf = TruthBuf::default();
    engine.add_observer(Box::new(TruthObserver(buf.clone())));
    let model_id = nt.id().to_string();
    let lot = (inst.lot_size.max(1)) as f64;
    let tick = inst.tick_size.max(1);

    let mut pairs: Vec<Pair> = Vec::with_capacity(ids.len());
    for (pos, id) in ids.iter().enumerate() {
        let idx = engine
            .shadow_index(id)
            .ok_or_else(|| QtError::Engine(format!("calibration: shadow {id} not registered in the engine")))?;
        let st = engine.shadow_state(idx);
        pairs.push(Pair {
            idx,
            spec_pos: pos,
            side: specs_by_pos[pos].side,
            qty: specs_by_pos[pos].qty as f64,
            ts_live: st.ts_live,
            price: None,
            phase: Phase::Pending,
            state: QState::default(),
            rng: Xoshiro256StarStar::new(stable_seed(cfg.seed, &[model_id.as_bytes(), id.as_bytes()])),
            touches: 0,
            rows: Vec::new(),
            row_event: Vec::new(),
            exact_filled: 0,
            first_fill_seen: false,
            went_live: false,
        });
    }
    let by_idx: BTreeMap<u32, usize> = pairs.iter().enumerate().map(|(i, p)| (p.idx, i)).collect();
    let by_id: BTreeMap<&str, usize> = ids.iter().enumerate().map(|(i, s)| (s.as_str(), i)).collect();
    let mut pending: Vec<usize> = (0..pairs.len()).collect();
    pending.sort_by_key(|&i| (pairs[i].ts_live, i));
    let mut pending_cursor = 0usize;
    // Live L2 pairs by (side, price).
    let mut at_level: BTreeMap<(usize, Px), BTreeSet<usize>> = BTreeMap::new();

    let mut l2 = L2Builder::new(cfg.l2, rules, inst.instrument_id);
    let mut vb = VisibleBook::new();
    let mut sink = FirstFillSink::default();
    let mut rows: Vec<L2Row> = Vec::new();
    let mut out = Collected::default();

    for e in events {
        let e = e?;
        if e.instrument_id != inst.instrument_id {
            continue;
        }
        engine.apply(&e, &mut sink)?;
        rows.clear();
        l2.push(&e, &mut rows);
        if !e.is_last() && rows.is_empty() {
            continue;
        }
        let evs = buf.drain();
        let cur = evs.last().filter(|ev| ev.sequence == e.sequence);
        let batch_here = !rows.is_empty();

        // Allocation rows of every drained event.
        for ev in &evs {
            let same_batch = batch_here && ev.sequence == e.sequence;
            for t in ev.truths.iter().filter(|t| t.cancel_ahead + t.cancel_behind > 0) {
                let Some(&pi) = by_idx.get(&t.shadow_idx) else { continue };
                let p = &mut pairs[pi];
                if p.price.is_none() {
                    p.price = engine.shadow_state(p.idx).price;
                }
                let Some(price) = p.price else { continue };
                let (l2_prev, l2_new, l2_trades) = if same_batch {
                    level_view(&rows, &vb, p.side, price)
                } else {
                    (None, None, None)
                };
                let dist = match ev.best[si(p.side)] {
                    None => 255,
                    Some(b) => (sign(p.side) * (b - price) / tick).clamp(0, 2) as u8,
                };
                out.alloc.push(AllocEventRow {
                    shadow_id: ids[p.spec_pos].clone(),
                    instrument_id: inst.instrument_id,
                    session_date: inst.session_date,
                    ts_event: ev.ts_event,
                    front: t.ahead_before as f64,
                    back: t.level_qty_before as f64 - t.ahead_before as f64,
                    ahead_cancel: t.cancel_ahead as f64,
                    behind_cancel: t.cancel_behind as f64,
                    l2_prev,
                    l2_new,
                    l2_trades,
                });
                out.alloc_dist.push(dist);
            }
        }
        let truth_of: BTreeMap<u32, &ShadowEventTruth> = cur
            .map(|ev| ev.truths.iter().map(|t| (t.shadow_idx, t)).collect())
            .unwrap_or_default();

        if batch_here {
            let batch_ts = e.ts_event;
            // L2 activation: first batch with ts_event > ts_live, arriving with the visible quantity before the batch.
            while pending_cursor < pending.len() && pairs[pending[pending_cursor]].ts_live < batch_ts {
                let i = pending[pending_cursor];
                pending_cursor += 1;
                let p = &mut pairs[i];
                let st = engine.shadow_state(p.idx);
                p.price = st.price;
                let Some(price) = p.price else {
                    p.phase = Phase::Done;
                    continue;
                };
                if st.status == ShadowStatus::Done && !truth_of.contains_key(&p.idx) {
                    p.phase = Phase::Done;
                    continue;
                }
                p.state = QState {
                    leaves: p.qty,
                    ..QState::default()
                };
                let o = ShadowL2 {
                    idx: p.idx,
                    side: p.side,
                    price,
                    qty: p.qty,
                    lot,
                };
                nt.on_arrive(&mut p.state, &o, vb.qty_at(p.side, price) as f64, &mut p.rng);
                p.phase = Phase::Live;
                p.went_live = true;
                at_level.entry((si(p.side), price)).or_default().insert(i);
            }
            // Pairs that ended at this event's start (cancel / amend) see none of its rows.
            let mut ended_at_start = Vec::new();
            for set in at_level.values() {
                for &i in set {
                    let p = &pairs[i];
                    let early = match truth_of.get(&p.idx) {
                        Some(t) => matches!(t.ended_now, Some(EndState::Cancelled | EndState::AmendedToFilled)),
                        None => engine.shadow_state(p.idx).status == ShadowStatus::Done,
                    };
                    if early {
                        ended_at_start.push(i);
                    }
                }
            }
            for i in ended_at_start {
                retire(&mut pairs, &mut at_level, i);
            }
            // First-fill bookkeeping of this event.
            let mut cum: BTreeMap<usize, f64> = BTreeMap::new();
            let mut first_here: BTreeSet<usize> = BTreeSet::new();
            for row in &rows {
                vb.apply(row);
                let Some(px) = row.price else { continue };
                match row.kind {
                    L2Kind::Trade => {
                        let hidden = row.flags & l2flags::HIDDEN_TRADE != 0;
                        let targets: Vec<usize> = if hidden {
                            if !cfg.faithful {
                                continue;
                            }
                            [0, 1]
                                .iter()
                                .filter_map(|s| at_level.get(&(*s, px)))
                                .flatten()
                                .copied()
                                .collect()
                        } else {
                            match BookSide::from_side(row.side) {
                                Some(hit) => at_level
                                    .get(&(si(hit), px))
                                    .map(|s| s.iter().copied().collect())
                                    .unwrap_or_default(),
                                None => Vec::new(),
                            }
                        };
                        for i in targets {
                            let p = &mut pairs[i];
                            let o = ShadowL2 {
                                idx: p.idx,
                                side: p.side,
                                price: px,
                                qty: p.qty,
                                lot,
                            };
                            let v = row.trade_qty as f64;
                            if hidden {
                                let _ = nt.on_trade(&mut p.state, &o, row.ts_event, v, Side::None, &mut p.rng);
                                continue;
                            }
                            if row.aggressor != p.side.opposite().to_side() {
                                continue;
                            }
                            // C3 only needs touches up to the exact first-fill event.
                            if p.first_fill_seen
                                || sink
                                    .first
                                    .get(&ids[p.spec_pos])
                                    .is_some_and(|(seq, _)| *seq != e.sequence)
                            {
                                continue;
                            }
                            let front = nt.queue_ahead(&p.state);
                            let fd = nt.on_trade(&mut p.state, &o, row.ts_event, v, row.aggressor, &mut p.rng);
                            let excess = match fd {
                                FillDelta::None => 0.0,
                                FillDelta::Qty(q) => q.max(0.0),
                                FillDelta::Full => p.qty,
                            };
                            p.touches += 1;
                            let c0 = cum.get(&i).copied().unwrap_or(0.0);
                            let c1 = c0 + v;
                            cum.insert(i, c1);
                            let (filled_now, first) = match truth_of.get(&p.idx) {
                                Some(t) if t.filled_now > 0 => {
                                    let qa = t.ahead_before.saturating_sub(t.cancel_ahead) as f64;
                                    let qty_now = engine.shadow_state(p.idx).qty;
                                    let rem = qty_now.saturating_sub(p.exact_filled) as f64;
                                    let dep = (c1 - qa).clamp(0.0, rem) - (c0 - qa).clamp(0.0, rem);
                                    let is_first_event = !p.first_fill_seen
                                        && sink
                                            .first
                                            .get(&ids[p.spec_pos])
                                            .is_some_and(|(seq, _)| *seq == e.sequence);
                                    let first = dep > 0.0 && is_first_event && !first_here.contains(&i);
                                    (dep > 0.0, first)
                                }
                                _ => (false, false),
                            };
                            if first {
                                first_here.insert(i);
                            }
                            p.row_event.push(e.sequence);
                            p.rows.push(TouchRecordRow {
                                shadow_id: ids[p.spec_pos].clone(),
                                ts_event: row.ts_event,
                                touch_index: p.touches,
                                nt_front_before: front,
                                nt_excess: excess,
                                truth_filled_now: filled_now,
                                truth_first_fill_here: first,
                            });
                        }
                    }
                    L2Kind::Depth | L2Kind::Snapshot => {
                        let Some(side) = BookSide::from_side(row.side) else {
                            continue;
                        };
                        let targets: Vec<usize> = at_level
                            .get(&(si(side), px))
                            .map(|s| s.iter().copied().collect())
                            .unwrap_or_default();
                        for i in targets {
                            let p = &mut pairs[i];
                            let o = ShadowL2 {
                                idx: p.idx,
                                side: p.side,
                                price: px,
                                qty: p.qty,
                                lot,
                            };
                            let _ = nt.on_depth(
                                &mut p.state,
                                &o,
                                row.ts_event,
                                row.prev_qty as f64,
                                row.new_qty as f64,
                                row.flags,
                                &mut p.rng,
                            );
                        }
                    }
                    L2Kind::Clear => {}
                }
            }
        }
        // First fills of this event end the touch record. A first fill not carried by a touch of this event
        // (trade-through, cross, or a queue-depleted fill without a touch row) is censored by C3 with M_i = the
        // touches of strictly earlier EVENTS (E28 / E38: "strictly before" is per event, the record batch ending in
        // LAST), so the touch rows recorded during this event are dropped.
        for id in std::mem::take(&mut sink.recent) {
            let Some(&i) = by_id.get(id.as_str()) else { continue };
            let p = &mut pairs[i];
            if p.first_fill_seen {
                continue;
            }
            p.first_fill_seen = true;
            let carried = sink
                .first
                .get(&id)
                .is_some_and(|(seq, reason)| *seq == e.sequence && *reason == FillReason::QueueDepleted)
                && p.rows
                    .iter()
                    .zip(&p.row_event)
                    .any(|(r, ev)| *ev == e.sequence && r.truth_first_fill_here);
            if !carried {
                let keep = p.row_event.iter().take_while(|ev| **ev != e.sequence).count();
                p.rows.truncate(keep);
                p.row_event.truncate(keep);
            }
        }
        // Exact fill accounting and retirement after the event.
        for (idx, t) in &truth_of {
            if let Some(&i) = by_idx.get(idx) {
                pairs[i].exact_filled += t.filled_now;
            }
        }
        let done: Vec<usize> = at_level
            .values()
            .flatten()
            .copied()
            .filter(|&i| engine.shadow_state(pairs[i].idx).status == ShadowStatus::Done)
            .collect();
        for i in done {
            retire(&mut pairs, &mut at_level, i);
        }
    }
    let mut fin = FirstFillSink::default();
    engine.finish(&mut fin)?;
    let _ = buf.drain();
    for p in pairs {
        if !p.went_live {
            continue;
        }
        out.shadows.push(LiveShadow {
            shadow_id: ids[p.spec_pos].clone(),
            instrument_id: inst.instrument_id,
            session_date: inst.session_date,
            ts_live: p.ts_live,
        });
        out.touch.extend(p.rows);
    }
    Ok(out)
}

fn retire(pairs: &mut [Pair], at_level: &mut BTreeMap<(usize, Px), BTreeSet<usize>>, i: usize) {
    let p = &mut pairs[i];
    p.phase = Phase::Done;
    if let Some(px) = p.price {
        let key = (si(p.side), px);
        if let Some(s) = at_level.get_mut(&key) {
            s.remove(&i);
            if s.is_empty() {
                at_level.remove(&key);
            }
        }
    }
}

/// (prev, new, at-price opposite-aggressor displayed trades) of a level in one batch; None when out of view.
fn level_view(rows: &[L2Row], vb: &VisibleBook, side: BookSide, price: Px) -> (Option<f64>, Option<f64>, Option<f64>) {
    let mut prev = None;
    let mut new = None;
    let mut trades = 0.0;
    for r in rows {
        if r.price != Some(price) {
            continue;
        }
        match r.kind {
            L2Kind::Depth | L2Kind::Snapshot if BookSide::from_side(r.side) == Some(side) => {
                if prev.is_none() {
                    prev = Some(r.prev_qty as f64);
                }
                new = Some(r.new_qty as f64);
            }
            L2Kind::Trade
                if r.flags & l2flags::HIDDEN_TRADE == 0
                    && BookSide::from_side(r.side) == Some(side)
                    && r.aggressor == side.opposite().to_side() =>
            {
                trades += r.trade_qty as f64;
            }
            _ => {}
        }
    }
    match (prev, new) {
        (Some(p), Some(n)) => (Some(p), Some(n), Some(trades)),
        _ if vb.in_view(side, price) => {
            // The batch did not touch the level (the view is read before the batch is applied).
            let q = vb.qty_at(side, price) as f64;
            (Some(q), Some(q), Some(trades))
        }
        _ => (None, None, None),
    }
}

/// Partition-level collection on a dataset (with the dataset's ingest-recorded feed gaps and trading halts, as
/// `replay`).
pub fn collect_partition(
    part: &Partition,
    ds: &Dataset,
    shadows: Vec<ShadowSpec>,
    nt: &dyn QueueModel,
    cfg: &CollectConfig,
) -> QtResult<Collected> {
    let dq = qt_replay::ingest_dq(ds);
    let gaps = dq.as_ref().map(|d| d.feed_gaps.clone()).unwrap_or_default();
    let halts = dq.as_ref().map(qt_replay::halts_of).unwrap_or_default();
    collect_partition_gaps(part, ds, shadows, nt, cfg, &gaps, &halts)
}

fn collect_partition_gaps(
    part: &Partition,
    ds: &Dataset,
    shadows: Vec<ShadowSpec>,
    nt: &dyn QueueModel,
    cfg: &CollectConfig,
    gaps: &[FeedGap],
    halts: &[qt_replay::TradingHalt],
) -> QtResult<Collected> {
    let inst = ds
        .instrument(part.instrument_id, part.session_date)
        .ok_or_else(|| QtError::InputFormat {
            location: format!("instruments.parquet: {}/{}", part.instrument_id, part.session_date),
            msg: "partition without an instrument row".into(),
        })?;
    let part_gaps = qt_replay::partition_gaps(gaps, part);
    let part_halts = qt_replay::partition_halts(halts, part, Some(inst.midnight_utc_ns));
    collect_events_with_spans(part.mbo()?, inst, ds.rules(), shadows, nt, cfg, &part_gaps, &part_halts)
}

/// Collects every partition that has shadows, in partition order.
pub fn collect_dataset(
    ds: &Dataset,
    shadows: Vec<ShadowSpec>,
    nt: &dyn QueueModel,
    cfg: &CollectConfig,
    threads: usize,
) -> QtResult<Collected> {
    let mut by_part = qt_replay::split_shadows(ds, shadows)?;
    let parts: Vec<Partition> = ds
        .partitions()
        .iter()
        .filter(|p| by_part.contains_key(&(p.session_date, p.instrument_id)))
        .cloned()
        .collect();
    let jobs: BTreeMap<(i32, u32), Mutex<Vec<ShadowSpec>>> = std::mem::take(&mut by_part)
        .into_iter()
        .map(|(k, v)| (k, Mutex::new(v)))
        .collect();
    let dq = qt_replay::ingest_dq(ds);
    let gaps = dq.as_ref().map(|d| d.feed_gaps.clone()).unwrap_or_default();
    let halts = dq.as_ref().map(qt_replay::halts_of).unwrap_or_default();
    let results = qt_replay::for_each_partition(&parts, threads.max(1), |part| {
        let shadows = jobs
            .get(&(part.session_date, part.instrument_id))
            .and_then(|m| m.lock().ok().map(|mut v| std::mem::take(&mut *v)))
            .unwrap_or_default();
        collect_partition_gaps(part, ds, shadows, nt, cfg, &gaps, &halts)
    })?;
    let mut out = Collected::default();
    for r in results {
        out.append(r);
    }
    Ok(out)
}
