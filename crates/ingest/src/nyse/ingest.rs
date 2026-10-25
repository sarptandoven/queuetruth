//! `ingest()` for `format = "nyse_ibf"`: TAQ Integrated Feed channel files of one NYSE Group market -> dataset.
//!
//! Spec: `venue` must resolve to rule set `nyse` (`nyse` or `xnys`), else `QT-CONFIG-001`. Session
//! dates come from the file names (`EQY_US_<EXCH>_IBF_<n>_<YYYYMMDD>`) or from `session_date` / `midnight_utc_ns`
//! (which must agree with the names); every record of a file goes to that file's session date. SourceTime is read as
//! local time in `tz` (default America/New_York: the TAQ spec gives no zone, and the US/Eastern reading puts the core
//! session transitions at 09:30; a clock check warns when they are not). Prices: `price_exp` in -12..=-4 (default
//! -9); sizes in shares (`size_exp` 0); tick default 0.01; no `instruments` file. Options: `open_s`, `close_s`,
//! `memory_rows`, and `check_symbol_sequence` (default true: SymbolSeqNum gaps become feed gaps).
//!
//! Inputs are read in canonical order (session date, channel, path), whatever the order given, so instrument ids do
//! not depend on it, and they must belong to one market. Each file is parsed and mapped on a reader thread while this
//! thread writes the records (the ITCH / IEX pipeline).

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use qt_schema::json::{DataQuality, FeedGap, IngestSummary, InputFile};
use qt_schema::rows::TradeKind;
use qt_schema::specs::{IngestSpec, SessionTz};
use qt_types::{Instrument, QtError, QtResult, RuleSet, Ts, Venue, rules_for};
use serde_json::Value;

use super::FORMAT;
use super::mapper::{FAILOVER_GAP_REASON, InstrumentInfo, MapOutput, Mapper, MapperStats};
use super::reader::{FileKind, Reader, parse_file_name};
use super::rules::{Allocation, Market, PARITY_COUNTER, parity_warning};
use crate::dq::MboDqScan;
use crate::guard::GuardLimits;
use crate::time::{Date, format_date, midnight_utc_ns, parse_date, session_date_of_midnight, tz_name};
use crate::units::{decimal_to_fixed, decimal_to_units, rescale};
use crate::{DatasetKind, DatasetWriter};

/// Continuous session of every NYSE Group market: 09:30 to 16:00 local.
const OPEN_S: u32 = 34_200;
const CLOSE_S: u32 = 57_600;
/// Largest `close_s` (two days), as the other formats.
const MAX_CLOSE_S: u32 = 172_800;
const OPTIONS: [&str; 4] = ["open_s", "close_s", "memory_rows", "check_symbol_sequence"];
/// Core-session transitions within this distance of the expected local time pass the clock check.
const CLOCK_TOLERANCE_NS: u64 = 300_000_000_000;
const NS_PER_HOUR: u64 = 3_600_000_000_000;
/// Records per block handed from the reader thread to the writer.
const BLOCK: usize = 32_768;

fn cfg(msg: impl Into<String>) -> QtError {
    QtError::Config(msg.into())
}

/// Validated spec values.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Plan {
    pub(crate) price_exp: i8,
    pub(crate) tick: i64,
    pub(crate) lot: u64,
    pub(crate) tz: SessionTz,
    /// From `session_date` / `midnight_utc_ns`: `(date, midnight)`.
    pub(crate) fixed: Option<(i32, Ts)>,
    pub(crate) open_s: u32,
    pub(crate) close_s: u32,
    pub(crate) source_venue: Option<String>,
    pub(crate) symbols: Option<Vec<String>>,
    pub(crate) memory_rows: Option<usize>,
    pub(crate) check_symbol_sequence: bool,
}

fn opt_u32(v: &Value, key: &str) -> QtResult<u32> {
    v.as_u64()
        .and_then(|x| u32::try_from(x).ok())
        .ok_or_else(|| cfg(format!("options.{key}: expected an integer")))
}

fn field_or_option(field: &str, spec_v: Option<u32>, opt_v: Option<u32>) -> QtResult<Option<u32>> {
    match (spec_v, opt_v) {
        (Some(a), Some(b)) if a != b => Err(cfg(format!(
            "{field}: the spec field {field} = {a} and options.{field} = {b} disagree (set one of them)"
        ))),
        (Some(a), _) | (None, Some(a)) => Ok(Some(a)),
        (None, None) => Ok(None),
    }
}

