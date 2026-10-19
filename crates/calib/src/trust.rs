//! Fail-safe diagnostics of a fitted parameter (trader review D1, D2, E1): grid-edge estimates, flat likelihood
//! stretches and non-identified parameters are reported as `trust:` lines in `warnings` and in the structured
//! `qt.calibration_result.trust` (schema 1.1, `assess_rust`), which the export refuses (`queuetruth.export.trust`
//! reads the same tokens; `calibrate-live` computes the same flags in Python).
//!
//! Definitions, on the evaluated grid `theta_0 < ... < theta_{n-1}` (width `W = theta_{n-1} - theta_0`) with log
//! likelihood `l_g` (NaN = `-inf`), grid maximum `g = max l_g`, tie tolerance `tol = 1e-9 max(1, |g|)`:
//! - **plateau**: the contiguous run of grid points around the first grid argmax with `g - l_g <= tol`; reported when
//!   it holds two or more points (any value in it fits equally well).
//! - **at_grid_edge** (refuses export): the plateau holds the first or the last grid point, so the maximum may lie
//!   outside the grid.
//! - **not_identified** (refuses export): `l` is the same at every finite grid value (within `tol`).
//! - **flat_likelihood** (refuses export): the 95% profile region `{2 (l_max - l_g) <= 3.841459}` holds every grid
//!   point, or the curvature at the plateau implies a 95% half-width `sqrt(3.841459 / I) >= W / 4`, where
//!   `I = mean over the existing neighbours s of the plateau of 2 (g - l_s) / d_s^2` and `d_s` is the distance from
//!   the plateau's centre to that neighbour (for a quadratic `l` this is its exact curvature).
//! - **profile_ci_open**: the profile region reaches exactly one grid end.
//! - **degenerate_ci** (refuses export): a refined (continuous) estimate's bootstrap interval has zero width; it is
//!   suppressed.
//! - **heterogeneous_strata** (note, trader review D3): two instrument strata have disjoint intervals, so one pooled
//!   parameter does not describe both; use the per-instrument estimates in `stratified`.

use qt_schema::json::{CalibrationResult, CalibrationTrust, TrustVerdict};

use crate::optim::PROFILE_CRIT;

/// Relative tolerance under which two log likelihoods tie.
pub const FLAT_REL_TOL: f64 = 1e-9;
/// A 95% half-width of at least this share of the grid width counts as a flat likelihood.
pub const FLAT_FRACTION: f64 = 0.25;
/// Prefix of every diagnostic line in `warnings`.
pub const PREFIX: &str = "trust: ";
/// Fewest events of an exportable fit (as `queuetruth.export.trust.MIN_EXPORT_EVENTS`).
pub const MIN_EXPORT_EVENTS: u64 = 30;
/// Refusing tokens in report order (as `queuetruth.export.trust.REFUSING`).
pub const REFUSING: [&str; 9] = [
    "no_estimate",
    "few_events",
    "at_grid_edge",
    "flat_likelihood",
    "not_identified",
    "degenerate_ci",
    "own_orders_in_depth",
    "no_held_out",
    "not_beating_default",
];
/// Line tokens that are not diagnostics (written by the Python layer for the held-out verdict).
const OWN_TOKENS: [&str; 2] = ["beats_default", "verdict"];

/// Grid end.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Edge {
    /// First grid point.
    Lower,
    /// Last grid point.
    Upper,
}

impl Edge {
    fn word(self) -> &'static str {
        match self {
            Edge::Lower => "lower",
            Edge::Upper => "upper",
        }
    }
}

/// Diagnostics of a grid likelihood.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct GridDiag {
    /// First grid argmax (None: no finite value).
    pub argmax: Option<usize>,
    /// Index range of the plateau around the argmax (inclusive).
    pub plateau: Option<(usize, usize)>,
    /// Grid end inside the plateau.
    pub at_edge: Option<Edge>,
    /// Every finite grid value ties.
    pub constant: bool,
    /// Curvature estimate `I` at the plateau (0 when no neighbour is lower).
    pub curvature: Option<f64>,
    /// `sqrt(3.841459 / I)` (infinite when `I = 0`).
    pub halfwidth: Option<f64>,
    /// The 95% profile region holds every grid point.
    pub profile_spans_grid: bool,
    /// (lower open, upper open) of the grid-based profile region.
    pub profile_open: (bool, bool),
    /// Grid width `W`.
    pub width: f64,
}

impl GridDiag {
    /// Flat likelihood (profile spans the grid or curvature below the threshold).
    pub fn flat(&self) -> bool {
        self.profile_spans_grid
            || self
                .halfwidth
                .is_some_and(|w| w >= FLAT_FRACTION * self.width && self.width > 0.0)
    }
}

fn v(x: f64) -> f64 {
    if x.is_nan() { f64::NEG_INFINITY } else { x }
}

