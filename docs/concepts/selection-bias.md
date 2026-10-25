# Selection bias: is the best of many still good?

Whenever several models, parameters or strategies are tried and the best one is reported, the best one looks better
than it is: some of its lead is luck that will not repeat. `queuetruth.stats.selection` holds the standard corrections,
and the zoo leaderboard uses one of them (the model confidence set) to say which fill models are statistically tied
for best at each horizon.

| Question | Method | Function |
|---|---|---|
| Is this Sharpe ratio above a benchmark, given its skew, fat tails and track record length? | Probabilistic Sharpe ratio (PSR) | `probabilistic_sharpe_ratio`, `psr_from_returns`, `min_track_record_length` |
| Is the best of `N` trials better than what `N` skill-less trials would show? | Deflated Sharpe ratio (DSR) | `deflated_sharpe_ratio`, `dsr_from_returns`, `expected_max_sharpe` |
| How often does the in-sample winner end up below median out of sample? | Probability of backtest overfitting (PBO), CSCV | `probability_of_backtest_overfitting` |
| Does any model beat a benchmark? | Hansen's SPA test | `spa_test` |
| Which models beat the benchmark, with the familywise error controlled? | Romano-Wolf StepM | `stepm` |
| Which models are tied for best, with no benchmark? | Model confidence set (MCS) | `model_confidence_set` |

All bootstraps are deterministic: draws come from `bootstrap_rng(seed, test_id)`, or, for cluster (day or
30-minute) blocks, from `block_counts(n_blocks, b, bootstrap_seed(seed, test_id))`, the same draw matrix as every
other `queuetruth.stats` block bootstrap. The MCS on clusters uses a wild cluster bootstrap instead, whose weights
also come from `bootstrap_rng(seed, test_id)` (see "Few clusters" below). The functions live in
`queuetruth.stats.selection`; `queuetruth.stats` re-exports them with the result classes `McsResult`, `PboResult`,
`SpaResult` and `StepMResult` (the constants stay in the submodule).

## Sharpe ratios: PSR and DSR

`SR = mean / std` (ddof = 1) per period, never annualized. With `g3` the sample skewness and `g4` the sample
kurtosis (Pearson, 3 for normal returns; the biased moment estimators of `scipy.stats`), the estimator's standard
error is (Mertens 2002; Lo 2002 for normal returns, where it is `sqrt((1 + SR^2 / 2) / (n - 1))`):

```text
sigma(SR) = sqrt( (1 - g3 SR + (g4 - 1) / 4 SR^2) / (n - 1) )
PSR(SR*)  = Phi( (SR - SR*) / sigma(SR) )
MinTRL    = 1 + (1 - g3 SR + (g4 - 1) / 4 SR^2) (Phi^-1(1 - alpha) / (SR - SR*))^2
```

`PSR(SR*)` is the probability that the true Sharpe exceeds `SR*`; `MinTRL` is the track record length at which it
reaches `1 - alpha`. The deflated Sharpe ratio (Bailey and Lopez de Prado 2014) sets the benchmark to the expected
maximum Sharpe of `N` independent trials with no skill whose Sharpes have variance `V`:

```text
SR0 = sqrt(V) ( (1 - gamma) Phi^-1(1 - 1/N) + gamma Phi^-1(1 - 1/(N e)) ),   gamma = 0.5772...
DSR = PSR(SR0)
```

With one trial `SR0 = 0` and DSR is PSR(0). `dsr_from_returns(returns, trial_sharpes)` takes `N` and `V` from the
per-period Sharpes of every trial that was run, the selected one included. The honest input is the full count of
trials, including the ones that were thrown away; that count is what a trial ledger records. When the ledger counts
trials that left no return series, pass that count as `dsr_from_returns(returns, trial_sharpes, n_trials=N)`
(`N >= len(trial_sharpes)`); `V` still comes from the Sharpes that exist.

**Checks.** The paper's worked example (annualized SR 2.5 over 1,250 daily returns, 100 trials, annualized variance
of the trial Sharpes 1/2, skewness -3, kurtosis 10) gives `SR0 = 0.1132` per day and `DSR = 0.9004`, as published.
The standard error matches a Monte Carlo of skewed, fat-tailed returns (skewness 2, kurtosis 9) to within 5%, where
the normal formula is more than 10% off. The `SR0` approximation overstates the exact expected maximum of `N`
normals (by quadrature) by 2.33% at `N = 10`, 0.92% at 100, 0.42% at 1,000 and 0.23% at 10,000.

