//! Per-partition zoo simulation (SPEC 5.3.2): shadow lifecycle (activation, cancels, horizons, censoring), the
//! L2 view and hbt depth mirror, op generation per L2 batch for every live shadow, the MBO arm of `hbt_l3_fifo`
//! (driven by the exact engine's observer callbacks) and the prediction / fill / trajectory rows.

use std::collections::{BTreeMap, BTreeSet};

use qt_book::{Book, Effect};
use qt_schema::rows::{
    EndState, L2Kind, L2Row, Liquidity, PriceSpec, Quirk, ZooFillReason, ZooFillRow, ZooPredictionRow,
    ZooTrajectoryRow, l2flags,
};
use qt_types::{Action, BookSide, Dur, MboEvent, Px, Qty, Side, Ts, Xoshiro256StarStar, stable_seed};

use crate::l3::{L3Pair, L3Reason};
use crate::pair::{FillRec, ModelSlot, Op, Pair, PairEnd, shadow_l2};
use crate::view::{HbtDepth, INVALID_MAX, INVALID_MIN, View};
use crate::{Mode, ModelId, ShadowL2};

/// One shadow of a partition with its lifecycle times (latency already applied).
#[derive(Clone, Debug, PartialEq)]
pub struct ShadowInput {
    /// Shadow id.
    pub shadow_id: String,
    /// Instrument.
    pub instrument_id: u32,
    /// Side.
    pub side: BookSide,
    /// Price rule.
    pub price: PriceSpec,
    /// Quantity.
    pub qty: Qty,
    /// Target activation time.
    pub ts_live: Ts,
    /// Cancel effective time.
    pub cancel_at: Option<Ts>,
    /// Priority-keeping size reductions `(effective time, new total quantity)` in amend row order (schema 1.1
    /// `ZooSpec.amends`; latency applied as for the exact engine). As in the exact engine, amends effective at or
    /// before `ts_live` apply before activation (a new quantity of 0 cancels the shadow), later ones at the start of
    /// the first batch / event strictly after their time.
    pub amends: Vec<(Ts, Qty)>,
}

/// Row kinds of the internal L2 row.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RowKind {
    /// Level update.
    Depth,
    /// Snapshot level.
    Snapshot,
    /// Trade.
    Trade,
    /// Clear.
    Clear,
}

/// L2 row with `f64` quantities (from `qt.l2`, or hand-built in tests).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Row {
    /// Row time.
    pub ts: Ts,
    /// Kind.
    pub kind: RowKind,
    /// Level side (trades: the hit side; `N` for hidden prints and clears).
    pub side: Side,
    /// Price (ignored for clears).
    pub price: Px,
    /// New level quantity, or trade quantity.
    pub qty: f64,
    /// Aggressor (trades).
    pub aggressor: Side,
    /// `qt.l2` flags.
    pub flags: u8,
    /// Clear only one side (hbt per-side clear; `qt.l2` clears are full).
    pub clear_side: Option<BookSide>,
    /// Clear up to a price (with `clear_side`).
    pub clear_upto: Option<Px>,
}

impl Row {
    /// Converts a `qt.l2` row.
    pub fn from_l2(r: &L2Row) -> Self {
        let (kind, qty) = match r.kind {
            L2Kind::Depth => (RowKind::Depth, r.new_qty as f64),
            L2Kind::Snapshot => (RowKind::Snapshot, r.new_qty as f64),
            L2Kind::Trade => (RowKind::Trade, r.trade_qty as f64),
            L2Kind::Clear => (RowKind::Clear, 0.0),
        };
        let flags = if r.kind == L2Kind::Snapshot {
            r.flags | l2flags::SNAPSHOT
        } else {
            r.flags
        };
        Row {
            ts: r.ts_event,
            kind,
            side: r.side,
            price: r.price.unwrap_or(0),
            qty,
            aggressor: r.aggressor,
            flags,
            clear_side: None,
            clear_upto: None,
        }
    }

    /// Hidden print (`HIDDEN_TRADE`). A side-N trade without the flag (CME implied) is a displayed trade whose
    /// hit side comes from `side` (L2Builder inference, audit minor 12) or the aggressor; unsided ones are dropped.
    fn hidden(&self) -> bool {
        self.kind == RowKind::Trade && self.flags & l2flags::HIDDEN_TRADE != 0
    }
}

/// Splits rows into batches (`LAST_IN_BATCH` closes a batch; an `event_id` change starts one).
pub fn split_batches(rows: &[L2Row]) -> Vec<std::ops::Range<usize>> {
    let mut out = Vec::new();
    let mut start = 0;
    for i in 0..rows.len() {
        if i > start && rows[i].event_id != rows[i - 1].event_id {
            out.push(start..i);
            start = i;
        }
        if rows[i].flags & l2flags::LAST_IN_BATCH != 0 {
            out.push(start..i + 1);
            start = i + 1;
        }
    }
    if start < rows.len() {
        out.push(start..rows.len());
    }
    out
}

/// Settings of one partition simulation.
#[derive(Clone, Debug, PartialEq)]
pub struct SimConfig {
    /// Prediction horizons, sorted, unique, including `h_max`.
    pub horizons: Vec<Dur>,
    /// Maximum horizon.
    pub h_max: Dur,
    /// Instrument tick size (price units).
    pub tick: Px,
    /// Instrument lot size (size units).
    pub lot: f64,
    /// `cadence_ns == 0`.
    pub cadence0: bool,
    /// Run seed.
    pub seed: u64,
    /// Write trajectory rows.
    pub write_trajectory: bool,
    /// Record per-op traces (tests).
    pub trace: bool,
}

/// Model of the run: L2 pair slot or MBO (`hbt_l3_fifo`).
enum Kind {
    L2(usize),
    L3(usize),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Status {
    Pending,
    Live,
    Done,
}

/// One `hbt_l3_fifo` run of a shadow.
struct L3Run {
    pair: Option<L3Pair>,
    fills: Vec<FillRec>,
    ended: bool,
}

struct ShadowRt {
    input: ShadowInput,
    price: Option<Px>,
    rejected: bool,
    status: Status,
    l3_status: Status,
    visible_at_live: f64,
    out_of_view: bool,
    pairs: Vec<Pair>,
    l3: Vec<L3Run>,
    touched: bool,
    l3_touched: bool,
    qty_f: f64,
    activated_ts: Option<Ts>,
    /// Next amend of the L2 arm / of the `hbt_l3_fifo` arm.
    next_amend: usize,
    l3_next_amend: usize,
}

/// Exact truth of one shadow (from the exact engine's results).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Truth {
    /// Exact `ts_live`.
    pub ts_live: Ts,
    /// Resolved price.
    pub price: Option<Px>,
    /// First fill.
    pub ts_first_fill: Option<Ts>,
    /// Fill timeline `(ts, qty)`.
    pub fills: Vec<(Ts, Qty)>,
    /// Administrative censoring time (audit A2): the exact end time when the shadow ended `censored_*` or
    /// `open_at_end`; cancels and amends are not censoring.
    pub censor_time: Option<Ts>,
    /// Queue ahead at activation.
    pub queue_ahead_at_live: Option<Qty>,
    /// Level quantity at activation.
    pub level_qty_at_live: Option<Qty>,
    /// End state.
    pub end_state: Option<EndState>,
    /// `ts_event` of the event that activated the shadow in the exact engine.
    pub ts_activated: Option<Ts>,
}

