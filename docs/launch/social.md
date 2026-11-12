# Social drafts

Short versions of [the long post](atlas-first-edition.md). The headline numbers are from IEX over 20 trading days
(2026-10-09 to 2026-11-06), 1 minute after an order goes live; keep that context when you shorten them further. Values
marked `{{...}}` come from the site before posting (see [the launch kit](README.md)).

## Hacker News

Title (80 characters at most):

> Show HN: We replayed 20 days of real order queues to check backtest fill models

Text:

> Backtests of limit-order strategies have to guess whether each order would have filled. IEX publishes its full
> order-by-order feed, so the real queue can be replayed exactly. We placed simulated orders into the queues of every
> symbol that traded on IEX over 20 trading days (265,402 symbol-days, 12,440 symbols) and compared the exact fills
> with the fill models of NautilusTrader, hftbacktest and a Databento tutorial.
>
> Within a minute the real queue fills 11.97% of the orders. NautilusTrader's touch model fills 13% more; hftbacktest's
> L3 model 15% fewer, because it ignores hidden executions and judges trade-throughs against the feed's best price (the
> released hftbacktest does exactly this); NautilusTrader's queue-position model is within 0.06 percentage points.
> {{PNL_SENTENCE_20D}}
>
> Everything is open source (Apache-2.0) and every number can be re-run from IEX's public files and checked against a
> published Merkle root. The atlas rebuilds for each trading day. Limits: one venue, no market impact.
>
> https://sarptandoven.github.io/queuetruth/

## r/algotrading

Title:

> I replayed every symbol's real order queue on IEX for 20 days to see how wrong backtest fill models are

Body: the Hacker News text above, then:

> If you backtest limit orders with NautilusTrader, the touch model with prob_fill_on_limit=1 is the optimistic bound
> (it fills like you are always first in the queue); queue-position tracking was within 0.06 pp of the real queue. With
> hftbacktest's L3 model, expect it to miss fills of orders that improve the touch. To check your own backtest:
> `pipx run queuetruth demo --yes`, and the guide for your backtester: https://sarptandoven.github.io/queuetruth/docs/
>
> Happy to answer questions on the method. The how-to-read page lists the limits:
> https://github.com/sarptandoven/queuetruth/blob/main/docs/concepts/fill-realism-atlas.md

## X (thread)

1. How wrong is your backtester's fill model? We replayed the real order queue of every symbol on IEX for 20 trading
   days (265,402 symbol-days) and compared. Thread, with the data and the code:
2. Within 1 minute, the real queue fills 11.97% of the orders. NautilusTrader's touch model fills 13% more.
   hftbacktest's L3 model, given the same order-level data, 15% fewer. {{PROBQUEUE_SENTENCE_20D}}
3. The closest: NautilusTrader's queue-position model, within 0.06 percentage points.
4. It matters in money. {{PNL_SENTENCE_20D}}
5. Why the L3 model misses fills: it checks trade-throughs against the feed's best price instead of your order's
   price, and it ignores executions against hidden orders. The released hftbacktest does the same on every order we
   traced; on IEX, hidden executions are three quarters of the gap.
6. Every number re-runs from IEX's public files and checks against a published Merkle root. Open source, rebuilt each
   trading day. One venue so far. https://sarptandoven.github.io/queuetruth/

## LinkedIn

> Backtests of limit-order strategies guess whether each order would have filled. We checked those guesses against
> the real order queue of every symbol on IEX over 20 trading days: the popular fill models are off by up to 15% in
> fill rate. {{PNL_SENTENCE_20D}} The results, the method and the code are public, and every number can be re-run and
> verified: https://sarptandoven.github.io/queuetruth/
