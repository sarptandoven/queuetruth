//! Properties of the reference shadow engine on generated streams with random shadows (abs and touch, latency with
//! jitter, cancels, amends): fill accounting, trajectory monotonicity, one live/end row, shadow independence,
//! determinism and jitter seeding.

use proptest::prelude::*;
use qt_schema::rows::{EndState, PriceSpec, ShadowAmend, ShadowSource, ShadowSpec, TrajectoryKind};
use qt_testkit::{RefConfig, SynthConfig, TrajectoryDetail, generate, try_reference_shadows};
use qt_types::{BookSide, LatencyModel, Venue, rules};

fn venue_of(i: u8) -> Venue {
    [Venue::Cme, Venue::Nasdaq, Venue::Generic][usize::from(i % 3)]
}

#[derive(Clone, Debug)]
struct Raw {
    side: bool,
    touch: Option<i32>,
    px_off: i64,
    qty: u64,
    at: u16,
    latency: i64,
    cancel: Option<i64>,
    amend: Option<(i64, u64)>,
}

fn raw() -> impl Strategy<Value = Raw> {
    (
        any::<bool>(),
        prop::option::of(-2i32..=1),
        -3i64..=3,
        1u64..40,
        0u16..1000,
        prop_oneof![Just(0i64), 0i64..3_000_000],
        prop::option::of(1i64..30_000_000_000),
        prop::option::of((1i64..10_000_000_000, 0u64..40)),
    )
        .prop_map(|(side, touch, px_off, qty, at, latency, cancel, amend)| Raw {
            side,
            touch,
            px_off,
            qty,
            at,
            latency,
            cancel,
            amend,
        })
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 48, ..ProptestConfig::default() })]
    #[test]
    fn reference_shadow_properties(seed in 0u64..1000, v in 0u8..3, raws in prop::collection::vec(raw(), 1..12), jitter in 0i64..2_000_000, full in any::<bool>()) {
        let venue = venue_of(v);
        let mut c = SynthConfig::for_venue(venue, seed);
        c.instruments = 1;
        c.events_per_instrument = 700;
        c.p_clear = if venue == Venue::Nasdaq { 0.0 } else { 0.003 };
        let out = generate(&c);
        let t0 = out.events[0].ts_event;
        let t1 = out.events[out.events.len() - 1].ts_event;
        let mid = out.expected_bbo[out.expected_bbo.len() / 2];
        let mut shadows = Vec::new();
        let mut amends = Vec::new();
        for (i, r) in raws.iter().enumerate() {
            let side = if r.side { BookSide::Bid } else { BookSide::Ask };
            let base = mid.bid_px.or(mid.ask_px).unwrap_or(c.base_px);
            let price = match r.touch {
                Some(o) => PriceSpec::Touch { offset_ticks: o },
                None => PriceSpec::Abs(base + r.px_off * c.tick),
            };
            let ts_submit = t0 + (t1 - t0) / 1000 * i64::from(r.at);
            let id = format!("p{i}");
            if let Some((dt, q)) = r.amend {
                if q < r.qty {
                    amends.push(ShadowAmend { shadow_id: id.clone(), ts_send: ts_submit + dt, new_qty: q });
                }
            }
            shadows.push(ShadowSpec {
                shadow_id: id,
                instrument_id: 1,
                side,
                price,
                qty: r.qty,
                ts_submit,
                latency_ns: r.latency,
                ts_cancel: r.cancel.map(|d| ts_submit + d),
                group: None,
                source: ShadowSource::Sampled,
                order_key: None,
            });
        }
        let mut cfg = RefConfig::new(rules(venue));
        cfg.latency = LatencyModel { feed_ns: 1_000, decision_ns: 2_000, gateway_ns: 3_000, jitter_ns: jitter };
        cfg.seed = seed;
        cfg.instruments = out.instruments.clone();
        cfg.trajectory = if full { TrajectoryDetail::Full } else { TrajectoryDetail::Ahead };
        let res = try_reference_shadows(&out.events, &cfg, &shadows, &amends).expect("valid");
        prop_assert_eq!(&res, &try_reference_shadows(&out.events, &cfg, &shadows, &amends).expect("valid"));
        for (k, (r, fills, traj)) in res.iter().enumerate() {
            let sum: u64 = fills.iter().map(|f| f.fill_qty).sum();
            prop_assert_eq!(sum, r.filled_qty);
            prop_assert!(r.filled_qty <= r.qty);
            prop_assert_eq!(r.n_fills as usize, fills.len());
            for w in fills.windows(2) {
                prop_assert!(w[1].cum_qty > w[0].cum_qty);
                prop_assert!(w[1].ts_event >= w[0].ts_event);
            }
            if r.end_state == EndState::Filled {
                prop_assert_eq!(r.filled_qty, r.qty);
                prop_assert!(r.ts_full_fill.is_some());
            }
            prop_assert_eq!(traj.iter().filter(|t| t.kind == TrajectoryKind::End).count(), 1);
            prop_assert_eq!(traj.last().map(|t| t.kind), Some(TrajectoryKind::End));
            prop_assert!(traj.iter().filter(|t| t.kind == TrajectoryKind::Live).count() <= 1);
            prop_assert_eq!(traj.iter().any(|t| t.kind == TrajectoryKind::Live), r.queue_ahead_at_live.is_some());
            for t in traj.iter().filter(|t| t.kind == TrajectoryKind::Event) {
                prop_assert!(t.queue_ahead_after <= t.queue_ahead_before);
                prop_assert!(t.rank_after <= t.rank_before);
                prop_assert!(t.cancel_ahead_qty <= t.queue_ahead_before);
            }
            prop_assert!(r.latency_applied_ns >= 6_000 + shadows[k].latency_ns);
            prop_assert!(r.latency_applied_ns <= 6_000 + shadows[k].latency_ns + jitter);
            // Independence: the shadow alone gives the same answer.
            let my_amends: Vec<_> = amends.iter().filter(|a| a.shadow_id == shadows[k].shadow_id).cloned().collect();
            let alone = try_reference_shadows(&out.events, &cfg, &shadows[k..=k], &my_amends).expect("valid");
            prop_assert_eq!(&alone[0], &res[k]);
        }
    }
}

