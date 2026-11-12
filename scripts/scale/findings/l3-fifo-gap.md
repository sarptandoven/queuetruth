# Findings: why hftbacktest's L3 FIFO model fills fewer orders than the exact queue

The atlas reports that `hbt_l3_fifo[mode=faithful]` (hftbacktest's `L3FIFOQueueModel` with its L3 no-partial-fill
exchange, fed the same order-level data as the exact replay) fills about 15% fewer small resting orders than the
exact replay within 1 minute, and books less P&L. A FIFO model that reads order-level data should be close to the
exact queue, so this page traces the gap on real data. Short version:

- **The gap is hftbacktest's own behaviour, not a replica error.** The released hftbacktest 2.4.4, run on the same
  orders and the same IEX DEEP+ and Nasdaq ITCH records, gives the same first fill as queuetruth's replica on every
  one of 117,356 orders (96,028 on IEX, 21,328 on Nasdaq). Its current master (`5f3ec40`) differs from 2.4.4 only in
  the status it gives a modified backtest order. Nothing in `crates/zoo` needs fixing.
- **Every missed fill is an order alone ahead of the displayed book.** In each one, when the exact replay fills the
  order, no displayed order on its side is at or better than its price: it improved the touch, or the orders at its
  level left. hftbacktest has two rules that miss exactly that case:
  - **R1, trade-through judged against the feed's best price.** hftbacktest fills a better-priced backtest order only
    when an execution is through the feed's best price, not through the order's own price. ITCH and DEEP+ remove
    each executed order before the next execution, so the executed order is always at the feed's best, and an order
    priced better than every displayed order never fills this way.
  - **R2, hidden executions are ignored.** hftbacktest skips a fill without a side. The exact replay fills a resting
    order from a hidden print at its price once nothing displayed is ahead of it, or through its price (the hidden
    print rule of `docs/concepts/shadow-orders.md`).
- **On IEX, R2 is three quarters of the gap.** Of the fills only the exact replay makes within 60 s, 75% come from
  hidden prints and 25% from R1. On IEX, hidden executions outnumber displayed ones several times over, so R2 is
  mostly IEX-specific in size. R1 is not: on the Nasdaq demo window it is 40% of a smaller gap.
- **None of the suspected IEX causes shows up.** No missed fill falls outside R1 and R2, and the hftbacktest runs
  had no unknown order ids, no errors and no order expired at entry. Cancels and size cuts ahead of the order,
  partial executions, order-id reuse, repricing order types (D-Limit, D-Peg) and the speed bump all reach both
  models the same way, through DEEP+ adds, deletes, modifies and executions in feed order.
