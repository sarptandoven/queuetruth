//! Trading halts (SPEC 1.2: "shadows resting through an auction or halt are censored (`end_state =
//! censored_gap`)"; finding F2 of the 2020-01-30 atlas trace, `scripts/scale/findings/README.md`). With the trading
//! state (`Engine::set_trading_halts`, `ReplayOptions::trading_halts`) no shadow is live while its instrument is not
//! in continuous trading: a live shadow ends `censored_gap` at the halt, a shadow whose `ts_live` falls in the halt
//! never goes live and ends `censored_gap` at its `ts_live`, and everything before the halt and after the
//! resumption is unchanged.
//!
//! `f2_reproduction_*` is the Rust port of `scripts/scale/findings/repro_halt_shadow_fill.py`: the same 15-message
//! ITCH day, ingested by `qt_ingest`, replayed by `run_dataset_with`.

mod common;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use common::{Market, Scn, abs_shadow, cfg, instrument, random_shadows};
use qt_ingest::itch::{Body, Header, Message, StockDirectory, encode_file, stock};
use qt_replay::sinks::VecSink;
use qt_replay::{BboMode, Engine, EngineReport, FillRule, ReplayOptions, TradingHalt, run_dataset_with};
use qt_schema::codec::TableRow;
use qt_schema::io::read_table_as;
use qt_schema::rows::{
    EndState, FillReason, ShadowFillRow, ShadowResultRow, ShadowSpec, TrajectoryKind, TrajectoryRow,
};
use qt_schema::specs::ReplaySpec;
use qt_types::{BookSide, MboEvent, Venue};
use serde_json::json;

// ---- the F2 reproduction: an ITCH day with a halt ---------------------------------------------------------------

const NS: i64 = 1_000_000_000;
const HOUR: i64 = 3_600 * NS;
const MIN: i64 = 60 * NS;
/// 2020-01-02 00:00 America/New_York (EST, UTC-5) in UTC ns.
const MIDNIGHT: i64 = 1_577_941_200 * NS;
/// `H` state H (reason T1) and the `H` state T that resumes trading, ns after midnight.
const HALT: i64 = 10 * HOUR + MIN;
const RESUME: i64 = 10 * HOUR + 5 * MIN;
const SYMBOL: &str = "HLTX";

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-replay-halts-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn msg(kind: u8, locate: u16, ts: i64, body: Body) -> Message {
    Message {
        header: Header {
            kind,
            locate,
            tracking: 0,
            ts_ns: ts as u64,
        },
        body,
    }
}

/// The day of `repro_halt_shadow_fill.py` (prices with 4 decimals, times ns after midnight New York): HLTX trades,
/// halts at 10:01:00, a sell at 9.99 arrives and is deleted during the halt (the displayed book never crosses), and
/// trading resumes at 10:05:00 with no halt cross.
fn itch_day() -> Vec<Message> {
    let s = stock(SYMBOL);
    let add = |ts: i64, order_ref: u64, buy_sell: u8, price: u32| {
        msg(
            b'A',
            1,
            ts,
            Body::AddOrder {
                order_ref,
                buy_sell,
                shares: 100,
                stock: s,
                price,
                mpid: None,
            },
        )
    };
    let delete = |ts: i64, order_ref: u64| msg(b'D', 1, ts, Body::Delete { order_ref });
    let action = |ts: i64, state: u8, reason: [u8; 4]| {
        msg(
            b'H',
            1,
            ts,
            Body::TradingAction {
                stock: s,
                state,
                reason,
            },
        )
    };
    let system = |ts: i64, event: u8| msg(b'S', 0, ts, Body::SystemEvent { event });
    vec![
        system(4 * HOUR, b'O'),
        msg(
            b'R',
            1,
            4 * HOUR + 1,
            Body::StockDirectory(StockDirectory {
                stock: s,
                market_category: b'Q',
                financial_status: b'N',
                round_lot_size: 100,
                round_lots_only: b'N',
                issue_classification: b'C',
                issue_sub_type: *b"Z ",
                authenticity: b'P',
                short_sale_threshold: b'N',
                ipo_flag: b'N',
                luld_tier: b'1',
                etp_flag: b'N',
                etp_leverage_factor: 0,
                inverse_indicator: b'N',
            }),
        ),
        action(4 * HOUR + 2, b'T', *b"    "),
        system(9 * HOUR + 30 * MIN, b'Q'),
        add(10 * HOUR, 1, b'B', 99_800),
        add(10 * HOUR + 1_000_000, 2, b'S', 100_200),
        action(HALT, b'H', *b"T1  "),
        add(10 * HOUR + 2 * MIN, 3, b'S', 99_900),
        delete(10 * HOUR + 3 * MIN, 3),
        action(RESUME, b'T', *b"    "),
        delete(10 * HOUR + 10 * MIN, 1),
        delete(10 * HOUR + 10 * MIN + 1, 2),
        system(16 * HOUR, b'M'),
        system(20 * HOUR, b'E'),
        system(20 * HOUR + 5 * MIN, b'C'),
    ]
}

