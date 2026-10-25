//! Regression tests for the queuetruth side of the oracle disagreements on real Nasdaq ITCH data
//! (docs/design/extensions/oracle-disagreements.md). Each `real_*` test replays the records of one disagreeing
//! mirror shadow, reduced to the shadow's level and the executions that decide it, with their real timestamps,
//! sequences, order references, prices and sizes (2019-12-30 and 2020-01-30, `qt.mirror_real_orders` seed 0);
//! orders that only give the book its shape (a bid or ask away from the action) are placeholders with round
//! timestamps. Each `real_*` test fails on queuetruth 0.1.1. The other tests pin the edges of each rule.
//!
//! - D1 receipt-order priority: on Nasdaq ITCH data (`Instrument.source` `itch`, `etcsv` or `dbn`) an `A` whose
//!   reference is below the largest reference added before the shadow went live was received before the shadow
//!   (an order held until the open and released then) and joins its queue ahead.
//! - D2 hidden-print resting side: a side-`N` print at the `ts_event` of an execution sweep of one resting side is
//!   the sweeping aggressor continuing into hidden interest on that side; it never fills a shadow on the other side.
//! - D3 swept midpoint prints: such a print off the tick grid still trades through a shadow on the swept side.
//!
//! D1 to D3 apply to Nasdaq ITCH data only (`order_ref_priority`); a twin-linked shadow (A1) entering at its twin's
//! `A` takes the twin's own reference as its bound.

mod common;

use qt_replay::sinks::VecSink;
use qt_replay::{Engine, EngineConfig, FillRule, trace_shadow};
use qt_schema::json::WhySubject;
use qt_schema::rows::{EndState, FillReason, PriceSpec, ShadowSource, ShadowSpec, TrajectoryKind};
use qt_types::{BookSide, Instrument, MboEvent, Venue, flags, rules};

/// 2019-12-30 local midnight (America/New_York) in UTC ns.
const MID_D1: i64 = 1_577_682_000_000_000_000;
/// 2020-01-30 local midnight in UTC ns.
const MID_D2: i64 = 1_580_360_400_000_000_000;

fn nasdaq(id: u32, midnight: i64, source: &str) -> Instrument {
    let mut i = common::instrument(Venue::Nasdaq);
    i.instrument_id = id;
    i.tick_size = 10_000_000;
    i.midnight_utc_ns = midnight;
    i.open_s = 34_200;
    i.close_s = 57_600;
    i.session_tz = "America/New_York".into();
    i.source = source.into();
    i
}

/// ITCH messages: every record of one message shares its sequence; the last record of a message carries `LAST`
/// (one message, one event, as queuetruth's ITCH reader writes them).
struct Tape {
    inst: u32,
    recs: Vec<MboEvent>,
}

impl Tape {
    fn new(inst: u32) -> Self {
        Self { inst, recs: Vec::new() }
    }

    /// One message: (action, side, price, size, order id) records.
    fn msg(mut self, ts: i64, seq: u64, recs: &[(u8, u8, i64, u64, u64)]) -> Self {
        for (k, &(action, side, price, size, order_id)) in recs.iter().enumerate() {
            self.recs.push(MboEvent {
                ts_event: ts,
                ts_recv: ts,
                instrument_id: self.inst,
                action,
                side,
                price,
                size,
                order_id,
                sequence: seq,
                flags: if k + 1 == recs.len() { flags::LAST } else { 0 },
                ..MboEvent::default()
            });
        }
        self
    }

    fn add(self, ts: i64, seq: u64, side: u8, px: i64, size: u64, id: u64) -> Self {
        self.msg(ts, seq, &[(b'A', side, px, size, id)])
    }

    /// An executed displayed order: `T` (aggressor), `F`, `C` in one message; `exec_px` is the print price.
    #[allow(clippy::too_many_arguments)]
    fn exec(self, ts: i64, seq: u64, aggr: u8, resting: u8, px: i64, exec_px: i64, size: u64, id: u64) -> Self {
        self.msg(
            ts,
            seq,
            &[
                (b'T', aggr, exec_px, size, 0),
                (b'F', resting, exec_px, size, id),
                (b'C', resting, px, size, id),
            ],
        )
    }