- **hftbacktest fills most of these orders later, on an adverse move.** Of the 2,670 orders it misses on IEX, it
  fills 1,785 later in their life, 1,632 of them only when the opposite side crosses their price (a median of 71 s
  after the exact replay's fill). So the backtest swaps a fill from a trade inside the spread for a fill after the
  price moved through the order. That is consistent with the atlas's lower P&L for this model; this run did not
  measure P&L.

## How it was traced

1. **The source.** Every rule of `crates/zoo/src/l3.rs` (faithful mode) and of the MBO arm in `crates/zoo/src/sim.rs`
   was read against hftbacktest's `L3FIFOQueueModel` (`hftbacktest/src/backtest/models/queue.rs`) and
   `L3NoPartialFillExchange` (`hftbacktest/src/backtest/proc/l3_nopartialfillexchange.rs`) at the tag `py-v2.4.4`
   (`a244a14`) and at master (`5f3ec40`). The differences are listed in the next section.
2. **The data.** The IEX DEEP+ day 2026-10-22 (a day of the `atlas-days` release) was streamed with
   `iex_split.py --url ... --date 2026-10-22 --buckets 1` for 12 symbols across the liquidity tiers (index ETFs,
   mega caps, low-priced deep queues, mid caps, small caps) and ingested with `format: iex_deep_plus`, 12.1M records.
   The Nasdaq ITCH demo day was cached with `queuetruth demo --yes --minutes 30` (2019-12-30, 09:35 to 10:05 ET, the
   demo's four liquid names, 1.36M records) and ingested with `format: itch`.
3. **The harness** (`l3_fifo_gap/`) runs both modes of `qt_zoo::l3::L3Pair` on each symbol: the faithful mode
   (hftbacktest's rules) and the corrected mode, which equals the exact replay (oracle O-ZOO-4). It draws shadows
   with the `standard` placements (60% at the touch, 20% one tick behind, 10% two behind, 10% one tick better) on a
   two-sided, uncrossed book in regular hours, live 300 s each, and only where the next event is strictly later, so
   hftbacktest issues each order at the same point of the tape. At the corrected first fill it records what the
   event held and names the cause of a fill the faithful mode lacks.
4. **The released hftbacktest** (`hbt_l3_run.py`, in the venv of `integrations/hftbacktest/setup_venvs.sh`) replays the
   same records as hftbacktest L3 events with the same orders (GTX, zero latency, cancelled at their end) through
   `L3FIFOQueueModel` and its exchange, with the order loop of `hbt_driver.py`.
5. **The zoo itself.** `qt.sample_shadows` (`standard` population, 3,000 shadows per symbol) and the native zoo with
   `hbt_l3_fifo` in both modes on the IEX dataset, scored as the atlas scores it (first fill within 60 s); then
   `queuetruth why` on missed orders.

## Every behavioural difference between the replica and hftbacktest

| Behaviour | hftbacktest 2.4.4 and master | Replica (`crates/zoo`) | Effect on the first fill |
|---|---|---|---|
| Queue position at entry | appended behind every feed order at its price | `ahead_set` = every order at the price at activation | none |
| Fill from the queue | a backtest order ahead of the executed feed order fills in full | an execution of an order not in `ahead_set` at the price fills in full | none |
| Trade-through | only if the execution is through the feed's best, then every backtest order between the best and the execution (tick-range shortcut) | ported, with the shortcut | none (this is R1) |
| Fill without a side (hidden) | skipped (`todo: handle properly if no side is provided`) | `T` records ignored in faithful mode | none (this is R2) |
| Cross | on an add or modify that moves the opposite best through the order | at event end, if the opposite best is at or through the price | none here: one DEEP+ or ITCH message is one event |
| Feed modify | keeps priority on the same price and a smaller size, else to the back | a size cut (`C` part) keeps priority, a delete and re-add loses it | none: the DEEP+ mapping writes them that way |
| GTX order crossing at entry | expires | expires | none (not exercised: entries on uncrossed books) |
| Whole-order fills | yes | yes | none on the first fill |
| Master vs 2.4.4 | a modified backtest order gets status `New` | not modelled | none |

## Results

The 60 s fill rates below are of the harness's shadows, which differ from the atlas's population (no size mix, every
`every`-th event, uncrossed books only), so they explain the gap, not restate it.

| Run | Orders | Exact fills within 60 s | Faithful | Difference | hftbacktest 2.4.4 differs from the replica |
|---|---|---|---|---|---|
| IEX 2026-10-22, 12 symbols | 96,028 | 45.40% | 42.62% | -2.78 pp | 0 orders |
| Nasdaq 2019-12-30, 4 symbols, 30 min | 21,328 | 85.97% | 84.80% | -1.17 pp | 0 orders |
| Zoo, IEX, `standard` population | 36,000 | 59.13% | 56.97% | -2.17 pp | (corrected mode: +0.00 pp) |

The fills only the exact replay makes within 60 s, by cause:

| Cause | IEX | share | Nasdaq | share |
|---|---|---|---|---|
| R2: hidden print through the price | 1,027 | 38.5% | 100 | 35.3% |
| R2: hidden print at the price, nothing displayed ahead | 976 | 36.6% | 69 | 24.4% |
| R1: the order's level emptied by cancels before | 420 | 15.7% | 37 | 13.1% |
| R1: the order's level emptied by executions before | 68 | 2.5% | 41 | 14.5% |
| R1: the order's level emptied in a sweep at the same time | 12 | 0.4% | 1 | 0.4% |
| R1: placed one tick better (improving the touch) | 102 | 3.8% | 17 | 6.0% |
| R1: placed at an empty level | 65 | 2.4% | 18 | 6.4% |
| R1: execution below the feed's best (tick-range shortcut) | 0 | 0% | 0 | 0% |
| anything else | 0 | 0% | 0 | 0% |
| **total** | **2,670** | | **283** | |

- In every R1 case the execution was at the feed's best, which was worse than the order's price. Most R1 orders were
  placed at or behind the touch and were left alone above the book when the orders at and ahead of their level
  left; only a minority improved the touch.
- In the zoo run, the 780 orders the faithful mode misses have corrected first-fill reason `trade_through` (550) or
  `queue` (230); `queue` can only be a hidden print for this model. `queuetruth why` on them shows the fill on a
  side `N` print with nothing ahead (`queue_depleted`) or through the price (`trade_through`).
- On IEX the faithful mode makes no fill the exact replay lacks. On Nasdaq it makes 34: executions behind the order
  at a price other than its resting price, which the exact replay does not count (ruling A4b).
- The IEX `T` records the mapping writes as hidden prints are not copies of displayed executions: under 1% share
  their time, price and size with an `F` record. On IEX they outnumber the displayed executions about 5 to 1 (AAPL:
  17,411 against 3,765, about 70% of them on the cent grid); on the Nasdaq window they are about 1 in 7.
- The faithful mode fills 1,785 of the 2,670 IEX misses later in their life (1,632 by a cross, 153 from the queue)
  and 161 of the 283 Nasdaq misses (121 by a cross).
- `crates/zoo/tests/l3_faithful_gap.rs` pins the headline on the synthetic Nasdaq days: every missed fill is an order
  with no displayed order at or ahead of it on its side (`every_missed_fill_is_an_order_alone_ahead_of_the_displayed_book`).

## What it means for traders

- If you backtest passive orders with hftbacktest's L3 FIFO model on a Nasdaq ITCH or IEX DEEP+ style feed, it is
  pessimistic exactly when your order is the best displayed price on its side: after you improve the touch, or after
  the orders ahead of you and at your price leave. Those are the orders a market maker most wants filled.
- On IEX most of the missing fills come from hidden liquidity trading at or through your price. hftbacktest cannot
  see those trades, so no queue setting fixes it; the fix is in the exchange model (upstream draft below).
- The model does not simply lose these fills: it fills most of them later, when the market moves through your price.
  Expect its P&L to be too low for passive strategies, not only its fill count.
- Everything else in the model agrees with the exact queue order by order, so for orders that stay inside the
  displayed queue its fills can be trusted.

## What this does not show

- The cause shares come from one IEX day and 12 symbols, and a 30-minute Nasdaq window of 4 symbols. They describe
  the mechanism; the atlas's 20-day numbers remain the measured size of the gap.
- The hidden-print rule of the exact replay is queuetruth's ruling (displayed interest has priority over hidden
  interest at the same price), not a fact read from the feed. R2's share is the share of the gap that rests on it.
- GTX expiry at entry was not exercised (entries on uncrossed books only); the atlas's 2026-10-09 sample attributed
  no misses to it (`docs/concepts/l2-model-zoo.md`).
- P&L was not measured in these runs.

## Reproduce

```sh
uv run --frozen --no-sync python scripts/scale/iex_split.py --date 2026-10-22 --buckets 1 \
    --url https://storage.googleapis.com/iex/data/feeds/20261022/20261022_IEXTP1_DPLS1.0.pcap.gz \
    --symbols SPY,QQQ,TSLA,NVDA,AAPL,F,SOFI,PLTR,ETSY,ROKU,CLOV,BMBL --out .scratch/s7/split
# ingest the bucket (qt.ingest, format iex_deep_plus, venue iex) to .scratch/s7/ds-iex, then:
uv run --frozen --no-sync python -I scripts/scale/findings/l3_fifo_gap/dump_mbo.py .scratch/s7/ds-iex .scratch/s7/bin-iex
cargo build --release --manifest-path scripts/scale/findings/l3_fifo_gap/Cargo.toml
# per symbol: <records.bin> <tick> <09:30 ET in ns> <16:00 ET in ns> <every> 42 <out.jsonl> <symbol>,
# every = max(1, records / 9000)
scripts/scale/findings/l3_fifo_gap/target/release/l3-fifo-gap ...
# per symbol, in the hftbacktest venv:
.scratch/hbt-venv/bin/python -I scripts/scale/findings/l3_fifo_gap/hbt_l3_run.py <records.bin> <tick> <out.jsonl> <hbt/SYMBOL.json>
python3 scripts/scale/findings/l3_fifo_gap/summarize.py <harness out dir> <hbt dir>
```

The Nasdaq run is the same from `queuetruth demo --yes --minutes 30` (its cached `subset.itch50.gz`, ingested with
`format: itch` and `session_date` 2019-12-30), with the window 09:35 to 10:05 ET. The datasets, the per-order
outputs and the hftbacktest runs hold prices and times from the source files and stay outside the repository
(`docs/data-and-licensing.md`); this page holds counts only.
