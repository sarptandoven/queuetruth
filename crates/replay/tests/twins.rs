//! Audit ruling A1, self-exclusion twins (RD-11): an excluded real order that a shadow enters at is replaced by
//! that shadow only. Each test reproduces, on a minimal tape, a cause of discordance found on the real Nasdaq days
//! of RD-11 (2019-12-30, 2020-01-30) when the twins were removed from the tape for everyone, and checks the engine
//! against the independent reference (`qt_testkit::try_reference_shadows` with `twin_linking`).

mod common;

use common::abs_shadow;
use qt_replay::sinks::VecSink;
use qt_replay::{Engine, EngineConfig, EngineReport};
use qt_schema::rows::{EndState, ShadowSpec};
use qt_testkit::scenario::Scenario;
use qt_testkit::{RefConfig, try_reference_shadows};
use qt_types::{BookSide, MboEvent, Side, Venue, rules};

fn run(venue: Venue, recs: &[MboEvent], shadows: &[ShadowSpec], excl: &[u64], link: bool) -> (VecSink, EngineReport) {
    let mut eng = Engine::new(EngineConfig::new(rules(venue)), &[], shadows.to_vec(), vec![]).unwrap();
    eng.set_sequence_gap_detection(false);
    eng.set_excluded_orders(excl.iter().map(|&o| (1, o)));
    eng.set_twin_linking(link);
    let mut sink = VecSink::default();
    for r in recs {
        eng.apply(r, &mut sink).unwrap();
    }
    let rep = eng.finish(&mut sink).unwrap();
    if link {
        // The reference models the same semantics.
        let mut rc = RefConfig::new(rules(venue));
        rc.exclude_order_ids = excl.to_vec();
        rc.twin_linking = true;
        rc.dq_flags = true;
        let truth = try_reference_shadows(recs, &rc, shadows, &[]).unwrap();
        for (k, t) in truth.iter().enumerate() {
            assert_eq!(t.0, sink.shadow_results[k], "{}", shadows[k].shadow_id);
            let mut got: Vec<_> = sink
                .shadow_fills
                .iter()
                .filter(|f| f.shadow_id == shadows[k].shadow_id)
                .cloned()
                .collect();
            got.sort_by_key(|f| f.fill_seq);
            assert_eq!(t.1, got, "{}", shadows[k].shadow_id);
        }
    }
    (sink, rep)
}

fn fills(sink: &VecSink, id: &str) -> Vec<(i64, u64, i64)> {
    sink.shadow_fills
        .iter()
        .filter(|f| f.shadow_id == id)
        .map(|f| (f.ts_event, f.fill_qty, f.fill_px))
        .collect()
}

fn result<'a>(sink: &'a VecSink, id: &str) -> &'a qt_schema::rows::ShadowResultRow {
    sink.shadow_results.iter().find(|r| r.shadow_id == id).unwrap()
}

/// Cause (c) off-display execution (RD-11 13:246882941, 5291:272004587, 7451:116217320, 7451:24862472,
/// 7992:46355100; day two 5294:37257563, 7457:23970604, 7457:353387488, 8000:144110116): a display-slid ask shows at
/// 101 but executes at its limit 100 while another order is displayed ahead at 101. The twin's execution is its
/// shadow's fill, at the execution price; with the twin removed for everyone it was never credited (A4b).
#[test]
fn twin_executed_off_its_displayed_price_fills_its_shadow() {
    let recs = Scenario::new(Venue::Nasdaq)
        .event(10, |e| e.add(1, BookSide::Ask, 101, 50).add(9, BookSide::Bid, 98, 500))
        .event(20, |e| e.add(5, BookSide::Ask, 101, 30))
        .event(30, |e| e.execute_at(5, 30, 100, true))
        .build();
    let x = abs_shadow("X", BookSide::Ask, 101, 30, 20, 0);
    let (sink, rep) = run(Venue::Nasdaq, &recs, std::slice::from_ref(&x), &[5], true);
    assert_eq!(fills(&sink, "X"), vec![(30, 30, 100)]);
    assert_eq!(result(&sink, "X").queue_ahead_at_live, Some(50));
    let d = &rep.diagnostics;
    assert_eq!(
        (
            d.twins_linked,
            d.twin_fills_off_display,
            d.twin_fills_with_ahead,
            d.excluded_records
        ),
        (1, 1, 0, 0)
    );
    // Removed for everyone (no linking): the off-display execution is not shadow volume.
    let (old, _) = run(Venue::Nasdaq, &recs, std::slice::from_ref(&x), &[5], false);
    assert!(fills(&old, "X").is_empty());
    // The scorer's 1 ns alignment (ts_live one ns before a twin record that opens its timestamp) links too.
    let early = abs_shadow("X", BookSide::Ask, 101, 30, 19, 0);
    let (sink, rep) = run(Venue::Nasdaq, &recs, std::slice::from_ref(&early), &[5], true);
    assert_eq!(fills(&sink, "X"), vec![(30, 30, 100)]);
    assert_eq!(rep.diagnostics.twins_linked, 1);
}

