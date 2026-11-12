# The next phase: from engine to product (0.4 to 1.0)

This page is the plan for queuetruth after 0.3.0. [ROADMAP.md](../../ROADMAP.md) is its short form. It rests on four
audits made on 2026-11-12 at commit f5715d7: a new trader's first hour with the CLI and Python API, the
architecture, the docs and integrations, and what traders and framework users say they need (public sources). Their
evidence is summarised here; where a number comes from one of them, the section says so.

## 1. Where queuetruth stands

**What is strong.**

- The truth engine is exact and fast. Over 16.7M records of two public Nasdaq days: 0 book invariant violations, 0
  trade-parity mismatches, an independent matching oracle agreeing on every shadow order. 3,000 orders times 16
  models score in 8 seconds on a laptop.
- The public Fill-Realism Atlas works end to end on GitHub: 22 IEX DEEP+ days are kept, the IEX split runs in Rust
  (the whole 2026-10-09 day in 5.6 minutes) and the 20-day site builds on a hosted runner in 22 minutes. Its
  20-day headline: within 1 minute the exact queue fills 11.97% of small resting orders; NautilusTrader's touch fill
  model fills 1.50 pp more (13% more orders), hftbacktest's L3 FIFO model 1.82 pp fewer, NautilusTrader's
  queue_position model is closest (+0.06 pp).
- Some outputs already delight: the demo's one-sentence answer in 23 seconds, `score`'s `RESULT:` and `P&L:` lines,
  `reconcile`'s per-order reasons, the fail-safe export, stable error codes, byte-exact certificate reruns.

**What stands in the way.**

- **It cannot be reached.** PyPI has 0.2.0, while the README and the how-tos describe 0.3.0 (reconcile, LEAN,
  Backtrader, VectorBT fail on the released wheel). Neither plugin is on PyPI. GitHub Pages is off, so the atlas has
  no public URL, and no README or docs page links it.
- **It is hard to use.** There is no one command from "my backtest" to "how wrong is it"; the first hour did not get
  there. The CLI has 31 top-level commands, 62 leaf commands and 517 command-specific flags, in two parsers with two
  help styles, and the same flag names mean different things (`--format`, `--sim`, `--live`). Raw units leak out
  (fixed-point prices, ASCII side codes, nanosecond horizon keys). Every calibration a newcomer ran said "do not use".
- **One result is wrong.** On a short capture (under 5 bootstrap blocks), `zoo`, `report` and `zoo-toxicity` print
  zero-width intervals and mark every model significant, contradicting the demo on the same data.
- **It is hard to change.** Every contract (specs, documents, run ids, trust flags) is written in both Rust and Python
  and has drifted (Rust knows 11 live venues, Python 18). Formats, venues, zoo families and backtesters are closed
  lists edited in 5 to 14 places each; adding LEAN touched 33 files. About 32k lines of Python products re-implement
  engine semantics (4 markout implementations, 3 calibration engines, 3 order books), and about 32k lines of scripts
  act as a second codebase whose tests do not run on pull requests.
- **The docs are large and stale.** 83 navigation entries and about 145k words; three different "what is validated"
  tables; about 30 "v0.1" labels; the hftbacktest default export (`partial_fill_exchange`) aborts on the released
  hftbacktest; the NautilusTrader replay strategy fails on NautilusTrader 2.x.

**What the market says** (public sources, see the research notes in section 10).

- The gap is documented by the frameworks themselves: Freqtrade, LEAN and NautilusTrader fill a limit order when the
  price touches it by default; hftbacktest's docs tell users to compare with live trading and adjust.
- Maintainers decline to own it: NautilusTrader closed several queue-realism issues as not planned and said it does
  not need "a new execution realism layer". That leaves room for a neutral referee.
- Practitioners ask for exactly this tool: hftbacktest discussion #332 asks how to validate the queue penalty against
  live fills and proposes McNemar tests on discordant fills, markouts at 100 ms, 1 s and 10 s, time-to-fill survival
  curves, and fitting latency before the queue.
- The largest audience has bars, not ticks: Freqtrade (57k stars), Backtrader (24k), LEAN (22k) and VectorBT (10k)
  users fill on the candle's high or low. They cannot run an exact replay, but they can use what one measures.
- No independent head-to-head benchmark of fill models against the exact queue exists. A small project (mbo-queue)
  is converging on the idea, so time matters.

## 2. The goal

**queuetruth is the reality check every backtest runs before money does.** For any trader, in under a minute and
inside the tool they already use, it answers three questions in this order, each with an interval and a way to check
it:

1. **How wrong is my backtest?** Fills and P&L, in plain words.
2. **Why?** Which orders, which rule, which part of the queue.
3. **What fixes it?** A parameter or a fill model to paste, with a verdict on whether to trust it.

The engine stays Apache-2.0, runs locally, ships no market data and sends nothing anywhere.

### Who it is for, in order

| Who | What they have | What they need first |
|---|---|---|
| **Bar-based traders** (Freqtrade, Backtrader, VectorBT, LEAN, backtesting.py) | Candles and a backtest; often a live or dry-run bot | How many of their backtest's fills are real, without any order-level data; a realistic fill rule they can switch on |
| **Event-driven users** (NautilusTrader, hftbacktest) | Ticks or L2, sometimes L3 | The fitted parameter to paste, inside their engine, and a check against their live fills |
| **Bot operators and crypto market makers** | Live fills from an exchange | Their simulator scored order by order against live; a warning when live drifts from the model |
| **Simulator maintainers** | A matching engine | A regression check in CI and a badge |
| **Researchers and agent builders** | Strategies and agents | A look-ahead-free sandbox with exact fills and verifiable results |
| **Everyone curious** | A browser | The atlas: how far each popular fill model is from the real queue, rebuilt every trading day |

## 3. Principles every change follows

1. **Answer first.** The first line of every output is the answer in plain words with its interval; details follow and
   diagnostics stay behind `--verbose`.
2. **Zero data first.** The atlas and the tables built from it answer before the user brings any data. Bringing data
   makes the answer exact for the user's own orders.
3. **Meet users in their tool.** Every framework gets one line inside the user's own code (a fill model, an analyzer,
   venue options) or one command over its own output. No new workflow to learn.
4. **One way to do each thing.** One CLI parser, one config shape per operation, one registry per extension axis, one
   source for every contract. Generated artifacts are checked in CI.
5. **A small core.** The core wheel holds the truth engine, evaluation and the interfaces. Products (prediction
   markets, forecasts, sandbox) are their own packages; heavy dependencies are extras.
6. **Never claim more than is validated.** Validation status is data, generated into one page, and every claim links
   to its check.
7. **Human units at every boundary.** Symbols, decimal prices, venue-local times and durations such as `1s` in every
   output, API result and flag; raw integers stay inside.

## 4. The product, simplified

### 4.1 Six verbs

The everyday CLI becomes six verbs. Every current command stays available under `queuetruth advanced` (or in its own
package) with deprecation warnings for two minor releases, so nothing breaks.

| Verb | What it does | Replaces |
|---|---|---|
| `queuetruth demo [--backtester X]` | One minute on a public day, answered for your library | `demo` (now NautilusTrader only) |
| `queuetruth atlas [--library X] [--symbol S] [--tier T]` | The atlas answer for your library and symbols, with the config to paste; works offline from a cached copy | reading the site by hand |
| `queuetruth check BACKTEST [--data D]` | How wrong your backtest is. Without data: from the atlas (how many fills were touch-only and how many such fills the real queue gives). With order-level data: your backtest's own orders replayed exactly, per order and in P&L | `shadows sample` + `zoo` + `zoo-toxicity` + `report`; `live import --venue sim-*` + `score` |
| `queuetruth fit --backtester X (--data D \| --live L)` | The parameter that fixes it, with its verdict; the snippet is written only when the fit passes | `calibrate alloc/nt-touch/survival/latency`, `calibrate-live`, `export hftbacktest/nautilus` |
| `queuetruth compare --backtest B --live L [--data D]` | Your backtest against your live run, order by order: pairs, reasons, scorecard, latency | `reconcile`, `live import`, `score`, `shadows from-live` |
| `queuetruth data add FILES` / `data fetch iex` | Ingest with format, venue and date inferred, quality checked, days merged; free IEX days fetched with attribution | `ingest`, `dq`, `scripts/realdata/merge_datasets.py`, `scripts/iex/stream_subset.py` |

Two helpers stay at the top level: `explain RUN ORDER` (now `why`) and `report` / `verify` (reports, certificates,
atlas numbers). `queuetruth --help` lists these and nothing else.

### 4.2 The Python API

The same verbs as functions with typed keyword arguments and human units: `qt.check("backtest.zip", data="ds")`,
`qt.fit(backtester="nautilus", data="ds")`, `qt.compare(backtest=..., live=...)`, `qt.atlas.lookup(library=...,
symbol="AAPL")`. Every result has `.summary` (the answer line), `.table()` (a pandas DataFrame with symbols, decimals
and venue-local times), `.to_html()` and a notebook view. Horizons are keyed by `"1s"`, not nanosecond strings. Spec
dicts stay accepted for scripts and services.

