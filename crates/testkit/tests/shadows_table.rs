//! SPEC 6.2 S1-S16 reproduced by `reference_shadows` (hand-built scenarios, venue cme unless stated, tick 1,
//! instrument midnight 0, close 86,400 s).

#![allow(clippy::type_complexity)]

use qt_schema::rows::{
    EndState, FillReason, FirstFillReason, PriceSpec, ShadowAmend, ShadowFillRow, ShadowResultRow, ShadowSource,
    ShadowSpec, TrajectoryKind, TrajectoryRow,
};
use qt_testkit::refbook::DqTracker;
use qt_testkit::scenario::{EventBuilder, Scenario};
use qt_testkit::{RefConfig, ShadowTruth, TrajectoryDetail, reference_shadows, try_reference_shadows};
use qt_types::{BookSide, LatencyModel, MboEvent, Side, Venue, rules};

const B: BookSide = BookSide::Bid;
const A: BookSide = BookSide::Ask;

fn x(qty: u64) -> ShadowSpec {
    ShadowSpec {
        shadow_id: "X".into(),
        instrument_id: 1,
        side: B,
        price: PriceSpec::Abs(100),
        qty,
        ts_submit: 15,
        latency_ns: 0,
        ts_cancel: None,
        group: None,
        source: ShadowSource::User,
        order_key: None,
    }
}

fn e0(e: &mut EventBuilder) -> &mut EventBuilder {
    e.add(1, B, 100, 60).add(2, B, 100, 40).add(9, A, 101, 500)
}

/// E0 + E1 (`A o3 B 100 x<o3>`).
fn base(venue: Venue, o3: u64) -> Scenario {
    Scenario::new(venue).event(10, e0).event(20, |e| e.add(3, B, 100, o3))
}

/// S1 events (o3 size as given).
fn s1_events(o3: u64) -> Scenario {
    base(Venue::Cme, o3)
        .event(30, |e| e.trade(Side::Ask, 100, 60).fill(1, 60))
        .event(40, |e| e.trade(Side::Ask, 100, 50).fill(2, 40).fill(3, 10))
}

fn s2_events() -> Vec<MboEvent> {
    s1_events(50)
        .event(50, |e| e.trade(Side::Ask, 100, 25).fill(3, 25))
        .build()
}

fn run(events: &[MboEvent], venue: Venue, shadows: &[ShadowSpec], amends: &[ShadowAmend]) -> Vec<ShadowTruth> {
    reference_shadows(events, rules(venue), LatencyModel::default(), shadows, amends)
}

fn one(events: &[MboEvent], venue: Venue, s: ShadowSpec) -> ShadowTruth {
    let mut v = run(events, venue, &[s], &[]);
    assert_eq!(v.len(), 1);
    v.remove(0)
}

fn full(events: &[MboEvent], s: ShadowSpec) -> ShadowTruth {
    let mut cfg = RefConfig::new(rules(Venue::Cme));
    cfg.trajectory = TrajectoryDetail::Full;
    let mut v = try_reference_shadows(events, &cfg, &[s], &[]).expect("valid input");
    v.remove(0)
}

#[allow(clippy::too_many_arguments)]
fn fill_row(
    seq: u32,
    ts: i64,
    sequence: u64,
    qty: u64,
    cum: u64,
    leaves: u64,
    reason: FillReason,
    qa: u64,
    traded: u64,
    through: u64,
    behind: u64,
) -> ShadowFillRow {
    ShadowFillRow {
        shadow_id: "X".into(),
        fill_seq: seq,
        ts_event: ts,
        sequence,
        fill_qty: qty,
        cum_qty: cum,
        leaves_qty: leaves,
        fill_px: 100,
        reason,
        queue_ahead_before: qa,
        traded_at_px_event: traded,
        trade_through_event: through,
        behind_volume_executed: behind,
    }
}

