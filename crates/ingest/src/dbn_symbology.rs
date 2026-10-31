//! Dated DBN symbology and instrument definitions (re-exported by [`crate::dbn`]).
//!
//! Sources, in lookup order:
//! - **metadata mappings** of every input (`stype_out = instrument_id`, UTC dates, end exclusive). A mapping is keyed
//!   by the *input* symbol of the query, so only a `raw_symbol` query (or, for files whose `stype_in` is mixed, a key
//!   that does not look like a parent `ROOT.FUT` / `ROOT.OPT` or continuous `ROOT.c.0` symbol) gives raw symbols;
//!   parent and continuous keys are kept as **aliases**: they select instruments in a symbol filter, and a
//!   continuous alias (never a parent) names an instrument when no definition does and it resolves to exactly that
//!   one instrument id on that date;
//! - **definition records** (`definition` files, or definition records inside a mixed-schema file such as a live
//!   capture): valid from the UTC date of `ts_recv` (`ts_event` when `ts_recv` is undefined) until superseded; they
//!   supply the raw symbol, `min_price_increment` (the tick), the contract multiplier
//!   (`min_price_increment_amount / min_price_increment`), the currency and `match_algorithm`. A delete (`D`) is
//!   counted but supplies nothing; undefined or non-positive increments are skipped;
//! - **symbol-mapping records** (live captures): the output symbol is a raw symbol when `stype_out` is
//!   `raw_symbol`; the input symbol is an alias.
//!
//! Every definition and symbol-mapping record is kept as a receipt in a per-id timeline in a total order
//! (receive time, then content, then source label), so the result never depends on the order of the inputs.
//!
//! [`Symbology::check`] refuses inputs that disagree: inputs from different Databento datasets; on one UTC date, a
//! raw symbol with two instrument ids or an id with two raw symbols; and `XNAS.ITCH` definitions of both vintages of
//! Databento's 2026-10-31 re-issue (which gives data before 2023-11-28 new instrument ids and keeps the Nasdaq stock
//! locate in the definitions' `raw_instrument_id`). [`Symbology::unverified_xnas_vintage`] names the partitions
//! whose vintage cannot be checked because no MBO metadata maps their raw symbol.

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::Path;

use ::dbn::{MatchAlgorithm, Metadata, SType, Schema};
use qt_types::{QtError, QtResult};

/// Nanoseconds per UTC day (DBN symbology dates are UTC dates).
const NS_PER_DAY: i64 = 86_400_000_000_000;

/// DBN undefined timestamp.
const UNDEF_TS: u64 = u64::MAX;

/// Most conflicts (or partitions) quoted in one message.
const MAX_CONFLICTS_SHOWN: usize = 5;

/// Most partitions or instruments quoted in a non-FIFO rejection.
const MAX_NON_FIFO_SHOWN: usize = 10;

/// Databento dataset of Nasdaq TotalView-ITCH history (the dataset of the 2026-10-31 re-issue).
pub const XNAS_ITCH: &str = "XNAS.ITCH";

/// 2023-11-28 (days since 1970-01-01): the re-issue gives `XNAS.ITCH` data before this UTC date new instrument ids
/// and keeps the ids from it on.
pub const XNAS_REISSUE_CUTOVER_DAY: i32 = 19_689;

/// UTC date (days since 1970-01-01) of a timestamp, as DBN symbology dates count.
pub fn utc_day(ts: i64) -> i32 {
    i32::try_from(ts.div_euclid(NS_PER_DAY)).unwrap_or(if ts < 0 { i32::MIN } else { i32::MAX })
}

fn utc_day_u(ts: u64) -> i32 {
    utc_day(i64::try_from(ts).unwrap_or(i64::MAX))
}

/// First timestamp after UTC date `day` (0 for dates before 1970-01-01).
fn day_end(day: i32) -> u64 {
    u64::try_from((i64::from(day) + 1).saturating_mul(NS_PER_DAY)).unwrap_or(0)
}

/// True when `ts` falls before 01:00 UTC on its date (the last after-hours hour of the previous Eastern session).
fn is_early(ts: u64) -> bool {
    i64::try_from(ts).is_ok_and(|t| t.rem_euclid(NS_PER_DAY) < NS_PER_DAY / 24)
}

/// `YYYY-MM-DD` of a UTC day number (proleptic Gregorian; Howard Hinnant's civil-from-days).
pub fn format_utc_day(day: i32) -> String {
    let z = i64::from(day) + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}-{m:02}-{d:02}")
}

/// `[start_day, end_day)` (days since 1970-01-01, UTC) of a metadata mapping interval (Julian day 2,440,588 is
/// 1970-01-01).
fn interval_days(i: &::dbn::MappingInterval) -> (i32, i32) {
    (
        i.start_date.to_julian_day() - 2_440_588,
        i.end_date.to_julian_day() - 2_440_588,
    )
}

/// True for a Databento continuous symbol: `ROOT.{c,n,v}.RANK` (`ES.c.0`, `CL.n.1`, `ZN.v.2`).
pub fn is_continuous_symbol(s: &str) -> bool {
    let parts: Vec<&str> = s.split('.').collect();
    matches!(parts.as_slice(), [root, rule, rank]
        if !root.is_empty()
            && matches!(*rule, "c" | "n" | "v")
            && !rank.is_empty()
            && rank.bytes().all(|b| b.is_ascii_digit()))
}

/// True when a metadata mapping key looks like a Databento parent (`ES.FUT`, `ES.OPT`, `BTC.SPOT`) or continuous
/// (`ES.c.0`, `CL.n.1`, `ZN.v.2`) symbol. Used only when the file's `stype_in` is mixed (unset).
pub fn looks_like_smart_symbol(s: &str) -> bool {
    let parent = matches!(s.split('.').collect::<Vec<_>>().as_slice(), [root, kind]
        if !root.is_empty() && matches!(*kind, "FUT" | "OPT" | "SPOT" | "MLEG"));
    parent || is_continuous_symbol(s)
}

/// Name of a DBN `match_algorithm` code (`F` FIFO, `C` pro-rata, ...; `unknown` for a byte `dbn` does not define).
pub fn match_algorithm_name(code: u8) -> &'static str {
    use MatchAlgorithm as M;
    match M::try_from(code) {
        Ok(M::Undefined) => "undefined",
        Ok(M::Fifo) => "FIFO",
        Ok(M::Configurable) => "configurable",
        Ok(M::ProRata) => "pro-rata",
        Ok(M::FifoLmm) => "FIFO with LMM allocation",
        Ok(M::ThresholdProRata) => "threshold pro-rata",
        Ok(M::FifoTopLmm) => "FIFO with top-order and LMM allocation",
        Ok(M::ThresholdProRataLmm) => "threshold pro-rata with LMM allocation",
        Ok(M::EurodollarFutures) => "Eurodollar futures allocation",
        Ok(M::TimeProRata) => "time pro-rata",
        Ok(M::InstitutionalPrioritization) => "institutional prioritization (two-pass FIFO)",
        Ok(M::Allocation) => "allocation (pro-rata with a top-order allocation)",
        Err(_) => "unknown",
    }
}

