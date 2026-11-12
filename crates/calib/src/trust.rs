//! Fail-safe diagnostics of a fitted parameter (trader review D1, D2, E1): grid-edge estimates, flat likelihood
//! stretches and non-identified parameters are reported as `trust:` lines in `warnings` and in the structured
//! `qt.calibration_result.trust` (schema 1.1, `assess_rust`), which the export refuses. The export rule itself (the
//! held-out comparison with the default, the refusal reasons and the annotation, `assess` / `annotate` below) is
//! implemented here once; `queuetruth.export.trust` calls it through `_core`.
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

/// Python `format(x, ".6g")`, exactly: 6 significant digits, trailing zeros trimmed, exponent form below 1e-4 or
/// from 1e6 after rounding (`1e-05`, `1.23457e+08`), and `nan` / `inf` / `-0` as Python writes them.
pub fn g6(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.into();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0" } else { "0" }.into();
    }
    fn trim(s: &str) -> &str {
        if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.')
        } else {
            s
        }
    }
    // The decimal exponent after rounding to 6 significant digits decides the form, as in C's `%g`.
    let sci = format!("{x:.5e}");
    let (m, e) = sci.split_once('e').unwrap_or((&sci, "0"));
    let e: i32 = e.parse().unwrap_or(0);
    if (-4..6).contains(&e) {
        let dec = (5 - e) as usize;
        trim(&format!("{x:.dec$}")).to_owned()
    } else {
        format!("{}e{}{:02}", trim(m), if e < 0 { '-' } else { '+' }, e.abs())
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

// ---- the export rule: held-out comparison with the default, refusal reasons, annotation ---------------------------
//
// One implementation, used by `calibrate` (Rust) and by `queuetruth.export.trust` (Python, through
// `_core.trust_assess` / `_core.trust_annotate`). The texts are the bytes stored in calibrations' `warnings`.

/// Relative tolerance of a tie between two held-out scores (as `queuetruth.export.trust.TIE_REL_TOL`).
pub const TIE_REL_TOL: f64 = 1e-9;

/// Generic texts of the refusing tokens known only from the structured field.
pub const TOKEN_TEXT: [(&str, &str); 9] = [
    ("no_estimate", "the fit has no finite estimate"),
    ("few_events", "fitted on fewer than 30 events"),
    ("at_grid_edge", "the maximum is at an end of the fitting grid"),
    (
        "flat_likelihood",
        "the likelihood is flat: the parameter is not determined by the data",
    ),
    ("not_identified", "the likelihood is the same at every grid value"),
    ("degenerate_ci", "the bootstrap interval has zero width"),
    (
        "own_orders_in_depth",
        "the user's own resting orders are still in the L2 depth the fit ran on",
    ),
    ("no_held_out", "no held-out comparison with the default"),
    ("not_beating_default", "the fit does not beat the default out of sample"),
];

/// Held-out comparison of the fitted and the default model at one horizon.
#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct HorizonVerdict {
    /// Horizon.
    pub horizon_ns: i64,
    /// Fitted log loss.
    pub fitted_logloss: Option<f64>,
    /// Default log loss.
    pub default_logloss: Option<f64>,
    /// Fitted Brier score.
    pub fitted_brier: Option<f64>,
    /// Default Brier score.
    pub default_brier: Option<f64>,
    /// `better` | `worse` | `tie` | `mixed` | `n/a`.
    pub label: &'static str,
}

/// Assessment of one calibration (`queuetruth.export.trust.Trust`).
#[derive(Clone, Debug, Default, PartialEq, serde::Serialize)]
pub struct Assessment {
    /// `trust:` flags found or derived: token -> text, in the order found.
    pub flags: Vec<(String, String)>,
    /// Held-out comparison per horizon, ascending.
    pub horizons: Vec<HorizonVerdict>,
    /// Fitted model id.
    pub fitted_id: Option<String>,
    /// Default model id.
    pub default_id: Option<String>,
    /// Refusal reasons as (token, text), in `REFUSING` order.
    pub reasons: Vec<(String, String)>,
}

impl Assessment {
    fn flag(&self, tok: &str) -> Option<&str> {
        self.flags.iter().find(|(k, _)| k == tok).map(|(_, v)| v.as_str())
    }

    fn set_flag(&mut self, tok: &str, text: String) {
        match self.flags.iter_mut().find(|(k, _)| k == tok) {
            Some(e) => e.1 = text,
            None => self.flags.push((tok.to_owned(), text)),
        }
    }
}

