//! Findings of the independent Rust review in the replay engine, each
//! with the reference (`qt_testkit`) where it models the case: M3 (`why` uses replay's partition engine), M2
//! (ingest feed gaps censor shadows), M9 (abs shadows use the instrument tick), m11 (amends in time order), m12
//! (cancel and amends due together apply in time order), m13 (per-event truth of `crossed_at_live` fills), m14
//! (one `crossed_unfilled` per cross), m15 (a flagged re-add at another price leaves the queue), m17 (exclusions
//! counted once), m18 (an excluded order's bad-book record still censors), m19 (A4b for the excluded twin's
//! executions), m22 (huge sample sizes are rejected).

mod common;

use std::sync::{Arc, Mutex};

use common::{INST, Scn, abs_shadow, instrument};
use qt_replay::sinks::VecSink;
use qt_replay::{Engine, EngineConfig, EventEnd, EventObserver, ShadowEventTruth, trace_shadow};
use qt_schema::json::WhySubject;
use qt_schema::rows::{EndState, PriceSpec, ShadowAmend, ShadowSource, ShadowSpec};
use qt_testkit::scenario::Scenario;
use qt_testkit::{RefConfig, try_reference_shadows};
use qt_types::{BookSide, Instrument, MboEvent, Side, Venue, flags, rules};

fn engine(venue: Venue, insts: &[Instrument], recs: &[MboEvent], shadows: Vec<ShadowSpec>) -> (VecSink, Engine) {
    let mut eng = Engine::new(EngineConfig::new(rules(venue)), insts, shadows, vec![]).unwrap();
    eng.set_sequence_gap_detection(false);
    let mut sink = VecSink::default();
    for r in recs {
        eng.apply(r, &mut sink).unwrap();
    }
    (sink, eng)
}

fn fills(sink: &VecSink, id: &str) -> Vec<(i64, u64)> {
    sink.shadow_fills
        .iter()
        .filter(|f| f.shadow_id == id)
        .map(|f| (f.ts_event, f.fill_qty))
        .collect()
}

/// M3: on one instrument's slice of a feed (sequence numbers jump), `why` agrees with replay.
#[test]
fn m3_why_matches_replay_on_a_feed_partition() {
    let mut recs = Scn::new()
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
        .recs;
    for (i, r) in recs.iter_mut().enumerate() {
        r.sequence = 10 * (i as u64 + 1);
    }
    let x = abs_shadow("X", BookSide::Bid, 100, 10, 15, 0);
    let (mut sink, eng) = engine(Venue::Cme, &[instrument(Venue::Cme)], &recs, vec![x.clone()]);
    eng.finish(&mut sink).unwrap();
    let t = trace_shadow(
        recs.iter().map(|e| Ok(*e)),
        EngineConfig::new(rules(Venue::Cme)),
        &[instrument(Venue::Cme)],
        x,
        vec![],
        5000,
        WhySubject {
            shadow_id: Some("X".into()),
            order_key: None,
        },
    )
    .unwrap();
    assert_eq!(sink.shadow_results[0].end_state, EndState::Filled);
    assert_eq!((t.outcome.end_state.as_str(), t.outcome.filled_qty), ("filled", 10));
}

/// M9: an abs shadow and a touch shadow resolving to the same price see the same off-tick hidden print (tick 2:
/// a print at 101 is off the grid and never fills), in the engine and in the reference.
#[test]
fn m9_abs_and_touch_shadows_share_the_tick_grid() {
    let mut inst = instrument(Venue::Nasdaq);
    inst.tick_size = 2;
    let s = Scn::new()
        .ev(10, |e| {
            e.add(1, b'B', 96, 100).add(9, b'A', 104, 100);
        })
        .ev(20, |e| {
            e.trade(b'N', 101, 50);
        })
        .ev(30, |e| {
            e.trade(b'N', 100, 7);
        });
    let touch = ShadowSpec {
        shadow_id: "XT".into(),
        instrument_id: INST,
        side: BookSide::Bid,
        price: PriceSpec::Touch { offset_ticks: 3 },
        qty: 10,
        ts_submit: 15,
        latency_ns: 0,
        ts_cancel: None,
        group: None,
        source: ShadowSource::User,
        order_key: None,
    };
    let shadows = vec![abs_shadow("XA", BookSide::Bid, 102, 10, 15, 0), touch];
    let (mut sink, eng) = engine(Venue::Nasdaq, std::slice::from_ref(&inst), &s.recs, shadows.clone());
    eng.finish(&mut sink).unwrap();
    // The on-grid print at 100 trades through both (A3d: no displayed interest at or through 100).
    assert_eq!(fills(&sink, "XA"), vec![(30, 7)]);
    assert_eq!(fills(&sink, "XT"), vec![(30, 7)]);
    let mut rc = RefConfig::new(rules(Venue::Nasdaq));
    rc.instruments = vec![inst];
    let truth = try_reference_shadows(&s.recs, &rc, &shadows, &[]).unwrap();
    for (r, f, _) in &truth {
        let want: Vec<_> = f.iter().map(|x| (x.ts_event, x.fill_qty)).collect();
        assert_eq!(fills(&sink, &r.shadow_id), want, "{}", r.shadow_id);
    }
}

