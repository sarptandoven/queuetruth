//! Hand-built scenario fixtures for E36, oracle rulings A3d / A4b / A6b and audit minors 15 / 24 (docs/design/spec.md,
//! docs/concepts/audit-rules.md), shared by the reference tests here and the engine tests in `qt_replay`. Each case is a
//! stream (scenario DSL, tick 1, UTC-day session), a shadow schedule, and the expected fills, end states and race
//! flags written by hand from the specification text (not from either engine):
//! - `a_locked_book_priority_before_shadow` (36a): in a locked / crossed book the real same-side orders with priority
//!   (better price, or the queue ahead at the shadow's price) take the opposite size first; the resting shadow gets
//!   the rest as the maker at its own price, never the same opposite size twice; `crossed_at_live` allocates the same
//!   way but walks the opposite levels as a taker at their prices.
//! - `c_hidden_print_display_rule` (oracle ruling A3d, superseding the quote-rule reading of 36c, Nasdaq): a hidden
//!   print is usable only when no displayed bid rests at or above it and no displayed ask at or below it; it then
//!   fills shadows at its price and trades through shadows better than it, on either side.
//! - `d_duplicate_add_requeues_nasdaq` / `d_duplicate_add_requeues_cme` (36d): a duplicate add outside a flagged
//!   snapshot moves the order to the back of the queue; `d_snapshot_readd_keeps_queue_cme`: inside a flagged
//!   snapshot it keeps its place.
//! - `f_price_improved_execution_behind` (oracle ruling A4b): an execution at a price other than the order's
//!   displayed price never fills the shadow (neither as volume behind it nor as a trade-through).
//! - `g_cme_modify_above_post_fill_remainder` (oracle ruling A6b): on CME an `M` above the order's post-fill
//!   remainder loses priority.
//! - `e15_cancel_side_race` (minor 15): a fill within the race window before the cancel's effective time flags
//!   `ambiguous_race`.
//! - `e24_removal_after_unpaired_fill` (minor 24): a `C` one event after an unpaired `F` counts in
//!   `data_quality.source_specific.removal_after_unpaired_fill`.

use qt_schema::rows::{EndState, FillReason, PriceSpec, ShadowSource, ShadowSpec};
use qt_types::{BookSide, Dur, MboEvent, Px, Qty, Side, Ts, Venue, flags};

use crate::scenario::Scenario;

/// Expected outcome of one shadow.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Expect {
    /// Shadow id.
    pub shadow_id: &'static str,
    /// Fills as (ts_event, fill_qty, reason, fill_px), in order.
    pub fills: Vec<(Ts, Qty, FillReason, Px)>,
    /// End state.
    pub end_state: EndState,
    /// `ambiguous_race` (with the case's race window).
    pub ambiguous_race: bool,
}

/// One scenario fixture.
#[derive(Clone, Debug)]
pub struct Case {
    /// Name.
    pub name: &'static str,
    /// Venue.
    pub venue: Venue,
    /// Records (instrument 1, tick 1).
    pub events: Vec<MboEvent>,
    /// Shadow schedule.
    pub shadows: Vec<ShadowSpec>,
    /// `race_window_ns` of the replay.
    pub race_window_ns: Dur,
    /// Expected outcome per shadow, schedule order.
    pub expected: Vec<Expect>,
    /// Expected `removal_after_unpaired_fill` (0 = absent from `source_specific`).
    pub removal_after_unpaired_fill: u64,
}

const B: BookSide = BookSide::Bid;
const A: BookSide = BookSide::Ask;

/// A shadow at an absolute price with zero latency.
pub fn shadow(id: &str, side: BookSide, px: Px, qty: Qty, ts_submit: Ts, ts_cancel: Option<Ts>) -> ShadowSpec {
    ShadowSpec {
        shadow_id: id.into(),
        instrument_id: 1,
        side,
        price: PriceSpec::Abs(px),
        qty,
        ts_submit,
        latency_ns: 0,
        ts_cancel,
        group: None,
        source: ShadowSource::User,
        order_key: None,
    }
}

