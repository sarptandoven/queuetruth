//! `ingest()` for `itch | dbn | etcsv | parquet`: reads the inputs in feed order, routes records to
//! `(session_date, instrument_id)` partitions, builds the instrument rows and data-quality counters, and writes the
//! dataset through `DatasetWriter`.
//!
//! Sessions: ITCH files are one US/Eastern day each (date from `session_date`, the `MMDDYYYY` file name or
//! `midnight_utc_ns`, which must be that day's Eastern midnight): ITCH timestamps are nanoseconds since Eastern
//! midnight, so the records and the instrument rows always use America/New_York whatever the venue rule set, and a
//! `tz` other than America/New_York is `QT-CONFIG-001`. For the other formats an explicit `midnight_utc_ns` or
//! `session_date` puts every record in that one session; otherwise each record goes to the local date (in `tz`,
//! default US Eastern for venue nasdaq and UTC otherwise) containing its `ts_event` (snapshot records: their
//! `ts_recv`, the snapshot time, because a snapshot's `ts_event` may be the resting order's original time). Records
//! outside `[open_s, close_s)` are kept (they are only `out_of_session` for shadows): Nasdaq after-hours data up to
//! 20:00 ET (01:00 UTC in winter) stays on its Eastern date.
//!
//! Session window (`open_s` / `close_s`, seconds after local midnight): the 1.1 spec fields, else the v0.1
//! `options.open_s` / `options.close_s`, else the venue default (Nasdaq 34,200 / 57,600 = 09:30-16:00 ET, else 0 /
//! 86,400); a spec field and the option of the same name with different values are `QT-CONFIG-001`. The parquet
//! format keeps the window of explicit instrument rows unless one of these is set, which then overrides every row.
//!
//! Sequence gaps (DBN, etcsv, parquet feed files) are detected per channel over the whole feed (`seqgap`); ITCH
//! sequences are synthetic and contiguous, so ITCH datasets carry no sequence-gap counters.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use ::dbn::Schema;
use qt_schema::json::{DataQuality, FeedGap, IngestSummary, InputFile};
use qt_schema::rows::{TradeKind, TradeRow};
use qt_schema::specs::{IngestFormat, IngestSpec, SessionTz};
use qt_schema::{codec, io as qio, spec_echo};
use qt_types::{Instrument, MboEvent, QtError, QtResult, RuleSet, Ts, Venue, VenueRules, flags, rules_for};
use serde_json::Value;

use crate::dbn::{DbnReader, Symbology, utc_day};
use crate::dq::MboDqScan;
use crate::etcsv::{CsvKind, MboCsvReader, detect, first_trade_ts, read_instruments_csv, read_trades_csv};
use crate::guard::GuardLimits;
use crate::iex::{
    CaptureReader, FeedEvent, FeedReader as IexFeed, MapOutput as IexMapOutput, Mapper as IexMapper, MsgCtx,
    ParseError as IexParseError, parse as iex_parse,
};
use crate::itch::{MapOutput, Mapper, Reader, parse, stock_str};
use crate::seqgap::{FastMap, SeqGaps};
use crate::time::{Date, format_date, midnight_utc_ns, parse_date, session_date_of, session_date_of_midnight, tz_name};
use crate::units::{decimal_to_fixed, decimal_to_units, rescale};
use crate::{Dataset, DatasetKind, DatasetWriter};

const NASDAQ_OPEN_S: u32 = 34_200;
const NASDAQ_CLOSE_S: u32 = 57_600;
/// Largest `close_s` (two days).
const MAX_CLOSE_S: u32 = 172_800;
const ALLOWED_OPTIONS: [&str; 3] = ["open_s", "close_s", "memory_rows"];

fn cfg(msg: impl Into<String>) -> QtError {
    QtError::Config(msg.into())
}

/// Spec values shared by every format.
struct Common {
    venue: Venue,
    /// Rule set (`venue` is its record-semantics venue; `iex` runs on `Venue::Nasdaq`).
    rule_set: RuleSet,
    rules: VenueRules,
    format: &'static str,
    price_exp: i8,
    size_exp: i8,
    tick: Option<i64>,
    lot: Option<u64>,
    tz: SessionTz,
    fixed: Option<(i32, Ts)>,
    open_s: u32,
    close_s: u32,
    /// `open_s` / `close_s` set by the spec field or the option (not the venue default).
    open_set: Option<u32>,
    close_set: Option<u32>,
    source_venue: String,
    symbols: Option<BTreeSet<String>>,
    memory_rows: Option<usize>,
}

fn opt_u32(v: &Value, key: &str) -> QtResult<u32> {
    v.as_u64()
        .and_then(|x| u32::try_from(x).ok())
        .ok_or_else(|| cfg(format!("options.{key}: expected an integer")))
}

/// The 1.1 spec field, else the v0.1 option of the same name; both set to different values is an error.
fn field_or_option(field: &str, spec_v: Option<u32>, opt_v: Option<u32>) -> QtResult<Option<u32>> {
    match (spec_v, opt_v) {
        (Some(a), Some(b)) if a != b => Err(cfg(format!(
            "{field}: the spec field {field} = {a} and options.{field} = {b} disagree (set one of them)"
        ))),
        (Some(a), _) | (None, Some(a)) => Ok(Some(a)),
        (None, None) => Ok(None),
    }
}

fn check_window(open_s: u32, close_s: u32, what: &str) -> QtResult<()> {
    if open_s > close_s || close_s > MAX_CLOSE_S {
        return Err(cfg(format!(
            "open_s / close_s: {open_s} / {close_s} out of range{what} (need open_s <= close_s <= {MAX_CLOSE_S} \
             seconds after local midnight)"
        )));
    }
    Ok(())
}

impl Common {
    fn new(spec: &IngestSpec, format: &'static str) -> QtResult<Self> {
        let rule_set = if format == IEX_FORMAT {
            let rs: RuleSet = spec.venue.parse()?;
            if rs != RuleSet::Iex {
                return Err(cfg(format!(
                    "venue: format {IEX_FORMAT} is IEX DEEP+ data and uses rule set iex, got {:?}",
                    spec.venue
                )));
            }
            rs
        } else {
            RuleSet::from(spec.venue.parse::<Venue>()?)
        };
        let venue = rule_set.venue();
        if !(-12..=0).contains(&spec.price_exp) {
            return Err(cfg(format!("price_exp: must be in -12..=0, got {}", spec.price_exp)));
        }
        let size_exp = spec.size_exp.unwrap_or(0);
        if !(-18..=18).contains(&size_exp) {
            return Err(cfg(format!("size_exp: must be in -18..=18, got {size_exp}")));
        }
        let tick = match &spec.tick_size {
            Some(s) => {
                let t = decimal_to_fixed(s, spec.price_exp)?;
                if t <= 0 {
                    return Err(cfg(format!("tick_size: must be > 0, got {s:?}")));
                }
                Some(t)
            }
            None => None,
        };
        let lot = match &spec.lot_size {
            Some(s) => {
                let l = decimal_to_units(s, size_exp)?;
                if l == 0 {
                    return Err(cfg(format!("lot_size: must be > 0, got {s:?}")));
                }
                Some(l)
            }
            None => None,
        };
        let tz = if format == "itch" {
            match spec.tz {
                None | Some(SessionTz::AmericaNewYork) => SessionTz::AmericaNewYork,
                Some(other) => {
                    return Err(cfg(format!(
                        "tz: ITCH timestamps are nanoseconds since US/Eastern midnight, so format itch needs tz \
                         America/New_York (or null), got {}",
                        tz_name(other)
                    )));
                }
            }
        } else {
            spec.tz.unwrap_or(if venue == Venue::Nasdaq {
                SessionTz::AmericaNewYork
            } else {
                SessionTz::Utc
            })
        };
        let fixed = match (spec.midnight_utc_ns, &spec.session_date) {
            (Some(m), Some(d)) => Some((parse_date(d)?, m)),
            (Some(m), None) => {
                if m < 0 {
                    return Err(cfg(format!("midnight_utc_ns: must be >= 0, got {m}")));
                }
                Some((session_date_of_midnight(m, tz)?, m))
            }
            (None, Some(d)) => {
                let d = parse_date(d)?;
                Some((d, midnight_utc_ns(d, tz)?))
            }
            (None, None) => None,
        };
        let (mut opt_open, mut opt_close) = (None, None);
        let mut memory_rows = None;
        for (k, v) in &spec.options {
            match k.as_str() {
                "open_s" => opt_open = Some(opt_u32(v, k)?),
                "close_s" => opt_close = Some(opt_u32(v, k)?),
                "memory_rows" => memory_rows = Some(opt_u32(v, k)? as usize),
                _ => {
                    return Err(cfg(format!(
                        "options.{k}: unknown option for format {format} (allowed: {})",
                        ALLOWED_OPTIONS.join(", ")
                    )));
                }
            }
        }
        let open_set = field_or_option("open_s", spec.open_s, opt_open)?;
        let close_set = field_or_option("close_s", spec.close_s, opt_close)?;
        let (open_d, close_d) = if venue == Venue::Nasdaq {
            (NASDAQ_OPEN_S, NASDAQ_CLOSE_S)
        } else {
            (0, 86_400)
        };
        let (open_s, close_s) = (open_set.unwrap_or(open_d), close_set.unwrap_or(close_d));
        check_window(open_s, close_s, "")?;
        let source_venue = spec.source_venue.clone().unwrap_or_else(|| rule_set.name().to_owned());
        let symbols = spec
            .symbols
            .as_ref()
            .map(|v| v.iter().map(|s| s.trim().to_ascii_uppercase()).collect());
        Ok(Self {
            venue,
            rule_set,
            rules: rules_for(rule_set),
            format,
            price_exp: spec.price_exp,
            size_exp,
            tick,
            lot,
            tz,
            fixed,
            open_s,
            close_s,
            open_set,
            close_set,
            source_venue,
            symbols,
            memory_rows,
        })
    }

