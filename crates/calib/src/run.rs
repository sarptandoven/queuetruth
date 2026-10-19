//! `calibrate`, Rust part (SPEC 5.4 protocol): collects allocation events and touch records over the train and
//! test windows, writes `alloc_events.parquet`, `touch_records.parquet` and `fit.json` (a `qt.calibration_result`
//! with empty `held_out`, `baseline_held_out` and `exports`; the Python layer adds them).
//!
//! Reporting: power families report `n = exp(theta)` (profile and bootstrap bounds transformed the same way),
//! Databento and survival report `k`, NT touch reports `p`. Bootstrap blocks are session dates (>= 5 dates) or UTC
//! half-hours, as `queuetruth.stats`; allocation rows are blocked by `ts_event`, touch shadows by `ts_live`. Window
//! membership is per shadow: its session date, or its block among `n_blocks` equal-duration blocks of the
//! continuous session (`ts_live` position).
//!
//! Fail-safe (trader review D1, E1; Rust review B1, M5, m24): a window date that is not in the dataset, a train
//! window with no live shadow, a given test window with none, and a fit with no informative events / touches are
//! `QT-DATA-001` errors, never a result. `nt_touch` refuses `l2.cadence_ns != 0` (QT-CONFIG-001: per-touch truth is
//! exact only unconflated). Grid-edge estimates, flat or non-identified likelihoods and zero-width bootstrap
//! intervals are reported as `trust:` lines in `warnings` and in the structured `trust` field (`crate::trust`;
//! a Rust-only result is always `refused` with `no_held_out` until the Python layer adds the held-out comparison),
//! and the export refuses them.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use indexmap::IndexMap;
use qt_ingest::Dataset;
use qt_schema::io::TableWriter;
use qt_schema::rows::{AllocEventRow, TouchRecordRow};
use qt_schema::specs::{CalibrationFamily, CalibrationSpec, ModeName, WindowSpec};
use qt_schema::{codec, json};
use qt_types::{Instrument, QtError, QtResult};
use serde_json::Value;

use crate::collect::{self, CollectConfig, Collected, LiveShadow};
use crate::{AllocFamily, FitOptions, alloc, bootstrap, optim, refs, survival, touch, trust};

/// Minimum informative events of a stratified estimate.
pub const MIN_STRATUM_EVENTS: usize = 1_000;
const TOL: f64 = 1e-7;

/// Parsed train/test window.
#[derive(Clone, Debug, Default)]
pub struct Window {
    dates: Option<BTreeSet<i32>>,
    blocks: Option<(BTreeSet<u32>, u32)>,
}

impl Window {
    /// Parses a `WindowSpec` (`field` names it in errors).
    pub fn parse(w: &WindowSpec, field: &str) -> QtResult<Self> {
        let dates = match &w.dates {
            Some(d) => Some(
                d.iter()
                    .map(|s| qt_ingest::time::parse_date(s))
                    .collect::<QtResult<BTreeSet<i32>>>()
                    .map_err(|e| QtError::Config(format!("{field}.dates: {e}")))?,
            ),
            None => None,
        };
        let blocks = match &w.blocks {
            Some(b) => {
                if b.n_blocks == 0 || b.index.iter().any(|i| *i >= b.n_blocks) {
                    return Err(QtError::Config(format!(
                        "{field}.blocks: indices must be < n_blocks = {}",
                        b.n_blocks
                    )));
                }
                Some((b.index.iter().copied().collect(), b.n_blocks))
            }
            None => None,
        };
        Ok(Self { dates, blocks })
    }

    /// The window's session dates (None = not restricted by date).
    pub fn dates(&self) -> Option<&BTreeSet<i32>> {
        self.dates.as_ref()
    }

    /// True when the window restricts by dates or blocks.
    pub fn is_given(&self) -> bool {
        self.dates.is_some() || self.blocks.is_some()
    }

    /// True when the shadow belongs to the window (no dates and no blocks = all data).
    pub fn contains(&self, s: &LiveShadow, inst: Option<&Instrument>) -> bool {
        if let Some(d) = &self.dates
            && !d.contains(&s.session_date)
        {
            return false;
        }
        if let Some((idx, n)) = &self.blocks {
            return idx.contains(&block_of(s, inst, *n));
        }
        true
    }
}

