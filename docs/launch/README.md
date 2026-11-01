# Launch kit: the Fill-Realism Atlas, first edition

Drafts for announcing the first public atlas edition (IEX DEEP+, 2026-10-09). Nothing here is posted anywhere by the
repository; a maintainer edits and posts it. Pages under `docs/launch/` are left out of the docs site and of the
placeholder check.

| file | what it is |
|---|---|
| [atlas-first-edition.md](atlas-first-edition.md) | the long post: what was measured, the results, why, what to do, how to check it |
| [social.md](social.md) | Hacker News, r/algotrading, X and LinkedIn versions |
| [upstream.md](upstream.md) | heads-up issues for hftbacktest and NautilusTrader, to send before the public post |

## Before posting

1. Merge #2 (dependencies) and then #1 (the atlas pipeline, P&L error and site) into `main`.
2. Turn on GitHub Pages: Settings > Pages > Source: GitHub Actions. Run `.github/workflows/atlas.yml` once by hand
   with date `2026-10-09` so the site holds this edition, and check that https://sarptandoven.github.io/queuetruth/
   shows the numbers quoted in these drafts (the numbers below come from a local build of the same day; the atlas root
   was `8d65f7816ce88de7ec3f166260c140565a28fea7a75ba51bd16af16bd15e67b1`).
3. Merge #5 (the L3 FIFO analysis and its tests, which the hftbacktest note points to), then send the two upstream
   notes in [upstream.md](upstream.md) a few days before the public post, so the maintainers are not surprised and
   can correct anything we got wrong. Fill in `{{HFTBACKTEST_ISSUE_URL}}` and `{{PUBLISH_DATE}}`.
4. Re-read the limits section of the long post. Every number in the drafts is from one day on one venue; say so
   wherever a number is quoted without its context.

## The numbers quoted

All from the `standard` shadow population (one lot or the median order size, at the touch or one or two ticks from
it), faithful mode (the libraries as released), orders checked 60 s after they go live, 95% intervals from a bootstrap
over symbols. 9,306 symbols, 5,783,416 orders; 100 symbol-days whose replay checks flagged or that were halted are left
out.

| model | fill rate vs exact, all symbols | top 100 symbols | ticks of P&L per order vs exact, top 100 |
|---|---|---|---|
| exact replay | 10.76% fill | 45.67% fill | -1.114 per order |
| NautilusTrader touch model, `prob_fill_on_limit=1` | +1.62 pp (+15%) | +2.25 pp (+4.9%) | +0.106 |
| NautilusTrader queue_position model | +0.05 pp (+0.5%) | -0.09 pp | -0.049 (not clear of 0) |
| hftbacktest ProbQueue (11 settings) | -0.71 to -1.02 pp (-6.6% to -9.4%) | -1.03 to -2.10 pp | -0.322 to -0.377 |
| hftbacktest L3 FIFO (order-level input) | -1.62 pp (-15.0%) | -2.99 pp (-6.6%) | -0.639 |
| Databento tutorial estimator | -0.87 to -0.90 pp (-8%) | -1.67 to -1.77 pp | -0.342 to -0.344 |

Symbols ranked 501 to 1,500 by trading activity show the largest relative errors: NautilusTrader's touch model fills
16.2% more orders than the exact replay there, hftbacktest's ProbQueue models 9.6% to 13.4% fewer and its L3 model
21.2% fewer.
