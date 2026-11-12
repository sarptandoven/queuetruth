# The L2 model zoo

The zoo runs the queue models people use in L2 backtesters on an L2 view of a dataset, while the exact engine
replays the same dataset at order level. Both arms see the same shadow orders with the same lives. The only
difference is information: the models see sizes per price level and trades, the exact engine sees every order.

## The L2 view

The L2 view is built from the MBO data by an aggregator that is independent of the exact book (the two are compared
after every event, O-L2-1).

| Setting | Default | Meaning |
|---|---|---|
| `depth_levels` (`--l2-depth`) | 10 | Levels per side visible to the models. A level pushed below the top N is reported with size 0 and flag `OUT_OF_VIEW`, as an N-level feed would. `0` shows all levels. |
| `cadence_ns` (`--l2-cadence-ns`) | 0 | `0`: one update batch per exchange event, the most informative L2 feed possible. Greater than 0: depth is conflated into time buckets (net change per bucket, flag `CONFLATED`), as many vendor feeds are. Trades are never conflated. |
| `trade_position` (`--l2-trades`) | `before` | Within a batch, trade rows come before depth rows (`after` reverses). |
| `include_counts` | true | Order counts per level are available to models that use them. |
| `emit_hidden_trades` | true | Hidden prints are emitted with flag `HIDDEN_TRADE`. |

Because the default view is per event, unconflated and complete within ten levels, the errors measured with it are
a floor for what the same models do on a real vendor L2 feed. Use `--l2-cadence-ns` and `--l2-depth` to approximate
your feed. On a venue without hidden orders, a trade with no aggressor side (for example CME implied) is sided from
the quote before the batch, or left unsided and counted.

## How a model sees an order

- **Arrival.** The model sees the order at the first L2 batch with `ts_event > ts_live` (with cadence 0, the same
  event as the exact engine) and receives the visible size at its price before that batch (`Q`; 0 if the price is
  out of view). Touch-mode prices are taken from the exact engine.
- **Rows**, in batch order, after the visible book applies each row: a trade at the order's price with an aggressor
  on the opposite side is `on_trade(v)`; a trade through the order's price is `on_trade_through(v)`; a depth or
  snapshot row at the order's side and price is `on_depth(prev, new)`; a clear ends the order `censored_clear`
  (corrected mode).
- **After the batch**, if the visible opposite best is at or through the price: `on_cross`. Corrected mode caps a
  cross fill at the visible opposite size (a taker fill).
- Cancels and censoring times come from the exact engine, so both arms evaluate the same order over the same life.
  Priority-keeping amends come from `--amends` (a `qt.shadow_amend` file), and `--exclude-order-ids FILE` removes
  real orders (for example your own) from both the tape and the L2 view before the shadows run.
- `hbt_l3_fifo` receives the MBO records instead of L2 rows.

## Notation

`F` is the model's estimate of queue ahead, `T` the trade volume accumulated since the last depth update, `L` the lot
size, `Q` the visible size at arrival, `prev` and `new` the level size before and after a depth row. `rmin(a, b)` is
Rust's `f64::min` (if one argument is NaN it returns the other). `round` rounds half away from zero.

## Faithful and corrected modes

Every model runs in one of two modes (`--mode`):

- **`faithful`** reproduces hftbacktest at commit `5f3ec40` (released as hftbacktest 2.4.4) bit for bit on the same
  platform, including its known quirks. This is what your hftbacktest backtest actually does. Faithful fills that
  hit a quirk are tagged in `zoo_fills.quirk`. Faithful mode also reproduces hftbacktest's order handling: an order
  at or through the opposite best on arrival is a post-only (GTX) order that expires; a book clear keeps the order
  and its estimate; the cross check runs per depth row when the opposite best moves toward the order; each order is
  independent of the others.
- **`corrected`** fixes the quirks listed under each model. Comparing the two tells you how much of a model's error
  comes from the model and how much from implementation details.

## The models

### (a) `hbt_risk_adverse`

hftbacktest `RiskAdverseQueueModel`. Assumes every decrease not explained by trades came from behind you.

