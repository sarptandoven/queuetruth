# Explain one fill

**Goal:** answer "why did this order fill (or not) at that moment?" for one shadow order or one of your live
orders, record by record: which orders were ahead of it, what removed them, and which event filled it.

**You need:** an MBO dataset (queue position needs order-level data) and either a shadow schedule
(`qt.shadow_spec`) or a canonical live log.

`queuetruth why` replays the order's instrument-day with that one order injected into the exact engine and writes a
`qt.why_trace`. It is the same engine and the same rules as `queuetruth replay`.

## A shadow order

Make some shadows on the synthetic dataset and replay them, so you can pick one that filled:

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --out synth
queuetruth shadows sample --data synth --n 20 --seed 1 --out shadows.parquet
queuetruth replay --data synth --shadows shadows.parquet --out r
```

`r/shadow_results.parquet` has one row per shadow with `end_state`, `filled_qty`, `ts_first_fill` and
`first_fill_reason`. Take the first shadow that filled and trace it:

<!-- check-docs: run -->
```sh
sid=$(python -c "
import pyarrow.parquet as pq
rows = pq.read_table('r/shadow_results.parquet').to_pylist()
print(next(r['shadow_id'] for r in rows if r['filled_qty'] > 0))
")
echo "tracing $sid"
queuetruth why --data synth --shadows shadows.parquet --shadow-id "$sid" --out why.json
```

The terminal shows the order and its outcome first, then the event table (time, action, order id, side, price, size,
effect, whether the touched order was ahead, queue ahead, rank, level size, fill, note; the fill rows are marked), and
ends with a one-sentence `conclusion`, for example "Filled 1 of 1 at ...: the opposite side crossed your price (a
taker fill). Queue ahead went 12 -> 0 over 10.9 s (12 cancelled)." `--out` writes the whole document;
`--output-format json` prints it to stdout instead. `--max-events N` caps the event rows (default 5,000); when the cap is hit, `truncated` is true.

## Reading the trace

Print the event rows as a table:

<!-- check-docs: run -->
```python
import json

t = json.load(open("why.json"))
print(t["subject"], t["side"], t["price"], t["qty"], t["outcome"])
print(f"{'ts_event':>20} {'act':>3} {'order_id':>14} {'size':>5} {'effect':>7} {'ahead':>5} "
      f"{'q_ahead':>7} {'rank':>4} {'level':>5} {'fill':>4}  note")
for e in t["events"]:
    print(f"{e['ts_event']:>20} {e['action']:>3} {e['order_id']:>14} {e['size']:>5} {e['effect']:>7} "
          f"{str(e['is_ahead']):>5} {e['queue_ahead_after']:>7} {e['rank_after']:>4} {e['level_qty_after']:>5} "
          f"{e['fill_qty']:>4}  {e['note'] or ''}")
```

The document (`qt.why_trace`, [schema](../reference/schemas.md#qtwhy_trace)):

| Field | Meaning |
|---|---|
| `subject.shadow_id`, `subject.order_key` | The traced shadow; `order_key` is set when the trace comes from a live order. |
| `instrument_id`, `side` (`B` / `A`), `price`, `qty` | The order. `price` is the resolved price (a touch-priced shadow gets its price at activation); null if it never resolved. |
| `ts_live` | When the order became live in the book; null if it never did. |
| `outcome.filled_qty`, `outcome.ts_first_fill`, `outcome.end_state` | Same values as the order's `qt.shadow_results` row (`filled`, `cancelled`, `rejected_would_cross`, `censored_clear`, ...). |
| `events` | One row per relevant record while the order is live. |
| `truncated` | True when `--max-events` cut the rows. |

Each event row is one MBO record of the instrument that is relevant to the order: a record of an order ahead of
it, a record at its price on either side, a change at its level, or a trade through its price. Records before
activation are not shown.

| Column | Meaning |
|---|---|
| `ts_event`, `sequence` | The record's exchange time and sequence number. |
| `action` | `A` add, `C` cancel, `M` modify, `R` clear, `T` trade print, `F` fill of a real order, `N` none. |
| `order_id`, `side`, `price`, `size` | The record's fields (`order_id` 0 for prints). |
| `effect` | What the record did to the book: `add`, `remove`, `reduce`, `replace`, `clear`, `none`. |
| `is_ahead` | Whether the record's order was ahead of the traced order in the queue. |
| `queue_ahead_after` | Exact quantity ahead of the order after the record's event. |
| `rank_after` | Number of orders ahead after the event. |
| `level_qty_after` | Total displayed quantity at the order's price after the event. |
| `fill_qty` | Quantity the order filled in this event, on the event's last row. |
| `note` | `fill <qty> <reason>` on the row where the order filled; reasons as in `qt.shadow_fills`: `queue_depleted`, `trade_through`, `crossed`, `crossed_at_live`. |

How to read it: queue ahead only shrinks through events on the specific orders ahead (`is_ahead = true` rows with
effect `remove` or `reduce`), never through prints alone (audit ruling A4). The order fills when an execution
reaches it once the queue ahead is 0 (`queue_depleted`), when a real order resting at a worse price on its side
executes (`trade_through`), or when the opposite side moves through its price (`crossed`, a taker fill, ruling A5).
A fill at activation is one synthetic row with action `N`, `order_id` 0 and note `fill <qty> crossed_at_live`. See
[Queue ahead](../concepts/queue-ahead.md) and [Rules from the independent audit](../concepts/audit-rules.md).

`why` replays the order alone with the same engine, so for a shadow without amends its outcome equals that
shadow's `qt.shadow_results` row from `replay` (`why_and_session_on_synth_small` in `crates/replay/tests/gate.rs`;
the event rows themselves are checked on a hand-built scenario in `why_trace_builder_on_s1`,
`crates/replay/tests/misc.rs`). v0.1 `why` does not apply shadow amends and replays without feed or gateway
latency, so compare it with a replay run without `--latency`.

## Compare with a queue model

`--model ID` adds a zoo model's queue estimate next to the exact queue on every event, and the trace names the
first event where the two diverge (`first_divergence`; by default a gap of 10% of the order's size, at least one
size unit). The model's estimate comes from a one-shadow zoo run, or from an existing zoo output directory with
`--zoo DIR` (run with `--write-trajectory`).

<!-- check-docs: run -->
```sh
sid=$(python -c "
import pyarrow.parquet as pq
rows = pq.read_table('r/shadow_results.parquet').to_pylist()
print(next(r['shadow_id'] for r in rows if r['filled_qty'] > 0))
")
queuetruth why --data synth --shadows shadows.parquet --shadow-id "$sid" --model "hbt_power3[n=2]" --out why-model.json
python -c "
import json
t = json.load(open('why-model.json'))
print(t['model_id'], t['first_divergence'])
print(t['conclusion'])
"
```

From Python the same is `queuetruth.api.why_explain(spec, model=..., zoo=...)`. On an L2-only dataset there is no
exact queue: `why` fails with `QT-CONFIG-001`, and its hint gives a command that works there (the model's queue
trajectory from `zoo --write-trajectory`).

## One of your live orders

Give the live log and an `order_key` instead of a shadow file. `why` first builds the order's schedule from the
live log (entry = the exchange time of the acknowledgement, as in `queuetruth shadows from-live`) and traces its
first leg. Here the key is the first order of the synthetic live log that has a fill:

<!-- check-docs: run -->
```sh
key=$(python -c "
import pyarrow.parquet as pq
ev = pq.read_table('synth/own_orders/live/events.parquet').to_pylist()
print(sorted({e['order_key'] for e in ev if e['event_type'] == 'fill'})[0])
")
echo "tracing $key"
queuetruth why --data synth --live synth/own_orders/live --order-key "$key" --max-events 200 --out why-live.json
```

`subject.shadow_id` is the leg id (the `order_key` for the first leg) and `subject.order_key` is the key you gave.
A key with no schedulable leg (market, IOC or FOK orders, or no entry time) fails with `QT-CONFIG-001`
(`test_shadows_from_live_and_live_why` in `python/tests/api/test_api_native.py`).

On an MBO venue your live order is itself in the tape. Like the scorer, `why --live` removes the order's own records
from the tape before it traces the twin (audit ruling A1, see
[Score my Nautilus backtest](score-nautilus-vs-binance.md#what-the-scorer-does)), so the trace shows what your order
saw in its own place in the queue.

## From Python

`qt.why(spec)` takes the same fields as the CLI (`dataset`, `shadows` + `shadow_id`, or `live` + `order_key`,
`max_events`, `out`) and returns a `WhyTrace` model:

<!-- check-docs: run -->
```python
import json

import pyarrow.parquet as pq

import queuetruth as qt

rows = pq.read_table("r/shadow_results.parquet").to_pylist()
sid = next(r["shadow_id"] for r in rows if r["filled_qty"] > 0)
t = qt.why({"dataset": {"path": "synth"}, "shadows": {"path": "shadows.parquet"}, "shadow_id": sid})
print(t.outcome.end_state, t.outcome.filled_qty, len(t.events), t.truncated)
fills = [e for e in t.events if e.fill_qty > 0]
print([(e.ts_event, e.fill_qty, e.note) for e in fills])
assert t.outcome.filled_qty == sum(e.fill_qty for e in t.events)
assert json.load(open("why.json"))["outcome"] == t.outcome.model_dump()
```

## From an agent

The MCP server (`queuetruth mcp serve --local`) has the read-only tool `qt_local_why` with arguments
`dataset_path`, `shadows_path` and `shadow_id`; it returns the same `qt.why_trace` document. It traces shadows
only: for a live order, build its schedule first with `queuetruth shadows from-live` and pass the leg's
`shadow_id`. See [Use queuetruth from an agent](use-from-an-agent.md) and the [MCP reference](../reference/mcp.md).

## In the report

`queuetruth report` adds a "Why" section with traces for up to `--max-why` orders (default 20). For a scorecard it
picks the orders the arms disagree on, sim-only first, then live-only, then jointly filled orders with the largest
time-to-fill difference; for a zoo run, the shadows with the largest model-versus-exact error. Each trace shows the
order's facts, an event table (long traces are cut to their first and last rows) and a step chart of the exact
queue ahead over time, with the model's queue-ahead estimate next to it when the zoo run wrote
`zoo_trajectory.parquet` (`queuetruth zoo --write-trajectory`).

The events come from, in this order: a `qt.why_trace` JSON file in one of the `--in` directories, the exact engine
(the same as `queuetruth why`) when the run's dataset and live log or shadow file resolve from the current
directory, the exact queue trajectory in the inputs, or else a note with the `queuetruth why` command to run.

<!-- check-docs: run -->
```sh
queuetruth score --live synth/own_orders/live --sim-model "hbt_power3[n=2]" --data synth --out sc
queuetruth report --in sc --max-why 3 --out why-report.html
```
