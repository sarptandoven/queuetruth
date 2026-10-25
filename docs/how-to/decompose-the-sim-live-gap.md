# Decompose the sim-live gap

**Goal:** you know your simulator and your live trading disagree on P&L. Find out where the money went, order by
order and in total, in the buckets a desk reports: fill timing (delay), spread paid, missed passive fills, adverse
selection on fills, partial fills, and fees or rebates. The buckets add up exactly to the gap.

**You need:** what `queuetruth score` needs (a canonical live log, a simulator arm and a dataset with quotes for
markouts). Every `queuetruth score --out DIR` run writes the decomposition to `DIR/score_shortfall.parquet` (table
`qt.score_shortfall`) next to the other scorecard files; there is nothing extra to switch on. Without a dataset the
table is still written, with every order unvalued (there are no quotes to mark fills with).

## What the gap is

For each scored order and each markout horizon `h` of the scorecard (`markout_horizons_ns`, default 50 ms to
300 s), each arm's result is the markout P&L of its fills, net of fees:

```text
R_arm = sum over the arm's fills of  d * (M(t + h) - P) * q * k  -  fee
gap   = R_sim - R_live
```

`d` is +1 for a buy and -1 for a sell, `P` the fill price, `q` the quantity, `t` the fill time, `M` the venue mid of
the prevailing quote (the same `mid` and end-of-data rule as the [markouts](../concepts/scorecard.md#markouts)) and
`k` the currency value of one price unit times one size unit (`10^price_exp * 10^size_exp * multiplier`). The fills
are the ones in `score_fills.parquet`: both arms, inside the evaluation window, maker and taker. A positive gap
means the simulator overstates what live earned: it is the shortfall live paid relative to the simulation. An
unfilled order earns nothing in either arm, so a fill that one arm got and the other did not is valued at its
markout: that is the opportunity cost.

## The buckets

Units are matched first in, first out: the first unit live filled (fills in time order) is paired with the first
unit the simulator filled, and so on up to the smaller of the two filled quantities (`matched_qty`). With `M-` the
mid of the last quote strictly before a fill, the value difference of a matched pair splits the way implementation
shortfall does, with the simulated fill as the benchmark: where the mid stood when each arm filled, what each arm
paid against that mid, and where each fill was marked.

| Bucket | Column | Per matched unit, or per order | What it shows |
|---|---|---|---|
| Delay | `delay_ccy` | `d * (M-_live - M-_sim) * k` | How far the mid moved between the simulator's fill and live's. Positive when it moved against live: a late taker buying after the mid rose pays it here. |
| Spread paid | `spread_ccy` | `d * ((P_live - M-_live) - (P_sim - M-_sim)) * k` | The effective half-spread live paid minus the one the simulator paid. A live taker fill against a simulated maker fill at the same mid costs the full quoted spread; a passive fill earns a negative half-spread. |
| Adverse selection | `adverse_selection_ccy` | `d * (M_sim(t+h) - M_live(t+h)) * k` | The difference of the marks: the mid ran further against the arm marked lower. |
| Missed fills | `missed_fill_ccy` | `R_sim` if live did not fill, `-R_live` if the simulator did not (gross of fees) | Opportunity cost, measured with markouts. A missed toxic fill has a negative markout, so missing it shows as a gain for that arm. |
| Partial fills | `partial_fill_ccy` | the gross markout value of the units beyond `matched_qty`, `+` for the simulator, `-` for live | Both arms filled, in different sizes. |
| Fees | `fees_ccy` | live fees minus simulator fees, every fill | Rebates are negative fees. See [fees](#fees) for how the simulator arm is charged. |

On a matched unit, delay, spread and adverse selection telescope to the unit's value difference
`d * (M_sim(t+h) - P_sim - M_live(t+h) + P_live) * k`, so the six buckets always sum to the gap. The market buckets
are computed as exact integers (half price units times size units; `bid + ask` is always an integer), so the identity
is exact there; the currency columns are those integers converted once, and `total_ccy`, which is computed
separately from each arm's fills, matches their sum to floating-point rounding.

Three edge rules matter when you read the table:

- A unit filled in both arms is never a missed fill: an order that live filled with a taker fill and the simulator
  filled passively lands in spread (and delay, if the fills are at different times), not in missed fills.
- Two passive fills at the same price have no price difference to explain. When they happen at different times,
  delay and spread are equal and opposite (the mid moved between the fills, and the half-spread each arm earned
  against its own mid moved by the same amount), and the whole difference is adverse selection. Read delay and
  spread together for passive orders; they carry the timing, and adverse selection carries the result.
- A late taker fill is delay: if both arms took liquidity at the same effective half-spread and were marked at the
  same mid, the price the mid moved before live arrived is all in delay, with spread and adverse selection 0.

### Fees

The fees bucket uses the fees of `score_fills.parquet`. Live fills carry the fees in the live log. The simulator
arm is charged the live fee rate for its liquidity (average live fee per unit of notional, per instrument, taker or
maker), as in the scorecard's net-of-fee markouts, so:

- an external simulator's own reported fees are not used, and an error in its fee model does not show here;
- per-order fee differences are not zero even for a perfect reproduction when live's per-fill rates vary (fee
  tiers, rounding), because the simulator is charged the average rate;
- a simulated fill whose liquidity live never had (for example a simulated taker fill when every live fill was
  maker) has no rate and counts as 0. On an order with a known fee on some fill, such fills are counted in `note`
  (`N sim fill(s) without a fee counted as 0`) and in the total rows;
- a fee in a currency other than the instrument's is left out and counted in `note`.

## Run it

This uses the synthetic dataset and its 200 order lives of [Getting started](../getting-started.md), with
`hbt_power3[n=2]` as the simulator:

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --out synth
queuetruth score --live synth/own_orders/live --sim-model "hbt_power3[n=2]" --data synth --out sc
```

Read the totals at the 1 s horizon and check the identity on every order:

<!-- check-docs: run -->
```python
import math

import pyarrow.parquet as pq

rows = pq.read_table("sc/score_shortfall.parquet").to_pylist()
buckets = ["delay", "spread", "missed_fill", "adverse_selection", "partial_fill", "fees"]
for r in rows:
    if r["scope"] == "total" and r["horizon_ns"] == 1_000_000_000:
        print(f"{r['statistic']:>6} {r['currency']} n={r['n']}",
              " ".join(f"{b}={r[b + '_ccy']:+.2f}" for b in [*buckets, "total"]))
orders = [r for r in rows if r["scope"] == "order" and r["valued"]]
worst = max(abs(math.fsum(r[b + "_ccy"] for b in buckets) - r["total_ccy"]) for r in orders)
print(f"{len(orders)} order rows, largest |sum of buckets - total| = {worst}")
assert worst < 1e-9
```

On this run (measured 2026-10-24) the 1 s totals over the 184 scored orders are: missed fills -37.50 USD, adverse
selection -62.50, partial fills -56.25, delay, spread and fees 0, total -156.25. The mean per order is -0.85 USD
with a 95% CI of [-1.89, -0.20]. So `hbt_power3[n=2]` understates live here, mostly through fills it missed: 3
orders live filled and the model did not, and 4 it filled short, all on fills that earned money. The
adverse-selection total comes from a single order (one of the 93 that both arms filled) and its CI reaches 0, so it
is not a finding about the model. The identity check prints 0.0 on all 1,288 order rows, and the total rows' `note`
says that 16 of the 200 live orders have no simulator outcome and are not decomposed.

## Reading the table

- **One row per order and horizon** (`scope = order`, `statistic = value`): the six buckets, `total_ccy`,
  `live_qty`, `sim_qty` and `matched_qty` (priced fill quantity in the window, maker and taker), `class` (the
  scorecard's maker-fill class) and `currency`. For bps, `live_notional_ccy` and `sim_notional_ccy` are each arm's
  filled notional (`sum |P| q k`); `notional_ccy` is the order quantity times the limit price of the order's first
  leg, null when that price is 0 or missing (a market order).
- **Four total rows per horizon and currency** (`scope = total`): `sum` and `mean` over the valued orders, and the
  95% percentile CI of the per-order mean (`ci_lo`, `ci_hi`) for each bucket and the total. The CIs use the
  scorecard's clustered block bootstrap with the block kind the scorecard chose over all scored orders
  (session-date blocks with 5 or more dates, else 30-minute blocks; `bootstrap.b` and `bootstrap.seed`), also for a
  horizon or currency whose valued orders span fewer dates. All buckets of one horizon are resampled with the same
  draws, so the buckets of every replicate add up to its total; the CI bounds themselves do not add.
- **Unvalued orders** (`valued = false`): an order is left out of the totals at a horizon when one of its fills,
  maker or taker, has no price (the scorer drops such fills from `score_fills.parquet` and passes their quantity),
  when there is no quote at `t + h` (past the end of data, or an empty side), or when a matched fill has no quote
  before it. The reason is in `note`, and the total rows count them. Without a dataset every order is unvalued.
- **What is not decomposed**: only scored orders (live orders paired with a simulator outcome) are in the table.
  The total rows' `note` counts what is left out, so a reconciliation of total live P&L knows what is missing: live
  fills of orders without a scheduled leg (fills-only logs), and live orders without a simulator outcome.

Pick the horizon the way you pick a markout horizon: short ones (50 ms to 1 s) isolate the spread and the immediate
adverse selection, long ones (60 s, 300 s) include the drift your strategy would hold through. Delay and spread do
not depend on the horizon: they only use the quotes before the fills.

## A worked example

One buy order for 1,000 shares at 100.00, horizon 1 s, prices in cents (`price_exp = -2`), size and multiplier 1.
Quotes (bid / ask): 100.00 / 100.02 from 0 s, 100.00 / 100.01 from 1.5 s, 99.99 / 100.00 from 3.2 s.

| Arm | Fills | Fee |
|---|---|---|
| Simulator | 600 at 1.0 s, 400 at 2.0 s, all at 100.00 | -1.20 and -0.80 (a 0.002 rebate per share) |
| Live | 400 at 3.0 s at 100.00 | -0.80 |

The marks: the simulator's first fill has `M-` = 100.010 and `M(t+1s)` = 100.005, its second 100.005 and 100.005;
live's fill has `M-` = 100.005 and `M(t+1s)` = 99.995. Live filled 400, so the first 400 units of the simulator's
first fill are matched with it.

| Bucket | Computation | USD |
|---|---|---|
| Delay | 400 x (100.005 - 100.010) | -2.00 |
| Spread | 400 x ((100.00 - 100.005) - (100.00 - 100.010)) | +2.00 |
| Adverse selection | 400 x (100.005 - 99.995) | +4.00 |
| Partial fills | 200 x 0.005 + 400 x 0.005, the simulator's 600 extra units | +3.00 |
| Missed fills | both arms filled | 0.00 |
| Fees | -0.80 - (-2.00) | +1.20 |
| **Gap** | R_sim = 5.00 + 2.00 = 7.00; R_live = -2.00 + 0.80 = -1.20 | **+8.20** |

The simulator overstates live by 8.20 USD on this order. On the 400 matched shares, live filled after the spread
had narrowed: the mid it filled against was half a cent lower (delay -2.00, in live's favour), but it earned half a
cent of spread instead of a full cent (spread +2.00). Both arms rested at 100.00, so these two cancel, and the whole
matched difference is the mark: the mid fell a cent under live's fill and not under the simulator's (adverse
selection +4.00). The simulator also filled 600 shares live never got (partial fills, valued at their 1 s markout)
and collected the rebates on them (fees). The test `test_worked_example_of_the_how_to_page` in
`python/tests/score/test_shortfall.py` checks every number in this table, including both arms' results.

## Real orders: RD-11

On the RD-11 real-order sample (3,000 real Nasdaq order lives per day, replayed as a live log, exact arm, 60 s
evaluation window; measured 2026-10-24), every order row is valued and the identity holds on all 21,000 rows of each
run, exactly in integer units and with a largest |sum of buckets - total| of 1.4e-14 USD; `total_ccy` matches the sum
of each arm's `score_markouts.mo_ccy` minus fees to within 1e-12 USD. Totals at the 1 s horizon (sum over 3,000
orders, USD; these logs carry no fees):

| Day | Simulator | delay | spread | missed fills | adverse selection | partial fills | total |
|---|---|---|---|---|---|---|---|
| 2019-12-30 | exact, self-exclusion on | 0 | 0 | 0 | 0 | 0 | 0 |
| 2019-12-30 | exact, self-exclusion off | +1.19 | -1.19 | +44.15 | -1.00 | +36.13 | +79.28 |
| 2019-12-30 | `front_of_queue` | -3.54 | +3.54 | +13.49 | +2.64 | -0.41 | +15.72 |
| 2020-01-30 | exact, self-exclusion on | 0 | 0 | 0 | 0 | 0 | 0 |
| 2020-01-30 | exact, self-exclusion off | +2.98 | -2.98 | +475.12 | -2.90 | +27.45 | +499.67 |
| 2020-01-30 | `front_of_queue` | -22.94 | +22.94 | +13.70 | +6.50 | +0.66 | +20.86 |

With self-exclusion the exact arm reproduces every live fill, so every bucket is exactly zero. Without it, the
simulated order queues behind its own still-resting twin and misses fills live got: the missed-fill bucket is
positive because those fills had negative 1 s markouts on these days, so the simulator looks better by avoiding
them. Every matched pair in these runs is two passive fills at the same price, so delay and spread cancel on every
order (the per-order |delay + spread| sums to 0 in all six runs); read them as the timing signal. Without
self-exclusion the simulator's first fill comes later than live's on 22 (day one) and 12 (day two) jointly filled
orders and never earlier. By the simulator's later fill the mid had moved towards the order's price,
so delay is positive, and spread, the smaller half-spread that later passive fill earned, offsets it.
`front_of_queue`, which assumes nothing is queued ahead of the order (see the
[model zoo](../concepts/l2-model-zoo.md)), fills first on 17 and 19 jointly filled orders and never later, so the
signs flip. What the timing does to the result is in adverse selection, which comes from few orders (1 and 2
orders without self-exclusion, 7 and 7 for `front_of_queue`). The 95% CI of the per-order mean total excludes zero
only at the 50 ms and 100 ms horizons (both days of `front_of_queue`, day two without self-exclusion); at 1 s and
longer every CI includes zero, so read these runs as illustrations of the table, not as findings about the models.

## Limits

- The reference is the venue mid of the dataset (single feed), as for the scorecard's markouts; `microprice` is not
  used here because its value is not an exact multiple of a price unit.
- Fill timing shows in delay (for passive fills, together with the offsetting spread and the adverse selection that
  follows) without asking why the arms filled at different times: queue position, latency and a modelling error
  all show up there. Use the scorecard's queue-decile tests and
  [explain one fill](explain-one-fill.md) to find the cause.
- Units are paired first in, first out within an order: unit k of live's fills against unit k of the simulator's.
  When the simulator misses an early fill and gets a later one, its fill pairs with live's early fill and reads as
  late. Example (a buy at a price of 100, values in half price units): live fills 5 at t1 and 5 at t2, the
  simulator 5 at t2; `2M-` is 200 and 210, `2M(t+h)` is 204 and 230. FIFO gives delay -50, spread +50, adverse
  selection +130 and partial fills -150; pairing by time would give 0, 0, 0 and -20. The total (-20) is the same,
  but the pairing moves value among delay, spread, adverse selection and partial fills
  (`test_fifo_pairing_reads_unit_k_of_the_order`).
- Fees follow the scorecard's fee model (see [fees](#fees)): the simulator is charged live's average rate.
- An order whose fills span two instrument-days is valued at the price scale of its entry day; the scorer refuses
  a scale change within an instrument, so this only matters for datasets that change it.
- Cost: the step runs on every `score --out`. Time and memory grow linearly with orders times horizons. On a
  synthetic log of 20,000 orders with 4 fills per arm and 7 horizons (140,028 rows) it took 9.5 to 13.3 s over
  three runs, and the traced peak memory for 5,000 orders was 66 MB (measured 2026-10-24 on a shared laptop under
  heavy load). A log of a million orders therefore needs minutes and gigabytes for this step alone.
