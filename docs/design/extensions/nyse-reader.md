# Extension: NYSE TAQ Integrated Feed reader and the `nyse` rule set

Proposed spec text for the `nyse_ibf` ingest format (`qt_ingest::nyse`) and the `nyse` rule set, the first rule
set whose allocation is not FIFO. It is additive (schema 1.2: JSON `schema_version` stays 1, no Arrow change) and is
written to be folded into `docs/design/spec.md` at the sections named below. User guide:
[Read NYSE order-by-order data](../../how-to/nyse-order-level.md).

## 3.1 Core types: `RuleSet::Nyse`

```rust
pub enum RuleSet { Generic, Nasdaq, Cme, Coinbase, CryptoL3, Iex, Nyse }  // names ... |iex|nyse
// FromStr adds "nyse" | "xnys"; RuleSet::Nyse.venue() == Venue::Nasdaq ("nyse" is not a Venue).
// rules_for(RuleSet::Nyse) = rules(Venue::Nasdaq) with rule_set = Nyse, sequence_meaningful = false,
// validation = SyntheticOnly.
```

Databento's dataset code `xnys.pillar` is deliberately not an alias: DBN files are read by `--format dbn`, which
resolves a `Venue`, not a rule set, so the alias would suggest a route that does not exist (section 9, open items).

## 3.2 Venue rules: column `nyse`

Every value equals `nasdaq` except three:

| field | `nyse` | why |
|---|---|---|
| `sequence_meaningful` | false | Record `sequence` is the channel SequenceNumber. The TAQ product leaves out sequenced control messages: the Source Time Reference message alone is sent once a second per channel (Pillar Equities Common Client Specification v2.4p, section 4.2), so holes in the numbering are expected and are not losses. The reader checks Symbol Sequence Numbers instead (3.12.3). |
| `rule_set` | `nyse` | |
| `validation` | SyntheticOnly | Built and tested from the specification on hand-written and generated files; no real file has been replayed (section 9). |

`modify_records_present` false (a Modify Order becomes a partial `C` or `C` + `A`), `trades_have_aggressor` true,
`supports_hidden_orders` true (Non-Displayed Trade is a side-`N` print), `fill_removal_shares_sequence` true,
`hidden_lower_priority` true (NYSE Rule 7.36(e): Priority 2 Display Orders rank ahead of Priority 3 Non-Display
Orders), `iceberg_refresh_loses_priority` false (it governs `M` records, which `nyse` datasets do not carry; Pillar
gives the replenished part of a reserve order a new Order ID, so it arrives as an add at the back of the queue).
Replay's default `HiddenPrintPolicy` follows the record venue (`behind_and_through`). The manifest's `venue` is the
record semantics (`nasdaq`); the market is in `source_venue` and the rule set in `rule_set`.

