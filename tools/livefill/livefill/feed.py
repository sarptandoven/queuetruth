"""Bitstamp WebSocket API v2 public feed: message fields, classification and the JSONL line form that
`queuetruth ingest --format bitstamp` reads.

Feed facts used here (observed on the 2026-10-12 BTC/USD capture behind RD-12 and on this kit's own captures; the
docs page is https://www.bitstamp.net/websocket/v2/):

- `live_orders_<market>`: `order_created` / `order_changed` / `order_deleted` for every order of the market, with
  `data.id` (integer), `data.order_type` (0 buy, 1 sell), `data.price_str`, `data.amount_str` (remaining amount)
  and `data.microtimestamp` (string, venue microseconds). Top-level `event_id` and `pre_event_id` chain the
  messages of one market: `pre_event_id` equals the previous message's `event_id`; null marks a server restart.
- `live_trades_<market>`: `trade` with `data.id` (trade id), `buy_order_id`, `sell_order_id`, `type` (0: the buy
  order was the taker, 1: the sell order was), `price_str`, `amount_str`, `microtimestamp`. No event chain.
- `detail_order_book_<market>`: `event = "data"`, the best 100 orders per side as `[price, amount, order_id]` in
  priority order, on every book change.
- Control events: `bts:subscription_succeeded`, `bts:error`, `bts:heartbeat`, `bts:request_reconnect` (the server
  is about to close the connection; reconnect).

The REST `GET /api/v2/order_book/<market>/?group=2` body has the same `bids` / `asks` triples for the whole book
plus `microtimestamp`; it is stored as a `rest_book` row and can seed an exported segment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from queuetruth.errors import QtError

WS_URL = "wss://ws.bitstamp.net"
REST_URL = "https://www.bitstamp.net"
#: Channel prefixes the capture subscribes to by default (detail_order_book seeds the book and is the parity oracle).
DEFAULT_CHANNELS = ("live_orders", "live_trades", "detail_order_book")
KNOWN_CHANNELS = ("live_orders", "live_trades", "detail_order_book", "order_book", "diff_order_book", "full_order_book")
ORDER_EVENTS = ("order_created", "order_changed", "order_deleted")
CONTROL_EVENTS = ("bts:subscription_succeeded", "bts:unsubscription_succeeded", "bts:error", "bts:heartbeat")
RECONNECT_EVENT = "bts:request_reconnect"


def split_channel(channel: str) -> tuple[str, str]:
    """`live_orders_btcusd` -> (`live_orders`, `btcusd`); a channel without `_` has no market."""
    prefix, sep, market = channel.rpartition("_")
    if not sep:
        return channel, ""
    return prefix, market


def check_market(market: str) -> str:
    """A market symbol as Bitstamp spells it in channel names (lower-case letters and digits)."""
    m = market.strip().lower()
    if not m or not m.isalnum() or not m.isascii():
        raise QtError("QT-USAGE-001", f"market {market!r}: expected a Bitstamp market symbol such as btcusd or usdtusd")
    return m


def check_channels(channels: list[str] | tuple[str, ...]) -> list[str]:
    """Channel prefixes, validated; `live_orders` is mandatory (it is the order-level feed)."""
    out = []
    for c in channels:
        c = c.strip()
        if c not in KNOWN_CHANNELS:
            raise QtError("QT-USAGE-001", f"channel {c!r}: expected one of {list(KNOWN_CHANNELS)}")
        if c not in out:
            out.append(c)
    if "live_orders" not in out:
        raise QtError("QT-USAGE-001", "channels: live_orders is required (it is the order-level feed)")
    return out


def subscribe_messages(markets: list[str], channels: list[str]) -> list[str]:
    """`bts:subscribe` requests, one per (market, channel), in a fixed order."""
    return [
        json.dumps({"event": "bts:subscribe", "data": {"channel": f"{c}_{m}"}}, separators=(",", ":"))
        for m in markets
        for c in channels
    ]


def int_or_none(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return int(s)
    return None


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))


@dataclass(frozen=True)
class Parsed:
    """The indexed fields of one feed message (everything else stays in the raw text)."""

    event: str | None
    channel: str | None
    market: str | None
    prefix: str | None
    event_id: str | None
    pre_event_id: str | None
    has_pre_event_id: bool
    microtimestamp: int | None
    order_id: int | None
    trade_id: int | None
    buy_order_id: int | None
    sell_order_id: int | None
    data: Any


def parse_message(raw: str) -> Parsed:
    """Parses one WebSocket text frame; non-JSON or non-object frames raise `QT-INPUT-002`."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise QtError("QT-INPUT-002", f"bitstamp frame is not JSON: {e.msg} at offset {e.pos}") from e
    if not isinstance(msg, dict):
        raise QtError("QT-INPUT-002", "bitstamp frame is not a JSON object")
    event = msg.get("event") if isinstance(msg.get("event"), str) else None
    channel = msg.get("channel") if isinstance(msg.get("channel"), str) else None
    prefix, market = split_channel(channel) if channel else (None, None)
    data = msg.get("data")
    d = data if isinstance(data, dict) else {}
    is_trade = event == "trade"
    is_order = event in ORDER_EVENTS
    return Parsed(
        event=event,
        channel=channel,
        market=market or None,
        prefix=prefix,
        event_id=_str_or_none(msg.get("event_id")),
        pre_event_id=_str_or_none(msg.get("pre_event_id")),
        has_pre_event_id="pre_event_id" in msg,
        microtimestamp=int_or_none(d.get("microtimestamp")),
        order_id=int_or_none(d.get("id")) if is_order else None,
        trade_id=int_or_none(d.get("id")) if is_trade else None,
        buy_order_id=int_or_none(d.get("buy_order_id")) if is_trade else None,
        sell_order_id=int_or_none(d.get("sell_order_id")) if is_trade else None,
        data=data,
    )


