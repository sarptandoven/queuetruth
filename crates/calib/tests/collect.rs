//! Collection tests (SPEC 5.4 C1/C3 inputs) with a local mock NT `queue_position` tracker: a hand-built scenario with
//! hand-computed alloc rows and touch records, and O-CAL-5 (testkit market part, gate test): every alloc row matches
//! the engine's full trajectory and the independent testkit reference (pure cancels at the level).

use std::collections::BTreeMap;

use qt_calib::collect::{CollectConfig, collect_events};
use qt_replay::{Engine, EngineConfig, Sink, TrajectoryMode};
use qt_schema::rows::{AllocEventRow, PriceSpec, ShadowSource, ShadowSpec, TrajectoryKind, TrajectoryRow};
use qt_testkit::scenario::Scenario;
use qt_testkit::{RefConfig, SynthConfig, TrajectoryDetail, generate, try_reference_shadows};
use qt_types::{BookSide, Side, Ts, Venue, Xoshiro256StarStar, rules};
use qt_zoo::{Exchange, Family, FillDelta, Mode, ModelId, ModelInput, QState, QueueModel, ShadowL2};

/// SPEC 5.3.3 (d) without the faithful hidden-trade and out-of-view details.
struct MockNt(ModelId);

impl MockNt {
    fn new() -> Self {
        Self(ModelId {
            family: Family::NtQueueL2,
            params: BTreeMap::new(),
            mode: Mode::Corrected,
            exchange: Exchange::Partial,
        })
    }
}

impl QueueModel for MockNt {
    fn id(&self) -> &ModelId {
        &self.0
    }
    fn input(&self) -> ModelInput {
        ModelInput::L2
    }
    fn on_arrive(&self, s: &mut QState, _o: &ShadowL2, q: f64, _rng: &mut Xoshiro256StarStar) {
        s.front = q;
    }
    fn on_trade(
        &self,
        s: &mut QState,
        _o: &ShadowL2,
        _ts: Ts,
        v: f64,
        _a: Side,
        _rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        let consumed = v.min(s.front);
        let remaining = v - consumed;
        if remaining <= 0.0 {
            s.front = (s.front - v).max(0.0);
            FillDelta::None
        } else {
            s.front = 0.0;
            FillDelta::Qty(remaining.min(s.leaves))
        }
    }
    fn on_depth(
        &self,
        s: &mut QState,
        _o: &ShadowL2,
        _ts: Ts,
        _p: f64,
        new: f64,
        _f: u8,
        _rng: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        s.front = if new == 0.0 { 0.0 } else { s.front.min(new) };
        FillDelta::None
    }
    fn queue_ahead(&self, s: &QState) -> f64 {
        s.front
    }
}

fn cfg() -> CollectConfig {
    CollectConfig {
        l2: qt_l2::L2Config::default(),
        seed: 0,
        faithful: false,
    }
}

fn shadow(id: &str, side: BookSide, price: PriceSpec, qty: u64, ts_submit: Ts) -> ShadowSpec {
    ShadowSpec {
        shadow_id: id.into(),
        instrument_id: 1,
        side,
        price,
        qty,
        ts_submit,
        latency_ns: 0,
        ts_cancel: None,
        group: None,
        source: ShadowSource::User,
        order_key: None,
    }
}

