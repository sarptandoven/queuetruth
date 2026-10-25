"""Selection-aware statistics: is the best of many still good once the search is counted?

numpy/scipy only. Every bootstrap is deterministic: the generator is `bootstrap_rng(seed, test_id)` (that is
`numpy.random.default_rng(stable_seed(seed, [b"bootstrap", test_id]))`) and cluster draws are `block_counts(n_blocks,
b, bootstrap_seed(seed, test_id))`, the same draw matrix as every other `queuetruth.stats` block bootstrap (the wild
cluster bootstrap of the MCS draws its weights from `bootstrap_rng(seed, test_id)`).

Sharpe ratios (Bailey and Lopez de Prado 2012, 2014). `sharpe_ratio = mean / std (ddof = 1)` per period, never
annualized. With `g3` the sample skewness and `g4` the sample (Pearson, non-excess) kurtosis, both the biased moment
estimators of `scipy.stats.skew` / `scipy.stats.kurtosis(fisher=False)`, the estimator's standard error is
`sigma(SR) = sqrt((1 - g3 SR + (g4 - 1) / 4 SR^2) / (n - 1))` (Mertens 2002; `= sqrt((1 + SR^2 / 2) / (n - 1))`
for normal returns, Lo 2002) and
- `probabilistic_sharpe_ratio = Phi((SR - SR*) / sigma(SR))`: probability that the true Sharpe exceeds `SR*`;
- `expected_max_sharpe(N, V) = mean + sqrt(V) ((1 - gamma) Phi^-1(1 - 1/N) + gamma Phi^-1(1 - 1/(N e)))`
  (`gamma` Euler-Mascheroni): the expected maximum of `N` independent trial Sharpes of variance `V` under a zero true
  Sharpe (`mean` for `N = 1`);
- `deflated_sharpe_ratio = PSR(SR* = expected_max_sharpe(N, V))`: the PSR of the selected trial against the best
  Sharpe that `N` skill-less trials would show;
- `min_track_record_length = 1 + (1 - g3 SR + (g4 - 1) / 4 SR^2) (Phi^-1(1 - alpha) / (SR - SR*))^2`: the `n` at
  which `PSR(SR*) = 1 - alpha`.

Probability of backtest overfitting (Bailey, Borwein, Lopez de Prado, Zhu 2017), CSCV: the `T x N` performance
matrix (rows = periods in time order, columns = trial configurations) is cut into `S` contiguous row blocks
(`numpy.array_split`, sizes differ by at most one row). For each of the `C(S, S/2)` ways to pick half the blocks as
in-sample (IS, the rest out-of-sample, OOS) the IS-best trial `n*` (first on ties) gets its OOS rank `r` among the
`N` OOS performances (ascending, average ranks for ties), `w = r / (N + 1)` and `logit = ln(w / (1 - w))`;
`pbo = #{logit <= 0} / C(S, S/2)`. Also reported: `prob_oos_loss = #{OOS(n*) < 0} / C(S, S/2)` and the OLS
degradation line `OOS(n*) = intercept + slope IS(n*)`. Performance is the Sharpe ratio (default) or the mean.

Benchmark tests on losses (`d_k = L_benchmark - L_k`, positive = model `k` better; `n` rows, `k` models):
- `spa_test` (Hansen 2005): `t_k = sqrt(n) dbar_k / omega_k`, `T = max(0, max_k t_k)`; bootstrap
  `T*_b = max(0, max_k sqrt(n) (dbar*_kb - mu_k) / omega_k)` with `mu_k = max(dbar_k, 0)` (lower),
  `dbar_k 1{dbar_k >= -sqrt(omega_k^2 / n * 2 ln ln n)}` (consistent) or `dbar_k` (upper, White's Reality Check);
  `p = #{T*_b >= T} / B` (`>=`: with `T = 0`, no model ahead of the benchmark, `p = 1`; with continuous losses
  it equals Hansen's strict count and `arch`'s). `omega_k^2` estimates `Var(sqrt(n) dbar_k)`: the
  stationary-bootstrap kernel of Politis and Romano (1994) with `q = 1 / block_size` (Hansen 2005 section 2;
  row-index bootstraps) or the bootstrap variance `n mean_b (dbar*_kb - dbar_k)^2` (cluster bootstraps). With
  `studentize=False`, `omega_k = 1` (the statistic of `arch.bootstrap.SPA`, which never studentizes); the
  consistent threshold always uses the estimated `omega_k`.
- `stepm` (Romano and Wolf 2005): step `j` takes `c_j` = the `1 - alpha` quantile (`numpy.quantile`, linear) of
  `max_{k in A_j} sqrt(n) (dbar*_kb - mu_k) / omega_k` over the models `A_j` not yet rejected and rejects
  `H_k: E[d_k] <= 0` for every `k` in `A_j` with `t_k > c_j`; it stops when a step rejects nothing (or nothing is
  left). `recenter="upper"` (`mu_k = dbar_k`) is Romano and Wolf; `"consistent"` is Hansen's SPA recentering (the
  choice of `arch.bootstrap.StepM`). Controls the familywise error rate at `alpha`.

Model confidence set (Hansen, Lunde, Nason 2011) on an `n x m` loss matrix: start from all models; at each step test
equal expected loss among the active set `A` and, if rejected at `alpha`, drop the worst model. Bootstrap means
`Lbar*_ib` (one draw for every model, so pairs keep their dependence) give centred replicates and variances:
- `method="R"` (`T_R`): `t_ij = (Lbar_i - Lbar_j) / se_ij`, `se_ij^2 = mean_b ((Lbar*_ib - Lbar*_jb) - (Lbar_i -
  Lbar_j))^2`, `T_R = max_{i, j in A} t_ij`, eliminate `argmax_i max_{j in A} t_ij`;
- `method="max"` (`T_max`): `d_i = Lbar_i - mean_{j in A} Lbar_j`, `se_i^2 = mean_b (d*_ib - d_i)^2` (recomputed
  per step), `T_max = max_{i in A} d_i / se_i`, eliminate its argmax;
- `p_step = #{T*_b >= T} / B`; the MCS p-value of the model eliminated at step `j` is `max_{s <= j} p_step(s)`; the
  last model has p-value 1; the `1 - alpha` MCS is `{i: p_i > alpha}`. A ratio with a zero standard error reads
  0 when its numerator is 0 (identical forecasts are never separated) and `+-inf` otherwise; ties eliminate the
  lowest index first. `>=` (not `>`) keeps identical models in the set; with continuous losses it equals the
  strict count of `arch.bootstrap.MCS`. A p-value has resolution `1 / B`: 0 means below `1 / B`.
- `studentize="fixed"` is the studentization above (Hansen, Lunde and Nason; `arch`). With few clusters it treats
  `se_ij` as known and the replicate maxima come out too small: measured whole-set coverage of the 90% `T_R` MCS
  of 16 equal models on 13 clusters was 0.42 instead of 0.90 (1,000 simulations, docs/concepts/selection-bias.md).
  With `blocks=` the default `studentize="auto"` is `"wild"`, the wild cluster bootstrap-t (Cameron, Gelbach and
  Miller 2008; Webb's six-point weights `WEBB_WEIGHTS`, Webb 2023): with `e_g = S_g - n_g Lbar` the per-cluster
  residuals (`S_g` sums, `n_g` rows, `N = sum n_g`), `se_ij^2 = sum_g (e_gi - e_gj)^2 / N^2`; replicate `b` draws one
  weight `w_bg` per cluster (from `bootstrap_rng(seed, test_id)`, shared by every model), `Lbar*_b = Lbar + sum_g w_bg
  e_g / N` and is studentized with its own `se*_bij^2 = sum_g (e*_bgi - e*_bgj)^2 / N^2`, `e*_bg = w_bg e_g - n_g
  (Lbar*_b - Lbar)`; `T_max` likewise with `e_gi - mean_{j in A} e_gj`. Measured coverage of the same set: 0.88 at 13
  clusters, 0.83 at 5; it fails at 3 clusters (0.2), which is why the zoo leaderboard needs 5. Memory is
  `O(B m^2)` for the cached pairwise `t*` up to `2 CHUNK_ELEMENTS` elements, else chunks of at most
  `CHUNK_ELEMENTS` recomputed at each of the `m - 1` steps (time `O(B G m^3)` for wild `T_R`).

Bootstraps (`bootstrap=`): `"stationary"` (Politis and Romano 1994: a new block starts with probability
`1 / block_size` at each row, block starts uniform, indices wrap), `"circular"` (fixed blocks of `block_size`,
wrapping), `"moving"` (fixed blocks inside the sample). `block_size` defaults to `int(sqrt(n))` (as `arch`). Draws
come in chunks of at most `CHUNK_ELEMENTS` indices (one chunk, hence the single-call draws, when `B n` fits).
`blocks=` (one cluster label per row, e.g. session dates or 30-minute buckets from `block_labels`) switches to the
cluster bootstrap of `queuetruth.stats.resampling` (the MCS uses the wild one above unless `studentize="fixed"`):
replicate mean `= counts_b @ S_k / counts_b @ n_k` over per-cluster sums; it needs at least `MIN_CLUSTERS = 2`
clusters (`ValueError` otherwise: one cluster has no between-cluster variance and every replicate would equal the
sample). SPA and StepM studentize with the fixed
bootstrap variance only; with few clusters treat their p-values as optimistic. `indices=` (a `B x n` integer
array) supplies the draws directly (cross-checks).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from itertools import combinations
from typing import Literal

import numpy as np
from scipy import stats as sps

from queuetruth.stats.resampling import block_counts, bootstrap_rng, bootstrap_seed

EULER_GAMMA = 0.5772156649015329
Bootstrap = Literal["stationary", "circular", "moving"]
Recenter = Literal["lower", "consistent", "upper"]
Studentize = Literal["auto", "fixed", "wild"]


# ---- Sharpe ratio family ----------------------------------------------------------------------------------------


def _returns(returns: np.ndarray) -> np.ndarray:
    r = np.asarray(returns, dtype=float).ravel()
    if r.size < 2:
        raise ValueError("need at least two returns")
    if not np.isfinite(r).all():
        raise ValueError("returns must be finite")
    return r


def sharpe_ratio(returns: np.ndarray) -> float:
    """Per-period Sharpe ratio `mean / std (ddof = 1)`; 0 for a constant zero series, `+-inf` for a constant
    non-zero one."""
    r = _returns(returns)
    mu = float(np.mean(r))
    sd = float(np.std(r, ddof=1))
    if sd == 0.0:
        return 0.0 if mu == 0.0 else math.copysign(math.inf, mu)
    return mu / sd


def sharpe_moments(returns: np.ndarray) -> tuple[float, int, float, float]:
    """`(SR, n, skewness, kurtosis)`: biased sample moments, Pearson (non-excess) kurtosis (3 for constant series)."""
    r = _returns(returns)
    sr = sharpe_ratio(r)
    if float(np.std(r)) == 0.0:
        return sr, int(r.size), 0.0, 3.0
    return sr, int(r.size), float(sps.skew(r)), float(sps.kurtosis(r, fisher=False))


def sharpe_ratio_std(sr: float, n: int, *, skew: float = 0.0, kurtosis: float = 3.0) -> float:
    """Standard error of the Sharpe estimator, `sqrt((1 - g3 SR + (g4 - 1) / 4 SR^2) / (n - 1))`."""
    if n < 2:
        raise ValueError(f"need n >= 2, got {n}")
    q = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    if not q > 0.0:
        raise ValueError(f"inconsistent moments: 1 - skew SR + (kurtosis - 1) / 4 SR^2 = {q} <= 0")
    return math.sqrt(q / (n - 1))


def probabilistic_sharpe_ratio(
    sr: float, n: int, *, skew: float = 0.0, kurtosis: float = 3.0, sr_benchmark: float = 0.0
) -> float:
    """`Phi((SR - SR*) / sigma(SR))` (Bailey and Lopez de Prado 2012); an infinite `SR` (a constant non-zero
    series) reads 1 above the benchmark and 0 below."""
    if math.isnan(sr) or math.isnan(sr_benchmark):
        raise ValueError("sr and sr_benchmark must not be NaN")
    if math.isinf(sr):
        return 1.0 if sr > sr_benchmark else 0.0
    return float(sps.norm.cdf((sr - sr_benchmark) / sharpe_ratio_std(sr, n, skew=skew, kurtosis=kurtosis)))


def psr_from_returns(returns: np.ndarray, sr_benchmark: float = 0.0) -> float:
    """PSR of a return series with its own sample moments."""
    sr, n, g3, g4 = sharpe_moments(returns)
    return probabilistic_sharpe_ratio(sr, n, skew=g3, kurtosis=g4, sr_benchmark=sr_benchmark)


def expected_max_sharpe(n_trials: int, var_sr: float, mean_sr: float = 0.0) -> float:
    """Expected maximum Sharpe of `n_trials` independent skill-less trials whose Sharpes have variance `var_sr`."""
    if n_trials < 1:
        raise ValueError(f"need n_trials >= 1, got {n_trials}")
    if not var_sr >= 0.0:
        raise ValueError(f"var_sr must be >= 0, got {var_sr}")
    if n_trials == 1:
        return float(mean_sr)
    z1 = sps.norm.ppf(1.0 - 1.0 / n_trials)
    z2 = sps.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(mean_sr + math.sqrt(var_sr) * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


def deflated_sharpe_ratio(
    sr: float,
    n: int,
    *,
    n_trials: int,
    var_sr: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    mean_sr: float = 0.0,
) -> float:
    """PSR of the selected trial against `expected_max_sharpe(n_trials, var_sr, mean_sr)` (Bailey and Lopez de Prado
    2014). Equal to PSR(0) for one trial with `mean_sr = 0`."""
    sr0 = expected_max_sharpe(n_trials, var_sr, mean_sr)
    return probabilistic_sharpe_ratio(sr, n, skew=skew, kurtosis=kurtosis, sr_benchmark=sr0)


def dsr_from_returns(returns: np.ndarray, trial_sharpes: np.ndarray, *, n_trials: int | None = None) -> float:
    """DSR of `returns` (the selected trial) with `V = var(trial_sharpes, ddof = 1)` and `N = n_trials`, or
    `len(trial_sharpes)` without it; `trial_sharpes` holds the per-period Sharpes of every trial that has a series,
    the selected one included. Pass `n_trials` (>= `len(trial_sharpes)`) when trials without a series were also
    tried, as a trial ledger counts them, so the deflation is not understated."""
    ts = np.asarray(trial_sharpes, dtype=float).ravel()
    if ts.size < 1 or not np.isfinite(ts).all():
        raise ValueError("trial_sharpes must hold at least one finite Sharpe ratio")
    n_tr = int(ts.size) if n_trials is None else int(n_trials)
    if n_tr < ts.size:
        raise ValueError(f"n_trials = {n_trials} is below the {ts.size} trial Sharpes given")
    sr, n, g3, g4 = sharpe_moments(returns)
    var = float(np.var(ts, ddof=1)) if ts.size > 1 else 0.0
    return deflated_sharpe_ratio(sr, n, n_trials=n_tr, var_sr=var, skew=g3, kurtosis=g4)


def min_track_record_length(
    sr: float, *, sr_benchmark: float = 0.0, skew: float = 0.0, kurtosis: float = 3.0, alpha: float = 0.05
) -> float:
    """Track record length at which `PSR(sr_benchmark) = 1 - alpha` (inf when `sr <= sr_benchmark`)."""
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must lie in (0, 1), got {alpha}")
    if sr <= sr_benchmark:
        return math.inf
    q = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    if not q > 0.0:
        raise ValueError(f"inconsistent moments: 1 - skew SR + (kurtosis - 1) / 4 SR^2 = {q} <= 0")
    return float(1.0 + q * (sps.norm.ppf(1.0 - alpha) / (sr - sr_benchmark)) ** 2)


# ---- probability of backtest overfitting (CSCV) -----------------------------------------------------------------


@dataclass(frozen=True)
class PboResult:
    """CSCV outcome; arrays are per combination, in `itertools.combinations(range(S), S // 2)` order."""

    pbo: float
    logits: np.ndarray
    selected: np.ndarray
    is_performance: np.ndarray
    oos_performance: np.ndarray
    prob_oos_loss: float
    degradation_slope: float
    degradation_intercept: float
    n_splits: int
    n_combinations: int


def _perf(cnt: np.ndarray, mean: np.ndarray, m2: np.ndarray, rows: list[int], metric: str) -> np.ndarray:
    """Mean or Sharpe of the union of row blocks from per-block counts, means and centred sums of squares
    (pairwise combination of Chan, Golub and LeVeque, so a constant series has exactly zero spread)."""
    n = float(cnt[rows].sum())
    mu = (cnt[rows, None] * mean[rows]).sum(axis=0) / n
    if metric == "mean":
        return mu
    ss = m2[rows].sum(axis=0) + (cnt[rows, None] * (mean[rows] - mu) ** 2).sum(axis=0)
    sd = np.sqrt(np.maximum(ss, 0.0) / (n - 1.0))
    return _safe_ratio(mu, sd)


def probability_of_backtest_overfitting(
    performance: np.ndarray,
    *,
    n_splits: int = 16,
    metric: Literal["sharpe", "mean"] | Callable[[np.ndarray], np.ndarray] = "sharpe",
) -> PboResult:
    """PBO by combinatorially symmetric cross-validation (module docstring). `performance` is `T x N` (periods x
    trials); `metric` maps a `t x N` row subset to `N` performances (callables get the rows in time order)."""
    x = np.asarray(performance, dtype=float)
    if x.ndim != 2:
        raise ValueError("performance must be a T x N matrix")
    t_rows, n_trials = x.shape
    if n_splits < 2 or n_splits % 2:
        raise ValueError(f"n_splits must be even and >= 2, got {n_splits}")
    if n_trials < 2:
        raise ValueError("need at least two trials")
    if (t_rows // n_splits) * (n_splits // 2) < 2:
        raise ValueError(f"T = {t_rows} rows are too few for {n_splits} splits: each half needs at least 2 rows")
    if not np.isfinite(x).all():
        raise ValueError("performance must be finite")
    if not (callable(metric) or metric in ("sharpe", "mean")):
        raise ValueError(f"unknown metric {metric!r}")
    parts = np.array_split(np.arange(t_rows), n_splits)
    if not callable(metric):
        cnt = np.array([p.size for p in parts], dtype=float)
        bmean = np.stack([x[p].mean(axis=0) for p in parts])
        bm2 = np.stack([((x[p] - x[p].mean(axis=0)) ** 2).sum(axis=0) for p in parts])
    combos = list(combinations(range(n_splits), n_splits // 2))
    logits = np.empty(len(combos))
    selected = np.empty(len(combos), dtype=np.int64)
    perf_is = np.empty(len(combos))
    perf_oos = np.empty(len(combos))
    everything = set(range(n_splits))
    for c, ins in enumerate(combos):
        outs = sorted(everything.difference(ins))
        if callable(metric):
            r_is = np.asarray(metric(x[np.concatenate([parts[i] for i in ins])]), dtype=float).ravel()
            r_oos = np.asarray(metric(x[np.concatenate([parts[i] for i in outs])]), dtype=float).ravel()
            if r_is.size != n_trials or r_oos.size != n_trials:
                raise ValueError("metric must return one performance per trial")
            if np.isnan(r_is).any() or np.isnan(r_oos).any():
                # argmax would select a NaN trial and its NaN logit would count as "not overfit"
                raise ValueError("metric returned a NaN performance (e.g. a 0/0 Sharpe of a trial that never trades)")
        else:
            r_is = _perf(cnt, bmean, bm2, list(ins), metric)
            r_oos = _perf(cnt, bmean, bm2, outs, metric)
        best = int(np.argmax(r_is))
        rank = float(sps.rankdata(r_oos, method="average")[best])
        w = rank / (n_trials + 1.0)
        logits[c] = math.log(w / (1.0 - w))
        selected[c] = best
        perf_is[c] = r_is[best]
        perf_oos[c] = r_oos[best]
    finite = np.isfinite(perf_is) & np.isfinite(perf_oos)
    slope = intercept = math.nan
    if finite.sum() >= 2 and np.ptp(perf_is[finite]) > 0:
        slope, intercept = (float(v) for v in np.polyfit(perf_is[finite], perf_oos[finite], 1))
    return PboResult(
        pbo=float(np.mean(logits <= 0.0)),
        logits=logits,
        selected=selected,
        is_performance=perf_is,
        oos_performance=perf_oos,
        prob_oos_loss=float(np.mean(perf_oos < 0.0)),
        degradation_slope=slope,
        degradation_intercept=intercept,
        n_splits=int(n_splits),
        n_combinations=len(combos),
    )


# ---- bootstrap draws --------------------------------------------------------------------------------------------

#: Largest number of array elements a bootstrap step materialises at once (index draws, replicate statistics).
CHUNK_ELEMENTS = 1 << 22
#: Fewest clusters the cluster bootstrap accepts (one cluster has no between-cluster variance at all).
MIN_CLUSTERS = 2


def _block_size(n: int, block_size: int | None) -> int:
    bs = int(math.sqrt(n)) if block_size is None else int(block_size)
    if bs < 1:
        raise ValueError(f"block_size must be >= 1, got {block_size}")
    return max(1, min(bs, n))


def _chunk_rows(b: int, per_row: int) -> int:
    """Replicates per chunk so that a chunk holds at most `CHUNK_ELEMENTS` elements (at least one replicate)."""
    return max(1, min(int(b), CHUNK_ELEMENTS // max(1, int(per_row))))


def _index_chunks(
    n: int, b: int, *, block_size: int | None, kind: Bootstrap, seed: int, test_id: str
) -> Iterator[np.ndarray]:
    """The rows of `bootstrap_indices` in consecutive chunks of `_chunk_rows(b, n)` replicates, each drawn from the
    one generator in replicate order (a single chunk whenever `b n <= CHUNK_ELEMENTS`)."""
    if kind not in ("stationary", "circular", "moving"):
        raise ValueError(f"unknown bootstrap {kind!r}")
    bs = _block_size(n, block_size)
    rng = bootstrap_rng(seed, test_id)
    t = np.arange(n)
    n_blk = -(-n // bs)
    step = _chunk_rows(b, n)
    for lo in range(0, b, step):
        c = min(step, b - lo)
        if kind == "stationary":
            start = rng.integers(0, n, size=(c, n))
            new = rng.random((c, n)) < 1.0 / bs
            new[:, 0] = True
            last = np.maximum.accumulate(np.where(new, t, 0), axis=1)
            yield (np.take_along_axis(start, last, axis=1) + (t - last)) % n
        elif kind == "circular":
            starts = rng.integers(0, n, size=(c, n_blk))
            yield ((starts[:, :, None] + np.arange(bs)).reshape(c, -1)[:, :n]) % n
        else:
            starts = rng.integers(0, n - bs + 1, size=(c, n_blk))
            yield (starts[:, :, None] + np.arange(bs)).reshape(c, -1)[:, :n]


def bootstrap_indices(
    n: int,
    b: int,
    *,
    block_size: int | None = None,
    kind: Bootstrap = "stationary",
    seed: int = 0,
    test_id: str = "selection",
) -> np.ndarray:
    """`b x n` row indices of a stationary, circular or moving block bootstrap (module docstring), drawn from
    `bootstrap_rng(seed, test_id)` in chunks of at most `CHUNK_ELEMENTS` indices (the tests use the same draws
    internally without materialising all `b x n` of them)."""
    if n < 1 or b < 1:
        raise ValueError(f"need n >= 1 and b >= 1, got n={n}, b={b}")
    return np.concatenate(
        list(_index_chunks(n, b, block_size=block_size, kind=kind, seed=seed, test_id=test_id)), axis=0
    )


#: Webb's six-point wild-bootstrap weights (mean 0, variance 1; Webb 2023), drawn uniformly per cluster.
WEBB_WEIGHTS = np.array([-math.sqrt(1.5), -1.0, -math.sqrt(0.5), math.sqrt(0.5), 1.0, math.sqrt(1.5)])


@dataclass(frozen=True)
class _Clusters:
    sums: np.ndarray  # (G, k) per-cluster sums
    sizes: np.ndarray  # (G,) rows per cluster
    draws: np.ndarray  # (B, G): multiplicities (pairs cluster bootstrap) or Webb weights (wild)
    wild: bool

    def residuals(self, means: np.ndarray) -> np.ndarray:
        """Per-cluster residuals `e_g = S_g - n_g Lbar` (`G x k`), the linearisation of a ratio mean."""
        return self.sums - self.sizes[:, None] * means[None, :]


@dataclass(frozen=True)
class _Draws:
    means: np.ndarray  # (k,)
    reps: np.ndarray  # (B, k) replicate means
    n: int
    cluster: _Clusters | None  # the cluster bootstrap's sums and draws (None for row-index bootstraps)

    @property
    def clustered(self) -> bool:
        return self.cluster is not None


def _replicate_means(
    x: np.ndarray,
    *,
    b: int,
    seed: int,
    test_id: str,
    bootstrap: Bootstrap,
    block_size: int | None,
    blocks: np.ndarray | None,
    indices: np.ndarray | None,
    wild: bool = False,
) -> _Draws:
    n = x.shape[0]
    means = x.mean(axis=0)
    if blocks is not None and indices is not None:
        raise ValueError("pass blocks or indices, not both")
    if blocks is not None:
        _check_b(b)
        lab = np.asarray(blocks).ravel()
        if lab.size != n:
            raise ValueError(f"blocks must have one label per row: {lab.size} != {n}")
        _, inv = np.unique(lab, return_inverse=True)
        inv = np.asarray(inv).ravel()
        n_blocks = int(inv.max()) + 1
        if n_blocks < MIN_CLUSTERS:
            raise ValueError(
                f"the cluster bootstrap needs at least {MIN_CLUSTERS} clusters, got {n_blocks}: one cluster has no "
                "between-cluster variance, every replicate would equal the sample"
            )
        sums = np.zeros((n_blocks, x.shape[1]))
        np.add.at(sums, inv, x)
        sizes = np.bincount(inv, minlength=n_blocks).astype(float)
        if wild:
            # wild cluster bootstrap: Lbar*_b = Lbar + sum_g w_bg e_g / N, weights from bootstrap_rng(seed, test_id)
            w = WEBB_WEIGHTS[bootstrap_rng(seed, test_id).integers(0, WEBB_WEIGHTS.size, size=(int(b), n_blocks))]
            cl = _Clusters(sums, sizes, w, True)
            reps = means[None, :] + (w @ cl.residuals(sums.sum(axis=0) / sizes.sum())) / sizes.sum()
            return _Draws(means, reps, n, cl)
        counts = block_counts(n_blocks, b, bootstrap_seed(seed, test_id)).astype(float)
        reps = (counts @ sums) / (counts @ sizes)[:, None]
        return _Draws(means, reps, n, _Clusters(sums, sizes, counts, False))
    if indices is None:
        _check_b(b)
        if n < 1:
            raise ValueError("need at least one row")
        chunks: Iterable[np.ndarray] = _index_chunks(
            n, b, block_size=block_size, kind=bootstrap, seed=seed, test_id=test_id
        )
        n_reps = int(b)
    else:
        idx = np.asarray(indices)
        if idx.ndim != 2 or idx.shape[0] < 1 or idx.shape[1] != n or not np.issubdtype(idx.dtype, np.integer):
            raise ValueError(f"indices must be an integer B x {n} array with B >= 1")
        if idx.size and (idx.min() < 0 or idx.max() >= n):
            raise ValueError("indices out of range")
        chunks, n_reps = (idx,), idx.shape[0]
    reps = np.empty((n_reps, x.shape[1]))
    r = 0
    for chunk in chunks:
        for row in chunk:  # one gather per replicate: memory n k, never B n k
            reps[r] = x[row].mean(axis=0)
            r += 1
    return _Draws(means, reps, n, None)


def _safe_ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """`num / den`; 0 where both are 0, `+-inf` where only `den` is 0."""
    num = np.asarray(num, dtype=float)
    pos = np.asarray(den) > 0
    with np.errstate(divide="ignore", invalid="ignore"):
        out = num / np.where(pos, den, 1.0)
    return np.where(pos, out, np.where(num == 0, 0.0, np.copysign(np.inf, num)))


def _check_b(b: int) -> None:
    if b < 1:
        raise ValueError(f"need b >= 1 bootstrap replicates, got {b}")


# ---- SPA and StepM ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SpaResult:
    """Hansen's SPA test (module docstring). `mean_diff[k] = mean(L_benchmark - L_k)` (positive = model better),
    `omega[k]` the standard deviation of `sqrt(n) mean_diff[k]`, `t[k] = sqrt(n) mean_diff[k] / omega[k]`."""

    statistic: float
    p_lower: float
    p_consistent: float
    p_upper: float
    mean_diff: np.ndarray
    omega: np.ndarray
    t: np.ndarray
    n: int
    b: int


def _losses(benchmark: np.ndarray, models: np.ndarray) -> np.ndarray:
    bm = np.asarray(benchmark, dtype=float).ravel()
    md = np.asarray(models, dtype=float)
    if md.ndim == 1:
        md = md[:, None]
    if md.ndim != 2 or md.shape[0] != bm.size:
        raise ValueError(f"models must be n x k with n = {bm.size}, got shape {md.shape}")
    if md.shape[1] < 1:
        raise ValueError("need at least one model")
    if bm.size < 3:
        raise ValueError("need at least 3 rows (the consistent threshold uses ln ln n)")
    if not (np.isfinite(bm).all() and np.isfinite(md).all()):
        raise ValueError("losses must be finite")
    return bm[:, None] - md


def _kernel_variance(d: np.ndarray, block_size: int) -> np.ndarray:
    """Politis-Romano stationary-bootstrap variance of `sqrt(n) mean(d)` (Hansen 2005; `arch` `_compute_variance`):
    `gamma_0 + 2 sum_{i=1}^{n-1} kappa_i gamma_i`, `kappa_i = (1 - i/n)(1 - q)^i + (i/n)(1 - q)^(n-i)`, with the
    autocovariances `gamma_i = sum_t dm_t dm_{t+i} / n` of the demeaned columns (by FFT)."""
    n = d.shape[0]
    q = 1.0 / block_size
    dm = d - d.mean(axis=0)
    size = 1 << (2 * n - 1).bit_length()
    f = np.fft.rfft(dm, n=size, axis=0)
    gamma = np.fft.irfft(f * np.conj(f), n=size, axis=0)[:n] / n
    i = np.arange(1, n, dtype=float)
    kappa = (1.0 - i / n) * (1.0 - q) ** i + (i / n) * (1.0 - q) ** (n - i)
    var = gamma[0] + 2.0 * (kappa[:, None] * gamma[1:]).sum(axis=0)
    return np.maximum(var, 0.0)


@dataclass(frozen=True)
class _SpaCore:
    dbar: np.ndarray
    omega: np.ndarray
    scale: np.ndarray  # omega used in the statistic (1 without studentization)
    centred: dict[str, np.ndarray]  # recenter -> (B, k) replicate sqrt(n) (dbar* - mu) / scale
    t: np.ndarray
    n: int


def _spa_core(
    d: np.ndarray,
    *,
    b: int,
    seed: int,
    test_id: str,
    bootstrap: Bootstrap,
    block_size: int | None,
    blocks: np.ndarray | None,
    indices: np.ndarray | None,
    studentize: bool,
) -> _SpaCore:
    n = d.shape[0]
    dr = _replicate_means(
        d, b=b, seed=seed, test_id=test_id, bootstrap=bootstrap, block_size=block_size, blocks=blocks, indices=indices
    )
    dbar = dr.means
    if dr.clustered:
        omega2 = n * np.mean((dr.reps - dbar) ** 2, axis=0)
    else:
        omega2 = _kernel_variance(d, _block_size(n, block_size))
    omega = np.sqrt(omega2)
    scale = omega if studentize else np.ones_like(omega)
    root_n = math.sqrt(n)
    threshold = -np.sqrt(omega2 / n * 2.0 * math.log(math.log(n)))
    mus = {
        "lower": np.maximum(dbar, 0.0),
        "consistent": np.where(dbar >= threshold, dbar, 0.0),
        "upper": dbar,
    }
    centred = {k: _safe_ratio(root_n * (dr.reps - mu), np.broadcast_to(scale, dr.reps.shape)) for k, mu in mus.items()}
    # a zero scale has zero replicate spread around dbar; the replicate ratio reads 0 there, never inf
    t = _safe_ratio(root_n * dbar, scale)
    return _SpaCore(dbar, omega, scale, centred, t, n)


def spa_test(
    benchmark: np.ndarray,
    models: np.ndarray,
    *,
    b: int = 1000,
    seed: int = 0,
    test_id: str = "spa",
    bootstrap: Bootstrap = "stationary",
    block_size: int | None = None,
    blocks: np.ndarray | None = None,
    indices: np.ndarray | None = None,
    studentize: bool = True,
) -> SpaResult:
    """Hansen's test of superior predictive ability on losses: H0 = no model beats the benchmark."""
    d = _losses(benchmark, models)
    core = _spa_core(
        d,
        b=b,
        seed=seed,
        test_id=test_id,
        bootstrap=bootstrap,
        block_size=block_size,
        blocks=blocks,
        indices=indices,
        studentize=studentize,
    )
    stat = float(max(0.0, float(np.max(core.t))))
    p = {}
    for kind, rep in core.centred.items():
        t_star = np.maximum(0.0, rep.max(axis=1))
        p[kind] = float(np.mean(t_star >= stat))
    return SpaResult(
        statistic=stat,
        p_lower=p["lower"],
        p_consistent=p["consistent"],
        p_upper=p["upper"],
        mean_diff=core.dbar,
        omega=core.omega,
        t=core.t,
        n=core.n,
        b=int(core.centred["upper"].shape[0]),
    )


