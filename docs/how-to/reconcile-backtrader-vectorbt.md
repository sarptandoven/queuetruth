# Reconcile a Backtrader or VectorBT backtest with live fills

**Goal:** bring the orders and fills of a Backtrader or VectorBT backtest into queuetruth, pair each simulated
order with the live order it stands for, and get a per-order account of the differences: which orders the backtest
filled that the market did not, which fills came at another price or another time, and which orders exist on one
side only. Then score the backtest, against your live fills or against exact-queue shadows.

**You need:** one export of the backtest (a few lines added to your Backtrader script, or one line after your
VectorBT portfolio), and your live orders and fills as a canonical live log, written by `queuetruth live import`
from your venue's or broker's records ([Import fills from my venue or broker](import-live-logs.md)). queuetruth
never imports Backtrader or VectorBT: it reads the exported JSON or CSV. The formats below were read from the
projects' public source (Backtrader 1.9.78.123, VectorBT 0.28.1); the fixtures in
`tests/fixtures/live/backtesters/` are written from that source by `make_fixtures.py`, not exported from a real
run, so check the counts against your own records the first time.

**From the command line or Python.** `queuetruth live import --venue sim-backtrader | sim-vectorbt` writes the
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

Copy this analyzer into your script and add it to Cerebro. It writes one JSON line per `notify_order` and
`notify_trade` call: the order's reference, data feed, status, type, size, prices, times and the execution bits that
are new in that notification (`order.executed.iterpending()`, the same source as Backtrader's own `Transactions`
analyzer).

<!-- check-docs: skip (needs Backtrader, which queuetruth does not install) -->
```python
import json
import math

import backtrader as bt


class QtOrderLog(bt.Analyzer):
    """Every order and trade notification as one JSON line, for queuetruth live import --venue sim-backtrader."""

    params = (("path", "bt_orders.jsonl"),)

    def start(self):
        self._f = open(self.p.path, "w", encoding="utf-8")

    def stop(self):
        self._f.close()

    @staticmethod
    def _t(num):
        return bt.num2date(num).isoformat() if num else None

    @staticmethod
    def _n(v):
        return v if v is not None and math.isfinite(v) else None

    def _data(self, data):
        return data._name or "data%d" % next(i for i, d in enumerate(self.strategy.datas) if d is data)

    def notify_order(self, order):
        bits = [
            {"dt": self._t(b.dt), "size": b.size, "price": b.price, "comm": b.comm}
            for b in order.executed.iterpending()
        ]
        row = {
            "event": "order",
            "ref": order.ref,
            "data": self._data(order.data),
            "status": order.getstatusname(),
            "ordtype": order.ordtypename(),
            "exectype": order.getordername(),
            "size": order.created.size,
            "price": self._n(order.created.price),
            "pricelimit": self._n(order.created.pricelimit),
            "created": self._t(order.created.dt),
            "dt": self._t(order.data.datetime[0]),
            "valid": self._t(order.valid) if order.valid else None,
            "exbits": bits,
            "order_key": order.info.get("order_key"),
        }
        self._f.write(json.dumps(row) + "\n")

    def notify_trade(self, trade):
        row = {
            "event": "trade",
            "ref": trade.ref,
            "data": self._data(trade.data),
            "status": trade.status_names[trade.status],
            "dtopen": self._t(trade.dtopen),
            "dtclose": self._t(trade.dtclose),
            "size": trade.size,
            "price": trade.price,
            "pnl": trade.pnl,
            "pnlcomm": trade.pnlcomm,
        }
        self._f.write(json.dumps(row) + "\n")


cerebro = bt.Cerebro()
# ... your data feeds and strategy ...
cerebro.addanalyzer(QtOrderLog, path="bt_orders.jsonl")
cerebro.run()
```

How the lines are read:

- One order per `ref`. `submit_sent` at `created` (the bar the strategy placed it on), `ack` at the bar of the
  `Accepted` notification, one fill per execution bit (fee = its `comm`), and `cancel_ack`, `expire` or `reject`
  (`Margin`: not enough cash, `Rejected`) at the bar of that notification. `Created` notifications are counted.
- `Market` and `Close` orders are market orders, `Limit` a limit order at `price`, `Stop` and `StopTrail` stop
  orders, `StopLimit` a stop-limit at `pricelimit`, `StopTrailLimit` a stop-limit without a limit price (it
  trails). Time in force is `gtc`, or `gtd` when the order has a `valid` time.
- Trade lines are positions, not orders: they are counted and their closed `pnlcomm` summed in a warning.
- Backtrader's datetimes are naive. With a data feed that has a `tz`, `num2date` gives UTC, the default. Otherwise
  they are in your data's own zone: pass `--option data_tz=Europe/Berlin` (any IANA name).
- Use the name of the live instrument as the data feed's `name` (`cerebro.adddata(data, name="BTCUSDT")`), or map
  it with `--option instrument=BTCUSDT` (every order) or `--option instruments=data0:BTCUSDT,data1:ETHUSDT`.
- If your backtest replays known live orders, pass the live order key to the order:
  `self.buy(price=61000, exectype=bt.Order.Limit, order_key="binance:main:7100001")`. Backtrader keeps it in
  `order.info`, the analyzer writes it, and that order pairs with that live order directly.

