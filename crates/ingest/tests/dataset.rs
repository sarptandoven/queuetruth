//! Dataset write / open / read round trip (SPEC 3.3, 3.10): routing by instrument and local session, spill-free and
//! spill-heavy writes give byte-identical files (sha256 stable), the manifest `dataset_id` rule, partition order,
//! `partition_for`, source trades, L2 datasets, and the open-time checks.

use std::path::{Path, PathBuf};

use qt_ingest::{Dataset, DatasetKind, DatasetWriter};
use qt_schema::io::sha256_file;
use qt_schema::json::{DataQuality, Verdict, dataset_id};
use qt_schema::rows::{L2Kind, L2Row, TradeKind, TradeRow, l2flags};
use qt_types::{Instrument, MboEvent, Side, Venue, flags};

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-dataset-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    d
}

const DAY: i64 = 86_400_000_000_000;
const D0: i32 = 19_000;

fn inst(iid: u32, date: i32) -> Instrument {
    Instrument {
        instrument_id: iid,
        session_date: date,
        symbol: format!("S{iid}"),
        venue: Venue::Cme,
        source_venue: "cme".into(),
        price_exp: -9,
        tick_size: 1,
        lot_size: 1,
        size_exp: 0,
        contract_multiplier: 1.0,
        currency: "USD".into(),
        session_tz: "UTC".into(),
        midnight_utc_ns: i64::from(date) * DAY,
        open_s: 0,
        close_s: 86_400,
        source: "test".into(),
    }
}

/// 2 instruments x 2 UTC days, interleaved in feed order.
fn events(n: u64) -> Vec<MboEvent> {
    (0..n)
        .map(|i| {
            let day = i64::from(i >= n / 2);
            MboEvent {
                ts_event: (i64::from(D0) + day) * DAY + i as i64 * 1_000,
                ts_recv: (i64::from(D0) + day) * DAY + i as i64 * 1_000 + 5,
                instrument_id: 7 + (i % 2) as u32,
                action: b'A',
                side: if i % 3 == 0 { b'B' } else { b'A' },
                price: 100 + (i % 17) as i64,
                size: 1 + i % 5,
                order_id: i + 1,
                flags: flags::LAST,
                sequence: i,
                channel_id: 1,
                publisher_id: 2,
            }
        })
        .collect()
}

fn write(root: &Path, evs: &[MboEvent], memory_rows: usize) -> qt_schema::json::IngestSummary {
    let mut w = DatasetWriter::create(root, DatasetKind::Mbo, Venue::Cme, "test").unwrap();
    w.set_instruments(vec![inst(7, D0), inst(8, D0), inst(7, D0 + 1), inst(8, D0 + 1)]);
    w.set_memory_rows(memory_rows);
    for e in evs {
        w.push_mbo(e).unwrap();
        if e.sequence % 10 < 2 {
            w.push_source_trade(&TradeRow {
                ts_event: e.ts_event,
                instrument_id: e.instrument_id,
                sequence: e.sequence,
                price: e.price,
                size: e.size,
                aggressor_side: Side::Bid,
                kind: TradeKind::Displayed,
                maker_order_id: Some(e.order_id),
            })
            .unwrap();
        }
    }
    w.finish(DataQuality::default()).unwrap()
}

/// More spilling partitions than are streamed (the rest spill to scratch files), partitions spanning several
/// `CHUNK_ROWS` batches and more than one row group: every spill setting gives the spill-free bytes.
#[test]
fn streamed_and_scratch_spills_are_byte_identical() {
    let n_inst = 20u32;
    // instrument 1 takes half the feed (1.2M rows: two row groups), the rest round-robin
    let evs: Vec<MboEvent> = (0..2_400_000u64)
        .map(|i| {
            let iid = if i % 2 == 0 {
                1
            } else {
                2 + (i / 2 % u64::from(n_inst - 1)) as u32
            };
            MboEvent {
                ts_event: i64::from(D0) * DAY + i as i64 * 1_000,
                ts_recv: i64::from(D0) * DAY + i as i64 * 1_000 + 5,
                instrument_id: iid,
                action: b'A',
                side: if i % 3 == 0 { b'B' } else { b'A' },
                price: 100 + (i % 17) as i64,
                size: 1 + i % 5,
                order_id: i + 1,
                flags: flags::LAST,
                sequence: i,
                channel_id: 1,
                publisher_id: 2,
            }
        })
        .collect();
    let run = |name: &str, memory_rows: usize| {
        let root = scratch(name);
        let mut w = DatasetWriter::create(&root, DatasetKind::Mbo, Venue::Cme, "test").unwrap();
        w.set_instruments((1..=n_inst).map(|i| inst(i, D0)).collect());
        w.set_memory_rows(memory_rows);
        for e in &evs {
            w.push_mbo(e).unwrap();
            if e.sequence % 7 == 0 {
                w.push_source_trade(&TradeRow {
                    ts_event: e.ts_event,
                    instrument_id: e.instrument_id,
                    sequence: e.sequence,
                    price: e.price,
                    size: e.size,
                    aggressor_side: Side::Ask,
                    kind: TradeKind::Displayed,
                    maker_order_id: None,
                })
                .unwrap();
            }
        }
        let s = w.finish(DataQuality::default()).unwrap();
        assert!(!root.join(".spill").exists(), "scratch removed");
        for f in &s.files {
            assert_eq!(sha256_file(&root.join(&f.name)).unwrap(), f.sha256, "{}", f.name);
        }
        if memory_rows == 5_000 {
            // large partitions are decoded ahead on threads: same rows, in feed order
            let ds = Dataset::open(&root).unwrap();
            for p in ds.partitions() {
                let got: Vec<MboEvent> = p.mbo().unwrap().map(Result::unwrap).collect();
                let want: Vec<MboEvent> = evs
                    .iter()
                    .filter(|e| e.instrument_id == p.instrument_id)
                    .copied()
                    .collect();
                assert_eq!(got, want, "instrument {}", p.instrument_id);
            }
            // a damaged file of a large partition is an error, never a short partition
            let big = &ds.partitions()[0];
            assert!(big.rows >= 1_000_000);
            let bytes = std::fs::read(&big.files[0]).unwrap();
            std::fs::write(&big.files[0], &bytes[..bytes.len() / 2]).unwrap();
            assert!(big.mbo().unwrap().any(|r| r.is_err()));
        }
        std::fs::remove_dir_all(&root).unwrap();
        s
    };
    let base = run("mix-none", 100_000_000);
    for (name, rows) in [("mix-small", 5_000), ("mix-mid", 150_000), ("mix-large", 700_000)] {
        let s = run(name, rows);
        assert_eq!(base.files, s.files, "memory_rows {rows}");
        assert_eq!(base.manifest, s.manifest, "memory_rows {rows}");
    }
}