Allocation is not a `VenueRules` field: the engines keep every price level in time order. The rule set documents it
per market and session date (`qt_ingest::nyse::rules::Market::allocation_on`; `Market::allocation` is today's rule):

| market | Market ID | file token | allocation | source |
|---|---|---|---|---|
| NYSE | 1 | `NYSE` | parity | NYSE Rules 7.36(a)(5), 7.36(h), 7.37(b); SR-NYSE-2019-05 (DMM Participant, approved 2019-05-29); SR-NYSE-2020-55 (Setter Priority trades in full, approved 2020-08-06) |
| NYSE Arca | 3 | `ARCA` | price-time | Rules 7.36-E, 7.37-E |
| NYSE American | 9 | `AMEX`, `MKT` | price-time from 2017-07-24 (Pillar); parity before, as the Floor-based NYSE MKT (`MKT` files, 2015-11-16 to 2017-07-21) | SR-NYSEMKT-2017-01 (notice Release 34-79993, 2017-02-09; approval as amended Release 34-80590, 2017-05-04) |
| NYSE National | 10 | `NATIONAL` | price-time | SR-NYSENAT-2018-02 |
| NYSE Texas | 11 | `TEXAS`, `CHICAGO` | price-time | SR-NYSECHX-2019-08 |

Parity, as NYSE Rule 7.37(b) states it: at each price an Aggressing Order goes first to unexecuted Market Orders by
time, then to the order with Setter Priority (the first round-lot displayed order that set a new NBBO at that price),
which trades its full displayed size (NYSE Parity FAQs, 2024; not kept in the Closing Auction or a reopening
auction), then to displayed orders on parity by Participant, then to non-displayed orders on parity. A
Participant is a Floor broker trading license, the DMM assigned to the security, or the Book Participant (every
other order, collectively). The Participants at a price sit on an allocation wheel in the order of their first
order there; a pointer names the next one; each turn allocates one round lot; an odd-lot remainder keeps the
pointer; inside the Book Participant orders fill by working time.

Label: the feed does not say which Participant entered an order, so on a parity market a FIFO rank is not the
allocation order and the error has no fixed sign. The ingest therefore writes the non-INFO `source_specific` counter
`nyse_parity_ranks_approximate` = the number of partitions (instrument-days) with records whose market allocated by
parity on that session date, whether they traded or not (an instrument's market is its Symbol Index Mapping's, else
the file names'), the INFO counter `nyse_parity_executions` = their displayed executions (Order Execution
messages), and a plain-words ingest warning naming the rule (NYSE Rule 7.37(b), or NYSE MKT's floor-based parity for
its days). Partitions whose market is unknown (no market in the file names, no Symbol Index Mapping) are counted in
the non-INFO `nyse_allocation_unknown`. Being non-INFO, the label makes the ingest verdict at least `warnings`;
replay merges the ingest data quality into its own (`qt_replay::run`), so its summary carries the reason `ingest:
nyse_parity_ranks_approximate = N` and a verdict of at least `warnings`, and `--strict` fails; `queuetruth dq`
reports it under `dq_counters`. The counter is absent on price-time market days. Since the label is permanent on
NYSE, `--strict` cannot gate an NYSE dataset; the guide gives a gate that ignores only the label. Two places do not
carry it yet: the per-shadow `dq_flags` of a replay come from the partition's own book counters
(`qt_replay::Engine`), and the zoo, calibration and features engines read only the dataset's feed gaps (the zoo's
terminal output still says "exact" on NYSE data; open items).

## 3.12 Schema 1.2 additions

- `IngestSpec.format` value `nyse_ibf` (Python `IngestSpec.format`, JSON Schema `qt.ingest_spec`); CLI spelling
  `nyse-ibf`.
- Manifest `rule_set` value `nyse` (Python `RuleSetName`, JSON Schemas `qt.dataset_manifest`, `qt.dataset`,
  `qt.ingest_summary`, and the `rule_set` enum of `service/openapi.json`); Python `queuetruth.dq` mirrors it as
  `RULES["nyse"]` (the `nasdaq` rules with `sequence_meaningful` false) and treats it as a US equity venue
  (sub-penny ticks below $1).
- `qt.data_quality.source_specific` INFO counters (never part of the verdict): `nyse_parity_executions`,
  `nyse_modify_keep_priority`, `nyse_modify_lose_priority`, `nyse_modify_noop`, `nyse_replace_orders`,
  `nyse_replace_reduce_same_price`, `nyse_nonprintable_executions`, `nyse_nonprintable_hidden_trades`,
  `nyse_zero_volume_crosses`, `nyse_attributed_adds`, `nyse_trade_cancels`, `nyse_cross_corrections` (TAQ spec
  sections 14 and 15: a bust or cross correction does not restore any order, so book builders may ignore them; the
  ITCH reader does not count broken trades either) and `nyse_failover_books_kept`.
- Non-INFO `nyse_*` counters (each makes the verdict at least `warnings`): `nyse_parity_ranks_approximate` (the
  label), `nyse_allocation_unknown`, `nyse_exec_exceeds_resting`, `nyse_side_mismatch`, `nyse_bad_side`,
  `nyse_bad_price`, `nyse_zero_size`, `nyse_malformed_lines`, `nyse_unknown_message_type`,
  `nyse_other_product_messages`, `nyse_refresh_runs`, `nyse_failover_books_cleared`,
  `nyse_files_without_order_messages`, `nyse_midnight_rollovers`, `nyse_channel_sequence_repeats`,
  `nyse_market_mismatch`, `nyse_non_equities_market`, `nyse_symbol_on_two_channels`, `nyse_symbol_sequence_missing`,
  `nyse_symbols_not_found`. Unknown Order IDs and live Order IDs added again go to the standard `unknown_order_refs`
  and `duplicate_adds`; symbol-sequence gaps to `sequence_gaps` and `feed_gaps`; channel and symbol sequence
  regressions to `sequence_regressions`.