def jsonl_line(raw: str, recv_ns: int, extra: dict[str, Any] | None = None) -> str:
    """The capture line for `queuetruth ingest --format bitstamp`: the received object text, unchanged byte for byte
    (numbers keep their token text), with `"recv_ns": N` (and `extra` keys) appended as the last members.

    JSON allows a raw line break only as whitespace between tokens, so line breaks become spaces."""
    text = raw.strip().replace("\r", " ").replace("\n", " ")
    if not (text.startswith("{") and text.endswith("}")):
        raise QtError("QT-INPUT-002", "bitstamp frame is not a JSON object")
    obj = json.loads(text)
    tail = {"recv_ns": int(recv_ns), **(extra or {})}
    clash = sorted(k for k in tail if k in obj)
    if clash:
        raise QtError("QT-INPUT-002", f"bitstamp frame already has member(s) {clash}")
    body = text[:-1].rstrip()
    sep = "" if body.endswith("{") else ","
    members = ",".join(f"{json.dumps(k)}:{json.dumps(v, separators=(',', ':'))}" for k, v in tail.items())
    return f"{body}{sep}{members}}}"


def rest_seed_line(market: str, body: str, recv_ns: int) -> str:
    """A REST `order_book?group=2` body as a `detail_order_book_<market>` data line (the shape the ingest seeds
    from), tagged `"livefill_seed": "rest"`."""
    text = body.strip().replace("\r", " ").replace("\n", " ")
    d = json.loads(text)
    if not isinstance(d, dict) or not isinstance(d.get("bids"), list) or not isinstance(d.get("asks"), list):
        raise QtError("QT-INPUT-002", f"REST order book of {market}: expected an object with bids and asks")
    if int_or_none(d.get("microtimestamp")) is None:
        raise QtError("QT-INPUT-002", f"REST order book of {market}: missing microtimestamp")
    for side in ("bids", "asks"):
        for e in d[side]:
            if not (isinstance(e, list) and len(e) == 3):
                raise QtError(
                    "QT-INPUT-002",
                    f"REST order book of {market}: {side} entries must be [price, amount, order_id] (use group=2)",
                )
    head = f'{{"event":"data","channel":"detail_order_book_{market}","data":'
    return f'{head}{text},"recv_ns":{int(recv_ns)},"livefill_seed":"rest"}}'
