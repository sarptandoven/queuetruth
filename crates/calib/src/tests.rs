//! Unit oracles of SPEC 6.4 that need no other crate's component: O-CAL-1, O-CAL-2, O-CAL-3, O-CAL-5 (optimizer),
//! the bootstrap stream parity with `tests/fixtures/reference/bootstrap.json`, and survival recovery on a local
//! mock `ParticleSimulator`. Tolerances follow finding 18 of the specification audit (`|ln n_hat - ln n0| < 0.02`,
//! `|k_hat - k0| < 0.012`, `|p_hat - p0| < 0.005`); the profile-CI coverage assertions use pinned seeds
//! (`1000 + 10 * family + n0 index`), recorded here as the audit asks.

use qt_schema::rows::{AllocEventRow, TouchRecordRow};
use qt_types::{Dur, QtResult, Xoshiro256StarStar, stable_seed};
use qt_zoo::{ParticleSimulator, ShadowCurve};

use crate::{AllocFamily, FitOptions, alloc, bootstrap, optim, survival, touch, trust};

fn event(front: f64, back: f64, ahead: f64, behind: f64) -> AllocEventRow {
    AllocEventRow {
        shadow_id: "s".into(),
        instrument_id: 1,
        session_date: 0,
        ts_event: 0,
        front,
        back,
        ahead_cancel: ahead,
        behind_cancel: behind,
        l2_prev: None,
        l2_new: None,
        l2_trades: None,
    }
}

/// 50,000 events, `(F, B) ~ U[1, 100]^2`, `b ~ Binomial(10, pi_theta0)`, `a = 10 - b`.
fn synth_events(family: AllocFamily, theta0: f64, seed: u64) -> Vec<AllocEventRow> {
    let mut rng = Xoshiro256StarStar::new(seed);
    (0..50_000)
        .map(|_| {
            let f = 1.0 + 99.0 * rng.next_f64();
            let b = 1.0 + 99.0 * rng.next_f64();
            let pi = alloc::pi_behind(family, theta0, f, b);
            let behind = (0..10).filter(|_| rng.next_f64() < pi).count() as f64;
            event(f, b, 10.0 - behind, behind)
        })
        .collect()
}

fn alloc_opts() -> FitOptions {
    FitOptions {
        grid_lo: -3.0,
        grid_hi: 4.0,
        grid_n: 141,
        tol: 1e-7,
    }
}

#[test]
fn o_cal_1_alloc_recovery_power_families() {
    for (fi, family) in [AllocFamily::Power, AllocFamily::Power2, AllocFamily::Power3]
        .into_iter()
        .enumerate()
    {
        for (ni, n0) in [0.5f64, 2.5, 6.0].into_iter().enumerate() {
            let ev = synth_events(family, n0.ln(), 1000 + 10 * fi as u64 + ni as u64);
            let (fit, (lo, hi)) = alloc::fit_alloc_detail(&ev, family, &alloc_opts());
            assert!(
                (fit.estimate - n0.ln()).abs() < 0.02,
                "{family:?} n0={n0}: ln n_hat = {}",
                fit.estimate
            );
            assert_eq!(fit.n_events, 50_000);
            let (lo, hi) = (lo.unwrap_or(f64::NAN), hi.unwrap_or(f64::NAN));
            assert!(
                lo < n0.ln() && n0.ln() < hi,
                "{family:?} n0={n0}: profile CI [{lo}, {hi}]"
            );
            assert_eq!(fit.profile_ci, Some((lo, hi)));
            assert_eq!(fit.grid.len(), 141);
            let l = alloc::loglik(&ev, family, fit.estimate).value;
            assert_eq!(l, fit.loglik);
        }
    }
}

#[test]
fn o_cal_2_alloc_recovery_databento() {
    let opts = FitOptions {
        grid_lo: -0.99,
        grid_hi: 0.99,
        grid_n: 199,
        tol: 1e-7,
    };
    for (i, k0) in [-0.8f64, 0.0, 0.5].into_iter().enumerate() {
        let ev = synth_events(AllocFamily::Databento, k0, 2000 + i as u64);
        let fit = alloc::fit_alloc(&ev, AllocFamily::Databento, &opts);
        assert!((fit.estimate - k0).abs() < 0.012, "k0={k0}: k_hat = {}", fit.estimate);
        let ci = fit.profile_ci.unwrap_or((f64::NAN, f64::NAN));
        assert!(ci.0 < fit.estimate && fit.estimate < ci.1);
    }
}

