//! O-PAR-3 (part 2): a synthetic ITCH day (every order/trade message type, a filtered symbol, an unknown type), built
//! with `qt_ingest::itch::encode`, ingested with `ingest(format = itch)` yields the expected MBO and trade records
//! with et converter semantics: `T` print, `F`, then `C` per execution; one `sequence` per message; `LAST` on the
//! message's last record; replace = `C` + `A` with the new reference; `Price (4)` x 1e5 to 1e-9.

use std::io::Write;
use std::path::{Path, PathBuf};

use flate2::Compression;
use flate2::write::GzEncoder;
use proptest::prelude::*;
use qt_ingest::itch::{Body, Header, MapOutput, Mapper, Message, Reader, StockDirectory, encode, encode_file, stock};
use qt_ingest::{Dataset, ingest};
use qt_schema::json::Verdict;
use qt_schema::rows::{TradeKind, TradeRow};
use qt_schema::specs::IngestSpec;
use qt_types::{Instrument, MboEvent, Side, Venue, flags};
use serde_json::json;

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-itch-day-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

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

fn dir(sym: &str) -> Body {
    Body::StockDirectory(StockDirectory {
        stock: stock(sym),
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
    })
}

fn add(locate: u16, ts: u64, id: u64, side: u8, shares: u32, px: u32, mpid: bool) -> Message {
    msg(
        if mpid { b'F' } else { b'A' },
        locate,
        ts,
        Body::AddOrder {
            order_ref: id,
            buy_sell: side,
            shares,
            stock: stock(if locate == 1 { "AAPL" } else { "MSFT" }),
            price: px,
            mpid: mpid.then_some(*b"NSDQ"),
        },
    )
}

fn t(ms: u64) -> u64 {
    34_200_000_000_000 + ms * 1_000_000
}

/// The fixture day (et `tests/roundtrip.rs`): AAPL (locate 1) is kept, MSFT (2) is filtered out, XYZ (3) is noise.
fn day() -> Vec<Message> {
    const AAPL: u16 = 1;
    const MSFT: u16 = 2;
    vec![
        msg(b'S', 0, t(0), Body::SystemEvent { event: b'O' }),
        msg(b'R', AAPL, t(1), dir("AAPL")),
        msg(b'R', MSFT, t(1), dir("MSFT")),
        msg(b'R', 3, t(1), dir("XYZ")),
        msg(
            b'H',
            AAPL,
            t(2),
            Body::TradingAction {
                stock: stock("AAPL"),
                state: b'T',
                reason: *b"    ",
            },
        ),
        msg(b'S', 0, t(3), Body::SystemEvent { event: b'Q' }),
        add(AAPL, t(10), 1, b'B', 100, 1_850_000, false),
        add(AAPL, t(11), 2, b'B', 200, 1_850_000, true),
        add(AAPL, t(12), 3, b'B', 50, 1_849_900, false),
        add(AAPL, t(13), 4, b'S', 300, 1_850_200, false),
        add(AAPL, t(14), 5, b'S', 100, 1_850_500, true),
        add(MSFT, t(15), 100, b'B', 10, 4_000_000, false),
        msg(
            b'E',
            MSFT,
            t(16),
            Body::Executed {
                order_ref: 100,
                shares: 10,
                match_no: 1,
            },
        ),
        msg(
            b'E',
            AAPL,
            t(20),
            Body::Executed {
                order_ref: 1,
                shares: 100,
                match_no: 2,
            },
        ),
        msg(
            b'E',
            AAPL,
            t(20),
            Body::Executed {
                order_ref: 2,
                shares: 50,
                match_no: 3,
            },
        ),
        msg(
            b'X',
            AAPL,
            t(30),
            Body::Cancel {
                order_ref: 2,
                shares: 50,
            },
        ),
        msg(
            b'U',
            AAPL,
            t(40),
            Body::Replace {
                old_ref: 4,
                new_ref: 6,
                shares: 200,
                price: 1_850_300,
            },
        ),
        msg(
            b'C',
            AAPL,
            t(50),
            Body::ExecutedWithPrice {
                order_ref: 5,
                shares: 40,
                match_no: 4,
                printable: b'N',
                price: 1_850_400,
            },
        ),
        msg(
            b'C',
            AAPL,
            t(51),
            Body::ExecutedWithPrice {
                order_ref: 6,
                shares: 20,
                match_no: 5,
                printable: b'Y',
                price: 1_850_300,
            },
        ),
        msg(
            b'P',
            AAPL,
            t(60),
            Body::Trade {
                order_ref: 0,
                buy_sell: b'B',
                shares: 70,
                stock: stock("AAPL"),
                price: 1_850_100,
                match_no: 6,
            },
        ),
        msg(
            b'Q',
            AAPL,
            t(61),
            Body::CrossTrade {
                shares: 1000,
                stock: stock("AAPL"),
                price: 1_850_200,
                match_no: 7,
                cross_type: b'O',
            },
        ),
        msg(
            b'Q',
            AAPL,
            t(62),
            Body::CrossTrade {
                shares: 0,
                stock: stock("AAPL"),
                price: 0,
                match_no: 8,
                cross_type: b'H',
            },
        ),
        msg(b'B', AAPL, t(63), Body::BrokenTrade { match_no: 6 }),
        msg(
            b'N',
            AAPL,
            t(64),
            Body::Rpii {
                stock: stock("AAPL"),
                interest: b'B',
            },
        ),
        msg(b'D', AAPL, t(70), Body::Delete { order_ref: 3 }),
        msg(b'S', 0, t(80), Body::SystemEvent { event: b'C' }),
    ]
}

