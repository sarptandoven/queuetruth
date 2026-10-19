//! etcsv and DBN ingest: et's exact CSV header layouts (mbo / trades / instruments) and DBN files encoded in-test
//! with the `dbn` crate (plain and zstd, MBO + definition + trades schemas) ingest to the expected records; session
//! routing by tz; symbol filters; malformed inputs are errors, never panics.

use std::ffi::c_char;
use std::path::{Path, PathBuf};

use ::dbn::encode::EncodeRecord;
use ::dbn::encode::dbn::Encoder;
use ::dbn::{
    FlagSet, InstrumentDefMsg, MappingInterval, MboMsg, Metadata, RecordHeader, SType, Schema, SymbolMapping, TradeMsg,
    rtype,
};
use proptest::prelude::*;
use qt_ingest::{Dataset, ingest};
use qt_schema::rows::TradeKind;
use qt_schema::specs::IngestSpec;
use qt_types::{MboEvent, Side, UNDEF_PRICE, Venue, flags};
use serde_json::json;

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-formats-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn spec(v: serde_json::Value) -> IngestSpec {
    serde_json::from_value(v).unwrap()
}

fn read_all(root: &Path) -> Vec<(u32, i32, Vec<MboEvent>)> {
    let ds = Dataset::open(root).unwrap();
    ds.partitions()
        .iter()
        .map(|p| {
            (
                p.instrument_id,
                p.session_date,
                p.mbo().unwrap().map(Result::unwrap).collect(),
            )
        })
        .collect()
}

const MID: i64 = 1_577_682_000_000_000_000; // 2019-12-30 00:00 US/Eastern (SPEC RD-1)

// ---- etcsv --------------------------------------------------------------------------------------------------------

const ET_MBO_HEADER: &str = "ts_event,ts_recv,instrument_id,action,side,price,size,order_id,flags,sequence,channel_id";
const ET_TRADES_HEADER: &str = "ts_event,instrument_id,price,size,aggressor_side,sequence";
const ET_ITCH_INSTRUMENTS: &str = "instrument_id,raw_symbol,market_category,financial_status,round_lot_size,round_lots_only,issue_classification,issue_sub_type,authenticity,short_sale_threshold,ipo_flag,luld_tier,etp_flag,etp_leverage_factor,inverse_indicator";

fn et_csv_day(dir: &Path) -> (PathBuf, PathBuf, PathBuf, Vec<MboEvent>) {
    let t = |s: i64| MID + 34_200_000_000_000 + s;
    let rows: Vec<MboEvent> = vec![
        MboEvent {
            ts_event: t(1),
            ts_recv: t(1),
            instrument_id: 13,
            action: b'A',
            side: b'B',
            price: 185_000_000_000,
            size: 100,
            order_id: 1,
            flags: flags::LAST,
            sequence: 1,
            ..MboEvent::default()
        },
        MboEvent {
            ts_event: t(2),
            ts_recv: t(2),
            instrument_id: 5291,
            action: b'A',
            side: b'A',
            price: 400_000_000_000,
            size: 5_000_000_000,
            order_id: 2,
            flags: flags::LAST,
            sequence: 2,
            ..MboEvent::default()
        },
        MboEvent {
            ts_event: t(3),
            ts_recv: t(3),
            instrument_id: 13,
            action: b'T',
            side: b'A',
            price: 185_000_000_000,
            size: 10,
            order_id: 0,
            flags: 0,
            sequence: 3,
            ..MboEvent::default()
        },
        MboEvent {
            ts_event: t(3),
            ts_recv: t(3),
            instrument_id: 13,
            action: b'F',
            side: b'B',
            price: 185_000_000_000,
            size: 10,
            order_id: 1,
            flags: 0,
            sequence: 3,
            ..MboEvent::default()
        },
        MboEvent {
            ts_event: t(3),
            ts_recv: t(3),
            instrument_id: 13,
            action: b'C',
            side: b'B',
            price: 185_000_000_000,
            size: 10,
            order_id: 1,
            flags: flags::LAST,
            sequence: 3,
            ..MboEvent::default()
        },
        MboEvent {
            ts_event: t(4),
            ts_recv: t(4),
            instrument_id: 5291,
            action: b'T',
            side: b'N',
            price: 400_010_000_000,
            size: 70,
            order_id: 0,
            flags: flags::LAST,
            sequence: 4,
            ..MboEvent::default()
        },
        MboEvent {
            ts_event: t(5),
            ts_recv: t(5),
            instrument_id: 13,
            action: b'R',
            side: b'N',
            price: UNDEF_PRICE,
            size: 0,
            order_id: 0,
            flags: flags::LAST,
            sequence: 5,
            ..MboEvent::default()
        },
    ];
    let mut mbo = format!("{ET_MBO_HEADER}\n");
    for e in &rows {
        mbo.push_str(&format!(
            "{},{},{},{},{},{},{},{},{},{},0\n",
            e.ts_event,
            e.ts_recv,
            e.instrument_id,
            e.action as char,
            e.side as char,
            e.price,
            e.size,
            e.order_id,
            e.flags,
            e.sequence
        ));
    }
    let trades = format!(
        "{ET_TRADES_HEADER}\n{},13,185000000000,10,A,3\n{},5291,400010000000,70,N,4\n",
        t(3),
        t(4)
    );
    let inst =
        format!("{ET_ITCH_INSTRUMENTS}\n13,AAPL,Q,N,100,N,C,Z,P,N,N,1,N,0,N\n5291,MSFT,Q,N,100,N,C,Z,P,N,N,1,N,0,N\n");
    let (m, tr, i) = (dir.join("mbo.csv"), dir.join("trades.csv"), dir.join("instruments.csv"));
    std::fs::write(&m, mbo).unwrap();
    std::fs::write(&tr, trades).unwrap();
    std::fs::write(&i, inst).unwrap();
    (m, tr, i, rows)
}

