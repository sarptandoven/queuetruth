//! Databento DBN reader (the `dbn` crate; plain or zstd inferred from content, inflated behind the decompression
//! guard). MBO records map to `MboEvent` with DBN semantics (u32 size and sequence widened to u64, `channel_id`
//! u8 -> u16, `publisher_id` from the header, timestamps checked to i64, prices rescaled exactly from 1e-9 to the
//! dataset `price_exp`; an inexact price is `QT-INPUT-004`). `definition` files supply symbols and ticks, `trades`
//! files supply source prints. Multi-file inputs are ordered by metadata `start` (stable).
//!
//! Hardening: records are decoded as stored (no version upgrade: MBO and trade layouts are identical in DBN v1-v3,
//! definitions are read with the file's own version), and the decompressed byte stream is checked by a framing
//! validator before the `dbn` decoder sees it, because the decoder asserts on misaligned or short records of
//! malformed files:
//! - the prelude must name a DBN version the pinned `dbn` crate reads (1 to [`SUPPORTED_DBN_VERSION`]); a newer
//!   header (for example DBN v4) is `QT-INPUT-002` naming the version and the supported range, never a panic;
//! - metadata length <= 256 MiB; every record length a multiple of 8 and at least the 16-byte header;
//! - the stream must not end inside the metadata or a record (a truncated plain `.dbn` is `QT-INPUT-002` naming the
//!   decompressed byte offset, not a shorter dataset).
//!
//! Symbology ([`Symbology`]): Databento instrument ids are only unique per UTC date (and the 2026-10-31 XNAS.ITCH
//! re-issue assigns new ids to data before 2023-11-28), so ingest never joins dates on `instrument_id` alone: the
//! metadata mappings (`stype_out = instrument_id`) are kept as dated intervals and definition records as dated
//! entries, and symbols, ticks and the symbol filter are resolved per (instrument id, UTC date).

use std::collections::{BTreeMap, HashMap};
use std::fs::File;
use std::io::BufReader;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::AtomicU64;

use ::dbn::decode::dbn::Decoder;
use ::dbn::decode::{DbnMetadata, DecodeRecordRef, DynReader};
use ::dbn::{InstrumentDefMsg, MboMsg, Metadata, RecordRef, SType, Schema, TradeMsg, VersionUpgradePolicy};
use qt_schema::rows::{TradeKind, TradeRow};
use qt_types::{MboEvent, QtError, QtResult, Side, UNDEF_PRICE, VenueRules};

use crate::guard::{CountingReader, GuardLimits, GuardedReader, READ_BUF, open_file};
use crate::units::rescale;

type Inner = FrameCheck<GuardedReader<DynReader<'static, BufReader<CountingReader<File>>>>>;

/// Largest accepted DBN metadata block.
pub const MAX_METADATA_BYTES: u64 = 256 << 20;

/// Newest DBN version the pinned `dbn` crate decodes (versions 1 to this one are accepted).
pub const SUPPORTED_DBN_VERSION: u8 = ::dbn::DBN_VERSION;

/// Nanoseconds per UTC day (DBN symbology dates are UTC dates).
const NS_PER_DAY: i64 = 86_400_000_000_000;

enum Frame {
    Prelude { buf: [u8; 8], got: usize },
    Skip { remaining: u64, start: u64, metadata: bool },
    Header,
    Passthrough,
}

/// Validates DBN framing on the decompressed stream (see module docs); bytes pass through unchanged.
pub struct FrameCheck<R> {
    inner: R,
    state: Frame,
    offset: u64,
}

impl<R> FrameCheck<R> {
    /// Wraps a decompressed DBN byte stream.
    pub fn new(inner: R) -> Self {
        Self {
            inner,
            state: Frame::Prelude { buf: [0; 8], got: 0 },
            offset: 0,
        }
    }

