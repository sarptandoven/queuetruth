//! Bitstamp WebSocket v2 public capture (`live_orders_<market>`, `live_trades_<market>`,
//! `detail_order_book_<market>` JSONL, one feed message per line plus `recv_ns`) -> MBO records, venue `generic`.
//! Port of et `sources/bitstamp.py::orders_trades_to_mbo` (same counters and record stream, except where the
//! same-millisecond priority, the maker exemption and the seed choice below depart from it).
//!
//! Mapping: `order_created` -> `A`; `order_changed` -> `M` (absolute remaining `amount_str`, `price_str`);
//! `order_deleted` -> `C` with the tracked remaining; `trade` -> `T` (aggressor = `type`, 0 buy / 1 sell, assumed to
//! be the taker side; order_id = that side's order id) then `F` for each of `buy_order_id` / `sell_order_id` resting
//! on the book. Bitstamp has no sequence numbers: messages are merged by (`microtimestamp`, `recv_ns`), order events
//! before trades on ties, and `sequence` is the 1-based position in that merged stream.
//! Same-millisecond priority (livefill-kit findings; `live_orders` stamps are whole milliseconds): within one
//! microtimestamp, `order_created` messages at one (side, price) are ranked by order id, not arrival (the venue's ids
//! increase with entry time and its snapshots queue such orders by id; on the RD-12 capture, arrival ranking put 133
//! of 5,418 snapshots out of the venue's priority order inside the seed's known region, id ranking none), and a trade
//! goes after the `order_created` of its maker (trades usually arrive before the order messages); every order's own
//! messages keep their order (`id_ties_reranked`, `trades_after_maker_create`). Requeueing `order_changed` messages
//! keep arrival order.
//! Seeding: per market, the first *unambiguous* `detail_order_book` snapshot: one after which no order or trade
//! message arrives (`recv_ns`) stamped before it. A message stamped before a snapshot but arriving after it may or
//! may not be reflected in it, so such snapshots are passed over (`seed_snapshots_skipped`); when none is
//! unambiguous the first one seeds and `seed_ambiguous` (a data-quality counter) is set; a snapshot without `recv_ns`
//! is taken as is. The seed gives `R` + one `A` (`SNAPSHOT`, `LAST` on the last) per resting order at the snapshot's
//! microtimestamp; older live events are skipped (`skipped_before_snapshot`).
//! Takers (et measurement on a 10-minute BTC/USD capture): every taker appears as `order_created` + `order_deleted`
//! within the same microsecond. With `taker_window_us` set (default 100): (1) a created->deleted pair within the
//! window emits nothing (`taker_orders_suppressed`, `deleted_suppressed`), unless a trade shows the order on the
//! passive side: such a maker rested and was filled within the window and is kept (`maker_in_taker_window`); (2) a
//! created / changed order whose price crosses our best opposite is held back (`created_crossing`,
//! `changed_crossing`; a resting order re-priced across the book is pulled with a `C`, `resting_crossing`) and opened
//! with an `A` inside the first later event that uncrosses it (`deferred_opened`; `LAST` moves to the `A`); (3) a
//! changed message for a suppressed id that does not cross re-opens it (`suppressed_rested`).
//! `deleted_unknown_outside_seed_range` counts unknown deletes priced beyond the seed snapshot's deepest level (the
//! snapshot carries 100 levels per side).

use std::collections::{BTreeMap, HashMap, HashSet};

use indexmap::IndexMap;
use qt_schema::rows::{TradeKind, TradeRow};
use qt_types::{MboEvent, QtError, QtResult, Side, UNDEF_PRICE, flags};

use crate::jsonx::{self, J};
use crate::{Counters, L3Output, units};

/// Default taker window (microseconds); et measured an empty gap between 1 and 500 us.
pub const DEFAULT_TAKER_WINDOW_US: i64 = 100;

/// Converter options.
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    /// Market (e.g. `btcusd`) -> instrument id; empty = every market seen, ids 1.. in sorted market order.
    pub instruments: IndexMap<String, u32>,
    /// Price exponent (default -9).
    pub price_exp: i8,
    /// Size exponent (default -8, i.e. 1e-8 lots).
    pub size_exp: i8,
    /// Taker handling window in microseconds; `None` disables taker suppression and crossing deferral.
    pub taker_window_us: Option<i64>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            instruments: IndexMap::new(),
            price_exp: -9,
            size_exp: -8,
            taker_window_us: Some(DEFAULT_TAKER_WINDOW_US),
        }
    }
}

