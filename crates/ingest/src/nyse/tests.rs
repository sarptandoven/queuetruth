//! End-to-end tests of the NYSE TAQ Integrated Feed ingest on the hand-written fixtures of `tests/fixtures/nyse/`
//! (one NYSE channel with parity allocations, one NYSE channel with a publisher failover, a book it did not refresh
//! and corrections, and the NYSE Arca counterpart of the first, allocated by price-time), the synthetic files written
//! by `synth`, and spec errors; and properties (`props`): the FIFO ranks recomputed from the mapped records equal the
//! generator's truth for any generated day, and any input maps to a self-consistent record stream.

use std::path::{Path, PathBuf};

use qt_schema::json::{IngestSummary, Verdict};
use qt_schema::rows::TradeKind;
use qt_schema::specs::{IngestSpec, SessionTz};
use qt_types::{MboEvent, RuleSet, Side, ValidationStatus, Venue, flags};
use serde_json::{Value, json};

use super::ingest::{ALLOCATION_UNKNOWN_COUNTER, clock_warning};
use super::mapper::FAILOVER_GAP_REASON;
use super::rules::{Market, PARITY_COUNTER};
use super::synth::{SynthSpec, generate};
use crate::{Dataset, ingest};

/// 2026-01-05 00:00 US/Eastern (EST) as UTC ns.
const MID: i64 = 1_767_589_200_000_000_000;
const DATE: i32 = 20_458;
const S: i64 = 1_000_000_000;

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/nyse")
}

fn fixture(name: &str) -> PathBuf {
    fixtures().join(name)
}

const NYSE3: &str = "EQY_US_NYSE_IBF_3_20260105.csv";
const NYSE4: &str = "EQY_US_NYSE_IBF_4_20260105.csv";
const ARCA1: &str = "EQY_US_ARCA_IBF_1_20260105.csv";

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-nyse-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn spec(inputs: &[PathBuf], out: &Path, extra: Value) -> IngestSpec {
    let mut v = json!({
        "format": "nyse_ibf",
        "venue": "nyse",
        "inputs": inputs.iter().map(|p| json!({"path": p})).collect::<Vec<_>>(),
        "out": out,
    });
    if let (Some(o), Value::Object(e)) = (v.as_object_mut(), extra) {
        o.extend(e);
    }
    serde_json::from_value(v).unwrap()
}

fn run(inputs: &[PathBuf], out: &Path, extra: Value) -> IngestSummary {
    ingest(&spec(inputs, out, extra)).unwrap_or_else(|e| panic!("{e}"))
}

fn records(ds: &Dataset, iid: u32) -> Vec<MboEvent> {
    let p = ds
        .partitions()
        .iter()
        .find(|p| p.instrument_id == iid)
        .unwrap_or_else(|| panic!("no partition for {iid}"));
    p.mbo().unwrap().map(Result::unwrap).collect()
}

fn source(dq: &qt_schema::json::DataQuality, k: &str) -> u64 {
    dq.source_specific.get(k).copied().unwrap_or(0)
}

