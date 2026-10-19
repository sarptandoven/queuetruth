"""Binance Spot importer (SPEC 5.7.1; fixture-validated only).

Inputs (auto-detected per record):
- user-data-stream `executionReport` messages (JSON lines; bare or inside the `{"subscriptionId", "event"}`
  envelope; optionally in a capture wrapper giving `ts_local_recv`);
- WebSocket API request/response captures (`{"dir", "local_ns", "msg"}` lines): `order.place` -> `submit_sent`,
  `order.cancel` -> `cancel_sent`, `order.amend.keepPriority` -> `amend_sent`; error responses -> `reject` /
  `post_only_reject` (code -2010 "would immediately match and take") / `cancel_reject` / `amend_reject`;
  successful responses only link ids (the user-data stream carries the lifecycle);
- REST `GET /api/v3/myTrades` (fills; no client id, joined through `orderId`) and `GET /api/v3/allOrders`
  (order records; an `ack` at `workingTime` and a terminal event at `updateTime`, both `record_time`, kept only when
  no captured event of the same type exists).

Mapping: `x` NEW -> ack, TRADE -> fill (`t` fill id, `l`/`L` qty/px, `z` cum, `q - z` leaves, `m` maker, `n`/`N`
fee), CANCELED -> cancel_ack (the order's client id is `C`; `c` is the cancel request's id), REPLACED (amend
keepPriority, qty down) -> amend_ack with `amend_keeps_priority = true`, REJECTED -> reject / post_only_reject,
EXPIRED -> expire (reason `eR`), TRADE_PREVENTION -> expire (reason `TRADE_PREVENTION:<V>:prevented_qty=<A>`).
`ts_exchange = T` (match_time; `E` record_time when `T` is absent); epoch unit detected (ms or us). LIMIT_MAKER ->
`tif = post_only`. Commission is a cost (positive). Events without an exchange time carry precision `ms` (the
venue's).
"""

from __future__ import annotations

from typing import Any

from queuetruth.live._common import (
    Record,
    as_bool,
    dec,
    dec_text,
    dsub,
    epoch_ns,
    text_or_none,
)
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.importers._base import ImportOptions, json_records, new_builder, side_of

VENUE = "binance"
_TYPES = {
    "LIMIT": ("limit", None),
    "LIMIT_MAKER": ("limit", "post_only"),
    "MARKET": ("market", None),
    "STOP_LOSS": ("stop", None),
    "STOP_LOSS_LIMIT": ("stop_limit", None),
    "TAKE_PROFIT": ("stop", None),
    "TAKE_PROFIT_LIMIT": ("stop_limit", None),
}
_TIF = {"GTC": "gtc", "IOC": "ioc", "FOK": "fok", "GTX": "gtx", "GTD": "gtd"}


def _type_tif(otype: Any, tif: Any) -> tuple[str | None, str | None]:
    if otype is None:
        return None, _TIF.get(str(tif).upper()) if tif is not None else None
    ot, forced = _TYPES.get(str(otype).upper(), ("other", None))
    t = forced or (_TIF.get(str(tif).upper(), "other") if tif is not None else None)
    return ot, t


def _ts(m: dict[str, Any], rec: Record) -> tuple[int, str, str]:
    if m.get("T") is not None:
        ns, prec = epoch_ns(m["T"], "auto", "T", rec.location)
        return ns, prec, "match_time"
    ns, prec = epoch_ns(m["E"], "auto", "E", rec.location)
    return ns, prec, "record_time"


def _order_fields(m: dict[str, Any]) -> dict[str, Any]:
    ot, tif = _type_tif(m.get("o"), m.get("f"))
    return {"instrument": text_or_none(m.get("s")), "side": side_of(m.get("S")), "order_type": ot, "tif": tif}


def _price(ot: str | None, raw: Any, what: str, loc: str) -> tuple[Any, Any]:
    p = dec(raw, what, loc)
    if p is None or (p == 0 and ot != "limit"):
        return None, None
    return p, dec_text(raw)