- MBO record flag: `F` and `C` of a non-printable Order Execution (an auction allocation) carry `PUBLISHER_SPECIFIC`
  (bit 1), so a consumer can tell auction allocations from continuous-book executions.

## 3.12.3 NYSE TAQ Integrated Feed (new section)

`nyse_ibf`: the TAQ NYSE Integrated Feed product of NYSE Group, the historical record of each XDP Integrated Feed
data channel of NYSE, NYSE American, NYSE Arca, NYSE National and NYSE Texas: one file per channel and trading day,
`EQY_US_<EXCH>_IBF_<n>_<YYYYMMDD>.gz`, newline-terminated ASCII CSV, one real-time message per line in feed order.
Read order by order by `qt_ingest::nyse`. Sources: TAQ Products Client Specification v2.4b (2026-09-08; layouts and
data types), Pillar Equities Common Client Specification v2.4p (sequence numbers, symbol sequence numbers, Source
Time Reference, Symbol Clear), XDP Integrated Feed Client Specification v2.3b (refresh), XDP Common Client
Specification v2.4 (publisher failover, section 9.2).

- Layers: `reader` (gzip by magic bytes behind the decompression guard, or raw; line framing; file names) ->
  `message` (CSV layouts of types 3, 34, 100 to 106, 110 to 114, 223) -> `mapper` (records, prints, sequences,
  refresh runs, failovers, parity accounting) -> `ingest`; `synth` writes synthetic files.
- Spec: `venue` must resolve to rule set `nyse`, else `QT-CONFIG-001`. Session dates from the file names, or from
  `session_date` / `midnight_utc_ns` (which must agree with the names and, for `midnight_utc_ns`, be the local
  midnight of the date); a name that does not follow the convention needs `session_date`. Every record of a file
  goes to that file's session date. `tz` default America/New_York (SourceTime is a time of day with no zone in the
  specification; see the clock check), `UTC` allowed. `price_exp` in -12..=-4 (prices go to 1/100 of a cent),
  default -9; `size_exp` 0 (shares); tick default 0.01; `lot_size` default 1; no `instruments` file;
  `open_s` / `close_s` default 34,200 / 57,600. Options: `open_s`, `close_s`, `memory_rows`,
  `check_symbol_sequence` (bool, default true). The stock summary file (`EQY_US_<EXCH>_STOCKSUM_<date>`) is
  refused (`QT-CONFIG-001`), and so are inputs of two markets or two files of one channel and day.
- Inputs are read in canonical order (session date, channel, path) whatever the order given; `instrument_id` = order
  of first appearance of the symbol over all inputs, before the symbol filter (the filter does not change ids). A
  new session date starts every symbol from an empty book. `source_venue` = the market (`nyse`, `nyse_arca`,
  `nyse_american`, `nyse_national`, `nyse_texas`, from the file names, else from the Symbol Index Mapping messages);
  the manifest carries `venue = nasdaq`, `rule_set = nyse`, `validation_status = synthetic_only`.
- Records: `sequence` = the channel SequenceNumber (the `T`, `F`, `C` of one message share it); `channel_id` = the
  channel of the file name (interim NYSE Tape A files `1A` to `4A` are 101 to 104; 0 for other names); `ts_event` =
  `ts_recv` = local midnight + SourceTime (+ one day per time the clock goes back by more than 12 hours inside a
  file, counted `nyse_midnight_rollovers`); `LAST` on the last record of each message; prices exact from the decimal
  text (more than 9 fraction digits, 0, or not representable at `price_exp`: `nyse_bad_price`, message skipped; an
  execution then uses the resting price).
