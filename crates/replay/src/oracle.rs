//! Queue-rank oracle (SPEC 5.1.4, et `RankOracle`): on every `F` the filled order's rank (orders ahead at its price
//! not consumed earlier in the same event), `level_depth` (better levels on its side still holding an unconsumed
//! order) and price-priority violation (`level_depth > 0`). Fills at one (side, resting price) within `sweep_gap_ns` of
//! the previous fill there form a sweep; a sweep closes on the gap (checked on the next record of the instrument), on
//! an add/cancel/modify of a non-member at that level, on `R`, or at end of input. `rank_sweep_adjusted` ignores
//! orders ahead executed in the same sweep. Rows are emitted when their sweep closes. 30-minute buckets are keyed
//! `HH:MM` after the instrument's local midnight; `first_60s_after_open` starts at `open_s`.

use std::collections::BTreeMap;

use indexmap::IndexMap;
use qt_book::{Effect, Rank};
use qt_schema::json::{RankBucket, RankOracle, RankWindow};
use qt_schema::rows::{ExecKind, RankTraceRow};
use qt_types::{BookSide, Dur, MboEvent, Px, QtResult, Ts};

use crate::Sink;

const BUCKET_NS: i64 = 1_800_000_000_000;
const WINDOW_NS: i64 = 60_000_000_000;

/// Where a fill falls in local time.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Clock {
    pub(crate) midnight: Ts,
    pub(crate) open_ns: i64,
}

impl Clock {
    fn locate(self, ts: Ts) -> (i64, bool) {
        let since = ts.saturating_sub(self.midnight).max(0);
        let open = since >= self.open_ns && since < self.open_ns.saturating_add(WINDOW_NS);
        (since / BUCKET_NS, open)
    }
}

#[derive(Clone, Copy, Debug, Default)]
struct Bucket {
    checked: u64,
    rank0: u64,
    rank0_adj: u64,
}

/// Run-wide counters.
#[derive(Clone, Debug, Default)]
pub(crate) struct Oracle {
    gap: Dur,
    checked: u64,
    unknown: u64,
    price_violations: u64,
    hist: [u64; 6],
    hist_adj: [u64; 6],
    sweeps: u64,
    sweeps_multi: u64,
    w_checked: u64,
    w_viol: u64,
    w_viol_adj: u64,
    buckets: BTreeMap<i64, Bucket>,
    next_sweep_id: u64,
}

struct PendingFill {
    ts_event: Ts,
    sequence: u64,
    order_id: u64,
    rank: u32,
    level_depth: u32,
    exec_kind: ExecKind,
    ahead: Vec<u64>,
    bucket: i64,
    open: bool,
}

/// Fills at one level chained within the sweep gap.
pub(crate) struct Sweep {
    id: u64,
    side: BookSide,
    price: Px,
    last_ts: Ts,
    members: Vec<u64>,
    fills: Vec<PendingFill>,
}

fn bin(r: u32) -> usize {
    (r as usize).min(5)
}

fn share(num: u64, den: u64) -> Option<f64> {
    (den > 0).then(|| num as f64 / den as f64)
}

impl Oracle {
    pub(crate) fn new(gap: Dur) -> Self {
        Self { gap, ..Self::default() }
    }

    fn close(&mut self, s: Sweep, instrument_id: u32, sink: &mut dyn Sink) -> QtResult<()> {
        self.sweeps += 1;
        self.sweeps_multi += u64::from(s.fills.len() > 1);
        for f in s.fills {
            let adj = f.ahead.iter().filter(|id| !s.members.contains(id)).count() as u32;
            self.hist_adj[bin(adj)] += 1;
            self.buckets.entry(f.bucket).or_default().rank0_adj += u64::from(adj == 0);
            if f.open {
                self.w_viol_adj += u64::from(adj > 0);
            }
            sink.rank_trace(&RankTraceRow {
                ts_event: f.ts_event,
                instrument_id,
                sequence: f.sequence,
                order_id: f.order_id,
                side: s.side,
                price: s.price,
                rank: f.rank,
                rank_sweep_adjusted: adj,
                level_depth: f.level_depth,
                sweep_id: s.id,
                exec_kind: f.exec_kind,
            })?;
        }
        Ok(())
    }

