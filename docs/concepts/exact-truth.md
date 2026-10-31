# Exact FIFO truth

queuetruth calls the output of its order-level replay "exact truth". This page says what that means, how the book is
checked, and where exactness stops. How shadow orders fill inside this book is on [Shadow orders](shadow-orders.md);
the rules that changed after the independent audit are collected on
[Rules from the independent audit](audit-rules.md).

## Why order-level data gives a ground truth

An L2 feed (market by price) gives the total displayed size at each price. An order-level feed (market by order,
MBO, or L3) gives every individual order: when it was added, reduced, modified, cancelled or executed, with an order
id. On a FIFO venue, orders at one price execute in arrival order. Replaying every record in feed order through a
book that keeps each price level as a FIFO list tells you, at every moment, exactly which orders are ahead of any
position in the queue. That is the quantity an L2 queue model has to guess.

Nasdaq TotalView-ITCH 5.0 is such a feed, and Nasdaq publishes full sample days. Databento's DBN MBO schema (CME
MDP 3.0) and the L3 channels of some crypto venues are others.

## The book

Each instrument has its own book: two sides of price levels, each level a FIFO list of resting orders. Records are
applied in source feed order and never re-sorted.

| Record | Effect on the book |
|---|---|
| `A` add | Appended at the tail of its price level. Ignored if the price is undefined or the size is 0. A duplicate order id outside a flagged snapshot moves the order to the tail and is counted (`duplicate_adds`). |
| `C` cancel | `size` is the quantity removed. Less than the resting size: a reduction, priority kept. Otherwise the order is removed. |
| `M` modify | Only on venues with modify records. Whether priority is kept depends on the venue rule (below); otherwise the order moves to the tail of its (new) level. Nasdaq has no modify record: a replace is a cancel plus a new add with a new reference. |
| `R` clear | Empties the instrument's book (snapshots, recoveries). |
| `T` trade | No book change. Carries the aggressor side when the venue reports it; `N` when it does not or the print is hidden. |
| `F` fill | No book change by itself. On ITCH and CME the removal that follows (`C`) changes the book. |

## Venue rules

Per-venue behaviour comes from a rule table (`qt_types::rules`, SPEC 3.2):

| Rule | `nasdaq` | `cme` | `coinbase` | `generic` |
|---|---|---|---|---|
| modify records present | no | yes | yes | yes |
| modify keeps priority | never | on a size decrease at the same price | on a size decrease | on a size decrease |
| snapshot begins with a clear | no | yes | no | no |
| snapshot records flagged | no | yes | no | yes |
| trades carry the aggressor side | yes | yes | yes | no |
| hidden (non-displayed) executions exist | yes | no | no | yes |
| fill and removal share a sequence number | yes | no | yes | yes |
| sequence numbers meaningful | yes | yes | yes | no |
| validation | real data | synthetic only | assumed | assumed |

On CME a modify with unchanged price and size (an iceberg refresh or an account change) loses priority. The
`validation` label is printed on every output that depends on it. In v0.1 only `nasdaq` is validated on real data;
see [Venue semantics](../venue-semantics.md) for the per-venue details and their sources.

## Events: the book is only consistent at event boundaries

A feed often needs several records to describe one matching-engine action. An aggressive order that sweeps three
resting orders produces a trade print and three fills and removals, and between those records the book can look
crossed. queuetruth groups records into **events**: a maximal run of records of one instrument that ends with a
record flagged `LAST`. The BBO, depth, L2 rows, shadow fill decisions and invariant checks happen only at event end.
Shadow activations, cancels and amends happen at event start. A partition that ends inside an event gets a synthetic
end, counted as `unterminated_events`.

## How the book is checked

Four checks, reported in `summary.json` of every replay:

1. **Invariant counters.** Eight counters that are zero on clean data: cancels exceeding the resting size, fills
   exceeding the resting size, fills of unknown orders, references to unknown orders, duplicate adds, unexpected
   modifies, books crossed after a complete event, negative spreads.
2. **Trade parity** (`--parity-trades`). Trades derived from the replay are matched in order against the source's
   own execution prints on `(ts_event, sequence, price, size, aggressor side)`. `parity_bbo` compares the derived BBO
   stream with a reference file.
