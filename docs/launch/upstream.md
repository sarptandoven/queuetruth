# Upstream notes

To send a few days before the public post, so each project hears it from us first and can correct anything we got
wrong. Edit the tone to your own; keep the numbers with their context.

## hftbacktest: the L3 FIFO model and orders priced better than every displayed order

The issue text is the draft
[`integrations/hftbacktest/upstream/l3-fifo-best-priced-orders.md`](https://github.com/sarptandoven/queuetruth/blob/main/integrations/hftbacktest/upstream/l3-fifo-best-priced-orders.md),
to open at https://github.com/nkaz001/hftbacktest. It replaces the note that was here, which predated the run on the
released hftbacktest. Its evidence is
[the findings page](https://github.com/sarptandoven/queuetruth/blob/main/scripts/scale/findings/l3-fifo-gap.md):
hftbacktest 2.4.4 gives the same first fill as queuetruth's replica on all 117,356 orders, and it misses fills
because it skips fills without a side (the hidden executions, about 3 in 4 of the misses on IEX) and counts a
trade-through only through the feed's best price.

Before sending, add the atlas context in one sentence, for example:

> Over 20 IEX DEEP+ trading days (2026-10-09 to 2026-11-06, 12,440 symbols), the model fills 1.82 percentage points
> fewer small resting orders within 1 minute than the exact replay (11.97%). We plan to publish these numbers on
> {{PUBLISH_DATE}} and would like to quote your view, or a fix, alongside them.

Then fill in `{{HFTBACKTEST_ISSUE_URL}}` in the long post with the issue's link. We also still have the fix for #312
ready (`integrations/hftbacktest/upstream/` in our repository).

## NautilusTrader: the touch model is the optimistic bound

Repository: https://github.com/nautechsystems/nautilus_trader (a discussion fits better than an issue: this is
documented behaviour, not a bug). The documentation change we prepared earlier is in
`integrations/nautilus/UPSTREAM.md`.

> Hi, we maintain queuetruth, which replays order-level feeds exactly and compares backtest fill models with the exact
> FIFO queue. Over 20 IEX DEEP+ trading days (2026-10-09 to 2026-11-06, 12,440 symbols, 265,402 symbol-days, orders
> checked 1 minute after they go live), our re-implementation of the default fill model with
> `prob_fill_on_limit=1` fills 13% more small resting orders than the exact replay (+1.50 percentage points; 95%
> interval +1.45 to +1.55), exactly like an order that is always first in the queue. Queue-position tracking was
> within 0.06 points. On the first day (2026-10-09), among the 100 most traded symbols, the touch model booked 0.11
> ticks per order more P&L than the exact replay.
>
> Our models are re-implemented from your documentation and the behaviour of the released package, so please tell us
> if any of it misrepresents NautilusTrader. A sentence in the fill model docs suggesting queue-position tracking for
> order-book backtests of passive orders would help users; the prepared text is linked above.
>
> We plan to publish these numbers on {{PUBLISH_DATE}}.
