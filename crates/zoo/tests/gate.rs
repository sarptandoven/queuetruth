//! Gate tests of the zoo (need `qt_book`'s engine, `qt_ingest`'s L2Builder / dataset and `qt_testkit`'s generator):
//! O-ZOO-4 (`hbt_l3_fifo` corrected equals the exact engine), O-ZOO-5 (threads 1 vs 4), O-ZOO-6 (bracket),
//! runner alignment with the exact arm (activation, prices, queue at live, censoring), the L2-only dataset mode,
//! `run_zoo` on the committed synthetic fixture, and the schema 1.1 options (amends, `exclude_order_ids`, the
//! `liquidity` columns, QT-CONFIG-001 for what an L2-only dataset cannot honour).

use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};

use qt_schema::rows::{
    EndState, L2Row, Liquidity, PriceSpec, ShadowAmend, ShadowSpec, ZooFillReason, ZooPredictionRow,
};
use qt_schema::specs::{DatasetRef, ExcludeOrderId, FileRef, L2Config, ZooSpec};
use qt_testkit::{SynthConfig, SynthOutput, generate};
use qt_types::{BookSide, LatencyModel, MboEvent, UNDEF_PRICE, Venue, rules};
use qt_zoo::runner::{Input, PartResult, StreamOptions, run_stream, shadow_inputs_with_amends};
use qt_zoo::sim::SimConfig;
use qt_zoo::{Exchange, Mode, ModelId};

const H_MAX: i64 = 300_000_000_000;

fn synth(venue: Venue, seed: u64, events: u64) -> SynthOutput {
    let mut c = SynthConfig::for_venue(venue, seed);
    c.instruments = 1;
    c.events_per_instrument = events;
    generate(&c)
}

fn shadows(out: &SynthOutput, n: usize, seed: u64) -> Vec<ShadowSpec> {
    let (s, _amends) = qt_testkit::fixtures::sample_shadows(out, n, seed);
    s
}

fn run(out: &SynthOutput, venue: Venue, specs: &[ShadowSpec], models: &[ModelId], horizons: Vec<i64>) -> PartResult {
    run_with(out, venue, specs, models, horizons, StreamOptions::default())
}

/// `run` with schema 1.1 stream options (amends, excluded real orders).
fn run_with(
    out: &SynthOutput,
    venue: Venue,
    specs: &[ShadowSpec],
    models: &[ModelId],
    horizons: Vec<i64>,
    opts: StreamOptions,
) -> PartResult {
    let inst = out.instruments[0].clone();
    let cfg = SimConfig {
        horizons,
        h_max: H_MAX,
        tick: inst.tick_size,
        lot: inst.lot_size.max(1) as f64,
        cadence0: true,
        seed: 3,
        write_trajectory: true,
        trace: false,
    };
    let lat = LatencyModel::default();
    let inputs = shadow_inputs_with_amends(specs, &opts.amends, &lat, 3).unwrap();
    let events = out.events.clone();
    run_stream(
        rules(venue),
        inst.session_date,
        Input::Mbo(Box::new(events.into_iter().map(Ok))),
        inst,
        specs.to_vec(),
        inputs,
        models,
        &cfg,
        &L2Config::default(),
        lat,
        3,
        false,
        opts,
    )
    .unwrap()
}

fn mid(s: &str) -> ModelId {
    s.parse().unwrap()
}

/// O-ZOO-4: `hbt_l3_fifo` corrected reproduces the exact engine's fill timeline (integer quantities and times).
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn o_zoo_4_l3_fifo_corrected_equals_exact() {
    let mut compared = 0;
    let mut filled = 0;
    for venue in [Venue::Cme, Venue::Nasdaq, Venue::Generic] {
        for seed in [1u64, 2, 3] {
            let out = synth(venue, seed, 3_000);
            let specs = shadows(&out, 80, seed);
            let r = run(&out, venue, &specs, &[mid("hbt_l3_fifo[mode=corrected]")], vec![H_MAX]);
            let mut model: BTreeMap<&str, Vec<(i64, u64)>> = BTreeMap::new();
            for f in &r.out.fills {
                model
                    .entry(f.shadow_id.as_str())
                    .or_default()
                    .push((f.ts_event, f.fill_qty as u64));
            }
            for f in &r.facts {
                let t = f.truth.as_ref().unwrap();
                // horizon cut: both arms observed up to ts_live + h_max
                let end = f.ts_live + H_MAX;
                let exact: Vec<(i64, u64)> = t.fills.iter().copied().filter(|x| x.0 <= end).collect();
                let ours = model.get(f.shadow_id.as_str()).cloned().unwrap_or_default();
                assert_eq!(
                    ours, exact,
                    "{venue:?} seed {seed} shadow {}: {:?}",
                    f.shadow_id, t.end_state
                );
                compared += 1;
                filled += usize::from(!exact.is_empty());
            }
        }
    }
    assert!(compared > 500 && filled > 50, "{compared} {filled}");
}

fn at_hmax<'a>(rows: &'a [ZooPredictionRow], prefix: &str) -> Vec<&'a ZooPredictionRow> {
    rows.iter()
        .filter(|r| r.horizon_ns == H_MAX && r.model_id.starts_with(prefix))
        .collect()
}

/// O-ZOO-6: fill counts at `h_max`: `front_of_queue >= exact >= trade_through_only`, and runner alignment with
/// the exact arm (activation event, queue at live, truth columns, A2 censoring, censoring copied to the models).
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn o_zoo_6_bracket_and_alignment() {
    for venue in [Venue::Cme, Venue::Nasdaq, Venue::Generic] {
        for mode in [Mode::Faithful, Mode::Corrected] {
            let out = synth(venue, 11, 3_000);
            let specs = shadows(&out, 80, 11);
            let models = qt_zoo::default_models(mode, Exchange::Partial);
            let r = run(&out, venue, &specs, &models, vec![1_000_000_000, H_MAX]);
            let preds = &r.out.predictions;
            assert_eq!(preds.len(), models.len() * specs.len() * 2);
            let fq = at_hmax(preds, "front_of_queue")
                .iter()
                .filter(|p| p.p_fill >= 0.5)
                .count();
            let tt = at_hmax(preds, "trade_through_only")
                .iter()
                .filter(|p| p.p_fill >= 0.5)
                .count();
            let ex = at_hmax(preds, "front_of_queue")
                .iter()
                .filter(|p| p.truth_filled == Some(true))
                .count();
            assert!(fq >= ex && ex >= tt, "{venue:?} {mode:?}: foq {fq} exact {ex} tto {tt}");
            // alignment
            for (k, f) in r.facts.iter().enumerate() {
                let t = f.truth.as_ref().unwrap();
                let rejected = matches!(
                    t.end_state,
                    Some(EndState::RejectedNoTouch | EndState::RejectedWouldCross)
                );
                if !rejected && t.ts_activated.is_some() {
                    assert_eq!(
                        r.out.activated[k], t.ts_activated,
                        "{venue:?} activation of {}",
                        f.shadow_id
                    );
                }
                assert_eq!(t.ts_live, f.ts_live);
            }
            for p in preds {
                let f = r.facts.iter().find(|f| f.shadow_id == p.shadow_id).unwrap();
                let t = f.truth.as_ref().unwrap();
                assert_eq!(p.queue_ahead_at_live, t.queue_ahead_at_live);
                assert_eq!(p.truth_ts_first_fill, t.ts_first_fill);
                assert!(p.truth_filled.is_some());
                assert_eq!(p.censored, p.ts_live + p.horizon_ns > f.admin_end, "A2 censoring");
            }
            // censoring copied: no corrected-model fill after an exact clear / gap end of that shadow
            if mode == Mode::Corrected {
                for fill in &r.out.fills {
                    let f = r.facts.iter().find(|f| f.shadow_id == fill.shadow_id).unwrap();
                    let t = f.truth.as_ref().unwrap();
                    if matches!(t.end_state, Some(EndState::CensoredClear | EndState::CensoredGap)) {
                        assert!(fill.ts_event <= f.admin_end, "{} filled after censoring", fill.model_id);
                    }
                }
            }
        }
    }
}