#[test]
fn etcsv_ingest_in_et_layouts() {
    let dir = scratch("etcsv");
    let (m, tr, i, rows) = et_csv_day(&dir);
    let out = dir.join("ds");
    let s = ingest(&spec(json!({
        "format": "etcsv", "venue": "nasdaq", "out": out.to_str().unwrap(),
        "inputs": [{"path": m.to_str().unwrap()}, {"path": tr.to_str().unwrap()}, {"path": i.to_str().unwrap()}],
        "midnight_utc_ns": MID, "tick_size": "0.01", "lot_size": "1"
    })))
    .unwrap();
    assert_eq!(s.manifest.inputs.len(), 3);
    assert_eq!(s.data_quality.records, 7);
    assert_eq!(s.data_quality.hidden_prints, 1);
    let got = read_all(&out);
    let date = qt_ingest::time::parse_date("2019-12-30").unwrap();
    let want13: Vec<MboEvent> = rows.iter().filter(|e| e.instrument_id == 13).copied().collect();
    let want5291: Vec<MboEvent> = rows.iter().filter(|e| e.instrument_id == 5291).copied().collect();
    assert_eq!(got, vec![(13, date, want13), (5291, date, want5291)]);
    let ds = Dataset::open(&out).unwrap();
    let a = ds.instrument(13, date).unwrap();
    assert_eq!(
        (
            a.symbol.as_str(),
            a.tick_size,
            a.lot_size,
            a.midnight_utc_ns,
            a.open_s,
            a.close_s
        ),
        ("AAPL", 10_000_000, 1, MID, 34_200, 57_600)
    );
    assert_eq!(a.session_tz, "America/New_York");
    let t = ds.partitions()[1].source_trades().unwrap().unwrap();
    assert_eq!(t.len(), 1);
    assert_eq!(
        (t[0].kind, t[0].aggressor_side, t[0].size, t[0].sequence),
        (TradeKind::Hidden, Side::None, 70, 4)
    );

    // Symbol filter, tz routing without midnight (Eastern dates), and price rescaling to -4.
    let out2 = dir.join("ds2");
    ingest(&spec(json!({
        "format": "etcsv", "venue": "nasdaq", "out": out2.to_str().unwrap(),
        "inputs": [{"path": m.to_str().unwrap()}], "instruments": {"path": i.to_str().unwrap()},
        "symbols": ["msft"], "price_exp": -4
    })))
    .unwrap();
    let got = read_all(&out2);
    assert_eq!(got.len(), 1);
    assert_eq!((got[0].0, got[0].1), (5291, date));
    assert_eq!(got[0].2[0].price, 4_000_000);
    assert_eq!(
        Dataset::open(&out2).unwrap().instrument(5291, date).unwrap().tick_size,
        100
    );
    // A price not exact at the target exponent is QT-INPUT-004.
    let e = ingest(&spec(json!({
        "format": "etcsv", "venue": "nasdaq", "out": dir.join("ds3").to_str().unwrap(),
        "inputs": [{"path": m.to_str().unwrap()}], "price_exp": 0
    })))
    .unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-004");
    // Unknown options, bad venue, missing inputs.
    let bad = |v: serde_json::Value| ingest(&spec(v)).unwrap_err().code();
    assert_eq!(
        bad(
            json!({"format": "etcsv", "venue": "nasdaq", "out": dir.join("x1").to_str().unwrap(), "inputs": [{"path": m.to_str().unwrap()}], "options": {"nope": 1}})
        ),
        "QT-CONFIG-001"
    );
    assert_eq!(
        bad(
            json!({"format": "etcsv", "venue": "lse", "out": dir.join("x2").to_str().unwrap(), "inputs": [{"path": m.to_str().unwrap()}]})
        ),
        "QT-CONFIG-001"
    );
    assert_eq!(
        bad(
            json!({"format": "etcsv", "venue": "nasdaq", "out": dir.join("x3").to_str().unwrap(), "inputs": [{"path": dir.join("missing.csv").to_str().unwrap()}]})
        ),
        "QT-INPUT-001"
    );
    assert_eq!(
        bad(
            json!({"format": "bitstamp", "venue": "generic", "out": dir.join("x4").to_str().unwrap(), "inputs": [{"path": m.to_str().unwrap()}]})
        ),
        "QT-CONFIG-001"
    );
    // A bad row names file and line.
    std::fs::write(
        &m,
        format!("{ET_MBO_HEADER}\n1,1,1,A,B,100,1,1,128,1,0\n1,1,1,A,B,1x0,1,1,128,1,0\n"),
    )
    .unwrap();
    let e = ingest(&spec(json!({"format": "etcsv", "venue": "generic", "out": dir.join("x5").to_str().unwrap(), "inputs": [{"path": m.to_str().unwrap()}]}))).unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002");
    assert!(e.to_string().contains("mbo.csv:3: bad price"), "{e}");
    std::fs::remove_dir_all(dir).ok();
}