/// `match_algorithm` codes that say nothing (DBN `Undefined` is a space; zero bytes appear in older files).
fn match_algorithm_defined(code: u8) -> bool {
    code != b' ' && code != 0
}

/// One instrument definition record, independent of the DBN version it was stored in.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DbnDefinition {
    /// Databento instrument id (per dataset; dated, see the module docs).
    pub instrument_id: u32,
    /// Capture-server receive time, UTC ns (`u64::MAX` when undefined).
    pub ts_recv: u64,
    /// Matching-engine time, UTC ns (`u64::MAX` when undefined).
    pub ts_event: u64,
    /// Publisher's raw symbol (trimmed; empty when absent).
    pub raw_symbol: String,
    /// Publisher's own instrument id (the Nasdaq stock locate on the re-issued XNAS.ITCH history), when defined
    /// (0, `i32::MAX`, `u32::MAX` and `u64::MAX` are undefined).
    pub raw_instrument_id: Option<u64>,
    /// Tick at 1e-9, when defined and positive.
    pub min_price_increment: Option<i64>,
    /// Value of one tick in `currency`, at 1e-9, when defined and positive (CME ES: 12.5).
    pub min_price_increment_amount: Option<i64>,
    /// Display factor at 1e-9, when defined.
    pub display_factor: Option<i64>,
    /// Round-lot size, when defined and positive (an order-size convention, not a size increment).
    pub min_lot_size_round_lot: Option<i32>,
    /// Instrument class byte (`F` future, `K` stock, `S` spread, `C` / `P` option, ...).
    pub instrument_class: u8,
    /// Match algorithm byte (see [`match_algorithm_name`]; a space when undefined).
    pub match_algorithm: u8,
    /// Security update action byte: `A` add, `M` modify, `D` delete (`?` for any other byte).
    pub security_update_action: u8,
    /// Exchange MIC or code.
    pub exchange: String,
    /// Underlying asset (product) code.
    pub asset: String,
    /// Security type (`FUT`, `EQUITY`, ...).
    pub security_type: String,
    /// Price currency.
    pub currency: String,
}

impl DbnDefinition {
    /// Time from which the definition applies: `ts_recv`, else `ts_event` (`None` when both are undefined).
    pub fn valid_from(&self) -> Option<u64> {
        [self.ts_recv, self.ts_event].into_iter().find(|t| *t != UNDEF_TS)
    }

    /// True for a delete (`security_update_action = D`).
    pub fn is_delete(&self) -> bool {
        self.security_update_action == b'D'
    }

    /// Contract multiplier: the value of one price unit, `min_price_increment_amount / min_price_increment`, when
    /// both are defined and positive (CME ES: 12.5 / 0.25 = 50; ZN: 15.625 / 0.015625 = 1000).
    pub fn contract_multiplier(&self) -> Option<f64> {
        multiplier(self.min_price_increment_amount, self.min_price_increment)
    }
}

fn multiplier(amount: Option<i64>, mpi: Option<i64>) -> Option<f64> {
    match (amount, mpi) {
        (Some(a), Some(m)) if a > 0 && m > 0 => Some(a as f64 / m as f64),
        _ => None,
    }
}

/// Counters of what [`Symbology`] read (reported as data-quality source counters; all but
/// `dbn_definitions_undated` are INFO counters that never raise the verdict, `qt_ingest::dq::INFO_SOURCE_COUNTERS`).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SymbologyStats {
    /// Definition records added (deletes excluded).
    pub definitions: u64,
    /// Definition records identical to the record before them in their id's timeline (midnight snapshots).
    pub definitions_repeated: u64,
    /// Delete definition records (counted, not applied).
    pub definition_deletes: u64,
    /// Definitions without a defined positive `min_price_increment` (the XNAS.ITCH definitions we could inspect
    /// carry none).
    pub definitions_without_tick: u64,
    /// Definitions with neither `ts_recv` nor `ts_event` (not applied).
    pub definitions_undated: u64,
    /// Symbol-mapping records added.
    pub symbol_mapping_records: u64,
    /// Metadata mapping intervals kept as aliases (parent / continuous / other non-raw input symbols).
    pub alias_intervals: u64,
}

impl SymbologyStats {
    /// Non-zero counters as `(name, value)` for the data-quality `source_specific` map.
    pub fn counters(&self) -> Vec<(&'static str, u64)> {
        [
            ("dbn_definition_repeats", self.definitions_repeated),
            ("dbn_definition_deletes", self.definition_deletes),
            ("dbn_definitions_without_tick", self.definitions_without_tick),
            ("dbn_definitions_undated", self.definitions_undated),
            ("dbn_symbol_mapping_records", self.symbol_mapping_records),
            ("dbn_alias_mapping_intervals", self.alias_intervals),
        ]
        .into_iter()
        .filter(|(_, v)| *v > 0)
        .collect()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum EntryKind {
    Definition,
    Mapping,
}

/// What a definition or symbol-mapping record says (interned: midnight snapshots repeat it every day).
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
struct Content {
    kind: EntryKind,
    symbol: Box<str>,
    alias: Box<str>,
    mpi: Option<i64>,
    mpi_amount: Option<i64>,
    match_algorithm: u8,
    raw_instrument_id: Option<u64>,
    currency: Box<str>,
}

impl Content {
    fn multiplier(&self) -> Option<f64> {
        multiplier(self.mpi_amount, self.mpi)
    }
}

/// One record received: when, what (an index into the interned contents) and from which source.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Receipt {
    at: u64,
    content: u32,
    src: u32,
}

/// The total order of receipts in a timeline: receive time, then content, then source label. It does not depend on
/// the order in which records were added.
fn receipt_order(a: &Receipt, b: &Receipt, contents: &[Content], sources: &[String]) -> Ordering {
    a.at.cmp(&b.at)
        .then_with(|| contents[a.content as usize].cmp(&contents[b.content as usize]))
        .then_with(|| sources[a.src as usize].cmp(&sources[b.src as usize]))
}

/// Inserts `r` at its place in the ordered timeline `v` (appending when it is the latest, the common case).
fn insert_ordered(v: &mut Vec<Receipt>, r: Receipt, contents: &[Content], sources: &[String]) {
    let after = |x: &Receipt| receipt_order(x, &r, contents, sources) != Ordering::Greater;
    if v.last().is_none_or(after) {
        v.push(r);
    } else {
        let k = v.partition_point(after);
        v.insert(k, r);
    }
}

/// Dated DBN symbology: metadata mapping intervals, definition records and symbol-mapping records (see the module
/// docs). Lookups are per (instrument id, UTC date) or per (symbol, UTC date), never by instrument id alone.
#[derive(Clone, Debug, Default)]
pub struct Symbology {
    by_id: HashMap<u32, Vec<Span<String>>>,
    by_symbol: HashMap<String, Vec<Span<u32>>>,
    alias_by_id: HashMap<u32, Vec<(i32, i32, String)>>,
    alias_ids: HashMap<String, Vec<(i32, i32, u32)>>,
    contents: Vec<Content>,
    content_ix: HashMap<Content, u32>,
    /// Definition and symbol-mapping receipts per instrument id, in [`receipt_order`].
    recs: HashMap<u32, Vec<Receipt>>,
    /// The symbol-mapping receipts of `recs` (the only ones with aliases).
    maps: HashMap<u32, Vec<Receipt>>,
    /// `(receive time, instrument id)` of every receipt naming a raw symbol, sorted.
    recs_by_symbol: HashMap<Box<str>, Vec<(u64, u32)>>,
    sources: Vec<String>,
    /// Per source: true when it is a data (MBO, trades or mixed-schema) file, not a `definition` file.
    data_source: Vec<bool>,
    datasets: BTreeMap<String, usize>,
    stats: SymbologyStats,
}

impl Symbology {
    /// Empty symbology.
    pub fn new() -> Self {
        Self::default()
    }