fn scratch(name: &str) -> PathBuf {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../.scratch/zoo-gate")
        .join(name);
    let _ = std::fs::remove_dir_all(&p);
    std::fs::create_dir_all(&p).unwrap();
    p
}

fn write_shadows(path: &Path, specs: &[ShadowSpec]) {
    let mut w = qt_schema::io::TableWriter::create(path, "qt.shadow_spec").unwrap();
    w.write(&qt_schema::codec::shadow_specs_to_batch(specs)).unwrap();
    w.finish().unwrap();
}

fn zoo_spec(data: &Path, shadows: &Path, out: &Path, threads: u32) -> ZooSpec {
    let v = serde_json::json!({
        "dataset": {"path": data.to_string_lossy()},
        "shadows": {"path": shadows.to_string_lossy()},
        "out": out.to_string_lossy(),
        "threads": threads,
        "write_trajectory": true,
        "particles": 50,
        "horizons_ns": [100_000_000_i64, 1_000_000_000_i64, 10_000_000_000_i64, 60_000_000_000_i64],
    });
    let s: ZooSpec = serde_json::from_value(v).unwrap();
    let _ = (DatasetRef::path("x"), FileRef::path("x"));
    s
}

fn synth_small_shadows(base: &Path) -> (PathBuf, PathBuf, Vec<ShadowSpec>) {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/synth_small");
    let ds = qt_ingest::Dataset::open(&root).unwrap();
    // shadows sampled at the fixture's own quotes (testkit sampler on each partition's records)
    let mut specs = Vec::new();
    for p in ds.partitions() {
        let events: Vec<_> = p.mbo().unwrap().map(|e| e.unwrap()).collect();
        let inst = ds.instrument(p.instrument_id, p.session_date).unwrap().clone();
        let so = qt_testkit::expectations(events, vec![inst]);
        let mut s = shadows(&so, 25, 5);
        for x in &mut s {
            x.shadow_id = format!("{}_{}_{}", p.session_date, p.instrument_id, x.shadow_id);
        }
        specs.extend(s);
    }
    let sh = base.join("shadows.parquet");
    write_shadows(&sh, &specs);
    (root, sh, specs)
}

/// `run_zoo` on the committed synthetic fixture; O-ZOO-5: `--threads 1` vs `4` give identical files.
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn run_zoo_synth_small_threads_deterministic() {
    let base = scratch("synth_small");
    let (root, sh, specs) = synth_small_shadows(&base);
    let s1 = qt_zoo::run_zoo(&zoo_spec(&root, &sh, &base.join("t1"), 1)).unwrap();
    let s4 = qt_zoo::run_zoo(&zoo_spec(&root, &sh, &base.join("t4"), 4)).unwrap();
    assert_eq!(s1.models.len(), 17);
    assert_eq!(s1.n_shadows, specs.len() as u64);
    assert!(s1.leaderboard.is_empty());
    assert_eq!(s1.files, s4.files, "threads must not change outputs");
    assert_eq!(s1.run_id, s4.run_id);
    let names: Vec<&str> = s1.files.iter().map(|f| f.name.as_str()).collect();
    assert_eq!(
        names,
        [
            "zoo_predictions.parquet",
            "zoo_fills.parquet",
            "zoo_trajectory.parquet",
            "zoo_shadows.parquet"
        ]
    );
    let p = s1.files[0].rows.unwrap();
    assert_eq!(p, 17 * specs.len() as u64 * 5);
    // faithful hbt #312 (qt ends only the pair where hbt ends the run, E8): only the affected (model, shadow) pairs are
    // aborted, from the abort on; every model keeps its rows, the aborted ones carry status `hbt_312_abort`, and
    // the per-model warning counts the aborted pairs
    let mut aborted: BTreeMap<String, HashSet<String>> = BTreeMap::new();
    let mut by_pair: BTreeMap<(String, String), Vec<(i64, bool)>> = BTreeMap::new();
    for b in qt_schema::io::read_table_as(&base.join("t1/zoo_predictions.parquet"), "qt.zoo_predictions").unwrap() {
        let b = b.unwrap();
        let rows = qt_schema::codec::batch_to_zoo_predictions(&b).unwrap();
        let st = b
            .column_by_name("status")
            .expect("status column")
            .as_any()
            .downcast_ref::<arrow_array::DictionaryArray<arrow_array::types::Int8Type>>()
            .expect("dictionary")
            .clone();
        for (i, r) in rows.iter().enumerate() {
            let a = !arrow_array::Array::is_null(&st, i);
            if a {
                aborted
                    .entry(r.model_id.clone())
                    .or_default()
                    .insert(r.shadow_id.clone());
            }
            by_pair
                .entry((r.model_id.clone(), r.shadow_id.clone()))
                .or_default()
                .push((r.horizon_ns, a));
        }
    }
    assert!(
        !aborted.is_empty(),
        "synth_small reaches the #312 event in faithful partial-exchange hbt models"
    );
    for (m, sids) in &aborted {
        assert!(m.contains("mode=faithful") && m.starts_with("hbt_"), "{m}");
        let w = s1
            .warnings
            .iter()
            .find(|w| w.starts_with(&format!("hbt_312_abort: {m}: ")))
            .unwrap_or_else(|| panic!("{m}: {:?}", s1.warnings));
        assert!(
            w.contains(&format!(": {} of {} shadow pair(s)", sids.len(), specs.len())),
            "{w}"
        );
        // the summary field carries the same per-model pair count (1.1, additive)
        assert_eq!(s1.hbt_312_aborts.get(m), Some(&(sids.len() as u64)), "{m}");
    }
    assert_eq!(s1.hbt_312_aborts.len(), aborted.len(), "{:?}", s1.hbt_312_aborts);
    assert_eq!(s4.hbt_312_aborts, s1.hbt_312_aborts, "thread count changes nothing");
    // once a pair is aborted at a horizon it stays aborted at every longer one
    for v in by_pair.values_mut() {
        v.sort_unstable();
        assert!(v.windows(2).all(|w| !w[0].1 || w[1].1), "{v:?}");
    }
    let text = std::fs::read_to_string(base.join("t1/summary.json")).unwrap();
    assert!(text.contains("\"schema\": \"qt.zoo_summary\""));
    let _ = std::fs::remove_dir_all(&base);
}

