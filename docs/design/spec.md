# queuetruth design specification (v0.1)

Fill-realism and queue-truth kit: exact order-level replay with shadow orders, an L2 queue-model zoo measured
against exact truth, calibration with config export, a learned L2 fill model, a sim-vs-live scorer, an exact-FIFO
referee, reports, an HTTP service and an MCP server. Apache-2.0.

> **About this document.** This is the design contract the v0.1 code was built and tested against. It is published as
> written, with internal process notes removed. It keeps the stable ids used across the code, tests and docs: `A<n>`
> are rulings of the independent specification audit (summarised in [Rules from the independent
> audit](../concepts/audit-rules.md)), `E<n>` and `M<n>` are numbered corrections and minor findings folded into the
> text (the notes they come from are not published), `O-*` are the oracle tests of section 6 and `RD-*` the real-data
> checks of section 6.8. Section numbers (for example "SPEC 5.4") are what code comments refer to. Where the code and
> this file disagree, the difference is listed under "Open conflicts" at the end; that list is a snapshot and is being
> refreshed. For user-facing behaviour, the [docs](../index.md) and the generated reference pages are authoritative.

## 0. How to read this file

- MUST / MUST NOT / SHOULD carry RFC 2119 meaning. Anything not specified is the implementer's choice, as long as
  it does not change a public name, type, column, unit, flag, endpoint or JSON field listed here.
- Names in `code` are exact: crate names, module paths, function names, Arrow column names, JSON fields, CLI flags.
- Change control: a contradiction or an impossible requirement is implemented with the narrowest reading and recorded
  as a deviation; a contract is never changed silently.
- Ids: `A<n>` = binding ruling n of the independent specification audit; `E<n>` = numbered correction n; `M<n>` =
  minor audit finding n (12..26), adopted as written unless an E or A item says otherwise; `R-<id>` and `S-<id>` =
  findings of the engineering and security reviews (`S-H1`, `S-M1`..`S-M6`, `S-L1`..`S-L7`). Later items supersede
  earlier ones where they say so (E27 supersedes A3, E28 corrects A7, A3d supersedes E27's hidden-print reading and
  E36(c), E48 refines A5).
- Contract vs code: where this file and the code disagree, the disagreement is listed with file:line evidence in
  "Open conflicts" at the end; this file stays the target. Section 3.11 lists the schema 1.1 fields.
- "et" means the author's earlier private prototype engine (`--et-root` points at a local checkout of its outputs);
  queuetruth ports its book and replay code, which is the author's own and is relicensed Apache-2.0 here. Checks
  that compare with et outputs need those artifacts and cannot be rerun from public data alone.
- "hbt" means hftbacktest at commit `5f3ec40b2afb764e0fea112f941ed85523ef4e88` (MIT). "NT" means NautilusTrader
  `develop` at `5247ad5f088992e09a62de4c050648584bddad7d` (LGPL-3.0: re-implemented from documented behaviour only,
  never copied). "DB" means Databento's queue-position blog and tutorial.

### 0.1 Global conventions (apply everywhere)

| Quantity | Representation | Notes |
|---|---|---|
| Timestamp | `i64` nanoseconds since Unix epoch, UTC. Arrow `timestamp[ns, tz="UTC"]`. JSON: integer ns. | Market-data timestamps MUST be `>= 0`. et used `u64`; convert with a checked cast. |
| Duration | `i64` ns. Arrow `int64`, column suffix `_ns`. | Horizons are durations. |
| Price | `i64` fixed point. Real price = `price * 10^price_exp`, `price_exp` per instrument (default `-9`, allowed `-12..=0`). Arrow `int64`. | `UNDEF_PRICE = i64::MAX` only inside `qt.mbo` (DBN convention); every other table uses null. |
| Size | `u64` in size units. Real size = `size * 10^size_exp`, `size_exp` per instrument (equities `0` = shares; crypto default `-8`). Arrow `uint64`. | Queue models compute in `f64` size units (section 5). |
| Side | ASCII byte `b'B'` bid/buy, `b'A'` ask/sell, `b'N'` none. Arrow `uint8`. JSON: `"B"`, `"A"`, `"N"`. | Live-log tables use strings `"buy"`/`"sell"` (section 3.8). |
| Action | ASCII byte `A C M R T F N` (DBN values). Arrow `uint8`. | |
| Money / fees | `f64` in the stated currency; fee sign: positive = cost, negative = rebate. | |
| Probabilities, scores | `f64`. | |
| IDs in files | `shadow_id`, `order_key`: UTF-8, 1..=128 chars, regex `^[A-Za-z0-9_.:\-]+$`. Duplicates are an error. | |
| Clock semantics | enum `ClockSemantics` (section 3.1), stored as Arrow `dictionary<int8, utf8>` / JSON string. | Every timestamp column whose meaning can vary carries a sibling `*_kind` column. |

Arrow table conventions: every table written by this project carries schema metadata
`qt.schema = "<table name>"`, `qt.schema_version = "1"`, `qt.engine_version = "<crate version>"`.
Parquet writer settings (determinism): ZSTD level 3, max row group 1,048,576 rows, `created_by = "queuetruth <version>"`,
dictionary encoding only for `utf8`/dictionary columns, statistics default. No wall-clock values may be written
into any data file or JSON summary except the `timings` object of a summary (section 3.10), which is excluded from
determinism hashes.

JSON conventions: every JSON document produced by the CLI, the Python API, the HTTP API or the MCP server has
top-level `"schema": "qt.<name>"` and `"schema_version": 1`, is UTF-8, has no NaN/Infinity (use `null`), and is
validated by the JSON Schema of the same name (section 3.10).

### 0.2 Error codes and exit codes

Errors carry a stable code `QT-<AREA>-<NNN>`:

| Code | Meaning |
|---|---|
| `QT-USAGE-001` | bad CLI usage / invalid flag combination |
| `QT-CONFIG-001` | invalid spec or config value (field named in message) |
| `QT-INPUT-001` | input file missing or unreadable |
| `QT-INPUT-002` | input format or schema mismatch (file, row or byte offset named) |
| `QT-INPUT-003` | duplicate id (`shadow_id`, `order_key`, `fill_id`) |
| `QT-INPUT-004` | unit conversion not exact (decimal string not representable at `price_exp`/`size_exp`) |
| `QT-DATA-001` | data-quality verdict `unusable` |
| `QT-DATA-002` | `--strict` and a non-zero invariant / anomaly counter |
| `QT-ENGINE-001` | internal invariant broken (bug); includes instrument, sequence |
| `QT-REMOTE-001` | HTTP API returned an error (problem `code` echoed) |
| `QT-GATE-001` | a requested gate failed (exit code 1, not 2) |
| `QT-CALIB-001` | `export` refused a calibration that is not trustworthy (5.10); exit code 1 like a failed gate; `--force` writes it with a NOT VALIDATED header |

A scorecard that scored no order is `QT-INPUT-002` "NOT SCORED: 0 of N live orders scored (...)" and exits 2 (rev
2026-10-19 final: E49, E52). Human output is answer-first; `--verbose` (global) prints every summary field after
the answer.

CLI exit codes: `0` success; `1` the command completed but a requested gate failed (`--strict`, `--fail-*`,
referee tolerance, scorecard gates); `2` any error (usage, config, input, data, remote, internal). With
`--format json` the CLI prints exactly one JSON document to stdout: the command's summary on 0/1, or an error object
`{"schema":"qt.error","schema_version":1,"code":"QT-...","message":"...","hint":"...|null","details":{}}` on 2.
Logs and progress go to stderr only.

## 1. Product scope for v0.1

### 1.1 In scope

1. **Ingest** order-level data into one normalized, partitioned dataset format (section 3.3):
   Nasdaq TotalView-ITCH 5.0 (`.gz` or raw), Databento DBN MBO (`.dbn`, `.dbn.zst`), the et MBO CSV layout
   (`etcsv`: et's `mbo.csv` + `trades.csv` + `instruments.csv`), our own normalized Parquet, and the crypto L3
   captures et reads (Bitstamp `live_orders`/`live_trades`/`detail_order_book` JSONL, Bitfinex `book_R0`/`trades`
   JSONL, Coinbase `full` JSONL). Plus Tardis `incremental_book_L2` + `trades` CSV(.gz) as an **L2-only** dataset
   (usable for zoo simulation and markouts, never as exact truth). Schema 1.2 (3.12) adds the order-level
   crypto sources Coinbase Exchange `level3` (`coinbase_level3`), Kraken WebSocket v2 `level3` (`kraken_l3`, a
   depth-window source: exact inside the subscribed window only) and Hyperliquid node outputs
   (`hyperliquid_node`), and IEX DEEP+ (`iex_deep_plus`, 3.12.2).
2. **Exact replay**: per-instrument venue-faithful FIFO book (port of et), event atomicity at `F_LAST`, invariant
   counters, queue-rank oracle with sweep grouping, derived BBO (change-only or full) / depth / trades, data-quality
   report, parity checks against source trades and BBO.
3. **Shadow orders** with a **per-fill timeline**, a **queue-ahead and queue-rank trajectory**, cancels and
   priority-keeping amends, configurable latency, and per-event truth decomposition (cancels ahead / behind, adds
   behind, traded at price) used by calibration. Real-order lifecycle extraction from MBO. Two fill rules: `exact`
   (default; queue ahead from order-level events, taker fills on crosses, Nasdaq hidden prints) and `et` (the et
   rule of 5.2.5, kept for et parity).
4. **L2 view synthesis** from MBO (MBP-N with configurable depth, cadence/conflation and trade-vs-depth ordering).
5. **Queue-model zoo** on the synthesized L2 view of the same day: hbt RiskAdverse, ProbQueue with Power, Power2,
   Power3, Log, Log2, hbt L3FIFO (on MBO), NT-style `queue_position` (L2), NT `prob_fill_on_limit` touch model and
   their combination, DB power model (Monte Carlo), and baselines (front-of-queue, trade-through-only,
   back-of-queue trade-rate, climatology, MMW bucket). Each model in `faithful` (bit-for-bit hbt semantics including
   quirks) or `corrected` mode. Error vs exact truth by horizon, queue-ahead decile and liquidity bucket.
6. **Calibration** against exact truth: allocation quasi-likelihood for `n` (Power/Power2/Power3) and `k` (DB),
   survival likelihood for particle models, closed-form geometric MLE for NT `prob_fill_on_limit`, latency
   distributions from live logs; exports as paste-ready hftbacktest and NautilusTrader configs plus an
   `IntpOrderLatency` `.npz`.
7. **Learned L2 fill model**: discrete-time competing-risk hazard (fill / cancel / censor), gradient-boosted,
   trained on L2 features synthesized from MBO with exact labels, isotonic-recalibrated, monotone in horizon,
   scored with proper rules.
8. **Live-log scorer**: canonical live order log (orders + events tables), importers for Binance, Bybit, OKX,
   Coinbase Advanced Trade, Kraken, Deribit, Hyperliquid, IBKR Flex, FIX 4.4 ExecutionReport drop copy; fixed-schedule
   replay; per-order 2x2 discordance with McNemar split by queue position at entry; time-to-fill comparison with
   censoring; markouts; day-block bootstrap CIs; FDR; equivalence margins; signed overall bias. The exact arm
   removes the user's own orders from the tape (self-exclusion); the primary discordance test is cluster-robust;
   importers for hftbacktest and NautilusTrader simulator outputs; `calibrate-live` fits zoo parameters to the
   user's own live fills on L2-only venues.
9. **Referee**: exact-FIFO regression oracle for other simulators, plus 16 synthetic bug-class fixtures (5.8), and
   an independent counterfactual matching-engine oracle for the shadow rules (6.2 O-SH-5).
10. **Outputs**: Parquet/Arrow tables, static self-contained HTML report, JSON summaries validated by published
    JSON Schemas shared by CLI, Python API, HTTP API and MCP.
11. **Service**: FastAPI + Postgres (data and PgQueuer queue) + S3-compatible store (SeaweedFS in compose, a local
    filesystem store for dev/tests), tenants, API keys, idempotency, RFC 9457 errors, OpenAPI, `/v1`, rate limits,
    usage metering with a Stripe adapter behind an interface (no-op biller by default), retention and deletion,
    zero-egress mode, strict per-tenant isolation.
12. **MCP server** (stdio) exposing read-mostly tools with the same JSON Schemas; long jobs as tasks or job handles.
13. **Packaging**: Rust workspace + one maturin abi3 wheel (`cp311-abi3`) containing `queuetruth._core`; a
    standalone Rust binary `queuetruth-core` for the native command subset.
14. **Quickstart data**: `queuetruth fixtures synth --out DIR` writes the synthetic quickstart dataset with truth
    tables, so pip-only users need no repository checkout.

### 1.2 Explicit non-goals for v0.1

- No strategy code, OMS, live trading, or order routing. No market impact: shadows never consume liquidity, never
  change the real book, and never see each other (independent shadows). Own-order queue interaction is a v0.2 item;
  the scorer reports how many live orders overlapped at the same price and side.
- No hidden-liquidity modelling (Nasdaq price-slid orders, icebergs, CME implied): flagged and counted only.
- FIFO venues only. Pro-rata books are rejected at ingest (`QT-CONFIG-001`).
- No auction modelling: shadows resting through an auction or halt are censored (`end_state = censored_gap`). As
  built, ingest records the trading state of Nasdaq ITCH and IEX DEEP+ (`qt.data_quality.trading_halts`) and every
  engine censors shadows there (5.2.7; finding F2, design extension halt-censoring H.6); DBN, NYSE and crypto
  datasets carry no spans yet.
- No exact truth from L2-only data. L2-only datasets support simulation and markouts only.
- DBN/CME: the reader ships, but its validation label stays `synthetic_only` until a real Databento parity run is
  done by the author (no Databento key is used by the project).
- Real ITCH bytes: both public days (2019-12-30, 2020-01-30) were stream-verified and re-run with queuetruth's own
  ITCH reader (ITCH-1, ITCH-2, ITCH-5, ITCH-O; 6.8), reproducible with `scripts/realdata/itch_subset.py` +
  `itch_realbytes.py`; claims those checks cover no longer need the "via et converted CSV" caveat, and
  the A3d hidden-print rule is checked on real P prints (superseding E14/E24). No
  other day or symbol has been run.
- No QR/analytic models (Cont-de Larrard, HLR, Lokin-Yu), no deep sequence models, no Rigtorp estimator (same family
  as ProbQueue), no Polars expression plugins, no conda-forge recipe, no free-threaded wheels, no Windows/musl
  wheels (CI skeleton builds macOS arm64 and manylinux x86_64 only).
- Service: no Modal executor, no OAuth, no Streamable-HTTP MCP endpoint, no SSO, no real Stripe calls, no pooling of
  any tenant's data or models across tenants, no storage of vendor market-data credentials, no MCP Tasks extension
  (the `mcp` SDK 2.3.0 has none; long jobs return `qt.job` handles).
- No automatic downloads. `scripts/fetch_nasdaq_itch.py` downloads only when run by a human with `--yes`.
- No market data in the repo, ever. Fixtures are synthetic or generated by our own code (section 9).

## 2. Repository layout

Cargo package names are `queuetruth-<dir>`; library names are `qt_<dir>`; directories are `crates/<dir>`.

```
queuetruth/
  Cargo.toml                 workspace: members, [workspace.dependencies] (all external deps pinned), profiles
  Cargo.lock                 committed; regenerated only on dependency changes
  rust-toolchain.toml        channel = "1.97.1" (installed here); MSRV rust-version = "1.85"
  pyproject.toml             maturin backend; package `queuetruth`; module `queuetruth._core`; deps, extras, groups
  uv.lock                    committed
  .python-version            3.13
  Makefile                   venv, develop, test-*, lint, schemas, realdata, bench targets
  LICENSE                    Apache-2.0 full text
  NOTICE                     third-party credits (section 9)
  README.md                  short; status-honesty table (what is validated on real vs synthetic data)
  SPEC.md                    pointer to docs/design/spec.md (this file); docs/design/build-plan.md
  .github/workflows/ci.yml   rust, python, service jobs
  crates/
    types/      queuetruth-types     MboEvent, Side/Action/flags, Venue + VenueRules, Instrument, ClockSemantics,
                                     LatencyModel, QtError, deterministic RNG (SplitMix64/xoshiro256**), stable_seed
    schema/     queuetruth-schema    Arrow schemas for every table (section 3), RecordBatch builders/readers for the
                                     core rows, serde mirrors of every JSON summary and every *Spec (section 4.2)
    book/       queuetruth-book      L3 FIFO book (arena + BTreeMap levels), Effect, invariants, rank queries,
                                     `reference::RefBook` (naive Vec-based book for differential tests)
    replay/     queuetruth-replay    Engine (event atomicity, sinks, observers), ShadowEngine (timeline, trajectory,
                                     amends, truth decomposition), RankOracle, real-order extraction, data quality,
                                     Parquet sinks, run_dataset (parallel by partition), Session (interactive)
    ingest/     queuetruth-ingest    Dataset (manifest, partitions, streaming reader/writer), ITCH 5.0 parser+mapper,
                                     DBN reader, etcsv reader, Parquet MBO, instruments, US/Eastern midnight
    crypto/     queuetruth-crypto    Bitstamp / Bitfinex / Coinbase L3 capture -> MboEvent; Tardis L2 CSV -> L2 rows
    l2/         queuetruth-l2        L2Builder: MBO -> L2 view (own MBP aggregate, independent of qt_book), cadence,
                                     out-of-view handling; L2 visible book used by the zoo; hbt npz event export
    zoo/        queuetruth-zoo       QueueModel trait, prob functions, every Rust zoo model, ModelId grammar, zoo runner
                                     (exact engine + L2Builder + models in one pass), predictions/fills writers,
                                     ParticleSimulator impl
    calib/      queuetruth-calib     alloc-event collection, allocation QMLE (n, k), survival fit, NT touch MLE,
                                     block bootstrap, optimizer (grid + golden section)
    features/   queuetruth-features  landmark feature + outcome extraction (shadows or real orders) -> Parquet
    testkit/    queuetruth-testkit   synthetic MBO generator with an independent reference engine and shadow ground
                                     truth; scenario DSL; synthetic DBN writer; dataset writer; referee bug-class fixtures
    cli/        queuetruth-cli       clap grammar (shared), native runner, binary `queuetruth-core`
    py/         queuetruth-py        PyO3 cdylib -> `queuetruth._core` (abi3-py311), GIL released around work
  python/
    queuetruth/
      __init__.py            re-exports the Python API (section 4.3)
      errors.py              QtError, QtApiError
      _core.pyi              type stubs for the extension
      api.py                 high-level functions (section 4.3); used by CLI, service worker, MCP local mode
      cli.py                 console script `queuetruth`: clap parse via _core, dispatch native/Python
      schemas/               models.py (Pydantic JSON summaries), specs.py (Pydantic *Spec), arrow.py (pyarrow
                             schemas mirroring qt_schema), export.py (writes schemas/json/*.schema.json)
      stats/                 mcnemar, survival (KM, Aalen-Johansen, log-rank, Gray), wilcoxon, bootstrap, fdr,
                             tost, calibration metrics (Brier, Murphy, HL, Spiegelhalter, reliability)
      eval/                  zoo evaluation vs truth (metrics, strata, CIs) and Python baselines
      model/                 learned hazard model: person-period expansion, splits, GBDT, recalibration, scoring rules
      live/                  canonical live log (schema, builder, validator), 9 importers, instrument map,
                             live -> shadow schedule, latency statistics
      score/                 scorecard assembly, markouts, position series, referee comparison
      export/                hftbacktest + NautilusTrader config text, IntpOrderLatency npz
      report/                static HTML report (inline SVG, no JS required)
      client/                HTTP client for the service (httpx)
      mcp/                   MCP stdio server (official `mcp` SDK 2.x)
  python/tests/              pytest suites per package (+ reference/ helpers, e.g. hbt_ref.py; oracle/ = the
                             independent matching-engine oracle package `qt_oracle` and O-SH-5)
  schemas/
    json/                    generated JSON Schemas (one file per qt.<name>), committed
    arrow/                   generated Arrow schema dumps (JSON), committed; Python parity test compares
  service/
    pyproject.toml           distribution `queuetruth-service` (uv workspace member)
    queuetruth_service/      app.py (FastAPI), routers/, auth.py, idempotency.py, ratelimit.py, db/ (pool,
                             migrations/*.sql, runner), queue.py (PgQueuer), worker.py, store/ (local, s3),
                             billing/ (interface, noop, stripe), retention.py, problems.py, settings.py, admin.py
    openapi.json             committed snapshot of /v1/openapi.json
    tests/                   unit, contract (schemathesis), tenancy, idempotency, queue, store, billing, zero-egress
  deploy/
    compose/docker-compose.yml   postgres:16.14-bookworm, chrislusf/seaweedfs:4.47 (S3 gateway), one-shot migrate,
                                 api, worker
    compose/.env.example         required secrets for `docker compose --env-file`
    docker/Dockerfile            multi-stage: build wheel, install wheel + service; runs as uid 10001
  docs/                      methodology, venue semantics, data handling and licensing, results (aggregates only);
                             site generator Zensical or mkdocs-material (dev dependency);
                             `scripts/check_docs.py` executes code blocks preceded by `<!-- check-docs: run -->`

  examples/                  synthetic end-to-end scripts (no market data)
  scripts/                   gen_schemas.py, realdata/ (validate_et.py, real_orders_to_live.py, merge_datasets.py,
                             et_reference.py, et_shadow_reference.py, selftest_renderers.py, expected.json),
                             fetch_nasdaq_itch.py, reference/ (generators for committed
                             statistical reference values), oracle/run_oracle.py (counterfactual oracle driver), bench/et_baseline.py
  tests/
    e2e/                     cross-component tests (CLI <-> Python API <-> HTTP parity, pipelines on synthetic data)
    fixtures/                small synthetic fixtures generated by our own code, with sha256 manifest
  benches/                   qtbench harness qtbench/ (stats, env, runner),
                             bench.py, cases.py (85 cases), check.py (regression gate), targets.json,
                             reference_et.json, schema/, baseline.json; drives the hidden
                             `queuetruth-core bench replay|l2|itch` subcommands
  .realdata/                 gitignored scratch for real-data validation outputs (never committed, never shipped)
```

Crate dependency graph (fixed; no other internal edges allowed):
`types <- schema <- {book, ingest}`; `{schema, ingest} <- {l2, crypto}`; `{book, ingest, l2} <- replay`;
`{book, replay, l2} <- zoo`; `{book, replay, l2, zoo} <- {calib, features}`; `{types, schema} <- testkit`.
A crate may depend directly on anything upstream of it (transitive closure; e.g. every crate uses `qt_types` and
`qt_schema` directly); dev-dependencies may add any crate that does not create a cycle (fixed: `book` and
`testkit` for `l2`; `testkit` for book, replay, ingest, zoo, calib, features; `ingest` and `l2` for testkit-free
replay tests). Every edge, normal and dev, is wired up front. (testkit has its own
reference engine and MUST NOT depend on qt_book, qt_replay, qt_ingest or qt_l2); every non-test crate `<- cli <- py`.
`testkit` is a dev-dependency of book, replay, ingest, l2, zoo, calib and features, and a normal dependency of `cli`
only (for `referee fixtures`).

## 3. Data contracts

### 3.1 Core Rust types (`qt_types`, implemented first)

```rust
pub type Ts = i64;   // ns since epoch, UTC
pub type Dur = i64;  // ns
pub type Px = i64;   // fixed point, 10^price_exp
pub type Qty = u64;  // size units, 10^size_exp
pub const UNDEF_PRICE: Px = i64::MAX;

pub mod flags {            // identical bit values to DBN
    pub const LAST: u8 = 1 << 7;  pub const TOB: u8 = 1 << 6;  pub const SNAPSHOT: u8 = 1 << 5;
    pub const MBP: u8 = 1 << 4;   pub const BAD_TS_RECV: u8 = 1 << 3;
    pub const MAYBE_BAD_BOOK: u8 = 1 << 2;  pub const PUBLISHER_SPECIFIC: u8 = 1 << 1;
}

#[repr(u8)] #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub enum Action { Add = b'A', Cancel = b'C', Modify = b'M', Clear = b'R', Trade = b'T', Fill = b'F', None = b'N' }
impl Action { pub const fn from_byte(b: u8) -> Option<Self>; pub const fn as_byte(self) -> u8; }

#[repr(u8)] #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub enum Side { Bid = b'B', Ask = b'A', None = b'N' }
impl Side { pub const fn from_byte(b: u8) -> Self /* unknown -> None */; pub const fn as_byte(self) -> u8;
            pub const fn opposite(self) -> Self; }

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub enum BookSide { Bid, Ask }
impl BookSide { pub const fn from_side(s: Side) -> Option<Self>; pub const fn from_byte(b: u8) -> Option<Self>;
                pub const fn opposite(self) -> Self; pub const fn as_byte(self) -> u8; }

/// One normalized MBO record (DBN MboMsg semantics, u64 size, u64 sequence).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct MboEvent {
    pub ts_event: Ts,        // venue matching-engine time (ClockSemantics::MatchTime)
    pub ts_recv: Ts,         // capture time (CaptureTime); = ts_event when the source has none
    pub instrument_id: u32,
    pub action: u8,          // Action byte
    pub side: u8,            // Side byte; on T = aggressor side; N = unknown/none
    pub price: Px,           // UNDEF_PRICE when not applicable (R, N)
    pub size: Qty,
    pub order_id: u64,       // 0 when not applicable
    pub flags: u8,           // flags::*
    pub sequence: u64,       // venue/source sequence; ITCH: one per message (et), monotone
    pub channel_id: u16,
    pub publisher_id: u16,   // 0 = unknown
}
impl Default for MboEvent { /* price = UNDEF_PRICE, action = b'N', side = b'N', rest 0 */ }
impl MboEvent { pub fn action(&self) -> Option<Action>; pub fn side(&self) -> Side;
                pub fn is_last(&self) -> bool; pub fn is_snapshot(&self) -> bool; }

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Default, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Venue { #[default] Generic, Nasdaq, Cme, Coinbase }   // rule sets; crypto L3 sources use Generic
impl std::str::FromStr for Venue { /* "nasdaq"|"itch"|"xnas"|"XNAS.ITCH" -> Nasdaq; "cme"|"glbx"|"GLBX.MDP3" -> Cme;
                                       "coinbase" -> Coinbase; "generic"|"bitstamp"|"bitfinex" -> Generic;
                                       trimmed, case-insensitive; Err = QtError::Config */ }
impl Venue { pub fn name(self) -> &'static str; }   // + Display (as built)

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum PriorityRule { KeepOnSizeDecrease, AlwaysLose }
impl PriorityRule { pub fn keeps_priority(self, old_px: Px, old_sz: Qty, new_px: Px, new_sz: Qty) -> bool; }
// KeepOnSizeDecrease: old_px == new_px && new_sz <= old_sz.  AlwaysLose: false.
// CME: an M with the same price AND the same size loses priority (iceberg refresh,
// account change), and so does an M raising the size above the remaining size after same-event F records (A6b);
// qt_book applies this on top of keeps_priority when `rules.iceberg_refresh_loses_priority` (CME): an M keeps
// priority only at the same price with a size below the post-fill remaining size (equal to it after a same-event
// F), else it loses priority (`qt_book::cme_modify_keeps_priority`).

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ValidationStatus { RealData, SyntheticOnly, Assumed, FixtureOnly }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct VenueRules {
    pub venue: Venue,
    pub modify_records_present: bool,      // false: an M is a data error (counted, ignored)
    pub modify_priority: PriorityRule,
    pub snapshot_begins_with_clear: bool,  // CME files must start with a snapshot (else anomaly)
    pub snapshot_records_flagged: bool,    // duplicate A inside a flagged snapshot is not an anomaly
    pub trades_have_aggressor: bool,
    pub supports_hidden_orders: bool,      // T with side N consumes no displayed queue
    pub fill_removal_shares_sequence: bool,// pair F with its removing C by `sequence`, else within the F_LAST event
    pub utc_midnight_snapshot: bool,
    pub sequence_meaningful: bool,         // gap/regression counters only when true
    pub validation: ValidationStatus,
    pub rule_set: RuleSet,                 // 1.1: the named rule set these values come from
    pub iceberg_refresh_loses_priority: bool, // 1.1 (A6/A6b): CME true
    pub hidden_lower_priority: bool,       // 1.1 (A3d): displayed interest outranks hidden at a price; Nasdaq true
}
pub const fn rules(v: Venue) -> VenueRules;
// rule sets are named; `crypto_l3` = generic with supports_hidden_orders =
// false (Bitfinex hidden orders opt-in and flagged); it runs on Venue::Generic record semantics.
// 1.2: `iex` = IEX DEEP+ (3.12.2): the nasdaq values with rule_set iex; it runs on Venue::Nasdaq record semantics.
pub enum RuleSet { Generic, Nasdaq, Cme, Coinbase, CryptoL3, Iex }  // names generic|nasdaq|cme|coinbase|crypto_l3|iex
pub const fn rules_for(rs: RuleSet) -> VenueRules;  // FromStr: every Venue alias + crypto_l3 / crypto-l3 + iex / iexg

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ClockSemantics { MatchTime, RecordTime, GatewayTime, CaptureTime, LocalTime, Unknown }

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TsPrecision { S, Ms, Us, Ns }

#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct Instrument {          // one row per (instrument_id, session_date)
    pub instrument_id: u32,
    pub session_date: i32,       // days since 1970-01-01 (Arrow date32)
    pub symbol: String,
    pub venue: Venue,
    pub source_venue: String,    // "nasdaq" | "cme" | "coinbase" | "bitstamp" | "bitfinex" | "binance" ...
    pub price_exp: i8,
    pub tick_size: Px,           // in price units at price_exp
    pub lot_size: Qty,           // minimum size increment in size units (shares: 1)
    pub size_exp: i8,
    pub contract_multiplier: f64,// currency per (real price unit x real size unit)
    pub currency: String,
    pub session_tz: String,      // informational IANA name
    pub midnight_utc_ns: Ts,     // authoritative local midnight of session_date, as UTC ns
    pub open_s: u32,             // continuous-session open, seconds after local midnight (Nasdaq 34200)
    pub close_s: u32,            // continuous-session close (Nasdaq 57600; crypto 86400)
    pub source: String,          // ingest format name
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LatencyModel { pub feed_ns: Dur, pub decision_ns: Dur, pub gateway_ns: Dur, pub jitter_ns: Dur }
// et semantics, normative: submit delay = spec.latency_ns + feed + decision + gateway + J_submit;
// cancel delay = spec.latency_ns + gateway + J_cancel; amend delay = spec.latency_ns + gateway + J_amend_i.
// Jitter stream per shadow (order-free, thread-safe): Xoshiro256StarStar::new(stable_seed(run_seed, &[b"latency",
// shadow_id])); draw 1 = J_submit, draw 2 = J_cancel (always drawn, even without ts_cancel), then one draw per amend
// of that shadow in amend row order; each draw = range_inclusive(0, jitter_ns). jitter_ns = 0 -> every J = 0, no
// draws. run_seed = the spec's `seed` (CLI `--seed`).

/// FNV-1a 64 (offset 0xcbf29ce484222325, prime 0x100000001b3) over: run_seed as 8 LE bytes, then for each part
/// its length as 4 LE bytes followed by its bytes; the result is passed through splitmix64 once.
pub fn stable_seed(run_seed: u64, parts: &[&[u8]]) -> u64;
pub fn splitmix64(x: u64) -> u64;  // z = x + 0x9e3779b97f4a7c15; z = (z ^ z>>30) * 0xbf58476d1ce4e5b9;
                                   // z = (z ^ z>>27) * 0x94d049bb133111eb; z ^ z>>31  (wrapping arithmetic)
/// xoshiro256** (Blackman-Vigna). new(seed): state = 4 successive outputs of the splitmix64 sequence
/// s_{i+1} = s_i + 0x9e3779b97f4a7c15 starting at seed, each mixed as splitmix64 does.
pub struct Xoshiro256StarStar { s: [u64; 4] }
impl Xoshiro256StarStar { pub fn new(seed: u64) -> Self; pub fn next_u64(&mut self) -> u64;
    pub fn next_f64(&mut self) -> f64 /* (next_u64() >> 11) as f64 * 2^-53, in [0,1) */;
    pub fn range_inclusive(&mut self, lo: u64, hi: u64) -> u64 /* lo + next_u64() % (hi - lo + 1); bias accepted */; }

#[derive(Debug, thiserror::Error)]
pub enum QtError {            // Display = "<code>: <message>"
    #[error("QT-USAGE-001: {0}")] Usage(String),
    #[error("QT-CONFIG-001: {0}")] Config(String),
    #[error("QT-INPUT-001: {path}: {msg}")] InputMissing { path: String, msg: String },
    #[error("QT-INPUT-002: {location}: {msg}")] InputFormat { location: String, msg: String },
    #[error("QT-INPUT-003: duplicate {what}: {id}")] Duplicate { what: String, id: String },
    #[error("QT-INPUT-004: {0}")] Units(String),
    #[error("QT-DATA-001: {0}")] DataUnusable(String),
    #[error("QT-DATA-002: {0}")] Strict(String),
    #[error("QT-ENGINE-001: {0}")] Engine(String),
    #[error("QT-INPUT-001: {0}")] Io(#[from] std::io::Error),
}
impl QtError { pub fn code(&self) -> &'static str; }
pub type QtResult<T> = Result<T, QtError>;
```

The Python `xoshiro` mirror in `python/tests/reference/rng.py` MUST produce identical streams; a test
compares the first 1,000 `next_u64` of seeds 0, 1 and `stable_seed(7, [b"a", b"bc"])`.

### 3.2 Venue rules (normative values, ported from et `venue-semantics`)

| field | `nasdaq` | `cme` | `coinbase` | `generic` |
|---|---|---|---|---|
| modify_records_present | false | true | true | true |
| modify_priority | AlwaysLose | KeepOnSizeDecrease | KeepOnSizeDecrease | KeepOnSizeDecrease |
| snapshot_begins_with_clear | false | true | false | false |
| snapshot_records_flagged | false | true | false | true |
| trades_have_aggressor | true | true | true | false |
| supports_hidden_orders | true | false | false | true |
| fill_removal_shares_sequence | true | false | true | true |
| utc_midnight_snapshot | false | true | false | false |
| sequence_meaningful | true | true | true | false |
| validation | RealData | SyntheticOnly | Assumed | Assumed (Bitstamp/Bitfinex tolerance-checked in et) |

`docs/venue-semantics.md` paraphrases et's semantics notes with links; it does not copy CME wiki text.

1.1 rows: `iceberg_refresh_loses_priority` = nasdaq false,
cme true, coinbase false, generic false; `hidden_lower_priority` = nasdaq true, others false. Column `crypto_l3`
(Bitstamp, Bitfinex, Coinbase `full` L3 captures; 1.2: Coinbase Exchange `level3`, Kraken `level3`, Hyperliquid
node outputs; M25 resolved by E38): the `generic` values with `supports_hidden_orders = false`; `validation`
Assumed. The dataset manifest's `rule_set` (3.10) selects it; `ingest --venue generic` resolves to `crypto_l3` for
every crypto L3 format (`bitstamp`, `bitfinex`, `coinbase`, `coinbase_level3`, `kraken_l3`, `hyperliquid_node`;
not `tardis_l2`), `qt_crypto::write` records it in the manifest (schema 1.2; a manifest without `rule_set`
replays as `generic`), and replay reads
`manifest.rule_set` (`qt_replay::run`, `rules_for(effective_rule_set())`). (1) CME: an `M` keeps priority only on a
size decrease at the same price below the post-fill remaining size (5.1.1); (2) where `hidden_lower_priority`
holds, a side-N print can fill a shadow only under the A3d rule of 5.2.4a, governed by `HiddenPrintPolicy`
(default `behind_and_through` on Nasdaq, `ignore` elsewhere).

1.2 column `iex` (IEX DEEP+, 3.12.2): every value equal to `nasdaq` (FIFO by price, display, time; `modify_records_present`
false; `trades_have_aggressor` true; `supports_hidden_orders` true; `fill_removal_shares_sequence` true;
`sequence_meaningful` true; `hidden_lower_priority` true; `iceberg_refresh_loses_priority` false) except `rule_set`
= `iex`; `validation` RealData (one public day, 3.12.2). Each value cites DEEP+ v1.06, IEX-TP v1.26 or the IEX
Rule Book in `qt_types::IEX_RULES`. `iex` runs on `Venue::Nasdaq` records, so the manifest carries `venue = nasdaq`
and `rule_set = iex`.

### 3.3 Dataset directory (output of `ingest`, input of every engine command)

```
<dataset>/
  manifest.json            qt.dataset_manifest (section 3.10)
  instruments.parquet      qt.instruments
  mbo/date=YYYY-MM-DD/instrument_id=<u32>/part-00000.parquet     qt.mbo   (kind = "mbo")
  l2/date=YYYY-MM-DD/instrument_id=<u32>/part-00000.parquet      qt.l2    (kind = "l2": Tardis input)
  trades_source/date=.../instrument_id=.../part-00000.parquet    qt.trades (source prints when the format has them:
                                                                   etcsv trades.csv, ITCH prints; used for parity)
  data_quality.json        qt.data_quality (ingest-time counters)
```

A partition holds one instrument for one session date, rows in **source feed order** (never re-sorted). Books are
per instrument, so partitions replay independently and in parallel. Multi-file partitions use `part-00001...` in
order (the v0.1 writer always merges spills into one `part-00000.parquet`). ITCH `instrument_id` is the stock
locate code, which is per day; cross-day joins use `symbol`, and `price_exp_by_instrument` (keyed by id only)
assumes one `price_exp` per id across dates. `ingest` is single-date for etcsv; multi-date datasets (RD-9, RD-10)
are built by `scripts/realdata/merge_datasets.py` (hard links, `dataset_id` recomputed by the 3.10 rule). The
`dataset_id` hashes the `mbo`/`l2` partition files only; `trades_source` files are excluded (as built `crates/ingest/src/writer.rs:398`).

Session defaults: `session_tz` = `America/New_York` for venue `nasdaq` (and always
for ITCH), `UTC` otherwise; `open_s`/`close_s` = 34,200/57,600 (09:30-16:00 ET) for `nasdaq`, 0/86,400 otherwise;
overridable with the 1.1 fields `IngestSpec.open_s`, `close_s` (honoured by qt_ingest and qt_crypto). `midnight_utc_ns` without `session_date`: qt_ingest and qt_crypto derive the date alike
(`qt_ingest::time::session_date_of_midnight`: the local date in `tz` whose midnight it is, else the UTC date of
midnight + 12 h; negative is `QT-CONFIG-001`), and every record must then fall in that local day. Ingest records sequence gaps as `qt.data_quality.feed_gaps`; every engine (replay, why, zoo, calibrate,
features) loads them per partition (`qt_replay::ingest_dq`, `partition_gaps`, `Engine::set_feed_gaps`) and ends
live shadows `censored_gap` there; record-level sequence self-detection is off in every engine, because it
produced spurious gaps on multi-instrument feeds (2,874 of 3,000 shadows on a real day).

### 3.4 Arrow / Parquet table schemas

Notation: `name: type [null]  meaning`. `ts` = `timestamp[ns, tz="UTC"]`; `dict` = `dictionary<int8, utf8>` with
the listed values only. Column order is normative. `qt_schema::schema(name) -> Option<SchemaRef>` and
`queuetruth.schemas.arrow.SCHEMAS[name]` MUST agree exactly (field names, types, nullability, order).

Schema 1.1 layouts. Every table carries `qt.schema_version = "1.1"`. Columns added in
1.1 follow all v0.1 columns and are optional per file: a file holds either the v0.1 columns (`SCHEMAS[name]`,
`qt_schema::schema`) or all columns (`FULL_SCHEMAS[name]`, `qt_schema::full_schema`); readers accept both
(`conform`), and `upgrade` adds missing 1.1 columns (derived where the v0.1 columns imply them, else null).
Dictionary values added in 1.1 are appended, so v0.1 dictionary indices are unchanged. 1.1 columns
(`ADDED_COLUMNS`): `qt.shadow_results` ts_planned_end, planned_end_kind; `qt.shadow_fills` liquidity;
`qt.zoo_predictions` liquidity dict(maker,taker) [null], status dict(hbt_312_abort) [null] (null = an ordinary
prediction; `hbt_312_abort` = a row of a faithful hbt pair at or after its #312 abort, 5.3.2); `qt.zoo_fills` liquidity; `qt.features` midnight_utc_ns
int64 [null]; `qt.live_orders` replaces_order_key utf8 [null]; `qt.score_orders` price_exp, size_exp int8 [null];
`qt.score_fills` side, instrument_id, fee, fee_ccy, price_exp, size_exp; `qt.score_markouts` mo_bps, mo_net_ccy
float64 [null]. 1.1 dictionary values: `qt.live_orders.tif` `gtd`, `qt.live_orders.validation_status` `simulated`,
`qt.live_events.source` `sim`. New registered table: `qt.zoo_shadows`.

```
qt.instruments            one row per (instrument_id, session_date)
  instrument_id: uint32 | session_date: date32 | symbol: utf8 | venue: dict(nasdaq,cme,coinbase,generic)
  source_venue: utf8 | price_exp: int8 | tick_size: int64 | lot_size: uint64 | size_exp: int8
  contract_multiplier: float64 | currency: utf8 | session_tz: utf8 | midnight_utc_ns: int64 | open_s: uint32
  close_s: uint32 | source: utf8

qt.mbo                    one row per normalized MBO record (MboEvent field order)
  ts_event: ts | ts_recv: ts | instrument_id: uint32 | action: uint8 | side: uint8 | price: int64 | size: uint64
  order_id: uint64 | flags: uint8 | sequence: uint64 | channel_id: uint16 | publisher_id: uint16

qt.bbo                    after an F_LAST event; mode "change" = only when any of the 6 values changed, "full" = every event
  ts_event: ts | instrument_id: uint32 | sequence: uint64 | bid_px: int64 [null] | bid_sz: uint64 | bid_ct: uint32
  ask_px: int64 [null] | ask_sz: uint64 | ask_ct: uint32

qt.depth                  long format, after each F_LAST event when depth_levels > 0, only levels whose (price,size,count) changed
  ts_event: ts | instrument_id: uint32 | sequence: uint64 | side: uint8 | level: uint8 (0 = best)
  price: int64 [null] (null = level vacated) | size: uint64 | count: uint32

qt.trades                 one row per T record (derived by replay) or source print (trades_source)
  ts_event: ts | instrument_id: uint32 | sequence: uint64 | price: int64 | size: uint64 | aggressor_side: uint8
  kind: dict(displayed,hidden,cross) | maker_order_id: uint64 [null]
  # hidden = side N on a venue with supports_hidden_orders; cross = ITCH cross prints (Q). ITCH `C` with
  # printable = N produces no T and no source print. ITCH Q prints are `cross` in both
  # trades_source and replay-derived trades; replay takes the cross sequences from trades_source, counts them in
  # `source_specific.cross_prints` (INFO) and never treats them as hidden prints or shadow fill volume (rev
  # 2026-10-19 final: E48, OC-28 resolved).

qt.rank_trace             one row per checked execution (rank oracle on); et rank_trace.csv semantics
  ts_event: ts | instrument_id: uint32 | sequence: uint64 | order_id: uint64 | side: uint8 | price: int64
  rank: uint32 | rank_sweep_adjusted: uint32 | level_depth: uint32 | sweep_id: uint64 (per-partition counter from 1)
  exec_kind: dict(at_price,off_price)

qt.real_orders            one row per real resting order life (MBO), from extract_real_orders
  instrument_id: uint32 | session_date: date32 | order_id: uint64 | side: uint8 | price: int64 | size_at_add: uint64
  ts_add: ts | sequence_add: uint64 | queue_ahead_at_add: uint64 (displayed qty ahead at add) | rank_at_add: uint32
  level_qty_at_add: uint64 | ts_first_fill: ts [null] | ts_last_fill: ts [null] | filled_qty: uint64 | n_fills: uint32
  n_reduces: uint32 | ts_end: ts [null] | end_reason: dict(filled,cancelled,replaced,cleared,open_at_end)
  in_snapshot: bool    # added inside a flagged snapshot: queue position unknown, excluded from scoring by default

qt.shadow_spec            input: one row per shadow order
  shadow_id: utf8 | instrument_id: uint32 | side: uint8 (B|A) | price_mode: dict(abs,touch)
  price: int64 [null] (required when abs) | offset_ticks: int32 (touch mode: 0 join same-side best, -k k ticks
  behind/passive, +k k ticks improving; ignored for abs) | qty: uint64 | ts_submit: ts | latency_ns: int64 (>= 0)
  ts_cancel: ts [null] (send time) | group: utf8 [null] | source: dict(sampled,live_log,real_order_mirror,user)
  order_key: utf8 [null] (link to qt.live_orders)

qt.shadow_amend           input: priority-keeping size reductions only
  shadow_id: utf8 | ts_send: ts | new_qty: uint64 (total order qty after amend; must be < current qty)

qt.shadow_results         output: one row per shadow
  shadow_id: utf8 | instrument_id: uint32 | side: uint8 | price: int64 [null] (null if never resolved) | qty: uint64
  ts_submit: ts | ts_live: ts (target activation time ts_submit + submit delay; always set)
  ts_activated_event: ts [null] (ts_event of the first event the shadow saw) | ts_cancel_effective: ts [null]
  queue_ahead_at_live: uint64 [null] | rank_at_live: uint32 [null] | level_qty_at_live: uint64 [null]
  ts_first_fill: ts [null] | ts_last_fill: ts [null] | ts_full_fill: ts [null] | filled_qty: uint64 | n_fills: uint32
  queue_ahead_at_first_fill: uint64 [null]
  first_fill_reason: dict(queue_depleted,trade_through,crossed,crossed_at_live,never)
  end_state: dict(filled,cancelled,amended_to_filled,censored_session_end,censored_clear,censored_gap,
                  open_at_end,rejected_would_cross,rejected_no_touch)
  latency_applied_ns: int64 (= ts_live - ts_submit) | ambiguous_race: bool | out_of_session: bool
  dq_flags: uint32 (bit 1 = partition verdict warnings, bit 2 = partition verdict unusable; other bits 0 in v0.1)
  ts_planned_end: ts [null] (1.1) | planned_end_kind: dict(cancel,session_close,none) (1.1)
  # planned end = min(cancel effective time, session close) (A2, E36); also still written to
  # `shadow_planned_end.parquet` (5.1.6).

qt.shadow_fills           output: per-fill timeline (fixes et's first-fill-only output)
  shadow_id: utf8 | fill_seq: uint32 (1-based) | ts_event: ts | sequence: uint64 | fill_qty: uint64 | cum_qty: uint64
  leaves_qty: uint64 | fill_px: int64 | reason: dict(queue_depleted,trade_through,crossed,crossed_at_live)
  queue_ahead_before: uint64 | traded_at_px_event: uint64 | trade_through_event: uint64
  behind_volume_executed: uint64   # displayed volume executed this event against orders behind the shadow
  liquidity: dict(maker,taker) (1.1)
  # A5 final: `crossed_at_live` (the shadow arrives into a crossing book) is the
  # only taker fill: one row per opposite level walked, fill_px = that level's price. Every other fill, including
  # `crossed` (a resting shadow crossed later by an opposite arrival), is a maker fill at the shadow's own price.

qt.queue_trajectory       output: one row per (shadow, event) where anything at the shadow's level changed
                          (mode "ahead": only rows where queue_ahead, rank or filled changed, plus kind=live/end)
  shadow_id: utf8 | ts_event: ts | sequence: uint64 | kind: dict(live,event,end)
  queue_ahead_before: uint64 | queue_ahead_after: uint64 | rank_before: uint32 | rank_after: uint32
  level_qty_before: uint64 | level_qty_after: uint64      # displayed qty at the price, excluding the shadow
  cancel_ahead_qty: uint64 | cancel_behind_qty: uint64 | add_behind_qty: uint64
  traded_at_px_qty: uint64 | trade_through_qty: uint64 | filled_qty: uint64
  # kind=live: ts_event = ts_live, before == after == state at activation. kind=end: ts_event = time the shadow
  # left (fill, cancel, censor), all deltas 0.

qt.l2                     L2 view (synthesized from MBO by L2Builder, or ingested from Tardis)
  ts_event: ts | ts_recv: ts | instrument_id: uint32 | event_id: uint64 (monotone per partition, one per emitted batch)
  sequence: uint64 (source F_LAST record) | kind: dict(depth,trade,snapshot,clear) | side: uint8
  price: int64 [null] (null for clear) | prev_qty: uint64 | new_qty: uint64 | prev_ct: uint32 [null] | new_ct: uint32 [null]
  trade_qty: uint64 | aggressor: uint8 | level: uint8 (rank after update, 255 = out of view) | flags: uint8
  # flags: 1 LAST_IN_BATCH, 2 OUT_OF_VIEW (left top-N, not emptied), 4 INTO_VIEW, 8 SNAPSHOT, 16 CONFLATED, 32 HIDDEN_TRADE

qt.zoo_predictions        one row per (model_id, shadow_id, horizon) for horizons_ns plus h_max_ns; truth columns null
                          on L2-only datasets
  model_id: utf8 | shadow_id: utf8 | qty: uint64 | horizon_ns: int64 | p_fill: float64 (P first fill within horizon of ts_live)
  e_fill_qty: float64 (expected filled qty by horizon) | model_ts_first_fill: ts [null] (deterministic models; for
  particle models the median over particles, null if < 50% filled by h_max) | truth_filled: bool [null]
  truth_ts_first_fill: ts [null] | truth_filled_qty: uint64 [null] | censored: bool (ts_live + horizon >
  ts_admin_end of qt.zoo_shadows, independent of the fill outcome; false when there is no
  exact arm) | queue_ahead_at_live: uint64 [null] | level_qty_at_live: uint64 [null]
  visible_level_qty_at_live: uint64 | out_of_view_at_live: bool | instrument_id: uint32
  session_date: date32 | ts_live: ts | trade_vol_60s: uint64 (displayed volume traded in the instrument in the 60 s
  before ts_live; liquidity strata)

qt.zoo_fills              per-fill timeline per model (deterministic models; particle models: not written)
  model_id: utf8 | shadow_id: utf8 | fill_seq: uint32 | ts_event: ts | fill_qty: float64 | cum_qty: float64
  queue_ahead_est_before: float64 | reason: dict(queue,trade_through,crossed,crossed_at_live,touch_draw)
  quirk: dict(none,hbt_312,hbt_nan_reset)   # faithful-mode quirk that fired on this fill/order

qt.zoo_trajectory         optional (write_trajectory): model estimate vs exact, one row per L2 batch touching the level
  model_id: utf8 | shadow_id: utf8 | ts_event: ts | queue_ahead_est: float64 | queue_ahead_exact: uint64

qt.zoo_shadows            one row per zoo shadow: planned life for A2 evaluation (registered
                          in qt_schema and schemas/arrow)
  shadow_id: utf8 | ts_cancel_planned: ts [null] (exogenous cancel effective time) | ts_admin_end: ts [null]
  (administrative end: session close, clear, gap, end of data) | instrument_id: uint32 | session_date: date32
  ts_live: ts | session_close: ts | exact_end_state: utf8 [null] | never_cancel: bool (planned end null or
  >= ts_live + h_max)

qt.zoo_errors             metric table (Python eval, section 5.6)
  model_id: utf8 | metric: utf8 | horizon_ns: int64 [null] | stratum_kind: dict(all,queue_decile,liquidity_bucket,instrument)
  stratum: utf8 | value: float64 | ci_lo: float64 [null] | ci_hi: float64 [null] | n: uint64

qt.alloc_events           calibration input (section 5.4 C1); one row per (shadow, event) with cancel_ahead + cancel_behind > 0
  shadow_id: utf8 | instrument_id: uint32 | session_date: date32 | ts_event: ts | front: float64 (exact ahead before)
  back: float64 (exact level qty before minus front) | ahead_cancel: float64 | behind_cancel: float64
  l2_prev: float64 [null] | l2_new: float64 [null] | l2_trades: float64 [null]   # what an L2 model saw in that batch

qt.touch_records          calibration input (section 5.4 C3); one row per (shadow, at-price opposite-aggressor trade);
                          rows are kept up to and including the shadow's first-fill event, and truth_first_fill_here is
                          false when the first fill's reason is not queue_depleted (as built)
  shadow_id: utf8 | ts_event: ts | touch_index: uint32 (1-based) | nt_front_before: float64 | nt_excess: float64
  truth_filled_now: bool | truth_first_fill_here: bool

qt.features               one row per (order, landmark); section 5.5
  order_ref: utf8 (shadow_id or "<instrument_id>:<order_id>") | population: dict(shadows,real_orders)
  instrument_id: uint32 | session_date: date32 | ts_landmark: ts | landmark_ns: int64 | side_sign: int8
  <feature columns, section 5.5, float64, nullable where stated; inserted at index 7 by qt_schema::features_schema>
  event_ns: int64 (time from landmark to event or censoring) | cause: dict(fill,cancel,censor)

qt.live_orders            canonical live log, one row per order chain (section 3.8)
qt.live_events            canonical live log, one row per lifecycle event (section 3.8)

qt.score_orders           one row per scored order
  order_key: utf8 | instrument_id: uint32 [null] | session_date: date32 | side: dict(buy,sell) | limit_px: int64 | qty: uint64
  ts_entry: ts (exchange-time entry used for both arms) | live_filled: bool | live_filled_qty: uint64 | live_ts_first_fill: ts [null]
  sim_filled: bool | sim_filled_qty: uint64 | sim_ts_first_fill: ts [null] | class: dict(both,neither,live_only,sim_only)
  live_end: dict(filled,cancelled,expired,rejected,open) | queue_ahead_at_entry: uint64 [null]
  queue_ahead_source: dict(exact,l2_visible,none) | queue_decile: uint8 [null] (0..9) | liquidity_bucket: uint8 [null] (0..2)
  ttf_live_ns: int64 [null] | ttf_sim_ns: int64 [null] | overlap_same_px: bool | sim_model_id: utf8
  trade_vol_60s: uint64 [null] | legs: uint32 (shadow legs after amend splitting)

qt.score_fills            per fill, both arms; 1.1 columns: side dict(buy,sell), instrument_id uint32 [null], fee
                          float64 [null], fee_ccy utf8 [null], price_exp int8 [null], size_exp int8 [null]
  order_key: utf8 | arm: dict(live,sim) | fill_seq: uint32 | ts: ts | qty: uint64 | px: int64 | liquidity: dict(maker,taker,unknown)

qt.score_markouts         per fill x horizon x reference
  order_key: utf8 | arm: dict(live,sim) | fill_seq: uint32 | horizon_ns: int64 | ref: dict(mid,microprice)
  mo_ticks: float64 [null] | mo_ccy: float64 [null] | mm_ticks: float64 [null]   # null past end of data

qt.score_position         position step series per arm
  arm: dict(live,sim) | instrument_id: uint32 | ts: ts | position: int64 (signed size units)

qt.score_tests            every hypothesis test computed (section 5.7)
  test_id: utf8 | family: dict(primary,secondary) | stratum_kind: utf8 | stratum: utf8 | statistic: utf8
  value: float64 | p_value: float64 [null] | p_adj: float64 [null] | adj_method: dict(none,holm,bh,by)
  ci_lo: float64 [null] | ci_hi: float64 [null] | n: uint64 | note: utf8 [null]

qt.hazard_predictions     learned-model output per (order_ref, landmark, horizon)
  order_ref: utf8 | landmark_ns: int64 | horizon_ns: int64 | cif_fill_raw: float64 | cif_fill: float64 (recalibrated)
  cif_cancel: float64 | survival: float64 | y_fill: bool [null] | censored: bool | split: dict(train,calib,test)
```

### 3.5 Book snapshot / BBO in Rust (`qt_book`)

```rust
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub struct LevelView { pub price: Px, pub total: Qty, pub count: u32 }
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub struct Bbo { pub bid: Option<LevelView>, pub ask: Option<LevelView> }
#[derive(Clone, Debug, PartialEq, Eq)] pub struct RestingOrder { pub order_id: u64, pub size: Qty, pub ts_recv: Ts, pub sequence: u64 }
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Effect {                 // et semantics
    None,                         // T, F, N, C/M for an unknown order (counted), or an unchanged M off CME
    Add { order_id: u64, side: BookSide, price: Px, size: Qty },
    Remove { order_id: u64, side: BookSide, price: Px, size: Qty },
    Reduce { order_id: u64, side: BookSide, price: Px, by: Qty },          // priority kept
    Replace { order_id: u64, side: BookSide, old_price: Px, old_size: Qty, new_price: Px, new_size: Qty }, // lost priority
                                  // (incl. a CME same-price same-size M)
    Clear,
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct Invariants { pub cancels_exceeding_resting: u64, pub fills_exceeding_resting: u64,
    pub fills_unknown_order: u64, pub unknown_order_refs: u64, pub duplicate_adds: u64,
    pub unexpected_modifies: u64, pub crossed_books_after_last: u64, pub negative_spreads: u64 }
// as built additions: Book: Clone; Book::best_price, level_total, error(); Invariants::add, nonzero;
// IdHasher/IdMap/IdSet
```

### 3.6 Shadow inputs in Rust (`qt_schema::rows`; readers in `qt_schema::codec`; used by qt_replay)

```rust
pub enum PriceSpec { Abs(Px), Touch { offset_ticks: i32 } }
pub struct ShadowSpec { pub shadow_id: String, pub instrument_id: u32, pub side: BookSide, pub price: PriceSpec,
    pub qty: Qty, pub ts_submit: Ts, pub latency_ns: Dur, pub ts_cancel: Option<Ts>, pub group: Option<String>,
    pub source: ShadowSource, pub order_key: Option<String> }
pub enum ShadowSource { Sampled, LiveLog, RealOrderMirror, User }
pub struct ShadowAmend { pub shadow_id: String, pub ts_send: Ts, pub new_qty: Qty }
pub enum EndState { Filled, Cancelled, AmendedToFilled, CensoredSessionEnd, CensoredClear, CensoredGap, OpenAtEnd,
                    RejectedWouldCross, RejectedNoTouch }   // mirrors qt.shadow_results.end_state
// codec::read_shadow_specs(path: &Path) -> QtResult<Vec<ShadowSpec>>;   // Parquet qt.shadow_spec, or CSV with et's
// shadows.csv header (shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns,ts_cancel) -> price_mode abs
// codec::read_shadow_amends(path: &Path) -> QtResult<Vec<ShadowAmend>>;
```

### 3.7 L2 rows in Rust (`qt_schema::rows`)

```rust
pub enum L2Kind { Depth, Trade, Snapshot, Clear }
pub mod l2flags { pub const LAST_IN_BATCH: u8 = 1; pub const OUT_OF_VIEW: u8 = 2; pub const INTO_VIEW: u8 = 4;
                  pub const SNAPSHOT: u8 = 8; pub const CONFLATED: u8 = 16; pub const HIDDEN_TRADE: u8 = 32; }
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct L2Row { pub ts_event: Ts, pub ts_recv: Ts, pub instrument_id: u32, pub event_id: u64, pub sequence: u64,
    pub kind: L2Kind, pub side: Side, pub price: Option<Px>, pub prev_qty: Qty, pub new_qty: Qty,
    pub prev_ct: Option<u32>, pub new_ct: Option<u32>, pub trade_qty: Qty, pub aggressor: Side, pub level: u8, pub flags: u8 }
```

### 3.8 Canonical live order log

Directory `<live>/` with `orders.parquet` (`qt.live_orders`), `events.parquet` (`qt.live_events`),
`manifest.json` (`qt.live_manifest`). Keyed by `order_key`. Written only through
`queuetruth.live.write_canonical_log`.

```
qt.live_orders
  order_key: utf8 (kit-assigned "<venue>:<account>:<venue_order_id or root client_order_id>") | venue: utf8
  account: utf8 | instrument: utf8 (venue symbol) | instrument_id: uint32 [null] (mapped via instrument map)
  venue_order_id: utf8 [null] | client_order_id: utf8 [null] | client_order_id_chain: list<utf8> [null] (FIX replace chain)
  side: dict(buy,sell) | order_type: dict(limit,market,stop,stop_limit,other) | tif: dict(gtc,ioc,fok,post_only,gtx,day,other)
  post_only: bool | reduce_only: bool [null] | orig_qty: uint64 | qty_str: utf8 | limit_px: int64 [null] | px_str: utf8 [null]
  price_exp: int8 | size_exp: int8 | strategy_tag: utf8 [null] | validation_status: dict(real,fixture_only) | raw: large_utf8 [null]

qt.live_events
  order_key: utf8 | event_seq: uint32 (1-based, monotone within order) | event_type: dict(submit_sent,ack,reject,
    post_only_reject,amend_sent,amend_ack,amend_reject,cancel_sent,cancel_ack,cancel_reject,fill,fill_bust,
    fill_correct,expire,restate,done)
  qty: uint64 [null] (fill: last qty; amend: new total qty) | px: int64 [null] (fill price or new limit)
  leaves_qty: uint64 [null] | cum_qty: uint64 [null] | fill_id: utf8 [null] | ref_fill_id: utf8 [null]
  liquidity: dict(maker,taker,unknown) [null] | fee: float64 [null] (positive = cost) | fee_ccy: utf8 [null]
  amend_keeps_priority: bool [null] | ts_local_send: ts [null] | ts_exchange: ts [null] | ts_local_recv: ts [null]
  ts_exchange_kind: dict(match_time,record_time,gateway_time,unknown) | ts_precision: dict(s,ms,us,ns)
  source: dict(ws,rest,fix_dropcopy,flex,csv,synthetic) | reason: utf8 [null] | derived: bool (e.g. fill derived from
  cumulative-qty differencing) | raw: large_utf8 [null]
```

Validator rules (`queuetruth.live.validate`, errors are `QT-INPUT-002/003`): unique `order_key`; `event_seq`
contiguous from 1; `fill_id` unique per `(venue, instrument, order side, fill_id)` after dedupe, so both sides of
a self-trade are kept;
`cum_qty` equals the running sum of `fill` qty minus busts (corrections replace); the order's current total qty
(`orig_qty`, or the latest `amend_ack.qty`) `== cum_qty + leaves_qty` after every fill when both present (else
warning); at least one of the three timestamps per event; `ts_exchange_kind` and
`ts_precision` always set; fills of a `post_only` order with `liquidity = taker` are a warning.

Importer conventions : ack rows carry `qty` =
order total, `px` = limit, `leaves_qty` = total, `cum_qty` = 0; fills come only from per-fill sources; a
user-initiated cancel is `cancel_ack`, an exchange-initiated end (IOC remainder, STP, GTD expiry) is `expire`, a
post-only would-cross is `post_only_reject`; orders open when capture started are `restate`, never `ack`;
order-state exchange times are `record_time`, execution times `match_time`; fills-only sources build the order row
from the fills (`orig_qty` = sum of fills, `order_type`/`tif` = `other`, `limit_px` null) and the log is flagged
fills-only (5.7.5 refuses fill-rate tests on it); canonical fee sign is positive = cost (OKX `fillFee` and IBKR
`ibCommission` negated). GTD maps to `tif = other` until `gtd` exists (3.11). A replace that assigns a new venue
order id (FIX 150=5 with new 37, Hyperliquid modify) is recorded in the event `reason` text until the replace-link
field exists (3.11).

Simulator logs: `queuetruth.live.import_sim(kind, ...)` with `kind` in
{`hftbacktest`, `nautilus`} (also `live import --venue sim_hftbacktest|sim_nautilus`) writes a canonical log with
`venue = "sim:<kind>"`, `source = sim`, `validation_status = simulated`; `.leg<k>` ids are folded back into one
order with amend events. Simulator logs are identified by the `sim:` venue prefix (the scorer and referee detect it
there, with `source` as a fallback); a real venue name may not start with `sim:`.

### 3.9 Instrument map for live logs

CSV `instrument_map.csv`: `venue,instrument,instrument_id,session_date(optional),price_exp,size_exp,tick_size,lot_size`
(`tick_size`, `lot_size` as decimal strings). Without a map, importers use `price_exp = -9`, `size_exp = -8`
(crypto and FIX) or `0` (IBKR equities), `instrument_id = null`; decimal strings convert exactly or fail
`QT-INPUT-004`. A derived value that is not representable (Coinbase merged-execution VWAP) is written null with a
warning.

### 3.10 JSON documents (JSON Schemas in `schemas/json/qt.<name>.schema.json`)

Source of truth: Pydantic v2 models in `python/queuetruth/schemas/models.py` (summaries) and `specs.py` (specs),
written first. `scripts/gen_schemas.py` exports JSON Schema (draft 2020-12) and is run by `make schemas`;
a test fails if the committed files differ. Rust mirrors (`qt_schema::json`, `qt_schema::specs`) serialize to the
same field names and order; a Python test validates Rust-produced JSON against the schemas. Fields below are
required unless marked `?` (nullable/optional). `Files` = list of `{name, rows?, bytes, sha256}`.

| Schema | Fields |
|---|---|
| `qt.error` | code, message, hint?, details (object) |
| `qt.dataset_manifest` | dataset_id (hex sha256 of: the sorted list of every partition file's sha256, newline-joined, then a newline and `source_format`; identical rule for every writer incl. qt_testkit), kind (`mbo`/`l2`), source_format, venue, source_venue, inputs [{path, bytes, sha256}] (provenance only), partitions [{instrument_id, session_date, files [{path, rows, sha256}], rows, ts_min, ts_max}], price_exp_by_instrument {id: exp}, created_by, validation_status (`real_data`/`synthetic_only`/`assumed`/`fixture_only`) |
| `qt.data_quality` | records, maybe_bad_book_records, bad_ts_recv_records, snapshot_records, pre_snapshot_rows_skipped, unknown_order_refs, duplicate_adds, unexpected_modifies, sequence_gaps, sequence_regressions, out_of_order_ts_event, crossed_books_after_last, hidden_prints, off_price_executions, feed_gaps [{instrument_id, ts_start, ts_end, reason}], source_specific {str: int} (only non-zero counters; replay adds `unterminated_events`, `files_without_snapshot`, `removal_after_unpaired_fill` (M24); INFO counters that never affect the verdict: `itch_zero_share_cross`, `cross_prints`, `dbn_instrument_id_remapped` (`qt_ingest::dq::INFO_SOURCE_COUNTERS`)), verdict (`clean`/`warnings`/`unusable`; rule 5.1.5), reasons [str] |
| `qt.ingest_summary` | spec (IngestSpec echo), manifest (qt.dataset_manifest), data_quality, files (Files), timings? |
| `qt.replay_summary` | run_id (hex16, section 7.3), engine_version, spec (echo), stats {records, events, trades, instruments, partitions, snapshot_records}, invariants (8 counters), rank_oracle? {executions_checked, executions_unknown_order, price_priority_violations, fifo_rank_histogram[6], fifo_rank_histogram_sweep_adjusted[6], rank0_share, rank0_share_sweep_adjusted, sweeps, sweeps_multi_fill, sweep_gap_ns, first_60s_after_open {checked, rank_violations, rank_violations_sweep_adjusted, rank0_share, rank0_share_sweep_adjusted}, rank0_share_by_bucket {"HH:MM": {checked, rank0, rank0_sweep_adjusted, rank0_share, rank0_share_sweep_adjusted}}}, parity? {trades_compared, trades_mismatches, trades_missing_derived, trades_extra_derived, bbo_compared, bbo_mismatches, first_mismatches [object], pass}, shadows? {n, n_live, n_filled, n_partial_only, n_cancelled, n_censored, n_rejected, fills_rows, trajectory_rows}, data_quality, files, warnings [str], timings? {wall_s, records_per_s} |
| `qt.shadow_sample_summary` | spec, n, by_instrument {id: n}, by_placement {offset: n}, files |
| `qt.zoo_summary` | run_id, spec, n_shadows, models [model_id], leaderboard [{model_id, brier {h: v}, logloss {h: v}, fill_rate_bias {h: {value, ci_lo, ci_hi}}, overfill_bias {value, ci_lo, ci_hi}, qty_bias, ttf_log_ratio_median?, ttf_iad, qtrack_mae?, mcnemar {b, c, p_exact}}] (never-cancel shadows only, 5.6; `fill_rate_bias` is the AJ figure), files, warnings; 1.1 (additive, default {}): hbt_312_aborts {model_id: n_pairs} (5.3.2); 0.1.1 (additive): leaderboard items `exact_fill_rate {h: v}` and `model_fill_rate {h: v}` (default {}; `fill_rate_bias = model_fill_rate - exact_fill_rate`), `n_excluded_hbt_312` (default 0), `n_leaderboard` (default null) |
| `qt.calibration_result` | run_id, spec, kind, family, mode, param_name? (`n` for power families with estimate and CIs in n, not ln n; `k` databento; `p` NT), estimate?, ci_profile? [lo, hi], ci_bootstrap? [lo, hi], loglik?, n_events, n_shadows, train {dates|blocks}, test {dates|blocks}, held_out {model_id: {brier {h: v}, logloss {h: v}, fill_rate_bias {h: v}}}, baseline_held_out (same shape, default parameters), stratified [{stratum_kind, stratum, estimate, ci_lo?, ci_hi?, n}], latency? {entry_ns {p10,p50,p90,p99,n}, response_ns {...}, cancel_ns {...}, amend_ns {...}, clock_sync_assumed: bool}, exports {hftbacktest {python, notes [str]}, nautilus {python, notes [str]}, latency_npz?}, data_sources [{dataset_id, venue, source_venue, validation_status}], warnings |
| `qt.feature_summary` | spec, n_orders, n_rows, by_cause {fill, cancel, censor}, feature_names [str], privileged_names [str], files |
| `qt.hazard_model_card` | model_id, format_version (1), spec, grid_ns [int], causes ["fill","cancel"], feature_names, privileged (bool), split {scheme, train, calib, test}, n_orders {train, calib, test}, n_rows_train, scores_test {rcll, ipcw_logloss {h: v}, brier {h: v}, bss_vs_climatology {h: {value, ci_lo, ci_hi}}, ibs, calib_slope {h: v}, calib_intercept {h: v}, spiegelhalter_z {h: v}, hl_p {h: v}, c_index_uno (labelled improper), auc {h: v} (improper)}, zoo_comparison? {model_id: {brier {h: v}}}, recalibration ("isotonic"/"none"), files, warnings |
| `qt.live_import_summary` | venue, inputs [{path, sha256}], n_orders, n_events, n_fills, dedupe_dropped, unmatched_fills, warnings, validation_status, files |
| `qt.live_manifest` | venues [str], accounts [str], n_orders, n_events, instrument_map_sha256?, created_by |
| `qt.scorecard_summary` | the cluster-robust primary test is the 1.1 field `discordance_clustered` (also the `qt.score_tests` row `discordance_clustered`); `fills_only` is a 1.1 field; the `clusters:` / `fills_only:` warning prefixes remain for older readers. Fields: run_id, spec, live_source, sim_source {kind, model_id?, path?}, n_orders, n_matched, n_unmatched_live, n_unmatched_sim, n_overlap_same_px, two_by_two {both, neither, live_only, sim_only}, bias {fill_rate_bias, ci_lo, ci_hi, relative_overfill, qty_bias, qty_bias_ci_lo, qty_bias_ci_hi, direction (`sim_overfills`/`sim_underfills`/`no_significant_bias`)}, mcnemar {b, c, chi2_cc, p_chi2, p_exact, odds_ratio?, or_ci_lo?, or_ci_hi?}, by_queue_decile [{decile, both, neither, live_only, sim_only, p_exact, p_adj}], by_liquidity [same shape, key bucket], ttf {n_joint, median_log_ratio?, ci_lo?, ci_hi?, wilcoxon_p?, paired_perm_p?, logrank_p?, gray_p?, cif_fill {arm: {h: v}}}, markouts [{horizon_ns, ref, arm, n, mean_ticks, ci_lo, ci_hi}], markout_diff [{horizon_ns, ref, n, mean_diff_ticks, ci_lo, ci_hi, p_adj}], sim_only_vs_live_only_markout [{horizon_ns, sim_only_mean, live_only_mean, diff, ci_lo, ci_hi}], equivalence {fill_rate_margin, tost_p, fill_rate_equivalent, markout_margin_ticks, markout_horizon_ns, markout_tost_p, markout_equivalent}, fdr {method, q, n_tests, n_rejected}, verdict {headline, signed_bias, gates_passed (bool), failed_gates [str]}, data_quality?, files, warnings |
| `qt.referee_report` | (written as `<out>/referee_report.json`) spec, simulator {name, version?}, n_orders, n_both_filled, discordant {sim_only, exact_only}, qty_mismatch, max_abs_ttf_err_ns?, ttf_err_p50_ns?, by_reason {reason: n}, tolerance, pass, examples [{order_key, exact {...}, sim {...}}] (first 20) |
| `qt.why_trace` | subject {shadow_id? , order_key?}, instrument_id, side, price, qty, ts_live?, outcome {filled_qty, ts_first_fill?, end_state}, events [{ts_event, sequence, action, order_id, side, price, size, effect, is_ahead, queue_ahead_after, rank_after, level_qty_after, fill_qty, note?}] (capped at `max_events`, default 5000, with `truncated` bool) |
| `qt.report_summary` | inputs [path], out, sections [str], bytes, sha256 |
| `qt.job` | id, kind (`ingest`/`replay`/`calibration`/`scorecard`/`report`/`dataset_validate`/`deletion`), state (`queued`/`running`/`succeeded`/`failed`/`canceled`/`expired`), progress {fraction, stage}, resource {type, id}, created_at, started_at?, finished_at?, error? (problem), usage {meter: qty}, files_expire_at? |
| `qt.file_manifest` | job_id, files [{name, bytes, sha256, content_type, url, expires_at}] |
| `qt.estimate` | kind, records_est, shadows_est, cpu_seconds_est, bytes_out_est, cost_units, eta_seconds |
| `qt.problem` | RFC 9457: type, title, status, detail?, instance?, code, request_id, errors? [{loc, msg}] |
| `qt.export_summary` | target, files, text? |
| `qt.fixtures_summary` | out, fixtures [{name, path, files}] |
| `qt.live_validation` | path, n_orders, n_events, errors [str], warnings [str] |
| `qt.doctor` | python, platform, machine, core_version, schemas_ok, postgres?, docker?, problems [str] |
| `qt.bench` | data, records, wall_s [float], records_per_s_median |
| `qt.dq_report` | (1.2, `queuetruth dq`, written as `<out>/dq_report.json` next to `dq_report.html`) engine_version, spec (DqSpec echo, 4.2), dataset {path, dataset_id?, kind?, source_format?, venue?, source_venue?, rule_set?, validation_status?, partitions, records, session_dates [str], symbols [str]}, compare? (same shape), status (`pass`/`warn`/`fail`: the worst check; `skip` counts as pass), gate_failed (status reached `spec.fail_on`), counts {pass, warn, fail, skip}, checks [{id, title, status (`pass`/`warn`/`fail`/`skip`), summary, metrics {str: any}, evidence [{name, columns [str], rows [[any]], total, truncated}], notes [str]}] (ids in report order: `dq_counters`, `sequence`, `crossed_book`, `stale_book`, `orphan_refs`, `size_price`, `trade_parity`, `timestamps`, `session_coverage`, `symbology`, `snapshots`, `vintage`, `cross_source`, `rate_drift`), files (Files: the HTML page), warnings [str] |
| `qt.dataset`, `qt.upload`, `qt.api_key`, `qt.api_key_created`, `qt.usage_report`, `qt.tenant`, `qt.meta`, `qt.health`, `qt.page` | defined in section 4.5; `qt.api_key_created.secret` is nullable (null in idempotent replays) |
| `qt.<spec>_spec` (16 files: calibration, dq (1.2), export, feature, hazard_fit, ingest, live_calibration, live_import, mirror, referee, replay, report, scorecard, shadow_sample, why, zoo) | JSON Schemas of the 4.2 spec objects, generated from `specs.py` (as built) |

Schema 1.1 document fields (all optional, absent in 1.0 documents; JSON
`schema_version` stays the integer 1):
- `qt.dataset_manifest`: `rule_set?` (`generic`/`nasdaq`/`cme`/`coinbase`/`crypto_l3`; 1.2: `iex`), `source_vintage?` [{sha256,
  dbn_version?, dataset?, dbn_schema?, start_ns?, end_ns?}] (DBN metadata).
- `qt.replay_summary`: `shadow_diagnostics?` {hidden_print_fills, hidden_at_px_with_display_ahead, taker_fills,
  crossed_unfilled, behind_exec_with_ahead, excluded_requested, excluded_matched, excluded_records, twins_linked,
  twin_fills_with_queue_ahead, twin_fills_off_display} (twin counters: 5.2.4a).
- `qt.zoo_summary`: `hbt_312_aborts` {model_id: n_pairs} (default {}; 5.3.2).
- `qt.calibration_result`: `kind` adds `live`; `held_out_discordance?` {n, b, c, fill_rate_bias, ci_lo, ci_hi,
  p_exact, p_clustered, clustered_method, n_clusters}; `trust?` {verdict (`exportable`/`refused`), reasons [refusing
  tokens in order], flags [every trust token, sorted], beats_default {h: `better`/`tie`/`worse`/`mixed`/`n/a`}}.
- `qt.live_import_summary`: `coverage?` {fills_only, n_fills_only_orders}, `entry_fallback_share?` (float),
  `unmapped_instruments?` [{venue, instrument, n_orders}], `instrument_scales?`; `qt.live_manifest`: `coverage?`,
  `entry_fallback_share?`.
- `qt.scorecard_summary`: `n_twin_unmatched?`, `twin_match_rate?`, `discordance_clustered?` {method, estimate,
  ci_lo, ci_hi, p_value, n_clusters, n_clusters_discordant, design_effect, n_effective}, `n_taker_fills?`,
  `fills_only?`.
- `qt.hazard_model_card.scores_test`: `hl_p_et?` {h: v} (8 df, et parity) next to `hl_p` (IPCW, 5.5.7).
- `qt.live_calibration_test` (new registered document): model_id, grid, n, b, c, fill_rate_bias, ci_lo, ci_hi,
  p_exact, p_clustered, clustered_method, n_clusters.
- `qt.api_key_created.secret` is nullable (null in idempotent replays) in the shared model and JSON Schema.
- Ratified as built (E38 Section D): `LeaderboardEntry.ttf_iad`, `qty_bias` and `McNemar.p_chi2` are nullable;
  `RefereeSpec.simulator` defaults to `{name: "unknown"}`.

Timestamps inside service documents (`created_at` etc.) are RFC 3339 UTC strings; inside analysis documents they are
integer ns. `{h: v}` maps use the horizon in ns as a decimal string key.

### 3.11 Schema 1.1 (implemented)

(replacing the "planned" table of the first pass; all 22 planned items landed.) Schema
1.1 is additive. Arrow tables carry `qt.schema_version = "1.1"` (`qt_schema::ARROW_SCHEMA_VERSION`); JSON documents
keep the integer `schema_version: 1` (`qt_schema::SCHEMA_VERSION`). New columns, dictionary values and table are
listed in 3.4, new document fields in 3.10, new spec fields in 4.2, new `VenueRules` fields and the `crypto_l3`
rule set in 3.1/3.2.

Contract (E40): every spec field whose value differs from its default is honoured or raises `QT-CONFIG-001`; no
field is silently ignored. Run identity (7.3): a 1.1 spec field holding its default is dropped from the identity
document before hashing (`qt_schema::V1_1_DEFAULTS`, `drop_v1_1_defaults`; mirror
`queuetruth.schemas.runid.V1_1_DEFAULTS`), so a spec that does not set it keeps its v0.1 `run_id`, and
`schemas/run_id_kat.json` is kept, not regenerated; a non-default value is hashed like any field; summary echoes
keep every field. The table is keyed by a field that identifies the spec type (`sweep_gap_ns` ReplaySpec,
`write_fills` ZooSpec, `max_events` WhySpec, `target` ExportSpec, `markout_horizons_ns` ScorecardSpec, `fix`
LiveImportSpec, `format` IngestSpec, `gbdt` HazardFitSpec, `max_why` ReportSpec, `family` LiveCalibrationSpec)
and covers: ReplaySpec `fill_rule` ("exact"), `hidden_print_policy` (null), `exclude_order_ids` ([] and per item
`session_date` null); ZooSpec `amends` (null), `exclude_order_ids`; WhySpec `exclude_order_ids`; ExportSpec `force`
(false); ScorecardSpec `window_ns` (60 s), `match_window_ns` (1 ms), `self_exclude` (true), `markout_past_end`
(null); LiveImportSpec `tz` (null), `fix.venue_name` (null), `fix.checksum` ("exclude"), `fix.data_dictionary`
(null), `fix.session_venues` ({}), `fix.instrument_tag` (null); IngestSpec `open_s`, `close_s` (null);
HazardFitSpec `zoo_shadows` (null); ReportSpec `leaderboard_sort_horizon_ns` (null); LiveCalibrationSpec
`self_exclude` (true), `match_window_ns` (1 ms).

Not in 1.1 (requested, open): `ZooSpec`/CLI flag for the calibrate-live grid (R-E4); a `qt.zoo_summary` headline
(R-G2). (The scorer writes `qt.score_markouts`, `qt.score_orders` and `qt.score_fills` in the 1.1 layout, with
`mo_bps`/`mo_net_ccy` and `price_exp`/`size_exp` filled.)

### 3.12 Schema 1.2

Schema 1.2 is additive like 1.1 (JSON `schema_version` stays 1). It adds:

- `IngestSpec.format` values `coinbase_level3`, `kraken_l3`, `hyperliquid_node` (`qt_crypto`, 4.1) and
  `iex_deep_plus` (`qt_ingest::iex`, 3.12.2). The crypto ones take their tuning through `options` (4.1 `qt_crypto`),
  not new spec fields, so no `run_id` default table entry is needed.
- `qt.dq_report` and its spec `DqSpec` (`qt.dq_spec`) for `queuetruth dq` (3.10, 4.2, 4.4), registered in
  `queuetruth.schemas` (`DOC_MODELS`, `SPEC_MODELS`; re-exported by `queuetruth.dq.model`) with JSON Schemas and
  samples in `schemas/`. They are Python-only (no `qt_schema` mirror; listed in the Rust round-trip test's
  `PYTHON_ONLY`), and `DqSpec` is not hashed into a `run_id`.
- `RuleSet::Iex` (`iex`) and the manifest `rule_set` value `iex` (3.1, 3.2, 3.10).
- `qt.data_quality.source_specific` INFO counters (never affect the verdict; `qt_ingest::dq::INFO_SOURCE_COUNTERS`):
  `iex_modify_maintain_priority`, `iex_modify_reset_priority`, `iex_modify_noop`, `iex_duplicate_messages`,
  `iex_other_protocol_segments`.
- Crypto ingest warnings: `qt_crypto::Prepared::notes` (each source's exactness caveats in plain words) are written
  as ingest warnings (`qt.ingest_summary.warnings`, and `qt.data_quality.reasons` as `warning: ...`).

#### 3.12.1 Crypto L3 sources

Per-format capture layout, record mapping and exactness limits: `docs/how-to/crypto-l3-sources.md`. In short:
`coinbase_level3` is exact from a REST level-3 snapshot until the next sequence gap (a later snapshot re-seeds);
`kraken_l3` is exact only inside the subscribed depth window (10/100/1000 levels per side, CRC32-checked after every
message; `window_truncated_depth` is always in `source_specific`, so a Kraken dataset is never `clean`; no execution
records, so fills cannot be attributed); `hyperliquid_node` is exact from an L4 snapshot, with fills attributed to
maker and taker `oid`s from `node_fills`. A product or coin without a snapshot is warm-up only (a whole-range feed
gap).

#### 3.12.2 IEX DEEP+

`iex_deep_plus`: IEX HIST DEEP+ (feed code `DPLS`, published T+1 at IEX HIST as one gzipped pcapng capture of the
multicast stream per trading day) read order by order by `qt_ingest::iex`. Sources: IEX DEEP+ Specification v1.06
(2026-10-07/08; Message Protocol ID 0x8005) and IEX Transport Specification v1.26 (2026-06-02; IEX-TP framing and
sequencing); rule values cite the Investors Exchange Rule Book (2026-10-02). Guide and real-data run:
`docs/how-to/iex-public-demo.md`.

- Layers: `capture` (pcap / pcapng, gzip or raw; inflation cap raised to 512 GiB for this format) -> `transport`
  (Ethernet / IPv4 / UDP, IEX-TP header and message blocks) -> `feed` (A/B duplicate removal and sequencing per
  channel and session) -> `message` (DEEP+ layouts) -> `mapper` (MBO records and source prints); `synth` builds
  synthetic captures for tests.
- Spec: `venue` must resolve to rule set `iex` (`iex` or `iexg`), else `QT-CONFIG-001`; `tz` America/New_York (the
  `nasdaq` default of 3.3, records go to the US/Eastern date of `ts_event`; session 09:30-16:00 unless `open_s` /
  `close_s`); `price_exp` in -12..=-4 (DEEP+ prices have 4 decimals, scaled exactly; default -9); sizes in shares;
  tick default 0.01; no `instruments` file. `source_venue` = `iex`; the manifest carries `venue = nasdaq`,
  `rule_set = iex`.
- Records: `sequence` = the IEX-TP message sequence number (the `T`/`F`/`C` of one message share it), so sequence
  gaps are real feed gaps (`sequence_meaningful`), detected over the whole feed before the symbol filter;
  `channel_id` = IEX-TP Channel ID; `ts_event` = the message Timestamp (UTC ns); `ts_recv` = the segment Send Time
  (the Timestamp when the Send Time is earlier, counted `iex_send_time_before_event`); `F_LAST` on the last record
  of a message per instrument; `instrument_id` = order of first appearance of the symbol in the whole feed.
- Mapping: Add Order `a` -> `A`; Order Modify `M` with Modify Flags bit 0 = 1 (maintain priority) at the same price
  and a smaller size -> `C` of the decrease (priority kept, Rule 11.220(a)(3)); same price and size -> nothing
  (`iex_modify_noop`); bit 0 = 0 (reset) -> `C` of the remaining size + `A` (same id, back of the queue); bit 0 = 1
  with a new price or a larger size -> `C` + `A`, counted `iex_modify_maintain_unrepresentable`; to size 0 -> `C`;
  Order Delete `R` -> `C` of the remaining size; Order Executed `L` -> `T` (aggressor = opposite of the resting
  side) + `F` + `C` of the resting order (with the single-price-cross flag 0x08 the `T` has side `N`, print kind
  `cross`); Trade `T` (non-displayed against non-displayed) -> `T` side `N` (hidden print); Clear Book `C` -> `R`;
  system, directory, status, halt, short-sale, retail-liquidity, security-event and trade-break messages -> no
  records. `iex` datasets carry no `M` records.
- Not visible in DEEP+: non-displayed orders and the hidden parts of reserve orders (their executions are hidden
  prints), routed executions.
- Validation: `real_data` on the strength of 2026-10-09 (`20261009_IEXTP1_DPLS1.0.pcap.gz`, SPY, AAPL, NVDA, TSLA;
  `scripts/iex/stream_subset.py`): full stream 330,703,432 DEEP+ messages, 0 sequence gaps, 0 duplicates; ingest
  4,435,281 records, verdict `clean`; replay invariants all 0; rank oracle 21,301 displayed executions, every one at
  FIFO rank 0, 0 price-priority violations; control: treating the 1,742 same-price reset-priority modifies as
  priority-keeping puts 33 executions behind the head. The channelized variant `DPLC` (16 channels, since
  2026-08-07) uses the same messages and is sequenced per channel, but no `DPLC` day has been run.
- Licensing: IEX HIST data may be redistributed with IEX's attribution line (`qt_ingest::iex::ATTRIBUTION`, terms
  `TERMS_URL`; section 9 and `docs/data-and-licensing.md`). No IEX data is committed.

## 4. Public APIs

### 4.1 Rust crate APIs (cross-crate contract; signatures fixed before implementation)

Bodies not listed here are private. Every fallible function returns `QtResult<T>`.

**`qt_schema`**
```rust
pub const SCHEMA_VERSION: u32 = 1;
pub fn schema(name: &str) -> Option<arrow_schema::SchemaRef>;           // "qt.mbo", ... (section 3.4)
pub fn schema_names() -> &'static [&'static str];
pub fn schema_json(name: &str) -> Option<String>;                       // dump used by schemas/arrow/*.json
pub mod rows;    // ShadowSpec, PriceSpec, ShadowSource, ShadowAmend, L2Row, L2Kind, l2flags, BboRow, TradeRow,
                 // DepthRow, RankTraceRow, RealOrderRow, ShadowResultRow, ShadowFillRow, TrajectoryRow,
                 // ZooPredictionRow, ZooFillRow, ZooTrajectoryRow, AllocEventRow, TouchRecordRow, FeatureRow
                 // (dynamic columns; features_schema(names) inserts them at index 7) (as built)
pub mod codec;   // fn mbo_to_batch(&[MboEvent]) -> RecordBatch; fn batch_to_mbo(&RecordBatch) -> QtResult<Vec<MboEvent>>;
                 // same pair for every *Row type; fn read_shadow_specs(&Path); fn read_shadow_amends(&Path)
pub mod io;      // pub struct TableWriter { fn create(path, name) -> QtResult<Self> (unknown name: QT-ENGINE-001);
                 //   fn write(&mut self, &RecordBatch) -> QtResult<()>; fn finish(self) -> QtResult<FileEntry> }
                 //   (Parquet, settings of section 0.1)
                 // pub fn read_table(path) -> QtResult<TableReader> (TableReader: Iterator<Item =
                 //   QtResult<RecordBatch>>; checks qt.schema) (as built)
                 // pub fn sha256_file(path) -> QtResult<String>;  pub struct FileEntry { name, rows: Option<u64>, bytes, sha256 }
pub mod json;    // serde mirrors of every analysis summary in section 3.10 (field order = schema order); of the
                 // service documents only qt.problem and qt.job have mirrors (Open conflicts)
pub mod specs;   // serde mirrors of every spec in section 4.2
pub mod duration;// parse "100ms" | "1s" | "10s" | "1m" | "5m" | "250us" | "<integer ns>" -> Dur
```

**`qt_book`**
```rust
pub struct Book;
impl Book {
    pub fn new(rules: VenueRules) -> Self;
    pub fn rules(&self) -> &VenueRules;
    pub fn apply(&mut self, e: &MboEvent) -> Effect;                      // only A, C, M, R change the book
    pub fn best(&self, side: BookSide) -> Option<LevelView>;
    pub fn bbo(&self) -> Bbo;
    pub fn level(&self, side: BookSide, price: Px) -> Option<LevelView>;
    pub fn levels(&self, side: BookSide) -> impl Iterator<Item = LevelView> + '_;   // best first
    pub fn depth(&self, side: BookSide, n: usize) -> Vec<LevelView>;
    pub fn orders_at(&self, side: BookSide, price: Px) -> impl Iterator<Item = &RestingOrder> + '_;  // FIFO
    pub fn order(&self, order_id: u64) -> Option<(BookSide, Px, &RestingOrder)>;
    pub fn queue_ahead_of(&self, order_id: u64) -> Option<(Qty, u32)>;   // displayed qty and order count ahead
    pub fn order_rank(&self, order_id: u64, consumed: &[u64], ahead: Option<&mut Vec<u64>>) -> Option<Rank>;
    pub fn note_event_end(&mut self);                                      // crossed / negative-spread counters
    pub fn invariants(&self) -> &Invariants;
    pub fn audit(&self) -> Result<(), String>;    // O(n): level totals/counts, no empty level, locators consistent
    pub fn order_count(&self) -> usize;
}
pub struct Rank { pub side: BookSide, pub price: Px, pub rank: u32, pub level_depth: u32, pub best_px: Px }
pub mod reference { pub struct RefBook; /* same apply/bbo/level/levels/orders_at/queue_ahead_of, Vec-based, O(n) */ }
```

**`qt_ingest`**
```rust
pub enum DatasetKind { Mbo, L2 }
pub struct Dataset { pub root: PathBuf, pub manifest: json::DatasetManifest, pub instruments: Vec<Instrument> }
impl Dataset {
    pub fn open(root: &Path) -> QtResult<Self>;
    pub fn partitions(&self) -> &[Partition];                              // sorted (session_date, instrument_id)
    pub fn instrument(&self, instrument_id: u32, session_date: i32) -> Option<&Instrument>;
    pub fn rules(&self) -> VenueRules;
    pub fn partition_for(&self, instrument_id: u32, ts: Ts) -> Option<&Partition>;   // by local session date
}
pub struct Partition { pub instrument_id: u32, pub session_date: i32, pub files: Vec<PathBuf>, pub rows: u64,
                       pub ts_min: Ts, pub ts_max: Ts, pub kind: DatasetKind }
impl Partition {
    pub fn mbo(&self) -> QtResult<Box<dyn Iterator<Item = QtResult<MboEvent>> + Send>>;   // streaming, feed order
    pub fn l2(&self) -> QtResult<Box<dyn Iterator<Item = QtResult<L2Row>> + Send>>;
    pub fn source_trades(&self) -> QtResult<Option<Vec<TradeRow>>>;
}
pub struct DatasetWriter;
impl DatasetWriter {
    pub fn create(root: &Path, kind: DatasetKind, venue: Venue, source_format: &str) -> QtResult<Self>;
    pub fn set_instruments(&mut self, v: Vec<Instrument>);
    pub fn add_input(&mut self, path: &Path) -> QtResult<()>;              // records sha256 for the manifest
    pub fn push_mbo(&mut self, e: &MboEvent) -> QtResult<()>;              // routes by (instrument, local date)
    pub fn push_l2(&mut self, r: &L2Row) -> QtResult<()>;
    pub fn push_source_trade(&mut self, t: &TradeRow) -> QtResult<()>;
    pub fn finish(self, dq: json::DataQuality) -> QtResult<json::IngestSummary>;
    // as built:
    pub fn set_source_venue(&mut self, v: &str);  pub fn set_validation_status(&mut self, v: ValidationStatus);
    pub fn set_memory_rows(&mut self, n: usize);  pub fn set_threads(&mut self, n: usize);
    pub fn add_input_file(&mut self, f: InputFile);
    pub fn route(&self, instrument_id: u32, ts: Ts) -> QtResult<i32>;     // session date for a record
    pub fn push_mbo_at(&mut self, e: &MboEvent, session_date: i32) -> QtResult<()>;
    pub fn push_l2_at(&mut self, r: &L2Row, session_date: i32) -> QtResult<()>;
    pub fn push_source_trade_at(&mut self, t: &TradeRow, session_date: i32) -> QtResult<()>;
    pub fn partition_keys(&self) -> Vec<(i32, u32)>;
}
impl Partition { pub fn source_trades_dir(&self) -> Option<PathBuf>; }
pub fn write_json<T: serde::Serialize>(root: &Path, name: &str, doc: &T) -> QtResult<FileEntry>;
pub mod dq;      // MboDqScan {new, record, add_source, counters_mut, finish}; apply_verdict(&mut DataQuality,
                 // impl Into<Option<u64>>) (E21): the one verdict function of 5.1.5 (ingest passes None, replay
                 // the event count, qt_crypto None); qt_replay::dq re-exports it with UNUSABLE_RATIO and the
                 // INFO_SOURCE_COUNTERS list and adds only dq_flags and the partition merge
pub mod guard;   // decompression guard (8.10): GuardLimits, GuardedReader, open_maybe_gzip, ...
pub mod units;   // exact decimal -> fixed point: decimal_to_fixed, decimal_to_units, rescale, pow10
pub fn ingest(spec: &specs::IngestSpec) -> QtResult<json::IngestSummary>; // itch | dbn | etcsv | parquet
pub mod itch;    // Reader (gz/raw framing), parse, encode, encode_file(messages, path, gzip) for synthetic fixtures,
                 // Mapper (et convert semantics -> MboEvent + TradeRow)
pub mod dbn;     // DBN MBO reader (dbn crate), multi-file chronological, zstd; records decoded as stored
                 // (VersionUpgradePolicy::AsIs) after a framing check (FrameCheck: metadata <= 256 MiB, record
                 // length >= 16 and a multiple of 8), because the dbn crate panics on malformed input; an
                 // undefined ts_recv is an error
pub mod etcsv;   // et mbo.csv / trades.csv / instruments.csv readers
pub mod time;    // us_eastern_midnight_utc_ns(date) (DST rules as et `time.rs`), date parsing, parse_date,
                 // format_date, midnight_utc_ns(date, SessionTz), session_date_of, tz_from_name
```

**`qt_crypto`**: `pub fn ingest(spec: &specs::IngestSpec) -> QtResult<json::IngestSummary>` for formats
`bitstamp | bitfinex | coinbase | tardis_l2` and (1.2) `coinbase_level3 | kraken_l3 | hyperliquid_node`; modules
`bitstamp`, `bitfinex`, `coinbase`, `tardis` and (1.2) `coinbase_l3`, `kraken_l3`, `hyperliquid` (shared helpers
in `l3util`) each exposing a
pure converter `fn convert(lines: impl Iterator<Item = QtResult<String>>, cfg: &Config) -> QtResult<(Vec<MboEvent>|Vec<L2Row>, Vec<TradeRow>, Counters)>`
(`Counters = BTreeMap<String, u64>`), plus `prepare(spec) -> QtResult<Prepared>` and `write(&Prepared, out)`.
Rules: `ts_recv` falls back to `ts_event` when the capture has no receive time; Coinbase
`full` is deduplicated by `(product_id, sequence)` (counter `duplicates_dropped`); `source_specific` keeps only
non-zero counters; default `size_exp = -8`, tz `UTC`, `open_s = 0`, `close_s` = length of the day.
`options` (anything else is `QT-CONFIG-001`): `taker_window_us` (bitstamp; integer or null), `flags`, `max_size`
(bitfinex), `warmup_depth` (coinbase, default 10), `currency` (all); 1.2: `depth` (kraken_l3: 10\|100\|1000,
default the subscribe acknowledgement's depth, else 10 with `depth_assumed`), `checksum_precision` (kraken_l3:
`{symbol: [price_decimals, qty_decimals]}` from Kraken's `instrument` channel, used to rebuild the CRC32 input),
`snapshot_block` (hyperliquid_node: block height of a single L4 snapshot; default `capture_meta.json`
`snapshot_block`, else the digits of the snapshot file name), `max_block_gap_ms` (hyperliquid_node: a `block_time`
step above it is a feed gap; default 5000, 0 = off). Input resolution: a capture directory expands to its data
files in the source's read order (`coinbase_level3`: `book`, `level3`, `heartbeat`; `kraken_l3`: `level3`) plus
`capture_meta.json`; for `hyperliquid_node` directories are walked recursively (at most 6 levels) and files named
`l4*.json[.gz]` are L4 snapshots (`Inputs::snapshots`, read whole, at most 1 GiB inflated). `Prepared` gains
`notes: Vec<String>` (1.2): the source's exactness caveats (window truth, warm-up products, no execution records),
which `write` adds as ingest warnings (summary `warnings` and `data_quality.reasons`); `write` also records
`Prepared::rule_set` as the manifest's `rule_set` (3.2), which 0.1 did not.

**`qt_replay`**
```rust
pub enum BboMode { None, Change, Full }
pub enum TrajectoryMode { None, Ahead, Full }
pub struct EngineConfig { pub rules: VenueRules, pub latency: LatencyModel, pub seed: u64, pub rank_oracle: bool,
    pub sweep_gap_ns: Dur, pub bbo: BboMode, pub depth_levels: u16, pub trajectory: TrajectoryMode,
    pub race_window_ns: Dur, pub real_orders: bool, pub strict: bool }
impl EngineConfig { pub fn new(rules: VenueRules) -> Self; }
// as built:
#[derive(Default)] pub enum FillRule { #[default] Exact, Et }            // 5.2.4a / 5.2.5
pub enum HiddenPrintPolicy { Ignore, Behind, BehindAndThrough }        // default_for(&VenueRules): Nasdaq
impl HiddenPrintPolicy { pub fn default_for(r: &VenueRules) -> Self; } //   BehindAndThrough, else Ignore
pub struct ReplayOptions { pub exclude_order_ids: Vec<(u32, u64)>, pub fill_rule: FillRule,
    pub hidden_print_policy: Option<HiddenPrintPolicy> }               // built from the 1.1 ReplaySpec fields
pub fn run_dataset_with(spec: &specs::ReplaySpec, opts: &ReplayOptions) -> QtResult<json::ReplaySummary>;
pub struct ShadowDiagnostics { pub hidden_print_fills: u64, pub hidden_at_px_with_display_ahead: u64,
    pub taker_fills: u64, pub crossed_unfilled: u64, pub behind_exec_with_ahead: u64,
    pub excluded_requested: u64, pub excluded_matched: u64, pub excluded_records: u64 }
pub struct PlannedEnd { pub shadow_id: String, pub planned_end: Option<Ts>, pub kind: &'static str }
pub fn fill_liquidity(r: rows::FillReason) -> &'static str;            // crossed_at_live -> "taker", else "maker"
                                                                       //
pub fn ingest_dq(ds: &Dataset) -> Option<json::DataQuality>;            // ingest-time data quality (feed_gaps)
pub fn partition_gaps(gaps: &[json::FeedGap], part: &Partition) -> Vec<json::FeedGap>;  // (E59: used by zoo, calib,
                                                                       // features, why and replay)
pub fn strict_violations(s: &json::ReplaySummary) -> Vec<String>;
// Engine setters: set_fill_rule, set_hidden_print_policy, set_cross_prints (ITCH Q sequences, E48),
// set_excluded_orders(impl IntoIterator<Item = (u32, u64)>),
// set_sequence_gap_detection, set_feed_gaps; shadow_planned_ends() -> Vec<PlannedEnd>; stats() -> &Stats.
pub enum ShadowStatus { Pending, Live, Done }
pub struct ShadowState { pub status: ShadowStatus, pub price: Option<Px>, pub ts_live: Ts, pub queue_ahead: Option<Qty>,
    pub rank: Option<u32>, pub level_qty: Option<Qty>, pub filled: Qty, pub qty: Qty, pub end_state: Option<EndState> }
pub struct Stats { pub records: u64, pub events: u64, pub trades: u64, pub instruments: u64, pub partitions: u64,
    pub snapshot_records: u64, pub out_of_order_ts_event: u64, pub sequence_gaps: u64, pub sequence_regressions: u64,
    pub unterminated_events: u64, pub files_without_snapshot: u64, pub hidden_prints: u64, pub off_price_executions: u64,
    pub removal_after_unpaired_fill: u64, pub cross_prints: u64 }
pub trait Sink {                                       // every method defaults to Ok(())
    fn bbo(&mut self, r: &BboRow) -> QtResult<()>;  fn trade(&mut self, r: &TradeRow) -> QtResult<()>;
    fn depth(&mut self, r: &DepthRow) -> QtResult<()>;  fn rank_trace(&mut self, r: &RankTraceRow) -> QtResult<()>;
    fn shadow_fill(&mut self, r: &ShadowFillRow) -> QtResult<()>;  fn trajectory(&mut self, r: &TrajectoryRow) -> QtResult<()>;
    fn shadow_result(&mut self, r: &ShadowResultRow) -> QtResult<()>;  fn real_order(&mut self, r: &RealOrderRow) -> QtResult<()>;
}
pub struct ShadowEventTruth { pub shadow_idx: u32, pub activated_now: bool, pub ended_now: Option<EndState>,
    pub ahead_before: Qty, pub ahead_after: Qty, pub rank_before: u32, pub rank_after: u32,
    pub level_qty_before: Qty, pub level_qty_after: Qty, pub cancel_ahead: Qty, pub cancel_behind: Qty,
    pub add_behind: Qty, pub traded_at_px: Qty, pub trade_through: Qty, pub filled_now: Qty }
pub struct EventEnd<'a> { pub instrument_id: u32, pub ts_event: Ts, pub sequence: u64, pub book: &'a Book,
    pub records: &'a [MboEvent], pub effects: &'a [Effect], pub shadow_truth: &'a [ShadowEventTruth] }
pub trait EventObserver: Send {                        // defaults are no-ops
    fn on_event_start(&mut self, instrument_id: u32, ts_event: Ts, book: &Book) {}
    fn on_record(&mut self, e: &MboEvent, effect: &Effect, book: &Book) {}
    fn on_event_end(&mut self, end: &EventEnd<'_>) {}
    fn on_finish(&mut self) {}
}
pub struct Engine;
impl Engine {
    pub fn new(cfg: EngineConfig, instruments: &[Instrument], shadows: Vec<ShadowSpec>, amends: Vec<ShadowAmend>) -> QtResult<Self>;
    pub fn add_observer(&mut self, o: Box<dyn EventObserver>);
    pub fn apply(&mut self, e: &MboEvent, sink: &mut dyn Sink) -> QtResult<()>;
    pub fn finish(self, sink: &mut dyn Sink) -> QtResult<EngineReport>;
    pub fn book(&self, instrument_id: u32) -> Option<&Book>;
    pub fn shadow_index(&self, shadow_id: &str) -> Option<u32>;
    pub fn shadow_spec(&self, idx: u32) -> &ShadowSpec;
    pub fn shadow_state(&self, idx: u32) -> ShadowState;     // status, resolved price, ts_live, queue_ahead, rank, filled
}
pub struct EngineReport { pub stats: Stats, pub invariants: Invariants, pub rank_oracle: Option<json::RankOracle>,
                          pub data_quality: json::DataQuality, pub shadows: Option<json::ShadowCounts>,
                          pub diagnostics: ShadowDiagnostics }
pub fn run_partition(part: &Partition, ds: &Dataset, cfg: &EngineConfig, shadows: Vec<ShadowSpec>,
    amends: Vec<ShadowAmend>, observers: Vec<Box<dyn EventObserver>>, sink: &mut dyn Sink) -> QtResult<EngineReport>;
pub fn for_each_partition<T: Send>(parts: &[Partition], threads: usize,
    f: impl Fn(&Partition) -> QtResult<T> + Sync) -> QtResult<Vec<T>>;     // results in partition order
pub fn split_shadows(ds: &Dataset, shadows: Vec<ShadowSpec>) -> QtResult<BTreeMap<(i32, u32), Vec<ShadowSpec>>>;
pub fn run_dataset(spec: &specs::ReplaySpec) -> QtResult<json::ReplaySummary>;   // writes section 5.1.6 outputs
pub fn sample_shadows(spec: &specs::ShadowSampleSpec) -> QtResult<json::ShadowSampleSummary>;
pub fn mirror_real_orders(spec: &specs::MirrorSpec) -> QtResult<json::ShadowSampleSummary>;
pub fn why(spec: &specs::WhySpec) -> QtResult<json::WhyTrace>;
pub struct Session;   // open(root, venue: Option<Venue>), advance_to(ts), bbo(inst), depth(inst, n) -> (bids, asks),
                      // queue_ahead(inst, side, px)
pub fn trace_shadow(events: impl IntoIterator<Item = QtResult<MboEvent>>, cfg: EngineConfig, instruments: &[Instrument],
    shadow: ShadowSpec, amends: Vec<ShadowAmend>, max_events: usize, subject: json::WhySubject) -> QtResult<json::WhyTrace>;
```

**`qt_l2`**
```rust
pub enum TradePosition { Before, After }
pub struct L2Config { pub depth_levels: u16 /* 0 = all */, pub cadence_ns: Dur /* 0 = per F_LAST event */,
    pub trade_position: TradePosition, pub include_counts: bool, pub emit_hidden_trades: bool }
impl Default for L2Config { /* 10, 0, Before, true, true */ }
pub struct L2Builder;
impl L2Builder {
    pub fn new(cfg: L2Config, rules: VenueRules, instrument_id: u32) -> Self;
    pub fn push(&mut self, e: &MboEvent, out: &mut Vec<L2Row>);   // appends rows when a batch is emitted
    pub fn flush(&mut self, ts: Ts, out: &mut Vec<L2Row>);         // end of input / forced cadence boundary
    pub fn aggregate_level(&self, side: BookSide, px: Px) -> Option<(Qty, u32)>;   // own MBP aggregate (oracle use)
}
pub struct VisibleBook;   // built ONLY from emitted L2 rows; what an L2 model can see
impl VisibleBook { pub fn new() -> Self; pub fn apply(&mut self, r: &L2Row); pub fn qty_at(&self, side: BookSide, px: Px) -> Qty;
    pub fn in_view(&self, side: BookSide, px: Px) -> bool; pub fn best(&self, side: BookSide) -> Option<(Px, Qty)>;
    pub fn levels(&self, side: BookSide, n: usize) -> Vec<(Px, Qty, Option<u32>)>; }
pub fn synthesize(ds: &Dataset, cfg: &L2Config, out_root: &Path, threads: usize) -> QtResult<Vec<FileEntry>>;
pub fn write_hbt_npz(rows: &[L2Row], inst: &Instrument, feed_latency_ns: Dur, path: &Path) -> QtResult<FileEntry>;
/// For L2-only datasets: derive qt.bbo (change mode, counts null -> 0) and qt.trades from the L2 rows (markouts, scorer).
pub fn bbo_from_l2(ds: &Dataset, out_root: &Path, threads: usize) -> QtResult<Vec<FileEntry>>;  // bbo.parquet, trades.parquet
// as built:
pub fn synthesize_partition(p: &Partition, ds: &Dataset, cfg: &L2Config) -> QtResult<Vec<L2Row>>;
pub fn map_partitions<T: Send>(parts: &[Partition], threads: usize, f: impl Fn(&Partition) -> QtResult<T> + Sync)
    -> QtResult<Vec<T>>;
pub fn bbo_trades_from_rows(rows: impl IntoIterator<Item = QtResult<L2Row>>) -> QtResult<(Vec<BboRow>, Vec<TradeRow>)>;
pub struct HbtAction { pub ts: Ts, pub submit: bool /* false = cancel */, pub order_id: u64, pub side: BookSide,
    pub price: Px, pub qty: Qty, pub gtx: bool }   // one own-order action of the O-ZOO-3 sidecar
pub fn write_hbt_npz_with_orders(rows: &[L2Row], inst: &Instrument, feed_latency_ns: Dur, path: &Path,
    actions: &[HbtAction]) -> QtResult<(FileEntry, FileEntry)>;   // <stem>.npz + <stem>.orders.json
// write_hbt_npz also writes <stem>.orders.json (empty actions); L2Builder::unresolved_aggressor_trades() counts
// side-N trades it could not side (5.3.1).
```

**`qt_zoo`** (section 5.3 defines semantics)
```rust
pub enum FillDelta { None, Qty(f64), Full }
pub enum ModelInput { L2, Mbo }
pub enum Mode { Faithful, Corrected }
pub enum Exchange { Partial, NoPartial }
pub enum Family { HbtRiskAdverse, HbtPower, HbtPower2, HbtPower3, HbtLog, HbtLog2, HbtL3Fifo, NtQueueL2, NtProbTouch,
                  NtQueueL2ProbTouch, DatabentoMc, FrontOfQueue, TradeThroughOnly }
pub struct ModelId { pub family: Family, pub params: BTreeMap<String, f64>, pub mode: Mode, pub exchange: Exchange }
impl std::fmt::Display for ModelId {}  impl std::str::FromStr for ModelId {}   // grammar in section 5.3.2
pub struct ShadowL2 { pub idx: u32, pub side: BookSide, pub price: Px, pub qty: f64, pub lot: f64 }
pub struct Particle { pub front: f64, pub cum_trade: f64, pub filled: f64, pub first_fill: Option<Ts> }
pub struct QState { pub front: f64, pub cum_trade: f64, pub leaves: f64, pub filled: f64, pub done: bool,
                    pub touches: u32, pub particles: Vec<Particle>, pub quirk: u8 /* bitset: 1 hbt_312, 2 hbt_nan_reset */ }
pub trait QueueModel: Send + Sync {
    fn id(&self) -> &ModelId;
    fn input(&self) -> ModelInput;
    fn on_arrive(&self, s: &mut QState, o: &ShadowL2, visible_level_qty: f64, rng: &mut Xoshiro256StarStar);
    fn on_trade(&self, s: &mut QState, o: &ShadowL2, ts: Ts, trade_qty: f64, aggressor: Side, rng: &mut Xoshiro256StarStar) -> FillDelta;
    fn on_depth(&self, s: &mut QState, o: &ShadowL2, ts: Ts, prev_qty: f64, new_qty: f64, flags: u8, rng: &mut Xoshiro256StarStar) -> FillDelta;
    fn on_trade_through(&self, s: &mut QState, o: &ShadowL2) -> FillDelta { FillDelta::Full }
    fn on_cross(&self, s: &mut QState, o: &ShadowL2) -> FillDelta { FillDelta::Full }
    fn on_mbo(&self, s: &mut QState, o: &ShadowL2, e: &MboEvent, effect: &Effect, book: &Book) -> FillDelta { FillDelta::None }
    fn queue_ahead(&self, s: &QState) -> f64;
    fn p_filled(&self, s: &QState) -> Option<f64> { None }       // particle fraction with filled > 0
    fn pi_behind(&self, front: f64, back: f64) -> Option<f64> { None }
}
pub mod prob { // implemented first exactly as section 5.3.3; f64 ops in the stated order
    pub fn power(n: f64, front: f64, back: f64) -> f64;  pub fn power2(n: f64, front: f64, back: f64) -> f64;
    pub fn power3(n: f64, front: f64, back: f64) -> f64; pub fn log(front: f64, back: f64) -> f64;
    pub fn log2(front: f64, back: f64) -> f64;           pub fn db_p_front(k: f64, x: f64) -> f64;
    pub fn hbt_exec_lots(front: f64, lot: f64) -> i64;    // (-front / lot).round() as i64
}
pub fn build(id: &ModelId) -> QtResult<Box<dyn QueueModel>>;
pub fn default_models(mode: Mode, exchange: Exchange) -> Vec<ModelId>;   // the "all" set, section 5.3.4
pub fn run_zoo(spec: &specs::ZooSpec) -> QtResult<json::ZooSummary>;     // writes tables; leaderboard left empty,
                                                                           // filled by queuetruth.eval (Python)
// as built: public modules l3, model_id (DEFAULT_PARTICLES, expand_models, Family::{ALL, name, from_name}),
// models (HbtModel, NtModel, DatabentoModel, BoundModel, QUIRK_*), pair, runner, sim, view
pub struct ShadowCurve { pub shadow_idx: u32, pub survival: Vec<f64> /* on horizons */, pub truth_interval: Option<usize>, pub censored_from: Option<usize> }
pub trait ParticleSimulator: Sync { fn horizons(&self) -> &[Dur];
    fn simulate(&self, theta: f64, seed: u64) -> QtResult<Vec<ShadowCurve>>; }
pub fn particle_simulator(spec: &specs::CalibrationSpec) -> QtResult<Box<dyn ParticleSimulator>>;
```

**`qt_calib`**
```rust
pub enum AllocFamily { Power, Power2, Power3, Databento }
pub struct FitOptions { pub grid_lo: f64, pub grid_hi: f64, pub grid_n: usize, pub tol: f64 }
pub struct AllocFit { pub family: AllocFamily, pub estimate: f64, pub loglik: f64, pub n_events: u64, pub profile_ci: Option<(f64, f64)>, pub grid: Vec<(f64, f64)> }
pub struct TouchFit { pub p_hat: f64, pub n_orders: u64, pub n_filled: u64, pub touches: u64 }
pub struct SurvivalFit { pub estimate: f64, pub loglik: f64, pub grid: Vec<(f64, f64)> }
pub fn collect(spec: &specs::CalibrationSpec) -> QtResult<(Vec<AllocEventRow>, Vec<TouchRecordRow>)>;
pub fn alloc_loglik(events: &[AllocEventRow], family: AllocFamily, theta: f64) -> f64;
pub fn fit_alloc(events: &[AllocEventRow], family: AllocFamily, opts: &FitOptions) -> AllocFit;
pub fn fit_nt_touch(records: &[TouchRecordRow], queue_gated: bool) -> TouchFit;
pub fn fit_survival(sim: &dyn ParticleSimulator, thetas: &[f64], seed: u64, refine: bool) -> QtResult<SurvivalFit>;
pub fn golden_section_max(f: impl Fn(f64) -> f64, lo: f64, hi: f64, tol: f64) -> (f64, f64);
pub fn block_bootstrap(n_blocks: usize, b: u32, seed: u64, stat: impl Fn(&[u32]) -> f64 + Sync) -> (f64, f64);
pub fn calibrate(spec: &specs::CalibrationSpec) -> QtResult<json::CalibrationResult>;   // kinds alloc|survival|nt_touch
                                                                         // (latency -> QT-CONFIG-001: Python only)
```

**`qt_features`**: `pub const FEATURES: &[&str]; pub const PRIVILEGED: &[&str];`
`pub fn extract(spec: &specs::FeatureSpec) -> QtResult<json::FeatureSummary>;` (writes `<out>/features.parquet`;
the summary is returned, not written). Tracker columns are named `f_<family>_<key><value>` with `.` -> `p` and
`-` -> `m` (`tracker_column`), e.g. `f_hbt_power3_n1`; `queue_share_ra` is null when `hbt_risk_adverse` is not a
tracker (as built).

**`qt_testkit`** (dev only)
```rust
pub struct SynthConfig { pub seed: u64, pub venue: Venue, pub instruments: u32, pub events_per_instrument: u64,
    pub tick: Px, pub base_px: Px, pub max_levels: u32, pub mean_size: Qty, pub p_add: f64, pub p_cancel: f64,
    pub p_reduce: f64, pub p_modify: f64, pub p_trade: f64, pub p_clear: f64, pub hidden_print_rate: f64,
    pub snapshot_at_start: bool, pub ts_start: Ts, pub mean_gap_ns: Dur }
pub struct SynthOutput { pub events: Vec<MboEvent>, pub instruments: Vec<Instrument>, pub expected_bbo: Vec<BboRow>,
    pub expected_trades: Vec<TradeRow>, pub expected_depth_final: Vec<(u32, BookSide, Px, Qty, u32)> }
pub fn generate(cfg: &SynthConfig) -> SynthOutput;           // events valid for the venue's rules; F_LAST batching
pub fn reference_shadows(events: &[MboEvent], rules: VenueRules, latency: LatencyModel, shadows: &[ShadowSpec],
    amends: &[ShadowAmend]) -> Vec<(ShadowResultRow, Vec<ShadowFillRow>, Vec<TrajectoryRow>)>;  // independent, naive
pub fn write_dataset(out: &SynthOutput, root: &Path) -> QtResult<PathBuf>;  // section 3.3 layout via qt_schema::io
pub fn encode_dbn(events: &[MboEvent], path: &Path) -> QtResult<()>;
pub mod scenario;  // Scenario::new(venue).event(ts, |e| e.add(id, side, px, qty).cancel(id, qty).modify(id, px, qty)
                   //   .trade(aggressor, px, qty).fill(id, qty).hidden_print(px, qty)).clear(ts) ... .build() -> Vec<MboEvent>
                   // (each .event(...) block becomes one F_LAST batch; venue-correct F/C pairing is generated)
pub mod bugclasses; // referee fixtures, section 5.8 (16 classes)
// as built additions: generate_sessions, try_reference_shadows, RefConfig, write_dataset_with, modules audit,
// calendar, fixtures (fixtures synth), inject, refbook, shadows; scenario extras fill_only, execute, execute_at,
// replace, record, flag, skip_sequence, snapshot, instrument, channel
// examples/make_fixtures.rs (cargo run -p queuetruth-testkit --example make_fixtures --release) writes the shared,
// committed synthetic fixtures: tests/fixtures/synth_small/ (dataset, venue cme: 2 instruments x 2 session dates,
// ~50,000 records per partition, seed 7), tests/fixtures/synth_small_nasdaq/ (same, venue nasdaq, hidden prints),
// tests/fixtures/referee/<bug_class>/, and rewrites tests/fixtures/<dir>/MANIFEST.sha256 for each directory it owns.
```

**`qt_cli`**
```rust
pub struct CliExit { pub code: i32, pub text: String, pub to_stderr: bool }   // help/version: code 0 to stdout
pub fn parse(argv: &[String]) -> Result<CliCommand, CliExit>;
#[derive(serde::Serialize, serde::Deserialize)]
pub enum OutputFormat { Human, Json }
pub struct GlobalOpts { pub format: OutputFormat, pub log_level: String, pub threads: usize, pub seed: u64, pub quiet: bool }
pub struct CliCommand { pub path: Vec<String> /* e.g. ["shadows","sample"] */, pub global: GlobalOpts,
                        pub args: serde_json::Value, pub implementation: Implementation }
pub enum Implementation { Native, Python, Hybrid }
pub fn run_native(cmd: &CliCommand) -> QtResult<NativeOutput>;
pub struct NativeOutput { pub summary: serde_json::Value, pub gate_failed: bool }
// as built: error_document, error_exit, main_with, render_human; pub mod ops (fixtures_synth, bench_replay,
// synthesize_l2, bbo_from_l2, run_id). Flags without a spec field are collected in an `args._cli` object
// (e.g. score --window, live import --tz).
```

### 4.2 Spec objects (shared by Python API, CLI, HTTP API, MCP)

Pydantic models in `python/queuetruth/schemas/specs.py`, serde mirrors in `qt_schema::specs`; unknown fields are
rejected (`extra="forbid"`, `#[serde(deny_unknown_fields)]`). `DatasetRef = {"path": str} | {"dataset_id": str}`:
local callers use `path`; the HTTP API accepts only `dataset_id` (the worker resolves it to a path). Same for
`FileRef` (`path` | `upload_id` | `job_file: {job_id, name}`). Durations are integer ns. Defaults shown. Every `out`
field is `str | null` and execution-only (excluded from `run_id` and summary echoes); the HTTP API rejects a non-null
`out` with 422 (the worker assigns a private temp directory). `threads` is execution-only too.

| Spec | Fields (default) |
|---|---|
| `IngestSpec` | format (itch\|dbn\|etcsv\|parquet\|bitstamp\|bitfinex\|coinbase\|tardis_l2; 1.2: coinbase_level3\|kraken_l3\|hyperliquid_node\|iex_deep_plus (venue must resolve to `iex`, 3.12.2)), inputs [FileRef], venue (str), source_venue (null = from format), out (str), symbols ([str]\|null), session_date ("YYYY-MM-DD"\|null), midnight_utc_ns (int\|null), tz ("America/New_York"\|"UTC"\|null; null = venue default of 3.3), instruments (FileRef\|null; etcsv instruments.csv), price_exp (-9), size_exp (null = format default: 0 qt_ingest, -8 crypto), tick_size (decimal str\|null), lot_size (decimal str\|null), options (object: e.g. bitstamp taker_window_us 100; qt_ingest `open_s`, `close_s`; crypto keys in 4.1 `qt_crypto`), strict (false; must make the verdict gate apply in every format, Open conflicts); 1.1: open_s, close_s (int\|null = venue default, 3.3) |
| `ReplaySpec` | dataset (DatasetRef), out (str\|null), venue (str\|null override), shadows (FileRef\|null), amends (FileRef\|null), latency (LatencyModel zeros), rank_oracle (false), sweep_gap_ns (2_000_000), bbo ("change"), depth_levels (0), trajectory ("ahead"), race_window_ns (0), real_orders (false), l2 (L2Config\|null = do not write l2), parity_trades (false), parity_bbo (FileRef\|null), strict (false), threads (1), seed (0); 1.1: fill_rule ("exact"\|"et", "exact"), hidden_print_policy ("ignore"\|"behind"\|"behind_and_through"\|null = venue default), exclude_order_ids ([{instrument_id, order_id, session_date (null = any)}]; the Python API also accepts bare integer ids) |
| `L2Config` | depth_levels (10), cadence_ns (0), trade_position ("before"), include_counts (true), emit_hidden_trades (true) |
| `ShadowSampleSpec` | dataset, out, n, seed (0), allocation ("equal" per instrument-day \| "proportional" to records), sides (["B","A"]), placements ([{offset_ticks:0,weight:0.6},{-1,0.2},{-2,0.1},{1,0.1}]), qty ([{kind:"lots",lots:1,weight:0.5},{kind:"median_order_size",weight:0.5}]), window_open_s (300), window_close_s (600), lifetime_ns (300_000_000_000), latency_ns (0) |
| `MirrorSpec` | dataset, out, n (3000), seed (0), stratify (true: equal per instrument x outcome {filled, cancelled}), placement ("behind_real": ts_submit = real add time, latency 0) |
| `ZooSpec` | dataset, shadows (FileRef), out, models (["all"]), mode ("faithful"), exchange ("partial"), horizons_ns ([100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000]), h_max_ns (300_000_000_000), l2 (L2Config), particles (100), seed (0), latency, write_fills (true), write_trajectory (false), threads (1), bootstrap_b (1000); 1.1: amends (FileRef\|null), exclude_order_ids (as ReplaySpec) |
| `CalibrationSpec` | kind (alloc\|survival\|nt_touch\|latency), family (power\|power2\|power3\|databento\|nt_prob_touch\|nt_queue_l2_prob_touch\|latency), dataset\|null, shadows\|null, live\|null (latency), train (WindowSpec), test (WindowSpec), out, mode ("faithful"), exchange ("partial"), l2 (L2Config), horizons_ns (as ZooSpec), h_max_ns (300_000_000_000), grid (object: alloc {lo:-3.0, hi:4.0, n:141} in ln n; databento {lo:-0.99, hi:0.99, n:199}; survival {k: 25 evenly spaced in [-0.98, 0.9]}), bootstrap ({b:200, block:"auto"\|"day"\|"30min", seed:0}; auto = day if >= 5 dates else 30min), particles (100), seed (0), threads (1) |
| `WindowSpec` | dates (["YYYY-MM-DD"]\|null) or blocks ({index:[int], n_blocks:5}\|null); null both = all data |
| `FeatureSpec` | dataset, out, population ("shadows"\|"real_orders"), shadows (FileRef\|null), n_real_orders (int\|null = all), landmarks_ns ([0, 1_000_000_000, 5_000_000_000]), cancel_away_ticks (2), h_max_ns (60_000_000_000), l2 (L2Config), trackers (["hbt_risk_adverse", "hbt_power3[n=1]", "hbt_power3[n=3]", "hbt_log2"]), privileged (true), seed (0), threads (1); `cancel_away_ticks` null or 0 = no policy cancel (default stays 2) |
| `HazardFitSpec` | features (FileRef), out, grid_ns ([1e8, 2.5e8, 5e8, 1e9, 2e9, 5e9, 1e10, 3e10, 6e10] as ints), split ({scheme:"auto"\|"days"\|"blocks", n_blocks:5, embargo_ns:60_000_000_000}), gbdt ({max_iter:300, learning_rate:0.05, max_leaf_nodes:31, min_samples_leaf:100, l2_regularization:1.0}), recalibration ("isotonic"), privileged (false), seed (0), bootstrap_b (1000), zoo_predictions (FileRef\|null: qt.zoo_predictions for the same shadows, fills `zoo_comparison`); 1.1: zoo_shadows (FileRef\|null: qt.zoo_shadows of the zoo run; needs `zoo_predictions`, else `QT-CONFIG-001`; null = the sibling `zoo_shadows.parquet` if present) |
| `LiveImportSpec` | venue (binance\|bybit\|okx\|coinbase\|kraken\|deribit\|hyperliquid\|ibkr_flex\|fix44), inputs [FileRef], out, account (str\|null), instrument_map (FileRef\|null), append (false), fix ({delimiter:"auto", local_ts_regex:null}); 1.1: tz (IANA name\|null; IBKR Flex account zone), venues `sim_hftbacktest`, `sim_nautilus`, fix += {venue_name (null; may not start with `sim:`), checksum ("exclude"\|"keep"), data_dictionary (FileRef\|null), session_venues ({session: venue}), instrument_tag (int\|null)} |
| `ScorecardSpec` | live (FileRef dir), sim ({kind:"external", log: FileRef} \| {kind:"exact"} \| {kind:"zoo", model: str, l2: L2Config}), dataset (DatasetRef\|null; required for exact/zoo, markouts, queue position), out, entry ("exchange_ack"\|"send_plus_latency"), latency (LatencyModel\|null), markout_horizons_ns ([50_000_000, 100_000_000, 1e9, 1e10, 1.5e10, 6e10, 3e11] as ints), markout_refs (["mid"]), ttf_horizons_ns ([1e8, 1e9, 1e10, 6e10]), bootstrap ({b:1000, block:"auto", seed:0}; as CalibrationSpec), fdr ({method:"bh", q:0.05}), equivalence ({fill_rate_margin:0.02, markout_margin_ticks:0.25, markout_horizon_ns:1e9}), gates ({max_abs_fill_rate_bias:null, require_equivalence:false}), perm_b (2000). With `entry = exchange_ack`, a non-zero `latency` is rejected with `QT-CONFIG-001` (built). 1.1: window_ns (60_000_000_000), match_window_ns (1_000_000), self_exclude (true), markout_past_end (null\|"last_quote"); all honoured as spec fields |
| `ReportSpec` | inputs ([str] output dirs of score/zoo/calibrate/fit-hazard/replay), out (html path), title ("queuetruth report"), max_why (20); 1.1: leaderboard_sort_horizon_ns (int >= 1\|null = 60 s) |
| `RefereeSpec` | dataset, schedule (FileRef qt.shadow_spec), sim_fills (FileRef: canonical live dir or qt.shadow_fills parquet), simulator ({name, version\|null}; as built default {name: "unknown"}, unratified), tolerance ({max_discordant:0, max_qty_mismatch:0, max_abs_ttf_err_ns:0}), out |
| `WhySpec` | dataset, shadows (FileRef\|null), shadow_id (str\|null), live (FileRef\|null), order_key (str\|null), max_events (5000), out (str\|null); 1.1: exclude_order_ids (as ReplaySpec) |
| `ExportSpec` | target (hftbacktest\|nautilus\|l2_npz\|latency_npz), calibration (FileRef\|null), dataset\|null, instrument_id\|null, session_date\|null, live\|null, l2 (L2Config), feed_latency_ns (0, built), out, force (false; 1.1: write a refused calibration with the NOT VALIDATED header, 5.10) |
| `DqSpec` | (1.2, `queuetruth.dq.model`; `qt.dq_spec`) dataset (DatasetRef path), out (str\|null = return the report, write nothing), compare (DatasetRef\|null: a second dataset of the same sessions for `cross_source`), checks ([str]\|null = all; unknown id is `QT-CONFIG-001`), fail_on ("warn"\|"fail"\|"never", "warn"), max_evidence (20, 0..10,000), stale_ns (60_000_000_000), latency_outlier_ns (int\|null = 10 x p99, at least 1 ms), session_tolerance_ns (300_000_000_000), replay (true: run the exact replay for the book checks), verify_sources (false: re-hash recorded raw inputs), title (str\|null) |
| `LiveCalibrationSpec` | (registered in `specs.py`) dataset, live (FileRef dir), family (power\|power2\|power3\|databento\|nt_prob_touch\|nt_queue_l2_prob_touch), out, entry ("exchange_ack"), latency (null), mode ("faithful"), exchange ("partial"), l2 (L2Config), horizons_ns ([1e8, 1e9, 1e10] as ints), window_ns (60 s), grid (null = family default), train, test (WindowSpec; chronological split), particles (100), bootstrap ({b:200, block:"auto", seed:0}), seed (0), threads (1), self_exclude (true), match_window_ns (1_000_000) |

### 4.3 Python API (`queuetruth`)

All functions accept a Spec model or a plain dict (validated), release the GIL inside `_core`, and return the
Pydantic summary model. Arrow tables are returned as `pyarrow.Table`; inputs accept any object implementing
`__arrow_c_stream__` where a table is expected.

```python
import queuetruth as qt
qt.__version__: str
qt.ingest(spec) -> qt.schemas.IngestSummary
qt.replay(spec, *, fill_rule: str | None = None, hidden_print_policy: str | None = None,
          exclude_order_ids: list[int | str | dict] | None = None) -> qt.schemas.ReplaySummary
          # keywords override the 1.1 ReplaySpec fields
qt.sample_shadows(spec) -> qt.schemas.ShadowSampleSummary
qt.mirror_real_orders(spec) -> qt.schemas.ShadowSampleSummary
qt.shadows_from_live(live: str, out: str, *, entry: str = "exchange_ack", latency_ns: int | None = None) -> qt.schemas.ShadowSampleSummary
qt.run_zoo(spec) -> qt.schemas.ZooSummary                 # Rust run + qt.eval metrics
qt.calibrate(spec) -> qt.schemas.CalibrationResult        # alloc|survival|nt_touch (Rust + Python held-out) or latency (Python)
qt.extract_features(spec) -> qt.schemas.FeatureSummary
qt.fit_hazard(spec) -> qt.schemas.HazardModelCard
qt.import_live(spec, *, tz: str | None = None, **options) -> qt.schemas.LiveImportSummary   # tz overrides LiveImportSpec.tz
qt.validate_live(path: str) -> list[str]                  # warnings; raises qt.QtError on errors
qt.score(spec, **kw) -> qt.schemas.ScorecardSummary   # kw window_ns, match_window_ns, self_exclude override the
                                                        # spec fields and are folded into the spec before run_id
qt.report(spec) -> qt.schemas.ReportSummary
qt.dq(spec) -> queuetruth.dq.DqReport                     # 1.2; also queuetruth.dq.run(spec), write(report, out)
qt.referee(spec) -> qt.schemas.RefereeReport
qt.referee_fixtures(out: str) -> list[str]
qt.why(spec, *, model: str | None = None, zoo: str | None = None, divergence_tol: float | None = None) -> qt.schemas.WhyTrace
                                                          # model/zoo: why_explain (model queue estimate per event)
qt.export(spec, *, force: bool = False) -> qt.schemas.ExportSummary   # {target, files, text|null}; QT-CALIB-001 (5.10)
qt.calibrate_live(spec, **kw) -> qt.schemas.CalibrationResult        # kw self_exclude, match_window_ns, runner
qt.fixtures_synth(out: str, venue: str = "cme", sessions: int = 2) -> qt.schemas.FixturesSummary
qt.read_table(path: str, expect: str | None = None) -> pyarrow.Table   # checks qt.schema metadata
qt.Session(dataset: str, venue: str | None = None)        # .advance_to(ts) .bbo(inst) .depth(inst, n) .queue_ahead(inst, side, px)
qt.HazardModel.load(path: str, *, sha256: str | None = None, trusted: bool = False) -> qt.HazardModel
    # unpickling needs a trusted sha256 (pickle_sha256(card)) or trusted=True, else QT-CONFIG-001; mismatch
    # QT-INPUT-002; lazy import. .predict(features) -> pyarrow.Table (qt.hazard_predictions)
qt.QtError(Exception)                                      # .code, .message, .hint; raised for every QT-* error
qt.QtApiError(qt.QtError)                                  # code QT-REMOTE-001; .problem (qt.problem dict)
# both defined in python/queuetruth/errors.py
# in queuetruth.api only:
queuetruth.api.run_id(spec) -> str ; resolve_order_ids(...) ; why_explain(...) ; why_suggestion(...)
queuetruth.api.export_replay_schedule(target, *, out, schedule=None, amends=None, live=None, dataset=None,
                                      entry="exchange_ack") -> ReplayExport   # 5.10
queuetruth.api.fill_liquidity(reason) ; dataset_kind(path)
```

Module-level APIs used across packages (signatures binding; section 5 defines semantics):

```python
# queuetruth.stats
mcnemar(b: int, c: int) -> McNemarResult                  # chi2_cc, p_chi2, p_exact, odds_ratio, or_ci (Wald on log)
binom_exact_two_sided(k: int, n: int, p: float = 0.5) -> float
wilcoxon_signed_rank(d: np.ndarray, zero_method: str = "wilcox") -> WilcoxonResult   # matches scipy.stats.wilcoxon
kaplan_meier(time: np.ndarray, event: np.ndarray) -> StepFunction                  # event: bool
aalen_johansen(time, cause: np.ndarray, causes=(1, 2)) -> dict[int, StepFunction]   # cause 0 = censored
logrank(time_a, event_a, time_b, event_b) -> TestResult                             # matches lifelines
gray_test(time, cause, group, cause_of_interest=1) -> TestResult                    # Gray 1988, rho = 0
paired_permutation(x_live, x_sim, stat: Callable, b: int, seed: int) -> TestResult
day_block_bootstrap(blocks: np.ndarray, stat: Callable[[np.ndarray], float], b: int, seed: int, alpha=0.05) -> CI
    # seed is already derived by the caller (bootstrap_seed(seed, test_id)); draw procedure in 5.7.5
clustered_mcnemar(live_filled, sim_filled, clusters, *, b=1000, seed=0, alpha=0.05) -> ClusteredDiscordance   # A10, E26
obuchowski(b_k, c_k, n_k) -> TestResult ; bootstrap_seed(seed, test_id) -> int ; bootstrap_p_two_sided(reps) -> float
bootstrap_se(reps) -> float (ddof = 1) ; percentile_ci(reps, alpha) ; block_labels(...) ; stable_seed ; splitmix64
benjamini_hochberg(p: np.ndarray, q: float = 0.05) -> np.ndarray   # adjusted p; benjamini_yekutieli(p, q=0.05); holm(p)
tost_from_ci(ci90: tuple[float, float], margin: float) -> bool; tost_p_bootstrap(reps, margin) -> float (M22, used
by the scorer); tost_p(estimate, se, margin) -> float (normal approximation, kept for API compatibility)
brier(p, y) ; log_loss(p, y, eps=1e-6) ; murphy(p, y, n_bins=10) ; reliability_table(p, y, n_bins=10) (Wilson CIs)
hosmer_lemeshow(p, y, n_bins=10) ; spiegelhalter_z(p, y) ; calibration_slope_intercept(p, y)
uno_c_index(time, event, risk, tau=None, censor=None) ; auc_at(time, event, risk, tau, censor=None)   # improper
# queuetruth.eval
evaluate_zoo(predictions: pa.Table, *, h_max_ns: int, trajectory: pa.Table | None = None, bootstrap_b=1000, seed=0,
             planned_life: pa.Table | None = None)          # qt.zoo_shadows columns (shadow_id, ts_cancel_planned, ts_admin_end)
    -> tuple[pa.Table, list[dict]]                            # qt.zoo_errors, leaderboard
planned_life_table(...) -> pa.Table ; join_features(...)
assign_strata(predictions: pa.Table) -> pa.Table              # queue_decile, liquidity_bucket (section 5.6)
baseline_predictions(kind: str, train: pa.Table, test: pa.Table, horizons_ns) -> pa.Table   # climatology_aj | mmw_bucket | back_of_queue_rate
# queuetruth.model
HazardModel(spec: HazardFitSpec).fit(features: pa.Table) -> HazardModel ; .predict(features) -> pa.Table
expand_person_period(features: pa.Table, grid_ns: list[int]) -> pa.Table
make_split(features: pa.Table, split: dict | None = None, *, h_max_ns: int | None = None, sessions=None) -> pa.Table
    # adds split column, applies purge/embargo
rcll(h_fill, h_cancel, event_ns, cause, grid_ns) ; ipcw_logloss(cif_fill, cif_cancel, event_ns, cause, tau_ns, *, censoring=None)
ipcw_brier(cif_fill, event_ns, cause, tau_ns, *, censoring=None) ; ibs ; censoring_km ; cif_from_hazards
fit_hazard(spec: HazardFitSpec) -> HazardModelCard          # api.fit_hazard delegates here (E18)
# queuetruth.live
import_venue(spec: LiveImportSpec, *, tz: str | None = None) -> LiveImportSummary ; IMPORTERS: dict[str, Callable] (9 venues)
import_sim(kind: str, inputs, out, *, account, instrument_map, venue, append, **options) -> LiveImportSummary   # SIM_IMPORTERS = {hftbacktest, nautilus} (E11)
write_canonical_log(out, orders, events, *, validate_log=True, instrument_map_sha256=None) ; coverage(live_dir) -> dict
build_log(orders: list[dict], events: list[dict]) -> tuple[pa.Table, pa.Table] ; validate(path) -> list[str]
to_schedule(live_dir: str, *, entry: str, latency_ns: int | None, keep_priority_venues=None) -> tuple[pa.Table, pa.Table]
    # qt.shadow_spec, qt.shadow_amend; KEEP_PRIORITY_VENUES (5.7.2)
latency_stats(live_dir: str) -> dict ; synthetic_log(seed: int, n_orders: int, **kw) -> tuple[pa.Table, pa.Table]
# queuetruth.score
build_scorecard(spec: ScorecardSpec, *, runner=None, window_ns=60_000_000_000, self_exclude=True,
                match_window_ns=1_000_000) -> ScorecardSummary          #
compute_markouts(fills: pa.Table, bbo: pa.Table, instruments: pa.Table, horizons_ns, refs, *, last_event_ts) -> pa.Table
position_series(fills: pa.Table) -> pa.Table
compare_referee(exact_results, exact_fills, sim_log, tolerance, *, schedule, spec, simulator) -> RefereeReport
run_referee(spec: RefereeSpec) -> RefereeReport ; calibrate_live(spec, **kw) -> CalibrationResult ; LiveCalibrationSpec
# queuetruth.export
hftbacktest_snippet(cal: CalibrationResult) -> str ; nautilus_snippet(cal: CalibrationResult) -> str
write_latency_npz(live_dir: str, out: str) -> FileEntry ; run_export(spec) -> ExportSummary ; export_snippets ; load_calibration
# queuetruth.report
render(spec: ReportSpec) -> ReportSummary
render(spec, *, leaderboard_sort_horizon_ns=None)          # keyword overrides the spec field; <= 0 is QT-CONFIG-001
# as built:
# queuetruth.eval.leaderboard: DEFAULT_SORT_HORIZON_NS = 60 s; sort_horizon(horizons, target_ns) ;
#   rank_leaderboard(entries, horizon) ; bias_sentence(entry, horizon) ; duration_text(ns)          (5.6)
# queuetruth.live: import_quickfix(inputs, out, *, fix, venue_name, account, instrument_map, append, data_dictionary,
#   checksum, session_venues, instrument_tag, delimiter, local_ts_regex, tz) ; schedule_with_report ; build_schedule
#   ; KEEP_PRIORITY_VENUES ; UNCHANGED_MODIFY_LOSES_PRIORITY ; SIM_VENUES                              (5.7.1, 5.7.2)
# queuetruth.score: own_free_l2_dataset(...) (l2_own) ; match_twins ; align_to_twins ; not_scored_error   (5.7.3)
# queuetruth.export: run_export(spec, *, force=False) ; trust.assess / annotate / REFUSING / ExportRefused ;
#   replay.replay_schedule(target, out, ...) ; hftbacktest_replay_script(...) ; nautilus_replay_strategy(...)  (5.10)
```

`_core` (PyO3, `python/queuetruth/_core.pyi`): `version() -> str`, `schema_names() -> list[str]`,
`arrow_schema(name) -> pyarrow.Schema`, `cli_parse(argv: list[str]) -> str` (CliCommand JSON; raises `CliExit` with
`.code`, `.text`, `.to_stderr`), `cli_run_native(cmd_json: str) -> tuple[str, bool]`, and one function per native
operation taking a spec JSON string and returning the summary JSON string: `ingest`, `replay`, `sample_shadows`,
`mirror_real_orders`, `run_zoo`, `calibrate`, `extract_features`, `why`, `export_l2_npz`, `referee_fixtures`,
`synthesize_l2`, `bbo_from_l2` (spec JSON `{"dataset": {"path"}, "out", "threads"}`), `run_id`, `replay(spec_json,
options_json=None)` where `options_json` = `{"fill_rule", "hidden_print_policy", "exclude_order_ids"}` (items
`{instrument_id, order_id}` or a bare venue order id applying to every instrument, or `{"path": FILE}`),
`fixtures_synth(out, venue="cme", sessions=2)`, `read_table`, `write_table`; plus classes `Session` and
`CliExit` (as built). Errors raise `queuetruth._core.QtCoreError(code, message)`,
re-raised by `api.py` as `qt.QtError`. `_core.pyi` lists every one of these.

### 4.4 CLI

Console script `queuetruth` (Python entry `queuetruth.cli:main`): `_core.cli_parse(argv)` (clap grammar from
`qt_cli`), then native commands run in Rust via `_core.cli_run_native`, Python and hybrid commands in Python.
Standalone binary `queuetruth-core` (crate `qt_cli`) accepts the same grammar and runs native commands; Python-only
commands exit 2 with `QT-USAGE-001: requires the Python package (pip install queuetruth)`; hybrid commands run
their native part and say which fields are missing.

Global flags (before or after the subcommand): `--format human|json` (default human), `--log-level
error|warn|info|debug` (warn), `--threads N` (1; outputs MUST NOT depend on N), `--seed N` (0), `--quiet`.
Durations accept `250us`, `100ms`, `1s`, `10s`, `1m`, `5m` or integer ns. Lists are comma-separated. CLI enum values
use hyphens where the Spec value has underscores (`no-partial` = `no_partial`, `exchange-ack` = `exchange_ack`,
`real-orders` = `real_orders`, `tardis-l2` = `tardis_l2`, `coinbase-level3` = `coinbase_level3`, `kraken-l3` =
`kraken_l3`, `hyperliquid-node` = `hyperliquid_node`, `iex-deep-plus` = `iex_deep_plus`, `ibkr-flex` = `ibkr_flex`,
`l2-npz` = `l2_npz`); every flag maps to the same-named Spec field with hyphens replaced by underscores unless the table says otherwise.
Irregular flag -> field mappings: `--midnight-ns` -> `midnight_utc_ns`; `--option K=V` ->
`options.K` (V parsed as JSON when it parses, else a string); `--in` -> `inputs[].path`; `--data` -> `dataset.path`; `--out` -> `out`; `--shadows`
-> `shadows.path`; `--amends` -> `amends.path`; `--depth` -> `depth_levels`; `--date` -> `session_date`;
`--latency F,D,G` -> `latency.{feed_ns,decision_ns,gateway_ns}`; `--jitter-ns` -> `latency.jitter_ns`; `--l2-depth`
-> `l2.depth_levels`; `--l2-cadence-ns` -> `l2.cadence_ns`; `--l2-trades` -> `l2.trade_position`; `--write-l2` ->
`l2 = L2Config defaults` (overridden by the `--l2-*` flags); `--horizons` -> `horizons_ns`; `--h-max` -> `h_max_ns`;
`--lifetime` -> `lifetime_ns`; `--no-fills` -> `write_fills = false`; `--no-privileged` -> `privileged = false`;
`--population real-orders` -> `population = "real_orders"`; `--n-real` -> `n_real_orders`; `--landmarks` ->
`landmarks_ns`; `--grid` -> `grid_ns`; `--embargo` -> `split.embargo_ns`; `--n-blocks` -> `split.n_blocks`;
`--split` -> `split.scheme`; `--bootstrap-b` -> `bootstrap.b` (`bootstrap_b` in ZooSpec/HazardFitSpec);
`--bootstrap-block` -> `bootstrap.block`; `--fdr` -> `fdr.method`; `--q` -> `fdr.q`; `--fill-rate-margin`,
`--markout-margin-ticks` -> `equivalence.*`; `--fail-abs-bias` -> `gates.max_abs_fill_rate_bias`;
`--fail-not-equivalent` -> `gates.require_equivalence = true`; `--sim-log DIR` -> `sim = {kind: external, log}`;
`--sim exact` -> `sim = {kind: exact}`; `--sim-model ID` -> `sim = {kind: zoo, model: ID}`; `--markout-horizons` ->
`markout_horizons_ns`; `--simulator NAME[@VER]` -> `simulator`; `--max-*` (referee) -> `tolerance.*`;
`--placements o:w,...` -> `placements`; `--qty lots:N:w,median:w` -> `qty`; `--queue-gated` -> family choice;
`--window` -> `window_ns`; `--seed` (score) -> `bootstrap.seed`; `--force` -> `force`; `--sort-horizon` (report) ->
`leaderboard_sort_horizon_ns`; `--fix-*` (live import) -> `fix.*`; `--zoo-shadows` -> `zoo_shadows`. `--format` is
also accepted as `--output-format`; `ingest --format` also as `--input-format`.

Grammar as built. `crates/cli/tests/golden/cli_parse.json` (64 parsed
command lines with their command JSON) is authoritative for flag -> field mapping; the table lists every command
path and flag (from `queuetruth <cmd> --help`). Global flags on every command: `--format human|json`, `--log-level
error|warn|info|debug` (warn), `--threads N` (1), `--seed N` (0), `--quiet`, `--verbose` (human output: every
summary field after the answer). Flags still parsed into the `_cli` object rather than the spec (passed as keyword
overrides): replay `--fill-rule`, `--hidden-print-policy`, `--exclude-order-ids`; zoo `--exclude-order-ids`,
`--sort-horizon` (terminal ranking only); live import `--option`; score `--window`, `--match-window-ns`,
`--no-self-exclude` (the scorer folds these into the spec before `run_id`); why `--model`, `--zoo`. The replay
and zoo ones are folded into the spec too (`fold_replay_options`), so they change `run_id` (E38 OC-34).

| Command | Impl | Required | Options |
|---|---|---|---|
| `ingest` | native | `--format`, `--in`, `--venue`, `--out` | `--symbols`, `--date`, `--midnight-ns`, `--tz`, `--instruments`, `--price-exp`, `--size-exp`, `--tick-size`, `--lot-size`, `--source-venue`, `--option`, `--strict` |
| `replay` | native | `--data`, `--out` | `--shadows`, `--amends`, `--latency`, `--jitter-ns`, `--bbo none\|change\|full`, `--depth`, `--rank-oracle`, `--sweep-gap-ns`, `--trajectory none\|ahead\|full`, `--race-window-ns`, `--real-orders`, `--l2-depth`, `--l2-cadence-ns`, `--l2-trades before\|after`, `--write-l2`, `--parity-trades`, `--parity-bbo`, `--venue`, `--strict`, `--fill-rule exact\|et`, `--hidden-print-policy ignore\|behind\|behind-and-through`, `--exclude-order-ids` |
| `shadows sample` | native | `--data`, `--n`, `--out` | `--placements`, `--qty`, `--lifetime`, `--latency-ns`, `--window-open-s`, `--window-close-s`, `--allocation equal\|proportional`, `--sides` |
| `shadows mirror-real` | native | `--data`, `--out` | `--n`, `--no-stratify` |
| `shadows from-live` | python | `--live`, `--out` | `--entry exchange-ack\|send-plus-latency`, `--latency-ns` |
| `zoo` | hybrid | `--data`, `--shadows`, `--out` | `--models`, `--mode faithful\|corrected`, `--exchange partial\|no-partial`, `--horizons`, `--h-max`, `--l2-depth`, `--l2-cadence-ns`, `--l2-trades before\|after`, `--particles`, `--no-fills`, `--write-trajectory`, `--latency`, `--jitter-ns`, `--bootstrap-b`, `--amends`, `--exclude-order-ids`, `--sort-horizon` |
| `calibrate alloc` | hybrid | `--family`, `--data`, `--shadows`, `--out` | `--train`, `--test`, `--mode faithful\|corrected`, `--exchange partial\|no-partial`, `--l2-depth`, `--l2-cadence-ns`, `--l2-trades before\|after`, `--bootstrap-b`, `--bootstrap-block auto\|day\|30min`, `--particles`, `--horizons`, `--h-max` |
| `calibrate survival` | hybrid | `--family`, `--data`, `--shadows`, `--out` | `--train`, `--test`, `--mode faithful\|corrected`, `--exchange partial\|no-partial`, `--l2-depth`, `--l2-cadence-ns`, `--l2-trades before\|after`, `--bootstrap-b`, `--bootstrap-block auto\|day\|30min`, `--particles`, `--horizons`, `--h-max` |
| `calibrate nt-touch` | hybrid | `--data`, `--shadows`, `--out` | `--queue-gated`, `--train`, `--test`, `--mode faithful\|corrected`, `--exchange partial\|no-partial`, `--l2-depth`, `--l2-cadence-ns`, `--l2-trades before\|after`, `--bootstrap-b`, `--bootstrap-block auto\|day\|30min`, `--particles`, `--horizons`, `--h-max` |
| `calibrate latency` | python | `--live`, `--out` |  |
| `calibrate-live` | python | `--data`, `--live`, `--family` | `--out`, `--entry exchange-ack\|send-plus-latency`, `--latency`, `--jitter-ns`, `--mode faithful\|corrected`, `--exchange partial\|no-partial`, `--l2-depth`, `--l2-cadence-ns`, `--l2-trades before\|after`, `--horizons`, `--window`, `--train`, `--test`, `--particles`, `--bootstrap-b`, `--bootstrap-block auto\|day\|30min`, `--match-window-ns`, `--no-self-exclude` |
| `features` | native | `--data`, `--out` | `--shadows`, `--population shadows\|real-orders`, `--n-real`, `--landmarks`, `--cancel-away-ticks`, `--h-max`, `--no-privileged`, `--trackers`, `--l2-depth`, `--l2-cadence-ns`, `--l2-trades before\|after` |
| `fit-hazard` | python | `--features`, `--out` | `--split auto\|days\|blocks`, `--n-blocks`, `--embargo`, `--privileged`, `--recalibration isotonic\|none`, `--grid`, `--bootstrap-b`, `--zoo-predictions`, `--zoo-shadows` |
| `live import` | python | `--venue`, `--in`, `--out` | `--account`, `--instrument-map`, `--append`, `--tz`, `--fix-delimiter`, `--fix-local-ts-regex`, `--fix-venue-name`, `--fix-checksum exclude\|keep`, `--fix-data-dictionary`, `--fix-session-venue`, `--fix-instrument-tag`, `--option` |
| `live validate` | python | `--live` |  |
| `score` | python | `--live`, `--out` | `--sim-log`, `--sim exact`, `--sim-model`, `--data`, `--entry exchange-ack\|send-plus-latency`, `--latency`, `--jitter-ns`, `--markout-horizons`, `--markout-refs`, `--ttf-horizons`, `--bootstrap-b`, `--bootstrap-block auto\|day\|30min`, `--fdr bh\|by`, `--q`, `--fill-rate-margin`, `--markout-margin-ticks`, `--markout-horizon`, `--fail-abs-bias`, `--fail-not-equivalent`, `--perm-b`, `--l2-depth`, `--l2-cadence-ns`, `--l2-trades before\|after`, `--window`, `--match-window-ns`, `--no-self-exclude` |
| `report` | python | `--in`, `--out` | `--title`, `--max-why`, `--sort-horizon` |
| `dq` | python | `--data`, `--out` | `--compare`, `--checks`, `--fail-on warn\|fail\|never`, `--max-evidence`, `--stale-ns`, `--latency-outlier-ns`, `--session-tolerance-ns`, `--no-replay`, `--verify-sources`, `--title` |
| `referee run` | hybrid | `--data`, `--schedule`, `--sim-fills`, `--out` | `--simulator`, `--max-discordant`, `--max-qty-mismatch`, `--max-ttf-err-ns` |
| `referee fixtures` | native | `--out` |  |
| `fixtures synth` | native | `--out` | `--venue cme\|nasdaq`, `--sessions` |
| `why` | hybrid/native | `--data` | `--shadows`, `--shadow-id`, `--live`, `--order-key`, `--max-events`, `--out`, `--model`, `--zoo` |
| `export hftbacktest` | python | `--calibration` | `--out`, `--force` |
| `export nautilus` | python | `--calibration` | `--out`, `--force` |
| `export l2-npz` | native | `--data`, `--instrument-id`, `--date`, `--out` | `--l2-depth`, `--l2-cadence-ns`, `--l2-trades before\|after`, `--feed-latency-ns` |
| `export latency-npz` | python | `--live`, `--out` |  |
| `export replay-schedule` | python | `--target`, `--out` | `--schedule`, `--amends`, `--live`, `--data`, `--entry exchange-ack\|send-plus-latency` |
| `jobs submit` | python | `--spec` | `--idempotency-key`, `--wait`, `--timeout` |
| `jobs status` | python |  | `--timeout` |
| `jobs wait` | python |  | `--timeout` |
| `jobs files` | python |  | `--timeout` |
| `jobs download` | python |  | `--timeout`, `--out` |
| `schema list` | native |  |  |
| `schema show` | native |  |  |
| `doctor` | hybrid |  |  |
| `mcp serve` | python |  | `--allow-submit`, `--local` |
| `bench replay` | native |  | `--data`, `--repeat`, `--warmup`, `--mode book\|decode\|sinks\|rank-oracle`, `--source parquet\|memory`, `--shadows`, `--synth-shadows`, `--trajectory none\|ahead\|full`, `--synth-records`, `--out`, `--scratch` |
| `bench l2` | native |  | `--data`, `--repeat`, `--warmup`, `--source parquet\|memory`, `--synth-records`, `--scratch`, `--l2-depth`, `--l2-cadence-ns` |
| `bench itch` | native |  | `--synth-messages`, `--write`, `--repeat`, `--warmup` |

Summary schemas and gates (exit 1) are unchanged from v0.1-draft except: `score` exits 1 when
`verdict.gates_passed = false` (including the `own_orders_in_l2_depth` gate, 5.7.3) and 2 when nothing was scored
(NOT SCORED); `export hftbacktest|nautilus` exit 1 with `QT-CALIB-001` when the calibration is refused unless
`--force` (5.10); `export replay-schedule --target hftbacktest|nautilus` writes a replay adapter for the user's
backtester (5.10); `dq` (1.2) writes `dq_report.json` (`qt.dq_report`) and `dq_report.html` and exits 1 when
`gate_failed` (the worst check status reached `--fail-on`, default `warn`); `fixtures synth` writes the quickstart dataset (`--venue cme|nasdaq`, `--sessions 1..5`);
`bench replay|l2|itch` are hidden and emit `qt.bench` (7.1); `jobs` uses `Client().jobs`; `mcp serve` runs
`queuetruth.mcp.server.serve`. Human output is answer-first (`why_explain` / `why_suggestion` for why).

`jobs ...` and remote MCP use env `QT_API_URL` (default `http://127.0.0.1:8080`) and `QT_API_KEY`.
`ExportSummary` (`qt.export_summary`): target, files (Files), text (string|null).

### 4.5 HTTP API (service, `/v1`)

Auth: `Authorization: Bearer <key>`; HTTP Basic with the key as username and empty password is also accepted.
Every POST accepts `Idempotency-Key` (required on endpoints marked `I`). Every response carries `X-Request-Id`.
Errors are `application/problem+json` (`qt.problem`; `type` = `https://queuetruth.dev/problems/<slug>`).
Lists: `?limit=` (1..200, default 50) `&starting_after=<id>`; response `qt.page` = `{data: [...], has_more, next_cursor}`.
IDs are `<prefix>_<26-char ULID>`: `ten_`, `key_`, `up_`, `ds_`, `job_`, `rp_`, `cal_`, `sc_`, `rpt_`, `del_`
(tombstones, `/v1/tombstones`; migration 0006 renamed earlier `tomb_` rows, keeping the ULID), plus the internal row
prefixes `use_` (usage events) and `aud_` (audit rows), which no endpoint takes as a parameter. Path parameters are
named per resource (`{job_id}`, `{upload_id}`, `{dataset_id}`, `{replay_id}`, `{calibration_id}`,
`{scorecard_id}`, `{report_id}`, `{key_id}`); this table writes `{id}` (as built).
Service documents: `qt.tenant` {id, name, plan, zero_egress, retention {uploads_days, outputs_days}};
`qt.api_key` {id, prefix, scopes, created_at, last_used_at?, revoked_at?}; `qt.api_key_created` (= api_key +
`secret`, shown once); `qt.upload` {id, purpose (`market_data`/`live_log`/`sim_log`/`shadow_spec`/`model`/`other`),
filename, size_bytes, sha256, status (`pending`/`complete`/`verified`/`failed`), parts [{part_number, url,
expires_at}], created_at}; `qt.dataset` {id, name, kind, format, venue, status (`pending`/`validating`/`ready`/
`failed`/`deleted`), upload_ids, manifest?, data_quality?, created_at, job_id?}; `qt.usage_report` {from, to,
group_by, rows [{key, meter, quantity, unit}]}; `qt.meta` {api_version "v1", engine_version, schema_versions
{name: 1}, limits {max_upload_bytes, part_size_bytes, max_jobs_running, rate_limits}, formats [str], zero_egress};
`qt.health` {status (`ok`/`degraded`), db, store, queue, version}.

| Method & path | Scope | Body -> Response | Codes |
|---|---|---|---|
| GET `/v1/health` | none | -> qt.health | 200, 503 |
| GET `/v1/meta` | read | -> qt.meta | 200 |
| GET `/v1/openapi.json` | none | OpenAPI 3.1 | 200 |
| GET `/v1/tenant` | read | -> qt.tenant | 200 |
| POST `/v1/keys` (I) | admin | {scopes, name} -> qt.api_key_created | 201 |
| GET `/v1/keys` | admin | -> qt.page[qt.api_key] | 200 |
| DELETE `/v1/keys/{key_id}` | admin | -> (empty) | 204, 404 |
| POST `/v1/uploads` (I) | write | {purpose, filename, size_bytes, sha256, content_type?} -> qt.upload (presigned part URLs) | 201, 413, 422 |
| GET `/v1/uploads/{id}` | read | -> qt.upload | 200, 404 |
| POST `/v1/uploads/{id}/complete` (I) | write | {parts: [{part_number, etag}]} -> qt.upload (status complete; sha256 verified asynchronously -> verified\|failed) | 200, 409, 422 |
| DELETE `/v1/uploads/{id}` | write | -> | 204, 404 |
| POST `/v1/datasets` (I) | write | {name, kind, format, venue, upload_ids, ingest: IngestSpec minus inputs/out} -> qt.dataset (+ `Location` of the `dataset_validate` job) | 202, 422 |
| GET `/v1/datasets`, GET `/v1/datasets/{id}` | read | -> qt.page[qt.dataset] / qt.dataset | 200, 404 |
| DELETE `/v1/datasets/{id}` | write | -> qt.job (deletion job; objects hard-deleted, tombstone written; `Location`; optional `Idempotency-Key`) | 202, 404 |
| POST `/v1/replays/estimate` | read | ReplaySpec -> qt.estimate | 200, 422 |
| POST `/v1/replays` (I) | write | ReplaySpec -> {id, job: qt.job} | 202 + `Location: /v1/jobs/{job_id}` |
| GET `/v1/replays`, GET `/v1/replays/{id}` | read | -> {id, spec, job_id, summary?: qt.replay_summary} | 200, 404 |
| POST `/v1/calibrations/estimate`, POST `/v1/calibrations` (I), GET list/get | as replays | CalibrationSpec / qt.calibration_result | same |
| POST `/v1/scorecards/estimate`, POST `/v1/scorecards` (I), GET list/get | as replays | ScorecardSpec / qt.scorecard_summary | same |
| POST `/v1/reports` (I), GET `/v1/reports/{id}` | write/read | ReportSpec (inputs = job ids) / {id, job_id, files} | 202 / 200 |
| GET `/v1/jobs`, GET `/v1/jobs/{id}` | read | `?kind=&state=` -> qt.page[qt.job] / qt.job | 200, 404 |
| POST `/v1/jobs/{id}/cancel` (I) | write | -> qt.job (a queued job becomes `canceled` at once; a running job gets `cancel_requested`) | 200, 409 (terminal) |
| GET `/v1/jobs/{id}/files` | read | -> qt.file_manifest (presigned URLs, TTL <= 15 min) | 200, 404, 409 (not succeeded), 410 (expired) |
| GET `/v1/jobs/{id}/files/{name:path}` | read | 302 to presigned URL (S3) or 200 stream with `Range` support (local store); `name` may contain `/` | 302, 200, 206, 404, 410 |
| GET `/v1/usage` | read | `?from=&to=&group_by=meter\|day\|job` -> qt.usage_report | 200 |
| GET `/v1/retention`, PUT `/v1/retention` | read / admin | qt.tenant.retention (PUT accepts an optional `Idempotency-Key`) | 200 |
| GET `/v1/tombstones` | admin | -> qt.page[{id, resource_type, resource_id, deleted_at, object_count, sha256s}] | 200 |
| PUT/GET `/v1/_store/{key:path}` | signed URL only | local-store part upload and ranged download (HMAC-signed, expiring); a part must be exactly its expected length | 200, 206, 403, 413, 416, 422 (`part-size-mismatch`) |

Status mapping: request validation -> 422 (`validation-error`, `errors[]`); missing/invalid key -> 401; scope -> 403;
unknown or other-tenant id -> 404 (never 403); idempotency key reused with a different request hash -> 422
(`idempotency-key-reused`); same key still in flight -> 409 (`idempotency-in-flight`); terminal job cancel -> 409;
body > 1 MiB or upload > limit -> 413; rate limited -> 429 with `Retry-After`; unexpected -> 500 (`internal`, no
stack trace); dependency down -> 503.

Hardening changes: `POST /v1/uploads/{id}/complete`
checks the part layout and sizes (S3: ListParts) and returns 422 `upload-size-mismatch` (`QT-INPUT-001`, parts
deleted, stored as the idempotent response); `GET /v1/uploads/{id}` returns `parts: []` to keys without write
scope; `POST /v1/keys` returns 409 `key-limit` beyond `QT_MAX_ACTIVE_KEYS` unrevoked keys per tenant; job POSTs and
estimates return 422 `validation-error` (`QT-CONFIG-001`) when a compute knob exceeds `QT_COMPUTE_LIMITS` (checked
again by the worker); failed authentications are throttled per client address before any database access (429);
rate-limit buckets are per tenant, not per key; `qt.meta.limits.max_jobs_running` = `QT_TENANT_MAX_RUNNING` (or
the worker concurrency when 0).

Job inputs: upload references inside job specs that point at a `.zip`, `.tar`, `.tar.gz` or
`.tgz` upload are extracted by the worker (path checks, decompression guard of 8.10), so directory inputs (live
logs, etcsv sets) can be submitted as one archive. Buckets: GET -> `read`; other methods -> `write`; job-creating
POSTs (including POST `/v1/datasets`) -> `submit` only; estimates are `read` scope but POSTs, so they use `write`.

### 4.6 MCP server (stdio)

`queuetruth mcp serve` (module `queuetruth.mcp.server`, official `mcp` Python SDK 2.x). Remote mode (default):
needs `QT_API_KEY`, calls the HTTP API at `QT_API_URL`. Local mode (`--local` or no key): only the local read tools
(marked L) are listed; they read output directories under `QT_MCP_ROOT` (default cwd) and never write.
Tools are listed in this exact order; each declares `inputSchema` and `outputSchema` (the JSON Schemas of section 3.10
or the Spec schemas of 4.2) and returns `structuredContent` plus a short text rendering. Large artifacts are returned
as `resource_link` items (presigned URLs), never inline. No destructive tool exists (no deletes, no key management).

| Tool | Input | Output | Notes |
|---|---|---|---|
| `qt_meta` | {} | qt.meta | |
| `qt_list_datasets` | {limit?, starting_after?} | qt.page | |
| `qt_describe_dataset` | {dataset_id} | qt.dataset | |
| `qt_estimate` | {kind: replay\|calibration\|scorecard, spec} | qt.estimate | |
| `qt_get_job` | {job_id} | qt.job | |
| `qt_list_job_files` | {job_id} | qt.file_manifest | resource_link per file |
| `qt_get_replay` / `qt_get_calibration` / `qt_get_scorecard` | {id} | summary schema | |
| `qt_explain_metric` | {name} | {name, definition, formula, proper: bool, notes} | from a static table in `queuetruth.mcp.metrics` |
| `qt_submit_replay` / `qt_submit_calibration` / `qt_submit_scorecard` / `qt_submit_report` | {spec, idempotency_key?} | qt.job | only with `--allow-submit`; returns the qt.job handle for polling with `qt_get_job` (`mcp` 2.3.0 has no `io.modelcontextprotocol/tasks` extension, so the task branch is not used in v0.1) |
| `qt_local_read_summary` (L) | {path} | any qt.* summary (validated by its `schema` field) | |
| `qt_local_list_outputs` (L) | {path} | {entries: [{path, schema}]} | |
| `qt_local_why` (L) | {dataset_path, shadows_path, shadow_id} | qt.why_trace | runs `_core.why`; read-only |

(as built) The server is built on `mcp.server.mcpserver.MCPServer` (FastMCP no longer
exists in SDK 2.x); tool order = registration order. Errors return `isError` with a `qt.error` document;
`qt_get_replay`/`qt_get_calibration`/`qt_get_scorecard` raise `QT-REMOTE-001` with the job state while no summary
exists. Local summaries are found by `schema` field; a directory holding one document is read as that document.
 Calibration renderings carry the trust verdict and `beats_default`;
`qt_explain_metric`'s glossary (`queuetruth.mcp.metrics`) states AJ truth (never 1 - KM), planned-life eligibility
and the cluster-robust primary test (entry `discordance_clustered`); local tools resolve every path with realpath
and refuse symlinks leaving `QT_MCP_ROOT`. `qt_explain_metric` is not listed in local mode (it is not marked L;
ratified as built, rev 2026-10-19): local mode's tool set is exactly the L tools, which read only `QT_MCP_ROOT`, so a
keyless client sees one closed, read-only surface; the server instructions are per mode, and the local-mode
instructions name only the L tools (and say that the glossary needs remote mode).

## 5. Semantics and model definitions

### 5.1 Book and replay (port of et; et code is the reference implementation)

5.1.1 **Book.** Per instrument, two sides of `BTreeMap<Px, Level>`, each level a FIFO doubly linked list in a node
arena (et `book.rs`), order map with a fixed multiplicative hasher. `u32` node indices are checked (`QT-ENGINE-001`
on overflow). Record handling:
- `A`: ignored if `price == UNDEF_PRICE` or `size == 0`. A duplicate `order_id` replaces the resting order (removed,
  then appended at the back of its new level) and counts `duplicate_adds` unless `rules.snapshot_records_flagged &&
  flags & SNAPSHOT` (inside a flagged snapshot it still moves to the back, uncounted). Shadows follow the book: the
  order leaves the shadow's `ahead_set`.
- `C`: `size` is the quantity removed. `size < resting` -> `Reduce` (priority kept); otherwise `Remove`
  (`cancels_exceeding_resting += 1` when `size > resting`). Unknown id -> `unknown_order_refs += 1`, `Effect::None`.
- `M`: if `!rules.modify_records_present` -> `unexpected_modifies += 1`, ignored. Unknown id -> counted and treated
  as `A`. `size == 0` -> `Remove`. `rules.modify_priority.keeps_priority(old_px, old_sz, new_px, new_sz)` -> `Reduce`
  (or `None` if unchanged); else `Replace` (removed, appended at the tail of the new price level). On CME an `M`
  with unchanged price and size is a `Replace` (priority lost: iceberg refresh, account change); elsewhere it stays
  `None`. CME (`iceberg_refresh_loses_priority`): an `M` at the same price keeps priority only when its size is below the order's post-fill remaining size
  (`old_size - pending_fill`, fills reported by `F` earlier in the same event and not yet removed), or equal to it
  when there was such a fill; any other `M` (same size without a fill, size above the post-fill remaining size,
  price change) is a `Replace`. A size decrease keeps priority on CME (`qt_book::cme_modify_keeps_priority`).
- Capacity: arena index or level-total overflow is `QT-ENGINE-001`: the engine turns `Book::error()` into the error
.
- `R`: clears the instrument's book -> `Clear`.
- `F`: no book change; `fills_unknown_order` / `fills_exceeding_resting` counters. `T`, `N`: no book change.

5.1.2 **Event atomicity.** An *event* is the maximal run of consecutive records of one instrument partition that
ends with a record carrying `flags::LAST`. Books are consistent only at event end. BBO, depth, L2 rows, shadow fill
decisions and invariant checks (`note_event_end`: `crossed_books_after_last` when both sides exist and
`bid >= ask`, `negative_spreads` when `bid > ask`) happen at event end. Shadow activation, cancels and amends
happen at event start. A partition that ends inside an event gets a synthetic end (counter
`unterminated_events` in `source_specific`).

5.1.3 **Stats and anomalies** (et `Stats`): `records`, `events`, `trades`, `snapshot_records`,
`out_of_order_ts_event` (records applied in file order, never re-sorted), `sequence_gaps` /
`sequence_regressions` per `channel_id` only when `rules.sequence_meaningful` (snapshot records reset the channel),
`files_without_snapshot` only when `rules.snapshot_begins_with_clear` (reported in `qt.data_quality.source_specific`,
like `unterminated_events`).

5.1.4 **Rank oracle** (et `RankOracle`, exact port): on every `F` the filled order's rank (orders ahead at its price
not already consumed earlier in the same event), `level_depth` (better levels on its side still holding an
unconsumed order) and price-priority violation (`level_depth > 0`); sweep grouping with `sweep_gap_ns` (fills at one
(instrument, side, resting price) within the gap of the previous fill there form a sweep; a sweep closes on the gap,
on an add/cancel/modify of a non-member at that level, on `R`, or at end of input); `rank_sweep_adjusted` ignores
orders ahead executed in the same sweep; `exec_kind` = `at_price` iff the `F` price equals the resting price;
histograms with 6 bins (0..4, 5+); 30-minute local buckets keyed `HH:MM` from `instrument.midnight_utc_ns`;
`first_60s_after_open` from `instrument.open_s`. Rows emitted when their sweep closes.

5.1.5 **Data quality verdict.** Per partition: `unusable` if `maybe_bad_book_records > 0`, or
`crossed_books_after_last / events > 1e-3`, or `unknown_order_refs / records > 1e-3`; `warnings` if any counter in
`qt.data_quality` other than the info counters `hidden_prints` and `off_price_executions` is non-zero (including
`source_specific`, and feed gaps); else `clean`. The volumes `records` and `snapshot_records` never count.
Hidden prints and off-price executions are normal on Nasdaq and never set `warnings` or `dq_flags`. The
`source_specific` INFO counters `itch_zero_share_cross`, `cross_prints` and `dbn_instrument_id_remapped` never set
`warnings` either, and snapshot records (which carry the snapshot's own timestamps) are never
`out_of_order_ts_event`. One function implements the rule, `qt_ingest::dq::apply_verdict` (re-exported by
`qt_replay::dq`; ingest and qt_crypto use `records` as the event count, replay the replayed events; a zero
denominator counts as 1); the testkit reference re-derives it independently. Reasons list the unusable conditions,
or for `warnings` the non-zero anomaly counters. Dataset verdict = worst partition. `unusable` partitions are still
replayed but every analysis summary carries the verdict and `QT-DATA-001` is raised only under `--strict`.

5.1.6 **Replay output directory** (`ReplaySpec.out`): `summary.json` (qt.replay_summary), `trades.parquet`,
`bbo.parquet` (unless `bbo = none`), `depth.parquet` (if `depth_levels > 0`), `rank_trace.parquet` (if rank oracle),
`shadow_results.parquet`, `shadow_fills.parquet`, `queue_trajectory.parquet` (if shadows), `real_orders.parquet`
(if `real_orders`), `l2/date=.../instrument_id=.../part-00000.parquet` (if `l2`), `data_quality.json`, and with
shadows `shadow_planned_end.parquet` (metadata `qt.schema = "qt.x_shadow_planned_end"`; columns `shadow_id: utf8`,
`planned_end: ts [null]` = min(cancel effective time, session close), `kind: utf8` in {`cancel`, `session_close`,
`none`}; kept next to the 1.1 `qt.shadow_results` columns). Single-file
tables are the concatenation of per-partition outputs in partition order `(session_date, instrument_id)`, each in
emission order.

5.1.6a **L2-only datasets.** `run_dataset` on a dataset of kind `l2` fails with `QT-CONFIG-001` ("exact replay
needs MBO"); scorer and markouts call `qt_l2::bbo_from_l2` instead; the zoo runner runs models on the ingested L2
rows without an exact arm (no truth columns: `truth_*` null, predictions still written).

5.1.7 **Parity.** `parity_trades`: derived trades (from `T` records) vs the dataset's `trades_source`, matched in
order per partition on `(ts_event, sequence, price, size, aggressor_side)`; counts compared / mismatches / missing /
extra, first 20 mismatches. `parity_bbo`: compares `bbo = full` rows, in order **per instrument_id** (a reference that
interleaves instruments, like et's, is split by instrument while streaming), against a reference file: a qt.bbo
Parquet, or an et `bbo.csv` (header `ts_event,instrument_id,bid_px,bid_sz,ask_px,ask_sz,sequence`, empty side =
`9223372036854775807` with size 0). Pass = zero mismatches.

### 5.2 Shadow orders (et semantics extended; normative)

5.2.1 **Delays and activation.** `ts_live = ts_submit + submit_delay` (section 3.1 latency semantics, checked
arithmetic; overflow -> `QT-CONFIG-001`). The shadow activates at the start of the first event of its instrument
partition with `ts_event > ts_live` (strict), using the book as left by the previous event end. If its cancel or
an amend-to-zero is effective at or before `ts_live` and before that event, it never goes live (`end_state =
cancelled`, `ts_activated_event = null`). Shadows whose instrument/date partition does not exist end `censored_gap`
with a warning.

5.2.2 **Price resolution.** `abs`: the given price. `touch`: at activation let `B` be the same-side best price
(none -> `rejected_no_touch`); bid price = `B + offset_ticks * tick_size`, ask price = `B - offset_ticks * tick_size`
(negative offset = passive, behind the touch). If the resolved price would lock or cross the opposite best
(bid >= best ask, ask <= best bid) and `offset_ticks > 0` -> `rejected_would_cross`. (`offset_ticks <= 0` never
rejects.)

5.2.3 **State at activation.** `ahead_set` = ids of all orders resting at (side, price); `ahead = level total`;
`rank = |ahead_set|`; `traded = 0`. If the opposite best is at or through the price (bid: best ask <= price; ask:
best bid >= price): under fill rule `et` the shadow fills its full quantity immediately at its own price; under
`exact` (default) it takes the opposite displayed size as a taker (5.2.4a) and any remainder rests (rev
2026-10-19: A5, E27). Either way reason `crossed_at_live`, fill `ts_event = ts_live` (et convention),
`queue_ahead_before = ahead`.

5.2.3a **Fill rules**. `FillRule::Exact` (default of `Engine`, `run_partition` and
`run_dataset`) applies 5.2.4a; `FillRule::Et` applies 5.2.4-5.2.5 exactly as et does and is used for the et parity
checks RD-5/RD-6 and ITCH-5. Activation, price resolution, cancels, amends, termination, trajectory rows and truth
decomposition (5.2.1-5.2.3, 5.2.6-5.2.8) are shared. Selection: `ReplaySpec.fill_rule` (1.1), `replay
--fill-rule`, `qt.replay(..., fill_rule=)`.

5.2.4 **Per-record accumulation, fill rule `et`** (live shadows of the record's instrument; after the book applied
the record):
- Effect on an order in `ahead_set`: `Remove{size}` -> remove from set, `ev_removed[id] += size`; `Replace{old_size}`
  -> remove from set (lost priority, even at the same price), `ev_removed[id] += old_size`; `Reduce{by}` ->
  `ev_removed[id] += by`.
- `F` record for an order in `ahead_set` -> `ev_filled[id] += size`.
- `T` record: `consumes_queue = !(side == N && rules.supports_hidden_orders)`. If `consumes_queue` and the aggressor
  side != the shadow's side: price == shadow price -> `ev_traded += size`; price worse for the aggressor than the
  shadow's price (bid shadow: `p < price`; ask shadow: `p > price`) -> `ev_crossed += size`.
- Behind accounting (truth decomposition only, never affects fills), at the shadow's (side, price): `Add` ->
  `add_behind += size`; `Replace` whose new price is the shadow's price (the order now sits behind, whether it was
  ahead or not) -> `add_behind += new_size`; `Remove`/`Reduce`, or `Replace` whose old price is the shadow's price,
  of an order that was not in `ahead_set` before the record -> `ev_removed_behind[id] += size | by | old_size`;
  `F` of such an order -> `ev_filled_behind[id] += size`.

5.2.4a **Fill rule `exact`** (verified
against the O-SH-5 oracle: 0 disagreements on `synth_small` and `synth_small_nasdaq`; on the real ITCH days, with
rules D1 to D3 below, 3,000 of 3,000 mirror shadows per day from the raw bytes and 2,997 and 3,000 of 3,000 on the
CSV conversion, where the 3 are the oracle's 1 s cross window on a CSV without cross flags; 6.8 and design extension
oracle-disagreements O.7). Per event, for each live shadow:
- Queue ahead is maintained from order-level events of the orders in `ahead_set` only: their executions (`F`),
  cancels and deletes (`C`), and priority-losing modifies (`Replace`, including the 5.1.1 CME rules) reduce it by
  the removed quantity; prints (`T`) never change it, and off-price prints never shrink it (A4). A duplicate
  non-snapshot add follows the book (36d).
- Fill triggers, in order: (i) executions of the shadow's own twin (A1, below); (ii) an execution (`F`) of a real
  order resting at the shadow's (side, price) that is not in `ahead_set` (behind the shadow), at the order's
  displayed price, fills the shadow by that size, reason `queue_depleted` (if queue ahead is still > 0 the fill
  stands and `behind_exec_with_ahead` is counted); (iii) an execution of a real order on the shadow's side resting
  at a price strictly worse than the shadow's, at a price also strictly worse than the shadow's, fills by that size,
  reason `trade_through`; (iv) a cross, below. A4b: an execution of an order behind the shadow at a price other than
  its displayed price (price-improved, display-slid, or a non-printable execution) never fills the shadow; it only
  pairs with the order's removal (also for an excluded order's volume, review m19).
- Hidden prints (`T` with side `N` where `hidden_lower_priority`), per `HiddenPrintPolicy` (default
  `behind_and_through` on Nasdaq, `ignore` elsewhere; `behind` = at-price part only). A3d (supersedes E27's
  queue-empty reading and the E36(c) quote rule; E57): a print at `p` is usable only when the book at the print
  shows no displayed interest at or through `p` on either side (no bid >= `p`, no ask <= `p`: a non-displayed order
  ranks behind any displayed one, and a locked or crossed price is not continuous matching) and `p` is on the tick
  grid. A usable print reaches every shadow priced at or better than `p` on either side: at `p` it is volume behind
  the displayed queue (used once the queue ahead is exhausted, `queue_depleted`); strictly through `p` it is a
  `trade_through`. Declined prints at a shadow's price count `hidden_at_px_with_display_ahead`; fills from hidden
  prints count `hidden_print_fills`. ITCH cross prints (`Q`, trades_source kind `cross`) are never hidden prints
  (E48).
- Nasdaq ITCH receipt order and sweeps (rules D1 to D3; design extension oracle-disagreements O.3, O.4).
  `order_ref_priority(inst)` holds when `inst.venue` is Nasdaq and `inst.source` is `itch`, `etcsv` or `dbn` (for
  `dbn` the dataset is taken to be XNAS.ITCH). Other venues, sources, synthetic data and the `et` rule keep arrival
  order and the A3d print side.
  - D1 (receipt-order priority). Orders Nasdaq holds until the open are released 0.27 s to 40 s after it with their
    original, smaller references and keep their receipt priority. At activation a shadow records `prio_ref`, the
    largest order reference added on its instrument (in its partition) before it went live. A later `A` (not a
    duplicate add, not a priority-losing modify) at the shadow's price with a reference below `prio_ref` joins the
    shadow's queue ahead (`exact_qa`, `ahead_set`), and its cancels and executions are queue-ahead changes; a
    reference above `prio_ref` stays behind however late it arrives. A twin-linked shadow (A1) takes its twin's
    reference as `prio_ref`, so resting orders with a larger reference leave its queue ahead. The `why` tracer labels
    such an `A` as ahead. `prio_ref` is a per-instrument lower bound on the venue's shared counter, so an order held
    and released after the instrument's last add can rank behind the shadow when Nasdaq ranks it ahead (an optimistic
    bias, not observed on the published days).
  - D2 (hidden print continuing a sweep). Per resting side, an execution sweep is open from an `F` on that side until
    another record changes that side (an `A` or `M` there, a `C` there that is not the removal of an execution of the
    sweep, a clear) or the `ts_event` changes. A side-`N` print at the `ts_event` of exactly one open sweep is the
    sweeping aggressor reaching hidden interest on that side, so it reaches only shadows on that side; otherwise the
    A3d inference applies.
  - D3 (off-grid continuation). A side-`N` print that continues a sweep may be off the tick grid (a midpoint
    execution), and under `behind_and_through` it trades through a shadow on the swept side that it passes. Without
    a sweep an off-grid print is still never used.
  The independent oracle (`qt_oracle` 0.1.3, rule O1) applies the same receipt order; it already took the side of a
  side-`N` print from the open sweep. Outputs on Nasdaq ITCH data change for shadows live in the first 40 s after the
  open, twin-linked shadows there and shadows reached by such prints, so the release that ships D1 to D3 bumps
  `ENGINE_VERSION`.
- Crosses (A5 as refined by E48/E57; 36(a)): when the opposite best is at or through the shadow's price, the
  available quantity is the opposite displayed size at or through the price, best level first and FIFO within a
  level, after the first `prio` units go to the real same-side orders with priority over the shadow (every
  same-side level strictly better than its price plus the exact queue ahead at its price; conservative), net of
  what the shadow already took from each opposite order. At activation (`crossed_at_live`) the shadow is a taker:
  one fill row per opposite level at that level's price, `liquidity = taker`, and a partial fill leaves the
  remainder resting (E27). A resting shadow crossed later by an opposite arrival (`crossed`) is a maker: the same
  quantity in one row at the shadow's own price, `liquidity = maker`. Nothing available counts `crossed_unfilled`
  (once per cross); taker fills count `taker_fills`. S8 gives 0 with 100 ahead (E37).
- Self-exclusion twins (A1 native, E39; per-shadow twin linking): an `exclude_order_ids` item is the user's own
  live order on the tape, and the shadow that stands for it replaces it for that shadow only. Linking rule
  (`Engine::set_twin_linking`, on in `replay`, `why` and the zoo's exact engine; fill rule `exact` only): at an
  excluded order's `A` or `M` record (not on a sequence gap or a `MAYBE_BAD_BOOK` record), the shadow of the same
  instrument, side and abs price that is either pending with `ts_live` equal to the record's `ts_event`, or went
  live at the start of that record's event with `ts_live` 1 ns before it (the scorer's alignment,
  `align_to_twins`), and is not linked yet, is the order's twin shadow; among several, an equal quantity first, then
  input order. A pending twin shadow passes the pending checks of 5.2.1 (a cancel or amend to zero effective at or
  before `ts_live` ends it `cancelled`) and activates at that record, before it applies, on the book as it is
  there: it joins the tail of the level, so its queue ahead is exactly the order's place (mid-event, the level
  activity the event already had is not behind volume for it). A twin shadow that was already live loses the order
  from its `ahead_set` (a priority-keeping `M` leg). From then on the order's records are never level activity
  for its own shadow, and each execution of the order is a `queue_depleted` maker fill of that shadow at the
  execution's price, whatever the computed queue ahead and the displayed price (counted `twin_fills_with_queue_ahead`
  when queue ahead is still positive, `twin_fills_off_display` when the price is not the displayed one). A later
  `A`/`M` record of a linked order (a later leg after a modify) links the next leg's shadow the same way. For every
  other shadow, the book, BBO, trades, the rank oracle, real-order extraction and observers, a linked order stays
  on the tape as the real order it is (its executions are not their volume when it rests ahead of them). An
  excluded order that no shadow enters at (or any excluded order with linking off) is removed from the tape for
  everyone: its `A`/`C`/`M`/`F` records never reach the book, and its executions are credited to shadows at its
  level as volume behind and as trade-through volume to shadows strictly better on its side (A4b applies).
  Counters `excluded_requested`, `excluded_matched`, `excluded_records` (records removed), `twins_linked`. The
  independent oracle (`python/tests/oracle/qt_oracle`, 6.2) models the same counterfactual from this definition
  and the testkit reference (`RefConfig::twin_linking`) checks the engine shadow by shadow.
- Diagnostics (`ShadowDiagnostics`) are the 1.1 field `qt.replay_summary.shadow_diagnostics`.

5.2.5 **Event end, fill rule `et`** (exact order, et `on_event_end`):
1. `pure = sum over ids of max(0, ev_removed[id] - ev_filled[id])`; `ahead = max(0, ahead - pure)`.
2. `qa_before = max(0, ahead - traded)`; `traded += ev_traded`;
   `target = clamp(traded - ahead, 0, qty)`; if `target > filled`: fill `target - filled`, reason `queue_depleted`.
3. If `ev_crossed > 0` and remaining > 0: fill `min(ev_crossed, remaining)`, reason `trade_through`.
4. If remaining > 0 and the opposite best is at or through the price: fill remaining, reason `crossed`.
5. Pairing scratch: if `rules.fill_removal_shares_sequence`, unmatched `ev_filled` entries survive until a record
   with a different `sequence` arrives (et), else they are cleared; all other per-event scratch is cleared.
Every fill call emits one `qt.shadow_fills` row (`fill_seq` += 1, `ts_event` of the event, `queue_ahead_before =
qa_before`, `traded_at_px_event = ev_traded`, `trade_through_event = ev_crossed`, `behind_volume_executed = sum of
ev_filled_behind`). `queue_ahead = max(0, ahead - traded)` after the step is the trajectory value.
`ShadowEventTruth` for the event: `ahead_before` = `queue_ahead` before step 1, `ahead_after` = `queue_ahead`
after step 2 (the trajectory values), `cancel_ahead = min(pure, ahead_before)`, `cancel_behind = sum max(0,
ev_removed_behind - ev_filled_behind)`, `add_behind`, `traded_at_px = ev_traded`, `trade_through = ev_crossed`,
`level_qty_*` = book level total at the price (the shadow is never in the book), `rank_*` = `|ahead_set|`.

5.2.6 **Cancels and amends.** Cancel effective time `c = ts_cancel + cancel_delay`; at the start of an event with
`ts_event > c` the shadow ends `cancelled` (events with `ts_event <= c` still apply). Amends (`qt.shadow_amend`,
priority-keeping size reductions only; validated at load: `ts_send >= ts_submit`, `new_qty` strictly decreasing per
shadow, else `QT-INPUT-002`) take effect at `a = ts_send + amend_delay_i` (`amend_delay_i = latency_ns + gateway_ns +
J_amend_i`, 3.1) at the start of the first event with
`ts_event > a`: if `new_qty <= filled` the shadow ends `amended_to_filled`, else `qty = new_qty` (ahead and rank
unchanged). Price changes and size increases are not amends: the caller splits them into a cancel plus a new shadow
leg (section 5.7.2).

5.2.7 **Termination.** On an `R` record the instrument's live shadows end `censored_clear` at that record's
`ts_event` and their per-event scratch is discarded. A sequence gap (when meaningful) or a `MAYBE_BAD_BOOK` record
on the instrument ends live shadows `censored_gap`. Trading halts (`qt_replay::TradingHalt` spans `[ts_start,
ts_end)` per instrument, `Engine::set_trading_halts`, `ReplayOptions::trading_halts`; an empty span is
`QT-CONFIG-001`): a shadow live when a halt starts ends `censored_gap` at the halt start (an event at exactly that
instant still trades); a shadow whose `ts_live` falls inside a halt never goes live and ends `censored_gap` at its
`ts_live` unless its cancel is effective at or before `ts_live`; a shadow arriving exactly at the resumption trades
normally. Without spans nothing changes. Ingest records them for Nasdaq ITCH and IEX DEEP+
(`qt.data_quality.trading_halts`), and `run_dataset`, `why`, the zoo, calibration and features apply a dataset's
spans to each partition (design extension halt-censoring H.6). At end of partition input, live shadows end
`censored_session_end` if the partition's last `ts_event >= midnight_utc_ns + close_s * 1e9`, else `open_at_end`.
`out_of_session = ts_submit` outside `[midnight_utc_ns + open_s * 1e9, midnight_utc_ns + close_s * 1e9)` (i64
arithmetic). `ambiguous_race = race_window_ns > 0` and (`ts_activated_event - ts_live <=
race_window_ns`, or the first fill is within `race_window_ns` after `ts_activated_event`, or any fill has
`c - race_window_ns < ts_event <= c` with `c` the cancel effective time) (built).

5.2.8 **Trajectory rows.** Mode `ahead`: a `live` row at activation (`ts_event = ts_live`), one `event` row for each
event in which `queue_ahead`, `rank` or `filled` changed, and an `end` row. Mode `full`: additionally every event
in which `level_qty`, `cancel_behind` or `add_behind` is non-zero or changed. Columns as in section 3.4.

5.2.9 **Real-order extraction** (`real_orders = true`; port of et `fidelity/orders.py`, moved to Rust): one life per
`A` (or `Replace`, which ends the old life with `end_reason = replaced` and starts a new life with the same
`order_id` at the replace time). `queue_ahead_at_add` / `rank_at_add` / `level_qty_at_add` = level state before the
add; fills from `F` records of the order (`filled_qty`, `n_fills`, first/last fill ts); `n_reduces` = reductions not
paired with an `F` (same pairing rule as 5.2.5); end: a full removal paired with an `F` of the order (same pairing
rule) -> `filled`, any other full removal -> `cancelled` (built), `R` -> `cleared`, end of
input -> `open_at_end`.
`in_snapshot = flags & SNAPSHOT` on the add.

5.2.10 **Mirror shadows** (`shadows mirror-real`, et fidelity design): sample `n` real lives (seeded; stratified by
instrument x {filled, not filled}; `in_snapshot` excluded); shadow `r<order_id>[_<life>]`, price abs = real price,
qty = `size_at_add`, `ts_submit` = real add `ts_event`, `latency_ns = 0`, `ts_cancel` = the real removal time (send
time = removal `ts_event`) if the life ended `cancelled` or `replaced` (as built `replaced`
and `cleared` lives are excluded from the sample instead: known limitation, Open conflicts D), `source = real_order_mirror`. This
stratification is not et's (24 strata of side x distance x size), so RD-5/RD-6 use et's `shadows.csv`. Because activation is strict,
the shadow joins directly behind the real order. Oracle: the shadow can never fill before the real order's first fill
and never fill when the real order did not (`shadow_only = 0`, `shadow_before_real = 0`).

### 5.3 L2 view synthesis and the queue-model zoo

5.3.1 **L2Builder.** Maintains its own MBP aggregate from MBO (order map `id -> (side, price, size)`, level map
`(side, price) -> (qty, count)`) with the record semantics of 5.1.1 (priority irrelevant), independent of `qt_book`.
- Batching with `cadence_ns = 0`: one batch per event (emitted at the `LAST` record). With `cadence_ns > 0`: depth is
  emitted at the first `LAST` record with `ts_event >= bucket_end` (`bucket_end = (floor(ts_prev_emit / cadence) + 1)
  * cadence`), as the net change since the previous emission (flag `CONFLATED`); trade rows are never conflated and
  are emitted with their own `ts_event` in the batch in which they occurred.
- View: `depth_levels = N > 0` keeps the top N levels per side as the visible view; `0` = all levels. Depth rows: for
  every level in the new view whose (qty, count) differs from what the view last showed, or that newly entered the
  view (`INTO_VIEW`, `prev_qty` = 0); for every level that left the view: emptied -> `new_qty = 0`; still present
  but pushed below N -> `new_qty = 0` and `OUT_OF_VIEW`. `prev_qty`/`prev_ct` are what the view last showed.
  `level` = rank in the new view, 255 if out.
- Trade rows: one per `T` record; `side` = the hit side (opposite of the aggressor; `N` for hidden prints);
  hidden prints (side `N` on a venue with `supports_hidden_orders`) carry `HIDDEN_TRADE` and are dropped when
  `emit_hidden_trades = false`. A side-N trade on a venue without hidden orders (e.g. CME implied) is sided from the
  BBO before the batch: at the best bid -> aggressor `A`, at the best ask -> aggressor `B`; otherwise it stays `N`
  and is counted in `unresolved_aggressor_trades` (which supersedes M12's F-record rule).
- Order within a batch: `trade_position = before`: trade rows (record order) then depth rows (bid side then ask
  side, best first); `after`: depth then trades. The last row of a batch carries `LAST_IN_BATCH`; all depth rows of a
  batch share the `ts_event`, `ts_recv`, `sequence` of the batch's `LAST` record; `event_id` increments per batch.
- Snapshots: the first batch of a partition, and every batch containing an `R`, start with a `clear` row followed by
  `snapshot` rows (flag `SNAPSHOT`) for the whole view after the batch; no depth rows for that batch.
- `VisibleBook` applies rows only: `clear` empties; `depth`/`snapshot` set the level (0 removes); trades change nothing.

5.3.2 **Zoo runner** (`run_zoo`, per partition, partitions in parallel). One pass feeds every MBO record to the exact
`Engine` (shadows from the spec, truth observer), and to `L2Builder`; each emitted L2 batch is applied to the
`VisibleBook` and dispatched to every (model, shadow) pair that is live:
- Activation: the first batch with `ts_event > ts_live` (cadence 0: the same event as the exact engine). Touch-mode
  shadows use the price resolved by the exact engine (both arms evaluate the same order). `on_arrive` receives the
  `VisibleBook` quantity at the price **before** the batch is applied (0 and `out_of_view_at_live = true` if not in
  view). If the price is at or through the visible opposite best -> full fill `crossed_at_live`.
- Rows in batch order, `VisibleBook.apply(row)` first, then: trade row with aggressor opposite to the shadow's side
  (bid shadows are hit by `A` aggressors) at the shadow's price -> `on_trade`; through the price (bid: `px < price`,
  ask: `px > price`) -> `on_trade_through`; hidden trades (as `aggressor = N`) are dispatched only to NT families in faithful mode
  and to `front_of_queue` (both modes; 5.3.3 h); depth or snapshot row at the shadow's (side, price) -> `on_depth(prev_qty, new_qty, flags)`;
  `clear` -> the pair ends `censored_clear` (corrected mode; faithful: see below).
- After the batch: if not done and the visible opposite best is at or through the price -> `on_cross`
  (corrected mode). Corrected mode caps the cross fill at the visible opposite quantity at or through the price,
  net of what the pair already took (review M6), and applies the A5 refinement: `crossed_at_live` is a taker fill,
  a later `crossed` a maker fill at the shadow's price (`qt_zoo::fill_liquidity`).
- Faithful mode reproduces real hftbacktest 2.4.4 (= `5f3ec40` for orders that are never modified) (rev
  2026-10-19: E8): a shadow at or through the visible opposite best on arrival is a GTX order that expires (no
  fill, no `on_arrive`); `clear` keeps the order and its estimate (the following snapshot rows reach it as
  `on_depth(prev = 0, new)`); the cross check runs per depth row, only when the opposite best moves toward the
  order (hbt depth mirror with lot rounding and uncrossing); each own order is independent (it sees every trade in
  full and is not in another own order's queue). NT families follow NautilusTrader 1.231.0 on an `OrderBookDelta`
feed (behaviour re-implemented from documentation and observation, no NT code; agreement checked by
`scripts/nt_crosscheck`): they receive hidden prints as aggressor `N` (NT moves both book sides to the trade price),
which count as touches and, in the queue families, consume queue ahead like displayed prints; book size updates
never shrink their queue ahead; and the queue families do not fill on a trade-through or a cross while queue ahead
is positive (5.3.3 d-f). NautilusTrader 1.231.0 resets `queue_position` to 0 whenever an `OrderBookDepth10`
arrives, so on a depth-10 feed its queue families behave like `nt_prob_touch`; from 2.0.0rc5 a depth snapshot keeps
the queue position, and 2.0 caps queue ahead at a shrinking level's new size (RESULTS.md, NautilusTrader cross-check).
The faithful NT models describe 1.231.0 on delta feeds. Adopting these rules moved the faithful
day-one result at 1 s for the default `p = 1` by +3.13 pp (14.17% vs 11.03%), as verified by the cross-check.
- MBO-input families receive `on_mbo` for every record of the instrument after activation instead of L2 rows, and
  `on_cross` from the exact book at event end.
- Cancels, amends and censoring times are copied from the exact engine so both arms share the order's life.
- Faithful hbt #312 (E8 as superseded by ruling: per pair, not run-fatal). In hftbacktest the next fill attempt on a
  #312 zombie (5.3.3 a) raises `InvalidOrderStatus` and stops the whole backtest. qt stops only that (model,
  shadow) pair at that event (`PairEnd::HbtError`); the model and every other pair run on. The pair's
  `qt.zoo_predictions` rows whose deadline `ts_live + h` is at or after the abort carry the 1.1 column `status =
  hbt_312_abort` (null otherwise); they are not predictions. Since 0.1.1 `queuetruth.eval` leaves out every row of a
  shadow on which any model aborted, for every model (`common_population`; summary field `n_excluded_hbt_312`), so
  all models share one population: the aborts select the shadows that reach a partial fill, and dropping them for
  the aborted model only compared models on different populations. Per model, the summary field
  `hbt_312_aborts` {model_id: number of aborted pairs} (1.1, additive, models without an abort absent) and one
  warning line `hbt_312_abort: <model_id>: <n> of <n_shadows> shadow pair(s) aborted ...` (stable prefix, first
  pair's shadow id and abort time) report it. Corrected mode has no zombie and never aborts.
- `FillDelta::Full` fills the leaves; `Qty(q)` fills `min(q, leaves)`; a pair is done when `leaves <= 1e-9 * lot`.
- Predictions (`qt.zoo_predictions`), per horizon `h`: deterministic families: `p_fill = 1` iff first model fill
  `<= ts_live + h`; `e_fill_qty` = model filled qty by `ts_live + h`. Particle families: fraction of particles with a
  fill by `ts_live + h`; mean filled qty. `nt_prob_touch*`: analytic (5.3.3 e/f). Truth: exact first fill within
  `h`. Censoring is outcome-independent: `censored = ts_live + h > ts_admin_end`, where
  `ts_admin_end` is the exact shadow's end time only for `censored_session_end`, `censored_clear`, `censored_gap`
  and `open_at_end` (else +inf); a shared cancel (`cancelled`, `amended_to_filled`) is not censoring. A truth label
  at `h` is defined iff the shadow's planned (exogenous) end `>= ts_live + h`, whatever the fill outcome (5.6).
- Outputs: `<out>/zoo_predictions.parquet`, `zoo_fills.parquet` (if `write_fills`),
  `zoo_trajectory.parquet` (if `write_trajectory`), `zoo_shadows.parquet` (always; qt.zoo_shadows, 3.4) and
  `summary.json` (qt.zoo_summary, with `hbt_312_aborts`); the Python layer adds `zoo_errors.parquet` and fills the leaderboard (a missing
  `zoo_shadows.parquet` is `QT-INPUT-001`, never a silent fallback; E38 OC-7). `ZooSpec.amends` and
  `exclude_order_ids` are passed to the zoo's exact engine natively (MBO-driven models keep twin executions), and
  `zoo_fills`/`zoo_predictions` carry `liquidity`. The zoo, calibration and feature
  engines load the ingest feed gaps per partition (3.3; E59).

**ModelId grammar** (canonical `Display`): `<family>[<k>=<v>;...]` with keys sorted ascending, values printed with
Rust `{}` for f64 (shortest round-trip; integers print without `.0` only if the value is integral: write `2` not
`2.0`), keys `mode` (`faithful`|`corrected`) always present, `exch` (`partial`|`no_partial`) present only for
`hbt_risk_adverse`, `hbt_power*`, `hbt_log*`. Brackets omitted when there are no keys. Example:
`hbt_power3[exch=partial;mode=faithful;n=2]`, `databento_mc[k=-0.8;mode=faithful;particles=100]`. Parsing accepts
any key order; missing `mode`/`exch`/`particles`/`p` are filled from the spec or the family default (FeatureSpec
trackers: faithful, partial). Python formats f64 values like Rust `{}` with
`numpy.format_float_positional(x, unique=True, trim="-")` (positional, shortest round-trip, no exponent). Family names: `hbt_risk_adverse`, `hbt_power`,
`hbt_power2`, `hbt_power3`, `hbt_log`, `hbt_log2`, `hbt_l3_fifo`, `nt_queue_l2`, `nt_prob_touch`,
`nt_queue_l2_prob_touch`, `databento_mc`, `front_of_queue`, `trade_through_only`. Python-only predictors (not
QueueModels; predictions only): `climatology_aj`, `mmw_bucket`, `back_of_queue_rate`, `learned_hazard`, and `exact`.

5.3.3 **Model definitions.** `L` = instrument `lot_size` as f64 (size units), `Q` = visible level qty at arrival,
`F` = `s.front`, `T` = `s.cum_trade`, `rmin(a, b)` = Rust `f64::min` (if one argument is NaN the other is returned),
`round` = Rust `f64::round` (half away from zero), `as i64` saturating (NaN -> 0). Faithful mode reproduces hbt
`5f3ec40` bit for bit on the same platform, including its quirks; corrected mode fixes them as listed.

(a) `hbt_risk_adverse`. Arrive: `F = Q`. Trade(v): `F = F - v`, then exec. Depth(prev, new): `F = rmin(F, new)`.
Exec: `lots = (-F / L).round() as i64`; if `lots > 0` { `F = 0`; `q = lots as f64 * L` } else no fill.
Exchange (faithful): `partial`: `q > leaves` -> fill leaves (done); `q <= leaves` -> fill `min(q, leaves)`, and if
`round((leaves - q) / L) == 0` the order becomes a resting "Filled" zombie: quirk `hbt_312` (identical to `q == leaves` for whole-lot quantities). In hbt the zombie's next fill attempt (an at-price trade with
`round(-F/L) > 0`, a trade-through or a cross) raises `InvalidOrderStatus` and aborts the whole backtest
(run-fatal); qt aborts only that (model, shadow) pair from that event on (status `hbt_312_abort`, summary
`hbt_312_aborts`, 5.3.2). `no_partial`: any `q > 0` fills the whole leaves. Trade-through and cross: full fill.
Corrected: exec `lots = floor(max(-F, 0) / L + 1e-9)` (a fill needs volume strictly beyond the queue ahead, no
half-lot rounding); depth `F = min(max(F, 0), new)`; `partial` with `q >= leaves` fills leaves, no quirk.

(b) `hbt_power{,2,3}[n]`, `hbt_log`, `hbt_log2` (hbt ProbQueueModel). Arrive: `F = Q`, `T = 0`. Trade(v):
`F -= v; T += v`; exec as (a). Depth(prev, new), in this exact operation order:
```
chg = prev - new;  chg -= T;  T = 0;
if chg < 0.0 { F = rmin(F, new); return }
front = F;  back = prev - front;
pi = prob(front, back);  if pi.is_infinite() { pi = 1.0 }
est = front - (1.0 - pi) * chg + rmin(back - pi * chg, 0.0);
F = rmin(est, new);                               // NaN est -> F = new (quirk hbt_nan_reset)
```
`prob` (hbt `queue.rs`, f64, exact expressions):
```
power(n, f, b)  = b.powf(n) / (b.powf(n) + f.powf(n))
power2(n, f, b) = b.powf(n) / (b + f).powf(n)
power3(n, f, b) = 1.0 - (f / (f + b)).powf(n)
log(f, b)       = (1.0 + b).ln() / ((1.0 + b).ln() + (1.0 + f).ln())
log2(f, b)      = (1.0 + b).ln() / (1.0 + (b + f)).ln()
```
`pi` is the probability that a decrease is **behind** the order (hbt convention; complement of Rigtorp's and DB's).
Corrected: `front = max(F, 0)`; if `front == 0` -> `F = min(front, new)` and return; `back = max(prev - front, 0)`;
`pi = 0` if `back == 0`, else `prob(front, back)`, replaced by `back / (front + back)` if not finite, clamped to
[0, 1]; `F = min(max(est, 0), new)`; `T` is also reset at the end of every L2 batch when `cadence_ns = 0` (so a
trade never nets against a later batch); exec as (a) corrected.

Golden values (faithful, `T = 0` unless stated; must match to the last bit on macOS arm64):

| case | F | T | prev | new | pi | F' |
|---|---|---|---|---|---|---|
| power n=2 | 30 | 0 | 100 | 80 | 0.8448275862068966 | 26.896551724137932 |
| power2 n=2 | 30 | 0 | 100 | 80 | 0.49 | 19.8 |
| power3 n=2 | 30 | 0 | 100 | 80 | 0.91 | 28.2 |
| log | 30 | 0 | 100 | 80 | 0.5538345145878271 | 21.07669029175654 |
| log2 | 30 | 0 | 100 | 80 | 0.9236334915818913 | 28.472669831637827 |
| power3 n=3 (back overflow) | 90 | 0 | 100 | 40 | 0.2709999999999999 | 40.0 |
| power n=2, trade netting | 15 | 15 | 100 | 80 | 0.9697986577181208 | 14.848993288590604 |
| power n=1.5, negative F (NaN) | -0.4 | 0 | 50 | 40 | NaN | 40.0 (quirk hbt_nan_reset) |
| power3 n=2, increase | 30 | 0 | 100 | 120 | (not computed) | 30.0 |

Exec lots (faithful, `L = 1` unless stated): F = 0 -> 0; -0.49 -> 0; -0.5 -> 1; -1.0 -> 1; -2.5 -> 3;
(`L = 0.5`) -0.25 -> 1, -0.6 -> 1.

(c) `hbt_l3_fifo` (MBO input). Track `ahead_set` exactly as 5.2.3/5.2.4 (adds join behind; priority-losing modifies
of an ahead order move it behind; partial cancels keep places). Faithful (hbt L3FIFOQueueModel +
L3NoPartialFillExchange): on an `F` record for feed order X with execution price `e`: if X is on the shadow's side and
the shadow's price is strictly better than `e` (bid: `price > e`; ask: `price < e`) -> full fill; else if X rests at
the shadow's price and X is not in `ahead_set` (X is behind the shadow) -> full fill. After each event, if the exact
opposite best is at or through the price -> full fill. `R` -> censored_clear. Fills are always whole-order (overstates
fill size for qty > 1 lot). Corrected: fill quantities follow 5.2.5 exactly (must equal the exact engine; oracle
O-ZOO-4).

(d) `nt_queue_l2` (NT `queue_position=True` on L2, single order; faithful = NautilusTrader 1.231.0 on a delta
feed, re-implemented without NT code and cross-checked against it). Arrive: `F = Q`. Trade(v, aggressor) at the
order's price applies when the aggressor is opposite to the order side, or `N` (a hidden `NO_AGGRESSOR` print;
faithful only): `consumed = min(v, F)`; `remaining = v - consumed`; if `remaining <= 0` { `F = max(F - v, 0)`; no
fill } else { `F = 0`; `excess = min(remaining, leaves)`; fill `excess` }. Depth(prev, new), faithful: a size
update never moves `F` (only at-price trades shrink it); a deleted level (`new == 0`, including a level that
scrolled out of view, `OUT_OF_VIEW`) and every snapshot row set `F = 0`. Trade-through and cross, faithful: no fill
while `F > 0` (NT checks the queue before any fill at the order's level), else full fill. No lot rounding.
Corrected (queuetruth's own semantics): depth `F = min(F, new)`; a delete sets `F = 0` unless the level only
scrolled out of view (`OUT_OF_VIEW` leaves `F` unchanged); snapshot rows `F = min(F, new)`; hidden trades are
ignored; trade-through and cross fill fully whatever `F`.
Golden: `F = 100`; trade 60 -> `F = 40`, no fill; depth new 30 -> faithful `F = 40` (unchanged), corrected `F = 30`;
trade 45 with leaves 10 -> `F = 0`, faithful fills 5 (excess 45 - 40), corrected fills 10.

(e) `nt_prob_touch[p]` (NT `prob_fill_on_limit`, default `p = 1`). A *touch* is an at-price trade with an
aggressor opposite to the order side while the order is live; in faithful mode a hidden (`NO_AGGRESSOR`) print at
the order's price is a touch too (corrected: displayed prints only). At each touch draw `u = rng.next_f64()`;
success iff `p >= 1` or (`p > 0` and `u < p`). Success fills `min(leaves, v)`. Trade-through and cross fill fully.
Analytic prediction: `p_fill(h) = 1 - (1 - p)^{N(h)}` with `N(h)` = touches in `(ts_live, ts_live + h]` (hidden
prints included in faithful mode), and 1 once a trade-through or cross occurred by `h`; `e_fill_qty(h) = p_fill(h)
* qty`.

(f) `nt_queue_l2_prob_touch[p]`: on each touch (hidden prints included in faithful mode) first apply (d) to get
`excess`, then draw; fill iff success and `excess > 0`, quantity `min(leaves, excess)`; faithful trade-throughs and
crosses are gated by `F > 0` as in (d). Analytic: as (e) with `N(h)` counting only touches with `excess > 0` (the
deterministic `F` path of (d), with the faithful depth and gate rules), and 1 once an ungated trade-through or cross
occurred by `h`.

(g) `databento_mc[k, particles = 100]` (DB tutorial estimator re-implemented from its documented code behaviour; the
tutorial's displayed formula is not what its code does). `db_p_front(k, x) = x.powf(1.0 / (1.0 + k))` for
`k <= 0`, `1.0 - (1.0 - x).powf(1.0 / (1.0 - k))` for `k > 0`, `k` in (-1, 1). Per particle `j`: `F_j`, `filled_j`.
Faithful (MBP sizes only, as the tutorial): arrive `F_j = Q`; depth row with `d = prev - new > 0`: if `new == 0`
{ `F_j = 0` } else { `x = F_j / new`; `u = rng.next_f64()`; `r = u.powf(1.0 + k)` if `k <= 0` else
`1.0 - (1.0 - u).powf(1.0 - k)`; if `r < x` { `F_j -= d` } }; then `F_j = clamp(F_j, 0, new)`; increases: clamp only.
Trades do not move `F_j` (the tutorial has no trade channel); our documented extension for a fill trigger: an
at-price trade `v` fills particle `j` by `floor(max(v - F_j, 0) / L) * L` (capped at its leaves). Corrected: trades
separated as in (b) (`F_j -= v`, `T_j += v`, `chg = prev - new - T_j`, `T_j = 0`), allocation of the whole `chg` to
the front with probability `db_p_front(k, F_j / (F_j + B_j))`, `B_j = prev - F_j` (pre-update totals), then
`F_j = clamp(F_j, 0, new)`; trade fill `floor(max(-F_j, 0) / L) * L` then `F_j = max(F_j, 0)`.
RNG: one stream per (model, shadow): `Xoshiro256StarStar::new(stable_seed(seed, &[model_id, shadow_id]))`; draws in
row order, particles `j = 0..N` per depth row. `model_ts_first_fill` = median particle first-fill time (null if fewer
than half filled by `h_max`). Note: the expected-value version of this curve is `hbt_power3[n = 1 / (1 + k)]`.

(h) `front_of_queue` (optimistic bound, both modes): arrive `F = 0`; each at-price trade, displayed or hidden
(hidden prints are dispatched to it as `aggressor = N`, since A3d may let a hidden print fill the exact shadow), fills
`min(leaves, floor(v / L) * L)`; a trade-through (displayed or hidden), a cross and a cross on arrival fill the leaves.
Every exact fill path (5.2.4a) is a subset of these, so it fills no later than the exact engine.
(i) `trade_through_only` (pessimistic bound, both modes): at-price trades never fill; only a displayed trade-through
fills (the leaves). It never fills on a cross or on arrival into a crossed book (the exact engine first gives that
quantity to real orders with priority, E36(a), so a cross need not reach the shadow) and never on a hidden print
(A3d may decline it), so the exact engine fills no later than it.
Bracket (O-ZOO-6), order by order: for every shadow, the exact first fill within `h_max` implies a
`front_of_queue` first fill at or before it, and a `trade_through_only` first fill within `h_max` implies an exact
first fill at or before it (fill counts bracket as a consequence).

Out of view, corrected mode: every corrected family ignores `OUT_OF_VIEW` rows (keeps `F`);
a shadow arriving out of view has `F` unknown and initialises `F = new_qty` at the first `INTO_VIEW` or depth row
for its level. Faithful mode keeps the behaviour above. As built only `nt_queue_l2` corrected ignores
`OUT_OF_VIEW` (`crates/zoo/src/models.rs:233`; known limitation, Open conflicts D).

5.3.4 **Default `all` set** (16): `hbt_risk_adverse`; `hbt_power[n=2|3]`; `hbt_power2[n=2]`;
`hbt_power3[n=1|2|3]`; `hbt_log`; `hbt_log2`; `hbt_l3_fifo`; `nt_queue_l2`; `nt_prob_touch[p=1]`;
`databento_mc[k=-0.8]`; `databento_mc[k=-0.95]`; `front_of_queue`; `trade_through_only` (mode/exchange from spec).
0.1.0 also ran `hbt_power[n=1]`, which equals `hbt_power3[n=1]` (`b / (b + f) = 1 - f / (f + b)`) in every cell;
0.1.1 dropped the duplicate row. It still runs when named in `models`.

5.3.5 **hftbacktest npz export** (`write_hbt_npz`): structured array, 64-byte records, numpy dtype
`[("ev","<u8"),("exch_ts","<i8"),("local_ts","<i8"),("px","<f8"),("qty","<f8"),("order_id","<u8"),("ival","<i8"),("fval","<f8")]`,
written in the `np.savez_compressed(path, data=arr)` layout (zip with one DEFLATE-compressed `data.npy` member, by
the in-crate writer `qt_l2::zipw`; CRC-32 checked, fixed 1980-01-01 timestamp). Flags: `EXCH_EVENT = 1<<31`,
`LOCAL_EVENT = 1<<30`, `BUY_EVENT = 1<<29`, `SELL_EVENT = 1<<28`; kinds `DEPTH = 1`, `TRADE = 2`,
`DEPTH_CLEAR = 3`, `DEPTH_SNAPSHOT = 4`. Depth row -> `DEPTH | (BUY_EVENT if bid else SELL_EVENT) | EXCH | LOCAL`,
`px = price * 10^price_exp`, `qty = new_qty * 10^size_exp`; trade -> `TRADE | (BUY_EVENT if aggressor B else
SELL_EVENT) | EXCH | LOCAL`; clear -> `DEPTH_CLEAR | EXCH | LOCAL`; snapshot -> `DEPTH_SNAPSHOT | side | EXCH | LOCAL`;
hidden trades omitted, and trades whose aggressor stays `N` after the 5.3.1 siding are dropped; `exch_ts =
ts_event`, `local_ts = ts_recv + feed_latency_ns`. Every npz gets a sidecar `<stem>.orders.json` = `{id, tick_size,
lot_size, actions: [{ts, kind ("submit"|"cancel"), order_id, side ("B"|"A"), px, qty (real units), tif
("GTX"|"GTC")}]}` (`write_hbt_npz_with_orders`)
read by the O-ZOO-3 driver.

### 5.4 Calibration against exact truth

Collection (`qt_calib::collect`): run the zoo runner machinery (exact engine + L2Builder) over the train and test
windows. `qt.alloc_events`: one row per (shadow, exact event) with `cancel_ahead + cancel_behind > 0`, `front =
ahead_before`, `back = level_qty_before - ahead_before`, `ahead_cancel = cancel_ahead`, `behind_cancel =
cancel_behind`, plus what the L2 view showed for that level in that batch. `qt.touch_records`: one row per touch
(5.3.3 e) with the NT front before the touch, the NT excess (5.3.3 d), and the exact truth.

**C1 allocation quasi-likelihood** (families power, power2, power3, databento; teacher forcing at exact (F*, B*)):
`l(theta) = sum_j [ b_j ln pi_theta(F*_j, B*_j) + a_j ln(1 - pi_theta(F*_j, B*_j)) ]`, with `pi` clipped to
`[1e-12, 1 - 1e-12]`; events with `front <= 0` or `back <= 0` are skipped (no information); non-finite `pi` skips the
event and counts it. hbt families use `pi = prob(n, front, back)` with `theta = ln n`; databento uses
`pi = 1 - db_p_front(k, front / (front + back))` with `theta = k`. Maximize on the grid (power: `ln n` in [-3, 4],
141 points; databento: k in [-0.99, 0.99], 199 points), then golden-section on `[theta_{i-1}, theta_{i+1}]` to
`|dtheta| < 1e-7`. Profile CI (labelled quasi): `{theta: 2 (l_max - l(theta)) <= 3.841459}` by bisection within the
grid (open side -> null). Primary CI: block bootstrap (blocks = session dates; with fewer than 5 dates, 30-minute
local buckets), `B` resamples with replacement, RNG `Xoshiro256StarStar::new(stable_seed(seed, &[b"boot"]))`;
replicate `r` draws `n_blocks` block indices, each `range_inclusive(0, n_blocks - 1)`, replicate-major (built); refit on each (grid + golden), percentile 2.5 / 97.5. Power families report
`param_name = "n"` with the estimate and both CIs transformed to `n = exp(theta)`. Stratified estimates by instrument and by distance from touch
(0, 1, 2+ ticks) when each stratum has >= 1,000 events. `log`, `log2` have no parameter: their `l` is reported for
comparison (as built as `info:` lines in `warnings`).

**C2 survival likelihood** (databento_mc faithful or corrected): the train shadows are simulated for each `theta` on
the grid (`k` = 25 points evenly spaced in [-0.98, 0.9]) with **common random numbers** (same seed for every theta);
horizons `tau_1 < ... < tau_J` = spec horizons plus `h_max`. Smoothed survival `S_ij = (#particles of shadow i with no
fill by tau_j + 0.5) / (N + 1)`, `S_i0 = 1`, hazard `h_ij = 1 - S_ij / S_i,j-1`. Truth: `K_i` = interval of the exact
first fill (`delta_i = 1`) or the last fully observed interval (`delta_i = 0`). `l = sum_i [ sum_{j < K_i} ln(1 - h_ij)
+ delta_i ln h_iK_i + (1 - delta_i) ln(1 - h_iK_i) ]`. Grid argmax, optional golden refinement (same CRN seed).
No bootstrap CI for C2 in v0.1 (a warning says so).

**C3 NT touch MLE** (closed form): per train shadow, `delta_i = 1` if the exact first fill happened at a touch
(`truth_first_fill_here`), `m_i` = its `touch_index`; a shadow whose first fill was a trade-through or cross is
censored at that fill: `delta_i = 0`, `M_i` = touches strictly before that fill (A7 as corrected by
E28; recovers p = 0.200 in the audit simulation); unfilled shadows contribute `M_i` = touches before censoring.
"Strictly before" is per event (record batch ending in `LAST`), not per record: touches in the event of the
trade-through or cross are not counted (`collect` cuts the touch rows back to before the first-fill event). With `l2.cadence_ns > 0` the fill touches are
kept too (review M5, fixed). `p_hat = sum delta_i / sum [delta_i m_i +
(1 - delta_i) M_i]`. Queue-gated variant (for `nt_queue_l2_prob_touch`): count only touches with `nt_excess > 0`.
Golden: (delta, m/M) = (1, 3), (0, 5), (1, 1) -> `p_hat = 2/9 = 0.2222222222222222`. Note in outputs: `p` is per
touch and not portable across feeds with different print rates.

**Latency** (Python, `calibrate latency`): per order, `entry_ns = ack.ts_exchange - submit_sent.ts_local_send`,
`response_ns = ack.ts_local_recv - ack.ts_exchange` (only when `ts_exchange_kind` in {match_time, gateway_time});
`cancel_ns`, `amend_ns` likewise from cancel/amend sent -> ack; quantiles p10/p50/p90/p99; negative values dropped and
counted; `clock_sync_assumed = true` always (one-way latency needs synchronized clocks).

**Protocol.** Fit on `train`, evaluate on `test` with the zoo metrics of 5.6 for the fitted model and for the
defaults (`n = 2` for power families, `k = -0.8`, `p = 1`) -> `held_out` and `baseline_held_out`. The Rust part writes
`alloc_events.parquet`, `touch_records.parquet`, `fit.json` (a `qt.calibration_result` with empty `held_out`,
`baseline_held_out` and `exports`); the Python layer runs the held-out zoo evaluation and the
exports and writes `calibration.json` (qt.calibration_result). The standalone binary writes the Rust part only and
says so in `warnings`.

**Live calibration** (`calibrate-live`; as built in `queuetruth.score.calibrate_live`):
for L2-only venues without exact truth, the family's parameter is fitted to the user's own live fills. The live
orders are scheduled as in the scorer (5.7.2: instruction-sized legs, window W of A9, maker fills only; on an MBO
dataset the live orders' own records are removed first, A1). One zoo run evaluates every grid value on every
order's first leg (`hbt_<f>[n = e^theta]`, `databento_mc[k]`, `nt_prob_touch[p]`, `nt_queue_l2_prob_touch[p]`).
Likelihood: the C2 form with live truth (`K_i` = interval of the first maker fill in W, `delta_i = 1`; else the last
fully observed interval, `delta_i = 0`; orders with none skipped), model survival `S_ij = 1 - p_fill(tau_j)`
smoothed `(S N + 0.5) / (N + 1)` (`N` = particles, 1000 for analytic NT families, 1 for deterministic models);
grid argmax (no refinement); profile CI over the grid; day-block bootstrap CI. Split (chronological, hbt #332
protocol): explicit `train`/`test` dates, else the first half of the dates train; with one date, the first half of
the orders by entry time. Held-out (test orders): per-horizon Brier, log loss and fill-rate bias of the fitted and
the default model, and at H the 2x2 (model filled = `p_fill(H) >= 0.5`) with exact and cluster-robust McNemar.
Outputs: `calibration.json` (qt.calibration_result, `kind = live`,
`spec.calibration_source = "live"`, natural-unit `param_name`, `held_out_discordance`, `trust`) and
`live_calibration_test.json` (qt.live_calibration_test). Held-out rows are dropped only when end of data cuts them
before tau (A2, review F1); taker-first fills count as filled (F9). Fail-safe: missing dates, an empty train or
test window, or no filled train order is `QT-DATA-001`. Own-order removal: L2-only datasets use the l2_own copy
(5.7.3; if not applied the result carries the refusing token `trust: own_orders_in_depth`); MBO datasets use twin
matching per (instrument, session_date) and a filtered dataset copy. Live prices and sizes are converted to the
dataset's exponents (5.7.3). The result is accepted by `export hftbacktest|nautilus` subject to 5.10.

**Fail-safe calibration and trust**. A fit that cannot
be computed raises `QT-DATA-001` instead of returning a number (e.g. no informative allocation event, which the
power/power2 fits on crypto size units used to hide behind `n_events = 0`; all events skipped; empty windows). The
C2 likelihood censors at the planned end (M4). Every calibration result carries `trust:` lines in `warnings` and
the 1.1 `trust` field {verdict, reasons, flags, beats_default}, computed by `queuetruth.export.trust.assess`
(Rust: `qt_calib::trust`). Refusing tokens, in order: `no_estimate`, `few_events` (< 30 events), `at_grid_edge`,
`flat_likelihood`, `not_identified`, `degenerate_ci`, `own_orders_in_depth`, `no_held_out` (a Rust-only `fit.json`
is always refused), `not_beating_default`. `beats_default` per horizon compares the held-out fitted model with the
default (`n = 2`, `k = -0.8`, `p = 1`): exportable only if it is not worse on log loss and Brier at every horizon
and strictly better on one. Diagnostic (non-refusing) flags include `plateau`, `profile_ci_open`,
`heterogeneous_strata`, `ci_grid_resolution`.

### 5.5 Learned L2 fill model (discrete-time competing-risk hazard)

5.5.1 **Population and labels** (`qt_features::extract`). `population = shadows`: shadows from the spec; the order's
life starts at `ts_live`; competing **cancel** = policy cancel at the first exact event end where the same-side best
is `>= cancel_away_ticks` ticks better than the order's price (bid: `best_bid - price >= k * tick`); fill = exact first
fill; censor = exact end states other than filled/cancelled, or `h_max_ns` after the landmark. `population =
real_orders`: lives from `real_orders` (not `in_snapshot`), start `ts_add`, fill = first `F`, cancel = cancelled or
replaced, censor otherwise. One row per (order, landmark `l`) for each landmark at which the order is still resting
and unfilled (`t_l = start + l`); `event_ns` = time from `t_l` to the first of fill / cancel / censor (capped at
`h_max_ns`, cause `censor` at the cap).

5.5.2 **Features** (all from the `VisibleBook` and L2 trade rows with `ts_event <= t_l`; never from the exact book
except the privileged columns; `s = +1` bid, `-1` ask; nullable columns are null when undefined; the fixed int8
column `side_sign` is also a model feature):
`dist_ticks = s * (best_same - price) / tick` (null if no same-side best); `spread_ticks` (null if a side
is empty); `level_qty` (visible qty at the price); `level_ct` (null without counts); `own_qty_lots`;
`f_hbt_risk_adverse`, `f_hbt_power3_n1`, `f_hbt_power3_n3`, `f_hbt_log2` (tracker estimates of queue ahead from
5.3.3 run on the same L2 rows since arrival, clamped >= 0; equal to `level_qty` at landmark 0);
`queue_share_ra = f_hbt_risk_adverse / (level_qty + own_qty)` (0 if the denominator is 0); `elapsed_ns = t_l - start`;
`traded_at_px_since_entry`; `unexplained_dec_since_entry = sum over batches of max(0, prev - new - trades_in_batch)`
at the level; `imb_l1 = s * (Qb1 - Qa1) / (Qb1 + Qa1)`; `imb_l3` (top 3 levels per side); `wmid_offset_ticks = s *
(wmid - mid) / tick` with `wmid = (Qb1 Pa1 + Qa1 Pb1) / (Qb1 + Qa1)`; `same_sz_l1..l3`, `opp_sz_l1..l3` (0 if
missing); `ofi_1s`, `ofi_10s` (Cont-Kukanov-Stoikov L1 order-flow imbalance over `(t - W, t]`, signed by `s`,
divided by the window mean of `(Qb1 + Qa1) / 2`; null if no L1 change); `trade_vol_px_10s` (opposite-aggressor
volume at the price), `trade_vol_side_10s` (opposite-aggressor volume at any price), `trade_vol_all_60s`;
`dec_rate_px_10s` (unexplained decreases at the price per second); `vol_ticks_60s = sqrt(sum (dmid / tick)^2)`;
`bbo_changes_10s`; `tod_sin`, `tod_cos` of `2 pi frac`, `frac = clamp((t - midnight_utc_ns - open_s * 1e9) /
((close_s - open_s) * 1e9), 0, 1)`; `log_mid_over_tick = ln(mid / tick)`. Privileged (only if `privileged = true`): `p_f_exact` (exact queue
ahead), `p_rank_exact`, `p_level_qty_exact`. Outcome columns `event_ns`, `cause`. Unit test O-FEAT-1 proves no
lookahead.

5.5.3 **Person-period expansion** (Python): grid `tau_1..tau_J` (default 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60 s;
`tau_0 = 0`). For a row with `(e, c)`: for `j = 1..J`: if `e > tau_j` -> row `(j, target 0)`; else if `c = censor`
and `e = tau_j` -> row `(j, target 0)` and stop (censored at the end of interval j: it stays in j's risk set; in
particular the cap censoring at `h_max = tau_J` survives interval J); else (`tau_{j-1} < e <= tau_j`, or `j = 1` and
`e = 0`): `c = fill` -> target 1, `cancel` -> target 2, `censor` -> no row; stop (built).
Each row carries the features plus `interval_idx = j`, `log_tau_end = ln(tau_j in s)` and `row_id`.

5.5.4 **Model.** `sklearn.ensemble.HistGradientBoostingClassifier(loss="log_loss", learning_rate, max_iter,
max_leaf_nodes, min_samples_leaf, l2_regularization, early_stopping=False, random_state=seed)` on person-period rows of
the train split (3 classes). Hazards `h_f(j|x) = P(class 1)`, `h_c(j|x) = P(class 2)`;
`S(tau_j) = prod_{l<=j} (1 - h_f(l) - h_c(l))`; `CIF_fill(tau_j) = sum_{l<=j} h_f(l) S(tau_{l-1})`; `CIF_cancel`
likewise. CIFs are monotone in horizon by construction. `privileged = true` trains the Z6 upper-bound model with the
`p_*` columns.

5.5.5 **Recalibration.** Per `j`, `sklearn.isotonic.IsotonicRegression(y_min=0, y_max=1, increasing=True,
out_of_bounds="clip")` fitted on the calib split with `x = CIF_fill_raw(tau_j)`, `y = 1{e <= tau_j, c = fill}`,
excluding rows censored before `tau_j`, sample weights `1 / max(G(min(e, tau_j)-), 0.05)` with `G` the Kaplan-Meier
survival of censoring on the calib split, evaluated just before the time. Then per row
`cif_j = max(cif_j, cif_{j-1})` (monotone across horizons), clipped to [0, 1]; the cancel CIF is not recalibrated:
when `cif_fill(tau_J) + cif_cancel(tau_J) > 1` the row's cancel CIF is scaled by one constant so the sum is 1
.

5.5.6 **Splits** (`make_split`, by order: all landmarks follow the split of `t_0`). `auto`: `D` = distinct session
dates; `D >= 5` -> `days`: train = first `floor(0.6 D)` dates, calib = next `max(1, floor(0.2 D))`, test = the rest;
`2 <= D <= 4` -> test = last date, calib = last of `n_blocks` equal-duration blocks of each earlier date's continuous
session, train = the other blocks; `D = 1` -> `blocks` within the day: blocks `0..n-3` train, `n-2` calib, `n-1` test.
`make_split(features, split, *, h_max_ns, sessions)`; `fit` passes `h_max = max(grid_ns[-1], max event_ns)` (rev
2026-10-19: E18). Purge: drop rows of an earlier split whose `[t_l, t_l + h_max]` reaches the next split's start; embargo: drop calib and
test rows with `t_l < split_start + embargo_ns`. An assertion verifies no overlap after purge.

5.5.7 **Scoring on the test split.** Primary (proper): `rcll` = mean over test order-landmarks of `-ln P(observed)`,
`P = h_c(K) S(tau_{K-1})` for an event of cause `c` in interval `K`, `S(tau_m)` if censored at `e` with `m = max{j:
tau_j <= e}` (so `S(tau_J)` at the cap), `S(tau_{K-1})` if censored inside `K` (raw hazards;
probabilities floored at 1e-12); `ipcw_logloss(tau)` (Alberge et al.
2024 eq. 2): `-mean_i [ 1{e_i <= tau, c_i = fill} ln F_fill(tau) / G(e_i) + 1{e_i <= tau, c_i = cancel} ln F_cancel(tau)
/ G(e_i) + 1{e_i > tau} ln S(tau) / G(tau) ]` with recalibrated `F_fill`, `S = max(1 - F_fill - F_cancel, 1e-12)`,
`G` = KM of censoring on the test split read as `G(t-)` and floored at 0.05, rows censored
before `tau` weight 0; a censoring with `e >= tau` counts as known survival at `tau`. Secondary:
IPCW Brier for the fill cause `BS(tau) = mean_i w_i (1{e_i <= tau, c_i = fill} - F_fill(tau|x_i))^2` with `w_i =
1/G(e_i)` if an event (any cause) by `tau`, `1/G(tau)` if `e_i > tau`, 0 if censored before `tau`; `bss_vs_climatology
= 1 - BS_model / BS_clim` where `clim` = Aalen-Johansen `CIF_fill` of the train split by stratum (instrument x side x
dist bucket {<= -1, 0, 1, >= 2} x 30-minute time-of-day bucket; strata with < 50 train orders fall back to instrument x
side), CI by day-block (or 30-minute block) bootstrap of test orders with the 5.7.5 draw procedure (`block_counts` with
`bootstrap_seed(seed, "bss_vs_climatology:<tau>")`, E5); `ibs = sum_j BS(tau_j)(tau_j - tau_{j-1}) /
tau_J`; calibration slope (logistic regression of `y` on `logit(p)`, own IRLS) and `calib_intercept` =
calibration-in-the-large, the intercept of `logit P(y) = a + logit(p)` with the slope fixed at 1 (offset),
Spiegelhalter z `= sum (y - p)(1 - 2p) / sqrt(sum (1 - 2p)^2 p (1 - p))` (two-sided normal p), Hosmer-Lemeshow
`hl_p` (10 bins, 10 df on the held-out set) and `hl_p_et` (8 df, et parity); these diagnostics at `tau` use rows
with `e > tau` or an event by `tau`, weighted by the IPCW weights above (built `model.scoring.ipcw_calibration`: weighted IRLS slope, offset intercept, weighted Spiegelhalter, weighted HL with
df = bins used for `hl_p` and 8 df for `hl_p_et`; requires >= 2 weighted rows and both classes); `c_index_uno` (at 10 s)
and `auc(tau)` reported and labelled "discrimination only (improper)". Model selection uses only `rcll` and
`ipcw_logloss`. If zoo predictions for the same shadows are supplied (`population = shadows`, landmark 0), their Brier
fills `zoo_comparison`, on never-cancel shadows only (E45 ruling on F6, E55): a row is
comparable when the shadow's `never_cancel` flag is set and `ts_admin_end` is null or >= `ts_live + h`
(planned life from `HazardFitSpec.zoo_shadows` or the sibling `zoo_shadows.parquet`); the label is the zoo's
`truth_filled`, Brier unweighted; each zoo model is scored on its own rows and the hazard model on the rows common
to all zoo models; warnings give the row counts and policy-cancelled rows. Time of day in climatology and
baselines is DST-safe (`midnight_utc_ns`, review F8).

5.5.8 **Persistence.** `<out>/model.json` (qt.hazard_model_card), `model.npz` (a deterministic ZIP of a JSON header
and `.npy` arrays for the trees and isotonic layers, parsed without executing code and checked structurally before
use; its sha256 is recorded in the card and checked on load, against the caller's `sha256=` when given), and
`predictions.parquet` (qt.hazard_predictions for all splits). A v0.1 `model.pkl` is refused unless
`allow_pickle=True`, and then still needs the caller's trusted digest or `trusted=True`; the service never unpickles
user data. Format and loader: design extension model-perf M.1.

### 5.6 Zoo evaluation (Python `queuetruth.eval`)

Populations and censoring. `evaluate_zoo` requires `planned_life`
(qt.zoo_shadows: `ts_cancel_planned`, `ts_admin_end`; `QT-INPUT-001` when it is not given, `QT-INPUT-002` when a
predicted shadow has no row or a column is missing; planned ends before `ts_live` are clamped with a note);
`api.run_zoo`, the calibrate held-out evaluation, the report and the hazard card read it from
`zoo_shadows.parquet`. Label eligibility is never inferred from the outcome-flavoured `censored` column (A2, E38
OC-7). Every row of a shadow with a `status = hbt_312_abort` row (5.3.2), for every model, is left out before
anything is computed (one common population, 0.1.1). A row at horizon `h` is
eligible iff `ts_admin_end >= ts_live + h` (outcome-independent). **Never-cancel** shadows (planned cancel null or `>= ts_live + h_max`) give the unprefixed
metrics and the only leaderboard; **cancel-bearing** shadows (e.g. mirrors) are reported under `cancel_bearing/`,
their own cancel a competing risk. Truth CIFs are Aalen-Johansen (cause 1 fill, 2 own cancel, censored at
`min(ts_admin_end, largest horizon)`), never `1 - KM`; `fill_rate_bias_aj` and `ttf_iad` use eligible rows only
(review F5).

Leaderboard (`queuetruth.eval.leaderboard`, used identically by the CLI and the
report, which reuse `summary.json`'s leaderboard): sorted at the sort horizon (`ReportSpec.leaderboard_sort_horizon_ns`
/ `report --sort-horizon`, default 60 s; the largest horizon at or below the target, else the smallest) by
`(bias missing, |fill_rate_bias|, brier, model_id)`. One sentence per model: `At {h}, {model} fills {m}% of orders
vs {e}% in the exact replay, {x} pp {more|less} often[, within noise] (95% CI {lo} to {hi} pp).` (0.1.1; without the
rates `... fills {x} pp {more|less} often than the exact replay ...`), or `... fills {m}% of orders, as often as the
exact replay ({e}%, bias {x} pp)` when |bias| < 0.05 pp, or `... has no fill-rate bias: no shadow has an exact label
at that horizon.`; the CLI appends `; n = N; at {h_max} cluster-robust discordance p = ...`; with the optional
`aborts` argument a model with #312 aborts adds `; <n> of <N> shadow pairs (<x>%) aborted at the hbt #312 event
(excluded)`. The CLI table and the report table start with an "exact fill rate" row. The leaderboard value
`fill_rate_bias[h]` (with its CI) is the never-cancel AJ bias `fill_rate_bias_aj` at `h` (A2): mean of `p` over
the eligible never-cancel rows minus the truth AJ `CIF_fill(h)`, the same figure as the report column "bias vs AJ
CIF", so the headline, the sort key and the report agree. The eligible-row `mean(p) - mean(y)` stays in
`qt.zoo_errors` as the per-stratum `fill_rate_bias` diagnostic.

Strata per run: `queue_decile` = `searchsorted(q, queue_ahead_at_live, side="right")` clipped to 0..9, `q` = the
0.1..0.9 quantiles (`numpy.quantile`, method "linear") of `queue_ahead_at_live` over the run's shadows of the same
(instrument, date); `liquidity_bucket` = the same construction with terciles of `trade_vol_60s` (0..2). Metrics per
model, horizon and stratum, over eligible rows: `brier = mean((p - y)^2)`; `logloss = -mean(y ln pc + (1 - y)
ln(1 - pc))`, `pc = clip(p, 1e-6, 1 - 1e-6)`; `fill_rate_bias = mean(p) - mean(y)` per stratum (diagnostic) and, stratum `all`, `fill_rate_bias_aj =
mean(p) - CIF_truth(h)` (AJ; A2; the leaderboard value); at `h_max`: model filled = `p >= 0.5`, `b` = truth-only, `c` = model-only, `overfill_bias = (c - b) / N`;
discordance test: `clustered_mcnemar` (A10; clusters = session dates or 30-minute blocks) as primary, McNemar
`p_exact` (5.7.5) as secondary in the leaderboard's `mcnemar` field; `qty_bias = sum(e_fill_qty - truth_filled_qty) /
sum(qty)`; `ttf_log_ratio_median` = median over jointly filled (deterministic models) of `ln(max(model_ttf, 1us) /
max(truth_ttf, 1us))`; `ttf_iad = sum_j |mean_i p_fill(h_j) - CIF_truth(h_j)| (h_j - h_{j-1}) / h_max` over the
run's horizons plus `h_max` (AJ truth; the Rust zoo writes `h_max` rows; predictions without `h_max` rows have no
eligible row for the last term and give `ttf_iad = null`, never a sum over a shorter span divided by `h_max`);
`qtrack_mae` (only with trajectories) = time-weighted mean of `|F_est - F_exact| / (level_qty_at_live + qty)`
over each pair's live interval. `ttf_iad` (null if any horizon has no eligible row) and `qty_bias` are null
when undefined (ratified, E38). CIs: day-block bootstrap (30-minute blocks if fewer than 5 dates), `bootstrap_b` resamples,
percentile, draw procedure of 5.7.5. Python baselines produce `qt.zoo_predictions` rows for the same shadows:
`climatology_aj` (train-window AJ CIF by the 5.5.7 strata), `mmw_bucket` (train-window empirical fill rate by
15-minute time-of-day x `imb_l1` quintile at arrival), `back_of_queue_rate` (fills by `h` iff `trade_vol_px_10s /
10 s * h > level_qty + qty`, features at landmark 0, et `BackOfQueueModel`).

### 5.7 Live-log scorer

5.7.1 **Importers** (`queuetruth.live`, one module per venue; all fixture-validated only, `validation_status =
fixture_only`). Accepted inputs per venue: JSONL of captured WebSocket messages (one raw message per line, or wrapped
`{"recv_ns": int, "msg": <raw>}` to provide `ts_local_recv`), REST history JSON (array or JSONL), and the venue's CSV
export where one exists. Key mappings (from the venues' public API documentation):

| venue | orders/events source | fill id / dedupe | exchange time (kind) | liquidity | notes |
|---|---|---|---|---|---|
| binance | WS `executionReport` (`x` NEW/CANCELED/REPLACED/REJECTED/TRADE/EXPIRED/TRADE_PREVENTION), REST `myTrades` | `t` trade id, `I` exec id | `T` (match_time); `E` (record_time) | `m` | `amend/keepPriority` (qty down) -> amend_keeps_priority true; REST has no client id: join via `orderId` |
| bybit | WS `execution`, `order` | `execId` (dedupe double `Filled`) | `execTime` ms (match_time) | `isMaker` | `orderLinkId` = client id |
| okx | REST `/trade/fills(-history)`, WS `orders` | `tradeId` | `fillTime` (match_time); `ts` (record_time) | `execType` T/M | fee sign inverted (OKX negative = fee) |
| coinbase | REST List Fills; WS `user` (order-level) | `trade_id` / `entry_id` | `trade_time` (match_time) | `liquidity_indicator` | WS user fills derived by `cumulative_quantity` differencing (`derived = true`) |
| kraken | WS v2 `executions` (`exec_type` pending_new..restated) | `exec_id` | `timestamp` RFC3339 (match_time) | `liquidity_ind` t/m | `amended` = user amend, `restated` = restate |
| deribit | `user_trade`, `user.orders` | `trade_id` | `timestamp` ms (match_time) | `liquidity` M/T | side from the order record when joinable, else `direction` (ASSUMED, flagged) |
| hyperliquid | `WsFill`, `orderUpdates` | `(time, coin, tid)` | `time` ms (match_time) | `crossed` -> taker | fee negative = rebate |
| ibkr_flex | Flex Trades XML/CSV (Level of Detail = executions) | `IB Execution ID` | Trade Date/Time (match_time, s precision, account tz given by `--tz`) | unknown | no maker/taker field |
| fix44 | raw FIX log (SOH or `\|`), 35=D/F/G outbound, 35=8 and 35=9 inbound | ExecID 17; 150=H bust / G correct via ExecRefID 19 | TransactTime 60 (match_time on fills, gateway_time otherwise) | LastLiquidityInd 851 (1 maker, 2 taker) | ExecType 150: 0 ack, 4 cancel_ack, 5 amend_ack, 8 reject (post_only_reject when 18=6 and tag 58 says post-only), F fill, C expire, D restate, 3 done; 35=9 -> cancel_reject / amend_reject (434); pending A/E/6 dropped; `ts_local_send` from outbound SendingTime 52 only (inbound 52 is the venue clock), `ts_local_recv` from a log-line timestamp (`fix.local_ts_regex`) |

Simulator outputs: `queuetruth.live.import_sim("hftbacktest" | "nautilus", ...)` converts a
simulator's order/fill output into a canonical log (3.8 "Simulator logs") so it can be scored with `--sim-log` or
refereed. Hyperliquid fill ids are composite `<time>:<coin>:<tid>`; Binance and OKX trade ids are unique per
instrument only (hence the 3.8 fill key).

QuickFIX and FIX logs (`queuetruth.live.importers.quickfix`, `fix44`, `fixwire`,
`fixdict`): QuickFIX C++/Python (`.messages.current.log`, `.backup.<n>.log`), QuickFIX/J (`.messages.log`, binary
`.header`), QuickFIX/n and QuickFIX/Go message logs and FileStores (body, header, seqnums), gzip and rotated copies,
directories expanded; FIX 4.2 (150=1/2 with ExecTransType bust/correct), 4.3+, 4.4 (H/G) and 5.0SP2 (ApplVerID
1137); checksum validation (`fix.checksum` exclude|keep), per-direction MsgSeqNum checks (gaps, PossDup,
SequenceReset, ResetSeqNumFlag), drop-copy dedupe across sessions (`cross_session_duplicates`), optional data
dictionaries (`fix.data_dictionary`), per-session venue names (`fix.session_venues`), instrument tag
(`fix.instrument_tag`), missing TimeInForce = Day (counted). Round-trip latency per order (`ack_rtt_ns`,
`cancel_rtt_ns`, `amend_rtt_ns`, negative values dropped) is reported by `latency_stats` but not used by
`calibrate latency`. Import summaries carry `unmapped_instruments` and `instrument_scales`. Every importer remains
fixture-validated only (no real statement has been run).

5.7.2 **Schedule** (`to_schedule`, plus the scorer's adjustments in `queuetruth.score.schedule`). Only limit orders
that can rest (GTC/day/post_only/GTX) are scheduled; market
and IOC/FOK orders are counted and excluded. `entry = exchange_ack` (default): `ts_submit = ack.ts_exchange` (kind
match_time or gateway_time; otherwise fall back to `send_plus_latency` for that order with a warning), `latency_ns =
0`; `send_plus_latency`: `ts_submit = submit_sent.ts_local_send`, `latency_ns` = spec latency or the median
`entry_ns` of `latency_stats`. `ts_cancel` = `cancel_ack.ts_exchange` (or `expire`/`done` exchange time) in
`exchange_ack` mode, and `cancel_sent.ts_local_send` in `send_plus_latency` mode (the engine adds the delay). Amends:
qty-down with `amend_keeps_priority` true, or null on a venue whose documented amend keeps priority on a size
decrease (`KEEP_PRIORITY_VENUES`: binance (keepPriority), kraken, deribit, cme; Coinbase FIX through the FIX
importer, which writes `amend_keeps_priority = true`; all other venues: not kept; venue names compared
case-insensitively; on CME a same-size modify starts a new leg, `UNCHANGED_MODIFY_LOSES_PRIORITY`; the scorer also
keeps priority for every venue when the dataset venue is a CME label) -> `qt.shadow_amend` at
`amend_ack.ts_exchange`; price change or qty increase -> the current leg is cancelled at the amend ack time and a
new leg `<order_key>.leg<k>` (k = 1, 2, ...) starts there. Leg sizing follows the live
order's instructions as sent, never quantities derived from live fills: leg `k` total =
the instruction's total qty (`amend_ack.qty` in `exchange_ack` mode, `amend_sent.qty` in `send_plus_latency`), and
each arm truncates leg `k` to that total minus the same arm's fills of earlier legs (A8: each new leg carries the instruction total; `to_schedule` is `build_schedule` without the counts, so
`shadows from-live` output is instruction-sized too). Legs are aggregated back to `order_key` (filled = any leg filled; filled qty =
sum over legs; order qty = the order's largest total qty over its life, used as the denominator). `ts_entry`
(score_orders) = the first leg's `ts_submit`.

Evaluation window: both arms are evaluated on `W = [ts_entry, min(cancel instruction time,
ts_entry + H, end of data)]`, `H = window_ns` (default 60 s); the cancel instruction time is the live
`cancel_sent.ts_local_send`, else `cancel_ack.ts_exchange`. Fills outside W are ignored in both arms. Orders whose
window is cut by end of data before H are administratively censored in both arms.

5.7.3 **Arms and matching.** `sim.kind = exact`: replay the schedule with the exact engine on the dataset (MBO) in
**self-excluded** mode (native exclusion): each live order is matched to its own MBO
life per (instrument, session_date) (`match_twins`), first by venue order id from the live log (checked against
side, price and day), else heuristically by (side, price, size, `|ts_event - ts_entry| <= match_window_ns`,
default 1 ms); matched lives become `ReplaySpec.exclude_order_ids` items and each scheduled leg replaces its twin
(5.2.4a: it takes the twin's queue place and is filled by the twin's executions; a leg entered at the twin's add time
is aligned 1 ns so it activates on that record, `align_to_twins`, exchange_ack entry with zero latency only;
`filter_mbo` closes only its own sequence holes). The match rate is reported (`twin_match_rate`), unmatched live
orders are counted (`n_twin_unmatched`) and a warning fires below 95% on an MBO venue; `self_exclude = false`
disables it. Items carry the twin's `session_date` when the instrument-day is known (left out otherwise: Open
conflicts). `zoo`: run the named model
through the zoo runner on the dataset's L2 view (MBO datasets are synthesized; L2-only datasets are used as-is), with
the same `exclude_order_ids` passed natively to the zoo's exact engine. `external`: a canonical live-log directory
produced by the user's simulator for the same schedule; matched by `order_key` (legs aggregated per order key, review
F4); unmatched orders on either side are counted and excluded; simulator logs are recognised by the `sim:` venue
prefix (E38).

Own orders in L2 depth (`queuetruth.score.l2_own`): on an L2-only dataset the
user's own resting quantity and order count are subtracted from the depth and snapshot rows of a copy of the
dataset (changes aligned to feed rows within `match_window_ns`; prints kept), which feeds the zoo arm and the
visible-queue pass; markouts keep the source quotes. If the removal cannot be applied the scorecard fails the gate
`own_orders_in_l2_depth` and the headline starts `BLOCKED: own resting orders are in the L2 depth; `.

Units: live and external-sim prices and quantities are converted to the
dataset's `price_exp` / `size_exp` (`queuetruth.score.units`); an inexact conversion is `QT-INPUT-004`, a scale
change within one instrument `QT-INPUT-002`. A live fill with a null price keeps its quantity (review F13).

5.7.4 **Per-order outcome.** `filled` = any net fill (after busts/corrections) during the order's life; `ttf` =
first fill time minus `ts_entry`; `class` from (live_filled, sim_filled). `queue_ahead_at_entry`: exact replay
`queue_ahead_at_live` when the dataset is MBO (`exact`), visible level qty at entry for L2 datasets (`l2_visible`),
else null (`none`). `overlap_same_px`: another live order of the same account, instrument, side and price was resting
at entry. Taker fills of the sim arm (`liquidity = taker`, A5) are excluded from the maker
fill rate and markouts and reported separately (`taker_fills` rows, `markout_mean_taker`). Fills-only live logs
(3.8) make every statistic that needs unfilled orders (bias, McNemar, strata tests, equivalence, CIFs, ttf
permutation, log-rank, Gray) null, with the headline saying so. Taker fills are only
`crossed_at_live` fills (A5 final). NOT SCORED: when no live order could be scored
(reasons counted: no limit price, not restable, unmatched, ...), `score` raises `QT-INPUT-002` "NOT SCORED: 0 of N
live orders scored (<count> <reason>; ...)" and exits 2; the report shows a NOT SCORED callout.

5.7.5 **Statistics** (N = matched scored orders; `n11` both, `n00` neither, `b` live_only, `c` sim_only):
- Signed bias: `fill_rate_bias = (c - b) / N` (sim fill rate minus live fill rate); `relative_overfill = (n11 + c) /
  (n11 + b) - 1` (null if `n11 + b = 0`); `qty_bias = (sum sim_filled_qty - sum live_filled_qty) / sum qty`. 95% CIs by
  day-block bootstrap. `direction`: `sim_overfills` if CI lower > 0, `sim_underfills` if CI upper < 0, else
  `no_significant_bias`.
- McNemar: `chi2_cc = (|b - c| - 1)^2 / (b + c)` (0 if `b + c = 0`), `p_chi2 = chi2.sf(chi2_cc, 1)`, `p_exact =
  min(1, 2 * sum_{i=0}^{min(b,c)} C(b+c, i) / 2^(b+c))` (= `scipy.stats.binomtest(min(b, c), b + c, 0.5).pvalue`);
  `odds_ratio = c / b`, Wald CI `exp(ln(c/b) +- 1.96 sqrt(1/b + 1/c))` (null if `b` or `c` is 0). Exact McNemar
  is secondary, labelled "assumes independent orders". Golden: `b = 3, c = 12` ->
  `p_exact = 0.03515625`, `chi2_cc = 4.266666666666667`, `p_chi2 = 0.038867103812417245`.
- Cluster-robust discordance (primary): `clustered_mcnemar` with clusters = the
  bootstrap blocks (session dates, or 30-minute blocks with fewer than 5 dates); `d = (c - b) / N`; with `K >= 5`
  clusters Obuchowski's variance `K/(K-1) * sum_k ((c_k - b_k) - n_k d)^2 / N^2` and `p = 2 t.sf(|z|, K - 1)`, else
  the block bootstrap of `(c - b) / N` (same stream as `fill_rate_bias`); the effective cluster count, discordant
  cluster count, design effect and effective N are reported.
- Split by `queue_decile` at entry (deciles of `queue_ahead_at_entry` within instrument-day) and by
  `liquidity_bucket` (terciles of `trade_vol_60s`): per-stratum 2x2 and `p_exact`.
- Time to fill: jointly filled orders `d_i = ln max(ttf_sim, 1us) - ln max(ttf_live, 1us)`; `median_log_ratio` with
  bootstrap CI; Wilcoxon signed-rank (two-sided, `zero_method="wilcox"`, `method="auto"`, equal to
  `scipy.stats.wilcoxon`). Competing risks per arm: Aalen-Johansen `CIF_fill` (cause 1 fill, cause 2 cancel/expire,
  censored at end of data) at `ttf_horizons_ns`. Primary paired test: permutation (swap arm labels within each order
  with probability 1/2, `perm_b` permutations, seeded), statistic `D = sum_h |CIF_sim(h) - CIF_live(h)|`, `p = (1 +
  #{D* >= D}) / (1 + B)`. Descriptive (assume independent samples, labelled so): log-rank on the cause-specific fill
  hazard (cancels censored) and Gray's test (rho = 0) on `CIF_fill`.
- Markouts: `d = +1` buy, `-1` sell; `P` fill price; `M(t)` = mid of the **prevailing** quote: the last `qt.bbo` row
  with `ts <= t` (null if none, if one side is empty, or if `t` is beyond the instrument-day's last event);
  `mo_ticks = d (M(t+h) - P) / tick`; `mo_ccy = d (M(t+h) - P) 10^price_exp * qty 10^size_exp * multiplier`;
  `mm_ticks = d (M(t+h) - M(t-)) / tick` with `M(t-)` the last BBO strictly before `t`; `ref = microprice` uses the
  size-weighted mid `(Qb Pa + Qa Pb) / (Qb + Qa)`. Horizons default {50 ms, 100 ms, 1 s, 10 s, 15 s, 60 s, 300 s}
  (SEC 605 grid plus 100 ms and 10 s). Identity documented in the report: the SEC realized spread of the taker
  `RS_h = 2 * mo` of the maker. Aggregates: mean per (arm, horizon, ref) over fills with bootstrap CI;
  `markout_diff` over orders filled in both arms (first-fill markout sim minus live); sim-only vs live-only
  markout means and their difference with bootstrap CI.
  Markouts are venue-mid (single-feed) realized spreads, labelled "605-style, not NBBO"; `n` is reported per horizon
  so the past-end-of-data selection is visible; "the instrument-day's last event" is `t + h > ts_max` of the dataset
  (`last_event_ts`; equality is valid). `ScorecardSpec.markout_past_end = "last_quote"`
  uses the last quote at or before that time instead of null (built). Also in bps,
  `mo_bps = d (M(t+h) - P) / |P| 10^4`, and net of fees, `mo_net_ccy = mo_ccy - fee` when the fee currency matches
  (sim fills are charged the live fee rate for their liquidity); reported as `qt.score_tests` rows
  `markout_mean_bps` / `markout_mean_net_ccy` (the 1.1 `qt.score_markouts` columns are not written yet: Open
  conflicts).
- Day-block bootstrap: blocks = session date of `ts_entry`; fewer than 5 dates -> 30-minute local blocks and a
  warning `ci_block=30min`. RNG: `numpy.random.default_rng(stats.bootstrap_seed(seed, test_id))` =
  `default_rng(stable_seed(seed, [b"bootstrap", test_id]))`; the caller derives the seed and passes it to
  `day_block_bootstrap`. Draw procedure (E5): `labels = np.unique(blocks)`
  (sorted); `draws = rng.integers(0, n_labels, size=(b, n_labels))` in one call; `stat` receives the concatenated
  row indices of the drawn blocks in draw order; estimate = `stat(arange(n))`; CI = `np.quantile(reps, [a/2,
  1 - a/2])` (linear) after dropping NaN replicates. Python uses PCG64 and Rust xoshiro256** (7.2): cross-language
  CIs agree within Monte Carlo tolerance, not bit for bit.
- Bootstrap test p-value (two-sided): `p = min(1, 2 min((1 + #{r <= 0}) / (1 + B), (1 + #{r >= 0}) / (1 + B)))` over
  the finite replicates (E2 as ruled by E38, code form kept).
- Multiple testing: the primary family {cluster-robust discordance test (`discordance_clustered`), markout diff at
  1 s (first ref)} is Holm-adjusted; the exact McNemar and the fill-rate-bias bootstrap p are
  secondary; every other p-value in `qt.score_tests` (strata, horizons, ttf tests) is BH-adjusted (or BY if
  `fdr.method = "by"`) at `q`.
- Equivalence (TOST): `fill_rate_equivalent` iff the 90% percentile bootstrap CI of `fill_rate_bias` lies inside
  the closed interval `[-margin, margin]`; `tost_p` from the same bootstrap: `max(frac(bias* <= -margin),
  frac(bias* >= margin))` over the finite replicates (`stats.tost_p_bootstrap`), so the flag and the p-value use one
  method (M22; the E3 normal approximation `max(1 - Phi((est + margin) / se), Phi((est - margin) / se))` is no longer
  used by the scorer). Same for `markout_diff` at `markout_horizon_ns` with `markout_margin_ticks`. Non-rejection with small
  `b + c` is never reported as realism.
- Latency race: the 2x2 is also reported without orders whose exact-arm shadow has
  `ambiguous_race = true` (`mcnemar_race_free`).
- Gates: `max_abs_fill_rate_bias` (fail if `|fill_rate_bias| > X`), `require_equivalence` (fail if not
  `fill_rate_equivalent`); a failed gate sets `verdict.gates_passed = false` and the CLI exits 1. Headline (as built): significant: `"sim {overfills|underfills} live by {x} pp fill rate within {H} (95% CI
  [a, b]); McNemar p = {p}[ (cluster-robust p = {p}, {k} clusters)]; {N} orders[; no gates configured]"`; otherwise
  `"no significant fill-rate difference within {H}: {+x} pp (95% CI [a, b]); {equivalent|not shown equivalent}
  within ±{m} pp (TOST p = {t}); McNemar p = ...; {N} orders"`; fills-only: `"fill-rate bias and McNemar not
  computable: fills-only source (never-filled orders missing); {n} orders"`. The CLI RESULT line and the
  stored headline both lead with the cluster-robust p, then the plain McNemar p (A10).
- Position series: per arm and instrument, cumulative signed filled qty at each fill timestamp.
- Output directory: `summary.json` (qt.scorecard_summary), `score_orders.parquet`,
  `score_fills.parquet` (1.1 layout), `score_markouts.parquet` and `score_orders.parquet` (v0.1 layout),
  `score_position.parquet`, `score_tests.parquet`, `score_shortfall.parquet` (qt.score_shortfall, the per-order
  shortfall decomposition of design extension cost-decomposition), `schedule.parquet` (the scheduled legs) and, for
  a zoo arm on MBO, `zoo_trajectory.parquet`. `live import` writes the 3.8 directory; `shadows from-live` writes
  `--out` plus `<stem>_amends.parquet` when there are amends; `calibrate latency` writes `calibration.json` and
  `latency.npz`.

### 5.8 Referee

`referee run`: replay the schedule with the exact engine (fill rule `exact`); compare per `order_key` (or
`shadow_id`) with the sim fills (canonical live log or `qt.shadow_fills`): discordant fill outcome (`sim_only`,
`exact_only`), filled-qty mismatch, first-fill time error; `pass` iff every count and error is within `tolerance`.
Outputs: the native part replays the exact arm into `<out>/exact/` (5.1.6 layout); the report
is `<out>/referee_report.json`. The simulator name is taken from `--simulator`, else from a sim log's `venue =
sim:<kind>` (3.8). `referee fixtures` (`qt_testkit::bugclasses`) writes one directory per bug class, named
`NN_<slug>`, each with `dataset/`, `schedule.parquet`, `expected_results.parquet`, `expected_fills.parquet` and a
one-paragraph `README.txt`, plus the owner's `MANIFEST.sha256` (16 classes):
`01_unrelated_delete_keeps_position` (NT #4370 class); `02_level_shrink_caps_queue` (NT #5016);
`03_distinct_trades_each_consume` (NT #5017); `04_post_trade_book_decides` (NT #3596); `05_partial_fills_sum_to_cum`
(hbt #316); `06_fill_equal_to_leaves_completes` (hbt #312); `07_size_increase_loses_priority`;
`08_price_change_loses_priority`; `09_hidden_print_no_queue` (a hidden print does not consume the displayed queue,
Nasdaq); `10_trade_through_fills`; `11_fill_before_cancel_effective_stands` (latency race);
`12_hidden_execution_at_price_after_queue` (A3/E27: a hidden print at the price fills once no displayed interest
is ahead, and one through the price fills by trade-through); `13_execution_without_print` (A4: a non-printable
execution of an order ahead reduces queue ahead; a later execution behind fills); `14_off_price_print_not_trade_through`
(A4: an execution of an order ahead printed at a worse price is not a trade-through);
`15_cme_same_size_modify_loses_priority` (A6); `16_crossed_at_live_takes_opposite` (A5: taker fills at the
opposite levels, remainder rests). Expected outputs come from `reference_shadows` under the `exact` rule.

### 5.9 HTML report (`queuetruth.report.render`)

One self-contained file: inline CSS and inline SVG only, no external requests, renders fully without JavaScript
(optional inline JS only for copy buttons), light/dark via `prefers-color-scheme`, color-blind-safe palette, every
chart has an SVG `<title>` and a data table fallback in `<details>`, size <= 10 MB, deterministic bytes for the same
inputs (no generation timestamp). Sections, each present only when its input exists, in this order: header (inputs
with sha256, `validation_status` banner real vs synthetic); verdict card (scorecard headline); data quality;
2x2 + McNemar + signed bias; discordance by queue decile (bars live-only vs sim-only) and by liquidity; markout
curves live vs sim with CIs (log-x) and sim-only vs live-only; time-to-fill CIF overlay and QQ of log ttf; position
over time live vs sim per instrument (at most 2,000 points per series, last value per uniform time bucket); zoo
leaderboard, reliability diagrams per model x horizon (Wilson CIs), fill-rate bias by queue decile; calibration
likelihood profile, estimates and CIs, held-out comparison, export code blocks; learned model reliability per horizon,
BSS vs climatology with CI, comparison with zoo models; "why did this fill" traces for up to `max_why` orders
(scorecard: discordant orders, sim_only first, then by |ttf difference|; zoo: largest model-vs-exact error), each a
table of at-price events (ts, action, size, effect, ahead/behind, queue ahead after, rank) plus a step chart of exact
queue ahead vs model estimate when trajectories exist; config echo (spec JSON) and file hashes.

Additions (as built): the zoo section shows never-cancel results (leaderboard)
and `cancel_bearing/` results in separate tables (A2); the scorecard section reports taker fills separately from
maker fills (A5) and shows the cluster-robust test as primary with plain McNemar labelled secondary (A10); a
referee section renders `qt.referee_report`; `calibrate-live` output is rendered from `live_calibration_test.json`.
Inputs are recognised by their `schema` / `qt.schema` metadata, not file names. Final pass: the report reads the 1.1 fields (`discordance_clustered`, `fills_only`, `window_ns`, `n_taker_fills`, twin
matching, `liquidity`, `trust`), with the warning prefixes as a fallback; section order verdict, data_quality,
two_by_two, discordance, markouts, time_to_fill, position, zoo, calibration, learned_model, referee, why, config; the
zoo leaderboard reuses `summary.json` with the 5.6 ranking and sentences; calibration shows "Exportable" / "Not
exportable" from `trust`; why traces show the model queue estimate per event and the first divergence
(`why_explain`); NOT SCORED scorecards render a callout; markouts in ticks are labelled "605-style, not NBBO" and a
bps / net-of-fees table reads the `qt.score_tests` rows; the calibration likelihood profile uses train events only
(review E4).

### 5.10 Export templates (`queuetruth.export`)

hftbacktest (`hftbacktest_snippet`), exact layout (values substituted, `{n:.6g}`):
```python
# queuetruth {version} | calibration {run_id} | {family} fitted on {train}; held-out {test}
# data: {data_sources}
# Paste into your existing BacktestAsset chain (keep your data/tick/lot/fee settings).
from hftbacktest import BacktestAsset
asset = (
    BacktestAsset()
    # ... your existing .data(...), .tick_size(...), .lot_size(...), fee model ...
    .{builder}({args})
    .{exchange}()
    {latency_line}
)
```
`builder`/`args`: power -> `power_prob_queue_model(n)`, power2 -> `power_prob_queue_model2(n)`, power3 ->
`power_prob_queue_model3(n)`, databento with `k <= 0` -> `power_prob_queue_model3(1/(1+k))` (for `k > 0` no hbt
equivalent exists: the builder line is omitted with a note) (expected-value approximation of DB's
Monte Carlo, noted), log -> `log_prob_queue_model()`, log2 -> `log_prob_queue_model2()`, risk_adverse ->
`risk_adverse_queue_model()`. `exchange`: `partial_fill_exchange` or `no_partial_fill_exchange` per the fit's
exchange. `latency_line`: `.intp_order_latency("{npz}")` when a latency calibration exists, else omitted. Notes
always include hbt #316/#312 (partial-fill accounting in released versions) and the conflated-L2 caveat.
NautilusTrader (`nautilus_snippet`), layout for an NT touch-family fit with every latency component fitted (a
line that is not fitted is emitted as a comment instead; `queuetruth.export.nautilus`):
```python
# queuetruth {version} | calibration {run_id} | NautilusTrader 1.x and 2.x (names checked on 1.231.0 and 2.0.0rc7;
# 2.x renamed FillModel to DefaultFillModel and LatencyModel to StaticLatencyModel; the import picks either)
try:  # nautilus_trader 2.x
    from nautilus_trader.execution import DefaultFillModel as FillModel, StaticLatencyModel as LatencyModel
except ImportError:  # nautilus_trader 1.x
    from nautilus_trader.backtest.models import FillModel, LatencyModel
fill_model = FillModel(prob_fill_on_limit={p:.6g})
# not fitted: prob_slippage, random_seed (set them as in your current FillModel)
latency_model = LatencyModel(base_latency_nanos=0, insert_latency_nanos={insert}, update_latency_nanos={update}, cancel_latency_nanos={cancel})
# queue_position=True and trade_execution=True on the venue config when the fit was queue-gated ({gated})
```
Export refusal (`queuetruth.export.trust`): `export hftbacktest|nautilus` assesses
the calibration's trust (5.4) and refuses a result whose verdict is `refused` with `QT-CALIB-001` (exit 1), naming
the refusing tokens. `--force` (`ExportSpec.force`, `qt.export(..., force=True)`) writes it anyway, preceded by:
```
# !! NOT VALIDATED: `queuetruth export` refuses this calibration (`--force` writes it anyway). Reasons:
# !!   - <token>: <text>
# !! Do not paste these values into a production config without your own out-of-sample check.
```
plus `# fit: ...` and `# held-out: ...` lines and plateau / profile_ci_open / heterogeneous_strata notes. The
NautilusTrader snippet emits only fitted values: `FillModel(prob_fill_on_limit=...)` only for an NT touch family
with a finite `p`, and `LatencyModel` components only where samples exist; anything else is a commented line with
the reason (review E3, E7). On real data the RD-9 power3 fit (n = 3.10) is refused: held-out log loss at 5 min
0.0461 vs 0.0414 for n = 2.

Replay adapters (`export replay-schedule --target hftbacktest|nautilus`,
`queuetruth.export.replay.replay_schedule`, `api.export_replay_schedule`): from a schedule (`--schedule` /
`--amends`, or `--live` with `--data`) write a self-contained replay of the user's orders for their backtester:
hftbacktest (`hftbacktest_replay_script`: a Python script that drives hftbacktest with the schedule and writes
order snapshots JSONL plus `<out>.summary.json`; run end to end in the tests) and NautilusTrader
(`nautilus_replay_strategy`: `ScheduleReplayConfig` / `ScheduleReplayStrategy` writing `qt_nautilus_events.jsonl`;
text only, not executed in the tests). The output is scored back with `score --sim-log` after `live import --venue
sim_hftbacktest|sim_nautilus`. Latency `.npz`: rejects without an exchange timestamp, negative latencies and
non-match/gateway acks are dropped and counted; `.npz` is appended to the path.

`export l2-npz` takes `ExportSpec.feed_latency_ns` (`--feed-latency-ns`, default 0) for `local_ts` (rev
2026-10-19: E6). The NT `prob_fill_on_limit` value is the C3 estimate of 5.4 (trade-through/cross first fills
censored, E28).
Latency `.npz` (`write_latency_npz`): `np.savez_compressed(out, data=arr)` with dtype
`[("req_ts","<i8"),("exch_ts","<i8"),("resp_ts","<i8"),("_padding","<i8")]`, one row per submitted order sorted by
`req_ts` (`submit_sent.ts_local_send`, `ack.ts_exchange` or 0 for rejects, `ack/reject.ts_local_recv`), `_padding = 0`.

## 6. Oracles and acceptance tests (no claim ships without its oracle)

Test ids are stable; each test's name contains its id (e.g. `fn o_book_1_differential()`, `def test_o_stat_1_...`).
Commands are in the Makefile and [How queuetruth is tested](../testing.md). Synthetic data comes only from `qt_testkit` (Rust) or
`queuetruth.live.synthetic_log` / test-local generators (Python).

### 6.1 Book and replay

- **O-BOOK-1** differential (proptest, >= 2,000 cases, up to 3,000 records each, venues nasdaq/cme/generic): after every
  record `qt_book::Book` and `reference::RefBook` agree on BBO, every level (price, total, count), the FIFO order of
  every level and `queue_ahead_of` for every resting order.
- **O-BOOK-2** `Book::audit()` passes after every record in O-BOOK-1.
- **O-BOOK-3** invariants are all zero on `qt_testkit::generate` streams (5 seeds x 3 venues) and equal the injected
  counts on corrupted streams (unknown cancel, duplicate add outside snapshot, crossed add, M on nasdaq).
- **O-BOOK-4** every et `book.rs`, `replay.rs`, `tests/wave3.rs`, `tests/wave4.rs` (et file names) unit/integration test is ported
  (renamed by topic) and passes, including the modify-priority truth table and sweep grouping cases, plus the CME
  rows "M same price same size -> Replace" and "M same size after F in the same event -> Replace" (built in `crates/book/tests/et_book.rs:43`).
- **O-PAR-1** `bbo = full` rows and derived trades equal `SynthOutput.expected_bbo` / `expected_trades` exactly
  (5 seeds x 3 venues); `bbo = change` equals the full stream with consecutive duplicates removed.
- **O-PAR-2** DBN round trip: `encode_dbn` -> `ingest --format dbn` -> replay equals replaying the events directly
  (summary and file sha256s, excluding `inputs`).
- **O-PAR-3** ITCH: per-message-type byte fixtures produced by `qt_ingest::itch::encode` from the ITCH 5.0 spec layouts parse to
  the expected `Message`; synthetic ITCH day -> `ingest --format itch` yields the expected MBO/trade records (et
  converter semantics: `T` print, `F`, then `C` per execution; one `sequence` per message; `LAST` on the message's
  last record; replace = `C` + `A` with the new reference; price x 1e5 to 1e-9 scale).
- **O-PAR-4** etcsv -> Parquet ingest -> replay equals replaying the CSV records directly.
- **O-DQ-1** data-quality verdict rules of 5.1.5 on constructed counters, including: non-zero `hidden_prints` /
  `off_price_executions` alone give `clean`.

### 6.2 Shadows (hand-built scenarios, venue `cme` unless stated, tick 1, instrument midnight 0, close 86,400 s)

Expectations hold for both fill rules (5.2.3a) unless a row states `exact:` / `et:` values.
Base book E0 (`ts = 10`, one event): `A o1 B 100 x60`, `A o2 B 100 x40`, `A o9 A 101 x500`. Shadow X: bid 100,
`ts_submit = 15`, `latency_ns = 0` (so `ts_live = 15`). In each event, an execution is written `T <aggr> <px> <qty>`
followed by `F <id> <qty>` and `C <id> <qty>` per filled resting order (CME pairing within the event). Every record
has `sequence` = its 1-based index in the whole stream, `ts_recv = ts_event`, `channel_id = 0`, and the last record of
each event carries `LAST`. Expected values are normative (`qa` = queue ahead, `rk` = rank).

| id | qty | events after E0 | expected |
|---|---|---|---|
| S1 | 10 | E1 `ts=20`: `A o3 B 100 x30`. E2 `ts=30`: `T A 100 60; F o1 60; C o1 60`. E3 `ts=40`: `T A 100 50; F o2 40; C o2 40; F o3 10; C o3 10` | activated at E1 (`ts_activated_event = 20`), `qa_at_live = 100`, `rk = 2`, `level_qty_at_live = 100`; one fill: `ts 40, qty 10, cum 10, leaves 0, reason queue_depleted, queue_ahead_before 40, traded_at_px_event 50, behind_volume_executed 10`; `end_state filled`, `queue_ahead_at_first_fill = 40` |
| S2 | 30 | as S1 but `o3 x50`, plus E4 `ts=50`: `T A 100 25; F o3 25; C o3 25` | fills: `(40, 10, cum 10, qa_before 40)`, `(50, 20, cum 30, qa_before 0)`; `ts_first_fill 40`, `ts_full_fill 50`, `n_fills 2` |
| S3 | 10 | E1 as S1. E2 `ts=30`: `C o1 60`. E3 `ts=40`: `T A 100 45; F o2 40; C o2 40; F o3 5; C o3 5` | trajectory (ahead): live `(15, qa 100, rk 2)`; E2 `qa 100->40, rk 2->1, cancel_ahead 60`; E3 `qa 40->0, rk 1->0, traded 45, filled 5`; one fill of 5 at 40; end `open_at_end`, `filled_qty 5` |
| S4 | 10 | E1 as S1. E2 `ts=30`: `C o3 30` | no `ahead` trajectory row for E2; `full` mode row: `cancel_behind 30`, `level_qty 130->100`, `qa 100->100` |
| S5 | 10 | E1 as S1. E2: `C o1 20` | E2: `qa 100->80`, `rk 2->2`, `cancel_ahead 20` |
| S6 | 10 | E1 as S1. E2: `M o1 B 100 x70` (size increase, loses priority). E3: `T A 100 45; F o2 40; C o2 40; F o3 5; C o3 5` | E2: `qa 100->40`, `rk 2->1`, `cancel_ahead 60`, `add_behind 70`; E3 fill 5 |
| S7 | 10 | E0 also has `A o4 B 99 x50`; no o3. E1 `ts=20`: `A o5 A 102 x10`. E2 `ts=30`: `T A 100 100; F o1 60; C o1 60; F o2 40; C o2 40; T A 99 20; F o4 20; C o4 20` | no queue_depleted fill (traded 100 = ahead 100); one fill `qty 10, reason trade_through, ts 30, queue_ahead_before 100` |
| S8 | 10 | E1 as S1. E2 `ts=30`: `A o6 A 100 x5` | `et:` fill 10 `reason crossed` at 30. `exact:` the 5 offered lots go first to o1/o2 (ahead, priority), so no fill; `crossed_unfilled` counted; `end_state open_at_end` (built in `crates/testkit/tests/shadows_table.rs:392`). Both: `crossed_books_after_last` counted |
| S9 | 10 | E1 as S1; shadow price 101 (at the best ask) | fill 10 `reason crossed_at_live` at `ts 15`, `queue_ahead_at_live 0`; `exact:` taker fill at `fill_px 101` (opposite level) against o9 |
| S10 | 10 | S1 events, shadow `latency_ns = 5` (`ts_live = 20`) | not live at E1 (`20 > 20` is false); activates at E2 with `qa 130, rk 3`; no fill by E3 |
| S11a | 10 | S1 events, `ts_cancel = 35` | cancelled at the start of E3 (`35 < 40`); `filled_qty 0`, `end_state cancelled`, `ts_cancel_effective 35` |
| S11b | 10 | S1 events, `ts_cancel = 40` | E3 applies (`40 < 40` false): fills 10 at 40, `end_state filled` |
| S12 | 10 | venue `nasdaq`; E1 as S1; E2: `T N 100 500` (hidden print, no F/C) | no fill; `hidden_prints = 1`; `exact:` `hidden_at_px_with_display_ahead = 1` (displayed queue ahead 100 remains) |
| S13 | 30 | S2 events; amend `ts_send = 25, new_qty = 10` | from E2 `qty = 10`; one fill of 10 at 40; `end_state filled` |
| S14 | 10 | E1 as S1; E2 `ts=30`: `R` | `end_state censored_clear` at 30 |
| S15 | - | two shadows with the same `shadow_id` | `QT-INPUT-003` |
| S16 | 10 | E1 as S1; `price_mode touch`: offset 0 -> 100; -1 -> 99; +1 -> 101 -> `rejected_would_cross`. Separately, base book E0 without o1/o2 (no bids) + E1: `rejected_no_touch` | as stated; rejected shadows have no fills and no trajectory rows except `end` |
| S17 | 10 | venue `nasdaq`; E1 as S1 without o3; E2: executions of o1 60 and o2 40 (F/C, ITCH E messages); E3: `T N 100 50` | `exact:` fill 10 `reason queue_depleted` at E3 (hidden print with no displayed order left at 100; built in `crates/replay/tests/audit.rs:122`). `et:` / policy `ignore`: no fill. Variant with o3 30 resting behind: `exact:` no fill under E37 A3d (displayed interest at 100 remains) |
| S18 | 10 | E1 as S1; E2: o1 executed by a non-printable execution (`F o1 60; C o1 60`, no T); E3: `T A 100 45; F o2 40; C o2 40; F o3 5; C o3 5` | `exact:` fill 5 at E3 (`queue_depleted`, execution of o3 behind). `et:` no fill (traded 45 < ahead 100) (covered by referee class 13, no S18 unit test yet) |

- **O-SH-1** S1-S16 pass exactly (all `qt.shadow_results`, `qt.shadow_fills`, `qt.queue_trajectory` columns).
- **O-SH-2** differential vs `qt_testkit::reference_shadows` (independent naive implementation of 5.2): >= 1,000
  proptest cases with random streams, random shadows (abs and touch), latency with jitter, cancels and amends: equal
  results, fill timelines and trajectories.
- **O-SH-3** properties: `sum(fill_qty) = filled_qty <= qty`; `cum_qty` strictly increasing; `queue_ahead_after <=
  queue_ahead_before` on every trajectory row; a `queue_depleted` fill implies cumulative traded at price > ahead;
  replay with and without shadows produces byte-identical `bbo`, `trades`, `depth`, `rank_trace`.
- **O-SH-4** mirror shadows on synthetic data: `shadow_only = 0` and no shadow first fill before the real order's
  first fill (5.2.10).
- **O-SH-5** counterfactual oracle (`python/tests/oracle/`, driver
  `scripts/oracle/run_oracle.py`): an independent price/display/time matching engine (`qt_oracle`, written from
  3.2 and A1-A6 without reading `crates/book`/`crates/replay`) reconstructs the taker flow from the tape's
  executions, inserts each shadow as a real resting order into a private copy of the book and re-matches that flow;
  queuetruth's `exact` rule must equal the oracle's fills (quantity, event, reason class) on the hand-built audit
  scenarios, the referee fixtures and `synth_small`, within the oracle's stated limits (`qt_oracle.LIMITS`: static
  taker flow, invisible IOC remainders, hidden liquidity known only when it prints, no auction model). Under
  `fill_rule = et` the hidden and off-price cases are expected to differ, and the test asserts that. Own orders
  (A1, oracle 0.1.2): an excluded order stays in every other shadow's fork; the fork of the shadow that enters at
  its record (the 5.2.4a linking rule) is copied from the tape just before that record, never receives the order's
  `A`/`C`/`M` records, and turns each of its executions into the shadow's own fill at the execution price; an
  excluded order no shadow enters at is removed for everyone (`OracleConfig.twin_linking = False` removes all).
  Scenarios S20-S20i (off-display twin execution, 1 ns alignment, same-timestamp bursts, a hidden print at the
  twin's price, another shadow's twin, a twin added by a replace mid-event, input-order tie-break, a later CME leg)
  and `synth_small/own_orders` (200 live orders reproduced exactly) must match queuetruth exactly. A filled
  shadow's queue ahead in the event that completed it is a convention (0 in the oracle) and is not compared. The
  oracle's limits are documented in the docs page "How it's tested". Real data (E37, reported
  before the E37 fixes): engine vs oracle 2,981/2,989 of 3,000 shadows; et vs oracle 2,966/2,973 (et is
  pessimistic).

### 6.3 L2 and zoo

- **O-L2-1** `L2Builder::aggregate_level` equals `Book::level` (total, count) after every event (synthetic, all venues).
- **O-L2-2** `VisibleBook` rebuilt from the emitted rows equals the book's top-N (cadence 0) after every batch;
  `depth_levels = 0` reproduces the full book; with cadence, equals the book at each emission time.
- **O-L2-3** `OUT_OF_VIEW`/`INTO_VIEW` flags on constructed scroll-out/scroll-in sequences; trade-before-depth ordering.
- **O-ZOO-1** the golden table of 5.3.3(b), the exec-lots list and the NT golden of 5.3.3(d) match bit for bit
  (`f64::to_bits`) on macOS arm64 (elsewhere within 4 ulp).
- **O-ZOO-2** faithful parity: `python/tests/reference/hbt_ref.py` is a line-by-line Python transliteration of hbt
  `queue.rs` (`RiskAdverseQueueModel`, `ProbQueueModel` + the five probability functions) and of the
  `NoPartialFillExchange`/`PartialFillExchange` fill rules at the pinned commit (MIT, credited), using `math.pow`
  with a NaN rule for negative bases and non-integer exponents, Rust `min` NaN semantics and half-away-from-zero
  rounding. On 200 random L2 sequences per family (fixtures generated by a seeded Python generator and committed as
  Parquet), the Rust zoo's `F` trajectory (bit-exact) and fills equal the reference. Rust test reads the committed
  expected outputs; the Python test regenerates them.
- **O-ZOO-3** (optional, `make hbt-crosscheck` in a separate venv, see `make hbt-crosscheck`): `hftbacktest==2.4.4` driven by `write_hbt_npz` output
  of 3 synthetic days with the shadows as GTX orders gives the same first-fill timestamps as our faithful
  `hbt_risk_adverse` and `hbt_power3[n=2]` with `no_partial`. The tag diff is done: 2.4.4
  equals `5f3ec40` except on modify paths, so it applies to unmodified orders. Each npz carries the
  `<stem>.orders.json` sidecar of 5.3.5; shadows crossing on arrival (GTX expiry) and shadows live across an `R` are
  excluded from the parity counts by construction. The driver lives outside this repository.
- **O-ZOO-4** `hbt_l3_fifo` corrected equals the exact engine (integer fills, times) on random streams; faithful
  equals a Python transliteration of hbt `L3FIFOQueueModel.fill_market_feed_order` on constructed sequences.
- **O-ZOO-5** `databento_mc`: same seed -> identical predictions; `--threads 1` vs `4` identical; with
  `particles = 1, k = -0.8` a hand-traced 5-row sequence with a fixed RNG stream gives the documented positions.
- **O-ZOO-6** bracket property, order by order (5.3.3 h/i; `crates/zoo/tests/gate.rs`
  `bounds_bracket_exact_order_by_order` and `o_zoo_6_bracket_and_alignment`): on every testkit scenario case (the
  E36 / A3d / A4b / A6b corpus), on generated CME, Nasdaq and generic tapes and on `synth_small`, every shadow the
  exact arm fills within `h_max` is filled by `front_of_queue` (faithful and corrected) no later, and every shadow
  `trade_through_only` fills within `h_max` is filled by the exact arm no later; fill counts then satisfy
  `front_of_queue >= exact >= trade_through_only`.
- **O-ZOO-7** (built in `python/tests/eval/test_censoring_a2.py:82`): a perfectly calibrated
  synthetic predictor on shadows with random shared cancels has a `fill_rate_bias` CI containing 0 at every
  horizon (the audit's simulation as a regression test).

### 6.4 Calibration

- **O-CAL-1** recovery: 50,000 synthetic allocation events, `(F, B) ~ U[1,100]^2`, `b_j ~ Binomial(10, pi_n0)`,
  `a_j = 10 - b_j`: `|ln n_hat - ln n0| < 0.02` for each family and `n0` in {0.5, 2.5, 6}; profile CI contains `n0`
  on a pinned, recorded seed (built).
- **O-CAL-2** databento `k0` in {-0.8, 0, 0.5}: `|k_hat - k0| < 0.012` (built).
- **O-CAL-3** NT golden `p_hat = 2/9`; geometric recovery `p0 = 0.2`, 20,000 orders: `|p_hat - p0| < 0.005`; and
  with a competing trade-through process (probability 0.15 before each touch), trade-through first fills censored
  per E28: `|p_hat - p0| < 0.005` (built).
- **O-CAL-4** survival fit recovers `k0 = -0.8` within one grid step on data simulated by the same simulator with a
  different seed.
- **O-CAL-5** `golden_section_max` on 5 known unimodal functions to 1e-7; `alloc_events` on a testkit market satisfy
  `ahead_cancel + behind_cancel` = exact pure cancels at the level and match `qt.queue_trajectory` rows.

### 6.5 Features and learned model

- **O-FEAT-1** no lookahead: features at `t_l` are identical when every input record with `ts_event > t_l` is deleted
  (property test over random landmarks).
- **O-FEAT-2** hand-computed features on two scenario fixtures (imbalance, OFI, wmid offset, tracker values).
- **O-MOD-1** person-period expansion counts and targets on a 5-row example; **O-MOD-2** CIFs monotone in horizon,
  `CIF_fill + CIF_cancel <= 1`; **O-MOD-3** on a synthetic process whose fill hazard depends on imbalance and queue
  share, the learned model beats climatology (BSS lower CI > 0 at 1 s and 10 s); **O-MOD-4** splits: no temporal
  overlap after purge/embargo, orders never straddle splits; **O-MOD-5** `rcll`, `ipcw_logloss`, IPCW Brier equal
  hand-computed values on a 6-order example.

### 6.6 Statistics and scorer

- **O-STAT-1** `mcnemar(3, 12)` equals the golden values of 5.7.5 and `statsmodels.stats.contingency_tables.mcnemar`
  (exact and corrected chi-square) on 50 random tables; `clustered_mcnemar` hand formula; on a day-clustered null
  (10 days x 200 orders, per-day tilt U(-0.2, 0.2)) the primary test rejects at most 7% at alpha 0.05 (rev
  2026-10-19: A10; built in `python/tests/stats/test_clustered.py`).
- **O-STAT-2** `wilcoxon_signed_rank` equals `scipy.stats.wilcoxon` (statistic and p) on 20 seeded samples incl.
  zeros and ties; `kaplan_meier`, `aalen_johansen` equal `lifelines` (`KaplanMeierFitter`, `AalenJohansenFitter` on
  tie-free data) to 1e-12; `logrank` equals `lifelines.statistics.logrank_test` to 1e-9.
- **O-STAT-3** `gray_test` (implemented from Gray 1988; cmprsk is GPL and used only as the reference oracle, E4)
  equals committed reference values from R `cmprsk::cuminc` (`tests/fixtures/reference/gray_cmprsk.json`, generated
  by `scripts/reference/gen_gray.R`; rel tolerance 1e-9). The fixture exists (cmprsk 2.2.12 installed), so the
  `requires_gray_reference` skip no longer applies.
- **O-STAT-4** BH/BY/Holm equal `statsmodels.stats.multitest.multipletests`; bootstrap CI deterministic for a seed;
  TOST on constructed estimates.
- **O-STAT-5** `brier`, `murphy` (with `within_bin`), `hosmer_lemeshow`, `reliability_table` equal reference values
  computed once by running et's `python/exectruth/analytics/calibration.py` source under the project venv (imported
  read-only from its path) on seeded inputs, committed as `tests/fixtures/reference/et_calibration.json`.
- **O-LIVE-1** each importer: synthetic fixture files written from the documented formats (fake accounts, our own
  values) -> golden canonical rows; Bybit double `Filled` dedupe; FIX `H`/`G` bust/correct; Coinbase cumulative
  differencing; OKX fee sign; validator negative cases (gaps in `event_seq`, duplicate fills, `cum_qty` mismatch).
- **O-SCORE-1** synthetic market + live log whose fills are generated by the exact engine on the same schedule:
  `sim = exact` gives `b = c = 0`, bias 0 with CI containing 0, `fill_rate_equivalent = true`.
- **O-SCORE-2** the same live log scored against `sim-model front_of_queue`: `direction = sim_overfills`, CI lower > 0.
- **O-SCORE-3** markouts: prevailing-quote as-of (last BBO at or before `t + h`), null past end of data, `RS = 2 mo` identity, `mo_ccy` units; position series
  end equals the sum of signed fills; amend leg splitting aggregates back to the order; a re-priced leg is sized from
  the instruction, not live fills (A8).
- **O-SCORE-4** (built in `python/tests/score/test_score_gate.py:150`): synthetic live orders
  injected into a synthetic tape (the market reacts to them), exported as a live log and scored `sim = exact` with
  self-exclusion: zero discordance (`b = c = 0`); without self-exclusion `b > 0`.
- **O-SCORE-5**: a live order fully filled at `t` whose sim fills come only after `t` counts
  as `live_only`; fills outside W are ignored in both arms.
- **O-REF-1** every referee fixture (16, 5.8) passes with exact-engine output as the sim; each fixture has a
  deliberately buggy mini-simulator in the tests reproducing its bug class, and the referee reports `pass = false`
  (16 classes with mini-simulators for 12-16, classes 03/08 bugs redefined; built
  in `python/tests/score/test_score_gate.py` and `minisim.py`).

### 6.7 Interfaces

- **O-SCHEMA-1** Rust `qt_schema::schema(name)` and `queuetruth.schemas.arrow.SCHEMAS[name]` are identical for every
  name; **O-SCHEMA-2** `make schemas` regenerates `schemas/json` and `schemas/arrow` with no diff; **O-SCHEMA-3** every
  Rust-produced JSON in the e2e tests validates against its JSON Schema.
- **O-PARITY-1** (e2e) the same spec through `qt.replay()`, `queuetruth replay --format json`, `queuetruth-core
  replay`, and an HTTP replay job yields identical summaries (excluding `timings`, `spec` path fields) and identical
  output sha256s. Same for `zoo` and `score` (Python API vs CLI vs HTTP).
- **O-CLI-1** exit codes 0/1/2 and the `qt.error` JSON for each error class; `--help` exits 0.
- **O-API-1** schemathesis (from the in-process ASGI app, OpenAPI 3.1) stateless checks on every endpoint:
  `not_a_server_error`, `status_code_conformance`, `content_type_conformance`, `response_schema_conformance`.
- **O-API-2** idempotency: same key + same body -> same response, no second job; same key + different body -> 422;
  concurrent duplicate -> 409. **O-API-3** tenant isolation: tenant B gets 404 for every tenant-A id on every
  endpoint, cannot download A's files with B's key, and RLS returns 0 rows for direct SQL as `qt_app` with B's
  `app.tenant_id`. **O-API-4** RFC 9457 bodies for 401/403/404/409/413/422/429. **O-API-5** `service/openapi.json`
  equals the generated document. **O-API-6** rate limits return 429 with `Retry-After` and `RateLimit` headers.
- **O-Q-1** job lifecycle queued -> running -> succeeded; worker crash (lease expiry) -> requeued once, then failed
  after 3 attempts; cancel of running job; lost enqueue recovered by the sweeper. **O-STORE-1** multipart presigned
  upload, completion, sha256 verification, ranged download on `LocalStore` and on `S3Store` against a moto server.
  moto does not verify presigned signatures or expiry, so single-key scope and TTL <= 900 s are asserted on the URL
  structure under moto; 403 enforcement is covered by the LocalStore HMAC tests and the `requires_docker`
  SeaweedFS smoke test.
  **O-BILL-1** usage events have unique `identifier = <job_id>:<meter>`; replays of the push loop never duplicate;
  `NoopBiller` default; `StripeBiller` request shape against an `httpx.MockTransport`. **O-RET-1** retention purge and
  DELETE remove objects and rows and write a tombstone. **O-EGRESS-1** with `QT_ZERO_EGRESS=true` a job's child process
  cannot connect to any host except the store endpoint and the database; the biller is forced to Noop.
- **O-MCP-1** tool list in the exact order of 4.6, each with input/output schemas that validate; submit tools absent
  without `--allow-submit`; local mode lists only L tools and never writes; tool results validate against schemas
  (fake API via `httpx.MockTransport`).

### 6.8 Real-data validation on et artifacts (read-only; outputs under `.realdata/`, never committed)

Inputs: ``<et-root>/out/`` (converted ITCH days; the raw ITCH files are deleted).
Driver: `scripts/realdata/validate_et.py --et-root <et> --out .realdata/` Only aggregate numbers may be
copied into `docs/RESULTS.md`; no event-level rows, prices series or derived tables leave `.realdata/`.
Expected values, digests and comparison rules are in `scripts/realdata/expected.json` (recomputed from et artifacts). Results are in 6.8a. The et-parity checks RD-2 .. RD-6 replay with `fill_rule = et` and
`hidden_print_policy = ignore`; the default `exact` rule is compared with the O-SH-5 oracle,
not with et. Free disk was 6.8 GiB on 2026-10-18 (the real-data budget is <= 1.5 GB peak).

| id | check | expected |
|---|---|---|
| RD-1 | `ingest --format etcsv --venue nasdaq` of `out/itch-12302019/{mbo,trades,instruments}.csv` (Eastern midnight `1577682000000000000`, price_exp -9, tick 0.01 -> `10000000`, lot 1; `open_s 34200`, `close_s 57600` from the nasdaq default, 3.3) | 4 partitions (instrument ids 13, 5291, 7451, 7992), 6,103,262 records total; records per instrument 1,737,379 / 1,346,429 / 2,341,016 / 678,438; verdict not raised to `warnings` by the 35,860 side-N prints (E36(b)) |
| RD-2 | `replay --rank-oracle --sweep-gap-ns 2000000 --bbo full --parity-trades` | records 6,103,262; events 5,423,833; trades 211,867; all 8 invariants 0; executions_checked 176,103; price_priority_violations 0; rank0_share 0.9757528264708721 and sweep-adjusted 0.9872290648086631 (abs diff < 1e-12); sweeps 87,901; sweeps_multi_fill 37,990; trade parity 211,867 compared, 0 mismatches |
| RD-3 | BBO full stream rendered as et CSV rows (streamed, never written) | per instrument_id, the sequence of rows equals the rows of `out/replay-itch-12302019-v2/bbo.csv` for that instrument (et interleaves instruments in file order; compare per-instrument sha256 of the rendered rows) |
| RD-4 | `rank_trace` vs `out/replay-itch-12302019-v2/rank_trace.csv` (streamed, column-mapped, per instrument_id) | 176,103 rows; all columns equal except `sweep_id`, which is compared as a grouping (same partition of rows into sweeps, ids relabelled; et numbers sweeps globally) |
| RD-5 | shadows from `out/fidelity-itch/shadows.csv` (3,000, et CSV layout), `fill_rule = et` | per shadow equal to `out/fidelity-itch/engine/shadow_fills.csv`: `ts_live`, first fill ts (`ts_fill`), `filled_qty`, `queue_ahead_at_live` (= `queue_ahead_at_submit`), `queue_ahead_at_first_fill`, reason class (`trade_through`/`crossed`/`crossed_at_live` -> et `crossed`); sum of per-fill qty = `filled_qty`; trajectory -> et `queue_trace.csv` (11,680 rows) by the rule: keep the `live` row unless the shadow filled at activation or was cancelled before its activating event (`ts_cancel_effective < ts_activated_event`), keep `event` rows except the event whose fill brought `leaves_qty` to 0, drop `end` rows, then drop consecutive repeats of `queue_ahead_after`; plus the SPEC-reference fills digest (366 fill rows; reasons queue_depleted 114, trade_through 53, crossed 68, never 2,765) |
| RD-6 | day two: `out/itch-01302020`, `out/replay-itch-01302020`, `out/fidelity-01302020` (midnight `1580360400000000000`) | records 10,651,300; events 9,361,483; trades 421,420; executions_checked 314,369; rank0 0.9867925908725097 / 0.9908865059850048; sweeps 161,037; multi 65,081; invariants 0; BBO per-instrument equal (as RD-3); rank_trace as RD-4; shadows equal (as RD-5) |
| RD-7 | O-L2-1 on both real days | 0 mismatches |
| RD-8 | zoo, day one: 3,000 sampled shadows (seed 0) + the 3,000 et mirror shadows, all 17 models, faithful and corrected | runs; `hbt_l3_fifo` corrected equals exact; bracket property holds; leaderboard recorded |
| RD-9 | calibration: power3 `n`, databento `k`, NT `p` fitted on day one (blocks 0-3 of 5) and evaluated on day two, on the two-day dataset from `merge_datasets.py`; `train = {dates: [day one], blocks: {index: [0,1,2,3], n_blocks: 5}}` read as the intersection | finite estimates with bootstrap CIs (30-minute blocks: one train date); held-out table recorded |
| RD-10 | learned model: features for 20,000 sampled shadows per day, `split auto` (train/calib day one, test day two) | BSS vs climatology lower CI > 0 at 1 s and 10 s (et's +0.09 is skill vs a constant base rate on real orders with a within-day split, a weaker baseline than the stratified AJ climatology here) |
| RD-11 | scorecard: live = canonical log built from 3,000 sampled real orders per day (`scripts/realdata/real_orders_to_live.py`: submit/ack at the add `ts_event` (match_time), fills from the tape, cancel at the removal; default sample = et's 3,000) | `sim = exact` with self-exclusion (A1 native, 5.7.3): the sim arm reproduces the live orders: `live_only = sim_only = 0` and identical fills on every order (this replaced the et 2x2 expectation, which compares a different construction); no sim first fill before the live first fill; `sim-model` for each zoo model: signed-bias table recorded |
| RD-12 | crypto: et `data/bitstamp/20261012T020742Z`, `data/bitfinex/20261012T020742Z`, `venue generic`, `size_exp -8`, `options {taker_window_us: 100}` | ingest + replay run; invariants and et verdict counters recorded (Bitstamp `crossed_books_after_last = 0` with taker handling; et reference 77,272 / 51,701 records) |
| RD-13 | performance on day one | section 7.1 targets measured and recorded in `.realdata/bench.json` |

Real-bytes ITCH checks (`scripts/realdata/itch_subset.py` streams the public file and
keeps the 4 symbols, `itch_realbytes.py` runs the checks; results in `.realdata/itch/realbytes-<date>.json`):
ITCH-1 `ingest --format itch` on the subset: records, prints, per-instrument sha256 of every MBO record and source
print rendered in et's CSV layout equal et's, P/Q split equals the raw stream; ITCH-2 the RD-2/3/4 replay checks on
that dataset; ITCH-5 the RD-5 shadow checks (`fill_rule = et`); ITCH-O the O-SH-5 oracle with true P/Q prints
(cross window 0 and 1 s). (ITCH-3/4 are not defined.)

#### 6.8a Results (`.realdata/results.json` 2026-10-19 01:35 UTC with reruns)

[Results](../RESULTS.md) is the maintained record of these numbers and their source artifacts; where this table and that page differ, that page is current.

| id | result | status |
|---|---|---|
| RD-1, RD-1b | both days ingest exactly as expected (6,103,262 / 10,651,300 records, per-instrument counts, 211,867 / 421,420 prints, open/close 34,200 / 57,600); verdict `clean` | pass |
| RD-2 (+RD-3, RD-4) | day one: 6,103,262 / 5,423,833 / 211,867; 8 invariants 0; 176,103 checked, 0 priority violations; rank0 0.9757528264708721 / 0.9872290648086631; sweeps 87,901 / 37,990; trade parity 211,867/0; BBO parity 5,423,833/0; BBO and rank_trace digests equal | pass |
| RD-5, RD-6s | 3,000/3,000 et shadows per day equal (et rule); trace 11,680 / 12,210 rows; fill rows 366 / 317 | pass |
| RD-6 | day two: 10,651,300 / 9,361,483 / 421,420; 314,369 checked; rank0 0.9867925908725097 / 0.9908865059850048; parities 0 mismatches | pass |
| RD-7 | top-3 (both days) and top-5 (day two) depth after every one of 14,785,316 events equal et; 0 unconsumed rows | pass |
| RD-8 | 17 models x 2 modes on 3,000 never-cancel shadows per day; the O-ZOO-6 count bracket (`front_of_queue` >= exact >= `trade_through_only` fill counts) holds; it was not a per-order bound in that run (5 sampled shadows filled in the exact arm but not under `front_of_queue`; the bounds were since redefined, 5.3.3 h/i, and are tested order by order, O-ZOO-6) and faithful `nt_prob_touch[p=1]` exceeds `front_of_queue`; `hbt_l3_fifo` corrected = exact | recorded (leaderboard in docs/RESULTS.md) |
| RD-9 | power3 n 3.098 [2.922, 3.281]; Databento k -0.677 [-0.695, -0.658]; NT touch p 0.287 [0.269, 0.303]; NT queue_l2 touch p 0.895 [0.849, 0.935] (day one fit, day two held out); power3 export refused (QT-CALIB-001) | recorded |
| RD-10 | BSS vs climatology, held-out day two: 0.095 [0.072, 0.122] at 1 s, 0.115 [0.102, 0.128] at 10 s, 0.166 [0.152, 0.177] at 60 s | pass |
| RD-11 day one | with self-exclusion: 2x2 (both, neither, live_only, sim_only) = 250 / 2,750 / 0 / 0, identical fills on all 3,000 orders (earlier runs 242/2,748/8/2 and, without self-exclusion, 90/2,750/160/0) | pass |
| RD-11 day two | with self-exclusion: 195 / 2,805 / 0 / 0, identical fills on all 3,000 orders (without self-exclusion 87 / 2,805 / 108 / 0) | pass |
| RD-12 | Bitstamp crossed books after last record 0; record counts 77,272 / 51,701 equal et's converter | recorded |
| RD-13 | etcsv ingest 1.94M rec/s (target 1.5M, met); `bench replay` 4.26M rec/s (target 8M, missed; load1 43-101, Low Power Mode on) | fail (re-measure quiet) |
| ITCH-1, ITCH-2, ITCH-5 | both days pass (records 6,103,262 / 10,651,300; et record and print digests equal; all RD-2 and RD-5 numbers equal) | pass |
| ITCH-O | qt `exact` vs oracle: 2,942/3,000 (day one; disagreements 57 cross-window, 1 off-price) and 2,958/3,000 (day two; 39 cross-window, 3 oracle divergence) with cross window 0; filled shares equal (37,994 / 22,237) | pass |

Headline (never-cancel shadows, 3,000 per day, faithful mode; `docs/RESULTS.md`): `nt_prob_touch[p=1]` is the clear
overfiller (+3.07 pp [+2.50, +3.62] at 1 s, day one; +2.33 pp day two); `front_of_queue` +3.00 pp; hbt power/log
models within about 1 pp of exact; `hbt_risk_adverse` -0.37 to -1.20 pp; `trade_through_only` down to -3.73 pp; the
refit NT `p = 0.29` cuts the day-two 1 s overfill from +2.33 to +0.22 pp. L2 optimism is model-specific and modest
for hbt on these liquid Nasdaq names (E58). Superseded for the NT families and the bounds by the rerun after the
5.3.3 d-f faithful NT rules and the 5.3.3 h/i bounds: `nt_prob_touch[p=1]` +3.13 pp at 1 s on day one (14.17% vs
11.03%) and +2.80 pp on day two, equal to `front_of_queue`; nautilus_trader 1.231.0 on a delta feed fills the same
orders at every horizon (`scripts/nt_crosscheck`); the refit `p = 0.287` cuts the day-two 1 s overfill from +2.80 to
+0.41 pp. `docs/RESULTS.md` holds the current numbers.

## 7. Performance and determinism

### 7.1 Performance targets (release build, the reference Mac: Apple silicon arm64, 8 cores, 16 GB; single thread unless stated)

| Path | Target | Reference |
|---|---|---|
| ITCH parse + map to `MboEvent`, no output | >= 5.0M msg/s | et 6.05M msg/s parse+convert |
| ITCH -> dataset Parquet (ingest) | >= 2.5M msg/s | measured 2.35M msg/s under load (re-measure quiet) |
| etcsv -> dataset Parquet | >= 1.5M rec/s | |
| Replay from Parquet, book + invariants, no sinks | >= 8M rec/s | et 10.6M rec/s in memory |
| Replay with change-only BBO + trades Parquet sinks | >= 4M rec/s | et 4.7M rec/s writing CSV |
| Replay with rank oracle + rank_trace | >= 3M rec/s | et 1.07M rec/s from CSV end to end |
| Shadow overhead, 3,000 shadows on a 4-symbol day | <= 15% throughput loss vs no shadows | |
| Replay with 100,000 shadows, book + invariants | >= 5M rec/s | was <= 2.5x slower than no shadows until 2026-10-19; the cost ratio is reported, not gated (E74) |
| Trajectory `ahead` | <= 10% extra | |
| L2Builder depth 10, cadence 0 | >= 3M rec/s | |
| Zoo, 17 models x 3,000 shadows, 4-symbol day | <= 240 s single thread, <= 90 s with `--threads 4`, RSS <= 1.5 GB | unmeasured (E32) |
| Features, 20,000 shadows x 3 landmarks, one day | <= 120 s | unmeasured (E30) |
| `fit-hazard`, 60,000 order-landmarks | <= 120 s | |
| `score`, 10,000 live orders, B = 1,000 | <= 60 s | |
| Day-one real pipeline end to end (ingest, replay, zoo, calibrate, features, fit, score, report) | <= 15 min, peak RSS <= 2 GB, scratch <= 3 GB | et `run_fidelity` alone: 570 s, 3.0 GB |
| `qt.replay()` via Python vs `queuetruth-core replay` | within 5% | |

Measured by `make bench` with the qtbench harness : `benches/bench.py` runs the 85 cases of `benches/cases.py` (suites quick, full, realdata, gate =
default) as separate processes, interleaved round-robin, recording per-process wall, user/sys CPU, peak RSS and
load; it drives `queuetruth-core bench replay|l2|itch` (4.4) and the public CLI/API, and writes a
`qtbench.results` document to `.realdata/bench.json` (harness documents are `qtbench.*`, not `qt.*`; wall-clock
fields allowed; `qt.bench` stays the per-process output). Regression gate `benches/check.py` (`make bench-check`):
cost slowdown `median(cur)/median(base) - 1 > 0.20` and one-sided Mann-Whitney p <= 0.05 = `regression`; exit codes
0 ok, 1 regression, 2 error, 3 inconclusive (environment class differs: power source, Low Power Mode, CPU);
not CI-gating in v0.1. `benches/baseline.json` is promoted from a quiet run (AC power, Low Power Mode off, no
builds). `benches/reference_et.json` holds et's numbers measured 2026-10-18 under heavy load (load1
9-93, Low Power Mode on): replay 2M in memory 11.0M rec/s, ITCH parse+convert 5.88M msg/s, day-one RD-2 config
0.735M rec/s, 100,000 shadows 14.7x slower than none; it is a reference to beat, not a baseline.

As built: `benches/` holds the harness; `make bench` (`BENCH_SUITE ?= gate`)
writes `.realdata/bench.json`; `benches/baseline.json` is not promoted yet. Measured 2026-10-19 (full suite, kernel
and ingest cases, Low Power Mode on, so every number is a lower bound; docs/RESULTS.md section 7): ITCH parse 16.66M
msg/s; replay book 14.54M rec/s (2M) and 14.40M (10M); with sinks 6.25M; with rank oracle 5.70M; L2Builder 8.94M;
ITCH ingest 4.56M msg/s; shadow throughput loss 0.13 at 3,000; replay with 100,000 shadows 5.28M (2M), cost ratio
2.76x reported; trajectory extra 0.003; `bench replay` on day one 8.56M-8.60M rec/s. All met. Earlier gate-run
figures not re-measured since: Python vs CLI replay 1.18x (missed, target 5%); etcsv ingest 1.94M rec/s (met); zoo
7.8 s / 8.1 s per day (met); features 10.0 s (met); `fit-hazard` 172 s (missed, 120 s); score 11.4 s (met); peak RSS
1.81 GB.

### 7.2 Determinism requirements

- Same inputs + same spec + same version => byte-identical output files (Parquet, JSON excluding `timings`, HTML,
  npz) across runs and across `--threads` values. Parallelism is only across partitions; results are merged in
  partition order `(session_date, instrument_id)`.
- No iteration over `HashMap`/`HashSet` in any output-affecting path unless the keys are sorted first; use
  `BTreeMap`, `IndexMap` or sorted `Vec`s. Python: no iteration over `set`s of strings in output order; sort.
- All randomness: Rust `Xoshiro256StarStar` seeded through `stable_seed(seed, parts)` with the parts documented at
  each use (latency jitter `["latency", shadow_id]`, particles `[model_id, shadow_id]`, Bernoulli touches `[model_id,
  shadow_id]`, sampling `["sample", instrument_id LE, session_date LE]`, Rust bootstrap `["boot"]`); Python
  `numpy.random.default_rng(stable_seed(seed, [b"bootstrap", test_id]))` with `queuetruth.stats.stable_seed`
  (bit-identical port). Python bootstraps use numpy PCG64 while the Rust calibration
  bootstrap uses xoshiro256**: Python-vs-Rust CIs agree within Monte Carlo tolerance only, never bit for bit (rev
  2026-10-19: E29). The Rust index stream is fixed in 5.4; the Python draw procedure in 5.7.5.
- Floating-point sums in a fixed order (partition order, then event order); Python reductions on sorted data.
  `sklearn` estimators use `random_state = seed`; `OMP_NUM_THREADS` must not change results (tested with 1 and 4).
- Cross-platform: integer outputs identical; values involving `powf`/`ln`/`exp` may differ by <= 4 ulp between
  platforms; bit-exact goldens are asserted on macOS arm64 and with tolerance elsewhere.
- No wall-clock, hostname, username or absolute path in data files; JSON `spec` echoes keep paths as given.

### 7.3 Run identity

`run_id` = first 16 hex chars of `sha256(canonical_json({"engine_version": v, "spec": s}))` where `s` is the spec
with the **execution-only fields removed** (`out`, `threads`; nested `out` too) and every `DatasetRef`/`FileRef`
object replaced by `{"sha256": "<hex>"}` (a dataset's `dataset_id`; a file's sha256; a directory FileRef (live log)
by the sha256 of its `manifest.json`). Canonical JSON = RFC 8785 (JCS): sorted keys, no whitespace, integers as
integers, floats in the ECMAScript number format (so `1e-7`, not `1e-07`). Implemented as
`qt_schema::run_id(spec: &serde_json::Value, engine_version: &str, resolve: &dyn Fn(&serde_json::Value) ->
QtResult<String>) -> QtResult<String>` and its Python mirror `queuetruth.schemas.run_id(spec: dict, engine_version:
str, resolve: Callable[[dict], str]) -> str`; e2e asserts they agree. Summaries echo the spec with the same
execution-only fields removed (paths kept as given), so `summary.json` is byte-identical across `--threads` values
and output directories. Every output-affecting option MUST be a spec field so that it enters `run_id` and can be
set over HTTP/MCP (E38 OC-34); in 1.1 they all are. Keyword overrides are folded into the spec before hashing
(scorer `effective_spec`; native `replay --fill-rule/--hidden-print-policy/--exclude-order-ids` and zoo
`--exclude-order-ids` through `fold_replay_options`). Schema 1.1 fields at their default are dropped before hashing (3.11), so
`schemas/run_id_kat.json` is kept.

### 7.4 Determinism tests

`tests/e2e/test_determinism.py`: replay, zoo, calibrate, features, fit-hazard, score and report on the synthetic
fixture twice and with `--threads 1` vs `4` (and `OMP_NUM_THREADS=1` vs `4`); every output sha256 equal.

## 8. Service: security and multi-tenancy

### 8.1 Processes and configuration

`api` (FastAPI on uvicorn), `worker` (PgQueuer consumer), Postgres (`postgres:16.14-bookworm` in compose, the tested
version; 14+ supported), object store
(SeaweedFS S3 gateway in compose; `LocalStore` for dev and tests). The same `queuetruth` wheel runs inside the worker:
jobs call `queuetruth.api` functions in a child process (`multiprocessing` spawn) per job, with a per-kind timeout
(`QT_JOB_TIMEOUTS_S`, a JSON object `{kind: seconds}` over `replay`, `calibration`, `scorecard`, `report`,
`dataset_validate`, `ingest`; a kind it does not name uses `QT_JOB_TIMEOUT_S`, default 7,200; unknown kinds and
values <= 0 are configuration errors), `RLIMIT_AS` memory cap on Linux
(`QT_JOB_MEMORY_BYTES`, default 8 GiB; no-op on macOS, documented), and a private
temp dir deleted afterwards. Settings (env, `pydantic-settings`, prefix `QT_`): `DATABASE_URL` (owner role, used by
migrations), `DATABASE_URL_APP` (role `qt_app`), `STORE` (`local`|`s3`), `STORE_ROOT`, `S3_ENDPOINT`, `S3_BUCKET`,
`S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `KEY_PEPPER`, `SIGNING_KEY` (local-store URL HMAC),
`PUBLIC_BASE_URL`, `ZERO_EGRESS` (false), `BILLER` (`noop`|`stripe`), `STRIPE_API_KEY`, `MAX_UPLOAD_BYTES`
(21474836480), `PART_SIZE_BYTES` (67108864), `JOB_TIMEOUT_S`, `WORKER_CONCURRENCY` (2), `RETENTION_DAYS_DEFAULT` (30),
`ENV` (`dev`|`prod`; `prod` refuses to start without `KEY_PEPPER`, `SIGNING_KEY` and `DATABASE_URL_APP`).
 `DATABASE_URL_APP` (API, role `qt_app`) and `DATABASE_URL_WORKER` (worker, role
`qt_worker`) are required in every ENV: there is no fallback to the owner DSN. Security settings and the role model
are in 8.11.
Additional settings (as built): `S3_PUBLIC_ENDPOINT` (the host customers reach; presigned
SigV4 URLs are signed against it because SigV4 binds the Host; compose default `http://localhost:8333`),
`STRIPE_API_BASE`, `LEASE_S` (60), `HEARTBEAT_S` (20), `SWEEP_INTERVAL_S` (30), `MAINTENANCE_INTERVAL_S` (10),
`MAX_ATTEMPTS` (3), `PRESIGN_TTL_S` (900, capped at 900), `DB_POOL_MAX` (20), `RATE_LIMITS`, `LOG_LEVEL`,
`WORKER_HEALTH_FILE`; defaults `DATABASE_URL=postgresql:///queuetruth`, `STORE_ROOT=./.qt-store`,
`S3_BUCKET=queuetruth`, `S3_REGION=us-east-1`; `STORE=s3` requires `S3_ENDPOINT`.

### 8.2 Database (plain SQL migrations in `service/queuetruth_service/db/migrations/NNNN_*.sql`, tracked in `schema_migrations`)

Tables (all ids `text` with the prefixes of 4.5; all tenant tables have `tenant_id text not null references
tenants(id)` and RLS): `tenants(id, name, plan, zero_egress bool, retention_uploads_days int, retention_outputs_days
int, created_at)`; `api_keys(id, tenant_id, prefix unique, key_hash, scopes text[], name, created_at, last_used_at,
revoked_at)`; `idempotency_keys(tenant_id, key, method, path, request_hash, state ('in_progress'|'complete'),
response_status, response_body jsonb, created_at, primary key (tenant_id, key))`; `rate_limit_buckets(tenant_id,
key_id, bucket, tokens double precision, updated_at, primary key (key_id, bucket))`; `uploads(id, tenant_id, purpose,
filename, size_bytes, sha256, status, object_key, multipart_id, created_at, completed_at)`; `datasets(id, tenant_id,
name, kind, format, venue, status, upload_ids text[], manifest jsonb, data_quality jsonb, object_prefix, job_id,
created_at)`; `jobs(id, tenant_id, kind, state, spec jsonb, resource_type, resource_id, progress jsonb, error jsonb,
usage jsonb, attempts int, lease_until, enqueued_at, cancel_requested bool, created_at, started_at, finished_at,
files_expire_at, run_id)`; `job_files(job_id, tenant_id, name, bytes, sha256, content_type, object_key, primary key
(job_id, name))`; `replays`, `calibrations`, `scorecards`, `reports` (`id, tenant_id, job_id, spec jsonb, summary
jsonb, created_at`); `usage_events(id, tenant_id, job_id, meter, quantity numeric, unit, identifier unique,
created_at, pushed_at, push_error)`; `tombstones(id, tenant_id, resource_type, resource_id, object_count, sha256s
text[], deleted_at)`; `audit_log(id, tenant_id, actor_key_id, action, resource_type, resource_id, at)`. As built
extra columns: `tenants.stripe_customer_id`, `idempotency_keys.response_headers` and
`locked_at`, `uploads.content_type` and `part_size`, `datasets.ingest`. Migrations: `0001_core.sql`,
`0002_queue.sql`, `0003_pgqueuer.sql` (output of `pgq sql install` for PgQueuer 1.4.0, not idempotent, run once),
`0004_pgqueuer_grants.sql`; the runner records a sha256 per migration, refuses unless its role is superuser or
`BYPASSRLS`, and sets the `qt_app` password from `QT_DATABASE_URL_APP` (never in a committed `.sql`).
RLS: `alter table ... enable row level security; force row level security;` policy
`using (tenant_id = current_setting('app.tenant_id', true)) with check (same)`. The app role `qt_app` is
`NOSUPERUSER NOBYPASSRLS` and owns nothing. Every request and every job runs in transactions that start with
`SELECT set_config('app.tenant_id', $1, true)`. Key lookup by prefix uses a `SECURITY DEFINER` function
`auth_lookup(prefix)` that returns `(key_id, tenant_id, key_hash, scopes, revoked_at)` only. (Superseded by 8.11: `auth_lookup` is dropped; the API
authenticates and binds through `qt_authenticate`; migration `0005_security.sql` adds `qt_worker`, the signed
binding and per-tenant rate-limit buckets.) `qt_tenant_ids()` is
also `SECURITY DEFINER`; `tenants` has RLS keyed by `id`. `qt_app` has `GRANT select, insert, update, delete` on the
four PgQueuer tables and `usage` on their sequences (no RLS: they hold job ids only).

### 8.3 Queue (PgQueuer on the same Postgres; testable here without Docker)

The `jobs` table is the source of truth; PgQueuer only transports `{"job_id": ...}` to entrypoint `run_job`.
Submit: insert the job (`state = queued`) and the idempotency record in one transaction, commit, then enqueue and set
`enqueued_at` (`qt_mark_enqueued`). Because the payload carries only `job_id`, claim, heartbeat, finish, sweep
and mark-enqueued are `SECURITY DEFINER` SQL functions (`qt_claim_job`, `qt_heartbeat`, `qt_finish_job`,
`qt_sweep`, `qt_mark_enqueued`) that return `tenant_id`; the job body then runs in tenant transactions as `qt_app`
. Worker claim (idempotent): `UPDATE jobs SET state='running', attempts=attempts+1,
lease_until=now()+interval '60 seconds', started_at=coalesce(started_at, now()) WHERE id=$1 AND state='queued' AND
NOT cancel_requested RETURNING *` (0 rows -> acknowledge and drop). Heartbeat extends `lease_until` every 20 s and
returns `cancel_requested`; heartbeat and finish are fenced: `AND state='running' AND attempts = <claimed attempt>`,
so a worker that lost its lease can neither extend nor overwrite a later attempt.
Concurrency: PgQueuer 1.4 requires `max_concurrent_tasks >= 2 * batch_size`, so the worker uses `batch_size=1`,
`max_concurrent_tasks=max(2, QT_WORKER_CONCURRENCY)` and an `asyncio.Semaphore(QT_WORKER_CONCURRENCY)`; a worker
may hold one extra picked, not started, job. The psycopg connection must be
`autocommit=True`. Sweeper (every 30 s):
re-enqueue `queued` jobs with `enqueued_at` null or older than 5 minutes; requeue `running` jobs whose lease expired
if `attempts < 3`, else mark `failed` (`QT-ENGINE-001` problem "worker lost"). Cancel sets `cancel_requested`; the
worker checks it between stages and kills the child. Interface (`queue.py`):
`class JobQueue(Protocol): async def enqueue(self, job_id: str) -> None; async def run(self, handler, *, concurrency:
int, stop: asyncio.Event) -> None`. Tests run against an ephemeral cluster created by a pytest fixture (`initdb` +
`pg_ctl` from `QT_PG_BIN`, default `/opt/homebrew/opt/postgresql@16/bin`, unix socket in a temp dir, random port;
skipped with marker `requires_postgres` and a clear reason when the binaries are missing; if `QT_PG_DSN` is set, the
fixture uses that existing server instead of `initdb` and creates a uniquely named database per session, which is how
CI's `postgres:16` service container is used). Fallback: if PgQueuer
cannot be made to work with psycopg 3 on that cluster within a one-hour spike, `queue.py` implements the same
interface with `SELECT ... FOR UPDATE SKIP LOCKED` on a `queue_items` table plus `LISTEN/NOTIFY`, and the deviation is
recorded.

### 8.4 Object store

`ObjectStore` protocol: `create_multipart(key, size_bytes, part_size) -> (multipart_id, [PartUrl])`,
`complete_multipart(key, multipart_id, parts)`, `put_file(key, path) -> sha256`, `get_to_file(key, path)`,
`presign_get(key, ttl_s) -> url`, `head(key) -> {bytes}`, `delete_prefix(prefix) -> int`. `S3Store` (boto3; AWS,
SeaweedFS, moto) and `LocalStore` (filesystem under `STORE_ROOT`, URLs to `/v1/_store/...` signed with
HMAC-SHA256(`SIGNING_KEY`, method|key|part|expiry), `Range` support). `S3Store` uses SigV4 (`s3v4`),
path-style addressing and `request_checksum_calculation` / `response_checksum_validation = "when_required"`
(botocore >= 1.36 default checksums break S3-compatible stores and non-SDK presigned PUTs), with a separate
signing-only client for `S3_PUBLIC_ENDPOINT`. Object keys: `t/{tenant_id}/uploads/{upload_id}/
{filename}`, `t/{tenant_id}/datasets/{dataset_id}/...`, `t/{tenant_id}/jobs/{job_id}/{name}`. Presigned URL TTL <=
900 s and scoped to one key. Upload sha256 is verified by the worker after completion (`uploads.status = verified |
failed`).

### 8.5 Authentication and keys

Format `qt_live_<32 base62>_<6 base62 CRC32 of everything before the last underscore>` (`qt_test_` in `ENV=dev`),
random part from `secrets`. Stored: `prefix` = first 12 chars of the random part, `key_hash` =
HMAC-SHA256(`KEY_PEPPER`, full key) hex; verification with `hmac.compare_digest`. A bad checksum is rejected before
any DB lookup. Scopes `read` < `write` < `admin` (hierarchical). Revocation is immediate. `last_used_at` updated at
most once per minute. Bootstrap: `queuetruth-service admin create-tenant --name NAME` prints the first admin key
once. Keys never appear in logs, problems, job specs or idempotency records: `POST /v1/keys` stores its replay body
with `secret: null`.

### 8.6 Idempotency (Stripe semantics)

`Idempotency-Key` (1..255 chars) scoped to (tenant, key). `request_hash` = sha256 of method, path and canonical JSON
body. First request inserts `in_progress`; on completion stores status and body; a repeat with the same hash returns
the stored response (header `Idempotent-Replayed: true`); a different hash -> 422 `idempotency-key-reused`; a repeat
while `in_progress` -> 409 `idempotency-in-flight`. Validation failures (422 before execution) are not stored.
An `in_progress` record older than 60 s with the same hash is treated as abandoned and taken over; a handler
exception deletes the record (5xx are never stored, unlike Stripe; the job was not committed). Ratified as built
(rev 2026-10-19): a 4xx problem *raised* during execution (e.g. 404 dataset not found, 409 dataset not ready) is not
stored either, because raising rolls back the handler's transaction, so no effect exists for idempotency to protect,
and storing it would pin a transient condition to the key for 24 h (a client that waits for the dataset could not
retry with the same key); a 4xx the handler *returns* with committed effects (`upload-size-mismatch`, which fails the
upload and deletes its parts) is stored and replayed like a success. `request_hash = sha256("METHOD\nPATH\n" + canonical JSON body)`.
Records are pruned after 24 h.

### 8.7 Rate limits

Token buckets in Postgres per key: `read` 120/min (burst 60), `write` 30/min (burst 10), `submit` (POST that creates a
job) 10/min (burst 5). 429 with `Retry-After` and draft-11 headers (documented as draft-based; draft-11 has no
burst parameter, so the sustained quota is advertised): `RateLimit-Policy: "<bucket>";q=<per minute>;w=60` and
`RateLimit: "<bucket>";r=<floor(tokens)>;t=<seconds to next token>` on every response.
Buckets are taken by `qt_take_token(tenant, key_id, bucket, rate_per_s, burst)` inside the tenant transaction.

### 8.8 Tenant isolation and data handling

- Strict isolation: RLS on every tenant table; per-tenant object prefixes; single-key presigned URLs; cross-tenant ids
  return 404; no endpoint aggregates across tenants; calibrations, models and scorecards may reference only the
  tenant's own datasets and uploads; no global model or parameter artifacts are trained from customer data; usage
  data leaves only as metering quantities.
- The service never accepts or stores vendor market-data credentials (Databento, Tardis keys); customers upload data
  they are licensed to process; compute runs on that tenant's data only (the service acts as the customer's processor).
- Retention: uploads and job outputs expire after the tenant's retention (default 30 days, 1..365); a daily
  retention job deletes objects and rows, sets jobs `expired` and writes tombstones (ids, counts, sha256s, time; no
  data). `DELETE /v1/datasets/{id}` hard-deletes through a deletion job; `DELETE /v1/uploads/{id}` hard-deletes
  synchronously and writes a tombstone (as built). `audit_log` records key creation and
  revocation, deletions and retention changes.
- Zero-egress mode (`QT_ZERO_EGRESS=true` or `tenants.zero_egress`): the biller is forced to `noop`; job child
  processes install `queuetruth_service.netguard` (patches `socket.socket.connect`/`create_connection`) allowing only
  the DB host:port and the store endpoint host:port; any other connection raises; `GET /v1/meta` reports it; the CLI
  and MCP never call out except to `QT_API_URL`.

### 8.9 Metering and billing

On job success, in the same transaction as the state change, insert one `usage_events` row per meter with
`identifier = "<job_id>:<meter>"`: `replay_records`, `shadow_orders`, `zoo_pairs` (models x shadows),
`orders_scored`, `cpu_ms`; a daily job adds `storage_byte_days` (identifier
`storage:<tenant_id>:<day>:storage_byte_days`). A periodic pusher sends unpushed rows through
`Biller.push(events) -> list[PushResult]`; `NoopBiller` marks them pushed; `StripeBiller` posts Stripe Billing meter
events (`event_name` = meter, `identifier`, `payload {stripe_customer_id, value}`, `timestamp`) with httpx, never
retries a row whose identifier Stripe acknowledged, and is disabled in zero-egress mode. No real Stripe key is used
anywhere in this repo.

### 8.10 General hardening

JSON bodies <= 1 MiB; uploads <= `MAX_UPLOAD_BYTES`; decompression guard (inflated/compressed ratio <= 200 and
absolute <= 64 GiB) in ingest; all parsers are Rust with bounds checks and fuzzed (below); no shell
invocation with user input; workers resolve ids to their own temp paths (no user-supplied paths server-side);
problems never contain stack traces; structured JSON logs with `request_id`, `tenant_id`, `job_id` and no secrets or
data rows; responses carry `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`; CORS disabled by
default; TLS is terminated by a reverse proxy (documented); the container runs as a non-root user; dependencies are
pinned by `uv.lock`/`Cargo.lock`; CI job `supply-chain` runs `cargo audit --deny warnings` and `pip-audit --strict`
over the locked export (blocking; empty allow-lists in `.cargo/audit.toml`, `.github/pip-audit-allowlist.txt`); the
Dockerfile pins base images by digest and installs only the hashed locked set (`--require-hashes`, then `pip check`)
.

Fuzzing (design extensions rust-fuzz and python-fuzz). The Rust parsers that read user-supplied bytes (ITCH 5.0,
IEX DEEP+, DBN, `qt_ingest::ingest` end to end, etcsv, Parquet and the dataset reader, the seven `qt_crypto`
converters, the exact-decimal JSON reader, `qt_crypto::prepare` and `write`) have libFuzzer targets in `fuzz/`, a
standalone Cargo workspace. Every target enforces: no panic, abort or run above 10 s and no single allocation above
512 MiB (64 MiB for `dbn` and `ingest`); an error caused by input bytes is `QT-INPUT-*`, `QT-CONFIG-*` or `QT-DATA-*`,
never `QT-ENGINE-001`; ITCH and DEEP+ messages round-trip through `encode`; datasets an ingest path wrote read back.
Committed seeds are synthetic or copies of test fixtures; every finding is minimized into `fuzz/regressions/` with a
regression test, and `cargo test` in `fuzz/` replays seeds, regressions and known crashes on stable (`make
fuzz-rust`, local `make gate-3`). Arithmetic on input values is checked or saturating, and readers grow buffers with
the bytes received, never with a length a header announces. The Python parsers that uploads reach (live-log
importers, FIX and QuickFIX readers, the instrument map, the simulator importers and the `dq` loader) have a
Hypothesis suite in `python/tests/fuzz/` (marker `fuzz`, `make fuzz-py`) whose contract is: return or raise
`QtError` with a `QT-*` code, within 20 s and 256 MiB of traced allocations. Open: the pinned `dbn` decoder and the
`arrow-ipc` / `parquet` metadata readers can still panic on crafted files (`fuzz/known-crashes/`).

### 8.11 Security model as built (regression tests in `service/tests/security/`)

Roles. The owner (superuser or BYPASSRLS; migration runner and `create-tenant` only, `QT_DATABASE_URL`) owns every
table and SECURITY DEFINER function and sets the `qt_app` / `qt_worker` passwords from their DSNs. `qt_app` (API):
DML on the 14 tenant tables, select/update on `tenants`, insert on the PgQueuer tables; may run only
`qt_current_tenant()`, `qt_take_token()` and `qt_authenticate()`. `qt_worker`: DML on tenant tables and the
PgQueuer tables; may run `qt_bind_job`, `qt_bind_tenant`, `qt_tenant_ids`, `qt_claim_job`, `qt_heartbeat`,
`qt_finish_job`, `qt_sweep`, `qt_mark_enqueued` (it can reach every tenant by design). Neither app role may be
superuser, BYPASSRLS or own objects; `auth_lookup` is dropped; `create` on schema public is revoked.

Tenant binding. RLS policies compare rows with `qt_current_tenant()`, which returns a tenant only when
`app.tenant_id` carries a valid signature `app.tenant_sig` = MAC over (tenant, backend pid, transaction start)
keyed by `qt_binding_key` (readable only by BYPASSRLS roles); setting the settings by hand or replaying a signature
binds nothing. Bindings are made only by definer functions: the API's `qt_authenticate(prefix, key_hash)`
(key_hash = HMAC-SHA256(`QT_KEY_PEPPER`, key), re-presented in every request transaction so revocation is
immediate), the worker's `qt_bind_job(job_id, attempt)` (running job with that attempt only) and `qt_bind_tenant`
(maintenance). `qt_finish_job` also requires the bound tenant. Pools run `verify_role` at startup and refuse a role
that is or inherits superuser/BYPASSRLS, owns objects, or has the other role's functions; `ENV=prod` refuses an app
or worker DSN equal to the owner DSN. Every tenant query also filters by `tenant_id` explicitly.

Limits and hardening (S-M1..M5, L1..L7): local-store parts must be exactly their expected length and completion
checks sizes (4.5); storage is billed on measured bytes (`uploads.stored_bytes`); pending uploads expire after
`QT_PENDING_UPLOAD_TTL_S` (86,400); archives are extracted in the job child, never the worker parent, with at most
100,000 members, 4,096 bytes overhead per entry counted in the 200x ratio, 64 GiB, links and special files refused,
and tar extraction refused on Python versions without the patched `filter="data"`; job children get an allow-listed
environment (PATH, LANG, LC_*, TZ, SYSTEMROOT; HOME and TMPDIR in the job's private directory under `QT_JOB_ROOT`)
and an audit hook that refuses unpickling; rate-limit buckets are per tenant (`rate_limit_buckets` keyed by
(tenant_id, bucket)); at most `QT_MAX_ACTIVE_KEYS` (25) unrevoked keys per tenant; compute knobs bounded by
`QT_COMPUTE_LIMITS` (threads 16, particles 10,000, bootstrap b 10,000, perm_b 20,000) at the API and in the
worker; `QT_TENANT_MAX_RUNNING` (1; 0 = no cap; raise for single-tenant self-hosting) running jobs per tenant,
capped jobs deferred `QT_TENANT_DEFER_S` (5); failed authentications throttled per client (`QT_AUTH_FAILURE_LIMIT`
30/min, burst 10); access logs redact signatures, `X-Amz-*` and DSN passwords; netguard also patches `sendmsg` (a
tripwire: the boundary is the compose `backend` network, `internal: true`); MCP local tools refuse symlinks out of
`QT_MCP_ROOT`; `HazardModel.load` needs a trusted digest. The dev signing key is random per store root
(`<STORE_ROOT>/.signing-key`, mode 0600); in dev an unset `QT_KEY_PEPPER` falls back to a public constant.

Prod secret rules: placeholder or weak values (`change-me`, `example`, `secret`, `password`, `default`, fewer than
8 distinct characters) are refused; `QT_KEY_PEPPER` and `QT_SIGNING_KEY` >= 32 characters; S3 secret and every DSN
password >= 16; `QT_FORWARDED_ALLOW_IPS=*` refused (default empty: trust no proxy header).

Deploy defaults (compose): `QT_ENV=prod`; API and S3 published on 127.0.0.1 only (`QT_API_BIND`, `QT_S3_BIND`);
required secrets `QT_PG_PASSWORD`, `QT_APP_PASSWORD`, `QT_WORKER_PASSWORD`, `QT_S3_ACCESS_KEY_ID`,
`QT_S3_SECRET_ACCESS_KEY`, `QT_KEY_PEPPER`, `QT_SIGNING_KEY` (`.env.example` holds placeholders that prod refuses);
only `migrate` gets the owner DSN; worker `mem_limit` 18g, `pids_limit` 512, API 2g / 256; read-only containers,
tmpfs `/tmp`, `cap_drop ALL`, `no-new-privileges`; postgres, migrate and worker on the internal `backend` network
only. Create a tenant with `docker compose ... run --rm migrate queuetruth-service create-tenant --name NAME`.

## 9. Licensing and data guardrails

- Code: Apache-2.0 (`LICENSE`, `license = "Apache-2.0"` in every `Cargo.toml` and `pyproject.toml`). Ported et code is
  the author's own and is relicensed here.
- No market data in the repository, wheels, images or docs: no ITCH, DBN, Tardis, Coinbase, Bitstamp or Bitfinex
  records, not even small excerpts, and no tables derived event-by-event from them. Fixtures are synthetic or generated
  by our own code (`qt_testkit`, Python generators) and listed in the `MANIFEST.sha256` of their fixture directory
(`tests/fixtures/<dir>/MANIFEST.sha256`, owned by that directory's owner). Importer fixtures
  are written by us from the documented formats with fake accounts and values. The Databento `dbn` test file in et is
  not used (synthetic DBN only), so no Databento fixture notice is needed; if one is ever added, `NOTICE` must credit it.
- Real-data validation reads et's local artifacts in place and writes only to the gitignored `.realdata/`; only
  aggregate statistics are published (`docs/RESULTS.md`), with the source named (Nasdaq TotalView-ITCH public sample
  days) and links to Nasdaq EMI rather than copies.
- hftbacktest (MIT): logic ported in `crates/zoo/src/hbt*.rs`, `python/tests/reference/hbt_ref.py`; `NOTICE` carries
  the hftbacktest copyright line and the full MIT text is in `third_party/licenses/hftbacktest-MIT.txt`.
- NautilusTrader (LGPL-3.0): behaviour re-implemented from its public documentation and the research notes only;
  no NT source is copied, translated line by line, or vendored; `NOTICE` states this.
- Databento tutorial estimator: re-implemented from documented behaviour; credited in docs.
- R `cmprsk` (GPL >= 2) is used only to generate the committed reference numbers of O-STAT-3; Gray's test is
  implemented from Gray (1988, Ann. Stat. 16:1141), not transliterated from `crstm.f`.
- Service terms (docs): customers warrant licences for the data they process; no pooling; Coinbase- and
  Tardis-derived models are never offered to third parties by the hosted service.

`NOTICE` :
```
queuetruth
Copyright 2026 the queuetruth authors. Licensed under the Apache License, Version 2.0.

This product contains logic ported from hftbacktest (https://github.com/nkaz001/hftbacktest,
commit 5f3ec40b2afb764e0fea112f941ed85523ef4e88), Copyright (c) 2022 nkaz001@protonmail.com, licensed under the MIT License
(see third_party/licenses/hftbacktest-MIT.txt).

NautilusTrader (LGPL-3.0) queue-position and fill-model behaviours are re-implemented from public
documentation; no NautilusTrader source code is included.

Databento's queue-position tutorial estimator is re-implemented from its documented behaviour.
```

## 10. Design extensions

The changes below were designed and built after the v0.1 text of this file. Each has a design note in
`docs/design/extensions/`, which is the contract its code was built against; where a note changes behaviour that a
section above describes, the note describes the code as built until its text is merged here. Only 5.2.4a (rules D1
to D3), 5.2.7 (halts), 5.5.8, the 5.7 output directory, the 5.10 NautilusTrader snippet and 8.10 (fuzzing) have been
merged so far. The "Open conflicts" list below predates every note.

| Note | What it adds | Sections it amends or adds to |
|---|---|---|
| [agent-sandbox](extensions/agent-sandbox.md) | Agent sandbox: a simulated exchange over one replayed instrument-day, with MCP sandbox tools | 1.1, 3.10, 4.3, 4.6 |
| [atlas-trace](extensions/atlas-trace.md) | Per-symbol exactness, trading-state labels and flag tracing in the W1 scale pipeline | none (the `scripts/scale/` manifest contract); its finding F2 concerns 1.2 |
| [certificates](extensions/certificates.md) | Run certificates, `verify-cert`, hash commitments and signing keys | 0.2, 3.10, 4.3, 4.4, 7.2, 7.3 |
| [conformal-fill](extensions/conformal-fill.md) | Conformal fill probabilities, `fit-hazard --conformal-alpha` | new 5.5.9; 3.4 and 3.10 (`qt.conformal_*`); 4.4 |
| [cost-decomposition](extensions/cost-decomposition.md) | Implementation-shortfall decomposition of the sim-live gap | new 5.7.6; 3.4 (`qt.score_shortfall`); 5.7 output directory |
| [databento](extensions/databento.md) | DBN definitions, symbology, multiplier and currency, the XNAS.ITCH re-issue | 1.2 (non-FIFO books rejected), 2 (`qt_ingest` modules), 3 (DBN input), `qt.data_quality` |
| [demo-command](extensions/demo-command.md) | `queuetruth demo` | 4.4 (new subsection), 3.10 (`qt.demo_*`) |
| [fill-toxicity](extensions/fill-toxicity.md) | `zoo-toxicity`: fill mechanisms, markouts and the PnL split of each model's error | 3.4, 3.10, 4.4, 5.6 |
| [forecast-referee](extensions/forecast-referee.md) | `queuetruth forecast`: forecast ledger, scoring and Kelly PnL | 3.10, 4.3, 4.4; a new section after 5.7 |
| [freqtrade-hummingbot](extensions/freqtrade-hummingbot.md) | Freqtrade and Hummingbot importers, reconciliation and the candle-fill audit | 5.7.1, 5.7.5, 3.10 (`qt.bot_reconcile`) |
| [halt-censoring](extensions/halt-censoring.md) | Ingest records halt spans; every engine censors shadows there (fix of F2) | 1.2, 5.2.7 |
| [hftbacktest-integration](extensions/hftbacktest-integration.md) | hftbacktest queue-model config and order-by-order reconciliation (repository tooling) | 5.3.5, 5.10 |
| [impact-flag](extensions/impact-flag.md) | Impact and queue-share warnings for independent shadows | 1.2, 3.4 (`qt.shadow_impact`), 3.10, new 5.6a |
| [livefill-kit](extensions/livefill-kit.md) | Own-account live-fill kit for Bitstamp (repository tool on the public API) | none |
| [model-perf](extensions/model-perf.md) | `model.npz` instead of pickle, fit threads and fit-hazard performance | 5.5.6, 5.5.8, 7.1 |
| [nautilus-plugin](extensions/nautilus-plugin.md) | `queuetruth-nautilus`, a separate package for NautilusTrader | new 5.10.1 |
| [nightly-checks](extensions/nightly-checks.md) | Local nightly cross-checks (maintenance tooling) | 7.1 (benchmark gating); no package change |
| [notebooks](extensions/notebooks.md) | `queuetruth.frames` and the example notebooks | 4.3 |
| [nyse-reader](extensions/nyse-reader.md) | NYSE TAQ Integrated Feed reader and the `nyse` rule set | 3.1, 3.2, 3.12 (new 3.12.3), 4.4, 6, 9 |
| [oracle-disagreements](extensions/oracle-disagreements.md) | Receipt-order and sweep rules D1 to D3 on Nasdaq ITCH, oracle rule O1 | 5.2.4a (folded in above), 6.2 O-SH-5, 6.8 |
| [pm-kit](extensions/pm-kit.md) | Prediction-market kit for Kalshi and Polymarket (`queuetruth pm`) | self-contained; reuses 3.3, 3.4, 3.8, 5.3.2 |
| [python-fuzz](extensions/python-fuzz.md) | Parser hardening contract and the Python fuzz suite | 3.3 (`dq` dataset contract), 3.8 and 5.7.1 (live-import inputs), 6 |
| [report-redesign](extensions/report-redesign.md) | Answer-first HTML report | 5.9 |
| [rust-fuzz](extensions/rust-fuzz.md) | Fuzz targets for the Rust parsers | 8.10 (folded in above) |
| [selection-stats](extensions/selection-stats.md) | Selection-aware statistics and the leaderboard model confidence set | 3.10 (`qt.zoo_summary`), 4.3, 5.6, 6; a new subsection of 5.7 (proposed as 5.7.6, merged as 5.7.7) |
| [service-zoo-mcp](extensions/service-zoo-mcp.md) | Zoo and calibration over HTTP, MCP run mode, optional MCP tool modules | 4.5, 4.6, 8.2, 8.8 |
| [trial-ledger](extensions/trial-ledger.md) | Trial ledger | 0.2, 4.3, 4.4, 7.2, 8.8 |

## Open conflicts (snapshot of 2026-10-19)

Places where the code still disagrees with this file, re-verified against the code on 2026-10-19; this list
records them without changing code. Paths relative to the repo root; line numbers refer to that read. Resolved
and removed (cumulative): E36(a)-(d); A3d, A4b, A6b (E37); book overflow -> QT-ENGINE-001; cross prints separate
from hidden prints; the sim-log marker (E38 OC-9: venue prefix); O-REF-1 (16 classes, mini-simulators); `make
compose-config --env-file`; shared `api_key_created.secret` nullable; dataset archive extraction in the job child;
`KEEP_PRIORITY_VENUES` (CME, Coinbase FIX); M21 IPCW diagnostics; M26 `markout_past_end`; `score_fills` 1.1
columns; `shadow_diagnostics` structured; feed gaps in zoo/calib/features (E59); every schema 1.1 item;
ScorecardSpec `self_exclude` honoured; missing `zoo_shadows.parquet` in `run_zoo` is an error; the bootstrap p
form, the C3 reading, M25 and the nullable / default deviations by ruling (E38). Resolved in this pass: the
leaderboard bias is the AJ figure (A2, 5.6); no silent `censored` fallback anywhere (E38 OC-7: library, report,
hazard card); the BSS bootstrap follows E5; `collect` drops the first-fill event's touch rows (C3); hbt #312 is a
per-pair abort with a `status` column and `hbt_312_aborts` (5.3.2; E8 superseded by ruling); the stored scorecard
headline leads with the cluster-robust p (A10); legs are instruction-sized (A8); replay/zoo CLI overrides enter
the spec and `run_id` (OC-34); markouts / orders / fills in the 1.1 layout (E52); one `entry_fallback_share`
format, none at share 0 (E17, E23); venue names lowercased at import and in the instrument map (E53);
`IngestSpec.strict` honoured by `qt.ingest` / `qt_ingest::ingest`; one `apply_verdict` (5.1.5); the committed
`service/openapi.json` matches the generated one (E58 gate); the scorer's 2x2 without
race-flagged orders (M15); the bounds bracket the exact engine order by order (5.3.3 h/i, O-ZOO-6); A1
per-shadow twin linking modelled by the independent oracle (6.2 O-SH-5); stale doc comments on fill reasons,
`fill_px`, zoo liquidity, `selfexclude.py` and `builder.py`; `auc_at` argument orders aligned. Resolved in the
residual pass (rev 2026-10-19; item numbers of the snapshot are kept and never reused): 3 (M22: `tost_p` from the
bootstrap replicates behind the 90% CI, fill rate and markouts, 5.7.5); 5 (qt_crypto derives the date from
`midnight_utc_ns` as qt_ingest does, 3.3); 7 (`ttf_iad` spans `[0, h_max]`, null without `h_max` rows, 5.6); 8
(tombstone ids `del_`, migration 0006; `use_`/`aud_` in the grammar, 4.5); 9 (`QT_JOB_TIMEOUTS_S` per kind, 8.1;
raised 4xx problems not stored, ratified with rationale, 8.6); 10 (`qt_explain_metric` stays remote-only, ratified;
local-mode instructions name only L tools, 4.6); 12 (`cli.py` `_calibration_refused` dead branch removed; the
`ModelSlot::hidden` doc comment in `crates/zoo/src/pair.rs` already reads "NT families in faithful mode, and
`front_of_queue` in both modes", matching 5.3.3 h).

**A. Code contradicts a binding ruling**

| # | Ruling | Conflict | Evidence |
|---|---|---|---|
| 1 | R-F7, 5.7.3 | the scorer leaves `session_date` out of an `exclude_order_ids` item when the twin's instrument-day is unknown, and a by-id twin hit at the order's own price is accepted without a time check (only an off-price hit must fall within `window_ns`) | `python/queuetruth/score/scorecard.py:203-207`, `python/queuetruth/score/selfexclude.py:203-214` |

**C. Contract text the code does not meet (no ruling yet)**

| # | Section | Conflict | Evidence |
|---|---|---|---|
| 6 | 4.1 | `qt_schema::json` mirrors only `qt.problem` and `qt.job` of the service documents (no api_key, api_key_created, page, health, meta); the module doc still says service documents have no mirror | `crates/schema/src/json.rs:6`, `:2085-2190` |

**D. Known limitations (documented; not fixed in v0.1)**

The code keeps these behaviours; the contract text stays the target and these rows say what a user sees instead.

| # | Ruling | Limitation | Evidence |
|---|---|---|---|
| 2 | M16, 5.2.10 | mirror sampling excludes `replaced` (and `cleared`) lives instead of cancelling the mirror at the removal | `crates/replay/src/sample.rs:12-13`, `:264` |
| 4 | M23, 5.3.3 | corrected hbt/DB families do not ignore `OUT_OF_VIEW` rows or defer `F` for out-of-view arrivals (only `nt_queue_l2` does) | `crates/zoo/src/models.rs:199`, `:233`, `:435` |
| 11 | 5.2 (E57 open) | m21: shadows on a date without a partition use the previous day's instrument row; an amend to zero has no planned end in `qt.zoo_shadows` | `crates/replay/src/util.rs:60-63`, `crates/zoo/src/runner.rs:80-110`, `:421` |

Effect on results: (2) mirror samples hold only lives that did not end `replaced` or `cleared`, so mirror fill rates
are conditional on that; (4) for corrected hbt/DB families an `OUT_OF_VIEW` row reads as the level emptying, which
can move the shadow's queue estimate forward (earlier model fills) when its level scrolls out of the top-N view, and
an out-of-view arrival starts from the visible quantity (`nt_queue_l2` corrected is not affected); (11) a shadow
dated on a day without a partition is priced with the previous day's instrument row, and an amend-to-zero shadow has
no planned cancel, so zoo evaluation counts it as never-cancel.
