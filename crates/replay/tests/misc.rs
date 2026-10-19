//! O-DQ-1 (verdict rules of SPEC 5.1.5 on constructed counters), the why-trace builder on S1, the et-format BBO
//! parity reader on an in-test CSV, real-order lives (SPEC 5.2.9), and `for_each_partition` ordering.

#![allow(clippy::type_complexity)]

mod common;

use std::path::PathBuf;

use common::{Scn, abs_shadow, cfg, instrument};
use qt_ingest::{DatasetKind, Partition};
use qt_replay::dq::{apply_verdict, dq_flags, merge};
use qt_replay::parity::{BboParity, EtBboCsv, TradeParity, open_bbo_reference};
use qt_replay::{EngineConfig, for_each_partition, trace_shadow};
use qt_schema::json::{DataQuality, FeedGap, Verdict, WhySubject};
use qt_schema::rows::{BboRow, RealOrderEnd, TradeKind, TradeRow};
use qt_types::{BookSide, QtError, Side, Venue};

fn dq(f: impl FnOnce(&mut DataQuality)) -> DataQuality {
    let mut d = DataQuality {
        records: 1000,
        ..DataQuality::default()
    };
    f(&mut d);
    apply_verdict(&mut d, 1000);
    d
}

#[test]
fn o_dq_1_verdict_rules() {
    assert_eq!(dq(|_| {}).verdict, Verdict::Clean);
    // Volumes and the INFO counters (hidden prints, off-price executions: E36(b)) never raise the verdict;
    // every other counter does.
    let vol = dq(|d| d.snapshot_records = 5);
    assert_eq!((vol.verdict, vol.reasons.len()), (Verdict::Clean, 0));
    let info = dq(|d| {
        d.hidden_prints = 7;
        d.off_price_executions = 3;
    });
    assert_eq!(
        (info.verdict, info.reasons.len(), info.hidden_prints),
        (Verdict::Clean, 0, 7)
    );
    for set in [
        |d: &mut DataQuality| d.duplicate_adds = 1,
        |d: &mut DataQuality| d.bad_ts_recv_records = 1,
        |d: &mut DataQuality| d.unexpected_modifies = 1,
        |d: &mut DataQuality| d.sequence_gaps = 1,
        |d: &mut DataQuality| d.sequence_regressions = 1,
        |d: &mut DataQuality| d.out_of_order_ts_event = 1,
        |d: &mut DataQuality| d.pre_snapshot_rows_skipped = 1,
        |d: &mut DataQuality| d.crossed_books_after_last = 1,
        |d: &mut DataQuality| d.unknown_order_refs = 1,
        |d: &mut DataQuality| {
            d.feed_gaps.push(FeedGap {
                instrument_id: 1,
                ts_start: 1,
                ts_end: 2,
                reason: "test".into(),
            })
        },
        |d: &mut DataQuality| {
            d.source_specific.insert("files_without_snapshot".into(), 1);
        },
    ] {
        let d = dq(set);
        assert_eq!(d.verdict, Verdict::Warnings, "{:?}", d.reasons);
        assert_eq!(d.reasons.len(), 1);
    }
    // Ratios: 1 / 1000 is not above 1e-3; 2 / 1000 is.
    assert_eq!(dq(|d| d.crossed_books_after_last = 2).verdict, Verdict::Unusable);
    assert_eq!(dq(|d| d.unknown_order_refs = 2).verdict, Verdict::Unusable);
    let bad = dq(|d| d.maybe_bad_book_records = 1);
    assert_eq!(bad.verdict, Verdict::Unusable);
    assert!(bad.reasons[0].contains("maybe_bad_book_records"));
    assert_eq!(
        (
            dq_flags(Verdict::Clean),
            dq_flags(Verdict::Warnings),
            dq_flags(Verdict::Unusable)
        ),
        (0, 1, 2)
    );
    let m = merge(&[
        ("a".into(), dq(|_| {})),
        ("b".into(), bad.clone()),
        ("c".into(), dq(|d| d.duplicate_adds = 1)),
    ]);
    assert_eq!((m.verdict, m.records, m.duplicate_adds), (Verdict::Unusable, 3000, 1));
    assert!(m.reasons.iter().any(|r| r.starts_with("b: ")));
}

