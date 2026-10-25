# Fill toxicity: what a queue model's mistakes cost

The [zoo leaderboard](l2-model-zoo.md) counts how often a queue model's fills disagree with the exact arm. A
strategy does not care about counts; it cares about PnL. `queuetruth zoo-toxicity` prices the disagreement. It tags
every fill of the exact arm and of every model with the mechanism that produced it, measures signed mid markouts
after each fill, and splits each model's PnL error into the fills it wrongly grants, the fills it wrongly denies and
the fills it gets at the wrong time.

## Run it

`zoo-toxicity` reads a finished zoo run (with `zoo_fills.parquet`, so not `--no-fills`) and the dataset it ran on.
It replays the zoo's shadows exactly on that dataset to get the exact arm's fill rows, prints and quotes, checks that
the replay reproduces the zoo's exact arm shadow by shadow, and writes its outputs to `--out`.

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --venue nasdaq --out synth
queuetruth shadows sample --data synth --n 400 --seed 1 --out shadows.parquet
queuetruth zoo --data synth --shadows shadows.parquet --mode corrected --out zoo
queuetruth zoo-toxicity --zoo zoo --data synth --window 60s --out tox
```

The terminal answer comes first: the exact arm's markout per fill mechanism, then one row per model with its phantom
and missed counts and its `grant`, `deny`, `timing` and `bias` terms at the rank horizon (a `*` marks a 95% interval
that excludes 0) and its bias rank interval, then the answer in plain words: the exact arm's own PnL, the model that
misstates PnL the most, the one that gains the most from wrongly granted fills and the one that loses the most to
wrongly denied fills (with their phantom or missed markouts, and whether the first place is separated from the
second), and the two bounds. The full document is `tox/summary.json` (`qt.zoo_toxicity`):

<!-- check-docs: run -->
```python
import json

doc = json.load(open("tox/summary.json"))
print(doc["n_shadows"], "shadows,", doc["exact"]["n_fills"], "exact fills:", doc["exact"]["fills_by_mechanism"])
for r in doc["rankings"]["deny"][:3]:
    print(f"{r['rank']} {r['model_id']:50} deny {r['value']:+.3f} ticks/order, rank interval "
          f"{r['rank_lo']}-{r['rank_hi']}, separated from the next: {r['separated_from_next']}")
