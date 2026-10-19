//! Coinbase Exchange `full` channel capture (one feed message per line plus `recv_ns`; `matches`, `level2` /
//! `level2_batch` lines may be mixed in) -> MBO records, venue `coinbase`. Port of et `sources/coinbase.py::full_to_mbo`
//! and the warm-up rule of `level2_to_books`.
//!
//! Mapping: `received` -> nothing (a later `done` for it counts `done_never_opened`, e.g. market orders); `open` ->
//! `A` with `remaining_size`; `match` -> `T` (order_id = taker, aggressor = opposite of the feed's maker `side`) +
//! `F` (maker) + `C` (maker, matched size), `LAST` on the `C` (on the `T` when the maker is unknown:
//! `match_unknown_maker`); `change` -> `M` with `new_size` (+ `new_price`); `done` canceled -> `C` with the tracked
//! remainder (`cancel_size_mismatch` if the message's `remaining_size` differs); `done` filled -> no record, or a `C`
//! of the tracked remainder when one is left (`done_filled_nonzero`). A `done` therefore always removes the whole
//! order, as Bitstamp's `order_deleted` does (et emits the message's `remaining_size` / nothing, which leaves zombie
//! size after a sequence gap; review m27). Order UUIDs become integers numbered by first use. Records are stably
//! sorted by (ts_event, sequence) (a match's `T`/`F`/`C` stay together). `last_match` is ignored.
//! Merge (review M10): messages are de-duplicated by (product_id, sequence) (first copy kept), then each product's
//! sequenced messages are put in sequence order inside the input positions that product occupies, before the order
//! state machine and the continuity check run. So `full.jsonl` and `matches.jsonl` can both be given (a directory
//! reads `full` then `matches`) and a match that fills a hole of `full` is applied before its maker's `done`;
//! interleaving across products, and so order-id numbering, is unchanged for in-order input.
//! Sequence continuity per product, over the merged stream: every jump other than +1 counts in `gaps` and becomes a
//! feed gap between the two messages' times (earlier time first).
//! Warm-up: `full` has no snapshot, so orders resting before the capture are invisible until they trade or cancel.
//! When level2 lines are present, a level is "born" once it has been empty since capture start (absent from the
//! level2 snapshot or removed by an update); a level2 row is warm when every level of its top-`warmup_depth` book is
//! born. Each row is aligned to the last `full` record at or before its time; records before the first warm row of
//! their instrument are the warm-up (`warmup_events`), reported as a feed gap.

use std::collections::{BTreeMap, HashMap, HashSet};

use indexmap::IndexMap;
use qt_schema::json::FeedGap;
use qt_schema::rows::{TradeKind, TradeRow};
use qt_types::{MboEvent, QtError, QtResult, Side, flags};

use crate::jsonx::{self, J};
use crate::{Counters, L3Output, units};

/// Converter options.
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    /// Product (e.g. `BTC-USD`) -> instrument id; empty = every product seen, ids 1.. in sorted order.
    pub instruments: IndexMap<String, u32>,
    /// Price exponent (default -9).
    pub price_exp: i8,
    /// Size exponent (default -8).
    pub size_exp: i8,
    /// Book depth whose levels must all be born for a level2 row to be warm (default 10).
    pub warmup_depth: usize,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            instruments: IndexMap::new(),
            price_exp: -9,
            size_exp: -8,
            warmup_depth: 10,
        }
    }
}

/// Counter names reported even when zero (et `ANOMALIES`).
pub const ANOMALIES: [&str; 5] = [
    "match_unknown_maker",
    "change_unknown",
    "done_unknown",
    "done_filled_nonzero",
    "cancel_size_mismatch",
];

/// Anomaly / adjustment counters copied (when non-zero) into `qt.data_quality.source_specific` at ingest.
pub const DQ_COUNTERS: [&str; 7] = [
    "match_unknown_maker",
    "change_unknown",
    "done_unknown",
    "done_filled_nonzero",
    "cancel_size_mismatch",
    "warmup_events",
    "never_warm_instruments",
];

