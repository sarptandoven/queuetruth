//! One shadow's state and its event-end step (SPEC 5.2.5): pure cancels ahead shrink `ahead`, cumulative at-price
//! volume beyond `ahead` fills (`queue_depleted`), trade-throughs fill (`trade_through`), a marketable book fills the
//! remainder (`crossed`). Fill removals (`C`/`M` paired with an `F` of the same order, within the event or by
//! `sequence`) never count as cancels, so nothing is double counted. A shadow that replaces its excluded MBO twin
//! (audit ruling A1, self-exclusion) is filled by the twin's own executions and never sees the twin's records as
//! level activity.

use qt_book::{Book, IdSet, LevelView};
use qt_schema::rows::{EndState, FillReason, FirstFillReason, PriceSpec, ShadowFillRow};
use qt_types::{BookSide, Px, Qty, Ts};

use crate::ShadowStatus;

/// Adds `q` to the entry of `id` in a small association list.
#[inline]
pub(crate) fn bump(v: &mut Vec<(u64, Qty)>, id: u64, q: Qty) {
    if let Some(e) = v.iter_mut().find(|e| e.0 == id) {
        e.1 = e.1.saturating_add(q);
    } else {
        v.push((id, q));
    }
}

#[inline]
fn get(v: &[(u64, Qty)], id: u64) -> Qty {
    v.iter().find(|e| e.0 == id).map_or(0, |e| e.1)
}

/// What happened at one (side, price) level during the current event, shared by every shadow there: the level
/// total at event start, added volume (always behind), removals and executions by order id, and at-price print
/// volume of opposite (or unknown, where it consumes) aggressors. A shadow derives its behind-side truth from it by
/// leaving out its own ahead orders, so adds and behind cancels never visit the shadows of a level.
#[derive(Clone, Debug, Default)]
pub(crate) struct LevelAcc {
    pub(crate) before: Qty,
    pub(crate) add: Qty,
    pub(crate) removed: Vec<(u64, Qty)>,
    pub(crate) filled: Vec<(u64, Qty)>,
    /// Executions at a price other than the level's (A4b): pair with removals, never fill a shadow.
    pub(crate) off_price: Vec<(u64, Qty)>,
    pub(crate) traded: Qty,
}

impl LevelAcc {
    /// (cancel_behind, behind_exec) for a shadow whose ahead removals / fills this event are `ahead_removed` and
    /// `ahead_filled`; the shadow's own twin (`twin`, A1) is never behind it.
    pub(crate) fn behind(&self, ahead_removed: &[(u64, Qty)], ahead_filled: &[u64], twin: Option<u64>) -> (Qty, Qty) {
        let cancel = self
            .removed
            .iter()
            .filter(|(id, _)| !ahead_removed.iter().any(|e| e.0 == *id) && Some(*id) != twin)
            .map(|&(id, r)| r.saturating_sub(get(&self.filled, id)))
            .fold(0, Qty::saturating_add);
        let exec = self
            .filled
            .iter()
            .filter(|(id, _)| !ahead_filled.contains(id) && Some(*id) != twin)
            .map(|e| e.1)
            .fold(0, Qty::saturating_add);
        (cancel, exec)
    }

    /// Off-price execution volume of orders other than the shadow's ahead orders with fills this event (and its
    /// twin).
    pub(crate) fn behind_off_price(&self, ahead_filled: &[u64], twin: Option<u64>) -> Qty {
        self.off_price
            .iter()
            .filter(|(id, _)| !ahead_filled.contains(id) && Some(*id) != twin)
            .map(|e| e.1)
            .fold(0, Qty::saturating_add)
    }

