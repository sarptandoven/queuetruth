//! O-BOOK-4 (replay part): every test of et's `replay.rs` and `shadow.rs` and of its integration suites ported to
//! the in-memory `Engine` and renamed by topic. et shadow semantics are reproduced under `FillRule::Et` (the default
//! `Exact` rule follows the audit rulings; where both agree the test runs both). File-format round trips of et
//! (DBN vs CSV, export-csv) are covered by the gate tests O-PAR-2 / O-PAR-4. et's shadow harness had no
//! sequence-gap censoring, so the ported shadow tests switch stream gap detection off.

mod common;

use common::{Market, cfg, instrument};
use qt_book::{Book, reference::RefBook};
use qt_replay::sinks::VecSink;
use qt_replay::{BboMode, Engine, EngineConfig, EngineReport, FillRule, TrajectoryMode};
use qt_schema::rows::{
    EndState, FillReason, FirstFillReason, PriceSpec, ShadowResultRow, ShadowSource, ShadowSpec, TradeKind,
    TrajectoryKind,
};
use qt_types::{BookSide, Instrument, LatencyModel, MboEvent, Side, UNDEF_PRICE, Venue, flags, rules};

const T0: i64 = 1_000_000;
const I7: u32 = 7;

#[allow(clippy::too_many_arguments)]
fn rec(ts: i64, seq: u64, action: u8, side: u8, id: u64, px: i64, size: u64, last: bool) -> MboEvent {
    MboEvent {
        ts_event: ts,
        ts_recv: ts,
        instrument_id: I7,
        action,
        side,
        price: px,
        size,
        order_id: id,
        flags: if last { flags::LAST } else { 0 },
        sequence: seq,
        ..MboEvent::default()
    }
}

/// et `shadow.rs` record: sequence = ts / 1000.
fn r(ts: i64, action: u8, side: u8, id: u64, px: i64, size: u64, last: bool) -> MboEvent {
    rec(ts, (ts / 1000) as u64, action, side, id, px, size, last)
}

fn req(id: &str, side: BookSide, px: i64, qty: u64, ts_submit: i64, ts_cancel: Option<i64>) -> ShadowSpec {
    ShadowSpec {
        shadow_id: id.into(),
        instrument_id: I7,
        side,
        price: PriceSpec::Abs(px),
        qty,
        ts_submit,
        latency_ns: 100,
        ts_cancel,
        group: None,
        source: ShadowSource::User,
        order_key: None,
    }
}

fn inst7(venue: Venue) -> Instrument {
    Instrument {
        instrument_id: I7,
        ..instrument(venue)
    }
}

struct Run {
    eng: Option<Engine>,
    sink: VecSink,
}

impl Run {
    fn new(venue: Venue, shadows: Vec<ShadowSpec>, latency: LatencyModel, rule: FillRule) -> Self {
        let mut c = cfg(venue);
        c.latency = latency;
        let mut eng = Engine::new(c, &[inst7(venue)], shadows, vec![]).expect("engine");
        eng.set_sequence_gap_detection(false);
        eng.set_fill_rule(rule);
        Self {
            eng: Some(eng),
            sink: VecSink::default(),
        }
    }
    fn event(&mut self, mut recs: Vec<MboEvent>) {
        if let Some(l) = recs.last_mut() {
            l.flags |= flags::LAST;
        }
        for e in &recs {
            self.eng.as_mut().unwrap().apply(e, &mut self.sink).unwrap();
        }
    }
    fn qa(&self, id: &str) -> Option<u64> {
        let e = self.eng.as_ref().unwrap();
        let st = e.shadow_state(e.shadow_index(id)?);
        (st.status == qt_replay::ShadowStatus::Live)
            .then_some(st.queue_ahead)
            .flatten()
    }
    fn live(&self, id: &str) -> bool {
        self.qa(id).is_some()
    }
    fn finish(mut self) -> (VecSink, EngineReport) {
        let rep = self.eng.take().unwrap().finish(&mut self.sink).unwrap();
        (self.sink, rep)
    }
}

fn row<'a>(rows: &'a [ShadowResultRow], id: &str) -> &'a ShadowResultRow {
    rows.iter().find(|r| r.shadow_id == id).unwrap()
}

fn seed_book(h: &mut Run) {
    // bids: 100 <- [1:5, 2:7]; asks: 102 <- [3:4]
    h.event(vec![
        r(T0, b'A', b'B', 1, 100, 5, false),
        r(T0, b'A', b'B', 2, 100, 7, false),
        r(T0, b'A', b'A', 3, 102, 4, true),
    ]);
}

