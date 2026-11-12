//! The live-fill likelihood of `calibrate-live` (C2): one order's interval-censored log-likelihood under a model's
//! fill-probability curve, and the order-by-grid matrix the grid search maximizes.
//!
//! A model gives `P(fill by tau_j)` for the horizons `tau_1 < ... < tau_J`. The survival `1 - P` is smoothed toward
//! one half by `n_smooth` (a Monte Carlo model with `n` particles cannot say 0 or 1), turned into discrete hazards
//! clipped to `[1e-12, 1 - 1e-12]`, and the order's truth interval `k` (1-based) with `delta` (1 filled in
//! `(tau_{k-1}, tau_k]`, 0 still open at `tau_k`) picks the terms. The sum over the survived intervals follows
//! NumPy's pairwise summation, so the values match the earlier NumPy code to the last bit wherever `ln` does.

use qt_types::{QtError, QtResult};

const H_MIN: f64 = 1e-12;
const H_MAX: f64 = 1.0 - 1e-12;

/// NumPy's pairwise sum of a contiguous `f64` slice (`np.sum` of a 1-d array): eight accumulators per block of at
/// most 128 values, halves above that.
pub fn pairwise_sum(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0;
        for &x in a {
            res += x;
        }
        res
    } else if n <= 128 {
        let mut r = [0.0f64; 8];
        r.copy_from_slice(&a[..8]);
        let mut i = 8;
        while i < n - (n % 8) {
            for (j, rj) in r.iter_mut().enumerate() {
                *rj += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        for &x in &a[i..] {
            res += x;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
    }
}

/// C2 log-likelihood of one order: `p_by_h` = model `P(fill by tau_j)`, j = 1..J (NaN-free); truth interval `k`
/// (1-based) with `delta` (1 filled, 0 open); `n_smooth` the model's particle count (1 for a deterministic model).
pub fn interval_loglik(p_by_h: &[f64], k: usize, delta: bool, n_smooth: u64) -> QtResult<f64> {
    let j = p_by_h.len();
    if k == 0 || k > j {
        return Err(QtError::Config(format!(
            "interval_loglik: interval {k} is outside 1..={j} (one per horizon)"
        )));
    }
    let n = n_smooth as f64;
    let n1 = (n_smooth + 1) as f64;
    let mut terms = Vec::with_capacity(k - 1);
    let mut prev = 1.0f64;
    let mut last = 0.0f64;
    for (i, &p) in p_by_h.iter().take(k).enumerate() {
        let s = ((1.0 - p).clamp(0.0, 1.0) * n + 0.5) / n1;
        let h = (1.0 - s / prev).clamp(H_MIN, H_MAX);
        prev = s;
        if i + 1 < k {
            terms.push((1.0 - h).ln());
        } else {
            last = h;
        }
    }
    let mut ll = pairwise_sum(&terms);
    ll += if delta { last.ln() } else { (1.0 - last).ln() };
    Ok(ll)
}

/// The order-by-grid log-likelihood matrix of the grid search, row-major (`orders x grid`). `p` holds `J` fill
/// probabilities per (order, grid value), row-major `orders x grid x J`; a cell whose `present` flag is false (the
/// model has no prediction for that order) scores 0. `k` and `delta` are per order.
pub fn loglik_matrix(
    p: &[f64],
    present: &[bool],
    n_orders: usize,
    n_grid: usize,
    k: &[usize],
    delta: &[bool],
    n_smooth: u64,
) -> QtResult<Vec<f64>> {
    let cells = n_orders * n_grid;
    if present.len() != cells || k.len() != n_orders || delta.len() != n_orders {
        return Err(QtError::Config(format!(
            "loglik_matrix: {n_orders} orders x {n_grid} grid values need {cells} flags and {n_orders} intervals \
             (got {} flags, {} intervals, {} deltas)",
            present.len(),
            k.len(),
            delta.len()
        )));
    }
    if cells == 0 {
        return Ok(Vec::new());
    }
    if !p.len().is_multiple_of(cells) {
        return Err(QtError::Config(format!(
            "loglik_matrix: {} probabilities do not split into {cells} cells",
            p.len()
        )));
    }
    let j = p.len() / cells;
    let mut out = Vec::with_capacity(cells);
    for i in 0..n_orders {
        for g in 0..n_grid {
            let c = i * n_grid + g;
            out.push(if present[c] {
                interval_loglik(&p[c * j..(c + 1) * j], k[i], delta[i], n_smooth)?
            } else {
                0.0
            });
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hand_computation() {
        let p = [0.2, 0.5, 0.9];
        let s: Vec<f64> = p.iter().map(|x| ((1.0 - x) * 1.0 + 0.5) / 2.0).collect();
        let h = [1.0 - s[0], 1.0 - s[1] / s[0], 1.0 - s[2] / s[1]];
        let got = interval_loglik(&p, 2, true, 1).unwrap();
        assert!((got - ((1.0 - h[0]).ln() + h[1].ln())).abs() < 1e-15);
        let got = interval_loglik(&p, 3, false, 1).unwrap();
        let want = (1.0 - h[0]).ln() + (1.0 - h[1]).ln() + (1.0 - h[2]).ln();
        assert!((got - want).abs() < 1e-15);
        assert!(interval_loglik(&p, 0, true, 1).is_err());
        assert!(interval_loglik(&p, 4, true, 1).is_err());
    }

    #[test]
    fn pairwise_sum_matches_the_numpy_blocks() {
        let a: Vec<f64> = (0..300).map(|i| 1.0 / (i as f64 + 1.0)).collect();
        // below 8 values: a plain left fold
        assert_eq!(pairwise_sum(&a[..5]), (((a[0] + a[1]) + a[2]) + a[3]) + a[4]);
        // 8 to 128: eight accumulators then the tail
        let r: Vec<f64> = (0..8).map(|j| a[j] + a[8 + j]).collect();
        let want = (((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]))) + a[16];
        assert_eq!(pairwise_sum(&a[..17]), want);
        // above 128: split at a multiple of 8
        assert_eq!(pairwise_sum(&a), pairwise_sum(&a[..144]) + pairwise_sum(&a[144..]));
    }

    #[test]
    fn matrix_scores_missing_cells_zero() {
        let p = [0.1, 0.4, 0.3, 0.8];
        let m = loglik_matrix(&p, &[true, false], 1, 2, &[2], &[true], 1).unwrap();
        assert_eq!(m[0], interval_loglik(&p[..2], 2, true, 1).unwrap());
        assert_eq!(m[1], 0.0);
        assert!(loglik_matrix(&p, &[true], 1, 2, &[2], &[true], 1).is_err());
    }
}
