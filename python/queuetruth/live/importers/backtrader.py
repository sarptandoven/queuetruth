"""Backtrader importer: a backtest's order and trade notifications -> the simulator arm (`sim:backtrader`) of the
scorer and of `reconcile_backtrader` (shared code in `queuetruth.live.importers.backtest`).

Formats (read from Backtrader's public source, release 1.9.78.123; Backtrader itself is never imported):

- Notification lines (JSON lines, one object per line) written by the analyzer `QtOrderLog` of
  docs/how-to/reconcile-backtrader-vectorbt.md. Order lines (`"event": "order"`, one per `notify_order` call):
  `ref` (`Order.ref`), `data` (the data feed's name), `status` (`Order.Status` name: `Submitted`, `Accepted`,
  `Partial`, `Completed`, `Canceled`, `Expired`, `Margin`, `Rejected`; `Created` is counted), `ordtype` (`Buy` /
  `Sell`), `exectype` (`Order.ExecTypes`: `Market`, `Close`, `Limit`, `Stop`, `StopLimit`, `StopTrail`,
  `StopTrailLimit`, `Historical`), `size` (`order.created.size`, negative for a sell), `price` / `pricelimit`
  (`order.created`), `created` (`order.created.dt`), `dt` (the bar time of the notification), `valid` (null or
  the expiry), `exbits` (the execution bits new in this notification, `order.executed.iterpending()`: `dt`, `size`,
  `price`, `comm`) and `order_key` (`order.info["order_key"]`, optional). Trade lines (`"event": "trade"`, one per
  `notify_trade` call: `ref`, `data`, `status` `Created` / `Open` / `Closed`, `size`, `price`, `pnl`, `pnlcomm`)
  carry positions, not orders: they are counted and their closed P&L summed in a warning.
- The `Transactions` analyzer (`bt.analyzers.Transactions`) dumped as JSON: `{<bar datetime>: [[amount, price,
  sid, symbol, value], ...]}` (`headers=True` adds a `date` key, skipped). It nets every execution of one data
  feed in one bar, so each entry becomes one order known from fills only (`coverage = fills_only`; no unfilled
  orders, no limit prices).

Mapping: one order per `ref`. `submit_sent` at `created`, `ack` at the `dt` of the `Accepted` line (else at
`created`), one fill per execution bit (`<ref>:<k>`, fee = `comm`), `cancel_ack` / `expire` / `reject`
(`Margin`: not enough cash) at the line's `dt`. Order types: `Market` and `Close` -> market, `Limit` -> limit at
`price`, `Stop` and `StopTrail` -> stop, `StopLimit` -> stop_limit at `pricelimit`, `StopTrailLimit` ->
stop_limit without a limit price (it trails), `Historical` -> other; time in force `gtc`, `gtd` with a `valid`
time, `other` for market orders. Backtrader's datetimes are naive (UTC unless the data feed has a `tz`): option
`data_tz` names their zone. A `BackBroker` fill is the bar's price (open for market orders, the limit or the bar's
open through it for limit orders): no queue and no partial fills unless a filler is set. `order.info["order_key"]`
set to a live order key pairs that order with it directly.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live._common import Record, dump_raw, input_error, read_json_records
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
    key_hint,
    reconcile_backtest,
    rename,
    time_of,
)
from queuetruth.live.importers.freqtrade import ReconcileReport, check_options, db_int, dtext, finish_to_dir, num

BT_SIM = "sim:backtrader"
#: Backtrader release the format notes were read from.
BACKTRADER_VERSION = "1.9.78.123"
BT_OPTIONS = COMMON_OPTIONS
_NS = 1_000_000_000
_EXEC = {
    "market": "market",
    "close": "market",
    "limit": "limit",
    "stop": "stop",
    "stoptrail": "stop",
    "stoplimit": "stop_limit",
    "stoptraillimit": "stop_limit",
    "historical": "other",
}
_STATUS = ("created", "submitted", "accepted", "partial", "completed", "canceled", "cancelled", "expired")
_STATUS += ("margin", "rejected")
_END = {
    "canceled": ("cancel_ack", "Canceled"),
    "cancelled": ("cancel_ack", "Canceled"),
    "expired": ("expire", "Expired"),
    "margin": ("reject", "Margin: not enough cash"),
    "rejected": ("reject", "Rejected"),
}


def _name(v: Any) -> str:
    return str(v or "").strip().lower()


def _read_transactions(doc: dict[str, Any], rec: Record, run: SimRun, tz: str) -> None:
    """Entries of a `Transactions` analyzer dump -> orders known from fills only."""
    n = 0
    for k in sorted(doc):
        v = doc[k]
        if k == "date":
            continue  # headers=True: the column names
        loc = f"{rec.location}[{k!r}]"
        if not isinstance(v, list):
            raise input_error(loc, "a Transactions entry must be a list of [amount, price, sid, symbol, value]")
        ts, prec = time_of(k, "datetime", loc, tz)
        for j, row in enumerate(v):
            if not isinstance(row, list) or len(row) < 4:
                raise input_error(f"{loc}/{j}", "expected [amount, price, sid, symbol, value]")
            amount = num(row[0], "amount", f"{loc}/{j}")
            px = num(row[1], "price", f"{loc}/{j}")
            if amount is None or amount == 0 or px is None or px <= 0:
                raise input_error(f"{loc}/{j}", "amount must be non-zero and price positive")
            n += 1
            sym = str(row[3])
            sub = Record(row, dump_raw(row), f"{loc}/{j}", rec.file_idx, rec.line_idx * 100_000 + n)
            ref = f"txn-{sym}-{n}"
            o = SimOrder(
                ref=ref,
                instrument=sym,
                side="buy" if amount > 0 else "sell",
                order_type="other",
                tif="other",
                qty=None,
                limit_px=None,
                placed_ns=None,
                placed_precision=prec,
                rec=sub,
                record=False,
            )
            o.fills.append(SimFill(ts, prec, abs(amount), px, None, f"{ref}:fill", sub))
            run.orders.append(o)
    run.warn(
        f"Transactions analyzer: {n} entr(ies) imported as orders known from fills only (one per data feed and bar, "
        "net of every execution in it): no unfilled orders and no limit prices (coverage=fills_only); write the "
        "QtOrderLog analyzer's notification lines for the full order lifecycle"
    )


def _order_line(obj: dict[str, Any], rec: Record, by_ref: dict[str, SimOrder], run: SimRun, tz: str) -> None:
    loc = rec.location
    if obj.get("ref") is None:
        raise input_error(loc, "order notification without ref")
    ref = str(db_int(obj.get("ref"), "ref", loc))
    status = _name(obj.get("status"))
    if status not in _STATUS:
        raise input_error(loc, f"status {obj.get('status')!r} is not a Backtrader Order.Status name")
    if status == "created":
        run.skip("Created notification (no event)")
        return
    o = by_ref.get(ref)
    if o is None:
        side = _name(obj.get("ordtype"))
        if side not in ("buy", "sell"):
            raise input_error(loc, f"ordtype {obj.get('ordtype')!r} is not Buy / Sell")
        ex = _name(obj.get("exectype"))
        if ex not in _EXEC:
            raise input_error(loc, f"exectype {obj.get('exectype')!r} is not a Backtrader Order.ExecTypes name")
        size = num(obj.get("size"), "size", loc)
        if size is None or size == 0:
            raise input_error(loc, "size must be non-zero")
        if (size < 0) != (side == "sell"):
            raise input_error(loc, f"size {dtext(size)} contradicts ordtype {obj.get('ordtype')}")
        otype = _EXEC[ex]
        limit = None
        if ex == "limit":
            limit = num(obj.get("price"), "price", loc)
        elif ex == "stoplimit":
            limit = num(obj.get("pricelimit"), "pricelimit", loc)
        if otype in ("limit", "stop_limit") and ex != "stoptraillimit" and (limit is None or limit <= 0):
            raise input_error(loc, f"{obj.get('exectype')} order without a positive limit price")
        created, prec = time_of(obj.get("created"), "created", loc, tz)
        valid = obj.get("valid")
        tif = "other" if otype == "market" or ex == "historical" else ("gtc" if valid in (None, "") else "gtd")
        o = SimOrder(
            ref=ref,
            instrument=str(obj.get("data") or "data0"),
            side=side,
            order_type=otype,
            tif=tif,
            qty=abs(size),
            limit_px=limit,
            placed_ns=created,
            placed_precision=prec,
            rec=rec,
            submit_ns=created,
            key_hint=key_hint(obj.get("order_key")),
            note=f"backtrader {obj.get('exectype')} order (BackBroker fill: the bar's price, no queue)",
        )
        by_ref[ref] = o
        run.orders.append(o)
    if status == "accepted":
        o.placed_ns, o.placed_precision = time_of(obj.get("dt") or obj.get("created"), "dt", loc, tz)
    bits = obj.get("exbits")
    if status in ("partial", "completed"):
        if not isinstance(bits, list):
            raise input_error(
                loc, f"{obj.get('status')} notification without an exbits list (use the QtOrderLog analyzer)"
            )
    if isinstance(bits, list):
        for j, b in enumerate(bits):
            bl = f"{loc}/exbits/{j}"
            if not isinstance(b, dict):
                raise input_error(bl, "an execution bit must be an object {dt, size, price, comm}")
            q = num(b.get("size"), "size", bl)
            p = num(b.get("price"), "price", bl)
            if q is None or q == 0 or p is None or p <= 0:
                raise input_error(bl, "execution size must be non-zero and price positive")
            if (q < 0) != (o.side == "sell"):
                raise input_error(bl, f"execution size {dtext(q)} contradicts the {o.side} order")
            ts, prec = time_of(b.get("dt"), "dt", bl, tz)
            fee = num(b.get("comm"), "comm", bl) if b.get("comm") is not None else None
            sub = Record(b, dump_raw(b), bl, rec.file_idx, rec.line_idx * 1000 + j)
            o.fills.append(SimFill(ts, prec, abs(q), p, fee, f"{ref}:{len(o.fills) + 1}", sub))
    if status in _END:
        et, reason = _END[status]
        if o.end is None:
            ts, prec = time_of(obj.get("dt") or obj.get("created"), "dt", loc, tz)
            o.end = (ts, prec, et, reason)
        else:
            run.skip("second terminal notification of an order")


def read_backtrader(paths: list[str], opts: dict[str, Any]) -> SimRun:
    """`QtOrderLog` notification lines and / or `Transactions` analyzer dumps -> `SimRun` (rules in the module
    docstring)."""
    tz = data_tz(opts)
    run = SimRun("backtrader", [], [str(p) for p in paths])
    by_ref: dict[str, SimOrder] = {}
    trades: dict[str, int] = {}
    pnl: dict[str, Decimal] = {}
    for i, p in enumerate(paths):
        recs = read_json_records(Path(p), i)
        if not recs:
            raise input_error(str(p), "empty input (no Backtrader notification line)")
        if len(recs) == 1 and isinstance(recs[0].obj, dict) and "event" not in recs[0].obj:
            doc = recs[0].obj
            if doc and all(isinstance(v, list) for v in doc.values()):
                _read_transactions(doc, recs[0], run, tz)
                continue
        for rec in recs:
            obj = rec.obj
            if not isinstance(obj, dict) or _name(obj.get("event")) not in ("order", "trade"):
                raise input_error(
                    rec.location,
                    'not a Backtrader notification line ({"event": "order" | "trade", ...}, written by the '
                    "QtOrderLog analyzer) nor a Transactions analyzer dump",
                )
            if _name(obj.get("event")) == "trade":
                st = _name(obj.get("status"))
                if st not in ("created", "open", "closed"):
                    raise input_error(
                        rec.location, f"trade status {obj.get('status')!r} is not Created / Open / Closed"
                    )
                trades[st] = trades.get(st, 0) + 1
                if st == "closed":
                    v = num(obj.get("pnlcomm"), "pnlcomm", rec.location)
                    d = str(obj.get("data") or "data0")
                    pnl[d] = pnl.get(d, Decimal(0)) + (v or Decimal(0))
                continue
            _order_line(obj, rec, by_ref, run, tz)
    if trades:
        sums = ", ".join(f"{d} {dtext(v)}" for d, v in sorted(pnl.items()))
        run.warn(
            f"trade notifications: {sum(trades.values())} ({trades.get('closed', 0)} closed; pnlcomm {sums or 'n/a'}): "
            "positions, not orders, so they carry no event"
        )
    one, many = instrument_names(opts)
    for o in run.orders:
        o.instrument = rename(o.instrument, one, many)
        o.fills.sort(key=lambda f: (f.ts_ns, f.fill_id))
    apply_timeframe(run, opts)
    run.orders.sort(key=lambda o: (o.ref_ns, o.ref))
    if not run.orders:
        run.warn("no order notification: the log holds no order")
    return run


def import_backtrader_sim(paths: list[str], opts: ImportOptions) -> ImportResult:
    """Backtrader notification lines / Transactions dump -> canonical rows of venue `sim:backtrader`; option
    `pair_with` (a canonical live-log directory) gives paired orders the live `order_key`."""
    check_options(opts.extra, BT_OPTIONS, "backtrader")
    run = read_backtrader([str(p) for p in paths], opts.extra)
    return import_backtest_sim(run, opts)


def import_backtrader(
    inputs: list[str] | str,
    out: str | Path,
    *,
    account: str | None = None,
    instrument_map: str | None = None,
    append: bool = False,
    **options: Any,
) -> Any:
    """Backtrader notification lines -> canonical simulator log directory `out` (the same import as
    `queuetruth.live.import_sim("backtrader", ...)`); returns the `LiveImportSummary`."""
    from queuetruth.live.imap import load_instrument_map

    paths = [inputs] if isinstance(inputs, str) else [str(p) for p in inputs]
    for p in paths:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
    imap = load_instrument_map(instrument_map) if instrument_map else None
    res = import_backtrader_sim(paths, ImportOptions(account=account, imap=imap, extra=dict(options)))
    return finish_to_dir(BT_SIM, res, paths, Path(out), append=append, status="simulated")


def _paths(x: list[str | Path] | str | Path) -> list[str]:
    return [str(x)] if isinstance(x, (str, Path)) else [str(p) for p in x]


def reconcile_backtrader(
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
    """A Backtrader backtest (`sim`: notification lines) against a canonical live log (`live`: a directory written
    by `queuetruth live import`): writes `<out>/live`, `<out>/sim`, `<out>/score`, `reconcile.json` and
    `reconcile_orders.csv`; returns the `ReconcileReport`. `time_tol_ns` defaults to 0 with option `timeframe`
    (the fill is known to the bar) and 60 s without. Options: `timeframe`, `fill_delay_bars`, `pair_window_ms`,
    `instrument`, `instruments`, `data_tz`, `venue`, `price_exp`, `size_exp`."""
    check_options(options, BT_OPTIONS - {"pair_with", "map_venue"}, "backtrader reconcile")
    sp = _paths(sim)
    for p in [*sp, *_paths(live)]:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
    run = read_backtrader(sp, options)
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
    "BACKTRADER_VERSION",
    "BT_OPTIONS",
    "BT_SIM",
    "import_backtrader",
    "import_backtrader_sim",
    "read_backtrader",
    "reconcile_backtrader",
]
