# How queuetruth is tested

Rule: no claim in these docs ships without a test that would fail if the claim were false. Each test has a stable id
that appears in its function name (`fn o_book_1_...`, `def test_o_stat_1_...`), so you can find it with `grep -ri
o_book_1`. The status of every oracle in a release is the result of that release's gate run; this page lists what
each one checks, against what, and what it does not cover.

## Layers

1. **Hand-computed scenarios.** Small books written out by hand with every expected value stated in the
   specification before the code existed: 16 shadow scenarios plus two from the audit (S17, S18), golden values for
   the queue models, golden values for the statistics.
2. **Differential tests against independent implementations.** A deliberately naive reference book and a naive
   reference shadow engine written from the specification, not from the production code; a line-by-line
   transliteration of hftbacktest's queue code; statsmodels, scipy, lifelines and R as references for the
   statistics; an independent matching-engine oracle for the shadow rules.
3. **Property tests.** Random streams and random orders (proptest in Rust, Hypothesis in Python) checking invariants
   that must hold for any input.
4. **Real-data checks.** Two public Nasdaq ITCH days, compared with the predecessor engine's outputs and with the
   feed's own execution prints. Only aggregates are published.
5. **Interface parity and determinism.** The same spec through the Python API, the CLI, the standalone binary and
   the HTTP service gives identical summaries and output file hashes, across runs and thread counts.

## Oracle list

The release's gate run (`make gate-3`) runs every oracle below; a test that cannot run on a machine (for example
without Docker or the separate hftbacktest venv) is skipped with its reason printed. 0.1.0 is released only after
that gate and CI are green on the release commit.

### Book and replay

| Id | What it checks | Reference |
|---|---|---|
| O-BOOK-1 | After every record of at least 2,000 random streams (up to 3,000 records, venues nasdaq, cme, generic) the book equals a naive reference book: BBO, every level, FIFO order, queue ahead of every order. | `qt_book::reference::RefBook` |
| O-BOOK-2 | The book's internal audit passes after every record of O-BOOK-1. | `Book::audit` |
| O-BOOK-3 | Invariant counters are zero on generated streams and equal the injected count on corrupted ones. | `qt_testkit` generator |
| O-BOOK-4 | Every test of the predecessor engine's book is ported and passes, including the modify-priority truth table (with the CME same-size modify rows of audit A6) and sweep grouping. | predecessor tests |
| O-PAR-1 | BBO and derived trades equal the generator's expected output; change-only BBO equals the full stream without consecutive duplicates. | generator truth |
| O-PAR-2 | DBN round trip: encode, ingest, replay equals replaying the events directly. | generated DBN |
| O-PAR-3 | ITCH: per-message byte fixtures built from the ITCH 5.0 layouts parse correctly; a synthetic ITCH day ingests to the expected records. | ITCH 5.0 specification |
| O-PAR-4 | et CSV to Parquet ingest replays identically to the CSV records. | generated CSV |
| O-DQ-1 | Data-quality verdict rules on constructed counters. | hand-built |

### Shadows

| Id | What it checks | Reference |
|---|---|---|
| O-SH-1 | Scenarios S1 to S16 (plus S17, S18) exactly, every column of results, fills and trajectories, under both fill rules where they differ. | hand-computed |
| O-SH-2 | At least 1,000 random cases against the independent naive shadow engine (abs and touch prices, latency with jitter, cancels, amends). | `qt_testkit::reference_shadows` |
| O-SH-3 | Properties: fills sum to the filled quantity; queue ahead never increases; adding shadows changes no other output byte. | proptest |
| O-SH-4 | Mirror shadows never fill when the real order did not, and never before its first fill. | the data itself |
| O-SH-5 | The `exact` rule equals an independent counterfactual matching engine on the audit scenarios, the referee fixtures and the synthetic dataset; under the `et` rule the hidden and off-price cases are asserted to differ. | `python/tests/oracle/qt_oracle` |

### L2 view and zoo