    fn source(&mut self, label: &str, data: bool) -> usize {
        // Most recent first: records arrive file by file.
        if let Some(i) = self.sources.iter().rposition(|s| s == label) {
            self.data_source[i] |= data;
            return i;
        }
        self.sources.push(label.to_owned());
        self.data_source.push(data);
        self.sources.len() - 1
    }

    /// The preferred source of two that state the same interval: a data file, then the smaller label (so error
    /// messages and the vintage check do not depend on the input order).
    fn better_source(&self, a: usize, b: usize) -> bool {
        (!self.data_source[a], &self.sources[a]) < (!self.data_source[b], &self.sources[b])
    }

    /// Adds one file's metadata, labelled by its dataset, schema and start (see [`Self::add_metadata_from`]).
    pub fn add_metadata(&mut self, md: &Metadata) {
        let label = format!(
            "{} {} file starting {}",
            if md.dataset.is_empty() { "DBN" } else { &md.dataset },
            md.schema.map_or("mixed-schema", |s| s.as_str()),
            format_utc_day(utc_day_u(md.start))
        );
        self.add_metadata_labeled(md, label);
    }

    /// Adds one file's metadata, labelled by its path in error messages.
    pub fn add_metadata_from(&mut self, md: &Metadata, path: &Path) {
        self.add_metadata_labeled(md, path.display().to_string());
    }

    /// Adds the dataset and the mapping intervals of one file's metadata (only `stype_out = instrument_id`;
    /// duplicates ignored). Mapping keys that are not raw symbols become aliases (module docs). A file whose schema
    /// is not `definition` is a data file: its raw mappings confirm the vintage of the definitions
    /// ([`Self::unverified_xnas_vintage`]).
    pub fn add_metadata_labeled(&mut self, md: &Metadata, label: String) {
        let src = self.source(&label, md.schema != Some(Schema::Definition));
        let dataset = md.dataset.trim();
        if !dataset.is_empty() {
            let keep = self.datasets.get(dataset).is_none_or(|s| self.better_source(src, *s));
            if keep {
                self.datasets.insert(dataset.to_owned(), src);
            }
        }
        if md.stype_out != SType::InstrumentId {
            return;
        }
        for m in &md.mappings {
            let sym = m.raw_symbol.trim();
            if sym.is_empty() {
                continue;
            }
            let raw = match md.stype_in {
                Some(SType::RawSymbol) => true,
                None => !looks_like_smart_symbol(sym),
                Some(_) => false,
            };
            for i in &m.intervals {
                let Ok(id) = i.symbol.trim().parse::<u32>() else {
                    continue;
                };
                let (a, b) = interval_days(i);
                if b <= a {
                    continue;
                }
                if raw {
                    self.add_raw_interval(id, sym, a, b, src);
                } else {
                    let v = self.alias_by_id.entry(id).or_default();
                    if !v.iter().any(|(x, y, s)| (*x, *y) == (a, b) && s == sym) {
                        let k = v.partition_point(|(x, y, s)| (*x, *y, s.as_str()) <= (a, b, sym));
                        v.insert(k, (a, b, sym.to_owned()));
                        let w = self.alias_ids.entry(sym.to_owned()).or_default();
                        let k = w.partition_point(|e| *e <= (a, b, id));
                        w.insert(k, (a, b, id));
                        self.stats.alias_intervals += 1;
                    }
                }
            }
        }
    }

    fn add_raw_interval(&mut self, id: u32, sym: &str, a: i32, b: i32, src: usize) {
        let dup = self
            .by_id
            .get(&id)
            .and_then(|v| v.iter().position(|(x, y, s, _)| (*x, *y) == (a, b) && s == sym));
        if let Some(k) = dup {
            let old = self.by_id[&id][k].3;
            if self.better_source(src, old) {
                self.by_id.get_mut(&id).expect("present")[k].3 = src;
                if let Some(e) = self
                    .by_symbol
                    .get_mut(sym)
                    .and_then(|w| w.iter_mut().find(|e| **e == (a, b, id, old)))
                {
                    e.3 = src;
                }
            }
            return;
        }
        let v = self.by_id.entry(id).or_default();
        let k = v.partition_point(|(x, y, s, _)| (*x, *y, s.as_str()) <= (a, b, sym));
        v.insert(k, (a, b, sym.to_owned(), src));
        let w = self.by_symbol.entry(sym.to_owned()).or_default();
        let k = w.partition_point(|e| (e.0, e.1, e.2) <= (a, b, id));
        w.insert(k, (a, b, id, src));
    }

    fn intern(&mut self, c: Content) -> u32 {
        if let Some(i) = self.content_ix.get(&c) {
            return *i;
        }
        let i = u32::try_from(self.contents.len()).expect("fewer than 2^32 distinct definitions");
        self.contents.push(c.clone());
        self.content_ix.insert(c, i);
        i
    }

