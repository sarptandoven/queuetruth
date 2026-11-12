# Launch kit: the Fill-Realism Atlas

Drafts for announcing the public atlas (IEX DEEP+, the 20 trading days 2026-10-15 to 2026-11-11). Nothing here is
posted anywhere by the repository; a maintainer edits and posts it. Pages under `docs/launch/` are left out of the
docs site and of the placeholder check.

| file | what it is |
|---|---|
| [atlas-first-edition.md](atlas-first-edition.md) | the long post: what was measured, the results, why, what to do, how to check it |
| [social.md](social.md) | Hacker News, r/algotrading, X and LinkedIn versions |
| [upstream.md](upstream.md) | heads-up notes for hftbacktest and NautilusTrader, to send before the public post |

## Before posting

1. Check that GitHub Pages is on (Settings > Pages > Source: GitHub Actions); run `.github/workflows/atlas.yml` once
   by hand with `site_only` if the site or the docs are not there yet. Check https://sarptandoven.github.io/queuetruth/
   and the docs at https://sarptandoven.github.io/queuetruth/docs/.
2. The site always shows the newest 20 trading days, so on the day you post its window has moved on from the one
   quoted below. Either quote the site's current window (`queuetruth atlas --json` prints its numbers, dates and
   root) and update the figures in the drafts, or keep these figures with their dates. Fill in `{{ATLAS_ROOT}}` with
   the root of the window you quote (`f6833707955691f3fb6dbbe1d6a6a01e2e639afec2f5162623994788011c07c2` for the
   window below).
3. Send the two upstream notes in [upstream.md](upstream.md) a few days before the public post, so the maintainers
   are not surprised and can correct anything we got wrong. Fill in `{{HFTBACKTEST_ISSUE_URL}}` and
   `{{PUBLISH_DATE}}`.
4. Re-read the limits section of the long post. Every number is from one venue; say so wherever a number is quoted
   without its context.

## The numbers quoted

The 20-day window: IEX DEEP+, 2026-10-15 to 2026-11-11, 265,351 symbol-days, 12,456 symbols, atlas root
`f6833707955691f3fb6dbbe1d6a6a01e2e639afec2f5162623994788011c07c2`. The `standard` shadow population (one lot or the
median order size, at the touch or one or two ticks from it), faithful mode (the libraries as released), orders
checked 1 minute after they go live, 95% intervals from a bootstrap over symbols and days; symbol-days whose replay
checks flagged or that were halted are left out.

| model | fill rate vs exact, all symbols |
|---|---|
| exact replay | 12.20% fill |
| NautilusTrader touch model, `prob_fill_on_limit=1` | +1.48 pp (12% more orders; 95% interval +1.42 to +1.53) |
| NautilusTrader queue_position model | +0.06 pp (closest) |
| hftbacktest ProbQueue (9 settings) | -0.79 to -1.10 pp (6% to 9% fewer orders) |
| Databento tutorial estimator | -0.94 to -0.97 pp (about 8% fewer) |
| hftbacktest L3 FIFO (order-level input) | -1.88 pp (15% fewer orders) |

The 100 most traded symbols (336 symbols over the window, as tiers are set day by day):

| model | top 100 symbols, fill rate vs exact | ticks of P&L per order vs exact, top 100 |
|---|---|---|
| exact replay | 50.47% fill | -1.613 per order (95% interval -2.397 to -1.027) |
| NautilusTrader touch model, `prob_fill_on_limit=1` | +1.58 pp (+3.1%) | +0.260 (+0.129 to +0.424) |
| NautilusTrader queue_position model | +0.01 pp (not clear of 0) | +0.064 (not clear of 0) |
| hftbacktest ProbQueue (9 settings) | -1.62 to -2.49 pp (-3.2% to -4.9%) | -0.835 to -0.926 |
| hftbacktest L3 FIFO (order-level input) | -4.15 pp (-8.2%) | -1.888 (-2.586 to -1.280) |
| Databento tutorial estimator | -2.08 to -2.16 pp | -0.846 to -0.848 |

Relative errors by tier (touch model; ProbQueue; L3 FIFO): top 100 +3.1%; -3.2% to -4.9%; -8.2%. Ranks 101 to
500 +6.7%; -5.4% to -8.5%; -13.6%. Ranks 501 to 1,500 +12.7%; -9.5% to -13.0%; -22.1%. Ranks 1,501 to 4,000
+17.2%; -6.9% to -9.5%; -16.3%. The rest +7.3%; -1.3% to -1.8%; -4.0%.

Touch tables (bar backtests; day orders resting from placement to the close; bars built from IEX's prints): of the
orders a bar rule fills by touch, the share the real queue had filled by the end of the same bar.

| bar | all symbols | top 100 | ranks 1,501 to 4,000 |
|---|---|---|---|
| 1 minute | 88.8% (88.3% to 89.2%) | 95.7% (95.1% to 96.3%) | 86.4% (85.8% to 86.9%) |
| 5 minutes | 92.4% (92.0% to 92.7%) | 97.9% (97.6% to 98.2%) | 90.6% (90.1% to 91.1%) |
| 1 hour | 97.1% (96.9% to 97.3%) | 99.4% (99.3% to 99.5%) | 96.7% (96.5% to 96.9%) |

Why hftbacktest's L3 FIFO model fills fewer orders: it is hftbacktest's own behaviour, not an error of queuetruth's
replica. The released hftbacktest 2.4.4 gives the same first fill as the replica on all 117,356 orders of the
findings run. It skips fills that carry no side, the hidden executions (about 3 in 4 of the misses on IEX, where
hidden executions outnumber displayed ones about 5 to 1), and it counts a trade-through only through the feed's best
price, so an order alone ahead of the displayed book misses those fills. The hidden-execution share rests on
queuetruth's ruling that displayed interest fills before hidden interest at the same price. Evidence:
https://github.com/sarptandoven/queuetruth/blob/main/scripts/scale/findings/l3-fifo-gap.md