### 4.3 One line inside each framework

| Framework | The one line | How it is fed |
|---|---|---|
| NautilusTrader | `engine.add_venue(..., **qtn.venue_options(qt.fit(...)))` or `qtn.venue_options(qt.atlas.calibration("nautilus", tier="T1"))` | A fit on your data, or the atlas when you have none |
| hftbacktest | `asset = qthbt.configure(asset, calibration)` | Same |
| Backtrader | `cerebro.broker.set_filler(qt.backtrader.QueueFiller(tier="T1"))` and `cerebro.addanalyzer(qt.backtrader.OrderLog)` | The atlas touch tables (4.4) |
| LEAN | `security.set_fill_model(qt.lean.QueueFillModel(tier="T1"))` | Same |
| Freqtrade, VectorBT, backtesting.py | `queuetruth check <their result file>` | Same; the result is a realistic fill count and P&L range |
| Any simulator | `uses: sarptandoven/queuetruth/actions/referee@v1` | The referee fixtures |

Each framework lives in one package under `integrations/<name>/` behind a `Backtester` plugin interface (section 5), is
published with every release and is tested in CI against the framework's real released versions.

### 4.4 The atlas, version 2

The atlas becomes the front door and a data source, not only a page.

- **Chart first, plain words.** One chart of fill-rate bias by library model with intervals, one sentence per library,
  and a "what to do" box with the config to paste. Model ids and terms such as "faithful mode" move into tooltips and
  a "how it is made" page. The current front page is about 15,000 pixels of text.
- **Your symbol.** A lookup by symbol (static JSON shards), so a trader sees their own names.
- **Touch tables for bar-based backtests.** From the same exact replay: the probability that a resting limit order
  really fills given that the price touched it within a bar (1m, 5m, 1h), or went through it by k ticks, by
  liquidity tier and side; and the average markout of the fills that do happen. This is the answer for the largest
  audience, and it feeds the Backtrader and LEAN fill models and `check`.
- **A data API.** Versioned static files next to the site (`atlas/v1/summary.json`, `configs.json`,
  `touch.json`, `symbols/<prefix>.json`) used by `queuetruth atlas`, `check` and the plugins, with the atlas root to
  verify against.
- **Honest scope.** IEX carries about 4 to 5% of US volume and has a speed bump and repricing order types, so its
  queues may not transfer to other exchanges; the page says so and adds venues as their terms allow (Nasdaq sample
  days, crypto order-level venues). A "last built" date and the verify command are on every page.
- **Verify without a checkout.** `queuetruth verify atlas --day D [--unit U]` checks the published roots offline in
  seconds and can re-run one unit.

### 4.5 Docs: three doors

The docs site moves to the same Pages domain as the atlas, with about 35 entries instead of 83 (the docs audit has the
page-by-page moves): **See it in a minute** (demo), **Read the atlas**, **Check my backtester** (one page per
framework). Tutorials use free IEX data. One generated "What is validated" table replaces the three that disagree.
Every runnable block is tested against the released wheel, not only the checkout. Design notes and launch drafts leave
the user navigation.

## 5. A modular core

### 5.1 Target structure

```
crates/
  types/      units, civil dates, durations, parallel map, the error-code table
  schema/     Arrow tables; specs and documents generated from schemas/json; run_id, canonical JSON,
              dataset_id (the only implementation)
  dataset/    dataset, manifest, writer, data-quality counters
  formats/    SourceFormat trait and registry: itch, dbn, iex, crypto; nyse and etcsv behind features
  book/ replay/ l2/ zoo/ (FamilyDef registry) calib/ (with trust, the only implementation) features/
  ops/        the operation table: name -> fn(spec JSON) -> summary JSON
  export/     hftbacktest npz and zip (from l2)
  acquire/    streaming split and subset of public days (feature; from ingest and scripts)
  py/         PyO3 over ops, Session, the registries and the integrity functions
  cli/        queuetruth-core: `run <op> --spec`, JSON in and out
python/queuetruth/
  errors (Code enum)  integrity/  dataset/  contracts/  stats/  engine/  models/  calibrate/
  live/ (importer registry)  reconcile/  backtesters/ (plugin API)  evaluate/  report/  dq/
  ops/ (one module per operation; ledger and certificates as hooks)  runner/
  interfaces/ cli/ (one parser, generated from ops) mcp/ (one server) client/
packages/       queuetruth-pm  queuetruth-forecast  queuetruth-sandbox
integrations/   hftbacktest  nautilus  backtrader  lean  freqtrade  vectorbt  (one package each)
apps/           atlas (scripts/scale, site, workflows)  nightly  repro
testing/        oracle  hbt_crosscheck  reference
service/        uses runner/ and ops
```