/// The non-`T` spans of each symbol's Stock Trading Action (`H`) messages: from a message with state H, P or Q to
/// the next message with state T (`i64::MAX` when none follows).
fn halts_from_itch(msgs: &[Message], midnight: i64, ids: &BTreeMap<String, u32>) -> Vec<TradingHalt> {
    let mut open: BTreeMap<u32, i64> = BTreeMap::new();
    let mut out = Vec::new();
    for m in msgs {
        let Body::TradingAction { stock, state, .. } = m.body else {
            continue;
        };
        let name = String::from_utf8_lossy(&stock).trim_end().to_owned();
        let Some(&id) = ids.get(&name) else {
            continue;
        };
        let ts = midnight + m.header.ts_ns as i64;
        match (state, open.get(&id).copied()) {
            (b'T', Some(start)) => {
                open.remove(&id);
                out.push(TradingHalt {
                    instrument_id: id,
                    ts_start: start,
                    ts_end: ts,
                });
            }
            (b'H' | b'P' | b'Q', None) => {
                open.insert(id, ts);
            }
            _ => {}
        }
    }
    out.extend(open.into_iter().map(|(id, start)| TradingHalt {
        instrument_id: id,
        ts_start: start,
        ts_end: i64::MAX,
    }));
    out
}

fn read<R: TableRow>(p: &Path) -> Vec<R> {
    let mut out = Vec::new();
    for b in read_table_as(p, R::TABLE).unwrap() {
        out.extend(R::from_batch(&b.unwrap()).unwrap());
    }
    out
}

struct Out {
    results: Vec<ShadowResultRow>,
    fills: Vec<ShadowFillRow>,
    trajectory: Vec<TrajectoryRow>,
}

impl Out {
    fn result(&self, id: &str) -> &ShadowResultRow {
        self.results.iter().find(|r| r.shadow_id == id).unwrap()
    }

    /// `ts_event` of the shadow's `end` trajectory row (its end time).
    fn end_ts(&self, id: &str) -> i64 {
        self.trajectory
            .iter()
            .find(|r| r.shadow_id == id && r.kind == TrajectoryKind::End)
            .unwrap()
            .ts_event
    }
}

fn replay(ds: &Path, shadows: &Path, out: &Path, opts: &ReplayOptions) -> Out {
    let spec: ReplaySpec = serde_json::from_value(json!({
        "dataset": {"path": ds.display().to_string()},
        "shadows": {"path": shadows.display().to_string()},
        "out": out.display().to_string(),
    }))
    .unwrap();
    let summary = run_dataset_with(&spec, opts).unwrap();
    assert_eq!(
        summary.invariants.crossed_books_after_last, 0,
        "the displayed book never crosses"
    );
    Out {
        results: read(&out.join("shadow_results.parquet")),
        fills: read(&out.join("shadow_fills.parquet")),
        trajectory: read(&out.join("queue_trajectory.parquet")),
    }
}