#[test]
fn alloc_skips_uninformative_and_nonfinite() {
    let ev = vec![
        event(0.0, 5.0, 1.0, 1.0),
        event(5.0, 0.0, 1.0, 1.0),
        event(5.0, 5.0, 2.0, 3.0),
        event(f64::INFINITY, 5.0, 1.0, 1.0),
    ];
    let l = alloc::loglik(&ev, AllocFamily::Power, 0.0);
    assert_eq!(l.used, 1);
    // n = 1, F = B: pi = 0.5 -> 5 ln 0.5.
    assert!((l.value - 5.0 * 0.5f64.ln()).abs() < 1e-12);
    // pi clipped at 1e-12: power3 with huge n and F >> B gives pi = 1 - (F/(F+B))^n ~ 0 -> clipped, finite.
    let l = alloc::loglik(&[event(1e6, 1e-6, 0.0, 1.0)], AllocFamily::Power3, -20.0);
    assert!(l.value.is_finite());
    assert!((l.value - 1e-12f64.ln()).abs() < 1e-9);
    // Non-finite pi is counted and makes the whole l(theta) NaN (never a sum over a theta-dependent subset):
    // NaN theta.
    let two = [event(5.0, 5.0, 1.0, 1.0), event(5.0, 2.0, 1.0, 1.0)];
    let l = alloc::loglik(&two, AllocFamily::Power2, f64::NAN);
    assert_eq!((l.used, l.nonfinite), (0, 2));
    assert!(l.value.is_nan());
    // Huge n no longer overflows: power2 at n = exp(800) is (b / (b + f))^n = 0 -> clipped, finite.
    let l = alloc::loglik(&[event(5.0, 5.0, 1.0, 1.0)], AllocFamily::Power2, 800.0);
    assert_eq!((l.used, l.nonfinite), (1, 0));
    assert!(l.value.is_finite());
}

/// Rust review B1 (the review's `calib_numerics.rs`, ported): 2,000 events drawn from hbt power with n = 2,
/// sizes in shares and in crypto size units (x 1e8, 1 BTC = 1e8 at size_exp -8). hbt's `b^n / (b^n + f^n)`
/// overflowed at large n, every event was skipped and the empty sum (l = 0) won: n = 38.5 with n_events = 0.
fn review_b1_events(scale: f64) -> Vec<AllocEventRow> {
    let mut r = Xoshiro256StarStar::new(7);
    (0..2000)
        .map(|_| {
            let f = 1.0 + (r.next_u64() % 50) as f64;
            let b = 1.0 + (r.next_u64() % 50) as f64;
            let pi = b * b / (b * b + f * f);
            let behind = r.next_f64() < pi;
            event(
                f * scale,
                b * scale,
                if behind { 0.0 } else { 1.0 },
                if behind { 1.0 } else { 0.0 },
            )
        })
        .collect()
}

#[test]
fn review_b1_power_fit_is_scale_invariant_shares() {
    let fit = alloc::fit_alloc(&review_b1_events(1.0), AllocFamily::Power, &alloc_opts());
    assert!((fit.estimate.exp() - 2.0).abs() < 0.5, "n = {}", fit.estimate.exp());
    assert_eq!(fit.n_events, 2000);
}

#[test]
fn review_b1_power_fit_is_scale_invariant_crypto_size_units() {
    let shares = alloc::fit_alloc(&review_b1_events(1.0), AllocFamily::Power, &alloc_opts());
    let fit = alloc::fit_alloc(&review_b1_events(1e8), AllocFamily::Power, &alloc_opts());
    assert!(
        (fit.estimate.exp() - 2.0).abs() < 0.5,
        "n = {} with n_events = {}",
        fit.estimate.exp(),
        fit.n_events
    );
    assert_eq!(fit.n_events, 2000);
    assert!((fit.estimate - shares.estimate).abs() < 1e-6);
    assert!(fit.loglik < 0.0 && (fit.loglik - shares.loglik).abs() < 1e-6 * shares.loglik.abs());
    // Every grid value is finite at crypto scale (no theta-dependent skipping).
    assert!(fit.grid.iter().all(|(_, l)| l.is_finite()));
}

#[test]
fn review_b1_power2_fit_crypto_size_units() {
    let base = alloc::fit_alloc(&review_b1_events(1.0), AllocFamily::Power2, &alloc_opts());
    let fit = alloc::fit_alloc(&review_b1_events(1e8), AllocFamily::Power2, &alloc_opts());
    assert!(
        (fit.estimate - base.estimate).abs() < 1e-3,
        "{} vs {}",
        fit.estimate,
        base.estimate
    );
    assert_eq!(fit.n_events, 2000);
}