/// L2-only dataset: the zoo runs on the ingested L2 rows without an exact arm (truth columns null).
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn l2_only_dataset_mode() {
    let out = synth(Venue::Generic, 21, 2_000);
    let specs = shadows(&out, 30, 21);
    let base = scratch("l2only");
    let root = base.join("ds");
    let mut w =
        qt_ingest::DatasetWriter::create(&root, qt_ingest::DatasetKind::L2, Venue::Generic, "tardis_l2").unwrap();
    w.set_instruments(out.instruments.clone());
    let inst = &out.instruments[0];
    let mut b = qt_l2::L2Builder::new(qt_l2::L2Config::default(), rules(Venue::Generic), inst.instrument_id);
    let mut rows: Vec<L2Row> = Vec::new();
    for e in &out.events {
        b.push(e, &mut rows);
    }
    b.flush(out.events.last().map(|e| e.ts_event).unwrap_or(0), &mut rows);
    for r in &rows {
        w.push_l2(r).unwrap();
    }
    w.finish(Default::default()).unwrap();
    let sh = base.join("shadows.parquet");
    write_shadows(&sh, &specs);
    let mut spec = zoo_spec(&root, &sh, &base.join("out"), 2);
    spec.models = vec![
        "hbt_power3[n=2]".into(),
        "nt_queue_l2".into(),
        "databento_mc[k=-0.8]".into(),
    ];
    // schema 1.1 contract: what an L2-only dataset cannot honour is QT-CONFIG-001, never a warning
    let err = qt_zoo::run_zoo(&spec).unwrap_err();
    assert_eq!(err.code(), "QT-CONFIG-001", "{err}");
    assert!(err.to_string().contains("write_trajectory"), "{err}");
    spec.write_trajectory = false;
    let s = qt_zoo::run_zoo(&spec).unwrap();
    assert!(s.warnings.iter().all(|w| !w.contains("trajectory")), "{:?}", s.warnings);
    let mut preds = Vec::new();
    for batch in qt_schema::io::read_table_as(&base.join("out/zoo_predictions.parquet"), "qt.zoo_predictions").unwrap()
    {
        preds.extend(qt_schema::codec::batch_to_zoo_predictions(&batch.unwrap()).unwrap());
    }
    assert_eq!(preds.len(), 3 * specs.len() * 5);
    assert!(
        preds
            .iter()
            .all(|p| p.truth_filled.is_none() && p.truth_filled_qty.is_none() && !p.censored)
    );
    assert!(preds.iter().any(|p| p.p_fill > 0.0));
    let mut bad = spec.clone();
    bad.exclude_order_ids = vec![ExcludeOrderId {
        instrument_id: specs[0].instrument_id,
        order_id: 1,
        session_date: None,
    }];
    let err = qt_zoo::run_zoo(&bad).unwrap_err();
    assert_eq!(err.code(), "QT-CONFIG-001", "{err}");
    assert!(err.to_string().contains("exclude_order_ids"), "{err}");
    let mut bad = spec.clone();
    bad.l2.depth_levels = 3;
    let err = qt_zoo::run_zoo(&bad).unwrap_err();
    assert_eq!(err.code(), "QT-CONFIG-001", "{err}");
    assert!(err.to_string().contains("l2"), "{err}");
    // hbt_l3_fifo needs MBO
    spec.models = vec!["hbt_l3_fifo".into()];
    assert!(qt_zoo::run_zoo(&spec).is_err());
    let _ = std::fs::remove_dir_all(&base);
}

/// Particle simulator (C2 input for calibration): one data pass records the train shadows' tapes; `simulate` replays
/// them with common random numbers (identical for the same seed, independent of `k` in its stream).
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn particle_simulator_common_random_numbers() {
    let base = scratch("psim");
    let (root, sh, specs) = synth_small_shadows(&base);
    let spec: qt_schema::specs::CalibrationSpec = serde_json::from_value(serde_json::json!({
        "kind": "survival",
        "family": "databento",
        "dataset": {"path": root.to_string_lossy()},
        "shadows": {"path": sh.to_string_lossy()},
        "particles": 40,
        "threads": 2,
    }))
    .unwrap();
    let sim = qt_zoo::particle_simulator(&spec).unwrap();
    assert_eq!(
        sim.horizons(),
        &[
            100_000_000,
            1_000_000_000,
            10_000_000_000,
            60_000_000_000,
            300_000_000_000
        ]
    );
    let a = sim.simulate(-0.8, 7).unwrap();
    assert_eq!(a.len(), specs.len());
    assert_eq!(a, sim.simulate(-0.8, 7).unwrap());
    let b = sim.simulate(0.5, 7).unwrap();
    assert_eq!(a.len(), b.len());
    for c in &a {
        assert_eq!(c.survival.len(), 5);
        assert!(c.survival.iter().all(|s| *s > 0.0 && *s < 1.0 + 1e-12));
        assert!(c.survival.windows(2).all(|w| w[1] <= w[0]));
    }
    assert!(a.iter().any(|c| c.truth_interval.is_some()));
    // more front allocation (k > 0 moves cancels to the front) never fills later on average
    let mean = |v: &[qt_zoo::ShadowCurve]| v.iter().map(|c| c.survival[4]).sum::<f64>() / v.len() as f64;
    assert!(mean(&b) <= mean(&a) + 1e-9, "{} vs {}", mean(&b), mean(&a));
    let _ = std::fs::remove_dir_all(&base);
}

// ---- schema 1.1: ZooSpec.amends, ZooSpec.exclude_order_ids, liquidity columns --------------------------------