#[allow(clippy::too_many_arguments)]
fn traj(
    kind: TrajectoryKind,
    ts: i64,
    sequence: u64,
    qa: (u64, u64),
    rk: (u32, u32),
    lvl: (u64, u64),
    deltas: [u64; 5],
    filled: u64,
) -> TrajectoryRow {
    TrajectoryRow {
        shadow_id: "X".into(),
        ts_event: ts,
        sequence,
        kind,
        queue_ahead_before: qa.0,
        queue_ahead_after: qa.1,
        rank_before: rk.0,
        rank_after: rk.1,
        level_qty_before: lvl.0,
        level_qty_after: lvl.1,
        cancel_ahead_qty: deltas[0],
        cancel_behind_qty: deltas[1],
        add_behind_qty: deltas[2],
        traded_at_px_qty: deltas[3],
        trade_through_qty: deltas[4],
        filled_qty: filled,
    }
}

fn result(
    r: &ShadowResultRow,
) -> (
    Option<i64>,
    Option<u64>,
    Option<u32>,
    Option<u64>,
    u64,
    u32,
    FirstFillReason,
    EndState,
) {
    (
        r.ts_activated_event,
        r.queue_ahead_at_live,
        r.rank_at_live,
        r.level_qty_at_live,
        r.filled_qty,
        r.n_fills,
        r.first_fill_reason,
        r.end_state,
    )
}

#[test]
fn o_sh_ref_s1_queue_depleted_with_behind_volume() {
    let ev = s1_events(30).build();
    let (r, f, t) = one(&ev, Venue::Cme, x(10));
    assert_eq!(
        result(&r),
        (
            Some(20),
            Some(100),
            Some(2),
            Some(100),
            10,
            1,
            FirstFillReason::QueueDepleted,
            EndState::Filled
        )
    );
    assert_eq!(
        f,
        vec![fill_row(1, 40, 12, 10, 10, 0, FillReason::QueueDepleted, 40, 50, 0, 10)]
    );
    assert_eq!(r.queue_ahead_at_first_fill, Some(40));
    assert_eq!(
        (r.ts_first_fill, r.ts_last_fill, r.ts_full_fill),
        (Some(40), Some(40), Some(40))
    );
    assert_eq!(
        (r.ts_live, r.latency_applied_ns, r.price, r.qty),
        (15, 0, Some(100), 10)
    );
    assert!(!r.ambiguous_race && !r.out_of_session && r.dq_flags == 0 && r.ts_cancel_effective.is_none());
    assert_eq!(
        t,
        vec![
            traj(TrajectoryKind::Live, 15, 4, (100, 100), (2, 2), (100, 100), [0; 5], 0),
            traj(
                TrajectoryKind::Event,
                30,
                7,
                (100, 40),
                (2, 1),
                (130, 70),
                [0, 0, 0, 60, 0],
                0
            ),
            traj(
                TrajectoryKind::Event,
                40,
                12,
                (40, 0),
                (1, 0),
                (70, 20),
                [0, 0, 0, 50, 0],
                10
            ),
            traj(TrajectoryKind::End, 40, 12, (0, 0), (0, 0), (20, 20), [0; 5], 10),
        ]
    );
}

#[test]
fn o_sh_ref_s2_two_partial_fills() {
    let (r, f, _) = one(&s2_events(), Venue::Cme, x(30));
    assert_eq!(
        f,
        vec![
            fill_row(1, 40, 12, 10, 10, 20, FillReason::QueueDepleted, 40, 50, 0, 10),
            fill_row(2, 50, 15, 20, 30, 0, FillReason::QueueDepleted, 0, 25, 0, 25),
        ]
    );
    assert_eq!(
        (r.ts_first_fill, r.ts_full_fill, r.n_fills, r.end_state),
        (Some(40), Some(50), 2, EndState::Filled)
    );
}

