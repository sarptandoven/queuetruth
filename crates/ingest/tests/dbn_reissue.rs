//! DBN hardening for Databento's 2026-10-31 XNAS.ITCH re-issue and DBN v4 (all DBN files are encoded in-process
//! with the pinned `dbn` crate; no vendor bytes):
//! (a) midnight-UTC MBO snapshots mid-file (an unflagged `R` then `SNAPSHOT` adds whose `ts_event` is the resting
//!     order's original time) are routed by the snapshot time, kept in feed order, and are not anomalies;
//! (b) winter after-hours data up to 01:00 UTC stays on its US/Eastern session date (EST and EDT dates), outside
//!     the session window but kept;
//! (c) instrument ids are resolved per UTC date (same symbol with different ids, a different symbol reusing an id);
//!     a session spanning two UTC dates files a re-assigned id under the session date's id;
//! (d) data vintage fields of a DBN input; (e) an unsupported DBN version header is QT-INPUT-002 naming the version
//!     and the supported range (plain and zstd), never a panic.

use std::ffi::c_char;
use std::io::Write;
use std::num::NonZeroU64;
use std::path::{Path, PathBuf};

use ::dbn::encode::EncodeRecord;
use ::dbn::encode::dbn::Encoder;
use ::dbn::{
    FlagSet, InstrumentDefMsg, MappingInterval, MboMsg, Metadata, RecordHeader, SType, Schema, SymbolMapping, rtype,
};
use qt_ingest::{Dataset, ingest};
use qt_schema::json::{IngestSummary, Verdict};
use qt_schema::specs::IngestSpec;
use qt_types::{MboEvent, UNDEF_PRICE, flags};
use serde_json::json;

const D: i64 = 86_400_000_000_000;
const H: i64 = 3_600_000_000_000;
const S: i64 = 1_000_000_000;
const PX: i64 = 185_250_000_000; // 185.25 at 1e-9

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-dbn-reissue-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn spec(v: serde_json::Value) -> IngestSpec {
    serde_json::from_value(v).unwrap()
}

#[allow(clippy::too_many_arguments)]
fn ev(iid: u32, ts_event: i64, ts_recv: i64, action: u8, side: u8, oid: u64, fl: u8, seq: u64) -> MboEvent {
    MboEvent {
        ts_event,
        ts_recv,
        instrument_id: iid,
        action,
        side,
        price: if action == b'R' { UNDEF_PRICE } else { PX },
        size: if action == b'R' { 0 } else { 100 },
        order_id: if action == b'R' { 0 } else { oid },
        flags: fl,
        sequence: seq,
        channel_id: 0,
        publisher_id: 2,
    }
}

/// A normal record (`ts_recv = ts_event`, LAST).
fn n(iid: u32, ts: i64, action: u8, side: u8, oid: u64, seq: u64) -> MboEvent {
    ev(iid, ts, ts, action, side, oid, flags::LAST, seq)
}

fn mbo_msg(e: &MboEvent) -> MboMsg {
    MboMsg {
        hd: RecordHeader::new::<MboMsg>(rtype::MBO, e.publisher_id, e.instrument_id, e.ts_event as u64),
        order_id: e.order_id,
        price: e.price,
        size: e.size as u32,
        flags: FlagSet::new(e.flags),
        channel_id: e.channel_id as u8,
        action: e.action as c_char,
        side: e.side as c_char,
        ts_recv: e.ts_recv as u64,
        ts_in_delta: 0,
        sequence: e.sequence as u32,
    }
}

fn metadata(dataset: &str, schema: Schema, day0: i32, day1: i32, mappings: Vec<SymbolMapping>) -> Metadata {
    Metadata::builder()
        .dataset(dataset)
        .schema(Some(schema))
        .start((i64::from(day0) * D) as u64)
        .end(NonZeroU64::new((i64::from(day1) * D) as u64))
        .stype_in(Some(SType::RawSymbol))
        .stype_out(SType::InstrumentId)
        .mappings(mappings)
        .build()
}