    /// Closes the sweeps whose gap elapsed before `ts` (in their list order).
    pub(crate) fn expire(
        &mut self,
        sweeps: &mut Vec<Sweep>,
        ts: Ts,
        instrument_id: u32,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        let mut i = 0;
        while i < sweeps.len() {
            if ts.saturating_sub(sweeps[i].last_ts) > self.gap {
                let s = sweeps.swap_remove(i);
                self.close(s, instrument_id, sink)?;
            } else {
                i += 1;
            }
        }
        Ok(())
    }

    /// An `F`: raw rank now, sweep membership buffered. `rank` is `None` for an unknown order.
    pub(crate) fn fill(
        &mut self,
        sweeps: &mut Vec<Sweep>,
        e: &MboEvent,
        rank: Option<Rank>,
        ahead: &[u64],
        clock: Clock,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        let Some(r) = rank else {
            self.unknown += 1;
            return Ok(());
        };
        self.checked += 1;
        self.hist[bin(r.rank)] += 1;
        if r.level_depth > 0 {
            self.price_violations += 1;
        }
        let (bucket, open) = clock.locate(e.ts_event);
        let b = self.buckets.entry(bucket).or_default();
        b.checked += 1;
        b.rank0 += u64::from(r.rank == 0);
        if open {
            self.w_checked += 1;
            self.w_viol += u64::from(r.rank > 0);
        }
        let pos = sweeps.iter().position(|s| s.side == r.side && s.price == r.price);
        let pos = match pos {
            Some(p) if e.ts_event.saturating_sub(sweeps[p].last_ts) <= self.gap => p,
            Some(p) => {
                let s = sweeps.swap_remove(p);
                self.close(s, e.instrument_id, sink)?;
                sweeps.len()
            }
            None => sweeps.len(),
        };
        if pos == sweeps.len() {
            self.next_sweep_id += 1;
            sweeps.push(Sweep {
                id: self.next_sweep_id,
                side: r.side,
                price: r.price,
                last_ts: e.ts_event,
                members: Vec::new(),
                fills: Vec::new(),
            });
        }
        let s = &mut sweeps[pos];
        s.last_ts = e.ts_event;
        if !s.members.contains(&e.order_id) {
            s.members.push(e.order_id);
        }
        s.fills.push(PendingFill {
            ts_event: e.ts_event,
            sequence: e.sequence,
            order_id: e.order_id,
            rank: r.rank,
            level_depth: r.level_depth,
            exec_kind: if e.price == r.price {
                ExecKind::AtPrice
            } else {
                ExecKind::OffPrice
            },
            ahead: if r.rank > 0 { ahead.to_vec() } else { Vec::new() },
            bucket,
            open,
        });
        Ok(())
    }

    /// A book change at a sweep's level by a non-member closes that sweep; `R` closes all.
    pub(crate) fn touch(
        &mut self,
        sweeps: &mut Vec<Sweep>,
        effect: &Effect,
        instrument_id: u32,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        if sweeps.is_empty() {
            return Ok(());
        }
        let mut levels = [None, None];
        let order_id = match *effect {
            Effect::None => return Ok(()),
            Effect::Clear => {
                for s in std::mem::take(sweeps) {
                    self.close(s, instrument_id, sink)?;
                }
                return Ok(());
            }
            Effect::Add {
                order_id, side, price, ..
            }
            | Effect::Remove {
                order_id, side, price, ..
            }
            | Effect::Reduce {
                order_id, side, price, ..
            } => {
                levels[0] = Some((side, price));
                order_id
            }
            Effect::Replace {
                order_id,
                side,
                old_price,
                new_price,
                ..
            } => {
                levels = [Some((side, old_price)), Some((side, new_price))];
                order_id
            }
        };
        for (side, price) in levels.into_iter().flatten() {
            let hit = sweeps
                .iter()
                .position(|s| s.side == side && s.price == price && !s.members.contains(&order_id));
            if let Some(p) = hit {
                let s = sweeps.swap_remove(p);
                self.close(s, instrument_id, sink)?;
            }
        }
        Ok(())
    }