def _execution_report(b: LogBuilder, m: dict[str, Any], rec: Record) -> None:
    loc = rec.location
    x = str(m.get("x", "")).upper()
    oid = text_or_none(m.get("i"))
    cid = text_or_none(m.get("C")) if x == "CANCELED" and text_or_none(m.get("C")) else text_or_none(m.get("c"))
    fields = _order_fields(m)
    q = dec(m.get("q"), "q", loc)
    p, p_text = _price(fields["order_type"], m.get("p"), "p", loc)
    h = b.order(
        oid=oid, cid=cid, rec=rec, record=True, qty=q, qty_text=dec_text(m.get("q")), px=p, px_text=p_text, **fields
    )
    ts, prec, kind = _ts(m, rec)
    z = dec(m.get("z"), "z", loc)
    leaves = dsub(q, z) if q is not None and z is not None else None
    common: dict[str, Any] = {"rec": rec, "source": "ws", "kind": kind, "precision": prec, "ts_exchange": ts}
    if x == "NEW":
        b.event(h, "ack", qty=q, px=p, cum=z, leaves=leaves, **common)
    elif x == "TRADE":
        fee_ccy = text_or_none(m.get("N"))
        b.event(
            h,
            "fill",
            qty=dec(m.get("l"), "l", loc),
            px=dec(m.get("L"), "L", loc),
            cum=z,
            leaves=leaves,
            fill_id=text_or_none(m.get("t")),
            liquidity="maker" if as_bool(m.get("m")) else "taker",
            fee=dec(m.get("n"), "n", loc),
            fee_ccy=fee_ccy,
            **common,
        )
    elif x == "CANCELED":
        b.event(h, "cancel_ack", cum=z, leaves=dec(0, "0"), reason=text_or_none(m.get("X")) or "CANCELED", **common)
    elif x == "REPLACED":
        b.event(h, "amend_ack", qty=q, px=p, cum=z, leaves=leaves, keeps=True, **common)
    elif x == "REJECTED":
        r = text_or_none(m.get("r"))
        po = fields["tif"] == "post_only" and r is not None and "match" in r.lower()
        b.event(h, "post_only_reject" if po else "reject", cum=z, reason=r, **common)
    elif x == "EXPIRED":
        er = text_or_none(m.get("eR"))
        reason = er if er and er.upper() != "NONE" else text_or_none(m.get("X"))
        b.event(h, "expire", cum=z, reason=reason, **common)
    elif x == "TRADE_PREVENTION":
        reason = f"TRADE_PREVENTION:{text_or_none(m.get('V'))}:prevented_qty={dec_text(m.get('A'))}"
        b.event(h, "expire", cum=z, reason=reason, **common)
    else:
        b.skip(f"executionReport x={x}")


def _my_trade(b: LogBuilder, m: dict[str, Any], rec: Record) -> None:
    loc = rec.location
    side = "buy" if as_bool(m.get("isBuyer")) else "sell"
    h = b.order(oid=text_or_none(m.get("orderId")), rec=rec, instrument=text_or_none(m.get("symbol")), side=side)
    ts, prec = epoch_ns(m.get("time"), "auto", "time", loc)
    b.event(
        h,
        "fill",
        rec=rec,
        source="rest",
        kind="match_time",
        precision=prec,
        ts_exchange=ts,
        qty=dec(m.get("qty"), "qty", loc),
        px=dec(m.get("price"), "price", loc),
        fill_id=text_or_none(m.get("id")),
        liquidity="maker" if as_bool(m.get("isMaker")) else "taker",
        fee=dec(m.get("commission"), "commission", loc),
        fee_ccy=text_or_none(m.get("commissionAsset")),
    )


def _rest_order(b: LogBuilder, m: dict[str, Any], rec: Record) -> None:
    loc = rec.location
    ot, tif = _type_tif(m.get("type"), m.get("timeInForce"))
    p, p_text = _price(ot, m.get("price"), "price", loc)
    h = b.order(
        oid=text_or_none(m.get("orderId")),
        cid=text_or_none(m.get("clientOrderId")),
        rec=rec,
        record=True,
        qty=dec(m.get("origQty"), "origQty", loc),
        qty_text=dec_text(m.get("origQty")),
        px=p,
        px_text=p_text,
        instrument=text_or_none(m.get("symbol")),
        side=side_of(m.get("side")),
        order_type=ot,
        tif=tif,
    )
    common: dict[str, Any] = {"rec": rec, "source": "rest", "kind": "record_time", "snapshot": True}
    wt = m.get("workingTime")
    if wt is not None and dec(wt, "workingTime", loc) not in (None, 0) and int(dec(wt, "workingTime", loc) or 0) > 0:
        ts, prec = epoch_ns(wt, "auto", "workingTime", loc)
        b.event(h, "ack", ts_exchange=ts, precision=prec, reason="from allOrders.workingTime", **common)
    status = str(m.get("status", "")).upper()
    term = {"CANCELED": "cancel_ack", "EXPIRED": "expire", "EXPIRED_IN_MATCH": "expire", "REJECTED": "reject"}
    if status in term and m.get("updateTime") is not None:
        ts, prec = epoch_ns(m["updateTime"], "auto", "updateTime", loc)
        b.event(h, term[status], ts_exchange=ts, precision=prec, reason=f"{status} (allOrders.updateTime)", **common)


