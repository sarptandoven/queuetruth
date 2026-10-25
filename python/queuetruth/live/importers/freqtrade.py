"""Freqtrade importers: backtest result exports (simulator arm) and the trades database of a dry-run or live bot
(live arm) -> canonical live logs, a pairing of the two runs, `score` and a per-order explanation of the
differences; plus `candle_fill_audit`, which measures Freqtrade's candle-fill rule against exact FIFO truth.

Formats (read from Freqtrade's public source, `develop` at commit 1c0d709401b5, 2026-10-24):

- Backtest results (`freqtrade backtesting --export trades`): the zip `backtest-result-<date>.zip` whose member
  `backtest-result-<date>.json` holds `{"strategy": {<name>: {"trades": [...], "timeframe": "5m", ...}},
  "strategy_comparison": [...]}` (`optimize_reports/bt_storage.py`), the same JSON on its own (older exports), or
  `.last_result.json` (`{"latest_backtest": <file name>}`, resolved next to it). The member
  `<stem>_config.json`, when present, gives the exchange name and `order_types`. Trades are the
  `BT_DATA_COLUMNS` rows (`data/btanalysis/bt_fileutils.py`): `pair, amount, open_timestamp, close_timestamp,
  open_rate, close_rate, fee_open, fee_close, is_open, is_short, enter_tag, exit_reason, orders`; `orders` are
  minified `Order.to_json` rows `amount, safe_price, ft_order_side, order_filled_timestamp, ft_is_entry,
  ft_order_tag, cost` of `LocalTrade.select_filled_or_open_orders`. A backtest order fills at its price whenever
  the candle's range contains it (`Backtesting._get_order_filled`: `low <= rate <= high`, no slippage, no queue);
  its fill time is the open time of that candle. A limit entry priced through the placing candle is first clamped
  to its high (buy) or low (sell), so it fills on that candle. Orders that timed out are deleted
  (`check_order_cancel`), so the export holds filled orders and the orders still open when the backtest ended (no
  `order_filled_timestamp`: imported unfilled, placed at the trade's last fill, counted in a warning); the strategy
  statistics count the deleted ones (`timedout_entry_orders`, `timedout_exit_orders`, `canceled_entry_orders`,
  `replaced_entry_orders`). Exports without `orders` (before 2022) give one entry and one exit order per trade from
  `open_rate` / `close_rate` at the trade's open / close time (warned). The export has no order type: a backtest
  order paired with a live order takes the live order's type; others take the config's `order_types` (`entry`,
  `exit`, `stoploss` for stop-loss exits: `limit` -> `stop_limit`, `market` -> `stop`), else `limit` (warned).
- Trades database (`tradesv3.sqlite`, `tradesv3.dryrun.sqlite`; `persistence/trade_model.py`): tables `trades`
  (`id, exchange, pair, is_short, open_date, close_date, enter_tag, exit_reason, timeframe, trading_mode,
  fee_open_cost, fee_open_currency, fee_close_cost, fee_close_currency`) and `orders` (`ft_trade_id,
  ft_order_side, ft_pair, ft_amount, ft_price, ft_cancel_reason, ft_order_tag, order_id, status, order_type,
  side, price, average, amount, filled, stop_price, order_date, order_filled_date, order_update_date`). Datetimes
  are naive UTC text. One order row is one exchange order with its aggregate `filled` / `average`; it becomes one
  fill (`derived = true`, fill id `<order_id>:fill`). Dry-run order ids start with `dry_run_`
  (`Exchange.create_dry_run_order`): a dry-run log is marked `validation_status = simulated` (its fills are
  Freqtrade's dry-run matching: a limit order fills when the best opposite price of a polled order-book snapshot
  crosses it, `_dry_is_price_crossed`), its times are the bot's clock (`submit_sent` / `ack` local times), while a
  live log's `order_date` is the exchange's order time from ccxt (`ack` at `gateway_time`; option
  `order_date_clock = "local"` when your exchange does not report one). Fills use `order_filled_date`
  (`record_time` for live): ccxt's `lastTradeTimestamp`, or the bot's clock when the exchange does not report one
  (`Order.update_from_ccxt_object`). All these times are epoch milliseconds in ccxt (dry-run: `dt_ts()`), so the
  events carry precision `ms`. Cancels use `order_update_date` (`cancel_ack`, preceded by `cancel_sent` when the
  bot cancelled: an `ft_cancel_reason` other than "cancelled on exchange"; a stop-loss the bot cancels at exit has
  no reason and gets `cancel_ack` only). Freqtrade stores no time in force: limit orders get option `tif` (default
  `gtc`, Freqtrade's `order_time_in_force` default; warned). A dry-run order with `0 < filled < amount` is warned:
  dry-run limit orders fill all at once (`check_dry_limit_order_filled`) and a dry-run cancel resets `filled`.
- Bot log (`--logfile`): Freqtrade deletes a trade whose entry orders were all cancelled unfilled, so the database
  never holds those orders. Each `... Removing Trade(id=.., pair=.., amount=.., is_short=.., leverage=..,
  open_rate=.., open_since=..) from database.` line (`handle_cancel_enter`: `<Side> order fully cancelled.`
  first, after `<Side> order <ft_cancel_reason> for Trade(...).`; `handle_onexchange_order`: `Trade only had fully
  canceled entry orders.` first, an exchange cancel; `replace_order_failed`: after `Could not fully cancel order
  for Trade(...), therefore not replacing.`) restores one limit entry order placed at `open_since` (UTC, seconds)
  at `open_rate`, cancelled at the line's time (`log_tz`, default UTC), with `cancel_sent` only when the bot
  cancelled; its key is `<venue>:<account>:deleted-<trade id>-<open_since>` (SQLite reuses the ids of deleted
  rows). Removal lines in another shape are counted in a warning.

Numbers: Freqtrade stores IEEE doubles. Values are read as decimals rounded to 15 significant digits (the digits a
double carries exactly; `0.30000000000000004` -> `0.3`); live prices and sizes must then be exact at the
instrument's exponents (`QT-INPUT-004`), an aggregate fill price that is not becomes `px = null` (warned);
backtest prices and sizes (computed ROI / stop rates) are rounded half-even to the exponents and counted.

Pairing (`pair_runs`): the backtest and the bot share no order id, so their orders are linked by trade and role.
A live trade pairs with the unpaired backtest trade of the same pair and direction whose entry candle is closest
to the live trade's (floor of `open_date` to the timeframe), within `pair_tolerance_candles` (default 1). Within a
paired trade, each backtest order (entry orders, exit orders; filled stoploss orders count as exits) pairs with
the live order of its role that was placed latest in or before the backtest order's candle (placement candle for
the first entry, fill candle otherwise), else the earliest one placed within the tolerance after it. Untriggered
stop-loss orders pair only with untriggered stop-loss orders of another bot run (category `stop_not_triggered`
either way: a backtest simulates its stop inside the exit). Trades are bucketed by (pair, direction) and orders
by role, so pairing is O(n log n) in the number of trades. A paired
backtest order takes the live `order_key`; its fill time is clamped to the live order's entry time when the
candle opened before the live order existed (the candle's range is all the backtest knows; the scorer's window
starts at the live entry) and the candle time stays in the fill's `reason`. Unpaired backtest orders keep
`sim:freqtrade:<account>:bt<trade>-<pair>-<role><k>` keys and match no live order.

`reconcile_freqtrade(sim, live, out)` runs the whole flow (the simulator arm a backtest result or a dry-run bot,
the live arm a live or dry-run bot): both logs, `score` with the simulator as the external arm, and
`reconcile.json` / `reconcile_orders.csv` (`ReconcileReport`, one row per order of either arm with a category and
an explanation; `run_id` per SPEC 7.3 over the inputs' content and every option). The generic part (`OrderView`,
`explain_pairs`, `write_reconcile`, `reconcile_spec`) is shared with `queuetruth.live.importers.hummingbot`.
`import_sim("freqtrade", ..., pair_with=[live inputs])`: options `dry_run`, `venue`, `order_date_clock` describe
the simulator arm, `live_dry_run` and `live_venue` the `pair_with` run.
"""

from __future__ import annotations

import bisect
import csv
import datetime as dt
import io
import itertools
import json
import math
import re
import sqlite3
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.errors import QtError
from queuetruth.live._common import (
    Record,
    dec,
    dsub,
    epoch_ns,
    input_error,
    make_order_key,
    read_text,
    rfc3339_ns,
    units_error,
    utc_date_days,
)
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.imap import InstrumentEntry, InstrumentMap, load_instrument_map
from queuetruth.live.importers._base import ImportOptions
from queuetruth.schemas.io import sha256_file
from queuetruth.schemas.models import QtModel
from queuetruth.schemas.reconcile import ReconcileOrder, ReconcileReport, ReconcileScore

FT_SIM = "sim:freqtrade"
#: Freqtrade sources pinned for the fixtures and the format notes above.
FREQTRADE_COMMIT = "1c0d709401b56b2c85221e2960d1bd1238736245"
_NS = 1_000_000_000
_TF_UNIT = {"m": 60, "h": 3600, "d": 86_400, "w": 604_800}
_SIG = 15
_LOG_HEAD = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d{1,6})?) - [\w.]+ - \w+ - (?P<msg>.*)$")
_TRADE_REPR = (
    r"Trade\(id=(?P<id>\d+), pair=(?P<pair>[^,]+), amount=(?P<amount>[^,]+), is_short=(?P<short>True|False), "
    r"leverage=(?P<lev>[^,]+), open_rate=(?P<rate>[^,]+), open_since=(?P<since>[^)]+)\)"
)
#: the three messages after which Freqtrade deletes a trade (`freqtradebot.py`): `handle_cancel_enter`,
#: `handle_onexchange_order` (entries cancelled on the exchange) and `replace_order_failed`.
_LOG_REMOVE = re.compile(
    r"^(?:(?P<side>Buy|Sell) order fully cancelled\. |Trade only had fully canceled entry orders\. )?"
    r"Removing (?P<trade>" + _TRADE_REPR + r") from database\.$"
)
#: `handle_cancel_enter`: `<Side> order <ft_cancel_reason> for <Trade repr>.` just before the removal line.
_LOG_CANCEL = re.compile(r"^(?P<side>Buy|Sell) order (?P<reason>.+) for (?P<trade>Trade\(id=.*\))\.$")
#: `replace_order_failed`: the warning before its removal line.
_LOG_REPLACE_FAILED = re.compile(
    r"^Could not fully cancel order for (?P<trade>Trade\(id=.*\)), therefore not replacing\.$"
)
#: `constants.CANCEL_REASON["CANCELLED_ON_EXCHANGE"]`: the exchange, not the bot, cancelled the order.
_EXCHANGE_CANCEL = "cancelled on exchange"
#: what a `replace_order_failed` deletion means: the bot cancelled to replace and the cancel did not complete.
_CANCEL_REPLACE_FAILED = "cancel to replace not confirmed, replacement abandoned"
_STATUS = {"cancelled": "canceled", "canceled": "canceled", "closed": "closed", "open": "open"}
_STATUS.update({"expired": "expired", "rejected": "rejected"})
FT_OPTIONS = frozenset(
    {
        "strategy",
        "timeframe",
        "tif",
        "dry_run",
        "order_date_clock",
        "log_tz",
        "venue",
        "map_venue",
        "pair_with",
        "pair_tolerance_candles",
        "price_exp",
        "size_exp",
        "live_dry_run",
        "live_venue",
    }
)
#: Freqtrade's `internals.process_throttle_secs` default: a bot reacts to the market at most every 5 s, so two bot
#: runs (dry-run vs live) are compared with this time tolerance by default.
BOT_TIME_TOL_NS = 5 * 1_000_000_000


# ======================================================================================================== generic


def sig15(d: Decimal) -> Decimal:
    """`d` rounded half-even to 15 significant digits (the decimal digits an IEEE double carries exactly)."""
    if d.is_zero() or len(d.as_tuple().digits) <= _SIG:
        return d
    q = Decimal(1).scaleb(d.adjusted() - _SIG + 1)
    return d.quantize(q, rounding=ROUND_HALF_EVEN).normalize()


def num(value: Any, what: str, location: str) -> Decimal | None:
    """Decimal of a float-origin value (SQLite REAL, JSON number) rounded to 15 significant digits; None for null.
    Booleans, NaN and infinities fail `QT-INPUT-002`."""
    if value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            raise input_error(location, f"{what}: {value!r} is not finite")
        return sig15(Decimal(repr(value)))
    d = dec(value, what, location)
    return None if d is None else sig15(d)


def dtext(d: Decimal | None) -> str | None:
    """Plain positional text of a decimal (`61000`, `0.0016`)."""
    if d is None:
        return None
    t = format(d.normalize(), "f")
    return "0" if t in ("-0", "") else t


