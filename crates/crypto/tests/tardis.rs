//! Tardis `incremental_book_L2` + `trades` CSV -> `qt.l2` rows: grouping by `local_timestamp`, pre-snapshot skip,
//! snapshot -> clear + snapshot rows, amount 0 removes, trade side -> aggressor (`unknown` -> `N`).

mod common;

use common::{fixture, get, lines};
use indexmap::IndexMap;
use qt_crypto::tardis::{Config, convert};
use qt_schema::rows::{L2Kind, L2Row, l2flags};
use qt_types::Side;

const B: i64 = 1_700_000_000_000_000; // microseconds
const PX: i64 = 1_000_000_000; // 1.0 at 1e-9
const SZ: u64 = 100_000_000; // 1.0 at 1e-8

fn ns(us: i64) -> i64 {
    (B + us) * 1000
}

fn read(name: &str) -> Vec<String> {
    qt_crypto::input::lines(&[fixture(&format!("tardis/{name}"))])
        .unwrap()
        .map(Result::unwrap)
        .collect()
}

fn both() -> Vec<String> {
    let mut v = read("incremental_book_L2.csv");
    v.extend(read("trades.csv"));
    v
}

type Brief = (u64, L2Kind, Side, Option<i64>, u64, u64, u64, Side, u8, u8, u64);

fn brief(r: &L2Row) -> Brief {
    (
        r.event_id,
        r.kind,
        r.side,
        r.price,
        r.prev_qty,
        r.new_qty,
        r.trade_qty,
        r.aggressor,
        r.level,
        r.flags,
        r.sequence,
    )
}

#[test]
fn tardis_grouping_snapshot_removal_and_trades() {
    let (rows, trades, st) = convert(lines(&both()), &Config::default()).unwrap();
    let (d, t, s, c) = (L2Kind::Depth, L2Kind::Trade, L2Kind::Snapshot, L2Kind::Clear);
    let (bid, ask, none) = (Side::Bid, Side::Ask, Side::None);
    let (snap, last) = (l2flags::SNAPSHOT, l2flags::LAST_IN_BATCH);
    let want: Vec<Brief> = vec![
        // lts 1000: the first snapshot (the pre-snapshot row at 900 is skipped)
        (1, c, none, None, 0, 0, 0, none, 255, 0, 5),
        (1, s, bid, Some(100 * PX), 0, 3 * SZ / 2, 0, none, 0, snap, 5),
        (1, s, bid, Some(99 * PX + PX / 2), 0, 2 * SZ, 0, none, 1, snap, 5),
        (1, s, ask, Some(100 * PX + PX / 2), 0, SZ, 0, none, 0, snap, 5),
        (1, s, ask, Some(101 * PX), 0, 3 * SZ, 0, none, 1, snap | last, 5),
        // lts 2000: the trade first, then the net level changes (removing absent 102 changes nothing)
        (2, t, bid, Some(100 * PX), 0, 0, SZ / 2, ask, 255, 0, 6),
        (2, d, bid, Some(100 * PX), 3 * SZ / 2, SZ, 0, none, 0, 0, 9),
        (2, d, ask, Some(100 * PX + PX / 2), SZ, 0, 0, none, 255, last, 9),
        (3, t, ask, Some(101 * PX), 0, 0, SZ / 10, bid, 255, last, 10),
        (4, t, none, Some(101 * PX), 0, 0, SZ / 5, none, 255, last, 11),
        // lts 3000 sets 100.0 to its current size: no row, no batch
        // lts 4000: a new snapshot run clears and rebuilds the book
        (5, c, none, None, 0, 0, 0, none, 255, 0, 14),
        (5, s, bid, Some(99 * PX), 0, SZ, 0, none, 0, snap, 14),
        (5, s, ask, Some(100 * PX), 0, 2 * SZ, 0, none, 0, snap | last, 14),
    ];
    assert_eq!(rows.iter().map(brief).collect::<Vec<_>>(), want);
    assert!(
        rows.iter()
            .all(|r| r.instrument_id == 1 && r.prev_ct.is_none() && r.new_ct.is_none())
    );
    assert_eq!((rows[0].ts_event, rows[0].ts_recv), (ns(999), ns(1000)));
    assert_eq!(
        (rows[5].ts_event, rows[5].ts_recv, rows[6].ts_event),
        (ns(1999), ns(2000), ns(1998))
    );
    let tr: Vec<(i64, Side, u64, u64)> = trades
        .iter()
        .map(|t| (t.ts_event, t.aggressor_side, t.size, t.sequence))
        .collect();
    assert_eq!(
        tr,
        vec![
            (ns(1999), ask, SZ / 2, 6),
            (ns(2499), bid, SZ / 10, 10),
            (ns(2599), none, SZ / 5, 11)
        ]
    );
    assert_eq!(
        (
            get(&st, "pre_snapshot_rows_skipped"),
            get(&st, "remove_absent_level"),
            get(&st, "snapshots")
        ),
        (1, 1, 2)
    );
    assert_eq!(
        (
            get(&st, "unknown_side_trades"),
            get(&st, "batches"),
            get(&st, "book_rows"),
            get(&st, "trade_rows")
        ),
        (1, 5, 11, 3)
    );
    assert_eq!(
        (
            get(&st, "crossed_after_batch"),
            get(&st, "local_ts_regressions"),
            get(&st, "l2_rows")
        ),
        (0, 0, 13)
    );
}

#[test]
fn tardis_file_order_does_not_matter() {
    let mut v = read("trades.csv");
    v.extend(read("incremental_book_L2.csv"));
    assert_eq!(
        convert(lines(&v), &Config::default()).unwrap().0,
        convert(lines(&both()), &Config::default()).unwrap().0
    );
}

#[test]
fn tardis_symbols_map_filter_and_errors() {
    let mut v = both();
    let eth: Vec<String> = read("incremental_book_L2.csv")
        .into_iter()
        .skip(1)
        .map(|l| l.replace("BTCUSDT", "ETHUSDT"))
        .collect();
    v.push("exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount".into());
    v.extend(eth);
    let (rows, _, _) = convert(lines(&v), &Config::default()).unwrap();
    // auto map: sorted symbols -> BTCUSDT 1, ETHUSDT 2; batches interleave by local_timestamp
    assert_eq!(rows.iter().filter(|r| r.instrument_id == 2).count(), 10);
    assert_eq!(
        rows.iter().map(|r| r.instrument_id).take(6).collect::<Vec<_>>(),
        vec![1, 1, 1, 1, 1, 2]
    );
    let only_eth = Config {
        instruments: IndexMap::from([("ETHUSDT".to_string(), 7)]),
        ..Config::default()
    };
    let (rows, trades, _) = convert(lines(&v), &only_eth).unwrap();
    assert!(rows.iter().all(|r| r.instrument_id == 7) && trades.is_empty() && rows.len() == 10);
    let e = convert(
        lines(&["binance,BTCUSDT,1,1,true,bid,1,1".to_string()]),
        &Config::default(),
    )
    .unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002");
    let bad = vec![
        "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount".into(),
        "x,S,1,1,maybe,bid,1,1".into(),
    ];
    assert_eq!(
        convert(lines(&bad), &Config::default()).unwrap_err().code(),
        "QT-INPUT-002"
    );
    let inexact = vec![
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount".into(),
        "x,S,1,1,7,buy,1,0.000000001".into(),
    ];
    assert_eq!(
        convert(lines(&inexact), &Config::default()).unwrap_err().code(),
        "QT-INPUT-004"
    );
}