impl Plan {
    pub(crate) fn new(spec: &IngestSpec) -> QtResult<Self> {
        let rs: RuleSet = spec.venue.parse()?;
        if rs != RuleSet::Nyse {
            return Err(cfg(format!(
                "venue: format {FORMAT} is NYSE Group TAQ Integrated Feed data and uses rule set nyse, got {:?}",
                spec.venue
            )));
        }
        if !(-12..=-4).contains(&spec.price_exp) {
            return Err(cfg(format!(
                "price_exp: NYSE prices go to 1/100 of a cent (4 decimals); price_exp must be in -12..=-4, got {}",
                spec.price_exp
            )));
        }
        if let Some(e) = spec.size_exp.filter(|&e| e != 0) {
            return Err(cfg(format!(
                "size_exp: NYSE volumes are whole shares, so size_exp must be 0, got {e}"
            )));
        }
        if spec.instruments.is_some() {
            return Err(cfg(format!(
                "instruments: an instruments file is read by formats etcsv and parquet only, not {FORMAT} (symbols \
                 come from the Integrated Feed)"
            )));
        }
        let tz = spec.tz.unwrap_or(SessionTz::AmericaNewYork);
        let tick = match &spec.tick_size {
            Some(s) => {
                let t = decimal_to_fixed(s, spec.price_exp)?;
                if t <= 0 {
                    return Err(cfg(format!("tick_size: must be > 0, got {s:?}")));
                }
                t
            }
            None => rescale(1, -2, spec.price_exp).ok_or_else(|| cfg("price_exp: tick 0.01 not representable"))?,
        };
        let lot = match &spec.lot_size {
            Some(s) => {
                let l = decimal_to_units(s, 0)?;
                if l == 0 {
                    return Err(cfg(format!("lot_size: must be > 0, got {s:?}")));
                }
                l
            }
            None => 1,
        };
        let fixed = match (spec.midnight_utc_ns, &spec.session_date) {
            (Some(m), d) => {
                if m < 0 {
                    return Err(cfg(format!("midnight_utc_ns: must be >= 0, got {m}")));
                }
                let date = match d {
                    Some(d) => parse_date(d)?,
                    None => session_date_of_midnight(m, tz)?,
                };
                let want = midnight_utc_ns(date, tz)?;
                if m != want {
                    return Err(cfg(format!(
                        "midnight_utc_ns: {m} is not the {} midnight of {} ({want}); TAQ SourceTime is a time of day \
                         on the session date",
                        tz_name(tz),
                        format_date(date)
                    )));
                }
                Some((date, m))
            }
            (None, Some(d)) => {
                let date = parse_date(d)?;
                Some((date, midnight_utc_ns(date, tz)?))
            }
            (None, None) => None,
        };
        let (mut o_open, mut o_close, mut memory_rows, mut check) = (None, None, None, true);
        for (k, v) in &spec.options {
            match k.as_str() {
                "open_s" => o_open = Some(opt_u32(v, k)?),
                "close_s" => o_close = Some(opt_u32(v, k)?),
                "memory_rows" => memory_rows = Some(opt_u32(v, k)? as usize),
                "check_symbol_sequence" => {
                    check = v
                        .as_bool()
                        .ok_or_else(|| cfg("options.check_symbol_sequence: expected true or false"))?;
                }
                _ => {
                    return Err(cfg(format!(
                        "options.{k}: unknown option for format {FORMAT} (allowed: {})",
                        OPTIONS.join(", ")
                    )));
                }
            }
        }
        let open_s = field_or_option("open_s", spec.open_s, o_open)?.unwrap_or(OPEN_S);
        let close_s = field_or_option("close_s", spec.close_s, o_close)?.unwrap_or(CLOSE_S);
        if open_s > close_s || close_s > MAX_CLOSE_S {
            return Err(cfg(format!(
                "open_s / close_s: {open_s} / {close_s} out of range (need open_s <= close_s <= {MAX_CLOSE_S} seconds \
                 after local midnight)"
            )));
        }
        Ok(Self {
            price_exp: spec.price_exp,
            tick,
            lot,
            tz,
            fixed,
            open_s,
            close_s,
            source_venue: spec.source_venue.clone(),
            symbols: spec
                .symbols
                .as_ref()
                .map(|v| v.iter().map(|s| s.trim().to_ascii_uppercase()).collect()),
            memory_rows,
            check_symbol_sequence: check,
        })
    }
}