def iso_ns(ns: int | None) -> str | None:
    """UTC ISO 8601 text of an ns timestamp with millisecond digits (`2024-03-01T10:05:02.481Z`)."""
    if ns is None:
        return None
    t = dt.datetime.fromtimestamp(ns // _NS, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{t}.{(ns % _NS) // 1_000_000:03d}Z"


def duration_text(ns: int) -> str:
    """`3m12s`-style text of a duration: sub-second in ms, sub-minute in seconds with one decimal (`2.3s`, `4s`),
    longer ones rounded to the second."""
    if ns < 0:
        return "-" + duration_text(-ns)
    if ns < _NS:
        return f"{ns // 1_000_000}ms"
    if ns < 60 * _NS:
        t = f"{round(ns / _NS, 1):.1f}".removesuffix(".0")
        return f"{t}s" if t != "60" else "1m"
    s = (ns + _NS // 2) // _NS
    parts = []
    for unit, size in (("d", 86_400), ("h", 3600), ("m", 60)):
        if s >= size:
            parts.append(f"{s // size}{unit}")
            s %= size
    if s or not parts:
        parts.append(f"{s}s")
    return "".join(parts)


def opt_int(opts: dict[str, Any], key: str, default: int) -> int:
    """Integer option (an int or integer text); anything else fails `QT-CONFIG-001`."""
    v = opts.get(key, default)
    if isinstance(v, bool):
        raise QtError("QT-CONFIG-001", f"{key}: {v!r} is not an integer")
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except ValueError:
        raise QtError("QT-CONFIG-001", f"{key}: {v!r} is not an integer") from None


def db_int(v: Any, what: str, loc: str) -> int:
    """Integer value of an input file (an int or integer text); anything else fails `QT-INPUT-002`."""
    if isinstance(v, bool):
        raise input_error(loc, f"{what}: {v!r} is not an integer")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    try:
        return int(str(v).strip())
    except ValueError:
        raise input_error(loc, f"{what}: {v!r} is not an integer") from None


def timeframe_seconds(value: Any) -> int:
    """Freqtrade timeframe (`1m`, `5m`, `1h`, `1d`, `1w`, or integer minutes as in `trades.timeframe`) -> seconds;
    months and malformed values fail `QT-CONFIG-001`."""
    if isinstance(value, bool):
        raise QtError("QT-CONFIG-001", f"timeframe: {value!r} is not a timeframe")
    if isinstance(value, int):
        if value <= 0:
            raise QtError("QT-CONFIG-001", f"timeframe: {value} minutes must be positive")
        return value * 60
    m = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", str(value))
    if not m or int(m.group(1)) <= 0:
        raise QtError("QT-CONFIG-001", f"timeframe: {value!r} is not a fixed-length Freqtrade timeframe (5m, 1h, 1d)")
    return int(m.group(1)) * _TF_UNIT[m.group(2)]


@dataclass
class OrderView:
    """One order of one arm as the explanation sees it (exact decimals, ns timestamps)."""

    key: str
    instrument: str
    side: str
    role: str
    order_type: str
    qty: Decimal
    limit_px: Decimal | None
    placed_ns: int | None
    filled_qty: Decimal
    avg_px: Decimal | None
    first_fill_ns: int | None
    end_ns: int | None
    status: str
    note: str | None = None
    group: str | None = None
    #: time resolution of `first_fill_ns` (the candle length of a backtest; 0 = exact).
    resolution_ns: int = 0


CATEGORIES: tuple[str, ...] = (
    "match",
    "missed_fill",
    "extra_live_fill",
    "partial_fill",
    "price",
    "timing",
    "both_unfilled",
    "stop_not_triggered",
    "no_sim_twin",
    "no_live_twin",
)


#: below these the score's significance statement is not interpretable (a cluster-robust test needs clusters to vary
#: over; a handful of matched orders is an anecdote): the report says so in front of the headline.
MIN_SCORE_CLUSTERS = 2
MIN_SCORE_MATCHED = 10


def _status_of(v: OrderView) -> str:
    if v.filled_qty > 0:
        return "filled" if v.filled_qty >= v.qty else "partial"
    return v.status


def _bps(live: OrderView, sim: OrderView) -> tuple[float | None, Decimal | None]:
    if live.avg_px is None or sim.avg_px is None or live.avg_px <= 0:
        return None, None
    diff = dsub(live.avg_px, sim.avg_px) if live.side == "buy" else dsub(sim.avg_px, live.avg_px)
    joint = min(live.filled_qty, sim.filled_qty)
    return float(diff / live.avg_px * 10_000), diff * joint


def _px(d: Decimal | None) -> str:
    return "unknown price" if d is None else (dtext(d) or "0")


def _fmt_order(v: OrderView) -> str:
    px = f" @ {dtext(v.limit_px)}" if v.limit_px is not None else f" ({v.order_type})"
    return f"{v.role} {v.side} {dtext(v.qty)} {v.instrument}{px}"


def explain_pair(
    live: OrderView | None,
    sim: OrderView | None,
    *,
    price_tol_bps: float,
    time_tol_ns: int,
    sim_name: str,
    live_name: str,
    no_sim_hint: str,
    no_live_hint: str,
    score_class: str | None = None,
) -> ReconcileOrder:
    """Category, issues and explanation text of one live / simulator pair (either side may be missing)."""
    base = live if live is not None else sim
    assert base is not None
    row: dict[str, Any] = {
        "live_key": live.key if live else None,
        "sim_key": sim.key if sim else None,
        "instrument": base.instrument,
        "side": base.side,
        "role": base.role,
        "score_class": score_class,
        "qty": dtext(base.qty),
    }
    for arm, v in (("live", live), ("sim", sim)):
        if v is None:
            continue
        row[f"{arm}_status"] = _status_of(v)
        row[f"{arm}_limit_px"] = dtext(v.limit_px)
        row[f"{arm}_filled_qty"] = dtext(v.filled_qty)
        row[f"{arm}_avg_px"] = dtext(v.avg_px)
        row[f"{arm}_first_fill"] = iso_ns(v.first_fill_ns)
        row[f"{arm}_note"] = v.note
    if live is not None:
        row["live_placed"] = iso_ns(live.placed_ns)
    issues: list[str] = []
    one = live if sim is None else (sim if live is None else None)
    if (
        live is not None
        and sim is not None
        and live.role == sim.role == "stoploss"
        and live.filled_qty == sim.filled_qty == 0
    ):
        text = (
            f"{_fmt_order(live)}: neither stop-loss triggered ({live_name} {_status_of(live)}, {sim_name} "
            f"{_status_of(sim)}; the bot cancels its exchange stop-loss when the trade exits): agreement"
        )
        return ReconcileOrder(category="stop_not_triggered", issues=[], explanation=text, **row)
    if one is not None and one.role == "stoploss" and one.filled_qty == 0:
        arm = live_name if one is live else sim_name
        other = sim_name if one is live else live_name
        text = (
            f"{arm} {_fmt_order(one)} never triggered and ended {_status_of(one)} (the bot cancels its exchange "
            f"stop-loss when the trade exits); the {other} has no untriggered stop order to pair: agreement, not "
            "divergence (a stop that did trigger is an exit and pairs as one)"
        )
        return ReconcileOrder(category="stop_not_triggered", issues=[], explanation=text, **row)
    if live is None:
        assert sim is not None
        filled = sim.filled_qty > 0
        what = f"filled {dtext(sim.filled_qty)} at {_px(sim.avg_px)}" if filled else _status_of(sim)
        when = f" ({iso_ns(sim.first_fill_ns)})" if filled else ""
        text = f"{sim_name} {_fmt_order(sim)} {what}{when}; no {live_name} order pairs with it: {no_live_hint}"
        return ReconcileOrder(category="no_live_twin", issues=["no_live_twin"], explanation=text, **row)
    if sim is None:
        filled = live.filled_qty > 0
        what = f"filled {dtext(live.filled_qty)} at {_px(live.avg_px)}" if filled else _status_of(live)
        reason = f" ({live.note})" if live.note and not filled else ""
        text = f"{live_name} {_fmt_order(live)} {what}{reason}; no {sim_name} order pairs with it: {no_sim_hint}"
        return ReconcileOrder(category="no_sim_twin", issues=["no_sim_twin"], explanation=text, **row)
    lf, sf = live.filled_qty > 0, sim.filled_qty > 0
    parts: list[str] = []
    if sf and not lf:
        issues.append("missed_fill")
        rested = ""
        if live.end_ns is not None and live.placed_ns is not None:
            rested = f" rested {duration_text(live.end_ns - live.placed_ns)} and"
        why = f" ({live.note})" if live.note else ""
        rule = ""
        if sim.resolution_ns:
            rule = (
                f" in the {duration_text(sim.resolution_ns)} candle opening {iso_ns(sim.first_fill_ns)} (candle rule)"
            )
        parts.append(
            f"{sim_name} filled {dtext(sim.filled_qty)} at {_px(sim.avg_px)}{rule}; the {live_name} order{rested} "
            f"ended {_status_of(live)}{why} without a fill"
        )
    elif lf and not sf:
        issues.append("extra_live_fill")
        parts.append(
            f"{live_name} filled {dtext(live.filled_qty)} at {_px(live.avg_px)} ({iso_ns(live.first_fill_ns)}); "
            f"the {sim_name} order ended {_status_of(sim)} without a fill"
        )
    elif not lf and not sf:
        parts.append(f"neither arm filled (live {_status_of(live)}, {sim_name} {_status_of(sim)})")
    if lf and sf:
        if live.filled_qty != sim.filled_qty:
            issues.append("partial_fill")
            parts.append(f"filled quantity {live_name} {dtext(live.filled_qty)} vs {sim_name} {dtext(sim.filled_qty)}")
        bps, quote = _bps(live, sim)
        row["sim_price_advantage_bps"] = None if bps is None else round(bps, 6)
        row["sim_price_advantage_quote"] = dtext(quote)
        if bps is not None and abs(bps) > price_tol_bps:
            issues.append("price")
            better = "better" if bps > 0 else "worse"
            parts.append(
                f"price {live_name} {dtext(live.avg_px)} vs {sim_name} {dtext(sim.avg_px)}: the {sim_name} price is "
                f"{abs(bps):.2f} bps {better} ({dtext(quote)} quote on {dtext(min(live.filled_qty, sim.filled_qty))})"
            )
        if live.first_fill_ns is not None and sim.first_fill_ns is not None:
            delay = live.first_fill_ns - sim.first_fill_ns
            row["fill_delay_ns"] = delay
            res = sim.resolution_ns
            if res:
                outside = delay < -time_tol_ns or delay >= res + time_tol_ns
            else:
                outside = abs(delay) > time_tol_ns
            if outside:
                issues.append("timing")
                if res:
                    parts.append(
                        f"timing: {live_name} filled {iso_ns(live.first_fill_ns)}, outside the {sim_name} fill candle "
                        f"[{iso_ns(sim.first_fill_ns)}, +{duration_text(res)})"
                    )
                else:
                    lag = "after" if delay > 0 else "before"
                    parts.append(f"timing: {live_name} filled {duration_text(abs(delay))} {lag} the {sim_name}")
    order = ("missed_fill", "extra_live_fill", "partial_fill", "price", "timing")
    cat = next((c for c in order if c in issues), "match" if lf and sf else "both_unfilled")
    if cat == "match":
        parts.insert(0, f"both filled {dtext(live.filled_qty)} at {_px(live.avg_px)}")
    text = f"{_fmt_order(live)}: " + "; ".join(parts)
    return ReconcileOrder(category=cat, issues=issues, explanation=text, **row)  # type: ignore[arg-type]


def explain_pairs(
    pairs: list[tuple[OrderView | None, OrderView | None]],
    *,
    price_tol_bps: float = 0.5,
    time_tol_ns: int = 0,
    sim_name: str = "sim",
    live_name: str = "live",
    no_sim_hint: str = "",
    no_live_hint: str = "",
    score_classes: dict[str, str] | None = None,
) -> list[ReconcileOrder]:
    """`explain_pair` over every pair, sorted by (first time, keys)."""
    cls = score_classes or {}
    out: list[tuple[tuple[int, str, str], ReconcileOrder]] = []
    for live, sim in pairs:
        r = explain_pair(
            live,
            sim,
            price_tol_bps=price_tol_bps,
            time_tol_ns=time_tol_ns,
            sim_name=sim_name,
            live_name=live_name,
            no_sim_hint=no_sim_hint,
            no_live_hint=no_live_hint,
            score_class=cls.get(live.key) if live is not None else None,
        )
        t = min(
            [x for v in (live, sim) if v is not None for x in (v.placed_ns, v.first_fill_ns) if x is not None] or [0]
        )
        out.append(((t, r.live_key or "", r.sim_key or ""), r))
    out.sort(key=lambda x: x[0])
    return [r for _, r in out]


def summarize(rows: list[ReconcileOrder]) -> tuple[dict[str, int], dict[str, str], dict[str, str]]:
    """(counts per category, sim price advantage in quote per instrument, missed-fill notional per instrument)."""
    counts = {c: 0 for c in CATEGORIES}
    adv: dict[str, Decimal] = {}
    missed: dict[str, Decimal] = {}
    for r in rows:
        counts[r.category] += 1
        if r.sim_price_advantage_quote is not None:
            adv[r.instrument] = adv.get(r.instrument, Decimal(0)) + Decimal(r.sim_price_advantage_quote)
        if r.category == "missed_fill" and r.sim_filled_qty is not None:
            px = r.sim_avg_px or r.sim_limit_px
            if px is not None:
                missed[r.instrument] = missed.get(r.instrument, Decimal(0)) + Decimal(r.sim_filled_qty) * Decimal(px)
    return (
        counts,
        {k: dtext(v) or "0" for k, v in sorted(adv.items())},
        {k: dtext(v) or "0" for k, v in sorted(missed.items())},
    )


_CSV_COLUMNS = (
    "category",
    "live_key",
    "sim_key",
    "instrument",
    "side",
    "role",
    "score_class",
    "live_status",
    "sim_status",
    "qty",
    "live_limit_px",
    "sim_limit_px",
    "live_filled_qty",
    "sim_filled_qty",
    "live_avg_px",
    "sim_avg_px",
    "live_placed",
    "live_first_fill",
    "sim_first_fill",
    "fill_delay_ns",
    "sim_price_advantage_bps",
    "sim_price_advantage_quote",
    "explanation",
)


def write_reconcile(out: str | Path, report: ReconcileReport) -> list[Path]:
    """Writes `reconcile.json` (the report) and `reconcile_orders.csv` (one row per order) under `out`."""
    outp = Path(out)
    outp.mkdir(parents=True, exist_ok=True)
    j = outp / "reconcile.json"
    j.write_text(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    c = outp / "reconcile_orders.csv"
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_CSV_COLUMNS)
    for r in report.orders:
        d = r.model_dump(mode="json")
        w.writerow(["" if d.get(k) is None else d[k] for k in _CSV_COLUMNS])
    c.write_text(buf.getvalue(), encoding="utf-8")
    return [j, c]


def render_reconcile(report: ReconcileReport, *, max_rows: int | None = None) -> str:
    """Human text of a report: the score headline, the category counts and one line per order."""
    cov = "" if report.pairing_coverage is None else f" ({report.pairing_coverage:.0%} of live orders)"
    lines = [
        f"reconcile ({report.kind}): {report.n_live_orders} live orders, {report.n_sim_orders} simulator orders, "
        f"{report.n_paired} paired{cov}"
    ]
    if report.score is not None:
        s = report.score
        if s.interpretable:
            lines.append(f"score: {s.headline}")
        else:
            lines.append(
                f"score (not interpretable: {s.n_matched} matched orders in {s.n_clusters or 0} cluster(s), it needs "
                f"{MIN_SCORE_MATCHED} matched orders and {MIN_SCORE_CLUSTERS} clusters): {s.headline}"
            )
        lines.append(
            "score orders: "
            + ", ".join(f"{k} {s.two_by_two.get(k, 0)}" for k in ("both", "neither", "live_only", "sim_only"))
        )
    elif report.score_error:
        lines.append(f"score: not run ({report.score_error})")
    lines.append("categories: " + ", ".join(f"{k} {v}" for k, v in report.counts.items() if v))
    for inst, v in report.sim_price_advantage_quote.items():
        lines.append(f"sim price advantage {inst}: {v} (quote)")
    for inst, v in report.missed_fill_notional.items():
        lines.append(f"missed-fill notional {inst}: {v} (quote)")
    rows = report.orders if max_rows is None else report.orders[:max_rows]
    for r in rows:
        lines.append(f"[{r.category}] {r.live_key or r.sim_key}: {r.explanation}")
    return "\n".join(lines)


def reconcile_spec(
    kind: str,
    sim: list[str],
    live: list[str],
    *,
    instrument_map: str | None,
    options: dict[str, Any],
    **settings: Any,
) -> dict[str, Any]:
    """The reconciliation's spec: inputs as `FileRef`s (`{"path"}`, hashed by content), the instrument map likewise,
    every setting and option (JSON values). `run_id` and the report's `options` echo are built from it."""

    def js(v: Any) -> Any:
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        if isinstance(v, Decimal):
            return dtext(v)
        if isinstance(v, (list, tuple)):
            return [js(x) for x in v]
        if isinstance(v, dict):
            return {str(k): js(x) for k, x in v.items()}
        return str(v)

    return {
        "kind": f"{kind}_reconcile",
        "sim": [{"path": p} for p in sim],
        "live": [{"path": p} for p in live],
        "instrument_map": None if instrument_map is None else {"path": instrument_map},
        **{k: js(v) for k, v in sorted(settings.items())},
        "options": {k: js(options[k]) for k in sorted(options)},
    }


def reconcile_run_id(spec: dict[str, Any]) -> str:
    """SPEC 7.3 `run_id` of a reconciliation spec: RFC 8785 canonical JSON of the spec (each `{"path"}` replaced by
    the file's sha256) with the engine version."""
    from queuetruth.schemas.arrow import ENGINE_VERSION
    from queuetruth.schemas.runid import run_id

    return run_id(spec, ENGINE_VERSION, lambda ref: sha256_file(ref["path"]))


def auto_instrument_map(rows: list[tuple[str, str]], *, price_exp: int = -9, size_exp: int = -8) -> InstrumentMap:
    """An in-memory instrument map giving each (venue, instrument) an id 1..n in sorted order with the default
    scales (tick 10^price_exp, lot 10^size_exp): enough to schedule and score two bot logs against each other
    without a market-data dataset (whose ids would have to match)."""
    entries = []
    for i, (venue, inst) in enumerate(sorted(set(rows)), start=1):
        entries.append(InstrumentEntry(venue.lower(), inst, i, None, price_exp, size_exp, 1, 1))
    return InstrumentMap(entries)


def run_score(
    live_dir: Path, sim_dir: Path, out: Path, *, window_ns: int, scorer: Callable[..., Any] | None = None
) -> tuple[ReconcileScore | None, str | None, dict[str, str]]:
    """`queuetruth.api.score` with the simulator log as the external arm; returns (result, error, order classes)."""
    from queuetruth import api

    spec = {
        "live": {"path": str(live_dir)},
        "sim": {"kind": "external", "log": {"path": str(sim_dir)}},
        "out": str(out),
        "window_ns": int(window_ns),
    }
    try:
        doc = (scorer or api.score)(spec)
    except QtError as e:
        return None, str(e), {}
    d = doc.model_dump(mode="json", by_alias=True) if hasattr(doc, "model_dump") else dict(doc)
    classes: dict[str, str] = {}
    so = out / "score_orders.parquet"
    if so.exists():
        for r in pq.read_table(so, columns=["order_key", "class"]).to_pylist():
            classes[str(r["order_key"])] = str(r["class"])
    clustered = d.get("discordance_clustered") or {}
    n_clusters = clustered.get("n_clusters")
    n_matched = int(d.get("n_matched") or 0)
    res = ReconcileScore(
        path=out.name,
        n_clusters=None if n_clusters is None else int(n_clusters),
        interpretable=n_matched >= MIN_SCORE_MATCHED and int(n_clusters or 0) >= MIN_SCORE_CLUSTERS,
        run_id=str(d.get("run_id")),
        headline=str((d.get("verdict") or {}).get("headline", "")),
        n_orders=int(d.get("n_orders") or 0),
        n_matched=n_matched,
        n_unmatched_live=int(d.get("n_unmatched_live") or 0),
        n_unmatched_sim=int(d.get("n_unmatched_sim") or 0),
        two_by_two={k: int(v) for k, v in (d.get("two_by_two") or {}).items() if v is not None},
        fill_rate_bias=(d.get("bias") or {}).get("fill_rate_bias"),
    )
    return res, None, classes


def finish_to_dir(
    venue: str, res: ImportResult, paths: list[str], out: Path, *, append: bool = False, status: str = "fixture_only"
) -> Any:
    """Writes an import result as a canonical live-log directory with the shared import summary rules
    (`queuetruth.live._finish_import`: validation, coverage, unmapped instruments, manifest)."""
    from queuetruth.live import _finish_import

    return _finish_import(venue, res, paths, out, append=append, status=status)


def check_options(extra: dict[str, Any], allowed: frozenset[str], name: str) -> None:
    """`QT-CONFIG-001` for an option the importer does not read."""
    bad = sorted(set(extra) - allowed - {"fix"})
    if bad:
        raise QtError("QT-CONFIG-001", f"{name} importer: unknown option(s) {bad}; known: {sorted(allowed)}")


# ======================================================================================================= freqtrade


@dataclass
class FtOrder:
    """One Freqtrade order of a backtest, a trades database or a bot log."""

    ref: str
    trade: FtTrade
    role: str  # entry | exit | stoploss
    side: str
    order_type: str
    qty: Decimal
    filled: Decimal
    limit_px: Decimal | None
    avg_px: Decimal | None
    placed_ns: int | None
    filled_ns: int | None
    end_ns: int | None
    status: str
    cancel_reason: str | None
    tag: str | None
    rec: Record
    fee: Decimal | None = None
    fee_ccy: str | None = None
    key: str | None = None
    #: True / False when it is known whether the bot sent the cancel (bot log); None: from `cancel_reason`.
    bot_cancel: bool | None = None
    #: backtest: the exported `safe_price` (the order's rate, whatever its type).
    rate: Decimal | None = None


@dataclass
class FtTrade:
    """One Freqtrade trade (a position from entry to exit) with its orders."""

    ref: str
    pair: str
    is_short: bool
    open_ns: int
    is_open: bool
    enter_tag: str | None
    exit_reason: str | None
    source: str  # backtest | db | log
    futures: bool = False
    orders: list[FtOrder] = field(default_factory=list)

    @property
    def entry_side(self) -> str:
        return "sell" if self.is_short else "buy"

    @property
    def exit_side(self) -> str:
        return "buy" if self.is_short else "sell"


@dataclass
class FtRun:
    """Every trade of one Freqtrade run (a backtest, or a bot's database plus its log)."""

    kind: str  # backtest | dry_run | live
    venue: str | None
    timeframe_s: int | None
    trades: list[FtTrade]
    paths: list[str]
    warnings: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    strategy: str | None = None
    stake_currency: str | None = None
    #: the config's `order_types` (backtest zip / `_config.json`), when present.
    order_types: dict[str, Any] | None = None

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def orders(self) -> list[FtOrder]:
        return [o for t in self.trades for o in t.orders]


def _kind_of(path: Path) -> str:
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{path}: {e}") from e
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if head.startswith(b"SQLite format 3\x00"):
        return "sqlite"
    if head.lstrip()[:1] in (b"{", b"["):
        return "json"
    if not head:
        raise input_error(str(path), "empty file")
    return "log"


def _ms(v: Any, what: str, loc: str) -> int | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, float):
        if not math.isfinite(v) or v != int(v):
            raise input_error(loc, f"{what}: {v!r} is not an integer epoch value")
        v = int(v)
    return epoch_ns(v, "ms", what, loc)[0]


def _dbtime(v: Any, what: str, loc: str) -> int | None:
    """SQLAlchemy DateTime text (naive UTC) or an ISO text with an offset -> ns."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return _ms(v, what, loc)
    return rfc3339_ns(str(v).replace("+00:00", "Z"), what, loc, default_tz="UTC")[0]


def _bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return bool(v)


def _otype(raw: str | None) -> str:
    s = (raw or "").lower().replace("-", "_")
    if "stop" in s:
        return "stop_limit" if "limit" in s else "stop"
    if s in ("limit", "market"):
        return s
    return "other"


def _json_doc(text: str, where: str) -> Any:
    try:
        return json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as e:
        raise input_error(where, f"not JSON: {e}") from e


def _read_backtest_doc(path: Path, *, follow: bool = True) -> tuple[Any, dict[str, Any] | None, str]:
    """(results document, config document or None, location) of a zip, a results JSON or `.last_result.json`
    (followed once: its `latest_backtest` must be a bare file name next to it, as Freqtrade writes it)."""
    kind = _kind_of(path)
    if kind == "zip":
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as e:
            raise input_error(str(path), f"not a readable zip: {e}") from e
        with zf:
            names = zf.namelist()
            want = f"{path.stem}.json"
            if want not in names:
                cands = [
                    n
                    for n in names
                    if n.endswith(".json") and not n.endswith(("_config.json", ".meta.json")) and "/" not in n
                ]
                cands = [n for n in cands if n.startswith("backtest-result") and n.count("_") <= 1] or cands
                if len(cands) != 1:
                    raise input_error(str(path), f"no backtest result member {want} (members: {sorted(names)})")
                want = cands[0]
            try:
                body = zf.read(want).decode("utf-8")
            except (zipfile.BadZipFile, OSError, UnicodeDecodeError, EOFError) as e:
                raise input_error(f"{path}!{want}", f"unreadable member: {e}") from e
            cfg_name = f"{Path(want).stem}_config.json"
            cfg = None
            if cfg_name in names:
                c = _json_doc(zf.read(cfg_name).decode("utf-8"), f"{path}!{cfg_name}")
                cfg = c if isinstance(c, dict) else None
            return _json_doc(body, f"{path}!{want}"), cfg, f"{path}!{want}"
    if kind != "json":
        raise input_error(str(path), "not a Freqtrade backtest result (.zip or .json)")
    if path.name.endswith(".meta.json"):
        raise input_error(str(path), "a backtest metadata file; pass the backtest-result .zip or .json")
    doc = _json_doc(read_text(path), str(path))
    if isinstance(doc, dict) and set(doc) == {"latest_backtest"}:
        name = doc["latest_backtest"]
        if not follow:
            raise input_error(str(path), "latest_backtest points at another .last_result.json")
        if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
            raise input_error(str(path), f"latest_backtest {name!r} is not a bare file name")
        target = path.parent / name
        if not target.exists():
            raise QtError("QT-INPUT-001", f"{path}: latest_backtest {target} not found")
        return _read_backtest_doc(target, follow=False)
    cfg_path = path.with_name(f"{path.stem}_config.json")
    cfg = None
    if cfg_path.exists():
        c = _json_doc(read_text(cfg_path), str(cfg_path))
        cfg = c if isinstance(c, dict) else None
    return doc, cfg, str(path)


def read_backtest(path: str | Path, *, strategy: str | None = None, timeframe: Any = None) -> FtRun:
    """A Freqtrade backtest result (zip, JSON or `.last_result.json`) as an `FtRun` of kind `backtest`."""
    p = Path(path)
    doc, cfg, loc = _read_backtest_doc(p)
    if isinstance(doc, list):
        raise input_error(loc, "a list-only backtest result (pre-2021 format, unsupported by Freqtrade too)")
    if not isinstance(doc, dict) or not isinstance(doc.get("strategy"), dict) or not doc["strategy"]:
        raise input_error(loc, "no 'strategy' results object (not a Freqtrade backtest result)")
    strategies = doc["strategy"]
    if strategy is None:
        if len(strategies) != 1:
            raise QtError(
                "QT-CONFIG-001",
                f"{loc}: {len(strategies)} strategies ({sorted(strategies)}); pass option strategy=<name>",
            )
        strategy = next(iter(strategies))
    if strategy not in strategies:
        raise QtError("QT-CONFIG-001", f"{loc}: strategy {strategy!r} not in the result ({sorted(strategies)})")
    st = strategies[strategy]
    if not isinstance(st, dict) or not isinstance(st.get("trades"), list):
        raise input_error(f"{loc}/{strategy}", "strategy result without a 'trades' list")
    tf_raw = timeframe if timeframe is not None else st.get("timeframe")
    run = FtRun(
        kind="backtest",
        venue=None,
        timeframe_s=timeframe_seconds(tf_raw) if tf_raw is not None else None,
        trades=[],
        paths=[str(p)],
        strategy=strategy,
        stake_currency=st.get("stake_currency") if isinstance(st.get("stake_currency"), str) else None,
    )
    if cfg is not None and isinstance(cfg.get("exchange"), dict) and cfg["exchange"].get("name"):
        run.venue = str(cfg["exchange"]["name"]).lower()
    if cfg is not None and isinstance(cfg.get("order_types"), dict):
        run.order_types = cfg["order_types"]
    else:
        run.warn(
            "backtest: no order_types in the result's config: unpaired orders are typed limit (paired orders take "
            "the live order's type)"
        )
    for k in (
        "timedout_entry_orders",
        "timedout_exit_orders",
        "canceled_trade_entries",
        "canceled_entry_orders",
        "replaced_entry_orders",
        "rejected_signals",
        "timeframe_detail",
        "backtest_start_ts",
        "backtest_end_ts",
    ):
        if st.get(k) is not None:
            run.stats[k] = int(st[k]) if isinstance(st[k], Decimal) else st[k]
    if run.timeframe_s is None:
        run.warn("backtest: no timeframe in the result; pass option timeframe=<5m> for candle-level pairing")
    for i, t in enumerate(st["trades"]):
        if not isinstance(t, dict):
            raise input_error(f"{loc}/{strategy}/trades[{i}]", "trade is not an object")
    rows = sorted(
        enumerate(st["trades"]), key=lambda x: (_ms(x[1].get("open_timestamp"), "open_timestamp", loc) or 0, x[0])
    )
    tf_ns = (run.timeframe_s or 0) * _NS
    no_orders = 0
    unfilled = 0
    for n, (i, t) in enumerate(rows):
        tloc = f"{loc}/{strategy}/trades[{i}]"
        pair = t.get("pair")
        if not isinstance(pair, str) or not pair:
            raise input_error(tloc, "trade without pair")
        open_ns = _ms(t.get("open_timestamp"), "open_timestamp", tloc)
        if open_ns is None:
            raise input_error(tloc, "trade without open_timestamp")
        close_ns = _ms(t.get("close_timestamp"), "close_timestamp", tloc)
        trade = FtTrade(
            ref=f"bt{n}",
            pair=pair,
            is_short=_bool(t.get("is_short")),
            open_ns=open_ns,
            is_open=_bool(t.get("is_open")),
            enter_tag=t.get("enter_tag") if isinstance(t.get("enter_tag"), str) else None,
            exit_reason=t.get("exit_reason") if isinstance(t.get("exit_reason"), str) else None,
            source="backtest",
        )
        if trade.is_open:
            run.warn("backtest: trades left open at the end of the backtest (is_open = true) are included")
        orders = t.get("orders")
        derived = not isinstance(orders, list) or not orders
        if derived:
            no_orders += 1
            orders = [
                {"ft_is_entry": True, "amount": t.get("amount"), "safe_price": t.get("open_rate")},
                {"ft_is_entry": False, "amount": t.get("amount"), "safe_price": t.get("close_rate")},
            ]
            orders[0]["order_filled_timestamp"] = t.get("open_timestamp")
            orders[1]["order_filled_timestamp"] = t.get("close_timestamp")
            if close_ns is None:
                orders = orders[:1]
        counts = {"entry": 0, "exit": 0}
        last_fill = open_ns
        for j, o in enumerate(orders):
            oloc = f"{tloc}/orders[{j}]"
            if not isinstance(o, dict):
                raise input_error(oloc, "order is not an object")
            if o.get("ft_is_entry") is not None:
                role = "entry" if _bool(o["ft_is_entry"]) else "exit"
            elif o.get("ft_order_side") is not None:
                role = "entry" if str(o["ft_order_side"]).lower() == trade.entry_side else "exit"
            else:
                raise input_error(oloc, "order without ft_is_entry / ft_order_side")
            counts[role] += 1
            qty = num(o.get("amount"), "amount", oloc)
            px = num(o.get("safe_price"), "safe_price", oloc)
            if qty is None or qty <= 0:
                raise input_error(oloc, f"order amount must be positive, got {o.get('amount')!r}")
            if px is None or px <= 0:
                raise input_error(oloc, f"order safe_price must be positive, got {o.get('safe_price')!r}")
            filled_ns = _ms(o.get("order_filled_timestamp"), "order_filled_timestamp", oloc)
            if filled_ns is None and derived:
                # pre-2022 export: the trade's open / close time is the only time there is
                filled_ns = open_ns if role == "entry" else close_ns
                if filled_ns is None:
                    raise input_error(oloc, "trade without close_timestamp for its exit")
            if filled_ns is not None and tf_ns and filled_ns % tf_ns:
                run.warn("backtest: some fill times are not candle open times (timeframe_detail runs)")
            first_entry = role == "entry" and counts["entry"] == 1
            is_filled = filled_ns is not None
            if not is_filled:
                # `select_filled_or_open_orders` exports orders still open at the end of the backtest: unfilled,
                # placed (at the latest) after the trade's last fill
                unfilled += 1
            else:
                last_fill = max(last_fill, filled_ns)
            otype, olimit = _bt_type(run.order_types, role, o.get("ft_order_tag") or t.get("exit_reason"), px)
            fee_rate = num(t.get("fee_open" if role == "entry" else "fee_close"), "fee", oloc)
            tag = o.get("ft_order_tag")
            rec = Record(o, json.dumps(o, sort_keys=True, default=str), oloc, 0, n * 1000 + j)
            trade.orders.append(
                FtOrder(
                    ref=f"{trade.ref}-{pair}-{role}{counts[role]}",
                    trade=trade,
                    role=role,
                    side=trade.entry_side if role == "entry" else trade.exit_side,
                    order_type=otype,
                    qty=qty,
                    filled=qty if is_filled else Decimal(0),
                    limit_px=olimit,
                    avg_px=px if is_filled else None,
                    placed_ns=open_ns if first_entry else (filled_ns if is_filled else last_fill),
                    filled_ns=filled_ns,
                    end_ns=None,
                    status="closed" if is_filled else "open",
                    cancel_reason=None,
                    tag=tag if isinstance(tag, str) else (trade.enter_tag if role == "entry" else trade.exit_reason),
                    rec=rec,
                    fee=None if fee_rate is None or not is_filled else fee_rate * qty * px,
                    fee_ccy=run.stake_currency,
                    rate=px,
                )
            )
        run.trades.append(trade)
    if no_orders:
        run.warn(
            f"backtest: {no_orders} trade(s) without an orders list (pre-2022 export): one entry and one exit order "
            "derived from open_rate / close_rate"
        )
    if unfilled:
        run.warn(
            f"backtest: {unfilled} exported order(s) without order_filled_timestamp (open at the end of the "
            "backtest): unfilled, placed at the trade's last fill"
        )
    return run


#: exit reasons Freqtrade's backtest fills at a computed stop rate (`ExitType`): typed as the stop-loss order.
_STOP_EXITS = frozenset({"stop_loss", "stoploss_on_exchange", "trailing_stop_loss", "liquidation"})


def _bt_type(order_types: dict[str, Any] | None, role: str, reason: Any, px: Decimal) -> tuple[str, Decimal | None]:
    """(canonical order type, limit price) of a backtest order from the config's `order_types` (entry, exit,
    emergency_exit, force_exit, stoploss); `limit` without them."""
    if not order_types:
        return "limit", px
    key = "entry"
    if role != "entry":
        r = str(reason or "")
        key = "stoploss" if r in _STOP_EXITS else (r if r in ("emergency_exit", "force_exit") else "exit")
    raw = str(order_types.get(key) or order_types.get("exit" if role != "entry" else "entry") or "limit").lower()
    if key == "stoploss":
        return ("stop_limit", px) if raw == "limit" else ("stop", None)
    return ("market", None) if raw == "market" else ("limit", px)


_DB_TRADES_REQUIRED = ("id", "pair", "open_date")
_DB_ORDERS_REQUIRED = ("ft_trade_id", "ft_order_side", "order_id", "status", "order_date")


def read_trades_db(path: str | Path, *, dry_run: bool | None = None, venue: str | None = None) -> FtRun:
    """A Freqtrade trades database (read-only) as an `FtRun` of kind `dry_run` or `live`."""
    p = Path(path)
    if not p.exists():
        raise QtError("QT-INPUT-001", f"{p}: input not found")
    try:
        con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise input_error(str(p), f"cannot open SQLite database: {e}") from e
    try:
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        except sqlite3.DatabaseError as e:
            raise input_error(str(p), f"not a readable SQLite database: {e}") from e
        for t in ("trades", "orders"):
            if t not in tables:
                raise input_error(str(p), f"no '{t}' table (not a Freqtrade trades database; tables: {sorted(tables)})")
        cols = {t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")} for t in ("trades", "orders")}
        for t, req in (("trades", _DB_TRADES_REQUIRED), ("orders", _DB_ORDERS_REQUIRED)):
            miss = [c for c in req if c not in cols[t]]
            if miss:
                raise input_error(str(p), f"table {t} lacks column(s) {miss}")
        trades = [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY id")]
        orders = [dict(r) for r in con.execute("SELECT * FROM orders ORDER BY id")]
    finally:
        con.close()
    ids = [str(o["order_id"]) for o in orders if o.get("order_id") is not None]
    n_dry = sum(1 for i in ids if i.startswith("dry_run_"))
    if dry_run is None:
        if n_dry and n_dry != len(ids):
            raise input_error(str(p), f"{n_dry} of {len(ids)} orders are dry-run orders: mixed dry-run / live database")
        dry_run = bool(ids) and n_dry == len(ids)
    exchanges = sorted({str(t["exchange"]).lower() for t in trades if t.get("exchange")})
    if venue is None and len(exchanges) > 1:
        raise QtError("QT-CONFIG-001", f"{p}: trades on {exchanges}; pass option venue=<name> (one exchange per log)")
    run = FtRun(
        kind="dry_run" if dry_run else "live",
        venue=(venue or (exchanges[0] if exchanges else "freqtrade")).lower(),
        timeframe_s=None,
        trades=[],
        paths=[str(p)],
    )
    tfs = sorted(
        {
            db_int(t["timeframe"], "timeframe (minutes)", f"{p}:trades#{t['id']}")
            for t in trades
            if t.get("timeframe") not in (None, "")
        }
    )
    if len(tfs) == 1:
        run.timeframe_s = tfs[0] * 60
    elif len(tfs) > 1:
        run.warn(f"database: trades with timeframes {tfs} minutes; pass option timeframe")
    by_id: dict[Any, FtTrade] = {}
    fees: dict[Any, dict[str, Any]] = {}
    for t in trades:
        loc = f"{p}:trades#{t['id']}"
        open_ns = _dbtime(t.get("open_date"), "open_date", loc)
        if open_ns is None:
            raise input_error(loc, "trade without open_date")
        if not t.get("pair"):
            raise input_error(loc, "trade without pair")
        tr = FtTrade(
            ref=f"t{t['id']}",
            pair=str(t["pair"]),
            is_short=_bool(t.get("is_short")),
            open_ns=open_ns,
            is_open=_bool(t.get("is_open")),
            enter_tag=t.get("enter_tag"),
            exit_reason=t.get("exit_reason"),
            source="db",
            futures=str(t.get("trading_mode") or "").lower() == "futures",
        )
        by_id[t["id"]] = tr
        fees[t["id"]] = t
        run.trades.append(tr)
    for i, o in enumerate(orders):
        loc = f"{p}:orders#{o.get('id', i)}"
        tr = by_id.get(o.get("ft_trade_id"))
        if tr is None:
            run.skip("order row whose trade is not in the trades table")
            continue
        if o.get("order_id") in (None, ""):
            raise input_error(loc, "order without order_id")
        fside = str(o.get("ft_order_side") or "").lower()
        if fside not in ("buy", "sell", "stoploss"):
            raise input_error(loc, f"ft_order_side {o.get('ft_order_side')!r} is not buy / sell / stoploss")
        role = "stoploss" if fside == "stoploss" else ("entry" if fside == tr.entry_side else "exit")
        side = str(o.get("side") or (tr.exit_side if fside == "stoploss" else fside)).lower()
        if side not in ("buy", "sell"):
            raise input_error(loc, f"side {o.get('side')!r} is not buy / sell")
        qty = num(o.get("amount"), "amount", loc) or num(o.get("ft_amount"), "ft_amount", loc)
        if qty is None or qty <= 0:
            raise input_error(loc, "order without a positive amount / ft_amount")
        filled = num(o.get("filled"), "filled", loc) or Decimal(0)
        if filled < 0 or filled > qty:
            raise input_error(loc, f"filled {dtext(filled)} outside [0, amount {dtext(qty)}]")
        otype = _otype(o.get("order_type"))
        px = num(o.get("price"), "price", loc) or num(o.get("ft_price"), "ft_price", loc)
        stop = num(o.get("stop_price"), "stop_price", loc)
        limit = px if otype in ("limit", "stop_limit") else None
        if otype == "stop" and stop is None:
            stop = px
        avg = num(o.get("average"), "average", loc)
        if avg is None or avg <= 0:
            avg = px if otype != "market" else None
        status = _STATUS.get(str(o.get("status") or "").lower())
        if status is None:
            raise input_error(loc, f"status {o.get('status')!r} is not a ccxt order status")
        rec = Record(o, json.dumps(o, sort_keys=True, default=str), loc, 0, i)
        ft = FtOrder(
            ref=str(o["order_id"]),
            trade=tr,
            role=role,
            side=side,
            order_type=otype,
            qty=qty,
            filled=filled,
            limit_px=limit,
            avg_px=avg if filled > 0 else None,
            placed_ns=_dbtime(o.get("order_date"), "order_date", loc),
            filled_ns=_dbtime(o.get("order_filled_date"), "order_filled_date", loc),
            end_ns=_dbtime(o.get("order_update_date"), "order_update_date", loc),
            status=status,
            cancel_reason=o.get("ft_cancel_reason"),
            tag=o.get("ft_order_tag"),
            rec=rec,
        )
        if ft.placed_ns is None:
            raise input_error(loc, "order without order_date")
        if 0 < filled < qty and ft.ref.startswith("dry_run_"):
            # dry-run limit orders fill all at once (`check_dry_limit_order_filled`) and a dry-run cancel resets
            # `filled` to 0: a partial dry-run order points to an edited or mixed database
            run.warn(
                "database: dry-run order(s) partially filled, which Freqtrade's dry-run never produces (edited or "
                "mixed database?)"
            )
        tr.orders.append(ft)
    for tid, tr in by_id.items():
        t = fees[tid]
        # a trade's fee cost belongs to its order only when exactly one order of that role filled
        for roles, cost_k, ccy_k in (
            (("entry",), "fee_open_cost", "fee_open_currency"),
            (("exit", "stoploss"), "fee_close_cost", "fee_close_currency"),
        ):
            filled = [o for o in tr.orders if o.filled > 0 and o.role in roles]
            cost = num(t.get(cost_k), cost_k, f"{p}:trades#{tid}")
            if len(filled) == 1 and cost is not None:
                filled[0].fee, filled[0].fee_ccy = cost, t.get(ccy_k)
        tr.orders.sort(key=lambda o: (o.placed_ns or 0, o.rec.line_idx))
    refs: dict[str, int] = {}
    for o in run.orders():
        refs[o.ref] = refs.get(o.ref, 0) + 1
    for o in run.orders():
        if refs[o.ref] > 1:
            o.ref = f"{o.trade.pair}/{o.ref}"
            run.warn("database: order ids repeated across pairs; their keys carry the pair")
    return run


def read_bot_log(path: str | Path, *, tz: str = "UTC", run: FtRun) -> int:
    """Adds the entry orders of trades Freqtrade deleted from its database (log lines, see the module docstring) to
    `run`; returns how many. Each deletion is one of `handle_cancel_enter` (`<Side> order fully cancelled.
    Removing Trade(...) from database.`, after `<Side> order <reason> for Trade(...).`), `handle_onexchange_order`
    (`Trade only had fully canceled entry orders. Removing ...`: cancelled on the exchange) or
    `replace_order_failed` (`Removing ...` after `Could not fully cancel order for Trade(...), therefore not
    replacing.`). Lines that mention a removal in another shape are counted in a warning."""
    p = Path(path)
    text = read_text(p)
    n = 0
    bad = 0
    #: Trade repr -> (cancel reason, the bot cancelled) from the line before its removal
    reasons: dict[str, tuple[str, bool]] = {}
    for i, raw_line in enumerate(text.splitlines()):
        line = raw_line.strip()
        head = _LOG_HEAD.match(line)
        msg = head.group("msg") if head else ""
        c = _LOG_CANCEL.match(msg)
        if c:
            reason = c.group("reason")
            reasons[c.group("trade")] = (reason, not reason.startswith(_EXCHANGE_CANCEL))
            continue
        rf = _LOG_REPLACE_FAILED.match(msg)
        if rf:
            reasons[rf.group("trade")] = (_CANCEL_REPLACE_FAILED, True)
            continue
        if "Removing Trade(" not in line:
            continue
        m = _LOG_REMOVE.match(msg)
        loc = f"{p}:{i + 1}"
        if head is None or m is None:
            bad += 1
            continue
        if m.group("amount") == "N/A" or m.group("rate") == "N/A" or m.group("since") == "closed":
            bad += 1
            continue
        cancel_ns = rfc3339_ns(head.group("ts"), "log time", loc, default_tz=tz)[0]
        since_ns = rfc3339_ns(m.group("since"), "open_since", loc, default_tz="UTC")[0]
        qty = dec(m.group("amount"), "amount", loc)
        px = dec(m.group("rate"), "open_rate", loc)
        if qty is None or qty <= 0 or px is None or px <= 0:
            raise input_error(loc, "amount and open_rate must be positive")
        short = m.group("short") == "True"
        tr = FtTrade(
            ref=f"deleted-{m.group('id')}-{since_ns // _NS}",
            pair=m.group("pair"),
            is_short=short,
            open_ns=since_ns,
            is_open=False,
            enter_tag=None,
            exit_reason=None,
            source="log",
        )
        side = (m.group("side") or tr.entry_side.capitalize()).lower()
        if side != tr.entry_side:
            raise input_error(loc, f"{m.group('side')} entry contradicts is_short={m.group('short')}")
        prior = reasons.pop(m.group("trade"), None)
        if msg.startswith("Trade only had"):
            reason, bot = f"{_EXCHANGE_CANCEL}; trade removed from the database (bot log)", False
        elif m.group("side"):
            if prior is None:
                reason, bot = "fully cancelled entry, reason not in the log; trade removed from the database", None
            else:
                reason, bot = f"{prior[0]}; trade removed from the database (bot log)", prior[1]
        else:
            reason, bot = f"{_CANCEL_REPLACE_FAILED}; trade removed from the database (bot log)", True
        rec = Record({"line": raw_line}, raw_line, loc, 1, i)
        tr.orders.append(
            FtOrder(
                ref=tr.ref,
                trade=tr,
                role="entry",
                side=side,
                order_type="limit",
                qty=qty,
                filled=Decimal(0),
                limit_px=px,
                avg_px=None,
                placed_ns=since_ns,
                filled_ns=None,
                end_ns=cancel_ns,
                status="canceled",
                cancel_reason=reason,
                tag=None,
                rec=rec,
                bot_cancel=bot,
            )
        )
        run.trades.append(tr)
        n += 1
    if bad:
        run.warn(f"bot log {p}: {bad} 'Removing Trade(...)' line(s) not in a recognised format (ignored)")
    run.paths.append(str(p))
    return n


def read_bot_run(paths: list[str], opts: dict[str, Any]) -> FtRun:
    """A trades database plus optional bot logs (one database per run)."""
    dbs = [p for p in paths if _kind_of(Path(p)) == "sqlite"]
    logs = [p for p in paths if _kind_of(Path(p)) == "log"]
    other = [p for p in paths if p not in dbs and p not in logs]
    if other:
        raise QtError(
            "QT-CONFIG-001",
            f"{other}: backtest results are a simulator arm; import them with import_sim('freqtrade', ...)",
        )
    if len(dbs) > 1:
        raise QtError("QT-CONFIG-001", f"{dbs}: one Freqtrade trades database per log (one bot run)")
    if dbs:
        run = read_trades_db(dbs[0], dry_run=opts.get("dry_run"), venue=opts.get("venue"))
    else:
        dry = opts.get("dry_run")
        run = FtRun(
            kind="live" if dry is False else "dry_run",
            venue=str(opts.get("venue") or "freqtrade").lower(),
            timeframe_s=None,
            trades=[],
            paths=[],
        )
        run.warn("no trades database: only orders of deleted trades from the bot log")
    for lg in logs:
        read_bot_log(lg, tz=str(opts.get("log_tz") or "UTC"), run=run)
    if opts.get("timeframe") is not None:
        run.timeframe_s = timeframe_seconds(opts["timeframe"])
    run.trades.sort(key=lambda t: (t.open_ns, t.ref))
    return run


def read_run(paths: list[str], opts: dict[str, Any]) -> FtRun:
    """Backtest result (one file) or bot run (database + logs)."""
    kinds = [_kind_of(Path(p)) for p in paths]
    if any(k in ("zip", "json") for k in kinds):
        if len(paths) != 1:
            raise QtError("QT-CONFIG-001", f"{paths}: one backtest result per simulator log")
        return read_backtest(paths[0], strategy=opts.get("strategy"), timeframe=opts.get("timeframe"))
    return read_bot_run(paths, opts)


# ---- pairing ------------------------------------------------------------------------------------------------------


@dataclass
class RunPairing:
    """Order pairs of a live and a simulator Freqtrade run (`(live, sim)`; either may be None)."""

    pairs: list[tuple[FtOrder | None, FtOrder | None]]
    trade_pairs: list[tuple[FtTrade, FtTrade]]
    timeframe_s: int

    def sim_to_live(self) -> dict[int, FtOrder]:
        return {id(s): lv for lv, s in self.pairs if lv is not None and s is not None}


def _floor(ns: int, tf: int) -> int:
    return ns - ns % tf


def _sim_ref_ns(o: FtOrder) -> int:
    if o.trade.source == "backtest":
        first = o.role == "entry" and o is next(x for x in o.trade.orders if x.role == "entry")
        return o.trade.open_ns if first else (o.filled_ns or o.trade.open_ns)
    return o.placed_ns or o.trade.open_ns


def _role(o: FtOrder) -> str:
    """Pairing role: a filled stop-loss is an exit; an untriggered one pairs only with an untriggered stop-loss of
    the other run (a backtest has none: it simulates the stop inside its exit)."""
    if o.role == "stoploss":
        return "exit" if o.filled > 0 else "stop_open"
    return o.role


def pair_runs(live: FtRun, sim: FtRun, *, timeframe_s: int | None = None, tolerance: int = 1) -> RunPairing:
    """Pairs a live and a simulator run by trade and role (rules in the module docstring)."""
    tf = timeframe_s or sim.timeframe_s or live.timeframe_s
    if not tf:
        raise QtError("QT-CONFIG-001", "pairing needs the timeframe: pass option timeframe=<5m>")
    if tolerance < 0:
        raise QtError("QT-CONFIG-001", f"pair_tolerance_candles must be >= 0, got {tolerance}")
    tf_ns = tf * _NS
    # simulator trades bucketed by (pair, direction) and sorted by entry candle: each live trade scans only the
    # candles within the tolerance (O((L + S) log S) for runs of any length)
    buckets: dict[tuple[str, bool], list[tuple[int, int, str, int]]] = {}
    for idx, st in enumerate(sim.trades):
        buckets.setdefault((st.pair, st.is_short), []).append((st.open_ns // tf_ns, st.open_ns, st.ref, idx))
    candles: dict[tuple[str, bool], list[int]] = {}
    for k, b in buckets.items():
        b.sort()
        candles[k] = [x[0] for x in b]
    taken: set[int] = set()
    trade_pairs: list[tuple[FtTrade, FtTrade]] = []
    for lt in sorted(live.trades, key=lambda t: (t.open_ns, t.ref)):
        key = (lt.pair, lt.is_short)
        b = buckets.get(key)
        if not b:
            continue
        lc = lt.open_ns // tf_ns
        cs = candles[key]
        best: tuple[tuple[int, int, str], int] | None = None
        for j in range(bisect.bisect_left(cs, lc - tolerance), bisect.bisect_right(cs, lc + tolerance)):
            c, open_ns, ref, idx = b[j]
            if idx in taken:
                continue
            k2 = (abs(c - lc), open_ns, ref)
            if best is None or k2 < best[0]:
                best = (k2, idx)
        if best is not None:
            taken.add(best[1])
            trade_pairs.append((lt, sim.trades[best[1]]))
    pairs: list[tuple[FtOrder | None, FtOrder | None]] = []
    used: set[int] = set()
    for lt, st in trade_pairs:
        for role in ("entry", "exit", "stop_open"):
            cands = [o for o in lt.orders if _role(o) == role]
            for so in sorted((o for o in st.orders if _role(o) == role), key=_sim_ref_ns):
                ref = _floor(_sim_ref_ns(so), tf_ns)
                before = [o for o in cands if id(o) not in used and _floor(o.placed_ns or 0, tf_ns) <= ref]
                hit = max(before, key=lambda o: (o.placed_ns or 0, o.rec.line_idx), default=None)
                if hit is None:
                    after = [
                        o
                        for o in cands
                        if id(o) not in used and _floor(o.placed_ns or 0, tf_ns) <= ref + tolerance * tf_ns
                    ]
                    hit = min(after, key=lambda o: (o.placed_ns or 0, o.rec.line_idx), default=None)
                if hit is not None:
                    used.add(id(hit))
                pairs.append((hit, so))
    paired_sim = {id(s) for _, s in pairs if s is not None}
    for o in live.orders():
        if id(o) not in used:
            pairs.append((o, None))
    for o in sim.orders():
        if id(o) not in paired_sim:
            pairs.append((None, o))
    return RunPairing(pairs=pairs, trade_pairs=trade_pairs, timeframe_s=tf)


# ---- canonical rows -----------------------------------------------------------------------------------------------


def adopt_live_types(pairing: RunPairing) -> int:
    """Gives each paired backtest order the order type of its live twin (the backtest exports no order type; the
    config's `order_types` only cover unpaired ones); returns how many changed. The backtest rate stays the limit
    price of limit and stop-limit types, market and stop types have none."""
    n = 0
    for lo, so in pairing.pairs:
        if lo is None or so is None or so.trade.source != "backtest" or so.order_type == lo.order_type:
            continue
        so.order_type = lo.order_type
        so.limit_px = so.rate if lo.order_type in ("limit", "stop_limit") else None
        n += 1
    return n


def _exps(imap: InstrumentMap | None, venue: str, inst: str, ts: int, opts: dict[str, Any]) -> tuple[int, int]:
    e = imap.lookup(venue, inst, utc_date_days(ts)) if imap is not None else None
    if e is not None:
        return e.price_exp, e.size_exp
    return opt_int(opts, "price_exp", -9), opt_int(opts, "size_exp", -8)


def _round(d: Decimal | None, exp: int, counter: list[int]) -> Decimal | None:
    if d is None:
        return None
    q = d.quantize(Decimal(1).scaleb(exp), rounding=ROUND_HALF_EVEN)
    if q != d:
        counter[0] += 1
    return q.normalize() if q else Decimal(0)


def _tif(opts: dict[str, Any]) -> str:
    t = str(opts.get("tif") or "gtc").lower()
    t = {"po": "post_only", "gtc": "gtc", "ioc": "ioc", "fok": "fok", "post_only": "post_only"}.get(t, "")
    if not t:
        raise QtError("QT-CONFIG-001", f"tif must be gtc, ioc, fok or po, got {opts.get('tif')!r}")
    return t


def _check_exact(d: Decimal | None, exp: int, what: str) -> None:
    if d is None:
        return
    if d.scaleb(-exp) != d.scaleb(-exp).to_integral_value():
        raise units_error(f"{what}: {dtext(d)} is not representable at exponent {exp}")


def emit_orders(
    b: LogBuilder,
    run: FtRun,
    orders: list[FtOrder],
    *,
    account: str,
    opts: dict[str, Any],
    imap: InstrumentMap | None,
    map_venue: str,
    sim: bool,
    sim_live: dict[int, FtOrder] | None = None,
    sim_name: str = FT_SIM,
) -> dict[str, int]:
    """Adds the orders of a run to a builder (keys, events as in the module docstring); returns counters."""
    tif = _tif(opts)
    clock_local = run.kind == "dry_run" or str(opts.get("order_date_clock") or "").lower() == "local"
    if str(opts.get("order_date_clock") or "exchange").lower() not in ("exchange", "local"):
        raise QtError("QT-CONFIG-001", f"order_date_clock must be exchange or local, got {opts['order_date_clock']!r}")
    rounded = [0]
    clamped = 0
    src = "sim" if sim else "rest"
    for o in orders:
        t = o.trade
        if sim:
            live = (sim_live or {}).get(id(o))
            key = live.key if live is not None and live.key else make_order_key(sim_name, account, o.ref)
        else:
            key = make_order_key(b.venue, account, o.ref)
        o.key = key
        pexp, sexp = _exps(imap, map_venue, t.pair, o.placed_ns or o.filled_ns or t.open_ns, opts)
        qty, limit, avg, filled = o.qty, o.limit_px, o.avg_px, o.filled
        if run.kind == "backtest":
            qty, limit = _round(qty, sexp, rounded), _round(limit, pexp, rounded)
            avg, filled = _round(avg, pexp, rounded), _round(filled, sexp, rounded)
            assert qty is not None and filled is not None
        else:
            _check_exact(qty, sexp, f"{o.rec.location} amount")
            _check_exact(limit, pexp, f"{o.rec.location} price")
        h = b.order(
            oid=o.ref,
            rec=o.rec,
            record=True,
            qty=qty,
            qty_text=dtext(qty),
            px=limit,
            px_text=dtext(limit),
            instrument=t.pair,
            side=o.side,
            order_type=o.order_type,
            tif=tif if o.order_type in ("limit", "stop_limit") else ("gtc" if o.order_type == "stop" else "other"),
            post_only=tif == "post_only" and o.order_type == "limit",
            reduce_only=True if (t.futures and o.role != "entry") else None,
            strategy_tag=o.tag,
        )
        b.set_order_field(h, "key_override", key)
        cs: dict[str, Any] = {"rec": o.rec, "source": src, "stream": False}
        if run.kind == "backtest":
            placed = o.placed_ns if o.placed_ns is not None else o.filled_ns
            b.event(h, "ack", kind="record_time", precision="ms", ts_exchange=placed, qty=qty, px=limit, **cs)
            if o.filled_ns is None:
                continue  # open at the end of the backtest
            fill_ts = o.filled_ns
            note = f"backtest candle fill: candle {iso_ns(o.filled_ns)}"
            if run.timeframe_s:
                note += f" ({run.timeframe_s // 60}m timeframe; low <= price <= high)"
            live = (sim_live or {}).get(id(o))
            if live is not None and live.placed_ns is not None and live.placed_ns > fill_ts:
                fill_ts = live.placed_ns
                clamped += 1
                note += "; time clamped to the paired live order's entry"
            b.event(
                h,
                "fill",
                kind="record_time",
                precision="ms",
                ts_exchange=fill_ts,
                qty=filled,
                px=avg,
                fill_id=f"{o.ref}:fill",
                liquidity="unknown",
                fee=o.fee,
                fee_ccy=o.fee_ccy,
                reason=note,
                **cs,
            )
            continue
        # ccxt order times are epoch milliseconds (`timestamp`, `lastTradeTimestamp`); dry-run orders `dt_ts()` ms
        prec = "ms"
        if t.source == "log":
            # restored from a bot-log line: open_since (bot clock, seconds) and the line's time (ms)
            b.event(h, "submit_sent", kind="unknown", precision="s", ts_local_send=o.placed_ns, qty=qty, px=limit, **cs)
        elif clock_local:
            b.event(
                h, "submit_sent", kind="unknown", precision=prec, ts_local_send=o.placed_ns, qty=qty, px=limit, **cs
            )
            b.event(h, "ack", kind="unknown", precision=prec, ts_local_recv=o.placed_ns, qty=qty, px=limit, **cs)
        else:
            b.event(h, "ack", kind="gateway_time", precision=prec, ts_exchange=o.placed_ns, qty=qty, px=limit, **cs)
        if filled > 0:
            ts = o.filled_ns or o.end_ns or o.placed_ns
            if o.filled_ns is None:
                run.warn("database: filled orders without order_filled_date use order_update_date")
            times = {"ts_local_recv": ts} if clock_local else {"ts_exchange": ts}
            b.event(
                h,
                "fill",
                kind="unknown" if clock_local else "record_time",
                precision=prec,
                qty=filled,
                px=avg,
                fill_id=f"{o.ref}:fill",
                liquidity="taker" if o.order_type in ("market", "stop") else "unknown",
                fee=o.fee,
                fee_ccy=o.fee_ccy,
                derived=True,
                soft_px=True,
                reason="Freqtrade order row: filled / average (one aggregate fill per order)",
                **times,
                **cs,
            )
        if o.status in ("canceled", "expired", "rejected"):
            ts = o.end_ns or o.placed_ns
            reason = o.cancel_reason or {"canceled": "cancelled on the exchange"}.get(o.status, o.status)
            if o.status == "canceled":
                bot = o.bot_cancel
                if bot is None:
                    bot = bool(o.cancel_reason) and not str(o.cancel_reason).startswith(_EXCHANGE_CANCEL)
                if bot:
                    b.event(h, "cancel_sent", kind="unknown", precision=prec, ts_local_send=ts, reason=reason, **cs)
                b.event(h, "cancel_ack", kind="unknown", precision=prec, ts_local_recv=ts, reason=reason, **cs)
            else:
                et = "expire" if o.status == "expired" else "reject"
                b.event(h, et, kind="unknown", precision=prec, ts_local_recv=ts, reason=reason, **cs)
    if rounded[0]:
        b.warn(f"{b.venue}: {rounded[0]} backtest price / size value(s) rounded half-even to the instrument exponents")
    if clamped:
        b.warn(f"{b.venue}: {clamped} backtest fill time(s) clamped to the paired live order's entry time")
    return {"rounded": rounded[0], "clamped": clamped}


def _builder(
    venue: str, account: str, imap: InstrumentMap | None, opts: dict[str, Any], *, sim: bool, map_venue: str
) -> LogBuilder:
    return LogBuilder(
        venue,
        account=account,
        imap=imap,
        price_exp=opt_int(opts, "price_exp", -9),
        size_exp=opt_int(opts, "size_exp", -8),
        map_venue=map_venue,
        validation_status="simulated" if sim else "fixture_only",
        source_override="sim" if sim else None,
    )


def _finish(b: LogBuilder, run: FtRun) -> ImportResult:
    for w in run.warnings:
        b.warn(f"{b.venue}: {w}")
    for reason, n in run.skipped.items():
        b.skip(reason, n)
    if run.kind == "dry_run" and b.validation_status != "simulated":
        b.validation_status = "simulated"
        b.warn(
            f"{b.venue}: dry_run: fills are Freqtrade dry-run fills (a limit order fills when the best opposite "
            "price of a polled order-book snapshot crosses it), not exchange executions; validation_status = simulated"
        )
    if run.kind != "backtest" and run.orders():
        b.warn(
            f"{b.venue}: tif: Freqtrade does not store the order time in force; limit orders use option tif "
            "(default gtc, Freqtrade's order_time_in_force default)"
        )
    return b.finish()


def import_freqtrade(paths: list[str], opts: ImportOptions) -> ImportResult:
    """Freqtrade trades database (+ bot logs) of a dry-run or live bot -> canonical rows (venue = the exchange)."""
    check_options(opts.extra, FT_OPTIONS - {"live_dry_run", "live_venue", "pair_with"}, "freqtrade")
    run = read_bot_run([str(p) for p in paths], opts.extra)
    venue = run.venue or "freqtrade"
    account = opts.account or "main"
    b = _builder(venue, account, opts.imap, opts.extra, sim=False, map_venue=venue)
    emit_orders(b, run, run.orders(), account=account, opts=opts.extra, imap=opts.imap, map_venue=venue, sim=False)
    return _finish(b, run)


def import_freqtrade_sim(paths: list[str], opts: ImportOptions) -> ImportResult:
    """Freqtrade backtest result (or a dry-run database as a simulator arm) -> canonical rows of venue
    `sim:freqtrade`; option `pair_with` (live database + logs) gives paired orders the live `order_key`."""
    check_options(opts.extra, FT_OPTIONS, "freqtrade")
    run = read_run([str(p) for p in paths], opts.extra)
    account = opts.account or "main"
    sim_live: dict[int, FtOrder] = {}
    live_venue = None
    pw = opts.extra.get("pair_with")
    live_opts = ("live_dry_run", "live_venue")
    if not pw and any(k in opts.extra for k in live_opts):
        raise QtError("QT-CONFIG-001", f"freqtrade importer: options {list(live_opts)} need pair_with")
    pw_paths: list[str] = []
    if pw:
        pw_paths = [str(x) for x in ([pw] if isinstance(pw, (str, Path)) else pw)]
        # the live arm's own options: dry_run / venue / order_date_clock describe the simulator arm
        lopts = {
            "dry_run": opts.extra.get("live_dry_run"),
            "venue": opts.extra.get("live_venue"),
            "log_tz": opts.extra.get("log_tz"),
            "timeframe": opts.extra.get("timeframe"),
        }
        live = read_bot_run(pw_paths, lopts)
        live_venue = live.venue
        for o in live.orders():
            o.key = make_order_key(live.venue or "freqtrade", account, o.ref)
        pairing = pair_runs(
            live,
            run,
            timeframe_s=timeframe_seconds(opts.extra["timeframe"]) if opts.extra.get("timeframe") else None,
            tolerance=opt_int(opts.extra, "pair_tolerance_candles", 1),
        )
        adopt_live_types(pairing)
        sim_live = pairing.sim_to_live()
    map_venue = str(opts.extra.get("map_venue") or opts.extra.get("venue") or live_venue or run.venue or "freqtrade")
    b = _builder(FT_SIM, account, opts.imap, opts.extra, sim=True, map_venue=map_venue)
    emit_orders(
        b,
        run,
        run.orders(),
        account=account,
        opts=opts.extra,
        imap=opts.imap,
        map_venue=map_venue,
        sim=True,
        sim_live=sim_live,
    )
    if not pw:
        b.warn(f"{FT_SIM}: no pair_with inputs: simulator orders keep sim:freqtrade keys and match no live order")
    else:
        n = len(sim_live)
        b.warn(f"{FT_SIM}: {n} of {len(run.orders())} simulator order(s) paired with a live order (pair_with)")
        refs = ", ".join(f"{x} (sha256 {sha256_file(x)[:16]})" for x in pw_paths)
        b.warn(
            f"{FT_SIM}: pair_with: keys taken from {refs} as venue {live_venue}, account {account}; import that live "
            "log with the same venue and account, or no key matches"
        )
    return _finish(b, run)


def import_freqtrade_live(
    inputs: list[str] | str,
    out: str | Path,
    *,
    account: str | None = None,
    instrument_map: str | None = None,
    append: bool = False,
    **options: Any,
) -> Any:
    """Freqtrade trades database (+ bot logs) -> canonical live-log directory `out`; returns the
    `LiveImportSummary`. Options as in the module docstring (`dry_run`, `order_date_clock`, `tif`, `log_tz`,
    `venue`, `timeframe`, `price_exp`, `size_exp`)."""
    paths = [inputs] if isinstance(inputs, str) else [str(p) for p in inputs]
    for p in paths:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
    imap = load_instrument_map(instrument_map) if instrument_map else None
    res = import_freqtrade(paths, ImportOptions(account=account, imap=imap, extra=dict(options)))
    status = "simulated" if any(o["validation_status"] == "simulated" for o in res.orders) else "fixture_only"
    return finish_to_dir(res.venue, res, paths, Path(out), append=append, status=status)


# ---- reconcile ------------------------------------------------------------------------------------------------------


def _view(o: FtOrder, *, tf_ns: int, backtest: bool) -> OrderView:
    note = o.cancel_reason if o.filled == 0 else None
    if o.role == "entry" and o.trade.enter_tag and note is None:
        note = f"enter_tag {o.trade.enter_tag}"
    if o.role != "entry" and o.tag and note is None:
        note = f"exit {o.tag}"
    end = o.end_ns if o.status in ("canceled", "expired", "rejected") else None
    return OrderView(
        key=o.key or o.ref,
        instrument=o.trade.pair,
        side=o.side,
        role=o.role,
        order_type=o.order_type,
        qty=o.qty,
        limit_px=o.limit_px,
        placed_ns=o.placed_ns,
        filled_qty=o.filled,
        avg_px=o.avg_px if o.filled > 0 else None,
        first_fill_ns=o.filled_ns if o.filled > 0 else None,
        end_ns=end,
        status={"canceled": "cancelled", "closed": "closed"}.get(o.status, o.status),
        note=note,
        group=o.trade.ref,
        resolution_ns=tf_ns if backtest else 0,
    )


def _paths(x: list[str | Path] | str | Path) -> list[str]:
    return [str(x)] if isinstance(x, (str, Path)) else [str(p) for p in x]


def reconcile_freqtrade(
    sim: list[str | Path] | str | Path,
    live: list[str | Path] | str | Path,
    out: str | Path,
    *,
    account: str = "main",
    instrument_map: str | Path | None = None,
    window_ns: int = 86_400 * _NS,
    price_tol_bps: float = 0.5,
    time_tol_ns: int | None = None,
    score: bool = True,
    arms_as_given: bool = False,
    **options: Any,
) -> ReconcileReport:
    """Simulator arm vs a live (or dry-run) bot: writes `<out>/live` and `<out>/sim` (canonical logs), `<out>/score`
    (`queuetruth score`, sim = the simulator arm) and `<out>/reconcile.json` / `reconcile_orders.csv`; returns the
    `ReconcileReport`.

    `sim` is a backtest result (zip, JSON, `.last_result.json`) or a dry-run bot (trades database + bot logs);
    `live` is the bot run to check (database + logs). Without `instrument_map` the instruments get ids 1..n with
    default scales (`auto_instrument_map`, warned). `window_ns` is the scorer's evaluation horizon (default 1 day:
    Freqtrade orders rest for minutes; the live cancel still ends each window). `time_tol_ns` widens the timing
    check: default 0 against a backtest (whose fills are already a candle wide), `BOT_TIME_TOL_NS` (5 s) between two
    bots. A live-bot simulator arm with a dry-run live arm fails `QT-CONFIG-001` (swapped arguments) unless
    `arms_as_given`. Options as in the module docstring (`strategy`, `timeframe`, `pair_tolerance_candles`, `tif`;
    `dry_run`, `order_date_clock`, `venue` describe the live arm, `log_tz` every bot log; `price_exp`,
    `size_exp`)."""
    check_options(options, FT_OPTIONS - {"pair_with", "map_venue", "live_dry_run", "live_venue"}, "freqtrade reconcile")
    outp = Path(out)
    sim_paths, live_paths = _paths(sim), _paths(live)
    for p in [*sim_paths, *live_paths]:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
    # the live arm's options (dry_run, venue, order_date_clock) do not describe a bot simulator arm
    sopts = {k: options[k] for k in ("strategy", "timeframe", "log_tz") if k in options}
    bt = read_run(sim_paths, sopts)
    lv = read_bot_run(live_paths, options)
    warnings: list[str] = []
    if bt.kind == "live" and lv.kind != "live":
        if not arms_as_given:
            raise QtError(
                "QT-CONFIG-001",
                f"the simulator arm {sim_paths} is a live bot and the live arm {live_paths} a {lv.kind} bot: arms "
                "swapped? (reconcile_freqtrade(sim, live, out); pass arms_as_given=True to score them as given)",
            )
        warnings.append("arms_as_given: the simulator arm is a live bot, the live arm is not")
    elif lv.kind == "dry_run":
        warnings.append(
            "the live arm is a dry-run bot (Freqtrade's own simulator): this compares two fill models; reconcile "
            "against a live bot for exchange truth"
        )
    venue = lv.venue or bt.venue or "freqtrade"
    if instrument_map is not None:
        imap = load_instrument_map(str(instrument_map))
    else:
        insts = {(venue, t.pair) for t in lv.trades} | {(venue, t.pair) for t in bt.trades}
        imap = auto_instrument_map(
            sorted(insts), price_exp=opt_int(options, "price_exp", -9), size_exp=opt_int(options, "size_exp", -8)
        )
        warnings.append(
            "instrument_map: none given; instruments numbered 1..n in sorted order with default scales "
            "(price_exp -9, size_exp -8) so both logs share ids"
        )
    tf = timeframe_seconds(options["timeframe"]) if options.get("timeframe") else None
    pairing = pair_runs(lv, bt, timeframe_s=tf, tolerance=opt_int(options, "pair_tolerance_candles", 1))
    typed = adopt_live_types(pairing)
    if typed:
        warnings.append(f"sim: {typed} backtest order(s) take the order type of their live twin")
    backtest = bt.kind == "backtest"
    tol = (0 if backtest else BOT_TIME_TOL_NS) if time_tol_ns is None else int(time_tol_ns)
    lb = _builder(venue, account, imap, options, sim=False, map_venue=venue)
    emit_orders(lb, lv, lv.orders(), account=account, opts=options, imap=imap, map_venue=venue, sim=False)
    lres = _finish(lb, lv)
    sb = _builder(FT_SIM, account, imap, options, sim=True, map_venue=venue)
    emit_orders(
        sb,
        bt,
        bt.orders(),
        account=account,
        opts={k: options[k] for k in ("tif", "price_exp", "size_exp") if k in options},
        imap=imap,
        map_venue=venue,
        sim=True,
        sim_live=pairing.sim_to_live(),
    )
    sres = _finish(sb, bt)
    live_status = "simulated" if lv.kind == "dry_run" else "fixture_only"
    lsum = finish_to_dir(lres.venue, lres, lv.paths, outp / "live", status=live_status)
    ssum = finish_to_dir(FT_SIM, sres, bt.paths, outp / "sim", status="simulated")
    warnings += [f"live: {w}" for w in lsum.warnings] + [f"sim: {w}" for w in ssum.warnings]
    sc, err, classes = (None, "not requested", {})
    if score:
        sc, err, classes = run_score(outp / "live", outp / "sim", outp / "score", window_ns=window_ns)
    tf_ns = pairing.timeframe_s * _NS
    views: list[tuple[OrderView | None, OrderView | None]] = [
        (
            None if lo is None else _view(lo, tf_ns=tf_ns, backtest=False),
            None if so is None else _view(so, tf_ns=tf_ns, backtest=backtest),
        )
        for lo, so in pairing.pairs
    ]
    sim_name = "backtest" if backtest else "dry-run"
    live_name = "dry-run" if lv.kind == "dry_run" else "live"
    if backtest:
        timed = ", ".join(f"{k} {v}" for k, v in bt.stats.items() if k.startswith(("timedout", "canceled", "replaced")))
        no_sim_hint = (
            "the backtest has no order of this role here (no signal on this candle, or a backtest order that timed "
            f"out or was replaced: the export keeps filled and open orders only; this run: {timed or 'no counts'})"
        )
    else:
        no_sim_hint = f"the {sim_name} bot placed no order of this role on this candle"
    no_live_hint = (
        f"the {live_name} bot placed no order of this role on this candle (no signal, pairlist / max_open_trades / "
        "wallet state, or the bot was not running)"
    )
    rows = explain_pairs(
        views,
        price_tol_bps=price_tol_bps,
        time_tol_ns=tol,
        sim_name=sim_name,
        live_name=live_name,
        no_sim_hint=no_sim_hint,
        no_live_hint=no_live_hint,
        score_classes=classes,
    )
    counts, adv, missed = summarize(rows)
    spec = reconcile_spec(
        "freqtrade",
        sim_paths,
        live_paths,
        instrument_map=None if instrument_map is None else str(instrument_map),
        options=options,
        account=account,
        window_ns=int(window_ns),
        price_tol_bps=price_tol_bps,
        time_tol_ns=tol,
        score=bool(score),
        arms_as_given=bool(arms_as_given),
    )
    n_paired = sum(1 for lo, so in pairing.pairs if lo is not None and so is not None)
    n_live = len(lv.orders())
    report = ReconcileReport(
        kind="freqtrade",
        run_id=reconcile_run_id(spec),
        live_log="live",
        sim_log="sim",
        n_live_orders=n_live,
        n_sim_orders=len(bt.orders()),
        n_paired=n_paired,
        pairing_coverage=None if n_live == 0 else n_paired / n_live,
        counts=counts,
        sim_price_advantage_quote=adv,
        missed_fill_notional=missed,
        score=sc,
        score_error=err,
        options=spec,
        warnings=warnings,
        orders=rows,
    )
    write_reconcile(outp, report)
    return report


# ---- candle-fill audit -------------------------------------------------------------------------------------------


#: cluster block of the audit's bootstrap intervals: orders of one instrument placed in the same 30 minutes share
#: market conditions (the scorer's cluster unit too).
AUDIT_CLUSTER_NS = 1800 * _NS
AUDIT_BOOTSTRAP_DRAWS = 2000


class CandleAuditCounts(QtModel):
    """Candle-rule vs exact outcomes of one group of orders."""

    n: int
    candle_filled: int
    exact_filled: int
    exact_full: int
    both: int
    candle_only: int
    exact_only: int
    neither: int
    #: candle-rule fills from Freqtrade's placement clamp: a buy priced above its placing candle's high (a sell below
    #: its low) is placed at that high (low) and fills on that candle (`Backtesting.get_valid_price_and_stake` /
    #: custom exit prices).
    marketable_at_placement: int
    #: candle-rule fills whose limit was reached by prints inside the order's life [ts_live, end).
    window_reached: int
    #: candle_only orders whose limit prints inside the order's life reached (queue: the market touched or traded
    #: through the price, FIFO did not reach the order).
    candle_only_queue: int
    #: candle_only orders reached only by prints outside the order's life: after it ended, inside its last candle,
    #: or before it was placed, inside its first candle (`clock` anchor or a placement lag).
    candle_only_outside_life: int
    #: exact_only orders by the replay's first-fill reason: `crossed` (the opposite quote reached the limit while no
    #: print in any of the order's candles did), `trade_through` / `queue_depleted` (prints passed the limit, but in
    #: no single candle whose range contains it: a gap between candles).
    exact_only_by_reason: dict[str, int]
    #: candle_only / candle_filled: share of the candle rule's fills that exact FIFO does not fill.
    overfill_share: float | None
    #: Wilson 95% interval of overfill_share treating every order as independent (too narrow for orders that share
    #: an instrument and a time window).
    overfill_ci95: tuple[float, float] | None
    #: percentile 95% interval of overfill_share from a bootstrap over clusters (instrument x 30-minute block of
    #: ts_live; `AUDIT_BOOTSTRAP_DRAWS` draws, seed 0).
    overfill_ci95_cluster: tuple[float, float] | None
    n_clusters: int
    fill_rate_candle: float | None
    fill_rate_exact: float | None


class CandleAudit(QtModel):
    """`candle_fill_audit` result: Freqtrade's candle-fill rule replayed on the orders of an exact replay."""

    replay_dir: str
    timeframe_s: int
    timeout_s: int
    anchor: Literal["submit", "clock"]
    #: `submit` anchor: the first candle starts this long before the order went live (placement lag in the candle).
    lag_ns: int
    n_input: int
    n_excluded: dict[str, int]
    all: CandleAuditCounts
    #: candle-rule fills where the price only touched the limit (buy: window low == limit; sell: high == limit).
    touch: CandleAuditCounts
    #: candle-rule fills where the price traded through the limit (including the placement clamp).
    through: CandleAuditCounts
    by_side: dict[str, CandleAuditCounts]
    #: filled quantity in the window: candle rule (always the full order) vs exact (from shadow_fills.parquet).
    qty_candle: int | None
    qty_exact: int | None