/// m11: amends out of time order are `QT-INPUT-002` (engine and reference).
#[test]
fn m11_amends_out_of_time_order_are_rejected() {
    let am = vec![
        ShadowAmend {
            shadow_id: "X".into(),
            ts_send: 200,
            new_qty: 5,
        },
        ShadowAmend {
            shadow_id: "X".into(),
            ts_send: 100,
            new_qty: 3,
        },
    ];
    let x = abs_shadow("X", BookSide::Bid, 100, 10, 15, 0);
    let err = Engine::new(
        EngineConfig::new(rules(Venue::Cme)),
        &[instrument(Venue::Cme)],
        vec![x.clone()],
        am.clone(),
    )
    .err()
    .expect("rejected");
    assert_eq!(err.code(), "QT-INPUT-002");
    let rc = RefConfig::new(rules(Venue::Cme));
    let err = try_reference_shadows(&[], &rc, &[x], &am).expect_err("rejected");
    assert_eq!(err.code(), "QT-INPUT-002");
}

/// m12: a cancel effective at 45 and an amend to below the filled quantity at 47, both before the event at 50: the
/// cancel comes first (`cancelled` at 45, not `amended_to_filled` at 47).
#[test]
fn m12_cancel_and_amend_due_together_apply_in_time_order() {
    let s = Scenario::new(Venue::Cme)
        .event(10, |e| e.add(1, BookSide::Bid, 100, 5).add(9, BookSide::Ask, 101, 500))
        .event(20, |e| e.add(3, BookSide::Bid, 100, 20))
        .event(30, |e| e.trade(Side::Ask, 100, 7).fill(1, 5).fill(3, 2))
        .event(50, |e| e.add(4, BookSide::Bid, 99, 1));
    let recs = s.build();
    let mut x = abs_shadow("X", BookSide::Bid, 100, 10, 15, 0);
    x.ts_cancel = Some(45);
    let am = vec![ShadowAmend {
        shadow_id: "X".into(),
        ts_send: 47,
        new_qty: 2,
    }];
    let mut eng = Engine::new(EngineConfig::new(rules(Venue::Cme)), &[], vec![x.clone()], am.clone()).unwrap();
    let mut sink = VecSink::default();
    for r in &recs {
        eng.apply(r, &mut sink).unwrap();
    }
    eng.finish(&mut sink).unwrap();
    let r = &sink.shadow_results[0];
    assert_eq!((r.filled_qty, r.end_state), (2, EndState::Cancelled));
    let end = sink.trajectory.last().unwrap();
    assert_eq!(end.ts_event, 45);
    let mut rc = RefConfig::new(rules(Venue::Cme));
    rc.dq_flags = true;
    let truth = try_reference_shadows(&recs, &rc, &[x], &am).unwrap();
    assert_eq!(truth[0].0, *r);
    assert_eq!(truth[0].2, sink.trajectory);
}

#[derive(Default)]
struct Truths(Arc<Mutex<Vec<(i64, ShadowEventTruth)>>>);

impl EventObserver for Truths {
    fn on_event_end(&mut self, end: &EventEnd<'_>) {
        if let Ok(mut v) = self.0.lock() {
            v.extend(end.shadow_truth.iter().map(|t| (end.ts_event, t.clone())));
        }
    }
}

