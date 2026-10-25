# Extension: prediction-market kit (`queuetruth pm`)

Design contract of the Kalshi and Polymarket kit: readers for the user's own captures, an L2 book per market,
queue-position models calibrated against the queue truth the user's own orders reveal, and a reward-aware
market-making scorer. User guide: [Score market making on Kalshi or Polymarket from my own
captures](../../how-to/prediction-market-making.md). Sections of the main [specification](../spec.md) are cited
as SPEC n.n.

## 1. Scope and terms

- Inputs are files the user captured or exported under their own venue account: WebSocket frames, REST responses,
  market metadata and (Polymarket) a Polygon log export. The kit MUST NOT open network connections, ship venue data
  or offer any upload or publishing path. Every summary document carries `terms` (`queuetruth.pm.terms.TERMS_NOTICE`:
  self-hosted use on the user's own captures; Kalshi Rule 11.1 and the Polymarket and Polymarket US terms bar
  redistributing venue data and derived outputs). The sources of the terms statements are listed with the date read
  in `queuetruth.pm.terms.SOURCES`.
- Example data is invented: the reader tests build their messages by hand (`python/tests/pm/_helpers.py`, field
  for field from the documented schemas and examples), and the walkthrough captures are written by
  `queuetruth.pm.fixtures` (`queuetruth pm fixtures`) from an exact simulated FIFO book in the same formats. A
  generation MUST equal `tests/fixtures/pm/` byte for byte (`MANIFEST.sha256` lists the files); longer simulated
  captures are written to temporary directories only.

## 2. Command line

`queuetruth pm fixtures | ingest | calibrate | score` is a Python-only command group (`queuetruth.pm.cli`), routed
by `queuetruth.cli.main` before the Rust-parsed command table when the first command word is `pm`. It honours the
global flags `--output-format` / `--format` (`human` | `json`), `--quiet`, `--verbose`, `--log-level` and `--seed`
before or after `pm`, prints exactly one JSON document with `--format json` (the summary, or `qt.error` on failure)
and uses exit code 0 on success and 2 on any error. As in the main CLI, logging prints `LEVEL message` on stderr,
Python warnings raised during a run print as `warning: ...` after the answer, and an interrupt is `QT-USAGE-001`
(`interrupted`); `--threads` is refused (`QT-USAGE-001`: the kit runs in one thread). Durations need a unit (`ns`,
`us`, `ms`, `s`, `min`, `h`). Usage errors are `QT-USAGE-001`; an unexpected exception is `QT-ENGINE-001` with the
exception named.

| Command | Spec model (`queuetruth.pm.models`) | Writes |
|---|---|---|
| `pm fixtures --out DIR` | (none) | the example captures; prints `qt.pm_fixtures` (refuses a non-empty `DIR` unless it holds an earlier generation) |
| `pm ingest --venue kalshi\|polymarket --in PATH... [--chain PATH...] --out DIR` | `PmIngestSpec` | the ingest directory (section 4), `pm_ingest.json` (`qt.pm_ingest_summary`) |
| `pm calibrate --pm DIR --out DIR [--loss rmse\|mae] [--no-atomic] [--families F,...] [--grid LO:HI:N]` | `PmCalibrationSpec` | `pm_calibration.json` (`qt.pm_calibration`) |
| `pm score --pm DIR --out DIR [--quotes PATH] [--calibration PATH \| --model ID]` | `PmScoreSpec` | `pm_score.json` (`qt.pm_score`), `pm_orders.parquet`, `pm_fills.parquet` |

Specs validate with `QT-CONFIG-001` naming the first bad field (`validate_spec`); money fields are decimal text
(`Dollars`). Documents follow SPEC 3.10 (`schema`, `schema_version: 1`, no NaN) and carry `run_id` (SPEC 7.3: the
spec without `out`, inputs by content): input files by sha256, a `pm ingest` directory by its `run_id` and a `pm
calibrate` output by its `run_id` (`queuetruth.pm.workdir.resolve_ref`), so the same capture in two places gives
the same `run_id` at every step. JSON Schemas of the documents and specs, and dumps of the kit's Arrow tables, are
committed in `python/queuetruth/pm/schemas/` (`queuetruth.pm.schemas.export()`); they are not registered in
`queuetruth.schemas.DOC_MODELS` or the Rust core.

