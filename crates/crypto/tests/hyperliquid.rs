//! Hyperliquid node outputs (raw book diffs + fills + order statuses + L4 snapshot): golden records on the
//! hand-built fixture, fill attribution, seeding and anomalies.

mod common;

use common::{Act, acts, fixture, get, lines, replay};
use qt_crypto::hyperliquid::{Config, convert, convert_full};
use qt_types::Side;

const S: u8 = 32;
const L: u8 = 128;
const SL: u8 = 160;

fn px(s: &str) -> i64 {
    qt_crypto::units::price(s, -9).unwrap()
}

fn sz(s: &str) -> u64 {
    qt_crypto::units::size(s, -8).unwrap()
}

fn read(rel: &str) -> Vec<String> {
    qt_crypto::input::lines(&[fixture(rel)])
        .unwrap()
        .map(Result::unwrap)
        .collect()
}

fn all_lines() -> Vec<String> {
    let mut v = read("hyperliquid_node/node_raw_book_diffs_by_block/hourly/20261011/0");
    v.extend(read("hyperliquid_node/node_fills_by_block/hourly/20261011/0"));
    v.extend(read("hyperliquid_node/node_order_statuses_by_block/hourly/20261011/0"));
    v
}

fn with_snapshot() -> Config {
    Config {
        snapshots: vec![(
            1000,
            std::fs::read_to_string(fixture("hyperliquid_node/l4_snapshot_1000.json")).unwrap(),
        )],
        ..Config::default()
    }
}

#[test]
fn golden_fixture_records() {
    let (ev, trades, c) = convert(lines(&all_lines()), &with_snapshot()).unwrap();
    let want: Vec<Act> = vec![
        // L4 snapshot at height 1000, applied before block 1001 (trigger order 77 skipped)
        ('R', 'N', 0, 0, 0, S, 1000),
        ('A', 'B', px("100"), sz("1"), 11, S, 1000),
        ('A', 'B', px("100"), sz("0.5"), 12, S, 1000),
        ('A', 'A', px("101"), sz("2"), 21, SL, 1000),
        ('A', 'B', px("99.5"), sz("3"), 13, L, 1001),
        // block 1002: taker 31 sells 1.2: 11 filled (remove), 12 partially (update 0.5 -> 0.3)
        ('T', 'A', px("100"), sz("1"), 31, 0, 1002),
        ('F', 'B', px("100"), sz("1"), 11, 0, 1002),
        ('C', 'B', px("100"), sz("1"), 11, L, 1002),
        ('T', 'A', px("100"), sz("0.2"), 31, 0, 1002),
        ('F', 'B', px("100"), sz("0.2"), 12, 0, 1002),
        ('C', 'B', px("100"), sz("0.2"), 12, L, 1002),
        // block 1003: 13 canceled (order status)
        ('C', 'B', px("99.5"), sz("3"), 13, L, 1003),
        ('A', 'A', px("101.5"), sz("1"), 41, L, 1004),
        ('A', 'B', px("100"), sz("0.7"), 42, L, 1004),
        // block 1005: taker 51 buys 0.5 from 21
        ('T', 'B', px("101"), sz("0.5"), 51, 0, 1005),
        ('F', 'A', px("101"), sz("0.5"), 21, 0, 1005),
        ('C', 'A', px("101"), sz("0.5"), 21, L, 1005),
    ];
    assert_eq!(acts(&ev), want);
    // block_time -> ts_event, local_time -> ts_recv (20 ms later), the snapshot at the first later block's time
    let t = |ms: i64| qt_crypto::units::iso_ns("2026-10-11T00:00:00Z").unwrap() + ms * 1_000_000;
    assert_eq!((ev[0].ts_event, ev[0].ts_recv), (t(200), t(220)));
    assert_eq!(ev.last().map(|e| e.ts_event), Some(t(600)));
    assert_eq!(trades.len(), 3);
    assert_eq!(
        trades
            .iter()
            .map(|t| (t.aggressor_side, t.maker_order_id))
            .collect::<Vec<_>>(),
        vec![(Side::Ask, Some(11)), (Side::Ask, Some(12)), (Side::Bid, Some(21))]
    );
    for (k, v) in [
        ("superseded_by_snapshot", 1),
        ("snapshots", 1),
        ("snapshot_trigger_orders_skipped", 1),
        ("diffs", 8),
        ("fills", 6),
        ("order_statuses", 2),
        ("removed_canceled", 1),
        ("removed_filled", 1),
        ("unseeded_instruments", 0),
        ("diff_unknown_order", 0),
        ("unattributed_decreases", 0),
        ("fills_unmatched", 0),
        ("no_execution_records", 0),
        ("block_time_gaps", 0),
        ("open_at_end", 4),
    ] {
        assert_eq!(get(&c, k), v, "{k}");
    }
    let book = replay(&ev).unwrap();
    assert_eq!(
        book.side(1, b'B', 10),
        vec![(px("100"), sz("0.3")), (px("100"), sz("0.7"))]
    );
    assert_eq!(
        book.side(1, b'A', 10),
        vec![(px("101"), sz("1.5")), (px("101.5"), sz("1"))]
    );
}

