# queuetruth-nautilus

A calibrated `FillModel` for [NautilusTrader](https://github.com/nautechsystems/nautilus_trader), built from a
[queuetruth](https://github.com/sarptandoven/queuetruth) calibration: the `prob_fill_on_limit` fitted against an
exact order-book replay, the venue settings it was fitted under, and the fit's provenance and trust verdict.

NautilusTrader's default `prob_fill_on_limit = 1` fills every passive order the moment a trade prints at its price.
The evidence below is two public Nasdaq days (2019-12-30 to fit, 2020-01-30 held out), four names (AAPL, MSFT, SPY,
TSLA) and 3,000 sampled passive orders on the held-out day, run inside NautilusTrader itself. There the default
overfills by +2.80 pp at 1 s, and the value queuetruth fitted on the first day, `p = 0.2866`, cuts that to +0.40 pp
(2.0.0rc7) and +0.33 pp (1.231.0), mean of 8 seeds. At 60 s the improvement is not significant on 2.0.0rc7, and
one calibrated run is less accurate order by order than `p = 1` at 10 s and 60 s
([results](#held-out-result-inside-nautilustrader)). Read the next table before choosing this package.

## Which configuration should I use

Held-out day 2020-01-30, 3,000 orders, inside NautilusTrader. Bias is the fill rate minus the exact replay's
(order-level bootstrap 95% CI); Brier is the order-level score of the fill indicator at 1 s (lower is better).

| configuration | needs a fit | bias at 1 s, pp | bias at 60 s, pp | Brier at 1 s |
|---|---|---|---|---|
| touch model, `p = 1` (the default) | no | +2.80 [+2.27, +3.37] | +0.93 [+0.60, +1.30] | 0.0280 |
| touch model, calibrated `p = 0.2866`, one run (2.0.0rc7) | yes | +0.40 (seeds: +0.17 to +0.63) | -0.43 (seeds: -0.57 to -0.27) | 0.0202 |
| touch model, calibrated `p = 0.2866`, one run (1.231.0) | yes | +0.33 (seeds: +0.03 to +0.67) | -0.31 (seeds: -0.47 to -0.17) | 0.0214 |
| `queue_position=True`, `p = 1` (1.231.0) | no | -0.10 [-0.23, +0.00] | -0.23 [-0.40, -0.07] | 0.0010 |
| `queue_position=True`, `p = 1` (2.0.0rc7, queuetruth `docs/RESULTS.md`) | no | -0.03 | -0.07 | not recorded |

**Use `queue_position=True` at `p = 1` wherever your data supports it**: an L2 (MBP) book with trade prints, the
same inputs this package needs. It needs no fit, its aggregate bias is near zero, and its order-level Brier score is
about 20 times lower than one calibrated touch run's (0.0010 against 0.0202 and 0.0214 at 1 s). The 1.231.0 row is this package's
own run (`repro/results/heldout-nautilus-1.231.0-queue-forced.json`, the `nt_default` arm). The 2.0.0rc7 row was
measured with the harness whose end-of-window cancel does nothing on 2.x (see the end of this page); treat it as
pending a re-run. In 1.231.0, feed the book as `OrderBookDelta`s: an `OrderBookDepth10` snapshot resets the queue
position there, and the queue model then behaves like the touch model.

The calibrated touch `p` is still the right choice when the venue must run without queue tracking: a feed that
carries trade prints but no usable depth, an existing touch-model backtest you want to de-bias without changing its
mechanics, or a comparison against results produced with `queue_position=False`. A calibrated queue-gated `p` is
not an improvement on these days: the fit is refused, and forcing it makes the queue model worse (below).

## What `p` does not fix

`prob_fill_on_limit` thins touch fills at random, one independent draw per print at the order's price. It does not
look at queue depth, order size, time in queue or which side of the book is being run over. It therefore corrects
the aggregate fill rate, not which orders fill: on the held-out day one calibrated run scores a worse order-level
Brier than `p = 1` at 10 s and 60 s (table below). PnL and markouts of a passive strategy depend on which orders
fill. Fill-conditional markouts (adverse selection) are not validated here. For per-order or markout analysis
average several seeds, and prefer `queue_position=True` where the data allows. Always fix `random_seed` when
comparing runs: with `p < 1` the fills change with the seed (over 8 seeds the 1 s bias ranges over up to 0.64 pp),
and the package warns when no seed is given.

## Install

Not on PyPI yet; install from a queuetruth checkout:

```bash
pip install ./integrations/nautilus            # with nautilus_trader 1.231.x already installed
pip install --pre ./integrations/nautilus      # resolves the latest 2.0 release candidate
```

Requires Python 3.11+ and `nautilus_trader >= 1.231.0, < 3`. Supported and cross-checked: 1.231.x and 2.0.x
(checked on 1.231.0 and 2.0.0rc7). Other versions work with a `CalibrationWarning`; older than 1.231.0 is refused
(`QT-USAGE-001`).

On macOS older than 26, the 1.231.0 arm64 wheel on PyPI is tagged `macosx_26_0` although its extensions target
11.0, so pip skips it. Install the wheel under a `macosx_11_0` file name first (the recipe is in queuetruth's
`scripts/nt_crosscheck/requirements.txt`), then install this package. The 2.0 release candidates are
tagged `macosx_11_0` and install directly.

## Use

Fit with queuetruth (`queuetruth calibrate nt-touch ...`), then:

```python
import queuetruth_nautilus as qtn
from nautilus_trader.execution import FixedFeeModel  # 1.x: leave out
from nautilus_trader.model import AccountType, Currency, Money, OmsType
# 1.x: AccountType, OmsType from nautilus_trader.model.enums; Currency, Money from nautilus_trader.model.objects

USD = Currency.from_str("USD")
cal = qtn.load_calibration("cal/calibration.json")  # or the text `queuetruth export nautilus --out` wrote
engine.add_venue(
    venue=venue,
    oms_type=OmsType.NETTING,
    account_type=AccountType.MARGIN,
    base_currency=USD,
    starting_balances=[Money(1_000_000, USD)],
    fee_model=FixedFeeModel(Money(0, USD)),  # 1.x: leave out
    **qtn.venue_options(cal, random_seed=42),
)
```

The example is nautilus_trader 2.x, where from 2.0.0rc7 every backtest venue needs an explicit `fee_model` (a
zero-fee model too; use your own fees). On 1.x leave out the two lines marked `1.x: leave out` (1.x's `FixedFeeModel` refuses a zero
commission) and import from the modules the comment names. `tests/test_venue.py::test_readme_example_runs` runs this block on both lines.

`venue_options` returns `fill_model` (the release line's class with the fitted `prob_fill_on_limit`),
`book_type=L2_MBP`, `trade_execution=True`, `queue_position` (True only for a queue-gated fit) and, when the
calibration has a latency fit, `latency_model`. For the high-level API, `qtn.venue_config(cal, "XNAS", "NETTING",
"MARGIN", ["1000000 USD"])` returns a `BacktestVenueConfig` (in 1.x the models are importable configs, in 2.x the
objects, and `fee_model=...` is required on every 2.x release: `QT-CONFIG-001` without it). `qtn.fill_model(cal)` and
`qtn.latency_model(cal)` build one object each.

From the command line:

```bash
queuetruth-nautilus inspect cal/calibration.json      # what it sets, the trust verdict, held-out scores
queuetruth-nautilus snippet cal/calibration.json      # Python for the installed release line (--nt-major 1|2)
queuetruth-nautilus env                               # installed nautilus_trader and the class names used
```

Exit codes follow `queuetruth`: 0 success, 1 refused calibration (`QT-CALIB-001`), 2 any other error (a corrupt
file is `QT-INPUT-002`, an unexpected exception `QT-ENGINE-001`). Errors print `error: <code>: <message>` and a
hint on stderr; with `--format json` (alias `--json`) exactly one document goes to stdout: the `qt.error`, or for
`inspect` the `qt.nautilus_inspect` document (JSON Schema in `src/queuetruth_nautilus/schemas/`), whose `refused`
field carries the error of a refused calibration. Warnings go to stderr. `inspect` of a touch-model fit also points
at `queue_position=True` (see the first table).

## What is mapped

| calibration (`qt.calibration_result`) | NautilusTrader |
|---|---|
| `nt_prob_touch` estimate | `prob_fill_on_limit`, `queue_position=False` |
| `nt_queue_l2_prob_touch` estimate | `prob_fill_on_limit`, `queue_position=True` |
| other families (power, Databento, latency) | no fill model (`QT-CONFIG-001` if nothing else is fitted) |
| latency p50: entry, amend, cancel | `insert_latency_nanos`, `update_latency_nanos`, `cancel_latency_nanos`, `base_latency_nanos=0` |
| fitted on an L2 view with trade prints | `book_type=L2_MBP`, `trade_execution=True` |
| not fitted | `prob_slippage` (default 0.0) and `random_seed` are yours |

The class names differ by release line, and the package picks them for the installed one:

| | 1.x | 2.x |
|---|---|---|
| fill model | `nautilus_trader.backtest.models.FillModel` | `nautilus_trader.execution.DefaultFillModel` |
| latency model | `nautilus_trader.backtest.models.LatencyModel` | `nautilus_trader.execution.StaticLatencyModel` |
| cancel latency keyword | `cancel_latency_nanos` | `cancel_latency_nanos` |
| default `base_latency_nanos` | 1 ms | 0 |

`queuetruth export nautilus` (queuetruth 0.1.1) writes `LatencyModel(..., delete_latency_nanos=...)` and imports
from `nautilus_trader.backtest.models`; the keyword fails on both release lines and the import on 2.x. This package
reads that text (both keywords, and the fit's mode from the baseline it names) and builds the right objects;
`queuetruth-nautilus snippet` prints code that runs. Only fitted values become code: a latency component without
samples is left at the class default (0) and named in a comment.

## Trust

A calibration carries a verdict: `exportable` only when it has at least 30 events, no refusing diagnostic flag, and
beats NautilusTrader's default (`p = 1`) out of sample on Brier score and log loss. `fill_model`, `venue_options`,
`venue_config` and `snippet` raise `CalibrationRefused` (`QT-CALIB-001`) for a refused fit; `force=True` (`--force`)
builds it with a `CalibrationWarning`, for inspection. A result written before the verdict field existed is read
from its `trust: verdict:` warning line; one with no verdict at all is assessed by queuetruth when it is installed
and refused (`no_trust_verdict`) otherwise. Of the two real day-one fits in `tests/fixtures`, the touch fit is
exportable and the queue-gated fit (`p = 0.871`) is refused: out of sample it is worse than `p = 1` at 0.1 s, 1 s
and 60 s.

What the verdict does not cover. It scores the fit's expected fill probability per order, `1 - (1 - p)^touches`,
which is what an average over many seeds gets; one run is a single draw and scores worse (see the held-out tables).
And it compares the fit with the same model at `p = 1` only: a touch fit can pass while `queue_position=True` at
`p = 1` is far closer to exact, as on the days above.

Every constructor and `snippet` also emit a `CalibrationWarning`, once per call and pointing at your line, for: a fit
in queuetruth's `corrected` mode (its rules are not NautilusTrader's); an export text that does not name the fit's
mode; a queue-gated fit used on 2.x (fitted under 1.231.0's queue rules; 2.0 also caps queue ahead when a book update
shrinks the level, which changed at most 0.4% of orders at 0.1 to 60 s on the cross-checked days); `0 < p < 1`
without a `random_seed`; a latency component without samples; and an installed nautilus_trader outside the
cross-checked lines 1.231.x and 2.0.x.

`p` is a per-print probability. It depends on how often the feed prints and is not portable between feeds or
venues; fit it on data like the data you backtest on.

## Held-out result inside NautilusTrader

`repro/heldout.py` takes the calibration file, builds the fill model with this package, and runs queuetruth's
NautilusTrader harness (`scripts/nt_crosscheck/run.py`) on the held-out day: the day's L2 view (top 10 levels,
every event) as `OrderBookDelta`s and `TradeTick`s, 3,000 passive limit orders (1 or 100 shares, 2 ticks behind to
1 tick inside the touch, 2,916 submitted, the rest would cross or have no price), one isolated window per order, and
each order's first fill compared with queuetruth's exact FIFO replay of the full order book. The harness constructs
the fill model by name; the driver substitutes the object this package built and checks the requested probability
matches. The calibrated model is random per print, so it runs with 8 seeds.

Calibration `8fd2495564b974f8`: `nt_prob_touch`, `p = 0.28662597`, fitted on 2019-12-30 blocks 0-3 of 5 (3,604
touch events), held out on 2020-01-30. Bias is NautilusTrader's fill rate minus the exact fill rate. All CIs are
order-level bootstrap 95% intervals (2,000 replicates); the paired columns resample orders jointly for the default
and the calibrated model and are positive when the calibrated model is closer to exact:

- **|bias| reduction**: |default bias| minus |calibrated bias| (8-seed mean). The one-run version (|bias| averaged
  over seeds, in the JSON) has the same point values to 0.01 pp here, because every seed's bias has the same sign.
- **Brier gain**: default Brier minus calibrated Brier, per order, for one run (the score a single backtest gets).

**nautilus_trader 2.0.0rc7** (`repro/results/heldout-nautilus-2.0.0rc7.json`):

| horizon | exact | default bias, pp | calibrated bias, pp (8-seed mean) | per-seed bias, pp | \|bias\| reduction, pp | Brier default / 8-seed mean / one run | one-run Brier gain |
|---|---|---|---|---|---|---|---|
| 0.1 s | 3.07% | +0.80 [+0.50, +1.13] | +0.25 [+0.09, +0.43] | +0.13 to +0.33 | +0.55 [+0.34, +0.76] | 0.0080 / 0.0023 / 0.0041 | +0.0039 [+0.0019, +0.0060] |
| 1 s | 18.50% | +2.80 [+2.27, +3.37] | +0.40 [+0.00, +0.79] | +0.17 to +0.63 | +2.40 [+1.97, +2.85] | 0.0280 / 0.0119 / 0.0202 | +0.0078 [+0.0035, +0.0122] |
| 10 s | 58.20% | +2.03 [+1.57, +2.53] | -0.37 [-0.81, +0.04] | -0.50 to -0.13 | +1.66 [+0.85, +2.44] | 0.0203 / 0.0149 / 0.0242 | -0.0039 [-0.0081, +0.0004] |
| 60 s | 80.73% | +0.93 [+0.60, +1.30] | -0.43 [-0.74, -0.11] | -0.57 to -0.27 | +0.51 [-0.01, +1.08] | 0.0093 / 0.0077 / 0.0127 | -0.0033 [-0.0068, -0.0002] |

**nautilus_trader 1.231.0** (`repro/results/heldout-nautilus-1.231.0.json`):

| horizon | exact | default bias, pp | calibrated bias, pp (8-seed mean) | per-seed bias, pp | \|bias\| reduction, pp | Brier default / 8-seed mean / one run | one-run Brier gain |
|---|---|---|---|---|---|---|---|
| 0.1 s | 3.07% | +0.80 [+0.50, +1.13] | +0.25 [+0.07, +0.44] | +0.13 to +0.33 | +0.55 [+0.34, +0.78] | 0.0080 / 0.0029 / 0.0047 | +0.0033 [+0.0013, +0.0055] |
| 1 s | 18.50% | +2.80 [+2.27, +3.37] | +0.33 [-0.07, +0.73] | +0.03 to +0.67 | +2.47 [+2.02, +2.93] | 0.0280 / 0.0130 / 0.0214 | +0.0066 [+0.0023, +0.0110] |
| 10 s | 58.20% | +2.03 [+1.57, +2.53] | -0.39 [-0.83, +0.02] | -0.70 to -0.10 | +1.65 [+0.85, +2.41] | 0.0203 / 0.0145 / 0.0232 | -0.0029 [-0.0072, +0.0016] |
| 60 s | 80.73% | +0.93 [+0.60, +1.30] | -0.31 [-0.60, -0.03] | -0.47 to -0.17 | +0.62 [+0.10, +1.17] | 0.0093 / 0.0067 / 0.0115 | -0.0021 [-0.0052, +0.0009] |

What this shows, and what it does not:

- **The aggregate overfill is fixed at 0.1, 1 and 10 s.** The |bias| reduction CI excludes zero there on both release
  lines (at 1 s: +2.40 [+1.97, +2.85] pp on 2.0.0rc7, +2.47 [+2.02, +2.93] pp on 1.231.0).
- **At 60 s the improvement is not significant on 2.0.0rc7** (+0.51 [-0.01, +1.08] pp), and on 1.231.0 it is
  small (+0.62 [+0.10, +1.17] pp). At 60 s the calibrated model underfills significantly on both lines (-0.43 and
  -0.31 pp). queuetruth's own held-out evaluation of the fit predicted that (-0.38 pp at 60 s), so it is a property
  of one per-print `p` for every horizon, not of the engine.
- **Order by order, one run is better only at 0.1 and 1 s.** One calibrated run's Brier score is worse than the
  default's at 10 s and 60 s on both lines, significantly so at 60 s on 2.0.0rc7 (-0.0033 [-0.0068, -0.0002]). The
  8-seed average scores better than the default at every horizon (significantly at 0.1 to 10 s; 0.0119 against
  0.0280 at 1 s), but no backtest is an 8-seed average unless you make it one.
- **NautilusTrader does what queuetruth predicted, order by order.** queuetruth's held-out prediction for this fit
  gives each order the probability `1 - (1 - p)^touches`. On the 2,842 to 2,970 orders (depending on the horizon)
  where that probability is 0 or 1, every calibrated NautilusTrader run agrees with it on both lines (0
  mismatches). On the remaining 30 to 158 orders, the 8-seed mean minus the prediction is within +-0.08 pp in
  aggregate, with every CI containing zero (at 1 s: -0.02 [-0.13, +0.11] pp on 2.0.0rc7, -0.08 [-0.20, +0.03] pp
  on 1.231.0). Its spread matches 8 binomial draws: the squared deviation over its binomial variance averages 0.65
  to 1.04. The default agrees with queuetruth's replica of `p = 1` on 100% of orders at every horizon.

Reproduce (Nasdaq sample data via queuetruth's `scripts/realdata`; the `nautilus` step took 409 s on 2.0.0rc7 and
797 s on 1.231.0 with 2 processes, on an 8-core laptop shared with other heavy jobs; `score` takes seconds):

```bash
# queuetruth venv
.venv/bin/python integrations/nautilus/repro/heldout.py prepare --calibration CAL.json \
    --dataset .realdata/ds/nasdaq-2020-01-30 --shadows .realdata/shadows/2d-sampled-6000.parquet --work W
# a NautilusTrader venv with this package installed (once per version)
$NT_PY integrations/nautilus/repro/heldout.py nautilus --calibration CAL.json --work W --seeds 8 --procs 2
$NT_PY integrations/nautilus/repro/heldout.py score --calibration CAL.json --work W --results integrations/nautilus/repro/results
```

`score` reads the per-order prediction from `held_out/zoo_predictions.parquet` next to the calibration file
(`queuetruth calibrate` writes it), or from `--zoo-held-out`. The published reports used the refit below, whose
fitted model is the same `p` to the last bit. Fill times are compared as exact int64 nanoseconds.

`heldout.py fit --spec-from CAL.json --dataset .realdata/ds/nasdaq-2d ...` refits the calibration with the current
queuetruth build first. Refitting the published calibration this way (19 s) on queuetruth's CSV ingest of the same
two days gives the identical estimate, bootstrap CI, event count, held-out scores and trust verdict; only the run id
and dataset id differ, because the published fit used the raw ITCH ingest (`repro/results/refit-check.json`).

**The refused fit, inside NautilusTrader.** The queue-gated day-one fit (`nt_queue_l2_prob_touch`, `p = 0.871`,
calibration `4291537475bc10f3`) is refused because it does not beat `p = 1` out of sample. Run anyway (`--force`,
4 seeds, `queue_position=True`, nautilus_trader 1.231.0; `repro/results/heldout-nautilus-1.231.0-queue-forced.json`),
it confirms the verdict inside the engine: the default queue model is already close to exact on the held-out day,
and the fitted value only removes fills. The paired tests say "worse" from 1 s on, as they should for a bad fit.

| horizon | exact | default `p = 1` bias, pp | forced `p = 0.871` bias, pp | \|bias\| reduction, pp | Brier default / one run | one-run Brier gain |
|---|---|---|---|---|---|---|
| 0.1 s | 3.07% | +0.00 [-0.10, +0.10] | -0.01 [-0.10, +0.09] | -0.01 [-0.03, +0.02] | 0.0007 / 0.0008 | -0.0001 [-0.0003, +0.0000] |
| 1 s | 18.50% | -0.10 [-0.23, +0.00] | -0.24 [-0.39, -0.12] | -0.14 [-0.22, -0.07] | 0.0010 / 0.0024 | -0.0014 [-0.0022, -0.0008] |
| 10 s | 58.20% | -0.17 [-0.40, +0.03] | -0.41 [-0.65, -0.18] | -0.24 [-0.35, -0.13] | 0.0037 / 0.0059 | -0.0022 [-0.0033, -0.0013] |
| 60 s | 80.73% | -0.23 [-0.40, -0.07] | -0.33 [-0.53, -0.17] | -0.10 [-0.17, -0.04] | 0.0023 / 0.0033 | -0.0010 [-0.0017, -0.0004] |

The queue run is on 1.231.0 only: on 2.x the harness does not cancel unfilled orders (next paragraph), and with
`queue_position=True` resting orders share print volume, so a clean 2.x queue run needs the harness fixed first.
There is no per-order prediction for this fit (its calibration's held-out predictions are not in the repository).

**A harness caveat on 2.x.** In 2.0.0rc7 the order object a strategy gets from `order_factory.limit(...)` stays
`INITIALIZED` after submission, and `Strategy.cancel_order` takes a `ClientOrderId`; the harness's end-of-window
cancel (`if order.is_open: cancel_order(order)`) is therefore a no-op on 2.x, and unfilled orders keep resting (370
of the default run's orders first filled after their cancel time; 0 on 1.231.0). Those fills fall outside every
scored horizon, and with `queue_position=False` one resting order does not affect another's fills (the default run
on 2.x still agrees with the replica on every order), so the touch results above are unaffected; the queue-model
cross-check on 2.x is the place where it could matter. The engine tests in this package cancel through the cache and
pass the `ClientOrderId` on 2.x.

## Tests

```bash
pip install --pre "./integrations/nautilus[test]" && pytest integrations/nautilus/tests
```

Calibration parsing, snippets, the CLI and the held-out scoring (on hand-made outcomes, including a too-small `p`
that the paired tests must reject) run without NautilusTrader. With it installed, the tests also build every object
for the installed release line; run the README example above, the rendered snippet and a `venue_config` (through a
`BacktestNode` over a small catalog) in real backtests; and run a synthetic `BacktestEngine` that checks
the per-touch contract a calibrated `p` relies on: `p = 1` fills every touched order, `p = 0` none (a trade-through
still fills), one touch fills about `p` of 400 orders and two touches about `1 - (1 - p)^2`, the same seed gives the
same fills, and a queue-gated calibration waits for the shares ahead. The suite passes on nautilus_trader 1.231.0,
2.0.0rc4, 2.0.0rc5 and 2.0.0rc7, and without nautilus_trader for the parts that do not need it.

## Upstream

[UPSTREAM.md](UPSTREAM.md) is a draft PR description and design note for contributing the validated-parameter
guidance and a per-touch regression test to NautilusTrader. It has not been opened.

## License

Apache-2.0. NautilusTrader is LGPL-3.0; this package imports its public Python API and copies none of its source.
