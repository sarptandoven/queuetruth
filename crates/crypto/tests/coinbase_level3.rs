//! Coinbase Exchange `level3` (compact) captures: golden records on the hand-built fixture, schema handling,
//! sequence gaps, snapshots and re-snapshots.

mod common;

use common::{Act, acts, fixture, get, lines, replay};
use indexmap::IndexMap;
use qt_crypto::coinbase_l3::{Config, convert, convert_full};
use qt_crypto::units::iso_ns;
use qt_types::{MboEvent, Side};

const S: u8 = 32; // SNAPSHOT
const L: u8 = 128; // LAST
const SL: u8 = 160;

fn px(s: &str) -> i64 {
    qt_crypto::units::price(s, -9).unwrap()
}

fn sz(s: &str) -> u64 {
    qt_crypto::units::size(s, -8).unwrap()
}

fn t(s: &str) -> i64 {
    iso_ns(&format!("2026-10-11T00:00:{s}Z")).unwrap()
}

fn fixture_lines() -> Vec<String> {
    let files = [
        fixture("coinbase_level3/book.jsonl"),
        fixture("coinbase_level3/level3.jsonl"),
    ];
    qt_crypto::input::lines(&files).unwrap().map(Result::unwrap).collect()
}

fn of(events: &[MboEvent], inst: u32) -> Vec<Act> {
    acts(
        &events
            .iter()
            .filter(|e| e.instrument_id == inst)
            .copied()
            .collect::<Vec<_>>(),
    )
}

#[test]
fn golden_fixture_records() {
    let (events, trades, c) = convert(lines(&fixture_lines()), &Config::default()).unwrap();
    let b = |p: &str| px(p);
    let btc: Vec<Act> = vec![
        ('R', 'N', 0, 0, 0, S, 100),
        ('A', 'B', b("60000"), sz("0.5"), 1, S, 100),
        ('A', 'B', b("60000"), sz("0.25"), 2, S, 100),
        ('A', 'B', b("59999.5"), sz("1"), 3, S, 100),
        ('A', 'A', b("60001"), sz("0.75"), 4, SL, 100),
        ('A', 'A', b("60002"), sz("0.4"), 5, L, 101),
        ('T', 'A', b("60000"), sz("0.2"), 6, 0, 102),
        ('F', 'B', b("60000"), sz("0.2"), 1, 0, 102),
        ('C', 'B', b("60000"), sz("0.2"), 1, L, 102),
        ('M', 'B', b("60000"), sz("0.1"), 2, L, 103),
        ('T', 'A', b("60000"), sz("0.3"), 7, 0, 106),
        ('F', 'B', b("60000"), sz("0.3"), 1, 0, 106),
        ('C', 'B', b("60000"), sz("0.3"), 1, L, 106),
        ('C', 'A', b("60001"), sz("0.75"), 4, L, 108),
        ('A', 'B', b("59999.5"), sz("0.2"), 8, L, 110),
        ('R', 'N', 0, 0, 0, S, 111),
        ('A', 'B', b("60000"), sz("0.1"), 2, S, 111),
        ('A', 'B', b("59999.5"), sz("1"), 3, S, 111),
        ('A', 'B', b("59999.5"), sz("0.2"), 8, S, 111),
        ('A', 'B', b("59998"), sz("0.3"), 9, S, 111),
        ('A', 'A', b("60002"), sz("0.4"), 5, SL, 111),
        ('M', 'A', b("60002"), sz("0.3"), 5, L, 112),
        ('C', 'B', b("59999.5"), sz("1"), 3, L, 113),
    ];
    assert_eq!(of(&events, 1), btc);
    let eth: Vec<Act> = vec![
        ('A', 'B', b("2500.1"), sz("1.5"), 10, L, 5001),
        ('C', 'B', b("2500.1"), sz("1.5"), 10, L, 5002),
    ];
    assert_eq!(of(&events, 2), eth);
    // global order: (monotone) time, then product
    assert!(events.windows(2).all(|w| w[0].ts_event <= w[1].ts_event));
    // snapshot times: REST time, clamped to the neighbouring messages
    assert_eq!(events[0].ts_event, t("00.900000"));
    let snap2 = events.iter().find(|e| e.action == b'R' && e.sequence == 111).unwrap();
    assert_eq!(snap2.ts_event, t("02.500000"));
    // receive times come from the capture wrapper
    let open5 = events.iter().find(|e| e.sequence == 101).unwrap();
    assert_eq!(
        open5.ts_recv - t("01.000000"),
        1_791_676_800_000_000_000 + 1_000_007_000 - t("01.000000")
    );
    assert_eq!(trades.len(), 2);
    assert!(
        trades
            .iter()
            .all(|t| t.aggressor_side == Side::Ask && t.maker_order_id == Some(1))
    );
    for (k, v) in [
        ("superseded_by_snapshot", 2),
        ("duplicates_dropped", 1),
        ("gaps", 1),
        ("snapshots", 2),
        ("resnapshots", 1),
        ("done_not_on_book", 1),
        ("done_filled", 1),
        ("done_canceled", 3),
        ("unseeded_products", 1),
        ("heartbeats", 1),
        ("heartbeat_trailing_gaps", 0),
        ("schema_messages", 1),
        ("schema_assumed", 0),
        ("noop", 1),
        ("open", 3),
        ("match", 2),
        ("change", 2),
        ("done", 5),
        ("match_unknown_maker", 0),
        ("open_at_end", 4),
    ] {
        assert_eq!(get(&c, k), v, "{k}");
    }
    // the records replay into a consistent book
    let book = replay(&events).unwrap();
    assert_eq!(
        book.side(1, b'B', 10),
        vec![
            (b("60000"), sz("0.1")),
            (b("59999.5"), sz("0.2")),
            (b("59998"), sz("0.3"))
        ]
    );
    assert_eq!(book.side(1, b'A', 10), vec![(b("60002"), sz("0.3"))]);
}

