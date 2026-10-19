//! The committed synthetic fixtures (`tests/fixtures/synth_small`, `synth_small_nasdaq`): configuration, shadow
//! sampling and the `truth/` files written next to the dataset.
//!
//! Each fixture directory is a dataset root (SPEC 3.3) plus `README.txt` and `truth/`: `shadows.parquet`
//! (qt.shadow_spec), `amends.parquet` (qt.shadow_amend), `shadow_results.parquet`, `shadow_fills.parquet`,
//! `queue_trajectory.parquet` (trajectory `ahead`), all from `try_reference_shadows` with the replay defaults
//! (zero latency model, seed 0, race window 0) and the dataset's instrument rows, and `bbo.parquet` (the generator's
//! `bbo = full` expectation, one row per event). Truth rows are in shadow input order (partition, then sample
//! order); compare by `shadow_id`.

use std::path::Path;

use qt_schema::codec::{bbo_to_batch, shadow_amends_to_batch, shadow_specs_to_batch, trajectory_to_batch};
use qt_schema::io::TableWriter;
use qt_schema::rows::{BboRow, PriceSpec, ShadowAmend, ShadowSource, ShadowSpec};
use qt_types::{BookSide, QtError, QtResult, Ts, Venue, rules, stable_seed};

use crate::calendar::{self, NS_PER_S, civil_from_days};
use crate::dataset::route;
use crate::dist::Rng;
use crate::shadows::{RefConfig, fill_liquidity, try_reference_shadows, write_shadow_fills, write_shadow_results};
use crate::{QueueMix, SynthConfig, SynthOutput, generate_sessions_with, write_dataset};

/// Seed of the committed fixtures.
pub const FIXTURE_SEED: u64 = 7;
/// Session dates of the committed fixtures (local), first day.
pub const FIXTURE_DATE: (i32, u32, u32) = (2026, 6, 15);
/// Shadows sampled per partition for `truth/`.
pub const SHADOWS_PER_PARTITION: usize = 250;

/// Generator settings of `synth_small` (CME) / `synth_small_nasdaq`: 2 instruments, about 50,000 records per
/// partition, data from 5 minutes before the open to 15 minutes after the close.
pub fn fixture_config(venue: Venue) -> SynthConfig {
    let mut c = SynthConfig::for_venue(venue, FIXTURE_SEED);
    let d = calendar::days_from_civil(FIXTURE_DATE.0, FIXTURE_DATE.1, FIXTURE_DATE.2);
    let sr = calendar::session_rules(venue);
    c.ts_start = calendar::local_midnight(venue, d) + (i64::from(sr.open_s) - 300) * NS_PER_S;
    let span_s = i64::from(sr.close_s - sr.open_s) + 300 + 900;
    c.events_per_instrument = match venue {
        Venue::Nasdaq => 30_000,
        _ => 27_500,
    };
    c.mean_gap_ns = span_s * NS_PER_S / c.events_per_instrument as i64;
    if venue == Venue::Nasdaq {
        // Sweeps of several executions share one timestamp: fewer gaps per event.
        c.mean_gap_ns = c.mean_gap_ns * 127 / 100;
    }
    c
}

fn yyyymmdd(days: i32) -> String {
    let (y, m, d) = civil_from_days(days);
    format!("{y:04}{m:02}{d:02}")
}

/// Last BBO row of `inst` with `ts_event <= ts` (rows in event order).
fn bbo_asof(rows: &[&BboRow], ts: Ts) -> Option<BboRow> {
    let i = rows.partition_point(|r| r.ts_event <= ts);
    if i == 0 { None } else { Some(*rows[i - 1]) }
}