    fn observe(&mut self, mut b: &[u8]) -> std::io::Result<()> {
        let bad = |m: String| std::io::Error::new(std::io::ErrorKind::InvalidData, m);
        while !b.is_empty() {
            match &mut self.state {
                Frame::Passthrough => return Ok(()),
                Frame::Prelude { buf, got } => {
                    let n = (8 - *got).min(b.len());
                    buf[*got..*got + n].copy_from_slice(&b[..n]);
                    *got += n;
                    b = &b[n..];
                    self.offset += n as u64;
                    if *got == 8 {
                        if &buf[..3] != b"DBN" {
                            self.state = Frame::Passthrough;
                            return Ok(());
                        }
                        let version = buf[3];
                        if version == 0 || version > SUPPORTED_DBN_VERSION {
                            return Err(bad(format!(
                                "unsupported DBN version {version}: this build reads DBN versions 1 to \
                                 {SUPPORTED_DBN_VERSION} (pinned dbn crate); re-encode the file as DBN \
                                 v{SUPPORTED_DBN_VERSION} or use a queuetruth release whose dbn crate reads version \
                                 {version}"
                            )));
                        }
                        let len = u64::from(u32::from_le_bytes([buf[4], buf[5], buf[6], buf[7]]));
                        if len > MAX_METADATA_BYTES {
                            return Err(bad(format!("DBN metadata length {len} exceeds {MAX_METADATA_BYTES}")));
                        }
                        self.state = if len == 0 {
                            Frame::Header
                        } else {
                            Frame::Skip {
                                remaining: len,
                                start: self.offset,
                                metadata: true,
                            }
                        };
                    }
                }
                Frame::Skip { remaining, .. } => {
                    let n = (*remaining).min(b.len() as u64);
                    *remaining -= n;
                    b = &b[n as usize..];
                    self.offset += n;
                    if *remaining == 0 {
                        self.state = Frame::Header;
                    }
                }
                Frame::Header => {
                    let len = u64::from(b[0]) * 4;
                    if len < 16 || len % 8 != 0 {
                        return Err(bad(format!(
                            "malformed DBN record length {len} at decompressed byte {}",
                            self.offset
                        )));
                    }
                    let start = self.offset;
                    b = &b[1..];
                    self.offset += 1;
                    self.state = Frame::Skip {
                        remaining: len - 1,
                        start,
                        metadata: false,
                    };
                }
            }
        }
        Ok(())
    }

    /// End of the stream: an error when it ends inside the prelude, the metadata or a record.
    fn at_eof(&self) -> std::io::Result<()> {
        let bad = |m: String| std::io::Error::new(std::io::ErrorKind::InvalidData, m);
        match &self.state {
            Frame::Skip {
                remaining,
                start,
                metadata,
            } if *remaining > 0 => Err(bad(format!(
                "truncated DBN stream: the {} starting at decompressed byte {start} needs {remaining} more bytes \
                 (the stream ends at byte {})",
                if *metadata { "metadata" } else { "record" },
                self.offset
            ))),
            Frame::Prelude { got, .. } if *got > 0 => Err(bad(format!(
                "truncated DBN stream: it ends after {got} bytes of the 8-byte prelude"
            ))),
            _ => Ok(()),
        }
    }
}

impl<R: std::io::Read> std::io::Read for FrameCheck<R> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        // The dbn decoder reads `UnexpectedEof` as a clean end of input: report a truncated (compressed) stream as
        // invalid data instead.
        let n = self.inner.read(buf).map_err(|e| {
            if e.kind() == std::io::ErrorKind::UnexpectedEof {
                std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    format!("truncated DBN stream at decompressed byte {}: {e}", self.offset),
                )
            } else {
                e
            }
        })?;
        if n == 0 && !buf.is_empty() {
            self.at_eof()?;
        }
        self.observe(&buf[..n])?;
        Ok(n)
    }
}

/// DBN price exponent.
pub const DBN_PRICE_EXP: i8 = -9;
/// DBN undefined timestamp.
pub const UNDEF_TIMESTAMP: u64 = u64::MAX;

fn fmt_err(path: &Path, msg: impl std::fmt::Display) -> QtError {
    QtError::InputFormat {
        location: path.display().to_string(),
        msg: msg.to_string(),
    }
}

/// A `dbn` error as text (I/O errors with their own message, not the `Debug` form).
fn describe(e: &::dbn::Error) -> String {
    match e {
        ::dbn::Error::Io { source, context } => format!("{source} (while {context})"),
        other => other.to_string(),
    }
}

/// Why a record could not be converted.
enum ConvError {
    /// Bad field (`QT-INPUT-002`).
    Format(String),
    /// Price not exact at the dataset exponent (`QT-INPUT-004`).
    Units(String),
}

impl std::fmt::Display for ConvError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Format(s) | Self::Units(s) => f.write_str(s),
        }
    }
}