#[test]
fn o_dq_1_engine_counts_flags() {
    let s = Scn::new().ev(10, |e| {
        e.add(1, b'B', 100, 5).add(2, b'A', 101, 5);
    });
    let mut recs = s.recs;
    recs[1].flags |= qt_types::flags::MAYBE_BAD_BOOK;
    let (sink, rep) = common::run(
        cfg(Venue::Generic),
        &[instrument(Venue::Generic)],
        &recs,
        vec![abs_shadow("X", BookSide::Bid, 100, 1, 5, 0)],
        vec![],
    );
    assert_eq!(rep.data_quality.maybe_bad_book_records, 1);
    assert_eq!(rep.data_quality.verdict, Verdict::Unusable);
    assert_eq!(sink.shadow_results[0].dq_flags, 2);
    assert_eq!(sink.shadow_results[0].end_state, qt_schema::rows::EndState::CensoredGap);
}

fn s1() -> Scn {
    Scn::new()
        .ev(10, |e| {
            e.add(1, b'B', 100, 60).add(2, b'B', 100, 40).add(9, b'A', 101, 500);
        })
        .ev(20, |e| {
            e.add(3, b'B', 100, 30);
        })
        .ev(30, |e| {
            e.trade(b'A', 100, 60).exec(1, 60);
        })
        .ev(40, |e| {
            e.trade(b'A', 100, 50).exec(2, 40).exec(3, 10);
        })
}

#[test]
fn why_trace_builder_on_s1() {
    let recs = s1().recs;
    let subject = WhySubject {
        shadow_id: Some("X".into()),
        order_key: None,
    };
    let run = |max: usize| {
        trace_shadow(
            recs.iter().map(|e| Ok(*e)),
            cfg(Venue::Cme),
            &[instrument(Venue::Cme)],
            abs_shadow("X", BookSide::Bid, 100, 10, 15, 0),
            vec![],
            max,
            subject.clone(),
        )
        .unwrap()
    };
    let t = run(5000);
    assert_eq!(
        (t.instrument_id, t.side.as_str(), t.price, t.qty, t.ts_live),
        (1, "B", Some(100), 10, Some(15))
    );
    assert_eq!(
        (
            t.outcome.filled_qty,
            t.outcome.ts_first_fill,
            t.outcome.end_state.as_str()
        ),
        (10, Some(40), "filled")
    );
    let rows: Vec<(i64, &str, u64, &str, bool, u64, u32, u64, u64)> = t
        .events
        .iter()
        .map(|e| {
            (
                e.ts_event,
                e.action.as_str(),
                e.order_id,
                e.effect.as_str(),
                e.is_ahead,
                e.queue_ahead_after,
                e.rank_after,
                e.level_qty_after,
                e.fill_qty,
            )
        })
        .collect();
    assert_eq!(
        rows,
        vec![
            (20, "A", 3, "add", false, 100, 2, 130, 0),
            (30, "T", 0, "none", false, 40, 1, 70, 0),
            (30, "F", 1, "none", true, 40, 1, 70, 0),
            (30, "C", 1, "remove", true, 40, 1, 70, 0),
            (40, "T", 0, "none", false, 0, 0, 20, 0),
            (40, "F", 2, "none", true, 0, 0, 20, 0),
            (40, "C", 2, "remove", true, 0, 0, 20, 0),
            (40, "F", 3, "none", false, 0, 0, 20, 0),
            (40, "C", 3, "reduce", false, 0, 0, 20, 10),
        ]
    );
    assert_eq!(
        t.events.last().and_then(|e| e.note.clone()).as_deref(),
        Some("fill 10 queue_depleted")
    );
    assert!(!t.truncated);
    let short = run(4);
    assert_eq!((short.events.len(), short.truncated), (4, true));
    let doc = serde_json::to_value(&t).unwrap();
    assert_eq!(doc["schema"], "qt.why_trace");
}

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-replay-misc-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn bbo(ts: i64, inst: u32, seq: u64, bid: Option<(i64, u64)>, ask: Option<(i64, u64)>) -> BboRow {
    BboRow {
        ts_event: ts,
        instrument_id: inst,
        sequence: seq,
        bid_px: bid.map(|b| b.0),
        bid_sz: bid.map_or(0, |b| b.1),
        bid_ct: 1,
        ask_px: ask.map(|a| a.0),
        ask_sz: ask.map_or(0, |a| a.1),
        ask_ct: 1,
    }
}

