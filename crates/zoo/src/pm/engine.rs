//! The prediction-market queue engine: corrected-mode queue models over one instrument's `qt.l2` rows, every model
//! at once, in one pass over the batches. `queuetruth.pm.queue.Engine.run` calls `run` through `_core`; its module
//! docstring states the rules (activation, rows in batch order, the taker cap, the families, day-boundary
//! restatements and feed gaps).
//!
//! The arithmetic follows the earlier NumPy engine operation by operation: `maximum`/`minimum` propagate NaN as
//! NumPy's do, the probability families take `ln_1p` where NumPy took `log1p`, and positions are found with NumPy's
//! `searchsorted` probe order (sequences need not be monotone). Per-order updates are independent of each other, so
//! the order in which a level's orders are visited does not change any result.

// The per-model state is parallel arrays indexed by model, as the NumPy engine's vectors were.
#![allow(clippy::needless_range_loop)]

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap, HashMap};

use qt_types::{QtError, QtResult};

use super::families::PmFamily;
use crate::prob;

/// Relative tolerance under which leaves count as filled (`EPS * lot`).
pub const EPS: f64 = 1e-9;
/// Side codes of the canonical YES ladder.
pub const BID: i64 = b'B' as i64;
/// Ask side code.
pub const ASK: i64 = b'A' as i64;
/// Row kinds (`queuetruth.pm.dataset.KINDS`).
pub const K_DEPTH: i64 = 0;
/// Trade row.
pub const K_TRADE: i64 = 1;
/// Snapshot row.
pub const K_SNAPSHOT: i64 = 2;
/// Clear row.
pub const K_CLEAR: i64 = 3;

/// One model: family, parameter `n` (power families) and the one-order-per-change variant.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Model {
    /// Family.
    pub family: PmFamily,
    /// Parameter `n` of the power families.
    pub n: Option<f64>,
    /// One order per visible change.
    pub atomic: bool,
}

impl Model {
    /// Validates a model as `queuetruth.pm.queue.Model` does (`QT-CONFIG-001`).
    pub fn new(family: &str, n: Option<f64>, atomic: bool) -> QtResult<Model> {
        let f = PmFamily::from_name(family)
            .ok_or_else(|| QtError::Config(format!("model family {family:?}: not a prediction-market family")))?;
        let d = f.def();
        let needs_n = !d.params.is_empty();
        if needs_n && !n.is_some_and(|x| x.is_finite() && x > 0.0) {
            return Err(QtError::Config(format!(
                "model {family} needs a positive finite parameter n"
            )));
        }
        if !needs_n && n.is_some() {
            return Err(QtError::Config(format!("model {family} takes no parameter")));
        }
        if atomic && !d.atomic {
            return Err(QtError::Config(format!("model {family}: does not take `atomic`")));
        }
        Ok(Model { family: f, n, atomic })
    }
}

/// One instrument's `qt.l2` rows as columns, in file order.
#[derive(Clone, Debug, Default)]
pub struct Rows {
    /// Exchange time.
    pub ts_event: Vec<i64>,
    /// Local receive time.
    pub ts_recv: Vec<i64>,
    /// Batch id.
    pub event_id: Vec<i64>,
    /// Published sequence.
    pub sequence: Vec<i64>,
    /// Kind code (`K_*`).
    pub kind: Vec<i64>,
    /// Side code.
    pub side: Vec<i64>,
    /// Price (-1 for null).
    pub price: Vec<i64>,
    /// New level size.
    pub new_qty: Vec<i64>,
    /// Trade size.
    pub trade_qty: Vec<i64>,
    /// Flags (bit 0: last in batch).
    pub flags: Vec<i64>,
    /// Partition ordinal.
    pub part: Vec<i64>,
}

impl Rows {
    /// Number of rows.
    pub fn len(&self) -> usize {
        self.ts_event.len()
    }

    /// Whether there are no rows.
    pub fn is_empty(&self) -> bool {
        self.ts_event.is_empty()
    }

    fn check(&self) -> QtResult<()> {
        let n = self.len();
        let lens = [
            self.ts_recv.len(),
            self.event_id.len(),
            self.sequence.len(),
            self.kind.len(),
            self.side.len(),
            self.price.len(),
            self.new_qty.len(),
            self.trade_qty.len(),
            self.flags.len(),
            self.part.len(),
        ];
        if lens.iter().any(|&l| l != n) {
            return Err(QtError::Config(format!(
                "pm engine: columns of unequal length ({n} vs {lens:?})"
            )));
        }
        Ok(())
    }
}