/// Cause (c) same-nanosecond activation (RD-11 5291:4009027, 4009511, 4009523, the opening-cross release): the
/// twins are added among other orders of the same timestamp, so a strict time activation queues the shadow behind
/// orders that were behind its twin; the sweep then executes the twin while the shadow still sees queue ahead. The
/// shadow now activates at its twin's record, in the twin's place; identical legs link in input order.
#[test]
fn twin_inside_a_same_timestamp_burst_activates_in_its_place() {
    let recs = Scenario::new(Venue::Nasdaq)
        .event(5, |e| e.add(9, BookSide::Ask, 103, 500))
        .event(10, |e| e.add(1, BookSide::Bid, 100, 7))
        .event(10, |e| e.add(5, BookSide::Bid, 100, 1))
        .event(10, |e| e.add(2, BookSide::Bid, 100, 5))
        .event(10, |e| e.add(6, BookSide::Bid, 100, 1))
        .event(10, |e| e.add(3, BookSide::Bid, 100, 9))
        .event(20, |e| e.execute(1, 7))
        .event(20, |e| e.execute(5, 1))
        .event(20, |e| e.execute(2, 5))
        .event(20, |e| e.execute(6, 1))
        .build();
    let a = abs_shadow("A", BookSide::Bid, 100, 1, 10, 0);
    let b = abs_shadow("B", BookSide::Bid, 100, 1, 10, 0);
    let (sink, _) = run(Venue::Nasdaq, &recs, &[a.clone(), b.clone()], &[5, 6], true);
    assert_eq!(fills(&sink, "A"), vec![(20, 1, 100)]);
    assert_eq!(fills(&sink, "B"), vec![(20, 1, 100)]);
    // A replaces 5 (behind 1), B replaces 6 (behind 1, 5, 2; 5 is a real order for B).
    assert_eq!(result(&sink, "A").queue_ahead_at_live, Some(7));
    assert_eq!(result(&sink, "B").queue_ahead_at_live, Some(13));
    // Removed for everyone: both shadows queue behind every order of the timestamp and miss the sweep.
    let (old, _) = run(Venue::Nasdaq, &recs, &[a, b], &[5, 6], false);
    assert!(fills(&old, "A").is_empty() && fills(&old, "B").is_empty());
}

/// Cause (c)/(e) hidden print at the twin's displayed price (RD-11 5291:153121231, 5291:281124047): with the twin
/// removed from the book nothing is displayed at its price, so a side-N print there looked usable (A3d) and filled
/// the shadow although the displayed twin was not executed. The twin stays on the tape: the print is declined.
#[test]
fn hidden_print_at_the_twins_displayed_price_is_declined() {
    let recs = Scenario::new(Venue::Nasdaq)
        .event(10, |e| e.add(9, BookSide::Ask, 102, 100).add(8, BookSide::Bid, 99, 100))
        .event(20, |e| e.add(5, BookSide::Bid, 100, 100))
        .event(30, |e| e.hidden_print(100, 33))
        .event(40, |e| e.cancel(5, 100))
        .event(50, |e| e.add(4, BookSide::Bid, 98, 10))
        .build();
    let mut x = abs_shadow("X", BookSide::Bid, 100, 100, 20, 0);
    x.ts_cancel = Some(40);
    let (sink, rep) = run(Venue::Nasdaq, &recs, std::slice::from_ref(&x), &[5], true);
    assert!(fills(&sink, "X").is_empty());
    assert_eq!(result(&sink, "X").end_state, EndState::Cancelled);
    assert_eq!(rep.diagnostics.hidden_at_px_with_display_ahead, 1);
    let (old, _) = run(Venue::Nasdaq, &recs, std::slice::from_ref(&x), &[5], false);
    assert_eq!(fills(&old, "X"), vec![(30, 33, 100)]);
}

