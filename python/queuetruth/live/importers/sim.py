"""Simulator-output importers: hftbacktest 2.4.4 order snapshots and NautilusTrader backtest reports / order events
-> canonical live log, so `score` can compare a user's simulator against live trading (external arm).

A simulator log is marked by `venue = "sim:hftbacktest"` / `"sim:nautilus"` (scorer and referee detect simulator
logs by this prefix, E38 OC-9) and the schema 1.1 values `source = "sim"` and `validation_status = "simulated"`
(SPEC 3.11 #15; v0.1 logs used `synthetic` / `fixture_only`).

Order keys: a simulator order is linked to the live order it replays through its client order id. When the id
(Nautilus `client_order_id`, hftbacktest `order_key` column or `order_key_map`) has the order-key form
`<venue>:<account>:<id>` (valid id characters) it becomes the `order_key`; otherwise the key is
`sim:<name>:<account>:<id>`; without any id (hftbacktest rows without `order_key`) it is
`sim:hftbacktest:<account>:<order_id>`. A schedule leg suffix `.leg<k>` is stripped from the id in both forms, so
a simulator that places a new order per leg of a re-priced live order (`<order_key>.leg2`, `o3.leg2`, ...) gets
one canonical order per live order: the legs aggregate with leg k > 1 starting as amend_sent / amend_ack
(`queuetruth.live.builder`, "Schedule legs"). Instrument-map lookups use option `map_venue`
(default: the live venue the user names in `venue`, else the simulator name).

hftbacktest (snapshots of `hftbacktest` order objects written by the user's loop, JSON lines or CSV, one row per
order per snapshot; ns timestamps): columns `order_id, side (1 / -1 or buy / sell), price_tick, tick_size, qty,
leaves_qty, exec_qty, exec_price_tick, status, exch_timestamp, local_timestamp, maker, order_type, time_in_force`
and optional `order_key`, `instrument` / `asset_no`. Enums as in hftbacktest 2.4.4: status 0 none, 1 new, 2
expired, 3 filled, 4 canceled, 5 partially filled, 6 rejected, 7 replaced; time_in_force 0 GTC, 1 GTX, 2 FOK,
3 IOC; order_type 0 limit, 1 market (names are accepted too). The first snapshot gives `submit_sent` at
`local_timestamp`; status new -> ack at `exch_timestamp`; a `leaves_qty` decrease -> fill (qty = decrease, price
= `exec_price_tick * tick_size`, `maker` flag, `derived = true` because snapshots are differenced); canceled ->
cancel_ack, expired -> expire, rejected -> reject (GTX -> post_only_reject), a qty or price change -> amend_ack.

NautilusTrader (from its documented report and event fields; CSV, JSON lines or Parquet): rows of
`generate_orders_report` / `generate_order_fills_report` (order records: `client_order_id, venue_order_id,
instrument_id, side, type, quantity, price, time_in_force, post_only, reduce_only, status, filled_qty, avg_px,
ts_init, ts_last`), `generate_fills_report` (per fill: `client_order_id, trade_id, order_side, last_qty, last_px,
liquidity_side, commission, ts_event`) and order events with a `type` column or a file name naming the event
(`OrderInitialized`, `OrderSubmitted`, `OrderAccepted`, `OrderRejected`, `OrderCanceled`, `OrderExpired`,
`OrderUpdated`, `OrderFilled`, `OrderCancelRejected`, `OrderModifyRejected`, `OrderDenied`). Event rows give
captured lifecycle events; report rows give the order record plus an ack at `ts_init` and the terminal status at
`ts_last` (history records, dropped when an event of the same type exists), and one derived fill at `avg_px` for
an order with `filled_qty > 0` but no per-fill row. `instrument_id` `SYMBOL.VENUE` -> instrument `SYMBOL`.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa

from queuetruth.errors import QtError
from queuetruth.live._common import (
    Record,
    as_bool,
    dec,
    dec_text,
    dmul,
    dsub,
    epoch_ns,
    input_error,
    make_order_key,
    read_csv_rows,
    read_json_records,
    read_text,
    rfc3339_ns,
    text_or_none,
    valid_id,
)
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.importers._base import ImportOptions, side_of
from queuetruth.live.importers._parquet import read_parquet

HBT = "sim:hftbacktest"
NT = "sim:nautilus"
_LEG = re.compile(r"\.leg\d+$", re.IGNORECASE)
#: An integer epoch given as text (ASCII digits only).
_INT_TEXT = re.compile(r"-?[0-9]+")
#: An enum given as its number (hftbacktest writes small integers; 18 digits keep `int()` cheap and in range).
_ENUM_INT = re.compile(r"-?[0-9]{1,18}")
_HBT_STATUS = {
    0: "none",
    1: "new",
    2: "expired",
    3: "filled",
    4: "canceled",
    5: "partially_filled",
    6: "rejected",
    7: "replaced",
}
_HBT_TIF = {0: "gtc", 1: "gtx", 2: "fok", 3: "ioc"}
_NT_TIF = {"GTC": "gtc", "IOC": "ioc", "FOK": "fok", "DAY": "day", "GTD": "gtd"}
_NT_TYPE = {"LIMIT": "limit", "MARKET": "market", "STOP_MARKET": "stop", "STOP_LIMIT": "stop_limit"}


def _size_exp(v: Any) -> int:
    """Option `size_exp`: an integer in -18..18, or its decimal text (`QT-CONFIG-001` otherwise)."""
    n: int | None = None
    if isinstance(v, int) and not isinstance(v, bool):
        n = v
    elif isinstance(v, str) and _SIZE_EXP_TEXT.fullmatch(v.strip()):
        n = int(v.strip())
    if n is None or not -18 <= n <= 18:
        raise QtError("QT-CONFIG-001", f"size_exp must be an integer in -18..18, got {v!r:.40}")
    return n


_SIZE_EXP_TEXT = re.compile(r"[+-]?[0-9]{1,3}")


def _builder(name: str, opts: ImportOptions) -> LogBuilder:
    return LogBuilder(
        name,
        account=opts.account or "sim",
        imap=opts.imap,
        price_exp=-9,
        size_exp=_size_exp(opts.extra.get("size_exp", -8)),
        map_venue=str(opts.extra.get("map_venue") or opts.extra.get("venue") or name),
        source_override="sim",
        validation_status="simulated",
    )


def _key(b: LogBuilder, h: int, ident: str | None, opts: ImportOptions, *, fallback: bool = False) -> None:
    if ident is None:
        return
    base = _LEG.sub("", ident) or ident
    if opts.extra.get("key_from_client_id", True) and valid_id(base) and base.count(":") >= 2:
        b.set_order_field(h, "key_override", base)
    elif fallback:
        b.set_order_field(h, "key_override", make_order_key(b.venue, b.account or "sim", base))


#: Decoded-size slack of a simulator Parquet file (`_parquet.read_parquet`); smaller than the dq default because
#: every value becomes a Python object here (about 50 bytes each, so 16 MiB of Arrow data is a few hundred MiB).
_SIM_PARQUET_SLACK = 16 * 2**20


def _table_rows(path: Path, file_idx: int) -> list[Record]:
    """Rows of a CSV, Parquet or JSON / JSON-lines file as records of string-ish values."""
    if path.suffix.lower() in (".parquet", ".pq"):
        table = read_parquet(path, code="QT-INPUT-001", what="unreadable Parquet", slack=_SIM_PARQUET_SLACK)
        out = []
        i = 0
        for batch in table.to_batches():
            try:
                rows = batch.to_pylist()
            except (ValueError, ArithmeticError, pa.ArrowException) as e:  # a date or time outside Python's range
                raise QtError("QT-INPUT-002", f"{path}[{i}..]: value outside the supported range: {e}") from e
            for row in rows:
                clean = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
                out.append(Record(clean, repr(clean), f"{path}[{i}]", file_idx, i))
                i += 1
        return out
    text = read_text(path)
    if text.lstrip()[:1] in ("{", "["):
        return read_json_records(path, file_idx)
    header, rows = read_csv_rows(text, str(path))
    out = []
    for i, r in enumerate(rows):
        row = {header[j]: (r[j] if j < len(r) else "") for j in range(len(header))}
        out.append(Record(row, ",".join(r), f"{path}:{i + 2}", file_idx, i))
    return out


def _ns(v: Any, what: str, loc: str) -> int | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, (int, Decimal)) or (isinstance(v, str) and _INT_TEXT.fullmatch(v.strip())):
        d = dec(v, what, loc)
        if d is None or d <= 0:
            return None
        return epoch_ns(v, "auto", what, loc)[0]
    return rfc3339_ns(str(v).replace("+00:00", "Z"), what, loc, default_tz="UTC")[0]


# ---- hftbacktest ------------------------------------------------------------------------------------------------


def _enum(v: Any, table: dict[int, str]) -> str | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    s = str(v).strip().lower()
    if _ENUM_INT.fullmatch(s):
        return table.get(int(s))
    return s.replace(" ", "_")


def import_hftbacktest(paths: list[str], opts: ImportOptions) -> ImportResult:
    """hftbacktest order snapshots -> canonical rows (venue `sim:hftbacktest`)."""
    b = _builder(HBT, opts)
    raw_map = opts.extra.get("order_key_map") or {}
    if not isinstance(raw_map, dict):
        raise QtError("QT-CONFIG-001", "order_key_map must be an object {hftbacktest order_id: order_key}")
    key_map: dict[str, str] = {str(k): str(v) for k, v in raw_map.items()}
    state: dict[str, dict[str, Any]] = {}
    for file_idx, p in enumerate(paths):
        for rec in _table_rows(Path(p), file_idx):
            m = rec.obj
            loc = rec.location
            if not isinstance(m, dict) or m.get("order_id") in (None, ""):
                b.skip("hftbacktest row without order_id")
                continue
            oid = text_or_none(m.get("order_id"))
            assert oid is not None
            tick = dec(m.get("tick_size"), "tick_size", loc)
            if tick is None:
                raise input_error(loc, "tick_size is required to convert price ticks")
            px = dmul(dec(m.get("price_tick"), "price_tick", loc) or Decimal(0), tick)
            qty = dec(m.get("qty"), "qty", loc)
            leaves = dec(m.get("leaves_qty"), "leaves_qty", loc)
            status = _enum(m.get("status"), _HBT_STATUS) or "none"
            tif = _enum(m.get("time_in_force"), _HBT_TIF)
            ot = _enum(m.get("order_type"), {0: "limit", 1: "market"})
            raw_side = text_or_none(m.get("side"))
            side = {"1": "buy", "-1": "sell"}.get(raw_side or "", side_of(raw_side))
            inst = text_or_none(m.get("instrument")) or (
                f"asset{text_or_none(m.get('asset_no'))}" if m.get("asset_no") not in (None, "") else "asset0"
            )
            h = b.order(
                oid=oid,
                rec=rec,
                record=True,
                qty=qty,
                qty_text=dec_text(m.get("qty")),
                px=px,
                px_text=format(px.normalize(), "f"),
                instrument=inst,
                side=side,
                order_type=ot if ot in ("limit", "market") else "other",
                tif=tif if tif in ("gtc", "gtx", "fok", "ioc") else "other",
                post_only=tif == "gtx",
            )
            given = text_or_none(m.get("order_key")) or key_map.get(oid)
            _key(b, h, given, opts, fallback=given is not None)
            exch = _ns(m.get("exch_timestamp"), "exch_timestamp", loc)
            local = _ns(m.get("local_timestamp"), "local_timestamp", loc)
            common: dict[str, Any] = {"rec": rec, "source": "sim", "precision": "ns", "stream": True}
            st = state.get(oid)
            if st is None:
                if local is not None:
                    b.event(h, "submit_sent", kind="unknown", ts_local_send=local, qty=qty, px=px, **common)
                st = {"leaves": qty, "qty": qty, "px": px, "status": "none", "n": 0}
                state[oid] = st
            ts = exch if exch is not None else local
            if ts is None:
                raise input_error(loc, "snapshot without exch_timestamp or local_timestamp")
            ex: dict[str, Any] = {**common, "ts_exchange": exch, "ts_local_recv": None if exch else local}
            if status in ("new", "partially_filled", "filled") and st["status"] == "none":
                b.event(h, "ack", kind="match_time", qty=qty, px=px, **ex)
            if (
                st["status"] not in ("none",)
                and (qty != st["qty"] or px != st["px"])
                and status
                not in (
                    "canceled",
                    "expired",
                    "rejected",
                )
            ):
                b.event(h, "amend_ack", kind="match_time", qty=qty, px=px, reason="hftbacktest modify", **ex)
                st["leaves"] = dsub(qty, dsub(st["qty"], st["leaves"])) if qty is not None else st["leaves"]
                st["qty"], st["px"] = qty, px
            if leaves is not None and st["leaves"] is not None and leaves < st["leaves"]:
                st["n"] += 1
                fpx = dmul(dec(m.get("exec_price_tick"), "exec_price_tick", loc) or Decimal(0), tick)
                b.event(
                    h,
                    "fill",
                    kind="match_time",
                    qty=dsub(st["leaves"], leaves),
                    px=fpx,
                    fill_id=f"{oid}:{st['n']}",
                    liquidity="maker" if as_bool(m.get("maker")) else "taker",
                    derived=True,
                    reason="leaves_qty differencing of hftbacktest snapshots",
                    **ex,
                )
                st["leaves"] = leaves
            if status != st["status"]:
                if status == "canceled":
                    b.event(h, "cancel_ack", kind="match_time", **ex)
                elif status == "expired":
                    b.event(h, "expire", kind="match_time", **ex)
                elif status == "rejected":
                    b.event(h, "post_only_reject" if tif == "gtx" else "reject", kind="match_time", **ex)
            st["status"] = status
    return b.finish()


# ---- NautilusTrader ---------------------------------------------------------------------------------------------

_EVENT_NAMES = {
    "orderinitialized": "init",
    "ordersubmitted": "submit_sent",
    "orderaccepted": "ack",
    "orderrejected": "reject",
    "orderdenied": "reject",
    "ordercanceled": "cancel_ack",
    "orderexpired": "expire",
    "orderupdated": "amend_ack",
    "orderfilled": "fill",
    "ordercancelrejected": "cancel_reject",
    "ordermodifyrejected": "amend_reject",
}


def _event_name(row: dict[str, Any], path: Path) -> str | None:
    t = text_or_none(row.get("type")) or text_or_none(row.get("event_type")) or text_or_none(row.get("kind"))
    if t is not None:
        key = t.replace("_", "").lower()
        if key in _EVENT_NAMES:
            return _EVENT_NAMES[key]
    stem = path.stem.replace("_", "").lower()
    for k, v in _EVENT_NAMES.items():
        if stem.startswith(k):
            return v
    return None


def _inst(v: Any) -> str | None:
    s = text_or_none(v)
    if s is None:
        return None
    return s.rsplit(".", 1)[0] if "." in s else s


def _commission(v: Any, loc: str) -> tuple[Decimal | None, str | None]:
    s = text_or_none(v)
    if s is None:
        return None, None
    parts = s.split()
    return dec(parts[0], "commission", loc), (parts[1] if len(parts) > 1 else None)


def import_nautilus(paths: list[str], opts: ImportOptions) -> ImportResult:
    """NautilusTrader backtest reports and order events -> canonical rows (venue `sim:nautilus`)."""
    b = _builder(NT, opts)
    report_fills: list[tuple[int, dict[str, Any], Record]] = []
    per_fill_orders: set[int] = set()
    for file_idx, p in enumerate(paths):
        path = Path(p)
        for rec in _table_rows(path, file_idx):
            m = rec.obj
            loc = rec.location
            if not isinstance(m, dict):
                b.skip("non-object row")
                continue
            cid = text_or_none(m.get("client_order_id"))
            voi = text_or_none(m.get("venue_order_id"))
            ev = _event_name(m, path)
            is_report_order = ev is None and "status" in m and ("quantity" in m or "filled_qty" in m)
            is_fill_row = ev == "fill" or (ev is None and "last_qty" in m)
            if not (is_report_order or is_fill_row or ev is not None):
                b.skip("unrecognised Nautilus row")
                continue
            side = side_of(m.get("side") or m.get("order_side"))
            tif_raw = text_or_none(m.get("time_in_force"))
            po = as_bool(m.get("post_only"))
            ot_raw = text_or_none(m.get("type")) if is_report_order else text_or_none(m.get("order_type"))
            record = is_report_order or ev == "init"
            qty = dec(m.get("quantity"), "quantity", loc) if record else None
            px = dec(m.get("price"), "price", loc) if record and text_or_none(m.get("price")) else None
            h = b.order(
                cid=cid,
                rec=rec,
                record=record and qty is not None,
                qty=qty,
                qty_text=dec_text(m.get("quantity")) if record else None,
                px=px,
                px_text=dec_text(m.get("price")) if px is not None else None,
                instrument=_inst(m.get("instrument_id")),
                side=side,
                order_type=_NT_TYPE.get(ot_raw.upper(), "other") if ot_raw and record else None,
                tif=("post_only" if po else _NT_TIF.get(tif_raw.upper(), "other")) if tif_raw and record else None,
                post_only=po if record else None,
                reduce_only=as_bool(m.get("reduce_only")) if record else None,
            )
            if voi is not None and b.order_field(h, "venue_order_id") is None:
                b.set_order_field(h, "venue_order_id", voi)
            _key(b, h, cid, opts, fallback=True)
            common: dict[str, Any] = {"rec": rec, "source": "sim", "precision": "ns"}
            if is_fill_row:
                ts = _ns(m.get("ts_event"), "ts_event", loc)
                comm, ccy = _commission(m.get("commission"), loc)
                liq = str(m.get("liquidity_side", "")).upper()
                b.event(
                    h,
                    "fill",
                    kind="match_time",
                    ts_exchange=ts,
                    qty=dec(m.get("last_qty"), "last_qty", loc),
                    px=dec(m.get("last_px"), "last_px", loc),
                    fill_id=text_or_none(m.get("trade_id")),
                    liquidity={"MAKER": "maker", "TAKER": "taker"}.get(liq, "unknown"),
                    fee=comm,
                    fee_ccy=text_or_none(m.get("commission_currency")) or ccy,
                    stream=True,
                    **common,
                )
                per_fill_orders.add(h)
            elif ev is not None and ev != "init":
                ts = _ns(m.get("ts_event"), "ts_event", loc)
                reason = text_or_none(m.get("reason"))
                etype = ev
                if ev == "reject" and as_bool(m.get("due_post_only")):
                    etype = "post_only_reject"
                vals: dict[str, Any] = {}
                if ev == "amend_ack":
                    vals = {"qty": dec(m.get("quantity"), "quantity", loc), "px": dec(m.get("price"), "price", loc)}
                if ev == "submit_sent":
                    b.event(h, etype, kind="unknown", ts_local_send=ts, stream=True, reason=reason, **vals, **common)
                else:
                    b.event(h, etype, kind="match_time", ts_exchange=ts, stream=True, reason=reason, **vals, **common)
            elif is_report_order:
                ts_init = _ns(m.get("ts_init"), "ts_init", loc)
                ts_last = _ns(m.get("ts_last"), "ts_last", loc)
                status = str(m.get("status", "")).upper()
                hist: dict[str, Any] = {**common, "kind": "record_time", "snapshot": True}
                if ts_init is not None and status not in ("REJECTED", "DENIED"):
                    b.event(h, "ack", ts_exchange=ts_init, reason="from orders report ts_init", **hist)
                term = {"CANCELED": "cancel_ack", "EXPIRED": "expire", "REJECTED": "reject", "DENIED": "reject"}
                if status in term and ts_last is not None:
                    b.event(h, term[status], ts_exchange=ts_last, reason=f"{status} (orders report ts_last)", **hist)
                filled = dec(m.get("filled_qty"), "filled_qty", loc)
                if filled is not None and filled > 0:
                    report_fills.append((h, m, rec))
    for h, m, rec in report_fills:
        if h in per_fill_orders:
            continue
        loc = rec.location
        ts = _ns(m.get("ts_last"), "ts_last", loc)
        b.event(
            h,
            "fill",
            rec=rec,
            source="sim",
            kind="record_time",
            precision="ns",
            ts_exchange=ts,
            qty=dec(m.get("filled_qty"), "filled_qty", loc),
            px=dec(m.get("avg_px"), "avg_px", loc),
            liquidity="unknown",
            derived=True,
            soft_px=True,
            snapshot=True,
            reason="one fill derived from the orders report (filled_qty at avg_px, ts_last)",
        )
        b.warn(f"{NT}: fills derived from order reports where no per-fill rows were given")
    return b.finish()