/// Shadows standing in for real orders (audit A1 twins): side, price and size of the order's add, submitted 1 ns
/// before it (so they go live in the twin's queue place), no cancel; up to `n` orders that later execute and `n`
/// that never do, added after the first 300 records (a filtered copy of a tape that starts with twin-only events
/// would move the L2 view's initial snapshot after the twins' activation). Returns (shadows, twin order ids).
fn twins(out: &SynthOutput, n: usize) -> (Vec<ShadowSpec>, Vec<u64>) {
    let template = shadows(out, 1, 1).remove(0);
    let executed: HashSet<u64> = out
        .events
        .iter()
        .filter(|e| e.action == b'F')
        .map(|e| e.order_id)
        .collect();
    let mut seen = HashSet::new();
    let (mut n_exec, mut n_rest) = (0, 0);
    let mut specs = Vec::new();
    let mut ids = Vec::new();
    for e in out.events.iter().skip(300) {
        if e.action != b'A' || e.price == UNDEF_PRICE || e.size == 0 || !seen.insert(e.order_id) {
            continue;
        }
        let Some(side) = BookSide::from_byte(e.side) else {
            continue;
        };
        let exec = executed.contains(&e.order_id);
        let k = if exec { &mut n_exec } else { &mut n_rest };
        if *k >= n {
            continue;
        }
        *k += 1;
        let mut s = template.clone();
        s.shadow_id = format!("twin_{}", e.order_id);
        s.instrument_id = e.instrument_id;
        s.side = side;
        s.price = PriceSpec::Abs(e.price);
        s.qty = e.size;
        s.ts_submit = e.ts_event - 1;
        s.latency_ns = 0;
        s.ts_cancel = None;
        s.order_key = None;
        specs.push(s);
        ids.push(e.order_id);
    }
    (specs, ids)
}

/// The tape without the records of `ids` (`A`/`C`/`M`/`F`), a dropped `LAST` flag moved to the previous kept record
/// of the same event (the scorer's former filtered-copy fallback, `queuetruth.score.selfexclude.filter_mbo`).
fn filtered(out: &SynthOutput, ids: &[u64]) -> SynthOutput {
    let ids: HashSet<u64> = ids.iter().copied().collect();
    let mut events: Vec<MboEvent> = Vec::with_capacity(out.events.len());
    let mut open_kept: Option<usize> = None;
    for e in &out.events {
        let drop = ids.contains(&e.order_id) && matches!(e.action, b'A' | b'C' | b'M' | b'F');
        if drop {
            if e.is_last()
                && let Some(k) = open_kept
            {
                events[k].flags |= qt_types::flags::LAST;
            }
        } else {
            events.push(*e);
            open_kept = Some(events.len() - 1);
        }
        if e.is_last() {
            open_kept = None;
        }
    }
    SynthOutput { events, ..out.clone() }
}

/// Fill quantity per timestamp (fills at the same timestamp summed), in time order.
fn by_ts(v: &[(i64, u64)]) -> Vec<(i64, u64)> {
    let mut m: BTreeMap<i64, u64> = BTreeMap::new();
    for (t, q) in v {
        *m.entry(*t).or_default() += q;
    }
    m.into_iter().collect()
}

fn fills_by_shadow(r: &PartResult) -> BTreeMap<String, Vec<(i64, u64)>> {
    let mut m: BTreeMap<String, Vec<(i64, u64)>> = BTreeMap::new();
    for f in &r.out.fills {
        m.entry(f.shadow_id.clone())
            .or_default()
            .push((f.ts_event, f.fill_qty as u64));
    }
    m
}

/// A1 + amends (O-ZOO-4 under the 1.1 options): with real orders excluded from the tape and shadow amends applied,
/// corrected `hbt_l3_fifo` still reproduces the exact engine's fill timeline, the twins' executions are credited
/// to the shadows in their place (twins of executed orders fill, as the exact arm says), and every excluded id is
/// matched on the tape.
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn a1_exclusion_and_amends_l3_corrected_equals_exact() {
    let (mut compared, mut twin_filled, mut amended) = (0, 0, 0);
    for venue in [Venue::Cme, Venue::Nasdaq, Venue::Generic] {
        for seed in [4u64, 5] {
            let out = synth(venue, seed, 3_000);
            let (tw, ids) = twins(&out, 15);
            let (mut specs, amends) = qt_testkit::fixtures::sample_shadows(&out, 60, seed);
            amended += amends.len();
            specs.extend(tw);
            let opts = StreamOptions {
                amends: amends.clone(),
                exclude_order_ids: ids.clone(),
                ..StreamOptions::default()
            };
            let r = run_with(
                &out,
                venue,
                &specs,
                &[mid("hbt_l3_fifo[mode=corrected]")],
                vec![H_MAX],
                opts,
            );
            assert_eq!(r.excluded.requested, ids.len() as u64);
            assert_eq!(r.excluded.matched, ids.len() as u64, "{venue:?} seed {seed}");
            // twin linking (RD-11): a linked twin stays on the tape (replaced by its own shadow only); only the
            // orders no shadow enters at are removed, so fewer records go than with whole-order exclusion
            let all: u64 = out
                .events
                .iter()
                .filter(|e| ids.contains(&e.order_id) && matches!(e.action, b'A' | b'C' | b'M' | b'F'))
                .count() as u64;
            assert!(
                r.excluded.records < all,
                "{venue:?} seed {seed}: {} of {all}",
                r.excluded.records
            );
            let model = fills_by_shadow(&r);
            for f in &r.facts {
                let t = f.truth.as_ref().unwrap();
                let end = f.ts_live + H_MAX;
                let exact: Vec<(i64, u64)> = t.fills.iter().copied().filter(|x| x.0 <= end).collect();
                let ours = model.get(&f.shadow_id).cloned().unwrap_or_default();
                // twin linking: the exact engine fills a twin's shadow once per execution record of the twin, the
                // L3 model once per timestamp; the fill timeline (quantity by timestamp) is the same
                assert_eq!(
                    by_ts(&ours),
                    by_ts(&exact),
                    "{venue:?} seed {seed} shadow {}: {:?}",
                    f.shadow_id,
                    t.end_state
                );
                compared += 1;
                twin_filled += usize::from(f.shadow_id.starts_with("twin_") && !exact.is_empty());
            }
        }
    }
    assert!(compared > 400, "{compared}");
    assert!(twin_filled > 10, "twins credited with their executions: {twin_filled}");
    assert!(amended > 0, "the sampler produced no amends");
}