#[test]
fn nyse_channel_with_parity_allocations() {
    let out = scratch("nyse3").join("ds");
    let sum = run(&[fixture(NYSE3)], &out, json!({}));
    let ds = Dataset::open(&out).unwrap();
    let m = &ds.manifest;
    assert_eq!(
        (m.venue, m.rule_set, m.source_format.as_str()),
        (Venue::Nasdaq, Some(RuleSet::Nyse), "nyse_ibf")
    );
    assert_eq!(
        (m.source_venue.as_str(), m.validation_status),
        ("nyse", ValidationStatus::SyntheticOnly)
    );
    assert_eq!(m.effective_rule_set(), RuleSet::Nyse);
    let syms: Vec<(u32, &str)> = ds
        .instruments
        .iter()
        .map(|i| (i.instrument_id, i.symbol.as_str()))
        .collect();
    assert_eq!(syms, vec![(1, "QTN"), (2, "QTN B")]);
    for i in &ds.instruments {
        assert_eq!(
            (i.session_date, i.midnight_utc_ns, i.session_tz.as_str()),
            (DATE, MID, "America/New_York")
        );
        assert_eq!(
            (i.tick_size, i.lot_size, i.price_exp, i.size_exp),
            (10_000_000, 1, -9, 0)
        );
        assert_eq!(
            (i.open_s, i.close_s, i.source.as_str(), i.source_venue.as_str()),
            (34_200, 57_600, "nyse_ibf", "nyse")
        );
        assert_eq!(i.venue, Venue::Nasdaq);
    }
    let qtn = records(&ds, 1);
    let qtnb = records(&ds, 2);
    assert_eq!((qtn.len(), qtnb.len()), (46, 9));
    assert!(
        qtn.iter()
            .chain(&qtnb)
            .all(|e| e.channel_id == 3 && e.ts_recv == e.ts_event)
    );
    // The 800-share sell at 25.02 (09:31:30): Setter Priority (1011, behind 1010), then parity round lots.
    let fills: Vec<(u64, u64)> = qtn
        .iter()
        .filter(|e| e.action == b'F' && e.ts_event > MID + 34_290 * S && e.ts_event < MID + 34_291 * S)
        .map(|e| (e.order_id, e.size))
        .collect();
    assert_eq!(
        fills,
        vec![(1011, 500), (1010, 50), (1012, 50), (1013, 100), (1012, 100)]
    );
    // Every printable execution: T (aggressor = opposite of the resting side), F, C; LAST on the C.
    let e = qtn.iter().position(|e| e.action == b'F' && e.order_id == 1011).unwrap();
    let (t, f, c) = (qtn[e - 1], qtn[e], qtn[e + 1]);
    assert_eq!((t.action, t.side, t.order_id, t.size, t.flags), (b'T', b'A', 0, 500, 0));
    assert_eq!((f.side, c.action, c.side, c.size), (b'B', b'C', b'B', 500));
    assert!(c.is_last() && !f.is_last() && t.sequence == c.sequence && t.sequence == 41);
    assert_eq!(t.ts_event, MID + 34_290 * S + 1);
    // The opening auction: a side-N cross print and a non-printable execution (F + C, no T), the allocation flagged
    // PUBLISHER_SPECIFIC.
    let open: Vec<(u8, u8, u64, u64, u8)> = qtn
        .iter()
        .filter(|e| e.sequence == 27 || e.sequence == 28)
        .map(|e| {
            (
                e.action,
                e.side,
                e.size,
                e.order_id,
                e.flags & flags::PUBLISHER_SPECIFIC,
            )
        })
        .collect();
    let auction = flags::PUBLISHER_SPECIFIC;
    assert_eq!(
        open,
        vec![
            (b'T', b'N', 1200, 0, 0),
            (b'F', b'B', 300, 1005, auction),
            (b'C', b'B', 300, 1005, auction)
        ]
    );
    let auction_records = qtn.iter().filter(|e| e.flags & auction != 0).count();
    assert_eq!(
        auction_records, 6,
        "three non-printable executions: the open and two closing allocations"
    );
    // Modify: 1012 shrinks in place (C 50), 1013 grows to the back of the queue (C 100 + A 300).
    let modifies: Vec<(u8, u64, u64)> = qtn
        .iter()
        .filter(|e| e.sequence == 47 || e.sequence == 49)
        .map(|e| (e.action, e.order_id, e.size))
        .collect();
    assert_eq!(modifies, vec![(b'C', 1012, 50), (b'C', 1013, 100), (b'A', 1013, 300)]);
    // Prices: 25.035 hidden print exactly at 1e-9.
    assert!(
        qtn.iter()
            .any(|e| e.action == b'T' && e.side == b'N' && e.price == 25_035_000_000)
    );
    let trades = ds.partitions()[0].source_trades().unwrap().unwrap();
    let kinds: Vec<TradeKind> = trades.iter().map(|t| t.kind).collect();
    assert_eq!(
        kinds.iter().filter(|k| **k == TradeKind::Cross).count(),
        2,
        "QTN opening and closing crosses"
    );
    assert_eq!(kinds.len(), 9);
    assert!(
        trades
            .iter()
            .any(|t| t.maker_order_id == Some(1011) && t.aggressor_side == Side::Ask)
    );
    // Data quality: the book is clean; the parity label makes the verdict `warnings`.
    let dq = &sum.data_quality;
    assert_eq!(dq.records, 55);
    assert_eq!(
        (
            dq.unknown_order_refs,
            dq.duplicate_adds,
            dq.sequence_gaps,
            dq.sequence_regressions,
            dq.out_of_order_ts_event
        ),
        (0, 0, 0, 0, 0)
    );
    assert_eq!((dq.hidden_prints, source(dq, "cross_prints")), (1, 3));
    // The label counts the instrument-days of the parity market (QTN and QTN B), the INFO detail the executions.
    assert_eq!(
        (source(dq, PARITY_COUNTER), source(dq, "nyse_parity_executions")),
        (2, 10)
    );
    assert_eq!(
        (
            source(dq, "nyse_modify_keep_priority"),
            source(dq, "nyse_modify_lose_priority")
        ),
        (1, 1)
    );
    assert_eq!(
        (
            source(dq, "nyse_replace_orders"),
            source(dq, "nyse_nonprintable_executions")
        ),
        (1, 3)
    );
    assert_eq!(
        (
            source(dq, "nyse_nonprintable_hidden_trades"),
            source(dq, "nyse_attributed_adds")
        ),
        (3, 1)
    );
    assert_eq!(dq.verdict, Verdict::Warnings);
    assert_eq!(dq.reasons[0], format!("{PARITY_COUNTER} = 2"));
    assert!(
        dq.reasons[1].starts_with("warning: NYSE allocates executions by Setter Priority"),
        "{:?}",
        dq.reasons
    );
    assert_eq!(dq.reasons.len(), 2, "{:?}", dq.reasons);
    assert_eq!(sum.warnings.len(), 1);
    assert!(
        sum.warnings[0].contains("2 instrument-day(s)") && sum.warnings[0].contains("10 displayed execution(s)"),
        "{}",
        sum.warnings[0]
    );
    assert!(dq.feed_gaps.is_empty());
}

#[test]
fn arca_counterpart_is_clean() {
    let out = scratch("arca1").join("ds");
    let sum = run(&[fixture(ARCA1)], &out, json!({}));
    let ds = Dataset::open(&out).unwrap();
    assert_eq!(
        (ds.manifest.source_venue.as_str(), ds.manifest.rule_set),
        ("nyse_arca", Some(RuleSet::Nyse))
    );
    assert!(ds.instruments.iter().all(|i| i.source_venue == "nyse_arca"));
    let fills: Vec<(u64, u64)> = records(&ds, 1)
        .iter()
        .filter(|e| e.action == b'F' && e.ts_event > MID + 34_290 * S && e.ts_event < MID + 34_291 * S)
        .map(|e| (e.order_id, e.size))
        .collect();
    assert_eq!(fills, vec![(1010, 50), (1011, 500), (1012, 250)], "price-time: by time");
    let dq = &sum.data_quality;
    assert_eq!(
        (dq.verdict, dq.reasons.len(), sum.warnings.len()),
        (Verdict::Clean, 0, 0),
        "{:?}",
        dq.reasons
    );
    assert_eq!(source(dq, PARITY_COUNTER), 0);
    // strict passes on a clean dataset and fails on the parity label (QT-DATA-002 names the reason).
    let ok = scratch("arca1-strict").join("ds");
    run(&[fixture(ARCA1)], &ok, json!({"strict": true}));
    let bad = scratch("nyse3-strict").join("ds");
    let e = ingest(&spec(&[fixture(NYSE3)], &bad, json!({"strict": true}))).unwrap_err();
    assert_eq!(e.code(), "QT-DATA-002");
    assert!(e.to_string().contains("nyse_parity_ranks_approximate = 2"), "{e}");
}