/// One order to simulate (`queuetruth.pm.queue.SimOrder`).
#[derive(Clone, Debug, Default)]
pub struct SimOrder {
    /// Order key.
    pub key: String,
    /// Side code.
    pub side: i64,
    /// Price.
    pub price: i64,
    /// Size.
    pub qty: f64,
    /// Entry by time.
    pub ts_live: Option<i64>,
    /// Entry by position.
    pub entry_seq: Option<i64>,
    /// Joined during batch `entry_seq` (else just before it).
    pub inside: bool,
    /// End time.
    pub ts_end: Option<i64>,
    /// End by position.
    pub end_seq: Option<i64>,
    /// Left during batch `end_seq` (else just before it).
    pub end_inside: bool,
    /// Rejected instead of taking on arrival into a crossing book.
    pub post_only: bool,
    /// Priority-keeping reductions `(ts, new total qty)`.
    pub amends: Vec<(i64, f64)>,
    /// Local times at which to record the queue estimate (sorted).
    pub samples: Vec<i64>,
    /// Published batch seqs: record the estimate just before them.
    pub probes: Vec<i64>,
}

/// One fill: `(ts, qty, reason, liquidity, price)`.
pub type Fill = (i64, f64, &'static str, &'static str, f64);

/// Levels and per-model taken sizes a taker fill walks (`walk`).
type WalkPx<'a> = Option<(&'a [(i64, i64)], &'a [f64])>;

/// Per order: fills per model, end state, activation and recorded estimates.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct OrderResult {
    /// Batch time at which it went live.
    pub activated_ts: Option<i64>,
    /// Sequence of that batch.
    pub activated_seq: Option<i64>,
    /// Visible size at its level when it went live.
    pub visible_at_live: Option<f64>,
    /// End state.
    pub end: &'static str,
    /// End time.
    pub end_ts: Option<i64>,
    /// Fills per model.
    pub fills: Vec<Vec<Fill>>,
    /// `(local time, F per model)`.
    pub samples: Vec<(i64, Vec<f64>)>,
    /// `(seq, F per model before that batch)`.
    pub probes: Vec<(i64, Vec<f64>)>,
    /// Samples before it went live.
    pub samples_before_live: usize,
    /// Samples after it ended.
    pub samples_after_end: usize,
}

/// NumPy `maximum`: NaN when either is NaN, else the larger (the first on a tie).
fn nmax(a: f64, b: f64) -> f64 {
    if a >= b || a.is_nan() { a } else { b }
}

/// NumPy `minimum`.
fn nmin(a: f64, b: f64) -> f64 {
    if a <= b || a.is_nan() { a } else { b }
}

