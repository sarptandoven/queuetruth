//! IEX DEEP+ ingest on a synthetic capture built with `qt_ingest::iex::synth`: every order-book message type, the
//! Order Modify priority bit, executions with and without price improvement, a hidden (`T`) print, a single-price
//! cross, Clear Book, a filtered symbol, an A/B duplicate segment, a sequence gap, an unknown type, a short and an
//! empty message, and an over-length message. Checks the exact records, the dataset metadata (rule set `iex` on
//! Nasdaq records, `source_venue` iex) and the data-quality counters (gap -> `feed_gaps`).

use std::path::{Path, PathBuf};

use qt_ingest::iex::synth::{Segment, frames, message_bytes, pcap, pcapng, write_file};
use qt_ingest::iex::{Message, symbol};
use qt_ingest::{Dataset, ingest};
use qt_schema::json::Verdict;
use qt_schema::rows::{TradeKind, TradeRow};
use qt_schema::specs::IngestSpec;
use qt_types::{MboEvent, RuleSet, Side, UNDEF_PRICE, ValidationStatus, Venue, flags};
use serde_json::json;

/// 2026-10-09 09:30:00 US/Eastern.
const T0: i64 = 1_791_552_600_000_000_000;
/// 2026-10-09 00:00 US/Eastern as UTC ns.
const MID: i64 = 1_791_518_400_000_000_000;
const DATE: i32 = 20_735;
const SESSION: u32 = 0x50ff_0000;

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-iex-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn t(us: i64) -> i64 {
    T0 + us * 1_000
}

/// Price in DEEP+ units (4 decimals) from cents.
fn p(cents: i64) -> i64 {
    cents * 100
}

fn seg(first_seq: i64, at_us: i64, msgs: Vec<Vec<u8>>) -> Segment {
    Segment {
        channel: 1,
        session: SESSION,
        first_seq,
        send_time: t(at_us) + 50_000,
        capture_ns: t(at_us) + 2_000,
        messages: msgs,
    }
}

fn m(msg: Message) -> Vec<u8> {
    message_bytes(&msg)
}

fn add(us: i64, sym: &str, id: i64, buy: bool, size: u32, cents: i64) -> Vec<u8> {
    m(Message::AddOrder {
        side: if buy { b'8' } else { b'5' },
        ts: t(us),
        symbol: symbol(sym),
        order_id: id,
        size,
        price: p(cents),
    })
}

fn modify(us: i64, id: i64, maintain: bool, size: u32, cents: i64) -> Vec<u8> {
    m(Message::OrderModify {
        flags: u8::from(maintain),
        ts: t(us),
        symbol: symbol("SPY"),
        order_id: id,
        size,
        price: p(cents),
    })
}

fn exec(us: i64, id: i64, size: u32, cents: i64, sale_flags: u8) -> Vec<u8> {
    m(Message::OrderExecuted {
        sale_flags,
        ts: t(us),
        symbol: symbol("SPY"),
        order_id: id,
        size,
        price: p(cents),
        trade_id: 1_000 + id,
    })
}

fn directory(sym: &str) -> Vec<u8> {
    m(Message::SecurityDirectory {
        flags: 0x20,
        ts: t(-3_600_000_000),
        symbol: symbol(sym),
        round_lot: 100,
        adjusted_poc: p(50_000),
        luld_tier: 1,
    })
}

