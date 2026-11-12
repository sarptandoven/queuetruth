# %% [markdown]
# # NautilusTrader: the one line that makes its fills realistic
#
# NautilusTrader's default `FillModel` fills a resting limit order as soon as the market touches its price, as if
# you were first in the queue. This notebook gets a better setting two ways, the atlas when you have no order-level
# data and a fit when you do, applies it with one line, and then checks a NautilusTrader backtest order by order:
#
# ```python
# engine.add_venue(..., **qtn.venue_options(qt.fit("nautilus", data="my-dataset")))
# engine.add_venue(..., **qtn.venue_options(qt.atlas.calibration("nautilus", tier="T1")))
# ```
#
# Data: queuetruth's **synthetic** dataset and the repository's **synthetic** atlas (invented numbers). The numbers
# only show the workflow; they say nothing about a real venue. Every result reads the same way: `.summary` (the
# answer), `.table()`, `.to_html()`, `.save(path)`, and a compact view in a notebook (`display(result)`).
# %%
import importlib.util
from pathlib import Path

import pandas as pd
from IPython.display import display

import queuetruth as qt

pd.set_option("display.width", 140)
WORK = Path("qt-notebooks") / "05_nautilustrader"
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
# `qt.atlas.calibration` reads the atlas's closest NautilusTrader setting for a liquidity tier, as the same kind of
# result `qt.fit` returns. Without `source=` it reads the published atlas, after asking.

# %%
cal = qt.atlas.calibration("nautilus", tier="T1", source=ATLAS, yes=True)
display(cal)

# %%
display(cal.settings)

# %% [markdown]
# ## 2. Your data: fit `prob_fill_on_limit`
#
# `qt.fit` fits the setting on the first 70% of the data and compares it with the library default on the rest. The
# snippet is written only when the fit beats the default there.

# %%
fit = qt.fit(backtester="nautilus", data=DATA, split="70%", out=WORK / "fit")
display(fit)

# %%
display(fit.table())

# %% [markdown]
# ## 3. The one line
#
# `queuetruth_nautilus.venue_options` turns the result into `add_venue` keywords: the fill model with the fitted
# `prob_fill_on_limit`, `book_type=L2_MBP`, and `queue_position` when the fit is queue-gated. Without NautilusTrader
# installed the cell says so and shows the settings the line would apply.

# %%
if have("nautilus_trader", "queuetruth_nautilus"):
    import queuetruth_nautilus as qtn

    try:  # nautilus_trader 2.x
        from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
        from nautilus_trader.model import AccountType, Currency, Money, OmsType, Venue
    except ImportError:  # 1.231.x
        from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
        from nautilus_trader.model.enums import AccountType, OmsType
        from nautilus_trader.model.identifiers import Venue
        from nautilus_trader.model.objects import Currency, Money

    USD = Currency.from_str("USD")
    engine = BacktestEngine(config=BacktestEngineConfig())
    engine.add_venue(
        venue=Venue("XCME"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(1_000_000, USD)],
        **qtn.venue_options(fit, random_seed=42),
    )
    print("venue added with", sorted(qtn.venue_options(fit, random_seed=42)))
else:
    print("the settings the line applies:", fit.settings)

# %% [markdown]
# ## 4. How wrong was the backtest?
#
# `qt.check` replays a NautilusTrader backtest's own resting limit orders in the exact queue of the data. Here the
# backtest is twelve orders written as `generate_orders_report()` writes them; use your own report.

# %%
report = WORK / "orders_report.csv"
rows = [
    {
        "client_order_id": o.order,
        "venue_order_id": o.order.replace("O-", "V-"),
        "instrument_id": f"{o.symbol}.XCME",
        "side": o.side.upper(),
        "type": "LIMIT",
        "quantity": o.qty,
        "price": f"{o.price:.2f}",
        "time_in_force": "GTC",
        "post_only": False,
        "reduce_only": False,
        "status": "FILLED" if o.filled else "CANCELED",
        "filled_qty": o.qty if o.filled else 0,
        "avg_px": f"{o.price:.2f}" if o.filled else "",
        "ts_init": o.placed.value,
        "ts_last": o.done.value,
    }
    for o in ORDERS.itertuples()
]
pd.DataFrame(rows).to_csv(report, index=False)
res = qt.check(report, data=DATA, horizon="1s", window="10s", out=WORK / "check")
display(res)

# %%
display(res.table()[["order", "symbol", "side", "price", "entry_local", "backtest_filled", "real_filled", "outcome"]])

# %% [markdown]
# The same answer as a page to share, and as the JSON document:

# %%
print(res.save(WORK / "check.html"))
print(res.save(WORK / "check.json"))