    fn hidden(self, ts: i64, seq: u64, px: i64, size: u64) -> Self {
        self.msg(ts, seq, &[(b'T', b'N', px, size, 0)])
    }
}

fn shadow(id: &str, inst: u32, side: BookSide, px: i64, qty: u64, ts_submit: i64) -> ShadowSpec {
    ShadowSpec {
        shadow_id: id.into(),
        instrument_id: inst,
        side,
        price: PriceSpec::Abs(px),
        qty,
        ts_submit,
        latency_ns: 0,
        ts_cancel: None,
        group: None,
        source: ShadowSource::User,
        order_key: None,
    }
}

fn replay(inst: &Instrument, recs: &[MboEvent], shadows: Vec<ShadowSpec>, rule: FillRule) -> VecSink {
    let mut eng = Engine::new(
        EngineConfig::new(rules(Venue::Nasdaq)),
        std::slice::from_ref(inst),
        shadows,
        vec![],
    )
    .expect("engine");
    eng.set_sequence_gap_detection(false);
    eng.set_fill_rule(rule);
    let mut sink = VecSink::default();
    for e in recs {
        eng.apply(e, &mut sink).expect("apply");
    }
    eng.finish(&mut sink).expect("finish");
    sink
}

fn fills(sink: &VecSink, id: &str) -> Vec<(i64, u64, FillReason)> {
    sink.shadow_fills
        .iter()
        .filter(|f| f.shadow_id == id)
        .map(|f| (f.ts_event, f.fill_qty, f.reason))
        .collect()
}

/// (ts_event, queue ahead after) of the live and event trajectory rows.
fn ahead(sink: &VecSink, id: &str) -> Vec<(i64, u64)> {
    sink.trajectory
        .iter()
        .filter(|t| t.shadow_id == id && matches!(t.kind, TrajectoryKind::Live | TrajectoryKind::Event))
        .map(|t| (t.ts_event, t.queue_ahead_after))
        .collect()
}

// ---------------------------------------------------------------- D1 receipt-order priority

const AAPL: u32 = 13;
const P28921: i64 = 289_210_000_000;
const P28920: i64 = 289_200_000_000;

/// Mirror shadow `r4768961` (2019-12-30, AAPL bid 289.21 x 100 behind order 4768961, live at 08:32:31). Order
/// 3957993 (2 shares, received before 4768961) was held until the open and released at 09:30:02.846 with its
/// original reference; Nasdaq executed it before 4768961. queuetruth 0.1.1 put it behind the shadow and filled the
/// shadow 2 at its execution (09:30:13.864334161) with 100 displayed shares still ahead; the oracle and the tape say
/// the shadow first fills by trade-through at 09:30:13.868560963.
fn r4768961() -> Tape {
    Tape::new(AAPL)
        .add(1_577_712_700_000_000_000, 40_000, b'B', P28920, 2, 4_742_249)
        .add(1_577_712_749_945_170_290, 40_746, b'B', P28921, 100, 4_768_961)
        .add(1_577_712_751_801_629_738, 40_751, b'B', 289_140_000_000, 100, 4_769_813)
        .add(1_577_716_202_846_219_230, 63_482, b'B', P28921, 2, 3_957_993)
        .exec(
            1_577_716_213_864_334_161,
            68_161,
            b'A',
            b'B',
            P28921,
            P28921,
            2,
            3_957_993,
        )
        .exec(
            1_577_716_213_864_887_599,
            68_164,
            b'A',
            b'B',
            P28921,
            P28921,
            100,
            4_768_961,
        )
        .exec(
            1_577_716_213_868_560_963,
            68_177,
            b'A',
            b'B',
            P28920,
            P28920,
            2,
            4_742_249,
        )
}

#[test]
fn real_r4768961_released_order_with_older_reference_ranks_ahead() {
    let inst = nasdaq(AAPL, MID_D1, "itch");
    let x = shadow("r4768961", AAPL, BookSide::Bid, P28921, 100, 1_577_712_749_945_170_290);
    let sink = replay(&inst, &r4768961().recs, vec![x], FillRule::Exact);
    assert_eq!(
        fills(&sink, "r4768961"),
        vec![(1_577_716_213_868_560_963, 2, FillReason::TradeThrough)]
    );
    assert_eq!(
        ahead(&sink, "r4768961"),
        vec![
            (1_577_712_749_945_170_290, 100),
            (1_577_716_202_846_219_230, 102),
            (1_577_716_213_864_334_161, 100),
            (1_577_716_213_864_887_599, 0),
            (1_577_716_213_868_560_963, 0),
        ]
    );
    let r = &sink.shadow_results[0];
    assert_eq!((r.queue_ahead_at_live, r.rank_at_live), (Some(100), Some(1)));
    assert_eq!(r.queue_ahead_at_first_fill, Some(0));
}