/// Framed bytes of `day()` plus an unknown message type `Z` with a 15-byte body.
fn framed() -> Vec<u8> {
    let mut buf = Vec::new();
    for m in day() {
        encode(&m, &mut buf);
    }
    let mut z = vec![b'Z', 0, 1, 0, 1];
    z.extend_from_slice(&t(65).to_be_bytes()[2..]);
    z.extend_from_slice(&[0xAA; 15]);
    buf.extend_from_slice(&(z.len() as u16).to_be_bytes());
    buf.extend_from_slice(&z);
    buf
}

fn gz(bytes: &[u8]) -> Vec<u8> {
    let mut e = GzEncoder::new(Vec::new(), Compression::default());
    e.write_all(bytes).unwrap();
    e.finish().unwrap()
}

const MIDNIGHT: i64 = 1_548_824_400_000_000_000;

#[allow(clippy::too_many_arguments)]
fn ev(ms: u64, action: u8, side: u8, price: i64, size: u64, oid: u64, last: bool, seq: u64) -> MboEvent {
    let ts = MIDNIGHT + t(ms) as i64;
    MboEvent {
        ts_event: ts,
        ts_recv: ts,
        instrument_id: 1,
        action,
        side,
        price,
        size,
        order_id: oid,
        flags: if last { flags::LAST } else { 0 },
        sequence: seq,
        channel_id: 0,
        publisher_id: 0,
    }
}

fn expected_mbo() -> Vec<MboEvent> {
    let p = |x: i64| x * 100_000;
    vec![
        ev(10, b'A', b'B', p(1_850_000), 100, 1, true, 1),
        ev(11, b'A', b'B', p(1_850_000), 200, 2, true, 2),
        ev(12, b'A', b'B', p(1_849_900), 50, 3, true, 3),
        ev(13, b'A', b'A', p(1_850_200), 300, 4, true, 4),
        ev(14, b'A', b'A', p(1_850_500), 100, 5, true, 5),
        // E on order 1: T (aggressor A, order 0), F, C share sequence 6; LAST on the C.
        ev(20, b'T', b'A', p(1_850_000), 100, 0, false, 6),
        ev(20, b'F', b'B', p(1_850_000), 100, 1, false, 6),
        ev(20, b'C', b'B', p(1_850_000), 100, 1, true, 6),
        ev(20, b'T', b'A', p(1_850_000), 50, 0, false, 7),
        ev(20, b'F', b'B', p(1_850_000), 50, 2, false, 7),
        ev(20, b'C', b'B', p(1_850_000), 50, 2, true, 7),
        // X: partial cancel keeps priority.
        ev(30, b'C', b'B', p(1_850_000), 50, 2, true, 8),
        // U: C of the old remaining size, then A with the new id and the old side.
        ev(40, b'C', b'A', p(1_850_200), 300, 4, false, 9),
        ev(40, b'A', b'A', p(1_850_300), 200, 6, true, 9),
        // Non-printable C: F at the execution price, C at the resting price, no T.
        ev(50, b'F', b'A', p(1_850_400), 40, 5, false, 10),
        ev(50, b'C', b'A', p(1_850_500), 40, 5, true, 10),
        // Printable C: T (aggressor B) and F at the execution price.
        ev(51, b'T', b'B', p(1_850_300), 20, 0, false, 11),
        ev(51, b'F', b'A', p(1_850_300), 20, 6, false, 11),
        ev(51, b'C', b'A', p(1_850_300), 20, 6, true, 11),
        // P and Q: T with side N, order 0 (the zero-share Q emits nothing).
        ev(60, b'T', b'N', p(1_850_100), 70, 0, true, 12),
        ev(61, b'T', b'N', p(1_850_200), 1000, 0, true, 13),
        ev(70, b'C', b'B', p(1_849_900), 50, 3, true, 14),
    ]
}