    fn insert_receipt(&mut self, instrument_id: u32, content: Content, at: u64, src: usize) {
        let (kind, symbol) = (content.kind, content.symbol.clone());
        let content = self.intern(content);
        let r = Receipt {
            at,
            content,
            src: u32::try_from(src).expect("fewer than 2^32 sources"),
        };
        let Self {
            recs,
            maps,
            contents,
            sources,
            recs_by_symbol,
            ..
        } = self;
        insert_ordered(recs.entry(instrument_id).or_default(), r, contents, sources);
        if kind == EntryKind::Mapping {
            insert_ordered(maps.entry(instrument_id).or_default(), r, contents, sources);
        }
        if !symbol.is_empty() {
            let w = recs_by_symbol.entry(symbol).or_default();
            let e = (at, instrument_id);
            let k = w.partition_point(|x| *x <= e);
            if k == 0 || w[k - 1] != e {
                w.insert(k, e);
            }
        }
    }

    /// Adds one definition record (`min_price_increment` at 1e-9; later `ts_recv` supersedes earlier ones). Kept for
    /// callers that only have these four fields; see [`Self::add_definition_record`].
    pub fn add_definition(&mut self, instrument_id: u32, raw_symbol: &str, min_price_increment: i64, ts_recv: u64) {
        self.add_definition_record(
            &DbnDefinition {
                instrument_id,
                ts_recv,
                ts_event: UNDEF_TS,
                raw_symbol: raw_symbol.trim().to_owned(),
                raw_instrument_id: None,
                min_price_increment: (min_price_increment > 0 && min_price_increment != i64::MAX)
                    .then_some(min_price_increment),
                min_price_increment_amount: None,
                display_factor: None,
                min_lot_size_round_lot: None,
                instrument_class: 0,
                match_algorithm: b' ',
                security_update_action: b'A',
                exchange: String::new(),
                asset: String::new(),
                security_type: String::new(),
                currency: String::new(),
            },
            "definition",
        );
    }

    /// Adds one definition record read from `source` (a path or label for error messages). Deletes and undated
    /// records are counted and not applied.
    pub fn add_definition_record(&mut self, d: &DbnDefinition, source: &str) {
        if d.is_delete() {
            self.stats.definition_deletes += 1;
            return;
        }
        let Some(at) = d.valid_from() else {
            self.stats.definitions_undated += 1;
            return;
        };
        self.stats.definitions += 1;
        let mpi = d.min_price_increment.filter(|m| *m > 0 && *m != i64::MAX);
        if mpi.is_none() {
            self.stats.definitions_without_tick += 1;
        }
        let src = self.source(source, false);
        self.insert_receipt(
            d.instrument_id,
            Content {
                kind: EntryKind::Definition,
                symbol: d.raw_symbol.trim().into(),
                alias: "".into(),
                mpi,
                mpi_amount: d.min_price_increment_amount.filter(|m| *m > 0 && *m != i64::MAX),
                match_algorithm: d.match_algorithm,
                raw_instrument_id: d.raw_instrument_id,
                currency: d.currency.trim().into(),
            },
            at,
            src,
        );
    }

    /// Adds one symbol-mapping record (live captures): `raw_symbol` is the publisher symbol when the record's output
    /// symbology is `raw_symbol` (or its input is), `alias` the requested symbol when it differs; valid from `at_ns`
    /// (UTC ns).
    pub fn add_symbol_mapping(
        &mut self,
        instrument_id: u32,
        raw_symbol: Option<&str>,
        alias: Option<&str>,
        at_ns: u64,
        source: &str,
    ) {
        if at_ns == UNDEF_TS {
            return;
        }
        let symbol = raw_symbol.map(str::trim).unwrap_or("");
        let alias = alias.map(str::trim).filter(|a| *a != symbol).unwrap_or("");
        if symbol.is_empty() && alias.is_empty() {
            return;
        }
        self.stats.symbol_mapping_records += 1;
        let src = self.source(source, true);
        self.insert_receipt(
            instrument_id,
            Content {
                kind: EntryKind::Mapping,
                symbol: symbol.into(),
                alias: alias.into(),
                mpi: None,
                mpi_amount: None,
                match_algorithm: b' ',
                raw_instrument_id: None,
                currency: "".into(),
            },
            at_ns,
            src,
        );
    }

    /// Counters of what was read. The repeats are counted on the ordered timelines, so they do not depend on the
    /// order of the inputs.
    pub fn stats(&self) -> SymbologyStats {
        let mut s = self.stats;
        s.definitions_repeated = self
            .recs
            .values()
            .map(|v| {
                v.windows(2)
                    .filter(|w| {
                        w[0].content == w[1].content
                            && self.contents[w[1].content as usize].kind == EntryKind::Definition
                    })
                    .count() as u64
            })
            .sum();
        s
    }

    /// True when no mapping, alias and definition was added.
    pub fn is_empty(&self) -> bool {
        self.by_id.is_empty() && self.alias_by_id.is_empty() && self.recs.is_empty()
    }

    /// Databento datasets named by the inputs' metadata.
    pub fn datasets(&self) -> Vec<&str> {
        self.datasets.keys().map(String::as_str).collect()
    }

    fn content(&self, r: &Receipt) -> &Content {
        &self.contents[r.content as usize]
    }

    /// The receipts of `instrument_id` received on or before UTC date `day`, and all of them.
    fn receipts(&self, instrument_id: u32, day: i32) -> (&[Receipt], &[Receipt]) {
        let Some(v) = self.recs.get(&instrument_id) else {
            return (&[], &[]);
        };
        let end = day_end(day);
        (&v[..v.partition_point(|r| r.at < end)], v)
    }

    /// Raw symbol of `instrument_id` on UTC date `day`: the raw metadata interval holding `day` (latest start wins),
    /// else the latest definition or symbol mapping at or before `day` that names one, else a continuous alias that
    /// resolves to this id alone on `day` (never a parent symbol, even with one child).
    pub fn symbol_at(&self, instrument_id: u32, day: i32) -> Option<&str> {
        if let Some(v) = self.by_id.get(&instrument_id)
            && let Some((_, _, s, _)) = v.iter().rev().find(|(a, b, _, _)| *a <= day && day < *b)
        {
            return Some(s);
        }
        let (upto, _) = self.receipts(instrument_id, day);
        if let Some(c) = upto
            .iter()
            .rev()
            .map(|r| self.content(r))
            .find(|c| !c.symbol.is_empty())
        {
            return Some(&c.symbol);
        }
        self.unique_alias_at(instrument_id, day)
    }

