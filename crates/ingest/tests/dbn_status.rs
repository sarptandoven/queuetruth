//! DBN `status` records become trading-halt spans (SPEC 1.2 halt censoring, `docs/design/extensions/halt-censoring.md`
//! H.6): `Halt`, `Pause`, `Suspend` and `Quoting` (unless trading) open a span, `Trading` (unless not trading) closes
//! it, other actions change nothing; status records of unselected instruments, or of instruments without MBO records
//! that day, record nothing; a span still open ends at the next local midnight. Malformed status records are input
//! errors, never panics.

use std::path::{Path, PathBuf};

use ::dbn::encode::EncodeRecord;
use ::dbn::encode::dbn::Encoder;
use ::dbn::{FlagSet, MappingInterval, MboMsg, Metadata, RecordHeader, SType, Schema, StatusMsg, SymbolMapping, rtype};
use qt_ingest::dbn::DbnReader;
use qt_ingest::ingest;
use qt_schema::json::{IngestSummary, TradingHaltSpan};
use qt_testkit::StatusRecord;
use qt_types::{MboEvent, QtResult, flags};
use serde_json::json;

const NS: i64 = 1_000_000_000;
const HOUR: i64 = 3_600 * NS;
/// 2020-01-02 00:00 America/New_York (EST) in UTC ns.
const MID: i64 = 1_577_941_200 * NS;
const DAY: i64 = 24 * HOUR;

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-dbn-status-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn ev(iid: u32, ts: i64, order_id: u64, seq: u64) -> MboEvent {
    MboEvent {
        ts_event: MID + ts,
        ts_recv: MID + ts,
        instrument_id: iid,
        action: b'A',
        side: b'B',
        price: 10_000_000_000,
        size: 100,
        order_id,
        flags: flags::LAST,
        sequence: seq,
        channel_id: 0,
        publisher_id: 2,
    }
}

fn st(iid: u32, ts: i64, action: u16, reason: u16, is_trading: u8) -> StatusRecord {
    StatusRecord {
        instrument_id: iid,
        publisher_id: 2,
        ts_event: MID + ts,
        ts_recv: MID + ts,
        action,
        reason,
        is_trading,
    }
}

fn run(files: &[&Path], out: &Path) -> QtResult<IngestSummary> {
    let spec = json!({"format": "dbn", "venue": "nasdaq", "out": out.display().to_string(),
                      "inputs": files.iter().map(|f| json!({"path": f.display().to_string()})).collect::<Vec<_>>(),
                      "price_exp": -9, "tick_size": "0.01", "lot_size": "1", "tz": "America/New_York",
                      "symbols": ["7", "8"]});
    ingest(&serde_json::from_value(spec).unwrap())
}

fn span(iid: u32, a: i64, b: i64, state: &str, reason: &str) -> TradingHaltSpan {
    TradingHaltSpan {
        instrument_id: iid,
        ts_start: a,
        ts_end: b,
        state: state.into(),
        reason: reason.into(),
    }
}

#[test]
fn status_actions_map_to_trading_halt_spans() {
    let d = scratch("map");
    let mbo = d.join("mbo.dbn");
    let events: Vec<MboEvent> = [7, 8, 9]
        .into_iter()
        .enumerate()
        .flat_map(|(k, iid)| (0..3).map(move |i| ev(iid, 9 * HOUR + i * HOUR, (k * 10 + i as usize) as u64 + 1, 0)))
        .enumerate()
        .map(|(seq, mut e)| {
            e.sequence = seq as u64 + 1;
            e
        })
        .collect();
    qt_testkit::encode_dbn(&events, &mbo).unwrap();
    let status = d.join("status.dbn");
    let h = |x: f64| (x * HOUR as f64) as i64;
    qt_testkit::encode_dbn_status(
        &[
            st(7, h(9.5), 7, 1, b'Y'),    // trading at the open: no span
            st(7, h(10.0), 9, 50, b'N'),  // LULD pause
            st(7, h(10.1), 6, 0, b'N'),   // price indication: no change
            st(7, h(10.2), 7, 0, b'Y'),   // resumption
            st(7, h(11.0), 3, 15, b'Y'),  // quoting while trading: no change
            st(7, h(11.5), 7, 0, b'N'),   // trading while not trading: no change
            st(7, h(12.0), 3, 15, b'N'),  // quotation only (IPO): opens
            st(7, h(12.5), 8, 15, b'N'),  // halted in it: the span keeps its first state
            st(7, h(13.0), 7, 0, b'Y'),   // trading closes it
            st(7, h(15.0), 12, 1, b'N'),  // close: no change
            st(8, h(10.5), 10, 14, b'N'), // suspend, never lifted: ends at the next local midnight
            st(8, h(11.0), 15, 0, b'N'),  // not available for trading: no change
            st(9, h(10.0), 8, 30, b'N'),  // unselected instrument
            st(11, h(10.0), 8, 30, b'N'), // instrument without MBO records
        ],
        &status,
    )
    .unwrap();
    let s = run(&[&mbo, &status], &d.join("ds")).unwrap();
    assert_eq!(
        s.data_quality.trading_halts,
        vec![
            span(7, MID + h(10.0), MID + h(10.2), "dbn_status:9", "50"),
            span(7, MID + h(12.0), MID + h(13.0), "dbn_status:3", "15"),
            span(8, MID + h(10.5), MID + DAY, "dbn_status:10", "14"),
        ]
    );
    assert_eq!(s.data_quality.source_specific.get("dbn_status_records"), Some(&14));
    assert_eq!(s.data_quality.source_specific.get("dbn_other_records_skipped"), None);
    let _ = std::fs::remove_dir_all(&d);
}

