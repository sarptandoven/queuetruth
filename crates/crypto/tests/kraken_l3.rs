//! Kraken WebSocket v2 `level3` captures: checksum (the guide's example and the independently generated fixture),
//! the depth-window model (exits, re-entries, vanished orders, re-queues) and malformed input.

mod common;

use std::collections::HashMap;

use common::{acts, fixture, get, lines, replay};
use indexmap::IndexMap;
use qt_crypto::kraken_l3::{Config, WINDOW_FLAG, convert, convert_full};
use qt_crypto::l3util::crc32;

const L: u8 = 128;

fn px(s: &str) -> i64 {
    qt_crypto::units::price(s, -9).unwrap()
}

fn sz(s: &str) -> u64 {
    qt_crypto::units::size(s, -8).unwrap()
}

fn fixture_lines() -> Vec<String> {
    qt_crypto::input::lines(&[fixture("kraken_l3/level3.jsonl")])
        .unwrap()
        .map(Result::unwrap)
        .collect()
}

fn precise() -> Config {
    Config {
        precision: HashMap::from([("BTC/USD".to_string(), (2, 8))]),
        ..Config::default()
    }
}

#[test]
fn guide_checksum_example() {
    // the concatenated asks + bids string of the L3 checksum guide hashes to the documented value
    let asks = "44939545230839344939511126144939510000044939510000004495001033492644953064537449550250000\
                449596356300004495963563000044960133807244960288967575449670314392283449785677896044979235630000";
    let bids = "449394889686994493944521000044939410000000449394142963234493942500000044939410292988449394338800\
                004493941281408604493713346877449347356300004493022273429944930210000004493025550000449302700000\
                004493021500000044928010524044919633870000449195761000044912035630000449097669000044901988982";
    assert_eq!(crc32(format!("{asks}{bids}").as_bytes()), 1_063_832_831);
}

#[test]
fn guide_snapshot_verifies_with_string_fields() {
    // the guide's snapshot sends price / qty as strings with full precision: no precision option needed
    let o = |id: &str, p: &str, q: &str| {
        format!(
            r#"{{"order_id":"{id}","limit_price":"{p}","order_qty":"{q}","timestamp":"2024-01-08T12:26:39.526146327Z"}}"#
        )
    };
    let bids: Vec<String> = [
        ("44939.4", "0.88968699"),
        ("44939.4", "0.45210000"),
        ("44939.4", "0.10000000"),
        ("44939.4", "0.14296323"),
        ("44939.4", "0.25000000"),
        ("44939.4", "0.10292988"),
        ("44939.4", "0.33880000"),
        ("44939.4", "1.28140860"),
        ("44937.1", "0.03346877"),
        ("44934.7", "0.35630000"),
        ("44930.2", "0.22734299"),
        ("44930.2", "0.01000000"),
        ("44930.2", "0.05550000"),
        ("44930.2", "0.70000000"),
        ("44930.2", "0.15000000"),
        ("44928.0", "0.00105240"),
        ("44919.6", "0.33870000"),
        ("44919.5", "0.07610000"),
        ("44912.0", "0.35630000"),
        ("44909.7", "0.06690000"),
        ("44901.9", "0.00088982"),
    ]
    .iter()
    .enumerate()
    .map(|(i, (p, q))| o(&format!("B{i}"), p, q))
    .collect();
    let asks: Vec<String> = [
        ("44939.5", "4.52308393"),
        ("44939.5", "0.00111261"),
        ("44939.5", "0.00100000"),
        ("44939.5", "0.01000000"),
        ("44950.0", "0.10334926"),
        ("44953.0", "0.00064537"),
        ("44955.0", "0.00250000"),
        ("44959.6", "0.35630000"),
        ("44959.6", "0.35630000"),
        ("44960.1", "0.00338072"),
        ("44960.2", "0.88967575"),
        ("44967.0", "3.14392283"),
        ("44978.5", "0.06778960"),
        ("44979.2", "0.35630000"),
    ]
    .iter()
    .enumerate()
    .map(|(i, (p, q))| o(&format!("A{i}"), p, q))
    .collect();
    let line = format!(
        r#"{{"channel":"level3","type":"snapshot","data":[{{"symbol":"BTC/USD","checksum":1063832831,"bids":[{}],"asks":[{}],"timestamp":"2024-01-08T12:26:45.1Z"}}]}}"#,
        bids.join(","),
        asks.join(",")
    );
    // the guide's book has 14 bid levels and 12 ask levels: subscribe at depth 100 so the snapshot is not truncated
    let cfg = Config {
        depth: Some(100),
        ..Config::default()
    };
    let (ev, _, c) = convert(lines(&[line]), &cfg).unwrap();
    assert_eq!(ev.len(), 1 + 21 + 14);
    assert_eq!((get(&c, "checksums_verified"), get(&c, "checksum_mismatches")), (1, 0));
}

