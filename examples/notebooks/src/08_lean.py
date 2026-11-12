# %% [markdown]
# # LEAN: realistic limit fills in one line
#
# LEAN's default fill model fills a limit order as soon as a bar trades at or through its price. queuetruth's
# `QueueFillModel` keeps LEAN's own limit logic and accepts each such fill with the probability the atlas touch
# tables give for the order's tier, side and bar length; `qt.check` reads LEAN's order events and says how wrong the
# run was:
#
# ```python
# security.set_fill_model(qt.lean.QueueFillModel(tier="T1"))
# ```
#
# Data: queuetruth's **synthetic** dataset and the repository's **synthetic** atlas (invented numbers). The numbers
# only show the workflow; they say nothing about a real venue.
# %%
import importlib.util
import json
from pathlib import Path

import pandas as pd
from IPython.display import display

import queuetruth as qt
from queuetruth.backtesters.touchfill import TouchFill

pd.set_option("display.width", 140)
WORK = Path("qt-notebooks") / "08_lean"
WORK.mkdir(parents=True, exist_ok=True)

# queuetruth's synthetic order-level dataset: two futures (QTAU6, QTBU6), two days, invented prices.
DATA = WORK / "synth"
if not (DATA / "manifest.json").exists():
    qt.fixtures_synth(str(DATA))

# The repository's synthetic atlas (invented numbers, no market data): the checkout's copy, else the one on GitHub.
# Leave `source=` out to use the published atlas (it asks before downloading anything).
_local = Path(qt.__file__).resolve().parents[2] / "tests" / "fixtures" / "atlas_synth"
ATLAS = (
    str(_local)
    if (_local / "atlas" / "v1" / "manifest.json").exists()
    else "https://raw.githubusercontent.com/sarptandoven/queuetruth/main/tests/fixtures/atlas_synth/atlas/v1/"
)

# Twelve resting limit orders on the synthetic futures, as a bar backtest reports them: placed at `placed` (UTC),
# and filled 5 s later when `filled` (the touch rule), else cancelled 30 s later.
ORDERS = pd.DataFrame(
    [
        ("O-1", "QTBU6", "sell", 1, 5008.50, "2026-06-16 14:38:42", True),
        ("O-2", "QTAU6", "buy", 2, 4999.75, "2026-06-16 14:38:05", False),
        ("O-3", "QTBU6", "sell", 3, 5010.25, "2026-06-15 17:09:27", True),
        ("O-4", "QTAU6", "buy", 1, 4999.50, "2026-06-16 17:32:15", False),
        ("O-5", "QTAU6", "sell", 2, 5002.75, "2026-06-15 15:39:13", True),
        ("O-6", "QTAU6", "buy", 3, 5004.00, "2026-06-15 19:04:05", False),
        ("O-7", "QTBU6", "buy", 1, 5008.75, "2026-06-15 15:36:19", True),
        ("O-8", "QTBU6", "sell", 2, 5008.50, "2026-06-16 16:58:59", False),
        ("O-9", "QTAU6", "buy", 3, 4999.50, "2026-06-16 14:39:20", True),
        ("O-10", "QTAU6", "buy", 1, 4999.50, "2026-06-16 17:25:05", False),
        ("O-11", "QTBU6", "buy", 2, 5008.25, "2026-06-16 16:12:29", True),
        ("O-12", "QTAU6", "buy", 3, 5001.25, "2026-06-15 15:05:05", False),
    ],
    columns=["order", "symbol", "side", "qty", "price", "placed", "filled"],
)
ORDERS["placed"] = pd.to_datetime(ORDERS["placed"], utc=True)
ORDERS["done"] = ORDERS["placed"] + pd.to_timedelta(ORDERS["filled"].map({True: 5, False: 30}), unit="s")


def have(*modules, hint=None):
    """True when every module is importable; else prints which are missing and how to get them."""
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if missing:
        print(f"skipped: {', '.join(missing)} not installed here ({hint or 'pip install ' + ' '.join(missing)})")
    return not missing


print("queuetruth", qt.__version__, "| SYNTHETIC data and a SYNTHETIC atlas: the numbers show the workflow only")

# %% [markdown]
# ## 1. What the fill model draws from
#
# The touch tables give `P(real | touch)`: the probability that a resting limit order the bar touched really filled
# in the queue, by tier, side and bar length (here the synthetic atlas; without `atlas=` the published one, after
# asking).

# %%
touch = TouchFill(qt.atlas.touch_tables(ATLAS, yes=True), tier="T1")
display(
    pd.DataFrame(
        [{"bar": b, "side": s, "P(real | touch)": touch.probability(b, s)} for b in touch.bars for s in ("buy", "sell")]
    )
)

# %% [markdown]
# ## 2. The one line
#
# In a LEAN Python algorithm, after adding the security:
#
# ```python
# from AlgorithmImports import *
# import queuetruth as qt
#
# class MyAlgorithm(QCAlgorithm):
#     def initialize(self):
#         security = self.add_equity("SPY", Resolution.MINUTE)
#         security.set_fill_model(qt.lean.QueueFillModel(tier="T1"))
# ```
#
# LEAN runs algorithms in its own engine, so the model is made only where `AlgorithmImports` exists:

# %%
if have("AlgorithmImports", hint="LEAN runs algorithms in its own engine: use the lean CLI"):
    model = qt.lean.QueueFillModel(tier="T1", atlas=ATLAS, yes=True)
    print(type(model).__name__, model.summary())

# %% [markdown]
# ## 3. How wrong was the run?
#
# `qt.check` reads LEAN's order events (the `*-order-events.json` of a backtest). Here they are written for twelve
# orders on the synthetic futures; use your own. Without order-level data the answer is the atlas estimate:

# %%
events = []
for k, o in enumerate(ORDERS.itertuples(), start=1):
    sign = 1 if o.side == "buy" else -1
    base = {"algorithmId": "synthetic", "orderId": k, "symbol": o.symbol, "symbolValue": o.symbol,
            "symbolPermtick": o.symbol, "fillPriceCurrency": "USD", "direction": o.side, "orderType": "limit",
            "isAssignment": False, "quantity": sign * o.qty, "limitPrice": o.price}  # fmt: skip
    events.append({**base, "id": f"synthetic-{k}-1", "orderEventId": 1, "time": o.placed.timestamp(),
                   "status": "submitted", "fillPrice": 0.0, "fillQuantity": 0.0})  # fmt: skip
    if o.filled:
        done = {"status": "filled", "fillPrice": o.price, "fillQuantity": sign * o.qty, "orderFeeAmount": 0.0}
    else:
        done = {"status": "canceled", "fillPrice": 0.0, "fillQuantity": 0.0}
    events.append({**base, **done, "id": f"synthetic-{k}-2", "orderEventId": 2, "time": o.done.timestamp()})
path = WORK / "lean-order-events.json"
path.write_text(json.dumps(events))

estimate = qt.check(path, bar="5m", tier="T1", atlas=ATLAS, yes=True, out=WORK / "estimate")
display(estimate)

# %% [markdown]
# With order-level data of the same days, each order is replayed in the exact queue instead:

# %%
res = qt.check(path, data=DATA, horizon="1s", window="10s", out=WORK / "check")
display(res)

# %%
display(res.table()[["symbol", "side", "price", "entry_local", "backtest_filled", "real_filled", "outcome"]])
