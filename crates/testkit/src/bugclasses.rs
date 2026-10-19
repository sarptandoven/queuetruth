//! Referee bug-class fixtures: the eleven classes of SPEC 5.8 plus five from the binding audit rulings (12 A3 hidden
//! execution at the price, 13 A4 execution without a print, 14 A4 off-price print, 15 A6 CME same-size modify, 16 A5
//! taker fill at activation). Each is a small hand-built market with a one-shadow schedule whose exact outcome
//! differs from what a simulator with that bug produces. Expected results and fills come from
//! `try_reference_shadows` (zero latency model, seed 0, the fixture's instrument row, `dq_flags` on).
//!
//! CME fixtures: tick 0.25 (`250_000_000` at 1e-9), touch bid 5000.00, session 2026-06-15 from 14:00:00 UTC, the
//! file starts with a flagged snapshot. The Nasdaq fixture: tick 0.01, bid 100.00, 10:00:00 New York time, plain
//! adds. Written layout per fixture: `dataset/` (SPEC 3.3), `schedule.parquet` (qt.shadow_spec),
//! `expected_results.parquet` (qt.shadow_results), `expected_fills.parquet` (qt.shadow_fills), `README.txt`.

use std::path::Path;

use qt_schema::codec::shadow_specs_to_batch;
use qt_schema::io::{FileEntry, TableWriter};
use qt_schema::json::{FixtureEntry, FixturesSummary};
use qt_schema::rows::{PriceSpec, ShadowSource, ShadowSpec};
use qt_types::{BookSide, Instrument, MboEvent, Px, QtError, QtResult, Side, Ts, ValidationStatus, Venue, rules};

use crate::calendar::{NS_PER_S, days_from_civil, local_midnight};
use crate::dataset::{expectations, write_dataset_with};
use crate::scenario::Scenario;
use crate::shadows::{RefConfig, try_reference_shadows};
use crate::{ShadowTruth, write_manifest_sha256};

const MS: i64 = 1_000_000;
const CME_TICK: Px = 250_000_000;
const CME_PX0: Px = 5_000_000_000_000;
const NQ_TICK: Px = 10_000_000;
const NQ_PX0: Px = 100_000_000_000;

/// One referee fixture in memory.
#[derive(Clone, Debug)]
pub struct BugFixture {
    /// Directory name (`NN_<class>`).
    pub name: String,
    /// What the fixture shows and which simulator bug it catches.
    pub readme: String,
    /// Venue.
    pub venue: Venue,
    /// Records.
    pub events: Vec<MboEvent>,
    /// Instrument rows.
    pub instruments: Vec<Instrument>,
    /// Shadow schedule.
    pub schedule: Vec<ShadowSpec>,
    /// Exact expectations (reference engine).
    pub expected: Vec<ShadowTruth>,
}

fn cme_t0() -> Ts {
    let d = days_from_civil(2026, 6, 15);
    local_midnight(Venue::Cme, d) + 14 * 3_600 * NS_PER_S
}

fn nq_t0() -> Ts {
    let d = days_from_civil(2026, 6, 15);
    local_midnight(Venue::Nasdaq, d) + 10 * 3_600 * NS_PER_S
}

/// CME price `k` ticks from 5000.00.
fn p(k: i64) -> Px {
    CME_PX0 + k * CME_TICK
}

/// Nasdaq price `k` ticks from 100.00.
fn q(k: i64) -> Px {
    NQ_PX0 + k * NQ_TICK
}

fn shadow(
    id: &str,
    side: BookSide,
    price: Px,
    qty: u64,
    ts_submit: Ts,
    latency_ns: i64,
    cancel: Option<Ts>,
) -> ShadowSpec {
    ShadowSpec {
        shadow_id: id.to_owned(),
        instrument_id: 1,
        side,
        price: PriceSpec::Abs(price),
        qty,
        ts_submit,
        latency_ns,
        ts_cancel: cancel,
        group: None,
        source: ShadowSource::User,
        order_key: None,
    }
}

