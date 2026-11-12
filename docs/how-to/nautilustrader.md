# Check and calibrate my NautilusTrader backtest

**Goal:** choose a NautilusTrader fill setting that fills about as often as the real queue, fit it on your own
data when you can, put it in your backtest with one line, and check it against your live fills.

**You need:** for step 1, nothing. For step 2, `pip install queuetruth` and order-level (L3 or MBO) data for the
venue you backtest, as a queuetruth dataset (`queuetruth ingest`; [a free IEX DEEP+ day](iex-public-demo.md) works).
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

`queuetruth calibrate nt-touch` fits NautilusTrader's `prob_fill_on_limit` against the exact queue on one window
and scores it against the default `p = 1` on another. This block runs on the synthetic dataset that ships with
queuetruth (CME rules, two instruments, two days); its numbers say nothing about a real venue.

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --out synth > /dev/null
queuetruth shadows sample --data synth --n 2000 --out shadows.parquet \
    --placements 0:0.7,-1:0.3 --qty lots:1:0.5,lots:5:0.5
queuetruth calibrate nt-touch --data synth --shadows shadows.parquet \
    --train 2026-06-15 --test 2026-06-16 --out cal-nt
```

The answer comes first: the fitted `p` with its bootstrap interval, then per horizon the held-out log loss and
fill-rate bias of the fitted `p` and of `p = 1`, and the verdict. Only a fit that beats the default out of sample
is `exportable`:

<!-- check-docs: run -->
```python
import json

c = json.load(open("cal-nt/calibration.json"))
print(c["family"], c["param_name"], "=", round(c["estimate"], 4), "bootstrap 95%:", c["ci_bootstrap"])
print("verdict:", c["trust"]["verdict"], c["trust"]["reasons"])
```

On your data, place the shadows where your strategy places orders (or use your own orders:
`queuetruth shadows from-live`, see [Import fills from my venue or broker](import-live-logs.md)), and fit on some
days and test on others:

<!-- check-docs: skip (needs user data) -->
```sh
queuetruth calibrate nt-touch --data my-dataset --shadows my-orders.parquet \
    --train 2026-10-12,2026-10-13 --test 2026-10-14 --out cal-nt
```

`--queue-gated` fits `p` for `queue_position=True` instead. With one day, split it into blocks:
`--train blocks:0-3/5 --test blocks:4-4/5`. The method and its limits are in
[Calibration](../concepts/calibration.md).

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

Then pass the calibration to `add_venue`:

<!-- check-docs: skip (needs nautilus_trader and the plugin) -->
```python
import queuetruth_nautilus as qtn
from nautilus_trader.execution import FixedFeeModel  # 1.x: leave out
from nautilus_trader.model import AccountType, Currency, Money, OmsType

USD = Currency.from_str("USD")
cal = qtn.load_calibration("cal-nt/calibration.json")
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

`venue_options` returns the fill model with the fitted `prob_fill_on_limit`, `book_type=L2_MBP`,
`trade_execution=True`, `queue_position` (True only for a queue-gated fit) and, when the calibration has one, the
latency model. A refused calibration raises `CalibrationRefused` (`QT-CALIB-001`) and gives no settings. On 1.x,
import `AccountType` and `OmsType` from `nautilus_trader.model.enums` and `Currency` and `Money` from
`nautilus_trader.model.objects`. `queuetruth-nautilus snippet cal-nt/calibration.json` prints the same code for the
installed release line, and `queuetruth-nautilus inspect` shows what it sets and the trust verdict. The plugin's
[README](https://github.com/sarptandoven/queuetruth/blob/main/integrations/nautilus/README.md) has the held-out
results inside NautilusTrader, the high-level `venue_config` and the mapping per release line.

Without the plugin, `queuetruth export nautilus --calibration cal-nt/calibration.json --out nt_snippet.py` writes a
`FillModel(prob_fill_on_limit=...)` line to paste, with the fit in its header:

<!-- check-docs: run -->
```sh
queuetruth export nautilus --calibration cal-nt/calibration.json --out nt_snippet.py 2> /dev/null
grep -E "^# fit|FillModel\(" nt_snippet.py
```

## 4. Score it against your live fills

The last check is your own trading: replay your live orders, at their live times, through the NautilusTrader
backtest with the new setting, and compare each order's outcome with what really happened. In short:

1. `queuetruth live import --venue <venue>` builds the live log from your records.
2. `queuetruth export replay-schedule --target nautilus` turns it into a fixed order schedule, and the generated
   strategy replays it in your NautilusTrader backtest.
3. `queuetruth live import --venue sim-nautilus` reads the backtest's reports back.
4. `queuetruth score --sim-log` scores the backtest against the live fills, order by order, with markouts.

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