#[test]
fn parquet_ingest_re_ingests_a_dataset() {
    let dir = scratch("pq");
    let (m, tr, i, _) = et_csv_day(&dir);
    let a = dir.join("a");
    ingest(&spec(
        json!({"format": "etcsv", "venue": "nasdaq", "out": a.to_str().unwrap(), "midnight_utc_ns": MID,
        "inputs": [{"path": m.to_str().unwrap()}, {"path": tr.to_str().unwrap()}, {"path": i.to_str().unwrap()}]}),
    ))
    .unwrap();
    let b = dir.join("b");
    let s = ingest(&spec(json!({"format": "parquet", "venue": "nasdaq", "out": b.to_str().unwrap(), "inputs": [{"path": a.to_str().unwrap()}]}))).unwrap();
    assert_eq!(read_all(&a), read_all(&b));
    let da = Dataset::open(&a).unwrap();
    let db = Dataset::open(&b).unwrap();
    assert_eq!(
        da.manifest
            .partitions
            .iter()
            .map(|p| &p.files[0].sha256)
            .collect::<Vec<_>>(),
        db.manifest
            .partitions
            .iter()
            .map(|p| &p.files[0].sha256)
            .collect::<Vec<_>>(),
        "identical partition bytes"
    );
    assert_eq!(
        da.instruments
            .iter()
            .map(|i| (&i.symbol, i.tick_size))
            .collect::<Vec<_>>(),
        db.instruments
            .iter()
            .map(|i| (&i.symbol, i.tick_size))
            .collect::<Vec<_>>()
    );
    assert_eq!(s.manifest.source_format, "parquet");
    assert_ne!(
        s.manifest.dataset_id, da.manifest.dataset_id,
        "source_format is part of the id"
    );
    assert_eq!(
        db.partitions()[0].source_trades().unwrap(),
        da.partitions()[0].source_trades().unwrap()
    );
    // A single partition file plus its instruments table.
    let c = dir.join("c");
    let f = &da.partitions()[0].files[0];
    ingest(&spec(json!({"format": "parquet", "venue": "nasdaq", "out": c.to_str().unwrap(),
        "inputs": [{"path": f.to_str().unwrap()}], "instruments": {"path": a.join("instruments.parquet").to_str().unwrap()}}))).unwrap();
    assert_eq!(read_all(&c)[0], read_all(&a)[0]);
    std::fs::remove_dir_all(dir).ok();
}