/// Counter names reported even when zero (et `ANOMALIES`, plus the livefill-kit fixes: `id_ties_reranked`,
/// `maker_in_taker_window`, `trades_after_maker_create`, `seed_snapshots_skipped`, `seed_ambiguous`).
pub const ANOMALIES: [&str; 22] = [
    "changed_unknown",
    "deleted_unknown",
    "deleted_unknown_outside_seed_range",
    "created_duplicate",
    "price_changes",
    "delete_size_mismatch",
    "trade_maker_unknown",
    "seeded_orders",
    "skipped_before_snapshot",
    "taker_orders_suppressed",
    "created_crossing",
    "changed_crossing",
    "resting_crossing",
    "deferred_opened",
    "suppressed_rested",
    "deleted_suppressed",
    "pending_at_end",
    "id_ties_reranked",
    "maker_in_taker_window",
    "trades_after_maker_create",
    "seed_snapshots_skipped",
    "seed_ambiguous",
];

/// Anomaly / adjustment counters copied (when non-zero) into `qt.data_quality.source_specific` at ingest. The
/// re-ranking and re-ordering counters (`id_ties_reranked`, `maker_in_taker_window`, `trades_after_maker_create`) and
/// `seed_snapshots_skipped` follow documented feed behaviour and are not copied; `seed_ambiguous` is.
pub const DQ_COUNTERS: [&str; 18] = [
    "changed_unknown",
    "deleted_unknown",
    "deleted_unknown_outside_seed_range",
    "created_duplicate",
    "price_changes",
    "delete_size_mismatch",
    "trade_maker_unknown",
    "taker_orders_suppressed",
    "created_crossing",
    "changed_crossing",
    "resting_crossing",
    "deferred_opened",
    "suppressed_rested",
    "deleted_suppressed",
    "pending_at_end",
    "event_id_gaps",
    "event_id_restarts",
    "seed_ambiguous",
];

/// Converts capture lines to MBO records, source trades and counters.
pub fn convert(
    lines: impl Iterator<Item = QtResult<String>>,
    cfg: &Config,
) -> QtResult<(Vec<MboEvent>, Vec<TradeRow>, Counters)> {
    let o = convert_full(lines, cfg)?;
    Ok((o.events, o.trades, o.counters))
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Ev {
    Created,
    Changed,
    Deleted,
    Trade,
}

impl Ev {
    fn name(self) -> &'static str {
        match self {
            Ev::Created => "order_created",
            Ev::Changed => "order_changed",
            Ev::Deleted => "order_deleted",
            Ev::Trade => "trade",
        }
    }
}

struct Msg {
    ev: Ev,
    market: String,
    us: i64,
    recv: Option<i64>,
    line: u64,
    data: J,
}

struct Seed {
    us: i64,
    recv: Option<i64>,
    entries: Vec<(u64, u8, String, String)>,
}

/// A `detail_order_book` frame that can seed (ids on both sides, at least one order); the line is kept so that only
/// the chosen seed is parsed again.
struct SeedCandidate {
    line: u64,
    us: i64,
    recv: Option<i64>,
    text: String,
}

#[derive(Clone, Copy)]
enum Item {
    Seed(u32),
    Msg(usize),
}

/// Best price per side over a changing multiset of resting prices.
#[derive(Default)]
struct Best {
    bid: BTreeMap<i64, i64>,
    ask: BTreeMap<i64, i64>,
}

impl Best {
    fn side(&mut self, side: u8) -> &mut BTreeMap<i64, i64> {
        if side == b'B' { &mut self.bid } else { &mut self.ask }
    }
    fn add(&mut self, side: u8, px: i64) {
        *self.side(side).entry(px).or_insert(0) += 1;
    }
    fn remove(&mut self, side: u8, px: i64) {
        let m = self.side(side);
        let c = m.entry(px).or_insert(0);
        *c -= 1;
        if *c == 0 {
            m.remove(&px);
        }
    }
    fn crosses(&self, side: u8, px: i64) -> bool {
        if side == b'B' {
            self.ask.keys().next().is_some_and(|&a| px >= a)
        } else {
            self.bid.keys().next_back().is_some_and(|&b| px <= b)
        }
    }
}

fn fmt_err(line: u64, msg: impl Into<String>) -> QtError {
    QtError::InputFormat {
        location: format!("bitstamp line {line}"),
        msg: msg.into(),
    }
}

