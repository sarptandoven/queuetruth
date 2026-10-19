//! IngestSpec 1.1 `open_s` / `close_s` (SPEC 3.11 #18, review m5, E40) and the defaulted-tick warning (review
//! M7 note): spec field > `options` > venue default (Nasdaq 09:30-16:00 ET, else 0-86,400 s); a spec field and the
//! option of the same name must agree; explicit instrument rows of a parquet input take the window, tick and lot of
//! the spec, and spec fields that contradict them are config errors, never silently ignored.

use std::path::{Path, PathBuf};

use qt_ingest::itch::{Body, Header, Message, StockDirectory, encode_file, stock};
use qt_ingest::{Dataset, ingest};
use qt_schema::json::IngestSummary;
use qt_schema::specs::IngestSpec;
use qt_types::{Instrument, MboEvent, QtError, flags};
use serde_json::json;

const MID: i64 = 1_577_682_000_000_000_000; // 2019-12-30 00:00 US/Eastern
const DATE: i32 = 18_260; // 2019-12-30

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-session-window-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn rows() -> Vec<MboEvent> {
    (0..4u64)
        .map(|i| MboEvent {
            ts_event: MID + 40_000_000_000_000 + i as i64,
            ts_recv: MID + 40_000_000_000_000 + i as i64,
            instrument_id: 13,
            action: b'A',
            side: b'B',
            price: 100_000_000_000,
            size: 10,
            order_id: i + 1,
            flags: flags::LAST,
            sequence: i + 1,
            ..MboEvent::default()
        })
        .collect()
}

fn csv(dir: &Path) -> PathBuf {
    let p = dir.join("mbo.csv");
    let mut s =
        String::from("ts_event,ts_recv,instrument_id,action,side,price,size,order_id,flags,sequence,channel_id\n");
    for e in rows() {
        s.push_str(&format!(
            "{},{},13,A,B,{},{},{},{},{},0\n",
            e.ts_event, e.ts_recv, e.price, e.size, e.order_id, e.flags, e.sequence
        ));
    }
    std::fs::write(&p, s).unwrap();
    p
}

fn ingest_at(out: &Path, v: serde_json::Value) -> Result<IngestSummary, QtError> {
    let mut v = v;
    v.as_object_mut()
        .unwrap()
        .insert("out".into(), json!(out.to_str().unwrap()));
    let s: IngestSpec = serde_json::from_value(v).unwrap();
    ingest(&s)
}

fn row(out: &Path) -> Instrument {
    Dataset::open(out).unwrap().instrument(13, DATE).unwrap().clone()
}

#[test]
fn open_close_precedence_conflicts_and_ranges() {
    let dir = scratch("window");
    let m = csv(&dir);
    let mut k = 0;
    let mut go = |extra: serde_json::Value| -> Result<(u32, u32), String> {
        k += 1;
        let out = dir.join(format!("o{k}"));
        let mut v = json!({"format": "etcsv", "venue": "nasdaq", "inputs": [{"path": m.to_str().unwrap()}],
                           "midnight_utc_ns": MID});
        for (key, x) in extra.as_object().unwrap() {
            v.as_object_mut().unwrap().insert(key.clone(), x.clone());
        }
        ingest_at(&out, v)
            .map(|_| {
                let r = row(&out);
                (r.open_s, r.close_s)
            })
            .map_err(|e| format!("{} {e}", e.code()))
    };
    assert_eq!(go(json!({})), Ok((34_200, 57_600)), "nasdaq default (E24)");
    assert_eq!(go(json!({"venue": "generic", "tick_size": "0.01"})), Ok((0, 86_400)));
    assert_eq!(go(json!({"open_s": 36_000, "close_s": 50_400})), Ok((36_000, 50_400)));
    assert_eq!(go(json!({"options": {"open_s": 36_000}})), Ok((36_000, 57_600)));
    assert_eq!(
        go(json!({"open_s": 0, "options": {"close_s": 86_400}})),
        Ok((0, 86_400))
    );
    assert_eq!(
        go(json!({"open_s": 36_000, "options": {"open_s": 36_000}})),
        Ok((36_000, 57_600))
    );
    let e = go(json!({"open_s": 36_000, "options": {"open_s": 34_200}})).unwrap_err();
    assert!(e.starts_with("QT-CONFIG-001") && e.contains("open_s"), "{e}");
    let e = go(json!({"close_s": 50_000, "options": {"close_s": 50_001}})).unwrap_err();
    assert!(e.starts_with("QT-CONFIG-001") && e.contains("close_s"), "{e}");
    let e = go(json!({"close_s": 30_000})).unwrap_err();
    assert!(e.starts_with("QT-CONFIG-001") && e.contains("open_s / close_s"), "{e}");
    assert!(
        go(json!({"close_s": 172_801, "open_s": 0}))
            .unwrap_err()
            .starts_with("QT-CONFIG-001")
    );
    assert_eq!(go(json!({"close_s": 172_800})), Ok((34_200, 172_800)));
    assert!(
        go(json!({"options": {"open_s": "x"}}))
            .unwrap_err()
            .starts_with("QT-CONFIG-001")
    );
    let _ = std::fs::remove_dir_all(&dir);
}

