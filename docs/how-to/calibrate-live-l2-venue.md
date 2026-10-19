# Calibrate on an L2-only venue against my own fills

**Goal:** choose the parameter of a queue model (hftbacktest's `n`, Databento's `k` or NautilusTrader's `p`) for a
venue that publishes only L2 data, using your own live orders and fills as the truth, and check the choice on later
orders it was not fitted on.

**You need:** `pip install queuetruth`, L2 market data for the venue (for Binance: Tardis `incremental_book_L2` and
`trades` files), and your live order log for the same period. Two weeks or more is better than two days: the
held-out check needs enough orders.

## Why a different command

On an L2-only venue nobody knows the exact queue: the feed shows the size at each price, not the orders. So the
exact-truth fit of [Calibrate my hftbacktest queue model](calibrate-hftbacktest.md) is not possible. What you do
know is what happened to your own orders: when each one was acknowledged, whether it filled as a maker, and when.
`queuetruth calibrate-live` fits the model parameter to those outcomes:

1. Your live orders are scheduled as in the scorer: each order's first instruction-sized leg enters at the
   exchange acknowledgement (`--entry exchange-ack`, the default) or at send time plus latency
   (`--entry send-plus-latency --latency F,D,G`). Its window runs from entry to the earliest of your cancel
   instruction, entry plus `--window` (default 60 s), the next amend leg and the end of the data. Only maker fills
   inside the window count.
2. One zoo run evaluates every grid value of the family on every order, on the L2 view of the tape.
3. Each candidate is scored with a censored discrete-time likelihood of the order's first maker fill, and the grid
   value with the largest likelihood on the train orders wins.
4. The fitted value and the default are then compared on the test orders, which come later in time.

| `--family` | Zoo model | Parameter | Default grid (no refinement) | Default value |
|---|---|---|---|---|
| `power`, `power2`, `power3` | `hbt_power[n]`, `hbt_power2[n]`, `hbt_power3[n]` | `n` | `ln n` from -3 to 4, 29 points | `n = 2` |
| `databento` | `databento_mc[k]` | `k` | -0.98 to 0.9, 25 points | `k = -0.8` |
| `nt-prob-touch` | `nt_prob_touch[p]` | `p` | 0.02 to 1, 50 points | `p = 1` |
| `nt-queue-l2-prob-touch` | `nt_queue_l2_prob_touch[p]` | `p` | 0.02 to 1, 50 points | `p = 1` |

The likelihood, with horizons `tau_1 < ... < tau_J` = the `--horizons` below the window (default 0.1 s, 1 s,
10 s) plus the window itself:

```text
S_ij = smoothed model survival of order i at tau_j = ((1 - p_fill_ij) N + 0.5) / (N + 1)
         N = --particles for databento, 1000 for the NT families, 1 for the deterministic hbt models
h_ij = 1 - S_ij / S_i,j-1                                    (S_i0 = 1)
l_i  = sum_{j < K_i} ln(1 - h_ij) + d_i ln h_iK_i + (1 - d_i) ln(1 - h_iK_i)
K_i  = interval of the first live maker fill (d_i = 1), else the last interval fully observed (d_i = 0)
```

Orders with no fully observed interval carry no information and are skipped. The CLI has no flag for the grid;
pass `grid` (`lo`, `hi`, `n`, with `lo` and `hi` in `ln n` for the power families) to the Python API, as in the
last block of the walkthrough below. The likelihood is checked by hand in
`test_interval_loglik_matches_hand_computation`, and the fit, the split and the export path in
`test_calibrate_live_power3_fit_split_and_exports` (`python/tests/score/test_score_calibrate_live.py`, with a stand-in
zoo).

**The split is chronological.** With `--train` and `--test` dates, those dates are used; with `--train` alone, the
test window is every other date. Without either, the first half of the session dates trains and the rest tests
(with an odd count, the extra date goes to test). With a single date, the first half of the orders by entry time
trains and the second half tests (echoed as blocks 0 and 1 of 2). `blocks:I-J/N` windows select blocks of each
session, as for `calibrate alloc`. A window date missing from the dataset, an empty train or test window, or no
filled train order is an error (`QT-DATA-001`), not a silent fallback.

## Try it on the synthetic dataset

Every block in this section runs as written. The synthetic dataset is MBO, not L2; `calibrate-live` accepts both.
Its 200 "own orders" are real order lives taken from the synthetic tape, with a canonical live log in
`synth/own_orders/live/`. On an MBO tape the command first removes those orders' own records from the tape
(self-exclusion), so the model does not see your order twice; on an L2 tape that is not possible (see Limits).
The numbers are not a result about any venue.

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --out synth
queuetruth calibrate-live --data synth --live synth/own_orders/live --family power3 --out live-power3
```

The two session dates give a one-day train window (2026-06-15) and a one-day test window (2026-06-16). The output
directory holds `calibration.json` and `live_calibration_test.json`.

The terminal shows the answer first: the fitted value with its intervals, the held-out table against the default
with a `beats default` column, the held-out discordance on the test orders, the trust flags and a verdict line.

<!-- check-docs: run -->
```python
import json

c = json.load(open("live-power3/calibration.json"))
t = json.load(open("live-power3/live_calibration_test.json"))
print(c["kind"], c["family"], c["param_name"], "=", c["estimate"], "| source:", c["spec"]["calibration_source"])
print("trust:", c["trust"]["verdict"], c["trust"]["reasons"], "flags:", c["trust"]["flags"])
print("bootstrap CI:", c["ci_bootstrap"], " profile CI:", c["ci_profile"])
print("train", c["train"]["dates"], "test", c["test"]["dates"])
print("train orders with an observed interval / with a fill:", c["n_shadows"], "/", c["n_events"])
print("held-out discordance:", {k: t[k] for k in ("n", "b", "c", "fill_rate_bias", "ci_lo", "ci_hi",
                                                  "p_exact", "p_clustered", "clustered_method")})

# Is the parameter identified? A flat grid likelihood means the data cannot tell the values apart.
ll = [g["loglik"] for g in t["grid"]]
spread = max(ll) - min(ll)
print(f"grid log-likelihood spread: {spread:.3f}", "(flat: not identified)" if spread < 1.92 else "")
for w in c["warnings"]:
    print("warning:", w)
```

On this synthetic run the likelihood is flat over the lowest grid values and peaks at the lower grid edge, so the
result carries the trust flags `at_grid_edge` and `plateau` and the verdict `refused`: the 100 train orders cannot
tell those values of `n` apart. The example shows the files and the checks, not a usable fit; the synthetic tape's
order flow is not built to make queue position matter (see [Getting started](../getting-started.md)).

Read the result in this order:

1. **Identification.** `live_calibration_test.json` `grid` lists every grid value (`theta`, `value`, `loglik`). If
   the log-likelihood barely changes across the grid (a spread under 1.92 means even the profile interval covers
   the whole grid), the estimate is just the first grid point and means nothing: every candidate model
   predicted the same outcomes for your orders. `ci_profile` is then `null` on both sides. Get more orders, or
   orders where queue position matters (resting at the touch, not inside the spread).
2. **Held-out discordance** (the later orders). A model "fills" an order when its `p_fill` at the window end is at
   least 0.5. `b` counts orders that filled live but not in the model, `c` the reverse. `fill_rate_bias` (model
   minus live, with a block-bootstrap CI), the exact McNemar `p_exact` and the cluster-robust `p_clustered`
   (`clustered_method`, over `n_clusters` day or 30-minute blocks) are in `live_calibration_test.json`, and echoed in
   `calibration.json` `stratified` as rows with `stratum_kind = "held_out_test"` (`fill_rate_bias`, `mcnemar_b`,
   `mcnemar_c`, `mcnemar_p_exact`, `discordance_p_clustered`). A small bias with large `b` and `c` means the model
   gets the count right and the orders wrong.
3. **Fitted against default.** `held_out` (fitted) and `baseline_held_out` (default) give Brier, log loss and
   fill-rate bias per horizon on the test orders, and `trust.beats_default` the per-horizon comparison. The export
   refuses a fit that does not beat the default; even when it does, keep the value only if it is better at the
   horizons your strategy cares about.

`ci_bootstrap` resamples train orders by day (30-minute blocks with fewer than 5 dates; a `ci_block=30min` warning
says so) and refits on the grid. `ci_profile` is the grid range within the likelihood-ratio cut, `null` on a side
that reaches the grid edge. One warning always says the fit was against live outcomes, not exact queue truth.

Export it like any other calibration. The export is fail-safe: this result is refused with `QT-CALIB-001` and exit
code 1, and `--force` writes it anyway under a `# !! NOT VALIDATED` header that lists the reasons:

<!-- check-docs: run exit=1 -->
```sh
queuetruth export hftbacktest --calibration live-power3/calibration.json --out hbt_snippet.py
```

<!-- check-docs: run -->
```sh
queuetruth export hftbacktest --calibration live-power3/calibration.json --out hbt_snippet.py --force
queuetruth export nautilus --calibration live-power3/calibration.json --out nt_snippet.py --force
cat hbt_snippet.py nt_snippet.py
```

The result is a `qt.calibration_result` with `kind` `live` (and `spec.calibration_source` `"live"`); the held-out
discordance is also in its `held_out_discordance` field. For the power families the hftbacktest snippet carries the
`power_prob_queue_model*` line, and the Nautilus snippet emits no constructor (nothing NautilusTrader-specific was
fitted) and says to keep your own `FillModel`. For the NT families it is the other way round: the Nautilus snippet
carries the fitted `p` (and says `queue-gated (yes)` for `nt-queue-l2-prob-touch`), and the hftbacktest snippet
omits the queue-model line with a note. The notes are printed on stderr and stored in `calibration.json` under
`exports.<target>.notes`.

A narrower or finer grid goes through the Python API:

<!-- check-docs: run -->
```python
from queuetruth import api

cal = api.calibrate_live({
    "dataset": {"path": "synth"},
    "live": {"path": "synth/own_orders/live"},
    "family": "power3",
    "grid": {"lo": -1.0, "hi": 2.0, "n": 13},  # ln n from -1 to 2
    "train": {"dates": ["2026-06-15"]},
    "test": {"dates": ["2026-06-16"]},
    "out": "live-power3-fine",
})
print(cal.estimate, cal.ci_bootstrap, cal.train.dates, cal.test.dates)
```

## On Binance with Tardis data

The commands below need your data, so the docs check only checks their syntax.

### 1. Ingest the L2 tape

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth ingest --format tardis-l2 --venue generic --source-venue binance \
    --in binance_incremental_book_L2_2026-10-05_BTCUSDT.csv.gz --in binance_trades_2026-10-05_BTCUSDT.csv.gz \
    --in binance_incremental_book_L2_2026-10-06_BTCUSDT.csv.gz --in binance_trades_2026-10-06_BTCUSDT.csv.gz \
    --out bn-l2
```

Pass every day's book and trades files with repeated `--in` (plain or gzipped CSV). The dataset is L2 only
(`kind: l2`, rules `generic`, `validation_status: assumed`); it cannot serve as exact truth. Without
`--source-venue`, the label is taken from the files' `exchange` column. Check `data_quality.verdict` in the ingest
output; `pre_snapshot_rows_skipped` and `remove_absent_level` are counted there. The reader is tested on fixtures
(`ingest_prepare_tardis_plain_and_gzip`, `crates/crypto/tests/ingest.rs`), not yet on a full real day.

### 2. Import your live orders

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth live import --venue binance --account main --instrument-map instrument_map.csv \
    --in user_stream_2026-10-05.jsonl --in user_stream_2026-10-06.jsonl --in mytrades.json --out live/
queuetruth live validate --live live/
```

`instrument_map.csv` must give each symbol the same `instrument_id` as `bn-l2/instruments.parquet`. The importer,
its inputs and `live validate` are described in [Import fills from my venue or broker](import-live-logs.md).

### 3. Fit and export

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth calibrate-live --data bn-l2 --live live/ --family power3 \
    --train 2026-10-05 --test 2026-10-06 --out bn-cal-power3
queuetruth export hftbacktest --calibration bn-cal-power3/calibration.json --out hbt_snippet.py
```

`--l2-depth`, `--l2-cadence-ns` and `--l2-trades` set the L2 view the zoo runs on. Leave them at the defaults to
fit on the feed as delivered; if your backtest runs on a conflated or shallower view, fit on that view instead.
`--mode faithful` (the default) reproduces hftbacktest's own behaviour.

To see more than the fit reports (markouts, queue deciles, equivalence tests), score the chosen model once on the
test window with the full scorecard:

<!-- check-docs: skip (needs user data) -->
```sh
N=$(python -c 'import json; print(json.load(open("bn-cal-power3/calibration.json"))["estimate"])')
queuetruth score --live live/ --data bn-l2 --sim-model "hbt_power3[n=$N]" --out sc-fitted
```

See [Score my Nautilus backtest against my Binance fills](score-nautilus-vs-binance.md) for reading it.

## Limits

- **Your orders changed the book.** On an L2 tape your own resting size stays in the visible depth, so the model
  counts it in the queue; it cannot be removed (the result's warnings say so). Other traders may also have reacted
  to your orders. On an MBO tape your order's own records are removed first, and the warnings report how many live
  orders were matched to the tape.
- **Selection.** The fit is conditional on where and when your strategy placed orders. A parameter that fits
  orders resting at the touch says little about orders placed deeper in the book or in other market states.
- **Conflation.** Binance depth is conflated even at its fastest setting, so the parameter absorbs what the feed
  hides. It is specific to that feed, depth and conflation; refit when any of them change.
- **One parameter, few orders.** The grid is coarse and there is no refinement. With a few hundred orders the
  bootstrap interval can span much of the grid; read it before using the value.
- **Not exact truth.** A good fit means the model reproduces your fills on this venue, not that it tracks the true
  queue. For that you need order-level data (see [Calibration](../concepts/calibration.md)).
