//! et `python/tests/sources/test_coinbase.py` expectations on hand-built Coinbase `full` / `level2` streams.

mod common;

use common::{acts, get, lines};
use indexmap::IndexMap;
use qt_crypto::coinbase::{Config, convert};
use qt_crypto::units::iso_ns;
use qt_types::Side;

const T0: i64 = 1_792_000_000_000_000_000; // recv_ns base

fn cfg(depth: usize) -> Config {
    Config {
        instruments: IndexMap::from([("BTC-USD".to_string(), 1)]),
        warmup_depth: depth,
        ..Config::default()
    }
}

fn time(t: &str) -> String {
    format!("2026-10-11T00:00:{t}Z")
}

fn msg(ty: &str, seq: Option<i64>, t: &str, kw: &str) -> String {
    let s = seq.map(|q| format!(r#","sequence":{q}"#)).unwrap_or_default();
    format!(
        r#"{{"type":"{ty}","product_id":"BTC-USD","time":"{}","recv_ns":{}{s}{kw}}}"#,
        time(t),
        T0 + seq.unwrap_or(0)
    )
}

fn mtch(seq: i64, t: &str, maker: &str, taker: &str, side: &str, price: &str, size: &str) -> String {
    msg(
        "match",
        Some(seq),
        t,
        &format!(
            r#","maker_order_id":"{maker}","taker_order_id":"{taker}","side":"{side}","price":"{price}","size":"{size}""#
        ),
    )
}

fn full_stream(gap: bool) -> Vec<String> {
    let g = i64::from(gap);
    vec![
        msg(
            "received",
            Some(10),
            "01.000000",
            r#","order_id":"o1","order_type":"limit","side":"buy","price":"100.00","size":"0.5""#,
        ),
        msg(
            "open",
            Some(11),
            "01.000001",
            r#","order_id":"o1","side":"buy","price":"100.00","remaining_size":"0.5""#,
        ),
        msg(
            "received",
            Some(12),
            "01.000000",
            r#","order_id":"m1","order_type":"market","side":"sell","funds":"30""#,
        ),
        mtch(13, "01.000002", "o1", "m1", "buy", "100.00", "0.2"),
        msg(
            "done",
            Some(14),
            "01.000000",
            r#","order_id":"m1","reason":"filled","side":"sell","remaining_size":"0""#,
        ),
        msg(
            "change",
            Some(15),
            "02",
            r#","order_id":"o1","reason":"modify_order","side":"buy","old_size":"0.3","new_size":"0.25""#,
        ),
        msg(
            "open",
            Some(16),
            "02.5",
            r#","order_id":"o2","side":"sell","price":"100.50","remaining_size":"0.1""#,
        ),
        mtch(17 + g, "03", "o2", "t2", "sell", "100.50", "0.1"),
        msg(
            "done",
            Some(18 + g),
            "03",
            r#","order_id":"o2","reason":"filled","side":"sell","remaining_size":"0""#,
        ),
        msg(
            "done",
            Some(19 + g),
            "04",
            r#","order_id":"o1","reason":"canceled","side":"buy","price":"100.00","remaining_size":"0.25""#,
        ),
    ]
}

fn l2_stream() -> Vec<String> {
    let snap_ns = iso_ns("2026-10-11T00:00:00Z").unwrap();
    vec![
        format!(
            r#"{{"type":"snapshot","product_id":"BTC-USD","recv_ns":{snap_ns},"bids":[["100.00","0.5"],["99.99","1"],["99.98","2"]],"asks":[["100.01","0.25"],["100.02","3"]]}}"#
        ),
        msg(
            "l2update",
            None,
            "01.000002",
            r#","changes":[["buy","100.00","0.3"],["sell","100.01","0"]]"#,
        ),
        msg(
            "l2update",
            None,
            "03",
            r#","changes":[["buy","100.00","0"],["buy","99.99","0"],["buy","99.98","0"]]"#,
        ),
    ]
}

fn extra() -> String {
    msg(
        "l2update",
        None,
        "04",
        r#","changes":[["sell","100.02","0"],["sell","100.01","0.1"]]"#,
    )
}

#[test]
fn coinbase_full_to_mbo_actions_and_flags() {
    let (rows, tr, st) = convert(lines(&full_stream(false)), &cfg(10)).unwrap();
    let got: Vec<(char, char, u64, u8, u64)> = acts(&rows).into_iter().map(|a| (a.0, a.1, a.3, a.5, a.6)).collect();
    assert_eq!(
        got,
        vec![
            ('A', 'B', 50_000_000, 128, 11),
            ('T', 'A', 20_000_000, 0, 13),
            ('F', 'B', 20_000_000, 0, 13),
            ('C', 'B', 20_000_000, 128, 13),
            ('M', 'B', 25_000_000, 128, 15),
            ('A', 'A', 10_000_000, 128, 16),
            ('T', 'B', 10_000_000, 0, 17),
            ('F', 'A', 10_000_000, 0, 17),
            ('C', 'A', 10_000_000, 128, 17),
            ('C', 'B', 25_000_000, 128, 19),
        ]
    );
    assert!(rows[..5].iter().all(|r| r.price == 100_000_000_000) && rows[5].price == 100_500_000_000);
    assert_eq!(
        (rows[0].ts_event, rows[0].ts_recv),
        (iso_ns(&time("01.000001")).unwrap(), T0 + 11)
    );
    let (o1, m1) = (rows[0].order_id, rows[1].order_id);
    assert!(o1 != m1 && rows[2].order_id == o1 && rows[4].order_id == o1);
    assert_eq!(
        (
            get(&st, "market_orders"),
            get(&st, "done_never_opened"),
            get(&st, "done_unknown")
        ),
        (1, 1, 0)
    );
    assert_eq!(
        (
            get(&st, "done_filled"),
            get(&st, "done_filled_nonzero"),
            get(&st, "cancel_size_mismatch")
        ),
        (1, 0, 0)
    );
    assert_eq!(
        (
            get(&st, "open_at_end"),
            get(&st, "gaps"),
            get(&st, "records"),
            get(&st, "checked")
        ),
        (0, 0, 10, 10)
    );
    // source trades: one per match, maker known
    assert_eq!(tr.len(), 2);
    assert_eq!(
        (tr[0].aggressor_side, tr[0].maker_order_id, tr[0].sequence),
        (Side::Ask, Some(o1), 13)
    );
    assert_eq!(
        (tr[1].aggressor_side, tr[1].size, tr[1].price),
        (Side::Bid, 10_000_000, 100_500_000_000)
    );
}

#[test]
fn coinbase_sequence_gap_and_anomalies() {
    let (_, _, st) = convert(lines(&full_stream(true)), &cfg(10)).unwrap();
    assert_eq!(get(&st, "gaps"), 1);
    let mut bad = full_stream(false)[..2].to_vec();
    bad.push(msg(
        "done",
        Some(12),
        "01.000000",
        r#","order_id":"o1","reason":"filled","side":"buy","remaining_size":"0""#,
    ));
    let (_, _, st) = convert(lines(&bad), &cfg(10)).unwrap();
    assert_eq!(get(&st, "done_filled_nonzero"), 1); // filled while we still track 0.5 remaining
    let ghost = vec![msg(
        "done",
        Some(5),
        "01.000000",
        r#","order_id":"ghost","reason":"canceled","side":"buy","remaining_size":"1""#,
    )];
    let (rows, _, st) = convert(lines(&ghost), &cfg(10)).unwrap();
    assert_eq!((get(&st, "done_unknown"), get(&st, "records"), rows.len()), (1, 0, 0));
    // match against an unknown maker: T alone, carrying LAST
    let (rows, tr, st) = convert(lines(&[mtch(3, "01", "x", "y", "sell", "1", "1")]), &cfg(10)).unwrap();
    assert_eq!(acts(&rows), vec![('T', 'B', 1_000_000_000, 100_000_000, 1, 128, 3)]);
    assert_eq!((get(&st, "match_unknown_maker"), tr[0].maker_order_id), (1, None));
}

#[test]
fn coinbase_duplicate_matches_and_ignored_types() {
    // full.jsonl and matches.jsonl both carry a match: kept once; last_match and heartbeats are ignored
    let mut v = full_stream(false);
    v.push(full_stream(false)[3].clone());
    v.push(r#"{"type":"last_match","product_id":"BTC-USD","sequence":5,"time":"2026-10-11T00:00:00Z","side":"buy","price":"1","size":"1","maker_order_id":"a","taker_order_id":"b"}"#.into());
    v.push(r#"{"type":"heartbeat","product_id":"BTC-USD","sequence":1}"#.into());
    let (rows, tr, st) = convert(lines(&v), &cfg(10)).unwrap();
    assert_eq!(
        (
            rows.len(),
            tr.len(),
            get(&st, "duplicates_dropped"),
            get(&st, "ignored")
        ),
        (10, 2, 1, 2)
    );
}

#[test]
fn coinbase_level2_warmup() {
    let mut v = full_stream(false);
    v.extend(l2_stream());
    v.push(extra());
    let (_, _, st) = convert(lines(&v), &cfg(2)).unwrap();
    // only the extra row is warm (every top-2 level born in-window); it aligns to the cancel at 04 (seq 19)
    assert_eq!(
        (
            get(&st, "warm_rows"),
            get(&st, "warmup_events"),
            get(&st, "warmup_measured")
        ),
        (1, 9, 1)
    );
    assert_eq!(get(&st, "l2_rows_before_first_event"), 1); // the snapshot precedes every full record
    // without the extra row 100.02 (a snapshot level) is still resting: never warm
    let mut v = full_stream(false);
    v.extend(l2_stream());
    let (_, _, st) = convert(lines(&v), &cfg(2)).unwrap();
    assert_eq!(
        (
            get(&st, "warm_rows"),
            get(&st, "warmup_events"),
            get(&st, "never_warm_instruments")
        ),
        (0, 10, 1)
    );
    // no level2 data: warm-up not measured
    let (_, _, st) = convert(lines(&full_stream(false)), &cfg(2)).unwrap();
    assert_eq!((get(&st, "warmup_measured"), get(&st, "warmup_events")), (0, 0));
}

#[test]
fn coinbase_decimal_and_time_exactness() {
    let bad_px = vec![msg(
        "open",
        Some(1),
        "01",
        r#","order_id":"a","side":"buy","price":"1.0000000005","remaining_size":"1""#,
    )];
    assert_eq!(convert(lines(&bad_px), &cfg(10)).unwrap_err().code(), "QT-INPUT-004");
    let bad_sz = vec![msg(
        "open",
        Some(1),
        "01",
        r#","order_id":"a","side":"buy","price":"1","remaining_size":"0.000000015""#,
    )];
    assert_eq!(convert(lines(&bad_sz), &cfg(10)).unwrap_err().code(), "QT-INPUT-004");
    assert_eq!(
        iso_ns("2014-11-07T08:19:27.028459Z").unwrap(),
        1_415_348_367_028_459_000
    );
    assert_eq!(iso_ns("2014-11-07T08:19:27Z").unwrap(), 1_415_348_367_000_000_000);
}