**Assumptions.** Returns are stationary and serially uncorrelated (the formulas ignore autocorrelation); trials are
independent (correlated trials count for fewer effective trials, which this module does not estimate); the moments
are estimated well, which needs a long track record when tails are fat.

## Probability of backtest overfitting (CSCV)

Combinatorially symmetric cross-validation (Bailey, Borwein, Lopez de Prado, Zhu 2017) takes a `T x N` matrix of
per-period performance (rows in time order, one column per trial) and cuts the rows into `S` contiguous blocks
(`numpy.array_split`, sizes differ by at most one row). For each of the `C(S, S/2)` ways to choose half the blocks
as in-sample:

```text
n*      = argmax_n  R_IS(n)                (first index on ties)
w       = rank of R_OOS(n*) among the N out-of-sample performances / (N + 1)   (ascending, average ranks)
logit   = ln( w / (1 - w) )
PBO     = #{logit <= 0} / C(S, S/2)
```

`R` is the Sharpe ratio (default), the mean, or any function of the row subset. The result also gives the
probability of an out-of-sample loss, `#{R_OOS(n*) < 0} / C(S, S/2)`, and the degradation line, the OLS fit of
`R_OOS(n*)` on `R_IS(n*)`.

**Checks.** The logits equal a direct implementation of the paper's algorithm to 1e-12; a reversal (the in-sample
winner is always the out-of-sample loser) gives PBO 1; one dominant trial gives logit `ln N` on every split and PBO
0; skill-less trials give a mean PBO of 0.5 (40 seeds, 10 trials, 8 splits, within 0.08).

**Assumptions.** Blocks are exchangeable (no regime makes one half systematically unlike the other) and long
enough for the performance statistic to mean something; `S = 16` (12,870 splits) is the paper's choice.

## Benchmark tests: SPA and StepM

Losses `L` (smaller is better) of a benchmark and `k` models over `n` rows give differences `d_k = L_benchmark -
L_k` (positive: model `k` is better). Hansen's test of superior predictive ability (2005):

```text
t_k   = sqrt(n) dbar_k / omega_k,           T = max(0, max_k t_k)
T*_b  = max(0, max_k sqrt(n) (dbar*_kb - mu_k) / omega_k)
p     = #{T*_b >= T} / B
mu_k  = max(dbar_k, 0)                                          lower
        dbar_k 1{dbar_k >= -sqrt(omega_k^2 / n 2 ln ln n)}       consistent (Hansen's recommendation)
        dbar_k                                                  upper (White's Reality Check)
```

`omega_k^2` estimates `Var(sqrt(n) dbar_k)`: the stationary-bootstrap kernel of Politis and Romano with
`q = 1 / block_size` for row bootstraps, or `n mean_b (dbar*_kb - dbar_k)^2` for cluster bootstraps.
`studentize=False` sets `omega_k = 1` in the statistic. `p_lower <= p_consistent <= p_upper` always holds. The
count uses `>=`, so with no model ahead of the benchmark (`T = 0`) the p-value is 1.

Romano and Wolf's StepM (2005) names the models that beat the benchmark: at each step the critical value is the
`1 - alpha` quantile of `max_k sqrt(n) (dbar*_kb - mu_k) / omega_k` over the models not yet rejected, and every such
model with `t_k` above it is rejected; the steps repeat until one rejects nothing. `recenter="upper"` is Romano and
Wolf; `"consistent"` is Hansen's recentring (the choice of `arch`).

## Model confidence set

The MCS (Hansen, Lunde, Nason 2011) needs no benchmark. Starting from all `m` models it tests "equal expected loss"
on the models still in the set and, if rejected, drops the worst one; the models left when a test no longer rejects
form the `1 - alpha` model confidence set, which asymptotically (many rows, or many clusters) contains the best
model with probability at least `1 - alpha`. With few clusters the guarantee is only approximate, and the measured
coverage below is what to rely on. Every bootstrap replicate resamples the same rows for all models, so their
dependence is kept:

```text
T_R:   t_ij = (Lbar_i - Lbar_j) / se_ij,   T_R = max_{i,j in M} t_ij,     drop argmax_i max_j t_ij
T_max: d_i  = Lbar_i - mean_{j in M} Lbar_j,   t_i = d_i / se_i,   T_max = max_{i in M} t_i,   drop argmax_i t_i
se^2   = mean_b (centred replicate)^2      (T_max: recomputed on the current set)
p_step = #{T*_b >= T} / B
MCS p-value of the model dropped at step j = max over steps s <= j of p_step(s); the last model has p-value 1
```

A model is in the `1 - alpha` MCS when its p-value exceeds `alpha`. A p-value has resolution `1 / B`: 0 means
below `1 / B`, not certainty. A difference with zero standard error reads 0 when it is 0 (identical forecasts are
never separated) and infinite otherwise (a model worse on every row is dropped with p-value 0); ties drop the lowest
column first.

**Checks against `arch`.** `arch` 8.0.0 (`arch.bootstrap.MCS`, `SPA`, `StepM`), installed in a throwaway virtual
environment and fed the same bootstrap indices as this module (its bootstrap object replaced by one yielding
`bootstrap_indices(...)`), gives identical results on autocorrelated losses (250 rows, 6 models, 500 stationary
replicates, block size 7): MCS p-values for `T_R` and `T_max` equal to the last digit (largest difference 0.0),
the same 90% sets, identical non-studentized SPA p-values (0.074, 0.074, 0.162), kernel variances equal to a
relative 1.3e-15, and the same StepM superior sets at 5% and 10%. A smaller frozen case of the same comparison
(120 rows, 5 models, 200 replicates) is in the test suite. Two deliberate differences: `arch`'s SPA does not
studentize its statistic (here `studentize=True` is the default, as in Hansen 2005), and `arch` counts `T* > T`
strictly, which with identical models (`T = 0`) rejects equality; with continuous losses the two counts agree.

**Assumptions.** Loss differences are stationary with finite variance; the bootstrap must respect the dependence
in the data, which is why the leaderboard resamples whole session days (or 30-minute buckets).

### Few clusters: the wild cluster bootstrap-t