/// Python's builtin `max(a, b)` on floats: `b` only when `b > a`.
fn pymax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Python's builtin `min(a, b)` on floats: `b` only when `b < a`.
fn pymin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// NumPy's `searchsorted` of one key (its probe order, which matters when `arr` is not sorted).
fn searchsorted(arr: &[i64], key: i64, right: bool) -> usize {
    let (mut lo, mut hi) = (0usize, arr.len());
    while lo < hi {
        let mid = lo + ((hi - lo) >> 1);
        let v = arr[mid];
        if if right { v <= key } else { v < key } {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    lo
}

/// Aggregated sizes per price on both sides (sizes > 0 only).
#[derive(Default)]
struct Ladder {
    bid: BTreeMap<i64, i64>,
    ask: BTreeMap<i64, i64>,
}

impl Ladder {
    fn side(&self, side: i64) -> Option<&BTreeMap<i64, i64>> {
        match side {
            BID => Some(&self.bid),
            ASK => Some(&self.ask),
            _ => None,
        }
    }

    fn qty(&self, side: i64, price: i64) -> i64 {
        self.side(side).and_then(|b| b.get(&price).copied()).unwrap_or(0)
    }

    fn set(&mut self, side: i64, price: i64, qty: i64) -> QtResult<i64> {
        if qty < 0 {
            let c = u8::try_from(side).map_or('?', char::from);
            return Err(QtError::Engine(format!("negative level size {qty} at {c} {price}")));
        }
        let book = match side {
            BID => &mut self.bid,
            ASK => &mut self.ask,
            _ => return Err(QtError::Engine(format!("level side {side} is neither B nor A"))),
        };
        let prev = book.get(&price).copied().unwrap_or(0);
        if qty == 0 {
            book.remove(&price);
        } else {
            book.insert(price, qty);
        }
        Ok(prev)
    }

    fn best(&self, side: i64) -> Option<i64> {
        match side {
            BID => self.bid.keys().next_back().copied(),
            ASK => self.ask.keys().next().copied(),
            _ => None,
        }
    }

    fn clear(&mut self) {
        self.bid.clear();
        self.ask.clear();
    }

    fn len(&self) -> usize {
        self.bid.len() + self.ask.len()
    }

    /// Visible opposite levels at prices at or through `price`, best first.
    fn opp_through(&self, side: i64, price: i64) -> Vec<(i64, i64)> {
        if side == BID {
            self.ask.range(..=price).map(|(p, q)| (*p, *q)).collect()
        } else {
            self.bid.range(price..).rev().map(|(p, q)| (*p, *q)).collect()
        }
    }
}

/// Volume-weighted price of `qty` units taken from `levels` (best first) after the first `skip` units.
fn walk(levels: &[(i64, i64)], mut skip: f64, qty: f64) -> f64 {
    let mut cost = 0.0;
    let mut left = qty;
    for &(p, q) in levels {
        let mut avail = q as f64;
        if skip >= avail {
            skip -= avail;
            continue;
        }
        avail -= skip;
        skip = 0.0;
        let take = pymin(avail, left);
        cost += take * p as f64;
        left -= take;
        if left <= 0.0 {
            break;
        }
    }
    if qty > 0.0 { cost / qty } else { f64::NAN }
}

/// Probability that a decrease is behind the order (NaN or infinite values are replaced by the caller).
fn pi_behind(m: &Model, front: f64, back: f64) -> f64 {
    let n = m.n.unwrap_or(f64::NAN);
    match m.family {
        PmFamily::Power => prob::power(n, front, back),
        PmFamily::Power2 => prob::power2(n, front, back),
        PmFamily::Power3 => prob::power3(n, front, back),
        PmFamily::Log => back.ln_1p() / (back.ln_1p() + front.ln_1p()),
        PmFamily::Log2 => back.ln_1p() / (back + front).ln_1p(),
        _ => 0.0,
    }
}

struct Live {
    f: Vec<f64>,
    t: Vec<f64>,
    leaves: Vec<f64>,
    filled: Vec<f64>,
    done: Vec<bool>,
    taker_taken: Vec<f64>,
    si: usize,
    pi: usize,
}

struct Engine<'a> {
    models: &'a [Model],
    lot: f64,
    is_foq: Vec<bool>,
    is_tt: Vec<bool>,
    is_ra: Vec<bool>,
    is_prob: Vec<bool>,
}

impl Engine<'_> {
    fn k(&self) -> usize {
        self.models.len()
    }

    fn depth(&self, s: &mut Live, prev: f64, new: f64) {
        let base = prev - new;
        for m in 0..self.k() {
            if s.done[m] {
                continue;
            }
            if self.is_ra[m] {
                s.f[m] = nmin(nmax(s.f[m], 0.0), new);
            } else if self.is_prob[m] {
                let chg = base - s.t[m];
                s.t[m] = 0.0;
                if chg < 0.0 {
                    s.f[m] = nmin(nmax(s.f[m], 0.0), new);
                } else {
                    let front = nmax(s.f[m], 0.0);
                    if front == 0.0 {
                        s.f[m] = nmin(front, new);
                    } else {
                        let back = nmax(prev - front, 0.0);
                        let model = &self.models[m];
                        let mut pi = pi_behind(model, front, back);
                        if !pi.is_finite() {
                            pi = back / (front + back);
                        }
                        pi = if back == 0.0 { 0.0 } else { nmin(nmax(pi, 0.0), 1.0) };
                        if model.atomic {
                            let (ahead, behind) = (chg <= front, chg <= back);
                            if ahead && !behind {
                                pi = 0.0;
                            }
                            if behind && !ahead {
                                pi = 1.0;
                            }
                        }
                        let est = front - (1.0 - pi) * chg + nmin(back - pi * chg, 0.0);
                        s.f[m] = nmin(nmax(est, 0.0), new);
                    }
                }
            } else if self.is_tt[m] {
                s.f[m] = nmin(s.f[m], new);
            }
        }
    }

    /// Fills `q` (per model, capped at the leaves) at the order's price, or at the walked price (taker).
    #[allow(clippy::too_many_arguments)]
    fn fill(
        &self,
        s: &mut Live,
        res: &mut OrderResult,
        price: i64,
        ts: i64,
        q: &[f64],
        reason: &'static str,
        liquidity: &'static str,
        walk_px: WalkPx<'_>,
    ) {
        for m in 0..self.k() {
            let qm = nmin(if s.done[m] { 0.0 } else { q[m] }, s.leaves[m]);
            if qm <= 0.0 || qm.is_nan() {
                continue;
            }
            let px = match walk_px {
                None => price as f64,
                Some((levels, taken)) => walk(levels, taken[m], qm),
            };
            s.leaves[m] -= qm;
            s.filled[m] += qm;
            res.fills[m].push((ts, qm, reason, liquidity, px));
            if s.leaves[m] <= EPS * self.lot {
                s.leaves[m] = pymax(s.leaves[m], 0.0);
                s.done[m] = true;
            }
        }
    }

    fn trade(&self, s: &mut Live, res: &mut OrderResult, price: i64, ts: i64, v: f64) {
        let mut q = vec![0.0; self.k()];
        for m in 0..self.k() {
            if s.done[m] {
                continue;
            }
            if self.is_foq[m] {
                q[m] = (v / self.lot).floor() * self.lot;
            } else if self.is_ra[m] || self.is_prob[m] {
                s.f[m] -= v;
                if self.is_prob[m] {
                    s.t[m] += v;
                }
                let lots = (nmax(-s.f[m], 0.0) / self.lot + 1e-9).floor();
                if lots > 0.0 {
                    s.f[m] = 0.0;
                    q[m] = lots * self.lot;
                }
            }
        }
        self.fill(s, res, price, ts, &q, "queue", "maker", None);
    }

    fn through(&self, s: &mut Live, res: &mut OrderResult, price: i64, ts: i64) {
        let q: Vec<f64> = (0..self.k())
            .map(|m| if s.done[m] { 0.0 } else { s.leaves[m] })
            .collect();
        self.fill(s, res, price, ts, &q, "trade_through", "maker", None);
    }

    /// Taker cap of the corrected mode (see `queuetruth.pm.queue.Engine._cross`).
    #[allow(clippy::too_many_arguments)]
    fn cross(
        &self,
        s: &mut Live,
        res: &mut OrderResult,
        price: i64,
        ts: i64,
        levels: &[(i64, i64)],
        reason: &'static str,
        liquidity: &'static str,
    ) {
        let able: Vec<bool> = (0..self.k()).map(|m| !s.done[m] && !self.is_tt[m]).collect();
        if !able.iter().any(|&a| a) {
            return;
        }
        let opp = levels.iter().map(|&(_, q)| q).sum::<i64>() as f64;
        let cap = pymax(opp, 0.0);
        for t in &mut s.taker_taken {
            *t = nmin(*t, cap);
        }
        let avail: Vec<f64> = s.taker_taken.iter().map(|&t| nmax(opp - t, 0.0)).collect();
        let q: Vec<f64> = (0..self.k())
            .map(|m| {
                if able[m] && avail[m] > 0.0 {
                    nmin(s.leaves[m], avail[m])
                } else {
                    0.0
                }
            })
            .collect();
        let before = s.filled.clone();
        let taken = s.taker_taken.clone();
        let walk_px = if liquidity == "taker" {
            Some((levels, taken.as_slice()))
        } else {
            None
        };
        self.fill(s, res, price, ts, &q, reason, liquidity, walk_px);
        for m in 0..self.k() {
            s.taker_taken[m] += nmin(nmax(s.filled[m] - before[m], 0.0), avail[m]);
        }
    }
}