#[test]
fn fixture_checksums_all_verify_with_precision() {
    let o = convert_full(lines(&fixture_lines()), &precise()).unwrap();
    let c = &o.counters;
    assert_eq!((get(c, "checksums_verified"), get(c, "checksum_mismatches")), (11, 0));
    assert!(o.feed_gaps.is_empty());
    // floats lose trailing zeros ("0.5"): without the pair precision the checksums cannot match
    let o = convert_full(lines(&fixture_lines()), &Config::default()).unwrap();
    assert!(get(&o.counters, "checksum_mismatches") > 0);
    assert!(!o.feed_gaps.is_empty());
}

#[test]
fn fixture_window_counters_and_golden_window_records() {
    let (ev, trades, c) = convert(lines(&fixture_lines()), &precise()).unwrap();
    assert!(trades.is_empty());
    for (k, v) in [
        ("snapshots", 1),
        ("window_exits", 8),
        ("window_exit_levels", 3),
        ("window_reentries", 4), // step 3: three orders; step 6: one unchanged (one changed, one vanished)
        ("window_reentries_changed", 1),
        ("window_entries", 1),
        ("out_of_view_vanished", 3),
        ("reentry_requeued", 0),
        ("add_duplicate", 0),
        ("delete_unknown", 0),
        ("modify_unknown", 0),
        ("depth_assumed", 0),
        ("window_truncated_depth", 10),
        ("no_execution_records", 1),
        ("out_of_view_at_end", 0),
    ] {
        assert_eq!(get(&c, k), v, "{k}");
    }
    // every record written by the window logic, in order
    let w: Vec<_> = acts(
        &ev.iter()
            .filter(|e| e.flags & WINDOW_FLAG != 0)
            .copied()
            .collect::<Vec<_>>(),
    );
    let ids: Vec<u64> = w.iter().map(|a| a.4).collect();
    assert_eq!(
        w.iter().map(|a| (a.0, a.1, a.2, a.3)).collect::<Vec<_>>(),
        vec![
            ('C', 'B', px("99.1"), sz("0.5"),),  // step 6: cancelled while out of view
            ('M', 'B', px("99.1"), sz("0.2")),   // step 6: amended down while out of view (keeps its place)
            ('A', 'B', px("99.1"), sz("0.6")),   // step 6: added while out of view (unseen)
            ('C', 'A', px("101.0"), sz("0.75")), // step 9: side shrank to 9 levels: 101.0 is gone
            ('C', 'A', px("101.0"), sz("0.3")),
        ]
    );
    assert_eq!(ids[0], 11);
    assert_eq!(ids[1], 12);
    // no record is written when an order only leaves the window (step 1 adds the 100.05 bid and nothing else)
    let step1: Vec<_> = acts(&ev.iter().filter(|e| e.sequence == 2).copied().collect::<Vec<_>>());
    assert_eq!(step1, vec![('A', 'B', px("100.05"), sz("1"), 26, L, 2)]);
    // step 3: the delete of 100.05 and an unchanged re-entry of 99.1: only the delete is a record
    let step3: Vec<_> = acts(&ev.iter().filter(|e| e.sequence == 4).copied().collect::<Vec<_>>());
    assert_eq!(step3, vec![('C', 'B', px("100.05"), sz("0.4"), 26, L, 4)]);
}

