# Social drafts

Short versions of [the long post](atlas-first-edition.md). Every number is from IEX DEEP+ over the 20 trading days
2026-10-09 to 2026-11-06, 1 minute after an order goes live, unless it says otherwise; keep that context when you
shorten them further.

## Hacker News

Title (80 characters at most):

> Show HN: We replayed 20 days of real order queues to check backtest fill models

Text:

> Backtests of limit-order strategies have to guess whether each order would have filled. IEX publishes its full
> order-by-order feed, so the real queue can be replayed exactly. We placed simulated orders into the queues of
> every symbol that traded on IEX over 20 trading days (12,440 symbols, 265,402 symbol-days) and compared the exact
> fills with the fill models of NautilusTrader, hftbacktest and a Databento tutorial.
>
> Within 1 minute the real queue fills 11.97% of small resting orders. NautilusTrader's touch model fills 13% more
> (+1.50 percentage points); hftbacktest's L3 FIFO model, given the same order-level data, fills 1.82 points fewer,
> because it ignores hidden executions and judges trade-throughs against the feed's best price (we checked that the
> released hftbacktest does exactly this). NautilusTrader's queue-position model is within 0.06 points.
>
> To check your own backtest: `queuetruth check BACKTEST --data DATASET` replays its resting orders in the real
> queue and says how many of its fills were real. Everything is open source (Apache-2.0) and every number can be
> re-run from IEX's public files and checked against a published Merkle root. The atlas rebuilds for each trading
> day. Limits: one venue, no market impact.
>
> https://sarptandoven.github.io/queuetruth/

## r/algotrading

Title:

> I replayed every symbol's real order queue on IEX for 20 days to see how wrong backtest fill models are

Body: the Hacker News text above, then:

> If you backtest limit orders with NautilusTrader, the touch model with prob_fill_on_limit=1 is the optimistic bound
> (it fills like you are always first in the queue); queue-position tracking was within 0.06 pp of the real queue.
> With hftbacktest's L3 FIFO model, expect too few fills for orders that improve the touch or are left alone at the
> front. To try it with no data of your own: `pipx run queuetruth demo --yes`.
>
> Happy to answer questions on the method. The how-to-read page lists the limits:
> https://sarptandoven.github.io/queuetruth/docs/concepts/fill-realism-atlas/

## X (thread)

1. How wrong is your backtester's fill model? We replayed the real order queue of every symbol on IEX for 20 trading
   days (12,440 symbols) and compared. Thread, with the data and the code:
2. Within 1 minute, the real queue fills 11.97% of small resting orders. NautilusTrader's touch model fills 13% more.
   hftbacktest's L3 model, given the same order-level data, 1.82 points fewer.
3. The closest: NautilusTrader's queue-position model, within 0.06 percentage points.
4. Why the L3 model misses fills: it skips executions against hidden orders (about 3 in 4 of the misses on IEX) and
   checks trade-throughs against the feed's best price instead of your order's price. The released hftbacktest
   gives the same fills as our replica on all 117,356 orders we checked.
5. Check your own backtest: `queuetruth check BACKTEST --data DATASET` replays its orders in the real queue and says
   which fills were phantom.
6. Every number re-runs from IEX's public files and checks against a published Merkle root. Open source, rebuilt each
   trading day. One venue so far. https://sarptandoven.github.io/queuetruth/

## LinkedIn

> Backtests of limit-order strategies guess whether each order would have filled. We checked those guesses against
> the real order queue of every symbol on IEX over 20 trading days: the popular fill models are off by up to 15% in
> fill rate, in both directions. One command now checks your own backtest against the real queue. The results, the
> method and the code are public, and every number can be re-run and verified:
> https://sarptandoven.github.io/queuetruth/