/// `100ms`, `1s`, `90s`, `5m` (exact units only; else `ns`): the duration form of the stored `trust:` lines.
pub fn dur(ns: i64) -> String {
    for (unit, size) in [
        ("h", 3_600_000_000_000i64),
        ("m", 60_000_000_000),
        ("s", 1_000_000_000),
        ("ms", 1_000_000),
    ] {
        if ns >= size && ns % size == 0 && !(unit == "m" && ns < 300_000_000_000) {
            return format!("{}{unit}", ns / size);
        }
    }
    if ns >= 1_000 && ns % 1_000 == 0 {
        return format!("{}us", ns / 1_000);
    }
    format!("{ns}ns")
}

fn tie(a: f64, b: f64) -> bool {
    (a - b).abs() <= TIE_REL_TOL * a.abs().max(b.abs()).max(1e-300)
}

/// -1 fitted better (lower), 0 tie, 1 worse, None not comparable.
fn cmp(f: Option<f64>, d: Option<f64>) -> Option<i8> {
    let (f, d) = (f?, d?);
    if !(f.is_finite() && d.is_finite()) {
        return None;
    }
    Some(if tie(f, d) {
        0
    } else if f < d {
        -1
    } else {
        1
    })
}

/// Label of one horizon from the log-loss and Brier comparisons.
pub fn horizon_label(ll: Option<i8>, br: Option<i8>) -> &'static str {
    let codes: Vec<i8> = [ll, br].into_iter().flatten().collect();
    if codes.is_empty() {
        "n/a"
    } else if codes.contains(&1) && codes.contains(&-1) {
        "mixed"
    } else if codes.contains(&1) {
        "worse"
    } else if codes.contains(&-1) {
        "better"
    } else {
        "tie"
    }
}

/// (fitted model id, default model id, per-horizon verdicts) from `held_out` / `baseline_held_out`.
pub fn held_out_verdicts(res: &CalibrationResult) -> (Option<String>, Option<String>, Vec<HorizonVerdict>) {
    let f = res.held_out.first();
    let d = res.baseline_held_out.first();
    let fid = f.map(|(k, _)| k.clone());
    let did = d.map(|(k, _)| k.clone());
    let (Some((_, fho)), Some((_, dho))) = (f, d) else {
        return (fid, did, Vec::new());
    };
    let mut keys: Vec<i64> = [&fho.logloss, &fho.brier, &dho.logloss, &dho.brier]
        .iter()
        .flat_map(|m| m.keys())
        .filter_map(|k| k.trim().parse::<i64>().ok())
        .collect();
    keys.sort_unstable();
    keys.dedup();
    let get = |m: &qt_schema::json::HMap, k: &str| m.get(k).copied().flatten();
    let hs = keys
        .into_iter()
        .map(|h| {
            let k = h.to_string();
            let (fl, dl) = (get(&fho.logloss, &k), get(&dho.logloss, &k));
            let (fb, db) = (get(&fho.brier, &k), get(&dho.brier, &k));
            HorizonVerdict {
                horizon_ns: h,
                fitted_logloss: fl,
                default_logloss: dl,
                fitted_brier: fb,
                default_brier: db,
                label: horizon_label(cmp(fl, dl), cmp(fb, db)),
            }
        })
        .collect();
    (fid, did, hs)
}

fn opt_g6(x: Option<f64>) -> String {
    x.map_or_else(|| "-".to_owned(), g6)
}

fn beats(hs: &[HorizonVerdict]) -> bool {
    let labels: Vec<&str> = hs.iter().map(|h| h.label).filter(|l| *l != "n/a").collect();
    !labels.is_empty() && labels.contains(&"better") && !labels.iter().any(|l| *l == "worse" || *l == "mixed")
}

