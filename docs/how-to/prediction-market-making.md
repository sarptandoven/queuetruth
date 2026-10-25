# Score market making on Kalshi or Polymarket from my own captures

**Goal:** turn your own Kalshi or Polymarket captures into a replayable order book, check queue-position models
against the queue truth your own orders reveal, and get the PnL of your quoting as spread capture minus adverse
selection minus fees plus maker rewards, with the uncertainty about which orders would have filled shown as a band.

**You need:** `pip install queuetruth` (the `pm` commands are part of the Python CLI `queuetruth`, not of the
Rust-only `queuetruth-core`), and your own captures of one or more markets while you quoted them: the WebSocket
messages, a few REST responses, and for Polymarket optionally a Polygon log export of the exchange contract. To try
it first, `queuetruth pm fixtures` writes invented example captures in the same formats; every block below runs as
written on them, in an empty directory.

## The terms, plainly

This is not legal advice; read the documents yourself. What they say, as of 2026-10-22 to 2026-10-24
(`queuetruth.pm.terms` lists the sources):

- **Kalshi.** The rulebook (v1.29, Rule 11.1) lets members use market data for their own internal business purposes
  and bars distributing, selling or retransmitting it. The Developer Agreement limits API use to your own trading
  and bars use "for any other benchmarking or competitive purposes". Running this kit on your own captures for your
  own trading fits; sending captures, books or scores to anyone else, hosting them or publishing them needs Kalshi's
  written permission.
- **Polymarket.** The Terms of Use (effective 2026-08-11) bar market makers, proprietary trading firms, financial
  technology companies and market-data distributors ("Capital Market Clients") from accessing Polymarket data,
  on-chain data included, without a written agreement. If you are such a firm, get that agreement before you
  capture anything. Polymarket US (Rulebook Rule 5.3(g)) bars redistributing information received through its
  trading system.
- **This kit** reads only files you pass it, makes no network calls, ships no venue data and has no upload or
  publishing path. Every output document repeats a one-line notice (`terms`). Keep captures and outputs to yourself.
  The example captures come from a simulator (`queuetruth.pm.fixtures`) that writes the documented message formats
  with invented markets, accounts and values.

## What to capture

