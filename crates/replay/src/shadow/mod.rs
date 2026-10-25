//! Shadow orders (SPEC 5.2): independent orders that sit in the real FIFO queue without touching the book or tape.
//!
//! Timing: `ts_live = ts_submit + submit delay`; a shadow activates at the start of the first event of its instrument
//! with `ts_event > ts_live`, appended behind every order resting at its price in the book left by the previous event.
//! Cancels (`ts_cancel + cancel delay`) and amends (`ts_send + amend delay`) take effect at the start of the first
//! event strictly after their effective time. Work at an event start is an agenda ordered by time, then gap, cancel,
//! amend, activation (so a cancel effective at `ts_live` wins). Live shadows are indexed by (side, price), so a record
//! only touches the shadows of its level (or the price range a trade reaches); event-end work covers the shadows a
//! record touched plus those the book now crosses (all live shadows when observers need per-event truth).
//!
//! Self-exclusion twins (audit ruling A1, `link_twin`): a shadow can replace one excluded real order, its twin. The
//! twin stays on the tape for every other shadow; for its own shadow it is the shadow itself: the shadow activates
//! at the twin's `A` / `M` record (before it applies, so its queue ahead is exactly the twin's), the twin's records
//! are never level activity for it, and each execution of the twin at the shadow's level is a fill of the shadow
//! at the execution's price (`Shadow::step_exact`).

mod state;
mod timing;

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap};

use qt_book::{Book, Effect, IdMap};
use qt_schema::json::ShadowCounts;
use qt_schema::rows::{
    EndState, FillReason, Liquidity, PriceSpec, ShadowAmend, ShadowFillRow, ShadowResultRow, ShadowSpec,
    TrajectoryKind, TrajectoryRow,
};
use qt_types::{Action, BookSide, MboEvent, Px, QtResult, Qty, Side, Ts};

use crate::util::{InstrumentTable, SEC_NS};
use crate::why::Tracer;
use crate::{
    EngineConfig, FillRule, HiddenPrintPolicy, ShadowDiagnostics, ShadowEventTruth, ShadowState, ShadowStatus, Sink,
    TrajectoryMode,
};

use state::Shadow;
use state::{LevelAcc, StepOut, bump};

const K_GAP: u8 = 0;
const K_ITEM: u8 = 1;
/// A trading halt starts (SPEC 1.2; `add_halt`).
const K_HALT: u8 = 2;

type Agenda = BinaryHeap<Reverse<(Ts, u8, u32, u32)>>;

/// Shadows of one instrument.
#[derive(Default)]
struct InstShadows {
    agenda: Agenda,
    live: BTreeSet<u32>,
    bids: BTreeMap<Px, Vec<u32>>,
    asks: BTreeMap<Px, Vec<u32>>,
    dirty: Vec<u32>,
    /// Shadows activated at this event's start (truth).
    activated: Vec<u32>,
    /// Shadows that ended during this event outside the event-end step (truth).
    ended: Vec<u32>,
    /// Level accumulators of the current event, for levels that have shadows.
    lvl: BTreeMap<(u8, Px), LevelAcc>,
    /// Events ended so far (crossed-run tracking).
    events: u64,
    /// The instrument's order ids are ITCH order references (`order_ref_priority`): rules D1 to D3 apply under the
    /// `Exact` rule. Set at construction from the instrument row.
    itch: bool,
    /// Largest order reference added so far in the partition (`itch` only).
    max_ref: u64,
    /// Open execution sweep per resting side (index `side_ix`), for the resting side of hidden prints.
    sweep: [Sweep; 2],
    /// Trading halts `[start, end)`, sorted and merged (`add_halt`).
    halts: Vec<(Ts, Ts)>,
}

/// Executions on one resting side at one `ts_event` with no other change on that side since (oracle sweep rule,
/// gap 0): one aggressor walking the side. A hidden (side-`N`) print at the same `ts_event` is that aggressor
/// continuing into non-displayed interest on the same side.
#[derive(Clone, Debug, Default)]
struct Sweep {
    open: bool,
    ts: Ts,
    /// (order id, sequence) of the executions in the sweep: their removals are not book changes.
    execs: Vec<(u64, u64)>,
}

#[inline]
fn side_ix(side: BookSide) -> usize {
    match side {
        BookSide::Bid => 0,
        BookSide::Ask => 1,
    }
}

/// Whether the instrument's order ids are Nasdaq TotalView-ITCH order reference numbers, which increase in receipt
/// order within each instrument (they come from several counters, each shared by a group of symbols, not from one
/// feed-wide counter, so references of different instruments are not compared): true for Nasdaq instruments ingested as
/// `itch`, `etcsv` or `dbn` (a `dbn` file with the `nasdaq` rule set is taken to be XNAS.ITCH; other Databento MBO
/// feeds must not be ingested with it). Nasdaq holds orders accepted before the open that are not eligible before it
/// and releases them by `A` records with their original, smaller references during the first seconds of the session,
/// and those released orders keep their receipt priority: the matching engine executes them ahead of orders at the same
/// price that were displayed earlier but received later (on 2019-12-30, 608 of the 617 executions of a non-front order
/// in the first minute of AAPL, MSFT, SPY and TSLA hit the front order in reference order). This is not a general
/// reference-order rule: later in the day most out-of-order executions on SPY are not in reference order. Gates the
/// `Exact` rule's D1 to D3 (oracle-disagreements extension); other sources (and synthetic data) assign ids freely and
/// keep arrival order and the shadow-side inference of hidden prints.
pub(crate) fn order_ref_priority(inst: &qt_types::Instrument) -> bool {
    inst.venue == qt_types::Venue::Nasdaq && matches!(inst.source.as_str(), "itch" | "etcsv" | "dbn")
}

/// Live shadows at (side, price).
fn level_of<'a>(
    bids: &'a BTreeMap<Px, Vec<u32>>,
    asks: &'a BTreeMap<Px, Vec<u32>>,
    side: BookSide,
    px: Px,
) -> &'a [u32] {
    let m = match side {
        BookSide::Bid => bids,
        BookSide::Ask => asks,
    };
    m.get(&px).map_or(&[], Vec::as_slice)
}

/// The accumulator of (side, price), created at the level's first touch in the event; `delta` = the current record's
/// change of the level total, so `before` is the total at event start.
fn acc_of<'a>(
    lvl: &'a mut BTreeMap<(u8, Px), LevelAcc>,
    book: &Book,
    side: BookSide,
    px: Px,
    delta: i128,
) -> &'a mut LevelAcc {
    lvl.entry((side.as_byte(), px)).or_insert_with(|| LevelAcc {
        before: (i128::from(book.level_total(side, px)) - delta).clamp(0, i128::from(Qty::MAX)) as Qty,
        ..LevelAcc::default()
    })
}

impl InstShadows {
    /// Whether `ts` falls in a trading halt.
    fn halted_at(&self, ts: Ts) -> bool {
        let k = self.halts.partition_point(|&(start, _)| start <= ts);
        k > 0 && ts < self.halts[k - 1].1
    }

    fn insert(&mut self, idx: u32, side: BookSide, px: Px) {
        self.live.insert(idx);
        let m = match side {
            BookSide::Bid => &mut self.bids,
            BookSide::Ask => &mut self.asks,
        };
        m.entry(px).or_default().push(idx);
    }

    fn remove(&mut self, idx: u32, side: BookSide, px: Px) {
        self.live.remove(&idx);
        let m = match side {
            BookSide::Bid => &mut self.bids,
            BookSide::Ask => &mut self.asks,
        };
        if let Some(v) = m.get_mut(&px) {
            if let Some(p) = v.iter().position(|&i| i == idx) {
                v.swap_remove(p);
            }
            if v.is_empty() {
                m.remove(&px);
            }
        }
    }
}

/// All shadows of one engine.
pub(crate) struct ShadowEngine {
    specs: Vec<ShadowSpec>,
    st: Vec<Shadow>,
    by_id: std::collections::HashMap<String, u32>,
    inst: Vec<InstShadows>,
    slots: IdMap<u32, usize>,
    last_slot: std::cell::Cell<(u32, usize)>,
    by_sequence: bool,
    snapshot_flagged: bool,
    hidden: bool,
    rule: FillRule,
    hidden_policy: HiddenPrintPolicy,
    pub(crate) diag: ShadowDiagnostics,
    trajectory: TrajectoryMode,
    race_window: i64,
    instruments: InstrumentTable,
    fills_rows: u64,
    trajectory_rows: u64,
    pub(crate) collect_truth: bool,
    pub(crate) truth: Vec<ShadowEventTruth>,
    fills_buf: Vec<ShadowFillRow>,
    work: Vec<u32>,
    pub(crate) why: Option<Tracer>,
    /// Abs-price shadows by (instrument, `ts_live`): the twin-link candidates (`link_twin`).
    by_live: std::collections::HashMap<(u32, Ts), Vec<u32>>,
    /// Some shadow replaces a twin (enables the per-record twin checks).
    has_twins: bool,
}

/// Book facts a shadow needs at event end.
fn opposite_best(book: &Book, side: BookSide) -> Option<Px> {
    book.best_price(side.opposite())
}