/// F2: `S1` (buy 100 @ 10.00 at 10:00:10, the best bid, not marketable) rests into the halt; `S2` (buy 100 @ 10.03
/// at 10:02:30) arrives during it. Without the trading state both are filled during the halt (the defect as found);
/// with it neither is: `S1` ends `censored_gap` at the halt, `S2` never goes live and ends `censored_gap` at its
/// `ts_live`.
#[test]
fn f2_reproduction_itch_day_halt_censors_shadows() {
    let d = scratch("f2");
    let src = d.join("01022020.NASDAQ_ITCH50");
    let msgs = itch_day();
    encode_file(&msgs, &src, false).unwrap();
    let ds = d.join("ds");
    let spec = json!({"format": "itch", "inputs": [{"path": src.display().to_string()}],
                      "out": ds.display().to_string(), "symbols": [SYMBOL], "venue": "nasdaq",
                      "price_exp": -9, "tick_size": "0.01", "lot_size": "1"});
    qt_ingest::ingest(&serde_json::from_value(spec).unwrap()).unwrap();
    let dataset = qt_ingest::Dataset::open(&ds).unwrap();
    let parts = dataset.partitions();
    assert_eq!(parts.len(), 1);
    let iid = parts[0].instrument_id;
    let inst = dataset.instrument(iid, parts[0].session_date).unwrap();
    assert_eq!(inst.midnight_utc_ns, MIDNIGHT);

    let cancel = MIDNIGHT + 10 * HOUR + 6 * MIN;
    let s1_live = MIDNIGHT + 10 * HOUR + 10 * NS;
    let s2_live = MIDNIGHT + 10 * HOUR + 2 * MIN + 30 * NS;
    let csv = d.join("shadows.csv");
    std::fs::write(
        &csv,
        format!(
            "shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns,ts_cancel\n\
             S1,{iid},B,10000000000,100,{s1_live},0,{cancel}\n\
             S2,{iid},B,10030000000,100,{s2_live},0,{cancel}\n"
        ),
    )
    .unwrap();

    let halts = halts_from_itch(&msgs, MIDNIGHT, &BTreeMap::from([(SYMBOL.to_owned(), iid)]));
    assert_eq!(
        halts,
        vec![TradingHalt {
            instrument_id: iid,
            ts_start: MIDNIGHT + HALT,
            ts_end: MIDNIGHT + RESUME,
        }]
    );

    // Without the trading state the engine matches continuously through the halt.
    let before = replay(&ds, &csv, &d.join("no-state"), &ReplayOptions::default());
    let got: Vec<_> = before
        .fills
        .iter()
        .map(|f| (f.shadow_id.as_str(), f.ts_event, f.fill_qty, f.fill_px, f.reason))
        .collect();
    assert_eq!(
        got,
        vec![
            (
                "S1",
                MIDNIGHT + 10 * HOUR + 2 * MIN,
                100,
                10_000_000_000,
                FillReason::Crossed
            ),
            ("S2", s2_live, 100, 9_990_000_000, FillReason::CrossedAtLive),
        ]
    );

    // With it (SPEC 1.2).
    let opts = ReplayOptions {
        trading_halts: halts,
        ..ReplayOptions::default()
    };
    let after = replay(&ds, &csv, &d.join("state"), &opts);
    assert!(after.fills.is_empty(), "{:?}", after.fills);
    let s1 = after.result("S1");
    assert_eq!((s1.end_state, s1.filled_qty), (EndState::CensoredGap, 0));
    assert_eq!(
        s1.queue_ahead_at_live,
        Some(0),
        "S1 was live (the best bid) before the halt"
    );
    assert_eq!(after.end_ts("S1"), MIDNIGHT + HALT);
    let s2 = after.result("S2");
    assert_eq!((s2.end_state, s2.filled_qty), (EndState::CensoredGap, 0));
    assert_eq!(
        (s2.ts_activated_event, s2.queue_ahead_at_live),
        (None, None),
        "S2 never live"
    );
    assert_eq!(after.end_ts("S2"), s2_live);
    let _ = std::fs::remove_dir_all(&d);
}

