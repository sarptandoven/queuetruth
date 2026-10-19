//! O-SH-2 on the committed fixtures: the engine reproduces `qt_testkit`'s independent reference
//! (`try_reference_shadows`, written from the SPEC text + audit rulings) on `tests/fixtures/synth_small` (cme) and
//! `synth_small_nasdaq`: every `qt.shadow_results` row, the per-fill timelines and the `ahead` trajectories, and the
//! `bbo = full` stream. Reads the Parquet files directly (no `qt_ingest`), one engine per partition as
//! `run_partition` does.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use qt_replay::sinks::VecSink;
use qt_replay::{BboMode, Engine, EngineConfig};
use qt_schema::codec::TableRow;
use qt_schema::io::read_table_as;
use qt_schema::rows::{BboRow, ShadowAmend, ShadowFillRow, ShadowResultRow, ShadowSpec, TrajectoryRow};
use qt_types::{Instrument, MboEvent, rules};

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}

fn read<R: TableRow>(p: &Path) -> Vec<R> {
    let mut out = Vec::new();
    for b in read_table_as(p, R::TABLE).unwrap_or_else(|e| panic!("{}: {e}", p.display())) {
        out.extend(R::from_batch(&b.unwrap()).unwrap());
    }
    out
}

fn files(dir: &Path) -> Vec<PathBuf> {
    let mut v = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                stack.push(p);
            } else if p.extension().is_some_and(|x| x == "parquet") {
                v.push(p);
            }
        }
    }
    v.sort();
    v
}

fn check(name: &str) {
    let dir = root().join(name);
    if !dir.join("truth/shadow_results.parquet").exists() {
        panic!("{name}: fixture truth missing (make fixtures)");
    }
    let insts: Vec<Instrument> = read(&dir.join("instruments.parquet"));
    let venue = insts[0].venue;
    let shadows: Vec<ShadowSpec> = read(&dir.join("truth/shadows.parquet"));
    let amends: Vec<ShadowAmend> = read(&dir.join("truth/amends.parquet"));
    let mut by_part: BTreeMap<(i32, u32), Vec<ShadowSpec>> = BTreeMap::new();
    let day = 86_400_000_000_000i64;
    for s in &shadows {
        let row = insts
            .iter()
            .filter(|i| {
                i.instrument_id == s.instrument_id
                    && i.midnight_utc_ns <= s.ts_submit
                    && s.ts_submit < i.midnight_utc_ns + day
            })
            .max_by_key(|i| i.midnight_utc_ns)
            .expect("partition");
        by_part
            .entry((row.session_date, row.instrument_id))
            .or_default()
            .push(s.clone());
    }
    let mut got = VecSink::default();
    let mut bbo: Vec<BboRow> = Vec::new();
    let mut parts: Vec<((i32, u32), Vec<MboEvent>)> = Vec::new();
    for f in files(&dir.join("mbo")) {
        let ev: Vec<MboEvent> = read(&f);
        let inst = ev[0].instrument_id;
        let row = insts
            .iter()
            .filter(|i| i.instrument_id == inst && i.midnight_utc_ns <= ev[0].ts_event)
            .max_by_key(|i| i.midnight_utc_ns)
            .unwrap();
        parts.push(((row.session_date, inst), ev));
    }
    parts.sort_by_key(|p| p.0);
    for (key, ev) in &parts {
        let sh = by_part.remove(key).unwrap_or_default();
        let ids: std::collections::BTreeSet<&str> = sh.iter().map(|s| s.shadow_id.as_str()).collect();
        let am: Vec<ShadowAmend> = amends
            .iter()
            .filter(|a| ids.contains(a.shadow_id.as_str()))
            .cloned()
            .collect();
        let row: Vec<Instrument> = insts
            .iter()
            .filter(|i| (i.session_date, i.instrument_id) == *key)
            .cloned()
            .collect();
        let mut c = EngineConfig::new(rules(venue));
        c.bbo = BboMode::Full;
        let mut eng = Engine::new(c, &row, sh, am).unwrap();
        eng.set_sequence_gap_detection(false);
        let mut sink = VecSink::default();
        for e in ev {
            eng.apply(e, &mut sink).unwrap();
        }
        eng.finish(&mut sink).unwrap();
        got.shadow_results.extend(sink.shadow_results);
        got.shadow_fills.extend(sink.shadow_fills);
        got.trajectory.extend(sink.trajectory);
        bbo.extend(sink.bbo);
    }
    assert!(by_part.is_empty(), "{name}: shadows without partition");

    let want_bbo: Vec<BboRow> = read(&dir.join("truth/bbo.parquet"));
    let key = |b: &BboRow| (b.instrument_id, b.ts_event, b.sequence);
    let mut a = bbo.clone();
    let mut b = want_bbo.clone();
    a.sort_by_key(key);
    b.sort_by_key(key);
    assert_eq!(a.len(), b.len(), "{name}: bbo rows");
    for (x, y) in a.iter().zip(&b) {
        assert_eq!(x, y, "{name}: bbo");
    }

    let want: Vec<ShadowResultRow> = read(&dir.join("truth/shadow_results.parquet"));
    let wf: Vec<ShadowFillRow> = read(&dir.join("truth/shadow_fills.parquet"));
    let wt: Vec<TrajectoryRow> = read(&dir.join("truth/queue_trajectory.parquet"));
    let mut by_id: BTreeMap<&str, &ShadowResultRow> =
        got.shadow_results.iter().map(|r| (r.shadow_id.as_str(), r)).collect();
    let mut bad = Vec::new();
    for w in &want {
        let g = by_id.remove(w.shadow_id.as_str()).expect("result row");
        if g != w {
            bad.push(format!("result\n got {g:?}\nwant {w:?}"));
        }
    }
    assert!(by_id.is_empty());
    let group_f = |v: &[ShadowFillRow]| {
        let mut m: BTreeMap<String, Vec<ShadowFillRow>> = BTreeMap::new();
        for f in v {
            m.entry(f.shadow_id.clone()).or_default().push(f.clone());
        }
        m
    };
    let (gf, wf) = (group_f(&got.shadow_fills), group_f(&wf));
    for (id, w) in &wf {
        if gf.get(id) != Some(w) {
            bad.push(format!("fills {id}\n got {:?}\nwant {w:?}", gf.get(id)));
        }
    }
    assert_eq!(gf.len(), wf.len(), "{name}: shadows with fills");
    let group_t = |v: &[TrajectoryRow]| {
        let mut m: BTreeMap<String, Vec<TrajectoryRow>> = BTreeMap::new();
        for t in v {
            m.entry(t.shadow_id.clone()).or_default().push(t.clone());
        }
        m
    };
    let (gt, wt) = (group_t(&got.trajectory), group_t(&wt));
    for (id, w) in &wt {
        if gt.get(id) != Some(w) {
            bad.push(format!("trajectory {id}\n got {:?}\nwant {w:?}", gt.get(id)));
        }
    }
    let n = bad.len();
    bad.truncate(6);
    assert!(
        n == 0,
        "{name}: {n} mismatches of {} shadows\n{}",
        want.len(),
        bad.join("\n")
    );
    assert!(
        want.iter().filter(|r| r.filled_qty > 0).count() > 50,
        "{name}: fixture exercises fills"
    );
}