#[test]
fn scale_invariant_pi_matches_hbt_expressions_where_finite() {
    for (f, b) in [(1.0, 2.0), (30.0, 70.0), (100.0, 1.0), (3.0, 3.0)] {
        for n in [0.05, 0.5, 1.0, 2.0, 6.0, 30.0] {
            let p = alloc::pi_behind(AllocFamily::Power, f64::ln(n), f, b);
            let q = qt_zoo::prob::power(n, f, b);
            assert!(
                (p - q).abs() <= 1e-12 * q.abs().max(1e-300) + 1e-15,
                "power n={n} f={f} b={b}: {p} vs {q}"
            );
            let p = alloc::pi_behind(AllocFamily::Power2, f64::ln(n), f, b);
            let q = qt_zoo::prob::power2(n, f, b);
            assert!(
                (p - q).abs() <= 1e-12 * q.abs().max(1e-300) + 1e-15,
                "power2 n={n}: {p} vs {q}"
            );
        }
    }
}

/// Review m24: no informative events -> NaN estimate, not the grid's lower end.
#[test]
fn review_m24_no_informative_events_gives_nan() {
    let ev = vec![event(0.0, 5.0, 1.0, 1.0), event(5.0, 0.0, 1.0, 1.0)];
    let fit = alloc::fit_alloc(&ev, AllocFamily::Power3, &alloc_opts());
    assert!(fit.estimate.is_nan() && fit.loglik.is_nan());
    assert_eq!(fit.n_events, 0);
    assert!(fit.profile_ci.is_none());
}

/// Rust review M4 (ported): audit A2, a shadow whose planned end falls inside interval k has no label at k whatever
/// the outcome; the fill there is not an event.
#[test]
fn review_m4_survival_fill_inside_censored_interval_is_not_an_event() {
    let filled = ShadowCurve {
        shadow_idx: 0,
        survival: vec![0.5, 0.25],
        truth_interval: Some(1),
        censored_from: Some(1),
    };
    let unfilled = ShadowCurve {
        shadow_idx: 1,
        survival: vec![0.5, 0.25],
        truth_interval: None,
        censored_from: Some(1),
    };
    assert_eq!(survival::curve_loglik(&filled), survival::curve_loglik(&unfilled));
    assert!((survival::curve_loglik(&filled) - 0.5f64.ln()).abs() < 1e-12);
    // A fill before the planned end is still an event.
    let early = ShadowCurve {
        truth_interval: Some(0),
        ..filled.clone()
    };
    assert!((survival::curve_loglik(&early) - 0.5f64.ln()).abs() < 1e-12);
    let at_zero = ShadowCurve {
        censored_from: Some(0),
        ..filled
    };
    assert_eq!(survival::curve_loglik(&at_zero), 0.0);
}

fn quad_grid(centre: f64, scale: f64, lo: f64, hi: f64, n: usize) -> Vec<(f64, f64)> {
    optim::grid_points(lo, hi, n)
        .into_iter()
        .map(|t| (t, -(t - centre) * (t - centre) * scale))
        .collect()
}

#[test]
fn trust_well_identified_quadratic_has_no_flags() {
    // l = -50 (t - 0.7)^2: curvature 100, half-width sqrt(3.84 / 100) = 0.196 << 7 / 4.
    let g = quad_grid(0.7, 50.0, -3.0, 4.0, 141);
    let d = trust::analyze_grid(&g, 0.0);
    assert_eq!(d.at_edge, None);
    assert!(!d.flat() && !d.constant && !d.profile_spans_grid);
    assert!((d.curvature.unwrap_or(0.0) - 100.0).abs() < 1e-6, "{:?}", d.curvature);
    assert!(trust::grid_lines(&d, &g, "n", f64::exp).is_empty());
}

