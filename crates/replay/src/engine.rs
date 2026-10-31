//! The replay engine (SPEC 5.1): per-instrument books fed in feed order, event atomicity at `flags::LAST`, stats and
//! anomaly counters (et `Stats`), derived trades/BBO/depth, the rank oracle, shadows, real-order lives and observers.
//!
//! Per record: counters; event start (shadow agenda, observers) when the instrument has no open event; rank oracle on
//! `F`; book apply; trade row; shadow accounting (or censoring on `R`, `MAYBE_BAD_BOOK`, a detected sequence gap);
//! real-order lives; observers. At `LAST`: invariant check, BBO/depth rows, shadow fill decisions, observers.

use qt_book::{Book, Effect, IdMap, Invariants, LevelView};
use qt_schema::json::{DataQuality, FeedGap};
use qt_schema::rows::{BboRow, DepthRow, EndState, PlannedEndKind, ShadowAmend, ShadowSpec, TradeKind, TradeRow};
use qt_types::{Action, BookSide, Instrument, MboEvent, Px, QtError, QtResult, Qty, Ts, flags};

use crate::dq::{apply_verdict, dq_flags};
use crate::oracle::{Clock, Oracle, Sweep};
use crate::pairing::FillPairing;
use crate::real_orders::RealOrders;
use crate::shadow::ShadowEngine;
use crate::util::{DAY_NS, InstrumentTable, SEC_NS, utc_days};
use crate::why::Tracer;
use crate::{
    BboMode, EngineConfig, EngineReport, EventEnd, EventObserver, FillRule, HiddenPrintPolicy, PlannedEnd,
    ShadowDiagnostics, ShadowState, Sink, Stats, TradingHalt,
};

type BboKey = (Option<Px>, Qty, u32, Option<Px>, Qty, u32);

/// What an excluded order is (A1), decided at its first record.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Excluded {
    /// No record seen yet.
    New,
    /// Removed from the tape; its last known resting level (side, price).
    Dropped(Option<(BookSide, Px)>),
    /// Replaced by its own shadow only (twin linking): kept on the tape for everyone else.
    Twin,
}

struct Inst {
    id: u32,
    book: Book,
    open: bool,
    ev_ts: Ts,
    last_ts: Ts,
    last_seq: u64,
    prev_seq: Option<u64>,
    epoch: u64,
    consumed: Vec<u64>,
    sweeps: Vec<Sweep>,
    bbo_last: Option<BboKey>,
    depth_prev: [Vec<LevelView>; 2],
    real: Option<RealOrders>,
    records: Vec<MboEvent>,
    effects: Vec<Effect>,
    pairing: FillPairing,
}

/// Exact replay engine for one partition stream (any number of instruments).
pub struct Engine {
    cfg: EngineConfig,
    instruments: InstrumentTable,
    slots: IdMap<u32, usize>,
    insts: Vec<Inst>,
    last: Option<(u32, usize)>,
    shadows: ShadowEngine,
    observers: Vec<Box<dyn EventObserver>>,
    stats: Stats,
    maybe_bad_book: u64,
    bad_ts_recv: u64,
    seq: Vec<Option<u64>>,
    last_ts: Option<Ts>,
    seen_first: bool,
    detect_gaps: bool,
    feed_gaps: Vec<FeedGap>,
    oracle: Option<Oracle>,
    ahead_buf: Vec<u64>,
    /// Excluded (instrument, order id) -> what it is (A1).
    excluded: std::collections::HashMap<(u32, u64), Excluded>,
    excluded_seen: std::collections::HashSet<(u32, u64)>,
    excluded_records: u64,
    /// Excluded orders replaced by their own shadow (`set_twin_linking`).
    twin_linking: bool,
    twins_linked: u64,
    fill_rule: FillRule,
    hidden_policy: HiddenPrintPolicy,
    /// (instrument, sequence) of ITCH cross prints (`trades_source` kind `cross`): not hidden prints (OC-28).
    cross_prints: std::collections::HashSet<(u32, u64)>,
}