/// Diagnostics of `grid` (theta, l) sorted by theta; `l_max` is the refined maximum (>= the grid maximum) or NaN.
pub fn analyze_grid(grid: &[(f64, f64)], l_max: f64) -> GridDiag {
    let n = grid.len();
    let mut d = GridDiag {
        width: if n > 1 { grid[n - 1].0 - grid[0].0 } else { 0.0 },
        ..Default::default()
    };
    let Some(i) = crate::optim::grid_argmax(&grid.iter().map(|g| g.1).collect::<Vec<_>>()) else {
        return d;
    };
    d.argmax = Some(i);
    let g = grid[i].1;
    let tol = FLAT_REL_TOL * g.abs().max(1.0);
    let tied = |j: usize| g - v(grid[j].1) <= tol;
    let (mut a, mut b) = (i, i);
    while a > 0 && tied(a - 1) {
        a -= 1;
    }
    while b + 1 < n && tied(b + 1) {
        b += 1;
    }
    d.plateau = Some((a, b));
    if n > 1 {
        d.at_edge = if a == 0 {
            Some(Edge::Lower)
        } else if b == n - 1 {
            Some(Edge::Upper)
        } else {
            None
        };
    }
    let finite: Vec<f64> = grid.iter().map(|x| x.1).filter(|x| x.is_finite()).collect();
    let lo = finite.iter().copied().fold(f64::INFINITY, f64::min);
    d.constant = n > 1 && g - lo <= tol;
    let centre = 0.5 * (grid[a].0 + grid[b].0);
    let mut parts = Vec::new();
    for s in [a.checked_sub(1), (b + 1 < n).then_some(b + 1)].into_iter().flatten() {
        let ls = v(grid[s].1);
        let dist = (grid[s].0 - centre).abs();
        if dist > 0.0 {
            parts.push(if ls.is_finite() {
                2.0 * (g - ls) / (dist * dist)
            } else {
                f64::INFINITY
            });
        }
    }
    let curv = if parts.is_empty() {
        0.0
    } else {
        parts.iter().sum::<f64>() / parts.len() as f64
    };
    d.curvature = Some(curv);
    d.halfwidth = Some(if curv > 0.0 {
        (PROFILE_CRIT / curv).sqrt()
    } else {
        f64::INFINITY
    });
    let top = if l_max.is_finite() { l_max.max(g) } else { g };
    let inside = |j: usize| 2.0 * (top - v(grid[j].1)) <= PROFILE_CRIT;
    let (mut pa, mut pb) = (i, i);
    while pa > 0 && inside(pa - 1) {
        pa -= 1;
    }
    while pb + 1 < n && inside(pb + 1) {
        pb += 1;
    }
    d.profile_open = (pa == 0, pb + 1 == n);
    d.profile_spans_grid = n > 1 && pa == 0 && pb + 1 == n;
    d
}

/// Up to 6 significant digits, trailing zeros trimmed (Python `format(x, ".6g")` for ordinary magnitudes).
pub fn g6(x: f64) -> String {
    if !x.is_finite() {
        return format!("{x}");
    }
    if x == 0.0 {
        return "0".into();
    }
    let mag = x.abs().log10().floor() as i32;
    if !(-5..6).contains(&mag) {
        let s = format!("{x:.5e}");
        let (m, e) = s.split_once('e').unwrap_or((&s, "0"));
        let m = m.trim_end_matches('0').trim_end_matches('.');
        let e: i32 = e.parse().unwrap_or(0);
        return format!("{m}e{}{:02}", if e < 0 { '-' } else { '+' }, e.abs());
    }
    let dec = (5 - mag).max(0) as usize;
    let s = format!("{x:.dec$}");
    if s.contains('.') {
        s.trim_end_matches('0').trim_end_matches('.').to_owned()
    } else {
        s
    }
}

/// The `trust:` lines of a grid diagnosis. `to_nat` maps theta to the reported parameter, `param` names it.
pub fn grid_lines(d: &GridDiag, grid: &[(f64, f64)], param: &str, to_nat: impl Fn(f64) -> f64) -> Vec<String> {
    let mut out = Vec::new();
    let (Some((a, b)), Some(n)) = (d.plateau, grid.len().checked_sub(1)) else {
        return out;
    };
    let lo = to_nat(grid[0].0);
    let hi = to_nat(grid[n].0);
    if d.constant {
        out.push(format!(
            "{PREFIX}not_identified: the likelihood is the same at every grid value ({param} in [{}, {}]); the data \
             carry no information about {param}",
            g6(lo),
            g6(hi)
        ));
    }
    if let Some(e) = d.at_edge {
        let at = if e == Edge::Lower { lo } else { hi };
        out.push(format!(
            "{PREFIX}at_grid_edge: the maximum is at the {} end of the grid ({param} = {}; grid {param} in [{}, {}]); \
             the likelihood may keep rising outside it",
            e.word(),
            g6(at),
            g6(lo),
            g6(hi)
        ));
    }
    if b > a && !d.constant {
        out.push(format!(
            "{PREFIX}plateau: the likelihood ties over {} grid points, {param} in [{}, {}]; any value there fits \
             equally well",
            b - a + 1,
            g6(to_nat(grid[a].0)),
            g6(to_nat(grid[b].0))
        ));
    }
    if d.flat() && !d.constant {
        let why = if d.profile_spans_grid {
            "the 95% profile region holds every grid value".to_owned()
        } else {
            format!(
                "the curvature at the maximum implies a 95% half-width of {} grid units, at least {} of the grid \
                 width {}",
                g6(d.halfwidth.unwrap_or(f64::INFINITY)),
                FLAT_FRACTION,
                g6(d.width)
            )
        };
        out.push(format!(
            "{PREFIX}flat_likelihood: {why}; {param} is not determined by the data"
        ));
    } else if d.profile_open.0 != d.profile_open.1 {
        let side = if d.profile_open.0 { "lower" } else { "upper" };
        out.push(format!(
            "{PREFIX}profile_ci_open: the 95% profile region reaches the {side} grid end; that side of the interval \
             is unbounded on this grid"
        ));
    }
    out
}

