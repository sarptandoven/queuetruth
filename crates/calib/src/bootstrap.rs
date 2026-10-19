//! Block bootstrap for calibration (SPEC 5.4, 7.2). Stream `Xoshiro256StarStar::new(stable_seed(seed, &[b"boot"]))`;
//! replicate `r` draws `n_blocks` block indices, each `range_inclusive(0, n_blocks - 1)`, replicate-major (the
//! stream recorded in `tests/fixtures/reference/bootstrap.json`, `xoshiro_block_resample`). Blocks are the
//! sorted distinct labels (session dates with >= 5 dates, else UTC half-hours `floor(ts / 30 min)`, as
//! `queuetruth.stats.block_labels`); the percentile interval is numpy's `quantile(method="linear")` over the finite
//! replicates, as `queuetruth.stats.percentile_ci`.

use std::collections::BTreeMap;

use qt_schema::specs::BootstrapBlock;
use qt_types::{Ts, Xoshiro256StarStar, stable_seed};

/// Dates needed for day blocks under `auto`.
pub const MIN_DAY_BLOCKS: usize = 5;
/// Half an hour in ns.
pub const HALF_HOUR_NS: i64 = 1_800_000_000_000;

/// Block index draws, replicate-major: `b` rows of `n_blocks` indices.
pub fn block_draws(n_blocks: usize, b: u32, seed: u64) -> Vec<Vec<u32>> {
    let mut rng = Xoshiro256StarStar::new(stable_seed(seed, &[b"boot"]));
    if n_blocks == 0 {
        return vec![Vec::new(); b as usize];
    }
    let hi = (n_blocks - 1) as u64;
    (0..b)
        .map(|_| (0..n_blocks).map(|_| rng.range_inclusive(0, hi) as u32).collect())
        .collect()
}

/// numpy `quantile(sorted, q, method="linear")` on an ascending slice (NaN if empty).
pub fn quantile_linear(sorted: &[f64], q: f64) -> f64 {
    let n = sorted.len();
    if n == 0 {
        return f64::NAN;
    }
    let vi = (n as f64 - 1.0) * q;
    let lo = vi.floor().clamp(0.0, (n - 1) as f64);
    let gamma = vi - lo;
    let i = lo as usize;
    let j = (i + 1).min(n - 1);
    let (a, b) = (sorted[i], sorted[j]);
    let d = b - a;
    if gamma >= 0.5 {
        b - d * (1.0 - gamma)
    } else {
        a + d * gamma
    }
}

/// Percentile interval `(alpha / 2, 1 - alpha / 2)` of the finite replicates (NaN if none).
pub fn percentile_ci(reps: &[f64], alpha: f64) -> (f64, f64) {
    let mut r: Vec<f64> = reps.iter().copied().filter(|x| x.is_finite()).collect();
    r.sort_by(f64::total_cmp);
    (quantile_linear(&r, alpha / 2.0), quantile_linear(&r, 1.0 - alpha / 2.0))
}

/// Replicates of `stat` over the draws, evaluated on up to `threads` threads (results in replicate order).
pub fn replicates(
    n_blocks: usize,
    b: u32,
    seed: u64,
    threads: usize,
    stat: &(impl Fn(&[u32]) -> f64 + Sync),
) -> Vec<f64> {
    let draws = block_draws(n_blocks, b, seed);
    let threads = threads.clamp(1, draws.len().max(1));
    if threads == 1 {
        return draws.iter().map(|d| stat(d)).collect();
    }
    let chunk = draws.len().div_ceil(threads);
    std::thread::scope(|s| {
        let handles: Vec<_> = draws
            .chunks(chunk)
            .map(|c| s.spawn(move || c.iter().map(|d| stat(d)).collect::<Vec<f64>>()))
            .collect();
        handles
            .into_iter()
            .flat_map(|h| h.join().unwrap_or_else(|_| vec![f64::NAN; chunk]))
            .collect()
    })
}

/// Percentile (2.5, 97.5) block bootstrap of `stat` over resampled block indices (single thread).
pub fn block_bootstrap(n_blocks: usize, b: u32, seed: u64, stat: impl Fn(&[u32]) -> f64 + Sync) -> (f64, f64) {
    percentile_ci(&replicates(n_blocks, b, seed, 1, &stat), 0.05)
}

/// Block kind actually used.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BlockKind {
    /// Session dates.
    Day,
    /// UTC half-hours.
    HalfHour,
}

/// Block index per row (into the sorted distinct labels), the number of blocks and the kind.
pub fn block_labels(dates: &[i32], ts: &[Ts], rule: BootstrapBlock) -> (Vec<u32>, usize, BlockKind) {
    let n_dates = dates.iter().collect::<std::collections::BTreeSet<_>>().len();
    let kind = match rule {
        BootstrapBlock::Day => BlockKind::Day,
        BootstrapBlock::Min30 => BlockKind::HalfHour,
        BootstrapBlock::Auto if n_dates >= MIN_DAY_BLOCKS => BlockKind::Day,
        BootstrapBlock::Auto => BlockKind::HalfHour,
    };
    let raw: Vec<i64> = match kind {
        BlockKind::Day => dates.iter().map(|d| i64::from(*d)).collect(),
        BlockKind::HalfHour => ts.iter().map(|t| t.div_euclid(HALF_HOUR_NS)).collect(),
    };
    let mut index: BTreeMap<i64, u32> = raw.iter().map(|l| (*l, 0)).collect();
    for (i, v) in index.values_mut().enumerate() {
        *v = i as u32;
    }
    let labels = raw.iter().map(|l| index.get(l).copied().unwrap_or(0)).collect();
    (labels, index.len(), kind)
}

/// Multiplicity of every block in one replicate.
pub fn counts(draw: &[u32], n_blocks: usize) -> Vec<u32> {
    let mut c = vec![0u32; n_blocks];
    for &d in draw {
        if let Some(x) = c.get_mut(d as usize) {
            *x += 1;
        }
    }
    c
}