## 3. Units and the canonical ladder

Prices are YES-leg dollars at `price_exp = -4` (`$1.00 = 10_000`). Kalshi sizes are contracts at `size_exp = -2`,
Polymarket sizes shares at `size_exp = -6`. Venue decimals convert exactly or fail with `QT-INPUT-004`; binary
floats are refused (`QT-INPUT-002`). Every market is one instrument on the YES ladder: bids `B` = buying YES, asks
`A` = selling YES. A Kalshi NO bid at NO price `q` is a YES ask at `1 - q` (no-leg pricing, the documented default)
or at `q` when the captured subscription set `use_yes_price`. A Polymarket NO-token bid at `q` is a YES ask at `1 - q`
and a NO ask a YES bid; one token's book is the whole book (the YES token's book is used when captured).

## 4. Ingest

### 4.1 Output directory

`--out` MUST not exist or be empty (`QT-CONFIG-001`, as `queuetruth ingest`), and readers of the directory
(`queuetruth.pm.workdir.load`) read only the files `pm_ingest.json` lists (`book_own_free/` and `live/` only when
the summary says they were written), so no file of another run is ever read.

| Path | Content |
|---|---|
| `book/` | L2-only dataset of the published book (SPEC 3.3, 3.4 `qt.l2`; `kind: l2`, venue `generic`, `source_venue` kalshi or polymarket, `validation_status: assumed`, `source_format` `pm_kalshi` or `pm_polymarket`) |
| `book_own_free/` | the same with the user's own resting size removed (only with own orders) |
| `live/` | canonical live log of the user's orders and fills (SPEC 3.8) |
| `markets.json` | `qt.pm_markets`: grid, fee schedule, programs, settlement, tokens per market |
| `pm/queue_samples.parquet` | `qt.pm_queue_samples`: Kalshi official queue positions with local send and receive times |
| `pm/own_entries.parquet` | `qt.pm_own_entries`: where each own order (leg) joined its level and left it |
| `pm/chain_fills.parquet` | `qt.pm_chain_fills`: decoded `OrderFilled` events (Polymarket `--chain`) |
| `pm/filled_orders.parquet` | `qt.pm_filled_orders`: every order hash that received a maker fill |
| `pm/chain_queue.parquet` | `qt.pm_chain_queue`: queue ahead of each own order at its first on-chain fill |

### 4.2 Batches and rows

Book messages become batches (`queuetruth.pm.batches`): Kalshi deltas sharing an exchange `clock` form one batch;
every other message is a batch of its own. The first snapshot, and the first after a gap, resync the book (`clear`
+ `snapshot` rows); later Kalshi snapshots are restatements and every differing level is counted
(`snapshot_mismatch_levels`). A gap marker (a Kalshi `seq` gap, a sequence regression or a resubscription, section
4.4) opens a feed gap until the next snapshot (deltas in it are dropped and counted; the gap and its reason are
listed in `data_quality.json` and stop orders resting through it). A book returned out of band (a Kalshi REST
orderbook of a market the WebSocket streams) is a check: it passes when the book equals it at the request's send
time or after any batch up to its receive time, else it counts in `rest_book_mismatches` and
`rest_book_mismatch_levels` (data-quality anomalies). A print is attached to the batch, within `match_window_ns`
(1 s Kalshi, 2 s Polymarket), that decreased the hit level by at least its unexplained size (exact first, then
nearest in time, then earliest), else to a batch that decreased the level at all, else it becomes a `trade` batch
of its own; a decrease the venue attributed to the user (`client_order_id`) explains the user's own taker prints
first and other prints only when nothing else can, so a cancel and a sweep of one size in one millisecond keep their
own prints. Rows follow the Tardis conventions of SPEC 3.4 (trade rows first, `LAST_IN_BATCH` on the last row, level
= rank after the batch, 255 when emptied). Partitions are UTC dates; each partition after an instrument's first
starts with a restatement of the book. The data-quality verdict is the rule of SPEC 5.1.5; reader anomalies that
change the book (`duplicate_subscription_messages`) join its source-specific anomalies.

### 4.3 Own orders and queue entry points