#[test]
fn o_sh_ref_s3_cancel_ahead_then_partial() {
    let ev = base(Venue::Cme, 30)
        .event(30, |e| e.cancel(1, 60))
        .event(40, |e| e.trade(Side::Ask, 100, 45).fill(2, 40).fill(3, 5))
        .build();
    let (r, f, t) = one(&ev, Venue::Cme, x(10));
    assert_eq!(
        t,
        vec![
            traj(TrajectoryKind::Live, 15, 4, (100, 100), (2, 2), (100, 100), [0; 5], 0),
            traj(
                TrajectoryKind::Event,
                30,
                5,
                (100, 40),
                (2, 1),
                (130, 70),
                [60, 0, 0, 0, 0],
                0
            ),
            traj(
                TrajectoryKind::Event,
                40,
                10,
                (40, 0),
                (1, 0),
                (70, 25),
                [0, 0, 0, 45, 0],
                5
            ),
            traj(TrajectoryKind::End, 40, 10, (0, 0), (0, 0), (25, 25), [0; 5], 5),
        ]
    );
    assert_eq!(
        f,
        vec![fill_row(1, 40, 10, 5, 5, 5, FillReason::QueueDepleted, 40, 45, 0, 5)]
    );
    assert_eq!((r.end_state, r.filled_qty), (EndState::OpenAtEnd, 5));
}

#[test]
fn o_sh_ref_s4_cancel_behind_full_mode_only() {
    let ev = base(Venue::Cme, 30).event(30, |e| e.cancel(3, 30)).build();
    let (_, _, t) = one(&ev, Venue::Cme, x(10));
    assert_eq!(t.iter().filter(|r| r.kind == TrajectoryKind::Event).count(), 0);
    let (_, _, t) = full(&ev, x(10));
    assert_eq!(
        t,
        vec![
            traj(TrajectoryKind::Live, 15, 4, (100, 100), (2, 2), (100, 100), [0; 5], 0),
            traj(
                TrajectoryKind::Event,
                20,
                4,
                (100, 100),
                (2, 2),
                (100, 130),
                [0, 0, 30, 0, 0],
                0
            ),
            traj(
                TrajectoryKind::Event,
                30,
                5,
                (100, 100),
                (2, 2),
                (130, 100),
                [0, 30, 0, 0, 0],
                0
            ),
            traj(TrajectoryKind::End, 30, 5, (100, 100), (2, 2), (100, 100), [0; 5], 0),
        ]
    );
}

#[test]
fn o_sh_ref_s5_partial_cancel_ahead() {
    let ev = base(Venue::Cme, 30).event(30, |e| e.cancel(1, 20)).build();
    let (_, _, t) = one(&ev, Venue::Cme, x(10));
    assert_eq!(
        t[1],
        traj(
            TrajectoryKind::Event,
            30,
            5,
            (100, 80),
            (2, 2),
            (130, 110),
            [20, 0, 0, 0, 0],
            0
        )
    );
}

#[test]
fn o_sh_ref_s6_size_increase_loses_priority() {
    let ev = base(Venue::Cme, 30)
        .event(30, |e| e.modify(1, 100, 70))
        .event(40, |e| e.trade(Side::Ask, 100, 45).fill(2, 40).fill(3, 5))
        .build();
    let (r, f, t) = one(&ev, Venue::Cme, x(10));
    assert_eq!(
        t[1],
        traj(
            TrajectoryKind::Event,
            30,
            5,
            (100, 40),
            (2, 1),
            (130, 140),
            [60, 0, 70, 0, 0],
            0
        )
    );
    assert_eq!(f.len(), 1);
    assert_eq!((f[0].ts_event, f[0].fill_qty, f[0].queue_ahead_before), (40, 5, 40));
    assert_eq!(r.filled_qty, 5);
}

#[test]
fn o_sh_ref_s7_trade_through() {
    let ev = Scenario::new(Venue::Cme)
        .event(10, |e| e0(e).add(4, B, 99, 50))
        .event(20, |e| e.add(5, A, 102, 10))
        .event(30, |e| {
            e.trade(Side::Ask, 100, 100)
                .fill(1, 60)
                .fill(2, 40)
                .trade(Side::Ask, 99, 20)
                .fill(4, 20)
        })
        .build();
    let (r, f, _) = one(&ev, Venue::Cme, x(10));
    assert_eq!(
        f,
        vec![fill_row(
            1,
            30,
            13,
            10,
            10,
            0,
            FillReason::TradeThrough,
            100,
            100,
            20,
            0
        )]
    );
    assert_eq!(
        (r.first_fill_reason, r.end_state),
        (FirstFillReason::TradeThrough, EndState::Filled)
    );
}

