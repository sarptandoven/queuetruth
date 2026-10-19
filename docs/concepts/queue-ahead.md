# Queue ahead

**Queue ahead** is the displayed quantity that must trade or cancel before your order at the same price can fill.
**Rank** is the number of orders that make up that quantity. Both are known exactly in an order-level replay and
must be estimated from an L2 feed.

## Exact definition

When a shadow goes live, every real order resting at its side and price is ahead of it. The engine keeps that set of
order ids. From then on, for each order in the set:

- a cancel or reduction removes that much from queue ahead;
- an execution (`F` record) removes the executed size, and the matching removal is not counted again;
- a priority-losing modify, a replace or a duplicate add removes the whole order from the set (it is now behind).

```text
queue_ahead = sum of the remaining displayed sizes of the orders still in the ahead set
rank        = number of orders still in the ahead set
```

Orders that join the level after the shadow are behind it. Trade prints do not change queue ahead; only the records
of the orders ahead do. The shadow fills when real orders behind it execute, when the market trades through its
price, or when the book crosses it ([Shadow orders](shadow-orders.md#how-a-shadow-fills)).

## The decomposition

For every event that touches the shadow's level, the replay splits what happened (`qt.queue_trajectory`):

| Column | Meaning |
|---|---|
| `cancel_ahead_qty` | Size removed by cancels and reductions of orders ahead of the shadow. |
| `cancel_behind_qty` | Size removed by cancels of orders behind it. |
| `add_behind_qty` | Size added behind it (new orders, and priority-losing modifies that land at its price). |
| `traded_at_px_qty` | Print volume at its price with an opposite-side aggressor in the event. |
| `trade_through_qty` | Volume executed through its price in the event. |
| `queue_ahead_before/after`, `rank_before/after` | State before and after the event. |
| `level_qty_before/after` | Displayed size at the price (the shadow is never in the book). |
| `filled_qty` | The shadow's cumulative filled quantity after the event. |

An L2 feed shows only the level size and the trades. A decrease of the level size that trades do not explain is
`cancel_ahead + cancel_behind`, and the split between the two is exactly what an L2 queue model guesses. That split
is what [calibration](calibration.md) fits.

Trajectory mode `ahead` (the default) writes a row when the shadow goes live, a row for every event where queue
ahead, rank or filled quantity changed, and a row at the end. Mode `full` (`replay --trajectory full`) also writes
events where only the level size, `cancel_behind` or `add_behind` moved.

## A worked example

A bid level at 100 holds two orders: `o1` for 60 and `o2` for 40. You send a 10-lot bid at 100 at `t = 15` with zero
latency. Then `o3` for 30 joins at `t = 20`, `o1` cancels at `t = 30`, and at `t = 40` a sell aggressor trades 45 at
100, executing `o2` for 40 and then `o3` for 5.

| Event | What happened | Exact: queue ahead after | Exact: rank |
|---|---|---|---|
| `t = 20` | The shadow goes live at the start of this event (the first with `ts_event > 15`); `o3` joins behind it. | 100 | 2 |
| `t = 30` | `o1` cancels 60. | 40 | 1 |
| `t = 40` | `o2` executes 40, then `o3` executes 5. | 0, and the shadow fills 5 | 0 |

At `t = 40` the execution of `o2` empties the queue ahead, and the execution of `o3`, which sat behind the shadow,
fills the shadow by 5 (`queue_depleted`). The shadow is still resting for the other 5 when the data ends
(`open_at_end`). In the real data `o3` received those 5; shadows do not take volume from real orders
([Shadow orders](shadow-orders.md#what-a-shadow-does-not-do)). This is scenario S3 of the shadow test suite
(O-SH-1), where every value in the exact columns is asserted.

The same order seen through the L2 view of the same data (level size 100 at arrival, then 130, then 70, then a trade
of 45 and size 25), with the model equations of [The L2 model zoo](l2-model-zoo.md), lot size 1:

| After event | Exact | `front_of_queue` | `hbt_risk_adverse` | `hbt_power3[n=2]` |
|---|---|---|---|---|
| live | 100 | 0 | 100 | 100 |
| `t = 30` (size 130 to 70, no trade) | 40 | 0 | 70 | 64.5 |
| `t = 40` (trade 45, size 70 to 25) | 0, fills 5 | fills 10 | 25, no fill | 19.5, no fill |

`hbt_risk_adverse` assumes every unexplained decrease came from behind, so it advances only through trades and when
the level shrinks below its estimate. `hbt_power3[n=2]` gives the 60-lot decrease a probability `(100/130)^2 = 0.59`
of having come from the front, which moves its estimate forward by `0.59 * 60 = 35.5`. The exact answer is that all
60 was ahead. Here both L2 models are too pessimistic; on levels where cancels come mostly from behind they are too
optimistic. The model columns were computed by hand from the equations; the zoo measures which way and by how much
over thousands of orders.

## Rank and the rank oracle

The same machinery runs for real orders. On every real execution, the rank oracle records how many orders were
ahead of the executed order; on a FIFO venue with a correct book it is 0, which is how the book itself is validated
([Exact FIFO truth](exact-truth.md#how-the-book-is-checked)). For real resting orders, `replay --real-orders` writes
`real_orders.parquet` with `queue_ahead_at_add`, `rank_at_add`, fill times and how each order ended.

## Where to see it

- `queuetruth why --data DS --shadows S.parquet --shadow-id ID`: every book event at the order's level from
  activation to the end, whether each touched order was ahead, and queue ahead and rank after it.
- `queue_trajectory.parquet` from `replay --shadows`.
- `zoo --write-trajectory`: each model's estimate next to the exact value after every L2 batch, and the `qtrack_mae`
  metric (time-weighted mean absolute error of the estimate as a share of level size at live plus order size).
