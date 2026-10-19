# Import fills from my venue or broker

**Goal:** turn your venue's order and fill records into one canonical live log that the scorer
(`queuetruth score`), the live calibration (`queuetruth calibrate-live`), the latency fit and the schedule builder
(`queuetruth shadows from-live`) all read.

**You need:** your own order and fill records, in one of the formats listed below: captured WebSocket messages,
REST history responses, a broker export or a FIX log. Everything runs locally; nothing is uploaded.

## The canonical live log

A canonical live log is a directory with three files:

- `orders.parquet` (`qt.live_orders`): one row per order. `order_key` (`<venue>:<account>:<venue order id or root
  client order id>`), `venue`, `account`, `instrument`, `instrument_id`, `venue_order_id`, `client_order_id`,
  `client_order_id_chain`, `side`, `order_type`, `tif`, `post_only`, `orig_qty` and `limit_px` in fixed-point
  units (with the venue's decimal text in `qty_str` and `px_str`), `price_exp`, `size_exp`, `strategy_tag`,
  `validation_status`, `raw`.
- `events.parquet` (`qt.live_events`): one row per lifecycle event, numbered by `event_seq` from 1 per order.
  `event_type` is one of `submit_sent`, `ack`, `reject`, `post_only_reject`, `amend_sent`, `amend_ack`,
  `amend_reject`, `cancel_sent`, `cancel_ack`, `cancel_reject`, `fill`, `fill_bust`, `fill_correct`, `expire`,
  `restate`, `done`. Each event has up to three clocks: `ts_local_send` (your send time), `ts_exchange` (the venue's
  time) and `ts_local_recv` (your receive time), plus `ts_exchange_kind` (`match_time`, `record_time`,
  `gateway_time` or `unknown`) and `ts_precision` (`s`, `ms`, `us`, `ns`). Fill rows carry `qty`, `px`, `fill_id`,
  `liquidity` (`maker`, `taker`, `unknown`), `fee` (positive = cost) and `fee_ccy`; `cum_qty` and `leaves_qty`
  where known; `derived = true` when the importer computed the fill rather than read it.
- `manifest.json` (`qt.live_manifest`): venues, accounts, row counts, the instrument map's sha256 and `created_by`.

Field types are in the schema reference: [`qt.live_orders`](../reference/schemas.md#qtlive_orders),
[`qt.live_events`](../reference/schemas.md#qtlive_events).

Start with a log that already exists. `fixtures synth` writes one (200 synthetic orders on a synthetic CME tape,
venue `cme`, account `SYNTH`):

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --out synth
queuetruth live validate --live synth/own_orders/live
queuetruth --output-format json live validate --live synth/own_orders/live
```

## Import

<!-- check-docs: skip (needs your records) -->
```sh
queuetruth live import --venue binance --account main --instrument-map instrument_map.csv \
    --in user_stream_2026-10-05.jsonl --in mytrades_2026-10-05.json --out live/
```

`--venue` is one of `binance`, `bybit`, `okx`, `coinbase`, `kraken`, `deribit`, `hyperliquid`, `ibkr-flex`,
`fix44`, or one of the simulator outputs `sim-hftbacktest` and `sim-nautilus` (their options go in
`--option KEY=VALUE`). FIX logs take the `--fix-*` flags ([Score my QuickFIX-driven strategy](score-quickfix-strategy.md)). `--in` repeats; every input of one run goes into the same log and fills seen in more than one input are
deduplicated. `--append` adds to an existing log: orders with an existing `order_key` keep their row, events already
present (same fill id, or an identical lifecycle event) are dropped and counted, and events are renumbered
(`test_import_venue_append_dedupes`). `--tz` is the account time zone of venue-local times (IBKR Flex). The output is a
`qt.live_import_summary`: `n_orders`, `n_events`, `n_fills`, `dedupe_dropped`, `unmatched_fills`, `warnings`,
`validation_status` and the written files. The import validates the log before writing it and fails on any
validation error (see "Validate" below).

The same import from Python is `qt.import_live({"venue": ..., "inputs": [{"path": ...}], "out": ..., "account":
..., "instrument_map": {"path": ...}, "append": False, "fix": {...}}, tz=None)`. The `fix` options
(`delimiter`, `local_ts_regex`) have no CLI flag in v0.1; set them through the Python API or a `jobs submit` spec.

Try it on a synthetic fixture from the repository (written from Binance's public documentation, fake account):

<!-- check-docs: run -->
```sh
F="$QT_SRC/python/tests/live/fixtures/binance"
queuetruth live import --venue binance --account acct1 \
    --in "$F/01_ws_execution_report_lifecycle/user_data_stream.jsonl" --out bn-live
queuetruth live validate --live bn-live
```

This fixture gives 5 orders, 15 events and 6 fills (`test_import_venue_writes_valid_dir` in
`python/tests/live/test_import_api.py`). Importing the same inputs twice gives byte-identical files (same test).

Two inputs with the same fill, a WebSocket copy and a REST `myTrades` copy:

<!-- check-docs: run -->
```sh
F="$QT_SRC/python/tests/live/fixtures/binance/04_ws_rest_dedupe"
queuetruth live import --venue binance --account acct1 \
    --in "$F/user_data_stream.jsonl" --in "$F/myTrades.json" --out bn-dedupe
```

One fill is kept and `dedupe_dropped` is 1 (O-LIVE-1 golden `binance/04_ws_rest_dedupe`, checked by
`python/tests/live/test_importers.py`).

## Rules shared by every importer

These come from `python/queuetruth/live/builder.py` and apply to all venues.

- **Inputs.** JSON documents, JSON lines and CSV, detected per record. Every JSON-lines input may use a capture
  wrapper: `{"recv_ns": <int>, "msg": <raw>}` gives `ts_local_recv`; `{"dir": "out" | "in", "local_ns": <int>,
  "msg": <raw>}` is a request/response capture where `out` gives `ts_local_send` and `in` gives `ts_local_recv`.
  `msg` may be the raw message object or its JSON text.
- **Order identity.** Records join by venue order id and client order id; a client id never joins two orders with
  different venue order ids. `client_order_id` is the first client id; `client_order_id_chain` lists all of them
  when there is more than one.
- **Fill dedupe key: (instrument, `fill_id`).** Venue trade ids can be per symbol. A stream copy wins over a history
  copy, else the first seen; every dropped copy counts in `dedupe_dropped` and conflicting copies warn. Fills
  derived from cumulative quantities are dropped (and counted) when the same order has per-fill records.
- **History records.** Lifecycle events synthesised from a REST or CSV order record (for example an `ack` at the
  creation time) are dropped (and counted) when a captured event of the same type exists. Captured streams keep
  their capture order; history records are placed by timestamp.
- **Missing quantities.** `cum_qty` is completed as the running net filled quantity (busts subtract the referenced
  fill, corrections replace it); `leaves_qty` as current total minus `cum_qty`, only when an order-level record gave
  the total.
- **Orders seen only through fills** get `orig_qty` = summed fill quantity, `order_type` and `tif` = `other`,
  `limit_px` = null, a warning, and their fills count in `unmatched_fills`.
- **Nothing is dropped silently.** Records that cannot be mapped (no side, instrument or quantity) are excluded,
  counted and named in the warnings; messages without a canonical event are counted per reason.
- **Validation label.** Every importer is tested against synthetic fixtures written from the venue's public
  documentation (O-LIVE-1, `python/tests/live/test_importers.py`), not against real account exports, so every
  imported log has `validation_status = fixture_only` in v0.1. Check the import summary against your own records
  the first time.
- **Entry time.** The summary always has a line `entry_fallback_share=...`: the share of resting limit orders whose
  first `ack` has no `match_time` or `gateway_time` exchange time. Those orders cannot use the default entry rule
  (exchange acknowledgement) and fall back to send time plus latency when scheduled.

## Instrument map and exact decimals

`--instrument-map` is a CSV:

```text
venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size
binance,BTCUSDT,1,,-9,-8,0.01,0.00001
```

`instrument_id` must match the market-data dataset you will score against (read it from the dataset's
`instruments.parquet`). `tick_size` and `lot_size` are decimal strings in real units and must be exact at
`price_exp` / `size_exp`. A row with a `session_date` (`YYYY-MM-DD`, UTC) applies to that date only and wins over a
dateless row for the same venue and instrument; two rows with the same (venue, instrument, session_date) fail with
`QT-INPUT-003`. Prices or quantities that are not a multiple of `tick_size` / `lot_size` warn. Without a matching
row an importer uses the venue defaults (`price_exp = -9`; `size_exp = -8` for crypto venues and FIX, 0 for IBKR)
and leaves `instrument_id` null, and such orders cannot be scheduled.

Every price and size is parsed from the venue's decimal text (JSON numbers keep their token text, never a binary
float) and converted with `value * 10^-exp`. A value that is not an exact integer at the exponent fails the whole
import with `QT-INPUT-004`; nothing is rounded. Fees are stored as float64 in `fee`. Here a Deribit fixture is imported with `price_exp = 0`, so
its fractional prices cannot be represented (`test_inexact_decimal_fails_qt_input_004`):

<!-- check-docs: run exit=2 -->
```sh
F="$QT_SRC/python/tests/live/fixtures/deribit/01_ws_user_orders_trades"
printf 'venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size\n' > deribit_map.csv
printf 'deribit,BTC-PERPETUAL,101,,0,0,1,10\n' >> deribit_map.csv
queuetruth live import --venue deribit --instrument-map deribit_map.csv \
    --in "$F/ws_notifications.jsonl" --out deribit-live
```

## Venues

All importer facts below are from the code in `python/queuetruth/live/importers/`. "Kind" is
`ts_exchange_kind`. Fees are stored as costs (positive = you paid, negative = rebate) whatever the venue's sign.

### binance

- **Inputs:** user-data stream `executionReport` messages (bare, or inside the `{"subscriptionId", "event"}`
  envelope); WebSocket API request/response captures (`order.place` -> `submit_sent`, `order.cancel` ->
  `cancel_sent`, `order.amend.keepPriority` -> `amend_sent`, error responses -> `reject`, `post_only_reject` (code
  -2010), `cancel_reject`, `amend_reject`); REST `GET /api/v3/myTrades` (fills, joined through `orderId`, no client
  id) and `GET /api/v3/allOrders` (order records: `ack` at `workingTime`, terminal event at `updateTime`, both
  `record_time`).
- **Fill id:** trade id `t`. Quantities `l` / `L`, cumulative `z`, leaves `q - z`, fee `n` / `N`.
- **Exchange time:** `T`, kind `match_time`; `E` with kind `record_time` when `T` is absent. Epoch unit (ms or us)
  is detected.
- **Maker/taker:** `m`.
- **Quirks:** the cancel's own client id is `c`, the order's is `C`. `REPLACED` (amend keepPriority, size down) ->
  `amend_ack` with `amend_keeps_priority = true`. `TRADE_PREVENTION` -> `expire` with the prevented quantity in the
  reason. `LIMIT_MAKER` -> `tif = post_only`.

### bybit

- **Inputs:** private WebSocket `order` and `execution` topics; REST `/v5/execution/list` and `/v5/order/history`
  (`result.list` envelope or bare lists).
- **Fill id:** `execId`. Fills come only from executions with `execType = Trade`; other exec types are counted and
  skipped.
- **Exchange time:** `execTime` (ms), `match_time`. Order-topic `updatedTime` is `record_time`.
- **Maker/taker:** `isMaker`. Fee `execFee` (positive = cost, negative = rebate).
- **Quirks:** order messages are diffed against the previous state of the same order; a price or size change while
  working is an inferred `amend_ack` with `amend_keeps_priority = null` (Bybit documents nothing). Cancel reasons:
  `EC_PostOnlyWillTakeLiquidity` -> `post_only_reject`, `CancelByUser` -> `cancel_ack`, others (SMP, IOC/FOK
  remainder) -> `expire`. The documented double `Filled` message carrying a cancel rejection -> `cancel_reject`; an
  identical repeat is dropped and counted.

### okx

- **Inputs:** WebSocket `orders` channel, the VIP `fills` channel, REST `/api/v5/trade/fills(-history)` and
  `/api/v5/trade/orders-history(-archive)` (`{"code", "data"}` envelope or bare lists).
- **Fill id:** `tradeId` (unique per instrument). Quantities `fillSz` / `fillPx`; `cum_qty` = `accFillSz`.
- **Exchange time:** `fillTime`, `match_time`; order-state rows use `uTime` (`record_time`).
- **Maker/taker:** `execType` M / T.
- **Quirks:** OKX reports a charged fee as negative, so the canonical fee is `-fillFee` (per update, never the
  cumulative `fee`) or `-fee` on REST fills. `amendResult = 0` -> `amend_ack` (priority null), `-1` ->
  `amend_reject`. `canceled` with `cancelSource` 1 -> `cancel_ack`, 31 -> `post_only_reject`, anything else ->
  `expire`. `tag` -> `strategy_tag`. REST fills alone are a fills-only source
  (`test_fills_only_coverage_flag`).

### coinbase

- **Inputs:** WebSocket `user` channel; REST List Fills (`{"fills": [...]}`) and List Orders (`{"orders": [...]}`,
  order records only).
- **Fill id:** List Fills `entry_id` (`trade_id` is not unique for adjusted fills). The `user` channel is
  order-level, so fills from it are **derived** by differencing `cumulative_quantity`: `derived = true`, `fill_id`
  null, `liquidity = unknown`, price = change of `filled_value` / change of quantity (null with a warning when not
  exact), fee = change of `total_fees`. Derived fills are dropped (and counted) for orders that have List Fills
  rows.
- **Exchange time:** List Fills `trade_time`, `match_time`; `user` channel message `timestamp`, `record_time`.
- **Maker/taker:** List Fills `liquidity_indicator`; unknown for derived fills.
- **Quirks:** orders in the subscribe snapshot existed before the capture and become `restate`, not an entry.
  `size_in_quote = true` converts the quote size to base by exact division (else `QT-INPUT-004`). `REVERSAL` ->
  `fill_bust`, `CORRECTION` -> `fill_correct`, referencing the fill with the same `trade_id` in the same order.

### kraken

- **Inputs:** WebSocket v2 `executions` channel.
- **Fill id:** `exec_id`. Quantities `last_qty` / `last_price`.
- **Exchange time:** `timestamp` (RFC 3339), `match_time`.
- **Maker/taker:** `liquidity_ind` m / t. `fees[]` are summed when all entries have the same asset, else the fee is
  null with a warning.
- **Quirks:** updates after `pending_new` carry only changed fields, so order attributes come from the
  `pending_new` or snapshot record. `amended`: `amend_keeps_priority = true` for a size decrease at the same price,
  false for a price change or size increase. A post-only order cancelled before `new` -> `post_only_reject`; a
  user cancel -> `cancel_ack`; other reasons -> `expire`. GTD has no canonical tif and becomes `other`.

### deribit

- **Inputs:** JSON-RPC notifications on `user.orders.*` and `user.trades.*`; REST `get_user_trades_by_*`
  (`result.trades`) and `get_order_history_by_*` / `get_order_state`.
- **Fill id:** `trade_id`. Quantities `amount` / `price`; fee positive = cost, negative = maker rebate.
- **Exchange time:** trade `timestamp` (ms), `match_time`; order notifications are `record_time`.
- **Maker/taker:** `liquidity` M / T.
- **Quirks:** side comes from the order record when one is joinable; otherwise from the trade's `direction`, flagged
  `ASSUMED` in the event reason and a warning. `replaced = true` with a changed amount or price -> `amend_ack`
  (amount down at the same price keeps priority). `label` is the client order id.

### hyperliquid

- **Inputs:** WebSocket `userFills` and `orderUpdates`; info responses `userFills` / `userFillsByTime` and
  `historicalOrders`.
- **Fill id:** `<time>:<coin>:<tid>` (`tid` is unique only with block time and coin).
- **Exchange time:** `time` (ms), `match_time`; order `statusTimestamp`, `record_time`.
- **Maker/taker:** `crossed = true` -> taker. Fee negative = rebate (kept), `feeToken` is the currency.
- **Quirks:** `origSz` is the order size (`sz` is the remaining size); `cloid` is the client order id.
  `orderUpdates` carry no tif, so `tif = other` unless the status implies it. A modify executes as cancel + new
  order on Hyperliquid; the importer keeps it as one order with an `amend_ack` (priority lost, new oid in the
  reason). A fills response of exactly 2,000 rows warns that older fills may be missing.

### ibkr-flex

- **Inputs:** Flex Query "Trades" output with Level of Detail = Executions, as XML (`<Trade .../>` / `<Order .../>`)
  or CSV (plain header, or the `BOF/HEADER/DATA/EOF` layout). XML attribute names and CSV labels are both accepted.
- **Fill id:** `ibExecID` (fallback `tradeID`). Order id `ibOrderID`, client id `orderReference`.
- **Exchange time:** `dateTime` (or `tradeDate` + `tradeTime`) in the account's statement time zone, given with
  `--tz` (IANA name; default UTC with a warning; an explicit UTC offset in the value wins). Precision `s`, kind
  `match_time` (`test_ibkr_tz_argument_and_default`).
- **Maker/taker:** none in Flex; `liquidity = unknown`. Fee = `-ibCommission` (IBKR reports commissions as negative).
- **Quirks:** Flex has no order records, so every order is rebuilt from its executions (`limit_px` null): an IBKR
  log is always **fills-only** (see below). `BUY (Ca.)` / `SELL (Ca.)` or `transactionType = TradeCancel` ->
  `fill_bust` referencing the execution by `origTradeID`; this layout is not verified against a real export.
  Default `size_exp = 0`.

<!-- check-docs: run -->
```sh
F="$QT_SRC/python/tests/live/fixtures/ibkr_flex/01_flex_xml_executions"
queuetruth live import --venue ibkr-flex --account U1 --tz America/New_York \
    --in "$F/flex_trades.xml" --out ib-live
```

The warnings name the two orders rebuilt from fills and the line `coverage=fills_only for 2 of 2 order(s)`.

### fix44

- **Inputs:** QuickFIX C++ (and its Python binding), QuickFIX/J, QuickFIX/n and QuickFIX/Go FileLog message logs
  (current, backup and rotated, optionally gzip-compressed), FileStore bodies and sequence-number files, whole
  `FileLogPath` / `FileStorePath` directories, or any one-message-per-line FIX log (SOH, `|` or `^A`); FIX 4.2, 4.4
  and 5.0SP2. QuickFIX log-line time stamps are recognised per line; other loggers need `--fix-local-ts-regex`.
  Full walkthrough: [Score my QuickFIX-driven strategy](score-quickfix-strategy.md).
- **Checks:** every message is framed by BodyLength (9) and checked against CheckSum (10); failing, truncated or
  malformed messages are excluded and counted (`--fix-checksum keep` imports them). MsgSeqNum gaps, PossDupFlag
  resends, SequenceReset and session resets are tracked per direction; repeated resends are dropped and counted.
- **Messages:** outbound 35=D / F / G -> `submit_sent` / `cancel_sent` / `amend_sent` with `ts_local_send` = log
  time, else SendingTime (52). Inbound execution reports get `ts_local_recv` only from the log time (inbound 52 is the
  venue's clock). ExecType (150): 0 `ack`, F (and FIX 4.2 1 / 2) `fill`, 5 `amend_ack` (OrderQty 38 = new total), 4
  `cancel_ack` when you asked for the cancel else `expire`, 8 `reject` (`post_only_reject` for post-only rejections),
  C `expire`, D `restate`, H / G (or FIX 4.2 ExecTransType) `fill_bust` / `fill_correct`, 3 `done`; 35=9 ->
  `cancel_reject` / `amend_reject`; 35=j and 35=3 rejects of your requests -> `reject` / `cancel_reject` /
  `amend_reject`.
- **Fill id:** ExecID (17). LastQty 32, LastPx 31, CumQty 14, LeavesQty 151, Commission 12 + 479. A CumQty jump
  across an unrecovered gap becomes a `derived` fill.
- **Exchange time:** TransactTime (60): `match_time` on fills, `gateway_time` otherwise.
- **Maker/taker:** LastLiquidityInd (851) 1 maker, 2 taker, else AggressorIndicator (1057), else a data
  dictionary's custom field.
- **Options:** `--fix-venue-name` (the exchange; default `fix44`), `--fix-session-venue SESSION=VENUE`,
  `--fix-data-dictionary FILE` (custom tags and enums), `--fix-instrument-tag N`, `--fix-delimiter`,
  `--fix-local-ts-regex`, `--fix-checksum`; in Python `queuetruth.live.import_quickfix(...)` or the `fix` field of
  the import spec.
- **Quirks:** order identity follows the ClOrdID chain (11 / 41) within a session; a replace answered with a new
  OrderID stays linked to the order, a replace answered as a new order gets `replaces_order_key`. Duplicates across
  an order-entry and a drop-copy session are dropped by ExecID. `amend_keeps_priority` is null except on `coinbase`
  (FIX is silent; the venue's rule applies when the schedule is built).

### Simulator output (hftbacktest, Nautilus)

`queuetruth.live.import_sim(kind, inputs, out, account=None, instrument_map=None, venue=None, append=False,
**options)` with `kind` = `hftbacktest` or `nautilus` writes a canonical log of your simulator's orders for the
scorer's external arm (`queuetruth score --sim-log DIR`); on the command line it is `queuetruth live import --venue
sim-hftbacktest|sim-nautilus` with options as `--option KEY=VALUE`. The log has `venue = sim:hftbacktest` or
`sim:nautilus`, `source = sim` and `validation_status = simulated`. `queuetruth export replay-schedule` writes the
replay program whose output these importers read. A simulator order is linked to the live order it replays through its client order
id: an id of the form `<venue>:<account>:<id>` becomes the `order_key` (a `.leg<k>` suffix is stripped). `venue`
names the live venue whose instrument-map rows apply.

- **hftbacktest:** snapshots of hftbacktest order objects written by your loop (JSON lines or CSV, one row per order
  per snapshot, ns timestamps): `order_id`, `side` (1 / -1 or buy / sell), `price_tick`, `tick_size`, `qty`,
  `leaves_qty`, `exec_qty`, `exec_price_tick`, `status`, `exch_timestamp`, `local_timestamp`, `maker`,
  `order_type`, `time_in_force`, optional `order_key` and `instrument` / `asset_no`. Fills are derived from
  `leaves_qty` decreases (`derived = true`). Option `order_key_map` maps `order_id` to an `order_key`.
- **nautilus:** order report, fill report and order event rows; the columns are listed in
  [Score my Nautilus backtest](score-nautilus-vs-binance.md#5-import-the-backtest-output).

Tests: `test_hftbacktest_snapshots_jsonl_and_csv`, `test_nautilus_reports_and_events`
(`python/tests/live/test_import_api.py`).

## Validate

`queuetruth live validate --live DIR` prints a `qt.live_validation` document (`path`, `n_orders`, `n_events`,
`errors`, `warnings`) and exits 2 when there is any error. The import runs the same checks.

Errors (code `QT-INPUT-003` when any of them is a duplicate, else `QT-INPUT-002`):

- table columns or types differ from the schema;
- invalid or duplicate `order_key`;
- events for an order missing from `orders.parquet`;
- `event_seq` not contiguous from 1;
- duplicate `fill_id` per (venue, instrument);
- `cum_qty` different from the running net fill sum;
- a bust or correction that references no fill of the order;
- an event with no timestamp at all;
- `ts_exchange_kind` or `ts_precision` not set.

Warnings (exit 0):

- `cum_qty + leaves_qty` different from the order's current total after a fill (the total follows `amend_ack` and
  `restate` quantities, so legitimate amends do not warn);
- a `taker` fill on a `post_only` order.

The repository has one canonical fixture per case (`python/tests/live/fixtures/canonical/`, checked by
`test_o_live_1_validator_negative_cases`). A duplicate fill id:

<!-- check-docs: run exit=2 -->
```sh
queuetruth live validate --live "$QT_SRC/python/tests/live/fixtures/canonical/neg_duplicate_fill_id"
```

prints `QT-INPUT-003: ... duplicate fill_id F-5-1 ...` on stderr. A post-only order with a taker fill only warns:

<!-- check-docs: run -->
```sh
queuetruth live validate --live "$QT_SRC/python/tests/live/fixtures/canonical/warn_post_only_taker_fill"
```

From Python, `qt.validate_live(path)` returns the warnings and raises `QtError` on errors;
`queuetruth.live.check(path)` returns the `qt.live_validation` document without raising.

## Fills-only logs

A log built only from fill lists (IBKR Flex, OKX or Binance REST fills without order history, any export of trades
alone) has no never-filled orders. The import warns `coverage=fills_only for N of M order(s)`, and
`queuetruth.live.coverage(dir)` returns `{n_orders, fills_only_orders, fills_only_share, fills_only,
fills_only_keys}`. The scorer detects such a log by its events (every event is a fill, bust or correction) and
refuses the statistics that need unfilled orders: fill-rate bias, McNemar, the strata tests, equivalence and the CIF
tests are null and the headline says `not computable: fills-only source`; markouts and the time to fill conditional
on fill are still computed (`test_fills_only_source`, `test_fills_only_not_computable_but_ttf_and_markouts_computed`).
To score fill rates, import the order history or the order stream as well.

## Writing a canonical log from your own OMS or simulator

If no importer fits, write the rows yourself. `queuetruth.live.write_canonical_log(out, orders, events)` takes
lists of dicts with the canonical column names (or canonical tables), validates them (errors raise `QtError`) and
writes the directory. Omitted nullable columns are null, `post_only` defaults to false, `validation_status` to
`fixture_only`, `derived` to false, and events without `event_seq` are numbered in the order given.
`queuetruth.live.build_log(orders, events)` builds the two tables without validating or writing.

<!-- check-docs: run -->
```python
from queuetruth import live

S = 1_000_000_000
t0 = 1_781_530_000 * S
orders = [{
    "order_key": "myoms:acct1:42", "venue": "myoms", "account": "acct1", "instrument": "BTCUSDT",
    "instrument_id": 1, "venue_order_id": "42", "side": "buy", "order_type": "limit", "tif": "gtc",
    "orig_qty": 50_000_000, "qty_str": "0.5", "limit_px": 60_000_000_000_000, "px_str": "60000",
    "price_exp": -9, "size_exp": -8,
}]
common = {"order_key": "myoms:acct1:42", "ts_precision": "ns", "source": "csv"}
events = [
    {**common, "event_type": "submit_sent", "ts_local_send": t0, "ts_exchange_kind": "unknown"},
    {**common, "event_type": "ack", "ts_exchange": t0 + 2_000_000, "ts_exchange_kind": "match_time"},
    {**common, "event_type": "fill", "ts_exchange": t0 + 5 * S, "ts_exchange_kind": "match_time",
     "qty": 20_000_000, "px": 60_000_000_000_000, "fill_id": "T1", "liquidity": "maker", "cum_qty": 20_000_000},
    {**common, "event_type": "cancel_sent", "ts_local_send": t0 + 9 * S, "ts_exchange_kind": "unknown"},
    {**common, "event_type": "cancel_ack", "ts_exchange": t0 + 9 * S + 2_000_000, "ts_exchange_kind": "match_time"},
]
files = live.write_canonical_log("my-live", orders, events)
print([f.name for f in files])
```

<!-- check-docs: run -->
```sh
queuetruth live validate --live my-live
```

## Privacy

The `raw` column of both tables keeps the original message text of every record (the JSON line, CSV row or FIX
message) so that you can audit the conversion. It can contain account ids, client order ids and anything else the
venue sent. Strip it before you share a log:

<!-- check-docs: run -->
```python
import pyarrow as pa
import pyarrow.parquet as pq

for name in ("orders", "events"):
    t = pq.read_table(f"bn-live/{name}.parquet")
    i = t.schema.get_field_index("raw")
    t = t.set_column(i, t.schema.field(i), pa.nulls(t.num_rows, t.schema.field(i).type))
    pq.write_table(t, f"bn-live/{name}.parquet")
```

The schema and its metadata stay the same, so the log still validates:

<!-- check-docs: run -->
```sh
queuetruth live validate --live bn-live
```

Live logs never leave your machine unless you upload them to a queuetruth service yourself.
