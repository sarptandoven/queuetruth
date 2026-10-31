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
//! Symbology ([`Symbology`], in `dbn_symbology.rs`): Databento instrument ids are only unique per UTC date (and the
//! 2026-10-31 XNAS.ITCH re-issue assigns new ids to data before 2023-11-28), so ingest never joins dates on
//! `instrument_id` alone: the metadata mappings (`stype_out = instrument_id`) are kept as dated intervals (raw symbols,
//! or aliases for parent and continuous queries), definition and symbol-mapping records as dated entries, and
//! symbols, ticks, match algorithms and the symbol filter are resolved per (instrument id, UTC date). Definitions are
//! read from `definition` files and from mixed-schema files ([`DbnReader::scan_symbology`]); inputs whose symbology
//! disagrees on one date (two vintages, two datasets) are refused by [`Symbology::check`].

use std::collections::BTreeMap;
use std::fs::File;
use std::io::BufReader;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::AtomicU64;

use ::dbn::decode::dbn::Decoder;
use ::dbn::decode::{DbnMetadata, DecodeRecordRef, DynReader};
use ::dbn::{
    ErrorMsg, InstrumentDefMsg, MboMsg, Metadata, RecordRef, SType, Schema, SymbolMappingMsg, SystemCode, SystemMsg,
    TradeMsg, VersionUpgradePolicy,
};
use qt_schema::rows::{TradeKind, TradeRow};
use qt_types::{MboEvent, QtError, QtResult, Side, UNDEF_PRICE, VenueRules};

use crate::guard::{CountingReader, GuardLimits, GuardedReader, READ_BUF, open_file};
use crate::units::rescale;

#[path = "dbn_symbology.rs"]
mod symbology;

pub use symbology::{
    DbnDefinition, Symbology, SymbologyStats, XNAS_ITCH, XNAS_REISSUE_CUTOVER_DAY, format_utc_day,
    is_continuous_symbol, looks_like_smart_symbol, match_algorithm_name, utc_day,
};

type Inner = FrameCheck<GuardedReader<DynReader<'static, BufReader<CountingReader<File>>>>>;

/// Largest accepted DBN metadata block.
pub const MAX_METADATA_BYTES: u64 = 256 << 20;

/// Smallest possible DBN metadata block: the 100 fixed bytes and the 4-byte schema-definition length that follows
/// them. The pinned `dbn` decoder rejects blocks shorter than 100 bytes but panics on 100 to 103
/// (`fuzz/regressions/dbn/`), so `FrameCheck` rejects them first.
pub const MIN_METADATA_BYTES: u64 = 104;

/// Newest DBN version the pinned `dbn` crate decodes (versions 1 to this one are accepted).
pub const SUPPORTED_DBN_VERSION: u8 = ::dbn::DBN_VERSION;

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
    metadata_cap: u64,
}

impl<R> FrameCheck<R> {
    /// Wraps a decompressed DBN byte stream.
    pub fn new(inner: R) -> Self {
        Self {
            inner,
            state: Frame::Prelude { buf: [0; 8], got: 0 },
            offset: 0,
            metadata_cap: MAX_METADATA_BYTES,
        }
    }