@dataclass(frozen=True)
class StepMResult:
    """Romano-Wolf StepM: `superior` = models (column indices, ascending) whose `H_k: E[L_benchmark - L_k] <= 0` is
    rejected; `steps[j]` = the models rejected at step `j`, `critical_values[j]` its critical value (t units)."""

    superior: tuple[int, ...]
    steps: tuple[tuple[int, ...], ...]
    critical_values: tuple[float, ...]
    t: np.ndarray
    mean_diff: np.ndarray
    alpha: float


def stepm(
    benchmark: np.ndarray,
    models: np.ndarray,
    *,
    alpha: float = 0.05,
    b: int = 1000,
    seed: int = 0,
    test_id: str = "stepm",
    bootstrap: Bootstrap = "stationary",
    block_size: int | None = None,
    blocks: np.ndarray | None = None,
    indices: np.ndarray | None = None,
    studentize: bool = True,
    recenter: Recenter = "upper",
) -> StepMResult:
    """Stepwise multiple testing of every model against the benchmark at familywise error `alpha`."""
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must lie in (0, 1), got {alpha}")
    if recenter not in ("lower", "consistent", "upper"):
        raise ValueError(f"unknown recenter {recenter!r}")
    d = _losses(benchmark, models)
    core = _spa_core(
        d,
        b=b,
        seed=seed,
        test_id=test_id,
        bootstrap=bootstrap,
        block_size=block_size,
        blocks=blocks,
        indices=indices,
        studentize=studentize,
    )
    sim = core.centred[recenter]
    active = np.ones(d.shape[1], dtype=bool)
    steps: list[tuple[int, ...]] = []
    crits: list[float] = []
    while active.any():
        crit = float(np.quantile(sim[:, active].max(axis=1), 1.0 - alpha))
        rejected = active & (core.t > crit)
        crits.append(crit)
        if not rejected.any():
            break
        steps.append(tuple(int(i) for i in np.flatnonzero(rejected)))
        active &= ~rejected
    superior = tuple(sorted(i for s in steps for i in s))
    return StepMResult(superior, tuple(steps), tuple(crits), core.t, core.dbar, float(alpha))


