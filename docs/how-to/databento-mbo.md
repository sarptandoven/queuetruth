# Ingest Databento MBO for CME and Nasdaq

**Goal:** turn Databento's order-by-order (MBO) history of a CME future (`GLBX.MDP3`) or a Nasdaq stock
(`XNAS.ITCH`) into a queuetruth dataset with the right symbols and ticks, know what it will cost before you pay for
anything, and avoid the traps of Databento's symbology and of its 2026-10-31 Nasdaq re-issue.

**You need:** a Databento account and API key of your own (queuetruth never asks for, reads or stores it), the
`databento` Python client in any environment, and queuetruth. Steps 1 and 2 are free. Step 3 is the only step that
spends money, and it is yours to take: queuetruth never downloads vendor data.

Databento licenses its data for your internal use. Publishing results built on it has conditions (attribution, and a
CME derived-data agreement for external derivative works); read [Data and licensing](../data-and-licensing.md) before
you share anything.

## 1. Pick the dataset, schemas and symbols

| | CME futures | Nasdaq stocks |
|---|---|---|
| Databento dataset | `GLBX.MDP3` | `XNAS.ITCH` |
| queuetruth venue | `--venue cme` | `--venue nasdaq` |
| Session date | the UTC date (default `--tz UTC`) | the US/Eastern date (default `--tz America/New_York`) |
| Tick without a definition | none: a 1-unit tick and a warning | $0.01 (the venue default) |

Download two schemas for the same symbols and the same time range, on the same day:

- **`mbo`** (required): the order-by-order records queuetruth replays.
- **`definition`** (strongly recommended): one record per instrument and UTC date with the raw symbol, the tick
  (`min_price_increment`), the tick's value and currency, and the matching algorithm. Without it a CME ingest has no
  tick (see step 5). Request it for every whole UTC date the MBO covers, from 00:00 UTC, even when the MBO window is
  shorter: a partial-day request can miss an instrument's only definition record of that date.
- **`trades`** (optional): source prints, kept as `source_trades` next to the MBO partitions.

Ask for symbols with `stype_in="raw_symbol"` (for example `ESZ6`, `AAPL`) when you can: the MBO file's metadata
then maps each instrument id to its symbol on each date, which is also what lets queuetruth check that the MBO and
definition files agree (step 6). Parent (`ES.FUT`) and continuous (`ES.c.0`) requests also work, but their metadata
is keyed by the requested symbol, not by the contract: queuetruth keeps those keys as aliases (step 4), so add the
`definition` file to get the contract names. `ALL_SYMBOLS` requests carry no symbol mappings at all; the
`definition` file names their instruments.

**The time window.** For CME a whole UTC day is one session date. A Nasdaq session date is a US/Eastern date whose
after-hours end at 20:00 ET, which is 01:00 UTC on the next date during US standard time (EST) and 00:00 UTC during
daylight time (EDT). In EST a whole-UTC-day Nasdaq request therefore starts with the last after-hours hour of the
previous session (a one-hour stub partition) and misses its own session's last hour. Request the MBO of a Nasdaq
session date D over:

| D is in | MBO window |
|---|---|
| EST (November to March) | `start="D T05:00"`, `end="D+1 T01:00"` (UTC) |
| EDT (March to November) | `start="D T04:00"`, `end="D+1 T00:00"` (UTC) |

Both windows are whole multiples of 10 minutes, so the cost estimate stays exact (step 2). Request the definitions
of a Nasdaq day as the whole UTC dates D and D+1.

## 2. Estimate the cost (free)

