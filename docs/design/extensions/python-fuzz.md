# Extension: parser hardening contract and the fuzz suite

Spec text for the integrator to merge into `docs/design/spec.md` (the live-import input rules, the `dq` dataset
contract and the test strategy). Code: `python/queuetruth/live/_common.py`, `python/queuetruth/live/importers/`
(`_base.py`, `_parquet.py` (new), `_xml.py` (new), `binance.py`, `coinbase.py`, `deribit.py`, `fix44.py`,
`fixdict.py`, `fixwire.py`, `hyperliquid.py`, `ibkr_flex.py`, `kraken.py`, `quickfix.py`, `sim.py`),
`python/queuetruth/dq/` (`data.py`, `book.py`, `feed.py`, `compare.py`). Tests: `python/tests/fuzz/`.

Integrator notes:

- `python/queuetruth/live/_common.py` sits outside the workstream's owned paths (`live/importers/` and `dq/`), but
  it holds the decimal, time, CSV and JSON-lines helpers every importer calls, so the fixes live there rather than
  in eleven copies. Review it as code. No other workstream edits it.
- `dq/data.py` imports the bounded Parquet reader from `live/importers/_parquet.py`, so `dq` and the simulator
  importers share one expansion policy with the gzip reader. A neutral home (`queuetruth/_io.py`) would be cleaner;
  it is outside the owned paths.
- The suite registers a `fuzz` marker from its own conftest. A `make fuzz-py` target for nightly runs
  (`QT_FUZZ_PROFILE=deep QT_FUZZ_EXAMPLES=400 QT_FUZZ_STATS=...`), and the marker in `pyproject.toml`, belong to the
  Makefile and `pyproject.toml`, which this workstream does not own.

## Input contract (every parser an untrusted file reaches)

A parser returns, or raises `QtError` with a `QT-*` code. No other exception, no unbounded time or memory for any
input. In particular:

| Input | Rule | Error |
|---|---|---|
| Decimal fields (JSON numbers and decimal text) | \|x\| < 1e31, at most 40 fractional digits once trailing zeros are dropped (71 digit positions: sums of up to 10^9 values stay exact at 80 digits); zero with any exponent is zero. Float noise such as `1.3877787807814457e-17` (33 fractional digits) is inside; representability at a column's exponent is checked when the value is converted to fixed point | `QT-INPUT-004` |
| Epoch integers | inside one unit window (`s`, `ms`, `us`, `ns`: 2001-09 to 5138) before any integer is built | `QT-INPUT-002` (`QT-INPUT-004` past the decimal range) |
| RFC 3339, `YYYY-MM-DD`, IBKR Flex wall times, FIX UTCTimestamp and `local_ts_regex` times | a real calendar date-time, ASCII digits, UTC offset at most 23:59; second 60 accepted as POSIX time | `QT-INPUT-002` (FIX: the field is not a timestamp) |
| JSON lines | integers past Python's 4300-digit limit and nesting past the recursion limit are invalid lines (also inside a wrapped `msg`); `recv_ns` / `local_ns` within int64 | `QT-INPUT-002` |
| Nested JSON objects and arrays read by an importer | of the documented JSON type; non-object list items are skipped and counted | `QT-INPUT-002` |
| CSV (IBKR Flex, simulators, instrument map) | readable by Python's `csv` reader: a bare CR inside an unquoted field and a field over 131 072 characters are refused | `QT-INPUT-002` naming the file and line |
| FIX integers (BodyLength, data lengths, MsgSeqNum, NewSeqNo, RefSeqNum, store index, sequence files) | 1-20 ASCII digits, otherwise not an integer (framing problem, index mismatch or no number) | none (counted) |
| FileStore index records | sequence number, offset and size non-negative | none (index mismatch) |
| MsgSeqNum gaps | stored as ranges: cost does not grow with the gap's size | none |
| XML (data dictionaries, IBKR Flex) | no document type declaration in any encoding; elements serialisable. A Flex statement is read as UTF-8 whatever its declaration says (as before the hardening); a data dictionary is read in its declared encoding | `QT-INPUT-002` |
| Gzip inputs (QuickFIX) | at most 200x the compressed size plus 64 MiB; a corrupt stream is unreadable | `QT-INPUT-002` / `QT-INPUT-001` |
| Parquet inputs (simulators, `dq`) | decoded size at most 200x the file size plus 16 MiB (simulators, whose values become Python objects) or 64 MiB (`dq`); no nested columns; every Arrow error, invalid UTF-8 in a column name, and temporal values outside Python's date range | `QT-INPUT-001` (unreadable simulator file), `QT-INPUT-002` |
| Simulator options and enums | `size_exp` an integer (or its text) in -18..18; `order_key_map` an object; an hftbacktest enum is a number only when it is 1-18 ASCII digits with an optional minus, otherwise a name | `QT-CONFIG-001` |