/// A1 on the L2 arm with twin linking (RD-11): a linked twin stays a real order for every other shadow, so the
/// L2 view keeps its displayed size; only the excluded orders no shadow enters at are removed (records dropped,
/// event boundaries kept). The L2 view of a natively excluded run therefore equals the view of the tape without
/// the unlinked orders' records, and every L2 model predicts the same.
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn a1_exclusion_l2_view_equals_filtered_tape() {
    let models: Vec<ModelId> = [
        "hbt_power3[n=2]",
        "hbt_risk_adverse[mode=corrected]",
        "nt_queue_l2",
        "front_of_queue",
        "databento_mc[k=-0.8;particles=20]",
    ]
    .iter()
    .map(|s| mid(s))
    .collect();
    let key = |p: &ZooPredictionRow| (p.model_id.clone(), p.shadow_id.clone(), p.horizon_ns);
    for venue in [Venue::Cme, Venue::Nasdaq] {
        let out = synth(venue, 9, 3_000);
        let (tw, ids) = twins(&out, 20);
        let (mut specs, _) = qt_testkit::fixtures::sample_shadows(&out, 40, 9);
        specs.extend(tw);
        let hs = vec![1_000_000_000, H_MAX];
        // an excluded order is linked when excluding it alone removes none of its records
        let unlinked: Vec<u64> = ids
            .iter()
            .copied()
            .filter(|id| {
                run_with(
                    &out,
                    venue,
                    &specs,
                    &models[..1],
                    vec![H_MAX],
                    StreamOptions {
                        exclude_order_ids: vec![*id],
                        ..StreamOptions::default()
                    },
                )
                .excluded
                .records
                    > 0
            })
            .collect();
        assert!(unlinked.len() < ids.len(), "{venue:?}: no twin linked");
        let native = run_with(
            &out,
            venue,
            &specs,
            &models,
            hs.clone(),
            StreamOptions {
                exclude_order_ids: ids.clone(),
                ..StreamOptions::default()
            },
        );
        let copy = run_with(
            &filtered(&out, &unlinked),
            venue,
            &specs,
            &models,
            hs,
            StreamOptions::default(),
        );
        let a: BTreeMap<_, _> = native.out.predictions.iter().map(|p| (key(p), p)).collect();
        let b: BTreeMap<_, _> = copy.out.predictions.iter().map(|p| (key(p), p)).collect();
        assert_eq!(a.len(), b.len());
        for (k, x) in &a {
            let y = b[k];
            assert_eq!(
                (
                    x.p_fill,
                    x.e_fill_qty,
                    x.model_ts_first_fill,
                    x.visible_level_qty_at_live
                ),
                (
                    y.p_fill,
                    y.e_fill_qty,
                    y.model_ts_first_fill,
                    y.visible_level_qty_at_live
                ),
                "{venue:?} {k:?}"
            );
        }
    }
}

/// Amends on the L2 arm: a priority-keeping size reduction never adds fills (deterministic models), caps the
/// filled quantity at the amended total and changes some predictions on the synthetic tape.
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn amends_reduce_l2_model_fills() {
    let models: Vec<ModelId> = ["front_of_queue", "hbt_power3[n=2]", "hbt_risk_adverse[mode=corrected]"]
        .iter()
        .map(|s| mid(s))
        .collect();
    let mut changed = 0;
    for venue in [Venue::Cme, Venue::Generic] {
        let out = synth(venue, 13, 3_000);
        let (specs, amends) = qt_testkit::fixtures::sample_shadows(&out, 80, 13);
        assert!(!amends.is_empty());
        let mut final_qty: BTreeMap<&str, u64> = specs.iter().map(|s| (s.shadow_id.as_str(), s.qty)).collect();
        let mut last_amend: BTreeMap<&str, i64> = specs.iter().map(|s| (s.shadow_id.as_str(), i64::MIN)).collect();
        for a in &amends {
            final_qty.insert(a.shadow_id.as_str(), a.new_qty);
            last_amend.insert(a.shadow_id.as_str(), a.ts_send);
        }
        let with = run_with(
            &out,
            venue,
            &specs,
            &models,
            vec![H_MAX],
            StreamOptions {
                amends: amends.clone(),
                ..StreamOptions::default()
            },
        );
        let without = run_with(&out, venue, &specs, &models, vec![H_MAX], StreamOptions::default());
        for (x, y) in with.out.predictions.iter().zip(&without.out.predictions) {
            assert_eq!((&x.model_id, &x.shadow_id), (&y.model_id, &y.shadow_id));
            assert!(x.e_fill_qty <= y.e_fill_qty + 1e-9, "{} {}", x.model_id, x.shadow_id);
            // fills before the last amend took effect stay (an amend at or below them ends the order)
            let sid = x.shadow_id.as_str();
            if y.model_ts_first_fill.is_none_or(|t| t > last_amend[sid]) {
                assert!(x.e_fill_qty <= final_qty[sid] as f64 + 1e-9, "{} {sid}", x.model_id);
            }
            changed += usize::from(x.e_fill_qty < y.e_fill_qty);
        }
    }
    assert!(changed > 0, "amends changed no prediction");
}

