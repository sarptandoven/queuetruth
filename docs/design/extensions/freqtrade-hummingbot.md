# Extension: Freqtrade and Hummingbot reconciliation

Proposed SPEC text (5.7.1 importers, 5.7.5 external simulator arm) for the trading-bot importers, the run pairing,
the reconciliation document and the candle-fill audit. Implemented in
`python/queuetruth/live/importers/freqtrade.py` and `python/queuetruth/live/importers/hummingbot.py`; registered in
`python/queuetruth/live/importers/__init__.py` (`SIM_IMPORTERS["freqtrade"]`, `SIM_IMPORTERS["hummingbot"]`, and
`BOT_IMPORTERS` for the live arm). User guide: `docs/how-to/reconcile-freqtrade-hummingbot.md`.

## What is not wired yet (integrator)

- `LiveImportSpec.venue` is a closed `Literal`; it gains `freqtrade`, `hummingbot`, `sim_freqtrade` and
  `sim_hummingbot` (additive, 1.x minor). `queuetruth.live.import_venue` routes `freqtrade` / `hummingbot` to
  `BOT_IMPORTERS` and finishes with `_finish_import` (status `simulated` when the importer marked the orders so, as
  `import_freqtrade_live` does); `sim_*` already route through `import_sim` once the literal accepts them.
- CLI (`crates/cli/src/grammar.rs`): `live import --venue freqtrade|hummingbot|sim-freqtrade|sim-hummingbot`, with
  `--option pair_with=PATH` repeatable (a list) and the options below; and a command
  `queuetruth reconcile --kind freqtrade|hummingbot --sim PATH... --live PATH... --out DIR [--instrument-map F]
  [--window D] [--option K=V]` calling `reconcile_freqtrade` / `reconcile_hummingbot`. Until then the Python API is
  the entry point and `queuetruth score --live DIR --sim-log DIR` scores the written logs.
- Done at integration: `ReconcileReport` is the registered document `qt.bot_reconcile` (model in
  `queuetruth.schemas.reconcile`, still importable from the importer module), so `reconcile.json` carries
  `"schema": "qt.bot_reconcile"` and `"schema_version": 1`. Still open: `reconcile_orders.csv` as a Parquet table
  `qt.bot_reconcile_orders` with the `ReconcileOrder` columns (a format change).
- The generic part (`OrderView`, `explain_pair`, `explain_pairs`, `summarize`, `write_reconcile`, `run_score`,
  `auto_instrument_map`) lives in the Freqtrade module because this workstream owned two files; it belongs in
  `queuetruth.live.reconcile`. `finish_to_dir` imports the private `queuetruth.live._finish_import`; a public name
  for it would remove that.
- `docs/mkdocs.yml` gains nav entries for `how-to/reconcile-freqtrade-hummingbot.md` and this page, and the
  `queuetruth.live.import_sim` docstring names `freqtrade` and `hummingbot` next to `hftbacktest` and `nautilus`.
- The import manifest records the `pair_with` inputs (paths and sha256) of a simulator log; today a warning does.

## 5.7.1 (additions) Trading-bot importers

Every importer maps `(input paths, ImportOptions) -> ImportResult` with the shared `LogBuilder` semantics.

**Freqtrade live arm** (`import_freqtrade`; inputs: at most one trades database, any number of bot logs; a backtest
result fails `QT-CONFIG-001`):

- Trades database (SQLite, opened read-only): tables `trades` and `orders` of `persistence/trade_model.py`. Venue =
  `trades.exchange` (lower case; several exchanges fail `QT-CONFIG-001` unless option `venue`), instrument = the
  Freqtrade pair. One order row is one canonical order, key `<venue>:<account>:<order_id>` (order ids repeated
  across pairs are prefixed with the pair, warned). Role: `ft_order_side` `stoploss` -> stop-loss; the trade's
  entry side -> entry; else exit. `order_type` `limit` / `market` / `stop*` (`stop_limit` with `limit`).