    fn unique_alias_at(&self, instrument_id: u32, day: i32) -> Option<&str> {
        let v = self.alias_by_id.get(&instrument_id)?;
        v.iter()
            .rev()
            .filter(|(a, b, s)| *a <= day && day < *b && is_continuous_symbol(s))
            .map(|(_, _, s)| s.as_str())
            .find(|s| {
                self.alias_ids.get(*s).is_some_and(|w| {
                    w.iter()
                        .filter(|(a, b, _)| *a <= day && day < *b)
                        .all(|(_, _, id)| *id == instrument_id)
                })
            })
    }

    /// Aliases (parent, continuous or other requested symbols) of `instrument_id` on UTC date `day`: metadata alias
    /// intervals holding `day`, and every alias of the symbol mappings at or before `day` since the id's raw symbol
    /// last changed (a live capture subscribed to both `ES.FUT` and `ES.c.0` maps the front contract twice).
    pub fn aliases_at(&self, instrument_id: u32, day: i32) -> Vec<&str> {
        let mut out: Vec<&str> = self
            .alias_by_id
            .get(&instrument_id)
            .into_iter()
            .flatten()
            .filter(|(a, b, _)| *a <= day && day < *b)
            .map(|(_, _, s)| s.as_str())
            .collect();
        if let Some(v) = self.maps.get(&instrument_id) {
            let end = day_end(day);
            let mut current: Option<&str> = None;
            for r in v[..v.partition_point(|r| r.at < end)].iter().rev() {
                let c = self.content(r);
                if !c.symbol.is_empty() {
                    match current {
                        None => current = Some(&c.symbol),
                        Some(s) if s != &*c.symbol => break,
                        Some(_) => {}
                    }
                }
                if !c.alias.is_empty() {
                    out.push(&c.alias);
                }
            }
        }
        out.sort_unstable();
        out.dedup();
        out
    }

    /// Instrument id of raw `symbol` on UTC date `day` (raw metadata interval, else the latest definition or symbol
    /// mapping at or before `day`). Aliases are never used: a parent symbol has many ids.
    pub fn id_at(&self, symbol: &str, day: i32) -> Option<u32> {
        if let Some(v) = self.by_symbol.get(symbol)
            && let Some((_, _, id, _)) = v.iter().rev().find(|(a, b, _, _)| *a <= day && day < *b)
        {
            return Some(*id);
        }
        let w = self.recs_by_symbol.get(symbol)?;
        let end = day_end(day);
        w[..w.partition_point(|(t, _)| *t < end)].last().map(|(_, id)| *id)
    }

    /// The latest definition of `instrument_id` that passes `ok` and whose symbol is `symbol` (or names none): valid
    /// on `day`, else of any date.
    fn def_for(
        &self,
        instrument_id: u32,
        symbol: Option<&str>,
        day: i32,
        ok: impl Fn(&Content) -> bool,
    ) -> Option<&Content> {
        let fits = |c: &&Content| {
            c.kind == EntryKind::Definition && ok(c) && (c.symbol.is_empty() || symbol.is_none_or(|s| s == &*c.symbol))
        };
        let (upto, all) = self.receipts(instrument_id, day);
        upto.iter()
            .rev()
            .map(|r| self.content(r))
            .find(fits)
            .or_else(|| all.iter().rev().map(|r| self.content(r)).find(fits))
    }

    /// `min_price_increment` (1e-9) for an instrument partition: the latest definition of `instrument_id` whose
    /// symbol is `symbol` (or names none) valid on `day`, else the latest of any date, else the latest definition of
    /// `symbol` under any id. Non-positive and undefined (`i64::MAX`) increments are skipped.
    pub fn min_price_increment(&self, instrument_id: u32, symbol: Option<&str>, day: i32) -> Option<i64> {
        if let Some(m) = self
            .def_for(instrument_id, symbol, day, |c| c.mpi.is_some())
            .and_then(|c| c.mpi)
        {
            return Some(m);
        }
        let s = symbol?;
        let w = self.recs_by_symbol.get(s)?;
        w.iter().rev().find_map(|(t, id)| {
            let v = self.recs.get(id)?;
            v[v.partition_point(|r| r.at < *t)..]
                .iter()
                .take_while(|r| r.at == *t)
                .map(|r| self.content(r))
                .find(|c| c.kind == EntryKind::Definition && &*c.symbol == s && c.mpi.is_some())
                .and_then(|c| c.mpi)
        })
    }

    /// Contract multiplier (`min_price_increment_amount / min_price_increment`) for an instrument partition, by the
    /// same lookup by id as [`Self::min_price_increment`]; `None` when no definition defines both.
    pub fn contract_multiplier(&self, instrument_id: u32, symbol: Option<&str>, day: i32) -> Option<f64> {
        self.def_for(instrument_id, symbol, day, |c| c.multiplier().is_some())
            .and_then(Content::multiplier)
    }

    /// Price currency for an instrument partition (same lookup by id; `None` when no definition names one).
    pub fn currency(&self, instrument_id: u32, symbol: Option<&str>, day: i32) -> Option<&str> {
        self.def_for(instrument_id, symbol, day, |c| !c.currency.is_empty())
            .map(|c| &*c.currency)
    }

    /// `match_algorithm` byte for an instrument partition (same lookup by id as [`Self::min_price_increment`];
    /// `None` when no definition defines one).
    pub fn match_algorithm(&self, instrument_id: u32, symbol: Option<&str>, day: i32) -> Option<u8> {
        self.def_for(instrument_id, symbol, day, |c| {
            match_algorithm_defined(c.match_algorithm)
        })
        .map(|c| c.match_algorithm)
    }

    /// The match algorithm of a partition when its definition names one other than FIFO (`F`).
    pub fn non_fifo(&self, instrument_id: u32, symbol: Option<&str>, day: i32) -> Option<u8> {
        self.match_algorithm(instrument_id, symbol, day).filter(|m| *m != b'F')
    }

