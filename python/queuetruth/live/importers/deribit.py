"""Deribit importer (SPEC 5.7.1; fixture-validated only).

Inputs: JSON-RPC subscription notifications on `user.orders.*` and `user.trades.*` (JSON lines, optionally
capture-wrapped), REST `get_user_trades_by_*` (`result.trades`) and `get_order_history_by_*` / `get_order_state`
(`result` array or object). Numbers keep their JSON token text.

- Trades: `trade_id` fill id, `timestamp` ms match_time, `liquidity` M/T, `fee` (Deribit sign: positive = cost,
  negative = maker rebate), `amount`/`price`. Side comes from the order record when one is joinable; otherwise from
  the trade's `direction`, flagged in the event `reason` and a warning (ASSUMED).
- Orders: a non-numeric `price` (`"market_price"` on trigger-market orders) means no limit price: `limit_px` / event
  `px` null, counted in one warning (the import continues). First sight in state `open` (any non-rejected state
  for REST history) -> ack at `creation_timestamp` (record_time); `replaced = true` with a changed amount or
  price -> amend_ack (amount down at the same price keeps priority, a price change or amount increase loses it);
  `cancelled`: `user_request` -> cancel_ack, `edit_post_only_reject` / post-only rejections -> post_only_reject,
  other reasons -> expire; `rejected` -> reject. Order-notification times are record_time. `label` is the client
  order id. REST order history adds an `ack` and the terminal event as history records (used only when no
  captured event of the same type exists).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live._common import Record, as_bool, dec, dec_text, dsub, epoch_ns, text_or_none
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.importers._base import ImportOptions, json_records, new_builder, obj_field, side_of, sub_records

VENUE = "deribit"
_TIF = {
    "good_til_cancelled": "gtc",
    "good_til_day": "day",
    "fill_or_kill": "fok",
    "immediate_or_cancel": "ioc",
}
_TYPE = {
    "limit": "limit",
    "market": "market",
    "stop_limit": "stop_limit",
    "stop_market": "stop",
    "take_limit": "stop_limit",
    "take_market": "stop",
    "market_limit": "market",
}


@dataclass
class _St:
    amount: Decimal | None
    amount_text: str | None
    price: Decimal | None
    price_text: str | None


def _order_price(m: dict[str, Any], loc: str, non_numeric: dict[str, int]) -> Decimal | None:
    """Limit price of an order record. A non-numeric price (`"market_price"` on trigger-market orders) is no limit
    price: None, counted in `non_numeric` by its text."""
    v = m.get("price")
    try:
        return dec(v, "price", loc)
    except QtError:
        text = str(v)
        non_numeric[text] = non_numeric.get(text, 0) + 1
        return None


def _register(b: LogBuilder, m: dict[str, Any], rec: Record, px: Decimal | None) -> int:
    loc = rec.location
    po = as_bool(m.get("post_only"))
    tif = text_or_none(m.get("time_in_force"))
    ot = text_or_none(m.get("order_type"))
    return b.order(
        oid=text_or_none(m.get("order_id")),
        cid=text_or_none(m.get("label")),
        rec=rec,
        record=True,
        qty=dec(m.get("amount"), "amount", loc),
        qty_text=dec_text(m.get("amount")),
        px=px,
        px_text=dec_text(m.get("price")) if px is not None else None,
        instrument=text_or_none(m.get("instrument_name")),
        side=side_of(m.get("direction")),
        order_type=_TYPE.get(ot or "", "other") if ot else None,
        tif="post_only" if po else (_TIF.get(tif, "other") if tif else None),
        post_only=po,
        reduce_only=as_bool(m.get("reduce_only")),
    )


def _terminal(m: dict[str, Any]) -> tuple[str, str] | None:
    state = str(m.get("order_state", ""))
    reason = text_or_none(m.get("cancel_reason")) or text_or_none(m.get("reject_reason")) or state
    if state == "cancelled":
        if reason == "user_request":
            return "cancel_ack", reason
        if "post_only" in reason:
            return "post_only_reject", reason
        return "expire", reason
    if state == "rejected":
        return ("post_only_reject" if "post_only" in reason else "reject"), reason
    return None


def _order(
    b: LogBuilder, m: dict[str, Any], rec: Record, source: str, states: dict[int, _St], non_numeric: dict[str, int]
) -> None:
    loc = rec.location
    price = _order_price(m, loc, non_numeric)
    h = _register(b, m, rec, price)
    amount = dec(m.get("amount"), "amount", loc)
    filled = dec(m.get("filled_amount"), "filled_amount", loc) or Decimal(0)
    leaves = dsub(amount, filled) if amount is not None else None
    snapshot = source == "rest"
    st = states.get(h)
    term = _terminal(m)
    if st is None:
        state = str(m.get("order_state", ""))
        if (snapshot and state != "rejected") or (not snapshot and state == "open"):
            ts, prec = epoch_ns(m.get("creation_timestamp"), "ms", "creation_timestamp", loc)
            b.event(
                h,
                "ack",
                rec=rec,
                source=source,
                kind="record_time",
                precision=prec,
                ts_exchange=ts,
                qty=amount,
                px=price,
                cum=Decimal(0),
                leaves=amount,
                snapshot=snapshot,
                reason="from order history creation_timestamp" if snapshot else None,
            )
        st = _St(amount, dec_text(m.get("amount")), price, dec_text(m.get("price")))
        states[h] = st
    ts, prec = epoch_ns(m.get("last_update_timestamp"), "ms", "last_update_timestamp", loc)
    common: dict[str, Any] = {
        "rec": rec,
        "source": source,
        "kind": "record_time",
        "precision": prec,
        "ts_exchange": ts,
        "cum": filled,
        "snapshot": snapshot,
    }
    if as_bool(m.get("replaced")) and (amount != st.amount or price != st.price):
        if price != st.price:
            keeps = False
            reason = f"replaced: price {st.price_text} -> {dec_text(m.get('price'))} (documented: goes to end of queue)"
        elif amount is not None and st.amount is not None and amount < st.amount:
            keeps = True
            reason = (
                f"replaced: amount {st.amount_text} -> {dec_text(m.get('amount'))} at same price "
                "(documented: keeps time priority)"
            )
        else:
            keeps = False
            reason = f"replaced: amount {st.amount_text} -> {dec_text(m.get('amount'))} (amount up loses priority)"
        b.event(h, "amend_ack", qty=amount, px=price, leaves=leaves, keeps=keeps, reason=reason, **common)
        states[h] = _St(amount, dec_text(m.get("amount")), price, dec_text(m.get("price")))
    if term is not None:
        b.event(h, term[0], reason=term[1], **common)
    elif str(m.get("order_state")) in ("untriggered", "triggered"):
        b.skip(f"order_state {m.get('order_state')}")


def _trade(
    b: LogBuilder, m: dict[str, Any], rec: Record, source: str, orphans: dict[int, list[tuple[Any, str | None]]]
) -> None:
    loc = rec.location
    oid = text_or_none(m.get("order_id"))
    ot = text_or_none(m.get("order_type"))
    h = b.order(
        oid=oid,
        rec=rec,
        instrument=text_or_none(m.get("instrument_name")),
        order_type=_TYPE.get(ot or "", "other") if ot else None,
        reduce_only=as_bool(m.get("reduce_only")),
    )
    ts, prec = epoch_ns(m.get("timestamp"), "ms", "timestamp", loc)
    liq = str(m.get("liquidity", "")).upper()
    e = b.event(
        h,
        "fill",
        rec=rec,
        source=source,
        kind="match_time",
        precision=prec,
        ts_exchange=ts,
        qty=dec(m.get("amount"), "amount", loc),
        px=dec(m.get("price"), "price", loc),
        fill_id=text_or_none(m.get("trade_id")),
        liquidity={"M": "maker", "T": "taker"}.get(liq, "unknown"),
        fee=dec(m.get("fee"), "fee", loc),
        fee_ccy=text_or_none(m.get("fee_currency")),
    )
    orphans.setdefault(h, []).append((e, side_of(m.get("direction"))))


def import_deribit(paths: list[str], opts: ImportOptions) -> ImportResult:
    """Deribit user.orders / user.trades notifications and REST trades / order history -> canonical rows."""
    b = new_builder(VENUE, opts)
    states: dict[int, _St] = {}
    trades: dict[int, list[tuple[Any, str | None]]] = {}
    non_numeric: dict[str, int] = {}
    for rec in json_records(paths):
        obj = rec.obj
        if not isinstance(obj, dict):
            b.skip("non-object record")
            continue
        if obj.get("method") == "subscription":
            params = obj_field(obj.get("params"), "params", rec.location)
            channel = str(params.get("channel", ""))
            kind = channel.split(".")[1] if channel.count(".") >= 1 else ""
            for sub in sub_records(
                Record(params, rec.raw, rec.location, rec.file_idx, rec.line_idx, recv_ns=rec.recv_ns), "data"
            ):
                if not isinstance(sub.obj, dict):
                    b.skip("non-object record")
                elif channel.startswith("user.orders"):
                    _order(b, sub.obj, sub, "ws", states, non_numeric)
                elif channel.startswith("user.trades"):
                    _trade(b, sub.obj, sub, "ws", trades)
                else:
                    b.skip(f"channel {kind or channel}")
            continue
        if "result" in obj or "trades" in obj or "order_id" in obj:
            body = obj.get("result", obj)
            keys = ("trades",) if isinstance(body, dict) and "trades" in body else ()
            for sub in sub_records(Record(body, rec.raw, rec.location, rec.file_idx, rec.line_idx), *keys):
                m = sub.obj
                if not isinstance(m, dict):
                    b.skip("non-object record")
                elif "trade_id" in m:
                    _trade(b, m, sub, "rest", trades)
                elif "order_state" in m:
                    _order(b, m, sub, "rest", states, non_numeric)
                else:
                    b.skip("unrecognised record")
            continue
        b.skip(f"message {obj.get('method') or 'unknown'}")
    if non_numeric:
        texts = ", ".join(f"{t!r} x{n}" for t, n in sorted(non_numeric.items()))
        b.warn(
            f"{VENUE}: {sum(non_numeric.values())} order record(s) with a non-numeric price ({texts}; trigger-market "
            "orders): no limit price, limit_px / px left null"
        )
    for h, items in trades.items():
        if b.order_field(h, "side") is not None:
            continue
        direction = next((d for _, d in items if d is not None), None)
        if direction is None:
            continue
        b.set_order_field(h, "side", direction)
        for e, _ in items:
            e.reason = "side from trade.direction (ASSUMED: no joinable order record)"
        oid = b.order_field(h, "venue_order_id")
        b.warn(f"{VENUE}: order {oid}: side taken from trade direction (ASSUMED, no order record)")
    return b.finish()
