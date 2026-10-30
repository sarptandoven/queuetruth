# queuetruth

[![PyPI](https://img.shields.io/pypi/v/queuetruth.svg)](https://pypi.org/project/queuetruth/)
[![Python](https://img.shields.io/pypi/pyversions/queuetruth.svg)](https://pypi.org/project/queuetruth/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/sarptandoven/queuetruth/blob/main/LICENSE)

**Your backtest fills orders your live strategy never gets. queuetruth measures by how much, and why.**

A resting order fills only after everything ahead of it in the queue has traded or cancelled. Backtesters cannot see
that queue, so they guess. queuetruth replays order-level market data through an exact FIFO book, puts your orders
in the real queue and records when each one would really have filled. Against that answer it scores the fill models
of hftbacktest, NautilusTrader and Databento, fits their parameters, and scores your own simulator against your live
fills, order by order.

## See it in a minute

```sh
pipx run queuetruth demo --yes
```

This streams the first minutes of a public Nasdaq TotalView-ITCH day (about 315 MB, cached as a 7 MB subset), places
3,000 small resting orders in the real queue of AAPL, MSFT, SPY and TSLA, and prints the answer first:

```text
Within 1 s, NautilusTrader's default fill model fills 33.1% of these orders; the exact replay of the real queue
fills 29.4%: 3.6 pp (1.12x) too many (95% CI +2.6 to +4.8 pp).
```

followed by the table for every horizon and a self-contained HTML report. Nothing else is downloaded, and nothing is
sent anywhere: queuetruth has no telemetry. [Getting started](https://github.com/sarptandoven/queuetruth/blob/main/docs/getting-started.md)
walks through the same steps by hand.

<!-- Release step: pin this image URL to the release tag (.../queuetruth/vX.Y.Z/docs/assets/...) so the PyPI page and its chart cannot disagree. -->
[![Fill-rate bias of each queue-model configuration against the exact replay at 1 s on two public Nasdaq days, with day-block bootstrap 95% intervals, drawn from the faithful-mode table in docs/RESULTS.md section 4, every model scored on the same orders.](https://raw.githubusercontent.com/sarptandoven/queuetruth/main/docs/assets/fill-bias-1s.svg)](https://github.com/sarptandoven/queuetruth/blob/main/docs/RESULTS.md#4-how-far-are-l2-queue-models-from-exact-truth-rd-8-extended-the-headline-experiment)

## What it found on real data

Two public Nasdaq ITCH days (2019-12-30, 2020-01-30), 3,000 small orders at or near the touch per day, every order
scored against the exact replay of the real queue:

- **NautilusTrader's default touch fill model overfills.** In the real engine (nautilus_trader 1.231.0; 2.0.0rc7
  fills the same orders) it fills 14.17% of orders within 1 s where the real queue fills 11.03%: **+3.13 pp**
  (95% CI [+2.53, +3.77]); +2.80 pp on the second day. On deep-queue names (BAC, F, GE, SIRI, T, PFE) it reaches
  **+8.60 pp at 60 s**.
- **Calibration fixes it out of sample.** `p = 0.29`, fitted on day one, cuts day two's 1 s overfill from +2.8 pp to
  +0.4 pp. The export refuses any fit that does not beat the library default on held-out data.
- **hftbacktest's power and log models are close on liquid names**: within about 1 pp of exact at 1 s, 10 s and
  60 s. Its partial-fill exchange stops on this order flow (issue #312); with the fix queuetruth proposes, its fills
  equal queuetruth's faithful models on all 5,795 orders.

Two days, one venue, the most liquid US stocks: refit for your venue and feed. Every table, by horizon, queue
position and liquidity tier, plus 36 more symbols in five tiers:
[Results](https://github.com/sarptandoven/queuetruth/blob/main/docs/RESULTS.md).

## Pick your path

| You want to | Start here |
|---|---|
| know how wrong your backtester's fill model is | `queuetruth demo`, then [the model zoo on your data](https://github.com/sarptandoven/queuetruth/blob/main/docs/concepts/l2-model-zoo.md) |
| get a fitted parameter to paste into hftbacktest or NautilusTrader | [Calibrate my hftbacktest model](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/calibrate-hftbacktest.md), [calibrate on an L2-only venue](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/calibrate-live-l2-venue.md) |
| compare your backtest with your live fills | [Score my Nautilus backtest against my Binance fills](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/score-nautilus-vs-binance.md), [reconcile Freqtrade or Hummingbot](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/reconcile-freqtrade-hummingbot.md) |
| regression-test your own simulator's queue logic | [Referee my simulator](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/referee-your-simulator.md) |
| prove a backtest result to someone else | [Certify a result](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/certify-a-result.md) |
| let an AI agent trade a replayed day with exact fills | [Agent sandbox](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/agent-sandbox.md), [use from an agent (MCP)](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/use-from-an-agent.md) |
| make markets on Kalshi or Polymarket, or score forecasts | [Prediction-market making](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/prediction-market-making.md), [score forecasts](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/score-forecasts.md) |

Every term (shadow, zoo, exact arm, faithful and corrected) is in the
[Glossary](https://github.com/sarptandoven/queuetruth/blob/main/docs/concepts/glossary.md).

## Bring your data

| You have | You get | Read it with |
|---|---|---|
| Order-level data: Nasdaq TotalView-ITCH 5.0, IEX DEEP+ (free), Databento DBN MBO, Coinbase `level3`, Kraken L3, Hyperliquid node, Bitstamp, Bitfinex | exact replay, shadow orders, the zoo, calibration | `queuetruth ingest --format itch \| iex-deep-plus \| dbn \| ...` |
| An L2-only tape (Tardis `incremental_book_L2` and trades) | the queue models, markouts and a parameter fitted to your fills; never exact truth | `ingest --format tardis-l2` |
| Your live orders and fills: Binance, Bybit, OKX, Coinbase, Kraken, Deribit, Hyperliquid, IBKR Flex, FIX 4.4 drop copies, QuickFIX logs | the sim-vs-live scorecard, `calibrate-live`, a latency fit | `queuetruth live import --venue <venue>` |
| Your backtest's output: hftbacktest or NautilusTrader | your simulator scored order by order against your live fills | `live import --venue sim-hftbacktest` or `sim-nautilus`, then `score --sim-log` |
| A Freqtrade backtest or dry run, or a Hummingbot paper bot, next to the bot's live run | each order paired with its twin, why it differs, and the scorecard | `queuetruth reconcile --kind freqtrade` or `hummingbot` |

queuetruth ships no market data and fetches none except `demo`'s public sample day, after you agree. You need a
licence for the data you process: [Data and licensing](https://github.com/sarptandoven/queuetruth/blob/main/docs/data-and-licensing.md).

## Why the answer is exact

- **The book is right.** Over 16,754,562 records of the two public days, read from the raw ITCH bytes by
  queuetruth's own reader: 0 invariant violations, 0 trade-parity mismatches over 633,287 printed executions.
- **The fill rule is right.** An independent matching-engine oracle agrees with the exact fill rule on 3,000 of
  3,000 shadow orders on both days; 3,000 real orders per day used as a "live" log are reproduced exactly.
- **The replicas are faithful.** queuetruth's NautilusTrader models fill the same orders as nautilus_trader 1.231.0
  on 100% of orders at 1 s, 10 s and 60 s; its hftbacktest models are bit-exact against a transliteration of
  hftbacktest's source.
- **You can rerun all of it.** `make reproduce` streams the public day files and rebuilds every published number
  from the raw bytes. `queuetruth certify` and `verify-cert --rerun` re-execute a result bit for bit.

What each check covers and what is not tested yet:
[How queuetruth is tested](https://github.com/sarptandoven/queuetruth/blob/main/docs/testing.md).

## Status

| Validated on real data | Fixtures and synthetic data only |
|---|---|
| Exact book, shadow orders and scorer on Nasdaq ITCH (2 days); IEX DEEP+ (1 day); the NautilusTrader models against the real engine; calibration and the learned fill model on a held-out day | DBN/CME fidelity; Coinbase, Kraken and Hyperliquid L3 (Bitstamp and Bitfinex: one capture each, counters only); every live-log importer; the prediction-market kit; the forecast referee |

Exact truth covers displayed liquidity on FIFO venues; shadows are censored during trading halts (recorded from
ITCH and IEX feeds). It does not model your own order's impact on others, hidden or iceberg liquidity, auctions or
pro-rata matching. queuetruth is
not a backtester, OMS or trading system.

## Install

```sh
pip install queuetruth                 # CLI + Python API (abi3 wheels: macOS arm64/x86_64, Linux x86_64/aarch64)
pip install 'queuetruth[client]'       # + HTTP client for a self-hosted service
pip install 'queuetruth[mcp]'          # + MCP server for agents
```

CPython 3.11 or later. Other platforms build from source with Rust 1.85 or later. `queuetruth-core`, a standalone
binary for the native commands, is attached to each GitHub release. Interfaces: CLI, Python API, self-contained
HTML reports, a self-hostable HTTP service and an MCP server, all sharing published JSON Schemas.

The whole workflow on a synthetic dataset generated on your machine (no download; it shows the steps, not a result
about markets):

```sh
queuetruth fixtures synth --out synth
queuetruth shadows sample --data synth --n 1000 --out shadows.parquet
queuetruth zoo --data synth --shadows shadows.parquet --out zoo
```

The same from Python:

```python
import queuetruth as qt

z = qt.run_zoo({"dataset": {"path": "synth"}, "shadows": {"path": "shadows.parquet"}, "out": "zoo"})
for row in z.leaderboard:  # fill-rate bias vs exact at 60 s, with its 95% CI
    b = row.fill_rate_bias["60000000000"]
    print(f"{row.model_id:50s} {100 * b.value:+.2f} pp [{100 * b.ci_lo:+.2f}, {100 * b.ci_hi:+.2f}]")
```

## Development

```sh
make venv sync develop     # venv, locked dependencies, editable build of queuetruth._core
make test                  # lint + Rust + Python + service tests
make reproduce             # every published real-data number from the public Nasdaq files
```

[CONTRIBUTING.md](https://github.com/sarptandoven/queuetruth/blob/main/CONTRIBUTING.md) ·
[SECURITY.md](https://github.com/sarptandoven/queuetruth/blob/main/SECURITY.md) ·
[Roadmap](https://github.com/sarptandoven/queuetruth/blob/main/ROADMAP.md) ·
[Design specification](https://github.com/sarptandoven/queuetruth/blob/main/docs/design/spec.md) ·
[Changelog](https://github.com/sarptandoven/queuetruth/blob/main/CHANGELOG.md)

## Licence

Apache-2.0, Copyright 2026 Sarp Tandoven. Queue-model logic ported from hftbacktest (MIT, credited in `NOTICE`).
NautilusTrader behaviours are re-implemented from its public documentation and checked against its released package
by calling its public API; no NautilusTrader code is included. Databento's queue-position estimator is
re-implemented from its documented behaviour.
