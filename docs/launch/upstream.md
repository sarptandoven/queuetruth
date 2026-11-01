# Upstream notes

To send a few days before the public post, so each project hears it from us first and can correct anything we got
wrong. Edit the tone to your own; keep the numbers with their context.

## hftbacktest: L3 FIFO trade-through judged against the feed's best price

Repository: https://github.com/nkaz001/hftbacktest (open as an issue).

Title: `L3FIFOQueueModel: an order priced better than the feed's best never fills by trade-through`

> Hi, we maintain queuetruth, which replays order-level feeds exactly and compares backtest fill models with the exact
> FIFO queue. On a full IEX DEEP+ day (2026-10-09, 9,306 symbols, 5.8M simulated orders, 60 s after each goes live),
> `L3FIFOQueueModel` with `L3NoPartialFillExchange` (hftbacktest 2.4.4, commit 5f3ec40) fills 15% fewer orders than
> the exact replay (-1.62 percentage points; 95% interval -1.68 to -1.56).
>
> Two rules account for every missed fill we found. The first: in `L3FIFOQueueModel::fill_market_feed_order`
> (`queue.rs`, around line 1000) better-priced
> backtest bids are only filled `if exec_price_tick < depth.best_bid_tick()` (asks likewise), that is when the
> execution is through the feed's best price rather than through the backtest order's price. The source comment says
> this assumes "the fill event should occur before the cancel event which may update the best price". On ITCH and
> DEEP+ the executed order is removed by its own execution message, so the executed order is always at the feed's
> best, and a backtest order priced better than every feed order on its side (it improved the touch, or the orders at
> and above its price left) is never filled by a trade-through. The second: `L3NoPartialFillExchange` does not process
> executions against hidden orders, which fill a real order resting at or behind their price.
>
> On a sample of the IEX day (26 of 130 work units, 1,165,575 orders), the model misses 18,915 fills the exact replay
> makes (1.62% of orders): 58% are trade-throughs under the first rule (most of them orders at the touch whose level
> was swept or emptied ahead of them) and 42% are fills from the queue at the order's price, which for this model can
> only come from hidden executions. It never fills an order the exact replay does not.
>
> Tests that reproduce each case on hand-built books are in queuetruth's `crates/zoo/tests/l3_faithful_gap.rs`, and
> the analysis is in `docs/concepts/l2-model-zoo.md` section (c). Comparing the execution price with the backtest
> order's own price looks like the fix for the first rule, and handling hidden executions in the exchange for the
> second; we are happy to send a PR if that matches your intent. We also still have the
> fix for #312 ready (`integrations/hftbacktest/upstream/` in our repository).
>
> We plan to publish these numbers on {{PUBLISH_DATE}} and would like to quote your view, or a fix, alongside them.

## NautilusTrader: the touch model is the optimistic bound

Repository: https://github.com/nautechsystems/nautilus_trader (a discussion fits better than an issue: this is
documented behaviour, not a bug). The documentation change we prepared earlier is in
`integrations/nautilus/UPSTREAM.md`.

> Hi, we maintain queuetruth, which replays order-level feeds exactly and compares backtest fill models with the exact
> FIFO queue. On a full IEX DEEP+ day (2026-10-09, 9,306 symbols, 5.8M simulated orders, 60 s after each goes live),
> our re-implementation of the default fill model with `prob_fill_on_limit=1` fills 15% more orders than the exact
> replay (+1.62 percentage points; 95% interval +1.57 to +1.67), exactly like an order that is always first in the
> queue. Queue-position tracking was within 0.05 points. In P&L, among the 100 most traded symbols, the touch model
> books 0.11 ticks per order more than the exact replay.
>
> Our models are re-implemented from your documentation and the behaviour of the released package, so please tell us
> if any of it misrepresents NautilusTrader. A sentence in the fill model docs suggesting queue-position tracking for
> order-book backtests of passive orders would help users; the prepared text is linked above.
>
> We plan to publish these numbers on {{PUBLISH_DATE}}.