fn expected_trades() -> Vec<TradeRow> {
    let tr = |ms: u64, seq: u64, px: i64, size: u64, side: Side, kind: TradeKind, maker: Option<u64>| TradeRow {
        ts_event: MIDNIGHT + t(ms) as i64,
        instrument_id: 1,
        sequence: seq,
        price: px * 100_000,
        size,
        aggressor_side: side,
        kind,
        maker_order_id: maker,
    };
    vec![
        tr(20, 6, 1_850_000, 100, Side::Ask, TradeKind::Displayed, Some(1)),
        tr(20, 7, 1_850_000, 50, Side::Ask, TradeKind::Displayed, Some(2)),
        tr(51, 11, 1_850_300, 20, Side::Bid, TradeKind::Displayed, Some(6)),
        tr(60, 12, 1_850_100, 70, Side::None, TradeKind::Hidden, None),
        tr(61, 13, 1_850_200, 1000, Side::None, TradeKind::Cross, None),
    ]
}

fn spec(input: &Path, out: &Path, extra: serde_json::Value) -> IngestSpec {
    let mut v = json!({"format": "itch", "inputs": [{"path": input.to_str().unwrap()}], "venue": "nasdaq",
                       "out": out.to_str().unwrap()});
    if let (Some(o), Some(e)) = (v.as_object_mut(), extra.as_object()) {
        for (k, x) in e {
            o.insert(k.clone(), x.clone());
        }
    }
    serde_json::from_value(v).unwrap()
}

#[test]
fn o_par_3_synthetic_itch_day_ingests_to_expected_records() {
    let dir = scratch("day");
    let file = dir.join("01302019.NASDAQ_ITCH50.gz");
    std::fs::write(&file, gz(&framed())).unwrap();
    let out = dir.join("ds");
    let s = ingest(&spec(&file, &out, json!({"symbols": ["aapl", "NOPE"]}))).unwrap();
    assert_eq!(s.manifest.source_format, "itch");
    assert_eq!(s.manifest.venue, Venue::Nasdaq);
    assert_eq!(s.manifest.partitions.len(), 1);
    assert_eq!(s.manifest.inputs.len(), 1);
    assert_eq!(s.spec["format"], "itch");
    assert!(
        s.spec.get("out").is_none(),
        "execution-only field removed from the echo"
    );

    let dq = &s.data_quality;
    assert_eq!(dq.records, 22);
    // One `P` hidden print; the `Q` cross print is counted `cross_prints` (INFO, OC-28), not as a hidden print.
    assert_eq!(dq.hidden_prints, 1);
    assert_eq!(dq.source_specific.get("cross_prints"), Some(&1));
    assert_eq!(dq.unknown_order_refs, 0);
    assert_eq!(dq.source_specific.get("itch_unknown_message_type"), Some(&1));
    assert_eq!(dq.source_specific.get("itch_zero_share_cross"), Some(&1));
    assert_eq!(dq.source_specific.get("itch_symbols_not_found"), Some(&1));
    assert_eq!(dq.verdict, Verdict::Warnings);

    let ds = Dataset::open(&out).unwrap();
    assert_eq!(ds.partitions().len(), 1);
    let p = &ds.partitions()[0];
    assert_eq!((p.instrument_id, p.session_date, p.rows), (1, 17_926, 22));
    let got: Vec<MboEvent> = p.mbo().unwrap().map(Result::unwrap).collect();
    assert_eq!(got, expected_mbo());
    assert_eq!((p.ts_min, p.ts_max), (got[0].ts_event, got[21].ts_event));
    assert_eq!(p.source_trades().unwrap().unwrap(), expected_trades());
    assert_eq!(
        ds.instrument(1, 17_926).unwrap(),
        &Instrument {
            instrument_id: 1,
            session_date: 17_926,
            symbol: "AAPL".into(),
            venue: Venue::Nasdaq,
            source_venue: "nasdaq".into(),
            price_exp: -9,
            tick_size: 10_000_000,
            lot_size: 1,
            size_exp: 0,
            contract_multiplier: 1.0,
            currency: "USD".into(),
            session_tz: "America/New_York".into(),
            midnight_utc_ns: MIDNIGHT,
            open_s: 34_200,
            close_s: 57_600,
            source: "itch".into(),
        }
    );
    assert_eq!(ds.instruments.len(), 1, "only the selected directory entry");
    assert_eq!(ds.partition_for(1, MIDNIGHT + 1).map(|p| p.instrument_id), Some(1));
    assert!(ds.partition_for(1, MIDNIGHT - 1).is_none());
    assert_eq!(ds.manifest.price_exp_by_instrument.get("1"), Some(&-9));

    // Same input again (raw framing via encode_file + appended unknown) gives the same partition bytes.
    let raw = dir.join("01302019.NASDAQ_ITCH50");
    std::fs::write(&raw, framed()).unwrap();
    let s2 = ingest(&spec(&raw, &dir.join("ds2"), json!({"symbols": ["AAPL", "NOPE"]}))).unwrap();
    assert_eq!(s2.manifest.dataset_id, s.manifest.dataset_id);
    assert_eq!(s2.manifest.partitions, s.manifest.partitions);
    std::fs::remove_dir_all(dir).ok();
}