    fn selected(&self, symbol: &str) -> bool {
        self.symbols
            .as_ref()
            .is_none_or(|s| s.contains(&symbol.to_ascii_uppercase()))
    }

    #[inline]
    fn date_of(&self, ts: Ts) -> QtResult<i32> {
        match self.fixed {
            Some((d, _)) => Ok(d),
            None => session_date_of(ts, self.tz),
        }
    }

    fn midnight(&self, date: i32) -> QtResult<Ts> {
        match self.fixed {
            Some((d, m)) if d == date => Ok(m),
            _ => midnight_utc_ns(date, self.tz),
        }
    }

    /// Venue default tick (Nasdaq equities: 0.01) at `price_exp`, if representable.
    fn default_tick(&self) -> Option<i64> {
        if self.venue == Venue::Nasdaq {
            rescale(1, -2, self.price_exp)
        } else {
            None
        }
    }

    fn instrument(&self, iid: u32, date: i32, symbol: String, tick: i64, lot: u64) -> QtResult<Instrument> {
        Ok(self.instrument_at(iid, date, self.midnight(date)?, symbol, tick, lot))
    }

    fn instrument_at(&self, iid: u32, date: i32, midnight: Ts, symbol: String, tick: i64, lot: u64) -> Instrument {
        Instrument {
            instrument_id: iid,
            session_date: date,
            symbol,
            venue: self.venue,
            source_venue: self.source_venue.clone(),
            price_exp: self.price_exp,
            tick_size: tick,
            lot_size: lot,
            size_exp: self.size_exp,
            contract_multiplier: 1.0,
            currency: "USD".into(),
            session_tz: tz_name(self.tz).into(),
            midnight_utc_ns: midnight,
            open_s: self.open_s,
            close_s: self.close_s,
            source: self.format.into(),
        }
    }

    fn writer(&self, out: &Path, kind: DatasetKind) -> QtResult<DatasetWriter> {
        let mut w = DatasetWriter::create(out, kind, self.venue, self.format)?;
        w.set_source_venue(&self.source_venue);
        w.set_rule_set(Some(self.rule_set));
        w.set_validation_status(self.rules.validation);
        if let Some(n) = self.memory_rows {
            w.set_memory_rows(n);
        }
        Ok(w)
    }
}

/// Session date of a timestamp with a one-session cache (`Common::date_of` costs up to three midnight
/// conversions per call).
struct Dates {
    lo: Ts,
    hi: Ts,
    date: i32,
}

impl Dates {
    fn new() -> Self {
        Self { lo: 1, hi: 0, date: 0 }
    }

    #[inline]
    fn of(&mut self, c: &Common, ts: Ts) -> QtResult<i32> {
        if self.lo <= ts && ts < self.hi {
            return Ok(self.date);
        }
        let d = c.date_of(ts)?;
        let (lo, hi) = match c.fixed {
            Some(_) => (Ts::MIN, Ts::MAX),
            None => match (midnight_utc_ns(d, c.tz), midnight_utc_ns(d.saturating_add(1), c.tz)) {
                (Ok(a), Ok(b)) => (a, b),
                _ => (1, 0),
            },
        };
        (self.lo, self.hi, self.date) = (lo, hi, d);
        Ok(d)
    }
}

/// Routing time of a record: `ts_event`, or `ts_recv` (the snapshot time) for snapshot records, whose `ts_event`
/// may be the resting order's original time (Databento MBO snapshots, including the midnight-UTC ones).
#[inline]
fn route_ts(e: &MboEvent) -> Ts {
    if e.flags & flags::SNAPSHOT != 0 {
        e.ts_recv
    } else {
        e.ts_event
    }
}

/// Counts one record; a snapshot record is checked for `ts_event` order at its routing time (`route_ts`), so the
/// original order times inside a mid-file snapshot are not `out_of_order_ts_event` anomalies.
#[inline]
fn scan_record(scan: &mut MboDqScan, e: &MboEvent, date: i32, rules: &VenueRules) {
    if e.flags & flags::SNAPSHOT != 0 && e.ts_recv != e.ts_event {
        let mut v = *e;
        v.ts_event = e.ts_recv;
        scan.record(&v, date, rules);
    } else {
        scan.record(e, date, rules);
    }
}

/// Per-instrument metadata used to build the instrument rows of the partitions seen. `part_*` entries are per
/// `(session_date, instrument_id)` partition (DBN: resolved from the dated symbology) and win over the per-id maps.
#[derive(Default)]
struct Templates {
    symbol: BTreeMap<u32, String>,
    tick: BTreeMap<u32, i64>,
    part_symbol: BTreeMap<(i32, u32), String>,
    part_tick: BTreeMap<(i32, u32), i64>,
    /// Contract multiplier and currency per partition (DBN definitions; default 1 and USD).
    part_multiplier: BTreeMap<(i32, u32), f64>,
    part_currency: BTreeMap<(i32, u32), String>,
    seen: BTreeSet<(i32, u32)>,
    last_seen: Option<(i32, u32)>,
}

impl Templates {
    fn symbol_of(&self, iid: u32) -> String {
        self.symbol.get(&iid).cloned().unwrap_or_else(|| iid.to_string())
    }

    #[inline]
    fn see(&mut self, date: i32, iid: u32) {
        if self.last_seen != Some((date, iid)) {
            self.seen.insert((date, iid));
            self.last_seen = Some((date, iid));
        }
    }