Layers import only downward: contracts, data adapters, truth engine, models, evaluation, operations, interfaces,
products. An import-linter contract enforces this in CI, starting with today's violations listed as exceptions and
removing them one by one.

### 5.2 One file per extension

| To add | Today | After |
|---|---|---|
| A data format | 10 to 12 files | One Rust module implementing `SourceFormat` and one registry line; CLI choices, service lists and JSON Schema enums are generated |
| A live-log venue | 6 to 8 files (the Rust enum was forgotten 7 times) | One Python class registered as an `Importer` with a typed options model; third parties use the `queuetruth.importers` entry point |
| A queue model | Up to 14 files | One `FamilyDef` in its own file; every consumer (eval, service, calibrate, export, atlas, demo, help) reads `_core.zoo_families()` |
| A backtester | 33 files for LEAN | One package implementing `Backtester` (sim importer, replay adapter, snippet, fill model, reconciler) via the `queuetruth.backtesters` entry point; no core edits |

### 5.3 The migration, in an order that keeps everything green

| Step | What | Size |
|---|---|---|
| 0 | Guardrails: import-linter with recorded exceptions; CI runs the integration, scripts and plugin suites with real hftbacktest and NautilusTrader; drift tests for every hand-kept list; fix the live-venue and service-format drift | Small |
| 1 | Leaf deduplication: one integrity module (canonical JSON, hashes, hash chain, Merkle), one home for interval estimators, one duration parser; Rust units and dates in `types` | Medium |
| 2 | Registries: zoo families, source formats, importers, backtesters; `make generate` covers every generated artifact | Medium |
| 3 | A dataset layer used by the 15 readers and 5 writers | Medium |
| 4 | Single-source contracts: Rust specs generated from the JSON Schemas; run ids, seeds, trust and bootstrap only in Rust | Large |
| 5 | Operations and one CLI: `api.py` split into `ops/`; one parser generated from the specs; the six verbs; `queuetruth-core run <op>` | Large |
| 6 | Engine consolidation: prediction-market queue models in the Rust zoo, the sandbox on `_core.Session`, one markouts function, one schedule builder, live calibration on the Rust likelihood | Medium |
| 7 | Packaging: pm, forecast and sandbox as packages, scikit-learn as an extra, integrations behind the plugin API, apps and testing packages, archived private-prototype scripts | Medium |
| 8 | One runner for the service and MCP; one MCP server whose tools come from the operation table | Medium |

Steps 0 to 3 remove most of the drift risk at low cost; 4 and 5 bring the largest simplification; 6 and 7 move about
25 to 30k lines out of the core wheel.

## 6. Proof

- **Fix the short-capture intervals first.** With fewer than 5 bootstrap blocks, fall back to the demo's
  instrument-by-minute clusters or print "interval not available" and no significance marks.
- **The L3 FIFO gap.** Explain why hftbacktest's L3 FIFO model, which reads order-level data, fills 1.82 pp fewer
  orders than the exact queue on IEX, before the atlas launch says so: either a real behaviour of hftbacktest (an
  upstream report) or a difference in the replica (a fix).
- **Real data for every claim.** In order: IEX (22 days, to document), one crypto order-level venue (Coinbase through
  a free Tardis day, Bitfinex raw books), CME through Databento MBO (a local recipe; aggregates are published only if
  the licence allows), one real own-account scorecard (the Bitstamp kit), and real user exports for each importer.
- **The statistics practitioners ask for**, in `compare`: McNemar on discordant fills, markouts at 100 ms, 1 s and
  10 s, time-to-fill survival curves, and latency fitted before the queue.
- **Released-wheel checks.** CI runs the README, getting-started and every how-to against the released wheel, and the
  integrations against the frameworks' released versions.

## 7. Reach

- **Release cadence.** 0.3.0 now, then a minor release at each milestone, every two to three weeks; plugins publish
  with the core.
- **Launch.** When the atlas is public and v2 is out: one headline number with its interval, posted where traders are
  (r/algotrading, Hacker News, the QuantConnect forum, NexusFi, the hftbacktest and NautilusTrader communities), a
  Colab notebook per framework, an awesome-quant entry. Every post links the atlas and `queuetruth check`.