#[test]
fn o_book_4_shadow_queue_depletion_with_partial_then_full() {
    for rule in [FillRule::Et, FillRule::Exact] {
        let mut h = Run::new(
            Venue::Cme,
            vec![req("s", BookSide::Bid, 100, 4, T0 + 1000, None)],
            LatencyModel::default(),
            rule,
        );
        seed_book(&mut h);
        assert!(!h.live("s"));
        h.event(vec![r(T0 + 2000, b'A', b'B', 4, 100, 10, true)]);
        assert_eq!(h.qa("s"), Some(12));
        h.event(vec![
            r(T0 + 3000, b'T', b'A', 0, 100, 8, false),
            r(T0 + 3000, b'F', b'B', 1, 100, 5, false),
            r(T0 + 3000, b'F', b'B', 2, 100, 3, false),
            r(T0 + 3000, b'C', b'B', 1, 100, 5, false),
            r(T0 + 3000, b'C', b'B', 2, 100, 3, true),
        ]);
        assert_eq!(h.qa("s"), Some(4));
        h.event(vec![
            r(T0 + 4000, b'T', b'A', 0, 100, 7, false),
            r(T0 + 4000, b'F', b'B', 2, 100, 4, false),
            r(T0 + 4000, b'F', b'B', 4, 100, 3, false),
            r(T0 + 4000, b'C', b'B', 2, 100, 4, false),
            r(T0 + 4000, b'C', b'B', 4, 100, 3, true),
        ]);
        assert_eq!(h.qa("s"), Some(0));
        h.event(vec![
            r(T0 + 5000, b'T', b'A', 0, 100, 2, false),
            r(T0 + 5000, b'F', b'B', 4, 100, 2, false),
            r(T0 + 5000, b'C', b'B', 4, 100, 2, true),
        ]);
        assert!(!h.live("s"));
        let bid = h.eng.as_ref().unwrap().book(I7).unwrap().bbo().bid.map(|l| l.total);
        assert_eq!(bid, Some(5), "real book untouched by shadows");
        let (sink, _) = h.finish();
        let x = row(&sink.shadow_results, "s");
        assert_eq!(
            (
                x.filled_qty,
                x.first_fill_reason,
                x.ts_first_fill,
                x.queue_ahead_at_first_fill,
                x.queue_ahead_at_live
            ),
            (4, FirstFillReason::QueueDepleted, Some(T0 + 4000), Some(4), Some(12)),
            "{rule:?}"
        );
        // Per-fill timeline (et kept only the first fill).
        let f: Vec<(i64, u64)> = sink.shadow_fills.iter().map(|f| (f.ts_event, f.fill_qty)).collect();
        assert_eq!(f, vec![(T0 + 4000, 3), (T0 + 5000, 1)]);
    }
}

#[test]
fn o_book_4_shadow_cancels_and_priority_loss_ahead_advance_queue() {
    for rule in [FillRule::Et, FillRule::Exact] {
        let mut h = Run::new(
            Venue::Cme,
            vec![req("s", BookSide::Bid, 100, 2, T0 + 1000, None)],
            LatencyModel::default(),
            rule,
        );
        seed_book(&mut h);
        h.event(vec![r(T0 + 2000, b'C', b'B', 1, 100, 2, true)]);
        assert_eq!(h.qa("s"), Some(10));
        h.event(vec![r(T0 + 3000, b'M', b'B', 2, 100, 9, true)]);
        assert_eq!(h.qa("s"), Some(3));
        h.event(vec![r(T0 + 4000, b'M', b'B', 1, 99, 3, true)]);
        assert_eq!(h.qa("s"), Some(0));
        h.event(vec![
            r(T0 + 5000, b'T', b'A', 0, 100, 1, false),
            r(T0 + 5000, b'F', b'B', 2, 100, 1, false),
            r(T0 + 5000, b'C', b'B', 2, 100, 1, true),
        ]);
        let (sink, _) = h.finish();
        let x = row(&sink.shadow_results, "s");
        assert_eq!(
            (x.filled_qty, x.first_fill_reason, x.queue_ahead_at_first_fill),
            (1, FirstFillReason::QueueDepleted, Some(0)),
            "{rule:?}"
        );
    }
}

#[test]
fn o_book_4_shadow_crossed_by_opposite_best_and_by_trade_through() {
    let shadows = vec![
        req("inside", BookSide::Bid, 101, 3, T0 + 1000, None),
        req("through", BookSide::Ask, 101, 5, T0 + 2500, None),
    ];
    let mut h = Run::new(Venue::Cme, shadows, LatencyModel::default(), FillRule::Et);
    seed_book(&mut h);
    h.event(vec![r(T0 + 2000, b'A', b'A', 5, 101, 1, true)]);
    h.event(vec![
        r(T0 + 3000, b'T', b'B', 0, 101, 1, false),
        r(T0 + 3000, b'F', b'A', 5, 101, 1, false),
        r(T0 + 3000, b'C', b'A', 5, 101, 1, false),
        r(T0 + 3000, b'T', b'B', 0, 102, 2, false),
        r(T0 + 3000, b'F', b'A', 3, 102, 2, false),
        r(T0 + 3000, b'C', b'A', 3, 102, 2, true),
    ]);
    let (sink, _) = h.finish();
    let a = row(&sink.shadow_results, "inside");
    assert_eq!(
        (a.filled_qty, a.first_fill_reason, a.ts_first_fill),
        (3, FirstFillReason::Crossed, Some(T0 + 2000))
    );
    let b = row(&sink.shadow_results, "through");
    assert_eq!(
        (
            b.filled_qty,
            b.first_fill_reason,
            b.queue_ahead_at_first_fill,
            b.queue_ahead_at_live
        ),
        (2, FirstFillReason::TradeThrough, Some(1), Some(1))
    );
}

#[test]
fn o_book_4_shadow_marketable_at_activation_fills_at_ts_live() {
    for rule in [FillRule::Et, FillRule::Exact] {
        let mut h = Run::new(
            Venue::Cme,
            vec![req("m", BookSide::Bid, 103, 2, T0 + 1000, None)],
            LatencyModel::default(),
            rule,
        );
        seed_book(&mut h);
        h.event(vec![r(T0 + 2000, b'A', b'B', 9, 99, 1, true)]);
        let (sink, _) = h.finish();
        let x = row(&sink.shadow_results, "m");
        assert_eq!(
            (
                x.filled_qty,
                x.first_fill_reason,
                x.ts_first_fill,
                x.queue_ahead_at_live
            ),
            (2, FirstFillReason::CrossedAtLive, Some(T0 + 1100), Some(0))
        );
        // Exact: a taker at the resting ask (102); et: at the shadow price.
        let px = if rule == FillRule::Exact { 102 } else { 103 };
        assert_eq!(sink.shadow_fills[0].fill_px, px, "{rule:?}");
    }
}

