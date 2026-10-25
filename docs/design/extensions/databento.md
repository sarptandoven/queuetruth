# Extension: DBN definitions, symbology and the XNAS.ITCH re-issue

Spec text for the integrator to merge into `docs/design/spec.md` (sections 1.2 non-goals, 3.x DBN input, 4.x
`qt.data_quality`, and the `qt_ingest` module list). Code: `crates/ingest/src/dbn.rs`,
`crates/ingest/src/dbn_symbology.rs` (declared from `dbn.rs` with `#[path]`, re-exported as `qt_ingest::dbn::*`;
at merge it can become `crates/ingest/src/dbn/symbology.rs` with a plain `mod symbology;`), wiring in
`crates/ingest/src/ingest_impl.rs` (`ingest_dbn`, `Resolver::compute`, `Templates`) and the INFO list in
`crates/ingest/src/dq.rs`. Tests: `crates/ingest/tests/dbn_definitions.rs`.

Integrator notes: the `ingest_impl.rs` and `dq.rs` edits change behaviour (symbol filter, fail-fast non-FIFO error,
output cleanup on a refused DBN ingest, instrument multiplier and currency, verdict classes), so review them as code,
not as registrations. `crates/testkit/src/refbook.rs` keeps its own copy of the INFO list (3 names); have it call
`qt_ingest::dq::is_info_source_counter`. The shared `CARGO_TARGET_DIR` can serve a stale `qt_ingest` to another
worktree's tests: re-run clippy and the ingest tests after touching `crates/ingest` sources.

## Module list (`qt_ingest`)

```text
pub mod dbn;     // DBN MBO reader (dbn crate), multi-file chronological, zstd; records decoded as stored
                 // (VersionUpgradePolicy::AsIs) after a framing check; DbnDefinition (every definition field the
                 // ingest uses, DBN v1-v3), DbnReader::{next_definition_record, scan_symbology, gateway_errors};
                 // Symbology (dated metadata mappings, aliases, definitions, symbol mappings; check,
                 // unverified_xnas_vintage, non_fifo, non_fifo_error, check_fifo, stats)
```

## DBN symbology (3.x, `--format dbn`)

Instrument ids are resolved per (id, UTC date), never by id alone. Sources, in lookup order:

1. **Metadata mappings** (`stype_out = instrument_id`; UTC dates, end exclusive). A mapping is keyed by the query's
   input symbol. Keys are raw symbols when `stype_in = raw_symbol`, or when `stype_in` is unset (mixed) and the key
   does not have the shape of a parent (`ROOT.FUT`, `ROOT.OPT`, `ROOT.SPOT`, `ROOT.MLEG`) or continuous
   (`ROOT.{c,n,v}.RANK`) symbol. Other keys (parent, continuous, instrument-id or other input symbologies) are
   **aliases**.
