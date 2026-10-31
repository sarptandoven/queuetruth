# Reconcile Freqtrade and Hummingbot runs with dry-run and live trades

**Goal:** turn "my backtest differs from my dry-run" into a reproducible, per-order account: which orders the
backtest filled that the bot did not, which fills came at another price or another time, and which orders exist in
only one of the two runs. Then measure how far Freqtrade's candle-fill rule is from exact queue truth.

**You need:** for Freqtrade, a backtest result (`backtest-result-<date>.zip` from
`freqtrade backtesting --export trades`) and the bot's trades database (`tradesv3.dryrun.sqlite` or
`tradesv3.sqlite`), plus its log file if it ran with `--logfile`. For Hummingbot, the databases
(`data/<config>.sqlite`) of a live bot and of a paper-trade bot that ran the same strategy at the same time. Nothing
else: no market data and no exchange account.

**From the command line or Python.** `queuetruth reconcile --kind freqtrade|hummingbot` runs the whole comparison
below, and `queuetruth live import --venue freqtrade | hummingbot | sim-freqtrade | sim-hummingbot` writes one arm as
a canonical live log (importer options with `--option KEY=VALUE`, e.g. `--option dry_run=true`); the Python API is
`queuetruth.live.importers.freqtrade` and `queuetruth.live.importers.hummingbot`. Every file format
below was read from the projects' public source (Freqtrade `develop` at commit `1c0d709401b5`, Hummingbot 2.17.0 at
commit `9af100d6822d`); the fixtures in `tests/fixtures/live/` are written from that source, not exported from a
real bot, so check the counts against your own records the first time.

## What a Freqtrade backtest assumes

Freqtrade's backtester fills a limit order at its price, in full, whenever the price lies inside the candle's range
(`Backtesting._get_order_filled`: `low <= rate <= high`), and stamps the fill at the candle's open time. A limit
entry priced through the candle it is placed on is first clamped to that candle's high (buy) or low (sell), so it
fills there. There is no queue, no partial fill and no slippage. Its dry-run mode is a second simulator: a limit
order fills, in full, when the best opposite price of an order-book snapshot that the bot polls crosses it
(`_dry_is_price_crossed`, `check_dry_limit_order_filled`). Only a live bot's database holds exchange executions.

Two more facts shape what can be compared:

- The backtest export holds filled orders and the orders still open when the backtest ended (no fill time; they
  come in unfilled). Orders that timed out are deleted; the strategy statistics only count them
  (`timedout_entry_orders`, `timedout_exit_orders`, `canceled_entry_orders`, `replaced_entry_orders`).
- The bot deletes a trade whose entry orders were all cancelled unfilled, together with those orders. The database
  never shows them; the log does (`... Removing Trade(...) from database.`, after a timeout, an exchange cancel or a
  failed replacement), so pass the log file too.

## Part 1: Freqtrade backtest vs dry-run and live

### 1. Run the reconciliation on the fixtures

The fixtures are one strategy (`SampleStrategy`, 5m candles, BTC/USDT and ETH/USDT on Binance, stop-loss on the
exchange) run three times over the same four hours: a backtest result zip, a dry-run bot's database (as SQL text,
built into SQLite below) and log, and a live bot's database.

<!-- check-docs: run -->
```python
import os
import sqlite3

from queuetruth.live.importers.freqtrade import reconcile_freqtrade, render_reconcile

fx = os.path.join(os.environ["QT_FIXTURES"], "live", "freqtrade")
with open(os.path.join(fx, "tradesv3.dryrun.sql")) as f:
    sqlite3.connect("tradesv3.dryrun.sqlite").executescript(f.read())

report = reconcile_freqtrade(
    os.path.join(fx, "backtest-result-2024-03-01_14-00-00.zip"),  # simulator arm
    ["tradesv3.dryrun.sqlite", os.path.join(fx, "freqtrade.log")],  # the bot to check
    "rec",
    instrument_map=os.path.join(fx, "instrument_map.csv"),
)
print(render_reconcile(report))
```

The arguments are simulator, bot, output directory. It writes `rec/live` (the dry-run as a canonical live log),
`rec/sim` (the backtest as a simulator log), `rec/score` (the `queuetruth score` output) and `rec/reconcile.json`
plus `rec/reconcile_orders.csv`. The printout starts with the score and the counts:

```text
reconcile (freqtrade): 13 live orders, 10 simulator orders, 7 paired (54% of live orders)
score (not interpretable: 6 matched orders in 5 cluster(s), it needs 10 matched orders and 2 clusters): no
significant fill-rate difference within 86400 s: +33.3 pp (95% CI [+0.0, +66.7]); ...
score orders: both 4, neither 0, live_only 0, sim_only 2
categories: match 3, missed_fill 2, price 1, timing 1, stop_not_triggered 3, no_sim_twin 3, no_live_twin 3
sim price advantage BTC/USDT: 0.0288 (quote)
missed-fill notional BTC/USDT: 98.88 (quote)
missed-fill notional ETH/USDT: 102 (quote)
```

and then gives one line per order, for example:

```text
[missed_fill] binance:main:deleted-2-1709288103: entry buy 0.03 ETH/USDT @ 3400: backtest filled 0.03 at 3400 in
the 5m candle opening 2024-03-01T10:15:00.000Z (candle rule); the dry-run order rested 10m1s and ended cancelled
(cancelled due to timeout; trade removed from the database (bot log)) without a fill
[price] binance:main:dry_run_sell_BTC_USDT_0b7f3e91-...: stoploss sell 0.0016 BTC/USDT @ 59982.12: price dry-run
60570 vs backtest 60588: the backtest price is 2.97 bps better (0.0288 quote on 0.0016)
[timing] binance:main:dry_run_buy_BTC_USDT_e47b2c90-...: entry buy 0.0016 BTC/USDT @ 61500: timing: dry-run filled
2024-03-01T13:06:12.880Z, outside the backtest fill candle [2024-03-01T13:10:00.000Z, +5m)
```

The same from the command line (`--format json` prints the `qt.bot_reconcile` document instead):

<!-- check-docs: run -->
```sh
FX="$QT_FIXTURES/live/freqtrade"
queuetruth reconcile --kind freqtrade --sim "$FX/backtest-result-2024-03-01_14-00-00.zip" \
    --live tradesv3.dryrun.sqlite --live "$FX/freqtrade.log" --out rec-cli \
    --instrument-map "$FX/instrument_map.csv" --max-rows 3
```

Every number above is asserted in `test_reconcile_freqtrade_end_to_end` (`python/tests/live/test_freqtrade.py`); the
command line gives the same report (`python/tests/cli/test_cli_bots.py`).
Six of the dry-run's limit orders have a backtest twin and the backtest filled all six; the dry-run filled four.
The two it did not fill are the 10:15 ETH entry (the bot's log shows it timed out after 10 minutes; the database
does not have it) and the 13:40 BTC exit (timed out, then replaced at a lower price). Both are candle-rule fills:
the price reached the order inside the candle, the backtest filled it, the dry-run did not. The stop-loss sold 18
USDT lower than the backtest's stop price, and the 13:00 BTC entry filled at 13:06, a candle before the backtest's
fill. The three stop-loss orders that never triggered are `stop_not_triggered`: the backtest simulates its stop
inside the exit, so it has no order to pair them with, and that is agreement.

The score line says "not interpretable": six matched orders are an anecdote, whatever the p-value. The report marks
any score with fewer than 10 matched orders or fewer than 2 clusters this way; the per-order rows are what a run
this short is good for.

### 2. Score again from the command line

The logs are ordinary canonical logs, so `queuetruth score` runs on them directly. `--window` is the evaluation
horizon of each order (the live cancel still ends it); Freqtrade orders rest for minutes, so use your
`unfilledtimeout` or longer:

<!-- check-docs: run -->
```sh
queuetruth score --live rec/live --sim-log rec/sim --out sc --window 1440m
```

The result is the same scorecard as `rec/score` (same `run_id`): `two_by_two` both 4, sim_only 2, live_only 0,
`n_unmatched_live` 3 (dry-run limit orders without a backtest twin), `n_unmatched_sim` 4 (backtest orders without a
dry-run twin, plus the paired stop-loss, which is a stop order and is not scheduled). `score` reads only the
resting limit orders; the reconciliation explains every order.

### 3. Against a live bot