/// Rows produced by one partition.
#[derive(Default)]
pub struct PartitionOut {
    /// `qt.zoo_predictions` rows.
    pub predictions: Vec<ZooPredictionRow>,
    /// `qt.zoo_fills` rows.
    pub fills: Vec<ZooFillRow>,
    /// `qt.zoo_trajectory` rows.
    pub trajectory: Vec<ZooTrajectoryRow>,
    /// Administrative end per shadow (audit A2).
    pub admin_end: Vec<Ts>,
    /// Time of the L2 batch that activated each shadow (`None`: never live in the L2 arm).
    pub activated: Vec<Option<Ts>>,
    /// 1.1 `qt.zoo_predictions.liquidity`, one per prediction row: the liquidity of the model's first fill when it
    /// falls within the row's horizon (`crate::fill_liquidity`: `taker` only for `crossed_at_live`), null when the
    /// model has no fill by then and for particle models (no deterministic fill).
    pub pred_liquidity: Vec<Option<Liquidity>>,
    /// 1.1 `qt.zoo_predictions.status`, one per prediction row: true = `hbt_312_abort` (the faithful hbt pair was
    /// aborted at its #312 `InvalidOrderStatus` event at or before the row's deadline `ts_live + horizon`).
    pub pred_aborted: Vec<bool>,
    /// Faithful hbt #312 aborts per model id: (aborted pairs, first shadow, first abort ts).
    pub hbt_errors: BTreeMap<String, (u64, String, Ts)>,
    /// Warnings.
    pub warnings: Vec<String>,
}

/// One partition of the zoo run.
pub struct PartitionSim {
    cfg: SimConfig,
    slots: Vec<ModelSlot>,
    l3_ids: Vec<(ModelId, String)>,
    kinds: Vec<Kind>,
    shadows: Vec<ShadowRt>,
    pending: Vec<u32>,
    next_l2: usize,
    next_mbo: usize,
    index: BTreeMap<(u8, Px), Vec<u32>>,
    l3_index: BTreeMap<(u8, Px), Vec<u32>>,
    deadlines: BTreeSet<(Ts, u8, u32)>,
    l3_deadlines: BTreeSet<(Ts, u8, u32)>,
    view: View,
    hbt: HbtDepth,
    trades: Vec<(Ts, f64)>,
    exact_ahead: Vec<Option<Qty>>,
    gap_end: Vec<u32>,
    trajectory: Vec<ZooTrajectoryRow>,
    touched: Vec<u32>,
    l3_touched: Vec<u32>,
    last_ts: Ts,
    unsided_trades: u64,
    start_best: (Option<Px>, Option<Px>),
    admin_events: Vec<Ts>,
    record: bool,
    tapes: Vec<Vec<Op>>,
    warnings: Vec<String>,
    /// Executions of excluded real orders (audit A1) not yet credited to the `hbt_l3_fifo` shadows.
    excluded_execs: Vec<(BookSide, Px, Qty)>,
    /// `qt.l2` depth / trade rows without a price, skipped (review m27: never read as price 0).
    priceless_rows: u64,
    /// Engine shadow index -> local index.
    pub engine_map: Vec<Option<u32>>,
}

/// Index key side byte.
fn sk(side: BookSide) -> u8 {
    match side {
        BookSide::Bid => 0,
        BookSide::Ask => 1,
    }
}

const DL_CANCEL: u8 = 0;
const DL_HMAX: u8 = 1;
const DL_AMEND: u8 = 2;

fn resolve_price(
    spec: &PriceSpec,
    side: BookSide,
    tick: Px,
    best_same: Option<Px>,
    best_opp: Option<Px>,
) -> Option<Px> {
    match *spec {
        PriceSpec::Abs(p) => Some(p),
        PriceSpec::Touch { offset_ticks } => {
            let b = best_same?;
            let off = i64::from(offset_ticks).checked_mul(tick)?;
            let px = match side {
                BookSide::Bid => b.checked_add(off)?,
                BookSide::Ask => b.checked_sub(off)?,
            };
            if offset_ticks > 0 {
                let crosses = match side {
                    BookSide::Bid => best_opp.is_some_and(|a| px >= a),
                    BookSide::Ask => best_opp.is_some_and(|bb| px <= bb),
                };
                if crosses {
                    return None;
                }
            }
            Some(px)
        }
    }
}

impl PartitionSim {
    /// New simulation over `models` (canonical order) for the partition's shadows.
    pub fn new(cfg: SimConfig, models: &[ModelId], shadows: Vec<ShadowInput>) -> crate::QtResult<Self> {
        let mut slots = Vec::new();
        let mut l3_ids = Vec::new();
        let mut kinds = Vec::new();
        for id in models {
            if id.family == crate::Family::HbtL3Fifo {
                kinds.push(Kind::L3(l3_ids.len()));
                l3_ids.push((id.clone(), id.to_string()));
            } else {
                kinds.push(Kind::L2(slots.len()));
                slots.push(ModelSlot::new(crate::build(id)?));
            }
        }
        let mut rts = Vec::with_capacity(shadows.len());
        for s in shadows {
            let qty = s.qty as f64;
            let pairs = slots
                .iter()
                .map(|slot| {
                    let rng = Xoshiro256StarStar::new(stable_seed(
                        cfg.seed,
                        &[slot.id_str.as_bytes(), s.shadow_id.as_bytes()],
                    ));
                    let mut p = Pair::new(slot, qty, rng);
                    if cfg.trace {
                        p.trace = Some(Vec::new());
                    }
                    p
                })
                .collect();
            let l3 = l3_ids
                .iter()
                .map(|_| L3Run {
                    pair: None,
                    fills: Vec::new(),
                    ended: false,
                })
                .collect();
            rts.push(ShadowRt {
                input: s,
                price: None,
                rejected: false,
                status: Status::Pending,
                l3_status: Status::Pending,
                visible_at_live: 0.0,
                out_of_view: false,
                pairs,
                l3,
                touched: false,
                l3_touched: false,
                qty_f: qty,
                activated_ts: None,
                next_amend: 0,
                l3_next_amend: 0,
            });
        }
        let mut pending: Vec<u32> = (0..rts.len() as u32).collect();
        pending.sort_by_key(|&i| (rts[i as usize].input.ts_live, i));
        let n = rts.len();
        Ok(Self {
            hbt: HbtDepth::new(cfg.tick, cfg.lot),
            cfg,
            slots,
            l3_ids,
            kinds,
            shadows: rts,
            pending,
            next_l2: 0,
            next_mbo: 0,
            index: BTreeMap::new(),
            l3_index: BTreeMap::new(),
            deadlines: BTreeSet::new(),
            l3_deadlines: BTreeSet::new(),
            view: View::new(),
            trades: Vec::new(),
            exact_ahead: vec![None; n],
            gap_end: Vec::new(),
            trajectory: Vec::new(),
            touched: Vec::new(),
            l3_touched: Vec::new(),
            last_ts: Ts::MIN,
            unsided_trades: 0,
            start_best: (None, None),
            admin_events: Vec::new(),
            record: false,
            tapes: vec![Vec::new(); n],
            warnings: Vec::new(),
            excluded_execs: Vec::new(),
            priceless_rows: 0,
            engine_map: Vec::new(),
        })
    }

