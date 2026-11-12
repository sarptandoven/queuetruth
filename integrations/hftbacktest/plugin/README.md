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

Your data goes into a queuetruth dataset first (`queuetruth ingest`).

## Use

```python
import queuetruth_hftbacktest as qthbt

# 1. score hftbacktest (your BacktestAsset in my_asset.py) against the exact queue
prep = qthbt.prepare("my-dataset", "my-orders.parquet", "hbt-check")
qthbt.run(prep, python="hbt-venv/bin/python", snippet="my_asset.py")
print(qthbt.score(prep).text())

# 2. fit n on one window, check it against n = 2 on another, get the code to paste
cal = qthbt.calibrate("my-dataset", train="2026-10-12,2026-10-13", test="2026-10-14", out="hbt-cal")
print(cal.text())
print(cal.code)
```

One line in your hftbacktest code sets the queue model and exchange of a fit, or of the atlas's closest setting
when you have no data, on your `BacktestAsset` (your data, tick, lot, latency and fee settings stay):

```python
import queuetruth as qt

asset = qthbt.configure(asset, qt.fit("hftbacktest", data="my-dataset"))  # a fit on your data
asset = qthbt.configure(asset, qt.atlas.calibration("hftbacktest", tier="T1", yes=True))  # no data: the atlas
asset = qthbt.configure(asset, cal)  # the result of qthbt.calibrate above
```

`configure` is standard library only: in the hftbacktest venv, `pip install --no-deps queuetruth-hftbacktest` and
pass a saved result (`qt.atlas.calibration(...).save("fit.json")`, or `queuetruth fit`'s `qt-fit/fit.json`).
A refused fit raises `CalibrationRefused` unless `force=True`.

The same from the command line: `queuetruth-hftbacktest prepare | run | score | calibrate`.

How to use it, what each call runs and writes, how to read the result, the options, hftbacktest #312 and the
limits: [Score and calibrate my hftbacktest backtest](https://sarptandoven.github.io/queuetruth/docs/how-to/calibrate-hftbacktest/), the guide in queuetruth's docs, which starts with
this plugin.

## Tests

```sh
make test-hftbacktest-plugin          # or: uv run --frozen --no-sync pytest integrations/hftbacktest/plugin/tests
```

The tests run on queuetruth's synthetic dataset. Those that run hftbacktest use `QT_HBT_PY` (default
`.scratch/hbt-venv/bin/python`, from `integrations/hftbacktest/setup_venvs.sh` or `make hbt-crosscheck`; the older name
`QT_HBT_PYTHON` is still read) and skip with the reason without it. CI runs them with hftbacktest 2.4.4.

## Limits

- One simulator order per scheduled order: later legs of a re-priced live order are left out by `prepare`
  (replay those with `queuetruth export replay-schedule` and score with `queuetruth score --sim-log`).
- Fill times have the resolution of the replay step; the per-horizon comparison is of first fills.
- The calibration fits the allocation of cancels (teacher forced), not the fill rate itself; read its held-out
  fill-rate bias next to the log loss, and see [Calibration](https://github.com/sarptandoven/queuetruth/blob/main/docs/concepts/calibration.md).
- A fitted `n` is specific to the venue, feed, depth and the orders it was fitted on. Refit when they change.

## License

Apache-2.0, like queuetruth. hftbacktest (MIT) is not imported by this package; it runs in its own process.