Own orders come from the live log (`queuetruth.score.l2_own.OwnOrder`). Each change of an order's resting size is
placed inside one published batch (`queuetruth.pm.ownfree`): `exact` when a Kalshi delta carried the order's
`client_order_id` with the same level and size; `aligned` when a batch within the own-match window changed the level
in the same direction by at least the unexplained change; `unaligned` before the next batch touching the level;
`restated` for an order resting before the capture. `own_entries` has a row per stretch of an order at one price:
the first, and one per size increase at the same price (an amend up, which loses priority: `<order key>#up<k>`,
a re-entry at the back of the queue with the new size), each with where it joined (`seq`, `inside`) and where it
left (`end_seq`, `end_inside`: the change that took its resting size to 0 or the next increase). The own-free series
replays the published batches with the own resting size removed after each batch (negative results clamp to 0 and
are counted); a batch that only moved own orders is dropped, its prints kept.

### 4.4 Readers

Inputs are read in time order (`queuetruth.pm.capture.order_inputs`: by each file's first local time; files without
local times follow in the given order).

- Kalshi (`queuetruth.pm.kalshi`): WebSocket `orderbook_snapshot`, `orderbook_delta`, `trade`, `fill`,
  `user_order`, `market_lifecycle_v2` (settlement, `price_ranges`); REST fills, orders, orderbook, trades, queue
  positions (single and list), market (`price_ranges`, fee waiver, settlement), series (`fee_type`,
  `fee_multiplier`) and incentive programs.
  The orderbook frames of every input are merged by receive time (each file in its own order) and `seq` is checked
  per subscription: a gap gaps the subscription's markets; a repeat of an earlier `seq` with the same content is
  dropped (`duplicate_book_frames`); any other step back (`sequence_regressions`), a new `subscribed` reply for a
  known `sid`, or a snapshot that restarts the sequence is a new session (`orderbook_resubscriptions`) and gaps the
  subscription's markets until their next snapshot. A market is read from one subscription at a time: its snapshot
  on another `sid` moves it there (also a gap), and messages from the subscription it left are dropped and counted
  (`duplicate_subscription_messages`, a data-quality anomaly, warned about). Prints are deduplicated by `trade_id`
  across the WebSocket and REST (`duplicate_trades`; the WebSocket copy is kept; a conflicting duplicate is
  `QT-INPUT-002`), and the prints of the user's own taker fills are marked. A REST orderbook of a market with a
  WebSocket book is a check (section 4.2); for any other market the responses, in receive-time order, are its book.
  A queue position without a local receive time is `QT-INPUT-002`. Fills derived from `user_order` fill counts are
  dropped where the venue's own `fill` messages exist (counted as `derived_fills_superseded`); their fee is the
  growth of the order's reported `maker_fees_dollars` + `taker_fees_dollars`.