- Mapping: Add Order 100 -> `A` (`B` -> bid, `S` -> ask). Modify Order 101: same price and smaller size with
  PositionChange 0 -> `C` of the decrease (keeps priority, Rule 7.36(f)(3)); same price and size -> none; a new
  price, a larger size or PositionChange 1 -> `C` of the remaining size + `A` (same id, back of the queue); size 0 ->
  `C`. Delete Order 102 -> `C` of the remaining size. Order Execution 103 -> `T` (aggressor = opposite of the
  resting side, `order_id` 0, at the execution price; only when PrintableFlag is 1) + `F` (resting order, execution
  price) + `C` (resting price); print kind `displayed` with the maker id; non-printable executions (auction
  allocations) give `F` + `C` only, both flagged `PUBLISHER_SPECIFIC`. Replace Order 104 -> `C` of the old order +
  `A` with the new Order ID (back of the queue; when the old id is unknown the new order is still added if the
  message names its side). Add Order Refresh 106: the first of a run clears the symbol (`R`) when it had a book,
  then `A` per order; a run ends at the symbol's next other message; the Symbol Clear that precedes a failover
  refresh is not in the TAQ product, so an Order ID repeated inside a run starts a new run. Non-Displayed Trade 110
  -> `T` side `N` (hidden print) when printable, none otherwise. Cross Trade 111 -> `T` side `N`, print kind `cross`
  (none for 0 shares). Symbol Index Mapping 3, Security Status 34, Imbalance 105, Trade Cancel 112, Cross
  Correction 113, Retail Price Improvement 114 and Stock Summary 223 -> no records. A live Order ID added again
  first cancels the old order. Why a Replace goes to the back: on Pillar a cancel/replace keeps its time priority and
  Order ID only when it reduces the quantity (or changes the side from S to SS / SSE); every other cancel/replace,
  and the replenished part of a reserve order, gets a new time priority and a new Order ID (NYSE Pillar Trader
  Update, 2015-03-02). The reader expects the priority-keeping reduction as a Modify Order; TAQ spec section 6 sends
  a Modify Order for changes other than a cancel-replace and section 9 gives every Replace Order a new Order ID, so
  which message a reduction uses waits for a real file (`nyse_replace_reduce_same_price` counts Replace Orders at
  the old side and price with a smaller size).
- Failovers (TAQ spec section 11; XDP Common Client Specification v2.4 section 9.2): the backup publisher clears
  every symbol of the channel (Symbol Clear) and re-sends each order still resting as an Add Order Refresh, so a
  symbol whose orders were all removed during the outage gets no refresh. An Add Order Refresh of any symbol (the
  symbol filter does not matter) starts a failover of the file's channel, unless it continues the current one (an
  Add Order Refresh less than 60 s after the previous one of the channel, of a symbol not already refreshed and back
  to live messages in it: the per-symbol refresh sequences of one failover, with their Symbol Index Mapping and
  Security Status lines between them, are one failover). At its start every other selected symbol of the file with
  resting orders becomes a suspect; at the suspect's next order message an Add Order Refresh refreshes it (the run
  above); a Modify, Delete, Execution or Replace of one of its resting orders keeps its book
  (`nyse_failover_books_kept`: that refresh did not cover the whole channel); an Add Order, or a Replace Order of an
  order it does not have, clears its book first: `R` (in the message, at its time and sequence),
  `nyse_failover_books_cleared`, a `feed_gaps` entry from the symbol's last message before the failover to this one
  (reason `nyse failover: symbol cleared without refresh`), and the symbol sequence re-baselined. Other messages
  (Security Status, Imbalance, prints, cancels, references to unknown orders) leave it a suspect. Suspects left at
  the end of the file are cleared there (`R` at the file's latest SourceTime and last channel SequenceNumber, one
  message each, and the same feed gap).
- Layout tolerance: blank fields read as 0 (spec 2.2 rule 6); the informational fields the mapper does not use
  (codes and numbers of types 3, 34, 111 and 114, other than Symbol and Market ID) read as 0 when they hold an
  unexpected value instead of rejecting the line; trailing fields beyond a layout are ignored and
  missing optional trailing fields read as blank (Side of 101 / 104 was Reserved before spec 2.3g; 103 / 110
  carried DBExecID instead of trade conditions before 2.3e). The tables of 105 and 106 skip field number 4; both a
  contiguous line and one with an extra field 4 are read (field 5 is numeric in the first, the Symbol in the
  second).
