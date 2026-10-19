# Score my Nautilus backtest against my Binance fills

**Goal:** find out, order by order, whether your NautilusTrader backtest fills the orders that your live Binance
strategy filled, which way it is biased, where in the queue the disagreements are, and whether the extra fills are
the ones that lose money.

**You need:** your live Binance order and fill records for a period (one week or more is better than one day),
Binance L2 market data for the same period (for markouts and visible queue size; for example Tardis
`incremental_book_L2` and `trades` CSVs), and a Nautilus backtest that you can drive with a fixed order schedule.

The idea: replay your **live** orders, at their live times, through the backtester, then compare the two outcomes
for each order. Running your strategy logic in the backtest would compare two different sets of orders and tell you
little.

The page has two parts. Part 1 runs the whole workflow on synthetic data, so you can see every file and output
before you touch your own records. Part 2 is the same workflow on your Binance records and your Nautilus run.

## Part 1: the workflow on synthetic data

Run the blocks in order in one empty directory. They take about 40 seconds in total.

### 1. Data and a live log

`fixtures synth` writes the synthetic CME-style dataset of [Getting started](../getting-started.md). It also writes
`synth/own_orders/`: 200 real order lives of this tape, taken as if they were your own orders. `own_orders/live/` is
their canonical live log (venue `cme`, account `SYNTH`, `venue_order_id` = the MBO order id).

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --out synth
queuetruth live validate --live synth/own_orders/live
```

A canonical live log is a directory with `orders.parquet` (`qt.live_orders`, one row per order),
`events.parquet` (`qt.live_events`, one row per lifecycle event) and `manifest.json` (`qt.live_manifest`).
[Import fills from my venue or broker](import-live-logs.md) shows how to build one from venue records.

### 2. Score the exact replay (self-exclusion check)

The exact arm replays your live orders through queuetruth's own exact FIFO engine on the MBO tape. On an MBO venue
your live order is itself in the tape. Without care, its shadow would queue behind its own twin. The scorer removes
your own orders from the tape first (audit ruling A1, below).

<!-- check-docs: run -->
```sh
queuetruth score --live synth/own_orders/live --sim exact --data synth --out sc-exact
```

stderr shows the match first, for example `warning: self_exclusion: matched 200/200 live orders to MBO order ids
(100.0%; by id 200, heuristic 0)`: on this data every order matches by venue order id, because the synthetic log
stores the MBO order id there. Then stdout gives the answer first: a `RESULT:` line with the direction and size of the
fill-rate difference, its 95% interval and the cluster-robust p, the equivalence result, the order counts, the gate
status and the 2x2 table of orders by fill outcome. `--verbose` prints every statistic; `summary.json` and the report
hold them too.

The expected scorecard for this case is zero discordance, `two_by_two.live_only = 0` and `two_by_two.sim_only = 0`,
every live fill reproduced at the same event, and that is what the run shows. That is the A1 oracle O-SCORE-4
(`test_o_score_4_self_exclusion_real_orders` in `python/tests/score/test_score_gate.py`); the reference engine
checks the same property on this fixture in `a1_own_orders_self_excluded_exact_arm_has_zero_discordance`
(`crates/testkit/tests/audit.rs`) and `test_own_orders_excluded_reproduce_live_fills_exactly`
(`python/tests/oracle/test_oracle_fixtures.py`). With `--no-self-exclude` the shadow sits behind its twin and
live-only discordance is expected (`test_own_orders_not_excluded_show_the_self_twin_bias`).

### 3. Score a zoo model

The same live log against an L2 queue model from the [model zoo](../concepts/l2-model-zoo.md), run on the L2 view of
the same tape:

<!-- check-docs: run -->
```sh
queuetruth score --live synth/own_orders/live --sim-model "hbt_power3[n=2]" --data synth --out sc-power3
```

`sim_source.model_id` in the output is the full model id with its defaults filled in. The bias, the 2x2 table and
the tests are whatever this model does on this synthetic tape; they say nothing about any real venue.

### 4. Score a simulator's own output

This is the arm you will use for Nautilus: the simulator writes its orders and fills, queuetruth imports them into a
second canonical log and matches it to the live log by `order_key`.

The next block stands in for a backtest. It writes the two Nautilus report files that the importer reads
(`orders_report.csv` and `fills_report.csv`, columns as listed in step 5 of Part 2), copying the live fills, so this
"simulator" is perfect by construction. Each `client_order_id` is the live `order_key`, which is how the arms are
linked.

<!-- check-docs: run -->
```python
import csv
import pyarrow.parquet as pq