fn itch_day() -> Vec<Message> {
    let h = |kind: u8, ts_ns: u64| Header {
        kind,
        locate: 1,
        tracking: 1,
        ts_ns,
    };
    vec![
        Message {
            header: h(b'R', 1_000),
            body: Body::StockDirectory(StockDirectory {
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
        },
        Message {
            header: h(b'A', 34_200_000_000_000),
            body: Body::AddOrder {
                order_ref: 7,
                buy_sell: b'B',
                shares: 100,
                stock: stock("AAPL"),
                price: 1_852_500,
                mpid: None,
            },
        },
    ]
}

#[test]
fn itch_rows_take_the_window_fields() {
    let dir = scratch("itch");
    let f = dir.join("12302019.NASDAQ_ITCH50");
    encode_file(&itch_day(), &f, false).unwrap();
    let out = dir.join("ds");
    ingest_at(
        &out,
        json!({"format": "itch", "venue": "nasdaq", "inputs": [{"path": f.to_str().unwrap()}],
               "open_s": 14_400, "close_s": 72_000}),
    )
    .unwrap();
    let ds = Dataset::open(&out).unwrap();
    let r = ds.instrument(1, DATE).unwrap();
    assert_eq!((r.open_s, r.close_s, r.midnight_utc_ns), (14_400, 72_000, MID));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn parquet_explicit_rows_honour_or_reject_spec_fields() {
    let dir = scratch("parquet");
    let m = csv(&dir);
    let src = dir.join("src");
    ingest_at(
        &src,
        json!({"format": "etcsv", "venue": "nasdaq", "inputs": [{"path": m.to_str().unwrap()}], "midnight_utc_ns": MID}),
    )
    .unwrap();
    let base = json!({"format": "parquet", "venue": "nasdaq", "inputs": [{"path": src.to_str().unwrap()}]});
    let with = |extra: serde_json::Value| {
        let mut v = base.clone();
        for (key, x) in extra.as_object().unwrap() {
            v.as_object_mut().unwrap().insert(key.clone(), x.clone());
        }
        v
    };
    // No window fields: the rows keep their own window (not the venue default of the re-ingest venue).
    ingest_at(&dir.join("a"), with(json!({"venue": "generic"}))).unwrap();
    assert_eq!(
        (row(&dir.join("a")).open_s, row(&dir.join("a")).close_s),
        (34_200, 57_600)
    );
    // Window, tick and lot override every row.
    ingest_at(
        &dir.join("b"),
        with(json!({"open_s": 36_000, "options": {"close_s": 50_400}, "tick_size": "0.05", "lot_size": "100"})),
    )
    .unwrap();
    let r = row(&dir.join("b"));
    assert_eq!(
        (r.open_s, r.close_s, r.tick_size, r.lot_size, r.midnight_utc_ns),
        (36_000, 50_400, 50_000_000, 100, MID)
    );
    // Contradictions are config errors naming the field.
    for (k, extra) in [
        ("tz", json!({"tz": "UTC"})),
        ("session_date", json!({"session_date": "2019-12-30"})),
        ("midnight_utc_ns", json!({"midnight_utc_ns": MID})),
        ("price_exp", json!({"price_exp": -4})),
        ("size_exp", json!({"size_exp": 2})),
        ("open_s", json!({"open_s": 60_000})),
    ] {
        let e = ingest_at(&dir.join(format!("x-{k}")), with(extra)).unwrap_err();
        assert_eq!(e.code(), "QT-CONFIG-001", "{k}: {e}");
        assert!(e.to_string().contains(k), "{k}: {e}");
    }
    // Agreeing values are accepted.
    ingest_at(
        &dir.join("c"),
        with(json!({"tz": "America/New_York", "price_exp": -9, "size_exp": 0})),
    )
    .unwrap();
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn defaulted_tick_is_a_loud_warning() {
    let dir = scratch("tick");
    let f = dir.join("glbx.dbn");
    let ev: Vec<MboEvent> = rows()
        .into_iter()
        .map(|e| MboEvent { instrument_id: 42, ..e })
        .collect();
    qt_testkit::encode_dbn(&ev, &f).unwrap();
    let out = dir.join("ds");
    let s = ingest_at(
        &out,
        json!({"format": "dbn", "venue": "cme", "inputs": [{"path": f.to_str().unwrap()}]}),
    )
    .unwrap();
    let dq = &s.data_quality;
    assert_eq!(dq.source_specific.get("instruments_default_tick_size"), Some(&1));
    let w: Vec<&String> = dq.reasons.iter().filter(|r| r.starts_with("warning: ")).collect();
    assert_eq!(w.len(), 1, "{:?}", dq.reasons);
    assert!(
        w[0].contains("tick_size defaulted to 1 price unit")
            && w[0].contains("42 (id 42, 2019-12-30)")
            && w[0].contains("--tick-size"),
        "{}",
        w[0]
    );
    let on_disk: qt_schema::json::DataQuality =
        serde_json::from_str(&std::fs::read_to_string(out.join("data_quality.json")).unwrap()).unwrap();
    assert_eq!(on_disk.reasons, dq.reasons);
    // With a tick (or on Nasdaq, whose default tick is 0.01) there is no warning.
    for (name, extra) in [
        ("t", json!({"venue": "cme", "tick_size": "0.25"})),
        ("n", json!({"venue": "nasdaq"})),
    ] {
        let mut v = json!({"format": "dbn", "inputs": [{"path": f.to_str().unwrap()}]});
        for (key, x) in extra.as_object().unwrap() {
            v.as_object_mut().unwrap().insert(key.clone(), x.clone());
        }
        let s = ingest_at(&dir.join(name), v).unwrap();
        assert!(
            !s.data_quality.reasons.iter().any(|r| r.starts_with("warning: ")),
            "{name}"
        );
        assert!(
            !s.data_quality
                .source_specific
                .contains_key("instruments_default_tick_size")
        );
    }
    let _ = std::fs::remove_dir_all(&dir);
}
