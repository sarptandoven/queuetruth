//! O-FEAT-1 (no lookahead, property test) and O-FEAT-2 (hand-computed features on two scenario fixtures) with local
//! mock trackers; the O-FEAT-2 tracker values with the real `qt_zoo` models are a gate test.

use std::collections::BTreeMap;

use proptest::prelude::*;
use qt_features::extract::{ExtractConfig, PopOrder, extract_events};
use qt_features::{FEATURES, PRIVILEGED, feature_names};
use qt_replay::{Engine, EngineConfig, Sink};
use qt_schema::rows::{Cause, FeatureRow, PriceSpec, RealOrderRow, ShadowSource, ShadowSpec};
use qt_testkit::scenario::Scenario;
use qt_testkit::{SynthConfig, generate};
use qt_types::{BookSide, Instrument, MboEvent, Side, Ts, Venue, Xoshiro256StarStar, rules};
use qt_zoo::{Exchange, Family, FillDelta, Mode, ModelId, ModelInput, QState, QueueModel, ShadowL2, prob};

const S: i64 = 1_000_000_000;

fn id(family: Family, params: &[(&str, f64)]) -> ModelId {
    ModelId {
        family,
        params: params.iter().map(|(k, v)| ((*k).to_owned(), *v)).collect(),
        mode: Mode::Faithful,
        exchange: Exchange::Partial,
    }
}

/// hbt RiskAdverse (SPEC 5.3.3 a, faithful, lot 1).
struct MockRa(ModelId);
/// hbt ProbQueue with power3 (SPEC 5.3.3 b, faithful).
struct MockP3(ModelId, f64);

fn exec(s: &mut QState, lot: f64) {
    if prob::hbt_exec_lots(s.front, lot) > 0 {
        s.front = 0.0;
    }
}

impl QueueModel for MockRa {
    fn id(&self) -> &ModelId {
        &self.0
    }
    fn input(&self) -> ModelInput {
        ModelInput::L2
    }
    fn on_arrive(&self, s: &mut QState, _o: &ShadowL2, q: f64, _r: &mut Xoshiro256StarStar) {
        s.front = q;
    }
    fn on_trade(
        &self,
        s: &mut QState,
        o: &ShadowL2,
        _t: Ts,
        v: f64,
        _a: Side,
        _r: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        s.front -= v;
        exec(s, o.lot);
        FillDelta::None
    }
    fn on_depth(
        &self,
        s: &mut QState,
        _o: &ShadowL2,
        _t: Ts,
        _p: f64,
        new: f64,
        _f: u8,
        _r: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        s.front = s.front.min(new);
        FillDelta::None
    }
    fn queue_ahead(&self, s: &QState) -> f64 {
        s.front
    }
}

impl QueueModel for MockP3 {
    fn id(&self) -> &ModelId {
        &self.0
    }
    fn input(&self) -> ModelInput {
        ModelInput::L2
    }
    fn on_arrive(&self, s: &mut QState, _o: &ShadowL2, q: f64, _r: &mut Xoshiro256StarStar) {
        s.front = q;
        s.cum_trade = 0.0;
    }
    fn on_trade(
        &self,
        s: &mut QState,
        o: &ShadowL2,
        _t: Ts,
        v: f64,
        _a: Side,
        _r: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        s.front -= v;
        s.cum_trade += v;
        exec(s, o.lot);
        FillDelta::None
    }
    fn on_depth(
        &self,
        s: &mut QState,
        _o: &ShadowL2,
        _t: Ts,
        prev: f64,
        new: f64,
        _f: u8,
        _r: &mut Xoshiro256StarStar,
    ) -> FillDelta {
        let mut chg = prev - new;
        chg -= s.cum_trade;
        s.cum_trade = 0.0;
        if chg < 0.0 {
            s.front = s.front.min(new);
            return FillDelta::None;
        }
        let front = s.front;
        let back = prev - front;
        let mut pi = prob::power3(self.1, front, back);
        if pi.is_infinite() {
            pi = 1.0;
        }
        let est = front - (1.0 - pi) * chg + (back - pi * chg).min(0.0);
        s.front = est.min(new);
        FillDelta::None
    }
    fn queue_ahead(&self, s: &QState) -> f64 {
        s.front
    }
}