2. **Definition records**, from `definition` files and from mixed-schema files (`schema` unset, for example a saved
   live session; such files are read twice: once for definitions and symbol mappings, once for MBO). A definition
   applies from the UTC date of `ts_recv` (`ts_event` when `ts_recv` is undefined; neither: counted as
   `dbn_definitions_undated`, not applied) until a later one of the same id supersedes it. Every record is kept as a
   receipt in its id's timeline, in the total order (receive time, content, source label), so the result does not
   depend on the order of the inputs. A receipt identical to the one before it in that order (a midnight snapshot)
   is counted in `dbn_definition_repeats`. A delete (`security_update_action = D`) is counted
   (`dbn_definition_deletes`) and supplies nothing. `security_update_action` is read as a raw byte (DBN v1 declares
   it as an enum); bytes other than `A`, `M`, `D` read as `?`. `raw_instrument_id` 0, `i32::MAX` (the SBE `Int32`
   null that Databento's old-vintage definitions carry), `u32::MAX` and `u64::MAX` are undefined.
3. **Symbol-mapping records** (live): the output symbol is the raw symbol when `stype_out = raw_symbol` (DBN v1
   records always), else the input symbol when `stype_in = raw_symbol`; the other symbol is an alias. Valid from
   `start_ts` (else `ts_event`).

Lookups: the raw symbol of (id, date) is the raw metadata interval holding the date, else the latest definition or
symbol mapping at or before the date that names one, else a continuous alias (`ROOT.{c,n,v}.RANK`) that maps to this
id alone on that date (never a parent, even with one child); without any, the partition is labelled by its id
(`instruments_without_symbol`). `id_at(symbol, date)` (used to file an after-hours record under the session date's
id) never uses aliases. The tick is the latest definition of the partition's id whose symbol matches (or names none)
with a defined positive `min_price_increment`, valid on the session date, else of any date, else the latest
definition of the symbol under any id; it is rescaled exactly to `price_exp`; `tick_size` overrides it. The
instrument row's `contract_multiplier` is `min_price_increment_amount / min_price_increment` of the latest
definition (same lookup by id) that defines both as positive values (ES 50, ZN 1000), else 1; its `currency` is the
latest definition's non-empty `currency`, else `USD`. Round lot and display factor are not taken from definitions
(lot size stays `lot_size`, default 1). The symbol filter (`symbols`) matches the raw symbol or any alias of the
record's (id, UTC date): metadata alias intervals holding the date, and every alias of the symbol mappings at or
before the date since the id's raw symbol last changed.

## Input consistency (`QT-INPUT-002`)

After every input's metadata and definitions are read and before any record is written, the ingest refuses:

- inputs whose metadata names more than one Databento dataset (non-empty `dataset`), naming each with a file;
- for `XNAS.ITCH`, definitions received before 2023-11-28 of both vintages of Databento's 2026-10-31 re-issue, on any
  dates: one with `raw_instrument_id` undefined (the old vintage) and one with `raw_instrument_id` defined and
  different from its instrument id (the re-issue); the message quotes the first of each by (id, receive time);
- on one UTC date, a raw symbol mapped to two instrument ids or an id mapped to two raw symbols by the metadata of
  the inputs (overlapping intervals that disagree);
- a definition or symbol mapping whose (symbol, id) disagrees with the raw metadata on a UTC date it was received on.
  A record received before 01:00 UTC is also accepted when it agrees with the previous date's metadata (the last
  after-hours hour of the previous Eastern session, Databento's 2026-10-31 XNAS.ITCH re-issue); mere absence of
  metadata on the previous date does not count as agreement.

The message names up to five conflicts with their files and the total, and explains the likely cause: for `XNAS.ITCH`
with a conflict before 2023-11-28, two vintages of the same history (the re-issue gives data before 2023-11-28 new
instrument ids and keeps the Nasdaq stock locate in `raw_instrument_id`, quoted when defined) or two queries;
otherwise two different queries or downloads.

After the MBO pass, `XNAS.ITCH` partitions before 2023-11-28 whose symbol comes from definitions, symbol mappings or a
`definition` file's metadata, while no data file's (MBO, trades or mixed-schema) metadata maps that id to that
symbol on that date, are counted in `dbn_xnas_vintage_unverified` (a verdict counter) with a warning naming up to
five: their vintage cannot be checked against the definitions'. Partitions labelled by their id are not counted.
Not detected: inputs whose dates are each of one vintage, with no old-vintage definitions (or with MBO metadata
mapping every date); each date's symbols are then right.

A refused DBN ingest (any error after the inputs are opened) removes what it wrote: `out` itself when it did not
exist, its contents when it existed empty. A non-empty `out` is refused up front and never touched.

## Matching algorithm (1.2 non-goal "Pro-rata books are rejected at ingest")

At the first selected record of a (session date, instrument id) partition whose definition (same lookup as the tick,
by id) names a `match_algorithm` other than `F` (FIFO), the ingest fails with `QT-CONFIG-001` before the bulk of the
data is written, naming the partition as `SYMBOL (id N, YYYY-MM-DD: algorithm name 'X')` and up to ten other
instruments the symbol filter keeps whose latest definition is not FIFO either, and telling the user to list the
FIFO contracts explicitly in `symbols`. An undefined algorithm (space or zero byte) passes, as do partitions without
a definition. Algorithm names follow `dbn::MatchAlgorithm` (including `A`, allocation); other bytes are `unknown`.

## `qt.data_quality.source_specific`

New INFO counters (`qt_ingest::dq::INFO_SOURCE_COUNTERS`, never part of the verdict): `dbn_definition_repeats`,
`dbn_definition_deletes`, `dbn_definitions_without_tick`, `dbn_symbol_mapping_records`,
`dbn_alias_mapping_intervals`, and `dbn_mixed_schema_other_records` (records of other schemas in a mixed-schema file,
heartbeats and acknowledgements included). New verdict counters: `dbn_definitions_undated`,
`dbn_xnas_vintage_unverified`, and `dbn_gateway_error_records` (`ErrorMsg` records and `SystemMsg` slow-reader
warnings, in any file). `dbn_other_records_skipped` stays a verdict counter and now counts only records of another
schema in an `mbo` or `trades` file. (The testkit reference verdict in `crates/testkit/src/refbook.rs` lists only the
ITCH and DBN-remap INFO counters; see the integrator notes.)

## Measured

- Databento's definition test files (`databento/dbn` commit `d368005a`, `test_data.definition.dbn` (DBN v2) and
  `test_data.definition.v{1,2,3}.dbn.zst`) decode to the same two `XNAS.ITCH` MSFT records (ids 6819 on 2021-10-04
  and 6830 on 2021-10-05): `min_price_increment` undefined, round lot 100, class `K`, match algorithm `F`, exchange
  `XNAS`; with a synthetic MSFT MBO day under id 6819 the ingest is clean with symbol MSFT and the Nasdaq $0.01 tick
  (`dbn_definitions::databento_definition_test_files`, opt-in, `QT_DBN_DEFINITION_SAMPLES`).