// ---- engine-level rules -------------------------------------------------------------------------------------------

const INST: u32 = common::INST;

fn halt(ts_start: i64, ts_end: i64) -> TradingHalt {
    TradingHalt {
        instrument_id: INST,
        ts_start,
        ts_end,
    }
}

fn engine(venue: Venue, shadows: Vec<ShadowSpec>, halts: &[TradingHalt]) -> Engine {
    let mut c = cfg(venue);
    c.bbo = BboMode::Full;
    let mut eng = Engine::new(c, &[instrument(venue)], shadows, vec![]).unwrap();
    eng.set_trading_halts(halts).unwrap();
    eng
}

fn drive(mut eng: Engine, recs: &[MboEvent]) -> (Out, VecSink, EngineReport) {
    let mut sink = VecSink::default();
    for r in recs {
        eng.apply(r, &mut sink).unwrap();
    }
    let rep = eng.finish(&mut sink).unwrap();
    let out = Out {
        results: sink.shadow_results.clone(),
        fills: sink.shadow_fills.clone(),
        trajectory: sink.trajectory.clone(),
    };
    (out, sink, rep)
}

fn run(venue: Venue, recs: &[MboEvent], shadows: Vec<ShadowSpec>, halts: &[TradingHalt]) -> Out {
    drive(engine(venue, shadows, halts), recs).0
}

fn with_cancel(mut s: ShadowSpec, ts_cancel: i64) -> ShadowSpec {
    s.ts_cancel = Some(ts_cancel);
    s
}

fn fills_of(out: &Out, id: &str) -> Vec<(i64, u64, i64, FillReason)> {
    out.fills
        .iter()
        .filter(|f| f.shadow_id == id)
        .map(|f| (f.ts_event, f.fill_qty, f.fill_px, f.reason))
        .collect()
}

/// A book 99 / 101, then during the halt [50, 100) an ask at 100 that would cross bids at 100, deleted before the
/// resumption, then the same ask again after it.
fn halt_tape() -> Vec<MboEvent> {
    Scn::new()
        .ev(10, |e| {
            e.add(1, b'B', 99, 10).add(2, b'A', 101, 10);
        })
        .ev(60, |e| {
            e.add(3, b'A', 100, 10);
        })
        .ev(70, |e| {
            e.cancel(3, 10);
        })
        .ev(110, |e| {
            e.add(4, b'A', 100, 10);
        })
        .recs
}

/// A shadow's own cancel before the halt wins (`cancelled`); a cancel at the halt instant ties and the halt wins, as
/// a feed gap does (agenda order: time, then gap, cancel).
#[test]
fn cancel_before_the_halt_wins_and_a_tie_goes_to_the_halt() {
    let a = with_cancel(abs_shadow("A", BookSide::Bid, 100, 5, 20, 0), 40);
    let b = with_cancel(abs_shadow("B", BookSide::Bid, 100, 5, 20, 0), 50);
    let out = run(Venue::Nasdaq, &halt_tape(), vec![a, b], &[halt(50, 100)]);
    assert_eq!(out.result("A").end_state, EndState::Cancelled);
    assert_eq!(out.end_ts("A"), 40);
    assert_eq!(out.result("B").end_state, EndState::CensoredGap);
    assert_eq!(out.end_ts("B"), 50);
    assert!(out.fills.is_empty());
}

