# Your backtester's fill model is off by up to 15%. We measured it on every symbol of a real trading day.

A backtest of a strategy that places limit orders has to guess, for every order, whether it would have been filled:
was the order near the front of the queue at its price, or behind thousands of shares that arrived earlier? Backtesters
answer that with a fill model, a rule or a probability. Nobody can see the real queue in the data most people backtest
on, so these models are rarely checked against it.

Some public data does show the real queue. IEX publishes its full order-by-order feed (DEEP+) for every trading day,
and it may be republished with attribution. From it we can replay the exact first-in, first-out queue of every price
level of every symbol, place simulated orders into it, and see exactly when each one would have filled. Then we can ask
the popular fill models the same question and compare.

We did that for a whole day: IEX, 9 October 2026, every symbol that traded (9,306 of them), 5.8 million simulated
orders. This is the first edition of the Fill-Realism Atlas, and it will be rebuilt for each new trading day.

## What we found

Within 60 seconds of going live, the exact replay of the real queue fills 10.76% of these orders. The fill models
disagree with it in both directions:

- **NautilusTrader's touch fill model** with `prob_fill_on_limit=1` (fill as soon as the market touches your price)
  fills **15% more orders** than the real queue (+1.62 percentage points; 95% interval +1.57 to +1.67). It behaves
  exactly like an order that is always first in the queue: the optimistic bound.
- **hftbacktest's ProbQueue models** fill **7% to 9% fewer** (-0.71 to -1.02 points across 11 settings). Your
  backtest is pessimistic with them.
- **hftbacktest's L3 FIFO model**, which is given the same order-by-order data we replay, fills **15% fewer**
  (-1.62 points). Below we explain why.
- **NautilusTrader's queue_position model** is within **0.05 points** of the exact replay. On this day it is the one
  to use.

The errors are not the same everywhere. Among the 100 most traded symbols, orders fill often (45.67% within 60 s) and
the relative errors are smaller (+4.9% for the touch model, -2.2% to -4.6% for ProbQueue). Among symbols ranked 501 to
1,500 by activity they are largest: +16.2% for the touch model, -9.6% to -13.4% for ProbQueue and -21.2% for the L3
model.

## What it does to P&L

A fill model can be wrong about how many orders fill and still get P&L right, if the fills it gets wrong are worth
nothing. They are not. We marked every fill to the mid price 60 seconds later, per order placed, and compared each
model's P&L with the exact replay's. Among the 100 most traded symbols, the exact replay loses 1.114 ticks per order
(adverse selection: resting orders tend to fill just before the price moves against them). Then:

- hftbacktest's ProbQueue models book **0.32 to 0.38 ticks per order less** than that, and the L3 model **0.64 less**.
  Almost all of it comes from the fills they miss, and the fills they miss are the profitable ones.
- NautilusTrader's touch model books **0.11 ticks per order more**.
- NautilusTrader's queue_position model is within its interval of the exact P&L.

For a strategy that sends thousands of orders a day, a few tenths of a tick per order is the difference between a
strategy that works and one that does not.

## Why hftbacktest's L3 model misses fills

The L3 model tracks every order ahead of yours, like our replay does, so we expected it to agree. With its fills
decided by the exact replay's rules (our `corrected` mode) it agrees to within 0.01 points. The difference is in
hftbacktest's fill rules as released (2.4.4), checked against its source:

1. **A trade-through is judged against the feed's best price, not your order's price.** An order resting at a better
   price than every other order on its side (because it improved the touch, or because the orders ahead of it left)
   is only filled when an execution happens at a price worse than the feed's best. On feeds like IEX and Nasdaq the
   executed order is always at the feed's best, so such an order never fills this way. On our synthetic test days
   this rule explains 91 of the 93 fills the model misses (the IEX day's own breakdown is still to be published).
2. **Hidden executions are ignored.** Executions against non-displayed orders that trade through or at your price
   fill a real order but not a backtest one.

The details, with tests that reproduce each rule, are in {{HFTBACKTEST_ISSUE_URL}}.

## What to do

- **NautilusTrader users:** on order-book data, prefer queue-position tracking (`queue_position=True`) to the touch
  model.
- **hftbacktest users:** the ProbQueue settings closest to the real queue on this day were `power2` with `n=2` and
  `power3` with `n=1`; fit your own on your venue (see
  [calibrate hftbacktest](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/calibrate-hftbacktest.md)).
- **Everyone:** check your own backtest. `pipx run queuetruth demo --yes` places orders in the real queue of a public
  day in about a minute. To score your simulator against your own live fills, see
  [import live logs](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/import-live-logs.md).

## How to check any number

Each unit of work (a few symbols) produces a leaf with the hash of its input, its settings and the digests of its
outputs; the day's numbers sit under one Merkle root, `8d65f7816ce88de7ec3f166260c140565a28fea7a75ba51bd16af16bd15e67b1`.
Anyone can download IEX's public file for the day, re-run any unit with `scripts/scale/verify_unit.py` and check it
against that root. The code is open source (Apache-2.0), runs on a laptop and sends nothing anywhere.

## Limits

- **One day, one venue.** IEX carries a few percent of US equity volume, and its queues are thinner than Nasdaq's.
  The size of each error can differ elsewhere and on other days. The atlas adds a day each trading day, and the
  intervals will then cover days as well as symbols.
- **Simulated orders do not move the market.** They wait in the real queue without changing it, so there is no market
  impact and no reaction from other traders.
- **NautilusTrader's and Databento's models are re-implementations** from their public documentation and the
  behaviour of their released packages; hftbacktest's are reproduced bit for bit from its released code.
- **Markouts are not a strategy.** The P&L above is the mid move after a fill, before fees and rebates. It measures
  what a model's fill mistakes are worth, not what anyone would earn.

## Links

- The atlas: https://sarptandoven.github.io/queuetruth/
- How to read it, how it is made, its limits:
  https://github.com/sarptandoven/queuetruth/blob/main/docs/concepts/fill-realism-atlas.md
- The code: https://github.com/sarptandoven/queuetruth

Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical Data Terms
of Use (https://iextrading.com/iex-historical-data-terms/).