#[test]
fn o_par_3_unfiltered_day_and_price_exponent() {
    let dir = scratch("all");
    let file = dir.join("in.itch");
    encode_file(&day(), &file, true).unwrap();
    // Date from session_date (the file name has none); price_exp -4 keeps ITCH units.
    let s = ingest(&spec(
        &file,
        &dir.join("ds"),
        json!({"session_date": "2019-01-30", "price_exp": -4}),
    ))
    .unwrap();
    let ds = Dataset::open(&dir.join("ds")).unwrap();
    let parts: Vec<(u32, u64)> = ds.partitions().iter().map(|p| (p.instrument_id, p.rows)).collect();
    assert_eq!(parts, vec![(1, 22), (2, 4)]);
    assert_eq!(ds.instruments.len(), 3, "every directory entry (XYZ has no partition)");
    assert_eq!(ds.instrument(1, 17_926).unwrap().tick_size, 100);
    let first = ds.partitions()[0].mbo().unwrap().next().unwrap().unwrap();
    assert_eq!(first.price, 1_850_000);
    // MSFT: A (seq 6) then E -> T, F, C (seq 7): sequences are shared across instruments of the file.
    let msft: Vec<MboEvent> = ds.partitions()[1].mbo().unwrap().map(Result::unwrap).collect();
    assert_eq!(
        msft.iter().map(|e| (e.action, e.sequence)).collect::<Vec<_>>(),
        vec![(b'A', 6), (b'T', 7), (b'F', 7), (b'C', 7)]
    );
    assert_eq!(s.data_quality.records, 26);
    // No date source at all is a config error; a bad price_exp too.
    let e = ingest(&spec(&file, &dir.join("ds3"), json!({}))).unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001");
    let e = ingest(&spec(
        &file,
        &dir.join("ds4"),
        json!({"session_date": "2019-01-30", "price_exp": -2}),
    ))
    .unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001");
    // Output directory must be empty.
    let e = ingest(&spec(&file, &dir.join("ds"), json!({"session_date": "2019-01-30"}))).unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001");
    std::fs::remove_dir_all(dir).ok();
}

#[test]
fn o_par_3_truncated_file_is_an_input_error() {
    let dir = scratch("trunc");
    let mut bytes = framed();
    bytes.truncate(bytes.len() - 3);
    let file = dir.join("01302019.NASDAQ_ITCH50.gz");
    std::fs::write(&file, gz(&bytes)).unwrap();
    let e = ingest(&spec(&file, &dir.join("ds"), json!({}))).unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002");
    // A corrupt gzip stream too.
    let mut g = gz(&framed());
    let n = g.len();
    g[n / 2] ^= 0xff;
    g[n / 2 + 1] ^= 0xff;
    std::fs::write(&file, &g).unwrap();
    let e = ingest(&spec(&file, &dir.join("ds2"), json!({}))).unwrap_err();
    assert!(matches!(e.code(), "QT-INPUT-001" | "QT-INPUT-002"), "{e}");
    std::fs::remove_dir_all(dir).ok();
}