fn market_of(channel: &str) -> &str {
    channel.rsplit('_').next().unwrap_or("")
}

fn side_of(v: Option<&J>, line: u64, what: &str) -> QtResult<u8> {
    match v.and_then(J::as_i64) {
        Some(0) => Ok(b'B'),
        Some(1) => Ok(b'A'),
        _ => Err(fmt_err(line, format!("{what} must be 0 (buy) or 1 (sell)"))),
    }
}

fn id_field(d: &J, key: &str, line: u64) -> QtResult<u64> {
    d.get(key)
        .and_then(J::as_u64)
        .ok_or_else(|| fmt_err(line, format!("missing or non-integer {key}")))
}

fn str_field<'a>(d: &'a J, key: &str, line: u64) -> QtResult<&'a str> {
    d.get(key)
        .and_then(J::num_text)
        .ok_or_else(|| fmt_err(line, format!("missing {key}")))
}

fn us_to_ns(us: i64, line: u64) -> QtResult<i64> {
    units::scaled_ns(us, 1_000, &format!("bitstamp line {line}"))
}

#[allow(clippy::too_many_arguments)]
fn rec(
    ts: i64,
    recv: i64,
    inst: u32,
    action: u8,
    side: u8,
    price: i64,
    size: u64,
    oid: u64,
    fl: u8,
    seq: u64,
) -> MboEvent {
    MboEvent {
        ts_event: ts,
        ts_recv: recv,
        instrument_id: inst,
        action,
        side,
        price,
        size,
        order_id: oid,
        flags: fl,
        sequence: seq,
        channel_id: 0,
        publisher_id: 0,
    }
}

/// The passive (maker) order id of a trade message: `sell_order_id` when the buyer was the taker (`type` 0), else
/// `buy_order_id`; `None` when the fields are malformed (the conversion loop reports those).
fn maker_of(m: &Msg) -> Option<u64> {
    if m.ev != Ev::Trade {
        return None;
    }
    let key = match m.data.get("type").and_then(J::as_i64) {
        Some(0) => "sell_order_id",
        Some(1) => "buy_order_id",
        _ => return None,
    };
    m.data.get(key).and_then(J::as_u64)
}

/// Re-ranks each run of items with one microtimestamp (`live_orders` stamps are whole milliseconds) by the venue's
/// priority instead of arrival: (1) `order_created` messages at one (instrument, side, price) in order id order (the
/// venue's ids increase with entry time; messages of one millisecond can arrive out of that order), each taking the
/// place of the first of them that arrived; (2) a trade after the `order_created` of its maker when both are in the
/// run (trades often arrive before the order messages); (3) otherwise arrival order, and every order's own messages
/// keep their relative order. Counts `id_ties_reranked` (creates placed ahead of an earlier-arriving create of the
/// same level) and `trades_after_maker_create` (trades placed after a later-arriving maker create).
fn rank_ties(items: &mut [(i64, i64, Item)], msgs: &[(u32, Msg)], pe: i8, st: &mut Counters) -> QtResult<()> {
    let mut lo = 0;
    while lo < items.len() {
        let mut hi = lo + 1;
        while hi < items.len() && items[hi].0 == items[lo].0 {
            hi += 1;
        }
        if hi - lo > 1 {
            rank_run(&mut items[lo..hi], msgs, pe, st)?;
        }
        lo = hi;
    }
    Ok(())
}