/// One line: `beats the default <id> out of sample: better at 1s; tie at 100ms` / `DOES NOT BEAT ...` /
/// `not evaluated ...`, with the horizons written by `fmt`.
pub fn held_out_summary(a: &Assessment, fmt: &dyn Fn(i64) -> String) -> String {
    if a.horizons.is_empty() || a.horizons.iter().all(|h| h.label == "n/a") {
        return "not evaluated (no held-out metrics for the fitted and the default model)".into();
    }
    let mut parts = Vec::new();
    for (label, word) in [
        ("worse", "WORSE at"),
        ("mixed", "MIXED (better on one score, worse on the other) at"),
        ("better", "better at"),
        ("tie", "tie at"),
        ("n/a", "not scored at"),
    ] {
        let hs: Vec<String> = a
            .horizons
            .iter()
            .filter(|h| h.label == label)
            .map(|h| fmt(h.horizon_ns))
            .collect();
        if !hs.is_empty() {
            parts.push(format!("{word} {}", hs.join(", ")));
        }
    }
    let detail = match a.horizons.iter().find(|h| h.label == "worse" || h.label == "mixed") {
        Some(h) => format!(
            " (log loss at {}: fitted {} vs default {}; Brier {} vs {})",
            fmt(h.horizon_ns),
            opt_g6(h.fitted_logloss),
            opt_g6(h.default_logloss),
            opt_g6(h.fitted_brier),
            opt_g6(h.default_brier)
        ),
        None => String::new(),
    };
    let verdict = if beats(&a.horizons) { "beats" } else { "DOES NOT BEAT" };
    format!(
        "{verdict} the default {} out of sample: {}{detail}",
        a.default_id.as_deref().unwrap_or("?"),
        parts.join("; ")
    )
}

/// `trust:` lines of a warnings list as (token, text); a later line of a token replaces the text in place.
pub fn parse_flags(warnings: &[String]) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for w in warnings {
        let Some(rest) = w.strip_prefix(PREFIX) else { continue };
        let n = rest
            .find(|c: char| !(c.is_ascii_lowercase() || c == '_'))
            .unwrap_or(rest.len());
        if n == 0 {
            continue;
        }
        let Some(text) = rest[n..].strip_prefix(": ") else {
            continue;
        };
        match out.iter_mut().find(|(k, _)| k == &rest[..n]) {
            Some(e) => e.1 = text.to_owned(),
            None => out.push((rest[..n].to_owned(), text.to_owned())),
        }
    }
    out
}