```

| Flag | Default | Meaning |
|---|---|---|
| `--zoo` | required | Zoo output directory. |
| `--data` | required | The MBO dataset the zoo ran on. |
| `--out` | required | Output directory. |
| `--shadows` | the zoo summary's | Shadow spec of the zoo run, when the path recorded in the zoo summary does not resolve from here. |
| `--amends` | the zoo summary's | Shadow amend file of the zoo run, likewise (only for a zoo run with amends). |
| `--horizons` | `100ms,1s,10s,60s` | Markout horizons. |
| `--window` | the zoo's `--h-max` | Fill window from `ts_live`. It must be one of the zoo's prediction horizons, because the exact arm's status is read from the zoo's prediction rows there. |
| `--rank-horizon` | `60s` | Markout horizon of the rankings; one of `--horizons`. |
| `--bootstrap-b` | 1000 | Bootstrap resamples. |
| `--allow-truth-mismatch` | off | Evaluate the orders whose replay reproduces the zoo's exact arm when some do not. |
| `--seed` | 0 | Bootstrap seed. |
| `--threads` | 1 | Replay threads; the outputs do not depend on it. |

The replay uses the zoo's own shadows, amends, latency, jitter seed and `--exclude-order-ids`. Every input and
configuration check runs before it: a `--data` whose `dataset_id` differs from the one the zoo summary records is
`QT-INPUT-002`, and a `--window` or `--rank-horizon` that does not fit is `QT-CONFIG-001`. After the replay, an order
whose replayed exact fills within the window differ from the zoo's exact arm (filled or not, or a different first
fill time) means the inputs are not the zoo's: the run fails with `QT-INPUT-002`. With `--allow-truth-mismatch` those
orders are left out instead, counted in `n_excluded.truth_mismatch` and `truth_mismatch_rate`, and the rest are a
selected subset. On the real data below the count is 0.

## Fill mechanisms

| Mechanism | Exact arm (`shadow_fills.reason`) | Model arm (`zoo_fills.reason`) |
|---|---|---|
| `queue` | `queue_depleted`: the queue ahead was consumed by executions at the price | `queue` |
| `swept` | `trade_through`: the level was traded through | `trade_through` |
| `cross_on_arrival` | `crossed_at_live`: the order crossed the book on arrival (a taker fill) | `crossed_at_live` |
| `crossed_resting` | `crossed`: a resting order crossed by an opposite arrival at or through its price | `crossed` |
| `hidden_print` | a `queue_depleted` or `trade_through` fill caused by a hidden print (A3d) | not available: zoo fill rows carry no record sequence |
| `touch` | | `touch_draw` (`nt_prob_touch*`) |
| `particle` | | `databento_mc`: no fill rows are written; one row per filled order at the median particle first-fill time |

A fill is a hidden-print fill when its (instrument, timestamp, record sequence) is that of a `hidden` print in the
replay's `trades.parquet` and of no `displayed` print. This works because a print record has its own sequence while
an execution's trade, fill and removal records share one; the timestamp keeps the sequences of different days apart.
Over the whole 300 s order life of the day-one corrected run below it tags 415 exact fills, of which 173 are at the
order's price; the replay's own `shadow_diagnostics.hidden_print_fills` counts exactly those 173. Within the 60 s
window of the tables below it tags 336.

## Status of each order

For each model and order, within the fill window `W`:

| Status | Model filled | Exact arm filled |
|---|---|---|
| `confirmed` | yes | yes |
| `phantom` | yes | no |
| `missed` | no | yes |
| `neither` | no | no |

"Model filled" is the fill the model books: a model fill row at or before `ts_live + W`. This holds for the models
whose fills are random draws too (`nt_prob_touch[p<1]`, `nt_queue_l2_prob_touch`), so the status describes the fills a
backtest with that model would book. The particle models (`databento_mc`) write no fill rows: for them it is the
leaderboard's rule, `p_fill >= 0.5` at horizon `W`, and their fill is the median particle's. A model row's fills carry
the status of their order; `missed` fills are the exact arm's fills of the orders the model missed, with the exact
mechanism. `n_inconsistent` is a cross-check: an order whose deterministic forecast (`p_fill` exactly 0 or 1) disagrees
with the fill rows (the rows decide), or a filled particle order without a median first fill in the window (no
markout). It is 0 for every model on the synthetic fixture and on the real data below.

The population is the leaderboard's rule applied at `W`: orders with an exact arm, outside any faithful hbt #312
abort, with no planned cancel before `ts_live + W` and an administrative end at or after it. Orders the exact replay
rejected before they went live (`end_state` `rejected_*`) and that no arm fills are left out and counted in
`n_excluded.never_live`; one a model fills stays in, as a phantom.

## Markouts

The conventions are the scorer's. `d` is +1 for a buy and -1 for a sell, `P` the fill price and `M(t)` the mid of the
prevailing BBO of the fill's instrument-day (the last quote at or before `t`; undefined with an empty side, before
the first quote and past the instrument-day's last event).

- `mo_ticks = d (M(t + h) - P) / tick`, the fill's PnL per unit after `h` (realized spread in ticks).
- `mm_ticks = d (M(t + h) - M(t-)) / tick`, the mid move after the fill from the last quote strictly before it: the
  adverse selection itself, independent of the fill price.
- `mo_bps` and `mm_bps` are the same moves relative to `|P|` and `M(t-)`, times 10,000.

Exact fills use their own `fill_px`. A model maker fill uses the order's price; a model taker fill uses the
prevailing opposite best at the fill, capped at the limit (the limit when there is no quote, with a `taker_price`
warning), because the zoo does not record model fill prices.

## The PnL decomposition

Per order, take the first fill row of each arm and its `mo_ticks` at horizon `h` for one unit. Over the `n` orders
whose first-fill markouts are defined in every arm that filled them:

| Term | Formula | Reads as |
|---|---|---|
| `grant` | `sum over phantom orders of mo_model / n` | PnL of the fills the model wrongly grants |
| `deny` | `- sum over missed orders of mo_exact / n` | minus the PnL of the fills it wrongly denies |
| `timing` | `sum over confirmed orders of (mo_model - mo_exact) / n` | PnL error of fills both arms get, at different times or prices |
| `bias` | `grant + timing + deny` | `model_pnl - exact_pnl`, the model's PnL error per order |

Every term is in ticks per order of the population, unit quantity; positive means the model's simulated PnL is too
high. The same terms in bps are `*_bps`. Each model also reports the mean markout of its phantom, missed and
confirmed orders (`phantom_mo_ticks`, `missed_mo_ticks`, `confirmed_mo_ticks`, `confirmed_exact_mo_ticks` and the
`mm` versions): a phantom fill with a positive mean markout is a fill the model invents just before the price moves
in its favour.

The rankings at `--rank-horizon` order the models by the size of each term: `grant`, `deny` and `bias`, each by its
absolute value, largest first, with the signed value kept. Rank 1 of `deny` is the model whose wrongly denied fills
distort PnL the most, whichever way. Every statistic carries a 95% block-bootstrap interval: blocks are session dates
with 5 or more dates, else 30-minute buckets of `ts_live`. One draw matrix is shared by every model and statistic,
which keeps the intervals comparable and allows paired comparisons, and the rankings use it: each place carries its
rank interval over the replicates (`rank_lo` to `rank_hi`) and the paired gap to the next place with its interval
(`gap_to_next`); `separated_from_next` is true when that interval lies above 0. Places that are not separated are a
tie at this sample size.

The default rank horizon is 60 s because at 60 s the timing term is close to 0 for every model on the real data
below (within +/-0.03 ticks per order), so the rankings measure the fills that were wrongly granted or denied rather
than when the right fills happened; `--rank-horizon 1s` ranks the short-term view, where timing dominates.

## Outputs

| File | Rows | Content |
|---|---|---|
| `summary.json` | | `qt.zoo_toxicity`: population, exact fills by mechanism, per-model counts and decomposition per horizon, the fill-type table `by_type` (fill-quantity-weighted mean markouts per arm, status and mechanism), the rankings and the warnings. |
| `zoo_toxicity_fills.parquet` | one per fill | `arm` (`exact` or the model id), `shadow_id`, `fill_seq`, `instrument_id`, `side`, `ts_event`, `px`, `fill_qty`, `liquidity`, `mechanism`, `status` (model rows), `reason` (the source reason). |
| `zoo_toxicity_markouts.parquet` | one per fill and horizon | `arm`, `shadow_id`, `fill_seq`, `horizon_ns`, `mo_ticks`, `mo_bps`, `mm_ticks`, `mm_bps` (null when undefined). |
| `zoo_toxicity_shadows.parquet` | one per model and order | `status`, first-fill times and mechanisms of both arms. |

The three tables carry `qt.schema` metadata (`qt.zoo_toxicity_fills`, `qt.zoo_toxicity_markouts`,
`qt.zoo_toxicity_shadows`) and are in the schema registry, so `queuetruth schema show` prints their layout (also
`queuetruth.eval.toxicity.TABLES`) and `doctor` checks it.

The same document comes back from `queuetruth.eval.toxicity.run_zoo_toxicity(spec)`, and
`queuetruth.eval.toxicity.evaluate_toxicity` takes the tables directly.

## Day one on real Nasdaq data

Inputs: the day-one ITCH dataset (2019-12-30, AAPL, MSFT, SPY and TSLA, `.realdata/ds/nasdaq-2019-12-30`)
and the 3,000 shadows of the headline experiment (`.realdata/shadows/d1-sampled-3000.parquet`, see RESULTS.md
section 4), zoo run with the default 16 models, `h_max` 300 s, in each mode, then:

<!-- check-docs: skip (needs the licensed real data) -->
```sh
queuetruth zoo --data .realdata/ds/nasdaq-2019-12-30 --shadows .realdata/shadows/d1-sampled-3000.parquet \
  --mode corrected --threads 3 --out zoo-d1-corrected