def _ws_api(b: LogBuilder, m: dict[str, Any], rec: Record, pending: dict[str, tuple[str, dict[str, Any]]]) -> None:
    loc = rec.location
    if rec.direction == "out":
        method = str(m.get("method", ""))
        params = m.get("params") or {}
        req_id = text_or_none(m.get("id"))
        if req_id is not None:
            pending[req_id] = (method, params)
        common: dict[str, Any] = {"rec": rec, "source": "ws", "kind": "unknown", "precision": "ms"}
        if method == "order.place":
            ot, tif = _type_tif(params.get("type"), params.get("timeInForce"))
            p, p_text = _price(ot, params.get("price"), "price", loc)
            q = dec(params.get("quantity"), "quantity", loc)
            h = b.order(
                cid=text_or_none(params.get("newClientOrderId")),
                rec=rec,
                record=True,
                qty=q,
                qty_text=dec_text(params.get("quantity")),
                px=p,
                px_text=p_text,
                instrument=text_or_none(params.get("symbol")),
                side=side_of(params.get("side")),
                order_type=ot,
                tif=tif,
            )
            b.event(h, "submit_sent", qty=q, px=p, **common)
        elif method in ("order.cancel", "order.amend.keepPriority"):
            h = b.order(
                oid=text_or_none(params.get("orderId")),
                cid=text_or_none(params.get("origClientOrderId")),
                rec=rec,
                instrument=text_or_none(params.get("symbol")),
            )
            if method == "order.cancel":
                b.event(h, "cancel_sent", **common)
            else:
                b.event(h, "amend_sent", qty=dec(params.get("newQty"), "newQty", loc), **common)
        else:
            b.skip(f"ws-api request {method}")
        return
    req_id = text_or_none(m.get("id"))
    method, params = pending.get(req_id or "", ("", {}))
    status = int(dec(m.get("status"), "status", loc) or 0)
    if status < 400:
        res = m.get("result") or {}
        if method == "order.place" and isinstance(res, dict):
            b.order(oid=text_or_none(res.get("orderId")), cid=text_or_none(res.get("clientOrderId")), rec=rec)
        b.skip("ws-api success response (lifecycle comes from the user data stream)")
        return
    err = m.get("error") or {}
    reason = f"{text_or_none(err.get('code'))} {text_or_none(err.get('msg'))}"
    common = {"rec": rec, "source": "ws", "kind": "unknown", "precision": "ms"}
    if method == "order.place":
        h = b.order(cid=text_or_none(params.get("newClientOrderId")), rec=rec)
        po = "match and take" in reason.lower() or text_or_none(err.get("code")) == "-2010"
        b.event(h, "post_only_reject" if po else "reject", reason=reason, **common)
    elif method in ("order.cancel", "order.amend.keepPriority"):
        h = b.order(oid=text_or_none(params.get("orderId")), cid=text_or_none(params.get("origClientOrderId")), rec=rec)
        b.event(h, "cancel_reject" if method == "order.cancel" else "amend_reject", reason=reason, **common)
    else:
        b.skip(f"ws-api error response to {method or 'unknown request'}")


def import_binance(paths: list[str], opts: ImportOptions) -> ImportResult:
    """Binance user-data stream, WebSocket API captures, REST myTrades / allOrders -> canonical rows."""
    b = new_builder(VENUE, opts)
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    for rec in json_records(paths):
        m = rec.obj
        if isinstance(m, dict) and isinstance(m.get("event"), dict) and "subscriptionId" in m:
            m = m["event"]
        if not isinstance(m, dict):
            b.skip("non-object record")
            continue
        if m.get("e") == "executionReport":
            _execution_report(b, m, rec)
        elif rec.direction == "out" or ("status" in m and "id" in m and ("error" in m or "result" in m)):
            _ws_api(b, m, rec, pending)
        elif "isBuyer" in m and "orderId" in m:
            _my_trade(b, m, rec)
        elif "origQty" in m and "orderId" in m:
            _rest_order(b, m, rec)
        else:
            b.skip(f"record type {m.get('e') or 'unknown'}")
    return b.finish()