/// `ts_live` in `[ts_start, ts_end)` never goes live (both ends included as written: the halt instant is halted, the
/// resumption instant trades); a shadow live at the resumption instant fills normally after it.
#[test]
fn shadows_submitted_during_the_halt_never_go_live() {
    let c = abs_shadow("C", BookSide::Bid, 100, 5, 50, 0);
    let d = abs_shadow("D", BookSide::Bid, 100, 5, 99, 0);
    let e = abs_shadow("E", BookSide::Bid, 100, 5, 100, 0);
    let out = run(Venue::Nasdaq, &halt_tape(), vec![c, d, e], &[halt(50, 100)]);
    for (id, ts) in [("C", 50), ("D", 99)] {
        let r = out.result(id);
        assert_eq!(
            (r.end_state, r.filled_qty, r.ts_activated_event),
            (EndState::CensoredGap, 0, None),
            "{id}"
        );
        assert_eq!(out.end_ts(id), ts, "{id}");
    }
    assert_eq!(fills_of(&out, "E"), vec![(110, 5, 100, FillReason::Crossed)]);
    assert_eq!(out.result("E").end_state, EndState::Filled);
    // Without the halt C and D are filled at 60 by the ask the halted venue never matched.
    let c = abs_shadow("C", BookSide::Bid, 100, 5, 50, 0);
    let free = run(Venue::Nasdaq, &halt_tape(), vec![c], &[]);
    assert_eq!(fills_of(&free, "C"), vec![(60, 5, 100, FillReason::Crossed)]);
}

/// An event at the halt instant itself still trades (the venue matches what arrives before its halt message); the
/// next event ends the live shadows `censored_gap` at the halt instant, partial fills kept.
#[test]
fn an_event_at_the_halt_instant_still_trades() {
    let f = abs_shadow("F", BookSide::Bid, 100, 5, 20, 0);
    let g = abs_shadow("G", BookSide::Bid, 100, 8, 20, 0);
    let recs = Scn::new()
        .ev(10, |e| {
            e.add(1, b'B', 99, 10).add(2, b'A', 101, 10);
        })
        .ev(50, |e| {
            e.add(3, b'A', 100, 5);
        })
        .ev(60, |e| {
            e.add(4, b'A', 100, 5);
        })
        .recs;
    let out = run(Venue::Nasdaq, &recs, vec![f, g], &[halt(50, 100)]);
    assert_eq!(fills_of(&out, "F"), vec![(50, 5, 100, FillReason::Crossed)]);
    assert_eq!(out.result("F").end_state, EndState::Filled);
    assert_eq!(fills_of(&out, "G"), vec![(50, 5, 100, FillReason::Crossed)]);
    let g = out.result("G");
    assert_eq!((g.end_state, g.filled_qty), (EndState::CensoredGap, 5));
    assert_eq!(out.end_ts("G"), 50);
}

/// A shadow live before the halt with no event of its instrument until after the resumption is activated on the
/// book it went live on (an activation fill at `ts_live` is a pre-halt fill) and ends `censored_gap` at the halt.
#[test]
fn shadow_live_before_a_halt_without_events_until_after_it() {
    let h = abs_shadow("H", BookSide::Bid, 100, 5, 20, 0);
    let x = abs_shadow("X", BookSide::Bid, 101, 4, 20, 0);
    let recs = Scn::new()
        .ev(10, |e| {
            e.add(1, b'B', 99, 10).add(2, b'A', 101, 10);
        })
        .ev(70, |e| {
            e.add(3, b'A', 100, 10);
        })
        .recs;
    let out = run(Venue::Nasdaq, &recs, vec![h, x], &[halt(50, 60)]);
    let r = out.result("H");
    assert_eq!(
        (r.end_state, r.ts_activated_event, r.queue_ahead_at_live),
        (EndState::CensoredGap, Some(70), Some(0))
    );
    assert_eq!(out.end_ts("H"), 50);
    assert!(fills_of(&out, "H").is_empty());
    // X arrived into the ask at 101 before the halt: its taker fill at ts_live stands.
    assert_eq!(fills_of(&out, "X"), vec![(20, 4, 101, FillReason::CrossedAtLive)]);
    assert_eq!(out.result("X").end_state, EndState::Filled);
}