fn expect(shadow_id: &'static str, fills: Vec<(Ts, Qty, FillReason, Px)>, end_state: EndState) -> Expect {
    Expect {
        shadow_id,
        fills,
        end_state,
        ambiguous_race: false,
    }
}

fn case(
    name: &'static str,
    venue: Venue,
    events: Vec<MboEvent>,
    shadows: Vec<ShadowSpec>,
    expected: Vec<Expect>,
) -> Case {
    Case {
        name,
        venue,
        events,
        shadows,
        race_window_ns: 0,
        expected,
        removal_after_unpaired_fill: 0,
    }
}

/// Ruling 36a. Bids o1 60 + o2 40 @ 100, ask o9 @ 102. X (bid 100 x20) joins behind o1/o2, o3 (30) behind X.
/// - ts 30, ask o6 110 @ 100 locks the book: o1/o2 (100 ahead) take the first 100, X takes 10 at 100.
/// - ts 40, bid o7 5 @ 101 (better price) joins: priority 105 covers o6 up to X's earlier 10: nothing.
/// - ts 50, ask o8 8 @ 99: the priority volume takes o8 and 97 of o6; X takes 3 more of o6 at 100.
/// - Y (bid 100 x10, live at 40 behind o1/o2/o3 = 130 ahead): 130 >= the 110 offered, never fills.
/// - Z (bid 101 x10, live at 50 behind o7): crossed at live, priority 5, takes 10 of o6 at 100 (price improvement).
fn a_locked() -> Case {
    let events = Scenario::new(Venue::Cme)
        .event(10, |e| e.add(1, B, 100, 60).add(2, B, 100, 40).add(9, A, 102, 500))
        .event(20, |e| e.add(3, B, 100, 30))
        .event(30, |e| e.add(6, A, 100, 110))
        .event(40, |e| e.add(7, B, 101, 5))
        .event(50, |e| e.add(8, A, 99, 8))
        .build();
    case(
        "a_locked_book_priority_before_shadow",
        Venue::Cme,
        events,
        vec![
            shadow("X", B, 100, 20, 15, None),
            shadow("Y", B, 100, 10, 35, None),
            shadow("Z", B, 101, 10, 45, None),
        ],
        vec![
            expect(
                "X",
                vec![(30, 10, FillReason::Crossed, 100), (50, 3, FillReason::Crossed, 100)],
                EndState::OpenAtEnd,
            ),
            expect("Y", vec![], EndState::OpenAtEnd),
            expect("Z", vec![(45, 10, FillReason::CrossedAtLive, 100)], EndState::Filled),
        ],
    )
}

/// Oracle ruling A3d (Nasdaq; supersedes the quote-rule reading of E36(c)). Bid o1 50 @ 100 (o2 20 joins
/// behind XB), ask o9 50 @ 103; XB bid 100, XI bid 101, XJ ask 102, XA ask 103. A hidden print is usable only when no
/// displayed bid is at or above it and no displayed ask at or below it; it reaches shadows at or better than it:
/// - ts 30, 4 @ 101 (inside): XI fills 4 at its price. ts 40, 6 @ 102: XJ fills 6.
/// - ts 50, 7 @ 100: displayed bids rest at 100 (o1 ahead of XB, o2 behind): declined.
/// - ts 60, 5 @ 99: displayed bids at 100 are above it: declined (no trade-through for XB or XI).
/// - ts 70 o1 and o2 cancel; ts 80, 3 @ 100: XB fills 3 at its price, XI is traded through for 3.
/// - ts 90, 2 @ 103: the displayed ask o9 rests there: declined for XA.
fn c_display_rule() -> Case {
    let events = Scenario::new(Venue::Nasdaq)
        .event(10, |e| e.add(1, B, 100, 50).add(9, A, 103, 50))
        .event(20, |e| e.add(2, B, 100, 20))
        .event(30, |e| e.hidden_print(101, 4))
        .event(40, |e| e.hidden_print(102, 6))
        .event(50, |e| e.hidden_print(100, 7))
        .event(60, |e| e.hidden_print(99, 5))
        .event(70, |e| e.cancel(1, 50).cancel(2, 20))
        .event(80, |e| e.hidden_print(100, 3))
        .event(90, |e| e.hidden_print(103, 2))
        .build();
    case(
        "c_hidden_print_display_rule",
        Venue::Nasdaq,
        events,
        vec![
            shadow("XB", B, 100, 10, 15, None),
            shadow("XI", B, 101, 10, 15, None),
            shadow("XJ", A, 102, 10, 15, None),
            shadow("XA", A, 103, 10, 15, None),
        ],
        vec![
            expect("XB", vec![(80, 3, FillReason::QueueDepleted, 100)], EndState::OpenAtEnd),
            expect(
                "XI",
                vec![
                    (30, 4, FillReason::QueueDepleted, 101),
                    (80, 3, FillReason::TradeThrough, 101),
                ],
                EndState::OpenAtEnd,
            ),
            expect("XJ", vec![(40, 6, FillReason::QueueDepleted, 102)], EndState::OpenAtEnd),
            expect("XA", vec![], EndState::OpenAtEnd),
        ],
    )
}