/// The same records from a source that does not assign ids in receipt order: arrival order is the priority, the
/// released order is behind the shadow and its execution fills it (the 0.1.1 behaviour, kept off ITCH data).
#[test]
fn receipt_order_priority_needs_an_itch_source() {
    for source in ["synthetic", "test", "csv"] {
        let inst = nasdaq(AAPL, MID_D1, source);
        let x = shadow("x", AAPL, BookSide::Bid, P28921, 100, 1_577_712_749_945_170_290);
        let sink = replay(&inst, &r4768961().recs, vec![x], FillRule::Exact);
        assert_eq!(
            fills(&sink, "x")[0],
            (1_577_716_213_864_334_161, 2, FillReason::QueueDepleted),
            "{source}"
        );
    }
    for source in ["itch", "etcsv", "dbn"] {
        let inst = nasdaq(AAPL, MID_D1, source);
        let x = shadow("x", AAPL, BookSide::Bid, P28921, 100, 1_577_712_749_945_170_290);
        let sink = replay(&inst, &r4768961().recs, vec![x], FillRule::Exact);
        assert_eq!(fills(&sink, "x")[0].0, 1_577_716_213_868_560_963, "{source}");
    }
}

/// Other venues keep arrival order whatever the source says.
#[test]
fn receipt_order_priority_is_nasdaq_only() {
    let mut inst = nasdaq(AAPL, MID_D1, "dbn");
    inst.venue = Venue::Generic;
    let x = shadow("x", AAPL, BookSide::Bid, P28921, 100, 1_577_712_749_945_170_290);
    let mut eng = Engine::new(
        EngineConfig::new(rules(Venue::Generic)),
        std::slice::from_ref(&inst),
        vec![x],
        vec![],
    )
    .unwrap();
    eng.set_sequence_gap_detection(false);
    let mut sink = VecSink::default();
    for e in &r4768961().recs {
        eng.apply(e, &mut sink).unwrap();
    }
    eng.finish(&mut sink).unwrap();
    assert_eq!(
        fills(&sink, "x")[0],
        (1_577_716_213_864_334_161, 2, FillReason::QueueDepleted)
    );
}

/// The et rule (prototype parity, SPEC 5.2.5 as written) is unchanged: the released order is behind.
#[test]
fn receipt_order_priority_leaves_the_et_rule_alone() {
    let inst = nasdaq(AAPL, MID_D1, "itch");
    let x = shadow("x", AAPL, BookSide::Bid, P28921, 100, 1_577_712_749_945_170_290);
    let et = replay(&inst, &r4768961().recs, vec![x.clone()], FillRule::Et);
    let mut synth = inst.clone();
    synth.source = "synthetic".into();
    let et_synth = replay(&synth, &r4768961().recs, vec![x], FillRule::Et);
    assert_eq!(et.shadow_fills, et_synth.shadow_fills);
    assert_eq!(et.trajectory, et_synth.trajectory);
}

/// An order received after the shadow went live (reference above every reference added before it) stays behind,
/// even when it arrives out of reference order itself; only references below the shadow's bound move ahead.
#[test]
fn released_order_received_after_the_shadow_stays_behind() {
    let inst = nasdaq(AAPL, MID_D1, "itch");
    let recs = Tape::new(AAPL)
        .add(1_000, 1, b'B', P28921, 10, 100)
        .add(2_000, 2, b'A', 289_300_000_000, 10, 200)
        // the shadow goes live at the start of this event: bound = 200
        .add(3_000, 3, b'B', P28921, 5, 300)
        // released out of order: 250 > 200 (received after the shadow), 150 < 200 (before it)
        .add(4_000, 4, b'B', P28921, 7, 250)
        .add(4_000, 5, b'B', P28921, 3, 150)
        .recs;
    let x = shadow("x", AAPL, BookSide::Bid, P28921, 1, 2_500);
    let sink = replay(&inst, &recs, vec![x], FillRule::Exact);
    assert_eq!(ahead(&sink, "x"), vec![(2_500, 10), (4_000, 13)]);
    assert_eq!(sink.shadow_results[0].end_state, EndState::OpenAtEnd);
}