    fn sl2(&self, i: u32) -> ShadowL2 {
        let s = &self.shadows[i as usize];
        shadow_l2(i, s.input.side, s.price.unwrap_or(0), s.qty_f, self.cfg.lot)
    }

    fn dispatch(&mut self, i: u32, op: Op) {
        let o = self.sl2(i);
        let s = &mut self.shadows[i as usize];
        if s.status != Status::Live {
            return;
        }
        if self.record {
            self.tapes[i as usize].push(op);
        }
        for (k, p) in s.pairs.iter_mut().enumerate() {
            p.apply(&self.slots[k], &o, &op);
        }
        if !s.touched {
            s.touched = true;
            self.touched.push(i);
        }
    }

    fn remove_from(index: &mut BTreeMap<(u8, Px), Vec<u32>>, key: (u8, Px), i: u32) {
        if let Some(v) = index.get_mut(&key) {
            v.retain(|x| *x != i);
            if v.is_empty() {
                index.remove(&key);
            }
        }
    }

    fn stop_l2(&mut self, i: u32, why: PairEnd) {
        let s = &mut self.shadows[i as usize];
        if s.status == Status::Live {
            for p in &mut s.pairs {
                p.stop(why);
            }
        }
        self.retire(i, true);
    }

    fn retire_l2(&mut self, i: u32) {
        self.retire(i, false);
    }

    fn retire(&mut self, i: u32, forced: bool) {
        let record = self.record;
        let s = &mut self.shadows[i as usize];
        if s.status == Status::Live && (forced || (!record && s.pairs.iter().all(Pair::finished))) {
            s.status = Status::Done;
            let key = (sk(s.input.side), s.price.unwrap_or(0));
            Self::remove_from(&mut self.index, key, i);
        }
    }

    fn shadows_in(index: &BTreeMap<(u8, Px), Vec<u32>>, lo: (u8, Px), hi: (u8, Px)) -> Vec<u32> {
        if lo > hi {
            return Vec::new();
        }
        index.range(lo..=hi).flat_map(|(_, v)| v.iter().copied()).collect()
    }

    /// Shadows on `side` whose price is at or through a price `px` from the other side's point of view:
    /// bids with price >= px, asks with price <= px.
    fn at_or_through(index: &BTreeMap<(u8, Px), Vec<u32>>, side: BookSide, px: Px) -> Vec<u32> {
        match side {
            BookSide::Bid => Self::shadows_in(index, (0, px), (0, Px::MAX)),
            BookSide::Ask => Self::shadows_in(index, (1, Px::MIN), (1, px)),
        }
    }

    /// Lifecycle at the start of a batch with time `ts`.
    fn batch_start(&mut self, ts: Ts) {
        // engine-reported gaps
        for i in std::mem::take(&mut self.gap_end) {
            self.stop_l2(i, PairEnd::CensoredGap);
            let s = &mut self.shadows[i as usize];
            if s.status == Status::Pending {
                s.status = Status::Done;
            }
        }
        // activations
        while self.next_l2 < self.pending.len() {
            let i = self.pending[self.next_l2];
            if self.shadows[i as usize].input.ts_live >= ts {
                break;
            }
            self.next_l2 += 1;
            self.activate_l2(i, ts);
        }
        // cancels, amends and horizons
        while let Some(&(t, kind, i)) = self.deadlines.first() {
            if t >= ts {
                break;
            }
            self.deadlines.pop_first();
            if kind == DL_AMEND {
                self.amend_l2(i, ts);
                continue;
            }
            let why = if kind == DL_CANCEL {
                PairEnd::Cancelled
            } else {
                PairEnd::Stopped
            };
            self.stop_l2(i, why);
        }
    }

    /// The L2 arm's amends of a live shadow effective before `ts` (row order); the next one is scheduled.
    fn amend_l2(&mut self, i: u32, ts: Ts) {
        loop {
            let s = &mut self.shadows[i as usize];
            if s.status != Status::Live {
                return;
            }
            let Some(&(t, q)) = s.input.amends.get(s.next_amend) else {
                return;
            };
            if t >= ts {
                self.deadlines.insert((t, DL_AMEND, i));
                return;
            }
            s.next_amend += 1;
            s.qty_f = q as f64;
            self.dispatch(i, Op::Amend { ts: t, qty: q as f64 });
            self.retire_l2(i);
        }
    }

    /// Amends effective at or before `ts_live` from `from` on: (next amend index, last new quantity); stops at a new
    /// quantity of 0 (the shadow is cancelled before it goes live, as in the exact engine).
    fn amends_before_live(input: &ShadowInput, from: usize) -> (usize, Option<Qty>) {
        let mut k = from;
        let mut q = None;
        while let Some(&(t, nq)) = input.amends.get(k) {
            if t > input.ts_live {
                break;
            }
            k += 1;
            q = Some(nq);
            if nq == 0 {
                break;
            }
        }
        (k, q)
    }