/// m13: the per-event truth reports a `crossed_at_live` fill in its activation event, once.
#[test]
fn m13_truth_reports_crossed_at_live_fills_once() {
    let s = Scn::new()
        .ev(10, |e| {
            e.add(1, b'A', 101, 5).add(2, b'B', 99, 5);
        })
        .ev(20, |e| {
            e.add(3, b'B', 98, 1);
        })
        .ev(30, |e| {
            e.add(4, b'B', 97, 1);
        });
    let partial = abs_shadow("P", BookSide::Bid, 101, 20, 15, 0);
    let mut full = abs_shadow("F", BookSide::Bid, 101, 5, 15, 0);
    full.ts_cancel = None;
    let mut cancelled = abs_shadow("C", BookSide::Bid, 101, 3, 15, 0);
    cancelled.ts_cancel = Some(25);
    let truths = Arc::new(Mutex::new(Vec::new()));
    let mut eng = Engine::new(
        EngineConfig::new(rules(Venue::Cme)),
        &[instrument(Venue::Cme)],
        vec![partial, full, cancelled],
        vec![],
    )
    .unwrap();
    eng.add_observer(Box::new(Truths(truths.clone())));
    let mut sink = VecSink::default();
    for r in &s.recs {
        eng.apply(r, &mut sink).unwrap();
    }
    eng.finish(&mut sink).unwrap();
    let t = truths.lock().unwrap();
    let per = |idx: u32| -> Vec<(i64, u64)> {
        t.iter()
            .filter(|(_, x)| x.shadow_idx == idx && x.filled_now > 0)
            .map(|(ts, x)| (*ts, x.filled_now))
            .collect()
    };
    assert_eq!(
        per(0),
        vec![(20, 5)],
        "partial: the 5 lots taken at activation, in the activation event"
    );
    assert_eq!(per(1), vec![(20, 5)], "fully filled at activation");
    assert_eq!(
        per(2),
        vec![(20, 3)],
        "cancelled later: its activation fill is not reported again"
    );
}

/// m14: a locked book that persists over several events counts one `crossed_unfilled`; a new cross counts again.
#[test]
fn m14_crossed_unfilled_once_per_cross() {
    let s = Scn::new()
        .ev(10, |e| {
            e.add(1, b'B', 100, 60).add(9, b'A', 102, 500);
        })
        .ev(20, |e| {
            e.add(6, b'A', 100, 5);
        })
        .ev(30, |e| {
            e.add(7, b'B', 98, 1);
        })
        .ev(40, |e| {
            e.add(8, b'B', 97, 1);
        })
        .ev(50, |e| {
            e.cancel(6, 5);
        })
        .ev(60, |e| {
            e.add(10, b'A', 100, 5);
        });
    let (mut sink, eng) = engine(
        Venue::Cme,
        &[instrument(Venue::Cme)],
        &s.recs,
        vec![abs_shadow("X", BookSide::Bid, 100, 10, 15, 0)],
    );
    let rep = eng.finish(&mut sink).unwrap();
    assert!(sink.shadow_fills.is_empty(), "o1 (60 ahead) takes every crossing offer");
    assert_eq!(rep.diagnostics.crossed_unfilled, 2);
}

/// m15: a flagged snapshot re-add (CME) of an order ahead at another price takes it out of the queue ahead; at the
/// same price it keeps its place. Engine and reference agree.
#[test]
fn m15_flagged_readd_at_another_price_leaves_the_queue() {
    for (px, want_qa) in [(99, 40), (100, 100)] {
        let s = Scenario::new(Venue::Cme)
            .event(10, |e| {
                e.add(1, BookSide::Bid, 100, 60)
                    .add(2, BookSide::Bid, 100, 40)
                    .add(9, BookSide::Ask, 102, 500)
            })
            .event(20, |e| e.add(3, BookSide::Bid, 100, 30))
            .event(30, |e| e.add(1, BookSide::Bid, px, 60).flag(flags::SNAPSHOT));
        let recs = s.build();
        let x = abs_shadow("X", BookSide::Bid, 100, 10, 15, 0);
        let (mut sink, eng) = engine(Venue::Cme, &[], &recs, vec![x.clone()]);
        eng.finish(&mut sink).unwrap();
        let end = sink.trajectory.last().unwrap();
        assert_eq!(end.queue_ahead_after, want_qa, "re-add at {px}");
        let truth = try_reference_shadows(&recs, &RefConfig::new(rules(Venue::Cme)), &[x], &[]).unwrap();
        assert_eq!(truth[0].2, sink.trajectory, "re-add at {px}");
    }
}

