# queuetruth

**Your backtest fills orders your live strategy never gets. queuetruth measures by how much, and why.**

A passive order fills only after everything ahead of it in the queue has traded or cancelled. Backtesters that see
L2 data (sizes per price level) cannot know where you were in that queue, so they guess. queuetruth replays
order-level (MBO) data through an exact FIFO book, places shadow orders in the real queue and records when each one
would have filled. It runs the L2 queue models people use (hftbacktest's, ported from its source; NautilusTrader's,
re-implemented and checked order by order against the real engine; Databento's, re-implemented from its tutorial) on
an L2 view of the same data, measures how far each one is from the exact answer, fits their parameters against it,
and scores your simulator against your own live fills, order by order.

## What it found on real data

On 3,000 small orders at or near the touch of AAPL, MSFT, SPY and TSLA on each of two public Nasdaq TotalView-ITCH
days (2019-12-30 and 2020-01-30), every order evaluated against the exact replay of the real queue:

- **NautilusTrader's touch fill model, with the default `prob_fill_on_limit = 1`, overfills.** Verified in the real
  engine (nautilus_trader 1.231.0, `BacktestEngine` fed the L2 view as `OrderBookDelta`s,
  `FillModel(prob_fill_on_limit=1)`): it fills 14.17% of the orders within 1 s on 2019-12-30 where the exact replay
  fills 11.03%, +3.13 percentage points (95% CI [+2.53, +3.77]); +2.80 pp on 2020-01-30. queuetruth's faithful
  NautilusTrader models agree with the real engine on 100% of orders at 1 s, 10 s and 60 s on both days and on the
  synthetic fixture. On an `OrderBookDepth10` feed Nautilus resets its queue position at every snapshot, so its
  queue model then fills like the touch model (+0.9 to +3.3 pp above exact at 1 s to 60 s).
- **Calibration fixes it on the held-out Nasdaq day.** `p` fitted on day one is 0.29 (bootstrap CI [0.27, 0.30]); on
  day two it cuts the 1 s overfill from +2.8 pp to +0.4 pp and more than halves the 1 s Brier score. `p` is per
  print, so it is venue- and feed-specific: refit it for yours.
- **The hftbacktest power and log models are close on these names**: within about 1 pp of exact at 1 s, 10 s and
  60 s on both days, mostly slightly pessimistic. `hbt_risk_adverse` is pessimistic by up to 1.3 pp. Refitting
  power3's `n` and Databento's `k` gave values that score about like the defaults on day two, and the export refuses
  the fitted `n` because it does not beat `n = 2` at every horizon.

So L2 optimism is model-specific and, for hftbacktest on very liquid Nasdaq names, modest. These are two days and
four of the most liquid US stocks, where most small orders at the touch fill within a minute.

**Breadth: 36 more Nasdaq names in five liquidity tiers** (mega caps, deep-queue low-priced large caps, ETFs, mid
caps, small caps; same two days, raw ITCH bytes, every exactness check clean). The NautilusTrader default overfills
in every tier and most on the deep-queue names (BAC, F, GE, SIRI, T, PFE): +8.60 pp at 60 s on day one, where the
exact replay fills 17.0%. hftbacktest's `power3[n=2]` stays within 1.7 pp and `nt_queue_l2` within 1.5 pp of exact at
60 s in every tier. Order size barely moves first-fill bias. This is still two days of one venue, scored on the
first fill only.

All 17 rows (15 model configurations and 2 bounds), both modes, by horizon, queue decile and liquidity, the breadth
tables, and every artifact named: [docs/RESULTS.md](docs/RESULTS.md).

## Why the answer is exact

- **Raw public bytes, our own reader.** The leaderboard, the breadth run and the calibration above are computed
  from the raw Nasdaq ITCH 5.0 files with queuetruth's own ITCH reader. On the same build the leaderboard and the
  calibration are identical, cell for cell, to the same runs on the earlier private prototype's CSV conversion of
  those files. The book checks and the mirror-shadow checks below were also rerun from the raw bytes and match the
  prototype's outputs exactly.
- **The book is right.** 16,754,562 records with 0 invariant violations, 0 trade-parity mismatches over 633,287
  printed executions, 0 price-priority violations; 98.7% and 99.1% of real executions hit the head of their queue in
  our book after grouping sweeps.
- **The shadow rules are right.** Under the prototype's fill rule (`--fill-rule et`), 3,000 of 3,000 mirror shadows
  per day reproduce the prototype's fills and queue traces. An independent matching-engine oracle agrees with the
  default `exact` fill rule on 2,992 and 2,985 of 3,000 shadows (raw ITCH bytes, queuetruth's own mirror sampler,
  about half of them filling) and on 2,997 of 3,000 per day of the prototype's mirror shadows on its CSV conversion, with
  equal total filled quantity in every run.
- **The scorer is right.** 3,000 real orders per day used as a "live" log are reproduced exactly by the exact arm:
  250 filled in both, 2,750 in neither, 0 discordant on 2019-12-30; 195 / 2,805 / 0 on 2020-01-30; the same filled
  quantity and first-fill time on every order.
- **Reproduce it**: `make reproduce` (`scripts/repro/reproduce.py`) streams the two public day files from Nasdaq,
  keeps the four symbols (the raw files are never stored) and reruns ingest, replay, the leaderboard, calibration,
  the learned model and the oracle from those bytes alone, without the prototype's artifacts (1,387 s at 4 threads
  once the subsets are on disk). `make reproduce-breadth` (`scripts/repro/breadth.py`) reruns the 36-symbol breadth
  study. Rerun on the 0.1.0 commit (`4be5240`, 1,162 s), it reproduced the leaderboard on both days, all four
  calibrations, the learned model and the oracle identically. The checks that compare with the prototype's outputs
  need its artifacts, which are not public. Commands and provenance: [docs/RESULTS.md](docs/RESULTS.md).

