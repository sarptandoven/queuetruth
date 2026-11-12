# Fit my backtester's fill model

**Goal:** get the fill-model setting that makes your backtester's fills match reality, with a verdict on whether to
trust it. `queuetruth fit` fits the setting on part of your data, compares it with the library's default on the part
it did not see, and writes the line to paste only when the fit wins there. When it does not, it says why and what
would make it pass.

**You need:** queuetruth 0.4.0 or later, and order-level (MBO) data of your market as a queuetruth dataset (`queuetruth data add`; see
[Replay a public IEX DEEP+ day](iex-public-demo.md) for free data), or your own live fills with the L2 or MBO data of
the same period.

| `--backtester` | Setting it fits | Default it must beat |
|---|---|---|
| `hftbacktest` | `power_prob_queue_model3(n)` (`--family power2`, `power` for the other power models) | `n = 2` |
| `nautilus` | `FillModel(prob_fill_on_limit=p)` (`--family nt-queue-l2-prob-touch` for `queue_position=True`) | `p = 1` |
| `databento` | `k` of Databento's queue-position estimator | `k = -0.8` |

To find out first how wrong your backtest is, see [Check my backtest](check-my-backtest.md).

## 1. Fit on order-level data

This page uses the synthetic dataset that ships with queuetruth (two futures, two days, no market data).

<!-- check-docs: run -->
```bash
queuetruth advanced fixtures synth --out synth > /dev/null
queuetruth fit --backtester nautilus --data synth
```

The first line is the answer. On this synthetic run it reads:

```text
Use p = 0.5221 (95% CI 0.4839 to 0.5715) for NautilusTrader prob_fill_on_limit: on held-out data (2026-06-16, 500
shadows) it beats the default p = 1; at 60s the fill rate is off by +2.8 pp with the fit against +9.2 pp with the
default, log loss 89% lower. Snippet: qt-fit/nautilus.py
```

