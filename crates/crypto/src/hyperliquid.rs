//! Hyperliquid self-run node outputs -> MBO records (L4: every resting order with its on-chain id), rule set
//! `crypto_l3`. Sources (accessed 2026-10-19): node README <https://github.com/hyperliquid-dex/node> (flags
//! `--write-raw-book-diffs`, `--write-order-statuses`, `--write-fills`, `--batch-by-block`,
//! `--stream-with-block-info`, `compute-l4-snapshots`), L1 data schemas
//! <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/l1-data-schemas>, order status values
//! <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint> and the `WsFill` fields
//! <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions>.
//!
//! Inputs (every line of every file, classified by shape, so file names and order do not matter):
//! - `node_raw_book_diffs` (required): `{"user", "oid", "coin", "side": "Bid"|"Ask", "px", "raw_book_diff":
//!   {"new": {"sz"}} | {"update": {"origSz", "newSz"}} | "remove"}`. These lines carry no time, so they must be
//!   written with block info (`--batch-by-block` or `--stream-with-block-info`): `{"local_time", "block_time",
//!   "block_number", "events": [...]}`; a bare diff line is `QT-INPUT-002`.
//! - `node_fills` (optional, API fills format; bare `[user, fill]` / fill object or block-wrapped): `oid`, `coin`,
//!   `px`, `sz`, `side`, `time` (ms), `crossed` (taker), `tid` (shared by the two sides of a trade).
//! - `node_order_statuses` (optional): `{"time", "user", "status", "order": {"oid", ...}}`; used to classify a
//!   removal not explained by fills (canceled family vs filled).
//! - L4 snapshots from `compute-l4-snapshots` (`Config::snapshots`, with the block height they were taken at):
//!   `[[coin, [[bids...], [asks...]]], ...]`, orders "sorted in time-order at the same price level", trigger orders
//!   (`isTrigger`) last and skipped (not on the book).
//!
//! Book building: diffs are put in block order (input order within a block). A snapshot at height H is applied
//! before the first diff of a block > H (assumed: the state after block H) as `R` + `A` (`SNAPSHOT`, snapshot order =
//! queue order) per configured coin it holds; diffs of blocks <= H of a coin waiting for its first snapshot are
//! superseded (`superseded_by_snapshot`); a later snapshot re-seeds (`resnapshots`). `new` -> `A`; `update` to a
//! smaller size -> the maker's fills (`node_fills`, matched by `oid` in fill order) give `T` (taker oid and side from
//! the `crossed` fill with the same `tid`) + `F` + `C` per fill, any remainder `M` (`unattributed_decreases`; an
//! in-place decrease on Hyperliquid is a fill); a larger size -> `M` (`size_increases`); `remove` -> the maker's
//! fills as above, then `C` of the remainder (`removed_canceled` / `removed_filled_without_fills` /
//! `removed_unclassified` by the order status). Order ids are the venue's `oid`s; `sequence` = block number;
//! `ts_event` = `block_time`, `ts_recv` = `local_time`. User addresses are dropped.
//! Exactness: with a snapshot, the book is the full on-chain book, order by order and in queue order (a Hyperliquid
//! modify is a new order id). Without a snapshot a coin is warm-up only (`unseeded_instruments`, whole-range feed
//! gap). Block numbers are not checked for continuity (whether empty blocks are written is not documented);
//! instead a `block_time` jump above `Config::max_block_gap_ms` between consecutive diff blocks is a feed gap
//! (`block_time_gaps`), and unknown order references, size mismatches and unmatched fills are counted.

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};

use indexmap::IndexMap;
use qt_schema::json::FeedGap;
use qt_schema::rows::{TradeKind, TradeRow};
use qt_types::{MboEvent, QtError, QtResult, Side, flags};

use crate::jsonx::{self, J};
use crate::l3util::{self, rec};
use crate::{Counters, L3Output, units};

/// Converter options.
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    /// Coin (e.g. `BTC`, `@107`) -> instrument id; empty = every coin with diffs, ids 1.. in sorted order.
    pub instruments: IndexMap<String, u32>,
    /// Price exponent (default -9).
    pub price_exp: i8,
    /// Size exponent (default -8).
    pub size_exp: i8,
    /// L4 snapshots: (block height, file text).
    pub snapshots: Vec<(u64, String)>,
    /// Largest `block_time` step between consecutive diff blocks before a feed gap (ms; 0 = no check).
    pub max_block_gap_ms: u64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            instruments: IndexMap::new(),
            price_exp: -9,
            size_exp: -8,
            snapshots: Vec::new(),
            max_block_gap_ms: 5_000,
        }
    }
}