    /// The activity accumulated after `base` (a copy of this accumulator taken when a shadow activated in the middle
    /// of the event); `before` is the base's (the level total at that activation).
    pub(crate) fn since(&self, base: &LevelAcc) -> LevelAcc {
        let diff = |now: &[(u64, Qty)], then: &[(u64, Qty)]| -> Vec<(u64, Qty)> {
            now.iter()
                .map(|&(id, q)| (id, q.saturating_sub(get(then, id))))
                .filter(|e| e.1 > 0)
                .collect()
        };
        LevelAcc {
            before: base.before,
            add: self.add.saturating_sub(base.add),
            removed: diff(&self.removed, &base.removed),
            filled: diff(&self.filled, &base.filled),
            off_price: diff(&self.off_price, &base.off_price),
            traded: self.traded.saturating_sub(base.traded),
        }
    }
}

/// Totals of one event-end step.
#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct StepOut {
    pub(crate) qa_before_step: Qty,
    pub(crate) qa_after: Qty,
    pub(crate) pure: Qty,
    pub(crate) cancel_behind: Qty,
    pub(crate) add_behind: Qty,
    pub(crate) traded: Qty,
    pub(crate) crossed: Qty,
    pub(crate) filled_now: Qty,
    pub(crate) hidden_fill: bool,
    pub(crate) hidden_blocked: bool,
    pub(crate) taker_fills: u64,
    pub(crate) crossed_unfilled: bool,
    /// The book crossed the shadow's price at event end (A5).
    pub(crate) crossed_book: bool,
    pub(crate) behind_with_ahead: bool,
    /// Twin executions at the shadow's price credited while displayed queue ahead remained after the event (A1).
    pub(crate) twin_with_ahead: u64,
    /// Twin executions at a price other than the shadow's (display-slid orders; A1).
    pub(crate) twin_off_display: u64,
}

/// One shadow.
#[derive(Clone, Debug)]
pub(crate) struct Shadow {
    pub(crate) instrument_id: u32,
    pub(crate) side: BookSide,
    pub(crate) price_spec: PriceSpec,
    pub(crate) tick: Px,
    pub(crate) price: Option<Px>,
    pub(crate) qty: Qty,
    pub(crate) ts_live: Ts,
    pub(crate) cancel_eff: Option<Ts>,
    /// (effective time, new total qty) in amend row order.
    pub(crate) amends: Vec<(Ts, Qty)>,
    pub(crate) next_amend: usize,
    pub(crate) status: ShadowStatus,
    pub(crate) end_state: Option<EndState>,
    pub(crate) ts_end: Option<Ts>,
    pub(crate) ts_activated_event: Option<Ts>,
    pub(crate) qa_live: Option<Qty>,
    pub(crate) rank_live: Option<u32>,
    pub(crate) level_live: Option<Qty>,
    pub(crate) ahead_set: IdSet<u64>,
    /// Queue ahead reduced only by pure cancels.
    pub(crate) ahead: Qty,
    /// Cumulative at-price traded volume since live.
    pub(crate) traded: u128,
    /// Queue ahead, rank and level quantity after the last processed event.
    pub(crate) qa: Qty,
    pub(crate) rank: u32,
    pub(crate) level_qty: Qty,
    pub(crate) filled: Qty,
    pub(crate) n_fills: u32,
    pub(crate) ts_first_fill: Option<Ts>,
    pub(crate) ts_last_fill: Option<Ts>,
    pub(crate) ts_full_fill: Option<Ts>,
    pub(crate) qa_first_fill: Option<Qty>,
    pub(crate) first_reason: FirstFillReason,
    // Per-event scratch.
    pub(crate) ev_removed: Vec<(u64, Qty)>,
    pub(crate) ev_filled: Vec<(u64, Qty)>,
    pub(crate) epoch: u64,
    pub(crate) ev_crossed: Qty,
    /// Ahead orders with an `F` this event.
    pub(crate) ev_ahead_fill: Vec<u64>,
    /// Exact displayed queue ahead (`Exact` rule): sizes of the orders in `ahead_set`, updated by their records.
    pub(crate) exact_qa: Qty,
    pub(crate) ev_exec_through: Qty,
    pub(crate) ev_hidden_at: Qty,
    pub(crate) ev_hidden_through: Qty,
    pub(crate) ev_own_at: Qty,
    /// Flagged-snapshot re-adds of ahead orders at the shadow's level this event (not behind volume).
    pub(crate) ev_add_ahead: Qty,
    /// Opposite orders already taken by taker fills (id, qty).
    pub(crate) taken: Vec<(u64, Qty)>,
    /// Quantity filled at activation (`crossed_at_live`; per-event truth, review m13).
    pub(crate) fill_at_live: Qty,
    /// Last instrument event index at which the book crossed the shadow (one `crossed_unfilled` per cross, m14).
    pub(crate) last_cross: Option<u64>,
    pub(crate) dirty: bool,
    /// The excluded MBO order this shadow replaces (A1 self-exclusion): its records are the shadow's own, its
    /// executions the shadow's fills.
    pub(crate) twin: Option<u64>,
    /// The twin's executions this event: (qty, execution price).
    pub(crate) ev_twin: Vec<(Qty, Px)>,
    /// Level activity of the activation event before a mid-event activation (left out of that event's step).
    pub(crate) lvl_base: Option<LevelAcc>,
    /// Instrument event index of the activation.
    pub(crate) act_ev: u64,
    /// Receipt-order priority bound (`Exact` rule on venues whose order references are assigned in receipt order,
    /// `order_ref_priority`): the largest order reference added on the instrument before the shadow went live. A
    /// later `A` with a smaller reference was received before the shadow and ranks ahead of it (Nasdaq releases
    /// orders accepted before the open but held until it with their original receipt priority).
    pub(crate) prio_ref: Option<u64>,
}

