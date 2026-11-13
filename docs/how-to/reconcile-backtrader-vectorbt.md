# Reconcile a Backtrader, VectorBT or LEAN backtest with live fills

**Goal:** bring the orders and fills of a Backtrader, VectorBT or QuantConnect LEAN backtest into queuetruth, pair
each simulated order with the live order it stands for, and get a per-order account of the differences: which orders
the backtest filled that the market did not, which fills came at another price or another time, and which orders
exist on one side only. Then score the backtest, against your live fills or against exact-queue shadows.

**You need:** one export of the backtest (one line added to your Backtrader script, or one line after your
VectorBT portfolio), and your live orders and fills as a canonical live log, written by `queuetruth advanced live import`
from your venue's or broker's records ([Import fills from my venue or broker](import-live-logs.md)). queuetruth
never imports Backtrader or VectorBT: it reads the exported JSON or CSV. The formats below were read from the
projects' public source (Backtrader 1.9.78.123, VectorBT 0.28.1); the fixtures in
`tests/fixtures/live/backtesters/` are written from that source by `make_fixtures.py`, not exported from a real
run, so check the counts against your own records the first time.

**The everyday path.** `queuetruth compare --backtest BACKTEST --live LIVE` runs this comparison in one command: it
reads the backtester and the kind of live records from the files, imports a venue export when needed and
answers first. See [Compare my backtest with my live run](compare-backtest-and-live.md). This page shows the
steps underneath, for scripts and for options `compare` does not expose.

