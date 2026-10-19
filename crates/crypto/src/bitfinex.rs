//! Bitfinex public WebSocket v2 capture (`book_R0`, `trades`, plus `book_P0` / `hb` for sequence checks; one line
//! `{"recv_ns": N, "chan": C, "symbol": S, "msg": <raw frame>}` each) -> MBO records, venue `generic`.
//! Port of et `sources/bitfinex.py::raw_book_to_mbo` and the `SEQ_ALL` continuity check of `verify_capture`.
//!
//! Frames: `[CHAN, payload]` (books), `[CHAN, "te"|"tu", payload]` (trades), `[CHAN, "hb"]`; with the capture's conf
//! flags a frame carries a trailing `SEQ` (`SEQ_ALL` 65536) and/or `TIMESTAMP` ms (32768), in that order.
//! Numbers are read from the frame text, so float-looking prices and amounts convert exactly or fail.
//! Mapping: R0 snapshot -> `R` then `A` per order (`SNAPSHOT`, `LAST` on the last); update with a new id -> `A`;
//! known id with PRICE > 0 -> `M` (absolute |AMOUNT|, PRICE; `price_changes` counts moves); PRICE = 0 -> `C` with the
//! tracked remaining. Side = sign of AMOUNT. `te` -> `T` with aggressor = sign of AMOUNT (the taker), order_id 0;
//! `tu` and the trades snapshot are ignored. R0 carries **no fill records**: the maker decrement arrives as `M`/`C`,
//! so fills cannot be attributed to resting orders (counter `no_execution_records` = 1; not scorable as truth).
//! `ts_event` = frame TIMESTAMP ms when present, else `recv_ns`; `T` uses the trade's own MTS. Lines are merged by
//! (ts_event, recv_ns), R0 lines before trades lines on ties; `sequence` counts emitted events only.
//! Sizes: our `Qty` is u64, so a 50 BTC order (5e9 lots at 1e-8) is one record. `max_size` (default u64::MAX)
//! reproduces et's u32 path: larger orders become exact consecutive children with ids from `child_id`
//! (`chunked_orders`; et's ids for the first 16 children, a disjoint id region after that, at most `MAX_CHILDREN`).
//! `SEQ_ALL` continuity (when the flag is set): every frame of every channel in receive order, reset at `_capture`
//! reconnect markers (`seq_all_checked`, `seq_all_gaps`).

use std::collections::HashMap;

use indexmap::IndexMap;
use qt_schema::rows::{TradeKind, TradeRow};
use qt_types::{MboEvent, QtError, QtResult, Side, UNDEF_PRICE, flags};

use crate::jsonx::{self, J};
use crate::{Counters, L3Output, units};

/// Conf flag: millisecond timestamp appended to each frame.
pub const TIMESTAMP: u64 = 32_768;
/// Conf flag: sequence number appended to each frame (beta).
pub const SEQ_ALL: u64 = 65_536;
/// Smallest accepted `max_size` (bounds the number of children per order).
pub const MIN_MAX_SIZE: u64 = 1 << 20;
/// Synthetic child ids live far above any Bitfinex order id (observed < 2^40).
pub const CHUNK_ID_BASE: u64 = 1 << 48;
/// Children `k >= 16` (et's id formula `CHUNK_ID_BASE + oid * 16 + k` only holds 16 per order) live in a second
/// region `CHUNK_ID_WIDE_BASE + oid * MAX_CHILDREN + k`, disjoint from the first (review m27).
pub const CHUNK_ID_WIDE_BASE: u64 = 1 << 62;
/// Most children per order (`QT-INPUT-002` beyond; with `MIN_MAX_SIZE` that is 2^40 size units).
pub const MAX_CHILDREN: u64 = 1 << 20;

/// Converter options.
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    /// Symbol (e.g. `tBTCUSD`) -> instrument id; empty = every symbol seen, ids 1.. in sorted symbol order.
    pub instruments: IndexMap<String, u32>,
    /// Price exponent (default -9).
    pub price_exp: i8,
    /// Size exponent (default -8).
    pub size_exp: i8,
    /// Capture conf flags (`capture_meta.json` `flags`).
    pub flags: u64,
    /// Largest size per record; larger orders are chunked (default u64::MAX = never).
    pub max_size: u64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            instruments: IndexMap::new(),
            price_exp: -9,
            size_exp: -8,
            flags: 0,
            max_size: u64::MAX,
        }
    }
}