fn bbo_key(book: &Book) -> BboKey {
    let b = book.bbo();
    let (bp, bs, bc) = b.bid.map_or((None, 0, 0), |l| (Some(l.price), l.total, l.count));
    let (ap, asz, ac) = b.ask.map_or((None, 0, 0), |l| (Some(l.price), l.total, l.count));
    (bp, bs, bc, ap, asz, ac)
}

impl Engine {
    /// New engine with shadows and amends (validated: ids, amend ordering).
    pub fn new(
        cfg: EngineConfig,
        instruments: &[Instrument],
        shadows: Vec<ShadowSpec>,
        amends: Vec<ShadowAmend>,
    ) -> QtResult<Self> {
        if cfg.sweep_gap_ns < 0 {
            return Err(QtError::Config(format!(
                "sweep_gap_ns must be >= 0 (got {})",
                cfg.sweep_gap_ns
            )));
        }
        if cfg.race_window_ns < 0 {
            return Err(QtError::Config(format!(
                "race_window_ns must be >= 0 (got {})",
                cfg.race_window_ns
            )));
        }
        if cfg.depth_levels > 256 {
            return Err(QtError::Config(format!(
                "depth_levels must be <= 256 (got {})",
                cfg.depth_levels
            )));
        }
        let table = InstrumentTable::new(instruments);
        let cfg_rules = cfg.rules;
        let shadows = ShadowEngine::new(shadows, amends, &cfg, &table)?;
        Ok(Self {
            oracle: cfg.rank_oracle.then(|| Oracle::new(cfg.sweep_gap_ns)),
            cfg,
            instruments: table,
            slots: IdMap::default(),
            insts: Vec::new(),
            last: None,
            shadows,
            observers: Vec::new(),
            stats: Stats::default(),
            maybe_bad_book: 0,
            bad_ts_recv: 0,
            seq: Vec::new(),
            last_ts: None,
            seen_first: false,
            detect_gaps: true,
            feed_gaps: Vec::new(),
            ahead_buf: Vec::new(),
            excluded: std::collections::HashMap::new(),
            excluded_seen: std::collections::HashSet::new(),
            excluded_records: 0,
            twin_linking: false,
            twins_linked: 0,
            fill_rule: FillRule::Exact,
            hidden_policy: HiddenPrintPolicy::default_for(&cfg_rules),
            cross_prints: std::collections::HashSet::new(),
        })
    }

    /// Adds an observer.
    pub fn add_observer(&mut self, o: Box<dyn EventObserver>) {
        self.observers.push(o);
        self.shadows.collect_truth = true;
    }

    /// Counts sequence gaps (and censors shadows at them) from the record stream itself; on by default. Partition
    /// replays turn it off: a partition holds one instrument of a feed whose sequence numbers span all instruments,
    /// so its gaps come from ingest (`set_feed_gaps`).
    pub fn set_sequence_gap_detection(&mut self, on: bool) {
        self.detect_gaps = on;
    }

    /// Feed gaps detected at ingest: live shadows of the instrument end `censored_gap` at the start of the first
    /// event after `ts_start`; the gaps are reported in the data-quality document.
    pub fn set_feed_gaps(&mut self, gaps: &[FeedGap]) {
        for g in gaps {
            self.shadows.add_gap(g.instrument_id, g.ts_start);
            self.feed_gaps.push(g.clone());
        }
    }

    /// Trading halts (SPEC 1.2: shadows resting through an auction or halt are censored): no shadow is live while its
    /// instrument is not in continuous trading. A shadow live when a halt starts ends `censored_gap` at `ts_start`
    /// (at the start of the instrument's first event after it, so an event at `ts_start` itself still trades; its
    /// own cancel or amend effective before `ts_start` still applies, one at `ts_start` loses as it does to a feed
    /// gap); a shadow whose `ts_live` falls in `[ts_start, ts_end)` never goes live and ends `censored_gap` at its
    /// `ts_live` (a cancel or amend to zero effective at or before `ts_live` still ends it `cancelled`), also when it
    /// would be a twin's shadow (A1: the twin stays on the tape). Spans may overlap; an empty span (`ts_end <=
    /// ts_start`) is `QT-CONFIG-001`. Call before the first record; calls add up.
    pub fn set_trading_halts(&mut self, halts: &[TradingHalt]) -> QtResult<()> {
        if let Some(h) = halts.iter().find(|h| h.ts_end <= h.ts_start) {
            return Err(QtError::Config(format!(
                "trading halt of instrument {}: ts_end {} must be > ts_start {}",
                h.instrument_id, h.ts_end, h.ts_start
            )));
        }
        for h in halts {
            self.shadows.add_halt(h.instrument_id, h.ts_start, h.ts_end);
        }
        Ok(())
    }