#[derive(serde::Deserialize)]
struct View {
    bids: Vec<(String, String, String)>,
    asks: Vec<(String, String, String)>,
}

#[test]
fn fixture_replayed_window_equals_the_venue_view() {
    let (ev, _, _) = convert(lines(&fixture_lines()), &precise()).unwrap();
    let book = replay(&ev).unwrap();
    let v: View =
        serde_json::from_str(&std::fs::read_to_string(fixture("kraken_l3/expected_view.json")).unwrap()).unwrap();
    let want = |rows: &[(String, String, String)]| -> Vec<(i64, u64)> {
        rows.iter().map(|(_, p, q)| (px(p), sz(q))).collect()
    };
    assert_eq!(book.side(1, b'B', 10), want(&v.bids));
    assert_eq!(book.side(1, b'A', 10), want(&v.asks));
}

// ---- programmatic window scenarios (depth 10, one symbol) ----

fn upd(ms: u32, bids: &[(&str, &str, &str, &str)]) -> String {
    let b: Vec<String> = bids
        .iter()
        .map(|(e, id, p, q)| {
            format!(r#"{{"event":"{e}","order_id":"{id}","limit_price":"{p}","order_qty":"{q}","timestamp":"2026-10-11T00:00:00Z"}}"#)
        })
        .collect();
    format!(
        r#"{{"channel":"level3","type":"update","data":[{{"symbol":"X/USD","bids":[{}],"asks":[],"timestamp":"2026-10-11T00:00:{:02}.{:03}Z"}}]}}"#,
        b.join(","),
        ms / 1000,
        ms % 1000
    )
}

/// Ten bid levels 100..91 (order `o<price>`, size 1) and one ask `a1` at 101.
fn ten_plus() -> Vec<String> {
    let o = |id: &str, p: &str| {
        format!(r#"{{"order_id":"{id}","limit_price":"{p}","order_qty":"1","timestamp":"2026-10-11T00:00:00Z"}}"#)
    };
    let bids: Vec<String> = (91..=100).rev().map(|p| o(&format!("o{p}"), &p.to_string())).collect();
    vec![format!(
        r#"{{"channel":"level3","type":"snapshot","data":[{{"symbol":"X/USD","bids":[{}],"asks":[{}],"timestamp":"2026-10-11T00:00:00Z"}}]}}"#,
        bids.join(","),
        o("a1", "101")
    )]
}

#[test]
fn reentry_with_unseen_order_ahead_requeues_kept_orders() {
    // depth 10: snapshot of 10 bid levels; a new best bid pushes level 91 (k1, k2) out of view; while out of view an
    // order u is placed at 91 *ahead* of k2 (impossible by price-time unless k2 was re-queued, e.g. amended up):
    // the venue re-sends [k1, u, k2] and the converter re-queues k2 behind u.
    let mut v = ten_plus();
    // replace level 91 with two orders
    v[0] = v[0].replace(
        r#"{"order_id":"o91","limit_price":"91","order_qty":"1","timestamp":"2026-10-11T00:00:00Z"}"#,
        r#"{"order_id":"k1","limit_price":"91","order_qty":"1","timestamp":"2026-10-11T00:00:00Z"},{"order_id":"k2","limit_price":"91","order_qty":"2","timestamp":"2026-10-11T00:00:00Z"}"#,
    );
    v.push(upd(1, &[("add", "n1", "100.5", "1")]));
    v.push(upd(
        2,
        &[
            ("delete", "n1", "100.5", "1"),
            ("add", "k1", "91", "1"),
            ("add", "u", "91", "3"),
            ("add", "k2", "91", "2"),
        ],
    ));
    let (ev, _, c) = convert(lines(&v), &Config::default()).unwrap();
    assert_eq!(get(&c, "window_exits"), 2);
    assert_eq!(get(&c, "reentry_requeued"), 1);
    let last: Vec<_> = acts(&ev.iter().filter(|e| e.sequence == 3).copied().collect::<Vec<_>>());
    let (k2, u) = (11, 14);
    assert_eq!(
        last.iter()
            .map(|a| (a.0, a.2, a.3, a.4, a.5 & WINDOW_FLAG != 0))
            .collect::<Vec<_>>(),
        vec![
            ('C', px("100.5"), sz("1"), 13, false),
            ('C', px("91"), sz("2"), k2, true), // k2 re-queued
            ('A', px("91"), sz("3"), u, true),
            ('A', px("91"), sz("2"), k2, true),
        ]
    );
    // the replayed level 91 is in the venue's order: k1, u, k2
    let book = replay(&ev).unwrap();
    let side = book.side(1, b'B', 10);
    assert_eq!(
        &side[side.len() - 3..],
        &[(px("91"), sz("1")), (px("91"), sz("3")), (px("91"), sz("2"))]
    );
}

#[test]
fn anomalies_are_counted_not_fatal() {
    let mut v = vec![upd(0, &[("add", "early", "1", "1")])]; // before any snapshot
    v.extend(ten_plus());
    v.push(upd(1, &[("add", "o100", "100", "1")])); // repeated add of an unchanged in-view order: harmless
    v.push(upd(1, &[("add", "o99", "99", "2")])); // add of an in-view order with other values: duplicate
    v.push(upd(2, &[("modify", "zz", "100", "1")])); // unknown
    v.push(upd(3, &[("delete", "zz", "100", "1")])); // unknown
    v.push(upd(4, &[("bogus", "o100", "100", "1")]));
    let (_, _, c) = convert(lines(&v), &Config::default()).unwrap();
    for (k, n) in [
        ("updates_before_snapshot", 1),
        ("add_repeated", 1),
        ("add_duplicate", 1),
        ("modify_unknown", 1),
        ("delete_unknown", 1),
        ("unknown_events", 1),
        ("depth_assumed", 1),
        ("checksums_missing", 6),
    ] {
        assert_eq!(get(&c, k), n, "{k}");
    }
}

#[test]
fn depth_from_ack_option_and_errors() {
    let ack = r#"{"method":"subscribe","result":{"channel":"level3","symbol":"X/USD","depth":100},"success":true}"#;
    let mut v = vec![ack.to_string()];
    v.extend(ten_plus());
    v.push(upd(1, &[("add", "n1", "100.5", "1")])); // 11 levels fit in depth 100: nothing leaves the window
    let (_, _, c) = convert(lines(&v), &Config::default()).unwrap();
    assert_eq!((get(&c, "window_exits"), get(&c, "window_truncated_depth")), (0, 100));
    // the option overrides the acknowledgement
    let cfg = Config {
        depth: Some(10),
        ..Config::default()
    };
    let (_, _, c) = convert(lines(&v), &cfg).unwrap();
    assert_eq!(get(&c, "window_exits"), 1);
    let cfg = Config {
        depth: Some(5),
        ..Config::default()
    };
    assert_eq!(convert(lines(&v), &cfg).unwrap_err().code(), "QT-CONFIG-001");
    let bad_ack = ack.replace("100", "7");
    assert!(
        convert(lines(&[bad_ack]), &Config::default())
            .unwrap_err()
            .code()
            .starts_with("QT-INPUT")
    );
    for bad in [
        r#"{"channel":"level3","type":"snapshot","data":[{"bids":[]}]}"#,
        r#"{"channel":"level3","type":"snapshot","data":[{"symbol":"X/USD","bids":[{"order_id":"a"}],"timestamp":"2026-10-11T00:00:00Z"}]}"#,
        r#"{"channel":"level3","type":"snapshot","data":[{"symbol":"X/USD","bids":[],"timestamp":"yesterday"}]}"#,
        "{",
    ] {
        assert!(convert(lines(&[bad.to_string()]), &Config::default()).is_err(), "{bad}");
    }
    let cfg = Config {
        instruments: IndexMap::from([("Y/USD".to_string(), 1)]),
        ..Config::default()
    };
    let (ev, _, _) = convert(lines(&ten_plus()), &cfg).unwrap();
    assert!(ev.is_empty());
}
