"""Interval estimators, one implementation each (SPEC 5.6, 5.7.5).

Proportions:
- `wilson(k, n, edges=...)`: Wilson score interval with `z = Z95` (two-sided 95%). `edges` picks how the bounds
  meet [0, 1], and the callers differ on purpose because each published its numbers that way:
  `"exact"` (the report's reliability diagrams) returns exactly 0 at `k = 0` and exactly 1 at `k = n`, and `(0, 1)`
  for `n <= 0`; `"clip"` (the candle audit of `live.importers.freqtrade`) clips to [0, 1] and returns None for
  `n = 0`; `"none"` (the forecast calibration bins) returns the formula as computed, where rounding can leave the
  upper bound one ulp below 1 at `k = n`.
- `wilson_arrays(rate, n)`: the same formula, vectorised over observed rates and counts, no edge handling (the
  reliability table of `stats.calibration`).
- `clopper_pearson(k, n, level)`: the exact binomial interval from beta quantiles; (None, None) for `n <= 0`.

Bootstrap:
- `percentile_ci(replicates, alpha)`: linear quantiles at `alpha/2` and `1 - alpha/2` of the finite replicates.
- `ratio_replicates(counts, num, den)`: replicates of a ratio of sums (`sum num / sum den`) from per-block sums and
  the `(b, n_blocks)` block multiplicities of `stats.block_counts`; NaN where a replicate's denominator is 0.
  `summation="matmul"` (`counts @ sums`, every caller since the scorer) and `summation="elementwise"`
  (`(counts * sums).sum(axis=1)`, the zoo leaderboard) agree to about 1e-12 but not bit for bit: the zoo's
  published intervals keep the elementwise order.
- `block_counts_chunks(n_blocks, b, seed, max_cells)`: `block_counts` produced in row chunks of at most
  `max_cells` cells (float), for bootstraps whose draw matrix would not fit in memory at once (forecast clusters).

The block and cluster entry points themselves (`block_labels`, `ci_blocks` with the short-capture policy,
`block_counts`, `block_replicates`, `day_block_bootstrap`, `bootstrap_seed`) are in `queuetruth.stats.resampling`;
`queuetruth.stats` exports all of them. There is no BCa interval in queuetruth: every published bootstrap interval
is a percentile interval.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Literal

import numpy as np
from scipy.stats import beta as beta_dist

#: two-sided 95% normal quantile, `scipy.stats.norm.ppf(0.975)` to the last bit
Z95 = 1.959963984540054


def wilson(
    k: float, n: float, *, z: float = Z95, edges: Literal["exact", "clip", "none"] = "exact"
) -> tuple[float, float] | None:
    """Wilson score interval for `k` successes in `n` trials (module docstring for `edges`)."""
    if n <= 0:
        if edges == "exact":
            return (0.0, 1.0)
        if edges == "clip":
            return None
    phat = k / n
    den = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / den
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / den
    if edges == "exact":
        return (0.0 if k <= 0 else max(0.0, centre - half), 1.0 if k >= n else min(1.0, centre + half))
    if edges == "clip":
        return (max(0.0, centre - half), min(1.0, centre + half))
    return (centre - half, centre + half)


def wilson_arrays(rate: np.ndarray, n: np.ndarray, *, z: float = Z95) -> tuple[np.ndarray, np.ndarray]:
    """Wilson bounds per element of observed rates `rate` over counts `n` (float arrays, no edge handling)."""
    z2 = z * z
    centre = (rate + z2 / (2 * n)) / (1 + z2 / n)
    half = z * np.sqrt(rate * (1 - rate) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return centre - half, centre + half


def clopper_pearson(k: int, n: int, level: float = 0.95) -> tuple[float | None, float | None]:
    """Exact binomial interval for `k` successes in `n` (rows treated as independent)."""
    if n <= 0:
        return None, None
    a = (1.0 - level) / 2.0
    lo = 0.0 if k == 0 else float(beta_dist.ppf(a, k, n - k + 1))
    hi = 1.0 if k == n else float(beta_dist.ppf(1.0 - a, k + 1, n - k))
    return lo, hi


def percentile_ci(replicates: np.ndarray, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile interval of the finite replicates (NaN if none)."""
    r = np.asarray(replicates, dtype=float)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return float("nan"), float("nan")
    lo, hi = np.quantile(r, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(lo), float(hi)


def ratio_replicates(
    counts: np.ndarray,
    num: np.ndarray,
    den: np.ndarray,
    *,
    summation: Literal["matmul", "elementwise"] = "matmul",
) -> np.ndarray:
    """Bootstrap replicates of `sum num / sum den` from per-block sums and block multiplicities `counts`
    (`(b, n_blocks)`); NaN where a replicate's denominator is not positive."""
    if summation == "matmul":
        rn, rd = counts @ num, counts @ den
    else:
        rn = (counts * num[None, :]).sum(axis=1)
        rd = (counts * den[None, :]).sum(axis=1)
    return np.where(rd > 0, rn / np.where(rd > 0, rd, 1.0), np.nan)


def block_counts_chunks(n_blocks: int, b: int, seed: int, max_cells: int = 4_000_000) -> Iterator[np.ndarray]:
    """`(rows, n_blocks)` float multiplicity blocks whose concatenation equals
    `queuetruth.stats.block_counts(n_blocks, b, seed)`, each at most `max_cells` cells."""
    if b <= 0:
        return
    rng = np.random.default_rng(seed)
    step = max(1, max_cells // max(n_blocks, 1))
    done = 0
    while done < b:
        c = min(step, b - done)
        draws = rng.integers(0, n_blocks, size=(c, n_blocks))
        flat = (np.arange(c, dtype=np.int64)[:, None] * n_blocks + draws).ravel()
        yield np.bincount(flat, minlength=c * n_blocks).reshape(c, n_blocks).astype(float)
        done += c


__all__ = [
    "Z95",
    "block_counts_chunks",
    "clopper_pearson",
    "percentile_ci",
    "ratio_replicates",
    "wilson",
    "wilson_arrays",
]