/// `run_zoo` with the 1.1 options end to end: amends and exclusion files are read, unknown amend ids are an input
/// error, `qt.zoo_fills` / `qt.zoo_predictions` are written in the full 1.1 layout (`liquidity` from the fill reason;
/// predictions: the first model fill within the horizon, null without one and for particle models) and
/// `qt.zoo_shadows` uses its registered schema.
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn run_zoo_schema_1_1_options_and_layouts() {
    let base = scratch("v11");
    let (root, sh, specs) = synth_small_shadows(&base);
    let ds = qt_ingest::Dataset::open(&root).unwrap();
    let part = &ds.partitions()[0];
    let ids: Vec<u64> = part
        .mbo()
        .unwrap()
        .map(|e| e.unwrap())
        .filter(|e| e.action == b'A')
        .map(|e| e.order_id)
        .take(5)
        .collect();
    let amends: Vec<ShadowAmend> = specs
        .iter()
        .filter(|s| s.qty > 1)
        .take(5)
        .map(|s| ShadowAmend {
            shadow_id: s.shadow_id.clone(),
            ts_send: s.ts_submit + 1_000_000_000,
            new_qty: s.qty - 1,
        })
        .collect();
    assert!(!amends.is_empty());
    let am_path = base.join("amends.parquet");
    write_amends(&am_path, &amends);
    let mut spec = zoo_spec(&root, &sh, &base.join("out"), 2);
    spec.models = vec![
        "hbt_l3_fifo[mode=corrected]".into(),
        "hbt_power3[n=2]".into(),
        "databento_mc[k=-0.8]".into(),
    ];
    spec.amends = Some(FileRef::path(am_path.to_string_lossy()));
    spec.exclude_order_ids = ids
        .iter()
        .map(|o| ExcludeOrderId {
            instrument_id: part.instrument_id,
            order_id: *o,
            session_date: None,
        })
        .collect();
    let s = qt_zoo::run_zoo(&spec).unwrap();
    let w = s
        .warnings
        .iter()
        .find(|w| w.starts_with("exclude_order_ids:"))
        .unwrap_or_else(|| panic!("{:?}", s.warnings));
    assert!(w.contains("requested=5 matched=5"), "{w}");
    assert!(s.spec.get("amends").and_then(|v| v.get("path")).is_some());
    // full 1.1 layouts
    let read = |name: &str, table: &'static str| {
        let mut out = Vec::new();
        for b in qt_schema::io::read_table_as(&base.join("out").join(name), table).unwrap() {
            out.push(b.unwrap());
        }
        out
    };
    let pf = read("zoo_predictions.parquet", "qt.zoo_predictions");
    let ff = read("zoo_fills.parquet", "qt.zoo_fills");
    assert!(
        pf.iter()
            .all(|b| qt_schema::codec::has_added_columns(b, "qt.zoo_predictions"))
    );
    assert!(
        ff.iter()
            .all(|b| qt_schema::codec::has_added_columns(b, "qt.zoo_fills"))
    );
    let mut n_taker_or_maker = 0;
    for b in &ff {
        let rows = qt_schema::codec::batch_to_zoo_fills(b).unwrap();
        let liq = qt_schema::codec::batch_liquidity(b, "qt.zoo_fills").unwrap().unwrap();
        for (r, l) in rows.iter().zip(liq) {
            // A5 as refined by E48: only crossed_at_live is taker; a resting order later
            // crossed fills as a maker at its own price
            let want = if r.reason == ZooFillReason::CrossedAtLive {
                Liquidity::Taker
            } else {
                Liquidity::Maker
            };
            assert_eq!(l, Some(want), "{:?}", r.reason);
            n_taker_or_maker += 1;
        }
    }
    assert!(n_taker_or_maker > 0);
    let mut fills: BTreeMap<(String, String), Vec<(i64, ZooFillReason)>> = BTreeMap::new();
    for b in &ff {
        for r in qt_schema::codec::batch_to_zoo_fills(b).unwrap() {
            fills
                .entry((r.model_id.clone(), r.shadow_id.clone()))
                .or_default()
                .push((r.ts_event, r.reason));
        }
    }
    for b in &pf {
        let rows = qt_schema::codec::batch_to_zoo_predictions(b).unwrap();
        let liq = qt_schema::codec::batch_liquidity(b, "qt.zoo_predictions")
            .unwrap()
            .unwrap();
        for (r, l) in rows.iter().zip(liq) {
            if r.model_id.starts_with("databento_mc") {
                assert_eq!(l, None);
                continue;
            }
            let first = fills
                .get(&(r.model_id.clone(), r.shadow_id.clone()))
                .and_then(|v| v.first())
                .filter(|f| f.0 <= r.ts_live + r.horizon_ns);
            assert_eq!(
                l,
                first.map(|f| qt_zoo::fill_liquidity(f.1)),
                "{} {}",
                r.model_id,
                r.shadow_id
            );
        }
    }
    // qt.zoo_shadows: registered schema
    let zs = read("zoo_shadows.parquet", "qt.zoo_shadows");
    let reg = qt_schema::schema("qt.zoo_shadows").unwrap();
    for b in &zs {
        assert_eq!(b.schema().fields(), reg.fields());
    }
    // an amend of an unknown shadow is an input error, as in replay
    let bad = base.join("bad_amends.parquet");
    write_amends(
        &bad,
        &[ShadowAmend {
            shadow_id: "nope".into(),
            ts_send: 0,
            new_qty: 1,
        }],
    );
    spec.amends = Some(FileRef::path(bad.to_string_lossy()));
    let err = qt_zoo::run_zoo(&spec).unwrap_err();
    assert_eq!(err.code(), "QT-INPUT-002", "{err}");
    let _ = std::fs::remove_dir_all(&base);
}

fn write_amends(path: &Path, amends: &[ShadowAmend]) {
    let mut w = qt_schema::io::TableWriter::create(path, "qt.shadow_amend").unwrap();
    w.write(&qt_schema::codec::shadow_amends_to_batch(amends)).unwrap();
    w.finish().unwrap();
}

/// Without `write_trajectory`, an MBO zoo run still writes the model trajectories of the shadows
/// the report's why traces pick (`traced_shadows`, all models), equal to the rows of a full-trajectory run for those
/// shadows; the table metadata says the scope.
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn default_run_writes_traced_trajectories() {
    let base = scratch("traced");
    let (root, sh, _specs) = synth_small_shadows(&base);
    let mut full = zoo_spec(&root, &sh, &base.join("full"), 2);
    full.models = vec!["hbt_power3[n=2]".into(), "nt_queue_l2".into(), "front_of_queue".into()];
    let mut dflt = full.clone();
    dflt.write_trajectory = false;
    dflt.out = Some(base.join("dflt").to_string_lossy().into_owned());
    qt_zoo::run_zoo(&full).unwrap();
    let s = qt_zoo::run_zoo(&dflt).unwrap();
    assert!(s.files.iter().any(|f| f.name == "zoo_trajectory.parquet"));
    let read_traj = |dir: &str| {
        let p = base.join(dir).join("zoo_trajectory.parquet");
        let mut rows = Vec::new();
        let r = qt_schema::io::read_table_as(&p, "qt.zoo_trajectory").unwrap();
        let scope = r.schema().metadata().get("qt.zoo_trajectory_scope").cloned();
        for b in r {
            rows.extend(qt_schema::codec::batch_to_zoo_trajectory(&b.unwrap()).unwrap());
        }
        (rows, scope)
    };
    let mut preds = Vec::new();
    for b in qt_schema::io::read_table_as(&base.join("dflt/zoo_predictions.parquet"), "qt.zoo_predictions").unwrap() {
        preds.extend(qt_schema::codec::batch_to_zoo_predictions(&b.unwrap()).unwrap());
    }
    let traced = qt_zoo::runner::traced_shadows(&preds, qt_zoo::runner::TRACED_SHADOWS);
    assert!(!traced.is_empty() && traced.len() <= qt_zoo::runner::TRACED_SHADOWS);
    let (all, all_scope) = read_traj("full");
    let (got, scope) = read_traj("dflt");
    assert_eq!(all_scope.as_deref(), Some("all"));
    assert_eq!(scope, Some(format!("traced:{}", traced.len())));
    let ids: HashSet<&str> = traced.iter().map(String::as_str).collect();
    let mut want: Vec<_> = all.into_iter().filter(|r| ids.contains(r.shadow_id.as_str())).collect();
    let mut got = got;
    let key = |r: &qt_schema::rows::ZooTrajectoryRow| (r.model_id.clone(), r.shadow_id.clone(), r.ts_event);
    want.sort_by_key(key);
    got.sort_by_key(key);
    assert!(!got.is_empty());
    assert_eq!(got, want);
    let _ = std::fs::remove_dir_all(&base);
}

