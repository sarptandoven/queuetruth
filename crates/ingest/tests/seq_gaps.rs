//! Ingest-time sequence-gap detection for etcsv and parquet MBO inputs (review M2; SPEC 5.1.3, 5.2.7): per channel
//! over the whole feed, before the symbol filter and partitioning; feed gaps only for instruments whose partition
//! spans the gap; parquet re-ingest of a dataset carries the source's ingest gaps (its partition files are per
//! instrument, so their sequences are not a feed) and checks standalone feed-order files itself.

use std::path::{Path, PathBuf};

use qt_ingest::{Dataset, ingest};
use qt_schema::io::TableWriter;
use qt_schema::json::{DataQuality, FeedGap, IngestSummary, Verdict};
use qt_schema::specs::IngestSpec;
use qt_types::{MboEvent, flags};
use serde_json::json;

const T0: i64 = 1_577_682_000_000_000_000 + 40_000_000_000_000; // 2019-12-30 ~11:06 ET

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-seq-gaps-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn rec(iid: u32, ch: u16, seq: u64, ts: i64, action: u8) -> MboEvent {
    MboEvent {
        ts_event: ts,
        ts_recv: ts,
        instrument_id: iid,
        action,
        side: b'B',
        price: 100_000_000_000,
        size: 10,
        order_id: u64::from(iid) * 1000 + seq,
        flags: flags::LAST,
        sequence: seq,
        channel_id: ch,
        ..MboEvent::default()
    }
}

/// Channel 0 carries instruments 1, 2, 3 (one gap 4 -> 8); channel 1 carries instrument 4 (one regression).
fn feed() -> Vec<MboEvent> {
    vec![
        rec(1, 0, 1, T0, b'A'),
        rec(2, 0, 2, T0 + 1, b'A'),
        rec(3, 0, 3, T0 + 2, b'A'),
        rec(4, 1, 5, T0 + 3, b'A'),
        rec(1, 0, 4, T0 + 4, b'A'),
        rec(4, 1, 6, T0 + 5, b'A'),
        rec(1, 0, 8, T0 + 100, b'A'), // 5, 6, 7 lost
        rec(3, 0, 9, T0 + 101, b'A'),
        rec(4, 1, 4, T0 + 102, b'A'), // regression
        rec(1, 0, 10, T0 + 103, b'A'),
    ]
}

fn gap_of_1() -> FeedGap {
    FeedGap {
        instrument_id: 1,
        ts_start: T0 + 4,
        ts_end: T0 + 100,
        reason: "sequence gap on channel 0: 4 -> 8 (3 missing)".into(),
    }
}

fn write_csv(events: &[MboEvent], path: &Path) {
    let mut s =
        String::from("ts_event,ts_recv,instrument_id,action,side,price,size,order_id,flags,sequence,channel_id\n");
    for e in events {
        s.push_str(&format!(
            "{},{},{},{},{},{},{},{},{},{},{}\n",
            e.ts_event,
            e.ts_recv,
            e.instrument_id,
            e.action as char,
            e.side as char,
            e.price,
            e.size,
            e.order_id,
            e.flags,
            e.sequence,
            e.channel_id
        ));
    }
    std::fs::write(path, s).unwrap();
}

fn run(out: &Path, v: serde_json::Value) -> IngestSummary {
    let mut v = v;
    v.as_object_mut()
        .unwrap()
        .insert("out".into(), json!(out.to_str().unwrap()));
    let s: IngestSpec = serde_json::from_value(v).unwrap();
    ingest(&s).unwrap()
}

fn disk_dq(root: &Path) -> DataQuality {
    serde_json::from_str(&std::fs::read_to_string(root.join("data_quality.json")).unwrap()).unwrap()
}

