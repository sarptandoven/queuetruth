# Social drafts

Short versions of [the long post](atlas-first-edition.md). Every number is from IEX on 2026-10-09, 60 s after an order
goes live; keep that context when you shorten them further.

## Hacker News

Title (80 characters at most):

> Show HN: We replayed a full day of real order queues to check backtest fill models

Text:

> Backtests of limit-order strategies have to guess whether each order would have filled. IEX publishes its full
> order-by-order feed, so the real queue can be replayed exactly. We placed 5.8 million simulated orders into the
> queues of every symbol that traded on IEX on 9 October 2026 (9,306 symbols) and compared the exact fills with the
> fill models of NautilusTrader, hftbacktest and a Databento tutorial.
>
> NautilusTrader's touch model fills 15% more orders than the real queue; hftbacktest's ProbQueue models fill 7 to 9%
> fewer and its L3 model 15% fewer; NautilusTrader's queue-position model is within 0.05 percentage points. In P&L,
> among the 100 most traded symbols, hftbacktest's models book 0.3 to 0.6 ticks per order less than the real queue,
> mostly because the fills they miss are the profitable ones.
>
> Everything is open source (Apache-2.0) and every number can be re-run from IEX's public file and checked against a
> published Merkle root. The atlas rebuilds for each trading day. Limits: one day, one venue, no market impact.
>
> https://sarptandoven.github.io/queuetruth/

## r/algotrading

Title:

> I replayed every symbol's real order queue on IEX for a day to see how wrong backtest fill models are

Body: the Hacker News text above, then:

> If you backtest limit orders with NautilusTrader, the touch model with prob_fill_on_limit=1 is the optimistic bound
> (it fills like you are always first in the queue); queue-position tracking was within 0.05 pp of the real queue on
> this day. With hftbacktest, power2 (n=2) and power3 (n=1) were the closest ProbQueue settings. To check your own
> backtest: `pipx run queuetruth demo --yes`.
>
> Happy to answer questions on the method. The how-to-read page lists the limits:
> https://github.com/sarptandoven/queuetruth/blob/main/docs/concepts/fill-realism-atlas.md

## X (thread)

1. How wrong is your backtester's fill model? We replayed the real order queue of every symbol on IEX for a full day
   (9,306 symbols, 5.8M simulated orders) and compared. Thread, with the data and the code:
2. Within 60 s, the real queue fills 10.76% of the orders. NautilusTrader's touch model fills 15% more. hftbacktest's
   ProbQueue models fill 7 to 9% fewer. Its L3 model, given the same order-level data, 15% fewer.
3. The closest: NautilusTrader's queue-position model, within 0.05 percentage points.
4. It matters in money. Among the 100 most traded symbols, hftbacktest's models book 0.3 to 0.6 ticks per order less
   P&L than the real queue: the fills they miss are the profitable ones.
5. Why the L3 model misses fills: 58% are trade-throughs checked against the feed's best price instead of your
   order's price; 42% are executions against hidden orders, which it ignores. Details went to the maintainers.
6. Every number re-runs from IEX's public file and checks against a published Merkle root. Open source, rebuilt each
   trading day. One day, one venue so far. https://sarptandoven.github.io/queuetruth/

## LinkedIn

> Backtests of limit-order strategies guess whether each order would have filled. We checked those guesses against
> the real order queue of every symbol on IEX for a full trading day: the popular fill models are off by up to 15% in
> fill rate, and by 30% to 57% of the real P&L per order among the most traded symbols. The results, the method
> and the code are public, and every number can be re-run and verified:
> https://sarptandoven.github.io/queuetruth/