/// RD-11 twin linking in the zoo: with `exclude_order_ids` = the own orders of `synth_small/own_orders`, the zoo's
/// exact arm (its truth columns) equals `replay` with the same exclusion shadow for shadow (the own orders' shadows
/// and sampled shadows in the same books): queue ahead at live, first fill time and filled quantity by every horizon.
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn twin_linking_zoo_exact_arm_equals_replay_on_own_orders() {
    let base = scratch("twins_own_orders");
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/synth_small");
    let own = qt_schema::codec::read_shadow_specs(&root.join("own_orders/schedule.parquet")).unwrap();
    // the own orders' shadows plus sampled shadows that share their queues (a twin stays real for those)
    let (_, _, sampled) = synth_small_shadows(&base);
    let specs: Vec<ShadowSpec> = own.iter().cloned().chain(sampled).collect();
    let sched = base.join("all_shadows.parquet");
    write_shadows(&sched, &specs);
    let mut excl: Vec<ExcludeOrderId> = own
        .iter()
        .filter_map(|s| {
            let id = s.order_key.as_deref()?.rsplit(':').next()?.parse::<u64>().ok()?;
            Some(ExcludeOrderId {
                instrument_id: s.instrument_id,
                order_id: id,
                session_date: None,
            })
        })
        .collect();
    excl.sort_by_key(|x| (x.instrument_id, x.order_id));
    excl.dedup();
    assert!(!excl.is_empty());
    // replay with the exclusion (twin linking is always on there)
    let rspec: qt_schema::specs::ReplaySpec = serde_json::from_value(serde_json::json!({
        "dataset": {"path": root.to_string_lossy()},
        "shadows": {"path": sched.to_string_lossy()},
        "out": base.join("replay").to_string_lossy(),
        "bbo": "none",
        "exclude_order_ids": serde_json::to_value(&excl).unwrap(),
    }))
    .unwrap();
    qt_replay::run_dataset(&rspec).unwrap();
    let mut results = BTreeMap::new();
    for b in qt_schema::io::read_table_as(&base.join("replay/shadow_results.parquet"), "qt.shadow_results").unwrap() {
        for r in qt_schema::codec::batch_to_shadow_results(&b.unwrap()).unwrap() {
            results.insert(r.shadow_id.clone(), r);
        }
    }
    let mut fills: BTreeMap<String, Vec<(i64, u64)>> = BTreeMap::new();
    for b in qt_schema::io::read_table_as(&base.join("replay/shadow_fills.parquet"), "qt.shadow_fills").unwrap() {
        for f in qt_schema::codec::batch_to_shadow_fills(&b.unwrap()).unwrap() {
            fills
                .entry(f.shadow_id.clone())
                .or_default()
                .push((f.ts_event, f.fill_qty));
        }
    }
    // the zoo with the same exclusion
    let mut spec = zoo_spec(&root, &sched, &base.join("zoo"), 2);
    spec.models = vec!["front_of_queue".into()];
    spec.write_trajectory = false;
    spec.exclude_order_ids = excl.clone();
    let s = qt_zoo::run_zoo(&spec).unwrap();
    assert!(
        s.warnings.iter().all(|w| !w.contains("differs from the zoo")),
        "{:?}",
        s.warnings
    );
    // every own order is its shadow's twin: nothing is removed from the tape
    let w = s
        .warnings
        .iter()
        .find(|w| w.starts_with("exclude_order_ids:"))
        .unwrap_or_else(|| panic!("{:?}", s.warnings));
    assert!(w.ends_with("records_removed=0"), "{w}");
    let mut preds = Vec::new();
    for b in qt_schema::io::read_table_as(&base.join("zoo/zoo_predictions.parquet"), "qt.zoo_predictions").unwrap() {
        preds.extend(qt_schema::codec::batch_to_zoo_predictions(&b.unwrap()).unwrap());
    }
    assert!(!preds.is_empty());
    let mut filled = 0;
    for p in &preds {
        let r = results
            .get(&p.shadow_id)
            .unwrap_or_else(|| panic!("{}: no replay result", p.shadow_id));
        assert_eq!(p.ts_live, r.ts_live, "{}", p.shadow_id);
        assert_eq!(p.queue_ahead_at_live, r.queue_ahead_at_live, "{}", p.shadow_id);
        assert_eq!(p.truth_ts_first_fill, r.ts_first_fill, "{}", p.shadow_id);
        let dl = p.ts_live.saturating_add(p.horizon_ns);
        let want: u64 = fills
            .get(&p.shadow_id)
            .map(|v| v.iter().filter(|f| f.0 <= dl).map(|f| f.1).sum())
            .unwrap_or(0);
        assert_eq!(p.truth_filled_qty, Some(want), "{} at {}", p.shadow_id, p.horizon_ns);
        filled += usize::from(want > 0);
    }
    assert!(
        filled > 0,
        "the own orders' shadows must fill on their twins' executions"
    );
    let _ = std::fs::remove_dir_all(&base);
}

/// `ExcludeOrderId.session_date` (as `ReplaySpec.exclude_order_ids`): an item applies on that session date only.
/// The own orders dated with their own session reproduce the undated exclusion; dated on a day without data they
/// exclude nothing (the run equals the run without exclusion).
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn exclude_order_ids_honour_session_date() {
    let base = scratch("excl_dated");
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/synth_small");
    let sched = root.join("own_orders/schedule.parquet");
    let own = qt_schema::codec::read_shadow_specs(&sched).unwrap();
    let dates: HashSet<i32> = qt_ingest::Dataset::open(&root)
        .unwrap()
        .partitions()
        .iter()
        .map(|p| p.session_date)
        .collect();
    let items = |date: &dyn Fn(&ShadowSpec) -> Option<String>| -> Vec<ExcludeOrderId> {
        let mut v: Vec<ExcludeOrderId> = own
            .iter()
            .map(|s| ExcludeOrderId {
                instrument_id: s.instrument_id,
                order_id: s
                    .order_key
                    .as_deref()
                    .unwrap()
                    .rsplit(':')
                    .next()
                    .unwrap()
                    .parse()
                    .unwrap(),
                session_date: date(s),
            })
            .collect();
        v.sort_by_key(|a| (a.instrument_id, a.order_id));
        v.dedup();
        v
    };
    let own_date = |s: &ShadowSpec| {
        let d = (s.ts_submit / 86_400_000_000_000) as i32;
        assert!(dates.contains(&d), "{}: no partition on its UTC date", s.shadow_id);
        Some(qt_ingest::time::format_date(d))
    };
    let run = |name: &str, excl: Vec<ExcludeOrderId>| {
        let mut spec = zoo_spec(&root, &sched, &base.join(name), 1);
        spec.models = vec!["front_of_queue".into()];
        spec.write_trajectory = false;
        spec.exclude_order_ids = excl;
        let s = qt_zoo::run_zoo(&spec).unwrap();
        let mut rows = BTreeMap::new();
        for b in qt_schema::io::read_table_as(&base.join(name).join("zoo_predictions.parquet"), "qt.zoo_predictions")
            .unwrap()
        {
            for p in qt_schema::codec::batch_to_zoo_predictions(&b.unwrap()).unwrap() {
                rows.insert(
                    (p.shadow_id.clone(), p.horizon_ns),
                    (
                        p.truth_ts_first_fill,
                        p.truth_filled_qty,
                        p.queue_ahead_at_live,
                        p.p_fill,
                    ),
                );
            }
        }
        (s, rows)
    };
    let (_, undated) = run("undated", items(&|_| None));
    let (_, dated) = run("dated", items(&own_date));
    assert_eq!(dated, undated);
    let (none_s, none) = run("none", Vec::new());
    let (other_s, other) = run("other_day", items(&|_| Some("2000-01-03".to_owned())));
    assert_eq!(other, none);
    assert_ne!(undated, none, "the exclusion must matter on this fixture");
    let w = other_s
        .warnings
        .iter()
        .find(|w| w.starts_with("exclude_order_ids:"))
        .unwrap_or_else(|| panic!("{:?}", other_s.warnings));
    assert!(w.contains(" matched=0 "), "{w}");
    assert_ne!(other_s.run_id, none_s.run_id, "the dated items are part of the spec");
    let _ = std::fs::remove_dir_all(&base);
}