/// Ruling 36d (Nasdaq). X (bid 100 x10) is behind o1 (60). A duplicate add of o1 at 99 (no snapshot flags on
/// Nasdaq) follows the book: o1 leaves X's queue, no displayed bid is left at 100, and the hidden print of 15 at 100
/// fills X (A3d).
fn d_dup_nasdaq() -> Case {
    let events = Scenario::new(Venue::Nasdaq)
        .event(10, |e| e.add(1, B, 100, 60).add(9, A, 101, 500))
        .event(20, |e| e.add(5, A, 102, 10))
        .event(30, |e| e.add(1, B, 99, 60))
        .event(40, |e| e.hidden_print(100, 15))
        .build();
    case(
        "d_duplicate_add_requeues_nasdaq",
        Venue::Nasdaq,
        events,
        vec![shadow("X", B, 100, 10, 15, None)],
        vec![expect(
            "X",
            vec![(40, 10, FillReason::QueueDepleted, 100)],
            EndState::Filled,
        )],
    )
}

/// Oracle ruling A4b (Nasdaq). X (bid 100 x10) behind o1/o2, o3 (30) behind X, o4 (50) at 99. o3 is executed 10 at
/// 101 (price-improved): not volume reaching X. o4 is executed 20 at 100 (improved to X's price): not a
/// trade-through either. X never fills.
fn f_off_display() -> Case {
    let events = Scenario::new(Venue::Nasdaq)
        .event(10, |e| {
            e.add(1, B, 100, 60)
                .add(2, B, 100, 40)
                .add(4, B, 99, 50)
                .add(9, A, 102, 500)
        })
        .event(20, |e| e.add(3, B, 100, 30))
        .event(30, |e| e.execute_at(3, 10, 101, true))
        .event(40, |e| e.execute_at(4, 20, 100, true))
        .build();
    case(
        "f_price_improved_execution_behind",
        Venue::Nasdaq,
        events,
        vec![shadow("X", B, 100, 10, 15, None)],
        vec![expect("X", vec![], EndState::OpenAtEnd)],
    )
}

/// Oracle ruling A6b (CME). X (bid 100 x10) behind o1 (60) and o2 (40). At ts 30 o1 is executed 20 and then
/// modified to 50, above its post-fill remainder of 40: it loses priority. o2 is executed at 40, so X has no queue
/// ahead when an ask of 30 locks the book at 50: X takes 10 at 100. (With priority kept, o1's 50 would take the 30.)
fn g_modify_above_remainder() -> Case {
    let events = Scenario::new(Venue::Cme)
        .event(10, |e| e.add(1, B, 100, 60).add(2, B, 100, 40).add(9, A, 102, 500))
        .event(20, |e| e.add(3, B, 100, 30))
        .event(30, |e| e.trade(Side::Ask, 100, 20).fill_only(1, 20).modify(1, 100, 50))
        .event(40, |e| e.trade(Side::Ask, 100, 40).fill(2, 40))
        .event(50, |e| e.add(6, A, 100, 30))
        .build();
    case(
        "g_cme_modify_above_post_fill_remainder",
        Venue::Cme,
        events,
        vec![shadow("X", B, 100, 10, 15, None)],
        vec![expect("X", vec![(50, 10, FillReason::Crossed, 100)], EndState::Filled)],
    )
}