const FULL_TYPES: [&str; 6] = ["received", "open", "done", "match", "change", "activate"];

/// Converts capture lines to MBO records, source trades and counters.
pub fn convert(
    lines: impl Iterator<Item = QtResult<String>>,
    cfg: &Config,
) -> QtResult<(Vec<MboEvent>, Vec<TradeRow>, Counters)> {
    let o = convert_full(lines, cfg)?;
    Ok((o.events, o.trades, o.counters))
}

fn fmt_err(line: u64, msg: impl Into<String>) -> QtError {
    QtError::InputFormat {
        location: format!("coinbase line {line}"),
        msg: msg.into(),
    }
}

fn s<'a>(m: &'a J, key: &str, line: u64) -> QtResult<&'a str> {
    m.get(key)
        .and_then(J::num_text)
        .ok_or_else(|| fmt_err(line, format!("missing {key}")))
}

fn side_of(m: &J, line: u64) -> QtResult<u8> {
    match m.get("side").and_then(J::as_str) {
        Some("buy") => Ok(b'B'),
        Some("sell") => Ok(b'A'),
        _ => Err(fmt_err(line, "side must be buy or sell")),
    }
}

struct Row {
    ev: MboEvent,
}

/// Full conversion (records, trades, counters, resolved instruments, ingest-level quality numbers).
pub(crate) fn convert_full(lines: impl Iterator<Item = QtResult<String>>, cfg: &Config) -> QtResult<L3Output> {
    let mut full: Vec<(u64, J)> = Vec::new();
    let mut l2: Vec<(u64, J)> = Vec::new();
    let mut seen: HashSet<(String, i64)> = HashSet::new();
    let mut st = Counters::new();
    let bump = |st: &mut Counters, k: &str, by: u64| *st.entry(k.to_string()).or_insert(0) += by;
    for (k, line) in lines.enumerate() {
        let no = k as u64 + 1;
        let t = line?;
        if t.trim().is_empty() {
            continue;
        }
        let m = jsonx::parse(&t).map_err(|e| fmt_err(no, e))?;
        let ty = m.get("type").and_then(J::as_str).unwrap_or("");
        if FULL_TYPES.contains(&ty) {
            if let (Some(p), Some(q)) = (
                m.get("product_id").and_then(J::as_str),
                m.get("sequence").and_then(J::as_i64),
            ) {
                if !seen.insert((p.to_string(), q)) {
                    bump(&mut st, "duplicates_dropped", 1);
                    continue;
                }
            }
            full.push((no, m));
        } else if ty == "snapshot" || ty == "l2update" {
            l2.push((no, m));
        } else {
            bump(&mut st, "ignored", 1);
        }
    }

    merge_by_sequence(&mut full);

    let instruments: IndexMap<String, u32> = if cfg.instruments.is_empty() {
        let mut ps: Vec<&str> = full
            .iter()
            .filter_map(|(_, m)| m.get("product_id").and_then(J::as_str))
            .collect();
        ps.sort_unstable();
        ps.dedup();
        ps.into_iter()
            .enumerate()
            .map(|(i, p)| (p.to_string(), i as u32 + 1))
            .collect()
    } else {
        cfg.instruments.clone()
    };

    let (pe, se) = (cfg.price_exp, cfg.size_exp);
    let mut ids: HashMap<String, u64> = HashMap::new();
    let oid_of = |ids: &mut HashMap<String, u64>, uuid: &str| -> u64 {
        let n = ids.len() as u64 + 1;
        *ids.entry(uuid.to_string()).or_insert(n)
    };
    let mut book: HashMap<String, (u8, i64, i128)> = HashMap::new();
    let mut received: HashSet<String> = HashSet::new();
    let mut rows: Vec<Row> = Vec::new();
    let mut trades: Vec<TradeRow> = Vec::new();
    for (no, m) in &full {
        let no = *no;
        let ty = m.get("type").and_then(J::as_str).unwrap_or("");
        let Some(&inst) = m.get("product_id").and_then(J::as_str).and_then(|p| instruments.get(p)) else {
            bump(&mut st, "ignored", 1);
            continue;
        };
        let ts = units::iso_ns(s(m, "time", no)?).map_err(|_| fmt_err(no, "bad time"))?;
        let recv = m.get("recv_ns").and_then(J::as_i64).unwrap_or(ts);
        let seq_i = m
            .get("sequence")
            .and_then(J::as_i64)
            .ok_or_else(|| fmt_err(no, "missing sequence"))?;
        let seq = u64::try_from(seq_i).map_err(|_| fmt_err(no, "negative sequence"))?;
        let oid = m.get("order_id").and_then(J::as_str).unwrap_or("").to_string();
        let mk = |action: u8, side: u8, price: i64, size: u64, order_id: u64, fl: u8| Row {
            ev: MboEvent {
                ts_event: ts,
                ts_recv: recv,
                instrument_id: inst,
                action,
                side,
                price,
                size,
                order_id,
                flags: fl,
                sequence: seq,
                channel_id: 0,
                publisher_id: 0,
            },
        };
        match ty {
            "received" => {
                bump(&mut st, "received", 1);
                received.insert(oid);
                if m.get("order_type").and_then(J::as_str) == Some("market") {
                    bump(&mut st, "market_orders", 1);
                }
            }
            "open" => {
                bump(&mut st, "open", 1);
                let side = side_of(m, no)?;
                let px = units::price(s(m, "price", no)?, pe)?;
                let sz = units::size(s(m, "remaining_size", no)?, se)?;
                book.insert(oid.clone(), (side, px, i128::from(sz)));
                let id = oid_of(&mut ids, &oid);
                rows.push(mk(b'A', side, px, sz, id, flags::LAST));
            }
            "match" => {
                bump(&mut st, "match", 1);
                let maker = s(m, "maker_order_id", no)?.to_string();
                let taker = s(m, "taker_order_id", no)?.to_string();
                let px = units::price(s(m, "price", no)?, pe)?;
                let sz = units::size(s(m, "size", no)?, se)?;
                let agg = if side_of(m, no)? == b'B' { b'A' } else { b'B' };
                let tid = oid_of(&mut ids, &taker);
                rows.push(mk(b'T', agg, px, sz, tid, 0));
                let mut maker_id = None;
                if let Some(entry) = book.get_mut(&maker) {
                    entry.2 -= i128::from(sz);
                    let side = entry.0;
                    let id = oid_of(&mut ids, &maker);
                    maker_id = Some(id);
                    rows.push(mk(b'F', side, px, sz, id, 0));
                    rows.push(mk(b'C', side, px, sz, id, flags::LAST));
                } else {
                    bump(&mut st, "match_unknown_maker", 1);
                    if let Some(last) = rows.last_mut() {
                        last.ev.flags = flags::LAST;
                    }
                }
                trades.push(TradeRow {
                    ts_event: ts,
                    instrument_id: inst,
                    sequence: seq,
                    price: px,
                    size: sz,
                    aggressor_side: Side::from_byte(agg),
                    kind: TradeKind::Displayed,
                    maker_order_id: maker_id,
                });
            }
            "change" => {
                let Some(&(side, old_px, _)) = book.get(&oid) else {
                    bump(&mut st, "change_unknown", 1);
                    continue;
                };
                bump(&mut st, "change", 1);
                let px = match m.get("new_price").and_then(J::num_text).filter(|t| !t.is_empty()) {
                    Some(t) => units::price(t, pe)?,
                    None => old_px,
                };
                let sz = units::size(s(m, "new_size", no)?, se)?;
                book.insert(oid.clone(), (side, px, i128::from(sz)));
                let id = oid_of(&mut ids, &oid);
                rows.push(mk(b'M', side, px, sz, id, flags::LAST));
            }
            "done" => {
                let Some((side, px, rem)) = book.remove(&oid) else {
                    bump(
                        &mut st,
                        if received.contains(&oid) {
                            "done_never_opened"
                        } else {
                            "done_unknown"
                        },
                        1,
                    );
                    continue;
                };
                // the replayed book holds the tracked remainder: remove all of it (m27)
                let tracked = u64::try_from(rem.max(0)).unwrap_or(u64::MAX);
                if m.get("reason").and_then(J::as_str) == Some("canceled") {
                    bump(&mut st, "done_canceled", 1);
                    let sz = units::size(s(m, "remaining_size", no)?, se)?;
                    if i128::from(sz) != rem {
                        bump(&mut st, "cancel_size_mismatch", 1);
                    }
                } else {
                    bump(&mut st, "done_filled", 1);
                    let left = units::size(m.get("remaining_size").and_then(J::num_text).unwrap_or("0"), se)?;
                    if rem != 0 || left != 0 {
                        bump(&mut st, "done_filled_nonzero", 1);
                    }
                }
                if tracked > 0 {
                    let id = oid_of(&mut ids, &oid);
                    rows.push(mk(b'C', side, px, tracked, id, flags::LAST));
                }
            }
            _ => bump(&mut st, "ignored", 1),
        }
    }
    rows.sort_by_key(|r| (r.ev.ts_event, r.ev.sequence));
    trades.sort_by_key(|t| (t.ts_event, t.sequence));
    let events: Vec<MboEvent> = rows.into_iter().map(|r| r.ev).collect();

    // sequence continuity per product over every full message (et `sequence_gaps`)
    let mut last: HashMap<String, (i64, Option<i64>)> = HashMap::new();
    let (mut checked, mut gaps) = (0u64, 0u64);
    let mut feed_gaps: Vec<FeedGap> = Vec::new();
    for (_, m) in &full {
        let Some(q) = m.get("sequence").and_then(J::as_i64) else {
            continue;
        };
        let p = m.get("product_id").and_then(J::as_str).unwrap_or("").to_string();
        let ts = m.get("time").and_then(J::as_str).and_then(|t| units::iso_ns(t).ok());
        checked += 1;
        if let Some(&(prev, prev_ts)) = last.get(&p) {
            if prev.checked_add(1) != Some(q) {
                gaps += 1;
                if let (Some(&inst), Some(a), Some(b)) = (instruments.get(&p), prev_ts, ts) {
                    // feed times are not monotone in sequence (a `received` can carry an earlier time)
                    feed_gaps.push(FeedGap {
                        instrument_id: inst,
                        ts_start: a.min(b),
                        ts_end: a.max(b),
                        reason: format!("sequence gap {prev} -> {q}"),
                    });
                }
            }
        }
        last.insert(p, (q, ts));
    }

    // warm-up from level2 rows
    let warm = warmup(&l2, &instruments, cfg, &events, &mut st)?;
    let mut warmup_events = 0u64;
    for (inst, w) in &warm {
        let recs: Vec<&MboEvent> = events.iter().filter(|e| e.instrument_id == *inst).collect();
        let (Some(first), Some(lastr)) = (recs.first(), recs.last()) else {
            continue;
        };
        match w {
            Some((seq0, ts0)) => {
                warmup_events += recs.iter().filter(|e| e.sequence < *seq0).count() as u64;
                if *ts0 > first.ts_event {
                    feed_gaps.push(FeedGap {
                        instrument_id: *inst,
                        ts_start: first.ts_event,
                        ts_end: *ts0,
                        reason: "warmup: book incomplete until every top-N level was empty once".into(),
                    });
                }
            }
            None => {
                warmup_events += recs.len() as u64;
                bump(&mut st, "never_warm_instruments", 1);
                feed_gaps.push(FeedGap {
                    instrument_id: *inst,
                    ts_start: first.ts_event,
                    ts_end: lastr.ts_event,
                    reason: "warmup: level2 book never warmed up".into(),
                });
            }
        }
    }
    feed_gaps.sort_by_key(|g| (g.instrument_id, g.ts_start, g.ts_end));

    let mut out = Counters::new();
    for k in ANOMALIES {
        out.insert(k.to_string(), 0);
    }
    out.extend(st);
    out.insert("records".into(), events.len() as u64);
    out.insert("open_at_end".into(), book.len() as u64);
    out.insert("checked".into(), checked);
    out.insert("gaps".into(), gaps);
    out.insert("warmup_measured".into(), u64::from(!l2.is_empty()));
    out.insert("warmup_events".into(), warmup_events);
    Ok(L3Output {
        events,
        trades,
        counters: out,
        instruments,
        sequence_gaps: gaps,
        pre_snapshot_rows_skipped: 0,
        dq_counters: DQ_COUNTERS.to_vec(),
        feed_gaps,
    })
}

