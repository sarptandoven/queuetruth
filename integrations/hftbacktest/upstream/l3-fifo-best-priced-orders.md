# Draft: L3FIFOQueueModel misses fills of an order priced better than every displayed order

Status: **draft, not filed.** To open as an issue on https://github.com/nkaz001/hftbacktest after the maintainers of
queuetruth agree. The evidence is in `scripts/scale/findings/l3-fifo-gap.md`. It supersedes the hftbacktest part of
`docs/launch/upstream.md`, which predates the run on the released hftbacktest.

Title: `L3FIFOQueueModel: an order priced better than every displayed order fills only on a cross`

> Hi, we maintain queuetruth, which replays order-level feeds exactly and compares backtest fill models with the
> exact FIFO queue. With `L3FIFOQueueModel` and the L3 no-partial-fill exchange (hftbacktest 2.4.4 from PyPI; master
> `5f3ec40` has the same fill code), resting orders fill less often than in an exact replay of the same Nasdaq ITCH
> or IEX DEEP+ records. Every fill it misses is of a backtest order that is better than every displayed order on its
> side: it improved the touch, or the orders at and ahead of its level left. Two rules cause it.
>
> **1. Trade-through is judged against the feed's best price, not the backtest order's price.** In
> `fill_market_feed_order` (`queue.rs` line 1000 at `5f3ec40`, line 992 at `py-v2.4.4`), backtest bids above the
> executed feed order are filled only `if exec_price_tick < depth.best_bid_tick()` (asks likewise, line 1041). The
> comment says the fill event should come before the cancel that may update the best price. In ITCH and DEEP+ each
> execution message removes its own order, so in a sweep the earlier order is already gone when the next one
> executes, and the executed order is always at the feed's best. A backtest bid priced above the feed's best then
> never fills by trade-through; it fills only when the ask crosses it.
>
> Example: feed bids 5 @ 100 (o1) and 5 @ 99 (o2), backtest bid @ 100 behind o1. A seller sweeps both: o1 executes
> at 100 and is removed, then o2 executes at 99. At o2's fill the feed's best bid is 99, so `99 < 99` is false and the
> backtest bid stays unfilled, although the seller passed it.
>
> **2. Fills without a side are skipped.** `L3NoPartialFillExchange::process` handles a fill only
> `if event.is(BUY_EVENT) || event.is(SELL_EVENT)` (`l3_nopartialfillexchange.rs` line 394, `todo: handle properly if
> no side is provided`). Hidden executions on Nasdaq and IEX carry no side and no order id. When nothing displayed
> is at or through the print's price, a displayed backtest order at that price (or better) would have had priority
> over the hidden order that traded.
>
> **How we checked.** We ran hftbacktest 2.4.4 itself on 117,356 simulated one-lot GTX orders (zero latency) over the
> records of 12 symbols of one IEX DEEP+ day and of 4 symbols of a 30-minute Nasdaq ITCH window, and compared each
> order's first fill with an exact FIFO replay. Within 60 s, the exact replay fills 2.78 percentage points more of
> the IEX orders (2,670 orders) and 1.17 points more of the Nasdaq orders (283). Rule 1 explains 25% of the IEX
> misses and 40% of the Nasdaq ones, rule 2 the rest; nothing else. hftbacktest fills about two thirds of these
> orders later, mostly when the opposite side crosses their price, so passive P&L comes out too low as well as the
> fill count. Each case is reproduced on hand-built books in queuetruth's
> `crates/zoo/tests/l3_faithful_gap.rs`.
>
> **Possible fixes.** For rule 1: fill every backtest bid priced above the execution price, whatever the feed's best,
> for example `fill_bid_between::<true>(INVALID_MAX, exec_price_tick + 1)` in place of the `best_bid_tick()` branch
> (asks likewise); this also makes the result independent of whether a feed sends the cancel before or after the
> fill. For rule 2: an opt-in handling of side-less fills that fills backtest orders priced through the print, and
> at the print's price once no market-feed order is ahead of them in the queue. We are happy to send a pull request
> with tests if that matches your intent.