/// Per-shadow exclusion: another live order's twin stays a real order for this shadow (it was ahead of the live
/// order), and its executions are not this shadow's volume.
#[test]
fn another_twin_stays_in_the_queue() {
    let recs = Scenario::new(Venue::Cme)
        .event(5, |e| e.add(9, BookSide::Ask, 103, 500))
        .event(10, |e| e.add(5, BookSide::Bid, 100, 10))
        .event(20, |e| e.add(6, BookSide::Bid, 100, 10))
        .event(30, |e| e.trade(Side::Ask, 100, 10).fill(5, 10))
        .event(40, |e| e.cancel(6, 10))
        .event(50, |e| e.add(4, BookSide::Bid, 98, 10))
        .build();
    let a = abs_shadow("A", BookSide::Bid, 100, 10, 10, 0);
    let mut b = abs_shadow("B", BookSide::Bid, 100, 10, 20, 0);
    b.ts_cancel = Some(40);
    let (sink, _) = run(Venue::Cme, &recs, &[a.clone(), b.clone()], &[5, 6], true);
    assert_eq!(fills(&sink, "A"), vec![(30, 10, 100)]);
    assert!(fills(&sink, "B").is_empty());
    assert_eq!(result(&sink, "B").queue_ahead_at_live, Some(10));
    // Removed for everyone: B's queue lost A's twin and B was credited A's execution.
    let (old, _) = run(Venue::Cme, &recs, &[a, b], &[5, 6], false);
    assert_eq!(fills(&old, "B"), vec![(30, 10, 100)]);
}

/// A twin added inside an event (ITCH replace: `C` of the old order + `A` of the new id) activates its shadow
/// mid-event; the level activity the event had before is not behind volume for it.
#[test]
fn twin_added_by_a_replace_activates_mid_event() {
    let recs = Scenario::new(Venue::Nasdaq)
        .event(5, |e| e.add(9, BookSide::Ask, 103, 500))
        .event(10, |e| e.add(1, BookSide::Bid, 100, 20).add(7, BookSide::Bid, 100, 10))
        .event(20, |e| e.execute(1, 4).replace(7, 5, 100, 10))
        .event(30, |e| e.execute(1, 16))
        .event(40, |e| e.execute(5, 10))
        .build();
    let x = abs_shadow("X", BookSide::Bid, 100, 10, 20, 0);
    let y = abs_shadow("Y", BookSide::Bid, 100, 10, 15, 0);
    let (sink, _) = run(Venue::Nasdaq, &recs, &[x, y], &[5], true);
    assert_eq!(result(&sink, "X").queue_ahead_at_live, Some(16));
    assert_eq!(fills(&sink, "X"), vec![(40, 10, 100)]);
    // Y (ahead of the twin) is filled by the twin's execution behind it.
    assert_eq!(fills(&sink, "Y"), vec![(40, 10, 100)]);
}

/// Audit minor 15 with twins: a shadow that replaced its twin is placed and filled by the twin's own records, so it
/// is never race-flagged; a plain shadow entered at the same time is.
#[test]
fn twin_shadows_are_never_race_flagged() {
    let recs = Scenario::new(Venue::Nasdaq)
        .event(10, |e| e.add(1, BookSide::Ask, 101, 50).add(9, BookSide::Bid, 98, 500))
        .event(20, |e| e.add(5, BookSide::Ask, 101, 30))
        .event(21, |e| e.add(6, BookSide::Ask, 101, 10))
        .event(30, |e| e.execute_at(5, 30, 100, true))
        .build();
    let shadows = [
        abs_shadow("X", BookSide::Ask, 101, 30, 20, 0),
        abs_shadow("P", BookSide::Ask, 101, 30, 20, 0),
    ];
    let mut cfg = EngineConfig::new(rules(Venue::Nasdaq));
    cfg.race_window_ns = 5;
    let mut eng = Engine::new(cfg, &[], shadows.to_vec(), vec![]).unwrap();
    eng.set_sequence_gap_detection(false);
    eng.set_excluded_orders([(1, 5)]);
    eng.set_twin_linking(true);
    let mut sink = VecSink::default();
    for r in &recs {
        eng.apply(r, &mut sink).unwrap();
    }
    eng.finish(&mut sink).unwrap();
    assert!(!result(&sink, "X").ambiguous_race);
    assert!(result(&sink, "P").ambiguous_race, "P activates 1 ns after its entry");
    let mut rc = RefConfig::new(rules(Venue::Nasdaq));
    rc.exclude_order_ids = vec![5];
    rc.twin_linking = true;
    rc.race_window_ns = 5;
    rc.dq_flags = true;
    let truth = try_reference_shadows(&recs, &rc, &shadows, &[]).unwrap();
    assert_eq!(truth[0].0, sink.shadow_results[0]);
    assert_eq!(truth[1].0, sink.shadow_results[1]);
}