#[test]
fn o_sh_ref_s8_crossed_after_event_maker() {
    let ev = base(Venue::Cme, 30).event(30, |e| e.add(6, A, 100, 5)).build();
    // Audit ruling A5 with E36(a): the 5 lots offered at the shadow's price go to o1 and o2 first (100 ahead of
    // the shadow), so the shadow gets max(0, 5 - 100) = 0 and keeps resting.
    let (r, f, _) = one(&ev, Venue::Cme, x(10));
    assert!(f.is_empty());
    assert_eq!((r.filled_qty, r.end_state), (0, EndState::OpenAtEnd));
    // Once the orders ahead leave, the shadow takes the offer; the same offered size is not taken twice.
    let ev2 = base(Venue::Cme, 30)
        .event(30, |e| e.add(6, A, 100, 5))
        .event(40, |e| e.cancel(1, 60).cancel(2, 40))
        .event(50, |e| e.add(7, B, 99, 1))
        .build();
    let (r, f, _) = one(&ev2, Venue::Cme, x(10));
    assert_eq!(f, vec![fill_row(1, 40, 7, 5, 5, 5, FillReason::Crossed, 100, 0, 0, 0)]);
    assert_eq!(r.filled_qty, 5);
    let mut dq = DqTracker::new(rules(Venue::Cme));
    for e in &ev {
        dq.push(e);
    }
    assert_eq!(dq.finish().0.crossed_books_after_last, 1);
}

#[test]
fn o_sh_ref_s9_crossed_at_live() {
    let ev = s1_events(30).build();
    let mut s = x(10);
    s.price = PriceSpec::Abs(101);
    let (r, f, t) = one(&ev, Venue::Cme, s);
    assert_eq!(
        (r.queue_ahead_at_live, r.ts_activated_event, r.end_state),
        (Some(0), Some(20), EndState::Filled)
    );
    assert_eq!(
        (f.len(), f[0].ts_event, f[0].fill_qty, f[0].reason),
        (1, 15, 10, FillReason::CrossedAtLive)
    );
    assert_eq!(r.first_fill_reason, FirstFillReason::CrossedAtLive);
    assert_eq!(
        t.iter().map(|r| r.kind).collect::<Vec<_>>(),
        vec![TrajectoryKind::Live, TrajectoryKind::End]
    );
}

#[test]
fn o_sh_ref_s10_latency_strict_activation() {
    let ev = s1_events(30).build();
    let mut s = x(10);
    s.latency_ns = 5;
    let (r, f, _) = one(&ev, Venue::Cme, s);
    assert_eq!((r.ts_live, r.ts_activated_event), (20, Some(30)));
    assert_eq!((r.queue_ahead_at_live, r.rank_at_live), (Some(130), Some(3)));
    assert!(f.is_empty());
    assert_eq!(r.latency_applied_ns, 5);
}

#[test]
fn o_sh_ref_s11_cancel_effective_time() {
    let ev = s1_events(30).build();
    let mut a = x(10);
    a.ts_cancel = Some(35);
    let (r, f, t) = one(&ev, Venue::Cme, a);
    assert_eq!(
        (r.filled_qty, r.end_state, r.ts_cancel_effective),
        (0, EndState::Cancelled, Some(35))
    );
    assert!(f.is_empty());
    assert_eq!(t.last().map(|r| (r.kind, r.ts_event)), Some((TrajectoryKind::End, 35)));
    let mut b = x(10);
    b.ts_cancel = Some(40);
    let (r, f, _) = one(&ev, Venue::Cme, b);
    assert_eq!(
        (r.filled_qty, r.end_state, f.len(), f[0].ts_event),
        (10, EndState::Filled, 1, 40)
    );
}

