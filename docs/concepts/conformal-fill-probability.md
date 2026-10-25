# Conformal fill probabilities

A fill probability is only useful if you know how far to trust it. `queuetruth.model.conformal` wraps the learned
hazard model's probabilities ([The learned fill model](learned-model.md)) and the zoo models' probabilities
([The L2 model zoo](l2-model-zoo.md)) in conformal prediction: prediction sets for "filled within `tau`" with a
finite-sample coverage guarantee, conformal p-values, probability intervals, bounds on the time to the first fill,
and a test of the one assumption all of them rest on, that the days you calibrate on and the days you use the
model on are exchangeable.

## The label and why it is uncensored

For a horizon `tau` the label is `Y = 1{fill within tau}`, with the label of the predictions it wraps:

- hazard predictions (`qt.hazard_predictions`): a fill before the policy cancel, measured from the landmark;
- zoo predictions (`qt.zoo_predictions`): `truth_filled`, the exact never-cancel truth from `ts_live`.

Shadows never affect the market, and the replay follows every shadow to an administrative end that is fixed when it
goes live (the session close, or `h_max` for features). So `Y` is observed for every row whose end is at or after
`tau`, and that condition depends only on what is known at the landmark. Excluding the other rows is a selection on
the covariates, which keeps exchangeable rows exchangeable. The policy cancel is a deterministic rule on observed
data: a competing event that ends the order, not censoring. Rows ended early by an ingest-recorded feed gap
(`censored_gap`) are excluded too. Those ends are not known in advance; they are assumed outcome-independent, the
guarantee then holds conditional on no gap, and the report counts them. On the two real Nasdaq days below no row of
any horizon was excluded.

For zoo predictions the planned-life table `qt.zoo_shadows` is required, as in the zoo's own evaluation (SPEC 5.6):
the label at `tau` exists when `ts_admin_end >= ts_live + tau`, never inferred from the `censored` flag. Never-cancel
shadows (no planned cancel before the largest horizon) and cancel-bearing shadows such as real-order mirrors are
separate populations (`population=`), and every shadow that an hbt pair aborted on is left out for all models.

## When the guarantee holds

For a miscoverage level `alpha`, the guarantee holds exactly when all three conditions are true:

1. **The probabilities were produced without the calibration rows and without the test rows.** The hazard GBDT
   is fitted on the `train` split, so its raw CIF (`cif_fill_raw`) may be calibrated and tested on `calib` or `test`
   rows. The isotonic layer behind `cif_fill` is fitted on `calib`, so `cif_fill` with calibration or test rows from
   `calib` is refused (`QT-CONFIG-001`), and `train` rows are always refused, as calibration rows and as test rows.
   Zoo models with preset parameters may be calibrated on any rows. Zoo parameters fitted on data (`queuetruth
   calibrate alloc`, `survival`, `nt-touch`, `calibrate-live`) follow the GBDT's rule: neither the calibration nor
   the test rows may come from the fit's window.
2. **The label is observed** (previous section).
3. **Calibration and test rows are exchangeable within each category.** Categories always include the landmark,
   so an order contributes at most one row per category (rows of one order at different landmarks are dependent).
   A random split of one day's orders is exchangeable by construction. Two different days are exchangeable only if
   the market treated them alike, which the martingale below tests.

Under those conditions, within every category:

| Output | Guarantee |
|---|---|
| prediction set `C(x)` | `P(Y in C(X)) >= 1 - alpha`; `= 1 - alpha` exactly with smoothed p-values; below `1 - alpha + 1 / (n + 1)` with conservative p-values and no tied scores |
| conformal p-value of the true label | conservative: `P(p <= a) <= a` for every `a`; smoothed: exactly uniform |
| Venn-Abers interval `[p0, p1]` | the probability computed with the true label is calibrated, so `E[p0] <= P(Y = 1) <= E[p1]` |
| lower fill-time bound `lo` | "no fill within `tau_lo`" holds with probability `>= 1 - alpha` |
| upper fill-time bound `hi` | "a fill within `tau_hi`" holds with probability `>= 1 - alpha` |
| two-sided fill-time interval | both statements hold together with probability `>= 1 - alpha` (`alpha / 2` per side) |

The guarantee is marginal over calibration draws and test rows. It is not conditional on a single order, and a
single calibration set can land above or below the nominal level by sampling error.

## Methods

**Scores.** The nonconformity score of a candidate label is `s(x, y) = 1 - p_hat(y | x)`: `1 - p` for "filled",
`p` for "not filled".