impl ShadowEngine {
    pub(crate) fn new(
        specs: Vec<ShadowSpec>,
        amends: Vec<ShadowAmend>,
        cfg: &EngineConfig,
        instruments: &InstrumentTable,
    ) -> QtResult<Self> {
        let (st, by_id) = timing::build(&specs, &amends, cfg, instruments)?;
        let mut inst: Vec<InstShadows> = Vec::new();
        let mut slots: IdMap<u32, usize> = IdMap::default();
        let mut by_live: std::collections::HashMap<(u32, Ts), Vec<u32>> = std::collections::HashMap::new();
        for (i, s) in st.iter().enumerate() {
            let i = i as u32;
            if matches!(s.price_spec, PriceSpec::Abs(_)) {
                by_live.entry((s.instrument_id, s.ts_live)).or_default().push(i);
            }
            let k = *slots.entry(s.instrument_id).or_insert_with(|| {
                inst.push(InstShadows {
                    itch: instruments.any(s.instrument_id).is_some_and(order_ref_priority),
                    ..InstShadows::default()
                });
                inst.len() - 1
            });
            let a = &mut inst[k].agenda;
            a.push(Reverse((s.ts_live, K_ITEM, i, 0)));
            if let Some(c) = s.cancel_eff {
                a.push(Reverse((c, K_ITEM, i, 0)));
            }
            for (k, &(t, _)) in s.amends.iter().enumerate() {
                a.push(Reverse((t, K_ITEM, i, k as u32)));
            }
        }
        Ok(Self {
            specs,
            st,
            by_id,
            inst,
            slots,
            last_slot: std::cell::Cell::new((0, usize::MAX)),
            by_sequence: cfg.rules.fill_removal_shares_sequence,
            snapshot_flagged: cfg.rules.snapshot_records_flagged,
            hidden: cfg.rules.supports_hidden_orders,
            rule: FillRule::Exact,
            hidden_policy: HiddenPrintPolicy::default_for(&cfg.rules),
            diag: ShadowDiagnostics::default(),
            trajectory: cfg.trajectory,
            race_window: cfg.race_window_ns,
            instruments: instruments.clone(),
            fills_rows: 0,
            trajectory_rows: 0,
            collect_truth: false,
            truth: Vec::new(),
            fills_buf: Vec::new(),
            work: Vec::new(),
            why: None,
            by_live,
            has_twins: false,
        })
    }

    pub(crate) fn set_rule(&mut self, rule: FillRule, hidden: HiddenPrintPolicy) {
        self.rule = rule;
        self.hidden_policy = hidden;
    }

    /// Planned (exogenous) end of each shadow, input order (audit ruling A2): the earlier of its cancel effective
    /// time and its session close; never derived from the outcome.
    pub(crate) fn planned_ends(&self) -> Vec<(String, Option<Ts>, &'static str)> {
        self.specs
            .iter()
            .zip(&self.st)
            .map(|(spec, s)| {
                let close = self.instruments.at(spec.instrument_id, spec.ts_submit).map(|i| {
                    i.midnight_utc_ns
                        .saturating_add(i64::from(i.close_s).saturating_mul(SEC_NS))
                });
                let (t, kind) = match (s.cancel_eff, close) {
                    (Some(c), Some(e)) if e < c => (Some(e), "session_close"),
                    (Some(c), _) => (Some(c), "cancel"),
                    (None, Some(e)) => (Some(e), "session_close"),
                    (None, None) => (None, "none"),
                };
                (spec.shadow_id.clone(), t, kind)
            })
            .collect()
    }

    /// Index of an instrument's shadows (one-entry cache: records arrive in per-instrument runs).
    #[inline]
    fn slot(&self, id: u32) -> Option<usize> {
        let (i, k) = self.last_slot.get();
        if i == id && k != usize::MAX {
            return Some(k);
        }
        let k = *self.slots.get(&id)?;
        self.last_slot.set((id, k));
        Some(k)
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.st.is_empty()
    }

    pub(crate) fn has_instrument(&self, id: u32) -> bool {
        self.slot(id).is_some()
    }

    pub(crate) fn index(&self, id: &str) -> Option<u32> {
        self.by_id.get(id).copied()
    }

    pub(crate) fn spec(&self, idx: u32) -> &ShadowSpec {
        &self.specs[idx as usize]
    }

    /// Censors live shadows of `instrument_id` at the start of the first event after `ts_start` (feed gap).
    pub(crate) fn add_gap(&mut self, instrument_id: u32, ts_start: Ts) {
        if let Some(s) = self.slot(instrument_id).map(|k| &mut self.inst[k]) {
            s.agenda.push(Reverse((ts_start, K_GAP, 0, 0)));
        }
    }

    /// A trading halt `[start, end)` of `instrument_id` (SPEC 1.2; `Engine::set_trading_halts`): live shadows end
    /// `censored_gap` at `start` (at the start of the first event after it), and no shadow goes live in the span.
    pub(crate) fn add_halt(&mut self, instrument_id: u32, start: Ts, end: Ts) {
        let Some(s) = self.slot(instrument_id).map(|k| &mut self.inst[k]) else {
            return;
        };
        s.agenda.push(Reverse((start, K_HALT, 0, 0)));
        s.halts.push((start, end));
        s.halts.sort_unstable();
        let mut merged: Vec<(Ts, Ts)> = Vec::with_capacity(s.halts.len());
        for &(a, b) in &s.halts {
            match merged.last_mut() {
                Some(last) if a <= last.1 => last.1 = last.1.max(b),
                _ => merged.push((a, b)),
            }
        }
        s.halts = merged;
    }

    /// Whether `ts` falls in a trading halt of `instrument_id`.
    fn halted_at(&self, instrument_id: u32, ts: Ts) -> bool {
        self.slot(instrument_id).is_some_and(|k| self.inst[k].halted_at(ts))
    }