- Sequencing: channel SequenceNumbers are checked for regressions (`sequence_regressions`) and repeats
  (`nyse_channel_sequence_repeats`) only. Each symbol-sequenced message (34, 100 to 106, 110 to 114) of a selected
  symbol must carry the symbol's previous SymbolSeqNum + 1; a jump is a `sequence_gaps` entry plus a `feed_gaps`
  entry from the symbol's previous message to this one (replay censors that instrument's shadows there); a repeat or
  a decrease is a regression. Refresh runs are not checked, re-baseline the symbol and add a `feed_gaps` entry from
  the symbol's last message before the run to its first message after it.
- Wrong input: a file whose first non-blank line does not parse, or that holds a BBO / Trades / TRF message type
  before any order message, is `QT-INPUT-002` (named with its line); later malformed lines are counted
  (`nyse_malformed_lines`, the first one named in a warning) and skipped. A file without one order message (types
  100 to 104 and 106) is counted (`nyse_files_without_order_messages`, the first ten named in a warning). A
  truncated gzip stream and a line longer than 65,536 bytes (`reader::MAX_LINE`; real lines are under 300 bytes)
  are `QT-INPUT-002`.
- Clock check: Security Status transitions to the core session (`O`) happen at 09:30 US/Eastern. Per file, when the
  median SourceTime of those messages is within 5 minutes of 09:30 + the date's UTC offset and `tz` is
  America/New_York (or of 09:30 and `tz` is UTC), the ingest warns that the records are one clock off and names the
  `tz` to use; when it is near neither, it warns to check the clock. Session dates that are US daylight-saving
  switch days are warned about (SourceTime is placed on a fixed offset from local midnight).
- `nyse_symbol_on_two_channels` counts the symbol-sequenced messages of a selected symbol that arrive on another
  data channel than its previous one on the same day (Symbol Index Mapping and Stock Summary lines write no record
  and are not counted).
- Not visible in the feed: which Participant entered an order (FirmID only on attributed orders, counted
  `nyse_attributed_adds`; NYSE Parity FAQs, 2024: "There is no parity identifier on NYSE feeds"), non-displayed
  orders and the hidden parts of reserve orders (their executions are hidden prints), auction-only interest (it
  shows in Imbalance messages and the crosses only).

## 4.4 CLI

`queuetruth ingest --format nyse-ibf --venue nyse --in EQY_US_NYSE_IBF_3_20260105.csv.gz --out ds`; the `--venue`
help lists `nyse`.

## 6 Oracles (in `crates/ingest/src/nyse/`, fixtures in `tests/fixtures/nyse/`)

- O-NYSE-1 (layouts): every Integrated message type parses to the documented fields; `encode` and `parse_line` round
  trip; malformed fields name their field number; both layouts of 105 and 106.
- O-NYSE-2 (mapping): the exact records of every message type, priority keeping and losing modifies, duplicate ids,
  over-executions, the `PUBLISHER_SPECIFIC` flag of auction allocations, refresh runs (`R` + adds, gaps),
  symbol-sequence gaps and regressions, channel regressions and repeats, the midnight rollover, the day boundary,
  same-price Replace reductions.
- O-NYSE-3 (fixture day, parity): `EQY_US_NYSE_IBF_3_20260105.csv` gives 55 records (46 + 9), 11 prints, 0
  anomalies, verdict `warnings` with the single reason `nyse_parity_ranks_approximate = 2` (`nyse_parity_executions`
  10); the 800-share sell at 25.02 fills 1011 (500), 1010 (50), 1012 (50), 1013 (100), 1012 (100); `--strict` fails
  with `QT-DATA-002`.