    /// Instrument rows of the partitions seen and the labels of those whose tick was defaulted to 1 price unit.
    fn instruments(&self, c: &Common, scan: &mut MboDqScan) -> QtResult<(Vec<Instrument>, Vec<String>)> {
        let mut out = Vec::with_capacity(self.seen.len());
        let mut defaulted = Vec::new();
        let mut no_symbol = 0;
        for &(date, iid) in &self.seen {
            let symbol = self.part_symbol.get(&(date, iid)).or_else(|| self.symbol.get(&iid));
            if symbol.is_none() {
                no_symbol += 1;
            }
            let symbol = symbol.cloned().unwrap_or_else(|| iid.to_string());
            let tick = match c
                .tick
                .or_else(|| self.part_tick.get(&(date, iid)).copied())
                .or_else(|| self.tick.get(&iid).copied())
                .or_else(|| c.default_tick())
            {
                Some(t) => t,
                None => {
                    defaulted.push(format!("{symbol} (id {iid}, {})", format_date(date)));
                    1
                }
            };
            let mut row = c.instrument(iid, date, symbol, tick, c.lot.unwrap_or(1))?;
            if let Some(m) = self.part_multiplier.get(&(date, iid)) {
                row.contract_multiplier = *m;
            }
            if let Some(cur) = self.part_currency.get(&(date, iid)) {
                row.currency.clone_from(cur);
            }
            out.push(row);
        }
        scan.add_source("instruments_default_tick_size", defaulted.len() as u64);
        scan.add_source("instruments_without_symbol", no_symbol);
        Ok((out, defaulted))
    }

    /// Sets the instrument rows on the writer and warns loudly about defaulted ticks (review M7: a 1-unit tick
    /// makes tick-walking consumers crawl through every price unit between levels).
    fn apply(&self, c: &Common, scan: &mut MboDqScan, w: &mut DatasetWriter) -> QtResult<()> {
        let (rows, defaulted) = self.instruments(c, scan)?;
        w.set_instruments(rows);
        if !defaulted.is_empty() {
            w.add_warning(default_tick_warning(c, &defaulted));
        }
        Ok(())
    }
}

fn default_tick_warning(c: &Common, defaulted: &[String]) -> String {
    let shown: Vec<&str> = defaulted.iter().take(10).map(String::as_str).collect();
    let more = if defaulted.len() > 10 {
        format!(" and {} more", defaulted.len() - 10)
    } else {
        String::new()
    };
    let source = match c.format {
        "dbn" => " or a DBN definition file among the inputs",
        "etcsv" => " or an instruments.csv with min_price_increment",
        _ => "",
    };
    format!(
        "tick_size defaulted to 1 price unit (1e{}) on venue {} for {} instrument partition(s) with no tick from \
         the spec or the source: {}{more}. Pass --tick-size (IngestSpec.tick_size){source}; with a 1-unit tick, \
         tick-based consumers (zoo hbt depth mirror, calibrate, features) treat every price unit as a level and can \
         effectively hang on futures prices",
        c.price_exp,
        c.venue.name(),
        defaulted.len(),
        shown.join(", ")
    )
}

/// `instruments` (an instruments file) is read by the etcsv and parquet formats only (E40: never ignored).
fn no_instruments_file(spec: &IngestSpec, format: &str) -> QtResult<()> {
    if spec.instruments.is_some() {
        return Err(cfg(format!(
            "instruments: an instruments file is read by formats etcsv and parquet only, not {format} (symbols and \
             ticks come from the {format} input)"
        )));
    }
    Ok(())
}

fn input_paths(spec: &IngestSpec) -> QtResult<Vec<PathBuf>> {
    if spec.inputs.is_empty() {
        return Err(cfg("inputs: at least one input is required"));
    }
    spec.inputs
        .iter()
        .map(|f| {
            f.as_path()
                .map(PathBuf::from)
                .ok_or_else(|| cfg("inputs: local ingest needs {\"path\": ...} references"))
        })
        .collect()
}

pub(crate) fn ingest(spec: &IngestSpec) -> QtResult<IngestSummary> {
    let out = PathBuf::from(
        spec.out
            .as_deref()
            .ok_or_else(|| cfg("out: an output directory is required"))?,
    );
    let inputs = input_paths(spec)?;
    for p in &inputs {
        if !p.exists() {
            return Err(QtError::InputMissing {
                path: p.display().to_string(),
                msg: "no such file".into(),
            });
        }
    }
    let mut summary = match spec.format {
        IngestFormat::Itch => ingest_itch(spec, &inputs, &out)?,
        IngestFormat::Dbn => ingest_dbn(spec, &inputs, &out)?,
        IngestFormat::Etcsv => ingest_etcsv(spec, &inputs, &out)?,
        IngestFormat::Parquet => ingest_parquet(spec, &inputs, &out)?,
        IngestFormat::IexDeepPlus => ingest_iex(spec, &inputs, &out)?,
        IngestFormat::NyseIbf => crate::nyse::ingest(spec, &inputs, &out)?,
        other => {
            return Err(cfg(format!(
                "format: {:?} is ingested by qt_crypto::ingest, not qt_ingest",
                serde_json::to_value(other).unwrap_or(Value::Null)
            )));
        }
    };
    let v = serde_json::to_value(spec).map_err(|e| QtError::Engine(format!("spec echo: {e}")))?;
    if let Value::Object(o) = spec_echo(&v) {
        summary.spec = o;
    }
    // `IngestSpec.strict` (SPEC 4.2, as `qt_crypto::ingest`): a verdict other than clean is `QT-DATA-002`. The MBO
    // formats stream to `out`, so the dataset is written before the verdict is known; the error names it.
    if spec.strict && summary.data_quality.verdict != qt_schema::json::Verdict::Clean {
        return Err(QtError::Strict(format!(
            "data-quality verdict {:?}: {} (dataset written to {} without passing the strict gate)",
            summary.data_quality.verdict,
            summary.data_quality.reasons.join("; "),
            out.display()
        )));
    }
    Ok(summary)
}

/// Input provenance (size + sha256) computed on a background thread while the input is parsed.
struct InputsJob(std::thread::JoinHandle<QtResult<Vec<InputFile>>>);

impl InputsJob {
    fn start(inputs: &[PathBuf]) -> Self {
        let mut files = Vec::new();
        for p in inputs {
            if p.is_file() {
                files.push(p.clone());
            } else if p.is_dir() && p.join("manifest.json").is_file() {
                files.push(p.join("manifest.json"));
            }
        }
        Self(std::thread::spawn(move || {
            let mut out = Vec::with_capacity(files.len());
            for p in files {
                let bytes = std::fs::metadata(&p)
                    .map_err(|e| QtError::InputMissing {
                        path: p.display().to_string(),
                        msg: e.to_string(),
                    })?
                    .len();
                out.push(InputFile {
                    path: p.display().to_string(),
                    bytes,
                    sha256: qio::sha256_file(&p)?,
                });
            }
            Ok(out)
        }))
    }

    fn join(self, w: &mut DatasetWriter) -> QtResult<()> {
        let v = self
            .0
            .join()
            .map_err(|_| QtError::Engine("input hashing thread panicked".into()))??;
        for f in v {
            w.add_input_file(f);
        }
        Ok(())
    }
}

// ---- ITCH -------------------------------------------------------------------------------------------------------