- Mode: dry-run when every order id starts with `dry_run_` (a mix fails `QT-INPUT-002`; option `dry_run` forces).
  Dry-run: `submit_sent` (`ts_local_send`) and `ack` (`ts_local_recv`) at `order_date`, `validation_status =
  simulated`. Live: `ack` at `order_date` as `ts_exchange`, kind `gateway_time` (ccxt `timestamp`; option
  `order_date_clock = local` gives the dry-run events instead).
- Fill: one per row with `filled > 0`: `qty = filled`, `px = average` (soft: not exact at the exponent -> null,
  warned), at `order_filled_date` (else `order_update_date`, warned): `ts_exchange` kind `record_time` (live) or
  `ts_local_recv` (dry-run); `fill_id = <order_id>:fill`, `derived = true`; liquidity `taker` for market and stop
  orders, else `unknown`; fee = the trade's `fee_open_cost` / `fee_close_cost` when exactly one order of that role
  filled. Every event of a database order has precision `ms` (ccxt epoch milliseconds; dry-run `dt_ts()`).
  `order_filled_date` is ccxt's `lastTradeTimestamp`, or the bot's clock when the exchange omits it. A dry-run order
  (`dry_run_` id) with `0 < filled < amount` is warned (Freqtrade's dry-run fills all at once).
- Terminal: `canceled` -> `cancel_ack` at `order_update_date` (`ts_local_recv`), preceded by `cancel_sent`
  (`ts_local_send`) when `ft_cancel_reason` is set and does not start with `cancelled on exchange`; `expired` ->
  `expire`; `rejected` -> `reject`. Other statuses fail `QT-INPUT-002`.
- Time in force: option `tif` (`gtc` default, `ioc`, `fok`, `po`) for limit orders (Freqtrade does not store it;
  warned); stop orders `gtc`, market `other`. Futures exits are `reduce_only`.
- Bot log lines `<asctime> - <logger> - <level> - [(Buy|Sell) order fully cancelled. | Trade only had fully canceled
  entry orders. ]Removing Trade(id=I, pair=P, amount=A, is_short=S, leverage=L, open_rate=R, open_since=T) from
  database.` -> one limit entry order, key `<venue>:<account>:deleted-<I>-<epoch seconds of T>`, `submit_sent` at
  `T` (UTC, precision `s`), `cancel_ack` at `asctime` (zone option `log_tz`, default UTC; precision `ms`), preceded
  by `cancel_sent` when the bot cancelled: the `(Buy|Sell) order <reason> for <same Trade repr>.` line before it
  names a reason other than `cancelled on exchange`, or the bare `Removing` line follows `Could not fully cancel
  order for <Trade repr>, therefore not replacing.` (`replace_order_failed`); `Trade only had ...`
  (`handle_onexchange_order`) is an exchange cancel. `Removing Trade(` lines in another shape are counted in a
  warning.
- Numbers: doubles -> decimals rounded half-even to 15 significant digits; quantities and limit prices must be exact
  at the instrument exponents (`QT-INPUT-004`).

**Freqtrade simulator arm** (`import_freqtrade_sim`, `import_sim("freqtrade", ...)`; venue `sim:freqtrade`, source
`sim`, `validation_status = simulated`): one backtest result, or a bot run (database + logs, mapped as above).
Backtest result: the zip member `<zip stem>.json` (else its only `backtest-result*.json` member), a result JSON, or
`.last_result.json` (followed once; `latest_backtest` must be a bare file name in the same directory, else
`QT-INPUT-002`); `strategy` option when several. Each exported order -> one order with an `ack` at its placement
(the trade's `open_timestamp` for the first entry, else the fill time; kind `record_time`, `ms`) and one fill of its
full amount at `safe_price` at `order_filled_timestamp` (liquidity `unknown`, fee `fee_open` / `fee_close` times
notional in the stake currency); prices and sizes rounded half-even to the exponents (counted). An exported order
with a null `order_filled_timestamp` (open at the end of the backtest) has the `ack` only, placed at the trade's
last fill (counted). Exports without `orders` give one entry and one exit order per trade, filled at the trade's
open / close time (warned). Order type: the paired live order's (`limit` / `stop_limit` keep `safe_price` as the
limit, others none); unpaired, the config's `order_types` (`entry`; `exit`, `emergency_exit`, `force_exit` by exit
reason; `stoploss` for `stop_loss`, `stoploss_on_exchange`, `trailing_stop_loss`, `liquidation` exits: `limit` ->
`stop_limit`, `market` -> `stop`), else `limit` (warned). With `pair_with`, options `dry_run`, `venue` and
`order_date_clock` describe the simulator arm and `live_dry_run`, `live_venue` the `pair_with` run (without
`pair_with` those two fail `QT-CONFIG-001`); a warning records the `pair_with` paths, their sha256, the venue and
the account the keys come from.

