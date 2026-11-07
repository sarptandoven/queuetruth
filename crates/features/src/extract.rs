//! One-pass landmark extraction per partition (SPEC 5.5.1, 5.5.2): every MBO record goes to the exact `Engine` and
//! to the `L2Builder`; emitted batches update the `Market` and the order trackers.
//!
//! No lookahead: the exact (privileged) part of landmark `t_l` is taken at the start of the first event with
//! `ts_event > t_l` (book as left by events `<= t_l`), the L2 part just before the first batch whose `LAST` record has
//! `ts_event > t_l` is applied (trade rows inside that batch are not used even when their own `ts_event <= t_l`),
//! and the rest at end of input before any L2 flush.
//!
//! Labels (audit ruling A2: censoring only by outcome-independent ends): shadows start at `ts_live`; fill = exact
//! first fill; cancel = the policy cancel (first exact event end with the same-side best `>= cancel_away_ticks`
//! better than the price; `cancel_away_ticks = 0` disables it, so shadow labels are the exact never-cancel outcome up
//! to the shadow's own end, review F6) or an exact `cancelled` end (the order's own cancel is a competing event, never
//! censoring); censor = the other exact end states (`censored_*`, `open_at_end`) or the `h_max_ns` cap. Real orders
//! start at `ts_add`; fill = first `F`; cancel = `cancelled` / `replaced`; censor otherwise. Ties: fill, then cancel,
//! then censor. A row exists for landmark 0 of every order that went live and for `l > 0` when the order's event
//! time is after `t_l`.

use std::collections::{BTreeMap, BTreeSet};

use qt_book::Book;
use qt_l2::L2Builder;
use qt_replay::{Engine, EngineConfig, ShadowStatus, Sink};
use qt_schema::json::FeedGap;
use qt_schema::rows::{
    Cause, EndState, FeatureRow, L2Row, Population, PriceSpec, RealOrderEnd, RealOrderRow, ShadowFillRow,
    ShadowResultRow, ShadowSpec, TrajectoryKind, TrajectoryRow,
};
use qt_types::{BookSide, Dur, Instrument, MboEvent, Px, QtResult, Ts, VenueRules};
use qt_zoo::QueueModel;

use crate::market::{Market, si};
use crate::order::{OrderTrack, SnapshotCtx, snapshot};

/// Settings of one extraction pass.
#[derive(Clone, Debug)]
pub struct ExtractConfig {
    /// L2 view.
    pub l2: qt_l2::L2Config,
    /// Landmarks (ns after the order's start).
    pub landmarks_ns: Vec<Dur>,
    /// Policy-cancel distance in ticks (shadows); 0 = never cancel (`FeatureSpec.cancel_away_ticks` null or 0).
    pub cancel_away_ticks: u32,
    /// Cap of `event_ns`.
    pub h_max_ns: Dur,
    /// Write the privileged columns.
    pub privileged: bool,
    /// Seed (engine latency jitter, tracker RNG).
    pub seed: u64,
    /// Index of the `hbt_risk_adverse` tracker.
    pub ra_index: Option<usize>,
}

/// One order of the population.
#[derive(Clone, Debug)]
pub enum PopOrder {
    /// A shadow order.
    Shadow(ShadowSpec),
    /// A real order life.
    Real(RealOrderRow),
}

#[derive(Clone, Debug)]
struct Lm {
    l: Dur,
    t: Ts,
    own_qty: Option<f64>,
    privileged: [Option<f64>; 3],
    feats: Option<Vec<Option<f64>>>,
}

struct Ord {
    order_ref: String,
    side: BookSide,
    price: Option<Px>,
    qty: f64,
    start: Ts,
    idx: Option<u32>,
    real: Option<RealOrderRow>,
    track: Option<OrderTrack>,
    arrive_next_batch: bool,
    indexed: bool,
    retired: bool,
    policy_cancel: Option<Ts>,
    lms: Vec<Lm>,
}

#[derive(Default)]
struct LabelSink {
    first_fill: BTreeMap<String, Ts>,
    end_ts: BTreeMap<String, Ts>,
    result: BTreeMap<String, (EndState, Option<Ts>)>,
}