/// A released order ahead of the shadow keeps being exact queue ahead: a partial cancel shrinks it, its execution
/// does not fill the shadow, and only executions behind it do.
#[test]
fn released_order_ahead_cancels_and_executes_as_queue_ahead() {
    let inst = nasdaq(AAPL, MID_D1, "itch");
    let recs = Tape::new(AAPL)
        .add(1_000, 1, b'B', P28921, 10, 100)
        .add(2_000, 2, b'B', 289_000_000_000, 1, 200)
        .add(3_000, 3, b'B', P28921, 6, 50)
        .msg(4_000, 4, &[(b'C', b'B', P28921, 2, 50)])
        .exec(5_000, 5, b'A', b'B', P28921, P28921, 4, 50)
        .add(6_000, 6, b'B', P28921, 9, 300)
        .exec(7_000, 7, b'A', b'B', P28921, P28921, 3, 300)
        .recs;
    let x = shadow("x", AAPL, BookSide::Bid, P28921, 5, 2_500);
    let sink = replay(&inst, &recs, vec![x], FillRule::Exact);
    assert_eq!(
        ahead(&sink, "x"),
        vec![(2_500, 10), (3_000, 16), (4_000, 14), (5_000, 10), (7_000, 10)]
    );
    assert_eq!(fills(&sink, "x"), vec![(7_000, 3, FillReason::QueueDepleted)]);
}

/// A shadow alone at an empty level still ranks behind an older reference released there.
#[test]
fn released_order_at_an_empty_level_is_ahead_of_a_lone_shadow() {
    let inst = nasdaq(AAPL, MID_D1, "itch");
    let recs = Tape::new(AAPL)
        .add(1_000, 1, b'B', 289_000_000_000, 10, 500)
        .add(2_000, 2, b'B', 289_000_000_000, 1, 501)
        .add(3_000, 3, b'B', P28921, 4, 120)
        .exec(4_000, 4, b'A', b'B', P28921, P28921, 4, 120)
        .recs;
    let x = shadow("x", AAPL, BookSide::Bid, P28921, 5, 1_500);
    let sink = replay(&inst, &recs, vec![x], FillRule::Exact);
    assert_eq!(ahead(&sink, "x"), vec![(1_500, 0), (3_000, 4), (4_000, 0)]);
    assert!(fills(&sink, "x").is_empty());
}

// ---------------------------------------------------------------- D2 / D3 hidden prints after a sweep

const TSLA_D1: u32 = 7992;
const P41522: i64 = 415_220_000_000;

/// Mirror shadow `r247161108` (2019-12-30, TSLA ask 415.22 x 100 behind order 247161108). Its queue ahead
/// executes price-improved at 415.21; bids at 415.22 then cross it (13 and 1 shares). At 14:37:28.153975743 a seller
/// hits the 1-share bid at 415.22 and, in the next message of the same timestamp, 99 shares of hidden bids there
/// (`T N 415.22 99`). queuetruth 0.1.1 inferred the hidden order's side from the shadow and filled the ask 86 with
/// that seller's volume; the oracle (resting side from the open sweep) and the tape say the shadow fills its last 86
/// at 14:37:35.460539122, when a buyer executes order 247198060 behind it.
fn r247161108() -> Tape {
    Tape::new(TSLA_D1)
        .add(1_577_734_647_341_820_821, 4_364_280, b'A', P41522, 100, 247_161_108)
        .add(
            1_577_734_647_341_821_890,
            4_364_281,
            b'A',
            415_290_000_000,
            53,
            247_161_112,
        )
        .exec(
            1_577_734_647_341_855_461,
            4_364_284,
            b'B',
            b'A',
            P41522,
            415_210_000_000,
            100,
            247_161_108,
        )
        .add(1_577_734_647_492_272_117, 4_364_303, b'B', P41522, 13, 247_162_500)
        .msg(
            1_577_734_647_872_993_079,
            4_364_371,
            &[(b'C', b'B', P41522, 13, 247_162_500)],
        )
        .add(1_577_734_648_009_949_672, 4_364_377, b'B', P41522, 1, 247_166_024)
        .exec(
            1_577_734_648_153_975_743,
            4_364_386,
            b'A',
            b'B',
            P41522,
            P41522,
            1,
            247_166_024,
        )
        .hidden(1_577_734_648_153_975_743, 4_364_387, P41522, 99)
        .add(1_577_734_651_782_998_180, 4_365_063, b'A', P41522, 100, 247_198_060)
        .exec(
            1_577_734_655_460_539_122,
            4_365_566,
            b'B',
            b'A',
            P41522,
            P41522,
            87,
            247_198_060,
        )
}