#[test]
fn line_order_does_not_matter() {
    let mut v = all_lines();
    v.reverse();
    let (a, _, _) = convert(lines(&v), &with_snapshot()).unwrap();
    let (b, _, _) = convert(lines(&all_lines()), &with_snapshot()).unwrap();
    assert_eq!(a, b);
}

#[test]
fn without_snapshot_or_fills() {
    // diffs only: no seeding (warm-up feed gap, unknown references), no fill attribution
    let diffs = read("hyperliquid_node/node_raw_book_diffs_by_block/hourly/20261011/0");
    let o = convert_full(lines(&diffs), &Config::default()).unwrap();
    let c = &o.counters;
    assert_eq!(get(c, "unseeded_instruments"), 1);
    assert_eq!(get(c, "diff_unknown_order"), 3); // 11, 12 and 21 rest from before the capture
    assert_eq!(get(c, "no_execution_records"), 1);
    assert_eq!(get(c, "removed_unclassified"), 1);
    assert_eq!(o.feed_gaps.len(), 1);
    assert!(o.feed_gaps[0].reason.contains("no L4 snapshot"));
    // the pre-snapshot block is applied when there is no snapshot
    assert_eq!(acts(&o.events)[0], ('A', 'B', px("99"), sz("1"), 99, L, 1000));
    // with the snapshot but no fills: the update is an unattributed decrease (M), the remove a plain C
    let o = convert_full(lines(&diffs), &with_snapshot()).unwrap();
    assert_eq!(get(&o.counters, "unattributed_decreases"), 2);
    let b1002: Vec<Act> = acts(
        &o.events
            .iter()
            .filter(|e| e.sequence == 1002)
            .copied()
            .collect::<Vec<_>>(),
    );
    assert_eq!(
        b1002,
        vec![
            ('C', 'B', px("100"), sz("1"), 11, L, 1002),
            ('M', 'B', px("100"), sz("0.3"), 12, L, 1002)
        ]
    );
}

fn blk(n: u64, ms: u32, events: &str) -> String {
    format!(
        r#"{{"local_time":"2026-10-11T00:00:{:02}.{:03}","block_time":"2026-10-11T00:00:{:02}.{:03}","block_number":{n},"events":[{events}]}}"#,
        ms / 1000,
        ms % 1000,
        ms / 1000,
        ms % 1000
    )
}

