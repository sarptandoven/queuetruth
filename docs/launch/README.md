# Launch kit: the Fill-Realism Atlas

Drafts for announcing the public atlas (IEX DEEP+, the 20 trading days 2026-10-09 to 2026-11-06). Nothing here is
posted anywhere by the repository; a maintainer edits and posts it. Pages under `docs/launch/` are left out of the
docs site and of the placeholder check.

| file | what it is |
|---|---|
| [atlas-first-edition.md](atlas-first-edition.md) | the long post: what was measured, the results, why, what to do, how to check it |
| [social.md](social.md) | Hacker News, r/algotrading, X and LinkedIn versions |
| [upstream.md](upstream.md) | heads-up notes for hftbacktest and NautilusTrader, to send before the public post |

## Before posting

1. Check that GitHub Pages is on (Settings > Pages > Source: GitHub Actions) and that
   https://sarptandoven.github.io/queuetruth/ shows the 20-day numbers quoted below and the docs at
   https://sarptandoven.github.io/queuetruth/docs/.
2. Replace the per-tier and P&L figures in the long post, which are still from the first day (2026-10-09), with the
   20-day figures from the site, or keep them and keep their one-day label. Fill in `{{ATLAS_ROOT}}` with the root
   the site shows for the window.
3. Send the two upstream notes in [upstream.md](upstream.md) a few days before the public post, so the maintainers
   are not surprised and can correct anything we got wrong. Fill in `{{HFTBACKTEST_ISSUE_URL}}` and
   `{{PUBLISH_DATE}}`.
4. Re-read the limits section of the long post. Every number is from one venue; say so wherever a number is quoted
   without its context.

## The numbers quoted

The 20-day headline, as on README: IEX DEEP+, 2026-10-09 to 2026-11-06, 265,402 symbol-days, 12,440 symbols; the
`standard` shadow population (one lot or the median order size, at the touch or one or two ticks from it), faithful
mode (the libraries as released), orders checked 1 minute after they go live, 95% intervals from a bootstrap over
symbols; symbol-days whose replay checks flagged or that were halted are left out.

| model | fill rate vs exact, all symbols |
|---|---|
| exact replay | 11.97% fill |
| NautilusTrader touch model, `prob_fill_on_limit=1` | +1.50 pp (13% more orders; 95% interval +1.45 to +1.55) |
| NautilusTrader queue_position model | +0.06 pp (closest) |
| hftbacktest L3 FIFO (order-level input) | -1.82 pp |

Detail from the first day only (2026-10-09, 9,306 symbols, 5,783,416 orders, exact fill rate 10.76%), until it is
replaced with 20-day figures:

| model | top 100 symbols, fill rate vs exact | ticks of P&L per order vs exact, top 100 |
|---|---|---|
| exact replay | 45.67% fill | -1.114 per order |
| NautilusTrader touch model, `prob_fill_on_limit=1` | +2.25 pp (+4.9%) | +0.106 |
| NautilusTrader queue_position model | -0.09 pp | -0.049 (not clear of 0) |
| hftbacktest ProbQueue (11 settings) | -1.03 to -2.10 pp | -0.322 to -0.377 |
| hftbacktest L3 FIFO (order-level input) | -2.99 pp (-6.6%) | -0.639 |
| Databento tutorial estimator | -1.67 to -1.77 pp | -0.342 to -0.344 |

Why hftbacktest's L3 FIFO model fills fewer orders: it is hftbacktest's own behaviour, not an error of queuetruth's
replica. The released hftbacktest 2.4.4 gives the same first fill as the replica on all 117,356 orders of the
findings run. It skips fills that carry no side, the hidden executions (about 3 in 4 of the misses on IEX, where
hidden executions outnumber displayed ones about 5 to 1), and it counts a trade-through only through the feed's best
price, so an order alone ahead of the displayed book misses those fills. The hidden-execution share rests on
queuetruth's ruling that displayed interest fills before hidden interest at the same price. Evidence:
https://github.com/sarptandoven/queuetruth/blob/main/scripts/scale/findings/l3-fifo-gap.md