orders = pq.read_table("synth/own_orders/live/orders.parquet").to_pylist()
events = pq.read_table("synth/own_orders/live/events.parquet").to_pylist()
by_key = {}
for e in events:
    by_key.setdefault(e["order_key"], []).append(e)


def ns(t):
    return None if t is None else t.value


with open("orders_report.csv", "w", newline="") as fo, open("fills_report.csv", "w", newline="") as ff:
    wo, wf = csv.writer(fo), csv.writer(ff)
    wo.writerow(["client_order_id", "venue_order_id", "instrument_id", "side", "type", "quantity", "price",
                 "time_in_force", "post_only", "reduce_only", "status", "filled_qty", "avg_px", "ts_init", "ts_last"])
    wf.writerow(["client_order_id", "trade_id", "order_side", "last_qty", "last_px", "liquidity_side",
                 "commission", "ts_event", "instrument_id"])
    for o in orders:
        ev = by_key[o["order_key"]]
        fills = [e for e in ev if e["event_type"] == "fill"]
        filled = sum(e["qty"] for e in fills)
        ack = next(e for e in ev if e["event_type"] == "ack")
        if any(e["event_type"] == "cancel_ack" for e in ev):
            status = "CANCELED"
        elif filled == o["orig_qty"]:
            status = "FILLED"
        else:
            status = "ACCEPTED"
        inst = o["instrument"] + ".SIM"
        wo.writerow([o["order_key"], o["venue_order_id"], inst, o["side"].upper(), "LIMIT", o["orig_qty"],
                     o["px_str"], "GTC", False, False, status, filled, o["px_str"] if filled else "",
                     ns(ack["ts_exchange"]), ns(ev[-1]["ts_exchange"])])
        for e in fills:
            wf.writerow([o["order_key"], e["fill_id"], o["side"].upper(), e["qty"], o["px_str"], "MAKER", "",
                         ns(e["ts_exchange"]), inst])
```

Import the two files with the simulator importer and score them. From the command line the same import is
`queuetruth live import --venue sim-nautilus --account SYNTH --option size_exp=0 --in orders_report.csv
--in fills_report.csv --out nt-sim`; from Python:

<!-- check-docs: run -->
```python
from queuetruth import live

summary = live.import_sim("nautilus", ["orders_report.csv", "fills_report.csv"], "nt-sim",
                          account="SYNTH", size_exp=0)
print(summary.venue, summary.n_orders, summary.n_fills, summary.validation_status)
```

<!-- check-docs: run -->
```sh
queuetruth score --live synth/own_orders/live --sim-log nt-sim --data synth --out sc-nt
```

Because the report files copy the live fills, every order lands in `both` or `neither`: `live_only` and
`sim_only` are 0, `n_unmatched_live` and `n_unmatched_sim` are 0, and `sim_source.kind` is `external`. With a real
backtest these counts are what you came for.

### 5. The report

<!-- check-docs: run -->
```sh
queuetruth report --in sc-exact --in sc-power3 --in sc-nt --out score.html
```

One HTML file, no network access needed to view it. "Reading the result" below says what each section shows.

### 6. A gate for CI

`--fail-abs-bias X` makes `score` exit 1 when the absolute fill-rate bias exceeds `X` (a fraction: 0.02 is 2 pp), and
`--fail-not-equivalent` when the fill rate is not shown to be within the equivalence margin (default 2 pp). On this
data the zoo model's bias is small but not shown to be within 2 pp of live, so the equivalence gate fails:

<!-- check-docs: run exit=1 -->
```sh
queuetruth score --live synth/own_orders/live --sim-model "hbt_power3[n=2]" --data synth --out sc-gate \
    --fail-abs-bias 0.05 --fail-not-equivalent