impl Shadow {
    pub(crate) fn new(instrument_id: u32, side: BookSide, price_spec: PriceSpec, tick: Px, qty: Qty) -> Self {
        Self {
            instrument_id,
            side,
            price_spec,
            tick,
            price: match price_spec {
                PriceSpec::Abs(p) => Some(p),
                PriceSpec::Touch { .. } => None,
            },
            qty,
            ts_live: 0,
            cancel_eff: None,
            amends: Vec::new(),
            next_amend: 0,
            status: ShadowStatus::Pending,
            end_state: None,
            ts_end: None,
            ts_activated_event: None,
            qa_live: None,
            rank_live: None,
            level_live: None,
            ahead_set: IdSet::default(),
            ahead: 0,
            traded: 0,
            qa: 0,
            rank: 0,
            level_qty: 0,
            filled: 0,
            n_fills: 0,
            ts_first_fill: None,
            ts_last_fill: None,
            ts_full_fill: None,
            qa_first_fill: None,
            first_reason: FirstFillReason::Never,
            ev_removed: Vec::new(),
            ev_filled: Vec::new(),
            epoch: 0,
            ev_crossed: 0,
            ev_ahead_fill: Vec::new(),
            exact_qa: 0,
            ev_exec_through: 0,
            ev_hidden_at: 0,
            ev_hidden_through: 0,
            ev_own_at: 0,
            ev_add_ahead: 0,
            taken: Vec::new(),
            fill_at_live: 0,
            last_cross: None,
            dirty: false,
            twin: None,
            ev_twin: Vec::new(),
            lvl_base: None,
            act_ev: 0,
            prio_ref: None,
        }
    }

    #[inline]
    pub(crate) fn remaining(&self) -> Qty {
        self.qty.saturating_sub(self.filled)
    }

    #[inline]
    fn queue_ahead_now(&self) -> Qty {
        u128::from(self.ahead).saturating_sub(self.traded) as Qty
    }

    /// Opposite best at or through the price.
    #[inline]
    pub(crate) fn is_crossed(&self, opposite_best: Option<Px>) -> bool {
        match (self.price, opposite_best) {
            (Some(p), Some(o)) => match self.side {
                BookSide::Bid => o <= p,
                BookSide::Ask => o >= p,
            },
            _ => false,
        }
    }