A dry-run is Freqtrade's own simulator, so the comparison that settles a fill question is against a live bot. The
fixture `tradesv3.live.sql` is the same strategy run live (Binance order ids, so `order_date` is the exchange's
order time):

<!-- check-docs: run -->
```python
import os
import sqlite3

from queuetruth.live.importers.freqtrade import reconcile_freqtrade

fx = os.path.join(os.environ["QT_FIXTURES"], "live", "freqtrade")
for name in ("tradesv3.live", "tradesv3.dryrun"):
    if not os.path.exists(f"{name}.sqlite"):  # the dry-run database may exist from step 1
        with open(os.path.join(fx, f"{name}.sql")) as f:
            sqlite3.connect(f"{name}.sqlite").executescript(f.read())
imap = os.path.join(fx, "instrument_map.csv")
bt = os.path.join(fx, "backtest-result-2024-03-01_14-00-00.zip")
dry = ["tradesv3.dryrun.sqlite", os.path.join(fx, "freqtrade.log")]
for name, sim in (("backtest", bt), ("dry-run", dry)):
    r = reconcile_freqtrade(sim, "tradesv3.live.sqlite", f"vs-live-{name}", instrument_map=imap)
    print(name, "vs live:", {k: v for k, v in r.counts.items() if v}, r.score.two_by_two)
```

```text
backtest vs live: {'match': 5, 'missed_fill': 1, 'partial_fill': 2, 'price': 1, 'timing': 1, 'stop_not_triggered': 4, 'no_sim_twin': 1} {'both': 7, 'neither': 0, 'live_only': 0, 'sim_only': 1}
dry-run vs live: {'extra_live_fill': 2, 'price': 1, 'timing': 4, 'stop_not_triggered': 5, 'no_sim_twin': 4, 'no_live_twin': 3} {'both': 4, 'neither': 0, 'live_only': 2, 'sim_only': 0}
```

