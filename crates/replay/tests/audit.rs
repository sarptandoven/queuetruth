//! Audit rulings A1-A6 (binding): exclusion of the live order's own MBO twin, planned (exogenous)
//! shadow ends, hidden prints on Nasdaq, order-level queue-ahead accounting, taker fills on crosses, CME priority on
//! unchanged modifies. Each test contrasts the default `Exact` rule with SPEC-as-written (`Et`) where they differ.

mod common;

use common::{Scn, abs_shadow, cfg, instrument};
use qt_replay::sinks::VecSink;
use qt_replay::{Engine, EngineReport, FillRule, HiddenPrintPolicy};
use qt_schema::rows::{FillReason, ShadowFillRow, ShadowSpec, TrajectoryKind};
use qt_types::{BookSide, Venue};

const B: u8 = b'B';
const A: u8 = b'A';

fn base() -> Scn {
    Scn::new()
        .ev(10, |e| {
            e.add(1, B, 100, 60).add(2, B, 100, 40).add(9, A, 101, 500);
        })
        .ev(20, |e| {
            e.add(3, B, 100, 30);
        })
}

fn x(qty: u64) -> ShadowSpec {
    abs_shadow("X", BookSide::Bid, 100, qty, 15, 0)
}

fn go(venue: Venue, s: &Scn, shadows: Vec<ShadowSpec>, setup: impl FnOnce(&mut Engine)) -> (VecSink, EngineReport) {
    let mut eng = Engine::new(cfg(venue), &[instrument(venue)], shadows, vec![]).unwrap();
    setup(&mut eng);
    let mut sink = VecSink::default();
    for r in &s.recs {
        eng.apply(r, &mut sink).unwrap();
    }
    let rep = eng.finish(&mut sink).unwrap();
    (sink, rep)
}

fn fills(sink: &VecSink) -> Vec<(i64, u64, FillReason, i64)> {
    sink.shadow_fills
        .iter()
        .map(|f: &ShadowFillRow| (f.ts_event, f.fill_qty, f.reason, f.fill_px))
        .collect()
}

/// A1: the live order's own twin (o2, behind o1) is removed from the tape. The shadow queues behind o1 only, and
/// the twin's executions fill it once the queue ahead is gone (the shadow takes the twin's place).
#[test]
fn audit_a1_exclude_own_twin() {
    let s = base().ev(30, |e| {
        e.trade(A, 100, 100).exec(1, 60).exec(2, 40);
    });
    let (plain, _) = go(Venue::Cme, &s, vec![x(60)], |_| {});
    assert!(
        plain.shadow_fills.is_empty(),
        "self-twin: the shadow sits behind its own order"
    );
    let (sink, rep) = go(Venue::Cme, &s, vec![x(60)], |e| {
        e.set_excluded_orders([(1, 2), (1, 999)])
    });
    assert_eq!(fills(&sink), vec![(30, 40, FillReason::QueueDepleted, 100)]);
    assert_eq!(sink.shadow_results[0].queue_ahead_at_live, Some(60));
    let d = &rep.diagnostics;
    assert_eq!(
        (d.excluded_requested, d.excluded_matched, d.excluded_records),
        (2, 1, 3)
    );
    // The twin never reached the book: bid 100 holds o3 only at the end, and the tape keeps the print.
    assert_eq!(
        sink.bbo.last().map(|b| (b.bid_px, b.bid_sz, b.bid_ct)),
        Some((Some(100), 30, 1))
    );
    assert_eq!(sink.trades.len(), 1);
    // Twin volume while queue ahead remains is not credited.
    let early = base().ev(30, |e| {
        e.trade(A, 100, 40).exec(2, 40);
    });
    let (sink, _) = go(Venue::Cme, &early, vec![x(60)], |e| e.set_excluded_orders([(1, 2)]));
    assert!(sink.shadow_fills.is_empty());
}

