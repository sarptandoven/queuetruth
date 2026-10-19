//! Regression tests for the independent Rust engine review's findings in `qt_crypto`:
//! M10 (Coinbase `full` + `matches` merge), M11 (incremental decompression guard) and the m27 crypto edge cases.
//! The first test of each group is the review's failing test (`coinbase_order.rs`,
//! `crypto_gz_guard.rs` of the review), ported unchanged in substance.

mod common;

use std::io::Write;

use common::{get, tmpdir};
use indexmap::IndexMap;
use qt_crypto::bitfinex::{self, CHUNK_ID_BASE, CHUNK_ID_WIDE_BASE, MAX_CHILDREN, MIN_MAX_SIZE, child_id};
use qt_crypto::coinbase::{Config, convert};
use qt_crypto::input::{FileLines, inflate_limit};
use qt_crypto::units::dec_units;
use qt_types::MboEvent;

const T0: i64 = 1_792_000_000_000_000_000;

fn cfg() -> Config {
    Config {
        instruments: IndexMap::from([("BTC-USD".to_string(), 1)]),
        ..Config::default()
    }
}

fn msg_p(product: &str, ty: &str, seq: i64, t: &str, kw: &str) -> String {
    format!(
        r#"{{"type":"{ty}","product_id":"{product}","time":"2026-10-11T00:00:{t}Z","recv_ns":{},"sequence":{seq}{kw}}}"#,
        T0 + seq
    )
}

fn msg(ty: &str, seq: i64, t: &str, kw: &str) -> String {
    msg_p("BTC-USD", ty, seq, t, kw)
}

fn book_net(ev: &[MboEvent], id: u64) -> i128 {
    ev.iter()
        .filter(|e| e.order_id == id)
        .map(|e| match e.action {
            b'A' => i128::from(e.size),
            b'C' => -i128::from(e.size),
            _ => 0,
        })
        .sum()
}

fn ok_lines(v: Vec<String>) -> impl Iterator<Item = qt_types::QtResult<String>> {
    v.into_iter().map(Ok)
}

// ---- m27: Coinbase `done` removes the whole tracked order ------------------------------------------------------

/// Review test: after a missed `match` (sequence gap), `done reason=canceled` must cancel the tracked remainder, not
/// the message's `remaining_size`, or the order keeps zombie size.
#[test]
fn done_canceled_removes_the_whole_tracked_order() {
    let lines = vec![
        msg(
            "open",
            1,
            "01.000000",
            r#","order_id":"o1","side":"buy","price":"100.00","remaining_size":"5""#,
        ),
        // seq 2 (a match of 2) is missing from the capture
        msg(
            "done",
            3,
            "02.000000",
            r#","order_id":"o1","side":"buy","price":"100.00","reason":"canceled","remaining_size":"3""#,
        ),
    ];
    let (ev, _, counters) = convert(ok_lines(lines), &cfg()).unwrap();
    let oid = ev.iter().find(|e| e.action == b'A').unwrap().order_id;
    assert_eq!(book_net(&ev, oid), 0, "order left resting after done: {ev:?}");
    let got: Vec<(char, u64)> = ev.iter().map(|e| (e.action as char, e.size)).collect();
    assert_eq!(got, vec![('A', 500_000_000), ('C', 500_000_000)]);
    assert_eq!((get(&counters, "cancel_size_mismatch"), get(&counters, "gaps")), (1, 1));
}

#[test]
fn done_filled_with_a_tracked_remainder_removes_it() {
    let lines = vec![
        msg(
            "open",
            1,
            "01",
            r#","order_id":"o1","side":"sell","price":"100.00","remaining_size":"5""#,
        ),
        // seq 2 (a match of 5) is missing
        msg(
            "done",
            3,
            "02",
            r#","order_id":"o1","side":"sell","price":"100.00","reason":"filled","remaining_size":"0""#,
        ),
    ];
    let (ev, _, counters) = convert(ok_lines(lines), &cfg()).unwrap();
    let got: Vec<(char, char, u64, u8)> = ev
        .iter()
        .map(|e| (e.action as char, e.side as char, e.size, e.flags))
        .collect();
    assert_eq!(
        got,
        vec![
            ('A', 'A', 500_000_000, qt_types::flags::LAST),
            ('C', 'A', 500_000_000, qt_types::flags::LAST)
        ]
    );
    assert_eq!(
        (get(&counters, "done_filled_nonzero"), get(&counters, "open_at_end")),
        (1, 0)
    );
}