Databento bills only for time-series data; metadata, symbology and cost estimates are free
([metered pricing](https://databento.com/docs/api-historical/basics/metered-pricing)). Install the client in its
own environment (it is not a queuetruth dependency), export your key as `DATABENTO_API_KEY`, and ask what a request
would cost before you make it:

<!-- check-docs: skip (needs the databento client and the reader's own API key) -->
```python
import databento as db

client = db.Historical()  # reads DATABENTO_API_KEY

query = dict(
    dataset="GLBX.MDP3",
    symbols=["ESZ6"],
    stype_in="raw_symbol",
    start="2026-10-20",
    end="2026-10-21",
)
print(client.metadata.get_dataset_range(dataset=query["dataset"]))  # first and last available times
for schema in ("mbo", "definition", "trades"):
    cost = client.metadata.get_cost(schema=schema, **query)  # US dollars
    size = client.metadata.get_billable_size(schema=schema, **query)  # uncompressed bytes
    count = client.metadata.get_record_count(schema=schema, **query)
    print(f"{schema:<10} ${cost:,.2f}  {size / 1e9:,.3f} GB  {count:,} records")
```

For Nasdaq use `dataset="XNAS.ITCH"` and `symbols=["AAPL", "MSFT"]`. Things to know about the numbers:

- `get_cost` can over-report for time ranges that are not whole multiples of 10 minutes, and the bill follows the
  bytes actually sent. Use whole UTC days for CME, the window of step 1 for Nasdaq MBO, and whole UTC days for
  definitions (Databento's estimate for definitions is only exact for whole 24-hour ranges).
- MBO dominates. In queuetruth's own real-data run on Nasdaq's ITCH file of 2019-12-30 (a quiet holiday-week day),
  AAPL had 1.74 million MBO records and SPY 2.34 million; definitions are a few records per instrument and day.
- Repeated streaming requests are billed each time. If you will read the same range more than once, a batch job
  (`client.batch.submit_job`) is billed once and can be downloaded again for 30 days.
- A cheap first check: the same estimate for 2019-12-30 or 2020-01-30 on `XNAS.ITCH`, with the EST window of step 1
  (for 2019-12-30: `start="2019-12-30T05:00"`, `end="2019-12-31T01:00"`). Nasdaq publishes those two days of
  TotalView-ITCH for free, so a Databento copy of them can be compared record by record with Nasdaq's own file
  (step 6).

## 3. Download (billed: your decision)

When the estimate is acceptable, stream each schema to a file. Keep the files out of any repository you share.

<!-- check-docs: skip (spends money on the reader's Databento account) -->
```python
import databento as db

client = db.Historical()
query = dict(dataset="GLBX.MDP3", symbols=["ESZ6"], stype_in="raw_symbol", start="2026-10-20", end="2026-10-21")
for schema in ("mbo", "definition"):
    client.timeseries.get_range(schema=schema, path=f"glbx-esz6-20261020.{schema}.dbn.zst", **query)

# Nasdaq session 2026-01-14 (EST): the MBO over the session window, the definitions over whole UTC dates.
nasdaq = dict(dataset="XNAS.ITCH", symbols=["AAPL", "MSFT"], stype_in="raw_symbol")
client.timeseries.get_range(
    schema="mbo", start="2026-01-14T05:00", end="2026-01-15T01:00",
    path="xnas-aapl-msft-20260114.mbo.dbn.zst", **nasdaq,
)
client.timeseries.get_range(
    schema="definition", start="2026-01-14", end="2026-01-16",
    path="xnas-aapl-msft-20260114.definition.dbn.zst", **nasdaq,
)
```

Write down, next to the files, the dataset, the download date and the gateway you used. Vendors re-issue history
(step 6), and queuetruth records only each input's path, size and sha256 in the dataset's `manifest.json`.

## 4. Ingest

Pass every file of one query as `--in`. The order does not matter: MBO files are replayed in metadata start order,
and definitions are kept in one timeline per instrument id ordered by receive time, so any order of the same files
gives the same dataset.

<!-- check-docs: skip (needs Databento files the reader downloaded) -->
```bash
queuetruth ingest --format dbn --venue cme \
  --in glbx-esz6-20261020.mbo.dbn.zst --in glbx-esz6-20261020.definition.dbn.zst \
  --out ds/esz6-20261020
queuetruth ingest --format dbn --venue nasdaq \
  --in xnas-aapl-msft-20260114.mbo.dbn.zst --in xnas-aapl-msft-20260114.definition.dbn.zst \
  --symbols AAPL,MSFT --out ds/xnas-20260114
```

Plain `.dbn` and zstd `.dbn.zst` files of DBN versions 1 to 3 are read as stored. What queuetruth takes from the
inputs, per instrument id and UTC date:

| Source | What it supplies |
|---|---|
| metadata mappings of a `raw_symbol` query | the raw symbol of each instrument id on each date |
| metadata mappings of a parent, continuous or other query | an **alias**: it selects the instrument in `--symbols`, and names it only when no definition does and the alias maps to that one id on that date (a continuous symbol, never a parent with several contracts) |
| `definition` records (a `definition` file, or definition records inside a mixed-schema file such as a saved live session) | the raw symbol; the tick from `min_price_increment` (rescaled exactly to `--price-exp`); the contract multiplier, `min_price_increment_amount / min_price_increment` (ES: $12.50 / 0.25 = 50; ZN: $15.625 / (1/64) = 1000); the currency; the matching algorithm (non-FIFO books are rejected, step 5) |
| symbol-mapping records (saved live sessions) | the raw symbol (the output symbol of a `raw_symbol` mapping) and the requested symbol as an alias |

A definition applies from the UTC date of its `ts_recv` until a later one replaces it; a delete (`D`) and a record
without a defined, positive `min_price_increment` leave the earlier tick in force. `--tick-size` overrides every
definition. The contract multiplier is set only when a definition gives both the tick and its value; otherwise it
stays 1 and currency markouts (`mo_ccy`) are per price point, not in money. Without a definition the currency stays
USD. queuetruth does not take the round lot or display factor from definitions: the dataset's lot size stays
`--lot-size` (default 1, because odd lots trade).

`--symbols` matches a raw symbol or an alias, case-insensitively, so `--symbols ES.FUT` keeps every contract of a
parent request and `--symbols ESZ6` keeps one.

## 5. Read the warnings, errors and counters

`queuetruth ingest` prints warnings on stderr and keeps them in `data_quality.json` (`reasons`, prefixed
`warning:`), and the counters in `data_quality.source_specific`. An error stops the ingest before it writes
`manifest.json` and removes what it wrote to `--out` (an `--out` that was not empty is refused up front and never
touched), so you can fix the inputs and rerun with the same `--out`. Those specific to Databento input:

| You see | Meaning | What to do |
|---|---|---|
| warning `tick_size defaulted to 1 price unit ... can effectively hang on futures prices`; counter `instruments_default_tick_size` | no `--tick-size` and no definition with a tick for that instrument (a CME ingest without the `definition` file) | add the `definition` file of the same query, or pass `--tick-size` |
| `QT-CONFIG-001 ... is not matched by price-time priority (DBN definition match_algorithm)` | a product matched pro-rata, with market-maker allocations or another non-FIFO rule. queuetruth replays FIFO books only and rejects the others at ingest, at the first record of such an instrument. The message names it and the other selected instruments whose definitions are not FIFO | `--symbols` keeps only what it lists, so list the FIFO contracts explicitly (for example `--symbols ZNZ6,ZNH7` instead of the parent `ZN.FUT`) |
| `QT-INPUT-002 ... inputs disagree on the instrument symbology` | on one UTC date, a symbol has two instrument ids or an id has two symbols across the inputs (two vintages of the same history, or files of different queries) | download the MBO and definition files together, from one query and one vintage |
| `QT-INPUT-002 ... XNAS.ITCH definitions before 2023-11-28 come from both vintages` | definitions from before and after Databento's 2026-10-31 re-issue (step 6), on any dates | keep each vintage in its own folder and ingest it with its own MBO files |
| warning `... XNAS.ITCH partition(s) before 2023-11-28 take their symbol from definitions that no MBO file's metadata confirms`; counter `dbn_xnas_vintage_unverified` (verdict `warnings`) | the MBO metadata maps no raw symbol (an `ALL_SYMBOLS`, parent or `instrument_id` request), so queuetruth cannot check that the definitions are of the MBO's vintage (step 6) | request the MBO with `stype_in="raw_symbol"`, or make sure both files were downloaded on the same side of 2026-10-31 |
| `QT-INPUT-002 ... inputs come from 2 Databento datasets` | for example `GLBX.MDP3` definitions with `XNAS.ITCH` MBO | ingest each dataset separately |
| `QT-INPUT-002 ... unsupported DBN version 4` | a file newer than the reader's `dbn` crate (versions 1 to 3) | re-encode the file as DBN v3, or use a queuetruth release that reads the newer version |
| counters `dbn_definition_repeats`, `dbn_definition_deletes`, `dbn_definitions_without_tick`, `dbn_symbol_mapping_records`, `dbn_alias_mapping_intervals`, `dbn_mixed_schema_other_records` | definitions that repeated the one before them (midnight snapshots), deletes, definitions without a tick, live symbol mappings read, parent or continuous mapping intervals kept as aliases, records of other schemas in a mixed-schema file (definitions, mappings, status, statistics, heartbeats, acknowledgements) | none: these never change the data-quality verdict; `dbn_definitions_without_tick` is expected on `XNAS.ITCH` |
| counter `dbn_definitions_undated` (verdict `warnings`) | definitions with neither `ts_recv` nor `ts_event`; they are not applied | check the file; re-download it if it came from Databento |
| counter `instruments_without_symbol` (verdict `warnings`) | partitions labelled by their numeric id | add the `definition` file of the same query |
| counter `dbn_other_records_skipped` (verdict `warnings`) | records of another schema inside an `mbo` or `trades` file, which Databento does not send | check where the file came from |
| counter `dbn_gateway_error_records` (verdict `warnings`) | error records and slow-reader warnings in a saved live session: a symbol that did not resolve, or records the gateway may have dropped | check the capture; record it again if records may be missing |
| warning `tick_size defaulted ...` although you added the `definition` file | the definition request missed the instrument (a partial-day window, or another query) | request the definitions for whole UTC dates with the same symbols (step 1) |

Then run the data-quality checks on the dataset ([Check a feed](check-a-feed.md)):

<!-- check-docs: skip (needs a dataset ingested from the reader's Databento files) -->
```bash
queuetruth dq --data ds/esz6-20261020 --out dq/esz6-20261020
```

## 6. Nasdaq `XNAS.ITCH` and the 2026-10-31 re-issue

On 2026-10-31 Databento replaces its whole `XNAS.ITCH` history
([announcement](https://databento.com/blog/nasdaq-historical-data-changes-2026-09)); live data does not change.
Three changes matter to a replay, and this is how queuetruth handles each:

| Change | queuetruth |
|---|---|
| Data before 2023-11-28 gets Databento's synthetic instrument ids instead of Nasdaq's daily stock locate; the locate stays in the definitions' `raw_instrument_id` | ids are always resolved per (id, UTC date), never joined across dates. Mixes are refused where they can be seen: definitions of both vintages (the old vintage leaves `raw_instrument_id` undefined, the re-issue sets it to the locate), and MBO metadata that disagrees with the definitions on a date (a symbol with two ids or an id with two symbols), naming the files, the symbol, the date and the `raw_instrument_id`. When the MBO metadata maps no raw symbol, the pairing cannot be checked and the ingest warns (`dbn_xnas_vintage_unverified`) |
| After-hours during US standard time run to 01:00 UTC instead of stopping at 00:00 UTC, so each UTC date starts with the last hour of the previous session | records up to 01:00 UTC stay on their US/Eastern session date (past `close_s`, so outside shadow placement, but kept in the book); a record whose id was re-assigned between the two UTC dates is filed under the session date's id. Download Nasdaq MBO with the session window of step 1 to get each session whole |
| MBO, definition and status files gain snapshots at 00:00 UTC (Tuesday to Saturday in EST, Tuesday to Friday in EDT) | MBO snapshots (a clear `R` followed by `SNAPSHOT`-flagged adds) are routed by the snapshot time, kept in feed order and counted in `snapshot_records`, not as anomalies; definition snapshots that repeat a definition are kept once (`dbn_definition_repeats`); a definition received before 01:00 UTC may still carry the previous date's id |

The `XNAS.ITCH` definitions we could inspect carry no tick (Databento's own definition test file, MSFT on
2021-10-04 and 2021-10-05, leaves `min_price_increment` undefined), so the Nasdaq venue's $0.01 applies without a
warning. Stocks on another grid need `--tick-size` and an ingest of their own: sub-dollar stocks quote in $0.0001, and
the SEC's 2024 amendments to Reg NMS Rule 612 give qualifying stocks priced at $1 or more a $0.005 tick from their
compliance date, which SEC exemptive relief has postponed more than once. Check the current compliance date against
the dates you replay.

Not detected: MBO and definitions of one vintage on some dates and of the other vintage on other dates, without any
definitions of the old vintage (or with MBO metadata that maps every date). Each date's symbols are then right, but
the dates differ in their after-hours hour and midnight snapshots. Keep the vintages apart anyway.

What to do:

- Re-download, do not patch. Files you bought before 2026-10-31 are the old vintage; files downloaded on or after it
  are the new one. Keep them in separate folders, never in one ingest or one comparison, and re-run calibrations on
  the new files. This matters most for `ALL_SYMBOLS` downloads, whose MBO metadata cannot confirm the vintage.
- To see what changed on a day you already hold, ingest both vintages separately and compare them:
  `queuetruth dq --data ds/old --compare ds/new --out dq/old-vs-new`. Until 2026-10-31 Databento serves the re-issued
  data from a preview gateway (`db.Historical(gateway="hist-preview.databento.com")`), billed like the normal one, so
  estimate its cost first as in step 2. On an EST date expect the new vintage to have the session's 19:00 to 20:00 ET
  hour and the midnight snapshots that the old one lacks; with whole-UTC-day files, expect also a one-hour partition
  of the previous session date in the new vintage only. Neither is an error.
- To check Databento's copy against the exchange's own bytes, use one of the two free Nasdaq ITCH days:
  `queuetruth ingest --format itch --in 12302019.NASDAQ_ITCH50.gz --venue nasdaq --symbols AAPL,MSFT --out itch`,
  ingest the Databento MBO of 2019-12-30 downloaded with the EST window of step 1 for the same symbols as `dbn`, then
  `queuetruth dq --data itch --compare dbn --out dq-itch-vs-dbn`. A whole-UTC-day file of 2019-12-30 would show
  the 19:00 to 20:00 ET hour as missing and a partition of 2019-12-27 as extra.

## 7. CME `GLBX.MDP3` notes

- **Session date and snapshots.** The default session time zone for `cme` is UTC, so a session date is a UTC date.
  Databento inserts a full book snapshot at 00:00 UTC on weekdays; it is read as on Nasdaq.
- **Matching algorithm.** The equity-index outrights (ES, NQ) match FIFO. Other products, for example many
  interest-rate futures and options, use pro-rata or allocation rules, which queuetruth does not model. Their
  definitions say so in `match_algorithm`, and the ingest rejects them (step 5). Without the `definition` file
  queuetruth cannot tell, so check the product before you replay it. A parent request (`ZN.FUT`) can include spreads
  matched differently from the outrights; list the outrights you want with `--symbols`.
- **Contract value.** With the `definition` file each instrument row carries its contract multiplier and currency, so
  currency markouts are in dollars per contract.
- **Parent and continuous requests.** `ES.FUT` maps to every contract and spread of the product and `ES.c.0` to the
  front contract of each date; with the `definition` file each partition carries its contract symbol (`ESZ6`).
- **Implied liquidity** is not in MBO; implied trades arrive as side-`N` prints. See
  [Venue semantics](../venue-semantics.md) for the CME record and priority rules.

## Troubleshooting

- **Every partition is named by a number.** The query was not `raw_symbol` and no `definition` file was given. Add
  the `definition` file of the same query.
- **`--symbols ESZ6` selects nothing.** The symbol is resolved per date: check that the MBO or definition files map
  `ESZ6` on the dates you ingest. `queuetruth --verbose ingest ...` prints every counter and warning.
- **A CME ingest is slow downstream (zoo, calibrate, features).** The tick defaulted to one price unit; see step 5.
- **The ingest refuses files you downloaded weeks apart.** That is the vintage check of step 6: the same query gives
  different ids before and after the re-issue.
- **A Nasdaq dataset has a short partition dated the day before.** The MBO was downloaded as whole UTC days; see the
  time window in step 1.