/// Equal-duration block of the continuous session holding `ts_live` (clamped to `0..n`).
pub fn block_of(s: &LiveShadow, inst: Option<&Instrument>, n: u32) -> u32 {
    let Some(i) = inst else { return 0 };
    let open = i.midnight_utc_ns.saturating_add(i64::from(i.open_s) * 1_000_000_000);
    let len = (i64::from(i.close_s) - i64::from(i.open_s)).max(1) * 1_000_000_000;
    let pos = (s.ts_live.saturating_sub(open)) as f64 / len as f64;
    ((pos * f64::from(n)).floor().max(0.0) as u32).min(n.saturating_sub(1))
}

fn name_of<T: serde::Serialize>(v: &T) -> String {
    serde_json::to_value(v)
        .ok()
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_default()
}

fn finite(x: f64) -> Option<f64> {
    x.is_finite().then_some(x)
}

fn dates_text<'a>(d: impl IntoIterator<Item = &'a i32>) -> String {
    let v: Vec<String> = d.into_iter().map(|x| qt_ingest::time::format_date(*x)).collect();
    if v.is_empty() { "none".into() } else { v.join(", ") }
}

/// Session dates holding data (partitions of the dataset).
pub fn data_dates(ds: &Dataset) -> BTreeSet<i32> {
    ds.partitions().iter().map(|p| p.session_date).collect()
}

/// `QT-DATA-001` when a window names a date that is not in the dataset.
pub fn check_window_dates(w: &Window, field: &str, avail: &BTreeSet<i32>) -> QtResult<()> {
    if let Some(d) = w.dates() {
        let missing: Vec<i32> = d.iter().filter(|x| !avail.contains(x)).copied().collect();
        if !missing.is_empty() {
            return Err(QtError::DataUnusable(format!(
                "{field} window: date(s) {} not in the dataset; available: {}",
                dates_text(&missing),
                dates_text(avail)
            )));
        }
    }
    Ok(())
}

/// What a family fits.
enum Target {
    Alloc(AllocFamily),
    Survival,
    Touch { gated: bool },
}

fn target(spec: &CalibrationSpec) -> QtResult<(Target, &'static str)> {
    use json::CalibrationKind as K;
    Ok(match (spec.kind, spec.family) {
        (K::Alloc, CalibrationFamily::Power) => (Target::Alloc(AllocFamily::Power), "n"),
        (K::Alloc, CalibrationFamily::Power2) => (Target::Alloc(AllocFamily::Power2), "n"),
        (K::Alloc, CalibrationFamily::Power3) => (Target::Alloc(AllocFamily::Power3), "n"),
        (K::Alloc, CalibrationFamily::Databento) => (Target::Alloc(AllocFamily::Databento), "k"),
        (K::Survival, CalibrationFamily::Databento) => (Target::Survival, "k"),
        (K::NtTouch, CalibrationFamily::NtProbTouch) => (Target::Touch { gated: false }, "p"),
        (K::NtTouch, CalibrationFamily::NtQueueL2ProbTouch) => (Target::Touch { gated: true }, "p"),
        (K::Latency, _) => {
            return Err(QtError::Config(
                "kind: latency calibration is computed by the Python layer (queuetruth.calibrate)".into(),
            ));
        }
        (k, f) => {
            return Err(QtError::Config(format!(
                "family: {} is not valid for kind {}",
                name_of(&f),
                name_of(&k)
            )));
        }
    })
}

/// Optimizer options of an allocation family from the spec grids.
pub fn fit_options(spec: &CalibrationSpec, family: AllocFamily) -> FitOptions {
    let g = if family == AllocFamily::Databento {
        &spec.grid.databento
    } else {
        &spec.grid.alloc
    };
    FitOptions {
        grid_lo: g.lo,
        grid_hi: g.hi,
        grid_n: g.n as usize,
        tol: TOL,
    }
}

fn to_param(family: AllocFamily, theta: f64) -> f64 {
    if family == AllocFamily::Databento {
        theta
    } else {
        theta.exp()
    }
}