#[test]
fn trust_flags_grid_edge_plateau_flat_and_constant() {
    // Increasing to the upper end: at_grid_edge upper.
    let g: Vec<(f64, f64)> = optim::grid_points(-3.0, 4.0, 141)
        .into_iter()
        .map(|t| (t, 10.0 * t))
        .collect();
    let d = trust::analyze_grid(&g, f64::NAN);
    assert_eq!(d.at_edge, Some(trust::Edge::Upper));
    let lines = trust::grid_lines(&d, &g, "n", f64::exp);
    assert!(
        lines
            .iter()
            .any(|l| l.starts_with("trust: at_grid_edge: the maximum is at the upper end")),
        "{lines:?}"
    );

    // Review E1 (L2 tape): flat over the first 13 grid points, maximum at the lower end.
    let g: Vec<(f64, f64)> = optim::grid_points(-3.0, 4.0, 29)
        .into_iter()
        .enumerate()
        .map(|(i, t)| (t, if i < 13 { -54.5 } else { -54.5 - (i - 12) as f64 }))
        .collect();
    let d = trust::analyze_grid(&g, -54.5);
    assert_eq!((d.at_edge, d.plateau), (Some(trust::Edge::Lower), Some((0, 12))));
    let lines = trust::grid_lines(&d, &g, "n", f64::exp);
    assert!(lines.iter().any(|l| l.starts_with("trust: at_grid_edge:")));
    assert!(
        lines
            .iter()
            .any(|l| l.starts_with("trust: plateau: the likelihood ties over 13 grid points"))
    );

    // Review E1 (MBO tape): interior plateau [0.22, 0.47]; a narrow plateau is reported but not flat.
    let g: Vec<(f64, f64)> = optim::grid_points(-3.0, 4.0, 29)
        .into_iter()
        .enumerate()
        .map(|(i, t)| {
            (
                t,
                if (6..=9).contains(&i) {
                    -10.0
                } else {
                    -10.0 - 30.0 * (i as f64 - 7.5).abs()
                },
            )
        })
        .collect();
    let d = trust::analyze_grid(&g, -10.0);
    assert_eq!((d.at_edge, d.plateau), (None, Some((6, 9))));
    assert!(!d.flat());
    let lines = trust::grid_lines(&d, &g, "n", f64::exp);
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(lines[0].starts_with("trust: plateau: the likelihood ties over 4 grid points"));

    // Weak curvature: l = -0.01 (t - 0.5)^2 -> profile region holds the whole grid.
    let g = quad_grid(0.5, 0.01, -3.0, 4.0, 141);
    let d = trust::analyze_grid(&g, 0.0);
    assert!(d.profile_spans_grid && d.flat());
    let lines = trust::grid_lines(&d, &g, "n", f64::exp);
    assert!(
        lines
            .iter()
            .any(|l| l.starts_with("trust: flat_likelihood: the 95% profile region holds every grid"))
    );

    // Curvature below the threshold but profile closed: half-width >= W / 4.
    let g = quad_grid(0.5, 0.5, -3.0, 4.0, 141);
    let d = trust::analyze_grid(&g, 0.0);
    assert!(!d.profile_spans_grid);
    assert!(d.halfwidth.unwrap_or(0.0) >= 0.25 * 7.0 - 1e-9 || !d.flat(), "{d:?}");
    let g = quad_grid(0.5, 0.3, -3.0, 4.0, 141);
    let d = trust::analyze_grid(&g, 0.0);
    // I = 0.6, half-width sqrt(3.84 / 0.6) = 2.53 >= 1.75.
    assert!(d.flat() && !d.profile_spans_grid, "{d:?}");
    assert!(
        trust::grid_lines(&d, &g, "n", f64::exp)
            .iter()
            .any(|l| l.contains("curvature at the maximum"))
    );

    // Constant: not identified.
    let g: Vec<(f64, f64)> = optim::grid_points(-3.0, 4.0, 29)
        .into_iter()
        .map(|t| (t, -3.5))
        .collect();
    let d = trust::analyze_grid(&g, -3.5);
    assert!(d.constant);
    let lines = trust::grid_lines(&d, &g, "n", f64::exp);
    assert!(lines.iter().any(|l| l.starts_with("trust: not_identified:")));
    assert!(lines.iter().any(|l| l.starts_with("trust: at_grid_edge:")));

    // One open side of the profile region.
    let g = quad_grid(-2.5, 1.0, -3.0, 4.0, 141);
    let d = trust::analyze_grid(&g, 0.0);
    assert_eq!(d.profile_open, (true, false));
    assert!(
        trust::grid_lines(&d, &g, "n", f64::exp)
            .iter()
            .any(|l| l.starts_with("trust: profile_ci_open"))
    );
}

#[test]
fn trust_degenerate_ci_is_suppressed() {
    let (ci, line) = trust::check_ci((Some(0.0498), Some(0.0498)), "n", 3);
    assert_eq!(ci, (None, None));
    assert!(line.is_some_and(|l| l.starts_with("trust: degenerate_ci: every bootstrap replicate gave n = 0.0498")));
    let (_, line) = trust::check_ci((Some(1.5), Some(1.5)), "n", 1);
    assert!(line.is_some_and(|l| l.contains("single bootstrap block")));
    let (ci, line) = trust::check_ci((Some(0.9), Some(1.1)), "n", 3);
    assert_eq!((ci, line), ((Some(0.9), Some(1.1)), None));
    assert_eq!(trust::g6(0.0497870683678639), "0.0497871");
    assert_eq!(trust::g6(2.0), "2");
    assert_eq!(trust::g6(123456789.0), "1.23457e+08");
    assert_eq!(trust::g6(-0.8), "-0.8");
}