#[test]
fn real_r247161108_hidden_print_of_a_seller_sweep_never_fills_an_ask() {
    let inst = nasdaq(TSLA_D1, MID_D1, "itch");
    let x = shadow(
        "r247161108",
        TSLA_D1,
        BookSide::Ask,
        P41522,
        100,
        1_577_734_647_341_820_821,
    );
    let sink = replay(&inst, &r247161108().recs, vec![x], FillRule::Exact);
    assert_eq!(
        fills(&sink, "r247161108"),
        vec![
            (1_577_734_647_492_272_117, 13, FillReason::Crossed),
            (1_577_734_648_009_949_672, 1, FillReason::Crossed),
            (1_577_734_655_460_539_122, 86, FillReason::QueueDepleted),
        ]
    );
}

const TSLA_D1_P413: i64 = 413_000_000_000;

/// Mirror shadow `r110073796` (2019-12-30, TSLA bid 413.00, filled down to 71 shares by then). At
/// 11:57:05.215234703 a buyer lifts the displayed ask at 412.96 (29) and, in the next message of the same timestamp,
/// 71 shares of hidden asks at 413.00. queuetruth 0.1.1 filled the bid's last 71 shares with that buyer's volume;
/// the oracle and the tape fill it later. Reduced: the bid shadow of 71 has no queue ahead.
#[test]
fn real_r110073796_hidden_print_of_a_buyer_sweep_never_fills_a_bid() {
    let inst = nasdaq(TSLA_D1, MID_D1, "itch");
    let recs = Tape::new(TSLA_D1)
        .add(
            1_577_721_400_000_000_000,
            2_060_000,
            b'A',
            412_960_000_000,
            29,
            110_343_072,
        )
        .add(
            1_577_721_400_000_000_001,
            2_060_001,
            b'A',
            413_010_000_000,
            100,
            110_343_073,
        )
        .exec(
            1_577_721_425_215_234_703,
            2_068_009,
            b'B',
            b'A',
            412_960_000_000,
            412_960_000_000,
            29,
            110_343_072,
        )
        .hidden(1_577_721_425_215_234_703, 2_068_010, TSLA_D1_P413, 71)
        .recs;
    let x = shadow(
        "r110073796",
        TSLA_D1,
        BookSide::Bid,
        TSLA_D1_P413,
        71,
        1_577_721_410_000_000_000,
    );
    let sink = replay(&inst, &recs, vec![x], FillRule::Exact);
    // The ask at 412.96 crossed the bid: taker fill 29 at live; the buyer's hidden ask print never fills the bid.
    assert_eq!(
        fills(&sink, "r110073796"),
        vec![(1_577_721_410_000_000_000, 29, FillReason::CrossedAtLive)]
    );
}

const P100: i64 = 100_000_000_000;

/// The sweep of the shadow's own side: a seller hits the displayed bid at 100 then hidden bids at 100 in the next
/// message of the same timestamp; a bid shadow at 100 with no queue ahead left is ahead of hidden interest and fills.
#[test]
fn hidden_print_continuing_a_sweep_of_the_shadows_side_fills_it() {
    let inst = nasdaq(AAPL, MID_D1, "itch");
    let recs = Tape::new(AAPL)
        .add(1_000, 1, b'B', P100, 5, 10)
        .add(1_000, 2, b'A', 100_050_000_000, 5, 11)
        .exec(3_000, 3, b'A', b'B', P100, P100, 5, 10)
        .hidden(3_000, 4, P100, 8)
        .recs;
    let x = shadow("x", AAPL, BookSide::Bid, P100, 6, 2_000);
    let sink = replay(&inst, &recs, vec![x], FillRule::Exact);
    assert_eq!(fills(&sink, "x"), vec![(3_000, 6, FillReason::QueueDepleted)]);
}