```

The summary is still written; `verdict.gates_passed` is `false` and `verdict.failed_gates` names the failed gate. Exit codes: 0 pass, 1 failed gate (`QT-GATE-001`), 2 any other error
(`test_gates` in `python/tests/score/test_score_statistics.py` covers the verdict).

## Part 2: your Binance fills and your Nautilus backtest

### 1. Build the canonical live log

For Binance the importer reads captured user-data stream `executionReport` messages (JSON lines, optionally wrapped
as `{"recv_ns": <your receive time in ns>, "msg": <raw message>}` to keep local receive times), WebSocket API
request and response captures, and REST `myTrades` and `allOrders` history. Details for every venue are in
[Import fills from my venue or broker](import-live-logs.md).

`instrument_map.csv` gives each symbol its fixed-point scales. Use the same `instrument_id` as the market-data
dataset of step 3 (read it from `bn-l2/instruments.parquet`):

```text
venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size
binance,BTCUSDT,1,,-9,-8,0.01,0.00001
```

<!-- check-docs: skip (needs your Binance records) -->
```sh
queuetruth live import --venue binance --account main --instrument-map instrument_map.csv \
    --in user_stream_2026-10-05_11.jsonl --in mytrades_2026-10-05_11.json --out live/
queuetruth live validate --live live/
```

Decimal prices and sizes convert exactly or fail with `QT-INPUT-004`. Fills deduplicate on (instrument, fill id);
the Binance fill id is the trade id `t`. `ts_exchange` is the match time `T` (kind `match_time`). The importers
are tested against synthetic fixtures written from Binance's public documentation (O-LIVE-1), not against real
account exports, so every log is labelled `validation_status = fixture_only`. Check the import summary's counts
against your own records the first time.

Do not import from a fills-only source (for example `myTrades` alone) for scoring: never-filled orders are missing,
so the scorer reports fill-rate bias and McNemar as not computable (see "Fills-only logs" below).

### 2. Turn the live log into a fixed schedule

<!-- check-docs: skip (needs your live log) -->
```sh
queuetruth shadows from-live --live live/ --out schedule.parquet
```

`schedule.parquet` (`qt.shadow_spec`) has one row per order leg: `shadow_id`, `instrument_id`, `side`, `price`,
`qty`, `ts_submit` (by default the exchange time of the acknowledgement), `ts_cancel` and `order_key`. Only resting
limit orders are scheduled (tif `gtc`, `day`, `post_only` or `gtx`, with a limit price and a mapped
`instrument_id`); market, IOC and FOK orders and orders without a usable entry time are excluded and counted. The
first leg's `shadow_id` is the `order_key`. A price change, a size increase or a priority-losing decrease starts a
new leg named `<order_key>.leg<k>` (k = 2, 3, ...). A size decrease that keeps priority stays on its leg as an amend
row (`qt.shadow_amend`) in `schedule_amends.parquet` next to the schedule, written only when there is one.

`--entry send-plus-latency --latency-ns D` starts each order at its local send time plus a latency instead. The
default isolates fill logic from entry latency.

### 3. Ingest the market data

<!-- check-docs: skip (needs Tardis files) -->
```sh
queuetruth ingest --format tardis-l2 --venue generic --source-venue binance \
    --in binance-futures_incremental_book_L2_2026-10-05_BTCUSDT.csv.gz \
    --in binance-futures_trades_2026-10-05_BTCUSDT.csv.gz --out bn-l2