**Split and Mondrian conformal** (`BinaryConformal`). A taxonomy assigns each calibration row to a category: the
landmark, plus any of `dist_bucket` (`better`, `touch`, `behind1`, `behind2+`), `side`, `instrument` and, for zoo
models, `predicted` (the model's probability in bins of 0.2). With `label_conditional` the label is part of the
category too, which gives the same coverage separately for orders that filled and orders that did not. The p-value
of candidate `y` ranks its score among the `n` calibration scores of its category:

```text
conservative  p_y = (#{s_i >= s} + 1) / (n + 1)
smoothed      p_y = (#{s_i > s} + theta (#{s_i = s} + 1)) / (n + 1),   theta ~ U(0, 1]
C(x)          = {y : p_y > alpha}
```

A category with fewer than `ceil(1 / alpha) - 1` calibration rows (9 at `alpha = 0.1`) is marked `informative:
false`. With conservative p-values its sets always hold both labels. With smoothed p-values its sets are randomized:
a label is dropped at random, with probability up to `alpha`, so they say nothing about the row.

**Venn-Abers** (`VennAbers`). Isotonic regression of the calibration labels on `p`, refitted with the test row added
once as "not filled" (`p0`) and once as "filled" (`p1`). The implementation keeps the pool-adjacent-violators stacks
of every prefix and suffix of the calibration data, so adding a test row pools only the blocks next to it; a test
checks it against a full refit on every insertion point.

**Fill-time bounds** (`FillTimeConformal`). With `K` the grid interval of the first fill (`K = J + 1` without a fill
within the last grid point) and `F` the predicted CIF:

```text
lower:  scores F(tau_K) (+inf without a fill), t = floor(alpha (n + 1))-th smallest
        lo = (first j with F(tau_j) >= t) - 1          "no fill within tau_lo" (no claim when lo = 0)
upper:  scores F(tau_{K-1}) (F(tau_0) = 0, +inf without a fill), t = ceil((1 - alpha)(n + 1))-th smallest
        hi = min(J, 1 + last j with F(tau_j) <= t)     "a fill within tau_hi" (no claim when t = +inf)
```

Both follow from the rank of the test score among the calibration scores and from `F` being nondecreasing.

**Drift across days** (`exchangeability_martingale`). The true-label scores of every calibration row, then every
test row, each group of one role and session date in date order and shuffled within, get online smoothed p-values
`p_n = (#{i < n : s_i > s_n} + theta_n (#{i < n : s_i = s_n} + 1)) / n`. Ordering by role first means a shift
between the calibration and the test rows is visible also when both come from one date, as with `fit-hazard` on a
single day. Under exchangeability the p-values are independent and uniform. A test martingale bets on them: the
Simple Jumper (default; betting functions `1 + eps (p - 1/2)` for `eps` in {-1, 0, 1}, jump rate 0.01) or a uniform
mixture of power martingales `eps p^(eps - 1)`. By Ville's inequality, the chance that one cell's martingale ever
reaches 100 under exchangeability is at most 1%. Reaching it is an alarm: the calibration and the test rows differ
in a way that matters for these scores, and coverage across them is not guaranteed.

The level is per horizon x landmark cell. Over `m` cells the chance of some false alarm can reach `m / 100`, so the
report also gives the Bonferroni verdict, `drift_familywise_alarm`: some cell reached `100 m`, a family-wise level
of 1%. Tests check that the false-alarm rate over 400 exchangeable runs stays within 3 binomial standard deviations
of `1 / threshold`, and that the martingale alarms under a 4x fill-rate shift between calibration and test rows of
one date, of two dates, and without dates.

## Using it

<!-- check-docs: skip (needs fit-hazard and zoo outputs) -->
```python
import pyarrow.parquet as pq
from queuetruth.model import conformal as cf

preds = pq.read_table("model/predictions.parquet")      # fit-hazard output
feats = pq.read_table("feat/features.parquet")          # the features it was fitted on
out = cf.prepare_hazard(preds, feats, score="cif_fill_raw")
report, sets = cf.evaluate(out, cf.mask_split(out, "calib"), cf.mask_split(out, "test"),
                           {"alpha": 0.1, "strata": ["dist_bucket"]})
cf.write_report(report, sets, "model")                  # conformal.json + conformal_sets.parquet

zoo = pq.read_table("zoo/zoo_predictions.parquet")
life = pq.read_table("zoo/zoo_shadows.parquet")          # planned life: required
z = cf.prepare_zoo(zoo, "nt_queue_l2[mode=faithful]", life, feats)
zr, _ = cf.evaluate(z, cf.mask_dates(z, ["2019-12-30"]), cf.mask_dates(z, ["2020-01-30"]),
                    {"alpha": 0.1, "strata": ["predicted"], "smoothed": True})
```

To score orders whose outcome is not known yet, fit a `ConformalCalibrator` once, save it, and apply it to new
predictions. `predict` needs no labels; per row and horizon it returns the p-values, the set and the Venn-Abers
interval, and per row the fill-time bounds (one-sided at `alpha` and the two-sided interval):

<!-- check-docs: skip (needs fit-hazard outputs) -->
```python
cal = cf.ConformalCalibrator.fit(out, cf.mask_split(out, "calib"), {"alpha": 0.1})
cal.save("model/conformal_calibrator.parquet")

cal = cf.ConformalCalibrator.load("model/conformal_calibrator.parquet")
new = cf.prepare_hazard(pq.read_table("tomorrow/predictions.parquet"),
                        pq.read_table("tomorrow/features.parquet"), labels=False)
sets = cal.predict(new)                                  # qt.conformal_sets, y_fill null
```

`fit-hazard --conformal-alpha 0.1` does the hazard part after fitting: it calibrates `cif_fill_raw` on the calib
split, evaluates on the test split and writes `conformal.json` (`qt.conformal_report`), `conformal_sets.parquet`
(`qt.conformal_sets`: per test row and horizon the p-values, the set, the Venn-Abers interval and the fill-time
bounds) and `conformal_calibrator.parquet` next to the model. It checks `alpha`, `--out` and the features columns it
needs before fitting, lists the three files in the printed card, and prints the pooled coverage at 1 s and 10 s. It
warns when pooled coverage, or coverage among filled orders, is significantly below nominal (the 95%
Clopper-Pearson upper limit is under `1 - alpha`), and when the martingale alarms, with the per-cell and the
family-wise verdict. Zoo conformal has no CLI command yet; it is Python only (`prepare_zoo` and `evaluate`).
Smoothing variables and shuffles come from `stable_seed(seed, ...)`, so equal inputs give byte-identical outputs,
and `run_id` hashes the spec and the input digests.

## Results on two Nasdaq days

The L2-only hazard model (`.realdata/hazard/2d-40000-l2`) was trained on 2019-12-30 blocks 0 to 3 and its
isotonic layer fitted on block 4. Conformal calibration uses `cif_fill_raw` on block 4 (3,676 orders, 7,552
order-landmark rows) and tests on 2020-01-30 (19,464 orders, 29,840 rows), `alpha = 0.1`, categories landmark x
`dist_bucket`. The control column calibrates and tests on two random halves of the 2020-01-30 orders (14,865 and
14,975 rows), which are exchangeable by construction. Coverage is pooled over landmarks; the martingale column is
landmark 0 (calibration rows then test rows).

| Horizon | Coverage, day 1 to day 2 | 95% CI | Fill rate day 1 / day 2 | Martingale max (log10) | Coverage, same-day halves |
|---|---|---|---|---|---|
| 0.1 s | 0.770 | 0.765 to 0.774 | 0.009 / 0.024 | 9.67 (alarm) | 0.902 |
| 0.25 s | 0.775 | 0.770 to 0.780 | 0.024 / 0.054 | 2.71 (alarm) | 0.900 |
| 0.5 s | 0.798 | 0.793 to 0.803 | 0.044 / 0.100 | 0.81 | 0.901 |
| 1 s | 0.795 | 0.791 to 0.800 | 0.077 / 0.170 | 0.37 | 0.896 |
| 2 s | 0.812 | 0.807 to 0.816 | 0.138 / 0.257 | 0.20 | 0.894 |
| 5 s | 0.874 | 0.870 to 0.878 | 0.261 / 0.355 | 0.05 | 0.897 |
| 10 s | 0.903 | 0.899 to 0.906 | 0.353 / 0.388 | 0.03 | 0.897 |
| 30 s | 0.906 | 0.903 to 0.909 | 0.447 / 0.398 | 0.05 | 0.897 |
| 60 s | 0.904 | 0.901 to 0.907 | 0.460 / 0.398 | 0.37 | 0.897 |

The confidence interval treats rows as independent; orders of one day share a market path, so it is too narrow.

What the table shows:

- **Within one day coverage is nominal**: 0.894 to 0.902 at every horizon. The halves are exchangeable by
  construction, so this, and the martingale staying quiet in all 27 cells (largest value 10^1.24, far below 100),
  checks the method and its false-alarm rate. It is not evidence about the market.
- **Across days it fails at short horizons.** Fills within 1 s were more than twice as frequent on 2020-01-30 as in
  the late-afternoon calibration block of 2019-12-30, and coverage falls to 0.770 at 0.1 s. The martingale alarms
  in 6 of 27 cells (0.1 s at all three landmarks, 0.25 s at landmarks 0 and 5 s, 1 s at landmark 5 s), all at short
  horizons. Three of them (0.1 s at landmarks 0 and 5 s, 1 s at landmark 5 s) pass the Bonferroni threshold of
  2,700, so the shift is significant at a family-wise 1%. At 10 s and beyond coverage holds (0.903 to 0.906) with
  no alarm.
- **It is a day effect, not the time of day.** The calibration block covers 14:36 to 15:50 ET. Restricting the day
  2 test rows to the same clock window (5,493 rows) gives lower coverage still: 0.667 at 0.1 s, 0.754 at 1 s, 0.889
  at 10 s, with alarms in 9 of 27 cells.
- **The martingale misses part of the shift.** Coverage at 0.5 s to 5 s is also below nominal (0.798 to 0.874)
  without an alarm at landmark 0. An alarm means the guarantee is gone; no alarm does not prove it holds.

Coverage by distance from the touch at 1 s, landmark 0 (calibration / test rows across days: 753 / 4,092 behind 1,
397 / 2,010 behind 2+, 196 / 1,465 better than the best, 2,330 / 11,897 at the touch):

| Stratum | Day 1 to day 2 | Same-day halves |
|---|---|---|
| `behind1` | 0.704 | 0.889 |
| `touch` | 0.798 | 0.901 |
| `better` | 0.869 | 0.902 |
| `behind2+` | 0.951 | 0.894 |

**Marginal coverage hides the fills.** At 0.1 s 98% of rows do not fill, so a 90% set can ignore every fill: the
marginal sets covered 0.000 of the orders that filled within 0.1 s on the same-day halves, and 0.824 at 30 s. With
`label_conditional`, coverage among filled orders on the same-day halves is 0.899 to 0.912 at every horizon (among
unfilled orders 0.895 to 0.902), at the price of sets holding both labels for 0.645 to 0.663 of rows.

**Fill-time bounds.** The pooled lower bound ("no fill within `tau_lo`") covered 0.903 on the same-day halves and
0.839 across days; the two-sided interval covered 0.954 and 0.905. No upper bound was ever claimed: no category
fills within 60 s with probability near 0.9, so "a fill within `tau_hi`" cannot be stated at `alpha = 0.1`.

**Zoo models**, calibrated on all 20,000 sampled shadows of 2019-12-30 and tested on the 20,000 of 2020-01-30,
categories by predicted probability (every model here predicts 0 or 1; all 40,000 shadows are never-cancel, none
was aborted, and every label exists under the planned life):

| Model | Conservative, 1 s | Smoothed, 1 s | Conservative, 10 s | Smoothed, 10 s |
|---|---|---|---|---|
| `front_of_queue` | 0.996 | 0.906 | 0.975 | 0.914 |
| `hbt_power3[n=2]` | 0.990 | 0.898 | 0.986 | 0.900 |
| `hbt_risk_adverse` | 0.992 | 0.896 | 0.987 | 0.898 |
| `nt_queue_l2` | 0.999 | 0.899 | 0.997 | 0.900 |
| `trade_through_only` | 0.981 | 0.893 | 0.965 | 0.895 |

With scores that take only two values, conservative sets keep the predicted label whenever it is the common one, so
coverage is the model's accuracy, well above 0.9; smoothed p-values bring it to the nominal level by randomizing at
ties. No martingale alarm in any of the 10 horizon cells for any model, unlike the learned model's probabilities.
That is a failure to detect a shift in the pooled error rate, not proof that the rules are as accurate on day 2: the
per-category Venn-Abers interval shows one. It turns a hard rule into a calibrated probability, and when
`front_of_queue` predicts a fill within 1 s the day 1 interval is 0.786 while the observed rate on day 2 was 0.870.

Reproduce with `QT_REALDATA_DIR=.realdata pytest -m realdata python/tests/model/test_conformal.py`, which asserts
the numbers above.

## Limits

- Two days are one calibration day and one test day; the cross-day numbers are one draw, not a coverage estimate.
  Calibrate on held-out whole days and report coverage per day before trusting a level across days.
- Coverage is per category and on average. It says nothing about a single order.
- The fill-time bounds live on the grid; finer grids give sharper bounds.
- Live orders and real (non-shadow) orders have informative cancels; their labels are the competing-risk label, and
  the guarantee covers that label, not the never-cancel counterfactual.
