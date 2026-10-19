# Changelog

All notable changes to queuetruth are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).
The Python package, the Rust crates and `queuetruth._core` always share one version.

## [Unreleased]

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