/// Puts each product's sequenced messages in sequence order (stable) inside the positions that product occupies in
/// `full`; messages without a product or a sequence keep their position (review M10).
fn merge_by_sequence(full: &mut Vec<(u64, J)>) {
    let key = |m: &J| -> Option<(String, i64)> {
        Some((
            m.get("product_id").and_then(J::as_str)?.to_string(),
            m.get("sequence").and_then(J::as_i64)?,
        ))
    };
    let mut slots: HashMap<String, Vec<(usize, i64)>> = HashMap::new();
    for (i, (_, m)) in full.iter().enumerate() {
        if let Some((p, q)) = key(m) {
            slots.entry(p).or_default().push((i, q));
        }
    }
    let mut perm: Vec<usize> = (0..full.len()).collect();
    let mut moved = false;
    // products occupy disjoint positions, so the map's iteration order does not matter
    for v in slots.values() {
        let mut by_seq = v.clone();
        by_seq.sort_by_key(|&(_, q)| q);
        for (&(slot, _), &(src, _)) in v.iter().zip(&by_seq) {
            moved |= slot != src;
            perm[slot] = src;
        }
    }
    if !moved {
        return;
    }
    let mut taken: Vec<Option<(u64, J)>> = std::mem::take(full).into_iter().map(Some).collect();
    full.extend(perm.into_iter().filter_map(|src| taken[src].take()));
}