```text
arrive:          F = Q
trade(v):        F = F - v, then exec
depth(prev,new): F = rmin(F, new)
exec:            lots = round(-F / L) as integer
                 if lots > 0: F = 0, fill lots * L
```

Exchange (`--exchange`): `partial` fills `min(q, leaves)`; `no-partial` fills the whole order on any `q > 0`.
Trade-throughs and crosses fill fully.

Faithful quirks: a fill needs only half a lot of volume beyond the queue (`-F = 0.5 L` rounds to one lot). With the
partial-fill exchange, a fill that leaves less than half a lot makes the order a resting "filled" zombie (hftbacktest
issue #312, tagged `hbt_312`); hftbacktest raises `InvalidOrderStatus` on that order's next fill attempt and aborts
the whole backtest. queuetruth stops only that order: from the abort on, its rows carry status `hbt_312_abort`.
The aborts are not random (they hit the orders that reach a partial fill), so since 0.1.1 a shadow on which any
model aborted is left out of every model's metrics: all models are scored on one common population. The summary
gives the per-model counts (`hbt_312_aborts`, one `hbt_312_abort:` warning per model) and the number of shadows
left out (`n_excluded_hbt_312`); the terminal prints one line for all of them, and `--verbose` prints each (you may
see it in [Getting started](../getting-started.md)). Corrected:
`lots = floor(max(-F, 0) / L + 1e-9)` (volume strictly beyond the queue, no rounding up), depth update
`F = min(max(F, 0), new)`, and a fill equal to the leaves completes the order.

### (b) `hbt_power[n]`, `hbt_power2[n]`, `hbt_power3[n]`, `hbt_log`, `hbt_log2`

hftbacktest `ProbQueueModel` with its five probability functions. On arrival `F = Q`, `T = 0`. A trade reduces `F` and
is remembered in `T`; execution is as in (a). A depth update allocates the part of the decrease that trades do not
explain between front and back:

```text
chg = prev - new - T;  T = 0
if chg < 0:  F = rmin(F, new); stop                 # the level grew
front = F;  back = prev - front
pi    = prob(front, back);  if pi is infinite: pi = 1
est   = front - (1 - pi) * chg + rmin(back - pi * chg, 0)
F     = rmin(est, new)
```

`pi` is the probability that a unit of decrease is **behind** you (hftbacktest's convention; Rigtorp and Databento
state the complement). With `f = front`, `b = back`:

| Family | `prob(f, b)` |
|---|---|
| `hbt_power[n]` | `b^n / (b^n + f^n)` |
| `hbt_power2[n]` | `b^n / (b + f)^n` |
| `hbt_power3[n]` | `1 - (f / (f + b))^n` |
| `hbt_log` | `ln(1 + b) / (ln(1 + b) + ln(1 + f))` |
| `hbt_log2` | `ln(1 + b) / ln(1 + b + f)` |

Larger `n` in `hbt_power` and `hbt_power3` puts more of each decrease behind you, so the order advances more slowly:
higher `n` is more pessimistic.

Example, `hbt_power3[n=2]`, `F = 30`, level 100 to 80, no trades: `pi = 1 - 0.3^2 = 0.91`,
`est = 30 - 0.09 * 20 + rmin(70 - 18.2, 0) = 28.2`. This and eight more cases are golden values that must match to
the last bit (O-ZOO-1).

Faithful quirks: `T` carries over to the next depth update even if it belonged to an earlier batch; a negative `F`
with a non-integer `n` produces NaN, and `rmin(NaN, new)` resets `F` to the full level size (tagged
`hbt_nan_reset`). Corrected: `front = max(F, 0)` (if 0, only cap at `new`); `back = max(prev - front, 0)`; `pi = 0`
when `back = 0`; a non-finite `pi` is replaced by `back / (front + back)` and clamped to [0, 1];
`F = min(max(est, 0), new)`; `T` is reset at the end of every batch; execution as corrected (a).

### (c) `hbt_l3_fifo` (order-level input)

hftbacktest `L3FIFOQueueModel` with `L3NoPartialFillExchange`. Tracks the orders ahead exactly, like the exact engine.
Faithful: the shadow fills fully when an order on its side executes at a price worse than the shadow's, or when an
order resting behind it at its price executes, or when the opposite best is at or through its price after an
event. Fills are always whole-order, which overstates fill size for orders larger than one lot. Corrected: fill
quantities follow the exact engine and must equal it (O-ZOO-4).

**Why faithful fills fewer orders than the exact replay.** The two modes share the activation event, the price, the
latency, the population and the label (filled means first fill by the deadline, for `p_fill` and for
`truth_filled` alike), and the corrected mode equals the exact replay. So the gap comes from the fill rules alone.
Three of hftbacktest's rules give fewer fills:

1. **Trade-through is measured against the feed's best price on the order's side, not against the order's price.**
   hftbacktest fills better-priced backtest bids only when an execution is strictly below the feed's best bid
   (`if exec_price_tick < depth.best_bid_tick()` in `L3FIFOQueueModel::fill_market_feed_order`, asks likewise).
   Its source says it assumes that "the fill event should occur before the cancel event which may update the best
   price". In Nasdaq ITCH and IEX DEEP+ an execution does come before the removal of the same order, but each
   execution message carries its own removal, so a sweep's earlier order has left the book before the next execution
   arrives. The executed order is therefore always at the feed's best. A shadow priced better than every feed order
   on its side therefore never fills by trade-through. That happens when it improved the touch (the `+1` placement)
   or when the orders at and above its price executed or left, as in a sweep through its level. Only a cross of
   the opposite side fills it. The exact replay fills it at the first execution through its price, since that seller
   or buyer would have met the better order first. When the feed best is still on the book (an execution below it),
   hftbacktest's tick-range shortcut fills an order above that best only if the execution is at least 3 ticks below it.
2. **Hidden prints are ignored.** hftbacktest acts only on fills with a side. On Nasdaq and IEX the exact replay
   also fills from a usable hidden print at the price once no displayed order is ahead, or through the price
   ([hidden prints](shadow-orders.md#hidden-prints)).
3. **An order that crosses at activation expires.** A GTX order expires unfilled, while the exact replay takes
   liquidity. This needs a locked or crossed book when the order goes live, because the zoo rejects a `+1`
   placement that would cross.

**It is hftbacktest's own behaviour.** The released hftbacktest 2.4.4, run on the same IEX DEEP+ and Nasdaq ITCH
records with the same orders, gives the same first fill as the faithful replica on all 117,356 orders traced, and
every missed fill is rule 1 or rule 2; on IEX, hidden prints are three quarters of the gap. The trace, its cause
shares and how to rerun it: [the L3 FIFO findings](https://github.com/sarptandoven/queuetruth/blob/main/scripts/scale/findings/l3-fifo-gap.md).

Whole-order fills do not count here: they change how much fills, not whether or when an order first fills. Neither
do hftbacktest #312 aborts: those come from the L2 probability models, never this one, and `common_population`
drops those shadows for every model and for the exact arm alike. In the other direction, faithful counts an
execution behind the shadow at a price other than its resting price (ruling A4b), and a price-improved execution
below the feed best, as fills.

On the synthetic Nasdaq fixture days (`crates/zoo/tests/l3_faithful_gap.rs`), 5,655 one-lot shadows are placed
with the `standard` weights (60% at the touch, 20% one tick behind, 10% two behind, 10% one tick better) and live
60 s each. Faithful fills 56.66% of them within 60 s and the exact replay 58.73%. Of the 123 orders only the exact
replay fills, 118 (95.9%) are rule 1 and 5 (4.1%) are rule 2. Of the rule 1 misses, 103 were placed at or behind
the touch and 15 at `+1`. That split is by placement: at the missed execution, both kinds rest above every feed
order on their side. Rule 3 is not measured, because the harness activates orders only on uncrossed books. Faithful
has 6 fills the exact replay lacks: 5 price-improved executions and 1 cross. The test pins these counts.

These are shares on a synthetic Nasdaq tape. To bucket a real run's misses, read the
`hbt_l3_fifo[mode=corrected]` rows of `zoo_fills`: corrected equals the exact replay, and `zoo_fills` holds only
model fills, not the exact arm's. A miss's first-fill `reason` there is `trade_through` for rule 1 or a hidden print
through the price, `queue` for rule 2, and `crossed_at_live` for rule 3. A `queue` miss is a hidden print by
elimination, not by observation: faithful also fills on every displayed execution behind the order, so a corrected
`queue` fill that faithful lacks can only come from a hidden print at the price.

**The IEX DEEP+ day 2026-10-09, sample.** This bucketing ran on 26 of the atlas plan's 130 units (every fifth unit
by id), with each unit's own `standard` population, 60 s horizon, `h_max` 300 s, and uncensored rows with a label.
Of 1,165,575 orders, faithful misses 18,915 (1.62% of orders) and has no extra fills. On the full day (all 130 units, `standard` population,
flag policy `exclude`) the atlas has an exact fill rate of 10.76% within 60 s and faithful `hbt_l3_fifo` at
-1.62 pp (95% interval -1.68 to -1.56). In the top-100 tier those figures are 45.67% and -2.99 pp (-3.76 to
-2.26).

| corrected first-fill `reason` | missed orders | share |
|---|---|---|
| `trade_through` (rule 1, or a hidden print through the price) | 10,902 | 57.6% |
| `queue` (rule 2: a hidden print at the price, by elimination) | 8,013 | 42.4% |
| `crossed_at_live` (rule 3) | 0 | 0% |

By placement, the `trade_through` misses are 6,015 at the touch, 2,360 at `+1`, 1,767 at `-1` and 760 at `-2`.
Most are orders whose level was swept or emptied ahead of them, not orders that improved the touch. Hidden
executions are about 42% of the IEX gap against 4% on the synthetic tape, which carries far fewer hidden prints. The
faithful rules stay hftbacktest's.

### (d) `nt_queue_l2`

NautilusTrader-style queue position on L2 (`queue_position=True`), re-implemented from NautilusTrader's public
documentation and from the behaviour of its released package; no NautilusTrader code is used. In faithful mode the
three NT families ((d), (e) and (f) at `p = 1`) fill the same orders as nautilus_trader 1.231.0's `BacktestEngine` on
a delta-fed L2 book at every horizon checked, on both Nasdaq days and the synthetic fixture
([NautilusTrader cross-check](../RESULTS.md#nautilustrader-cross-check)).

```text
arrive:            F = Q
trade(v):          consumed = min(v, F); remaining = v - consumed
                   if remaining <= 0: F = max(F - v, 0)
                   else:              F = 0; fill min(remaining, leaves)
depth(prev, new):  faithful:  if new == 0 or snapshot: F = 0   # level deleted, including OUT_OF_VIEW
                              else: F unchanged                # size updates never move the queue
                   corrected: if new == 0: F = 0 (OUT_OF_VIEW: unchanged)
                              else: F = min(F, new)
```

No lot rounding. Faithful mode follows NautilusTrader 1.231.0 on an `OrderBookDelta` feed (checked against the real
library by `scripts/nt_crosscheck`): hidden prints (no aggressor) at the price decrement the queue on both sides,
book size updates never shrink `F` (only at-price trades do; a level delete, including one that only scrolled out of
view, or a snapshot row resets it to 0), and a trade-through or cross does not fill while `F > 0`. Corrected:
`F = min(F, new)` on size updates, `OUT_OF_VIEW` leaves `F` unchanged, hidden trades are ignored and there is no
queue gate. (The specification extends the out-of-view correction to every corrected family; so far only
`nt_queue_l2` has it.)

NautilusTrader 1.231.0 resets `queue_position` whenever an `OrderBookDepth10` arrives, so on a depth-10 feed its
queue families fill like `nt_prob_touch`. From 2.0.0rc5 a depth snapshot keeps the queue position, and 2.0 also caps
queue ahead at a level's new size when the level shrinks, so faithful `nt_queue_l2` (1.231.0's rules) agrees with
2.0.0rc7 on 99.60% to 100% of orders per day and horizon ([Results](../RESULTS.md#nautilustrader-cross-check)). These models describe
NautilusTrader 1.231.0 on a delta feed.

### (e) `nt_prob_touch[p]`

NautilusTrader `FillModel(prob_fill_on_limit=p)`. A touch is a displayed at-price trade with an opposite aggressor
while the order is live; in faithful mode a hidden print at the price is also a touch, for either side (NautilusTrader
moves both book sides to the trade price). At each touch the order fills `min(leaves, v)` with probability `p`. The prediction is
analytic:

```text
p_fill(h) = 1 - (1 - p)^N(h)           N(h) = touches in (ts_live, ts_live + h]
          = 1 once a trade-through or cross has occurred by h
```

`p` is per touch, so it depends on how often a feed prints; it does not transfer between feeds.

### (f) `nt_queue_l2_prob_touch[p]`

(d) then (e): at each touch, apply (d) to get the volume beyond the queue, and fill with probability `p` only if
that volume is positive. `N(h)` counts only such touches. In faithful mode a trade-through or cross sets the
prediction to 1 only once the deterministic `F` of (d) is 0 (NautilusTrader's queue gate).

### (g) `databento_mc[k]`

Databento's queue-position tutorial estimator, re-implemented from what its code does (its displayed formula
differs from its code). It is a Monte Carlo over `particles` (default 100) queue positions.

```text
p_front(k, x) = x^(1 / (1 + k))                for k <= 0
              = 1 - (1 - x)^(1 / (1 - k))      for k > 0,    k in (-1, 1)
```

Faithful (as the tutorial): each particle starts at `F_j = Q`. On a depth decrease `d = prev - new > 0`: if
`new = 0`, `F_j = 0`; else with `x = F_j / new` and a uniform draw `u`, `r = u^(1 + k)` (or `1 - (1 - u)^(1 - k)` for
`k > 0`), and if `r < x` the whole decrease is taken from the front (`F_j -= d`). Then `F_j` is clamped to
`[0, new]`. The tutorial has no trade channel; our documented extension fills a particle by
`floor(max(v - F_j, 0) / L) * L` on an at-price trade. Corrected: trades reduce `F_j` and are netted out of the next
decrease as in (b), and the front probability uses the pre-update totals `p_front(k, F_j / (F_j + B_j))`.

The prediction is the share of particles filled by each horizon; each (model, shadow) pair has its own seeded
random stream, so results do not depend on thread count (O-ZOO-5). The expected-value version of this model is
`hbt_power3[n = 1 / (1 + k)]` for `k <= 0`, so `k = -0.8` corresponds to `n = 5`.

### (h) `front_of_queue` and (i) `trade_through_only`

The bounds. `front_of_queue` assumes nothing is ahead: every at-price trade, displayed or hidden, fills
`min(leaves, floor(v / L) * L)`, and trade-throughs (displayed or hidden), crosses and a cross on arrival fill the
leaves. `trade_through_only` never fills on at-price trades, hidden prints or crosses; only a displayed trade-through
fills the leaves. Every way the exact engine can fill a shadow is one of `front_of_queue`'s, and every
`trade_through_only` fill is also an exact fill, so the bounds bracket the exact arm order by order: an order the exact
arm fills within `h_max` is filled by `front_of_queue` no later, and an order `trade_through_only` fills is filled by
the exact arm no later (O-ZOO-6). Fill counts then satisfy `front_of_queue >= exact >= trade_through_only`. The
bounds bound the exact arm, not the other models.

### Baselines that are not queue models

Computed in Python for the same orders: `climatology_aj` (fill probability by stratum from the training window),
`mmw_bucket` (fill rate by 15-minute time of day and top-of-book imbalance quintile), `back_of_queue_rate` (fills by
`h` if the recent at-price trade rate times `h` exceeds level size plus order size), and the
[learned model](learned-model.md).

## The default set

`--models all` runs 16 models: `hbt_risk_adverse`; `hbt_power[n=2,3]`; `hbt_power2[n=2]`; `hbt_power3[n=1,2,3]`;
`hbt_log`; `hbt_log2`; `hbt_l3_fifo`; `nt_queue_l2`; `nt_prob_touch[p=1]`; `databento_mc[k=-0.8]`;
`databento_mc[k=-0.95]`; `front_of_queue`; `trade_through_only`. Mode and exchange come from `--mode` and
`--exchange`.

Model ids are canonical strings with sorted keys, for example `hbt_power3[exch=partial;mode=faithful;n=2]` or
`databento_mc[k=-0.8;mode=faithful;particles=100]`. On the command line you may leave out keys that have defaults:
`--models "hbt_power3[n=2],nt_prob_touch[p=0.3]"`.

## Outputs

`zoo --out DIR` writes `summary.json` (`qt.zoo_summary`, with the leaderboard), `zoo_predictions.parquet` (one row
per model, shadow and horizon: `p_fill`, expected filled quantity, the exact outcome), `zoo_fills.parquet` (unless
`--no-fills`), `zoo_shadows.parquet` (each shadow's planned life and strata inputs), `zoo_errors.parquet` (every
metric by model, horizon and stratum, with CIs) and, with `--write-trajectory`, `zoo_trajectory.parquet`.

## How models are scored

The scoring rules follow the audit's outcome-independent censoring rule
([A2](audit-rules.md#a2-outcome-independent-censoring)):

- A shadow has a truth label at horizon `h` if and only if its planned end (session close or end of data, known
  before the replay) is at least `ts_live + h`, whatever its outcome. A cancel is never censoring.
- The **leaderboard** uses never-cancel shadows: no planned cancel within `h_max` (300 s by default). Sample them
  with a lifetime of at least `h_max` (`shadows sample` defaults to 300 s); shadows with shorter planned lives leave
  the leaderboard empty and appear only in the cancel-bearing metrics.
- **Cancel-bearing** shadows, such as mirror shadows with real cancel times, are evaluated separately under metric
  names prefixed `cancel_bearing/` in `zoo_errors.parquet`, with their own cancel as a competing risk.

Per model, horizon (100 ms, 1 s, 10 s, 60 s and `h_max`) and stratum, over eligible rows, with `p` the model's fill
probability (0 or 1 for deterministic models) and `y` the exact first fill by `min(ts_live + h, cancel)`:

| Metric | Definition |
|---|---|
| `fill_rate_bias` | `mean(p) - mean(y)`. Positive: the model fills more often than truth. 95% block-bootstrap CI. |
| `fill_rate_bias_aj` | `mean(p)` over the whole population minus the Aalen-Johansen fill incidence of truth at `h` (stratum `all`). |
| `brier` | `mean((p - y)^2)` |
| `logloss` | `-mean(y ln pc + (1 - y) ln(1 - pc))`, `pc = clip(p, 1e-6, 1 - 1e-6)` |
| `overfill_bias` | At `h_max`, model "filled" if `p >= 0.5`: `(c - b) / N` with `c` model-only fills and `b` truth-only fills. |
| discordance test | Primary: cluster-robust (Obuchowski with at least 5 session dates as clusters, else a block bootstrap of `(c - b) / N`), in `zoo_errors.parquet` as `discordance_p_primary` with `n_clusters`. Secondary: exact McNemar on `(b, c)`, the leaderboard's `mcnemar` field, which assumes independent orders. |
| `qty_bias` | `sum(expected filled qty - true filled qty) / sum(qty)` |
| `ttf_log_ratio_median` | Median over orders filled in both of `ln(max(model ttf, 1 us) / max(true ttf, 1 us))`. Deterministic models only. |
| `ttf_iad` | `sum_j abs(mean p(h_j) - CIF_truth(h_j)) * (h_j - h_{j-1}) / h_max` over the run's horizons plus `h_max`: the area between the model's mean fill curve and the Aalen-Johansen fill curve of truth over `[0, h_max]`. Null when any horizon, `h_max` included, has no eligible row (for example predictions without `h_max` rows), never a sum over a shorter span. |
| `qtrack_mae` | With `--write-trajectory`: time-weighted mean of `abs(F_est - F_exact) / (level size at live + qty)`. |

Strata: **queue decile** (deciles of exact queue ahead at activation within instrument and day), **liquidity
bucket** (terciles of displayed volume traded in the 60 s before activation) and **instrument**. CIs resample session
days (30-minute blocks when there are fewer than 5 days), 1,000 resamples by default (`--bootstrap-b`), one draw
matrix shared by every model so differences between models are not resampling noise.

Read the strata before the overall number. A model can be unbiased overall because it is too optimistic at the back
of the queue and too pessimistic at the front.
