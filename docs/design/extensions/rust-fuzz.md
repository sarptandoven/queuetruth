# Fuzzing the untrusted-input parsers (spec extension: rust-fuzz)

Status: implemented in `fuzz/` (cargo-fuzz, nightly) with stable regression replay. Proposed spec text for the
integrator; it replaces "fuzz targets are a v0.2 item" in section 8.10 and the "Fuzzing of the parsers (planned)"
line of `docs/testing.md`.

## Proposed text for SPEC 8.10 (input hardening)

The parsers that read user-supplied bytes have libFuzzer targets in `fuzz/` (a standalone Cargo workspace, not a
member of the main one): ITCH 5.0 (framing, `parse`, `encode`, `Mapper`; raw and gzip files; a parser-only target),
IEX DEEP+ (pcap and pcapng, raw and gzip, IEX-TP sequencing, `parse`, `encode`, `Mapper`; a parser-only target), DBN
(`DbnReader` plain and zstd by schema, `Symbology`), `qt_ingest::ingest` end to end for ITCH, IEX DEEP+ and DBN, the
etcsv format end to end, the Parquet format and the dataset reader (a standalone file, an `instruments` file, and
each file of a dataset), the seven `qt_crypto` converters, the exact-decimal JSON reader with the unit and time
parsers, and `qt_crypto::prepare` with `qt_crypto::write` on capture files.

Oracles every target enforces on every input:

1. No panic, abort, sanitizer report or run above 10 s, and no single allocation above 512 MiB (64 MiB for the `dbn`
   and `ingest` targets).
2. An error caused by input bytes is `QT-INPUT-*`, `QT-CONFIG-*` or `QT-DATA-*`; never `QT-ENGINE-001` or
   `QT-USAGE-001`.
3. ITCH and DEEP+ messages round-trip: `parse(encode(parse(b))) == parse(b)`.
4. Mapped ITCH and DEEP+ records: the last record of a message carries `LAST`, every record carries the message's
   sequence number, `ts_recv >= ts_event`.
5. DEEP+ delivers each `(channel, session)` once, in strictly increasing sequence order.
6. A dataset that an ingest path wrote (`ingest` for ITCH, IEX, DBN, etcsv and Parquet; `qt_crypto::write`) opens
   with `Dataset::open` and every record, L2 row and source trade decodes.
7. The crypto session day of any timestamp brackets it: `midnight(d) <= ts < midnight(d + 1)` in UTC and US
   Eastern, compared exactly wherever those midnights fit in `i64` nanoseconds.

Committed seeds (`fuzz/seeds/`) are synthetic or copies of repository test fixtures; real-data excerpts are used only
as local, uncommitted extra seeds. Every finding is minimized into `fuzz/regressions/<target>/` with a regression
test beside the fix; `cargo test` in `fuzz/` replays all seeds and regressions on the stable toolchain.

Arithmetic on values read from input is checked or saturating: counters that sum input-controlled quantities
saturate, and crypto session-date arithmetic is exact over the whole `i64` nanosecond range (a record in a session
day that ends after `i64::MAX` ns, 2262-04-11T23:47:16Z, is `QT-INPUT-002`; a `session_date` / `midnight_utc_ns`
whose day does not fit is `QT-CONFIG-001` naming the field that was set). The pcap / pcapng reader grows its read
buffer with the bytes received, never with a record length a header announces.

## Measured runs

Every target ran for at least 600 s with `fuzz/run.sh` (`-fork=1`, nice 15, `-max_len=65536`, `-timeout=10`,
`-malloc_limit_mb=512`, 64 for `dbn` and `ingest`) on an 8-core laptop in Low Power Mode, shared with other jobs. The
first ten rows ran on the final tree (the `parquet` binary predates only the file naming of its saved reproducers);
the seven converter targets ran in the previous session, and their harnesses
have changed since only in the stricter error oracle (2). Coverage is libFuzzer's edge count at the end of the run.
No run reported an OOM, timeout or crash; the `parquet` target swallowed panics at the five sites listed below.