/// A2: planned ends are the earlier of the cancel effective time and the session close, independent of outcome.
#[test]
fn audit_a2_planned_end_is_exogenous() {
    let s = base().ev(40, |e| {
        e.trade(A, 100, 130).exec(1, 60).exec(2, 40).exec(3, 30);
    });
    let mut c = abs_shadow("C", BookSide::Bid, 100, 10, 15, 0);
    c.ts_cancel = Some(35);
    let mut lat = abs_shadow("L", BookSide::Bid, 100, 10, 15, 4);
    lat.ts_cancel = Some(36);
    let mut eng = Engine::new(cfg(Venue::Cme), &[instrument(Venue::Cme)], vec![x(10), c, lat], vec![]).unwrap();
    let planned_before = eng.shadow_planned_ends();
    let mut sink = VecSink::default();
    for r in &s.recs {
        eng.apply(r, &mut sink).unwrap();
    }
    eng.finish(&mut sink).unwrap();
    let close = 86_400 * 1_000_000_000;
    let p: Vec<(Option<i64>, &str)> = planned_before.iter().map(|p| (p.planned_end, p.kind)).collect();
    // L: cancel sent at 36 is effective at 36 + latency 4.
    assert_eq!(
        p,
        vec![
            (Some(close), "session_close"),
            (Some(35), "cancel"),
            (Some(40), "cancel")
        ]
    );
    // X filled at 40 and C was cancelled at 35: the planned ends were fixed before either outcome.
    assert_eq!(sink.shadow_results[0].filled_qty, 10);
    assert_eq!(sink.shadow_results[1].end_state, qt_schema::rows::EndState::Cancelled);
}

/// A3 / A3d: Nasdaq hidden prints. Usable only with no displayed interest at or through the print price on either
/// side: at the shadow's price they fill it (S17); with displayed orders at the price, ahead (S12) or behind (S17b),
/// they are declined and counted; usable prints strictly through the shadow's price are trade-throughs.
#[test]
fn audit_a3_hidden_prints_on_nasdaq() {
    // A3d (oracle ruling): a hidden print is usable only when no displayed bid rests at or above it and no displayed
    // ask at or below it. S17: ahead 100 executed by E messages, nothing displayed left at 100, then T N 100 50.
    let e0 = || {
        Scn::new().ev(10, |e| {
            e.add(1, B, 100, 60).add(2, B, 100, 40).add(9, A, 101, 500);
        })
    };
    let s17 = e0()
        .ev(30, |e| {
            e.trade(A, 100, 100).exec(1, 60).exec(2, 40);
        })
        .ev(40, |e| {
            e.trade(b'N', 100, 50);
        });
    let (sink, rep) = go(Venue::Nasdaq, &s17, vec![x(10)], |_| {});
    assert_eq!(fills(&sink), vec![(40, 10, FillReason::QueueDepleted, 100)]);
    assert_eq!(rep.diagnostics.hidden_print_fills, 1);
    let (ign, _) = go(Venue::Nasdaq, &s17, vec![x(10)], |e| {
        e.set_hidden_print_policy(HiddenPrintPolicy::Ignore)
    });
    assert!(ign.shadow_fills.is_empty());
    // S17b: o3 rests displayed behind the shadow at 100: the print is declined (it would have traded with o3).
    let s17b = base()
        .ev(30, |e| {
            e.trade(A, 100, 100).exec(1, 60).exec(2, 40);
        })
        .ev(40, |e| {
            e.trade(b'N', 100, 50);
        });
    let (sink, rep) = go(Venue::Nasdaq, &s17b, vec![x(10)], |_| {});
    assert!(sink.shadow_fills.is_empty());
    assert_eq!(rep.diagnostics.hidden_at_px_with_display_ahead, 1);
    // S12: displayed queue ahead remains: declined.
    let s12 = base().ev(30, |e| {
        e.trade(b'N', 100, 500);
    });
    let (sink, rep) = go(Venue::Nasdaq, &s12, vec![x(10)], |_| {});
    assert!(sink.shadow_fills.is_empty());
    assert_eq!(rep.diagnostics.hidden_at_px_with_display_ahead, 1);
    // Through: a hidden print at 99 once no displayed bid is at or above 99 trades through the bid shadow at 100;
    // with displayed bids at 100 it is declined.
    let thr = e0()
        .ev(30, |e| {
            e.trade(A, 100, 100).exec(1, 60).exec(2, 40);
        })
        .ev(40, |e| {
            e.trade(b'N', 99, 20);
        });
    let (sink, _) = go(Venue::Nasdaq, &thr, vec![x(10)], |_| {});
    assert_eq!(fills(&sink), vec![(40, 10, FillReason::TradeThrough, 100)]);
    let (beh, _) = go(Venue::Nasdaq, &thr, vec![x(10)], |e| {
        e.set_hidden_print_policy(HiddenPrintPolicy::Behind)
    });
    assert!(beh.shadow_fills.is_empty());
    let blocked = base().ev(30, |e| {
        e.trade(b'N', 99, 20);
    });
    let (sink, _) = go(Venue::Nasdaq, &blocked, vec![x(10)], |_| {});
    assert!(sink.shadow_fills.is_empty());
    // CME (no hidden orders): a side-N print is an implied trade and takes the print path, never the hidden one.
    let (cme, rep) = go(Venue::Cme, &s17, vec![x(10)], |_| {});
    assert_eq!(rep.diagnostics.hidden_print_fills, 0);
    assert!(cme.shadow_fills.is_empty());
}

