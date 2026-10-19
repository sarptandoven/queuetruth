"""Bybit v5 importer (SPEC 5.7.1; fixture-validated only).

Inputs: private WebSocket `order` and `execution` topic messages (JSON lines, optionally capture-wrapped) and REST
`/v5/execution/list` and `/v5/order/history` responses (envelope `result.list`, or bare lists).

- Fills come only from executions (`execType = Trade`; other exec types are counted and skipped): `execId` fill id
  (duplicates dropped and counted), `execTime` ms match_time, `isMaker`, `execFee` (Bybit sign: positive = cost,
  negative = rebate), `leavesQty`.
- Order-topic messages are diffed against the previous state of the same order: first `New` -> ack; a price or qty
  change while working -> amend_ack inferred (`amend_keeps_priority = null`, Bybit documents nothing);
  `Cancelled`/`PartiallyFilledCanceled` -> post_only_reject (`EC_PostOnlyWillTakeLiquidity`), cancel_ack
  (`CancelByUser`), else expire (exchange-initiated: SMP, IOC/FOK remainder, ...); `Rejected` -> reject;
  `Deactivated` -> done; a repeated `Filled` carrying a cancel rejection -> cancel_reject (the documented double
  `Filled`), an identical repeat is dropped and counted. Order-topic times (`updatedTime`) are `record_time`.
- REST order history: an `ack` at `createdTime` (unless rejected) and the terminal event at `updatedTime`, both
  `record_time`, used only when no captured event of the same type exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from queuetruth.live._common import Record, as_bool, dec, dec_text, epoch_ns, text_or_none
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.importers._base import ImportOptions, json_records, new_builder, side_of, sub_records

VENUE = "bybit"
_TIF = {"GTC": "gtc", "IOC": "ioc", "FOK": "fok", "POSTONLY": "post_only", "RPI": "post_only"}
_TYPE = {"LIMIT": "limit", "MARKET": "market"}
_PO_REJECT = "EC_PostOnlyWillTakeLiquidity"


@dataclass
class _State:
    price: Decimal | None
    qty: Decimal | None
    status: str
    acked: bool


def _fields(m: dict[str, Any]) -> dict[str, Any]:
    tif = text_or_none(m.get("timeInForce"))
    ot = text_or_none(m.get("orderType"))
    return {
        "instrument": text_or_none(m.get("symbol")),
        "side": side_of(m.get("side")),
        "order_type": _TYPE.get(ot.upper(), "other") if ot else None,
        "tif": _TIF.get(tif.upper(), "other") if tif else None,
        "reduce_only": as_bool(m.get("reduceOnly")),
    }


def _register(b: LogBuilder, m: dict[str, Any], rec: Record, record: bool) -> int:
    loc = rec.location
    f = _fields(m)
    px = dec(m.get("price"), "price", loc)
    if px is not None and px == 0 and f["order_type"] != "limit":
        px = None
    return b.order(
        oid=text_or_none(m.get("orderId")),
        cid=text_or_none(m.get("orderLinkId")),
        rec=rec,
        record=record,
        qty=dec(m.get("qty"), "qty", loc),
        qty_text=dec_text(m.get("qty")),
        px=px,
        px_text=dec_text(m.get("price")) if px is not None else None,
        **f,
    )


def _terminal(m: dict[str, Any]) -> tuple[str, str] | None:
    status = str(m.get("orderStatus", ""))
    reject = text_or_none(m.get("rejectReason")) or ""
    cancel_type = text_or_none(m.get("cancelType")) or "UNKNOWN"
    tif = (text_or_none(m.get("timeInForce")) or "").upper()
    if status in ("Cancelled", "PartiallyFilledCanceled"):
        if reject == _PO_REJECT:
            return "post_only_reject", reject
        if cancel_type == "CancelByUser":
            return "cancel_ack", cancel_type
        if cancel_type == "UNKNOWN":
            return "expire", f"{status} tif={tif} cancelType={cancel_type}"
        return "expire", cancel_type
    if status == "Rejected":
        return ("post_only_reject" if reject == _PO_REJECT else "reject"), reject or status
    if status == "Deactivated":
        return "done", status
    return None


def _ws_order(b: LogBuilder, m: dict[str, Any], rec: Record, states: dict[int, _State]) -> None:
    loc = rec.location
    h = _register(b, m, rec, record=True)
    h = b.find_order(oid=text_or_none(m.get("orderId")), cid=text_or_none(m.get("orderLinkId"))) or h
    ts, prec = epoch_ns(m.get("updatedTime"), "ms", "updatedTime", loc)
    px, qty = dec(m.get("price"), "price", loc), dec(m.get("qty"), "qty", loc)
    common: dict[str, Any] = {
        "rec": rec,
        "source": "ws",
        "kind": "record_time",
        "precision": prec,
        "ts_exchange": ts,
        "cum": dec(m.get("cumExecQty"), "cumExecQty", loc),
        "leaves": dec(m.get("leavesQty"), "leavesQty", loc),
    }
    status = str(m.get("orderStatus", ""))
    st = states.get(h)
    if status == "Filled":
        if st is not None and st.status == "Filled":
            cancel_type = text_or_none(m.get("cancelType")) or "UNKNOWN"
            reject = text_or_none(m.get("rejectReason")) or "EC_NoError"
            if cancel_type != "UNKNOWN" or reject != "EC_NoError":
                b.event(h, "cancel_reject", reason=f"{reject} (second orderStatus=Filled)", **common)
            else:
                b.dropped_duplicate()
        states[h] = _State(px, qty, status, True if st is None else st.acked)
        return
    term = _terminal(m)
    if term is not None:
        b.event(h, term[0], reason=term[1], **common)
    elif status == "New" and (st is None or not st.acked):
        b.event(h, "ack", qty=qty, px=px, **common)
    elif status in ("New", "PartiallyFilled") and st is not None and (st.price, st.qty) != (px, qty):
        changes = []
        if st.price != px:
            changes.append(f"price {dec_text(st.price)} -> {dec_text(m.get('price'))}")
        if st.qty != qty:
            changes.append(f"qty {dec_text(st.qty)} -> {dec_text(m.get('qty'))}")
        b.event(h, "amend_ack", qty=qty, px=px, reason="inferred: " + ", ".join(changes), **common)
    elif status not in ("New", "PartiallyFilled"):
        b.skip(f"order status {status}")
    acked = status in ("New", "PartiallyFilled") or (st is not None and st.acked)
    states[h] = _State(px, qty, status, acked)


def _execution(b: LogBuilder, m: dict[str, Any], rec: Record, source: str) -> None:
    loc = rec.location
    if str(m.get("execType", "Trade")) != "Trade":
        b.skip(f"execType {m.get('execType')}")
        return
    f = _fields(m)
    h = b.order(
        oid=text_or_none(m.get("orderId")),
        cid=text_or_none(m.get("orderLinkId")),
        rec=rec,
        instrument=f["instrument"],
        side=f["side"],
        order_type=f["order_type"],
    )
    ts, prec = epoch_ns(m.get("execTime"), "ms", "execTime", loc)
    b.event(
        h,
        "fill",
        rec=rec,
        source=source,
        kind="match_time",
        precision=prec,
        ts_exchange=ts,
        qty=dec(m.get("execQty"), "execQty", loc),
        px=dec(m.get("execPrice"), "execPrice", loc),
        leaves=dec(m.get("leavesQty"), "leavesQty", loc),
        fill_id=text_or_none(m.get("execId")),
        liquidity="maker" if as_bool(m.get("isMaker")) else "taker",
        fee=dec(m.get("execFee"), "execFee", loc),
        fee_ccy=text_or_none(m.get("feeCurrency")),
    )


def _rest_order(b: LogBuilder, m: dict[str, Any], rec: Record) -> None:
    loc = rec.location
    h = _register(b, m, rec, record=True)
    term = _terminal(m)
    common: dict[str, Any] = {"rec": rec, "source": "rest", "kind": "record_time", "snapshot": True}
    if term is None or term[0] not in ("post_only_reject", "reject"):
        ts, prec = epoch_ns(m.get("createdTime"), "ms", "createdTime", loc)
        b.event(h, "ack", ts_exchange=ts, precision=prec, reason="from order/history createdTime", **common)
    if term is not None:
        ts, prec = epoch_ns(m.get("updatedTime"), "ms", "updatedTime", loc)
        b.event(h, term[0], ts_exchange=ts, precision=prec, reason=term[1], **common)


def import_bybit(paths: list[str], opts: ImportOptions) -> ImportResult:
    """Bybit WebSocket order/execution topics and REST execution list / order history -> canonical rows."""
    b = new_builder(VENUE, opts)
    states: dict[int, _State] = {}
    for rec in json_records(paths):
        obj = rec.obj
        if isinstance(obj, dict) and "topic" in obj:
            topic = str(obj.get("topic", "")).split(".")[0]
            if topic not in ("order", "execution"):
                b.skip(f"topic {topic}")
                continue
            for sub in sub_records(rec, "data"):
                if not isinstance(sub.obj, dict):
                    b.skip("non-object record")
                elif topic == "order":
                    _ws_order(b, sub.obj, sub, states)
                else:
                    _execution(b, sub.obj, sub, "ws")
            continue
        if isinstance(obj, dict) and "op" in obj:
            b.skip(f"control message {obj.get('op')}")
            continue
        for sub in sub_records(rec, "result", "list"):
            m = sub.obj
            if not isinstance(m, dict):
                b.skip("non-object record")
            elif "execId" in m:
                _execution(b, m, sub, "rest")
            elif "orderStatus" in m:
                _rest_order(b, m, sub)
            else:
                b.skip("unrecognised record")
    return b.finish()