- Polymarket (`queuetruth.pm.polymarket`): market channel `book`, `price_change`, `last_trade_price`,
  `tick_size_change`, `market_resolved`, `new_market`; user channel `order` and `trade` (first status is the fill,
  `FAILED` busts it); the SDK's camelCase `{topic, type, payload}` events; Gamma market objects (`clobTokenIds` or
  `positionIds` index 0 is YES; `feeSchedule`; reward settings). Two tokens of a market without a YES/NO mapping are
  `QT-INPUT-002`. The prints of the book token (else the YES token) are kept as they are; a print on the other token
  is dropped as the mirror of one of them, one for one, when an unmatched one has the same transaction hash, price,
  size and side (`duplicate_prints`), kept otherwise (`complement_prints_kept`). The docs give
  `last_trade_price.side` only as BUY or SELL and do not say whether `price_change.size` is a level's new size; the
  reader takes the side as the taker's and the size as absolute. A print that no decrease of the side it hits
  explains but a decrease of the other side at its price would is counted (`trades_other_side_explained`, a data
  quality anomaly; ingest warns when they are at least 5 and a fifth of an instrument's prints), and each message's
  best bid / ask is compared with the rebuilt book (`bba_mismatch`).
- Chain (`queuetruth.pm.chain`): `eth_getLogs` JSON (bare arrays, JSON-RPC envelopes or JSON lines) or CSV exports.
  Topics are Keccak-256 of the public event signatures of CTF Exchange v1 and v2 (`queuetruth.pm.keccak`). Within a
  transaction, maker `OrderFilled` events are grouped with the following taker `OrderFilled` and `OrdersMatched`
  (`match_idx`, `maker_pos` = operator order); an `OrderFilled` without `OrdersMatched` is an `operator_fill`.
  Removed (reorged) logs are dropped, identical duplicates collapsed, conflicting duplicates refused. Known
  contracts: the CTF Exchange, Neg Risk CTF Exchange and Protocol V2 Exchange of docs.polymarket.com/resources/contracts
  and the two v1 exchanges that settled the CLOB before CLOB V2 (2026-04-28); events of other contracts are decoded
  and the contracts named in a warning.

### 4.5 On-chain queue truth

Polymarket's documentation does not state its matching priority; the kit assumes price-time and reads the operator's
fill order as queue order. For each own order with a maker fill on chain: the published batch holding the print of
its first fill's transaction at its level is located; every maker fill at that level in that batch's transactions
that precedes the order's own fill (block, log index) was ahead of it at the batch start. `ahead_others` and
`ahead_own` split that size by owner. Under the assumption this is the order's queue ahead at that batch, including
when the fill completed the order; `fill_truth.bound_violations` and an own order filled while an earlier own order
at its price still rested are what would show the assumption failing. The same export checks the print sides: every
print carrying a transaction hash of the export must name an aggressor that one of that transaction's taker orders
had (`OrdersMatched`: a BUY of YES or a SELL of NO buys YES); `chain.prints_side_checked` and
`chain.prints_side_mismatch` count them, and any mismatch is warned about.

## 5. Queue engine

`queuetruth.pm.queue.Engine` replays one instrument's `qt.l2` rows and moves every live order's queue estimate under
several models at once, with the dispatch of the zoo's corrected mode (SPEC 5.3.2, 5.3.3): activation at the first
batch with `ts_event > ts_live` (or the first batch after a known entry point), `F` = visible size before that batch;
prints at the price consume `F` first; trade-through fills the leaves; the hftbacktest probability families update
`F` on depth changes; a crossed book fills as a taker on arrival (capped at the visible opposite size, priced at its
volume-weighted price; post-only orders are rejected) and as a maker at batch end; `clear` and feed gaps censor. An
order stops at the first batch after its end time, or, when its end was placed in the published series
(`end_seq`, `end_inside`), at the first batch after that position, so a print in a later batch with the cancel's
millisecond never fills it. Families: `front_of_queue` and `trade_through_only` (the fill bounds), `risk_adverse`
and `cancels_ahead` (the queue bounds), `power[n]`, `power2[n]`, `power3[n]`, `log`, `log2`. On the same dataset and
orders the engine MUST give the same fills as `queuetruth zoo --mode corrected`, given the models in the zoo's
spelling (`Model.zoo_id`: `hbt_power3[exch=partial;mode=corrected;n=2]`; tested against the compiled core).
`Model.parse` takes both spellings and refuses another `mode` or `exch`.

The probability families also come in an `atomic` variant (`power3[n=1;atomic]`, `log[atomic]`; not the bounds,
and not in the Rust zoo). It reads a decrease beyond the prints (`chg = prev - new - T`) as one order leaving, the
reading of a feed that publishes each exchange action on its own: with `front` the queue ahead and `back = prev -
front`, the order can have been ahead only if `chg <= front` and behind only if `chg <= back`. When exactly one
holds, the probability `pi` that the decrease was behind is 0 or 1; otherwise the family's `pi` is used. The variants
are estimates for the calibration to judge, not bounds: a change that nets several orders breaks the reading.

## 6. Calibration

Truth, in priority order: (1) Kalshi official queue positions, each placed on the book's clock at its local receive
time or the midpoint of send and receive (`sample_time`), compared with the model's `F` plus the user's own size
resting ahead at the same price; (2) the queue ahead at each own order's first filling batch from the chain
(Polymarket, `n_chain`; under the priority assumption of section 4.5); (3) from the book alone: the batch's
at-price prints minus the order's fills in that batch minus own size ahead, when those fills did not complete the
order. Each own fill belongs to the batch of the nearest at-price print within the own-match window that still has
print volume for it (the L2 rows carry no trade ids), so a later taker's fill never counts in the first batch.
Truths (2) and (3) exist only for orders that filled, and an order fills when more of the queue ahead than usual
left: they lean toward short queues, a fit on them (`fit_on = fill_truth`) is flagged (`fill_truth_selection`) and
warned about, and the warning names `queuetruth calibrate-live --data <dir>/book --live <dir>/live`, which fits on
every order. Every model is also checked on the test orders that never filled live (`unfilled_filled`, of
`n_unfilled_test_orders`: those the replay filled while they rested).

Families with a parameter run on a grid of `ln n` (`n` rounded to 6 significant digits), each also in its `atomic`
variant unless `atomic` is false. Own orders are sorted by (first entry time, order key) and split by
`train_fraction`; `test_from` records the first test order, and `pm_run_id` the ingest. The loss (`loss`, default
`rmse`) is order-weighted: each order's errors are reduced first (mean squared error, or mean absolute error for
`mae`), then averaged over orders (`rmse` takes the root of that mean). The fitted model has the smallest train loss
(ties: model id); its test loss carries a percentile bootstrap interval over test orders (resampling orders);
`best_beats_bounds_midpoint` compares it with the midpoint of the two queue bounds. Every model also reports the
order-weighted test MAE and RMSE, the mean signed error (bias) and median absolute error of the test samples, the
MAE and bias at the first filling batch, and its zoo spelling (`zoo_id`, None for `cancels_ahead` and the atomic
variants). RMSE is the default because a decrease is either ahead or behind: MAE is minimised by the median, which
commits ambiguous decreases to one side and biases the queue (measured below). `bounds_coverage` is the share of
official positions inside the queue bounds over the sample's send-to-receive window; `fill_truth.bound_violations`
counts orders whose true queue fell outside them.