| Id | What it checks | Reference |
|---|---|---|
| O-L2-1 | The L2 aggregate equals the exact book after every event. | `qt_book` |
| O-L2-2 | The visible book rebuilt from emitted rows equals the book's top N; with cadence, at each emission. | `qt_book` |
| O-L2-3 | Scroll-in and scroll-out flags; trade-before-depth ordering. | hand-built |
| O-ZOO-1 | Model golden values bit for bit (`f64::to_bits`) on macOS arm64, within 4 ulp elsewhere. | hand-computed, checked against hftbacktest |
| O-ZOO-2 | Faithful models equal a line-by-line transliteration of hftbacktest's `queue.rs` and exchange fill rules on 200 random L2 sequences per family (estimate trajectory bit-exact, fills equal). | `python/tests/reference/hbt_ref.py` |
| O-ZOO-3 | Optional: hftbacktest 2.4.4 itself, driven by our L2 export, gives the same first-fill times (`make hbt-crosscheck`, separate venv). | hftbacktest 2.4.4 |
| O-ZOO-4 | `hbt_l3_fifo` corrected equals the exact engine; faithful equals a transliteration of hftbacktest's L3 model. | exact engine |
| O-ZOO-5 | Monte Carlo models are seed-deterministic and independent of thread count. | itself |
| O-ZOO-6 | The bounds bracket the exact arm order by order: every shadow the exact arm fills within `h_max` is filled by `front_of_queue` no later, and every shadow `trade_through_only` fills is filled by the exact arm no later (`bounds_bracket_exact_order_by_order` on the scenario corpus, generated CME, Nasdaq and generic tapes and `synth_small`); every run also checks `front_of_queue >= exact >= trade_through_only` fill counts. The bounds bound the exact arm, not the other models. | bracket |
| O-ZOO-7 | A perfectly calibrated predictor with random shared cancels has a fill-rate-bias CI containing 0 at every horizon under the audit's censoring rule, and the old rule is shown to be biased. | the audit's simulation |

### Calibration

| Id | What it checks | Reference |
|---|---|---|
| O-CAL-1, O-CAL-2 | Parameter recovery on 50,000 simulated allocation events with known `n` or `k`: `abs(ln n_hat - ln n0) < 0.02`, `abs(k_hat - k0) < 0.012` (tolerances tightened after the audit computed the standard errors). | simulation |
| O-CAL-3 | NautilusTrader touch MLE golden `2/9`; recovery of `p = 0.2` within 0.005, also with a competing trade-through process (audit A7). | simulation |
| O-CAL-4 | Survival fit recovers `k = -0.8` within one grid step. | simulation |
| O-CAL-5 | Optimizer accuracy to 1e-7; calibration events agree with the queue trajectory. | hand-built |

### Features and learned model

| Id | What it checks |
|---|---|
| O-FEAT-1 | No lookahead: features are unchanged when every later record is deleted. |
| O-FEAT-2 | Hand-computed features on two scenarios. |
| O-MOD-1 to O-MOD-5 | Person-period expansion, monotone incidence curves, beating climatology on a known process, leak-free splits, scoring rules equal hand-computed values. |

### Statistics and scorer

| Id | What it checks | Reference |
|---|---|---|
| O-STAT-1 | McNemar equals statsmodels on 50 tables and the golden `b = 3, c = 12`; the clustered test's hand formula; under a day-clustered null the primary test rejects at most 7% at a nominal 5%. | statsmodels, simulation |
| O-STAT-2 | Wilcoxon equals scipy; Kaplan-Meier and Aalen-Johansen equal lifelines to 1e-12; log-rank to 1e-9. | scipy, lifelines |
| O-STAT-3 | Gray's test (implemented from Gray 1988) equals reference values from R `cmprsk` (used only to generate the reference file, since cmprsk is GPL). | R cmprsk 2.2.12 |
| O-STAT-4 | BH, BY and Holm equal statsmodels; bootstrap CIs are deterministic for a seed; TOST on constructed estimates. | statsmodels |
| O-STAT-5 | Brier, Murphy decomposition, Hosmer-Lemeshow and reliability tables equal the predecessor's values on seeded inputs. | predecessor code |
| O-LIVE-1 | Each importer against fixtures written from the venue's public format documentation with invented accounts and values: dedupe, busts and corrections, fee signs, validator negative cases. | public docs |
| O-SCORE-1 | A live log generated by the exact engine scores `b = c = 0` and equivalent against `--sim exact`. | construction |
| O-SCORE-2 | The same log against `front_of_queue` is detected as overfilling. | construction |
| O-SCORE-3 | Markout as-of rule, end-of-data nulls, units, position series; amend legs sized from instructions (A8). | hand-built |
| O-SCORE-4 | Your own orders injected into a synthetic tape: with self-exclusion `sim exact` gives zero discordance; without it, live-only discordance (A1). | construction |
| O-SCORE-5 | A live order filled at `t` with sim fills only after `t` counts as live-only (A9). | construction |
| O-REF-1 | Every referee fixture passes with the exact engine and fails with a deliberately buggy mini-simulator of its class. | mini-simulators |