    fn activate_l2(&mut self, i: u32, batch_ts: Ts) {
        let tick = self.cfg.tick;
        let s = &self.shadows[i as usize];
        if s.status != Status::Pending || s.rejected {
            return;
        }
        let ts_live = s.input.ts_live;
        if s.input.cancel_at.is_some_and(|c| c <= ts_live) {
            self.shadows[i as usize].status = Status::Done;
            return;
        }
        let (k, q) = Self::amends_before_live(&s.input, s.next_amend);
        {
            let s = &mut self.shadows[i as usize];
            s.next_amend = k;
            match q {
                Some(0) => {
                    s.status = Status::Done;
                    return;
                }
                Some(q) => s.qty_f = q as f64,
                None => {}
            }
        }
        let s = &self.shadows[i as usize];
        let side = s.input.side;
        let price = match s.price {
            Some(p) => Some(p),
            None => resolve_price(
                &s.input.price,
                side,
                tick,
                self.view.best(side),
                self.view.best(side.opposite()),
            ),
        };
        let Some(px) = price else {
            let s = &mut self.shadows[i as usize];
            s.rejected = true;
            s.status = Status::Done;
            return;
        };
        let q_view = self.view.qty_at(side, px);
        let in_view = self.view.in_view(side, px);
        let cross_view = self.view.crosses(side, px);
        let opp_view = self.view.opposite_through(side, px);
        let t = self.hbt.tick_of(px);
        let q_hbt = self.hbt.qty_at_tick(side, t);
        let cross_hbt = match side {
            BookSide::Bid => t >= self.hbt.best_ask_tick,
            BookSide::Ask => t <= self.hbt.best_bid_tick,
        };
        {
            let s = &mut self.shadows[i as usize];
            s.price = Some(px);
            s.status = Status::Live;
            s.activated_ts = Some(batch_ts);
            s.visible_at_live = q_view;
            s.out_of_view = !in_view;
        }
        self.index.entry((sk(side), px)).or_default().push(i);
        self.dispatch(
            i,
            Op::Arrive {
                ts: ts_live,
                q_view,
                q_hbt,
                cross_view,
                cross_hbt,
                opp_view,
            },
        );
        let s = &self.shadows[i as usize];
        let h_end = ts_live.saturating_add(self.cfg.h_max);
        self.deadlines.insert((h_end, DL_HMAX, i));
        if let Some(c) = s.input.cancel_at {
            self.deadlines.insert((c, DL_CANCEL, i));
        }
        if let Some(&(t, _)) = s.input.amends.get(s.next_amend) {
            self.deadlines.insert((t, DL_AMEND, i));
        }
        self.retire_l2(i);
    }

    /// One L2 batch (rows in batch order).
    pub fn on_batch(&mut self, rows: &[Row]) {
        // the batch's emission time: with `trade_position = after` and conflation the last row can be an older
        // trade (review m23); every row of a batch is at or before its emission
        let Some(ts) = rows.iter().map(|r| r.ts).max() else {
            return;
        };
        self.last_ts = self.last_ts.max(ts);
        self.batch_start(ts);
        for r in rows {
            match r.kind {
                RowKind::Clear => {
                    self.admin_events.push(r.ts);
                    self.view.clear(r.clear_side, r.clear_upto);
                    self.hbt.clear(r.clear_side, r.clear_upto);
                    let live: Vec<u32> = self.index.values().flat_map(|v| v.iter().copied()).collect();
                    for i in live {
                        self.dispatch(i, Op::Clear { ts: r.ts });
                    }
                }
                RowKind::Depth | RowKind::Snapshot => {
                    let Some(side) = BookSide::from_side(r.side) else {
                        continue;
                    };
                    let prev_view = self.view.set(side, r.price, r.qty);
                    let (prev_hbt, prev_best, new_best) = self.hbt.update(side, r.price, r.qty);
                    let flags = if r.kind == RowKind::Snapshot {
                        r.flags | l2flags::SNAPSHOT
                    } else {
                        r.flags
                    };
                    if let Some(v) = self.index.get(&(sk(side), r.price)).cloned() {
                        for i in v {
                            self.dispatch(
                                i,
                                Op::Depth {
                                    ts: r.ts,
                                    prev_view,
                                    prev_hbt,
                                    new: r.qty,
                                    flags,
                                },
                            );
                        }
                    }
                    // hbt: the opposite side's orders fill when this side's best moved toward them
                    let moved_toward = match side {
                        BookSide::Ask => new_best < prev_best && new_best != INVALID_MAX,
                        BookSide::Bid => new_best > prev_best && new_best != INVALID_MIN,
                    };
                    if moved_toward {
                        let tick = self.cfg.tick;
                        let other = side.opposite();
                        let bound = new_best.saturating_mul(tick);
                        let cand = match other {
                            BookSide::Bid => Self::at_or_through(&self.index, other, bound.saturating_sub(tick)),
                            BookSide::Ask => Self::at_or_through(&self.index, other, bound.saturating_add(tick)),
                        };
                        for i in cand {
                            let Some(px) = self.shadows[i as usize].price else {
                                continue;
                            };
                            let t = self.hbt.tick_of(px);
                            let hit = match other {
                                BookSide::Bid => t >= new_best && (prev_best == INVALID_MAX || t < prev_best),
                                BookSide::Ask => t <= new_best && (prev_best == INVALID_MIN || t > prev_best),
                            };
                            if hit {
                                self.dispatch(i, Op::CrossRow { ts: r.ts });
                            }
                        }
                    }
                }
                RowKind::Trade => {
                    let hidden = r.hidden();
                    if !hidden {
                        self.trades.push((r.ts, r.qty));
                    }
                    let mut at: Vec<u32> = Vec::new();
                    let mut aggressor = r.aggressor;
                    let mut through: Vec<u32> = Vec::new();
                    if hidden {
                        for side in [BookSide::Bid, BookSide::Ask] {
                            if let Some(v) = self.index.get(&(sk(side), r.price)) {
                                at.extend(v.iter().copied());
                            }
                        }
                        through.extend(Self::shadows_in(
                            &self.index,
                            (0, r.price.saturating_add(1)),
                            (0, Px::MAX),
                        ));
                        through.extend(Self::shadows_in(
                            &self.index,
                            (1, Px::MIN),
                            (1, r.price.saturating_sub(1)),
                        ));
                    } else if let Some(hit) =
                        BookSide::from_side(r.side).or_else(|| BookSide::from_side(r.aggressor).map(BookSide::opposite))
                    {
                        if let Some(v) = self.index.get(&(sk(hit), r.price)) {
                            at.extend(v.iter().copied());
                        }
                        through = match hit {
                            BookSide::Bid => {
                                Self::shadows_in(&self.index, (0, r.price.saturating_add(1)), (0, Px::MAX))
                            }
                            BookSide::Ask => {
                                Self::shadows_in(&self.index, (1, Px::MIN), (1, r.price.saturating_sub(1)))
                            }
                        };
                        aggressor = hit.opposite().to_side();
                    } else {
                        self.unsided_trades += 1;
                    }
                    for i in at {
                        self.dispatch(
                            i,
                            Op::Trade {
                                ts: r.ts,
                                qty: r.qty,
                                aggressor,
                                hidden,
                            },
                        );
                    }
                    for i in through {
                        self.dispatch(i, Op::Through { ts: r.ts, hidden });
                    }
                }
            }
        }
        // batch end: touched shadows and shadows the view crosses
        let mut end: Vec<u32> = std::mem::take(&mut self.touched);
        if let Some(a) = self.view.best(BookSide::Ask) {
            end.extend(Self::at_or_through(&self.index, BookSide::Bid, a));
        }
        if let Some(b) = self.view.best(BookSide::Bid) {
            end.extend(Self::at_or_through(&self.index, BookSide::Ask, b));
        }
        let mut seen = BTreeSet::new();
        for i in end {
            if !seen.insert(i) {
                continue;
            }
            let (side, px) = {
                let s = &self.shadows[i as usize];
                (s.input.side, s.price.unwrap_or(0))
            };
            let cross_view = self.view.crosses(side, px);
            let opp_view = if cross_view {
                self.view.opposite_through(side, px)
            } else {
                0.0
            };
            self.dispatch(
                i,
                Op::BatchEnd {
                    ts,
                    cross_view,
                    opp_view,
                    cadence0: self.cfg.cadence0,
                },
            );
            if self.cfg.write_trajectory {
                self.traj_rows(i, ts);
            }
            let s = &mut self.shadows[i as usize];
            s.touched = false;
            for p in &mut s.pairs {
                p.touched = false;
            }
            self.retire_l2(i);
        }
        self.touched.clear();
    }

