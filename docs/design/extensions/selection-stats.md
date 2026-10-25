# Extension: selection-aware statistics and the leaderboard MCS (`selection-stats`)

Proposed spec text for the integrator. Concept page: `docs/concepts/selection-bias.md`. Code:
`python/queuetruth/stats/selection.py`, `python/queuetruth/eval/leaderboard.py`; tests:
`python/tests/stats/test_selection.py`, `python/tests/eval/test_leaderboard_mcs.py`.

## 3.x `qt.zoo_summary` (additive leaderboard fields, schema_version unchanged)

Append to the `qt.zoo_summary` row of the documents table:

```text
selection-stats (additive, default {}): leaderboard items first_fill_error {h: v}, mcs_member {h: bool|null},
mcs_p {h: v}, mcs_brier_member {h: bool|null}, mcs_brier_p {h: v} (5.6 "Model confidence set")
```

## 4.x Module-level APIs (append to the `# queuetruth.stats` block)

```python
# queuetruth.stats.selection (numpy/scipy only; semantics 5.7.6)
sharpe_ratio(returns) -> float ; sharpe_moments(returns) -> (sr, n, skew, kurtosis)
sharpe_ratio_std(sr, n, *, skew=0, kurtosis=3) -> float
probabilistic_sharpe_ratio(sr, n, *, skew=0, kurtosis=3, sr_benchmark=0) -> float ; psr_from_returns(returns, sr_benchmark=0)
expected_max_sharpe(n_trials, var_sr, mean_sr=0) -> float
deflated_sharpe_ratio(sr, n, *, n_trials, var_sr, skew=0, kurtosis=3, mean_sr=0) -> float
dsr_from_returns(returns, trial_sharpes, *, n_trials=None) -> float   # n_trials >= len(trial_sharpes)
min_track_record_length(sr, *, sr_benchmark=0, skew=0, kurtosis=3, alpha=0.05) -> float
probability_of_backtest_overfitting(performance, *, n_splits=16, metric="sharpe") -> PboResult
bootstrap_indices(n, b, *, block_size=None, kind="stationary", seed=0, test_id="selection") -> np.ndarray   # b x n
spa_test(benchmark, models, *, b=1000, seed=0, test_id="spa", bootstrap="stationary", block_size=None, blocks=None,
         indices=None, studentize=True) -> SpaResult
stepm(benchmark, models, *, alpha=0.05, ..., studentize=True, recenter="upper") -> StepMResult
model_confidence_set(losses, *, alpha=0.10, method="R", b=1000, seed=0, test_id="mcs", bootstrap="stationary",
                     block_size=None, blocks=None, indices=None, studentize="auto") -> McsResult
# studentize: "auto" = "wild" with blocks=, else "fixed"; McsResult.studentize records the choice
CHUNK_ELEMENTS = 1 << 22 ; MIN_CLUSTERS = 2 ; WEBB_WEIGHTS (6,)
# queuetruth.eval.leaderboard
horizon_losses(predictions, planned_life, *, h_max_ns, exclude_families=()) -> list[HorizonLosses]
leaderboard_mcs(predictions, planned_life, *, h_max_ns, bootstrap_b=1000, seed=0, alpha=0.10, method="R",
                exclude_families=ORDER_LEVEL_FAMILIES) -> (dict[model_id, dict[field, dict[h, value]]], warnings)
annotate_mcs(entries, predictions, planned_life, *, h_max_ns, bootstrap_b=1000, seed=0, ...) -> (list, warnings)
mcs_mark(entry, horizon_ns) -> str ; has_mcs(entries) -> bool
mcs_table(entries, horizons) -> (legend lines, rows) ; mcs_sentence(entries, horizon_ns) -> str | None
MCS_ALPHA = 0.10 ; MCS_METHOD = "R" ; MCS_STUDENTIZE = "wild" ; MCS_MIN_BLOCKS = 5 ; MCS_WARNING_PREFIX = "mcs: "
```

## 5.6 Zoo evaluation: model confidence set (new paragraph after "Leaderboard")

Model confidence set (Hansen, Lunde, Nason 2011). For each horizon `h <= h_max`, on exactly the leaderboard rows
(never-cancel shadows of the common population with an exact arm and an eligible label at `h`), a balanced
`shadows x models` loss matrix over the L2-input models (`ORDER_LEVEL_FAMILIES = ("hbt_l3_fifo",)` is left out:
corrected, it equals the exact engine by construction, O-ZOO-4; shadows missing a row of one of the horizon's models
are dropped) for two losses:

```text
first-fill error  |min(T_model, h) - min(T_exact, h)| / h,  T = ts_first_fill - ts_live (null = inf;
                  T_exact = inf unless truth_filled), in [0, 1]; databento_mc reports the median particle time
Brier             (p_fill - truth_filled)^2   (its mean is the entry's brier[h] when no shadow is dropped)
```

The touch families with `p < 1` (`nt_prob_touch[p<1]`, `nt_queue_l2_prob_touch`) are scored on their one sampled
fill path (their `p_fill` is analytic). When `n_dropped > 0` at a horizon the summary gets the warning
`mcs: at <h>, <n> shadow(s) lack a row for some model and are left out of the model confidence set (its Brier means
then differ from brier)`.

