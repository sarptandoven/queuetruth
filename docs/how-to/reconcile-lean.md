# Reconcile a QuantConnect LEAN backtest with live fills

**Goal:** bring the orders and fills of a QuantConnect LEAN backtest into queuetruth, pair each simulated order with
the live order it stands for, and get a per-order account of the differences. Then score the backtest, against your
live fills or against exact-queue shadows. It works like the
[Backtrader and VectorBT guide](reconcile-backtrader-vectorbt.md), and gives the same report.

**You need:** the output files of one LEAN backtest, and your live orders and fills as a canonical live log, written
by `queuetruth live import` from your venue's or broker's records ([Import fills from my venue or
broker](import-live-logs.md)). queuetruth never imports LEAN: it reads the JSON that LEAN writes. The formats below
were read from LEAN's public source (release tag 18241 of github.com/QuantConnect/Lean). The fixtures in
`tests/fixtures/live/backtesters/` are written from that source by `make_fixtures.py`, not exported from a real run,
so check the counts against your own records the first time.

**From the command line or Python.** `queuetruth live import --venue sim-lean` writes the backtest as a simulator
log (venue `sim:lean`, `validation_status = simulated`), with options through `--option KEY=VALUE`. `queuetruth
reconcile --kind lean --sim RESULT_JSON --sim ORDER_EVENTS_JSON --live LIVE_LOG_DIR --out DIR` runs the whole
comparison. The Python API is `queuetruth.live.importers.lean`.

## What LEAN assumes

LEAN's default fill models fill an order against the prices of the data it sees: a market order at the last price
when it is placed, a limit order when a later bar trades at or through its limit, a stop order when a bar reaches
its stop. Every fill is the full size unless your fill model says otherwise, and there is no queue.

LEAN stamps every event with the algorithm's time, in UTC. On bar data that time is the end of the bar the
algorithm acted on. An order placed after the 09:00 to 10:00 bar is stamped 10:00, and a limit order the next bar
traded through is filled at 11:00, the end of that bar.

Pass the bar length as `--option timeframe=1h` (`1m`, `5m`, `1h`, `1d`, ...). The importer then moves a fill
stamped after its order's submission to the start of the bar that filled it (the 11:00 fill above becomes 10:00,
never earlier than the submission). A market order filled on the bar it was placed on keeps its time. The
reconciliation reads a live fill anywhere inside the backtest's fill bar as on time, and pairs orders within two
bars.

## 1. Find the backtest's output

A local backtest (the LEAN engine, or the LEAN CLI) writes two files per run into its results directory:

- `<algorithm id>.json`: the result. Its `orders` member holds every order (`Order`: `id`, `symbol`, `time`,
  `type`, `status`, `direction`, `quantity`, `limitPrice`, `stopPrice`, `properties.timeInForce`).
- `<algorithm id>-order-events.json`: every order event (`SerializedOrderEvent`: `orderId`, `orderEventId`,
  `symbolValue`, `time` in Unix seconds, `status`, `fillPrice`, `fillQuantity`, `direction`, `orderFeeAmount`,
  `orderFeeCurrency`, `message`, ...).

Give both. The fills are in the events file only: a result JSON alone fails `QT-INPUT-002`. A result document that
carries `orderEvents` next to `orders` (as a QuantConnect result packet may) needs no second file; an event given
twice is counted once. The events file alone works too: each order is then built from its events (`orderType`,
`quantity`, `limitPrice`), with a warning, and its time in force reads as `gtc`.

How the files are read:

- One order per order id. `submit_sent` at the order's `time`, `ack` at its first `submitted` event, one fill per
  `partiallyFilled` or `filled` event that has a fill quantity, `cancel_ack` at `canceled` and `reject` at
  `invalid`, with LEAN's message.
- An order LEAN refused before submitting it (`invalid`, for example for lack of buying power) never rested: it gets
  `submit_sent` and `reject`, no `ack`. An order LEAN invalidates later, at fill time, keeps its `ack` and then gets
  the `reject`.
- A Day or GoodTilDate order past its expiry is cancelled by LEAN with the message "The order has expired.". It is
  logged as `expire`, not `cancel_ack`.
- `new`, `none`, `cancelPending` and `updateSubmitted` events are counted; the orders section holds the order's
  final price and quantity. So is a `partiallyFilled` or `filled` event without a fill quantity: LEAN writes one
  when the brokerage model refuses an order update.