    /// `QT-CONFIG-001` for a partition whose definition names the non-FIFO algorithm `code` (the ingest raises it at
    /// the partition's first selected record, before the bulk of the data is written). It also names up to ten
    /// other instruments the symbol filter `selected` keeps (by raw symbol or alias) whose latest definition is not
    /// FIFO either, so one rerun can leave them all out.
    pub fn non_fifo_error(
        &self,
        date: i32,
        instrument_id: u32,
        symbol: Option<&str>,
        code: u8,
        selected: impl Fn(&str) -> bool,
    ) -> QtError {
        let mut ids: Vec<&u32> = self.recs.keys().collect();
        ids.sort_unstable();
        let mut others: BTreeSet<String> = BTreeSet::new();
        for id in ids {
            let Some((r, c)) = self.recs[id]
                .iter()
                .rev()
                .map(|r| (r, self.content(r)))
                .find(|(_, c)| c.kind == EntryKind::Definition && match_algorithm_defined(c.match_algorithm))
            else {
                continue;
            };
            if c.match_algorithm == b'F' || c.symbol.is_empty() || Some(&*c.symbol) == symbol {
                continue;
            }
            let day = utc_day_u(r.at);
            if selected(&c.symbol) || self.aliases_at(*id, day).into_iter().any(&selected) {
                others.insert(format!("{} ({})", c.symbol, match_algorithm_name(c.match_algorithm)));
            }
        }
        let also = if others.is_empty() {
            String::new()
        } else {
            let n = others.len();
            let shown: Vec<String> = others.into_iter().take(MAX_NON_FIFO_SHOWN).collect();
            let more = if n > shown.len() {
                format!(" and {} more", n - shown.len())
            } else {
                String::new()
            };
            format!(
                "; the definitions give {n} other selected instrument(s) a non-FIFO algorithm too: {}{more}",
                shown.join(", ")
            )
        };
        QtError::Config(format!(
            "{} (id {instrument_id}, {}: {} '{}') is not matched by price-time priority (DBN definition \
             match_algorithm){also}. queuetruth replays FIFO books only and rejects pro-rata and allocation books at \
             ingest; list the FIFO contracts explicitly with --symbols (IngestSpec.symbols), for example --symbols \
             ZNZ6,ZNH7 instead of the parent ZN.FUT. Nothing was written",
            symbol.map_or_else(|| instrument_id.to_string(), str::to_owned),
            format_utc_day(date),
            match_algorithm_name(code),
            char::from(code)
        ))
    }

