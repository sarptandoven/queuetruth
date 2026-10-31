# The Fill-Realism Atlas

The atlas is a public table that answers one question for every popular backtester fill model: on a real market
day, how far are its fills and its P&L from what the real order queue would have given? It is rebuilt from public
data each trading day by `.github/workflows/atlas.yml`, and every number on it can be re-run and checked against a
published Merkle root.

## How to read it

Each liquidity tier (`ALL` first) has one table. A row is one fill model: hftbacktest's queue models,
NautilusTrader's fill models, a Databento tutorial estimator, and two bounds (front of the queue, trade-through
only) that bracket every honest model. The columns are:

- **Fill-rate bias at each horizon**, in percentage points: the model's probability that an order has filled within
  1 s, 10 s, 60 s or 5 min, minus the share that did fill in the exact replay. Positive means the backtest fills
  orders the real queue would not have filled, so it is too optimistic. The interval under each value is a 95%
  interval from a bootstrap over dates and symbols.
- **P&L, ticks**: the backtest's P&L minus the exact replay's, per order placed, with each order's first fill
  marked to the mid 60 s later (`--pnl-horizon`). Positive means the backtest's P&L is too high. This is
  [`zoo-toxicity`](fill-toxicity.md)'s `bias_ticks`, and it splits into fills the model grants that never happen,
  fills it misses, and fills it times differently.
- **flagged**: the share of the row's orders on symbol-days whose replay checks raised a flag or that were not in
  continuous trading. With the published flag policy (`exclude`) those symbol-days are left out and this is 0.

The two sentences at the top name the non-bound model furthest from the real queue in fill rate (at the sort
horizon, 1 s by default) and in P&L. The tables are sorted by the size of the fill-rate bias at that horizon.

## How a number is made

1. **Data.** One full IEX DEEP+ day: every order book update of every symbol on IEX, which IEX publishes and
   allows to be republished with its attribution ([Data and licensing](../data-and-licensing.md)). The day is
   streamed once and split into 32 symbol buckets (`scripts/scale/iex_split.py`); a bucket is byte-identical to
   IEX's own filter of its symbols.
2. **Orders.** On each symbol, [shadow orders](shadow-orders.md) are placed at random times of the regular
   session, at the touch or one or two ticks behind or inside it, for one lot or the median order size (population
   `standard`; `large` uses 1,000 and 2,000 lots; `deep` sits 3 and 5 ticks behind). More active symbols get more
   orders.
3. **Truth.** The exact FIFO replay of the order-level feed says when each order would have filled
   ([Exact FIFO truth](exact-truth.md)). Each symbol's replay is checked against the venue's own trade prints first.
4. **Models.** The [L2 model zoo](l2-model-zoo.md) runs every fill model on the same orders from the aggregated
   (L2) view of the same book, in `faithful` mode (the libraries as released) and `corrected` mode (their known
   quirks fixed). The site shows `faithful`.
5. **Statistics.** Each unit of work sums its results per symbol-day (`queuetruth.eval.blocks`), so a day of 13,000
   symbol-days reduces to small tables, and the atlas combines days with a bootstrap that resamples dates and
   symbols together (`scripts/scale/atlas.py`). The P&L sums are exactly `zoo-toxicity`'s numbers over the same
   orders; a test runs the command on one unit and compares.

## Verify a number

The page shows the atlas root and every day's root. A day root is a Merkle root over the leaves of that day's
units; a leaf holds the unit's spec, the sha256 of its input bucket and the content digests of its outputs. To check
one unit, re-create its bucket from IEX's public file and re-run it:

<!-- check-docs: skip (needs the 9 GB IEX day file) -->
```bash
uv run --frozen --no-sync python scripts/scale/verify_unit.py --plan plan.json \
    --atlas atlas_manifest.json --unit-id 10092026-b000-u0001 --split split/10092026
```

It re-runs the unit, compares its leaf with the published one and checks the leaf's proof up to the root. Every
day's reduced tables are kept as assets of the repository's `atlas-days` release.

## Run a day yourself

One command takes an IEX day from the public file to the site (about 9 GB is streamed, never stored; plan on two
to four hours on a 4-core, 16 GB machine):

<!-- check-docs: skip (streams a 9 GB public file) -->
```bash
uv run --frozen --no-sync python scripts/scale/local_day.py --source iex --date 2026-10-09 \
    --url https://storage.googleapis.com/iex/data/feeds/20261009/20261009_IEXTP1_DPLS1.0.pcap.gz --site
```

To score your own simulator rather than the ones in the zoo, start from [getting started](../getting-started.md):
`pipx run queuetruth demo --yes` places orders in the real queue of a public day in about a minute.

## Limits

- **One venue.** IEX carries a few percent of US equity volume and has its own order types and speed bump. Its
  queues are thinner than Nasdaq's, so the size of a model's error can differ elsewhere; the method does not.
- **Your orders do not move the market.** A shadow order rests in the real queue without changing it: there is no
  market impact and no reaction by other traders ([what a shadow does not do](shadow-orders.md#what-a-shadow-does-not-do)).
  Large orders (`large`) are the least realistic for that reason.
- **Hidden liquidity, auctions and halts.** Hidden prints follow the venue's rule
  ([hidden prints](shadow-orders.md#hidden-prints)). Orders are placed during continuous trading, the exact fill
  rule does not model auction crosses, and an order resting through a trading halt is censored rather than scored.
- **Markouts are not a strategy.** The P&L is the mid move after a fill, per order placed, before fees and rebates.
  It measures what a model's fill mistakes are worth, not what a strategy would earn.
