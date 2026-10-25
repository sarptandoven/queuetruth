# queuetruth

[![PyPI](https://img.shields.io/pypi/v/queuetruth.svg)](https://pypi.org/project/queuetruth/)
[![Python](https://img.shields.io/pypi/pyversions/queuetruth.svg)](https://pypi.org/project/queuetruth/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/sarptandoven/queuetruth/blob/main/LICENSE)

**Your backtest fills orders your live strategy never gets. queuetruth measures by how much, and why.**

queuetruth is an open referee for fill simulation. It replays order-level market data through an exact FIFO book
and places your orders in the real queue. Against that exact answer it scores the queue models backtesters use
(hftbacktest, NautilusTrader, Databento) and fits their parameters; against your live fills it scores your own
simulator, order by order.

<!-- Release step: pin this image URL to the release tag (.../queuetruth/vX.Y.Z/docs/assets/...) so the PyPI page and its chart cannot disagree. -->
[![Fill-rate bias of each queue-model configuration against the exact replay at 1 s on two public Nasdaq days, with day-block bootstrap 95% intervals, drawn from the faithful-mode table in docs/RESULTS.md section 4, every model scored on the same orders.](https://raw.githubusercontent.com/sarptandoven/queuetruth/main/docs/assets/fill-bias-1s.svg)](https://github.com/sarptandoven/queuetruth/blob/main/docs/RESULTS.md#4-how-far-are-l2-queue-models-from-exact-truth-rd-8-extended-the-headline-experiment)

The chart is the two-day leaderboard at 1 s, every model scored on the same orders: 2,529 and 2,518 of the 3,000 a
day, leaving out the orders on which a faithful hftbacktest model hit its bug #312. What it shows is
summarised [below](https://github.com/sarptandoven/queuetruth#what-it-found-on-real-data); every table is in
[Results](https://github.com/sarptandoven/queuetruth/blob/main/docs/RESULTS.md).

## Quickstart

```sh
pip install queuetruth
queuetruth fixtures synth --out synth
queuetruth shadows sample --data synth --n 1000 --out shadows.parquet
queuetruth zoo --data synth --shadows shadows.parquet --out zoo
```

The last line prints a leaderboard of 16 models against the exact replay of a synthetic dataset generated on your
machine (no download). It shows the workflow, not a result about markets. To get the chart above on your own data,
bring a day of order-level data:

## Bring your data

| You have | Read it with | You get | Checked on |
|---|---|---|---|
| Nasdaq TotalView-ITCH 5.0 day files (free sample days from Nasdaq) | `queuetruth ingest --format itch --venue nasdaq` | exact replay, shadow orders, the zoo, calibration | 2 public days from the raw bytes, 40 symbols |
| IEX DEEP+ pcaps (free; redistributable with IEX's attribution) | `ingest --format iex-deep-plus --venue iex` ([guide](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/iex-public-demo.md)) | the same | 1 public day, 4 symbols |
| Databento DBN MBO files (CME, Nasdaq) | `ingest --format dbn --venue cme` or `--venue nasdaq` | the same | Databento's test files decode; CME rules synthetic only |
| Order-level crypto captures you record: Coinbase `level3`, Kraken L3, a Hyperliquid node, Bitstamp, Bitfinex | `ingest --format coinbase-level3`, `kraken-l3`, `hyperliquid-node`, `bitstamp` or `bitfinex`, with `--venue generic` ([guide](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/crypto-l3-sources.md)) | exact replay where the capture is complete | one capture per venue for Bitstamp and Bitfinex (counters); fixtures for the rest |
| An L2-only tape (Tardis `incremental_book_L2` and trades) | `ingest --format tardis-l2 --venue generic` ([guide](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/calibrate-live-l2-venue.md)) | the queue models, markouts and a parameter fitted to your fills; never exact truth | fixtures |
| Your live orders and fills: Binance, Bybit, OKX, Coinbase, Kraken, Deribit, Hyperliquid, IBKR Flex, FIX 4.4 drop copies and QuickFIX logs | `queuetruth live import --venue <venue>` ([guide](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/import-live-logs.md)) | the sim-vs-live scorecard, `calibrate-live`, a latency fit | fixtures from public format docs |
| Your backtest's output: hftbacktest or NautilusTrader | `live import --venue sim-hftbacktest` or `sim-nautilus`, then `score --sim-log` ([guide](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/score-nautilus-vs-binance.md)) | your simulator scored order by order against your live fills | fixtures |

queuetruth downloads nothing for you and makes no network calls when run locally. You need a licence for the data
you process: [Data and licensing](https://github.com/sarptandoven/queuetruth/blob/main/docs/data-and-licensing.md).
Every term used here is defined in the
[Glossary](https://github.com/sarptandoven/queuetruth/blob/main/docs/concepts/glossary.md).

## What it found on real data

On 3,000 small orders at or near the touch of AAPL, MSFT, SPY and TSLA on each of two public Nasdaq TotalView-ITCH
days (2019-12-30 and 2020-01-30), every order evaluated against the exact replay of the real queue:

- **NautilusTrader's touch fill model, with the default `prob_fill_on_limit = 1`, overfills.** Verified in the real
  engine (nautilus_trader 1.231.0, `BacktestEngine` fed the L2 view as `OrderBookDelta`s,
  `FillModel(prob_fill_on_limit=1)`): it fills 14.17% of the 3,000 orders within 1 s on 2019-12-30 where the exact
  replay fills 11.03%, +3.13 percentage points (95% CI [+2.53, +3.77]); +2.80 pp on 2020-01-30 (+3.08 and +2.50 pp on
  the orders the chart scores). The 2.0 release candidate (2.0.0rc7, `DefaultFillModel(prob_fill_on_limit=1)`) fills
  exactly the same orders. queuetruth's faithful NautilusTrader models agree with 1.231.0 on 100% of orders at 1 s, 10
  s and 60 s on both days and on the synthetic fixture; on 2.0.0rc7 the touch model still agrees on 100%, the queue
  model on 99.60% to 100% of orders per day and horizon (2.0 also caps the queue ahead at a level's new size when the
  level shrinks). Nautilus's own queue model (`queue_position=True`) is close to exact: within 0.8 pp on 1.231.0 and
  0.4 pp on 2.0.0rc7 on an L2 delta feed, and fed the MBO book (L3), 2.0.0rc7's matches the exact replay on every
  order on day one, and on day two on all but 1 order at 0.1 s and 2 at 10 s. On 1.231.0 an `OrderBookDepth10` feed
  resets the queue position at every snapshot, so the queue model then fills like the touch model (+0.9 to +3.3 pp
  above exact at 1 s to 60 s); the 2.0 release candidates (from rc5) keep it.
- **Calibration fixes it on the held-out Nasdaq day.** `p` fitted on day one is 0.29 (bootstrap CI [0.27, 0.30]); on
  day two it cuts the 1 s overfill from +2.8 pp to +0.4 pp and more than halves the 1 s Brier score. `p` is per
  print, so it is venue- and feed-specific: refit it for yours.
- **The hftbacktest power and log models are close on these names**: within about 1 pp of exact at 1 s, 10 s and
  60 s on both days, mostly slightly pessimistic, with every model scored on the same orders.
  `hbt_risk_adverse` is pessimistic by up to 1.3 pp. Refitting power3's `n` and Databento's `k` gave values that score
  about like the defaults on day two, and the export refuses the fitted `n` because it does not beat `n = 2` at every
  horizon.

So L2 optimism is model-specific and, for hftbacktest on very liquid Nasdaq names, modest. These are two days and
four of the most liquid US stocks, where most small orders at the touch fill within a minute.

**Breadth: 36 more Nasdaq names in five liquidity tiers** (mega caps, deep-queue low-priced large caps, ETFs, mid
caps, small caps; same two days, raw ITCH bytes, every exactness check clean). The NautilusTrader default overfills
in every tier and most on the deep-queue names (BAC, F, GE, SIRI, T, PFE): +8.60 pp at 60 s on day one, where the
exact replay fills 17.0%. hftbacktest's `power3[n=2]` stays within 1.7 pp and `nt_queue_l2` within 1.5 pp of exact at
60 s in every tier. Order size barely moves first-fill bias. This is still two days of one venue, scored on the
first fill only, and a queuetruth 0.1.0 run: each hftbacktest model there leaves out only its own #312 aborts.

All rows (every model configuration and the 2 bounds), both modes, by horizon, queue decile and liquidity, the breadth
tables, and every artifact named:
[docs/RESULTS.md](https://github.com/sarptandoven/queuetruth/blob/main/docs/RESULTS.md).

## Why the answer is exact

- **Raw public bytes, our own reader.** The leaderboard, the breadth run and the calibration above are computed from the
  raw Nasdaq ITCH 5.0 files with queuetruth's own ITCH reader. On the same build the leaderboard and the calibration are
  identical, cell for cell, to the same runs on the earlier private prototype's CSV conversion of those files. The book
  checks and the mirror-shadow checks below were also rerun from the raw bytes and match the prototype's outputs
  exactly.
- **The book is right.** 16,754,562 records with 0 invariant violations, 0 trade-parity mismatches over 633,287 printed
  executions, 0 price-priority violations; 98.7% and 99.1% of real executions hit the head of their queue in our book
  after grouping sweeps.
- **The shadow rules are right.** Under the prototype's fill rule (`--fill-rule et`), 3,000 of 3,000 mirror shadows per
  day reproduce the prototype's fills and queue traces. An independent matching-engine oracle agrees with the default
  `exact` fill rule on 3,000 of 3,000 shadows on both days (raw ITCH bytes, queuetruth's own mirror sampler, about
  half of them filling) and on 2,997 and 3,000 of 3,000 of the prototype's mirror shadows on its CSV conversion (the
  3: the oracle's 1 s cross window on a CSV without cross flags), with equal total filled quantity in every run.
- **The scorer is right.** 3,000 real orders per day used as a "live" log are reproduced exactly by the exact arm: 250
  filled in both, 2,750 in neither, 0 discordant on 2019-12-30; 195 / 2,805 / 0 on 2020-01-30; the same filled quantity
  and first-fill time on every order.
- **Reproduce it**: `make reproduce` (`scripts/repro/reproduce.py`) streams the two public day files from Nasdaq, keeps
  the four symbols (the raw files are never stored) and reruns ingest, replay, the leaderboard, calibration, the learned
  model and the oracle from those bytes alone, without the prototype's artifacts (1,387 s at 4 threads once the subsets
  are on disk). `make reproduce-breadth` (`scripts/repro/breadth.py`) reruns the 36-symbol breadth study. Rerun on the
  0.1.0 commit (`4be5240`, 1,162 s), it reproduced the leaderboard on both days, all four calibrations, the learned
  model and the oracle identically. The leaderboard above is the later rerun under 0.1.1's common population, which
  gives the same cells from the raw bytes and from the CSV conversion. The checks that compare with the prototype's
  outputs need its artifacts, which are not public. Commands and provenance:
  [docs/RESULTS.md](https://github.com/sarptandoven/queuetruth/blob/main/docs/RESULTS.md).

Also in 0.1.0: order-level ingest of IEX DEEP+ (free public data, redistributable with IEX's attribution;
[guide](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/iex-public-demo.md)), Coinbase Exchange
`level3`, Kraken L3 and Hyperliquid node captures
([guide](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/crypto-l3-sources.md)), and `queuetruth dq`, a
data-quality report for any dataset
([guide](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/check-a-feed.md)).

How every claim is tested, including what is not: [How queuetruth is
tested](https://github.com/sarptandoven/queuetruth/blob/main/docs/testing.md).

## Install

```sh
pip install queuetruth                 # CLI + Python API (abi3 wheels: macOS arm64/x86_64, Linux x86_64/aarch64)
pip install 'queuetruth[client]'       # + HTTP client for a self-hosted service
pip install 'queuetruth[mcp]'          # + MCP server for agents
```

CPython 3.11 or later. Other platforms build from source with Rust 1.85 or later (see
[Development](https://github.com/sarptandoven/queuetruth#development)). `queuetruth-core`, a standalone binary for
the native commands, is attached to each GitHub release.

After the [quickstart](https://github.com/sarptandoven/queuetruth#quickstart), `queuetruth report --in zoo --out
report.html` writes the same leaderboard as one self-contained HTML page. The synthetic order flow is not built to
make queue position matter, so its leaderboard says nothing about real markets; to get the Nasdaq table on your own
data, ingest a day you are licensed to use and run the same commands ([Getting
started](https://github.com/sarptandoven/queuetruth/blob/main/docs/getting-started.md)).

From Python:

```python
import queuetruth as qt

z = qt.run_zoo({"dataset": {"path": "synth"}, "shadows": {"path": "shadows.parquet"}, "out": "zoo"})
for row in z.leaderboard:  # fill-rate bias vs exact at 60 s, with its 95% CI
    b = row.fill_rate_bias["60000000000"]
    print(f"{row.model_id:50s} {100 * b.value:+.2f} pp [{100 * b.ci_lo:+.2f}, {100 * b.ci_hi:+.2f}]")
```

## What it does

- **Exact replay** of Nasdaq TotalView-ITCH 5.0, Databento DBN MBO and crypto L3 captures, with invariant
  counters, a queue-rank oracle and parity checks against the source's prints.
- **Shadow orders**: "if I had joined the bid at 10:31:05.120 for 200 shares, when would I have filled?" Per-fill
  timeline, queue trajectory, latency, cancels and amends; `queuetruth why` explains one order event by event.
- **Queue-model zoo**: 14 queue-model configurations from hftbacktest, NautilusTrader and Databento (one of them,
  `hbt_l3_fifo`, reads MBO rather than L2) plus 2 bounds, in `faithful` mode (each library's behaviour as
  implemented: bit for bit for hftbacktest; re-implemented for NautilusTrader and checked against nautilus_trader
  1.231.0 order by order; re-implemented from its tutorial for Databento) and
  `corrected` mode, scored against exact truth by horizon, queue position and liquidity.
- **Calibration and export**: `n`, `k` and `p` fitted on one window and scored on another. The export is fail-safe:
  it refuses a parameter that is at a grid edge, not identified, or does not beat the library default out of
  sample (`QT-CALIB-001`), unless you force it under a NOT VALIDATED header.
- **Sim-vs-live scorer**: your live order log (importers for Binance, Bybit, OKX, Coinbase, Kraken, Deribit,
  Hyperliquid, IBKR Flex, FIX drop copies and QuickFIX engine logs, and for hftbacktest and NautilusTrader output) replayed as a fixed schedule through your simulator, the exact engine or any zoo model,
  with your own orders removed from the tape, a per-order 2x2 table, cluster-robust tests, time to fill, markouts
  and CI gates.
- **Referee**: an exact-FIFO regression oracle for other simulators, with 16 bug-class fixtures.
- **Interfaces**: Python API, CLI, self-contained HTML reports, a self-hostable HTTP service and an MCP server, all
  sharing published JSON Schemas.

## What it is not

- Not a backtester, OMS or trading system: no strategy code, no order routing.
- No market data included, and nothing downloaded on its own; no telemetry, no network calls when run locally.
- Exact truth covers displayed liquidity on FIFO venues. It does not model your own order's impact on others,
  hidden or iceberg liquidity, auctions or pro-rata matching. L2-only data supports the models, markouts and
  calibration against your own fills, never exact truth.
- Validated on real data for Nasdaq only. DBN/CME fidelity is synthetic only; crypto L3 readers are checked on one
  capture per venue (counters, not fill fidelity); live-log importers are tested on fixtures written from public
  format documentation, not on real account exports.

## Status

| Component | Evidence | Status |
|---|---|---|
| Exact book and replay, Nasdaq | 2 public days from the raw ITCH bytes: invariants, trade parity, rank oracle, BBO and rank traces identical to the earlier prototype's | validated |
| Shadow orders | 2 x 3,000 mirror shadows identical to the prototype's (its fill rule); an independent oracle agrees on 3,000 of 3,000 on both days (raw ITCH bytes) and on 2,997 and 3,000 of 3,000 (prototype's CSV and shadows; the 3: the oracle's 1 s cross window on a CSV without cross flags) | validated |
| L2 queue-model zoo | 16 rows x 2 modes on 2 x 3,000 shadows (faithful mode scores the 2,529 and 2,518 on which no hftbacktest model aborts), plus 36 more symbols in 5 tiers; the bracket `front_of_queue` >= exact >= `trade_through_only` holds order by order by construction, tested on scenario and synthetic tapes and with 0 per-order violations on real day one; `hbt_l3_fifo` corrected equals exact | real-data leaderboard published |
| NautilusTrader models | faithful `nt_prob_touch` and `nt_queue_l2` agree with nautilus_trader 1.231.0's `BacktestEngine` (delta-fed L2) on 100% of orders at 1 s, 10 s and 60 s, on both days and the synthetic fixture (`scripts/nt_crosscheck`); on 2.0.0rc7 `nt_prob_touch` agrees on 100% and `nt_queue_l2` on 99.60% to 100% of orders per day and horizon (every difference is 2.0's queue cap on a shrinking level) | checked against the real engine (1.231.0 and 2.0.0rc7) |
| Calibration and export | fitted on day one, scored on day two; NautilusTrader `p` improves day two, power and Databento do not beat their defaults | runs on real data |
| Learned L2 fill model | Brier skill over climatology on the held-out day, lower 95% bound above 0 at 1 s and 10 s | validated |
| Scorer | 3,000 real orders per day reproduced exactly by the exact arm on both days | validated |
| Databento DBN / CME | Databento's DBN test files decode in every version; no real CME session | decoding only |
| Exact book and replay, IEX DEEP+ | 1 public day (2026-10-09; SPY, AAPL, NVDA, TSLA) from the raw pcap: verdict clean, invariants 0, all 21,301 displayed executions at FIFO rank 0 | validated on one day |
| Crypto L3 (Bitstamp, Bitfinex) | one 10-minute capture per venue, record counts equal to the prototype's converter | counters only |
| Crypto L3: Coinbase `level3`, Kraken L3, Hyperliquid node | synthetic fixtures and property tests; no real capture has been run | fixture only |
| Live-log and QuickFIX importers | fixtures from public format docs and the engines' sources | fixture only |
| HTTP service, MCP server | contract, tenancy, security regression and idempotency tests on an ephemeral Postgres 16 | validated on macOS arm64 |

## Your data

You are responsible for holding a licence for the data you process. What queuetruth does with data, what its outputs
contain and a summary of vendor terms (Nasdaq, Databento, Tardis, Coinbase, CME, IEX, Binance Vision) are in [Data and
licensing](https://github.com/sarptandoven/queuetruth/blob/main/docs/data-and-licensing.md). It is not legal advice.

## Development

```sh
make venv sync develop     # arm64 .venv, locked dependencies, editable build of queuetruth._core
make test                  # lint + Rust + Python + service tests
make test-e2e              # end-to-end pipeline, parity, determinism, service gate
make realdata              # real-data checks against the earlier prototype's artifacts (writes .realdata/ only)
make check-docs            # every runnable block in the docs, links, generated reference pages
```

How to contribute: [CONTRIBUTING.md](https://github.com/sarptandoven/queuetruth/blob/main/CONTRIBUTING.md). Security
reports: [SECURITY.md](https://github.com/sarptandoven/queuetruth/blob/main/SECURITY.md). The design specification is
[docs/design/spec.md](https://github.com/sarptandoven/queuetruth/blob/main/docs/design/spec.md); how it was built and
verified is [docs/design/build-plan.md](https://github.com/sarptandoven/queuetruth/blob/main/docs/design/build-plan.md).

## Licence

Apache-2.0, Copyright 2026 Sarp Tandoven. Queue-model logic ported from hftbacktest (MIT, credited in `NOTICE`).
NautilusTrader behaviours are re-implemented from its public documentation and checked against its released package
by calling its public API; no NautilusTrader code is included.
Databento's queue-position estimator is re-implemented from its documented behaviour.