#[test]
fn o_book_4_shadow_cancel_latency_and_never() {
    let shadows = vec![
        req("early", BookSide::Bid, 100, 1, T0 + 1000, Some(T0 + 1000)),
        req("late", BookSide::Bid, 100, 1, T0 + 1000, Some(T0 + 2500)),
        req("never", BookSide::Bid, 100, 1, T0 + 1000, None),
        req("pending", BookSide::Bid, 100, 1, T0 + 9_000_000, None),
    ];
    let mut h = Run::new(Venue::Cme, shadows, LatencyModel::default(), FillRule::Exact);
    seed_book(&mut h);
    h.event(vec![r(T0 + 2000, b'A', b'B', 9, 99, 1, true)]);
    assert!(!h.live("early"));
    assert!(h.live("late") && h.live("never"));
    h.event(vec![r(T0 + 3000, b'A', b'B', 10, 99, 1, true)]);
    assert!(!h.live("late"));
    h.event(vec![r(T0 + 4000, b'A', b'B', 11, 99, 1, true)]);
    let (sink, _) = h.finish();
    for id in ["early", "late", "never", "pending"] {
        let x = row(&sink.shadow_results, id);
        assert_eq!(
            (x.filled_qty, x.first_fill_reason, x.ts_first_fill),
            (0, FirstFillReason::Never, None),
            "{id}"
        );
    }
    assert_eq!(row(&sink.shadow_results, "pending").queue_ahead_at_live, None);
    assert_eq!(row(&sink.shadow_results, "late").queue_ahead_at_live, Some(12));
    assert_eq!(row(&sink.shadow_results, "early").end_state, EndState::Cancelled);
    assert_eq!(row(&sink.shadow_results, "early").ts_activated_event, None);
    assert_eq!(row(&sink.shadow_results, "never").end_state, EndState::OpenAtEnd);
}

#[test]
fn o_book_4_shadow_u64_sizes_in_queue_and_fills() {
    const BIG: u64 = 5_000_000_000;
    for rule in [FillRule::Et, FillRule::Exact] {
        let mut h = Run::new(
            Venue::Cme,
            vec![req("s", BookSide::Bid, 100, BIG, T0 + 1000, None)],
            LatencyModel::default(),
            rule,
        );
        h.event(vec![
            r(T0, b'A', b'B', 1, 100, BIG, false),
            r(T0, b'A', b'A', 3, 102, 4, true),
        ]);
        h.event(vec![r(T0 + 2000, b'A', b'B', 4, 99, 1, true)]);
        assert_eq!(h.qa("s"), Some(BIG));
        // Exact needs an execution behind: a second bid joins behind and trades 7.
        h.event(vec![r(T0 + 2500, b'A', b'B', 5, 100, 7, true)]);
        h.event(vec![
            r(T0 + 3000, b'T', b'A', 0, 100, BIG + 7, false),
            r(T0 + 3000, b'F', b'B', 1, 100, BIG, false),
            r(T0 + 3000, b'C', b'B', 1, 100, BIG, false),
            r(T0 + 3000, b'F', b'B', 5, 100, 7, false),
            r(T0 + 3000, b'C', b'B', 5, 100, 7, true),
        ]);
        assert_eq!(h.qa("s"), Some(0));
        let (sink, _) = h.finish();
        let x = row(&sink.shadow_results, "s");
        assert_eq!((x.filled_qty, x.queue_ahead_at_first_fill), (7, Some(BIG)), "{rule:?}");
    }
}

/// Nasdaq: a side-N print neither advances the queue nor counts as a sweep (et rule); an `F` and its `C` pair by
/// sequence even when every record carries `F_LAST`.
#[test]
fn o_book_4_shadow_nasdaq_hidden_prints_and_sequence_paired_fills() {
    let shadows = vec![
        req("q", BookSide::Bid, 100, 4, T0 + 1000, None),
        req("x", BookSide::Ask, 101, 5, T0 + 1000, None),
    ];
    let mut h = Run::new(Venue::Nasdaq, shadows, LatencyModel::default(), FillRule::Et);
    seed_book(&mut h);
    h.event(vec![r(T0 + 2000, b'A', b'B', 4, 99, 1, true)]);
    assert_eq!(h.qa("q"), Some(12));
    h.event(vec![
        r(T0 + 3000, b'T', b'N', 0, 100, 50, false),
        r(T0 + 3000, b'T', b'N', 0, 103, 50, true),
    ]);
    assert_eq!(h.qa("q"), Some(12));
    // The same prints with an aggressor side count on CME (et behaviour).
    let mut c = Run::new(
        Venue::Cme,
        vec![req("q", BookSide::Bid, 100, 4, T0 + 1000, None)],
        LatencyModel::default(),
        FillRule::Et,
    );
    seed_book(&mut c);
    c.event(vec![r(T0 + 2000, b'A', b'B', 4, 99, 1, true)]);
    c.event(vec![r(T0 + 3000, b'T', b'N', 0, 100, 5, true)]);
    assert_eq!(c.qa("q"), Some(7));
    // E message as T, F, C with F_LAST on each record; the C has the F's sequence: a fill removal.
    let mut t = r(T0 + 4000, b'T', b'A', 0, 100, 5, true);
    let mut f = r(T0 + 4000, b'F', b'B', 1, 100, 5, true);
    let mut cc = r(T0 + 4000, b'C', b'B', 1, 100, 5, true);
    for x in [&mut t, &mut f, &mut cc] {
        x.sequence = 77;
    }
    h.event(vec![t]);
    h.event(vec![f]);
    h.event(vec![cc]);
    assert_eq!(h.qa("q"), Some(7));
    let mut pc = r(T0 + 5000, b'C', b'B', 2, 100, 7, true);
    pc.sequence = 78;
    h.event(vec![pc]);
    assert_eq!(h.qa("q"), Some(0));
    let (sink, _) = h.finish();
    assert_eq!(row(&sink.shadow_results, "q").filled_qty, 0);
    assert_eq!(row(&sink.shadow_results, "x").filled_qty, 0);
}