/// m18 / m19: an excluded order's `MAYBE_BAD_BOOK` record still censors live shadows; an excluded twin executed
/// off its displayed price is not volume for the shadow (A4b).
#[test]
fn m18_m19_excluded_order_records() {
    let base = || {
        Scenario::new(Venue::Cme)
            .event(10, |e| {
                e.add(1, BookSide::Bid, 100, 60)
                    .add(5, BookSide::Bid, 100, 10)
                    .add(9, BookSide::Ask, 102, 500)
            })
            .event(20, |e| e.add(3, BookSide::Bid, 100, 30))
    };
    let x = abs_shadow("X", BookSide::Bid, 100, 10, 15, 0);
    let run = |recs: &[MboEvent]| {
        let mut eng = Engine::new(EngineConfig::new(rules(Venue::Cme)), &[], vec![x.clone()], vec![]).unwrap();
        eng.set_sequence_gap_detection(false);
        eng.set_excluded_orders([(1, 5)]);
        let mut sink = VecSink::default();
        for r in recs {
            eng.apply(r, &mut sink).unwrap();
        }
        let rep = eng.finish(&mut sink).unwrap();
        (sink, rep)
    };
    let bad = base().event(30, |e| e.cancel(5, 1).flag(flags::MAYBE_BAD_BOOK)).build();
    let (sink, _) = run(&bad);
    assert_eq!(sink.shadow_results[0].end_state, EndState::CensoredGap);
    // o1 leaves; the excluded twin o5 is executed at 101 (price-improved): no fill; at 100: fill.
    let off = base()
        .event(30, |e| e.cancel(1, 60))
        .event(40, |e| {
            e.trade(Side::Ask, 101, 4)
                .fill_only(5, 4)
                .record(qt_types::Action::Cancel, Side::Bid, 100, 4, 5)
        })
        .build();
    let mut off = off;
    for r in off.iter_mut().filter(|r| r.action == b'F' && r.order_id == 5) {
        r.price = 101;
    }
    let (sink, rep) = run(&off);
    assert!(sink.shadow_fills.is_empty(), "{:?}", sink.shadow_fills);
    assert_eq!(rep.diagnostics.excluded_matched, 1);
    let at = base()
        .event(30, |e| e.cancel(1, 60))
        .event(40, |e| e.trade(Side::Ask, 100, 4).fill(5, 4))
        .build();
    let (sink, _) = run(&at);
    assert_eq!(fills(&sink, "X"), vec![(40, 4)]);
    // The reference agrees on all three.
    let mut rc = RefConfig::new(rules(Venue::Cme));
    rc.exclude_order_ids = vec![5];
    rc.dq_flags = true;
    for recs in [&bad, &off, &at] {
        let truth = try_reference_shadows(recs, &rc, std::slice::from_ref(&x), &[]).unwrap();
        let (sink, _) = run(recs);
        assert_eq!(truth[0].0, sink.shadow_results[0]);
        assert_eq!(truth[0].1, sink.shadow_fills);
    }
}

/// m22: a huge sample size is `QT-CONFIG-001`, before any allocation.
#[test]
fn m22_huge_sample_is_rejected() {
    let spec: qt_schema::specs::ShadowSampleSpec = serde_json::from_value(serde_json::json!({
        "dataset": {"path": "/nonexistent"}, "out": "/nonexistent/x.parquet", "n": u64::MAX
    }))
    .unwrap();
    let err = qt_replay::sample_shadows(&spec).expect_err("rejected");
    assert_eq!(err.code(), "QT-CONFIG-001");
}