    pub(crate) fn state(&self, idx: u32) -> ShadowState {
        let s = &self.st[idx as usize];
        let live_seen = s.qa_live.is_some();
        ShadowState {
            status: s.status,
            price: s.price,
            ts_live: s.ts_live,
            queue_ahead: live_seen.then_some(s.qa),
            rank: live_seen.then_some(s.rank),
            level_qty: live_seen.then_some(s.level_qty),
            filled: s.filled,
            qty: s.qty,
            end_state: s.end_state,
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn traj(
        &mut self,
        sink: &mut dyn Sink,
        idx: u32,
        kind: TrajectoryKind,
        ts: Ts,
        sequence: u64,
        before: (Qty, u32, Qty),
        after: (Qty, u32, Qty),
        d: &StepOut,
    ) -> QtResult<()> {
        if self.trajectory == TrajectoryMode::None {
            return Ok(());
        }
        self.trajectory_rows += 1;
        sink.trajectory(&TrajectoryRow {
            shadow_id: self.specs[idx as usize].shadow_id.clone(),
            ts_event: ts,
            sequence,
            kind,
            queue_ahead_before: before.0,
            queue_ahead_after: after.0,
            rank_before: before.1,
            rank_after: after.1,
            level_qty_before: before.2,
            level_qty_after: after.2,
            cancel_ahead_qty: d.pure.min(d.qa_before_step),
            cancel_behind_qty: d.cancel_behind,
            add_behind_qty: d.add_behind,
            traded_at_px_qty: d.traded,
            trade_through_qty: d.crossed,
            filled_qty: self.st[idx as usize].filled,
        })
    }

    fn end_row(&mut self, sink: &mut dyn Sink, idx: u32, ts: Ts, sequence: u64) -> QtResult<()> {
        let s = &self.st[idx as usize];
        let st = (s.qa, s.rank, s.level_qty);
        self.traj(
            sink,
            idx,
            TrajectoryKind::End,
            ts,
            sequence,
            st,
            st,
            &StepOut::default(),
        )
    }

    /// Emits the buffered fills of one shadow with their liquidity (schema 1.1, A5 as refined by E48): only
    /// `crossed_at_live` is taker (the shadow arrives into a crossing book); a resting shadow crossed later
    /// (`crossed`) is the maker, like every other fill.
    fn emit_fills(&mut self, sink: &mut dyn Sink, idx: u32) -> QtResult<()> {
        if self.fills_buf.is_empty() {
            return Ok(());
        }
        let id = &self.specs[idx as usize].shadow_id;
        for mut r in self.fills_buf.drain(..) {
            r.shadow_id.clone_from(id);
            self.fills_rows += 1;
            let liq = crate::shadow_fill_liquidity(r.reason);
            self.diag.taker_fills += u64::from(liq == Liquidity::Taker);
            sink.shadow_fill_liquidity(&r, liq)?;
        }
        Ok(())
    }

    /// Level total at the last event end of a live shadow (its level's event-start total when the current event
    /// touched it, else the book's); the stored value for anyone else.
    fn known_level(&self, idx: u32, book: Option<&Book>) -> Qty {
        let s = &self.st[idx as usize];
        match (s.status, s.price, book) {
            (ShadowStatus::Live, Some(px), Some(b)) => self
                .slot(s.instrument_id)
                .and_then(|k| self.inst[k].lvl.get(&(s.side.as_byte(), px)))
                .map_or_else(|| b.level_total(s.side, px), |l| l.before),
            _ => s.level_qty,
        }
    }

    /// Ends a shadow outside the event-end step (cancel, amend, gap, clear, rejection).
    fn finish_one(
        &mut self,
        idx: u32,
        state: EndState,
        ts: Ts,
        sequence: u64,
        book: Option<&Book>,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        let level = self.known_level(idx, book);
        let s = &mut self.st[idx as usize];
        s.level_qty = level;
        let was_live = s.status == ShadowStatus::Live;
        let (side, price, iid) = (s.side, s.price, s.instrument_id);
        s.end(state, ts);
        if let Some(st) = self.slot(iid).map(|k| &mut self.inst[k]) {
            if was_live {
                if let Some(p) = price {
                    st.remove(idx, side, p);
                }
            }
            if self.collect_truth && was_live {
                st.ended.push(idx);
            }
        }
        self.end_row(sink, idx, ts, sequence)
    }

    fn activate(&mut self, idx: u32, ts_event: Ts, sequence: u64, book: &Book, sink: &mut dyn Sink) -> QtResult<()> {
        // Index of the event this activation starts.
        let iid = self.st[idx as usize].instrument_id;
        let (ev_now, itch, max_ref) = self.slot(iid).map_or((1, false, 0), |k| {
            (self.inst[k].events + 1, self.inst[k].itch, self.inst[k].max_ref)
        });
        let ref_prio = self.rule == FillRule::Exact && itch;
        let s = &mut self.st[idx as usize];
        s.ts_activated_event = Some(ts_event);
        s.act_ev = ev_now;
        if let PriceSpec::Touch { offset_ticks } = s.price_spec {
            let Some(b) = book.best_price(s.side) else {
                let ts_live = s.ts_live;
                return self.finish_one(idx, EndState::RejectedNoTouch, ts_live, sequence, None, sink);
            };
            let delta = i64::from(offset_ticks).checked_mul(s.tick);
            let px = delta.and_then(|d| match s.side {
                BookSide::Bid => b.checked_add(d),
                BookSide::Ask => b.checked_sub(d),
            });
            let Some(px) = px else {
                let ts_live = s.ts_live;
                return self.finish_one(idx, EndState::RejectedNoTouch, ts_live, sequence, None, sink);
            };
            s.price = Some(px);
            if offset_ticks > 0 && s.is_crossed(opposite_best(book, s.side)) {
                let ts_live = s.ts_live;
                return self.finish_one(idx, EndState::RejectedWouldCross, ts_live, sequence, None, sink);
            }
        }
        let Some(px) = s.price else {
            return Ok(());
        };
        let side = s.side;
        // A twin resting at the level (a priority-keeping `M` starting a leg) is the shadow itself, never ahead.
        let own = s
            .twin
            .and_then(|t| book.order(t))
            .filter(|&(os, op, _)| os == side && op == px)
            .map(|(_, _, o)| (o.order_id, o.size));
        let level = book.level(side, px).map(|mut l| {
            if let Some((_, q)) = own {
                l.total = l.total.saturating_sub(q);
                l.count = l.count.saturating_sub(1);
            }
            l
        });
        let skip = own.map(|o| o.0);
        s.activate(
            ts_event,
            level,
            book.orders_at(side, px)
                .map(|o| o.order_id)
                .filter(|&id| Some(id) != skip),
        );
        s.prio_ref = ref_prio.then_some(max_ref);
        // A twin-linked shadow entering at its twin's `A` (not yet on the book) was received at the twin's reference.
        if let Some(t) = s.twin.filter(|&t| ref_prio && book.order(t).is_none()) {
            s.bind_ref(t, book);
            s.qa_live = Some(s.qa);
            s.rank_live = Some(s.rank);
        }
        let ts_live = s.ts_live;
        let snap = (s.qa, s.rank, s.level_qty);
        self.traj(
            sink,
            idx,
            TrajectoryKind::Live,
            ts_live,
            sequence,
            snap,
            snap,
            &StepOut::default(),
        )?;
        let s = &mut self.st[idx as usize];
        if self.rule == FillRule::Exact && s.is_crossed(opposite_best(book, side)) {
            let ahead = s.ahead;
            let before = s.filled;
            let prio = s.priority_volume(book);
            let rows = s.taker_walk(
                book,
                prio,
                FillReason::CrossedAtLive,
                ts_live,
                sequence,
                ahead,
                0,
                0,
                0,
                &mut self.fills_buf,
            );
            self.diag.crossed_unfilled += u64::from(rows == 0);
            let q = s.filled - before;
            s.fill_at_live = q;
            s.last_cross = Some(ev_now);
            let (rank, level, done) = (s.rank, s.level_qty, s.status == ShadowStatus::Done);
            if q > 0 {
                if let Some(t) = self.why.as_mut().filter(|t| t.idx == idx) {
                    t.activation_fill(ts_live, sequence, side, px, q, ahead, rank, level);
                }
            }
            self.emit_fills(sink, idx)?;
            if done {
                if self.collect_truth {
                    if let Some(st) = self
                        .slot(self.st[idx as usize].instrument_id)
                        .map(|k| &mut self.inst[k])
                    {
                        st.activated.push(idx);
                        st.ended.push(idx);
                    }
                }
                return self.end_row(sink, idx, ts_live, sequence);
            }
        } else if self.rule == FillRule::Et && s.is_crossed(opposite_best(book, side)) {
            let ahead = s.ahead;
            let q = s.remaining();
            s.fill_at_live = q;
            self.fills_buf
                .extend(s.fill(q, FillReason::CrossedAtLive, ts_live, sequence, ahead, 0, 0, 0));
            if let Some(t) = self.why.as_mut().filter(|t| t.idx == idx) {
                t.activation_fill(ts_live, sequence, side, px, q, ahead, s.rank, s.level_qty);
            }
            self.emit_fills(sink, idx)?;
            if self.collect_truth {
                if let Some(st) = self
                    .slot(self.st[idx as usize].instrument_id)
                    .map(|k| &mut self.inst[k])
                {
                    st.activated.push(idx);
                    st.ended.push(idx);
                }
            }
            return self.end_row(sink, idx, ts_live, sequence);
        }
        let inst_id = self.st[idx as usize].instrument_id;
        if let Some(st) = self.slot(inst_id).map(|k| &mut self.inst[k]) {
            st.insert(idx, side, px);
            if self.collect_truth {
                st.activated.push(idx);
            }
        }
        Ok(())
    }

    /// Event start for `instrument_id` (first record `ts_event` = `t`): feed gaps due before `t` censor the live
    /// shadows; then, for each trading halt starting before `t` in time order, every shadow with an agenda item due
    /// is brought to the halt start (its work effective before it, in input order) and the live shadows end
    /// `censored_gap` there; then every shadow with an agenda item due (`< t`) is processed in input order. A
    /// pending shadow with `ts_live < t` first takes a cancel effective at or before `ts_live` (`cancelled`, never
    /// live), then its amends effective at or before `ts_live` in row order (`new_qty = 0`: `cancelled`, never live;
    /// else the new qty), then ends `censored_gap` at `ts_live` if that falls in a trading halt, else activates. A
    /// live shadow then applies its remaining amends effective before `t` in time order (`new_qty <= filled`:
    /// `amended_to_filled`) and the cancel effective before `t` at its place in that order (a tie goes to the
    /// cancel).
    #[inline]
    pub(crate) fn on_event_start(
        &mut self,
        instrument_id: u32,
        ts_event: Ts,
        sequence: u64,
        epoch: u64,
        book: &Book,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        let Some(st) = self.slot(instrument_id).map(|k| &mut self.inst[k]) else {
            return Ok(());
        };
        match st.agenda.peek() {
            Some(&Reverse((t, ..))) if t < ts_event => {
                self.event_start_work(instrument_id, ts_event, sequence, epoch, book, sink)
            }
            _ => Ok(()),
        }
    }

    /// The agenda part of an event start (kept out of line: the per-event fast path stays small).
    #[inline(never)]
    fn event_start_work(
        &mut self,
        instrument_id: u32,
        ts_event: Ts,
        sequence: u64,
        epoch: u64,
        book: &Book,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        let Some(st) = self.slot(instrument_id).map(|k| &mut self.inst[k]) else {
            return Ok(());
        };
        let mut due: Vec<u32> = Vec::new();
        let mut gaps: Vec<Ts> = Vec::new();
        let mut halts: Vec<Ts> = Vec::new();
        while let Some(&Reverse((t, kind, idx, _))) = st.agenda.peek() {
            if t >= ts_event {
                break;
            }
            st.agenda.pop();
            match kind {
                K_GAP => gaps.push(t),
                K_HALT => halts.push(t),
                _ => due.push(idx),
            }
        }
        for g in gaps {
            let live: Vec<u32> = self
                .slot(instrument_id)
                .map(|k| &self.inst[k])
                .map_or_else(Vec::new, |st| st.live.iter().copied().collect());
            for i in live {
                self.finish_one(i, EndState::CensoredGap, g, sequence, Some(book), sink)?;
            }
        }
        due.sort_unstable();
        due.dedup();
        // Trading halts (SPEC 1.2), in time order: the due work effective before the halt start, then the live
        // shadows end there (no event of the instrument came between, so the book is the one they rested on).
        halts.dedup();
        for h in halts {
            for &idx in &due {
                self.start_one(idx, ts_event, h, sequence, epoch, book, sink)?;
            }
            let live: Vec<u32> = self
                .slot(instrument_id)
                .map(|k| &self.inst[k])
                .map_or_else(Vec::new, |st| st.live.iter().copied().collect());
            for i in live {
                self.finish_one(i, EndState::CensoredGap, h, sequence, Some(book), sink)?;
            }
        }
        for idx in due {
            self.start_one(idx, ts_event, ts_event, sequence, epoch, book, sink)?;
        }
        Ok(())
    }

    /// A shadow's agenda work effective before `until` (`<= t`, the start of the event at `t`; `until < t` only to
    /// bring it to a trading-halt start): a pending shadow with `ts_live < until` takes a cancel or amends effective
    /// at or before `ts_live`, ends `censored_gap` at `ts_live` if that falls in a trading halt, else activates on
    /// the event at `t`; a live shadow applies its amends and cancel effective before `until`.
    #[allow(clippy::too_many_arguments)]
    fn start_one(
        &mut self,
        idx: u32,
        t: Ts,
        until: Ts,
        sequence: u64,
        epoch: u64,
        book: &Book,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        let s = &mut self.st[idx as usize];
        if s.status == ShadowStatus::Pending && s.ts_live < until {
            if let Some(c) = s.cancel_eff.filter(|&c| c <= s.ts_live) {
                return self.finish_one(idx, EndState::Cancelled, c, sequence, Some(book), sink);
            }
            while s.next_amend < s.amends.len() && s.amends[s.next_amend].0 <= s.ts_live {
                let (a, q) = s.amends[s.next_amend];
                s.next_amend += 1;
                if q == 0 {
                    return self.finish_one(idx, EndState::Cancelled, a, sequence, Some(book), sink);
                }
                s.qty = q;
            }
            let (iid, ts_live) = (s.instrument_id, s.ts_live);
            if self.halted_at(iid, ts_live) {
                return self.finish_one(idx, EndState::CensoredGap, ts_live, sequence, Some(book), sink);
            }
            self.st[idx as usize].epoch = epoch;
            self.activate(idx, t, sequence, book, sink)?;
        }
        let s = &mut self.st[idx as usize];
        if s.status == ShadowStatus::Live {
            // Due amends and the cancel in time order; the cancel wins a tie (review m12).
            let cancel = s.cancel_eff.filter(|&c| c < until);
            while s.next_amend < s.amends.len() && s.amends[s.next_amend].0 < until {
                let (a, q) = s.amends[s.next_amend];
                if cancel.is_some_and(|c| c <= a) {
                    break;
                }
                s.next_amend += 1;
                if q <= s.filled {
                    return self.finish_one(idx, EndState::AmendedToFilled, a, sequence, Some(book), sink);
                }
                s.qty = q;
            }
            if let Some(c) = cancel {
                return self.finish_one(idx, EndState::Cancelled, c, sequence, Some(book), sink);
            }
        }
        Ok(())
    }

    /// Audit ruling A1, self-exclusion: links the excluded real order `order_id` to the shadow that replaces it, at
    /// one of the order's `A` / `M` records (`ts`, `sequence`, at (`side`, `price`) with `size`; called before the
    /// record applies). The shadow is the first in input order, preferring an equal quantity, among the unlinked
    /// abs-price shadows of the instrument on that side and price that are either pending with `ts_live = ts` (it
    /// activates now, in the twin's queue place) or went live at the start of this very event with `ts_live = ts -
    /// 1` (the scorer's 1 ns alignment to a twin whose record opens its timestamp). Returns whether a shadow was
    /// linked.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn link_twin(
        &mut self,
        instrument_id: u32,
        order_id: u64,
        ts: Ts,
        sequence: u64,
        side: BookSide,
        price: Px,
        size: Qty,
        epoch: u64,
        book: &Book,
        sink: &mut dyn Sink,
    ) -> QtResult<bool> {
        let Some(k) = self.slot(instrument_id) else {
            return Ok(false);
        };
        let ev_now = self.inst[k].events + 1;
        let mut best: Option<(bool, u32)> = None;
        for (t, pending) in [(ts, true), (ts.saturating_sub(1), false)] {
            for &i in self.by_live.get(&(instrument_id, t)).map_or(&[][..], Vec::as_slice) {
                let s = &self.st[i as usize];
                let ok = if pending {
                    s.status == ShadowStatus::Pending
                } else {
                    s.status == ShadowStatus::Live && s.act_ev == ev_now
                };
                if !ok || s.twin.is_some() || s.side != side || s.price != Some(price) {
                    continue;
                }
                let key = (s.qty != size, i);
                if best.is_none_or(|b| key < b) {
                    best = Some(key);
                }
            }
        }
        let Some((_, idx)) = best else {
            return Ok(false);
        };
        self.has_twins = true;
        let s = &mut self.st[idx as usize];
        s.twin = Some(order_id);
        if s.status == ShadowStatus::Pending {
            self.start_twin(idx, ts, sequence, epoch, book, sink)?;
        } else if s.ahead_set.remove(&order_id) {
            // Went live at this event's start with the twin resting at its level (a leg at a priority-keeping `M`).
            let q = book.order(order_id).map_or(0, |(_, _, o)| o.size);
            s.exact_qa = s.exact_qa.saturating_sub(q);
            s.ahead = s.ahead.saturating_sub(q);
            s.qa = s.qa.saturating_sub(q);
            s.rank = s.ahead_set.len() as u32;
        } else if s.prio_ref.is_some() && book.order(order_id).is_none() {
            // Went live at this event's start; the twin enters now by its `A`, at its own reference (D1).
            s.bind_ref(order_id, book);
        }
        Ok(true)
    }

    /// Activates a pending twin-linked shadow at its twin's record (`link_twin`): the pending part of `start_one`
    /// (a cancel or amends effective at or before `ts_live`), then activation on the book before the record. When
    /// the record is inside an event, the level activity the event already had is kept aside (`Shadow::lvl_base`),
    /// so it is not behind volume for the shadow; the shadow takes part in this event's end step.
    fn start_twin(
        &mut self,
        idx: u32,
        t: Ts,
        sequence: u64,
        epoch: u64,
        book: &Book,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        let s = &mut self.st[idx as usize];
        if let Some(c) = s.cancel_eff.filter(|&c| c <= s.ts_live) {
            return self.finish_one(idx, EndState::Cancelled, c, sequence, Some(book), sink);
        }
        while s.next_amend < s.amends.len() && s.amends[s.next_amend].0 <= s.ts_live {
            let (a, q) = s.amends[s.next_amend];
            s.next_amend += 1;
            if q == 0 {
                return self.finish_one(idx, EndState::Cancelled, a, sequence, Some(book), sink);
            }
            s.qty = q;
        }
        let (iid, ts_live) = (s.instrument_id, s.ts_live);
        if self.halted_at(iid, ts_live) {
            // SPEC 1.2: never live in a halt; the twin stays on the tape, linked to its ended shadow.
            return self.finish_one(idx, EndState::CensoredGap, ts_live, sequence, Some(book), sink);
        }
        self.st[idx as usize].epoch = epoch;
        self.activate(idx, t, sequence, book, sink)?;
        let by_seq = self.by_sequence;
        let s = &self.st[idx as usize];
        if s.status != ShadowStatus::Live {
            return Ok(());
        }
        let (iid, side, px) = (s.instrument_id, s.side, s.price.unwrap_or_default());
        let Some(k) = self.slot(iid) else {
            return Ok(());
        };
        let st = &mut self.inst[k];
        let s = &mut self.st[idx as usize];
        s.lvl_base = st.lvl.get(&(side.as_byte(), px)).map(|l| LevelAcc {
            before: book.level_total(side, px),
            ..l.clone()
        });
        Self::touch(s, idx, &mut st.dirty, epoch, by_seq);
        Ok(())
    }

    #[inline]
    fn touch(s: &mut Shadow, idx: u32, dirty: &mut Vec<u32>, epoch: u64, by_sequence: bool) {
        if by_sequence && s.epoch != epoch {
            s.ev_filled.clear();
            s.epoch = epoch;
        }
        if !s.dirty {
            s.dirty = true;
            dirty.push(idx);
        }
    }

    /// Every shadow at the levels of the event's records is processed at event end (truth or `full` trajectory).
    fn full_rows(&self) -> bool {
        self.collect_truth || self.trajectory == TrajectoryMode::Full
    }

    /// A removal of `order_id` from (side, price): ahead shadows lose the queue in front of them; for everyone else
    /// it is level-wide behind activity.
    #[allow(clippy::too_many_arguments)]
    fn removal(&mut self, inst: u32, order_id: u64, side: BookSide, price: Px, qty: Qty, take: bool, epoch: u64) {
        let full = self.full_rows();
        let Some(st) = self.slot(inst).map(|k| &mut self.inst[k]) else {
            return;
        };
        let by_seq = self.by_sequence;
        let InstShadows {
            bids, asks, dirty, lvl, ..
        } = st;
        let list = level_of(bids, asks, side, price);
        if list.is_empty() {
            return;
        }
        if let Some(a) = lvl.get_mut(&(side.as_byte(), price)) {
            bump(&mut a.removed, order_id, qty);
        }
        for &i in list {
            let s = &mut self.st[i as usize];
            let ahead = if take {
                s.ahead_set.remove(&order_id)
            } else {
                s.ahead_set.contains(&order_id)
            };
            if ahead {
                Self::touch(s, i, dirty, epoch, by_seq);
                bump(&mut s.ev_removed, order_id, qty);
                s.exact_qa = s.exact_qa.saturating_sub(qty);
            } else if full {
                Self::touch(s, i, dirty, epoch, by_seq);
            }
        }
    }

    /// A duplicate add of an order inside a flagged snapshot (or under the et rule): an order ahead stays ahead with
    /// its new size (flagged snapshots re-add the book in FIFO order); for any other order only the add counts
    /// (behind). A re-add at the same level is not behind volume for the shadows it stays ahead of.
    #[allow(clippy::too_many_arguments)]
    fn refresh(
        &mut self,
        inst: u32,
        order_id: u64,
        side: BookSide,
        price: Px,
        old: Qty,
        new: Qty,
        epoch: u64,
        same_level: bool,
    ) {
        let Some(st) = self.slot(inst).map(|k| &mut self.inst[k]) else {
            return;
        };
        let by_seq = self.by_sequence;
        let InstShadows { bids, asks, dirty, .. } = st;
        for &i in level_of(bids, asks, side, price) {
            let s = &mut self.st[i as usize];
            if s.ahead_set.contains(&order_id) {
                Self::touch(s, i, dirty, epoch, by_seq);
                s.exact_qa = s.exact_qa.saturating_sub(old).saturating_add(new);
                if same_level {
                    s.ev_add_ahead = s.ev_add_ahead.saturating_add(new);
                }
            }
        }
    }

    /// Volume added at (side, price) by `order_id`: behind every shadow there (level-wide), except the shadow it is
    /// the twin of (A1: its own volume) and, for a new order (`fresh`: an `A`, not a re-add or a priority-losing
    /// modify) on an `order_ref_priority` instrument, the shadows whose `prio_ref` is above its reference: it was
    /// received before them, so it joins their queue ahead (`exact_qa`, `ahead_set`).
    #[allow(clippy::too_many_arguments)]
    fn add_behind(&mut self, inst: u32, order_id: u64, side: BookSide, price: Px, qty: Qty, fresh: bool, epoch: u64) {
        let full = self.full_rows();
        let twins = self.has_twins;
        let ref_prio = fresh && self.rule == FillRule::Exact;
        let Some(st) = self.slot(inst).map(|k| &mut self.inst[k]) else {
            return;
        };
        // Released out of reference order (rare: normally every `A` carries the largest reference so far).
        let late = ref_prio && st.itch && order_id < st.max_ref;
        let by_seq = self.by_sequence;
        let InstShadows {
            bids, asks, dirty, lvl, ..
        } = st;
        let list = level_of(bids, asks, side, price);
        if list.is_empty() {
            return;
        }
        if let Some(a) = lvl.get_mut(&(side.as_byte(), price)) {
            a.add = a.add.saturating_add(qty);
        }
        if full || twins || late {
            for &i in list {
                let s = &mut self.st[i as usize];
                let own = s.twin == Some(order_id);
                let ahead = !own && late && s.prio_ref.is_some_and(|b| order_id < b);
                if own || ahead {
                    s.ev_add_ahead = s.ev_add_ahead.saturating_add(qty);
                }
                if ahead {
                    s.ahead_set.insert(order_id);
                    s.exact_qa = s.exact_qa.saturating_add(qty);
                }
                if full || own || ahead {
                    Self::touch(s, i, dirty, epoch, by_seq);
                }
            }
        }
    }

    /// Creates the accumulators of the levels a record changes (before its accounting).
    fn prepare_levels(&mut self, inst: u32, effect: &Effect, dup: Option<(BookSide, Px, Qty)>, book: &Book) {
        let Some(st) = self.slot(inst).map(|k| &mut self.inst[k]) else {
            return;
        };
        let mut d: [(Option<(BookSide, Px)>, i128); 3] = [(None, 0); 3];
        if let Some((side, px, old)) = dup {
            d[0] = (Some((side, px)), -i128::from(old));
        }
        match *effect {
            Effect::Add { side, price, size, .. } => d[1] = (Some((side, price)), i128::from(size)),
            Effect::Remove { side, price, size, .. } => d[1] = (Some((side, price)), -i128::from(size)),
            Effect::Reduce { side, price, by, .. } => d[1] = (Some((side, price)), -i128::from(by)),
            Effect::Replace {
                side,
                old_price,
                old_size,
                new_price,
                new_size,
                ..
            } => {
                d[1] = (Some((side, old_price)), -i128::from(old_size));
                d[2] = (Some((side, new_price)), i128::from(new_size));
            }
            Effect::None | Effect::Clear => {}
        }
        for k in 0..3 {
            let Some((side, px)) = d[k].0 else {
                continue;
            };
            if level_of(&st.bids, &st.asks, side, px).is_empty() || st.lvl.contains_key(&(side.as_byte(), px)) {
                continue;
            }
            let delta: i128 = d.iter().filter(|x| x.0 == Some((side, px))).map(|x| x.1).sum();
            acc_of(&mut st.lvl, book, side, px, delta);
        }
    }

    /// Whether a record matters to the traced shadow (before the record's accounting).
    fn trace_record(&mut self, e: &MboEvent, effect: &Effect, fresh: bool) {
        let Some(t) = self.why.as_mut() else {
            return;
        };
        let s = &self.st[t.idx as usize];
        if s.status != ShadowStatus::Live || s.instrument_id != e.instrument_id {
            return;
        }
        if e.order_id != 0 && s.twin == Some(e.order_id) {
            // A1: the twin's records are the shadow's own (its fills show on the event's row).
            return;
        }
        let Some(px) = s.price else {
            return;
        };
        // An `A` released with a reference older than the shadow joins its queue ahead (D1, `add_behind`), although
        // it is added to `ahead_set` only after this trace.
        let joins = match *effect {
            Effect::Add {
                order_id, side, price, ..
            } => {
                fresh
                    && self.rule == FillRule::Exact
                    && side == s.side
                    && price == px
                    && s.prio_ref.is_some_and(|b| order_id < b)
            }
            _ => false,
        };
        let is_ahead = joins || (e.order_id != 0 && s.ahead_set.contains(&e.order_id));
        let level_touched = match *effect {
            Effect::Add { side, price, .. }
            | Effect::Remove { side, price, .. }
            | Effect::Reduce { side, price, .. } => side == s.side && price == px,
            Effect::Replace {
                side,
                old_price,
                new_price,
                ..
            } => side == s.side && (old_price == px || new_price == px),
            _ => false,
        };
        let through = e.action == b'T'
            && match s.side {
                BookSide::Bid => e.price < px,
                BookSide::Ask => e.price > px,
            };
        let relevant = is_ahead || level_touched || e.price == px || through;
        t.record(e, effect, is_ahead, relevant);
    }

    /// One record of `e.instrument_id` after the book applied it. `dup`: the resting order a duplicate add replaced.
    #[inline]
    pub(crate) fn on_record(
        &mut self,
        e: &MboEvent,
        effect: &Effect,
        dup: Option<(BookSide, Px, Qty)>,
        book: &Book,
        epoch: u64,
    ) {
        let Some(k) = self.slot(e.instrument_id) else {
            return;
        };
        let st = &self.inst[k];
        if !st.live.is_empty() && (self.why.is_some() || self.relevant(st, e, effect, dup, book)) {
            self.record_work(e, effect, dup, book, epoch);
        }
        if self.rule == FillRule::Exact {
            self.track(k, e, effect, book);
        }
    }

    /// Per-record instrument state of the `Exact` rule on `itch` instruments, kept whether or not a shadow is live:
    /// the largest order reference added (D1) and the open execution sweep of each resting side (hidden prints, D2
    /// and D3). Elsewhere no sweep is ever open, so a hidden print's side is always inferred from the shadow.
    #[inline]
    fn track(&mut self, k: usize, e: &MboEvent, effect: &Effect, book: &Book) {
        let st = &mut self.inst[k];
        if !st.itch {
            return;
        }
        if let Effect::Add { order_id, .. } = *effect {
            st.max_ref = st.max_ref.max(order_id);
        }
        if !self.hidden || self.hidden_policy == HiddenPrintPolicy::Ignore {
            return;
        }
        let close = |sw: &mut Sweep| {
            sw.open = false;
            sw.execs.clear();
        };
        match *effect {
            Effect::Add { side, .. } | Effect::Replace { side, .. } => close(&mut st.sweep[side_ix(side)]),
            Effect::Remove { order_id, side, .. } | Effect::Reduce { order_id, side, .. } => {
                let sw = &mut st.sweep[side_ix(side)];
                let by_seq = self.by_sequence;
                if sw.open
                    && !sw
                        .execs
                        .iter()
                        .any(|&(id, q)| id == order_id && (!by_seq || q == e.sequence))
                {
                    close(sw);
                }
            }
            Effect::Clear => st.sweep.iter_mut().for_each(close),
            Effect::None => {
                if e.action == Action::Fill.as_byte() {
                    if let Some((side, _, _)) = book.order(e.order_id) {
                        let sw = &mut st.sweep[side_ix(side)];
                        if !(sw.open && sw.ts == e.ts_event) {
                            sw.execs.clear();
                        }
                        sw.open = true;
                        sw.ts = e.ts_event;
                        sw.execs.push((e.order_id, e.sequence));
                    }
                }
            }
        }
    }

    /// Whether a record can matter to a live shadow of the instrument: it changes or executes at a level with
    /// shadows, executes through the price of a shadow on its side, prints at a shadow's price, or is a print the
    /// et rule or the hidden-print policy looks at.
    #[inline]
    fn relevant(
        &self,
        st: &InstShadows,
        e: &MboEvent,
        effect: &Effect,
        dup: Option<(BookSide, Px, Qty)>,
        book: &Book,
    ) -> bool {
        let at = |side: BookSide, px: Px| !level_of(&st.bids, &st.asks, side, px).is_empty();
        let better = |side: BookSide, px: Px| match side {
            BookSide::Bid => st.bids.keys().next_back().is_some_and(|&p| p > px),
            BookSide::Ask => st.asks.keys().next().is_some_and(|&p| p < px),
        };
        if dup.is_some_and(|(side, px, _)| at(side, px)) {
            return true;
        }
        let by_effect = match *effect {
            Effect::Add { side, price, .. }
            | Effect::Remove { side, price, .. }
            | Effect::Reduce { side, price, .. } => at(side, price),
            Effect::Replace {
                side,
                old_price,
                new_price,
                ..
            } => at(side, old_price) || at(side, new_price),
            Effect::None | Effect::Clear => false,
        };
        if by_effect {
            return true;
        }
        match Action::from_byte(e.action) {
            Some(Action::Fill) => book
                .order(e.order_id)
                .is_some_and(|(side, px, _)| at(side, px) || better(side, px)),
            Some(Action::Trade) => {
                self.rule == FillRule::Et
                    || (e.side == b'N' && self.hidden && self.hidden_policy != HiddenPrintPolicy::Ignore)
                    || at(BookSide::Bid, e.price)
                    || at(BookSide::Ask, e.price)
            }
            _ => false,
        }
    }

    /// The accounting part of `on_record` (out of line).
    #[inline(never)]
    fn record_work(
        &mut self,
        e: &MboEvent,
        effect: &Effect,
        dup: Option<(BookSide, Px, Qty)>,
        book: &Book,
        epoch: u64,
    ) {
        let inst = e.instrument_id;
        if self.why.is_some() {
            self.trace_record(e, effect, dup.is_none());
        }
        self.prepare_levels(inst, effect, dup, book);
        if let Some((side, price, old)) = dup {
            let in_snapshot = self.snapshot_flagged && e.flags & qt_types::flags::SNAPSHOT != 0;
            let same_level = matches!(*effect, Effect::Add { side: ns, price: np, .. } if ns == side && np == price);
            if self.rule == FillRule::Et || (in_snapshot && same_level) {
                self.refresh(inst, e.order_id, side, price, old, e.size, epoch, same_level);
            } else {
                // A flagged re-add at another price leaves the level like a priority-losing modify (review m15).
                // Ruling 36d: outside a flagged snapshot a duplicate add follows the book (removed, then appended at
                // the tail), so an order ahead leaves the queue ahead like a priority-losing modify.
                self.removal(inst, e.order_id, side, price, old, true, epoch);
            }
        }
        match *effect {
            Effect::Add {
                order_id,
                side,
                price,
                size,
            } => self.add_behind(inst, order_id, side, price, size, dup.is_none(), epoch),
            Effect::Remove {
                order_id,
                side,
                price,
                size,
            } => self.removal(inst, order_id, side, price, size, true, epoch),
            Effect::Reduce {
                order_id,
                side,
                price,
                by,
            } => self.removal(inst, order_id, side, price, by, false, epoch),
            Effect::Replace {
                order_id,
                side,
                old_price,
                old_size,
                new_price,
                new_size,
            } => {
                self.removal(inst, order_id, side, old_price, old_size, true, epoch);
                self.add_behind(inst, order_id, side, new_price, new_size, false, epoch);
            }
            Effect::None | Effect::Clear => {}
        }
        match Action::from_byte(e.action) {
            Some(Action::Fill) => {
                if let Some((side, price, _)) = book.order(e.order_id) {
                    self.execution(inst, Some(e.order_id), side, price, e.price, e.size, book, epoch);
                }
            }
            Some(Action::Trade) => {
                let aggr = e.side();
                if aggr == Side::None && self.hidden {
                    if self.rule == FillRule::Exact && self.hidden_policy != HiddenPrintPolicy::Ignore {
                        self.hidden_print(inst, e.price, e.size, e.ts_event, book, epoch);
                    }
                    return;
                }
                let et = self.rule == FillRule::Et;
                let Some(st) = self.slot(inst).map(|k| &mut self.inst[k]) else {
                    return;
                };
                let (p, q, by_seq) = (e.price, e.size, self.by_sequence);
                for side in [BookSide::Bid, BookSide::Ask] {
                    if aggr != side.to_side() && !level_of(&st.bids, &st.asks, side, p).is_empty() {
                        let a = acc_of(&mut st.lvl, book, side, p, 0);
                        a.traded = a.traded.saturating_add(q);
                    }
                }
                if !et {
                    return;
                }
                // et rule: at-price prints deplete the queue, prints through the price fill.
                let mut hit = |list: &Vec<u32>, at_price: bool, st_dirty: &mut Vec<u32>| {
                    for &i in list {
                        let s = &mut self.st[i as usize];
                        Self::touch(s, i, st_dirty, epoch, by_seq);
                        if !at_price {
                            s.ev_crossed = s.ev_crossed.saturating_add(q);
                        }
                    }
                };
                let InstShadows { bids, asks, dirty, .. } = st;
                if aggr != Side::Bid {
                    for (&px, list) in bids.range(p..) {
                        hit(list, px == p, dirty);
                    }
                }
                if aggr != Side::Ask {
                    for (&px, list) in asks.range(..=p) {
                        hit(list, px == p, dirty);
                    }
                }
            }
            _ => {}
        }
    }

    /// An execution of a resting order at (side, price) at the execution price `exec_px`: ahead/behind accounting
    /// at its level, and trade-through volume for shadows on that side strictly better than both the resting price
    /// and the execution price (A4). Oracle ruling A4b: an execution of an order behind the shadow at a price other
    /// than its displayed price (price-improved / off-display) never fills the shadow; it only pairs with the
    /// order's removal. `None` = an excluded own order (A1): its volume at its level counts once the queue ahead is
    /// exhausted (like hidden volume); through the price it is a trade-through like any execution.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn execution(
        &mut self,
        inst: u32,
        order_id: Option<u64>,
        side: BookSide,
        price: Px,
        exec_px: Px,
        size: Qty,
        book: &Book,
        epoch: u64,
    ) {
        let Some(st) = self.slot(inst).map(|k| &mut self.inst[k]) else {
            return;
        };
        let by_seq = self.by_sequence;
        let InstShadows {
            bids, asks, dirty, lvl, ..
        } = st;
        let list = level_of(bids, asks, side, price);
        if let (Some(id), false) = (order_id, list.is_empty()) {
            let a = acc_of(lvl, book, side, price, 0);
            bump(&mut a.filled, id, size);
            if exec_px != price {
                bump(&mut a.off_price, id, size);
            }
        }
        for &i in list {
            let s = &mut self.st[i as usize];
            Self::touch(s, i, dirty, epoch, by_seq);
            match order_id {
                Some(id) if s.ahead_set.contains(&id) => {
                    bump(&mut s.ev_filled, id, size);
                    s.ev_ahead_fill.push(id);
                }
                // A1: the shadow's own twin executed; the shadow fills at the execution's price.
                Some(id) if s.twin == Some(id) => s.ev_twin.push((size, exec_px)),
                Some(_) => {}
                // A4b for own volume too (review m19): only an execution at its displayed price.
                None if exec_px == price => s.ev_own_at = s.ev_own_at.saturating_add(size),
                None => {}
            }
        }
        // Strictly better than the resting price and than the execution price.
        let bound = match side {
            BookSide::Bid => price.max(exec_px),
            BookSide::Ask => price.min(exec_px),
        };
        let better: Vec<u32> = match side {
            BookSide::Bid => bids
                .range((std::ops::Bound::Excluded(bound), std::ops::Bound::Unbounded))
                .flat_map(|(_, l)| l.iter().copied())
                .collect(),
            BookSide::Ask => asks.range(..bound).flat_map(|(_, l)| l.iter().copied()).collect(),
        };
        for i in better {
            let s = &mut self.st[i as usize];
            Self::touch(s, i, dirty, epoch, by_seq);
            s.ev_exec_through = s.ev_exec_through.saturating_add(size);
        }
    }