/// The fixture day. SPY is kept; QQQ is filtered out (its messages still number the feed).
fn day() -> Vec<Segment> {
    let mut over = exec(32, 2, 10, 50_001, 0);
    over.extend_from_slice(&[0xee; 6]); // grown message: the documented prefix is read, the rest ignored
    vec![
        seg(
            1,
            -3_600_000_000,
            vec![
                m(Message::SystemEvent {
                    event: b'O',
                    ts: t(-3_600_000_000),
                }),
                directory("SPY"),
                directory("QQQ"),
                m(Message::TradingStatus {
                    status: b'T',
                    ts: t(-3_600_000_000),
                    symbol: symbol("SPY"),
                    reason: *b"    ",
                }),
            ],
        ),
        seg(
            5,
            1,
            vec![
                add(1, "SPY", 1, true, 100, 50_000),
                add(2, "SPY", 2, true, 200, 50_000),
                add(3, "QQQ", 3, false, 50, 40_000),
                add(4, "SPY", 4, false, 300, 50_005),
            ],
        ),
        seg(
            9,
            10,
            vec![
                modify(10, 1, true, 60, 50_000),   // maintain, size down -> C 40, keeps priority
                modify(11, 2, false, 200, 50_000), // reset, same size -> C 200 + A 200 (back of the queue)
                modify(12, 4, true, 300, 50_006),  // maintain bit with a new price -> C + A, counted
                modify(13, 1, true, 60, 50_000),   // maintain, nothing changed -> no record
            ],
        ),
        seg(
            13,
            20,
            vec![
                exec(20, 1, 50, 50_000, 0),
                m(Message::Trade {
                    sale_flags: 0,
                    ts: t(21),
                    symbol: symbol("SPY"),
                    size: 100,
                    price: p(50_002),
                    trade_id: 7,
                }),
                exec(22, 2, 999, 50_000, 0),    // more than resting: clamped, counted
                exec(23, 4, 100, 50_006, 0x08), // single-price cross
            ],
        ),
        seg(13, 20, vec![exec(20, 1, 50, 50_000, 0)]), // A/B duplicate of the first message above
        seg(
            20,
            30,
            vec![
                // 17..19 missing: a gap of 3 messages
                add(30, "SPY", 5, true, 10, 50_001),
                add(31, "SPY", 2, true, 10, 50_001),
                over,
                m(Message::OrderDelete {
                    ts: t(33),
                    symbol: symbol("SPY"),
                    order_id: 4,
                }),
                m(Message::OrderDelete {
                    ts: t(34),
                    symbol: symbol("SPY"),
                    order_id: 99,
                }),
                vec![0xfe; 20],
                vec![b'a', b'8', 0, 0, 0],
                vec![],
                m(Message::TradeBreak {
                    sale_flags: 0,
                    ts: t(35),
                    symbol: symbol("SPY"),
                    size: 100,
                    price: p(50_002),
                    trade_id: 7,
                }),
                m(Message::ClearBook {
                    ts: t(36),
                    symbol: symbol("SPY"),
                }),
                add(37, "SPY", 6, false, 5, 50_010),
            ],
        ),
        seg(31, 40, vec![]), // heartbeat
    ]
}

fn spec(input: &Path, out: &Path) -> IngestSpec {
    serde_json::from_value(json!({
        "format": "iex_deep_plus",
        "inputs": [{"path": input}],
        "venue": "iex",
        "out": out,
        "symbols": ["spy"],
    }))
    .unwrap()
}

fn ev(us: i64, seq: u64, action: u8, side: u8, cents: i64, size: u64, id: u64) -> MboEvent {
    MboEvent {
        ts_event: t(us),
        ts_recv: 0,
        instrument_id: 1,
        action,
        side,
        price: if cents == i64::MAX {
            UNDEF_PRICE
        } else {
            cents * 10_000_000
        },
        size,
        order_id: id,
        flags: 0,
        sequence: seq,
        channel_id: 1,
        publisher_id: 0,
    }
}

/// Expected SPY records (instrument 1: SPY is the first symbol of the feed), `ts_recv` filled per segment.
fn expected() -> Vec<MboEvent> {
    let send = |us: i64| t(us) + 50_000;
    let mut v: Vec<(MboEvent, i64)> = vec![
        (ev(1, 5, b'A', b'B', 50_000, 100, 1), 1),
        (ev(2, 6, b'A', b'B', 50_000, 200, 2), 1),
        (ev(4, 8, b'A', b'A', 50_005, 300, 4), 1),
        (ev(10, 9, b'C', b'B', 50_000, 40, 1), 10),
        (ev(11, 10, b'C', b'B', 50_000, 200, 2), 10),
        (ev(11, 10, b'A', b'B', 50_000, 200, 2), 10),
        (ev(12, 11, b'C', b'A', 50_005, 300, 4), 10),
        (ev(12, 11, b'A', b'A', 50_006, 300, 4), 10),
        (ev(20, 13, b'T', b'A', 50_000, 50, 0), 20),
        (ev(20, 13, b'F', b'B', 50_000, 50, 1), 20),
        (ev(20, 13, b'C', b'B', 50_000, 50, 1), 20),
        (ev(21, 14, b'T', b'N', 50_002, 100, 0), 20),
        (ev(22, 15, b'T', b'A', 50_000, 999, 0), 20),
        (ev(22, 15, b'F', b'B', 50_000, 200, 2), 20),
        (ev(22, 15, b'C', b'B', 50_000, 200, 2), 20),
        (ev(23, 16, b'T', b'N', 50_006, 100, 0), 20),
        (ev(23, 16, b'F', b'A', 50_006, 100, 4), 20),
        (ev(23, 16, b'C', b'A', 50_006, 100, 4), 20),
        (ev(30, 20, b'A', b'B', 50_001, 10, 5), 30),
        (ev(31, 21, b'A', b'B', 50_001, 10, 2), 30),
        (ev(32, 22, b'T', b'A', 50_001, 10, 0), 30),
        (ev(32, 22, b'F', b'B', 50_001, 10, 2), 30),
        (ev(32, 22, b'C', b'B', 50_001, 10, 2), 30),
        (ev(33, 23, b'C', b'A', 50_006, 200, 4), 30),
        (ev(36, 29, b'R', b'N', i64::MAX, 0, 0), 30),
        (ev(37, 30, b'A', b'A', 50_010, 5, 6), 30),
    ];
    for (e, at) in &mut v {
        e.ts_recv = send(*at);
    }
    let mut out: Vec<MboEvent> = v.into_iter().map(|(e, _)| e).collect();
    for i in 0..out.len() {
        if i + 1 == out.len() || out[i + 1].sequence != out[i].sequence {
            out[i].flags |= flags::LAST;
        }
    }
    out
}