// ---- O-ZOO-6 order by order --------------------------------------------------------------------------------------

/// Runs the two bounds (both modes) on one instrument's records against the exact arm.
fn run_bounds(venue: Venue, inst: qt_types::Instrument, events: Vec<MboEvent>, specs: &[ShadowSpec]) -> PartResult {
    let models: Vec<ModelId> = [
        "front_of_queue[mode=faithful]",
        "front_of_queue[mode=corrected]",
        "trade_through_only[mode=faithful]",
        "trade_through_only[mode=corrected]",
    ]
    .iter()
    .map(|s| mid(s))
    .collect();
    let cfg = SimConfig {
        horizons: vec![H_MAX],
        h_max: H_MAX,
        tick: inst.tick_size.max(1),
        lot: inst.lot_size.max(1) as f64,
        cadence0: true,
        seed: 3,
        write_trajectory: false,
        trace: false,
    };
    let lat = LatencyModel::default();
    let inputs = shadow_inputs_with_amends(specs, &[], &lat, 3).unwrap();
    let l2 = L2Config {
        emit_hidden_trades: true,
        ..L2Config::default()
    };
    run_stream(
        rules(venue),
        inst.session_date,
        Input::Mbo(Box::new(events.into_iter().map(Ok))),
        inst,
        specs.to_vec(),
        inputs,
        &models,
        &cfg,
        &l2,
        lat,
        3,
        false,
        StreamOptions::default(),
    )
    .unwrap()
}

/// Bracket violations of one run: (shadow, model, what) for every shadow whose exact first fill (within h_max) is
/// missing or earlier than `front_of_queue`'s, or whose `trade_through_only` first fill has no exact fill at or
/// before it.
fn bracket_violations(r: &PartResult) -> Vec<String> {
    let mut first: BTreeMap<(String, String), i64> = BTreeMap::new();
    for f in &r.out.fills {
        let k = (f.model_id.clone(), f.shadow_id.clone());
        let e = first.entry(k).or_insert(f.ts_event);
        *e = (*e).min(f.ts_event);
    }
    let mut bad = Vec::new();
    for f in &r.facts {
        let Some(t) = f.truth.as_ref() else { continue };
        let end = f.ts_live.saturating_add(H_MAX);
        let exact = t.ts_first_fill.filter(|x| *x <= end);
        for mode in ["faithful", "corrected"] {
            let fq = first
                .get(&(format!("front_of_queue[mode={mode}]"), f.shadow_id.clone()))
                .copied();
            let tto = first
                .get(&(format!("trade_through_only[mode={mode}]"), f.shadow_id.clone()))
                .copied()
                .filter(|x| *x <= end);
            if let Some(x) = exact
                && fq.is_none_or(|q| q > x)
            {
                bad.push(format!(
                    "{} front_of_queue[{mode}]: exact first fill {x} ({:?}), bound {fq:?}",
                    f.shadow_id, t.end_state
                ));
            }
            if let Some(y) = tto
                && exact.is_none_or(|x| x > y)
            {
                bad.push(format!(
                    "{} trade_through_only[{mode}]: bound fills at {y}, exact {exact:?}",
                    f.shadow_id
                ));
            }
        }
    }
    bad
}

/// O-ZOO-6 order by order (not only in counts): on every testkit scenario case (the E36 / A3d / A4b / A6b
/// corpus) and on generated tapes of every venue, each shadow the exact arm fills within h_max is filled by
/// `front_of_queue` no later, and each shadow `trade_through_only` fills is filled by the exact arm no later.
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the book, ingest and testkit crates"
)]
fn bounds_bracket_exact_order_by_order() {
    let mut bad = Vec::new();
    let mut compared = 0;
    for c in qt_testkit::cases::ruling36_cases() {
        let ts0 = c.events.first().map_or(0, |e| e.ts_event);
        let mut inst = qt_testkit::calendar::instrument_for(c.venue, 1, "SCN1", ts0, 1, 1);
        // the cases run on a UTC-day session (the reference default)
        inst.session_date = (ts0 / 86_400_000_000_000) as i32;
        inst.midnight_utc_ns = i64::from(inst.session_date) * 86_400_000_000_000;
        inst.open_s = 0;
        inst.close_s = 86_400;
        let r = run_bounds(c.venue, inst, c.events.clone(), &c.shadows);
        compared += r.facts.len();
        bad.extend(bracket_violations(&r).into_iter().map(|b| format!("{}: {b}", c.name)));
    }
    for venue in [Venue::Cme, Venue::Nasdaq, Venue::Generic] {
        for seed in [11u64, 12, 13] {
            let out = synth(venue, seed, 4_000);
            let specs = shadows(&out, 80, seed);
            let r = run_bounds(venue, out.instruments[0].clone(), out.events.clone(), &specs);
            compared += r.facts.len();
            bad.extend(
                bracket_violations(&r)
                    .into_iter()
                    .map(|b| format!("{venue:?}/{seed}: {b}")),
            );
        }
    }
    // the committed synth_small fixture, partition by partition
    let base = scratch("bracket_synth_small");
    let (root, _, specs) = synth_small_shadows(&base);
    let ds = qt_ingest::Dataset::open(&root).unwrap();
    for p in ds.partitions() {
        let inst = ds.instrument(p.instrument_id, p.session_date).unwrap().clone();
        let mine: Vec<ShadowSpec> = specs
            .iter()
            .filter(|s| {
                s.shadow_id
                    .starts_with(&format!("{}_{}_", p.session_date, p.instrument_id))
            })
            .cloned()
            .collect();
        let events: Vec<MboEvent> = p.mbo().unwrap().map(|e| e.unwrap()).collect();
        let r = run_bounds(ds.manifest.venue, inst, events, &mine);
        compared += r.facts.len();
        bad.extend(bracket_violations(&r).into_iter().map(|b| format!("synth_small: {b}")));
    }
    let _ = std::fs::remove_dir_all(&base);
    assert!(compared > 500, "{compared}");
    assert!(bad.is_empty(), "{} violations:\n{}", bad.len(), bad.join("\n"));
}