fn mock_models() -> Vec<Box<dyn QueueModel>> {
    vec![
        Box::new(MockRa(id(Family::HbtRiskAdverse, &[]))),
        Box::new(MockP3(id(Family::HbtPower3, &[("n", 1.0)]), 1.0)),
    ]
}

fn cfg(landmarks: &[i64], ra: bool) -> ExtractConfig {
    ExtractConfig {
        l2: qt_l2::L2Config::default(),
        landmarks_ns: landmarks.to_vec(),
        cancel_away_ticks: 2,
        h_max_ns: 60 * S,
        privileged: true,
        seed: 0,
        ra_index: ra.then_some(0),
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

fn t0() -> Ts {
    SynthConfig::for_venue(Venue::Cme, 0).ts_start
}

fn scenario_one() -> (Vec<MboEvent>, Instrument) {
    let t0 = t0();
    let sc = Scenario::new(Venue::Cme)
        .instrument(1)
        .event(t0 + S, |e| {
            e.add(1, BookSide::Bid, 100, 10)
                .add(2, BookSide::Bid, 100, 5)
                .add(3, BookSide::Bid, 99, 7)
                .add(6, BookSide::Bid, 98, 2)
                .add(4, BookSide::Ask, 101, 8)
                .add(5, BookSide::Ask, 102, 4)
                .add(7, BookSide::Ask, 103, 1)
        })
        .event(t0 + 2 * S, |e| e.add(8, BookSide::Bid, 100, 3))
        .event(t0 + 3 * S, |e| e.cancel(1, 4))
        .event(t0 + 4 * S, |e| e.trade(Side::Ask, 100, 6).fill(1, 6))
        .event(t0 + 4 * S + S / 2, |e| e.add(9, BookSide::Ask, 101, 2))
        .event(t0 + 8 * S, |e| e.add(10, BookSide::Ask, 104, 1));
    let inst = sc.instrument_rows(1).remove(0);
    (sc.build(), inst)
}

fn col(names: &[String], row: &FeatureRow, name: &str) -> Option<f64> {
    let i = names
        .iter()
        .position(|n| n == name)
        .unwrap_or_else(|| panic!("no column {name}"));
    row.values[i]
}

fn names_with_priv(models: &[Box<dyn QueueModel>]) -> Vec<String> {
    let ids: Vec<ModelId> = models.iter().map(|m| m.id().clone()).collect();
    let mut n = feature_names(&ids);
    n.extend(PRIVILEGED.iter().map(|s| (*s).to_owned()));
    n
}

fn tod(inst: &Instrument, t: Ts) -> (f64, f64) {
    let open = inst.midnight_utc_ns as f64 + f64::from(inst.open_s) * 1e9;
    let len = (f64::from(inst.close_s) - f64::from(inst.open_s)) * 1e9;
    let a = 2.0 * std::f64::consts::PI * ((t as f64 - open) / len).clamp(0.0, 1.0);
    (a.sin(), a.cos())
}

fn close(a: Option<f64>, b: f64) -> bool {
    a.is_some_and(|a| (a - b).abs() < 1e-12)
}

fn check_scenario_one(models: Vec<Box<dyn QueueModel>>) {
    let (events, inst) = scenario_one();
    let t0 = t0();
    let pop = vec![PopOrder::Shadow(shadow(
        "s1",
        BookSide::Bid,
        PriceSpec::Abs(100),
        2,
        t0 + S + S / 2,
    ))];
    let rows = extract_events(
        events.into_iter().map(Ok),
        &inst,
        rules(Venue::Cme),
        pop,
        &models,
        &cfg(&[0, S, 5 * S], true),
    )
    .unwrap_or_else(|e| panic!("{e}"));
    let n = names_with_priv(&models);
    let lm: BTreeMap<i64, &FeatureRow> = rows.iter().map(|r| (r.landmark_ns, r)).collect();
    assert_eq!(lm.len(), 3, "{rows:?}");
    // Landmark 0 (t0 + 1.5 s): view after the first event; trackers not arrived = level_qty; exact level pending.
    let r0 = lm[&0];
    assert_eq!(r0.ts_landmark, t0 + S + S / 2);
    for (c, v) in [
        ("level_qty", 15.0),
        ("f_hbt_risk_adverse", 15.0),
        ("f_hbt_power3_n1", 15.0),
        ("level_ct", 2.0),
    ] {
        assert!(close(col(&n, r0, c), v), "{c}: {:?}", col(&n, r0, c));
    }
    for (c, v) in [("p_f_exact", 15.0), ("p_rank_exact", 2.0), ("p_level_qty_exact", 15.0)] {
        assert!(close(col(&n, r0, c), v), "{c}: {:?}", col(&n, r0, c));
    }
    // Landmark 1 s (t0 + 2.5 s): arrival at the t0 + 2 s batch with 15, then 15 -> 18 (increase).
    let r1 = lm[&S];
    for (c, v) in [
        ("level_qty", 18.0),
        ("f_hbt_risk_adverse", 15.0),
        ("f_hbt_power3_n1", 15.0),
        ("p_f_exact", 15.0),
    ] {
        assert!(close(col(&n, r1, c), v), "{c}: {:?}", col(&n, r1, c));
    }
    // Landmark 5 s (t0 + 6.5 s): after the cancel, the trade and the ask add.
    let r5 = lm[&(5 * S)];
    let p3_after_cancel = {
        let pi = 1.0 - 15.0f64 / 18.0;
        (15.0 - (1.0 - pi) * 4.0 + (3.0 - pi * 4.0).min(0.0)).min(14.0)
    };
    let p3 = {
        let f = p3_after_cancel - 6.0;
        let (front, back) = (f, 14.0 - f);
        let pi = prob::power3(1.0, front, back);
        let est = front - (1.0 - pi) * 0.0 + (back - pi * 0.0).min(0.0);
        est.min(8.0)
    };
    let (ts, tc) = tod(&inst, t0 + 6 * S + S / 2);
    let want: Vec<(&str, Option<f64>)> = vec![
        ("dist_ticks", Some(0.0)),
        ("spread_ticks", Some(1.0)),
        ("level_qty", Some(8.0)),
        ("level_ct", Some(2.0)),
        ("own_qty_lots", Some(2.0)),
        ("f_hbt_risk_adverse", Some(8.0)),
        ("f_hbt_power3_n1", Some(p3)),
        ("queue_share_ra", Some(0.8)),
        ("elapsed_ns", Some(5e9)),
        ("traded_at_px_since_entry", Some(6.0)),
        ("unexplained_dec_since_entry", Some(4.0)),
        ("imb_l1", Some((8.0 - 10.0) / 18.0)),
        ("imb_l3", Some((17.0 - 15.0) / 32.0)),
        ("wmid_offset_ticks", Some((8.0 * 101.0 + 10.0 * 100.0) / 18.0 - 100.5)),
        ("same_sz_l1", Some(8.0)),
        ("same_sz_l2", Some(7.0)),
        ("same_sz_l3", Some(2.0)),
        ("opp_sz_l1", Some(10.0)),
        ("opp_sz_l2", Some(4.0)),
        ("opp_sz_l3", Some(1.0)),
        ("ofi_1s", None),
        (
            "ofi_10s",
            Some((3.0 - 4.0 - 6.0 - 2.0) / ((13.0 + 11.0 + 8.0 + 9.0) / 4.0)),
        ),
        ("trade_vol_px_10s", Some(6.0)),
        ("trade_vol_side_10s", Some(6.0)),
        ("trade_vol_all_60s", Some(6.0)),
        ("dec_rate_px_10s", Some(0.4)),
        ("vol_ticks_60s", Some(0.0)),
        ("bbo_changes_10s", Some(5.0)),
        ("tod_sin", Some(ts)),
        ("tod_cos", Some(tc)),
        ("log_mid_over_tick", Some(100.5f64.ln())),
        ("p_f_exact", Some(5.0)),
        ("p_rank_exact", Some(1.0)),
        ("p_level_qty_exact", Some(8.0)),
    ];
    for (c, v) in want {
        let got = col(&n, r5, c);
        match v {
            None => assert_eq!(got, None, "{c}"),
            Some(v) => assert!(got.is_some_and(|g| (g - v).abs() < 1e-9), "{c}: got {got:?}, want {v}"),
        }
    }
    assert_eq!(n.len(), FEATURES.len() - 2 + PRIVILEGED.len());
    // Never filled; ends open at the end of input (t0 + 8 s): censor.
    assert!(
        rows.iter()
            .all(|r| r.cause == Cause::Censor && r.side_sign == 1 && r.instrument_id == 1)
    );
    assert_eq!(r5.event_ns, t0 + 8 * S - (t0 + 6 * S + S / 2));
}

#[test]
fn o_feat_2_scenario_one_bid_shadow_mock_trackers() {
    check_scenario_one(mock_models());
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the zoo tracker models")]
fn o_feat_2_scenario_one_real_zoo_trackers() {
    let models: Vec<Box<dyn QueueModel>> = ["hbt_risk_adverse", "hbt_power3[n=1]"]
        .iter()
        .map(|s| {
            s.parse::<ModelId>()
                .and_then(|id| qt_zoo::build(&id))
                .unwrap_or_else(|e| panic!("{e}"))
        })
        .collect();
    check_scenario_one(models);
}

#[test]
fn o_feat_2_scenario_two_ask_shadow_policy_cancel() {
    let t0 = t0();
    let sc = Scenario::new(Venue::Cme)
        .instrument(1)
        .event(t0 + S / 4, |e| {
            e.add(1, BookSide::Bid, 100, 4)
                .add(2, BookSide::Ask, 102, 6)
                .add(3, BookSide::Ask, 103, 2)
        })
        .event(t0 + S, |e| e.add(4, BookSide::Ask, 102, 3))
        .event(t0 + 2 * S, |e| e.add(5, BookSide::Ask, 101, 5))
        .event(t0 + 3 * S, |e| e.cancel(1, 4).add(6, BookSide::Ask, 100, 1))
        .event(t0 + 5 * S, |e| e.add(7, BookSide::Bid, 98, 1));
    let inst = sc.instrument_rows(1).remove(0);
    let events = sc.build();
    let models = mock_models();
    let pop = vec![PopOrder::Shadow(shadow(
        "a1",
        BookSide::Ask,
        PriceSpec::Touch { offset_ticks: 0 },
        1,
        t0 + S / 2,
    ))];
    let rows = extract_events(
        events.iter().cloned().map(Ok),
        &inst,
        rules(Venue::Cme),
        pop,
        &models,
        &cfg(&[0, S], true),
    )
    .unwrap_or_else(|e| panic!("{e}"));
    let n = names_with_priv(&models);
    assert_eq!(rows.len(), 2, "{rows:?}");
    let r = &rows[1];
    assert_eq!((r.landmark_ns, r.side_sign), (S, -1));
    let want = [
        ("dist_ticks", 0.0),
        ("spread_ticks", 2.0),
        ("level_qty", 9.0),
        ("f_hbt_risk_adverse", 6.0),
        ("imb_l1", -(4.0 - 9.0) / 13.0),
        ("wmid_offset_ticks", -((4.0 * 102.0 + 9.0 * 100.0) / 13.0 - 101.0)),
        ("ofi_1s", -(-3.0) / 6.5),
        ("same_sz_l1", 9.0),
        ("opp_sz_l1", 4.0),
        ("p_f_exact", 6.0),
    ];
    for (c, v) in want {
        let got = col(&n, r, c);
        assert!(got.is_some_and(|g| (g - v).abs() < 1e-9), "{c}: got {got:?}, want {v}");
    }
    // Policy cancel at t0 + 3 s: the best ask is 2 ticks better than 102.
    assert_eq!(
        rows.iter().map(|r| (r.cause, r.event_ns)).collect::<Vec<_>>(),
        vec![(Cause::Cancel, 3 * S - S / 2), (Cause::Cancel, 3 * S - S / 2 - S)]
    );
    // 1.1: every row carries the instrument-day's local midnight (exact exchange-local time of day).
    assert!(rows.iter().all(|r| r.midnight_utc_ns == Some(inst.midnight_utc_ns)));
    // 1.1 (review F6): cancel_away_ticks 0 disables the policy cancel; the same shadow then runs to its exact end
    // (open at the end of data: censored), and the features before the would-be cancel are unchanged.
    let never = ExtractConfig {
        cancel_away_ticks: 0,
        ..cfg(&[0, S], true)
    };
    let pop = vec![PopOrder::Shadow(shadow(
        "a1",
        BookSide::Ask,
        PriceSpec::Touch { offset_ticks: 0 },
        1,
        t0 + S / 2,
    ))];
    let rows_nc = extract_events(
        events.into_iter().map(Ok),
        &inst,
        rules(Venue::Cme),
        pop,
        &models,
        &never,
    )
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(rows_nc.len(), 2, "{rows_nc:?}");
    for (a, b) in rows.iter().zip(&rows_nc) {
        assert_eq!((a.landmark_ns, &a.values), (b.landmark_ns, &b.values));
        assert_eq!(b.cause, Cause::Censor, "{b:?}");
        assert!(b.event_ns > a.event_ns, "{a:?} {b:?}");
    }
}

#[derive(Default)]
struct Lives(Vec<RealOrderRow>);

impl Sink for Lives {
    fn real_order(&mut self, r: &RealOrderRow) -> qt_types::QtResult<()> {
        self.0.push(*r);
        Ok(())
    }
}

#[test]
fn real_order_population_excludes_own_size() {
    let (events, inst) = scenario_one();
    let mut ec = EngineConfig::new(rules(Venue::Cme));
    ec.real_orders = true;
    let mut engine =
        Engine::new(ec, std::slice::from_ref(&inst), Vec::new(), Vec::new()).unwrap_or_else(|e| panic!("{e}"));
    let mut lives = Lives::default();
    for e in &events {
        engine.apply(e, &mut lives).unwrap_or_else(|e| panic!("{e}"));
    }
    engine.finish(&mut lives).unwrap_or_else(|e| panic!("{e}"));
    let life = lives
        .0
        .iter()
        .find(|r| r.order_id == 8)
        .copied()
        .unwrap_or_else(|| panic!("no life for order 8"));
    let models = mock_models();
    let rows = extract_events(
        events.into_iter().map(Ok),
        &inst,
        rules(Venue::Cme),
        vec![PopOrder::Real(life)],
        &models,
        &cfg(&[0], true),
    )
    .unwrap_or_else(|e| panic!("{e}"));
    let n = names_with_priv(&models);
    assert_eq!(rows.len(), 1);
    let r = &rows[0];
    assert_eq!(r.order_ref, "1:8");
    for (c, v) in [
        ("level_qty", 15.0),
        ("f_hbt_risk_adverse", 15.0),
        ("p_f_exact", 15.0),
        ("p_rank_exact", 2.0),
        ("p_level_qty_exact", 15.0),
    ] {
        assert!(close(col(&n, r, c), v), "{c}: {:?}", col(&n, r, c));
    }
    assert_eq!(r.cause, Cause::Censor);
}

fn market() -> (Vec<MboEvent>, Instrument, Vec<ShadowSpec>) {
    let mut sc = SynthConfig::for_venue(Venue::Cme, 11);
    sc.instruments = 1;
    sc.events_per_instrument = 3_000;
    let out = generate(&sc);
    let inst = out.instruments[0].clone();
    let ts0 = out.events.first().map_or(0, |e| e.ts_event);
    let shadows = (0..40i64)
        .map(|i| {
            let side = if i % 2 == 0 { BookSide::Bid } else { BookSide::Ask };
            shadow(
                &format!("s{i}"),
                side,
                PriceSpec::Touch {
                    offset_ticks: -(((i / 2) % 3) as i32),
                },
                1 + (i % 2) as u64,
                ts0 + i * 3 * S,
            )
        })
        .collect();
    (out.events, inst, shadows)
}

fn run(events: &[MboEvent], inst: &Instrument, shadows: &[ShadowSpec], landmarks: &[i64]) -> Vec<FeatureRow> {
    let pop = shadows.iter().cloned().map(PopOrder::Shadow).collect();
    extract_events(
        events.iter().copied().map(Ok),
        inst,
        rules(Venue::Cme),
        pop,
        &mock_models(),
        &cfg(landmarks, true),
    )
    .unwrap_or_else(|e| panic!("{e}"))
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 12, ..ProptestConfig::default() })]

    /// O-FEAT-1: features at `t_l` are identical when every record with `ts_event > t_l` is deleted.
    #[test]
    fn o_feat_1_no_lookahead(cut in 0.05f64..0.95, l1 in 1i64..20, l2 in 20i64..90) {
        let (events, inst, shadows) = market();
        let landmarks = [0, l1 * S / 4, l2 * S / 4];
        let full = run(&events, &inst, &shadows, &landmarks);
        let (lo, hi) = (events[0].ts_event, events[events.len() - 1].ts_event);
        let t_cut = lo + ((hi - lo) as f64 * cut) as i64;
        let truncated: Vec<MboEvent> = events.iter().copied().filter(|e| e.ts_event <= t_cut).collect();
        let part = run(&truncated, &inst, &shadows, &landmarks);
        let full_by: BTreeMap<(String, i64), &FeatureRow> = full.iter().map(|r| ((r.order_ref.clone(), r.landmark_ns), r)).collect();
        let mut compared = 0;
        for r in part.iter().filter(|r| r.ts_landmark <= t_cut) {
            if let Some(f) = full_by.get(&(r.order_ref.clone(), r.landmark_ns)) {
                prop_assert_eq!(&f.values, &r.values, "{} @ {}", r.order_ref, r.landmark_ns);
                compared += 1;
            }
        }
        prop_assert!(compared > 0 || part.is_empty());
    }
}

#[test]
fn o_feat_1_rows_exist_and_compare() {
    // Deterministic companion of the property test: a cut in the middle compares many rows.
    let (events, inst, shadows) = market();
    let landmarks = [0, S, 5 * S];
    let full = run(&events, &inst, &shadows, &landmarks);
    assert!(full.len() > 40, "{}", full.len());
    let mid = events[events.len() / 2].ts_event;
    let truncated: Vec<MboEvent> = events.iter().copied().filter(|e| e.ts_event <= mid).collect();
    let part = run(&truncated, &inst, &shadows, &landmarks);
    let full_by: BTreeMap<(String, i64), &FeatureRow> =
        full.iter().map(|r| ((r.order_ref.clone(), r.landmark_ns), r)).collect();
    let mut compared = 0;
    for r in &part {
        if let Some(f) = full_by.get(&(r.order_ref.clone(), r.landmark_ns)) {
            assert_eq!(f.values, r.values, "{} @ {}", r.order_ref, r.landmark_ns);
            compared += 1;
        }
    }
    assert!(compared >= 10, "compared {compared}");
    // Determinism.
    assert_eq!(run(&events, &inst, &shadows, &landmarks), full);
}
