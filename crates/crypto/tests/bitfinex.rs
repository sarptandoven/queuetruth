//! et `python/tests/sources/test_bitfinex.py` expectations on hand-built Bitfinex R0 / P0 / trades frames.

mod common;

use common::{Act, acts, get, lines};
use indexmap::IndexMap;
use qt_crypto::bitfinex::{CHUNK_ID_BASE, Config, SEQ_ALL, TIMESTAMP, child_id, chunk_sizes, convert, split_frame};
use qt_crypto::jsonx::{J, parse};
use qt_types::{Side, UNDEF_PRICE};

const T0: i64 = 1_712_131_200_000; // ms
const FLAGS: u64 = TIMESTAMP | SEQ_ALL;
const U32: u64 = u32::MAX as u64;

fn line(chan: &str, text: &str, recv: i64) -> String {
    format!(r#"{{"recv_ns":{recv},"chan":"{chan}","symbol":"tBTCUSD","msg":{text}}}"#)
}

fn cfg(flags: u64) -> Config {
    Config {
        instruments: IndexMap::from([("tBTCUSD".to_string(), 1)]),
        flags,
        ..Config::default()
    }
}

fn r0() -> Vec<String> {
    vec![
        line(
            "book_R0",
            &format!("[1,[[101,61200.5,0.5],[102,61205,-0.8]],1,{T0}]"),
            1,
        ),
        line("book_R0", &format!("[1,[103,61200.5,0.25],3,{}]", T0 + 1), 3),
        line(
            "trades",
            &format!("[3,[[9,{},0.1,61200.5]],6,{}]", T0 - 1000, T0 + 2),
            6,
        ),
        line(
            "trades",
            &format!(r#"[3,"te",[10,{},-0.2,61200.5],7,{}]"#, T0 + 3, T0 + 3),
            7,
        ),
        line(
            "trades",
            &format!(r#"[3,"tu",[10,{},-0.2,61200.5],8,{}]"#, T0 + 3, T0 + 3),
            8,
        ),
        line("book_R0", &format!("[1,[101,61200.5,0.3],9,{}]", T0 + 3), 9),
        line("book_R0", &format!("[1,[102,61206,-0.8],10,{}]", T0 + 4), 10),
        line("book_R0", &format!("[1,[103,0,1],11,{}]", T0 + 5), 11),
        line("book_R0", &format!("[1,[999,0,-1],12,{}]", T0 + 5), 12),
    ]
}

fn p0() -> Vec<String> {
    vec![
        line("book_P0", &format!("[2,[[61200.5,1,0.5],[61205,1,-0.8]],2,{T0}]"), 2),
        line("book_P0", &format!("[2,[61200.5,2,0.75],4,{}]", T0 + 1), 4),
        line("book_P0", &format!("[2,[61205,0,-1],13,{}]", T0 + 6), 13),
    ]
}

#[test]
fn bitfinex_split_frame() {
    let p = |s: &str| parse(s).unwrap();
    let f = p("[1,[[1,2,3]],5,99]");
    let (tag, pl, seq, ts) = split_frame(&f, FLAGS).unwrap();
    assert_eq!(
        (tag, pl.map(|x| x.as_arr().unwrap().len()), seq, ts),
        (None, Some(1), Some(5), Some(99))
    );
    let f = p(r#"[3,"te",[1,2,3,4],7,99]"#);
    let (tag, pl, seq, ts) = split_frame(&f, FLAGS).unwrap();
    assert_eq!((tag, pl.is_some(), seq, ts), (Some("te"), true, Some(7), Some(99)));
    let f = p(r#"[1,"hb",8,99]"#);
    assert_eq!(split_frame(&f, FLAGS).unwrap(), (Some("hb"), None, Some(8), Some(99)));
    let f = p("[1,[1,2,3]]");
    let (tag, pl, seq, ts) = split_frame(&f, 0).unwrap();
    assert_eq!((tag, pl.is_some(), seq, ts), (None, true, None, None));
    let f = p("[1,[1,2,3],42]");
    assert_eq!(split_frame(&f, TIMESTAMP).unwrap().3, Some(42));
    assert_eq!(split_frame(&J::Null, FLAGS).unwrap(), (None, None, None, None));
}

#[test]
fn bitfinex_raw_book_to_mbo() {
    let (rows, tr, st) = convert(lines(&r0()), &cfg(FLAGS)).unwrap();
    let want: Vec<Act> = vec![
        ('R', 'N', UNDEF_PRICE, 0, 0, 0, 1),
        ('A', 'B', 61_200_500_000_000, 50_000_000, 101, 32, 1),
        ('A', 'A', 61_205_000_000_000, 80_000_000, 102, 32 | 128, 1),
        ('A', 'B', 61_200_500_000_000, 25_000_000, 103, 128, 2),
        ('T', 'A', 61_200_500_000_000, 20_000_000, 0, 128, 3),
        ('M', 'B', 61_200_500_000_000, 30_000_000, 101, 128, 4),
        ('M', 'A', 61_206_000_000_000, 80_000_000, 102, 128, 5),
        ('C', 'B', 61_200_500_000_000, 25_000_000, 103, 128, 6),
    ];
    assert_eq!(acts(&rows), want);
    assert_eq!(
        (rows[0].ts_event, rows[4].ts_event, rows[4].ts_recv),
        (T0 * 1_000_000, (T0 + 3) * 1_000_000, 7)
    );
    assert_eq!(tr.len(), 1);
    let t = tr[0];
    assert_eq!(
        (
            t.ts_event,
            t.instrument_id,
            t.price,
            t.size,
            t.aggressor_side,
            t.sequence,
            t.maker_order_id
        ),
        (
            (T0 + 3) * 1_000_000,
            1,
            61_200_500_000_000,
            20_000_000,
            Side::Ask,
            3,
            None
        )
    );
    assert_eq!(
        (
            get(&st, "adds"),
            get(&st, "updates"),
            get(&st, "deletes"),
            get(&st, "te"),
            get(&st, "trades_ignored")
        ),
        (1, 2, 1, 1, 2)
    );
    assert_eq!(
        (
            get(&st, "price_changes"),
            get(&st, "delete_unknown"),
            get(&st, "open_at_end"),
            get(&st, "records")
        ),
        (1, 1, 2, 8)
    );
    assert_eq!(get(&st, "no_execution_records"), 1);
    let twice = vec![r0()[0].clone(), r0()[0].clone()];
    let (rows2, _, st2) = convert(lines(&twice), &cfg(FLAGS)).unwrap();
    assert_eq!(get(&st2, "snapshots_after_first"), 1);
    assert_eq!(rows2.iter().map(|r| r.action as char).collect::<String>(), "RAARAA");
}

#[test]
fn bitfinex_decimal_exactness_from_frame_text() {
    let (rows, _, _) = convert(
        lines(&[line("book_R0", "[1,[7,61200.12345678,0.12345678]]", 0)]),
        &cfg(0),
    )
    .unwrap();
    assert_eq!((rows[0].price, rows[0].size), (61_200_123_456_780, 12_345_678));
    let e = convert(
        lines(&[line("book_R0", "[1,[7,61200.5,0.30000000000000004]]", 0)]),
        &cfg(0),
    )
    .unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-004");
    let e = convert(lines(&[line("book_R0", "[1,[7,61200.0000000001,1]]", 0)]), &cfg(0)).unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-004");
    let (rows, _, _) = convert(lines(&[line("book_R0", "[1,[7,61200.5,-2]]", 123)]), &cfg(0)).unwrap();
    assert_eq!((rows[0].ts_event, rows[0].side), (123, b'A')); // no TIMESTAMP flag: receive time; negative = ask
}

#[test]
fn bitfinex_chunking_u32_and_u64_paths() {
    assert_eq!(chunk_sizes(5, U32), vec![5]);
    assert_eq!(chunk_sizes(1 << 33, U32), vec![U32, U32, 2]);
    assert_eq!(chunk_sizes(1 << 33, u64::MAX), vec![1 << 33]);
    assert_eq!(chunk_sizes(U32, U32), vec![U32]);
    assert_eq!(child_id(7, 0).unwrap(), 7);
    assert_eq!(child_id(7, 1).unwrap(), CHUNK_ID_BASE + 7 * 16 + 1);
    let big = line("book_R0", "[1,[7,61200.5,50]]", 0); // 50 BTC = 5e9 lots at 1e-8: above u32
    let u32cfg = Config {
        max_size: U32,
        ..cfg(0)
    };
    let (rows, _, st) = convert(lines(std::slice::from_ref(&big)), &u32cfg).unwrap();
    let got: Vec<(char, u64, u64, u8)> = rows
        .iter()
        .map(|r| (r.action as char, r.size, r.order_id, r.flags))
        .collect();
    assert_eq!(
        got,
        vec![
            ('A', U32, 7, 0),
            ('A', 5_000_000_000 - U32, child_id(7, 1).unwrap(), 128)
        ]
    );
    assert_eq!(get(&st, "chunked_orders"), 1);
    let (rows, _, st) = convert(lines(std::slice::from_ref(&big)), &cfg(0)).unwrap(); // u64 sizes: one record
    let got: Vec<(char, u64, u64, u8)> = rows
        .iter()
        .map(|r| (r.action as char, r.size, r.order_id, r.flags))
        .collect();
    assert_eq!(got, vec![('A', 5_000_000_000, 7, 128)]);
    assert_eq!(get(&st, "chunked_orders"), 0);
    let dele = vec![big, line("book_R0", "[1,[7,0,1]]", 1)];
    let (rows, _, _) = convert(lines(&dele), &cfg(0)).unwrap();
    let got: Vec<(char, u64)> = rows.iter().map(|r| (r.action as char, r.size)).collect();
    assert_eq!(got, vec![('A', 5_000_000_000), ('C', 5_000_000_000)]);
    // chunked delete removes every child
    let dele = vec![
        line("book_R0", "[1,[7,61200.5,50]]", 0),
        line("book_R0", "[1,[7,0,1]]", 1),
    ];
    let (rows, _, _) = convert(lines(&dele), &u32cfg).unwrap();
    assert_eq!(rows.iter().map(|r| r.action as char).collect::<String>(), "AACC");
}

#[test]
fn bitfinex_seq_all_continuity() {
    let mut cap: Vec<String> = r0().into_iter().filter(|l| l.contains("book_R0")).collect();
    cap.extend(p0());
    cap.extend(r0().into_iter().filter(|l| l.contains("\"trades\"")));
    let hb = line("hb", &format!(r#"[1,"hb",5,{}]"#, T0 + 2), 5);
    let mut with_hb = cap.clone();
    with_hb.push(hb);
    let (rows, tr, st) = convert(lines(&with_hb), &cfg(FLAGS)).unwrap();
    assert_eq!((rows.len(), tr.len()), (8, 1)); // P0 and hb are not order-level
    assert_eq!((get(&st, "seq_all_checked"), get(&st, "seq_all_gaps")), (13, 0));
    assert_eq!(
        (
            get(&st, "snapshots_book_R0"),
            get(&st, "snapshots_book_P0"),
            get(&st, "snapshots_trades")
        ),
        (1, 1, 1)
    );
    let (_, _, st) = convert(lines(&cap), &cfg(FLAGS)).unwrap(); // seq 5 missing -> one gap (4 -> 6)
    assert_eq!((get(&st, "seq_all_checked"), get(&st, "seq_all_gaps")), (12, 1));
    // a reconnect marker resets the chain
    let mut rc = cap.clone();
    rc.push(r#"{"recv_ns":5,"chan":"_capture","symbol":null,"msg":null}"#.to_string());
    let (_, _, st) = convert(lines(&rc), &cfg(FLAGS)).unwrap();
    assert_eq!(get(&st, "seq_all_gaps"), 0);
}