/// Seeded shadow schedule for every partition of a synthetic output: abs and touch prices (joins, behind,
/// improving and marketable), per-shadow latency, cancels and priority-keeping amends.
pub fn sample_shadows(out: &SynthOutput, per_partition: usize, seed: u64) -> (Vec<ShadowSpec>, Vec<ShadowAmend>) {
    let mut shadows = Vec::new();
    let mut amends = Vec::new();
    let mut rows: Vec<usize> = (0..out.instruments.len()).collect();
    rows.sort_by_key(|&i| (out.instruments[i].session_date, out.instruments[i].instrument_id));
    for ri in rows {
        let inst = &out.instruments[ri];
        let bbo: Vec<&BboRow> = out
            .expected_bbo
            .iter()
            .filter(|b| {
                b.instrument_id == inst.instrument_id
                    && route(&out.instruments, b.instrument_id, b.ts_event) == Some(ri)
            })
            .collect();
        let (Some(first), Some(last)) = (bbo.first(), bbo.last()) else {
            continue;
        };
        let (t0, t1) = (first.ts_event, last.ts_event);
        let mut rng = Rng::new(stable_seed(
            seed,
            &[
                b"sample",
                &inst.instrument_id.to_le_bytes(),
                &inst.session_date.to_le_bytes(),
            ],
        ));
        let tick = inst.tick_size;
        let mean = match inst.venue {
            Venue::Nasdaq => 150,
            Venue::Cme => 4,
            _ => 20,
        };
        for k in 0..per_partition {
            let ts_submit = t0 + rng.range(0, (t1 - t0).max(0) as u64) as i64;
            let side = if rng.chance(0.5) { BookSide::Bid } else { BookSide::Ask };
            let sign = if side == BookSide::Bid { 1 } else { -1 };
            let quote = bbo_asof(&bbo, ts_submit);
            let same = quote.and_then(|q| if side == BookSide::Bid { q.bid_px } else { q.ask_px });
            let opp = quote.and_then(|q| if side == BookSide::Bid { q.ask_px } else { q.bid_px });
            let price = match same {
                Some(b) if rng.chance(0.45) => {
                    let u = rng.f64();
                    let px = if u < 0.45 {
                        b
                    } else if u < 0.65 {
                        b - sign * tick
                    } else if u < 0.80 {
                        b - sign * rng.range(2, 4) as i64 * tick
                    } else if u < 0.90 {
                        opp.unwrap_or(b + sign * tick)
                    } else {
                        b + sign * tick
                    };
                    PriceSpec::Abs(px)
                }
                _ => {
                    let w = [0.5, 0.25, 0.10, 0.15];
                    let offset_ticks = [0, -1, -2, 1][rng.weighted(&w)];
                    PriceSpec::Touch { offset_ticks }
                }
            };
            let qty = rng.size(mean);
            let latency_ns = if rng.chance(0.3) {
                0
            } else {
                rng.range(1_000, 2_000_000) as i64
            };
            let ts_cancel = rng
                .chance(0.35)
                .then(|| ts_submit + (rng.exp_ns(20.0 * NS_PER_S as f64)).max(1_000_000));
            let shadow_id = format!("s{}_{}_{k:04}", yyyymmdd(inst.session_date), inst.instrument_id);
            if qty >= 3 && rng.chance(0.1) {
                let n = 1 + rng.index(2);
                let mut ts = ts_submit;
                let mut cur = qty;
                for _ in 0..n {
                    if cur < 2 {
                        break;
                    }
                    ts += rng.exp_ns(5.0 * NS_PER_S as f64).max(1);
                    cur = rng.range(1, cur - 1);
                    amends.push(ShadowAmend {
                        shadow_id: shadow_id.clone(),
                        ts_send: ts,
                        new_qty: cur,
                    });
                }
            }
            shadows.push(ShadowSpec {
                shadow_id,
                instrument_id: inst.instrument_id,
                side,
                price,
                qty,
                ts_submit,
                latency_ns,
                ts_cancel,
                group: Some(
                    if matches!(price, PriceSpec::Touch { .. }) {
                        "touch"
                    } else {
                        "abs"
                    }
                    .to_owned(),
                ),
                source: ShadowSource::User,
                order_key: None,
            });
        }
    }
    (shadows, amends)
}

fn io_err(path: &Path, e: impl std::fmt::Display) -> QtError {
    QtError::InputMissing {
        path: path.display().to_string(),
        msg: e.to_string(),
    }
}