#[test]
fn failover_and_corrections_channel() {
    let out = scratch("nyse4").join("ds");
    let sum = run(&[fixture(NYSE4)], &out, json!({}));
    let ds = Dataset::open(&out).unwrap();
    let qtf = records(&ds, 1);
    let actions: String = qtf.iter().map(|e| e.action as char).collect();
    // adds, an execution (T F C), an add, the refresh (R + 2 A), adds, the closing cross, deletes
    assert_eq!(actions, "AAATFCARAAAATCCCC");
    let r = qtf.iter().find(|e| e.action == b'R').unwrap();
    assert_eq!(
        (r.sequence, r.ts_event, r.price),
        (24, MID + 36_005 * S, qt_types::UNDEF_PRICE)
    );
    // QTG lost both its orders in the outage, so the backup re-sent nothing for it: its book is cleared before its
    // next add, an offer at 29.90 that would have crossed the stale 30.00 bid.
    let qtg = records(&ds, 2);
    let g: Vec<(u8, u64, u64)> = qtg.iter().map(|e| (e.action, e.order_id, e.sequence)).collect();
    assert_eq!(
        g,
        vec![
            (b'A', 5001, 11),
            (b'A', 5002, 22),
            (b'R', 0, 29),
            (b'A', 5003, 29),
            (b'C', 5003, 44)
        ]
    );
    assert_eq!(qtg[2].ts_event, MID + 36_120 * S);
    let dq = &sum.data_quality;
    assert_eq!(dq.records, 22);
    assert_eq!(
        (
            source(dq, "nyse_refresh_runs"),
            source(dq, "nyse_failover_books_cleared"),
            source(dq, PARITY_COUNTER)
        ),
        (1, 1, 2)
    );
    assert_eq!(
        (
            source(dq, "nyse_unknown_message_type"),
            source(dq, "nyse_symbol_sequence_missing")
        ),
        (1, 2)
    );
    // Trade Cancel and Cross Correction are INFO (book builders may ignore them, TAQ spec sections 14 and 15).
    assert_eq!(
        (source(dq, "nyse_trade_cancels"), source(dq, "nyse_cross_corrections")),
        (1, 1)
    );
    assert_eq!((dq.sequence_gaps, dq.unknown_order_refs), (1, 0));
    let gaps: Vec<(u32, i64, i64, &str)> = dq
        .feed_gaps
        .iter()
        .map(|g| (g.instrument_id, g.ts_start, g.ts_end, g.reason.as_str()))
        .collect();
    assert_eq!(
        gaps,
        vec![
            // 09:42:00 -> 10:01:00: the refresh
            (
                1,
                MID + 34_920 * S,
                MID + 36_060 * S,
                "nyse book refresh (Add Order Refresh run): the book was cleared and rebuilt"
            ),
            // 10:30:00 -> 11:00:00: two lost messages
            (
                1,
                MID + 37_800 * S,
                MID + 39_600 * S,
                "nyse symbol sequence gap: SymbolSeqNum 11 -> 14 (2 message(s) missing)"
            ),
            // 09:45:00 -> 10:02:00: QTG's book cleared without a refresh
            (2, MID + 35_100 * S, MID + 36_120 * S, FAILOVER_GAP_REASON),
        ]
    );
    assert_eq!(dq.verdict, Verdict::Warnings);
    for want in [
        "feed_gaps = 3",
        "sequence_gaps = 1",
        "nyse_failover_books_cleared = 1",
        "nyse_refresh_runs = 1",
        "nyse_unknown_message_type = 1",
        "nyse_parity_ranks_approximate = 2",
    ] {
        assert!(dq.reasons.iter().any(|r| r == want), "{want}: {:?}", dq.reasons);
    }
    assert!(
        !dq.reasons
            .iter()
            .any(|r| r.starts_with("nyse_trade_cancels") || r.starts_with("nyse_cross_corrections")),
        "{:?}",
        dq.reasons
    );
    for want in [
        "1 Add Order Refresh run(s)",
        "1 book(s) cleared after a publisher failover",
    ] {
        assert!(
            sum.warnings.iter().any(|w| w.starts_with(want)),
            "{want}: {:?}",
            sum.warnings
        );
    }
}

/// The label follows the market, not the executions: an NYSE day without a single execution is labelled too, and a
/// day whose market cannot be told (a renamed file without Symbol Index Mapping lines) is counted as unknown.
#[test]
fn the_label_follows_the_market_not_the_executions() {
    let dir = scratch("noexec");
    let lines = "100,2,09:30:01.000000000,QTQ,1,7,10.00,300,B,,\n100,3,09:30:01.000000001,QTQ,2,8,10.05,300,S,,\n\
                 100,5,09:30:02.000000000,QTQ,3,9,10.01,100,B,,\n102,7,09:30:03.000000000,QTQ,4,9,\n";
    let nyse = dir.join("EQY_US_NYSE_IBF_2_20260105.csv");
    std::fs::write(&nyse, lines).unwrap();
    let sum = run(std::slice::from_ref(&nyse), &dir.join("nyse"), json!({}));
    let dq = &sum.data_quality;
    assert_eq!(
        (
            source(dq, PARITY_COUNTER),
            source(dq, "nyse_parity_executions"),
            dq.verdict
        ),
        (1, 0, Verdict::Warnings)
    );
    assert_eq!(dq.reasons[0], format!("{PARITY_COUNTER} = 1"));
    assert!(
        sum.warnings.iter().any(|w| w.contains("0 displayed execution(s)")),
        "{:?}",
        sum.warnings
    );
    let e = ingest(&spec(
        std::slice::from_ref(&nyse),
        &dir.join("strict"),
        json!({"strict": true}),
    ))
    .unwrap_err();
    assert_eq!(e.code(), "QT-DATA-002");
    let renamed = dir.join("day.csv");
    std::fs::write(&renamed, lines).unwrap();
    let sum = run(
        std::slice::from_ref(&renamed),
        &dir.join("unknown"),
        json!({"session_date": "2026-01-05"}),
    );
    let dq = &sum.data_quality;
    assert_eq!(
        (source(dq, ALLOCATION_UNKNOWN_COUNTER), source(dq, PARITY_COUNTER)),
        (1, 0)
    );
    assert!(
        dq.reasons.iter().any(|r| r == "nyse_allocation_unknown = 1"),
        "{:?}",
        dq.reasons
    );
    assert!(
        sum.warnings.iter().any(|w| w.contains("market is unknown")),
        "{:?}",
        sum.warnings
    );
}