# ---- model confidence set ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class McsResult:
    """Model confidence set: `included` = column indices (ascending) with `p_values > alpha`; `eliminated` = the
    elimination order (the surviving model last); `step_p` = the test p-value at each elimination; `studentize` =
    `"fixed"` (Hansen, Lunde and Nason) or `"wild"` (wild cluster bootstrap-t), module docstring."""

    included: tuple[int, ...]
    p_values: np.ndarray
    eliminated: tuple[int, ...]
    step_p: tuple[float, ...]
    mean_loss: np.ndarray
    method: str
    alpha: float
    b: int
    studentize: str = "fixed"


def _replicate_residuals(cl: _Clusters, lo: int, step: int, idx: np.ndarray) -> np.ndarray:
    """Wild replicate residuals `e*_bg = w_bg e_g - n_g delta*_b`, `delta*_b = sum_g w_bg e_g / N` (`c x G x k`),
    of the active models `idx` for the replicates `lo:lo + step`."""
    w = cl.draws[lo : lo + step]
    e = cl.residuals(cl.sums.sum(axis=0) / cl.sizes.sum())[:, idx]
    delta = (w @ e) / cl.sizes.sum()
    return w[:, :, None] * e[None, :, :] - cl.sizes[None, :, None] * delta[:, None, :]


