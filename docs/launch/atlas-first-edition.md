# Your backtester's fill model is off by up to 15%. We measured it on 20 days of real order queues.

A backtest of a strategy that places limit orders has to guess, for every order, whether it would have been filled:
was the order near the front of the queue at its price, or behind thousands of shares that arrived earlier? Backtesters
answer that with a fill model, a rule or a probability. Nobody can see the real queue in the data most people backtest
on, so these models are rarely checked against it.

Some public data does show the real queue. IEX publishes its full order-by-order feed (DEEP+) for every trading day,
and it may be republished with attribution. From it we can replay the exact first-in, first-out queue of every price
level of every symbol, place simulated orders into it, and see exactly when each one would have filled. Then we can ask
the popular fill models the same question and compare.

We did that for 20 trading days: IEX, 9 October to 6 November 2026, every symbol that traded (12,440 symbols,
265,402 symbol-days). This is the Fill-Realism Atlas, and it is rebuilt after each new trading day.

## What we found

Within 1 minute of going live, the exact replay of the real queue fills 11.97% of these orders. The fill models
disagree with it in both directions:

- **NautilusTrader's touch fill model** with `prob_fill_on_limit=1` (fill as soon as the market touches your price)
  fills **13% more orders** than the real queue (+1.50 percentage points; 95% interval +1.45 to +1.55). It behaves
  exactly like an order that is always first in the queue: the optimistic bound.
- **hftbacktest's L3 FIFO model**, which is given the same order-by-order data we replay, fills **1.82 points
  fewer**. Below we explain why: it is hftbacktest's own rules, not a modelling error on our side.
- **NautilusTrader's queue_position model** is within **0.06 points** of the exact replay. It is the one to use.
- **hftbacktest's ProbQueue models** fill fewer orders than the real queue: on the first day, 7% to 9% fewer (-0.71
  to -1.02 points across 11 settings). Your backtest is pessimistic with them.

The errors are not the same everywhere. On the first day, among the 100 most traded symbols, orders filled often
(45.67% within 1 minute) and the relative errors were smaller (+4.9% for the touch model, -2.2% to -4.6% for
ProbQueue). Among symbols ranked 501 to 1,500 by activity they were largest: +16.2% for the touch model, -9.6% to
-13.4% for ProbQueue and -21.2% for the L3 model. The site has these tiers for all 20 days.

## What it does to P&L

A fill model can be wrong about how many orders fill and still get P&L right, if the fills it gets wrong are worth
nothing. They are not. We marked every fill to the mid price 1 minute later, per order placed, and compared each
model's P&L with the exact replay's. On the first day, among the 100 most traded symbols, the exact replay lost 1.114
ticks per order (adverse selection: resting orders tend to fill just before the price moves against them). Then:

- hftbacktest's ProbQueue models booked **0.32 to 0.38 ticks per order less** than that, and the L3 model **0.64
  less**. Almost all of it comes from the fills they miss, and the fills they miss are the profitable ones.
- NautilusTrader's touch model booked **0.11 ticks per order more**.
- NautilusTrader's queue_position model was within its interval of the exact P&L.

For a strategy that sends thousands of orders a day, a few tenths of a tick per order is the difference between a
strategy that works and one that does not.

## Why hftbacktest's L3 model misses fills

The L3 model tracks every order ahead of yours, like our replay does, so we expected it to agree. With its fills
decided by the exact replay's rules (our `corrected` mode) it agrees exactly. To rule out an error in our replica, we
ran the released hftbacktest 2.4.4 itself on the same orders and the same IEX and Nasdaq records: it gives the same
first fill as our replica on all 117,356 orders. So the gap is hftbacktest's own behaviour, and it comes from two of
its fill rules:

1. **Hidden executions are ignored.** hftbacktest skips a fill that carries no side. Executions against
   non-displayed orders carry none, yet they fill a real order resting at their price once nothing displayed is
   ahead of it, or through its price. On IEX, hidden executions outnumber displayed ones about 5 to 1, and they are
   about 3 in 4 of the misses. (That share rests on our ruling that displayed interest fills before hidden interest
   at the same price.)
2. **A trade-through is judged against the feed's best price, not your order's price.** On feeds like IEX and
   Nasdaq the executed order is always at the feed's best, so an order priced better than every displayed order on
   its side (because it improved the touch, or because the orders at its level left) never fills this way.

Every missed fill is of an order alone ahead of the displayed book. hftbacktest fills most of them later, when the
opposite side crosses their price, so it swaps a fill inside the spread for a fill after an adverse move: its P&L is
too low as well as its fill count. For orders that stay inside the displayed queue its fills agree with the real
queue order by order.

The details, with tests that reproduce each rule:
https://github.com/sarptandoven/queuetruth/blob/main/scripts/scale/findings/l3-fifo-gap.md, and our note to
hftbacktest: {{HFTBACKTEST_ISSUE_URL}}.

## What to do

- **Check your own backtest in one command.** `queuetruth check BACKTEST --data DATASET` replays your backtest's
  own resting limit orders in the real queue of an order-level dataset (a free IEX day works) and tells you how many
  of its fills were real: hftbacktest, NautilusTrader, Backtrader, VectorBT, LEAN and Freqtrade outputs are read
  as they are. See
  [check my backtest](https://sarptandoven.github.io/queuetruth/docs/how-to/check-my-backtest/).
- **No data yet?** `pipx run queuetruth demo --yes` places orders in the real queue of a public day in about a
  minute.
- **NautilusTrader users:** on order-book data, prefer queue-position tracking (`queue_position=True`) to the touch
  model.
- **hftbacktest users:** the ProbQueue settings closest to the real queue on the first day were `power2` with `n=2`
  and `power3` with `n=1`; fit your own on your venue (see
  [calibrate hftbacktest](https://sarptandoven.github.io/queuetruth/docs/how-to/calibrate-hftbacktest/)). With the
  L3 FIFO model, expect too few fills for orders that improve the touch or are left alone at the front.
- **Live traders:** to score your simulator against your own live fills, see
  [import live logs](https://sarptandoven.github.io/queuetruth/docs/how-to/import-live-logs/).

## How to check any number

Each unit of work (a few symbols) produces a leaf with the hash of its input, its settings and the digests of its
outputs; the window's numbers sit under one Merkle root, `{{ATLAS_ROOT}}`. Anyone can download IEX's public file for
a day, re-run any unit with `scripts/scale/verify_unit.py` and check it against that root. The code is open source
(Apache-2.0), runs on a laptop and sends nothing anywhere.

## Limits

- **One venue.** IEX carries a few percent of US equity volume, and its queues are thinner than Nasdaq's; it also
  has far more hidden executions. The size of each error can differ elsewhere. The intervals cover symbols; the
  atlas adds a day each trading day.
- **Simulated orders do not move the market.** They wait in the real queue without changing it, so there is no market
  impact and no reaction from other traders.
- **NautilusTrader's and Databento's models are re-implementations** from their public documentation and the
  behaviour of their released packages; hftbacktest's are reproduced bit for bit from its released code, and its L3
  model was checked against the released package order by order.
- **Markouts are not a strategy.** The P&L above is the mid move after a fill, before fees and rebates. It measures
  what a model's fill mistakes are worth, not what anyone would earn.

## Links

- The atlas: https://sarptandoven.github.io/queuetruth/
- The docs: https://sarptandoven.github.io/queuetruth/docs/
- How to read the atlas, how it is made, its limits:
  https://sarptandoven.github.io/queuetruth/docs/concepts/fill-realism-atlas/
- The code: https://github.com/sarptandoven/queuetruth

Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical Data Terms
of Use (https://iextrading.com/iex-historical-data-terms/).