#[test]
fn reference_jitter_follows_stable_seed_streams() {
    let mut c = SynthConfig::for_venue(Venue::Cme, 3);
    c.instruments = 1;
    c.events_per_instrument = 200;
    let out = generate(&c);
    let s = ShadowSpec {
        shadow_id: "j".into(),
        instrument_id: 1,
        side: BookSide::Bid,
        price: PriceSpec::Touch { offset_ticks: 0 },
        qty: 1,
        ts_submit: out.events[10].ts_event,
        latency_ns: 7,
        ts_cancel: Some(out.events[10].ts_event + 1),
        group: None,
        source: ShadowSource::User,
        order_key: None,
    };
    let mut cfg = RefConfig::new(rules(Venue::Cme));
    cfg.latency.jitter_ns = 1_000;
    cfg.seed = 42;
    let r = &try_reference_shadows(&out.events, &cfg, std::slice::from_ref(&s), &[]).expect("ok")[0].0;
    let mut rng = qt_types::Xoshiro256StarStar::new(qt_types::stable_seed(42, &[b"latency", b"j"]));
    let js = rng.range_inclusive(0, 1_000) as i64;
    let jc = rng.range_inclusive(0, 1_000) as i64;
    assert_eq!(r.latency_applied_ns, 7 + js);
    assert_eq!(r.ts_cancel_effective, Some(s.ts_submit + 1 + 7 + jc));
}

#[test]
fn reference_rejects_bad_amends_and_latency() {
    let out = generate(&SynthConfig {
        events_per_instrument: 50,
        ..SynthConfig::for_venue(Venue::Cme, 1)
    });
    let s = ShadowSpec {
        shadow_id: "a".into(),
        instrument_id: 1,
        side: BookSide::Bid,
        price: PriceSpec::Abs(1),
        qty: 5,
        ts_submit: 100,
        latency_ns: 0,
        ts_cancel: None,
        group: None,
        source: ShadowSource::User,
        order_key: None,
    };
    let cfg = RefConfig::new(rules(Venue::Cme));
    let amend = |ts_send, new_qty| ShadowAmend {
        shadow_id: "a".into(),
        ts_send,
        new_qty,
    };
    for bad in [
        vec![amend(99, 3)],
        vec![amend(100, 5)],
        vec![amend(100, 3), amend(101, 3)],
    ] {
        let e = try_reference_shadows(&out.events, &cfg, std::slice::from_ref(&s), &bad).expect_err("bad amend");
        assert_eq!(e.code(), "QT-INPUT-002");
    }
    let mut neg = cfg.clone();
    neg.latency.gateway_ns = -1;
    assert_eq!(
        try_reference_shadows(&out.events, &neg, std::slice::from_ref(&s), &[])
            .expect_err("neg")
            .code(),
        "QT-CONFIG-001"
    );
    let missing = ShadowSpec {
        instrument_id: 9,
        ..s.clone()
    };
    let r = &try_reference_shadows(&out.events, &cfg, &[missing], &[]).expect("ok")[0].0;
    assert_eq!((r.end_state, r.ts_activated_event), (EndState::CensoredGap, None));
}
