# Score my own live fills (Bitstamp kit, IBKR on IEX guide)

**Goal:** put real orders from your own account through the scorer cheaply, with exact queue truth for every order:
record the venue's public order-level feed, place small passive orders, find each one in the public feed by its
venue order id, and score the fills that the simulators predict against what really happened.

**You need:** Python with queuetruth installed, the kit in `tools/livefill/` of the repository, and the `websockets`
package for live captures. Nothing in this guide needs an account until step 6. Live orders are placed only by the
account owner, explicitly, as described in step 6.

Bitstamp suits this because its public `live_orders` channel carries every order of a market with its venue order
id, and the id that the REST API returns when you place an order is expected to be that same id (step 6 says how to
confirm it on the first live run). A passive order of yours is therefore visible order by order in a public feed
that anyone can record, without any vendor data. Its fills are exact. Its queue position at entry is exact when the
replay's seed snapshot showed its price order by order, which the default REST seed always does (`entry_exact`).
Venue timestamps have millisecond resolution. The [limits](#limits) say where the replay and the ingest can still
differ.

All commands below run from the repository root with the kit on the Python path:

```text
export PYTHONPATH="$PWD/tools/livefill"
python -m livefill --help
```

## What the kit does and never does

| Step | Command | Network | Places orders |
|---|---|---|---|
| Capture the public feed | `capture` | public WebSocket + public REST | no |
| Check a capture | `check` | none | no |
| Rehearse on other traders' orders | `stand-in` | none | no |
| Plan and dry-run the harness | `harness` (default) | public REST only (or none with `--ref-bid`) | no |
| Place real orders | `harness --live --confirm ...` | private REST | **yes, your account** |
| Match, build the live log, score | `match`, `pipeline` | none | no |

A live run needs all of these together, or the harness refuses with `QT-USAGE-001` before it builds a request:
`--live`, `--confirm place-real-orders-on-<market>` (the exact string), `BITSTAMP_API_KEY` and `BITSTAMP_API_SECRET`
in the environment (never on the command line), explicit `--max-order-notional` and `--max-total-notional` caps, and
maker-or-cancel orders (`--no-moc` is dry-run only). The live HTTP transport can only be constructed from the object
that this check returns, and a dry run cannot be given a live transport (`test_live_guard_refuses_every_incomplete_request`,
`test_harness_modes_need_matching_transports`, `test_cli_harness_guards_and_dry_run` in
`tools/livefill/tests/test_harness.py`).

Two options are easy to confuse: `--book-seed` (check, export, match, pipeline) chooses the book snapshot a segment
is replayed from; `--seed` (harness, stand-in, synth) is a random seed.

## 1. Rehearse the whole pipeline offline

`synth` writes a synthetic Bitstamp capture with your own orders in it and the harness log that placed them. The
capture is produced by a small price-time engine that mimics how the real channels look (step 3).

<!-- check-docs: run -->
```sh
export PYTHONPATH="$QT_SRC/tools/livefill"
python -m livefill synth --out syn
python -m livefill pipeline syn/capture --log syn/harness_log.jsonl --out syn-run --models front_of_queue --strict
```

The pipeline checks the capture, exports it, matches the 24 own orders, writes the canonical live log, ingests the
market data, scores two arms and pools them. It prints a one-line summary per arm on stderr and the whole
`livefill.pipeline` document on stdout. The exact arm agrees with every live outcome: its 2x2 table has no
discordant order (`test_pipeline_end_to_end`), so `--strict` exits 0. One own order there rested and was filled
within the same millisecond. Up to queuetruth 0.1.1 the ingest dropped such an order as a taker and the scorer found
23 of 24 MBO twins; the ingest now keeps an order that a trade names as the maker, so it finds all 24, and the
matcher no longer flags that order `ingest_suppressed`.

<!-- check-docs: run -->
```sh
python - <<'EOF'
import json
d = json.load(open("syn-run/pipeline.json"))
seg = d["per_capture"][0]["segments"]["0"]
for s in seg["scores"]:
    print(s["arm"], s["two_by_two"], "twin match", s["twin_match_rate"])
for arm, p in d["pooled"].items():
    print("pooled", arm, p["two_by_two"], "bias", p["fill_rate_bias"], "CI", p["bias_ci"])
print("exact arm agrees:", d["exact_agrees"], "ingest_suppressed:", seg["ingest_suppressed"])
EOF
```

## 2. Capture the public feed

<!-- check-docs: skip (opens a live connection to Bitstamp) -->
```sh
python -m livefill capture --market usdtusd --market btcusd --duration 2h --out captures/2026-10-24
```

The daemon subscribes to `live_orders`, `live_trades` and `detail_order_book` for each market, fetches a full REST
book (`order_book?group=2`, which carries order ids) after each subscription, and writes:

- `messages/part-NNNNN.parquet`: every frame exactly as received plus indexed columns (`seq`, `recv_ns`, `mono_ns`,
  `conn`, `segment`, `kind`, `channel`, `market`, `event`, `event_id`, `pre_event_id`, `microtimestamp`, `order_id`,
  `trade_id`, `buy_order_id`, `sell_order_id`, `raw`). Parts are flushed every 10 s or 20,000 rows by temporary
  file and rename, so a killed daemon loses at most one buffer.
- `gaps.jsonl`, appended and flushed the moment a gap is seen, and `capture_meta.json`, rewritten at every flush.

Without `--duration` it runs until SIGINT or SIGTERM. It answers silence with `bts:heartbeat`, reconnects with
backoff after `bts:request_reconnect`, a transport error or `--stall-s` seconds without any frame, and resubscribes.
A storage error (a full disk) is not a transport error: the daemon stops with `QT-INPUT-001` and records the error
in `capture_meta.json` (`test_storage_errors_stop_the_capture`).

**Gap detection.** `live_orders` messages carry `event_id` and `pre_event_id`; per market and connection each
`pre_event_id` must be the previous `event_id`. A *gap* is one of `event_chain` (a break in that chain), `restart`
(`pre_event_id` null after earlier messages: the venue restarted the chain) or `reconnect` (a new connection; its
`reason` says why the old one ended, for example `stall: no frame for 61.0 s`). A break in one market leaves the other
markets' chains alone, so a drop that hits several markets at once is a gap in each
(`test_a_break_in_one_market_keeps_the_other_markets_chains`); a reconnect resets every chain. A message without a
`pre_event_id` member cannot be checked; it is counted (`no_pre_event_id`), not treated as a restart. Each gap starts
a new *segment* at the frame that revealed it; segments are exported, seeded and ingested separately, so a lost
message never corrupts another segment's book. A REST book whose request was sent before a gap and answered after
it describes the market before the gap: it is stored as `stale_rest_book` and never seeds anything
(`test_rest_book_requested_before_a_gap_is_stale`). A repeated `event_id` is kept as `kind = duplicate` and never
exported. The chain cannot see a lost trade (trades have no chain; the matcher's fill checks catch it for your orders)
or a loss after the last message of a capture. Tests: `test_chain_break_restart_duplicate_and_missing_ids`,
`test_daemon_reconnects_on_request_and_on_stall` (against a local WebSocket server that requests a reconnect and
then goes silent).

**Measured on a real capture.** A 2-minute public capture of `btcusd` and `usdtusd` on 2026-10-24 (13:59:27 to 14:01:27 UTC)
recorded 14,604 frames (13,341 `live_orders`, 28 `live_trades`, 1,225 `detail_order_book`, 2 REST books) in 1.3 MB of
Parquet, with one connection, no gap and one segment.

## 3. Check a capture

`check` replays each segment's `live_orders` from its seed and compares the book with every later
`detail_order_book` frame the venue sent (its best 100 orders per side: ids, amounts and queue order).

<!-- check-docs: run -->
```sh
export PYTHONPATH="$QT_SRC/tools/livefill"
python -m livefill check syn/capture --strict > check.json
python -c "import json; d = json.load(open('check.json')); print(d['verdict'], d['compared'], d['unexplained'])"
```

The verdict is the first that applies of `inconsistent` (a difference no nearby message explains, or an unknown
delete, below), `unchecked` (nothing could be compared), `partial` (some (segment, market) stream had no seed or no
later snapshot; `unchecked_streams` names them), `gaps` (consistent within segments) and `consistent`. `--strict`
exits 1 unless the verdict is `consistent`. What the comparison has to allow for, all measured on the 2-minute
capture above and on the 10-minute BTC/USD capture of 2026-10-12 behind RD-12 ([Results](../RESULTS.md)):

- **Stamps are whole milliseconds.** All 101,824 `live_orders` and 135 trade stamps of the 10-minute capture, and all
  13,341 and 28 of the 2-minute capture, end in `000` microseconds, while snapshots carry real microseconds.
- **The snapshot channel runs ahead of `live_orders`.** A snapshot often already shows orders whose messages arrive
  later, so a snapshot is compared with the book at its own arrival position and every difference must be explained
  by a message about the same order within `--window` messages (default 256). The largest distance needed was 165
  messages on the 10-minute capture and 38 on the 2-minute capture.
- **Same-millisecond orders queue by order id.** Two orders entered at one price in the same millisecond can arrive in
  the opposite order of their priority; the venue ranks them by id, and so does the replay. Ranking them by arrival
  instead (`--arrival-ties`, the ingest's rule) leaves 64 of 5,407 snapshots of the 10-minute capture unexplained
  (`test_rd12_capture_round_trip_and_check`, marker `realdata`).
- **The seed must be unambiguous.** After the seed, the ingest applies exactly the messages stamped at or after the
  seed's stamp. A message stamped in the seed's millisecond but arriving after it may or may not be in the seed, so the
  first seed is the first snapshot after which no message arrives stamped earlier. On the 10-minute capture that is
  the snapshot at `seq` 278, not the first one.
- **REST books are older than they arrive, and complete.** The REST book is the whole book (4,641 bids and 4,423
  asks of `btcusd`), stamped before it arrives; applying the messages stamped after it aligns it. The default seed
  (`--book-seed auto`) is the REST book when the segment has one, else the first `detail_order_book` frame, which
  shows only 100 orders per side.

**Lost messages between snapshots.** Inside the part of the book the seed showed order by order (with a REST seed,
all of it), every order is on the replayed book. A delete of an order the book never held, whose `order_created`
never arrived and that is not a taker, therefore means a lost `order_created`; `check` counts these as
`unknown_deletes` and they make the verdict `inconsistent` (`test_unknown_deletes_in_the_known_region_are_lost_creates`).
To measure what `check` catches on its own (the event chain of step 2 catches every lost `live_orders` message
first), one message at a time was removed from the real 2-minute `btcusd` stream, 30 random picks of each kind:

| Message removed | REST seed | WebSocket seed |
|---|---|---|
| `order_created` of an order with only a create and a delete | 30 of 30 caught | 24 of 30 caught |
| any `order_deleted` | 20 of 30 caught | 19 of 30 caught |

A lost delete is visible only while the order it leaves behind is among the 100 best of its side.

Results:

| Capture | Seed | Snapshots compared | Exact | Explained | Unexplained |
|---|---|---|---|---|---|
| 10 min, BTC/USD, 2026-10-12 | WebSocket (`seq` 278) | 5,407 | 2,843 | 2,564 | 0 |
| 2 min, BTC/USD + USDT/USD, 2026-10-24 | WebSocket | 1,223 | 834 | 389 | 0 |
| 2 min, BTC/USD + USDT/USD, 2026-10-24 | REST | 1,204 | 823 | 381 | 0 |

None of the three has an unknown delete. With the REST seed, `deleted_unknown` (deletes of orders the seed did not
show) on `btcusd` falls from 22 to 0, and the ingested segment holds 21,502 records instead of 12,615. The
10-minute capture, imported with `import-jsonl` and exported with its first snapshot as RD-12 did, ingests to RD-12's
77,272 records (`test_rd12_capture_round_trip_and_check`).

**Memory.** `check`, `export` and `match` hold one (segment, market) stream at a time, and read each snapshot from the
capture only when they compare it (`test_check_memory_is_one_stream_at_a_time`). Peak resident memory therefore
follows the longest stream. Measured on the 10-minute BTC/USD capture (101,824 `live_orders` messages, one segment):
`check` 428 to 445 MB over two runs, `export` 431 MB, `match` with 200 orders 412 MB, against 151 MB for starting
Python with the kit loaded. Checking its first 37,904, 75,671 and all 101,824 messages peaked at 334, 397 and 445 MB, about 1.7 MB per
1,000 messages. At that message rate (about 610,000 an hour), a gapless hour of BTC/USD would need about 1.3 GB
(extrapolated, not measured). The 2-minute two-market capture peaks at 274 MB for `check`.

## 4. Rehearse on other traders' orders

Before any account exists, `stand-in` writes a harness log that treats up to `--n` other traders' resting orders of a
capture as if they were yours (orders that rested and never took liquidity). The default `--sample filled_first`
takes orders that were filled first, which exercises the fill path but tilts the sample toward fills; `--sample
random` draws uniformly. The local send times are invented (50 ms before the order appeared); the queue positions,
fills and venue stamps come from the feed.

<!-- check-docs: run -->
```sh
export PYTHONPATH="$QT_SRC/tools/livefill"
python -m livefill stand-in syn/capture --out stand-in.jsonl --n 40
python -m livefill match syn/capture --log stand-in.jsonl --out stand-in-match
```

On the 2-minute real capture, 200 stand-in orders per market (`filled_first`), through `pipeline` with both logs:

| Market | Orders | Filled | Entry position exact | Exact arm 2x2 (both/neither/live only/sim only) |
|---|---|---|---|---|
| `btcusd` | 200 | 14 | 200 | 14 / 186 / 0 / 0 |
| `usdtusd` | 200 | 1 | 200 | 1 / 199 / 0 / 0 |

Every order was found by id, the check was `consistent`, the exact arm reproduced every outcome, and the trades of
each order summed to the sum of its `amount_traded` fields. On `usdtusd` one order lost 400.00201 of its amount with
no trade: it replaced an order that had been filled that much, so the matcher flags it `fills_consistent = false`.
This is a rehearsal of the tooling on two minutes of third-party orders, not evidence about any model, so the zoo
arms are not reported here.

## 5. Plan and dry-run the order harness

The harness first makes one signed read-only call (`account_balances`, the preflight); a live run stops before any
order unless the venue answers it and its server signature verifies. Then it places one order at a time: it reads
the public touch, places a maker-or-cancel (`moc_order`) limit order of a random notional at a random distance behind
the touch (`--ticks-behind`, default `0,0,0,1,2`), keeps it for a random time (exponential, `--hold-mean` 30 s,
capped at `--hold-max`), cancels it, reads its final status, and waits a random gap. The plan is a pure function of
the options and `--seed`. Client order ids are `qtlf-<plan tag>-<run nonce>-<index>`: the nonce is drawn for each
run and logged in `run_start`, so running the same command twice never reuses an id. Prices are passive against the
touch read just before the submit (a buy at most the best bid, a sell at least the best ask), the amount is rounded
down to the market's lot, and because that touch can be stale by the time the order arrives, a live run always
sends `moc_order`, so the venue cancels an order that would take.

A dry run builds and signs every request (with a dummy key) and answers it from an in-process simulator; nothing is
sent. `--fast` runs on a simulated clock and `--ref-bid` / `--ref-ask` replace the public REST touch:

<!-- check-docs: run -->
```sh
export PYTHONPATH="$QT_SRC/tools/livefill"
python -m livefill harness --market usdtusd --log dry.jsonl --n-orders 5 --fast \
    --ref-bid 0.99874 --ref-ask 0.99875 --base-decimals 5 --counter-decimals 5 --min-value 10
```

The log (`livefill.harness_log`) starts with `run_start` (mode, configuration, plan, nonce and the client order id
prefix) and has one record per request: `preflight`, `submit`, `cancel`, `status` and `open_orders` with `sent_ns`,
`recv_ns`, the parameters, the outcome (`ok`, `error` or `unknown`) and the venue's answer. Safety rules, each tested
in `test_harness.py`:

- Caps are checked before every submit on the exact notional `price * amount`: at least the market's
  `minimum_order_value`, at most `--max-order-notional`, the submitted total at most `--max-total-notional`, one
  order open at a time, and with `--max-net-position` (base units) the net filled amount of the run (buys minus
  sells, from each order's final `order_status`) plus the new order within that bound
  (`test_caps_compare_the_exact_notional`, `test_net_position_cap_counts_fills`).
- An answer that does not settle the outcome makes it `unknown`, never a rejection: any transport error (a timeout,
  `IncompleteRead`, a bad status line, an undecodable body), HTTP 408, 429 or any 5xx, a body that is not JSON, or a
  server signature that does not verify. A submit with an unknown outcome stops the run; the order is looked up by
  client order id and kept as possibly open (`test_any_lost_answer_is_unknown_and_the_order_is_swept`).
- A file named `STOP` next to the log stops new orders; any exception stops the run.
- On every exit the harness sweeps: it cancels every order it believes open, then lists the market's open orders and
  cancels every one whose client order id carries this run's prefix; if that listing fails, it cancels each unresolved
  order by its client order id (`test_unknown_outcome_with_a_failed_status_lookup_is_still_swept`,
  `test_live_bad_server_signatures_fail_closed`).

## 6. Going live (the account owner's step)

<!-- check-docs: skip (places real orders on the account owner's Bitstamp account) -->
```sh
export BITSTAMP_API_KEY=... BITSTAMP_API_SECRET=...
python -m livefill capture --market usdtusd --out captures/run1 &
python -m livefill harness --market usdtusd --log runs/run1.jsonl --n-orders 200 \
    --live --confirm place-real-orders-on-usdtusd --max-order-notional 30 --max-total-notional 6000 \
    --max-net-position 100
```

Before you start:

- **Fund both currencies.** With `--side random` the harness sells as well as buys: a sell needs the base currency
  (USDT here) and a buy the counter currency (USD), or the venue rejects the submit for insufficient funds. Hold at
  least `--max-order-notional` of each, plus room for `--max-net-position`.
- **Expect net exposure.** Fills on random sides leave a net position, bounded by `--max-net-position` in base units
  (100 USDT above).
- Keep the capture running from before the first order until after the last cancel; an order whose life spans a gap
  is not scored. Check the fee schedule and minimum order value of the market; the harness reads the minimum from the
  public markets list.

**First live run check.** The kit's join relies on the REST order id being the feed's order id, and its request
signing and server-signature check follow the API v2 documentation but have not been tried against the real server.
Start with a few orders (`--n-orders 5`) and check before running more: the preflight passed, every placed order was
matched by id (`matched_by` has no `signature_after_id_miss` and `id_misses` is 0; any miss means the ids differ), and
`venue_inconsistent` is 0 (the trades equal each order's executed amount from `order_status`).

## 7. Match and score

<!-- check-docs: run -->
```sh
export PYTHONPATH="$QT_SRC/tools/livefill"
python -m livefill match syn/capture --log syn/harness_log.jsonl --out syn-match
python -c "import pyarrow.parquet as pq; t = pq.read_table('syn-match/own_orders.parquet'); print(t.num_rows, t.column_names[:8])"
```

`match` writes `own_orders.parquet` (one row per harness order: how it was found, segment, entry stamp, the queue
position at entry as orders and quantity ahead, level rank and quantity at better prices, `entry_exact`, fills, end
state and the consistency checks), `own_fills.parquet` (trade id, venue stamp, price, quantity, maker or taker, the
quantity still ahead just before, fee), `own_queue.parquet` (one row each time the quantity ahead changed, with the
cause) and `match.json`.

- **Finding orders.** By venue order id. A submit whose answer was lost is searched for by signature (side, price,
  amount, appearing within 1 s before to 30 s after the send): matched when exactly one unmatched order fits,
  `ambiguous` otherwise. Odd amounts make signatures unique. An id that the feed never carried is searched for by
  signature too (`signature_after_id_miss`, counted in `id_misses` with a warning). Orders are keyed by log and
  index; a client order id that two submits share fails with `QT-INPUT-003` (`test_two_sessions_are_kept_apart`).
- **Exactness.** `entry_exact` is true when the order's price lies strictly inside the part of the book its seed
  showed order by order (always with a REST seed).
- **Fees.** Each fill's fee is the fee of the final `order_status` transaction with the same trade id, in the
  market's counter currency (`test_fees_come_from_the_order_status_transactions`). Dry runs, stand-ins and synthetic
  logs have none; a live fill without one is counted in `live_fills_without_fee` with a warning.
- **Consistency checks.** `fills_consistent` (the trades equal the amount the order lost on `live_orders`),
  `amount_traded_consistent` (the trades equal the sum of the per-message `amount_traded`; Bitstamp reports the
  amount each message executed, not a running total), `venue_consistent` (the trades equal the executed amount of the
  final `order_status`, live runs) and `fifo_violations` (a fill while orders were still ahead at the price, after
  allowing for trades printed before their order messages arrive).

`pipeline` takes one or more captures and every harness log of the run. Each capture gets the orders sent while it
was recording, on its markets (an order in no capture is counted in `orders_outside_captures`; one in two captures
of the same market is refused). For each capture it runs `check`, `export`, `match`, writes one canonical live log per
segment, ingests each segment with `queuetruth ingest --format bitstamp` and runs `queuetruth score` with the exact
arm and each model of `--models` (comma-separated, as `queuetruth zoo --models` takes them, or `all`). Its results go
under `cap-K/` of `--out`, which must be new or empty (`QT-CONFIG-001` otherwise; the kit never deletes anything).

The headline is `pooled`: per arm, the scored orders of every segment of every capture, with the summed 2x2 table,
the exact McNemar test on the pooled discordant counts, and the fill-rate bias (sim minus live) with a 95% interval
from a bootstrap that resamples whole segments (`test_pipeline_with_gaps_pools_the_segments`,
`test_pipeline_pools_several_captures`). Reaching the roadmap's 1,000 orders at one order every 35 seconds takes
about ten hours, over several captures and segments; they are pooled in one call. `pipeline.json` lists the
equivalent `queuetruth` commands and a `run_id` (16 hex digits over the inputs, the options and the queuetruth and
kit versions); two runs on the same inputs write byte-identical scorecards (`test_pipeline_end_to_end`).
`--require-consistent` stops with `QT-DATA-001` when a check is `inconsistent`, and `--strict` exits 1 when the exact
arm disagrees with live or a check is `inconsistent`.

The canonical live log (the directory `queuetruth live import` writes, validated on write) maps each order to
`order_key` `bitstamp:<account>:<venue order id>`, venue `bitstamp`, a `gtc` limit order with `post_only` for
maker-or-cancel:

| Event | Clocks | From |
|---|---|---|
| `submit_sent` | `ts_local_send` = harness send time | harness log |
| `ack` | `ts_exchange` = `order_created` stamp (`match_time`), `ts_local_recv` = REST answer | feed and harness |
| `fill` | `ts_exchange` = trade stamp (`match_time`), `ts_local_recv` = feed receive time; `fee` from `order_status` | `live_trades`, harness log |
| `cancel_sent` | `ts_local_send` = harness cancel time | harness log |
| `cancel_ack`, or `expire` without a cancel | `ts_exchange` = `order_deleted` stamp | `live_orders` |

`fill_id` is the venue trade id, `ts_precision` is `ms` when every feed stamp is a whole millisecond, and
`validation_status` is `real` only for orders of a live harness run. Rejected submits become `submit_sent` +
`reject`. Orders not found in the feed, orders whose life spans a gap and ambiguous signatures are excluded and
counted.

## Limits

- **A maker filled within the taker window.** An order whose only messages are `order_created` and `order_deleted`
  within 100 microseconds looks like a taker. `queuetruth ingest --format bitstamp` keeps it when a trade names it as
  the maker (counted in `maker_in_taker_window`; queuetruth 0.1.1 and earlier dropped it), and so does `check`. The
  matcher's `ingest_suppressed` flag follows the same rule. An order that no trade names as the maker is still
  dropped as a taker and flagged.
- **Same-millisecond orders.** The replay in `check` and `match` and, since the release after 0.1.1, the ingest rank
  new orders at one side and price within one millisecond by order id, as the venue's snapshots show. Datasets
  ingested with 0.1.1 or earlier ranked them by arrival and can differ from the venue's queue inside one millisecond;
  re-ingest captures made with those versions.
- **Venue stamps are milliseconds.** Exchange times of acks, fills and cancels carry Bitstamp's millisecond stamps.
- **Fees only from the account.** The public feed carries no fees; they come from the harness's `order_status`
  transactions (live runs only).
- **Redistribution.** Bitstamp's market-data terms for redistribution were not found
  ([data and licensing](../data-and-licensing.md)); keep captures private and publish aggregates.

## The IBKR route: limit orders on IEX matched against DEEP+ (guide)

IEX publishes its order-by-order DEEP+ feed for every trading day, free and redistributable with its attribution
line ([Replay a public IEX DEEP+ day](iex-public-demo.md)). Orders sent to IEX from an Interactive Brokers account
therefore leave a public, publishable trace. The kit contains no IBKR trading code; this is the procedure.

1. **Orders.** Send displayed, non-marketable limit orders with IEX as the destination (directed routing instead of
   `SMART`), time in force `DAY`, plain limit orders (not IEX's discretionary peg or D-Limit types, whose prices IEX
   can move). Use odd share counts (for example 137) and randomized times, sizes and distances from the touch, as the
   Bitstamp harness does; record your own send time for every order and cancel with nanosecond local clocks.
2. **Your records.** Export the day's executions with an IBKR Flex Query (Trades, level of detail Executions) and
   import them with `queuetruth live import --venue ibkr-flex --tz America/New_York`
   ([Import fills from my venue or broker](import-live-logs.md#ibkr-flex)). Flex has no order records, so that log is
   fills-only: orders that never filled are missing and the scorer refuses fill-rate statistics. For fill rates,
   write every order from your own order log (send, cancel and IBKR's status callbacks) with
   `queuetruth.live.write_canonical_log`, mapping events as in the table above.
3. **The market data.** Download the day's DEEP+ pcap from the HIST listing and ingest it with
   `queuetruth ingest --format iex-deep-plus --venue iex` for the symbols you traded.
4. **The join.** IBKR's records do not give a field documented to equal DEEP+'s order id, so find each order in the
   DEEP+ dataset by signature: the add with your symbol, side, price and odd size that appears within a short window
   after your send time, unique among candidates (the same rule as the Bitstamp signature match). Use its DEEP+
   timestamp as the `ack` exchange time (`match_time`) and its order id as `venue_order_id`, so self-exclusion matches
   by id. Check the join on the first day: every fill in your Flex export should be an Order Executed of the matched
   DEEP+ order with the same price and size.
5. **Score.** `queuetruth score --live <your log> --data <DEEP+ dataset> --sim exact`, then each zoo model, as in
   step 7. DEEP+ shows no non-displayed orders and does not report routed executions, so keep the orders displayed
   and non-marketable.

Whether IBKR's paper trading can stand in is a separate question: paper fills are simulated by the broker, not
matched in a real queue, so they are not exact truth.
