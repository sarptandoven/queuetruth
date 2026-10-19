"""Kraken WebSocket v2 `executions` importer (SPEC 5.7.1; fixture-validated only).

Inputs: `executions` channel messages (JSON lines, optionally capture-wrapped). Numbers keep their JSON token text
(no binary floats). Updates after `pending_new` carry only changed fields, so order attributes come from the
`pending_new` / snapshot record.

- `pending_new` -> order record only (counted, no canonical event); `new` -> ack; subscribe `snapshot` orders
  existed before the capture -> `restate` (not an entry).
- `trade` (and a `filled` row that carries `exec_id`) -> fill: `exec_id` fill id, `last_qty`/`last_price`,
  `liquidity_ind` m/t, `fees[]` summed when every entry has the same asset (else fee null with a warning),
  `timestamp` RFC 3339 match_time. Status-only `filled` rows are counted, no event.
- `amended` -> amend_ack with `amend_keeps_priority` = true for a qty decrease at the same price, false for a price
  change or qty increase (Kraken amend semantics); `restated` (engine-initiated) -> restate.
- `canceled`: a post-only order cancelled before `new` -> post_only_reject; a user-requested cancel (reason
  mentioning "user", or no reason) -> cancel_ack; any other reason -> expire. `expired` -> expire.
- `iceberg_refill`, `status` -> counted, no event. GTD -> `gtd` (schema 1.1, SPEC 3.11 #17; v0.1: `other`).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from queuetruth.live._common import Record, as_bool, dec, dec_text, dsub, dsum, rfc3339_ns, text_or_none
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.importers._base import ImportOptions, json_records, new_builder, side_of

VENUE = "kraken"
_TIF = {"GTC": "gtc", "IOC": "ioc", "GTD": "gtd", "FOK": "fok"}
_TYPE = {
    "limit": "limit",
    "market": "market",
    "stop-loss": "stop",
    "stop-loss-limit": "stop_limit",
    "take-profit": "stop",
    "take-profit-limit": "stop_limit",
}


@dataclass
class _St:
    qty: Decimal | None
    qty_text: str | None
    px: Decimal | None
    px_text: str | None
    post_only: bool
    acked: bool


def _fee(b: LogBuilder, m: dict[str, Any], loc: str) -> tuple[Decimal | None, str | None]:
    fees = m.get("fees")
    if not isinstance(fees, list) or not fees:
        return None, None
    assets = {text_or_none(f.get("asset")) for f in fees if isinstance(f, dict)}
    if len(assets) != 1:
        b.warn(
            f"{VENUE}: fill {m.get('exec_id')} has fees in several assets {sorted(map(str, assets))} (fee left null)"
        )
        return None, None
    total = dsum([dec(f.get("qty"), "fees.qty", loc) or Decimal(0) for f in fees])
    return total, assets.pop()


def _item(b: LogBuilder, m: dict[str, Any], rec: Record, msg_type: str, states: dict[int, _St]) -> None:
    loc = rec.location
    etype = str(m.get("exec_type", ""))
    oid = text_or_none(m.get("order_id"))
    cid = text_or_none(m.get("cl_ord_id"))
    tif_raw = text_or_none(m.get("time_in_force"))
    po = as_bool(m.get("post_only"))
    qty, px = dec(m.get("order_qty"), "order_qty", loc), dec(m.get("limit_price"), "limit_price", loc)
    has_order_fields = m.get("order_qty") is not None and m.get("side") is not None
    ot_raw = text_or_none(m.get("order_type"))
    h = b.order(
        oid=oid,
        cid=cid,
        rec=rec,
        record=has_order_fields and etype != "trade",
        qty=qty if has_order_fields else None,
        qty_text=dec_text(m.get("order_qty")) if has_order_fields else None,
        px=px if has_order_fields and px is not None and (px != 0 or ot_raw == "limit") else None,
        px_text=dec_text(m.get("limit_price")) if has_order_fields and px is not None else None,
        instrument=text_or_none(m.get("symbol")),
        side=side_of(m.get("side")),
        order_type=_TYPE.get(ot_raw.lower(), "other") if ot_raw else None,
        tif=("post_only" if po else _TIF.get(tif_raw.upper(), "other")) if tif_raw else None,
        post_only=po,
    )
    st = states.get(h)
    if st is None:
        st = _St(qty, dec_text(m.get("order_qty")), px, dec_text(m.get("limit_price")), bool(po), False)
        states[h] = st
    ts, prec = rfc3339_ns(m.get("timestamp"), "timestamp", loc)
    common: dict[str, Any] = {"rec": rec, "source": "ws", "kind": "match_time", "precision": prec, "ts_exchange": ts}
    cum = dec(m.get("cum_qty"), "cum_qty", loc)
    if msg_type == "snapshot":
        c = cum or Decimal(0)
        b.event(
            h,
            "restate",
            qty=st.qty,
            px=st.px,
            cum=c,
            leaves=dsub(st.qty, c) if st.qty is not None else None,
            reason="snapshot: open before capture start",
            **common,
        )
        st.acked = True
        return
    if etype == "pending_new":
        b.skip("pending_new (order received, not yet live)")
    elif etype == "new":
        b.event(h, "ack", qty=st.qty, px=st.px, cum=cum, **common)
        st.acked = True
    elif etype in ("trade", "filled") and m.get("exec_id") is not None:
        fee, fee_ccy = _fee(b, m, loc)
        liq = str(m.get("liquidity_ind", "")).lower()
        b.event(
            h,
            "fill",
            qty=dec(m.get("last_qty"), "last_qty", loc),
            px=dec(m.get("last_price"), "last_price", loc),
            cum=cum,
            fill_id=text_or_none(m.get("exec_id")),
            liquidity={"m": "maker", "t": "taker"}.get(liq, "unknown"),
            fee=fee,
            fee_ccy=fee_ccy,
            **common,
        )
    elif etype == "filled":
        b.skip("filled status row without exec_id")
    elif etype in ("amended", "restated"):
        new_q = qty if qty is not None else st.qty
        new_p = px if px is not None else st.px
        if etype == "restated":
            reason = f"restated: {text_or_none(m.get('reason'))}"
            b.event(h, "restate", qty=new_q, px=new_p, cum=cum, reason=reason, **common)
        else:
            if new_p != st.px:
                keeps = False
                reason = (
                    f"amended: limit_price {st.px_text} -> {dec_text(m.get('limit_price'))} "
                    "(documented: back of new price queue)"
                )
            elif new_q is not None and st.qty is not None and new_q < st.qty:
                keeps = True
                reason = (
                    f"amended: order_qty {st.qty_text} -> {dec_text(m.get('order_qty'))} "
                    "(documented: qty down keeps queue position)"
                )
            else:
                keeps = False
                reason = f"amended: order_qty {st.qty_text} -> {dec_text(m.get('order_qty'))} (qty up loses priority)"
            b.event(h, "amend_ack", qty=new_q, px=new_p, cum=cum, keeps=keeps, reason=reason, **common)
        if qty is not None:
            st.qty, st.qty_text = qty, dec_text(m.get("order_qty"))
        if px is not None:
            st.px, st.px_text = px, dec_text(m.get("limit_price"))
    elif etype == "canceled":
        reason = text_or_none(m.get("reason"))
        if st.post_only and not st.acked:
            b.event(h, "post_only_reject", cum=cum, reason=reason, **common)
        elif reason is None or "user" in reason.lower():
            b.event(h, "cancel_ack", cum=cum, reason=reason, **common)
        else:
            b.event(h, "expire", cum=cum, reason=reason, **common)
    elif etype == "expired":
        b.event(h, "expire", cum=cum, reason=text_or_none(m.get("reason")) or "expired", **common)
    else:
        b.skip(f"exec_type {etype}")


def import_kraken(paths: list[str], opts: ImportOptions) -> ImportResult:
    """Kraken WebSocket v2 executions -> canonical rows."""
    b = new_builder(VENUE, opts)
    states: dict[int, _St] = {}
    for rec in json_records(paths):
        obj = rec.obj
        if not isinstance(obj, dict) or obj.get("channel") != "executions":
            b.skip(f"message {obj.get('channel') or obj.get('method') if isinstance(obj, dict) else 'non-object'}")
            continue
        data = obj.get("data") or []
        for i, m in enumerate(data if isinstance(data, list) else [data]):
            sub = Record(m, rec.raw, f"{rec.location}/{i}", rec.file_idx, rec.line_idx, recv_ns=rec.recv_ns)
            _item(b, m, sub, str(obj.get("type", "update")), states)
    return b.finish()