    /// Refuses partitions whose definition names a match algorithm other than FIFO (`F`), naming up to ten
    /// (`QT-CONFIG-001`); an undefined algorithm passes. `parts` are `(session date, instrument id, symbol)`. The
    /// ingest uses [`Self::non_fifo`] and [`Self::non_fifo_error`] per partition instead, to fail before writing.
    pub fn check_fifo<'a>(&self, parts: impl IntoIterator<Item = (i32, u32, Option<&'a str>)>) -> QtResult<()> {
        let mut hits: Vec<String> = Vec::new();
        let mut n = 0usize;
        for (date, iid, sym) in parts {
            if let Some(m) = self.non_fifo(iid, sym, date) {
                n += 1;
                if hits.len() < MAX_NON_FIFO_SHOWN {
                    hits.push(format!(
                        "{} (id {iid}, {}: {} '{}')",
                        sym.map_or_else(|| iid.to_string(), str::to_owned),
                        format_utc_day(date),
                        match_algorithm_name(m),
                        char::from(m)
                    ));
                }
            }
        }
        if n == 0 {
            return Ok(());
        }
        let more = if n > hits.len() {
            format!(" and {} more", n - hits.len())
        } else {
            String::new()
        };
        Err(QtError::Config(format!(
            "{n} instrument partition(s) are not matched by price-time priority (DBN definition match_algorithm): \
             {}{more}. queuetruth replays FIFO books only and rejects pro-rata and allocation books at ingest; list \
             the FIFO contracts explicitly with --symbols (IngestSpec.symbols)",
            hits.join(", ")
        )))
    }

    fn is_xnas(&self) -> bool {
        self.datasets.contains_key(XNAS_ITCH)
    }

    /// `XNAS.ITCH` definitions before 2023-11-28 of both vintages: an old-vintage one (`raw_instrument_id`
    /// undefined) and a re-issued one (`raw_instrument_id` defined and not the instrument id), first of each by
    /// (id, receive time).
    fn xnas_definition_vintages(&self) -> Option<String> {
        if !self.is_xnas() {
            return None;
        }
        let mut ids: Vec<&u32> = self.recs.keys().collect();
        ids.sort_unstable();
        let mut old: Option<(u32, &Receipt)> = None;
        let mut new: Option<(u32, &Receipt)> = None;
        for id in ids {
            for r in &self.recs[id] {
                let c = self.content(r);
                if c.kind != EntryKind::Definition || utc_day_u(r.at) >= XNAS_REISSUE_CUTOVER_DAY {
                    continue;
                }
                match c.raw_instrument_id {
                    None => {
                        old.get_or_insert((*id, r));
                    }
                    Some(x) if x != u64::from(*id) => {
                        new.get_or_insert((*id, r));
                    }
                    Some(_) => {}
                }
            }
            if old.is_some() && new.is_some() {
                break;
            }
        }
        let ((oi, o), (ni, n)) = (old?, new?);
        let describe = |id: u32, r: &Receipt, what: String| {
            let c = self.content(r);
            format!(
                "{} id {id} on {} ({what}, in {})",
                if c.symbol.is_empty() { "instrument" } else { &c.symbol },
                format_utc_day(utc_day_u(r.at)),
                self.sources[r.src as usize]
            )
        };
        Some(format!(
            "the {XNAS_ITCH} definitions before 2023-11-28 come from both vintages of Databento's history: {} and {}. \
             Databento's 2026-10-31 re-issue gave that history new instrument ids, so ids of the two vintages name \
             different instruments; keep each vintage in its own folder and ingest it with its own MBO files",
            describe(
                oi,
                o,
                "raw_instrument_id undefined: the vintage before the re-issue".into()
            ),
            describe(
                ni,
                n,
                format!(
                    "raw_instrument_id {}: the re-issued vintage",
                    self.content(n).raw_instrument_id.unwrap_or_default()
                )
            ),
        ))
    }

    /// Refuses inputs that disagree: more than one Databento dataset; `XNAS.ITCH` definitions of both vintages of
    /// the 2026-10-31 re-issue; or, on one UTC date, a raw symbol with two instrument ids or an id with two raw
    /// symbols (metadata against metadata, and definitions or symbol mappings against metadata). `QT-INPUT-002`
    /// names up to five conflicts and their sources.
    pub fn check(&self) -> QtResult<()> {
        let err = |msg: String| QtError::InputFormat {
            location: "DBN symbology".into(),
            msg,
        };
        if self.datasets.len() > 1 {
            let list: Vec<String> = self
                .datasets
                .iter()
                .map(|(d, s)| format!("{d} ({})", self.sources[*s]))
                .collect();
            return Err(err(format!(
                "inputs come from {} Databento datasets: {}; instrument ids are only meaningful within one dataset, \
                 so ingest each dataset separately",
                list.len(),
                list.join(", ")
            )));
        }
        if let Some(m) = self.xnas_definition_vintages() {
            return Err(err(m));
        }
        let mut conflicts: Vec<String> = Vec::new();
        let mut total = 0usize;
        let mut before_cutover = false;
        let mut note = |day: i32, c: String| {
            total += 1;
            before_cutover |= day < XNAS_REISSUE_CUTOVER_DAY;
            if conflicts.len() < MAX_CONFLICTS_SHOWN {
                conflicts.push(c);
            }
        };
        // Metadata against metadata: overlapping intervals that disagree.
        let mut syms: Vec<&String> = self.by_symbol.keys().collect();
        syms.sort_unstable();
        for s in syms {
            for (a, x, y) in overlaps(&self.by_symbol[s], |e| e.2) {
                note(
                    a,
                    format!(
                        "{s} on {}: instrument id {} ({}) and {} ({})",
                        format_utc_day(a),
                        x.2,
                        self.sources[x.3],
                        y.2,
                        self.sources[y.3]
                    ),
                );
            }
        }
        let mut ids: Vec<&u32> = self.by_id.keys().collect();
        ids.sort_unstable();
        for id in ids {
            for (a, x, y) in overlaps(&self.by_id[id], |e| e.2.clone()) {
                note(
                    a,
                    format!(
                        "instrument id {id} on {}: symbol {} ({}) and {} ({})",
                        format_utc_day(a),
                        x.2,
                        self.sources[x.3],
                        y.2,
                        self.sources[y.3]
                    ),
                );
            }
        }
        // Definitions and symbol mappings against metadata, on the UTC date of every receipt (binary search on
        // merged timelines: a full-market year is millions of receipts). One conflict is quoted per run of
        // identical receipts.
        let sym_line: HashMap<&str, Vec<Span<u32>>> = self
            .by_symbol
            .iter()
            .map(|(k, v)| (k.as_str(), merged(v, |e| e.2)))
            .collect();
        let id_line: HashMap<u32, Vec<Span<String>>> = self
            .by_id
            .iter()
            .map(|(k, v)| (*k, merged(v, |e| e.2.clone())))
            .collect();
        let mut ids: Vec<&u32> = self.recs.keys().collect();
        ids.sort_unstable();
        for id in ids {
            let id_tl = id_line.get(id);
            let mut skip: Option<u32> = None;
            let mut last_ok: Option<(u32, i32, bool)> = None;
            for r in &self.recs[id] {
                let e = self.content(r);
                if e.symbol.is_empty() || skip == Some(r.content) {
                    continue;
                }
                skip = None;
                let (day, early) = (utc_day_u(r.at), is_early(r.at));
                // A receipt like the last accepted one, on its date, needs no new lookup (a later receipt on that
                // date is only exempt when the accepted one was not received in the after-hours hour).
                if last_ok.is_some_and(|(c, d, e0)| c == r.content && d == day && (!e0 || early)) {
                    continue;
                }
                let sym_tl = sym_line.get(&*e.symbol);
                let sym_ok = |d: i32| interval_at(sym_tl, d).is_none_or(|x| x.2 == *id);
                let id_ok = |d: i32| interval_at(id_tl, d).is_none_or(|x| *x.2 == *e.symbol);
                // Received before 01:00 UTC: the previous UTC date's session may still be open (Nasdaq after-hours
                // run to 01:00 UTC in winter), so agreement with that date's symbology (not mere absence of it) is
                // accepted too.
                let prev_agrees = || {
                    let p = day - 1;
                    sym_ok(p) && id_ok(p) && (interval_at(sym_tl, p).is_some() || interval_at(id_tl, p).is_some())
                };
                if (sym_ok(day) && id_ok(day)) || (early && prev_agrees()) {
                    last_ok = Some((r.content, day, early));
                    continue;
                }
                let what = match (e.kind, e.raw_instrument_id) {
                    (EntryKind::Definition, Some(x)) => format!("definition, raw_instrument_id {x}"),
                    (EntryKind::Definition, None) => "definition".to_owned(),
                    (EntryKind::Mapping, _) => "symbol mapping".to_owned(),
                };
                let src = &self.sources[r.src as usize];
                if let Some((_, _, mid, msrc)) = interval_at(sym_tl, day).filter(|x| x.2 != *id) {
                    note(
                        day,
                        format!(
                            "{} on {}: instrument id {mid} ({}) and {id} ({what} in {src})",
                            e.symbol,
                            format_utc_day(day),
                            self.sources[*msrc],
                        ),
                    );
                } else if let Some((_, _, msym, msrc)) = interval_at(id_tl, day) {
                    note(
                        day,
                        format!(
                            "instrument id {id} on {}: symbol {msym} ({}) and {} ({what} in {src})",
                            format_utc_day(day),
                            self.sources[*msrc],
                            e.symbol,
                        ),
                    );
                }
                skip = Some(r.content);
            }
        }
        if total == 0 {
            return Ok(());
        }
        let more = if total > conflicts.len() {
            format!(" and {} more", total - conflicts.len())
        } else {
            String::new()
        };
        let cause = if self.is_xnas() && before_cutover {
            "The files come from different vintages of the same history (Databento's 2026-10-31 XNAS.ITCH re-issue \
             assigns new instrument ids to data before 2023-11-28; the Nasdaq stock locate stays in the definitions' \
             raw_instrument_id) or from different queries; download the MBO and definition files of one vintage \
             together and ingest them without older files"
        } else {
            "The files come from different queries or downloads; download the MBO and definition files of one query \
             together and ingest them without other files"
        };
        Err(err(format!(
            "inputs disagree on the instrument symbology ({total} conflict(s)): {}{more}. {cause}",
            conflicts.join("; ")
        )))
    }

    /// `XNAS.ITCH` partitions before 2023-11-28 whose symbol comes from definition or symbol-mapping records (or a
    /// `definition` file's metadata) while no data file's metadata maps the id on that date: their vintage cannot be
    /// checked against the definitions'. Returns the count and a warning naming up to five, or `None`. `parts` are
    /// `(session date, partition instrument id)`; a partition labelled by its numeric id is not counted.
    pub fn unverified_xnas_vintage(&self, parts: impl IntoIterator<Item = (i32, u32)>) -> Option<(u64, String)> {
        if !self.is_xnas() {
            return None;
        }
        let mut n = 0u64;
        let mut shown: Vec<String> = Vec::new();
        for (date, iid) in parts {
            if date >= XNAS_REISSUE_CUTOVER_DAY {
                continue;
            }
            let Some(sym) = self.symbol_at(iid, date) else {
                continue;
            };
            let confirmed = self.by_id.get(&iid).is_some_and(|v| {
                v.iter()
                    .any(|(a, b, s, src)| *a <= date && date < *b && s == sym && self.data_source[*src])
            });
            if confirmed {
                continue;
            }
            n += 1;
            if shown.len() < MAX_CONFLICTS_SHOWN {
                shown.push(format!("{sym} (id {iid}, {})", format_utc_day(date)));
            }
        }
        if n == 0 {
            return None;
        }
        let more = if n > shown.len() as u64 {
            format!(" and {} more", n - shown.len() as u64)
        } else {
            String::new()
        };
        Some((
            n,
            format!(
                "{n} {XNAS_ITCH} partition(s) before 2023-11-28 take their symbol from definitions that no MBO \
                 file's metadata confirms: {}{more}. Databento's 2026-10-31 re-issue gave this history new instrument \
                 ids, so these symbols are right only if the MBO and definition files are of the same vintage \
                 (downloaded on the same side of 2026-10-31). Request the MBO with stype_in=\"raw_symbol\" so the \
                 ingest can check it, or keep each vintage in its own folder",
                shown.join(", ")
            ),
        ))
    }
}

