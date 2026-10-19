//! Crypto L3 captures (Bitstamp, Bitfinex, Coinbase) -> MBO datasets and Tardis L2 CSV -> L2-only datasets
//! (SPEC 1.1, 4.1 `qt_crypto`). Each source module exposes a pure converter
//! `convert(lines, cfg) -> QtResult<(records, source trades, counters)>`; `ingest` resolves the inputs, converts,
//! cuts session dates (UTC days unless `tz` says otherwise), re-seeds L3 books at session-date boundaries and
//! writes the dataset through `qt_ingest::DatasetWriter`.
//!
//! Units: prices at `10^price_exp` (default -9), sizes at `10^size_exp` (crypto default -8); every decimal string
//! converts exactly or fails `QT-INPUT-004`. Instrument ids: the order of `symbols` when given, else the capture's
//! `capture_meta.json` list, else the sorted symbols seen. `tick_size` / `lot_size` default to the greatest common
//! divisor of the prices / sizes seen per instrument (counted in the summary's source counters as inferred).
//! Session-date boundary: partitions replay independently, so before the first record of a later session date an
//! L3 instrument with resting orders gets a synthetic `R` + `A` (`SNAPSHOT`) book in priority order
//! (`day_boundary_reseeds`, `day_boundary_reseeded_orders`). L2 (Tardis) inputs are cut at session dates without
//! synthetic snapshots (Tardis day files start with one).
//! `options`: `taker_window_us` (bitstamp; integer, or null to disable taker handling), `flags` and `max_size`
//! (bitfinex; `flags` defaults to `capture_meta.json`), `warmup_depth` (coinbase, default 10), `currency` (all).
//! A capture directory input expands to its data files in the source's read order plus `capture_meta.json`.
//! `qt.data_quality.source_specific` holds the non-zero anomaly / adjustment counters of the source
//! (`<module>::DQ_COUNTERS`); the full counter set is returned by each `convert` and kept in `Prepared::counters`.
//! Rule set (schema 1.1, E38, audit M25): `venue` is read as a rule-set name; for the L3 formats (bitstamp,
//! bitfinex, coinbase) `generic` and its aliases resolve to `crypto_l3` (`generic` without hidden orders), since the
//! `generic` rules were built for consolidated data. `coinbase`, `crypto_l3` and the other names are kept as given;
//! Tardis L2 keeps `generic`. `crypto_l3` runs on `Venue::Generic` records, so the manifest and instrument rows
//! carry `generic`; the resolved name is `Prepared::rule_set`.
//! Session window (schema 1.1, 3.11 #18): `open_s` / `close_s` default to 0 / the length of the local day.

#![forbid(unsafe_code)]

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use indexmap::IndexMap;
use qt_ingest::{DatasetKind, DatasetWriter};
use qt_schema::json::{self, DataQuality, FeedGap};
use qt_schema::rows::{L2Row, TradeRow};
use qt_schema::specs::{self, IngestFormat, SessionTz};
use qt_types::{Instrument, MboEvent, QtError, QtResult, RuleSet, Venue, flags, rules_for};
use serde_json::Value;

pub mod bitfinex;
pub mod bitstamp;
pub mod coinbase;
pub mod input;
pub mod jsonx;
pub mod tardis;
pub mod units;

use units::SessionClock;

/// Source-specific counters (reported in `qt.data_quality.source_specific`).
pub type Counters = std::collections::BTreeMap<String, u64>;

/// Output of an L3 converter with the extra numbers ingest needs.
pub(crate) struct L3Output {
    pub events: Vec<MboEvent>,
    pub trades: Vec<TradeRow>,
    pub counters: Counters,
    pub instruments: IndexMap<String, u32>,
    pub sequence_gaps: u64,
    pub pre_snapshot_rows_skipped: u64,
    pub dq_counters: Vec<&'static str>,
    pub feed_gaps: Vec<FeedGap>,
}

