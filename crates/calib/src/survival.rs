//! C2 survival likelihood (SPEC 5.4). Each `ShadowCurve.survival[j]` is the smoothed particle survival
//! `S_ij = (#no fill by tau_j + 0.5) / (N + 1)` produced by the simulator; `S_i0 = 1`, `h_ij = 1 - S_ij / S_i,j-1`.
//! `l = sum_i [sum_{j<K_i} ln(1 - h_ij) + delta_i ln h_iK + (1 - delta_i) ln(1 - h_iK)]` with `K_i` the interval of
//! the exact first fill (`truth_interval`, delta = 1) or the last fully observed interval (`censored_from - 1`,
//! delta = 0; no censoring = all intervals observed). A fill at or after `censored_from` is censored there like an
//! unfilled shadow (audit A2: censoring at the planned end is outcome-independent). Hazards are clamped to `[1e-12, 1 - 1e-12]` because the
//! smoothing keeps `S` positive but not strictly decreasing. Every theta uses the same seed (common random numbers).

use std::cell::RefCell;

use qt_types::{QtError, QtResult};
use qt_zoo::{ParticleSimulator, ShadowCurve};

use crate::SurvivalFit;
use crate::optim;

const H_CLIP: f64 = 1e-12;

fn clamp_h(h: f64) -> f64 {
    if h.is_nan() {
        H_CLIP
    } else {
        h.clamp(H_CLIP, 1.0 - H_CLIP)
    }
}

/// Log likelihood contribution of one shadow curve.
pub fn curve_loglik(c: &ShadowCurve) -> f64 {
    let j_max = c.survival.len();
    // Audit A2 (review M4): an administrative end at `censored_from` censors the shadow there whatever its outcome;
    // a fill at or after that interval is not an event (the interval is only partly observed).
    let (k, delta) = match (c.truth_interval, c.censored_from) {
        (Some(k), Some(cf)) if k >= cf => match cf.min(j_max).checked_sub(1) {
            Some(k) => (k, false),
            None => return 0.0,
        },
        (Some(k), _) if k < j_max => (k, true),
        (Some(_), _) | (None, None) => match j_max.checked_sub(1) {
            Some(k) => (k, false),
            None => return 0.0,
        },
        (None, Some(cf)) => match cf.min(j_max).checked_sub(1) {
            Some(k) => (k, false),
            None => return 0.0,
        },
    };
    let mut s_prev = 1.0f64;
    let mut l = 0.0;
    for j in 0..=k {
        let s = c.survival[j];
        let h = clamp_h(1.0 - s / s_prev);
        if j < k || !delta {
            l += (1.0 - h).ln();
        } else {
            l += h.ln();
        }
        s_prev = s;
    }
    l
}

/// Log likelihood of a set of curves (sum in shadow order).
pub fn loglik(curves: &[ShadowCurve]) -> f64 {
    curves.iter().map(curve_loglik).sum()
}

/// Grid argmax over `thetas` with the same `seed` for every theta, then an optional golden refinement.
pub fn fit_survival(sim: &dyn ParticleSimulator, thetas: &[f64], seed: u64, refine: bool) -> QtResult<SurvivalFit> {
    if thetas.is_empty() {
        return Err(QtError::Config("survival grid: needs at least one theta".into()));
    }
    let err: RefCell<Option<QtError>> = RefCell::new(None);
    let f = |theta: f64| -> f64 {
        if err.borrow().is_some() {
            return f64::NAN;
        }
        match sim.simulate(theta, seed) {
            Ok(c) => loglik(&c),
            Err(e) => {
                *err.borrow_mut() = Some(e);
                f64::NAN
            }
        }
    };
    let values: Vec<f64> = thetas.iter().map(|t| f(*t)).collect();
    if let Some(e) = err.borrow_mut().take() {
        return Err(e);
    }
    let grid: Vec<(f64, f64)> = thetas.iter().copied().zip(values.iter().copied()).collect();
    let Some(i) = optim::grid_argmax(&values) else {
        return Ok(SurvivalFit {
            estimate: f64::NAN,
            loglik: f64::NAN,
            grid,
        });
    };
    let (mut estimate, mut ll) = (thetas[i], values[i]);
    if refine && thetas.len() > 1 {
        let step = (thetas[thetas.len() - 1] - thetas[0]).abs() / (thetas.len() - 1) as f64;
        let (x, fx) = optim::refine(&f, thetas, &values, i, (step * 1e-3).max(1e-9));
        if let Some(e) = err.borrow_mut().take() {
            return Err(e);
        }
        (estimate, ll) = (x, fx);
    }
    Ok(SurvivalFit {
        estimate,
        loglik: ll,
        grid,
    })
}