/// Bootstrap CI of an allocation fit (theta units) over row blocks.
pub fn alloc_bootstrap(
    rows: &[AllocEventRow],
    family: AllocFamily,
    opts: &FitOptions,
    boot: &qt_schema::specs::BootstrapSpec,
    threads: usize,
) -> (f64, f64) {
    let dates: Vec<i32> = rows.iter().map(|r| r.session_date).collect();
    let ts: Vec<i64> = rows.iter().map(|r| r.ts_event).collect();
    let (labels, nb, _) = bootstrap::block_labels(&dates, &ts, boot.block);
    let mut members: Vec<Vec<&AllocEventRow>> = vec![Vec::new(); nb];
    for (r, l) in rows.iter().zip(&labels) {
        members[*l as usize].push(r);
    }
    let thetas = optim::grid_points(opts.grid_lo, opts.grid_hi, opts.grid_n);
    let table: Vec<Vec<f64>> = members
        .iter()
        .map(|m| {
            thetas
                .iter()
                .map(|t| alloc::loglik(m.iter().copied(), family, *t).value)
                .collect()
        })
        .collect();
    let informative: Vec<bool> = members
        .iter()
        .map(|m| m.iter().any(|e| alloc::informative(e)))
        .collect();
    let stat = |draw: &[u32]| -> f64 {
        let c = bootstrap::counts(draw, nb);
        let used: Vec<usize> = (0..nb).filter(|b| c[*b] > 0).collect();
        if !used.iter().any(|b| informative[*b]) {
            // Review m24: a replicate without information has no estimate (not the grid's lower end).
            return f64::NAN;
        }
        let vals: Vec<f64> = (0..thetas.len())
            .map(|g| used.iter().map(|b| f64::from(c[*b]) * table[*b][g]).sum())
            .collect();
        let Some(i) = optim::grid_argmax(&vals) else {
            return f64::NAN;
        };
        let f = |t: f64| -> f64 {
            used.iter()
                .map(|b| f64::from(c[*b]) * alloc::loglik(members[*b].iter().copied(), family, t).value)
                .sum()
        };
        optim::refine(&f, &thetas, &vals, i, opts.tol).0
    };
    let reps = bootstrap::replicates(nb, boot.b, boot.seed, threads, &stat);
    bootstrap::percentile_ci(&reps, 0.05)
}

/// Bootstrap CI of the NT touch MLE over shadow blocks.
pub fn touch_bootstrap(
    rows: &[TouchRecordRow],
    shadows: &[LiveShadow],
    gated: bool,
    boot: &qt_schema::specs::BootstrapSpec,
    threads: usize,
) -> (f64, f64) {
    let with_rows: BTreeSet<&str> = rows.iter().map(|r| r.shadow_id.as_str()).collect();
    let sh: Vec<&LiveShadow> = shadows
        .iter()
        .filter(|s| with_rows.contains(s.shadow_id.as_str()))
        .collect();
    let dates: Vec<i32> = sh.iter().map(|s| s.session_date).collect();
    let ts: Vec<i64> = sh.iter().map(|s| s.ts_live).collect();
    let (labels, nb, _) = bootstrap::block_labels(&dates, &ts, boot.block);
    let block: BTreeMap<&str, u32> = sh
        .iter()
        .zip(&labels)
        .map(|(s, l)| (s.shadow_id.as_str(), *l))
        .collect();
    let mut per: Vec<Vec<TouchRecordRow>> = vec![Vec::new(); nb];
    for r in rows {
        if let Some(b) = block.get(r.shadow_id.as_str()) {
            per[*b as usize].push(r.clone());
        }
    }
    let sums: Vec<(f64, f64)> = per
        .iter()
        .map(|p| {
            let f = touch::fit_nt_touch(p, gated);
            (f.n_filled as f64, f.touches as f64)
        })
        .collect();
    let stat = |draw: &[u32]| -> f64 {
        let (mut num, mut den) = (0.0, 0.0);
        for d in draw {
            num += sums[*d as usize].0;
            den += sums[*d as usize].1;
        }
        if den > 0.0 { num / den } else { f64::NAN }
    };
    let reps = bootstrap::replicates(nb, boot.b, boot.seed, threads, &stat);
    bootstrap::percentile_ci(&reps, 0.05)
}