/// Everything `ingest` would write, computed without touching the output directory.
#[derive(Clone, Debug)]
pub struct Prepared {
    /// Ingest format name (`bitstamp`, `bitfinex`, `coinbase`, `tardis_l2`).
    pub format: &'static str,
    /// True for Tardis (L2-only dataset).
    pub l2_only: bool,
    /// Record-semantics venue written to the manifest and instrument rows (`rule_set.venue()`).
    pub venue: Venue,
    /// Rule set the ingest applied (schema 1.1): `crypto_l3` for the L3 formats unless another one is named.
    pub rule_set: RuleSet,
    /// Source venue written to the manifest and instrument rows.
    pub source_venue: String,
    /// Instrument rows, sorted by (session_date, instrument_id).
    pub instruments: Vec<Instrument>,
    /// MBO records in feed order (L3 formats).
    pub events: Vec<MboEvent>,
    /// L2 rows in feed order (Tardis).
    pub l2: Vec<L2Row>,
    /// Source trades.
    pub trades: Vec<TradeRow>,
    /// Files read (data files, then `capture_meta.json`).
    pub inputs: Vec<PathBuf>,
    /// Every converter counter plus the ingest-level ones (superset of `data_quality.source_specific`).
    pub counters: Counters,
    /// Ingest-time data quality (verdict applied).
    pub data_quality: DataQuality,
    /// Spec echo (execution-only fields removed).
    pub spec_echo: json::Object,
}

const OPTION_KEYS: [&str; 5] = ["taker_window_us", "flags", "max_size", "warmup_depth", "currency"];
/// Largest `close_s` (two days, as `qt_ingest`).
const MAX_CLOSE_S: u32 = 172_800;

fn cfg_err(msg: impl Into<String>) -> QtError {
    QtError::Config(msg.into())
}

fn opt_i64(spec: &specs::IngestSpec, key: &str) -> QtResult<Option<Option<i64>>> {
    match spec.options.get(key) {
        None => Ok(None),
        Some(Value::Null) => Ok(Some(None)),
        Some(v) => v
            .as_i64()
            .map(|x| Some(Some(x)))
            .ok_or_else(|| cfg_err(format!("options.{key}: expected an integer"))),
    }
}

fn format_name(f: IngestFormat) -> QtResult<&'static str> {
    match f {
        IngestFormat::Bitstamp => Ok("bitstamp"),
        IngestFormat::Bitfinex => Ok("bitfinex"),
        IngestFormat::Coinbase => Ok("coinbase"),
        IngestFormat::TardisL2 => Ok("tardis_l2"),
        other => Err(cfg_err(format!(
            "format: {} is not a crypto format (qt_crypto handles bitstamp|bitfinex|coinbase|tardis_l2)",
            serde_json::to_value(other)
                .ok()
                .and_then(|v| v.as_str().map(str::to_string))
                .unwrap_or_default()
        ))),
    }
}

const QUOTES: [&str; 16] = [
    "FDUSD", "USDT", "USDC", "BUSD", "TUSD", "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "BTC", "ETH", "DAI",
    "PAX",
];

/// Quote currency guessed from a symbol (`btcusd`, `tBTCUSD`, `BTC-USD`, `BTCUSDT`); empty when unknown.
pub fn guess_currency(symbol: &str) -> String {
    let up = symbol.to_ascii_uppercase();
    if let Some(last) = up
        .rsplit(['-', '/', ':', '_'])
        .next()
        .filter(|_| up.contains(['-', '/', ':', '_']))
    {
        if QUOTES.contains(&last) {
            return last.to_string();
        }
    }
    QUOTES
        .iter()
        .find(|q| up.len() > q.len() && up.ends_with(*q))
        .map(|q| (*q).to_string())
        .unwrap_or_default()
}

/// Rule set of an ingest: `venue` parsed as a rule-set name, with `generic` (and its aliases `bitstamp`,
/// `bitfinex`) meaning `crypto_l3` for the L3 formats (E38, M25).
pub fn rule_set_of(venue: &str, format: IngestFormat) -> QtResult<RuleSet> {
    let rs: RuleSet = venue.parse()?;
    Ok(match (rs, format) {
        (RuleSet::Generic, IngestFormat::Bitstamp | IngestFormat::Bitfinex | IngestFormat::Coinbase) => {
            RuleSet::CryptoL3
        }
        _ => rs,
    })
}

