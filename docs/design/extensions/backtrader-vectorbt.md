# Extension: Backtrader, VectorBT and LEAN importers

Proposed SPEC text (5.7.1 importers, 5.7.5 external simulator arm) for the importers of backtesters that have no
live arm of their own. Implemented in `python/queuetruth/live/importers/backtrader.py`,
`python/queuetruth/live/importers/vectorbt.py`, `python/queuetruth/live/importers/lean.py` and the shared
`python/queuetruth/live/importers/backtest.py`; registered as `SIM_IMPORTERS["backtrader"]`, `SIM_IMPORTERS["vectorbt"]`
and `SIM_IMPORTERS["lean"]`. User guides: `docs/how-to/reconcile-backtrader-vectorbt.md` and
`docs/how-to/reconcile-lean.md`. The reconciliation document and its explanations are those of
[Freqtrade and Hummingbot reconciliation](freqtrade-hummingbot.md).

## Wiring

- `LiveImportSpec.venue` accepts `sim_backtrader` and `sim_vectorbt` (schema 1.3, additive); both route through
  `queuetruth.live.import_sim`. The CLI takes `live import --venue sim-backtrader | sim-vectorbt` with `--option
  KEY=VALUE`, and `queuetruth reconcile --kind backtrader | vectorbt --sim PATH... --live DIR --out DIR` (the
  Python-only group of `queuetruth.live.reconcile_cli`).
- `qt.bot_reconcile.kind` gains `backtrader` and `vectorbt` (additive; `schema_version` stays 1).
- LEAN: `LiveImportSpec.venue` accepts `sim_lean`, the CLI `live import --venue sim-lean` and `reconcile --kind
  lean`, and `qt.bot_reconcile.kind` gains `lean` (all additive).
- Neither library is imported or added as a dependency: the importers read exported JSON and CSV only.
- Still open: the generic code (`OrderView`, `explain_pairs`, ...) still lives in the Freqtrade module; the shared
  backtest code imports it from there.

## 5.7.1 (additions) Backtester importers

Every importer maps `(input paths, ImportOptions) -> ImportResult` with the shared `LogBuilder` semantics. The log
venue is `sim:backtrader` / `sim:vectorbt`, `source = sim`, `validation_status = simulated`.

**Backtrader** (formats of release 1.9.78.123): JSON lines of the documented `QtOrderLog` analyzer. Order lines
(`event = order`): `ref`, `data`, `status` (`Order.Status` name), `ordtype`, `exectype` (`Order.ExecTypes` name),
`size` (signed), `price`, `pricelimit`, `created`, `dt`, `valid`, `exbits` (`[{dt, size, price, comm}]`, the
`iterpending()` bits of the notification), `order_key`. One order per `ref` of a file (several files: `<file>-<ref>`, since
`Order.ref` restarts per run; the same content twice, or notification lines with a Transactions dump, fail
`QT-CONFIG-001`): `submit_sent` at `created` (`ts_local_send`), `ack` at the `Accepted` line's `dt` only
(`ts_exchange`, `record_time`; an order refused at submission has none), one fill per bit (`fill_id =
<ref>:<k>`, fee = `comm`), `cancel_ack` / `expire` / `reject` (`Canceled` / `Expired` / `Margin`, `Rejected`) at
the line's `dt`; `Created` is counted, a second terminal line is counted. Types: `Market`, `Close` -> market;
`Limit` -> limit at `price`; `Stop`, `StopTrail` -> stop; `StopLimit` -> stop_limit at `pricelimit`;
`StopTrailLimit` -> stop_limit without a limit price; `Historical` -> other. Time in force `gtc`, `gtd` with
`valid`, `other` for market. Trade lines (`event = trade`) are counted and their closed `pnlcomm` summed in a
warning. A `Transactions` analyzer dump (`{datetime: [[amount, price, sid, symbol, value], ...]}`, `date` key
skipped) gives one order per entry known from its fill only. Failures (`QT-INPUT-002`): an unknown status, order
type or side, a size whose sign contradicts the side, a `Partial` / `Completed` line without `exbits`, a non-positive
price, a limit order without a limit price, a line that is neither.

**VectorBT** (formats of release 0.28.1): `records_readable` of `pf.orders` as CSV or JSON records: `Order Id`,
`Column`, `Timestamp`, `Size`, `Price`, `Fees`, `Side`. One order per (`Column`, `Order Id`) (a repeat is dropped
and counted) with an `ack` and one fill of `Size` at `Price` at `Timestamp` (fee = `Fees`, liquidity `unknown`),
order type option `order_type` (`market` default, or `limit` at `Price`). Trade records (`Exit Trade Id`, `Entry
Trade Id`, `Trade Id` or `Position Id`; `Entry Timestamp`, `Avg Entry Price`, `Exit Timestamp`, `Direction`,
`Status`, ...) next to order records are counted and their closed `PnL` summed in a warning; alone they give one
entry order per trade and one exit order per closed trade (warned). Integer timestamps are epoch milliseconds;
values below 10^11 are bar numbers and fail `QT-INPUT-002`, as do raw records (`id, col, idx, ...`) and
column-oriented JSON. More than one column fails `QT-CONFIG-001` unless option `column` or `instruments`
(`instrument` alone does not choose a column). Epoch values are read in `data_tz` (pandas writes a naive index's
wall-clock time as UTC).

