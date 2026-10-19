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
filling you exactly when you would be picked off.

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
no significant fill-rate difference within 60 s: +0.0 pp (95% CI [+0.0, +0.0]); equivalent within ±2 pp (TOST p = 0); McNemar p = 1 (cluster-robust p = 1, 26 clusters); 200 orders; no gates configured
```

The terminal output of `queuetruth score` leads with the same answer in a `RESULT:` line, then the equivalence
result, the order counts, the gate status and the 2x2 table; `--verbose` prints every statistic. A scorecard with
no scored order is an error (`QT-INPUT-002`, "NOT SCORED"), not an empty result.

## Fills-only logs

Some sources only report fills (never-filled orders are absent). Then every statistic that needs unfilled orders is
not computable: bias, McNemar, strata tests, equivalence, the incidence curves and their tests are null and the
headline says so. Markouts and the time-to-fill comparison conditional on fill are still computed.

## Output files

`score --out DIR` writes `summary.json` (`qt.scorecard_summary`), `score_orders.parquet` (one row per order with both
arms' outcomes), `score_fills.parquet`, `score_markouts.parquet`, `score_position.parquet` (cumulative position per
arm and instrument, the plot people usually make by hand), `score_tests.parquet` (every test with raw and adjusted p)
and `schedule.parquet` (the scheduled legs).

## Caveats the report states

- Your live orders affected the book; the sim arm's shadows do not. The report counts live orders that overlapped
  another of yours at the same price and side (`n_overlap_same_px`).
- With `--entry exchange-ack`, the sim starts from the moment the exchange acknowledged the order, so the comparison
  isolates queue and fill logic from entry latency. Use `send-plus-latency` to include it.
- Results depend on data quality; the data-quality verdict is shown first.