/// A file without one order message (an Imbalances file renamed, say) is not an Integrated Feed channel day: it is
/// counted and named instead of ingesting as a clean empty dataset.
#[test]
fn a_file_without_order_messages_is_counted() {
    let dir = scratch("noorders");
    let imb = dir.join("EQY_US_NYSE_IBF_5_20260105.csv");
    std::fs::write(
        &imb,
        "3,2,QTQ,1,1,N,C,100,,,0,Y,1,100\n34,3,09:30:00.000000000,QTQ,1,O,~,,,,,,~,O\n\
         105,5,09:31:00.000000000,QTQ,2,25.00,900,300,,0930,M,S,,,,,,,0,0,0,,,\n",
    )
    .unwrap();
    let sum = run(std::slice::from_ref(&imb), &dir.join("ds"), json!({}));
    let dq = &sum.data_quality;
    assert_eq!(
        (dq.records, source(dq, "nyse_files_without_order_messages"), dq.verdict),
        (0, 1, Verdict::Warnings)
    );
    let named = |w: &String| w.starts_with("1 input file(s) hold no order message") && w.contains("IBF_5_20260105");
    assert!(sum.warnings.iter().any(named), "{:?}", sum.warnings);
}

/// Inputs are read in canonical order: two channels given in either order, raw or gzip, give the same dataset.
#[test]
fn input_order_and_compression_do_not_change_the_dataset() {
    let dir = scratch("order");
    let gz3 = dir.join(format!("{NYSE3}.gz"));
    let mut e = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
    std::io::Write::write_all(&mut e, &std::fs::read(fixture(NYSE3)).unwrap()).unwrap();
    std::fs::write(&gz3, e.finish().unwrap()).unwrap();
    let a = run(&[fixture(NYSE3), fixture(NYSE4)], &dir.join("a"), json!({}));
    let b = run(&[fixture(NYSE4), gz3], &dir.join("b"), json!({}));
    assert_eq!(a.manifest.dataset_id, b.manifest.dataset_id);
    assert_eq!(a.manifest.partitions, b.manifest.partitions);
    assert_eq!(a.data_quality, b.data_quality);
    let ds = Dataset::open(&dir.join("a")).unwrap();
    let syms: Vec<(u32, &str)> = ds
        .instruments
        .iter()
        .map(|i| (i.instrument_id, i.symbol.as_str()))
        .collect();
    assert_eq!(syms, vec![(1, "QTN"), (2, "QTN B"), (3, "QTF"), (4, "QTG")]);
    let channels: Vec<u16> = (1..=4).map(|i| records(&ds, i)[0].channel_id).collect();
    assert_eq!(channels, vec![3, 3, 4, 4]);
    assert_eq!(a.data_quality.source_specific.get(PARITY_COUNTER), Some(&4));
    assert_eq!(a.data_quality.source_specific.get("nyse_parity_executions"), Some(&11));
}

#[test]
fn symbol_filter_keeps_ids() {
    let out = scratch("filter").join("ds");
    let sum = run(&[fixture(NYSE3)], &out, json!({"symbols": ["qtn b", "NOPE"]}));
    let ds = Dataset::open(&out).unwrap();
    let ids: Vec<u32> = ds.partitions().iter().map(|p| p.instrument_id).collect();
    assert_eq!(ids, vec![2], "QTN B keeps id 2 (first appearance before the filter)");
    let dq = &sum.data_quality;
    assert_eq!(
        (
            dq.records,
            source(dq, "nyse_symbols_not_found"),
            source(dq, PARITY_COUNTER),
            source(dq, "nyse_parity_executions")
        ),
        (9, 1, 1, 1)
    );
    assert!(
        sum.warnings
            .iter()
            .any(|w| w.contains("NOPE") && w.contains("\"BRK A\"")),
        "{:?}",
        sum.warnings
    );
}

#[test]
fn spec_and_input_errors() {
    let dir = scratch("errors");
    let out = dir.join("ds");
    let code = |inputs: &[PathBuf], extra: Value| {
        ingest(&spec(inputs, &out, extra))
            .err()
            .map(|e| (e.code(), e.to_string()))
    };
    let n3 = [fixture(NYSE3)];
    let cases: Vec<(Value, &str)> = vec![
        (json!({"venue": "nasdaq"}), "uses rule set nyse"),
        (json!({"price_exp": -2}), "price_exp"),
        (json!({"size_exp": 1}), "size_exp"),
        (json!({"instruments": {"path": "i.csv"}}), "instruments"),
        (json!({"options": {"depth": 10}}), "options.depth"),
        (
            json!({"options": {"check_symbol_sequence": "yes"}}),
            "check_symbol_sequence",
        ),
        (json!({"options": {"open_s": 1}, "open_s": 2}), "disagree"),
        (json!({"open_s": 60_000}), "open_s / close_s"),
        (json!({"tick_size": "0"}), "tick_size"),
        (json!({"lot_size": "0"}), "lot_size"),
        (json!({"session_date": "2026-01-06"}), "is named for 2026-01-05"),
        (
            json!({"midnight_utc_ns": MID + 1}),
            "is not the America/New_York midnight",
        ),
    ];
    for (extra, want) in cases {
        let (c, msg) = code(&n3, extra.clone()).unwrap_or_else(|| panic!("{extra} passed"));
        assert_eq!(c, "QT-CONFIG-001", "{extra}: {msg}");
        assert!(msg.contains(want), "{extra}: {msg}");
    }
    let renamed = dir.join("channel.csv");
    std::fs::copy(fixture(NYSE3), &renamed).unwrap();
    let (c, msg) = code(std::slice::from_ref(&renamed), json!({})).unwrap();
    assert!(c == "QT-CONFIG-001" && msg.contains("pass session_date"), "{msg}");
    let summary = dir.join("EQY_US_NYSE_STOCKSUM_20260105.csv");
    std::fs::write(&summary, "223,2,16:00:00.000000000,QTN,25.1,24.9,25.0,25.02,5000\n").unwrap();
    let (_, msg) = code(&[summary], json!({})).unwrap();
    assert!(msg.contains("stock summary file"), "{msg}");
    let (_, msg) = code(&[fixture(NYSE3), fixture(ARCA1)], json!({})).unwrap();
    assert!(msg.contains("NYSE, NYSE Arca"), "{msg}");
    let (_, msg) = code(&[fixture(NYSE3), fixture(NYSE3)], json!({})).unwrap();
    assert!(msg.contains("both channel 3 of 2026-01-05"), "{msg}");
    // A file of another product fails on its first lines with QT-INPUT-002.
    let bbo = dir.join("EQY_US_NYSE_IBF_9_20260105.csv");
    std::fs::write(
        &bbo,
        "3,2,QTN,1,1,N,C,100,,,0,Y,1,100\n140,3,09:30:00.000000000,QTN,1,25.1,100,25.0,100,R,\n",
    )
    .unwrap();
    let (c, msg) = code(&[bbo], json!({})).unwrap();
    assert!(c == "QT-INPUT-002" && msg.contains("TAQ BBO, Trades or TRF"), "{msg}");
    let header = dir.join("EQY_US_NYSE_IBF_8_20260105.csv");
    std::fs::write(&header, "MsgType,SequenceNumber\n").unwrap();
    let (c, msg) = code(&[header], json!({})).unwrap();
    assert!(
        c == "QT-INPUT-002" && msg.contains("line 1") && msg.contains("not a TAQ Integrated Feed file"),
        "{msg}"
    );
    // A renamed file with an explicit session date and UTC clock reads fine (channel 0, market from the data).
    let ok = run(
        std::slice::from_ref(&renamed),
        &dir.join("ok"),
        json!({"session_date": "2026-01-05", "tz": "UTC"}),
    );
    let ds = Dataset::open(&dir.join("ok")).unwrap();
    assert_eq!(
        (ds.manifest.source_venue.as_str(), records(&ds, 1)[0].channel_id),
        ("nyse", 0)
    );
    assert_eq!(ds.instruments[0].midnight_utc_ns, MID - 5 * 3_600 * S);
    // The clock check notices that the core session opens at 09:30 in the file, which is not 09:30 UTC.
    assert!(
        ok.warnings
            .iter()
            .any(|w| w.contains("ingest again with tz America/New_York")),
        "{:?}",
        ok.warnings
    );
}