#[test]
fn o_sh_ref_s12_hidden_print_nasdaq() {
    let ev = base(Venue::Nasdaq, 30)
        .event(30, |e| e.trade(Side::None, 100, 500))
        .build();
    let (r, f, _) = one(&ev, Venue::Nasdaq, x(10));
    assert!(f.is_empty());
    assert_eq!((r.filled_qty, r.end_state), (0, EndState::OpenAtEnd));
    let mut dq = DqTracker::new(rules(Venue::Nasdaq));
    for e in &ev {
        dq.push(e);
    }
    assert_eq!(dq.finish().0.hidden_prints, 1);
}

#[test]
fn o_sh_ref_s13_amend_keeps_priority() {
    let amend = ShadowAmend {
        shadow_id: "X".into(),
        ts_send: 25,
        new_qty: 10,
    };
    let v = run(&s2_events(), Venue::Cme, &[x(30)], &[amend]);
    let (r, f, _) = &v[0];
    assert_eq!((r.qty, r.filled_qty, r.end_state), (10, 10, EndState::Filled));
    assert_eq!(f.len(), 1);
    assert_eq!((f[0].ts_event, f[0].fill_qty, f[0].leaves_qty), (40, 10, 0));
}

#[test]
fn o_sh_ref_s14_clear_censors() {
    let ev = base(Venue::Cme, 30).clear(30).build();
    let (r, f, t) = one(&ev, Venue::Cme, x(10));
    assert!(f.is_empty());
    assert_eq!(r.end_state, EndState::CensoredClear);
    assert_eq!(
        t.last().map(|r| (r.kind, r.ts_event, r.sequence)),
        Some((TrajectoryKind::End, 30, 5))
    );
}

#[test]
fn o_sh_ref_s15_duplicate_shadow_id() {
    let ev = s1_events(30).build();
    let cfg = RefConfig::new(rules(Venue::Cme));
    let err = try_reference_shadows(&ev, &cfg, &[x(10), x(5)], &[]).expect_err("duplicate id");
    assert_eq!(err.code(), "QT-INPUT-003");
    assert!(run(&ev, Venue::Cme, &[x(10), x(5)], &[]).is_empty());
}

#[test]
fn o_sh_ref_s16_touch_pricing_and_rejections() {
    let ev = base(Venue::Cme, 30).build();
    let touch = |id: &str, off: i32| ShadowSpec {
        shadow_id: id.into(),
        price: PriceSpec::Touch { offset_ticks: off },
        ..x(10)
    };
    let v = run(
        &ev,
        Venue::Cme,
        &[touch("t0", 0), touch("tm1", -1), touch("tp1", 1)],
        &[],
    );
    assert_eq!(
        (v[0].0.price, v[0].0.end_state, v[0].0.queue_ahead_at_live),
        (Some(100), EndState::OpenAtEnd, Some(100))
    );
    assert_eq!(
        (v[1].0.price, v[1].0.end_state, v[1].0.queue_ahead_at_live),
        (Some(99), EndState::OpenAtEnd, Some(0))
    );
    assert_eq!(
        (v[2].0.price, v[2].0.end_state),
        (Some(101), EndState::RejectedWouldCross)
    );
    assert!(v[2].1.is_empty());
    assert_eq!(
        v[2].2.iter().map(|r| r.kind).collect::<Vec<_>>(),
        vec![TrajectoryKind::End]
    );
    let no_bids = Scenario::new(Venue::Cme)
        .event(10, |e| e.add(9, A, 101, 500))
        .event(20, |e| e.add(3, B, 100, 30))
        .build();
    let v = run(&no_bids, Venue::Cme, &[touch("t0", 0)], &[]);
    let (r, f, t) = &v[0];
    assert_eq!((r.price, r.end_state), (None, EndState::RejectedNoTouch));
    assert!(f.is_empty());
    assert_eq!(t.iter().map(|r| r.kind).collect::<Vec<_>>(), vec![TrajectoryKind::End]);
}