/// One input in reading order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Input {
    pub(crate) path: PathBuf,
    pub(crate) date: i32,
    pub(crate) midnight: Ts,
    /// From the file name; 0 when the name does not follow the TAQ convention.
    pub(crate) channel: u16,
    pub(crate) market: Option<Market>,
}

/// Inputs in canonical order, with their session dates; one market only.
pub(crate) fn plan_inputs(plan: &Plan, inputs: &[PathBuf]) -> QtResult<Vec<Input>> {
    let mut out = Vec::with_capacity(inputs.len());
    let mut markets = BTreeSet::new();
    for path in inputs {
        let named = match parse_file_name(path) {
            Some(FileKind::StockSummary { .. }) => {
                return Err(cfg(format!(
                    "inputs: {} is a TAQ stock summary file (type 223 only, no orders); pass the \
                     EQY_US_<market>_IBF_<channel>_<date> channel files",
                    path.display()
                )));
            }
            Some(FileKind::Integrated { market, channel, date }) => Some((market, channel, date.days_from_civil())),
            None => None,
        };
        let date = match (named, plan.fixed) {
            (Some((_, _, d)), Some((f, _))) if d != f => {
                return Err(cfg(format!(
                    "session_date: {} is named for {} but the spec says {}",
                    path.display(),
                    format_date(d),
                    format_date(f)
                )));
            }
            (Some((_, _, d)), _) => d,
            (None, Some((f, _))) => f,
            (None, None) => {
                return Err(cfg(format!(
                    "session_date: {} does not follow the TAQ name EQY_US_<market>_IBF_<channel>_<YYYYMMDD>; pass \
                     session_date (--date)",
                    path.display()
                )));
            }
        };
        let midnight = match plan.fixed {
            Some((f, m)) if f == date => m,
            _ => midnight_utc_ns(date, plan.tz)?,
        };
        if let Some((m, _, _)) = named {
            markets.insert(m);
        }
        out.push(Input {
            path: path.clone(),
            date,
            midnight,
            channel: named.map_or(0, |(_, c, _)| c),
            market: named.map(|(m, _, _)| m),
        });
    }
    if markets.len() > 1 {
        let names: Vec<String> = markets.iter().map(ToString::to_string).collect();
        return Err(cfg(format!(
            "inputs: the files belong to {} (one dataset holds one market; ingest each market separately)",
            names.join(", ")
        )));
    }
    out.sort_by(|a, b| (a.date, a.channel, &a.path).cmp(&(b.date, b.channel, &b.path)));
    for w in out.windows(2) {
        if w[0].channel != 0 && (w[0].date, w[0].channel) == (w[1].date, w[1].channel) {
            return Err(cfg(format!(
                "inputs: {} and {} are both channel {} of {}",
                w[0].path.display(),
                w[1].path.display(),
                w[0].channel,
                format_date(w[0].date)
            )));
        }
    }
    Ok(out)
}

/// The clock check of one file: Security Status transitions to the core session (`O`) happen at 09:30 US/Eastern.
/// `None` when the file has none, or when their median is where `tz` says it should be.
pub(crate) fn clock_warning(label: &str, opens: &[u64], tz: SessionTz, date: i32) -> Option<String> {
    if opens.is_empty() {
        return None;
    }
    let mut v = opens.to_vec();
    v.sort_unstable();
    let median = v[v.len() / 2];
    let eastern = u64::from(OPEN_S) * 1_000_000_000;
    let offset = Date::from_days(date).map_or(5, Date::eastern_offset_hours) as u64;
    let utc = eastern + offset * NS_PER_HOUR;
    let near = |x: u64| median.abs_diff(x) <= CLOCK_TOLERANCE_NS;
    let hhmm = |ns: u64| {
        format!(
            "{:02}:{:02}:{:02}",
            ns / NS_PER_HOUR,
            ns / 60_000_000_000 % 60,
            ns / 1_000_000_000 % 60
        )
    };
    let looks = if near(eastern) {
        Some(SessionTz::AmericaNewYork)
    } else if near(utc) {
        Some(SessionTz::Utc)
    } else {
        None
    };
    match looks {
        Some(z) if z == tz => None,
        Some(z) => Some(format!(
            "{label}: the core session opens at {} in the file's SourceTime (median of {} Security Status `O` \
             messages), which is 09:30 US/Eastern in {} time, not in {}: the records are dated one clock off; ingest \
             again with tz {}",
            hhmm(median),
            v.len(),
            tz_name(z),
            tz_name(tz),
            tz_name(z)
        )),
        None => Some(format!(
            "{label}: the core session opens at {} in the file's SourceTime (median of {} Security Status `O` \
             messages), neither 09:30 US/Eastern nor its UTC time {}: check the file's clock before trusting the \
             timestamps",
            hhmm(median),
            v.len(),
            hhmm(utc)
        )),
    }
}