- An order below the lot size is rounded to quantity 0 and invalidated by LEAN (direction Hold). It is counted and
  left out.
- Fees: LEAN charges the whole order's fee on the final `filled` event; partial fills carry none. A fee in the fill
  price's currency is kept. A crypto buy charged in its base asset (LEAN's Binance fee model charges BTC on a
  BTCUSDT buy) has that fee subtracted from `fillQuantity` by LEAN: the importer logs the gross quantity
  (`fillQuantity` plus the fee) and the fee in the quote currency at the fill price. A fee in any other currency is
  left out, with a warning.
- `Market`, `MarketOnOpen` and `MarketOnClose` are market orders, `Limit` a limit order at `limitPrice`,
  `StopMarket` and `TrailingStop` stop orders, `StopLimit` a stop-limit at `limitPrice`, and `LimitIfTouched` a
  stop-limit at `limitPrice` (as for Binance's `TAKE_PROFIT_LIMIT`). Option exercises and combo orders fail
  `QT-INPUT-002`. Time in force is `gtc`, `day` or `gtd`, from `timeInForce`. Contingent orders (OTO, OCO,
  brackets) are warned: LEAN may hold one until its trigger, but its `ack` is its `submitted` event.
- Enums are read as numbers or names. The C# property names of `OrderEvent` (`OrderId`, `Id`, `UtcTime`,
  `FillQuantity`, `OrderFee`, ...) and the hyphenated names of older releases (`order-id`, `order-event-id`, ...)
  read the same.
- An order-events CSV whose header uses the JSON names (for example the events file passed through
  `pandas.read_json(...).to_csv(...)`) reads the same as the JSON. LEAN writes no CSV of its own.
- The instrument is the symbol's ticker (`symbolValue`, or `symbol.value` in the orders section). Name it after the
  live instrument with `--option instrument=BTCUSDT` (every order) or
  `--option instruments=BTCUSDT:BTCUSDT,ETHUSDT:ETH-USDT` (per ticker; a ticker the backtest does not trade fails
  `QT-CONFIG-001`).
- Times are UTC, as LEAN reports them, so there is no `data_tz` option. Unix seconds are rounded to the microsecond.
- Without the orders section, an order takes the quantity and prices of its last event: LEAN stamps every event
  with the order's current values, so an update shows there.
- Order ids restart at 1 in every backtest, so one import holds one backtest. Two orders sections, events of two
  `algorithmId`s, the same file twice, fills beyond an order's quantity or an event for an order the orders section
  does not hold fail with `QT-CONFIG-001` or `QT-INPUT-002`: the files come from different runs. For the same
  reason `--append` to an existing log is refused: write each backtest to its own `--out` directory.

## 2. Import the live fills

The live arm is any canonical live log. The fixtures hold the live run as a captured Binance user-data stream:

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth live import --venue binance --account main --in "$FX/binance_user_stream.jsonl" \
    --instrument-map "$FX/instrument_map.csv" --out live
```

It is the invented BTCUSDT strategy of the Backtrader and VectorBT guide: seven live orders on 1-hour bars on
2024-03-01, of which four filled, one was cancelled unfilled, one was cancelled after filling 0.008 of 0.02, and
one is still open.

## 3. Reconcile

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth reconcile --kind lean --sim "$FX/lean_result.json" --sim "$FX/lean_order_events.json" --live live \
    --out rec-lean --option timeframe=1h
```

It writes `rec-lean/live`, `rec-lean/sim`, `rec-lean/score`, `rec-lean/reconcile.json` (`qt.bot_reconcile`, kind
`lean`) and `rec-lean/reconcile_orders.csv`, as for the other backtesters. The printout starts with the counts:

```text
reconcile (lean): 7 live orders, 7 simulator orders, 6 paired (86% of live orders)
score orders: both 4, neither 0, live_only 0, sim_only 1
categories: match 2, missed_fill 1, partial_fill 1, price 1, timing 1, no_sim_twin 1, no_live_twin 1
sim price advantage BTCUSDT: 0.1962 (quote)
missed-fill notional BTCUSDT: 608 (quote)
```

and then gives one line per order, for example:

```text
[missed_fill] binance:main:7100003: order buy 0.01 BTCUSDT @ 60800: backtest filled 0.01 at 60800 in the 1h candle
opening 2024-03-01T14:00:00.000Z (candle rule); the live order rested 59m28s and ended cancelled without a fill
[timing] binance:main:7100006: order buy 0.01 BTCUSDT @ 61100: timing: live filled 2024-03-01T21:30:00.000Z,
outside the backtest fill candle [2024-03-01T20:00:00.000Z, +1h)
[no_live_twin] sim:lean:main:7: backtest order buy 0.01 BTCUSDT @ 61300 expired; no live order pairs with it: ...
```

The backtest's order 7 expired at its GoodTilDate time and has no live twin. The rest reads as in
the Backtrader guide: LEAN filled the 14:00 buy because the bar traded at its limit, while the live order rested
an hour in the queue and was cancelled.

The same in Python:

<!-- check-docs: run -->
```python
import os

from queuetruth.live.importers.freqtrade import render_reconcile
from queuetruth.live.importers.lean import reconcile_lean

fx = os.path.join(os.environ["QT_FIXTURES"], "live", "backtesters")
sim = [os.path.join(fx, "lean_result.json"), os.path.join(fx, "lean_order_events.json")]
report = reconcile_lean(sim, "live", "rec-lean-py", timeframe="1h")
print(render_reconcile(report, max_rows=3))
assert report.counts["missed_fill"] == 1 and report.n_paired == 6
```

Every number above is asserted in `test_reconcile_end_to_end` (`python/tests/live/test_lean.py`); the command line
gives the same report (`python/tests/cli/test_cli_backtesters.py`).

**Pairing, categories and refusals** are those of the Backtrader and VectorBT guide
([section 3](reconcile-backtrader-vectorbt.md#3-reconcile)). The reference time of a LEAN order is its `time`
(the submission). A paired order takes the live `order_key`; a backtest fill before the live order's entry is
moved to that entry, with the bar time kept in the fill's `reason`.

## 4. Options

| Option | Meaning |
|---|---|
| `timeframe` | bar length (`1m`, `5m`, `1h`, `1d`, `1w`): fills stamped at a bar's end move to its start; default pairing window two bars |
| `fill_delay_bars` | move every fill this many bars later, after the move above (needs `timeframe`) |
| `pair_window_ms` | pairing window in milliseconds |
| `instrument`, `instruments` | live instrument name of every order, or per ticker (`TICKER:NAME,...`) |
| `venue` | the live venue (when the live log holds several; for `live import`, the venue of the instrument-map rows) |
| `pair_with` | `live import` only: a canonical live-log directory to pair with (`reconcile` takes it as `--live`) |
| `price_exp`, `size_exp` | scales of instruments missing from the instrument map (default -9, -8) |

LEAN writes prices, quantities and fees as C# decimals. They are read exactly, then rounded half-even to the
instrument's tick and lot (counted in a warning).

## 5. Import the backtest alone, then score

To build the simulator log yourself, for `queuetruth score --sim-log`, the referee or the HTML report, import it
with the live log to pair with:

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth live import --venue sim-lean --in "$FX/lean_result.json" --in "$FX/lean_order_events.json" \
    --instrument-map "$FX/instrument_map.csv" --option pair_with=live --option timeframe=1h --out lean-sim
queuetruth score --live live --sim-log lean-sim --out sc-lean --window 1440m
```

The same in Python is `queuetruth.live.import_sim("lean", [result, events], "lean-sim", pair_with="live",
timeframe="1h", instrument_map=...)`. Without `pair_with`, the simulator orders keep `sim:lean:<account>:<id>` keys
and match no live order (warned). To score the backtest next to exact-queue shadows on an order-level dataset,
follow [section 6 of the Backtrader and VectorBT guide](reconcile-backtrader-vectorbt.md#6-against-exact-queue-shadows)
with this simulator log.

## Limits

- On bar data a LEAN fill time is a bar's end. Without `timeframe` it is read as an exact time, an hour late for an
  hourly limit fill. Give `timeframe` for bar data; leave it out for tick data.
- Contingent orders are warned, not modelled: a child order's `ack` can come before its trigger.
- The orders section holds an order's final price and quantity. An order updated while it rested
  (`updateSubmitted`) is logged at its last limit price.
- Option exercises and combo orders are refused. Import a backtest of single-instrument orders.
- `score` reads only resting limit orders with a usable entry time; market and stop orders are explained by the
  reconciliation but not scored.
