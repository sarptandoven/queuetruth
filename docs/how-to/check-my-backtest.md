# Check my backtest against the real queue

**Goal:** find out how wrong your backtest's fills are, in one command. `queuetruth check` replays your backtest's
own resting limit orders, at their own times and prices, in the exact queue of an order-level dataset, and compares
them order by order: which fills were real, which were phantom (the backtest filled the order, the real queue did
not), how long the fills took, and what that does to P&L.

**You need:** your backtester's output file, and order-level (MBO) data that covers the backtest's days, as a
queuetruth dataset (`queuetruth ingest`; see [Replay a public IEX DEEP+ day](iex-public-demo.md) for free data).
`check` reads these outputs:

| Backtester | File |
|---|---|
| hftbacktest | order snapshots (JSON lines, CSV or Parquet; the format of `live import --venue sim-hftbacktest`) |
| NautilusTrader | `generate_orders_report` / `generate_fills_report` output, or order event files |
| Backtrader | the `qt.backtrader.OrderLog` lines ([Reconcile a Backtrader or VectorBT backtest](reconcile-backtrader-vectorbt.md)) |
| VectorBT | `pf.orders.records_readable` as CSV or JSON |
| LEAN | the backtest result JSON and its order events ([Reconcile a LEAN backtest](reconcile-lean.md)) |
| Freqtrade | the backtest result (`.zip` or `.json`) |

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
queuetruth fixtures synth --out synth > /dev/null
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
- **Intervals.** 95% block-bootstrap intervals from `queuetruth score`, which `check` runs with your backtest in
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

The estimate that needs no data (from the atlas touch tables: how many of your fills were touch-only, and how many
such fills the real queue gives) comes in a later release. Until then `check` without `--data` says what it needs
and gives no number:

<!-- check-docs: run exit=2 -->
```bash
echo '{"order_id": 1}' > snapshots.jsonl
queuetruth check snapshots.jsonl
```

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

## Limits

- Only resting limit orders have a queue position, so only they are checked; market orders, IOC and FOK orders are
  counted and left out.
- VectorBT records are fills only: each is checked as a limit order resting at its fill price from the bar's time
  (`--option order_type=market` leaves them out).
- Bar backtests (Backtrader, VectorBT, LEAN, Freqtrade) know times only to the bar; pass `--option timeframe=5m` and,
  for naive times, `--option data_tz=America/New_York` as for `live import`.
- The dataset must be order-level (MBO). An L2-only dataset has no exact queue.