/// Counter names reported even when zero.
pub const ANOMALIES: [&str; 10] = [
    "unseeded_instruments",
    "diff_unknown_order",
    "duplicate_new",
    "update_orig_mismatch",
    "unattributed_decreases",
    "fill_size_mismatch",
    "fill_price_mismatch",
    "fills_unmatched",
    "removed_filled_without_fills",
    "block_time_gaps",
];

/// Anomaly / caveat counters copied (when non-zero) into `qt.data_quality.source_specific` at ingest.
pub const DQ_COUNTERS: [&str; 11] = [
    "unseeded_instruments",
    "diff_unknown_order",
    "duplicate_new",
    "update_orig_mismatch",
    "unattributed_decreases",
    "fill_size_mismatch",
    "fill_price_mismatch",
    "fills_unmatched",
    "removed_filled_without_fills",
    "block_time_gaps",
    "no_execution_records",
];

fn fmt_err(line: u64, msg: impl Into<String>) -> QtError {
    QtError::InputFormat {
        location: format!("hyperliquid_node line {line}"),
        msg: msg.into(),
    }
}

/// Converts node output lines to MBO records, source trades and counters.
pub fn convert(
    lines: impl Iterator<Item = QtResult<String>>,
    cfg: &Config,
) -> QtResult<(Vec<MboEvent>, Vec<TradeRow>, Counters)> {
    let o = convert_full(lines, cfg)?;
    Ok((o.events, o.trades, o.counters))
}

enum DiffKind {
    New(u64),
    Update(u64, u64),
    Remove,
}

struct Diff {
    block: u64,
    ts: i64,
    recv: i64,
    coin: String,
    oid: u64,
    side: u8,
    px: i64,
    kind: DiffKind,
}

struct Fill {
    coin: String,
    px: i64,
    sz: u64,
    tid: u64,
}

/// Block context of a wrapped line.
#[derive(Clone, Copy)]
struct Block {
    number: u64,
    ts: i64,
    recv: i64,
}

fn side_of(v: Option<&J>) -> Option<u8> {
    match v.and_then(J::as_str)? {
        "Bid" | "B" => Some(b'B'),
        "Ask" | "A" => Some(b'A'),
        _ => None,
    }
}

const CANCEL_STATUSES: [&str; 1] = ["scheduledCancel"];

fn is_cancel_status(s: &str) -> bool {
    s.ends_with("anceled") || CANCEL_STATUSES.contains(&s)
}

struct Parsed {
    diffs: Vec<Diff>,
    makers: HashMap<u64, VecDeque<Fill>>,
    takers: HashMap<(String, u64), (u64, u8)>,
    statuses: HashMap<u64, String>,
    fills: u64,
}