#[test]
fn clock_check() {
    let open = 34_200 * 1_000_000_000u64;
    let h = 3_600 * 1_000_000_000u64;
    assert_eq!(clock_warning("f", &[], SessionTz::AmericaNewYork, DATE), None);
    assert_eq!(
        clock_warning(
            "f",
            &[open, open + 2, open + 30_000_000_000],
            SessionTz::AmericaNewYork,
            DATE
        ),
        None
    );
    // EST date: 09:30 Eastern is 14:30 UTC; EDT date (2026-07-01, day 20635): 13:30 UTC.
    let w = clock_warning("f", &[open + 5 * h], SessionTz::AmericaNewYork, DATE).unwrap();
    assert!(
        w.contains("in UTC time") && w.contains("ingest again with tz UTC") && w.contains("14:30:00"),
        "{w}"
    );
    assert_eq!(clock_warning("f", &[open + 5 * h], SessionTz::Utc, DATE), None);
    assert_eq!(clock_warning("f", &[open + 4 * h], SessionTz::Utc, 20_635), None);
    let w = clock_warning("f", &[open + 2 * h, open + 2 * h], SessionTz::AmericaNewYork, DATE).unwrap();
    assert!(w.contains("neither 09:30 US/Eastern nor its UTC time 14:30:00"), "{w}");
}

#[test]
fn daylight_saving_switch_day_and_midnight_options() {
    let dir = scratch("dst");
    // 2026-03-08 is the US spring-forward Sunday.
    let f = dir.join("EQY_US_ARCA_IBF_2_20260308.csv");
    std::fs::write(
        &f,
        "3,2,QTA,3,1,P,C,100,,,0,Y,1,100\n100,3,09:30:00.000000000,QTA,1,7,10.00,100,B,,\n",
    )
    .unwrap();
    let sum = run(std::slice::from_ref(&f), &dir.join("ds"), json!({}));
    assert!(
        sum.warnings
            .iter()
            .any(|w| w.contains("2026-03-08 is a US daylight-saving switch day")),
        "{:?}",
        sum.warnings
    );
    // midnight_utc_ns alone names the session date; options open_s / close_s / memory_rows are honoured.
    let mid = crate::time::midnight_utc_ns(20_520, SessionTz::AmericaNewYork).unwrap();
    run(
        std::slice::from_ref(&f),
        &dir.join("ds2"),
        json!({"midnight_utc_ns": mid, "options": {"open_s": 34_000, "close_s": 58_000, "memory_rows": 1}}),
    );
    let ds = Dataset::open(&dir.join("ds2")).unwrap();
    assert_eq!(
        (
            ds.instruments[0].open_s,
            ds.instruments[0].close_s,
            ds.instruments[0].midnight_utc_ns
        ),
        (34_000, 58_000, mid)
    );
}

/// NYSE American allocated on parity as the Floor-based NYSE MKT until its move to Pillar on 2017-07-24: an
/// `EQY_US_MKT_IBF_*` day carries the parity label, the same messages on a later NYSE American day do not.
#[test]
fn nyse_mkt_days_before_pillar_are_labelled_parity() {
    let dir = scratch("mkt");
    // An add, a printable execution of 100 of its 300 shares and a delete; the same lines on both days.
    let lines = "3,2,QTM,9,1,A,C,100,,,0,Y,1,100\n34,3,09:30:00.000000000,QTM,1,O,~,,,,,,~,O\n\
                 100,5,09:30:01.000000000,QTM,2,7,10.00,300,B,,\n103,7,09:30:02.000000000,QTM,3,7,1,10.00,100,1,,@,,,\n\
                 102,9,09:30:03.000000000,QTM,4,7,\n";
    let mkt = dir.join("EQY_US_MKT_IBF_1_20170301.csv");
    std::fs::write(&mkt, lines).unwrap();
    let sum = run(std::slice::from_ref(&mkt), &dir.join("mkt"), json!({}));
    let dq = &sum.data_quality;
    assert_eq!((source(dq, PARITY_COUNTER), dq.verdict), (1, Verdict::Warnings));
    assert_eq!(dq.reasons[0], format!("{PARITY_COUNTER} = 1"));
    assert!(
        sum.warnings
            .iter()
            .any(|w| w.starts_with("NYSE MKT (NYSE American before its move to Pillar")),
        "{:?}",
        sum.warnings
    );
    assert_eq!(sum.manifest.source_venue, "nyse_american");
    let amex = dir.join("EQY_US_AMEX_IBF_1_20260105.csv");
    std::fs::write(&amex, lines).unwrap();
    let sum = run(std::slice::from_ref(&amex), &dir.join("amex"), json!({}));
    let dq = &sum.data_quality;
    assert_eq!(
        (source(dq, PARITY_COUNTER), dq.verdict, sum.warnings.len()),
        (0, Verdict::Clean, 0),
        "{:?}",
        dq.reasons
    );
}