/// What closes a sweep: a later timestamp, an add on the swept side, a cancel there that is not an execution's
/// removal, or a sweep of both sides at that timestamp. Then the print's side is unknown and inferred from the
/// shadow, as before (an ask shadow at the print price fills).
#[test]
fn hidden_print_without_an_open_sweep_infers_the_side_from_the_shadow() {
    let inst = nasdaq(AAPL, MID_D1, "itch");
    let base = || {
        Tape::new(AAPL)
            .add(1_000, 1, b'B', P100, 5, 10)
            .add(1_000, 2, b'B', 99_900_000_000, 5, 11)
            .add(1_000, 3, b'A', 100_100_000_000, 5, 12)
            .add(1_000, 4, b'A', 100_200_000_000, 5, 13)
            .exec(3_000, 5, b'A', b'B', P100, P100, 5, 10)
    };
    let cases: Vec<(&str, Tape)> = vec![
        ("later ts", base().hidden(3_001, 6, P100, 8)),
        (
            "add on the swept side",
            base()
                .add(3_000, 6, b'B', 99_800_000_000, 1, 14)
                .hidden(3_000, 7, P100, 8),
        ),
        (
            "pure cancel on the swept side",
            base()
                .msg(3_000, 6, &[(b'C', b'B', 99_900_000_000, 1, 11)])
                .hidden(3_000, 7, P100, 8),
        ),
        (
            "both sides swept",
            base()
                .exec(3_000, 6, b'B', b'A', 100_100_000_000, 100_100_000_000, 5, 12)
                .hidden(3_000, 7, P100, 8),
        ),
    ];
    // `y` asks 20 at the print price: the bid at 100 crosses it at live (5, `crossed_at_live`), then the print
    // reaches it at its price with nothing ahead (`queue_depleted`) unless a bid-side sweep is open. `x` asks
    // above the print and is never reached by it.
    let depleted = |sink: &VecSink, id: &str| -> u64 {
        fills(sink, id)
            .iter()
            .filter(|f| f.2 == FillReason::QueueDepleted)
            .map(|f| f.1)
            .sum()
    };
    let run = |t: Tape| {
        let x = shadow("x", AAPL, BookSide::Ask, P100 + 50_000_000, 6, 2_000);
        let y = shadow("y", AAPL, BookSide::Ask, P100, 20, 2_000);
        replay(&inst, &t.recs, vec![x, y], FillRule::Exact)
    };
    for (name, t) in cases {
        let sink = run(t);
        assert_eq!(fills(&sink, "y")[0], (2_000, 5, FillReason::CrossedAtLive), "{name}");
        assert_eq!(depleted(&sink, "y"), 8, "{name}");
        assert_eq!(depleted(&sink, "x"), 0, "{name}");
    }
    // Control: the open sweep of the bid side (only the executed order's own removal since) keeps the print off
    // the asks.
    let sink = run(base().hidden(3_000, 6, P100, 8));
    assert_eq!(fills(&sink, "y"), vec![(2_000, 5, FillReason::CrossedAtLive)]);
    assert_eq!(depleted(&sink, "x"), 0);
}

const TSLA_D2: u32 = 8000;

/// The records of mirror shadow `r105372220` (below).
fn r105372220() -> Vec<MboEvent> {
    Tape::new(TSLA_D2)
        .add(
            1_580_397_900_000_000_000,
            2_266_000,
            b'B',
            644_000_000_000,
            3,
            105_290_000,
        )
        .add(
            1_580_397_900_000_000_001,
            2_266_001,
            b'A',
            644_270_000_000,
            2,
            105_298_096,
        )
        .add(
            1_580_397_900_000_000_002,
            2_266_002,
            b'A',
            644_580_000_000,
            15,
            105_298_097,
        )
        .exec(
            1_580_397_911_383_254_342,
            2_266_748,
            b'B',
            b'A',
            644_270_000_000,
            644_270_000_000,
            2,
            105_298_096,
        )
        .hidden(1_580_397_911_383_254_342, 2_266_749, 644_315_000_000, 4)
        .recs
}

