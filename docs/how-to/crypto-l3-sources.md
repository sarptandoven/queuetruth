# Ingest order-level crypto feeds (Coinbase level3, Kraken L3, Hyperliquid node)

**Goal:** turn your own order-level captures from Coinbase Exchange, Kraken or a Hyperliquid node into a queuetruth
MBO dataset, and know exactly where that dataset is truth and where it is not.

**You need:** captures you recorded yourself. The Coinbase `level3` and Kraken `level3` channels require API
credentials, so queuetruth ships no capture tool and no real data for them. For Hyperliquid you need a non-validator
node that you run with the `--write-*` flags below. Everything runs locally.

| Format (`--format`) | What you capture | Exact where | Not exact where |
|---|---|---|---|
| `coinbase-level3` | Coinbase Exchange WebSocket `level3` (compact `full`) plus REST `book?level=3` snapshots | Full depth, every order, venue order ids, queue order, from a snapshot until the next sequence gap | Between a sequence gap and the next snapshot; products with no snapshot (warm-up only) |
| `kraken-l3` | Kraken spot WebSocket v2 `level3` (`ws-l3.kraken.com/v2`) | Inside the subscribed depth (10, 100 or 1000 price levels per side), every order in queue order, verified by the CRC32 checksum | Any level beyond the window; whether a removal was a fill or a cancel; iceberg hidden size, untriggered stops, in-flight orders |
| `hyperliquid-node` | Your node's `node_raw_book_diffs`, `node_fills`, `node_order_statuses` (block-wrapped) plus an L4 snapshot | Full depth, every order with its on-chain `oid`, queue order, fills attributed to maker and taker orders | Coins without an L4 snapshot (warm-up only); times when the node fell behind (`block_time` gaps) |

Coinbase Advanced Trade has no order-level channel (its public WebSocket channels are `level2`, `ticker`,
`market_trades` and similar), so `coinbase-level3` reads the Coinbase Exchange feed.

All three formats use the `crypto_l3` rule set (`--venue generic` resolves to it): a modify keeps queue priority
only on a size decrease at the same price, which is what Coinbase `change`, Kraken amends and Hyperliquid in-place
size changes do. Prices default to `10^-9` and sizes to `10^-8` units; every decimal converts exactly or the ingest
fails with `QT-INPUT-004`.

## Coinbase Exchange `level3`

### What to capture

One JSON value per line, each wrapped with your receive time:

```json
{"recv_ns": 1791676801000007000, "msg": ["open","BTC-USD","101","5e1f...","sell","60002.00","0.40000000","2026-10-11T00:00:01.000000Z"]}
```

- The first frame after subscribing is the schema, `{"type": "level3", "schema": {"open": [...], "done": [...],
  "match": [...], "change": [...], "noop": [...]}}`. Keep it in the capture: every later frame is an array whose
  values follow the schema of its type, and Coinbase says the schema "should not change, it may". A new schema frame
  applies from its line on. Without one, the documented schema is used and `schema_assumed` counts the frames.
- REST snapshots, one per line, as `{"recv_ns": N, "product_id": "BTC-USD", "book": <body of GET
  /products/BTC-USD/book?level=3>}` (bids and asks as `[price, size, order_id]`). Take one after subscribing (the
  documented procedure: queue messages, fetch the snapshot, drop messages at or below its `sequence`) and another
  after every reconnect or sequence gap.
- Optional `heartbeat` frames: their `sequence` lets the converter detect a gap at the end of the capture.

A capture directory is read by file stem: `book` (snapshots), then `level3`, then `heartbeat`, then any other data
file (`.jsonl` or `.jsonl.gz`); `capture_meta.json` with `{"products": [...]}` fixes the instrument ids.

### How it becomes MBO

Per product, messages are de-duplicated by sequence and put in sequence order. The first snapshot becomes `R` plus
one `A` per order (`SNAPSHOT` flag, REST order is queue order). Then:

| level3 message | Records |
|---|---|
| `open` | `A` with the resting size |
| `change` | `M` (a null price keeps the price) |
| `match` | `T` (taker id; aggressor = opposite of the maker's side) + `F` + `C` of the maker |
| `done` | `C` of the remainder when one is left (fills always arrive as `match` first, so this is exactly a cancel or expiry); nothing when the order was filled to zero |
| `noop` | nothing; it only keeps the sequence continuous |

The compact channel has no `received` message, so a `done` or `change` for an order that never rested (a taker)
is ignored (`done_not_on_book`, `change_not_on_book`).

### Exactness guarantees and limits

- With a snapshot and no sequence gap, the dataset is the full-depth book order by order, with Coinbase's order ids
  and queue order. Fills are attributed to the resting order (`F`), and the taker id is on the `T`.
- A sequence gap opens a feed gap that lasts until the next snapshot (`sequence_gaps`, `feed_gaps`). A later
  snapshot re-seeds the book (`resnapshots`).
- A product with no snapshot is warm-up only: orders resting before the capture are invisible, so its whole range is
  a feed gap and `unseeded_products` is counted.
- Coinbase: "Authenticated feed messages do not increment the sequence number", so a dropped message that only an
  authenticated user sees cannot be detected.

## Kraken `level3` (a depth-window source)

### What to capture

Subscribe with `{"method": "subscribe", "params": {"channel": "level3", "symbol": [...], "depth": 10|100|1000,
"snapshot": true, "token": "..."}}` and record every frame, wrapped as `{"recv_ns": N, "msg": <frame>}`. Keep the
subscribe acknowledgement: its `depth` is the window size. Without it the converter assumes the documented default
of 10 and counts `depth_assumed`; `--option depth=100` overrides both.

### The window model

Kraken shows only the best `depth` price levels per side and sends no delete when a level falls out of scope. When
a level leaves the scope, "all orders in the next available level will generate an add event". The converter models
the window explicitly:

- The in-view book is truncated to `depth` levels at the event that pushes a level out, and again after each
  message. Orders of the dropped levels become **out of view** (`window_exits`). No record is written: they are not cancels, and they stay in the dataset frozen
  at their last known state.
- When a level comes back into view, its adds are reconciled with the orders kept at that price. An unchanged order
  writes nothing (`window_reentries`). A smaller size writes `M`, which keeps its queue position, as a Kraken amend
  down does. An order the level no longer holds was removed while out of view and gets `C` when this is learnt
  (`out_of_view_vanished`). An order never seen before gets `A` at the back (`window_entries`). If the venue's queue
  order cannot be reached that way, the kept orders from that point are re-queued with `C` + `A`
  (`reentry_requeued`).
- An add that repeats an unchanged in-view order means its level left and re-entered the window inside one
  message and was re-sent whole; it is reconciled the same way, so an order removed in between gets `C`
  (`add_repeated`).
- An out-of-view order whose price now lies inside the window can no longer be on the book (the venue would have
  re-sent it), so it gets `C` (`out_of_view_vanished`).
- Every record written by the window logic carries the `PUBLISHER_SPECIFIC` flag, so you can filter them.

### Integrity: the checksum

Kraken has no sequence numbers. Every snapshot and update carries a CRC32 over the top 10 levels, order by order, so
it also checks queue order. The converter verifies it after every message against its in-view book
(`checksums_verified`, `checksum_mismatches`). A mismatch opens a feed gap that lasts until the next matching
checksum.

Kraken sends `limit_price` and `order_qty` as JSON floats, which drop trailing zeros (`0.5`), while the checksum is
computed at the pair's precision (`0.50000000`). Give the pair precision so the checksum can be rebuilt:

<!-- check-docs: skip (needs your own authenticated Kraken capture) -->
```sh
queuetruth ingest --format kraken-l3 --venue generic --in kraken_capture/ --out kraken-ds \
  --option 'checksum_precision={"BTC/USD": [1, 8], "ETH/USD": [2, 8]}'
```

The precision is `[price_precision, qty_precision]` from Kraken's `instrument` channel. Without it the received
text is used, which matches only when the feed sent full-precision strings.

### Exactness guarantees and limits

- Inside the window: every order, in queue order, with Kraken's order ids, checked by the checksum after every
  message.
- Beyond the window: not truth. `window_truncated_depth` (the depth) is always in `source_specific`, so a Kraken
  dataset is never `clean`, and the ingest warnings say where truth stops. Do not trust queue ranks for orders
  priced beyond the window.
- The feed has no executions. `modify` is "a fill" (or an amend) and `delete` is "full fill or cancel", so fills
  cannot be attributed to resting orders (`no_execution_records` = 1, `deletes_fill_or_cancel`). Join Kraken's
  `trade` channel yourself if you need prints.
- The feed excludes in-flight orders, unmatched market orders, untriggered stop-loss and take-profit orders, and the
  hidden quantity of iceberg orders.
- Removal times for orders that vanished while out of view are the times the converter learnt of them, not the
  venue's.

## Hyperliquid node outputs

### What to capture

Run a non-validator node with:

<!-- check-docs: skip (runs your Hyperliquid node) -->
```sh
~/hl-visor run-non-validator --write-raw-book-diffs --write-fills --write-order-statuses --batch-by-block
```

`--batch-by-block` (or `--stream-with-block-info`) is required: raw book diffs carry no time of their own, and the
block wrapper `{"local_time", "block_time", "block_number", "events": [...]}` supplies it. A bare diff line is
`QT-INPUT-002`. For a seeded book, also compute an L4 snapshot from a state file at a known height and name it with
that height:

<!-- check-docs: skip (runs your Hyperliquid node) -->
```sh
./hl-node --chain Mainnet compute-l4-snapshots ~/hl/data/periodic_abci_states/20261011/812340000.rmp l4_snapshot_812340000.json
```

Give the node's data directories (or the files) to the ingest; directories are walked recursively and every line is
classified by its shape, so file names and order do not matter. Files named `l4*.json[.gz]` are snapshots. The
height comes from `--option snapshot_block=N`, from `snapshot_block` in `capture_meta.json`, or from the digits in
the file name.

<!-- check-docs: skip (needs your own node output) -->
```sh
queuetruth ingest --format hyperliquid-node --venue generic --symbols BTC,ETH \
  --in ~/hl/data/node_raw_book_diffs_by_block --in ~/hl/data/node_fills_by_block \
  --in ~/hl/data/node_order_statuses_by_block --in l4_snapshot_812340000.json --out hl-ds
```

### How it becomes MBO

Diffs are put in block order. The snapshot is applied before the first block above its height (the state after that
block) as `R` + `A` per order, in the snapshot's time order within each level; trigger orders are skipped. Diffs of
earlier blocks are superseded (`superseded_by_snapshot`). Then:

| raw book diff | Records |
|---|---|
| `new` | `A` |
| `update` to a smaller size | per maker fill of that `oid` (from `node_fills`, in fill order): `T` (taker `oid` and side from the `crossed` fill with the same `tid`) + `F` + `C`; any remainder `M` (`unattributed_decreases`) |
| `update` to a larger size | `M` (`size_increases`) |
| `remove` | the maker's fills as above, then `C` of the remainder; the order status says why (`removed_canceled`, `removed_filled_without_fills`, `removed_unclassified`) |

Order ids are the venue's `oid`s, `sequence` is the block number, `ts_event` is `block_time` and `ts_recv` is your
node's `local_time`. User addresses are dropped.

### Exactness guarantees and limits

- With a snapshot: the full on-chain book, order by order, in queue order (a Hyperliquid modify is a new `oid`, so
  queue position is never kept across a modify), with every fill attributed to its maker and taker order.
- Without a snapshot a coin is warm-up only (`unseeded_instruments`, a whole-range feed gap): orders resting before
  the first diff are invisible, and their updates count as `diff_unknown_order`.
- Without `node_fills`, size decreases cannot be attributed to fills (`no_execution_records` = 1).
- Whether the node writes empty blocks is not documented, so block numbers are not checked for continuity. A
  `block_time` step above `max_block_gap_ms` (default 5000, `--option max_block_gap_ms=0` turns the check off)
  between consecutive diff blocks is a feed gap (`block_time_gaps`). Size mismatches and unmatched fills are counted
  (`update_orig_mismatch`, `fill_size_mismatch`, `fill_price_mismatch`, `fills_unmatched`).
- No licence was found for Hyperliquid node data; keep datasets local.

## Read the result

Every caveat above is in the dataset's `data_quality.json`: the counters in `source_specific`, the gaps in
`feed_gaps`, and the plain-language notes as `warning: ...` entries in `reasons` (also in the ingest summary's
`warnings`). Any non-zero caveat counter makes the verdict `warnings`; `--strict` refuses anything but `clean`.

| Counter | Source | Meaning |
|---|---|---|
| `unseeded_products`, `unseeded_instruments` | Coinbase, Hyperliquid | No snapshot: warm-up only |
| `schema_assumed` | Coinbase | No schema frame in the capture |
| `match_unknown_maker`, `match_overfill`, `open_duplicate` | Coinbase | Book state disagrees with the feed |
| `heartbeat_trailing_gaps` | Coinbase | Messages missing at the end of the capture |
| `window_truncated_depth` | Kraken | Truth stops at this many levels per side |
| `checksum_mismatches` | Kraken | In-view book differs from the venue's |
| `out_of_view_vanished`, `reentry_requeued` | Kraken | Orders removed or re-queued while out of view |
| `add_duplicate`, `modify_unknown`, `delete_unknown`, `out_of_view_events` | Kraken | Feed events that do not fit the book |
| `no_execution_records` | Kraken, Hyperliquid | Fills cannot be attributed |
| `diff_unknown_order`, `duplicate_new`, `update_orig_mismatch`, `unattributed_decreases` | Hyperliquid | Diffs that do not fit the book or the fills |
| `fill_size_mismatch`, `fill_price_mismatch`, `fills_unmatched`, `removed_filled_without_fills` | Hyperliquid | Fills that do not fit the diffs |
| `block_time_gaps` | Hyperliquid | The node fell behind |

## Sources

All accessed 2026-10-19.

- Coinbase Exchange WebSocket channels, `level3` and `full`:
  [docs.cdp.coinbase.com](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels); sequence numbers:
  [overview](https://docs.cdp.coinbase.com/exchange/websocket-feed/overview); authentication:
  [authentication](https://docs.cdp.coinbase.com/exchange/websocket-feed/authentication); REST level 3 book:
  [Get product book](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-book);
  Advanced Trade channels:
  [WebSocket channels](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-channels).
- Kraken WebSocket v2 `level3`: [docs.kraken.com](https://docs.kraken.com/api/docs/websocket-v2/level3); L3
  checksum: [guide](https://docs.kraken.com/exchange/guides/websockets/l3-checksum-v2); amend priority:
  [amends](https://docs.kraken.com/exchange/guides/general/amends).
- Hyperliquid node flags and `compute-l4-snapshots`: [hyperliquid-dex/node](https://github.com/hyperliquid-dex/node);
  file schemas: [L1 data schemas](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/l1-data-schemas);
  order statuses: [info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint);
  fill fields: [WebSocket subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions).

The test fixtures under `crates/crypto/tests/fixtures/` (`coinbase_level3`, `kraken_l3`, `hyperliquid_node`) are
hand-built in these formats; the Kraken one is generated by `gen.py`, a small publisher model whose checksums come
from Python's `zlib.crc32`, independent of the Rust code.
