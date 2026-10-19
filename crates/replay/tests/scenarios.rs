//! O-SH-1: the hand-built shadow scenarios S1-S16 of SPEC 6.2 (venue cme unless stated, tick 1, midnight 0, close
//! 86,400 s), checked on `qt.shadow_results`, `qt.shadow_fills` and `qt.queue_trajectory` columns.

mod common;

use common::{Scn, abs_shadow, cfg, instrument, run};
use qt_replay::sinks::VecSink;
use qt_replay::{EngineConfig, EngineReport, TrajectoryMode};
use qt_schema::rows::{
    EndState, FillReason, FirstFillReason, PriceSpec, ShadowAmend, ShadowFillRow, ShadowResultRow, ShadowSpec,
    TrajectoryKind, TrajectoryRow,
};
use qt_types::{BookSide, QtError, Venue};

const B: u8 = b'B';
const A: u8 = b'A';

/// E0 (ts 10): `A o1 B 100 x60`, `A o2 B 100 x40`, `A o9 A 101 x500` (+ `A o4 B 99 x50` with `o4`).
fn base(o4: bool) -> Scn {
    Scn::new().ev(10, |e| {
        e.add(1, B, 100, 60).add(2, B, 100, 40);
        if o4 {
            e.add(4, B, 99, 50);
        }
        e.add(9, A, 101, 500);
    })
}

fn e1(s: Scn) -> Scn {
    s.ev(20, |e| {
        e.add(3, B, 100, 30);
    })
}

/// S1 events: E1 `A o3`, E2 `T A 100 60; F o1 60; C o1 60`, E3 `T A 100 50; F o2 40; C o2 40; F o3 10; C o3 10`.
fn s1_events() -> Scn {
    e1(base(false))
        .ev(30, |e| {
            e.trade(A, 100, 60).exec(1, 60);
        })
        .ev(40, |e| {
            e.trade(A, 100, 50).exec(2, 40).exec(3, 10);
        })
}

fn x(qty: u64) -> ShadowSpec {
    abs_shadow("X", BookSide::Bid, 100, qty, 15, 0)
}

fn go(venue: Venue, s: Scn, shadows: Vec<ShadowSpec>, amends: Vec<ShadowAmend>) -> (VecSink, EngineReport) {
    go_cfg(cfg(venue), venue, s, shadows, amends)
}

fn go_cfg(
    c: EngineConfig,
    venue: Venue,
    s: Scn,
    shadows: Vec<ShadowSpec>,
    amends: Vec<ShadowAmend>,
) -> (VecSink, EngineReport) {
    run(c, &[instrument(venue)], &s.recs, shadows, amends)
}

fn result(sink: &VecSink) -> &ShadowResultRow {
    assert_eq!(sink.shadow_results.len(), 1);
    &sink.shadow_results[0]
}

#[allow(clippy::too_many_arguments)]
fn fill(
    seq_no: u32,
    ts: i64,
    sequence: u64,
    q: u64,
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
        fill_seq: seq_no,
        ts_event: ts,
        sequence,
        fill_qty: q,
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
    lv: (u64, u64),
    d: [u64; 5],
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
        level_qty_before: lv.0,
        level_qty_after: lv.1,
        cancel_ahead_qty: d[0],
        cancel_behind_qty: d[1],
        add_behind_qty: d[2],
        traded_at_px_qty: d[3],
        trade_through_qty: d[4],
        filled_qty: filled,
    }
}

