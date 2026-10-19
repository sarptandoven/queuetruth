//! The E36 / audit-minor scenario fixtures (`qt_testkit::cases`) reproduced by the reference engine:
//! expected fills, end states and race flags per shadow, and the `removal_after_unpaired_fill` counter of the
//! reference data-quality tracker.

use qt_schema::json::Verdict;
use qt_testkit::cases::ruling36_cases;
use qt_testkit::refbook::DqTracker;
use qt_testkit::{RefConfig, try_reference_shadows};
use qt_types::rules;

#[test]
fn ruling36_cases_reference_matches_expectations() {
    for c in ruling36_cases() {
        let mut cfg = RefConfig::new(rules(c.venue));
        cfg.race_window_ns = c.race_window_ns;
        let out = try_reference_shadows(&c.events, &cfg, &c.shadows, &[]).expect("valid case");
        assert_eq!(out.len(), c.expected.len(), "{}", c.name);
        for ((r, fills, traj), want) in out.iter().zip(&c.expected) {
            let ctx = format!("{} / {}", c.name, want.shadow_id);
            assert_eq!(r.shadow_id, want.shadow_id, "{ctx}");
            let got: Vec<_> = fills
                .iter()
                .map(|f| (f.ts_event, f.fill_qty, f.reason, f.fill_px))
                .collect();
            assert_eq!(got, want.fills, "{ctx}");
            assert_eq!(
                (r.end_state, r.ambiguous_race),
                (want.end_state, want.ambiguous_race),
                "{ctx}"
            );
            assert_eq!(r.filled_qty, fills.iter().map(|f| f.fill_qty).sum::<u64>(), "{ctx}");
            // Queue ahead never grows (a re-queued duplicate leaves the queue ahead, never re-enters it).
            for t in traj {
                assert!(t.queue_ahead_after <= t.queue_ahead_before, "{ctx}: {t:?}");
            }
        }
        let mut dq = DqTracker::new(rules(c.venue));
        for e in &c.events {
            dq.push(e);
        }
        let (dq, _) = dq.finish();
        assert_eq!(
            dq.source_specific
                .get("removal_after_unpaired_fill")
                .copied()
                .unwrap_or(0),
            c.removal_after_unpaired_fill,
            "{}",
            c.name
        );
        if c.removal_after_unpaired_fill > 0 {
            assert_eq!(dq.verdict, Verdict::Warnings, "{}: {:?}", c.name, dq.reasons);
        }
    }
}

#[test]
fn ruling36c_hidden_prints_are_info_counters() {
    let c = ruling36_cases()
        .into_iter()
        .find(|c| c.name == "c_hidden_print_display_rule")
        .expect("case");
    let mut dq = DqTracker::new(rules(c.venue));
    for e in &c.events {
        dq.push(e);
    }
    let (dq, _) = dq.finish();
    assert_eq!((dq.hidden_prints, dq.verdict), (6, Verdict::Clean), "{:?}", dq.reasons);
}