/// A4: queue ahead from the ahead orders' own records. S18: a non-printable execution of an ahead order (F + C, no
/// T) still shrinks the queue, so a later at-price execution behind fills 5. An off-price print (T at 99 for an
/// order resting at 100) never counts as a trade-through.
#[test]
fn audit_a4_order_level_queue_ahead() {
    let s18 = base()
        .ev(30, |e| {
            e.exec(1, 60);
        })
        .ev(40, |e| {
            e.trade(A, 100, 45).exec(2, 40).exec(3, 5);
        });
    let (sink, _) = go(Venue::Cme, &s18, vec![x(10)], |_| {});
    assert_eq!(fills(&sink), vec![(40, 5, FillReason::QueueDepleted, 100)]);
    let ev: Vec<(i64, u64, u64)> = sink
        .trajectory
        .iter()
        .filter(|t| t.kind == TrajectoryKind::Event)
        .map(|t| (t.ts_event, t.queue_ahead_before, t.queue_ahead_after))
        .collect();
    assert_eq!(ev, vec![(30, 100, 40), (40, 40, 0)]);
    let (et, _) = go(Venue::Cme, &s18, vec![x(10)], |e| e.set_fill_rule(FillRule::Et));
    assert!(et.shadow_fills.is_empty(), "et: the stale queue never depletes");

    let off = base().ev(30, |e| {
        e.trade(A, 99, 60).exec(1, 60);
    });
    let (sink, _) = go(Venue::Cme, &off, vec![x(10)], |_| {});
    assert!(sink.shadow_fills.is_empty());
    assert_eq!(sink.shadow_results[0].filled_qty, 0);
    let (et, _) = go(Venue::Cme, &off, vec![x(10)], |e| e.set_fill_rule(FillRule::Et));
    assert_eq!(fills(&et), vec![(30, 10, FillReason::TradeThrough, 100)]);
    // An execution of an order resting strictly through the price fills as a trade-through.
    let thr = Scn::new()
        .ev(10, |e| {
            e.add(1, B, 100, 60).add(4, B, 99, 50).add(9, A, 101, 500);
        })
        .ev(30, |e| {
            e.trade(A, 100, 60).exec(1, 60).trade(A, 99, 20).exec(4, 20);
        });
    let (sink, _) = go(Venue::Cme, &thr, vec![x(10)], |_| {});
    assert_eq!(fills(&sink), vec![(30, 10, FillReason::TradeThrough, 100)]);
}