#[test]
fn o_sh_2_fixture_truth_synth_small_cme() {
    check("synth_small");
}

#[test]
fn o_sh_2_fixture_truth_synth_small_nasdaq() {
    check("synth_small_nasdaq");
}

/// Audit ruling A1 on the committed own-orders fixture: the exact-arm schedule replayed with the user's own MBO
/// orders excluded reproduces the reference results and fills.
#[test]
fn audit_a1_fixture_own_orders_excluded() {
    let dir = root().join("synth_small");
    let insts: Vec<Instrument> = read(&dir.join("instruments.parquet"));
    let schedule: Vec<ShadowSpec> = read(&dir.join("own_orders/schedule.parquet"));
    let want: Vec<ShadowResultRow> = read(&dir.join("own_orders/expected_results.parquet"));
    let wf: Vec<ShadowFillRow> = read(&dir.join("own_orders/expected_fills.parquet"));
    let excluded: Vec<(u32, u64)> = schedule
        .iter()
        .map(|s| {
            (
                s.instrument_id,
                s.shadow_id.rsplit(':').next().unwrap().parse().unwrap(),
            )
        })
        .collect();
    let day = 86_400_000_000_000i64;
    let mut got = VecSink::default();
    let mut matched = 0;
    for f in files(&dir.join("mbo")) {
        let ev: Vec<MboEvent> = read(&f);
        let inst = ev[0].instrument_id;
        let row = insts
            .iter()
            .filter(|i| i.instrument_id == inst && i.midnight_utc_ns <= ev[0].ts_event)
            .max_by_key(|i| i.midnight_utc_ns)
            .unwrap()
            .clone();
        let sh: Vec<ShadowSpec> = schedule
            .iter()
            .filter(|s| {
                s.instrument_id == inst && s.ts_submit >= row.midnight_utc_ns && s.ts_submit < row.midnight_utc_ns + day
            })
            .cloned()
            .collect();
        let mut eng = Engine::new(
            EngineConfig::new(rules(row.venue)),
            std::slice::from_ref(&row),
            sh,
            vec![],
        )
        .unwrap();
        eng.set_sequence_gap_detection(false);
        eng.set_excluded_orders(excluded.iter().copied().filter(|e| e.0 == inst));
        eng.set_twin_linking(true);
        for e in &ev {
            eng.apply(e, &mut got).unwrap();
        }
        let rep = eng.finish(&mut got).unwrap();
        matched += rep.diagnostics.excluded_matched;
    }
    assert!(matched >= schedule.len() as u64, "every own order appears on the tape");
    let mut g = got.shadow_results.clone();
    let mut w = want.clone();
    g.sort_by(|a, b| a.shadow_id.cmp(&b.shadow_id));
    w.sort_by(|a, b| a.shadow_id.cmp(&b.shadow_id));
    assert_eq!(g.len(), w.len());
    for (x, y) in g.iter().zip(&w) {
        assert_eq!(x, y);
    }
    let key = |f: &ShadowFillRow| (f.shadow_id.clone(), f.fill_seq);
    let mut gf = got.shadow_fills.clone();
    let mut wf = wf;
    gf.sort_by_key(key);
    wf.sort_by_key(key);
    assert_eq!(gf.len(), wf.len());
    for (x, y) in gf.iter().zip(&wf) {
        assert_eq!(x, y);
    }
    assert!(wf.len() > 50);
}

