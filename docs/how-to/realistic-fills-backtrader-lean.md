# Realistic fills in Backtrader and LEAN

**Goal:** make a bar backtest fill resting limit orders as often as the real queue does, with one line in your own
script. Backtrader and LEAN fill a limit order as soon as a bar trades at its price. Many of those fills are not
real: the price touched your limit and turned back before the queue in front of you was gone. queuetruth's fill
models keep the backtester's own limit logic and accept each such fill with the probability that the real queue
filled it.

**You need:** queuetruth 0.4.0 or later and the liquidity tier of your instruments. Nothing else: no order-level data of your own. The fill models
read the atlas touch tables (`atlas/v1/touch.json`) through the same download-after-consent cache as `queuetruth
atlas`: pass `yes=True` once and the cached copy works offline from then on. `table="touch.json"` reads a copy you
saved instead.

## Backtrader

<!-- check-docs: skip (needs Backtrader, which queuetruth does not install) -->
```python
import backtrader as bt
import queuetruth as qt

cerebro = bt.Cerebro()
# ... your data feeds (1-minute, 5-minute or 1-hour bars) and strategy ...
filler = qt.backtrader.QueueFiller(tier="T1", seed=0, yes=True)  # the atlas's touch tables
cerebro.broker.set_filler(filler)
cerebro.addanalyzer(qt.backtrader.OrderLog)  # optional: writes bt_orders.jsonl for queuetruth check
cerebro.run()
print(filler.summary())  # draws made, fills given, fills declined
```

`QueueFiller` is a Backtrader filler. On each bar that reaches a resting `Limit` order (or a triggered `StopLimit`),
it draws whether the real queue filled it, with `P(real | touch)` for the order's tier, side and bar length. If the
draw says yes the order fills in full at Backtrader's price; if not, the order keeps resting and can fill on a later
bar. Market, close and stop orders fill as Backtrader fills them. The bar length comes from the data feed
(`timeframe` and `compression`); pass `bar="5m"` to set it. `filler.decisions` lists every draw: the order, the
bar, the probability, the random number and whether it filled.

The filler needs no Backtrader to be made, so this block runs anywhere: the same line on the synthetic atlas the
tests use (invented numbers), and the probabilities it will draw with.

<!-- check-docs: run -->
```python
import os

import queuetruth as qt

atlas = os.path.join(os.environ["QT_FIXTURES"], "atlas_synth")  # without atlas= it is the public atlas
filler = qt.backtrader.QueueFiller(tier="T1", seed=0, atlas=atlas)
print(filler.touch.tier, filler.touch.probability("1m", "buy"), filler.touch.probability("5m", "sell"))
```

## LEAN

<!-- check-docs: skip (runs inside a LEAN Python algorithm) -->
```python
from AlgorithmImports import *
import queuetruth as qt


class MyAlgorithm(QCAlgorithm):
    def initialize(self):
        equity = self.add_equity("SPY", Resolution.MINUTE)
        equity.set_fill_model(qt.lean.QueueFillModel(tier="T1", seed=0))  # the atlas's cached touch tables
```

`QueueFillModel` returns a LEAN `FillModel` that calls LEAN's own `limit_fill` and, when LEAN fills the order,
accepts the fill with the same probability. A declined fill leaves the order open. The bar length comes from the
security's data resolution (minute or hour); pass `bar="5m"` for 5-minute bars. queuetruth's tests run this model
against a stand-in of LEAN's documented Python API, not against LEAN itself.

## The options

| Option | Meaning |
|---|---|
| `table` | a `touch.json` file, a `queuetruth.atlas.touch.TouchTables` or its dict; without it the atlas's tables |
| `atlas`, `yes` | where the atlas is (a directory or URL; default the public atlas) and the consent to download it |
| `tier` | the liquidity tier of the instruments, as the table names it (`T1_top100`) or by its short name (`T1`); `ALL` pools the tiers |
| `rule` | `touch` (default): the bar's low reaches a buy's limit (high for a sell); `penetrate_1`, `penetrate_2`: it goes 1 or 2 ticks through. A penetrate rule needs `tick=` the tick size |
| `seed` | the draws' seed (default 0). The same seed and the same orders give the same fills, in any run |
| `bar` | `1m`, `5m` or `1h` when it cannot be read from the data |

The models never invent a probability. If the table has no row for the tier, rule, bar length or side, or the row
has no value, they stop with `QT-CONFIG-001` and say what the table has. Other bar lengths (15 minutes, daily) are
refused for the same reason.

## Where the probabilities come from, and what they are not

- **The source.** `P(real | touch)` is measured by the atlas: its shadow orders replayed in the exact queue of IEX
  DEEP+ days, compared with what a bar rule says on bars built from the same day's prints
  ([Touch tables](../concepts/touch-tables.md)). That is one venue, about 4 to 5% of US equity volume, with a speed
  bump and its own order types. Its queues may not transfer to other venues, to other asset classes or to crypto.
- **The approximations.** The ones listed on the touch-tables page apply here unchanged: orders placed at random
  times and counted from their bar, bars from one venue's prints (consolidated candles touch more often, so a larger
  share of their fills are not real), each order at the back of the queue, the first fill of a small order, day
  orders.
- **Every touching bar draws again.** The table measures the first bar that reaches the order. While a declined
  order keeps resting, each later bar that reaches it draws with the same probability, which is not measured.
- **A fill rule, not a queue model.** These models replace "fill on touch" with "fill on touch with the measured
  frequency". They know nothing about your order's place in the queue, its size or the book at that moment. For
  that, run your backtest's own orders through `queuetruth check` with order-level data
  ([Check my backtest](check-my-backtest.md)).

## Check the result

Add `qt.backtrader.OrderLog` to the run and check its log against the real queue when you have order-level data for
the same days:

<!-- check-docs: skip (needs a Backtrader run and a dataset) -->
```bash
queuetruth check bt_orders.jsonl --data DATASET
```
