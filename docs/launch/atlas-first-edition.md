# Your backtester's fill model is off by up to 15%. We measured it on 20 trading days of real order queues.

A backtest of a strategy that places limit orders has to guess, for every order, whether it would have been filled:
was the order near the front of the queue at its price, or behind thousands of shares that arrived earlier? Backtesters
answer that with a fill model, a rule or a probability. Nobody can see the real queue in the data most people backtest
on, so these models are rarely checked against it.

Some public data does show the real queue. IEX publishes its full order-by-order feed (DEEP+) for every trading day,
and it may be republished with attribution. From it we can replay the exact first-in, first-out queue of every price
level of every symbol, place simulated orders into it, and see exactly when each one would have filled. Then we can ask
the popular fill models the same question and compare.

We did that for 20 trading days: IEX, 9 October to 6 November 2026, every symbol that traded (265,402 symbol-days of
12,440 symbols). This is the Fill-Realism Atlas, and it is rebuilt for each new trading day.

## What we found

Within 1 minute of going live, the exact replay of the real queue fills 11.97% of these orders. The fill models
disagree with it in both directions:

- **NautilusTrader's touch fill model** with `prob_fill_on_limit=1` (fill as soon as the market touches your price)
  fills **13% more orders** than the real queue (+1.50 percentage points; 95% interval +1.45 to +1.55). It behaves
  like an order that is always first in the queue: the optimistic bound.
- **hftbacktest's L3 FIFO model**, which is given the same order-by-order data we replay, fills **15% fewer**
  (-1.82 points). Below we explain why: it is how hftbacktest itself behaves.
- **hftbacktest's ProbQueue models** fill {{PROBQUEUE_RANGE_20D}} fewer. Your backtest is pessimistic with them.
- **NautilusTrader's queue_position model** is within **0.06 points** of the exact replay. It is the one to use.

The errors are not the same everywhere. On the first day (2026-10-09), among the 100 most traded symbols orders filled
often (45.67% within 60 s) and the relative errors were smaller (+4.9% for the touch model, -2.2% to -4.6% for
ProbQueue). Among symbols ranked 501 to 1,500 by activity they were largest: +16.2% for the touch model, -9.6% to
-13.4% for ProbQueue and -21.2% for the L3 model. {{TIER_NUMBERS_20D}}

## What it does to P&L

A fill model can be wrong about how many orders fill and still get P&L right, if the fills it gets wrong are worth
nothing. They are not. We marked every fill to the mid price 60 seconds later, per order placed, and compared each
model's P&L with the exact replay's. On the first day, among the 100 most traded symbols, the exact replay lost 1.114
ticks per order (adverse selection: resting orders tend to fill just before the price moves against them). Then:

- hftbacktest's ProbQueue models booked **0.32 to 0.38 ticks per order less** than that, and the L3 model **0.64
  less**. Almost all of it comes from the fills they miss, and the fills they miss are the profitable ones.
- NautilusTrader's touch model booked **0.11 ticks per order more**.
- NautilusTrader's queue_position model was within its interval of the exact P&L.

{{PNL_NUMBERS_20D}}

For a strategy that sends thousands of orders a day, a few tenths of a tick per order is the difference between a
strategy that works and one that does not.

## Why hftbacktest's L3 model misses fills

The L3 model tracks every order ahead of yours, like our replay does, so we expected it to agree. We ran the released
hftbacktest 2.4.4 itself on the same records and the same orders, one IEX day (12 symbols) and the Nasdaq demo
window: it gives the same first fill as our replica on all 117,356 orders. So the gap is hftbacktest's own behaviour.
Two of its rules cause every missed fill:

1. **A trade-through is judged against the feed's best price, not your order's price.** An order resting at a better
   price than every displayed order on its side (because it improved the touch, or because the orders ahead of it
   left) is only filled when an execution happens at a price worse than the feed's best. On feeds like IEX and Nasdaq
   the executed order is always at the feed's best, so such an order fills only when the price crosses it.
2. **Hidden executions are ignored.** Executions against non-displayed orders at or through your price fill a real
   order but not a backtest one. On IEX, hidden executions outnumber displayed ones several times over, and they are
   three quarters of the gap.

Most of the orders it misses do fill later in hftbacktest, after the price has moved through them: the backtest swaps
a fill inside the spread for a fill on an adverse move, which is where the lower P&L comes from.

The trace, its cause shares and how to rerun it:
https://github.com/sarptandoven/queuetruth/blob/main/scripts/scale/findings/l3-fifo-gap.md. Our note to the
hftbacktest maintainers: {{HFTBACKTEST_ISSUE_URL}}.

## What to do

- **NautilusTrader users:** on order-book data, prefer queue-position tracking (`queue_position=True`) to the touch
  model.
- **hftbacktest users:** fit the ProbQueue setting to your venue (see
  [calibrate hftbacktest](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/calibrate-hftbacktest.md)),
  and expect the L3 model to miss fills of orders that improve the touch.
- **Everyone:** check your own backtest. `pipx run queuetruth demo --yes` places orders in the real queue of a public
  day in about a minute. The docs, with one guide per backtester: https://sarptandoven.github.io/queuetruth/docs/

## How to check any number

Each unit of work (a few symbols) produces a leaf with the hash of its input, its settings and the digests of its
outputs; each day's numbers sit under one Merkle root, which the site lists for every day ({{ATLAS_ROOT}}). Anyone can download IEX's
public file for a day, re-run any unit with `scripts/scale/verify_unit.py` and check it against that root. The code is
open source (Apache-2.0), runs on a laptop and sends nothing anywhere.

## Limits

- **One venue.** IEX carries a few percent of US equity volume, has a speed bump and repricing order types, and its
  queues are thinner than Nasdaq's. The size of each error can differ elsewhere.
- **Simulated orders do not move the market.** They wait in the real queue without changing it, so there is no market
  impact and no reaction from other traders.
- **NautilusTrader's and Databento's models are re-implementations** from their public documentation and the
  behaviour of their released packages; hftbacktest's are reproduced from its released code and checked against the
  released hftbacktest.
- **Markouts are not a strategy.** The P&L above is the mid move after a fill, before fees and rebates. It measures
  what a model's fill mistakes are worth, not what anyone would earn.

## Links

- The atlas: https://sarptandoven.github.io/queuetruth/
- The docs: https://sarptandoven.github.io/queuetruth/docs/
- How to read the atlas, how it is made, its limits:
  https://github.com/sarptandoven/queuetruth/blob/main/docs/concepts/fill-realism-atlas.md
- The code: https://github.com/sarptandoven/queuetruth

Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical Data Terms
of Use (https://iextrading.com/iex-historical-data-terms/).