/// Symbology of one raw symbol: `[d0, d1)` UTC dates -> instrument id.
fn mapping(raw: &str, spans: &[(i32, i32, u32)]) -> SymbolMapping {
    let date = |day: i32| metadata("X", Schema::Mbo, day, day + 1, vec![]).start().date();
    SymbolMapping {
        raw_symbol: raw.into(),
        intervals: spans
            .iter()
            .map(|&(a, b, id)| MappingInterval {
                start_date: date(a),
                end_date: date(b),
                symbol: id.to_string(),
            })
            .collect(),
    }
}

fn write_dbn<R: ::dbn::encode::DbnEncodable>(path: &Path, md: &Metadata, recs: &[R]) {
    let f = std::fs::File::create(path).unwrap();
    let mut enc = Encoder::new(f, md).unwrap();
    for r in recs {
        enc.encode_record(r).unwrap();
    }
}

fn write_mbo(path: &Path, md: &Metadata, events: &[MboEvent]) {
    write_dbn(path, md, &events.iter().map(mbo_msg).collect::<Vec<_>>());
}

fn partitions(root: &Path) -> Vec<(i32, u32, Vec<MboEvent>)> {
    let ds = Dataset::open(root).unwrap();
    ds.partitions()
        .iter()
        .map(|p| {
            (
                p.session_date,
                p.instrument_id,
                p.mbo().unwrap().map(Result::unwrap).collect(),
            )
        })
        .collect()
}

fn run(dir: &Path, name: &str, files: &[&Path], extra: serde_json::Value) -> (PathBuf, IngestSummary) {
    let out = dir.join(name);
    let mut v = json!({"format": "dbn", "out": out.to_str().unwrap(),
        "inputs": files.iter().map(|f| json!({"path": f.to_str().unwrap()})).collect::<Vec<_>>()});
    for (k, x) in extra.as_object().unwrap() {
        v.as_object_mut().unwrap().insert(k.clone(), x.clone());
    }
    let s = ingest(&spec(v)).unwrap();
    (out, s)
}

// ---- (a) midnight-UTC snapshot inside an MBO file (CME, UTC sessions) -------------------------------------------

#[test]
fn a_midnight_snapshot_mid_file_is_routed_by_snapshot_time_and_not_an_anomaly() {
    let dir = scratch("snap");
    let d0: i32 = 20_000;
    let t0 = i64::from(d0) * D;
    let mid = t0 + D;
    let day0 = vec![
        n(7, t0 + 10 * H, b'A', b'B', 1, 1),
        n(7, t0 + 10 * H + S, b'A', b'A', 2, 2),
        n(7, t0 + 10 * H + 2 * S, b'C', b'B', 1, 3),
        n(7, t0 + 23 * H, b'A', b'B', 3, 4),
    ];
    // Snapshot at 00:00 UTC: an unflagged clear, then the resting orders flagged SNAPSHOT with their original
    // ts_event (previous UTC day) and ts_recv = the snapshot time.
    let snap = vec![
        ev(7, mid, mid, b'R', b'N', 0, 0, 0),
        ev(7, t0 + 10 * H + S, mid, b'A', b'A', 2, flags::SNAPSHOT, 0),
        ev(7, t0 + 23 * H, mid, b'A', b'B', 3, flags::SNAPSHOT | flags::LAST, 0),
    ];
    let day1 = vec![n(7, mid + H, b'A', b'B', 4, 5), n(7, mid + H + 1, b'C', b'A', 2, 6)];
    let all: Vec<MboEvent> = day0.iter().chain(&snap).chain(&day1).copied().collect();
    let f = dir.join("glbx.mbo.dbn");
    let map = vec![mapping("ESZ4", &[(d0, d0 + 2, 7)])];
    write_mbo(&f, &metadata("GLBX.MDP3", Schema::Mbo, d0, d0 + 2, map), &all);
    let (out, s) = run(&dir, "ds", &[&f], json!({"venue": "cme", "tick_size": "0.25"}));
    let next: Vec<MboEvent> = snap.iter().chain(&day1).copied().collect();
    assert_eq!(partitions(&out), vec![(d0, 7, day0), (d0 + 1, 7, next)]);
    let dq = &s.data_quality;
    assert_eq!(
        (
            dq.records,
            dq.snapshot_records,
            dq.out_of_order_ts_event,
            dq.sequence_gaps,
            dq.sequence_regressions,
            dq.feed_gaps.len()
        ),
        (9, 2, 0, 0, 0, 0)
    );
    assert_eq!(dq.verdict, Verdict::Clean, "{:?}", dq.reasons);
    let _ = std::fs::remove_dir_all(&dir);
}