#[test]
fn symbol_sequence_check_can_be_turned_off() {
    let out = scratch("noseq").join("ds");
    let sum = run(
        &[fixture(NYSE4)],
        &out,
        json!({"options": {"check_symbol_sequence": false}}),
    );
    let dq = &sum.data_quality;
    assert_eq!(
        (dq.sequence_gaps, dq.feed_gaps.len()),
        (0, 2),
        "only the refresh gap and the cleared book are left"
    );
}

fn synth_spec(market: Market) -> SynthSpec {
    SynthSpec {
        market,
        symbols: vec!["QTS".into(), "QTT A".into()],
        events_per_symbol: 3_000,
        seed: 20_260_106,
        floor_brokers: 3,
    }
}

const SYNTH: [(&str, Market); 2] = [
    ("EQY_US_NYSE_IBF_7_20260106.csv", Market::Nyse),
    ("EQY_US_ARCA_IBF_7_20260106.csv", Market::Arca),
];

/// The synthetic fixtures are the generator's output (regenerate with `QT_BLESS=1`).
#[test]
fn synthetic_fixtures_are_current() {
    for (name, market) in SYNTH {
        let day = generate(&synth_spec(market));
        let path = fixture(name);
        if std::env::var_os("QT_BLESS").is_some() {
            std::fs::write(&path, &day.csv).unwrap();
        }
        let committed =
            std::fs::read(&path).unwrap_or_else(|e| panic!("{}: {e} (run once with QT_BLESS=1)", path.display()));
        assert!(
            committed == day.csv,
            "{name} differs from synth::generate (QT_BLESS=1 rewrites it)"
        );
    }
}

/// The synthetic days ingest without an anomaly; on the parity market a share of the executions are behind the
/// head of the FIFO queue, on the price-time market none are (ground truth for `replay --rank-oracle`).
#[test]
fn synthetic_days_ingest_cleanly() {
    for (name, market) in SYNTH {
        let day = generate(&synth_spec(market));
        let out = scratch(name).join("ds");
        let sum = run(&[fixture(name)], &out, json!({}));
        let dq = &sum.data_quality;
        assert_eq!(
            (
                dq.unknown_order_refs,
                dq.duplicate_adds,
                dq.sequence_gaps,
                dq.sequence_regressions,
                dq.out_of_order_ts_event
            ),
            (0, 0, 0, 0, 0),
            "{name}"
        );
        let behind = day.executions.iter().filter(|e| e.fifo_rank > 0).count();
        match market {
            Market::Nyse => {
                assert_eq!(
                    (source(dq, PARITY_COUNTER), source(dq, "nyse_parity_executions")),
                    (2, day.executions.len() as u64)
                );
                assert!(behind > 0);
                assert_eq!(dq.reasons[0], format!("{PARITY_COUNTER} = 2"));
            }
            _ => {
                assert_eq!(behind, 0);
                assert_eq!(dq.verdict, Verdict::Clean, "{:?}", dq.reasons);
            }
        }
        let ds = Dataset::open(&out).unwrap();
        let fills: usize = ds
            .partitions()
            .iter()
            .map(|p| p.mbo().unwrap().filter(|e| e.as_ref().unwrap().action == b'F').count())
            .sum();
        assert_eq!(fills, day.executions.len(), "{name}");
        assert!(ds.partitions().iter().all(|p| p.mbo().unwrap().all(|e| {
            let e = e.unwrap();
            e.action != b'F' || e.flags & flags::LAST == 0
        })));
    }
}

