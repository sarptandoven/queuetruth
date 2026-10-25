"""Hyperliquid importer (SPEC 5.7.1; fixture-validated only).

Inputs: WebSocket `userFills` and `orderUpdates` channel messages (JSON lines, optionally capture-wrapped) and the
info endpoint responses `userFills` / `userFillsByTime` (array of fills) and `historicalOrders` (array of
`{order, status, statusTimestamp}` with `orderType` and `tif`).

- Fills: `fill_id = "<time>:<coin>:<tid>"` (`tid` is unique only with block time and coin), `time` ms match_time,
  `crossed` true -> taker, `fee` (negative = rebate, kept; positive = cost), `feeToken`. A fills response of exactly
  2,000 rows (the documented cap) warns that older fills may be missing.
- Orders: `origSz` is the order size (`sz` is the remaining size); `cloid` is the client order id. `orderUpdates`
  carry no tif: `tif = other` unless the status implies it (`badAloPxRejected` -> post_only, `iocCancelRejected` ->
  ioc); `historicalOrders` give `Gtc`/`Alo`/`Ioc`. First `open` -> ack (statusTimestamp, record_time); `canceled`
  -> cancel_ack; a cancel paired in the same message with an open order sharing the `cloid` is a modify, which
  Hyperliquid executes as cancel + new order: it stays one order (one order_key per client-order chain)
  with an amend_ack (new oid in the reason, priority lost). The pairing is resolved per message before any of its
  updates is processed (the new oid's `open` may come before the old oid's `canceled`): the replaced order is
  registered from its `canceled` update, the new oid is routed to it, and the new oid's `open` adds no ack (the
  amend_ack records it); `badAloPxRejected` -> post_only_reject, other `*Rejected` -> reject; venue-initiated
  `*Canceled` / `scheduledCancel` -> expire; `filled` -> no event.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from queuetruth.live._common import Record, as_bool, dec, dec_text, dsub, epoch_ns, text_or_none
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.importers._base import ImportOptions, json_records, new_builder, obj_field, side_of, sub_records

VENUE = "hyperliquid"
FILLS_CAP = 2000
_TIF = {"GTC": "gtc", "ALO": "post_only", "IOC": "ioc", "FRONTENDMARKET": "ioc"}


def _fill(b: LogBuilder, f: dict[str, Any], rec: Record, source: str) -> None:
    loc = rec.location
    coin = text_or_none(f.get("coin"))
    h = b.order(
        oid=text_or_none(f.get("oid")),
        cid=text_or_none(f.get("cloid")),
        rec=rec,
        instrument=coin,
        side=side_of(f.get("side")),
    )
    ts, prec = epoch_ns(f.get("time"), "ms", "time", loc)
    b.event(
        h,
        "fill",
        rec=rec,
        source=source,
        kind="match_time",
        precision=prec,
        ts_exchange=ts,
        qty=dec(f.get("sz"), "sz", loc),
        px=dec(f.get("px"), "px", loc),
        fill_id=f"{text_or_none(f.get('time'))}:{coin}:{text_or_none(f.get('tid'))}",
        liquidity="taker" if as_bool(f.get("crossed")) else "maker",
        fee=dec(f.get("fee"), "fee", loc),
        fee_ccy=text_or_none(f.get("feeToken")),
    )


def _status_event(status: str) -> tuple[str, str] | None:
    if status == "canceled":
        return "cancel_ack", "canceled"
    if status == "badAloPxRejected":
        return "post_only_reject", status
    if status == "rejected" or status.endswith("Rejected"):
        return "reject", status
    if status.endswith("Canceled") or status == "scheduledCancel":
        return "expire", status
    return None


def _register(
    b: LogBuilder, u: dict[str, Any], rec: Record
) -> tuple[int, Decimal | None, Decimal | None, Decimal | None]:
    """Finds or creates the order of an order-status record (merging its static fields); returns the handle, the
    order size (`origSz`), the remaining size (`sz`) and the limit price."""
    loc = rec.location
    o = obj_field(u.get("order"), "order", loc)
    status = str(u.get("status", ""))
    oid = text_or_none(o.get("oid"))
    tif_raw = text_or_none(o.get("tif"))
    tif = _TIF.get(tif_raw.upper(), "other") if tif_raw else None
    if tif is None:
        tif = {"badAloPxRejected": "post_only", "iocCancelRejected": "ioc"}.get(status)
    ot_raw = text_or_none(o.get("orderType"))
    orig = o.get("origSz") if o.get("origSz") is not None else o.get("sz")
    size = dec(orig, "origSz", loc)
    remaining = dec(o.get("sz"), "sz", loc)
    px = dec(o.get("limitPx"), "limitPx", loc)
    h = b.order(
        oid=oid,
        cid=text_or_none(o.get("cloid")),
        rec=rec,
        record=True,
        qty=size,
        qty_text=dec_text(orig),
        px=px,
        px_text=dec_text(o.get("limitPx")),
        instrument=text_or_none(o.get("coin")),
        side=side_of(o.get("side")),
        order_type=("limit" if ot_raw.lower() == "limit" else "other") if ot_raw else "limit",
        tif=tif,
        post_only=tif == "post_only" if tif is not None else None,
        reduce_only=as_bool(o.get("reduceOnly")),
    )
    return h, size, remaining, px


def _order_update(
    b: LogBuilder,
    u: dict[str, Any],
    rec: Record,
    source: str,
    acked: set[int],
    modified: dict[str, dict[str, Any]],
    new_oids: frozenset[str] = frozenset(),
) -> None:
    loc = rec.location
    o = obj_field(u.get("order"), "order", loc)
    status = str(u.get("status", ""))
    oid = text_or_none(o.get("oid"))
    h, size, remaining, px = _register(b, u, rec)
    snapshot = source == "rest"
    cum = dsub(size, remaining) if size is not None and remaining is not None and not snapshot else None
    ts, prec = epoch_ns(u.get("statusTimestamp"), "ms", "statusTimestamp", loc)
    common: dict[str, Any] = {
        "rec": rec,
        "source": source,
        "kind": "record_time",
        "precision": prec,
        "snapshot": snapshot,
    }
    ev = _status_event(status)
    if snapshot and (ev is None or ev[0] not in ("reject", "post_only_reject")):
        ots, oprec = epoch_ns(o.get("timestamp"), "ms", "timestamp", loc)
        b.event(
            h,
            "ack",
            ts_exchange=ots,
            qty=size,
            px=px,
            reason="from historicalOrders order.timestamp",
            **{**common, "precision": oprec},
        )
    if status == "open":
        if h not in acked and not snapshot and oid not in new_oids:
            b.event(h, "ack", ts_exchange=ts, qty=size, px=px, cum=cum or Decimal(0), leaves=remaining, **common)
        acked.add(h)
    elif status == "canceled" and oid in modified:
        new = modified[oid]
        new_oid = str(new.get("oid"))
        b.alias_oid(h, new_oid)
        new_size = new.get("origSz") if new.get("origSz") is not None else new.get("sz")
        b.event(
            h,
            "amend_ack",
            ts_exchange=ts,
            cum=cum,
            qty=dec(new_size, "origSz", loc),
            px=dec(new.get("limitPx"), "limitPx", loc),
            keeps=False,
            reason=f"modify: venue cancelled oid {oid} and placed oid {new_oid} (same cloid)",
            **common,
        )
    elif ev is not None:
        b.event(h, ev[0], ts_exchange=ts, cum=cum, reason=ev[1], **common)
    elif status != "filled":
        b.skip(f"order status {status}")


def import_hyperliquid(paths: list[str], opts: ImportOptions) -> ImportResult:
    """Hyperliquid userFills / orderUpdates and info userFills / historicalOrders -> canonical rows."""
    b = new_builder(VENUE, opts)
    acked: set[int] = set()
    rest_fill_counts: dict[int, int] = {}
    for rec in json_records(paths):
        obj = rec.obj
        if isinstance(obj, dict) and "channel" in obj:
            ch = obj.get("channel")
            data = obj.get("data")
            if ch == "userFills" and isinstance(data, dict):
                for sub in sub_records(
                    Record(data, rec.raw, rec.location, rec.file_idx, rec.line_idx, recv_ns=rec.recv_ns), "fills"
                ):
                    if isinstance(sub.obj, dict):
                        _fill(b, sub.obj, sub, "ws")
                    else:
                        b.skip("non-object record")
            elif ch == "orderUpdates" and isinstance(data, list):
                modified = _modifies(data)
                subs = [
                    Record(u, rec.raw, f"{rec.location}/{i}", rec.file_idx, rec.line_idx, recv_ns=rec.recv_ns)
                    for i, u in enumerate(data)
                ]
                new_oids = _prealias(b, data, subs, modified)
                for u, sub in zip(data, subs, strict=True):
                    if isinstance(u, dict):
                        _order_update(b, u, sub, "ws", acked, modified, new_oids)
                    else:
                        b.skip("non-object record")
            else:
                b.skip(f"channel {ch}")
            continue
        if isinstance(obj, dict) and "tid" in obj and "coin" in obj:
            rest_fill_counts[rec.file_idx] = rest_fill_counts.get(rec.file_idx, 0) + 1
            _fill(b, obj, rec, "rest")
        elif isinstance(obj, dict) and isinstance(obj.get("order"), dict) and "status" in obj:
            _order_update(b, obj, rec, "rest", acked, {})
        else:
            b.skip("unrecognised record")
    for idx in sorted(rest_fill_counts):
        if rest_fill_counts[idx] == FILLS_CAP:
            b.warn(
                f"{VENUE}: input {paths[idx]} holds exactly {FILLS_CAP} fills (the documented response cap): older "
                "fills may be missing"
            )
    return b.finish()


def _modifies(updates: list[Any]) -> dict[str, dict[str, Any]]:
    """Canceled oid -> the new order of a cancel/open pair sharing a cloid within one orderUpdates message."""
    opened: dict[str, dict[str, Any]] = {}
    for u in updates:
        o = _order_obj(u)
        if isinstance(u, dict) and u.get("status") == "open" and o.get("cloid") is not None:
            opened[str(o["cloid"])] = o
    out: dict[str, dict[str, Any]] = {}
    for u in updates:
        o = _order_obj(u)
        if isinstance(u, dict) and u.get("status") == "canceled" and o.get("cloid") is not None:
            new = opened.get(str(o["cloid"]))
            if new is not None and str(new.get("oid")) != str(o.get("oid")):
                out[str(o.get("oid"))] = new
    return out


def _order_obj(u: Any) -> dict[str, Any]:
    """The `order` object of an update (`{}` when the update or its order is not an object: the update itself is
    rejected or skipped when it is processed)."""
    o = u.get("order") if isinstance(u, dict) else None
    return o if isinstance(o, dict) else {}


def _prealias(
    b: LogBuilder, updates: list[Any], subs: list[Record], modified: dict[str, dict[str, Any]]
) -> frozenset[str]:
    """Resolves the modify pairs of one orderUpdates message before any of its updates is processed: registers each
    replaced order from its `canceled` update and routes the new oid to it, so a new-oid `open` listed before the
    old oid's `canceled` joins the same order instead of starting a second one (one order per client-order chain).
    Returns the new oids."""
    new_oids: set[str] = set()
    for u, sub in zip(updates, subs, strict=True):
        if not isinstance(u, dict) or u.get("status") != "canceled":
            continue
        old = text_or_none(_order_obj(u).get("oid"))
        if old is None or old not in modified:
            continue
        h, _, _, _ = _register(b, u, sub)
        new_oid = str(modified[old].get("oid"))
        b.alias_oid(h, new_oid)
        new_oids.add(new_oid)
    return frozenset(new_oids)
