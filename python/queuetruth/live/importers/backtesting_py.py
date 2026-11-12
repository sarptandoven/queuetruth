"""backtesting.py importer: the trades of a `Backtest.run()` result -> the simulator arm (`sim:backtesting_py`) of the
scorer and of `queuetruth check` (shared code in `queuetruth.live.importers.backtest`).

Format (read from backtesting.py's public source, release 0.6.6; backtesting.py itself is never imported): `stats =
bt.run()` is a pandas Series whose `_trades` member is a DataFrame of the closed trades, one row each, with columns
`Size` (signed: positive long, negative short), `EntryBar`, `ExitBar`, `EntryPrice`, `ExitPrice`, `SL`, `TP`, `PnL`,
`Commission` (entry and exit together, in cash), `ReturnPct`, `EntryTime`, `ExitTime`, `Duration`, `Tag`, and one
`Entry_<indicator>` / `Exit_<indicator>` column per indicator. The result has no order records: `stats._strategy`
holds only the orders still pending at the end. Export the trades with

    stats._trades.to_csv("trades.csv")                                         # or
    stats._trades.to_json("trades.json", orient="records", date_format="iso")

(both without pickle; pandas' default JSON dates, epoch milliseconds, are read too).

Each trade becomes an entry order (buy for a long trade, sell for a short one) of `abs(Size)` filled in full at
`EntryPrice` and `EntryTime`, and an exit order of the other side filled at `ExitPrice` and `ExitTime`. backtesting.py
fills an order in full at one price with no queue, so each order has an `ack` and one fill, liquidity `unknown`;
option `order_type` is `market` (default) or `limit` (the fill price is the limit price: `check` uses `limit`). An
order that closed one trade and opened the next appears in both trades (warned). `Commission` is split between the
two orders in proportion to their notional (exact for backtesting.py's relative commission, warned). Timestamps:
ISO 8601 text or epoch milliseconds, naive times in option `data_tz` (default UTC); bar numbers (a backtest on data
without a DatetimeIndex) fail. A run trades one instrument and the export does not name it: option `instrument`
names it (without it the orders are on `asset0`, which `check` maps to a dataset with one symbol).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live.builder import ImportResult
from queuetruth.live.importers._base import ImportOptions
from queuetruth.live.importers.backtest import (
    COMMON_OPTIONS,
    SimFill,
    SimOrder,
    SimRun,
    apply_timeframe,
    data_tz,
    import_backtest_sim,
    instrument_names,
)
from queuetruth.live.importers.freqtrade import check_options, dtext, finish_to_dir, num
from queuetruth.live.importers.vectorbt import _new_order, _order_type, _req, _table, _ts

SIM = "sim:backtesting_py"
#: backtesting.py release the format notes were read from.
BACKTESTING_PY_VERSION = "0.6.6"
OPTIONS = COMMON_OPTIONS | {"order_type"}
#: Columns every `_trades` export has; `detect` and the importer require them.
REQUIRED = ("Size", "EntryPrice", "ExitPrice", "EntryTime", "ExitTime")
#: Columns that mark a backtesting.py `_trades` table (no other backtester writes `EntryBar` and `ExitBar`).
MARKERS = ("EntryBar", "ExitBar", "EntryPrice", "ExitPrice")
_KNOWN = {*REQUIRED, *MARKERS, "SL", "TP", "PnL", "Commission", "ReturnPct", "Duration", "Tag"}
#: The instrument of an export that names none (`queuetruth.check.symbols` maps it to a one-symbol dataset).
PLACEHOLDER = "asset0"


def _missing(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "nat", "nan", "none"))


def _split_fee(total: Decimal | None, entry: Decimal, exit_: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """`Commission` split between entry and exit in proportion to their notional."""
    if total is None:
        return None, None
    if exit_ is None or entry + exit_ == 0:
        return total, None
    e = (total * entry / (entry + exit_)).quantize(Decimal("1e-12"))
    return e, total - e


def read_backtesting_py(paths: list[str], opts: dict[str, Any]) -> SimRun:
    """`_trades` exports -> `SimRun` (rules in the module docstring)."""
    tz = data_tz(opts)
    otype = _order_type(opts)
    one, many = instrument_names(opts)
    if many:
        raise QtError(
            "QT-CONFIG-001",
            "backtesting.py: a run trades one instrument; name it with option instrument=NAME, not instruments",
        )
    label = one or PLACEHOLDER
    run = SimRun("backtesting_py", [], [str(p) for p in paths])
    ignored: set[str] = set()
    n_trades = 0
    n_open = 0
    pnl = Decimal(0)
    split = False
    for i, p in enumerate(paths):
        pp = Path(p)
        cols, rows = _table(pp, i)
        if not rows:
            raise QtError("QT-INPUT-002", f"{pp}: no trades")
        lack = [c for c in REQUIRED if c not in cols]
        if lack:
            raise QtError(
                "QT-INPUT-002",
                f"{pp}: not a backtesting.py trades export (stats._trades): missing {lack}; got {cols[:12]}",
                'write it with stats._trades.to_csv("trades.csv") or .to_json("trades.json", orient="records")',
            )
        ignored |= {c for c in cols if c not in _KNOWN and not c.startswith(("Entry_", "Exit_"))}
        for j, (row, rec) in enumerate(rows):
            loc = rec.location
            tid = str(j)
            size = _req(row, "Size", loc)
            if size == 0:
                raise QtError("QT-INPUT-002", f"{loc}: Size is 0")
            qty = abs(size)
            entry_side, exit_side = ("buy", "sell") if size > 0 else ("sell", "buy")
            epx = _req(row, "EntryPrice", loc)
            ets, eprec = _ts(row.get("EntryTime"), "EntryTime", loc, tz)
            closed = not _missing(row.get("ExitTime")) and not _missing(row.get("ExitPrice"))
            xpx = _req(row, "ExitPrice", loc) if closed else None
            if epx <= 0 or (xpx is not None and xpx <= 0):
                raise QtError("QT-INPUT-002", f"{loc}: EntryPrice and ExitPrice must be positive")
            fee = num(row.get("Commission"), "Commission", loc)
            efee, xfee = _split_fee(fee, qty * epx, None if xpx is None else qty * xpx)
            split = split or (fee is not None and xfee is not None and fee != 0)
            pnl += num(row.get("PnL"), "PnL", loc) or Decimal(0)
            n_trades += 1
            legs: list[tuple[str, str, Decimal, int, str, Decimal | None]] = [
                ("entry", entry_side, epx, ets, eprec, efee)
            ]
            if xpx is not None:
                xts, xprec = _ts(row.get("ExitTime"), "ExitTime", loc, tz)
                legs.append(("exit", exit_side, xpx, xts, xprec, xfee))
            else:
                n_open += 1
            for role, side, px, ts, prec, f in legs:
                ref = f"{label}-f{i}-t{tid}-{role}"
                o: SimOrder = _new_order(ref, label, side, qty, px, ts, prec, rec, otype)
                o.note = f"backtesting.py trade {tid} {role} (filled in full at its price, no queue)"
                o.fills.append(SimFill(ts, prec, qty, px, f, f"{ref}:fill", rec))
                run.orders.append(o)
    if ignored:
        run.warn(f"columns ignored: {sorted(ignored)}")
    if one is None:
        run.warn(f"the export names no instrument: its orders are on {PLACEHOLDER}; pass option instrument=NAME")
    run.warn(
        f"orders derived from {n_trades} trade(s): one entry and one exit order per trade ({n_open} open), "
        f"PnL {dtext(pnl)} in total; an order that closed one trade and opened the next appears in both"
    )
    if split:
        run.warn(
            "Commission (entry and exit together) split between the two orders in proportion to their notional: "
            "exact for a relative commission, an approximation for a fixed one"
        )
    apply_timeframe(run, opts)
    run.orders.sort(key=lambda o: (o.ref_ns, o.ref))
    return run


def import_backtesting_py_sim(paths: list[str], opts: ImportOptions) -> ImportResult:
    """backtesting.py `_trades` exports -> canonical rows of venue `sim:backtesting_py`; option `pair_with` (a
    canonical live-log directory) gives paired orders the live `order_key`."""
    check_options(opts.extra, OPTIONS, "backtesting_py")
    run = read_backtesting_py([str(p) for p in paths], opts.extra)
    return import_backtest_sim(run, opts)


def import_backtesting_py(
    inputs: list[str] | str,
    out: str | Path,
    *,
    account: str | None = None,
    instrument_map: str | None = None,
    append: bool = False,
    **options: Any,
) -> Any:
    """backtesting.py exports -> canonical simulator log directory `out` (the same import as
    `queuetruth.live.import_sim("backtesting_py", ...)`); returns the `LiveImportSummary`."""
    from queuetruth.live.imap import load_instrument_map

    paths = [inputs] if isinstance(inputs, str) else [str(p) for p in inputs]
    for p in paths:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
    imap = load_instrument_map(instrument_map) if instrument_map else None
    res = import_backtesting_py_sim(paths, ImportOptions(account=account, imap=imap, extra=dict(options)))
    return finish_to_dir(SIM, res, paths, Path(out), append=append, status="simulated")


__all__ = [
    "BACKTESTING_PY_VERSION",
    "MARKERS",
    "OPTIONS",
    "SIM",
    "import_backtesting_py",
    "import_backtesting_py_sim",
    "read_backtesting_py",
]