/// Session dates on which US/Eastern changes its UTC offset (SourceTime is placed on a fixed offset from local
/// midnight, so times after 02:00 on those days are one hour off).
fn dst_switch_dates(inputs: &[Input], tz: SessionTz) -> Vec<i32> {
    if tz != SessionTz::AmericaNewYork {
        return Vec::new();
    }
    let mut out: Vec<i32> = inputs
        .iter()
        .map(|i| i.date)
        .filter(|&d| {
            let off = |x: i32| Date::from_days(x).map(Date::eastern_offset_hours);
            off(d) != off(d + 1)
        })
        .collect();
    out.dedup();
    out
}

/// Starts hashing the inputs on a thread (size + sha256 for the manifest).
fn hash_inputs(inputs: &[Input]) -> std::thread::JoinHandle<QtResult<Vec<InputFile>>> {
    let paths: Vec<PathBuf> = inputs.iter().map(|i| i.path.clone()).collect();
    std::thread::spawn(move || {
        paths
            .into_iter()
            .map(|p| {
                let bytes = std::fs::metadata(&p)
                    .map_err(|e| QtError::InputMissing {
                        path: p.display().to_string(),
                        msg: e.to_string(),
                    })?
                    .len();
                Ok(InputFile {
                    path: p.display().to_string(),
                    bytes,
                    sha256: qt_schema::io::sha256_file(&p)?,
                })
            })
            .collect()
    })
}

/// Reads one file on a thread, mapping as it goes, and writes its records; returns the mapper.
fn ingest_file(
    mut mapper: Mapper,
    input: &Input,
    w: &mut DatasetWriter,
    scan: &mut MboDqScan,
    seen: &mut BTreeSet<(i32, u32)>,
) -> QtResult<Mapper> {
    let rules = rules_for(RuleSet::Nyse);
    let (tx, rx) = std::sync::mpsc::sync_channel::<MapOutput>(4);
    let path = input.path.clone();
    let producer = std::thread::spawn(move || -> (Mapper, QtResult<()>) {
        let run = |m: &mut Mapper| -> QtResult<()> {
            let mut reader = Reader::open(&path, GuardLimits::default())?;
            let mut block = MapOutput::default();
            let mut one = MapOutput::default();
            while let Some((line_no, line)) = reader.next_line()? {
                m.apply_line(line, line_no, &mut one)?;
                block.mbo.append(&mut one.mbo);
                block.trades.append(&mut one.trades);
                if block.mbo.len() >= BLOCK && tx.send(std::mem::take(&mut block)).is_err() {
                    return Err(QtError::Engine("NYSE writer stopped".into()));
                }
            }
            // The file's last records: books a failover cleared without a refresh and no later message showed.
            m.end_file(&mut one);
            block.mbo.append(&mut one.mbo);
            let _ = tx.send(block);
            Ok(())
        };
        let r = run(&mut mapper);
        (mapper, r)
    });
    let date = input.date;
    let mut failure = None;
    // Records come in runs of one instrument: insert a partition key only when it changes.
    let mut last_key = None;
    for block in rx.iter() {
        // A cross print is a side-N trade like a hidden print; its `cross` trade row (same instrument and
        // sequence) tells them apart (OC-28), so it is counted as a cross print, not a hidden print.
        let crosses: BTreeSet<(u32, u64)> = block
            .trades
            .iter()
            .filter(|t| t.kind == TradeKind::Cross)
            .map(|t| (t.instrument_id, t.sequence))
            .collect();
        let res = (|| -> QtResult<()> {
            for e in &block.mbo {
                scan.record(e, date, &rules);
                if e.action == b'T' && e.side == b'N' && crosses.contains(&(e.instrument_id, e.sequence)) {
                    scan.cross_print();
                }
                w.push_mbo_at(e, date)?;
                let key = (date, e.instrument_id);
                if last_key != Some(key) {
                    seen.insert(key);
                    last_key = Some(key);
                }
            }
            for t in &block.trades {
                w.push_source_trade_at(t, date)?;
            }
            Ok(())
        })();
        if let Err(e) = res {
            failure = Some(e);
            break;
        }
    }
    drop(rx);
    let (mapper, read) = producer
        .join()
        .map_err(|_| QtError::Engine("NYSE reader thread panicked".into()))?;
    if let Some(e) = failure {
        return Err(e);
    }
    read?;
    Ok(mapper)
}