// ---- DBN --------------------------------------------------------------------------------------------------------

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

fn metadata(schema: Schema, start: u64, mappings: Vec<SymbolMapping>) -> Metadata {
    Metadata::builder()
        .dataset("GLBX.MDP3")
        .schema(Some(schema))
        .start(start)
        .stype_in(Some(SType::RawSymbol))
        .stype_out(SType::InstrumentId)
        .mappings(mappings)
        .build()
}

fn write_dbn<R: ::dbn::encode::DbnEncodable>(path: &Path, md: &Metadata, recs: &[R], zstd: bool) {
    let f = std::fs::File::create(path).unwrap();
    if zstd {
        let mut enc = Encoder::with_zstd(f, md).unwrap();
        for r in recs {
            enc.encode_record(r).unwrap();
        }
    } else {
        let mut enc = Encoder::new(f, md).unwrap();
        for r in recs {
            enc.encode_record(r).unwrap();
        }
    }
}

const D: i64 = 86_400_000_000_000;

fn cme_events(day: i64, n: u64, iid: u32) -> Vec<MboEvent> {
    let mut v = vec![MboEvent {
        ts_event: day * D + 1,
        ts_recv: day * D + 2,
        instrument_id: iid,
        action: b'R',
        side: b'N',
        price: UNDEF_PRICE,
        flags: flags::SNAPSHOT,
        sequence: 1,
        publisher_id: 1,
        ..MboEvent::default()
    }];
    for i in 0..n {
        v.push(MboEvent {
            ts_event: day * D + 1_000 + i as i64,
            ts_recv: day * D + 1_005 + i as i64,
            instrument_id: iid,
            action: b"AMCTF"[(i % 5) as usize],
            side: if i % 2 == 0 { b'B' } else { b'A' },
            price: 4_500_250_000_000 + (i % 7) as i64 * 250_000_000,
            size: 1 + i % 9,
            order_id: 1 + i / 5,
            flags: if i % 3 == 2 { flags::LAST } else { 0 },
            sequence: 2 + i,
            channel_id: 3,
            publisher_id: 1,
        });
    }
    v
}