impl Sink for LabelSink {
    fn shadow_fill(&mut self, r: &ShadowFillRow) -> QtResult<()> {
        if r.fill_seq == 1 {
            self.first_fill.entry(r.shadow_id.clone()).or_insert(r.ts_event);
        }
        Ok(())
    }
    fn trajectory(&mut self, r: &TrajectoryRow) -> QtResult<()> {
        if r.kind == TrajectoryKind::End {
            self.end_ts.insert(r.shadow_id.clone(), r.ts_event);
        }
        Ok(())
    }
    fn shadow_result(&mut self, r: &ShadowResultRow) -> QtResult<()> {
        self.result
            .insert(r.shadow_id.clone(), (r.end_state, r.ts_activated_event));
        Ok(())
    }
}

fn level_of(book: Option<&Book>, side: BookSide, px: Px) -> (f64, f64) {
    book.and_then(|b| b.level(side, px))
        .map_or((0.0, 0.0), |l| (l.total as f64, f64::from(l.count)))
}

/// Price a pending touch-mode shadow would resolve to on `book` (SPEC 5.2.2, rejections not applied).
fn resolve_price(spec: &ShadowSpec, book: Option<&Book>, tick: Px) -> Option<Px> {
    match spec.price {
        PriceSpec::Abs(p) => Some(p),
        PriceSpec::Touch { offset_ticks } => {
            let b = book?.best(spec.side)?.price;
            let d = i64::from(offset_ticks) * tick;
            Some(match spec.side {
                BookSide::Bid => b + d,
                BookSide::Ask => b - d,
            })
        }
    }
}

struct Driver<'a> {
    inst: &'a Instrument,
    models: &'a [Box<dyn QueueModel>],
    cfg: &'a ExtractConfig,
    ords: Vec<Ord>,
    specs: Vec<Option<ShadowSpec>>,
    exact_q: BTreeSet<(Ts, usize, usize)>,
    l2_q: BTreeSet<(Ts, usize, usize)>,
    market: Market,
    at_level: BTreeMap<(usize, Px), BTreeSet<usize>>,
    real_by_id: BTreeMap<u64, Vec<usize>>,
}