```

This is an L2-only dataset. It is enough for markouts, visible queue size at entry and zoo models, never for
exact truth (Binance publishes no order-level feed), so `--sim exact` refuses it.

### 4. Run the schedule through Nautilus

`export replay-schedule` writes a NautilusTrader strategy that ignores market data and replays your live schedule:
leg 1 of each live order is a limit order submitted at its time (post-only when the live order was), later legs and
priority-keeping amends are `modify_order` calls, and the last leg's cancel is a `cancel_order`. It records the order
events it receives as JSON lines keyed by the live `order_key`, so Nautilus's own client order ids stay internal.

<!-- check-docs: skip (needs your live log and dataset) -->
```sh
queuetruth export replay-schedule --target nautilus --live live/ --data bn-l2 --out qt_replay_strategy.py
```

The file's docstring shows how to use it: add `ScheduleReplayStrategy(ScheduleReplayConfig(events_path=...))` to
your `BacktestEngine` (with your venue, data, fill model and latency model already configured), run the engine, and
the strategy writes the events file when it stops. Map the schedule's instrument ids to Nautilus instrument ids with
`ScheduleReplayConfig(instrument_ids={"1": "BTCUSDT-PERP.BINANCE"})`. The strategy is generated against
NautilusTrader's documented API at a pinned commit and is not run in queuetruth's tests (no NautilusTrader code is
used); check the names against your installed version. `--target hftbacktest` writes the equivalent replay script for
hftbacktest 2.4.4, which the tests do run end to end.

If you drive Nautilus yourself instead, give each order the schedule row's `shadow_id` as its client order id: the
importer turns `<venue>:<account>:<id>` into the `order_key` and strips a `.leg<k>` suffix; any other id becomes
`sim:nautilus:<account>:<client order id>`, matches no live order and is counted in `n_unmatched_sim`. Then write
Nautilus's orders and fills reports to files (CSV, Parquet or JSON lines).

### 5. Import the backtest output

`queuetruth.live.import_sim("nautilus", inputs, out, ...)` (or `queuetruth live import --venue sim-nautilus`) reads
the replay strategy's events file, or three kinds of report rows, recognised per row (code:
`python/queuetruth/live/importers/sim.py`):

| Rows | Columns read | What they become |
|---|---|---|
| Order report rows (a `status` column and `quantity` or `filled_qty`) | `client_order_id`, `venue_order_id`, `instrument_id`, `side`, `type`, `quantity`, `price`, `time_in_force`, `post_only`, `reduce_only`, `status`, `filled_qty`, `avg_px`, `ts_init`, `ts_last` | The order record; an `ack` at `ts_init` (unless `REJECTED` or `DENIED`); a terminal event at `ts_last` for `CANCELED`, `EXPIRED`, `REJECTED`, `DENIED`. Both are history records (kind `record_time`), dropped when an event row of the same type exists. |
| Fill rows (a `last_qty` column, or event type `OrderFilled`) | `client_order_id`, `trade_id`, `order_side`, `last_qty`, `last_px`, `liquidity_side`, `commission` (`"0.60 USDT"` splits into fee and currency), `commission_currency`, `ts_event` | One `fill` per row, `fill_id` = `trade_id`, kind `match_time`, liquidity `MAKER` / `TAKER` (else `unknown`). |
| Order event rows (a `type`, `event_type` or `kind` column, or a file name, naming the event) | `client_order_id`, `ts_event`, `reason`, `due_post_only`, and `quantity` / `price` on `OrderUpdated` | `OrderSubmitted` -> `submit_sent` (local send time), `OrderAccepted` -> `ack`, `OrderRejected` / `OrderDenied` -> `reject` (`post_only_reject` with `due_post_only`), `OrderCanceled` -> `cancel_ack`, `OrderExpired` -> `expire`, `OrderUpdated` -> `amend_ack`, `OrderCancelRejected` -> `cancel_reject`, `OrderModifyRejected` -> `amend_reject`; `OrderInitialized` gives the order record. |

`instrument_id` `SYMBOL.VENUE` becomes instrument `SYMBOL`. An order with `filled_qty > 0` and no fill rows gets one
derived fill of `filled_qty` at `avg_px` at `ts_last` (`derived = true`, liquidity `unknown`, and a warning): that
loses partial-fill timing, so export the fills report as well.

<!-- check-docs: skip (needs your Nautilus reports) -->
```python
from queuetruth import live

summary = live.import_sim(
    "nautilus",
    ["nt_orders_report.csv", "nt_fills_report.csv"],
    "nt-sim",
    account="main",
    instrument_map="instrument_map.csv",
    venue="binance",
)
print(summary.n_orders, summary.n_fills, summary.warnings)
```

`venue="binance"` makes the importer look up the instrument map rows of the live venue, so both logs use the same
`instrument_id`, `price_exp` and `size_exp`. Other options: `size_exp` (default -8 without a map row),
`key_from_client_id=False` (always use the `sim:nautilus:...` key), `append=True`; on the command line pass them as
`--option KEY=VALUE`. The written log has `venue = sim:nautilus`, `source = sim` and
`validation_status = simulated`. `import_sim("hftbacktest", ...)` (`--venue sim-hftbacktest`) does the same for the
hftbacktest replay script's order snapshots.

If you would rather convert the reports yourself, `queuetruth.live.write_canonical_log(out, orders, events)` writes
a validated canonical log directory from rows with the canonical column names (see
[`qt.live_orders`](../reference/schemas.md#qtlive_orders) and [`qt.live_events`](../reference/schemas.md#qtlive_events)).

### 6. Score and report

<!-- check-docs: skip (needs your data) -->
```sh
queuetruth score --live live/ --sim-log nt-sim/ --data bn-l2 --out sc-nautilus
queuetruth report --in sc-nautilus --out sc-nautilus.html
```

### 7. Try other fill models on the same schedule

The same live log can be scored against any zoo model on the Binance L2 data, without running Nautilus:

<!-- check-docs: skip (needs your data) -->
```sh
for m in nt_queue_l2 "nt_prob_touch[p=0.3]" "hbt_power3[n=3]" hbt_risk_adverse; do
  queuetruth score --live live/ --sim-model "$m" --data bn-l2 --out "sc-$m"