#[test]
fn alloc_fixed_families_report_loglik() {
    let ev = vec![event(30.0, 70.0, 2.0, 8.0)];
    let l = alloc::loglik_fixed(&ev, alloc::FixedFamily::Log);
    let pi = qt_zoo::prob::log(30.0, 70.0);
    assert!((l.value - (8.0 * pi.ln() + 2.0 * (1.0 - pi).ln())).abs() < 1e-12);
}

fn touch_row(id: &str, idx: u32, excess: f64, filled: bool, first: bool) -> TouchRecordRow {
    TouchRecordRow {
        shadow_id: id.into(),
        ts_event: i64::from(idx),
        touch_index: idx,
        nt_front_before: 0.0,
        nt_excess: excess,
        truth_filled_now: filled,
        truth_first_fill_here: first,
    }
}

#[test]
fn o_cal_3_nt_touch_golden_and_recovery() {
    // (delta, m/M) = (1, 3), (0, 5), (1, 1) -> 2/9.
    let mut rows = Vec::new();
    for i in 1..=3 {
        rows.push(touch_row("a", i, 1.0, i == 3, i == 3));
    }
    for i in 1..=5 {
        rows.push(touch_row("b", i, 1.0, false, false));
    }
    rows.push(touch_row("c", 1, 1.0, true, true));
    let fit = touch::fit_nt_touch(&rows, false);
    assert_eq!(fit.p_hat, 2.0 / 9.0);
    assert_eq!(fit.p_hat, 0.2222222222222222);
    assert_eq!((fit.n_orders, fit.n_filled, fit.touches), (3, 2, 9));

    // Geometric recovery: p0 = 0.2, 20,000 orders censored after U{1..30} touches.
    let mut rng = Xoshiro256StarStar::new(stable_seed(3, &[b"o_cal_3"]));
    let mut rows = Vec::new();
    for o in 0..20_000u32 {
        let id = format!("o{o}");
        let limit = 1 + rng.range_inclusive(0, 29) as u32;
        for k in 1..=limit {
            let hit = rng.next_f64() < 0.2;
            rows.push(touch_row(&id, k, 1.0, hit, hit));
            if hit {
                break;
            }
        }
    }
    let fit = touch::fit_nt_touch(&rows, false);
    assert!((fit.p_hat - 0.2).abs() < 0.005, "p_hat = {}", fit.p_hat);
    assert_eq!(fit.n_orders, 20_000);
}

/// Audit `checks/c3.py` (ruling A7): p = 0.2 per touch, a competing trade-through just before each touch with
/// probability 0.15, administrative censoring after U{1..29} touches. Excluding trade-through orders gives about
/// 0.300; censoring them at the trade-through (touch rows only before it) recovers 0.200.
#[test]
fn o_cal_3_nt_touch_competing_trade_through_audit_c3() {
    let mut rng = Xoshiro256StarStar::new(stable_seed(0, &[b"audit_c3"]));
    let (mut rows, mut rows_excluding) = (Vec::new(), Vec::new());
    for o in 0..200_000u32 {
        let id = format!("o{o}");
        let cap = 1 + rng.range_inclusive(0, 28) as u32;
        let mut mine = Vec::new();
        let mut trade_through = false;
        for k in 1..=cap {
            if rng.next_f64() < 0.15 {
                trade_through = true;
                break;
            }
            let hit = rng.next_f64() < 0.2;
            mine.push(touch_row(&id, k, 1.0, hit, hit));
            if hit {
                break;
            }
        }
        if !trade_through {
            rows_excluding.extend(mine.iter().cloned());
        }
        rows.extend(mine);
    }
    let fit = touch::fit_nt_touch(&rows, false);
    assert!((fit.p_hat - 0.2).abs() < 0.005, "p_hat = {}", fit.p_hat);
    let biased = touch::fit_nt_touch(&rows_excluding, false);
    assert!(
        biased.p_hat > 0.27,
        "excluding trade-through orders should bias upward, got {}",
        biased.p_hat
    );
}