The `T_R` MCS at `alpha = 0.10` (5.7.6) with the wild cluster bootstrap-t over the blocks of the leaderboard CIs
(session dates with at least 5 dates, else 30-minute buckets of `ts_live`, labelled over the whole population;
Webb weights from `bootstrap_rng(seed, "zoo_mcs")`, `bootstrap_b` replicates) gives `mcs_p[h]` and
`mcs_member[h] = mcs_p[h] > 0.10` (first-fill error), `mcs_brier_p[h]` and `mcs_brier_member[h]` (Brier), and
`first_fill_error[h]` (the mean loss). p-values have resolution `1 / bootstrap_b` (0 means below it). Fields are null for a model without a row at
`h`, for the excluded families, and at a horizon with fewer than `MCS_MIN_BLOCKS = 5` bootstrap blocks, which also
adds the warning `mcs: at <h>, <n> bootstrap block(s) (<day|30min>) < 5: no model confidence set (MCS fields null)`
(one block has no between-block variance; measured whole-set coverage of the 90% set of 16 equal models is 0.88 at
13 blocks, 0.83 at 5 and 0.21 at 3). Only `api.run_zoo` annotates the leaderboard (`_evaluate(..., mcs=True)`; the
calibrate held-out evaluation does not run the MCS); `ZooSpec` is unchanged, so the run_id is unchanged. CLI zoo
output: when any entry carries MCS fields, a second table after the fill-rate-bias table gives each ranked model's
first-fill error in % of the horizon with `#` after a member (legend `first-fill error in % of the horizon (# = tied
with the best, 90% MCS):`, preceded by `not ranked on first-fill error: ...` naming the order-level reference), and
the plain-words section ends with one `mcs_sentence` at the sort horizon; older summaries render as before.

## 5.7.6 Selection-aware statistics (new subsection)

Semantics as in the `queuetruth.stats.selection` module docstring: PSR and DSR (Bailey and Lopez de Prado 2012,
2014; Mertens standard error with biased sample skewness and Pearson kurtosis, `SR = mean / std (ddof = 1)` per
period); MinTRL; PBO by CSCV (contiguous `numpy.array_split` blocks, first index on in-sample ties, average ranks out
of sample, `PBO = #{logit <= 0} / C(S, S/2)`); SPA (Hansen 2005; studentized by default with the Politis-Romano
stationary-bootstrap kernel variance for row bootstraps or the bootstrap variance for cluster bootstraps; lower /
consistent / upper recentring; `p = #{T* >= T} / B`); StepM (Romano and Wolf 2005, quantiles by `numpy.quantile`
linear; `recenter="consistent"` reproduces `arch.bootstrap.StepM`); MCS (`T_R` or `T_max`, `p_step = #{T* >= T} /
B`, MCS p-values as running maxima along the elimination order, zero standard errors read 0 for a zero difference
and `+-inf` otherwise, ties eliminate the lowest index first; with `blocks=` the default studentization is the wild
cluster bootstrap-t with Webb's six-point weights and per-replicate cluster-robust standard errors, `"fixed"` the
Hansen-Lunde-Nason bootstrap variance). Row bootstraps (`stationary`, `circular`, `moving`) draw from
`bootstrap_rng(seed, test_id)` in chunks of at most `CHUNK_ELEMENTS` indices; `blocks=` uses `block_counts` as 5.7.5
(the wild MCS: `bootstrap_rng(seed, test_id)` weights) and needs at least `MIN_CLUSTERS = 2` clusters (`ValueError`);
`indices=` takes explicit draws. PBO with a callable metric raises `ValueError` on a NaN performance.

## 6. Oracles (additions)

- **O-STAT-SEL-1** DSR reproduces the worked example of Bailey and Lopez de Prado (2014): `SR0 = 0.1132`,
  `DSR = 0.9004`.
- **O-STAT-SEL-2** On identical bootstrap indices, `model_confidence_set` (`T_R`, `T_max`), `spa_test`
  (`studentize=False`) and `stepm` (`studentize=False, recenter="consistent"`) equal `arch` 8.0.0 `MCS`, `SPA` and
  `StepM` (frozen case in the tests; 250 x 6 comparison recorded in the concept page).
- **O-STAT-SEL-3** PBO logits equal a direct implementation of the published algorithm; reversal gives PBO 1,
  dominance PBO 0, skill-less trials a mean PBO of 0.5.
- **O-ZOO-MCS-1** The leaderboard MCS uses the leaderboard rows (mean Brier loss equals `brier[h]` on a balanced
  panel), is deterministic in `(seed, bootstrap_b)`, leaves `hbt_l3_fifo` out, and keeps identical forecasts
  together.
- **O-ZOO-MCS-2** A horizon with fewer than 5 bootstrap blocks has null MCS fields and an `mcs: ` warning; the
  stats functions raise on a single cluster.
- **O-STAT-SEL-4** The wild cluster bootstrap-t MCS equals a direct loop implementation and keeps the whole set of
  16 equal models on the real day's 13 blocks at least 0.82 of 100 simulations (0.880 of 1,000 measured).
