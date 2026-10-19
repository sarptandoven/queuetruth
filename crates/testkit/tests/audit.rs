//! Audit rulings A1 (own orders excluded from the tape: the exact arm reproduces the live log, zero discordance) and
//! A2 (outcome-independent censoring: a calibrated predictor has no fill-rate bias).

use std::path::PathBuf;

use qt_schema::io::read_table_as;
use qt_schema::json::LiveManifest;
use qt_schema::rows::EndState;
use qt_testkit::audit::{censoring_sim, own_orders, own_reference, write_own_orders};
use qt_testkit::{SynthConfig, generate_sessions};
use qt_types::Venue;

#[test]
fn a1_own_orders_self_excluded_exact_arm_has_zero_discordance() {
    let mut c = SynthConfig::for_venue(Venue::Cme, 21);
    c.events_per_instrument = 3_000;
    let out = generate_sessions(&c, 2);
    let orders = own_orders(&out, 40, 21);
    assert!(orders.len() >= 100, "{}", orders.len());
    let filled_live = orders.iter().filter(|o| !o.fills.is_empty()).count();
    let cancelled = orders.iter().filter(|o| o.ts_cancel.is_some()).count();
    assert!(filled_live > 10 && cancelled > 10, "{filled_live} {cancelled}");
    let excl = own_reference(&out, &orders, true).expect("reference");
    let (mut both, mut neither) = (0, 0);
    for (o, (r, f, _)) in orders.iter().zip(&excl) {
        let live: Vec<(i64, u64)> = o.fills.iter().map(|x| (x.0, x.1)).collect();
        let sim: Vec<(i64, u64)> = f.iter().map(|x| (x.ts_event, x.fill_qty)).collect();
        assert_eq!(sim, live, "{}", o.order_key());
        let live_full = o.fills.iter().map(|x| x.1).sum::<u64>() == o.qty;
        assert_eq!(
            r.end_state,
            if live_full {
                EndState::Filled
            } else {
                EndState::Cancelled
            },
            "{}",
            o.order_key()
        );
        if live.is_empty() { neither += 1 } else { both += 1 }
    }
    assert_eq!(both + neither, orders.len());
    // Without exclusion the twin sits ahead of its own shadow: live fills the sim misses (the A1 bias).
    let naive = own_reference(&out, &orders, false).expect("reference");
    let live_only = orders
        .iter()
        .zip(&naive)
        .filter(|(o, t)| !o.fills.is_empty() && t.0.filled_qty == 0)
        .count();
    let sim_only = orders
        .iter()
        .zip(&naive)
        .filter(|(o, t)| o.fills.is_empty() && t.0.filled_qty > 0)
        .count();
    assert!(live_only > 0);
    assert_eq!(sim_only, 0);

    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!("qt-testkit-own-{}", std::process::id()));
    let n_events = write_own_orders(&out, &orders, &dir).expect("write");
    let rows = |p: &str, t: &str| -> usize {
        read_table_as(&dir.join(p), t)
            .expect("open")
            .map(|b| b.expect("batch").num_rows())
            .sum()
    };
    assert_eq!(rows("live/orders.parquet", "qt.live_orders"), orders.len());
    assert_eq!(rows("live/events.parquet", "qt.live_events") as u64, n_events);
    assert_eq!(rows("schedule.parquet", "qt.shadow_spec"), orders.len());
    assert_eq!(rows("expected_results.parquet", "qt.shadow_results"), orders.len());
    let m: LiveManifest =
        serde_json::from_slice(&std::fs::read(dir.join("live/manifest.json")).expect("read")).expect("parse");
    assert_eq!((m.n_orders, m.n_events), (orders.len() as u64, n_events));
    std::fs::remove_dir_all(dir).ok();
}

#[test]
fn a2_calibrated_predictor_unbiased_only_with_outcome_independent_censoring() {
    let (res, pred) = censoring_sim(20_000, 1, 1_000_000_000, 0);
    let n = pred.len() as f64;
    let bias_all = pred
        .iter()
        .map(|p| p.p_fill - f64::from(u8::from(p.truth_filled == Some(true))))
        .sum::<f64>()
        / n;
    assert!(bias_all.abs() < 0.01, "{bias_all}");
    // Pre-audit rule: drop unfilled rows whose life ended before the horizon.
    let kept: Vec<_> = pred
        .iter()
        .zip(&res)
        .filter(|(p, r)| {
            p.truth_filled == Some(true) || r.ts_cancel_effective.unwrap_or(i64::MAX) - r.ts_live >= p.horizon_ns
        })
        .map(|(p, _)| p)
        .collect();
    let bias_old = kept
        .iter()
        .map(|p| p.p_fill - f64::from(u8::from(p.truth_filled == Some(true))))
        .sum::<f64>()
        / kept.len() as f64;
    assert!(bias_old < -0.08, "{bias_old}");
    assert!(pred.iter().all(|p| !p.censored));
}