    /// A hidden (side-`N`) print at `p` (A3 as amended by the oracle ruling A3d). It is usable only when the book
    /// at the print shows no displayed interest at or through `p` on either side (no bid `>= p`, no ask `<= p`: a
    /// non-displayed order there ranks behind any displayed one, and a locked or crossed price is not continuous
    /// matching). A usable print reaches the shadows priced at or better than `p` on its resting side: at `p` it is
    /// volume behind the displayed queue (used once the queue ahead is exhausted, `queue_depleted`); strictly through
    /// `p` it is a trade-through (`BehindAndThrough`). The resting side is the side of the open execution sweep at
    /// the print's `ts_event` (the aggressor that just executed displayed orders on that side and continued into
    /// hidden interest there; `Sweep`), and then `p` may be off the tick grid (a midpoint execution the aggressor
    /// reached). Without exactly one such sweep (always, on instruments that are not `itch`: D2 and D3 are validated
    /// on Nasdaq ITCH data only) the side is inferred from the shadow (either side) and `p` must be on the tick
    /// grid. Declined prints at a shadow's price count `hidden_at_px_with_display_ahead`.
    fn hidden_print(&mut self, inst: u32, p: Px, size: Qty, ts: Ts, book: &Book, epoch: u64) {
        let through = self.hidden_policy == HiddenPrintPolicy::BehindAndThrough;
        let Some(k) = self.slot(inst) else {
            return;
        };
        let st = &mut self.inst[k];
        let open = |i: usize| st.sweep[i].open && st.sweep[i].ts == ts;
        let (bid_ok, ask_ok) = match (open(0), open(1)) {
            (true, false) => (true, false),
            (false, true) => (false, true),
            _ => (true, true),
        };
        // A print continuing a sweep is that aggressor's volume (its limit is at or beyond `p`), so an off-grid `p`
        // (a midpoint execution) still trades through a shadow it passes; with no sweep an off-grid print may be two
        // non-displayed orders meeting at the midpoint and is never used.
        let swept = bid_ok != ask_ok;
        let by_seq = self.by_sequence;
        let usable = book.best_price(BookSide::Bid).is_none_or(|b| b < p)
            && book.best_price(BookSide::Ask).is_none_or(|a| a > p);
        let InstShadows { bids, asks, dirty, .. } = st;
        if !usable {
            let n = level_of(bids, asks, BookSide::Bid, p).len() + level_of(bids, asks, BookSide::Ask, p).len();
            self.diag.hidden_at_px_with_display_ahead += n as u64;
            return;
        }
        let mut at: Vec<u32> = Vec::new();
        let mut thr: Vec<u32> = Vec::new();
        let empty = BTreeMap::new();
        for (&px, l) in (if bid_ok { &*bids } else { &empty }).range(p..) {
            if px == p {
                at.extend_from_slice(l);
            } else if through {
                thr.extend_from_slice(l);
            }
        }
        for (&px, l) in (if ask_ok { &*asks } else { &empty }).range(..=p) {
            if px == p {
                at.extend_from_slice(l);
            } else if through {
                thr.extend_from_slice(l);
            }
        }
        for (list, is_at) in [(at, true), (thr, false)] {
            for i in list {
                let s = &mut self.st[i as usize];
                if !swept && p.rem_euclid(s.tick.max(1)) != 0 {
                    continue;
                }
                Self::touch(s, i, dirty, epoch, by_seq);
                if is_at {
                    s.ev_hidden_at = s.ev_hidden_at.saturating_add(size);
                } else {
                    s.ev_hidden_through = s.ev_hidden_through.saturating_add(size);
                }
            }
        }
    }

