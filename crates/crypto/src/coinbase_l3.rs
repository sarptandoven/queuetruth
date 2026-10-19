//! Coinbase Exchange `level3` channel capture (the compact form of `full`; authenticated) plus REST
//! `GET /products/{id}/book?level=3` snapshots -> MBO records, rule set `crypto_l3`.
//! Message schemas: <https://docs.cdp.coinbase.com/exchange/websocket-feed/channels#level3-channel> (accessed
//! 2026-10-19). Coinbase Advanced Trade has no order-level channel (its public channels are `level2`, `ticker`,
//! `market_trades`, ...), so this is the only Coinbase order-level source.
//!
//! Capture lines: `{"recv_ns": N, "msg": <frame>}` or the bare frame. The first frame after subscribing is the
//! schema `{"type": "level3", "schema": {"open": [field, ...], ...}}`; every later frame is an array whose values
//! follow the schema of its type (`frame[0]`). A new schema frame replaces the current one from that line on; arrays
//! seen before any schema use the documented schema (`schema_assumed`). Snapshot lines:
//! `{"recv_ns": N, "product_id": "BTC-USD", "book": <REST level=3 body: {"sequence", "bids": [[price, size,
//! order_id], ...], "asks": [...], "time"}>}`. `heartbeat` frames (`sequence` per product) are used for the trailing
//! gap check; everything else (subscriptions, errors) is ignored.
//!
//! Book building (the documented `full` procedure): per product, messages are de-duplicated by sequence, put in
//! sequence order and those at or below the first snapshot's sequence are dropped (`superseded_by_snapshot`); the
//! snapshot becomes `R` + `A` (`SNAPSHOT`, REST order = queue order) and later messages apply on top. A later
//! snapshot re-seeds (`R` + `A`, `resnapshots`) and closes any open gap. Mapping: `open` -> `A` (`size` is the resting
//! size); `change` -> `M` (null `price` keeps the price); `match` -> `T` (taker id, aggressor = opposite of the
//! tracked maker side, `N` when the maker is unknown) + `F` + `C` of the maker; `done` -> `C` of the tracked
//! remainder when one is left (a cancel: fills always arrive as `match` first, so a non-zero remainder at `done` is
//! exactly a cancel / expiry) and nothing when the order was filled to zero; `noop` advances the sequence only.
//! `done` / `change` for an order not on the book (taker or not-yet-open orders, which the compact channel does not
//! announce) are ignored (`done_not_on_book`, `change_not_on_book`).
//! Exactness: with a snapshot and no sequence gap the book is the venue's full-depth book, order by order, with
//! venue order ids and queue order (KeepOnSizeDecrease, as Coinbase `change`). A sequence gap opens a feed gap that
//! lasts until the next snapshot (the book is uncertain in between). A product without a snapshot is warm-up only:
//! orders resting before the capture are invisible, so its whole range is a feed gap (`unseeded_products`).
//! Coinbase: "Authenticated feed messages do not increment the sequence number" (auth docs), so a dropped
//! authenticated-only message cannot be detected.

use std::collections::{BTreeMap, HashMap, HashSet};

use indexmap::IndexMap;
use qt_schema::json::FeedGap;
use qt_schema::rows::{TradeKind, TradeRow};
use qt_types::{MboEvent, QtError, QtResult, Side, flags};

use crate::jsonx::J;
use crate::l3util::{self, rec};
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
}

impl Default for Config {
    fn default() -> Self {
        Self {
            instruments: IndexMap::new(),
            price_exp: -9,
            size_exp: -8,
        }
    }
}

/// The documented `level3` schema (used until the capture's own schema frame is seen).
pub const DOC_SCHEMA: [(&str, &[&str]); 5] = [
    (
        "change",
        &["type", "product_id", "sequence", "order_id", "price", "size", "time"],
    ),
    ("done", &["type", "product_id", "sequence", "order_id", "time"]),
    (
        "match",
        &[
            "type",
            "product_id",
            "sequence",
            "maker_order_id",
            "taker_order_id",
            "price",
            "size",
            "time",
        ],
    ),
    ("noop", &["type", "product_id", "sequence", "time"]),
    (
        "open",
        &[
            "type",
            "product_id",
            "sequence",
            "order_id",
            "side",
            "price",
            "size",
            "time",
        ],
    ),
];

