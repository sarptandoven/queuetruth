# Shadow orders

A shadow order is a passive limit order you did not send but want to evaluate: instrument, side, price, quantity
and send time. The exact engine places it in the real queue of an order-level replay and records when, why and how
much of it would have filled. This page states the fill rule the engine uses by default (`Exact`). The rule
replaced an earlier one after the independent audit; [Rules from the independent audit](audit-rules.md) lists what
changed and why.

## Inputs

One row per shadow (`qt.shadow_spec`, Parquet; a CSV with header
`shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns,ts_cancel` is also read):

| Field | Meaning |
|---|---|
| `shadow_id` | Your id, unique. 1 to 128 characters of `[A-Za-z0-9_.:-]`. |
| `instrument_id`, `side` | The instrument and `B` (bid) or `A` (ask). |
| `price_mode` | `abs`: use `price`. `touch`: resolve against the same-side best at activation. |
| `price` / `offset_ticks` | Absolute fixed-point price, or ticks from the touch: `0` joins the best, `-1` is one tick behind it, `+1` one tick inside the spread. |
| `qty` | Size in size units. |
| `ts_submit` | When you sent it, ns since the epoch, UTC. |
| `latency_ns` | Extra delay for this order. |
| `ts_cancel` | Optional cancel send time. |

Optional amends (`qt.shadow_amend`: `shadow_id, ts_send, new_qty`) are priority-keeping size reductions. A price
change or a size increase is not an amend: it is a cancel plus a new shadow leg.