    /// Ends every live shadow of the instrument (clear, gap, bad book) at a record.
    pub(crate) fn censor(
        &mut self,
        instrument_id: u32,
        state: EndState,
        ts: Ts,
        sequence: u64,
        book: &Book,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        let live: Vec<u32> = match self.slot(instrument_id).map(|k| &self.inst[k]) {
            Some(st) => st.live.iter().copied().collect(),
            None => return Ok(()),
        };
        for i in live {
            self.finish_one(i, state, ts, sequence, Some(book), sink)?;
        }
        Ok(())
    }

    /// Event end for `instrument_id` (book consistent).
    #[inline]
    pub(crate) fn on_event_end(
        &mut self,
        instrument_id: u32,
        ts: Ts,
        sequence: u64,
        epoch: u64,
        book: &Book,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        if !self.truth.is_empty() {
            self.truth.clear();
        }
        let Some(st) = self.slot(instrument_id).map(|k| &mut self.inst[k]) else {
            return Ok(());
        };
        st.events += 1;
        // Fast path: nothing touched, nothing started or ended, and the book crosses no shadow.
        let (best_bid, best_ask) = (book.best_price(BookSide::Bid), book.best_price(BookSide::Ask));
        let crossed = best_ask.is_some_and(|a| st.bids.keys().next_back().is_some_and(|&p| p >= a))
            || best_bid.is_some_and(|b| st.asks.keys().next().is_some_and(|&p| p <= b));
        if !self.collect_truth
            && !crossed
            && st.dirty.is_empty()
            && st.lvl.is_empty()
            && st.activated.is_empty()
            && st.ended.is_empty()
            && self.why.as_ref().is_none_or(|t| !t.has_pending())
        {
            return Ok(());
        }
        self.event_end_work(instrument_id, ts, sequence, epoch, book, crossed, sink)
    }

