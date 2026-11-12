# Compare my backtest with my live run

**Goal:** put your backtest next to the live run of the same strategy and see, order by order, where they part:
how many of the backtest's fills happened live, which fills only one side made and why, what that is worth, how
fast orders filled, how the fills marked out and how much latency the live run had. One command, whatever your
backtester and wherever your live records come from:

```sh
queuetruth compare --backtest BACKTEST --live LIVE [--data DATASET]
```

**You need:** queuetruth 0.3.0 or later, your backtester's output, and the live records of the same strategy over
the same days. Market data is optional: with an order-level dataset (`--data`) every fill is also marked to the
mid, which gives the markouts and the P&L gap per order, and the live orders are replayed in the exact queue.

`compare` is the everyday front door. It reads which backtester wrote the backtest and what the live records are,
then runs the comparison queuetruth already has for that pair. Nothing is computed twice: the pairing and the
reasons are `queuetruth reconcile`'s, the scorecard, time to fill, markouts and P&L gap are `queuetruth score`'s.

| Backtest | Live records | What `compare` runs |
|---|---|---|
| Freqtrade backtest result (`.zip`, `.json`) or dry-run database | the bot's trades database and log | `reconcile --kind freqtrade` |
| Hummingbot paper-bot database | the live bot's database or trades CSV | `reconcile --kind hummingbot` |
| Backtrader `OrderLog` lines, VectorBT records, LEAN result and order events | a venue export, a bot's records or a canonical live log | `live import` when needed, then `reconcile --kind backtrader / vectorbt / lean` |
| hftbacktest snapshots, NautilusTrader reports or events | a venue export or a canonical live log | `live import` when needed, `live import --venue sim-*`, then `score --sim-log` (and `score --sim exact` with `--data`) |

Bots and bar backtesters are paired with their live twin by the reconciler, which knows how each one places and
fills orders. Event-driven simulators run on the live schedule, so their orders carry the live order keys and are
compared key by key.

The examples below run on synthetic fixtures that ship with queuetruth (no real orders, fills or market data). If
you installed from PyPI, clone the repository and set `QT_FIXTURES` to its `tests/fixtures` directory; the docs
check sets it for you.

## 1. A Freqtrade backtest against the live bot

<!-- check-docs: run -->
```python
import os
import sqlite3

fx = os.path.join(os.environ["QT_FIXTURES"], "live", "freqtrade")
with open(os.path.join(fx, "tradesv3.live.sql")) as f:
    sqlite3.connect("tradesv3.sqlite").executescript(f.read())
```

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/freqtrade"
queuetruth compare --backtest "$FX/backtest-result-2024-03-01_14-00-00.zip" \
  --live tradesv3.sqlite --live "$FX/freqtrade.log" --out cmp-ft --max-rows 3
