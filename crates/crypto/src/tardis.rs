//! Tardis.dev `incremental_book_L2` + `trades` CSV(.gz) -> `qt.l2` rows and source trades (L2-only datasets: usable
//! for zoo simulation and markouts, never as exact truth).
//!
//! Input: the lines of one or more CSV files, each starting with its header (`exchange,symbol,timestamp,
//! local_timestamp,...`); a header with `is_snapshot,side,price,amount` starts book rows, one with
//! `id,side,price,amount` trade rows. Timestamps are microseconds (`timestamp` from the exchange, falling back to
//! `local_timestamp` when empty). Rows are stably ordered by `local_timestamp` and grouped into one batch per
//! (instrument, `local_timestamp`) (Tardis: "grouping rows by the local_timestamp field" recovers messages).
//! Book semantics: `amount` is the new level size, not a delta; 0 removes the level. Non-snapshot rows before the
//! first snapshot of a symbol are skipped (`pre_snapshot_rows_skipped`). A batch containing snapshot rows (a run of
//! `is_snapshot = true` rows clears the book when it starts) emits a `clear` row and then one `snapshot` row per
//! level of the whole book after the batch (flag `SNAPSHOT`, bids then asks, best first); other batches emit one
//! `depth` row per touched level whose size changed (net over the batch, bids then asks, best first) with
//! `prev_qty` = the size before the batch. Trade rows come first in their batch (`trade_position = before`):
//! `aggressor` = the Tardis taker `side` (`buy` -> `B`, `sell` -> `A`, `unknown` -> `N`), `side` = the hit side
//! (opposite, `N` when unknown). Every row of a batch shares `event_id` (1-based per instrument); the last carries
//! `LAST_IN_BATCH`. `sequence` = 1-based position of the source row within its instrument's ordered rows (depth,
//! snapshot and clear rows: the batch's last book row). `level` = rank in the book after the batch (0 = best,
//! saturating at 254; 255 for removed levels, clears and trades). Counts are not available (`prev_ct`/`new_ct` null).

use std::collections::{BTreeMap, HashMap};

use indexmap::IndexMap;
use qt_schema::rows::{L2Kind, L2Row, TradeKind, TradeRow, l2flags};
use qt_types::{QtError, QtResult, Side};

use crate::{Counters, units};

/// Converter options.
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    /// Symbol -> instrument id; empty = every symbol seen, ids 1.. in sorted symbol order.
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

/// Anomaly counters copied (when non-zero) into `qt.data_quality.source_specific` at ingest.
pub const DQ_COUNTERS: [&str; 6] = [
    "remove_absent_level",
    "local_ts_regressions",
    "crossed_after_batch",
    "instruments_without_snapshot",
    "unknown_side_trades",
    "snapshot_zero_rows",
];

/// Deepest rank reported in `level` (deeper levels saturate here; 255 means out of view).
pub const MAX_LEVEL: u8 = 254;

/// Converts CSV lines to L2 rows, trades and counters.
pub fn convert(
    lines: impl Iterator<Item = QtResult<String>>,
    cfg: &Config,
) -> QtResult<(Vec<L2Row>, Vec<TradeRow>, Counters)> {
    let o = convert_full(lines, cfg)?;
    Ok((o.rows, o.trades, o.counters))
}

/// Full conversion output.
pub(crate) struct L2Output {
    pub rows: Vec<L2Row>,
    pub trades: Vec<TradeRow>,
    pub counters: Counters,
    pub instruments: IndexMap<String, u32>,
    pub exchange: Option<String>,
    pub pre_snapshot_rows_skipped: u64,
}

#[derive(Clone, Copy)]
enum Kind {
    Book { snap: bool, side: u8, qty: u64 },
    Trade { agg: u8, qty: u64 },
}

#[derive(Clone, Copy)]
struct In {
    sym: usize,
    ts: i64,
    lts: i64,
    px: i64,
    kind: Kind,
}

struct Header {
    book: bool,
    symbol: usize,
    ts: usize,
    lts: usize,
    snap: usize,
    side: usize,
    price: usize,
    amount: usize,
    exchange: usize,
}