/// M2 (replay side): ingest feed gaps (`data_quality.json`) censor the live shadows of their instrument in
/// `run_dataset`, and a gap is reported once.
#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit crate")]
fn m2_ingest_feed_gaps_censor_shadows() {
    let mut cfg = qt_testkit::SynthConfig::for_venue(Venue::Cme, 3);
    cfg.instruments = 1;
    cfg.events_per_instrument = 600;
    let out = qt_testkit::generate(&cfg);
    let root = std::env::temp_dir().join(format!("qt-replay-m2-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    qt_testkit::write_dataset(&out, &root).unwrap();
    let mid = out.events[out.events.len() / 2].ts_event;
    let dq_path = root.join("data_quality.json");
    let mut dq: qt_schema::json::DataQuality =
        serde_json::from_str(&std::fs::read_to_string(&dq_path).unwrap()).unwrap();
    dq.feed_gaps.push(qt_schema::json::FeedGap {
        instrument_id: 1,
        ts_start: mid,
        ts_end: mid + 1,
        reason: "sequence gap (test)".into(),
    });
    std::fs::write(&dq_path, serde_json::to_string(&dq).unwrap()).unwrap();
    let first = out
        .events
        .iter()
        .find(|e| e.flags & flags::SNAPSHOT == 0)
        .unwrap()
        .ts_event;
    let deep = cfg.base_px - 200 * cfg.tick;
    let shadows = vec![abs_shadow("G", BookSide::Bid, deep, 1, first + 1, 0)];
    let sh_path = root.join("g.parquet");
    let mut w = qt_schema::io::TableWriter::create(&sh_path, "qt.shadow_spec").unwrap();
    let mut s = shadows;
    s[0].instrument_id = 1;
    w.write(&qt_schema::codec::shadow_specs_to_batch(&s)).unwrap();
    w.finish().unwrap();
    let spec: qt_schema::specs::ReplaySpec = serde_json::from_value(serde_json::json!({
        "dataset": {"path": root.display().to_string()}, "out": root.join("out").display().to_string(),
        "shadows": {"path": sh_path.display().to_string()}, "bbo": "none"
    }))
    .unwrap();
    let sum = qt_replay::run_dataset(&spec).unwrap();
    assert_eq!(sum.data_quality.feed_gaps.len(), 1);
    let res: Vec<qt_schema::rows::ShadowResultRow> = {
        use qt_schema::codec::TableRow;
        let mut v = Vec::new();
        for b in qt_schema::io::read_table_as(&root.join("out/shadow_results.parquet"), "qt.shadow_results").unwrap() {
            v.extend(qt_schema::rows::ShadowResultRow::from_batch(&b.unwrap()).unwrap());
        }
        v
    };
    assert_eq!(res[0].end_state, EndState::CensoredGap);
    std::fs::remove_dir_all(&root).ok();
}

/// OC-28: an ITCH cross print (side-N `T` that `trades_source` marks `cross`) is neither a hidden print nor shadow
/// fill volume; it counts `cross_prints` (INFO, `source_specific`).
#[test]
fn oc28_cross_prints_are_not_hidden_prints() {
    let s = Scn::new()
        .ev(10, |e| {
            e.add(1, b'B', 99, 60).add(9, b'A', 102, 500);
        })
        .ev(20, |e| {
            e.add(2, b'B', 98, 5);
        })
        .ev(30, |e| {
            e.trade(b'N', 100, 50);
        });
    let cross_seq = s.recs.last().unwrap().sequence;
    for crossed in [false, true] {
        let mut eng = Engine::new(
            EngineConfig::new(rules(Venue::Nasdaq)),
            &[instrument(Venue::Nasdaq)],
            vec![abs_shadow("X", BookSide::Bid, 100, 10, 15, 0)],
            vec![],
        )
        .unwrap();
        if crossed {
            eng.set_cross_prints([(INST, cross_seq)]);
        }
        let mut sink = VecSink::default();
        for r in &s.recs {
            eng.apply(r, &mut sink).unwrap();
        }
        let rep = eng.finish(&mut sink).unwrap();
        let dq = &rep.data_quality;
        if crossed {
            assert!(sink.shadow_fills.is_empty());
            assert_eq!(
                (dq.hidden_prints, dq.source_specific.get("cross_prints").copied()),
                (0, Some(1))
            );
            assert_eq!(dq.verdict, qt_schema::json::Verdict::Clean, "{:?}", dq.reasons);
            assert_eq!(sink.trades[0].kind, qt_schema::rows::TradeKind::Cross);
        } else {
            assert_eq!(
                fills(&sink, "X"),
                vec![(30, 10)],
                "a usable hidden print inside the spread (A3d)"
            );
            assert_eq!(dq.hidden_prints, 1);
        }
    }
}

/// Snapshot records (with their older snapshot timestamps) are not ordering anomalies, in the engine and in
/// the reference tracker.
#[test]
fn snapshot_records_are_not_ordering_anomalies() {
    let recs = Scenario::new(Venue::Cme)
        .event(100, |e| e.add(1, BookSide::Bid, 100, 5).add(9, BookSide::Ask, 101, 5))
        .event(50, |e| e.add(2, BookSide::Bid, 99, 5).flag(flags::SNAPSHOT))
        .event(110, |e| e.add(3, BookSide::Bid, 98, 5))
        .event(105, |e| e.add(4, BookSide::Bid, 97, 5))
        .build();
    let (mut sink, eng) = engine(Venue::Cme, &[], &recs, vec![]);
    let rep = eng.finish(&mut sink).unwrap();
    assert_eq!(rep.data_quality.out_of_order_ts_event, 1, "only 105 after 110");
    let mut t = qt_testkit::refbook::DqTracker::new(rules(Venue::Cme));
    for e in &recs {
        t.push(e);
    }
    assert_eq!(t.finish().0.out_of_order_ts_event, 1);
}
