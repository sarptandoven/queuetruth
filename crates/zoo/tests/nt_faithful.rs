//! Faithful NautilusTrader models against what NautilusTrader 1.231.0 does on an `OrderBookDelta` feed (the
//! `scripts/nt_crosscheck` discrepancy classes of the day-one and day-two runs), on small hand-built L2 tapes:
//! - `hidden_at_price` (example shadow `s13_20191230_000122`: a bid with 200 ahead that NT fills 542 ms after live
//!   on a hidden print at its price): a hidden (`NO_AGGRESSOR`) print at the order's price is a touch for either side;
//! - `depth_min` (example shadow `s13_20191230_000179`: a bid with 400 ahead that the zoo filled early and NT filled
//!   7.6 s after live): book size updates never shrink the queue ahead, only at-price trades do, a level delete or
//!   snapshot resets it to 0, and trade-throughs and crosses do not fill while queue ahead remains.
//!
//! Corrected mode keeps queuetruth's semantics (hidden prints ignored, `F = min(F, new)`, no queue gate).

use qt_schema::rows::{PriceSpec, ZooPredictionRow, l2flags};
use qt_types::{BookSide, Side, Ts};
use qt_zoo::models::{nt_depth_step, nt_gate_blocks};
use qt_zoo::sim::{PartitionOut, PartitionSim, Row, RowKind, ShadowInput, SimConfig};
use qt_zoo::{Mode, ModelId};

const LIVE: Ts = 15;

fn mid(s: &str) -> ModelId {
    s.parse().unwrap_or_else(|e| panic!("{s}: {e}"))
}

fn row(ts: Ts, kind: RowKind, side: Side, price: i64, qty: f64, aggressor: Side, flags: u8) -> Row {
    Row {
        ts,
        kind,
        side,
        price,
        qty,
        aggressor,
        flags,
        clear_side: None,
        clear_upto: None,
    }
}

fn depth(ts: Ts, side: Side, price: i64, qty: f64) -> Row {
    row(ts, RowKind::Depth, side, price, qty, Side::None, 0)
}

/// Displayed trade with aggressor `agg` (hits the opposite side).
fn trade(ts: Ts, agg: Side, price: i64, qty: f64) -> Row {
    row(ts, RowKind::Trade, agg.opposite(), price, qty, agg, 0)
}

/// Hidden print: no side, no aggressor (NautilusTrader `AggressorSide::NO_AGGRESSOR`).
fn hidden(ts: Ts, price: i64, qty: f64) -> Row {
    row(
        ts,
        RowKind::Trade,
        Side::None,
        price,
        qty,
        Side::None,
        l2flags::HIDDEN_TRADE,
    )
}

fn shadow(id: &str, side: BookSide, price: i64, qty: u64) -> ShadowInput {
    ShadowInput {
        shadow_id: id.into(),
        instrument_id: 7,
        side,
        price: PriceSpec::Abs(price),
        qty,
        ts_live: LIVE,
        cancel_at: None,
        amends: Vec::new(),
    }
}

fn run(models: &[&str], shadows: Vec<ShadowInput>, batches: &[Vec<Row>]) -> PartitionOut {
    let cfg = SimConfig {
        horizons: vec![10, 50, 100, 1000],
        h_max: 1000,
        tick: 1,
        lot: 1.0,
        cadence0: true,
        seed: 0,
        write_trajectory: false,
        trace: false,
    };
    let models: Vec<ModelId> = models.iter().map(|m| mid(m)).collect();
    let mut sim = PartitionSim::new(cfg, &models, shadows).unwrap_or_else(|e| panic!("{e}"));
    for b in batches {
        sim.on_batch(b);
    }
    sim.finish(None, 3, true, 5000)
}

fn get<'a>(out: &'a PartitionOut, model: &str, shadow: &str, h: i64) -> &'a ZooPredictionRow {
    let canon = mid(model).to_string();
    out.predictions
        .iter()
        .find(|r| r.model_id == canon && r.shadow_id == shadow && r.horizon_ns == h)
        .unwrap_or_else(|| panic!("{canon} {shadow} {h}"))
}