#[test]
fn etcsv_gaps_are_per_channel_over_the_whole_feed() {
    let dir = scratch("etcsv");
    let csv = dir.join("mbo.csv");
    write_csv(&feed(), &csv);
    let base =
        json!({"format": "etcsv", "venue": "cme", "inputs": [{"path": csv.to_str().unwrap()}], "tick_size": "0.01"});
    let s = run(&dir.join("all"), base.clone());
    let dq = &s.data_quality;
    // Instrument 2 has no record after the gap, 3 has records on both sides, 4 is on another channel.
    let gap3 = FeedGap {
        instrument_id: 3,
        ..gap_of_1()
    };
    assert_eq!((dq.sequence_gaps, dq.sequence_regressions), (1, 1));
    assert_eq!(dq.feed_gaps, vec![gap_of_1(), gap3]);
    assert_eq!(dq.verdict, Verdict::Warnings);
    assert_eq!(disk_dq(&dir.join("all")).feed_gaps, dq.feed_gaps);
    // The symbol filter drops instrument 3, whose records still close the channel's sequence (no false 2 -> 4).
    let mut f = base.clone();
    f["symbols"] = json!(["1", "4"]);
    let s = run(&dir.join("f"), f);
    assert_eq!(
        (s.data_quality.sequence_gaps, s.data_quality.feed_gaps.clone()),
        (1, vec![gap_of_1()])
    );
    // Venue generic: sequences are not meaningful.
    let mut g = base;
    g["venue"] = json!("generic");
    let s = run(&dir.join("g"), g);
    assert_eq!(
        (s.data_quality.sequence_gaps, s.data_quality.sequence_regressions),
        (0, 0)
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn parquet_reingest_carries_gaps_and_checks_feed_files() {
    let dir = scratch("parquet");
    let csv = dir.join("mbo.csv");
    write_csv(&feed(), &csv);
    let src = dir.join("src");
    let s0 = run(
        &src,
        json!({"format": "etcsv", "venue": "cme", "inputs": [{"path": csv.to_str().unwrap()}], "tick_size": "0.01"}),
    );
    // Re-ingest of the dataset directory: same counters and gaps (not re-detected on per-instrument partitions).
    let s = run(
        &dir.join("re"),
        json!({"format": "parquet", "venue": "cme", "inputs": [{"path": src.to_str().unwrap()}]}),
    );
    let key = |d: &DataQuality| (d.sequence_gaps, d.sequence_regressions, d.feed_gaps.clone());
    assert_eq!(key(&s.data_quality), key(&s0.data_quality));
    // Filtered to instrument 2: the feed-level counters stay, the gaps of instruments not kept go.
    let s = run(
        &dir.join("re2"),
        json!({"format": "parquet", "venue": "cme", "inputs": [{"path": src.to_str().unwrap()}], "symbols": ["2"]}),
    );
    assert_eq!(key(&s.data_quality), (1, 1, vec![]));
    // One partition file given directly: carried from its dataset, never re-detected.
    let ds = Dataset::open(&src).unwrap();
    let p1 = ds.partitions().iter().find(|p| p.instrument_id == 1).unwrap();
    let s = run(
        &dir.join("re3"),
        json!({"format": "parquet", "venue": "cme", "inputs": [{"path": p1.files[0].to_str().unwrap()}]}),
    );
    assert_eq!(key(&s.data_quality), (1, 1, vec![gap_of_1()]));
    // A standalone qt.mbo file in feed order is checked like any feed (the filter does not hide records).
    let feed_file = dir.join("feed.parquet");
    let mut w = TableWriter::create(&feed_file, "qt.mbo").unwrap();
    w.write(&qt_schema::codec::mbo_to_batch(&feed())).unwrap();
    w.finish().unwrap();
    let s = run(
        &dir.join("re4"),
        json!({"format": "parquet", "venue": "cme", "inputs": [{"path": feed_file.to_str().unwrap()}],
               "symbols": ["1"], "tick_size": "0.01"}),
    );
    assert_eq!(key(&s.data_quality), (1, 1, vec![gap_of_1()]));
    let _ = std::fs::remove_dir_all(&dir);
}

/// Open conflict 18 (SPEC 4.2): `IngestSpec.strict` is honoured by `qt_ingest::ingest` itself (as `qt_crypto`), not
/// only by the CLI gate: a non-clean verdict is `QT-DATA-002`; without `strict` the same input succeeds.
#[test]
fn strict_spec_refuses_a_non_clean_verdict() {
    let dir = scratch("strict");
    let csv = dir.join("mbo.csv");
    write_csv(&feed(), &csv);
    let base =
        json!({"format": "etcsv", "venue": "cme", "inputs": [{"path": csv.to_str().unwrap()}], "tick_size": "0.01"});
    assert_eq!(
        run(&dir.join("lax"), base.clone()).data_quality.verdict,
        Verdict::Warnings
    );
    let mut v = base;
    v["strict"] = json!(true);
    v["out"] = json!(dir.join("strict").to_str().unwrap());
    let s: IngestSpec = serde_json::from_value(v).unwrap();
    let err = ingest(&s).unwrap_err();
    assert_eq!(err.code(), "QT-DATA-002", "{err}");
    assert!(err.to_string().contains("Warnings"), "{err}");
}