fn json_float(v: &serde_json::Value) -> qt_types::QtResult<f64> {
    match v {
        serde_json::Value::Number(n) => n.as_f64(),
        serde_json::Value::Bool(b) => Some(f64::from(u8::from(*b))),
        serde_json::Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
    .ok_or_else(|| qt_types::QtError::Config(format!("trust: grid bound {v} is not a number")))
}

/// Flags derivable from the numbers of a result written before `trust:` lines existed.
fn legacy_flags(res: &CalibrationResult) -> qt_types::QtResult<Vec<(String, String)>> {
    let mut out = Vec::new();
    if let Some((Some(lo), hi)) = res.ci_bootstrap
        && Some(lo) == hi
    {
        out.push((
            "degenerate_ci".to_owned(),
            format!("bootstrap CI [{}, {}] has zero width", g6(lo), g6(lo)),
        ));
    }
    let fam = res.family.to_lowercase();
    let fam = fam.strip_prefix("hbt_").unwrap_or(&fam);
    let grid = res.spec.get("grid").and_then(|g| g.as_object());
    if let (Some(est), Some(grid), qt_schema::json::CalibrationKind::Alloc) = (res.estimate, grid, res.kind) {
        let g = grid
            .get(if fam == "databento" { "databento" } else { "alloc" })
            .and_then(|g| g.as_object());
        if let Some(g) = g
            && g.contains_key("lo")
            && g.contains_key("hi")
        {
            let (lo, hi) = (json_float(&g["lo"])?, json_float(&g["hi"])?);
            // Python's `max(estimate, 1e-300)`: a NaN estimate stays NaN.
            let theta = if fam == "databento" {
                est
            } else {
                (if 1e-300 > est { 1e-300 } else { est }).ln()
            };
            if [lo, hi].iter().any(|e| (theta - e).abs() <= 1e-9 * e.abs().max(1.0)) {
                out.push((
                    "at_grid_edge".to_owned(),
                    format!("estimate {} is the end of the fitting grid", g6(est)),
                ));
            }
        }
    }
    Ok(out)
}

/// Exportability of a calibration from its `trust:` lines and its numbers (the structured `trust` field is
/// ignored): `queuetruth.export.trust` documents the rule. Texts write horizons with `dur`.
pub fn assess(res: &CalibrationResult) -> qt_types::QtResult<Assessment> {
    let mut a = Assessment {
        flags: parse_flags(&res.warnings),
        ..Assessment::default()
    };
    if !a.flags.iter().any(|(k, _)| !OWN_TOKENS.contains(&k.as_str())) {
        for (k, v) in legacy_flags(res)? {
            if a.flag(&k).is_none() {
                a.set_flag(&k, v);
            }
        }
    }
    let mut reasons: Vec<(&str, String)> = Vec::new();
    if res.kind == qt_schema::json::CalibrationKind::Latency {
        if res.latency.as_ref().is_none_or(|l| l.entry_ns.n == 0) {
            reasons.push((
                "no_estimate",
                "no entry-latency samples (no ack with an exchange match/gateway timestamp)".into(),
            ));
        }
        a.reasons = ordered(reasons);
        return Ok(a);
    }
    let (fid, did, hs) = held_out_verdicts(res);
    (a.fitted_id, a.default_id, a.horizons) = (fid, did, hs);
    if res.estimate.is_none_or(|e| !e.is_finite()) {
        reasons.push(("no_estimate", "the fit has no finite estimate".into()));
    }
    if res.n_events < MIN_EXPORT_EVENTS {
        reasons.push((
            "few_events",
            format!("fitted on {} event(s), fewer than {MIN_EXPORT_EVENTS}", res.n_events),
        ));
    }
    for tok in [
        "at_grid_edge",
        "flat_likelihood",
        "not_identified",
        "degenerate_ci",
        "own_orders_in_depth",
    ] {
        if let Some(t) = a.flag(tok) {
            reasons.push((tok, t.to_owned()));
        }
    }
    if a.horizons.is_empty() || a.horizons.iter().all(|h| h.label == "n/a") {
        reasons.push((
            "no_held_out",
            "no held-out comparison with the default (empty test window, or a Rust-only fit.json): the fit is not \
             shown to beat the default out of sample"
                .into(),
        ));
    } else if !beats(&a.horizons) {
        reasons.push(("not_beating_default", held_out_summary(&a, &dur)));
    }
    a.reasons = ordered(reasons);
    Ok(a)
}

fn ordered(reasons: Vec<(&str, String)>) -> Vec<(String, String)> {
    REFUSING
        .iter()
        .filter_map(|t| {
            reasons
                .iter()
                .find(|(k, _)| k == t)
                .map(|(k, v)| ((*k).to_owned(), v.clone()))
        })
        .collect()
}

/// The structured `trust` field (schema 1.1) of an assessment.
pub fn structured(a: &Assessment) -> CalibrationTrust {
    let mut flags: Vec<String> = a
        .flags
        .iter()
        .map(|(k, _)| k.clone())
        .filter(|k| !OWN_TOKENS.contains(&k.as_str()))
        .collect();
    flags.sort();
    CalibrationTrust {
        verdict: if a.reasons.is_empty() {
            TrustVerdict::Exportable
        } else {
            TrustVerdict::Refused
        },
        reasons: a.reasons.iter().map(|(k, _)| k.clone()).collect(),
        flags,
        beats_default: a
            .horizons
            .iter()
            .map(|h| {
                let v = serde_json::from_value(serde_json::Value::String(h.label.to_owned()))
                    .unwrap_or(qt_schema::json::BeatsDefault::NotAvailable);
                (h.horizon_ns.to_string(), v)
            })
            .collect(),
    }
}

/// `trust: beats_default: ...` (when held-out metrics exist) and `trust: verdict: ...` lines.
pub fn trust_lines(a: &Assessment) -> Vec<String> {
    let mut out = Vec::new();
    if !a.horizons.is_empty() {
        let codes: Vec<String> = a
            .horizons
            .iter()
            .map(|h| format!("{}={}", h.horizon_ns, h.label))
            .collect();
        out.push(format!(
            "{PREFIX}beats_default: {} ({})",
            codes.join(","),
            held_out_summary(a, &dur)
        ));
    }
    if a.reasons.is_empty() {
        out.push(format!("{PREFIX}verdict: exportable"));
    } else {
        let toks: Vec<&str> = a.reasons.iter().map(|(k, _)| k.as_str()).collect();
        out.push(format!("{PREFIX}verdict: refused ({})", toks.join(", ")));
    }
    out
}

/// `res` with the structured `trust` field and its `warnings` ending with the `beats_default` and `verdict` lines
/// (replacing older ones); the assessment ignores an earlier structured field.
pub fn annotate(res: &CalibrationResult) -> qt_types::QtResult<CalibrationResult> {
    let mut base = res.clone();
    base.warnings
        .retain(|w| !OWN_TOKENS.iter().any(|t| w.starts_with(&format!("{PREFIX}{t}:"))));
    base.trust = None;
    let a = assess(&base)?;
    base.warnings.extend(trust_lines(&a));
    base.trust = Some(structured(&a));
    Ok(base)
}