/// `heterogeneous_strata` line when two strata of `kind` have disjoint intervals (None otherwise).
pub fn disjoint_strata(strata: &[qt_schema::json::StratumEstimate], kind: &str, param: &str) -> Option<String> {
    let s: Vec<_> = strata
        .iter()
        .filter(|x| x.stratum_kind == kind)
        .filter_map(|x| Some((x, x.ci_lo?, x.ci_hi?)))
        .collect();
    for (i, (a, alo, ahi)) in s.iter().enumerate() {
        for (b, blo, bhi) in s.iter().skip(i + 1) {
            if ahi < blo || bhi < alo {
                return Some(format!(
                    "{PREFIX}heterogeneous_strata: {kind} {} has {param} in [{}, {}] and {kind} {} in [{}, {}] \
                     (disjoint): one pooled {param} does not describe both; see `stratified`",
                    a.stratum,
                    g6(*alo),
                    g6(*ahi),
                    b.stratum,
                    g6(*blo),
                    g6(*bhi)
                ));
            }
        }
    }
    None
}

/// Degenerate (zero-width) bootstrap interval check over `n_blocks` bootstrap blocks: returns the suppressed
/// interval and the line, if any.
pub fn check_ci(
    ci: (Option<f64>, Option<f64>),
    param: &str,
    n_blocks: usize,
) -> ((Option<f64>, Option<f64>), Option<String>) {
    match ci {
        (Some(lo), Some(hi)) if lo == hi => {
            let why = if n_blocks <= 1 {
                format!(
                    "the train data fall in a single bootstrap block, so every replicate is the same fit ({param} = \
                     {}): no interval can be estimated",
                    g6(lo)
                )
            } else {
                format!(
                    "every bootstrap replicate gave {param} = {}, a zero-width interval that is not a precision \
                     statement",
                    g6(lo)
                )
            };
            ((None, None), Some(format!("{PREFIX}degenerate_ci: {why}; suppressed")))
        }
        other => (other, None),
    }
}

/// Diagnostic tokens of the `trust:` lines in `warnings`, sorted and unique.
pub fn line_tokens(warnings: &[String]) -> Vec<String> {
    let mut out: Vec<String> = warnings
        .iter()
        .filter_map(|w| w.strip_prefix(PREFIX))
        .filter_map(|r| r.split_once(':').map(|(t, _)| t.to_owned()))
        .filter(|t| !t.is_empty() && t.chars().all(|c| c.is_ascii_lowercase() || c == '_'))
        .filter(|t| !OWN_TOKENS.contains(&t.as_str()))
        .collect();
    out.sort();
    out.dedup();
    out
}

/// Structured `trust` of a Rust-only result (`fit.json`): the refusing diagnostics plus `no_held_out`, since the
/// held-out comparison with the default is computed by the Python layer, which replaces this field
/// (`queuetruth.export.trust.annotate`). `beats_default` is empty (not assessed here).
pub fn assess_rust(res: &CalibrationResult) -> CalibrationTrust {
    let flags = line_tokens(&res.warnings);
    let mut reasons = Vec::new();
    for tok in REFUSING {
        let hit = match tok {
            "no_estimate" => res.estimate.is_none_or(|e| !e.is_finite()),
            "few_events" => res.n_events < MIN_EXPORT_EVENTS,
            "no_held_out" => res.held_out.is_empty() || res.baseline_held_out.is_empty(),
            "not_beating_default" => false,
            t => flags.iter().any(|f| f == t),
        };
        if hit {
            reasons.push(tok.to_owned());
        }
    }
    CalibrationTrust {
        verdict: if reasons.is_empty() {
            TrustVerdict::Exportable
        } else {
            TrustVerdict::Refused
        },
        reasons,
        flags,
        beats_default: Default::default(),
    }
}