    /// Selects the shadow fill rule (default `Exact`; `Et` for et parity). Call before the first record.
    pub fn set_fill_rule(&mut self, rule: FillRule) {
        self.fill_rule = rule;
        self.shadows.set_rule(self.fill_rule, self.hidden_policy);
    }

    /// Selects the hidden-print policy (default `HiddenPrintPolicy::default_for(rules)`). Call before the first record.
    pub fn set_hidden_print_policy(&mut self, policy: HiddenPrintPolicy) {
        self.hidden_policy = policy;
        self.shadows.set_rule(self.fill_rule, self.hidden_policy);
    }

    /// Audit ruling A1: real orders removed from the tape before shadows are injected (a live order's own MBO twin).
    /// Their `A`/`C`/`M`/`F` records never reach the book, the oracle, real-order lives or observers; their
    /// executions are credited to shadows at their level as volume behind (the shadow takes the twin's place) and
    /// as trade-through volume to shadows strictly better on that side. `T` prints stay on the tape. Call before the
    /// first record.
    pub fn set_excluded_orders(&mut self, ids: impl IntoIterator<Item = (u32, u64)>) {
        for k in ids {
            self.excluded.insert(k, Excluded::New);
        }
    }

    /// Audit ruling A1, self-exclusion (off by default; `replay` and `why` turn it on): an excluded order whose
    /// first record is an `A` / `M` that a shadow enters at (same instrument, side and abs price, `ts_live` = the
    /// record's `ts_event`, or 1 ns before it when the shadow went live at the start of that record's event; the
    /// first such shadow in input order, an equal quantity first) is that shadow's twin. It is not removed from the
    /// tape: every other shadow, the book, BBO, trades and the oracle see it as the real order it is, while its own
    /// shadow activates at the twin's record in the twin's queue place, never counts the twin's records as level
    /// activity, and is filled by each execution of the twin at the execution's price. Later `A` / `M` records of
    /// a twin link the shadows of its later legs the same way. Excluded orders without such a shadow are removed
    /// from the tape (`set_excluded_orders`). Exact fill rule only. Call before the first record.
    pub fn set_twin_linking(&mut self, on: bool) {
        self.twin_linking = on;
    }

    /// ITCH cross prints (the `sequence` of side-`N` `T` records the source marks as crosses, from `trades_source`
    /// kind `cross`; OC-28): counted `cross_prints` (INFO), never hidden prints, never shadow fill volume. Call
    /// before the first record.
    pub fn set_cross_prints(&mut self, keys: impl IntoIterator<Item = (u32, u64)>) {
        self.cross_prints.extend(keys);
    }

    /// An engine for an interactive session: no shadows yet, each instrument ready for `add_shadow` (so feed gaps and
    /// trading halts set after this reach the shadows added later).
    pub fn new_interactive(cfg: EngineConfig, instruments: &[Instrument]) -> QtResult<Self> {
        let mut eng = Self::new(cfg, instruments, Vec::new(), Vec::new())?;
        for i in instruments {
            eng.shadows.ensure_instrument(i.instrument_id);
        }
        Ok(eng)
    }

    fn check_not_past(&self, ts: Ts, what: &str, shadow_id: &str) -> QtResult<()> {
        match self.last_ts {
            Some(p) if ts < p => Err(QtError::Config(format!(
                "shadow {shadow_id}: {what} {ts} is before the replay position {p} (records up to it were applied)"
            ))),
            _ => Ok(()),
        }
    }

