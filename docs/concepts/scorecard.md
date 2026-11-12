# Scorecard statistics

`queuetruth score` answers one question: if my simulator had received exactly the orders I sent live, at the same
times, would it have filled the same ones? It compares order by order, not in aggregate, because a model can produce
the right number of fills and still pick the wrong orders.

## The schedule

Your live order log ([canonical live log](../how-to/import-live-logs.md)) is turned into a fixed schedule of shadow
orders. Only limit orders that can rest (GTC, day, post-only, GTX) are scheduled; market, IOC and FOK orders are
counted and excluded.

| `--entry` | Shadow send time | Latency |
|---|---|---|
| `exchange-ack` (default) | the exchange timestamp of the order's acknowledgement, when it is a match or gateway time (otherwise that order falls back to `send-plus-latency`, with a warning) | 0 |
| `send-plus-latency` | your local send time | the spec's latency, else the median measured entry latency |

Cancels are scheduled at the cancel acknowledgement's exchange time (in `send-plus-latency` mode, at the local send
time plus the cancel delay). A quantity reduction that keeps priority on that venue becomes an amend. A price change
or size increase cancels the current leg and starts a new leg `<order_key>.leg<k>`. Each leg's size follows the live
**instruction** (the amend's new total), truncated by the same arm's own fills on earlier legs, never by the live
fills ([A8](audit-rules.md#a8-sim-legs-follow-instructions)). Legs are aggregated back to the order: filled if any leg
filled, filled quantity summed, and the order's largest total quantity as the denominator.

**Evaluation window** ([A9](audit-rules.md#a9-same-evaluation-window)). Both arms are evaluated on the same window per
order, `W = [entry, min(live cancel instruction, entry + H, end of data)]` with `H = 60 s` by default. Fills outside
`W` are ignored in both arms. Orders whose window is cut by the end of data are censored in both arms.

## The two arms

- **live**: what your log says happened.
- **sim**, one of:
  - `--sim exact`: the exact engine on order-level data (the reference);
  - `--sim-model ID`: a zoo model on the L2 view of your data;
  - `--sim-log DIR`: your own simulator's output for the same schedule, as a canonical log (importers exist for
    hftbacktest and NautilusTrader output; see
    [Score my Nautilus backtest](../how-to/score-nautilus-vs-binance.md)).

Orders present in only one arm are counted (`n_unmatched_live`, `n_unmatched_sim`) and excluded. Fills are net of
busts and corrections.

## The exact arm and your own orders

On an order-level venue your live order is itself in the data. The exact arm therefore runs in **self-excluded**
mode ([A1](audit-rules.md#a1-own-order-exclusion)): each live order is matched to its own MBO order (by venue order
id, else by side, price, size and time within 1 ms of entry), and the matched orders' records are removed from the
tape before their shadows are replayed; the engine credits each twin's executions to the shadow in its place
(`replay --exclude-order-ids`, done natively for the exact and the zoo arms). The match rate is reported, with
a warning below 95% on a venue whose feed carries every order. Without this, the shadow would sit behind its own
twin and the scorer would report a false "sim underfills". `--no-self-exclude` turns it off. On the synthetic
own-orders data the exact arm reproduces all 200 live orders (O-SCORE-4), and on 3,000 real Nasdaq orders per day
used as a live log it reproduces every order's filled quantity and first-fill time on both public days (2x2 250 /
2,750 / 0 / 0 and 195 / 2,805 / 0 / 0; [Results](../RESULTS.md), RD-11).

L2-only tapes carry no order ids. There the queue models run on a copy of the dataset with your own resting orders
removed from the visible depth (aligned to the feed within `--match-window-ns`); if that removal is not applied the
scorecard is blocked (failed gate `own_orders_in_l2_depth`, exit 1, a `BLOCKED:` headline).

## Per-order classes

With `N` matched orders: `n11` filled in both, `n00` in neither, `b` live only, `c` sim only. Sim fills with reason
`crossed` or `crossed_at_live` are taker fills ([A5](audit-rules.md#a5-taker-fills)); they are excluded from the
maker fill rate and markouts and reported separately.

## Signed bias

```text
fill_rate_bias    = (c - b) / N                                 sim fill rate minus live fill rate
relative_overfill = (n11 + c) / (n11 + b) - 1                   null if n11 + b = 0
qty_bias          = (sum sim filled qty - sum live filled qty) / sum order qty
```

95% CIs by day-block bootstrap. `direction` is `sim_overfills` if the CI's lower end is above 0, `sim_underfills` if
its upper end is below 0, otherwise `no_significant_bias`.

## Discordance tests

Only the discordant orders (`b`, `c`) carry information about a difference in fill probability.

**Primary: cluster-robust** ([A10](audit-rules.md#a10-clustered-discordance-test)). Orders on one day share market
conditions, so their disagreements are correlated. With clusters = session dates (30-minute blocks when there are
fewer than 5 dates), `d = (c - b) / N` and `K` clusters:

```text
K >= 5:  var = K / (K - 1) * sum_k ((c_k - b_k) - n_k d)^2 / N^2     (Obuchowski)
         p   = 2 * t.sf(abs(d) / sqrt(var), K - 1)
K <  5:  block bootstrap of (c - b) / N, on the same draws as the fill_rate_bias CI
```

The effective cluster count, discordant clusters, design effect and effective N are reported: in the
`discordance_clustered` row of `score_tests.parquet`, in a `clusters:` line of the summary warnings, and, from
schema 1.1, in the summary field `discordance_clustered`.

**Secondary: exact McNemar**, labelled "assumes independent orders":

```text
chi2_cc    = (abs(b - c) - 1)^2 / (b + c)                       0 if b + c = 0
p_chi2     = P(chi2_1 > chi2_cc)
p_exact    = min(1, 2 * sum_{i=0}^{min(b,c)} C(b+c, i) / 2^(b+c))
odds_ratio = c / b,  95% CI exp(ln(c/b) +- 1.96 sqrt(1/b + 1/c))    null if b or c is 0
```

Example (a golden test value, O-STAT-1): `b = 3`, `c = 12` gives `p_exact = 0.03515625`,
`chi2_cc = 4.266666666666667`, `p_chi2 = 0.038867103812417245`.

The same 2x2 and exact test are reported per **queue decile at entry** (exact queue ahead when the data is
order-level, visible level size when it is L2) and per **liquidity bucket** (terciles of volume traded in the 60 s
before entry). Discordance concentrated at the back of the queue is the usual signature of an optimistic queue
model.

## Time to fill

For orders filled in both arms, `d_i = ln max(ttf_sim, 1 us) - ln max(ttf_live, 1 us)`. Reported: the median of `d`
with a bootstrap CI, and a Wilcoxon signed-rank test (two-sided, equal to `scipy.stats.wilcoxon`).

Because many orders never fill, each arm also gets an Aalen-Johansen cumulative incidence of fill (cause 1 fill,
cause 2 cancel or expire, censored at end of data) at 100 ms, 1 s, 10 s and 60 s. The primary paired test is a
permutation test: swap the arm labels within each order with probability 1/2, recompute
`D = sum_h abs(CIF_sim(h) - CIF_live(h))`, and report `p = (1 + #{D* >= D}) / (1 + B)` over `B = 2,000` permutations.
A log-rank test on the fill hazard and Gray's test on the fill incidence are also reported, labelled descriptive
because they assume independent samples and the arms are paired.

**Survival curves** (summary field `ttf_survival`, schema 1.3). Each arm also gets a Kaplan-Meier curve of time to
the first maker fill on the same orders. Here a cancel, an expiry, a first fill as taker and the end of the window
censor the order at their own time, so `S(t)` is the share of orders still unfilled at `t` had none been cancelled.
The cumulative incidence above is the other view, with cancels as a competing risk. Reported per arm: the curve
(at most 300 points; longer curves are sampled at log-spaced times), `S(h)` at each time-to-fill horizon with a 95%
block-bootstrap CI, the median time to fill with its CI (null when fewer than half the orders fill), and
`S_sim(h) - S_live(h)` with its CI. A negative difference means the simulator fills sooner than live.

The test is a log-rank on the fill hazard, stratified by instrument, with a cluster-robust variance:

```text
u_i = delta_i (z_i - zbar(T_i)) - sum_{t_k <= T_i} (z_i - zbar_k) d_k / n_k     z = 1 for sim, per stratum
U   = sum_i u_i                                                               the stratified log-rank score
V   = C / (C - 1) * sum_c (sum_{i in c} u_i)^2                                clusters c = instrument x block
chi2 = U^2 / V,  p = P(chi2_1 > chi2)
```

Both arms of an order fall in one cluster, so the pairing is respected (Lee, Wei and Amato, 1992). With fewer than 5
instrument blocks each order is its own cluster, and `method` says so. With `C` clusters the statistic cannot exceed
`C - 1`, so a few clusters cannot give a very small p-value. The test is in the secondary family
(`ttf_logrank_clustered` in `score_tests.parquet`). The report draws both curves with the intervals as whiskers, and
the terminal answer gives one line: the share still unfilled at 1 s per arm, their difference and the test.

## Markouts

For each fill, with `d = +1` for a buy and `-1` for a sell, fill price `P`, and `M(t)` the mid of the prevailing quote
(the last BBO at or before `t`; null if a side is empty or `t` is past the instrument-day's last event):

```text
mo_ticks(h) = d (M(t + h) - P) / tick
mo_ccy(h)   = d (M(t + h) - P) * 10^price_exp * qty * 10^size_exp * multiplier
mm_ticks(h) = d (M(t + h) - M(t-)) / tick          M(t-) = last BBO strictly before t
```

The reference `microprice` uses `(Qb Pa + Qa Pb) / (Qb + Qa)` instead of the mid (spec field `markout_refs`). Default
horizons: 50 ms, 100 ms, 1 s, 10 s, 15 s, 60 s, 300 s. These are venue-mid realized spreads in the style of SEC Rule
605, not NBBO-based; `n` is reported per horizon so the fills dropped near the end of data are visible. The realized
spread of the taker who hit your passive order equals `2 * mo` of your fill. Reported: mean markout per arm and
horizon with bootstrap CIs; the difference (sim minus live) on orders filled in both; and the mean markout of
sim-only fills against live-only fills. If sim-only fills mark out worse than live-only fills, the simulator is
filling you exactly when you would be picked off. From schema 1.3 the discordant rows also give the number of fills
behind each mean and each mean's own CI (`n_sim_only`, `sim_only_ci_lo`, ...), since these groups are often small.
The terminal answer prints the paired difference at 100 ms, 1 s and 10 s with its CI.

## Bootstrap and multiple testing

- CIs resample **session days** of entry (30-minute local blocks if there are fewer than 5 days, with the warning
  `ci_block=30min`), 1,000 resamples by default, seeded per test so results are reproducible. Python uses PCG64 and
  Rust xoshiro256**, so CIs computed by the two languages agree within Monte Carlo tolerance, not bit for bit.
- The **primary family** (the cluster-robust discordance test and the markout difference at 1 s) is Holm-adjusted.
- Every other p-value (strata, horizons, time-to-fill tests) is Benjamini-Hochberg adjusted at `q = 0.05`
  (Benjamini-Yekutieli with `--fdr by`). All are listed in `score_tests.parquet` with raw and adjusted values.

## Equivalence

Failing to reject "no difference" with few discordant orders is not evidence of realism. To claim the sim matches
live, the scorer uses two one-sided tests (TOST) against a margin you set:

```text
fill_rate_equivalent  iff  the 90% bootstrap CI of fill_rate_bias lies inside [-margin, margin]
tost_p = max(frac(bias* <= -margin), frac(bias* >= margin))    over the finite bootstrap replicates bias*
```

`tost_p` comes from the same bootstrap replicates as the 90% CI (`queuetruth.stats.tost_p_bootstrap`), so the flag
and the p-value use one method: the fill rate is equivalent exactly when `tost_p` is at most 0.05, up to how the
percentile interval is interpolated. The markout test (`markout_tost_p`, `markout_equivalent`) works the same way on
the bootstrap replicates of the mean markout difference. Defaults: fill-rate margin 0.02 (2 pp,
`--fill-rate-margin`), markout margin 0.25 ticks at 1 s (`--markout-margin-ticks`). The normal approximation
`stats.tost_p(estimate, se, margin)` is kept in the Python API for compatibility; the scorer no longer uses it.

## Gates and the headline

`--fail-abs-bias X` exits 1 if `abs(fill_rate_bias) > X`; `--fail-not-equivalent` exits 1 unless the fill rate is
equivalent. Both are meant for CI. The headline in the summary (`verdict.headline`) and the report states the direction
and size of the difference within the window with its 95% interval, the equivalence result, the exact McNemar p
followed by the cluster-robust p and cluster count, the number of orders and the gate status, for example:

```text
no significant fill-rate difference within 1min: +0.0 pp (95% CI [+0.0, +0.0]); equivalent within ±2 pp (TOST p = 0); McNemar p = 1 (cluster-robust p = 1, 26 clusters); 200 orders; no gates configured
```

The terminal output of `queuetruth score` leads with the same answer in a `RESULT:` line, then the P&L gap in a
`P&L:` line (next section), the equivalence result, the order counts, the gate status and the 2x2 table; `--verbose`
prints every statistic. A scorecard with no scored order is an error (`QT-INPUT-002`, "NOT SCORED"), not an empty
result.

## The P&L gap

The fill-rate gap says how often the simulator fills; the P&L gap says what that is worth. It is the per-order mean
of the [shortfall decomposition](../how-to/decompose-the-sim-live-gap.md)'s `total_ccy`, the simulator's markout P&L
minus live's on each scored order, net of fees, with the 95% interval of the same block bootstrap. Positive means
the simulator's P&L is too high. The horizon is the markout equivalence horizon (`--markout-horizon`, 1 s by
default), or the closest markout horizon when that one is not among `--markout-horizons`.

A difference is stated only when its 95% interval excludes 0. Otherwise the line says that no P&L difference was
found, with the estimate and its interval, which is not evidence that the P&L matches. The parts are the shortfall
buckets per order regrouped by what the simulator did:

| Part | Shortfall buckets | Reads as |
|---|---|---|
| `granted` | `missed_fill` of orders only the simulator filled, `partial_fill` of orders it filled more of than live | fills it grants that live did not get |
| `denied` | `missed_fill` of orders only live filled, `partial_fill` of orders it filled less of than live | fills live got that it misses |
| `timing` | `delay + spread + adverse_selection` | when and at what price it fills the orders live also filled |
| `fees` | `fees` | fees and rebates |

The parts are per-order means over the same orders, so they add up to the gap, and each has a 95% interval on the
same bootstrap draws as the gap (`parts_ci_lo`, `parts_ci_hi`). A finding says "mostly from" a part only when that
part has the sign of the gap, its own interval excludes 0, and it is more than half of the sum of the absolute
parts. Parts that offset one another, or a part that comes from a handful of orders, name nothing. On the synthetic
data of [Getting started](../getting-started.md) (measured 2026-11-01), `front_of_queue` gives:

```text
RESULT: the simulator fills 9.0 pp more often than live (95% CI +5.7 to +12.7 pp) within 1min of entry; significant (cluster-robust p = 7.73e-05, 26 clusters); plain McNemar p = 7.63e-06 assumes independent orders
P&L: the simulator's P&L is 5.78 USD per order too high (95% CI +3.40 to +8.75) at the 1s markout, mostly from fills it grants that live did not get (+3.09 USD per order); 200 orders
```

The rest, +2.69 USD per order, is timing: fills both arms got, which the model gets earlier. With
`--markout-horizon 60s` the timing part is close to 0 (-0.09, interval -0.88 to +0.59) and granted fills are
almost the whole gap (+3.28 of +3.19), as in the [atlas](fill-realism-atlas.md), whose P&L column uses 60 s. Short
horizons isolate the spread and the immediate adverse selection, long ones the fills themselves. `hbt_power3[n=2]`
on the same data understates live, "mostly from fills live got that it misses (-0.510 USD per order)"; its timing
part (-0.340) has an interval that reaches 0 (see [Decompose the sim-live gap](../how-to/decompose-the-sim-live-gap.md)).

A fills-only live log does not hold the orders live never filled, so the fills the simulator grants on them cannot
be counted: the line says the P&L gap is not answerable and states no finding (`direction = not_answerable`).

Each currency gets its own line. Orders that cannot be valued (no quote to mark a fill, a fill without a price) are
left out and counted; when none can be valued (for example without a dataset) the line says the P&L is not
computable and why, with the count per reason. Orders of an instrument missing from the dataset have no currency
and are counted only in the shortfall table's total rows when another currency was valued. The same entries are in
`summary.json` as `pnl_gap` (schema 1.2, written with `--out`), and the report shows the sentence in its answer
block and under the verdict headline. A scorecard blocked by own orders in the L2 depth prints no `P&L:` line and
the report shows none; its `pnl_gap` headlines carry the same `BLOCKED:` prefix as the verdict headline.

## Fills-only logs

Some sources only report fills (never-filled orders are absent). Then every statistic that needs unfilled orders is
not computable: bias, McNemar, strata tests, equivalence, the incidence curves and their tests are null and the
headline says so. Markouts and the time-to-fill comparison conditional on fill are still computed.

## Output files

`score --out DIR` writes `summary.json` (`qt.scorecard_summary`, with the P&L gap in `pnl_gap`), `score_orders.parquet` (one row per order with both
arms' outcomes), `score_fills.parquet`, `score_markouts.parquet`, `score_position.parquet` (cumulative position per
arm and instrument, the plot people usually make by hand), `score_tests.parquet` (every test with raw and adjusted p),
`score_shortfall.parquet` (`qt.score_shortfall`: each order's sim-live gap split into delay, spread, missed fills,
adverse selection, partial fills and fees, per markout horizon; see
[Decompose the sim-live gap](../how-to/decompose-the-sim-live-gap.md)) and `schedule.parquet` (the scheduled legs).

## Caveats the report states

- Your live orders affected the book; the sim arm's shadows do not. The report counts live orders that overlapped
  another of yours at the same price and side (`n_overlap_same_px`).
- With `--entry exchange-ack`, the sim starts from the moment the exchange acknowledged the order, so the comparison
  isolates queue and fill logic from entry latency. Use `send-plus-latency` to include it.
- Results depend on data quality; the data-quality verdict is shown first.