#[test]
fn o_book_4_shadow_latency_model_adds_to_submit_and_cancel_delays() {
    let model = LatencyModel {
        feed_ns: 50,
        decision_ns: 25,
        gateway_ns: 25,
        jitter_ns: 0,
    };
    let shadows = vec![
        req("s", BookSide::Bid, 100, 1, T0 + 1000, None),
        req("c", BookSide::Bid, 100, 1, T0 + 1000, Some(T0 + 1500)),
    ];
    let mut h = Run::new(Venue::Cme, shadows, model, FillRule::Exact);
    seed_book(&mut h);
    h.event(vec![r(T0 + 1150, b'A', b'B', 9, 99, 1, true)]);
    assert!(!h.live("s"));
    h.event(vec![r(T0 + 1201, b'A', b'B', 10, 99, 1, true)]);
    assert!(h.live("s") && h.live("c"));
    h.event(vec![r(T0 + 1620, b'A', b'B', 11, 99, 1, true)]);
    assert!(h.live("c"));
    h.event(vec![r(T0 + 1626, b'A', b'B', 12, 99, 1, true)]);
    assert!(!h.live("c"));
    let (sink, _) = h.finish();
    let x = row(&sink.shadow_results, "s");
    assert_eq!((x.ts_live, x.latency_applied_ns), (T0 + 1200, 200));
    assert_eq!(row(&sink.shadow_results, "c").ts_cancel_effective, Some(T0 + 1625));

    // Jitter: deterministic per seed, bounded, varies, and depends on the seed (stable_seed per shadow id).
    let reqs: Vec<ShadowSpec> = (0..16)
        .map(|i| req(&format!("j{i}"), BookSide::Bid, 100, 1, T0 + 1000, None))
        .collect();
    let jit = LatencyModel {
        jitter_ns: 1_000,
        ..LatencyModel::default()
    };
    let lat = |seed: u64| -> Vec<i64> {
        let mut c = cfg(Venue::Cme);
        c.latency = jit;
        c.seed = seed;
        let eng = Engine::new(c, &[inst7(Venue::Cme)], reqs.clone(), vec![]).unwrap();
        let mut s = VecSink::default();
        eng.finish(&mut s).unwrap();
        s.shadow_results.iter().map(|r| r.latency_applied_ns).collect()
    };
    let (a, b, c) = (lat(7), lat(7), lat(8));
    assert_eq!(a, b);
    assert!(a.iter().all(|l| (100..=1_100).contains(l)));
    assert!(a.iter().any(|l| *l != a[0]));
    assert_ne!(a, c);
    // Independently computed first draw for shadow j0, seed 7.
    let mut rng = qt_types::Xoshiro256StarStar::new(qt_types::stable_seed(7, &[b"latency", b"j0"]));
    assert_eq!(a[0], 100 + rng.range_inclusive(0, 1_000) as i64);
}

/// et `queue_trace` (rows at activation and whenever queue ahead changed) = trajectory rows `live` + `event`
/// rows whose queue ahead changed.
#[test]
fn o_book_4_queue_trace_rows_on_every_change() {
    for rule in [FillRule::Et, FillRule::Exact] {
        let mut h = Run::new(
            Venue::Cme,
            vec![req("s", BookSide::Bid, 100, 4, T0 + 1000, None)],
            LatencyModel::default(),
            rule,
        );
        let (a, b, c, t, f) = (b'A', b'B', b'C', b'T', b'F');
        let ev: Vec<Vec<MboEvent>> = vec![
            vec![
                rec(T0, 1, a, b, 1, 100, 5, false),
                rec(T0, 1, a, b, 2, 100, 7, false),
                rec(T0, 1, a, b'A', 3, 102, 4, true),
            ],
            vec![rec(T0 + 2000, 2, a, b, 4, 100, 10, true)],
            vec![
                rec(T0 + 3000, 3, t, b'A', 0, 100, 8, false),
                rec(T0 + 3000, 3, f, b, 1, 100, 5, false),
                rec(T0 + 3000, 3, f, b, 2, 100, 3, false),
                rec(T0 + 3000, 3, c, b, 1, 100, 5, false),
                rec(T0 + 3000, 3, c, b, 2, 100, 3, true),
            ],
            vec![
                rec(T0 + 4000, 4, t, b'A', 0, 100, 7, false),
                rec(T0 + 4000, 4, f, b, 2, 100, 4, false),
                rec(T0 + 4000, 4, f, b, 4, 100, 3, false),
                rec(T0 + 4000, 4, c, b, 2, 100, 4, false),
                rec(T0 + 4000, 4, c, b, 4, 100, 3, true),
            ],
            vec![
                rec(T0 + 5000, 5, t, b'A', 0, 100, 2, false),
                rec(T0 + 5000, 5, f, b, 4, 100, 2, false),
                rec(T0 + 5000, 5, c, b, 4, 100, 2, true),
            ],
        ];
        for e in ev {
            h.event(e);
        }
        let (sink, rep) = h.finish();
        let mut last = None;
        let trace: Vec<(i64, u64)> = sink
            .trajectory
            .iter()
            .filter(|t| t.kind != TrajectoryKind::End)
            .filter_map(|t| {
                let keep = last != Some(t.queue_ahead_after);
                last = Some(t.queue_ahead_after);
                keep.then_some((t.ts_event, t.queue_ahead_after))
            })
            .collect();
        assert_eq!(trace, vec![(T0 + 1100, 12), (T0 + 3000, 4), (T0 + 4000, 0)], "{rule:?}");
        let x = &sink.shadow_results[0];
        assert_eq!((x.filled_qty, x.latency_applied_ns), (4, 100));
        assert_eq!(rep.stats.sequence_gaps + rep.stats.sequence_regressions, 0);
    }
}

