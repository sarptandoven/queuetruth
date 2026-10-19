# Calibration

Calibration picks a queue model's parameter from what actually happened in the queue, instead of from guesswork or
from how well a backtest's PnL matches. There are two kinds:

- **against exact truth** (`calibrate alloc | survival | nt-touch`): needs order-level data for the venue, or for a
  venue you are willing to treat as similar; it does not need your fills;
- **against your own fills** (`calibrate-live`): for L2-only venues, where there is no exact truth; it needs your live
  order log and L2 (or MBO) market data for the same period.

| Command | Family | Parameter | Method |
|---|---|---|---|
| `calibrate alloc --family power\|power2\|power3` | hftbacktest power models | `n` | allocation quasi-likelihood (C1) |
| `calibrate alloc --family databento` | Databento estimator | `k` | allocation quasi-likelihood (C1) |
| `calibrate survival --family databento` | Databento Monte Carlo | `k` | survival likelihood by simulation (C2) |
| `calibrate nt-touch [--queue-gated]` | NautilusTrader `prob_fill_on_limit` | `p` | closed-form maximum likelihood (C3) |
| `calibrate latency --live DIR` | latency | quantiles | empirical, from your order log |
| `calibrate-live` | any of the above families | `n`, `k` or `p` | survival likelihood against your live fills |

