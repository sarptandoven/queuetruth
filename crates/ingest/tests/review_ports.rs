//! Ports of the independent review's failing ingest tests (`itch_tz.rs` for M1,
//! `seq_gap.rs` for M2, `dbn_truncated.rs` for m3) plus m7 (inexact DBN price is QT-INPUT-004). Each test failed
//! before the fix.

use std::path::{Path, PathBuf};

use qt_ingest::itch::{Body, Header, Message, StockDirectory, encode_file, stock};
use qt_ingest::{Dataset, ingest};
use qt_schema::json::{DataQuality, FeedGap, Verdict};
use qt_schema::specs::IngestSpec;
use qt_types::{MboEvent, flags};
use serde_json::json;

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-review-ports-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn spec(v: serde_json::Value) -> IngestSpec {
    serde_json::from_value(v).unwrap()
}

// ---- M1: ITCH session midnight is always US/Eastern -------------------------------------------------------------

fn msg(kind: u8, locate: u16, ts_ns: u64, body: Body) -> Message {
    Message {
        header: Header {
            kind,
            locate,
            tracking: 1,
            ts_ns,
        },
        body,
    }
}

fn itch_day() -> Vec<Message> {
    vec![
        msg(
            b'R',
            1,
            1_000,
            Body::StockDirectory(StockDirectory {
                stock: stock("AAPL"),
                market_category: b'Q',
                financial_status: b'N',
                round_lot_size: 100,
                round_lots_only: b'N',
                issue_classification: b'C',
                issue_sub_type: *b"Z ",
                authenticity: b'P',
                short_sale_threshold: b'N',
                ipo_flag: b'N',
                luld_tier: b'1',
                etp_flag: b'N',
                etp_leverage_factor: 0,
                inverse_indicator: b'N',
            }),
        ),
        msg(
            b'A',
            1,
            34_200_000_000_000,
            Body::AddOrder {
                order_ref: 7,
                buy_sell: b'B',
                shares: 100,
                stock: stock("AAPL"),
                price: 1_852_500,
                mpid: None,
            },
        ),
    ]
}

/// Ingests the two-message ITCH day of 2019-01-30 with `extra` spec fields: (first ts_event, instrument midnight,
/// open_s, session_tz) or the error code.
fn run_itch(name: &str, extra: serde_json::Value) -> Result<(i64, i64, u32, String), String> {
    let d = scratch(name);
    let file = d.join("01302019.NASDAQ_ITCH50");
    encode_file(&itch_day(), &file, false).unwrap();
    let out = d.join("ds");
    let mut v = json!({"format": "itch", "inputs": [{"path": file.to_str().unwrap()}], "venue": "nasdaq",
                       "out": out.to_str().unwrap()});
    for (k, x) in extra.as_object().unwrap() {
        v.as_object_mut().unwrap().insert(k.clone(), x.clone());
    }
    let r = ingest(&spec(v)).map_err(|e| e.code().to_owned()).map(|_| {
        let ds = Dataset::open(&out).unwrap();
        let p = &ds.partitions()[0];
        let rec = p.mbo().unwrap().next().unwrap().unwrap();
        let inst = ds.instrument(p.instrument_id, p.session_date).unwrap().clone();
        (rec.ts_event, inst.midnight_utc_ns, inst.open_s, inst.session_tz)
    });
    let _ = std::fs::remove_dir_all(&d);
    r
}

// 2019-01-30: Eastern midnight = 2019-01-30T05:00:00Z = 1_548_824_400 s.
const NY_MIDNIGHT: i64 = 1_548_824_400_000_000_000;
const ADD_TS: i64 = NY_MIDNIGHT + 34_200_000_000_000;

#[test]
fn m1_itch_nasdaq_default_is_eastern() {
    let got = run_itch("tz-default", json!({})).unwrap();
    assert_eq!(got, (ADD_TS, NY_MIDNIGHT, 34_200, "America/New_York".to_owned()));
    let got = run_itch("tz-ny", json!({"tz": "America/New_York"})).unwrap();
    assert_eq!(got, (ADD_TS, NY_MIDNIGHT, 34_200, "America/New_York".to_owned()));
}

