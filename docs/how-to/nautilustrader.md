# Check and calibrate my NautilusTrader backtest

**Goal:** choose a NautilusTrader fill setting that fills about as often as the real queue, fit it on your own
data when you can, put it in your backtest with one line, and check it against your live fills.

**You need:** for step 1, nothing. For step 2, `pip install queuetruth` and order-level (MBO) data for the
venue you backtest, as a queuetruth dataset (`queuetruth data add`; [a free IEX DEEP+ day](iex-public-demo.md) works).
For step 3, the `queuetruth-nautilus` plugin
([`integrations/nautilus`](https://github.com/sarptandoven/queuetruth/tree/main/integrations/nautilus)) next to
`nautilus_trader` 1.231 or 2.x. For step 4, your live order and fill records.

## 1. Pick a setting from the atlas

The [Fill-Realism Atlas](../concepts/fill-realism-atlas.md) measures NautilusTrader's fill models against the
exact queue on every symbol of free IEX DEEP+ data. Over 20 trading days (2026-10-09 to 2026-11-06; 265,402
symbol-days, 12,440 symbols), small resting orders within 1 minute:

| setting | fill rate against the exact queue (11.97%) |
|---|---|
| touch model, `prob_fill_on_limit=1` (the default) | +1.50 pp: 13% more orders (95% interval +1.45 to +1.55) |
| `queue_position=True`, `prob_fill_on_limit=1` | +0.06 pp |

**Use `queue_position=True` wherever your data supports it** (an L2 book with trade prints). It needs no fit. The
default touch model fills like an order that is always first in the queue, so it is the optimistic bound. IEX is
one venue; the atlas shows the numbers by liquidity tier, and step 2 checks them on yours.

The touch model with a fitted `prob_fill_on_limit` is the choice when the venue must run without queue tracking: a
feed with trade prints but no usable depth, or an existing touch-model backtest you want to de-bias without
changing how it fills.

## 2. Fit `prob_fill_on_limit` on your data

`queuetruth fit --backtester nautilus` fits NautilusTrader's `prob_fill_on_limit` against the exact queue on part
of your data (it runs `queuetruth advanced calibrate nt-touch`) and compares it with the default `p = 1` on the part it did
not see. It writes the setting only when the fit wins there. This block runs on the synthetic dataset that ships
with queuetruth (CME rules, two instruments, two days); its numbers say nothing about a real venue.

<!-- check-docs: run -->
```sh
queuetruth advanced fixtures synth --out synth > /dev/null
queuetruth fit --backtester nautilus --data synth
```

The first line is the answer: the fitted `p` with its 95% interval, whether it beats `p = 1` on the held-out data,
and by how much. Then come the held-out table per horizon, the verdict and what to paste. On your data:

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth fit --backtester nautilus --data my-dataset --shadows my-orders.parquet
```

`--shadows` takes orders placed like yours (or your own: `queuetruth advanced shadows from-live`, see
[Import fills from my venue or broker](import-live-logs.md)); without it `fit` samples 1,000.
`--family nt-queue-l2-prob-touch` fits `p` for `queue_position=True` instead, and `--live` fits to your own live
fills. The split, the verdict and every option are in [Fit my backtester's fill model](fit-my-backtester.md); the
method is in [Calibration](../concepts/calibration.md).

**What `p` does not fix.** `prob_fill_on_limit` thins touch fills at random, one draw per print at the order's
price. It corrects the aggregate fill rate, not which orders fill, so per-order results and markouts stay closer
to the truth with `queue_position=True`. Fix `random_seed` when you compare runs.

## 3. Put it in your backtest

The `queuetruth-nautilus` plugin turns the calibration into the venue settings of the installed NautilusTrader
release line. Install it from a queuetruth checkout (it is not on PyPI yet):

<!-- check-docs: skip (installs packages) -->
```sh
pip install ./integrations/nautilus            # next to nautilus_trader 1.231.x
pip install --pre ./integrations/nautilus      # resolves the latest 2.0 release candidate
```

Then pass the calibration to `add_venue`. The one line takes the result of `qt.fit` (a fit on your data) or of
`qt.atlas.calibration` (the atlas's closest setting, when you have no data) as it is:

<!-- check-docs: skip (needs nautilus_trader, the plugin and user data; downloads the public atlas after asking) -->
```python
import queuetruth as qt
import queuetruth_nautilus as qtn

engine.add_venue(..., **qtn.venue_options(qt.fit("nautilus", data="my-dataset")))
engine.add_venue(..., **qtn.venue_options(qt.atlas.calibration("nautilus", tier="T1", yes=True)))
```

`qt.atlas.calibration` reads the atlas's `configs.json` through the same download-after-consent cache as
`queuetruth atlas` (`yes=True` agrees to the download; a cached copy works offline). `symbol="AAPL"` picks the
symbol's liquidity tier. The setting carries where it came from (atlas root, tier, horizon, days) and the atlas
verdict, and using it warns that it is the atlas (one venue, small orders), not your data. This block runs it on the
synthetic atlas the tests use (invented numbers) inside a NautilusTrader backtest engine, where `nautilus_trader`
and the plugin are installed:

<!-- check-docs: run needs=nautilus_trader,queuetruth_nautilus -->
```python
import os
import warnings

import queuetruth as qt
import queuetruth_nautilus as qtn

try:  # nautilus_trader 2.x
    from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.execution import FixedFeeModel
    from nautilus_trader.model import AccountType, Currency, Money, OmsType, Venue

    USD = Currency.from_str("USD")
    fees = {"fee_model": FixedFeeModel(Money(0, USD))}
except ImportError:  # 1.231.x
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Currency, Money

    USD = Currency.from_str("USD")
    fees = {}

atlas = os.path.join(os.environ["QT_FIXTURES"], "atlas_synth")  # without source= it is the public atlas
cal = qt.atlas.calibration("nautilus", tier="T1", source=atlas)
print(cal.summary)
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    opts = qtn.venue_options(cal, random_seed=42)
print([str(w.message) for w in caught][0])
engine = BacktestEngine(config=BacktestEngineConfig())
engine.add_venue(
    venue=Venue("XNAS"),
    oms_type=OmsType.NETTING,
    account_type=AccountType.MARGIN,
    base_currency=USD,
    starting_balances=[Money(1_000_000, USD)],
    **fees,
    **opts,
)
print(sorted(opts))
```

With a fit on your data, step by step:

<!-- check-docs: skip (needs nautilus_trader and the plugin) -->
```python
import queuetruth_nautilus as qtn
from nautilus_trader.execution import FixedFeeModel  # 1.x: leave out
from nautilus_trader.model import AccountType, Currency, Money, OmsType

USD = Currency.from_str("USD")
cal = qtn.load_calibration("qt-fit")  # the fit.json of `queuetruth fit`, or the FitResult of qt.fit
engine.add_venue(
    venue=venue,
    oms_type=OmsType.NETTING,
    account_type=AccountType.MARGIN,
    base_currency=USD,
    starting_balances=[Money(1_000_000, USD)],
    fee_model=FixedFeeModel(Money(0, USD)),  # 1.x: leave out; use your own fees
    **qtn.venue_options(cal, random_seed=42),
)
```

`venue_options` returns the fill model with the fitted (or the atlas's) `prob_fill_on_limit`, `book_type=L2_MBP`,
`trade_execution=True`, `queue_position` (True only for a queue-gated fit) and, when the calibration has one, the
latency model. A refused calibration raises `CalibrationRefused` (`QT-CALIB-001`) and gives no settings. On 1.x,
import `AccountType` and `OmsType` from `nautilus_trader.model.enums` and `Currency` and `Money` from
`nautilus_trader.model.objects`. `queuetruth-nautilus snippet qt-fit/calibration/calibration.json` prints the same code for the
installed release line, and `queuetruth-nautilus inspect` shows what it sets and the trust verdict. The plugin's
[README](https://github.com/sarptandoven/queuetruth/blob/main/integrations/nautilus/README.md) has the held-out
results inside NautilusTrader, the high-level `venue_config` and the mapping per release line.

Without the plugin, paste the `FillModel(prob_fill_on_limit=...)` line `fit` wrote, with the fit in its header:

<!-- check-docs: run -->
```sh
grep -E "^# fit|FillModel\(" qt-fit/nautilus.py
```

## 4. Score it against your live fills

The last check is your own trading: replay your live orders, at their live times, through the NautilusTrader
backtest with the new setting, and compare each order's outcome with what really happened. In short:

1. `queuetruth advanced live import --venue <venue>` builds the live log from your records.
2. `queuetruth advanced export replay-schedule --target nautilus` turns it into a fixed order schedule, and the generated
   strategy replays it in your NautilusTrader backtest.
3. `queuetruth advanced live import --venue sim-nautilus` reads the backtest's reports back.
4. `queuetruth advanced score --sim-log` scores the backtest against the live fills, order by order, with markouts.

The full walkthrough, first on synthetic data and then on Binance records, is
[Score my Nautilus backtest against my live fills](score-nautilus-vs-binance.md). To check a finished NautilusTrader
backtest against the exact queue instead, without live fills, use `queuetruth check`
([Check my backtest](check-my-backtest.md)).

## Limits

- The atlas numbers are from one venue, IEX, with its own rules; fit on your venue when you can.
- A fitted `p` is specific to the feed it was fitted on: its trade-print and quote-update rates set how many draws
  an order gets. Refit when the data source changes, and see [Data and licensing](../data-and-licensing.md) before
  you share fitted parameters.
- queuetruth's NautilusTrader models are re-implemented from its documentation and checked against the released
  package (1.231.0 and 2.0 release candidates); see [What is validated](../validated.md).