/// `hidden_at_price` (s13_20191230_000122-like): bid 100 with 200 ahead, ask 101 with 300 ahead. A hidden print of
/// 50 at 100 is a touch for the bid (NT fills it at p = 1); a hidden print of 400 at 101 is a touch for the ask, and
/// for the queue families it carries 100 past the 300 ahead.
#[test]
fn hidden_print_at_price_is_a_faithful_nt_touch() {
    let models = [
        "nt_prob_touch[mode=faithful;p=1]",
        "nt_prob_touch[mode=faithful;p=0.29]",
        "nt_prob_touch[mode=corrected;p=1]",
        "nt_queue_l2_prob_touch[mode=faithful;p=1]",
        "nt_queue_l2_prob_touch[mode=corrected;p=1]",
        "nt_queue_l2[mode=faithful]",
    ];
    let out = run(
        &models,
        vec![
            shadow("b", BookSide::Bid, 100, 100),
            shadow("a", BookSide::Ask, 101, 100),
        ],
        &[
            vec![depth(10, Side::Bid, 100, 200.0), depth(10, Side::Ask, 101, 300.0)],
            // activation batch (the view before it: 200 ahead of the bid, 300 ahead of the ask)
            vec![depth(20, Side::Bid, 99, 10.0)],
            vec![hidden(57, 100, 50.0)],
            vec![hidden(60, 101, 400.0)],
        ],
    );
    let touch1 = "nt_prob_touch[mode=faithful;p=1]";
    assert_eq!(get(&out, touch1, "b", 10).p_fill, 0.0);
    // bid: touched by the hidden print at its price (deadline 65 covers ts 57)
    assert_eq!(get(&out, touch1, "b", 50).p_fill, 1.0);
    assert_eq!(get(&out, touch1, "b", 50).model_ts_first_fill, Some(57));
    assert_eq!(get(&out, touch1, "b", 50).e_fill_qty, 100.0);
    // ask: the hidden print at 101 touches the ask side too (NT moves both book sides to the trade price)
    assert_eq!(get(&out, touch1, "a", 50).model_ts_first_fill, Some(60));
    assert_eq!(get(&out, touch1, "a", 50).p_fill, 1.0);
    // analytic p < 1: one touch each by h = 50
    let p29 = "nt_prob_touch[mode=faithful;p=0.29]";
    for s in ["a", "b"] {
        assert!((get(&out, p29, s, 50).p_fill - 0.29).abs() < 1e-12, "{s}");
        assert_eq!(get(&out, p29, s, 10).p_fill, 0.0, "{s}");
    }
    // corrected mode ignores hidden prints
    for m in [
        "nt_prob_touch[mode=corrected;p=1]",
        "nt_queue_l2_prob_touch[mode=corrected;p=1]",
    ] {
        for s in ["a", "b"] {
            assert_eq!(get(&out, m, s, 1000).p_fill, 0.0, "{m} {s}");
        }
    }
    // queue-gated touch: 50 < 200 ahead of the bid is no touch; 400 > 300 ahead of the ask is
    let gated = "nt_queue_l2_prob_touch[mode=faithful;p=1]";
    assert_eq!(get(&out, gated, "b", 1000).p_fill, 0.0);
    assert_eq!(get(&out, gated, "a", 50).p_fill, 1.0);
    assert_eq!(get(&out, gated, "a", 50).model_ts_first_fill, Some(60));
    assert_eq!(get(&out, gated, "a", 50).e_fill_qty, 100.0);
    // the deterministic queue model: the bid's queue shrinks to 150, the ask fills the excess 100
    let q = "nt_queue_l2[mode=faithful]";
    assert_eq!(get(&out, q, "b", 1000).p_fill, 0.0);
    assert_eq!(get(&out, q, "a", 50).model_ts_first_fill, Some(60));
    assert_eq!(get(&out, q, "a", 50).e_fill_qty, 100.0);
}

