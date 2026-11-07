"""QuantConnect LEAN importer: a backtest's orders and order events -> the simulator arm (`sim:lean`) of the scorer
and of `reconcile_lean` (shared code in `queuetruth.live.importers.backtest`).

Formats (read from LEAN's public source, release tag 18241 of github.com/QuantConnect/Lean; LEAN itself is never
imported):

- The backtest result JSON, `<algorithm id>.json` (`BacktestingResultHandler.StoreResult`, `BacktestResult`): its
  `orders` member maps each order id to an `Order` (`Common/Orders/Order.cs`, camelCase names): `id`, `symbol`
  (`SymbolJsonConverter`: an object `{value, id, permtick}`; `value` is the ticker), `time` (UTC creation time, ISO
  8601), `type` (`OrderType`: 0 Market, 1 Limit, 2 StopMarket, 3 StopLimit, 4 MarketOnOpen, 5 MarketOnClose,
  6 OptionExercise, 7 LimitIfTouched, 8 ComboMarket, 9 ComboLimit, 10 ComboLegLimit, 11 TrailingStop), `status`
  (`OrderStatus`: 0 New, 1 Submitted, 2 PartiallyFilled, 3 Filled, 5 Canceled, 6 None, 7 Invalid, 8 CancelPending,
  9 UpdateSubmitted), `direction` (`OrderDirection`: 0 Buy, 1 Sell, 2 Hold), `quantity` (negative for a sell),
  `limitPrice`, `stopPrice`, `properties.timeInForce` (`TimeInForceJsonConverter`: `{}` for good-til-canceled, else
  a `$type` naming `DayTimeInForce` or `GoodTilDateTimeInForce`). A result packet of the QuantConnect API may also
  carry `orderEvents`; a local run leaves it out.
- The order events, `<algorithm id>-order-events.json` (`BaseResultsHandler.StoreOrderEvents`): a JSON array of
  `SerializedOrderEvent` (`Common/Orders/Serialization/SerializedOrderEvent.cs`): `id` (`<algorithm id>-<order
  id>-<order event id>`), `algorithmId`, `orderId`, `orderEventId`, `symbol` (the security identifier text, such as
  `BTCUSDT 2XR`), `symbolValue` (the ticker), `time` (UTC, Unix seconds), `status` (the `OrderStatus` name in
  camelCase: `submitted`, `partiallyFilled`, `filled`, `canceled`, `invalid`, `cancelPending`, `updateSubmitted`,
  `new`, `none`), `fillPrice`, `fillPriceCurrency`, `fillQuantity` (negative for a sell), `direction` (`buy`,
  `sell`), `orderType` (optional), `quantity`, `limitPrice`, `stopPrice`, `orderFeeAmount`, `orderFeeCurrency`,
  `message`. The pre-camelCase names (`order-id`, `order-event-id`, `symbol-value`, `order-fee-amount`, ...) and
  the C# property names of `OrderEvent` (`OrderId`, `Id`, `Symbol`, `UtcTime`, `Status`, `FillPrice`,
  `FillQuantity`, `Direction`, `OrderFee` = `{Value: {Amount, Currency}}`, `Quantity`, `LimitPrice`, ...) are read
  too, with enums as names or numbers. A CSV with the same names as its header (for example the events file passed
  through `pandas.read_json(...).to_csv(...)`) reads the same way. LEAN writes no CSV of its own.

Mapping: one order per order id (ids restart at 1 in every backtest, so one import holds one backtest: a second
orders section, events of another `algorithmId`, or the same input twice fail `QT-CONFIG-001`). `submit_sent` at
the order's `time`, `ack` at its first `submitted` event (none for an order that ended `invalid` without one), one
fill per `partiallyFilled` / `filled` event (`<order id>:<order event id>`, fee = `orderFeeAmount` when its currency
is the fill price's), `cancel_ack` at `canceled`, `reject` at `invalid` (with LEAN's message). `new`, `none`,
`cancelPending` and `updateSubmitted` events are counted: the orders section holds the order's final price and
quantity. Order types: Market, MarketOnOpen and MarketOnClose -> market, Limit -> limit at `limitPrice`, StopMarket
and TrailingStop -> stop, StopLimit -> stop_limit at `limitPrice`, LimitIfTouched -> stop_limit at `limitPrice` (as
Binance's TAKE_PROFIT_LIMIT); OptionExercise and the combo types fail `QT-INPUT-002`. Time in force: `gtc`, `day`,
`gtd` from `timeInForce`, `other` for market orders. Without an orders section the order comes from its events
(`orderType`, `quantity`, `limitPrice`, `direction`); an event stream without `orderType` then fails. An orders
section without any order event fails: the fills are in the events file. The instrument is the symbol's ticker
(`symbol.value`, `symbolValue`), renamed with options `instrument` / `instruments`. LEAN reports every time in UTC.

LEAN stamps an event with the algorithm's time, which on bar data is the end of the bar it acted on. With option
`timeframe`, a fill stamped later than its order's submission (a resting order the next bar traded through) is moved
one bar earlier, to the start of the bar that filled it, never before the submission; a fill at the submission time
(a market order filled on the bar it was placed on) keeps its time. The reconciliation then reads a live fill inside
that bar as on time, as for Backtrader and VectorBT.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live._common import Record, dec, dump_raw, input_error, read_csv_rows, read_json_records, read_text
from queuetruth.live.builder import ImportResult
from queuetruth.live.importers._base import ImportOptions
from queuetruth.live.importers.backtest import (
    COMMON_OPTIONS,
    SimFill,
    SimOrder,
    SimRun,
    apply_timeframe,
    check_labels,
    import_backtest_sim,
    instrument_names,
    reconcile_backtest,
    rename,
    time_of,
)
from queuetruth.live.importers.freqtrade import (
    ReconcileReport,
    check_options,
    db_int,
    dtext,
    finish_to_dir,
    num,
    timeframe_seconds,
)
from queuetruth.schemas.io import sha256_file

LEAN_SIM = "sim:lean"
#: LEAN release (git tag of github.com/QuantConnect/Lean) the format notes were read from.
LEAN_VERSION = "18241"
#: LEAN reports every time in UTC, so `data_tz` does not apply.
LEAN_OPTIONS = COMMON_OPTIONS - {"data_tz"}
_NS = 1_000_000_000

#: `OrderType` (numbers and names, lowercased) -> (canonical order type, uses `limitPrice`, LEAN name).
_TYPES: dict[str, tuple[str, bool, str]] = {
    "market": ("market", False, "Market"),
    "limit": ("limit", True, "Limit"),
    "stopmarket": ("stop", False, "StopMarket"),
    "stoplimit": ("stop_limit", True, "StopLimit"),
    "marketonopen": ("market", False, "MarketOnOpen"),
    "marketonclose": ("market", False, "MarketOnClose"),
    "limitiftouched": ("stop_limit", True, "LimitIfTouched"),
    "trailingstop": ("stop", False, "TrailingStop"),
}
_TYPE_NAMES = (
    "market", "limit", "stopmarket", "stoplimit", "marketonopen", "marketonclose", "optionexercise",
    "limitiftouched", "combomarket", "combolimit", "comboleglimit", "trailingstop",
)  # fmt: skip
_STATUS_NAMES = {0: "new", 1: "submitted", 2: "partiallyfilled", 3: "filled", 5: "canceled", 6: "none"}
_STATUS_NAMES |= {7: "invalid", 8: "cancelpending", 9: "updatesubmitted"}
_STATUSES = frozenset(_STATUS_NAMES.values())
_COUNTED = {
    "new": "New event (no event)",
    "none": "None event (no event)",
    "cancelpending": "CancelPending event (the Canceled event ends the order)",
    "updatesubmitted": "UpdateSubmitted event (the orders section holds the final price and quantity)",
}


def _norm(k: Any) -> str:
    return str(k).replace("-", "").replace("_", "").lower()


def _fields(obj: dict[str, Any]) -> dict[str, Any]:
    """An object's members by normalised name (case, `-` and `_` ignored): LEAN has written camelCase, PascalCase
    and hyphenated names over its releases."""
    out: dict[str, Any] = {}
    for k, v in obj.items():
        out.setdefault(_norm(k), v)
    return out


def _enum(v: Any, names: tuple[str, ...] | dict[int, str], what: str, loc: str) -> str:
    """A LEAN enum given as its number or its name -> the lowercased name."""
    if isinstance(v, bool) or v is None:
        raise input_error(loc, f"{what}: {v!r} is not a LEAN {what} value")
    if isinstance(v, (int, Decimal)) or (isinstance(v, str) and v.strip().isdigit()):
        n = int(Decimal(str(v).strip()))
        table = names if isinstance(names, dict) else dict(enumerate(names))
        if n not in table:
            raise input_error(loc, f"{what}: {v!r} is not a LEAN {what} number")
        return table[n]
    s = _norm(v)
    known = set(names.values()) if isinstance(names, dict) else set(names)
    if s not in known:
        raise input_error(loc, f"{what}: {v!r} is not a LEAN {what} name")
    return s


def _side(v: Any, loc: str) -> str:
    d = _enum(v, ("buy", "sell", "hold"), "OrderDirection", loc)
    if d == "hold":
        raise input_error(loc, "direction Hold: an order of quantity zero has no side")
    return d


def _time(v: Any, what: str, loc: str) -> tuple[int, str]:
    """A LEAN time: Unix seconds (a JSON number, as `SerializedOrderEvent.time` writes it; doubles are rounded to
    the microsecond) or ISO 8601 text (naive text is UTC, as LEAN reports every time)."""
    if isinstance(v, bool):
        raise input_error(loc, f"{what}: {v!r} is not a time")
    d = None
    if isinstance(v, (int, float, Decimal)):
        d = num(v, what, loc) if isinstance(v, float) else dec(v, what, loc)
    elif isinstance(v, str) and v.strip().replace(".", "", 1).isdigit():
        d = dec(v.strip(), what, loc)
    if d is None:
        return time_of(v, what, loc, "UTC")
    if not 0 < d < 10**11:
        raise input_error(loc, f"{what}: {v!r} is not a Unix time in seconds")
    us = int((d * 1_000_000).to_integral_value(rounding=ROUND_HALF_EVEN))
    prec = "s" if us % 1_000_000 == 0 else ("ms" if us % 1000 == 0 else "us")
    return us * 1000, prec


def _symbol(v: Any, loc: str) -> str:
    """The ticker of a LEAN symbol: `symbol.value` of a `SymbolJsonConverter` object, else a plain text (a
    security identifier such as `BTCUSDT 2XR` gives its first word)."""
    if isinstance(v, dict):
        f = _fields(v)
        for k in ("value", "permtick"):
            if f.get(k) not in (None, ""):
                return str(f[k]).strip()
        if f.get("id") not in (None, ""):
            return str(f["id"]).split()[0]
        raise input_error(loc, "symbol object without value, permtick or id")
    if v in (None, "") or not str(v).strip():
        raise input_error(loc, "missing symbol")
    return str(v).split()[0]


def _tif(f: dict[str, Any], loc: str) -> str:
    props = f.get("properties")
    t = _fields(props).get("timeinforce") if isinstance(props, dict) else None
    if t is None:
        t = f.get("timeinforce")
    if t is None:
        return "gtc"
    if isinstance(t, dict):
        typ = str(_fields(t).get("$type") or "")
        if not typ:
            return "gtc"  # TimeInForceJsonConverter leaves $type out for good-til-canceled
        t = typ
    s = _norm(str(t).rsplit(".", 1)[-1].split(",")[0])
    for key, tif in (("goodtildate", "gtd"), ("goodtilcanceled", "gtc"), ("day", "day")):
        if s.startswith(key):
            return tif
    raise input_error(loc, f"timeInForce {t!r} is not GoodTilCanceled, Day or GoodTilDate")


def _order_type(v: Any, loc: str) -> tuple[str, bool, str]:
    name = _enum(v, _TYPE_NAMES, "OrderType", loc)
    if name not in _TYPES:
        raise input_error(
            loc,
            f"order type {name} cannot be mapped (option exercises and combo orders have no single-instrument "
            "order to score); import a backtest of market, limit, stop and stop-limit orders",
        )
    return _TYPES[name]


# ---- inputs --------------------------------------------------------------------------------------------------------


class _Event:
    """One LEAN order event (normalised fields)."""

    __slots__ = ("algo", "event_id", "f", "order_id", "prec", "rec", "status", "ts")

    def __init__(self, obj: dict[str, Any], rec: Record) -> None:
        loc = rec.location
        f = _fields(obj)
        self.f, self.rec = f, rec
        if f.get("orderid") is None:
            raise input_error(loc, "order event without orderId")
        self.order_id = db_int(f["orderid"], "orderId", loc)
        eid = f.get("ordereventid")
        if eid is None:
            raw = f.get("id")
            if isinstance(raw, str) and "-" in raw:  # `<algorithm id>-<order id>-<order event id>`
                eid = raw.rsplit("-", 1)[1]
            else:
                eid = raw  # `OrderEvent.Id` (C# property names)
        if eid is None:
            raise input_error(loc, "order event without orderEventId")
        self.event_id = db_int(eid, "orderEventId", loc)
        if f.get("status") is None:
            raise input_error(loc, "order event without status")
        self.status = _enum(f["status"], _STATUS_NAMES, "OrderStatus", loc)
        t = f.get("utctime") if f.get("utctime") is not None else f.get("time")
        self.ts, self.prec = _time(t, "time", loc)
        algo = f.get("algorithmid")
        self.algo = None if algo in (None, "") else str(algo)

    def key(self) -> tuple[Any, ...]:
        f = self.f
        return (self.status, self.ts, str(f.get("fillquantity")), str(f.get("fillprice")))

    def symbol(self) -> str:
        f = self.f
        if f.get("symbolvalue") not in (None, ""):
            return str(f["symbolvalue"]).strip()
        return _symbol(f.get("symbol"), self.rec.location)

    def fee(self) -> tuple[Decimal | None, str | None]:
        f, loc = self.f, self.rec.location
        amount, cur = f.get("orderfeeamount"), f.get("orderfeecurrency")
        fee = f.get("orderfee")
        if amount is None and isinstance(fee, dict):  # `OrderFee` = {Value: {Amount, Currency}}
            val = _fields(fee).get("value")
            vf = _fields(val) if isinstance(val, dict) else _fields(fee)
            amount, cur = vf.get("amount"), vf.get("currency")
        if amount in (None, ""):
            return None, None
        return num(amount, "orderFeeAmount", loc), (None if cur in (None, "") else str(cur))


def _csv_records(path: Path, file_idx: int) -> list[Record]:
    header, rows = read_csv_rows(read_text(path), str(path))
    out = []
    for i, r in enumerate(rows):
        row = {header[j]: (r[j] if j < len(r) else "") for j in range(len(header)) if header[j]}
        row = {k: (None if v == "" else v) for k, v in row.items()}
        out.append(Record(row, dump_raw(row), f"{path}:{i + 2}", file_idx, i))
    return out


def _read_file(path: Path, file_idx: int) -> tuple[list[Record], list[Record]]:
    """(order records, event records) of one input: a result JSON (`orders`, maybe `orderEvents`), an order-events
    JSON array or JSON lines, or an order-events CSV."""
    text = read_text(path)
    if not text.strip():
        raise input_error(str(path), "empty input (no LEAN orders or order events)")
    if text.lstrip()[:1] not in ("[", "{"):
        recs = _csv_records(path, file_idx)
        if not recs or "orderid" not in {_norm(k) for k in recs[0].obj}:
            raise input_error(str(path), "not a LEAN order-events CSV (no orderId column)")
        return [], recs
    recs = read_json_records(path, file_idx)
    orders: list[Record] = []
    events: list[Record] = []
    for rec in recs:
        obj = rec.obj
        if not isinstance(obj, dict):
            raise input_error(rec.location, "expected a LEAN result object or an order event object")
        f = _fields(obj)
        if "orders" in f or "orderevents" in f:
            raw_orders = f.get("orders")
            if isinstance(raw_orders, dict):
                items = [(f"{rec.location}/orders/{k}", v) for k, v in raw_orders.items()]
            elif isinstance(raw_orders, list):
                items = [(f"{rec.location}/orders/{i}", v) for i, v in enumerate(raw_orders)]
            elif raw_orders is None:
                items = []
            else:
                raise input_error(rec.location, "orders must be an object (order id -> order) or an array")
            for n, (loc, v) in enumerate(items):
                orders.append(Record(v, dump_raw(v), loc, file_idx, rec.line_idx * 1_000_000 + n))
            raw_events = f.get("orderevents")
            if raw_events is not None and not isinstance(raw_events, list):
                raise input_error(rec.location, "orderEvents must be an array")
            for n, v in enumerate(raw_events or []):
                loc = f"{rec.location}/orderEvents/{n}"
                events.append(Record(v, dump_raw(v), loc, file_idx, rec.line_idx * 1_000_000 + 500_000 + n))
        elif "orderid" in f:
            events.append(rec)
        else:
            raise input_error(
                rec.location,
                "not LEAN backtest output: expected the result JSON (with orders) or order events (with orderId)",
            )
    if not orders and not events:
        raise input_error(str(path), "no LEAN orders or order events")
    return orders, events


# ---- orders --------------------------------------------------------------------------------------------------------


def _new_order(
    ref: str,
    inst: str,
    side: str,
    otype: tuple[str, bool, str],
    tif: str,
    qty: Decimal,
    f: dict[str, Any],
    rec: Record,
    submit: tuple[int, str] | None,
) -> SimOrder:
    kind, uses_limit, lean_name = otype
    loc = rec.location
    limit = num(f.get("limitprice"), "limitPrice", loc) if uses_limit else None
    if uses_limit and (limit is None or limit <= 0):
        raise input_error(loc, f"{lean_name} order without a positive limitPrice")
    return SimOrder(
        ref=ref,
        instrument=inst,
        side=side,
        order_type=kind,
        tif="other" if kind == "market" else tif,
        qty=qty,
        limit_px=limit,
        placed_ns=None,  # set by the Submitted event: an Invalid order never rested
        placed_precision=submit[1] if submit else "s",
        rec=rec,
        submit_ns=submit[0] if submit else None,
        note=f"lean {lean_name} order (fill model: the bar's prices, no queue)",
    )


def _signed_qty(v: Any, side: str, what: str, loc: str) -> Decimal:
    q = num(v, what, loc)
    if q is None or q == 0:
        raise input_error(loc, f"{what} must be non-zero")
    if (q < 0) != (side == "sell"):
        raise input_error(loc, f"{what} {dtext(q)} contradicts the {side} direction")
    return abs(q)


def _order_from_record(rec: Record) -> tuple[int, SimOrder, str]:
    """(order id, order, the orders section's status name) of one `Order` object."""
    loc = rec.location
    if not isinstance(rec.obj, dict):
        raise input_error(loc, "an order must be an object")
    f = _fields(rec.obj)
    if f.get("id") is None:
        raise input_error(loc, "order without id")
    oid = db_int(f["id"], "id", loc)
    if f.get("type") is None:
        raise input_error(loc, "order without type")
    otype = _order_type(f["type"], loc)
    qty_raw = num(f.get("quantity"), "quantity", loc)
    if f.get("direction") is not None:
        side = _side(f["direction"], loc)
    elif qty_raw is not None and qty_raw != 0:
        side = "buy" if qty_raw > 0 else "sell"
    else:
        raise input_error(loc, "order without direction or a signed quantity")
    qty = _signed_qty(f.get("quantity"), side, "quantity", loc)
    t = f.get("createdtime") if f.get("time") is None else f.get("time")
    submit = _time(t, "time", loc)
    status = _enum(f.get("status", 0), _STATUS_NAMES, "OrderStatus", loc)
    o = _new_order(str(oid), _symbol(f.get("symbol"), loc), side, otype, _tif(f, loc), qty, f, rec, submit)
    return oid, o, status


def _order_from_event(ev: _Event) -> SimOrder:
    """An order known from its events only (no orders section): type, quantity and prices of its first event."""
    f, loc = ev.f, ev.rec.location
    if f.get("ordertype") in (None, ""):
        raise input_error(
            loc,
            "order event without orderType and no orders section: give the backtest result JSON (with orders) too",
        )
    otype = _order_type(f["ordertype"], loc)
    side = _side(f.get("direction"), loc)
    qty = _signed_qty(f.get("quantity"), side, "quantity", loc)
    # without the orders section the order's creation time is its first event's
    return _new_order(str(ev.order_id), ev.symbol(), side, otype, "gtc", qty, f, ev.rec, (ev.ts, ev.prec))


def _apply_events(o: SimOrder, evs: list[_Event], run: SimRun, fees: list[int]) -> None:
    for ev in evs:
        f, loc = ev.f, ev.rec.location
        st = ev.status
        if st == "submitted":
            if o.placed_ns is None:
                o.placed_ns, o.placed_precision = ev.ts, ev.prec
            else:
                run.skip("second Submitted event of an order")
        elif st in ("partiallyfilled", "filled"):
            q = _signed_qty(f.get("fillquantity"), o.side, "fillQuantity", loc)
            px = num(f.get("fillprice"), "fillPrice", loc)
            if px is None or px <= 0:
                raise input_error(loc, "fillPrice must be positive")
            fee, cur = ev.fee()
            pcur = f.get("fillpricecurrency")
            if fee is not None and cur is not None and pcur not in (None, "") and cur != str(pcur):
                fees[0] += 1
                fee = None
            o.fills.append(SimFill(ev.ts, ev.prec, q, px, fee, f"{o.ref}:{ev.event_id}", ev.rec))
        elif st in ("canceled", "invalid"):
            if o.end is not None:
                run.skip("second terminal event of an order")
                continue
            msg = str(f.get("message") or "").strip()
            et, label = ("cancel_ack", "Canceled") if st == "canceled" else ("reject", "Invalid")
            o.end = (ev.ts, ev.prec, et, f"{label}: {msg}" if msg else label)
        else:
            run.skip(_COUNTED[st])


def _check_fills(o: SimOrder, evs: list[_Event]) -> None:
    """Fills beyond the order's quantity, or events of another symbol, mean the inputs do not belong together."""
    if o.qty is not None and o.filled > o.qty:
        raise input_error(
            o.rec.location,
            f"the fills of order {o.ref} sum to {dtext(o.filled)}, more than its quantity {dtext(o.qty)}: the result "
            "JSON and the order-events file come from different backtests?",
        )
    for ev in evs:
        f = ev.f
        if f.get("symbolvalue") in (None, "") and f.get("symbol") is None:
            continue
        if ev.symbol() != o.instrument:
            raise input_error(
                ev.rec.location, f"order {o.ref} event of symbol {ev.symbol()}, but the order is {o.instrument}"
            )


def _check_status(oid: int, o: SimOrder, status: str, loc: str) -> None:
    """The orders section's final status must have its events."""
    if status in ("partiallyfilled", "filled") and not o.fills:
        raise input_error(loc, f"order {oid} has status {status} but no fill event (give the order-events file)")
    if status in ("canceled", "invalid") and o.end is None:
        raise input_error(loc, f"order {oid} has status {status} but no {status} event (give the order-events file)")


def _bar_start(run: SimRun, opts: dict[str, Any]) -> None:
    """Option `timeframe`: a fill stamped after its order's submission is moved to the start of its bar."""
    tf = opts.get("timeframe")
    if tf in (None, ""):
        return
    step = timeframe_seconds(tf) * _NS
    moved = 0
    for o in run.orders:
        if o.submit_ns is None:
            continue
        for fl in o.fills:
            if fl.ts_ns > o.submit_ns:
                fl.ts_ns = max(fl.ts_ns - step, o.submit_ns)
                moved += 1
    if moved:
        run.warn(
            f"timeframe={tf}: {moved} fill time(s) stamped at the end of their bar (LEAN's time) moved to the bar's "
            "start, never before the order's submission"
        )


def read_lean(paths: list[str], opts: dict[str, Any]) -> SimRun:
    """LEAN backtest result JSON and order-events files -> `SimRun` (rules in the module docstring). One import is
    one backtest: order ids restart at 1 in every backtest."""
    run = SimRun("lean", [], [str(p) for p in paths])
    seen: dict[str, str] = {}
    order_recs: list[Record] = []
    events: list[_Event] = []
    n_sections = 0
    for i, p in enumerate(paths):
        digest = sha256_file(p) if Path(p).is_file() else ""
        if digest in seen:
            raise QtError("QT-CONFIG-001", f"{p}: the same input as {seen[digest]} (sha256 {digest[:16]}): given twice")
        seen[digest] = str(p)
        o, e = _read_file(Path(p), i)
        if o:
            n_sections += 1
        order_recs += o
        for rec in e:
            if not isinstance(rec.obj, dict):
                raise input_error(rec.location, "an order event must be an object")
            events.append(_Event(rec.obj, rec))
    if n_sections > 1:
        raise QtError(
            "QT-CONFIG-001",
            f"{n_sections} LEAN orders sections given: order ids restart at 1 in every backtest, so one import holds "
            "one backtest (its result JSON and its order-events file)",
        )
    algos = sorted({e.algo for e in events if e.algo is not None})
    if len(algos) > 1:
        raise QtError(
            "QT-CONFIG-001",
            f"order events of {len(algos)} backtests (algorithmId {algos[:4]}): import one backtest at a time",
        )
    if not events:
        raise input_error(
            str(paths[0]) if paths else "inputs",
            "no LEAN order events: a local backtest writes them to <algorithm id>-order-events.json next to the "
            "result JSON; give that file too (the orders section has no fills)",
        )
    uniq: dict[tuple[int, int], _Event] = {}
    for ev in events:
        k = (ev.order_id, ev.event_id)
        prev = uniq.get(k)
        if prev is None:
            uniq[k] = ev
        elif prev.key() == ev.key():
            run.skip("duplicate order event (same orderId and orderEventId)")
        else:
            raise input_error(
                ev.rec.location,
                f"order {ev.order_id} event {ev.event_id} differs from {prev.rec.location}: two backtests mixed?",
            )
    by_order: dict[int, list[_Event]] = {}
    for ev in sorted(uniq.values(), key=lambda e: (e.order_id, e.event_id, e.ts)):
        by_order.setdefault(ev.order_id, []).append(ev)
    fees = [0]
    if order_recs:
        orders: dict[int, tuple[SimOrder, str, str]] = {}
        for rec in order_recs:
            oid, o, status = _order_from_record(rec)
            if oid in orders:
                raise input_error(rec.location, f"order id {oid} twice in the orders section")
            orders[oid] = (o, status, rec.location)
        stray = sorted(set(by_order) - set(orders))
        if stray:
            ev = by_order[stray[0]][0]
            raise input_error(
                ev.rec.location,
                f"order events for order id(s) {stray[:5]} that the orders section does not hold: the result JSON "
                "and the order-events file come from different backtests?",
            )
        for oid in sorted(orders):
            o, status, loc = orders[oid]
            _apply_events(o, by_order.get(oid, []), run, fees)
            _check_fills(o, by_order.get(oid, []))
            _check_status(oid, o, status, loc)
            run.orders.append(o)
    else:
        run.warn(
            "no orders section (the result JSON): orders built from their events (type, quantity and prices of the "
            "first event; submission at the first event's time)"
        )
        for oid in sorted(by_order):
            evs = by_order[oid]
            o = _order_from_event(evs[0])
            _apply_events(o, evs, run, fees)
            _check_fills(o, evs)
            run.orders.append(o)
    for o in run.orders:
        if o.placed_ns is None and (o.end is None or o.end[2] != "reject") and (o.fills or o.end is not None):
            # no Submitted event but the order traded or was cancelled: it rested from its creation
            o.placed_ns, o.placed_precision = o.submit_ns, o.placed_precision
    if fees[0]:
        run.warn(f"{fees[0]} fill fee(s) in another currency than the fill price left out")
    one, many = instrument_names(opts)
    check_labels({o.instrument for o in run.orders}, many, "symbols")
    for o in run.orders:
        o.instrument = rename(o.instrument, one, many)
        o.fills.sort(key=lambda fl: (fl.ts_ns, fl.fill_id))
    _bar_start(run, opts)
    apply_timeframe(run, opts)
    run.orders.sort(key=lambda o: (o.ref_ns, o.ref))
    return run


def import_lean_sim(paths: list[str], opts: ImportOptions) -> ImportResult:
    """LEAN result JSON / order events -> canonical rows of venue `sim:lean`; option `pair_with` (a canonical
    live-log directory) gives paired orders the live `order_key`."""
    check_options(opts.extra, LEAN_OPTIONS, "lean")
    run = read_lean([str(p) for p in paths], opts.extra)
    return import_backtest_sim(run, opts)


def import_lean(
    inputs: list[str] | str,
    out: str | Path,
    *,
    account: str | None = None,
    instrument_map: str | None = None,
    append: bool = False,
    **options: Any,
) -> Any:
    """LEAN backtest output -> canonical simulator log directory `out` (the same import as
    `queuetruth.live.import_sim("lean", ...)`); returns the `LiveImportSummary`."""
    from queuetruth.live.imap import load_instrument_map

    paths = [inputs] if isinstance(inputs, str) else [str(p) for p in inputs]
    for p in paths:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
    imap = load_instrument_map(instrument_map) if instrument_map else None
    res = import_lean_sim(paths, ImportOptions(account=account, imap=imap, extra=dict(options)))
    return finish_to_dir(LEAN_SIM, res, paths, Path(out), append=append, status="simulated")


def _paths(x: list[str | Path] | str | Path) -> list[str]:
    return [str(x)] if isinstance(x, (str, Path)) else [str(p) for p in x]


def reconcile_lean(
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
    """A LEAN backtest (`sim`: the result JSON and its order-events file) against a canonical live log (`live`: a
    directory written by `queuetruth live import`); outputs as `reconcile_backtrader`. Options: `timeframe`,
    `fill_delay_bars`, `pair_window_ms`, `instrument`, `instruments`, `venue`, `price_exp`, `size_exp`."""
    check_options(options, LEAN_OPTIONS - {"pair_with", "map_venue"}, "lean reconcile")
    sp = _paths(sim)
    for p in [*sp, *_paths(live)]:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
    run = read_lean(sp, options)
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
    "LEAN_OPTIONS",
    "LEAN_SIM",
    "LEAN_VERSION",
    "import_lean",
    "import_lean_sim",
    "read_lean",
    "reconcile_lean",
]