// ---- M10: `full` + `matches` merged by (product, sequence) -----------------------------------------------------

/// Review test: a `matches.jsonl` line that fills a gap of `full.jsonl` is applied in sequence order, before its
/// maker's `done`.
#[test]
fn gap_filling_match_is_processed_in_sequence_order() {
    let lines = vec![
        // full.jsonl
        msg(
            "open",
            1,
            "01.000000",
            r#","order_id":"o1","side":"sell","price":"100.00","remaining_size":"2""#,
        ),
        msg(
            "done",
            3,
            "03.000000",
            r#","order_id":"o1","side":"sell","price":"100.00","reason":"filled","remaining_size":"0""#,
        ),
        // matches.jsonl (seq 2 missing from full)
        msg(
            "match",
            2,
            "02.000000",
            r#","maker_order_id":"o1","taker_order_id":"t1","side":"sell","price":"100.00","size":"2""#,
        ),
    ];
    let (ev, tr, counters) = convert(ok_lines(lines), &cfg()).unwrap();
    assert_eq!(get(&counters, "match_unknown_maker"), 0);
    let got: Vec<(char, u64, u64)> = ev.iter().map(|e| (e.action as char, e.size, e.sequence)).collect();
    assert_eq!(
        got,
        vec![
            ('A', 200_000_000, 1),
            ('T', 200_000_000, 2),
            ('F', 200_000_000, 2),
            ('C', 200_000_000, 2)
        ]
    );
    let maker = ev[0].order_id;
    assert_eq!(book_net(&ev, maker), 0);
    assert_eq!(tr[0].maker_order_id, Some(maker));
    assert_eq!(
        (
            get(&counters, "done_filled_nonzero"),
            get(&counters, "gaps"),
            get(&counters, "checked")
        ),
        (0, 0, 3)
    );
}

/// The same capture as files in a directory (read as `full` then `matches`): no sequence gap and no feed gap; a hole
/// left after the merge is one gap whose feed gap runs forward in time.
#[test]
fn capture_dir_full_and_matches_merge_before_gaps() {
    let d = tmpdir("review_m10");
    let full = [
        msg(
            "open",
            1,
            "01",
            r#","order_id":"o1","side":"sell","price":"100.00","remaining_size":"2""#,
        ),
        msg(
            "done",
            3,
            "03",
            r#","order_id":"o1","side":"sell","price":"100.00","reason":"filled","remaining_size":"0""#,
        ),
        msg(
            "open",
            6,
            "06",
            r#","order_id":"o2","side":"buy","price":"99.00","remaining_size":"1""#,
        ),
    ];
    let matches = [msg(
        "match",
        2,
        "02",
        r#","maker_order_id":"o1","taker_order_id":"t1","side":"sell","price":"100.00","size":"2""#,
    )];
    std::fs::write(d.join("full.jsonl"), full.join("\n") + "\n").unwrap();
    std::fs::write(d.join("matches.jsonl"), matches.join("\n") + "\n").unwrap();
    let spec: qt_schema::specs::IngestSpec = serde_json::from_value(serde_json::json!({
        "format": "coinbase",
        "venue": "coinbase",
        "inputs": [{"path": d.display().to_string()}],
    }))
    .unwrap();
    let p = qt_crypto::prepare(&spec).unwrap();
    assert_eq!(p.counters["match_unknown_maker"], 0);
    // seq 4 and 5 are really missing: one gap (3 -> 6), not a second one for the late match
    assert_eq!((p.counters["gaps"], p.data_quality.sequence_gaps), (1, 1));
    let g = &p.data_quality.feed_gaps;
    assert_eq!(g.len(), 1, "{g:?}");
    assert!(
        g[0].ts_start <= g[0].ts_end && g[0].reason == "sequence gap 3 -> 6",
        "{g:?}"
    );
    std::fs::remove_dir_all(&d).unwrap();
}