fn flush_probes(s: &mut Live, o: &SimOrder, res: &mut OrderResult, seq: i64) {
    while s.pi < o.probes.len() && o.probes[s.pi] <= seq {
        res.probes.push((o.probes[s.pi], s.f.clone()));
        s.pi += 1;
    }
}

fn flush_samples(s: &mut Live, o: &SimOrder, res: &mut OrderResult, until_recv: Option<i64>) {
    while s.si < o.samples.len() && until_recv.is_none_or(|u| o.samples[s.si] < u) {
        res.samples.push((o.samples[s.si], s.f.clone()));
        s.si += 1;
    }
}

/// Row index where each batch starts (a new `event_id` or partition, or after a `LAST_IN_BATCH` row).
pub fn batch_starts(rows: &Rows) -> Vec<usize> {
    let mut starts = vec![0];
    for r in 1..rows.len() {
        if rows.part[r] != rows.part[r - 1] || rows.event_id[r] != rows.event_id[r - 1] || rows.flags[r - 1] & 1 != 0 {
            starts.push(r);
        }
    }
    starts
}

/// The live orders and their level index.
struct Book {
    live: BTreeMap<usize, Live>,
    by_level: HashMap<(i64, i64), BTreeSet<usize>>,
}

impl Book {
    fn stop(&mut self, i: usize, why: &'static str, ts: Option<i64>, orders: &[SimOrder], results: &mut [OrderResult]) {
        let Some(mut s) = self.live.remove(&i) else {
            return;
        };
        let o = &orders[i];
        if let Some(set) = self.by_level.get_mut(&(o.side, o.price)) {
            set.remove(&i);
        }
        let r = &mut results[i];
        r.end = why;
        r.end_ts = ts;
        r.samples_after_end += o.samples.len() - s.si;
        s.si = o.samples.len();
    }

    fn stop_all(&mut self, why: &'static str, ts: i64, orders: &[SimOrder], results: &mut [OrderResult]) {
        let ids: Vec<usize> = self.live.keys().copied().collect();
        for i in ids {
            self.stop(i, why, Some(ts), orders, results);
        }
    }
}