- Databento's five MBO test files still ingest and replay with verdict `clean` and identical decoded rows
  (`scripts/realdata/dbn_samples.py --offline`).
- Symbology at full-market scale (`dbn_definitions::bench_symbology_check_full_market_year`, release build, run under
  `/usr/bin/time -l`): 10,000 symbols with a new id on each of 252 UTC dates in two files' metadata (2.52 million
  intervals each) and 5.04 million definitions (half of them midnight repeats) built in 14.1 and 14.6 s and checked
  in 10.1 and 12.8 s over two runs, with a peak resident set of 1.35 and 1.51 GB, on the shared 8-core laptop in
  Low Power Mode with a load average near 225 from other jobs (an unloaded machine should be faster). Definition
  contents are interned and each record is a 16-byte receipt; the metadata intervals dominate the memory. It is
  small next to ingesting the MBO records of a full-market year.
- Input order: the same MBO file and three daily definition files (an id re-used by another contract on the middle
  date, no raw metadata) give byte-identical `data_quality.json` and `instruments.parquet` in all 24 input orders
  (`dbn_definitions::f_inputs_in_any_order_give_the_same_dataset`).

## Not covered

- DBN v4 is still refused (`QT-INPUT-002`, unchanged): the pinned `dbn` 0.71 crate reads versions 1 to 3.
- `status` schema files are still refused as inputs (`unsupported DBN schema status`); status records inside
  mixed-schema files are skipped and counted (`dbn_mixed_schema_other_records`).
- No opt-in drops non-FIFO partitions instead of failing: it needs an `IngestSpec` field and a CLI flag, which this
  workstream does not own. The remedy is an explicit `symbols` list.
- No `IngestSpec` override of the contract multiplier or currency exists; without definitions they stay 1 and `USD`.
- No real Databento CME or re-issued XNAS.ITCH bytes were available (no purchases): the re-issue behaviour is tested
  on synthetic DBN files built from Databento's announcement; the DBN/CME validation label stays `synthetic_only`.