- O-NYSE-4 (fixture day, price-time): the NYSE Arca counterpart fills 1010, 1011, 1012 by time and is `clean`.
- O-NYSE-5 (failover): `EQY_US_NYSE_IBF_4_20260105.csv` gives 22 records, one refresh run (`R` at sequence 24), one
  book cleared without a refresh (`QTG`: `R` at sequence 29, before the offer that would have crossed its stale
  bid), one trade cancel and one cross correction (INFO), one symbol-sequence gap (2 messages), one unknown type and
  three feed gaps; its replay has 0 crossed books.
- O-NYSE-6 (determinism): two channels given in either order, raw or gzip, give the same `dataset_id`, partitions
  and data quality; instrument ids do not depend on the symbol filter.
- O-NYSE-7 (allocation model): `synth` reproduces former NYSE Rule 72's parity example in Pillar terms, Setter
  Priority, the odd-lot pointer rules; its committed outputs are current (`QT_BLESS=1`) and their FIFO-rank ground
  truth is pinned (NYSE: 3,678 executions, 2,665 at rank 0; NYSE Arca: 1,982, all at rank 0). The share behind the
  head comes from the generator's parameters and is not an estimate for NYSE.
- O-NYSE-8 (rank oracle): `replay --rank-oracle` on the two synthetic files reports exactly the generator's FIFO-rank
  histogram, and so it does on a large synthetic NYSE day (2,349,888 lines, 1,070,418 executions); every invariant
  0 (measured 2026-10-24; see the guide).