### Interfaces and service

| Id | What it checks |
|---|---|
| O-SCHEMA-1 to O-SCHEMA-3 | Rust and Python table schemas identical; `make schemas` leaves no diff; Rust-produced JSON validates against the JSON Schemas. |
| O-PARITY-1 | The same spec through the Python API, the CLI, the standalone binary and an HTTP job gives identical summaries and output hashes. |
| O-CLI-1 | Exit codes 0, 1, 2 and the `qt.error` document per error class. |
| O-API-1 to O-API-6 | Schemathesis conformance on every endpoint; idempotency; tenant isolation including database row-level security; problem bodies; the committed OpenAPI document is current; rate limits. |
| O-Q-1, O-STORE-1, O-BILL-1, O-RET-1 | Job lifecycle with crash recovery; multipart uploads and ranged downloads on the local store and on S3 (moto); metering; retention and deletion. |
| O-EGRESS-1 | In zero-egress mode a job cannot open a connection to any host other than the database and the object store. |
| O-MCP-1 | The MCP tools list, schemas and local read-only behaviour. |

## The independent audit and prep work

Before and during the build, separate pieces of work started from primary sources rather than from the
specification's own claims, and their results became tests or rulings:

- **Specification audit.** The shadow engine, zoo evaluation, calibration and scorer sections were checked against
  hftbacktest's source at the pinned commit, the ITCH 5.0 specification and the predecessor engine's venue notes,
  with reproducible simulation checks for each statistical claim. It found 2 blockers, 9 major and 15 minor issues;
  [Rules from the independent audit](concepts/audit-rules.md) lists them with the regression test for each.
- **Statistical reference values.** The reference files under `tests/fixtures/reference/` were generated by scripts
  that call statsmodels, scipy, lifelines, scikit-learn and R `cmprsk`, with the package versions recorded in
  `PROVENANCE.json`. They regenerate byte for byte, and the scripts' random number generator was written
  independently and checked against the Rust generator's known-answer file (`schemas/rng_kat.json`).
- **hftbacktest cross-check.** The SPEC golden table was checked bit for bit against hftbacktest's own model code;
  the released wheel 2.4.4 was compared with a build of the pinned commit on 83 scenarios, 18 queue models and both
  exchanges (they agree on every fill, status and queue value); and the Python transliteration used by O-ZOO-2 was
  replayed over every recorded queue-model call of those runs. Faithful mode's order-handling details (zombie
  orders, GTX expiry, clears) come from that work.
- **NautilusTrader cross-check.** `scripts/nt_crosscheck/run.py` runs nautilus_trader 1.231.0 in its own venv
  (public Python API only, no NautilusTrader code in queuetruth) on the zoo's L2 view of each day, fed as
  `OrderBookDelta`s, with the same orders as the zoo's shadows, and compares first fills order by order. The faithful
  `nt_prob_touch`, `nt_queue_l2` and `nt_queue_l2_prob_touch` (at `p = 1`) agree with it on 100% of orders at every
  horizon on both Nasdaq days and the synthetic fixture. The faithful rules for hidden prints, size updates that do
  not shrink the queue, and the queue gate on trade-throughs come from that work. Fed `OrderBookDepth10` snapshots
  instead, 1.231.0 resets queue position at every snapshot and its queue model fills like the touch model; the zoo
  models describe the delta feed. The same harness runs on nautilus_trader 2.0.0rc7: the touch model fills the same
  orders as 1.231.0, the queue model agrees with faithful `nt_queue_l2` on 99.60% to 100% of orders (2.0 caps queue
  ahead when a level shrinks), the depth feed no longer resets the queue (from rc5), and fed the MBO book as L3
  deltas, 2.0's queue model fills the same orders as the exact replay on all but 3 order-horizons
  (`scripts/nt_crosscheck/l3_mbo.py`).
