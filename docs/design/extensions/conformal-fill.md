# Extension: conformal fill probabilities (`qt.conformal_report`, `qt.conformal_sets`)

Proposed SPEC text (new 5.5.9, after the hazard model card) and the document and table schemas. Implemented in
`python/queuetruth/model/conformal.py`; the CLI hook is the `fit-hazard --conformal-alpha` flag
(`crates/cli/src/grammar.rs`, `_cli.conformal_alpha`; dispatched in `python/queuetruth/cli.py`). Tests:
`python/tests/model/test_conformal.py`. Concept page: `docs/concepts/conformal-fill-probability.md`.

## 5.5.9 Conformal fill probabilities

**Inputs.** Per (order, landmark) row and grid horizon `tau_j`: a fill probability `p` and the label `Y_j = 1{fill
within tau_j}` or unknown. From `qt.hazard_predictions` (`prepare_hazard`): `p = cif_fill_raw` (default) or
`cif_fill`, `Y = y_fill` (null = censored before `tau_j` = unknown); the `qt.features` table it was predicted from,
joined on `(order_ref, landmark_ns)`, gives the strata, `session_date` and `ts_landmark`, and its `event_ns` /
`cause` must reproduce `y_fill` (else `QT-INPUT-002`). Rows are keyed by the `(order_ref, landmark_ns)` pair. With
`labels=False` (orders without an outcome yet) `y_fill`, `split` and the outcome columns are not read and every
label is unknown. From `qt.zoo_predictions` of one `model_id` (`prepare_zoo`), under the rules of `evaluate_zoo`
(SPEC 5.6, A2, E38 OC-7): the planned-life table (`qt.zoo_shadows`: `shadow_id`, `ts_cancel_planned`,
`ts_admin_end`) is required (`QT-INPUT-001`) and must cover every shadow (`QT-INPUT-002`); the predictions are first
reduced to `common_population` (every shadow an hbt pair aborted on at #312 leaves every model); landmark 0, `p =
p_fill`, `Y = truth_filled`, known at `h` iff `truth_filled` is not null and `ts_admin_end` is null or `>= ts_live +
h` (the `censored` column is not read); population `never_cancel` (default: no planned cancel before `ts_live` + the
largest horizon) or `cancel_bearing`, the other left out with a note; dates and times from `session_date` /
`ts_live`; features rows are optional (missing ones get stratum `na`, counted in a note). Every row must have
exactly one value per horizon (`QT-INPUT-002`). Preparation notes become report warnings; without a session date
(`dates_known` false) the martingale groups by role only, with a warning.

**Strata.** `dist_bucket` from `rint(dist_ticks)`: `< 0` `better`, `0` `touch`, `1` `behind1`, `>= 2` `behind2+`,
null `na`; `side` (`bid` / `ask` from `side_sign`); `instrument`; `predicted` (zoo only, per horizon: `p` in bins
`[0, 0.2), ..., [0.8, 1.0]`). The Mondrian category of a row at horizon `j` is `landmark=<l>|<name>=<value>|...` and,
with `label_conditional`, the label.

**Validity refusals** (`QT-CONFIG-001`): calibration or test rows from the hazard `train` split; `score =
cif_fill` with calibration or test rows from the `calib` split; overlapping calibration and test rows; an empty
selection; an invalid spec field.

**Prediction sets.** Score `s(x, y) = 1 - p` for `y = 1`, `p` for `y = 0`. Per category with sorted calibration
scores (size `n`): conservative `p_y = (#{s_i >= s} + 1) / (n + 1)`, smoothed `p_y = (#{s_i > s} + theta (#{s_i =
s} + 1)) / (n + 1)` with one `theta` in (0, 1] per test row; set `{y : p_y > alpha}`. A category with no calibration
rows gives `p = 1` (conservative) or `theta` (smoothed). `min_informative_n(alpha) = ceil(1 / alpha - 1)`: below it
conservative sets hold both labels and smoothed sets are randomized.

**Venn-Abers.** Per category without the label: `p0` / `p1` = the isotonic regression (pooled ties, PAVA) of the
calibration labels on `p` with the test row added with label 0 / 1, evaluated at the test row. Computed from
persistent prefix and suffix PAVA stacks; a category without calibration rows gives `(0, 1)`.

**Fill-time bounds.** On rows observed at every horizon, `K` = first `j` with `Y_j = 1`, else `J + 1`; `F` the row's
`p` across the grid (must be nondecreasing within `1e-9`, else `QT-INPUT-002`; a running maximum is applied).
Categories exclude `predicted` and the label. Lower: scores `F(tau_K)` (`+inf` if `K = J + 1`), `k = floor(alpha (n +
1))`; `k = 0` gives `lo = 0`; else `t` = `k`-th smallest, `lo = (min j with F(tau_j) >= t) - 1`, `J` if none. Upper:
scores `F(tau_{K-1})` (`0` if `K = 1`, `+inf` if `K = J + 1`), `k = ceil((1 - alpha)(n + 1))`; `k > n` or an infinite
`k`-th smallest gives no claim (`hi = -1`); else `hi = min(J, 1 + #{j : F(tau_j) <= t})`. Two-sided: `alpha / 2`
each.

**Martingale.** Per horizon and landmark, the rows with a known label in groups `calib:<date>` (ascending), then
`test:<date>` (ascending), or `calib`, `test` without dates; each group shuffled (`shuffle_within_day`, default) or
in `ts_landmark` order; the scores are the true-label scores; online smoothed p-values `p_n = (#{i < n : s_i > s_n} + theta_n (#{i < n : s_i =
s_n} + 1)) / n`; Simple Jumper (`jumper_rate`, betting functions `1 + eps (p - 1/2)`, `eps in {-1, 0, 1}`, start
`1/3` each) or the equal-weight mixture over `eps_k = (k - 1/2) / 100` of `prod eps p^(eps - 1)`. Alarm at the first
`n` with `M_n >= alarm_threshold` (default 100), a per-cell level. Family-wise (Bonferroni): `drift_familywise_alarm`
= some cell's maximum `>= alarm_threshold x m` over the `m` cells. The mixture is streamed in chunks of 16,384
p-values (memory `O(100 x chunk)`).

**Determinism.** `theta` for sets: `default_rng(stable_seed(seed, [b"conformal", "sets:<h>:<l>"]))`; shuffle:
`"order:<h>:<l>"`; martingale `theta`: `"martingale:<h>:<l>"`; `random_order_halves`: `"halves"`. `run_id` = SPEC 7.3
`run_id` of `{conformal: spec, source, model_id, score, inputs: sha256(grid, order_ref, landmark_ns, date, time_ns,
p, y), dates_known, strata: {name: sha256}, calib: sha256(mask), test: sha256(mask)}`. The `sets:<h>:<l>` stream
draws one `theta` per row at landmark `l` of the predicted selection, in row order, so `ConformalCalibrator.predict`
on the test rows reproduces `evaluate`.

## Spec (`ConformalSpec`)

| Field | Default | Meaning |
|---|---|---|
| `alpha` | 0.1 | miscoverage, in (0, 1) |
| `strata` | `["dist_bucket"]` | Mondrian strata besides the landmark |
| `label_conditional` | false | categories per label |
| `smoothed` | false | smoothed p-values (exact coverage) |
| `seed` | 0 | u64 |
| `martingale` | `jumper` | `jumper` or `mixture` |
| `jumper_rate` | 0.01 | in (0, 1] |
| `alarm_threshold` | 100 | > 1 |
| `shuffle_within_day` | true | shuffle each date before the martingale |

## `conformal.json` (`qt.conformal_report`, schema_version 1)

`run_id`, `spec` (echo), `source` (`hazard` / `zoo`), `model_id`, `score`, `alpha`, `grid_ns`, `n_calib_rows`,
`n_test_rows`, `calib_dates`, `test_dates` (empty without dates), `guarantee` (text), `cells`, `fill_time`,
`drift`, `drift_familywise_threshold` (null without cells), `drift_familywise_alarm`, `warnings`.

- `cells[]` (`CoverageCell`): `horizon_ns`, `landmark_ns` (null = pooled over landmarks), `stratum` (`all` =
  pooled; without strata only `all`, one cell per horizon x landmark), `n_calib`, `n_test`, `n_test_fill`, `coverage`, `coverage_ci_lo` / `coverage_ci_hi` (Clopper-Pearson 95%, rows treated
  as independent), `frac_empty`, `frac_singleton`, `frac_both`, `fill_rate_calib`, `fill_rate_test`, `mean_p_test`,
  `va_p0_mean`, `va_p1_mean`, `va_true_label_mean`, `coverage_fill`, `coverage_nofill`, `informative`.
- `fill_time[]` (`FillTimeCell`): `landmark_ns`, `stratum`, `n_calib`, `n_test`, `lower_coverage`,
  `upper_coverage`, `interval_coverage` (at `alpha`, the interval at `alpha / 2` per side), `frac_lower_claim`,
  `frac_upper_claim`, `median_lower_ns`, `median_upper_ns`.
- `drift[]` (`DriftCell`): `horizon_ns`, `landmark_ns`, `method`, `n`, `log10_final`, `log10_max`, `alarm`,
  `alarm_group`, `groups[]` (`group`, `role` (`calib` / `test`), `date` (null without dates), `n`, `log10_end`,
  `log10_max`, `mean_pvalue`, `ks_uniform`).

The Pydantic model is `ConformalReport` (a `QtDoc`) in `queuetruth.schemas.conformal`, importable from
`queuetruth.model.conformal` as before. It is registered in `queuetruth.schemas.models.DOC_MODELS` and `schemas/json`
(a Python-only document, without a Rust mirror).

## `conformal_sets.parquet` (`qt.conformal_sets`, schema_version 1)

`evaluate` writes one row per test row and horizon with a known label; `ConformalCalibrator.predict` one row per
selected row and horizon, with `y_fill` null when the label is unknown. Ordered by row then horizon:

| Column | Type | Meaning |
|---|---|---|
| `order_ref` | utf8 | order |
| `landmark_ns` | int64 | landmark |
| `horizon_ns` | int64 | horizon |
| `stratum` | dictionary<int16, utf8> | stratum label (without the landmark) |
| `p` | float64 | input probability |
| `y_fill` | bool, null | label (null = unknown) |
| `pvalue_nofill`, `pvalue_fill` | float64 | conformal p-values of the two labels |
| `set_nofill`, `set_fill` | bool | membership in the prediction set |
| `va_p0`, `va_p1` | float64 | Venn-Abers interval |
| `no_fill_within_ns` | int64, null | one-sided lower fill-time bound at `alpha` (null = no claim) |
| `fill_within_ns` | int64, null | one-sided upper fill-time bound at `alpha` (null = no claim) |
| `interval_no_fill_within_ns` | int64, null | lower end of the two-sided interval (`alpha / 2` per side) |
| `interval_fill_within_ns` | int64, null | upper end of the two-sided interval (`alpha / 2` per side) |

The Arrow schema is `queuetruth.model.conformal.sets_schema()`; it is not yet in `queuetruth.schemas.arrow.TABLES`.

## `conformal_calibrator.parquet` (`qt.conformal_calibrator`, schema_version 1)

`ConformalCalibrator.save` / `load`. One row per stored calibration value, sorted by part, horizon, landmark,
category, label, value; the settings (`spec`, `grid_ns`, `source`, `score`, `model_id`, `n_calib_rows`) are JSON in
the schema metadata key `qt.conformal_calibrator`. A loaded calibrator predicts exactly as the fitted one.

| Column | Type | Meaning |
|---|---|---|
| `part` | utf8 | `set` (sorted conformal scores), `va` (Venn-Abers unique probabilities), `time_lo` / `time_hi` (fill-time scores, `+inf` without a fill) |
| `horizon_ns` | int64, null | horizon (null for `time_*`) |
| `landmark_ns` | int64 | landmark |
| `category` | utf8 | stratum label within the landmark |
| `label` | int8 | `set` with `label_conditional`: the label; else -1 |
| `value` | float64 | score, or the unique probability `u` for `va` |
| `weight`, `label_sum` | float64, null | `va` only: count and label sum at `u` |

`predict` refuses outcomes on another grid (`QT-INPUT-002`) or with another score column (`QT-CONFIG-001`).
`load` raises `QT-INPUT-001` for a missing file and `QT-INPUT-002` for another table. Not yet in
`queuetruth.schemas.arrow.TABLES`.

## CLI

`fit-hazard ... --conformal-alpha A`: before the fit, `A`, `--out` (`QT-CONFIG-001`) and the features table
(`QT-INPUT-001` when missing; `QT-INPUT-002` without `order_ref`, `landmark_ns`, `dist_ticks`, `session_date`,
`ts_landmark`, `event_ns`, `cause`) are checked. After it, `cif_fill_raw` is calibrated on the calib split and
evaluated on the test split with the default spec (`seed` = `--seed`); `conformal.json` (written to a temporary
file, then renamed), `conformal_sets.parquet` and `conformal_calibrator.parquet` go to `--out`, and their `Files`
entries are appended to the printed card's `files`. Stderr lines: pooled coverage at 1 s and 10 s (the first and
last horizons when those are off the grid); pooled coverage, and coverage among filled orders, whose 95%
Clopper-Pearson upper limit is below `1 - alpha`; martingale alarms with the per-cell count and the family-wise
verdict. `model.json` is unchanged; listing the files there and an optional `conformal` block in `HazardFitSpec`
(so the API, service and MCP can request it) are integration steps. Zoo conformal has no CLI command (Python only).