/// Mini book used for session-boundary re-seeding: order id -> (side, price, size, priority).
#[derive(Default)]
struct SeedBook {
    orders: HashMap<u64, (u8, i64, u64, u64)>,
    prio: u64,
}

impl SeedBook {
    fn apply(&mut self, e: &MboEvent, keep: qt_types::PriorityRule) {
        match e.action {
            b'A' if e.price != qt_types::UNDEF_PRICE && e.size > 0 => {
                self.prio += 1;
                self.orders.insert(e.order_id, (e.side, e.price, e.size, self.prio));
            }
            b'C' => {
                if let Some(o) = self.orders.get_mut(&e.order_id) {
                    if e.size >= o.2 {
                        self.orders.remove(&e.order_id);
                    } else {
                        o.2 -= e.size;
                    }
                }
            }
            b'M' => match self.orders.get_mut(&e.order_id) {
                Some(_) if e.size == 0 => {
                    self.orders.remove(&e.order_id);
                }
                Some(o) => {
                    if !keep.keeps_priority(o.1, o.2, e.price, e.size) {
                        self.prio += 1;
                        o.3 = self.prio;
                    }
                    (o.1, o.2) = (e.price, e.size);
                }
                None if e.size > 0 && e.price != qt_types::UNDEF_PRICE => {
                    self.prio += 1;
                    self.orders.insert(e.order_id, (e.side, e.price, e.size, self.prio));
                }
                None => {}
            },
            b'R' => self.orders.clear(),
            _ => {}
        }
    }

    /// Resting orders: bids best first, then asks best first, FIFO within a level.
    fn snapshot(&self) -> Vec<(u64, u8, i64, u64)> {
        let mut v: Vec<(u64, u8, i64, u64, u64)> = self
            .orders
            .iter()
            .map(|(&id, &(s, p, q, pr))| (id, s, p, q, pr))
            .collect();
        v.sort_by_key(|&(id, s, p, _, pr)| {
            (
                s != b'B',
                if s == b'B' { -i128::from(p) } else { i128::from(p) },
                pr,
                id,
            )
        });
        v.into_iter().map(|(id, s, p, q, _)| (id, s, p, q)).collect()
    }
}

/// Inserts `R` + `A` (`SNAPSHOT`) books before the first record of each later session date per instrument.
fn reseed_at_boundaries(
    events: Vec<MboEvent>,
    date_of: impl Fn(i64) -> i32,
    rule_set: RuleSet,
    counters: &mut Counters,
) -> Vec<MboEvent> {
    let keep = rules_for(rule_set).modify_priority;
    let mut books: HashMap<u32, (i32, SeedBook)> = HashMap::new();
    let mut out = Vec::with_capacity(events.len());
    let (mut reseeds, mut reseeded) = (0u64, 0u64);
    for e in events {
        let d = date_of(e.ts_event);
        let (cur, book) = books.entry(e.instrument_id).or_insert_with(|| (d, SeedBook::default()));
        if d > *cur {
            *cur = d;
            let snap = book.snapshot();
            if !snap.is_empty() {
                reseeds += 1;
                reseeded += snap.len() as u64;
                let base = MboEvent {
                    ts_event: e.ts_event,
                    ts_recv: e.ts_recv,
                    instrument_id: e.instrument_id,
                    sequence: e.sequence,
                    channel_id: e.channel_id,
                    publisher_id: e.publisher_id,
                    ..MboEvent::default()
                };
                out.push(MboEvent {
                    action: b'R',
                    side: b'N',
                    flags: 0,
                    ..base
                });
                let n = snap.len();
                for (k, (id, s, p, q)) in snap.into_iter().enumerate() {
                    let fl = flags::SNAPSHOT | if k + 1 == n { flags::LAST } else { 0 };
                    out.push(MboEvent {
                        action: b'A',
                        side: s,
                        price: p,
                        size: q,
                        order_id: id,
                        flags: fl,
                        ..base
                    });
                }
            }
        }
        book.apply(&e, keep);
        out.push(e);
    }
    counters.insert("day_boundary_reseeds".into(), reseeds);
    counters.insert("day_boundary_reseeded_orders".into(), reseeded);
    out
}

