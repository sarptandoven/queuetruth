# The Fill-Realism Atlas

**The site: [sarptandoven.github.io/queuetruth](https://sarptandoven.github.io/queuetruth/).**

The atlas answers one question for every popular backtester fill model: on a real market day, how far are its fills
and its P&L from what the real order queue would have given? It places small resting orders in the real queue of
every symbol of a public exchange day, replays every order of the day exactly to see which of them fill, and runs the
fill models of hftbacktest, NautilusTrader and a Databento estimator on the same orders. It is rebuilt from free IEX
DEEP+ data each trading day over the newest 20 days, and every number can be re-run and checked against a published
Merkle root. Keeping it running is in [Run the atlas](../how-to/run-the-atlas.md).

## The headline

The front page answers first, in plain words. Its first line is the share of small resting orders the real queue
fills within 1 minute, over all symbols. Then comes one sentence per library:

- **NautilusTrader**: how many more orders its default fill rule (fill when the price touches the order) fills than
  the real queue, in percentage points with a 95% interval and as a share of the real fills; then its closest
  setting and whether it is still clearly off.
- **hftbacktest** and **Databento**: the range of their settings against the real queue, then the closest setting and
  its verdict.

A difference is stated only when its 95% interval excludes 0 on the side of its estimate. Otherwise the sentence says
the setting is not clearly different from the real queue.

Over 20 trading days (2026-10-09 to 2026-11-06; 265,402 symbol-days, 12,440 symbols), the exact queue fills 11.97% of
small resting orders within 1 minute. NautilusTrader's touch fill model fills 1.50 pp more (13% more orders; 95%
interval +1.45 to +1.55 pp), and NautilusTrader's `queue_position` model is closest at +0.06 pp. The site always
shows the newest 20 days, so its numbers move a little from day to day.

## How to read the page

The front page has, in this order:

1. **The answer**: the sentences above.
2. **One chart**: every library's settings at 1 minute (all symbols), grouped by library. A dot is the fill-rate bias
   in percentage points (the model's fill rate minus the real queue's); the whisker is its 95% interval. A dot is red
   when the model clearly fills too many orders, blue when it clearly fills too few, grey when the interval includes
   0. Each row's tooltip has the model id (such as `nt_prob_touch[mode=faithful;p=1]`). The two bounds that bracket
   every honest model (always at the front of the queue; fill only on a trade-through) are left out of the chart.
3. **What to do**: one box per library with its closest setting, the verdict, and the config to paste (below).
4. **Your symbol**: a lookup that shows every library setting on one symbol at 1 s, 10 s and 1 min, with intervals
   and the number of orders behind each.
5. **What this covers**: IEX carries about 4 to 5% of US equity volume, delays every incoming order by a 350
   microsecond speed bump and has order types that reprice themselves, so its queues may not behave like those of
   other exchanges.