These importers need queuetruth 0.3.0 or later; the `queuetruth advanced reconcile` spelling needs 0.4.0 (on 0.3.x
the command is `queuetruth reconcile`). For a QuantConnect LEAN backtest, see
[section 7](#7-quantconnect-lean): the same report, from LEAN's result and order-events JSON.

**From the command line or Python.** `queuetruth advanced live import --venue sim-backtrader | sim-vectorbt` writes the
backtest as a simulator log (venue `sim:backtrader` / `sim:vectorbt`, `validation_status = simulated`), with options
through `--option KEY=VALUE`. `queuetruth reconcile --kind backtrader | vectorbt --sim EXPORT --live LIVE_LOG_DIR
--out DIR` runs the whole comparison. The Python API is `queuetruth.live.importers.backtrader` and
`queuetruth.live.importers.vectorbt`.

## What the two backtesters assume

Backtrader's default broker (`BackBroker`) fills an order against a bar's prices: a market order at the next bar's
open, a limit order when the next bar trades at or through its price (at the limit, or at the open when the bar
opens through it). Every fill is the full size unless you set a filler, and there is no queue. An order you create
on one bar can fill on the next bar at the earliest.

VectorBT simulates fills only: every record of `pf.orders` is an execution of its full size at the price it was
given (by default the bar's close, moved by `slippage`). There is no resting order, no unfilled order and no queue.

Both therefore know a fill's time only to the bar. Pass the bar length as `--option timeframe=1h` (`1m`, `5m`, `1h`,
`1d`, ...): the reconciliation then reads a live fill anywhere inside the backtest's fill bar as on time, and pairs
orders within two bars.

## 1. Export the backtest

### Backtrader: add an analyzer

Add queuetruth's analyzer to Cerebro. It writes one JSON line per `notify_order` and `notify_trade` call: the
order's reference, data feed, status, type, size, prices, times and the execution bits that are new in that
notification (`order.executed.iterpending()`, the same source as Backtrader's own `Transactions` analyzer).

<!-- check-docs: skip (needs Backtrader, which queuetruth does not install) -->
```python
import backtrader as bt
import queuetruth as qt

cerebro = bt.Cerebro()
# ... your data feeds and strategy ...
cerebro.addanalyzer(qt.backtrader.OrderLog)  # writes bt_orders.jsonl; path="run1.jsonl" names another file
cerebro.run()
```

`qt.backtrader.OrderLog` needs queuetruth 0.4.0 or later and imports Backtrader only when you use it. On
an older release, copy the analyzer from
[python/queuetruth/backtrader.py](https://github.com/sarptandoven/queuetruth/blob/main/python/queuetruth/backtrader.py)
(`_order_log_class`): it is the `QtOrderLog` class earlier versions of this page printed, and writes the same lines.
To make the backtest's limit fills realistic as well, see
[Realistic fills in Backtrader and LEAN](realistic-fills-backtrader-lean.md).

How the lines are read:

- One order per `ref`. `submit_sent` at `created` (the bar the strategy placed it on), `ack` at the bar of the
  `Accepted` notification (`BackBroker` delivers `Submitted` and `Accepted` on the next bar), one fill per execution
  bit (fee = its `comm`), and `cancel_ack`, `expire` or `reject` (`Margin`: not enough cash, `Rejected`) at the bar
  of that notification. An order refused at submission (`Submitted`, then `Margin`) never rested: it gets
  `submit_sent` and `reject`, no `ack`. `Created` notifications are counted.
- One file is one run. `Order.ref` restarts at 1 in every Backtrader process, so several `--sim` files number
  their orders `<file>-<ref>` (`1-3` is order 3 of the first file). The same file twice, under any name, fails
  `QT-CONFIG-001`.
- `Market` and `Close` orders are market orders, `Limit` a limit order at `price`, `Stop` and `StopTrail` stop
  orders, `StopLimit` a stop-limit at `pricelimit`, `StopTrailLimit` a stop-limit without a limit price (it
  trails). Time in force is `gtc`, or `gtd` when the order has a `valid` time.
- Trade lines are positions, not orders: they are counted and their closed `pnlcomm` summed in a warning.
- Backtrader's datetimes are naive. With a data feed that has a `tz`, `num2date` in this analyzer gives UTC, the
  default. Otherwise they are in your data's own zone: pass `--option data_tz=Europe/Berlin` (any IANA name).
- Use the name of the live instrument as the data feed's `name` (`cerebro.adddata(data, name="BTCUSDT")`), or map
  it with `--option instrument=BTCUSDT` (every order) or `--option instruments=data0:BTCUSDT,data1:ETHUSDT` (a
  label that names no data feed of the file fails `QT-CONFIG-001`).
- If your backtest replays known live orders, pass the live order key to the order:
  `self.buy(price=61000, exectype=bt.Order.Limit, order_key="binance:main:7100001")`. Backtrader keeps it in
  `order.info`, the analyzer writes it, and that order pairs with that live order directly.

Already running the standard `Transactions` analyzer? Its result works too, with less information: it nets every
execution of a data feed in one bar, so each entry becomes one order known from its fill only (no unfilled orders,
no limit prices; the import says `coverage=fills_only`). Give it instead of the `OrderLog` lines, never with
them: both record the same executions, so the import refuses the pair (`QT-CONFIG-001`). Its keys come from
`strategy.datetime.datetime()`, which is in the data feed's own zone (its `tz` when it has one), not UTC: pass that
zone as `--option data_tz=...`.

<!-- check-docs: skip (needs Backtrader) -->
```python
cerebro.addanalyzer(bt.analyzers.Transactions, _name="txn")
strat = cerebro.run()[0]
with open("bt_transactions.json", "w") as f:
    json.dump({k.isoformat(): v for k, v in strat.analyzers.txn.get_analysis().items()}, f)
```

### VectorBT: export the order records

After building the portfolio, write its order records in readable form, as CSV or as JSON records:

<!-- check-docs: skip (needs VectorBT, which queuetruth does not install) -->
```python
import vectorbt as vbt

pf = vbt.Portfolio.from_signals(close, entries, exits, fees=0.001, freq="1h")
pf.orders.records_readable.to_csv("vbt_orders.csv")
# or: pf.orders.records_readable.to_json("vbt_orders.json", orient="records")
# optional, for the P&L check in the import warnings:
pf.trades.records_readable.to_csv("vbt_trades.csv")
```

How the records are read:

- Columns `Order Id`, `Column`, `Timestamp`, `Size`, `Price`, `Fees`, `Side`. Each record is one order with an
  `ack` and one fill of its size at its price at the bar's timestamp; `Fees` is the fee in your cash currency.
- The portfolio needs a `DatetimeIndex`: `Timestamp` must be a time (ISO text, a date alone for daily bars, which
  reads as midnight, or pandas' JSON epoch milliseconds), not a bar number. Naive times are UTC unless you pass
  `--option data_tz=...`. That includes JSON epoch values: pandas writes a naive index's wall-clock time as if it
  were UTC, so a portfolio indexed by naive New York times needs `--option data_tz=America/New_York` for its JSON as
  for its CSV. Leave `data_tz` unset when the index is tz-aware (its epochs are true UTC).
- `Column` is the instrument. A portfolio with several columns (several symbols, or a parameter sweep, where every
  column is a separate backtest) needs `--option column=<label>` to keep one (copy the label as the CSV writes it,
  for a sweep `(10, 'BTCUSDT')`; `--option instrument=NAME` then names it), or
  `--option instruments=<label>:<live instrument>,...` to name each kept column. `instrument` alone over several
  columns fails `QT-CONFIG-001`: it would merge separate backtests into one instrument.
- VectorBT records no order type. They are market orders by default; pass `--option order_type=limit` when your
  live strategy rests limit orders at the prices the backtest fills at. Fill liquidity is `unknown` (the records
  do not say).
- With trade records alone (`pf.trades.records_readable`), the importer derives one entry order per trade and one
  exit order per closed trade at the average prices, with a warning. Export the order records when you can.
- VectorBT stamps a fill with the label of its bar. If your bars are labelled by their open time and the backtest
  fills at the close (VectorBT's default price), the fill happened one bar later: pass `--option fill_delay_bars=1`.

## 2. Import the live fills

`$QT_FIXTURES` is the `tests/fixtures` directory of a queuetruth source checkout: after `git clone
https://github.com/sarptandoven/queuetruth`, run `export QT_FIXTURES=$PWD/queuetruth/tests/fixtures`. The docs check
sets it for you.

The live arm is any canonical live log. The fixtures hold the live run as a captured Binance user-data stream:

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth advanced live import --venue binance --account main --in "$FX/binance_user_stream.jsonl" \
    --instrument-map "$FX/instrument_map.csv" --out live
```

The fixture is one invented BTCUSDT strategy on 1-hour bars on 2024-03-01: seven live orders, of which four filled,
one was cancelled unfilled, one was cancelled after filling 0.008 of 0.02, and one is still open.

## 3. Reconcile

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth advanced reconcile --kind backtrader --sim "$FX/bt_orders.jsonl" --live live --out rec-bt \
    --option timeframe=1h
```

`--sim` is the export (repeatable), `--live` one canonical live-log directory, `--out` the output directory. It
writes `rec-bt/live` (the live log, with instrument ids from it), `rec-bt/sim` (the backtest as a simulator log),
`rec-bt/score` (the `queuetruth advanced score` output), `rec-bt/reconcile.json` (`qt.bot_reconcile`, the same document as
for Freqtrade and Hummingbot) and `rec-bt/reconcile_orders.csv`. The printout starts with the score and the counts:

```text
reconcile (backtrader): 7 live orders, 7 simulator orders, 6 paired (86% of live orders)
score (not interpretable: 5 matched orders in 5 cluster(s), it needs 10 matched orders and 2 clusters): no
significant fill-rate difference within 86400 s: +20.0 pp (95% CI [+0.0, +60.0]); ...
score orders: both 4, neither 0, live_only 0, sim_only 1
categories: match 2, missed_fill 1, partial_fill 1, price 1, timing 1, no_sim_twin 1, no_live_twin 1
sim price advantage BTCUSDT: 0.1962 (quote)
missed-fill notional BTCUSDT: 608 (quote)
```

and then gives one line per order:

```text
[missed_fill] binance:main:7100003: order buy 0.01 BTCUSDT @ 60800: backtest filled 0.01 at 60800 in the 1h candle
opening 2024-03-01T14:00:00.000Z (candle rule); the live order rested 59m28s and ended cancelled without a fill
[price] binance:main:7100004: order sell 0.01 BTCUSDT (market): price live 61190.5 vs backtest 61210.12: the
backtest price is 3.21 bps better (0.1962 quote on 0.01)
[partial_fill] binance:main:7100005: order buy 0.02 BTCUSDT @ 60900: filled quantity live 0.008 vs backtest 0.02
[timing] binance:main:7100006: order buy 0.01 BTCUSDT @ 61100: timing: live filled 2024-03-01T21:30:00.000Z,
outside the backtest fill candle [2024-03-01T20:00:00.000Z, +1h)
[no_live_twin] sim:backtrader:main:7: backtest order buy 0.01 BTCUSDT @ 61300 expired; no live order pairs with it:
the live log has no order of this instrument and side within the pairing window
[no_sim_twin] binance:main:7100007: live order sell 0.01 BTCUSDT @ 61800 open; no backtest order pairs with it: ...
```

The backtest filled the 14:00 buy at 60800 because the bar traded there; the live order rested an hour at the back
of the queue and was cancelled. That is the bar rule's blind spot, and the reason to score against exact queues
(section 6). The market sell has slippage in the backtest (61210.12, rounded to the 0.01 tick), 3.21 bps better than
the live fill. The 18:00 buy filled 0.02 in the backtest and 0.008 live.

VectorBT gives the same account (it has no expired order, so no `no_live_twin`):

<!-- check-docs: run -->
```python
import os

from queuetruth.live.importers.freqtrade import render_reconcile
from queuetruth.live.importers.vectorbt import reconcile_vectorbt

fx = os.path.join(os.environ["QT_FIXTURES"], "live", "backtesters")
report = reconcile_vectorbt(os.path.join(fx, "vbt_orders.csv"), "live", "rec-vbt", timeframe="1h")
print(render_reconcile(report, max_rows=3))
assert report.counts["missed_fill"] == 1 and report.n_paired == 6
```

Every number above is asserted in `test_reconcile_end_to_end`; the
command line gives the same report.

**Pairing.** The backtest and the live run share no order id. A simulated order pairs with the unpaired live order
of the same instrument and side placed nearest to the backtest's submission time (Backtrader: `created`; VectorBT:
the record's timestamp), within `pair_window_ms` (default two bars of `timeframe`, else 60 s); ties go to the
smaller quantity difference. A Backtrader order that carries an `order_key` pairs with that live order first.
Paired simulator orders take the live `order_key`, so `queuetruth advanced score` matches them. A backtest fill stamped
before the live order existed (the bar's time) is moved to the live order's entry time, with the bar time kept in
the fill's `reason`: the scorer's window starts at the live entry.

VectorBT has no submission time, so its reference is the fill bar's timestamp. A live limit order placed more than
`pair_window_ms` before it filled (it rested for hours) is too far from that bar to pair, and pairs with nothing or
with a later order of the same side. When your live orders rest longer than two bars, widen the window
(`--option pair_window_ms=...`, at least the longest rest you expect) and check `pairing_coverage` and the
`no_sim_twin` rows.

**Categories** (first that applies): `no_live_twin`, `no_sim_twin`, `missed_fill` (the backtest filled, live did
not), `extra_live_fill`, `partial_fill` (filled quantities differ), `price` (more than `--price-tol-bps`, default
0.5 bps; positive = the backtest bought cheaper or sold dearer), `timing` (the live first fill is outside the
backtest's fill bar, widened by `--time-tol`; without `timeframe`, more than 60 s from the backtest's fill),
`match`, `both_unfilled`.

**Refusals.** A live arm that is itself a simulator log or a simulated run (venue `sim:*` or `validation_status =
simulated`) fails `QT-CONFIG-001` (arms swapped?) unless `--arms-as-given`. A `--live` path that is not a canonical
live-log directory fails `QT-INPUT-001`; more than one `--live` fails `QT-CONFIG-001`. An export in another shape
fails `QT-INPUT-002` naming the line; an unknown option fails `QT-CONFIG-001` with the list of known ones.

## 4. Options

| Option | Applies to | Meaning |
|---|---|---|
| `timeframe` | both | bar length (`1m`, `5m`, `1h`, `1d`, `1w`): the fill time is known to the bar; default pairing window two bars |
| `fill_delay_bars` | both | move every fill this many bars later (needs `timeframe`) |
| `pair_window_ms` | both | pairing window in milliseconds |
| `instrument`, `instruments` | both | live instrument name of every order, or per data feed / column (`LABEL:NAME,...`) |
| `data_tz` | both | IANA zone of naive timestamps (default UTC) |
| `venue` | both | the live venue (when the live log holds several; for `live import`, the venue of the instrument-map rows) |
| `pair_with` | `live import` | a canonical live-log directory to pair with (`reconcile` takes it as `--live`) |
| `price_exp`, `size_exp` | both | scales of instruments missing from the instrument map (default -9, -8) |
| `column` | VectorBT | keep one column of a multi-column portfolio |
| `order_type` | VectorBT | `market` (default) or `limit` |

Prices and sizes in both libraries are doubles. They are read as decimals rounded to 15 significant digits, then
rounded half-even to the instrument's tick and lot (counted in a warning), as for a Freqtrade backtest.

## 5. Import the backtest alone, then score

`reconcile` runs `score` for you. To build the simulator log yourself, for `queuetruth advanced score --sim-log`, the
referee or the HTML report, import it with the live log to pair with:

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth advanced live import --venue sim-vectorbt --in "$FX/vbt_orders.csv" --instrument-map "$FX/instrument_map.csv" \
    --option pair_with=live --option timeframe=1h --out vbt-sim
queuetruth advanced score --live live --sim-log vbt-sim --out sc-vbt --window 1440m
```

The same in Python is `queuetruth.live.import_sim("vectorbt", ["vbt_orders.csv"], "vbt-sim", pair_with="live",
timeframe="1h", instrument_map=...)`. Without `pair_with`, the simulator orders keep `sim:vectorbt:<account>:<ref>`
keys and match no live order (warned).

## 6. Against exact-queue shadows

A live run tells you what happened once. Exact-queue shadows tell you what would have happened to each of your
orders in the real FIFO queue of an order-level feed, which is the truth a bar backtest approximates. Score the
backtest and the exact arm against the same live log, on the same dataset, and read them side by side.

On the synthetic dataset this runs in seconds. The block writes a VectorBT export that fills every order of the
synthetic live log at its limit price when it is placed: a bar backtester's view when every bar trades through.

<!-- check-docs: run -->
```sh
queuetruth advanced fixtures synth --out synth
```

<!-- check-docs: run -->
```python
import csv
import datetime as dt

from queuetruth.live import read_log, table_rows

orders, events = (table_rows(t) for t in read_log("synth/own_orders/live"))
ack = {e["order_key"]: e["ts_exchange"] for e in events if e["event_type"] == "ack"}
with open("synth_vbt_orders.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Order Id", "Column", "Timestamp", "Size", "Price", "Fees", "Side"])
    for i, o in enumerate(sorted(orders, key=lambda o: ack[o["order_key"]])):
        t = ack[o["order_key"]]
        stamp = dt.datetime.fromtimestamp(t // 10**9, dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
        w.writerow([i, o["instrument"], f"{stamp}.{t % 10**9:09d}+00:00", o["qty_str"], o["px_str"], 0,
                    o["side"].capitalize()])
```

<!-- check-docs: run -->
```sh
queuetruth advanced live import --venue sim-vectorbt --in synth_vbt_orders.csv --out synth-vbt \
    --option pair_with=synth/own_orders/live --option order_type=limit \
    --option instruments=QTAU6:QTAU6,QTBU6:QTBU6 --option size_exp=0
queuetruth advanced score --live synth/own_orders/live --sim-log synth-vbt --data synth --out sc-bar
queuetruth advanced score --live synth/own_orders/live --sim exact --data synth --out sc-exact
queuetruth report --in sc-exact --in sc-bar --out bar-vs-exact.html
```

The exact arm agrees with the live log on all 200 orders (`both` 112, `neither` 88: the synthetic live fills are
exact-engine fills). The bar backtest fills all 200, so it over-fills live by 44.0 pp within the 60 s window: the
88 orders the queue never reached are `sim_only` (`test_backtest_against_exact_queue_shadows` in
`python/tests/cli/test_cli_backtesters.py`). With your own data, use your order-level dataset for `--data` and the
instrument map that matches its `instrument_id`s for both imports:

<!-- check-docs: skip (needs your live log and an order-level dataset) -->
```sh
queuetruth advanced score --live live --sim-log bt-sim --data my-dataset --out sc-backtest
queuetruth advanced score --live live --sim exact --data my-dataset --out sc-exact
queuetruth report --in sc-exact --in sc-backtest --out backtest-vs-exact.html
```

## 7. QuantConnect LEAN

Since 0.3.0. The same report from a LEAN backtest: LEAN's result JSON and its order-events JSON. queuetruth never
imports LEAN: it reads the JSON that LEAN writes. The formats were read from LEAN's public source (release tag 18241
of github.com/QuantConnect/Lean); the fixtures in `tests/fixtures/live/backtesters/` are written from that source by
`make_fixtures.py`, not exported from a real run, so check the counts against your own records the first time.

`queuetruth advanced live import --venue sim-lean` writes the backtest as a simulator log (venue `sim:lean`,
`validation_status = simulated`), with options through `--option KEY=VALUE`. `queuetruth reconcile --kind lean --sim
RESULT_JSON --sim ORDER_EVENTS_JSON --live LIVE_LOG_DIR --out DIR` runs the whole comparison. The Python API is
`queuetruth.live.importers.lean`.

### What LEAN assumes

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

### LEAN 1. Find the backtest's output

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
  price's currency is kept, a zero fee too. A spot crypto buy charged in its base asset (LEAN's Binance fee model
  charges BTC on a BTCUSDT buy) has that fee subtracted from `fillQuantity` by LEAN: the importer logs the gross
  quantity (`fillQuantity` plus the fee) and the fee in the quote currency at the fill price. LEAN does this for spot
  crypto only, so a crypto future's fee in its base asset is not added back. A fee in any other currency is left
  out, with a warning.
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

### LEAN 2. Import the live fills

The live arm is the `live` log of [step 2](#2-import-the-live-fills) above: the same invented BTCUSDT strategy, seven
live orders on 1-hour bars on 2024-03-01.

### LEAN 3. Reconcile

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth advanced reconcile --kind lean --sim "$FX/lean_result.json" --sim "$FX/lean_order_events.json" --live live \
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

Every number above is asserted in `test_reconcile_end_to_end`; the command line
gives the same report.

**Pairing, categories and refusals** are those of the Backtrader and VectorBT guide
([section 3](reconcile-backtrader-vectorbt.md#3-reconcile)). The reference time of a LEAN order is its `time`
(the submission). A paired order takes the live `order_key`; a backtest fill before the live order's entry is
moved to that entry, with the bar time kept in the fill's `reason`.

### LEAN 4. Options

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

### LEAN 5. Import the backtest alone, then score

To build the simulator log yourself, for `queuetruth advanced score --sim-log`, the referee or the HTML report, import it
with the live log to pair with:

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth advanced live import --venue sim-lean --in "$FX/lean_result.json" --in "$FX/lean_order_events.json" \
    --instrument-map "$FX/instrument_map.csv" --option pair_with=live --option timeframe=1h --out lean-sim
queuetruth advanced score --live live --sim-log lean-sim --out sc-lean --window 1440m
```

The same in Python is `queuetruth.live.import_sim("lean", [result, events], "lean-sim", pair_with="live",
timeframe="1h", instrument_map=...)`. Without `pair_with`, the simulator orders keep `sim:lean:<account>:<id>` keys
and match no live order (warned). To score the backtest next to exact-queue shadows on an order-level dataset,
follow [section 6](#6-against-exact-queue-shadows)
with this simulator log.

### LEAN limits

- On bar data a LEAN fill time is a bar's end. Without `timeframe` it is read as an exact time, an hour late for an
  hourly limit fill. Give `timeframe` for bar data; leave it out for tick data.
- Contingent orders are warned, not modelled: a child order's `ack` can come before its trigger.
- The orders section holds an order's final price and quantity. An order updated while it rested
  (`updateSubmitted`) is logged at its last limit price.
- Option exercises and combo orders are refused. Import a backtest of single-instrument orders.
- `score` reads only resting limit orders with a usable entry time; market and stop orders are explained by the
  reconciliation but not scored.

## Limits

- The fill time of both backtesters is a bar's label. A live fill inside the bar is on time only when you give
  `timeframe`; bars labelled by their close time, or fills at the close of bars labelled by their open, need
  `fill_delay_bars` or `--time-tol`.
- VectorBT pairs by fill time (it has no submission time): long-resting live limit orders need a wider
  `pair_window_ms`.
- A Backtrader order with several execution bits keeps each bit as a fill; a `Transactions` dump nets them per bar.
- VectorBT records only executions, so it never contributes an unfilled order: a live order it has no record for
  is `no_sim_twin`, not `missed_fill`.
- `score` reads only resting limit orders with a usable entry time; market orders are explained by the
  reconciliation but not scored.

<details>
<summary>Evidence: the tests behind this page</summary>

<ul>
<li>3. Reconcile: <code>python/tests/live/test_backtesters.py</code></li>
<li>3. Reconcile: <code>python/tests/cli/test_cli_backtesters.py</code></li>
<li>LEAN 3. Reconcile: <code>python/tests/live/test_lean.py</code></li>
<li>LEAN 3. Reconcile: <code>python/tests/cli/test_cli_backtesters.py</code></li>
</ul>

</details>