- O-NYSE-9 (properties, `tests::props`): for any generated day of any of the five markets (random seed, 1 to 3
  symbols, 50 to 600 events per symbol, 0 to 4 Floor brokers) the FIFO rank of every execution recomputed from the
  mapped records alone, by an independent FIFO book, equals the generator's truth, with no anomaly and the parity
  executions equal to the parity market's executions; and any mix of valid, truncated and garbage lines (colliding
  order ids, odd prices, sizes, sides and flags, refresh messages and so failovers, other products, unknown types,
  random bytes) maps without a panic to a self-consistent record stream, the end of the file included (every `C`
  and `F` names a live order and never exceeds it, one sequence and one `LAST` per message, the mapper's live-order
  count equal to the stream's). 48 cases each per run; 2,000 each in a one-off run on 2026-10-24, in which at least
  one book was cleared after a failover in 1,811 of the fuzz cases.
- O-NYSE-10 (allocation by date): `Market::allocation_on` is parity for NYSE American before 2017-07-24 and
  price-time from that day; an `EQY_US_MKT_IBF_*` day of 2017 carries `nyse_parity_ranks_approximate` and the NYSE
  MKT warning, the same lines on an NYSE American day of 2026 ingest `clean`.
- O-NYSE-11 (failovers, `mapper::tests`): a failover clears at its next add the books it did not refresh, keeps the
  ones that reference a resting order, ignores Security Status lines and references to unknown orders, clears the
  remaining suspects at the end of the file; the per-symbol spin of XDP Common Client Specification 9.2 is one
  failover (no symbol refreshed early in it is cleared); a symbol refreshed again after live messages starts a new
  failover; refresh messages of symbols outside the filter start one too.
- O-NYSE-12 (labels): an NYSE day without executions carries `nyse_parity_ranks_approximate = 1` and fails
  `--strict`; a renamed file without a market is counted in `nyse_allocation_unknown`; a file without order messages
  in `nyse_files_without_order_messages`.

## 9 Licensing and data guardrails

NYSE posts sample files of this product at `ftp.nyse.com/Historical Data Samples/` (read 2026-10-24). The directory
has no terms file; the product page on www.nyse.com links to it, and the only terms found are ICE's website Terms of
Use, which govern www.nyse.com (and every ICE-operated website on which they appear) and the content available
through it: a licence to download content for personal, non-commercial use, and, without prior written permission,
no adapting, extracting, compiling or creating derivative works from it. NYSE's market data policies also bar
redistributing its market data externally on a historical basis, in any form, without a specific licence (the
Historical Use policy, written for vendors of its real-time data). queuetruth therefore downloads no NYSE file,
ships none and was validated on hand-written and synthetic files only (`validation = SyntheticOnly`). Users with
licensed data (the purchased TAQ product under NYSE's agreements, a vendor feed under the vendor's terms, or NYSE's
written permission for the samples) run the reader on their own files; outputs stay internal unless that licence
says otherwise. The hosted service does not accept `nyse_ibf` (`service/queuetruth_service/api_models.py` is
unchanged; `service/openapi.json` gains only the `nyse` rule-set name). The HTML report lists `nyse_ibf` among the
vendor formats (`queuetruth.report.load.VENDOR_FORMATS`), so a dataset read from a real NYSE file is labelled "Vendor
data; venue rules validated on synthetic data only" rather than "Synthetic or fixture data".

## Open items

- A replay on real NYSE files, to move `validation` from SyntheticOnly: the SymbolSeqNum continuity assumption, the
  SourceTime zone, the 105 / 106 layouts, which message a priority-keeping reduction uses, how a failover shows in
  the files (the channel sequence reset and the re-sent Symbol Index Mapping and Security Status lines of XDP Common
  9.2; whether refreshes of single symbols occur, which the channel-wide failover rule would treat as failovers),
  pre-opening locked or crossed books of NYSE-listed (Tape A) symbols before the opening auction (replay counts them
  as `crossed_books_after_last`) and the overnight session (spec 2.4b adds session state `N`) all need a real file.
- Carry the label further: fold the dataset's non-INFO ingest counters into the partition verdict that sets each
  shadow row's `dq_flags` (`Engine::finish` in `crates/replay/src/engine.rs`), or add a parity flag bit; carry the
  ingest `data_quality` reasons into the zoo, calibration and features summaries (they read only `feed_gaps` today)
  with a one-line terminal warning such as "truth is FIFO-approximate: nyse_parity_ranks_approximate".
- Auction allocations: replay infers shadow fills from non-printable executions by FIFO; a follow-up in
  `crates/replay` can censor shadows at the records flagged `PUBLISHER_SPECIFIC` by this reader.
- Databento XNYS.PILLAR (follow-up for the DBN reader, databento workstream): `--format dbn` resolves a `Venue`, so
  NYSE MBO from Databento runs under the `nasdaq` rules with no parity label. Accept `--venue nyse` for DBN, choose
  the market from the DBN dataset or publisher, and reuse `nyse::rules::Market::allocation_on` and
  `nyse::rules::PARITY_COUNTER` (with the partition rule of `nyse::ingest::allocation_labels`).
- The HTML report's dataset line shows `venue nasdaq` (the record semantics) for NYSE data; show `rule_set` and
  `source_venue` there (report follow-up).
- Parity-aware queue models: the label says FIFO ranks are approximate; an allocation-wheel replay needs the
  Participant of each order, which the feed does not carry. Estimating Participant shares from the executions
  (the rank oracle's histogram, net of a same-day price-time control, is the observable) is a candidate.
- The XDP binary captures (`Real Time Data Samples/`) are not read; only the TAQ CSV product is.
- Integration (files this workstream does not own): add `how-to/nyse-order-level.md` ("Read NYSE order-by-order
  data") and this page to the `docs/mkdocs.yml` nav, and regenerate `docs/reference/` (`docs/gen_reference.py`:
  the CLI page lists the `nyse-ibf` format and the `nyse` venue, the schema page the `nyse_ibf` format); add
  `("nyse-ibf", IngestFormat::NyseIbf)` to the format table of `crates/cli/tests/cli_parse.rs` (and its golden
  argv file); merge the `RuleSet` / `IngestFormat` enum edits in a fixed order with the other workstreams that add
  variants. Proposed short sections: `docs/venue-semantics.md`, "NYSE Group TAQ Integrated Feed (`--format
  nyse-ibf`, venue `nyse`)", from 3.2 and 3.12.3 above; `docs/data-and-licensing.md`, a vendor note from section 9.
- Unrelated issues found on the way: `replay --venue` parses a record `Venue`, so its help text's `crypto_l3` and
  `iex` (and `nyse`) are refused there although `ingest --venue` accepts them; `VENDOR_FORMATS` lists `iex`, but IEX
  datasets carry `source_format = iex_deep_plus` (harmless today, the `iex` rules are `real_data`).