/// Snapshot E0 of the CME fixtures: bids o1 x60, o2 x40 at 5000.00; ask o9 x500 at 5000.25.
fn cme_base(t0: Ts) -> Scenario {
    Scenario::new(Venue::Cme).snapshot(t0, |e| {
        e.add(1, BookSide::Bid, p(0), 60)
            .add(2, BookSide::Bid, p(0), 40)
            .add(9, BookSide::Ask, p(1), 500)
    })
}

fn build(name: &str, readme: &str, sc: Scenario, tick: Px, schedule: Vec<ShadowSpec>) -> QtResult<BugFixture> {
    let venue = sc.venue();
    let instruments = sc.instrument_rows(tick);
    let events = sc.build();
    let mut cfg = RefConfig::new(rules(venue));
    cfg.instruments = instruments.clone();
    cfg.dq_flags = true;
    let expected = try_reference_shadows(&events, &cfg, &schedule, &[])?;
    Ok(BugFixture {
        name: name.to_owned(),
        readme: readme.to_owned(),
        venue,
        events,
        instruments,
        schedule,
        expected,
    })
}

/// The sixteen fixtures (SPEC 5.8 classes 1-11, audit classes 12-16), in order.
pub fn all() -> QtResult<Vec<BugFixture>> {
    let t = cme_t0();
    let ms = |k: i64| t + k * MS;
    let x = |qty: u64| vec![shadow("x", BookSide::Bid, p(0), qty, ms(5), 0, None)];
    let mut v = Vec::new();

    v.push(build(
        "01_unrelated_delete_keeps_position",
        "NT #4370 class. Shadow bid 10 @ 5000.00 joins behind 100 lots (o1 60, o2 40); o3 (30) joins behind it and is \
         then deleted. Deleting an order behind the shadow must not move the shadow to the front: the following \
         executions of 20 + 40 + 35 against o1/o2 leave 5 lots ahead, so the exact engine never fills it. A simulator \
         that resets the queue position on any delete at the level fills the shadow at the first execution.",
        cme_base(t)
            .event(ms(10), |e| e.add(3, BookSide::Bid, p(0), 30))
            .event(ms(20), |e| e.cancel(3, 30))
            .event(ms(30), |e| e.trade(Side::Ask, p(0), 20).fill(1, 20))
            .event(ms(40), |e| e.trade(Side::Ask, p(0), 40).fill(1, 40))
            .event(ms(50), |e| e.trade(Side::Ask, p(0), 35).fill(2, 35)),
        CME_TICK,
        x(10),
    )?);

    v.push(build(
        "02_level_shrink_caps_queue",
        "NT #5016 class. Shadow bid 10 @ 5000.00 has 100 lots ahead (o1 60, o2 40) and o3 (30) behind. o1 cancels 60, \
         so the displayed level (70) falls below the old queue ahead (100) and the true queue ahead is 40. A trade of \
         45 then fills o2 (40) and o3 (5): the exact engine fills the shadow 5 (queue depleted). A simulator that \
         does not shrink or cap its queue estimate when the level shrinks keeps 100 ahead and never fills.",
        cme_base(t)
            .event(ms(10), |e| e.add(3, BookSide::Bid, p(0), 30))
            .event(ms(20), |e| e.cancel(1, 60))
            .event(ms(30), |e| e.trade(Side::Ask, p(0), 45).fill(2, 40).fill(3, 5)),
        CME_TICK,
        x(10),
    )?);

    v.push(build(
        "03_distinct_trades_each_consume",
        "NT #5017 class. Shadow bid 10 @ 5000.00 behind 100 lots. Two distinct trades of 50 print at the same price \
         and the same timestamp in two separate events; each consumes its own volume, so the queue ahead reaches 0. A \
         third trade of 50 (against o3 behind) fills the shadow 10. A simulator that merges identical prints counts \
         only 100 traded by the end and never fills.",
        cme_base(t)
            .event(ms(10), |e| e.add(3, BookSide::Bid, p(0), 50))
            .event(ms(20), |e| e.trade(Side::Ask, p(0), 50).fill(1, 50))
            .event(ms(20), |e| e.trade(Side::Ask, p(0), 50).fill(1, 10).fill(2, 40))
            .event(ms(30), |e| e.trade(Side::Ask, p(0), 50).fill(3, 50)),
        CME_TICK,
        x(10),
    )?);

    v.push(build(
        "04_post_trade_book_decides",
        "NT #3596 class. Bid o1 20 @ 5000.00, asks 10 @ 5000.25 and 500 @ 5000.50; shadow bid 30 @ 5000.00 is behind \
         o1. A seller takes o1 (20) and rests its remainder 15 @ 5000.00 in the same event. The fill decision uses the \
         post-trade book: the best ask is now at the shadow's price, so the exact engine fills the resting shadow 15 at \
         5000.00 as the maker (crossed, audit ruling A5) and it keeps resting with 15. A simulator that decides on the \
         pre-trade book sees no cross and no queue fill.",
        Scenario::new(Venue::Cme)
            .snapshot(t, |e| {
                e.add(1, BookSide::Bid, p(0), 20)
                    .add(8, BookSide::Ask, p(1), 10)
                    .add(9, BookSide::Ask, p(2), 500)
            })
            .event(ms(10), |e| {
                e.trade(Side::Ask, p(0), 20)
                    .fill(1, 20)
                    .add(10, BookSide::Ask, p(0), 15)
            }),
        CME_TICK,
        x(30),
    )?);

    v.push(build(
        "05_partial_fills_sum_to_cum",
        "hbt #316 class. Shadow bid 30 @ 5000.00 behind 100 lots, o3 (100) behind it. Three trades fill it 10, 15 and \
         5: cum_qty 10, 25, 30, leaves 20, 5, 0, end state filled. A simulator that reports cumulative quantities as \
         per-fill quantities (or double counts partials) shows a filled quantity above 30.",
        cme_base(t)
            .event(ms(10), |e| e.add(3, BookSide::Bid, p(0), 100))
            .event(ms(20), |e| {
                e.trade(Side::Ask, p(0), 110).fill(1, 60).fill(2, 40).fill(3, 10)
            })
            .event(ms(30), |e| e.trade(Side::Ask, p(0), 15).fill(3, 15))
            .event(ms(40), |e| e.trade(Side::Ask, p(0), 5).fill(3, 5)),
        CME_TICK,
        x(30),
    )?);

    v.push(build(
        "06_fill_equal_to_leaves_completes",
        "hbt #312 class. Shadow bid 10 @ 5000.00 is filled 4 by a sweep (leaves 6); a later trade gives exactly 6 more \
         at its price. A fill equal to the leaves quantity completes the order: filled 10, end state filled. A \
         simulator that only completes on a strictly larger fill leaves it open at 4.",
        cme_base(t)
            .event(ms(10), |e| e.add(3, BookSide::Bid, p(0), 50))
            .event(ms(20), |e| {
                e.trade(Side::Ask, p(0), 104).fill(1, 60).fill(2, 40).fill(3, 4)
            })
            .event(ms(30), |e| e.trade(Side::Ask, p(0), 6).fill(3, 6)),
        CME_TICK,
        x(10),
    )?);

    v.push(build(
        "07_size_increase_loses_priority",
        "Shadow bid 10 @ 5000.00 behind o1 (60) and o2 (40); o3 (30) behind. o1 modifies its size up to 70 and goes to \
         the back of the level. A trade of 45 fills o2 (40) and o3 (5): the exact engine fills the shadow 5. A \
         simulator that keeps o1's priority on a size increase keeps 100 ahead and does not fill.",
        cme_base(t)
            .event(ms(10), |e| e.add(3, BookSide::Bid, p(0), 30))
            .event(ms(20), |e| e.modify(1, p(0), 70))
            .event(ms(30), |e| e.trade(Side::Ask, p(0), 45).fill(2, 40).fill(3, 5)),
        CME_TICK,
        x(10),
    )?);

    v.push(build(
        "08_price_change_loses_priority",
        "Shadow bid 10 @ 5000.00 behind o1 (60) and o2 (40); o3 (30) behind. o1 moves to 4999.75 and back to 5000.00, \
         which puts it behind the shadow and o3. A trade of 45 fills o2 (40) and o3 (5): the exact engine fills the \
         shadow 5. A simulator that treats a price modify as in place keeps o1 ahead and does not fill.",
        cme_base(t)
            .event(ms(10), |e| e.add(3, BookSide::Bid, p(0), 30))
            .event(ms(20), |e| e.modify(1, p(-1), 60))
            .event(ms(30), |e| e.modify(1, p(0), 60))
            .event(ms(40), |e| e.trade(Side::Ask, p(0), 45).fill(2, 40).fill(3, 5)),
        CME_TICK,
        x(10),
    )?);

    let n = nq_t0();
    let nms = |k: i64| n + k * MS;
    v.push(build(
        "09_hidden_print_no_queue",
        "Nasdaq. Shadow bid 100 @ 100.00 behind 500 displayed shares (o1 300, o2 200). A hidden-order print of 600 \
         shares at 100.00 (side N, no F/C) consumes no displayed queue; a displayed execution of 100 against o1 leaves \
         400 ahead, so the exact engine does not fill. A simulator that counts hidden prints as queue consumption \
         fills the shadow.",
        Scenario::new(Venue::Nasdaq)
            .event(n, |e| e.add(1, BookSide::Bid, q(0), 300))
            .event(n, |e| e.add(2, BookSide::Bid, q(0), 200))
            .event(n, |e| e.add(9, BookSide::Ask, q(1), 500))
            .event(nms(10), |e| e.hidden_print(q(0), 600))
            .event(nms(20), |e| e.execute(1, 100)),
        NQ_TICK,
        vec![shadow("x", BookSide::Bid, q(0), 100, nms(5), 0, None)],
    )?);

    v.push(build(
        "10_trade_through_fills",
        "Shadow bid 10 @ 5000.00 behind 100 lots; o4 (50) rests at 4999.75. A sweep trades 100 at 5000.00 (o1, o2) \
         and 20 at 4999.75 (o4) in one event. Exactly the queue ahead traded at the price, but the sweep went through \
         the price: the exact engine fills 10 by trade-through. A queue-only simulator does not fill.",
        Scenario::new(Venue::Cme)
            .snapshot(t, |e| {
                e.add(1, BookSide::Bid, p(0), 60)
                    .add(2, BookSide::Bid, p(0), 40)
                    .add(4, BookSide::Bid, p(-1), 50)
                    .add(9, BookSide::Ask, p(1), 500)
            })
            .event(ms(10), |e| e.add(5, BookSide::Ask, p(2), 10))
            .event(ms(20), |e| {
                e.trade(Side::Ask, p(0), 100)
                    .fill(1, 60)
                    .fill(2, 40)
                    .trade(Side::Ask, p(-1), 20)
                    .fill(4, 20)
            }),
        CME_TICK,
        x(10),
    )?);

    v.push(build(
        "11_fill_before_cancel_effective_stands",
        "Latency race. Shadow bid 10 @ 5000.00 with 5 ms latency (live at +5 ms, activated at +10 ms) sends a cancel \
         at +20 ms, effective at +25 ms. A sweep at +22 ms trades through its queue (110 at 5000.00 against o1, o2 and \
         o3 behind it): the fill happens before the cancel is effective and stands (filled 10). A simulator that \
         applies the cancel at its send time reports it cancelled and unfilled.",
        cme_base(t)
            .event(ms(10), |e| e.add(3, BookSide::Bid, p(0), 30))
            .event(ms(22), |e| {
                e.trade(Side::Ask, p(0), 110).fill(1, 60).fill(2, 40).fill(3, 10)
            })
            .event(ms(30), |e| e.add(11, BookSide::Bid, p(0), 5)),
        CME_TICK,
        vec![shadow("x", BookSide::Bid, p(0), 10, ms(0), 5 * MS, Some(ms(20)))],
    )?);
    v.push(build(
        "12_hidden_execution_at_price_after_queue",
        "Nasdaq, audit ruling A3. Shadow bid 30 @ 100.00 behind o1 (60) and o2 (40); o4 (100) rests at 99.98. Both are \
         executed (E messages), so no displayed interest is left ahead. A hidden-order print of 20 at 100.00 then fills \
         the shadow 20 (displayed before hidden at a price), and a hidden print of 15 at 99.99, through the shadow's \
         price while it was the best bid, fills the last 10 by trade-through. A simulator that ignores side-N prints \
         leaves the shadow unfilled.",
        Scenario::new(Venue::Nasdaq)
            .event(n, |e| e.add(1, BookSide::Bid, q(0), 60))
            .event(n, |e| e.add(2, BookSide::Bid, q(0), 40))
            .event(n, |e| e.add(4, BookSide::Bid, q(-2), 100))
            .event(n, |e| e.add(9, BookSide::Ask, q(1), 500))
            .event(nms(10), |e| e.execute(1, 60))
            .event(nms(10), |e| e.execute(2, 40))
            .event(nms(20), |e| e.hidden_print(q(0), 20))
            .event(nms(30), |e| e.hidden_print(q(-1), 15)),
        NQ_TICK,
        vec![shadow("x", BookSide::Bid, q(0), 30, nms(5), 0, None)],
    )?);

    v.push(build(
        "13_execution_without_print",
        "Nasdaq, audit ruling A4. Shadow bid 10 @ 100.00 behind o1 (60) and o2 (40); o3 (50) joins behind it. o1 is \
         executed by a non-printable execution (F and C, no T), then o2 (40) and o3 (5) by printed executions. Queue \
         ahead follows the orders, not the prints: it is 0 after o2, and the execution of o3 behind the shadow fills it \
         5. A print-driven simulator sees only 45 traded against 100 ahead and does not fill.",
        Scenario::new(Venue::Nasdaq)
            .event(n, |e| e.add(1, BookSide::Bid, q(0), 60))
            .event(n, |e| e.add(2, BookSide::Bid, q(0), 40))
            .event(n, |e| e.add(9, BookSide::Ask, q(1), 500))
            .event(nms(10), |e| e.add(3, BookSide::Bid, q(0), 50))
            .event(nms(20), |e| e.execute_at(1, 60, q(0), false))
            .event(nms(30), |e| e.execute(2, 40))
            .event(nms(30), |e| e.execute(3, 5)),
        NQ_TICK,
        vec![shadow("x", BookSide::Bid, q(0), 10, nms(5), 0, None)],
    )?);

    v.push(build(
        "14_off_price_print_not_trade_through",
        "Nasdaq, audit ruling A4. Shadow bid 10 @ 100.00 behind o1 (60) and o2 (40). o1 is executed with price at 99.99 \
         (printed at 99.99, F at 99.99, C at its resting 100.00). The print is below the shadow's price but it is an \
         execution of an order ahead, not a trade-through: queue ahead drops to 40 and the shadow does not fill. A \
         simulator that treats any print below the bid as a trade-through fills it.",
        Scenario::new(Venue::Nasdaq)
            .event(n, |e| e.add(1, BookSide::Bid, q(0), 60))
            .event(n, |e| e.add(2, BookSide::Bid, q(0), 40))
            .event(n, |e| e.add(9, BookSide::Ask, q(1), 500))
            .event(nms(10), |e| e.add(3, BookSide::Bid, q(0), 30))
            .event(nms(20), |e| e.execute_at(1, 60, q(-1), true)),
        NQ_TICK,
        vec![shadow("x", BookSide::Bid, q(0), 10, nms(5), 0, None)],
    )?);

    v.push(build(
        "15_cme_same_size_modify_loses_priority",
        "CME, audit ruling A6. Shadow bid 10 @ 5000.00 behind o1 (60) and o2 (40); o3 (30) behind. o1 sends a modify \
         with the same price and size (iceberg refresh / attribute change), which loses priority on CME. A trade of 45 \
         fills o2 (40) and o3 (5): the exact engine fills the shadow 5. A simulator that keeps priority on any \
         non-increasing modify keeps 100 ahead and does not fill.",
        cme_base(t)
            .event(ms(10), |e| e.add(3, BookSide::Bid, p(0), 30))
            .event(ms(20), |e| e.modify(1, p(0), 60))
            .event(ms(30), |e| e.trade(Side::Ask, p(0), 45).fill(2, 40).fill(3, 5)),
        CME_TICK,
        x(10),
    )?);

    v.push(build(
        "16_crossed_at_live_takes_opposite",
        "CME, audit ruling A5. Asks 10 @ 5000.25 and 15 @ 5000.50; a marketable shadow bid 30 @ 5000.50 arrives. It \
         fills as a taker against the displayed offers: 10 at 5000.25 and 15 at 5000.50 (reason crossed_at_live, one \
         fill per price level), and the remaining 5 rest at 5000.50. A later event does not let it take the same offers \
         twice. A simulator that fills the full 30 at its own limit price as a maker fails on quantity and price.",
        Scenario::new(Venue::Cme)
            .snapshot(t, |e| {
                e.add(1, BookSide::Bid, p(0), 60).add(8, BookSide::Ask, p(1), 10).add(9, BookSide::Ask, p(2), 15)
            })
            .event(ms(10), |e| e.add(3, BookSide::Bid, p(0), 5))
            .event(ms(20), |e| e.add(4, BookSide::Bid, p(-1), 5)),
        CME_TICK,
        vec![shadow("x", BookSide::Bid, p(2), 30, ms(5), 0, None)],
    )?);
    Ok(v)
}