class _RangeStat:
    """`T_R` statistics: `t` (`m x m`) and, per active set, the replicate maxima of `t*` over its ordered pairs."""

    def __init__(self, dr: _Draws, wild: bool) -> None:
        self.dr, self.wild = dr, wild
        mean, reps = dr.means, dr.reps
        m = mean.size
        self.diff = mean[:, None] - mean[None, :]
        if wild:
            cl = dr.cluster
            assert cl is not None
            e = cl.residuals(mean)
            v = np.zeros((m, m))
            for g in range(e.shape[0]):  # G passes of m x m: never G m^2 at once
                v += (e[g, :, None] - e[g, None, :]) ** 2
            self.se = np.sqrt(v) / float(cl.sizes.sum())
            self.step = _chunk_rows(reps.shape[0], e.shape[0] * m * m)
        else:
            acc = None
            self.step = _chunk_rows(reps.shape[0], m * m)
            for lo in range(0, reps.shape[0], self.step):
                r = reps[lo : lo + self.step]
                sq = (((r[:, :, None] - r[:, None, :]) - self.diff) ** 2).sum(axis=0)
                acc = sq if acc is None else acc + sq
            self.se = np.sqrt(acc / reps.shape[0])  # type: ignore[operator]
        self.t = _safe_ratio(self.diff, self.se)
        # pairwise statistics depend only on their pair: cache t* for every pair when it is small enough
        self.cache = None
        if reps.shape[0] * m * m <= 2 * CHUNK_ELEMENTS:
            self.cache = np.concatenate(list(self._t_star(np.arange(m))), axis=0)

    def _t_star(self, idx: np.ndarray) -> Iterator[np.ndarray]:
        reps = self.dr.reps
        diff = self.diff[np.ix_(idx, idx)]
        for lo in range(0, reps.shape[0], self.step):
            r = reps[lo : lo + self.step][:, idx]
            num = (r[:, :, None] - r[:, None, :]) - diff
            if not self.wild:
                yield _safe_ratio(num, np.broadcast_to(self.se[np.ix_(idx, idx)], num.shape))
                continue
            cl = self.dr.cluster
            assert cl is not None
            eb = _replicate_residuals(cl, lo, self.step, idx)  # (c, G, k)
            vb = ((eb[:, :, :, None] - eb[:, :, None, :]) ** 2).sum(axis=1)
            yield _safe_ratio(num, np.sqrt(vb) / float(cl.sizes.sum()))

    def step_test(self, idx: np.ndarray) -> tuple[float, int, np.ndarray]:
        sub = self.t[np.ix_(idx, idx)]
        if self.cache is not None:
            sim = self.cache[:, idx][:, :, idx].reshape(self.cache.shape[0], -1).max(axis=1)
        else:
            sim = np.concatenate([ts.reshape(ts.shape[0], -1).max(axis=1) for ts in self._t_star(idx)])
        return float(sub.max()), int(np.argmax(sub.max(axis=1))), sim