| target | executions | inputs/s | coverage | seconds |
|---|---|---|---|---|
| `itch` | 400,059 | 661 | 2,265 | 605 |
| `itch_parse` | 6,063,889 | 10,090 | 665 | 601 |
| `iex` | 540,926 | 894 | 2,791 | 605 |
| `iex_parse` | 6,084,241 | 10,073 | 918 | 604 |
| `dbn` | 743,607 | 1,233 | 1,673 | 603 |
| `ingest` | 91,158 | 145 | 18,687 | 627 |
| `etcsv` | 181,967 | 304 | 14,748 | 599 |
| `parquet` | 138,437 | 223 | 16,698 | 620 |
| `crypto_prepare` | 578,390 | 931 | 24,396 | 621 |
| `crypto_json` | 6,560,859 | 10,826 | 1,253 | 606 |
| `crypto_bitstamp` | 1,371,603 | 2,234 | 2,631 | 614 |
| `crypto_bitfinex` | 2,526,694 | 4,108 | 1,771 | 615 |
| `crypto_coinbase` | 1,217,173 | 1,992 | 2,948 | 611 |
| `crypto_coinbase_level3` | 860,634 | 1,397 | 2,559 | 616 |
| `crypto_kraken_l3` | 1,075,094 | 1,748 | 3,163 | 615 |
| `crypto_hyperliquid` | 2,239,184 | 3,659 | 2,812 | 612 |
| `crypto_tardis` | 4,028,045 | 6,603 | 1,554 | 610 |

The parser-only targets run 11 (DEEP+) to 15 (ITCH) times faster than the full ones (`itch` and `iex` spend most of
each input building the mappers' 64 Ki-locate tables), so ITCH and DEEP+ framing and layouts get millions of inputs
per run. The
first, 21 s runs found the three bugs fixed here: the IEX gap-counter overflow, the 16 MiB pcap allocation and the
crypto last-day overflow.

## Known gaps (reported, not fixed here)

Three findings sit in code this workstream does not own. Each has reproducers in `fuzz/known-crashes/<kind>/` (first
byte: the harness selector; strip it to get the file), a filter in the harnesses so fuzzing continues past it, and a
stable test that fails as soon as the fault is gone, so that the inputs move to `fuzz/regressions/` and the filter is
dropped in the same change.

Effect of the two panics, measured with the reproducers: `queuetruth ingest` prints a Python traceback ending in
`pyo3_runtime.PanicException` and exits 1 instead of reporting `QT-INPUT-002`; a service job fails with
`QT-ENGINE-001` ("internal error (PanicException)"), measured by running the ingest runner through
`queuetruth_service.executor.run_child`, the function the worker uses. A crafted upload is reported as an internal
fault; nothing else is affected.

### DBN metadata of 100 to 103 bytes (panic)

The pinned `dbn` 0.71.0 decoder panics on a DBN prelude that announces 100 to 103 metadata bytes.
`DbnFsm::decode_metadata` accepts any length from `METADATA_FIXED_LEN` (100), then reads the u32 schema-definition
length at metadata offset 100, past the end of the metadata slice: `mid > len` at `dbn-0.71.0/src/decode.rs:324`.
Every DBN version (1 to 3) and both encodings (plain, and zstd, which `DbnReader::open` infers from the frame magic)
reach it, on the calling thread, before any record is read. Lengths of 104 and more are bounds-checked by the decoder.

Reproducers: `fuzz/known-crashes/dbn/` `fuzz_metadata_len_100` (the minimized fuzzer find, DBN v3),
`v3_metadata_len_100_zeros`, `v1_metadata_len_103` and `zstd_v3_metadata_len_100`; test
`fuzz/tests/replay.rs::known_dbn_crashes_still_reproduce`; filter `qt_fuzz::dbn_known_metadata_panic`.

Suggested fix in `crates/ingest/src/dbn.rs`: `FrameCheck::observe` already parses the decompressed prelude (plain or
zstd) before the decoder sees it and rejects a metadata length above `MAX_METADATA_BYTES`. One more bound in its
`Frame::Prelude` arm rejects a nonzero length below 104 with `InvalidData`, which becomes `QT-INPUT-002`. The SPEC
text on `FrameCheck` then names the bound: "a nonzero metadata length below 104 bytes (the fixed metadata fields
plus the schema-definition length) or above 256 MiB is `QT-INPUT-002`".

### DBN metadata allocated before it is read

