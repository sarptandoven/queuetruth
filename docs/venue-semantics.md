# Venue semantics

The queue position of an order depends on venue rules that are easy to get wrong: whether a modify keeps priority,
whether hidden executions exist and where they rank, how a fill and its removal are paired, how snapshots start a
book. A wrong rule does not crash anything; it silently shifts every downstream number. This page lists the rules
queuetruth applies per venue and per input format, and how well each is validated.

The claims below paraphrase the predecessor engine's venue notes (each claim there is tagged as verified in a primary
source or assumed) and the ingest code in `crates/ingest` and `crates/crypto`. Primary sources:
[Nasdaq TotalView-ITCH 5.0 specification](https://nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQTVITCHspecification.pdf),
[Databento GLBX.MDP3 dataset notes](https://databento.com/docs/venues-and-datasets/glbx-mdp3),
[Databento XNAS.ITCH dataset notes](https://databento.com/docs/venues-and-datasets/xnas-itch),
[DBN record and flag definitions](https://github.com/databento/dbn) and CME Globex's published MDP 3.0 and matching
algorithm documentation.

## The rule table

| Rule | What it controls | `nasdaq` | `cme` | `coinbase` | `generic` |
|---|---|---|---|---|---|
| `modify_records_present` | Whether `M` records are legal (otherwise counted as `unexpected_modifies` and ignored). | no | yes | yes | yes |
| `modify_priority` | When a modify keeps queue priority. | never | size decrease at the same price only | size decrease | size decrease |
| `snapshot_begins_with_clear` | Files must start with an `R` and a snapshot (otherwise an anomaly). | no | yes | no | no |
| `snapshot_records_flagged` | A duplicate add inside a flagged snapshot is not an anomaly. | no | yes | no | yes |
| `trades_have_aggressor` | `T.side` is the aggressor side. | yes | yes | yes | no |
| `supports_hidden_orders` | A `T` with side `N` is a hidden execution that consumes no displayed queue. | yes | no | no | yes |
| `fill_removal_shares_sequence` | An `F` is paired with its removing `C` by `sequence` (else within the event). | yes | no | yes | yes |
| `utc_midnight_snapshot` | The source inserts a snapshot at 00:00 UTC. | no | yes | no | no |
| `sequence_meaningful` | Sequence gaps and regressions are counted. | yes | yes | yes | no |
| validation | How the rules were validated. | real data | synthetic only | assumed | assumed |

`real data` means the rules were checked against real files of that venue (for Nasdaq: two public ITCH days, with
invariants, trade parity and the rank oracle; see [How queuetruth is tested](testing.md#real-data-checks)).
`synthetic only` means the reader and rules are tested on generated data but no real file has been replayed with
parity checks yet. `assumed` means the rules follow documentation and the predecessor engine's captures, without a
parity run. Every output that depends on a rule set carries its label.

Two more named rule sets reuse these columns. `crypto_l3` is `generic` without hidden orders (a side-`N` trade
consumes displayed queue); `--venue generic` resolves to it for every crypto order-level format, and the dataset
manifest records it (`rule_set`). `iex` (0.2.0) has the `nasdaq` values and runs on Nasdaq record semantics; it is
validated on real data (one public day, below).

## Nasdaq TotalView-ITCH 5.0 (`--format itch`, venue `nasdaq`)

- **Message mapping.** Add Order and Add Order with MPID become `A`. Order Executed becomes a `T` print (aggressor
  opposite to the resting side) plus `F` for the resting order plus `C` for the executed size. Order Executed with
  Price does the same at the execution price, but emits the `T` print only when the message is printable; the `C`
  is at the resting price. Order Cancel is a partial `C`; Order Delete is a `C` of the remaining size. Order Replace
  is a `C` of the old order plus an `A` with the new reference number.
- **No modify record.** A replace always creates a new order at the back of its level, even for a pure size
  decrease, because ITCH gives the new order a new reference. Partial cancels keep priority.
- **Hidden and cross prints.** Executions of non-displayed orders arrive as Trade (non-cross) messages with no book
  change; since 2014 their buy/sell indicator carries no aggressor information, so they become `T` with side `N`.
  Cross Trade messages (opening, closing and halt crosses) are also side-`N` prints and are omitted when they have
  zero shares. Non-printable executions are part of a later bulk print, which is why queue ahead is maintained from
  order records rather than prints ([A4](concepts/audit-rules.md#a4-queue-ahead-from-order-records)). Hidden
  interest ranks behind displayed interest at a price ([A3](concepts/audit-rules.md#a3-hidden-prints)).
- **Sequencing.** One `sequence` per ITCH message, shared by its `T`, `F` and `C`; `LAST` on the message's last
  record. `instrument_id` is the stock locate code, which is assigned per day, so cross-day joins use the symbol.
- **Prices and time.** ITCH prices have four decimals and are scaled exactly to the dataset's `price_exp`
  (default `-9`). Timestamps are nanoseconds since midnight Eastern time; the session defaults to 09:30 to 16:00
  US/Eastern, with daylight saving handled from the date.
- **Book start.** No snapshots: order references are unique per day and each day starts from an empty book.

## Databento DBN MBO and CME MDP 3.0 (`--format dbn`, venue `cme`)

- **Records.** DBN `mbo` records carry the actions `A`, `C`, `M`, `R`, `T`, `F`, `N` and the flags `LAST`,
  `SNAPSHOT`, `MAYBE_BAD_BOOK`, `BAD_TS_RECV`. Records of one instrument are never reordered, so FIFO order can be
  read from message order even though CME's priority tag is not exposed. Plain and zstd files are read; DBN v1 and v2
  files are decoded as stored.
- **Events.** `LAST` marks the last record of an event per instrument. Between records without it the book is
  updating and can look crossed.
- **Fills.** A `T` is followed by `F` records for the resting orders, which do not change the book; the quantity
  removal arrives as a separate update (`C` for a full fill, `M` with the new size for a partial fill) in the same
  event. A removal that arrives after an unpaired fill is counted (`removal_after_unpaired_fill`) and raises a
  data-quality warning.
- **Priority.** An order loses priority when its quantity increases, its price changes, or its account changes; a
  same-price size decrease keeps priority. An iceberg's refreshed slice goes to the back of the level. Both an
  account change and an iceberg refresh can appear as a modify with unchanged price and size, so on CME such a modify
  loses priority ([A6](concepts/audit-rules.md#a6-cme-modify-priority)).
- **Snapshots.** Snapshot records are flagged and introduced by an `R`; Databento inserts a full snapshot at 00:00 UTC
  each weekday. Snapshot orders carry the snapshot time, not their original entry time, so their queue order within
  a level is the snapshot's order.
- **Not modelled.** The implied book is not part of MBO; implied trades are side-`N` prints, which the L2 view sides
  from the quote where it can. FIFO replay is exact only for FIFO products: queuetruth has no rule set for pro-rata or
  allocation matching, so do not replay such products. Pre-open and pause periods can show locked or crossed books.
- **Validation: synthetic only.** No Databento key is used by the project; the DBN reader is tested by round trips
  of generated files (O-PAR-2).

## IEX DEEP+ (`--format iex-deep-plus`, venue `iex`)

IEX publishes every trading day of its order-by-order DEEP+ feed for free, and allows redistribution with its
attribution line. The reader follows the IEX DEEP+ Specification v1.06 and the IEX Transport Specification v1.26.
Every DEEP+ Order Modify says whether the order kept its time priority (Modify Flags bit 0), so priority is read,
not inferred: a maintained-priority size decrease at the same price becomes a partial `C` and keeps the queue
position; a reset becomes `C` of the remainder plus `A` with the same id at the back of the queue. `iex` datasets
therefore carry no `M` records. Order Executed becomes `T` (aggressor = the opposite of the resting side) + `F` + `C`;
a Trade between non-displayed orders becomes a side-`N` hidden print; Clear Book becomes `R`. Record `sequence` is the
IEX-TP message sequence number, so gaps are real feed gaps, and A/B duplicates are dropped per channel. DEEP+ does not
show non-displayed orders or the hidden parts of reserve orders, and routed executions are not reported.

- **Validation: real data.** One public day (2026-10-09; SPY, AAPL, NVDA, TSLA; 4,435,281 records) from the raw
  pcap: verdict clean, every replay invariant 0, and all 21,301 displayed executions at FIFO rank 0 of their price
  level. Steps and the full result table: [Replay a public IEX DEEP+ day](how-to/iex-public-demo.md).

## Crypto order-level captures (venue `generic` or `coinbase`)

These readers port the predecessor engine's converters for captures you record yourself. Each line of a capture is
one feed message plus your receive time `recv_ns`.

- **Bitstamp** (`--format bitstamp`: `live_orders`, `live_trades`, `detail_order_book` JSONL). Order created,
  changed and deleted become `A`, `M` (absolute remaining size) and `C`; a trade becomes a `T` (aggressor from the
  trade type) plus `F` for each resting order. Bitstamp has no sequence numbers, so messages are merged by
  microsecond timestamp and receive time. The first order-book snapshot seeds the book. Takers appear as an order
  created and deleted within the same microsecond; within `taker_window_us` (default 100) such pairs are suppressed
  and orders that would cross are held back until the book uncrosses (counters in `source_specific`).
- **Bitfinex** (`--format bitfinex`: raw `book_R0` and `trades`, plus `book_P0` and heartbeats for sequence
  checks). The raw book gives per-order adds, updates (`M`) and deletes (`C`); trades give `T` with the taker side.
  The raw book carries **no fill records**: a maker's decrease arrives as an update or delete, so fills cannot be
  attributed to resting orders. Such a dataset is not scorable as exact truth (counter `no_execution_records`).
- **Coinbase Exchange** (`--format coinbase`: the `full` channel, optionally with `matches` and `level2` lines).
  `open` becomes `A`, `match` becomes `T` plus `F` and `C` for the maker, `change` becomes `M`, `done` (cancelled)
  becomes `C`. Messages are de-duplicated by product and sequence; every sequence jump other than +1 is a feed gap.
  The `full` channel has no snapshot, so orders resting before the capture started are invisible until they trade or
  cancel; with `level2` lines present, the reader marks the warm-up period until every level of the top of book has
  been observed from empty, and reports it as a feed gap. Coinbase now requires an authenticated connection for the
  `full`, `level2` and `level3` channels, and authenticated messages do not increment the sequence number, so the
  gap check cannot see a dropped authenticated message. The compact `level3` channel is read by `coinbase-level3`
  (below).
- **0.2.0 sources** (`--format coinbase-level3`, `kraken-l3`, `hyperliquid-node`): Coinbase Exchange `level3` with
  REST level-3 snapshots, Kraken WebSocket v2 `level3` (exact only inside the subscribed depth window, CRC32-checked)
  and Hyperliquid node outputs with an L4 snapshot. Capture layout, record mapping and limits:
  [Ingest order-level crypto feeds](how-to/crypto-l3-sources.md). **Validation: synthetic fixtures only.**
- **Validation: assumed.** Each reader was checked on one capture by the predecessor engine at most. queuetruth
  replayed one 10-minute public capture per venue (2026-10-12, BTC-USD): Bitstamp 77,272 records and Bitfinex 51,701
  records, equal to the predecessor's converted counts, with every invariant 0 and, on Bitstamp, 0 crossed books
  after taker handling ([Results](RESULTS.md), RD-12). That shows replay and counters, not fill fidelity: neither feed
  carries the execution records exact queue truth needs.

## Tardis L2 (`--format tardis-l2`, L2 only)

`incremental_book_L2` and `trades` CSV files (optionally gzipped) become `qt.l2` rows and source trades. Rows are
ordered by `local_timestamp` and grouped into one batch per instrument and `local_timestamp`, which recovers the
original messages. `amount` is the new level size (0 removes the level). Rows before the first snapshot of a symbol
are skipped and counted. Trade rows come first in their batch, with the aggressor from the Tardis taker side
(`unknown` stays unsided). Order counts are not available. An L2-only dataset drives the zoo, markouts and
`calibrate-live`, never exact truth: `replay` on it fails with `QT-CONFIG-001`.

## The et CSV and normalized Parquet formats

`--format etcsv` reads the predecessor engine's `mbo.csv`, `trades.csv` and `instruments.csv`; the v0.1 checks
against the predecessor (`make realdata`) replay the two public Nasdaq days from it, while the public reproduction
(`make reproduce`) ingests the raw ITCH bytes (`--format itch`). `--format parquet` reads queuetruth's own normalized
`qt.mbo` tables.

## Out of scope

Pro-rata and allocation matching, auctions (orders resting through them are censored), halts, CME implied
liquidity, and iceberg reconstruction beyond the displayed slice. Kraken L3 is read since 0.2.0, but only as a
depth-window source: Kraken sends no delete when a level leaves the subscribed window, so queue-rank truth stops at
the window. See [Data and licensing](data-and-licensing.md#vendor-notes) for the terms of IEX, Kraken and the other
sources.

## Adding a venue

A new venue needs a rule-table entry (`qt_types::rules`), an ingest adapter, synthetic fixtures generated by
`qt_testkit` that exercise its rules, and a real-data parity run (invariants, trade parity, rank oracle) before its
validation label can change from `assumed`.