impl Driver<'_> {
    fn exact_snapshot(&mut self, upto: Option<Ts>, engine: &Engine) {
        while let Some(&(t, oi, li)) = self.exact_q.first() {
            if upto.is_some_and(|u| t >= u) {
                break;
            }
            self.exact_q.pop_first();
            let book = engine.book(self.inst.instrument_id);
            let o = &self.ords[oi];
            let (own, pv): (Option<f64>, [Option<f64>; 3]) = match (&o.real, o.idx) {
                (Some(r), _) => match book.and_then(|b| b.order(r.order_id)) {
                    Some((s, p, ro)) if s == r.side && p == r.price => {
                        let own = ro.size as f64;
                        let q = book.and_then(|b| b.queue_ahead_of(r.order_id));
                        let (lvl, _) = level_of(book, s, p);
                        (
                            Some(own),
                            [
                                q.map(|x| x.0 as f64),
                                q.map(|x| f64::from(x.1)),
                                Some((lvl - own).max(0.0)),
                            ],
                        )
                    }
                    _ => (None, [None; 3]),
                },
                (None, Some(idx)) => {
                    let st = engine.shadow_state(idx);
                    match st.status {
                        ShadowStatus::Live => (
                            Some(st.qty.saturating_sub(st.filled) as f64),
                            [
                                st.queue_ahead.map(|x| x as f64),
                                st.rank.map(f64::from),
                                st.level_qty.map(|x| x as f64),
                            ],
                        ),
                        ShadowStatus::Pending => {
                            let spec = self.specs[oi].as_ref();
                            match spec.and_then(|s| resolve_price(s, book, self.inst.tick_size.max(1))) {
                                Some(p) => {
                                    let (tot, n) = level_of(book, o.side, p);
                                    (Some(o.qty), [Some(tot), Some(n), Some(tot)])
                                }
                                None => (Some(o.qty), [None; 3]),
                            }
                        }
                        ShadowStatus::Done => (None, [None; 3]),
                    }
                }
                (None, None) => (None, [None; 3]),
            };
            let lm = &mut self.ords[oi].lms[li];
            lm.own_qty = own;
            lm.privileged = pv;
        }
    }

    fn ensure_track(&mut self, oi: usize, engine: &Engine) -> bool {
        let o = &mut self.ords[oi];
        if o.track.is_some() {
            return true;
        }
        if o.price.is_none()
            && let Some(idx) = o.idx
        {
            o.price = engine.shadow_state(idx).price;
        }
        let Some(price) = o.price else { return false };
        let lot = self.inst.lot_size.max(1) as f64;
        o.track = Some(OrderTrack::new(
            o.side,
            price,
            o.qty,
            lot,
            o.start,
            self.models,
            self.cfg.seed,
            &o.order_ref,
        ));
        true
    }

    fn l2_snapshot(&mut self, upto: Option<Ts>, engine: &Engine) {
        while let Some(&(t, oi, li)) = self.l2_q.first() {
            if upto.is_some_and(|u| t >= u) {
                break;
            }
            self.l2_q.pop_first();
            if !self.ensure_track(oi, engine) {
                continue;
            }
            let own_qty = self.ords[oi].lms[li].own_qty.unwrap_or(self.ords[oi].qty);
            let Some(track) = self.ords[oi].track.as_ref() else {
                continue;
            };
            let ctx = SnapshotCtx {
                inst: self.inst,
                t,
                own_qty,
                ra_index: self.cfg.ra_index,
                include_counts: self.cfg.l2.include_counts,
            };
            let f = snapshot(&self.market, track, self.models, &ctx);
            self.ords[oi].lms[li].feats = Some(f);
        }
    }

    fn arrive(&mut self, oi: usize, engine: &Engine) {
        if !self.ensure_track(oi, engine) {
            self.ords[oi].retired = true;
            return;
        }
        let o = &mut self.ords[oi];
        let Some(track) = o.track.as_mut() else { return };
        let visible = self.market.vb.qty_at(track.side, track.price) as f64;
        track.arrive(self.models, visible);
        self.at_level
            .entry((si(track.side), track.price))
            .or_default()
            .insert(oi);
        o.indexed = true;
    }

    fn retire(&mut self, oi: usize) {
        let o = &mut self.ords[oi];
        o.retired = true;
        if o.indexed
            && let Some(t) = o.track.as_ref()
        {
            let key = (si(t.side), t.price);
            if let Some(s) = self.at_level.get_mut(&key) {
                s.remove(&oi);
                if s.is_empty() {
                    self.at_level.remove(&key);
                }
            }
            o.indexed = false;
        }
    }

    fn apply_batch(&mut self, rows: &[L2Row], engine: &Engine) {
        let summary = self.market.apply_batch(rows);
        let book = engine.book(self.inst.instrument_id);
        for r in rows {
            let Some(px) = r.price else { continue };
            for s in 0..2usize {
                let Some(set) = self.at_level.get(&(s, px)) else {
                    continue;
                };
                let ids: Vec<usize> = set.iter().copied().collect();
                for oi in ids {
                    let own_after = match &self.ords[oi].real {
                        Some(ro) => book
                            .and_then(|b| b.order(ro.order_id))
                            .filter(|(sd, p, _)| *sd == ro.side && *p == ro.price)
                            .map_or(0.0, |(_, _, o)| o.size as f64),
                        None => 0.0,
                    };
                    if let Some(t) = self.ords[oi].track.as_mut() {
                        t.on_row(self.models, r, own_after);
                    }
                }
            }
        }
        for (key, b) in &summary {
            if let Some(set) = self.at_level.get(key) {
                for &oi in set {
                    if let Some(t) = self.ords[oi].track.as_mut() {
                        t.on_level_batch(b);
                    }
                }
            }
        }
    }
}

/// Feature rows of one instrument partition (orders all on `inst`), rows in population order then landmark order;
/// without ingest feed gaps (in-memory records; `extract_events_with_gaps` for a dataset partition).
pub fn extract_events(
    events: impl Iterator<Item = QtResult<MboEvent>>,
    inst: &Instrument,
    rules: VenueRules,
    population: Vec<PopOrder>,
    models: &[Box<dyn QueueModel>],
    cfg: &ExtractConfig,
) -> QtResult<Vec<FeatureRow>> {
    extract_events_with_gaps(events, inst, rules, population, models, cfg, &[])
}