fn diff(oid: u64, side: &str, p: &str, d: &str) -> String {
    format!(r#"{{"user":"0x01","oid":{oid},"coin":"ETH","side":"{side}","px":"{p}","raw_book_diff":{d}}}"#)
}

fn fill(oid: u64, side: &str, p: &str, s: &str, tid: u64, crossed: bool) -> String {
    format!(
        r#"["0x01",{{"coin":"ETH","px":"{p}","sz":"{s}","side":"{side}","time":1,"oid":{oid},"crossed":{crossed},"tid":{tid},"fee":"0","hash":"0x"}}]"#
    )
}

#[test]
fn fill_attribution_edge_cases() {
    let v = vec![
        blk(1, 0, &diff(1, "Bid", "10", r#"{"new":{"sz":"2"}}"#)),
        // a fill larger than the decrease is split; the rest serves the next decrease
        blk(
            2,
            100,
            &diff(1, "Bid", "10", r#"{"update":{"origSz":"2","newSz":"1.5"}}"#),
        ),
        blk(2, 100, &fill(1, "B", "10", "1", 7, false)),
        blk(3, 200, &diff(1, "Bid", "10", "\"remove\"")),
        // a fill at another price is counted; the taker side is missing
        blk(4, 300, &diff(2, "Ask", "11", r#"{"new":{"sz":"1"}}"#)),
        blk(
            5,
            400,
            &diff(2, "Ask", "11", r#"{"update":{"origSz":"0.9","newSz":"0"}}"#),
        ),
        blk(5, 400, &fill(2, "A", "11.5", "1", 8, false)),
        // a fill nobody consumes, and a duplicate fill line
        blk(6, 9000, &fill(99, "A", "11", "1", 9, false)),
        blk(6, 9000, &fill(99, "A", "11", "1", 9, false)),
        // remove of an order whose status says filled but no fill arrived
        blk(7, 9100, &diff(3, "Bid", "9", r#"{"new":{"sz":"1"}}"#)),
        blk(8, 9200, &diff(3, "Bid", "9", "\"remove\"")),
        r#"{"time":"2026-10-11T00:00:09.200","user":"0x01","status":"filled","order":{"oid":3,"coin":"ETH"}}"#.into(),
    ];
    let (ev, _, c) = convert(lines(&v), &Config::default()).unwrap();
    for (k, n) in [
        ("fill_size_mismatch", 1),
        ("fill_price_mismatch", 1),
        ("taker_fill_missing", 3),
        ("update_orig_mismatch", 1),
        ("fills_unmatched", 1),
        ("duplicate_fills_dropped", 1),
        ("removed_filled_without_fills", 1),
        ("block_time_gaps", 1), // 400 ms -> 9100 ms between diff blocks
    ] {
        assert_eq!(get(&c, k), n, "{k}");
    }
    replay(&ev).unwrap();
    let b2: Vec<Act> = acts(&ev.iter().filter(|e| e.sequence == 2).copied().collect::<Vec<_>>());
    assert_eq!(
        b2,
        vec![
            ('T', 'A', px("10"), sz("0.5"), 0, 0, 2),
            ('F', 'B', px("10"), sz("0.5"), 1, 0, 2),
            ('C', 'B', px("10"), sz("0.5"), 1, L, 2)
        ]
    );
    let b3: Vec<Act> = acts(&ev.iter().filter(|e| e.sequence == 3).copied().collect::<Vec<_>>());
    assert_eq!(
        b3.iter().map(|a| (a.0, a.3)).collect::<Vec<_>>(),
        vec![('T', sz("0.5")), ('F', sz("0.5")), ('C', sz("0.5")), ('C', sz("1"))]
    );
    // an update to zero removes the order (fill then nothing left)
    let b5: Vec<Act> = acts(&ev.iter().filter(|e| e.sequence == 5).copied().collect::<Vec<_>>());
    assert_eq!(b5.iter().map(|a| a.0).collect::<String>(), "TFC");
    let cfg = Config {
        max_block_gap_ms: 0,
        ..Config::default()
    };
    let (_, _, c) = convert(lines(&v), &cfg).unwrap();
    assert_eq!(get(&c, "block_time_gaps"), 0);
}

#[test]
fn errors() {
    // a diff without block info has no time
    let bare = diff(1, "Bid", "10", r#"{"new":{"sz":"1"}}"#);
    let e = convert(lines(&[bare]), &Config::default()).unwrap_err();
    assert!(e.to_string().contains("--batch-by-block"), "{e}");
    for bad in [
        blk(1, 0, &diff(1, "Up", "10", r#"{"new":{"sz":"1"}}"#)),
        blk(1, 0, &diff(1, "Bid", "10", r#"{"grow":{"sz":"1"}}"#)),
        blk(1, 0, &diff(1, "Bid", "1e400x", r#"{"new":{"sz":"1"}}"#)),
        r#"{"block_time":"nope","block_number":1,"events":[]}"#.to_string(),
        blk(
            1,
            0,
            r#"["0x1",{"coin":"ETH","tid":1,"oid":1,"side":"B","px":"1","sz":"1"}]"#,
        ),
    ] {
        assert!(
            convert(lines(std::slice::from_ref(&bad)), &Config::default()).is_err(),
            "{bad}"
        );
    }
    let cfg = Config {
        snapshots: vec![(1, "{\"not\":\"an array\"}".into())],
        ..Config::default()
    };
    assert!(convert(lines(&[]), &cfg).is_err());
    assert_eq!(
        qt_crypto::input::snapshot_height_from_name(std::path::Path::new("l4_snapshot_812345678.json.gz")),
        Some(812_345_678)
    );
    assert_eq!(
        qt_crypto::input::snapshot_height_from_name(std::path::Path::new("l4.json")),
        None
    );
}
