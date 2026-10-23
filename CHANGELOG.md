# Changelog

All notable changes to queuetruth are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).
The Python package, the Rust crates and `queuetruth._core` always share one version.

## [Unreleased]

## [0.1.1] - 2026-10-23

A release for first-time users: a stranger should get a readable answer in five minutes. Data schema unchanged
(1.2); the new summary fields are additive with defaults.

### Real-data results

- NautilusTrader cross-check re-run on nautilus_trader 2.0.0rc7 (2026-10-22, same 3,000 orders per day; `run.py`
  now runs on 1.x and 2.x). The default touch model (`DefaultFillModel(prob_fill_on_limit=1)`) fills exactly the same
  orders as 1.231.0 on both days and the synthetic fixture, so the overfill stands: +3.13 pp [+2.53, +3.77] at 1 s on
  2019-12-30 and +2.80 pp [+2.23, +3.37] on 2020-01-30, and faithful `nt_prob_touch` still agrees on 100% of orders.
- 2.0 changes Nautilus's queue model. On a delta feed it also caps queue ahead at a level's new size when the level
  shrinks (present from 2.0.0rc4), so faithful `nt_queue_l2` (1.231.0's rules) agrees with 2.0.0rc7's
  `queue_position=True` on 99.60% to 99.97% of orders per horizon; every difference (18 orders on day one, 10 on day
  two) is the cap, and Nautilus fills each one earlier. Nautilus's own bias against exact at 1 s / 10 s / 60 s goes
  from -0.10 / -0.77 / -0.70 pp to -0.03 / -0.37 / -0.37 pp on day one and from -0.10 / -0.17 / -0.23 pp to
  -0.03 / -0.03 / -0.07 pp on day two.
- The `OrderBookDepth10` reset is gone from 2.0.0rc5 (#4942): fed depth snapshots, 2.0's queue model fills exactly
  the orders it fills on the delta feed (1.231.0: like the touch model, +2.87 and +2.67 pp at 1 s).
- Fed the MBO book as L3 deltas (`book_type=L3_MBO`), 2.0.0rc7's queue model fills the same orders as the exact
  replay at every horizon on day one; on day two it differs on 1 order at 0.1 s and 2 at 10 s (hidden prints, which
  the harness passes as `NO_AGGRESSOR` trades and the exact replay does not count; bias at most +0.07 pp). 1.231.0's
  L3 queue model overfills: +1.73 pp at 1 s on day one, +1.67 pp on day two.

### Changed

- **One common population for every zoo model.** A shadow on which any faithful hftbacktest model hit its bug #312
  is now left out of every model's metrics (`queuetruth.eval.common_population`), not just the aborted model's.
  The aborts hit the orders that reach a partial fill, so in 0.1.0 the hbt rows were scored on fewer, less
  fill-prone shadows than the others (n = 945 against 994 in the quickstart), against a truth computed on all of
  them. On the synthetic quickstart the hbt models move from about -2.9 pp to -0.8 pp at 60 s; expect the hbt rows of
  a real-data leaderboard to move too, and every row to be computed on fewer shadows. New summary fields
  `n_excluded_hbt_312` and `n_leaderboard`. The calibrate held-out comparison (fitted vs default) uses the same
  rule.
- **`--models all` runs 16 models, not 17.** `hbt_power[n=1]` (`b / (b + f)`) equals `hbt_power3[n=1]`
  (`1 - f / (f + b)`) in every cell and was dropped from the default set; it still runs when named.
- **Unit-less durations are rejected** on every flag whose name does not end in `-ns`: `--lifetime 5` was silently
  5 ns and is now `QT-USAGE-001` ("write 5s, 5ms, 5us or 5ns"). `0` and the `*-ns` flags keep accepting bare
  integers.
- **Quiet terminal output.** The per-model `hbt_312_abort:` warnings collapse into one line with a link to the
  hftbacktest issue; informational lines (`info: `, `ci_block=`, `self_exclusion: matched`, all-zero shadow
  diagnostics) print only with `--verbose` or `--log-level info`. The answer is flushed to stdout before stderr.
  `summary.json` keeps every warning.
- **Real Databento CME data is no longer labelled synthetic.** The report used the manifest's `validation_status`
  (the venue rule set's validation level) as the data's provenance, so a DBN file ingested with `--venue cme` got a
  red "Synthetic or fixture data" banner. Vendor-format data (`dbn`, `itch`, `iex`, `etcsv`) whose rules are
  validated on synthetic data only now gets "Vendor data; venue rules validated on synthetic data only".
- README: the PyPI page no longer says the package is not on PyPI, every link is absolute (relative links 404 on
  PyPI), and PyPI, Python and licence badges were added.
- The NautilusTrader cross-check tests pin each version's rules: on 2.x the depth feed must fill like the delta
  feed, and the `p = 0.29` expectation is checked under each side's own rules.

### Added

- **Base rates next to every bias.** Leaderboard entries carry `exact_fill_rate` and `model_fill_rate` per horizon
  (`fill_rate_bias = model_fill_rate - exact_fill_rate`). The CLI table and the report table start with an "exact
  fill rate" row, and each plain-words sentence reads "fills 56.2% of orders vs 57.0% in the exact replay, 0.8 pp
  less often".
- `queuetruth zoo --list-models` (also `--format json`): the `all` set with one line per model, then the
  parameterised and opt-in families.
- "Did you mean": an unknown model family names the closest one (`hbt_powr3` -> `hbt_power3`), and model ids are
  checked before any data is read.
- `--help` shows each flag's default (from the spec's JSON Schema, e.g. `--lifetime [default: 5m]`,
  `--horizons [default: 100ms,1s,10s,60s ...]`).
- `sample_shadows`: `allocation: per_partition` with `partition_quotas` (`"<instrument_id>"` or
  `"<instrument_id>_<YYYYMMDD>"`): each instrument-day draws `n` (or its quota) shadows whatever else the dataset
  holds, so a dataset split into shards draws exactly the shadows of the whole. An empty `partition_quotas` stays out
  of `run_id`; existing run ids are unchanged.
- `queuetruth.eval.blocks`: summable zoo statistics per model, population, stratum, horizon, instrument-day and
  30-minute block (`block_stats`); `evaluate_blocks` rebuilds `evaluate_zoo`'s ratio metrics and block-bootstrap
  CIs from them across shards; `cluster_bootstrap` (two-way dates x symbols), `shrink` (empirical Bayes) and `bh`.
- `scripts/scale/`: the W1 full-market pipeline (ITCH day split into symbol buckets, planner, work units with shard
  manifests, aggregator with a Merkle-rooted run manifest, single-unit verifier, Modal entry points, and the local
  proof that 72 single-symbol units reproduce the 36-symbol breadth run of docs/RESULTS.md 4b).
- `scripts/nt_crosscheck/l3_mbo.py` and `l3_score.py`: feed the shadows' MBO book to NautilusTrader as L3 deltas and
  score each order's first fill against the exact replay.

### Fixed

- The local store's signed-URL check and the live-log, FIX and CLI parsers guarded `int()` with `str.isdigit()`,
  which accepts characters such as `²` that `int()` rejects; a tampered `exp` on `PUT /v1/_store/{key}` returned
  500 instead of 403 (found by the API fuzz test). Every guard now uses `isdecimal()`, and signed-URL expiries must
  be ASCII digits.

## [0.1.0] - 2026-10-19

First public version. Validated on real data for Nasdaq TotalView-ITCH (two public days, four symbols) and IEX
DEEP+ (one public day, four symbols); everything
else is tested on synthetic data and fixtures, as listed under "Known limitations". Evidence:
[docs/RESULTS.md](docs/RESULTS.md) and [How queuetruth is tested](docs/testing.md). Data schema 1.2
(design: [spec 3.12](docs/design/spec.md)).

### Real-data results

- Exact replay of 16,754,562 records of two public Nasdaq ITCH days (2019-12-30, 2020-01-30; AAPL, MSFT, SPY, TSLA)
  from the raw bytes with queuetruth's own ITCH reader: 0 invariant violations, 0 trade-parity mismatches over 633,287
  prints, 0 price-priority violations; BBO and rank traces, and 3,000 of 3,000 mirror shadows per day (under its
  fill rule), identical to the author's earlier private prototype. An independent matching-engine oracle agrees with
  the default shadow fill rule on 2,992 and 2,985 of 3,000 shadows from the raw ITCH bytes (queuetruth's own mirror
  sampler) and on 2,997 of 3,000 per day of the prototype's mirror shadows on its CSV conversion.
- Public reproduction from the raw Nasdaq files alone (`make reproduce`, `scripts/repro/reproduce.py`): the
  leaderboard and RD-9 from the raw bytes are identical to the same runs on the CSV conversion with the same build,
  and RD-10 to the earlier CSV run. Rerun on commit `4be5240`, `make reproduce` reproduced the leaderboard on both
  days, all four calibrations, the learned model and the oracle identically (1,162 s; `.realdata/repro/results.json`
  sha256 `446c6b09ba71dbd8...`). Against the earlier published leaderboard, every change comes from engine changes: the bounds
  (`trade_through_only` now -6.0 .. -15.8 pp, `front_of_queue` +0.9 .. +3.3 pp), the verified faithful NautilusTrader
  models, and faithful hftbacktest shadows that reach the #312 event now left out per shadow (179 to 342 of 3,000
  per model, 6.0% to 11.4%); the hftbacktest rows moved by at most 0.1 pp.
- NautilusTrader result verified against the real engine: nautilus_trader 1.231.0, `FillModel(prob_fill_on_limit=1)`,
  its `BacktestEngine` fed the L2 view as `OrderBookDelta`s with the same 3,000 orders per day, overfills by
  +3.13 pp [+2.53, +3.77] at 1 s on 2019-12-30 (14.17% against 11.03% exact) and +2.80 pp on 2020-01-30.
  queuetruth's faithful NautilusTrader models (`nt_prob_touch`, `nt_queue_l2`, `nt_queue_l2_prob_touch` at `p = 1`)
  agree with it on 100% of orders at 0.1 s, 1 s, 10 s and 60 s on both days and on the synthetic fixture
  (`scripts/nt_crosscheck`, separate venv, public API only). On an `OrderBookDepth10` feed Nautilus resets
  `queue_position` at every snapshot, so its queue model then fills like the touch model (+0.9 to +3.3 pp above exact
  at 1 s to 60 s); the faithful models describe the delta feed.
- L2 model leaderboard on 3,000 never-cancel shadows per day: the hftbacktest power and log models stay within
  about 1 pp of exact; `hbt_risk_adverse` is pessimistic by up to 1.3 pp; faithful `nt_prob_touch[p=1]` reaches the
  `front_of_queue` bound. The bounds were redefined (`front_of_queue` also fills on hidden prints and on a cross at
  arrival; `trade_through_only` fills only on a displayed trade-through) so that they bracket the exact arm order
  by order, and O-ZOO-6 now tests that shadow by shadow on the scenario corpus, generated CME, Nasdaq and generic
  tapes and `synth_small`. On real day one the per-order violations went from 5 (the earlier run, before the
  redefinition) to 0 (3,000 sampled shadows, both modes).
- Breadth (`make reproduce-breadth`): 36 more Nasdaq symbols in five liquidity tiers on the same two days, from the
  raw bytes, with 0 invariant violations, 0 price-priority violations and 0 trade-parity mismatches in every tier.
  The NautilusTrader default overfills in every tier, most on the deep-queue names (+8.60 pp at 60 s on day one);
  hftbacktest `power3[n=2]` stays within 1.7 pp and `nt_queue_l2` within 1.5 pp at 60 s; order size barely moves
  first-fill bias.
- Calibration fitted on day one and scored on day two (Nasdaq; `p` is venue-specific): NautilusTrader touch
  `p = 0.287` cuts the 1 s overfill from +2.80 pp to +0.41 pp; the queue-gated `p` (0.871 [0.816, 0.919], 613
  events) does not beat `p = 1`; power3 `n` and Databento `k` score about like their defaults, and the export refuses
  the fitted `n`.
- Scorer (RD-11): 3,000 real orders per day used as a live log are reproduced exactly by the exact arm with
  self-exclusion on both days (250 / 2,750 / 0 / 0 and 195 / 2,805 / 0 / 0; identical filled quantity and
  first-fill time on every order). This rests on per-shadow twin linking: an excluded live order is replaced, for
  its own shadow only, by that shadow, which takes the order's exact queue place and is filled by the order's
  executions, while every other shadow still sees the order on the tape. An earlier build, before the root-cause fix
  of the self-exclusion path, disagreed with live on 10 orders on day one (242 / 2,748 / 8 / 2).
- Performance (RD-13 passes): the book-only replay of real day one from Parquet runs at 8.56M to 8.60M records/s
  against its 8M target, with Low Power Mode on. Every SPEC 7.1 throughput target is met with byte-identical outputs
  (synthetic replay 14.5M records/s, with Parquet sinks 6.25M, ITCH ingest 4.56M messages/s, 100,000 shadows 5.28M
  records/s); the zoo runs 17 models x 3,000 shadows in 9.0 s.
- Learned fill model: Brier skill over climatology on the held-out day 0.095 [0.072, 0.122] at 1 s and 0.115
  [0.102, 0.128] at 10 s.

### Added

- **Ingest** into one partitioned Parquet dataset format: Nasdaq TotalView-ITCH 5.0, Databento DBN MBO (versions 1
  to 3), the earlier prototype's CSV layout (`etcsv`), our own normalized Parquet, crypto L3 captures (Bitstamp, Bitfinex, Coinbase)
  and Tardis L2 (L2 only: simulation, markouts and live calibration, never exact truth). Every dataset carries a
  validation label.
- **Exact replay**: per-instrument FIFO book with invariant counters, a queue-rank oracle with sweep grouping,
  derived BBO, depth and trades, a data-quality report and parity checks against the source's own prints.
- **Shadow orders** with the fill rules of the independent specification audit (queue ahead from the records of the
  orders ahead, hidden prints behind displayed interest, taker fills on crossed books, CME same-size modifies losing
  priority); per-fill timeline, queue trajectory, latency, cancels and amends; `--fill-rule et` for parity with the
  earlier prototype; `--exclude-order-ids` to remove real orders from the tape.
- **`why`**: one order event by event with a one-sentence conclusion; `--model` / `--zoo` add a queue model's
  estimate and the first event where it diverges from the exact queue.
- **Queue-model zoo**: 17 rows by default, 15 model configurations plus 2 bounds (hftbacktest RiskAdverse,
  Power/Power2/Power3/Log/Log2 and L3FIFO, ported from its source; NautilusTrader queue and touch models and
  Databento's Monte Carlo estimator, re-implemented from their documentation; front-of-queue and
  trade-through-only bounds) in `faithful` and `corrected` mode, scored with outcome-independent censoring and
  cluster-robust discordance tests by horizon, queue decile and liquidity; `--amends`, `--exclude-order-ids`;
  answer-first leaderboard in the terminal.
- **Calibration** of `n`, `k` and `p` against exact truth, and `calibrate-live` against your own fills on L2-only or
  MBO tapes, each with a trust verdict (grid edge, identification, beats the default out of sample).
- **Fail-safe export** of hftbacktest and NautilusTrader snippets: a refused calibration exits 1 with
  `QT-CALIB-001`; `--force` writes it under a NOT VALIDATED header. `export replay-schedule` writes a program that
  replays a live order schedule in hftbacktest or NautilusTrader; `export latency-npz` and `export l2-npz`.
- **Learned fill model**: a discrete-time competing-risk hazard model (`features`, `fit-hazard`);
  `HazardModel.load` unpickles only with a trusted sha256 or an explicit `trusted=True`.
- **Sim-vs-live scorer**: canonical live order log; importers for Binance, Bybit, OKX, Coinbase, Kraken, Deribit,
  Hyperliquid, IBKR Flex, FIX drop copies and QuickFIX engine logs (C++, J, n, Go; FIX 4.2, 4.4, 5.0SP2), and for
  hftbacktest and NautilusTrader output (`live import --venue sim-hftbacktest|sim-nautilus`); own-order exclusion on
  MBO and L2 tapes; instruction-sized legs; one evaluation window for both arms; cluster-robust primary test; time to
  fill; markouts; `--fail-*` CI gates; answer-first terminal output.
- **Referee**: exact-FIFO regression oracle for other simulators with 16 synthetic bug-class fixtures.
- **Interfaces**: Python API, CLI, self-contained HTML reports (`--sort-horizon`), JSON summaries validated by
  published JSON Schemas (schema 1.1), an MCP stdio server (extra `[mcp]`) and an HTTP client (extra `[client]`).
- **Quickstart data**: `queuetruth fixtures synth --out DIR` writes the synthetic dataset used by the docs, so a pip
  install needs no repository checkout.
- **Self-hostable service** (`queuetruth-service`, FastAPI + PostgreSQL + S3-compatible store) with separate
  unprivileged database roles for the api and the worker, tenant binding by API key or held job, per-tenant job and
  compute limits, zero-egress mode and a Docker Compose stack. It is not published to PyPI in 0.1.0; run it from the
  repository or the compose stack in `deploy/`. Per-kind job timeouts (`QT_JOB_TIMEOUTS_S`, falling back to
  `QT_JOB_TIMEOUT_S`); tombstone ids use the `del_` prefix (migration 0006 renames older `tomb_` ids); errors a
  handler raises (any 4xx problem, and 5xx) are not stored under an idempotency key, so a retry with the same key
  works once the cause is fixed.

- `queuetruth dq` (and `queuetruth.api.dq`, `queuetruth.dq.run`): a data-quality battery over any ingested MBO
  dataset, with an optional second dataset of the same sessions for cross-source parity. Fourteen checks (sequence,
  crossed and stale books, orphan references, sizes and prices, trade parity, timestamps, session coverage,
  symbology, snapshots, vintage, cross-source parity, rate drift, plus the ingest and replay counters), each `pass`,
  `warn`, `fail` or `skip` with its numbers and the first offending rows. Writes `dq_report.json` (`qt.dq_report`)
  and a self-contained `dq_report.html`; exits 1 when a check reaches `--fail-on` (default `warn`). Guide:
  [Check a feed before you trust it](docs/how-to/check-a-feed.md).
- Order-level crypto sources for `queuetruth ingest`: `--format coinbase-level3` (Coinbase Exchange WebSocket
  `level3` plus REST level-3 snapshots), `kraken-l3` (Kraken WebSocket v2 `level3`; exact inside the subscribed depth
  window only, CRC32-checked after every message) and `hyperliquid-node` (a non-validator node's raw book diffs,
  fills and order statuses plus an L4 snapshot). New `--option` keys: `depth`, `checksum_precision` (Kraken),
  `snapshot_block`, `max_block_gap_ms` (Hyperliquid). Each source's exactness caveats are written as ingest
  warnings. Guide: [Ingest order-level crypto feeds](docs/how-to/crypto-l3-sources.md).
- IEX DEEP+: `--format iex-deep-plus` reads IEX HIST DEEP+ pcap / pcapng files (gzip or raw) order by order under
  the new `iex` rule set: IEX-TP sequencing and A/B de-duplication per channel, and the DEEP+ Order Modify priority
  bit mapped exactly (a maintained-priority size decrease keeps the queue position, a reset goes to the back). IEX
  HIST data may be redistributed with IEX's attribution line, so this is the public demo source
  ([Replay a public IEX DEEP+ day](docs/how-to/iex-public-demo.md), `scripts/iex/stream_subset.py`). Real-data run
  on 2026-10-09 (SPY, AAPL, NVDA, TSLA; 4,435,281 records): verdict `clean`, every replay invariant 0, and all
  21,301 displayed executions at FIFO rank 0 of their price level; the `iex` rule set is labelled `real_data`.
- Schema 1.2 documents `qt.dq_report` and `qt.dq_spec` are registered with the other JSON Schemas
  (`queuetruth schema list`); the HTTP API's ingest format lists (`/v1/meta`, dataset creation) include the four new
  formats.
- Crypto L3 datasets record their resolved rule set in the manifest (`rule_set: crypto_l3` for Bitstamp, Bitfinex,
  Coinbase and the order-level sources when ingested with `--venue generic`), so side-`N` prints consume displayed
  queue; a manifest without `rule_set` replays as `generic`.

### Security

- Dropped the `zip` crate (RUSTSEC-2026-0367, -0368, -0369): hftbacktest `.npz` files are written by a small
  in-crate zip writer (DEFLATE, matching np.savez_compressed; CRC-32 known-answer and numpy round-trip tested).
  queuetruth never extracted archives, so it was not exposed; `cargo audit` is clean.
- The service was hardened after a security review; every finding was reproduced, fixed and kept as a regression test
  (`service/tests/security/`): tenant isolation that never fails open (the api and the worker refuse to start
  unless connected as their own unprivileged role, `FORCE ROW LEVEL SECURITY` plus explicit tenant filters, signed
  tenant bindings that need an API key's HMAC or a held job attempt and cannot be replayed); job children with a
  minimal environment and no service secrets; upload sizes enforced by the store and archive extraction bounded
  inside the job child; per-tenant rate limits, key caps, bounded compute knobs and a cap on each tenant's running
  jobs; `QT_ENV=prod` refusing placeholder, short or low-variety secrets and `QT_FORWARDED_ALLOW_IPS=*`; the compose
  stack publishing the api and the store on 127.0.0.1 by default; hash-pinned image dependencies, digest-pinned base
  images, and `cargo-audit` and `pip-audit` in CI.

### Packaging

- One abi3 wheel per platform (`cp311-abi3`, CPython 3.11 and later) built with maturin, plus an sdist. Wheels carry
  `LICENSE`, `NOTICE`, the hftbacktest MIT licence and the licences of every bundled Rust crate.
- Published platforms: macOS arm64 and x86_64, Linux x86_64 and aarch64 (manylinux). Windows and musllinux wheels
  are built by the release workflow as experiments only and are not published. Elsewhere, pip builds from the sdist
  (Rust toolchain required).
- Standalone `queuetruth-core` binaries (native commands only, no Python) are attached to the GitHub release, not
  shipped in the wheel.
- PyPI uploads use Trusted Publishing with PEP 740 attestations; every artifact also gets a GitHub build-provenance
  attestation.

### Project

- Contributor guide (`CONTRIBUTING.md`, DCO sign-off), security policy (`SECURITY.md`), code of conduct, issue and
  pull request templates. The design specification and a summary of how the project was built and verified are in
  `docs/design/`.

### Known limitations

- The Databento model is re-implemented from its public tutorial and has not been checked against Databento's own
  code. The NautilusTrader models are checked against nautilus_trader 1.231.0 on a delta-fed L2 book only; on an
  `OrderBookDepth10` feed Nautilus's queue model behaves like its touch model. The `export nautilus` snippet and the
  `export replay-schedule --target nautilus` strategy are not run against NautilusTrader in the tests.
- Real-data validation covers two Nasdaq days: four very liquid symbols in full, plus 36 more symbols in five
  liquidity tiers (first-fill bias only, no fill quantity). Other venues and other regimes are not shown.
- DBN/CME semantics are synthetic only (Databento's DBN test files decode, but no real CME session was replayed).
- The Bitstamp and Bitfinex readers are checked on one 10-minute capture per venue (counters, not fill fidelity);
  the Coinbase `level3`, Kraken L3 and Hyperliquid node readers on synthetic fixtures only.
- Live-log and QuickFIX importers are tested on fixtures written from public format documentation and the engines'
  sources, not on real account exports.
- Performance is measured on one Apple silicon laptop with Low Power Mode on (every SPEC 7.1 throughput target met;
  real day one replays at 8.56M records/s). Other hardware is unmeasured, and `fit-hazard` (172 s against 120 s) and
  the Python binding's overhead over the CLI (18% against 5%) still miss their targets.
- Exact truth covers displayed liquidity on FIFO venues only; hidden liquidity, auctions, pro-rata books and
  own-order queue interaction are out of scope.
