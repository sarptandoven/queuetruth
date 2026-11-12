# Score and calibrate my hftbacktest backtest

**Goal:** find out how far hftbacktest's fills are from the exact queue for your orders and your `BacktestAsset`
settings, replace a guessed `power_prob_queue_model3(n)` with an `n` fitted against what really happened in the
queue, check it on data it was not fitted on, and paste the result into your backtest.

**You need:** order-level (L3 or MBO) data for the venue you backtest (Nasdaq ITCH, IEX DEEP+, Databento DBN MBO,
or your own Coinbase, Bitstamp or Bitfinex L3 captures) as a queuetruth dataset (`queuetruth data add`, see
[step 1 of the recipe by hand](#1-ingest-and-check-the-data)); `pip install queuetruth-hftbacktest` (it installs
queuetruth); and, for scoring, a separate venv with hftbacktest 2.4.4.

If your venue only publishes L2 (for example Binance), there is no exact truth to fit against. Use
[Calibrate on an L2-only venue against my own fills](calibrate-live-l2-venue.md) instead. If you have both, do
both: this page gives the parameter, the other checks that it transfers to your venue and feed.

No data yet? The [Fill-Realism Atlas](../concepts/fill-realism-atlas.md) scores hftbacktest's models against the
exact queue on free IEX DEEP+ data, by liquidity tier, and names the closest ProbQueue setting. Its L3 FIFO model
fills fewer orders than the exact queue because of hftbacktest's own rules, not a modelling error: see
[the L2 model zoo](../concepts/l2-model-zoo.md#c-hbt_l3_fifo-order-level-input).

For the fitted setting alone, in one command and without the plugin, use
`queuetruth fit --backtester hftbacktest` ([Fit my backtester's fill model](fit-my-backtester.md)). This page goes
further: it scores hftbacktest itself, compares the power families, and checks the fit by queue position.

The page has three parts. The plugin, `queuetruth-hftbacktest`, does the work in a few calls: it scores hftbacktest
itself against the exact queue and calibrates its queue model. Then the same calibration step by step with
queuetruth's own commands, on the synthetic dataset, so you can see every file. Then that recipe on your own data,
with the checks the plugin does not do (the queue-decile check and the latency fit). The method behind the
calibration (teacher-forced allocation quasi-likelihood, the bootstrap, the held-out protocol and the trust check)
is in [Calibration](../concepts/calibration.md).

## The plugin

`queuetruth-hftbacktest` wraps queuetruth's own commands (`replay`, `export l2-npz`, `export replay-schedule`,
`live import --venue sim-hftbacktest`, `referee run`, `shadows sample`, `calibrate alloc`, `export hftbacktest`) and
adds no model of its own. Its source, tests and release workflow are in
[`integrations/hftbacktest/plugin`](https://github.com/sarptandoven/queuetruth/tree/main/integrations/hftbacktest/plugin).
The code in this part needs the plugin installed, so the docs check only checks its syntax. The same steps run on
the synthetic dataset in the plugin's tests (`make test-hftbacktest-plugin`), including hftbacktest itself when its
venv is there.

<!-- check-docs: skip (installs packages) -->
```sh
pip install queuetruth-hftbacktest
```

### Set up hftbacktest in its own venv

hftbacktest pins numba and numpy versions that are not queuetruth's, so it runs in its own venv and the plugin calls
it by path. These are the versions the plugin is tested with:

<!-- check-docs: skip (installs packages) -->
```sh
python -m venv hbt-venv
hbt-venv/bin/pip install numba==0.68.0 llvmlite==0.50.0 numpy==2.5.3
hbt-venv/bin/pip install --no-deps hftbacktest==2.4.4
```

In a clone of queuetruth, `integrations/hftbacktest/setup_venvs.sh` makes the same venv in `.scratch/hbt-venv`.

### 1. Score hftbacktest against the exact queue

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
(`queuetruth advanced shadows from-live`, see [Import fills from my venue or broker](import-live-logs.md)) or orders placed
like yours:

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth advanced shadows sample --data my-dataset --n 3000 --out my-orders.parquet \
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
`queuetruth advanced export hftbacktest`) or a build with the fix (see the
[hftbacktest integration](https://github.com/sarptandoven/queuetruth/tree/main/integrations/hftbacktest)).

### 2. Calibrate the queue model

<!-- check-docs: skip (needs the plugin and user data) -->
```python
import queuetruth_hftbacktest as qthbt

cal = qthbt.calibrate("my-dataset", train="2026-10-12,2026-10-13", test="2026-10-14", out="hbt-cal",
                      orders="my-orders.parquet")
print(cal.text())
print(cal.code)
```

For each of `power3`, `power2` and `power` (`families=`), `calibrate` runs `queuetruth advanced calibrate alloc` on the train
window and scores the fitted `n` and the default `n = 2` on the test window. Among the fits queuetruth's trust check
lets through, the one with the smallest held-out log loss at 60 s (`horizon_ns=`) wins. `cal.text()` starts with the
line to use, then a table of every family: the fitted `n`, its bootstrap 95% interval, the held-out log loss and
fill-rate bias at that horizon for the fitted value and for `n = 2`, and the trust verdict with its reasons.

`cal.code` is the `queuetruth advanced export hftbacktest` snippet of the winner, unchanged, also written to
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

### From the command line

<!-- check-docs: skip (needs the plugin, user data and hftbacktest) -->
```sh
queuetruth-hftbacktest prepare --data my-dataset --orders my-orders.parquet --out hbt-check
queuetruth-hftbacktest run hbt-check --python hbt-venv/bin/python --snippet my_asset.py
queuetruth-hftbacktest score hbt-check
queuetruth-hftbacktest calibrate --data my-dataset --train 2026-10-12,2026-10-13 --test 2026-10-14 --out hbt-cal
```

`--format json` prints the result document (or a `qt.error` document on failure). Exit codes follow `queuetruth`:
0 success, 1 when no calibrated model passes the trust check (`QT-CALIB-001`), 2 any other error.

## Step by step on the synthetic dataset

This is what the plugin's `calibrate` runs for one family, with queuetruth's own commands, plus the checks it leaves
to you.
Every block in this section runs as written (the docs check executes them in order in one empty directory).
The data is the synthetic quickstart dataset: CME rules, 2 instruments, 2 session days (2026-06-15 and
2026-06-16). Its numbers are not a result about any real venue; they only show what the files look like.

### 1. Make the data and check the replay

<!-- check-docs: run -->
```sh
queuetruth advanced fixtures synth --out synth
queuetruth advanced replay --data synth --out synth-replay --parity-trades
```

<!-- check-docs: run -->
```python
import json

s = json.load(open("synth-replay/summary.json"))
bad = {k: v for k, v in s["invariants"].items() if v}
print("invariants not zero:", bad or "none")
print("trade parity pass:", s["parity"]["pass"])
print("data-quality verdict:", s["data_quality"]["verdict"])
assert not bad and s["parity"]["pass"] and s["data_quality"]["verdict"] != "unusable"
```

### 2. Sample shadows, fit `n` on day 1, score it on day 2

<!-- check-docs: run -->
```sh
queuetruth advanced shadows sample --data synth --n 2000 --out shadows.parquet \
    --placements 0:0.7,-1:0.3 --qty lots:1:0.5,lots:5:0.5
queuetruth advanced calibrate alloc --family power3 --data synth --shadows shadows.parquet \
    --train 2026-06-15 --test 2026-06-16 --out cal-power3
```

With two days, one day trains and the other tests. The terminal shows the answer first: the fitted value with its
profile and bootstrap intervals, a table of held-out log loss and fill-rate bias for the fitted value and the
default per horizon with a `beats default` column, any trust flags, and a verdict line. On this synthetic data the
fitted `n` loses to the default `n = 2` at 60 s, so the verdict is `DO NOT USE (export refuses it:
not_beating_default)`. `calibrate alloc` writes `cal-power3/calibration.json` (the
result), `fit.json` (the Rust fit alone, with empty `held_out` and `exports`), `alloc_events.parquet`,
`touch_records.parquet` and `held_out/` (the zoo run on the test window).

<!-- check-docs: run -->
```python
import json

c = json.load(open("cal-power3/calibration.json"))
print(c["family"], c["param_name"], "=", c["estimate"])
print("bootstrap CI (primary):", c["ci_bootstrap"])
print("profile CI (quasi):    ", c["ci_profile"])
print("events / shadows used: ", c["n_events"], "/", c["n_shadows"])
print("train", c["train"], "test", c["test"])
(fitted_id, fitted), = c["held_out"].items()
(base_id, base), = c["baseline_held_out"].items()
def f(x):
    return "null" if x is None else f"{x:.5f}"

print("horizon_ns      logloss fitted / n=2     fill-rate bias fitted / n=2")
for h in fitted["logloss"]:
    print(f"{h:>14}  {f(fitted['logloss'][h])} / {f(base['logloss'][h])}"
          f"     {f(fitted['fill_rate_bias'][h])} / {f(base['fill_rate_bias'][h])}")
```

### 3. Re-check the fitted model by queue decile

The fit scores each cancel one step at a time at the exact queue state. Running the full model on its own can
drift, so run it through the zoo next to the default and look at where it is still wrong:

<!-- check-docs: run -->
```sh
N=$(python -c 'import json; print(json.load(open("cal-power3/calibration.json"))["estimate"])')
queuetruth advanced zoo --data synth --shadows shadows.parquet --out zoo-check \
    --models "hbt_power3[n=2],hbt_power3[n=$N]"
```

<!-- check-docs: run -->
```python
import pyarrow.parquet as pq

rows = [r for r in pq.read_table("zoo-check/zoo_errors.parquet").to_pylist()
        if r["metric"] == "fill_rate_bias" and r["stratum_kind"] == "queue_decile"
        and r["horizon_ns"] == 60_000_000_000]
def f(x):
    return "null" if x is None else f"{x:+.4f}"

for r in sorted(rows, key=lambda r: (r["model_id"], int(r["stratum"]))):
    print(f"{r['model_id']}  decile {r['stratum']}: bias {f(r['value'])} "
          f"[{f(r['ci_lo'])}, {f(r['ci_hi'])}]  n={r['n']}")
```

### 4. Export the snippet, or see why it is refused

`queuetruth advanced export` is fail-safe: it refuses to write a parameter that is not safe to paste. A calibration is
exportable only if it has a finite estimate from at least 30 events, carries no refusing trust flag
(`at_grid_edge`, `flat_likelihood`, `not_identified`, `degenerate_ci`, `own_orders_in_depth`), and beats the
library's default out of sample: at every held-out horizon it is not worse than the default on log loss and Brier
score, and it is strictly better somewhere. The verdict is stored in `calibration.json` under `trust` (`verdict`,
`reasons`, `flags`, and `beats_default` per horizon: `better`, `tie`, `worse`, `mixed` or `n/a`).

On this synthetic data the fitted `n` is worse than `n = 2` at 60 s, so the export refuses with `QT-CALIB-001` and
exit code 1:

<!-- check-docs: run exit=1 -->
```sh
queuetruth advanced export hftbacktest --calibration cal-power3/calibration.json --out hbt_snippet.py
```

<!-- check-docs: run -->
```python
import json

t = json.load(open("cal-power3/calibration.json"))["trust"]
print("verdict:", t["verdict"], t["reasons"])
print("beats default by horizon (ns):", t["beats_default"])
```

`--force` writes the snippet anyway, under a `# !! NOT VALIDATED` header that lists the reasons, and repeats the
reasons on stderr. Use it to inspect a fit, not to paste it:

<!-- check-docs: run -->
```sh
queuetruth advanced export hftbacktest --calibration cal-power3/calibration.json --out hbt_forced.py --force
head -8 hbt_forced.py
queuetruth advanced export l2-npz --data synth --instrument-id 1 --date 2026-06-16 --out synth_l2_20260616.npz
```

The snippet text is also stored in `calibration.json` under `exports.hftbacktest.python` (with the same header when
the verdict is `refused`), and its notes under `exports.hftbacktest.notes`; `export` prints the notes on stderr as
`warning:` lines.

## Step by step on your own data

The commands below need your data, so the docs check only checks their syntax.

### 1. Ingest and check the data

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth data add coinbase_full_20261012.jsonl.gz --input-format coinbase --venue coinbase --out cb-ds
queuetruth advanced replay --data cb-ds --out cb-replay --parity-trades
```

Source formats: `itch`, `dbn`, `etcsv`, `parquet`, `bitstamp`, `bitfinex`, `coinbase` (and `tardis-l2`, which is
L2 only and cannot be used here). Open `cb-replay/summary.json` and check `invariants` (every counter zero),
`parity.pass` and `data_quality.verdict` (`clean`, `warnings` or `unusable`). Do not calibrate on `unusable`: the
fit would be against a broken book. The dataset's `validation_status` travels into the export header: `nasdaq`
rules are `real_data`, `coinbase` and `generic` are `assumed`, `cme` is `synthetic_only`.

### 2. Place shadows that look like your orders

The fit uses shadows, so place them where your strategy places orders. If you quote at the touch and one tick
behind with sizes of 1 to 5 lots:

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth advanced shadows sample --data cb-ds --n 20000 --out cb-shadows.parquet \
    --placements 0:0.7,-1:0.3 --qty lots:1:0.5,lots:5:0.5
```

About `--lifetime`: the held-out comparison in `calibration.json` covers only shadows with no planned cancel before
the maximum horizon (`--h-max`, default 300 s). With a shorter `--lifetime`, the horizons beyond it are not scored
(the verdict says `not scored at 60s, 5m`), and the fit cannot show that it beats the default there. To check
cancelling orders, run `zoo` (step 4) and read the `cancel_bearing/` metrics in `zoo_errors.parquet`, where your own
cancel is a competing risk.

If you already trade this venue, you can use your real orders instead:
`queuetruth advanced shadows from-live --live live/ --out cb-shadows.parquet` (see
[Import fills from my venue or broker](import-live-logs.md) for building `live/`). On an MBO venue your real orders
are also in the tape. `calibrate alloc` has no flag to remove them, so each shadow sits behind its own twin; the zoo
can (`zoo --exclude-order-ids FILE`, one MBO order id or `instrument_id,order_id` per line).

### 3. Fit on one window, score on another

<!-- check-docs: skip (needs user data) -->
```sh
for fam in power power2 power3 databento; do
  queuetruth advanced calibrate alloc --family $fam --data cb-ds --shadows cb-shadows.parquet \
      --train 2026-10-12,2026-10-13 --test 2026-10-14 --out cal-$fam
done
```

With a single day, split it into blocks of the session: `--train blocks:0-3/5 --test blocks:4-4/5` fits on the
first four of five equal blocks and tests on the last. Without `--test`, the held-out metrics are computed on all
data, train window included, and a warning says so.

Each `cal-<family>/calibration.json` is a `qt.calibration_result`
([schema](../reference/schemas.md)). The fields that matter here:

| Field | Meaning |
|---|---|
| `family`, `param_name`, `estimate` | the fitted parameter in natural units: `n` for the power families, `k` for `databento` |
| `ci_bootstrap` | primary 95% interval: block bootstrap over session days, or 30-minute blocks with fewer than 5 days (200 resamples by default, `--bootstrap-b`) |
| `ci_profile` | likelihood-ratio interval, labelled quasi: narrower than the truth; `null` on a side that reaches the grid edge |
| `loglik`, `n_events`, `n_shadows` | maximized quasi log-likelihood, allocation events used, train shadows with at least one event |
| `train`, `test` | the windows, as `dates` or `blocks` (`index`, `n_blocks`) |
| `held_out` | for the fitted model on the test window, per horizon in ns: `brier`, `logloss`, `fill_rate_bias` |
| `baseline_held_out` | the same for the default (`n = 2` for the power families, `k = -0.8`) |
| `stratified` | estimates by instrument and by distance from the touch; empty when no stratum reaches 1,000 events |
| `trust` | `verdict` (`exportable` or `refused`), `reasons`, `flags`, `beats_default` per horizon |
| `exports` | `hftbacktest` and `nautilus` snippets (`python`, `notes`), `latency_npz` |
| `data_sources` | `dataset_id`, `venue`, `source_venue`, `validation_status` |
| `warnings` | includes the quasi log-likelihood of `hbt_log` and `hbt_log2` for comparison |

`trust` records whether the fit is exportable (step 6). Even when it is, keep a fitted parameter only if its
held-out log loss and fill-rate bias are better than the default's at the horizons your strategy cares about. Compare families the same way: the one with the smallest held-out log loss
wins, not the one with the largest training likelihood (the families have different likelihood scales). If the
fitted `databento` `k` is above 0, the export has no hftbacktest line for it (see step 6).

Two caveats, both explained in [Calibration](../concepts/calibration.md):

- **Teacher forcing.** The fit scores each cancel at the exact queue state, not at the model's own running
  estimate. A parameter that allocates single cancels well can still drift when the model runs on its own, which
  is why the held-out check runs the full model and why step 4 exists.
- **Quasi-likelihood interval.** Cancel sizes are treated as independent units, which they are not, so
  `ci_profile` is too narrow. Quote `ci_bootstrap`. With 2 to 4 days the bootstrap resamples 30-minute blocks,
  which is weaker than resampling days.

`queuetruth report --in cal-power3 --out cal.html` shows the estimates, the likelihood profile and the held-out
Brier comparison.

### 4. Look at where it is still wrong

<!-- check-docs: skip (needs user data) -->
```sh
N=$(python -c 'import json; print(json.load(open("cal-power3/calibration.json"))["estimate"])')
queuetruth advanced zoo --data cb-ds --shadows cb-shadows.parquet --out zoo-check \
    --models "hbt_power3[n=2],hbt_power3[n=$N],hbt_risk_adverse" --write-trajectory
queuetruth report --in zoo-check --out zoo-check.html
```

Read fill-rate bias **by queue decile** (`zoo_errors.parquet`, `stratum_kind = "queue_decile"`, as in the
synthetic walkthrough, or the report). A single `n` can overfill at the back of the queue and underfill at the
front; check that your fitted value is balanced where your orders sit. The zoo's `summary.json` `warnings` also
list, per model, how many shadows would have aborted a real hftbacktest run with `InvalidOrderStatus` (issue #312);
the zoo scores each shadow as its own backtest.

### 5. Latency (optional)

If you have a live order log with local send, exchange and local receive timestamps:

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth advanced calibrate latency --live live/ --out cal-latency
queuetruth advanced export latency-npz --live live/ --out order_latency.npz
```

`cal-latency/calibration.json` (kind `latency`) holds p10, p50, p90 and p99 of the entry, response, cancel and
amend latencies under `latency`, and `calibrate latency` also writes `cal-latency/latency.npz`. One-way latencies
assume synchronized clocks (`clock_sync_assumed: true`). `order_latency.npz` has hftbacktest's `IntpOrderLatency`
layout (`req_ts`, `exch_ts`, `resp_ts`, `_padding`), one row per submitted order, sorted by `req_ts`.

A queue-model calibration and a latency calibration are separate results; no flag merges them. The
snippet from `cal-power3` therefore has no latency line. Add `.intp_order_latency("order_latency.npz")` to the
chain yourself, or take that line from `export hftbacktest --calibration cal-latency/calibration.json`; the path in that line is
the bare file name `latency.npz`, so point it at `cal-latency/latency.npz`.

### 6. Export and paste

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth advanced export hftbacktest --calibration cal-power3/calibration.json --out hbt_snippet.py
```

If the calibration is refused, the command exits 1 with `QT-CALIB-001` and the reasons, and writes nothing (see
step 4 of the synthetic walkthrough). Otherwise the file has this layout (`<...>` marks values from your run):

```text
# queuetruth 0.1.0 | calibration <run id> | power3 fitted on 2026-10-12,2026-10-13; held-out 2026-10-14
# data: coinbase (assumed, dataset <first 12 hex of the dataset id>)
# fit: n = <estimate> (alloc); <events> events from <shadows> shadows; bootstrap CI [<lo>, <hi>]; profile CI [<lo>, <hi>]
# held-out: <the beats-default sentence>
# exchange: no_partial_fill_exchange, which runs on the released hftbacktest 2.4.4. The fit simulated
# partial_fill_exchange; ... (hftbacktest #312) ...
# Paste into your existing BacktestAsset chain (keep your data/tick/lot/fee settings).
from hftbacktest import BacktestAsset
asset = (
    BacktestAsset()
    # ... your existing .data(...), .tick_size(...), .lot_size(...), fee model ...
    .power_prob_queue_model3(<estimate>)
    .no_partial_fill_exchange()
)
```

The value is printed with 6 significant digits. The builder follows the family: `power_prob_queue_model`,
`power_prob_queue_model2`, `power_prob_queue_model3`; `databento` with `-1 < k <= 0` maps to
`power_prob_queue_model3(1/(1+k))`, an expected-value approximation of the Monte Carlo estimator (a note says
so); for `k > 0` the queue-model line is omitted with a note. The exchange line is `.no_partial_fill_exchange()`,
which runs on the released hftbacktest 2.4.4: with `.partial_fill_exchange()` 2.4.4 stops the backtest with error 14
(hftbacktest #312). When the fit simulated `partial` (`--exchange partial`, the default of `calibrate`), the header
says so; the two differ only when a fill is smaller than the order. In Python,
`hftbacktest_snippet(cal, exchange="partial")` writes `.partial_fill_exchange()` for a build with the fix. Keep
`--mode faithful` (the
default) for parameters you will use in hftbacktest: the fit then reproduces hftbacktest's own behaviour, quirks
included. Layouts are checked in `test_hftbacktest_golden` and `test_hftbacktest_builders`
(`python/tests/export/test_export_text.py`); `test_hftbacktest_snippet_runs_in_hftbacktest` builds the chain in a
real hftbacktest 2.4.4 when one is installed for the tests.

The notes, printed on stderr and stored in `exports.hftbacktest.notes`, always include these two
(`python/queuetruth/export/hftbacktest.py`):

- hftbacktest #316/#312: released versions apply partial fills of resting orders to position only when the order
  completes, and a fill exactly equal to leaves can raise `InvalidOrderStatus`. Check partial-fill accounting in
  your version before comparing fill quantities.
- The parameter is specific to the feed, depth and conflation it was fitted on. Refit when the data source changes.

To check that your installed hftbacktest behaves like queuetruth's faithful model on the same L2 view, export that
view and run your backtest on it:

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth advanced export l2-npz --data cb-ds --instrument-id 1 --date 2026-10-14 --out cb_l2_20261014.npz
```

`--l2-depth`, `--l2-cadence-ns` and `--l2-trades` must match the fit (defaults: 10 levels, no conflation, trades
before depth); `--feed-latency-ns` sets `local_ts = ts_recv + latency`.

## Limits

- One hftbacktest order per scheduled order. `prepare` leaves out later legs of a re-priced live order; to replay
  those, use `queuetruth advanced export replay-schedule` and `queuetruth advanced score --sim-log` directly.
- The per-horizon comparison is of first fills, at the replay step's time resolution.
- A fitted `n` is specific to the venue, feed, depth and orders it was fitted on: refit when they change.

## Data licences

Calibrating on vendor data for your own backtests is usually internal use, but you are responsible for your
licence, especially before you publish or share fitted parameters. See
[Data and licensing](../data-and-licensing.md).