#[test]
fn dataset_write_open_read_round_trip_is_byte_stable() {
    let evs = events(200_000);
    let a = scratch("a");
    let b = scratch("b");
    let sa = write(&a, &evs, 10_000_000);
    let sb = write(&b, &evs, 997); // spills constantly
    assert!(!b.join(".spill").exists(), "scratch removed");
    assert_eq!(sa.files, sb.files, "file names, rows, bytes and sha256 identical");
    assert_eq!(sa.manifest, sb.manifest);
    for f in &sa.files {
        assert_eq!(sha256_file(&a.join(&f.name)).unwrap(), f.sha256, "{}", f.name);
    }
    // dataset_id rule: sorted partition-file sha256s, newline-joined, newline, source_format.
    let shas: Vec<String> = sa
        .manifest
        .partitions
        .iter()
        .flat_map(|p| p.files.iter().map(|f| f.sha256.clone()))
        .collect();
    assert_eq!(sa.manifest.dataset_id, dataset_id(&shas, "test"));
    assert_eq!(sa.data_quality.records, 200_000);
    assert_eq!(sa.data_quality.verdict, Verdict::Clean);

    let ds = Dataset::open(&a).unwrap();
    let keys: Vec<(i32, u32, u64)> = ds
        .partitions()
        .iter()
        .map(|p| (p.session_date, p.instrument_id, p.rows))
        .collect();
    assert_eq!(
        keys,
        vec![
            (D0, 7, 50_000),
            (D0, 8, 50_000),
            (D0 + 1, 7, 50_000),
            (D0 + 1, 8, 50_000)
        ]
    );
    let mut back = Vec::new();
    for p in ds.partitions() {
        let rows: Vec<MboEvent> = p.mbo().unwrap().map(Result::unwrap).collect();
        assert_eq!(rows.len() as u64, p.rows);
        assert!(rows.iter().all(|e| e.instrument_id == p.instrument_id));
        assert_eq!(p.ts_min, rows[0].ts_event);
        assert_eq!(p.ts_max, rows.last().unwrap().ts_event);
        assert!(p.files[0].ends_with(format!(
            "mbo/date={}/instrument_id={}/part-00000.parquet",
            qt_ingest::time::format_date(p.session_date),
            p.instrument_id
        )));
        let trades = p.source_trades().unwrap().unwrap();
        assert_eq!(trades.len(), rows.iter().filter(|e| e.sequence % 10 < 2).count());
        back.extend(rows);
    }
    let mut sorted = evs.clone();
    sorted.sort_by_key(|e| (e.ts_event.div_euclid(DAY), e.instrument_id, e.sequence));
    assert_eq!(
        back, sorted,
        "feed order within partitions, partitions in (date, instrument) order"
    );
    assert_eq!(ds.rules(), qt_types::rules(Venue::Cme));
    assert_eq!(ds.instrument(8, D0 + 1).unwrap().symbol, "S8");
    assert!(ds.instrument(9, D0).is_none());
    let p = ds.partition_for(8, i64::from(D0 + 1) * DAY + 3).unwrap();
    assert_eq!((p.instrument_id, p.session_date), (8, D0 + 1));
    assert!(ds.partition_for(8, i64::from(D0 + 2) * DAY).is_none());
    std::fs::remove_dir_all(a).ok();
    std::fs::remove_dir_all(b).ok();
}