    /// Adds a shadow while replaying (interactive sessions). Its replay is the one a run with it in the input would
    /// make, because its work happens at the start of the first event after `ts_live >= ts_submit`, so `ts_submit`
    /// must not be before the last record applied. Returns its index.
    pub fn add_shadow(&mut self, spec: ShadowSpec) -> QtResult<u32> {
        self.check_not_past(spec.ts_submit, "ts_submit", &spec.shadow_id)?;
        self.shadows.add(spec)
    }

    /// Sends a cancel at `ts_cancel` for a shadow without one (not before the last record applied).
    pub fn cancel_shadow(&mut self, shadow_id: &str, ts_cancel: Ts) -> QtResult<u32> {
        self.check_not_past(ts_cancel, "ts_cancel", shadow_id)?;
        self.shadows.cancel(shadow_id, ts_cancel)
    }

    /// Sends a priority-keeping reduction to `new_qty` at `ts_send` (not before the last record applied).
    pub fn amend_shadow(&mut self, shadow_id: &str, ts_send: Ts, new_qty: Qty) -> QtResult<u32> {
        self.check_not_past(ts_send, "ts_send", shadow_id)?;
        self.shadows.amend(shadow_id, ts_send, new_qty)
    }

    /// Planned (exogenous) end of every shadow, input order (A2).
    pub fn shadow_planned_ends(&self) -> Vec<PlannedEnd> {
        self.shadows
            .planned_ends()
            .into_iter()
            .map(|(shadow_id, planned_end, kind)| PlannedEnd {
                shadow_id,
                planned_end,
                kind,
            })
            .collect()
    }

    pub(crate) fn trace(&mut self, idx: u32, max_events: usize) {
        self.shadows.why = Some(Tracer::new(idx, max_events));
    }

    /// Counters so far.
    pub fn stats(&self) -> &Stats {
        &self.stats
    }

    #[inline]
    fn slot(&mut self, id: u32, ts: Ts) -> usize {
        if let Some((i, s)) = self.last {
            if i == id {
                return s;
            }
        }
        let slot = match self.slots.get(&id) {
            Some(&s) => s,
            None => {
                let session_date = self
                    .instruments
                    .at(id, ts)
                    .map_or_else(|| utc_days(ts), |i| i.session_date);
                self.insts.push(Inst {
                    id,
                    book: Book::new(self.cfg.rules),
                    open: false,
                    ev_ts: ts,
                    last_ts: ts,
                    last_seq: 0,
                    prev_seq: None,
                    epoch: 0,
                    consumed: Vec::new(),
                    sweeps: Vec::new(),
                    bbo_last: None,
                    depth_prev: [Vec::new(), Vec::new()],
                    real: self
                        .cfg
                        .real_orders
                        .then(|| RealOrders::new(id, session_date, self.cfg.rules.fill_removal_shares_sequence)),
                    records: Vec::new(),
                    effects: Vec::new(),
                    pairing: FillPairing::default(),
                });
                let s = self.insts.len() - 1;
                self.slots.insert(id, s);
                s
            }
        };
        self.last = Some((id, slot));
        slot
    }

    fn clock(&self, id: u32, ts: Ts) -> Clock {
        match self.instruments.at(id, ts) {
            Some(i) => Clock {
                midnight: i.midnight_utc_ns,
                open_ns: i64::from(i.open_s).saturating_mul(SEC_NS),
            },
            None => Clock {
                midnight: ts - ts.rem_euclid(DAY_NS),
                open_ns: 34_200 * SEC_NS,
            },
        }
    }

