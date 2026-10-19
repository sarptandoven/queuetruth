//! One-dimensional maximization: evenly spaced grid, golden-section refinement on the grid neighbours of the grid
//! argmax, and the quasi profile-likelihood interval `{theta: 2 (l_max - l(theta)) <= 3.841459}` by bisection.
//! NaN objective values count as `-inf`.

/// chi-square(1) 95% quantile used by the profile interval (SPEC 5.4 C1).
pub const PROFILE_CRIT: f64 = 3.841459;

const INV_PHI: f64 = 0.618_033_988_749_894_9;
const MAX_ITER: usize = 2_000;

fn val(x: f64) -> f64 {
    if x.is_nan() { f64::NEG_INFINITY } else { x }
}

/// `n` evenly spaced points from `lo` to `hi` (both included; `n = 1` gives `[lo]`).
pub fn grid_points(lo: f64, hi: f64, n: usize) -> Vec<f64> {
    match n {
        0 => Vec::new(),
        1 => vec![lo],
        _ => (0..n)
            .map(|i| {
                if i + 1 == n {
                    hi
                } else {
                    lo + (hi - lo) * (i as f64) / ((n - 1) as f64)
                }
            })
            .collect(),
    }
}

/// Golden-section maximization of a unimodal `f` on `[lo, hi]` until the bracket is narrower than `tol`;
/// returns the bracket midpoint and `f` there.
pub fn golden_section_max(f: impl Fn(f64) -> f64, lo: f64, hi: f64, tol: f64) -> (f64, f64) {
    let (mut a, mut b) = if lo <= hi { (lo, hi) } else { (hi, lo) };
    let tol = if tol.is_finite() && tol > 0.0 { tol } else { 1e-7 };
    if !(a.is_finite() && b.is_finite()) {
        return (f64::NAN, f64::NAN);
    }
    let mut c = b - INV_PHI * (b - a);
    let mut d = a + INV_PHI * (b - a);
    let mut fc = val(f(c));
    let mut fd = val(f(d));
    let mut it = 0;
    while b - a > tol && it < MAX_ITER {
        if fc >= fd {
            b = d;
            d = c;
            fd = fc;
            c = b - INV_PHI * (b - a);
            fc = val(f(c));
        } else {
            a = c;
            c = d;
            fc = fd;
            d = a + INV_PHI * (b - a);
            fd = val(f(d));
        }
        it += 1;
    }
    let x = 0.5 * (a + b);
    (x, f(x))
}

/// Result of a grid + golden fit.
#[derive(Clone, Debug, PartialEq)]
pub struct CurveFit {
    /// Argmax (NaN when every value is `-inf`/NaN or the grid is empty).
    pub estimate: f64,
    /// Objective at the estimate.
    pub max: f64,
    /// (theta, value) on the grid.
    pub grid: Vec<(f64, f64)>,
}

/// Index of the first grid maximum (None if all values are `-inf`/NaN).
pub fn grid_argmax(values: &[f64]) -> Option<usize> {
    let mut best: Option<(usize, f64)> = None;
    for (i, v) in values.iter().map(|v| val(*v)).enumerate() {
        if v == f64::NEG_INFINITY {
            continue;
        }
        if best.is_none_or(|(_, b)| v > b) {
            best = Some((i, v));
        }
    }
    best.map(|(i, _)| i)
}

/// Golden refinement on `[theta_{i-1}, theta_{i+1}]` (clamped at the grid ends) of a grid argmax `i`; the grid
/// point is kept when the refinement does not improve on it.
pub fn refine(f: &impl Fn(f64) -> f64, thetas: &[f64], values: &[f64], i: usize, tol: f64) -> (f64, f64) {
    let lo = thetas[i.saturating_sub(1)];
    let hi = thetas[(i + 1).min(thetas.len() - 1)];
    let grid_best = (thetas[i], values[i]);
    if lo == hi {
        return grid_best;
    }
    let (x, fx) = golden_section_max(f, lo, hi, tol);
    if val(fx) >= val(grid_best.1) {
        (x, fx)
    } else {
        grid_best
    }
}

