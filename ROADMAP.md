# Roadmap

queuetruth's aim is to be the neutral, open referee for trading simulation: every claim a backtest, a paper-trading
run or a trading agent makes about fills, checked against the exact queue and reported with an interval, a reason
and a fix. The engine stays Apache-2.0, runs locally, ships no market data and sends nothing anywhere.

This page lists what is being worked on and in what order. Items move when evidence or users say they should; open
an issue to argue for one. Ids (`W1`, ...) are referenced from code and scripts.

## Now

- **Halts censor shadows end to end (F2).** Done after 0.2.0: ingest records Nasdaq ITCH and IEX DEEP+ trading
  halts and every engine censors shadows there
  ([halt censoring](docs/design/extensions/halt-censoring.md)). Next: DBN `status` records and NYSE security status.
- **First real own-account fills.** Score a simulator against real fills from a real account (Bitstamp via
  `tools/livefill`, then Hyperliquid), and publish the scorecard. Every live-log importer is still checked on
  fixtures only; this is the most important missing proof.
- **Upstream fixes.** Submit the prepared hftbacktest #312 fix (`integrations/hftbacktest/upstream/`) and the
  NautilusTrader documentation change (`integrations/nautilus/UPSTREAM.md`); publish `queuetruth-nautilus` to PyPI.
- **Freqtrade and Hummingbot from the CLI.** Done after 0.2.0: `live import --venue freqtrade | hummingbot` and
  `queuetruth reconcile` ([design](docs/design/extensions/freqtrade-hummingbot.md)). Next: real bot exports.
- **Untrusted-input hardening.** The `dbn` decoder faults are kept out by our reader and the fuzz targets run weekly
  in CI (done after 0.2.0); the Parquet and Arrow panics in `fuzz/known-crashes/parquet/` are still open.

## Next

- **W1: the Fill-Realism Atlas.** A public, nightly-rebuilt site answering "how wrong is my backtester on this
  venue, and which parameter fixes it?": bias by simulator, venue, liquidity tier and horizon with intervals,
  calibrated parameters to paste with their trust verdict, Merkle roots and a verify command on every table. Built
  only on data that may be republished: IEX DEEP+ (with IEX's attribution), on-chain venues, and aggregate
  statistics. The pipeline (`scripts/scale/`) and the site generator (`scripts/scale/atlas_site.py`) exist; next
  are the cloud run over the 19 public Nasdaq days, an IEX DEEP+ splitter so the same pipeline runs nightly on IEX,
  and hosting.
- **Upstream compatibility dashboard.** `scripts/nightly` made public: every new hftbacktest and NautilusTrader
  release checked against the exact queue within a day.
- **`referee` as a GitHub Action**: done after 0.2.0 (`actions/referee`); next is getting simulator maintainers to
  run it and show the badge.
- **Plugins for more backtesters**: hftbacktest packaged like the NautilusTrader plugin, then Hummingbot, Freqtrade,
  LEAN, VectorBT and Backtrader adapters.
- **A one-line "reality gap"** in every report: how much a simulator overstates fills and P&L, in plain words, first.

## Later

- **Agent arena.** The agent sandbox as a repeatable, look-ahead-free benchmark for trading agents: incremental
  replay sessions (linear rather than quadratic in episode length), a blind mode that hides instrument and date,
  event-driven wake-ups, multi-instrument episodes and certified results on days after the agents' training data.
- **Hosted service.** Accounts, a web interface and a hosted MCP endpoint on top of the existing multi-tenant
  service, with the same rules as the self-hosted one: customers' data only, no pooling, no vendor credentials.
- **Verified results.** Public verification pages for certificates, commitments anchored with a timestamp
  authority or transparency log, and key revocation.
- **Engine frontier.** Your own orders' effect on the queue, pro-rata and allocation matching (CME), auctions and
  halt crosses, hidden and iceberg liquidity, real-data validation for DBN/CME, NYSE and the crypto L3 sources.
- **An open fill-realism report format** and conformance suite (the `qt.*` schemas and referee fixtures), and a
  model registry so anyone can add a queue model to the zoo.
- **Live drift monitoring.** Streaming ingest and shadows that run next to production orders, with alerts when live
  fills drift from the calibrated model.
