//! Synthetic DBN writer: `MboEvent` -> DBN v3 `MboMsg` (`rtype` MBO, `ts_in_delta` 0), and [`StatusRecord`] -> DBN
//! `StatusMsg` (`rtype` status). Metadata dataset `XNAS.ITCH` when the first record's publisher id is 2, else
//! `GLBX.MDP3`; `stype` instrument id; `start` = first `ts_event`. Size, sequence, channel and timestamps are
//! checked conversions (`QT-INPUT-002` on overflow).

use std::ffi::c_char;
use std::path::Path;

use dbn::Metadata;
use dbn::encode::{DbnEncodable, DbnEncoder, EncodeRecord};
use dbn::enums::{SType, Schema, rtype};
use dbn::flags::FlagSet;
use dbn::record::{MboMsg, RecordHeader, StatusMsg};
use qt_types::{MboEvent, QtError, QtResult};

fn bad(i: usize, field: &str, v: impl std::fmt::Display) -> QtError {
    QtError::InputFormat {
        location: format!("record {i}"),
        msg: format!("{field} {v} does not fit the DBN MBO field"),
    }
}

fn to_msg(i: usize, e: &MboEvent) -> QtResult<MboMsg> {
    let ts_event = u64::try_from(e.ts_event).map_err(|_| bad(i, "ts_event", e.ts_event))?;
    let ts_recv = u64::try_from(e.ts_recv).map_err(|_| bad(i, "ts_recv", e.ts_recv))?;
    Ok(MboMsg {
        hd: RecordHeader::new::<MboMsg>(rtype::MBO, e.publisher_id, e.instrument_id, ts_event),
        order_id: e.order_id,
        price: e.price,
        size: u32::try_from(e.size).map_err(|_| bad(i, "size", e.size))?,
        flags: FlagSet::new(e.flags),
        channel_id: u8::try_from(e.channel_id).map_err(|_| bad(i, "channel_id", e.channel_id))?,
        action: e.action as c_char,
        side: e.side as c_char,
        ts_recv,
        ts_in_delta: 0,
        sequence: u32::try_from(e.sequence).map_err(|_| bad(i, "sequence", e.sequence))?,
    })
}

fn io_err(path: &Path, e: impl std::fmt::Display) -> QtError {
    QtError::InputMissing {
        path: path.display().to_string(),
        msg: e.to_string(),
    }
}

/// One synthetic DBN status record (`StatusMsg`; `action` and `reason` are Databento's `StatusAction` and
/// `StatusReason` codes, `is_trading` `Y`, `N` or `~`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StatusRecord {
    /// Databento instrument id.
    pub instrument_id: u32,
    /// Publisher id (2: XNAS.ITCH).
    pub publisher_id: u16,
    /// Venue time, UTC ns.
    pub ts_event: i64,
    /// Capture time, UTC ns.
    pub ts_recv: i64,
    /// `StatusAction` code (7 trading, 8 halt, 9 pause, ...).
    pub action: u16,
    /// `StatusReason` code (0 none).
    pub reason: u16,
    /// `is_trading` byte.
    pub is_trading: u8,
}

fn to_status(i: usize, s: &StatusRecord) -> QtResult<StatusMsg> {
    let ts_event = u64::try_from(s.ts_event).map_err(|_| bad(i, "ts_event", s.ts_event))?;
    let ts_recv = u64::try_from(s.ts_recv).map_err(|_| bad(i, "ts_recv", s.ts_recv))?;
    Ok(StatusMsg {
        hd: RecordHeader::new::<StatusMsg>(rtype::STATUS, s.publisher_id, s.instrument_id, ts_event),
        ts_recv,
        action: s.action,
        reason: s.reason,
        trading_event: 0,
        is_trading: s.is_trading as c_char,
        is_quoting: b'~' as c_char,
        is_short_sell_restricted: b'~' as c_char,
        ..StatusMsg::default()
    })
}