    fn traj_rows(&mut self, i: u32, ts: Ts) {
        let Some(exact) = self.exact_ahead[i as usize] else {
            return;
        };
        let s = &self.shadows[i as usize];
        for (k, p) in s.pairs.iter().enumerate() {
            if !p.touched || !p.live {
                continue;
            }
            self.trajectory.push(ZooTrajectoryRow {
                model_id: self.slots[k].id_str.clone(),
                shadow_id: s.input.shadow_id.clone(),
                ts_event: ts,
                queue_ahead_est: self.slots[k].model.queue_ahead(&p.st),
                queue_ahead_exact: exact,
            });
        }
    }

    // ---- MBO arm (exact engine observer) ---------------------------------------------------------------------

    /// Event start in the exact engine: price resolution from the exact book and `hbt_l3_fifo` activation.
    pub fn mbo_event_start(&mut self, ts: Ts, book: &Book) {
        self.start_best = (
            book.best(BookSide::Bid).map(|l| l.price),
            book.best(BookSide::Ask).map(|l| l.price),
        );
        while self.next_mbo < self.pending.len() {
            let i = self.pending[self.next_mbo];
            if self.shadows[i as usize].input.ts_live >= ts {
                break;
            }
            self.next_mbo += 1;
            let tick = self.cfg.tick;
            let s = &mut self.shadows[i as usize];
            let side = s.input.side;
            match resolve_price(
                &s.input.price,
                side,
                tick,
                book.best(side).map(|l| l.price),
                book.best(side.opposite()).map(|l| l.price),
            ) {
                Some(px) => s.price = Some(px),
                None => {
                    s.rejected = true;
                    s.status = Status::Done;
                    s.l3_status = Status::Done;
                    continue;
                }
            }
            if self.l3_ids.is_empty() {
                continue;
            }
            if s.input.cancel_at.is_some_and(|c| c <= s.input.ts_live) {
                s.l3_status = Status::Done;
                continue;
            }
            let (k, q) = Self::amends_before_live(&s.input, s.l3_next_amend);
            s.l3_next_amend = k;
            if q == Some(0) {
                s.l3_status = Status::Done;
                continue;
            }
            let px = s.price.unwrap_or(0);
            let qty = q.unwrap_or(s.input.qty);
            let ts_live = s.input.ts_live;
            s.l3_status = Status::Live;
            let crossed = match side {
                BookSide::Bid => book.best(BookSide::Ask).is_some_and(|l| l.price <= px),
                BookSide::Ask => book.best(BookSide::Bid).is_some_and(|l| l.price >= px),
            };
            for (k, run) in s.l3.iter_mut().enumerate() {
                let mode = self.l3_ids[k].0.mode;
                let mut pair = L3Pair::new(side, px, qty, mode, book).with_tick(tick);
                if crossed {
                    if mode == Mode::Faithful {
                        // hbt GTX order crossing on arrival: expired unfilled
                        run.ended = true;
                        continue;
                    }
                    let qa = pair.queue_ahead(book);
                    for q in pair.taker_walk(book) {
                        let cum = run.fills.last().map(|f| f.cum).unwrap_or(0.0) + q as f64;
                        run.fills.push(FillRec {
                            ts: ts_live,
                            qty: q as f64,
                            cum,
                            qa_before: qa,
                            reason: ZooFillReason::CrossedAtLive,
                            quirk: Quirk::None,
                        });
                    }
                    if pair.leaves() == 0 {
                        run.ended = true;
                        continue;
                    }
                }
                run.pair = Some(pair);
            }
            if s.l3.iter().all(|r| r.ended) {
                s.l3_status = Status::Done;
                continue;
            }
            self.l3_index.entry((sk(side), px)).or_default().push(i);
            self.l3_deadlines
                .insert((ts_live.saturating_add(self.cfg.h_max), DL_HMAX, i));
            if let Some(c) = s.input.cancel_at {
                self.l3_deadlines.insert((c, DL_CANCEL, i));
            }
            if let Some(&(t, _)) = s.input.amends.get(s.l3_next_amend) {
                self.l3_deadlines.insert((t, DL_AMEND, i));
            }
        }
        while let Some(&(t, kind, i)) = self.l3_deadlines.first() {
            if t >= ts {
                break;
            }
            self.l3_deadlines.pop_first();
            if kind == DL_AMEND {
                self.amend_l3(i, ts);
            } else {
                self.stop_l3(i);
            }
        }
        // cadence 0: the L2 arm's lifecycle (activation, cancels, horizons) runs at the exact event start too, so an
        // event that changes nothing visible (no L2 batch) still activates the shadow at the exact engine's event
        if self.cfg.cadence0 {
            self.batch_start(ts);
        }
    }

    /// The `hbt_l3_fifo` arm's amends of a live shadow effective before `ts` (row order); the next one is scheduled.
    fn amend_l3(&mut self, i: u32, ts: Ts) {
        let s = &mut self.shadows[i as usize];
        if s.l3_status != Status::Live {
            return;
        }
        while let Some(&(t, q)) = s.input.amends.get(s.l3_next_amend) {
            if t >= ts {
                self.l3_deadlines.insert((t, DL_AMEND, i));
                break;
            }
            s.l3_next_amend += 1;
            for r in &mut s.l3 {
                if let (false, Some(p)) = (r.ended, r.pair.as_mut())
                    && !p.amend(q)
                {
                    r.ended = true;
                }
            }
        }
        if s.l3.iter().all(|r| r.ended) {
            s.l3_status = Status::Done;
            let key = (sk(s.input.side), s.price.unwrap_or(0));
            Self::remove_from(&mut self.l3_index, key, i);
        }
    }

