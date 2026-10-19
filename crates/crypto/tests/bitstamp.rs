//! et `python/tests/sources/test_bitstamp.py` expectations on hand-built Bitstamp streams (no network).

mod common;

use common::{Act, acts, get, lines};
use indexmap::IndexMap;
use qt_crypto::bitstamp::{Config, convert};
use qt_schema::rows::TradeKind;
use qt_types::{MboEvent, Side, UNDEF_PRICE};

const US0: i64 = 1_712_131_200_000_000;

fn cfg() -> Config {
    Config {
        instruments: IndexMap::from([("btcusd".to_string(), 1)]),
        ..Config::default()
    }
}

#[allow(clippy::too_many_arguments)]
fn order_full(
    ev: &str,
    oid: u64,
    side: u8,
    px: &str,
    amt: &str,
    us: i64,
    eid: Option<(&str, Option<&str>)>,
    recv: i64,
) -> String {
    let chain = match eid {
        Some((e, Some(p))) => format!(r#","event_id":"{e}","pre_event_id":"{p}""#),
        Some((e, None)) => format!(r#","event_id":"{e}","pre_event_id":null"#),
        None => String::new(),
    };
    format!(
        r#"{{"event":"{ev}","channel":"live_orders_btcusd","data":{{"id":{oid},"id_str":"{oid}","order_type":{side},"price_str":"{px}","amount_str":"{amt}","microtimestamp":"{}"}},"recv_ns":{recv}{chain}}}"#,
        US0 + us
    )
}

fn order(ev: &str, oid: u64, side: u8, px: &str, amt: &str, us: i64) -> String {
    order_full(ev, oid, side, px, amt, us, None, 0)
}

fn order_r(ev: &str, oid: u64, side: u8, px: &str, amt: &str, us: i64, recv: i64) -> String {
    order_full(ev, oid, side, px, amt, us, None, recv)
}

fn trade(typ: u8, px: &str, amt: &str, us: i64, buy: u64, sell: u64, recv: i64) -> String {
    format!(
        r#"{{"event":"trade","channel":"live_trades_btcusd","data":{{"id":7,"amount_str":"{amt}","price_str":"{px}","type":{typ},"microtimestamp":"{}","buy_order_id":{buy},"sell_order_id":{sell}}},"recv_ns":{recv}}}"#,
        US0 + us
    )
}

fn book(us: i64, bids: &str, asks: &str, channel: &str) -> String {
    format!(
        r#"{{"event":"data","channel":"{channel}","data":{{"bids":{bids},"asks":{asks},"microtimestamp":"{}"}},"recv_ns":0}}"#,
        US0 + us
    )
}

fn orders() -> Vec<String> {
    vec![
        order_full(
            "order_created",
            11,
            0,
            "61200.00",
            "0.50000000",
            0,
            Some(("e1", Some("e0"))),
            0,
        ),
        order_full(
            "order_created",
            12,
            1,
            "61205.00",
            "0.80000000",
            1,
            Some(("e2", Some("e1"))),
            0,
        ),
        order_full(
            "order_changed",
            11,
            0,
            "61200.00",
            "0.30000000",
            2,
            Some(("e3", Some("e2"))),
            5,
        ),
        order_full(
            "order_changed",
            12,
            1,
            "61210.00",
            "0.80000000",
            3,
            Some(("e4", Some("e3"))),
            0,
        ),
        order_full(
            "order_deleted",
            11,
            0,
            "61200.00",
            "0.30000000",
            4,
            Some(("e5", Some("e4"))),
            0,
        ),
        order_full(
            "order_deleted",
            12,
            1,
            "61210.00",
            "0.70000000",
            5,
            Some(("e6", Some("e5"))),
            0,
        ),
    ]
}

fn trades() -> Vec<String> {
    vec![trade(1, "61200.00", "0.20000000", 2, 11, 99, 1)]
}

fn run(v: Vec<String>, c: &Config) -> (Vec<MboEvent>, Vec<qt_schema::rows::TradeRow>, qt_crypto::Counters) {
    convert(lines(&v), c).unwrap()
}

fn seeds_line() -> String {
    book(
        0,
        r#"[["61200.00","1.00000000","1"]]"#,
        r#"[["61205.00","0.80000000","2"]]"#,
        "detail_order_book_btcusd",
    )
}

fn act6(rows: &[MboEvent]) -> Vec<(char, char, i64, u64, u64, u8)> {
    acts(rows).into_iter().map(|a| (a.0, a.1, a.2, a.3, a.4, a.5)).collect()
}

#[test]
fn bitstamp_orders_and_trades_to_mbo() {
    let mut v = orders();
    v.extend(trades());
    let (rows, tr, st) = run(v, &cfg());
    let want: Vec<Act> = vec![
        ('A', 'B', 61_200_000_000_000, 50_000_000, 11, 128, 1),
        ('A', 'A', 61_205_000_000_000, 80_000_000, 12, 128, 2),
        ('T', 'A', 61_200_000_000_000, 20_000_000, 99, 0, 3),
        ('F', 'B', 61_200_000_000_000, 20_000_000, 11, 128, 3),
        ('M', 'B', 61_200_000_000_000, 30_000_000, 11, 128, 4),
        ('M', 'A', 61_210_000_000_000, 80_000_000, 12, 128, 5),
        ('C', 'B', 61_200_000_000_000, 30_000_000, 11, 128, 6),
        ('C', 'A', 61_210_000_000_000, 80_000_000, 12, 128, 7),
    ];
    assert_eq!(acts(&rows), want);
    assert_eq!(rows[0].ts_event, US0 * 1000);
    assert_eq!(rows[2].ts_event, (US0 + 2) * 1000);
    assert_eq!(rows[2].ts_recv, 1);
    assert_eq!(tr.len(), 1);
    let t = tr[0];
    assert_eq!(
        (
            t.ts_event,
            t.instrument_id,
            t.price,
            t.size,
            t.aggressor_side,
            t.sequence
        ),
        ((US0 + 2) * 1000, 1, 61_200_000_000_000, 20_000_000, Side::Ask, 3)
    );
    assert_eq!((t.kind, t.maker_order_id), (TradeKind::Displayed, Some(11)));
    assert_eq!(
        (
            get(&st, "price_changes"),
            get(&st, "delete_size_mismatch"),
            get(&st, "trade_maker_unknown")
        ),
        (1, 1, 0)
    );
    assert_eq!(
        (
            get(&st, "open_at_end"),
            get(&st, "records"),
            get(&st, "changed_unknown")
        ),
        (0, 8, 0)
    );
    assert_eq!(
        (
            get(&st, "order_created"),
            get(&st, "order_changed"),
            get(&st, "order_deleted"),
            get(&st, "trade")
        ),
        (2, 2, 2, 1)
    );
}

#[test]
fn bitstamp_unknown_and_duplicate_orders() {
    let o = orders();
    let v = vec![
        order("order_changed", 11, 0, "61200.00", "0.3", 0),
        order("order_created", 11, 0, "61200.00", "0.5", 1),
        o[4].clone(),
        o[4].clone(),
    ];
    let (rows, _, st) = run(v, &cfg());
    assert_eq!(rows.iter().map(|r| r.action as char).collect::<String>(), "AMC");
    assert_eq!(
        (
            get(&st, "changed_unknown"),
            get(&st, "created_duplicate"),
            get(&st, "deleted_unknown")
        ),
        (1, 1, 1)
    );
    let (rows, _, st) = run(vec![trade(0, "1", "1", 0, 1, 2, 0)], &cfg());
    assert_eq!(get(&st, "trade_maker_unknown"), 1);
    assert_eq!(acts(&rows), vec![('T', 'B', 1_000_000_000, 100_000_000, 1, 128, 1)]);
}

#[test]
fn bitstamp_event_chain_gaps_and_restart() {
    let (_, _, st) = run(orders(), &cfg());
    assert_eq!(
        (
            get(&st, "event_id_checked"),
            get(&st, "event_id_gaps"),
            get(&st, "event_id_restarts")
        ),
        (6, 0, 0)
    );
    let o = orders();
    let broken = vec![
        o[0].clone(),
        o[1].clone(),
        order_full("order_deleted", 11, 0, "1", "1", 9, Some(("e9", Some("e8"))), 0),
        order_full("order_created", 5, 0, "1", "1", 10, Some(("e10", None)), 0),
    ];
    let (_, _, st) = run(broken, &cfg());
    assert_eq!(
        (
            get(&st, "event_id_checked"),
            get(&st, "event_id_gaps"),
            get(&st, "event_id_restarts")
        ),
        (4, 1, 1)
    );
}

#[test]
fn bitstamp_decimal_exactness() {
    let e = convert(
        lines(&[order("order_created", 1, 0, "61200.00", "0.000000015", 0)]),
        &cfg(),
    )
    .unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-004");
    let e = convert(
        lines(&[order("order_created", 1, 0, "61200.0000000001", "1", 0)]),
        &cfg(),
    )
    .unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-004");
    let (rows, _, _) = run(
        vec![order("order_created", 1, 0, "61200.12345678", "0.12345678", 0)],
        &cfg(),
    );
    assert_eq!(
        (rows[0].price, rows[0].size, rows[0].ts_event),
        (61_200_123_456_780, 12_345_678, US0 * 1000)
    );
    let e = convert(lines(&["{not json".to_string()]), &cfg()).unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002");
}

#[test]
fn bitstamp_seed_from_detail_snapshot() {
    // a plain order_book snapshot has no ids and cannot seed
    let (_, _, st) = run(
        vec![
            book(0, r#"[["1","1"]]"#, "[]", "order_book_btcusd"),
            order("order_created", 1, 0, "1", "1", 0),
        ],
        &cfg(),
    );
    assert_eq!(get(&st, "seeded_instruments"), 0);
    let snap = book(
        0,
        r#"[["61200.00","1.00000000","1500000000"]]"#,
        r#"[["61205.00","0.80000000","1500000009"]]"#,
        "detail_order_book_btcusd",
    );
    let stale = order("order_created", 7, 0, "61100.00", "1", -5);
    let mut v = vec![snap, book(9, "[]", "[]", "detail_order_book_btcusd"), stale];
    v.extend(orders());
    v.extend(trades());
    let (rows, _, st) = run(v, &cfg());
    assert_eq!(
        acts(&rows[..4]),
        vec![
            ('R', 'N', UNDEF_PRICE, 0, 0, 0, 1),
            ('A', 'B', 61_200_000_000_000, 100_000_000, 1_500_000_000, 32, 1),
            ('A', 'A', 61_205_000_000_000, 80_000_000, 1_500_000_009, 32 | 128, 1),
            ('A', 'B', 61_200_000_000_000, 50_000_000, 11, 128, 2),
        ]
    );
    assert_eq!(rows[0].ts_event, US0 * 1000);
    assert_eq!(
        (
            get(&st, "seeded_orders"),
            get(&st, "skipped_before_snapshot"),
            get(&st, "open_at_end")
        ),
        (2, 1, 2)
    );
    assert_eq!(rows.len(), 3 + 8);
}

fn taker_stream() -> Vec<String> {
    vec![
        // (a) IOC-style taker: trade, created, deleted (amount 0), then the maker's decrement, all at us 10
        trade(0, "61205.00", "0.00010000", 10, 21, 2, 1),
        order_r("order_created", 21, 0, "61205.00", "0.00010000", 10, 2),
        order_r("order_deleted", 21, 0, "61205.00", "0", 10, 3),
        order_r("order_changed", 2, 1, "61205.00", "0.79990000", 10, 4),
        // (b) sweeping taker 22 (buy 1.0 @ 61210 crosses the 61205 ask): held back until the maker is gone
        order_r("order_created", 22, 0, "61210.00", "1.00000000", 20, 1),
        trade(0, "61205.00", "0.79990000", 20, 22, 2, 2),
        order_r("order_changed", 22, 0, "61205.00", "0.20010000", 20, 3),
        order_r("order_changed", 2, 1, "61205.00", "0", 20, 4),
        order_r("order_deleted", 2, 1, "61205.00", "0", 20, 5),
        order("order_changed", 22, 0, "61210.00", "0.20010000", 30),
        order("order_deleted", 22, 0, "61210.00", "0.20010000", 40),
    ]
}

#[test]
fn bitstamp_taker_suppression_and_crossing_deferral() {
    let mut v = vec![seeds_line()];
    v.extend(taker_stream());
    let (rows, tr, st) = run(v.clone(), &cfg());
    assert_eq!(
        act6(&rows[3..]),
        vec![
            ('T', 'B', 61_205_000_000_000, 10_000, 21, 0),
            ('F', 'A', 61_205_000_000_000, 10_000, 2, 128),
            ('M', 'A', 61_205_000_000_000, 79_990_000, 2, 128),
            ('T', 'B', 61_205_000_000_000, 79_990_000, 22, 0),
            ('F', 'A', 61_205_000_000_000, 79_990_000, 2, 128),
            ('M', 'A', 61_205_000_000_000, 0, 2, 128),
            ('A', 'B', 61_205_000_000_000, 20_010_000, 22, 128),
            ('M', 'B', 61_210_000_000_000, 20_010_000, 22, 128),
            ('C', 'B', 61_210_000_000_000, 20_010_000, 22, 128),
        ]
    );
    assert_eq!(rows[9].sequence, rows[8].sequence + 1);
    assert_eq!(rows[9].ts_event, (US0 + 20) * 1000);
    assert_eq!(
        (
            tr.len(),
            get(&st, "records"),
            get(&st, "open_at_end"),
            get(&st, "pending_at_end")
        ),
        (2, 12, 1, 0)
    );
    assert_eq!(
        (
            get(&st, "taker_orders_suppressed"),
            get(&st, "deleted_suppressed"),
            get(&st, "created_crossing")
        ),
        (1, 1, 1)
    );
    assert_eq!(
        (
            get(&st, "changed_crossing"),
            get(&st, "deferred_opened"),
            get(&st, "price_changes"),
            get(&st, "suppressed_rested")
        ),
        (1, 1, 1, 0)
    );
    assert_eq!(
        (
            get(&st, "delete_size_mismatch"),
            get(&st, "deleted_unknown"),
            get(&st, "taker_window_us")
        ),
        (0, 0, 100)
    );
    // taker handling off: the same stream crosses the book
    let off = Config {
        taker_window_us: None,
        ..cfg()
    };
    let (rows0, _, st0) = run(v, &off);
    assert_eq!(
        rows0[3..].iter().map(|r| r.action as char).collect::<String>(),
        "TFACMATFFMMMC"
    );
    assert_eq!(
        (
            get(&st0, "taker_orders_suppressed"),
            get(&st0, "created_crossing"),
            get(&st0, "taker_handling")
        ),
        (0, 0, 0)
    );
    assert!(!st0.contains_key("taker_window_us"));
}

#[test]
fn bitstamp_taker_window_and_resting_crossing() {
    let quick = vec![
        seeds_line(),
        order("order_created", 31, 0, "61190.00", "1", 50),
        order("order_deleted", 31, 0, "61190.00", "1", 200),
    ];
    let (rows, _, st) = run(quick.clone(), &cfg()); // 150 us > the 100 us window: a real order
    assert_eq!(rows[3..].iter().map(|r| r.action as char).collect::<String>(), "AC");
    assert_eq!(get(&st, "taker_orders_suppressed"), 0);
    let (rows, _, st) = run(
        quick,
        &Config {
            taker_window_us: Some(200),
            ..cfg()
        },
    );
    assert_eq!(
        (
            rows.len(),
            get(&st, "taker_orders_suppressed"),
            get(&st, "deleted_suppressed")
        ),
        (3, 1, 1)
    );
    // a resting bid re-priced onto the ask: its remainder is pulled, it opens once the ask is gone
    let repr = vec![
        seeds_line(),
        order("order_changed", 1, 0, "61205.00", "1", 60),
        order("order_deleted", 2, 1, "61205.00", "0.8", 61),
    ];
    let (rows, _, st) = run(repr.clone(), &cfg());
    assert_eq!(
        act6(&rows[3..]),
        vec![
            ('C', 'B', 61_200_000_000_000, 100_000_000, 1, 128),
            ('C', 'A', 61_205_000_000_000, 80_000_000, 2, 0),
            ('A', 'B', 61_205_000_000_000, 100_000_000, 1, 128),
        ]
    );
    assert_eq!(
        (
            get(&st, "resting_crossing"),
            get(&st, "deferred_opened"),
            get(&st, "price_changes")
        ),
        (1, 1, 1)
    );
    let (_, _, st) = run(repr[..2].to_vec(), &cfg());
    assert_eq!((get(&st, "pending_at_end"), get(&st, "open_at_end")), (1, 1));
}

#[test]
fn bitstamp_deleted_unknown_root_cause() {
    let dels = vec![
        order("order_deleted", 77, 0, "61100.00", "1", 5),
        order("order_deleted", 78, 1, "61300.00", "1", 6),
        order("order_deleted", 79, 0, "61202.00", "1", 7),
    ];
    let mut v = vec![seeds_line()];
    v.extend(dels.clone());
    let (_, _, st) = run(v, &cfg());
    assert_eq!(
        (
            get(&st, "deleted_unknown"),
            get(&st, "deleted_unknown_outside_seed_range")
        ),
        (3, 2)
    );
    let (_, _, st) = run(dels[..1].to_vec(), &cfg());
    assert_eq!(
        (
            get(&st, "deleted_unknown"),
            get(&st, "deleted_unknown_outside_seed_range")
        ),
        (1, 0)
    );
}

#[test]
fn bitstamp_auto_instruments_and_tie_order() {
    // empty map: markets sorted; orders sort ahead of trades at equal (microtimestamp, recv_ns) whatever the line order
    let v = vec![
        trade(1, "61200.00", "0.1", 0, 5, 11, 0),
        order("order_created", 12, 0, "61200.00", "0.5", 0).replace("live_orders_btcusd", "live_orders_ethusd"),
        order("order_created", 11, 0, "61200.00", "0.5", 0),
    ];
    let (rows, _, _) = convert(lines(&v), &Config::default()).unwrap();
    let got: Vec<(char, u32)> = rows.iter().map(|r| (r.action as char, r.instrument_id)).collect();
    assert_eq!(got, vec![('A', 2), ('A', 1), ('T', 1), ('F', 1)]);
}