    /// Applies one record.
    pub fn apply(&mut self, e: &MboEvent, sink: &mut dyn Sink) -> QtResult<()> {
        let ts = e.ts_event;
        let rules = self.cfg.rules;
        self.stats.records += 1;
        let snapshot = e.flags & flags::SNAPSHOT != 0;
        // Snapshot records carry the snapshot's own timestamps: no ordering check, no clock move.
        if !snapshot {
            if self.last_ts.is_some_and(|p| ts < p) {
                self.stats.out_of_order_ts_event += 1;
            }
            self.last_ts = Some(ts);
        }
        if !self.seen_first {
            self.seen_first = true;
            if rules.snapshot_begins_with_clear && !snapshot {
                self.stats.files_without_snapshot += 1;
            }
        }
        if snapshot {
            self.stats.snapshot_records += 1;
        }
        let bad_book = e.flags & flags::MAYBE_BAD_BOOK != 0;
        self.maybe_bad_book += u64::from(bad_book);
        self.bad_ts_recv += u64::from(e.flags & flags::BAD_TS_RECV != 0);
        let mut gap = false;
        if rules.sequence_meaningful {
            let ch = usize::from(e.channel_id);
            if self.seq.len() <= ch {
                self.seq.resize(ch + 1, None);
            }
            if snapshot {
                self.seq[ch] = None;
            } else {
                if let Some(prev) = self.seq[ch] {
                    if e.sequence > prev.saturating_add(1) {
                        if self.detect_gaps {
                            self.stats.sequence_gaps += 1;
                            gap = true;
                        }
                    } else if e.sequence < prev {
                        self.stats.sequence_regressions += 1;
                    }
                }
                self.seq[ch] = Some(e.sequence);
            }
        }

        let id = e.instrument_id;
        let slot = self.slot(id, ts);
        let opened = {
            let inst = &mut self.insts[slot];
            if inst.prev_seq != Some(e.sequence) {
                inst.epoch += 1;
                inst.prev_seq = Some(e.sequence);
            }
            inst.last_ts = ts;
            inst.last_seq = e.sequence;
            let opened = !inst.open;
            if opened {
                inst.open = true;
                inst.ev_ts = ts;
            }
            opened
        };
        let has_shadows = self.shadows.has_instrument(id);
        if opened {
            let inst = &self.insts[slot];
            if has_shadows {
                self.shadows
                    .on_event_start(id, ts, e.sequence, inst.epoch, &inst.book, sink)?;
            }
            for o in &mut self.observers {
                o.on_event_start(id, ts, &inst.book);
            }
        }

        let action = Action::from_byte(e.action);
        if !self.excluded.is_empty()
            && matches!(
                action,
                Some(Action::Add | Action::Cancel | Action::Modify | Action::Fill)
            )
        {
            let key = (id, e.order_id);
            if let Some(&state) = self.excluded.get(&key) {
                self.excluded_seen.insert(key);
                let level = BookSide::from_byte(e.side)
                    .filter(|_| e.price != qt_types::UNDEF_PRICE)
                    .map(|s| (s, e.price));
                let entry = matches!(action, Some(Action::Add | Action::Modify));
                let state = match state {
                    Excluded::New | Excluded::Twin if entry && !(gap || bad_book) => {
                        // A twin's shadow enters at this record (A1 self-exclusion; a later leg of a twin too).
                        let linked = match level {
                            Some((side, px))
                                if self.twin_linking && has_shadows && self.fill_rule == FillRule::Exact =>
                            {
                                let inst = &self.insts[slot];
                                self.shadows.link_twin(
                                    id, e.order_id, ts, e.sequence, side, px, e.size, inst.epoch, &inst.book, sink,
                                )?
                            }
                            _ => false,
                        };
                        if state == Excluded::Twin || linked {
                            self.twins_linked += u64::from(state == Excluded::New);
                            Excluded::Twin
                        } else {
                            Excluded::Dropped(None)
                        }
                    }
                    Excluded::New => Excluded::Dropped(None),
                    s => s,
                };
                self.excluded.insert(key, state);
                if let Excluded::Dropped(prev) = state {
                    if has_shadows && (gap || bad_book) {
                        // A gap or a bad-book flag censors live shadows even on an excluded order's record (m18).
                        let inst = &self.insts[slot];
                        self.shadows
                            .censor(id, EndState::CensoredGap, ts, e.sequence, &inst.book, sink)?;
                    }
                    self.excluded_records += 1;
                    match action {
                        Some(Action::Add | Action::Modify) => {
                            self.excluded.insert(key, Excluded::Dropped(level));
                        }
                        Some(Action::Fill) => {
                            if let (Some((side, px)), true) = (prev, has_shadows) {
                                let inst = &self.insts[slot];
                                self.shadows
                                    .execution(id, None, side, px, e.price, e.size, &inst.book, inst.epoch);
                            }
                        }
                        _ => {}
                    }
                    if e.flags & flags::LAST != 0 {
                        self.end_event(slot, sink)?;
                    }
                    return Ok(());
                }
                // A twin: its record goes on like any real order's (the shadow engine treats it as the twin's own).
            }
        }
        if self.oracle.is_some() {
            let clock = self.clock(id, ts);
            if let Some(o) = self.oracle.as_mut() {
                let inst = &mut self.insts[slot];
                if !inst.sweeps.is_empty() {
                    o.expire(&mut inst.sweeps, ts, id, sink)?;
                }
                if action == Some(Action::Fill) {
                    let rank = inst
                        .book
                        .order_rank(e.order_id, &inst.consumed, Some(&mut self.ahead_buf));
                    inst.consumed.push(e.order_id);
                    o.fill(&mut inst.sweeps, e, rank, &self.ahead_buf, clock, sink)?;
                }
            }
        }

        // Shadows end before a clear / gap / bad-book record applies (their last known state is the book before it).
        let censor = has_shadows && (gap || bad_book || action == Some(Action::Clear));
        if censor {
            let state = if gap || bad_book {
                EndState::CensoredGap
            } else {
                EndState::CensoredClear
            };
            let inst = &self.insts[slot];
            self.shadows.censor(id, state, ts, e.sequence, &inst.book, sink)?;
        }
        let inst = &mut self.insts[slot];
        let pre = if action == Some(Action::Add) {
            inst.book.order(e.order_id).map(|(s, p, o)| (s, p, o.size))
        } else {
            None
        };
        let effect = inst.book.apply(e);
        if let Some(err) = inst.book.error() {
            return Err(QtError::Engine(format!(
                "instrument {id}, sequence {}: {err}",
                e.sequence
            )));
        }
        let dup = if matches!(effect, Effect::Add { .. }) {
            pre
        } else {
            None
        };
        if let Some(o) = self.oracle.as_mut() {
            o.touch(&mut inst.sweeps, &effect, id, sink)?;
        }
        match action {
            Some(Action::Trade) => {
                self.stats.trades += 1;
                let cross =
                    e.side == b'N' && !self.cross_prints.is_empty() && self.cross_prints.contains(&(id, e.sequence));
                let hidden = e.side == b'N' && rules.supports_hidden_orders && !cross;
                self.stats.hidden_prints += u64::from(hidden);
                self.stats.cross_prints += u64::from(cross);
                sink.trade(&TradeRow {
                    ts_event: ts,
                    instrument_id: id,
                    sequence: e.sequence,
                    price: e.price,
                    size: e.size,
                    aggressor_side: e.side(),
                    kind: if cross {
                        TradeKind::Cross
                    } else if hidden {
                        TradeKind::Hidden
                    } else {
                        TradeKind::Displayed
                    },
                    maker_order_id: None,
                })?;
            }
            Some(Action::Fill) if inst.book.order(e.order_id).is_some_and(|(_, px, _)| px != e.price) => {
                self.stats.off_price_executions += 1;
            }
            _ => {}
        }
        let known = action == Some(Action::Fill) && inst.book.order(e.order_id).is_some();
        if inst.pairing.record(rules.fill_removal_shares_sequence, e, known) {
            self.stats.removal_after_unpaired_fill += 1;
        }
        let skip_cross = action == Some(Action::Trade)
            && !self.cross_prints.is_empty()
            && self.cross_prints.contains(&(id, e.sequence));
        if has_shadows && !censor && !skip_cross {
            self.shadows.on_record(e, &effect, dup, &inst.book, inst.epoch);
        }
        if let Some(r) = inst.real.as_mut() {
            r.on_record(e, &effect, dup.is_some(), &inst.book, inst.epoch, sink)?;
        }
        if !self.observers.is_empty() {
            for o in &mut self.observers {
                o.on_record(e, &effect, &inst.book);
            }
            inst.records.push(*e);
            inst.effects.push(effect);
        }
        if e.flags & flags::LAST != 0 {
            self.end_event(slot, sink)?;
        }
        Ok(())
    }