fn parse_event(e: &J, block: Option<Block>, no: u64, cfg: &Config, p: &mut Parsed, st: &mut Counters) -> QtResult<()> {
    let bump = |st: &mut Counters, k: &str| *st.entry(k.to_string()).or_insert(0) += 1;
    let (pe, se) = (cfg.price_exp, cfg.size_exp);
    if let Some(d) = e.get("raw_book_diff") {
        let b = block.ok_or_else(|| {
            fmt_err(
                no,
                "raw book diff without block info: run the node with --batch-by-block or --stream-with-block-info",
            )
        })?;
        let coin = e
            .get("coin")
            .and_then(J::as_str)
            .ok_or_else(|| fmt_err(no, "diff without coin"))?
            .to_string();
        let oid = e
            .get("oid")
            .and_then(J::as_u64)
            .ok_or_else(|| fmt_err(no, "diff without integer oid"))?;
        let side = side_of(e.get("side")).ok_or_else(|| fmt_err(no, "diff side must be Bid or Ask"))?;
        let px = units::price(
            e.get("px")
                .and_then(J::num_text)
                .ok_or_else(|| fmt_err(no, "diff without px"))?,
            pe,
        )?;
        let num = |v: Option<&J>, what: &str| -> QtResult<u64> {
            units::size(
                v.and_then(J::num_text)
                    .ok_or_else(|| fmt_err(no, format!("diff without {what}")))?,
                se,
            )
        };
        let kind = if d.as_str() == Some("remove") {
            DiffKind::Remove
        } else if let Some(n) = d.get("new") {
            DiffKind::New(num(n.get("sz"), "new.sz")?)
        } else if let Some(u) = d.get("update") {
            DiffKind::Update(
                num(u.get("origSz"), "update.origSz")?,
                num(u.get("newSz"), "update.newSz")?,
            )
        } else {
            return Err(fmt_err(no, "raw_book_diff is not new / update / remove"));
        };
        bump(st, "diffs");
        p.diffs.push(Diff {
            block: b.number,
            ts: b.ts,
            recv: b.recv,
            coin,
            oid,
            side,
            px,
            kind,
        });
        return Ok(());
    }
    if let (Some(s), Some(o)) = (e.get("status").and_then(J::as_str), e.get("order")) {
        bump(st, "order_statuses");
        if let Some(oid) = o.get("oid").and_then(J::as_u64) {
            if s != "open" && s != "triggered" {
                p.statuses.insert(oid, s.to_string());
            }
        }
        return Ok(());
    }
    // fills: `[user, fill]` or a fill object
    let f = match e.as_arr() {
        Some([_, f]) if f.get("tid").is_some() => f,
        _ if e.get("tid").is_some() => e,
        _ => {
            bump(st, "ignored");
            return Ok(());
        }
    };
    let coin = f
        .get("coin")
        .and_then(J::as_str)
        .ok_or_else(|| fmt_err(no, "fill without coin"))?
        .to_string();
    let (Some(oid), Some(tid)) = (f.get("oid").and_then(J::as_u64), f.get("tid").and_then(J::as_u64)) else {
        return Err(fmt_err(no, "fill without integer oid / tid"));
    };
    let side = side_of(f.get("side")).ok_or_else(|| fmt_err(no, "fill side must be B or A"))?;
    let crossed = match f.get("crossed") {
        Some(J::Bool(b)) => *b,
        _ => return Err(fmt_err(no, "fill without boolean crossed")),
    };
    let px = units::price(
        f.get("px")
            .and_then(J::num_text)
            .ok_or_else(|| fmt_err(no, "fill without px"))?,
        pe,
    )?;
    let sz = units::size(
        f.get("sz")
            .and_then(J::num_text)
            .ok_or_else(|| fmt_err(no, "fill without sz"))?,
        se,
    )?;
    p.fills += 1;
    if crossed {
        p.takers.insert((coin, tid), (oid, side));
    } else {
        p.makers.entry(oid).or_default().push_back(Fill { coin, px, sz, tid });
    }
    Ok(())
}

/// Snapshot orders per coin: (side, px, sz, oid) in queue order (bids then asks).
type SnapBook = HashMap<String, Vec<(u8, i64, u64, u64)>>;

fn parse_snapshot(text: &str, cfg: &Config, st: &mut Counters) -> QtResult<SnapBook> {
    let err = |m: String| QtError::InputFormat {
        location: "hyperliquid l4 snapshot".into(),
        msg: m,
    };
    let v = jsonx::parse(text).map_err(err)?;
    let mut out = SnapBook::new();
    for entry in v
        .as_arr()
        .ok_or_else(|| err("not a JSON array of [coin, [bids, asks]]".into()))?
    {
        let (Some(coin), Some(sides)) = (
            entry.as_arr().and_then(|a| a.first()).and_then(J::as_str),
            entry.as_arr().and_then(|a| a.get(1)).and_then(J::as_arr),
        ) else {
            return Err(err("entry is not [coin, [bids, asks]]".into()));
        };
        if !cfg.instruments.is_empty() && !cfg.instruments.contains_key(coin) {
            continue;
        }
        let mut orders = Vec::new();
        for (k, side) in [(0usize, b'B'), (1, b'A')] {
            for o in sides.get(k).and_then(J::as_arr).unwrap_or(&[]) {
                if matches!(o.get("isTrigger"), Some(J::Bool(true))) {
                    *st.entry("snapshot_trigger_orders_skipped".into()).or_insert(0) += 1;
                    continue;
                }
                let (Some(px), Some(sz), Some(oid)) = (
                    o.get("limitPx").and_then(J::num_text),
                    o.get("sz").and_then(J::num_text),
                    o.get("oid").and_then(J::as_u64),
                ) else {
                    return Err(err(format!("{coin}: order without limitPx / sz / oid")));
                };
                let (px, sz) = (units::price(px, cfg.price_exp)?, units::size(sz, cfg.size_exp)?);
                if sz > 0 {
                    orders.push((side, px, sz, oid));
                }
            }
        }
        out.insert(coin.to_string(), orders);
    }
    Ok(out)
}