#[test]
fn dbn_ingest_plain_and_zstd_with_definitions_and_trades() {
    let dir = scratch("dbn");
    let day1: Vec<MboEvent> = cme_events(19_000, 500, 42);
    let day2: Vec<MboEvent> = cme_events(19_001, 300, 42);
    // `time::Date` values without naming the `time` crate: dates of metadata start times.
    let date_of = |ns: i64| metadata(Schema::Mbo, ns as u64, vec![]).start().date();
    let map = vec![SymbolMapping {
        raw_symbol: "ESH1".into(),
        intervals: vec![MappingInterval {
            start_date: date_of(19_000 * D),
            end_date: date_of(19_002 * D),
            symbol: "42".into(),
        }],
    }];
    let (f1, f2) = (dir.join("day1.dbn"), dir.join("day2.dbn.zst"));
    // Given out of order: chronological ordering by metadata start.
    write_dbn(
        &f2,
        &metadata(Schema::Mbo, (19_001 * D) as u64, map.clone()),
        &day2.iter().map(mbo_msg).collect::<Vec<_>>(),
        true,
    );
    write_dbn(
        &f1,
        &metadata(Schema::Mbo, (19_000 * D) as u64, map),
        &day1.iter().map(mbo_msg).collect::<Vec<_>>(),
        false,
    );
    let def = InstrumentDefMsg {
        hd: RecordHeader::new::<InstrumentDefMsg>(rtype::INSTRUMENT_DEF, 1, 42, 0),
        min_price_increment: 250_000_000,
        ..Default::default()
    };
    let defs = dir.join("defs.dbn");
    write_dbn(&defs, &metadata(Schema::Definition, 0, vec![]), &[def], false);
    let trade = TradeMsg {
        hd: RecordHeader::new::<TradeMsg>(rtype::MBP_0, 1, 42, (19_000 * D + 1_003) as u64),
        price: 4_500_250_000_000,
        size: 4,
        action: b'T' as c_char,
        side: b'A' as c_char,
        flags: FlagSet::new(0),
        depth: 0,
        ts_recv: (19_000 * D + 1_004) as u64,
        ts_in_delta: 0,
        sequence: 5,
    };
    let trades = dir.join("trades.dbn");
    write_dbn(&trades, &metadata(Schema::Trades, 0, vec![]), &[trade], false);

    assert_eq!(qt_ingest::dbn::read_mbo(&f1, -9).unwrap(), day1);
    assert_eq!(qt_ingest::dbn::read_mbo(&f2, -9).unwrap(), day2);
    let out = dir.join("ds");
    let s = ingest(&spec(json!({"format": "dbn", "venue": "cme", "out": out.to_str().unwrap(),
        "inputs": [{"path": f2.to_str().unwrap()}, {"path": defs.to_str().unwrap()}, {"path": trades.to_str().unwrap()}, {"path": f1.to_str().unwrap()}]})))
    .unwrap();
    assert_eq!(s.manifest.venue, Venue::Cme);
    assert_eq!(s.data_quality.records, 802);
    assert_eq!(s.data_quality.snapshot_records, 2);
    let got = read_all(&out);
    assert_eq!(got, vec![(42, 19_000, day1), (42, 19_001, day2)]);
    let ds = Dataset::open(&out).unwrap();
    let i = ds.instrument(42, 19_000).unwrap();
    // Definition supplies the tick; the symbology supplies the symbol; CME default tz is UTC.
    assert_eq!(
        (i.symbol.as_str(), i.tick_size, i.midnight_utc_ns, i.session_tz.as_str()),
        ("ESH1", 250_000_000, 19_000 * D, "UTC")
    );
    let t = ds.partitions()[0].source_trades().unwrap().unwrap();
    assert_eq!(
        (t.len(), t[0].size, t[0].aggressor_side, t[0].kind),
        (1, 4, Side::Ask, TradeKind::Displayed)
    );
    assert!(ds.partitions()[1].source_trades().unwrap().is_none());
    // Price rescale to -2 (ticks of 0.25 are exact), symbol filter that excludes everything.
    let out2 = dir.join("ds2");
    ingest(&spec(
        json!({"format": "dbn", "venue": "cme", "out": out2.to_str().unwrap(), "price_exp": -2,
        "inputs": [{"path": f1.to_str().unwrap()}]}),
    ))
    .unwrap();
    assert_eq!(read_all(&out2)[0].2[1].price, 450_025);
    let out3 = dir.join("ds3");
    let s3 = ingest(&spec(
        json!({"format": "dbn", "venue": "cme", "out": out3.to_str().unwrap(), "symbols": ["NQH1"],
        "inputs": [{"path": f1.to_str().unwrap()}]}),
    ))
    .unwrap();
    assert!(s3.manifest.partitions.is_empty());
    std::fs::remove_dir_all(dir).ok();
}

#[test]
fn dbn_malformed_files_are_errors() {
    let dir = scratch("dbnbad");
    let p = dir.join("x.dbn");
    std::fs::write(&p, b"not a dbn file at all").unwrap();
    assert_eq!(
        qt_ingest::dbn::DbnReader::open(&p).err().unwrap().code(),
        "QT-INPUT-002"
    );
    std::fs::write(&p, b"").unwrap();
    assert!(qt_ingest::dbn::DbnReader::open(&p).is_err());
    std::fs::remove_dir_all(dir).ok();
}