/// Converter invariants (et): executions and cancels never exceed the tracked remaining size; every E/C/X/D/U must
/// reference a live order. A clean fixture has zero anomalies; a corrupted one reports each kind.
#[test]
fn converter_invariants_on_clean_and_corrupted_streams() {
    let mut conv = Mapper::new(0, None, -9).unwrap();
    let mut out = MapOutput::default();
    let mut live: std::collections::HashMap<u64, u64> = Default::default();
    for m in day() {
        conv.apply(&m, &mut out).unwrap();
        for r in &out.mbo {
            match r.action {
                b'A' => assert!(live.insert(r.order_id, r.size).is_none(), "dup {}", r.order_id),
                b'C' => {
                    let rem = live.get_mut(&r.order_id).expect("C references a live order");
                    assert!(r.size <= *rem, "C exceeds remaining");
                    *rem -= r.size;
                    if *rem == 0 {
                        live.remove(&r.order_id);
                    }
                }
                b'F' => assert!(r.size <= live[&r.order_id], "F exceeds remaining"),
                b'T' => assert_eq!(r.order_id, 0),
                _ => panic!("unexpected action {}", r.action as char),
            }
            assert_eq!(r.price % 100_000, 0);
        }
        if let Some(last) = out.mbo.last() {
            assert_eq!(last.flags, flags::LAST);
            assert!(out.mbo[..out.mbo.len() - 1].iter().all(|r| r.flags == 0));
            assert!(out.mbo.iter().all(|r| r.sequence == last.sequence));
        }
    }
    let s = conv.finish();
    assert_eq!(s.anomalies.total(), s.anomalies.zero_share_cross);
    assert_eq!(s.anomalies.zero_share_cross, 1);
    assert_eq!(s.orders_live, live.len() as u64);
    assert_eq!(s.orders_live, 3);
    assert_eq!(s.selected_instruments, 3);
    assert_eq!(s.messages, 26);
    assert_eq!(s.by_type["A"], 4);
    assert_eq!(s.by_type["F"], 2);
    assert_eq!(s.system_events["O"], 34_200_000_000_000);

    let bad = vec![
        add(1, 1, 1, b'B', 100, 1_000_000, false),
        add(1, 2, 1, b'S', 5, 1_000_100, false),
        msg(
            b'E',
            1,
            3,
            Body::Executed {
                order_ref: 99,
                shares: 1,
                match_no: 1,
            },
        ),
        msg(
            b'X',
            1,
            4,
            Body::Cancel {
                order_ref: 1,
                shares: 50,
            },
        ),
        msg(
            b'E',
            1,
            5,
            Body::Executed {
                order_ref: 1,
                shares: 7,
                match_no: 2,
            },
        ),
        msg(b'D', 1, 6, Body::Delete { order_ref: 1 }),
        msg(
            b'U',
            1,
            7,
            Body::Replace {
                old_ref: 42,
                new_ref: 43,
                shares: 1,
                price: 1,
            },
        ),
        add(1, 8, 7, b'X', 1, 1, false),
        add(1, 9, 8, b'B', 10, 1_000_000, false),
        msg(
            b'E',
            2,
            10,
            Body::Executed {
                order_ref: 8,
                shares: 20,
                match_no: 3,
            },
        ),
    ];
    let mut conv = Mapper::new(0, None, -9).unwrap();
    let mut emitted = Vec::new();
    for m in &bad {
        conv.apply(m, &mut out).unwrap();
        emitted.extend(out.mbo.iter().copied());
    }
    let a = &conv.finish().anomalies;
    assert_eq!(
        (
            a.duplicate_order_ref,
            a.cancel_exceeds_resting,
            a.exec_exceeds_resting,
            a.locate_mismatch,
            a.bad_side
        ),
        (1, 1, 1, 1, 1)
    );
    assert_eq!(a.unknown_order_ref, 4);
    let x: Vec<_> = emitted.iter().filter(|r| r.action == b'C' && r.order_id == 1).collect();
    assert_eq!(x.len(), 1);
    assert_eq!(x[0].size, 5);
    let f: Vec<_> = emitted.iter().filter(|r| r.action == b'F' && r.order_id == 8).collect();
    assert_eq!(f[0].size, 10);
}