fn ingest_itch(spec: &IngestSpec, inputs: &[PathBuf], out: &Path) -> QtResult<IngestSummary> {
    let c = Common::new(spec, "itch")?;
    no_instruments_file(spec, "itch")?;
    // Always US/Eastern (Common::new rejects any other tz for ITCH): ITCH ns are since Eastern midnight.
    let tz = c.tz;
    if inputs.len() > 1 && (spec.session_date.is_some() || spec.midnight_utc_ns.is_some()) {
        return Err(cfg(
            "session_date / midnight_utc_ns: only valid with a single ITCH input (dates come from file names)",
        ));
    }
    let eastern = |m: Ts, d: i32| -> QtResult<()> {
        let ny = midnight_utc_ns(d, tz)?;
        if m != ny {
            return Err(cfg(format!(
                "midnight_utc_ns: {m} is not the US/Eastern midnight of {} ({ny}); ITCH timestamps are nanoseconds \
                 since Eastern midnight",
                format_date(d)
            )));
        }
        Ok(())
    };
    let symbols: Option<Vec<String>> = spec.symbols.clone();
    let tick = match c.tick {
        Some(t) => t,
        None => rescale(1, -2, c.price_exp).ok_or_else(|| cfg("price_exp: ITCH tick 0.01 not representable"))?,
    };
    let lot = c.lot.unwrap_or(1);
    let mut plan = Vec::with_capacity(inputs.len());
    let mut dates = BTreeSet::new();
    for path in inputs {
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        let (date, midnight) = match (&spec.session_date, spec.midnight_utc_ns) {
            (Some(d), m) => {
                let d = parse_date(d)?;
                let m = m.map_or_else(|| midnight_utc_ns(d, tz), Ok)?;
                eastern(m, d)?;
                (d, m)
            }
            (None, Some(m)) => {
                let d = session_date_of_midnight(m, tz)?;
                eastern(m, d)?;
                (d, m)
            }
            (None, None) => {
                let d = Date::from_itch_filename(&name).ok_or_else(|| {
                    cfg(format!(
                        "session_date: cannot read MMDDYYYY from {name:?}; pass session_date (--date)"
                    ))
                })?;
                (d.days_from_civil(), midnight_utc_ns(d.days_from_civil(), tz)?)
            }
        };
        if !dates.insert(date) {
            return Err(cfg(format!(
                "inputs: two ITCH files for session date {}",
                format_date(date)
            )));
        }
        Mapper::new(midnight, None, c.price_exp)?;
        plan.push((path, date, midnight));
    }
    let mut w = c.writer(out, DatasetKind::Mbo)?;
    let job = InputsJob::start(inputs);
    let mut scan = MboDqScan::new();
    let mut instruments: Vec<Instrument> = Vec::new();
    for (path, date, midnight) in plan {
        // Two-stage pipeline: a reader thread frames, parses and maps; this thread routes and writes.
        let mut emitted = vec![false; 65_536];
        let (tx, rx) = std::sync::mpsc::sync_channel::<QtResult<MapOutput>>(4);
        let syms = symbols.clone();
        let price_exp = c.price_exp;
        let path_owned = path.clone();
        let producer = std::thread::spawn(move || -> QtResult<Mapper> {
            let mut mapper = Mapper::new(midnight, syms.as_deref(), price_exp)?;
            let run = |mapper: &mut Mapper| -> QtResult<()> {
                let mut reader = Reader::open(&path_owned)?;
                let mut block = MapOutput::default();
                let mut one = MapOutput::default();
                while let Some(bytes) = reader.next_message()? {
                    match parse(bytes) {
                        Ok(msg) => {
                            mapper.apply(&msg, &mut one)?;
                            block.mbo.append(&mut one.mbo);
                            block.trades.append(&mut one.trades);
                            if block.mbo.len() >= 32_768 {
                                let full = std::mem::replace(
                                    &mut block,
                                    MapOutput {
                                        mbo: Vec::with_capacity(33_000),
                                        trades: Vec::new(),
                                    },
                                );
                                if tx.send(Ok(full)).is_err() {
                                    return Ok(());
                                }
                            }
                        }
                        Err(_) => mapper.note_short_message(),
                    }
                }
                let _ = tx.send(Ok(block));
                Ok(())
            };
            if let Err(e) = run(&mut mapper) {
                let _ = tx.send(Err(e));
            }
            Ok(mapper)
        });
        let mut failure = None;
        for block in rx.iter() {
            let block = match block {
                Ok(b) => b,
                Err(e) => {
                    failure = Some(e);
                    break;
                }
            };
            // OC-28: a `Q` cross print is a side-`N` trade like a hidden print; its trade row (kind `cross`, same
            // instrument and sequence) tells them apart, so it is counted `cross_prints` (INFO), not a hidden print.
            let crosses: std::collections::HashSet<(u32, u64)> = block
                .trades
                .iter()
                .filter(|t| t.kind == TradeKind::Cross)
                .map(|t| (t.instrument_id, t.sequence))
                .collect();
            for e in &block.mbo {
                scan.record(e, date, &c.rules);
                if e.action == b'T' && e.side == b'N' && crosses.contains(&(e.instrument_id, e.sequence)) {
                    if c.rules.supports_hidden_orders {
                        scan.cross_print(); // `record` counted it as a hidden print
                    } else {
                        scan.add_source("cross_prints", 1);
                    }
                }
                w.push_mbo_at(e, date)?;
                emitted[e.instrument_id as usize & 0xffff] = true;
            }
            for t in &block.trades {
                w.push_source_trade_at(t, date)?;
            }
        }
        drop(rx);
        let mut mapper = producer
            .join()
            .map_err(|_| QtError::Engine("ITCH reader thread panicked".into()))??;
        if let Some(e) = failure {
            return Err(e);
        }
        let stats = mapper.finish().clone();
        let a = &stats.anomalies;
        let dq = scan.counters_mut();
        dq.unknown_order_refs += a.unknown_order_ref;
        dq.duplicate_adds += a.duplicate_order_ref;
        for (k, v) in a.named() {
            if k != "itch_unknown_order_ref" && k != "itch_duplicate_order_ref" {
                scan.add_source(k, v);
            }
        }
        scan.add_source("itch_symbols_not_found", stats.symbols_not_found.len() as u64);
        // Trading-halt spans of the file's selected instruments (SPEC 1.2): replay censors shadows there.
        scan.counters_mut().trading_halts.extend(mapper.take_trading_halts());
        let mut have = vec![false; 65_536];
        for (locate, dir) in &mapper.directory {
            let i = usize::from(*locate);
            if have[i] {
                scan.add_source("itch_duplicate_directory", 1);
                continue;
            }
            have[i] = true;
            instruments.push(c.instrument_at(
                u32::from(*locate),
                date,
                midnight,
                stock_str(&dir.stock).to_owned(),
                tick,
                lot,
            ));
        }
        let mut orphans = 0;
        for (i, e) in emitted.iter().enumerate() {
            if *e && !have[i] {
                orphans += 1;
                instruments.push(c.instrument_at(i as u32, date, midnight, i.to_string(), tick, lot));
            }
        }
        scan.add_source("itch_locate_without_directory", orphans);
    }
    w.set_instruments(instruments);
    job.join(&mut w)?;
    let mut dq = scan.finish();
    crate::halts::sort(&mut dq.trading_halts);
    w.finish(dq)
}

// ---- IEX DEEP+ --------------------------------------------------------------------------------------------------

/// `IngestSpec.format` name of IEX DEEP+.
const IEX_FORMAT: &str = "iex_deep_plus";

/// IEX HIST days are 8-18 GB of gzip inflating about 4.5x: the absolute inflation cap is raised for this format
/// (the ratio guard is unchanged).
const IEX_MAX_INFLATED: u64 = 512 << 30;

/// One transport message seen by the sequence-gap detector (all messages, kept or not, before the symbol filter).
#[derive(Clone, Copy)]
struct IexObs {
    seq: u64,
    channel: u16,
    ts: Ts,
    kept: Option<u32>,
    /// A new IEX-TP session on the channel: its numbering restarts.
    reset: bool,
}

#[derive(Default)]
struct IexBlock {
    mbo: Vec<MboEvent>,
    trades: Vec<TradeRow>,
    obs: Vec<IexObs>,
}

/// Mapper and transport counters of one input.
struct IexDone {
    mapper: IexMapper,
    feed: Vec<(String, u64)>,
    feed_stats: crate::iex::FeedStats,
}