fn io_err(path: &Path, e: impl std::fmt::Display) -> QtError {
    QtError::InputMissing {
        path: path.display().to_string(),
        msg: e.to_string(),
    }
}

fn write_one(f: &BugFixture, dir: &Path) -> QtResult<Vec<FileEntry>> {
    std::fs::create_dir_all(dir).map_err(|e| io_err(dir, e))?;
    let synth = expectations(f.events.clone(), f.instruments.clone());
    write_dataset_with(&synth, &dir.join("dataset"), Some(ValidationStatus::FixtureOnly))?;
    let mut w = TableWriter::create(&dir.join("schedule.parquet"), "qt.shadow_spec")?;
    w.write(&shadow_specs_to_batch(&f.schedule))?;
    w.finish()?;
    let results: Vec<_> = f.expected.iter().map(|t| t.0.clone()).collect();
    let fills: Vec<_> = f.expected.iter().flat_map(|t| t.1.iter().cloned()).collect();
    crate::shadows::write_shadow_results(&dir.join("expected_results.parquet"), &results, &f.instruments)?;
    let liq = crate::shadows::fill_liquidity(&fills);
    crate::shadows::write_shadow_fills(&dir.join("expected_fills.parquet"), &fills, &liq)?;
    let readme = dir.join("README.txt");
    std::fs::write(&readme, format!("{}\n\n{}\n", f.name, f.readme)).map_err(|e| io_err(&readme, e))?;
    let mut files = Vec::new();
    for (name, sha) in list_files(dir)? {
        let path = dir.join(&name);
        let bytes = std::fs::metadata(&path).map_err(|e| io_err(&path, e))?.len();
        files.push(FileEntry {
            name,
            rows: None,
            bytes,
            sha256: sha,
        });
    }
    Ok(files)
}

fn list_files(dir: &Path) -> QtResult<Vec<(String, String)>> {
    // Reuse the manifest walker on a temporary listing without leaving a manifest behind.
    let listed = write_manifest_sha256(dir)?;
    let m = dir.join("MANIFEST.sha256");
    std::fs::remove_file(&m).map_err(|e| io_err(&m, e))?;
    Ok(listed)
}

/// Writes every fixture under `out/<name>/` plus `out/MANIFEST.sha256`; existing fixture directories are replaced.
pub fn write_fixtures(out: &Path) -> QtResult<FixturesSummary> {
    std::fs::create_dir_all(out).map_err(|e| io_err(out, e))?;
    let mut fixtures = Vec::new();
    for f in all()? {
        let dir = out.join(&f.name);
        if dir.exists() {
            std::fs::remove_dir_all(&dir).map_err(|e| io_err(&dir, e))?;
        }
        let files = write_one(&f, &dir)?;
        fixtures.push(FixtureEntry {
            name: f.name.clone(),
            path: dir.display().to_string(),
            files,
        });
    }
    write_manifest_sha256(out)?;
    Ok(FixturesSummary {
        out: out.display().to_string(),
        fixtures,
        ..FixturesSummary::default()
    })
}