- **Importer fixtures.** Synthetic inputs for all nine importers, written from each venue's public API
  documentation with invented ids, prices and accounts, with hand-specified expected canonical rows (O-LIVE-1).
- **Real-data expectations.** Every real-data target was recomputed from the predecessor engine's artifacts by
  separate scripts, including an independent Python port of the shadow rules that reproduces the predecessor's
  shadow fills on both days. Its README claims were checked against the recomputation.
- **Independent matching-engine oracle.** `python/tests/oracle/qt_oracle` was written from the venue rules and the
  audit rulings without reading the production book or replay code (O-SH-5). On the datasets ingested from the raw
  ITCH bytes, with queuetruth's own mirror sampler (about half of the shadows fill), it agrees with the engine on
  3,000 of 3,000 shadows on both days; on the predecessor engine's CSV conversion, with the predecessor's mirror
  shadows, on 2,997 and 3,000 of 3,000 (the 3: the oracle's 1 s cross window on a CSV without cross flags), with
  equal filled quantities in every run. The earlier disagreements (2,992 and 2,985 of 3,000 on the raw bytes) were
  traced order by order to orders Nasdaq releases after the open with their receipt priority, which both engines had
  queued at the tail, and to two hidden-print cases; the fixes and the evidence are in
  [Oracle disagreements on real data](design/extensions/oracle-disagreements.md) and [Results](RESULTS.md),
  section 3. Its stated limits:
  the taker flow is held fixed (nobody reacts to the shadow); IOC remainders that executed nothing leave no trace;
  hidden liquidity is known only when it prints; prints that could not have traded under display and price priority,
  and cross prints, are not used; auctions are not modelled; latency is `latency_ns` only, without jitter; shadows are
  evaluated one at a time; disagreements are attributed heuristically. The full list is `qt_oracle.LIMITS`.

These were done by AI coding agents working for the same author, not by an outside firm. They are independent of the
production code, not of the project.

## Real-data checks

The real-data driver (`make realdata`, `scripts/realdata/validate_et.py`) replays two public Nasdaq TotalView-ITCH
days (2019-12-30 and 2020-01-30, 4 symbols: AAPL, MSFT, SPY, TSLA; 16,754,562 records). Every number and its source
artifact is in [Results](RESULTS.md); the summary:

| Check | What it compares | Result |
|---|---|---|
| RD-1 | Ingest of both days | partitions, records per instrument (6,103,262 and 10,651,300), session bounds and source prints equal to the expected values |
| RD-2, RD-6 | Invariant counters and trade parity per day | all 8 invariants 0; 211,867 and 421,420 source prints with 0 mismatches; 0 price-priority violations over 490,472 executions |
| RD-3 | Per-instrument BBO stream identity with the predecessor engine | identical digests, 0 mismatches over 5,423,833 and 9,361,483 events |
| RD-4 | Rank-trace identity and the rank-0 shares | identical; rank 0 after sweep grouping 98.72% and 99.09% |
| RD-5 | Shadow fills identical to the predecessor engine under the `et` rule | 3,000 of 3,000 mirror shadows per day, 0 mismatches, identical queue traces |
| RD-7 | L2 aggregate identity | the visible top 3 (both days) and top 5 (day two) identical after every event |
| RD-8 | Zoo leaderboard on real orders | 17 models in both modes on 3,000 never-cancel shadows per day (the published leaderboard, [Results](RESULTS.md) section 4, is the later 16-model rerun under 0.1.1's common population); the count bracket (O-ZOO-6) holds; the faithful NautilusTrader touch model reaches `front_of_queue` in every day and horizon cell; `hbt_l3_fifo` corrected equals exact on every row. In the earlier run, 5 sampled shadows filled in the exact arm and not under `front_of_queue`; the bounds were then redefined so that the exact fills are a subset of `front_of_queue`'s by construction (SPEC 5.3.3 h/i), and on real day one the per-order violations went from 5 to 0 (3,000 sampled shadows, both modes) |
| RD-8 breadth | Exactness and zoo on 36 more Nasdaq symbols in 5 liquidity tiers | raw ITCH bytes, both days: 0 invariant violations, 0 price-priority violations, 0 trade-parity mismatches in every tier; the leaderboard on standard, large and deep shadows per tier ([Results](RESULTS.md), section 4b) |
| RD-8 NT | Faithful NautilusTrader models against nautilus_trader 1.231.0 | `BacktestEngine` fed the same L2 view as `OrderBookDelta`s with the same orders: `nt_prob_touch[p=1]` and `nt_queue_l2` fill the same orders at 0.1 s, 1 s, 10 s and 60 s on both days and on the synthetic fixture (100%); Nautilus's touch bias at 1 s is +3.13 pp [+2.53, +3.77] on day one and +2.80 pp on day two (`scripts/nt_crosscheck`, separate venv) |
| RD-9 | Calibration fitted on day one, scored on day two | NautilusTrader `p = 0.287` cuts the 1 s overfill from +2.80 to +0.41 pp; power3 `n` and Databento `k` score about like their defaults, and the export refuses the fitted `n` |
| RD-10 | Learned model skill on the held-out day | Brier skill over climatology 0.095 [0.072, 0.122] at 1 s and 0.115 [0.102, 0.128] at 10 s (L2-only features) |
| RD-11 | Scorer on real orders used as "live" | the exact arm with self-exclusion reproduces all 3,000 orders per day (250 / 2,750 / 0 / 0 and 195 / 2,805 / 0 / 0), with the live filled quantity and first-fill time on every order |
| RD-12 | Crypto capture replay counters | one 10-minute Bitstamp and Bitfinex capture, invariants 0; Bitfinex record count equal to the predecessor's, Bitstamp 77,157 records since the reader's seed rule changed (77,272, equal, before) |
| RD-13 | Performance | book-only replay of real day one from Parquet at 8.56M to 8.60M records/s against the 8M target (Low Power Mode on); see [Performance](#performance) |

What to keep in mind when reading these:

- The predecessor engine is the author's own earlier private prototype. Agreement with it shows the
  port is faithful; the checks a reader can rerun from public data alone are the invariants, trade parity against
  the feed's prints and the rank-0 share.
- `make realdata` reads the predecessor's CSV conversion of the two days (`--format etcsv`). RD-1 to RD-6 were also
  repeated from the raw public ITCH bytes with queuetruth's own ITCH reader, with identical results
  (`scripts/realdata/itch_subset.py` streams one public day file and keeps the messages of the chosen symbols, the
  raw file is never stored; `scripts/realdata/itch_realbytes.py` runs the checks). `make reproduce`
  (`scripts/repro/reproduce.py`) computes the published leaderboard, RD-9, RD-10 and the oracle from the raw bytes
  alone; on the same build the leaderboard and RD-9 equal the same runs on the CSV conversion cell for cell, and its
  rerun on the 0.1.0 commit (`4be5240`) reproduced the leaderboard, the four calibrations, the learned model and the
  oracle identically ([provenance](RESULTS.md#provenance)).
  `make reproduce-breadth` runs the 36-symbol breadth study. RD-7, RD-11, RD-12 and RD-13 have not been repeated on
  the ITCH-ingested datasets. The CSV conversion merges hidden and cross prints, so the hidden-print rule
  ([A3](concepts/audit-rules.md#a3-hidden-prints)) is checked on real data only from the raw files.
- With two days, every bootstrap uses 30-minute blocks, so the published intervals capture intraday variation only.

## Fuzzing

- **Rust parsers** (`fuzz/`, cargo-fuzz; a separate Cargo workspace): libFuzzer targets for ITCH 5.0, IEX DEEP+, DBN,
  `qt_ingest::ingest` end to end, the etcsv and Parquet formats and the dataset reader, the seven crypto converters,
  `qt_crypto::prepare`, and the exact-decimal JSON reader with the unit and time parsers. Beyond "no panic", every
  target checks that an error caused by input bytes is `QT-INPUT-*`, `QT-CONFIG-*` or `QT-DATA-*`, that ITCH and
  DEEP+ messages round-trip through `encode`, that mapped records carry `LAST` and their message sequence, and that a
  dataset an ingest path wrote opens and reads back. Each target ran at least 600 s; the runs, the findings and their
  fixes are in [the rust-fuzz design note](design/extensions/rust-fuzz.md). `make fuzz-rust` replays every seed,
  every minimized regression and the known crashes on the stable toolchain.
- **Python parsers** (`python/tests/fuzz`, Hypothesis, marker `fuzz`): the FIX wire, dictionary and store readers,
  the IBKR Flex and simulator importers, the instrument map, every venue importer and the `dq` dataset loader. Each
  property checks that a parser returns or raises `QtError` with a `QT-*` code, within bounded time and memory. The
  default test run uses 40 examples per property; `make fuzz-py` runs the deep profile (400 by default) and writes
  per-target statistics.

## Determinism

- Byte-identical Parquet, JSON (except the `timings` block), HTML and npz for the same inputs, spec and version;
  tested across two runs and `--threads 1` against `4` (and `OMP_NUM_THREADS=1` against `4`).
- All randomness comes from seeded streams keyed by documented parts (for example the model id and shadow id), so
  output does not depend on processing order.
- `run_id` is the first 16 hex characters of a sha256 over the canonical JSON of the engine version and the spec,
  with every input replaced by its sha256.

## Performance

Measured on an Apple M1 Pro (8 cores, 16 GB) with Low Power Mode on, so these are lower bounds. Every SPEC 7.1
throughput target is met: book-only replay from Parquet 14.5M records/s on synthetic data and 8.56M to 8.60M on real
day one (target 8M), replay with Parquet sinks 6.25M (target 4M), ITCH parse and map 16.7M messages/s (target 5M),
ITCH ingest 4.56M messages/s (target 2.5M), replay with 100,000 shadows 5.28M records/s (target 5M). The zoo for 17
models and 3,000 shadows of day one takes 7.6 to 9.0 s at 4 threads (target 90 s). Two targets are still missed:
`fit-hazard` (172 s against 120 s) and the Python binding's overhead over the CLI (18% against 5%). The full table is
in [Results](RESULTS.md), section 7; a quiet-machine baseline for the regression gate is still to be recorded.

## What is not tested yet

- DBN and CME semantics on real data (synthetic only).
- Importers on real account exports (fixtures only).
- The ITCH reader on more than the two raw public days and four symbols above.
- Three parser faults that fuzzing found are known and not fixed: the pinned `dbn` 0.71.0 decoder panics on a DBN
  prelude announcing 100 to 103 metadata bytes and allocates the announced metadata length (up to 256 MiB) before
  reading it, and the Parquet reader panics on some corrupt file metadata (five sites in `arrow-ipc` / `parquet`
  59.3.0). A panic surfaces as a Python `PanicException` or a service job's `QT-ENGINE-001` instead of
  `QT-INPUT-002`. The reproducers are in `fuzz/known-crashes/`.
- The Rust fuzz harness tests (`make fuzz-rust`) run in the local gate (`make gate-3`), not in CI; the libFuzzer
  campaigns themselves (`fuzz/run.sh`, nightly toolchain) are run by hand.
- Docker Compose end to end in CI: `make compose-config` validates the file, and the compose smoke test runs only
  when a Docker daemon is available.
- The optional hftbacktest cross-check (O-ZOO-3) runs only in its separate venv (`make hbt-crosscheck`); the
  NautilusTrader cross-check runs only in its own venv (`scripts/nt_crosscheck`) and is not part of `make test`.
- The NautilusTrader snippet that `export nautilus` writes runs against stand-in 1.x and 2.x modules in every test
  run, and against a real nautilus_trader only when `QT_NT_PYTHON` names an interpreter that has it (checked on
  1.231.0 and 2.0.0rc7). The `export replay-schedule --target nautilus` strategy runs against a stub of its API.

## Run the tests yourself

```sh
make venv sync develop
make test          # lint, Rust tests, Python tests, service tests (ephemeral Postgres)
make test-e2e      # cross-interface parity and determinism
make realdata      # needs the predecessor engine's artifacts; writes only to .realdata/
python docs/check_docs.py   # every runnable block in these docs, links, generated reference pages
```
