//! O-SH-3: shadow properties on random venue-consistent streams with random shadows (abs and touch, latency with
//! jitter, cancels, amends), under both fill rules: fills add up and never exceed the quantity, `cum_qty` strictly
//! increases, queue ahead never grows along the trajectory and stays within the level, a `queue_depleted` fill is
//! backed by volume (Exact: executions behind or hidden prints; Et: cumulative traded at price beyond queue ahead),
//! and replays with and without shadows give identical BBO, trades, depth and rank-trace rows.

mod common;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use common::{Market, cfg, instrument, random_shadows};
use qt_replay::sinks::VecSink;
use qt_replay::{BboMode, Engine, EventEnd, EventObserver, FillRule, ShadowEventTruth, TrajectoryMode};
use qt_schema::rows::{FillReason, TrajectoryKind};
use qt_types::{LatencyModel, Venue};

#[derive(Default)]
struct Truths(Arc<Mutex<Vec<(i64, ShadowEventTruth)>>>);

impl EventObserver for Truths {
    fn on_event_end(&mut self, end: &EventEnd<'_>) {
        if let Ok(mut v) = self.0.lock() {
            v.extend(end.shadow_truth.iter().map(|t| (end.ts_event, t.clone())));
        }
    }
}

fn check(seed: u64, venue: Venue, rule: FillRule) {
    let events = Market::generate(seed, venue, 1200);
    let (shadows, amends) = random_shadows(seed, &events, 60);
    let mut c = cfg(venue);
    c.depth_levels = 3;
    c.bbo = BboMode::Full;
    c.rank_oracle = true;
    c.trajectory = TrajectoryMode::Full;
    c.seed = seed;
    c.latency = LatencyModel {
        feed_ns: 1_000,
        decision_ns: 500,
        gateway_ns: 700,
        jitter_ns: 50_000,
    };
    let truths = Arc::new(Mutex::new(Vec::new()));
    let mut eng = Engine::new(c.clone(), &[instrument(venue)], shadows.clone(), amends).unwrap();
    eng.set_fill_rule(rule);
    eng.add_observer(Box::new(Truths(truths.clone())));
    let mut with = VecSink::default();
    for e in &events {
        eng.apply(e, &mut with).unwrap();
    }
    let rep = eng.finish(&mut with).unwrap();
    let ctx = format!("seed {seed} {venue:?} {rule:?}");

    // Fills.
    let mut by: HashMap<&str, Vec<&qt_schema::rows::ShadowFillRow>> = HashMap::new();
    for f in &with.shadow_fills {
        by.entry(f.shadow_id.as_str()).or_default().push(f);
    }
    for r in &with.shadow_results {
        let fs = by.get(r.shadow_id.as_str()).cloned().unwrap_or_default();
        let sum: u64 = fs.iter().map(|f| f.fill_qty).sum();
        assert_eq!(sum, r.filled_qty, "{ctx} {}", r.shadow_id);
        assert!(r.filled_qty <= r.qty, "{ctx}");
        assert_eq!(fs.len() as u32, r.n_fills, "{ctx}");
        for (k, w) in fs.iter().enumerate() {
            assert_eq!(w.fill_seq, k as u32 + 1, "{ctx}");
            assert!(w.fill_qty > 0, "{ctx}");
            if k > 0 {
                assert!(w.cum_qty > fs[k - 1].cum_qty, "{ctx}");
            }
        }
        if let Some(f) = fs.first() {
            assert_eq!(Some(f.ts_event), r.ts_first_fill, "{ctx}");
        }
    }
    // Trajectory.
    for t in &with.trajectory {
        assert!(t.queue_ahead_after <= t.queue_ahead_before, "{ctx} {t:?}");
        // Exact queue ahead is a subset of the level (et's print-based queue can go stale: audit item 4).
        if t.kind == TrajectoryKind::Event && rule == FillRule::Exact {
            assert!(t.queue_ahead_after <= t.level_qty_after, "{ctx} {t:?}");
        }
    }
    // queue_depleted fills are backed by volume.
    let truths = truths.lock().unwrap().clone();
    let idx: HashMap<&str, u32> = shadows
        .iter()
        .enumerate()
        .map(|(i, s)| (s.shadow_id.as_str(), i as u32))
        .collect();
    for f in with
        .shadow_fills
        .iter()
        .filter(|f| f.reason == FillReason::QueueDepleted)
    {
        match rule {
            FillRule::Exact => {
                if venue != Venue::Nasdaq {
                    assert!(f.behind_volume_executed >= f.fill_qty, "{ctx} {f:?}");
                } else {
                    assert!(
                        f.behind_volume_executed > 0 || rep.diagnostics.hidden_print_fills > 0,
                        "{ctx} {f:?}"
                    );
                }
            }
            FillRule::Et => {
                let i = idx[f.shadow_id.as_str()];
                let r = with.shadow_results.iter().find(|r| r.shadow_id == f.shadow_id).unwrap();
                let (mut traded, mut cancelled) = (0u64, 0u64);
                for (ts, t) in truths.iter().filter(|(_, t)| t.shadow_idx == i) {
                    if *ts > f.ts_event {
                        break;
                    }
                    traded += t.traded_at_px;
                    cancelled += t.cancel_ahead;
                }
                let ahead = r.queue_ahead_at_live.unwrap().saturating_sub(cancelled);
                assert!(traded > ahead, "{ctx} {f:?} traded {traded} ahead {ahead}");
            }
        }
    }
    // Shadows never change the market outputs.
    let mut without = VecSink::default();
    let mut eng = Engine::new(c, &[instrument(venue)], vec![], vec![]).unwrap();
    for e in &events {
        eng.apply(e, &mut without).unwrap();
    }
    eng.finish(&mut without).unwrap();
    assert_eq!(with.bbo, without.bbo, "{ctx}");
    assert_eq!(with.trades, without.trades, "{ctx}");
    assert_eq!(with.depth, without.depth, "{ctx}");
    assert_eq!(with.rank_trace, without.rank_trace, "{ctx}");
    assert!(!with.bbo.is_empty());
}

#[test]
fn o_sh_3_shadow_properties_on_random_streams() {
    let mut fills = 0;
    for venue in [Venue::Cme, Venue::Nasdaq, Venue::Generic] {
        for rule in [FillRule::Exact, FillRule::Et] {
            for seed in 0..25 {
                check(seed, venue, rule);
                fills += 1;
            }
        }
    }
    assert_eq!(fills, 150);
}

/// The generator does produce fills of every kind the properties talk about.
#[test]
fn o_sh_3_generator_exercises_fills() {
    let mut reasons = std::collections::BTreeSet::new();
    for seed in 0..10 {
        let events = Market::generate(seed, Venue::Cme, 1200);
        let (shadows, amends) = random_shadows(seed, &events, 60);
        let (sink, _) = common::run(cfg(Venue::Cme), &[instrument(Venue::Cme)], &events, shadows, amends);
        reasons.extend(sink.shadow_fills.iter().map(|f| f.reason));
    }
    assert!(reasons.contains(&FillReason::QueueDepleted), "{reasons:?}");
    assert!(reasons.contains(&FillReason::TradeThrough), "{reasons:?}");
}