class _MaxStat:
    """`T_max` statistics, recomputed for each active set."""

    def __init__(self, dr: _Draws, wild: bool) -> None:
        self.dr, self.wild = dr, wild
        if wild:
            cl = dr.cluster
            assert cl is not None
            self.e = cl.residuals(dr.means)
            self.step = _chunk_rows(dr.reps.shape[0], self.e.shape[0] * dr.means.size)

    def step_test(self, idx: np.ndarray) -> tuple[float, int, np.ndarray]:
        mean, reps = self.dr.means, self.dr.reps
        d = mean[idx] - mean[idx].mean()
        if not self.wild:
            d_star = reps[:, idx] - reps[:, idx].mean(axis=1, keepdims=True)
            cen = d_star - d
            se_i = np.sqrt(np.mean(cen**2, axis=0))
            ti = _safe_ratio(d, se_i)
            sim = _safe_ratio(cen, np.broadcast_to(se_i, cen.shape)).max(axis=1)
            return float(ti.max()), int(np.argmax(ti)), sim
        cl = self.dr.cluster
        assert cl is not None
        total = float(cl.sizes.sum())
        ed = self.e[:, idx] - self.e[:, idx].mean(axis=1, keepdims=True)
        ti = _safe_ratio(d, np.sqrt((ed**2).sum(axis=0)) / total)
        sims = []
        for lo in range(0, reps.shape[0], self.step):
            r = reps[lo : lo + self.step][:, idx]
            eb = _replicate_residuals(cl, lo, self.step, idx)  # (c, G, k)
            eb -= eb.mean(axis=2, keepdims=True)
            sd = np.sqrt((eb**2).sum(axis=1)) / total
            sims.append(_safe_ratio((r - r.mean(axis=1, keepdims=True)) - d, sd).max(axis=1))
        return float(ti.max()), int(np.argmax(ti)), np.concatenate(sims)