/// Simulates `orders` over `rows` (one instrument) under every model and returns one result per order (same
/// order). `gaps` are the instrument's feed gaps `(start, end)`; `end_ts` the end of the data (the last batch time
/// when None). See `queuetruth.pm.queue.Engine.run`.
pub fn run(
    models: &[Model],
    lot: f64,
    rows: &Rows,
    orders: &[SimOrder],
    gaps: &[(i64, i64)],
    end_ts: Option<i64>,
) -> QtResult<Vec<OrderResult>> {
    if models.is_empty() {
        return Err(QtError::Config("no models to run".into()));
    }
    rows.check()?;
    let k = models.len();
    let eng = Engine {
        models,
        lot,
        is_foq: models.iter().map(|m| m.family == PmFamily::FrontOfQueue).collect(),
        is_tt: models.iter().map(|m| m.family == PmFamily::TradeThroughOnly).collect(),
        is_ra: models.iter().map(|m| m.family == PmFamily::RiskAdverse).collect(),
        is_prob: models.iter().map(|m| m.family.def().prob).collect(),
    };
    let mut results: Vec<OrderResult> = orders
        .iter()
        .map(|_| OrderResult {
            end: "pending",
            fills: vec![Vec::new(); k],
            ..OrderResult::default()
        })
        .collect();
    let n = rows.len();
    if n == 0 {
        for r in &mut results {
            r.end = "no_data";
        }
        return Ok(results);
    }
    let starts = batch_starts(rows);
    let bounds: Vec<(usize, usize)> = starts
        .iter()
        .enumerate()
        .map(|(j, &a)| (a, starts.get(j + 1).copied().unwrap_or(n)))
        .collect();
    let nb = bounds.len();
    let b_ts: Vec<i64> = bounds
        .iter()
        .map(|&(a, b)| rows.ts_event[a..b].iter().copied().max().unwrap_or(0))
        .collect();
    let b_recv: Vec<i64> = bounds
        .iter()
        .map(|&(a, b)| rows.ts_recv[a..b].iter().copied().max().unwrap_or(0))
        .collect();
    let b_seq: Vec<i64> = bounds.iter().map(|&(_, b)| rows.sequence[b - 1]).collect();
    let cummax = |v: &[i64]| -> Vec<i64> {
        let mut acc = i64::MIN;
        v.iter()
            .map(|&x| {
                acc = acc.max(x);
                acc
            })
            .collect()
    };
    let run_ts = cummax(&b_ts);
    let run_recv = cummax(&b_recv);
    let mut act_at: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
    let mut stop_at: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
    for (i, o) in orders.iter().enumerate() {
        if o.samples.windows(2).any(|w| w[0] > w[1]) {
            return Err(QtError::Config(format!("order {}: sample times must be sorted", o.key)));
        }
        if o.qty <= 0.0 {
            results[i].end = "empty";
            continue;
        }
        let kb = if let Some(seq) = o.entry_seq {
            searchsorted(&b_seq, seq, o.inside)
        } else if let Some(t) = o.ts_live {
            searchsorted(&run_ts, t, true)
        } else {
            return Err(QtError::Config(format!("order {}: needs ts_live or entry_seq", o.key)));
        };
        if kb >= nb {
            results[i].end = "after_data";
            results[i].samples_after_end = o.samples.len();
            continue;
        }
        if let Some(es) = o.end_seq {
            let k_end = searchsorted(&b_seq, es, o.end_inside);
            if k_end <= kb {
                results[i].end = "cancelled_before_live";
                results[i].end_ts = o.ts_end;
                continue;
            }
            stop_at.entry(k_end).or_default().push(i);
        }
        act_at.entry(kb).or_default().push(i);
    }
    let mut gap_starts: Vec<i64> = gaps.iter().map(|g| g.0).collect();
    gap_starts.sort_unstable();
    // (ts, 0 amend / 1 end, order, amend index), smallest first
    let mut deadlines: BinaryHeap<Reverse<(i64, u8, usize, usize)>> = BinaryHeap::new();
    let mut book = Book {
        live: BTreeMap::new(),
        by_level: HashMap::new(),
    };
    let mut view = Ladder::default();
    let mut gap_i = 0;
    let level_ids = |book: &Book, key: (i64, i64)| -> Vec<usize> {
        book.by_level
            .get(&key)
            .map(|s| s.iter().copied().collect())
            .unwrap_or_default()
    };

    for (bi, &(a, b)) in bounds.iter().enumerate() {
        let ts = b_ts[bi];
        for (&i, s) in book.live.iter_mut() {
            flush_samples(s, &orders[i], &mut results[i], Some(run_recv[bi]));
            flush_probes(s, &orders[i], &mut results[i], b_seq[bi]);
        }
        while gap_i < gap_starts.len() && gap_starts[gap_i] < ts {
            book.stop_all("censored_gap", ts, orders, &mut results);
            gap_i += 1;
        }
        let prev_recv = if bi > 0 { Some(run_recv[bi - 1]) } else { None };
        for &i in act_at.get(&bi).map(Vec::as_slice).unwrap_or_default() {
            let o = &orders[i];
            let res = &mut results[i];
            if let (Some(te), Some(tl)) = (o.ts_end, o.ts_live)
                && te <= tl
            {
                res.end = "cancelled_before_live";
                continue;
            }
            let mut s = Live {
                f: vec![0.0; k],
                t: vec![0.0; k],
                leaves: vec![o.qty; k],
                filled: vec![0.0; k],
                done: vec![false; k],
                taker_taken: vec![0.0; k],
                si: 0,
                pi: 0,
            };
            while s.si < o.samples.len() && prev_recv.is_some_and(|p| o.samples[s.si] < p) {
                s.si += 1;
            }
            res.samples_before_live = s.si;
            let q_view = view.qty(o.side, o.price) as f64;
            let opp_best = view.best(if o.side == BID { ASK } else { BID });
            let crosses = opp_best.is_some_and(|p| (o.side == BID && p <= o.price) || (o.side == ASK && p >= o.price));
            res.activated_ts = Some(ts);
            res.activated_seq = Some(b_seq[bi]);
            res.visible_at_live = Some(q_view);
            if crosses && o.post_only {
                res.end = "rejected_post_only";
                res.end_ts = Some(ts);
                continue;
            }
            for m in 0..k {
                s.f[m] = if eng.is_foq[m] { 0.0 } else { q_view };
            }
            if crosses {
                let at = o.ts_live.unwrap_or(ts);
                let levels = view.opp_through(o.side, o.price);
                eng.cross(&mut s, res, o.price, at, &levels, "crossed_at_live", "taker");
            }
            flush_samples(&mut s, o, res, Some(run_recv[bi]));
            while s.pi < o.probes.len() && o.probes[s.pi] < b_seq[bi] {
                s.pi += 1;
            }
            flush_probes(&mut s, o, res, b_seq[bi]);
            for (j, &(t, _)) in o.amends.iter().enumerate() {
                deadlines.push(Reverse((t, 0, i, j)));
            }
            if let (Some(te), None) = (o.ts_end, o.end_seq) {
                deadlines.push(Reverse((te, 1, i, 0)));
            }
            let all_done = s.done.iter().all(|&d| d);
            book.live.insert(i, s);
            book.by_level.entry((o.side, o.price)).or_default().insert(i);
            if all_done {
                book.stop(i, "filled", Some(ts), orders, &mut results);
            }
        }
        if let Some(ids) = stop_at.remove(&bi) {
            for i in ids {
                let t = orders[i].ts_end.unwrap_or(ts);
                book.stop(i, "cancelled", Some(t), orders, &mut results);
            }
        }
        while let Some(Reverse((t, kind, i, j))) = deadlines.peek().copied() {
            if t >= ts {
                break;
            }
            deadlines.pop();
            let Some(s) = book.live.get_mut(&i) else {
                continue;
            };
            if kind == 1 {
                book.stop(i, "cancelled", Some(t), orders, &mut results);
                continue;
            }
            let q = orders[i].amends[j].1;
            for m in 0..k {
                if !s.done[m] {
                    s.leaves[m] = nmax(q - s.filled[m], 0.0);
                    if s.leaves[m] <= EPS * lot {
                        s.done[m] = true;
                    }
                }
            }
            if s.done.iter().all(|&d| d) {
                book.stop(i, "amended_to_filled", Some(t), orders, &mut results);
            }
        }
        let mut restatement = false;
        if rows.kind[a] == K_CLEAR && view.len() > 0 {
            let mut lv: HashMap<(i64, i64), i64> = HashMap::new();
            for r in a + 1..b {
                lv.insert((rows.side[r], rows.price[r]), rows.new_qty[r]);
            }
            let cur = view.bid.len() + view.ask.len();
            restatement = lv.len() == cur
                && lv
                    .iter()
                    .all(|(&(sd, p), &q)| view.side(sd).and_then(|m| m.get(&p)) == Some(&q));
        }
        let mut touched: BTreeSet<usize> = BTreeSet::new();
        for r in a..b {
            let kind = rows.kind[r];
            if kind == K_CLEAR {
                view.clear();
                if !restatement {
                    book.stop_all("censored_clear", rows.ts_event[r], orders, &mut results);
                }
                continue;
            }
            if kind == K_DEPTH || kind == K_SNAPSHOT {
                let (side, price) = (rows.side[r], rows.price[r]);
                let prev = view.set(side, price, rows.new_qty[r])?;
                if restatement {
                    continue;
                }
                for i in level_ids(&book, (side, price)) {
                    if let Some(s) = book.live.get_mut(&i) {
                        eng.depth(s, prev as f64, rows.new_qty[r] as f64);
                        touched.insert(i);
                    }
                }
                continue;
            }
            if kind == K_TRADE {
                let (hit, price) = (rows.side[r], rows.price[r]);
                if hit != BID && hit != ASK {
                    continue;
                }
                let (v, rts) = (rows.trade_qty[r] as f64, rows.ts_event[r]);
                for i in level_ids(&book, (hit, price)) {
                    if let Some(s) = book.live.get_mut(&i) {
                        eng.trade(s, &mut results[i], orders[i].price, rts, v);
                        touched.insert(i);
                    }
                }
                let through: Vec<usize> = book
                    .by_level
                    .iter()
                    .filter(|((sd, p), idx)| {
                        *sd == hit && !idx.is_empty() && ((hit == BID && *p > price) || (hit == ASK && *p < price))
                    })
                    .flat_map(|(_, idx)| idx.iter().copied())
                    .collect();
                for i in through {
                    if let Some(s) = book.live.get_mut(&i) {
                        eng.through(s, &mut results[i], orders[i].price, rts);
                        touched.insert(i);
                    }
                }
            }
        }
        let mut ends = touched;
        let (best_a, best_b) = (view.best(ASK), view.best(BID));
        for (&(sd, p), idx) in &book.by_level {
            if (sd == BID && best_a.is_some_and(|x| p >= x)) || (sd == ASK && best_b.is_some_and(|x| p <= x)) {
                ends.extend(idx.iter().copied());
            }
        }
        for i in ends {
            let Some(s) = book.live.get_mut(&i) else {
                continue;
            };
            let o = &orders[i];
            let opp = if o.side == BID { best_a } else { best_b };
            if opp.is_some_and(|x| (o.side == BID && x <= o.price) || (o.side == ASK && x >= o.price)) {
                let levels = view.opp_through(o.side, o.price);
                eng.cross(s, &mut results[i], o.price, ts, &levels, "crossed", "maker");
            }
            s.t.iter_mut().for_each(|t| *t = 0.0);
            if s.done.iter().all(|&d| d) {
                book.stop(i, "filled", Some(ts), orders, &mut results);
            }
        }
    }
    let last = end_ts.unwrap_or(b_ts[nb - 1]);
    for (&i, s) in book.live.iter_mut() {
        flush_samples(s, &orders[i], &mut results[i], None);
    }
    let mut pending_end: HashMap<usize, i64> = HashMap::new();
    for Reverse((t, kind, i, _)) in deadlines.iter() {
        if *kind == 1 {
            pending_end.insert(*i, *t);
        }
    }
    for ids in stop_at.values() {
        for &i in ids {
            if let Some(t) = orders[i].ts_end {
                pending_end.insert(i, t);
            }
        }
    }
    let ids: Vec<usize> = book.live.keys().copied().collect();
    for i in ids {
        match pending_end.get(&i) {
            Some(&t) if t <= last => book.stop(i, "cancelled", Some(t), orders, &mut results),
            _ => book.stop(i, "open_at_end", Some(last), orders, &mut results),
        }
    }
    Ok(results)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Rows from `(ts, event_id, kind, side, price, new_qty, trade_qty)`, one batch per event id.
    fn rows(spec: &[(i64, i64, i64, i64, i64, i64, i64)]) -> Rows {
        let mut r = Rows::default();
        for (j, &(ts, ev, kind, side, price, new_qty, trade_qty)) in spec.iter().enumerate() {
            r.ts_event.push(ts);
            r.ts_recv.push(ts);
            r.event_id.push(ev);
            r.sequence.push(ev);
            r.kind.push(kind);
            r.side.push(side);
            r.price.push(price);
            r.new_qty.push(new_qty);
            r.trade_qty.push(trade_qty);
            let last = spec.get(j + 1).is_none_or(|n| n.1 != ev);
            r.flags.push(i64::from(last));
            r.part.push(0);
        }
        r
    }

    fn m(f: &str, n: Option<f64>, atomic: bool) -> Model {
        Model::new(f, n, atomic).unwrap()
    }

    fn order(qty: f64, ts_live: i64) -> SimOrder {
        SimOrder {
            key: "o".into(),
            side: BID,
            price: 4900,
            qty,
            ts_live: Some(ts_live),
            ..SimOrder::default()
        }
    }

    #[test]
    fn models_validate_like_the_python_model() {
        assert!(Model::new("power3", Some(1.5), true).is_ok());
        assert!(Model::new("power3", None, false).is_err());
        assert!(Model::new("power3", Some(-1.0), false).is_err());
        assert!(Model::new("risk_adverse", Some(2.0), false).is_err());
        assert!(Model::new("risk_adverse", None, true).is_err());
        assert!(Model::new("nope", None, false).is_err());
        assert!(run(&[], 1.0, &Rows::default(), &[], &[], None).is_err());
    }

    #[test]
    fn searchsorted_probes_like_numpy() {
        assert_eq!(searchsorted(&[1, 3, 3, 5], 3, false), 1);
        assert_eq!(searchsorted(&[1, 3, 3, 5], 3, true), 3);
        assert_eq!(searchsorted(&[], 3, true), 0);
        // unsorted: numpy's probes (mid = 2 -> 1 < 4, then mid = 3 -> 9 >= 4) give 3
        assert_eq!(searchsorted(&[5, 9, 1, 9], 4, false), 3);
    }

    #[test]
    fn prints_drain_the_queue_then_fill_and_bounds_bracket() {
        let r = rows(&[
            (0, 1, K_SNAPSHOT, BID, 4900, 10, 0),
            (0, 1, K_SNAPSHOT, ASK, 5100, 10, 0),
            (10, 2, K_TRADE, BID, 4900, 0, 6),
            (10, 2, K_DEPTH, BID, 4900, 4, 0),
            (20, 3, K_TRADE, BID, 4900, 0, 7),
            (20, 3, K_DEPTH, BID, 4900, 0, 0),
        ]);
        let models = [
            m("front_of_queue", None, false),
            m("trade_through_only", None, false),
            m("risk_adverse", None, false),
            m("power3", Some(1.0), false),
        ];
        let res = run(&models, 1.0, &r, &[order(5.0, 1)], &[], None).unwrap();
        let o = &res[0];
        assert_eq!(o.activated_ts, Some(10));
        assert_eq!(o.visible_at_live, Some(10.0));
        // front of queue: 5 of the first print; trade-through: nothing; the queue models: 3 of the second print
        assert_eq!(o.fills[0], vec![(10, 5.0, "queue", "maker", 4900.0)]);
        assert!(o.fills[1].is_empty());
        assert_eq!(o.fills[2], vec![(20, 3.0, "queue", "maker", 4900.0)]);
        assert_eq!(o.fills[3], vec![(20, 3.0, "queue", "maker", 4900.0)]);
        assert_eq!(o.end, "open_at_end");
        assert_eq!(o.end_ts, Some(20));
    }

    #[test]
    fn a_decrease_splits_by_pi_and_atomic_places_one_order() {
        let r = rows(&[
            (0, 1, K_SNAPSHOT, BID, 4900, 10, 0),
            (0, 1, K_SNAPSHOT, ASK, 5100, 10, 0),
            (10, 2, K_DEPTH, BID, 4900, 14, 0), // 4 join behind
            (20, 3, K_DEPTH, BID, 4900, 9, 0),  // 5 leave: fits ahead (10), not behind (4)
        ]);
        let models = [
            m("cancels_ahead", None, false),
            m("power3", Some(1.0), false),
            m("power3", Some(1.0), true),
        ];
        let mut o = order(1.0, 1);
        o.samples = vec![15, 25];
        let res = run(&models, 1.0, &r, &[o], &[], None).unwrap();
        let s = &res[0].samples;
        assert_eq!(s[0], (15, vec![10.0, 10.0, 10.0]));
        // cancels ahead: all 5 ahead (10 -> 5); power3 n=1 splits with pi = 1 - 10/14; atomic: 5 only fits ahead
        assert_eq!(s[1].1[0], 5.0);
        let pi: f64 = 1.0 - 10.0 / 14.0;
        assert_eq!(s[1].1[1], 10.0 - (1.0 - pi) * 5.0 + (4.0 - pi * 5.0).min(0.0));
        assert_eq!(s[1].1[2], 5.0);
    }

    #[test]
    fn post_only_into_a_crossing_book_is_rejected_and_a_taker_walks_the_levels() {
        let r = rows(&[
            (0, 1, K_SNAPSHOT, ASK, 4800, 2, 0),
            (0, 1, K_SNAPSHOT, ASK, 4900, 3, 0),
            (10, 2, K_DEPTH, ASK, 5000, 1, 0),
        ]);
        let models = [m("front_of_queue", None, false)];
        let mut po = order(4.0, 1);
        po.post_only = true;
        let taker = order(4.0, 1);
        let res = run(&models, 1.0, &r, &[po, taker], &[], None).unwrap();
        assert_eq!(res[0].end, "rejected_post_only");
        // 2 at 4800 and 2 at 4900
        assert_eq!(res[1].fills[0], vec![(1, 4.0, "crossed_at_live", "taker", 4850.0)]);
        assert_eq!(res[1].end, "filled");
    }

    #[test]
    fn a_day_boundary_restatement_keeps_orders_and_a_changed_one_stops_them() {
        let snap = |ts, ev, q| {
            [
                (ts, ev, K_CLEAR, b'N' as i64, -1, 0, 0),
                (ts, ev, K_SNAPSHOT, BID, 4900, q, 0),
                (ts, ev, K_SNAPSHOT, ASK, 5100, 10, 0),
            ]
        };
        let mut spec = snap(0, 1, 10).to_vec();
        spec.extend(snap(10, 2, 10));
        spec.extend(snap(20, 3, 9));
        let r = rows(&spec);
        let res = run(&[m("risk_adverse", None, false)], 1.0, &r, &[order(1.0, 1)], &[], None).unwrap();
        assert_eq!(res[0].activated_ts, Some(10));
        assert_eq!((res[0].end, res[0].end_ts), ("censored_clear", Some(20)));
        let res = run(
            &[m("risk_adverse", None, false)],
            1.0,
            &r,
            &[order(1.0, 1)],
            &[(15, 16)],
            None,
        )
        .unwrap();
        assert_eq!((res[0].end, res[0].end_ts), ("censored_gap", Some(20)));
        assert_eq!(
            run(
                &[m("log", None, false)],
                1.0,
                &Rows::default(),
                &[order(1.0, 1)],
                &[],
                None
            )
            .unwrap()[0]
                .end,
            "no_data"
        );
    }
}
