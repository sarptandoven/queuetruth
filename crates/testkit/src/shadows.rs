//! Reference shadow engine: a naive, independent implementation of SPEC 5.2 as amended by the binding audit rulings
//! A1, A3, A4, A5 and A6, E36, the matching-engine oracle rulings A3d, A4b and A6b, and audit
//! minor 15 (docs/design/spec.md), written from the text (not from `qt_replay`). Books are
//! `refbook::NaiveBook` (Vec scans); every shadow keeps its own `ahead_set` of (order id, tracked size).
//!
//! Fill rules (per event, decided at its `LAST` record on the post-event book):
//! - Queue ahead (A4) = the sum of the tracked sizes of the real orders that were ahead at activation and are still
//!   ahead; it shrinks only through order-level events on those orders (`C` / `M` reduce or remove, priority-losing
//!   `M`, which also covers executions through their removing `C`). Prints never change it.
//! - `queue_depleted` (A4 i): volume executed (`F`) against real orders behind the shadow at its side and price
//!   (resting position found through the book) at that displayed price; an execution of such an order at another
//!   price (price-improved / off-display, A4b) never fills the shadow. Also, once the queue ahead is 0 after the
//!   event's removals: usable Nasdaq side-`N` prints at the shadow's price (A3d, below) and executions of excluded
//!   own orders at the price (A1). With queue ahead left, such volume is ignored.
//! - A Nasdaq side-`N` print at `q` is usable (A3d, superseding the quote-rule reading of E36(c)) when the book
//!   at the print has no displayed bid at or above `q`, no displayed ask at or below `q` (displayed beats hidden at
//!   a price; a locked or crossed price is not continuous matching) and `q` is on the tick grid; it then reaches
//!   every shadow at or better than `q` on either side (its resting side inferred from the shadow).
//! - `trade_through` (A4 ii): `F` volume of real orders on the shadow's side resting at a price strictly worse than
//!   the shadow's (bid: lower) and executed at a price strictly worse too, plus usable Nasdaq side-`N` prints
//!   strictly through the price (A3d). Off-price executions of orders resting at the shadow's price or better are
//!   neither.
//! - `crossed` / `crossed_at_live` (A5): when the opposite best is at or through the price (after an event, or at
//!   activation), the shadow is allocated the displayed opposite orders at prices at or through its price, best
//!   price first, FIFO. Ruling 36a (conservative): the real same-side orders with priority (strictly better price,
//!   plus the queue ahead at the shadow's price) are allocated the first units of that opposite sequence; the
//!   shadow takes from each order what is left beyond their share, net of what it already took from that order
//!   on earlier events. `crossed_at_live` (the shadow arrives into the crossing book) is a TAKER walk: one fill
//!   row per opposite price level with `fill_px` = that level's price. `crossed` (a resting shadow crossed later by
//!   an opposite arrival; A5 as refined by E48) is the MAKER: the same quantity in one row at the shadow's own
//!   price. The rest keeps resting with its queue position. `fill_liquidity` gives the 1.1 liquidity column.
//! - Fill order inside an event: queue_depleted, trade_through, crossed; each capped by the remaining quantity.
//! - CME priority (A6, A6b) follows `refbook::NaiveBook` (post-fill remainder).
//! - Own orders (A1): `RefConfig::exclude_order_ids` removes those orders from the tape (their `A`/`M`/`C` records
//!   are dropped); their `F` records become unattributed execution volume at their resting price (rule above), and
//!   their `T` prints stay.
//! - Twins (A1 self-exclusion, `RefConfig::twin_linking`): an excluded order whose first record is an `A` / `M`
//!   (not a gap or bad-book record) that a shadow enters at is that shadow's twin instead: candidates are the
//!   shadows with an abs price equal to the record's, on its side, not yet linked, and either pending with
//!   `ts_live` = the record's `ts_event` or live since the start of the record's event with `ts_live` one ns
//!   earlier; the first in input order with an equal quantity, else the first. The twin is not removed from the
//!   tape (every other shadow sees a real order). Its shadow, if pending, starts at the record (a cancel / amends at
//!   or before `ts_live` first, as at an event start), activating on the book before the record with the twin left
//!   out of its queue ahead; the twin's records are never level activity for it, and each `F` of the twin while it
//!   rests at the shadow's side and price is a `queue_depleted` fill of the shadow at the `F` price, before the
//!   event's other fills. A later `A` / `M` of a twin links its next leg's shadow the same way.
//!
//! Conventions where the SPEC leaves a choice (the narrowest reading):
//! - Partitions: records are split by instrument and, when `RefConfig::instruments` has rows for it, by the row
//!   with the greatest `midnight_utc_ns <= ts_event`. Without rows the session is the UTC day of the partition's
//!   first record, open 0 s, close 86,400 s, tick `default_tick`. A shadow belongs to the partition of
//!   (`instrument_id`, `ts_submit`); none -> `censored_gap`.
//! - Event start (first record of an event, `t` = its `ts_event`), per shadow in input order: a pending shadow with
//!   `ts_live < t` first takes the cancel (effective `c <= ts_live` -> `cancelled`, never live) and the amends
//!   effective at or before `ts_live` (`new_qty = 0` -> `cancelled`, never live; else `qty = new_qty`); then it
//!   resolves its price (touch: `rejected_no_touch` / `rejected_would_cross`), activates and may fill
//!   `crossed_at_live`. A live shadow then applies the remaining amends with effective time `< t` and the cancel if
//!   `c < t`, in time order, the cancel first on a tie (`new_qty <= filled` -> `amended_to_filled`; review m12).
//!   Amends must be in time order per shadow (`QT-INPUT-002`, review m11). Amend delay = `latency_ns +
//!   gateway_ns + J_amend` (SPEC 3.1, audit minor 13).
//! - Per record: a sequence gap (when `sequence_meaningful`, per `channel_id`, snapshot records reset their channel)
//!   or a `MAYBE_BAD_BOOK` record ends live shadows `censored_gap` before the record applies; an `R` ends them
//!   `censored_clear` after it applies. A partition ending inside an event gets a synthetic event end.
//! - A duplicate `A` inside a flagged snapshot (`snapshot_records_flagged` and `SNAPSHOT`) of an order in the ahead
//!   set, at its old side and price, refreshes its tracked size and keeps it ahead (flagged snapshots re-add the book
//!   in FIFO order). Any other
//!   duplicate `A` follows the book (E36(d)): the old order is removed (an ahead order leaves the ahead set,
//!   its old size counting as a removal ahead; else a removal behind at its old level) and the new one is added at
//!   the tail of its level (behind).
//! - Rows: `sequence` of a fill / event row = the event's `LAST` record; of a live row, an event-start end and a
//!   `crossed_at_live` fill = the event's first record; of an `R` / gap end = that record; of a session end = the
//!   partition's last record; 0 for a shadow without a partition. Every shadow gets exactly one `end` row; its
//!   `ts_event` is the fill time, the effective cancel / amend time, the `R` / gap record time, `ts_live` for
//!   rejections and missing partitions, or the partition's last `ts_event`; its state repeats the last known
//!   (queue ahead, rank, level qty, filled), zeros if never live. `queue_ahead_before` of a fill = queue ahead at the
//!   start of its event (at activation for `crossed_at_live`).
//! - Truth decomposition: `cancel_ahead` = removals of ahead orders not paired with an `F` of the same order (same
//!   event, or same `sequence` on venues with `fill_removal_shares_sequence`); `traded_at_px` = at-price `T` print
//!   volume by an opposite (or unknown, non-Nasdaq) aggressor; `trade_through` = the trade-through volume above.
//! - `ambiguous_race` (race window `w > 0`, shadow activated): activation within `w` of `ts_live`, a first fill
//!   within `w` after activation, or (audit minor 15) any fill with `c - w < ts_event <= c` for the cancel's
//!   effective time `c`; never for a shadow linked to its twin (placed and filled by the twin's own records).
//! - Result row: `price` = abs price, or the resolved touch price (null if never resolved); `qty` = final quantity
//!   after amends; `ts_activated_event` = the activation event (also set for rejections); `ts_cancel_effective` =
//!   `ts_cancel + cancel delay` whenever `ts_cancel` is set; pending shadows at the end of input end like live ones
//!   (`censored_session_end` / `open_at_end`); `dq_flags` = 1 (verdict warnings) or 2 (unusable) of the partition's
//!   own data-quality verdict (`refbook::DqTracker`) when `RefConfig::dq_flags`, else 0.
//! - Trajectory `full` adds the events where `level_qty` changed or `cancel_behind` / `add_behind` is non-zero.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use qt_schema::codec::{
    PlannedEndCols, liquidity_column, planned_end_columns, shadow_fills_to_batch, shadow_results_to_batch, validate_id,
    with_added_columns,
};
use qt_schema::io::{FileEntry, TableWriter};
use qt_schema::json::Verdict;
use qt_schema::rows::{
    EndState, FillReason, FirstFillReason, Liquidity, PlannedEndKind, PriceSpec, ShadowAmend, ShadowFillRow,
    ShadowResultRow, ShadowSpec, TrajectoryKind, TrajectoryRow,
};
use qt_types::{
    Action, BookSide, Dur, Instrument, LatencyModel, MboEvent, Px, QtError, QtResult, Qty, Side, Ts, Venue, VenueRules,
    Xoshiro256StarStar, flags, stable_seed,
};

