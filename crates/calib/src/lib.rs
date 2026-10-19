//! Calibration of queue models against exact truth (SPEC 4.1 `qt_calib`, 5.4): allocation-event collection,
//! allocation quasi-likelihood for `n` / `k`, survival likelihood, NT touch MLE, block bootstrap, grid + golden
//! section optimizer.
//!
//! Audit rulings applied (docs/concepts/audit-rules.md): A7, shadows first filled by a
//! trade-through or cross are censored at that fill in the NT touch MLE (not excluded); A2, likelihoods use only
//! outcome-independent censoring (the survival fit takes `censored_from` from the simulator's planned ends, and a
//! fill at or after it is censored there).
//!
//! Fail-safe (trader review D1, D2, E1): `calibrate` refuses empty or degenerate fits with `QT-DATA-001` and reports
//! grid-edge, flat and non-identified estimates as `trust:` warnings (module `trust`) that the export refuses.
//! The allocation likelihood is evaluated from queue shares, so it is invariant to the size unit (review B1).

#![forbid(unsafe_code)]

pub mod alloc;
pub mod bootstrap;
pub mod collect;
mod model;
pub mod optim;
pub mod refs;
pub mod run;
pub mod survival;
pub mod touch;
pub mod trust;

#[cfg(test)]
mod tests;

pub use model::*;

use qt_schema::rows::{AllocEventRow, TouchRecordRow};
use qt_schema::{json, specs};
use qt_types::QtResult;
use qt_zoo::ParticleSimulator;

/// Collects `qt.alloc_events` and `qt.touch_records` over the train and test windows.
pub fn collect(spec: &specs::CalibrationSpec) -> QtResult<(Vec<AllocEventRow>, Vec<TouchRecordRow>)> {
    let ds = qt_ingest::Dataset::open(refs::dataset_path(spec.dataset.as_ref(), "dataset")?)?;
    let (c, _) = run::collect_windows(spec, &ds)?;
    Ok((c.alloc, c.touch))
}

/// Allocation quasi log likelihood (C1).
pub fn alloc_loglik(events: &[AllocEventRow], family: AllocFamily, theta: f64) -> f64 {
    alloc::loglik(events, family, theta).value
}

/// Grid + golden-section maximization of `alloc_loglik` with the profile CI.
pub fn fit_alloc(events: &[AllocEventRow], family: AllocFamily, opts: &FitOptions) -> AllocFit {
    alloc::fit_alloc(events, family, opts)
}

/// Closed-form NT touch MLE (C3).
pub fn fit_nt_touch(records: &[TouchRecordRow], queue_gated: bool) -> TouchFit {
    touch::fit_nt_touch(records, queue_gated)
}

/// Survival likelihood fit over `thetas` with common random numbers (C2).
pub fn fit_survival(sim: &dyn ParticleSimulator, thetas: &[f64], seed: u64, refine: bool) -> QtResult<SurvivalFit> {
    survival::fit_survival(sim, thetas, seed, refine)
}

/// Golden-section maximization on `[lo, hi]` to `tol`; returns (argmax, max).
pub fn golden_section_max(f: impl Fn(f64) -> f64, lo: f64, hi: f64, tol: f64) -> (f64, f64) {
    optim::golden_section_max(f, lo, hi, tol)
}

/// Percentile (2.5, 97.5) block bootstrap of `stat` over resampled block indices.
pub fn block_bootstrap(n_blocks: usize, b: u32, seed: u64, stat: impl Fn(&[u32]) -> f64 + Sync) -> (f64, f64) {
    bootstrap::block_bootstrap(n_blocks, b, seed, stat)
}

/// `calibrate` (Rust part, kinds alloc | survival | nt_touch): writes alloc_events, touch_records and fit.json.
pub fn calibrate(spec: &specs::CalibrationSpec) -> QtResult<json::CalibrationResult> {
    run::calibrate(spec)
}