Also in 0.1.0: order-level ingest of IEX DEEP+ (free public data, redistributable with IEX's attribution;
[guide](docs/how-to/iex-public-demo.md)), Coinbase Exchange `level3`, Kraken L3 and Hyperliquid node captures
([guide](docs/how-to/crypto-l3-sources.md)), and `queuetruth dq`, a data-quality report for any dataset
([guide](docs/how-to/check-a-feed.md)).

How every claim is tested, including what is not: [How queuetruth is tested](docs/testing.md).

## Install and quickstart (about 5 minutes)

0.1.0 is not on PyPI yet. Until it is, build from a checkout (see [Development](#development)); after the release:

```sh
pip install queuetruth                 # CLI + Python API (abi3 wheels: macOS arm64/x86_64, Linux x86_64/aarch64)
pip install 'queuetruth[client]'       # + HTTP client for a self-hosted service
pip install 'queuetruth[mcp]'          # + MCP server for agents
```

CPython 3.11 or later. Other platforms build from source with Rust 1.85 or later. `queuetruth-core`, a standalone
binary for the native commands, is attached to each GitHub release.

```sh
queuetruth fixtures synth --out synth                 # synthetic MBO dataset, generated locally (no download)
queuetruth shadows sample --data synth --n 1000 --out shadows.parquet
queuetruth zoo --data synth --shadows shadows.parquet --out zoo   # prints the leaderboard first
queuetruth report --in zoo --out report.html
```

The synthetic data shows the workflow, not the result: its order flow is not built to make queue position matter,
so its leaderboard says nothing about real markets. The evidence about real markets is the Nasdaq table
in [docs/RESULTS.md](docs/RESULTS.md); to get it on your own data, ingest a day you are licensed to use and run the
same commands ([Getting started](docs/getting-started.md)).

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
- **Queue-model zoo**: 15 queue-model configurations from hftbacktest, NautilusTrader and Databento (one of them,
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
| Shadow orders | 2 x 3,000 mirror shadows identical to the prototype's (its fill rule); an independent oracle agrees on 2,992 and 2,985 of 3,000 (raw ITCH bytes) and 2,997 of 3,000 per day (prototype's CSV and shadows) | validated |
| L2 queue-model zoo | 17 rows x 2 modes on 2 x 3,000 shadows, plus 36 more symbols in 5 tiers; the bracket `front_of_queue` >= exact >= `trade_through_only` holds order by order by construction, tested on scenario and synthetic tapes and with 0 per-order violations on real day one; `hbt_l3_fifo` corrected equals exact | real-data leaderboard published |
| NautilusTrader models | faithful `nt_prob_touch` and `nt_queue_l2` agree with nautilus_trader 1.231.0's `BacktestEngine` (delta-fed L2) on 100% of orders at 1 s, 10 s and 60 s, on both days and the synthetic fixture (`scripts/nt_crosscheck`) | checked against the real engine |
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

You are responsible for holding a licence for the data you process. What queuetruth does with data, what its
outputs contain and a summary of vendor terms (Nasdaq, Databento, Tardis, Coinbase, CME, IEX, Binance Vision) are in
[Data and licensing](docs/data-and-licensing.md). It is not legal advice.

## Development

```sh
make venv sync develop     # arm64 .venv, locked dependencies, editable build of queuetruth._core
make test                  # lint + Rust + Python + service tests
make test-e2e              # end-to-end pipeline, parity, determinism, service gate
make realdata              # real-data checks against the earlier prototype's artifacts (writes .realdata/ only)
make check-docs            # every runnable block in the docs, links, generated reference pages
```

How to contribute: [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md). The design
specification is [docs/design/spec.md](docs/design/spec.md); how it was built and verified is
[docs/design/build-plan.md](docs/design/build-plan.md).

## Licence

Apache-2.0, Copyright 2026 Sarp Tandoven. Queue-model logic ported from hftbacktest (MIT, credited in `NOTICE`).
NautilusTrader behaviours are re-implemented from its public documentation and checked against its released package
by calling its public API; no NautilusTrader code is included.
Databento's queue-position estimator is re-implemented from its documented behaviour.