fn stratified(
    rows: &[AllocEventRow],
    dist: &[u8],
    family: AllocFamily,
    opts: &FitOptions,
) -> Vec<json::StratumEstimate> {
    let mut out = Vec::new();
    let mut by_inst: BTreeMap<u32, Vec<AllocEventRow>> = BTreeMap::new();
    let mut by_dist: BTreeMap<u8, Vec<AllocEventRow>> = BTreeMap::new();
    for (r, d) in rows.iter().zip(dist) {
        by_inst.entry(r.instrument_id).or_default().push(r.clone());
        if *d != 255 {
            by_dist.entry(*d).or_default().push(r.clone());
        }
    }
    let groups = by_inst
        .into_iter()
        .map(|(k, v)| ("instrument", k.to_string(), v))
        .chain(by_dist.into_iter().map(|(k, v)| {
            (
                "distance_ticks",
                if k >= 2 { "2+".to_owned() } else { k.to_string() },
                v,
            )
        }));
    for (kind, stratum, v) in groups {
        if v.iter().filter(|e| alloc::informative(e)).count() < MIN_STRATUM_EVENTS {
            continue;
        }
        let (fit, (lo, hi)) = alloc::fit_alloc_detail(&v, family, opts);
        out.push(json::StratumEstimate {
            stratum_kind: kind.to_owned(),
            stratum,
            estimate: finite(to_param(family, fit.estimate)),
            ci_lo: lo.map(|x| to_param(family, x)).and_then(finite),
            ci_hi: hi.map(|x| to_param(family, x)).and_then(finite),
            n: fit.n_events,
        });
    }
    out
}

fn write_rows(path: &Path, name: &str, batch: &arrow_array::RecordBatch) -> QtResult<()> {
    let mut w = TableWriter::create(path, name)?;
    w.write(batch)?;
    w.finish()?;
    Ok(())
}

/// Collection restricted to the shadows of the train and test windows; returns (rows, train shadow ids).
/// Refuses (QT-DATA-001) window dates missing from the dataset, an empty train window and an empty given test
/// window.
pub fn collect_windows(spec: &CalibrationSpec, ds: &Dataset) -> QtResult<(Collected, BTreeSet<String>)> {
    let train = Window::parse(&spec.train, "train")?;
    let test = Window::parse(&spec.test, "test")?;
    let avail = data_dates(ds);
    check_window_dates(&train, "train", &avail)?;
    check_window_dates(&test, "test", &avail)?;
    let shadows = codec::read_shadow_specs(refs::file_path(spec.shadows.as_ref(), "shadows")?)?;
    let nt = qt_zoo::build(&collect::nt_tracker_id(spec.mode)?)?;
    let cfg = CollectConfig {
        l2: (&spec.l2).into(),
        seed: spec.seed,
        faithful: spec.mode == ModeName::Faithful,
    };
    let col = collect::collect_dataset(ds, shadows, nt.as_ref(), &cfg, spec.threads as usize)?;
    let mut keep = BTreeSet::new();
    let mut train_ids = BTreeSet::new();
    let mut n_test = 0usize;
    for s in &col.shadows {
        let inst = ds.instrument(s.instrument_id, s.session_date);
        let in_train = train.contains(s, inst);
        let in_test = test.contains(s, inst);
        if in_train || in_test {
            keep.insert(s.shadow_id.clone());
        }
        if in_train {
            train_ids.insert(s.shadow_id.clone());
        }
        n_test += usize::from(in_test);
    }
    let live_dates = || dates_text(&col.shadows.iter().map(|s| s.session_date).collect::<BTreeSet<_>>());
    if train_ids.is_empty() {
        return Err(QtError::DataUnusable(format!(
            "train window holds no shadow that went live ({} live shadow(s) in the data, on {}); nothing to fit",
            col.shadows.len(),
            live_dates()
        )));
    }
    if test.is_given() && n_test == 0 {
        return Err(QtError::DataUnusable(format!(
            "test window holds no shadow that went live ({} live shadow(s) in the data, on {}); the fit could not \
             be checked out of sample",
            col.shadows.len(),
            live_dates()
        )));
    }
    let mut out = Collected::default();
    for (r, d) in col.alloc.into_iter().zip(col.alloc_dist) {
        if keep.contains(&r.shadow_id) {
            out.alloc.push(r);
            out.alloc_dist.push(d);
        }
    }
    out.touch = col.touch.into_iter().filter(|r| keep.contains(&r.shadow_id)).collect();
    out.shadows = col
        .shadows
        .into_iter()
        .filter(|s| keep.contains(&s.shadow_id))
        .collect();
    Ok((out, train_ids))
}