/// The allocation label of each partition written (`(session date, instrument id)` in `seen`): the `(market,
/// date)` of every partition whose market allocated by parity on its session date (its FIFO queue ranks are
/// approximations whether or not it traded), and the number of partitions whose market is unknown (no file-name
/// market and no Symbol Index Mapping). An instrument's market is its Symbol Index Mapping's, else the inputs'.
pub(crate) fn allocation_labels(
    seen: &BTreeSet<(i32, u32)>,
    info: &[InstrumentInfo],
    inputs_market: Option<Market>,
) -> (Vec<(Market, i32)>, u64) {
    let mut parity = Vec::new();
    let mut unknown = 0;
    for &(date, iid) in seen {
        let market = info.get(iid as usize - 1).and_then(|i| i.market).or(inputs_market);
        match market {
            Some(m) if m.allocation_on(date) == Allocation::Parity => parity.push((m, date)),
            Some(_) => {}
            None => unknown += 1,
        }
    }
    (parity, unknown)
}

/// Instrument rows of the partitions written.
fn instrument_rows(
    plan: &Plan,
    inputs: &[Input],
    info: &[InstrumentInfo],
    seen: &BTreeSet<(i32, u32)>,
    venue: &str,
) -> QtResult<Vec<Instrument>> {
    let midnights: BTreeMap<i32, Ts> = inputs.iter().map(|i| (i.date, i.midnight)).collect();
    seen.iter()
        .map(|&(date, iid)| {
            let i = info
                .get(iid as usize - 1)
                .ok_or_else(|| QtError::Engine(format!("nyse: records for instrument {iid} without a symbol")))?;
            let midnight = match midnights.get(&date) {
                Some(&m) => m,
                None => midnight_utc_ns(date, plan.tz)?,
            };
            Ok(Instrument {
                instrument_id: iid,
                session_date: date,
                symbol: i.symbol.clone(),
                venue: Venue::Nasdaq,
                source_venue: match (&plan.source_venue, i.market) {
                    (Some(s), _) => s.clone(),
                    (None, Some(m)) => m.source_venue().to_owned(),
                    (None, None) => venue.to_owned(),
                },
                price_exp: plan.price_exp,
                tick_size: plan.tick,
                lot_size: plan.lot,
                size_exp: 0,
                contract_multiplier: 1.0,
                currency: "USD".into(),
                session_tz: tz_name(plan.tz).into(),
                midnight_utc_ns: midnight,
                open_s: plan.open_s,
                close_s: plan.close_s,
                source: FORMAT.into(),
            })
        })
        .collect()
}

/// Non-INFO counter of the partitions whose market, and so allocation rule, is unknown.
pub(crate) const ALLOCATION_UNKNOWN_COUNTER: &str = "nyse_allocation_unknown";