/// One open DBN file.
pub struct DbnReader {
    dec: Decoder<Inner>,
    path: PathBuf,
    /// Records skipped because they were not of the requested type.
    pub skipped: u64,
    /// Records read (all types).
    pub records: u64,
}

impl DbnReader {
    /// Opens a DBN file with the default decompression guard.
    pub fn open(path: &Path) -> QtResult<Self> {
        Self::open_with(path, GuardLimits::default())
    }

    /// Opens a DBN file with explicit decompression limits.
    pub fn open_with(path: &Path, limits: GuardLimits) -> QtResult<Self> {
        let file = open_file(path)?;
        let count = Arc::new(AtomicU64::new(0));
        let src = BufReader::with_capacity(READ_BUF, CountingReader::new(file, count.clone()));
        let dynr = DynReader::inferred_with_buffer(src).map_err(|e| fmt_err(path, describe(&e)))?;
        let guarded = FrameCheck::new(GuardedReader::new(dynr, count, limits));
        let dec = Decoder::with_upgrade_policy(guarded, VersionUpgradePolicy::AsIs)
            .map_err(|e| fmt_err(path, format!("not a readable DBN file: {}", describe(&e))))?;
        Ok(Self {
            dec,
            path: path.to_owned(),
            skipped: 0,
            records: 0,
        })
    }

    /// File metadata.
    pub fn metadata(&self) -> &Metadata {
        self.dec.metadata()
    }

    /// Path.
    pub fn path(&self) -> &Path {
        &self.path
    }

    fn next_ref(&mut self) -> QtResult<Option<RecordRef<'_>>> {
        let n = self.records;
        let r = self
            .dec
            .decode_record_ref()
            .map_err(|e| fmt_err(&self.path, format!("record {}: {}", n + 1, describe(&e))))?;
        if r.is_some() {
            self.records += 1;
        }
        Ok(r)
    }

    fn conv_err(&self, e: ConvError) -> QtError {
        let at = format!("{} record {}", self.path.display(), self.records);
        match e {
            ConvError::Units(m) => QtError::Units(format!("{at}: {m}")),
            ConvError::Format(m) => QtError::InputFormat { location: at, msg: m },
        }
    }

    /// Next MBO record converted at `price_exp` (other record types are skipped and counted).
    pub fn next_mbo(&mut self, price_exp: i8) -> QtResult<Option<MboEvent>> {
        loop {
            let path = self.path.clone();
            let Some(rec) = self.next_ref()? else { return Ok(None) };
            if rec.has::<MboMsg>() {
                let m = rec.try_get::<MboMsg>().map_err(|e| fmt_err(&path, describe(&e)))?;
                let out = convert_mbo(m, price_exp);
                return out.map(Some).map_err(|e| self.conv_err(e));
            }
            self.skipped += 1;
        }
    }

    /// Next trade print converted at `price_exp` (other record types are skipped and counted).
    pub fn next_trade(&mut self, price_exp: i8, rules: &VenueRules) -> QtResult<Option<TradeRow>> {
        loop {
            let path = self.path.clone();
            let Some(rec) = self.next_ref()? else { return Ok(None) };
            if rec.has::<TradeMsg>() {
                let t = rec.try_get::<TradeMsg>().map_err(|e| fmt_err(&path, describe(&e)))?;
                let out = convert_trade(t, price_exp, rules);
                return out.map(Some).map_err(|e| self.conv_err(e));
            }
            self.skipped += 1;
        }
    }

    /// Next instrument definition as `(instrument_id, raw_symbol, min_price_increment at 1e-9, ts_recv)`.
    pub fn next_definition(&mut self) -> QtResult<Option<(u32, String, i64, u64)>> {
        let version = self.dec.metadata().version;
        loop {
            let path = self.path.clone();
            let Some(rec) = self.next_ref()? else { return Ok(None) };
            if rec.has::<InstrumentDefMsg>() {
                macro_rules! def {
                    ($t:ty) => {{
                        let d = rec
                            .try_get::<$t>()
                            .map_err(|e| fmt_err(&path, describe(&e)))?;
                        let sym = d.raw_symbol().unwrap_or("").to_owned();
                        (d.hd.instrument_id, sym, d.min_price_increment, d.ts_recv)
                    }};
                }
                let out = match version {
                    1 => def!(::dbn::v1::InstrumentDefMsg),
                    2 => def!(::dbn::v2::InstrumentDefMsg),
                    _ => def!(::dbn::v3::InstrumentDefMsg),
                };
                return Ok(Some(out));
            }
            self.skipped += 1;
        }
    }
}

