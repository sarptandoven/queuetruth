# Roadmap

queuetruth's aim is to be the reality check every backtest runs before money does: for any trader, in under a
minute and inside the tool they already use, how wrong the backtest is, why, and what fixes it, each with an interval
and a way to check it. The engine stays Apache-2.0, runs locally, ships no market data and sends nothing anywhere.

The full plan, with the evidence behind it, is [the next phase](docs/design/next-phase.md). This page is its short
form. Items move when evidence or users say they should; open an issue to argue for one. Ids (`W1`, `M1`, ...) are
referenced from code and scripts.

## M0: Unblock (now)

- Release 0.3.0 to PyPI and turn on GitHub Pages, so `pip install queuetruth` gives every documented command and the
  Fill-Realism Atlas (W1) has a public URL.
- Valid intervals on short captures: with fewer than five bootstrap blocks, `zoo`, `report` and `zoo-toxicity` no
  longer print zero-width intervals or mark every model significant.
- A truth sweep of README and the docs: the atlas linked from the first screen, one table of what is validated
  (IEX DEEP+ now covers 24 days), no stale version labels.
- Integrations that run as documented: the hftbacktest export defaults to an exchange that runs on the released
  hftbacktest; the NautilusTrader replay strategy works on 2.x; the plugin suites run in CI against real releases.

## M1: Answer in a minute (0.4.0)

- **Atlas v2**: chart first and in plain words, a lookup by symbol, a versioned data API next to the site, and
  touch tables for bar-based backtests (how often a limit order the price touched really fills, by tier and bar
  length).
- **`queuetruth atlas`** and **`queuetruth check`**: the atlas answer for your library from the terminal, and how
  wrong your backtest is in one command, from the atlas when you have no data and exactly when you do.
- Human units in every output (symbols, decimal prices, local times, durations such as `1s`).
- Docs with three doors (see it, read the atlas, check my backtester) on the same Pages site.
- Explain the gap between hftbacktest's L3 FIFO model and the exact queue on IEX.

## M2: Fix it in your tool (0.5.0)

- A `Backtester` plugin interface; NautilusTrader and hftbacktest plugins on PyPI; a Backtrader filler and order
  log; a LEAN fill model; `check` for Freqtrade, VectorBT and backtesting.py results.
- `queuetruth fit` (the parameter that fixes it, written only when it passes) and `queuetruth compare` (backtest
  against live, order by order).

## M3: One way to do everything (0.6.0)

- Registries for formats, live-log venues, queue models and backtesters: adding one is one file.
- One dataset layer, contracts with a single source (Rust specs generated from the JSON Schemas), operations split
  out of `api.py`, one CLI parser with six everyday verbs, a typed Python API.
- Import-linter layers and drift tests in CI.

## M4: Proof everywhere (0.7.0)

- Real-data validation of one crypto order-level venue and of CME through Databento MBO; the first real
  own-account scorecard (Bitstamp, then Hyperliquid); real exports for each importer.
- The statistics practitioners ask for in `compare`: McNemar on discordant fills, markouts at 100 ms, 1 s and 10 s,
  time-to-fill survival curves, latency fitted before the queue.
- Live drift alerts (`compare --watch`).
- Upstream: the hftbacktest #312 fix and the NautilusTrader documentation change.

## M5: 1.0

- Prediction markets, forecasts and the agent sandbox as their own packages; scikit-learn as an extra; one runner and
  one MCP server for the service and agents.
- Stability tiers on every public name and an API freeze.
- The launch: the atlas headline where traders are, a notebook per framework.

## Later

- **Hosted service.** Accounts, a web interface and a hosted MCP endpoint on top of the existing multi-tenant
  service, with the same rules as the self-hosted one: customers' data only, no pooling, no vendor credentials.
- **Verified results.** Public verification pages for certificates, commitments anchored with a timestamp authority
  or transparency log, and key revocation.
- **Engine frontier.** Your own orders' effect on the queue, pro-rata and allocation matching (CME), auctions and
  halt crosses, hidden and iceberg liquidity.
- **Agent arena.** The sandbox as a repeatable, look-ahead-free benchmark for trading agents on days after their
  training data.
- **An open fill-realism report format** and conformance suite, and a model registry so anyone can add a queue model
  to the zoo.
