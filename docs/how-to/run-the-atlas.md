# Run the atlas

**Goal:** keep the [Fill-Realism Atlas](../concepts/fill-realism-atlas.md) running: the daily IEX build, the Nasdaq
sample days next to it, and a day run on your own machine. This page is for maintainers; readers of the atlas need
[the concept page](../concepts/fill-realism-atlas.md) only.

**You need:** write access to the repository for the releases and variables below, or a checkout with
`make develop` for a local day.

## The daily build

`.github/workflows/atlas.yml` runs every trading day. It streams the newest IEX DEEP+ day, keeps the reduced day as
`day-<date>.tar.gz` on the `atlas-days` release, combines the newest 20 kept days (`scripts/scale/atlas.py combine
--flag-policy exclude`) and builds the site with `scripts/scale/atlas_site.py`, which also writes the data API under
`atlas/v1/`. GitHub Pages serves the result at
[sarptandoven.github.io/queuetruth](https://sarptandoven.github.io/queuetruth/).
`.github/workflows/atlas-backfill.yml` fills the newest 20 sessions at once, one runner per day. The site's "last
built" date is the newest day of the window unless the build passes `--built-at` or sets `SOURCE_DATE_EPOCH`, so
two builds of the same days give the same bytes.

## Nasdaq days on the site

The Nasdaq days are the free TotalView-ITCH 5.0 sample days Nasdaq publishes at
[emi.nasdaq.com](https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/). Nothing derived from them is public until a
maintainer decides, because [Data and licensing](../data-and-licensing.md) does not settle whether the atlas's
tables of the sample files may be published:

- The reduced days are kept on a draft release, which only people with write access see. A maintainer publishes it
  with `gh release edit atlas-days-nasdaq --draft=false`.
- The site shows them only when the repository variable `ATLAS_NASDAQ_SITE` is `true` and the release is
  published, so the verify link on every Nasdaq page works for its readers. Publish the release first, then set the
  variable. A manual run with `nasdaq_site` shows them until the next scheduled deploy, which follows the variable
  again.
- The Nasdaq part of the site is best effort: when its days cannot be combined or its pages cannot be built, the
  IEX site is built alone and still deploys. Before the first IEX day the site is the compatibility page alone,
  without Nasdaq.

## A Nasdaq day on one runner

A manual run of `.github/workflows/atlas.yml` with `venue: nasdaq` and a day such as `01302020` runs that sample day
on one GitHub-hosted runner (4 cores, 16 GB, a 6 h job). It keeps the reduced day as
`nasdaq-day-<date>.tar.gz` on the draft `atlas-days-nasdaq` release, apart from the IEX days.

A Nasdaq day is far larger than an IEX day: 2020-01-30 has 423 million messages and about 8,900 symbols. The local
pilot of that day (run `45c0dd05ddf70b67`, `scripts/scale/findings/README.md`) planned 130 units of at most 300
predicted core-seconds, so its plan predicts at most about 10.8 core-hours; the pilot's plan file is not kept, so
whether the day fits the budget below is decided by the first run and recorded in its `local_day.json`. When the
plan predicts more than the workflow's budget (10 core-hours), the day runs a symbol sample:

- Symbols are ranked into the liquidity tiers on the whole day first.
- Every tier keeps the same number of symbols, the largest number that fits the budget. A tier with fewer symbols
  keeps them all.
- Within a tier, the kept symbols are the ones with the smallest `sha256("<seed>:<date>:<symbol>")`. The choice
  depends on the symbol and the date only, never on a result.
- The other symbols are excluded with the reason `outside the symbol sample`, so the kept symbols are part of the
  run id. Only the rule, the seed and the per-tier count enter it besides: another budget or a refit cost model that
  keeps the same symbols gives the same run id.
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
  So four fit at 3 GiB each (`--unit-mem-gib 3`), with room on a runner whose available memory is below 14 GiB. A
  single symbol gets a unit of its own and fits 3 GiB up to about 11 million records.
- When memory holds fewer units than asked, `local_day.py` shrinks the budget in proportion before it plans the
  day (three workers of four: 7.5 core-hours), so the units still fit their wall time.

Two things the synthetic days do not measure: the runner's own speed (its 4 vCPUs may be 2 cores with two threads
each) and the time each unit spends reading its whole bucket (about 13 million messages on a real day; smaller units
mean about three times as many). Check both in the first run's `local_day.json` (unit wall times and CPU), and run
`runner_budget.py` once on a hosted runner, before relying on the budget.

Re-run the measurement on another machine before changing the budget:

<!-- check-docs: skip (runs about two minutes of synthetic units) -->
```bash
uv run --frozen --no-sync python scripts/scale/runner_budget.py --workers 4 --unit-hours 3.5
```

## Run a day yourself


One command takes an IEX day from the public file to the site (about 9 GB is streamed, never stored; plan on two
to four hours on a 4-core, 16 GB machine). Units of at most 100 predicted core-seconds peak below 3 GB, so four run
at a time there, as in the workflows; the day's numbers do not depend on the unit size:

<!-- check-docs: skip (streams a 9 GB public file) -->
```bash
uv run --frozen --no-sync python scripts/scale/local_day.py --source iex --date 2026-10-09 \
    --url https://storage.googleapis.com/iex/data/feeds/20261009/20261009_IEXTP1_DPLS1.0.pcap.gz \
    --workers 4 --threads 1 --unit-core-s 100 --unit-mem-gib 3.5 --site
```

A Nasdaq sample day runs the same way, on a symbol sample when the whole day is above the budget (the 5.6 GB file is
streamed, never stored; its buckets need about 7 GB of disk):

<!-- check-docs: skip (streams a 5.6 GB public file) -->
```bash
uv run --frozen --no-sync python scripts/scale/local_day.py --source itch --date 01302020 \
    --workers 4 --threads 1 --unit-core-s 100 --unit-mem-gib 3 --sample-budget-core-h 10 --site
```