    /// Execution of `size` of an excluded real order (audit A1: the user's own MBO twin, removed from the tape)
    /// resting on `side` at `px`. The runner reports it before the exact engine applies (and drops) the record; it is
    /// credited to the `hbt_l3_fifo` shadows at that level (volume behind: the shadow takes the twin's place) and to
    /// those strictly better on that side (trade-through) at the next record or event end, i.e. after the event
    /// start the record may open, as the exact engine credits it.
    pub fn mbo_excluded_exec(&mut self, side: BookSide, px: Px, size: Qty) {
        self.excluded_execs.push((side, px, size));
    }

    fn drain_excluded(&mut self, book: &Book) {
        if self.excluded_execs.is_empty() {
            return;
        }
        for (side, px, size) in std::mem::take(&mut self.excluded_execs) {
            if self.l3_index.is_empty() {
                continue;
            }
            let mut cand: Vec<u32> = self.l3_index.get(&(sk(side), px)).cloned().unwrap_or_default();
            cand.extend(match side {
                BookSide::Bid => Self::shadows_in(&self.l3_index, (0, px.saturating_add(1)), (0, Px::MAX)),
                BookSide::Ask => Self::shadows_in(&self.l3_index, (1, Px::MIN), (1, px.saturating_sub(1))),
            });
            for i in cand {
                let s = &mut self.shadows[i as usize];
                if s.l3_status != Status::Live {
                    continue;
                }
                for r in &mut s.l3 {
                    if let (false, Some(p)) = (r.ended, r.pair.as_mut()) {
                        p.on_excluded_exec(side, px, size, book);
                    }
                }
                if !s.l3_touched {
                    s.l3_touched = true;
                    self.l3_touched.push(i);
                }
            }
        }
    }

    fn stop_l3(&mut self, i: u32) {
        let s = &mut self.shadows[i as usize];
        if s.l3_status != Status::Live {
            return;
        }
        for r in &mut s.l3 {
            r.ended = true;
        }
        s.l3_status = Status::Done;
        let key = (sk(s.input.side), s.price.unwrap_or(0));
        Self::remove_from(&mut self.l3_index, key, i);
    }

    fn l3_touch(&mut self, v: Vec<u32>, e: &MboEvent, effect: &Effect, book: &Book) {
        let start_best = self.start_best;
        for i in v {
            let s = &mut self.shadows[i as usize];
            if s.l3_status != Status::Live {
                continue;
            }
            for r in &mut s.l3 {
                if let (false, Some(p)) = (r.ended, r.pair.as_mut()) {
                    p.on_record(e, effect, book, start_best);
                }
            }
            if !s.l3_touched {
                s.l3_touched = true;
                self.l3_touched.push(i);
            }
        }
    }

    /// One record of the exact engine (after the book applied it).
    pub fn mbo_record(&mut self, e: &MboEvent, effect: &Effect, book: &Book) {
        if e.flags & qt_types::flags::MAYBE_BAD_BOOK != 0 {
            self.admin_events.push(e.ts_event);
        }
        self.drain_excluded(book);
        if self.l3_index.is_empty() {
            return;
        }
        if matches!(effect, Effect::Clear) {
            let live: Vec<u32> = self.l3_index.values().flat_map(|v| v.iter().copied()).collect();
            for i in live {
                self.stop_l3(i);
            }
            return;
        }
        let mut cand: Vec<u32> = Vec::new();
        match effect {
            Effect::Remove { side, price, .. } | Effect::Reduce { side, price, .. } => {
                if let Some(v) = self.l3_index.get(&(sk(*side), *price)) {
                    cand.extend(v.iter().copied());
                }
            }
            Effect::Replace { side, old_price, .. } => {
                if let Some(v) = self.l3_index.get(&(sk(*side), *old_price)) {
                    cand.extend(v.iter().copied());
                }
            }
            _ => {}
        }
        match e.action() {
            Some(Action::Fill) => {
                if let Some((side, px, _)) = book.order(e.order_id) {
                    if let Some(v) = self.l3_index.get(&(sk(side), px)) {
                        cand.extend(v.iter().copied());
                    }
                    // shadows on that side at a better price than the execution (faithful) or the resting price
                    let lim = match side {
                        BookSide::Bid => e.price.min(px),
                        BookSide::Ask => e.price.max(px),
                    };
                    let better = match side {
                        BookSide::Bid => Self::shadows_in(&self.l3_index, (0, lim.saturating_add(1)), (0, Px::MAX)),
                        BookSide::Ask => Self::shadows_in(&self.l3_index, (1, Px::MIN), (1, lim.saturating_sub(1))),
                    };
                    cand.extend(better);
                }
            }
            Some(Action::Trade) => {
                for side in [BookSide::Bid, BookSide::Ask] {
                    if side.to_side() == e.side() {
                        continue;
                    }
                    if let Some(v) = self.l3_index.get(&(sk(side), e.price)) {
                        cand.extend(v.iter().copied());
                    }
                    let thr = match side {
                        BookSide::Bid => Self::shadows_in(&self.l3_index, (0, e.price.saturating_add(1)), (0, Px::MAX)),
                        BookSide::Ask => Self::shadows_in(&self.l3_index, (1, Px::MIN), (1, e.price.saturating_sub(1))),
                    };
                    cand.extend(thr);
                }
            }
            _ => {}
        }
        cand.sort_unstable();
        cand.dedup();
        self.l3_touch(cand, e, effect, book);
    }