fn valid_dbn_bytes(zstd: bool) -> Vec<u8> {
    let dir = scratch(if zstd { "fz" } else { "fp" });
    let p = dir.join("f.dbn");
    write_dbn(
        &p,
        &metadata(Schema::Mbo, 0, vec![]),
        &cme_events(19_000, 20, 1).iter().map(mbo_msg).collect::<Vec<_>>(),
        zstd,
    );
    let b = std::fs::read(&p).unwrap();
    std::fs::remove_dir_all(dir).ok();
    b
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 300, ..ProptestConfig::default() })]

    /// Mutated and truncated DBN files (plain and zstd) never panic the reader.
    #[test]
    fn dbn_reader_never_panics_on_mutations(zstd in any::<bool>(), cut in 0usize..2000, flips in prop::collection::vec((0usize..2000, any::<u8>()), 0..8)) {
        let mut b = valid_dbn_bytes(zstd);
        for (i, x) in flips {
            let n = b.len();
            b[i % n] ^= x;
        }
        b.truncate(cut.min(b.len()));
        let dir = scratch(&format!("fuzz-{}", cut));
        let p = dir.join("m.dbn");
        std::fs::write(&p, &b).unwrap();
        if let Ok(mut r) = qt_ingest::dbn::DbnReader::open(&p) {
            for _ in 0..100 {
                match r.next_mbo(-9) {
                    Ok(Some(_)) => {}
                    Ok(None) | Err(_) => break,
                }
            }
        }
        std::fs::remove_dir_all(dir).ok();
    }

    /// Arbitrary etcsv content never panics the readers.
    #[test]
    fn etcsv_readers_never_panic(body in "[0-9A-Z,\\-\\r\\n ]{0,300}", header_cut in 0usize..12) {
        let dir = scratch(&format!("csvfuzz-{header_cut}-{}", body.len()));
        let cols: Vec<&str> = ET_MBO_HEADER.split(',').collect();
        let header = cols[..cols.len() - header_cut.min(cols.len())].join(",");
        let p = dir.join("mbo.csv");
        std::fs::write(&p, format!("{header}\n{body}")).unwrap();
        if let Ok(mut r) = qt_ingest::etcsv::MboCsvReader::open(&p) {
            while let Ok(Some(_)) = r.next() {}
        }
        let _ = qt_ingest::etcsv::read_trades_csv(&p, |_| Ok(()));
        let _ = qt_ingest::etcsv::read_instruments_csv(&p);
        let _ = qt_ingest::etcsv::detect(&p);
        std::fs::remove_dir_all(dir).ok();
    }
}

/// etcsv -> dataset Parquet throughput on a generated 3M-row `mbo.csv`. `cargo test --release -p queuetruth-ingest
/// --test formats -- --ignored bench_etcsv --nocapture`.
#[test]
#[ignore]
fn bench_etcsv_ingest() {
    use std::io::Write as _;
    let dir = scratch("bench-etcsv");
    let p = dir.join("mbo.csv");
    let mut w = std::io::BufWriter::new(std::fs::File::create(&p).unwrap());
    writeln!(w, "{ET_MBO_HEADER}").unwrap();
    const N: u64 = 3_000_000;
    for i in 0..N {
        let ts = MID + 34_200_000_000_000 + i as i64 * 1_000;
        let a = b"ACTF"[(i % 4) as usize] as char;
        writeln!(
            w,
            "{ts},{ts},{},{a},B,{},100,{},{},{},0",
            13 + (i % 4) * 1000,
            185_000_000_000 + (i % 50) * 10_000_000,
            i / 2,
            if i % 2 == 1 { 128 } else { 0 },
            i / 2
        )
        .unwrap();
    }
    drop(w);
    let t0 = std::time::Instant::now();
    let s = ingest(&spec(
        json!({"format": "etcsv", "venue": "nasdaq", "out": dir.join("ds").to_str().unwrap(),
        "inputs": [{"path": p.to_str().unwrap()}], "midnight_utc_ns": MID}),
    ))
    .unwrap();
    let secs = t0.elapsed().as_secs_f64();
    println!(
        "etcsv -> dataset parquet: {} records in {secs:.2} s = {:.2}M rec/s",
        s.data_quality.records,
        N as f64 / secs / 1e6
    );
    std::fs::remove_dir_all(dir).ok();
}
