# hftbacktest integration

Three things for people who backtest with [hftbacktest](https://github.com/nkaz001/hftbacktest):

1. **Export** queuetruth's calibrated queue-model parameters as an hftbacktest config: the fitted `n` of the power
   families, with its confidence intervals, per-instrument estimates, held-out scores and trust verdict, next to the
   default `n = 2` and the parameter-free `log` models (`export_config.py`, `hbt_config.py`).
2. **Reconcile** hftbacktest with queuetruth on real data, order by order: run hftbacktest itself on the same orders
   and L2 view as queuetruth's zoo, and compare every fill with queuetruth's re-implementation of the same model and
   with the exact FIFO replay (`reconcile.py`, `hbt_driver.py`).
3. **hftbacktest #312**: a minimal reproduction and a proposed fix with tests, not submitted
   ([UPSTREAM.md](UPSTREAM.md)).

## Where it lives

The integration is in the queuetruth repository, not in the `queuetruth` wheel on PyPI: use a clone at a version
that contains `integrations/hftbacktest/` (any commit after 0.1.1). The queuetruth side (`export_config.py`,
`reconcile.py`) runs in the repository venv. hftbacktest always runs in its own venv (its numba and numpy pins are
not queuetruth's). `hbt_config.py` is standard library only: in your own hftbacktest code, put
`integrations/hftbacktest` on `PYTHONPATH` or copy that one file next to your strategy.

## Setup

```sh
integrations/hftbacktest/setup_venvs.sh                    # .scratch/hbt-venv: released hftbacktest 2.4.4
integrations/hftbacktest/upstream/verify.sh                # optional: builds the #312-patched wheel and checks it
integrations/hftbacktest/setup_venvs.sh .scratch/hbt312/wheel/hftbacktest-2.4.4-*.whl   # .scratch/hbt-venv-312
```

`setup_venvs.sh` installs `hftbacktest==2.4.4` with `--no-deps` (as `make hbt-crosscheck` does; the drivers do not
need its plotting dependencies) and numba 0.68.0, llvmlite 0.50.0 and numpy 2.5.3, the versions the documented
results ran on.

## 1. Export calibrated parameters

```sh
queuetruth calibrate alloc --family power3 --data DATASET --shadows SHADOWS --train 2019-12-30 --test 2020-01-30 \
    --out out/calib-power3
python integrations/hftbacktest/export_config.py --calibration out/calib-power3 --calibration out/calib-power2 \
    --log --defaults --out hbt_queue_config.json
```

Each power-family calibration (`power`, `power2`, `power3`; allocation quasi-likelihood, spec 5.4 C1) gives a
`fitted` entry; `--defaults` adds the family at `n = 2` (the baseline the held-out comparison used) and `--log` adds
`log_prob_queue_model()` and `log_prob_queue_model2()`, which have no parameter but whose quasi log likelihood on
the same train events the calibration reports. Several calibrations in one config must come from the same data,
shadows, train window, L2 view, exchange and train events, or the export refuses (`QT-CONFIG-001`): only then are
their likelihoods comparable. A calibration that queuetruth's trust check refuses (for example one that does not
beat the default out of sample) is exported only with `--force` (exit 1 without it, as `queuetruth export`), and
then carries a `NOT VALIDATED` note.

A fitted entry's `source` carries what you need to judge the parameter: both CIs and the per-stratum estimates
(`stratified`) on the `n` scale, the held-out Brier score, log loss and fill-rate bias per horizon of the fitted
model (the `default` entry carries the baseline's), the trust verdict and every trust flag. Non-refusing flags
become notes with the same text as `queuetruth export hftbacktest` prints, most importantly
`note (heterogeneous_strata)`: the instruments disagree about `n`. The format and its rules are in
[docs/design/extensions/hftbacktest-integration.md](../../docs/design/extensions/hftbacktest-integration.md).

In your hftbacktest code (any venv, with `hbt_config.py` importable as above):

```python
import hbt_config
from hftbacktest import BacktestAsset

cfg = hbt_config.load("hbt_queue_config.json")
entry = hbt_config.select(cfg, ["hbt_power3[exch=partial;mode=faithful;n=3.165744804349019]"])[0]
asset = BacktestAsset().data(data).tick_size(0.01).lot_size(1.0)  # your settings
asset = hbt_config.apply(asset, entry)  # .power_prob_queue_model3(3.165744804349019).partial_fill_exchange()
```

The `model_id` strings carry `n` at full precision and are valid `queuetruth zoo --models` ids, so the model you run
in hftbacktest is exactly the one queuetruth scored.

### Result on real data

Nasdaq TotalView-ITCH, AAPL, MSFT, SPY and TSLA, fitted on 2019-12-30 and held out on 2020-01-30 (the 2d-sampled-6000
shadows of `.realdata`; 58,620 allocation events from 2,398 train shadows, top-10 L2 view, faithful mode,
partial-fill exchange; each `calibrate alloc --threads 2` took 52 to 71 s wall time on a shared laptop):

| model | fitted n | bootstrap 95% CI | per instrument (95% CI) | train quasi log likelihood | trust verdict | in the config |
|---|---|---|---|---|---|---|
| `log2` | (none) | | | -1,919,225 | | yes (`--log`) |
| `power2` | 0.1634 | [0.1515, 0.1784] | AAPL 0.293 [0.291, 0.296], MSFT 0.131 [0.131, 0.132], SPY 0.177 [0.177, 0.178] | -1,951,214 | exportable, `heterogeneous_strata` | yes, with `n = 2` |
| `power3` | 3.1657 | [2.9841, 3.3438] | AAPL 2.186 [2.175, 2.197], MSFT 3.679 [3.673, 3.686], SPY 2.973 [2.969, 2.978] | -2,136,416 | exportable, `heterogeneous_strata` | yes, with `n = 2` |
| `power` | 1.1670 | [1.0649, 1.2600] | AAPL 1.033, MSFT 1.019, SPY 1.258 | -4,156,097 | refused: does not beat `n = 2` out of sample (worse log loss at 10 s) | only with `--force` |
| `log` | (none) | | | -5,790,713 | | yes (`--log`) |

(TSLA, 561 of the 58,620 events, has no stratum of its own in the calibration.) The per-instrument intervals do not overlap: one pooled `n`
does not describe these four symbols, and a backtest of a single symbol should refit on that symbol rather than use
the pooled value. The documented config has six entries: `power3` and `power2` fitted and at `n = 2`, `log` and
`log2`.

**Which entry to use.** The train quasi log likelihood scores where cancellations come from (the allocation target
of C1); it is not the fill-rate target. `log2` ranks first on it, yet in the reconciliation below it is pessimistic
by 0.4 to 0.7 percentage points at 1 to 60 s, like the fitted exponents. On the held-out day the fitted `power3`
and `power2` beat `n = 2` on the proper scores per order (the calibrations' held-out metrics; Brier at 1 s / 10 s / 60 s: `power3` 0.00940 / 0.01486 /
0.00709 vs 0.00974 / 0.01555 / 0.00745; `power2` 0.00942 / 0.01601 / 0.00707 vs 0.01043 / 0.01672 / 0.00744), but
their held-out fill-rate bias is further below zero (at 10 s: `power3` -0.93 pp vs -0.66 pp, `power2` -0.82 vs
-0.53). If you care about the aggregate fill rate, `n = 2` of either family is the closer choice on this data; if
you care about which individual orders fill, the fitted values are. Each entry's `source.held_out` has these numbers
for every horizon, and the realism tables of section 2 give them against exact truth on all orders.

The exportable `power3` verdict is marginal. RD-9 in [docs/RESULTS.md](../../docs/RESULTS.md) fits the same family on
the same shadows but on train blocks 0-3 of 5 of day one (45,902 events): `n = 3.098`, refused because it is worse
than `n = 2` at 60 s (Brier 0.00743 vs 0.00741) and 5 min. Here the fit uses all of day one (58,620 events): `n =
3.166`, better at 1 s, 10 s and 60 s and tied (to 1e-9 relative) at 100 ms and 5 min. A small change in the train
window flips the verdict.

## 2. Reconcile hftbacktest with queuetruth

```sh
python integrations/hftbacktest/reconcile.py --data DATASET --shadows SHADOWS --config hbt_queue_config.json \
    --out out/recon --hbt stock=.scratch/hbt-venv/bin/python --hbt patched=.scratch/hbt-venv-312/bin/python \
    --reference patched
```

Under `--out` it caches the exact replay, a faithful zoo run of the config's models, one hftbacktest npz per
instrument-day (`queuetruth export l2-npz`, the zoo's L2 view) with an orders sidecar (every shadow the zoo ran, as a
GTX order submitted at its activation time at its resolved price and cancelled at its planned end), and the
hftbacktest runs of each `--hbt` build. `manifest.json` records what the cache was computed from (dataset id,
shadows hash, L2 view, models, and each build's hftbacktest version and native-module hash); a run with other inputs
on the same `--out` is refused unless `--fresh`. `reconciliation.json` and `reconciliation.md` then report, per
model:

- **engine parity**: orders whose whole fill sequence in hftbacktest equals queuetruth's faithful model, and orders
  with the same first fill. A build that stopped is compared only on the orders live before its stop, on their fills
  before it, and the counts say how many orders that is;
- **#312**: where each build stopped, the orders live before the stop, the first queuetruth `hbt_312` zombie fill of
  that instrument-day, whether the stopped build's fills before the stop equal the reference build's, and whether
  the build stops at the abort time queuetruth predicts for its first aborted pair;
- **fill realism** (builds that ran to the end): per horizon, exact first fill within `h` against hftbacktest's, over
  the orders whose label is defined, with the paired fill-rate difference and a cluster-robust 95% CI; the same for
  the full fill and per session date; and the distribution of hftbacktest's first-fill time minus the exact one.

`per_order.parquet` has one row per model, build and order (exact, hftbacktest and queuetruth-faithful first fill,
full fill and filled quantity, and the differences), to see which orders a model gets wrong and by how much.

`hbt_driver.py` is the hftbacktest side: a numba loop that steps every feed timestamp while an own order is live and
jumps between actions otherwise. It reproduces the per-row O-ZOO-3 driver of `python/tests/zoo/hbt_crosscheck.py`
exactly: identical fills and stop codes on all 2,988 runs of the O-ZOO-3 scenarios, with the released and with the
patched build (`evidence/synthetic_regression.py`), and on random feeds in the tests. It records the hftbacktest
version and the sha256 of the native module it ran, so a result is tied to a binary.

### Result on real data

The same two days and four symbols: 5,795 orders (of the 6,000 shadows, 205 would have crossed the book on arrival
and are left out, as hftbacktest would expire them as GTX), 8 instrument-days, the six models of the config above,
the released hftbacktest 2.4.4 (`stock`, native module `b46e629b...`) and the build with the #312 fix (`patched`,
`6c81d997...`). The order flow: 694 to 750 GTX orders per instrument-day, at the touch or one or two ticks behind,
of 1 or 100 shares, each live 5 minutes, about 9 to 10 live at any time. A full `reconcile.py --fresh` run took
378 s on a shared laptop (exact replay 15 s, zoo 30 s, npz export 59 s, hftbacktest 88 s for the released build and
176 s for the patched one). Each hftbacktest process loads its npz in 0.04 to 1.2 s and compiles the numba loop
once in 2.8 to 9.7 s; after that one model over a whole instrument-day (456,298 to 4,517,301 feed rows, 179,692 to
2,063,512 loop steps) takes 0.28 to 3.2 s in `hbt_driver.py` with the patched build (median 0.55 s).

**Engine parity.** On the patched build, hftbacktest's fills equal queuetruth's faithful models on every order. The
released build stops early on every instrument-day, so only the few orders submitted before its stop can be
compared; those are identical too:

| model | patched build: identical fills | released 2.4.4: orders live before its stop | of which with a fill before the stop | identical before the stop | 2.4.4 instrument-days stopped by #312 | zoo pairs that reach #312 (of 6,000) |
|---|---|---|---|---|---|---|
| `hbt_power3[n=3.165744804349019]` (fitted) | 5,795 of 5,795 | 183 of 5,795 | 171 | 183 of 183 | 8 of 8 | 404 |
| `hbt_power3[n=2]` | 5,795 of 5,795 | 185 of 5,795 | 171 | 185 of 185 | 8 of 8 | 416 |
| `hbt_power2[n=0.1633981666582094]` (fitted) | 5,795 of 5,795 | 185 of 5,795 | 171 | 185 of 185 | 8 of 8 | 445 |
| `hbt_power2[n=2]` | 5,795 of 5,795 | 127 of 5,795 | 109 | 127 of 127 | 8 of 8 | 665 |
| `hbt_log` | 5,795 of 5,795 | 164 of 5,795 | 148 | 164 of 164 | 8 of 8 | 411 |
| `hbt_log2` | 5,795 of 5,795 | 185 of 5,795 | 171 | 185 of 185 | 8 of 8 | 440 |

(Model ids shortened: every one is `exch=partial;mode=faithful`.) Identical means the whole fill sequence of the
order, timestamps and quantities, per timestamp.

**#312 on real data.** With this order flow, 404 to 665 of the 6,000 orders per model (6.7 to 11.1%) reach the
#312 state within their life (queuetruth's faithful zoo flags them), and in hftbacktest any one of them stops the
whole backtest. The released 2.4.4 therefore stopped with error 14 on every instrument-day for every model, 7.3 to
26.6 minutes after the 09:30 open (median over the 8 instrument-days: 14.4 minutes for `power3` fitted and at
`n = 2`, `power2` fitted and `log2`; 13.6 for `log`; 12.1 for `power2` at `n = 2`), always at or after the first
`hbt_312` zombie fill that queuetruth records for that instrument-day. Every fill before the stop is identical to
the patched build's, and for each model the stop on the instrument-day of queuetruth's first aborted pair is exactly
at the abort time queuetruth predicts for that pair. How soon a run stops depends on how many orders it places: a
strategy with a few orders a day runs much longer, but each order that completes through a sub-lot or exact-leaves
maker fill and then sees another trade at its price ends the run.

**Fill realism.** The patched build runs every order to its end, so hftbacktest can be scored against the exact
replay on all 5,795 orders (the zoo's own leaderboard has to leave out the 802 shadows that reach #312 in some
model). First-fill rate bias, hftbacktest minus exact, in percentage points with the cluster-robust 95% CI
(clusters: instrument, date and 30-minute bucket), over both days:

| model | 1 s | 10 s | 60 s | 5 min |
|---|---|---|---|---|
| exact fill rate | 15.29% | 55.20% | 80.62% | 91.27% |
| `hbt_power3[n=3.165744804349019]` (fitted) | -0.52 [-0.74, -0.29] | -0.88 [-1.17, -0.59] | -0.71 [-1.04, -0.44] | -0.21 [-0.42, -0.05] |
| `hbt_power3[n=2]` | -0.33 [-0.56, -0.09] | -0.43 [-0.76, -0.10] | -0.38 [-0.68, -0.12] | -0.09 [-0.27, +0.05] |
| `hbt_power2[n=0.1633981666582094]` (fitted) | -0.41 [-0.65, -0.19] | -0.69 [-1.00, -0.37] | -0.52 [-0.83, -0.24] | -0.12 [-0.29, +0.02] |
| `hbt_power2[n=2]` | -0.14 [-0.40, +0.12] | +0.14 [-0.29, +0.61] | -0.02 [-0.36, +0.28] | +0.10 [-0.09, +0.27] |
| `hbt_log` | -0.38 [-0.63, -0.14] | -0.79 [-1.08, -0.45] | -0.47 [-0.77, -0.19] | -0.09 [-0.28, +0.07] |
| `hbt_log2` | -0.41 [-0.65, -0.19] | -0.66 [-0.97, -0.34] | -0.50 [-0.81, -0.24] | -0.12 [-0.29, +0.02] |

The fits used day one, so the held-out day matters most. On 2020-01-30 alone (2,916 orders):

| model | 1 s | 10 s | 60 s | 5 min |
|---|---|---|---|---|
| exact fill rate | 19.03% | 59.88% | 83.06% | 92.22% |
| `hbt_power3[n=3.165744804349019]` (fitted) | -0.62 [-0.97, -0.27] | -0.93 [-1.33, -0.51] | -0.48 [-0.85, -0.14] | -0.17 [-0.43, +0.07] |
| `hbt_power3[n=2]` | -0.51 [-0.88, -0.13] | -0.65 [-1.10, -0.14] | -0.24 [-0.57, +0.10] | -0.03 [-0.24, +0.17] |
| `hbt_power2[n=0.1633981666582094]` (fitted) | -0.55 [-0.91, -0.17] | -0.82 [-1.26, -0.35] | -0.31 [-0.63, +0.00] | -0.07 [-0.25, +0.13] |
| `hbt_power2[n=2]` | -0.38 [-0.78, +0.00] | -0.45 [-0.98, +0.10] | -0.10 [-0.45, +0.24] | +0.07 [-0.14, +0.30] |
| `hbt_log` | -0.51 [-0.91, -0.10] | -0.96 [-1.38, -0.52] | -0.27 [-0.59, +0.03] | -0.07 [-0.29, +0.14] |
| `hbt_log2` | -0.55 [-0.91, -0.17] | -0.82 [-1.26, -0.35] | -0.31 [-0.63, +0.00] | -0.07 [-0.25, +0.13] |

On the training day (2019-12-30, 2,879 orders) the fitted `power3` is at -0.42, -0.83, -0.94 and -0.24 pp and `n = 2`
at -0.14, -0.21, -0.52 and -0.14. Every model is within 1 pp of exact at every horizon on both days. On both days the
point estimates of the fitted exponents are more pessimistic than `n = 2` of the same family at every horizon; on the
held-out day the gap is 0.1 to 0.4 pp and the intervals overlap (the paired fitted-minus-default difference is not
computed here). `power2` at `n = 2` is the only model whose CI covers 0 at every horizon over both days.

Most of the pessimism is in the first fill, not the fill: for the full fill (the order completely filled within
`h`), every model except `power2` at `n = 2` has a CI that covers 0 at 60 s and 5 min (for example fitted `power3`
-0.12 [-0.38, +0.10] and +0.02 [-0.15, +0.19]). Over the whole order life, hftbacktest and the exact replay fill
the same orders almost always (for `log2`: 5,277 both, 12 exact only, 5 hftbacktest only), and 4,024 to 4,178 of the
orders both fill have their first fill at the same nanosecond; the 95th percentile of hftbacktest's first fill minus
the exact one is +26 ms for `power2` at `n = 2` and +239 to +855 ms for the other models. Where the exact queue
gives a partial fill first, one reading (not tested here) is that the estimated queue position reaches the front
later and then fills more at once.

### Reproduce the documented run

```sh
RD=.realdata    # the Nasdaq ITCH datasets and shadows of the real-data runs (docs/RESULTS.md)
for f in power power2 power3; do
  queuetruth --threads 2 calibrate alloc --family $f --data $RD/ds/nasdaq-2d \
    --shadows $RD/shadows/2d-sampled-6000.parquet --train 2019-12-30 --test 2020-01-30 --out .scratch/rd/calib-$f
done
python integrations/hftbacktest/export_config.py --calibration .scratch/rd/calib-power3 \
  --calibration .scratch/rd/calib-power2 --log --defaults --out .scratch/rd/hbt_queue_config.json
python integrations/hftbacktest/reconcile.py --data $RD/ds/nasdaq-2d --shadows $RD/shadows/2d-sampled-6000.parquet \
  --config .scratch/rd/hbt_queue_config.json --out .scratch/rd/recon --hbt stock=.scratch/hbt-venv/bin/python \
  --hbt patched=.scratch/hbt-venv-312/bin/python --reference patched --threads 2 --fresh
python integrations/hftbacktest/evidence/readme_tables.py .scratch/rd/recon/reconciliation.json
```

`readme_tables.py` prints every table of this section (medians are `statistics.median`). The documented run had zoo
run id `72d4e4207dde2eb6` and calibration run ids `51ddb045010023a4` (power), `6708a33682fbe3c1` (power2) and
`36cbd2f1bfda57b5` (power3). Its aggregate numbers (counts, rates, CIs and stop times, no per-order data) are in
[`evidence/results.json`](evidence/results.json), together with the synthetic regression. Outputs stay under
`.scratch` (market data and derived per-order files are never tracked).

## 3. How this relates to queuetruth's other hftbacktest paths

| path | use it to |
|---|---|
| `queuetruth export hftbacktest` | paste one calibrated model into your backtest: a Python snippet with the parameter (6 significant digits) and the trust header |
| `export_config.py` (this directory) | keep several models, their full-precision parameters, CIs, per-instrument estimates and held-out scores in one machine-readable file, and apply any of them with `hbt_config.apply` |
| `queuetruth export replay-schedule --target hftbacktest` | replay your own live orders in hftbacktest (10 ms steps) and score the backtest against your live fills (`import_sim("hftbacktest")`, `queuetruth score --sim-log`) |
| `reconcile.py` (this directory) | find which hftbacktest model is closest to exact truth on a dataset, and check that hftbacktest itself agrees with queuetruth's re-implementation |

To score your live orders with a model from the config, edit the replay script's `configure_asset` (it is called
when no `--snippet` is given):

```python
import sys

sys.path.insert(0, "/path/to/queuetruth/integrations/hftbacktest")
import hbt_config

ENTRY = hbt_config.select(hbt_config.load("hbt_queue_config.json"), ["hbt_power3[exch=partial;mode=faithful;n=2]"])[0]


def configure_asset(asset):
    return hbt_config.apply(asset, ENTRY).constant_order_latency(0, 0)  # your latency and fees here
```

`hbt_driver.py` output does not go to the scorer; the replay script is the path for live orders.

## 4. hftbacktest #312

hftbacktest's `partial_fill_exchange` marks an order `Filled` when a maker fill leaves less than half a lot, but takes
it out of the book only when the queue model's fill is strictly larger than the leaves. The completed order stays in
the book and its next fill attempt makes `elapse` return 14 (`InvalidOrderStatus`), which stops the whole backtest.
[UPSTREAM.md](UPSTREAM.md) has the root cause, why the one-line `>` to `>=` change of the report and of the open pull
request #323 misses the report's own sizes (lot 0.01, qty 0.1), the proposed patch, its 7 Rust and 5 Python tests,
the synthetic and real-data evidence, and how it relates to #323. `upstream/repro_312.py` reproduces it in seconds:

```console
$ .scratch/hbt-venv/bin/python integrations/hftbacktest/upstream/repro_312.py
hftbacktest 2.4.4
  equal_leaves      ABORT: elapse returned 14 (InvalidOrderStatus); last order state {...'status': 'filled'...}
  float_residual    ABORT: elapse returned 14 (InvalidOrderStatus); ...
  non_lot_quantity  ABORT: elapse returned 14 (InvalidOrderStatus); ...
  trade_through     ABORT: elapse returned 14 (InvalidOrderStatus); ...
```

## Files

| path | what |
|---|---|
| `hbt_config.py` | `qtint.hbt_queue_config` format: build, validate, load, select, `apply` to a `BacktestAsset` (stdlib only) |
| `export_config.py` | calibrations to config (queuetruth venv) |
| `hbt_driver.py` | runs hftbacktest on an npz and orders sidecar, records fills, stops and the engine identity (hftbacktest venv) |
| `reconcile.py` | the real-data reconciliation (queuetruth venv; calls `hbt_driver.py` in each hftbacktest venv) |
| `setup_venvs.sh` | creates the hftbacktest venvs |
| `evidence/` | `synthetic_regression.py` and `driver_parity.py` (the O-ZOO-3 regression and driver parity), `pos316.py` (#316), `readme_tables.py` (the tables above), `results.json` (aggregates of the documented runs) |
| `upstream/` | #312: patch, reproduction, `verify.sh`, the wheel build's pinned `Cargo.lock` |
| `tests/` | pytest suite; the tests that need hftbacktest run it in the venvs and skip with a reason when they are missing |

## Tests

```sh
.venv/bin/python -m pytest integrations/hftbacktest/tests
```

`QT_HBT_PY` and `QT_HBT_PY_312` point the tests at other venvs (defaults `.scratch/hbt-venv` and
`.scratch/hbt-venv-312`). The suite is not yet part of `make test` (see the extension page for the requested
Makefile target).

## Limits

- One exchange model per reconciliation run (`partial` or `no_partial`), zero latency and fees, own orders that do
  not interact (each order sees every trade in full, as in hftbacktest and the zoo). Latency calibrations are not
  exported here; `queuetruth export hftbacktest` already writes `intp_order_latency` lines.
- The log families are exported without a parameter: there is nothing to fit, only a likelihood to compare.
- hftbacktest #316 (partial fills of a resting order reach `position()` only when the order completes) is
  documented in UPSTREAM.md but not fixed; the reconciliation compares fills, not positions.
- The patched build is a local wheel. With the order flow above (about 700 orders per instrument-day, 9 to 10 live
  at once), every instrument-day stopped within 27 minutes of the open on the released 2.4.4 with
  `partial_fill_exchange`; a strategy with fewer orders stops later, or not at all on a given day.