`trust` follows `calibrate-live`'s (`verdict`, `reasons`, `flags`): `refused` for any of `no_estimate` (nothing
fitted), `few_orders` (fewer than 30 orders with truth in the train or the test half, `n_train_orders_with_truth`,
`n_test_orders_with_truth`), `no_held_out`, `at_grid_edge`, `best_is_bound` (a queue bound is the best fit) and
`not_beating_midpoint`, in that order; else `usable`. Each reason is also a `trust:` warning line, ending with
`trust: verdict: ...`; the terminal view shows the bounds and their midpoint whatever their rank, and the verdict.

Measured on captures written by the simulator (exact FIFO books in which cancels take uniformly random resting
orders, one per action; `python/tests/pm/test_generated_captures.py`): with 8,000 exchange events (358 own orders,
1,487 official positions) coverage is 100%, the fitted `power[n=0.22313;atomic]` has a test RMSE of 8.35 contracts
against 8.80 for the bounds midpoint and 12.48 / 14.63 for the bounds, the verdict is `usable`, and of 64 unfilled
test orders `risk_adverse` fills 0, the fit 3 and `cancels_ahead` 5; with 50,000 events (2,276 orders, an
integration test) the best atomic variant beats the best plain family (test RMSE 7.67 against 8.25, MAE 2.76
against 3.39), the MAE ranking picks `power2[n=54.5982;atomic]`, 3.38 contracts short on the test orders and worse
than the RMSE pick even by test MAE (2.87 against 2.76), and the RMSE pick sits at the grid's lower end
(`at_grid_edge`). One order per cancel is what the atomic reading assumes, so their gain there is partly by
construction.

## 7. Scoring

Orders (live legs from their exact entry points, or hypothetical quotes by time) are replayed on the own-free book
under `pessimistic` (`trade_through_only`), `model` and `optimistic` (`front_of_queue`). A live leg rests in the
replay as long as it rested live: to its placed end (cancel, expiry, re-price or increase); to the end of the data
when it was still resting when the capture ended; to the later of its last fill and `window_ns` after entry when it
filled completely (no cancel instruction to replay). Each leg is a shadow of its own (the user's other orders ahead
at its price are not in its queue; a warning counts the legs that had some). Per scenario, with `m` the mid before a
fill, `M` the mark (settlement, an override, or the last mid with a warning), `s = +1` for a buy: `spread_capture =
sum s (m - p) q`, `adverse_selection = sum s (m - M) q`, `trading = spread_capture - adverse_selection`, `total =
trading - fees + rewards + rebates`. `band` is `[min, model, max]` over the scenarios (a range, not a bound);
`trading_bound` and `net_trading_bound` sum each order's minimum and maximum over the two bounds and bound the
trading PnL of the true fills. The live arm scores the live fills (their reported fees) and the live resting size on
the published book over each leg's real life. `model_source` says where the model came from (`calibration`, `given`,
`default`) and `calibration_trust` repeats the calibration's verdict; a `refused` one is scored with a warning and
named in the terminal headline. Sim versus live: per model, the 2x2 table of first fills within `window_ns` of entry
(at most to the live end), fill-rate bias with a bootstrap interval, the exact McNemar p-value, the median log ratio
of times to first fill and bracket violations; when the model was calibrated on the same ingest (`pm_run_id`), its
comparison is repeated on the calibration's `train` and `test` orders (`SimVsLive.orders`). Markouts per scenario
and horizon use the mid at `t + h`. The terminal headline gives the model's total with the scenario range, then the
trading bound, and flags a live total outside the scenario range.