/// Converts the spec's inputs and computes instruments and data quality without writing anything.
pub fn prepare(spec: &specs::IngestSpec) -> QtResult<Prepared> {
    let format = format_name(spec.format)?;
    let rule_set = rule_set_of(&spec.venue, spec.format)?;
    let venue = rule_set.venue();
    if !(-12..=0).contains(&spec.price_exp) {
        return Err(cfg_err("price_exp: must be in -12..=0"));
    }
    let size_exp = spec.size_exp.unwrap_or(-8);
    if !(-18..=0).contains(&size_exp) {
        return Err(cfg_err("size_exp: must be in -18..=0"));
    }
    if spec.close_s.is_some_and(|c| c > MAX_CLOSE_S) || spec.open_s.zip(spec.close_s).is_some_and(|(o, c)| o > c) {
        return Err(cfg_err(format!(
            "open_s / close_s: need open_s <= close_s <= {MAX_CLOSE_S} (got {:?} / {:?})",
            spec.open_s, spec.close_s
        )));
    }
    if let Some(k) = spec.options.keys().find(|k| !OPTION_KEYS.contains(&k.as_str())) {
        return Err(cfg_err(format!(
            "options.{k}: unknown option (allowed: {})",
            OPTION_KEYS.join(", ")
        )));
    }
    let inp = input::resolve(&spec.inputs, format)?;
    let names: Vec<String> = match (&spec.symbols, inp.meta.symbols.is_empty()) {
        (Some(s), _) => s.clone(),
        (None, false) => inp.meta.symbols.clone(),
        (None, true) => Vec::new(),
    };
    let map: IndexMap<String, u32> = names
        .iter()
        .enumerate()
        .map(|(i, s)| (s.clone(), i as u32 + 1))
        .collect();
    if map.len() != names.len() {
        return Err(cfg_err("symbols: duplicate symbol"));
    }
    let lines = input::lines(&inp.files)?;
    let (pe, se) = (spec.price_exp, size_exp);

    let mut counters: Counters;
    let mut dq = DataQuality::default();
    let (mut events, l2, trades, instruments_map, exchange, dq_keys);
    match format {
        "tardis_l2" => {
            let o = tardis::convert_full(
                lines,
                &tardis::Config {
                    instruments: map,
                    price_exp: pe,
                    size_exp: se,
                },
            )?;
            if o.rows.is_empty() && o.trades.is_empty() {
                return Err(QtError::InputFormat {
                    location: "tardis".into(),
                    msg: "no rows converted".into(),
                });
            }
            dq.pre_snapshot_rows_skipped = o.pre_snapshot_rows_skipped;
            dq.records = o.rows.len() as u64;
            dq.snapshot_records = o
                .rows
                .iter()
                .filter(|r| r.flags & qt_schema::rows::l2flags::SNAPSHOT != 0)
                .count() as u64;
            counters = o.counters;
            (events, l2, trades, instruments_map, exchange) = (Vec::new(), o.rows, o.trades, o.instruments, o.exchange);
            dq_keys = tardis::DQ_COUNTERS.to_vec();
        }
        _ => {
            let o = match format {
                "bitstamp" => {
                    let mut c = bitstamp::Config {
                        instruments: map,
                        price_exp: pe,
                        size_exp: se,
                        ..Default::default()
                    };
                    if let Some(w) = opt_i64(spec, "taker_window_us")? {
                        c.taker_window_us = w;
                    }
                    bitstamp::convert_full(lines, &c)?
                }
                "bitfinex" => {
                    let mut c = bitfinex::Config {
                        instruments: map,
                        price_exp: pe,
                        size_exp: se,
                        flags: inp.meta.flags.unwrap_or(0),
                        ..Default::default()
                    };
                    if let Some(f) = opt_i64(spec, "flags")? {
                        c.flags = f.and_then(|x| u64::try_from(x).ok()).unwrap_or(0);
                    }
                    if let Some(m) = opt_i64(spec, "max_size")? {
                        c.max_size = m
                            .and_then(|x| u64::try_from(x).ok())
                            .filter(|x| *x > 0)
                            .unwrap_or(u64::MAX);
                    }
                    bitfinex::convert_full(lines, &c)?
                }
                _ => {
                    let mut c = coinbase::Config {
                        instruments: map,
                        price_exp: pe,
                        size_exp: se,
                        ..Default::default()
                    };
                    if let Some(Some(d)) = opt_i64(spec, "warmup_depth")? {
                        c.warmup_depth =
                            usize::try_from(d).map_err(|_| cfg_err("options.warmup_depth: must be >= 0"))?;
                    }
                    coinbase::convert_full(lines, &c)?
                }
            };
            let order_level_keys: &[&str] = match format {
                "bitstamp" => &["order_created", "order_changed", "order_deleted"],
                "bitfinex" => &["snapshots", "adds", "updates", "deletes", "delete_unknown"],
                _ => &["received", "open", "change", "done_canceled", "done_filled", "match"],
            };
            let order_level: u64 = order_level_keys.iter().filter_map(|k| o.counters.get(*k)).sum();
            let other_unknown: u64 = ["done_unknown", "done_never_opened", "change_unknown"]
                .iter()
                .filter_map(|k| o.counters.get(*k))
                .sum();
            if order_level + other_unknown == 0 {
                return Err(QtError::InputFormat {
                    location: format.into(),
                    msg: "no order-level records: an L2-only capture cannot be ingested as MBO".into(),
                });
            }
            dq.pre_snapshot_rows_skipped = o.pre_snapshot_rows_skipped;
            dq.sequence_gaps = o.sequence_gaps;
            dq.feed_gaps = o.feed_gaps;
            counters = o.counters;
            (events, l2, trades, instruments_map, exchange) = (o.events, Vec::new(), o.trades, o.instruments, None);
            dq_keys = o.dq_counters;
        }
    }
    let source_venue = spec.source_venue.clone().unwrap_or_else(|| match format {
        "tardis_l2" => exchange.clone().unwrap_or_else(|| "tardis".into()),
        f => f.to_string(),
    });

    // session dates
    let clock = match spec.tz {
        Some(SessionTz::AmericaNewYork) => SessionClock::Eastern,
        _ => SessionClock::Utc,
    };
    let fixed: Option<(i32, i64)> = match (&spec.session_date, spec.midnight_utc_ns) {
        (Some(d), m) => {
            let date = units::parse_date(d)?;
            Some((date, m.unwrap_or_else(|| units::midnight_ns(clock, date))))
        }
        // as qt_ingest (3.3): the date is derived from the midnight (the local date whose midnight it is, else the
        // UTC date of midnight + 12 h), never a rejection
        (None, Some(m)) => {
            if m < 0 {
                return Err(cfg_err(format!("midnight_utc_ns: must be >= 0, got {m}")));
            }
            let tz = match clock {
                SessionClock::Eastern => SessionTz::AmericaNewYork,
                SessionClock::Utc => SessionTz::Utc,
            };
            Some((qt_ingest::time::session_date_of_midnight(m, tz)?, m))
        }
        (None, None) => None,
    };
    let ts_all = events
        .iter()
        .map(|e| e.ts_event)
        .chain(l2.iter().map(|r| r.ts_event))
        .chain(trades.iter().map(|t| t.ts_event));
    for ts in ts_all {
        if ts < 0 {
            return Err(QtError::InputFormat {
                location: format.into(),
                msg: format!("negative timestamp {ts}"),
            });
        }
        if let Some((_, m)) = fixed {
            if ts < m || ts >= m + 86_400_000_000_000 {
                return Err(cfg_err(format!(
                    "session_date: record at {ts} ns is outside {} (local day from {m} ns); omit session_date and \
                     midnight_utc_ns to cut days at local midnight",
                    fixed.map_or_else(String::new, |(d, _)| qt_ingest::time::format_date(d))
                )));
            }
        }
    }
    let date_of = |ts: i64| fixed.map_or_else(|| units::session_date_of(clock, ts), |(d, _)| d);
    if !events.is_empty() {
        events = reseed_at_boundaries(events, date_of, rule_set, &mut counters);
    }

    // instruments: one row per (instrument, session date) present
    let id_to_sym: BTreeMap<u32, &String> = instruments_map.iter().map(|(s, &i)| (i, s)).collect();
    let mut present: BTreeMap<(i32, u32), ()> = BTreeMap::new();
    let mut px_gcd: HashMap<u32, u64> = HashMap::new();
    let mut sz_gcd: HashMap<u32, u64> = HashMap::new();
    let mut note = |inst: u32, px: Option<i64>, sz: u64| {
        if let Some(p) = px.filter(|p| *p != qt_types::UNDEF_PRICE) {
            let g = px_gcd.entry(inst).or_insert(0);
            *g = units::gcd(*g, p.unsigned_abs());
        }
        if sz > 0 {
            let g = sz_gcd.entry(inst).or_insert(0);
            *g = units::gcd(*g, sz);
        }
    };
    for e in &events {
        present.insert((date_of(e.ts_event), e.instrument_id), ());
        note(e.instrument_id, Some(e.price), e.size);
    }
    for r in &l2 {
        present.insert((date_of(r.ts_event), r.instrument_id), ());
        note(r.instrument_id, r.price, r.new_qty.max(r.trade_qty));
    }
    for t in &trades {
        present.insert((date_of(t.ts_event), t.instrument_id), ());
        note(t.instrument_id, Some(t.price), t.size);
    }
    let tick = spec.tick_size.as_deref().map(|s| units::price(s, pe)).transpose()?;
    let lot = spec.lot_size.as_deref().map(|s| units::size(s, se)).transpose()?;
    if tick.is_some_and(|t| t <= 0) || lot == Some(0) {
        return Err(cfg_err("tick_size / lot_size: must be positive"));
    }
    let currency_opt = spec.options.get("currency").and_then(Value::as_str).map(str::to_string);
    let tz_name = if clock == SessionClock::Eastern {
        "America/New_York"
    } else {
        "UTC"
    };
    let mut instruments = Vec::new();
    for &(date, inst) in present.keys() {
        let symbol = id_to_sym.get(&inst).map(|s| (*s).clone()).unwrap_or_default();
        let midnight = fixed.map_or_else(|| units::midnight_ns(clock, date), |(_, m)| m);
        let next = fixed.map_or_else(|| units::midnight_ns(clock, date + 1), |(_, m)| m + 86_400_000_000_000);
        let open_s = spec.open_s.unwrap_or(0);
        let close_s = spec
            .close_s
            .unwrap_or_else(|| u32::try_from((next - midnight) / 1_000_000_000).unwrap_or(86_400));
        if open_s > close_s {
            return Err(cfg_err(format!(
                "open_s: {open_s} is after close_s {close_s} (the length of the local day)"
            )));
        }
        instruments.push(Instrument {
            instrument_id: inst,
            session_date: date,
            symbol: symbol.clone(),
            venue,
            source_venue: source_venue.clone(),
            price_exp: pe,
            tick_size: tick.unwrap_or_else(|| {
                px_gcd
                    .get(&inst)
                    .copied()
                    .filter(|g| *g > 0)
                    .map_or(1, |g| i64::try_from(g).unwrap_or(1))
            }),
            lot_size: lot.unwrap_or_else(|| sz_gcd.get(&inst).copied().filter(|g| *g > 0).unwrap_or(1)),
            size_exp: se,
            contract_multiplier: 1.0,
            currency: currency_opt.clone().unwrap_or_else(|| guess_currency(&symbol)),
            session_tz: tz_name.into(),
            midnight_utc_ns: midnight,
            open_s,
            close_s,
            source: format.into(),
        });
    }
    counters.insert("tick_size_inferred".into(), u64::from(tick.is_none()));
    counters.insert("lot_size_inferred".into(), u64::from(lot.is_none()));

    // data quality
    if !events.is_empty() {
        let r = rules_for(rule_set);
        let mut last: HashMap<(i32, u32), i64> = HashMap::new();
        dq.records = events.len() as u64;
        for e in &events {
            if e.flags & flags::SNAPSHOT != 0 {
                dq.snapshot_records += 1;
            }
            if e.action == b'T' && e.side == b'N' && r.supports_hidden_orders {
                dq.hidden_prints += 1;
            }
            let l = last.entry((date_of(e.ts_event), e.instrument_id)).or_insert(i64::MIN);
            if e.ts_event < *l {
                dq.out_of_order_ts_event += 1;
            }
            *l = e.ts_event;
        }
    } else {
        let mut last: HashMap<(i32, u32), i64> = HashMap::new();
        for r in &l2 {
            let l = last.entry((date_of(r.ts_event), r.instrument_id)).or_insert(i64::MIN);
            if r.ts_event < *l {
                dq.out_of_order_ts_event += 1;
            }
            *l = r.ts_event;
        }
    }
    let mut ss = IndexMap::new();
    for k in dq_keys
        .iter()
        .copied()
        .chain(["day_boundary_reseeds", "day_boundary_reseeded_orders"])
    {
        if let Some(&v) = counters.get(k).filter(|v| **v > 0) {
            ss.insert(k.to_string(), v);
        }
    }
    dq.source_specific = ss;
    qt_ingest::dq::apply_verdict(&mut dq, None);

    let mut inputs = inp.files.clone();
    inputs.extend(inp.meta_path.clone());
    let echo = qt_schema::spec_echo(&serde_json::to_value(spec).map_err(|e| QtError::Engine(e.to_string()))?);
    let spec_echo = match echo {
        Value::Object(o) => o,
        _ => json::Object::new(),
    };
    Ok(Prepared {
        format,
        l2_only: format == "tardis_l2",
        venue,
        rule_set,
        source_venue,
        instruments,
        events,
        l2,
        trades,
        inputs,
        counters,
        data_quality: dq,
        spec_echo,
    })
}

