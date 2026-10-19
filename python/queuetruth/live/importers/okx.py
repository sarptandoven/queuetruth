"""OKX v5 importer (SPEC 5.7.1; fixture-validated only).

Inputs: WebSocket `orders` channel pushes (JSON lines, optionally capture-wrapped), the VIP `fills` channel, REST
`/api/v5/trade/fills(-history)` and `/api/v5/trade/orders-history(-archive)` responses (`{"code", "data"}`
envelope or bare lists).

- Fees: OKX reports a charged fee as negative and a rebate as positive; canonical `fee = -fillFee` (WS, per update,
  never the cumulative `fee`) or `-fee` (REST fills).
- Fills: `tradeId` (unique per instrument), `fillSz`/`fillPx`, `fillTime` match_time, `execType` M/T. `cum_qty` =
  `accFillSz`, `leaves_qty` = `sz - accFillSz` on order-channel rows.
- Order-channel states: first `live` -> ack; `amendResult = 0` -> amend_ack (`sz` = new total qty; priority
  undocumented -> null), `amendResult = -1` -> amend_reject; `canceled`: `cancelSource` 1 -> cancel_ack, 31
  (post-only would take) -> post_only_reject, any other source (STP 32, IOC/FOK remainder 14/13, system ...) ->
  expire; `mmp_canceled` -> expire. State rows use `uTime` (record_time).
- REST order history: an `ack` at `cTime` and the terminal event at `uTime` (record_time), used only when no
  captured event of the same type exists. `tag` -> `strategy_tag`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from queuetruth.live._common import Record, as_bool, dec, dec_text, dsub, epoch_ns, text_or_none
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.importers._base import ImportOptions, json_records, new_builder, side_of, sub_records

VENUE = "okx"
_ORD_TYPE = {
    "limit": ("limit", "gtc"),
    "post_only": ("limit", "post_only"),
    "ioc": ("limit", "ioc"),
    "fok": ("limit", "fok"),
    "market": ("market", "other"),
    "optimal_limit_ioc": ("other", "ioc"),
    "mmp": ("limit", "gtc"),
    "mmp_and_post_only": ("limit", "post_only"),
}


def _neg(v: Decimal | None) -> Decimal | None:
    return None if v is None else -v


def _register(b: LogBuilder, m: dict[str, Any], rec: Record, record: bool) -> int:
    loc = rec.location
    ot, tif = _ORD_TYPE.get(str(m.get("ordType", "")).lower(), ("other", "other"))
    px = dec(m.get("px"), "px", loc)
    if px is not None and ot != "limit" and px == 0:
        px = None
    return b.order(
        oid=text_or_none(m.get("ordId")),
        cid=text_or_none(m.get("clOrdId")),
        rec=rec,
        record=record,
        qty=dec(m.get("sz"), "sz", loc),
        qty_text=dec_text(m.get("sz")),
        px=px,
        px_text=dec_text(m.get("px")) if px is not None else None,
        instrument=text_or_none(m.get("instId")),
        side=side_of(m.get("side")),
        order_type=ot if record else None,
        tif=tif if record else None,
        reduce_only=as_bool(m.get("reduceOnly")),
        strategy_tag=text_or_none(m.get("tag")),
    )


def _terminal(m: dict[str, Any]) -> tuple[str, str] | None:
    state = str(m.get("state", ""))
    if state == "canceled":
        src = text_or_none(m.get("cancelSource")) or ""
        if src == "1":
            return "cancel_ack", f"cancelSource={src}"
        if src == "31":
            return "post_only_reject", f"cancelSource={src}"
        if src == "32":
            return "expire", f"cancelSource={src} stpMode={text_or_none(m.get('stpMode'))}"
        return "expire", f"cancelSource={src}" if src else "canceled"
    if state == "mmp_canceled":
        return "expire", state
    return None


def _ws_order(b: LogBuilder, m: dict[str, Any], rec: Record, acked: set[int]) -> None:
    loc = rec.location
    h = _register(b, m, rec, record=True)
    sz, acc = dec(m.get("sz"), "sz", loc), dec(m.get("accFillSz"), "accFillSz", loc)
    leaves = dsub(sz, acc) if sz is not None and acc is not None else None
    fill_sz = dec(m.get("fillSz"), "fillSz", loc)
    trade_id = text_or_none(m.get("tradeId"))
    if trade_id is not None and fill_sz is not None and fill_sz > 0:
        ts, prec = epoch_ns(m.get("fillTime"), "ms", "fillTime", loc)
        b.event(
            h,
            "fill",
            rec=rec,
            source="ws",
            kind="match_time",
            precision=prec,
            ts_exchange=ts,
            qty=fill_sz,
            px=dec(m.get("fillPx"), "fillPx", loc),
            cum=acc,
            leaves=leaves,
            fill_id=trade_id,
            liquidity={"M": "maker", "T": "taker"}.get(str(m.get("execType", "")).upper(), "unknown"),
            fee=_neg(dec(m.get("fillFee"), "fillFee", loc)),
            fee_ccy=text_or_none(m.get("fillFeeCcy")) or text_or_none(m.get("feeCcy")),
        )
        return
    ts, prec = epoch_ns(m.get("uTime"), "ms", "uTime", loc)
    common: dict[str, Any] = {
        "rec": rec,
        "source": "ws",
        "kind": "record_time",
        "precision": prec,
        "ts_exchange": ts,
        "cum": acc,
    }
    amend = text_or_none(m.get("amendResult"))
    term = _terminal(m)
    if amend in ("0", "-1"):
        reason = (
            f"amendSource={text_or_none(m.get('amendSource'))} amendResult={amend} reqId={text_or_none(m.get('reqId'))}"
        )
        if amend == "0":
            px = dec(m.get("px"), "px", loc)
            b.event(h, "amend_ack", qty=sz, px=px, leaves=leaves, reason=reason, **common)
        else:
            b.event(h, "amend_reject", reason=f"{reason} code={text_or_none(m.get('code'))}", **common)
    elif term is not None:
        b.event(h, term[0], reason=term[1], **common)
    elif m.get("state") == "live" and h not in acked:
        b.event(h, "ack", qty=sz, px=dec(m.get("px"), "px", loc), leaves=leaves, **common)
    else:
        b.skip(f"orders-channel update state={m.get('state')} without fill or amend")
    if m.get("state") in ("live", "partially_filled"):
        acked.add(h)


def _fill(b: LogBuilder, m: dict[str, Any], rec: Record, source: str) -> None:
    loc = rec.location
    h = _register(b, m, rec, record=False)
    t_field = "fillTime" if m.get("fillTime") not in (None, "") else "ts"
    ts, prec = epoch_ns(m.get(t_field), "ms", t_field, loc)
    b.event(
        h,
        "fill",
        rec=rec,
        source=source,
        kind="match_time",
        precision=prec,
        ts_exchange=ts,
        qty=dec(m.get("fillSz"), "fillSz", loc),
        px=dec(m.get("fillPx"), "fillPx", loc),
        fill_id=text_or_none(m.get("tradeId")),
        liquidity={"M": "maker", "T": "taker"}.get(str(m.get("execType", "")).upper(), "unknown"),
        fee=_neg(dec(m.get("fee"), "fee", loc)),
        fee_ccy=text_or_none(m.get("feeCcy")),
    )


def _rest_order(b: LogBuilder, m: dict[str, Any], rec: Record) -> None:
    loc = rec.location
    h = _register(b, m, rec, record=True)
    term = _terminal(m)
    common: dict[str, Any] = {"rec": rec, "source": "rest", "kind": "record_time", "snapshot": True}
    if term is None or term[0] != "post_only_reject":
        ts, prec = epoch_ns(m.get("cTime"), "ms", "cTime", loc)
        b.event(h, "ack", ts_exchange=ts, precision=prec, reason="from orders-history cTime", **common)
    if term is not None:
        ts, prec = epoch_ns(m.get("uTime"), "ms", "uTime", loc)
        b.event(h, term[0], ts_exchange=ts, precision=prec, reason=term[1], **common)


def import_okx(paths: list[str], opts: ImportOptions) -> ImportResult:
    """OKX orders / fills channels and REST fills / order history -> canonical rows."""
    b = new_builder(VENUE, opts)
    acked: set[int] = set()
    for rec in json_records(paths):
        obj = rec.obj
        channel = None
        if isinstance(obj, dict) and isinstance(obj.get("arg"), dict):
            channel = obj["arg"].get("channel")
            if "data" not in obj:
                b.skip(f"control message {obj.get('event') or channel}")
                continue
        elif isinstance(obj, dict) and "event" in obj and "data" not in obj:
            b.skip(f"control message {obj.get('event')}")
            continue
        for sub in sub_records(rec, "data"):
            m = sub.obj
            if not isinstance(m, dict):
                b.skip("non-object record")
            elif channel == "orders":
                _ws_order(b, m, sub, acked)
            elif channel == "fills":
                _fill(b, m, sub, "ws")
            elif channel is not None:
                b.skip(f"channel {channel}")
            elif "billId" in m or ("tradeId" in m and "state" not in m):
                _fill(b, m, sub, "rest")
            elif "state" in m and "ordId" in m:
                _rest_order(b, m, sub)
            else:
                b.skip("unrecognised record")
    return b.finish()
