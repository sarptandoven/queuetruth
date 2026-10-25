# hftbacktest #312: reproduction and proposed fix

Status: **prepared, not submitted.** Nothing in this directory has been posted to GitHub. An open pull request,
[#323](https://github.com/nkaz001/hftbacktest/pull/323), already claims #312 with the `>=` change that this
directory shows to be incomplete (see [Prior work](#prior-work-and-how-this-patch-relates-to-it)); the recommended
action is a review comment on #323 with the test case and the helper below, not a competing pull request.

| item | value |
|---|---|
| issue | [nkaz001/hftbacktest#312](https://github.com/nkaz001/hftbacktest/issues/312), "PartialFillExchange fails with InvalidOrderStatus when filled_qty == leaves_qty", opened 2026-05-04, closed as not planned by the stale bot on 2026-09-02 with no maintainer reply; #301 reports the same error code |
| affected | `hftbacktest/src/backtest/proc/partialfillexchange.rs` since c60d774 ("fix: partially filled orders deleted", which replaced an unconditional removal by the strict `>` branch; dated 2025-08-10 in the report) up to master 5f3ec40 (2025-12-23, the last commit) and the PyPI wheel 2.4.4 |
| patch | [`upstream/0001-partialfillexchange-remove-completed-orders-312.patch`](upstream/0001-partialfillexchange-remove-completed-orders-312.patch), against master 5f3ec40; applies to tag py-v2.4.4 with its last hunk 4 lines earlier (`git apply`: offset -4; BSD `patch` also prints a harmless "No such line 864" notice) |
| reproduction | [`upstream/repro_312.py`](upstream/repro_312.py) (numpy + hftbacktest only) |
| prior work | [#323](https://github.com/nkaz001/hftbacktest/pull/323) (vedjaw, opened 2026-08-01, open with no maintainer review when checked on 2026-10-24): `>` to `>=` in `partialfillexchange.rs` plus a `local.rs` change for #316, "Closes #312, #316"; fork commits `be916b0` (hashed00, 2026-05-04, the same `>=` change) and `6b307c2` (0xLian117, 2026-06-22, a reverted #316 attempt in `local.rs`) |
| re-verification | [`upstream/verify.sh`](upstream/verify.sh) rebuilds the Rust and Python test results, the reproduction's exit codes, the synthetic regression, the driver parity and the #316 measurement from public sources and the repository's O-ZOO-3 scenarios; the real-data numbers need the Nasdaq ITCH datasets (README.md, "Reproduce the documented run") |
| license | hftbacktest is MIT-licensed. The patch's context lines and `py-hftbacktest-2.4.4-build.Cargo.lock` (resolved from hftbacktest's own manifests) derive from it and stay under its license; the rest of this directory is queuetruth's (Apache-2.0) |

## The bug

`PartialFillExchange::fill` (5f3ec40, line 208) decides the status from the leaves quantity rounded to lots:

```rust
order.leaves_qty -= exec_qty;
if (order.leaves_qty / self.depth.lot_size()).round() > 0f64 {
    order.status = Status::PartiallyFilled;
} else {
    order.status = Status::Filled;
}
```

But `check_if_sell_filled` and `check_if_buy_filled` (lines 155 and 195) register a maker order for removal from
the book only when the queue model's fill is **strictly larger** than the leaves:

```rust
let exec_qty = if filled_qty > order.leaves_qty {
    self.filled_orders.push(order.order_id);
    order.leaves_qty
} else {
    filled_qty
};
```

So whenever the leaves left after the execution round to zero lots without `filled_qty > leaves_qty`, the order
becomes `Filled` and stays in `orders`, `buy_orders` / `sell_orders`. The next trade at its price (with queue
volume past it), trade through its price or best-price cross calls `fill()` on it again, which returns
`BacktestError::InvalidOrderStatus` (line 220). `elapse` returns 14 and the whole backtest stops, for every order
and every asset, not just the completed one.

Three ways an order completes without leaving the book, and one more path that hits the resulting zombie, all
reproduced by `repro_312.py` on the released wheel:

| case | sizes | why it completes without `filled_qty > leaves_qty` (or how the zombie is hit) |
|---|---|---|
| `equal_leaves` | lot 1, qty 2, fills 1 then 1 | `filled_qty == leaves_qty` (the report's case) |
| `float_residual` | lot 0.01, qty 0.1 (the report's sizes), fills 0.01 then 0.09 | leaves are `0.1 - 0.01 = 0.09000000000000001`, the queue model fills `9 * 0.01 = 0.09`: `filled_qty < leaves_qty`, the 1.4e-17 left rounds to 0 lots |
| `non_lot_quantity` | lot 1, qty 1.4, fills 1 | the 0.4 left rounds to 0 lots |
| `trade_through` | lot 1, sell 2, fills 1 then 1, then a buy trade through the price | completes as in `equal_leaves`; the zombie is then hit by the trade-through path (`Ordering::Less`) instead of a trade at its price |

`NoPartialFillExchange` is not affected: it always executes the whole leaves and registers the order.

### Why the reporter's one-line fix is not enough

The report suggests changing `>` to `>=`. That fixes `filled_qty == leaves_qty`, but not the floating-point
residual of the report's own sizes: with lot 0.01 and qty 0.1, 5 of the 9 possible first-fill sizes leave a leaves
quantity one ulp above the next lot multiple (0.09000000000000001, 0.060000000000000005, 0.04000000000000001,
0.020000000000000004, 0.010000000000000009), and the next queue fill (a whole number of lots) is then smaller than
the leaves. The Rust test `sub_lot_residual_completes_the_order` fails with `>=` (see the table below), and so does
the non-lot quantity case.

## The fix

Register the order for removal whenever the leaves left after the execution round to zero lots, the same rule
`fill()` uses for the status, in one helper used by both sides:

```rust
fn maker_exec_qty(&mut self, order: &Order, filled_qty: f64) -> f64 {
    if filled_qty > order.leaves_qty {
        self.filled_orders.push(order.order_id);
        return order.leaves_qty;
    }
    if ((order.leaves_qty - filled_qty) / self.depth.lot_size()).round() <= 0.0 {
        self.filled_orders.push(order.order_id);
    }
    filled_qty
}
```

Executed quantities, statuses, fees, timestamps and responses are unchanged; the only behaviour change is that a
completed order leaves the book. Alternatives considered:

- **Execute the whole leaves when the order completes** (so leaves end at exactly 0). Cleaner position arithmetic
  for the residual case, but it changes fills: on the `non_lot_quantity` case the order would execute 1.4 where the
  queue allowed 1.0, and on the synthetic regression set 18 of the 177 aborting runs would get different fill
  quantities. Rejected to keep the fix a pure removal.
- **Push to `filled_orders` inside `fill()`** whenever the status becomes `Filled`. The best-price cross paths
  (`on_best_bid_update`, `on_best_ask_update`) already push before calling `fill()` and `remove_filled_orders`
  unwraps each removal, so a second push would panic, while the taker paths of `ack_new` fill orders that never
  entered the book. Every caller would need changing. Rejected as larger than needed. (A review comment on #323
  proposes this push among a larger rework; it needs those existing pushes removed first.)

## Prior work and how this patch relates to it

- **The reporter's fix** (`be916b0` on hashed00's fork, 2026-05-04) and **#323** (vedjaw, opened 2026-08-01) make
  the same `>` to `>=` change in `check_if_{buy,sell}_filled`. It fixes `equal_leaves` and `trade_through`, but not
  `float_residual` (the report's own lot 0.01 / qty 0.1) or `non_lot_quantity`: the two Rust tests named below fail
  with `>=`. This patch is the `partialfillexchange.rs` half of #323 done with the rounding rule `fill()` already
  uses, so it covers all four cases.
- **#323's `local.rs` change** (apply `PartiallyFilled` responses to position, for #316) is independent of this
  patch: this patch does not touch `local.rs`, and the measurement below shows #316 unchanged by it. The two diffs
  do overlap textually in `partialfillexchange.rs` (both rewrite the two `if filled_qty > order.leaves_qty`
  branches), so one has to be rebased on the other.
- **#323's review thread** (Altairpaca, 2026-09-12; CthulhuMythos, 2026-10-12) reports further problems on the PR
  head: a rejected cancel or modify echoes a stale `exec_qty` that the new `local.rs` code counts twice, taker
  executions of an IOC order across several levels and the expiry of its remainder are not reported piece by
  piece, and a modify after a partial fill can replay the last execution. None of these is a #312 abort, and this
  patch neither fixes nor changes them; its tests do not cover IOC or modify.
- **0xLian117's `6b307c2`** (2026-06-22) reverted their own one-line `local.rs` attempt at #316; it does not touch
  `partialfillexchange.rs`.

Recommended action: a review comment on #323 with the `sub_lot_residual_completes_the_order` and
`sub_lot_leaves_of_a_non_lot_quantity_complete_the_order` tests and the `maker_exec_qty` helper as a suggested change
to its `partialfillexchange.rs` diff. If #323 merges as is, a follow-up pull request that references #323 and #312
and touches only `partialfillexchange.rs` (the helper and the 7 tests; text below).

## Evidence

All numbers from runs on 2026-10-24, macOS arm64, rustc 1.97.1, Python 3.13, numba 0.68.0, numpy 2.5.3.

### Tests in the patch

The patch adds 7 Rust unit tests to `partialfillexchange.rs` (a full `Backtest` with `RiskAdverseQueueModel`, zero
latency, one GTX order) and 5 Python tests in `py-hftbacktest/tests/test_partial_fill_312.py` (unittest, no data
files, the style of the existing `test_hftbacktest.py`).

| build | Rust: `cargo test --no-default-features --features backtest --lib partialfillexchange` | Python tests |
|---|---|---|
| 5f3ec40 unpatched (tests only) | 5 of 7 fail, each with `Err(InvalidOrderStatus)` | 2.4.4 wheel: 4 of 5 fail (`14 != 1`) |
| 5f3ec40 with the reporter's `>=` | 2 of 7 fail (`sub_lot_residual_completes_the_order`, `sub_lot_leaves_of_a_non_lot_quantity_complete_the_order`) | not built |
| 5f3ec40 with the patch | 7 of 7 pass; the whole lib suite passes (29 tests) | patched 2.4.4 wheel: 5 of 5 pass |

The two tests that pass everywhere (`genuine_partial_fill_keeps_the_order_resting`,
`fill_larger_than_leaves_still_completes`) guard existing behaviour. `rustfmt +nightly` with the repository's
`rustfmt.toml` reports no change; `cargo clippy` reports no new warning in the file.

### Regression on the O-ZOO-3 synthetic scenarios

queuetruth's O-ZOO-3 scenarios (`tests/fixtures/zoo/hbt_real/scenarios.json`, 83 scenarios: 23 hand-built, 60 seeded
random, every fifth with lot 0.5) under 18 queue models x 2 exchanges, run by `python/tests/zoo/hbt_crosscheck.py`
(per-row stepping) on the released 2.4.4 wheel and on the patched wheel; `evidence/synthetic_regression.py` writes
the feeds, runs both builds and counts (verify.sh checks every number of this table):

| | released 2.4.4 | patched |
|---|---|---|
| runs | 2,988 | 2,988 |
| runs stopped by error 14 | 177 | 0 |
| runs without an error whose fills, status events and first fills are identical to the other build | 2,811 of 2,811 | |
| stopped runs whose fills up to the stop equal the patched run's first fills | 177 of 177 | |
| runs where `hbt_driver.py` (the reconciliation's numba driver) gives the same fills and stop code as the per-row driver | 2,988 of 2,988 | 2,988 of 2,988 |

### Real data

Nasdaq TotalView-ITCH 5.0, 2019-12-30 and 2020-01-30, AAPL, MSFT, SPY, TSLA (8 instrument-days), 5,795 GTX
orders (queuetruth's sampled shadows: 694 to 750 per instrument-day, at the touch or one or two ticks behind, 1 or
100 shares, each live 5 minutes, about 9 to 10 live at any time), top-10 L2 view, partial-fill exchange, zero
latency, 6 queue models (the calibrated `power2` and `power3`, both at the default `n = 2`, `log`, `log2`). Run by
`reconcile.py`; full results and the commands in README.md, aggregates in `evidence/results.json`.

The number that transfers to other order flows: 404 to 665 of the 6,000 orders per model (6.7 to 11.1%) reach the
#312 state within their life, and in hftbacktest any one of them stops the whole run.

| | released 2.4.4 | patched |
|---|---|---|
| model x instrument-day runs | 48 | 48 |
| runs stopped by error 14 | 48 (every model on every instrument-day) | 0 |
| when the run stops | 7.3 to 26.6 minutes after the 09:30 open (median over the instrument-days: 14.4 for four models, 13.6 for `log`, 12.1 for `power2` at `n = 2`) | runs to the end of the day |
| fills before the stop equal to the patched build's | 48 of 48 | |
| orders whose whole fill sequence equals queuetruth's faithful re-implementation of the model | all orders live before the stop: 127 to 185 per model (109 to 171 of them with a fill before it) | 5,795 of 5,795 for each model |

So with this order flow, a backtest on the released wheel with `partial_fill_exchange` stopped within 27 minutes of
the open on every instrument-day, and the fix changes nothing before that point. A strategy that places fewer
orders reaches the first such order later, or not at all on a given day.

## How to verify

```sh
integrations/hftbacktest/upstream/verify.sh            # Rust, wheel, Python tests, repro, evidence/ (10-20 min)
integrations/hftbacktest/upstream/verify.sh --skip-wheel   # Rust tests only (about 2 min)
```

It downloads the GitHub tarballs of 5f3ec40 and py-v2.4.4 and the crates.io crate 0.9.4 (for its published
`Cargo.lock`), checks the sha256 of the files the patch touches, and prints each expectation it checks. The wheel is
built from py-v2.4.4 plus the patch with the pinned lockfile `py-hftbacktest-2.4.4-build.Cargo.lock` (resolved with a
14-day minimum publish age, the repository's supply-chain rule), with LTO off to keep the build short; the fix does not
depend on optimisation. With both venvs it then runs `evidence/synthetic_regression.py` and `evidence/pos316.py`
and checks the synthetic-regression, driver-parity and #316 numbers above. A full `verify.sh` run from an empty work
directory met every expectation of the Rust and Python stages on 2026-10-24 in 796 s on a shared laptop (before the
evidence stage existed); the current script, rerun on the same day in the same work directory (downloads and cargo
target reused), met all of them, the evidence stage included, in 397 s: Rust unpatched 5 failures, `>=` 2, patched
0 and 29 of 29 lib tests; Python tests `FAILED (failures=4)` on 2.4.4 and `OK` patched; `repro_312.py` exit 3 and 0;
synthetic regression 2,988 runs, 177 / 0 stopped, 2,811 identical, 177 prefixes; driver parity 2,988 of 2,988 on
each build; #316 position 1.0 on both. Wheel hashes are not reproducible between builds (three builds of the same
sources gave `7e8437aa...`, `7a942545...` and `4e67578b...`); every build passes the same checks. The real-data run
records the native module it used (`6c81d997...` patched, `b46e629b...` released) in `reconciliation.json`.

## Related, not fixed here

- **#316, position accounting.** `LocalProcessor` (`local.rs` line 102) applies a fill to the local state only when
  the response status is `Filled`, with that response's `exec_qty`: partial fills of a resting order never reach the
  position. Measured by `evidence/pos316.py` on the released and the patched build: an order of 2 filled 1 + 1
  leaves `position(0) = 1.0` on both. This is independent of #312 and needs its own change in `local.rs` (#323
  proposes one).
- **`ack_modify` on a partially filled order** sets `leaves_qty = qty`, forgetting the executed quantity. Not
  exercised by the tests here.

## Suggested text

Review comment on #323:

> The `>=` change fixes `filled_qty == leaves_qty`, but not the report's own sizes: with lot 0.01 and qty 0.1, a
> first fill of 0.01 leaves `0.09000000000000001`, the next queue fill is `0.09 < leaves`, the 1.4e-17 left rounds to
> 0 lots, `fill()` sets `Filled`, and the order stays in the book (the next trade at its price returns
> `InvalidOrderStatus`). The same happens for a quantity that is not a lot multiple (qty 1.4, lot 1). Suggested
> change: register the order whenever the leaves after the execution round to zero lots, the rule `fill()` uses for
> the status (helper and two failing tests below). Executed quantities are unchanged; this is independent of the
> `local.rs` part.

Follow-up pull request, if #323 merges without it:

> **PartialFillExchange: also remove orders completed by a sub-lot residual (follow-up to #323, #312)**
>
> `fill()` marks an order `Filled` once its leaves round to zero lots, but `check_if_{buy,sell}_filled` register it
> for removal only when the queue fill reaches the leaves (`>=` since #323). A floating-point residual (lot 0.01,
> qty 0.1: leaves 0.09000000000000001 against a fill of 0.09) or a quantity that is not a lot multiple still leaves
> a `Filled` order in the book, and the next trade fails with `InvalidOrderStatus`. This registers the order
> whenever the leaves left after the execution round to zero lots. Executed quantities are unchanged. Adds 7 Rust
> unit tests (2 fail with `>=`) and 5 Python tests. On 2,988 synthetic runs only the 177 that stopped with error 14
> change, and their fills up to the stop are identical.