The studentization above (`studentize="fixed"`, as in Hansen, Lunde and Nason and in `arch`) estimates each `se_ij`
once from the bootstrap and then treats it as known. With a cluster bootstrap over a handful of clusters that
estimate is itself very noisy, and the replicate maxima come out too small, so the set is too small. With `blocks=`
the default `studentize="auto"` therefore uses the wild cluster bootstrap-t (Cameron, Gelbach and Miller 2008, with
Webb's six-point weights, Webb 2023). With `S_g` the per-cluster loss sums, `n_g` the cluster sizes, `N = sum n_g`
and `e_g = S_g - n_g Lbar` the per-cluster residuals:

```text
se_ij^2       = sum_g (e_gi - e_gj)^2 / N^2                          (cluster-robust)
w_bg          one weight per cluster and replicate from {-sqrt(3/2), -1, -sqrt(1/2), sqrt(1/2), 1, sqrt(3/2)},
              shared by every model
Lbar*_b       = Lbar + sum_g w_bg e_g / N
e*_bg         = w_bg e_g - n_g (Lbar*_b - Lbar)
se*_bij^2     = sum_g (e*_bgi - e*_bgj)^2 / N^2                      (each replicate studentized by its own se)
t*_bij        = ((Lbar*_bi - Lbar*_bj) - (Lbar_i - Lbar_j)) / se*_bij
```

`T_max` is the same with `e_gi - mean_{j in M} e_gj`. The cluster bootstrap needs at least two clusters
(`ValueError` otherwise): one cluster has no between-cluster variance, and every replicate would equal the sample.

Measured coverage: the share of 1,000 simulations in which the 90% `T_R` MCS keeps the whole set of models with
equal expected loss (iid standard normal losses, `B = 1000`; the target is 0.90, Monte Carlo standard error about
0.01):

| Setup | Wild cluster bootstrap-t | Fixed bootstrap variance |
|---|---|---|
| 16 models, the real day's 13 thirty-minute blocks (148 to 271 rows each) | 0.880 | 0.423 |
| 16 models, 5 blocks of 600 rows | 0.834 | 0.005 |
| 16 models, 3 blocks of 1,000 rows | 0.205 | 0.000 |
| 4 models, the real day's 13 blocks | 0.898 | 0.769 |
| 4 models, 5 blocks of 600 rows | 0.893 | 0.534 |

On sparse losses shaped like first-fill errors (a uniform loss on 5% or 1% of the rows, else 0; 16 models, the real
day's 13 blocks, 400 simulations) the wild bootstrap kept the whole set 0.907 and 0.887 of the time, the fixed
variance 0.440 and 0.468. So the wild bootstrap is close to nominal at 13 clusters, somewhat liberal at 5, and fails
at 3; the leaderboard requires 5. `test_coverage_at_the_leaderboard_setup` repeats the first row with 100
simulations. SPA and StepM still use the fixed bootstrap variance; with few clusters treat their p-values as
optimistic.

Memory is `O(B m^2)` for the cached pairwise replicate statistics up to `2 * CHUNK_ELEMENTS` (about 8.4 million)
elements; larger problems are computed in chunks of at most `CHUNK_ELEMENTS` elements at each elimination step.
The tests draw row bootstrap indices in chunks of the same size, so they never hold the whole `B x n` index matrix
(`bootstrap_indices` itself still returns it).

## The MCS on the zoo leaderboard

`queuetruth zoo` adds five fields to each leaderboard entry, keyed by horizon (`qt.zoo_summary`, additive, null where
undefined):

| Field | Meaning |
|---|---|
| `first_fill_error` | mean over shadows of `|min(T_model, h) - min(T_exact, h)| / h` |
| `mcs_member`, `mcs_p` | in the 90% MCS for first-fill error, and its MCS p-value |
| `mcs_brier_member`, `mcs_brier_p` | the same for the Brier loss `(p_fill - truth_filled)^2` |

`T = ts_first_fill - ts_live` (null means never; the exact term is `h` when the exact arm did not fill by `h`), so
the first-fill error is a share of the horizon in [0, 1]: 0 when the model's first fill is exactly right or both sides
do not fill by `h`. The particle family reports its median particle first-fill time, which is the forecast that
minimizes the expected absolute error. The touch families with `p < 1` (`nt_prob_touch[p<1]`,
`nt_queue_l2_prob_touch`) have an analytic `p_fill` but one random fill path; their first-fill error scores that
sampled path, which is the fill time a backtest using the model would see, so their sampling noise counts as error
(their Brier loss uses the analytic `p_fill`). When every model has a row for every shadow, the mean Brier loss is the
entry's `brier[h]`. A shadow that lacks a row for some model is left out of that horizon's set but stays in
`brier[h]`, and a summary warning gives the count.

The rows are exactly the leaderboard rows: never-cancel shadows of the common population (shadows with a faithful
hbt #312 abort are out for every model) with an exact arm whose label exists at `h`. The MCS uses `T_R`, `alpha =
0.10`, the wild cluster bootstrap-t above with the run's `bootstrap_b` and `seed` (`test_id = "zoo_mcs"`), and the
blocks of every leaderboard CI: session dates when there are at least 5, else 30-minute buckets of `ts_live`. A
horizon with fewer than 5 blocks (`MCS_MIN_BLOCKS`; for example a run inside one half-hour) gets null MCS fields
and a warning such as `mcs: at 60 s, 1 bootstrap block(s) (30min) < 5: no model confidence set (MCS fields null)`.
The spec, and so the run_id, does not change. `hbt_l3_fifo` reads the order-level records and, corrected, equals
the exact engine by construction, so it is left out of the set (its fields are null); with it the set would be that
model alone at every horizon.

The terminal output adds a second table after the fill-rate-bias table, with each model's first-fill error and `#`
for a member of the set, and one plain-words line at the sort horizon:

```text
not ranked on first-fill error: hbt_l3_fifo[mode=corrected] (order-level reference)
first-fill error in % of the horizon (# = tied with the best, 90% MCS):
...
  At 60 s, 8 of 16 models are tied for the lowest first-fill error (90% MCS): databento_mc[...] (1.0%), ...
```

### Real data: Nasdaq, 2019-12-30, 3,000 sampled shadows

The day-one corrected zoo (`.realdata/zoo/d1-sampled-corrected`, run `5bd208b2957122b3`, 16 L2 models, 3,000
never-cancel shadows, 13 thirty-minute blocks, `bootstrap_b = 1000`, seed 0; the MCS for all five horizons and both
losses takes about 2 s):

| Horizon | Lowest first-fill error | Tied for best (first-fill error, 90% MCS) | Brier MCS |
|---|---|---|---|
| 0.1 s | 0.07%, a 13-way exact tie | 14 of 16: all but `front_of_queue` and `nt_prob_touch[p=1]` | the same 14 |
| 1 s | `databento_mc[k=-0.8]` 0.225%, three models at 0.226% | 13: all but `trade_through_only`, `front_of_queue`, `nt_prob_touch` | the same 13 |
| 10 s | `databento_mc[k=-0.8]` 0.82% | 4: `databento_mc[k=-0.8]`, `databento_mc[k=-0.95]`, `nt_queue_l2`, `hbt_risk_adverse` | 5 |
| 60 s | `databento_mc[k=-0.8]` 1.00% | 8: the 10 s four plus `hbt_power3[n=3]`, `hbt_log2`, `hbt_power3[n=2]`, `hbt_power[n=3]` | 13 |
| 300 s | `databento_mc[k=-0.8]` 0.54% | the same 8 | 11 |

So "`databento_mc[k=-0.8]` ranks first" is too strong at every horizon: it has the lowest mean error from 1 s on, but
three to twelve other models are statistically indistinguishable from it on 3,000 orders of one day (at 10 s the
runner-ups have p = 0.24, 0.24 and 0.15). At 0.1 s almost nothing fills: 13 models have exactly the same error, so
no single model is lowest. The touch-style models (`front_of_queue`, `nt_prob_touch[p=1]`, and from 1 s on
`trade_through_only`) are outside the first-fill-error set at every horizon.

On the same day in faithful mode, `nt_queue_l2` alone is in the first-fill-error set from 1 s on (the next model's
p-value is at most 0.025). At 0.1 s it has error 0 and the 13 runner-ups have p = 0.101, just above `alpha`, so
14 models are in the set there. On the 283 mirrors of the day's real orders (corrected, 16 thirty-minute blocks), 13,
10, 15, 16 and 16 of the 16 models are tied at the five horizons: a few hundred orders cannot separate the models.
These figures are asserted by `test_real_day_one_leaderboard_mcs` (marked `realdata`, run with `QT_REALDATA_DIR`
set). They are 90% sets with the coverage measured above (0.88 for 16 equal models at 13 blocks), not exact
rankings.

## Example

<!-- check-docs: run -->
```python
import numpy as np

from queuetruth.stats.selection import deflated_sharpe_ratio, model_confidence_set

# Bailey and Lopez de Prado (2014): DSR 0.9004
dsr = deflated_sharpe_ratio(2.5 / 250**0.5, 1250, n_trials=100, var_sr=0.5 / 250, skew=-3.0, kurtosis=10.0)
print(f"DSR = {dsr:.4f}")

# three models, the third clearly worse; day blocks of 50 rows
rng = np.random.default_rng(0)
losses = rng.standard_normal((500, 3)) + np.array([0.0, 0.02, 0.5])
mcs = model_confidence_set(losses, alpha=0.10, blocks=np.repeat(np.arange(10), 50), b=500, seed=0)
print("in the 90% MCS:", mcs.included, "p-values:", np.round(mcs.p_values, 3))
assert 2 not in mcs.included and round(dsr, 4) == 0.9004
```

## References

- Bailey, D. H. and Lopez de Prado, M. (2012). The Sharpe ratio efficient frontier. Journal of Risk 15(2).
- Bailey, D. H. and Lopez de Prado, M. (2014). The deflated Sharpe ratio: correcting for selection bias, backtest
  overfitting and non-normality. Journal of Portfolio Management 40(5).
- Bailey, D. H., Borwein, J., Lopez de Prado, M. and Zhu, Q. J. (2017). The probability of backtest overfitting.
  Journal of Computational Finance 20(4).
- Hansen, P. R. (2005). A test for superior predictive ability. Journal of Business and Economic Statistics 23(4).
- Hansen, P. R., Lunde, A. and Nason, J. M. (2011). The model confidence set. Econometrica 79(2).
- Lo, A. W. (2002). The statistics of Sharpe ratios. Financial Analysts Journal 58(4).
- Politis, D. N. and Romano, J. P. (1994). The stationary bootstrap. JASA 89(428).
- Romano, J. P. and Wolf, M. (2005). Stepwise multiple testing as formalized data snooping. Econometrica 73(4).
- White, H. (2000). A reality check for data snooping. Econometrica 68(5).
- `arch` (Kevin Sheppard), `arch.bootstrap` multiple-comparison procedures, version 8.0.0.