/// `extract_events` with the ingest-recorded feed gaps of the partition (`qt_replay::partition_gaps`), set on the
/// exact engine exactly as `replay` does: a shadow live across a gap ends `censored_gap` there (label censor).
pub fn extract_events_with_gaps(
    events: impl Iterator<Item = QtResult<MboEvent>>,
    inst: &Instrument,
    rules: VenueRules,
    population: Vec<PopOrder>,
    models: &[Box<dyn QueueModel>],
    cfg: &ExtractConfig,
    feed_gaps: &[FeedGap],
) -> QtResult<Vec<FeatureRow>> {
    extract_events_with_spans(events, inst, rules, population, models, cfg, feed_gaps, &[])
}

/// `extract_events_with_gaps` with the ingest-recorded trading halts of the partition too
/// (`qt_replay::partition_halts`, SPEC 1.2), set on the exact engine exactly as `replay` does: a shadow live at a
/// halt ends `censored_gap` there (label censor), one going live in a halt never goes live.
#[allow(clippy::too_many_arguments)]
pub fn extract_events_with_spans(
    events: impl Iterator<Item = QtResult<MboEvent>>,
    inst: &Instrument,
    rules: VenueRules,
    population: Vec<PopOrder>,
    models: &[Box<dyn QueueModel>],
    cfg: &ExtractConfig,
    feed_gaps: &[FeedGap],
    trading_halts: &[qt_replay::TradingHalt],
) -> QtResult<Vec<FeatureRow>> {
    let shadows: Vec<ShadowSpec> = population
        .iter()
        .filter_map(|p| match p {
            PopOrder::Shadow(s) => Some(s.clone()),
            PopOrder::Real(_) => None,
        })
        .collect();
    let mut ec = EngineConfig::new(rules);
    ec.seed = cfg.seed;
    ec.bbo = qt_replay::BboMode::None;
    ec.trajectory = qt_replay::TrajectoryMode::Ahead;
    let mut engine = Engine::new(ec, std::slice::from_ref(inst), shadows, Vec::new())?;
    // as qt_replay::run_dataset: sequence gaps are an ingest-time property, not detected per partition (a multi-instrument
    // feed's global sequence jumps between instruments);
    // the partition's ingest-recorded gaps censor the shadows live across them
    engine.set_sequence_gap_detection(false);
    engine.set_feed_gaps(feed_gaps);
    engine.set_trading_halts(trading_halts)?;
    let mut d = Driver {
        inst,
        models,
        cfg,
        ords: Vec::with_capacity(population.len()),
        specs: Vec::with_capacity(population.len()),
        exact_q: BTreeSet::new(),
        l2_q: BTreeSet::new(),
        market: Market::new(inst.tick_size),
        at_level: BTreeMap::new(),
        real_by_id: BTreeMap::new(),
    };
    for (oi, p) in population.into_iter().enumerate() {
        let (ord, spec) = match p {
            PopOrder::Shadow(s) => {
                let idx = engine.shadow_index(&s.shadow_id).ok_or_else(|| {
                    qt_types::QtError::Engine(format!("features: shadow {} not registered", s.shadow_id))
                })?;
                let st = engine.shadow_state(idx);
                let ord = Ord {
                    order_ref: s.shadow_id.clone(),
                    side: s.side,
                    price: match s.price {
                        PriceSpec::Abs(p) => Some(p),
                        PriceSpec::Touch { .. } => None,
                    },
                    qty: s.qty as f64,
                    start: st.ts_live,
                    idx: Some(idx),
                    real: None,
                    track: None,
                    arrive_next_batch: false,
                    indexed: false,
                    retired: false,
                    policy_cancel: None,
                    lms: Vec::new(),
                };
                (ord, Some(s))
            }
            PopOrder::Real(r) => {
                d.real_by_id.entry(r.order_id).or_default().push(oi);
                let ord = Ord {
                    order_ref: format!("{}:{}", r.instrument_id, r.order_id),
                    side: r.side,
                    price: Some(r.price),
                    qty: r.size_at_add as f64,
                    start: r.ts_add,
                    idx: None,
                    real: Some(r),
                    track: None,
                    arrive_next_batch: false,
                    indexed: false,
                    retired: false,
                    policy_cancel: None,
                    lms: Vec::new(),
                };
                (ord, None)
            }
        };
        d.ords.push(ord);
        d.specs.push(spec);
    }
    for oi in 0..d.ords.len() {
        let start = d.ords[oi].start;
        let lms: Vec<Lm> = cfg
            .landmarks_ns
            .iter()
            .map(|l| Lm {
                l: *l,
                t: start.saturating_add(*l),
                own_qty: None,
                privileged: [None; 3],
                feats: None,
            })
            .collect();
        for (li, lm) in lms.iter().enumerate() {
            d.exact_q.insert((lm.t, oi, li));
            d.l2_q.insert((lm.t, oi, li));
        }
        d.ords[oi].lms = lms;
    }
    let mut pending: Vec<usize> = d
        .ords
        .iter()
        .enumerate()
        .filter(|(_, o)| o.idx.is_some())
        .map(|(i, _)| i)
        .collect();
    pending.sort_by_key(|&i| (d.ords[i].start, i));
    let (mut arrive_cursor, mut watch_cursor) = (0usize, 0usize);
    let mut watch: BTreeSet<usize> = BTreeSet::new();
    let tick = inst.tick_size.max(1);
    let away = i64::from(cfg.cancel_away_ticks) * tick;

    let mut l2 = L2Builder::new(cfg.l2, rules, inst.instrument_id);
    let mut sink = LabelSink::default();
    let mut rows: Vec<L2Row> = Vec::new();
    let mut at_start = true;
    let mut real_arrivals: Vec<usize> = Vec::new();
    let mut last_ts: Ts = inst.midnight_utc_ns;
    for e in events {
        let e = e?;
        if e.instrument_id != inst.instrument_id {
            continue;
        }
        if at_start {
            // Landmarks with t_l < ts_event see the book as left by the previous event.
            d.exact_snapshot(Some(e.ts_event), &engine);
        }
        engine.apply(&e, &mut sink)?;
        at_start = e.is_last();
        last_ts = e.ts_event;
        if let Some(ois) = d.real_by_id.get(&e.order_id) {
            for &oi in ois {
                if d.ords[oi]
                    .real
                    .as_ref()
                    .is_some_and(|r| r.sequence_add == e.sequence && r.ts_add == e.ts_event)
                {
                    d.ords[oi].arrive_next_batch = true;
                    real_arrivals.push(oi);
                }
            }
        }
        rows.clear();
        l2.push(&e, &mut rows);
        if !rows.is_empty() {
            d.l2_snapshot(Some(e.ts_event), &engine);
            while arrive_cursor < pending.len() && d.ords[pending[arrive_cursor]].start < e.ts_event {
                let oi = pending[arrive_cursor];
                arrive_cursor += 1;
                let st = engine.shadow_state(d.ords[oi].idx.unwrap_or(0));
                if st.price.is_none() {
                    d.ords[oi].retired = true;
                    continue;
                }
                d.arrive(oi, &engine);
            }
            for oi in std::mem::take(&mut real_arrivals) {
                if d.ords[oi].arrive_next_batch {
                    d.ords[oi].arrive_next_batch = false;
                    d.arrive(oi, &engine);
                }
            }
            d.apply_batch(&rows, &engine);
        }
        if e.is_last() {
            // Policy cancels (shadows) and retirement after the event end.
            while watch_cursor < pending.len() && d.ords[pending[watch_cursor]].start < e.ts_event {
                watch.insert(pending[watch_cursor]);
                watch_cursor += 1;
            }
            let book = engine.book(inst.instrument_id);
            let mut gone = Vec::new();
            for &oi in &watch {
                let st = engine.shadow_state(d.ords[oi].idx.unwrap_or(0));
                match st.status {
                    ShadowStatus::Done => gone.push(oi),
                    ShadowStatus::Pending => {}
                    ShadowStatus::Live if cfg.cancel_away_ticks == 0 => {}
                    ShadowStatus::Live => {
                        let (Some(p), Some(b)) = (st.price, book.and_then(|b| b.best(d.ords[oi].side))) else {
                            continue;
                        };
                        let better = match d.ords[oi].side {
                            BookSide::Bid => b.price - p,
                            BookSide::Ask => p - b.price,
                        };
                        if better >= away {
                            d.ords[oi].policy_cancel = Some(e.ts_event);
                            gone.push(oi);
                        }
                    }
                }
            }
            for oi in gone {
                watch.remove(&oi);
                d.retire(oi);
            }
            let ended: Vec<usize> = d
                .at_level
                .values()
                .flatten()
                .copied()
                .filter(|&oi| {
                    d.ords[oi]
                        .real
                        .as_ref()
                        .is_some_and(|r| r.ts_end.is_some_and(|t| t <= e.ts_event))
                })
                .collect();
            for oi in ended {
                d.retire(oi);
            }
        }
    }
    d.exact_snapshot(None, &engine);
    d.l2_snapshot(None, &engine);
    engine.finish(&mut sink)?;
    Ok(finalize(d.ords, &sink, last_ts, cfg, inst))
}