fn ts(v: u64, what: &str) -> Result<i64, ConvError> {
    if v == UNDEF_TIMESTAMP {
        return Err(ConvError::Format(format!("undefined {what}")));
    }
    i64::try_from(v).map_err(|_| ConvError::Format(format!("{what} {v} out of range")))
}

fn px(p: i64, price_exp: i8) -> Result<i64, ConvError> {
    rescale(p, DBN_PRICE_EXP, price_exp)
        .ok_or_else(|| ConvError::Units(format!("price {p} (1e-9) not exact at 10^{price_exp}")))
}

fn convert_mbo(m: &MboMsg, price_exp: i8) -> Result<MboEvent, ConvError> {
    Ok(MboEvent {
        ts_event: ts(m.hd.ts_event, "ts_event")?,
        ts_recv: ts(m.ts_recv, "ts_recv")?,
        instrument_id: m.hd.instrument_id,
        action: m.action as u8,
        side: m.side as u8,
        price: if m.price == UNDEF_PRICE {
            UNDEF_PRICE
        } else {
            px(m.price, price_exp)?
        },
        size: u64::from(m.size),
        order_id: m.order_id,
        flags: m.flags.raw(),
        sequence: u64::from(m.sequence),
        channel_id: u16::from(m.channel_id),
        publisher_id: m.hd.publisher_id,
    })
}

fn convert_trade(t: &TradeMsg, price_exp: i8, rules: &VenueRules) -> Result<TradeRow, ConvError> {
    let side = Side::from_byte(t.side as u8);
    Ok(TradeRow {
        ts_event: ts(t.hd.ts_event, "ts_event")?,
        instrument_id: t.hd.instrument_id,
        sequence: u64::from(t.sequence),
        price: px(t.price, price_exp)?,
        size: u64::from(t.size),
        aggressor_side: side,
        kind: if side == Side::None && rules.supports_hidden_orders {
            TradeKind::Hidden
        } else {
            TradeKind::Displayed
        },
        maker_order_id: None,
    })
}

/// One DBN MBO record as a normalized event (the error text names the field; `DbnReader::next_mbo` maps an
/// inexact price to `QT-INPUT-004`).
pub fn mbo_from_msg(m: &MboMsg, price_exp: i8) -> Result<MboEvent, String> {
    convert_mbo(m, price_exp).map_err(|e| e.to_string())
}

/// One DBN trade record as a source print (side N on a venue with hidden orders is `hidden`).
pub fn trade_from_msg(t: &TradeMsg, price_exp: i8, rules: &VenueRules) -> Result<TradeRow, String> {
    convert_trade(t, price_exp, rules).map_err(|e| e.to_string())
}

/// `instrument_id -> raw symbol` from the metadata symbology (stype_out instrument_id), latest interval wins.
/// Dates are ignored: use [`Symbology`] whenever more than one UTC date is involved.
pub fn symbol_map(md: &Metadata) -> BTreeMap<u32, String> {
    let mut out = BTreeMap::new();
    if md.stype_out != SType::InstrumentId {
        return out;
    }
    for m in &md.mappings {
        let mut iv: Vec<_> = m.intervals.iter().collect();
        iv.sort_by_key(|i| i.start_date);
        for i in iv {
            if let Ok(id) = i.symbol.trim().parse::<u32>() {
                out.insert(id, m.raw_symbol.clone());
            }
        }
    }
    out
}

/// UTC date (days since 1970-01-01) of a timestamp, as DBN symbology dates count.
pub fn utc_day(ts: i64) -> i32 {
    i32::try_from(ts.div_euclid(NS_PER_DAY)).unwrap_or(if ts < 0 { i32::MIN } else { i32::MAX })
}

/// `[start_day, end_day)` (days since 1970-01-01, UTC) of a metadata mapping interval (Julian day 2,440,588 is
/// 1970-01-01).
fn interval_days(i: &::dbn::MappingInterval) -> (i32, i32) {
    (
        i.start_date.to_julian_day() - 2_440_588,
        i.end_date.to_julian_day() - 2_440_588,
    )
}

#[derive(Clone, Debug)]
struct DefEntry {
    day: i32,
    ts_recv: u64,
    symbol: String,
    min_price_increment: i64,
}