/// Mirror shadow `r105372220` (2020-01-30, TSLA ask 644.10 x 400, nothing ahead of it). At 15:25:11.383254342
/// a buyer lifts the displayed ask at 644.27 (2) and, in the next message of the same timestamp, 4 shares of a hidden
/// midpoint ask at 644.315. The buyer's limit reached 644.315, through the shadow's 644.10, so the shadow trades
/// those 4 too (as in the oracle); 0.1.1 skipped every print off the tick grid.
#[test]
fn real_r105372220_swept_midpoint_print_trades_through() {
    let inst = nasdaq(TSLA_D2, MID_D2, "itch");
    let recs = r105372220();
    let x = shadow(
        "r105372220",
        TSLA_D2,
        BookSide::Ask,
        644_100_000_000,
        400,
        1_580_397_908_311_763_442,
    );
    let sink = replay(&inst, &recs, vec![x], FillRule::Exact);
    assert_eq!(
        fills(&sink, "r105372220"),
        vec![
            (1_580_397_911_383_254_342, 2, FillReason::TradeThrough),
            (1_580_397_911_383_254_342, 4, FillReason::TradeThrough),
        ]
    );
    // Without the sweep (a later timestamp) the off-grid print stays unusable.
    let mut recs2 = recs.clone();
    let last = recs2.len() - 1;
    recs2[last].ts_event += 1;
    recs2[last].ts_recv += 1;
    let x = shadow(
        "r105372220",
        TSLA_D2,
        BookSide::Ask,
        644_100_000_000,
        400,
        1_580_397_908_311_763_442,
    );
    let sink = replay(&inst, &recs2, vec![x], FillRule::Exact);
    assert_eq!(
        fills(&sink, "r105372220"),
        vec![(1_580_397_911_383_254_342, 2, FillReason::TradeThrough)]
    );
}

/// The `why` tracer labels a released order that joins the queue ahead (D1) as ahead, at its own `A`.
#[test]
fn why_trace_marks_a_released_order_ahead() {
    let inst = nasdaq(AAPL, MID_D1, "itch");
    let x = shadow("r4768961", AAPL, BookSide::Bid, P28921, 100, 1_577_712_749_945_170_290);
    let recs = r4768961().recs;
    let t = trace_shadow(
        recs.iter().map(|e| Ok(*e)),
        EngineConfig::new(rules(Venue::Nasdaq)),
        std::slice::from_ref(&inst),
        x,
        vec![],
        5000,
        WhySubject {
            shadow_id: Some("r4768961".into()),
            order_key: None,
        },
    )
    .unwrap();
    let add = t
        .events
        .iter()
        .find(|e| e.order_id == 3_957_993 && e.action == "A")
        .expect("the release is traced");
    assert!(add.is_ahead);
    assert_eq!(add.queue_ahead_after, 102);
    // A later order at the level (4769813 is at another price; 4768961 was resting at activation) stays behind.
    let mut synth = inst.clone();
    synth.source = "synthetic".into();
    let x = shadow("r4768961", AAPL, BookSide::Bid, P28921, 100, 1_577_712_749_945_170_290);
    let t = trace_shadow(
        recs.iter().map(|e| Ok(*e)),
        EngineConfig::new(rules(Venue::Nasdaq)),
        std::slice::from_ref(&synth),
        x,
        vec![],
        5000,
        WhySubject {
            shadow_id: Some("r4768961".into()),
            order_key: None,
        },
    )
    .unwrap();
    let add = t
        .events
        .iter()
        .find(|e| e.order_id == 3_957_993 && e.action == "A")
        .expect("the add is traced");
    assert!(!add.is_ahead);
    assert_eq!(add.queue_ahead_after, 100);
}

