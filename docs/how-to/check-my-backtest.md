# Check my backtest against the real queue

**Goal:** find out how wrong your backtest's fills are, in one command. `queuetruth check` replays your backtest's
own resting limit orders, at their own times and prices, in the exact queue of an order-level dataset, and compares
them order by order: which fills were real, which were phantom (the backtest filled the order, the real queue did
not), how long the fills took, and what that does to P&L.

**You need:** queuetruth 0.4.0 or later, your backtester's output file, and order-level (MBO) data that covers the
backtest's days, as a
queuetruth dataset (`queuetruth data add`; see [Replay a public IEX DEEP+ day](iex-public-demo.md) for free data).
Without data, `check` estimates from the public atlas instead ([without order-level data](#without-order-level-data)).
`check` reads these outputs:

| Backtester | File |
|---|---|
| hftbacktest | order snapshots (JSON lines, CSV or Parquet; the format of `live import --venue sim-hftbacktest`) |
| NautilusTrader | `generate_orders_report` / `generate_fills_report` output, or order event files |
| Backtrader | the `qt.backtrader.OrderLog` lines ([Reconcile a Backtrader or VectorBT backtest](reconcile-backtrader-vectorbt.md)) |
| VectorBT | `pf.orders.records_readable` as CSV or JSON |
| LEAN | the backtest result JSON and its order events ([Reconcile a LEAN backtest](reconcile-lean.md)) |
| Freqtrade | the backtest result (`.zip` or `.json`) |
| backtesting.py | the trades of a `Backtest.run()` result, `stats._trades`, as CSV or JSON ([below](#backtestingpy)) |

`check` reads which backtester wrote the file from its columns or keys; `--backtester NAME` says it when the file
does not. It maps the backtest's instrument names to the dataset's symbols (`AAPL`, `AAPL.XNAS` and `aapl` all match
`AAPL`; `BTC/USDT` matches `BTCUSDT`). It asks for `--instrument-map` only when a name matches no symbol, and then
says which name and why.

## 1. A dataset and a backtest

This page uses the synthetic dataset that ships with queuetruth (two futures, `QTAU6` and `QTBU6`, two days, no
market data) and a small synthetic NautilusTrader orders report: twelve resting limit orders at the touch, six of
which the backtest says filled 5 seconds after they were placed.

<!-- check-docs: run -->
```bash
queuetruth advanced fixtures synth --out synth > /dev/null
cat > orders_report.csv <<'EOF'
client_order_id,venue_order_id,instrument_id,side,type,quantity,price,time_in_force,post_only,reduce_only,status,filled_qty,avg_px,ts_init,ts_last
O-1,V-1,QTBU6.XCME,SELL,LIMIT,1,5008.50,GTC,False,False,FILLED,1,5008.50,1781620722000000000,1781620727000000000
O-2,V-2,QTAU6.XCME,BUY,LIMIT,2,4999.75,GTC,False,False,CANCELED,0,,1781620685000000000,1781620715000000000
O-3,V-3,QTBU6.XCME,SELL,LIMIT,3,5010.25,GTC,False,False,FILLED,3,5010.25,1781543367000000000,1781543372000000000
O-4,V-4,QTAU6.XCME,BUY,LIMIT,1,4999.50,GTC,False,False,CANCELED,0,,1781631135000000000,1781631165000000000
O-5,V-5,QTAU6.XCME,SELL,LIMIT,2,5002.75,GTC,False,False,FILLED,2,5002.75,1781537953000000000,1781537958000000000
O-6,V-6,QTAU6.XCME,BUY,LIMIT,3,5004.00,GTC,False,False,CANCELED,0,,1781550245000000000,1781550275000000000
O-7,V-7,QTBU6.XCME,BUY,LIMIT,1,5008.75,GTC,False,False,FILLED,1,5008.75,1781537779000000000,1781537784000000000
O-8,V-8,QTBU6.XCME,SELL,LIMIT,2,5008.50,GTC,False,False,CANCELED,0,,1781629139000000000,1781629169000000000
O-9,V-9,QTAU6.XCME,BUY,LIMIT,3,4999.50,GTC,False,False,FILLED,3,4999.50,1781620760000000000,1781620765000000000
O-10,V-10,QTAU6.XCME,BUY,LIMIT,1,4999.50,GTC,False,False,CANCELED,0,,1781630705000000000,1781630735000000000
O-11,V-11,QTBU6.XCME,BUY,LIMIT,2,5008.25,GTC,False,False,FILLED,2,5008.25,1781625149000000000,1781625154000000000
O-12,V-12,QTAU6.XCME,BUY,LIMIT,3,5001.25,GTC,False,False,CANCELED,0,,1781535905000000000,1781535935000000000
EOF
```

## 2. Check it

<!-- check-docs: run -->
```bash
queuetruth check orders_report.csv --data synth --window 10s --out qt-check --html
```

The first line is the answer. On this synthetic run it reads:

```text
Your backtest filled 6 of 12 limit orders within 10s; in the real queue 3 would have filled (backtest minus real:
+3 orders, 95% CI -3 to +8). 5 fills are phantom; no P&L gap shown: backtest minus real is +9.38 USD per order
(95% CI -4.86 to +18.75 includes 0) at the 1min markout.
```

Then come the order-by-order table (filled in both, phantom, filled in the real queue only, filled in neither), the
median time to fill of the orders both filled, the symbols with the most overfilling, and a `queuetruth why` command
for a few phantom fills that traces the order event by event through the real queue
([Explain one fill](explain-one-fill.md)). Twelve orders are too few for a firm answer, which is why the intervals
are wide; the same command on a real backtest's thousands of orders narrows them.

What the numbers mean:

- **Within the window.** A fill counts when it happens within `--window` of the order's entry (default `1h`) and
  before the backtest cancelled the order, in both the backtest and the real queue.
- **Entry.** Each order enters the real queue when it entered the backtest's book: the backtest's acknowledgement
  time, else its submit time. Your order is placed behind everything already resting at its price.
- **Phantom fill.** The backtest filled the order and the real queue did not within the window.
- **P&L gap.** Each side's fills marked to the mid `--markout` later (default `1min`), net of the backtest's fees,
  backtest minus real, per order. A gap is stated as "too high" or "too low" only when its 95% interval excludes 0.
- **Intervals.** 95% block-bootstrap intervals from `queuetruth advanced score`, which `check` runs with your backtest in
  the place of the live log.

`--json` prints the whole summary (`qt.check`) instead; the output directory holds `check.json`, `orders.csv` (one
row per order with symbol, decimal price, venue-local entry time and both outcomes), `check.html` (with `--html`),
the imported log (`backtest/`), the generated `instrument_map.csv` and the full scorecard (`score/`).

## 3. From Python

<!-- check-docs: run -->
```python
import os

import queuetruth as qt

os.makedirs("bt", exist_ok=True)
with open("bt/snapshots.jsonl", "w") as f:  # two hftbacktest snapshots of one order
    f.write(
        '{"order_id": 1, "instrument": "QTAU6", "side": 1, "price_tick": 20018, "tick_size": 0.25, "qty": 1,'
        ' "leaves_qty": 1, "exec_price_tick": 0, "status": 1, "order_type": 0, "time_in_force": 0,'
        ' "local_timestamp": 1781544730000000000, "exch_timestamp": 1781544730000001000}\n'
        '{"order_id": 1, "instrument": "QTAU6", "side": 1, "price_tick": 20018, "tick_size": 0.25, "qty": 1,'
        ' "leaves_qty": 0, "exec_price_tick": 20018, "status": 3, "order_type": 0, "time_in_force": 0, "maker": 1,'
        ' "local_timestamp": 1781544735000001000, "exch_timestamp": 1781544735000000000}\n'
    )
result = qt.check("bt/snapshots.jsonl", data="synth", out="qt-check-py", window="10min")
print(result.summary)
print(result.doc["two_by_two"])
```

`result.summary` is the answer line, `result.doc` the full summary, `result.table()` one row per order as a pandas
DataFrame and `result.to_html()` the report page.

## Without order-level data

Most bar backtests have no order-level data for their days. Without `--data`, `check` answers from the
[Fill-Realism Atlas touch tables](../concepts/touch-tables.md) instead: how many of your backtest's fills were
touch-only, and how many such fills the real queue gives. It needs nothing but your backtest's output.

<!-- check-docs: skip (downloads the public atlas, after asking) -->
```bash
queuetruth check backtest-result.zip                 # Freqtrade: the bar length is in the result
queuetruth check bt_orders.jsonl --bar 5m            # Backtrader: say the bar length when the log does not
queuetruth check orders_report.csv --bar 1h --tier T2
```

It asks before it downloads the atlas data API (a few MB, checked against its manifest and cached, as for
[`queuetruth atlas`](../reference/cli.md)); `--yes` skips the question and `--atlas DIR` reads a local copy. The
first line is the answer. With an invented atlas (the numbers below are not measured) it reads:

```text
Your backtest filled 20 of 40 resting limit orders when a 5m bar touched the limit. In the real queue about 10 of
those fills would have happened (95% interval 6 to 15), so about 10 (49%) are touch-only. Its P&L is about 0.30 ticks
per order too high (95% interval 0.21 to 0.41). An estimate from the Fill-Realism Atlas touch tables (IEX DEEP+, one
venue, 3 days) for orders like the atlas's shadow orders, not an exact replay of yours: pass --data for that.
```

A table per liquidity tier and side follows, then what the estimate rests on and how it was computed.

How it works:

- **Which fills.** Every resting limit order of the backtest that filled is taken as a fill the bar rule gave it
  (the price touched the limit within a bar). Market orders are left out.
- **Tier.** Each symbol's liquidity tier comes from the atlas's symbol lookup. A symbol the atlas does not hold
  (a future, a crypto pair, a stock not seen on IEX) has no tier there, so `check` stops and asks for `--tier T1`
  to `T5` (`T1` is the 100 most traded US stocks, `T5` the less traded) or `--tier ALL` (the atlas's average).
- **Bar length.** From `--bar`, the importer option `timeframe`, a Freqtrade result's `timeframe`, or the log's own
  timestamps when every order and fill time is on a 1m, 5m or 1h grid. The tables have 1m, 5m and 1h bars only.
- **Real fills.** Per tier and side, your touch fills times P(real | touch), the share of a touch rule's fills that
  the real queue also gave within the same bar. The 95% interval comes from 20,000 simulated draws: each table
  cell's P(real | touch) is drawn from a Beta distribution with the cell's value and the spread of its 95% interval,
  all cells moving together (their errors come from one bootstrap, so this is the cautious choice), and then the
  real fills from a binomial over your fills. `--seed` fixes the draws.
- **P&L.** The tables' P&L overstatement per order placed (the touch rule's markout minus the real queue's, one bar
  after the fill, in ticks), averaged over your resting orders. It is stated as too high or too low only when its
  95% interval excludes 0.

What it rests on, and what it is not:

- One venue. The tables come from IEX DEEP+ (about 4 to 5% of US volume), with bars built from IEX's prints; other
  venues and consolidated candles can differ ([what is approximate](../concepts/touch-tables.md#what-is-approximate)).
- Orders like the atlas's shadows: small orders joining the back of the queue and resting as day orders. Your orders
  may be larger, rest shorter, or sit elsewhere in the queue.
- It is an estimate for orders like these, not an exact replay of your orders. With order-level data of your
  backtest's days, `--data` gives the exact answer, order by order.
- `check` never invents a number. A tier, side or bar length the tables do not have, or a cell without an interval,
  stops with the reason. Until the atlas has touch tables (the kept days are being backfilled), it says so and gives
  no number.

`--json` prints the `qt.check_estimate` document; the output directory holds `check.json` and `groups.csv` (one row
per tier and side). From Python: `qt.check("backtest-result.zip", bar="5m", yes=True)`.

## When the symbols do not match

When a backtest instrument matches no dataset symbol (or hftbacktest snapshots carry no instrument and the dataset
has several symbols), `check` stops with `QT-INPUT-002` and names each name with its reason. Write an instrument map
for those names, with the dataset's venue in the `venue` column and the dataset's units:

```text
venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size
cme,asset0,1,,-9,0,0.25,1
```

and pass it with `--instrument-map instrument_map.csv`. The generated `qt-check/instrument_map.csv` of an earlier run
is a good start. Orders on instruments that do not map while others do are left out and counted in a note
(`--verbose` prints the notes).

## backtesting.py

A [backtesting.py](https://kernc.github.io/backtesting.py/) run returns a pandas Series; its `_trades` member holds
one row per closed trade (`Size`, `EntryPrice`, `ExitPrice`, `EntryTime`, `ExitTime`, `Commission`, ...). The
result has no order records of its own, so save the trades, as CSV or as pickle-free JSON:

<!-- check-docs: run needs=backtesting,pandas -->
```python
import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy

# a synthetic price series; use your own data and strategy
idx = pd.date_range("2026-06-15 14:00", periods=300, freq="1min")
close = 100 + np.cumsum(np.random.default_rng(0).normal(0, 0.05, len(idx)))
ohlc = pd.DataFrame({"Open": close, "High": close + 0.02, "Low": close - 0.02, "Close": close}, index=idx)


class EveryHalfHour(Strategy):
    def init(self):
        pass

    def next(self):
        if len(self.data) % 30 == 0:
            self.position.close()
            self.buy(size=1, limit=self.data.Close[-1] - 0.01)


stats = Backtest(ohlc, EveryHalfHour, cash=10_000, commission=0.0002, finalize_trades=True).run()
stats._trades.to_csv("trades.csv")
stats._trades.to_json("trades.json", orient="records", date_format="iso")
print(len(stats._trades), "trades")
```

`check` reads either file. Each trade becomes two orders: the entry (a buy for a long trade, a sell for a short
one) and the exit, each filled in full at its price and time, with the trade's `Commission` split between the two in
proportion to their notional. backtesting.py fills an order in full at one price with no queue, so `check` treats
each of these fills as a limit order resting at its fill price (`--option order_type=market` leaves them out). A run
trades one instrument and the file does not name it: `--option instrument=SYMBOL` names it (a dataset with a single
symbol needs nothing). On the synthetic dataset, with a few trades written by hand in backtesting.py's layout:

<!-- check-docs: run -->
```bash
queuetruth advanced fixtures synth --out synth > /dev/null
cat > trades.csv <<'EOF'
,Size,EntryBar,ExitBar,EntryPrice,ExitPrice,SL,TP,PnL,Commission,ReturnPct,EntryTime,ExitTime,Duration,Tag
0,-1,0,1,5001.5,5002.5,,,-1.02,0.02,-0.0002,2026-06-15 15:06:40,2026-06-15 15:39:26,0 days 00:32:46,
1,-3,2,3,5002.75,5004.5,,,-5.27,0.02,-0.00035,2026-06-15 15:32:12,2026-06-15 17:59:24,0 days 02:27:12,
2,-1,4,5,5003.25,5003.5,,,-0.27,0.02,-5e-05,2026-06-15 17:13:28,2026-06-15 18:39:00,0 days 01:25:32,
3,-2,6,7,5005.0,5004.25,,,1.48,0.02,0.00015,2026-06-15 17:37:58,2026-06-15 18:58:54,0 days 01:20:56,
EOF
queuetruth check trades.csv --data synth --option instrument=QTAU6 --window 10s --out qt-check-btpy
```

Without order-level data, the same file gets the atlas estimate
([without order-level data](#without-order-level-data)): how many of its fills the real queue would give, and the
P&L they add. Say the bar length, and the tier when the symbol is not one the atlas holds:

<!-- check-docs: skip (downloads the public atlas, after asking) -->
```bash
queuetruth check trades.csv --bar 5m --option instrument=AAPL
queuetruth check trades.csv --bar 1h --tier T2
```

Naive times are read as UTC; `--option data_tz=America/New_York` reads them in another zone. A backtest on data
without a `DatetimeIndex` has bar numbers instead of times, which `check` cannot place on the tape: give the data a
time index. `queuetruth advanced live import --venue sim-backtesting-py` imports the same files as a simulator log.

## Limits

- Only resting limit orders have a queue position, so only they are checked; market orders, IOC and FOK orders are
  counted and left out.
- VectorBT records are fills only: each is checked as a limit order resting at its fill price from the bar's time
  (`--option order_type=market` leaves them out).
- Bar backtests (Backtrader, VectorBT, LEAN, Freqtrade) know times only to the bar; pass `--option timeframe=5m` and,
  for naive times, `--option data_tz=America/New_York` as for `live import`.
- The dataset must be order-level (MBO). An L2-only dataset has no exact queue.