#[test]
fn et_bbo_csv_parity_reader() {
    let dir = scratch("bbo");
    let p = dir.join("bbo.csv");
    std::fs::write(
        &p,
        "ts_event,instrument_id,bid_px,bid_sz,ask_px,ask_sz,sequence\n\
         10,13,100,5,9223372036854775807,0,1\n\
         11,99,7,1,8,1,2\n\
         12,13,100,5,101,4,3\n\
         \n\
         20,13,9223372036854775807,0,101,4,4\n",
    )
    .unwrap();
    let rows: Vec<BboRow> = EtBboCsv::open(&p, Some(13), (0, 100))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(rows.len(), 3);
    assert_eq!((rows[0].bid_px, rows[0].ask_px, rows[0].ask_sz), (Some(100), None, 0));
    assert_eq!((rows[2].bid_px, rows[2].sequence), (None, 4));
    let ranged: Vec<BboRow> = EtBboCsv::open(&p, Some(13), (11, 15))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(ranged.len(), 1);
    // Derived rows carry counts; the et layout has none, so they are not compared.
    let derived = [
        bbo(10, 13, 1, Some((100, 5)), None),
        bbo(12, 13, 3, Some((100, 5)), Some((101, 4))),
        bbo(20, 13, 4, None, Some((101, 4))),
    ];
    let (it, counts) = open_bbo_reference(&p, 13, (0, 100)).unwrap();
    assert!(!counts);
    let mut par = BboParity::new(13, it, counts);
    for d in &derived {
        par.derived(d).unwrap();
    }
    let rep = par.finish().unwrap();
    assert_eq!((rep.bbo_compared, rep.bbo_mismatches), (3, 0));
    let (it, counts) = open_bbo_reference(&p, 13, (0, 100)).unwrap();
    let mut par = BboParity::new(13, it, counts);
    par.derived(&derived[0]).unwrap();
    par.derived(&bbo(12, 13, 3, Some((100, 6)), Some((101, 4)))).unwrap();
    let rep = par.finish().unwrap();
    assert_eq!(
        (rep.bbo_compared, rep.bbo_mismatches),
        (2, 2),
        "one value mismatch + one reference row unmatched"
    );
    assert_eq!(rep.first_mismatches.len(), 2);
    // Bad header and bad row.
    std::fs::write(dir.join("bad.csv"), "ts,instrument_id\n").unwrap();
    assert!(matches!(
        EtBboCsv::open(&dir.join("bad.csv"), None, (0, 1)),
        Err(QtError::InputFormat { .. })
    ));
    std::fs::write(
        dir.join("bad2.csv"),
        "ts_event,instrument_id,bid_px,bid_sz,ask_px,ask_sz,sequence\nx,1,1,1,1,1,1\n",
    )
    .unwrap();
    let mut it = EtBboCsv::open(&dir.join("bad2.csv"), None, (0, 1)).unwrap();
    assert_eq!(it.next().unwrap().unwrap_err().code(), "QT-INPUT-002");
    // Trade parity: in-order key comparison, missing and extra rows.
    let t = |ts: i64, px: i64| TradeRow {
        ts_event: ts,
        instrument_id: 13,
        sequence: ts as u64,
        price: px,
        size: 1,
        aggressor_side: Side::Ask,
        kind: TradeKind::Displayed,
        maker_order_id: None,
    };
    let mut tp = TradeParity::new(13, vec![t(1, 100), t(2, 100), t(3, 100)]);
    tp.derived(&t(1, 100));
    tp.derived(&t(2, 101));
    let r = tp.finish();
    assert_eq!(
        (
            r.trades_compared,
            r.trades_mismatches,
            r.trades_missing_derived,
            r.trades_extra_derived
        ),
        (2, 1, 1, 0)
    );
    let m = qt_replay::parity::merge(&[r]);
    assert!(!m.pass);
    std::fs::remove_dir_all(dir).ok();
}