#[test]
fn o_book_4_sequence_gaps_and_regressions_per_channel_skip_snapshots() {
    let mut eng = Engine::new(cfg(Venue::Cme), &[], vec![], vec![]).unwrap();
    let mut sink = VecSink::default();
    let a = |seq: u64, id: u64| rec(1_000 + seq as i64, seq, b'A', b'B', id, 100, 1, true);
    let mut snap = a(0, 1);
    snap.flags |= flags::SNAPSHOT;
    for x in [snap, a(10, 2), a(11, 3), a(13, 4), a(14, 5), a(12, 6), a(13, 7)] {
        eng.apply(&x, &mut sink).unwrap();
    }
    let mut other = a(500, 8);
    other.channel_id = 1;
    eng.apply(&other, &mut sink).unwrap();
    let mut snap2 = a(0, 9);
    snap2.flags |= flags::SNAPSHOT;
    eng.apply(&snap2, &mut sink).unwrap();
    eng.apply(&a(900, 10), &mut sink).unwrap();
    let rep = eng.finish(&mut sink).unwrap();
    assert_eq!((rep.stats.sequence_gaps, rep.stats.sequence_regressions), (1, 1));
    assert_eq!(rep.stats.snapshot_records, 2);
    // et counts the second snapshot (ts 1000 after 1900) too; snapshot records are no ordering anomaly here
    // (they carry the snapshot's own timestamps), so only 1012 after 1014 counts.
    assert_eq!(rep.stats.out_of_order_ts_event, 1);
    assert_eq!(rep.stats.files_without_snapshot, 0);
    assert_eq!(rep.data_quality.sequence_gaps, 1);
}