    fn end_event(&mut self, slot: usize, sink: &mut dyn Sink) -> QtResult<()> {
        let inst = &mut self.insts[slot];
        let (id, ts, seq) = (inst.id, inst.last_ts, inst.last_seq);
        inst.book.note_event_end();
        inst.pairing.event_end(self.cfg.rules.fill_removal_shares_sequence);
        self.stats.events += 1;
        if self.cfg.bbo != BboMode::None {
            let key = bbo_key(&inst.book);
            if self.cfg.bbo == BboMode::Full || inst.bbo_last != Some(key) {
                inst.bbo_last = Some(key);
                sink.bbo(&BboRow {
                    ts_event: ts,
                    instrument_id: id,
                    sequence: seq,
                    bid_px: key.0,
                    bid_sz: key.1,
                    bid_ct: key.2,
                    ask_px: key.3,
                    ask_sz: key.4,
                    ask_ct: key.5,
                })?;
            }
        }
        let n = usize::from(self.cfg.depth_levels);
        if n > 0 {
            for (k, side) in [BookSide::Bid, BookSide::Ask].into_iter().enumerate() {
                let cur = inst.book.depth(side, n);
                let prev = &inst.depth_prev[k];
                for i in 0..prev.len().max(cur.len()) {
                    let (a, b) = (prev.get(i), cur.get(i));
                    if a != b {
                        sink.depth(&DepthRow {
                            ts_event: ts,
                            instrument_id: id,
                            sequence: seq,
                            side,
                            level: i as u8,
                            price: b.map(|l| l.price),
                            size: b.map_or(0, |l| l.total),
                            count: b.map_or(0, |l| l.count),
                        })?;
                    }
                }
                inst.depth_prev[k] = cur;
            }
        }
        if self.shadows.has_instrument(id) {
            self.shadows.on_event_end(id, ts, seq, inst.epoch, &inst.book, sink)?;
        } else {
            self.shadows.truth.clear();
        }
        if let Some(r) = inst.real.as_mut() {
            r.on_event_end();
        }
        if !self.observers.is_empty() {
            let end = EventEnd {
                instrument_id: id,
                ts_event: ts,
                sequence: seq,
                book: &inst.book,
                records: &inst.records,
                effects: &inst.effects,
                shadow_truth: &self.shadows.truth,
            };
            for o in &mut self.observers {
                o.on_event_end(&end);
            }
            inst.records.clear();
            inst.effects.clear();
        }
        inst.consumed.clear();
        inst.open = false;
        Ok(())
    }