fn arb_msg() -> impl Strategy<Value = Message> {
    let id = 1u64..12;
    let sh = 0u32..300;
    let px = 1_000_000u32..1_000_050;
    let loc = 1u16..3;
    prop_oneof![
        (
            loc.clone(),
            id.clone(),
            prop::sample::select(vec![b'B', b'S', b'X']),
            sh.clone(),
            px.clone()
        )
            .prop_map(|(l, i, s, n, p)| msg(
                b'A',
                l,
                1,
                Body::AddOrder {
                    order_ref: i,
                    buy_sell: s,
                    shares: n,
                    stock: stock("A"),
                    price: p,
                    mpid: None
                }
            )),
        (loc.clone(), id.clone(), sh.clone()).prop_map(|(l, i, n)| msg(
            b'E',
            l,
            2,
            Body::Executed {
                order_ref: i,
                shares: n,
                match_no: 0
            }
        )),
        (loc.clone(), id.clone(), sh.clone(), any::<bool>(), px.clone()).prop_map(|(l, i, n, pr, p)| msg(
            b'C',
            l,
            3,
            Body::ExecutedWithPrice {
                order_ref: i,
                shares: n,
                match_no: 0,
                printable: if pr { b'Y' } else { b'N' },
                price: p
            }
        )),
        (loc.clone(), id.clone(), sh.clone()).prop_map(|(l, i, n)| msg(
            b'X',
            l,
            4,
            Body::Cancel {
                order_ref: i,
                shares: n
            }
        )),
        (loc.clone(), id.clone()).prop_map(|(l, i)| msg(b'D', l, 5, Body::Delete { order_ref: i })),
        (loc.clone(), id.clone(), id.clone(), sh.clone(), px.clone()).prop_map(|(l, a, b, n, p)| msg(
            b'U',
            l,
            6,
            Body::Replace {
                old_ref: a,
                new_ref: b,
                shares: n,
                price: p
            }
        )),
        (loc.clone(), any::<u64>(), px.clone()).prop_map(|(l, n, p)| msg(
            b'Q',
            l,
            7,
            Body::CrossTrade {
                shares: n % 3,
                stock: stock("A"),
                price: p,
                match_no: 0,
                cross_type: b'O'
            }
        )),
        (loc, sh, px).prop_map(|(l, n, p)| msg(
            b'P',
            l,
            8,
            Body::Trade {
                order_ref: 0,
                buy_sell: b'B',
                shares: n,
                stock: stock("A"),
                price: p,
                match_no: 0
            }
        )),
    ]
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 1000, ..ProptestConfig::default() })]

    /// Random order-message streams: never a panic; emitted C/F never exceed the size the records themselves left
    /// resting; LAST exactly on each message's last record; sequences strictly increase by one per message.
    #[test]
    fn mapper_invariants_on_random_streams(msgs in prop::collection::vec(arb_msg(), 0..200)) {
        let mut conv = Mapper::new(1_000, None, -9).unwrap();
        let mut out = MapOutput::default();
        let mut live: std::collections::HashMap<u64, u64> = Default::default();
        let mut last_seq = 0;
        for m in &msgs {
            conv.apply(m, &mut out).unwrap();
            if let Some(last) = out.mbo.last() {
                prop_assert_eq!(last.sequence, last_seq + 1);
                last_seq = last.sequence;
                prop_assert_eq!(last.flags, flags::LAST);
            }
            for r in &out.mbo {
                match r.action {
                    b'A' => { live.insert(r.order_id, r.size); }
                    b'C' => {
                        let rem = live.get_mut(&r.order_id);
                        prop_assert!(rem.is_some());
                        let rem = rem.unwrap();
                        prop_assert!(r.size <= *rem);
                        *rem -= r.size;
                        if *rem == 0 { live.remove(&r.order_id); }
                    }
                    b'F' => prop_assert!(r.size <= live.get(&r.order_id).copied().unwrap_or(0)),
                    b'T' => prop_assert_eq!(r.order_id, 0),
                    _ => prop_assert!(false),
                }
            }
        }
        prop_assert_eq!(conv.live_orders(), live.len());
    }

    /// The framing reader never panics on arbitrary bytes and never returns more bytes than it was given.
    #[test]
    fn reader_never_panics(bytes in prop::collection::vec(any::<u8>(), 0..400)) {
        let mut r = Reader::new(std::io::Cursor::new(bytes.clone()));
        let mut total = 0usize;
        while let Ok(Some(m)) = r.next_message() {
            total += m.len() + 2;
            let _ = qt_ingest::itch::parse(m);
        }
        prop_assert!(total <= bytes.len());
    }
}