/// Generates and writes one synthetic fixture directory (dataset + `truth/` + `README.txt`; the caller writes
/// `MANIFEST.sha256`). Replaces `dir`. Returns the generated output.
pub fn write_synth_fixture(dir: &Path, cfg: &SynthConfig, sessions: u32, readme: &str) -> QtResult<SynthOutput> {
    if dir.exists() {
        std::fs::remove_dir_all(dir).map_err(|e| io_err(dir, e))?;
    }
    let out = generate_sessions_with(cfg, sessions, Some(QueueMix::REALISTIC));
    write_dataset(&out, dir)?;
    let (shadows, amends) = sample_shadows(&out, SHADOWS_PER_PARTITION, cfg.seed);
    let mut rc = RefConfig::new(rules(cfg.venue));
    rc.instruments = out.instruments.clone();
    rc.dq_flags = true;
    let truth = try_reference_shadows(&out.events, &rc, &shadows, &amends)?;
    let t = dir.join("truth");
    let mut w = TableWriter::create(&t.join("shadows.parquet"), "qt.shadow_spec")?;
    w.write(&shadow_specs_to_batch(&shadows))?;
    w.finish()?;
    let mut w = TableWriter::create(&t.join("amends.parquet"), "qt.shadow_amend")?;
    w.write(&shadow_amends_to_batch(&amends))?;
    w.finish()?;
    let results: Vec<_> = truth.iter().map(|x| x.0.clone()).collect();
    let fills: Vec<_> = truth.iter().flat_map(|x| x.1.iter().cloned()).collect();
    let traj: Vec<_> = truth.iter().flat_map(|x| x.2.iter().cloned()).collect();
    write_shadow_results(&t.join("shadow_results.parquet"), &results, &out.instruments)?;
    let liq = fill_liquidity(&fills);
    write_shadow_fills(&t.join("shadow_fills.parquet"), &fills, &liq)?;
    let mut w = TableWriter::create(&t.join("queue_trajectory.parquet"), "qt.queue_trajectory")?;
    w.write(&trajectory_to_batch(&traj))?;
    w.finish()?;
    let mut w = TableWriter::create(&t.join("bbo.parquet"), "qt.bbo")?;
    for chunk in out.expected_bbo.chunks(65_536) {
        w.write(&bbo_to_batch(chunk))?;
    }
    w.finish()?;
    let path = dir.join("README.txt");
    std::fs::write(&path, readme).map_err(|e| io_err(&path, e))?;
    Ok(out)
}

/// Per-partition record and event counts `(date, instrument, records, events, trades, hidden prints)`.
pub fn partition_stats(out: &SynthOutput) -> Vec<(String, u32, u64, u64, u64, u64)> {
    let mut rows: Vec<usize> = (0..out.instruments.len()).collect();
    rows.sort_by_key(|&i| (out.instruments[i].session_date, out.instruments[i].instrument_id));
    rows.into_iter()
        .map(|ri| {
            let inst = &out.instruments[ri];
            let mine = |id: u32, ts: Ts| id == inst.instrument_id && route(&out.instruments, id, ts) == Some(ri);
            let recs: Vec<_> = out
                .events
                .iter()
                .filter(|e| mine(e.instrument_id, e.ts_event))
                .collect();
            let events = recs.iter().filter(|e| e.is_last()).count() as u64;
            let trades = recs.iter().filter(|e| e.action == b'T').count() as u64;
            let hidden = recs.iter().filter(|e| e.action == b'T' && e.side == b'N').count() as u64;
            (
                calendar::date_string(inst.session_date),
                inst.instrument_id,
                recs.len() as u64,
                events,
                trades,
                hidden,
            )
        })
        .collect()
}