    /// Ends the shadow.
    pub(crate) fn end(&mut self, state: EndState, ts: Ts) {
        self.status = ShadowStatus::Done;
        self.end_state = Some(state);
        self.ts_end = Some(ts);
        self.clear_scratch(true);
    }

    pub(crate) fn clear_scratch(&mut self, all: bool) {
        self.ev_removed.clear();
        if all {
            self.ev_filled.clear();
        }
        self.ev_crossed = 0;
        self.ev_ahead_fill.clear();
        self.ev_exec_through = 0;
        self.ev_hidden_at = 0;
        self.ev_hidden_through = 0;
        self.ev_own_at = 0;
        self.ev_add_ahead = 0;
        self.ev_twin.clear();
        self.lvl_base = None;
        self.dirty = false;
    }

    /// State at activation: `level` is the book level at the resolved price (book as left by the previous event).
    pub(crate) fn activate(&mut self, ts_event: Ts, level: Option<LevelView>, ids: impl Iterator<Item = u64>) {
        self.status = ShadowStatus::Live;
        self.ts_activated_event = Some(ts_event);
        self.ahead_set.clear();
        self.ahead_set.extend(ids);
        let total = level.map_or(0, |l| l.total);
        self.ahead = total;
        self.traded = 0;
        self.qa = total;
        self.exact_qa = total;
        self.rank = self.ahead_set.len() as u32;
        self.level_qty = total;
        self.qa_live = Some(total);
        self.rank_live = Some(self.rank);
        self.level_live = Some(total);
    }

    /// Receipt-order bound of a twin-linked shadow (A1) entering at its twin's `A` on an `order_ref_priority`
    /// instrument: the shadow stands for the twin, received at reference `r`, so `prio_ref = r`, and resting orders
    /// with a larger reference (received after the twin, displayed before it was released from the pre-open hold)
    /// leave its queue ahead. Exact on ITCH data, where a priority-losing replace takes a new reference.
    pub(crate) fn bind_ref(&mut self, r: u64, book: &Book) {
        self.prio_ref = Some(r);
        let later: Vec<u64> = self.ahead_set.iter().copied().filter(|&id| id > r).collect();
        for id in later {
            self.ahead_set.remove(&id);
            let q = book.order(id).map_or(0, |(_, _, o)| o.size);
            self.exact_qa = self.exact_qa.saturating_sub(q);
            self.ahead = self.ahead.saturating_sub(q);
            self.qa = self.qa.saturating_sub(q);
        }
        self.rank = self.ahead_set.len() as u32;
    }