/// `depth_min` (s13_20191230_000179-like): bid 100 for 100 with 400 ahead. Cancels take the level to 100, two sells
/// of 80 and 70 print at 100. The zoo used to clamp the queue to the level (400 -> 100 -> 20) and fill 50 at the
/// second print; NautilusTrader keeps 400 - 80 - 70 = 250 ahead, so neither print fills, and neither does the cross
/// at ts 55 nor the trade-through at ts 60 while that queue remains. Only the level delete at ts 70 resets the queue,
/// after which the still crossed book fills the order.
#[test]
fn faithful_nt_queue_never_shrinks_on_depth_and_gates_throughs() {
    let models = [
        "nt_queue_l2[mode=faithful]",
        "nt_queue_l2[mode=corrected]",
        "nt_queue_l2_prob_touch[mode=faithful;p=0.5]",
        "nt_queue_l2_prob_touch[mode=corrected;p=0.5]",
        "nt_prob_touch[mode=faithful;p=1]",
    ];
    let out = run(
        &models,
        vec![shadow("b", BookSide::Bid, 100, 100)],
        &[
            vec![depth(10, Side::Bid, 100, 400.0), depth(10, Side::Ask, 102, 300.0)],
            vec![depth(20, Side::Bid, 99, 10.0)],
            vec![depth(30, Side::Bid, 100, 100.0)],
            vec![trade(40, Side::Ask, 100, 80.0), depth(40, Side::Bid, 100, 20.0)],
            vec![trade(50, Side::Ask, 100, 70.0), depth(50, Side::Bid, 100, 30.0)],
            // the ask drops onto the bid's price: a cross the queue gate blocks
            vec![depth(55, Side::Ask, 100, 30.0)],
            // trade-through below the bid: blocked as well while 250 remain ahead
            vec![trade(60, Side::Ask, 99, 10.0)],
            // level deleted: queue ahead reset to 0, the cross now fills
            vec![depth(70, Side::Bid, 100, 0.0)],
        ],
    );
    // faithful queue: nothing until the delete (deadline 65 at h = 50), then a full cross fill at 70
    let q = "nt_queue_l2[mode=faithful]";
    assert_eq!(get(&out, q, "b", 50).p_fill, 0.0);
    assert_eq!(get(&out, q, "b", 100).p_fill, 1.0);
    assert_eq!(get(&out, q, "b", 100).model_ts_first_fill, Some(70));
    assert_eq!(get(&out, q, "b", 100).e_fill_qty, 100.0);
    let fills: Vec<_> = out.fills.iter().filter(|f| f.model_id == mid(q).to_string()).collect();
    assert_eq!(fills.len(), 1, "{fills:?}");
    assert_eq!(fills[0].ts_event, 70);
    // corrected keeps the clamp: 400 -> 100 (depth) -> 20 (trade 80) -> 20 (depth 20) -> fills 50 of the 70 print
    let c = "nt_queue_l2[mode=corrected]";
    assert_eq!(get(&out, c, "b", 50).model_ts_first_fill, Some(50));
    assert!(get(&out, c, "b", 50).e_fill_qty >= 50.0);
    // the analytic queue-gated prediction: no excess touch, and the cross / trade-through count only once F = 0
    let g = "nt_queue_l2_prob_touch[mode=faithful;p=0.5]";
    assert_eq!(get(&out, g, "b", 50).p_fill, 0.0);
    assert_eq!(get(&out, g, "b", 100).p_fill, 1.0);
    // corrected: the clamped queue gives one excess touch at 50 (p = 0.5) and the ungated cross at 55 makes it 1
    let gc = "nt_queue_l2_prob_touch[mode=corrected;p=0.5]";
    assert_eq!(get(&out, gc, "b", 10).p_fill, 0.0);
    assert_eq!(get(&out, gc, "b", 50).p_fill, 1.0);
    // the touch model has no queue and no gate: the first displayed print at its price fills
    let t = "nt_prob_touch[mode=faithful;p=1]";
    assert_eq!(get(&out, t, "b", 50).model_ts_first_fill, Some(40));
}

/// The faithful queue step in isolation: size updates (up or down) keep `F`, a delete or a snapshot row resets it to 0;
/// corrected clamps to the new size and keeps `F` on an out-of-view delete. The gate applies to faithful queue
/// families with queue ahead only.
#[test]
fn faithful_nt_depth_step_and_gate() {
    let mut f = 400.0;
    nt_depth_step(&mut f, 100.0, 0, Mode::Faithful);
    assert_eq!(f, 400.0);
    nt_depth_step(&mut f, 900.0, 0, Mode::Faithful);
    assert_eq!(f, 400.0);
    nt_depth_step(&mut f, 0.0, l2flags::OUT_OF_VIEW, Mode::Faithful);
    assert_eq!(f, 0.0);
    let mut f = 400.0;
    nt_depth_step(&mut f, 500.0, l2flags::SNAPSHOT, Mode::Faithful);
    assert_eq!(f, 0.0);
    let mut f = 400.0;
    nt_depth_step(&mut f, 100.0, 0, Mode::Corrected);
    assert_eq!(f, 100.0);
    nt_depth_step(&mut f, 0.0, l2flags::OUT_OF_VIEW, Mode::Corrected);
    assert_eq!(f, 100.0);
    nt_depth_step(&mut f, 0.0, 0, Mode::Corrected);
    assert_eq!(f, 0.0);
    assert!(nt_gate_blocks(true, Mode::Faithful, 1.0));
    assert!(!nt_gate_blocks(true, Mode::Faithful, 0.0));
    assert!(!nt_gate_blocks(true, Mode::Corrected, 1.0));
    assert!(!nt_gate_blocks(false, Mode::Faithful, 1.0));
}