/// Dated DBN symbology: metadata mapping intervals (`stype_out = instrument_id`, UTC dates, end exclusive) and
/// definition records (valid from the UTC date of their `ts_recv` until superseded). Lookups are per
/// (instrument id, UTC date) or per (symbol, UTC date), never by instrument id alone.
#[derive(Clone, Debug, Default)]
pub struct Symbology {
    by_id: HashMap<u32, Vec<(i32, i32, String)>>,
    by_symbol: HashMap<String, Vec<(i32, i32, u32)>>,
    defs: HashMap<u32, Vec<DefEntry>>,
    defs_by_symbol: HashMap<String, Vec<(i32, u64, u32)>>,
}

impl Symbology {
    /// Empty symbology.
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds the mapping intervals of one file's metadata (only `stype_out = instrument_id`; duplicates ignored).
    pub fn add_metadata(&mut self, md: &Metadata) {
        if md.stype_out != SType::InstrumentId {
            return;
        }
        for m in &md.mappings {
            let sym = m.raw_symbol.trim();
            if sym.is_empty() {
                continue;
            }
            for i in &m.intervals {
                let Ok(id) = i.symbol.trim().parse::<u32>() else {
                    continue;
                };
                let (a, b) = interval_days(i);
                if b <= a {
                    continue;
                }
                let v = self.by_id.entry(id).or_default();
                if !v.iter().any(|(x, y, s)| (*x, *y) == (a, b) && s == sym) {
                    v.push((a, b, sym.to_owned()));
                    v.sort_by_key(|(x, y, _)| (*x, *y));
                    let w = self.by_symbol.entry(sym.to_owned()).or_default();
                    w.push((a, b, id));
                    w.sort_unstable();
                }
            }
        }
    }

    /// Adds one definition record (`min_price_increment` at 1e-9; later `ts_recv` supersedes earlier ones).
    pub fn add_definition(&mut self, instrument_id: u32, raw_symbol: &str, min_price_increment: i64, ts_recv: u64) {
        let day = utc_day(i64::try_from(ts_recv).unwrap_or(i64::MAX));
        let symbol = raw_symbol.trim().to_owned();
        let v = self.defs.entry(instrument_id).or_default();
        let at = v.partition_point(|d| d.ts_recv <= ts_recv);
        v.insert(
            at,
            DefEntry {
                day,
                ts_recv,
                symbol: symbol.clone(),
                min_price_increment,
            },
        );
        if !symbol.is_empty() {
            let w = self.defs_by_symbol.entry(symbol).or_default();
            let at = w.partition_point(|(_, t, _)| *t <= ts_recv);
            w.insert(at, (day, ts_recv, instrument_id));
        }
    }

    /// True when no mapping and no definition was added.
    pub fn is_empty(&self) -> bool {
        self.by_id.is_empty() && self.defs.is_empty()
    }

    /// Raw symbol of `instrument_id` on UTC date `day`: the metadata interval holding `day` (latest start wins),
    /// else the latest definition at or before `day` that names a symbol.
    pub fn symbol_at(&self, instrument_id: u32, day: i32) -> Option<&str> {
        if let Some(v) = self.by_id.get(&instrument_id) {
            if let Some((_, _, s)) = v.iter().rev().find(|(a, b, _)| *a <= day && day < *b) {
                return Some(s);
            }
        }
        self.defs
            .get(&instrument_id)?
            .iter()
            .rev()
            .find(|d| d.day <= day && !d.symbol.is_empty())
            .map(|d| d.symbol.as_str())
    }

    /// Instrument id of `symbol` on UTC date `day` (metadata interval, else latest definition at or before `day`).
    pub fn id_at(&self, symbol: &str, day: i32) -> Option<u32> {
        if let Some(v) = self.by_symbol.get(symbol) {
            if let Some((_, _, id)) = v.iter().rev().find(|(a, b, _)| *a <= day && day < *b) {
                return Some(*id);
            }
        }
        self.defs_by_symbol
            .get(symbol)?
            .iter()
            .rev()
            .find(|(d, _, _)| *d <= day)
            .map(|(_, _, id)| *id)
    }