/// IEX DEEP+ pcap / pcapng (gzip or raw). Timestamps are UTC ns, so records route to the local date (default
/// America/New_York) of their `ts_event`; `instrument_id`s are assigned per symbol in order of first appearance and
/// shared by every input of the ingest. Record `sequence` is the IEX-TP message sequence number and gaps are
/// detected over the whole feed (`sequence_meaningful` of the `iex` rule set).
fn ingest_iex(spec: &IngestSpec, inputs: &[PathBuf], out: &Path) -> QtResult<IngestSummary> {
    let c = Common::new(spec, IEX_FORMAT)?;
    no_instruments_file(spec, IEX_FORMAT)?;
    let mut mapper = IexMapper::new(spec.symbols.as_deref(), c.price_exp)?;
    let limits = GuardLimits {
        max_bytes: IEX_MAX_INFLATED,
        ..GuardLimits::default()
    };
    let mut w = c.writer(out, DatasetKind::Mbo)?;
    let job = InputsJob::start(inputs);
    let mut scan = MboDqScan::new();
    let mut gaps = SeqGaps::new(c.rules.sequence_meaningful);
    let mut dates = Dates::new();
    let mut tpl = Templates::default();
    let mut feed_totals: BTreeMap<String, u64> = BTreeMap::new();
    let mut fs_total = crate::iex::FeedStats::default();
    for path in inputs {
        mapper.reset_book();
        let (tx, rx) = std::sync::mpsc::sync_channel::<QtResult<IexBlock>>(4);
        let path_owned = path.clone();
        let producer = std::thread::spawn(move || -> QtResult<IexDone> {
            let mut feed = IexFeed::new(CaptureReader::open(&path_owned, limits)?);
            let mut block = IexBlock::default();
            let mut one = IexMapOutput::default();
            let mut last_ts: Ts = 0;
            let res = feed.for_each(|ev| {
                match ev {
                    FeedEvent::NewSession { channel, send_time, .. } => block.obs.push(IexObs {
                        seq: 0,
                        channel: u16::try_from(channel).unwrap_or(u16::MAX),
                        ts: send_time,
                        kept: None,
                        reset: true,
                    }),
                    FeedEvent::Message(m) => {
                        let channel = u16::try_from(m.channel).unwrap_or(u16::MAX);
                        let ctx = MsgCtx {
                            seq: m.seq,
                            channel,
                            send_time: m.send_time,
                        };
                        let mut kept = None;
                        match iex_parse(m.data) {
                            Ok(msg) => {
                                mapper.apply(ctx, &msg, &mut one)?;
                                if let Some(t) = msg.ts() {
                                    last_ts = t;
                                }
                                kept = one.mbo.first().map(|e| e.instrument_id);
                                block.mbo.append(&mut one.mbo);
                                block.trades.append(&mut one.trades);
                            }
                            Err(e) => mapper.note_unparsed(matches!(e, IexParseError::Short { .. })),
                        }
                        block.obs.push(IexObs {
                            seq: m.seq,
                            channel,
                            ts: last_ts,
                            kept,
                            reset: false,
                        });
                    }
                }
                if block.mbo.len() >= 32_768 || block.obs.len() >= 65_536 {
                    let full = std::mem::take(&mut block);
                    if tx.send(Ok(full)).is_err() {
                        return Err(QtError::Engine("IEX writer stopped".into()));
                    }
                }
                Ok(())
            });
            match res {
                Ok(()) => {
                    let _ = tx.send(Ok(block));
                }
                Err(e) => {
                    let _ = tx.send(Err(e));
                }
            }
            Ok(IexDone {
                feed: feed.named(),
                feed_stats: feed.stats.clone(),
                mapper,
            })
        });
        let mut failure = None;
        for block in rx.iter() {
            let block = match block {
                Ok(b) => b,
                Err(e) => {
                    failure = Some(e);
                    break;
                }
            };
            for o in &block.obs {
                let date = dates.of(&c, o.ts)?;
                let probe = MboEvent {
                    ts_event: o.ts,
                    ts_recv: o.ts,
                    action: b'N',
                    side: b'N',
                    sequence: o.seq,
                    channel_id: o.channel,
                    flags: if o.reset { flags::SNAPSHOT } else { 0 },
                    ..MboEvent::default()
                };
                gaps.observe(&probe, date, o.kept);
            }
            // OC-28: side-N prints flagged single-price cross are cross prints, not hidden prints.
            let crosses: std::collections::HashSet<(u32, u64)> = block
                .trades
                .iter()
                .filter(|t| t.kind == TradeKind::Cross)
                .map(|t| (t.instrument_id, t.sequence))
                .collect();
            for e in &block.mbo {
                let date = dates.of(&c, e.ts_event)?;
                scan.record(e, date, &c.rules);
                if e.action == b'T' && e.side == b'N' && crosses.contains(&(e.instrument_id, e.sequence)) {
                    scan.cross_print();
                }
                w.push_mbo_at(e, date)?;
                tpl.see(date, e.instrument_id);
            }
            for t in &block.trades {
                let date = dates.of(&c, t.ts_event)?;
                w.push_source_trade_at(t, date)?;
            }
        }
        drop(rx);
        let done = producer
            .join()
            .map_err(|_| QtError::Engine("IEX reader thread panicked".into()))??;
        if let Some(e) = failure {
            return Err(e);
        }
        mapper = done.mapper;
        for (k, v) in done.feed {
            *feed_totals.entry(k).or_insert(0) += v;
        }
        let f = done.feed_stats;
        fs_total.duplicate_messages += f.duplicate_messages;
        fs_total.other_protocol_segments += f.other_protocol_segments;
    }
    let stats = mapper.finish().clone();
    let a = &stats.anomalies;
    let dq = scan.counters_mut();
    dq.unknown_order_refs += a.unknown_order_ref;
    dq.duplicate_adds += a.duplicate_order_ref;
    for (k, v) in a.named() {
        if k != "iex_unknown_order_ref" && k != "iex_duplicate_order_ref" {
            scan.add_source(k, v);
        }
    }
    for (k, v) in &feed_totals {
        scan.add_source(k, *v);
    }
    scan.add_source("iex_symbols_not_found", stats.symbols_not_found.len() as u64);
    // INFO counters (dq::INFO_SOURCE_COUNTERS): volumes, never part of the verdict.
    scan.add_source("iex_modify_maintain_priority", stats.modify_maintain_priority);
    scan.add_source("iex_modify_reset_priority", stats.modify_reset_priority);
    scan.add_source("iex_modify_noop", stats.modify_noop);
    scan.add_source("iex_duplicate_messages", fs_total.duplicate_messages);
    scan.add_source("iex_other_protocol_segments", fs_total.other_protocol_segments);
    for (i, info) in mapper.instruments.iter().enumerate() {
        if info.selected {
            tpl.symbol.insert(i as u32 + 1, info.symbol.clone());
        }
    }
    tpl.apply(&c, &mut scan, &mut w)?;
    job.join(&mut w)?;
    // Trading-halt spans (SPEC 1.2): one still open at the end ends at the end of the last message's session day.
    let end_of_input = match stats.last_ts {
        Some(t) if t > 0 => {
            let d = dates.of(&c, t)?;
            midnight_utc_ns(d + 1, c.tz)?
        }
        _ => Ts::MAX,
    };
    // A span names an instrument of `instruments.parquet`: rows are written for instruments with records only, so
    // the spans of a symbol without records (halted all session) are dropped.
    let mut halts = mapper.take_trading_halts(end_of_input);
    let with_rows: BTreeSet<u32> = tpl.seen.iter().map(|&(_, iid)| iid).collect();
    halts.retain(|h| with_rows.contains(&h.instrument_id));
    let mut dq = scan.finish();
    dq.trading_halts = halts;
    gaps.finish(&mut dq);
    w.finish(dq)
}

// ---- DBN --------------------------------------------------------------------------------------------------------

/// Resolves DBN instrument ids per (id, UTC date of the record, session date) through the dated symbology:
/// symbol, symbol filter, and the partition id. When a session spans two UTC symbology dates (Eastern sessions:
/// after-hours up to 01:00 UTC in winter) and the record's symbol has another id on the session's own date, the
/// record is filed under that id, so one partition never mixes two symbols; a partition that would still mix two
/// symbols is `QT-INPUT-002`.
struct Resolver<'a> {
    sym: &'a Symbology,
    cache: FastMap<(u32, i32, i32), Option<u32>>,
    last: Option<((u32, i32, i32), Option<u32>)>,
    part: HashMap<(i32, u32), String>,
}