Already running the standard `Transactions` analyzer? Its result works too, with less information: it nets every
execution of a data feed in one bar, so each entry becomes one order known from its fill only (no unfilled orders,
no limit prices; the import says `coverage=fills_only`).

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
- The portfolio needs a `DatetimeIndex`: `Timestamp` must be a time (ISO text, or pandas' JSON epoch milliseconds),
  not a bar number. Naive times are UTC unless you pass `--option data_tz=...`.
- `Column` is the instrument. A portfolio with several columns (several symbols, or a parameter sweep, where every
  column is a separate backtest) needs `--option column=<label>` to keep one, or
  `--option instruments=<label>:<live instrument>,...` to name each kept column.
- VectorBT records no order type. They are market orders by default; pass `--option order_type=limit` when your
  live strategy rests limit orders at the prices the backtest fills at. Fill liquidity is `unknown` (the records
  do not say).
- With trade records alone (`pf.trades.records_readable`), the importer derives one entry order per trade and one
  exit order per closed trade at the average prices, with a warning. Export the order records when you can.
- VectorBT stamps a fill with the label of its bar. If your bars are labelled by their open time and the backtest
  fills at the close (VectorBT's default price), the fill happened one bar later: pass `--option fill_delay_bars=1`.

## 2. Import the live fills

The live arm is any canonical live log. The fixtures hold the live run as a captured Binance user-data stream:

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth live import --venue binance --account main --in "$FX/binance_user_stream.jsonl" \
    --instrument-map "$FX/instrument_map.csv" --out live
```

The fixture is one invented BTCUSDT strategy on 1-hour bars on 2024-03-01: seven live orders, of which four filled,
one was cancelled unfilled, one was cancelled after filling 0.008 of 0.02, and one is still open.

## 3. Reconcile

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth reconcile --kind backtrader --sim "$FX/bt_orders.jsonl" --live live --out rec-bt \
    --option timeframe=1h
```

`--sim` is the export (repeatable), `--live` one canonical live-log directory, `--out` the output directory. It
writes `rec-bt/live` (the live log, with instrument ids from it), `rec-bt/sim` (the backtest as a simulator log),
`rec-bt/score` (the `queuetruth score` output), `rec-bt/reconcile.json` (`qt.bot_reconcile`, the same document as
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

Every number above is asserted in `test_reconcile_end_to_end` (`python/tests/live/test_backtesters.py`); the
command line gives the same report (`python/tests/cli/test_cli_backtesters.py`).

**Pairing.** The backtest and the live run share no order id. A simulated order pairs with the unpaired live order
of the same instrument and side placed nearest to the backtest's submission time (Backtrader: `created`; VectorBT:
the record's timestamp), within `pair_window_ms` (default two bars of `timeframe`, else 60 s); ties go to the
smaller quantity difference. A Backtrader order that carries an `order_key` pairs with that live order first.
Paired simulator orders take the live `order_key`, so `queuetruth score` matches them. A backtest fill stamped
before the live order existed (the bar's time) is moved to the live order's entry time, with the bar time kept in
the fill's `reason`: the scorer's window starts at the live entry.

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

`reconcile` runs `score` for you. To build the simulator log yourself, for `queuetruth score --sim-log`, the
referee or the HTML report, import it with the live log to pair with:

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth live import --venue sim-vectorbt --in "$FX/vbt_orders.csv" --instrument-map "$FX/instrument_map.csv" \
    --option pair_with=live --option timeframe=1h --out vbt-sim
queuetruth score --live live --sim-log vbt-sim --out sc-vbt --window 1440m
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
queuetruth fixtures synth --out synth
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
queuetruth live import --venue sim-vectorbt --in synth_vbt_orders.csv --out synth-vbt \
    --option pair_with=synth/own_orders/live --option order_type=limit \
    --option instruments=QTAU6:QTAU6,QTBU6:QTBU6 --option size_exp=0
queuetruth score --live synth/own_orders/live --sim-log synth-vbt --data synth --out sc-bar
queuetruth score --live synth/own_orders/live --sim exact --data synth --out sc-exact
queuetruth report --in sc-exact --in sc-bar --out bar-vs-exact.html
```

The exact arm agrees with the live log on all 200 orders (`both` 112, `neither` 88: the synthetic live fills are
exact-engine fills). The bar backtest fills all 200, so it over-fills live by 44.0 pp within the 60 s window: the
88 orders the queue never reached are `sim_only` (`test_backtest_against_exact_queue_shadows` in
`python/tests/cli/test_cli_backtesters.py`). With your own data, use your order-level dataset for `--data` and the
instrument map that matches its `instrument_id`s for both imports:

<!-- check-docs: skip (needs your live log and an order-level dataset) -->
```sh
queuetruth score --live live --sim-log bt-sim --data my-dataset --out sc-backtest
queuetruth score --live live --sim exact --data my-dataset --out sc-exact
queuetruth report --in sc-exact --in sc-backtest --out backtest-vs-exact.html
```

## Limits

- The fill time of both backtesters is a bar's label. A live fill inside the bar is on time only when you give
  `timeframe`; bars labelled by their close time, or fills at the close of bars labelled by their open, need
  `fill_delay_bars` or `--time-tol`.
- A Backtrader order with several execution bits keeps each bit as a fill; a `Transactions` dump nets them per bar.
- VectorBT records only executions, so it never contributes an unfilled order: a live order it has no record for
  is `no_sim_twin`, not `missed_fill`.
- `score` reads only resting limit orders with a usable entry time; market orders are explained by the
  reconciliation but not scored.