/// Counter names reported even when zero.
pub const ANOMALIES: [&str; 6] = [
    "schema_assumed",
    "match_unknown_maker",
    "match_overfill",
    "open_duplicate",
    "unseeded_products",
    "heartbeat_trailing_gaps",
];

/// Anomaly / caveat counters copied (when non-zero) into `qt.data_quality.source_specific` at ingest.
pub const DQ_COUNTERS: [&str; 6] = ANOMALIES;

const L3_TYPES: [&str; 5] = ["change", "done", "match", "noop", "open"];

fn fmt_err(line: u64, msg: impl Into<String>) -> QtError {
    QtError::InputFormat {
        location: format!("coinbase_level3 line {line}"),
        msg: msg.into(),
    }
}

/// Converts capture lines to MBO records, source trades and counters.
pub fn convert(
    lines: impl Iterator<Item = QtResult<String>>,
    cfg: &Config,
) -> QtResult<(Vec<MboEvent>, Vec<TradeRow>, Counters)> {
    let o = convert_full(lines, cfg)?;
    Ok((o.events, o.trades, o.counters))
}

/// One decoded `level3` message.
struct Msg {
    no: u64,
    recv: Option<i64>,
    ty: String,
    seq: i64,
    ts: i64,
    fields: Vec<(String, J)>,
}

impl Msg {
    fn get(&self, k: &str) -> Option<&J> {
        self.fields.iter().find(|(n, _)| n == k).map(|(_, v)| v)
    }

    fn text(&self, k: &str) -> QtResult<&str> {
        self.get(k)
            .and_then(J::num_text)
            .ok_or_else(|| fmt_err(self.no, format!("{} message: missing {k}", self.ty)))
    }
}

/// One REST level=3 snapshot.
struct Snap {
    no: u64,
    recv: Option<i64>,
    seq: i64,
    ts: Option<i64>,
    book: J,
}

enum Ev {
    Msg(Msg),
    Snap(Snap),
}

impl Ev {
    fn key(&self) -> (i64, u8) {
        match self {
            Ev::Msg(m) => (m.seq, 0),
            Ev::Snap(s) => (s.seq, 1),
        }
    }
}

type Schema = HashMap<String, Vec<String>>;

fn doc_schema() -> Schema {
    DOC_SCHEMA
        .iter()
        .map(|(t, f)| ((*t).to_string(), f.iter().map(|x| (*x).to_string()).collect()))
        .collect()
}

fn read_schema(v: &J, no: u64) -> QtResult<Schema> {
    let J::Obj(m) = v else {
        return Err(fmt_err(no, "level3 schema is not an object"));
    };
    let mut s = Schema::new();
    for (t, f) in m {
        let names = f
            .as_arr()
            .ok_or_else(|| fmt_err(no, format!("level3 schema for {t} is not a list")))?
            .iter()
            .map(|x| x.as_str().map(str::to_string))
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| fmt_err(no, format!("level3 schema for {t} has a non-string field name")))?;
        s.insert(t.clone(), names);
    }
    Ok(s)
}

fn seq_of(v: Option<&J>, no: u64) -> QtResult<i64> {
    let q = v
        .and_then(J::as_i64)
        .ok_or_else(|| fmt_err(no, "missing or non-integer sequence"))?;
    if q < 0 {
        return Err(fmt_err(no, "negative sequence"));
    }
    Ok(q)
}