impl<'a> Resolver<'a> {
    fn new(sym: &'a Symbology) -> Self {
        Self {
            sym,
            cache: FastMap::default(),
            last: None,
            part: HashMap::new(),
        }
    }

    /// The partition instrument id of a record (`None`: dropped by the symbol filter).
    #[inline]
    fn resolve(&mut self, c: &Common, id: u32, day: i32, date: i32) -> QtResult<Option<u32>> {
        let key = (id, day, date);
        if let Some((k, v)) = self.last {
            if k == key {
                return Ok(v);
            }
        }
        let v = match self.cache.get(&key) {
            Some(v) => *v,
            None => {
                let v = self.compute(c, id, day, date)?;
                self.cache.insert(key, v);
                v
            }
        };
        self.last = Some((key, v));
        Ok(v)
    }

    fn compute(&mut self, c: &Common, id: u32, day: i32, date: i32) -> QtResult<Option<u32>> {
        let (iid, name) = match self.sym.symbol_at(id, day) {
            Some(s) if day != date => (self.sym.id_at(s, date).unwrap_or(id), Some(s)),
            Some(s) => (id, Some(s)),
            None => (id, self.sym.symbol_at(id, date)),
        };
        let label = name.map_or_else(|| iid.to_string(), str::to_owned);
        if !c.selected(&label) && !self.sym.aliases_at(id, day).into_iter().any(|a| c.selected(a)) {
            return Ok(None);
        }
        // Spec 1.2 non-goal: pro-rata and allocation books are rejected at ingest, at their first selected record.
        if let Some(m) = self.sym.non_fifo(iid, name, date) {
            return Err(self.sym.non_fifo_error(date, iid, name, m, |s| c.selected(s)));
        }
        if let Some(n) = name {
            match self.part.get(&(date, iid)) {
                Some(prev) if prev != n => {
                    return Err(QtError::InputFormat {
                        location: format!("instrument_id {iid} session {}", format_date(date)),
                        msg: format!(
                            "records of symbols {prev} and {n} would share one partition: the DBN symbology \
                             re-assigns instrument id {id} between UTC dates inside this session and {n} has no id \
                             on {}; ingest the UTC dates separately with session_date, or add the definition / \
                             symbology covering {}",
                            format_date(date),
                            format_date(date)
                        ),
                    });
                }
                Some(_) => {}
                None => {
                    self.part.insert((date, iid), n.to_owned());
                }
            }
        }
        Ok(Some(iid))
    }
}

/// A refused DBN ingest (a vintage mix, a non-FIFO book, ...) removes what it wrote to `out`, so the same `out` can
/// be used again. Only an `out` this ingest's writer created (or found empty) is cleaned; a non-empty `out` is never
/// touched (the writer refuses it).
fn ingest_dbn(spec: &IngestSpec, inputs: &[PathBuf], out: &Path) -> QtResult<IngestSummary> {
    let existed = out.exists();
    let mut created = false;
    let r = ingest_dbn_into(spec, inputs, out, &mut created);
    if r.is_err() && created {
        if existed {
            for e in std::fs::read_dir(out).into_iter().flatten().flatten() {
                let p = e.path();
                let _ = if p.is_dir() {
                    std::fs::remove_dir_all(&p)
                } else {
                    std::fs::remove_file(&p)
                };
            }
        } else {
            let _ = std::fs::remove_dir_all(out);
        }
    }
    r
}

fn ingest_dbn_into(spec: &IngestSpec, inputs: &[PathBuf], out: &Path, created: &mut bool) -> QtResult<IngestSummary> {
    let c = Common::new(spec, "dbn")?;
    no_instruments_file(spec, "dbn")?;
    let mut t = Templates::default();
    let mut sym = Symbology::new();
    let mut mbo_files: Vec<(u64, PathBuf)> = Vec::new();
    let mut trade_files: Vec<(u64, PathBuf)> = Vec::new();
    for p in inputs {
        let mut r = DbnReader::open(p)?;
        let (schema, start) = (r.metadata().schema, r.metadata().start);
        match schema {
            Some(Schema::Definition) => {
                r.scan_symbology(&mut sym)?;
            }
            Some(Schema::Mbo) | None => {
                if schema.is_none() {
                    r.scan_symbology(&mut sym)?;
                } else {
                    sym.add_metadata_from(r.metadata(), p);
                }
                mbo_files.push((start, p.clone()));
            }
            Some(Schema::Trades) => {
                sym.add_metadata_from(r.metadata(), p);
                trade_files.push((start, p.clone()));
            }
            Some(other) => {
                return Err(QtError::InputFormat {
                    location: p.display().to_string(),
                    msg: format!("unsupported DBN schema {other} (use mbo, trades or definition files)"),
                });
            }
        }
    }
    sym.check()?;
    mbo_files.sort_by_key(|(s, _)| *s);
    trade_files.sort_by_key(|(s, _)| *s);
    let mut w = c.writer(out, DatasetKind::Mbo)?;
    *created = true;
    let job = InputsJob::start(inputs);
    let mut scan = MboDqScan::new();
    let mut gaps = SeqGaps::new(c.rules.sequence_meaningful);
    let mut dates = Dates::new();
    let mut res = Resolver::new(&sym);
    let mut remapped = 0u64;
    for (_, p) in &mbo_files {
        let mut r = DbnReader::open(p)?;
        while let Some(mut e) = r.next_mbo(c.price_exp)? {
            let date = dates.of(&c, route_ts(&e))?;
            let Some(iid) = res.resolve(&c, e.instrument_id, utc_day(e.ts_recv), date)? else {
                gaps.observe(&e, date, None);
                continue;
            };
            if iid != e.instrument_id {
                remapped += 1;
                e.instrument_id = iid;
            }
            gaps.observe(&e, date, Some(iid));
            t.see(date, iid);
            scan_record(&mut scan, &e, date, &c.rules);
            w.push_mbo_at(&e, date)?;
        }
        // Other record types are expected in a mixed-schema file (INFO), not in an mbo-schema one.
        let mixed = r.metadata().schema.is_none();
        scan.add_source(
            if mixed {
                "dbn_mixed_schema_other_records"
            } else {
                "dbn_other_records_skipped"
            },
            r.skipped,
        );
        scan.add_source("dbn_gateway_error_records", r.gateway_errors);
    }
    for (_, p) in &trade_files {
        let mut r = DbnReader::open(p)?;
        while let Some(mut tr) = r.next_trade(c.price_exp, &c.rules)? {
            let date = dates.of(&c, tr.ts_event)?;
            let Some(iid) = res.resolve(&c, tr.instrument_id, utc_day(tr.ts_event), date)? else {
                continue;
            };
            tr.instrument_id = iid;
            t.see(date, iid);
            w.push_source_trade_at(&tr, date)?;
        }
        scan.add_source("dbn_other_records_skipped", r.skipped);
        scan.add_source("dbn_gateway_error_records", r.gateway_errors);
    }
    scan.add_source("dbn_instrument_id_remapped", remapped);
    t.part_symbol = res.part.into_iter().collect();
    for &(date, iid) in &t.seen {
        let name = t.part_symbol.get(&(date, iid)).map(String::as_str);
        if c.tick.is_none() {
            if let Some(tick) = sym
                .min_price_increment(iid, name, date)
                .and_then(|m| rescale(m, -9, c.price_exp))
            {
                t.part_tick.insert((date, iid), tick);
            }
        }
        if let Some(m) = sym.contract_multiplier(iid, name, date) {
            t.part_multiplier.insert((date, iid), m);
        }
        if let Some(cur) = sym.currency(iid, name, date) {
            t.part_currency.insert((date, iid), cur.to_owned());
        }
    }
    for (k, v) in sym.stats().counters() {
        scan.add_source(k, v);
    }
    if let Some((n, warning)) = sym.unverified_xnas_vintage(t.seen.iter().copied()) {
        scan.add_source("dbn_xnas_vintage_unverified", n);
        w.add_warning(warning);
    }
    t.apply(&c, &mut scan, &mut w)?;
    job.join(&mut w)?;
    let mut dq = scan.finish();
    gaps.finish(&mut dq);
    w.finish(dq)
}