fn check_dataset(root: &Path) {
    let ds = Dataset::open(root).unwrap();
    assert_eq!(ds.manifest.venue, Venue::Nasdaq, "iex runs on Nasdaq record semantics");
    assert_eq!(ds.manifest.rule_set, Some(RuleSet::Iex));
    assert_eq!(ds.manifest.effective_rule_set(), RuleSet::Iex);
    assert_eq!(ds.manifest.source_venue, "iex");
    assert_eq!(ds.manifest.source_format, "iex_deep_plus");
    assert_eq!(
        ds.manifest.validation_status,
        qt_types::rules_for(RuleSet::Iex).validation
    );
    assert_ne!(ds.manifest.validation_status, ValidationStatus::Assumed);
    assert_eq!(ds.instruments.len(), 1);
    let i = &ds.instruments[0];
    assert_eq!((i.instrument_id, i.session_date, i.symbol.as_str()), (1, DATE, "SPY"));
    assert_eq!(
        (i.tick_size, i.lot_size, i.price_exp, i.size_exp),
        (10_000_000, 1, -9, 0)
    );
    assert_eq!((i.midnight_utc_ns, i.open_s, i.close_s), (MID, 34_200, 57_600));
    assert_eq!((i.source.as_str(), i.source_venue.as_str()), ("iex_deep_plus", "iex"));
    assert_eq!(i.session_tz, "America/New_York");
    let parts = ds.partitions();
    assert_eq!(parts.len(), 1);
    let got: Vec<MboEvent> = parts[0].mbo().unwrap().map(Result::unwrap).collect();
    let want = expected();
    for (k, (g, w)) in got.iter().zip(&want).enumerate() {
        assert_eq!(g, w, "record {k}");
    }
    assert_eq!(got.len(), want.len());
    let trades = parts[0].source_trades().unwrap().unwrap();
    let tr = |us: i64, seq: u64, cents: i64, size: u64, side: Side, kind: TradeKind, maker: Option<u64>| TradeRow {
        ts_event: t(us),
        instrument_id: 1,
        sequence: seq,
        price: cents * 10_000_000,
        size,
        aggressor_side: side,
        kind,
        maker_order_id: maker,
    };
    assert_eq!(
        trades,
        vec![
            tr(20, 13, 50_000, 50, Side::Ask, TradeKind::Displayed, Some(1)),
            tr(21, 14, 50_002, 100, Side::None, TradeKind::Hidden, None),
            tr(22, 15, 50_000, 999, Side::Ask, TradeKind::Displayed, Some(2)),
            tr(23, 16, 50_006, 100, Side::None, TradeKind::Cross, Some(4)),
            tr(32, 22, 50_001, 10, Side::Ask, TradeKind::Displayed, Some(2)),
        ]
    );
}