/// et `parses_shadows_csv` and `shadow_fills_csv_roundtrips`: the et shadows.csv layout reads through
/// `qt_schema::codec`, and result rows round-trip through the Parquet codec.
#[test]
fn o_book_4_shadows_csv_and_results_round_trip() {
    let dir = std::env::temp_dir().join(format!("qt-replay-etport-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("shadows.csv");
    std::fs::write(
        &p,
        "shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns,ts_cancel\na,1,B,100,2,10,5,\nb,1,sell,101,3,20,5,30\n",
    )
    .unwrap();
    let s = qt_schema::codec::read_shadow_specs(&p).unwrap();
    assert_eq!(
        (s[0].side, s[0].ts_cancel, s[1].side, s[1].ts_cancel),
        (BookSide::Bid, None, BookSide::Ask, Some(30))
    );
    std::fs::write(
        &p,
        "instrument_id,shadow_id,qty,price,side,latency_ns,ts_submit,strategy,note\n2,c,1,99,ask,0,7,mm,hello world\n",
    )
    .unwrap();
    let s = qt_schema::codec::read_shadow_specs(&p).unwrap();
    assert_eq!(
        (s[0].shadow_id.as_str(), s[0].instrument_id, s[0].price, s[0].ts_submit),
        ("c", 2, PriceSpec::Abs(99), 7)
    );
    std::fs::write(
        &p,
        "shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns\nd,1,B,1,1,1,0\nd,1,B,1,1,1,0\n",
    )
    .unwrap();
    assert_eq!(
        qt_schema::codec::read_shadow_specs(&p).unwrap_err().code(),
        "QT-INPUT-003"
    );
    std::fs::remove_dir_all(&dir).ok();

    let mut h = Run::new(
        Venue::Cme,
        vec![req("s", BookSide::Bid, 100, 4, T0 + 1000, None)],
        LatencyModel::default(),
        FillRule::Exact,
    );
    seed_book(&mut h);
    let (sink, _) = h.finish();
    let batch = qt_schema::codec::shadow_results_to_batch(&sink.shadow_results);
    assert_eq!(
        qt_schema::codec::batch_to_shadow_results(&batch).unwrap(),
        sink.shadow_results
    );
}

/// et `depth_csv_matches_reference_book`: depth rows (change-only, long format) rebuild the reference book's
/// top N after every event.
#[test]
fn o_book_4_depth_rows_match_reference_book() {
    const N: usize = 4;
    for venue in [Venue::Cme, Venue::Nasdaq, Venue::Generic] {
        let events = Market::generate(3, venue, 600);
        let mut c = cfg(venue);
        c.depth_levels = N as u16;
        c.bbo = BboMode::Full;
        let (sink, _) = common::run(c, &[instrument(venue)], &events, vec![], vec![]);
        let mut rb = RefBook::new(rules(venue));
        let mut view: [Vec<Option<(i64, u64, u32)>>; 2] = [vec![None; N], vec![None; N]];
        let mut di = 0;
        let mut ev_i = 0;
        for e in &events {
            rb.apply(e);
            if e.flags & flags::LAST == 0 {
                continue;
            }
            while di < sink.depth.len()
                && sink.depth[di].sequence == e.sequence
                && sink.depth[di].ts_event == e.ts_event
            {
                let d = &sink.depth[di];
                let k = usize::from(d.side == BookSide::Ask);
                view[k][usize::from(d.level)] = d.price.map(|p| (p, d.size, d.count));
                di += 1;
            }
            for (k, side) in [BookSide::Bid, BookSide::Ask].into_iter().enumerate() {
                let want: Vec<Option<(i64, u64, u32)>> = (0..N)
                    .map(|i| rb.levels(side).nth(i).map(|l| (l.price, l.total, l.count)))
                    .collect();
                assert_eq!(view[k], want, "{venue:?} event {ev_i}");
            }
            let b = rb.bbo();
            let row = &sink.bbo[ev_i];
            assert_eq!(
                (row.bid_px, row.ask_px),
                (b.bid.map(|l| l.price), b.ask.map(|l| l.price))
            );
            ev_i += 1;
        }
        assert_eq!(di, sink.depth.len());
        assert_eq!(ev_i, sink.bbo.len());
    }
}

/// et `invariants_zero_on_synth_and_counted_on_corrupted_stream`, on the local generator.
#[test]
fn o_book_4_invariants_zero_on_synthetic_and_counted_on_corrupted() {
    for venue in [Venue::Cme, Venue::Nasdaq, Venue::Generic] {
        let events = Market::generate(5, venue, 1500);
        let mut c = cfg(venue);
        c.rank_oracle = true;
        let (sink, rep) = common::run(c.clone(), &[instrument(venue)], &events, vec![], vec![]);
        assert_eq!(rep.invariants, qt_book::Invariants::default(), "{venue:?}");
        let o = rep.rank_oracle.clone().unwrap();
        let fills = events.iter().filter(|e| e.action == b'F').count() as u64;
        assert!(fills > 50);
        assert_eq!(o.executions_checked, fills);
        assert_eq!(o.fifo_rank_histogram, [fills, 0, 0, 0, 0, 0]);
        assert_eq!(o.fifo_rank_histogram_sweep_adjusted, [fills, 0, 0, 0, 0, 0]);
        assert_eq!((o.price_priority_violations, o.executions_unknown_order), (0, 0));
        assert_eq!((o.rank0_share, o.rank0_share_sweep_adjusted), (Some(1.0), Some(1.0)));
        assert!(o.sweeps > 0 && o.sweeps <= fills);
        assert_eq!(o.rank0_share_by_bucket.values().map(|b| b.checked).sum::<u64>(), fills);
        assert_eq!(sink.rank_trace.len() as u64, fills);

        // Corrupt: oversize cancel and fill, unknown C and F, duplicate add, crossing add.
        let mut ev = events.clone();
        let n = ev.len();
        let find = |ev: &[MboEvent], a: u8, from: usize| (from..ev.len()).find(|&i| ev[i].action == a).unwrap();
        let i = find(&ev, b'C', n / 2);
        ev[i].size = 4_000_000_000;
        let i = find(&ev, b'F', n / 2);
        ev[i].size = 4_000_000_000;
        let i = find(&ev, b'A', n / 2);
        let mut dup = ev[i];
        dup.flags &= !flags::LAST;
        ev.insert(i + 1, dup);
        let i = find(&ev, b'C', 10);
        let mut ghost = ev[i];
        ghost.order_id = 999_999_999;
        ghost.flags = 0;
        let mut gf = ghost;
        gf.action = b'F';
        ev.insert(i, gf);
        ev.insert(i, ghost);
        let i = (n / 3..ev.len()).find(|&i| ev[i].flags & flags::LAST != 0).unwrap();
        let mut cross = ev[i];
        cross.action = b'A';
        cross.side = b'B';
        cross.order_id = 888_888_888;
        cross.price = 2_000;
        cross.size = 1;
        ev.insert(i + 1, cross);
        let (_, rep) = common::run(c, &[instrument(venue)], &ev, vec![], vec![]);
        let inv = rep.invariants;
        assert!(
            inv.cancels_exceeding_resting >= 1 && inv.fills_exceeding_resting >= 1,
            "{inv:?}"
        );
        assert!(
            inv.fills_unknown_order >= 1 && inv.unknown_order_refs >= 1 && inv.duplicate_adds >= 1,
            "{inv:?}"
        );
        assert!(
            inv.crossed_books_after_last >= 1 && inv.negative_spreads >= 1,
            "{inv:?}"
        );
        assert_eq!(inv.unexpected_modifies, 0);
        let o = rep.rank_oracle.unwrap();
        assert!(o.price_priority_violations >= 1);
        // The ghost fill, plus any later fill of the order the oversize cancel removed.
        assert!(o.executions_unknown_order >= 1);
        assert_eq!(
            o.rank0_share,
            Some(o.fifo_rank_histogram[0] as f64 / o.executions_checked as f64)
        );
        assert_ne!(rep.data_quality.verdict, qt_schema::json::Verdict::Clean);
    }
}

/// et `venue_nasdaq_semantics_in_engine` (in memory).
#[test]
fn o_book_4_venue_nasdaq_semantics_in_engine() {
    let t0: i64 = 1_700_000_000_000_000_000;
    let row = |ts: i64, act: u8, side: u8, px: i64, sz: u64, id: u64, last: bool, seq: u64| MboEvent {
        instrument_id: 1,
        ..rec(ts, seq, act, side, id, px, sz, last)
    };
    let recs = vec![
        row(t0, b'A', b'B', 100, 5, 1, false, 1),
        row(t0, b'A', b'B', 100, 7, 2, false, 1),
        row(t0, b'A', b'A', 102, 4, 3, true, 1),
        row(t0 + 1000, b'M', b'B', 100, 50, 1, true, 2),
        row(t0 + 2000, b'A', b'B', 100, 10, 4, true, 3),
        row(t0 + 3000, b'T', b'N', 100, 500, 0, false, 4),
        row(t0 + 3000, b'T', b'N', 103, 900, 0, true, 4),
        row(t0 + 4000, b'T', b'A', 100, 5, 0, false, 5),
        row(t0 + 4000, b'F', b'B', 100, 5, 1, false, 5),
        row(t0 + 4000, b'C', b'B', 100, 5, 1, true, 5),
        row(t0 + 5000, b'C', b'B', 100, 3, 2, true, 6),
    ];
    let mut sh = req("s", BookSide::Bid, 100, 4, t0 + 500, None);
    sh.instrument_id = 1;
    let mut c = cfg(Venue::Nasdaq);
    c.depth_levels = 2;
    c.bbo = BboMode::Full;
    let (sink, rep) = common::run(c, &[instrument(Venue::Nasdaq)], &recs, vec![sh.clone()], vec![]);
    assert_eq!(rep.invariants.unexpected_modifies, 1);
    assert_eq!(rep.stats.files_without_snapshot, 0);
    let tr: Vec<(i64, i64, u64, Side, TradeKind)> = sink
        .trades
        .iter()
        .map(|t| (t.ts_event, t.price, t.size, t.aggressor_side, t.kind))
        .collect();
    assert_eq!(
        tr,
        vec![
            (t0 + 3000, 100, 500, Side::None, TradeKind::Hidden),
            (t0 + 3000, 103, 900, Side::None, TradeKind::Hidden),
            (t0 + 4000, 100, 5, Side::Ask, TradeKind::Displayed)
        ]
    );
    let last = sink.bbo.last().unwrap();
    assert_eq!(
        (
            last.ts_event,
            last.bid_px,
            last.bid_sz,
            last.ask_px,
            last.ask_sz,
            last.sequence
        ),
        (t0 + 5000, Some(100), 14, Some(102), 4, 6)
    );
    let qa: Vec<(i64, u64)> = sink
        .trajectory
        .iter()
        .filter(|t| t.kind != TrajectoryKind::End)
        .map(|t| (t.ts_event, t.queue_ahead_after))
        .collect();
    assert_eq!(qa, vec![(t0 + 600, 12), (t0 + 4000, 7), (t0 + 5000, 4)]);
    assert_eq!(sink.shadow_results[0].filled_qty, 0);
    // CME rules: the M applies (size up: back of the queue), and the missing snapshot is flagged.
    let (sink, rep) = common::run(cfg(Venue::Cme), &[instrument(Venue::Cme)], &recs, vec![sh], vec![]);
    assert_eq!(rep.stats.files_without_snapshot, 1);
    assert_eq!(rep.data_quality.source_specific.get("files_without_snapshot"), Some(&1));
    assert!(sink.shadow_fills.iter().any(|f| f.reason == FillReason::QueueDepleted));
    // Coinbase / generic: duplicate id is an anomaly.
    for venue in [Venue::Coinbase, Venue::Generic] {
        let dup = vec![
            row(t0, b'A', b'B', 100, 5, 1, true, 1),
            row(t0 + 1, b'A', b'B', 101, 5, 1, true, 2),
        ];
        let mut c = cfg(venue);
        c.bbo = BboMode::Full;
        let (sink, rep) = common::run(c, &[instrument(venue)], &dup, vec![], vec![]);
        assert_eq!(rep.invariants.duplicate_adds, 1);
        let b = sink.bbo.last().unwrap();
        assert_eq!((b.bid_px, b.bid_sz, b.ask_px, b.ask_sz), (Some(101), 5, None, 0));
    }
}

/// et `u64_sizes_flow_through_csv_inputs_and_outputs` (in memory).
#[test]
fn o_book_4_u64_sizes_through_bbo_depth_trades_and_shadows() {
    const BIG: u64 = 5_000_000_000;
    let t = 1_700_000_000_000_000_000i64;
    let row = |ts: i64, act: u8, side: u8, px: i64, sz: u64, id: u64, last: bool, seq: u64| MboEvent {
        instrument_id: 1,
        ..rec(ts, seq, act, side, id, px, sz, last)
    };
    let recs = vec![
        row(t, b'A', b'B', 100, BIG, 1, true, 1),
        row(t + 1, b'A', b'A', 101, BIG + 1, 2, true, 2),
        row(t + 2, b'A', b'B', 100, 3, 3, true, 3),
        row(t + 3, b'T', b'A', 100, BIG + 2, 0, false, 4),
        row(t + 3, b'F', b'B', 100, BIG, 1, false, 4),
        row(t + 3, b'C', b'B', 100, BIG, 1, false, 4),
        row(t + 3, b'F', b'B', 100, 2, 3, false, 4),
        row(t + 3, b'C', b'B', 100, 2, 3, true, 4),
    ];
    let mut sh = req("s", BookSide::Bid, 100, BIG, t + 1, None);
    sh.instrument_id = 1;
    sh.latency_ns = 0;
    let mut c = cfg(Venue::Nasdaq);
    c.depth_levels = 2;
    c.bbo = BboMode::Full;
    c.rank_oracle = true;
    let (sink, rep) = common::run(c, &[instrument(Venue::Nasdaq)], &recs, vec![sh], vec![]);
    assert_eq!(rep.rank_oracle.unwrap().rank0_share, Some(1.0));
    let b: Vec<(Option<i64>, u64, Option<i64>, u64)> = sink
        .bbo
        .iter()
        .map(|b| (b.bid_px, b.bid_sz, b.ask_px, b.ask_sz))
        .collect();
    assert_eq!(b[1], (Some(100), BIG, Some(101), BIG + 1));
    assert_eq!(b[2], (Some(100), BIG + 3, Some(101), BIG + 1));
    assert_eq!(b[3], (Some(100), 1, Some(101), BIG + 1));
    assert!(sink.depth.iter().any(|d| d.size == BIG + 3 && d.side == BookSide::Bid));
    assert_eq!(sink.trades[0].size, BIG + 2);
    let x = &sink.shadow_results[0];
    assert_eq!(
        (
            x.ts_live,
            x.ts_activated_event,
            x.ts_first_fill,
            x.filled_qty,
            x.queue_ahead_at_live,
            x.queue_ahead_at_first_fill
        ),
        (t + 1, Some(t + 2), Some(t + 3), 2, Some(BIG), Some(BIG))
    );
    assert_eq!(x.first_fill_reason, FirstFillReason::QueueDepleted);
}

/// et `sweep_adjusted_rank_groups_out_of_order_fills`.
#[test]
fn o_book_4_sweep_adjusted_rank_groups_out_of_order_fills() {
    let t0 = 1_700_000_000_000_000_000i64;
    let mut recs = Vec::new();
    let mut seq = 0u64;
    let push = |recs: &mut Vec<MboEvent>, ts: i64, act: u8, side: u8, px: i64, sz: u64, id: u64, last: bool, s: u64| {
        recs.push(MboEvent {
            instrument_id: 1,
            ..rec(ts, s, act, side, id, px, sz, last)
        });
    };
    for (i, id) in [1u64, 2, 3].into_iter().enumerate() {
        push(&mut recs, t0, b'A', b'B', 100, 5, id, true, i as u64 + 1);
    }
    push(&mut recs, t0, b'A', b'A', 102, 4, 9, true, 4);
    seq += 4;
    let exec = |recs: &mut Vec<MboEvent>, seq: &mut u64, ts: i64, id: u64| {
        *seq += 1;
        for (a, s, i, last) in [(b'T', b'A', 0, false), (b'F', b'B', id, false), (b'C', b'B', id, true)] {
            recs.push(MboEvent {
                instrument_id: 1,
                ..rec(ts, *seq, a, s, i, 100, 5, last)
            });
        }
    };
    let t1 = t0 + 1_000_000;
    exec(&mut recs, &mut seq, t1, 3);
    exec(&mut recs, &mut seq, t1 + 300, 2);
    exec(&mut recs, &mut seq, t1 + 600, 1);
    let t2 = t0 + 10_000_000;
    seq += 1;
    push(&mut recs, t2, b'A', b'B', 100, 5, 4, true, seq);
    seq += 1;
    push(&mut recs, t2, b'A', b'B', 100, 5, 5, true, seq);
    let t3 = t0 + 20_000_000;
    exec(&mut recs, &mut seq, t3, 5);
    seq += 1;
    push(&mut recs, t3 + 100, b'A', b'B', 100, 5, 6, true, seq);
    exec(&mut recs, &mut seq, t3 + 200, 4);
    let midnight = t0 - 34_200 * 1_000_000_000;
    let inst = Instrument {
        midnight_utc_ns: midnight,
        open_s: 34_200,
        close_s: 57_600,
        ..instrument(Venue::Nasdaq)
    };
    let mut c = cfg(Venue::Nasdaq);
    c.rank_oracle = true;
    let (sink, rep) = common::run(c.clone(), std::slice::from_ref(&inst), &recs, vec![], vec![]);
    let o = rep.rank_oracle.unwrap();
    assert_eq!(o.executions_checked, 5);
    assert_eq!(o.fifo_rank_histogram, [2, 2, 1, 0, 0, 0]);
    assert_eq!(o.fifo_rank_histogram_sweep_adjusted, [4, 1, 0, 0, 0, 0]);
    assert_eq!((o.rank0_share, o.rank0_share_sweep_adjusted), (Some(0.4), Some(0.8)));
    assert_eq!((o.sweeps, o.sweeps_multi_fill, o.sweep_gap_ns), (3, 1, 2_000_000));
    let w = &o.first_60s_after_open;
    assert_eq!(
        (w.checked, w.rank_violations, w.rank_violations_sweep_adjusted),
        (5, 3, 1)
    );
    assert_eq!((w.rank0_share, w.rank0_share_sweep_adjusted), (Some(0.4), Some(0.8)));
    let b = &o.rank0_share_by_bucket["09:30"];
    assert_eq!((b.checked, b.rank0, b.rank0_sweep_adjusted), (5, 2, 4));
    assert_eq!(o.rank0_share_by_bucket.len(), 1);
    let trace: Vec<(i64, u64, u64, u32, u32, u32, u64)> = sink
        .rank_trace
        .iter()
        .map(|r| {
            (
                r.ts_event,
                r.sequence,
                r.order_id,
                r.rank,
                r.rank_sweep_adjusted,
                r.level_depth,
                r.sweep_id,
            )
        })
        .collect();
    assert_eq!(
        trace,
        vec![
            (t1, 5, 3, 2, 0, 0, 1),
            (t1 + 300, 6, 2, 1, 0, 0, 1),
            (t1 + 600, 7, 1, 0, 0, 0, 1),
            (t3, 10, 5, 1, 1, 0, 2),
            (t3 + 200, 12, 4, 0, 0, 0, 3)
        ]
    );
    // Wider gap: same boundaries (level touches); no instrument row: UTC buckets.
    let mut wide = c.clone();
    wide.sweep_gap_ns = 100_000_000;
    let (_, rep) = common::run(wide, &[], &recs, vec![], vec![]);
    let o = rep.rank_oracle.unwrap();
    assert_eq!((o.sweeps, o.fifo_rank_histogram_sweep_adjusted[0]), (3, 4));
    assert!(
        o.rank0_share_by_bucket.contains_key("22:00"),
        "{:?}",
        o.rank0_share_by_bucket
    );
    assert_eq!(o.first_60s_after_open.checked, 0);
    // Zero gap: every fill its own sweep.
    let mut zero = c;
    zero.sweep_gap_ns = 0;
    let (_, rep) = common::run(zero, &[inst], &recs, vec![], vec![]);
    let o = rep.rank_oracle.unwrap();
    assert_eq!(o.fifo_rank_histogram_sweep_adjusted, o.fifo_rank_histogram);
    assert_eq!(o.sweeps, 5);
}

/// The book used by the engine is the qt_book `Book` (API smoke: queue position of a resting order).
#[test]
fn o_book_4_engine_book_queries() {
    let events = Market::generate(11, Venue::Cme, 300);
    let mut eng = Engine::new(EngineConfig::new(rules(Venue::Cme)), &[], vec![], vec![]).unwrap();
    let mut s = VecSink::default();
    let mut b = Book::new(rules(Venue::Cme));
    for e in &events {
        eng.apply(e, &mut s).unwrap();
        b.apply(e);
    }
    assert_eq!(eng.book(common::INST).unwrap().bbo(), b.bbo());
    assert_eq!(eng.book(42).map(|_| ()), None);
    let _ = (UNDEF_PRICE, TrajectoryMode::Ahead);
}