3. **Queue-rank oracle** (`--rank-oracle`). For every real execution, the rank of the executed order within its
   price level at that moment (orders ahead of it not already consumed earlier in the same event). On a FIFO venue
   with a correct book it is 0. Executions at one price within `sweep_gap_ns` (default 2 ms) of each other form a
   sweep; the sweep-adjusted rank ignores orders consumed earlier in the same sweep. The summary gives the share at
   rank 0, a histogram of ranks 0 to 5+, the share in the first 60 s after the open and the share per 30-minute
   bucket.
4. **Differential and reference tests.** The production book is compared after every record with a deliberately
   naive reference book on thousands of random streams (O-BOOK-1, O-BOOK-2). See
   [How queuetruth is tested](../testing.md).

On the two public Nasdaq TotalView-ITCH days used for the published results (2019-12-30 and 2020-01-30, AAPL, MSFT,
SPY and TSLA; [Results](../RESULTS.md) sections 1 and 2): 0 invariant violations over 16,754,562 records, 0
trade-parity mismatches over 633,287 source prints, 0 price-priority violations over 490,472 checked executions, and
98.72% (day one) and 99.09% (day two) of executions at rank 0 after sweep grouping. The checks were run on the
predecessor engine's CSV conversion of the files and repeated from the raw ITCH bytes with queuetruth's own ITCH
reader (`scripts/realdata/itch_subset.py`, `scripts/realdata/itch_realbytes.py`), with the same results. The
remaining executions are listed by time bucket; we do not claim to explain all of them.

## Data quality

Every partition (one instrument on one session date) gets a verdict:

- `unusable` if any record is flagged `MAYBE_BAD_BOOK`, or more than 0.1% of events end with a crossed book, or more
  than 0.1% of records reference unknown orders;
- `warnings` if any anomaly counter is non-zero: sequence gaps or regressions, out-of-order timestamps, unknown
  references, duplicate adds, unexpected modifies, crossed books, pre-snapshot rows skipped, feed gaps, or a
  source-specific counter such as `removal_after_unpaired_fill`;
- `clean` otherwise.

Hidden prints and off-price executions are counted (`hidden_prints`, `off_price_executions`) but do not raise the
verdict: they are normal on Nasdaq. The dataset verdict is the worst partition. Unusable partitions are still
replayed, every analysis summary carries the verdict, and `--strict` turns it into an error (`QT-DATA-001`). A
sim-vs-live comparison on unusable data measures the data, not the simulator.

## Where exactness stops

- **Hidden and non-displayed liquidity.** The book is exact for displayed orders only. Hidden executions are
  counted and handled by the rule on [Shadow orders](shadow-orders.md#hidden-prints); price-slid orders, reserve
  (iceberg) quantity beyond the displayed slice and CME implied liquidity are not modelled.
- **Your own order's effect on others.** A shadow never takes volume from orders behind it and never changes anyone's
  behaviour. See [Shadow orders](shadow-orders.md#what-a-shadow-does-not-do).
- **Auctions and halts.** Not modelled as matching. Ingest records when each instrument is halted, paused or quoting
  before an IPO or halt cross (Nasdaq ITCH `H` and `h` messages, IEX DEEP+ trading status and operational halts:
  `trading_halts` in `data_quality.json`), and `replay`, `why`, the zoo, calibration and features censor shadows
  there: a shadow live when a halt starts ends `censored_gap`, and one that would go live during it never does.
  Taking part in the halt or IPO cross is not modelled. Datasets ingested before this was added carry no spans and
  treat a halted symbol as trading continuously: re-ingest them. DBN, NYSE and the crypto sources record no spans
  yet ([halt censoring](../design/extensions/halt-censoring.md)).
- **Pro-rata and allocation matching.** There is no rule set for them; FIFO replay of such a product is not exact.
- **L2-only data** (for example Tardis `incremental_book_L2`) drives the zoo, markouts and `calibrate-live`, never
  exact truth.
- **Timestamps.** Exchange timestamps are matching-engine times. The latency between your decision and the matching
  engine is an input you supply, not something the replay can know.
