# Launch kit: the Fill-Realism Atlas, 20-day edition

Drafts for announcing the public atlas (IEX DEEP+, the 20 trading days 2026-10-09 to 2026-11-06). Nothing here is
posted anywhere by the repository; a maintainer edits and posts it. Pages under `docs/launch/` are left out of the docs
site and of the placeholder check.

| file | what it is |
|---|---|
| [atlas-first-edition.md](atlas-first-edition.md) | the long post: what was measured, the results, why, what to do, how to check it |
| [social.md](social.md) | Hacker News, r/algotrading, X and LinkedIn versions |
| [upstream.md](upstream.md) | heads-up notes for hftbacktest and NautilusTrader, to send before the public post |

## Before posting

1. Turn on GitHub Pages (Settings > Pages > Source: GitHub Actions) and run `.github/workflows/atlas.yml` once by
   hand with `site_only`. Check that https://sarptandoven.github.io/queuetruth/ shows the numbers below and that the
   docs are at https://sarptandoven.github.io/queuetruth/docs/.
2. Fill the marked values from the site: the day roots (`{{ATLAS_ROOT}}`), and the 20-day values the drafts still
   mark (`{{...}}`): the hftbacktest ProbQueue and Databento ranges, the tier and P&L figures. Until then the drafts
   quote those from the first day, 2026-10-09, and say so.
3. Send the two upstream notes a few days before the public post, so the maintainers are not surprised and can
   correct anything we got wrong: the hftbacktest one is
   [the L3 FIFO draft](https://github.com/sarptandoven/queuetruth/blob/main/integrations/hftbacktest/upstream/l3-fifo-best-priced-orders.md),
   the NautilusTrader one is in [upstream.md](upstream.md). Fill in `{{HFTBACKTEST_ISSUE_URL}}` and
   `{{PUBLISH_DATE}}`.
4. Re-read the limits section of the long post. Every number is from one venue; say so wherever a number is quoted
   without its context.

## The numbers quoted

20 trading days, 2026-10-09 to 2026-11-06: 265,402 symbol-days of 12,440 symbols. `standard` shadow population (one
lot or the median order size, at the touch or one or two ticks from it), faithful mode (the libraries as released),
orders checked 1 minute after they go live; symbol-days whose replay checks flagged or that were halted are left out.

| model | fill rate vs exact, all symbols |
|---|---|
| exact replay | 11.97% fill |
| NautilusTrader touch model, `prob_fill_on_limit=1` | +1.50 pp (13% more orders; 95% interval +1.45 to +1.55) |
| NautilusTrader queue_position model | +0.06 pp |
| hftbacktest L3 FIFO (order-level input) | -1.82 pp (15% fewer orders) |
| hftbacktest ProbQueue (11 settings) | `{{PROBQUEUE_RANGE_20D}}` |
| Databento tutorial estimator | `{{DATABENTO_RANGE_20D}}` |

**Why hftbacktest's L3 model fills fewer** ([findings](https://github.com/sarptandoven/queuetruth/blob/main/scripts/scale/findings/l3-fifo-gap.md)):
the released hftbacktest 2.4.4, run on the same records and orders, gives the same first fill as queuetruth's replica
on all 117,356 orders traced (one IEX day, 12 symbols, and the Nasdaq demo window). Every fill it misses is of an
order priced better than every displayed order on its side: it judges a trade-through against the feed's best price,
not the order's price, and it ignores hidden executions. On IEX, hidden executions are three quarters of the gap.
Most missed orders fill later in hftbacktest, after the price moved through them.

**First day only (2026-10-09, 9,306 symbols, 5,783,416 orders)**, quoted in the drafts until the 20-day values are
filled in: among the 100 most traded symbols the exact replay fills 45.67% and books -1.114 ticks of P&L per order;
the touch model books +0.106 ticks per order more, ProbQueue -0.322 to -0.377, the L3 model -0.639. Symbols ranked 501
to 1,500 show the largest relative errors: the touch model +16.2%, ProbQueue -9.6% to -13.4%, the L3 model -21.2%.
