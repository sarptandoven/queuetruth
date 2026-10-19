//! C1 allocation quasi-likelihood (SPEC 5.4): `l(theta) = sum_j [b_j ln pi + a_j ln(1 - pi)]` at the exact
//! `(F*, B*)` of each cancel event, `pi` = probability that a decrease is behind the order (hbt convention), clipped
//! to `[1e-12, 1 - 1e-12]`. Events with `front <= 0` or `back <= 0` carry no information and are skipped. hbt
//! families use `theta = ln n`, Databento `theta = k`.
//!
//! Scale invariance (review B1): `pi` is evaluated from queue shares, never from raw powers of the sizes. hbt's
//! `b^n / (b^n + f^n)` overflows to `inf / inf` once `n ln(size) > 709` (crypto size units, 1 BTC = 1e8), which
//! used to skip every event at large `n` and let an empty sum (`l = 0`) win the argmax. Here power is
//! `1 / (1 + exp(n (ln f - ln b)))` (stable logistic), power2 `exp(n (ln b - ln(b + f)))`, power3
//! `1 - (f / (f + b))^n`: the same functions of `(F, B)` up to rounding, unchanged under `(F, B) -> (cF, cB)`.
//! (`qt_zoo::prob` keeps hbt's exact expressions for faithful replay.) If any informative event gives a non-finite
//! `pi` at some `theta`, the whole `l(theta)` is NaN (never a sum over a theta-dependent subset), and the count is
//! reported.

use qt_schema::rows::AllocEventRow;
use qt_zoo::prob;

use crate::optim;
use crate::{AllocFamily, AllocFit, FitOptions};

/// Lower clip of `pi`.
pub const PI_CLIP: f64 = 1e-12;

/// Numerically stable logistic `1 / (1 + exp(-z))`.
fn logistic(z: f64) -> f64 {
    if z >= 0.0 {
        1.0 / (1.0 + (-z).exp())
    } else {
        let e = z.exp();
        e / (1.0 + e)
    }
}

/// hbt power `b^n / (b^n + f^n)` from queue shares (scale-invariant; see the module doc).
pub fn power_pi(n: f64, front: f64, back: f64) -> f64 {
    logistic(n * (back.ln() - front.ln()))
}

/// hbt power2 `b^n / (b + f)^n` from queue shares (scale-invariant).
pub fn power2_pi(n: f64, front: f64, back: f64) -> f64 {
    (n * (back.ln() - (back + front).ln())).exp()
}

/// `pi_theta(front, back)` for one family (unclipped, scale-invariant).
pub fn pi_behind(family: AllocFamily, theta: f64, front: f64, back: f64) -> f64 {
    match family {
        AllocFamily::Power => power_pi(theta.exp(), front, back),
        AllocFamily::Power2 => power2_pi(theta.exp(), front, back),
        AllocFamily::Power3 => prob::power3(theta.exp(), front, back),
        AllocFamily::Databento => 1.0 - prob::db_p_front(theta, front / (front + back)),
    }
}

/// Parameter-free comparison families (`hbt_log`, `hbt_log2`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FixedFamily {
    /// hbt LogProbQueueFunc.
    Log,
    /// hbt LogProbQueueFunc2.
    Log2,
}

/// Quasi log likelihood with its event counts.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct LogLik {
    /// Sum of the terms (NaN when any informative event had a non-finite `pi`).
    pub value: f64,
    /// Events that contributed.
    pub used: u64,
    /// Informative events whose `pi` was not finite (they make `value` NaN).
    pub nonfinite: u64,
}

/// True when the event carries allocation information (`front > 0`, `back > 0`, finite volumes).
pub fn informative(e: &AllocEventRow) -> bool {
    e.front > 0.0
        && e.back > 0.0
        && e.front.is_finite()
        && e.back.is_finite()
        && e.ahead_cancel.is_finite()
        && e.behind_cancel.is_finite()
}

fn term(pi: f64, e: &AllocEventRow, acc: &mut LogLik) {
    if !pi.is_finite() {
        acc.nonfinite += 1;
        return;
    }
    let pi = pi.clamp(PI_CLIP, 1.0 - PI_CLIP);
    acc.value += e.behind_cancel * pi.ln() + e.ahead_cancel * (1.0 - pi).ln();
    acc.used += 1;
}

fn finish(mut acc: LogLik) -> LogLik {
    if acc.nonfinite > 0 {
        acc.value = f64::NAN;
    }
    acc
}

/// Quasi log likelihood of `family` at `theta` over `events` (in iteration order).
pub fn loglik<'a>(events: impl IntoIterator<Item = &'a AllocEventRow>, family: AllocFamily, theta: f64) -> LogLik {
    let mut acc = LogLik::default();
    match family {
        AllocFamily::Databento => {
            for e in events.into_iter().filter(|e| informative(e)) {
                term(1.0 - prob::db_p_front(theta, e.front / (e.front + e.back)), e, &mut acc);
            }
        }
        _ => {
            let n = theta.exp();
            let f: fn(f64, f64, f64) -> f64 = match family {
                AllocFamily::Power => power_pi,
                AllocFamily::Power2 => power2_pi,
                _ => prob::power3,
            };
            for e in events.into_iter().filter(|e| informative(e)) {
                term(f(n, e.front, e.back), e, &mut acc);
            }
        }
    }
    finish(acc)
}

/// Quasi log likelihood of a parameter-free family (reported for comparison).
pub fn loglik_fixed<'a>(events: impl IntoIterator<Item = &'a AllocEventRow>, family: FixedFamily) -> LogLik {
    let mut acc = LogLik::default();
    for e in events.into_iter().filter(|e| informative(e)) {
        let pi = match family {
            FixedFamily::Log => prob::log(e.front, e.back),
            FixedFamily::Log2 => prob::log2(e.front, e.back),
        };
        term(pi, e, &mut acc);
    }
    finish(acc)
}

/// Grid + golden fit of `family` with the quasi profile interval (each side None when open). Without informative
/// events the estimate is NaN (review m24: an all-zero likelihood must not publish the grid's lower end).
pub fn fit_alloc_detail(
    events: &[AllocEventRow],
    family: AllocFamily,
    opts: &FitOptions,
) -> (AllocFit, (Option<f64>, Option<f64>)) {
    let thetas = optim::grid_points(opts.grid_lo, opts.grid_hi, opts.grid_n);
    if !events.iter().any(informative) {
        let grid = thetas.iter().map(|t| (*t, f64::NAN)).collect();
        let fit = AllocFit {
            family,
            estimate: f64::NAN,
            loglik: f64::NAN,
            n_events: 0,
            profile_ci: None,
            grid,
        };
        return (fit, (None, None));
    }
    let f = |t: f64| loglik(events, family, t).value;
    let fit = optim::fit_curve(f, &thetas, opts.tol);
    let ci = optim::profile_interval(f, &fit, opts.tol);
    let n_events = if fit.estimate.is_finite() {
        loglik(events, family, fit.estimate).used
    } else {
        events.iter().filter(|e| informative(e)).count() as u64
    };
    let profile_ci = match ci {
        (Some(lo), Some(hi)) => Some((lo, hi)),
        _ => None,
    };
    (
        AllocFit {
            family,
            estimate: fit.estimate,
            loglik: fit.max,
            n_events,
            profile_ci,
            grid: fit.grid,
        },
        ci,
    )
}

/// Grid + golden-section maximization of `alloc_loglik` with the profile CI.
pub fn fit_alloc(events: &[AllocEventRow], family: AllocFamily, opts: &FitOptions) -> AllocFit {
    fit_alloc_detail(events, family, opts).0
}