/// Writes a prepared conversion as a dataset at `out` (SPEC 3.3 layout).
pub fn write(p: &Prepared, out: &Path) -> QtResult<json::IngestSummary> {
    let kind = if p.l2_only { DatasetKind::L2 } else { DatasetKind::Mbo };
    let mut w = DatasetWriter::create(out, kind, p.venue, p.format)?;
    w.set_source_venue(&p.source_venue);
    w.set_instruments(p.instruments.clone());
    for f in &p.inputs {
        w.add_input(f)?;
    }
    for e in &p.events {
        w.push_mbo(e)?;
    }
    for r in &p.l2 {
        w.push_l2(r)?;
    }
    for t in &p.trades {
        w.push_source_trade(t)?;
    }
    let mut summary = w.finish(p.data_quality.clone())?;
    summary.spec = p.spec_echo.clone();
    Ok(summary)
}

/// `ingest` for formats `bitstamp | bitfinex | coinbase | tardis_l2`.
pub fn ingest(spec: &specs::IngestSpec) -> QtResult<json::IngestSummary> {
    let out = spec.out.as_deref().ok_or_else(|| cfg_err("out: required"))?;
    let p = prepare(spec)?;
    if spec.strict && p.data_quality.verdict != json::Verdict::Clean {
        return Err(QtError::Strict(format!(
            "data-quality verdict {:?}: {}",
            p.data_quality.verdict,
            p.data_quality.reasons.join("; ")
        )));
    }
    write(&p, Path::new(out))
}
