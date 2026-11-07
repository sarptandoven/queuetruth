# queuetruth-hftbacktest

Two things for [hftbacktest](https://github.com/nkaz001/hftbacktest) users who have order-level (L3 or MBO) data
for the venue they backtest, each in one or two calls of [queuetruth](https://github.com/sarptandoven/queuetruth):

1. **Score your backtest's fills against exact-queue truth.** Your orders and your `BacktestAsset` settings run in
   hftbacktest itself, on the L2 view of the data; the same orders run through queuetruth's exact FIFO replay of the
   order-level data. You get, per horizon, how many more or fewer orders hftbacktest fills than the exact queue,
   with a cluster-robust 95% interval, and an order-by-order referee report.
2. **Calibrate the queue model.** Fit the `n` of `power_prob_queue_model3`, `power_prob_queue_model2` and
   `power_prob_queue_model` on one window, check it against the default `n = 2` on another, and get the
   `BacktestAsset` code to paste. A fit that does not beat the default out of sample gives no code.

The package is a thin layer over queuetruth's own commands (`replay`, `export l2-npz`, `export replay-schedule`,
`live import --venue sim-hftbacktest`, `referee run`, `shadows sample`, `calibrate alloc`, `export hftbacktest`);
it adds no model of its own.

## Install

```sh
pip install queuetruth-hftbacktest        # installs queuetruth too
```

hftbacktest runs in its own venv (its numba and numpy pins are not queuetruth's), which the plugin calls by path.
The tested versions are hftbacktest 2.4.4 with numba 0.68.0, llvmlite 0.50.0 and numpy 2.5.3:

```sh
python -m venv hbt-venv
hbt-venv/bin/pip install numba==0.68.0 llvmlite==0.50.0 numpy==2.5.3
hbt-venv/bin/pip install --no-deps hftbacktest==2.4.4
```

Your data goes into a queuetruth dataset first (`queuetruth ingest`, see
[Calibrate my hftbacktest queue model](https://github.com/sarptandoven/queuetruth/blob/main/docs/how-to/calibrate-hftbacktest.md) for the formats).

## 1. Score

```python
import queuetruth_hftbacktest as qthbt

prep = qthbt.prepare("my-dataset", "my-orders.parquet", "hbt-check")
qthbt.run(prep, python="hbt-venv/bin/python", snippet="my_asset.py")
res = qthbt.score(prep)
print(res.text())
```

- `my-orders.parquet` is a `qt.shadow_spec` table: your orders (`queuetruth shadows from-live`), or orders placed
  like yours (`queuetruth shadows sample --placements 0:0.7,-1:0.3 ...`).
- `my_asset.py` defines `asset`, an hftbacktest `BacktestAsset` with your queue model, exchange model, latency and
  fees, for example `asset = BacktestAsset().power_prob_queue_model3(2.0).partial_fill_exchange()`. The replay sets
  data, tick and lot size itself. A `queuetruth export hftbacktest` snippet, or the code `calibrate` writes, works as
  is (`run(prep, code=cal.code)`).
- Instead of `run`, you can run the command `prepare` prints in the hftbacktest venv yourself.

`prepare` replays the orders with the exact engine (touch-relative prices resolved at entry), writes one hftbacktest
event file per instrument-day (`queuetruth export l2-npz`) and the hftbacktest replay script of the orders
(`queuetruth export replay-schedule --target hftbacktest`). `score` imports what hftbacktest did and compares it
with the exact replay of the same orders. In `hbt-check/`:

| file | what |
|---|---|
| `hbt_score.json` | per horizon (default 1 s, 10 s, 60 s, 5 min): orders scored, exact and hftbacktest fill rates (first fill within the horizon), their difference with a cluster-robust 95% CI (clusters: instrument, session date, 30 minutes) and the exact McNemar p; the referee counts; the hftbacktest version and the replay summary |
| `referee_report.json` | `qt.referee_report`: orders only one side filled, quantity mismatches, first-fill time errors, up to 20 examples |
| `schedule.parquet`, `exact/` | the orders as replayed (absolute prices, entry at the exact entry time) and their exact replay |
| `npz/`, `hbt_replay.py`, `instrument_map.csv` | what hftbacktest ran on, and the units map back to the dataset |
| `hbt_orders.jsonl` (+ `.summary.json`, `.engine.json`) | hftbacktest's order states, its replay summary and version |

Each order lives until its planned cancel or 5 minutes after entry, whichever is first (`prepare(window_ns=...)`, 0
for no limit); the cap is written into the schedule, so hftbacktest and the exact replay end every order at the same
time. An order counts at horizon `h` only if its planned end is at or after entry plus `h`. hftbacktest records fills at
the end of each replay step (10 ms by default, `prepare(step_ns=...)`), so first-fill times are compared with a
tolerance of one step.

**hftbacktest #312.** With `partial_fill_exchange`, released hftbacktest 2.4.4 stops the whole backtest with error
14 when an order completes through a fill equal to its leaves and is then hit again (see
[UPSTREAM.md](https://github.com/sarptandoven/queuetruth/blob/main/integrations/hftbacktest/UPSTREAM.md)). `run` raises `QT-USAGE-001` naming the code (`allow_stop=True` returns instead), and
`score` scores only the orders entered before the stop, on horizons that end before it, with a warning. Use
`no_partial_fill_exchange()` or a build with the fix to score a whole day.

## 2. Calibrate

```python
cal = qthbt.calibrate(
    "my-dataset", train="2026-10-12,2026-10-13", test="2026-10-14", out="hbt-cal", orders="my-orders.parquet"
)
print(cal.text())
print(cal.code)  # paste into your backtest; also written to hbt-cal/hbt_queue_model.py
```

For each power family, `calibrate` runs `queuetruth calibrate alloc` (the fitted `n` with bootstrap and profile
intervals, the held-out Brier score, log loss and fill-rate bias of the fitted value and of `n = 2`, and queuetruth's
trust verdict). Among the families the trust check lets through, the smallest held-out log loss at 60 s
(`horizon_ns=`) wins, and its code is `queuetruth export hftbacktest`'s snippet, unchanged:

```text
# queuetruth 0.2.0 | calibration <run id> | power3 fitted on <train>; held-out <test>
# data: <venue> (<validation status>, dataset <id>)
# fit: n = <estimate> (alloc); <events> events from <shadows> shadows; bootstrap CI [<lo>, <hi>]; profile CI [...]
# held-out: <the beats-default sentence>
# Paste into your existing BacktestAsset chain (keep your data/tick/lot/fee settings).
from hftbacktest import BacktestAsset
asset = (
    BacktestAsset()
    # ... your existing .data(...), .tick_size(...), .lot_size(...), fee model ...
    .power_prob_queue_model3(<estimate>)
    .partial_fill_exchange()
)
```

Without `orders`, 2,000 orders are sampled with queuetruth's defaults; sample orders like yours if your placements
differ. Windows are dates (`2026-10-12,2026-10-13`) or blocks of each session (`blocks:0-3/5` to train on the first
four fifths and `blocks:4-4/5` to test on the last). If every family is refused, `cal.code` raises
`CalibrationRefused` (`QT-CALIB-001`) with the reasons per family: keep `n = 2`. `force=True` writes the best fit under
queuetruth's `# !! NOT VALIDATED` header, for inspection only.

To check the calibrated model in hftbacktest itself, score it: `qthbt.run(prep, python=..., code=cal.code)` then
`qthbt.score(prep)`, ideally on orders and days the fit did not see.

## Command line

```sh
queuetruth-hftbacktest prepare --data my-dataset --orders my-orders.parquet --out hbt-check
queuetruth-hftbacktest run hbt-check --python hbt-venv/bin/python --snippet my_asset.py
queuetruth-hftbacktest score hbt-check
queuetruth-hftbacktest calibrate --data my-dataset --train 2026-10-12,2026-10-13 --test 2026-10-14 --out hbt-cal
```

`--format json` (or `--json`) prints the result document, or a `qt.error` document on failure. Exit codes as
`queuetruth`: 0 success, 1 no calibrated model passes the trust check, 2 any other error.

## Tests

```sh
make test-hftbacktest-plugin          # or: uv run --frozen --no-sync pytest integrations/hftbacktest/plugin/tests
```

The tests run on queuetruth's synthetic dataset. Those that run hftbacktest use `QT_HBT_PY` (default
`.scratch/hbt-venv/bin/python`, from `integrations/hftbacktest/setup_venvs.sh`) and skip with the reason without it.

## Limits

- One simulator order per scheduled order: later legs of a re-priced live order are left out by `prepare`
  (replay those with `queuetruth export replay-schedule` and score with `queuetruth score --sim-log`).
- Fill times have the resolution of the replay step; the per-horizon comparison is of first fills.
- The calibration fits the allocation of cancels (teacher forced), not the fill rate itself; read its held-out
  fill-rate bias next to the log loss, and see [Calibration](https://github.com/sarptandoven/queuetruth/blob/main/docs/concepts/calibration.md).
- A fitted `n` is specific to the venue, feed, depth and the orders it was fitted on. Refit when they change.

## License

Apache-2.0, like queuetruth. hftbacktest (MIT) is not imported by this package; it runs in its own process.