/// A halt that runs past a session date's midnight carries on in the symbol's partition of the next date, whose
/// instrument id differs (CME, session date = UTC date): one span per partition, cut at midnight. The records are in
/// a mixed-schema file (status records between the MBO records).
#[test]
fn a_halt_across_midnight_carries_into_the_next_date() {
    const D0: i64 = 20_000;
    let d = scratch("midnight");
    let t0 = D0 * DAY;
    let mbo = |iid: u32, ts: i64, oid: u64, seq: u32| MboMsg {
        hd: RecordHeader::new::<MboMsg>(rtype::MBO, 1, iid, ts as u64),
        order_id: oid,
        price: 4_500_000_000_000,
        size: 1,
        flags: FlagSet::new(flags::LAST),
        channel_id: 0,
        action: b'A' as std::ffi::c_char,
        side: b'B' as std::ffi::c_char,
        ts_recv: ts as u64,
        ts_in_delta: 0,
        sequence: seq,
    };
    let status = |iid: u32, ts: i64, action: u16, is_trading: u8| StatusMsg {
        hd: RecordHeader::new::<StatusMsg>(rtype::STATUS, 1, iid, ts as u64),
        ts_recv: ts as u64,
        action,
        reason: 3,
        is_trading: is_trading as std::ffi::c_char,
        ..StatusMsg::default()
    };
    let date = |day: i64| {
        Metadata::builder()
            .dataset("GLBX.MDP3")
            .schema(None)
            .start((day * DAY) as u64)
            .stype_in(Some(SType::RawSymbol))
            .stype_out(SType::InstrumentId)
            .build()
            .start()
            .date()
    };
    let md = Metadata::builder()
        .dataset("GLBX.MDP3")
        .schema(None)
        .start(t0 as u64)
        .stype_in(Some(SType::RawSymbol))
        .stype_out(SType::InstrumentId)
        .mappings(vec![SymbolMapping {
            raw_symbol: "ESZ4".into(),
            intervals: vec![
                MappingInterval {
                    start_date: date(D0),
                    end_date: date(D0 + 1),
                    symbol: "42".into(),
                },
                MappingInterval {
                    start_date: date(D0 + 1),
                    end_date: date(D0 + 2),
                    symbol: "43".into(),
                },
            ],
        }])
        .build();
    let p = d.join("live.dbn");
    let mut enc = Encoder::new(std::fs::File::create(&p).unwrap(), &md).unwrap();
    let (halt, resume) = (t0 + 23 * HOUR + 50 * 60 * NS, t0 + DAY + 20 * 60 * NS);
    enc.encode_record(&mbo(42, t0 + 23 * HOUR, 1, 1)).unwrap();
    enc.encode_record(&status(42, halt, 8, b'N')).unwrap();
    enc.encode_record(&status(43, resume, 7, b'Y')).unwrap();
    enc.encode_record(&mbo(43, t0 + DAY + HOUR, 2, 2)).unwrap();
    drop(enc);
    let spec = json!({"format": "dbn", "venue": "cme", "out": d.join("ds").display().to_string(),
                      "inputs": [{"path": p.display().to_string()}], "tick_size": "0.25"});
    let s = ingest(&serde_json::from_value(spec).unwrap()).unwrap();
    assert_eq!(
        s.data_quality.trading_halts,
        vec![
            span(42, halt, t0 + DAY, "dbn_status:8", "3"),
            span(43, t0 + DAY, resume, "dbn_status:8", "3"),
        ]
    );
    assert_eq!(s.data_quality.source_specific.get("dbn_status_records"), Some(&2));
    let _ = std::fs::remove_dir_all(&d);
}