/// End of input: a shadow whose `ts_live` falls in a halt that lasts to the end ends `censored_gap` at its
/// `ts_live` (not `open_at_end`); a shadow resting into it ends `censored_gap` at the halt.
#[test]
fn halt_to_the_end_of_the_input() {
    let p = abs_shadow("P", BookSide::Bid, 100, 5, 80, 0);
    let q = abs_shadow("Q", BookSide::Bid, 100, 5, 20, 0);
    let recs = Scn::new()
        .ev(10, |e| {
            e.add(1, b'B', 99, 10).add(2, b'A', 101, 10);
        })
        .ev(60, |e| {
            e.add(3, b'A', 100, 10);
        })
        .recs;
    let out = run(Venue::Nasdaq, &recs, vec![p, q], &[halt(50, i64::MAX)]);
    assert_eq!(out.result("P").end_state, EndState::CensoredGap);
    assert_eq!(out.end_ts("P"), 80);
    assert_eq!(out.result("Q").end_state, EndState::CensoredGap);
    assert_eq!(out.end_ts("Q"), 50);
    assert!(out.fills.is_empty());
    // Without the halt P would still be pending at the end, Q filled at 60.
    let p = abs_shadow("P", BookSide::Bid, 100, 5, 80, 0);
    let free = run(Venue::Nasdaq, &recs, vec![p], &[]);
    assert_eq!(free.result("P").end_state, EndState::OpenAtEnd);
}

/// An empty span is a configuration error.
#[test]
fn empty_halt_span_is_a_config_error() {
    let mut eng = Engine::new(cfg(Venue::Nasdaq), &[instrument(Venue::Nasdaq)], vec![], vec![]).unwrap();
    for h in [halt(50, 50), halt(50, 40)] {
        let err = eng.set_trading_halts(&[h]).expect_err("empty span");
        assert_eq!(err.code(), "QT-CONFIG-001");
    }
}

/// A twin-linked shadow (A1) whose twin is added during a halt never goes live; the twin stays on the tape.
#[test]
fn twin_added_during_a_halt() {
    let x = abs_shadow("X", BookSide::Bid, 99, 30, 20, 0);
    let recs = Scn::new()
        .ev(10, |e| {
            e.add(1, b'A', 101, 50);
        })
        .ev(20, |e| {
            e.add(5, b'B', 99, 30);
        })
        .ev(30, |e| {
            e.add(6, b'A', 99, 30);
        })
        .recs;
    let mut eng = engine(Venue::Nasdaq, vec![x], &[halt(15, 100)]);
    eng.set_excluded_orders([(INST, 5)]);
    eng.set_twin_linking(true);
    let (out, sink, rep) = drive(eng, &recs);
    let r = out.result("X");
    assert_eq!(
        (r.end_state, r.ts_activated_event, r.filled_qty),
        (EndState::CensoredGap, None, 0)
    );
    assert_eq!(out.end_ts("X"), 20);
    assert_eq!(rep.diagnostics.twins_linked, 1);
    let at20 = sink.bbo.iter().find(|b| b.ts_event == 20).unwrap();
    assert_eq!((at20.bid_px, at20.bid_sz), (Some(99), 30), "the twin stays on the tape");
}

// ---- property: only the halt changes anything ---------------------------------------------------------------------