// ---- (b) Nasdaq after-hours up to 01:00 UTC stays on the Eastern date ------------------------------------------

/// Two UTC-day files around Eastern date `d1` (`off` = hours from Eastern to UTC midnight: 5 EST, 4 EDT).
fn after_hours_case(d1: i32, off: i64) {
    let dir = scratch(&format!("ah-{d1}"));
    let u1 = i64::from(d1) * D;
    let u2 = u1 + D;
    let id = 13;
    // UTC file d1: starts with the previous Eastern session's after-hours (00:30 UTC).
    let f1_recs = vec![
        n(id, u1 + 30 * 60 * S, b'A', b'B', 100, 900),
        n(id, u1 + (9 + off) * H + 30 * 60 * S, b'A', b'B', 1, 1), // 09:30 ET
        n(id, u1 + (10 + off) * H, b'A', b'A', 2, 2),
        n(id, u1 + 23 * H, b'A', b'B', 3, 3),
        n(id, u1 + 23 * H + 30 * 60 * S, b'C', b'B', 1, 4),
    ];
    // UTC file d1 + 1: midnight snapshot (clear + flagged adds), after-hours to 00:59 UTC, then the next session.
    let f2_recs = vec![
        ev(id, u2, u2, b'R', b'N', 0, 0, 0),
        ev(id, u1 + (10 + off) * H, u2, b'A', b'A', 2, flags::SNAPSHOT, 0),
        ev(id, u1 + 23 * H, u2, b'A', b'B', 3, flags::SNAPSHOT | flags::LAST, 0),
        n(id, u2 + 10 * 60 * S, b'A', b'B', 4, 5),
        n(id, u2 + 59 * 60 * S, b'C', b'B', 4, 6),
        n(id, u2 + (9 + off) * H + 30 * 60 * S, b'A', b'B', 1, 1), // next session restarts its numbering
    ];
    let map = vec![mapping("AAPL", &[(d1 - 1, d1 + 2, id)])];
    let (f1, f2) = (dir.join("d1.mbo.dbn"), dir.join("d2.mbo.dbn"));
    write_mbo(
        &f1,
        &metadata("XNAS.ITCH", Schema::Mbo, d1, d1 + 1, map.clone()),
        &f1_recs,
    );
    write_mbo(&f2, &metadata("XNAS.ITCH", Schema::Mbo, d1 + 1, d1 + 2, map), &f2_recs);
    // Inputs given out of order: files are ordered by metadata start.
    let (out, s) = run(&dir, "ds", &[&f2, &f1], json!({"venue": "nasdaq"}));
    let session: Vec<MboEvent> = f1_recs[1..].iter().chain(&f2_recs[..5]).copied().collect();
    assert_eq!(
        partitions(&out),
        vec![
            (d1 - 1, id, vec![f1_recs[0]]),
            (d1, id, session.clone()),
            (d1 + 1, id, vec![f2_recs[5]])
        ]
    );
    let ds = Dataset::open(&out).unwrap();
    let i = ds.instrument(id, d1).unwrap();
    assert_eq!(
        (i.session_tz.as_str(), i.midnight_utc_ns, i.open_s, i.close_s),
        ("America/New_York", u1 + off * H, 34_200, 57_600)
    );
    // The last after-hours record is past close_s (out of session for shadows) but kept on its Eastern date.
    let last = session.last().unwrap().ts_event;
    assert!(last >= i.midnight_utc_ns + 57_600 * S && last < u2 + H);
    assert_eq!(ds.partition_for(id, last).map(|p| p.session_date), Some(d1));
    let dq = &s.data_quality;
    assert_eq!(
        (
            dq.snapshot_records,
            dq.out_of_order_ts_event,
            dq.sequence_gaps,
            dq.sequence_regressions
        ),
        (2, 0, 0, 0)
    );
    assert_eq!(dq.verdict, Verdict::Clean, "{:?}", dq.reasons);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn b_est_after_hours_to_0100_utc_stay_on_the_eastern_date() {
    after_hours_case(20_467, 5); // 2026-01-14 (EST)
}

#[test]
fn b_edt_after_hours_stay_on_the_eastern_date() {
    after_hours_case(20_648, 4); // 2026-07-14 (EDT)
}

// ---- (c) instrument ids per UTC date ----------------------------------------------------------------------------

fn def(iid: u32, symbol: &str, mpi: i64, ts_recv: i64) -> InstrumentDefMsg {
    let mut d = InstrumentDefMsg {
        hd: RecordHeader::new::<InstrumentDefMsg>(rtype::INSTRUMENT_DEF, 2, iid, ts_recv as u64),
        ts_recv: ts_recv as u64,
        min_price_increment: mpi,
        ..Default::default()
    };
    for (i, b) in symbol.bytes().enumerate() {
        d.raw_symbol[i] = b as c_char;
    }
    d
}

#[test]
fn c_instrument_ids_are_resolved_per_utc_date() {
    let dir = scratch("ids");
    let d1: i32 = 20_467; // 2026-01-14 (EST), UTC dates d1 and d1 + 1
    let d2 = d1 + 1;
    let (u1, u2) = (i64::from(d1) * D, i64::from(d2) * D);
    // AAPL is id 7 on d1 and id 8 on d2; MSFT is id 9 on d1 and re-uses id 7 on d2.
    let map = vec![
        mapping("AAPL", &[(d1, d2, 7), (d2, d2 + 1, 8)]),
        mapping("MSFT", &[(d1, d2, 9), (d2, d2 + 1, 7)]),
    ];
    let f1_recs = vec![
        n(7, u1 + 15 * H, b'A', b'B', 1, 1),
        n(9, u1 + 15 * H + S, b'A', b'A', 2, 2),
    ];
    let f2_recs = vec![
        // After-hours of the d1 Eastern session, but on UTC date d2 (ids of d2).
        n(8, u2 + 30 * 60 * S, b'A', b'B', 3, 3),
        n(7, u2 + 31 * 60 * S, b'C', b'A', 2, 4),
        // The d2 session.
        n(8, u2 + 15 * H, b'A', b'B', 1, 1),
        n(7, u2 + 15 * H + S, b'A', b'A', 2, 2),
    ];
    let (f1, f2, fd) = (dir.join("d1.dbn"), dir.join("d2.dbn"), dir.join("defs.dbn"));
    write_mbo(&f1, &metadata("XNAS.ITCH", Schema::Mbo, d1, d2, map.clone()), &f1_recs);
    write_mbo(
        &f2,
        &metadata("XNAS.ITCH", Schema::Mbo, d2, d2 + 1, map.clone()),
        &f2_recs,
    );
    let defs = vec![
        def(7, "AAPL", 10_000_000, u1),
        def(9, "MSFT", 50_000_000, u1),
        def(8, "AAPL", 10_000_000, u2),
        def(7, "MSFT", 50_000_000, u2),
    ];
    write_dbn(
        &fd,
        &metadata("XNAS.ITCH", Schema::Definition, d1, d2 + 1, vec![]),
        &defs,
    );
    let (out, s) = run(&dir, "ds", &[&f1, &f2, &fd], json!({"venue": "nasdaq"}));
    let re = |e: &MboEvent, iid: u32| MboEvent {
        instrument_id: iid,
        ..*e
    };
    assert_eq!(
        partitions(&out),
        vec![
            (d1, 7, vec![f1_recs[0], re(&f2_recs[0], 7)]),
            (d1, 9, vec![f1_recs[1], re(&f2_recs[1], 9)]),
            (d2, 7, vec![f2_recs[3]]),
            (d2, 8, vec![f2_recs[2]]),
        ]
    );
    let ds = Dataset::open(&out).unwrap();
    let row = |d: i32, i: u32| {
        let r = ds.instrument(i, d).unwrap();
        (r.symbol.clone(), r.tick_size)
    };
    assert_eq!(row(d1, 7), ("AAPL".into(), 10_000_000));
    assert_eq!(row(d1, 9), ("MSFT".into(), 50_000_000));
    assert_eq!(row(d2, 7), ("MSFT".into(), 50_000_000));
    assert_eq!(row(d2, 8), ("AAPL".into(), 10_000_000));
    assert_eq!(
        s.data_quality.source_specific.get("dbn_instrument_id_remapped"),
        Some(&2)
    );
    assert_eq!(
        (s.data_quality.sequence_gaps, s.data_quality.sequence_regressions),
        (0, 0)
    );
    // The symbol filter follows the dated symbology, not the id.
    let (out2, _) = run(
        &dir,
        "aapl",
        &[&f1, &f2, &fd],
        json!({"venue": "nasdaq", "symbols": ["aapl"]}),
    );
    let got: Vec<(i32, u32, usize)> = partitions(&out2).into_iter().map(|(d, i, v)| (d, i, v.len())).collect();
    assert_eq!(got, vec![(d1, 7, 2), (d2, 8, 1)]);
    // Re-ingest as parquet: the filter reads the instrument rows per (session date, id) as well.
    let out3 = dir.join("re");
    ingest(&spec(
        json!({"format": "parquet", "venue": "nasdaq", "out": out3.to_str().unwrap(),
        "inputs": [{"path": out.to_str().unwrap()}], "symbols": ["AAPL"]}),
    ))
    .unwrap();
    let got: Vec<(i32, u32, usize)> = partitions(&out3).into_iter().map(|(d, i, v)| (d, i, v.len())).collect();
    assert_eq!(got, vec![(d1, 7, 2), (d2, 8, 1)]);
    // A symbol that only exists on d2 and re-uses the d1 id of another symbol cannot share that partition.
    let mut map3 = map;
    map3.push(mapping("NEWCO", &[(d2, d2 + 1, 9)]));
    let f3 = dir.join("d2b.dbn");
    write_mbo(
        &f3,
        &metadata("XNAS.ITCH", Schema::Mbo, d2, d2 + 1, map3),
        &[n(9, u2 + 40 * 60 * S, b'A', b'B', 5, 5)],
    );
    let e = ingest(&spec(
        json!({"format": "dbn", "venue": "nasdaq", "out": dir.join("bad").to_str().unwrap(),
        "inputs": [{"path": f1.to_str().unwrap()}, {"path": f3.to_str().unwrap()}]}),
    ))
    .unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002");
    assert!(e.to_string().contains("MSFT") && e.to_string().contains("NEWCO"), "{e}");
    let _ = std::fs::remove_dir_all(&dir);
}

// ---- (d) vintage --------------------------------------------------------------------------------------------------

#[test]
fn d_vintage_fields_and_input_hashes() {
    let dir = scratch("vintage");
    let d1: i32 = 20_467;
    let f = dir.join("x.dbn");
    let md = Metadata::builder()
        .dataset("XNAS.ITCH")
        .schema(Some(Schema::Mbo))
        .start((i64::from(d1) * D) as u64)
        .end(NonZeroU64::new((i64::from(d1 + 1) * D) as u64))
        .stype_in(Some(SType::RawSymbol))
        .stype_out(SType::InstrumentId)
        .symbols(vec!["AAPL".into(), "ZZZZ".into()])
        .not_found(vec!["ZZZZ".into()])
        .mappings(vec![mapping("AAPL", &[(d1, d1 + 1, 13)])])
        .build();
    write_mbo(&f, &md, &[n(13, i64::from(d1) * D + 15 * H, b'A', b'B', 1, 1)]);
    let v = qt_ingest::dbn::vintage(&f).unwrap();
    assert_eq!(
        v,
        qt_ingest::dbn::DbnVintage {
            dbn_version: qt_ingest::dbn::SUPPORTED_DBN_VERSION,
            dataset: "XNAS.ITCH".into(),
            schema: Some("mbo".into()),
            start_ns: (i64::from(d1) * D) as u64,
            end_ns: Some((i64::from(d1 + 1) * D) as u64),
            stype_in: Some("raw_symbol".into()),
            stype_out: "instrument_id".into(),
            limit: None,
            symbols: vec!["AAPL".into(), "ZZZZ".into()],
            partial: vec![],
            not_found: vec!["ZZZZ".into()],
        }
    );
    let (_, s) = run(&dir, "ds", &[&f], json!({"venue": "nasdaq"}));
    let bytes = std::fs::read(&f).unwrap();
    assert_eq!(s.manifest.inputs.len(), 1);
    assert_eq!(s.manifest.inputs[0].bytes, bytes.len() as u64);
    assert_eq!(s.manifest.inputs[0].sha256, qt_schema::io::sha256_bytes(&bytes));
    let _ = std::fs::remove_dir_all(&dir);
}

// ---- (e) unsupported DBN versions ---------------------------------------------------------------------------------

fn zstd(bytes: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut w = ::dbn::encode::DynWriter::new(&mut out, ::dbn::Compression::Zstd).unwrap();
    w.write_all(bytes).unwrap();
    w.finish().unwrap();
    drop(w);
    out
}

#[test]
fn e_unsupported_dbn_version_header_is_a_clear_input_error() {
    let dir = scratch("v4");
    let d1: i32 = 20_467;
    let f = dir.join("v3.dbn");
    write_mbo(
        &f,
        &metadata("XNAS.ITCH", Schema::Mbo, d1, d1 + 1, vec![]),
        &[n(13, i64::from(d1) * D + 15 * H, b'A', b'B', 1, 1)],
    );
    let v3 = std::fs::read(&f).unwrap();
    assert_eq!((&v3[..3], v3[3]), (&b"DBN"[..], qt_ingest::dbn::SUPPORTED_DBN_VERSION));
    let sup = qt_ingest::dbn::SUPPORTED_DBN_VERSION;
    for version in [sup + 1, 9, 255, 0] {
        let mut b = v3.clone();
        b[3] = version;
        for (name, bytes) in [("plain.dbn", b.clone()), ("comp.dbn.zst", zstd(&b))] {
            let p = dir.join(format!("v{version}-{name}"));
            std::fs::write(&p, bytes).unwrap();
            let e = qt_ingest::dbn::DbnReader::open(&p)
                .err()
                .expect("unsupported version accepted");
            assert_eq!(e.code(), "QT-INPUT-002", "{e}");
            let m = e.to_string();
            assert!(
                m.contains(&format!("unsupported DBN version {version}")) && m.contains(&format!("1 to {sup}")),
                "{m}"
            );
            let e = ingest(&spec(json!({"format": "dbn", "venue": "nasdaq",
                "out": dir.join(format!("ds-{version}-{name}")).to_str().unwrap(),
                "inputs": [{"path": p.to_str().unwrap()}]})))
            .unwrap_err();
            assert_eq!(e.code(), "QT-INPUT-002");
        }
    }
    // A bare v4 prelude (no metadata at all) is the same error, not a panic.
    let p = dir.join("bare.dbn");
    std::fs::write(&p, b"DBN\x04\x10\x00\x00\x00").unwrap();
    assert_eq!(
        qt_ingest::dbn::DbnReader::open(&p).err().unwrap().code(),
        "QT-INPUT-002"
    );
    let _ = std::fs::remove_dir_all(&dir);
}