/// One dated interval: `[start day, end day)`, the mapped value, the source index.
type Span<T> = (i32, i32, T, usize);

/// The interval of a merged timeline ([`merged`]) that holds `day`.
fn interval_at<T>(v: Option<&Vec<Span<T>>>, day: i32) -> Option<&Span<T>> {
    let v = v?;
    let k = v.partition_point(|e| e.0 <= day);
    v[..k].last().filter(|e| day < e.1)
}

/// A timeline sorted by start with overlapping or adjacent intervals of the same key merged (the first one's source
/// is kept). Without disagreeing overlaps (checked first) the result does not overlap, so [`interval_at`] is exact.
fn merged<T: Clone, K: PartialEq>(v: &[Span<T>], key: impl Fn(&Span<T>) -> K) -> Vec<Span<T>> {
    let mut out: Vec<Span<T>> = Vec::with_capacity(v.len());
    for e in v {
        match out.last_mut() {
            Some(last) if e.0 <= last.1 && key(last) == key(e) => last.1 = last.1.max(e.1),
            _ => out.push(e.clone()),
        }
    }
    out
}

/// Overlapping intervals (sorted by start) whose keys differ: `(first common day, earlier, later)`, one per later
/// interval.
fn overlaps<T, K: PartialEq>(v: &[Span<T>], key: impl Fn(&Span<T>) -> K) -> Vec<(i32, &Span<T>, &Span<T>)> {
    let mut out = Vec::new();
    let mut active: Vec<&Span<T>> = Vec::new();
    for e in v {
        active.retain(|x| x.1 > e.0);
        if let Some(x) = active.iter().find(|x| key(x) != key(e)) {
            out.push((e.0.max(x.0), *x, e));
        }
        active.push(e);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn format_utc_day_matches_known_dates() {
        assert_eq!(format_utc_day(0), "1970-01-01");
        assert_eq!(format_utc_day(XNAS_REISSUE_CUTOVER_DAY), "2023-11-28");
        assert_eq!(format_utc_day(20_757), "2026-10-31");
        assert_eq!(format_utc_day(-1), "1969-12-31");
        assert_eq!(format_utc_day(11_016), "2000-02-29");
    }

    #[test]
    fn day_bounds() {
        assert_eq!(day_end(0), NS_PER_DAY as u64);
        assert_eq!(day_end(-1), 0);
        assert_eq!(day_end(-5), 0);
        assert!(is_early(NS_PER_DAY as u64) && is_early((NS_PER_DAY + NS_PER_DAY / 24 - 1) as u64));
        assert!(!is_early((NS_PER_DAY + NS_PER_DAY / 24) as u64) && !is_early(u64::MAX));
    }

    #[test]
    fn smart_symbol_shapes() {
        for s in ["ES.FUT", "ES.OPT", "BTC.SPOT", "ES.c.0", "CL.n.1", "ZN.v.12"] {
            assert!(looks_like_smart_symbol(s), "{s}");
        }
        for s in ["ESZ4", "BRK.B", "AAPL", "ES.x.0", "ES.c.", ".FUT", "ES.c.0a", "A.B.C.D"] {
            assert!(!looks_like_smart_symbol(s), "{s}");
        }
        assert!(is_continuous_symbol("ES.c.0") && !is_continuous_symbol("ES.FUT"));
    }

    #[test]
    fn overlaps_find_disagreeing_intervals_only() {
        let v = vec![(0, 10, 7u32, 0usize), (2, 3, 7, 1), (5, 6, 8, 1), (10, 12, 8, 0)];
        let o = overlaps(&v, |e| e.2);
        assert_eq!(o.len(), 1);
        assert_eq!((o[0].0, o[0].1.2, o[0].2.2), (5, 7, 8));
        assert!(overlaps(&[(0, 1, 1u32, 0usize), (1, 2, 2, 0)], |e| e.2).is_empty());
    }

    #[test]
    fn merged_timelines_and_lookup() {
        let v = vec![(0, 3, 7u32, 0usize), (2, 5, 7, 1), (5, 6, 7, 1), (8, 9, 8, 0)];
        let m = merged(&v, |e| e.2);
        assert_eq!(m, vec![(0, 6, 7, 0), (8, 9, 8, 0)]);
        let at = |d| interval_at(Some(&m), d).map(|e| e.2);
        assert_eq!(
            (at(-1), at(0), at(5), at(6), at(7), at(8), at(9)),
            (None, Some(7), Some(7), None, None, Some(8), None)
        );
        assert!(interval_at::<u32>(None, 0).is_none());
    }

    #[test]
    fn match_algorithm_names() {
        assert_eq!(match_algorithm_name(b'F'), "FIFO");
        assert_eq!(match_algorithm_name(b'C'), "pro-rata");
        assert_eq!(
            match_algorithm_name(b'A'),
            "allocation (pro-rata with a top-order allocation)"
        );
        assert_eq!(match_algorithm_name(b' '), "undefined");
        assert_eq!(match_algorithm_name(b'?'), "unknown");
        assert!(!match_algorithm_defined(b' ') && !match_algorithm_defined(0) && match_algorithm_defined(b'F'));
    }

    #[test]
    fn multiplier_from_tick_value() {
        // ES: 0.25 index points worth $12.50; ZN: 1/64 of a point worth $15.625.
        assert_eq!(multiplier(Some(12_500_000_000), Some(250_000_000)), Some(50.0));
        assert_eq!(multiplier(Some(15_625_000_000), Some(15_625_000)), Some(1000.0));
        assert_eq!(multiplier(None, Some(250_000_000)), None);
        assert_eq!(multiplier(Some(12_500_000_000), None), None);
        assert_eq!(multiplier(Some(0), Some(1)), None);
    }
}