/// Per-coin book.
#[derive(Default)]
struct CoinBook {
    seeded: bool,
    orders: HashMap<u64, (u8, i64, u64)>,
    first_ts: Option<i64>,
    last_ts: Option<i64>,
}

/// Full conversion (records, trades, counters, resolved instruments, ingest-level quality numbers).
pub fn convert_full(lines: impl Iterator<Item = QtResult<String>>, cfg: &Config) -> QtResult<L3Output> {
    let mut st = Counters::new();
    let bump = |st: &mut Counters, k: &str| *st.entry(k.to_string()).or_insert(0) += 1;
    let mut p = Parsed {
        diffs: Vec::new(),
        makers: HashMap::new(),
        takers: HashMap::new(),
        statuses: HashMap::new(),
        fills: 0,
    };
    let mut seen_fills: HashSet<(u64, u64)> = HashSet::new();
    let mut last_block: Option<u64> = None;
    for (k, line) in lines.enumerate() {
        let no = k as u64 + 1;
        let t = line?;
        if t.trim().is_empty() {
            continue;
        }
        let v = jsonx::parse(&t).map_err(|e| fmt_err(no, e))?;
        if let (Some(evs), Some(number)) = (
            v.get("events").and_then(J::as_arr),
            v.get("block_number").and_then(J::as_u64),
        ) {
            let ts = l3util::iso_utc(
                v.get("block_time")
                    .and_then(J::as_str)
                    .ok_or_else(|| fmt_err(no, "block line without block_time"))?,
            )
            .map_err(|_| fmt_err(no, "bad block_time"))?;
            let recv = match v.get("local_time").and_then(J::as_str) {
                Some(s) => l3util::iso_utc(s).map_err(|_| fmt_err(no, "bad local_time"))?,
                None => ts,
            };
            if last_block.is_some_and(|b| number < b) {
                bump(&mut st, "block_number_regressions");
            }
            last_block = Some(number);
            bump(&mut st, "blocks");
            let b = Block { number, ts, recv };
            for e in evs {
                // de-duplicate fills seen twice (overlapping files)
                if let Some(key) = fill_key(e) {
                    if !seen_fills.insert(key) {
                        bump(&mut st, "duplicate_fills_dropped");
                        continue;
                    }
                }
                parse_event(e, Some(b), no, cfg, &mut p, &mut st)?;
            }
        } else {
            if let Some(key) = fill_key(&v) {
                if !seen_fills.insert(key) {
                    bump(&mut st, "duplicate_fills_dropped");
                    continue;
                }
            }
            parse_event(&v, None, no, cfg, &mut p, &mut st)?;
        }
    }
    p.diffs.sort_by_key(|d| d.block);

    let instruments: IndexMap<String, u32> = if cfg.instruments.is_empty() {
        let mut coins: Vec<&str> = p.diffs.iter().map(|d| d.coin.as_str()).collect();
        coins.sort_unstable();
        coins.dedup();
        coins
            .into_iter()
            .enumerate()
            .map(|(i, c)| (c.to_string(), i as u32 + 1))
            .collect()
    } else {
        cfg.instruments.clone()
    };
    let mut snaps: Vec<(u64, SnapBook)> = Vec::new();
    let scfg = Config {
        instruments: instruments.clone(),
        ..cfg.clone()
    };
    for (h, text) in &cfg.snapshots {
        snaps.push((*h, parse_snapshot(text, &scfg, &mut st)?));
    }
    snaps.sort_by_key(|(h, _)| *h);
    let has_snapshot: HashSet<String> = snaps.iter().flat_map(|(_, b)| b.keys().cloned()).collect();

    let mut books: HashMap<String, CoinBook> = HashMap::new();
    let mut out: Vec<MboEvent> = Vec::new();
    let mut trades: Vec<TradeRow> = Vec::new();
    let mut feed_gaps: Vec<FeedGap> = Vec::new();
    let mut next_snap = 0usize;
    let mut prev_block: Option<(u64, i64)> = None;
    let gap_ns = i64::try_from(cfg.max_block_gap_ms)
        .unwrap_or(i64::MAX / 1_000_000)
        .saturating_mul(1_000_000);
    let mut time_gaps: Vec<(i64, i64)> = Vec::new();
    for d in &p.diffs {
        if prev_block.is_none_or(|(b, _)| b != d.block) {
            if let Some((_, pts)) = prev_block {
                if gap_ns > 0 && d.ts - pts > gap_ns {
                    time_gaps.push((pts, d.ts));
                }
            }
            prev_block = Some((d.block, d.ts));
        }
        // snapshots taken at heights below this block
        while next_snap < snaps.len() && snaps[next_snap].0 < d.block {
            let (h, sb) = &snaps[next_snap];
            next_snap += 1;
            bump(&mut st, "snapshots");
            for (coin, &inst) in &instruments {
                let Some(orders) = sb.get(coin) else { continue };
                let b = books.entry(coin.clone()).or_default();
                if b.seeded {
                    bump(&mut st, "resnapshots");
                }
                b.seeded = true;
                b.orders.clear();
                let start = out.len();
                out.push(rec(d.ts, d.recv, inst, b'R', b'N', 0, 0, 0, flags::SNAPSHOT, *h));
                for &(side, px, sz, oid) in orders {
                    if b.orders.contains_key(&oid) {
                        bump(&mut st, "snapshot_duplicate_orders");
                        continue;
                    }
                    b.orders.insert(oid, (side, px, sz));
                    out.push(rec(d.ts, d.recv, inst, b'A', side, px, sz, oid, flags::SNAPSHOT, *h));
                }
                l3util::mark_last(&mut out, start);
                b.first_ts.get_or_insert(d.ts);
                b.last_ts = Some(d.ts);
            }
        }
        let Some(&inst) = instruments.get(&d.coin) else {
            bump(&mut st, "ignored_diffs");
            continue;
        };
        let b = books.entry(d.coin.clone()).or_default();
        if has_snapshot.contains(&d.coin) && !b.seeded {
            bump(&mut st, "superseded_by_snapshot");
            continue;
        }
        b.first_ts.get_or_insert(d.ts);
        b.last_ts = Some(d.ts);
        let (ts, recv, seq) = (d.ts, d.recv, d.block);
        let start = out.len();
        // consumes the maker's fills against `amount`; returns the size consumed
        let mut fills_for = |out: &mut Vec<MboEvent>,
                             trades: &mut Vec<TradeRow>,
                             st: &mut Counters,
                             side: u8,
                             opx: i64,
                             amount: u64|
         -> u64 {
            let mut left = amount;
            while left > 0 {
                let Some(q) = p.makers.get_mut(&d.oid) else { break };
                let Some(mut f) = q.pop_front() else { break };
                if f.coin != d.coin {
                    continue;
                }
                let take = f.sz.min(left);
                if f.sz > left {
                    *st.entry("fill_size_mismatch".into()).or_insert(0) += 1;
                    f.sz -= left;
                    let tid = f.tid;
                    q.push_front(Fill {
                        coin: f.coin.clone(),
                        px: f.px,
                        sz: f.sz,
                        tid,
                    });
                }
                if f.px != opx {
                    *st.entry("fill_price_mismatch".into()).or_insert(0) += 1;
                }
                let (tk, agg) = match p.takers.get(&(d.coin.clone(), f.tid)) {
                    Some(&(o, s)) => (o, s),
                    None => {
                        *st.entry("taker_fill_missing".into()).or_insert(0) += 1;
                        (0, if side == b'B' { b'A' } else { b'B' })
                    }
                };
                out.push(rec(ts, recv, inst, b'T', agg, f.px, take, tk, 0, seq));
                out.push(rec(ts, recv, inst, b'F', side, f.px, take, d.oid, 0, seq));
                out.push(rec(ts, recv, inst, b'C', side, opx, take, d.oid, 0, seq));
                trades.push(TradeRow {
                    ts_event: ts,
                    instrument_id: inst,
                    sequence: seq,
                    price: f.px,
                    size: take,
                    aggressor_side: Side::from_byte(agg),
                    kind: TradeKind::Displayed,
                    maker_order_id: Some(d.oid),
                });
                left -= take;
            }
            amount - left
        };
        match d.kind {
            DiffKind::New(sz) => {
                bump(&mut st, "new");
                if b.orders.insert(d.oid, (d.side, d.px, sz)).is_some() {
                    bump(&mut st, "duplicate_new");
                    out.push(rec(ts, recv, inst, b'M', d.side, d.px, sz, d.oid, 0, seq));
                } else {
                    out.push(rec(ts, recv, inst, b'A', d.side, d.px, sz, d.oid, 0, seq));
                }
            }
            DiffKind::Update(orig, new) => {
                bump(&mut st, "update");
                let Some(&(side, opx, cur)) = b.orders.get(&d.oid) else {
                    bump(&mut st, "diff_unknown_order");
                    continue;
                };
                if cur != orig {
                    bump(&mut st, "update_orig_mismatch");
                }
                if new < cur {
                    let dec = cur - new;
                    let got = fills_for(&mut out, &mut trades, &mut st, side, opx, dec);
                    if got < dec {
                        bump(&mut st, "unattributed_decreases");
                        if new == 0 {
                            out.push(rec(ts, recv, inst, b'C', side, opx, dec - got, d.oid, 0, seq));
                        } else {
                            out.push(rec(ts, recv, inst, b'M', side, opx, new, d.oid, 0, seq));
                        }
                    }
                } else if new > cur {
                    bump(&mut st, "size_increases");
                    out.push(rec(ts, recv, inst, b'M', side, opx, new, d.oid, 0, seq));
                }
                if new == 0 {
                    b.orders.remove(&d.oid);
                } else if let Some(o) = b.orders.get_mut(&d.oid) {
                    o.2 = new;
                }
            }
            DiffKind::Remove => {
                bump(&mut st, "remove");
                let Some((side, opx, cur)) = b.orders.remove(&d.oid) else {
                    bump(&mut st, "diff_unknown_order");
                    continue;
                };
                let got = fills_for(&mut out, &mut trades, &mut st, side, opx, cur);
                if got < cur {
                    match p.statuses.get(&d.oid).map(String::as_str) {
                        Some(s) if is_cancel_status(s) => bump(&mut st, "removed_canceled"),
                        Some("filled") => bump(&mut st, "removed_filled_without_fills"),
                        Some(_) => bump(&mut st, "removed_other_status"),
                        None => bump(&mut st, "removed_unclassified"),
                    }
                    out.push(rec(ts, recv, inst, b'C', side, opx, cur - got, d.oid, 0, seq));
                } else {
                    bump(&mut st, "removed_filled");
                }
            }
        }
        l3util::mark_last(&mut out, start);
    }
    let unmatched: u64 = p
        .makers
        .values()
        .flat_map(|q| q.iter())
        .filter(|f| instruments.contains_key(&f.coin))
        .count() as u64;
    if unmatched > 0 {
        st.insert("fills_unmatched".into(), unmatched);
    }
    let by_inst: BTreeMap<u32, &CoinBook> = books
        .iter()
        .filter_map(|(c, b)| instruments.get(c).map(|&i| (i, b)))
        .collect();
    for (&inst, b) in &by_inst {
        let (Some(a), Some(z)) = (b.first_ts, b.last_ts) else {
            continue;
        };
        if !b.seeded {
            bump(&mut st, "unseeded_instruments");
            feed_gaps.push(FeedGap {
                instrument_id: inst,
                ts_start: a,
                ts_end: z,
                reason: "no L4 snapshot: orders resting before the first diff are invisible".into(),
            });
        }
        for &(s, e) in &time_gaps {
            if e >= a && s <= z {
                feed_gaps.push(FeedGap {
                    instrument_id: inst,
                    ts_start: s,
                    ts_end: e,
                    reason: format!("block_time gap above {} ms between diff blocks", cfg.max_block_gap_ms),
                });
            }
        }
    }
    if !time_gaps.is_empty() {
        st.insert("block_time_gaps".into(), time_gaps.len() as u64);
    }
    feed_gaps.sort_by_key(|g| (g.instrument_id, g.ts_start, g.ts_end));
    let open_at_end: u64 = books.values().map(|b| b.orders.len() as u64).sum();
    let mut counters = Counters::new();
    for k in ANOMALIES {
        counters.insert(k.to_string(), 0);
    }
    counters.extend(st);
    counters.insert("fills".into(), p.fills);
    counters.insert(
        "no_execution_records".into(),
        u64::from(p.fills == 0 && !out.is_empty()),
    );
    counters.insert("records".into(), out.len() as u64);
    counters.insert("open_at_end".into(), open_at_end);
    Ok(L3Output {
        events: out,
        trades,
        counters,
        instruments,
        sequence_gaps: 0,
        pre_snapshot_rows_skipped: 0,
        dq_counters: DQ_COUNTERS.to_vec(),
        feed_gaps,
    })
}

/// (tid, oid) of a fill event, for de-duplication.
fn fill_key(e: &J) -> Option<(u64, u64)> {
    let f = match e.as_arr() {
        Some([_, f]) => f,
        _ => e,
    };
    Some((f.get("tid")?.as_u64()?, f.get("oid")?.as_u64()?))
}