#[test]
fn collect_scenario_hand_computed() {
    let t0: Ts = 1_781_500_000_000_000_000;
    let sc = Scenario::new(Venue::Cme)
        .instrument(1)
        .event(t0 + 1000, |e| {
            e.add(1, BookSide::Bid, 100, 10)
                .add(2, BookSide::Bid, 100, 5)
                .add(9, BookSide::Ask, 101, 10)
        })
        .event(t0 + 2000, |e| e.add(3, BookSide::Bid, 100, 4))
        .event(t0 + 3000, |e| e.cancel(1, 4).cancel(3, 1))
        .event(t0 + 4000, |e| e.trade(Side::Ask, 100, 6).fill(1, 6))
        .event(t0 + 5000, |e| e.trade(Side::Ask, 100, 8).fill(2, 5).fill(3, 3));
    let inst = sc.instrument_rows(1).remove(0);
    let events = sc.build();
    let sh = vec![shadow("s1", BookSide::Bid, PriceSpec::Abs(100), 3, t0 + 1500)];
    let nt = MockNt::new();
    let got = collect_events(events.into_iter().map(Ok), &inst, rules(Venue::Cme), sh, &nt, &cfg())
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(got.alloc.len(), 1, "{:?}", got.alloc);
    let a = &got.alloc[0];
    assert_eq!(
        (a.ts_event, a.front, a.back, a.ahead_cancel, a.behind_cancel),
        (t0 + 3000, 15.0, 4.0, 4.0, 1.0)
    );
    assert_eq!((a.l2_prev, a.l2_new, a.l2_trades), (Some(19.0), Some(14.0), Some(0.0)));
    assert_eq!(got.alloc_dist, vec![0]);
    let t: Vec<_> = got
        .touch
        .iter()
        .map(|r| {
            (
                r.touch_index,
                r.nt_front_before,
                r.nt_excess,
                r.truth_filled_now,
                r.truth_first_fill_here,
            )
        })
        .collect();
    assert_eq!(t, vec![(1, 14.0, 0.0, false, false), (2, 8.0, 0.0, true, true)]);
    assert_eq!(got.shadows.len(), 1);
    assert_eq!(got.shadows[0].ts_live, t0 + 1500);
    let fit = qt_calib::fit_nt_touch(&got.touch, false);
    assert_eq!((fit.n_filled, fit.touches), (1, 2));
}

#[test]
fn collect_trade_through_first_fill_is_censored() {
    // A bid shadow at 100 sees one at-price touch, then a trade through 99 fills it: its touch rows stop there and
    // C3 censors it with M = 1 (audit ruling A7).
    let t0: Ts = 1_781_500_000_000_000_000;
    let sc = Scenario::new(Venue::Cme)
        .instrument(1)
        .event(t0 + 1000, |e| {
            e.add(1, BookSide::Bid, 100, 10)
                .add(2, BookSide::Bid, 99, 5)
                .add(9, BookSide::Ask, 101, 10)
        })
        .event(t0 + 2000, |e| e.trade(Side::Ask, 100, 2).fill(1, 2))
        .event(t0 + 3000, |e| {
            e.trade(Side::Ask, 100, 8).fill(1, 8).trade(Side::Ask, 99, 1).fill(2, 1)
        })
        .event(t0 + 4000, |e| e.add(4, BookSide::Bid, 100, 3))
        .event(t0 + 5000, |e| e.trade(Side::Ask, 100, 3).fill(4, 3));
    let inst = sc.instrument_rows(1).remove(0);
    let sh = vec![shadow("s1", BookSide::Bid, PriceSpec::Abs(100), 1, t0 + 1500)];
    let got = collect_events(
        sc.build().into_iter().map(Ok),
        &inst,
        rules(Venue::Cme),
        sh,
        &MockNt::new(),
        &cfg(),
    )
    .unwrap_or_else(|e| panic!("{e}"));
    assert!(got.touch.iter().all(|r| !r.truth_first_fill_here));
    // open conflict 5 (E28 / E38): "touches strictly before" is per EVENT, so the at-price touch in the
    // trade-through's own event (t0 + 3000) is not counted: M = 1
    let kept: Vec<(u32, Ts)> = got.touch.iter().map(|r| (r.touch_index, r.ts_event)).collect();
    assert_eq!(kept, vec![(1, t0 + 2000)], "{:?}", got.touch);
    let fit = qt_calib::fit_nt_touch(&got.touch, false);
    assert_eq!((fit.n_orders, fit.n_filled, fit.touches), (1, 0, 1));
}

#[derive(Default)]
struct TrajSink(Vec<TrajectoryRow>);