// ---- etcsv ------------------------------------------------------------------------------------------------------

fn ingest_etcsv(spec: &IngestSpec, inputs: &[PathBuf], out: &Path) -> QtResult<IngestSummary> {
    let c = Common::new(spec, "etcsv")?;
    let mut t = Templates::default();
    let mut mbo_files = Vec::new();
    let mut trade_files = Vec::new();
    let mut inst_files = Vec::new();
    if let Some(f) = &spec.instruments {
        inst_files.push(PathBuf::from(
            f.as_path()
                .ok_or_else(|| cfg("instruments: a {\"path\": ...} reference"))?,
        ));
    }
    for p in inputs {
        match detect(p)? {
            CsvKind::Mbo => mbo_files.push((MboCsvReader::first_ts_event(p)?, p.clone())),
            CsvKind::Trades => trade_files.push((first_trade_ts(p)?, p.clone())),
            CsvKind::Instruments => inst_files.push(p.clone()),
        }
    }
    if mbo_files.is_empty() {
        return Err(cfg("inputs: etcsv needs at least one mbo.csv"));
    }
    for p in &inst_files {
        for i in read_instruments_csv(p)? {
            if let Some(mpi) = i.min_price_increment.filter(|m| *m > 0 && *m != i64::MAX) {
                if let Some(tick) = rescale(mpi, -9, c.price_exp) {
                    t.tick.insert(i.instrument_id, tick);
                }
            }
            t.symbol.insert(i.instrument_id, i.raw_symbol);
        }
    }
    mbo_files.sort_by_key(|(s, _)| *s);
    trade_files.sort_by_key(|(s, _)| *s);
    let mut w = c.writer(out, DatasetKind::Mbo)?;
    let mut hashed: Vec<PathBuf> = inputs.to_vec();
    if let Some(p) = inst_files.first().filter(|p| !inputs.contains(p)) {
        hashed.push(p.clone());
    }
    let job = InputsJob::start(&hashed);
    let mut scan = MboDqScan::new();
    let mut gaps = SeqGaps::new(c.rules.sequence_meaningful);
    let mut dates = Dates::new();
    let mut keep: BTreeMap<u32, bool> = BTreeMap::new();
    let mut last: Option<(u32, bool)> = None;
    for (_, p) in &mbo_files {
        let mut r = MboCsvReader::open(p)?;
        while let Some(mut e) = r.next()? {
            let sel = match last {
                Some((i, s)) if i == e.instrument_id => s,
                _ => {
                    let s = *keep
                        .entry(e.instrument_id)
                        .or_insert_with(|| c.selected(&t.symbol_of(e.instrument_id)));
                    last = Some((e.instrument_id, s));
                    s
                }
            };
            if !sel {
                if gaps.on() {
                    let date = dates.of(&c, route_ts(&e))?;
                    gaps.observe(&e, date, None);
                }
                continue;
            }
            e.price = rescale(e.price, -9, c.price_exp).ok_or_else(|| {
                QtError::Units(format!(
                    "{}:{}: price {} not exact at 10^{}",
                    p.display(),
                    r.line(),
                    e.price,
                    c.price_exp
                ))
            })?;
            let date = dates.of(&c, route_ts(&e))?;
            gaps.observe(&e, date, Some(e.instrument_id));
            t.see(date, e.instrument_id);
            scan_record(&mut scan, &e, date, &c.rules);
            w.push_mbo_at(&e, date)?;
        }
    }
    for (_, p) in &trade_files {
        read_trades_csv(p, |row| {
            let mut tr: TradeRow = row.to_trade(&c.rules);
            let sel = *keep
                .entry(tr.instrument_id)
                .or_insert_with(|| c.selected(&t.symbol_of(tr.instrument_id)));
            if !sel {
                return Ok(());
            }
            tr.price = rescale(tr.price, -9, c.price_exp)
                .ok_or_else(|| QtError::Units(format!("{}: trade price {} not exact", p.display(), tr.price)))?;
            let date = dates.of(&c, tr.ts_event)?;
            t.see(date, tr.instrument_id);
            w.push_source_trade_at(&tr, date)
        })?;
    }
    t.apply(&c, &mut scan, &mut w)?;
    job.join(&mut w)?;
    let mut dq = scan.finish();
    gaps.finish(&mut dq);
    w.finish(dq)
}

// ---- Parquet ----------------------------------------------------------------------------------------------------

#[derive(Default)]
struct Collected {
    /// `qt.mbo` files with whether they are in source feed order (standalone files) or one partition of an existing
    /// dataset (per instrument: feed order across instruments is gone, so its gaps come from that dataset).
    mbo: Vec<(PathBuf, bool)>,
    trades: Vec<PathBuf>,
    l2: Vec<PathBuf>,
    explicit: Vec<Instrument>,
    /// Roots of existing datasets whose ingest-time sequence counters and feed gaps are carried over.
    carried: BTreeSet<PathBuf>,
}

/// The dataset root of a partition file `<root>/<mbo|l2>/date=.../instrument_id=.../part-*.parquet`.
fn partition_root(p: &Path) -> Option<PathBuf> {
    let inst = p.parent()?;
    let date = inst.parent()?;
    let named = |d: &Path, prefix: &str| {
        d.file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| n.starts_with(prefix))
    };
    if !named(inst, "instrument_id=") || !named(date, "date=") {
        return None;
    }
    Some(date.parent()?.parent()?.to_owned())
}

impl Collected {
    fn add(&mut self, p: &Path, feed_order: bool) -> QtResult<()> {
        let r = qio::read_table(p)?;
        match r.name() {
            "qt.mbo" => self.mbo.push((p.to_owned(), feed_order)),
            "qt.trades" => self.trades.push(p.to_owned()),
            "qt.l2" => self.l2.push(p.to_owned()),
            "qt.instruments" => {
                for b in r {
                    self.explicit.extend(codec::batch_to_instruments(&b?)?);
                }
            }
            other => {
                return Err(QtError::InputFormat {
                    location: p.display().to_string(),
                    msg: format!("table {other} cannot be ingested (qt.mbo, qt.trades, qt.l2, qt.instruments)"),
                });
            }
        }
        Ok(())
    }
}

/// Honours the spec fields that describe instruments on explicit instrument rows of a parquet input (E40:
/// never silently ignored): `open_s` / `close_s` / `tick_size` / `lot_size` override every row; `tz`, `price_exp` and
/// `size_exp` must agree with the rows (records are copied without rescaling); `session_date` / `midnight_utc_ns`
/// cannot be combined with rows (the rows' midnights route the records).
fn apply_spec_to_rows(spec: &IngestSpec, c: &Common, rows: &mut [Instrument]) -> QtResult<()> {
    if spec.session_date.is_some() || spec.midnight_utc_ns.is_some() {
        return Err(cfg(
            "session_date / midnight_utc_ns: the explicit instrument rows of the parquet input route the records by \
             their own midnight_utc_ns; drop these fields or ingest without instrument rows",
        ));
    }
    for r in rows.iter_mut() {
        let at = || format!("instrument row {} {}", r.instrument_id, format_date(r.session_date));
        if let Some(tz) = spec.tz {
            if r.session_tz != tz_name(tz) {
                return Err(cfg(format!(
                    "tz: {} disagrees with session_tz {} of {}",
                    tz_name(tz),
                    r.session_tz,
                    at()
                )));
            }
        }
        if spec.price_exp != -9 && spec.price_exp != r.price_exp {
            return Err(cfg(format!(
                "price_exp: {} disagrees with price_exp {} of {} (parquet records are copied without rescaling)",
                spec.price_exp,
                r.price_exp,
                at()
            )));
        }
        if let Some(e) = spec.size_exp.filter(|e| *e != r.size_exp) {
            return Err(cfg(format!(
                "size_exp: {e} disagrees with size_exp {} of {} (parquet records are copied without rescaling)",
                r.size_exp,
                at()
            )));
        }
        if let Some(s) = &spec.tick_size {
            let t = decimal_to_fixed(s, r.price_exp)?;
            if t <= 0 {
                return Err(cfg(format!("tick_size: must be > 0, got {s:?}")));
            }
            r.tick_size = t;
        }
        if let Some(s) = &spec.lot_size {
            let l = decimal_to_units(s, r.size_exp)?;
            if l == 0 {
                return Err(cfg(format!("lot_size: must be > 0, got {s:?}")));
            }
            r.lot_size = l;
        }
        if let Some(o) = c.open_set {
            r.open_s = o;
        }
        if let Some(cl) = c.close_set {
            r.close_s = cl;
        }
        check_window(r.open_s, r.close_s, &format!(" for {}", at()))?;
    }
    Ok(())
}