    /// Closes every sweep of an instrument (end of input).
    pub(crate) fn flush(&mut self, sweeps: &mut Vec<Sweep>, instrument_id: u32, sink: &mut dyn Sink) -> QtResult<()> {
        for s in std::mem::take(sweeps) {
            self.close(s, instrument_id, sink)?;
        }
        Ok(())
    }

    /// The summary document.
    pub(crate) fn summary(&self) -> RankOracle {
        let mut by_bucket = IndexMap::new();
        for (b, v) in &self.buckets {
            let mins = b * 30;
            by_bucket.insert(
                format!("{:02}:{:02}", mins / 60, mins % 60),
                RankBucket {
                    checked: v.checked,
                    rank0: v.rank0,
                    rank0_sweep_adjusted: v.rank0_adj,
                    rank0_share: share(v.rank0, v.checked),
                    rank0_share_sweep_adjusted: share(v.rank0_adj, v.checked),
                },
            );
        }
        RankOracle {
            executions_checked: self.checked,
            executions_unknown_order: self.unknown,
            price_priority_violations: self.price_violations,
            fifo_rank_histogram: self.hist,
            fifo_rank_histogram_sweep_adjusted: self.hist_adj,
            rank0_share: share(self.hist[0], self.checked),
            rank0_share_sweep_adjusted: share(self.hist_adj[0], self.checked),
            sweeps: self.sweeps,
            sweeps_multi_fill: self.sweeps_multi,
            sweep_gap_ns: self.gap,
            first_60s_after_open: RankWindow {
                checked: self.w_checked,
                rank_violations: self.w_viol,
                rank_violations_sweep_adjusted: self.w_viol_adj,
                rank0_share: share(self.w_checked - self.w_viol, self.w_checked),
                rank0_share_sweep_adjusted: share(self.w_checked - self.w_viol_adj, self.w_checked),
            },
            rank0_share_by_bucket: by_bucket,
        }
    }
}

/// Merges per-partition oracle summaries (counts added, shares recomputed, buckets merged in key order).
pub(crate) fn merge(parts: &[RankOracle]) -> Option<RankOracle> {
    let first = parts.first()?;
    let mut m = RankOracle {
        sweep_gap_ns: first.sweep_gap_ns,
        ..RankOracle::default()
    };
    let mut buckets: BTreeMap<String, RankBucket> = BTreeMap::new();
    for p in parts {
        m.executions_checked += p.executions_checked;
        m.executions_unknown_order += p.executions_unknown_order;
        m.price_priority_violations += p.price_priority_violations;
        for i in 0..6 {
            m.fifo_rank_histogram[i] += p.fifo_rank_histogram[i];
            m.fifo_rank_histogram_sweep_adjusted[i] += p.fifo_rank_histogram_sweep_adjusted[i];
        }
        m.sweeps += p.sweeps;
        m.sweeps_multi_fill += p.sweeps_multi_fill;
        let w = &mut m.first_60s_after_open;
        w.checked += p.first_60s_after_open.checked;
        w.rank_violations += p.first_60s_after_open.rank_violations;
        w.rank_violations_sweep_adjusted += p.first_60s_after_open.rank_violations_sweep_adjusted;
        for (k, v) in &p.rank0_share_by_bucket {
            let b = buckets.entry(k.clone()).or_default();
            b.checked += v.checked;
            b.rank0 += v.rank0;
            b.rank0_sweep_adjusted += v.rank0_sweep_adjusted;
        }
    }
    m.rank0_share = share(m.fifo_rank_histogram[0], m.executions_checked);
    m.rank0_share_sweep_adjusted = share(m.fifo_rank_histogram_sweep_adjusted[0], m.executions_checked);
    let w = &mut m.first_60s_after_open;
    w.rank0_share = share(w.checked - w.rank_violations, w.checked);
    w.rank0_share_sweep_adjusted = share(w.checked - w.rank_violations_sweep_adjusted, w.checked);
    for (k, mut v) in buckets {
        v.rank0_share = share(v.rank0, v.checked);
        v.rank0_share_sweep_adjusted = share(v.rank0_sweep_adjusted, v.checked);
        m.rank0_share_by_bucket.insert(k, v);
    }
    Some(m)
}