    /// Rejects a metadata block longer than `cap` bytes (at most `MAX_METADATA_BYTES`) before the decoder allocates
    /// it: the pinned `dbn` decoder grows its buffer to the announced length before reading any of it
    /// (`fuzz/regressions/dbn/alloc_*`).
    pub fn with_metadata_cap(mut self, cap: u64) -> Self {
        self.metadata_cap = cap.min(MAX_METADATA_BYTES);
        self
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
                        if len > self.metadata_cap {
                            return Err(bad(format!(
                                "DBN metadata length {len} cannot fit in this file (at most {} bytes)",
                                self.metadata_cap
                            )));
                        }
                        if len < MIN_METADATA_BYTES {
                            return Err(bad(format!(
                                "DBN metadata length {len} is shorter than its fixed fields ({MIN_METADATA_BYTES} \
                                 bytes)"
                            )));
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
    /// Records skipped because they were not of the requested type (gateway errors excluded).
    pub skipped: u64,
    /// Gateway error records skipped: `ErrorMsg` and slow-reader `SystemMsg` (live captures: a symbol that did not
    /// resolve, records the gateway may have dropped). Never expected in clean data.
    pub gateway_errors: u64,
    /// Records read (all types).
    pub records: u64,
}

/// The largest metadata block a DBN file of this size can hold: an uncompressed file (it starts with `DBN`) holds
/// at most its size minus the 8-byte prelude; a compressed one at most what the decompression guard lets it inflate
/// to before the ratio check (`GuardLimits`).
fn metadata_cap(path: &Path, limits: &GuardLimits) -> QtResult<u64> {
    use std::io::Read;
    let mut f = open_file(path)?;
    let size = f
        .metadata()
        .map(|m| m.len())
        .map_err(|e| fmt_err(path, e.to_string()))?;
    let mut magic = [0u8; 3];
    let plain = f.read_exact(&mut magic).is_ok() && &magic == b"DBN";
    Ok(if plain {
        size.saturating_sub(8)
    } else {
        limits.min_checked.max(size.saturating_mul(limits.max_ratio))
    })
}

impl DbnReader {
    /// Opens a DBN file with the default decompression guard.
    pub fn open(path: &Path) -> QtResult<Self> {
        Self::open_with(path, GuardLimits::default())
    }

    /// Opens a DBN file with explicit decompression limits.
    pub fn open_with(path: &Path, limits: GuardLimits) -> QtResult<Self> {
        let cap = metadata_cap(path, &limits)?;
        let file = open_file(path)?;
        let count = Arc::new(AtomicU64::new(0));
        let src = BufReader::with_capacity(READ_BUF, CountingReader::new(file, count.clone()));
        let dynr = DynReader::inferred_with_buffer(src).map_err(|e| fmt_err(path, describe(&e)))?;
        let guarded = FrameCheck::new(GuardedReader::new(dynr, count, limits)).with_metadata_cap(cap);
        let dec = Decoder::with_upgrade_policy(guarded, VersionUpgradePolicy::AsIs)
            .map_err(|e| fmt_err(path, format!("not a readable DBN file: {}", describe(&e))))?;
        Ok(Self {
            dec,
            path: path.to_owned(),
            skipped: 0,
            gateway_errors: 0,
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

    /// Counts one skipped record (`gateway` from [`is_gateway_error`]).
    fn skip(&mut self, gateway: bool) {
        if gateway {
            self.gateway_errors += 1;
        } else {
            self.skipped += 1;
        }
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
        let version = self.dec.metadata().version;
        loop {
            let path = self.path.clone();
            let Some(rec) = self.next_ref()? else { return Ok(None) };
            if rec.has::<MboMsg>() {
                let m = rec.try_get::<MboMsg>().map_err(|e| fmt_err(&path, describe(&e)))?;
                let out = convert_mbo(m, price_exp);
                return out.map(Some).map_err(|e| self.conv_err(e));
            }
            let gateway = is_gateway_error(&rec, version);
            self.skip(gateway);
        }
    }

    /// Next trade print converted at `price_exp` (other record types are skipped and counted).
    pub fn next_trade(&mut self, price_exp: i8, rules: &VenueRules) -> QtResult<Option<TradeRow>> {
        let version = self.dec.metadata().version;
        loop {
            let path = self.path.clone();
            let Some(rec) = self.next_ref()? else { return Ok(None) };
            if rec.has::<TradeMsg>() {
                let t = rec.try_get::<TradeMsg>().map_err(|e| fmt_err(&path, describe(&e)))?;
                let out = convert_trade(t, price_exp, rules);
                return out.map(Some).map_err(|e| self.conv_err(e));
            }
            let gateway = is_gateway_error(&rec, version);
            self.skip(gateway);
        }
    }

    /// Next instrument definition as `(instrument_id, raw_symbol, min_price_increment at 1e-9, ts_recv)` (an
    /// undefined increment is `i64::MAX`, as stored); see [`Self::next_definition_record`] for every field.
    pub fn next_definition(&mut self) -> QtResult<Option<(u32, String, i64, u64)>> {
        Ok(self.next_definition_record()?.map(|d| {
            (
                d.instrument_id,
                d.raw_symbol,
                d.min_price_increment.unwrap_or(i64::MAX),
                d.ts_recv,
            )
        }))
    }

    /// Next instrument definition record, read with the file's own DBN version (other record types are skipped and
    /// counted).
    pub fn next_definition_record(&mut self) -> QtResult<Option<DbnDefinition>> {
        let version = self.dec.metadata().version;
        loop {
            let path = self.path.clone();
            let Some(rec) = self.next_ref()? else { return Ok(None) };
            if rec.has::<InstrumentDefMsg>() {
                return definition_of(&rec, version).map(Some).map_err(|e| fmt_err(&path, e));
            }
            let gateway = is_gateway_error(&rec, version);
            self.skip(gateway);
        }
    }

    /// Reads the file's metadata and every definition and symbol-mapping record into `sym` (a `definition` file,
    /// or a mixed-schema file such as a live capture, whose other records are skipped and counted). Returns the
    /// number of records added.
    pub fn scan_symbology(&mut self, sym: &mut Symbology) -> QtResult<u64> {
        let version = self.dec.metadata().version;
        sym.add_metadata_from(self.dec.metadata(), &self.path);
        let label = self.path.display().to_string();
        let mut added = 0;
        loop {
            let path = self.path.clone();
            let Some(rec) = self.next_ref()? else { return Ok(added) };
            if rec.has::<InstrumentDefMsg>() {
                let d = definition_of(&rec, version).map_err(|e| fmt_err(&path, e))?;
                sym.add_definition_record(&d, &label);
                added += 1;
            } else if rec.has::<SymbolMappingMsg>() {
                let (iid, raw, alias, at) = symbol_mapping_of(&rec, version).map_err(|e| fmt_err(&path, e))?;
                sym.add_symbol_mapping(iid, raw.as_deref(), alias.as_deref(), at, &label);
                added += 1;
            } else {
                let gateway = is_gateway_error(&rec, version);
                self.skip(gateway);
            }
        }
    }
}

/// A defined positive 1e-9 quantity (`i64::MAX` is DBN's undefined price).
fn defined_px(v: i64) -> Option<i64> {
    (v > 0 && v != i64::MAX).then_some(v)
}

/// A defined publisher instrument id: 0, `i32::MAX` (the SBE `Int32` null; Databento's old-vintage XNAS.ITCH
/// definitions carry it), `u32::MAX` and `u64::MAX` are undefined.
fn defined_raw_id(v: u64) -> Option<u64> {
    const I32_NULL: u64 = i32::MAX as u64;
    (v != 0 && v != I32_NULL && v != u64::from(u32::MAX) && v != u64::MAX).then_some(v)
}

/// A security update action byte: `A`, `M` or `D`, else `?`.
fn update_action(b: u8) -> u8 {
    if matches!(b, b'A' | b'M' | b'D') { b } else { b'?' }
}

/// True for a gateway error record: an `ErrorMsg` (for example a symbol that did not resolve), or a `SystemMsg`
/// whose code is a slow-reader warning (records may have been dropped). Heartbeats, subscription and replay
/// acknowledgements, and DBN v1 system messages (which carry no code) are not errors.
fn is_gateway_error(rec: &RecordRef<'_>, version: u8) -> bool {
    if rec.has::<ErrorMsg>() {
        return true;
    }
    version > 1
        && rec.has::<SystemMsg>()
        && rec
            .try_get::<SystemMsg>()
            .is_ok_and(|m| matches!(m.code(), Ok(SystemCode::SlowReaderWarning)))
}

/// A NUL-padded DBN string field, trimmed (an invalid UTF-8 field reads as empty).
fn text(r: ::dbn::Result<&str>) -> String {
    r.map(|s| s.trim().to_owned()).unwrap_or_default()
}

/// One definition record of a file of DBN `version`, as a [`DbnDefinition`].
fn definition_of(rec: &RecordRef<'_>, version: u8) -> Result<DbnDefinition, String> {
    // `security_update_action` is read as a raw byte: DBN v1 declares it as a Rust enum, so reading the field of a
    // record whose byte is not a variant would be undefined behaviour.
    macro_rules! def {
        ($t:ty) => {{
            let action = AsRef::<[u8]>::as_ref(rec)
                .get(std::mem::offset_of!($t, security_update_action))
                .copied()
                .map_or(b'?', update_action);
            let d = rec.try_get::<$t>().map_err(|e| describe(&e))?;
            DbnDefinition {
                instrument_id: d.hd.instrument_id,
                ts_recv: d.ts_recv,
                ts_event: d.hd.ts_event,
                raw_symbol: text(d.raw_symbol()),
                raw_instrument_id: defined_raw_id(u64::from(d.raw_instrument_id)),
                min_price_increment: defined_px(d.min_price_increment),
                min_price_increment_amount: defined_px(d.min_price_increment_amount),
                display_factor: (d.display_factor != i64::MAX).then_some(d.display_factor),
                min_lot_size_round_lot: (d.min_lot_size_round_lot > 0 && d.min_lot_size_round_lot != i32::MAX)
                    .then_some(d.min_lot_size_round_lot),
                instrument_class: d.instrument_class as u8,
                match_algorithm: d.match_algorithm as u8,
                security_update_action: action,
                exchange: text(d.exchange()),
                asset: text(d.asset()),
                security_type: text(d.security_type()),
                currency: text(d.currency()),
            }
        }};
    }
    Ok(match version {
        1 => def!(::dbn::v1::InstrumentDefMsg),
        2 => def!(::dbn::v2::InstrumentDefMsg),
        _ => def!(::dbn::v3::InstrumentDefMsg),
    })
}

/// One symbol-mapping record as `(instrument_id, raw symbol, alias, valid from)`: the output symbol is a raw symbol
/// when the output symbology is `raw_symbol` (DBN v1 records, from live sessions, always map to raw symbols), else
/// the input symbol is when the input symbology is; the other is an alias. Valid from `start_ts`, else `ts_event`.
fn symbol_mapping_of(rec: &RecordRef<'_>, version: u8) -> Result<(u32, Option<String>, Option<String>, u64), String> {
    let pick = |a: u64, b: u64| if a != UNDEF_TIMESTAMP { a } else { b };
    if version == 1 {
        let m = rec.try_get::<::dbn::v1::SymbolMappingMsg>().map_err(|e| describe(&e))?;
        let (i, o) = (text(m.stype_in_symbol()), text(m.stype_out_symbol()));
        return Ok((m.hd.instrument_id, Some(o), Some(i), pick(m.start_ts, m.hd.ts_event)));
    }
    let m = rec.try_get::<::dbn::v3::SymbolMappingMsg>().map_err(|e| describe(&e))?;
    let (i, o) = (text(m.stype_in_symbol()), text(m.stype_out_symbol()));
    let (raw, alias) = match (m.stype_in(), m.stype_out()) {
        (_, Ok(SType::RawSymbol)) => (Some(o), Some(i)),
        (Ok(SType::RawSymbol), _) => (Some(i), None),
        _ => (None, Some(i)),
    };
    Ok((m.hd.instrument_id, raw, alias, pick(m.start_ts, m.hd.ts_event)))
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
