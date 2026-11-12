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
reasons are `queuetruth advanced reconcile`'s, the scorecard, time to fill, markouts and P&L gap are `queuetruth advanced score`'s.

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
7 of the backtest's 10 fills also filled live (70%, 95% CI 40% to 89%, orders counted as independent: 2 instrument-day clusters, fewer than 5); live filled 10 order(s) in all.
Fills only the backtest made: 3 (2 the live order rested and did not fill; 1 no live order to pair it with (none was placed live)).
Fills only live made: 3 (3 no backtest order to pair it with (the backtest did not place it)).
discordant fills: the backtest and live fill as many orders: 3 backtest-only against 3 live-only, p = 1, orders counted as independent: 2 instrument-day clusters, fewer than 5.
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
5 of the backtest's 6 fills also filled live (83%, 95% CI 44% to 97%, orders counted as independent: 1 instrument-day cluster, fewer than 5); live filled 5 order(s) in all.
Fills only the backtest made: 1 (1 the live order rested and did not fill).
discordant fills: the backtest fills more orders than live: 1 backtest-only against 0 live-only, p = 1, orders counted as independent: 1 instrument-day cluster, fewer than 5.
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
83 of the backtest's 101 fills also filled live (82%, 95% CI 74% to 88%, orders counted as independent: 4 instrument-day clusters, fewer than 5); live filled 112 order(s) in all.
Fills only the backtest made: 18 (18 the live order did not fill within the window).
Fills only live made: 29 (29 the backtest order did not fill within the window).
discordant fills: the backtest fills fewer orders than live: 18 backtest-only against 29 live-only, p = 0.14, orders counted as independent: 4 instrument-day clusters, fewer than 5.
P&L gap per order (marked to the dataset's mid): the simulator's P&L is 13.72 USD per order too high (95% CI +4.81 to +23.43) at the 1s markout, mostly from fills it grants that live did not get (+17.00 USD per order); 200 orders
time to fill: still unfilled after 1s (Kaplan-Meier, cancels censored): live 95% [93%, 98%] vs sim 88% [85%, 91%]; sim minus live -7.5 pp (95% CI -10.8 to -4.4); log-rank p = 0.189 (by instrument, 51 clusters)
markouts: sim minus live, ticks of mid, orders filled in both (95% CI): 100ms -0.01 [-0.04, +0.02], 1s -0.01 [-0.04, +0.00], 10s +0.00 [-0.02, +0.02]
latency: median entry latency 30us (p10 30us, p90 30us, 200 orders), exchange ack minus local send, fitted from the live log
exact queue: the live orders replayed in the dataset's real queue fill as often as live (95% CI +0.0 to +0.0 pp).
discordant fills: the exact queue and live fill the same orders (no order filled on one side only).
latency first: with the live latency applied before the queue (median 30us, fitted from the live log), the exact queue fills as often as live (95% CI +0.0 to +0.0 pp); entering at the send time with no latency it fills 0.5 pp less often than live: the latency explains all of that gap.
```

The live log here was generated from the synthetic exact queue, which is why the exact queue matches it. On your
own live run, a gap between the exact queue and live points at something outside the queue (your own impact,
missing data); a gap between the backtest and the exact queue is the backtester's fill model. The latency is
taken out first (the `latency first` line), so a latency gap is not reported as a queue gap.

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

- **Fills that happened live.** Of the orders the backtest filled, how many also filled live. Orders on the same
  instrument and day are not independent (one market, one queue), so the 95% interval resamples instrument-by-day
  clusters (a cluster bootstrap, 1,000 resamples, the same every run). With fewer than 5 such clusters a bootstrap
  says nothing, so the line falls back to the Wilson interval and says so: `orders counted as independent: 2
  instrument-day clusters, fewer than 5`. Read that one as the narrowest the interval could be.
- **Discordant fills.** Paired by order, each run filled or did not. The orders only one run filled are the
  discordant pairs: `b` the backtest filled and live did not, `c` the reverse. Equal counts mean the backtest fills
  as often as live, whatever it gets wrong order by order. The p-value is McNemar's test on `b` and `c`:
  Obuchowski's cluster-adjusted form with at least 5 instrument-day clusters, else the exact test with orders
  counted as independent, as the line says. With `--data` the same line follows for the exact queue against live.
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
- **Latency first.** With `--data` and a measured latency, the latency is fitted before the queue is scored, as
  `queuetruth calibrate-live --latency-from-log` does: the live orders are replayed in the exact queue entering at
  their send time, once with no latency and once with the median latency added. The first gap to live includes the
  latency; the second is what is left for the queue. The line says which share of the first the latency explains.

Files in the output directory (default `qt-compare`):

| File | What it holds |
|---|---|
| `orders.csv` | one row per order: symbol, side, prices as decimals, times in the venue's local time (`tz`), both outcomes, the reason, the price and P&L gap |
| `compare.json` | the `qt.compare` document: every answer line and the numbers behind it |
| `compare.html` | with `--html`, the report page |
| `reconcile.json`, `reconcile_orders.csv`, `live/`, `sim/`, `score/` | for bots and bar backtesters, the reconciliation as `queuetruth advanced reconcile` writes it |
| `live-import/`, `backtest/`, `score/`, `exact/` | for event-driven simulators, the imported logs and the scorecards |
| `exact-send/`, `exact-latency/` | with `--data` and a measured latency, the exact queue entering at the send time with no latency and with the fitted latency |
| `watch.json` | with `--watch` or `--once`, the baseline window and the last check (see below) |

In `compare.json`, `fills.interval` says which interval was used (`method` is `instrument_day` or `independent`,
with `n_clusters`), `discordance` and `exact_discordance` hold `backtest_only`, `live_only`, `p`, `test`
(`obuchowski`, `block_bootstrap` or `mcnemar_exact`) and, when clustered, the interval of the fill-rate gap, and
`latency_first` holds the two gaps and `share_explained`.

## Watch for drift

A backtest that matched live last month can stop matching: the market changes, the venue changes, your strategy
changes. `--watch` keeps the comparison running and tells you when the gap moves:

```sh
queuetruth compare --backtest BACKTEST --live LIVE [--data DATASET] --out qt-compare --watch --interval 5min
```

It checks the live path every `--interval` (a directory or a file that grows) and runs the comparison again only
when a file in it changed. The first check is the baseline window: the orders already there. Every later check
takes the orders entered since and prints one line with the drift of three gaps since the baseline, each with its
95% interval:

- **fill-share gap**: the share of orders the backtest fills minus the share live fills, in percentage points;
- **P&L gap per order**: the backtest's P&L minus live's per order, marked to the mid (needs `--data`);
- **markout gap 1s**: the backtest's first-fill markout at 1 s minus live's, in ticks (needs `--data`).

Each interval resamples instrument-by-day clusters of each window (orders counted as independent, and labelled so,
with fewer than 5 clusters in a window). When an interval excludes zero and the drift is at least its threshold,
an `ALERT:` line follows:

```text
2026-11-12 14:05:00 UTC  drift since the baseline: 112 new paired orders (baseline 300): fill-share gap +6.1 pp [+2.3, +9.8]; P&L gap per order +0.84 USD [-0.12, +1.80]; markout gap 1s -0.31 ticks [-0.52, -0.09] (clustered by instrument and day, 9 and 6 clusters)
ALERT: fill-share gap moved +6.1 pp since the baseline (95% CI +2.3 to +9.8, clustered by instrument and day, 9 and 6 clusters), at least --alert-pp 2
ALERT: markout gap 1s moved -0.31 ticks since the baseline (95% CI -0.52 to -0.09, clustered by instrument and day, 9 and 6 clusters), at least --alert-ticks 0.25
```

(an illustration of the format; the numbers are not from a real run). The thresholds are `--alert-pp` (default
2, the scorer's equivalence margin), `--alert-ticks` (default 0.25) and `--alert-pnl` (in the P&L currency per order;
no default, so the P&L gap alerts only when you set one).

`--once` runs a single check and exits: 0 no alert, 1 alert, 2 error, like `queuetruth fit`. When nothing changed
since the last check it says so and exits with the last check's code. The state is a small file, `watch.json` in
the `--out` directory: the baseline's per-cluster sums and the last check. Delete it to start a new baseline.

<!-- check-docs: run -->
```sh
queuetruth compare --backtest orders_report.csv --backtest fills_report.csv \
  --live synth/own_orders/live --data synth --out cmp-watch --once
queuetruth compare --backtest orders_report.csv --backtest fills_report.csv \
  --live synth/own_orders/live --data synth --out cmp-watch --once
```

The first check prints the baseline; the second finds no new live records.

There is no network and no notification: `compare --watch` prints and sets its exit code, and you wire that to what
you already use. From cron, every 15 minutes, keeping the lines in a log and mailing the alerts through cron's own
mail:

```sh
*/15 * * * * cd /srv/strategy && queuetruth compare --backtest bt.json --live live/ --out qt-compare --once >> watch.log || grep ALERT watch.log | tail -3
```

From a CI schedule (GitHub Actions here), where a failed job is the alert:

```yaml
on:
  schedule:
    - cron: "17 * * * *"
jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - run: pip install queuetruth
      # restore qt-compare/watch.json from your cache or storage first, so the baseline survives between runs
      - run: queuetruth compare --backtest bt.json --live live/ --out qt-compare --once
```

Exit code 1 fails the job; exit code 2 (an unreadable file, say) fails it too, with the error printed.

## When `compare` asks for something

`compare` asks only for what it cannot read from the files, and says what is missing:

- **Which backtester.** When no file says, pass `--backtester NAME`.
- **Which venue.** When the live files do not say (a venue's CSV with generic columns), pass `--venue V`.
- **An instrument map.** Never for two logs of the same venue. With `--data`, live instruments are matched to the
  dataset's symbols (`BTC/USDT` matches `BTCUSDT`); a name that matches none is named in the error, and
  `--instrument-map FILE` says which dataset instrument it is.
- **Keys.** An hftbacktest or NautilusTrader backtest whose orders carry no live order key was not run on the
  live schedule; the error says so. Run it on the schedule `queuetruth advanced export replay-schedule` writes.

`queuetruth advanced reconcile` and `queuetruth advanced score` stay available with every option for scripted use.