/// Two products interleaved in `full`, each with a gap filled from `matches`: the result equals the fully in-order
/// capture, including the integer order ids (cross-product interleaving is kept).
#[test]
fn merge_keeps_product_interleaving_and_ids() {
    let open = |p: &str, seq: i64, oid: &str| {
        msg_p(
            p,
            "open",
            seq,
            &format!("{:02}", seq),
            &format!(r#","order_id":"{oid}","side":"sell","price":"100.00","remaining_size":"2""#),
        )
    };
    let mtch = |p: &str, seq: i64, maker: &str, taker: &str| {
        msg_p(
            p,
            "match",
            seq,
            &format!("{:02}", seq),
            &format!(
                r#","maker_order_id":"{maker}","taker_order_id":"{taker}","side":"sell","price":"100.00","size":"1""#
            ),
        )
    };
    let done = |p: &str, seq: i64, oid: &str| {
        msg_p(
            p,
            "done",
            seq,
            &format!("{:02}", seq),
            &format!(r#","order_id":"{oid}","side":"sell","price":"100.00","reason":"canceled","remaining_size":"1""#),
        )
    };
    let in_order = vec![
        open("BTC-USD", 1, "b1"),
        open("ETH-USD", 1, "e1"),
        mtch("BTC-USD", 2, "b1", "bt"),
        mtch("ETH-USD", 2, "e1", "et"),
        done("BTC-USD", 3, "b1"),
        done("ETH-USD", 3, "e1"),
    ];
    let split = vec![
        in_order[0].clone(),
        in_order[1].clone(),
        in_order[4].clone(),
        in_order[5].clone(),
        in_order[2].clone(), // matches.jsonl
        in_order[3].clone(),
    ];
    let cfg2 = Config {
        instruments: IndexMap::from([("BTC-USD".to_string(), 1), ("ETH-USD".to_string(), 2)]),
        ..Config::default()
    };
    let a = convert(ok_lines(in_order), &cfg2).unwrap();
    let b = convert(ok_lines(split), &cfg2).unwrap();
    assert_eq!((&a.0, &a.1), (&b.0, &b.1));
    assert_eq!(
        (
            get(&b.2, "match_unknown_maker"),
            get(&b.2, "gaps"),
            get(&b.2, "open_at_end")
        ),
        (0, 0, 0)
    );
}

// ---- M11: decompression guard on the inflating stream -----------------------------------------------------------

/// Review test: a gzip bomb without newlines is refused once about `200 x compressed` bytes were inflated, not after
/// the whole 64 MiB line was buffered.
#[test]
fn gzip_bomb_line_is_stopped_before_it_is_buffered() {
    let dir = tmpdir("review_gz");
    let p = dir.join("bomb.jsonl.gz");
    let mut e = flate2::write::GzEncoder::new(std::fs::File::create(&p).unwrap(), flate2::Compression::best());
    let chunk = vec![b'a'; 1 << 20];
    for _ in 0..64 {
        e.write_all(&chunk).unwrap(); // 64 MiB, one line, no newline
    }
    e.finish().unwrap();
    let compressed = std::fs::metadata(&p).unwrap().len();
    let mut lines = FileLines::open(&p).unwrap();
    let r = lines.next().unwrap();
    assert!(lines.next().is_none());
    std::fs::remove_dir_all(&dir).unwrap();
    let err = match r {
        Ok(s) => panic!("Ok(line of {} bytes) from {compressed} compressed bytes", s.len()),
        Err(e) => e,
    };
    let text = err.to_string();
    assert_eq!(err.code(), "QT-INPUT-002");
    assert!(
        text.contains("decompression guard") && !text.contains("67108864 bytes inflated"),
        "{text}"
    );
    // the guard trips within one read chunk (1 MiB) of the limit
    let inflated: u64 = text
        .split("decompression guard: ")
        .nth(1)
        .and_then(|t| t.split(' ').next())
        .and_then(|n| n.parse().ok())
        .unwrap_or_else(|| panic!("no inflated count in {text}"));
    let limit = inflate_limit(compressed);
    assert!(limit < 64 << 20, "fixture too weakly compressed: {compressed} bytes");
    assert!(
        inflated > limit && inflated <= limit + (1 << 20),
        "{inflated} vs limit {limit}"
    );
}

// ---- m27: Bitfinex child ids and decimal exponents ---------------------------------------------------------------

#[test]
fn bitfinex_child_ids_are_distinct_past_sixteen_children() {
    // et's ids for the first 16 children are kept
    assert_eq!(child_id(7, 1).unwrap(), CHUNK_ID_BASE + 7 * 16 + 1);
    assert_eq!(child_id(7, 15).unwrap(), CHUNK_ID_BASE + 7 * 16 + 15);
    // et's formula repeats from k = 16: child_id(oid, k) == child_id(oid + 1, k - 16)
    assert_ne!(child_id(7, 17).unwrap(), child_id(8, 1).unwrap());
    assert_eq!(child_id(7, 16).unwrap(), CHUNK_ID_WIDE_BASE + 7 * MAX_CHILDREN + 16);
    let mut seen = std::collections::HashSet::new();
    for oid in [0u64, 1, 2, 3, 1 << 39, (1 << 40) - 1] {
        for k in (0..40).chain([MAX_CHILDREN as usize - 1]) {
            assert!(seen.insert(child_id(oid, k).unwrap()), "duplicate id for ({oid}, {k})");
        }
    }
    assert!(child_id(7, MAX_CHILDREN as usize).is_err());
    assert!(child_id(u64::MAX / 2, 1).is_err());
}

#[test]
fn bitfinex_many_children_convert_without_collisions_and_are_bounded() {
    let line = |payload: &str, n: i64| {
        format!(
            r#"{{"recv_ns":{},"chan":"book_R0","symbol":"tBTCUSD","msg":[1,{payload}]}}"#,
            T0 + n
        )
    };
    let cfg = bitfinex::Config {
        instruments: IndexMap::from([("tBTCUSD".to_string(), 1)]),
        max_size: MIN_MAX_SIZE,
        ..Default::default()
    };
    // two adjacent order ids, 0.2 BTC each = 2e7 units = 20 children of 2^20
    let snap = line("[[7,61200.5,0.2],[8,61201,-0.2]]", 0);
    let (rows, _, st) = bitfinex::convert(std::iter::once(Ok(snap)), &cfg).unwrap();
    let adds: Vec<&MboEvent> = rows.iter().filter(|r| r.action == b'A').collect();
    assert_eq!(adds.len(), 40);
    let ids: std::collections::HashSet<u64> = adds.iter().map(|r| r.order_id).collect();
    assert_eq!(ids.len(), 40, "child ids collide");
    assert_eq!(adds.iter().map(|r| r.size).sum::<u64>(), 40_000_000);
    assert_eq!(get(&st, "chunked_orders"), 2);
    // an order needing more than MAX_CHILDREN children is refused before anything is allocated
    let huge = line("[[9,61200.5,11000]]", 1);
    let e = bitfinex::convert(std::iter::once(Ok(huge)), &cfg).unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002");
}

#[test]
fn decimal_exponent_at_i32_min_does_not_overflow() {
    assert!(dec_units("1e-2147483648", 0).is_err());
    assert_eq!(dec_units("0e-2147483648", 0), Ok(0));
    assert!(dec_units("1e2147483647", -1).is_err());
}