/// Full conversion (records, trades, counters, resolved instruments, ingest-level quality numbers).
pub fn convert_full(lines: impl Iterator<Item = QtResult<String>>, cfg: &Config) -> QtResult<L3Output> {
    let mut st = Counters::new();
    let bump = |st: &mut Counters, k: &str| *st.entry(k.to_string()).or_insert(0) += 1;
    let mut schema: Option<Schema> = None;
    let fallback = doc_schema();
    let mut per: BTreeMap<String, Vec<Ev>> = BTreeMap::new();
    let mut hb: HashMap<String, (i64, Option<i64>)> = HashMap::new();
    let mut seen: HashSet<(String, i64)> = HashSet::new();
    for (k, line) in lines.enumerate() {
        let no = k as u64 + 1;
        let t = line?;
        if t.trim().is_empty() {
            continue;
        }
        let f = l3util::unwrap_line(&t, no, fmt_err)?;
        // REST snapshot line
        if let Some(book) = f.msg.get("book") {
            let p = f
                .msg
                .get("product_id")
                .and_then(J::as_str)
                .ok_or_else(|| fmt_err(no, "snapshot line without product_id"))?
                .to_string();
            let seq = seq_of(book.get("sequence"), no)?;
            let ts = match book.get("time").and_then(J::as_str) {
                Some(s) => Some(units::iso_ns(s).map_err(|_| fmt_err(no, "bad snapshot time"))?),
                None => None,
            };
            bump(&mut st, "snapshots");
            per.entry(p).or_default().push(Ev::Snap(Snap {
                no,
                recv: f.recv,
                seq,
                ts,
                book: book.clone(),
            }));
            continue;
        }
        let (ty, fields): (String, Vec<(String, J)>) = match &f.msg {
            J::Arr(a) => {
                let ty = a
                    .first()
                    .and_then(J::as_str)
                    .ok_or_else(|| fmt_err(no, "array frame without a type"))?
                    .to_string();
                let names = match &schema {
                    Some(s) => s.get(&ty),
                    None => {
                        if fallback.contains_key(&ty) {
                            bump(&mut st, "schema_assumed");
                        }
                        fallback.get(&ty)
                    }
                };
                let Some(names) = names else {
                    bump(&mut st, "unknown_type");
                    continue;
                };
                (ty, names.iter().cloned().zip(a.iter().cloned()).collect())
            }
            J::Obj(m) => {
                let ty = f.msg.get("type").and_then(J::as_str).unwrap_or("").to_string();
                if ty == "level3" {
                    if let Some(s) = f.msg.get("schema") {
                        let s = read_schema(s, no)?;
                        if schema.as_ref().is_some_and(|old| *old != s) {
                            bump(&mut st, "schema_changes");
                        }
                        bump(&mut st, "schema_messages");
                        schema = Some(s);
                        continue;
                    }
                }
                if ty == "heartbeat" {
                    if let (Some(p), Some(q)) = (
                        f.msg.get("product_id").and_then(J::as_str),
                        f.msg.get("sequence").and_then(J::as_i64),
                    ) {
                        bump(&mut st, "heartbeats");
                        let ts = f
                            .msg
                            .get("time")
                            .and_then(J::as_str)
                            .and_then(|s| units::iso_ns(s).ok());
                        let e = hb.entry(p.to_string()).or_insert((q, ts));
                        if q > e.0 {
                            *e = (q, ts);
                        }
                    }
                    continue;
                }
                if !L3_TYPES.contains(&ty.as_str()) {
                    bump(&mut st, "ignored");
                    continue;
                }
                (ty, m.clone())
            }
            _ => {
                bump(&mut st, "ignored");
                continue;
            }
        };
        if !L3_TYPES.contains(&ty.as_str()) {
            bump(&mut st, "unknown_type");
            continue;
        }
        let get = |k: &str| fields.iter().find(|(n, _)| n == k).map(|(_, v)| v);
        let p = get("product_id")
            .and_then(J::as_str)
            .ok_or_else(|| fmt_err(no, format!("{ty} message: missing product_id")))?
            .to_string();
        let seq = seq_of(get("sequence"), no)?;
        let ts = units::iso_ns(
            get("time")
                .and_then(J::as_str)
                .ok_or_else(|| fmt_err(no, format!("{ty} message: missing time")))?,
        )
        .map_err(|_| fmt_err(no, "bad time"))?;
        if !seen.insert((p.clone(), seq)) {
            bump(&mut st, "duplicates_dropped");
            continue;
        }
        per.entry(p).or_default().push(Ev::Msg(Msg {
            no,
            recv: f.recv,
            ty,
            seq,
            ts,
            fields,
        }));
    }

    let instruments: IndexMap<String, u32> = if cfg.instruments.is_empty() {
        per.keys().enumerate().map(|(i, p)| (p.clone(), i as u32 + 1)).collect()
    } else {
        cfg.instruments.clone()
    };

    let (pe, se) = (cfg.price_exp, cfg.size_exp);
    let mut ids: HashMap<String, u64> = HashMap::new();
    let mut oid_of = |uuid: &str| -> u64 {
        let n = ids.len() as u64 + 1;
        *ids.entry(uuid.to_string()).or_insert(n)
    };
    // (sort key, product index, record)
    let mut out: Vec<(i64, usize, MboEvent)> = Vec::new();
    let mut trades: Vec<(i64, usize, TradeRow)> = Vec::new();
    let mut feed_gaps: Vec<FeedGap> = Vec::new();
    let mut gaps = 0u64;
    let mut open_at_end = 0u64;
    for (pi, (product, evs)) in per.iter_mut().enumerate() {
        let Some(&inst) = instruments.get(product) else {
            st.entry("ignored".into())
                .and_modify(|x| *x += evs.len() as u64)
                .or_insert(evs.len() as u64);
            continue;
        };
        evs.sort_by_key(Ev::key);
        let has_snapshot = evs.iter().any(|e| matches!(e, Ev::Snap(_)));
        let mut seeded = false;
        let mut book: HashMap<String, (u8, i64, u64)> = HashMap::new();
        let mut last_seq: Option<i64> = None;
        let mut last_ts: Option<i64> = None;
        let mut mono = i64::MIN;
        let mut gap_open: Option<(i64, String)> = None;
        let mut recs: Vec<MboEvent> = Vec::new();
        let (mut first_ts, mut final_ts) = (None::<i64>, None::<i64>);
        for i in 0..evs.len() {
            match &evs[i] {
                Ev::Snap(s) => {
                    let next_ts = evs[i + 1..].iter().find_map(|e| match e {
                        Ev::Msg(m) => Some(m.ts),
                        Ev::Snap(_) => None,
                    });
                    let mut ts = s.ts.or(s.recv).or(next_ts).or(last_ts).unwrap_or(0);
                    if let Some(n) = next_ts {
                        ts = ts.min(n);
                    }
                    if let Some(l) = last_ts {
                        ts = ts.max(l);
                    }
                    let recv = s.recv.unwrap_or(ts).max(0);
                    let seq = u64::try_from(s.seq).unwrap_or(0);
                    if seeded {
                        *st.entry("resnapshots".into()).or_insert(0) += 1;
                    }
                    if let Some((a, why)) = gap_open.take() {
                        feed_gaps.push(FeedGap {
                            instrument_id: inst,
                            ts_start: a.min(ts),
                            ts_end: a.max(ts),
                            reason: format!("{why}; book uncertain until the next snapshot"),
                        });
                    }
                    book.clear();
                    let start = recs.len();
                    recs.push(rec(ts, recv, inst, b'R', b'N', 0, 0, 0, flags::SNAPSHOT, seq));
                    for (key, side) in [("bids", b'B'), ("asks", b'A')] {
                        for row in s.book.get(key).and_then(J::as_arr).unwrap_or(&[]) {
                            let r = row.as_arr().unwrap_or(&[]);
                            let (Some(px), Some(sz), Some(id)) = (
                                r.first().and_then(J::num_text),
                                r.get(1).and_then(J::num_text),
                                r.get(2).and_then(J::as_str),
                            ) else {
                                return Err(fmt_err(
                                    s.no,
                                    format!("snapshot {key} row is not [price, size, order_id] (level=3 book?)"),
                                ));
                            };
                            let (px, sz) = (units::price(px, pe)?, units::size(sz, se)?);
                            if sz == 0 {
                                *st.entry("snapshot_zero_size_rows".into()).or_insert(0) += 1;
                                continue;
                            }
                            if book.contains_key(id) {
                                *st.entry("snapshot_duplicate_orders".into()).or_insert(0) += 1;
                                continue;
                            }
                            book.insert(id.to_string(), (side, px, sz));
                            recs.push(rec(
                                ts,
                                recv,
                                inst,
                                b'A',
                                side,
                                px,
                                sz,
                                oid_of(id),
                                flags::SNAPSHOT,
                                seq,
                            ));
                        }
                    }
                    l3util::mark_last(&mut recs, start);
                    seeded = true;
                    last_seq = Some(s.seq);
                    last_ts = Some(ts);
                    first_ts.get_or_insert(ts);
                    final_ts = Some(ts);
                    for r in &mut recs[start..] {
                        mono = mono.max(r.ts_event);
                        out.push((mono, pi, *r));
                    }
                    recs.truncate(start);
                }
                Ev::Msg(m) => {
                    if has_snapshot && !seeded {
                        *st.entry("superseded_by_snapshot".into()).or_insert(0) += 1;
                        continue;
                    }
                    if let Some(p) = last_seq {
                        if p.checked_add(1) != Some(m.seq) {
                            gaps += 1;
                            let a = last_ts.unwrap_or(m.ts);
                            gap_open.get_or_insert((a, format!("sequence gap {p} -> {}", m.seq)));
                        }
                    }
                    last_seq = Some(m.seq);
                    let (ts, seq) = (m.ts, m.seq as u64);
                    let recv = m.recv.unwrap_or(ts);
                    first_ts.get_or_insert(ts);
                    final_ts = Some(ts);
                    last_ts = Some(ts);
                    let start = recs.len();
                    *st.entry(m.ty.clone()).or_insert(0) += 1;
                    match m.ty.as_str() {
                        "open" => {
                            let id = m.text("order_id")?.to_string();
                            let side = match m.text("side")? {
                                "buy" => b'B',
                                "sell" => b'A',
                                _ => return Err(fmt_err(m.no, "open side must be buy or sell")),
                            };
                            let px = units::price(m.text("price")?, pe)?;
                            let sz = units::size(m.text("size")?, se)?;
                            // a second `open` of a live order moves it (M), never a second add; an entry filled to
                            // zero (waiting for its `done`) is no longer in the replayed book
                            let action = match book.insert(id.clone(), (side, px, sz)) {
                                Some(old) if old.2 > 0 => {
                                    bump(&mut st, "open_duplicate");
                                    b'M'
                                }
                                Some(_) => {
                                    bump(&mut st, "open_duplicate");
                                    b'A'
                                }
                                None => b'A',
                            };
                            recs.push(rec(ts, recv, inst, action, side, px, sz, oid_of(&id), 0, seq));
                        }
                        "change" => {
                            let id = m.text("order_id")?.to_string();
                            if let Some(o) = book.get_mut(&id).filter(|o| o.2 > 0) {
                                let px = match m.get("price").and_then(J::num_text).filter(|t| !t.is_empty()) {
                                    Some(t) => units::price(t, pe)?,
                                    None => o.1,
                                };
                                let sz = units::size(m.text("size")?, se)?;
                                (o.1, o.2) = (px, sz);
                                let side = o.0;
                                recs.push(rec(ts, recv, inst, b'M', side, px, sz, oid_of(&id), 0, seq));
                            } else {
                                bump(&mut st, "change_not_on_book");
                            }
                        }
                        "match" => {
                            let maker = m.text("maker_order_id")?.to_string();
                            let taker = m.text("taker_order_id")?.to_string();
                            let px = units::price(m.text("price")?, pe)?;
                            let sz = units::size(m.text("size")?, se)?;
                            let maker_side = book.get(&maker).map(|o| o.0);
                            let agg = match maker_side {
                                Some(b'B') => b'A',
                                Some(_) => b'B',
                                None => b'N',
                            };
                            recs.push(rec(ts, recv, inst, b'T', agg, px, sz, oid_of(&taker), 0, seq));
                            let mut maker_id = None;
                            if book.get(&maker).is_some_and(|o| o.2 == 0) {
                                // filled to zero already (its `done` is still to come)
                                bump(&mut st, "match_overfill");
                            } else if let Some(o) = book.get_mut(&maker) {
                                let take = if sz > o.2 {
                                    bump(&mut st, "match_overfill");
                                    o.2
                                } else {
                                    sz
                                };
                                o.2 -= take;
                                let (side, opx) = (o.0, o.1);
                                if opx != px {
                                    bump(&mut st, "match_off_price");
                                }
                                let id = oid_of(&maker);
                                maker_id = Some(id);
                                recs.push(rec(ts, recv, inst, b'F', side, px, sz, id, 0, seq));
                                if take > 0 {
                                    recs.push(rec(ts, recv, inst, b'C', side, opx, take, id, 0, seq));
                                }
                            } else {
                                bump(&mut st, "match_unknown_maker");
                            }
                            trades.push((
                                mono.max(ts),
                                pi,
                                TradeRow {
                                    ts_event: ts,
                                    instrument_id: inst,
                                    sequence: seq,
                                    price: px,
                                    size: sz,
                                    aggressor_side: Side::from_byte(agg),
                                    kind: TradeKind::Displayed,
                                    maker_order_id: maker_id,
                                },
                            ));
                        }
                        "done" => {
                            let id = m.text("order_id")?.to_string();
                            match book.remove(&id) {
                                Some((side, px, rem)) if rem > 0 => {
                                    bump(&mut st, "done_canceled");
                                    recs.push(rec(ts, recv, inst, b'C', side, px, rem, oid_of(&id), 0, seq));
                                }
                                Some(_) => bump(&mut st, "done_filled"),
                                None => bump(&mut st, "done_not_on_book"),
                            }
                        }
                        _ => {} // noop: sequence only
                    }
                    l3util::mark_last(&mut recs, start);
                    for r in &mut recs[start..] {
                        mono = mono.max(r.ts_event);
                        out.push((mono, pi, *r));
                    }
                    recs.truncate(start);
                }
            }
        }
        if let (Some((a, why)), Some(e)) = (gap_open.take(), final_ts) {
            feed_gaps.push(FeedGap {
                instrument_id: inst,
                ts_start: a.min(e),
                ts_end: a.max(e),
                reason: format!("{why}; no later snapshot"),
            });
        }
        if let (Some(&(q, hts)), Some(l)) = (hb.get(product), last_seq) {
            if q > l {
                bump(&mut st, "heartbeat_trailing_gaps");
                let a = final_ts.unwrap_or(0);
                let b = hts.unwrap_or(a);
                feed_gaps.push(FeedGap {
                    instrument_id: inst,
                    ts_start: a.min(b),
                    ts_end: a.max(b),
                    reason: format!("heartbeat sequence {q} beyond the last message {l}"),
                });
            }
        }
        if !has_snapshot {
            if let (Some(a), Some(b)) = (first_ts, final_ts) {
                bump(&mut st, "unseeded_products");
                feed_gaps.push(FeedGap {
                    instrument_id: inst,
                    ts_start: a,
                    ts_end: b,
                    reason: "no level3 REST snapshot: orders resting before the capture are invisible".into(),
                });
            }
        }
        open_at_end += book.len() as u64;
    }
    out.sort_by_key(|(k, p, _)| (*k, *p));
    trades.sort_by_key(|(k, p, _)| (*k, *p));
    let events: Vec<MboEvent> = out.into_iter().map(|(_, _, e)| e).collect();
    let trades: Vec<TradeRow> = trades.into_iter().map(|(_, _, t)| t).collect();
    feed_gaps.sort_by_key(|g| (g.instrument_id, g.ts_start, g.ts_end));

    let mut counters = Counters::new();
    for k in ANOMALIES {
        counters.insert(k.to_string(), 0);
    }
    counters.extend(st);
    counters.insert("records".into(), events.len() as u64);
    counters.insert("open_at_end".into(), open_at_end);
    counters.insert("gaps".into(), gaps);
    Ok(L3Output {
        events,
        trades,
        counters,
        instruments,
        sequence_gaps: gaps,
        pre_snapshot_rows_skipped: 0,
        dq_counters: DQ_COUNTERS.to_vec(),
        feed_gaps,
    })
}