#[test]
fn nt_touch_exclusion_and_gating() {
    let rows = vec![
        // First filled by a trade-through after one unfilled touch: censored with M = 1 (audit ruling A7).
        touch_row("x", 1, 1.0, false, false),
        // Gated: touches 1 (excess 0), 2 (excess 3, first fill) -> m = 1 gated touch.
        touch_row("g", 1, 0.0, false, false),
        touch_row("g", 2, 3.0, true, true),
        // Gated: first fill at a non-gated touch -> censored with the gated touches before it (1).
        touch_row("h", 1, 2.0, false, false),
        touch_row("h", 2, 0.0, true, true),
        // Unfilled: 3 touches, 2 gated.
        touch_row("u", 1, 1.0, false, false),
        touch_row("u", 2, 0.0, false, false),
        touch_row("u", 3, 1.0, false, false),
    ];
    let plain = touch::fit_nt_touch(&rows, false);
    assert_eq!((plain.n_orders, plain.n_filled, plain.touches), (4, 2, 1 + 2 + 2 + 3));
    let gated = touch::fit_nt_touch(&rows, true);
    assert_eq!((gated.n_orders, gated.n_filled, gated.touches), (4, 1, 1 + 1 + 1 + 2));
    assert_eq!(gated.p_hat, 0.2);
    assert!(touch::fit_nt_touch(&[], false).p_hat.is_nan());
}

#[test]
fn o_cal_5_golden_section_known_functions() {
    type Case = (Box<dyn Fn(f64) -> f64>, f64, f64, f64);
    let cases: Vec<Case> = vec![
        (Box::new(|x| -(x - 0.3) * (x - 0.3)), -1.0, 2.0, 0.3),
        (Box::new(|x: f64| x.sin()), 0.0, 3.0, std::f64::consts::FRAC_PI_2),
        (Box::new(|x: f64| x * (-x).exp()), 0.0, 5.0, 1.0),
        (Box::new(|x: f64| -(x - 0.25).abs()), -2.0, 1.0, 0.25),
        (Box::new(|x: f64| x.ln() - x), 0.1, 4.0, 1.0),
        (Box::new(|x: f64| -(x - 1.234567).powi(4)), 0.0, 3.0, 1.234567),
        (Box::new(|x: f64| -x), 0.0, 1.0, 0.0),
    ];
    for (i, (f, lo, hi, xstar)) in cases.iter().enumerate() {
        let (x, fx) = optim::golden_section_max(f, *lo, *hi, 1e-7);
        assert!((x - xstar).abs() < 1e-7, "case {i}: x = {x}, x* = {xstar}");
        assert_eq!(fx, f(x));
        // Reversed bounds give the same answer.
        let (x2, _) = optim::golden_section_max(f, *hi, *lo, 1e-7);
        assert_eq!(x, x2);
    }
}

#[test]
fn bootstrap_stream_matches_reference_fixture() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/reference/bootstrap.json");
    let text = std::fs::read_to_string(&path).unwrap_or_default();
    let doc: serde_json::Value = serde_json::from_str(&text).unwrap_or(serde_json::Value::Null);
    let cases = doc["xoshiro_block_resample"].as_array().cloned().unwrap_or_default();
    assert_eq!(cases.len(), 3, "fixture {} missing or changed", path.display());
    for c in &cases {
        let seed = c["seed"].as_u64().unwrap_or(u64::MAX - 1);
        let n_blocks = c["n_blocks"].as_u64().unwrap_or(0) as usize;
        assert_eq!(Some(stable_seed(seed, &[b"boot"])), c["stream_seed"].as_u64());
        let want: Vec<Vec<u32>> = c["draws"]
            .as_array()
            .map(|a| {
                a.iter()
                    .map(|r| {
                        r.as_array()
                            .map(|r| r.iter().filter_map(|v| v.as_u64()).map(|v| v as u32).collect())
                            .unwrap_or_default()
                    })
                    .collect()
            })
            .unwrap_or_default();
        let got = bootstrap::block_draws(n_blocks, want.len() as u32, seed);
        assert_eq!(got, want, "seed {seed}, n_blocks {n_blocks}");
    }
    // Embedded copy of the first case, so a regenerated fixture cannot silently move the stream.
    assert_eq!(
        bootstrap::block_draws(7, 2, 0),
        vec![vec![4, 6, 6, 0, 1, 3, 1], vec![1, 5, 4, 0, 6, 3, 0]]
    );
}