/// A5 with E36(a) and the E48 refinement: a resting shadow crossed by an opposite arrival is the maker at
/// its own price, for the opposite size left after the same-side volume with priority (better price, or the queue
/// ahead at the shadow's price), never taking the same opposite size twice; a shadow arriving into a crossing book
/// (`crossed_at_live`) is a taker walking the opposite levels at their prices.
#[test]
fn audit_a5_taker_walk() {
    let s = base()
        .ev(30, |e| {
            e.add(6, A, 100, 105);
        })
        .ev(40, |e| {
            e.add(7, B, 98, 1);
        });
    let (sink, rep) = go(Venue::Cme, &s, vec![x(10)], |_| {});
    // o1 + o2 (100 ahead) take the first 100 of the crossing 105-lot ask; the resting shadow takes the other 5 as
    // the maker at its own price (A5 as refined by E48), once: nothing again at 40 while the same ask rests.
    assert_eq!(fills(&sink), vec![(30, 5, FillReason::Crossed, 100)]);
    // `crossed_unfilled` counts once per cross (review m14): this cross gave a fill when it started.
    assert_eq!((rep.diagnostics.taker_fills, rep.diagnostics.crossed_unfilled), (0, 0));
    assert_eq!(sink.fill_liquidity, vec![qt_schema::rows::Liquidity::Maker]);
    assert_eq!(qt_replay::fill_liquidity(FillReason::Crossed), "maker");
    assert_eq!(qt_replay::fill_liquidity(FillReason::CrossedAtLive), "taker");
    // A thin cross goes to the orders ahead; once they leave, the shadow takes it, then a later new offer.
    let thin = base()
        .ev(30, |e| {
            e.add(6, A, 100, 3);
        })
        .ev(40, |e| {
            e.cancel(1, 60).cancel(2, 40);
        })
        .ev(50, |e| {
            e.add(8, A, 99, 4);
        });
    let (sink, _) = go(Venue::Cme, &thin, vec![x(10)], |_| {});
    assert_eq!(
        fills(&sink),
        vec![(40, 3, FillReason::Crossed, 100), (50, 4, FillReason::Crossed, 100)]
    );
    // Marketable at activation: walks 101 then 102.
    let book = Scn::new().ev(10, |e| {
        e.add(1, A, 101, 5).add(2, A, 102, 10).add(3, B, 99, 4);
    });
    let s = book.ev(20, |e| {
        e.add(4, B, 98, 1);
    });
    let (sink, _) = go(
        Venue::Cme,
        &s,
        vec![abs_shadow("X", BookSide::Bid, 102, 12, 15, 0)],
        |_| {},
    );
    assert_eq!(
        fills(&sink),
        vec![
            (15, 5, FillReason::CrossedAtLive, 101),
            (15, 7, FillReason::CrossedAtLive, 102)
        ]
    );
    let r = &sink.shadow_results[0];
    assert_eq!((r.filled_qty, r.end_state), (12, qt_schema::rows::EndState::Filled));
}

/// A6: on CME an `M` with unchanged price and size loses priority: the order leaves the queue ahead.
#[test]
fn audit_a6_cme_unchanged_modify_loses_priority() {
    let s = base().ev(30, |e| {
        e.modify(1, B, 100, 60);
    });
    let (sink, _) = go(Venue::Cme, &s, vec![x(10)], |_| {});
    let row = sink
        .trajectory
        .iter()
        .find(|t| t.kind == TrajectoryKind::Event)
        .expect("row");
    assert_eq!(
        (
            row.queue_ahead_before,
            row.queue_ahead_after,
            row.rank_after,
            row.add_behind_qty
        ),
        (100, 40, 1, 60)
    );
    let (generic, _) = go(Venue::Generic, &s, vec![x(10)], |_| {});
    assert!(generic.trajectory.iter().all(|t| t.kind != TrajectoryKind::Event));
}