use crate::ShadowTruth;
use crate::calendar::{NS_PER_DAY, NS_PER_S};
use crate::refbook::{DqTracker, NaiveBook, RefEffect, RefOrder, SeqCheck, SeqTracker};

/// Trajectory detail (SPEC 5.2.8).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum TrajectoryDetail {
    /// `live`, `end`, and events where queue ahead, rank or filled changed.
    #[default]
    Ahead,
    /// `ahead` plus events where the level qty changed or behind activity is non-zero.
    Full,
}

/// Settings of the reference shadow engine (module doc).
#[derive(Clone, Debug, PartialEq)]
pub struct RefConfig {
    /// Venue rules.
    pub rules: VenueRules,
    /// Latency model (SPEC 3.1).
    pub latency: LatencyModel,
    /// Run seed for jitter.
    pub seed: u64,
    /// `race_window_ns` of the replay spec.
    pub race_window_ns: Dur,
    /// Trajectory detail.
    pub trajectory: TrajectoryDetail,
    /// Instrument rows (sessions, ticks); empty = UTC-day convention.
    pub instruments: Vec<Instrument>,
    /// Tick for partitions without an instrument row.
    pub default_tick: Px,
    /// Fill `dq_flags` from each partition's own data-quality verdict.
    pub dq_flags: bool,
    /// The user's own order ids, removed from the tape (audit ruling A1).
    pub exclude_order_ids: Vec<u64>,
    /// Excluded orders a shadow enters at are that shadow's twins (module doc; `qt_replay` turns it on in `replay`).
    pub twin_linking: bool,
}