    /// The processing part of an event end (out of line).
    #[allow(clippy::too_many_arguments)]
    #[inline(never)]
    fn event_end_work(
        &mut self,
        instrument_id: u32,
        ts: Ts,
        sequence: u64,
        epoch: u64,
        book: &Book,
        crossed: bool,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        let (best_bid, best_ask) = (book.best_price(BookSide::Bid), book.best_price(BookSide::Ask));
        let Some(st) = self.slot(instrument_id).map(|k| &mut self.inst[k]) else {
            return Ok(());
        };
        let ev_now = st.events;
        let mut work = std::mem::take(&mut self.work);
        work.clear();
        if self.collect_truth {
            work.extend(st.live.iter().copied());
        } else {
            work.append(&mut st.dirty);
            if crossed {
                if let Some(a) = best_ask {
                    for list in st.bids.range(a..).map(|(_, l)| l) {
                        work.extend_from_slice(list);
                    }
                }
                if let Some(b) = best_bid {
                    for list in st.asks.range(..=b).map(|(_, l)| l) {
                        work.extend_from_slice(list);
                    }
                }
            }
            if self.trajectory == TrajectoryMode::Full {
                for &(sb, px) in st.lvl.keys() {
                    if let Some(side) = BookSide::from_byte(sb) {
                        work.extend_from_slice(level_of(&st.bids, &st.asks, side, px));
                    }
                }
            }
            work.sort_unstable();
            work.dedup();
        }
        st.dirty.clear();
        let lvl = std::mem::take(&mut st.lvl);
        let activated = std::mem::take(&mut st.activated);
        let ended = std::mem::take(&mut st.ended);
        let by_seq = self.by_sequence;
        for &i in &work {
            let s = &mut self.st[i as usize];
            if s.status != ShadowStatus::Live {
                continue;
            }
            if by_seq && s.epoch != epoch {
                s.ev_filled.clear();
                s.epoch = epoch;
            }
            let Some(px) = s.price else {
                continue;
            };
            let side = s.side;
            let level_after = book.level_total(side, px);
            let lv = lvl.get(&(side.as_byte(), px));
            let lvl_before = s.lvl_base.as_ref().map(|b| b.before).or(lv.map(|l| l.before));
            let before = (s.qa, s.rank, lvl_before.unwrap_or(level_after));
            let d = match self.rule {
                FillRule::Exact => s.step_exact(ts, sequence, by_seq, book, level_after, lv, &mut self.fills_buf),
                FillRule::Et => s.step_et(
                    ts,
                    sequence,
                    by_seq,
                    opposite_best(book, side),
                    level_after,
                    lv,
                    &mut self.fills_buf,
                ),
            };
            self.diag.hidden_print_fills += u64::from(d.hidden_fill);
            self.diag.hidden_at_px_with_display_ahead += u64::from(d.hidden_blocked);
            if d.crossed_book {
                // One count per run of crossed events (review m14).
                let fresh = !s.last_cross.is_some_and(|x| x + 1 >= ev_now);
                self.diag.crossed_unfilled += u64::from(d.crossed_unfilled && fresh);
                s.last_cross = Some(ev_now);
            }
            self.diag.behind_exec_with_ahead += u64::from(d.behind_with_ahead && d.filled_now > 0);
            self.diag.twin_fills_with_ahead += d.twin_with_ahead;
            self.diag.twin_fills_off_display += d.twin_off_display;
            let after = (d.qa_after, s.rank, level_after);
            let done = s.status == ShadowStatus::Done;
            if let Some(t) = self.why.as_mut().filter(|t| t.idx == i) {
                t.end_event(after.0, after.1, after.2, d.filled_now, &self.fills_buf);
            }
            self.emit_fills(sink, i)?;
            let changed = after.0 != before.0 || after.1 != before.1 || d.filled_now > 0;
            let full_extra = after.2 != before.2 || d.cancel_behind > 0 || d.add_behind > 0;
            if changed || (self.trajectory == TrajectoryMode::Full && full_extra) {
                self.traj(sink, i, TrajectoryKind::Event, ts, sequence, before, after, &d)?;
            }
            if self.collect_truth {
                self.truth.push(ShadowEventTruth {
                    shadow_idx: i,
                    activated_now: activated.contains(&i),
                    ended_now: done.then_some(EndState::Filled),
                    ahead_before: before.0,
                    ahead_after: d.qa_after,
                    rank_before: before.1,
                    rank_after: after.1,
                    level_qty_before: before.2,
                    level_qty_after: level_after,
                    cancel_ahead: d.pure.min(d.qa_before_step),
                    cancel_behind: d.cancel_behind,
                    add_behind: d.add_behind,
                    traded_at_px: d.traded,
                    trade_through: d.crossed,
                    // The event's fills include a `crossed_at_live` fill at this event's start (review m13).
                    filled_now: d.filled_now
                        + if activated.contains(&i) {
                            self.st[i as usize].fill_at_live
                        } else {
                            0
                        },
                });
            }
            if done {
                if let Some(st) = self.slot(instrument_id).map(|k| &mut self.inst[k]) {
                    st.remove(i, side, px);
                }
                self.end_row(sink, i, ts, sequence)?;
            }
        }
        if let Some(t) = self.why.as_mut() {
            let s = &self.st[t.idx as usize];
            if s.instrument_id == instrument_id && t.has_pending() && work.binary_search(&t.idx).is_err() {
                let level = s.price.map_or(0, |p| book.level_total(s.side, p));
                t.end_event(s.qa, s.rank, level, 0, &[]);
            }
        }
        if self.collect_truth {
            for i in ended {
                let s = &self.st[i as usize];
                self.truth.push(ShadowEventTruth {
                    shadow_idx: i,
                    activated_now: activated.contains(&i),
                    ended_now: s.end_state,
                    ahead_before: s.qa,
                    ahead_after: s.qa,
                    rank_before: s.rank,
                    rank_after: s.rank,
                    level_qty_before: s.level_qty,
                    level_qty_after: s.level_qty,
                    filled_now: if activated.contains(&i) { s.fill_at_live } else { 0 },
                    ..ShadowEventTruth::default()
                });
            }
            self.truth.sort_by_key(|t| t.shadow_idx);
        }
        self.work = work;
        if let Some(st) = self.slot(instrument_id).map(|k| &mut self.inst[k]) {
            let mut lvl = lvl;
            lvl.clear();
            st.lvl = lvl;
        }
        Ok(())
    }