#[test]
fn gaps_and_unseeded_products_are_feed_gaps() {
    let o = convert_full(lines(&fixture_lines()), &Config::default()).unwrap();
    assert_eq!(o.sequence_gaps, 1);
    let g: Vec<(u32, i64, i64)> = o
        .feed_gaps
        .iter()
        .map(|g| (g.instrument_id, g.ts_start, g.ts_end))
        .collect();
    assert_eq!(
        g,
        vec![
            (1, t("01.500000"), t("02.500000")), // gap 108 -> 110 until the re-snapshot
            (2, t("01.050000"), t("01.550000")), // ETH-USD has no snapshot
        ]
    );
    assert!(o.feed_gaps[0].reason.contains("sequence gap 108 -> 110"));
}

fn w(msg: &str) -> String {
    format!(r#"{{"recv_ns":1,"msg":{msg}}}"#)
}

const SNAP: &str = r#"{"recv_ns":1,"product_id":"BTC-USD","book":{"sequence":10,"bids":[["100.00","1","a"]],"asks":[["101.00","2","b"]],"time":"2026-10-11T00:00:00.000000Z"}}"#;

#[test]
fn schema_frame_reorders_fields_and_missing_schema_is_assumed() {
    // a schema with a different field order is honoured for later arrays
    let v = vec![
        SNAP.to_string(),
        w(
            r#"{"type":"level3","schema":{"open":["type","sequence","product_id","time","order_id","side","size","price"],"done":["type","product_id","sequence","order_id","time"]}}"#,
        ),
        w(r#"["open","11","BTC-USD","2026-10-11T00:00:01.000000Z","c","sell","0.5","102.00"]"#),
        w(r#"["done","BTC-USD","12","c","2026-10-11T00:00:02.000000Z"]"#),
    ];
    let (ev, _, c) = convert(lines(&v), &Config::default()).unwrap();
    assert_eq!(
        acts(&ev[3..]),
        vec![
            ('A', 'A', px("102"), sz("0.5"), 3, L, 11),
            ('C', 'A', px("102"), sz("0.5"), 3, L, 12)
        ]
    );
    assert_eq!(get(&c, "schema_assumed"), 0);
    // without a schema frame the documented schema is used and counted
    let v = vec![
        SNAP.to_string(),
        w(r#"["open","BTC-USD","11","c","sell","102.00","0.5","2026-10-11T00:00:01.000000Z"]"#),
    ];
    let (ev, _, c) = convert(lines(&v), &Config::default()).unwrap();
    assert_eq!(acts(&ev[3..]), vec![('A', 'A', px("102"), sz("0.5"), 3, L, 11)]);
    assert_eq!(get(&c, "schema_assumed"), 1);
    // a schema change mid-stream is counted
    let v = vec![
        w(r#"{"type":"level3","schema":{"noop":["type","product_id","sequence","time"]}}"#),
        w(r#"{"type":"level3","schema":{"noop":["type","sequence","product_id","time"]}}"#),
    ];
    let (_, _, c) = convert(lines(&v), &Config::default()).unwrap();
    assert_eq!(get(&c, "schema_changes"), 1);
}

#[test]
fn match_on_unknown_maker_and_done_semantics() {
    let v = vec![
        SNAP.to_string(),
        // taker hits an order that is not on the seeded book: aggressor unknown
        w(r#"["match","BTC-USD","11","zz","t","100.00","0.1","2026-10-11T00:00:01.000000Z"]"#),
        // overfill of a known maker is clamped
        w(r#"["match","BTC-USD","12","a","t","100.00","1.5","2026-10-11T00:00:02.000000Z"]"#),
        w(r#"["done","BTC-USD","13","a","2026-10-11T00:00:03.000000Z"]"#),
        // change with a null price keeps the price
        w(r#"["change","BTC-USD","14","b",null,"1.25","2026-10-11T00:00:04.000000Z"]"#),
    ];
    let (ev, tr, c) = convert(lines(&v), &Config::default()).unwrap();
    assert_eq!(
        acts(&ev[3..]),
        vec![
            ('T', 'N', px("100"), sz("0.1"), 3, L, 11),
            ('T', 'A', px("100"), sz("1.5"), 3, 0, 12),
            ('F', 'B', px("100"), sz("1.5"), 1, 0, 12),
            ('C', 'B', px("100"), sz("1"), 1, L, 12),
            ('M', 'A', px("101"), sz("1.25"), 2, L, 14),
        ]
    );
    assert_eq!(tr[0].aggressor_side, Side::None);
    assert_eq!(
        (
            get(&c, "match_unknown_maker"),
            get(&c, "match_overfill"),
            get(&c, "done_filled")
        ),
        (1, 1, 1)
    );
}

#[test]
fn heartbeat_beyond_last_message_is_a_trailing_gap() {
    let v = vec![
        SNAP.to_string(),
        w(r#"["noop","BTC-USD","11","2026-10-11T00:00:01.000000Z"]"#),
        w(
            r#"{"type":"heartbeat","sequence":15,"last_trade_id":1,"product_id":"BTC-USD","time":"2026-10-11T00:00:05.000000Z"}"#,
        ),
    ];
    let o = convert_full(lines(&v), &Config::default()).unwrap();
    assert_eq!(get(&o.counters, "heartbeat_trailing_gaps"), 1);
    assert_eq!(o.feed_gaps.len(), 1);
}

#[test]
fn malformed_input_is_an_error_not_a_panic() {
    for bad in [
        r#"{"recv_ns":1,"msg":["open","BTC-USD"]}"#,
        r#"{"recv_ns":1,"msg":["open","BTC-USD","x","c","buy","1","1","2026-10-11T00:00:01Z"]}"#,
        r#"{"recv_ns":1,"msg":["open","BTC-USD","11","c","up","1","1","2026-10-11T00:00:01Z"]}"#,
        r#"{"recv_ns":1,"msg":["open","BTC-USD","11","c","buy","1.0000000001","1","2026-10-11T00:00:01Z"]}"#,
        r#"{"recv_ns":1,"product_id":"BTC-USD","book":{"sequence":1,"bids":[["1","1"]],"asks":[]}}"#,
        r#"{"recv_ns":1,"msg":{"type":"level3","schema":{"open":"nope"}}}"#,
        "not json",
    ] {
        let v = vec![SNAP.to_string(), bad.to_string()];
        let e = convert(lines(&v), &Config::default()).unwrap_err();
        assert!(e.code().starts_with("QT-INPUT"), "{bad}: {e}");
    }
    let cfg = Config {
        instruments: IndexMap::from([("BTC-USD".to_string(), 7)]),
        ..Config::default()
    };
    let (ev, _, _) = convert(lines(&[SNAP.to_string()]), &cfg).unwrap();
    assert!(ev.iter().all(|e| e.instrument_id == 7));
}