fn rank_run(run: &mut [(i64, i64, Item)], msgs: &[(u32, Msg)], pe: i8, st: &mut Counters) -> QtResult<()> {
    use std::cmp::Reverse;
    use std::collections::BinaryHeap;
    let n = run.len();
    let mut succ: Vec<Vec<usize>> = vec![Vec::new(); n];
    let mut indeg = vec![0usize; n];
    let mut slot: Vec<usize> = (0..n).collect();
    let mut last_of: HashMap<(u32, u64), usize> = HashMap::new();
    let mut created: HashMap<(u32, u64), usize> = HashMap::new();
    let mut levels: HashMap<(u32, u8, i64), Vec<(u64, usize)>> = HashMap::new();
    let mut trades: Vec<(usize, u32, u64)> = Vec::new();
    let mut edge = |succ: &mut Vec<Vec<usize>>, a: usize, b: usize| {
        succ[a].push(b);
        indeg[b] += 1;
    };
    for (k, it) in run.iter().enumerate() {
        let Item::Msg(j) = it.2 else { continue };
        let (inst, m) = (msgs[j].0, &msgs[j].1);
        if m.ev == Ev::Trade {
            if let Some(maker) = maker_of(m) {
                trades.push((k, inst, maker));
            }
            continue;
        }
        let oid = id_field(&m.data, "id", m.line)?;
        if let Some(prev) = last_of.insert((inst, oid), k) {
            edge(&mut succ, prev, k);
        }
        if m.ev == Ev::Created {
            created.entry((inst, oid)).or_insert(k);
            let side = side_of(m.data.get("order_type"), m.line, "order_type")?;
            let px = units::price(str_field(&m.data, "price_str", m.line)?, pe)?;
            levels.entry((inst, side, px)).or_default().push((oid, k));
        }
    }
    let mut reranked = 0u64;
    for lv in levels.values_mut().filter(|lv| lv.len() > 1) {
        let mut max_before = 0u64;
        for &(oid, _) in lv.iter() {
            reranked += u64::from(max_before > oid);
            max_before = max_before.max(oid);
        }
        lv.sort_unstable();
        let mut first = usize::MAX;
        for w in (0..lv.len()).rev() {
            first = first.min(lv[w].1);
            slot[lv[w].1] = first;
        }
        for w in lv.windows(2) {
            edge(&mut succ, w[0].1, w[1].1);
        }
    }
    let mut moved = 0u64;
    for (k, inst, maker) in trades {
        if let Some(&c) = created.get(&(inst, maker)) {
            edge(&mut succ, c, k);
            moved += u64::from(c > k);
        }
    }
    let mut ready: BinaryHeap<Reverse<(usize, usize)>> = (0..n)
        .filter(|&k| indeg[k] == 0)
        .map(|k| Reverse((slot[k], k)))
        .collect();
    let mut order = Vec::with_capacity(n);
    while let Some(Reverse((_, k))) = ready.pop() {
        order.push(k);
        for &b in &succ[k] {
            indeg[b] -= 1;
            if indeg[b] == 0 {
                ready.push(Reverse((slot[b], b)));
            }
        }
    }
    if order.len() != n {
        return Err(QtError::Engine("bitstamp: cyclic same-millisecond ordering".into()));
    }
    if order.iter().enumerate().any(|(a, &b)| a != b) {
        let old: Vec<(i64, i64, Item)> = run.to_vec();
        for (dst, src) in order.into_iter().enumerate() {
            run[dst] = old[src];
        }
    }
    *st.entry("id_ties_reranked".into()).or_insert(0) += reranked;
    *st.entry("trades_after_maker_create".into()).or_insert(0) += moved;
    Ok(())
}