#[test]
fn writer_and_open_reject_bad_input() {
    let root = scratch("bad");
    let mut w = DatasetWriter::create(&root, DatasetKind::Mbo, Venue::Cme, "test").unwrap();
    let e = MboEvent {
        ts_event: 5,
        instrument_id: 3,
        ..MboEvent::default()
    };
    assert_eq!(w.push_mbo(&e).unwrap_err().code(), "QT-INPUT-002", "no instrument row");
    w.set_instruments(vec![inst(3, 0), inst(3, 2)]);
    assert_eq!(
        w.push_mbo(&MboEvent { ts_event: DAY + 1, ..e }).unwrap_err().code(),
        "QT-INPUT-002",
        "no session"
    );
    assert_eq!(
        w.push_mbo(&MboEvent { ts_event: -1, ..e }).unwrap_err().code(),
        "QT-INPUT-002"
    );
    let l2 = L2Row {
        ts_event: 1,
        ts_recv: 1,
        instrument_id: 3,
        event_id: 1,
        sequence: 1,
        kind: L2Kind::Clear,
        side: Side::None,
        price: None,
        prev_qty: 0,
        new_qty: 0,
        prev_ct: None,
        new_ct: None,
        trade_qty: 0,
        aggressor: Side::None,
        level: 255,
        flags: l2flags::LAST_IN_BATCH,
    };
    assert_eq!(
        w.push_l2(&l2).unwrap_err().code(),
        "QT-ENGINE-001",
        "L2 row into an MBO dataset"
    );
    w.push_mbo(&e).unwrap();
    w.push_mbo(&MboEvent {
        ts_event: 2 * DAY + 9,
        ..e
    })
    .unwrap();
    let s = w
        .finish(DataQuality {
            maybe_bad_book_records: 1,
            ..Default::default()
        })
        .unwrap();
    assert_eq!(s.data_quality.verdict, Verdict::Unusable);
    assert_eq!(s.data_quality.records, 2);
    assert_eq!(s.manifest.partitions.len(), 2);
    // Not empty any more.
    assert_eq!(
        DatasetWriter::create(&root, DatasetKind::Mbo, Venue::Cme, "t")
            .err()
            .unwrap()
            .code(),
        "QT-CONFIG-001"
    );
    // A missing partition file and a corrupt manifest are input errors.
    let part = root.join(&s.manifest.partitions[0].files[0].path);
    std::fs::rename(&part, root.join("moved")).unwrap();
    assert_eq!(Dataset::open(&root).err().unwrap().code(), "QT-INPUT-001");
    std::fs::rename(root.join("moved"), &part).unwrap();
    Dataset::open(&root).unwrap();
    std::fs::write(root.join("manifest.json"), "{\"schema\": \"qt.nope\"}").unwrap();
    assert_eq!(Dataset::open(&root).err().unwrap().code(), "QT-INPUT-002");
    assert_eq!(Dataset::open(&root.join("nope")).err().unwrap().code(), "QT-INPUT-001");
    std::fs::remove_dir_all(root).ok();
}

#[test]
fn l2_dataset_round_trip() {
    let root = scratch("l2");
    let mut w = DatasetWriter::create(&root, DatasetKind::L2, Venue::Generic, "tardis_l2").unwrap();
    w.set_instruments(vec![inst(1, 0)]);
    let rows: Vec<L2Row> = (0..1000u64)
        .map(|i| L2Row {
            ts_event: i as i64,
            ts_recv: i as i64 + 1,
            instrument_id: 1,
            event_id: i / 3 + 1,
            sequence: i,
            kind: if i % 7 == 0 { L2Kind::Trade } else { L2Kind::Depth },
            side: if i % 2 == 0 { Side::Bid } else { Side::Ask },
            price: Some(100 + (i % 9) as i64),
            prev_qty: i,
            new_qty: i + 1,
            prev_ct: None,
            new_ct: Some(3),
            trade_qty: 0,
            aggressor: Side::None,
            level: (i % 10) as u8,
            flags: if i % 3 == 2 { l2flags::LAST_IN_BATCH } else { 0 },
        })
        .collect();
    for r in &rows {
        w.push_l2(r).unwrap();
    }
    assert_eq!(w.push_mbo(&MboEvent::default()).unwrap_err().code(), "QT-ENGINE-001");
    let s = w.finish(DataQuality::default()).unwrap();
    assert_eq!(s.manifest.kind, qt_schema::json::DatasetKindName::L2);
    let ds = Dataset::open(&root).unwrap();
    let p = &ds.partitions()[0];
    assert_eq!(p.kind, DatasetKind::L2);
    assert!(
        p.files[0]
            .to_string_lossy()
            .contains("/l2/date=1970-01-01/instrument_id=1/")
    );
    let back: Vec<L2Row> = p.l2().unwrap().map(Result::unwrap).collect();
    assert_eq!(back, rows);
    assert_eq!(p.mbo().err().unwrap().code(), "QT-CONFIG-001", "exact replay needs MBO");
    assert!(p.source_trades().unwrap().is_none());
    std::fs::remove_dir_all(root).ok();
}