You can write these tables yourself, sample them (`shadows sample`), mirror real orders (`shadows mirror-real`) or
derive them from your live order log (`shadows from-live`). Full column lists are in
[Schemas](../reference/schemas.md#qtshadow_spec).

## Latency

A replay-wide latency model has four parts: `feed_ns`, `decision_ns`, `gateway_ns`, `jitter_ns`
(`--latency F,D,G` and `--jitter-ns` on the command line).

```text
submit delay  = latency_ns + feed_ns + decision_ns + gateway_ns + J_submit
cancel delay  = latency_ns + gateway_ns + J_cancel
amend delay_i = latency_ns + gateway_ns + J_amend_i
ts_live       = ts_submit + submit delay
```

Each `J` is drawn uniformly from `[0, jitter_ns]` from a random stream seeded by the run seed and the `shadow_id`,
so results do not depend on processing order or thread count.

## Activation

The shadow goes live at the **start of the first event with `ts_event > ts_live`** (strictly later), against the
book as the previous event left it. If its cancel takes effect before that, it never goes live
(`end_state = cancelled`).

At activation:

- **Price.** `abs` uses the given price. `touch` takes the same-side best `B`: bid price `B + offset_ticks * tick`,
  ask price `B - offset_ticks * tick`. No same-side best: `rejected_no_touch`. A positive offset that would lock or
  cross the opposite side: `rejected_would_cross`.
- **Queue ahead.** Every real order resting at that side and price is ahead of the shadow. The engine keeps the set
  of those order ids and their sizes. Queue ahead is their total displayed size; rank is their count.
- **Marketable on arrival.** If the opposite best is at or through the price, the shadow is the arriving order, so it
  fills as a **taker**: it walks the opposite displayed orders at or through its price, best level first and FIFO
  within a level, one fill row per level at that level's price, reason `crossed_at_live`. Real same-side orders with
  priority (a better price, or the same price and ahead of it) are matched first. Any remainder rests with the full
  queue ahead.

## How queue ahead changes

Queue ahead is maintained from the order-level records of the specific orders ahead, never from trade prints:

- a cancel or reduction of an order ahead reduces queue ahead by that size;
- an execution of an order ahead (its `F` record) reduces queue ahead by the executed size, and the removal that
  follows is not counted a second time;
- a modify that loses priority, a replace, or a duplicate add moves the order behind the shadow: its whole size
  leaves queue ahead;
- orders that join the level after the shadow are behind it and never affect it.

Trade prints (`T` records) do not move queue ahead. They are recorded in the trajectory as `traded_at_px_qty` for
the truth decomposition only. This matters where prints and executions disagree: ITCH executions marked
non-printable, and executions at a price other than the display price.

## How a shadow fills

At the end of each event, in this order:

1. **Executions behind the shadow** (`queue_depleted`). An execution of a real order resting behind the shadow at
   its price fills the shadow by that volume, up to its remaining quantity. Under price-time priority this can only
   happen once everything ahead has gone; if it happens while queue ahead is still positive, the event is counted as
   a priority anomaly (`behind_exec_with_ahead` in the summary: `shadow_diagnostics` from schema 1.1, a warnings line before).
2. **Hidden prints and excluded own orders** (`queue_depleted`), once the displayed queue ahead is zero: see
   [hidden prints](#hidden-prints) and the scorer's [own-order exclusion](scorecard.md#the-exact-arm-and-your-own-orders).
3. **Trade-throughs** (`trade_through`). An execution of a real same-side order resting at a price strictly worse
   than the shadow's (for a bid shadow, a bid below it) means the aggressor went through the shadow's price, so the
   shadow fills by that volume, up to its remaining quantity.
4. **Crossed book** (`crossed`). If the opposite best is at or through the price after the event, the shadow fills as
   a taker by walking the opposite displayed orders at or through its price, never taking the same opposite size
   twice. Real same-side orders with priority over the shadow are matched first, as on arrival. If no opposite size
   is left for the shadow, nothing fills and the event is counted (`crossed_unfilled`).

Each fill is one row in `shadow_fills.parquet`: event time and sequence, fill quantity, cumulative and leaves
quantity, fill price, reason, queue ahead before the event, at-price print volume and through-price volume in the
event, and the volume executed that event against orders behind the shadow. Partial fills are separate rows and the
sum of `fill_qty` equals `filled_qty` (O-SH-3). Fills with reason `crossed` or `crossed_at_live` are taker fills;
the scorer and the report keep them out of maker fill-rate and markout statistics and show them separately.

## Hidden prints

On venues with hidden orders (Nasdaq; the rule `supports_hidden_orders`), an execution of non-displayed interest
prints as a `T` record with side `N` and no order-level records. Displayed interest has priority over hidden
interest at the same price, so a hidden print at price `p` is used only when the book at that moment shows no
displayed interest at or through `p` on either side (no bid at or above `p`, no ask at or below `p`) and `p` is on
the tick grid. Otherwise the print is ignored, and counted (`hidden_at_px_with_display_ahead`) when a shadow rests
at `p`. A usable print reaches every shadow priced at or better than `p`:

- at the shadow's price it is volume behind the displayed queue, and fills the shadow once its queue ahead is zero
  (`queue_depleted`);
- strictly through the shadow's price it is a trade-through (`trade_through`).

This policy (`behind-and-through`) is the default on Nasdaq; on other venues hidden prints never fill shadows.
`replay --hidden-print-policy ignore|behind|behind-and-through` overrides the default (`behind` keeps only the
at-price part). The
summary reports `hidden_print_fills`. On the public Nasdaq days, the hidden-print rule can only be verified from raw
ITCH files, because the earlier engine's CSV conversion merges hidden and cross prints; see
[How queuetruth is tested](../testing.md#real-data-checks).

## Cancels and amends

A cancel sent at `ts_cancel` takes effect at `c = ts_cancel + cancel delay`. The shadow ends `cancelled` at the start
of the first event with `ts_event > c`; events at or before `c` still apply, so a fill while the cancel is in
flight stands. With `--race-window-ns W`, a shadow is flagged `ambiguous_race` when it activated within `W` of
`ts_live`, when its first fill came within `W` after activation, or when a fill fell within `W` before its cancel took
effect.

An amend sent at `ts_send` takes effect at `ts_send + amend delay`, at the start of the next event. If the new
quantity is at or below what has already filled, the shadow ends `amended_to_filled`; otherwise its quantity
shrinks and its queue position is unchanged.

## How a shadow ends

| `end_state` | When |
|---|---|
| `filled` | Full quantity filled. |
| `cancelled` | Its cancel took effect first. |
| `amended_to_filled` | An amend reduced it to what had already filled. |
| `censored_session_end` | Still resting at the end of input, which reached the session close. |
| `open_at_end` | Still resting at the end of input, before the close. |
| `censored_clear` | The instrument's book was cleared (`R` record). |
| `censored_gap` | Sequence gap, a `MAYBE_BAD_BOOK` record, or no data for its instrument and date. |
| `rejected_would_cross`, `rejected_no_touch` | A touch-mode price could not be resolved as passive. |

`shadow_planned_end.parquet` gives each shadow's planned end (its own cancel or the session close), known before
the replay and independent of whether it filled. The zoo uses it to decide which shadows have a label at each
horizon ([outcome-independent censoring](audit-rules.md#a2-outcome-independent-censoring)).

## Mirror shadows

`shadows mirror-real` samples real resting orders from the MBO data and places a shadow with the same side, price and
size, sent at the real order's arrival time with zero latency and cancelled when the real order was cancelled. Real
lives inside a snapshot, or that ended by a replace or a book clear, are not sampled: their queue position is not
comparable. Because activation is strictly after `ts_live`, the shadow joins directly behind the real order. Two
properties follow and are tested (O-SH-4): the shadow never fills when the real order did not, and never fills
before the real order's first fill. Mirror shadows give a population whose placement follows real traders'
behaviour; because they carry real cancel times, the zoo reports them separately with the cancel as a competing
risk.

## What a shadow does not do

Shadows are **independent and infinitesimal**:

- They never consume liquidity. When a shadow fills, the real orders behind it still fill as they did in the data.
  A real order of the same size would have taken that volume from them.
- They never see each other. Two shadows at the same price do not queue behind one another.
- They never change the book or anyone's behaviour. A real order would have added to the displayed size and might
  have changed what others did.

For a passive order that is small relative to its level, these effects are second order. For large orders, or many
of your own orders at one price, they are not; the scorer reports how many of your live orders overlapped another
of yours at the same price and side. Own-order queue interaction is planned for a later version.

## Where to see it

- `queuetruth why --data DS --shadows S.parquet --shadow-id ID`: every book event at the shadow's level with queue
  ahead after each ([Explain one fill](../how-to/explain-one-fill.md)).
- `queue_trajectory.parquet` from `replay --shadows` ([Queue ahead](queue-ahead.md)).
- The rule as written in Rust: `crates/replay/src/shadow/` (`FillRule::Exact`, the default). The earlier rule
  (at-price prints deplete the queue, a crossed book fills the remainder at the shadow's price) is kept for parity
  runs against the predecessor engine: `replay --fill-rule et`.
- `replay --exclude-order-ids PATH` removes real orders from the tape before the shadows run (a JSON list, or one
  `order_id` or `instrument_id,order_id` per line); their executions still count as volume at their price, so a
  shadow placed where your order was takes its place in the queue. The scorer applies the same exclusion on its
  own ([Scorecard statistics](scorecard.md#the-exact-arm-and-your-own-orders)).