`DbnFsm::decode_prelude` grows its buffer to the announced metadata length + 8 before reading any of it, and
`FrameCheck` accepts lengths up to `MAX_METADATA_BYTES` (256 MiB). A 24-byte plain file (`DBN\x03`, the u32 LE
length 268,435,456, 16 zero bytes) or its 24-byte zstd frame makes one 256 MiB allocation, then fails with
`QT-INPUT-002` ("truncated DBN stream"): the same class as the 16 MiB pcap allocation fixed here. It went unnoticed in
the first runs because the 512 MiB `-malloc_limit_mb` sat above the 256 MiB cap; the `dbn` and `ingest` targets now
run with 64 MiB and skip announced lengths above 16 MiB (`qt_fuzz::dbn_known_metadata_alloc`).

Reproducers: `fuzz/known-crashes/dbn-alloc/` `v3_metadata_len_256mib` and `zstd_v3_metadata_len_256mib`; test
`fuzz/tests/dbn_alloc.rs` (a counting global allocator asserts the 256 MiB request).

Suggested fix in `crates/ingest/src/dbn.rs`: bound the announced length by what can arrive. For a plain file,
`DbnReader::open_with` knows the file size and can reject a metadata length above it before decoding. For zstd, the
decompressed size is unknown up front: `FrameCheck` can hold back the prelude and feed the decoder the metadata only
once it has arrived, or `MAX_METADATA_BYTES` can come down to the size real symbology needs.

### Corrupt Parquet metadata (panic)

Corrupt metadata in a Parquet file panics inside the pinned `arrow-ipc` 59.3.0 and `parquet` 59.3.0 crates: the
`arrow-ipc` sites while `qt_schema::io::open_table` loads the footer (`ArrowReaderMetadata::load`, before any
queuetruth check runs), the `parquet` site when `TableReader::next` reads a row group whose column chunk range is
negative (backtraces taken through the CLI). The `parquet` target found five distinct panic sites in a 600 s run (the
review found the second one by mutating footer bytes):

| site | message |
|---|---|
| `arrow-ipc-59.3.0/src/convert.rs:299` | ``called `Option::unwrap()` on a `None` value`` |
| `arrow-ipc-59.3.0/src/convert.rs:332` | `Int type with bit width of 0 and signed of false not supported` |
| `arrow-ipc-59.3.0/src/convert.rs:354` | `FloatingPoint type with precision of <UNKNOWN 64> not supported` |
| `arrow-ipc-59.3.0/src/convert.rs:514` | `not implemented: Type NONE not supported` |
| `parquet-59.3.0/src/file/metadata/mod.rs:1063` | `column start and length should not be negative` |

The four `arrow-ipc` sites decode the Arrow schema that Parquet writers embed in the footer (`ARROW:schema`). Every
path that reads Parquet reaches them: a `parquet` ingest input, its `instruments` file, and any file of a dataset
directory (`Dataset::open` reads `instruments.parquet`, partitions are read when replayed), so an uploaded dataset
fails the same way in replay jobs.

Reproducers: `fuzz/known-crashes/parquet/`, one per site and named after it, plus the review's
`review_arrow_ipc_convert_332_int_bit_width` (selector 0: a standalone input; 2: a dataset's MBO partition; 3: a
dataset's `instruments.parquet`). They are not minimized: the filter that lets the target run past them also hides
them from `cargo fuzz tmin`. Test `fuzz/tests/replay.rs::known_parquet_crashes_still_reproduce`; filter
`qt_fuzz::parquet_known_panic`, which swallows only panics whose source location is inside the `parquet` or
`arrow-*` crates, so a panic in queuetruth's own Parquet code still fails the target.

Suggested fix in `crates/schema/src/io.rs` (`open_table`, used by `read_table` for every Parquet input and dataset
file): run the metadata load and the batch decoding under `std::panic::catch_unwind` and map a panic to
`QT-INPUT-002` naming the file, or upgrade `parquet` / `arrow` once releases bound these reads. Loading with
`ArrowReaderOptions::with_skip_arrow_metadata(true)` would avoid the four `arrow-ipc` sites, but changes how column
types are inferred, so `fields_match` needs checking against it first. Then move the
reproducers to `fuzz/regressions/parquet/` and drop the filter in `qt_fuzz::parquet`.