Against the backtest, the live ETH entry filled 0.012 of 0.03 before its timeout (`partial_fill`), so its market
exit sold 0.012 at 3424.1 where the backtest sold 0.03 at 3425.5 (`partial_fill` and `price`); the stop-loss filled
at 60560 against the backtest's 60588 (`price`, 4.62 bps); the 12:25 ROI exit timed out unfilled (`missed_fill`)
and its replacement has no backtest twin. The paired market exit and stop-limit order take the live order's type in
the simulator log, because the backtest export has no order type. Against the dry-run, two bots are compared with a
5 s timing tolerance (Freqtrade's `process_throttle_secs` default; option `time_tol_ns`), and untriggered
stop-losses on both sides pair with each other. These numbers are asserted in
`test_reconcile_backtest_vs_live_end_to_end` and `test_reconcile_dry_run_vs_live_end_to_end`.

### 4. The categories

`reconcile_orders.csv` has one row per order of either run (`category`, both keys, both statuses, quantities,
prices, times, `fill_delay_ns`, `sim_price_advantage_bps` and the explanation). The categories:

| Category | Meaning |
|---|---|
| `match` | Both filled the same quantity at the same price (within `price_tol_bps`, default 0.5) and the live fill falls inside the backtest's fill candle (two bots: within `time_tol_ns`). |
| `missed_fill` | The simulator filled, the bot did not: the candle rule overfilled (or the bot cancelled sooner). |
| `extra_live_fill` | The bot filled, the simulator did not. |
| `partial_fill` | Both filled, different quantities (the candle rule always fills in full). |
| `price` | Both filled, the average prices differ by more than the tolerance. Positive `sim_price_advantage_bps` means the simulator bought cheaper or sold dearer. |
| `timing` | Both filled, the live fill is outside the backtest's fill candle (two bots or Hummingbot: more than `time_tol_ns` apart). |
| `both_unfilled` | Neither filled. |
| `stop_not_triggered` | A stop-loss order that never triggered and was cancelled when the trade exited, on one side or both: agreement. |
| `no_sim_twin` | A bot order with no simulator order of that role on that candle: no signal in the backtest, or a backtest order that timed out or was replaced (not exported). |
| `no_live_twin` | A simulator order with no bot order: no signal live, pairlist, `max_open_trades` or wallet state, or the bot was not running. |

`no_sim_twin` and `no_live_twin` are signal or state divergence, not fill divergence: look for look-ahead bias
(`freqtrade lookahead-analysis`) or a different wallet before blaming the fill model. A low pairing coverage
(`n_paired` of the live orders, printed on the first line) says the same.

### 5. How orders are paired

The backtest and the bot share no order id. A bot trade pairs with the unpaired backtest trade of the same pair and
direction whose entry candle is closest (floor of the trade's `open_date` to the timeframe), within
`pair_tolerance_candles` (default 1). Inside a paired trade, each backtest order pairs with the bot order of the
same role (entry or exit; a filled stop-loss order counts as an exit) placed latest in or before the backtest
order's candle (its placement candle for the first entry, its fill candle otherwise), else the earliest one placed
within the tolerance after it. That is why the 13:45 backtest exit pairs with the dry-run's 13:40 exit order, which
timed out, and not with the 13:50 replacement. Trades are bucketed by pair and direction, so a run of any length
pairs in about a second per 25,000 trades (`test_pair_runs_scales_linearly`).

A paired backtest order takes the bot's `order_key`, so `score` matches them. Its fill time is the candle's open;
when that is before the bot's order existed, the fill time is clamped to the bot order's entry time (the scorer's
window starts there) and the candle time stays in the event's `reason`.

### 6. Your own runs

<!-- check-docs: skip (needs your Freqtrade files) -->
```python
from queuetruth.live.importers.freqtrade import reconcile_freqtrade, render_reconcile

report = reconcile_freqtrade(
    "user_data/backtest_results/backtest-result-2026-10-20_09-12-44.zip",
    ["tradesv3.sqlite", "freqtrade.log"],
    "reconcile-out",
    strategy="MyStrategy",          # needed when the result holds several strategies
    instrument_map="instrument_map.csv",
)
print(render_reconcile(report))
```

Options (keyword arguments; unknown ones fail `QT-CONFIG-001`, malformed values too):

| Option | Default | Use |
|---|---|---|
| `strategy` | the only one | Strategy inside a multi-strategy result. |
| `timeframe` | from the result / database | Candle length for pairing (`5m`, `1h`). |
| `pair_tolerance_candles` | 1 | Candles a bot trade or order may lag the backtest. |
| `instrument_map` | ids 1..n, default scales | Instrument map CSV (`venue,instrument,...`, venue = the exchange name, instrument = the Freqtrade pair, e.g. `binance,BTC/USDT,...`). |
| `window_ns` | 1 day | Score horizon per order. |
| `price_tol_bps` | 0.5 | Price difference counted as `price`. |
| `time_tol_ns` | 0 vs a backtest, 5 s between bots | Fill-time difference counted as `timing`. |
| `tif` | `gtc` | Freqtrade stores no time in force: `gtc`, `ioc`, `fok` or `po`. |
| `dry_run` | from the order ids | Force the bot arm to dry-run (`dry_run_` ids) or live. |
| `order_date_clock` | `exchange` (live) | `local` when your exchange reports no order time (ccxt `timestamp`), so Freqtrade wrote its own clock. |
| `log_tz` | `UTC` | Time zone of the logs' timestamps (Python logging writes local time). |
| `venue` | `trades.exchange` | Venue name of the bot's log. |
| `price_exp`, `size_exp` | -9, -8 | Fixed-point exponents when the map has no row. |
| `arms_as_given` | false | A live bot as the simulator arm with a dry-run bot as the live arm fails `QT-CONFIG-001` (swapped arguments) unless set. |

`dry_run`, `order_date_clock` and `venue` describe the bot arm; a dry-run simulator arm is detected from its order
ids. `reconcile.json` carries a `run_id` over the inputs' content and every option (SPEC 7.3), and its paths are
relative to the output directory, so two runs on the same inputs write the same bytes wherever they write.

To build the logs without the reconciliation:

<!-- check-docs: skip (needs your Freqtrade files) -->
```python
from queuetruth import live
from queuetruth.live.importers.freqtrade import import_freqtrade_live

import_freqtrade_live(["tradesv3.sqlite", "freqtrade.log"], "live", account="main",
                      instrument_map="instrument_map.csv")
live.import_sim("freqtrade", ["backtest-result-2026-10-20_09-12-44.zip"], "sim", account="main",
                instrument_map="instrument_map.csv", pair_with=["tradesv3.sqlite", "freqtrade.log"])
```

The simulator log takes its keys from the `pair_with` run as venue and account, so `account` (and `live_venue`,
when you set `venue` on the live import) must be the live import's, or no key matches. The simulator manifest
lists only the simulator inputs; a warning records the `pair_with` files and their sha256. With `pair_with`,
`dry_run`, `venue` and `order_date_clock` describe the simulator arm and `live_dry_run`, `live_venue` the live one.
`import_sim("freqtrade", ...)` also takes a dry-run database as the simulator arm, to score dry-run against live.
What the importers write:

- Dry-run logs are `validation_status = simulated` with local times (`submit_sent` and `ack` at `order_date`, fills
  at `order_filled_date` as receive times). Live logs have the `ack` at the exchange's order time (`gateway_time`)
  and fills at `order_filled_date` (`record_time`): ccxt's `lastTradeTimestamp`, or the bot's own clock when the
  exchange does not report one. ccxt times are milliseconds, so the events carry precision `ms`.
- One fill per order row (Freqtrade keeps `filled` and `average`, not the executions), fill id `<order_id>:fill`,
  `derived = true`. A trade's fee goes to its order when exactly one order of that role filled.
- A cancel is `cancel_ack` at `order_update_date`, preceded by `cancel_sent` when the bot cancelled
  (`ft_cancel_reason` set, other than "cancelled on exchange"), so the scorer's window ends at the bot's cancel.
- Freqtrade's doubles are read as decimals rounded to 15 significant digits (`60588.00000000001` is 60588). Live
  prices must then be exact at the instrument's exponent (`QT-INPUT-004`); backtest prices and sizes are rounded
  half-even to it and counted in a warning.
- Malformed inputs fail with `QT-INPUT-002` naming the file and row (a zip without the result member, a result
  without `strategy`, a `.last_result.json` that names anything but a file next to it, a database without `trades`
  / `orders` or a needed column, a dry-run / live mix, an unknown order status, `filled` above `amount`, a
  non-integer `trades.timeframe`). A partially filled dry-run order, which Freqtrade's dry-run never produces, is
  warned.

## Part 2: Hummingbot paper trading vs live

A paper-trade connector (`<exchange>_PaperTrade`) fills a resting limit order in full when the opposite best price
of its order book reaches the order's price, or a public trade prints through it: no queue position either. Run the
paper bot next to the live bot with the same configuration, then:

<!-- check-docs: run -->
```python
import os
import sqlite3

from queuetruth.live.importers.freqtrade import render_reconcile
from queuetruth.live.importers.hummingbot import reconcile_hummingbot

fx = os.path.join(os.environ["QT_FIXTURES"], "live", "hummingbot")
for name in ("conf_pure_mm_1", "conf_pure_mm_1_paper"):
    with open(os.path.join(fx, f"{name}.sql")) as f:
        sqlite3.connect(f"{name}.sqlite").executescript(f.read())

# simulator (paper bot) first, then the live bot, as in reconcile_freqtrade
report = reconcile_hummingbot("conf_pure_mm_1_paper.sqlite", "conf_pure_mm_1.sqlite", "hb", window_ns=30_000_000_000)
print(render_reconcile(report))
```

The fixtures are two `pure_market_making` bots quoting 0.001 BTC on each side and refreshing every 30 s. The
reconciliation pairs 8 of the 9 orders (same symbol and side, equal price, created within `pair_window_ms`, default
1000, of each other) and finds: the paper bot filled two orders the live bot cancelled unfilled (`missed_fill`), one
order filled 0.001 on paper and 0.0004 live (`partial_fill`), two fills came 7.2 s and 2.3 s apart (`timing`), and
the paper bot rejected a sell (insufficient paper balance) that filled live (`extra_live_fill`). The score: both 3,
neither 2, live_only 1, sim_only 2, marked not interpretable (8 orders in one 30-minute cluster)
(`test_reconcile_hummingbot_end_to_end` in `python/tests/live/test_hummingbot.py`). Set `window_ns` to your
`order_refresh_time`. Passing the live bot first fails `QT-CONFIG-001` (a paper live arm against a non-paper
simulator arm), unless `arms_as_given=True`.

Inputs: the database (`Order`, `OrderStatus`, `TradeFill`). Hummingbot stores prices, amounts and quote fees as
`SqliteDecimal(6)`: the value times 10^6 as an integer, truncated to 6 decimals when written, read here exactly.
Price differences below 10^-6 are therefore invisible in both arms; the reconciliation warns when your instrument
map gives a tick below that. The trades CSV (`data/trades_<config>.csv`) keeps full precision and can be added to
an arm's database: a fill in both keeps the CSV copy. A CSV alone has fills only (no creation times, no unfilled
orders), so an arm without its database fails `QT-CONFIG-001`. Times are the bot's clock: `submit_sent` at
`creation_timestamp`, fills, cancels and failures as receive times. `LIMIT_MAKER` orders are post-only. A fill whose
`trade_fee_in_quote` is 0 next to a non-zero `trade_fee` (Hummingbot stores 0 when its fee conversion fails) gets
its fee recomputed and counted in a warning. The building blocks are `import_hummingbot_live(...)` and
`live.import_sim("hummingbot", [paper db], out, pair_with=[live db])`; options `market` (the live market, in both
entry points; the paper arm is `<market>_PaperTrade`), `pair_window_ms`, `pair_price_tol`, `venue`, `price_exp`,
`size_exp`. Pairing buckets orders by symbol and side, so 50,000 orders per arm pair in about two seconds
(`test_pair_orders_scales_linearly`).

## Part 3: how wrong is the candle-fill rule?

The reconciliation shows where a backtest and a bot disagree, but a dry-run is itself a simulator. To measure the
candle-fill rule itself, apply it to orders whose true outcome is known: shadow orders in an exact FIFO replay of
order-level data. `candle_fill_audit` takes a `queuetruth replay --shadows` output, rebuilds candles from the
replay's trade prints and checks each order the way Freqtrade does: the placement clamp on the first candle, then
every candle while the order is open, filled when one candle's `low <= price <= high`. It compares that with the
exact outcome over the order's life [`ts_live`, `min(cancel, ts_live + timeout)`).

Where the order sits in its first candle matters. `anchor="submit"` with `lag_ns=0` starts the candles at the
order's activation: the whole first candle is after placement, the rule's best case. A Freqtrade bot decides when
a candle closes and places its order a few seconds into the next one (one pass of its loop,
`process_throttle_secs` 5 s by default), while the backtest credits the whole candle; `lag_ns` starts the first
candle that long before the order. `anchor="clock"` uses the clock grid: the sampled orders' times are unrelated to
the grid, so each lands uniformly inside its first candle, a mean lag of half a candle (2.5 minutes at 5m), which
is far later than a Freqtrade bot places.

### On the synthetic dataset

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --out synth
queuetruth shadows sample --data synth --n 2000 --lifetime 10m --out shadows.parquet
queuetruth replay --data synth --shadows shadows.parquet --out replay
```

<!-- check-docs: run -->
```python
from queuetruth.live.importers.freqtrade import candle_fill_audit

for anchor, lag_s in (("submit", 0), ("submit", 5), ("clock", 0)):
    a = candle_fill_audit("replay", timeframe="5m", timeout=600, anchor=anchor, lag_ns=lag_s * 1_000_000_000)
    al, t = a.all, a.touch
    print(f"{anchor} lag {lag_s}s: {al.n} orders, candle rule {al.candle_filled} fills, FIFO {al.exact_filled}; "
          f"candle-only {al.candle_only} (queue {al.candle_only_queue}, outside the order's life "
          f"{al.candle_only_outside_life}), FIFO-only {al.exact_only}; overfill {al.overfill_share:.2%}; "
          f"touch-only candles: {t.candle_only} of {t.n} unfilled under FIFO")
```

On this synthetic tape (2000 sampled shadows, 151 rejected at entry for crossing, 10-minute life, 5m candles) the
candle rule fills 1757 orders against FIFO's 1713 at zero lag (2.50% of its fills are false; 95% interval 1.87% to
3.35% treating orders as independent, 1.52% to 3.66% resampling instrument x 30-minute clusters), 1785 with a 5 s
lag (4.03%) and 1795 on the clock grid (4.57%). The synthetic tape says nothing about any real venue.

### On Nasdaq ITCH (measured)

The same procedure on two days of real Nasdaq TotalView-ITCH order-level data (AAPL, MSFT, SPY, TSLA on 2019-12-30
and 2020-01-30), 3000 shadows per day from `queuetruth shadows sample --n 3000 --lifetime 10m` (seed 0; 60% at the
touch, 30% one or two ticks behind, 10% one tick inside; times uniform in the session), replayed with the default
exact FIFO rule, 10-minute timeout:

<!-- check-docs: skip (needs the ITCH datasets of the Getting started page) -->
```sh
queuetruth shadows sample --data nasdaq-20200130 --n 3000 --lifetime 10m --out shadows.parquet
queuetruth replay --data nasdaq-20200130 --shadows shadows.parquet --out replay-20200130
python -c "
from queuetruth.live.importers.freqtrade import candle_fill_audit
for lag_s in (0, 1, 5, 30, 150):
    a = candle_fill_audit('replay-20200130', timeframe='5m', timeout=600, lag_ns=lag_s * 10**9)
    print(lag_s, a.all.model_dump_json())
print(candle_fill_audit('replay-20200130', timeframe='5m', timeout=600, anchor='clock').model_dump_json(indent=1))
"
```

Placement lag, 5m candles (overfill = the share of the candle rule's fills that FIFO does not give; intervals: 95%,
first treating orders as independent (Wilson), then a bootstrap over the 52 instrument x 30-minute clusters):

| Placement | 2019-12-30 overfill | Candle-only (queue / outside life) | 2020-01-30 overfill | Candle-only (queue / outside life) |
|---|---|---|---|---|
| at the candle open (lag 0) | 0.48% (0.28 to 0.82; 0.26 to 0.74) | 13 (13 / 0) | 0.36% (0.20 to 0.67; 0.15 to 0.59) | 10 (10 / 0) |
| 1 s into the candle | 1.94% (1.49 to 2.53; 1.35 to 2.58) | 53 (13 / 40) | 2.21% (1.72 to 2.82; 1.64 to 2.83) | 62 (10 / 52) |
| 5 s | 3.14% (2.56 to 3.86; 2.51 to 3.81) | 87 (13 / 74) | 3.30% (2.70 to 4.02; 2.61 to 4.04) | 94 (10 / 84) |
| 30 s | 5.07% (4.32 to 5.94; 4.17 to 6.01) | 143 (13 / 130) | 4.41% (3.72 to 5.22; 3.63 to 5.25) | 127 (10 / 117) |
| 150 s (half a candle) | 6.03% (5.21 to 6.97; 5.06 to 7.10) | 172 (13 / 159) | 5.16% (4.42 to 6.03; 4.31 to 6.07) | 150 (10 / 140) |
| clock grid (uniform, mean 150 s) | 5.73% (4.94 to 6.65; 4.76 to 6.76) | 163 (13 / 150) | 5.07% (4.33 to 5.92; 4.22 to 5.97) | 147 (10 / 137) |

Fill counts and the touch-only candles:

| Day | Candles | Orders | Candle-rule fills (placement clamp) | FIFO fills | FIFO-only (reason) | Touch-only candles unfilled under FIFO (95%, independent) |
|---|---|---|---|---|---|---|
| 2019-12-30 | 5m, lag 0 | 2879 | 2693 (14) | 2680 | 0 | 13 of 123: 10.6% (6.3 to 17.3) |
| 2019-12-30 | 5m, lag 5 s | 2879 | 2767 (3) | 2680 | 0 | 31 of 128: 24.2% (17.6 to 32.3) |
| 2019-12-30 | 1m, clock | 2879 | 2800 (3) | 2680 | 0 | 22 of 316: 7.0% (4.6 to 10.3) |
| 2020-01-30 | 5m, lag 0 | 2916 | 2760 (15) | 2755 | 5 (quote crossed) | 10 of 83: 12.0% (6.7 to 20.8) |
| 2020-01-30 | 5m, lag 5 s | 2916 | 2848 (3) | 2755 | 1 (quote crossed) | 27 of 97: 27.8% (19.9 to 37.5) |
| 2020-01-30 | 1m, clock | 2916 | 2879 (0) | 2755 | 0 | 16 of 164: 9.8% (6.1 to 15.3) |

With 1m candles at lag 0 the totals equal the 5m ones (only the touch split changes: 13 of 330 and 10 of 186
touch-only fills unfilled): no order's prints crossed its limit exactly at a candle boundary on either grid, so the
gap between candles decided no order on these days.

What this says:

- At the candle open (lag 0) the candle rule gets the number of fills nearly right for orders at or near the touch
  of very liquid stocks with a 10-minute life (93.5% vs 93.1% and 94.7% vs 94.5% fill rate): almost every such
  order is traded through within ten minutes, and a print through the limit fills it in both models.
- Its queue errors sit in the candles that only touch the limit: 10.6% and 12.0% of those fills never happen under
  FIFO, because the queue ahead was not consumed (intervals above; 123 and 83 orders, so wide). A strategy whose
  limits sit where the price turns (support, resistance, mean-reversion entries) lives in exactly those candles, so
  its overfill is that rate, not the average.
- Placement lag is the larger error. A bot that places 1 to 5 seconds into its candle, as Freqtrade does, is
  credited with fills from prints before its order existed: 1.9% to 3.3% of the candle rule's fills are false on
  these days, against 0.4% to 0.5% at the candle open. The clock-grid rows (5.1% to 5.7%) are orders that land half
  a candle late on average, not a Freqtrade bot.
- The FIFO fills the rule misses are not gaps between candles. At lag 0 the placement clamp fills the orders priced
  through every print of their first candle (14 and 15 orders; FIFO fills them by trade-through), and the 5 left on
  2020-01-30 were filled when the opposite quote crossed the limit with no print at or through it, which no
  candle range can show.
- The candle rule always fills the whole quantity: 132,086 shares against FIFO's 131,476 on 2019-12-30 at lag 0
  (+0.5%), 135,526 at 5 s (+3.1%) and 139,364 on the clock grid (+6.0%); on 2020-01-30 141,756, 146,695 and 149,521
  against 140,717.
- The direction agrees with the zoo's continuous-time touch model: `nt_prob_touch[p=1]` fills on any touch and
  overfills by +1.43 and +0.93 pp of fill rate at a 60 s horizon on these two days ([Results](../RESULTS.md),
  section 4; a different shadow sample and horizon, so a consistency check, not a matched comparison). At a
  10-minute life most orders are traded through and the candle rule's fill-rate gap at lag 0 is +0.45 and +0.17 pp.

These are sampled orders of four US equities, not a crypto bot's orders: they show the size and the location of
the error (touch-only candles and placement lag), not the overfill of your strategy. The repository's crypto
captures (10 minutes of Bitstamp and Bitfinex) cannot serve as truth here: their feeds carry no execution records,
so exact queue outcomes are not established on them ([Results](../RESULTS.md), RD-12). For your own strategy,
reconcile your backtest against your own live fills (Part 1). The measurement script is the block above; the
per-case logic is covered by `test_candle_fill_audit_cases`, `test_candle_fill_audit_placement_clamp_lag_and_reasons`
and `test_candle_fill_audit_on_an_exact_replay` (`python/tests/live/test_freqtrade.py`).

## Caveats

- A dry-run is a simulator: reconciling a backtest against it measures the gap between two fill models. Only a
  live database (exchange executions) is truth; reconcile against live too (Part 1, step 3).
- Freqtrade keeps one aggregate fill per order, so partial-fill timing inside an order is lost; the fill time is
  `order_filled_date` (the last fill), which is the bot's clock when the exchange reports no last trade time.
- Pairing is by candle and role; a strategy that opens several trades on one pair in one candle can be paired
  in a different order than you expect. Check the pairing coverage and the `no_*_twin` rows.
- Hummingbot pairing needs the two bots in lockstep: started together, the same `order_refresh_time` and
  `filled_order_delay`, inventory skew off (or the same inventory), no hanging orders. After the first fill that
  differs, two `pure_market_making` bots quote different prices and nothing pairs; the fixtures are lockstep. Use a
  `pair_price_tol` of a tick or two, watch the coverage on the first line (a warning below 50%), and read unpaired
  orders as divergence of the bots, not of fills.
- The score's p-values assume enough orders and clusters: below 10 matched orders or 2 clusters the report marks
  the score not interpretable.
- Everything runs locally; nothing is uploaded.