fn finalize(ords: Vec<Ord>, sink: &LabelSink, last_ts: Ts, cfg: &ExtractConfig, inst: &Instrument) -> Vec<FeatureRow> {
    let mut out = Vec::new();
    for o in ords {
        let (population, events): (Population, Vec<(Ts, u8, Cause)>) = match &o.real {
            None => {
                let Some((state, activated)) = sink.result.get(&o.order_ref) else {
                    continue;
                };
                if activated.is_none() || matches!(state, EndState::RejectedWouldCross | EndState::RejectedNoTouch) {
                    continue;
                }
                let mut ev = Vec::new();
                if let Some(t) = sink.first_fill.get(&o.order_ref) {
                    ev.push((*t, 0, Cause::Fill));
                }
                if let Some(t) = o.policy_cancel {
                    ev.push((t, 1, Cause::Cancel));
                }
                let t_end = sink.end_ts.get(&o.order_ref).copied().unwrap_or(last_ts);
                match state {
                    EndState::Filled => {}
                    EndState::Cancelled => ev.push((t_end, 1, Cause::Cancel)),
                    _ => ev.push((t_end, 2, Cause::Censor)),
                }
                (Population::Shadows, ev)
            }
            Some(r) => {
                let mut ev = Vec::new();
                if let Some(t) = r.ts_first_fill {
                    ev.push((t, 0, Cause::Fill));
                }
                let t_end = r.ts_end.unwrap_or(last_ts);
                match r.end_reason {
                    RealOrderEnd::Filled => {}
                    RealOrderEnd::Cancelled | RealOrderEnd::Replaced => ev.push((t_end, 1, Cause::Cancel)),
                    RealOrderEnd::Cleared | RealOrderEnd::OpenAtEnd => ev.push((t_end, 2, Cause::Censor)),
                }
                (Population::RealOrders, ev)
            }
        };
        let Some(&(t_event, _, cause)) = events.iter().min_by_key(|x| (x.0, x.1)) else {
            continue;
        };
        let (instrument_id, session_date, side) = match &o.real {
            Some(r) => (r.instrument_id, r.session_date, r.side),
            None => (inst.instrument_id, inst.session_date, o.side),
        };
        for lm in o.lms {
            if !(lm.l == 0 || t_event > lm.t) {
                continue;
            }
            let Some(mut values) = lm.feats else { continue };
            if cfg.privileged {
                values.extend(lm.privileged);
            }
            let mut e = t_event.saturating_sub(lm.t).max(0);
            let mut c = cause;
            if e > cfg.h_max_ns {
                e = cfg.h_max_ns;
                c = Cause::Censor;
            }
            out.push(FeatureRow {
                order_ref: o.order_ref.clone(),
                population,
                instrument_id,
                session_date,
                ts_landmark: lm.t,
                landmark_ns: lm.l,
                side_sign: if side == BookSide::Bid { 1 } else { -1 },
                values,
                event_ns: e,
                cause: c,
                midnight_utc_ns: Some(inst.midnight_utc_ns),
            });
        }
    }
    out
}