    /// Event end of the exact engine: `hbt_l3_fifo` event-end fills, exact queue-ahead for trajectories and
    /// engine-reported gap censoring.
    pub fn mbo_event_end(&mut self, ts: Ts, _sequence: u64, book: &Book, truth: &[qt_replay::ShadowEventTruth]) {
        self.drain_excluded(book);
        for t in truth {
            let Some(Some(i)) = self.engine_map.get(t.shadow_idx as usize).copied() else {
                continue;
            };
            self.exact_ahead[i as usize] = Some(t.ahead_after);
            if t.ended_now == Some(EndState::CensoredGap) {
                self.admin_events.push(ts);
                self.gap_end.push(i);
                self.stop_l3(i);
            }
        }
        if self.l3_index.is_empty() {
            for i in std::mem::take(&mut self.l3_touched) {
                self.shadows[i as usize].l3_touched = false;
            }
            return;
        }
        let mut cand = std::mem::take(&mut self.l3_touched);
        if let Some(a) = book.best(BookSide::Ask) {
            cand.extend(Self::at_or_through(&self.l3_index, BookSide::Bid, a.price));
        }
        if let Some(b) = book.best(BookSide::Bid) {
            cand.extend(Self::at_or_through(&self.l3_index, BookSide::Ask, b.price));
        }
        cand.sort_unstable();
        cand.dedup();
        for i in cand {
            let exact = self.exact_ahead[i as usize];
            let s = &mut self.shadows[i as usize];
            s.l3_touched = false;
            if s.l3_status != Status::Live {
                continue;
            }
            for (k, r) in s.l3.iter_mut().enumerate() {
                if r.ended {
                    continue;
                }
                let Some(p) = r.pair.as_mut() else {
                    continue;
                };
                let qa = p.queue_ahead(book);
                for (q, reason) in p.on_event_end(book) {
                    let cum = r.fills.last().map(|f| f.cum).unwrap_or(0.0) + q as f64;
                    r.fills.push(FillRec {
                        ts,
                        qty: q as f64,
                        cum,
                        qa_before: qa,
                        reason: match reason {
                            L3Reason::Queue => ZooFillReason::Queue,
                            L3Reason::TradeThrough => ZooFillReason::TradeThrough,
                            L3Reason::Crossed => ZooFillReason::Crossed,
                        },
                        quirk: Quirk::None,
                    });
                }
                if p.leaves() == 0 {
                    r.ended = true;
                }
                if self.cfg.write_trajectory
                    && let Some(ex) = exact
                {
                    self.trajectory.push(ZooTrajectoryRow {
                        model_id: self.l3_ids[k].1.clone(),
                        shadow_id: s.input.shadow_id.clone(),
                        ts_event: ts,
                        queue_ahead_est: p.queue_ahead(book),
                        queue_ahead_exact: ex,
                    });
                }
            }
            if s.l3.iter().all(|r| r.ended) {
                s.l3_status = Status::Done;
                let key = (sk(s.input.side), s.price.unwrap_or(0));
                Self::remove_from(&mut self.l3_index, key, i);
            }
        }
    }

    // ---- outputs ---------------------------------------------------------------------------------------------

    /// Record mode (particle simulator): every op of every live shadow is kept, and shadows stay live until a
    /// cancel, a horizon, a censoring event or the end of data (not when their pairs fill).
    pub fn set_record(&mut self, on: bool) {
        self.record = on;
    }

    /// Recorded op tapes (record mode), one per shadow.
    pub fn tapes(&self) -> Vec<Vec<Op>> {
        self.tapes.clone()
    }

    /// Fractional shadow quantities (tests replaying hftbacktest scenarios with non-lot sizes).
    pub fn set_qty_override(&mut self, q: Vec<f64>) {
        for (s, x) in self.shadows.iter_mut().zip(q) {
            s.qty_f = x;
            for p in &mut s.pairs {
                p.st.leaves = x;
            }
        }
    }

    /// Adds warnings to the partition output.
    pub fn push_warnings(&mut self, w: Vec<String>) {
        self.warnings.extend(w);
    }

    /// One `qt.l2` batch: rows converted with [`Row::from_l2`]; depth / snapshot / trade rows without a price are
    /// skipped and counted (a null price is never read as price 0, which would be a trade through every bid).
    pub fn on_l2_batch(&mut self, rows: &[L2Row]) {
        let before = rows.len();
        let b: Vec<Row> = rows
            .iter()
            .filter(|r| r.price.is_some() || r.kind == L2Kind::Clear)
            .map(Row::from_l2)
            .collect();
        self.priceless_rows += (before - b.len()) as u64;
        self.on_batch(&b);
    }

    /// Administrative end of a shadow (audit A2, outcome-independent): the first clear, bad-book record or
    /// engine-reported gap after `ts_live`, the session close, or the end of data, whichever comes first.
    pub fn admin_end(&self, ts_live: Ts, close: Ts) -> Ts {
        let k = self.admin_events.partition_point(|t| *t <= ts_live);
        let next = self.admin_events.get(k).copied().unwrap_or(Ts::MAX);
        next.min(close)
            .min(if self.last_ts == Ts::MIN { Ts::MAX } else { self.last_ts })
    }

    /// Resolved price of a shadow (after activation).
    pub fn price_of(&self, i: u32) -> Option<Px> {
        self.shadows.get(i as usize).and_then(|s| s.price)
    }

    /// Access to a pair (tests).
    pub fn pair(&self, shadow: u32, slot: usize) -> Option<&Pair> {
        self.shadows.get(shadow as usize).and_then(|s| s.pairs.get(slot))
    }