#[test]
fn bootstrap_percentile_matches_numpy_linear() {
    // numpy.quantile([4, 1, 3, 2], [0.025, 0.975]) = [1.075, 3.925]; NaN replicates are ignored.
    let (lo, hi) = bootstrap::percentile_ci(&[4.0, 1.0, f64::NAN, 3.0, 2.0], 0.05);
    assert_eq!((lo, hi), (1.075, 3.925));
    assert_eq!(bootstrap::quantile_linear(&[0.1, 0.2, 0.7], 0.5), 0.2);
    assert!(bootstrap::percentile_ci(&[f64::NAN], 0.05).0.is_nan());
    // Deterministic for a seed, threads do not change replicates.
    let stat = |d: &[u32]| d.iter().map(|x| f64::from(*x)).sum::<f64>() / d.len() as f64;
    let a = bootstrap::replicates(12, 200, 5, 1, &stat);
    let b = bootstrap::replicates(12, 200, 5, 4, &stat);
    assert_eq!(a, b);
    let ci = bootstrap::block_bootstrap(12, 200, 5, stat);
    assert_eq!(ci, bootstrap::percentile_ci(&a, 0.05));
    assert!(ci.0 < 5.5 && 5.5 < ci.1);
}

#[test]
fn bootstrap_block_labels_follow_python_rule() {
    let day = 86_400_000_000_000i64;
    let dates = [3, 1, 1, 2, 4, 5];
    let ts: Vec<i64> = dates.iter().map(|d| i64::from(*d) * day).collect();
    let (l, n, k) = bootstrap::block_labels(&dates, &ts, qt_schema::specs::BootstrapBlock::Auto);
    assert_eq!((l, n, k), (vec![2, 0, 0, 1, 3, 4], 5, bootstrap::BlockKind::Day));
    let ts = [
        0,
        bootstrap::HALF_HOUR_NS - 1,
        bootstrap::HALF_HOUR_NS,
        5 * bootstrap::HALF_HOUR_NS,
    ];
    let (l, n, k) = bootstrap::block_labels(&[0, 0, 0, 0], &ts, qt_schema::specs::BootstrapBlock::Auto);
    assert_eq!((l, n, k), (vec![0, 0, 1, 2], 3, bootstrap::BlockKind::HalfHour));
    assert_eq!(bootstrap::counts(&[2, 0, 2], 3), vec![1, 0, 2]);
}

/// Mock particle simulator: shadow `i` with covariate `x_i` fills in each interval with probability
/// `x_i^(1 / (1 + theta))` (the Databento front-allocation curve); truth generated the same way from `theta0`.
struct MockSim {
    horizons: Vec<Dur>,
    xs: Vec<f64>,
    truth: Vec<(Option<usize>, Option<usize>)>,
    particles: u32,
}

impl MockSim {
    fn new(theta0: f64, n: usize, truth_seed: u64) -> Self {
        let horizons = vec![1, 2, 3, 4, 5];
        let mut rng = Xoshiro256StarStar::new(truth_seed);
        let mut xs = Vec::new();
        let mut truth = Vec::new();
        for _ in 0..n {
            let x = 0.5 + 0.48 * rng.next_f64();
            let q = x.powf(1.0 / (1.0 + theta0));
            let censor_at = rng.range_inclusive(2, 6) as usize; // 6 = never censored
            let mut t = (None, if censor_at < 5 { Some(censor_at) } else { None });
            for j in 0..censor_at.min(5) {
                if rng.next_f64() < q {
                    t = (Some(j), None);
                    break;
                }
            }
            xs.push(x);
            truth.push(t);
        }
        Self {
            horizons,
            xs,
            truth,
            particles: 100,
        }
    }
}

impl ParticleSimulator for MockSim {
    fn horizons(&self) -> &[Dur] {
        &self.horizons
    }
    fn simulate(&self, theta: f64, seed: u64) -> QtResult<Vec<ShadowCurve>> {
        let n = f64::from(self.particles);
        Ok(self
            .xs
            .iter()
            .zip(&self.truth)
            .enumerate()
            .map(|(i, (x, t))| {
                let mut rng = Xoshiro256StarStar::new(stable_seed(seed, &[&(i as u64).to_le_bytes()]));
                let q = x.powf(1.0 / (1.0 + theta));
                let mut alive = vec![0u32; self.horizons.len()];
                for _ in 0..self.particles {
                    let mut filled = false;
                    for a in alive.iter_mut() {
                        filled = filled || rng.next_f64() < q;
                        if !filled {
                            *a += 1;
                        }
                    }
                }
                ShadowCurve {
                    shadow_idx: i as u32,
                    survival: alive.iter().map(|a| (f64::from(*a) + 0.5) / (n + 1.0)).collect(),
                    truth_interval: t.0,
                    censored_from: t.1,
                }
            })
            .collect())
    }
}