/// Adds the ingest-time sequence counters and the feed gaps (those `keep` accepts) of existing datasets.
fn carry_gaps(roots: &BTreeSet<PathBuf>, keep: impl Fn(&FeedGap) -> bool, dq: &mut DataQuality) {
    for root in roots {
        let Ok(text) = std::fs::read_to_string(root.join("data_quality.json")) else {
            continue;
        };
        let Ok(src) = serde_json::from_str::<DataQuality>(&text) else {
            continue;
        };
        dq.sequence_gaps += src.sequence_gaps;
        dq.sequence_regressions += src.sequence_regressions;
        dq.feed_gaps.extend(src.feed_gaps.into_iter().filter(|g| keep(g)));
    }
    dq.feed_gaps.sort_by(|a, b| {
        (a.instrument_id, a.ts_start, a.ts_end, &a.reason).cmp(&(b.instrument_id, b.ts_start, b.ts_end, &b.reason))
    });
    dq.feed_gaps.dedup();
}

fn ingest_parquet(spec: &IngestSpec, inputs: &[PathBuf], out: &Path) -> QtResult<IngestSummary> {
    let c = Common::new(spec, "parquet")?;
    let mut col = Collected::default();
    for p in inputs {
        if p.is_dir() {
            let ds = Dataset::open(p)?;
            col.explicit.extend(ds.instruments.iter().cloned());
            col.carried.insert(p.clone());
            for part in ds.partitions() {
                for f in &part.files {
                    col.add(f, false)?;
                }
                if let Some(dir) = part.source_trades_dir().filter(|d| d.is_dir()) {
                    let mut fs: Vec<PathBuf> = std::fs::read_dir(&dir)
                        .map_err(|e| QtError::InputMissing {
                            path: dir.display().to_string(),
                            msg: e.to_string(),
                        })?
                        .filter_map(|e| e.ok().map(|e| e.path()))
                        .filter(|p| p.extension().is_some_and(|x| x == "parquet"))
                        .collect();
                    fs.sort();
                    col.trades.extend(fs);
                }
            }
        } else {
            match partition_root(p) {
                Some(root) => {
                    if root.join("data_quality.json").is_file() {
                        col.carried.insert(root);
                    }
                    col.add(p, false)?;
                }
                None => col.add(p, true)?,
            }
        }
    }
    if let Some(f) = &spec.instruments {
        let p = PathBuf::from(
            f.as_path()
                .ok_or_else(|| cfg("instruments: a {\"path\": ...} reference"))?,
        );
        col.add(&p, true)?;
    }
    let Collected {
        mbo,
        trades,
        l2,
        mut explicit,
        carried,
    } = col;
    if !mbo.is_empty() && !l2.is_empty() {
        return Err(cfg("inputs: qt.mbo and qt.l2 tables cannot be mixed in one dataset"));
    }
    if mbo.is_empty() && l2.is_empty() {
        return Err(cfg("inputs: no qt.mbo or qt.l2 table among the parquet inputs"));
    }
    let kind = if l2.is_empty() {
        DatasetKind::Mbo
    } else {
        DatasetKind::L2
    };
    let have_explicit = !explicit.is_empty();
    if have_explicit {
        explicit.sort_by_key(|i| (i.session_date, i.instrument_id));
        explicit.dedup_by_key(|i| (i.session_date, i.instrument_id));
        apply_spec_to_rows(spec, &c, &mut explicit)?;
    }
    let mut w = c.writer(out, kind)?;
    let job = InputsJob::start(inputs);
    let mut scan = MboDqScan::new();
    if have_explicit {
        w.set_instruments(explicit.clone());
    }
    // Symbols of explicit rows per (session date, id): ids are only unique per date (ITCH locate codes, DBN ids).
    let syms: BTreeMap<(i32, u32), String> = explicit
        .iter()
        .map(|i| ((i.session_date, i.instrument_id), i.symbol.clone()))
        .collect();
    let mut t = Templates::default();
    let mut dates = Dates::new();
    // Session date and symbol-filter verdict of a row (`None`: no instrument row and filtered out anyway).
    let mut place = |w: &DatasetWriter, iid: u32, ts: Ts| -> QtResult<Option<(i32, bool)>> {
        if !have_explicit {
            return Ok(Some((dates.of(&c, ts)?, c.selected(&iid.to_string()))));
        }
        match w.route(iid, ts) {
            Ok(d) => {
                let sel = match syms.get(&(d, iid)) {
                    Some(s) => c.selected(s),
                    None => c.selected(&iid.to_string()),
                };
                Ok(Some((d, sel)))
            }
            Err(_) if !c.selected(&iid.to_string()) => Ok(None),
            Err(e) => Err(e),
        }
    };
    let mut gaps = SeqGaps::new(c.rules.sequence_meaningful);
    for (p, feed_order) in &mbo {
        for b in qio::read_table_as(p, "qt.mbo")? {
            for e in codec::batch_to_mbo(&b?)? {
                let Some((date, sel)) = place(&w, e.instrument_id, route_ts(&e))? else {
                    continue;
                };
                if *feed_order {
                    gaps.observe(&e, date, sel.then_some(e.instrument_id));
                }
                if !sel {
                    continue;
                }
                t.see(date, e.instrument_id);
                scan_record(&mut scan, &e, date, &c.rules);
                w.push_mbo_at(&e, date)?;
            }
        }
    }
    for p in &l2 {
        for b in qio::read_table_as(p, "qt.l2")? {
            for r in codec::batch_to_l2(&b?)? {
                let Some((date, true)) = place(&w, r.instrument_id, r.ts_event)? else {
                    continue;
                };
                t.see(date, r.instrument_id);
                scan.counters_mut().records += 1;
                w.push_l2_at(&r, date)?;
            }
        }
    }
    for p in &trades {
        for b in qio::read_table_as(p, "qt.trades")? {
            for tr in codec::batch_to_trades(&b?)? {
                let Some((date, true)) = place(&w, tr.instrument_id, tr.ts_event)? else {
                    continue;
                };
                t.see(date, tr.instrument_id);
                w.push_source_trade_at(&tr, date)?;
            }
        }
    }
    if have_explicit {
        // Keep only rows of partitions present (explicit rows describe the data; extra rows are dropped).
        let rows: Vec<Instrument> = explicit
            .into_iter()
            .filter(|i| t.seen.contains(&(i.session_date, i.instrument_id)))
            .collect();
        w.set_instruments(rows);
    } else {
        t.apply(&c, &mut scan, &mut w)?;
    }
    let mut dq: DataQuality = scan.finish();
    gaps.finish(&mut dq);
    // A carried gap stays when the partition holding its end (by the final instrument rows) was kept.
    let seen = &t.seen;
    carry_gaps(
        &carried,
        |g| {
            w.route(g.instrument_id, g.ts_end)
                .is_ok_and(|d| seen.contains(&(d, g.instrument_id)))
        },
        &mut dq,
    );
    job.join(&mut w)?;
    w.finish(dq)
}