def _wilson(k: int, n: int) -> tuple[float, float] | None:
    if n == 0:
        return None
    z = 1.959963984540054
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _cluster_ci(num: np.ndarray, den: np.ndarray, cl: np.ndarray) -> tuple[tuple[float, float] | None, int]:
    """Percentile 95% interval of sum(num) / sum(den) resampling whole clusters (seed 0); (None, k) when the
    denominator is 0 or there is one cluster."""
    if num.size == 0:
        return None, 0
    ids, inv = np.unique(cl, return_inverse=True)
    k = int(ids.size)
    if den.sum() == 0 or k < 2:
        return None, k
    n_k = np.bincount(inv, weights=num.astype(float), minlength=k)
    d_k = np.bincount(inv, weights=den.astype(float), minlength=k)
    rng = np.random.default_rng(0)
    draws = rng.integers(0, k, size=(AUDIT_BOOTSTRAP_DRAWS, k))
    nn, dd = n_k[draws].sum(axis=1), d_k[draws].sum(axis=1)
    ratio = np.divide(nn, dd, out=np.full(nn.shape, np.nan), where=dd > 0)
    ratio = ratio[np.isfinite(ratio)]
    if ratio.size == 0:
        return None, k
    lo, hi = np.percentile(ratio, [2.5, 97.5])
    return (float(lo), float(hi)), k