The 200x gzip ratio leaves room for real logs: synthetic QuickFIX logs of 20 000 lines compress 11.7x (heartbeats
only, 2.1 MB) and 9.6x (order flow, 4.1 MB) at gzip level 9, and 10.5x / 9.2x at level 6; 200 000 lines give the
same ratios (11.8x and 9.7x). `test_gzip_limit_leaves_room_for_real_logs` pins the level-9 ratios of the 20 000-line
logs. Deflate's ceiling is about 1032x. There is no absolute cap, so a 100 MB file may expand to 20 GB in memory;
the memory limit of the service's import child is the backstop. The reader keeps one buffer (no joined copy).

The Parquet limit counts what decoding costs, not what the file stores: before decoding it checks the footer's
stored size (bounds every page, dictionary pages included) and the row count times the narrowest decoded row (2
million rows of two string columns fit in a 12 KB zstd file); while decoding, it reads string and binary columns
dictionary-encoded and sums the lengths of the entries the indices name before any value is copied (a 4 MiB entry
repeated on 20 000 rows is 84 GB decoded, from a 398-byte file). Both files are `test_bug29_*` cases.

Residual risk (not changed here): the `dq` vintage check probes `manifest.inputs[].path`, the provenance paths of
the raw inputs, which are absolute and outside the dataset by design. It reports whether each path exists and its
size, and with `--verify-sources` hashes it. Run for a tenant (service, MCP), that tells the caller whether a
server path exists and confirms a guessed sha256. The service should run `dq` without source probing, or confine
input paths to a configured raw-input root; partition files are confined (below).

Coverage: the properties reach every parser above. Not fuzzed: the canonical live-log and score readers
(`live/canonical.py`, `score/_io.py`, Parquet written by queuetruth itself), shadow specs (validated by pydantic
models), the Freqtrade and Hummingbot importers (another workstream owns them), and `dq`'s `trades_source` tables
(guarded against unreadable files, column types not validated; `make_dataset` cannot produce them).

## `dq` dataset contract (3.3)

`Dataset` validates on load, so a damaged or hand-edited dataset fails `QT-INPUT-002` naming the field:

- `manifest.json`: standard JSON (no `NaN`); text fields are strings; `partitions[].instrument_id` in uint32,
  `session_date` a date32 day number (an integer, as the engine's `i32` reads it: no ISO string, float or bool),
  `rows` in [0, int64 max], `ts_min` / `ts_max` int64, `files[].path` a relative path of normal components (no
  absolute path, drive, `..`, leading `./` or NUL: the engine's `check_rel_path`, lexical, so a partition directory
  may be a symlink to another volume, as the engine allows), `price_exp_by_instrument` values in -12..0.
- `instruments.parquet`: the columns the checks read cast safely to their `qt.instruments` types;
  `instrument_id` and `session_date` present and non-null; `session_date` within 0001-01-01..9999-12-31 and
  `price_exp` within -12..0 (SPEC 1.2).
- MBO partitions: every `qt.mbo` column present, cast safely to its type, no nulls; only the columns a check
  reads are decoded.
- `data_quality.json`: the scalar counters are JSON numbers (not bools or strings), `source_specific` an object of
  numbers, `feed_gaps` an array of objects, `reasons` an array of strings. Unparseable JSON still counts as absent
  (the `dq_counters` check warns).

Checks: the US-equity sub-penny rule never builds an integer it cannot compare with an int64 price; cross-source
parity rescales prices only when the result fits int64 (`QT-INPUT-004` otherwise: 10^9 at `price_exp` 0 is 10^21 at
-12).

## Test strategy (fuzzing)

`python/tests/fuzz/` holds Hypothesis properties per parser surface and one regression test per bug found
(`test_regressions.py`). Input families per importer: plausible structured records, hostile structured records,
records with exactly one hostile value (every field is reached with the others valid; the first run's hostile
families stopped at the first of about ten bad fields, which is how the hftbacktest enum bug survived), mutated
committed fixtures, and raw bytes; plus typed Parquet columns at the int32 / int64 limits for the simulators and
`dq`, and an input-size property (`test_fuzz_scaling.py`) that times every parser on a repetitive input (a fixture
prefix, then one token repeated: digit runs, separators, tag starts, brackets, quotes) at 16 KiB and 128 KiB and
fails when the larger takes more than 16x the smaller plus 0.25 s (linear is 8x, quadratic 64x). The other
properties use inputs of a few KB, where super-linear behaviour is invisible.

Each example runs under `fuzz._harness.guarded`: a `QT-*` code or a return value, at most 20 s (SIGALRM, with a
faulthandler backstop for C-level hangs), at most 256 MiB of traced Python allocations, and a resident-memory
backstop of 1.5 GiB growth that ends the process (exit status 3) when C code allocates without bound. When a
backstop ends the process, pytest's capture swallows the traceback: rerun the property with `-s`. The default
budget is 40 derandomized examples per property (reproducible in CI: 90 targets, 3 323 examples, 73 s wall for
the directory on the laptop below); `QT_FUZZ_EXAMPLES=N` and
`QT_FUZZ_PROFILE=deep` run longer random campaigns, and `QT_FUZZ_STATS=<path>` writes per-target statistics.
Every test carries the `fuzz` marker; `-m "not fuzz"` deselects the suite.

Deep campaign of the final tree (`QT_FUZZ_PROFILE=deep QT_FUZZ_EXAMPLES=400 QT_FUZZ_STATS=<file>`, one test file at
a time, on the shared 8-core laptop in Low Power Mode). Example time excludes Hypothesis generation and setup:

| File | Targets | Examples | Returned | `QtError` | Example time | Slowest example | Wall |
|---|---:|---:|---:|---:|---:|---:|---:|
| `test_fuzz_venues.py` | 36 | 13 400 | 5 725 | 7 675 | 169 s | 2.0 s | 326 s |
| `test_fuzz_fix.py` | 12 | 9 200 | 8 365 | 835 | 42 s | 0.25 s | 111 s |
| `test_fuzz_flex_sim.py` | 18 | 3 708 | 1 723 | 1 985 | 40 s | 0.21 s | 90 s |
| `test_fuzz_dq.py` | 5 | 1 800 | 1 053 | 747 | 60 s | 0.23 s | 161 s |
| `test_fuzz_imap.py` | 3 | 1 200 | 142 | 1 058 | 2 s | 0.04 s | 10 s |
| `test_fuzz_scaling.py` | 15 | 2 400 | 1 500 | 900 | 56 s | 1.7 s | 62 s |
| total | 89 | 31 708 | | | 369 s | | 760 s |

No property failed after the fixes; peak traced allocations were 22 MiB. The slowest venue example is a 2.6 MB
mutated Hyperliquid file. The first deep `dq` run failed in the test's own dataset editing (a session date at the
int32 limit that a later edit read back), not in `dq`.

Cost of the hardened decimal helpers (`timeit`, best of 5, same laptop): `dec` 0.46 to 0.61 us, `to_fixed` 0.70 to
0.65 us, `dmul` 0.21 to 0.22 us per call. End to end, importing a 30 000-line FIX 4.4 order-flow log (6.3 MB) takes
3.71-3.75 s on main and 3.75-3.85 s here, and 20 000 hftbacktest rows 2.45-2.47 s on main and 2.26-2.48 s here
(two runs each, best of three imports per run): within run-to-run noise.