#[test]
fn survival_recovers_theta_on_mock_simulator() {
    let sim = MockSim::new(-0.8, 3000, 77);
    let thetas = optim::grid_points(-0.98, 0.9, 25);
    let step = thetas[1] - thetas[0];
    let fit = survival::fit_survival(&sim, &thetas, 12345, false).unwrap_or_else(|e| panic!("{e}"));
    assert!((fit.estimate + 0.8).abs() <= step, "k_hat = {}", fit.estimate);
    assert_eq!(fit.grid.len(), 25);
    let refined = survival::fit_survival(&sim, &thetas, 12345, true).unwrap_or_else(|e| panic!("{e}"));
    assert!(refined.loglik >= fit.loglik);
    assert!(
        (refined.estimate + 0.8).abs() <= step,
        "{} {} {:?}",
        refined.estimate,
        fit.estimate,
        fit.grid
    );
    // Same seed -> identical fit.
    assert_eq!(survival::fit_survival(&sim, &thetas, 12345, false).ok(), Some(fit));
}

#[test]
fn survival_curve_loglik_by_hand() {
    let c = ShadowCurve {
        shadow_idx: 0,
        survival: vec![0.8, 0.5, 0.5],
        truth_interval: Some(1),
        censored_from: None,
    };
    // h = [0.2, 0.375]: ln(0.8) + ln(0.375).
    assert!((survival::curve_loglik(&c) - (0.8f64.ln() + 0.375f64.ln())).abs() < 1e-12);
    let c = ShadowCurve {
        censored_from: Some(2),
        truth_interval: None,
        ..c
    };
    assert!((survival::curve_loglik(&c) - (0.8f64.ln() + 0.625f64.ln())).abs() < 1e-12);
    let c = ShadowCurve {
        censored_from: None,
        ..c
    };
    // Third interval: h = 0 -> clamped, ln(1 - 1e-12).
    assert!((survival::curve_loglik(&c) - (0.8f64.ln() + 0.625f64.ln())).abs() < 1e-9);
    let c = ShadowCurve {
        censored_from: Some(0),
        ..c
    };
    assert_eq!(survival::curve_loglik(&c), 0.0);
    // Truth fill where no particle filled: h = 0 is floored, finite.
    let c = ShadowCurve {
        survival: vec![0.8, 0.8],
        truth_interval: Some(1),
        censored_from: None,
        shadow_idx: 0,
    };
    assert!(survival::curve_loglik(&c).is_finite());
}

#[test]
fn trust_disjoint_instrument_strata_are_flagged() {
    let st = |s: &str, lo: f64, hi: f64| qt_schema::json::StratumEstimate {
        stratum_kind: "instrument".into(),
        stratum: s.into(),
        estimate: Some(0.5 * (lo + hi)),
        ci_lo: Some(lo),
        ci_hi: Some(hi),
        n: 2000,
    };
    // Trader review D3: n = 1.10 [1.05, 1.15] and 0.88 [0.84, 0.92].
    let line = trust::disjoint_strata(&[st("1", 1.05, 1.15), st("2", 0.84, 0.92)], "instrument", "n");
    assert!(line.is_some_and(|l| l.starts_with("trust: heterogeneous_strata: instrument 1 has n in [1.05, 1.15]")));
    assert!(trust::disjoint_strata(&[st("1", 1.0, 1.2), st("2", 1.1, 1.3)], "instrument", "n").is_none());
}

#[test]
fn trust_structured_assessment_of_rust_only_results() {
    use qt_schema::json::{CalibrationResult, TrustVerdict};
    let r = CalibrationResult {
        estimate: Some(0.0497870683678639),
        n_events: 0,
        warnings: vec![
            "rust part only: held_out ...".into(),
            "trust: plateau: the likelihood ties over 13 grid points".into(),
            "trust: at_grid_edge: the maximum is at the lower end".into(),
            "trust: degenerate_ci: every bootstrap replicate gave n = 0.0497871".into(),
            "trust: beats_default: 1=tie (x)".into(),
            "info: p is per touch".into(),
        ],
        ..Default::default()
    };
    let t = trust::assess_rust(&r);
    assert_eq!(t.verdict, TrustVerdict::Refused);
    assert_eq!(
        t.reasons,
        ["few_events", "at_grid_edge", "degenerate_ci", "no_held_out"]
    );
    assert_eq!(t.flags, ["at_grid_edge", "degenerate_ci", "plateau"]);
    assert!(t.beats_default.is_empty());
    let none = trust::assess_rust(&CalibrationResult::default());
    assert_eq!(none.reasons, ["no_estimate", "few_events", "no_held_out"]);
}