/// Per instrument with level2 data: `Some((first warm aligned sequence, its ts_event))` or `None` (never warm).
fn warmup(
    l2: &[(u64, J)],
    instruments: &IndexMap<String, u32>,
    cfg: &Config,
    events: &[MboEvent],
    st: &mut Counters,
) -> QtResult<BTreeMap<u32, Option<(u64, i64)>>> {
    let mut out: BTreeMap<u32, Option<(u64, i64)>> = BTreeMap::new();
    if l2.is_empty() {
        return Ok(out);
    }
    for k in ["warm_rows", "l2_rows_before_first_event"] {
        st.entry(k.to_string()).or_insert(0);
    }
    let (pe, se, depth) = (cfg.price_exp, cfg.size_exp, cfg.warmup_depth);
    let mut msgs: Vec<&(u64, J)> = l2.iter().collect();
    msgs.sort_by_key(|(_, m)| m.get("recv_ns").and_then(J::as_i64).unwrap_or(0));
    type Side2 = BTreeMap<i64, u64>;
    let mut books: HashMap<u32, (Side2, Side2)> = HashMap::new();
    let mut initial: HashMap<u32, HashSet<(u8, i64)>> = HashMap::new();
    let mut born: HashMap<u32, HashSet<(u8, i64)>> = HashMap::new();
    let mut rows: Vec<(i64, u32, bool)> = Vec::new();
    let mut bump = |k: &str| *st.entry(k.to_string()).or_insert(0) += 1;
    for (no, m) in msgs {
        let no = *no;
        let Some(&inst) = m.get("product_id").and_then(J::as_str).and_then(|p| instruments.get(p)) else {
            continue;
        };
        let te = if m.get("type").and_then(J::as_str) == Some("snapshot") {
            bump("level2_snapshots");
            let mut sides: [Side2; 2] = [BTreeMap::new(), BTreeMap::new()];
            for (k, key) in ["bids", "asks"].iter().enumerate() {
                for lv in m.get(key).and_then(J::as_arr).unwrap_or(&[]) {
                    let [p, q, ..] = lv.as_arr().unwrap_or(&[]) else {
                        return Err(fmt_err(no, "bad level"));
                    };
                    let px = units::price(p.num_text().unwrap_or(""), pe)?;
                    sides[k].insert(px, units::size(q.num_text().unwrap_or(""), se)?);
                }
            }
            let [bids, asks] = sides;
            let init: HashSet<(u8, i64)> = bids
                .keys()
                .map(|&p| (b'B', p))
                .chain(asks.keys().map(|&p| (b'A', p)))
                .collect();
            initial.insert(inst, init);
            born.insert(inst, HashSet::new());
            books.insert(inst, (bids, asks));
            m.get("recv_ns").and_then(J::as_i64).unwrap_or(0)
        } else {
            let Some((bids, asks)) = books.get_mut(&inst) else {
                bump("updates_before_snapshot");
                continue;
            };
            bump("level2_updates");
            for c in m.get("changes").and_then(J::as_arr).unwrap_or(&[]) {
                let [sd, p, q, ..] = c.as_arr().unwrap_or(&[]) else {
                    return Err(fmt_err(no, "bad change"));
                };
                let side = if sd.as_str() == Some("buy") { b'B' } else { b'A' };
                let px = units::price(p.num_text().unwrap_or(""), pe)?;
                let sz = units::size(q.num_text().unwrap_or(""), se)?;
                let book = if side == b'B' { &mut *bids } else { &mut *asks };
                if sz == 0 {
                    book.remove(&px);
                    born.entry(inst).or_default().insert((side, px));
                } else {
                    book.insert(px, sz);
                }
            }
            units::iso_ns(s(m, "time", no)?).map_err(|_| fmt_err(no, "bad time"))?
        };
        let Some((bids, asks)) = books.get(&inst) else { continue };
        let levels = bids
            .keys()
            .rev()
            .take(depth)
            .map(|&p| (b'B', p))
            .chain(asks.keys().take(depth).map(|&p| (b'A', p)));
        let empty = HashSet::new();
        let (init, b) = (initial.get(&inst).unwrap_or(&empty), born.get(&inst).unwrap_or(&empty));
        let is_warm = levels.into_iter().all(|k| !init.contains(&k) || b.contains(&k));
        if is_warm {
            bump("warm_rows");
        }
        rows.push((te, inst, is_warm));
    }
    // align each row to the last record of its instrument with ts_event <= its time (et assign_sequence)
    let mut per: HashMap<u32, Vec<(i64, u64)>> = HashMap::new();
    for e in events {
        per.entry(e.instrument_id).or_default().push((e.ts_event, e.sequence));
    }
    for v in per.values_mut() {
        v.sort_unstable();
    }
    for (te, inst, is_warm) in rows {
        let entry = out.entry(inst).or_insert(None);
        let Some(v) = per.get(&inst) else { continue };
        let k = v.partition_point(|&(t, _)| t <= te);
        if k == 0 {
            bump("l2_rows_before_first_event");
            continue;
        }
        if is_warm {
            let (t, q) = v[k - 1];
            if entry.is_none_or(|(q0, _)| q < q0) {
                *entry = Some((q, t));
            }
        }
    }
    Ok(out)
}