/// Replays a dataset directory (one engine per partition) with a schedule.
fn run_ds(ds: &Path, shadows: &[ShadowSpec]) -> VecSink {
    let insts: Vec<Instrument> = read(&ds.join("instruments.parquet"));
    let day = 86_400_000_000_000i64;
    let mut got = VecSink::default();
    let mut used = 0;
    for f in files(&ds.join("mbo")) {
        let ev: Vec<MboEvent> = read(&f);
        let inst = ev[0].instrument_id;
        let row = insts
            .iter()
            .filter(|i| i.instrument_id == inst && i.midnight_utc_ns <= ev[0].ts_event)
            .max_by_key(|i| i.midnight_utc_ns)
            .unwrap()
            .clone();
        let sh: Vec<ShadowSpec> = shadows
            .iter()
            .filter(|s| {
                s.instrument_id == inst && s.ts_submit >= row.midnight_utc_ns && s.ts_submit < row.midnight_utc_ns + day
            })
            .cloned()
            .collect();
        used += sh.len();
        let mut eng = Engine::new(
            EngineConfig::new(rules(row.venue)),
            std::slice::from_ref(&row),
            sh,
            vec![],
        )
        .unwrap();
        eng.set_sequence_gap_detection(false);
        for e in &ev {
            eng.apply(e, &mut got).unwrap();
        }
        eng.finish(&mut got).unwrap();
    }
    assert_eq!(used, shadows.len(), "{}: every shadow has a partition", ds.display());
    got
}

/// The referee bug-class fixtures (SPEC 5.8): the exact engine reproduces each fixture's expected results and fills.
#[test]
fn o_ref_fixtures_exact_engine_matches_expected() {
    let mut dirs: Vec<PathBuf> = std::fs::read_dir(root().join("referee"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.is_dir())
        .collect();
    dirs.sort();
    assert!(dirs.len() >= 16);
    for d in dirs {
        let schedule: Vec<ShadowSpec> = read(&d.join("schedule.parquet"));
        let got = run_ds(&d.join("dataset"), &schedule);
        let mut want: Vec<ShadowResultRow> = read(&d.join("expected_results.parquet"));
        let mut wf: Vec<ShadowFillRow> = read(&d.join("expected_fills.parquet"));
        let mut g = got.shadow_results.clone();
        g.sort_by(|a, b| a.shadow_id.cmp(&b.shadow_id));
        want.sort_by(|a, b| a.shadow_id.cmp(&b.shadow_id));
        assert_eq!(g, want, "{}", d.display());
        let mut gf = got.shadow_fills.clone();
        let key = |f: &ShadowFillRow| (f.shadow_id.clone(), f.fill_seq);
        gf.sort_by_key(key);
        wf.sort_by_key(key);
        assert_eq!(gf, wf, "{}", d.display());
    }
}
