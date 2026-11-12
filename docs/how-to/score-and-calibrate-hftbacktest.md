# Score and calibrate my hftbacktest backtest with the plugin

**Goal:** in a few calls, find out how far hftbacktest's fills are from the exact queue for your orders and your
`BacktestAsset` settings, then fit the ProbQueue `n` of `power_prob_queue_model3` (or `2`, or the plain power model)
and paste the result into your backtest.

**You need:** `pip install queuetruth-hftbacktest` (it installs queuetruth), order-level (L3 or MBO) data for the
venue you backtest as a queuetruth dataset (`queuetruth ingest`, see
[Calibrate my hftbacktest queue model](calibrate-hftbacktest.md)), and, for scoring, a separate venv with
hftbacktest 2.4.4.

The plugin, `queuetruth-hftbacktest`, wraps queuetruth's own commands and adds no model of its own. Its source,
tests and release workflow are in
[`integrations/hftbacktest/plugin`](https://github.com/sarptandoven/queuetruth/tree/main/integrations/hftbacktest/plugin).
For the full method behind the calibration (teacher-forced allocation quasi-likelihood, the bootstrap, the held-out
protocol and the trust check) read [Calibrate my hftbacktest queue model](calibrate-hftbacktest.md) and
[Calibration](../concepts/calibration.md); this page is the short path.

The code on this page needs the plugin installed, so the docs check only checks its syntax. The same steps run on
the synthetic dataset in the plugin's tests (`make test-hftbacktest-plugin`), including hftbacktest itself when its
venv is there.

## Set up hftbacktest in its own venv

hftbacktest pins numba and numpy versions that are not queuetruth's, so it runs in its own venv and the plugin calls
it by path. These are the versions the plugin is tested with:

<!-- check-docs: skip (installs packages) -->
```sh
python -m venv hbt-venv
hbt-venv/bin/pip install numba==0.68.0 llvmlite==0.50.0 numpy==2.5.3
hbt-venv/bin/pip install --no-deps hftbacktest==2.4.4
```

In a clone of queuetruth, `integrations/hftbacktest/setup_venvs.sh` makes the same venv in `.scratch/hbt-venv`.

## 1. Score hftbacktest against the exact queue

Write your hftbacktest settings in a file that defines `asset`. The replay adds the data, tick size and lot size:

<!-- check-docs: skip (needs hftbacktest) -->
```python
# my_asset.py
from hftbacktest import BacktestAsset

asset = BacktestAsset().power_prob_queue_model3(2.0).no_partial_fill_exchange().constant_order_latency(0, 0)
```

Then, in the environment that has the plugin:

<!-- check-docs: skip (needs the plugin, user data and hftbacktest) -->
```python
import queuetruth_hftbacktest as qthbt

prep = qthbt.prepare("my-dataset", "my-orders.parquet", "hbt-check")
qthbt.run(prep, python="hbt-venv/bin/python", snippet="my_asset.py")
res = qthbt.score(prep)
print(res.text())
```

`my-orders.parquet` is a `qt.shadow_spec` table of the orders to test: your own orders
(`queuetruth shadows from-live`, see [Import fills from my venue or broker](import-live-logs.md)) or orders placed
like yours:

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth shadows sample --data my-dataset --n 3000 --out my-orders.parquet \
    --placements 0:0.7,-1:0.3 --qty lots:1:0.5,lots:5:0.5
```

What each call does:

| call | queuetruth commands it runs | writes |
|---|---|---|
| `prepare` | `replay` (twice: resolve prices, then the truth), `export l2-npz` per instrument-day, `export replay-schedule --target hftbacktest` | `schedule.parquet`, `exact/`, `npz/`, `hbt_replay.py`, `instrument_map.csv`, `prepared.json` |
| `run` | none: runs `hbt_replay.py` in the hftbacktest venv | `hbt_orders.jsonl` and its `.summary.json` and `.engine.json` |
| `score` | `live import --venue sim-hftbacktest`, `referee run`, and the cluster-robust paired fill-rate difference of `queuetruth.stats` | `hbt_log/`, `referee_report.json`, `hbt_score.json` |

`res.text()` leads with the gap. For each horizon (1 s, 10 s, 60 s and 5 min by default) it gives the share of
orders with a first fill within the horizon in hftbacktest and in the exact replay, their difference in percentage
points with a cluster-robust 95% interval (clusters: instrument, session date and 30-minute bucket) and the relative
gap, for example "(8.0% fewer fills)". An order counts at a horizon only when its planned end is at or after its
entry plus the horizon. The first line gives the order-by-order referee counts: orders only the exact queue filled,
orders only hftbacktest filled, and orders filled by both with another quantity. `hbt_score.json` has every number,
with the McNemar p per horizon.

Each order lives until its planned cancel or 5 minutes after entry, whichever comes first (`prepare(window_ns=...)`,
`0` for no limit). The cap is written into the schedule, so hftbacktest and the exact replay end every order at the
same time, and horizons longer than the window are not scored.

`prepare` leaves out orders the exact engine rejects (a touch-relative price that would cross the book on arrival,
or no touch to price against) and counts them in `prep.skipped`. If you run hftbacktest yourself instead of `run`,
use the command `queuetruth-hftbacktest prepare` prints and pass its output file to `score`.

**Fill times.** The replay advances hftbacktest in steps of 10 ms while an order is open (`prepare(step_ns=...)`),
so a fill is recorded at the end of its step; the referee compares first-fill times with a tolerance of one step
(`score(ttf_tolerance_ns=...)`).

**hftbacktest #312.** With `partial_fill_exchange()`, the released hftbacktest 2.4.4 stops the whole backtest with
error 14 (`InvalidOrderStatus`) when an order that completed through a fill equal to its leaves is hit again. It
happens on the synthetic dataset too. `run` then raises `QT-USAGE-001` with the code (pass `allow_stop=True` to get
the output instead), and `score` scores only the orders entered before the stop, on the horizons that end before it,
with a warning. To score a whole day, use `no_partial_fill_exchange()` (the default of `calibrate` and of
`queuetruth export hftbacktest`) or a build with the fix (see the
[hftbacktest integration](https://github.com/sarptandoven/queuetruth/tree/main/integrations/hftbacktest)).

## 2. Calibrate the queue model

<!-- check-docs: skip (needs the plugin and user data) -->
```python
import queuetruth_hftbacktest as qthbt

cal = qthbt.calibrate("my-dataset", train="2026-10-12,2026-10-13", test="2026-10-14", out="hbt-cal",
                      orders="my-orders.parquet")
print(cal.text())
print(cal.code)
```

For each of `power3`, `power2` and `power` (`families=`), `calibrate` runs `queuetruth calibrate alloc` on the train
window and scores the fitted `n` and the default `n = 2` on the test window. Among the fits queuetruth's trust check
lets through, the one with the smallest held-out log loss at 60 s (`horizon_ns=`) wins. `cal.text()` starts with the
line to use, then a table of every family: the fitted `n`, its bootstrap 95% interval, the held-out log loss and
fill-rate bias at that horizon for the fitted value and for `n = 2`, and the trust verdict with its reasons.

`cal.code` is the `queuetruth export hftbacktest` snippet of the winner, unchanged, also written to
`hbt-cal/hbt_queue_model.py`: a `BacktestAsset` chain with `.power_prob_queue_model3(<n>)` (or the winning family's
builder) and the exchange model of the fit, under a header with the fit, its intervals and the held-out verdict. The
value has 6 significant digits; the full-precision estimate is in `hbt-cal/cal-<family>/calibration.json`.

If no family passes the trust check (for example none beats `n = 2` on the held-out window at every horizon),
`cal.code` raises `CalibrationRefused` (`QT-CALIB-001`) with the reasons per family, and there is no
`hbt_queue_model.py`. Keep `n = 2`, or fit on more data. `calibrate(..., force=True)` writes the best fit anyway
under queuetruth's `# !! NOT VALIDATED` header, for inspection only.

Options worth knowing:

| argument | default | meaning |
|---|---|---|
| `orders` | sampled | the orders the fit uses (`qt.shadow_spec`); without it, `n_orders` (2,000) are sampled with queuetruth's default placements and `seed` |
| `train`, `test` | | dates (`2026-10-12,2026-10-13`) or blocks of each session (`blocks:0-3/5`, `blocks:4-4/5`); without `test` the held-out scores are on all data, with a warning |
| `exchange` | `no_partial` | `no_partial` (`no_partial_fill_exchange`, runs on hftbacktest 2.4.4) or `partial` (`partial_fill_exchange`, stops 2.4.4 with error 14, #312) |
| `families` | `power3`, `power2`, `power` | the power families to fit |
| `force` | `False` | export the best refused fit under `NOT VALIDATED` |

The fit scores where cancellations come from, not the fill rate itself. Read the held-out fill-rate bias next to the
log loss: a fitted `n` that ranks orders better can still move the aggregate fill rate away from exact. To see what
the calibrated model does inside hftbacktest, score it on orders and days the fit did not see:

<!-- check-docs: skip (needs the plugin, user data and hftbacktest) -->
```python
prep = qthbt.prepare("my-dataset", "other-orders.parquet", "hbt-check-cal")
qthbt.run(prep, python="hbt-venv/bin/python", code=cal.code)
print(qthbt.score(prep).text())
```

## From the command line

<!-- check-docs: skip (needs the plugin, user data and hftbacktest) -->
```sh
queuetruth-hftbacktest prepare --data my-dataset --orders my-orders.parquet --out hbt-check
queuetruth-hftbacktest run hbt-check --python hbt-venv/bin/python --snippet my_asset.py
queuetruth-hftbacktest score hbt-check
queuetruth-hftbacktest calibrate --data my-dataset --train 2026-10-12,2026-10-13 --test 2026-10-14 --out hbt-cal
```

`--format json` prints the result document (or a `qt.error` document on failure). Exit codes follow `queuetruth`:
0 success, 1 when no calibrated model passes the trust check (`QT-CALIB-001`), 2 any other error.

## Limits

- One hftbacktest order per scheduled order. `prepare` leaves out later legs of a re-priced live order; to replay
  those, use `queuetruth export replay-schedule` and `queuetruth score --sim-log` directly.
- The per-horizon comparison is of first fills, at the replay step's time resolution.
- A fitted `n` is specific to the venue, feed, depth and orders it was fitted on: refit when they change, and see
  [Data and licensing](../data-and-licensing.md) before you share fitted parameters.