    /// Ends input (synthetic event end, censoring) and reports.
    pub fn finish(mut self, sink: &mut dyn Sink) -> QtResult<EngineReport> {
        self.finish_inner(sink)
    }

    /// `finish` plus the trace of the traced shadow.
    pub(crate) fn finish_traced(mut self, sink: &mut dyn Sink) -> QtResult<(EngineReport, Option<Tracer>)> {
        let r = self.finish_inner(sink)?;
        Ok((r, self.shadows.why.take()))
    }

    fn finish_inner(&mut self, sink: &mut dyn Sink) -> QtResult<EngineReport> {
        for slot in 0..self.insts.len() {
            if self.insts[slot].open {
                self.stats.unterminated_events += 1;
                self.end_event(slot, sink)?;
            }
        }
        if let Some(o) = self.oracle.as_mut() {
            for inst in &mut self.insts {
                o.flush(&mut inst.sweeps, inst.id, sink)?;
            }
        }
        {
            let slots = &self.slots;
            let insts = &self.insts;
            let last = |id: u32| {
                slots
                    .get(&id)
                    .map(|&s| (insts[s].last_ts, insts[s].last_seq, &insts[s].book))
            };
            self.shadows.finish(&last, sink)?;
        }
        for inst in &mut self.insts {
            if let Some(r) = inst.real.as_mut() {
                r.finish(sink)?;
            }
        }
        self.stats.instruments = self.insts.len() as u64;
        let mut inv = Invariants::default();
        for i in &self.insts {
            inv.add(i.book.invariants());
        }
        let mut dq = DataQuality {
            records: self.stats.records,
            maybe_bad_book_records: self.maybe_bad_book,
            bad_ts_recv_records: self.bad_ts_recv,
            snapshot_records: self.stats.snapshot_records,
            unknown_order_refs: inv.unknown_order_refs + inv.fills_unknown_order,
            duplicate_adds: inv.duplicate_adds,
            unexpected_modifies: inv.unexpected_modifies,
            sequence_gaps: self.stats.sequence_gaps,
            sequence_regressions: self.stats.sequence_regressions,
            out_of_order_ts_event: self.stats.out_of_order_ts_event,
            crossed_books_after_last: inv.crossed_books_after_last,
            hidden_prints: self.stats.hidden_prints,
            off_price_executions: self.stats.off_price_executions,
            feed_gaps: std::mem::take(&mut self.feed_gaps),
            ..DataQuality::default()
        };
        for (k, v) in [
            ("unterminated_events", self.stats.unterminated_events),
            ("files_without_snapshot", self.stats.files_without_snapshot),
            ("removal_after_unpaired_fill", self.stats.removal_after_unpaired_fill),
            ("cross_prints", self.stats.cross_prints),
        ] {
            if v > 0 {
                dq.source_specific.insert(k.into(), v);
            }
        }
        apply_verdict(&mut dq, self.stats.events);
        let flags = dq_flags(dq.verdict);
        let shadows = if self.shadows.is_empty() {
            None
        } else {
            let planned = self.shadows.planned_ends();
            for (r, (_, end, kind)) in self.shadows.results(flags).iter().zip(planned) {
                let kind = match kind {
                    "cancel" => PlannedEndKind::Cancel,
                    "session_close" => PlannedEndKind::SessionClose,
                    _ => PlannedEndKind::None,
                };
                sink.shadow_result_planned(r, (end, kind))?;
            }
            Some(self.shadows.counts())
        };
        for o in &mut self.observers {
            o.on_finish();
        }
        Ok(EngineReport {
            stats: std::mem::take(&mut self.stats),
            invariants: inv,
            rank_oracle: self.oracle.as_ref().map(Oracle::summary),
            data_quality: dq,
            shadows,
            diagnostics: ShadowDiagnostics {
                excluded_requested: self.excluded.len() as u64,
                excluded_matched: self.excluded_seen.len() as u64,
                excluded_matched_ids: {
                    let mut v: Vec<(u32, u64)> = self.excluded_seen.iter().copied().collect();
                    v.sort_unstable();
                    v
                },
                excluded_records: self.excluded_records,
                twins_linked: self.twins_linked,
                ..self.shadows.diag.clone()
            },
        })
    }