#[test]
fn real_order_lives() {
    // CME: o1 partially filled (paired, no reduce) then cancelled; o2 reduced (unpaired), then repriced (replaced),
    // the new life is cleared by R; o3 is fully filled; o4 open at end.
    let s = Scn::new()
        .ev(10, |e| {
            e.add(1, b'B', 100, 10).add(2, b'B', 100, 20).add(3, b'A', 101, 5);
        })
        .ev(20, |e| {
            e.trade(b'A', 100, 4).exec(1, 4);
        })
        .ev(30, |e| {
            e.cancel(2, 5);
        })
        .ev(40, |e| {
            e.modify(2, b'B', 99, 15);
        })
        .ev(50, |e| {
            e.trade(b'B', 101, 5).exec(3, 5);
        })
        .ev(60, |e| {
            e.cancel(1, 6);
        })
        .ev(70, |e| {
            e.add(4, b'A', 102, 1);
        })
        .ev(80, |e| {
            e.clear();
        })
        .ev(90, |e| {
            e.add(5, b'B', 98, 2);
        });
    let mut c = EngineConfig::new(qt_types::rules(Venue::Cme));
    c.real_orders = true;
    let (sink, _) = common::run(c, &[instrument(Venue::Cme)], &s.recs, vec![], vec![]);
    let lives: Vec<(u64, i64, u64, u64, u32, u32, u64, u32, Option<i64>, RealOrderEnd)> = sink
        .real_orders
        .iter()
        .map(|r| {
            (
                r.order_id,
                r.price,
                r.size_at_add,
                r.queue_ahead_at_add,
                r.rank_at_add,
                r.n_fills,
                r.filled_qty,
                r.n_reduces,
                r.ts_end,
                r.end_reason,
            )
        })
        .collect();
    assert_eq!(
        lives,
        vec![
            (2, 100, 20, 10, 1, 0, 0, 1, Some(40), RealOrderEnd::Replaced),
            (3, 101, 5, 0, 0, 1, 5, 0, Some(50), RealOrderEnd::Filled),
            (1, 100, 10, 0, 0, 1, 4, 0, Some(60), RealOrderEnd::Cancelled),
            (2, 99, 15, 0, 0, 0, 0, 0, Some(80), RealOrderEnd::Cleared),
            (4, 102, 1, 0, 0, 0, 0, 0, Some(80), RealOrderEnd::Cleared),
            (5, 98, 2, 0, 0, 0, 0, 0, None, RealOrderEnd::OpenAtEnd),
        ]
    );
    assert!(sink.real_orders.iter().all(|r| r.session_date == 0 && !r.in_snapshot));
}

fn part(inst: u32, date: i32) -> Partition {
    Partition {
        instrument_id: inst,
        session_date: date,
        files: Vec::new(),
        rows: 0,
        ts_min: 0,
        ts_max: 0,
        kind: DatasetKind::Mbo,
    }
}

#[test]
fn for_each_partition_keeps_partition_order() {
    let parts: Vec<Partition> = (0..23).map(|i| part(i, 100 + i as i32)).collect();
    let f = |p: &Partition| -> qt_types::QtResult<u32> {
        std::thread::sleep(std::time::Duration::from_micros(u64::from(23 - p.instrument_id) * 50));
        Ok(p.instrument_id * 2)
    };
    let one = for_each_partition(&parts, 1, f).unwrap();
    let four = for_each_partition(&parts, 4, f).unwrap();
    assert_eq!(one, four);
    assert_eq!(one, (0..23).map(|i| i * 2).collect::<Vec<_>>());
    let err = for_each_partition(&parts, 4, |p| {
        if p.instrument_id >= 5 {
            Err(QtError::Config(format!("p{}", p.instrument_id)))
        } else {
            Ok(())
        }
    })
    .unwrap_err();
    assert_eq!(err.to_string(), "QT-CONFIG-001: p5");
    assert!(for_each_partition(&[], 3, |_| Ok(())).unwrap().is_empty());
}