/// Counter names reported even when zero (et `ANOMALIES` plus `chunked_orders`).
pub const ANOMALIES: [&str; 5] = [
    "delete_unknown",
    "price_changes",
    "snapshots_after_first",
    "unparsed",
    "chunked_orders",
];

/// Anomaly / adjustment counters copied (when non-zero) into `qt.data_quality.source_specific` at ingest.
pub const DQ_COUNTERS: [&str; 6] = [
    "delete_unknown",
    "price_changes",
    "snapshots_after_first",
    "unparsed",
    "chunked_orders",
    "no_execution_records",
];

/// Parts of a frame: (tag, payload, seq, ts_ms).
pub type FrameParts<'a> = (Option<&'a str>, Option<&'a J>, Option<i64>, Option<i64>);

/// `[CHAN, (tag,) (payload,) (seq,) (ts_ms)]` -> (tag, payload, seq, ts_ms); a non-frame gives all `None`.
pub fn split_frame(frame: &J, flags: u64) -> QtResult<FrameParts<'_>> {
    let Some(a) = frame.as_arr().filter(|a| a.len() >= 2) else {
        return Ok((None, None, None, None));
    };
    let tag = a[1].as_str();
    let mut i = if tag.is_some_and(|t| !t.is_empty()) { 2 } else { 1 };
    let mut payload = None;
    if tag != Some("hb") && i < a.len() && a[i].as_arr().is_some() {
        payload = Some(&a[i]);
        i += 1;
    }
    let mut trailing = &a[i..];
    let int = |v: &J| v.as_i64().ok_or_else(|| fmt_err(0, "frame trailer is not an integer"));
    let mut seq = None;
    if flags & SEQ_ALL != 0 && !trailing.is_empty() {
        seq = Some(int(&trailing[0])?);
        trailing = &trailing[1..];
    }
    let mut ts = None;
    if flags & TIMESTAMP != 0 && !trailing.is_empty() {
        ts = Some(int(&trailing[0])?);
    }
    Ok((tag, payload, seq, ts))
}

/// Splits an exact size into pieces of at most `max_size` (last piece carries the remainder).
pub fn chunk_sizes(sz: u64, max_size: u64) -> Vec<u64> {
    if sz <= max_size || max_size == 0 {
        return vec![sz];
    }
    let (n, rem) = (sz / max_size, sz % max_size);
    let mut v = vec![max_size; n as usize];
    if rem > 0 {
        v.push(rem);
    }
    v
}

/// Order id of child `k` of order `oid`: the first child keeps the order id; children 1..16 get et's ids
/// (`CHUNK_ID_BASE + oid * 16 + k`, below `CHUNK_ID_WIDE_BASE`); later ones `CHUNK_ID_WIDE_BASE + oid *
/// MAX_CHILDREN + k`. Ids are distinct for distinct `(oid, k)` (et's formula alone repeats from k = 16).
pub fn child_id(oid: u64, k: usize) -> QtResult<u64> {
    if k == 0 && oid < CHUNK_ID_BASE {
        return Ok(oid);
    }
    let k = k as u64;
    let id = if k < 16 {
        oid.checked_mul(16)
            .and_then(|x| x.checked_add(CHUNK_ID_BASE))
            .and_then(|x| x.checked_add(k))
            .filter(|x| *x < CHUNK_ID_WIDE_BASE)
    } else if k < MAX_CHILDREN {
        oid.checked_mul(MAX_CHILDREN)
            .and_then(|x| x.checked_add(CHUNK_ID_WIDE_BASE))
            .and_then(|x| x.checked_add(k))
    } else {
        None
    };
    id.ok_or_else(|| fmt_err(0, format!("child id out of range for order {oid}, child {k}")))
}