    /// Book of an instrument.
    pub fn book(&self, instrument_id: u32) -> Option<&Book> {
        self.slots.get(&instrument_id).map(|&s| &self.insts[s].book)
    }

    /// Index of a shadow id.
    pub fn shadow_index(&self, shadow_id: &str) -> Option<u32> {
        self.shadows.index(shadow_id)
    }

    /// Spec of a shadow (panics on an index not returned by `shadow_index`).
    pub fn shadow_spec(&self, idx: u32) -> &ShadowSpec {
        self.shadows.spec(idx)
    }

    /// State of a shadow (panics on an index not returned by `shadow_index`).
    pub fn shadow_state(&self, idx: u32) -> ShadowState {
        let mut st = self.shadows.state(idx);
        if st.status == crate::ShadowStatus::Live {
            let spec = self.shadows.spec(idx);
            if let (Some(px), Some(b)) = (st.price, self.book(spec.instrument_id)) {
                st.level_qty = Some(b.level_total(spec.side, px));
            }
        }
        st
    }

    /// Ends every shadow as `censored_gap` without replaying (their partition does not exist).
    pub(crate) fn censor_unreplayed(&mut self, sink: &mut dyn Sink) -> QtResult<()> {
        self.shadows.censor_unreplayed(sink)
    }
}
