# Glossary

The project's own terms, and the statistical ones its outputs print, in one place. Each entry is a short definition
and a link to the page that states the rule in full; where the two disagree, the linked page and the
[design specification](../design/spec.md) win. Terms are grouped by the stage of the pipeline they belong to; the
[index](#index) at the bottom lists them alphabetically.

## Data and the book

### Order-level data

Market data that carries every individual order: when it was added, reduced, modified, cancelled or executed, with
an order id. Also called market by order (MBO) or L3. Nasdaq TotalView-ITCH 5.0, IEX DEEP+, Databento's DBN MBO
schema and the L3 channels of some crypto venues are order-level. Only order-level data on a FIFO venue gives
[exact truth](#exact-truth). See [Exact FIFO truth](exact-truth.md#why-order-level-data-gives-a-ground-truth).

### L2 data

Market by price: the total displayed size at each price level, plus trades, without individual orders. An L2 feed
cannot say where an order sits in the queue, so an L2 backtester guesses with a [queue model](#queue-model).
L2-only data drives the [zoo](#zoo), markouts and [`calibrate-live`](#calibrate-live), never exact truth.

### L2 view

The L2 feed queuetruth synthesizes from order-level data so that the queue models and the exact engine see the same
market. It is built by an aggregator independent of the exact book. By default it is per event, unconflated and ten
levels deep, the most informative L2 feed possible, so the model errors measured on it are a floor for what the same
models do on a vendor feed. `--l2-depth`, `--l2-cadence-ns` (conflation into time buckets) and `--l2-trades` make it
look like your feed. See [The L2 model zoo](l2-model-zoo.md#the-l2-view).

### Dataset

A directory queuetruth ingests data into: `manifest.json`, `instruments.parquet`, `data_quality.json` and Parquet
partitions of `qt.mbo` (or `qt.l2`) records. Its `dataset_id`, recorded in the manifest, is a sha256 over the sha256
of every partition file and the source format, so it names the content, not the path; a [run_id](#run_id) hashes it
in place of the path. A [partition](#partition) is one instrument on one session date.
See [Getting started](../getting-started.md) and [Schemas](../reference/schemas.md).

### Partition

One instrument on one session date: the unit of parallelism, of the [data-quality verdict](#data-quality-verdict)
and of output merge order `(session_date, instrument_id)`.

### Event

A maximal run of records of one instrument that ends with a record flagged `LAST`: one matching-engine action, such
as an aggressive order sweeping three resting orders. The book is consistent only at event boundaries, so BBO, depth,
L2 rows, shadow fill decisions and invariant checks happen at event end; shadow activations, cancels and amends happen
at event start. See [Exact FIFO truth](exact-truth.md#events-the-book-is-only-consistent-at-event-boundaries).

### Exact truth

What the order-level replay says happened: a per-instrument book whose price levels are FIFO lists of real orders,
fed every record in source order, so the orders ahead of any queue position are known at every moment. Also "the
exact replay" or "exact FIFO". It covers displayed liquidity on FIFO venues only: not hidden or iceberg quantity,
auctions, pro-rata matching or your own order's effect on others. See
[Exact FIFO truth](exact-truth.md#where-exactness-stops).

### Venue rules

The per-venue rule table (`qt_types::rules`): whether modify records exist and keep priority, whether trades carry
the aggressor side, whether hidden executions exist, how snapshots begin. Every venue has a `validation` label
printed on every output that depends on it: `real data` (`nasdaq`, and the `iex` rule set on one public day),
`synthetic only` (`cme`) or `assumed` (`coinbase`, `generic`, `crypto_l3`). See
[Venue semantics](../venue-semantics.md#the-rule-table).

### Rule set

The named set of [venue rules](#venue-rules) the engines apply to a dataset: `nasdaq`, `cme`, `coinbase`, `generic`,
and two that reuse those columns. `crypto_l3` is `generic` without hidden orders (a side-`N` trade consumes displayed
queue); `ingest --venue generic` resolves to it for every crypto order-level format. `iex` has the `nasdaq` values
and runs on Nasdaq record semantics. The dataset manifest records it as `rule_set` (null in older manifests, where it
follows from `venue`). See [Venue semantics](../venue-semantics.md#the-rule-table).

### Source venue

The venue the data came from, as a string in the manifest and `instruments.parquet` (`source_venue`: `nasdaq`,
`cme`, `bitstamp`, `binance` and so on), as opposed to `venue`, the rule family the engines apply. Several source
venues share one rule set; a calibration's data sources record both.

### Fixed point: price_exp, size_exp, tick and lot

Prices and sizes are integers: real price `= price * 10^price_exp` (per instrument, default `-9`) and real size
`= size * 10^size_exp` (equities `0`, crypto default `-8`). `tick_size` is the price increment in those price units
and `lot_size` the minimum size increment (1 for shares). A decimal that cannot be represented exactly at the
instrument's exponents fails with [`QT-INPUT-004`](../reference/errors.md#qt-input-004); fix the instrument map. See
[Schemas](../reference/schemas.md).

### Validation status

The provenance label carried as `validation_status`, so a report on synthetic or fixture data says so on its first
screen. Two vocabularies: a dataset manifest and a calibration's data sources use `real_data`, `synthetic_only`,
`assumed` or `fixture_only` (how far the venue's rules were checked); a canonical live log uses `real`,
`fixture_only` or `simulated` (a simulator's output imported as a log). It names how far the rules or the source were
checked, not the quality of the data itself.

### Invariant counters

Eight counters that are zero on a clean replay: cancels or fills exceeding the resting size, fills of unknown
orders, references to unknown orders, duplicate adds, unexpected modifies, books crossed after a complete event and
negative spreads. On the two public Nasdaq days all eight are 0 over 16,754,562 records
([Results](../RESULTS.md)).

### Trade parity

The check that trades derived from the replay match the source's own execution prints, in order, on
`(ts_event, sequence, price, size, aggressor side)` (`--parity-trades`).

### Rank oracle

For every real execution, the rank of the executed order within its price level at that moment
(`--rank-oracle`). On a FIFO venue with a correct book it is 0. Executions at one price within `sweep_gap_ns`
(default 2 ms) form a **sweep**; the **sweep-adjusted** rank ignores orders consumed earlier in the same sweep. See
[Queue ahead](queue-ahead.md#rank-and-the-rank-oracle).

### Data-quality verdict

Per partition and for the dataset: `clean`, `warnings` (an anomaly counter is non-zero) or `unusable` (bad-book flags,
or more than 0.1% crossed books or unknown references). Every analysis summary carries it; `--strict` turns
`unusable` into `QT-DATA-001`. `queuetruth dq` writes a fuller data-quality report (`qt.dq_report`) for any dataset.
See [Exact FIFO truth](exact-truth.md#data-quality) and [Check a feed before you trust it](../how-to/check-a-feed.md).

### Hidden print

An execution of non-displayed interest: a `T` record with side `N` and no order-level records. On Nasdaq it can fill
a shadow only once no displayed interest is at or through its price (displayed interest has priority), under the
default `behind-and-through` policy. See [Shadow orders](shadow-orders.md#hidden-prints).

## Shadow orders

### Shadow order

A passive limit order you did not send but want to evaluate: instrument, side, price (absolute or relative to the
touch), quantity and send time, with optional cancel and priority-keeping amends. The exact engine places it in the
real queue and records when, why and how much of it would have filled. Shadows are **independent and
infinitesimal**: they never consume liquidity, never see each other and never change anyone's behaviour. See
[Shadow orders](shadow-orders.md).

### Touch

The best price on a side: the best bid or the best ask. A touch-relative shadow (`price_mode = touch`) takes its
price from the same-side best at activation plus `offset_ticks`: `0` joins the best, `-1` is one tick behind it, `+1`
one tick inside the spread. With no same-side best the shadow ends `rejected_no_touch`. "At or near the touch" in the
results means offsets `-2` to `+1`. See [Shadow orders](shadow-orders.md#activation).

### Queue ahead

The displayed quantity that must trade or cancel before an order at the same price can fill; **rank** is the number
of orders that make it up. Exact in an order-level replay, followed from the records of the specific orders ahead,
never from trade prints; estimated by every L2 queue model. See [Queue ahead](queue-ahead.md).

### Activation

The moment a shadow goes live (`ts_live = ts_submit + submit delay`): the start of the first event with
`ts_event > ts_live`, strictly later, against the book as the previous event left it. Every real order resting at
that side and price is ahead of it. See [Shadow orders](shadow-orders.md#activation).

### Latency model

The delay between a shadow's send time and its activation, cancel or amend, from four replay-wide parts (`feed_ns`,
`decision_ns`, `gateway_ns`, a uniform `jitter_ns`) and each order's own `latency_ns`. The submit delay is
`latency_ns + feed_ns + decision_ns + gateway_ns` plus jitter; a cancel or amend waits only `latency_ns + gateway_ns`
plus its own jitter. Jitter is drawn from a stream seeded by the run seed and the `shadow_id`, so results do not
depend on thread count. See [Shadow orders](shadow-orders.md#latency).

### Fill reason

Why a shadow filled, one per fill row: `queue_depleted` (real orders behind it executed after everything ahead had
gone), `trade_through` (a same-side order at a worse price executed), `crossed` (the opposite best reached its
price after an event) and `crossed_at_live` (marketable on arrival). See
[Shadow orders](shadow-orders.md#how-a-shadow-fills).

### Taker fill

A fill with reason `crossed` or `crossed_at_live`: the shadow walked the opposite displayed book, best level first.
Taker fills are kept out of maker fill-rate and markout statistics and reported separately (audit rule
[A5](audit-rules.md#a5-taker-fills)).

### End state

How a shadow ended: `filled`, `cancelled`, `amended_to_filled`, `censored_session_end`, `open_at_end`,
`censored_clear`, `censored_gap`, `rejected_would_cross` or `rejected_no_touch`. See
[Shadow orders](shadow-orders.md#how-a-shadow-ends).

### Planned end

A shadow's own cancel or the session close, known before the replay and independent of whether it filled
(`shadow_planned_end.parquet`). The zoo decides which shadows have a truth label at each horizon from the planned end
only, never from the outcome (audit rule [A2](audit-rules.md#a2-outcome-independent-censoring)).

### why trace

`queuetruth why`: replays one order's instrument-day with that order (a shadow, or one of your live orders) injected
into the exact engine and writes a `qt.why_trace`, record by record: which orders were ahead of it, what removed them
and which event filled it. `--model` adds a zoo model's view of the same order. See
[Explain one fill](../how-to/explain-one-fill.md).

### Mirror shadow

A shadow copied from a real resting order (`shadows mirror-real`): same side, price and size, sent at the real
order's arrival with zero latency and cancelled when the real order was. It joins directly behind its real twin, so
it never fills when the real order did not, nor before it (O-SH-4). See
[Shadow orders](shadow-orders.md#mirror-shadows).

### Fill rule

The rule set the exact engine fills shadows by. `exact` is the default and the one the docs mean. `et` reproduces
the earlier private prototype's rule (at-price prints deplete the queue) and exists only for parity runs against that
prototype (`replay --fill-rule et`).

### Prototype

The author's earlier private engine ("et", also called the predecessor engine). Its outputs on the two public Nasdaq
days are the expected values of the real-data checks RD-1 to RD-7, which queuetruth reproduces exactly, also from the
raw ITCH bytes. Its artifacts are not public; the public reproduction (`make reproduce`) does not need them. See
[Results](../RESULTS.md).

## The L2 model zoo

### Queue model

A rule a backtester uses to estimate queue ahead and decide when a passive order fills, from level sizes and trades
only. One zoo model is the exception: `hbt_l3_fifo` reproduces hftbacktest's L3 queue model and reads the order-level
book, not the L2 view. The zoo runs the ones people use: hftbacktest's (`hbt_*`, ported from its source),
NautilusTrader's (`nt_*`, re-implemented and checked order by order against the real engine) and Databento's (`databento_mc`,
re-implemented from its tutorial). See [The L2 model zoo](l2-model-zoo.md#the-models).

### Zoo

`queuetruth zoo`: the experiment that runs a set of queue models on the [L2 view](#l2-view) of a dataset while the
exact engine replays the same dataset at order level, with the same shadow orders and the same lives, and scores each
model against the exact answer. `--models all` runs 16: 14 queue-model configurations plus the 2
[bounds](#bounds). `hbt_power[n=1]` equals `hbt_power3[n=1]` in every cell, so since 0.1.1 it runs only when named.

### Model id

The canonical name of one model configuration, with sorted keys, for example
`hbt_power3[exch=partial;mode=faithful;n=2]`. On the command line keys at their default may be left out:
`hbt_power3[n=2]`, `nt_prob_touch[p=0.3]`. `queuetruth zoo --list-models` lists them.

### Particles

`databento_mc[k]` is a Monte Carlo over `particles` (default 100) queue positions per shadow; its prediction is the
share of particles filled by each horizon. Each (model, shadow) pair has its own seeded random stream (O-ZOO-5), so
results do not depend on thread count. The key is left out of the id on the command line when it is at its default.
See [The L2 model zoo](l2-model-zoo.md#g-databento_mck).

### Queue-gated touch model

`nt_queue_l2_prob_touch[p]`: NautilusTrader's touch model gated by its queue model. At each touch it fills with
probability `p` only if `nt_queue_l2` says the traded volume went beyond the queue ahead. See
[The L2 model zoo](l2-model-zoo.md#f-nt_queue_l2_prob_touchp).

### Exact arm

In the zoo, the exact engine's outcome for each shadow: the truth every model is scored against. In the
[scorer](#scorer), the sim arm run as `--sim exact` (see [scorer arms](#scorer-arms)). Same engine, same rules.

### Faithful and corrected modes

Every model runs in one of two modes (`--mode`). **Faithful** reproduces the library as implemented, quirks
included: bit for bit for hftbacktest at commit `5f3ec40` (released as 2.4.4); NautilusTrader's behaviour as
checked against nautilus_trader 1.231.0's `BacktestEngine`; Databento's tutorial as its code runs. This is what your
backtest actually does. **Corrected** fixes the listed quirks. The difference between the two tells you how much of a
model's error comes from the model and how much from implementation details. See
[The L2 model zoo](l2-model-zoo.md#faithful-and-corrected-modes).

### Quirk

A behaviour of a library's implementation that its own model does not intend, reproduced in faithful mode and fixed
in corrected mode, for example hftbacktest filling on half a lot of volume beyond the queue. Faithful fills that hit
one are tagged in `zoo_fills.quirk` (`hbt_312`, `hbt_nan_reset`).

### Bounds

Two reference rules that are not queue models: `front_of_queue` assumes nothing is ahead, `trade_through_only`
fills only on a displayed trade-through. They bracket the exact arm order by order by construction: an order the
exact arm fills is filled by `front_of_queue` no later, and an order `trade_through_only` fills is filled by the
exact arm no later (O-ZOO-6). The bounds bound the exact arm, not the other models. See
[The L2 model zoo](l2-model-zoo.md#h-front_of_queue-and-i-trade_through_only).

### hftbacktest #312

hftbacktest issue #312: with the partial-fill exchange, a fill that leaves less than half a lot makes the order a
resting "filled" zombie, and hftbacktest raises `InvalidOrderStatus` and aborts the whole backtest on its next fill
attempt. In faithful mode queuetruth stops only that order: its rows carry status `hbt_312_abort` and the summary
counts them (`hbt_312_aborts`). Because the aborts hit the orders that reach a partial fill, since 0.1.1 a shadow on
which any model aborted is left out of every model's metrics, the **common population**, and the summary reports how
many were left out (`n_excluded_hbt_312`). In 0.1.0 each model left out only its own aborts; the published
real-data leaderboard was rerun under the common population (see [Leaderboard](#leaderboard)). See
[The L2 model zoo](l2-model-zoo.md#a-hbt_risk_adverse).

### Horizon

The time after activation at which a fill is counted: 100 ms, 1 s, 10 s, 60 s and `h_max` (300 s by default) in the
zoo; per-order evaluation windows in the scorer. "Bias at 1 s" means fills within one second of activation.

### Never-cancel shadow

A shadow with no planned cancel within `h_max`. The zoo leaderboard uses only these. Shadows with real cancel times,
such as mirror shadows, are **cancel-bearing**: they are scored separately under metric names prefixed
`cancel_bearing/`, with their own cancel as a competing risk. See
[The L2 model zoo](l2-model-zoo.md#how-models-are-scored).

### Fill-rate bias

A model's mean fill probability minus the exact fill rate at a horizon, in percentage points: positive means the model
fills more often than truth (it **overfills**, it is optimistic), negative that it underfills. The leaderboard figure
uses the Aalen-Johansen fill incidence of truth (`fill_rate_bias_aj`), and every bias comes with a 95% day-block
bootstrap interval. In the scorer the same name means sim fill rate minus live fill rate, `(c - b) / N`.

### Brier score and log loss

Proper scores of a probability `p` against the outcome `y` (1 if truth filled by the horizon, else 0): Brier is
`mean((p - y)^2)`, log loss `-mean(y ln p + (1 - y) ln(1 - p))` with `p` clipped to `[1e-6, 1 - 1e-6]`. Lower is
better for both. The leaderboard reports both per horizon, and a calibration's [trust verdict](#trust-verdict)
compares them with the library default. See [The L2 model zoo](l2-model-zoo.md#how-models-are-scored).

### Aalen-Johansen incidence

The estimate of the probability of a fill by time `h` when orders can also be cancelled first (a competing risk) or
leave the sample (censoring). queuetruth uses it for every truth fill curve, never one minus Kaplan-Meier, which would
treat cancels as censoring and overstate fills.

### Leaderboard

The zoo's ranked table in `summary.json` (`qt.zoo_summary`) and on the terminal: per model, fill-rate bias with its
CI at each horizon, Brier score, log loss, overfill bias, quantity bias, time-to-fill error and the McNemar counts,
on never-cancel shadows of the common population (since 0.1.1). The published two-day real-data leaderboard
([Results](../RESULTS.md) section 4) is computed that way: faithful mode scores every model on the same 2,529 and
2,518 of 3,000 orders per day. The breadth run (section 4b) is still the 0.1.0 run, where each hftbacktest model left
out only its own #312 aborts.

### Strata

The cuts every zoo metric is also reported on: **queue decile** (deciles of exact queue ahead at activation within
instrument and day; decile 0 has the least queue ahead), **liquidity bucket** (terciles of displayed volume traded
in the 60 s before activation) and instrument. A model can be unbiased overall and wrong in both directions by
decile; read the strata first.

### Day-block bootstrap

How queuetruth computes intervals: resample whole session days (30-minute blocks when there are fewer than 5 days),
1,000 resamples by default, one draw matrix shared by every model so differences between models are not resampling
noise. Results computed this way cover intraday variation of the days you have, not variation across days you do
not.

### Breadth tiers

The five liquidity tiers of the 36-symbol breadth study (mega caps, deep-queue low-priced large caps, ETFs, mid caps,
small caps), fixed in advance. See [Results](../RESULTS.md) section 4b.

## Calibration and export

### Calibration

Fitting a queue model's parameter to what actually happened in the queue: `n` for the hftbacktest power models, `k`
for Databento's estimator, `p` for NautilusTrader's `prob_fill_on_limit`. Against exact truth, by allocation
quasi-likelihood (C1, scored one event at a time at the exact state, **teacher forcing**), survival likelihood by
simulation (C2) or closed-form maximum likelihood (C3). Always fitted on one window and scored on another (the
**held-out** window) against the library default. See [Calibration](calibration.md).

### calibrate-live

Calibration against your own live fills instead of exact truth, for L2-only venues: the parameter that best
reproduces your fills, with your placement, on the feed you trade. See
[Calibration](calibration.md#calibration-against-your-own-fills-calibrate-live).

### Trust verdict

The structured verdict every calibration carries (`trust` in `calibration.json`): `exportable` only with a finite
estimate from at least 30 events, no refusing flag (`at_grid_edge`, `flat_likelihood`, `not_identified`,
`degenerate_ci`, `own_orders_in_depth`), and a fit that **beats the default** out of sample: not worse than the
library default on log loss or Brier score at any held-out horizon, and strictly better at one. Otherwise
`refused`, with reasons. See [Calibration](calibration.md#trust-verdict).

### Calibration export

`queuetruth export hftbacktest` and `export nautilus`: turn a calibration into a block of Python to paste into your
backtest configuration, with a header naming the run, the data, the fit and the held-out result. Fail-safe: a
`refused` calibration exits 1 with `QT-CALIB-001` and writes nothing, unless `--force`, which writes it under a
`# !! NOT VALIDATED` header. See [Calibration](calibration.md#export).

## The learned fill model

### Learned fill model

A competing-risk hazard model (gradient-boosted, discrete time) trained on L2 features synthesized from order-level
data with labels from the exact replay, so it can be applied where you only have L2. It predicts the probability of a
fill (and of a cancel) within each horizon from a **landmark**, a time after entry at which the order is still
resting. The **privileged** variant (`fit-hazard --privileged`) also uses exact queue ahead; it is an upper bound,
and the gap between the two measures what L2 data cannot tell you about fills on that venue. See
[The learned fill model](learned-model.md).

### Brier skill vs climatology

`bss_vs_climatology`, the learned model's headline skill score: `1 - BS_model / BS_clim`, where climatology is the
training window's Aalen-Johansen fill curve by instrument, side, distance bucket and 30-minute bucket. Above 0 the
model beats that lookup table; its CI is a day-block bootstrap of test orders. See
[The learned fill model](learned-model.md#scoring).

## The sim-vs-live scorer

### Scorer

`queuetruth score`: replays the orders you sent live, at the same times, through a simulator and compares fills
order by order, not in aggregate, because a model can produce the right number of fills and still pick the wrong
orders. Its output is a **scorecard** (`qt.scorecard_summary`). See [Scorecard statistics](scorecard.md).

### Canonical live log

The one format every importer writes and the scorer, `calibrate-live` and `shadows from-live` read: a directory with
`orders.parquet` (`qt.live_orders`), `events.parquet` (`qt.live_events`, each lifecycle event with up to three
clocks) and `manifest.json`. Importers exist for Binance, Bybit, OKX, Coinbase, Kraken, Deribit, Hyperliquid, IBKR
Flex, FIX drop copies, QuickFIX logs and hftbacktest and NautilusTrader output. See
[Import fills from my venue or broker](../how-to/import-live-logs.md#the-canonical-live-log).

### Schedule and legs

The fixed list of shadow orders the scorer builds from your live log (`schedule.parquet`). A price change or size
increase cancels the current leg and starts a new one, `<order_key>.leg<k>`; each leg's size follows your live
instruction, never your live fills (audit rule [A8](audit-rules.md#a8-sim-legs-follow-instructions)). Legs are
aggregated back to the order.

### Entry mode

When a scheduled order enters the sim: `exchange-ack` (default) at the exchange time of your order's
acknowledgement with zero latency, which isolates queue and fill logic; `send-plus-latency` at your local send time
plus a latency, which includes entry latency. See [Scorecard statistics](scorecard.md#the-schedule).

### Scorer arms

The two sides the scorer compares. **live** is what your log says happened. **sim** is one of: `--sim exact` (the
exact engine on order-level data, the reference), `--sim-model ID` (a zoo model on the L2 view) or `--sim-log DIR`
(your own simulator's output for the same schedule). Both arms use one [evaluation window](#evaluation-window) per
order. See [Scorecard statistics](scorecard.md#the-two-arms).

### Self-exclusion

On an order-level venue your live order is itself in the data, so a shadow of it would sit behind its own **twin**
and the exact arm would report a false "sim underfills". Self-exclusion matches each live order to its MBO order (by
venue order id, else side, price, size and time within `--match-window-ns`, 1 ms by default), removes the twin's
records from the tape and credits its executions to the shadow in its place. The match rate is reported.
`--no-self-exclude` turns it off. On L2-only tapes your resting orders are removed from the visible depth instead, and
if that cannot be done the scorecard is blocked (`own_orders_in_l2_depth`). Audit rule
[A1](audit-rules.md#a1-own-order-exclusion); [Scorecard statistics](scorecard.md#the-exact-arm-and-your-own-orders).

### Evaluation window

The window per order in which both arms' fills count: from entry to the earliest of your live cancel instruction,
entry plus `H` (60 s by default) and the end of data. Fills outside it are ignored in both arms (audit rule
[A9](audit-rules.md#a9-same-evaluation-window)).

### 2x2 table and discordance

Per matched order: filled in both (`n11`), in neither (`n00`), live only (`b`) or sim only (`c`). Only the
**discordant** orders `b` and `c` carry information about a difference. The primary test is **cluster-robust**
(clusters are session dates, because orders on one day share market conditions; audit rule
[A10](audit-rules.md#a10-clustered-discordance-test)); the exact **McNemar** test is secondary and labelled
"assumes independent orders". See [Scorecard statistics](scorecard.md#discordance-tests).

### Markout

How far the mid moved in your favour after a fill, in ticks or currency, at horizons from 50 ms to 300 s:
`d (M(t + h) - P) / tick`. If the sim's extra fills mark out worse than live-only fills, the simulator is filling you
exactly when you would be picked off. See [Scorecard statistics](scorecard.md#markouts).

### Multiple testing: Holm and FDR

The scorecard's primary family (the cluster-robust discordance test and the 1 s markout difference) is
Holm-adjusted. Every other p-value (strata, horizons, time-to-fill tests) controls the false discovery rate (FDR):
Benjamini-Hochberg at `q = 0.05`, or Benjamini-Yekutieli with `--fdr by`. `score_tests.parquet` lists raw and adjusted
values. See [Scorecard statistics](scorecard.md#bootstrap-and-multiple-testing).

### Equivalence

The claim that sim and live match within a margin you set, by two one-sided tests (TOST) on bootstrap replicates:
the fill rate is equivalent when the 90% CI of its bias lies inside `[-margin, margin]` (2 pp by default). Failing to
reject "no difference" is not evidence of realism; equivalence is. See [Scorecard statistics](scorecard.md#equivalence).

### Gate

A pass or fail condition for CI: `--fail-abs-bias`, `--fail-not-equivalent`, `--strict`, a referee tolerance. The
command completes and writes its outputs, then exits 1 with `QT-GATE-001`. See
[Error codes](../reference/errors.md#qt-gate-001).

### Fills-only log

A live log that reports only fills, so never-filled orders are absent. Every statistic that needs unfilled orders
(bias, McNemar, equivalence, incidence curves) is then null and the headline says so; markouts and time to fill
conditional on a fill are still computed.

## The referee

### Referee

An exact-FIFO regression oracle for other simulators: `queuetruth referee fixtures` writes 16 small bug-class
fixtures (one order each, with the exact engine's expected fills), and `referee run` compares your simulator's fills
with them order by order. Each class is a queue or fill-logic bug: classes 1 to 11 are the list in section 5.8 of the
[design specification](../design/spec.md), several of them reported in widely used engines, and classes 12 to 16
come from the audit rulings. See [Referee my simulator](../how-to/referee-your-simulator.md).

## Runs, outputs and interfaces

### Spec

The JSON object that fully describes a run: inputs, every output-affecting option and the seed. Every
output-affecting option is a spec field, so the CLI, the Python API, the HTTP service and the MCP server accept the
same specs and give the same outputs. Summaries echo the spec.

### run_id

The first 16 hex characters of `sha256(canonical_json({"engine_version", "spec"}))`, where the spec has its
execution-only fields removed (`out`, `threads`) and every dataset or file reference replaced by its content hash.
Canonical JSON is RFC 8785. So the same inputs, spec and version give the same `run_id` and byte-identical outputs
(JSON excluding `timings`) across runs, thread counts and output directories; schema 1.1 fields at their default are
dropped before hashing, so older run ids stay valid. See the [design specification](../design/spec.md) section 7.3.

### Determinism

Same inputs, same spec and same version give byte-identical output files (Parquet, JSON excluding the `timings`
object, HTML) across runs and `--threads` values. All
randomness is seeded from the run seed and stable ids (shadow id, model id), never from processing order.

### Summary

The `summary.json` every command writes: the run's `run_id`, the echoed spec, the headline numbers, the
data-quality verdict, the files written and every warning. Each kind has a published JSON Schema (`qt.replay_summary`,
`qt.zoo_summary`, `qt.calibration_result`, `qt.scorecard_summary` and so on).

### qt schemas

Every JSON document and Arrow table queuetruth writes has a published schema named `qt.<name>`. Arrow tables carry
the metadata `qt.schema_version = "1.1"` and JSON documents the integer `schema_version: 1`. The design
specification's "schema 1.2" (section 3.12) only adds formats, rule sets and `qt.dq_report`, and does not change
those markers. `queuetruth schema list` lists them. See
[Schemas](../reference/schemas.md).

### QT error codes

Every error carries a stable code `QT-<AREA>-<NNN>` (for example `QT-USAGE-001`, `QT-INPUT-002`, `QT-CALIB-001`)
that names the file, row, byte offset or spec field at fault. Exit code 2 is an error, 1 a gate that failed or an
export that was refused. See [Error codes](../reference/errors.md).

### doctor

`queuetruth doctor`: checks the install. It prints the Python and platform details and the engine version, says
whether the bundled JSON Schemas load and whether PostgreSQL and Docker are present (only the self-hosted service
needs them), and exits 1 if it finds a problem. See [Getting started](../getting-started.md#2-install).

### Report

`queuetruth report`: one self-contained HTML page built from one or more output directories (replay, zoo,
calibration, scorecard), with the data's provenance and data-quality verdict shown first.

### Service and MCP server

The self-hostable HTTP service runs the same specs as jobs for a team, with API keys, tenancy and Postgres; the MCP
server exposes the same operations as tools to AI agents. Both share the published JSON Schemas. See
[Self-host the service](../how-to/self-host-the-service.md) and
[Use queuetruth from an agent](../how-to/use-from-an-agent.md).

## Evidence and testing

### Oracle

A test with a stable id that would fail if a documented claim were false, named by area: `O-BOOK-*` (book and
replay), `O-SH-*` (shadows), `O-ZOO-*` (zoo), `O-CAL-*` (calibration), `O-STAT-*` and `O-SCORE-*` (statistics and
scorer), and so on. The id appears in the test function's name. See
[How queuetruth is tested](../testing.md#oracle-list).

### Audit rules

A1 to A11: the rules adopted after an independent audit of the specification against primary sources found two
problems that would have biased a headline number by construction and nine that bias an estimate in a common case.
Each became a binding rule, a code change and a regression test. See
[Rules from the independent audit](audit-rules.md).

### Matching-engine oracle

An independent price-time matching engine that inserts each shadow as a real order into the reconstructed taker flow
and compares its fills with the exact engine's (O-SH-5). On raw ITCH bytes it agrees with the default fill rule on
3,000 of 3,000 shadows on both public days, since both apply the receipt-order priority of orders Nasdaq releases
after the open (rules D1 to D3 in [Venue semantics](../venue-semantics.md)). See [Results](../RESULTS.md) section
3.

### Real-data checks

RD-1 to RD-13: the checks run on the two public Nasdaq TotalView-ITCH days (2019-12-30 and 2020-01-30), from book
exactness against the prototype (RD-1 to RD-7) to the zoo leaderboard (RD-8), calibration (RD-9), the learned model
(RD-10), the scorer (RD-11) and performance (RD-13). `make reproduce` reruns them from the public files. See
[Results](../RESULTS.md).

## Index

| Term | Section |
|---|---|
| 2x2 table, discordance, McNemar | [2x2 table and discordance](#2x2-table-and-discordance) |
| Aalen-Johansen incidence | [Aalen-Johansen incidence](#aalen-johansen-incidence) |
| A1 to A11 | [Audit rules](#audit-rules) |
| activation, `ts_live` | [Activation](#activation) |
| beats the default | [Trust verdict](#trust-verdict) |
| bounds, `front_of_queue`, `trade_through_only` | [Bounds](#bounds) |
| breadth tiers | [Breadth tiers](#breadth-tiers) |
| Brier score, log loss | [Brier score and log loss](#brier-score-and-log-loss) |
| Brier skill score, `bss_vs_climatology`, climatology | [Brier skill vs climatology](#brier-skill-vs-climatology) |
| calibrate-live | [calibrate-live](#calibrate-live) |
| calibration, C1, C2, C3, held-out window, teacher forcing | [Calibration](#calibration) |
| calibration export, `QT-CALIB-001`, NOT VALIDATED | [Calibration export](#calibration-export) |
| cancel-bearing | [Never-cancel shadow](#never-cancel-shadow) |
| canonical live log | [Canonical live log](#canonical-live-log) |
| common population | [hftbacktest #312](#hftbacktest-312) |
| data-quality verdict, `queuetruth dq` | [Data-quality verdict](#data-quality-verdict) |
| dataset, `dataset_id` | [Dataset](#dataset) |
| day-block bootstrap | [Day-block bootstrap](#day-block-bootstrap) |
| determinism | [Determinism](#determinism) |
| `doctor` | [doctor](#doctor) |
| end state | [End state](#end-state) |
| entry mode, `exchange-ack`, `send-plus-latency` | [Entry mode](#entry-mode) |
| equivalence, TOST | [Equivalence](#equivalence) |
| evaluation window | [Evaluation window](#evaluation-window) |
| event | [Event](#event) |
| exact arm | [Exact arm](#exact-arm) |
| exact truth, exact replay | [Exact truth](#exact-truth) |
| faithful, corrected | [Faithful and corrected modes](#faithful-and-corrected-modes) |
| FDR, Benjamini-Hochberg, Holm | [Multiple testing: Holm and FDR](#multiple-testing-holm-and-fdr) |
| fill reason, `queue_depleted`, `trade_through`, `crossed` | [Fill reason](#fill-reason) |
| fill rule, `exact`, `et` | [Fill rule](#fill-rule) |
| fill-rate bias, overfill | [Fill-rate bias](#fill-rate-bias) |
| fills-only log | [Fills-only log](#fills-only-log) |
| gate, `QT-GATE-001` | [Gate](#gate) |
| hftbacktest #312, `hbt_312_abort`, `n_excluded_hbt_312` | [hftbacktest #312](#hftbacktest-312) |
| hidden print | [Hidden print](#hidden-print) |
| horizon, `h_max` | [Horizon](#horizon) |
| invariant counters | [Invariant counters](#invariant-counters) |
| L2 data, market by price | [L2 data](#l2-data) |
| L2 view, conflation | [L2 view](#l2-view) |
| landmark, learned fill model | [Learned fill model](#learned-fill-model) |
| latency model, jitter | [Latency model](#latency-model) |
| leaderboard | [Leaderboard](#leaderboard) |
| markout | [Markout](#markout) |
| matching-engine oracle, O-SH-5 | [Matching-engine oracle](#matching-engine-oracle) |
| MBO, L3, order-level data | [Order-level data](#order-level-data) |
| MCP server, HTTP service | [Service and MCP server](#service-and-mcp-server) |
| mirror shadow | [Mirror shadow](#mirror-shadow) |
| model id | [Model id](#model-id) |
| never-cancel shadow | [Never-cancel shadow](#never-cancel-shadow) |
| oracle, `O-*` test ids | [Oracle](#oracle) |
| `particles` | [Particles](#particles) |
| partition | [Partition](#partition) |
| planned end | [Planned end](#planned-end) |
| prototype, predecessor engine | [Prototype](#prototype) |
| `price_exp`, `size_exp`, tick, lot, `QT-INPUT-004` | [Fixed point: price_exp, size_exp, tick and lot](#fixed-point-price_exp-size_exp-tick-and-lot) |
| `qt.*` schemas | [qt schemas](#qt-schemas) |
| `QT-*` error codes, exit codes | [QT error codes](#qt-error-codes) |
| queue ahead, rank | [Queue ahead](#queue-ahead) |
| queue decile, liquidity bucket, strata | [Strata](#strata) |
| queue-gated touch model, `nt_queue_l2_prob_touch` | [Queue-gated touch model](#queue-gated-touch-model) |
| queue model | [Queue model](#queue-model) |
| quirk | [Quirk](#quirk) |
| rank oracle, sweep | [Rank oracle](#rank-oracle) |
| RD-1 to RD-13, `make reproduce` | [Real-data checks](#real-data-checks) |
| referee, bug-class fixtures | [Referee](#referee) |
| report | [Report](#report) |
| rule set, `rule_set`, `crypto_l3`, `iex` | [Rule set](#rule-set) |
| `run_id` | [run_id](#run_id) |
| schedule, legs | [Schedule and legs](#schedule-and-legs) |
| scorer, scorecard | [Scorer](#scorer) |
| scorer arms, live, sim | [Scorer arms](#scorer-arms) |
| self-exclusion, twin, `--no-self-exclude` | [Self-exclusion](#self-exclusion) |
| shadow order, independent and infinitesimal | [Shadow order](#shadow-order) |
| source venue, `source_venue` | [Source venue](#source-venue) |
| spec | [Spec](#spec) |
| summary, `summary.json` | [Summary](#summary) |
| taker fill | [Taker fill](#taker-fill) |
| touch, `offset_ticks`, `rejected_no_touch` | [Touch](#touch) |
| trade parity | [Trade parity](#trade-parity) |
| trust verdict, refusing flags | [Trust verdict](#trust-verdict) |
| validation status | [Validation status](#validation-status) |
| venue rules | [Venue rules](#venue-rules) |
| `why`, `qt.why_trace` | [why trace](#why-trace) |
| zoo, `--models all` | [Zoo](#zoo) |