#[test]
fn m1_itch_with_tz_utc_is_a_config_error() {
    // Was: every ts_event shifted 5 h early and the rows said UTC. Now rejected (QT-CONFIG-001 naming tz).
    assert_eq!(
        run_itch("tz-utc", json!({"tz": "UTC"})),
        Err("QT-CONFIG-001".to_owned())
    );
}

#[test]
fn m1_itch_with_venue_generic_keeps_records_and_rows_on_eastern_midnight() {
    let (ts, mid, open, tz) = run_itch("generic", json!({"venue": "generic"})).unwrap();
    assert_eq!(
        ts - mid,
        34_200_000_000_000,
        "record is not 09:30 after the instrument's midnight"
    );
    assert_eq!((ts, mid, tz.as_str()), (ADD_TS, NY_MIDNIGHT, "America/New_York"));
    assert_eq!(open, 0, "generic venue default window");
}

#[test]
fn m1_itch_midnight_must_be_the_eastern_midnight() {
    // An explicit midnight that is not Eastern midnight would shift every record.
    let utc_midnight = NY_MIDNIGHT - 5 * 3_600_000_000_000;
    assert_eq!(
        run_itch("mid-utc", json!({"midnight_utc_ns": utc_midnight})),
        Err("QT-CONFIG-001".to_owned())
    );
    assert_eq!(
        run_itch(
            "mid-utc2",
            json!({"session_date": "2019-01-30", "midnight_utc_ns": utc_midnight})
        ),
        Err("QT-CONFIG-001".to_owned())
    );
    let got = run_itch("mid-ny", json!({"midnight_utc_ns": NY_MIDNIGHT})).unwrap();
    assert_eq!((got.0, got.1), (ADD_TS, NY_MIDNIGHT));
}

// ---- M2: sequence gaps of MBO feeds are detected at ingest ------------------------------------------------------

fn rec(seq: u64, ts: i64, action: u8, side: u8, px: i64, size: u64, id: u64) -> MboEvent {
    MboEvent {
        ts_event: ts,
        ts_recv: ts,
        instrument_id: 1,
        action,
        side,
        price: px,
        size,
        order_id: id,
        flags: flags::LAST,
        sequence: seq,
        channel_id: 0,
        ..MboEvent::default()
    }
}

fn read_dq(root: &Path) -> DataQuality {
    serde_json::from_str(&std::fs::read_to_string(root.join("data_quality.json")).unwrap()).unwrap()
}

#[test]
fn m2_dbn_channel_gap_is_detected() {
    let d = scratch("seqgap");
    let base = 1_781_481_600_000_000_000i64 + 50_000_000_000_000; // 2026-06-15 + ~13.9 h UTC
    let px = 5_000_000_000_000i64;
    // A jump from sequence 3 to 50 on the same channel (46 messages lost).
    let ev = vec![
        rec(1, base, b'A', b'B', px, 10, 1),
        rec(2, base + 1, b'A', b'A', px + 250_000_000, 10, 2),
        rec(3, base + 2, b'A', b'B', px, 5, 3),
        rec(50, base + 10_000, b'C', b'B', px, 10, 1),
        rec(51, base + 10_001, b'A', b'B', px, 7, 4),
    ];
    let f = d.join("glbx.dbn");
    qt_testkit::encode_dbn(&ev, &f).unwrap();
    let out = d.join("ds");
    let s = ingest(&spec(json!({
        "format": "dbn", "inputs": [{"path": f.to_str().unwrap()}], "venue": "cme",
        "out": out.to_str().unwrap(), "tick_size": "0.25"
    })))
    .unwrap();
    let dq = &s.data_quality;
    let want = vec![FeedGap {
        instrument_id: 1,
        ts_start: base + 2,
        ts_end: base + 10_000,
        reason: "sequence gap on channel 0: 3 -> 50 (46 missing)".into(),
    }];
    assert_eq!((dq.sequence_gaps, dq.sequence_regressions), (1, 0));
    assert_eq!(dq.feed_gaps, want);
    assert_eq!(dq.verdict, Verdict::Warnings);
    assert!(dq.reasons.iter().any(|r| r == "sequence_gaps = 1"), "{:?}", dq.reasons);
    // Replay reads the gaps from the dataset's data_quality.json (the agreed path).
    assert_eq!(read_dq(&out).feed_gaps, want);
    // A venue without meaningful sequences (generic) does not count them.
    let s = ingest(&spec(json!({
        "format": "dbn", "inputs": [{"path": f.to_str().unwrap()}], "venue": "generic",
        "out": d.join("ds2").to_str().unwrap(), "tick_size": "0.25"
    })))
    .unwrap();
    assert_eq!((s.data_quality.sequence_gaps, s.data_quality.feed_gaps.len()), (0, 0));
    let _ = std::fs::remove_dir_all(&d);
}

