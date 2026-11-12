# Upstream notes

To send a few days before the public post, so each project hears it from us first and can correct anything we got
wrong. Edit the tone to your own; keep the numbers with their context.

## hftbacktest: L3 FIFO misses fills of orders priced better than the displayed book

The note to send is the draft
[`integrations/hftbacktest/upstream/l3-fifo-best-priced-orders.md`](https://github.com/sarptandoven/queuetruth/blob/main/integrations/hftbacktest/upstream/l3-fifo-best-priced-orders.md).
It replaces the earlier text of this section, which predates the run on the released hftbacktest. It rests on
[the L3 FIFO findings](https://github.com/sarptandoven/queuetruth/blob/main/scripts/scale/findings/l3-fifo-gap.md):
hftbacktest 2.4.4 itself gives the same first fill as queuetruth's replica on all 117,356 orders traced, and every
missed fill is an order priced better than every displayed order on its side (trade-through judged against the feed's
best price, and hidden executions ignored). The 20-day atlas gap to quote with it: -1.82 percentage points (15% fewer
orders) within 1 minute, 2026-10-09 to 2026-11-06.

## NautilusTrader: the touch model is the optimistic bound

Repository: https://github.com/nautechsystems/nautilus_trader (a discussion fits better than an issue: this is
documented behaviour, not a bug). The documentation change we prepared earlier is in
`integrations/nautilus/UPSTREAM.md`.

> Hi, we maintain queuetruth, which replays order-level feeds exactly and compares backtest fill models with the exact
> FIFO queue. Over 20 IEX DEEP+ trading days (2026-10-09 to 2026-11-06, 265,402 symbol-days, orders checked 1 minute
> after each goes live), our re-implementation of the default fill model with `prob_fill_on_limit=1` fills 13% more
> orders than the exact replay (+1.50 percentage points; 95% interval +1.45 to +1.55), like an order that is always
> first in the queue. Queue-position tracking was within 0.06 points. {{NAUTILUS_PNL_20D}}
>
> Our models are re-implemented from your documentation and the behaviour of the released package, so please tell us
> if any of it misrepresents NautilusTrader. A sentence in the fill model docs suggesting queue-position tracking for
> order-book backtests of passive orders would help users; the prepared text is linked above.
>
> We plan to publish these numbers on {{PUBLISH_DATE}}.
