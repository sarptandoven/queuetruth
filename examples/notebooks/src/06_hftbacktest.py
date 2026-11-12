# %% [markdown]
# # hftbacktest: the one line that sets a realistic queue model
#
# hftbacktest's `BacktestAsset` needs a queue model and an exchange model, and the usual choice is a guess. This
# notebook gets the setting two ways, the atlas when you have no order-level data and a fit when you do, applies it
# with one line, and then checks an hftbacktest run order by order:
#
# ```python
# asset = qthbt.configure(asset, qt.fit("hftbacktest", data="my-dataset"))
# asset = qthbt.configure(asset, qt.atlas.calibration("hftbacktest", tier="T1"))
# ```
#
# Data: queuetruth's **synthetic** dataset and the repository's **synthetic** atlas (invented numbers). The numbers
# only show the workflow; they say nothing about a real venue. Every result reads the same way: `.summary` (the
# answer), `.table()`, `.to_html()`, `.save(path)`, and a compact view in a notebook (`display(result)`).
# %%
import importlib.util
import json
from pathlib import Path

import pandas as pd
from IPython.display import display

import queuetruth as qt

pd.set_option("display.width", 140)
WORK = Path("qt-notebooks") / "06_hftbacktest"
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
# ## 1. No data yet: the atlas's closest setting
#
# `qt.atlas.calibration` reads the atlas's closest hftbacktest setting for a liquidity tier, as the same kind of
# result `qt.fit` returns. Without `source=` it reads the published atlas, after asking.

# %%
cal = qt.atlas.calibration("hftbacktest", tier="ALL", source=ATLAS, yes=True)
display(cal)

# %%
display(cal.settings)

# %% [markdown]
# ## 2. Your data: fit the `power_prob_queue_model3` exponent
#
# `qt.fit` fits `n` on the first 70% of the data and compares it with the library default `n = 2` on the rest. A
# fit that does not beat the default there is refused, says why, and writes no snippet: here it is refused.

# %%
fit = qt.fit(backtester="hftbacktest", data=DATA, split="70%", out=WORK / "fit")
display(fit)

# %%
print("passed:", fit.passed)
display(fit.table())

# %% [markdown]
# ## 3. The one line
#
# `queuetruth_hftbacktest.configure` sets the result's queue model and exchange on your `BacktestAsset` and leaves
# your data, tick, lot, latency and fee settings alone. A refused fit raises `CalibrationRefused`, so the line here
# takes the atlas setting. `configure` uses the standard library only: in a separate hftbacktest venv, save the
# result here (`cal.save("atlas-fit.json")`) and pass the path there.

# %%
cal.save(WORK / "atlas-fit.json")
if have("hftbacktest", "queuetruth_hftbacktest"):
    import queuetruth_hftbacktest as qthbt
    from hftbacktest import BacktestAsset

    asset = BacktestAsset().linear_asset(1.0).tick_size(0.25).lot_size(1.0)  # your settings
    asset = qthbt.configure(asset, cal)
    print("configured:", qthbt.settings(str(WORK / "atlas-fit.json")))
else:
    print("the settings the line applies:", cal.settings)

# %% [markdown]
# ## 4. How wrong was the run?
#
# `qt.check` replays an hftbacktest run's own resting limit orders in the exact queue of the data. Here the run is
# twelve orders written as order snapshots (one JSON line per order state, as `Recorder` users usually save them);
# use your own.

# %%
log = WORK / "hbt_snapshots.jsonl"
with log.open("w") as f:
    for k, o in enumerate(ORDERS.itertuples(), start=1):
        tick = round(o.price / 0.25)
        base = {"order_id": k, "instrument": o.symbol, "side": 1 if o.side == "buy" else -1, "price_tick": tick,
                "tick_size": 0.25, "qty": o.qty, "order_type": 0, "time_in_force": 0, "maker": 1}  # fmt: skip
        new = {"leaves_qty": o.qty, "exec_price_tick": 0, "status": 1}
        end = {"leaves_qty": 0, "exec_price_tick": tick, "status": 3} if o.filled else {**new, "status": 4}
        f.write(json.dumps({**base, **new, "local_timestamp": o.placed.value, "exch_timestamp": o.placed.value + 1000}))
        f.write(
            "\n" + json.dumps({**base, **end, "local_timestamp": o.done.value + 1000, "exch_timestamp": o.done.value})
        )
        f.write("\n")
res = qt.check(log, data=DATA, horizon="1s", window="10s", out=WORK / "check")
display(res)

# %%
display(
    res.table()[["symbol", "side", "price", "entry_local", "backtest_filled", "real_filled", "queue_ahead", "outcome"]]
)