- **Upstream.** Submit the prepared hftbacktest #312 fix and the NautilusTrader documentation change; offer the referee
  Action to simulator maintainers as a free regression service.
- **Sustainability, later.** The engine stays free. Hosted calibration on a customer's own licensed data, drift alerts
  and certified reports are the paid layer, without paywalling anything in the core.

## 8. Milestones

Each milestone ends with a release and has exit criteria that can be checked.

| Milestone | Contents | Exit criteria |
|---|---|---|
| **M0 Unblock** (days) | Pages on and 0.3.0 released (maintainer actions); the 2026-11-05 day kept; short-capture intervals fixed; README and docs truth sweep (atlas linked, status tables, stale labels); hftbacktest export defaults to the exchange that runs on the released hftbacktest; NautilusTrader 2.x replay strategy fixed; integration suites in CI | The atlas has a public URL linked from README; `pip install queuetruth` gives every documented command; no zero-width interval on a 15-minute capture; CI runs the plugin suites against real engines |
| **M1 Answer in a minute** (0.4.0) | Atlas v2 with the data API and touch tables; `queuetruth atlas`; `check` in both modes; human units in outputs; docs with three doors on Pages; the L3 FIFO gap explained | A trader with no data gets a sourced answer for their library in under 30 seconds; a Freqtrade or Backtrader user gets a realistic fill count from one command |
| **M2 Fix it in your tool** (0.5.0) | The `Backtester` plugin API; NautilusTrader and hftbacktest plugins on PyPI; Backtrader filler and analyzer; LEAN fill model; `fit` and `compare` | Each framework in 4.3 has a one-line path that runs in CI against the framework's real releases |
| **M3 One way to do everything** (0.6.0) | Migration steps 0 to 5: registries, dataset layer, single-source contracts, operations, one CLI with six verbs, the typed Python API | `queuetruth --help` lists at most 8 commands; one parser; adding a backtester is one package and adding a format one file and one line; no hand-kept lists without a drift test |
| **M4 Proof everywhere** (0.7.0) | Crypto order-level and CME validation, the own-account scorecard, the generated validation page, practitioner statistics in `compare`, a drift-alert prototype (`compare --watch`) | At least four venues validated on real data; one real account scored and published |
| **M5 1.0** | Migration steps 6 to 8; products as packages; stability tiers on every public name; launch campaign | API frozen with stability tiers; core wheel without the product packages; launch posts out |

## 9. How progress is measured

queuetruth has no telemetry and keeps it that way; these come from public counters and from the repository itself.

| Measure | Now | Target |
|---|---|---|
| Time to a sourced answer with no data | not possible without reading the site by hand | under 30 seconds (`queuetruth atlas`, `check`) |
| Commands to score my backtest | about 7 (Binance path), not reached in the first hour | 1 |
| Top-level CLI commands / parsers | 31 / 2 | at most 8 / 1 |
| Files to add a backtester / format | 33 / 10 to 12 | 1 package / 1 file and 1 line |
| Docs navigation entries | 83 | about 35, every runnable block tested on the released wheel |
| Venues validated on real data | Nasdaq ITCH, IEX DEEP+ | plus one crypto order-level venue and CME |
| Real accounts scored | 0 | at least 1, published |
| Public signals | PyPI 0.2.0; plugins unpublished | PyPI downloads, plugin installs, referee Action users, upstream changes merged, atlas links |

## 10. Research notes

The four audits behind this plan were run against commit f5715d7 on 2026-11-12:

- **First hour as a trader** (README, getting-started and every "pick your path" row run literally): 25 ranked issues,
  the interface map (31 top-level commands, 62 leaf commands, 517 command-specific flags), timings.
- **Architecture** (every crate, subpackage, script directory and integration): the layering, 15 structural problems
  with file and line evidence, the extension-point counts, the target structure and the migration in 5.3.
- **Docs and integrations** (every page and every integration, with the plugin suites run on real hftbacktest 2.4.4,
  NautilusTrader 1.231.0 and 2.0.0rc9): a status table of every page, 20 ranked issues, the three-door structure in
  4.5, the integration matrix.
- **Market and positioning** (public sources only): framework sizes, the documented touch-fill defaults, hftbacktest
  discussion #332, NautilusTrader issues #4631, #5488 and #5554, the competitive landscape, free order-level data and
  its licences, adoption patterns.