`hbt_log` and `hbt_log2` have no parameter; their likelihood is reported for comparison (as `info:` lines in the
result's `warnings`).

## C1: allocation quasi-likelihood (`n`, `k`)

Every exact event in which orders at a shadow's level were cancelled gives one observation: how much of the
cancelled size was ahead of the shadow (`a`) and how much behind it (`b`), at the exact front `F*` and back `B*`
before the event (`alloc_events.parquet`). A probability model `pi_theta(F, B)` says what share of a decrease falls
behind. The fit maximizes

```text
l(theta) = sum_j [ b_j ln pi_theta(F*_j, B*_j) + a_j ln(1 - pi_theta(F*_j, B*_j)) ]
```

with `pi` clipped to `[1e-12, 1 - 1e-12]`. Events with `F* <= 0` or `B* <= 0` carry no information and are skipped.
For the hftbacktest families `pi = prob(n, F, B)` and `theta = ln n`; for Databento `pi = 1 - p_front(k, F / (F + B))`
and `theta = k` (formulas on [The L2 model zoo](l2-model-zoo.md)). The maximum is found on a grid (`ln n` in [-3, 4],
141 points; `k` in [-0.99, 0.99], 199 points) and refined by golden-section search to `1e-7`. Power families report
`param_name = "n"`, with the estimate and both intervals converted from `ln n` to `n`.

This is **teacher forcing**: each event is scored at the exact queue state, not at the model's own running estimate.
It answers "which `n` allocates cancels most like reality, one step at a time". A model run on its own drifts from the
exact state and can carry quirks, so the fitted parameter is always re-scored by running the full model on a
held-out window (below).

**Uncertainty.** Cancel sizes are treated as counts of independent units, which they are not, so the
likelihood-ratio interval (`2 (l_max - l(theta)) <= 3.841459`) is labelled *quasi* (`ci_profile`) and is too narrow.
The primary interval (`ci_bootstrap`) is a block bootstrap: resample session days (30-minute blocks with fewer than
5 days) with replacement, refit, take the 2.5 and 97.5 percentiles (200 resamples by default, `--bootstrap-b`).

**Strata.** Separate estimates by instrument and by distance from the touch (0, 1, 2+ ticks) are reported
(`stratified`) when a stratum has at least 1,000 events. If they differ, a single `n` is a compromise.

## C2: survival likelihood (`k`, Databento Monte Carlo)

For each `k` on a grid of 25 values in [-0.98, 0.9], the training shadows are simulated with the same random numbers
(common random numbers, so differences between grid points are not noise). With horizons `tau_1 < ... < tau_J` (the
spec horizons plus `h_max`):

```text
S_ij = (particles of shadow i with no fill by tau_j + 0.5) / (N + 1),   S_i0 = 1
h_ij = 1 - S_ij / S_i,j-1
l    = sum_i [ sum_{j < K_i} ln(1 - h_ij) + d_i ln h_iK_i + (1 - d_i) ln(1 - h_iK_i) ]
```

`K_i` is the interval of the exact first fill (`d_i = 1`) or the last fully observed interval (`d_i = 0`). This fits
the model as it is used, end to end, at the cost of simulation. v0.1 computes no bootstrap interval for C2 and says
so in `warnings`.

## C3: NautilusTrader touch probability (`p`)

Per training shadow (`touch_records.parquet`): `d_i = 1` if its exact first fill happened on an at-price touch, at
touch number `m_i`; unfilled shadows contribute the `M_i` touches before censoring; a shadow whose first fill was a
trade-through or a cross is censored at that fill (`d_i = 0`, `M_i` = touches strictly before it).

```text
p_hat = sum d_i / sum [ d_i m_i + (1 - d_i) M_i ]
```

The censoring of trade-through fills is the audit's correction
([A7](audit-rules.md#a7-touch-probability-fit)): dropping those shadows instead biased `p_hat` upward (0.300 for a
true 0.200 in the audit's simulation, which a regression test reproduces). `--queue-gated` fits
`nt_queue_l2_prob_touch`, counting only touches where the L2 queue estimate was exhausted. `p` is per touch: it
depends on how often the feed prints and does not transfer between feeds.

## Latency

From a canonical live log ([Import fills from my venue or broker](../how-to/import-live-logs.md)), per order:

```text
entry_ns    = ack.ts_exchange - submit_sent.ts_local_send
response_ns = ack.ts_local_recv - ack.ts_exchange      (only if the exchange time is a match or gateway time)
cancel_ns, amend_ns: likewise, from cancel or amend sent to its acknowledgement
```

Quantiles p10, p50, p90 and p99; negative values are dropped and counted. One-way latency assumes your clock and the
exchange's are synchronized; the result says so (`clock_sync_assumed: true`).

## Protocol: fit on one window, score on another

`--train` and `--test` take dates (`2020-01-30,2020-01-31`) or blocks of each session (`blocks:0-3/5` is the first four
of five equal blocks of each day; `blocks:4/5` the fifth). The fitted model and the default parameters (`n = 2`,
`k = -0.8`, `p = 1`) both run through the full zoo evaluation on the test window against exact truth, and
`calibration.json` (`qt.calibration_result`) reports both under `held_out` and `baseline_held_out`, with Brier score,
log loss and fill-rate bias per horizon. If the fitted parameter does not beat the default on held-out data, the
result shows that too, and the export refuses it (next sections). The calibration directory also holds `fit.json` (the Rust fit before the held-out step),
`alloc_events.parquet` and `touch_records.parquet`.

## Calibration against your own fills (`calibrate-live`)

On an L2-only venue there is no exact truth, but your live fills are a truth of their own. `calibrate-live` schedules
your live orders as the scorer does (instruction-sized legs, the same evaluation window, maker fills only; on an
MBO tape your own orders are removed first, [A1](audit-rules.md#a1-own-order-exclusion)), runs one zoo pass that
evaluates every grid value on every order's first leg, and fits by the C2 likelihood with your first maker fill in
the window as the event. The split is chronological: explicit train and test dates, else the first half of the dates
trains; with one date, the first half of the orders by entry time. On the test orders it reports Brier score, log loss
and fill-rate bias of the fitted and the default model per horizon, and the 2x2 of model against live with the exact
and the cluster-robust discordance tests (`held_out_discordance`). The result is a `calibration.json` with `kind`
`live`, which `export hftbacktest` and `export nautilus` accept under the same trust rules, plus
`live_calibration_test.json`. On an L2-only tape the queue models run on a copy without your own resting orders in the
visible depth; when that cannot be done the result carries the refusing flag `own_orders_in_depth`. Walkthrough:
[Calibrate on an L2-only venue](../how-to/calibrate-live-l2-venue.md).

What it measures is different from exact calibration: the parameter that best reproduces *your* fills, with *your*
placement and *your* orders' effect on the book, on the feed you trade.

## Trust verdict

Every calibration result carries a structured verdict (`trust` in `calibration.json`), and the terminal output of
`calibrate` and `calibrate-live` ends with it. A fit is **exportable** only when all of these hold:

- it has a finite estimate fitted on at least 30 events;
- no refusing flag is set: `at_grid_edge` (the maximum is at an end of the grid, so the likelihood may keep rising
  outside it), `flat_likelihood` or `not_identified` (the data cannot tell the values apart), `degenerate_ci`,
  `own_orders_in_depth` (`calibrate-live` on an L2 tape whose own resting orders could not be removed);
- it **beats the default** out of sample: at every held-out horizon where both are scored it is not worse than the
  library default (`n = 2`, `k = -0.8`, `p = 1`) on log loss or Brier score, and it is strictly better somewhere.
  `trust.beats_default` gives `better`, `tie`, `worse`, `mixed` or `n/a` per horizon.

Other flags (`plateau`, `profile_ci_open`, `ci_grid_resolution`) are notes that do not refuse on their own. The
verdict is `exportable` or `refused` with its `reasons`; it is also written as `trust:` lines in `warnings`.

On the real Nasdaq days, the NautilusTrader touch probability fitted on day one (`p = 0.287`) cut the day-two 1 s
overfill from +2.80 pp to +0.41 pp, while the fitted power3 `n = 3.098` was refused: worse than `n = 2` at the 60 s and
5 min horizons although better at 1 s and 10 s ([Results](../RESULTS.md), RD-9).

## Export

`export hftbacktest` and `export nautilus` refuse a calibration whose verdict is `refused`: they print the reasons
and exit 1 with `QT-CALIB-001`, writing nothing. `--force` writes the snippet anyway with a `# !! NOT VALIDATED`
header that lists the reasons; use it to inspect a fit, not to paste it. The Nautilus snippet emits only what was
fitted (a power-family fit gives no `FillModel` line, a fit without latency no `LatencyModel`).

`queuetruth export hftbacktest --calibration cal/calibration.json` prints a block to paste into an existing
`BacktestAsset` chain:

```python
# queuetruth {version} | calibration {run_id} | {family} fitted on {train}; held-out {test}
# data: {data_sources}
# fit: {param} = {estimate} ({kind}); {events} events from {shadows} shadows; bootstrap CI [...]; profile CI [...]
# held-out: {the beats-default sentence}
# Paste into your existing BacktestAsset chain (keep your data/tick/lot/fee settings).
from hftbacktest import BacktestAsset
asset = (
    BacktestAsset()
    # ... your existing .data(...), .tick_size(...), .lot_size(...), fee model ...
    .power_prob_queue_model3({n})
    .partial_fill_exchange()
)
```

Mappings: power, power2 and power3 to `power_prob_queue_model`, `power_prob_queue_model2` and
`power_prob_queue_model3`; log and log2 to `log_prob_queue_model()` and `log_prob_queue_model2()`; risk-averse to
`risk_adverse_queue_model()`; Databento with `k <= 0` to `power_prob_queue_model3(1 / (1 + k))`, the expected-value
equivalent of the Monte Carlo (for `k > 0` there is no hftbacktest equivalent and the line is omitted with a note).
The exchange line follows the fit's exchange. A `.intp_order_latency("...")` line appears when a latency
calibration exists. The notes always mention hftbacktest's partial-fill accounting issues (#316, #312) and that a
parameter fitted on an unconflated L2 view may need re-checking on a conflated feed.

`queuetruth export nautilus --calibration cal/calibration.json` prints a `FillModel(prob_fill_on_limit=...)` and
`LatencyModel(...)` block written against NautilusTrader's documented API at a pinned commit, with a note to verify
the names for your version. `export latency-npz --live DIR` writes hftbacktest's `IntpOrderLatency` layout
(`req_ts, exch_ts, resp_ts`), one row per submitted order.

## What calibration cannot do

- Exact calibration fits on the venue whose order-level data you have. Moving parameters to a different venue or to
  an L2-only feed (for example from Nasdaq or Coinbase L3 to Binance L2) is an assumption; the result names the venue
  that supplied the truth. To check the transfer on your own fills, use the [scorer](scorecard.md) or
  `calibrate-live`.
- It does not fix a model's structural limits: `hbt_risk_adverse` has no parameter, and no value of `n` makes a model
  see hidden liquidity.