    /// Records one fill and returns its row (without the shadow id, filled by the caller).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn fill(
        &mut self,
        q: Qty,
        reason: FillReason,
        ts: Ts,
        sequence: u64,
        qa_before: Qty,
        traded_ev: Qty,
        crossed_ev: Qty,
        behind_exec: Qty,
    ) -> Option<ShadowFillRow> {
        let px = self.price.unwrap_or_default();
        self.fill_px(
            q,
            reason,
            px,
            ts,
            sequence,
            qa_before,
            traded_ev,
            crossed_ev,
            behind_exec,
        )
    }

    /// `fill` at an explicit price (taker fills walk the opposite book).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn fill_px(
        &mut self,
        q: Qty,
        reason: FillReason,
        px: Px,
        ts: Ts,
        sequence: u64,
        qa_before: Qty,
        traded_ev: Qty,
        crossed_ev: Qty,
        behind_exec: Qty,
    ) -> Option<ShadowFillRow> {
        let q = q.min(self.remaining());
        if q == 0 {
            return None;
        }
        if self.filled == 0 {
            self.first_reason = match reason {
                FillReason::QueueDepleted => FirstFillReason::QueueDepleted,
                FillReason::TradeThrough => FirstFillReason::TradeThrough,
                FillReason::Crossed => FirstFillReason::Crossed,
                FillReason::CrossedAtLive => FirstFillReason::CrossedAtLive,
            };
            self.ts_first_fill = Some(ts);
            self.qa_first_fill = Some(qa_before);
        }
        self.filled += q;
        self.n_fills += 1;
        self.ts_last_fill = Some(ts);
        let row = ShadowFillRow {
            shadow_id: String::new(),
            fill_seq: self.n_fills,
            ts_event: ts,
            sequence,
            fill_qty: q,
            cum_qty: self.filled,
            leaves_qty: self.qty - self.filled,
            fill_px: px,
            reason,
            queue_ahead_before: qa_before,
            traded_at_px_event: traded_ev,
            trade_through_event: crossed_ev,
            behind_volume_executed: behind_exec,
        };
        if self.filled >= self.qty {
            self.ts_full_fill = Some(ts);
            self.end(EndState::Filled, ts);
        }
        Some(row)
    }

    /// The event-end step; fills are appended to `fills`. `by_sequence`: keep unmatched fills for the next records
    /// of the same sequence. `opposite_best` and `level_after` describe the book after the event.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn step_et(
        &mut self,
        ts: Ts,
        sequence: u64,
        by_sequence: bool,
        opposite_best: Option<Px>,
        level_after: Qty,
        lv: Option<&LevelAcc>,
        fills: &mut Vec<ShadowFillRow>,
    ) -> StepOut {
        let qa_before_step = self.qa;
        let pure: Qty = self
            .ev_removed
            .iter()
            .map(|&(id, r)| r.saturating_sub(get(&self.ev_filled, id)))
            .fold(0, Qty::saturating_add);
        self.ahead = self.ahead.saturating_sub(pure);
        let qa_before = self.queue_ahead_now();
        let traded = lv.map_or(0, |l| l.traded);
        self.traded += u128::from(traded);
        let (cancel_behind, behind_exec) = lv.map_or((0, 0), |l| l.behind(&self.ev_removed, &self.ev_ahead_fill, None));
        let (crossed, add_behind) = (self.ev_crossed, lv.map_or(0, |l| l.add));
        let filled_start = self.filled;
        let target = self
            .traded
            .saturating_sub(u128::from(self.ahead))
            .min(u128::from(self.qty)) as Qty;
        if target > self.filled {
            fills.extend(self.fill(
                target - self.filled,
                FillReason::QueueDepleted,
                ts,
                sequence,
                qa_before,
                traded,
                crossed,
                behind_exec,
            ));
        }
        if self.status == ShadowStatus::Live && crossed > 0 && self.remaining() > 0 {
            fills.extend(self.fill(
                crossed,
                FillReason::TradeThrough,
                ts,
                sequence,
                qa_before,
                traded,
                crossed,
                behind_exec,
            ));
        }
        if self.status == ShadowStatus::Live && self.remaining() > 0 && self.is_crossed(opposite_best) {
            fills.extend(self.fill(
                self.remaining(),
                FillReason::Crossed,
                ts,
                sequence,
                qa_before,
                traded,
                crossed,
                behind_exec,
            ));
        }
        let out = StepOut {
            qa_before_step,
            qa_after: self.queue_ahead_now(),
            pure,
            cancel_behind,
            add_behind,
            traded,
            crossed,
            filled_now: self.filled - filled_start,
            ..StepOut::default()
        };
        if by_sequence {
            for &(id, removed) in &self.ev_removed {
                if let Some(f) = self.ev_filled.iter_mut().find(|e| e.0 == id) {
                    f.1 = f.1.saturating_sub(removed);
                }
            }
            self.ev_filled.retain(|e| e.1 > 0);
            self.clear_scratch(false);
        } else {
            self.clear_scratch(true);
        }
        self.qa = out.qa_after;
        self.rank = self.ahead_set.len() as u32;
        self.level_qty = level_after;
        out
    }

    /// Fills against the opposite displayed orders at or through the price (A5 as refined by E48, E36(a)),
    /// best level first and FIFO within a level. The first `prio` units of that sequence go to the real same-side
    /// orders with priority over the shadow (better price, or its price and ahead of it), which match first
    /// (conservative); from each order the shadow then takes what is left beyond that allocation, net of what it
    /// already took from it. `crossed_at_live` (the shadow arrives into a crossing book) is a taker walk: one fill
    /// row per level at the level's price. `crossed` (a resting shadow crossed by an opposite arrival) is a maker
    /// fill: the same quantity in one row at the shadow's own price. Returns the number of rows.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn taker_walk(
        &mut self,
        book: &Book,
        prio: Qty,
        reason: FillReason,
        ts: Ts,
        sequence: u64,
        qa_before: Qty,
        traded_ev: Qty,
        through_ev: Qty,
        behind_exec: Qty,
        fills: &mut Vec<ShadowFillRow>,
    ) -> u64 {
        let Some(p) = self.price else {
            return 0;
        };
        let opp = self.side.opposite();
        let mut skip = prio;
        let mut rows = 0;
        let mut plan: Vec<(Px, Qty)> = Vec::new();
        let mut want = self.remaining();
        'levels: for lv in book.levels(opp) {
            let reach = match self.side {
                BookSide::Bid => lv.price <= p,
                BookSide::Ask => lv.price >= p,
            };
            if !reach || want == 0 {
                break;
            }
            let mut at_level: Qty = 0;
            for o in book.orders_at(opp, lv.price) {
                let sk = skip.min(o.size);
                skip -= sk;
                let avail = (o.size - sk).saturating_sub(get(&self.taken, o.order_id));
                let q = avail.min(want);
                if q > 0 {
                    bump(&mut self.taken, o.order_id, q);
                    at_level += q;
                    want -= q;
                }
                if want == 0 {
                    if at_level > 0 {
                        plan.push((lv.price, at_level));
                    }
                    break 'levels;
                }
            }
            if at_level > 0 {
                plan.push((lv.price, at_level));
            }
        }
        if reason == FillReason::Crossed {
            // Maker at its resting price.
            let q: Qty = plan.iter().map(|x| x.1).sum();
            plan = if q > 0 { vec![(p, q)] } else { Vec::new() };
        }
        for (px, q) in plan {
            if let Some(r) = self.fill_px(
                q,
                reason,
                px,
                ts,
                sequence,
                qa_before,
                traded_ev,
                through_ev,
                behind_exec,
            ) {
                fills.push(r);
                rows += 1;
            }
        }
        rows
    }

    /// Displayed volume of the real same-side orders with priority over the shadow in a locked or crossed book
    /// (E36(a)): every level strictly better than its price plus the exact queue ahead at its price.
    pub(crate) fn priority_volume(&self, book: &Book) -> Qty {
        let Some(p) = self.price else {
            return 0;
        };
        let better = book
            .levels(self.side)
            .take_while(|l| match self.side {
                BookSide::Bid => l.price > p,
                BookSide::Ask => l.price < p,
            })
            .fold(0, |a: Qty, l| a.saturating_add(l.total));
        better.saturating_add(self.exact_qa)
    }

    /// The `Exact` event-end step (audit rulings A3-A5). Queue ahead is `exact_qa` (already reduced by the records of
    /// the orders ahead). Fills, in order: the executions of the shadow's own twin (A1 self-exclusion: the twin is
    /// the live order the shadow replaces, so each of its executions is a `queue_depleted` fill at the execution's
    /// price, whatever the computed queue ahead and whatever its displayed price); executions behind the shadow at
    /// its price plus, once the displayed queue ahead is exhausted, hidden prints at the price (`queue_depleted`);
    /// executions of orders resting strictly through the price and hidden prints through it (`trade_through`); a
    /// cross walks the opposite book as taker after the same-side volume with priority (`crossed`).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn step_exact(
        &mut self,
        ts: Ts,
        sequence: u64,
        by_sequence: bool,
        book: &Book,
        level_after: Qty,
        lv: Option<&LevelAcc>,
        fills: &mut Vec<ShadowFillRow>,
    ) -> StepOut {
        let qa_before_step = self.qa;
        let pure: Qty = self
            .ev_removed
            .iter()
            .map(|&(id, r)| r.saturating_sub(get(&self.ev_filled, id)))
            .fold(0, Qty::saturating_add);
        let qa_after = self.exact_qa;
        let since = match (&self.lvl_base, lv) {
            (Some(b), Some(l)) => Some(l.since(b)),
            _ => None,
        };
        let lv = since.as_ref().or(lv);
        let twin = self.twin;
        let (cancel_behind, behind_exec) = lv.map_or((0, 0), |l| l.behind(&self.ev_removed, &self.ev_ahead_fill, twin));
        let behind_off = lv.map_or(0, |l| l.behind_off_price(&self.ev_ahead_fill, twin));
        let (traded, add_behind) = lv.map_or((0, 0), |l| (l.traded, l.add.saturating_sub(self.ev_add_ahead)));
        let through = self.ev_exec_through.saturating_add(self.ev_hidden_through);
        let filled_start = self.filled;
        let mut out = StepOut::default();
        for (q, px) in std::mem::take(&mut self.ev_twin) {
            if Some(px) != self.price {
                out.twin_off_display += 1;
            } else if qa_after > 0 {
                out.twin_with_ahead += 1;
            }
            if self.status == ShadowStatus::Live {
                fills.extend(self.fill_px(
                    q,
                    FillReason::QueueDepleted,
                    px,
                    ts,
                    sequence,
                    qa_before_step,
                    traded,
                    through,
                    behind_exec,
                ));
            }
        }
        let mut v = behind_exec.saturating_sub(behind_off);
        out.behind_with_ahead = v > 0 && qa_after > 0;
        let mut hidden_used = false;
        if self.ev_hidden_at > 0 {
            if qa_after == 0 {
                v = v.saturating_add(self.ev_hidden_at);
                hidden_used = true;
            } else {
                out.hidden_blocked = true;
            }
        }
        if qa_after == 0 {
            v = v.saturating_add(self.ev_own_at);
        }
        if v > 0 {
            let before = self.filled;
            fills.extend(self.fill(
                v,
                FillReason::QueueDepleted,
                ts,
                sequence,
                qa_before_step,
                traded,
                through,
                behind_exec,
            ));
            out.hidden_fill = hidden_used && self.filled > before;
        }
        if self.status == ShadowStatus::Live && through > 0 && self.remaining() > 0 {
            fills.extend(self.fill(
                through,
                FillReason::TradeThrough,
                ts,
                sequence,
                qa_before_step,
                traded,
                through,
                behind_exec,
            ));
        }
        if self.status == ShadowStatus::Live
            && self.remaining() > 0
            && self.is_crossed(book.best_price(self.side.opposite()))
        {
            let prio = self.priority_volume(book);
            let rows = self.taker_walk(
                book,
                prio,
                FillReason::Crossed,
                ts,
                sequence,
                qa_before_step,
                traded,
                through,
                behind_exec,
                fills,
            );
            out.taker_fills = rows;
            out.crossed_unfilled = rows == 0;
            out.crossed_book = true;
        }
        out.qa_before_step = qa_before_step;
        out.qa_after = qa_after;
        out.pure = pure;
        out.cancel_behind = cancel_behind;
        out.add_behind = add_behind;
        out.traded = traded;
        out.crossed = through;
        out.filled_now = self.filled - filled_start;
        if by_sequence {
            for &(id, removed) in &self.ev_removed {
                if let Some(f) = self.ev_filled.iter_mut().find(|e| e.0 == id) {
                    f.1 = f.1.saturating_sub(removed);
                }
            }
            self.ev_filled.retain(|e| e.1 > 0);
            self.clear_scratch(false);
        } else {
            self.clear_scratch(true);
        }
        self.qa = qa_after;
        self.rank = self.ahead_set.len() as u32;
        self.level_qty = level_after;
        out
    }
}