impl RefConfig {
    /// Defaults: zero latency, seed 0, no race window, trajectory `ahead`, no instruments, tick 1, `dq_flags` 0, no
    /// excluded orders.
    pub fn new(rules: VenueRules) -> Self {
        Self {
            rules,
            latency: LatencyModel::default(),
            seed: 0,
            race_window_ns: 0,
            trajectory: TrajectoryDetail::Ahead,
            instruments: Vec::new(),
            default_tick: 1,
            dq_flags: false,
            exclude_order_ids: Vec::new(),
            twin_linking: false,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Status {
    Pending,
    Live,
    Done,
}

#[derive(Clone, Debug, Default)]
struct Scratch {
    removed: BTreeMap<u64, Qty>,
    filled: BTreeMap<u64, Qty>,
    filled_seq: u64,
    traded: Qty,
    exec_behind: Qty,
    exec_behind_at_display: Qty,
    through: Qty,
    hidden_at_px: Qty,
    hidden_through: Qty,
    own_at_px: Qty,
    twin: Vec<(Qty, Px)>,
    removed_behind: BTreeMap<u64, Qty>,
    filled_behind: BTreeMap<u64, Qty>,
    add_behind: Qty,
    qa_before: Qty,
    rank_before: u32,
    lvl_before: Qty,
    filled_before: Qty,
}

#[derive(Clone, Debug)]
struct Sh {
    spec: ShadowSpec,
    ts_live: Ts,
    cancel_eff: Option<Ts>,
    amends: Vec<(Ts, Qty)>,
    next_amend: usize,
    qty: Qty,
    status: Status,
    price: Option<Px>,
    ahead_set: Vec<(u64, Qty)>,
    taken: BTreeMap<u64, Qty>,
    filled: Qty,
    ts_activated: Option<Ts>,
    qa_live: Option<Qty>,
    rank_live: Option<u32>,
    lvl_live: Option<Qty>,
    last_lvl: Qty,
    fills: Vec<ShadowFillRow>,
    traj: Vec<TrajectoryRow>,
    end_state: Option<EndState>,
    dq_flags: u32,
    session: Option<(Ts, u32, u32)>,
    ev: Scratch,
    twin: Option<u64>,
    act_ev: u64,
}

/// Values shared by the fill rows of one event.
#[derive(Clone, Copy)]
struct FillCtx {
    ts: Ts,
    seq: u64,
    qa_before: Qty,
    traded: Qty,
    through: Qty,
    behind: Qty,
}

impl Sh {
    fn side(&self) -> BookSide {
        self.spec.side
    }

    fn px(&self) -> Px {
        self.price.unwrap_or_default()
    }

    fn qa(&self) -> Qty {
        self.ahead_set.iter().map(|o| o.1).sum()
    }

    fn remaining(&self) -> Qty {
        self.qty.saturating_sub(self.filled)
    }

    /// Strictly worse than the shadow's price for its side (bid: lower).
    fn worse(&self, p: Px) -> bool {
        match self.side() {
            BookSide::Bid => p < self.px(),
            BookSide::Ask => p > self.px(),
        }
    }

    fn ahead_pos(&self, id: u64) -> Option<usize> {
        self.ahead_set.iter().position(|o| o.0 == id)
    }

    fn fill(&mut self, qty: Qty, reason: FillReason, px: Px, c: FillCtx) {
        let q = qty.min(self.remaining());
        if q == 0 {
            return;
        }
        self.filled += q;
        self.fills.push(ShadowFillRow {
            shadow_id: self.spec.shadow_id.clone(),
            fill_seq: self.fills.len() as u32 + 1,
            ts_event: c.ts,
            sequence: c.seq,
            fill_qty: q,
            cum_qty: self.filled,
            leaves_qty: self.qty - self.filled,
            fill_px: px,
            reason,
            queue_ahead_before: c.qa_before,
            traded_at_px_event: c.traded,
            trade_through_event: c.through,
            behind_volume_executed: c.behind,
        });
    }

    /// Taker fills (A5) against displayed opposite orders at or through the price, best price first, FIFO. Ruling
    /// 36a: the real same-side orders with priority over the shadow (strictly better price, or the queue ahead at
    /// its price) match first, so the first `priority` units of that opposite sequence are theirs; from each opposite
    /// order the shadow takes what is left beyond their share, net of what it already took from that order.
    fn take_opposite(&mut self, book: &NaiveBook, reason: FillReason, c: FillCtx) {
        let (side, px) = (self.side(), self.px());
        let better: Qty = book
            .levels(side)
            .iter()
            .filter(|l| match side {
                BookSide::Bid => l.0 > px,
                BookSide::Ask => l.0 < px,
            })
            .map(|l| l.1)
            .sum();
        let mut priority = better + self.qa();
        let opp = side.opposite();
        // A resting shadow crossed by an opposite arrival is the maker at its own price (one row); only a shadow
        // arriving into a crossing book (`crossed_at_live`) walks the opposite levels at their prices.
        let maker = reason == FillReason::Crossed;
        let mut maker_qty = 0;
        for (p, _, _) in book.levels(opp) {
            let at_or_through = match side {
                BookSide::Bid => p <= px,
                BookSide::Ask => p >= px,
            };
            if !at_or_through || self.remaining() == 0 {
                break;
            }
            let mut left = self.remaining() - maker_qty;
            if left == 0 {
                break;
            }
            let mut got = 0;
            for o in book.orders_at(opp, p) {
                let theirs = priority.min(o.size);
                priority -= theirs;
                let used = self.taken.entry(o.order_id).or_insert(0);
                let t = (o.size - theirs).saturating_sub(*used).min(left);
                *used += t;
                left -= t;
                got += t;
                if left == 0 {
                    break;
                }
            }
            if maker {
                maker_qty += got;
            } else {
                self.fill(got, reason, p, c);
            }
        }
        if maker {
            self.fill(maker_qty, reason, px, c);
        }
    }

    fn row(&self, kind: TrajectoryKind, ts: Ts, seq: u64) -> TrajectoryRow {
        let (qa, rank, lvl) = if self.qa_live.is_some() {
            (self.qa(), self.ahead_set.len() as u32, self.last_lvl)
        } else {
            (0, 0, 0)
        };
        TrajectoryRow {
            shadow_id: self.spec.shadow_id.clone(),
            ts_event: ts,
            sequence: seq,
            kind,
            queue_ahead_before: qa,
            queue_ahead_after: qa,
            rank_before: rank,
            rank_after: rank,
            level_qty_before: lvl,
            level_qty_after: lvl,
            cancel_ahead_qty: 0,
            cancel_behind_qty: 0,
            add_behind_qty: 0,
            traded_at_px_qty: 0,
            trade_through_qty: 0,
            filled_qty: self.filled,
        }
    }

    fn end(&mut self, state: EndState, ts: Ts, seq: u64) {
        self.status = Status::Done;
        self.end_state = Some(state);
        let r = self.row(TrajectoryKind::End, ts, seq);
        self.traj.push(r);
        self.ev = Scratch::default();
    }

    /// Starts the per-event scratch from the current state.
    fn begin_event(&mut self, book: &NaiveBook) {
        let keep_filled = std::mem::take(&mut self.ev.filled);
        let keep_seq = self.ev.filled_seq;
        self.ev = Scratch {
            filled: keep_filled,
            filled_seq: keep_seq,
            qa_before: self.qa(),
            rank_before: self.ahead_set.len() as u32,
            lvl_before: book.level(self.side(), self.px()).0,
            filled_before: self.filled,
            ..Scratch::default()
        };
    }
}

fn opposite_crosses(book: &NaiveBook, side: BookSide, px: Px) -> bool {
    match side {
        BookSide::Bid => book.best_px(BookSide::Ask).is_some_and(|a| a <= px),
        BookSide::Ask => book.best_px(BookSide::Bid).is_some_and(|b| b >= px),
    }
}

fn cfg_err(msg: String) -> QtError {
    QtError::Config(msg)
}

fn nonneg(name: &str, v: Dur) -> QtResult<u64> {
    u64::try_from(v).map_err(|_| cfg_err(format!("latency.{name}: must be >= 0 (got {v})")))
}

fn add_ts(a: Ts, b: Dur, what: &str, id: &str) -> QtResult<Ts> {
    a.checked_add(b)
        .ok_or_else(|| cfg_err(format!("shadow {id}: {what} overflows the timestamp range")))
}

/// Builds the per-shadow state: latency (SPEC 3.1 jitter streams), amends (validated), cancel time.
fn prepare(cfg: &RefConfig, shadows: &[ShadowSpec], amends: &[ShadowAmend]) -> QtResult<Vec<Sh>> {
    let lm = cfg.latency;
    for (n, v) in [
        ("feed_ns", lm.feed_ns),
        ("decision_ns", lm.decision_ns),
        ("gateway_ns", lm.gateway_ns),
    ] {
        nonneg(n, v)?;
    }
    let jitter = nonneg("jitter_ns", lm.jitter_ns)?;
    let mut by_id: BTreeMap<&str, usize> = BTreeMap::new();
    for (i, s) in shadows.iter().enumerate() {
        validate_id("shadow_id", &s.shadow_id, &format!("shadows row {i}"))?;
        if by_id.insert(s.shadow_id.as_str(), i).is_some() {
            return Err(QtError::Duplicate {
                what: "shadow_id".into(),
                id: s.shadow_id.clone(),
            });
        }
        if s.latency_ns < 0 {
            return Err(QtError::InputFormat {
                location: format!("shadows row {i}"),
                msg: format!("latency_ns must be >= 0 (got {})", s.latency_ns),
            });
        }
        if s.qty == 0 {
            return Err(QtError::InputFormat {
                location: format!("shadows row {i}"),
                msg: "qty must be > 0".into(),
            });
        }
    }
    let mut per: Vec<Vec<&ShadowAmend>> = vec![Vec::new(); shadows.len()];
    for (j, a) in amends.iter().enumerate() {
        let Some(&i) = by_id.get(a.shadow_id.as_str()) else {
            return Err(QtError::InputFormat {
                location: format!("amends row {j}"),
                msg: format!("unknown shadow_id {:?}", a.shadow_id),
            });
        };
        let s = &shadows[i];
        let prev = per[i].last().map_or(s.qty, |p| p.new_qty);
        let prev_send = per[i].last().map_or(s.ts_submit, |p| p.ts_send);
        if a.ts_send < prev_send || a.new_qty >= prev {
            return Err(QtError::InputFormat {
                location: format!("amends row {j}"),
                msg: format!(
                    "amend of {:?} must have ts_send >= ts_submit and >= the previous amend's (time order), and \
                     new_qty strictly decreasing (< {prev})",
                    a.shadow_id
                ),
            });
        }
        per[i].push(a);
    }
    let mut out = Vec::with_capacity(shadows.len());
    for (i, s) in shadows.iter().enumerate() {
        let id = s.shadow_id.as_str();
        let mut draws = if jitter > 0 {
            Some(Xoshiro256StarStar::new(stable_seed(
                cfg.seed,
                &[b"latency", id.as_bytes()],
            )))
        } else {
            None
        };
        let mut draw = || -> Dur { draws.as_mut().map_or(0, |r| r.range_inclusive(0, jitter) as Dur) };
        let j_submit = draw();
        let j_cancel = draw();
        let submit_delay = [lm.feed_ns, lm.decision_ns, lm.gateway_ns, j_submit]
            .iter()
            .try_fold(s.latency_ns, |acc, x| acc.checked_add(*x))
            .ok_or_else(|| cfg_err(format!("shadow {id}: submit delay overflows")))?;
        let cancel_delay = [lm.gateway_ns, j_cancel]
            .iter()
            .try_fold(s.latency_ns, |acc, x| acc.checked_add(*x))
            .ok_or_else(|| cfg_err(format!("shadow {id}: cancel delay overflows")))?;
        let ts_live = add_ts(s.ts_submit, submit_delay, "ts_live", id)?;
        let cancel_eff = match s.ts_cancel {
            Some(c) => Some(add_ts(c, cancel_delay, "cancel time", id)?),
            None => None,
        };
        let mut am = Vec::with_capacity(per[i].len());
        for a in &per[i] {
            let j = draw();
            let delay = s
                .latency_ns
                .checked_add(lm.gateway_ns)
                .and_then(|x| x.checked_add(j))
                .ok_or_else(|| cfg_err(format!("shadow {id}: amend delay overflows")))?;
            am.push((add_ts(a.ts_send, delay, "amend time", id)?, a.new_qty));
        }
        out.push(Sh {
            spec: s.clone(),
            ts_live,
            cancel_eff,
            amends: am,
            next_amend: 0,
            qty: s.qty,
            status: Status::Pending,
            price: match s.price {
                PriceSpec::Abs(p) => Some(p),
                PriceSpec::Touch { .. } => None,
            },
            ahead_set: Vec::new(),
            taken: BTreeMap::new(),
            filled: 0,
            ts_activated: None,
            qa_live: None,
            rank_live: None,
            lvl_live: None,
            last_lvl: 0,
            fills: Vec::new(),
            traj: Vec::new(),
            end_state: None,
            dq_flags: 0,
            session: None,
            ev: Scratch::default(),
            twin: None,
            act_ev: 0,
        });
    }
    Ok(out)
}

/// One partition: instrument, session bounds, tick, record indices.
struct Part {
    midnight: Ts,
    open_s: u32,
    close_s: u32,
    tick: Px,
    recs: Vec<usize>,
    shadows: Vec<usize>,
}

fn row_for(insts: &[Instrument], inst: u32, ts: Ts) -> Option<usize> {
    let mut best: Option<usize> = None;
    for (i, r) in insts.iter().enumerate() {
        if r.instrument_id == inst
            && r.midnight_utc_ns <= ts
            && best.is_none_or(|b| insts[b].midnight_utc_ns < r.midnight_utc_ns)
        {
            best = Some(i);
        }
    }
    best
}

fn has_rows(insts: &[Instrument], inst: u32) -> bool {
    insts.iter().any(|r| r.instrument_id == inst)
}

/// Fallible reference engine with explicit settings (module doc); output in input order.
pub fn try_reference_shadows(
    events: &[MboEvent],
    cfg: &RefConfig,
    shadows: &[ShadowSpec],
    amends: &[ShadowAmend],
) -> QtResult<Vec<ShadowTruth>> {
    let mut shs = prepare(cfg, shadows, amends)?;
    let insts = &cfg.instruments;
    let mut keys: BTreeMap<(u32, Option<usize>), usize> = BTreeMap::new();
    let mut parts: Vec<Part> = Vec::new();
    for (i, e) in events.iter().enumerate() {
        let row = if has_rows(insts, e.instrument_id) {
            row_for(insts, e.instrument_id, e.ts_event)
        } else {
            None
        };
        let k = *keys.entry((e.instrument_id, row)).or_insert_with(|| {
            let (midnight, open_s, close_s, tick) = match row {
                Some(r) => (
                    insts[r].midnight_utc_ns,
                    insts[r].open_s,
                    insts[r].close_s,
                    insts[r].tick_size,
                ),
                None => (
                    e.ts_event.div_euclid(NS_PER_DAY) * NS_PER_DAY,
                    0,
                    86_400,
                    cfg.default_tick,
                ),
            };
            parts.push(Part {
                midnight,
                open_s,
                close_s,
                tick: tick.max(1),
                recs: Vec::new(),
                shadows: Vec::new(),
            });
            parts.len() - 1
        });
        parts[k].recs.push(i);
    }
    for (i, s) in shs.iter_mut().enumerate() {
        let inst = s.spec.instrument_id;
        let key = if has_rows(insts, inst) {
            (inst, row_for(insts, inst, s.spec.ts_submit))
        } else {
            (inst, None)
        };
        let part = if has_rows(insts, inst) && key.1.is_none() {
            None
        } else {
            keys.get(&key).copied()
        };
        match part {
            Some(p) => parts[p].shadows.push(i),
            None => {
                if let Some(r) = row_for(insts, inst, s.spec.ts_submit) {
                    let r = &insts[r];
                    s.session = Some((r.midnight_utc_ns, r.open_s, r.close_s));
                }
                let ts = s.ts_live;
                s.end(EndState::CensoredGap, ts, 0);
            }
        }
    }
    for p in &parts {
        run_partition(cfg, p, events, &mut shs);
    }
    Ok(shs.into_iter().map(|s| finish(cfg, s)).collect())
}

fn finish(cfg: &RefConfig, s: Sh) -> ShadowTruth {
    let first = s.fills.first();
    let full = s.fills.iter().find(|f| f.cum_qty == s.qty).map(|f| f.ts_event);
    let first_reason = first.map_or(FirstFillReason::Never, |f| match f.reason {
        FillReason::QueueDepleted => FirstFillReason::QueueDepleted,
        FillReason::TradeThrough => FirstFillReason::TradeThrough,
        FillReason::Crossed => FirstFillReason::Crossed,
        FillReason::CrossedAtLive => FirstFillReason::CrossedAtLive,
    });
    let race = cfg.race_window_ns;
    // Audit minor 15: a fill within the race window before the cancel's effective time `c` also raced.
    let cancel_race = s
        .cancel_eff
        .is_some_and(|c| s.fills.iter().any(|f| f.ts_event <= c && c - f.ts_event < race));
    let ambiguous_race = race > 0
        && s.twin.is_none()
        && s.ts_activated.is_some_and(|act| {
            act - s.ts_live <= race
                || first.is_some_and(|f| f.ts_event >= act && f.ts_event - act <= race)
                || cancel_race
        });
    let out_of_session = s.session.is_some_and(|(m, o, c)| {
        let open = m + i64::from(o) * NS_PER_S;
        let close = m + i64::from(c) * NS_PER_S;
        s.spec.ts_submit < open || s.spec.ts_submit >= close
    });
    let res = ShadowResultRow {
        shadow_id: s.spec.shadow_id.clone(),
        instrument_id: s.spec.instrument_id,
        side: s.spec.side,
        price: s.price,
        qty: s.qty,
        ts_submit: s.spec.ts_submit,
        ts_live: s.ts_live,
        ts_activated_event: s.ts_activated,
        ts_cancel_effective: s.cancel_eff,
        queue_ahead_at_live: s.qa_live,
        rank_at_live: s.rank_live,
        level_qty_at_live: s.lvl_live,
        ts_first_fill: first.map(|f| f.ts_event),
        ts_last_fill: s.fills.last().map(|f| f.ts_event),
        ts_full_fill: full,
        filled_qty: s.filled,
        n_fills: s.fills.len() as u32,
        queue_ahead_at_first_fill: first.map(|f| f.queue_ahead_before),
        first_fill_reason: first_reason,
        end_state: s.end_state.unwrap_or(EndState::OpenAtEnd),
        latency_applied_ns: s.ts_live - s.spec.ts_submit,
        ambiguous_race,
        out_of_session,
        dq_flags: s.dq_flags,
    };
    (res, s.fills, s.traj)
}

fn run_partition(cfg: &RefConfig, p: &Part, events: &[MboEvent], shs: &mut [Sh]) {
    let rules = cfg.rules;
    let dq_bits = if cfg.dq_flags {
        let mut t = DqTracker::new(rules);
        for &i in &p.recs {
            t.push(&events[i]);
        }
        match t.finish().0.verdict {
            Verdict::Clean => 0,
            Verdict::Warnings => 1,
            Verdict::Unusable => 2,
        }
    } else {
        0
    };
    for &i in &p.shadows {
        shs[i].dq_flags = dq_bits;
        shs[i].session = Some((p.midnight, p.open_s, p.close_s));
    }
    let excluded: BTreeSet<u64> = cfg.exclude_order_ids.iter().copied().collect();
    let mut own: BTreeMap<u64, (BookSide, Px)> = BTreeMap::new();
    // Excluded order -> true for a twin, false for removed (decided at its first record).
    let mut kind: BTreeMap<u64, bool> = BTreeMap::new();
    let mut ev_idx = 0u64;
    let mut book = NaiveBook::new(rules);
    let mut seqs = SeqTracker::default();
    let mut in_event = false;
    let mut last: Option<&MboEvent> = None;
    for &ri in &p.recs {
        let e = &events[ri];
        if !in_event {
            in_event = true;
            ev_idx += 1;
            for &i in &p.shadows {
                let was_pending = shs[i].status == Status::Pending;
                event_start(&mut shs[i], &book, e, p.tick);
                if was_pending && shs[i].status == Status::Live {
                    shs[i].act_ev = ev_idx;
                }
            }
        }
        let gap = rules.sequence_meaningful && seqs.check(e) == SeqCheck::Gap;
        let bad = gap || e.flags & flags::MAYBE_BAD_BOOK != 0;
        if bad {
            for &i in &p.shadows {
                if shs[i].status == Status::Live {
                    shs[i].end(EndState::CensoredGap, e.ts_event, e.sequence);
                }
            }
        }
        let action = Action::from_byte(e.action);
        let ours = e.order_id != 0 && excluded.contains(&e.order_id);
        if ours && matches!(action, Some(Action::Add | Action::Modify)) && !bad && kind.get(&e.order_id) != Some(&false)
        {
            let linked = cfg.twin_linking && link_twin(p, shs, &book, e, ev_idx);
            kind.entry(e.order_id).or_insert(linked);
        }
        if ours {
            kind.entry(e.order_id).or_insert(false);
        }
        if ours && kind.get(&e.order_id) == Some(&false) {
            match action {
                Some(Action::Add | Action::Modify) => {
                    if let Some(side) = BookSide::from_byte(e.side) {
                        own.insert(e.order_id, (side, e.price));
                    }
                }
                Some(Action::Fill) => {
                    if let Some(&(side, px)) = own.get(&e.order_id) {
                        for &i in &p.shadows {
                            let s = &mut shs[i];
                            if s.status == Status::Live && s.side() == side {
                                // A4b: own volume counts at its displayed price only; through it, executed
                                // through the shadow's price too.
                                if px == s.px() {
                                    if e.price == px {
                                        s.ev.own_at_px += e.size;
                                    }
                                } else if s.worse(px) && s.worse(e.price) {
                                    s.ev.through += e.size;
                                }
                            }
                        }
                    }
                }
                _ => {}
            }
        } else {
            let before = if action == Some(Action::Add) {
                book.order(e.order_id)
            } else {
                None
            };
            let effect = book.apply(e);
            let dup = before.filter(|_| matches!(effect, RefEffect::Add { .. }));
            if effect == RefEffect::Clear {
                for &i in &p.shadows {
                    if shs[i].status == Status::Live {
                        shs[i].end(EndState::CensoredClear, e.ts_event, e.sequence);
                    }
                }
            } else {
                for &i in &p.shadows {
                    if shs[i].status == Status::Live {
                        on_record(&mut shs[i], &book, e, &effect, dup, &rules, p.tick);
                    }
                }
            }
        }
        if e.is_last() {
            for &i in &p.shadows {
                if shs[i].status == Status::Live {
                    event_end(&mut shs[i], &book, e, &rules, cfg.trajectory);
                }
            }
            book.note_event_end();
            in_event = false;
        }
        last = Some(e);
    }
    let Some(last) = last else {
        return;
    };
    if in_event {
        for &i in &p.shadows {
            if shs[i].status == Status::Live {
                event_end(&mut shs[i], &book, last, &rules, cfg.trajectory);
            }
        }
    }
    let close = p.midnight + i64::from(p.close_s) * NS_PER_S;
    let state = if last.ts_event >= close {
        EndState::CensoredSessionEnd
    } else {
        EndState::OpenAtEnd
    };
    for &i in &p.shadows {
        if shs[i].status != Status::Done {
            shs[i].end(state, last.ts_event, last.sequence);
        }
    }
}

/// Links the twin of the excluded order of record `e` (an `A` / `M`) to its shadow (module doc): returns whether one
/// was linked.
fn link_twin(p: &Part, shs: &mut [Sh], book: &NaiveBook, e: &MboEvent, ev_idx: u64) -> bool {
    let Some(side) = BookSide::from_byte(e.side) else {
        return false;
    };
    let t = e.ts_event;
    let mut best: Option<(bool, usize)> = None;
    for &i in &p.shadows {
        let s = &shs[i];
        let ok = match s.status {
            Status::Pending => s.ts_live == t,
            Status::Live => s.ts_live == t - 1 && s.act_ev == ev_idx,
            Status::Done => false,
        };
        if !ok || s.twin.is_some() || s.side() != side || !matches!(s.spec.price, PriceSpec::Abs(px) if px == e.price) {
            continue;
        }
        let key = (s.qty != e.size, i);
        if best.is_none_or(|b| key < b) {
            best = Some(key);
        }
    }
    let Some((_, i)) = best else {
        return false;
    };
    let s = &mut shs[i];
    s.twin = Some(e.order_id);
    if s.status == Status::Live {
        if let Some(k) = s.ahead_pos(e.order_id) {
            s.ahead_set.remove(k);
        }
        return true;
    }
    let seq = e.sequence;
    if let Some(c) = s.cancel_eff.filter(|&c| c <= s.ts_live) {
        s.end(EndState::Cancelled, c, seq);
        return true;
    }
    while s.next_amend < s.amends.len() && s.amends[s.next_amend].0 <= s.ts_live {
        let (a, q) = s.amends[s.next_amend];
        s.next_amend += 1;
        if q == 0 {
            s.end(EndState::Cancelled, a, seq);
            return true;
        }
        s.qty = q;
    }
    activate(s, book, t, seq, p.tick);
    s.act_ev = ev_idx;
    if s.status == Status::Live {
        s.begin_event(book);
    }
    true
}

fn event_start(s: &mut Sh, book: &NaiveBook, first: &MboEvent, tick: Px) {
    let t = first.ts_event;
    let seq = first.sequence;
    if s.status == Status::Pending && s.ts_live < t {
        if let Some(c) = s.cancel_eff.filter(|&c| c <= s.ts_live) {
            s.end(EndState::Cancelled, c, seq);
            return;
        }
        while s.next_amend < s.amends.len() && s.amends[s.next_amend].0 <= s.ts_live {
            let (a, q) = s.amends[s.next_amend];
            s.next_amend += 1;
            if q == 0 {
                s.end(EndState::Cancelled, a, seq);
                return;
            }
            s.qty = q;
        }
        activate(s, book, t, seq, tick);
    }
    if s.status == Status::Live {
        // Amends and the cancel due before `t` apply in time order, the cancel first on a tie (review m12).
        loop {
            let amend = s.amends.get(s.next_amend).copied().filter(|&(a, _)| a < t);
            let cancel = s.cancel_eff.filter(|&c| c < t);
            match (cancel, amend) {
                (Some(c), Some((a, _))) if c <= a => {
                    s.end(EndState::Cancelled, c, seq);
                    return;
                }
                (Some(c), None) => {
                    s.end(EndState::Cancelled, c, seq);
                    return;
                }
                (_, Some((a, q))) => {
                    s.next_amend += 1;
                    if q <= s.filled {
                        s.end(EndState::AmendedToFilled, a, seq);
                        return;
                    }
                    s.qty = q;
                }
                (None, None) => break,
            }
        }
        s.begin_event(book);
    }
}

fn activate(s: &mut Sh, book: &NaiveBook, t: Ts, seq: u64, tick: Px) {
    s.ts_activated = Some(t);
    let side = s.side();
    if let PriceSpec::Touch { offset_ticks } = s.spec.price {
        let Some(b) = book.best_px(side) else {
            let ts = s.ts_live;
            s.end(EndState::RejectedNoTouch, ts, seq);
            return;
        };
        let delta = i64::from(offset_ticks).saturating_mul(tick);
        let px = match side {
            BookSide::Bid => b.saturating_add(delta),
            BookSide::Ask => b.saturating_sub(delta),
        };
        s.price = Some(px);
        if offset_ticks > 0 && opposite_crosses(book, side, px) {
            let ts = s.ts_live;
            s.end(EndState::RejectedWouldCross, ts, seq);
            return;
        }
    }
    let px = s.px();
    // A twin resting at the level (a leg at a priority-keeping `M`) is the shadow itself, never ahead.
    let orders: Vec<_> = book
        .orders_at(side, px)
        .into_iter()
        .filter(|o| Some(o.order_id) != s.twin)
        .collect();
    let total: Qty = orders.iter().map(|o| o.size).sum();
    s.ahead_set = orders.iter().map(|o| (o.order_id, o.size)).collect();
    s.qa_live = Some(total);
    s.rank_live = Some(s.ahead_set.len() as u32);
    s.lvl_live = Some(total);
    s.last_lvl = total;
    s.status = Status::Live;
    let live = s.row(TrajectoryKind::Live, s.ts_live, seq);
    s.traj.push(live);
    if opposite_crosses(book, side, px) {
        let ctx = FillCtx {
            ts: s.ts_live,
            seq,
            qa_before: total,
            traded: 0,
            through: 0,
            behind: 0,
        };
        s.take_opposite(book, FillReason::CrossedAtLive, ctx);
        if s.remaining() == 0 {
            let ts = s.ts_live;
            s.end(EndState::Filled, ts, seq);
        }
    }
}

fn on_record(
    s: &mut Sh,
    book: &NaiveBook,
    e: &MboEvent,
    effect: &RefEffect,
    dup: Option<RefOrder>,
    rules: &VenueRules,
    tick: Px,
) {
    if rules.fill_removal_shares_sequence && e.sequence != s.ev.filled_seq {
        s.ev.filled.clear();
        s.ev.filled_seq = e.sequence;
    }
    let (side, px) = (s.side(), s.px());
    if e.order_id != 0 && s.twin == Some(e.order_id) {
        // A1: the twin's records are the shadow's own; its executions at the shadow's level are its fills.
        if Action::from_byte(e.action) == Some(Action::Fill)
            && book.order(e.order_id).is_some_and(|o| o.side == side && o.price == px)
        {
            s.ev.twin.push((e.size, e.price));
        }
        return;
    }
    match *effect {
        RefEffect::Remove {
            order_id,
            side: os,
            price,
            size,
        } => {
            if let Some(i) = s.ahead_pos(order_id) {
                s.ahead_set.remove(i);
                *s.ev.removed.entry(order_id).or_default() += size;
            } else if os == side && price == px {
                *s.ev.removed_behind.entry(order_id).or_default() += size;
            }
        }
        RefEffect::Replace {
            order_id,
            side: os,
            old_price,
            old_size,
            new_price,
            new_size,
        } => {
            if let Some(i) = s.ahead_pos(order_id) {
                s.ahead_set.remove(i);
                *s.ev.removed.entry(order_id).or_default() += old_size;
            } else if os == side && old_price == px {
                *s.ev.removed_behind.entry(order_id).or_default() += old_size;
            }
            if os == side && new_price == px {
                s.ev.add_behind += new_size;
            }
        }
        RefEffect::Reduce {
            order_id,
            side: os,
            price,
            by,
        } => {
            if let Some(i) = s.ahead_pos(order_id) {
                s.ahead_set[i].1 = s.ahead_set[i].1.saturating_sub(by);
                *s.ev.removed.entry(order_id).or_default() += by;
            } else if os == side && price == px {
                *s.ev.removed_behind.entry(order_id).or_default() += by;
            }
        }
        RefEffect::Add {
            order_id,
            side: os,
            price,
            size,
        } => {
            // A flagged snapshot re-add at the same level keeps its place; at another price it leaves the level
            // (review m15) like any duplicate.
            let snapshot_readd = rules.snapshot_records_flagged
                && e.flags & flags::SNAPSHOT != 0
                && dup.is_some_and(|o| o.side == os && o.price == price);
            match dup.filter(|_| !snapshot_readd) {
                // Ruling 36d: a duplicate add outside a flagged snapshot follows the book (old order removed, new
                // one appended at the tail of its level).
                Some(old) => {
                    if let Some(i) = s.ahead_pos(order_id) {
                        s.ahead_set.remove(i);
                        *s.ev.removed.entry(order_id).or_default() += old.size;
                    } else if old.side == side && old.price == px {
                        *s.ev.removed_behind.entry(order_id).or_default() += old.size;
                    }
                    if os == side && price == px {
                        s.ev.add_behind += size;
                    }
                }
                None => {
                    if let Some(i) = s.ahead_pos(order_id) {
                        s.ahead_set[i].1 = size;
                    } else if os == side && price == px {
                        s.ev.add_behind += size;
                    }
                }
            }
        }
        RefEffect::None | RefEffect::Clear => {}
    }
    match Action::from_byte(e.action) {
        Some(Action::Fill) => {
            if s.ahead_pos(e.order_id).is_some() {
                *s.ev.filled.entry(e.order_id).or_default() += e.size;
            } else if let Some(o) = book.order(e.order_id).filter(|o| o.side == side) {
                if o.price == px {
                    s.ev.exec_behind += e.size;
                    *s.ev.filled_behind.entry(e.order_id).or_default() += e.size;
                    // A4b: only an execution at the order's displayed price is volume reaching the shadow.
                    if e.price == o.price {
                        s.ev.exec_behind_at_display += e.size;
                    }
                } else if s.worse(o.price) && s.worse(e.price) {
                    s.ev.through += e.size;
                }
            }
        }
        Some(Action::Trade) => {
            let ts = Side::from_byte(e.side);
            if ts == Side::None && rules.venue == Venue::Nasdaq {
                // A3d: usable only with no displayed bid at or above and no displayed ask at or below the print
                // (book at the print), on the tick grid; it then reaches the shadow from its own side.
                let q = e.price;
                let usable = book.best_px(BookSide::Bid).is_none_or(|b| b < q)
                    && book.best_px(BookSide::Ask).is_none_or(|a| a > q)
                    && q.rem_euclid(tick.max(1)) == 0;
                if usable {
                    if q == px {
                        s.ev.hidden_at_px += e.size;
                    } else if s.worse(q) {
                        s.ev.hidden_through += e.size;
                    }
                }
            } else if !(ts == Side::None && rules.supports_hidden_orders) && ts != side.to_side() && e.price == px {
                s.ev.traded += e.size;
            }
        }
        _ => {}
    }
}

fn event_end(s: &mut Sh, book: &NaiveBook, last: &MboEvent, rules: &VenueRules, detail: TrajectoryDetail) {
    let (t, seq) = (last.ts_event, last.sequence);
    let pure: Qty =
        s.ev.removed
            .iter()
            .map(|(id, r)| r.saturating_sub(s.ev.filled.get(id).copied().unwrap_or(0)))
            .sum();
    let qa_start = s.ev.qa_before;
    let qa_now = s.qa();
    let mut behind = s.ev.exec_behind_at_display;
    if qa_now == 0 {
        behind += s.ev.hidden_at_px + s.ev.own_at_px;
    }
    let through = s.ev.through + s.ev.hidden_through;
    let ctx = FillCtx {
        ts: t,
        seq,
        qa_before: qa_start,
        traded: s.ev.traded,
        through,
        behind: s.ev.exec_behind,
    };
    let px = s.px();
    for (q, p) in std::mem::take(&mut s.ev.twin) {
        s.fill(q, FillReason::QueueDepleted, p, ctx);
    }
    s.fill(behind, FillReason::QueueDepleted, px, ctx);
    s.fill(through, FillReason::TradeThrough, px, ctx);
    if s.remaining() > 0 && opposite_crosses(book, s.side(), px) {
        s.take_opposite(book, FillReason::Crossed, ctx);
    }
    let rank_after = s.ahead_set.len() as u32;
    let lvl_after = book.level(s.side(), px).0;
    let cancel_behind: Qty =
        s.ev.removed_behind
            .iter()
            .map(|(id, r)| r.saturating_sub(s.ev.filled_behind.get(id).copied().unwrap_or(0)))
            .sum();
    let changed = qa_now != qa_start || rank_after != s.ev.rank_before || s.filled != s.ev.filled_before;
    let full_extra =
        detail == TrajectoryDetail::Full && (lvl_after != s.ev.lvl_before || cancel_behind > 0 || s.ev.add_behind > 0);
    if changed || full_extra {
        s.traj.push(TrajectoryRow {
            shadow_id: s.spec.shadow_id.clone(),
            ts_event: t,
            sequence: seq,
            kind: TrajectoryKind::Event,
            queue_ahead_before: qa_start,
            queue_ahead_after: qa_now,
            rank_before: s.ev.rank_before,
            rank_after,
            level_qty_before: s.ev.lvl_before,
            level_qty_after: lvl_after,
            cancel_ahead_qty: pure.min(qa_start),
            cancel_behind_qty: cancel_behind,
            add_behind_qty: s.ev.add_behind,
            traded_at_px_qty: s.ev.traded,
            trade_through_qty: through,
            filled_qty: s.filled,
        });
    }
    s.last_lvl = lvl_after;
    if rules.fill_removal_shares_sequence {
        let removed = std::mem::take(&mut s.ev.removed);
        for (id, r) in &removed {
            if let Some(f) = s.ev.filled.get_mut(id) {
                *f = f.saturating_sub(*r);
            }
        }
        s.ev.filled.retain(|_, f| *f > 0);
    } else {
        s.ev.filled.clear();
    }
    if s.remaining() == 0 {
        s.end(EndState::Filled, t, seq);
    } else {
        // The next event of a still-live shadow starts from this state (event_start refreshes it).
        s.begin_event(book);
    }
}

/// Planned (exogenous) end of a shadow (audit ruling A2; schema 1.1 `qt.shadow_results.ts_planned_end` and
/// `planned_end_kind`): the earlier of its cancel's effective time and the session close of its instrument row (the
/// row with the greatest `midnight_utc_ns <= ts_submit`), the cancel winning a tie; `none` without either.
pub fn planned_end(r: &ShadowResultRow, instruments: &[Instrument]) -> PlannedEndCols {
    let close = row_for(instruments, r.instrument_id, r.ts_submit).map(|i| {
        let x = &instruments[i];
        x.midnight_utc_ns + i64::from(x.close_s) * NS_PER_S
    });
    match (r.ts_cancel_effective, close) {
        (Some(c), Some(e)) if e < c => (Some(e), PlannedEndKind::SessionClose),
        (Some(c), _) => (Some(c), PlannedEndKind::Cancel),
        (None, Some(e)) => (Some(e), PlannedEndKind::SessionClose),
        (None, None) => (None, PlannedEndKind::None),
    }
}

/// Writes `qt.shadow_results` rows in the full schema-1.1 layout (planned ends from `planned_end`).
pub fn write_shadow_results(path: &Path, rows: &[ShadowResultRow], instruments: &[Instrument]) -> QtResult<FileEntry> {
    let planned: Vec<PlannedEndCols> = rows.iter().map(|r| planned_end(r, instruments)).collect();
    let batch = with_added_columns(
        "qt.shadow_results",
        &shadow_results_to_batch(rows),
        planned_end_columns(&planned),
    )?;
    let mut w = TableWriter::create_full(path, "qt.shadow_results")?;
    w.write(&batch)?;
    w.finish()
}

/// Writes `qt.shadow_fills` rows in the full schema-1.1 layout with their `liquidity` (`fill_liquidity`).
pub fn write_shadow_fills(path: &Path, rows: &[ShadowFillRow], liquidity: &[Liquidity]) -> QtResult<FileEntry> {
    if rows.len() != liquidity.len() {
        return Err(QtError::Engine("write_shadow_fills: one liquidity per fill".into()));
    }
    let batch = with_added_columns(
        "qt.shadow_fills",
        &shadow_fills_to_batch(rows),
        vec![liquidity_column(liquidity.iter().map(|l| Some(*l)))],
    )?;
    let mut w = TableWriter::create_full(path, "qt.shadow_fills")?;
    w.write(&batch)?;
    w.finish()
}

/// Liquidity of reference fills (schema 1.1; A5 as refined by E48): taker only for
/// `crossed_at_live` (the shadow arrives into a crossing book); a resting shadow crossed later (`crossed`) is the
/// maker at its own price, as are `queue_depleted` and `trade_through` fills.
pub fn fill_liquidity(fills: &[ShadowFillRow]) -> Vec<Liquidity> {
    fills
        .iter()
        .map(|f| match f.reason {
            FillReason::CrossedAtLive => Liquidity::Taker,
            FillReason::Crossed | FillReason::QueueDepleted | FillReason::TradeThrough => Liquidity::Maker,
        })
        .collect()
}