done
```

`nt_queue_l2` in faithful mode reproduces NautilusTrader 1.231.0's L2 queue position (`queue_position=True`) on an
`OrderBookDelta` feed: on the Nasdaq days it fills the same orders as the real `BacktestEngine` at every horizon
checked ([Results](../RESULTS.md#nautilustrader-cross-check)). On an `OrderBookDepth10` feed Nautilus resets queue
position at every snapshot, so compare with `nt_prob_touch[p=1]` instead. If the matching zoo model agrees with your
Nautilus run, the gap to live is the model's, not your configuration's.

### 8. Put it in CI

<!-- check-docs: skip (needs your data) -->
```sh
queuetruth score --live live/ --sim-log nt-sim/ --data bn-l2 --out sc \
    --fail-abs-bias 0.02 --fail-not-equivalent
```

Exit code 1 means the backtest's fill rate is more than 2 pp from live, or is not shown to be within the
equivalence margin (`--fill-rate-margin`, default 0.02). Few discordant orders do not count as agreement:
equivalence needs the 90% bootstrap CI inside the margin (TOST, `test_tost_equivalence`).

## What the scorer does

These rules come from the independent audit ([Rules from the independent audit](../concepts/audit-rules.md)); each
is covered by a test in `python/tests/score/test_score_scorecard.py` (with an injected engine) unless noted.

- **Own-order exclusion (A1), `--sim exact` and zoo arms on MBO data.** Before the twin is injected, each scheduled
  live order is matched to its MBO order id: first by `venue_order_id` (an `A` record of the instrument with that
  order id), else by a heuristic, the unmatched `A` record with the same side, price and size closest to the entry
  time within `--match-window-ns` (default 1 ms). The matched orders' records are removed from the replayed tape.
  The match rate is reported in the warnings, with an extra warning below 95%: unmatched orders stay in the tape and
  their twins may sit behind them. `--no-self-exclude` turns this off. Tests: `test_a1_self_exclusion_by_venue_order_id`,
  `test_a1_self_exclusion_heuristic_match`, O-SCORE-4. On an L2-only tape there are no order ids: the queue models run on
  a copy of the dataset with your own resting orders removed from the visible depth (aligned within
  `--match-window-ns`); if that removal is not applied, the scorecard is blocked (failed gate
  `own_orders_in_l2_depth`, exit 1, a `BLOCKED:` headline).
- **Sim legs follow live instructions (A8).** A re-priced or upsized leg is sized from the instruction (the
  `amend_ack` or `amend_sent` quantity), never from quantities derived from live fills; legs aggregate to one order
  per `order_key`. Test: `test_a8_legs_follow_instructions_and_aggregate`.
- **Same evaluation window in both arms (A9).** Each order is evaluated on W = [entry, min(live cancel instruction,
  entry + H, end of data)], H = `--window` (default 60 s). The cancel instruction is the live `cancel_sent` local
  time, else the `cancel_ack` exchange time. Fills outside W are ignored in both arms. Test:
  `test_a9_window_ignores_late_fills_in_both_arms`.
- **Taker fills reported separately (A5).** A fill flagged `liquidity = taker` (the book crossed the order) does not
  count as a maker fill: it is excluded from the fill rate and the maker markouts, counted in the warning
  `taker_fills: live X, sim Y`, and shown separately in the report. Fills with `unknown` liquidity count as maker.
  Tests: `test_a5_crossed_sim_fill_is_taker`, `test_taker_fills_excluded_from_maker_markouts`
  (`test_score_statistics.py`).
- **Clustered discordance test is primary (A10).** Orders on the same day are not independent. The primary test is
  cluster-robust: the Obuchowski cluster-adjusted McNemar test with 5 or more clusters, else a block bootstrap of
  (c - b) / N. Clusters are session dates; with fewer than 5 dates the scorer uses 30-minute blocks and warns
  `ci_block=30min`. The plain exact McNemar p is reported as secondary with the note "assumes independent orders".
  Primary tests are Holm-adjusted, secondary tests BH (or `--fdr by`) adjusted. Tests: `test_o_stat_1_clustered_*`
  (`python/tests/stats/test_clustered.py`), `test_holm_primary_and_bh_secondary_families`.

### Fills-only logs

A log built only from fill lists has no never-filled orders. The scorer detects it (every event is a fill, bust or
correction, or the manifest or orders mark `fills_only`) and refuses the statistics that need unfilled orders:
fill-rate bias, McNemar, the strata tests, equivalence and the CIF tests are null, the headline says
`not computable: fills-only source`, and a `--fail-abs-bias` or `--fail-not-equivalent` gate fails. Markouts and the
time-to-fill comparison conditional on fill are still computed. Tests: `test_fills_only_source`,
`test_fills_only_not_computable_but_ttf_and_markouts_computed`.

## Reading the result

`score` writes `summary.json` (`qt.scorecard_summary`, fields in
[the schema reference](../reference/schemas.md#qtscorecard_summary)) and five tables: `score_orders.parquet` (one
row per matched order, with `class` = `both`, `neither`, `live_only` or `sim_only`), `score_fills.parquet`,
`score_markouts.parquet`, `score_position.parquet`, `score_tests.parquet`, plus the instruction-sized
`schedule.parquet`.

The headline (`verdict.headline`) states the direction and size of the fill-rate difference within the window with
its 95% interval, the equivalence result, the exact McNemar p with the cluster-robust p and cluster count, the
number of orders and the gate status. On the synthetic exact arm above it reads:

```text
no significant fill-rate difference within 60 s: +0.0 pp (95% CI [+0.0, +0.0]); equivalent within ±2 pp (TOST p = 0); McNemar p = 1 (cluster-robust p = 1, 26 clusters); 200 orders; no gates configured
```

The cluster-robust test is also in the summary field `discordance_clustered` (method, estimate, interval, p,
clusters, design effect). Read the summary and the report in this order:

1. **Counts.** `n_orders` is the live log; `n_matched` the scored orders; `n_unmatched_live` scheduled orders with
   no simulator outcome; `n_unmatched_sim` simulator orders that match no scheduled live order. Many unmatched
   orders mean the schedule replay or the client order ids are off. `n_overlap_same_px` counts live orders that
   rested at the same price and side as another of yours.
2. **Data quality and warnings**: the self-exclusion match rate, `not_scheduled`, `taker_fills`, `clusters`,
   `ci_block=30min`.
3. **2x2 table and signed bias.** `sim_only` orders are fills your backtest gave you that the market did not;
   `live_only` are the reverse. `bias.fill_rate_bias = (sim_only - live_only) / n_matched` with a block-bootstrap
   95% CI; `bias.direction` is `sim_overfills` or `sim_underfills` only when the CI excludes 0. The verdict cards
   show the cluster-robust primary test next to the secondary McNemar p.
4. **Discordance by queue decile at entry** (`by_queue_decile`; exact queue ahead on MBO data, visible level size at
   entry on L2 data) and by liquidity tercile (`by_liquidity`, traded volume in the 60 s before entry). Sim-only
   fills concentrated in the deep deciles mean the fill model ignores queue position.
5. **Markouts** (`markouts`, `markout_diff`, `sim_only_vs_live_only_markout`; default horizons 50 ms to 300 s,
   reference `mid`, `--markout-refs mid,microprice` adds the microprice). If sim-only fills mark out worse than
   live-only fills, the backtest adds fills at the moments the market would have picked you off, and its PnL is
   optimistic by more than the fill count suggests.
6. **Time to fill** (`ttf`: median log ratio over jointly filled orders, Aalen-Johansen CIF per arm, paired
   permutation test) and the **position over time** per instrument.
7. **Why traces** for up to `--max-why` (default 20) disagreeing orders, sim-only first. See
   [Explain one fill](explain-one-fill.md).

## Caveats

- Your live orders changed the book; the replayed orders in the backtest may not see the same reactions.
  `n_overlap_same_px` counts the orders most exposed to this.
- Binance's depth stream is conflated even at its fastest setting, so any L2 backtest on it works with less
  information than the exchange had.
- `send-plus-latency` entry assumes your clock was synchronized with Binance's.
- The zoo arm applies priority-keeping amends (`zoo --amends`) and your own-order exclusion like the exact arm.
- Live data and fills stay on your machine. Nothing is uploaded unless you run the hosted service and submit a job
  yourself.