**Pairing** (`pair_runs`, option `pair_with`): candle `c(t) = t - t mod timeframe` (timeframe from option, the
backtest, or `trades.timeframe`). A live trade pairs with the unpaired simulator trade of the same pair and
`is_short` minimising `|c(open_live) - c(open_sim)| / timeframe <= pair_tolerance_candles` (default 1), ties by
earlier simulator open (trades bucketed by pair and direction, sorted by candle: O(n log n)). Within a trade pair
and a role (entry; exit including filled stop-loss orders; untriggered stop-loss orders, which pair only with each
other), simulator
orders in order of their reference time `r` (placement for the first backtest entry and for database orders, fill
time otherwise) each take the unpaired live order with `c(placed) <= c(r)` and the latest placement, else the one
with `c(placed) <= c(r) + tolerance * timeframe` and the earliest placement. A paired simulator order's key is the
live key; its fill time is `max(fill time, live order's placed time)` (`reason` keeps the candle time). Unpaired:
`sim:freqtrade:<account>:<ref>`.

**Hummingbot** (`import_hummingbot`, `import_hummingbot_sim`): database tables `Order`, `OrderStatus`, `TradeFill`
(`SqliteDecimal(6)` integers / 10^6; REAL values read as decimals) and trades CSVs. One market per log (option
`market`); venue = the market without `_PaperTrade` / `_paper_trade`; a paper market imported as the live arm is
`simulated`. Key `<venue>:<account>:<exchange_order_id or client id>`. Side from `*OrderCreated` or the fills (a
contradiction fails `QT-INPUT-002`; neither -> excluded). `submit_sent` at `creation_timestamp`; fills at their
time as `ts_local_recv`, `fill_id = exchange_trade_id` (CSV and database copies deduplicate), fee =
`trade_fee_in_quote` in the quote asset else a single flat fee (a stored 0 next to a non-zero `trade_fee` is
recomputed: percent x price x amount when charged in the quote, else the single flat fee; counted); a fill in a
database and a CSV keeps the CSV copy (full precision); `OrderCancelled` -> `cancel_ack`, `OrderFailure` ->
`reject`, `OrderExpired` -> `expire` (receive times); `*OrderCompleted` and unknown statuses counted. `LIMIT_MAKER`
-> post-only, `MARKET` fills taker, `LIMIT_MAKER` fills maker. Pairing (`pair_orders`): a paper order takes the
unpaired live order of the same symbol and side with `|price diff| <= pair_price_tol` (default 0) and the nearest
creation time within `pair_window_ms` (default 1000); live orders bucketed by (symbol, side) and sorted by creation
time (O(n log n)). An arm whose orders all lack a creation time (trades CSVs alone) fails `QT-CONFIG-001` naming
the database. Option `market` names the live market in `import_sim` and `reconcile_hummingbot`; the paper arm is
the market's `_PaperTrade` market.

## Reconciliation document