/// D2 and D3 are validated on Nasdaq ITCH data only: elsewhere (here a Nasdaq-rules dataset from another source)
/// a hidden print's side is inferred from the shadow and an off-grid print is never used, as in 0.1.1.
#[test]
fn hidden_print_sweep_rules_need_an_itch_source() {
    let print_ts = 1_577_734_648_153_975_743;
    let run = |source: &str| {
        let inst = nasdaq(TSLA_D1, MID_D1, source);
        let x = shadow("x", TSLA_D1, BookSide::Ask, P41522, 100, 1_577_734_647_341_820_821);
        replay(&inst, &r247161108().recs, vec![x], FillRule::Exact)
    };
    let itch = run("itch");
    assert!(fills(&itch, "x").iter().all(|f| f.0 != print_ts));
    let synth = run("synthetic");
    assert!(
        fills(&synth, "x").iter().any(|f| f.0 == print_ts),
        "{:?}",
        fills(&synth, "x")
    );
    let inst = nasdaq(TSLA_D2, MID_D2, "parquet");
    let x = shadow(
        "x",
        TSLA_D2,
        BookSide::Ask,
        644_100_000_000,
        400,
        1_580_397_908_311_763_442,
    );
    let sink = replay(&inst, &r105372220(), vec![x], FillRule::Exact);
    assert_eq!(
        fills(&sink, "x"),
        vec![(1_580_397_911_383_254_342, 2, FillReason::TradeThrough)]
    );
}

/// A twin-linked shadow (A1, the user's own order excluded from the tape) runs with the given excluded ids.
fn replay_twin(inst: &Instrument, recs: &[MboEvent], x: ShadowSpec, excluded: &[(u32, u64)]) -> VecSink {
    let mut eng = Engine::new(
        EngineConfig::new(rules(Venue::Nasdaq)),
        std::slice::from_ref(inst),
        vec![x],
        vec![],
    )
    .unwrap();
    eng.set_sequence_gap_detection(false);
    eng.set_excluded_orders(excluded.iter().copied());
    eng.set_twin_linking(true);
    let mut sink = VecSink::default();
    for e in recs {
        eng.apply(e, &mut sink).unwrap();
    }
    eng.finish(&mut sink).unwrap();
    sink
}

/// A twin that is itself a released held order (reference 300, below the 500 already added) stands at its own
/// reference: the resting 400 (received after it, displayed before its release) is behind the shadow, a later
/// release with reference 350 stays behind, one with 250 joins ahead.
#[test]
fn twin_released_from_the_hold_ranks_at_its_own_reference() {
    let inst = nasdaq(AAPL, MID_D1, "itch");
    let recs = Tape::new(AAPL)
        .add(1_000, 1, b'B', P28921, 10, 100)
        .add(1_100, 2, b'B', P28921, 5, 400)
        .add(1_200, 3, b'A', 289_300_000_000, 1, 500)
        .add(3_000, 4, b'B', P28921, 4, 300)
        .add(4_000, 5, b'B', P28921, 7, 350)
        .add(5_000, 6, b'B', P28921, 2, 250)
        .recs;
    let x = shadow("x", AAPL, BookSide::Bid, P28921, 4, 3_000);
    let sink = replay_twin(&inst, &recs, x, &[(AAPL, 300)]);
    let r = &sink.shadow_results[0];
    assert_eq!((r.queue_ahead_at_live, r.rank_at_live), (Some(10), Some(1)));
    assert_eq!(ahead(&sink, "x"), vec![(3_000, 10), (5_000, 12)]);
    // Off ITCH data the twin takes the tail of the level, behind 400.
    let mut synth = inst.clone();
    synth.source = "synthetic".into();
    let x = shadow("x", AAPL, BookSide::Bid, P28921, 4, 3_000);
    let sink = replay_twin(&synth, &recs, x, &[(AAPL, 300)]);
    assert_eq!(sink.shadow_results[0].queue_ahead_at_live, Some(15));
}

/// A twin received after every reference added so far (the usual case) bounds the shadow at its own reference: a
/// release received before the twin (reference 400, between the bound at activation and the twin's 600) is ahead.
#[test]
fn twin_bound_covers_references_received_before_it() {
    let inst = nasdaq(AAPL, MID_D1, "itch");
    let recs = Tape::new(AAPL)
        .add(1_000, 1, b'B', P28921, 10, 100)
        .add(1_200, 2, b'A', 289_300_000_000, 1, 200)
        .add(3_000, 3, b'B', P28921, 4, 600)
        .add(4_000, 4, b'B', P28921, 3, 400)
        .add(5_000, 5, b'B', P28921, 2, 700)
        .recs;
    let x = shadow("x", AAPL, BookSide::Bid, P28921, 4, 3_000);
    let sink = replay_twin(&inst, &recs, x, &[(AAPL, 600)]);
    assert_eq!(ahead(&sink, "x"), vec![(3_000, 10), (4_000, 13)]);
}