/// `README.txt` of a synthetic fixture: what it is, how it was generated, its layout and partition statistics.
pub fn readme(name: &str, cfg: &SynthConfig, sessions: u32, out: &SynthOutput) -> String {
    let sr = calendar::session_rules(cfg.venue);
    let mut s = format!(
        "{name}: synthetic {venue} MBO dataset generated by queuetruth-testkit (no market data).\n\n\
         Regenerate: make fixtures (cargo run --locked --release -p queuetruth-testkit --example make_fixtures);\n\
         MANIFEST.sha256 lists every file (shasum -a 256 -c MANIFEST.sha256).\n\n\
         Generator: qt_testkit::generate_sessions_with(fixture_config({venue}), {sessions}, \
         Some(QueueMix::REALISTIC)): 30% of cancels and partial cancels from the front of the queue, 25% from its \
         front half, the rest uniform, 25% of modifies queue-jumping re-quotes; seed {seed}; {n} instruments; \
         {ev} events per instrument and session; tick {tick} and base price {base} at price_exp -9; up to {lv} \
         levels per side; mean order size {ms}; weights add {pa}, cancel {pc}, reduce {pr}, modify {pm}, sweep {pt}, \
         snapshot clear {pk}, hidden print {ph}; snapshot at start {snap}; mean event gap {gap} ns.\n\
         Session: time zone {tz}, open {open} s, close {close} s after local midnight; data runs from 5 minutes \
         before the open to about 15 minutes after the close.\n\n\
         Layout: manifest.json, instruments.parquet, data_quality.json, mbo/date=*/instrument_id=*/part-00000.parquet \
         (qt.mbo, feed order), trades_source/... (qt.trades, the T records), truth/ (independent expectations: \
         shadows.parquet + amends.parquet = a seeded schedule of {sp} shadows per partition with abs and touch \
         prices, latency, cancels and amends; shadow_results / shadow_fills / queue_trajectory (ahead) from \
         qt_testkit::try_reference_shadows with the replay defaults; bbo.parquet = bbo full mode, one row per event).\n\n\
         Partitions (date, instrument, records, events, trade prints, hidden prints):\n",
        venue = cfg.venue,
        seed = cfg.seed,
        n = cfg.instruments,
        ev = cfg.events_per_instrument,
        tick = cfg.tick,
        base = cfg.base_px,
        lv = cfg.max_levels,
        ms = cfg.mean_size,
        pa = cfg.p_add,
        pc = cfg.p_cancel,
        pr = cfg.p_reduce,
        pm = cfg.p_modify,
        pt = cfg.p_trade,
        pk = cfg.p_clear,
        ph = cfg.hidden_print_rate,
        snap = cfg.snapshot_at_start,
        gap = cfg.mean_gap_ns,
        tz = sr.tz,
        open = sr.open_s,
        close = sr.close_s,
        sp = SHADOWS_PER_PARTITION,
    );
    for (d, i, r, e, t, h) in partition_stats(out) {
        s.push_str(&format!(
            "  {d} instrument {i}: {r} records, {e} events, {t} prints, {h} hidden\n"
        ));
    }
    s
}

/// Own orders per partition written to `own_orders/` (audit ruling A1).
pub const OWN_ORDERS_PER_PARTITION: usize = 50;
/// Rows of the A2 censoring simulation.
pub const CENSORING_SIM_ROWS: usize = 20_000;

/// Writes the audit inputs next to a fixture: `own_orders/` (A1: live log + exact-arm schedule + self-excluded
/// expectations) and `truth/censoring_sim_{results,predictions}.parquet` (A2). Returns README text.
pub fn write_audit_inputs(dir: &Path, out: &SynthOutput, seed: u64) -> QtResult<String> {
    let orders = crate::audit::own_orders(out, OWN_ORDERS_PER_PARTITION, seed);
    let n_events = crate::audit::write_own_orders(out, &orders, &dir.join("own_orders"))?;
    let filled = orders.iter().filter(|o| !o.fills.is_empty()).count();
    let date = out.instruments.first().map_or(0, |i| i.session_date);
    let ts0 = calendar::local_midnight(Venue::Generic, date);
    crate::audit::write_censoring_sim(
        &dir.join("truth/censoring_sim_results.parquet"),
        &dir.join("truth/censoring_sim_predictions.parquet"),
        CENSORING_SIM_ROWS,
        seed,
        ts0,
        date,
    )?;
    Ok(format!(
        "\nAudit inputs (binding rulings in docs/concepts/audit-rules.md):\n\
         - own_orders/ (A1): {} real order lives of this tape (up to {OWN_ORDERS_PER_PARTITION} per partition, {filled} \
         with fills) taken as the user's own orders: live/ is their canonical live log (venue cme, account SYNTH, \
         venue_order_id = MBO order_id, {n_events} events), schedule.parquet the exact-arm schedule (ts_submit = ack \
         = add time, ts_cancel = cancel ack), expected_results / expected_fills the reference replay with these \
         order ids excluded from the tape. Expected scorer result for sim = exact with self-exclusion: zero \
         discordance (every live fill reproduced at the same event); without exclusion the twin sits ahead of its \
         own shadow and live-only discordance appears.\n\
         - truth/censoring_sim_results.parquet + truth/censoring_sim_predictions.parquet (A2): {CENSORING_SIM_ROWS} \
         simulated orders (fill time ~ Exp(30 s) via qt_testkit::dist, shared cancel ~ U(0, 120 s) as \
         ts_cancel_effective, horizon 60 s) and a predictor perfectly calibrated for these draws (model_id \
         calibrated_oracle, censored = false). Expected: fill-rate bias about 0 over all rows; about -0.12 if \
         unfilled rows cancelled before the horizon are dropped.\n",
        orders.len()
    ))
}