/// `chunk_sizes` with the `MAX_CHILDREN` bound checked before anything is allocated.
fn pieces_of(sz: u64, max_size: u64, line: u64) -> QtResult<Vec<u64>> {
    if max_size > 0 && sz / max_size >= MAX_CHILDREN {
        return Err(fmt_err(
            line,
            format!("size {sz} needs more than {MAX_CHILDREN} children of max_size {max_size}"),
        ));
    }
    Ok(chunk_sizes(sz, max_size))
}

fn fmt_err(line: u64, msg: impl Into<String>) -> QtError {
    QtError::InputFormat {
        location: format!("bitfinex line {line}"),
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

/// (ts_event, recv_ns, instrument, line, tag, payload) of one order-level frame.
type Item<'a> = (i64, i64, u32, &'a Line, Option<&'a str>, &'a J);

struct Line {
    no: u64,
    recv: i64,
    recv_known: bool,
    chan: String,
    symbol: Option<String>,
    msg: J,
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

fn text(v: &J, line: u64, what: &str) -> QtResult<String> {
    v.num_text()
        .map(str::to_string)
        .ok_or_else(|| fmt_err(line, format!("{what} is not a number")))
}

/// Full conversion (records, trades, counters, resolved instruments, ingest-level quality numbers).
pub(crate) fn convert_full(lines: impl Iterator<Item = QtResult<String>>, cfg: &Config) -> QtResult<L3Output> {
    let mut all: Vec<Line> = Vec::new();
    for (k, line) in lines.enumerate() {
        let no = k as u64 + 1;
        let t = line?;
        if t.trim().is_empty() {
            continue;
        }
        let mut v = jsonx::parse(&t).map_err(|e| fmt_err(no, e))?;
        let recv_opt = v.get("recv_ns").and_then(J::as_i64);
        let (recv, recv_known) = (recv_opt.unwrap_or(0), recv_opt.is_some());
        let chan = v.get("chan").and_then(J::as_str).unwrap_or("").to_string();
        let symbol = v.get("symbol").and_then(J::as_str).map(str::to_string);
        let msg = match &mut v {
            J::Obj(f) => f
                .iter()
                .position(|(k, _)| k == "msg")
                .map(|i| f.swap_remove(i).1)
                .unwrap_or(J::Null),
            _ => return Err(fmt_err(no, "line is not an object")),
        };
        all.push(Line {
            no,
            recv,
            recv_known,
            chan,
            symbol,
            msg,
        });
    }
    if cfg.max_size < MIN_MAX_SIZE {
        return Err(QtError::Config(format!("bitfinex max_size: must be >= {MIN_MAX_SIZE}")));
    }
    let fl = cfg.flags;
    let mut st = Counters::new();
    let bump = |st: &mut Counters, k: &str, by: u64| *st.entry(k.to_string()).or_insert(0) += by;

    // SEQ_ALL continuity over every stored frame in receive order (et verify_capture).
    let mut by_recv: Vec<&Line> = all.iter().collect();
    by_recv.sort_by_key(|l| l.recv);
    let (mut seq_checked, mut seq_gaps, mut last): (u64, u64, Option<i64>) = (0, 0, None);
    for l in &by_recv {
        if l.chan == "_capture" {
            last = None;
            continue;
        }
        let (tag, payload, seq, _) = split_frame(&l.msg, fl).map_err(|_| fmt_err(l.no, "bad frame trailer"))?;
        if tag.is_none()
            && payload
                .and_then(J::as_arr)
                .and_then(|p| p.first())
                .is_some_and(|x| x.as_arr().is_some())
        {
            bump(&mut st, &format!("snapshots_{}", l.chan), 1);
        }
        if let Some(s) = seq {
            seq_checked += 1;
            if last.is_some_and(|p| s != p + 1) {
                seq_gaps += 1;
            }
            last = Some(s);
        }
    }

    let instruments: IndexMap<String, u32> = if cfg.instruments.is_empty() {
        let mut syms: Vec<&String> = all
            .iter()
            .filter(|l| l.chan == "book_R0" || l.chan == "trades")
            .filter_map(|l| l.symbol.as_ref())
            .collect();
        syms.sort();
        syms.dedup();
        syms.into_iter()
            .enumerate()
            .map(|(i, s)| (s.clone(), i as u32 + 1))
            .collect()
    } else {
        cfg.instruments.clone()
    };

    // items: R0 lines first, then trades lines, sorted by (ts_event, recv_ns)
    let mut items: Vec<Item<'_>> = Vec::new();
    for chan in ["book_R0", "trades"] {
        for l in all.iter().filter(|l| l.chan == chan) {
            let Some(&inst) = l.symbol.as_ref().and_then(|s| instruments.get(s)) else {
                continue;
            };
            let (tag, payload, _, ts) = split_frame(&l.msg, fl).map_err(|_| fmt_err(l.no, "bad frame trailer"))?;
            let Some(payload) = payload else {
                if tag != Some("hb") {
                    bump(&mut st, "unparsed", 1);
                }
                continue;
            };
            let te = match ts {
                Some(ms) => units::scaled_ns(ms, 1_000_000, &format!("bitfinex line {}", l.no))?,
                None => l.recv,
            };
            items.push((te, l.recv, inst, l, tag, payload));
        }
    }
    items.sort_by_key(|it| (it.0, it.1));

    let (pe, se) = (cfg.price_exp, cfg.size_exp);
    let mut book: HashMap<u64, (u32, u8, i64, Vec<u64>)> = HashMap::new();
    let mut snapped: Vec<u32> = Vec::new();
    let mut rows: Vec<MboEvent> = Vec::new();
    let mut trades: Vec<TradeRow> = Vec::new();
    let mut seq: u64 = 0;
    bump(&mut st, "chunked_orders", 0);
    for (te, recv, inst, l, tag, payload) in items {
        let p = payload.as_arr().unwrap_or(&[]);
        let tr = if l.recv_known { recv } else { te };
        if l.chan == "trades" {
            if tag != Some("te") {
                bump(&mut st, "trades_ignored", 1);
                continue;
            }
            bump(&mut st, "te", 1);
            let [_, mts, amt, px, ..] = p else {
                return Err(fmt_err(l.no, "te payload is not [ID, MTS, AMOUNT, PRICE]"));
            };
            seq += 1;
            let (sign, sz) = units::signed_size(&text(amt, l.no, "AMOUNT")?, se)?;
            let pxi = units::price(&text(px, l.no, "PRICE")?, pe)?;
            let ms = mts.as_i64().ok_or_else(|| fmt_err(l.no, "MTS is not an integer"))?;
            let ts = units::scaled_ns(ms, 1_000_000, &format!("bitfinex line {}", l.no))?;
            let agg = if sign > 0 { b'B' } else { b'A' };
            let tr = if l.recv_known { recv } else { ts };
            rows.push(rec(ts, tr, inst, b'T', agg, pxi, sz, 0, flags::LAST, seq));
            trades.push(TradeRow {
                ts_event: ts,
                instrument_id: inst,
                sequence: seq,
                price: pxi,
                size: sz,
                aggressor_side: Side::from_byte(agg),
                kind: TradeKind::Displayed,
                maker_order_id: None,
            });
            continue;
        }
        if p.is_empty() {
            bump(&mut st, "unparsed", 1);
            continue;
        }
        if p[0].as_arr().is_some() {
            bump(&mut st, "snapshots", 1);
            if snapped.contains(&inst) {
                bump(&mut st, "snapshots_after_first", 1);
            } else {
                snapped.push(inst);
            }
            seq += 1;
            book.retain(|_, v| v.0 != inst);
            rows.push(rec(te, tr, inst, b'R', b'N', UNDEF_PRICE, 0, 0, 0, seq));
            let n_entries = p.len();
            for (k, e) in p.iter().enumerate() {
                let [oid, px, amt] = e.as_arr().unwrap_or(&[]) else {
                    return Err(fmt_err(l.no, "R0 entry is not [ORDER_ID, PRICE, AMOUNT]"));
                };
                let oid = oid
                    .as_u64()
                    .ok_or_else(|| fmt_err(l.no, "ORDER_ID is not an integer"))?;
                let (sign, sz) = units::signed_size(&text(amt, l.no, "AMOUNT")?, se)?;
                let pxi = units::price(&text(px, l.no, "PRICE")?, pe)?;
                let side = if sign > 0 { b'B' } else { b'A' };
                let pieces = pieces_of(sz, cfg.max_size, l.no)?;
                if pieces.len() > 1 {
                    bump(&mut st, "chunked_orders", 1);
                }
                for (j, piece) in pieces.iter().enumerate() {
                    let last = k + 1 == n_entries && j + 1 == pieces.len();
                    let f = flags::SNAPSHOT | if last { flags::LAST } else { 0 };
                    rows.push(rec(te, tr, inst, b'A', side, pxi, *piece, child_id(oid, j)?, f, seq));
                }
                book.insert(oid, (inst, side, pxi, pieces));
            }
            continue;
        }
        let [oid, px, amt, ..] = p else {
            return Err(fmt_err(l.no, "R0 update is not [ORDER_ID, PRICE, AMOUNT]"));
        };
        let oid = oid
            .as_u64()
            .ok_or_else(|| fmt_err(l.no, "ORDER_ID is not an integer"))?;
        let px_text = text(px, l.no, "PRICE")?;
        if units::is_zero(&px_text) {
            let Some((_, bside, bpx, pieces)) = book.remove(&oid) else {
                bump(&mut st, "delete_unknown", 1);
                continue;
            };
            bump(&mut st, "deletes", 1);
            seq += 1;
            for (j, piece) in pieces.iter().enumerate() {
                let f = if j + 1 == pieces.len() { flags::LAST } else { 0 };
                rows.push(rec(te, tr, inst, b'C', bside, bpx, *piece, child_id(oid, j)?, f, seq));
            }
            continue;
        }
        let (sign, sz) = units::signed_size(&text(amt, l.no, "AMOUNT")?, se)?;
        let pxi = units::price(&px_text, pe)?;
        let side = if sign > 0 { b'B' } else { b'A' };
        seq += 1;
        let pieces = pieces_of(sz, cfg.max_size, l.no)?;
        let mut evs: Vec<(u8, u64, u64)> = Vec::new();
        if let Some((_, _, old_px, old_pieces)) = book.get(&oid) {
            bump(&mut st, "updates", 1);
            if pxi != *old_px {
                bump(&mut st, "price_changes", 1);
            }
            for (j, piece) in pieces.iter().enumerate() {
                evs.push((
                    if j < old_pieces.len() { b'M' } else { b'A' },
                    *piece,
                    child_id(oid, j)?,
                ));
            }
            for (j, piece) in old_pieces.iter().enumerate().skip(pieces.len()) {
                evs.push((b'C', *piece, child_id(oid, j)?));
            }
        } else {
            bump(&mut st, "adds", 1);
            if pieces.len() > 1 {
                bump(&mut st, "chunked_orders", 1);
            }
            for (j, piece) in pieces.iter().enumerate() {
                evs.push((b'A', *piece, child_id(oid, j)?));
            }
        }
        let n = evs.len();
        for (j, (act, piece, cid)) in evs.into_iter().enumerate() {
            rows.push(rec(
                te,
                tr,
                inst,
                act,
                side,
                pxi,
                piece,
                cid,
                if j + 1 == n { flags::LAST } else { 0 },
                seq,
            ));
        }
        book.insert(oid, (inst, side, pxi, pieces));
    }

    let mut out = Counters::new();
    for k in ANOMALIES {
        out.insert(k.to_string(), 0);
    }
    out.extend(st);
    out.insert("records".into(), rows.len() as u64);
    out.insert("open_at_end".into(), book.len() as u64);
    out.insert("seq_all_checked".into(), seq_checked);
    out.insert("seq_all_gaps".into(), seq_gaps);
    out.insert("no_execution_records".into(), 1);
    out.insert("max_size_chunking".into(), u64::from(cfg.max_size < u64::MAX));
    Ok(L3Output {
        events: rows,
        trades,
        counters: out,
        instruments,
        sequence_gaps: seq_gaps,
        pre_snapshot_rows_skipped: 0,
        dq_counters: DQ_COUNTERS.to_vec(),
        feed_gaps: Vec::new(),
    })
}