Then come the fit (its events and family), the split, a table of the held-out comparison at each horizon, the
verdict and what to paste. For NautilusTrader that is the snippet in `qt-fit/nautilus.py` and one line for the
[queuetruth-nautilus](https://github.com/sarptandoven/queuetruth/blob/main/integrations/nautilus/README.md) plugin:

```python
import queuetruth_nautilus as qtn

engine.add_venue(..., **qtn.venue_options(qtn.load_calibration("qt-fit/calibration/calibration.json")))
```

What the numbers mean:

- **Shadows.** Without `--shadows FILE`, `fit` samples 1,000 resting orders from the dataset (`--n-shadows N`) and
  replays them in the exact queue, which is the truth it fits to.
- **Interval.** The 95% block-bootstrap interval of the fitted value (a profile interval when there is no bootstrap
  one, labelled so).
- **Held out.** The fitted and the default setting are both run on the orders of the held-out window. The fill-rate
  error is the model's fill rate minus the exact one; log loss scores each order's predicted fill probability (lower
  is better). The fit passes when it is better at one horizon at least and worse at none.

The verdict is the same fail-safe check `queuetruth advanced export` applies: enough events, a usable interval, a value
inside the range the fit searches, and a win over the default on held-out data. The exit code is 0 when the fit
passes and the snippet is written, 1 when it is refused, 2 on an error.

## 2. When the fit is refused

<!-- check-docs: run exit=1 -->
```bash
queuetruth fit --backtester hftbacktest --data synth --out qt-fit-hbt
```

```text
Do not use n = 0.6439 (95% CI 0.56 to 0.7207) for hftbacktest power_prob_queue_model3 yet: on held-out data
(2026-06-16, 500 shadows) it does not beat the default n = 2 (worse at 60s). Keep the default; no snippet written.
```

No snippet is written. The report lists why (each reason of the verdict) and what would make the fit pass. Where the
reason is too little data, it says how much more: how many more orders the train window needs for 30 informative
events, how many more minutes of data the interval needs (it resamples half-hours of data, or days once there are
5), or how many more held-out orders a verdict needs. Where the fit simply does not beat the default on plenty of
held-out data, the default is as good on this data: keep it.

## 3. Choosing the held-out data

By default `fit` trains on the first 70% of the data in time and holds out the rest:

- with two days or more, the first 70% of the days (at least one on each side);
- with one day, 70% of the time between the first and the last order, so a 15-minute capture is cut after about 10
  minutes.

`--split 60%` changes the share. `--test-after HH:MM` holds out everything after a venue-local time on every day
(here the session runs 13:30 to 20:00 venue time):

<!-- check-docs: run -->
```bash
queuetruth fit --backtester nautilus --data synth --test-after 18:00 --out qt-fit-after
```

```text
split: held out after 18:00:00 venue time: train before 18:00:00 (701 shadows), held out from 18:00:00 (299 shadows)
```

`--train` and `--test` take the expert windows of `queuetruth advanced calibrate`: dates (`2026-06-15`) or `blocks:I-J/N`.
Blocks are fifths (`/5`) of the whole trading session, not of your data, so on a short capture `blocks:4/5` can
hold no order at all; `fit` then stops with `QT-DATA-001` and says so. `--split` and `--test-after` are cut where
the data are.

## 4. Fit to your own live fills

With your live order log (in the canonical form `queuetruth advanced live import` writes) and the market data of the same
period, `--live` fits the setting to your own fills instead of the exact queue (`queuetruth advanced calibrate-live`). The
synthetic dataset ships such a log of 200 orders:

<!-- check-docs: run exit=1 -->
```bash
queuetruth fit --backtester nautilus --data synth --live synth/own_orders/live --out qt-fit-live
```

```text
Do not use p = 0.54 (95% CI 0.42 to 0.66) for NautilusTrader prob_fill_on_limit yet: on held-out data (2026-06-16,
100 live orders) it does not beat the default p = 1 (mixed at 10s, 60s: better on one score, worse on the other).
Keep the default; no snippet written.
```

Each order enters at its exchange acknowledgement, which already includes your latency. On an L2-only tape your own
resting orders are removed from the depth first ([Calibrate on an L2-only venue](calibrate-live-l2-venue.md) has the
details).

## 5. From Python

<!-- check-docs: run -->
```python
import queuetruth as qt

res = qt.fit("nautilus", data="synth", out="qt-fit-py")
print(res.summary)
print(res.passed, res.doc["param"]["value"], res.doc["param"]["ci"])
if res.passed:
    print(res.snippet)
```

`res.summary` is the answer line, `res.doc` the whole `qt.fit` document (also in `qt-fit-py/fit.json`), `res.snippet`
the paste-ready text (None when refused) and `res.calibration` the `qt.calibration_result` behind it.

## 6. One line in your framework

The result goes into your framework as it is, and so does the atlas's closest setting when you have no data:
`qt.atlas.calibration(library, tier=None, symbol=None)` returns the same kind of result, with `source == "atlas"`,
where it came from (atlas root, tier, horizon, days) and the atlas verdict, read through the same
download-after-consent cache as `queuetruth atlas`. `res.settings` holds what to set, in the library's own names.

| Framework | The one line | How it is fed |
|---|---|---|
| NautilusTrader | `engine.add_venue(..., **qtn.venue_options(qt.fit(...)))` or `qtn.venue_options(qt.atlas.calibration("nautilus", tier="T1"))` | a fit on your data, or the atlas when you have none ([guide](nautilustrader.md)) |
| hftbacktest | `asset = qthbt.configure(asset, calibration)` | the same ([guide](calibrate-hftbacktest.md#3-one-line-in-your-hftbacktest-code)) |
| Backtrader | `cerebro.broker.set_filler(qt.backtrader.QueueFiller(tier="T1"))` and `cerebro.addanalyzer(qt.backtrader.OrderLog)` | the atlas touch tables ([guide](realistic-fills-backtrader-lean.md)) |
| LEAN | `security.set_fill_model(qt.lean.QueueFillModel(tier="T1"))` | the same |
| Freqtrade, VectorBT, backtesting.py | `queuetruth check <their result file>` | the same; the result is a realistic fill count and P&L range ([guide](check-my-backtest.md)) |
| Any simulator | `uses: sarptandoven/queuetruth/actions/referee@v1` | the referee fixtures ([guide](referee-your-simulator.md)) |

Here `qtn` is `queuetruth_nautilus` and `qthbt` is `queuetruth_hftbacktest`, the two plugins. On the synthetic atlas
the tests use (invented numbers; without `source=` it is the public atlas, which asks before it downloads):

<!-- check-docs: run -->
```python
import os

import queuetruth as qt

atlas = os.path.join(os.environ["QT_FIXTURES"], "atlas_synth")
cal = qt.atlas.calibration("nautilus", tier="T1", source=atlas)
print(cal.summary)
print(cal.source, cal.settings)
print(qt.atlas.calibration("hftbacktest", symbol="ZZZA", source=atlas).settings)
print(qt.fit("nautilus", data="synth", out="qt-fit-py").settings)
```

## Files

`--out` (default `qt-fit`) holds `fit.json` (the answer), `calibration/` (the calibration run, with
`calibration.json`), `shadows.parquet` when the shadows were sampled, and `<backtester>.py` only when the fit passes.

## Limits

- A fit is specific to the data it ran on: the venue, the period, and for L2 models the depth and conflation of the
  feed. Fit again when they change.
- NautilusTrader's `prob_fill_on_limit` thins touch fills at random: it corrects how many orders fill, not which
  ones. Where your data has depth, `queue_position=True` at `p = 1` needs no fit (see the plugin README).
- `fit` does not yet fit the latency first from a log with send and acknowledgement times; `queuetruth calibrate
  latency --live LOG` fits it on its own.