/// Random venue-consistent streams with random shadows (abs and touch, latency with jitter, cancels, amends), both
/// fill rules, a halt in the middle third: compared with the run without it, every shadow live at or after the
/// resumption, and every shadow that ended before the halt, has the same result and fills; a shadow whose
/// `ts_live` falls in the halt never goes live (unless its own cancel came first) and ends at its `ts_live`; a
/// shadow live at the halt keeps its fills up to the halt instant and ends there `censored_gap` (or as before, when
/// it ended at that very instant); no fill falls inside the halt; halts of another instrument change nothing.
fn check_property(seed: u64, venue: Venue, rule: FillRule) -> [u32; 4] {
    let events = Market::generate(seed, venue, 1200);
    let (shadows, amends) = random_shadows(seed, &events, 60);
    let (t0, t1) = (events[0].ts_event, events[events.len() - 1].ts_event);
    let h0 = t0 + (t1 - t0) / 3 + (seed as i64 % 7) * 1_000_000;
    let h1 = h0 + (t1 - t0) / 6;
    let mut c = cfg(venue);
    c.seed = seed;
    c.latency.jitter_ns = 50_000;
    let go = |halts: &[TradingHalt]| {
        let mut eng = Engine::new(c.clone(), &[instrument(venue)], shadows.clone(), amends.clone()).unwrap();
        eng.set_fill_rule(rule);
        eng.set_trading_halts(halts).unwrap();
        drive(eng, &events).0
    };
    let free = go(&[]);
    let halted = go(&[halt(h0, h1)]);
    let other = go(&[TradingHalt {
        instrument_id: INST + 1,
        ts_start: h0,
        ts_end: h1,
    }]);
    let ctx = format!("seed {seed} {venue:?} {rule:?} halt [{h0}, {h1})");
    assert_eq!(free.results, other.results, "{ctx}");
    assert_eq!(free.fills, other.fills, "{ctx}");
    assert_eq!(free.trajectory, other.trajectory, "{ctx}");
    let mut kinds = [0u32; 4];
    for (a, b) in free.results.iter().zip(&halted.results) {
        let id = a.shadow_id.as_str();
        let ctx = format!("{ctx} {id}");
        let (fa, fb) = (fills_of(&free, id), fills_of(&halted, id));
        let end_a = free.end_ts(id);
        if a.ts_live >= h1 || end_a < h0 {
            assert_eq!(a, b, "{ctx}");
            assert_eq!(fa, fb, "{ctx}");
            kinds[0] += 1;
        } else if a.ts_live >= h0 {
            assert!(fb.is_empty(), "{ctx}");
            assert_eq!(b.ts_activated_event, None, "{ctx}");
            assert!(
                matches!(b.end_state, EndState::CensoredGap | EndState::Cancelled),
                "{ctx}: {b:?}"
            );
            if b.end_state == EndState::CensoredGap {
                assert_eq!(halted.end_ts(id), a.ts_live, "{ctx}");
            }
            kinds[1] += 1;
        } else {
            let kept: Vec<_> = fa.iter().copied().filter(|f| f.0 <= h0).collect();
            assert_eq!(fb, kept, "{ctx}");
            if b.end_state == EndState::CensoredGap && a.end_state != EndState::CensoredGap {
                assert_eq!(halted.end_ts(id), h0, "{ctx}");
                kinds[2] += 1;
            } else {
                assert_eq!(a, b, "{ctx}");
                assert!(end_a <= h0 || b.end_state == EndState::CensoredGap, "{ctx}");
                kinds[3] += 1;
            }
        }
    }
    assert!(
        halted.fills.iter().all(|f| f.ts_event <= h0 || f.ts_event >= h1),
        "{ctx}"
    );
    kinds
}

#[test]
fn only_the_halt_changes_anything_on_random_streams() {
    for venue in [Venue::Cme, Venue::Nasdaq, Venue::Generic] {
        for rule in [FillRule::Exact, FillRule::Et] {
            let mut kinds = [0u32; 4];
            for seed in 0..12 {
                for (k, n) in kinds.iter_mut().zip(check_property(seed, venue, rule)) {
                    *k += n;
                }
            }
            // Every case is exercised: unchanged, never live in the halt, censored at the halt.
            assert!(
                kinds[0] > 0 && kinds[1] > 0 && kinds[2] > 0,
                "{venue:?} {rule:?}: {kinds:?}"
            );
        }
    }
}