    /// End of input: remaining live and pending shadows end `censored_session_end` when the instrument's last
    /// event is at or after its session close, else `open_at_end`; a pending shadow whose `ts_live` falls in a
    /// trading halt ends `censored_gap` at its `ts_live` (SPEC 1.2). `last(id)` = (last ts_event, last sequence).
    pub(crate) fn finish<'b>(
        &mut self,
        last: &dyn Fn(u32) -> Option<(Ts, u64, &'b Book)>,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        for idx in 0..self.st.len() as u32 {
            let s = &self.st[idx as usize];
            if s.status == ShadowStatus::Done {
                continue;
            }
            let inst = s.instrument_id;
            let found = last(inst);
            if s.status == ShadowStatus::Pending && self.halted_at(inst, s.ts_live) {
                let (ts, seq) = (s.ts_live, found.map_or(0, |l| l.1));
                self.finish_one(idx, EndState::CensoredGap, ts, seq, found.map(|l| l.2), sink)?;
                continue;
            }
            let (ts, seq) = found.map_or((s.ts_live, 0), |l| (l.0, l.1));
            let close = self.instruments.at(inst, ts).map(|i| {
                i.midnight_utc_ns
                    .saturating_add(i64::from(i.close_s).saturating_mul(SEC_NS))
            });
            let state = match (found, close) {
                (Some((t, _, _)), Some(c)) if t >= c => EndState::CensoredSessionEnd,
                _ => EndState::OpenAtEnd,
            };
            self.finish_one(idx, state, ts, seq, found.map(|l| l.2), sink)?;
        }
        Ok(())
    }

    /// Ends a never-replayed shadow (no partition) as `censored_gap` at its `ts_live`.
    pub(crate) fn censor_unreplayed(&mut self, sink: &mut dyn Sink) -> QtResult<()> {
        for idx in 0..self.st.len() as u32 {
            if self.st[idx as usize].status != ShadowStatus::Done {
                let ts = self.st[idx as usize].ts_live;
                self.finish_one(idx, EndState::CensoredGap, ts, 0, None, sink)?;
            }
        }
        Ok(())
    }

    /// Result rows in input order.
    pub(crate) fn results(&self, dq_flags: u32) -> Vec<ShadowResultRow> {
        self.specs
            .iter()
            .zip(&self.st)
            .map(|(spec, s)| {
                let act = s.ts_activated_event;
                // Activation side (SPEC 5.2.7) and cancel side (audit minor 15): a fill in `(c - w, c]` raced the
                // cancel. Fills are time-ordered and never after `c`, so the last fill decides. A shadow that replaced
                // its twin (A1) is placed and filled by the twin's own records: no race.
                let w = self.race_window;
                let cancel_race = s
                    .cancel_eff
                    .zip(s.ts_last_fill)
                    .is_some_and(|(c, f)| f <= c && f > c.saturating_sub(w));
                let ambiguous = w > 0
                    && s.twin.is_none()
                    && act.is_some_and(|a| {
                        a.saturating_sub(s.ts_live) <= w
                            || s.ts_first_fill.is_some_and(|f| f >= a && f - a <= w)
                            || cancel_race
                    });
                let out_of_session = self
                    .instruments
                    .at(spec.instrument_id, spec.ts_submit)
                    .is_some_and(|i| {
                        let open = i
                            .midnight_utc_ns
                            .saturating_add(i64::from(i.open_s).saturating_mul(SEC_NS));
                        let close = i
                            .midnight_utc_ns
                            .saturating_add(i64::from(i.close_s).saturating_mul(SEC_NS));
                        spec.ts_submit < open || spec.ts_submit >= close
                    });
                ShadowResultRow {
                    shadow_id: spec.shadow_id.clone(),
                    instrument_id: spec.instrument_id,
                    side: spec.side,
                    price: s.price,
                    qty: s.qty,
                    ts_submit: spec.ts_submit,
                    ts_live: s.ts_live,
                    ts_activated_event: act,
                    ts_cancel_effective: s.cancel_eff,
                    queue_ahead_at_live: s.qa_live,
                    rank_at_live: s.rank_live,
                    level_qty_at_live: s.level_live,
                    ts_first_fill: s.ts_first_fill,
                    ts_last_fill: s.ts_last_fill,
                    ts_full_fill: s.ts_full_fill,
                    filled_qty: s.filled,
                    n_fills: s.n_fills,
                    queue_ahead_at_first_fill: s.qa_first_fill,
                    first_fill_reason: s.first_reason,
                    end_state: s.end_state.unwrap_or(EndState::OpenAtEnd),
                    latency_applied_ns: s.ts_live.saturating_sub(spec.ts_submit),
                    ambiguous_race: ambiguous,
                    out_of_session,
                    dq_flags,
                }
            })
            .collect()
    }

    pub(crate) fn counts(&self) -> ShadowCounts {
        let mut c = ShadowCounts {
            n: self.st.len() as u64,
            fills_rows: self.fills_rows,
            trajectory_rows: self.trajectory_rows,
            ..ShadowCounts::default()
        };
        for s in &self.st {
            let end = s.end_state.unwrap_or(EndState::OpenAtEnd);
            let rejected = matches!(end, EndState::RejectedNoTouch | EndState::RejectedWouldCross);
            c.n_live += u64::from(s.qa_live.is_some() && !rejected);
            match end {
                EndState::Filled => c.n_filled += 1,
                EndState::Cancelled => c.n_cancelled += 1,
                EndState::CensoredSessionEnd
                | EndState::CensoredClear
                | EndState::CensoredGap
                | EndState::OpenAtEnd => c.n_censored += 1,
                EndState::RejectedNoTouch | EndState::RejectedWouldCross => c.n_rejected += 1,
                EndState::AmendedToFilled => {}
            }
            if s.filled > 0 && end != EndState::Filled {
                c.n_partial_only += 1;
            }
        }
        c
    }
}