/// Ruling 36d (CME): X (bid 100 x10) behind o1 (60), o2 (20) behind X; o1 is re-added at ts 30, then an ask of 30
/// locks the book at 100. Outside a snapshot o1 went to the back: no priority ahead of X, which takes 10 at 100.
/// With the re-add flagged `SNAPSHOT` (CME flags its snapshots) o1 keeps its place and takes the 30 first.
fn d_dup_cme(snapshot: bool) -> Case {
    let events = Scenario::new(Venue::Cme)
        .event(10, |e| e.add(1, B, 100, 60).add(9, A, 102, 500))
        .event(20, |e| e.add(2, B, 100, 20))
        .event(30, |e| {
            let e = e.add(1, B, 100, 60);
            if snapshot { e.flag(flags::SNAPSHOT) } else { e }
        })
        .event(40, |e| e.add(6, A, 100, 30))
        .build();
    let (name, want) = if snapshot {
        (
            "d_snapshot_readd_keeps_queue_cme",
            expect("X", vec![], EndState::OpenAtEnd),
        )
    } else {
        (
            "d_duplicate_add_requeues_cme",
            expect("X", vec![(40, 10, FillReason::Crossed, 100)], EndState::Filled),
        )
    };
    case(
        name,
        Venue::Cme,
        events,
        vec![shadow("X", B, 100, 10, 15, None)],
        vec![want],
    )
}

/// Audit minor 15 (race window 3 ns). X and W (bid 100 x10, live at 20 behind o1/o2) fill 10 at 40 from the
/// execution of o3 behind them. X's cancel is effective at 42 (40 > 42 - 3: ambiguous); W's at 50 (not).
fn e15_race() -> Case {
    let events = Scenario::new(Venue::Cme)
        .event(10, |e| e.add(1, B, 100, 60).add(2, B, 100, 40).add(9, A, 101, 500))
        .event(20, |e| e.add(3, B, 100, 30))
        .event(40, |e| e.trade(Side::Ask, 100, 10).fill(3, 10))
        .build();
    let mut c = case(
        "e15_cancel_side_race",
        Venue::Cme,
        events,
        vec![
            shadow("X", B, 100, 10, 15, Some(42)),
            shadow("W", B, 100, 10, 15, Some(50)),
        ],
        vec![
            Expect {
                ambiguous_race: true,
                ..expect("X", vec![(40, 10, FillReason::QueueDepleted, 100)], EndState::Filled)
            },
            expect("W", vec![(40, 10, FillReason::QueueDepleted, 100)], EndState::Filled),
        ],
    );
    c.race_window_ns = 3;
    c
}

/// Audit minor 24 (CME pairs within the event): o1's `F` at 30 has no removal in its event; the `C` at 31 counts.
/// o2's `F` + `C` in one event pair and do not count.
fn e24_late_removal() -> Case {
    let events = Scenario::new(Venue::Cme)
        .event(10, |e| e.add(1, B, 100, 60).add(2, B, 100, 40).add(9, A, 101, 500))
        .event(30, |e| e.trade(Side::Ask, 100, 60).fill_only(1, 60))
        .event(31, |e| e.cancel(1, 60))
        .event(40, |e| e.trade(Side::Ask, 100, 40).fill(2, 40))
        .build();
    let mut c = case("e24_removal_after_unpaired_fill", Venue::Cme, events, vec![], vec![]);
    c.removal_after_unpaired_fill = 1;
    c
}

/// Every case, in a fixed order.
pub fn ruling36_cases() -> Vec<Case> {
    vec![
        a_locked(),
        c_display_rule(),
        d_dup_nasdaq(),
        d_dup_cme(false),
        d_dup_cme(true),
        f_off_display(),
        g_modify_above_remainder(),
        e15_race(),
        e24_late_removal(),
    ]
}