/// One record of a file: MBO or status.
enum Rec {
    Mbo(MboMsg),
    Status(StatusMsg),
}

impl Rec {
    fn header(&self) -> &RecordHeader {
        match self {
            Self::Mbo(m) => &m.hd,
            Self::Status(m) => &m.hd,
        }
    }

    fn encode<W: std::io::Write>(&self, enc: &mut DbnEncoder<W>) -> dbn::Result<()> {
        fn one<W: std::io::Write, R: DbnEncodable>(enc: &mut DbnEncoder<W>, r: &R) -> dbn::Result<()> {
            enc.encode_record(r)
        }
        match self {
            Self::Mbo(m) => one(enc, m),
            Self::Status(m) => one(enc, m),
        }
    }
}

fn write_file(recs: &[Rec], schema: Schema, path: &Path) -> QtResult<()> {
    let dataset = if recs.first().is_some_and(|r| r.header().publisher_id == 2) {
        "XNAS.ITCH"
    } else {
        "GLBX.MDP3"
    };
    let start = recs.first().map_or(0, |r| r.header().ts_event);
    let metadata = Metadata::builder()
        .dataset(dataset)
        .schema(Some(schema))
        .start(start)
        .stype_in(Some(SType::InstrumentId))
        .stype_out(SType::InstrumentId)
        .build();
    let enc_err = |e: dbn::Error| QtError::Engine(format!("{}: DBN encode: {e}", path.display()));
    let mut buf: Vec<u8> = Vec::new();
    if path.extension().is_some_and(|x| x == "zst") {
        // The zstd frame is finished when the encoder drops (writing into a Vec cannot fail).
        let mut enc = DbnEncoder::with_zstd(&mut buf, &metadata).map_err(enc_err)?;
        for r in recs {
            r.encode(&mut enc).map_err(enc_err)?;
        }
        enc.flush().map_err(enc_err)?;
    } else {
        let mut enc = DbnEncoder::new(&mut buf, &metadata).map_err(enc_err)?;
        for r in recs {
            r.encode(&mut enc).map_err(enc_err)?;
        }
        enc.flush().map_err(enc_err)?;
    }
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent).map_err(|e| io_err(parent, e))?;
    }
    std::fs::write(path, &buf).map_err(|e| io_err(path, e))?;
    Ok(())
}

/// Writes `events` as one DBN MBO file (Zstandard when the path ends in `.zst`).
pub(crate) fn encode_dbn(events: &[MboEvent], path: &Path) -> QtResult<()> {
    encode_dbn_with_status(events, &[], path)
}

/// Writes `events` and `status` as one DBN file of schema `mbo`: the events in the given order, each status record
/// (in the given order) just before the first event with a later `ts_recv`, the rest at the end.
pub(crate) fn encode_dbn_with_status(events: &[MboEvent], status: &[StatusRecord], path: &Path) -> QtResult<()> {
    let mut recs = Vec::with_capacity(events.len() + status.len());
    let mut pending = status.iter().enumerate().peekable();
    for (i, e) in events.iter().enumerate() {
        let m = to_msg(i, e)?;
        while let Some((j, s)) = pending.next_if(|(_, s)| s.ts_recv <= e.ts_recv) {
            recs.push(Rec::Status(to_status(j, s)?));
        }
        recs.push(Rec::Mbo(m));
    }
    for (j, s) in pending {
        recs.push(Rec::Status(to_status(j, s)?));
    }
    write_file(&recs, Schema::Mbo, path)
}

/// Writes `status` as one DBN file of schema `status`.
pub(crate) fn encode_dbn_status(status: &[StatusRecord], path: &Path) -> QtResult<()> {
    let recs = status
        .iter()
        .enumerate()
        .map(|(i, s)| to_status(i, s).map(Rec::Status))
        .collect::<QtResult<Vec<_>>>()?;
    write_file(&recs, Schema::Status, path)
}