def _counts(
    c: np.ndarray,
    e: np.ndarray,
    full: np.ndarray,
    w: np.ndarray,
    mk: np.ndarray,
    reason: np.ndarray,
    cl: np.ndarray,
) -> CandleAuditCounts:
    n = int(c.size)
    cf, ef = int(c.sum()), int(e.sum())
    both = int((c & e).sum())
    co = int((c & ~e).sum())
    eo = ~c & e
    by_reason: dict[str, int] = {}
    for r in reason[eo]:
        by_reason[str(r)] = by_reason.get(str(r), 0) + 1
    ci_cl, _ = _cluster_ci((c & ~e)[c], np.ones(cf, dtype=np.int64), cl[c])
    return CandleAuditCounts(
        marketable_at_placement=int(mk.sum()),
        window_reached=int((c & w).sum()),
        candle_only_queue=int((c & ~e & w).sum()),
        candle_only_outside_life=int((c & ~e & ~w).sum()),
        exact_only_by_reason=dict(sorted(by_reason.items())),
        n=n,
        candle_filled=cf,
        exact_filled=ef,
        exact_full=int(full.sum()),
        both=both,
        candle_only=co,
        exact_only=int(eo.sum()),
        neither=int((~c & ~e).sum()),
        overfill_share=None if cf == 0 else co / cf,
        overfill_ci95=_wilson(co, cf),
        overfill_ci95_cluster=ci_cl,
        n_clusters=int(np.unique(cl).size),
        fill_rate_candle=None if n == 0 else cf / n,
        fill_rate_exact=None if n == 0 else ef / n,
    )