**LEAN** (formats of release tag 18241): the result JSON's `orders` (`Order`: `id`, `symbol` `{value, id,
permtick}`, `time`, `type` and `status` and `direction` as numbers, signed `quantity`, `limitPrice`, `stopPrice`,
`properties.timeInForce`) and the order events (`SerializedOrderEvent`: `orderId`, `orderEventId`, `symbolValue`,
`time` in Unix seconds, `status` as a camelCase name, `fillPrice`, signed `fillQuantity`, `direction`, `orderType`,
`quantity`, `limitPrice`, `orderFeeAmount`, `orderFeeCurrency`, `message`) from `<algorithm id>-order-events.json`,
a result's `orderEvents`, or a CSV with those names. Names are matched without case, `-` or `_` (camelCase,
hyphenated and C# property names); enums as numbers or names. One order per id: `submit_sent` at `time`, `ack` at
the first `submitted` event, one fill per `partiallyFilled` / `filled` event with a non-zero fill quantity
(`fill_id = <order id>:<order event id>`; a zero one, written after a refused update, is counted), `cancel_ack` at
`canceled`, `expire` at `canceled` with "The order has expired.", `reject` at `invalid` (an invalid order without a
`submitted` event gets no `ack`); `new`, `none`, `cancelPending`, `updateSubmitted` are counted, and so is an order
rounded to quantity 0 and invalidated (left out). Fees: a fee in the fill price's currency is kept; a crypto buy's
fee in its base asset (subtracted from `fillQuantity` by LEAN) is added back to the quantity and converted to quote
at the fill price; other currencies are dropped and counted. Contingent orders are warned. Decimals are exact. Types: Market, MarketOnOpen, MarketOnClose -> market; Limit -> limit; StopMarket,
TrailingStop -> stop; StopLimit, LimitIfTouched -> stop_limit at `limitPrice`; OptionExercise and the combo types
fail `QT-INPUT-002`. Without an orders section the order is built from its events (`orderType` required; quantity and prices of the
last event).
Events given twice are counted once. `QT-CONFIG-001`: two orders sections, two `algorithmId`s, the same input twice,
appending to an existing log, option `data_tz` (LEAN times are UTC). `QT-INPUT-002`: an orders section without any event, an event of an order the
section does not hold, of another symbol, or the same event id with other content, fills beyond the order quantity,
a final status (filled, canceled, invalid) without its event. With `timeframe`, a fill stamped after its order's
submission (LEAN stamps the bar's end) moves one bar earlier, never before the submission (counted).

**Shared** (all three; LEAN has no `data_tz`): times are ISO 8601 text, or a date alone (midnight; precision `s`), naive in option `data_tz`
(default UTC; precision from the text), or epoch ms; doubles are read as decimals rounded to 15 significant digits, then rounded half-even to the instrument
exponents (counted), in place, so a reconciliation quotes the logged values. Options: `timeframe` (bar length; the
fill time is known to `[t, t + timeframe)`), `fill_delay_bars` (fills `k` bars later; needs `timeframe`),
`instrument` / `instruments` (`LABEL:NAME,...`; a label naming no data feed or column fails `QT-CONFIG-001`), `venue`, `map_venue`, `price_exp`, `size_exp`, `pair_with`,
`pair_window_ms`.

## 5.7.5 (additions) The live arm and the pairing

The live arm is one canonical live-log directory. Placement = the first `ack` (else `submit_sent`) of an order;
fills are its `fill` events; end = its first terminal event. Pairing (`pair_sim_live`): first, a simulator order
whose `order_key` hint (`order.info["order_key"]`, an id of the form `<venue>:<account>:<id>`) names a live order of
the same side takes it. Then each simulator order in order of its reference time `r` (submission, else placement,
else first fill) takes the unpaired live order of the same instrument and side with `|placed - r| <=
pair_window_ms` (default 2 x `timeframe`, else 60 s), minimising `(|placed - r|, |qty difference|, placed, key)`;
live orders are bucketed by (instrument, side) and sorted by placement (O((L + S) log L)). A paired simulator order
takes the live `order_key`, and each of its fills stamped before the live order's placement is moved to that
placement (counted; the bar time kept in the fill's `reason`). With option `pair_with` the importer pairs against
that log and warns with its orders' sha256 and venue.

`reconcile_backtrader(sim, live, out, ...)` / `reconcile_vectorbt(...)` / `reconcile_lean(...)`: a live arm whose every order is a
simulator's or simulated fails `QT-CONFIG-001` unless `arms_as_given`; several venues need option `venue`. Without
an instrument map the live log's ids and scales are used, instruments without an id numbered after its largest id.
Writes `<out>/live` (the live rows with those ids), `<out>/sim`, `<out>/score` (`window_ns` default 1 day) and
`reconcile.json` / `reconcile_orders.csv` with `kind = backtrader | vectorbt | lean`; `time_tol_ns` defaults to 0 with
`timeframe` (bar resolution) and 60 s without. The spec's `live` inputs are the live log's table files, hashed into
`run_id`. Below 50% pairing coverage, and for simulator instruments absent from the live log, the report warns.