`reconcile_freqtrade(sim inputs, live inputs, out, ...)` (sim: a backtest result or a dry-run bot) /
`reconcile_hummingbot(sim inputs, live inputs, out, ...)` (sim: the paper bot) write `<out>/live`, `<out>/sim`
(canonical logs), `<out>/score` (`score` with `sim.kind = external`, `window_ns` default 1 day / 60 s) and
`<out>/reconcile.json` (`ReconcileReport`: kind, run_id, log paths relative to `out`, order counts,
`pairing_coverage = n_paired / n_live_orders`, `counts` per category, `sim_price_advantage_quote` and
`missed_fill_notional` per instrument, score result (with `n_clusters` and `interpretable = n_matched >= 10 and
n_clusters >= 2`) or error, the spec as `options`, warnings, rows) and `<out>/reconcile_orders.csv`. Without an
instrument map the instruments are numbered 1..n in sorted order with the default exponents (warned). Swapped arms
fail `QT-CONFIG-001` unless `arms_as_given`: a live Freqtrade bot as the simulator arm with a non-live bot arm, a
paper Hummingbot live arm with a non-paper simulator arm. The spec (`reconcile_spec`) is `{kind:
"<kind>_reconcile", sim: [FileRef], live: [FileRef], instrument_map: FileRef | null, account, window_ns,
price_tol_bps, time_tol_ns, score, arms_as_given, options: {...}}`; `run_id` is SPEC 7.3's (`queuetruth.schemas.run_id`
with the engine version, each FileRef resolved to its file's sha256), so it does not depend on `out` or on where
the inputs live, and `reconcile.json` is byte-identical across output directories. A paper Hummingbot run below
50% pairing coverage is warned.

Each row is a live order, a simulator order, or a pair. Category (first that applies): `stop_not_triggered` (an
unfilled stop-loss order without a twin, or two paired unfilled stop-loss orders; no issues), `no_live_twin`,
`no_sim_twin`, `missed_fill` (sim filled > 0, live 0), `extra_live_fill`, `partial_fill` (filled quantities
differ), `price` (`|bps| > price_tol_bps`, default 0.5; `bps = d (P_live - P_sim) / P_live * 10^4`, `d = +1` buy,
`-1` sell, average prices), `timing` (backtest: live first fill outside `[candle, candle + timeframe)` widened by
`time_tol_ns`, default 0; else `|delay| > time_tol_ns`, default 5 s between two Freqtrade bots
(`process_throttle_secs`), 1 s for Hummingbot), `match` (both filled), `both_unfilled`.
`issues` lists every one that applies. `sim_price_advantage_quote = d (P_live - P_sim) min(q_live, q_sim)`.

## Candle-fill audit

`candle_fill_audit(replay_dir, timeframe, timeout, anchor, lag_ns)` over `shadow_results.parquet`,
`trades.parquet` and optionally `shadow_fills.parquet` of a replay with shadows. Excluded (counted):
`ambiguous_race`, `out_of_session`, `end_state` `rejected*`, null price, `end <= ts_live` with `end =
min(ts_cancel_effective, ts_live + timeout)`. Candles: `anchor = submit` -> `[a + k tf, a + (k+1) tf)` with `a =
ts_live - lag_ns` (`0 <= lag_ns < tf`), `clock` -> the clock grid from `c(ts_live)` (`lag_ns` must be 0), for
every candle starting before `end`; each candle's range = min / max of its trade prints (no prints -> no range).
Candle fill: the first candle has prints and a buy is above its high (a sell below its low: Freqtrade's placement
clamp, `marketable_at_placement`), or some candle has `low <= price <= high`; touch when that candle's low (buy) /
high (sell) equals the price. Exact fill: `ts_first_fill < end`. Window reached: a print in `[ts_live, end)` at or
through the price. Counts per group (all, touch, through, per side): 2x2, `marketable_at_placement`,
`candle_only_queue` (candle-only and window reached), `candle_only_outside_life`, `exact_only_by_reason` (the
replay's `first_fill_reason`), `overfill_share = candle_only / candle_filled` with a Wilson 95% interval
(independent orders) and a percentile 95% interval from 2,000 bootstrap draws (seed 0) over clusters (instrument x
30-minute block of `ts_live`; null with one cluster), `n_clusters`, fill rates; quantities: full order quantity
for candle fills, fills before `end` from `shadow_fills.parquet`.