/// `calibrate` (Rust part).
pub fn calibrate(spec: &CalibrationSpec) -> QtResult<json::CalibrationResult> {
    let (target, param) = target(spec)?;
    if matches!(target, Target::Touch { .. }) && spec.l2.cadence_ns != 0 {
        // Review M5: per-touch truth is exact only when every event emits its own L2 batch.
        return Err(QtError::Config(format!(
            "l2.cadence_ns: nt_touch calibration needs unconflated L2 (cadence 0), got {} ns; conflated batches \
             lose the touches that filled the shadow and bias p toward 0",
            spec.l2.cadence_ns
        )));
    }
    let out = PathBuf::from(
        spec.out
            .as_deref()
            .ok_or_else(|| QtError::Config("out: calibrate needs an output directory".into()))?,
    );
    let spec_value = serde_json::to_value(spec).map_err(|e| QtError::Engine(format!("calibration spec: {e}")))?;
    let run_id = qt_schema::run_id(&spec_value, qt_schema::ENGINE_VERSION, &refs::resolve)?;
    let ds = Dataset::open(refs::dataset_path(spec.dataset.as_ref(), "dataset")?)?;
    let (col, train_ids) = collect_windows(spec, &ds)?;
    std::fs::create_dir_all(&out).map_err(|e| QtError::InputMissing {
        path: out.display().to_string(),
        msg: e.to_string(),
    })?;
    write_rows(
        &out.join("alloc_events.parquet"),
        "qt.alloc_events",
        &codec::alloc_events_to_batch(&col.alloc),
    )?;
    write_rows(
        &out.join("touch_records.parquet"),
        "qt.touch_records",
        &codec::touch_records_to_batch(&col.touch),
    )?;

    let threads = spec.threads.max(1) as usize;
    let mut warnings = vec![
        "rust part only: held_out, baseline_held_out and exports are filled by the Python layer (calibration.json)"
            .to_owned(),
    ];
    let mut res = json::CalibrationResult {
        run_id,
        spec: match qt_schema::spec_echo(&spec_value) {
            Value::Object(m) => m,
            _ => serde_json::Map::new(),
        },
        kind: spec.kind,
        family: name_of(&spec.family),
        mode: name_of(&spec.mode),
        param_name: Some(param.to_owned()),
        train: spec.train.clone(),
        test: spec.test.clone(),
        held_out: IndexMap::new(),
        baseline_held_out: IndexMap::new(),
        data_sources: vec![json::DataSource {
            dataset_id: ds.manifest.dataset_id.clone(),
            venue: name_of(&ds.manifest.venue),
            source_venue: ds.manifest.source_venue.clone(),
            validation_status: ds.manifest.validation_status,
        }],
        ..Default::default()
    };
    match target {
        Target::Alloc(family) => {
            let (rows, dist): (Vec<AllocEventRow>, Vec<u8>) = col
                .alloc
                .iter()
                .zip(&col.alloc_dist)
                .filter(|(r, _)| train_ids.contains(&r.shadow_id))
                .map(|(r, d)| (r.clone(), *d))
                .unzip();
            let n_informative = rows.iter().filter(|e| alloc::informative(e)).count();
            if n_informative == 0 {
                return Err(QtError::DataUnusable(format!(
                    "no informative allocation events in the train window ({} event row(s) from {} train shadow(s); \
                     an event informs the fit only when there is queue both ahead of and behind the shadow)",
                    rows.len(),
                    train_ids.len()
                )));
            }
            let opts = fit_options(spec, family);
            let (fit, (lo, hi)) = alloc::fit_alloc_detail(&rows, family, &opts);
            if !fit.estimate.is_finite() {
                return Err(QtError::DataUnusable(format!(
                    "allocation likelihood is not finite at any grid value ({n_informative} informative events)"
                )));
            }
            res.estimate = finite(to_param(family, fit.estimate));
            res.loglik = finite(fit.loglik);
            res.ci_profile = Some((lo.map(|x| to_param(family, x)), hi.map(|x| to_param(family, x))));
            let (blo, bhi) = alloc_bootstrap(&rows, family, &opts, &spec.bootstrap, threads);
            let dates: Vec<i32> = rows.iter().map(|r| r.session_date).collect();
            let ts: Vec<i64> = rows.iter().map(|r| r.ts_event).collect();
            let nb = bootstrap::block_labels(&dates, &ts, spec.bootstrap.block).1;
            let (ci, line) = trust::check_ci(
                (finite(to_param(family, blo)), finite(to_param(family, bhi))),
                param,
                nb,
            );
            res.ci_bootstrap = Some(ci);
            let nonfinite = fit.grid.iter().filter(|(_, l)| l.is_nan()).count();
            if nonfinite > 0 {
                warnings.push(format!(
                    "{}nonfinite: the likelihood was not finite at {nonfinite} of {} grid values (excluded)",
                    trust::PREFIX,
                    fit.grid.len()
                ));
            }
            let diag = trust::analyze_grid(&fit.grid, fit.loglik);
            warnings.extend(trust::grid_lines(&diag, &fit.grid, param, |t| to_param(family, t)));
            warnings.extend(line);
            res.n_events = fit.n_events;
            res.n_shadows = rows.iter().map(|r| r.shadow_id.as_str()).collect::<BTreeSet<_>>().len() as u64;
            res.stratified = stratified(&rows, &dist, family, &opts);
            warnings.extend(trust::disjoint_strata(&res.stratified, "instrument", param));
            for (name, f) in [
                ("hbt_log", alloc::FixedFamily::Log),
                ("hbt_log2", alloc::FixedFamily::Log2),
            ] {
                let l = alloc::loglik_fixed(&rows, f);
                warnings.push(format!(
                    "info: comparison quasi log likelihood {name} = {} ({} events)",
                    l.value, l.used
                ));
            }
        }
        Target::Touch { gated } => {
            let rows: Vec<TouchRecordRow> = col
                .touch
                .iter()
                .filter(|r| train_ids.contains(&r.shadow_id))
                .cloned()
                .collect();
            let fit = touch::fit_nt_touch(&rows, gated);
            if fit.touches == 0 || !fit.p_hat.is_finite() {
                return Err(QtError::DataUnusable(format!(
                    "no {}touches in the train window ({} train shadow(s)); prob_fill_on_limit cannot be fitted",
                    if gated { "queue-gated " } else { "" },
                    train_ids.len()
                )));
            }
            res.estimate = finite(fit.p_hat);
            res.n_events = fit.touches;
            res.n_shadows = fit.n_orders;
            let train_sh: Vec<LiveShadow> = col
                .shadows
                .iter()
                .filter(|s| train_ids.contains(&s.shadow_id))
                .cloned()
                .collect();
            let (lo, hi) = touch_bootstrap(&rows, &train_sh, gated, &spec.bootstrap, threads);
            let dates: Vec<i32> = train_sh.iter().map(|s| s.session_date).collect();
            let ts: Vec<i64> = train_sh.iter().map(|s| s.ts_live).collect();
            let nb = bootstrap::block_labels(&dates, &ts, spec.bootstrap.block).1;
            let (ci, line) = trust::check_ci((finite(lo), finite(hi)), param, nb);
            res.ci_bootstrap = Some(ci);
            if fit.n_filled == 0 || fit.p_hat >= 1.0 {
                let (end, what) = if fit.n_filled == 0 {
                    ("lower", format!("no touch filled a shadow ({} touches)", fit.touches))
                } else {
                    (
                        "upper",
                        format!("every counted touch filled ({} of {})", fit.n_filled, fit.touches),
                    )
                };
                warnings.push(format!(
                    "{}at_grid_edge: p = {} is the {end} end of the parameter range: {what}",
                    trust::PREFIX,
                    trust::g6(fit.p_hat)
                ));
            }
            warnings.extend(line);
            warnings.push("info: p is per touch and not portable across feeds with different print rates".into());
        }
        Target::Survival => {
            let sim = qt_zoo::particle_simulator(spec)?;
            let g = &spec.grid.survival;
            let thetas = optim::grid_points(g.lo, g.hi, g.n as usize);
            let fit = survival::fit_survival(sim.as_ref(), &thetas, spec.seed, true)?;
            if !fit.estimate.is_finite() {
                return Err(QtError::DataUnusable(format!(
                    "survival likelihood is not finite at any grid value ({} train shadow(s))",
                    train_ids.len()
                )));
            }
            res.estimate = finite(fit.estimate);
            res.loglik = finite(fit.loglik);
            res.n_shadows = train_ids.len() as u64;
            res.n_events = train_ids.len() as u64;
            let diag = trust::analyze_grid(&fit.grid, fit.loglik);
            warnings.extend(trust::grid_lines(&diag, &fit.grid, param, |t| t));
            warnings.push("info: survival fit has no bootstrap CI in v0.1 (one simulation per grid point)".into());
        }
    }
    res.warnings = warnings;
    res.trust = Some(trust::assess_rust(&res));
    let text = serde_json::to_string_pretty(&res).map_err(|e| QtError::Engine(format!("fit.json: {e}")))? + "\n";
    let path = out.join("fit.json");
    std::fs::write(&path, text).map_err(|e| QtError::InputMissing {
        path: path.display().to_string(),
        msg: e.to_string(),
    })?;
    Ok(res)
}
