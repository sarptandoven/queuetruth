# The Fill-Realism Atlas

The atlas is a public table that answers one question for every popular backtester fill model: on a real market
day, how far are its fills and its P&L from what the real order queue would have given? It is rebuilt from public
data each trading day by `.github/workflows/atlas.yml`, and every number on it can be re-run and checked against a
published Merkle root. The daily venue is IEX. Nasdaq TotalView-ITCH sample days can sit next to it
([two venues](#two-venues)).

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

The two sentences at the top give the exact fill rate and name the most optimistic and the most pessimistic
non-bound models (at the sort horizon, 60 s by default) and the largest P&L error, but only among models whose 95%
interval excludes 0: a difference whose interval reaches 0 is never stated as a finding. The tables are sorted by
the size of the fill-rate bias at that horizon. Above each table, a chart draws every model's bias at that horizon
as a dot with its interval as a whisker; it is coloured only when the interval excludes 0, and the bounds are left
out of it so they do not squash the scale. Every value in a chart is also in the table below it.

## Which setting fixes it

The "Best setting by tier" table takes, for each tier and library, the setting whose fill-rate bias at the sort
horizon is closest to 0 as a point estimate (hftbacktest's L3 FIFO model is left out: it needs the order-level data
the exact replay uses). It says whether that setting is **still clearly off** (its 95% interval excludes 0 on the
side of its estimate; the colours follow the same rule) or shows **no clear difference**, which means the atlas cannot tell it from the exact
replay at this sample size, not that it is exact. Settings whose intervals overlap the closest one's are listed
under it: the atlas does not rank those by closeness. Next to it is the config to paste:

- hftbacktest: the queue model and exchange calls of your `BacktestAsset()` chain, for example
  `.power_prob_queue_model2(2).partial_fill_exchange()`, built from the same builder and exchange tables as
  `queuetruth export hftbacktest` ([Calibrate my hftbacktest queue model](../how-to/calibrate-hftbacktest.md)). The
  exchange follows the model id's `exch` (`partial` or `no_partial`).
- NautilusTrader: the `BacktestEngine.add_venue` keywords, for example
  `fill_model=FillModel(prob_fill_on_limit=1), book_type=BookType.L2_MBP, trade_execution=True, queue_position=True`
  for the `queue_position` model, as the NautilusTrader integration sets them (`FillModel` is `DefaultFillModel`
  in 2.x). The faithful models follow nautilus_trader 1.231.0; from 2.0 a book update that shrinks the order's level
  also caps the queue ahead, so a few orders fill differently. Below `prob_fill_on_limit=1`, set `random_seed` to
  compare runs.
- Databento's tutorial estimator has no library of its own; for `-1 < k <= 0` the table gives hftbacktest's
  expected-value equivalent `.power_prob_queue_model3(1 / (1 + k)).partial_fill_exchange()`, the exchange the zoo
  runs it with.

Where no released library reproduces the model (corrected mode, Databento with `k > 0`), the table shows only the
model id. The best setting is the closest one on this venue, horizon and population; refit it on your own data
before relying on it. What each model does is in [the L2 model zoo](l2-model-zoo.md).

## One page per library

`hftbacktest.html`, `nautilustrader.html` and `databento.html` each show one library's models across every tier
and horizon (fill rate and P&L), its best settings by tier with the configs, a chart per tier and a plain-words
summary: the exact fill rate, how many of the library's settings fill clearly more or fewer orders, its closest point
estimate with whether it is still clearly off, and the settings it cannot be ranked against. The index links to them.

## Two venues

The site can show two venues: IEX DEEP+ at its top address (the default) and Nasdaq TotalView-ITCH under
`nasdaq/`. A line at the top of every page switches venue. Each venue has its own pages: its own tables,
headlines, library pages, atlas and day roots, the command that re-creates a day's buckets from its public file,
and its own attribution line. The two are never pooled: a model's error on IEX and on Nasdaq are separate rows on
separate pages.

The Nasdaq days are the free TotalView-ITCH 5.0 sample days Nasdaq publishes at
[emi.nasdaq.com](https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/). The site shows them only when a maintainer turns them
on (the repository variable `ATLAS_NASDAQ_SITE`, or `nasdaq_site` on a manual run). It is off by default, because
[Data and licensing](../data-and-licensing.md) does not settle whether aggregates of the sample files may be
published.

## A Nasdaq day on one runner

A manual run of `.github/workflows/atlas.yml` with `venue: nasdaq` and a day such as `01302020` runs that sample day
on one GitHub-hosted runner (4 cores, 16 GB, a 6 h job). It keeps the reduced day as
`nasdaq-day-<date>.tar.gz` on the `atlas-days-nasdaq` release, apart from the IEX days.

A Nasdaq day is far larger than an IEX day: 2020-01-30 has 423 million messages and about 8,900 symbols, and its
plan predicts about 8 to 12 core-hours of units. When the plan predicts more than the workflow's budget (10
core-hours), the day runs a symbol sample:

- Symbols are ranked into the liquidity tiers on the whole day first.
- Every tier keeps the same number of symbols, the largest number that fits the budget. A tier with fewer symbols
  keeps them all.
- Within a tier, the kept symbols are the ones with the smallest `sha256("<seed>:<date>:<symbol>")`. The choice
  depends on the symbol and the date only, never on a result.
- The other symbols are excluded with the reason `outside the symbol sample`, so the sample is part of the run id.
- The plan, the day's `day_manifest.json` (`symbol_sample`), the atlas manifest and the venue's pages say how many
  symbols each tier kept of how many.

The buckets are still the whole day's, so a unit re-runs from buckets re-created from the public file as for any
day. A day whose prediction fits the budget runs every symbol and has no `symbol_sample`. In a sampled day the
`ALL` rows pool the sampled symbols, so each tier counts with its sample size, not with its share of the market.
Read the tier tables for a market-wide picture.

**How the budget was sized.** `scripts/scale/runner_budget.py` runs synthetic ITCH days
(`python/tests/scale/scale_itch.py`, no market data) through the same split, plan and units on a given machine.
On a 4-core, 15 GiB machine like the runner, four units at a time with one thread each:

- Unit CPU time was 0.38 to 0.56 of the cost model's prediction (three runs). The prediction carries a x1.5
  safety, so the slow end was used.
- The units kept the cores about 82% busy.
- Peak memory was about 0.42 GB plus 40 MB per 1,000 orders and 0.15 to 0.22 GB per million records.
- With a margin of 2 for real order flow, which is busier than synthetic, 3.5 h of units hold
  `3.5 x 4 x 0.82 / (0.56 x 2)`, about 10 predicted core-hours. That leaves more than 2 h of the job for the
  build, the stream and split of the 5.6 GB file, and the reduce.
- Units of at most 100 predicted core-seconds (`--unit-core-s 100`) held about 40,000 orders and peaked near 2 GB.
  So four fit at 3.5 GiB each (`--unit-mem-gib 3.5`). A single symbol gets a unit of its own and fits 3.5 GiB up to
  about 13 million records.

Re-run the measurement on another machine before changing the budget:

<!-- check-docs: skip (runs about two minutes of synthetic units) -->
```bash
uv run --frozen --no-sync python scripts/scale/runner_budget.py --workers 4 --unit-hours 3.5
```

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

A Nasdaq sample day runs the same way, on a symbol sample when the whole day is above the budget (the 5.6 GB file is
streamed, never stored; its buckets need about 7 GB of disk):

<!-- check-docs: skip (streams a 5.6 GB public file) -->
```bash
uv run --frozen --no-sync python scripts/scale/local_day.py --source itch --date 01302020 \
    --workers 4 --threads 1 --unit-core-s 100 --unit-mem-gib 3.5 --sample-budget-core-h 10 --site
```

To score your own simulator rather than the ones in the zoo, start from [getting started](../getting-started.md):
`pipx run queuetruth demo --yes` places orders in the real queue of a public day in about a minute.

## Limits

- **One venue by default.** IEX carries a few percent of US equity volume and has its own order types and speed
  bump. Its queues are thinner than Nasdaq's, so the size of a model's error can differ elsewhere; the method does
  not. Nasdaq sample days can be shown next to it ([two venues](#two-venues)), but they are a few old days, often
  on a symbol sample, not a daily series.
- **Your orders do not move the market.** A shadow order rests in the real queue without changing it: there is no
  market impact and no reaction by other traders ([what a shadow does not do](shadow-orders.md#what-a-shadow-does-not-do)).
  Large orders (`large`) are the least realistic for that reason.
- **Hidden liquidity, auctions and halts.** Hidden prints follow the venue's rule
  ([hidden prints](shadow-orders.md#hidden-prints)). Orders are placed during continuous trading, the exact fill
  rule does not model auction crosses, and an order resting through a trading halt is censored rather than scored.
- **hftbacktest's order-level model sees the same queue but fills less.** Faithful `hbt_l3_fifo` tracks the orders
  ahead exactly, yet fills fewer orders than the exact replay. It never fills an order priced better than the feed's
  best by a trade-through when the executed order is at that best, which is how IEX publishes every execution. It
  also ignores hidden prints. On a sample of the 2026-10-09 day (26 of 130 units, `standard` population), about 58%
  of its missed fills come from the trade-through rule and about 42% from hidden executions (attributed by
  elimination, not observed directly). The gap is a rule of hftbacktest, not a different queue or a different
  definition of "filled";
  [`hbt_l3_fifo`](l2-model-zoo.md#c-hbt_l3_fifo-order-level-input) has the details.
- **Markouts are not a strategy.** The P&L is the mid move after a fill, per order placed, before fees and rebates.
  It measures what a model's fill mistakes are worth, not what a strategy would earn.