def model_confidence_set(
    losses: np.ndarray,
    *,
    alpha: float = 0.10,
    method: Literal["R", "max"] = "R",
    b: int = 1000,
    seed: int = 0,
    test_id: str = "mcs",
    bootstrap: Bootstrap = "stationary",
    block_size: int | None = None,
    blocks: np.ndarray | None = None,
    indices: np.ndarray | None = None,
    studentize: Studentize = "auto",
) -> McsResult:
    """Hansen-Lunde-Nason MCS on an `n x m` loss matrix (rows = observations, columns = models). `studentize="auto"`
    is the wild cluster bootstrap-t (`"wild"`) with `blocks=` and the fixed bootstrap variance of Hansen, Lunde and
    Nason (`"fixed"`) otherwise (module docstring)."""
    x = np.asarray(losses, dtype=float)
    if x.ndim != 2 or x.shape[0] < 2 or x.shape[1] < 1:
        raise ValueError(f"losses must be an n x m matrix with n >= 2, m >= 1; got shape {x.shape}")
    if not np.isfinite(x).all():
        raise ValueError("losses must be finite")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must lie in (0, 1), got {alpha}")
    if method not in ("R", "max"):
        raise ValueError(f"unknown MCS method {method!r}")
    if studentize not in ("auto", "fixed", "wild"):
        raise ValueError(f"unknown studentize {studentize!r}")
    if studentize == "wild" and blocks is None:
        raise ValueError("studentize='wild' needs blocks= (the wild cluster bootstrap)")
    wild = studentize == "wild" or (studentize == "auto" and blocks is not None)
    m = x.shape[1]
    dr = _replicate_means(
        x,
        b=b,
        seed=seed,
        test_id=test_id,
        bootstrap=bootstrap,
        block_size=block_size,
        blocks=blocks,
        indices=indices,
        wild=wild,
    )
    engine = _RangeStat(dr, wild) if method == "R" else _MaxStat(dr, wild)
    active = np.ones(m, dtype=bool)
    order: list[int] = []
    step_p: list[float] = []
    while active.sum() > 1:
        idx = np.flatnonzero(active)
        stat, worst_pos, sim = engine.step_test(idx)
        worst = int(idx[worst_pos])
        step_p.append(float(np.mean(sim >= stat)))
        order.append(worst)
        active[worst] = False
    last = int(np.flatnonzero(active)[0])
    order.append(last)
    step_p.append(1.0)
    p = np.empty(m)
    running = 0.0
    for i, ps in zip(order, step_p, strict=True):
        running = max(running, ps)
        p[i] = running
    included = tuple(int(i) for i in np.flatnonzero(p > alpha))
    return McsResult(
        included,
        p,
        tuple(order),
        tuple(step_p),
        dr.means,
        method,
        float(alpha),
        int(dr.reps.shape[0]),
        "wild" if wild else "fixed",
    )


__all__ = [
    "CHUNK_ELEMENTS",
    "EULER_GAMMA",
    "MIN_CLUSTERS",
    "WEBB_WEIGHTS",
    "McsResult",
    "PboResult",
    "SpaResult",
    "StepMResult",
    "bootstrap_indices",
    "deflated_sharpe_ratio",
    "dsr_from_returns",
    "expected_max_sharpe",
    "min_track_record_length",
    "model_confidence_set",
    "probabilistic_sharpe_ratio",
    "probability_of_backtest_overfitting",
    "psr_from_returns",
    "sharpe_moments",
    "sharpe_ratio",
    "sharpe_ratio_std",
    "spa_test",
    "stepm",
]