/// Counters, feed gaps and warnings from the mapper (SPEC 5.1.5 counters; `source_specific` `nyse_*`). `parity`
/// and `unknown`: the allocation labels of the partitions written (`allocation_labels`).
fn finish_counters(
    scan: &mut MboDqScan,
    mapper: &Mapper,
    st: &MapperStats,
    (parity, unknown): (&[(Market, i32)], u64),
    warnings: &mut Vec<String>,
) {
    let a = &st.anomalies;
    let dq: &mut DataQuality = scan.counters_mut();
    dq.unknown_order_refs += a.unknown_order_ref;
    dq.duplicate_adds += a.duplicate_order_ref;
    dq.sequence_gaps += mapper.sequence_gaps;
    dq.sequence_regressions += mapper.sequence_regressions;
    let mut gaps: Vec<FeedGap> = mapper.gaps().to_vec();
    gaps.sort_by(|x, y| {
        (x.instrument_id, x.ts_start, x.ts_end, &x.reason).cmp(&(y.instrument_id, y.ts_start, y.ts_end, &y.reason))
    });
    dq.feed_gaps.extend(gaps);
    // The label first, so that it leads the source-specific reasons: the queue ranks of every partition of a parity
    // market are FIFO approximations, whether or not it traded (non-INFO on purpose).
    scan.add_source(PARITY_COUNTER, parity.len() as u64);
    // The rule named in the warning is the earliest (market, date) of a parity partition (deterministic).
    if let Some(&(m, date)) = parity.iter().min() {
        warnings.push(parity_warning(m, date, parity.len(), st.parity_executions));
    }
    scan.add_source(ALLOCATION_UNKNOWN_COUNTER, unknown);
    for (k, v) in a.named() {
        if k != "nyse_unknown_order_ref" && k != "nyse_duplicate_order_ref" {
            scan.add_source(k, v);
        }
    }
    scan.add_source("nyse_symbols_not_found", st.symbols_not_found.len() as u64);
    // INFO counters (dq::INFO_SOURCE_COUNTERS): volumes, never part of the verdict.
    scan.add_source("nyse_parity_executions", st.parity_executions);
    scan.add_source("nyse_modify_keep_priority", st.modify_keep_priority);
    scan.add_source("nyse_modify_lose_priority", st.modify_lose_priority);
    scan.add_source("nyse_modify_noop", st.modify_noop);
    scan.add_source("nyse_replace_orders", st.replace_orders);
    scan.add_source("nyse_replace_reduce_same_price", st.replace_reduce_same_price);
    scan.add_source("nyse_nonprintable_executions", st.nonprintable_executions);
    scan.add_source("nyse_nonprintable_hidden_trades", st.nonprintable_hidden_trades);
    scan.add_source("nyse_zero_volume_crosses", st.zero_volume_crosses);
    scan.add_source("nyse_attributed_adds", st.attributed_adds);
    scan.add_source("nyse_trade_cancels", st.trade_cancels);
    scan.add_source("nyse_cross_corrections", st.cross_corrections);
    scan.add_source("nyse_failover_books_kept", st.failover_books_kept);
    if unknown > 0 {
        warnings.push(format!(
            "{unknown} instrument-day(s) of symbols whose market is unknown (the file names do not follow \
             EQY_US_<market>_IBF_<n>_<date> and no Symbol Index Mapping message names a market): whether their FIFO \
             queue ranks are exact is unknown (NYSE allocates by parity, its sister markets by price-time)"
        ));
    }
    if a.failover_books_cleared > 0 {
        warnings.push(format!(
            "{} book(s) cleared after a publisher failover: the symbol had resting orders but got no Add Order \
             Refresh (the backup clears every symbol of the channel and re-sends only the orders still resting, TAQ \
             Products Client Specification v2.4b section 11), so its book was cleared (`R`) at its next Add or at the \
             end of the file, inside a feed gap ({FAILOVER_GAP_REASON:?})",
            a.failover_books_cleared
        ));
    }
    if st.failover_books_kept > 0 {
        warnings.push(format!(
            "{} symbol(s) referenced their resting orders after an Add Order Refresh on their channel without being \
             refreshed, so their books were kept: that refresh did not cover the whole channel (a refresh of single \
             symbols rather than a publisher failover)",
            st.failover_books_kept
        ));
    }
    if a.files_without_order_messages > 0 {
        warnings.push(format!(
            "{} input file(s) hold no order message (Add, Modify, Delete, Execution, Replace or Add Order Refresh): \
             {}; an Integrated Feed channel file of a trading day always has them (another product renamed, or a \
             truncated file?)",
            a.files_without_order_messages,
            mapper.files_without_orders.join(", ")
        ));
    }
    if !st.symbols_not_found.is_empty() {
        let shown: Vec<&str> = st.symbols_not_found.iter().take(10).map(String::as_str).collect();
        warnings.push(format!(
            "symbols never seen in the inputs: {} (NYSE symbology writes share classes with a space, e.g. \"BRK A\")",
            shown.join(", ")
        ));
    }
    if let Some(first) = &mapper.first_malformed {
        warnings.push(format!(
            "{} malformed line(s) skipped; first: {first}",
            a.malformed_lines
        ));
    }
    if a.refresh_runs > 0 {
        warnings.push(format!(
            "{} Add Order Refresh run(s): the publisher re-sent a symbol's book after a failover; the book was cleared \
             (`R`) and rebuilt, and a feed gap covers each run",
            a.refresh_runs
        ));
    }
    if a.midnight_rollovers > 0 {
        warnings.push(format!(
            "the SourceTime clock passed midnight {} time(s) inside a file: later records are dated the next day but \
             stay in the file's session-date partition",
            a.midnight_rollovers
        ));
    }
    if a.symbol_on_two_channels > 0 {
        warnings.push(format!(
            "{} symbol(s) appeared on two data channels of one day: their records are written channel by channel, \
             not in one feed order",
            a.symbol_on_two_channels
        ));
    }
    if a.market_mismatch > 0 {
        warnings.push(format!(
            "{} Symbol Index Mapping message(s) name another market than the file name; the message's market is used",
            a.market_mismatch
        ));
    }
}