fn fmt_err(line: u64, msg: impl Into<String>) -> QtError {
    QtError::InputFormat {
        location: format!("tardis line {line}"),
        msg: msg.into(),
    }
}

fn split(line: &str) -> Vec<&str> {
    line.split(',').map(|f| f.trim().trim_matches('"')).collect()
}

fn header(fields: &[&str], line: u64) -> QtResult<Header> {
    let idx = |name: &str| fields.iter().position(|f| *f == name);
    let need = |name: &str| idx(name).ok_or_else(|| fmt_err(line, format!("header has no {name} column")));
    let book = idx("is_snapshot").is_some();
    if !book && idx("id").is_none() {
        return Err(fmt_err(
            line,
            "header is neither incremental_book_L2 (is_snapshot) nor trades (id)",
        ));
    }
    Ok(Header {
        book,
        symbol: need("symbol")?,
        ts: need("timestamp")?,
        lts: need("local_timestamp")?,
        snap: idx("is_snapshot").unwrap_or(usize::MAX),
        side: need("side")?,
        price: need("price")?,
        amount: need("amount")?,
        exchange: need("exchange")?,
    })
}

fn us(field: &str, line: u64, what: &str) -> QtResult<i64> {
    let v: i64 = field
        .parse()
        .map_err(|_| fmt_err(line, format!("{what} {field:?} is not integer microseconds")))?;
    units::scaled_ns(v, 1_000, &format!("tardis line {line}"))
}

