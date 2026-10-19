//! Public result and option types of `qt_calib` (SPEC 4.1).

/// Allocation family.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum AllocFamily {
    /// hbt power (`theta = ln n`).
    Power,
    /// hbt power2 (`theta = ln n`).
    Power2,
    /// hbt power3 (`theta = ln n`).
    Power3,
    /// Databento (`theta = k`).
    Databento,
}

/// Grid and tolerance of the optimizer.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct FitOptions {
    /// Grid lower end.
    pub grid_lo: f64,
    /// Grid upper end.
    pub grid_hi: f64,
    /// Grid points.
    pub grid_n: usize,
    /// Golden-section tolerance.
    pub tol: f64,
}

/// Allocation fit.
#[derive(Clone, Debug, PartialEq)]
pub struct AllocFit {
    /// Family.
    pub family: AllocFamily,
    /// Estimate of theta.
    pub estimate: f64,
    /// Quasi log likelihood at the estimate.
    pub loglik: f64,
    /// Events used.
    pub n_events: u64,
    /// Profile CI (open side -> None).
    pub profile_ci: Option<(f64, f64)>,
    /// (theta, loglik) on the grid.
    pub grid: Vec<(f64, f64)>,
}

/// NT touch MLE.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TouchFit {
    /// Estimate of p.
    pub p_hat: f64,
    /// Orders used.
    pub n_orders: u64,
    /// Orders filled at a touch.
    pub n_filled: u64,
    /// Touches counted.
    pub touches: u64,
}

/// Survival fit.
#[derive(Clone, Debug, PartialEq)]
pub struct SurvivalFit {
    /// Estimate of k.
    pub estimate: f64,
    /// Log likelihood at the estimate.
    pub loglik: f64,
    /// (theta, loglik) on the grid.
    pub grid: Vec<(f64, f64)>,
}