/// `ingest` for `nyse_ibf` (dispatched by `ingest_impl::ingest`, which checks `out`, the inputs, `strict` and echoes
/// the spec).
pub(crate) fn ingest(spec: &IngestSpec, inputs: &[PathBuf], out: &Path) -> QtResult<IngestSummary> {
    let plan = Plan::new(spec)?;
    let inputs = plan_inputs(&plan, inputs)?;
    let rules = rules_for(RuleSet::Nyse);
    let mut w = DatasetWriter::create(out, DatasetKind::Mbo, Venue::Nasdaq, FORMAT)?;
    w.set_rule_set(Some(RuleSet::Nyse));
    w.set_validation_status(rules.validation);
    if let Some(n) = plan.memory_rows {
        w.set_memory_rows(n);
    }
    let hashing = hash_inputs(&inputs);
    let mut warnings = Vec::new();
    for d in dst_switch_dates(&inputs, plan.tz) {
        warnings.push(format!(
            "session date {} is a US daylight-saving switch day: SourceTime is placed on the offset of local midnight, \
             so records after 02:00 local are one hour off",
            format_date(d)
        ));
    }
    let mut scan = MboDqScan::new();
    let mut seen = BTreeSet::new();
    let mut mapper = Mapper::new(plan.symbols.as_deref(), plan.price_exp, plan.check_symbol_sequence)?;
    for input in &inputs {
        let label = input.path.display().to_string();
        mapper.begin_file(super::mapper::FileCtx {
            date: input.date,
            midnight: input.midnight,
            channel: input.channel,
            market: input.market,
            label: label.clone(),
        });
        mapper = ingest_file(mapper, input, &mut w, &mut scan, &mut seen)?;
        if let Some(msg) = clock_warning(&label, mapper.file_core_opens(), plan.tz, input.date) {
            warnings.push(msg);
        }
    }
    let st = mapper.finish().clone();
    let info = mapper.instruments();
    let inputs_market = inputs.iter().find_map(|i| i.market);
    let market = inputs_market.or_else(|| info.iter().filter(|i| i.selected).find_map(|i| i.market));
    let venue = plan
        .source_venue
        .clone()
        .unwrap_or_else(|| market.map_or(RuleSet::Nyse.name(), Market::source_venue).to_owned());
    w.set_source_venue(&venue);
    let (parity, unknown) = allocation_labels(&seen, &info, inputs_market);
    finish_counters(&mut scan, &mapper, &st, (&parity, unknown), &mut warnings);
    w.set_instruments(instrument_rows(&plan, &inputs, &info, &seen, &venue)?);
    for f in hashing
        .join()
        .map_err(|_| QtError::Engine("input hashing thread panicked".into()))??
    {
        w.add_input_file(f);
    }
    for msg in &warnings {
        w.add_warning(msg.clone());
    }
    let mut summary = w.finish(scan.finish())?;
    summary.warnings = warnings;
    Ok(summary)
}