```

The answer comes first:

```text
7 of the backtest's 10 fills also filled live (70%, 95% CI 40% to 89%, orders counted as independent); live filled 10 order(s) in all.
Fills only the backtest made: 3 (2 the live order rested and did not fill; 1 no live order to pair it with (none was placed live)).
Fills only live made: 3 (3 no backtest order to pair it with (the backtest did not place it)).
P&L gap BTC/USDT: on fills both made the backtest's prices were better by 0.0448 (quote).
P&L gap ETH/USDT: fills only the backtest made are 204.717 of notional (quote).
time to fill: still unfilled after 1s (Kaplan-Meier, cancels censored): live 100% [100%, 100%] vs sim 12% [0%, 43%]; ...
markouts: need --data (fills are marked to the dataset's mid at 100 ms, 1 s and 10 s).
latency: not measured: no live order has both a local send time and an exchange ack time (...)
```

and then the orders that differ, fills only one side made first, each with the reconciler's explanation. No
instrument map, no `--kind`, no import step: the backtest is read as Freqtrade's from the file, and the database and
log as the bot's own records.

## 2. A VectorBT backtest against a venue export

A Backtrader, VectorBT or LEAN backtest is compared with the venue's own records. `compare` imports them first
(here a Binance user-data stream), then pairs the orders:

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/backtesters"
queuetruth compare --backtest "$FX/vbt_orders.csv" --live "$FX/binance_user_stream.jsonl" \
  --option timeframe=1h --out cmp-vbt --html
```

```text
5 of the backtest's 6 fills also filled live (83%, 95% CI 44% to 97%, orders counted as independent); live filled 5 order(s) in all.
Fills only the backtest made: 1 (1 the live order rested and did not fill).
P&L gap BTCUSDT: on fills both made the backtest's prices were better by 0.19623456789 (quote); fills only the backtest made are 608 of notional (quote).
...
```

`--option` passes importer options to the backtest's importer (`timeframe` is the bar size the backtest ran on).
`--html` writes `cmp-vbt/compare.html`, one self-contained page with the answer and every order.

## 3. A NautilusTrader backtest against a live log, with market data

An event-driven backtest ran on the live schedule, so each of its orders has the key of the live order it replays.
The next block stands in for one: it writes NautilusTrader orders and fills reports from the synthetic live log,
filling a few orders that live did not fill and missing a few that live filled.

<!-- check-docs: run -->
```python
import csv

import pyarrow.parquet as pq

from queuetruth import api

api.fixtures_synth("synth")
orders = pq.read_table("synth/own_orders/live/orders.parquet").to_pylist()
events = pq.read_table("synth/own_orders/live/events.parquet").to_pylist()
by_key = {}
for e in events:
    by_key.setdefault(e["order_key"], []).append(e)

with open("orders_report.csv", "w", newline="") as fo, open("fills_report.csv", "w", newline="") as ff:
    wo, wf = csv.writer(fo), csv.writer(ff)
    wo.writerow(["client_order_id", "venue_order_id", "instrument_id", "side", "type", "quantity", "price",
                 "time_in_force", "post_only", "reduce_only", "status", "filled_qty", "avg_px", "ts_init", "ts_last"])
    wf.writerow(["client_order_id", "trade_id", "order_side", "last_qty", "last_px", "liquidity_side",
                 "commission", "ts_event", "instrument_id"])
    for i, o in enumerate(sorted(orders, key=lambda r: r["order_key"])):
        ev = by_key[o["order_key"]]
        ack = next(e["ts_exchange"].value for e in ev if e["event_type"] == "ack")
        end = max(e["ts_exchange"].value for e in ev if e["ts_exchange"] is not None)
        fills = [e["ts_exchange"].value for e in ev if e["event_type"] == "fill"]
        if fills:  # the backtest misses every fifth live fill
            filled, ts = i % 5 != 0, fills[-1]
        else:  # and fills every third order live did not fill, 1 s after its ack
            filled, ts = i % 3 == 0, ack + 1_000_000_000
        side, inst = o["side"].upper(), o["instrument"] + ".SIM"
        status = "FILLED" if filled else "CANCELED"
        wo.writerow([o["order_key"], f"V{i}", inst, side, "LIMIT", o["orig_qty"], o["px_str"], "GTC", False, False,
                     status, o["orig_qty"] if filled else 0, o["px_str"] if filled else "", ack, max(end, ts + 1)])
        if filled:
            wf.writerow([o["order_key"], f"T{i}", side, o["orig_qty"], o["px_str"], "MAKER", "", ts, inst])
```

<!-- check-docs: run -->
```sh
queuetruth compare --backtest orders_report.csv --backtest fills_report.csv \
  --live synth/own_orders/live --data synth --out cmp-nt --max-rows 3
```

With `--data` every line has its number:

```text
83 of the backtest's 101 fills also filled live (82%, 95% CI 74% to 88%, orders counted as independent); live filled 112 order(s) in all.
Fills only the backtest made: 18 (18 the live order did not fill within the window).
Fills only live made: 29 (29 the backtest order did not fill within the window).
P&L gap per order (marked to the dataset's mid): the simulator's P&L is 13.72 USD per order too high (95% CI +4.81 to +23.43) at the 1 s markout, mostly from fills it grants that live did not get (+17.00 USD per order); 200 orders
time to fill: still unfilled after 1s (Kaplan-Meier, cancels censored): live 95% [93%, 98%] vs sim 88% [85%, 91%]; sim minus live -7.5 pp (95% CI -10.8 to -4.4); log-rank p = 0.189 (by instrument, 51 clusters)
markouts: sim minus live, ticks of mid, orders filled in both (95% CI): 100ms -0.01 [-0.04, +0.02], 1s -0.01 [-0.04, +0.00], 10s +0.00 [-0.02, +0.02]
latency: median entry latency 0.03 ms (p10 0.03 ms, p90 0.03 ms, 200 orders), exchange ack minus local send, fitted from the live log
exact queue: the live orders replayed in the dataset's real queue fill as often as live (95% CI +0.0 to +0.0 pp).
```

The live log here was generated from the synthetic exact queue, which is why the exact queue matches it. On your
own live run, a gap between the exact queue and live points at something outside the queue (latency, your own
impact, missing data); a gap between the backtest and the exact queue is the backtester's fill model.

## 4. From Python

<!-- check-docs: run -->
```python
import queuetruth as qt

res = qt.compare(["orders_report.csv", "fills_report.csv"], "synth/own_orders/live", data="synth", out="cmp-py")
print(res.summary)
df = res.table()  # one row per order: symbols, decimal prices, venue-local times
print(df[df.outcome != "both"][["order", "symbol", "side", "price_gap", "pnl_gap", "why"]].head())
```

`res.doc` is the `qt.compare` document (also in `cmp-py/compare.json`), `res.to_html()` the report page, and in a
notebook the result shows that page.

## Reading the answer

- **Fills that happened live.** Of the orders the backtest filled, how many also filled live. The interval is a
  95% Wilson interval that counts orders as independent; orders placed close together are not, so read it as the
  narrowest it could be. The scorecard's cluster-robust test is in `score/summary.json`.
- **Fills only one side made, and why.** For bots and bar backtesters the reasons are the reconciler's categories
  (the live order rested and did not fill, the bot never placed the order, and so on); the full sentence for each
  order is in the `why` column. For event-driven simulators the order either filled within the window or did not.
- **P&L gap.** Without data: how much better the backtest's prices were on fills both runs made, and the notional
  of the fills only the backtest made. With `--data`: each order's fills marked to the dataset's mid, with the
  gap per order and its interval (the `pnl_gap` column, backtest minus live).
- **Time to fill.** Kaplan-Meier curves of the time to the first fill in each run, cancels censored, with a
  clustered log-rank test (see [Validate a backtest against live fills](validate-against-live.md#4-time-to-fill-survival-curves)).
- **Markouts.** At 100 ms, 1 s and 10 s, on the orders both runs filled; they need `--data`.
- **Latency.** The median time from your send to the exchange's ack, measured on the live log when it has both
  times. To fit your backtester's latency with it, see `queuetruth fit` ([Fit my backtester's fill model](fit-my-backtester.md)).

Files in the output directory (default `qt-compare`):

| File | What it holds |
|---|---|
| `orders.csv` | one row per order: symbol, side, prices as decimals, times in the venue's local time (`tz`), both outcomes, the reason, the price and P&L gap |
| `compare.json` | the `qt.compare` document: every answer line and the numbers behind it |
| `compare.html` | with `--html`, the report page |
| `reconcile.json`, `reconcile_orders.csv`, `live/`, `sim/`, `score/` | for bots and bar backtesters, the reconciliation as `queuetruth reconcile` writes it |
| `live-import/`, `backtest/`, `score/`, `exact/` | for event-driven simulators, the imported logs and the scorecards |

## When `compare` asks for something

`compare` asks only for what it cannot read from the files, and says what is missing:

- **Which backtester.** When no file says, pass `--backtester NAME`.
- **Which venue.** When the live files do not say (a venue's CSV with generic columns), pass `--venue V`.
- **An instrument map.** Never for two logs of the same venue. With `--data`, live instruments are matched to the
  dataset's symbols (`BTC/USDT` matches `BTCUSDT`); a name that matches none is named in the error, and
  `--instrument-map FILE` says which dataset instrument it is.
- **Keys.** An hftbacktest or NautilusTrader backtest whose orders carry no live order key was not run on the
  live schedule; the error says so. Run it on the schedule `queuetruth export replay-schedule` writes.

`queuetruth reconcile` and `queuetruth score` stay available with every option for scripted use.