/// Maximizes `f` on the grid, then refines with golden section to `tol` (`tol <= 0` disables refinement).
pub fn fit_curve(f: impl Fn(f64) -> f64, thetas: &[f64], tol: f64) -> CurveFit {
    let values: Vec<f64> = thetas.iter().map(|t| f(*t)).collect();
    let grid: Vec<(f64, f64)> = thetas.iter().copied().zip(values.iter().copied()).collect();
    let Some(i) = grid_argmax(&values) else {
        return CurveFit {
            estimate: f64::NAN,
            max: f64::NAN,
            grid,
        };
    };
    let (estimate, max) = if tol > 0.0 {
        refine(&f, thetas, &values, i, tol)
    } else {
        (thetas[i], values[i])
    };
    CurveFit { estimate, max, grid }
}

fn bisect(f: &impl Fn(f64) -> f64, inside: f64, outside: f64, target: f64, tol: f64) -> f64 {
    // f(inside) >= target > f(outside); returns the crossing point.
    let (mut a, mut b) = (inside, outside);
    let mut it = 0;
    while (b - a).abs() > tol && it < MAX_ITER {
        let m = 0.5 * (a + b);
        if val(f(m)) >= target {
            a = m;
        } else {
            b = m;
        }
        it += 1;
    }
    0.5 * (a + b)
}

/// Quasi profile interval of a fit: each side is None when no grid point on that side leaves the region.
pub fn profile_interval(f: impl Fn(f64) -> f64, fit: &CurveFit, tol: f64) -> (Option<f64>, Option<f64>) {
    if !fit.estimate.is_finite() || !fit.max.is_finite() {
        return (None, None);
    }
    let target = fit.max - PROFILE_CRIT / 2.0;
    let tol = if tol > 0.0 { tol } else { 1e-7 };
    let est = fit.estimate;
    let mut lower = None;
    let mut inside = est;
    for &(t, v) in fit.grid.iter().rev().filter(|(t, _)| *t < est) {
        if val(v) < target {
            lower = Some(bisect(&f, inside, t, target, tol));
            break;
        }
        inside = t;
    }
    let mut upper = None;
    let mut inside = est;
    for &(t, v) in fit.grid.iter().filter(|(t, _)| *t > est) {
        if val(v) < target {
            upper = Some(bisect(&f, inside, t, target, tol));
            break;
        }
        inside = t;
    }
    (lower, upper)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grid_endpoints_exact() {
        let g = grid_points(-3.0, 4.0, 141);
        assert_eq!(g.len(), 141);
        assert_eq!(g[0], -3.0);
        assert_eq!(g[140], 4.0);
        assert!((g[1] - g[0] - 0.05).abs() < 1e-12);
        assert_eq!(grid_points(1.0, 2.0, 1), vec![1.0]);
        assert!(grid_points(1.0, 2.0, 0).is_empty());
    }

    #[test]
    fn argmax_skips_nan_and_takes_first() {
        assert_eq!(grid_argmax(&[f64::NAN, 1.0, 3.0, 3.0, f64::NEG_INFINITY]), Some(2));
        assert_eq!(grid_argmax(&[f64::NAN, f64::NEG_INFINITY]), None);
    }

    #[test]
    fn profile_interval_of_quadratic() {
        // l = -(t - 1)^2 / (2 * 0.25): 2 (lmax - l) = (t - 1)^2 / 0.25 <= 3.841459 -> t = 1 +- 0.5 sqrt(3.841459).
        let f = |t: f64| -(t - 1.0) * (t - 1.0) / 0.5;
        let fit = fit_curve(f, &grid_points(-3.0, 4.0, 141), 1e-9);
        assert!((fit.estimate - 1.0).abs() < 1e-7);
        let (lo, hi) = profile_interval(f, &fit, 1e-10);
        let half = 0.5 * PROFILE_CRIT.sqrt();
        assert!((lo.unwrap_or(f64::NAN) - (1.0 - half)).abs() < 1e-8);
        assert!((hi.unwrap_or(f64::NAN) - (1.0 + half)).abs() < 1e-8);
        // Open side: the region reaches the grid end.
        let wide = |t: f64| -(t - 3.9) * (t - 3.9) / 10.0;
        let fit = fit_curve(wide, &grid_points(-3.0, 4.0, 141), 1e-9);
        let (lo, hi) = profile_interval(wide, &fit, 1e-10);
        assert!(lo.is_some());
        assert!(hi.is_none());
    }
}