/// Parse + map throughput on an in-memory synthetic stream (10M messages, mixed A/E/X/D/U/P) and ITCH -> dataset
/// Parquet ingest on a 5M-message raw file. `cargo test --release -p queuetruth-ingest --test itch_day --
/// --ignored bench_itch --nocapture`.
#[test]
#[ignore]
fn bench_itch_parse_map_and_ingest() {
    use std::time::Instant;
    fn stream(n: usize) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(n * 40);
        let mut id = 1u64;
        for i in 0..n {
            let ts = (i as u64) * 1000;
            let loc = 1 + (i % 4) as u16;
            let m = match i % 6 {
                0 | 1 => {
                    id += 1;
                    add(
                        loc,
                        ts,
                        id,
                        if i % 2 == 0 { b'B' } else { b'S' },
                        100,
                        1_000_000 + (i % 50) as u32 * 100,
                        i % 4 == 0,
                    )
                }
                2 => msg(
                    b'E',
                    loc,
                    ts,
                    Body::Executed {
                        order_ref: id,
                        shares: 30,
                        match_no: i as u64,
                    },
                ),
                3 => msg(
                    b'X',
                    loc,
                    ts,
                    Body::Cancel {
                        order_ref: id,
                        shares: 20,
                    },
                ),
                4 => msg(
                    b'U',
                    loc,
                    ts,
                    Body::Replace {
                        old_ref: id,
                        new_ref: id + 1_000_000_000,
                        shares: 60,
                        price: 1_000_050,
                    },
                ),
                _ => msg(
                    b'P',
                    loc,
                    ts,
                    Body::Trade {
                        order_ref: 0,
                        buy_sell: b'B',
                        shares: 10,
                        stock: stock("AAPL"),
                        price: 1_000_000,
                        match_no: i as u64,
                    },
                ),
            };
            encode(&m, &mut bytes);
        }
        bytes
    }
    const N: usize = 10_000_000;
    let bytes = stream(N);
    let t0 = Instant::now();
    let mut r = Reader::new(std::io::Cursor::new(&bytes));
    let mut conv = Mapper::new(0, None, -9).unwrap();
    let mut out = MapOutput::default();
    let mut recs = 0u64;
    let mut n = 0usize;
    while let Some(b) = r.next_message().unwrap() {
        conv.apply(&qt_ingest::itch::parse(b).unwrap(), &mut out).unwrap();
        recs += out.mbo.len() as u64;
        n += 1;
    }
    let s = t0.elapsed().as_secs_f64();
    assert_eq!(n, N);
    println!(
        "parse+map: {N} msgs, {recs} records in {s:.2} s = {:.2}M msg/s",
        N as f64 / s / 1e6
    );

    let tmp = scratch("bench");
    let mut msgs = vec![
        msg(b'R', 1, 0, dir("AAPL")),
        msg(b'R', 2, 0, dir("MSFT")),
        msg(b'R', 3, 0, dir("SPY")),
        msg(b'R', 4, 0, dir("TSLA")),
    ];
    let mut head = Vec::new();
    for m in msgs.drain(..) {
        encode(&m, &mut head);
    }
    const M: usize = 5_000_000;
    let mut file_bytes = head;
    file_bytes.extend_from_slice(&stream(M));
    let file = tmp.join("01302019.NASDAQ_ITCH50");
    std::fs::write(&file, &file_bytes).unwrap();
    drop(file_bytes);
    let t0 = Instant::now();
    let sum = ingest(&spec(&file, &tmp.join("ds"), json!({}))).unwrap();
    let s = t0.elapsed().as_secs_f64();
    println!(
        "itch -> dataset parquet: {M} msgs ({} records, {} partitions) in {s:.2} s = {:.2}M msg/s",
        sum.data_quality.records,
        sum.manifest.partitions.len(),
        M as f64 / s / 1e6
    );
    std::fs::remove_dir_all(tmp).ok();
}