/// Full conversion (rows, trades, counters, resolved instruments).
pub(crate) fn convert_full(lines: impl Iterator<Item = QtResult<String>>, cfg: &Config) -> QtResult<L2Output> {
    let (pe, se) = (cfg.price_exp, cfg.size_exp);
    let mut st = Counters::new();
    let bump = |st: &mut Counters, k: &str, by: u64| *st.entry(k.to_string()).or_insert(0) += by;
    let mut syms: Vec<String> = Vec::new();
    let mut sym_ix: HashMap<String, usize> = HashMap::new();
    let mut input: Vec<In> = Vec::new();
    let mut hdr: Option<Header> = None;
    let mut exchange: Option<String> = None;
    let mut last_lts: Option<i64> = None;
    for (k, line) in lines.enumerate() {
        let no = k as u64 + 1;
        let text = line?;
        if text.trim().is_empty() {
            continue;
        }
        let f = split(&text);
        if f.first() == Some(&"exchange") {
            hdr = Some(header(&f, no)?);
            last_lts = None;
            continue;
        }
        let Some(h) = &hdr else {
            return Err(fmt_err(no, "data row before any CSV header"));
        };
        let get = |i: usize, what: &str| f.get(i).copied().ok_or_else(|| fmt_err(no, format!("missing {what}")));
        let sym = get(h.symbol, "symbol")?;
        if !cfg.instruments.is_empty() && !cfg.instruments.contains_key(sym) {
            continue;
        }
        if exchange.is_none() {
            exchange = Some(get(h.exchange, "exchange")?.to_string());
        }
        let lts = us(get(h.lts, "local_timestamp")?, no, "local_timestamp")?;
        let ts_f = get(h.ts, "timestamp")?;
        let ts = if ts_f.is_empty() {
            lts
        } else {
            us(ts_f, no, "timestamp")?
        };
        if last_lts.is_some_and(|p| lts < p) {
            bump(&mut st, "local_ts_regressions", 1);
        }
        last_lts = Some(lts);
        let px = units::price(get(h.price, "price")?, pe)?;
        let qty = units::size(get(h.amount, "amount")?, se)?;
        let side = get(h.side, "side")?;
        let kind = if h.book {
            bump(&mut st, "book_rows", 1);
            let snap = match get(h.snap, "is_snapshot")? {
                "true" | "True" | "TRUE" | "1" => true,
                "false" | "False" | "FALSE" | "0" => false,
                other => return Err(fmt_err(no, format!("is_snapshot {other:?} is not a boolean"))),
            };
            let side = match side {
                "bid" | "buy" => b'B',
                "ask" | "sell" => b'A',
                other => return Err(fmt_err(no, format!("book side {other:?} is not bid/ask"))),
            };
            Kind::Book { snap, side, qty }
        } else {
            bump(&mut st, "trade_rows", 1);
            let agg = match side {
                "buy" => b'B',
                "sell" => b'A',
                _ => {
                    bump(&mut st, "unknown_side_trades", 1);
                    b'N'
                }
            };
            Kind::Trade { agg, qty }
        };
        let si = match sym_ix.get(sym) {
            Some(&i) => i,
            None => {
                syms.push(sym.to_string());
                sym_ix.insert(sym.to_string(), syms.len() - 1);
                syms.len() - 1
            }
        };
        input.push(In {
            sym: si,
            ts,
            lts,
            px,
            kind,
        });
    }

    let instruments: IndexMap<String, u32> = if cfg.instruments.is_empty() {
        let mut v: Vec<&String> = syms.iter().collect();
        v.sort();
        v.into_iter()
            .enumerate()
            .map(|(i, s)| (s.clone(), i as u32 + 1))
            .collect()
    } else {
        cfg.instruments.clone()
    };
    let inst_of: Vec<u32> = syms.iter().map(|s| instruments.get(s).copied().unwrap_or(0)).collect();
    // stable: trades before book rows at the same (local_timestamp, instrument)
    input.sort_by_key(|r| (r.lts, inst_of[r.sym], matches!(r.kind, Kind::Book { .. })));

    #[derive(Default)]
    struct State {
        bids: BTreeMap<i64, u64>,
        asks: BTreeMap<i64, u64>,
        seen_snapshot: bool,
        in_snapshot: bool,
        event_id: u64,
        row_seq: u64,
    }
    let mut states: BTreeMap<u32, State> = BTreeMap::new();
    let mut rows: Vec<L2Row> = Vec::new();
    let mut trades: Vec<TradeRow> = Vec::new();
    let (mut pre_skipped, mut crossed) = (0u64, 0u64);
    let mut i = 0;
    while i < input.len() {
        let (lts, inst) = (input[i].lts, inst_of[input[i].sym]);
        let mut j = i;
        while j < input.len() && input[j].lts == lts && inst_of[input[j].sym] == inst {
            j += 1;
        }
        let batch = &input[i..j];
        i = j;
        let s = states.entry(inst).or_default();
        let mut out: Vec<L2Row> = Vec::new();
        let mut touched: BTreeMap<(u8, i64), u64> = BTreeMap::new();
        let (mut snapshot_batch, mut last_book) = (false, None::<(i64, u64)>);
        let base = L2Row {
            ts_event: 0,
            ts_recv: lts,
            instrument_id: inst,
            event_id: 0,
            sequence: 0,
            kind: L2Kind::Depth,
            side: Side::None,
            price: None,
            prev_qty: 0,
            new_qty: 0,
            prev_ct: None,
            new_ct: None,
            trade_qty: 0,
            aggressor: Side::None,
            level: 255,
            flags: 0,
        };
        for r in batch {
            s.row_seq += 1;
            match r.kind {
                Kind::Trade { agg, qty } => {
                    let aggressor = Side::from_byte(agg);
                    out.push(L2Row {
                        ts_event: r.ts,
                        sequence: s.row_seq,
                        kind: L2Kind::Trade,
                        side: aggressor.opposite(),
                        price: Some(r.px),
                        trade_qty: qty,
                        aggressor,
                        ..base
                    });
                    trades.push(TradeRow {
                        ts_event: r.ts,
                        instrument_id: inst,
                        sequence: s.row_seq,
                        price: r.px,
                        size: qty,
                        aggressor_side: aggressor,
                        kind: TradeKind::Displayed,
                        maker_order_id: None,
                    });
                }
                Kind::Book { snap, side, qty } => {
                    if snap {
                        if !s.in_snapshot {
                            s.bids.clear();
                            s.asks.clear();
                            touched.clear();
                            s.in_snapshot = true;
                            s.seen_snapshot = true;
                            bump(&mut st, "snapshots", 1);
                        }
                        snapshot_batch = true;
                        last_book = Some((r.ts, s.row_seq));
                        let book = if side == b'B' { &mut s.bids } else { &mut s.asks };
                        if qty == 0 {
                            bump(&mut st, "snapshot_zero_rows", 1);
                            book.remove(&r.px);
                        } else {
                            book.insert(r.px, qty);
                        }
                        continue;
                    }
                    if !s.seen_snapshot {
                        pre_skipped += 1;
                        continue;
                    }
                    s.in_snapshot = false;
                    last_book = Some((r.ts, s.row_seq));
                    let book = if side == b'B' { &mut s.bids } else { &mut s.asks };
                    let prev = book.get(&r.px).copied().unwrap_or(0);
                    touched.entry((side, r.px)).or_insert(prev);
                    if qty == 0 {
                        if book.remove(&r.px).is_none() {
                            bump(&mut st, "remove_absent_level", 1);
                        }
                    } else {
                        book.insert(r.px, qty);
                    }
                }
            }
        }
        let rank = |s: &State, side: u8, px: i64| -> u8 {
            let n = if side == b'B' {
                s.bids
                    .range(px.saturating_add(1)..)
                    .take(usize::from(MAX_LEVEL))
                    .count()
            } else {
                s.asks.range(..px).take(usize::from(MAX_LEVEL)).count()
            };
            u8::try_from(n).unwrap_or(MAX_LEVEL)
        };
        if let Some((ts_book, seq_book)) = last_book {
            let depth = L2Row {
                ts_event: ts_book,
                sequence: seq_book,
                ..base
            };
            if snapshot_batch {
                out.push(L2Row {
                    kind: L2Kind::Clear,
                    ..depth
                });
                for (side, levels) in [
                    (b'B', s.bids.iter().rev().collect::<Vec<_>>()),
                    (b'A', s.asks.iter().collect()),
                ] {
                    for (k, (&px, &q)) in levels.into_iter().enumerate() {
                        out.push(L2Row {
                            kind: L2Kind::Snapshot,
                            side: Side::from_byte(side),
                            price: Some(px),
                            new_qty: q,
                            level: u8::try_from(k).unwrap_or(MAX_LEVEL).min(MAX_LEVEL),
                            flags: l2flags::SNAPSHOT,
                            ..depth
                        });
                    }
                }
            } else {
                let bids = touched.iter().filter(|((sd, _), _)| *sd == b'B').rev();
                let asks = touched.iter().filter(|((sd, _), _)| *sd == b'A');
                for (&(side, px), &prev) in bids.chain(asks) {
                    let book = if side == b'B' { &s.bids } else { &s.asks };
                    let new = book.get(&px).copied().unwrap_or(0);
                    if new == prev {
                        continue;
                    }
                    out.push(L2Row {
                        side: Side::from_byte(side),
                        price: Some(px),
                        prev_qty: prev,
                        new_qty: new,
                        level: if new > 0 { rank(s, side, px) } else { 255 },
                        ..depth
                    });
                }
            }
        }
        if out.is_empty() {
            continue;
        }
        s.event_id += 1;
        let n = out.len();
        for (k, mut r) in out.into_iter().enumerate() {
            r.event_id = s.event_id;
            if k + 1 == n {
                r.flags |= l2flags::LAST_IN_BATCH;
            }
            rows.push(r);
        }
        if let (Some((&b, _)), Some((&a, _))) = (s.bids.iter().next_back(), s.asks.iter().next())
            && b >= a
        {
            crossed += 1;
        }
    }
    let without_snapshot = states.values().filter(|s| !s.seen_snapshot).count() as u64
        + instruments.values().filter(|id| !states.contains_key(id)).count() as u64;
    let batches: u64 = states.values().map(|s| s.event_id).sum();
    let mut counters = Counters::new();
    for k in ["book_rows", "trade_rows", "snapshots"] {
        counters.insert(k.into(), 0);
    }
    for k in DQ_COUNTERS {
        counters.insert(k.into(), 0);
    }
    counters.extend(st);
    counters.insert("pre_snapshot_rows_skipped".into(), pre_skipped);
    counters.insert("crossed_after_batch".into(), crossed);
    counters.insert("instruments_without_snapshot".into(), without_snapshot);
    counters.insert("batches".into(), batches);
    counters.insert("l2_rows".into(), rows.len() as u64);
    Ok(L2Output {
        rows,
        trades,
        counters,
        instruments,
        exchange,
        pre_snapshot_rows_skipped: pre_skipped,
    })
}