def candle_fill_audit(
    replay_dir: str | Path,
    *,
    timeframe: Any = "5m",
    timeout: int = 600,
    anchor: Literal["submit", "clock"] = "submit",
    lag_ns: int = 0,
) -> CandleAudit:
    """Freqtrade's backtest fill rule (`low <= price <= high` of a candle while the order is open, full fill at the
    limit) applied to the shadow orders of a `queuetruth replay --shadows` output directory
    (`shadow_results.parquet`, `trades.parquet`, optionally `shadow_fills.parquet`), against the exact FIFO outcome
    of the same orders.

    Each order is open over [ts_live, end), end = min(ts_cancel_effective, ts_live + timeout s). The candle rule
    checks every candle the order is open at. With `anchor = "submit"` the candles start `lag_ns` before ts_live
    (0: Freqtrade places at the candle's open and the whole first candle is after placement, the rule's best case;
    a bot that places `lag` seconds into the candle gets `lag_ns`); with `"clock"` they are the timeframe's clock
    grid (the order lands uniformly inside its first candle when its time is unrelated to the grid, as sampled
    shadows are: a mean lag of half a candle). Candle ranges come from the replay's trade prints; candles without
    prints have no range. Freqtrade's placement clamp applies on the first candle: a buy priced above its high (a
    sell below its low) is placed at that high (low) and fills (`marketable_at_placement`). Each later candle is
    checked on its own, so a price that gaps through the limit between two candles is no candle fill (`exact_only`
    counts those orders when FIFO filled them, by first-fill reason). `touch` / `through` split the candle-rule
    fills by the first filling candle: its low (buy) or high (sell) equals the limit, or passes it. Exact: a fill
    before `end`. A candle-rule fill that exact FIFO does not give is split into `candle_only_queue` (prints inside
    [ts_live, end) reached the limit: the queue ahead was not consumed) and `candle_only_outside_life` (only prints
    outside [ts_live, end) inside the order's candles did). Orders flagged `ambiguous_race` or `out_of_session`,
    rejected at entry (post-only shadows that would cross) and orders never live before `end` are excluded
    (counted)."""
    rd = Path(replay_dir)
    tf_ns = timeframe_seconds(timeframe) * _NS
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise QtError("QT-CONFIG-001", f"timeout must be positive integer seconds, got {timeout!r}")
    if anchor not in ("submit", "clock"):
        raise QtError("QT-CONFIG-001", f"anchor must be submit or clock, got {anchor!r}")
    if isinstance(lag_ns, bool) or not isinstance(lag_ns, int) or not 0 <= lag_ns < tf_ns:
        raise QtError("QT-CONFIG-001", f"lag_ns must be an integer in [0, timeframe), got {lag_ns!r}")
    if lag_ns and anchor != "submit":
        raise QtError("QT-CONFIG-001", "lag_ns applies to anchor='submit' (the clock grid has no placement lag)")
    for f in ("shadow_results.parquet", "trades.parquet"):
        if not (rd / f).exists():
            raise QtError("QT-INPUT-001", f"{rd / f}: not found (run queuetruth replay --shadows)")
    res = pq.read_table(rd / "shadow_results.parquet")
    res = res.cast(
        pa.schema([pa.field(f.name, pa.int64()) if pa.types.is_timestamp(f.type) else f for f in res.schema])
    )
    trd = pq.read_table(rd / "trades.parquet", columns=["ts_event", "instrument_id", "price"])

    def ints(t: Any, col: str) -> np.ndarray:
        c = t.column(col)
        if str(c.type).startswith("timestamp"):
            c = c.cast("int64")
        return np.asarray(c.to_numpy(zero_copy_only=False))

    t_iid, t_ts, t_px = ints(trd, "instrument_id"), ints(trd, "ts_event"), ints(trd, "price")
    order = np.lexsort((t_ts, t_iid))
    t_iid, t_ts, t_px = t_iid[order], t_ts[order], t_px[order]
    bounds: dict[int, tuple[int, int]] = {}
    if t_iid.size:
        cuts = np.flatnonzero(np.diff(t_iid)) + 1
        starts = np.concatenate(([0], cuts))
        ends = np.concatenate((cuts, [t_iid.size]))
        for s, e in zip(starts, ends, strict=True):
            bounds[int(t_iid[s])] = (int(s), int(e))
    rows = res.to_pylist()
    fills_qty: dict[str, list[tuple[int, int]]] = {}
    fpath = rd / "shadow_fills.parquet"
    if fpath.exists():
        ft = pq.read_table(fpath, columns=["shadow_id", "ts_event", "fill_qty"])
        for sid, ts, q in zip(
            ft.column("shadow_id").to_pylist(), ints(ft, "ts_event"), ft.column("fill_qty").to_pylist(), strict=True
        ):
            fills_qty.setdefault(str(sid), []).append((int(ts), int(q)))

    def ns(v: Any) -> int | None:
        return None if v is None else int(v)

    excluded = {"ambiguous_race": 0, "out_of_session": 0, "rejected": 0, "not_live": 0, "no_price": 0}
    cand: list[bool] = []
    reached: list[bool] = []
    exact: list[bool] = []
    full: list[bool] = []
    touch: list[bool] = []
    marketable: list[bool] = []
    reasons: list[str] = []
    clusters: list[str] = []
    sides: list[str] = []
    q_candle = 0
    q_exact = 0
    for r in rows:
        if r.get("ambiguous_race"):
            excluded["ambiguous_race"] += 1
            continue
        if r.get("out_of_session"):
            excluded["out_of_session"] += 1
            continue
        if str(r.get("end_state") or "").startswith("rejected"):
            excluded["rejected"] += 1
            continue
        if r.get("price") is None:
            excluded["no_price"] += 1
            continue
        lo = ns(r["ts_live"])
        assert lo is not None
        end = lo + timeout * _NS
        ce = ns(r.get("ts_cancel_effective"))
        if ce is not None:
            end = min(end, ce)
        if end <= lo:
            excluded["not_live"] += 1
            continue
        a = lo - lag_ns if anchor == "submit" else lo - lo % tf_ns
        z = a + (-(-(end - a) // tf_ns)) * tf_ns
        s, e = bounds.get(int(r["instrument_id"]), (0, 0))
        edges = s + np.searchsorted(t_ts[s:e], np.arange(a, z + 1, tf_ns, dtype=np.int64), side="left")
        px = int(r["price"])
        buy = int(r["side"]) == 66
        c = tch = mk = False
        # Freqtrade checks each candle on its own: low <= price <= high (a gap through the price between candles
        # is no fill); candles without prints have no range. On the placing candle the price is first clamped to
        # its high (buy) / low (sell), which fills a marketable limit there.
        for k, (i0, i1) in enumerate(itertools.pairwise(edges)):
            if i1 > i0:
                lo_px, hi_px = int(t_px[i0:i1].min()), int(t_px[i0:i1].max())
                if k == 0 and (px > hi_px if buy else px < lo_px):
                    c = mk = True
                    break
                if lo_px <= px <= hi_px:
                    c = True
                    tch = lo_px == px if buy else hi_px == px
                    break
        w0 = s + int(np.searchsorted(t_ts[s:e], lo, side="left"))
        w1 = s + int(np.searchsorted(t_ts[s:e], end, side="left"))
        wr = w1 > w0 and (int(t_px[w0:w1].min()) <= px if buy else int(t_px[w0:w1].max()) >= px)
        reached.append(bool(wr))
        ff = ns(r.get("ts_first_fill"))
        fu = ns(r.get("ts_full_fill"))
        cand.append(c)
        exact.append(ff is not None and ff < end)
        full.append(fu is not None and fu < end)
        touch.append(tch)
        marketable.append(mk)
        reasons.append(str(r.get("first_fill_reason") or "unknown"))
        clusters.append(f"{int(r['instrument_id'])}:{lo // AUDIT_CLUSTER_NS}")
        sides.append("buy" if buy else "sell")
        if c:
            q_candle += int(r["qty"])
        if fills_qty:
            q_exact += sum(q for ts, q in fills_qty.get(str(r["shadow_id"]), []) if ts < end)
    arrs = (
        np.array(cand, bool),
        np.array(exact, bool),
        np.array(full, bool),
        np.array(reached, bool),
        np.array(marketable, bool),
        np.array(reasons, dtype=object),
        np.array(clusters, dtype=object),
    )
    c_a = arrs[0]
    t_a = np.array(touch, bool)
    s_a = np.array(sides)
    thr = c_a & ~t_a
    # touch / through split the candle rule's fills; the orders the candle rule does not fill belong to neither
    return CandleAudit(
        replay_dir=str(rd),
        timeframe_s=tf_ns // _NS,
        timeout_s=int(timeout),
        anchor=anchor,
        lag_ns=int(lag_ns),
        n_input=len(rows),
        n_excluded=excluded,
        all=_counts(*arrs),
        touch=_counts(*(x[t_a] for x in arrs)),
        through=_counts(*(x[thr] for x in arrs)),
        by_side={sd: _counts(*(x[s_a == sd] for x in arrs)) for sd in ("buy", "sell")},
        qty_candle=q_candle,
        qty_exact=q_exact if fills_qty else None,
    )


__all__ = [
    "CATEGORIES",
    "FREQTRADE_COMMIT",
    "FT_SIM",
    "CandleAudit",
    "CandleAuditCounts",
    "FtOrder",
    "FtRun",
    "FtTrade",
    "OrderView",
    "ReconcileOrder",
    "ReconcileReport",
    "ReconcileScore",
    "RunPairing",
    "auto_instrument_map",
    "candle_fill_audit",
    "explain_pair",
    "explain_pairs",
    "import_freqtrade",
    "import_freqtrade_live",
    "import_freqtrade_sim",
    "pair_runs",
    "read_backtest",
    "read_bot_log",
    "read_bot_run",
    "read_trades_db",
    "reconcile_freqtrade",
    "render_reconcile",
    "summarize",
    "timeframe_seconds",
    "write_reconcile",
]