    /// Ends the input; builds the rows. `truth[i]` = exact truth of local shadow `i` (`None`: L2-only).
    #[allow(clippy::too_many_arguments)]
    pub fn finish(mut self, truth: Option<&[Truth]>, session_date: i32, write_fills: bool, close: Ts) -> PartitionOut {
        let mut out = PartitionOut {
            warnings: std::mem::take(&mut self.warnings),
            ..PartitionOut::default()
        };
        self.admin_events.sort_unstable();
        out.admin_end = self
            .shadows
            .iter()
            .map(|s| self.admin_end(s.input.ts_live, close))
            .collect();
        out.activated = self.shadows.iter().map(|s| s.activated_ts).collect();
        let n = self.shadows.len() as u32;
        for i in 0..n {
            self.stop_l2(i, PairEnd::Stopped);
        }
        // displayed trade volume prefix sums
        let mut tv = std::mem::take(&mut self.trades);
        tv.sort_by_key(|t| t.0);
        let mut prefix = Vec::with_capacity(tv.len() + 1);
        prefix.push(0.0f64);
        for t in &tv {
            let last = prefix.last().copied().unwrap_or(0.0);
            prefix.push(last + t.1);
        }
        let vol_upto = |ts: Ts| -> f64 {
            let k = tv.partition_point(|t| t.0 <= ts);
            prefix[k]
        };
        let lot = self.cfg.lot;
        for kind in &self.kinds {
            for (si, s) in self.shadows.iter().enumerate() {
                let tr = truth.and_then(|t| t.get(si));
                let ts_live = s.input.ts_live;
                let qty = s.input.qty;
                let vol60 = (vol_upto(ts_live) - vol_upto(ts_live.saturating_sub(60_000_000_000))).max(0.0);
                // the pair's #312 abort time (faithful hbt): rows whose deadline reaches it are not predictions
                let abort: Option<Ts> = match kind {
                    Kind::L2(k) => s.pairs[*k].hbt_error,
                    Kind::L3(_) => None,
                };
                let (model_id, fills, preds): (&str, &[FillRec], Vec<(f64, f64)>) = match kind {
                    Kind::L2(k) => {
                        let slot = &self.slots[*k];
                        let p = &s.pairs[*k];
                        let preds = self
                            .cfg
                            .horizons
                            .iter()
                            .map(|&h| pred_l2(slot, p, ts_live, h, qty))
                            .collect();
                        if let Some(err) = p.hbt_error {
                            let e = out.hbt_errors.entry(slot.id_str.clone()).or_insert((
                                0,
                                s.input.shadow_id.clone(),
                                err,
                            ));
                            e.0 += 1;
                        }
                        (slot.id_str.as_str(), &p.fills, preds)
                    }
                    Kind::L3(k) => {
                        let r = &s.l3[*k];
                        let preds = self
                            .cfg
                            .horizons
                            .iter()
                            .map(|&h| {
                                let dl = ts_live.saturating_add(h);
                                let p = if r.fills.first().is_some_and(|f| f.ts <= dl) {
                                    1.0
                                } else {
                                    0.0
                                };
                                let e: f64 = r
                                    .fills
                                    .iter()
                                    .filter(|f| f.ts <= dl)
                                    .map(|f| f.qty)
                                    .fold(0.0, |a, b| a + b);
                                (p, e)
                            })
                            .collect();
                        (self.l3_ids[*k].1.as_str(), &r.fills, preds)
                    }
                };
                let h_end = ts_live.saturating_add(self.cfg.h_max);
                let particle = matches!(kind, Kind::L2(k) if self.slots[*k].particles);
                let model_first = match kind {
                    Kind::L2(k) if self.slots[*k].particles => particle_median(&s.pairs[*k], h_end),
                    _ => fills.first().map(|f| f.ts).filter(|t| *t <= h_end),
                };
                for (h, (p, e)) in self.cfg.horizons.iter().zip(preds) {
                    let dl = ts_live.saturating_add(*h);
                    let (truth_filled, truth_ts, truth_qty, censored, qa, lq) = match tr {
                        Some(t) => {
                            let filled = t.ts_first_fill.is_some_and(|f| f <= dl);
                            let q: Qty = t.fills.iter().filter(|f| f.0 <= dl).map(|f| f.1).sum();
                            // audit A2: outcome-independent administrative censoring (never a cancel or a fill)
                            let cens = dl > out.admin_end[si];
                            (
                                Some(filled),
                                t.ts_first_fill,
                                Some(q),
                                cens,
                                t.queue_ahead_at_live,
                                t.level_qty_at_live,
                            )
                        }
                        None => (None, None, None, false, None, None),
                    };
                    out.predictions.push(ZooPredictionRow {
                        model_id: model_id.to_owned(),
                        shadow_id: s.input.shadow_id.clone(),
                        qty,
                        horizon_ns: *h,
                        p_fill: p,
                        e_fill_qty: e,
                        model_ts_first_fill: model_first,
                        truth_filled,
                        truth_ts_first_fill: truth_ts,
                        truth_filled_qty: truth_qty,
                        censored,
                        queue_ahead_at_live: qa,
                        level_qty_at_live: lq,
                        visible_level_qty_at_live: s.visible_at_live.max(0.0).round() as Qty,
                        out_of_view_at_live: s.out_of_view,
                        instrument_id: s.input.instrument_id,
                        session_date,
                        ts_live,
                        trade_vol_60s: vol60.round() as Qty,
                    });
                    out.pred_aborted.push(abort.is_some_and(|t| dl >= t));
                    out.pred_liquidity.push(if particle {
                        None
                    } else {
                        fills
                            .first()
                            .filter(|f| f.ts <= dl)
                            .map(|f| crate::fill_liquidity(f.reason))
                    });
                }
                if write_fills && !particle {
                    for (n, f) in fills.iter().enumerate() {
                        out.fills.push(ZooFillRow {
                            model_id: model_id.to_owned(),
                            shadow_id: s.input.shadow_id.clone(),
                            fill_seq: n as u32 + 1,
                            ts_event: f.ts,
                            fill_qty: f.qty,
                            cum_qty: f.cum,
                            queue_ahead_est_before: f.qa_before,
                            reason: f.reason,
                            quirk: f.quirk,
                        });
                    }
                }
            }
        }
        let _ = lot;
        if self.priceless_rows > 0 {
            out.warnings.push(format!(
                "{} qt.l2 depth/trade rows without a price were skipped",
                self.priceless_rows
            ));
        }
        if self.unsided_trades > 0 {
            out.warnings.push(format!(
                "{} trade rows without hit side or aggressor (side N, not flagged hidden) were not dispatched",
                self.unsided_trades
            ));
        }
        out.trajectory = std::mem::take(&mut self.trajectory);
        out
    }
}

/// Prediction `(p_fill, e_fill_qty)` of an L2 pair at horizon `h`.
fn pred_l2(slot: &ModelSlot, p: &Pair, ts_live: Ts, h: Dur, qty: Qty) -> (f64, f64) {
    let dl = ts_live.saturating_add(h);
    if slot.particles {
        let n = p.st.particles.len();
        if n == 0 {
            return (0.0, 0.0);
        }
        let k =
            p.st.particles
                .iter()
                .filter(|x| x.first_fill.is_some_and(|f| f <= dl))
                .count();
        let e: f64 = p
            .pfills
            .iter()
            .filter(|x| x.0 <= dl)
            .map(|x| x.1)
            .fold(0.0, |a, b| a + b);
        return (k as f64 / n as f64, e / n as f64);
    }
    if let (Some((pp, gated)), Some(t)) = (slot.touch, p.tracker.as_ref()) {
        let _ = gated;
        if !p.live {
            return (0.0, 0.0);
        }
        let pf = if t.through.is_some_and(|x| x <= dl) {
            1.0
        } else {
            let n = t.touches.iter().filter(|x| **x <= dl).count();
            if n == 0 {
                0.0
            } else if pp >= 1.0 {
                1.0
            } else {
                1.0 - (1.0 - pp).powi(n.min(i32::MAX as usize) as i32)
            }
        };
        return (pf, pf * qty as f64);
    }
    let pf = if p.first_fill().is_some_and(|f| f <= dl) {
        1.0
    } else {
        0.0
    };
    let e: f64 = p
        .fills
        .iter()
        .filter(|f| f.ts <= dl)
        .map(|f| f.qty)
        .fold(0.0, |a, b| a + b);
    (pf, e)
}

/// Median particle first-fill time (null when fewer than half filled by `h_end`).
fn particle_median(p: &Pair, h_end: Ts) -> Option<Ts> {
    let n = p.st.particles.len();
    if n == 0 {
        return None;
    }
    let mut t: Vec<Ts> =
        p.st.particles
            .iter()
            .map(|x| x.first_fill.filter(|f| *f <= h_end).unwrap_or(Ts::MAX))
            .collect();
    t.sort_unstable();
    let m = t[(n - 1) / 2];
    if m == Ts::MAX { None } else { Some(m) }
}