// ---- m3 / m7: DBN truncation and inexact prices -----------------------------------------------------------------

fn events(n: usize) -> Vec<MboEvent> {
    (0..n)
        .map(|i| MboEvent {
            ts_event: 1_000_000_000 + i as i64,
            ts_recv: 1_000_000_000 + i as i64,
            instrument_id: 1,
            action: b'A',
            side: b'B',
            price: 100_000_000_000,
            size: 1,
            order_id: i as u64 + 1,
            flags: flags::LAST,
            sequence: i as u64 + 1,
            ..MboEvent::default()
        })
        .collect()
}

fn count(path: &Path) -> Result<usize, qt_types::QtError> {
    let mut r = qt_ingest::dbn::DbnReader::open(path)?;
    let mut n = 0;
    while r.next_mbo(-9)?.is_some() {
        n += 1;
    }
    Ok(n)
}

fn check_truncated(name: &str, cut: impl Fn(usize) -> usize) {
    let dir = scratch(&format!("trunc-{name}"));
    let p = dir.join(name);
    qt_testkit::encode_dbn(&events(1000), &p).unwrap();
    let full = std::fs::read(&p).unwrap();
    assert_eq!(count(&p).unwrap(), 1000);
    std::fs::write(&p, &full[..cut(full.len())]).unwrap();
    let got = count(&p);
    let _ = std::fs::remove_dir_all(&dir);
    let e = got.expect_err("truncated DBN accepted silently");
    assert_eq!(e.code(), "QT-INPUT-002", "{name}: {e}");
}

#[test]
fn m3_truncated_plain_dbn_is_an_error() {
    check_truncated("t.dbn", |n| n - 10);
    let dir = scratch("trunc-msg");
    let p = dir.join("t.dbn");
    qt_testkit::encode_dbn(&events(10), &p).unwrap();
    let full = std::fs::read(&p).unwrap();
    std::fs::write(&p, &full[..full.len() - 3]).unwrap();
    let e = count(&p).unwrap_err().to_string();
    assert!(
        e.contains("truncated DBN stream") && e.contains("needs 3 more bytes"),
        "{e}"
    );
    // Cut exactly at a record boundary: a valid, shorter stream (the framing cannot tell).
    std::fs::write(&p, &full[..full.len() - 56]).unwrap();
    assert_eq!(count(&p).unwrap(), 9);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn m3_truncated_zstd_dbn_is_an_error() {
    check_truncated("t.dbn.zst", |n| n / 2);
}

#[test]
fn m3_truncated_zstd_dbn_tail_is_an_error() {
    check_truncated("t2.dbn.zst", |n| n - 10);
}

#[test]
fn m7_inexact_dbn_price_is_a_unit_error() {
    let d = scratch("units");
    let mut ev = events(3);
    ev[1].price = 100_000_000_001; // 100.000000001: not exact at 10^-2
    let f = d.join("u.dbn");
    qt_testkit::encode_dbn(&ev, &f).unwrap();
    let mut r = qt_ingest::dbn::DbnReader::open(&f).unwrap();
    assert!(r.next_mbo(-2).unwrap().is_some());
    let e = r.next_mbo(-2).unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-004", "{e}");
    assert!(e.to_string().contains("record 2"), "{e}");
    let e = ingest(&spec(json!({
        "format": "dbn", "inputs": [{"path": f.to_str().unwrap()}], "venue": "cme",
        "out": d.join("ds").to_str().unwrap(), "price_exp": -2, "tick_size": "0.01"
    })))
    .unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-004");
    let _ = std::fs::remove_dir_all(&d);
}