Fees and rewards (`queuetruth.pm.fees`, `queuetruth.pm.rewards`, sources and dates in their docstrings):

- Kalshi: `0.07 x fee_multiplier x share x C x P x (1 - P)` with the maker share of the series' `fee_type`, fee
  rounding per fill with the per-order accumulator and the member's balance precision; `flat` series need
  `flat_taker` and `flat_maker`; fills before a fee waiver are free.
- Kalshi LIP: per snapshot and side, reference price at a fifth of the target size, scores `DF^ticks x size`
  normalized per side, both sides needed; expected payout `reward x integral of the snapshot score / (2 x snapshots
  in the period)`.
- Polymarket: taker fee `C x rate x p x (1 - p)` rounded down to 5 decimals, makers free; maker rebate share of the
  rebate pool by fee equivalent; liquidity rewards `S(v, s) = ((v - s) / v)^2 x b`, `Q_min` with `c = 3` inside
  [0.10, 0.90], 1,440 samples a day, with the other makers' summed `Q_min` bounded over every split of their size:
  `[max(Q_long / c, (Q1 + Q2) / (c + 1)), max(Q_short + (Q_long - Q_short) / c, (Q1 + Q2) / c)]` in range, both
  ends reached for every `c >= 1`.

## 8. Performance

The kit is pure Python (NumPy for the calibration's error tables) and linear in the capture: per-level indexes for
own orders, trades and fills, and reward integrals that hold only the orders resting at each moment. On an 8-core
laptop in low-power mode (2026-10-24), a simulated 130-minute Kalshi capture (50,000 exchange events, 89,568
capture lines, 62,450 book rows, 2,276 own orders, 10,412 official positions) took 3.0 s of CPU time to ingest,
3.6 s to calibrate and 4.5 s to score (3 to 5 s wall each); 5,000 events took 0.3 to 0.7 s per command.

## 9. Error codes

`QT-USAGE-001` (flags, `--threads`, an interrupt), `QT-CONFIG-001` (spec values, fee schedules, grids, programs, a
non-empty `--out`), `QT-INPUT-001` (missing files), `QT-INPUT-002` (malformed captures, logs or quotes, with the file
and line; conflicting duplicate trades; a calibration that fitted no model), `QT-INPUT-003` (duplicate `quote_id`),
`QT-INPUT-004` (inexact decimals), `QT-ENGINE-001` (invariants and unexpected errors).

## 10. Not covered

Kalshi's flat fee table and per-account LIP caps are not machine readable (passed by the user); the direction of
Polymarket's fee rounding and the size cutoff of its adjusted midpoint are not documented (the readings above are
used and stated); the maker shares 0.25 / 0.5 rest on the get-series description (the current fee schedule PDF
could not be fetched); Polymarket's matching priority is assumed (section 4.5); the main CLI's `queuetruth --help`
does not list `pm` (the Rust command table is unchanged). Not modelled: Polymarket's 250 ms taker delay and sports
delays (they matter for quotes that cross, which the scorer rejects as post-only by default), Kalshi RFQ executions
beyond their prints, Polymarket US (aggregated depth over gRPC), interaction between the user's own orders at one
price in the score's replay, and Polymarket reconnects (the market channel has no sequence numbers; a fresh `book`
after a reconnect is read as a restatement, without a feed gap). Neither venue's data could be obtained for this work (an account, or a written
agreement for a fintech's use of Polymarket data, would be needed), so the readers are tested on hand-written and
simulated messages in the documented formats, not on real captures.