#[test]
fn o_sh_1_s1_queue_depleted_single_fill() {
    let (sink, rep) = go(Venue::Cme, s1_events(), vec![x(10)], vec![]);
    let r = result(&sink);
    let want = ShadowResultRow {
        shadow_id: "X".into(),
        instrument_id: 1,
        side: BookSide::Bid,
        price: Some(100),
        qty: 10,
        ts_submit: 15,
        ts_live: 15,
        ts_activated_event: Some(20),
        ts_cancel_effective: None,
        queue_ahead_at_live: Some(100),
        rank_at_live: Some(2),
        level_qty_at_live: Some(100),
        ts_first_fill: Some(40),
        ts_last_fill: Some(40),
        ts_full_fill: Some(40),
        filled_qty: 10,
        n_fills: 1,
        queue_ahead_at_first_fill: Some(40),
        first_fill_reason: FirstFillReason::QueueDepleted,
        end_state: EndState::Filled,
        latency_applied_ns: 0,
        ambiguous_race: false,
        out_of_session: false,
        // CME stream without a leading snapshot: verdict `warnings` (files_without_snapshot).
        dq_flags: 1,
    };
    assert_eq!(*r, want);
    assert_eq!(
        sink.shadow_fills,
        vec![fill(1, 40, 12, 10, 10, 0, FillReason::QueueDepleted, 40, 50, 0, 10)]
    );
    assert_eq!(
        sink.trajectory,
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
    let c = rep.shadows.expect("shadow counts");
    assert_eq!(
        (c.n, c.n_live, c.n_filled, c.fills_rows, c.trajectory_rows),
        (1, 1, 1, 1, 4)
    );
    assert_eq!(rep.invariants, qt_book::Invariants::default());
}

#[test]
fn o_sh_1_s2_partial_then_full() {
    let s = e1(base(false))
        .ev(30, |e| {
            e.trade(A, 100, 60).exec(1, 60);
        })
        .ev(40, |e| {
            e.trade(A, 100, 50).exec(2, 40).exec(3, 10);
        })
        .ev(50, |e| {
            e.trade(A, 100, 25).exec(3, 25);
        });
    // o3 is x50 in S2.
    let mut s = s;
    s.recs[3].size = 50;
    let (sink, _) = go(Venue::Cme, s, vec![x(30)], vec![]);
    assert_eq!(
        sink.shadow_fills,
        vec![
            fill(1, 40, 12, 10, 10, 20, FillReason::QueueDepleted, 40, 50, 0, 10),
            fill(2, 50, 15, 20, 30, 0, FillReason::QueueDepleted, 0, 25, 0, 25),
        ]
    );
    let r = result(&sink);
    assert_eq!(
        (r.ts_first_fill, r.ts_full_fill, r.n_fills, r.filled_qty, r.end_state),
        (Some(40), Some(50), 2, 30, EndState::Filled)
    );
}

#[test]
fn o_sh_1_s3_cancel_ahead_then_partial() {
    let s = e1(base(false))
        .ev(30, |e| {
            e.cancel(1, 60);
        })
        .ev(40, |e| {
            e.trade(A, 100, 45).exec(2, 40).exec(3, 5);
        });
    let (sink, _) = go(Venue::Cme, s, vec![x(10)], vec![]);
    assert_eq!(
        sink.trajectory,
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
        sink.shadow_fills,
        vec![fill(1, 40, 10, 5, 5, 5, FillReason::QueueDepleted, 40, 45, 0, 5)]
    );
    let r = result(&sink);
    assert_eq!((r.end_state, r.filled_qty), (EndState::OpenAtEnd, 5));
}

#[test]
fn o_sh_1_s4_cancel_behind_only_in_full_mode() {
    let mk = || {
        e1(base(false)).ev(30, |e| {
            e.cancel(3, 30);
        })
    };
    let (ahead, _) = go(Venue::Cme, mk(), vec![x(10)], vec![]);
    assert!(
        ahead.trajectory.iter().all(|t| t.kind != TrajectoryKind::Event),
        "{:?}",
        ahead.trajectory
    );
    let mut c = cfg(Venue::Cme);
    c.trajectory = TrajectoryMode::Full;
    let (full, _) = go_cfg(c, Venue::Cme, mk(), vec![x(10)], vec![]);
    let rows: Vec<&TrajectoryRow> = full
        .trajectory
        .iter()
        .filter(|t| t.kind == TrajectoryKind::Event)
        .collect();
    assert_eq!(
        rows,
        vec![
            &traj(
                TrajectoryKind::Event,
                20,
                4,
                (100, 100),
                (2, 2),
                (100, 130),
                [0, 0, 30, 0, 0],
                0
            ),
            &traj(
                TrajectoryKind::Event,
                30,
                5,
                (100, 100),
                (2, 2),
                (130, 100),
                [0, 30, 0, 0, 0],
                0
            ),
        ]
    );
}

#[test]
fn o_sh_1_s5_partial_cancel_ahead() {
    let s = e1(base(false)).ev(30, |e| {
        e.cancel(1, 20);
    });
    let (sink, _) = go(Venue::Cme, s, vec![x(10)], vec![]);
    let row = sink
        .trajectory
        .iter()
        .find(|t| t.ts_event == 30 && t.kind == TrajectoryKind::Event);
    assert_eq!(
        row,
        Some(&traj(
            TrajectoryKind::Event,
            30,
            5,
            (100, 80),
            (2, 2),
            (130, 110),
            [20, 0, 0, 0, 0],
            0
        ))
    );
}

#[test]
fn o_sh_1_s6_size_increase_loses_priority() {
    let s = e1(base(false))
        .ev(30, |e| {
            e.modify(1, B, 100, 70);
        })
        .ev(40, |e| {
            e.trade(A, 100, 45).exec(2, 40).exec(3, 5);
        });
    let (sink, _) = go(Venue::Cme, s, vec![x(10)], vec![]);
    let row = sink
        .trajectory
        .iter()
        .find(|t| t.ts_event == 30 && t.kind == TrajectoryKind::Event);
    assert_eq!(
        row,
        Some(&traj(
            TrajectoryKind::Event,
            30,
            5,
            (100, 40),
            (2, 1),
            (130, 140),
            [60, 0, 70, 0, 0],
            0
        ))
    );
    assert_eq!(sink.shadow_fills.len(), 1);
    assert_eq!((sink.shadow_fills[0].ts_event, sink.shadow_fills[0].fill_qty), (40, 5));
}

#[test]
fn o_sh_1_s7_trade_through_fills_after_full_queue() {
    let s = base(true)
        .ev(20, |e| {
            e.add(5, A, 102, 10);
        })
        .ev(30, |e| {
            e.trade(A, 100, 100)
                .exec(1, 60)
                .exec(2, 40)
                .trade(A, 99, 20)
                .exec(4, 20);
        });
    let (sink, _) = go(Venue::Cme, s, vec![x(10)], vec![]);
    assert_eq!(
        sink.shadow_fills,
        vec![fill(1, 30, 13, 10, 10, 0, FillReason::TradeThrough, 100, 100, 20, 0)]
    );
    assert_eq!(result(&sink).first_fill_reason, FirstFillReason::TradeThrough);
}

#[test]
fn o_sh_1_s8_crossed_by_opposite_add() {
    let mk = || {
        e1(base(false)).ev(30, |e| {
            e.add(6, A, 100, 5);
        })
    };
    // Audit ruling A5 with E36(a) (default `Exact` rule): the crossing 5-lot ask goes to o1/o2, which are ahead
    // of the shadow at its price (fill max(0, 5 - 100) = 0, audit correction c); the crossed-unfilled counter counts.
    let (sink, rep) = go(Venue::Cme, mk(), vec![x(10)], vec![]);
    assert!(sink.shadow_fills.is_empty());
    assert_eq!(rep.invariants.crossed_books_after_last, 1);
    assert_eq!((rep.diagnostics.taker_fills, rep.diagnostics.crossed_unfilled), (0, 1));
    assert_eq!(result(&sink).end_state, EndState::OpenAtEnd);
    // SPEC 6.2 S8 as written, under the et rule: fill 10 `crossed` at 30.
    let mut eng = qt_replay::Engine::new(cfg(Venue::Cme), &[instrument(Venue::Cme)], vec![x(10)], vec![]).unwrap();
    eng.set_fill_rule(qt_replay::FillRule::Et);
    let mut et = VecSink::default();
    for r in &mk().recs {
        eng.apply(r, &mut et).unwrap();
    }
    eng.finish(&mut et).unwrap();
    assert_eq!(
        et.shadow_fills,
        vec![fill(1, 30, 5, 10, 10, 0, FillReason::Crossed, 100, 0, 0, 0)]
    );
}

#[test]
fn o_sh_1_s9_crossed_at_live() {
    let mut s9 = x(10);
    s9.price = PriceSpec::Abs(101);
    let (sink, _) = go(Venue::Cme, e1(base(false)), vec![s9], vec![]);
    let r = result(&sink);
    assert_eq!(
        (
            r.ts_first_fill,
            r.filled_qty,
            r.queue_ahead_at_live,
            r.first_fill_reason,
            r.end_state
        ),
        (Some(15), 10, Some(0), FirstFillReason::CrossedAtLive, EndState::Filled)
    );
    assert_eq!(sink.shadow_fills.len(), 1);
    let f = &sink.shadow_fills[0];
    assert_eq!(
        (f.ts_event, f.fill_px, f.reason, f.queue_ahead_before),
        (15, 101, FillReason::CrossedAtLive, 0)
    );
    assert_eq!(qt_replay::fill_liquidity(f.reason), "taker");
    let kinds: Vec<TrajectoryKind> = sink.trajectory.iter().map(|t| t.kind).collect();
    assert_eq!(kinds, vec![TrajectoryKind::Live, TrajectoryKind::End]);
}

#[test]
fn o_sh_1_s10_latency_strict_activation() {
    let (sink, _) = go(
        Venue::Cme,
        s1_events(),
        vec![abs_shadow("X", BookSide::Bid, 100, 10, 15, 5)],
        vec![],
    );
    let r = result(&sink);
    assert_eq!(
        (
            r.ts_live,
            r.ts_activated_event,
            r.queue_ahead_at_live,
            r.rank_at_live,
            r.filled_qty
        ),
        (20, Some(30), Some(130), Some(3), 0)
    );
    assert!(sink.shadow_fills.is_empty());
    assert_eq!(r.latency_applied_ns, 5);
}

#[test]
fn o_sh_1_s11_cancel_effective_time() {
    let mut a = x(10);
    a.ts_cancel = Some(35);
    let (sink, _) = go(Venue::Cme, s1_events(), vec![a], vec![]);
    let r = result(&sink);
    assert_eq!(
        (r.filled_qty, r.end_state, r.ts_cancel_effective),
        (0, EndState::Cancelled, Some(35))
    );
    let end = sink.trajectory.last().expect("end row");
    assert_eq!((end.kind, end.ts_event), (TrajectoryKind::End, 35));

    let mut b = x(10);
    b.ts_cancel = Some(40);
    let (sink, _) = go(Venue::Cme, s1_events(), vec![b], vec![]);
    let r = result(&sink);
    assert_eq!(
        (r.filled_qty, r.end_state, r.ts_first_fill),
        (10, EndState::Filled, Some(40))
    );
}

#[test]
fn o_sh_1_s12_nasdaq_hidden_print_moves_nothing() {
    let s = e1(base(false)).ev(30, |e| {
        e.trade(b'N', 100, 500);
    });
    let (sink, rep) = go(Venue::Nasdaq, s, vec![x(10)], vec![]);
    let r = result(&sink);
    assert_eq!((r.filled_qty, r.end_state), (0, EndState::OpenAtEnd));
    assert!(sink.shadow_fills.is_empty());
    assert_eq!(rep.stats.hidden_prints, 1);
    assert_eq!(rep.data_quality.hidden_prints, 1);
    assert_eq!(sink.trades[0].kind, qt_schema::rows::TradeKind::Hidden);
}

#[test]
fn o_sh_1_s13_amend_reduces_qty() {
    let s = e1(base(false))
        .ev(30, |e| {
            e.trade(A, 100, 60).exec(1, 60);
        })
        .ev(40, |e| {
            e.trade(A, 100, 50).exec(2, 40).exec(3, 10);
        })
        .ev(50, |e| {
            e.trade(A, 100, 25).exec(3, 25);
        });
    let mut s = s;
    s.recs[3].size = 50;
    let amend = ShadowAmend {
        shadow_id: "X".into(),
        ts_send: 25,
        new_qty: 10,
    };
    let (sink, _) = go(Venue::Cme, s, vec![x(30)], vec![amend]);
    assert_eq!(
        sink.shadow_fills,
        vec![fill(1, 40, 12, 10, 10, 0, FillReason::QueueDepleted, 40, 50, 0, 10)]
    );
    let r = result(&sink);
    // `qty` = the final quantity after amends (the testkit's reading).
    assert_eq!((r.end_state, r.filled_qty, r.qty), (EndState::Filled, 10, 10));
}

#[test]
fn o_sh_1_s14_clear_censors() {
    let s = e1(base(false)).ev(30, |e| {
        e.clear();
    });
    let (sink, _) = go(Venue::Cme, s, vec![x(10)], vec![]);
    let r = result(&sink);
    assert_eq!((r.end_state, r.filled_qty), (EndState::CensoredClear, 0));
    let end = sink.trajectory.last().expect("end row");
    assert_eq!((end.kind, end.ts_event, end.sequence), (TrajectoryKind::End, 30, 5));
}

#[test]
fn o_sh_1_s15_duplicate_shadow_id() {
    let err = qt_replay::Engine::new(cfg(Venue::Cme), &[instrument(Venue::Cme)], vec![x(10), x(5)], vec![])
        .err()
        .expect("duplicate id is an error");
    assert!(matches!(err, QtError::Duplicate { .. }));
    assert_eq!(err.code(), "QT-INPUT-003");
}

#[test]
fn o_sh_1_s16_touch_pricing_and_rejections() {
    let touch = |id: &str, off: i32| {
        let mut s = abs_shadow(id, BookSide::Bid, 0, 10, 15, 0);
        s.price = PriceSpec::Touch { offset_ticks: off };
        s
    };
    let (sink, _) = go(
        Venue::Cme,
        e1(base(false)),
        vec![touch("T0", 0), touch("Tm1", -1), touch("Tp1", 1)],
        vec![],
    );
    let by = |id: &str| sink.shadow_results.iter().find(|r| r.shadow_id == id).expect("row");
    assert_eq!((by("T0").price, by("T0").end_state), (Some(100), EndState::OpenAtEnd));
    assert_eq!(by("T0").queue_ahead_at_live, Some(100));
    assert_eq!((by("Tm1").price, by("Tm1").queue_ahead_at_live), (Some(99), Some(0)));
    assert_eq!(
        (by("Tp1").price, by("Tp1").end_state),
        (Some(101), EndState::RejectedWouldCross)
    );
    assert_eq!(by("Tp1").queue_ahead_at_live, None);
    let tp1: Vec<&TrajectoryRow> = sink.trajectory.iter().filter(|t| t.shadow_id == "Tp1").collect();
    assert_eq!(tp1.len(), 1);
    assert_eq!(tp1[0].kind, TrajectoryKind::End);
    assert!(sink.shadow_fills.iter().all(|f| f.shadow_id != "Tp1"));

    // No bids at activation.
    let s = Scn::new()
        .ev(10, |e| {
            e.add(9, A, 101, 500);
        })
        .ev(20, |e| {
            e.add(3, B, 100, 30);
        });
    let (sink, rep) = go(Venue::Cme, s, vec![touch("T0", 0)], vec![]);
    let r = result(&sink);
    assert_eq!(
        (r.end_state, r.price, r.filled_qty),
        (EndState::RejectedNoTouch, None, 0)
    );
    assert_eq!(sink.trajectory.len(), 1);
    assert_eq!(rep.shadows.map(|c| (c.n_rejected, c.n_live)), Some((1, 0)));
}