fn write_raw(path: &Path, schema: Schema, recs: &[StatusMsg]) {
    let md = Metadata::builder()
        .dataset("XNAS.ITCH")
        .schema(Some(schema))
        .start(MID as u64)
        .stype_in(Some(SType::InstrumentId))
        .stype_out(SType::InstrumentId)
        .build();
    let mut enc = Encoder::new(std::fs::File::create(path).unwrap(), &md).unwrap();
    for r in recs {
        enc.encode_record(r).unwrap();
    }
}

/// A status record whose `ts_event` is undefined is an input error, as for MBO records.
#[test]
fn undefined_status_time_is_an_input_error() {
    let d = scratch("undef");
    let rec = StatusMsg {
        hd: RecordHeader::new::<StatusMsg>(rtype::STATUS, 2, 7, u64::MAX),
        ts_recv: MID as u64,
        action: 8,
        ..StatusMsg::default()
    };
    for schema in [Schema::Status, Schema::Mbo] {
        let p = d.join(format!("{schema}.dbn"));
        write_raw(&p, schema, std::slice::from_ref(&rec));
        let mut r = DbnReader::open(&p).unwrap();
        let e = if schema == Schema::Status {
            r.next_status().unwrap_err()
        } else {
            r.next_mbo(-9).unwrap_err()
        };
        assert_eq!(e.code(), "QT-INPUT-002", "{e}");
        assert!(e.to_string().contains("undefined ts_event"), "{e}");
    }
    let _ = std::fs::remove_dir_all(&d);
}

/// `fuzz/regressions/dbn/status_record_too_short`: a record with the status `rtype` and only a 16-byte header (in
/// a `status` file) is `QT-INPUT-002` from both readers, not a panic or a silently skipped record.
#[test]
fn short_status_record_is_an_input_error() {
    let f = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fuzz/regressions/dbn/status_record_too_short");
    let data = std::fs::read(&f).unwrap();
    let d = scratch("short");
    let p = d.join("input.dbn");
    // Fuzz inputs are `[selector][file]`.
    std::fs::write(&p, &data[1..]).unwrap();
    let mut r = DbnReader::open(&p).unwrap();
    assert_eq!(r.metadata().schema, Some(Schema::Status));
    let e = r.next_status().unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002", "{e}");
    let mut r = DbnReader::open(&p).unwrap();
    let e = r.next_mbo(-9).unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002", "{e}");
    let _ = std::fs::remove_dir_all(&d);
}

/// The regression input is what `write_short_status_regression` writes (kept in step with the file).
#[test]
fn short_status_regression_is_current() {
    let f = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fuzz/regressions/dbn/status_record_too_short");
    assert_eq!(std::fs::read(&f).unwrap(), short_status_bytes());
}

/// `[selector 0][DBN v3 status file with one 16-byte status record]`.
fn short_status_bytes() -> Vec<u8> {
    let d = scratch("short-gen");
    let p = d.join("one.dbn");
    write_raw(&p, Schema::Status, &[StatusMsg::default()]);
    let mut b = std::fs::read(&p).unwrap();
    let _ = std::fs::remove_dir_all(&d);
    let rec = std::mem::size_of::<StatusMsg>();
    b.truncate(b.len() - rec + 16);
    let at = b.len() - 16;
    b[at] = 4; // length in 4-byte words: the header only
    b[at + 1] = rtype::STATUS;
    let mut out = vec![0u8];
    out.extend(b);
    out
}

/// Writes the regression input: `QT_WRITE_REGRESSION=1 cargo test -p queuetruth-ingest --test dbn_status`.
#[test]
fn write_short_status_regression() {
    if std::env::var_os("QT_WRITE_REGRESSION").is_some() {
        let f = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fuzz/regressions/dbn/status_record_too_short");
        std::fs::write(f, short_status_bytes()).unwrap();
    }
}