/// Writes a large synthetic NYSE day for throughput measurements of the release CLI (`QT_NYSE_SYNTH_OUT=<file>
/// cargo test -p queuetruth-ingest --lib nyse::tests::write_large_synthetic_day -- --ignored`;
/// `QT_NYSE_SYNTH_EVENTS` sets the events per symbol, default 400,000 on 4 symbols).
#[test]
#[ignore]
fn write_large_synthetic_day() {
    let Some(path) = std::env::var_os("QT_NYSE_SYNTH_OUT") else {
        panic!("set QT_NYSE_SYNTH_OUT to the file to write");
    };
    let events: usize = std::env::var("QT_NYSE_SYNTH_EVENTS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(400_000);
    let day = generate(&SynthSpec {
        market: Market::Nyse,
        symbols: vec!["QTA".into(), "QTB".into(), "QTC A".into(), "QTD".into()],
        events_per_symbol: events,
        seed: 1,
        floor_brokers: 4,
    });
    std::fs::write(&path, &day.csv).unwrap();
    let mut hist = [0usize; 6];
    for e in &day.executions {
        hist[(e.fifo_rank as usize).min(5)] += 1;
    }
    println!(
        "wrote {}: {} lines, {} bytes, {} executions, FIFO rank histogram {hist:?}",
        PathBuf::from(&path).display(),
        day.messages,
        day.csv.len(),
        day.executions.len()
    );
}

/// The generator's ground truth for the synthetic fixtures, pinned: the FIFO rank of every execution (0, 1, 2, 3,
/// 4, 5 or more orders ahead at its price). `replay --rank-oracle` on the ingested files must report the same
/// histogram (docs/how-to/nyse-order-level.md).
#[test]
fn synthetic_truth_is_pinned() {
    let want: [(u64, usize, u64, [usize; 6]); 2] = [
        (8_468, 3_678, 364_335, [2_665, 444, 218, 107, 60, 184]),
        (6_798, 1_982, 345_251, [1_982, 0, 0, 0, 0, 0]),
    ];
    for ((name, market), (lines, execs, shares, hist)) in SYNTH.into_iter().zip(want) {
        let day = generate(&synth_spec(market));
        let mut h = [0usize; 6];
        for e in &day.executions {
            h[(e.fifo_rank as usize).min(5)] += 1;
        }
        let total: u64 = day.executions.iter().map(|e| e.volume).sum();
        assert_eq!(
            (day.messages, day.executions.len(), total, h),
            (lines, execs, shares, hist),
            "{name}"
        );
    }
}

// ---- properties ---------------------------------------------------------------------------------------------------

mod props {
    use std::collections::HashMap;

    use proptest::prelude::*;
    use qt_types::MboEvent;

    use crate::nyse::mapper::{Anomalies, FileCtx, MapOutput, Mapper};
    use crate::nyse::message::parse_line;
    use crate::nyse::rules::{Allocation, Market};
    use crate::nyse::synth::{SynthSpec, generate};

    /// A FIFO book rebuilt from the mapped records alone (independent of the mapper's own order table): price levels
    /// in time order per `(instrument, side, price)`. It panics on a record stream that is not self-consistent (a `C`
    /// or `F` of an order that is not live, or a `C` larger than the order), and records the FIFO rank (orders ahead
    /// at the price) of every `F`.
    #[derive(Default)]
    struct FifoBook {
        levels: HashMap<(u32, u8, i64), Vec<u64>>,
        orders: HashMap<(u32, u64), (u8, i64, u64)>,
        ranks: Vec<u32>,
    }

    impl FifoBook {
        fn apply(&mut self, e: &MboEvent) {
            let key = (e.instrument_id, e.order_id);
            match e.action {
                b'A' => {
                    assert!(e.size > 0 && e.price > 0, "add {e:?}");
                    assert!(
                        self.orders.insert(key, (e.side, e.price, e.size)).is_none(),
                        "live id added: {e:?}"
                    );
                    self.levels
                        .entry((e.instrument_id, e.side, e.price))
                        .or_default()
                        .push(e.order_id);
                }
                b'C' => {
                    let (side, price, size) = *self
                        .orders
                        .get(&key)
                        .unwrap_or_else(|| panic!("cancel of no order {e:?}"));
                    assert!(
                        e.side == side && e.price == price,
                        "cancel at another side or price {e:?}"
                    );
                    let left = size
                        .checked_sub(e.size)
                        .unwrap_or_else(|| panic!("cancel above size {e:?}"));
                    if left == 0 {
                        self.orders.remove(&key);
                        let level = (e.instrument_id, side, price);
                        let q = self
                            .levels
                            .get_mut(&level)
                            .unwrap_or_else(|| panic!("no level for {e:?}"));
                        q.retain(|&id| id != e.order_id);
                        if q.is_empty() {
                            self.levels.remove(&level);
                        }
                    } else {
                        self.orders.insert(key, (side, price, left));
                    }
                }
                b'F' => {
                    let (side, price, size) = *self
                        .orders
                        .get(&key)
                        .unwrap_or_else(|| panic!("fill of no order {e:?}"));
                    assert!(e.size <= size && e.side == side, "fill {e:?}");
                    let q = &self.levels[&(e.instrument_id, side, price)];
                    let rank = q
                        .iter()
                        .position(|&id| id == e.order_id)
                        .unwrap_or_else(|| panic!("{e:?}"));
                    self.ranks.push(rank as u32);
                }
                b'R' => {
                    self.orders.retain(|k, _| k.0 != e.instrument_id);
                    self.levels.retain(|k, _| k.0 != e.instrument_id);
                }
                b'T' => assert_eq!(e.order_id, 0, "{e:?}"),
                other => panic!("unexpected action {} in {e:?}", other as char),
            }
        }
    }

    fn mapper(market: Option<Market>) -> Mapper {
        let mut m = Mapper::new(None, -9, true).unwrap();
        m.begin_file(FileCtx {
            date: 20_459,
            midnight: 1_767_675_600_000_000_000, // 2026-01-06 00:00 US/Eastern
            channel: 7,
            market,
            label: "prop.csv".into(),
        });
        m
    }

    /// Maps every line (`Err` results are allowed and skipped), ends the file and feeds the records to a `FifoBook`.
    fn map_all<'a>(m: &mut Mapper, lines: impl IntoIterator<Item = &'a [u8]>) -> FifoBook {
        let mut book = FifoBook::default();
        let mut out = MapOutput::default();
        for (i, line) in lines.into_iter().enumerate() {
            if m.apply_line(line, i as u64 + 1, &mut out).is_ok() {
                if let Some((last, rest)) = out.mbo.split_last() {
                    assert!(
                        last.is_last() && rest.iter().all(|e| !e.is_last()),
                        "LAST flags {:?}",
                        out.mbo
                    );
                    assert!(out.mbo.iter().all(|e| e.sequence == last.sequence), "{:?}", out.mbo);
                }
                for e in &out.mbo {
                    book.apply(e);
                }
            }
        }
        // The end of the file clears the books a failover left unsettled: one `R` message each.
        m.end_file(&mut out);
        for e in &out.mbo {
            assert!(e.action == b'R' && e.is_last(), "{e:?}");
            book.apply(e);
        }
        book
    }

    /// One field value: from `bad` when the pick byte is below 16 (1 in 16), else from `good`.
    fn val(b: u8, good: &[&'static str], bad: &[&'static str]) -> &'static str {
        let b = usize::from(b);
        if b < 16 && !bad.is_empty() {
            bad[b % bad.len()]
        } else {
            good[b % good.len()]
        }
    }

    /// One line of message type `t` whose fields are drawn by `picks` (mostly valid values, 1 in 16 a garbage value),
    /// cut to `cut` body fields when that is shorter than the layout (old or broken layouts). Order ids, prices and
    /// sizes come from small sets, so ids collide (duplicates, unknown references, executions of live orders) and
    /// the symbol sequence is drawn too (gaps, repeats, regressions).
    fn fuzz_line(t: u16, seq: u64, time: &str, sym: &str, picks: &[u8], cut: usize) -> Vec<u8> {
        let id = |b| val(b, &["11", "12", "13", "14", "15"], &["x", "0", "18446744073709551616"]);
        let px = |b| {
            val(
                b,
                &["25.10", "25.11", "25.12", "25.105", "25.1", "0.0001"],
                &["", "1.0000000001", "1e3"],
            )
        };
        let vol = |b| val(b, &["100", "300", "50", "1", "200"], &["", "0", "1e3"]);
        let side = |b| val(b, &["B", "S"], &["X", "", "BS"]);
        let flag = |b| val(b, &["1", "0", "1"], &["", "2"]);
        let pos = |b| val(b, &["", "0", "", "1"], &["2"]);
        let cond = |b| val(b, &["", "@", "I"], &["XX"]);
        let info = |b| val(b, &["", "0", "N", "~", "O", "X", "25.10"], &["??"]);
        let tid = |b| val(b, &["1", "2", "3"], &["x"]);
        let p = |k: usize| picks[k % picks.len()];
        let body: Vec<&str> = match t {
            100 | 106 => vec![
                id(p(2)),
                px(p(3)),
                vol(p(4)),
                side(p(5)),
                val(p(6), &["", "", "ABCD"], &[]),
                "",
            ],
            101 => vec![id(p(2)), px(p(3)), vol(p(4)), pos(p(5)), side(p(6)), ""],
            102 => vec![id(p(2)), ""],
            103 => vec![
                id(p(2)),
                tid(p(3)),
                px(p(4)),
                vol(p(5)),
                flag(p(6)),
                "",
                cond(p(7)),
                "",
                "",
                cond(p(8)),
            ],
            104 => vec![id(p(2)), id(p(3)), px(p(4)), vol(p(5)), side(p(6)), ""],
            110 => vec![tid(p(2)), px(p(3)), vol(p(4)), flag(p(5)), cond(p(6)), "", "", ""],
            111 => vec![tid(p(2)), px(p(3)), vol(p(4)), val(p(5), &["O", "6", "E", ""], &["OO"])],
            112 => vec![tid(p(2))],
            113 => vec![tid(p(2)), vol(p(3))],
            34 | 105 | 114 => (0..19).map(|k| info(p(k + 2))).collect(),
            _ => vec![info(p(2)), id(p(3)), px(p(4)), vol(p(5))],
        };
        let body = body[..cut.clamp(1, body.len())].join(",");
        let symseq = val(p(0), &["1", "2", "3", "4", "5"], &["", "x"]);
        let line = match t {
            3 => format!(
                "3,{seq},{sym},{},1,N,C,100,,,0,Y,1,100",
                val(p(1), &["1", "1", "3"], &["4", "x"])
            ),
            223 => format!("223,{seq},{time},{sym},,,,,{}", vol(p(1))),
            // Add Order Refresh in both documented layouts (with and without an empty field 4).
            106 if p(1) % 2 == 1 => format!("106,{seq},{time},,{sym},{symseq},{body}"),
            _ => format!("{t},{seq},{time},{sym},{symseq},{body}"),
        };
        line.into_bytes()
    }

    proptest! {
        #![proptest_config(ProptestConfig { cases: 48, ..ProptestConfig::default() })]

        /// For any generated day, the FIFO rank of every execution recomputed from the mapped records alone equals
        /// the generator's ground truth (the oracle `replay --rank-oracle` reproduces on the committed fixtures), the
        /// mapping has no anomaly, and the parity label counts exactly the parity market's executions.
        #[test]
        fn mapped_records_reproduce_the_generators_fifo_ranks(
            seed in any::<u64>(),
            market in prop::sample::select(Market::ALL.to_vec()),
            floor_brokers in 0u8..5,
            symbols in 1usize..4,
            events in 50usize..600,
        ) {
            let names = ["QTA", "QTB A", "QTC"];
            let day = generate(&SynthSpec {
                market,
                symbols: names[..symbols].iter().map(|s| (*s).to_owned()).collect(),
                events_per_symbol: events,
                seed,
                floor_brokers,
            });
            let mut m = mapper(Some(market));
            let book = map_all(&mut m, day.csv.split(|&b| b == b'\n').filter(|l| !l.is_empty()));
            let truth: Vec<u32> = day.executions.iter().map(|e| e.fifo_rank).collect();
            prop_assert_eq!(&book.ranks, &truth);
            let st = m.finish().clone();
            prop_assert_eq!(&st.anomalies, &Anomalies::default());
            prop_assert_eq!((m.sequence_gaps, m.sequence_regressions, m.gaps().len()), (0, 0, 0));
            prop_assert_eq!(st.orders_live as usize, book.orders.len());
            let parity = if market.allocation() == Allocation::Parity { truth.len() as u64 } else { 0 };
            prop_assert_eq!((st.executions, st.parity_executions), (truth.len() as u64, parity));
            if market.allocation() == Allocation::PriceTime {
                prop_assert!(truth.iter().all(|&r| r == 0));
            }
        }

        /// Whatever the lines hold (mostly well-formed messages with colliding order ids, odd prices, sizes, sides
        /// and flags, truncated layouts, other products, unknown types and random bytes), parsing never panics, the
        /// mapper never panics, the records it emits are a self-consistent book stream (every `C` and `F` names a
        /// live order, never more than its size, one sequence and one `LAST` per message) and its live-order count
        /// matches that stream.
        #[test]
        fn any_lines_map_to_a_self_consistent_record_stream(
            lines in prop::collection::vec(
                (
                    prop::sample::select(vec![
                        3u16, 34, 100, 100, 100, 100, 100, 101, 101, 102, 103, 103, 103, 103, 104, 104, 105, 106,
                        106, 110, 111, 112, 113, 114, 140, 199, 223,
                    ]),
                    prop_oneof![
                        15 => prop::sample::select(vec![
                            "09:30:00.000000001", "09:30:01", "09:30:02.25", "10:00:00", "11:00:00.5", "12:00:00",
                            "13:00:00", "14:00:00", "15:00:00", "23:59:59", "00:00:01",
                        ]),
                        1 => Just("9:30"),
                    ],
                    prop_oneof![15 => prop::sample::select(vec!["QTX", "QTY A"]), 1 => Just("1234")],
                    prop::collection::vec(any::<u8>(), 12),
                    0usize..48,
                ),
                1..400,
            ),
            noise in prop::collection::vec(any::<u8>(), 0..64),
        ) {
            let mut text: Vec<Vec<u8>> = vec![b"3,1,QTX,1,1,N,C,100,,,0,Y,1,100".to_vec()];
            for (n, (t, time, sym, picks, cut)) in lines.iter().enumerate() {
                text.push(fuzz_line(*t, n as u64 + 2, time, sym, picks, *cut));
            }
            text.push(noise.clone());
            for l in &text {
                let _ = parse_line(l);
            }
            let mut m = mapper(None);
            let book = map_all(&mut m, text.iter().map(Vec::as_slice));
            let st = m.finish().clone();
            prop_assert_eq!(st.orders_live as usize, book.orders.len());
        }
    }
}