impl Sink for TrajSink {
    fn trajectory(&mut self, r: &TrajectoryRow) -> qt_types::QtResult<()> {
        self.0.push(r.clone());
        Ok(())
    }
}

type Key = (String, Ts);

fn cancels_by_key(rows: &[TrajectoryRow]) -> BTreeMap<Key, (u64, u64, u64, u64)> {
    let mut m: BTreeMap<Key, (u64, u64, u64, u64)> = BTreeMap::new();
    for r in rows
        .iter()
        .filter(|r| r.kind == TrajectoryKind::Event && r.cancel_ahead_qty + r.cancel_behind_qty > 0)
    {
        let e = m.entry((r.shadow_id.clone(), r.ts_event)).or_default();
        e.0 += r.cancel_ahead_qty;
        e.1 += r.cancel_behind_qty;
        e.2 = r.queue_ahead_before;
        e.3 = r.level_qty_before;
    }
    m
}

fn alloc_by_key(rows: &[AllocEventRow]) -> BTreeMap<Key, (u64, u64, u64, u64)> {
    let mut m: BTreeMap<Key, (u64, u64, u64, u64)> = BTreeMap::new();
    for r in rows {
        let e = m.entry((r.shadow_id.clone(), r.ts_event)).or_default();
        e.0 += r.ahead_cancel as u64;
        e.1 += r.behind_cancel as u64;
        e.2 = r.front as u64;
        e.3 = (r.front + r.back) as u64;
    }
    m
}

#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs replay trajectories and the testkit reference"
)]
fn o_cal_5_alloc_events_match_trajectory_on_testkit_market() {
    let mut sc = SynthConfig::for_venue(Venue::Cme, 7);
    sc.instruments = 1;
    sc.events_per_instrument = 20_000;
    let out = generate(&sc);
    let inst = out.instruments[0].clone();
    let ts0 = out.events.first().map_or(0, |e| e.ts_event);
    let shadows: Vec<ShadowSpec> = (0..200)
        .map(|i| {
            let side = if i % 2 == 0 { BookSide::Bid } else { BookSide::Ask };
            let off = -((i / 2) % 2);
            shadow(
                &format!("s{i}"),
                side,
                PriceSpec::Touch { offset_ticks: off },
                1 + (i % 3) as u64,
                ts0 + i as i64 * 3_000_000_000,
            )
        })
        .collect();
    let rules = rules(Venue::Cme);
    let got = collect_events(
        out.events.iter().copied().map(Ok),
        &inst,
        rules,
        shadows.clone(),
        &MockNt::new(),
        &cfg(),
    )
    .unwrap_or_else(|e| panic!("{e}"));
    assert!(got.alloc.len() > 100, "too few alloc events: {}", got.alloc.len());
    let mine = alloc_by_key(&got.alloc);

    let mut ec = EngineConfig::new(rules);
    ec.trajectory = TrajectoryMode::Full;
    let mut engine =
        Engine::new(ec, std::slice::from_ref(&inst), shadows.clone(), Vec::new()).unwrap_or_else(|e| panic!("{e}"));
    let mut sink = TrajSink::default();
    for e in &out.events {
        engine.apply(e, &mut sink).unwrap_or_else(|e| panic!("{e}"));
    }
    engine.finish(&mut sink).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(mine, cancels_by_key(&sink.0), "alloc rows vs engine trajectory");

    let mut rc = RefConfig::new(rules);
    rc.trajectory = TrajectoryDetail::Full;
    rc.instruments = vec![inst.clone()];
    rc.default_tick = inst.tick_size;
    let reference = try_reference_shadows(&out.events, &rc, &shadows, &[]).unwrap_or_else(|e| panic!("{e}"));
    let traj: Vec<TrajectoryRow> = reference.into_iter().flat_map(|(_, _, t)| t).collect();
    assert_eq!(mine, cancels_by_key(&traj), "alloc rows vs testkit reference");
}
