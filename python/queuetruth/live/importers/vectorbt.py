"""VectorBT importer: a `Portfolio`'s order (or trade) records -> the simulator arm (`sim:vectorbt`) of the scorer
and of `reconcile_vectorbt` (shared code in `queuetruth.live.importers.backtest`).

Formats (read from VectorBT's public source, release 0.28.1; VectorBT itself is never imported):

- Order records, `pf.orders.records_readable` (`Records.records_readable` with `orders_field_config`) written with
  `.to_csv(path)` or `.to_json(path, orient="records")`: columns `Order Id`, `Column` (the column label: a symbol,
  or a parameter combination of a sweep), `Timestamp` (the index label of the bar), `Size` (positive), `Price`,
  `Fees` (in cash, the quote currency) and `Side` (`Buy` / `Sell`; `0` / `1` from the raw records). A CSV written
  with its index has a first unnamed column, ignored; other unknown columns are named in a warning.
- Trade records, `pf.trades.records_readable` (exit trades; also `pf.entry_trades`, `pf.positions`): `Exit Trade
  Id` (or `Entry Trade Id`, `Trade Id`, `Position Id` as the id column), `Column`, `Size`, `Entry Timestamp`, `Avg
  Entry Price`, `Entry Fees`, `Exit Timestamp`, `Avg Exit Price`, `Exit Fees`, `PnL`, `Return`, `Direction`
  (`Long` / `Short`), `Status` (`Open` / `Closed`). Next to order records they are counted and their P&L summed in
  a warning. Alone they give one entry order per trade and one exit order per closed trade, at the average prices
  (warned: an order that closed one trade and opened the next appears twice, and an open trade's exit is not an
  order).

Every VectorBT order record is an execution: the simulator fills an order in full at the price it was given
(`from_signals` / `from_orders`, default the bar's close; `slippage` moves it), with no queue and no order that
rests unfilled. Each record becomes one order with an `ack` and one fill of its size at its price at the bar's
timestamp, liquidity `unknown` (the records do not say); option `order_type` is `market` (default) or `limit` (the
record's price is the limit price: the live strategy rests limit orders at the prices the backtest fills at).
Timestamps: ISO 8601 text (naive times in option `data_tz`, default UTC) or epoch milliseconds (pandas' JSON
default; values before 1973 are bar numbers of a portfolio without a DatetimeIndex and fail). A sweep's columns
are separate backtests: with more than one column, option `column` keeps one, or `instrument` / `instruments`
(`LABEL:NAME,...`) names the live instrument of each kept column.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live._common import Record, dump_raw, epoch_ns, input_error, read_csv_rows, read_json_records, read_text
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
    reconcile_backtest,
    rename,
    time_of,
)
from queuetruth.live.importers.freqtrade import ReconcileReport, check_options, dtext, finish_to_dir, num

VBT_SIM = "sim:vectorbt"
#: VectorBT release the format notes were read from.
VECTORBT_VERSION = "0.28.1"
VBT_OPTIONS = COMMON_OPTIONS | {"column", "order_type"}
_NS = 1_000_000_000
_ORDER_COLS = ("Order Id", "Column", "Timestamp", "Size", "Price", "Fees", "Side")
_TRADE_IDS = ("Exit Trade Id", "Entry Trade Id", "Trade Id", "Position Id")
_TRADE_COLS = ("Column", "Size", "Entry Timestamp", "Avg Entry Price", "Entry Fees", "Exit Timestamp")
_TRADE_COLS += ("Avg Exit Price", "Exit Fees", "PnL", "Direction", "Status")
_RAW_COLS = ("id", "col", "idx", "size", "price", "fees", "side")
#: pandas writes epoch milliseconds; anything below this (1973-03-03) is a bar number, not a time.
_MIN_EPOCH_MS = 100_000_000_000


def _ts(v: Any, what: str, loc: str, tz: str) -> tuple[int, str]:
    if isinstance(v, int) and not isinstance(v, bool):
        if 0 <= v < _MIN_EPOCH_MS:
            raise input_error(
                loc,
                f"{what}: {v} is a bar number, not a time (the portfolio has no DatetimeIndex: build it on a "
                "time-indexed price series, or set the index before exporting)",
            )
        return epoch_ns(v, "ms", what, loc)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return _ts(int(v.strip()), what, loc, tz)
    return time_of(v, what, loc, tz)


def _side(v: Any, loc: str) -> str:
    s = str(v).strip().lower()
    side = {"buy": "buy", "sell": "sell", "0": "buy", "1": "sell"}.get(s)
    if side is None:
        raise input_error(loc, f"Side {v!r} is not Buy / Sell")
    return side


def _table(path: Path, file_idx: int) -> tuple[list[str], list[tuple[dict[str, Any], Record]]]:
    """(columns, rows with their records) of a CSV or a JSON `orient="records"` export."""
    text = read_text(path)
    head = text.lstrip()[:1]
    if head in ("[", "{"):
        recs = read_json_records(path, file_idx)
        rows: list[tuple[dict[str, Any], Record]] = []
        cols: list[str] = []
        for r in recs:
            if not isinstance(r.obj, dict):
                raise input_error(r.location, 'expected one object per record (to_json(path, orient="records"))')
            if not rows and any(isinstance(v, dict) for v in r.obj.values()):
                raise input_error(
                    r.location, 'a column-oriented JSON export: write it with to_json(path, orient="records")'
                )
            for k in r.obj:
                if k not in cols:
                    cols.append(k)
            rows.append((r.obj, r))
        return cols, rows
    header, raw = read_csv_rows(text, str(path))
    out = []
    for i, r in enumerate(raw):
        row = {header[j]: (r[j] if j < len(r) else "") for j in range(len(header)) if header[j]}
        out.append((row, Record(row, dump_raw(row), f"{path}:{i + 2}", file_idx, i)))
    return [h for h in header if h], out


def _kind(cols: list[str], path: Path) -> str:
    if all(c in cols for c in _ORDER_COLS):
        return "orders"
    if any(c in cols for c in _TRADE_IDS) and all(c in cols for c in _TRADE_COLS):
        return "trades"
    if all(c in cols for c in _RAW_COLS):
        raise input_error(
            str(path),
            "raw order records (pf.orders.records: id, col, idx, ...) have bar numbers and column numbers only; "
            "export pf.orders.records_readable instead",
        )
    raise input_error(
        str(path),
        f"not VectorBT records_readable output: need the order columns {list(_ORDER_COLS)} or the trade columns "
        f"{list(_TRADE_COLS)} with one of {list(_TRADE_IDS)}; got {cols[:12]}",
    )


def _req(row: dict[str, Any], col: str, loc: str) -> Decimal:
    v = num(row.get(col), col, loc)
    if v is None:
        raise input_error(loc, f"{col}: missing value")
    return v


def _order_type(opts: dict[str, Any]) -> str:
    t = str(opts.get("order_type") or "market").strip().lower()
    if t not in ("market", "limit"):
        raise QtError("QT-CONFIG-001", f"order_type must be market or limit, got {opts.get('order_type')!r}")
    return t


def _new_order(
    ref: str, label: str, side: str, qty: Decimal, px: Decimal, ts: int, prec: str, rec: Record, otype: str
) -> SimOrder:
    o = SimOrder(
        ref=ref,
        instrument=label,
        side=side,
        order_type=otype,
        tif="gtc" if otype == "limit" else "other",
        qty=qty,
        limit_px=px if otype == "limit" else None,
        placed_ns=ts,
        placed_precision=prec,
        rec=rec,
        note="vectorbt order record (filled in full at its price, no queue)",
        liquidity="unknown",
    )
    return o


def read_vectorbt(paths: list[str], opts: dict[str, Any]) -> SimRun:
    """`records_readable` order (or trade) exports -> `SimRun` (rules in the module docstring)."""
    tz = data_tz(opts)
    otype = _order_type(opts)
    run = SimRun("vectorbt", [], [str(p) for p in paths])
    orders: list[tuple[str, SimOrder]] = []
    trades: list[tuple[str, dict[str, Any], Record]] = []
    ignored: set[str] = set()
    seen: set[str] = set()
    for i, p in enumerate(paths):
        pp = Path(p)
        cols, rows = _table(pp, i)
        if not rows:
            raise input_error(str(pp), "no records")
        kind = _kind(cols, pp)
        known = set(_ORDER_COLS) if kind == "orders" else {*_TRADE_IDS, *_TRADE_COLS, "Return", "Parent Id"}
        ignored |= {c for c in cols if c not in known}
        for row, rec in rows:
            loc = rec.location
            label = str(row.get("Column") if row.get("Column") is not None else "")
            if kind == "trades":
                trades.append((label, row, rec))
                continue
            oid = str(row.get("Order Id") if row.get("Order Id") is not None else "").strip()
            if not oid:
                raise input_error(loc, "Order Id: missing value")
            ref = f"{label}-{oid}"
            if ref in seen:
                run.skip("duplicate order record (same Column and Order Id)")
                continue
            seen.add(ref)
            qty, px = _req(row, "Size", loc), _req(row, "Price", loc)
            if qty <= 0 or px <= 0:
                raise input_error(loc, f"Size {dtext(qty)} and Price {dtext(px)} must be positive")
            fee = num(row.get("Fees"), "Fees", loc)
            ts, prec = _ts(row.get("Timestamp"), "Timestamp", loc, tz)
            o = _new_order(ref, label, _side(row.get("Side"), loc), qty, px, ts, prec, rec, otype)
            o.fills.append(SimFill(ts, prec, qty, px, fee, f"{ref}:fill", rec))
            orders.append((label, o))
    if ignored:
        run.warn(f"columns ignored: {sorted(ignored)}")
    if orders and trades:
        closed = [t for t in trades if str(t[1].get("Status")).strip().lower() == "closed"]
        pnl: dict[str, Decimal] = {}
        for label, row, rec in closed:
            pnl[label] = pnl.get(label, Decimal(0)) + (num(row.get("PnL"), "PnL", rec.location) or Decimal(0))
        sums = ", ".join(f"{k} {dtext(v)}" for k, v in sorted(pnl.items()))
        run.warn(
            f"trade records: {len(trades)} ({len(closed)} closed; PnL {sums or 'n/a'}): the order records carry the "
            "fills, so the trades add no event"
        )
    elif trades:
        orders = _orders_from_trades(trades, run, tz, otype)
    labels = sorted({label for label, _ in orders})
    one, many = instrument_names(opts)
    col = opts.get("column")
    if col not in (None, ""):
        keep = str(col)
        if keep not in labels:
            raise QtError("QT-CONFIG-001", f"column {keep!r}: not in the export's columns {labels}")
        orders = [(lb, o) for lb, o in orders if lb == keep]
    elif many:
        missing = sorted(set(many) - set(labels))
        if missing:
            raise QtError("QT-CONFIG-001", f"instruments: columns {missing} are not in the export's columns {labels}")
        left = sorted(set(labels) - set(many))
        if left:
            run.warn(f"columns {left} not named in option instruments left out")
        orders = [(lb, o) for lb, o in orders if lb in many]
    elif len(labels) > 1 and one is None:
        raise QtError(
            "QT-CONFIG-001",
            f"the export holds {len(labels)} columns {labels[:10]}: each column of a VectorBT portfolio is its own "
            "backtest (a symbol or a parameter combination); pass option column=<label> to keep one, or "
            "instruments=LABEL:NAME,... to name the live instrument of each",
        )
    for label, o in orders:
        o.instrument = rename(label, one, many)
    run.orders = [o for _, o in orders]
    apply_timeframe(run, opts)
    run.orders.sort(key=lambda o: (o.ref_ns, o.ref))
    return run


def _orders_from_trades(
    trades: list[tuple[str, dict[str, Any], Record]], run: SimRun, tz: str, otype: str
) -> list[tuple[str, SimOrder]]:
    out: list[tuple[str, SimOrder]] = []
    n_open = 0
    for label, row, rec in trades:
        loc = rec.location
        tid = next((str(row[c]).strip() for c in _TRADE_IDS if row.get(c) not in (None, "")), "")
        if not tid:
            raise input_error(loc, f"no trade id ({' / '.join(_TRADE_IDS)})")
        d = str(row.get("Direction")).strip().lower()
        if d not in ("long", "short", "0", "1"):
            raise input_error(loc, f"Direction {row.get('Direction')!r} is not Long / Short")
        entry_side = "buy" if d in ("long", "0") else "sell"
        st = str(row.get("Status")).strip().lower()
        if st not in ("open", "closed", "0", "1"):
            raise input_error(loc, f"Status {row.get('Status')!r} is not Open / Closed")
        qty = _req(row, "Size", loc)
        if qty <= 0:
            raise input_error(loc, "Size must be positive")
        legs = [("entry", entry_side, "Entry Timestamp", "Avg Entry Price", "Entry Fees")]
        if st in ("closed", "1"):
            legs.append(
                ("exit", "sell" if entry_side == "buy" else "buy", "Exit Timestamp", "Avg Exit Price", "Exit Fees")
            )
        else:
            n_open += 1
        for role, side, tcol, pcol, fcol in legs:
            px = _req(row, pcol, loc)
            if px <= 0:
                raise input_error(loc, f"{pcol} must be positive")
            ts, prec = _ts(row.get(tcol), tcol, loc, tz)
            ref = f"{label}-t{tid}-{role}"
            o = _new_order(ref, label, side, qty, px, ts, prec, rec, otype)
            o.note = f"vectorbt trade record {tid} {role} at the average price"
            o.fills.append(SimFill(ts, prec, qty, px, num(row.get(fcol), fcol, loc), f"{ref}:fill", rec))
            out.append((label, o))
    run.warn(
        f"orders derived from {len(trades)} trade record(s) (no order records given): one entry order per trade and "
        f"one exit order per closed trade ({n_open} open); an order that closed one trade and opened the next "
        "appears twice; export pf.orders.records_readable for one row per order"
    )
    return out


def import_vectorbt_sim(paths: list[str], opts: ImportOptions) -> ImportResult:
    """VectorBT `records_readable` exports -> canonical rows of venue `sim:vectorbt`; option `pair_with` (a
    canonical live-log directory) gives paired orders the live `order_key`."""
    check_options(opts.extra, VBT_OPTIONS, "vectorbt")
    run = read_vectorbt([str(p) for p in paths], opts.extra)
    return import_backtest_sim(run, opts)


def import_vectorbt(
    inputs: list[str] | str,
    out: str | Path,
    *,
    account: str | None = None,
    instrument_map: str | None = None,
    append: bool = False,
    **options: Any,
) -> Any:
    """VectorBT exports -> canonical simulator log directory `out` (the same import as
    `queuetruth.live.import_sim("vectorbt", ...)`); returns the `LiveImportSummary`."""
    from queuetruth.live.imap import load_instrument_map

    paths = [inputs] if isinstance(inputs, str) else [str(p) for p in inputs]
    for p in paths:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
    imap = load_instrument_map(instrument_map) if instrument_map else None
    res = import_vectorbt_sim(paths, ImportOptions(account=account, imap=imap, extra=dict(options)))
    return finish_to_dir(VBT_SIM, res, paths, Path(out), append=append, status="simulated")


def _paths(x: list[str | Path] | str | Path) -> list[str]:
    return [str(x)] if isinstance(x, (str, Path)) else [str(p) for p in x]


def reconcile_vectorbt(
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
    """A VectorBT backtest (`sim`: `records_readable` exports) against a canonical live log (`live`: a directory
    written by `queuetruth live import`); outputs as `reconcile_backtrader`. Options: `timeframe`,
    `fill_delay_bars`, `pair_window_ms`, `column`, `instrument`, `instruments`, `order_type`, `data_tz`, `venue`,
    `price_exp`, `size_exp`."""
    check_options(options, VBT_OPTIONS - {"pair_with", "map_venue"}, "vectorbt reconcile")
    sp = _paths(sim)
    for p in [*sp, *_paths(live)]:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
    run = read_vectorbt(sp, options)
    return reconcile_backtest(
        run,
        live,
        out,
        sim_paths=sp,
        account=account,
        instrument_map=instrument_map,
        window_ns=window_ns,
        price_tol_bps=price_tol_bps,
        time_tol_ns=time_tol_ns,
        score=score,
        arms_as_given=arms_as_given,
        options=options,
    )


__all__ = [
    "VBT_OPTIONS",
    "VBT_SIM",
    "VECTORBT_VERSION",
    "import_vectorbt",
    "import_vectorbt_sim",
    "read_vectorbt",
    "reconcile_vectorbt",
]