queuetruth zoo-toxicity --zoo zoo-d1-corrected --data .realdata/ds/nasdaq-2019-12-30 --window 60s \
  --threads 3 --out tox-d1-corrected-60s
```

Every run below reproduced the zoo's exact arm on every order (`truth_mismatch` 0) and had no inconsistent order.
121 of the 3,000 shadows were priced at or through the opposite best and rejected before going live; no arm fills
them, so they are left out (`never_live`), and the corrected population is 2,879 orders. Wall time on the shared
8-core laptop: 10 to 14 s per zoo run and 13.9 s for the `zoo-toxicity` run above (`--threads 3`; exact replay,
up to 3,772 exact and 51,107 model fill rows over the four runs, 1,000 bootstrap draws). One day gives 13 thirty-minute blocks, so the intervals are wide for
rare statuses; read them as day-one evidence, not as a population estimate.

**The exact arm's fills by mechanism** (corrected run, 60 s window, 2,879 orders, 3,183 fills; mean `mo_ticks` with
95% CI, last column `mm_ticks` at 1 s):

| mechanism | fills | 100 ms | 1 s | 10 s | 60 s | mid move 1 s |
|---|---|---|---|---|---|---|
| `queue` | 1,290 | -0.05 [-0.25, +0.20] | -0.32 [-0.56, -0.05] | -1.46 [-3.18, -0.54] | -1.03 [-2.54, +0.34] | -1.58 [-1.95, -1.23] |
| `swept` | 737 | -1.38 [-1.85, -0.85] | -2.62 [-3.35, -1.85] | -3.77 [-5.11, -2.45] | -2.13 [-7.55, +1.79] | -4.40 [-5.07, -3.66] |
| `crossed_resting` | 820 | -0.96 [-1.16, -0.75] | -1.19 [-1.62, -0.89] | -1.15 [-1.80, -0.60] | -0.04 [-0.99, +1.25] | -0.97 [-1.53, -0.65] |
| `hidden_print` | 336 | -0.08 [-1.09, +1.57] | -1.69 [-3.03, -0.53] | -3.28 [-7.17, +1.22] | -2.81 [-7.25, +2.34] | -3.42 [-5.76, -1.75] |
| all | 3,183 | -0.62 [-0.83, -0.36] | -1.18 [-1.44, -0.90] | -1.97 [-2.81, -1.24] | -1.07 [-2.06, -0.26] | -2.09 [-2.53, -1.74] |

From 100 ms to 10 s a sweep is a worse fill than a fill from the queue draining at the price. On the same bootstrap
draws, the paired difference `swept - queue` is -1.33 [-1.72, -0.93] ticks at 100 ms, -2.30 [-2.95, -1.67] at 1 s
and -2.32 [-4.16, -0.02] at 10 s, and at 1 s the mid moves 4.4 ticks against a swept order. A queue fill is close to
flat at 100 ms. Sweeps and hidden-print fills are not separated at 1 s or later (`swept - hidden_print` at 1 s:
-0.94 [-2.39, +0.86]), and at 60 s no two mechanisms are separated.

**Each model's PnL error at the 1 s markout** (corrected run, 60 s window, ticks per order; the exact arm's own PnL
is -0.697 [-0.877, -0.496] ticks per order):

| model | phantom | missed | grant | deny | timing | bias |
|---|---|---|---|---|---|---|
| `front_of_queue` | 43 | 0 | +0.022 [+0.012, +0.032] | +0.000 [+0.000, +0.000] | +0.196 [+0.142, +0.258] | +0.218 [+0.164, +0.277] |
| `nt_prob_touch[p=1]` | 43 | 20 | +0.022 [+0.012, +0.032] | -0.033 [-0.054, -0.016] | +0.119 [+0.040, +0.200] | +0.107 [+0.025, +0.189] |
| `hbt_l3_fifo` | 0 | 0 | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] |
| `hbt_power2[n=2]` | 22 | 22 | +0.009 [+0.004, +0.014] | -0.038 [-0.057, -0.022] | -0.061 [-0.101, -0.020] | -0.090 [-0.144, -0.039] |
| `hbt_log` | 22 | 22 | +0.009 [+0.004, +0.014] | -0.038 [-0.057, -0.022] | -0.070 [-0.105, -0.028] | -0.099 [-0.151, -0.048] |
| `hbt_power3[n=1]` | 19 | 24 | +0.008 [+0.004, +0.013] | -0.039 [-0.057, -0.022] | -0.076 [-0.110, -0.041] | -0.106 [-0.153, -0.061] |
| `hbt_power[n=2]` | 14 | 26 | +0.007 [+0.003, +0.012] | -0.039 [-0.058, -0.023] | -0.081 [-0.117, -0.045] | -0.113 [-0.162, -0.068] |
| `hbt_power[n=3]` | 11 | 25 | +0.007 [+0.002, +0.012] | -0.039 [-0.058, -0.023] | -0.086 [-0.122, -0.050] | -0.118 [-0.167, -0.074] |
| `hbt_power3[n=2]` | 10 | 26 | +0.007 [+0.002, +0.012] | -0.039 [-0.058, -0.023] | -0.097 [-0.128, -0.061] | -0.129 [-0.175, -0.086] |
| `hbt_log2` | 7 | 29 | +0.004 [+0.001, +0.008] | -0.041 [-0.059, -0.024] | -0.101 [-0.133, -0.065] | -0.138 [-0.182, -0.094] |
| `hbt_power3[n=3]` | 5 | 29 | +0.003 [+0.000, +0.009] | -0.041 [-0.059, -0.024] | -0.107 [-0.143, -0.068] | -0.144 [-0.189, -0.098] |
| `databento_mc[k=-0.8]` | 1 | 30 | +0.001 [+0.000, +0.003] | -0.040 [-0.059, -0.024] | -0.126 [-0.164, -0.087] | -0.166 [-0.208, -0.123] |
| `databento_mc[k=-0.95]` | 1 | 33 | +0.001 [+0.000, +0.003] | -0.041 [-0.059, -0.025] | -0.131 [-0.167, -0.092] | -0.171 [-0.211, -0.129] |
| `hbt_risk_adverse` | 1 | 34 | +0.001 [+0.000, +0.003] | -0.041 [-0.059, -0.025] | -0.133 [-0.169, -0.096] | -0.173 [-0.213, -0.132] |
| `nt_queue_l2` | 0 | 34 | +0.000 [+0.000, +0.000] | -0.041 [-0.059, -0.025] | -0.143 [-0.184, -0.099] | -0.184 [-0.234, -0.139] |
| `trade_through_only` | 0 | 304 | +0.000 [+0.000, +0.000] | -0.085 [-0.120, -0.057] | -0.678 [-0.812, -0.566] | -0.764 [-0.911, -0.647] |

What it shows on these orders:

- **Phantom fills are the profitable ones.** Every model's wrongly granted fills have a positive mean markout: +1.1
  to +2.5 ticks at 1 s (for example `hbt_power3[n=2]` +1.90 [+1.17, +2.60], `nt_prob_touch[p=1]` +1.44
  [+1.03, +1.79]), against -1.18 for the exact arm's fills. An order the market did not fill is one the price moved
  away from, so a model that fills it books a gain that never existed. `grant` is positive for every model with a
  phantom.
- **Missed fills here are favourable too**, +3.5 to +5.0 ticks at 1 s for every model except `trade_through_only`
  (+0.81), because most of them are hidden-print fills: 29 of the 35 missed fills of `hbt_power3[n=2]` (mean +4.72
  ticks, quantity-weighted), on 22 of its 26 missed orders, all TSLA. Those TSLA orders rest at the touch or one or
  two ticks behind it (one is one tick inside), with a quoted spread of 6 to 41 ticks (median 15.5) at `ts_live`. The
  hidden prints are in the L2 trade feed (rows flagged `HIDDEN_TRADE`), but the hbt and particle models,
  `trade_through_only` and the NT models in corrected mode ignore them, while `front_of_queue` uses them and misses
  none. Denying these fills understates PnL, so `deny` is negative; the fix is an L2 model that consumes hidden
  prints.
- **Timing dominates the error at 1 s.** On orders both arms fill, the hbt power and log models, the particle models,
  `hbt_risk_adverse` and `nt_queue_l2` fill at moments with worse 1 s markouts than the exact fill (-0.06 to -0.14
  ticks per order), while the optimistic `front_of_queue` and `nt_prob_touch[p=1]` fill earlier and look +0.12 to
  +0.20 ticks better. At the 60 s markout the timing term is within +/-0.03 ticks per order for every model, and
  the remaining error is the grant and deny terms.
- The two bounds bracket the exact arm order by order: `front_of_queue` never misses and `trade_through_only` never
  grants. Corrected `hbt_l3_fifo` is the exact arm (0 phantom, 0 missed, every term exactly 0).

**The rankings the command gives** (corrected run, 60 s window, at the default 60 s markout, in `|bias|` order; the
exact arm's PnL is -1.086 [-2.475, -0.165] ticks per order; last column the bias rank and its 95% rank interval):

| model | grant | deny | timing | bias | bias rank |
|---|---|---|---|---|---|
| `trade_through_only` | +0.000 [+0.000, +0.000] | -0.975 [-1.499, -0.645] | +0.026 [-0.091, +0.152] | -0.949 [-1.537, -0.572] | 1 (1-1) |
| `nt_queue_l2` | +0.000 [+0.000, +0.000] | -0.201 [-0.368, -0.054] | +0.012 [-0.038, +0.057] | -0.189 [-0.387, -0.026] | 2 (2-9) |
| `hbt_risk_adverse` | +0.003 [+0.000, +0.009] | -0.201 [-0.368, -0.054] | +0.010 [-0.043, +0.057] | -0.188 [-0.388, -0.022] | 3 (2-10) |
| `databento_mc[k=-0.95]` | +0.003 [+0.000, +0.009] | -0.188 [-0.349, -0.047] | +0.009 [-0.043, +0.057] | -0.175 [-0.368, -0.017] | 4 (2-11) |
| `hbt_power3[n=3]` | +0.013 [+0.000, +0.033] | -0.197 [-0.365, -0.047] | +0.010 [-0.039, +0.054] | -0.174 [-0.378, -0.005] | 5 (2-11) |
| `databento_mc[k=-0.8]` | +0.003 [+0.000, +0.009] | -0.182 [-0.346, -0.037] | +0.009 [-0.041, +0.056] | -0.170 [-0.360, -0.012] | 6 (4-12) |
| `hbt_log2` | +0.017 [+0.005, +0.029] | -0.197 [-0.365, -0.047] | +0.013 [-0.037, +0.057] | -0.168 [-0.368, -0.002] | 7 (4-12) |
| `hbt_power3[n=2]` | +0.027 [+0.010, +0.047] | -0.196 [-0.364, -0.044] | +0.015 [-0.036, +0.058] | -0.154 [-0.360, +0.018] | 8 (6-15) |
| `hbt_power[n=2]` | +0.036 [+0.019, +0.055] | -0.196 [-0.364, -0.044] | +0.011 [-0.036, +0.052] | -0.148 [-0.351, +0.023] | 9 (6-14) |
| `hbt_power3[n=1]` | +0.045 [+0.026, +0.067] | -0.195 [-0.363, -0.044] | +0.012 [-0.035, +0.055] | -0.138 [-0.344, +0.033] | 10 (6-14) |
| `hbt_power[n=3]` | +0.032 [+0.014, +0.052] | -0.182 [-0.348, -0.037] | +0.015 [-0.035, +0.062] | -0.135 [-0.337, +0.024] | 11 (7-14) |
| `hbt_log` | +0.050 [+0.029, +0.072] | -0.182 [-0.348, -0.039] | +0.009 [-0.039, +0.053] | -0.123 [-0.325, +0.042] | 12 (6-15) |
| `hbt_power2[n=2]` | +0.050 [+0.029, +0.072] | -0.182 [-0.348, -0.039] | +0.011 [-0.039, +0.059] | -0.121 [-0.321, +0.043] | 13 (5-15) |
| `front_of_queue` | +0.147 [+0.088, +0.206] | +0.000 [+0.000, +0.000] | -0.028 [-0.081, +0.025] | +0.119 [+0.035, +0.201] | 14 (2-15) |
| `nt_prob_touch[p=1]` | +0.147 [+0.088, +0.206] | -0.177 [-0.340, -0.035] | -0.005 [-0.058, +0.037] | -0.035 [-0.234, +0.147] | 15 (3-15) |
| `hbt_l3_fifo` | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | 16 (16-16) |

At 60 s the answer is resolved only at the ends. `trade_through_only` is rank 1 for `deny` and `bias` with a rank
interval of 1 to 1: it denies 0.98 ticks per order of fills, about five times the next model, and the gap is
separated. `front_of_queue` and `nt_prob_touch[p=1]` share the first two places for `grant` (+0.147 each: the same 43
phantom orders) and are separated from the hbt models behind them. Between those ends the 13 hbt, particle and
`nt_queue_l2` models deny 0.18 to 0.20 ticks per order and their rank intervals overlap: at one day of data they are
a tie. Corrected `hbt_l3_fifo` is exactly 0 throughout.

In faithful mode (2,408 orders: the 471 orders with a faithful hbt #312 abort leave every model's population, as do
the same 121 never-live orders) the same picture holds, and faithful `hbt_l3_fifo` stands out: 9 phantom and 62
missed fills in the 60 s window and a bias of -0.716 [-0.838, -0.604] ticks per order at 1 s, of which -0.624 is
timing. Over the whole 300 s order life (`--window 300s`, the default here) almost every order fills in every arm
(corrected: 2,600 of 2,879 exact), phantom and missed orders are rare (at most 19 and 10 per model, except
`trade_through_only` with 133 missed), and at 1 s the error is nearly all timing: `front_of_queue` +0.229
[+0.182, +0.282] and `trade_through_only` -1.027 [-1.185, -0.901] ticks per order.

## Limits

- Markouts are against the venue's own BBO mid (single feed, "605-style, not NBBO"), as in the scorer.
- A model taker fill's price is the prevailing opposite best capped at the limit, not a level-by-level walk; in
  the four real-data runs above every fill of every arm is a maker fill (touch-priced shadows that would cross are
  rejected before they go live).
- Particle models (`databento_mc`) contribute one fill per order at the median particle first-fill time, for the
  whole quantity: their markouts describe the median particle, not the mixture.
- The decomposition uses each arm's first fill per order and one unit; quantity-weighted markouts of every fill row
  are in `by_type` and in `zoo_toxicity_markouts.parquet`.
- Model fills caused by hidden prints keep their model mechanism (`queue` or `swept`): zoo fill rows carry no
  record sequence to match.
- The output tables are not in the schema registry yet (see Outputs).