6. **Last built and how to verify**: the build date, the atlas root and the steps to check it ([verify a
   number](#verify-a-number)).

Every page carries IEX's attribution. The other pages:

- `hftbacktest.html`, `nautilustrader.html`, `databento.html`: one library each, with the same answer, chart and
  "what to do" box, then its models across every tier and horizon (fill rate and P&L) and its best setting by tier.
- `tables.html` ("all numbers"): every model, tier and horizon. Each liquidity tier (`ALL` first) has a chart and a
  table; a row is one fill model, with the fill-rate bias at 1 s, 10 s, 1 min and 5 min, the **P&L error** (the
  backtest's P&L minus the exact replay's, per order placed, with each first fill marked to the mid 60 s later; this
  is [`zoo-toxicity`](fill-toxicity.md)'s `bias_ticks`) and the **flagged** share (orders on symbol-days whose replay
  checks raised a flag; 0 under the published flag policy `exclude`).
- `about.html`: how to read it, how it is made and its limits, in short.

## Which setting fixes it

The "What to do" boxes on the front page and the "Best setting by tier" tables take, for each tier and library, the setting whose fill-rate bias at the sort
horizon is closest to 0 as a point estimate (hftbacktest's L3 FIFO model is left out: it needs the order-level data
the exact replay uses). It says whether that setting is **still clearly off** (its 95% interval excludes 0 on the
side of its estimate; the colours follow the same rule) or shows **no clear difference**, which means the atlas cannot tell it from the exact
replay at this sample size, not that it is exact. Settings whose intervals overlap the closest one's are listed
with it: the atlas does not rank those by closeness. Next to it is the config to paste:

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

## The data API

Next to the site, `atlas/v1/` holds the same numbers as static JSON files, for programs and for `queuetruth atlas`.
Each is a document with a JSON Schema in [the schema reference](../reference/schemas.md):

| File | Schema | What it holds |
|---|---|---|
| `manifest.json` | `qt.atlas_api_manifest` | API version 1, when the site was built, the atlas root, the venue, the window and its days, symbol-days and symbols, the mode, population and headline horizon, IEX's attribution, and the size and sha256 of every other file |
| `summary.json` | `qt.atlas_summary` | One row per library, setting, mode, shadow population, tier and horizon: the human label and the model id, the fill-rate bias in percentage points with its interval, the exact and model fill rates in percent, the verdict, the orders, symbols and days, the P&L error in ticks with its interval where the atlas has it at that horizon, and the flagged share |
| `configs.json` | `qt.atlas_configs` | Per library and tier: the closest setting, its bias and verdict, the settings it cannot be ranked against, and the config to paste with what kind of config it is |
| `symbols/<letter>.json` | `qt.atlas_symbols` | The symbols whose first letter is `<letter>` (`_` for others): tier, days, and per library setting the bias at 1 s, 10 s and 1 min with its interval, the shrunk estimate, the orders and the days |

Units are human: horizons are `1s`, `10s`, `1min`, fill rates percent, biases percentage points, P&L ticks. The
manifest's hashes let a reader check every file it downloaded. A later version of the API gets a new directory
(`atlas/v2/`); files may be added to `v1` without changing the existing ones.

## In your terminal

`queuetruth atlas` prints the answer for your library, tier or symbol, the table and the config to paste:

<!-- check-docs: skip (asks before it downloads the public data API) -->
```bash
queuetruth atlas --library nautilus
queuetruth atlas --library hftbacktest --tier T1_top100
queuetruth atlas --symbol AAPL --horizon 10s
queuetruth atlas --library nautilus --json
```

It reads the data API of the public site, or a local copy with `--from DIR` (a site directory or its `atlas/v1`).
Like `queuetruth demo`, it downloads nothing until you agree: it asks on a terminal, or takes `--yes`. The files go to
the user cache directory (`QT_CACHE_DIR` moves it), are checked against the sha256 in the manifest and are used
offline afterwards; `--refresh` gets the newest build. `--json` (or `--format json`) prints the `qt.atlas_answer`
document. From Python:

<!-- check-docs: skip (asks before it downloads the public data API) -->
```python
import queuetruth as qt

answer = qt.atlas.lookup(library="nautilus", symbol="AAPL", yes=True)
print(answer.summary)       # the answer in plain words
answer.table()              # a pandas DataFrame: setting, horizon, bias and interval in pp, verdict, orders
answer.configs[0]["config"]  # the closest setting of the symbol's tier, to paste
```

A single symbol has few orders, so its intervals are wide; the config is always the one of its tier.

## Two venues

The site can show Nasdaq TotalView-ITCH sample days next to IEX: IEX stays at the top address, Nasdaq is under
`nasdaq/`, and a line at the top of every page switches venue. Each venue has its own pages, data API, atlas and day
roots and attribution; the two are never pooled. How maintainers publish them is in
[Run the atlas](../how-to/run-the-atlas.md#nasdaq-days-on-the-site).

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
units. A leaf holds the unit's spec, the sha256 of its input bucket and of the files it wrote, and the content
digests of its outputs. Every day is kept as `day-<date>.tar.gz` on the repository's `atlas-days` release: its
reduced tables, its `day_manifest.json` (the day root and every leaf's proof) and the `plan.json` it ran. To check
one unit:

1. Download and unpack the day, and take `atlas_manifest.json` from the site's `data/` folder.
2. Check out the commit the day was built at (the site's "built at" column, or `builds` in `day_manifest.json`)
   and run `make develop`. The file sha256 values depend on the build's Parquet writer, so another commit can
   compute the same numbers and still give a different leaf.
3. Re-create the unit's bucket from IEX's public file (about 9 GB is streamed, never stored; the URL is the day's
   `source.url` in `day_manifest.json`).
4. Re-run the unit.

<!-- check-docs: skip (needs the 9 GB IEX day file) -->
```bash
uv run --frozen --no-sync python scripts/scale/iex_split.py --url "$URL" --date 2026-10-09 --out split
uv run --frozen --no-sync python scripts/scale/verify_unit.py --day day-2026-10-09 \
    --atlas-manifest atlas_manifest.json --unit-id 10092026-b000-u0001 --split split/10092026
```

`verify_unit.py` checks that the unit's spec hashes to the leaf's, re-runs the unit and compares its leaf with the
published one. It also checks the leaf's proof up to the day root, and that the atlas lists that day root and
recomputes its own root from the day roots. It says when the checkout is not the commit the day was built at.

### Verify a Nasdaq day

The steps are the same, with the Nasdaq names:

1. The day is `nasdaq-day-<date>.tar.gz` on the `atlas-days-nasdaq` release (a draft until a maintainer publishes
   it), and the atlas manifest is in the site's `nasdaq/data/` folder.
2. Re-create the buckets from Nasdaq's sample file. For the two pinned days (2019-12-30 and 2020-01-30) pass only
   `--date`, so `itch_split.py` checks the stream against the pinned hashes; for another day add `--url` with the
   day's `source.url`.
3. Re-run the unit. A unit of a sampled day re-runs from the whole day's buckets like any other.

<!-- check-docs: skip (streams the 5.6 GB Nasdaq sample file) -->
```bash
uv run --frozen --no-sync python scripts/scale/itch_split.py --date 01302020 --buckets 32 --out split
uv run --frozen --no-sync python scripts/scale/verify_unit.py --day nasdaq-day-2020-01-30 \
    --atlas-manifest atlas_manifest.json --unit-id 01302020-b000-u0000 --split split/01302020
```

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