/// Full conversion (records, trades, counters, resolved instruments, ingest-level quality numbers).
pub(crate) fn convert_full(lines: impl Iterator<Item = QtResult<String>>, cfg: &Config) -> QtResult<L3Output> {
    let mut orders: Vec<Msg> = Vec::new();
    let mut trades_in: Vec<Msg> = Vec::new();
    let mut detail: IndexMap<String, Vec<SeedCandidate>> = IndexMap::new();
    let mut st: Counters = Counters::new();
    let mut chain: Vec<(String, Option<String>, Option<String>, bool)> = Vec::new(); // market, event_id, pre, has_pre
    for (k, line) in lines.enumerate() {
        let line_no = k as u64 + 1;
        let text = line?;
        if text.trim().is_empty() {
            continue;
        }
        let mut m = jsonx::parse(&text).map_err(|e| fmt_err(line_no, e))?;
        let event = m.get("event").and_then(J::as_str).unwrap_or("").to_string();
        let channel = m.get("channel").and_then(J::as_str).unwrap_or("").to_string();
        let recv = m.get("recv_ns").and_then(J::as_i64);
        let ev = match event.as_str() {
            "order_created" => Some(Ev::Created),
            "order_changed" => Some(Ev::Changed),
            "order_deleted" => Some(Ev::Deleted),
            "trade" => Some(Ev::Trade),
            _ => None,
        };
        let market = market_of(&channel).to_string();
        match ev {
            Some(ev) => {
                if ev != Ev::Trade && m.get("event_id").is_some() {
                    let id_text = |v: Option<&J>| v.and_then(|x| x.num_text().map(str::to_string));
                    let pre = m.get("pre_event_id");
                    chain.push((
                        market.clone(),
                        id_text(m.get("event_id")),
                        id_text(pre),
                        pre.is_some_and(|p| !p.is_null()),
                    ));
                }
                let data = match &mut m {
                    J::Obj(fields) => fields
                        .iter()
                        .position(|(k, _)| k == "data")
                        .map(|i| fields.swap_remove(i).1)
                        .ok_or_else(|| fmt_err(line_no, "missing data"))?,
                    _ => return Err(fmt_err(line_no, "message is not an object")),
                };
                let us: i64 = str_field(&data, "microtimestamp", line_no)?
                    .trim()
                    .parse()
                    .map_err(|_| fmt_err(line_no, "microtimestamp is not an integer"))?;
                let msg = Msg {
                    ev,
                    market,
                    us,
                    recv,
                    line: line_no,
                    data,
                };
                if ev == Ev::Trade {
                    trades_in.push(msg)
                } else {
                    orders.push(msg)
                }
            }
            None if event == "data" && channel.starts_with("detail_order_book_") => {
                let d = m.get("data").unwrap_or(&J::Null);
                let usable = ["bids", "asks"].iter().all(|s| {
                    d.get(s)
                        .and_then(J::as_arr)
                        .is_some_and(|a| a.iter().all(|e| e.as_arr().is_some_and(|x| x.len() == 3)))
                });
                let n = ["bids", "asks"]
                    .iter()
                    .map(|s| d.get(s).and_then(J::as_arr).map_or(0, <[J]>::len))
                    .sum::<usize>();
                if usable && n > 0 {
                    let us: i64 = str_field(d, "microtimestamp", line_no)?
                        .trim()
                        .parse()
                        .map_err(|_| fmt_err(line_no, "microtimestamp is not an integer"))?;
                    detail.entry(market).or_default().push(SeedCandidate {
                        line: line_no,
                        us,
                        recv,
                        text,
                    });
                }
                *st.entry("book_snapshots".into()).or_insert(0) += 1;
            }
            None if event == "data" => *st.entry("book_snapshots".into()).or_insert(0) += 1,
            None => *st.entry("ignored_messages".into()).or_insert(0) += 1,
        }
    }
    let instruments: IndexMap<String, u32> = if cfg.instruments.is_empty() {
        let mut ms: Vec<&String> = orders.iter().chain(&trades_in).map(|m| &m.market).collect();
        ms.sort();
        ms.dedup();
        ms.into_iter()
            .enumerate()
            .map(|(i, m)| (m.clone(), i as u32 + 1))
            .collect()
    } else {
        cfg.instruments.clone()
    };

    // Seeds: per instrument, the first unambiguous detail snapshot (see the module docs), else the first one.
    let mut seeds: IndexMap<u32, Seed> = IndexMap::new();
    let mut stamps: HashMap<&str, Vec<(i64, i64)>> = HashMap::new(); // market -> (recv_ns, microtimestamp)
    for m in orders.iter().chain(&trades_in) {
        if let Some(r) = m.recv {
            stamps.entry(m.market.as_str()).or_default().push((r, m.us));
        }
    }
    for (market, cands) in &detail {
        let Some(&inst) = instruments.get(market) else { continue };
        let mut arrivals = stamps.remove(market.as_str()).unwrap_or_default();
        arrivals.sort_unstable();
        // min_after[k] = smallest microtimestamp among the messages arriving at or after arrivals[k]
        let mut min_after = vec![i64::MAX; arrivals.len() + 1];
        for k in (0..arrivals.len()).rev() {
            min_after[k] = min_after[k + 1].min(arrivals[k].1);
        }
        let clean = |c: &SeedCandidate| match c.recv {
            Some(r) => min_after[arrivals.partition_point(|a| a.0 <= r)] >= c.us,
            None => true, // arrival unknown: taken as is
        };
        let chosen = match cands.iter().position(clean) {
            Some(k) => {
                *st.entry("seed_snapshots_skipped".into()).or_insert(0) += k as u64;
                &cands[k]
            }
            None => {
                *st.entry("seed_ambiguous".into()).or_insert(0) += 1;
                &cands[0]
            }
        };
        let (line_no, us, recv) = (&chosen.line, chosen.us, &chosen.recv);
        let d = jsonx::parse(&chosen.text).map_err(|e| fmt_err(*line_no, e))?;
        let d = d.get("data").unwrap_or(&J::Null);
        let mut entries = Vec::new();
        for (key, side) in [("bids", b'B'), ("asks", b'A')] {
            for e in d.get(key).and_then(J::as_arr).unwrap_or(&[]) {
                let e = e.as_arr().unwrap_or(&[]);
                let (p, a, id) = match e {
                    [p, a, id] => (p, a, id),
                    _ => {
                        return Err(fmt_err(
                            *line_no,
                            "detail_order_book entry is not [price, amount, order_id]",
                        ));
                    }
                };
                let oid = id
                    .as_u64()
                    .ok_or_else(|| fmt_err(*line_no, "order_id is not an integer"))?;
                let p = p.num_text().ok_or_else(|| fmt_err(*line_no, "bad price"))?;
                let a = a.num_text().ok_or_else(|| fmt_err(*line_no, "bad amount"))?;
                entries.push((oid, side, p.to_string(), a.to_string()));
            }
        }
        seeds.insert(
            inst,
            Seed {
                us,
                recv: *recv,
                entries,
            },
        );
    }

    // Merge: seeds first (they sort ahead of any message at their microtimestamp), then orders, then trades.
    let mut msgs: Vec<(u32, Msg)> = Vec::new();
    for m in orders.into_iter().chain(trades_in) {
        let Some(&inst) = instruments.get(&m.market) else {
            continue;
        };
        if seeds.get(&inst).is_some_and(|s| m.us < s.us) {
            *st.entry("skipped_before_snapshot".into()).or_insert(0) += 1;
            continue;
        }
        msgs.push((inst, m));
    }
    let mut items: Vec<(i64, i64, Item)> = seeds.iter().map(|(&inst, s)| (s.us, -1, Item::Seed(inst))).collect();
    items.extend(
        msgs.iter()
            .enumerate()
            .map(|(i, (_, m))| (m.us, m.recv.unwrap_or(0), Item::Msg(i))),
    );
    items.sort_by_key(|it| (it.0, it.1));
    rank_ties(&mut items, &msgs, cfg.price_exp, &mut st)?;

    let handle = cfg.taker_window_us.is_some();
    let window = cfg.taker_window_us.unwrap_or(0);
    // next live_orders event (index, event, microtimestamp) for the same id
    let mut nxt: Vec<Option<(usize, Ev, i64)>> = vec![None; items.len()];
    if handle {
        let mut last: HashMap<u64, (usize, Ev, i64)> = HashMap::new();
        for i in (0..items.len()).rev() {
            let Item::Msg(j) = items[i].2 else { continue };
            let m = &msgs[j].1;
            if m.ev == Ev::Trade {
                continue;
            }
            let oid = id_field(&m.data, "id", m.line)?;
            nxt[i] = last.get(&oid).copied();
            last.insert(oid, (i, m.ev, m.us));
        }
    }

    // orders a trade shows on the passive side: they rested, whatever their messages look like
    let makers: HashSet<(u32, u64)> = msgs
        .iter()
        .filter_map(|(inst, m)| maker_of(m).map(|id| (*inst, id)))
        .collect();

    let (pe, se) = (cfg.price_exp, cfg.size_exp);
    let mut book: HashMap<u64, (u8, i64, u64)> = HashMap::new();
    let mut best: HashMap<u32, Best> = HashMap::new();
    let mut seed_range: HashMap<u32, (Option<i64>, Option<i64>)> = HashMap::new();
    let mut suppressed: HashSet<u64> = HashSet::new();
    let mut pending: IndexMap<u64, (u32, u8, i64, u64)> = IndexMap::new();
    let mut rows: Vec<MboEvent> = Vec::new();
    let mut trades: Vec<TradeRow> = Vec::new();
    let bump = |st: &mut Counters, k: &str, by: u64| *st.entry(k.to_string()).or_insert(0) += by;

    for (i, (us, _, item)) in items.iter().enumerate() {
        let seq = i as u64 + 1;
        let j = match item {
            Item::Seed(inst) => {
                let Some(s) = seeds.get(inst) else { continue };
                let ts = us_to_ns(*us, 0)?;
                let recv = s.recv.unwrap_or(ts);
                bump(&mut st, "seeded_orders", s.entries.len() as u64);
                rows.push(rec(ts, recv, *inst, b'R', b'N', UNDEF_PRICE, 0, 0, 0, seq));
                let b = best.entry(*inst).or_default();
                let (mut lo, mut hi): (Option<i64>, Option<i64>) = (None, None);
                for (k, (oid, side, p, a)) in s.entries.iter().enumerate() {
                    let (px, sz) = (units::price(p, pe)?, units::size(a, se)?);
                    book.insert(*oid, (*side, px, sz));
                    b.add(*side, px);
                    let fl = flags::SNAPSHOT | if k + 1 == s.entries.len() { flags::LAST } else { 0 };
                    rows.push(rec(ts, recv, *inst, b'A', *side, px, sz, *oid, fl, seq));
                    if *side == b'B' {
                        lo = Some(lo.map_or(px, |v| v.min(px)));
                    } else {
                        hi = Some(hi.map_or(px, |v| v.max(px)));
                    }
                }
                seed_range.insert(*inst, (lo, hi));
                continue;
            }
            Item::Msg(j) => *j,
        };
        let (inst, m) = (&msgs[j].0, &msgs[j].1);
        let inst = *inst;
        let d = &m.data;
        let ts = us_to_ns(m.us, m.line)?;
        let recv = m.recv.unwrap_or(ts);
        let px = units::price(str_field(d, "price_str", m.line)?, pe)?;
        let sz = units::size(str_field(d, "amount_str", m.line)?, se)?;
        bump(&mut st, m.ev.name(), 1);
        let n_before = rows.len();
        let b = best.entry(inst).or_default();
        if m.ev == Ev::Trade {
            let agg = side_of(d.get("type"), m.line, "type")?;
            let buy = id_field(d, "buy_order_id", m.line)?;
            let sell = id_field(d, "sell_order_id", m.line)?;
            let (taker, maker) = if agg == b'B' { (buy, sell) } else { (sell, buy) };
            trades.push(TradeRow {
                ts_event: ts,
                instrument_id: inst,
                sequence: seq,
                price: px,
                size: sz,
                aggressor_side: Side::from_byte(agg),
                kind: TradeKind::Displayed,
                maker_order_id: Some(maker),
            });
            let mut group: Vec<(u8, u8, u64)> = vec![(b'T', agg, taker)];
            for id in [buy, sell] {
                if let Some(&(bs, _, _)) = book.get(&id) {
                    group.push((b'F', bs, id));
                }
            }
            if group.len() == 1 {
                bump(&mut st, "trade_maker_unknown", 1);
            }
            let n = group.len();
            for (k, (act, side, oid)) in group.into_iter().enumerate() {
                rows.push(rec(
                    ts,
                    recv,
                    inst,
                    act,
                    side,
                    px,
                    sz,
                    oid,
                    if k + 1 == n { flags::LAST } else { 0 },
                    seq,
                ));
            }
        } else {
            let oid = id_field(d, "id", m.line)?;
            let side = side_of(d.get("order_type"), m.line, "order_type")?;
            if handle && m.ev != Ev::Deleted && !book.contains_key(&oid) {
                let quick =
                    m.ev == Ev::Created && nxt[i].is_some_and(|(_, e, nus)| e == Ev::Deleted && nus - m.us <= window);
                let taker = quick && !makers.contains(&(inst, oid));
                if quick && !taker {
                    bump(&mut st, "maker_in_taker_window", 1);
                }
                if taker {
                    bump(&mut st, "taker_orders_suppressed", 1);
                    suppressed.insert(oid);
                    pending.shift_remove(&oid);
                    continue;
                }
                if b.crosses(side, px) {
                    bump(
                        &mut st,
                        if m.ev == Ev::Created {
                            "created_crossing"
                        } else {
                            "changed_crossing"
                        },
                        1,
                    );
                    suppressed.insert(oid);
                    pending.insert(oid, (inst, side, px, sz));
                    continue;
                }
                if m.ev == Ev::Changed && suppressed.contains(&oid) {
                    bump(&mut st, "suppressed_rested", 1);
                    suppressed.remove(&oid);
                    pending.shift_remove(&oid);
                    book.insert(oid, (side, px, sz));
                    b.add(side, px);
                    rows.push(rec(ts, recv, inst, b'A', side, px, sz, oid, flags::LAST, seq));
                    continue;
                }
            }
            if m.ev == Ev::Created || (m.ev == Ev::Changed && !book.contains_key(&oid)) {
                let known = book.get(&oid).copied();
                if let Some((os, op, _)) = known {
                    bump(&mut st, "created_duplicate", 1);
                    b.remove(os, op);
                } else if m.ev == Ev::Changed {
                    bump(&mut st, "changed_unknown", 1);
                }
                book.insert(oid, (side, px, sz));
                b.add(side, px);
                let act = if known.is_some() { b'M' } else { b'A' };
                rows.push(rec(ts, recv, inst, act, side, px, sz, oid, flags::LAST, seq));
            } else if m.ev == Ev::Changed {
                let Some((bside, bpx, rem)) = book.remove(&oid) else {
                    return Err(QtError::Engine("bitstamp: changed order vanished".into()));
                };
                if px != bpx {
                    bump(&mut st, "price_changes", 1);
                }
                b.remove(bside, bpx);
                if handle && b.crosses(side, px) {
                    bump(&mut st, "resting_crossing", 1);
                    suppressed.insert(oid);
                    pending.insert(oid, (inst, side, px, sz));
                    if rem > 0 {
                        rows.push(rec(ts, recv, inst, b'C', bside, bpx, rem, oid, flags::LAST, seq));
                    }
                } else {
                    book.insert(oid, (side, px, sz));
                    b.add(side, px);
                    rows.push(rec(ts, recv, inst, b'M', side, px, sz, oid, flags::LAST, seq));
                }
            } else if let Some((bside, bpx, rem)) = book.remove(&oid) {
                b.remove(bside, bpx);
                if sz != rem {
                    bump(&mut st, "delete_size_mismatch", 1);
                }
                if rem > 0 {
                    rows.push(rec(ts, recv, inst, b'C', bside, bpx, rem, oid, flags::LAST, seq));
                }
            } else {
                if suppressed.remove(&oid) {
                    bump(&mut st, "deleted_suppressed", 1);
                    pending.shift_remove(&oid);
                    continue;
                }
                bump(&mut st, "deleted_unknown", 1);
                let (lo, hi) = seed_range.get(&inst).copied().unwrap_or((None, None));
                let outside =
                    (side == b'B' && lo.is_some_and(|l| px < l)) || (side == b'A' && hi.is_some_and(|h| px > h));
                if outside {
                    bump(&mut st, "deleted_unknown_outside_seed_range", 1);
                }
                continue;
            }
        }
        if !pending.is_empty() {
            let b = best.entry(inst).or_default();
            let keys: Vec<u64> = pending.keys().copied().collect();
            for poid in keys {
                let Some(&(pinst, pside, ppx, psz)) = pending.get(&poid) else {
                    continue;
                };
                if pinst == inst && !b.crosses(pside, ppx) {
                    pending.shift_remove(&poid);
                    suppressed.remove(&poid);
                    bump(&mut st, "deferred_opened", 1);
                    book.insert(poid, (pside, ppx, psz));
                    b.add(pside, ppx);
                    if rows.len() > n_before {
                        if let Some(last) = rows.last_mut() {
                            last.flags &= !flags::LAST;
                            last.sequence = seq;
                        }
                    }
                    rows.push(rec(ts, recv, inst, b'A', pside, ppx, psz, poid, flags::LAST, seq));
                }
            }
        }
    }

    // event_id / pre_event_id continuity per market (et `event_chain`)
    let (mut checked, mut gaps, mut restarts) = (0u64, 0u64, 0u64);
    let mut last: HashMap<String, Option<String>> = HashMap::new();
    for (mk, eid, pre, has_pre) in &chain {
        checked += 1;
        if !has_pre {
            restarts += 1;
        } else if let Some(Some(prev)) = last.get(mk) {
            if pre.as_ref() != Some(prev) {
                gaps += 1;
            }
        }
        last.insert(mk.clone(), eid.clone());
    }

    let mut out = Counters::new();
    for k in ANOMALIES {
        out.insert(k.to_string(), 0);
    }
    out.extend(st);
    out.insert("records".into(), rows.len() as u64);
    out.insert("open_at_end".into(), book.len() as u64);
    out.insert("pending_at_end".into(), pending.len() as u64);
    out.insert("event_id_checked".into(), checked);
    out.insert("event_id_gaps".into(), gaps);
    out.insert("event_id_restarts".into(), restarts);
    out.insert("seeded_instruments".into(), seeds.len() as u64);
    out.insert("taker_handling".into(), u64::from(handle));
    if let Some(w) = cfg.taker_window_us {
        out.insert("taker_window_us".into(), u64::try_from(w).unwrap_or(0));
    }
    let skipped = out.get("skipped_before_snapshot").copied().unwrap_or(0);
    Ok(L3Output {
        events: rows,
        trades,
        counters: out,
        instruments,
        sequence_gaps: gaps,
        pre_snapshot_rows_skipped: skipped,
        dq_counters: DQ_COUNTERS.to_vec(),
        feed_gaps: Vec::new(),
    })
}
