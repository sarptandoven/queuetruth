# Rules from the independent audit

Before release, the specification of the shadow engine, the zoo evaluation, calibration and the scorer was audited
against primary sources (hftbacktest source at the pinned commit, the Nasdaq ITCH 5.0 specification, the
predecessor engine's venue notes) rather than against its own claims. The audit found two problems that would have
biased a headline number by construction and nine that bias an estimate in a common case, plus smaller ones. Each
finding became a binding rule, an implementation change and a regression test. This page lists them so you can see
what the numbers rest on.

Severity, as the audit defined it: a **blocker** biases a published number by construction; a **major** finding
biases a label or estimate in a common case, or leaves a bug class no test could catch.

| Rule | Finding | Severity | What the code does now | Regression test |
|---|---|---|---|---|
| [A1](#a1-own-order-exclusion) | The scorer's exact arm counted your own order twice | blocker | removes your live orders from the tape before replaying their shadows | O-SCORE-4, `audit_a1_exclude_own_twin` |
| [A2](#a2-outcome-independent-censoring) | Zoo truth labels were censored on the outcome | blocker | labels depend only on the planned end; cancels are a competing risk | O-ZOO-7 |
| [A3](#a3-hidden-prints) | Hidden prints never filled shadows on Nasdaq | major | hidden prints fill once the displayed queue ahead is empty | `audit_a3_hidden_prints_on_nasdaq`, O-SH-1 S12 |
| [A4](#a4-queue-ahead-from-order-records) | Queue ahead depended on trade prints | major | queue ahead follows the records of the orders ahead | `audit_a4_order_level_queue_ahead` |
| [A5](#a5-taker-fills) | Crossed books filled the full quantity as a maker | major | taker fills walk the opposite book and are reported apart | `audit_a5_taker_walk`, O-SH-1 S8 and S9 |
| [A6](#a6-cme-modify-priority) | CME same-size modifies kept priority | major | on CME they lose priority | `o_book_4_a6_cme_unchanged_modify_loses_priority` |
| [A7](#a7-touch-probability-fit) | The NautilusTrader `p` fit dropped orders filled by trade-through | major | those orders are censored at that fill | `o_cal_3_nt_touch_competing_trade_through_audit_c3` |
| [A8](#a8-sim-legs-follow-instructions) | Re-priced scorer legs were sized from live fills | major | legs follow the live instructions | `test_a8_legs_follow_instructions_and_aggregate` |
| [A9](#a9-same-evaluation-window) | The sim arm could rest after the live order ended | major | both arms use one evaluation window | `test_a9_window_ignores_late_fills_in_both_arms` |
| [A10](#a10-clustered-discordance-test) | The primary McNemar test assumed independent orders | major | the primary test is cluster-robust | `test_o_stat_1_clustered_null_size_at_most_0_07` |
| [A11](#a11-a-ground-truth-oracle) | No oracle could catch A3 to A6 | major | an independent matching-engine oracle | O-SH-5 (`python/tests/oracle/`) |

## A1: own-order exclusion

On an order-level venue your live order is itself in the data. Replaying a shadow of it at its acknowledgement time
put the shadow directly behind its own twin, so the shadow could fill only after the live order had completely
filled. The scorer's exact arm would then report "sim underfills" for every partly filled order.

Now, for `score --sim exact` (and `calibrate-live` on an MBO tape), each live order is matched to its MBO order:
first by venue order id, else by side, price and size with the add closest to the entry time within
`match_window_ns` (default 1 ms). The matched orders' `A`, `C`, `M` and `F` records are removed from the replayed
tape; their trade prints stay as unattributed volume at the price, which fills the shadow once the queue ahead is
gone (the shadow takes the twin's place). The match rate is reported. The synthetic quickstart dataset ships 200
such orders in `own_orders/` together with the expected result: with exclusion, `sim exact` must reproduce every
live fill at the same event (zero discordance), and without it live-only discordance appears. O-SCORE-4 checks
both. On real data, 3,000 real Nasdaq orders per day used as a live log are reproduced exactly by the exact arm with
self-exclusion on both public days, while without it the sim arm underfills by 5.3 pp on day one
([Results](../RESULTS.md), RD-11).

## A2: outcome-independent censoring

The zoo compared each model's fill probability with the exact outcome at each horizon, and dropped a row when the
order was unfilled and its life had ended before the horizon. Filled orders stayed in even when their life was
short, so the sample over-represented fills, and a cancel both arms share was treated as censoring. In the audit's
simulation a perfectly calibrated model showed a fill-rate bias of about -0.12 instead of 0.

Now a truth label at horizon `h` exists if and only if the shadow's **planned** end (its session close or end of
data, known before the replay) is at least `ts_live + h`, whatever the outcome. A cancel is never censoring. The
leaderboard uses never-cancel shadows (no planned cancel within `h_max`); shadows with real cancel times, such as
mirror shadows, are evaluated separately under metric names prefixed `cancel_bearing/`, with their own cancel as a
competing risk. Fill curves of truth are Aalen-Johansen cumulative incidence, never one minus Kaplan-Meier. O-ZOO-7
checks that a calibrated predictor's bias CI contains 0 under the new rule and that the old rule is biased.

## A3: hidden prints

Nasdaq executes in price, display, time priority: hidden interest at a price trades only after all displayed
interest there, so a displayed shadow is ahead of it. The old rule never let a hidden print fill a shadow. Now a
hidden print is used when the book at that moment shows no displayed interest at or through its price (a later
oracle ruling tightened the first version of this rule); it then fills a shadow at its price once the shadow's
queue ahead is empty, and is a trade-through for shadows priced better than the print. Details:
[Shadow orders](shadow-orders.md#hidden-prints).

## A4: queue ahead from order records

Queue ahead used to be depleted by at-price trade prints. Executions without an at-price print (ITCH non-printable
executions, executions away from the display price) then left the queue stale, and an off-price print could
wrongly count as a trade-through. Now queue ahead follows the records of the specific orders ahead (cancels,
reductions, executions, priority-losing modifies), and a shadow fills from executions of orders behind it, from
trade-throughs and from crosses. Prints never shrink queue ahead.

## A5: taker fills

When the book crossed a shadow, or the shadow was marketable on arrival, it filled its full quantity at its own
price as a maker, ignoring the opposite size and the queue ahead. Now a crossed shadow fills as a taker: it walks the
opposite displayed orders at or through its price (after real same-side orders with priority), at those levels'
prices, one fill row per level; a partial fill on arrival leaves the rest resting. Fills with reason `crossed` or
`crossed_at_live` are taker fills, excluded from maker fill-rate and markout statistics and reported separately.
Schema 1.1 adds a `liquidity` column (`maker`, `taker`) to the fill tables; with a build that does not write it
yet, the reason identifies taker fills.

## A6: CME modify priority

On CME an iceberg refresh or an account change appears as a modify with unchanged price and size, and it loses
priority. The old rule kept such orders ahead of the shadow. Now, on CME, only a pure size decrease at the same price
keeps priority.

## A7: touch-probability fit

The closed-form fit of NautilusTrader's `prob_fill_on_limit` excluded shadows whose first fill was a trade-through
or a cross. Those shadows survived some touches first, so excluding them conditions on the outcome; in the audit's
simulation the estimate came out 0.300 for a true 0.200. Now such shadows contribute no fill and the touches strictly
before that fill. The regression test reproduces the audit's simulation and recovers 0.200.

## A8: sim legs follow instructions

When a live order was re-priced or its size increased, the scorer started a new sim leg sized as the new total minus
the live order's fills so far, which made the sim arm depend on the live outcome. Now each leg follows the live
instruction as sent (the amend's new total), truncated by the same arm's own earlier fills.

## A9: same evaluation window

If a live order filled completely, nothing ended the sim leg, which could keep resting and fill later, turning
"live only" into "both". Now both arms are evaluated on one window per order, from entry to the earliest of the
live cancel instruction, entry plus 60 s (default) and end of data. Fills outside the window are ignored in both
arms.

## A10: clustered discordance test

Orders of one strategy on one day share market conditions, so the direction of disagreement between sim and live is
correlated within a day. Exact McNemar treats orders as independent; in the audit's simulation of a marginally null
day-clustered process it rejected at about 16% at a nominal 5%. Now the primary discordance test is cluster-robust:
Obuchowski's clustered McNemar with session dates as clusters when there are at least 5, else a block bootstrap of
`(c - b) / N`. Plain McNemar is still reported, labelled as assuming independent orders, in the secondary family.
The regression test checks that the primary test rejects at most 7% under the clustered null.

## A11: a ground-truth oracle

Every shadow test checked agreement with the specification or with the predecessor engine, which share its
semantics, so findings A3 to A6 could not have been caught. The repository now has an independent price-display-time
matching engine (`python/tests/oracle/qt_oracle`, driver `scripts/oracle/run_oracle.py`). A separate toy exchange
runs the same order flow twice, without and with the shadow as a real order; the oracle, given only the first run's
tape, must reproduce the shadow's fills in the second run. The oracle is also compared with queuetruth's replay on
the synthetic fixtures. See [How queuetruth is tested](../testing.md#the-independent-audit-and-prep-work).

## Smaller corrections

The audit's minor findings are applied as written unless a rule above overrides them. Verified in the v0.1 code: the
amend delay uses the amend's own jitter draw; `ambiguous_race` also covers fills just before a cancel takes effect;
a removal that arrives after an unpaired fill raises a data-quality warning (`removal_after_unpaired_fill`). Not yet
applied in v0.1: the Hosmer-Lemeshow test of the learned model still uses `g - 2` degrees of freedom on held-out data
(the audit recommends `g`).
