//! Synthetic DBN writer: `MboEvent` -> DBN v3 `MboMsg` (`rtype` MBO, `ts_in_delta` 0). Metadata dataset
//! `XNAS.ITCH` when the first record's publisher id is 2, else `GLBX.MDP3`; `stype` instrument id; `start` = first
//! `ts_event`. Size, sequence, channel and timestamps are checked conversions (`QT-INPUT-002` on overflow).

use std::ffi::c_char;
use std::path::Path;

use dbn::Metadata;
use dbn::encode::{DbnEncoder, EncodeRecord};
use dbn::enums::{SType, Schema, rtype};
use dbn::flags::FlagSet;
use dbn::record::{MboMsg, RecordHeader};
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

/// Writes `events` as one DBN MBO file (Zstandard when the path ends in `.zst`).
pub(crate) fn encode_dbn(events: &[MboEvent], path: &Path) -> QtResult<()> {
    let msgs = events
        .iter()
        .enumerate()
        .map(|(i, e)| to_msg(i, e))
        .collect::<QtResult<Vec<_>>>()?;
    let dataset = if events.first().is_some_and(|e| e.publisher_id == 2) {
        "XNAS.ITCH"
    } else {
        "GLBX.MDP3"
    };
    let start = msgs.first().map_or(0, |m| m.hd.ts_event);
    let metadata = Metadata::builder()
        .dataset(dataset)
        .schema(Some(Schema::Mbo))
        .start(start)
        .stype_in(Some(SType::InstrumentId))
        .stype_out(SType::InstrumentId)
        .build();
    let enc_err = |e: dbn::Error| QtError::Engine(format!("{}: DBN encode: {e}", path.display()));
    let mut buf: Vec<u8> = Vec::new();
    if path.extension().is_some_and(|x| x == "zst") {
        // The zstd frame is finished when the encoder drops (writing into a Vec cannot fail).
        let mut enc = DbnEncoder::with_zstd(&mut buf, &metadata).map_err(enc_err)?;
        for m in &msgs {
            enc.encode_record(m).map_err(enc_err)?;
        }
        enc.flush().map_err(enc_err)?;
    } else {
        let mut enc = DbnEncoder::new(&mut buf, &metadata).map_err(enc_err)?;
        for m in &msgs {
            enc.encode_record(m).map_err(enc_err)?;
        }
        enc.flush().map_err(enc_err)?;
    }
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent).map_err(|e| io_err(parent, e))?;
    }
    std::fs::write(path, &buf).map_err(|e| io_err(path, e))?;
    Ok(())
}