    /// `min_price_increment` (1e-9) for an instrument partition: the latest definition of `instrument_id` whose
    /// symbol is `symbol` (or names none) valid on `day`, else the latest of any date, else the latest definition of
    /// `symbol` under any id. Non-positive and undefined (`i64::MAX`) increments are skipped.
    pub fn min_price_increment(&self, instrument_id: u32, symbol: Option<&str>, day: i32) -> Option<i64> {
        let ok = |d: &&DefEntry| {
            d.min_price_increment > 0
                && d.min_price_increment != i64::MAX
                && (d.symbol.is_empty() || symbol.is_none_or(|s| s == d.symbol))
        };
        if let Some(v) = self.defs.get(&instrument_id) {
            if let Some(d) = v.iter().rev().filter(|d| d.day <= day).find(ok) {
                return Some(d.min_price_increment);
            }
            if let Some(d) = v.iter().rev().find(ok) {
                return Some(d.min_price_increment);
            }
        }
        let s = symbol?;
        let w = self.defs_by_symbol.get(s)?;
        w.iter().rev().find_map(|(_, t, id)| {
            self.defs
                .get(id)?
                .iter()
                .find(|d| d.ts_recv == *t && d.symbol == s)
                .and_then(|d| {
                    (d.min_price_increment > 0 && d.min_price_increment != i64::MAX).then_some(d.min_price_increment)
                })
        })
    }
}

/// Data vintage of one DBN input, from its metadata (not yet a `qt.dataset_manifest` field: requested for schema
/// 1.1). The file bytes are pinned by the manifest's `inputs[].sha256`; record publisher
/// ids are kept per record in `qt.mbo.publisher_id`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DbnVintage {
    /// DBN version of the file (1 to [`SUPPORTED_DBN_VERSION`]).
    pub dbn_version: u8,
    /// Databento dataset code (`XNAS.ITCH`, `GLBX.MDP3`, ...).
    pub dataset: String,
    /// Schema name (`mbo`, `definition`, `trades`, ...), `None` for mixed files.
    pub schema: Option<String>,
    /// Query start, UTC ns.
    pub start_ns: u64,
    /// Query end, UTC ns (exclusive), when set.
    pub end_ns: Option<u64>,
    /// Input symbology type.
    pub stype_in: Option<String>,
    /// Output symbology type.
    pub stype_out: String,
    /// Record limit of the query, when set.
    pub limit: Option<u64>,
    /// Requested symbols (as in the metadata).
    pub symbols: Vec<String>,
    /// Symbols that did not resolve for the whole query range.
    pub partial: Vec<String>,
    /// Symbols that did not resolve at all.
    pub not_found: Vec<String>,
}

/// The vintage fields of one DBN file's metadata.
pub fn vintage_of(md: &Metadata) -> DbnVintage {
    DbnVintage {
        dbn_version: md.version,
        dataset: md.dataset.clone(),
        schema: md.schema.map(|s| s.as_str().to_owned()),
        start_ns: md.start,
        end_ns: md.end.map(std::num::NonZeroU64::get),
        stype_in: md.stype_in.map(|s| s.as_str().to_owned()),
        stype_out: md.stype_out.as_str().to_owned(),
        limit: md.limit.map(std::num::NonZeroU64::get),
        symbols: md.symbols.clone(),
        partial: md.partial.clone(),
        not_found: md.not_found.clone(),
    }
}

/// Opens a DBN file and returns its vintage fields.
pub fn vintage(path: &Path) -> QtResult<DbnVintage> {
    Ok(vintage_of(DbnReader::open(path)?.metadata()))
}

/// Schema and start of one file (for ordering and dispatch).
pub fn probe(path: &Path) -> QtResult<(Option<Schema>, u64)> {
    let r = DbnReader::open(path)?;
    Ok((r.metadata().schema, r.metadata().start))
}

/// `paths` sorted by metadata `start` (stable, so equal starts keep the given order).
pub fn chronological(paths: &[PathBuf]) -> QtResult<Vec<PathBuf>> {
    let mut keyed: Vec<(u64, &PathBuf)> = paths.iter().map(|p| Ok((probe(p)?.1, p))).collect::<QtResult<_>>()?;
    keyed.sort_by_key(|(start, _)| *start);
    Ok(keyed.into_iter().map(|(_, p)| p.clone()).collect())
}

/// Every MBO record of a file (tests and small inputs).
pub fn read_mbo(path: &Path, price_exp: i8) -> QtResult<Vec<MboEvent>> {
    let mut r = DbnReader::open(path)?;
    let mut out = Vec::new();
    while let Some(e) = r.next_mbo(price_exp)? {
        out.push(e);
    }
    Ok(out)
}
