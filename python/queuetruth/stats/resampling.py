"""Block bootstrap (SPEC 5.6, 5.7.5).

Blocks are session dates when a run has at least 5 distinct dates, else 30-minute buckets of the timestamp
(`block_labels`). One replicate draws `n_blocks` block indices with replacement
(`rng.integers(0, n_blocks, size=(b, n_blocks))`, all replicates drawn at once) and evaluates the statistic on the
concatenated rows of the drawn blocks. Percentile CIs use `numpy.nanquantile` (linear) at `alpha/2`, `1 - alpha/2`;
replicates whose statistic is NaN are ignored. The generator is `numpy.random.default_rng(seed)`; callers derive
`seed = stable_seed(run_seed, [b"bootstrap", test_id])` (`bootstrap_seed`) so every test has its own stream.
`block_counts` returns the same draws as per-block multiplicities, so ratio statistics (`sum num / sum den`) can be
bootstrapped as `counts @ num_by_block / counts @ den_by_block` with replicates identical in distribution to
`day_block_bootstrap`.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from queuetruth.stats._seed import stable_seed
from queuetruth.stats._types import CI

MIN_DAY_BLOCKS = 5
HALF_HOUR_NS = 1_800_000_000_000


def bootstrap_seed(seed: int, test_id: str) -> int:
    """`stable_seed(seed, [b"bootstrap", test_id])`."""
    return stable_seed(seed, [b"bootstrap", test_id])


def bootstrap_rng(seed: int, test_id: str) -> np.random.Generator:
    """`numpy.random.default_rng(stable_seed(seed, [b"bootstrap", test_id]))`."""
    return np.random.default_rng(bootstrap_seed(seed, test_id))


def _days(session_date: np.ndarray) -> np.ndarray:
    x = np.asarray(session_date)
    if np.issubdtype(x.dtype, np.datetime64):
        return x.astype("datetime64[D]").astype(np.int64)
    if x.dtype == object:
        return np.array([np.datetime64(v, "D").astype(np.int64) for v in x], dtype=np.int64)
    return x.astype(np.int64)


def _ns(ts: np.ndarray) -> np.ndarray:
    x = np.asarray(ts)
    if np.issubdtype(x.dtype, np.datetime64):
        return x.astype("datetime64[ns]").astype(np.int64)
    return x.astype(np.int64)


def block_labels(
    session_date: np.ndarray, ts_ns: np.ndarray, min_dates: int = MIN_DAY_BLOCKS
) -> tuple[np.ndarray, str]:
    """Block label per row and the block kind: `("day", days since epoch)` with >= `min_dates` distinct dates, else
    `("30min", floor(ts / 30 min))` (UTC half-hours; equal to local half-hours for whole/half-hour UTC offsets)."""
    days = _days(session_date)
    if np.unique(days).size >= min_dates:
        return days, "day"
    return np.floor_divide(_ns(ts_ns), HALF_HOUR_NS), "30min"


def _draws(n_blocks: int, b: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, n_blocks, size=(int(b), n_blocks))


def block_counts(n_blocks: int, b: int, seed: int) -> np.ndarray:
    """`(b, n_blocks)` multiplicity of each block in each replicate (same draws as `day_block_bootstrap`)."""
    if n_blocks <= 0:
        return np.zeros((int(b), 0), dtype=np.int64)
    draws = _draws(n_blocks, b, seed)
    counts = np.zeros((int(b), n_blocks), dtype=np.int64)
    np.add.at(counts, (np.repeat(np.arange(int(b)), n_blocks), draws.ravel()), 1)
    return counts


def block_replicates(
    blocks: np.ndarray, stat: Callable[[np.ndarray], float], b: int, seed: int
) -> tuple[float, np.ndarray]:
    """`(stat(all rows), replicates)`; `stat` receives row indices (with repetition) into the caller's data."""
    labels = np.asarray(blocks).ravel()
    n = labels.size
    estimate = float(stat(np.arange(n)))
    if n == 0:
        return estimate, np.full(int(b), np.nan)
    _, inv = np.unique(labels, return_inverse=True)
    order = np.argsort(inv, kind="stable")
    bounds = np.cumsum(np.bincount(inv))
    members = np.split(order, bounds[:-1])
    draws = _draws(len(members), b, seed)
    reps = np.empty(int(b))
    for i in range(int(b)):
        reps[i] = float(stat(np.concatenate([members[j] for j in draws[i]])))
    return estimate, reps


def percentile_ci(replicates: np.ndarray, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile interval of the finite replicates (NaN if none)."""
    r = np.asarray(replicates, dtype=float)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return float("nan"), float("nan")
    lo, hi = np.quantile(r, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(lo), float(hi)


def bootstrap_p_two_sided(replicates: np.ndarray, null: float = 0.0) -> float:
    """`min(1, 2 min((1 + #{rep <= null}) / (1 + B), (1 + #{rep >= null}) / (1 + B)))` over the B finite replicates
    (SPEC 5.7.5 test, capped at 1 and never exactly 0 by the (1 + #) / (1 + B) form)."""
    r = np.asarray(replicates, dtype=float)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return float("nan")
    lo = (1 + int(np.sum(r <= null))) / (1 + r.size)
    hi = (1 + int(np.sum(r >= null))) / (1 + r.size)
    return float(min(1.0, 2.0 * min(lo, hi)))


def bootstrap_se(replicates: np.ndarray) -> float:
    """Bootstrap standard deviation (ddof = 1) of the finite replicates."""
    r = np.asarray(replicates, dtype=float)
    r = r[np.isfinite(r)]
    return float(np.std(r, ddof=1)) if r.size > 1 else float("nan")


def day_block_bootstrap(
    blocks: np.ndarray, stat: Callable[[np.ndarray], float], b: int, seed: int, alpha: float = 0.05
) -> CI:
    """Block bootstrap percentile CI over resampled blocks."""
    estimate, reps = block_replicates(blocks, stat, b, seed)
    lo, hi = percentile_ci(reps, alpha)
    return CI(estimate=estimate, lo=lo, hi=hi)
