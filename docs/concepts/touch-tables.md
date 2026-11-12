# Touch tables: how many bar-backtest fills are real

Most backtesters run on candles. Freqtrade, Backtrader, LEAN, VectorBT and backtesting.py fill a resting limit
order when a bar's low (for a buy) or high (for a sell) reaches the limit price. Freqtrade's docs say backtesting
assumes "All orders are filled at the requested price (no slippage) as long as the price is within the candle's
high/low range"; LEAN fills a limit "as soon as the limit price is hit". Some traders are stricter and fill only
when a print goes through the limit by a tick or two.

A bar backtester cannot know where its order would have stood in the queue. The atlas's exact replay can. For every
order the atlas places in the real queue, the touch tables compare what a bar rule would have said with what the
real queue did, and publish the result by liquidity tier, side and bar length. You need no data of your own to use
them.

## The measurement

**Orders.** The atlas's shadow orders, as it samples them ([shadow orders](shadow-orders.md)): a limit buy or sell
placed at a random time `t0` in the regular session (from 5 minutes after the open to 10 minutes before the close),
at the best price on its side or a few ticks away, joining the back of the queue at that price. For the touch
tables each order rests as a day order: from `t0` until it fills or the regular session closes at `C`. (The atlas's
fill-model tables cancel the same orders after 5 minutes.) The exact replay gives the time `tF` of the order's
first fill in the real queue, if it fills before `C`.

**Bars.** Bar lengths `T` of 1 minute, 5 minutes and 1 hour, aligned to the clock: bar `k` covers
`[k T, (k + 1) T)` of the venue's local day. A bar's low and high come from the prints (trades) of the replayed day
in the regular session, as the trades of a candle data set would.

**Rules.** For a buy at limit price `L` with tick size `tick` (a sell is the mirror image, with highs):

- **touch**: the order fills in the first bar whose low is at or below `L`;
- **penetrate by k ticks** (`k` = 1, 2): the first bar whose low is at or below `L - k tick`.

The first bar is the one holding `t0`, counted from `t0` on: prints before the order existed never fill it. The
rule's fill bar is `bR`; the real fill's bar is `bF`, the bar holding `tF`.

**What is reported**, for each rule and bar length:

| Column | Meaning |
|---|---|
| real fill rate | share of orders the real queue filled before the close |
| rule fill rate | share of orders the rule filled before the close |
| P(real \| rule) | of the orders the rule fills, the share the real queue had filled by the end of the same bar (`tF` in a bar `bF <= bR`): how many of the backtest's fills are real |
| P(rule \| real) | of the orders the real queue fills, the share the rule had filled by the end of the same bar (`bR <= bF`): how many real fills it catches |
| real markout | the average markout of the real fills, in ticks |
| rule markout | the average markout of the rule's fills, in ticks |
| P&L overstated | the rule's markout P&L minus the real one, summed over fills and divided by the orders placed, in ticks per order |

A markout is `side x (P(t + T) - L) / tick`: the move from the limit price to the last print at or before one bar
after the fill (at the close at the latest), signed so that positive is good for the order. `t` is `tF` for a real
fill, and for a rule fill the time of the first print in its fill bar that reached the rule's price. A touch rule
books fills where the price touched the limit and turned back; the real queue mostly fills when the price goes
through. So the rule's fills tend to have better markouts, and the last column shows by how much a bar backtest's
P&L is too high per order it places.

Every value has a 95% interval from the same bootstrap over dates and symbols as the rest of the atlas
(`queuetruth.eval.blocks.cluster_bootstrap` with clusters `session_date` and `symbol`). Sides are reported apart
(`buy`, `sell`) and together (`all`); tiers apart and together (`ALL`).

## What is approximate

- **Bar alignment against placement.** A bar backtester places orders at bar boundaries. The atlas's orders are
  placed at random times, so the first bar is counted from `t0` on, as if the backtester had placed the order at
  that bar's open. Because of this, whether the rule fills by the close does not depend on the bar length; the bar
  length decides when it fills, and so how many of its fills are real by then.
- **Prints from one venue.** The bars are built from the prints of the replayed venue (IEX in the public atlas,
  about 4 to 5% of US volume). Consolidated candles have more prints and wider ranges, so on them the rules fill
  more often than here, and a larger share of those extra fills are not real.
- **Queue position.** Each order joins the back of the queue at placement, as the atlas samples it. An order that
  was already resting would stand further ahead. Orders are small and do not change the book.
- **Fills without a print.** The real queue can fill an order without a print at its price, when the book crosses
  it. No bar rule can see such a fill, so the touch rule catches slightly fewer than all real fills.
- **First fill.** A real fill is the order's first fill, of any size. The standard population's orders are one lot
  or the median order size.
- **Day orders.** The order rests until the close. A backtest that cancels unfilled orders after a few bars sees
  fewer fills on both sides.
- **Halts and flags.** As in the rest of the atlas, symbol-days whose replay checks raised a flag or that were not
  in continuous trading are left out under the published flag policy.

## Where to find them

The atlas site has a short section on them, and `atlas/v1/touch.json` holds every row (schema:
`queuetruth.atlas.touch.TouchTables`). They are built by `scripts/scale/atlas_touch.py`: each unit writes
`touch_stats.parquet` (sums per symbol, rule, bar length and side, not per order), `atlas.py reduce-day` keeps them
as `touch_cells.parquet` and `atlas.py combine` writes `touch_by_tier.parquet`. Days reduced before the tables
existed have none; `atlas_manifest.json` lists the days used under `touch_days`.