#[test]
fn synthetic_day_maps_every_message_type() {
    let dir = scratch("day");
    let segs = day();
    for (name, bytes, gz) in [
        ("day.pcapng.gz", pcapng(&frames(&segs)), true),
        ("day.pcap", pcap(&frames(&segs)), false),
    ] {
        let input = dir.join(name);
        write_file(&bytes, &input, gz).unwrap();
        let out = dir.join(format!("{name}.ds"));
        let s = ingest(&spec(&input, &out)).unwrap();
        check_dataset(&out);
        let dq = &s.data_quality;
        assert_eq!(dq.records, expected().len() as u64);
        assert_eq!(dq.sequence_gaps, 1, "17..19 missing");
        assert_eq!(
            dq.sequence_regressions, 0,
            "the duplicate segment is dropped, not a regression"
        );
        assert_eq!(dq.feed_gaps.len(), 1);
        let g = &dq.feed_gaps[0];
        assert_eq!((g.instrument_id, g.ts_start, g.ts_end), (1, t(23), t(30)));
        assert!(g.reason.contains("16 -> 20 (3 missing)"), "{}", g.reason);
        assert_eq!(dq.unknown_order_refs, 1, "delete of id 99");
        assert_eq!(dq.duplicate_adds, 0);
        assert_eq!(dq.hidden_prints, 1, "the T print; the cross is a cross print");
        let ss = &dq.source_specific;
        let get = |k: &str| ss.get(k).copied().unwrap_or(0);
        assert_eq!(get("cross_prints"), 1);
        assert_eq!(get("iex_modify_maintain_priority"), 3);
        assert_eq!(get("iex_modify_reset_priority"), 1);
        assert_eq!(get("iex_modify_noop"), 1);
        assert_eq!(get("iex_modify_maintain_unrepresentable"), 1);
        assert_eq!(get("iex_exec_exceeds_resting"), 1);
        assert_eq!(get("iex_duplicate_messages"), 1);
        assert_eq!(get("iex_unknown_message_type"), 1);
        assert_eq!(get("iex_short_message"), 1);
        assert_eq!(get("iex_empty_message"), 1);
        assert_eq!(get("iex_trade_breaks"), 1);
        assert_eq!(get("iex_symbols_not_found"), 0);
        // One unknown reference in 26 records is above the 1e-3 ratio of SPEC 5.1.5.
        assert_eq!(dq.verdict, Verdict::Unusable, "{:?}", dq.reasons);
        assert!(
            dq.reasons
                .iter()
                .any(|r| r.contains("unknown_order_refs / records = 1 / 26"))
        );
    }
    std::fs::remove_dir_all(dir).ok();
}

#[test]
fn venue_and_options_are_checked() {
    let dir = scratch("cfg");
    let input = dir.join("d.pcap");
    write_file(&pcap(&frames(&day())), &input, false).unwrap();
    let mut s = spec(&input, &dir.join("o1"));
    s.venue = "nasdaq".into();
    let e = ingest(&s).unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001");
    assert!(e.to_string().contains("rule set iex"), "{e}");
    let mut s = spec(&input, &dir.join("o2"));
    s.price_exp = -2;
    assert_eq!(ingest(&s).unwrap_err().code(), "QT-CONFIG-001");
    let mut s = spec(&input, &dir.join("o3"));
    s.instruments = Some(serde_json::from_value(json!({"path": "x.csv"})).unwrap());
    assert_eq!(ingest(&s).unwrap_err().code(), "QT-CONFIG-001");
    // `iexg` is an alias; no symbol filter keeps QQQ too (ids by first appearance: SPY 1, QQQ 2).
    let mut s = spec(&input, &dir.join("o4"));
    s.venue = "IEXG".into();
    s.symbols = None;
    ingest(&s).unwrap();
    let ds = Dataset::open(&dir.join("o4")).unwrap();
    let syms: Vec<(u32, &str)> = ds
        .instruments
        .iter()
        .map(|i| (i.instrument_id, i.symbol.as_str()))
        .collect();
    assert_eq!(syms, [(1, "SPY"), (2, "QQQ")]);
    std::fs::remove_dir_all(dir).ok();
}

#[test]
fn old_formats_keep_their_spec_values() {
    // The 1.2 enum values are accepted; the crypto ones are routed by the CLI / Python layer, not qt_ingest.
    for f in [
        "iex_deep_plus",
        "coinbase_level3",
        "kraken_l3",
        "hyperliquid_node",
        "itch",
        "tardis_l2",
    ] {
        let s: IngestSpec = serde_json::from_value(json!({"format": f, "inputs": [{"path": "x"}], "venue": "generic"}))
            .unwrap_or_else(|e| panic!("{f}: {e}"));
        assert_eq!(serde_json::to_value(s.format).unwrap(), json!(f));
    }
    let dir = scratch("crypto");
    let input = dir.join("x");
    std::fs::write(&input, b"{}").unwrap();
    for f in ["coinbase_level3", "kraken_l3", "hyperliquid_node"] {
        let s: IngestSpec = serde_json::from_value(json!({
            "format": f, "inputs": [{"path": input}], "venue": "generic", "out": dir.join(f),
        }))
        .unwrap();
        assert_eq!(ingest(&s).unwrap_err().code(), "QT-CONFIG-001", "{f}");
    }
    std::fs::remove_dir_all(dir).ok();
}
