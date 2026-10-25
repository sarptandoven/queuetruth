"""Coinbase Advanced Trade importer (SPEC 5.7.1; fixture-validated only).

Inputs: WebSocket `user` channel messages (JSON lines, optionally capture-wrapped), REST List Fills
(`{"fills": [...]}`) and REST List Orders (`{"orders": [...]}`, order records only).

- The `user` channel is order-level: fills are derived by differencing `cumulative_quantity` (`derived = true`,
  `fill_id` null, `liquidity = unknown`, price = delta(`filled_value`) / delta qty, null with a warning when not
  exact, fee = delta(`total_fees`) in the product's quote currency, `ts_exchange` = message `timestamp` as
  `record_time`). Derived fills are dropped (and counted) for orders that have List Fills rows.
- First update showing the order accepted (`OPEN`, `FILLED`, `CANCELLED`, `EXPIRED`, or any fill) -> ack at
  `creation_time` (record_time); an order first seen as `PENDING` is acked on that later update. Orders in the
  subscribe `snapshot` existed before the capture -> `restate` (not an entry); a drop of `leaves_quantity` without a
  fill, or a `limit_price` change -> amend_ack inferred (priority undocumented -> null); `CANCELLED` -> cancel_ack,
  `EXPIRED` -> expire, `FAILED` -> reject (post-only rejections -> post_only_reject). Order size =
  `cumulative_quantity + leaves_quantity` at first sight.
- The per-order state only moves forward (messages are taken in capture order; `sequence_num` is per connection and
  not used): an update whose `cumulative_quantity` is below the order's last seen value, or that repeats a terminal
  status (`FILLED` / `CANCELLED` / `EXPIRED` / `FAILED`) without a higher quantity, is a replayed or out-of-order
  message: ignored and counted in the warnings (no fill is derived twice, no second terminal event).
- List Fills: `entry_id` is the fill id (`trade_id` is not unique for adjusted fills), `trade_time` match_time,
  `liquidity_indicator`, `commission` (positive = cost, quote currency). `size_in_quote = true` converts the quote
  size to base by an exact division. When the quotient is not an exact decimal (10 / 67123.45) the fill quantity is
  unknown and cannot be written null (fills need a quantity; SPEC 3.9, E17/G13): that order is excluded with every
  record of it (user channel, List Fills, List Orders), counted and named in one warning, and the import continues
  (a row without `order_id` is dropped alone, also warned). `REVERSAL` -> fill_bust and `CORRECTION` ->
  fill_correct referencing the `FILL` with the same `trade_id` in the same order (kind unknown); unmatched ones warn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live._common import (
    Record,
    as_bool,
    ddiv_exact,
    dec,
    dec_text,
    dsub,
    dsum,
    rfc3339_ns,
    text_or_none,
)
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.importers._base import (
    ImportOptions,
    json_records,
    list_field,
    new_builder,
    obj_field,
    side_of,
    sub_records,
)

VENUE = "coinbase"
_TIF = {
    "GOOD_UNTIL_CANCELLED": "gtc",
    "GOOD_UNTIL_DATE_TIME": "gtd",
    "IMMEDIATE_OR_CANCEL": "ioc",
    "FILL_OR_KILL": "fok",
}
_TYPE = {"LIMIT": "limit", "MARKET": "market", "STOP": "stop", "STOP_LIMIT": "stop_limit"}


@dataclass
class _WsState:
    cum: Decimal
    leaves: Decimal
    value: Decimal
    fees: Decimal
    n_fills: int
    px: Decimal | None
    acked: bool  # an ack (or the snapshot restate) was emitted
    ended: bool = False  # a terminal status (FILLED / CANCELLED / EXPIRED / FAILED) was seen


@dataclass
class _Excluded:
    """Orders with a quote-sized fill whose base size is not an exact decimal (excluded with every record)."""

    inexact: dict[str, list[str]] = field(default_factory=dict)  # order_id -> the inexact fills
    dropped: dict[str, int] = field(default_factory=dict)  # order_id -> records dropped
    loose: list[str] = field(default_factory=list)  # inexact rows without an order_id (dropped one by one)

    def drop(self, oid: str | None) -> bool:
        """True (and counted) when the record belongs to an excluded order."""
        if oid is None or oid not in self.inexact:
            return False
        self.dropped[oid] = self.dropped.get(oid, 0) + 1
        return True


_ACK_STATUSES = ("OPEN", "FILLED", "CANCELLED", "EXPIRED")
_END_STATUSES = ("FILLED", "CANCELLED", "EXPIRED", "FAILED")
_STALE = (
    "stale user-channel order update ignored (cumulative_quantity below the order's last seen value, or a repeat "
    "after its terminal status: replayed or out-of-order message)"
)


def _norm(d: Decimal | None) -> str:
    if d is None:
        return "null"
    n = d.normalize()
    return format(n, "f")


def _quote_ccy(product: str | None) -> str | None:
    if not product or "-" not in product:
        return None
    return product.rsplit("-", 1)[1]


def _ws_fields(o: dict[str, Any]) -> dict[str, Any]:
    ot = text_or_none(o.get("order_type"))
    tif = text_or_none(o.get("time_in_force"))
    po = as_bool(o.get("post_only"))
    return {
        "instrument": text_or_none(o.get("product_id")),
        "side": side_of(o.get("order_side") or o.get("side")),
        "order_type": _TYPE.get(ot.upper(), "other") if ot else None,
        "tif": "post_only" if po else (_TIF.get(tif.upper(), "other") if tif else None),
        "post_only": po,
    }


def _ws_order(
    b: LogBuilder,
    o: dict[str, Any],
    rec: Record,
    ev_type: str,
    msg_ts: tuple[int, str],
    states: dict[int, _WsState],
    ex: _Excluded,
) -> None:
    loc = rec.location
    if ex.drop(text_or_none(o.get("order_id"))):
        return
    cum = dec(o.get("cumulative_quantity"), "cumulative_quantity", loc) or Decimal(0)
    leaves = dec(o.get("leaves_quantity"), "leaves_quantity", loc) or Decimal(0)
    value = dec(o.get("filled_value"), "filled_value", loc) or Decimal(0)
    fees = dec(o.get("total_fees"), "total_fees", loc) or Decimal(0)
    n_fills = int(dec(o.get("number_of_fills"), "number_of_fills", loc) or 0)
    px = dec(o.get("limit_price"), "limit_price", loc)
    if px is not None and px == 0:
        px = None
    size = dsum([cum, leaves])
    f = _ws_fields(o)
    h = b.order(
        oid=text_or_none(o.get("order_id")),
        cid=text_or_none(o.get("client_order_id")),
        rec=rec,
        record=True,
        qty=size,
        qty_text=format(size, "f") if text_or_none(o.get("leaves_quantity")) is None else _size_text(o, size),
        px=px,
        px_text=dec_text(o.get("limit_price")) if px is not None else None,
        **f,
    )
    st = states.get(h)
    status = str(o.get("status", "")).upper()
    if st is not None and (cum < st.cum or (st.ended and cum <= st.cum)):
        b.skip(_STALE)
        return
    ws_ts, ws_prec = msg_ts
    common: dict[str, Any] = {"rec": rec, "source": "ws", "kind": "record_time"}
    ct = text_or_none(o.get("creation_time"))
    if st is None:
        if ev_type == "snapshot":
            cts, cprec = rfc3339_ns(ct, "creation_time", loc) if ct else (ws_ts, ws_prec)
            b.event(
                h,
                "restate",
                ts_exchange=cts,
                precision=cprec,
                qty=size,
                px=px,
                cum=cum,
                leaves=leaves,
                reason="snapshot: open before capture start",
                **common,
            )
            st = _WsState(cum, leaves, value, fees, n_fills, px, True)
        else:
            st = _WsState(Decimal(0), size, Decimal(0), Decimal(0), 0, px, False)
    if not st.acked and (status in _ACK_STATUSES or cum > 0):
        # first update showing the order accepted (an order first seen as PENDING is acked here, later)
        cts, cprec = rfc3339_ns(ct, "creation_time", loc) if ct else (ws_ts, ws_prec)
        b.event(
            h,
            "ack",
            ts_exchange=cts,
            precision=cprec,
            qty=size,
            px=px,
            cum=Decimal(0),
            leaves=size,
            reason="creation_time",
            **common,
        )
        st.acked = True
    common.update(ts_exchange=ws_ts, precision=ws_prec)
    if cum > st.cum:
        dq = dsub(cum, st.cum)
        dv = dsub(value, st.value)
        fpx = ddiv_exact(dv, dq)
        merged = n_fills - st.n_fills
        reason = (
            f"derived: cumulative_quantity {_norm(st.cum)} -> {_norm(cum)}, number_of_fills {st.n_fills} -> {n_fills}"
        )
        if merged > 1:
            reason += f" ({merged} executions merged)"
        if fpx is None:
            b.warn(f"{VENUE}: order {o.get('order_id')}: derived fill price {dv}/{dq} not exact (px left null)")
            reason += "; derived px not exact -> px null"
        b.event(
            h,
            "fill",
            qty=dq,
            px=fpx,
            cum=cum,
            leaves=leaves,
            liquidity="unknown",
            fee=dsub(fees, st.fees),
            fee_ccy=_quote_ccy(text_or_none(o.get("product_id"))),
            derived=True,
            soft_px=True,
            reason=reason,
            **common,
        )
        b.warn(f"{VENUE}: fills derived from cumulative_quantity differencing (no per-fill data on the user channel)")
    elif status == "OPEN" and (leaves < st.leaves or (px is not None and st.px is not None and px != st.px)):
        parts = []
        if leaves < st.leaves:
            parts.append(f"leaves_quantity {_norm(st.leaves)} -> {_norm(leaves)} with no fill")
        if px is not None and st.px is not None and px != st.px:
            parts.append(f"limit_price {_norm(st.px)} -> {_norm(px)}")
        b.event(
            h, "amend_ack", qty=size, px=px, cum=cum, leaves=leaves, reason="inferred: " + ", ".join(parts), **common
        )
    if st.ended:
        pass  # a terminal event was already written; a late fill above is all this update adds
    elif status == "CANCELLED":
        b.event(
            h, "cancel_ack", cum=cum, leaves=Decimal(0), reason=text_or_none(o.get("cancel_reason")) or status, **common
        )
    elif status == "EXPIRED":
        b.event(h, "expire", cum=cum, leaves=Decimal(0), reason=status, **common)
    elif status == "FAILED":
        r = text_or_none(o.get("reject_reason")) or status
        po = "POST_ONLY" in r.upper()
        b.event(h, "post_only_reject" if po else "reject", cum=cum, leaves=Decimal(0), reason=r, **common)
    elif status not in ("OPEN", "FILLED") and cum <= st.cum:
        b.skip(f"user-channel status {status}")
    states[h] = _WsState(cum, leaves, value, fees, n_fills, px, st.acked, st.ended or status in _END_STATUSES)


def _size_text(o: dict[str, Any], size: Decimal) -> str:
    cum = text_or_none(o.get("cumulative_quantity")) or "0"
    leaves = text_or_none(o.get("leaves_quantity")) or "0"
    if dec(cum, "cumulative_quantity") == 0:
        return leaves
    if dec(leaves, "leaves_quantity") == 0:
        return cum
    return format(size, "f")


def _list_fill(
    b: LogBuilder,
    m: dict[str, Any],
    rec: Record,
    adjustments: list[tuple[int, dict[str, Any], Record]],
    fills_by_trade: dict[tuple[int, str], str],
    ex: _Excluded,
) -> None:
    product = text_or_none(m.get("product_id"))
    oid = text_or_none(m.get("order_id"))
    if ex.drop(oid):
        return
    h = b.order(oid=oid, rec=rec, instrument=product, side=side_of(m.get("side")))
    ttype = str(m.get("trade_type", "FILL")).upper()
    if ttype in ("REVERSAL", "CORRECTION"):
        adjustments.append((h, m, rec))
        return
    if ttype != "FILL":
        b.skip(f"List Fills trade_type {ttype}")
        return
    if not _emit_fill(b, h, m, rec, "fill", None, ex):
        return
    tid = text_or_none(m.get("trade_id"))
    fid = text_or_none(m.get("entry_id"))
    if tid is not None and fid is not None:
        fills_by_trade[(h, tid)] = fid


def _inexact_quote(m: Any) -> str | None:
    """Description of a `size_in_quote` List Fills row whose base size (quote size / price) is not an exact decimal;
    None for every other row (malformed rows are reported by the main pass)."""
    if not isinstance(m, dict) or not as_bool(m.get("size_in_quote")):
        return None
    try:
        size, price = dec(m.get("size"), "size"), dec(m.get("price"), "price")
    except QtError:
        return None
    if size is None or price is None or price == 0 or ddiv_exact(size, price) is not None:
        return None
    ccy = _quote_ccy(text_or_none(m.get("product_id")))
    return f"entry {text_or_none(m.get('entry_id'))}: {dec_text(m.get('size'))} {ccy} / {dec_text(m.get('price'))}"


def _scan_inexact(recs: list[Record]) -> _Excluded:
    """Pre-pass over the List Fills rows: orders with an inexact quote-sized fill (see the module docstring)."""
    ex = _Excluded()
    for rec in recs:
        obj = rec.obj
        if not isinstance(obj, dict) or obj.get("channel") is not None:
            continue
        if "fills" in obj:
            rows = [sub.obj for sub in sub_records(rec, "fills")]
        elif "orders" in obj or "entry_id" not in obj:
            continue
        else:
            rows = [obj]
        for m in rows:
            what = _inexact_quote(m)
            if what is None:
                continue
            oid = text_or_none(m.get("order_id"))
            if oid is not None:  # rows without an order_id are dropped one by one by `_emit_fill`
                ex.inexact.setdefault(oid, []).append(what)
    return ex


def _warn_excluded(b: LogBuilder, ex: _Excluded) -> None:
    why = "a size_in_quote fill's base size (quote size / price) is not an exact decimal"
    if ex.inexact:
        ids = sorted(ex.inexact)
        detail = "; ".join(f"{o} ({', '.join(ex.inexact[o])})" for o in ids[:20])
        b.warn(
            f"{VENUE}: {len(ids)} order(s) excluded with their {sum(ex.dropped.values())} record(s) (user channel, "
            f"List Fills, List Orders): {why}, so the filled quantity is unknown: {detail}"
        )
    if ex.loose:
        b.warn(
            f"{VENUE}: {len(ex.loose)} List Fills row(s) without order_id dropped: {why}: " + "; ".join(ex.loose[:20])
        )


def _emit_fill(
    b: LogBuilder, h: int, m: dict[str, Any], rec: Record, etype: str, ref: str | None, ex: _Excluded
) -> bool:
    """Writes a List Fills row as a fill / bust / correction; False (row dropped and counted) when its quote size
    has no exact base size."""
    loc = rec.location
    product = text_or_none(m.get("product_id"))
    price = dec(m.get("price"), "price", loc)
    size = dec(m.get("size"), "size", loc)
    reason = None
    if as_bool(m.get("size_in_quote")):
        if size is None or price is None or price == 0:
            raise QtError("QT-INPUT-004", f"{loc}: size_in_quote fill without price")
        base = ddiv_exact(size, price)
        if base is None:  # only rows without an order_id get here (orders with one are excluded up front)
            ex.loose.append(_inexact_quote(m) or loc)
            return False
        reason = f"size_in_quote: {dec_text(m.get('size'))} {_quote_ccy(product)} / {dec_text(m.get('price'))}"
        size = base
    ts, prec = rfc3339_ns(m.get("trade_time"), "trade_time", loc)
    liq = str(m.get("liquidity_indicator", "")).upper()
    if etype != "fill":
        reason = f"trade_type={str(m.get('trade_type')).upper()} trade_id={text_or_none(m.get('trade_id'))}"
    b.event(
        h,
        etype,
        rec=rec,
        source="rest",
        kind="match_time" if etype == "fill" else "unknown",
        precision=prec,
        ts_exchange=ts,
        qty=size,
        px=price,
        fill_id=text_or_none(m.get("entry_id")),
        ref_fill_id=ref,
        liquidity={"MAKER": "maker", "TAKER": "taker"}.get(liq, "unknown"),
        fee=dec(m.get("commission"), "commission", loc),
        fee_ccy=_quote_ccy(product),
        reason=reason,
    )
    return True


_CONFIG_TIF = {
    "limit_limit_gtc": ("limit", "gtc"),
    "limit_limit_gtd": ("limit", "gtd"),
    "limit_limit_fok": ("limit", "fok"),
    "sor_limit_ioc": ("limit", "ioc"),
    "market_market_ioc": ("market", "ioc"),
    "market_market_fok": ("market", "fok"),
    "stop_limit_stop_limit_gtc": ("stop_limit", "gtc"),
    "stop_limit_stop_limit_gtd": ("stop_limit", "gtd"),
}


def _list_order(b: LogBuilder, m: dict[str, Any], rec: Record, ex: _Excluded) -> None:
    loc = rec.location
    if ex.drop(text_or_none(m.get("order_id"))):
        return
    cfg = obj_field(m.get("order_configuration"), "order_configuration", loc)
    name = next(iter(cfg), None)
    body = obj_field(cfg.get(name), f"order_configuration.{name}", loc) if name else {}
    ot, tif = _CONFIG_TIF.get(name or "", ("other", "other"))
    po = as_bool((body or {}).get("post_only"))
    size_raw = (body or {}).get("base_size")
    px_raw = (body or {}).get("limit_price")
    h = b.order(
        oid=text_or_none(m.get("order_id")),
        cid=text_or_none(m.get("client_order_id")),
        rec=rec,
        record=size_raw is not None,
        qty=dec(size_raw, "base_size", loc),
        qty_text=dec_text(size_raw),
        px=dec(px_raw, "limit_price", loc),
        px_text=dec_text(px_raw),
        instrument=text_or_none(m.get("product_id")),
        side=side_of(m.get("side")),
        order_type=ot,
        tif="post_only" if po else tif,
        post_only=po,
    )
    ct = text_or_none(m.get("created_time"))
    if ct:
        ts, prec = rfc3339_ns(ct, "created_time", loc)
        b.event(
            h,
            "ack",
            rec=rec,
            source="rest",
            kind="record_time",
            precision=prec,
            ts_exchange=ts,
            snapshot=True,
            reason="from List Orders created_time",
        )
    if str(m.get("status", "")).upper() in ("CANCELLED", "EXPIRED", "FAILED"):
        b.skip("List Orders terminal status without an event time")


def import_coinbase(paths: list[str], opts: ImportOptions) -> ImportResult:
    """Coinbase `user` channel, List Fills and List Orders -> canonical rows."""
    b = new_builder(VENUE, opts)
    states: dict[int, _WsState] = {}
    adjustments: list[tuple[int, dict[str, Any], Record]] = []
    fills_by_trade: dict[tuple[int, str], str] = {}
    recs = json_records(paths)
    ex = _scan_inexact(recs)
    for rec in recs:
        obj = rec.obj
        if isinstance(obj, dict) and obj.get("channel") is not None:
            if obj.get("channel") != "user":
                b.skip(f"channel {obj.get('channel')}")
                continue
            ts_text = text_or_none(obj.get("timestamp"))
            if ts_text is None:
                raise QtError("QT-INPUT-002", f"{rec.location}: user message without timestamp")
            msg_ts = rfc3339_ns(ts_text, "timestamp", rec.location)
            for k, ev in enumerate(list_field(obj.get("events"), "events", rec.location)):
                ev = obj_field(ev, f"events[{k}]", rec.location)
                for i, o in enumerate(list_field(ev.get("orders"), f"events[{k}].orders", rec.location)):
                    sub = Record(o, rec.raw, f"{rec.location}/{i}", rec.file_idx, rec.line_idx, recv_ns=rec.recv_ns)
                    if not isinstance(o, dict):
                        b.skip("non-object record")
                        continue
                    _ws_order(b, o, sub, str(ev.get("type", "update")), msg_ts, states, ex)
            continue
        if isinstance(obj, dict) and "fills" in obj:
            for sub in sub_records(rec, "fills"):
                if not isinstance(sub.obj, dict):
                    b.skip("non-object record")
                    continue
                _list_fill(b, sub.obj, sub, adjustments, fills_by_trade, ex)
            continue
        if isinstance(obj, dict) and "orders" in obj:
            for sub in sub_records(rec, "orders"):
                if not isinstance(sub.obj, dict):
                    b.skip("non-object record")
                    continue
                _list_order(b, sub.obj, sub, ex)
            continue
        if isinstance(obj, dict) and "entry_id" in obj:
            _list_fill(b, obj, rec, adjustments, fills_by_trade, ex)
        elif isinstance(obj, dict) and "order_configuration" in obj:
            _list_order(b, obj, rec, ex)
        else:
            b.skip("unrecognised record")
    for h, m, rec in adjustments:
        tid = text_or_none(m.get("trade_id"))
        ref = fills_by_trade.get((h, tid or ""))
        if ref is None:
            b.warn(f"{VENUE}: {m.get('trade_type')} {m.get('entry_id')} matches no FILL with trade_id {tid}")
        etype = "fill_bust" if str(m.get("trade_type")).upper() == "REVERSAL" else "fill_correct"
        _emit_fill(b, h, m, rec, etype, ref, ex)
    _warn_excluded(b, ex)
    return b.finish()