Write every received frame as one JSON line wrapped with your local receive time, `{"recv_ns": <int>, "msg":
<frame>}`, and every request you send as `{"dir": "out", "local_ns": <int>, "msg": {"method": ..., "path": ...}}`
followed by its response `{"dir": "in", "local_ns": <int>, "msg": <response>}`. These are the capture wrappers of
[Import fills from my venue or broker](import-live-logs.md#rules-shared-by-every-importer). Files may be gzipped and
may be passed in any order: they are read in time order (each file by its first receive time), so a capture
rotated into several files needs no sorting. A bare frame without a wrapper is read too, but then the kit has only
the venue's own timestamps.

### Kalshi

| What | From | Why |
|---|---|---|
| `orderbook_snapshot`, `orderbook_delta` | WebSocket `orderbook_delta` channel, and the `subscribe` command you sent with its `subscribed` answer | The book. Deltas with one `clock` are one exchange action. `client_order_id` on a delta marks your own change. The command's `use_yes_price` tells how NO levels are priced (no-leg by default: a NO bid at 0.56 is a YES ask at 0.44). |
| `trade` | `trade` channel | Prints (taker side, block trades) |
| `fill`, `user_order` | `fill` and `user_orders` channels | Your orders and fills; becomes the canonical live log |
| `market_lifecycle_v2` | `market_lifecycle_v2` channel | The settlement value, and the price grid (`price_ranges`) |
| Queue positions | `GET /portfolio/orders/{order_id}/queue_position` or `GET /portfolio/orders/queue_positions`, wrapped with send and receive times | Kalshi's official queue position, the calibration target |
| Market, series, incentive programs | `GET /markets/{ticker}`, `GET /series/{series_ticker}`, `GET /incentive_programs` | Price grid, `fee_type` and `fee_multiplier`, fee waiver, liquidity incentive programs |
| Fills and trades exports | `GET /portfolio/fills`, `GET /markets/trades` | What you missed on the socket (deduplicated by trade id; a print in both is counted once) |

Poll the queue position of each resting order now and then (the example polls about once a second). Each answer
needs its local receive time: without it the sample cannot be placed on the book's clock and is refused.

Kalshi numbers `seq` per subscription and per connection. Keep the `subscribed` answers in the capture: after a
reconnect the book is gapped from the last message before the outage until the next snapshot, and orders resting
through the gap are cut off (censored), whether the subscription came back with the same `sid` or a new one. A
frame repeated with the same `sid`, `seq` and content is dropped once. A `GET /markets/{ticker}/orderbook` response
for a market the WebSocket streams only checks the streamed book (it must match the book at some moment between
the request and the answer; `rest_book_mismatches` counts those that do not), so it never rewrites it.

### Polymarket

| What | From | Why |
|---|---|---|
| `book`, `price_change`, `last_trade_price`, `tick_size_change`, `market_resolved` | CLOB WebSocket market channel, either token or both | The book (one token's book is the whole book), prints with their transaction hash, the result |
| `order`, `trade` | User channel | Your orders and fills |
| Market metadata | Gamma `GET /markets?condition_ids=...` saved as JSON | Which token is YES (index 0 of `clobTokenIds`), tick size, `feeSchedule`, `rewardsMinSize`, `rewardsMaxSpread`, `clobRewards` |
| Exchange events (optional) | `eth_getLogs` on the exchange contract over your capture's blocks, saved as JSON (or a CSV with `block_number, tx_hash, log_index, address, topic0..topic3, data`) | `OrderFilled` and `OrdersMatched`: which resting orders filled, in what order |

## Walk through it on the example captures

The example captures are one Kalshi market and one Polymarket market, about a minute each, simulated with an exact
price-time queue so their `truth.json` holds what no venue publishes (every order's true queue ahead). The numbers
below are checked by `python/tests/pm/test_howto_numbers.py`; they say nothing about either venue.

<!-- check-docs: run -->
```sh
queuetruth pm fixtures --out pmfix
```

```text
pm fixtures: wrote 11 files under pmfix (invented captures, no venue data; README.txt lists them)
```

### Kalshi: ingest

<!-- check-docs: run -->
```sh
queuetruth pm ingest --venue kalshi --in pmfix/kalshi/ws.jsonl --in pmfix/kalshi/rest.jsonl --out kx
```

```text
pm ingest (kalshi): 1 market(s), book 719 rows, verdict clean
own orders: 21 orders, 16 fills
queue entry points: 26 exact, 16 aligned, 0 unaligned, 0 restated
official queue positions: 76
```

`kx/` now holds:

- `book/`: the book as published, an ordinary queuetruth L2 dataset (`kind: l2`) on the YES price ladder: YES bids
  and YES asks at prices of $0.0001 units, contracts at 2 decimals. `queuetruth zoo`, `dq` and the scorer read it.
- `book_own_free/`: the same book without your own resting size, the book a model of your order must see.
- `live/`: your orders and fills as a canonical live log.
- `pm/own_entries.parquet`: where each of your orders joined its level and where it left it. `exact` entries are
  the batches whose delta carried your `client_order_id`; `aligned` ones are matched by time, level and size. An
  order amended to a larger size loses its priority: it gets a second row (`<order key>#up1`), its re-entry at the
  back of the queue.
- `pm/queue_samples.parquet`: the official queue positions, keyed to your orders.
- `markets.json`, `pm_ingest.json`: what the captures said about each market, and the summary.

`--out` must be a new or empty directory, so a second run never mixes its files with an earlier one's. Check
`book.verdict` first: `warnings` lists the reasons (a sequence gap, a resubscription, deltas dropped until the next
snapshot, prints matched to no book change, messages dropped from a second subscription), `unusable` means the book
crossed itself too often to trust.

### Kalshi: calibrate

<!-- check-docs: run -->
```sh
queuetruth pm calibrate --pm kx --out kx-cal
```

```text
pm calibrate (kalshi, fit on queue_samples): best power[n=7.38906;atomic]
orders 21 (train 10, test 11), samples 76, bounds coverage 100.0%
  model (by train rmse)            train    test    bias  at fills  unfilled  (contracts; at fills: MAE; unfilled: of 3 unfilled test orders)
  power[n=7.38906;atomic]            4.5     5.5    +2.5       0.0       0/3
...
  risk_adverse                       8.5     5.6    +2.6       0.0       0/3
  bounds_midpoint                    9.3     2.8    +1.2       3.8
  cancels_ahead                     16.6     0.8    -0.2       7.5       0/3
best beats the bounds midpoint on the test orders: no
verdict: DO NOT USE (few_orders, not_beating_midpoint); pm score warns when it scores this fit
```

The hftbacktest queue models of the [L2 model zoo](../concepts/l2-model-zoo.md) (`power`, `power2`, `power3` on a
grid of `n`, `log`, `log2`) and the two queue bounds are replayed in the zoo's corrected mode for each of your
orders from the point where it joined the queue to the point where it left it, and its queue estimate (plus your own
size resting ahead at the same price) is compared with each official position. Your orders are split by entry
time: the first half trains, the rest tests. Read it in this order:

1. **Bounds coverage**: the share of official positions between `cancels_ahead` (every cancel at the level was
   ahead of you: the shortest possible queue) and `risk_adverse` (only prints shrink the queue: the longest). On a
   clean feed it is 100%. Less points at missed deltas, a wrong clock or changes the venue netted together.
2. **Train and test error** per family (in contracts), best on train first, with the bias on the test orders
   (negative: the model's queue is too short, so its fills come too early), the error at your fills, and
   `unfilled`: of the test orders that never filled, how many the model's replay filled. The two bounds and their
   midpoint are always listed.
3. **The verdict**, in the form of `calibrate-live`'s: `usable`, or `DO NOT USE` with its reasons (`trust` in
   `pm_calibration.json`, and a `trust:` line per reason in the warnings). A fit is refused when nothing was fitted
   (`no_estimate`), when fewer than 30 orders of the train or the test half have queue truth (`few_orders`), when no
   test order has any (`no_held_out`), when the best `n` sits at an end of the grid (`at_grid_edge`: widen
   `--grid`), when the best fit is one of the two queue bounds (`best_is_bound`), or when it does not beat the
   midpoint of the bounds on the test orders (`not_beating_midpoint`). The example is far too short: 8 train and
   10 test orders have official positions.

Each family is fitted twice. Kalshi publishes every exchange action on its own (the deltas of one action share a
`clock`), so a decrease at your level beyond the prints is one order leaving: it can have been ahead of you only if
it fits in the queue ahead, and behind you only if it fits in the size behind. The `atomic` variants (`[...;atomic]`)
place such a decrease where only one of the two is possible and split it by the family's rule otherwise; the plain
families always split. `--no-atomic` fits the plain families only. Each plain family's fit also names its spelling
in the zoo (`zoo_id`, for example `hbt_power3[exch=partial;mode=corrected;n=2.117]`), which `queuetruth zoo
--models` takes as is; the atomic variants have no zoo equivalent.

The fit minimises the root mean square error, order-weighted: each order's squared errors are averaged first, so a
long-lived order with many samples does not outweigh the others. A cancel is either ahead of you or behind, so an
estimate in between is never exactly right, and the median-seeking mean absolute error (`--loss mae`) rewards
models that put every ambiguous cancel on one side: a queue that is short or long on average, and fills that come
early or late.

How well this works on more data, with the same simulator (`python/tests/pm/test_generated_captures.py`):

- **A 19-minute capture** (8,000 exchange events, 358 of your orders, 1,487 official positions): every official
  position lies between the bounds, and the fitted `power[n=0.22313;atomic]` has a test RMSE of 8.35 contracts
  against 8.80 for the bounds midpoint and 12.48 and 14.63 for the two bounds, with a bias under half a contract
  (the bounds are more than 5 contracts off each way, by design). The verdict is `usable` (166 train and 169 test
  orders with official positions). Of the 64 test orders that never filled, `risk_adverse` fills none, the fit 3
  and `cancels_ahead` 5.
- **A 130-minute capture** (50,000 events, 2,276 orders; an integration test, `pytest -m integration`): the best
  atomic variant beats the best plain family, test RMSE 7.67 against 8.25 contracts and mean absolute error 2.76
  against 3.39. Ranked by MAE instead, the fit picks `power2[n=54.5982;atomic]`, whose queue is 3.38 contracts
  short on the test orders and whose test MAE (2.87) is worse than the RMSE pick's. The RMSE pick,
  `power[n=0.0183156;atomic]`, sits at the lower end of the default grid, so its verdict is `at_grid_edge`.

The simulator's cancels take uniformly random resting orders, one per action, which is what the atomic reading
assumes: on its captures the atomic variants win partly by construction. How real Kalshi traders cancel is what
your own official positions decide.

`fill_truth` is a second check that needs no venue endpoint: the prints of the batch that first filled your order
consumed everything ahead of it first, so when that batch's fills of your order did not complete it, the queue
ahead was exactly the batch's prints at your price minus your fills in that batch (minus your own size ahead).
`bound_violations` counts orders whose true queue fell outside the bounds; on a consistent feed it is 0. It sees
only orders that filled, which leans toward short queues (next section).

### Kalshi: score

<!-- check-docs: run -->
```sh
queuetruth pm score --pm kx --calibration kx-cal --out kx-score
```

```text
pm score (kalshi, live, model power[n=7.38906;atomic], calibration refused): total PnL $13.19 (scenarios $13.19 to $14.52)
trading PnL bound over orders: [$3.80, $25.30], net of fees [$3.11, $24.61]
model: spread capture $3.18, adverse selection -$10.62, fees $0.61, rewards $0.0005, rebates $0.00
live: total $13.19, trading $13.80, fees $0.61
calibration verdict: refused (few_orders, not_beating_midpoint): the model scenario is not a calibrated estimate
...
  sim vs live front_of_queue           fill-rate bias +19.0 pp  (live 0 only, sim 4 only, bracket violations 0)
```

Without `--quotes` your own live orders are scored: each one is replayed from its exact entry point on the
own-free book under three fill scenarios, until the point where it left the queue live (its cancel, expiry,
re-price or size increase; an order still resting when the capture ended rests to the end of the data, and one that
filled completely rests until its last fill or `--window` after entry, whichever is later).

| Scenario | Fill model | Meaning |
|---|---|---|
| `pessimistic` | `trade_through_only` | fills only when a print goes through its price |
| `model` | the calibrated model | the estimate |
| `optimistic` | `front_of_queue` | no queue ahead |

On a FIFO book an order's true fills lie between the two bounds, so they bracket what really happened. The PnL of
each scenario, in dollars, with `m` the mid just before a fill, `M` the mark (the settlement value of YES, else the
last mid, with a warning), `p` the fill price, `q` the quantity and `s` = +1 for a buy:

```text
spread_capture    = sum s (m - p) q
adverse_selection = sum s (m - M) q          (positive: the price moved against your fills)
trading           = spread_capture - adverse_selection = sum s (M - p) q
total             = trading - fees + rewards + rebates
```

The first line gives the model's total and the range over the three scenarios. That range is not a confidence
interval and not a guarantee: filling more can be worse. The bound is the second line, `band.trading_bound`: each
order's trading PnL is linear in its filled quantity, which lies between the two bounds' quantities, so the sum of
each order's smaller and larger value bounds the trading PnL of the true fills. Here the live trading PnL ($13.80)
is inside it ($3.80 to $25.30). `net_trading_bound` does the same net of fees.

The live arm scores what really happened, over each order's real life: your live fills and their fees, and the
rewards your live resting orders earned on the published book. When the model comes from `pm calibrate`, the run
says so and repeats a `refused` verdict (`calibration_trust`; the example's calibration is refused, so read its
model scenario as one estimate between the bounds). `sim_vs_live` compares, per fill model, which of your orders
filled live and in the replay within `--window` of entry (default 60 s): the fill-rate bias with a bootstrap
interval, the exact McNemar test and `bracket_violations` (a live fill earlier than the optimistic bound or later
than the pessimistic one, which a consistent feed never shows). On the example `front_of_queue` fills 4 orders that
never filled live (+19.0 pp), the bias of assuming no queue. When the model was fitted on this same capture, its
comparison is repeated on the calibration's train and test orders (`orders`); read the test row, the orders the
fit never saw.

Each order is replayed as a shadow of its own, as in the zoo: your other orders resting ahead of it at the same
price are not in its queue, so stacking several orders at one price makes the later ones fill early in the replay
(a warning counts them; the calibration does add your size ahead).

<!-- check-docs: run -->
```python
import json

s = json.load(open("kx-score/pm_score.json"))
for name in ("pessimistic", "model", "optimistic"):
    p = s["scenarios"][name]
    print(f"{name:12} fills {p['fills']:3}  trading {p['trading']:8.2f}  fees {p['fees']:6.2f}  total {p['total']:8.2f}")
print("live        ", {k: round(v, 4) for k, v in s["live"].items() if k in ("trading", "fees", "rewards", "total")})
print("LIP program", s["rewards"]["programs"][0]["program_id"], "expected payout by scenario",
      {k: round(v["point"], 6) for k, v in s["rewards"]["by_scenario"].items()})
for row in s["markouts"][:4]:
    print("markout", row["scenario"], row["horizon_ns"] // 10**9, "s:", row["adverse_selection"])
```

Each row of `kx-score/pm_orders.parquet` is one order under one scenario (its fills, end, trading PnL and fees);
`pm_fills.parquet` lists every fill with its reason and the mid before it.

### Polymarket, with the chain

<!-- check-docs: run -->
```sh
P=pmfix/polymarket
queuetruth pm ingest --venue polymarket --in "$P/market.jsonl" --in "$P/user.jsonl" --in "$P/gamma.json" \
    --chain "$P/logs.json" --out px
queuetruth pm calibrate --pm px --out px-cal
queuetruth pm score --pm px --calibration px-cal --out px-score
```

```text
pm ingest (polymarket): 1 market(s), book 498 rows, verdict clean
own orders: 17 orders, 23 fills
queue entry points: 0 exact, 43 aligned, 0 unaligned, 0 restated
chain: 122 maker fills in 65 matches, 77 filled orders
pm calibrate (polymarket, fit on fill_truth): best cancels_ahead
...
verdict: DO NOT USE (few_orders, best_is_bound, not_beating_midpoint); pm score warns when it scores this fit
...
pm score (polymarket, live, model cancels_ahead, calibration refused): total PnL -$17.50 (scenarios -$20.62 to -$17.50)
trading PnL bound over orders: [-$54.25, $14.50], net of fees [-$54.25, $14.50]
...
live: total -$36.19, trading -$37.09, fees $0.00
  the live total is outside the scenario range (the scenarios replay fills and bound nothing); the trading bound holds the live trading PnL
```

Polymarket settles each match on chain: the exchange emits one `OrderFilled` per resting order in the order the
operator filled them, then the taker's `OrderFilled` and `OrdersMatched`. `pm/chain_fills.parquet` decodes them
(`match_idx`, `maker_pos`, side, price and size of every fill), and `pm/filled_orders.parquet` is the list of every
resting order that filled, the part of the queue the chain makes visible.

Polymarket's documentation does not say in what priority it matches resting orders. The kit assumes price-time,
reads the operator's fill order as the queue order, and so takes the makers filled at your price before you in the
transactions of that batch as the queue ahead of you, completed orders included: `pm/chain_queue.parquet`, the
calibration's target (`fill_truth.n_chain`). Two things would show the assumption failing: `bound_violations`
(a true queue outside the two bounds), and, in `pm/chain_fills.parquet`, one of your orders filled while an
earlier order of yours at the same price, still resting, was passed over. On the example, which is FIFO by
construction, all 15 orders' queue truth equals the simulator's.

Polymarket has no queue endpoint, so the calibration fits on that fill truth (in shares), and fill truth exists
only for orders that filled. An order fills when more of the queue ahead than usual left, so that truth favours
short queues and models that fill early: the run warns about it (`fill_truth_selection`), and here the best fit is
the shortest-queue bound itself. `unfilled` checks each model on the orders that never filled. For a fit on every
order, filled or not, run the live calibration on the published book and your live log (it removes your own
orders itself):

<!-- check-docs: run -->
```sh
queuetruth calibrate-live --data px/book --live px/live --family power3 --mode corrected --out px-live
```

On the example it refuses too (`few_events`): one minute of quoting is not enough data for either fit.

The score counts the market's liquidity rewards and maker rebates (makers pay no fees) and marks to the result (NO
won here). The live total of -$36.19 lies outside the scenario range, and the run says so: the calibrated model
filled 400 shares where you filled 360.75, among them an ask that never filled live, so the replay ended 50 shares
long YES where you ended 89.25 long, and YES settled at 0. The trading bound still holds the live trading PnL
(-$37.09 in -$54.25 to $14.50).

## Fees and rewards, as the venues publish them

Every formula below is in `queuetruth.pm.fees` or `queuetruth.pm.rewards`, with its source and the date it was
read, and is tested against the venue's own worked examples where it publishes them.

**Kalshi trading fees** (the fee schedule filed with the CFTC on 2022-09-09, `fees = round up(0.07 x C x P x (1-P))`,
whose fee tables the kit reproduces cent for cent; the series' `fee_type` and `fee_multiplier` from `GET /series`;
docs.kalshi.com fee rounding; read 2026-10-24):

```text
model fee = 0.07 x fee_multiplier x share x C x P x (1 - P)       (C contracts at P dollars)
share     = 1 (taker); maker: 0 (quadratic), 0.25 (quadratic_with_maker_fees), 0.5 (quadratic_with_combo_maker_fees)
```

Each fill's trade fee is the model fee rounded up to $0.000001; a rounding fee aligns the balance to $0.0001
(direct members) or $0.01 (`--member non-direct`); the order's accumulator rebates whole multiples of that
precision. The maker shares are the API's own description of the fee types ("a 0.5 maker multiplier instead of
0.25"); the current fee schedule's Maker Fees section could not be fetched to cross-check them. A `flat` series uses
the fee schedule's table of fixed amounts, which is not machine readable: pass `--flat-taker` and `--flat-maker`.
Fills before a market's `fee_waiver_expiration_time` are free. Your live fills keep the `fee_cost` Kalshi reported
(or, for fills you only see as a growing fill count in `user_order`, the growth of the order's reported fees).

**Kalshi Liquidity Incentive Program** (the program terms filed with the CFTC on 2026-10-14, effective from
2026-10-28; help.kalshi.com, read 2026-10-24): once a second at a random moment, per side (YES bids, and NO bids,
which are the YES asks), walk the bids from the best: the price where the size reaches a fifth of the target size is
the reference price, and the walk stops at the target size (a side that never reaches it excludes the snapshot).
Each bid scores `discount_factor ^ ticks below the reference price x size`, normalized per side. The payout is
`period reward x sum of your snapshot scores / (2 x snapshots in the period)`, paid at $1.00 or more, rounded down
to the cent. The kit integrates your score over time on the book plus your resting size, which is the expected
payout of the random snapshots in your capture.

**Polymarket taker fees and maker rebates** (docs.polymarket.com trading/fees and programs/maker-rebates, read
2026-10-24):

```text
taker fee      = C x feeRate x p x (1 - p), rounded down to 5 decimals         (makers pay nothing)
fee_equivalent = the same formula for every filled maker order
daily rebate   = your fee_equivalent / the market's fee_equivalent x rebate pool
```

The rate and the rebate share come from the market's `feeSchedule` (`rate`, `rebateRate`), else from
`--poly-category` (the documented category table). The kit estimates the market's fee equivalent from the prints
in your capture.

**Polymarket liquidity rewards** (docs.polymarket.com programs/liquidity-rewards, read 2026-10-24): an order `s`
cents from the size-cutoff-adjusted midpoint scores `((v - s) / v)^2 x b` up to the max spread `v`; your bid-side
and ask-side scores give `Q_min = max(min(Q_one, Q_two), max(Q_one, Q_two) / 3)` when the midpoint is between 0.10
and 0.90, else `min(Q_one, Q_two)`; your share of the sample is your `Q_min` over everyone's; a day has 1,440
samples, and the reward is your summed share over the day's total times the market's daily reward. An L2 book does
not say how the other makers' size splits between them, so the kit reports the share with all of it as one maker
(`rewards`) and the bounds over every split (`rewards_lower`, `rewards_upper`). Orders under `rewardsMinSize` score
nothing, and a warning says so.

Pass `--actual-rewards` with what you were paid for the window to see the difference (`live_minus_actual`).

## Score hypothetical quotes

`--quotes` replaces your live orders with quotes from a CSV or Parquet file: `quote_id`, `instrument` (symbol) or
`instrument_id`, `side` (buy or sell), `price` (YES dollars), `qty` (contracts or shares), `ts_submit` (ns or RFC
3339), and optionally `latency_ns`, `ts_cancel` and `post_only` (default true: a quote that would cross is
rejected).

<!-- check-docs: run -->
```sh
head -3 pmfix/quotes.csv
queuetruth pm score --pm kx --quotes pmfix/quotes.csv --model "power3[n=1]" --out kx-quotes
```

A quote goes live at the first batch after its submit time plus latency, behind the size visible there. Without
`--calibration` or `--model` the uncalibrated `power3[n=1]` is used, with a warning. `--model` also takes the zoo's
spelling (`hbt_power3[exch=partial;mode=corrected;n=1]`).

## Run the zoo on the book

The datasets are ordinary queuetruth L2 datasets, so the zoo runs on them, with shadow prices and sizes in the
dataset's units (4400 is $0.44, 1500 is 15 contracts). On an L2 dataset it has no exact truth to score against, but
it writes every model's fills (`zoo_fills.parquet`); the kit's replay gives the same fills for the same orders
(`test_engine_reproduces_the_rust_zoo_corrected_mode`).

<!-- check-docs: run -->
```sh
printf 'shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns,ts_cancel\n' > shadows.csv
printf 's1,1,B,4400,1500,1792764003000000000,0,1792764011000000000\n' >> shadows.csv
queuetruth zoo --data kx/book_own_free --shadows shadows.csv --mode corrected \
    --models 'front_of_queue,trade_through_only,hbt_power3[n=2]' --out kx-zoo
```

## From Python

<!-- check-docs: run -->
```python
from queuetruth.pm.score import score

doc = score({"pm": {"path": "kx"}, "model": "risk_adverse", "settle": {"KXDEMO-26OCT23-T50": "0"}, "out": "kx-what-if"})
print(doc.mark, round(doc.band["total"].model, 2), doc.warnings)
```

The specs and documents are Pydantic models (`queuetruth.pm.models`); their JSON Schemas are committed in
`python/queuetruth/pm/schemas/`. Errors are `QtError` with the codes of [Error codes](../reference/errors.md):
`QT-USAGE-001` for bad flags, `QT-CONFIG-001` for a bad spec value or a used `--out`, `QT-INPUT-001` for a missing
file, `QT-INPUT-002` for a malformed capture (file and line named), `QT-INPUT-004` for a price or size that is not
exact. The `run_id` of every document depends on the bytes of your captures, not on where they are.

## Limits

- **Not yet run on real captures.** The readers follow the venues' documented message formats field for field and
  are tested on hand-written and simulated messages; no real Kalshi or Polymarket capture has been through the kit.
  A malformed or unexpected message fails with the file and line named, and the counters in `pm_ingest.json` show
  what was skipped.
- **Your captures only.** The kit does not connect to either venue; it reads what you saved. A gap in your Kalshi
  capture (a missing `seq`, or a reconnect) leaves the book gapped until the next snapshot, and orders resting
  through it are cut off (censored). Polymarket's market channel has no sequence numbers, so a reconnect there is
  not detected: its fresh `book` is read as a restatement and orders rest through the outage. Ingest each
  connection's capture on its own when you know it dropped.
- **The book is L2.** Queue positions are model estimates between two bounds. On Kalshi your official queue
  positions measure the estimate; on Polymarket the chain gives the truth only at your fills, which leans toward
  short queues.
- **Polymarket's priority is assumed.** The chain's queue truth rests on price-time matching, which Polymarket's
  documentation does not state.
- **Kalshi deltas net.** Deltas of one `clock` are one exchange action. When a single delta nets an add against a
  cancel at the same level (or one action removes several orders), the true queue can fall below
  `cancels_ahead`, and the `atomic` variants' reading of a decrease as one order is wrong for that change; bounds
  coverage and the held-out error show how often it matters.
- **Orders are shadows of their own.** The score replays each of your orders without your other orders at the same
  price ahead of it (a warning counts the orders this touches).
- **Polymarket books mirror.** One token's book is read as the whole book (a NO bid at q is a YES ask at 1 - q).
  With both tokens captured the tops are compared after every message (`complement_top_mismatch`).
- **Message details Polymarket leaves open.** The docs give `last_trade_price.side` only as BUY or SELL and do not
  say whether `price_change.size` is a level's new size. The kit reads the side as the taker's and the size as the
  new size, and checks both: prints that only a decrease of the other side explains are counted
  (`trades_other_side_explained`, with a warning when they are many), with a chain export every print is checked
  against the taker orders of its transaction (`chain.prints_side_mismatch`), and each message's stated best bid
  and ask is compared with the rebuilt book (`bba_mismatch`).
- **Speed bumps are not modelled.** Polymarket holds marketable orders on some markets for 250 ms (and longer
  around live games); quotes are post-only by default, so a quote that would cross is rejected rather than filled.
- **Fee and reward details the venues leave open:** the direction of Polymarket's 5-decimal fee rounding (down is
  used, since the docs say anything under 0.00001 rounds to zero), a `feeSchedule.exponent` other than 1 (applied
  to `p (1 - p)` and flagged), the size cutoff of Polymarket's adjusted midpoint (levels whose aggregated size
  reaches the min size) and Kalshi's flat fee table (pass its amounts yourself). The $1 payout minimum, the rounding
  down to the cent and Kalshi's per-account cap apply to a whole period's payout, so they are not applied to the
  expected payout of a capture.
- **Pure Python, linear in the capture.** On a simulated 130-minute capture (50,000 exchange events, 89,568
  capture lines, 62,450 book rows, 2,276 of your orders, 10,412 official positions), `pm ingest`, `pm calibrate`
  and `pm score` took 3.0, 3.6 and 4.5 s of CPU time on an 8-core laptop in low-power mode (2026-10-24). No real
  full-day capture of a busy market has been run through the kit.
- **Rewards are expectations.** Both programs sample the book at random moments; the kit computes the expected
  payout of your capture's part of a period, not what the venue's draw paid. The other makers' Polymarket scores are
  bounded, not known.
