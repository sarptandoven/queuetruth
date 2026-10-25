"""Kalshi readers: the user's own WebSocket captures and REST exports (docs.kalshi.com, read 2026-10-24).

WebSocket (`wss://.../trade-api/ws/v2`; JSON lines, optionally capture-wrapped, see `queuetruth.pm.capture`):

- `orderbook_snapshot` (`msg.yes_dollars_fp`, `msg.no_dollars_fp`: `[price, count_fp]` levels, `msg.clock`) and
  `orderbook_delta` (`msg.price_dollars`, `msg.delta_fp`, `msg.side` yes|no, `msg.clock`, optional
  `msg.client_order_id` "present only when you caused this orderbook change", `msg.ts_ms`, deprecated `msg.ts`).
  Deltas sharing a `clock` are one exchange action (one batch). YES levels are YES bids; NO levels are NO bids, i.e.
  YES asks at `1 - price` under the default no-leg pricing, or at `price` when the subscription set
  `use_yes_price: true` (read from a captured `subscribe` command, else `yes_price` = auto|true|false; auto
  falls back to the documented default `false` with a warning).
- Sessions and sequence numbers. The orderbook frames of every input are merged by local receive time (each file
  keeps its own order) and `seq` is checked per subscription (`sid`), as the docs ask, per connection: a gap
  (`seq` jumps) marks every market of the subscription as gapped until its next snapshot; a frame repeating an
  earlier `seq` with the same content is a duplicate and dropped (`duplicate_book_frames`); any other step back
  (`sequence_regressions`), a new `subscribed` reply for a known `sid`, or a snapshot that restarts the sequence is
  a new session (a reconnect: `orderbook_resubscriptions`), which gaps the subscription's markets until their next
  snapshot, so orders resting through the outage are censored. A market's book is read from one subscription at a
  time: a snapshot of it on another `sid` moves it there (a reconnect that got a new `sid`, also a gap), and
  messages from the subscription it left are dropped, counted (`duplicate_subscription_messages`, a data-quality
  anomaly) and warned about.
- `trade` (`yes_price_dollars`, `count_fp`, `taker_outcome_side` (or `taker_book_side`, legacy `taker_side`),
  `is_block_trade`, `ts_ms`): a print at the YES price; taker yes = buy aggressor. Block trades are matched off
  book and never touch the queue. Prints are deduplicated by `trade_id` across the WebSocket and REST exports
  (`duplicate_trades`; the WebSocket copy is kept); two prints with one id and a different market, price, size or
  side are `QT-INPUT-002`.
- `fill` (own fills: `trade_id`, `order_id`, `client_order_id`, `is_taker`, `book_side` bid|ask (legacy `side` +
  `action`), `yes_price_dollars`, `count_fp`, `fee_cost`, `ts_ms`) and `user_order` (`status` resting|canceled|
  executed, `yes_price_dollars`, `fill_count_fp`, `remaining_count_fp`, `initial_count_fp`, `maker_fill_cost_dollars`,
  `taker_fill_cost_dollars`, `maker_fees_dollars`, `taker_fees_dollars`, `created_ts_ms`, `last_updated_ts_ms`,
  `last_update_reason`) -> the canonical live log. The first `user_order` of an order is an `ack` at
  `created_ts_ms` (match_time: the exchange's entry time), or a `restate` when the order was created before the
  capture started; later updates are diffed: a price change or a size increase is an `amend_ack` that loses
  priority, a size decrease without fills one that keeps it (`Decrease`), `canceled` is a `cancel_ack` (no
  reason), `post_only_reject` (`PostOnlyCrossCancel`) or `expire` (any other exchange reason), `executed` is
  `done`. Fill counters that grow without a `fill` message give derived fills (dropped where per-fill records
  exist); maker / taker comes from which fill cost grew, the fee from the growth of the order's reported fees.
- `market_lifecycle_v2`: `determined` / `settled` events give the settlement value, and `price_ranges` (on
  `created` and `price_level_structure_updated`) the market's price grid.

REST exports (one JSON response per line or file, optionally wrapped; endpoints are recognised by their payload
key): `{"fills": [...]}` (`GET /portfolio/fills`), `{"orders": [...]}` / `{"order": ...}`, `{"orderbook_fp": ...}`
(`GET /markets/{ticker}/orderbook`; the ticker from the wrapper's `ticker` or the captured request path; NO levels
always no-leg priced: for a market whose book the WebSocket delivers, each response is only compared with that book
between the request's send and receive times, `queuetruth.pm.batches` checks; for any other market the responses,
in receive-time order, are its book), `{"trades": [...]}`, `{"queue_positions": [...]}`
(`GET /portfolio/orders/queue_positions`)
and `{"queue_position_fp": ...}` (`GET /portfolio/orders/{order_id}/queue_position`, order id from the wrapper or the
request path), `{"market": ...}` / `{"markets": [...]}` (`price_ranges`, `fee_waiver_expiration_time`, `result`,
`settlement_value_dollars`), `{"series": ...}` (`fee_type`, `fee_multiplier`; a market belongs to the series whose
ticker prefixes its own) and `{"incentive_programs": [...]}` (liquidity programs: `period_reward` in centi-cents,
`target_size_fp`, `discount_factor_bps`). Queue positions need the local receive time (capture wrapper): without
it they are refused (`QT-INPUT-002`), because they cannot be placed on the book's clock.

Inputs are read in time order (`queuetruth.pm.capture.order_inputs`), so rotated captures can be passed in any
order; captures without local receive times are read in the order given.
"""

from __future__ import annotations

import heapq
import re
from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live._common import Record, dump_raw, rfc3339_ns
from queuetruth.live.builder import LogBuilder
from queuetruth.pm.batches import BookMsg, TradeMsg
from queuetruth.pm.book import ASK, BID
from queuetruth.pm.capture import Frame, iter_frames, order_inputs
from queuetruth.pm.fees import KALSHI_FEE_TYPES, KalshiFeeSchedule
from queuetruth.pm.rewards import LipProgram, PriceGrid
from queuetruth.pm.units import KALSHI_SIZE_EXP, ONE, PRICE_EXP, dec, ms_to_ns, price_units, req_dec, size_units
from queuetruth.pm.venue import MarketInfo, QueueSample, VenueCapture

VENUE = "kalshi"
_ORDER_PATH = re.compile(r"/portfolio/orders/([^/?#]+)/queue_position")
_BOOK_PATH = re.compile(r"/markets/([^/?#]+)/orderbook")
_END_REASONS_EXPIRE = {
    "ExpiryCancel",
    "CloseCancel",
    "HaltCancel",
    "SelfTradeCancel",
    "MarginCancel",
    "SettlementBoundsCancel",
    "ReduceOnlyCancel",
}


#: Sequence numbers remembered per subscription to tell a duplicated frame from a restarted sequence.
_RECENT_SEQS = 4096


@dataclass
class _BookRec:
    """One orderbook frame or `subscribed` reply, kept until every input is read: `finish` merges the inputs by
    receive time and runs the session and sequence checks on the merged stream."""

    kind: str  # subscribed | snapshot | delta
    file_idx: int
    recv: int | None
    sid: int | None = None
    seq: int | None = None
    ticker: str = ""
    msg: BookMsg | None = None

    def content(self) -> tuple[Any, ...]:
        """What a duplicated frame repeats exactly."""
        m = self.msg
        if m is None:
            return ()
        return (m.kind, tuple(m.levels), m.group, m.own_tag, m.ts_event)


@dataclass
class _OrderState:
    status: str
    fill: int
    remaining: int
    price: int
    maker_cost: Decimal
    taker_cost: Decimal
    fees: Decimal | None = None  # maker_fees_dollars + taker_fees_dollars (cumulative), None when not reported


def _side(m: dict[str, Any], where: str) -> str:
    """`buy` (YES bid) or `sell` (YES ask) from `book_side`, `outcome_side`, or the legacy `side` + `action`."""
    bs = m.get("book_side")
    if bs in ("bid", "ask"):
        return "buy" if bs == "bid" else "sell"
    os_ = m.get("outcome_side")
    if os_ in ("yes", "no"):
        return "buy" if os_ == "yes" else "sell"
    if "is_yes" in m and isinstance(m["is_yes"], bool):
        return "buy" if m["is_yes"] else "sell"
    side, action = m.get("side"), m.get("action")
    if side in ("yes", "no") and action in ("buy", "sell"):
        long_yes = (side == "yes") == (action == "buy")
        return "buy" if long_yes else "sell"
    raise QtError("QT-INPUT-002", f"{where}: cannot tell the order direction (no book_side / outcome_side)")


def _taker_aggressor(m: dict[str, Any], where: str) -> int:
    v = m.get("taker_outcome_side") or m.get("taker_side")
    if v in ("yes", "no"):
        return BID if v == "yes" else ASK
    b = m.get("taker_book_side")
    if b in ("bid", "ask"):
        return BID if b == "bid" else ASK
    raise QtError("QT-INPUT-002", f"{where}: trade without taker side")


def _ms(v: Any, what: str, where: str) -> int | None:
    return None if v is None else ms_to_ns(v, what, where)


def _rfc(v: Any, what: str, where: str) -> int | None:
    if v is None or v == "":
        return None
    return rfc3339_ns(str(v), what, where)[0]


def _record(fr: Frame) -> Record:
    """A live-log record of an own-order frame; `raw` keeps the frame's JSON (the canonical log's `raw` column)."""
    return Record(
        fr.obj, dump_raw(fr.obj), fr.location, fr.file_idx, fr.line_idx * 1000 + fr.sub_idx, recv_ns=fr.recv_ns
    )


class KalshiReader:
    """Accumulates one ingest's Kalshi inputs (see the module docstring)."""

    def __init__(self, *, account: str = "default", yes_price: str = "auto", member: str = "direct") -> None:
        if yes_price not in ("auto", "true", "false"):
            raise QtError("QT-CONFIG-001", f"kalshi yes_price {yes_price!r}: expected auto, true or false")
        self.cap = VenueCapture(VENUE)
        self.cap.builder = LogBuilder(VENUE, account=account, price_exp=PRICE_EXP, size_exp=KALSHI_SIZE_EXP)
        self.yes_price_opt = yes_price
        self.member = member
        self._cmd_yes: dict[int, bool] = {}
        self._sid_yes: dict[int, bool] = {}
        self._book_recs: list[_BookRec] = []
        self._rest_books: list[tuple[str, BookMsg, int | None, int]] = []  # (ticker, snapshot, send time, file)
        self._trade_ids: dict[str, tuple[str, int, bool]] = {}  # trade id -> (ticker, index, from the WebSocket)
        self._orders: dict[str, _OrderState] = {}
        self._series: dict[str, KalshiFeeSchedule] = {}
        self._flat: set[str] = set()
        self._waivers: dict[str, int] = {}
        self._assumed_yes_price = False
        self._lip_raw: list[tuple[dict[str, Any], str]] = []
        self._fill_order_ids: set[str] = set()
        self._taker_trade_ids: set[str] = set()
        self._derived_orders: set[str] = set()
        self._derived: list[tuple[str, Frame, dict[str, Any]]] = []

    # ---- helpers ------------------------------------------------------------------------------------------------

    def _market(self, ticker: str) -> MarketInfo:
        m = self.cap.markets.get(ticker)
        if m is None:
            m = MarketInfo(ticker, VENUE, KALSHI_SIZE_EXP, "USD")
            self.cap.markets[ticker] = m
            self.cap.book.setdefault(ticker, [])
            self.cap.trades.setdefault(ticker, [])
        return m

    def _ts(self, fr: Frame, *cands: int | None) -> int:
        for c in cands:
            if c is not None:
                return c
        if fr.recv_ns is not None:
            return fr.recv_ns
        raise QtError("QT-INPUT-002", f"{fr.location}: message has no exchange time and no capture receive time")

    def _note_time(self, ns: int | None) -> None:
        if ns is not None and (self.cap.first_ns is None or ns < self.cap.first_ns):
            self.cap.first_ns = ns

    def _use_yes(self, sid: int | None) -> bool:
        if self.yes_price_opt != "auto":
            return self.yes_price_opt == "true"
        if sid is not None and sid in self._sid_yes:
            return self._sid_yes[sid]
        self._assumed_yes_price = True
        return False

    def _no_price(self, raw: Any, yes_scale: bool, where: str) -> int:
        p = price_units(raw, "no price", where)
        return p if yes_scale else ONE - p

    # ---- frames -------------------------------------------------------------------------------------------------

    def read(self, paths: list[str | Path]) -> None:
        """Reads every frame of the inputs (in time order, `order_inputs`)."""
        ordered = order_inputs(paths)
        self.cap.inputs.extend(ordered)
        for fr in iter_frames(list(ordered)):
            self._note_time(fr.recv_ns)
            obj = fr.obj
            if fr.direction == "out":
                self._command(obj)
                continue
            if not isinstance(obj, dict):
                self.cap.count("non_object_frames")
                continue
            t = obj.get("type")
            if isinstance(t, str):
                self._ws(t, obj, fr)
            else:
                self._rest(obj, fr)

    def _command(self, obj: Any) -> None:
        if isinstance(obj, dict) and obj.get("cmd") == "subscribe":
            params = obj.get("params") or {}
            chans = params.get("channels") or []
            if "orderbook_delta" in chans and isinstance(obj.get("id"), int):
                self._cmd_yes[obj["id"]] = bool(params.get("use_yes_price", False))

    def _ws(self, t: str, obj: dict[str, Any], fr: Frame) -> None:
        msg = obj.get("msg")
        if t == "subscribed":
            if isinstance(msg, dict) and msg.get("channel") == "orderbook_delta":
                sid, cid = msg.get("sid"), obj.get("id")
                if isinstance(sid, int):
                    if isinstance(cid, int) and cid in self._cmd_yes:
                        self._sid_yes[sid] = self._cmd_yes[cid]
                    self._book_recs.append(_BookRec("subscribed", fr.file_idx, fr.recv_ns, sid=sid))
            return
        if t in ("ok", "unsubscribed", "list_subscriptions"):
            self.cap.count(f"control_{t}")
            return
        if t == "error":
            self.cap.count("error_messages")
            self.cap.warn(f"kalshi: error message(s) in the capture, first at {fr.location}: {msg}")
            return
        if not isinstance(msg, dict):
            self.cap.count(f"skipped_{t}")
            return
        if t in ("orderbook_snapshot", "orderbook_delta"):
            self._book(t, obj, msg, fr)
        elif t == "trade":
            self._trade(msg, fr, ws=True)
        elif t == "fill":
            self._fill(msg, fr, source="ws")
        elif t == "user_order":
            self._user_order(msg, fr)
        elif t == "market_lifecycle_v2":
            self._lifecycle(msg, fr)
        else:
            self.cap.count(f"skipped_{t}")

    def _book(self, t: str, obj: dict[str, Any], msg: dict[str, Any], fr: Frame) -> None:
        where = fr.location
        ticker = msg.get("market_ticker")
        if not isinstance(ticker, str) or not ticker:
            raise QtError("QT-INPUT-002", f"{where}: {t} without market_ticker")
        sid = obj.get("sid") if isinstance(obj.get("sid"), int) else None
        seq = obj.get("seq") if isinstance(obj.get("seq"), int) else None
        self._market(ticker)
        yes_scale = self._use_yes(sid)
        send = _ms(obj.get("sending_ts_ms"), "sending_ts_ms", where)
        if t == "orderbook_snapshot":
            levels: list[tuple[int, int, int]] = []
            for raw in msg.get("yes_dollars_fp") or []:
                levels.append((BID, price_units(raw[0], "yes price", where), size_units(raw[1], -2, "count", where)))
            for raw in msg.get("no_dollars_fp") or []:
                levels.append((ASK, self._no_price(raw[0], yes_scale, where), size_units(raw[1], -2, "count", where)))
            ts = self._ts(fr, send)
            snap = BookMsg("snapshot", ts, fr.recv_ns or ts, levels, location=where, restate_expected=True)
            self._book_recs.append(_BookRec("snapshot", fr.file_idx, fr.recv_ns, sid, seq, ticker, snap))
            return
        side_raw = msg.get("side")
        if side_raw not in ("yes", "no"):
            raise QtError("QT-INPUT-002", f"{where}: orderbook_delta side {side_raw!r}")
        if side_raw == "yes":
            side, price = BID, price_units(msg.get("price_dollars"), "price_dollars", where)
        else:
            side, price = ASK, self._no_price(msg.get("price_dollars"), yes_scale, where)
        delta = size_units(msg.get("delta_fp"), -2, "delta_fp", where, signed=True)
        ts_ex = _ms(msg.get("ts_ms"), "ts_ms", where) or _rfc(msg.get("ts"), "ts", where)
        ts = self._ts(fr, ts_ex, send)
        clock = msg.get("clock")
        tag = msg.get("client_order_id") if isinstance(msg.get("client_order_id"), str) else None
        bm = BookMsg(
            "delta",
            ts,
            fr.recv_ns or ts,
            [(side, price, delta)],
            group=("clock", clock) if isinstance(clock, int) else None,
            own_tag=tag,
            location=where,
        )
        self._book_recs.append(_BookRec("delta", fr.file_idx, fr.recv_ns, sid, seq, ticker, bm))
        if tag is not None:
            self.cap.count("own_attributed_deltas")

    def _trade(self, m: dict[str, Any], fr: Frame, *, ws: bool) -> None:
        where = fr.location
        ticker = m.get("market_ticker") or m.get("ticker")
        if not isinstance(ticker, str):
            raise QtError("QT-INPUT-002", f"{where}: trade without market ticker")
        self._market(ticker)
        price = price_units(m.get("yes_price_dollars"), "yes_price_dollars", where)
        size = size_units(m.get("count_fp"), -2, "count_fp", where)
        ts = _ms(m.get("ts_ms"), "ts_ms", where) or _rfc(m.get("created_time"), "created_time", where)
        if ts is None and m.get("ts") is not None:
            ts = int(req_dec(m.get("ts"), "ts", where)) * 1_000_000_000
        ts = self._ts(fr, ts)
        tid = str(m["trade_id"]) if m.get("trade_id") is not None else None
        t = TradeMsg(
            ts,
            fr.recv_ns or ts,
            price,
            size,
            _taker_aggressor(m, where),
            trade_id=tid,
            block=bool(m.get("is_block_trade", False)),
            location=where,
        )
        self.cap.count("trades_ws" if ws else "trades_rest")
        if tid is None:
            self.cap.trades[ticker].append(t)
            return
        seen = self._trade_ids.get(tid)
        if seen is None:
            self._trade_ids[tid] = (ticker, len(self.cap.trades[ticker]), ws)
            self.cap.trades[ticker].append(t)
            return
        tk0, i0, ws0 = seen
        old = self.cap.trades[tk0][i0]
        if (tk0, old.price, old.size, old.aggressor) != (ticker, t.price, t.size, t.aggressor):
            raise QtError(
                "QT-INPUT-002",
                f"{where}: trade {tid} ({ticker} {t.size} at {t.price}, aggressor {chr(t.aggressor)}) conflicts with "
                f"{old.location} ({tk0} {old.size} at {old.price}, aggressor {chr(old.aggressor)})",
            )
        self.cap.count("duplicate_trades")
        if ws and not ws0:  # the WebSocket copy carries the receive time of the live stream
            self.cap.trades[tk0][i0] = t
            self._trade_ids[tid] = (tk0, i0, True)

    def _fill(self, m: dict[str, Any], fr: Frame, *, source: str) -> None:
        b = self.cap.builder
        assert b is not None
        where = fr.location
        oid = m.get("order_id")
        if not isinstance(oid, str):
            raise QtError("QT-INPUT-002", f"{where}: fill without order_id")
        ticker = m.get("market_ticker") or m.get("ticker")
        if isinstance(ticker, str):
            self._market(ticker)
        cid = m.get("client_order_id") if isinstance(m.get("client_order_id"), str) else None
        if cid:
            self.cap.cids.setdefault(oid, set()).add(cid)
        h = b.order(oid=oid, cid=cid, rec=_record(fr), instrument=ticker, side=_side(m, where))
        ts = _ms(m.get("ts_ms"), "ts_ms", where) or _rfc(m.get("created_time"), "created_time", where)
        prec = "ms"
        if ts is None and m.get("ts") is not None:
            ts, prec = int(req_dec(m.get("ts"), "ts", where)) * 1_000_000_000, "s"
        if ts is None:
            raise QtError("QT-INPUT-002", f"{where}: fill without a timestamp")
        fee = dec(m.get("fee_cost"), "fee_cost", where)
        fid = m.get("trade_id") or m.get("fill_id")
        if m.get("is_taker") is True and m.get("trade_id") is not None:
            self._taker_trade_ids.add(str(m["trade_id"]))
        b.event(
            h,
            "fill",
            rec=_record(fr) if source == "ws" else None,
            source=source,
            kind="match_time",
            precision=prec,
            ts_exchange=ts,
            qty=req_dec(m.get("count_fp"), "count_fp", where),
            px=req_dec(m.get("yes_price_dollars"), "yes_price_dollars", where),
            fill_id=str(fid) if fid is not None else None,
            liquidity="taker" if m.get("is_taker") is True else "maker" if m.get("is_taker") is False else "unknown",
            fee=fee,
            fee_ccy="USD" if fee is not None else None,
        )
        self._fill_order_ids.add(oid)
        self.cap.count("own_fills_ws" if source == "ws" else "own_fills_rest")

    def _order_fields(self, m: dict[str, Any], where: str) -> dict[str, Any]:
        ticker = m.get("ticker") or m.get("market_ticker")
        if not isinstance(ticker, str):
            raise QtError("QT-INPUT-002", f"{where}: order without ticker")
        self._market(ticker)
        typ = m.get("type")
        exp = m.get("expiration_ts_ms") or m.get("expiration_time")
        return {
            "instrument": ticker,
            "side": _side(m, where),
            "order_type": "market" if typ == "market" else "limit",
            "tif": "gtd" if exp else "gtc",
        }

    def _user_order(self, m: dict[str, Any], fr: Frame) -> None:
        b = self.cap.builder
        assert b is not None
        where = fr.location
        oid = m.get("order_id")
        if not isinstance(oid, str):
            raise QtError("QT-INPUT-002", f"{where}: user_order without order_id")
        cid = m.get("client_order_id") if isinstance(m.get("client_order_id"), str) and m["client_order_id"] else None
        if cid:
            self.cap.cids.setdefault(oid, set()).add(cid)
        initial = req_dec(m.get("initial_count_fp"), "initial_count_fp", where)
        px_dec = req_dec(m.get("yes_price_dollars"), "yes_price_dollars", where)
        rec = _record(fr)
        h = b.order(
            oid=oid,
            cid=cid,
            rec=rec,
            record=True,
            qty=initial,
            qty_text=str(m.get("initial_count_fp")),
            px=px_dec,
            px_text=str(m.get("yes_price_dollars")),
            **self._order_fields(m, where),
        )
        fill = size_units(m.get("fill_count_fp"), -2, "fill_count_fp", where)
        rem = size_units(m.get("remaining_count_fp"), -2, "remaining_count_fp", where)
        price = price_units(m.get("yes_price_dollars"), "yes_price_dollars", where)
        status = str(m.get("status"))
        created = _ms(m.get("created_ts_ms"), "created_ts_ms", where) or _rfc(
            m.get("created_time"), "created_time", where
        )
        updated = _ms(m.get("last_updated_ts_ms"), "last_updated_ts_ms", where) or _rfc(
            m.get("last_update_time"), "last_update_time", where
        )
        maker_cost = dec(m.get("maker_fill_cost_dollars"), "maker_fill_cost_dollars", where) or Decimal(0)
        taker_cost = dec(m.get("taker_fill_cost_dollars"), "taker_fill_cost_dollars", where) or Decimal(0)
        fee_parts = [dec(m.get(k), k, where) for k in ("maker_fees_dollars", "taker_fees_dollars")]
        fees = None if all(x is None for x in fee_parts) else sum((x for x in fee_parts if x is not None), Decimal(0))
        ts_upd = self._ts(fr, updated, created)
        common: dict[str, Any] = {"rec": rec, "source": "ws", "precision": "ms"}
        prev = self._orders.get(oid)
        if prev is None:
            ts_c = self._ts(fr, created)
            start = self.cap.first_ns
            initial_u = size_units(m.get("initial_count_fp"), -2, "initial_count_fp", where)
            if start is not None and created is not None and created < start - 1_000_000_000:
                b.event(
                    h,
                    "restate",
                    kind="record_time",
                    ts_exchange=ts_upd,
                    qty=initial,
                    px=px_dec,
                    cum=Decimal(fill).scaleb(-2),
                    leaves=Decimal(rem).scaleb(-2),
                    reason="open before the capture started",
                    **common,
                )
                prev = _OrderState("resting", fill, rem, price, maker_cost, taker_cost, fees)
            else:
                b.event(h, "ack", kind="match_time", ts_exchange=ts_c, qty=initial, px=px_dec, **common)
                prev = _OrderState("resting", 0, initial_u, price, Decimal(0), Decimal(0), Decimal(0))
        now = _OrderState(status, fill, rem, price, maker_cost, taker_cost, fees)
        self._diff(h, oid, prev, now, m, ts_upd, fr, common)
        self._orders[oid] = now

    def _diff(
        self,
        h: int,
        oid: str,
        prev: _OrderState,
        now: _OrderState,
        m: dict[str, Any],
        ts: int,
        fr: Frame,
        common: dict[str, Any],
    ) -> None:
        b = self.cap.builder
        assert b is not None
        reason = m.get("last_update_reason") or ""
        status, fill, rem, price = now.status, now.fill, now.remaining, now.price
        if fill > prev.fill:
            self._derived_orders.add(oid)
            self.cap.count("derived_fills")
            liq = (
                "taker"
                if now.taker_cost > prev.taker_cost
                else "maker"
                if now.maker_cost > prev.maker_cost
                else "unknown"
            )
            fee = now.fees - prev.fees if now.fees is not None and prev.fees is not None else None
            b.event(
                h,
                "fill",
                kind="record_time",
                ts_exchange=ts,
                qty=Decimal(fill - prev.fill).scaleb(-2),
                px=Decimal(price).scaleb(PRICE_EXP),
                liquidity=liq,
                fee=fee,
                fee_ccy="USD" if fee is not None else None,
                derived=True,
                reason="derived from user_order fill_count (no fill message)",
                **common,
            )
        total_prev, total_now = prev.fill + prev.remaining, fill + rem
        if status != "canceled" and price != prev.price:
            b.event(
                h,
                "amend_ack",
                kind="record_time",
                ts_exchange=ts,
                qty=Decimal(total_now).scaleb(-2),
                px=Decimal(price).scaleb(PRICE_EXP),
                keeps=False,
                reason=f"amend: price {prev.price} -> {price} ({reason or 'Amend'})",
                **common,
            )
        elif status != "canceled" and total_now != total_prev:
            b.event(
                h,
                "amend_ack",
                kind="record_time",
                ts_exchange=ts,
                qty=Decimal(total_now).scaleb(-2),
                px=Decimal(price).scaleb(PRICE_EXP),
                keeps=total_now < total_prev,
                reason=f"amend: total {total_prev} -> {total_now} ({reason or 'Amend'})",
                **common,
            )
        if status == prev.status:
            return
        if status == "canceled":
            if reason == "PostOnlyCrossCancel":
                b.event(h, "post_only_reject", kind="record_time", ts_exchange=ts, reason=reason, **common)
            elif reason in _END_REASONS_EXPIRE:
                b.event(h, "expire", kind="record_time", ts_exchange=ts, reason=reason, **common)
            else:
                b.event(h, "cancel_ack", kind="record_time", ts_exchange=ts, reason=reason or None, **common)
        elif status == "executed":
            b.event(h, "done", kind="record_time", ts_exchange=ts, reason="executed", **common)
        elif status not in ("resting", "unknown"):
            self.cap.count(f"user_order_status_{status}")

    def _lifecycle(self, m: dict[str, Any], fr: Frame) -> None:
        ticker = m.get("market_ticker")
        if not isinstance(ticker, str):
            return
        et = m.get("event_type")
        grid = _grid(m.get("price_ranges"), fr.location)  # on `created` and `price_level_structure_updated`
        if grid is not None:
            self._market(ticker).grid = grid
        if et in ("determined", "settled") and m.get("settlement_value") is not None:
            mk = self._market(ticker)
            mk.settlement = req_dec(m.get("settlement_value"), "settlement_value", fr.location)
            ts = m.get("settled_ts") or m.get("determination_ts")
            if ts is not None:
                mk.settled_ns = int(req_dec(ts, "settled_ts", fr.location)) * 1_000_000_000
            mk.meta["result"] = m.get("result")
        self.cap.count(f"lifecycle_{et}")

    # ---- REST ---------------------------------------------------------------------------------------------------

    def _rest(self, obj: dict[str, Any], fr: Frame) -> None:
        where = fr.location
        path = str((fr.request or {}).get("path") or fr.context.get("path") or "")
        if "fills" in obj and isinstance(obj["fills"], list):
            for k, f in enumerate(obj["fills"]):
                self._fill(f, _sub(fr, k, f), source="rest")
        elif "orders" in obj and isinstance(obj["orders"], list):
            for k, o in enumerate(obj["orders"]):
                self._rest_order(o, _sub(fr, k, o))
        elif "order" in obj and isinstance(obj["order"], dict):
            self._rest_order(obj["order"], fr)
        elif "orderbook_fp" in obj or "orderbook" in obj:
            ticker = fr.context.get("ticker") or fr.context.get("market_ticker")
            mpath = _BOOK_PATH.search(path)
            if not ticker and mpath:
                ticker = mpath.group(1)
            if not ticker:
                raise QtError("QT-INPUT-002", f"{where}: orderbook response without a ticker (wrapper or request path)")
            ob = obj.get("orderbook_fp") or obj.get("orderbook") or {}
            levels: list[tuple[int, int, int]] = []
            for raw in ob.get("yes_dollars") or []:
                q = size_units(raw[1], -2, "count", where)
                levels.append((BID, price_units(raw[0], "yes price", where), q))
            for raw in ob.get("no_dollars") or []:
                q = size_units(raw[1], -2, "count", where)
                levels.append((ASK, ONE - price_units(raw[0], "no price", where), q))
            self._market(str(ticker))
            ts = self._ts(fr)
            snap = BookMsg("snapshot", ts, ts, levels, location=where)
            self._rest_books.append((str(ticker), snap, fr.send_ns, fr.file_idx))
            self.cap.count("rest_orderbooks")
        elif "trades" in obj and isinstance(obj["trades"], list):
            for k, t in enumerate(obj["trades"]):
                self._trade(t, _sub(fr, k, t), ws=False)
        elif "queue_positions" in obj and isinstance(obj["queue_positions"], list):
            for k, q in enumerate(obj["queue_positions"]):
                self._sample(q.get("order_id"), q.get("market_ticker"), q.get("queue_position_fp"), fr, f"{where}/{k}")
        elif "queue_position_fp" in obj:
            oid = fr.context.get("order_id")
            mp = _ORDER_PATH.search(path)
            if not oid and mp:
                oid = mp.group(1)
            self._sample(oid, None, obj["queue_position_fp"], fr, where)
        elif "market" in obj and isinstance(obj["market"], dict):
            self._market_meta(obj["market"], where)
        elif "markets" in obj and isinstance(obj["markets"], list):
            for k, mk in enumerate(obj["markets"]):
                self._market_meta(mk, f"{where}/{k}")
        elif "series" in obj and isinstance(obj["series"], dict):
            self._series_meta(obj["series"], where)
        elif "incentive_programs" in obj and isinstance(obj["incentive_programs"], list):
            for k, p in enumerate(obj["incentive_programs"]):
                self._lip_raw.append((p, f"{where}/{k}"))
        else:
            self.cap.count("unrecognised_rest_payloads")

    def _sample(self, oid: Any, market: Any, qp: Any, fr: Frame, where: str) -> None:
        if not isinstance(oid, str) or not oid:
            raise QtError("QT-INPUT-002", f"{where}: queue position without an order id (wrapper order_id or path)")
        if fr.recv_ns is None:
            raise QtError(
                "QT-INPUT-002",
                f"{where}: queue position without a local receive time; capture it as "
                '{"recv_ns": <ns>, "msg": <response>} so it can be placed on the book\'s clock',
            )
        self.cap.samples.append(
            QueueSample(
                oid,
                market if isinstance(market, str) else None,
                size_units(qp, -2, "queue_position_fp", where),
                fr.recv_ns,
                fr.send_ns,
                where,
            )
        )

    def _rest_order(self, o: dict[str, Any], fr: Frame) -> None:
        b = self.cap.builder
        assert b is not None
        where = fr.location
        oid = o.get("order_id")
        if not isinstance(oid, str):
            raise QtError("QT-INPUT-002", f"{where}: order without order_id")
        cid = o.get("client_order_id") if isinstance(o.get("client_order_id"), str) and o["client_order_id"] else None
        if cid:
            self.cap.cids.setdefault(oid, set()).add(cid)
        initial = req_dec(o.get("initial_count_fp"), "initial_count_fp", where)
        h = b.order(
            oid=oid,
            cid=cid,
            record=True,
            qty=initial,
            qty_text=str(o.get("initial_count_fp")),
            px=req_dec(o.get("yes_price_dollars"), "yes_price_dollars", where),
            px_text=str(o.get("yes_price_dollars")),
            raw=None,
            **self._order_fields(o, where),
        )
        created = _rfc(o.get("created_time"), "created_time", where)
        updated = _rfc(o.get("last_update_time"), "last_update_time", where)
        common: dict[str, Any] = {"rec": None, "source": "rest", "precision": "ms", "snapshot": True, "stream": False}
        if created is not None:
            b.event(h, "ack", kind="match_time", ts_exchange=created, qty=initial, **common)
        status = o.get("status")
        if updated is not None and status == "canceled":
            b.event(h, "cancel_ack", kind="record_time", ts_exchange=updated, **common)
        elif updated is not None and status == "executed":
            b.event(h, "done", kind="record_time", ts_exchange=updated, **common)
        self.cap.count("rest_orders")

    def _market_meta(self, mk: dict[str, Any], where: str) -> None:
        ticker = mk.get("ticker")
        if not isinstance(ticker, str):
            raise QtError("QT-INPUT-002", f"{where}: market without ticker")
        info = self._market(ticker)
        grid = _grid(mk.get("price_ranges"), where)
        if grid is not None:
            info.grid = grid
        waiver = _rfc(mk.get("fee_waiver_expiration_time"), "fee_waiver_expiration_time", where)
        if waiver is not None:
            self._waivers[ticker] = waiver
        sv = mk.get("settlement_value_dollars") or mk.get("yes_settlement_value_dollars")
        if sv is not None and mk.get("result") in ("yes", "no", "scalar"):
            info.settlement = req_dec(sv, "settlement_value_dollars", where)
            info.settled_ns = _rfc(mk.get("settlement_ts"), "settlement_ts", where)
            info.meta["result"] = mk.get("result")
        for k in ("event_ticker", "status", "open_time", "close_time", "price_level_structure"):
            if mk.get(k) is not None:
                info.meta[k] = mk.get(k)

    def _series_meta(self, s: dict[str, Any], where: str) -> None:
        ticker = s.get("ticker")
        ft = s.get("fee_type")
        if not isinstance(ticker, str) or ft not in KALSHI_FEE_TYPES:
            raise QtError("QT-INPUT-002", f"{where}: series without ticker or with fee_type {ft!r}")
        if ft == "flat":
            self.cap.warn(
                f"kalshi: series {ticker} has fee_type flat (the fee schedule's Specific Trading Fees Table, not "
                "machine readable): pass its per-contract amounts to score"
            )
            self._flat.add(ticker)
            return
        mult = req_dec(s.get("fee_multiplier", 1), "fee_multiplier", where)
        self._series[ticker] = KalshiFeeSchedule(fee_type=ft, fee_multiplier=mult)

    # ---- finish -------------------------------------------------------------------------------------------------

    def _finish_book(self) -> None:
        """Every market's book messages: the orderbook frames of all inputs merged by receive time (each file in its
        own order), checked per subscription for sessions and sequence numbers (see the module docstring), then the
        REST orderbook responses as checks or as the book."""
        cap = self.cap
        recs = self._book_recs
        timed = all(r.recv is not None for r in recs)
        if timed and recs:
            by_file: dict[int, list[_BookRec]] = {}
            for r in recs:
                by_file.setdefault(r.file_idx, []).append(r)
            recs = list(heapq.merge(*(by_file[k] for k in sorted(by_file)), key=lambda r: r.recv))
        sid_seq: dict[int, int] = {}
        recent: dict[int, dict[int, tuple[Any, ...]]] = {}
        market_sid: dict[str, int] = {}
        sid_markets: dict[int, set[str]] = {}
        ws_markets: set[str] = set()

        def gap(tickers: set[str] | list[str], ts: int, why: str, where: str) -> None:
            for tk in sorted(tickers):
                cap.book[tk].append(BookMsg("gap", ts, ts, location=where, reason=why))

        for r in recs:
            ts = r.recv if r.recv is not None else (r.msg.ts_recv if r.msg is not None else 0)
            if r.kind == "subscribed":
                sid = r.sid
                assert sid is not None
                if sid in sid_seq or sid_markets.get(sid):
                    # the subscription id is answered again: a new session, its sequence restarts
                    cap.count("orderbook_resubscriptions")
                    gap(sid_markets.get(sid, set()), ts, "resubscription", f"subscribed sid {sid}")
                    sid_seq.pop(sid, None)
                    recent.pop(sid, None)
                continue
            assert r.msg is not None
            tk, sid, seq = r.ticker, r.sid, r.seq
            where = r.msg.location
            if sid is not None:
                bound = market_sid.get(tk)
                if bound is not None and bound != sid:
                    if r.kind != "snapshot":
                        cap.count("duplicate_subscription_messages")
                        continue
                    # the market's book now comes from another subscription (a reconnect that got a new sid, or a
                    # second subscription): resync on this snapshot and drop the old subscription's messages
                    cap.count("orderbook_resubscriptions")
                    gap([tk], ts, "resubscription", where)
                    sid_markets[bound].discard(tk)
                market_sid[tk] = sid
                sid_markets.setdefault(sid, set()).add(tk)
                if seq is not None:
                    last = sid_seq.get(sid)
                    if last is not None and seq != last + 1:
                        mine = recent.get(sid, {})
                        if seq <= last and mine.get(seq) == r.content():
                            cap.count("duplicate_book_frames")
                            continue
                        if seq > last:
                            cap.sequence_gaps += 1
                            gap(sid_markets[sid], ts, "sequence gap", where)
                        elif r.kind == "snapshot":
                            cap.count("orderbook_resubscriptions")
                            gap(sid_markets[sid], ts, "resubscription", where)
                            mine.clear()
                        else:
                            cap.sequence_regressions += 1
                            gap(sid_markets[sid], ts, "sequence regression", where)
                            mine.clear()
                    sid_seq[sid] = seq
                    mine = recent.setdefault(sid, {})
                    mine[seq] = r.content()
                    if len(mine) > _RECENT_SEQS:
                        del mine[next(iter(mine))]
            ws_markets.add(tk)
            cap.book[tk].append(r.msg)
        rest: dict[str, list[tuple[BookMsg, int | None, int]]] = {}
        for tk, snap, send, fi in self._rest_books:
            rest.setdefault(tk, []).append((snap, send, fi))
        for tk, lst in rest.items():
            if tk not in ws_markets:
                # the market's only book: the responses in receive-time order
                cap.book[tk] = [m for m, _s, _f in sorted(lst, key=lambda x: (x[0].ts_recv, x[2]))]
                continue
            if not timed:
                cap.count("rest_orderbooks_unchecked", len(lst))
                continue
            book = cap.book[tk]
            keys = [m.ts_recv for m in book]
            ins: list[tuple[int, int, int, BookMsg]] = []
            for n, (m, send, _f) in enumerate(lst):
                t_open = send if send is not None and send <= m.ts_recv else m.ts_recv
                ins.append(
                    (bisect_right(keys, t_open), 0, n, BookMsg("check", t_open, t_open, m.levels, group=("open", n)))
                )
                ins.append(
                    (
                        bisect_right(keys, m.ts_recv),
                        1,
                        n,
                        BookMsg("check", m.ts_recv, m.ts_recv, m.levels, group=("close", n), location=m.location),
                    )
                )
            ins.sort(key=lambda x: (x[0], x[1], x[2]))
            merged: list[BookMsg] = []
            j = 0
            for k, m in enumerate(book):
                while j < len(ins) and ins[j][0] <= k:
                    merged.append(ins[j][3])
                    j += 1
                merged.append(m)
            merged += [x[3] for x in ins[j:]]
            cap.book[tk] = merged

    def finish(self) -> VenueCapture:
        """Resolves the books, fees, programs and warnings; returns the capture."""
        cap = self.cap
        self._finish_book()
        for prints in cap.trades.values():  # the user's own taker prints explain their own tagged decreases
            for t in prints:
                t.own = t.trade_id is not None and t.trade_id in self._taker_trade_ids
        for ticker, info in cap.markets.items():
            series = [s for s in (*self._series, *self._flat) if ticker == s or ticker.startswith(s + "-")]
            if series and max(series, key=len) in self._series:
                base = self._series[max(series, key=len)]
                info.kalshi_fee = KalshiFeeSchedule(
                    fee_type=base.fee_type,
                    fee_multiplier=base.fee_multiplier,
                    fee_waiver_until_ns=self._waivers.get(ticker),
                )
            else:
                # flat series or no series captured: `pm score` decides (flat needs the per-contract amounts)
                info.meta["kalshi_fee_type"] = "flat" if series else None
                info.meta["fee_waiver_until_ns"] = self._waivers.get(ticker)
        for p, where in self._lip_raw:
            if p.get("incentive_type") not in (None, "liquidity"):
                cap.count("incentive_programs_not_liquidity")
                continue
            ticker = p.get("market_ticker")
            if not isinstance(ticker, str):
                raise QtError("QT-INPUT-002", f"{where}: incentive program without market_ticker")
            start, end = _rfc(p.get("start_date"), "start_date", where), _rfc(p.get("end_date"), "end_date", where)
            if start is None or end is None:
                raise QtError("QT-INPUT-002", f"{where}: incentive program without start_date / end_date")
            if p.get("target_size_fp") is None or p.get("discount_factor_bps") is None:
                raise QtError("QT-INPUT-002", f"{where}: liquidity program lacks target_size_fp or discount_factor_bps")
            cap_cc = p.get("max_reward_per_account")
            cap_d = None if cap_cc is None else req_dec(cap_cc, "max_reward_per_account", where) / Decimal(10_000)
            prog = LipProgram(
                market_ticker=ticker,
                start_ns=start,
                end_ns=end,
                period_reward=req_dec(p.get("period_reward"), "period_reward", where) / Decimal(10_000),
                target_size=size_units(p.get("target_size_fp"), -2, "target_size_fp", where),
                discount_factor=req_dec(p.get("discount_factor_bps"), "discount_factor_bps", where) / Decimal(10_000),
                max_reward_per_account=cap_d,
                program_id=str(p.get("id", "")),
            )
            self._market(ticker).lip.append(prog)
        if self._assumed_yes_price and any(cap.book.values()):
            cap.warn(
                "kalshi: use_yes_price unknown for some orderbook messages (no subscribe command captured): NO levels "
                "read in no-leg pricing, the documented default; pass yes_price=true if your subscription set it"
            )
        if cap.sequence_gaps:
            cap.warn(f"kalshi: {cap.sequence_gaps} orderbook seq gap(s): affected books are gapped until a snapshot")
        if cap.sequence_regressions:
            cap.warn(
                f"kalshi: {cap.sequence_regressions} orderbook seq regression(s) without a new snapshot: read as a "
                "new session; affected books are gapped until their next snapshot"
            )
        n = cap.counters.get("orderbook_resubscriptions", 0)
        if n:
            cap.warn(
                f"kalshi: {n} orderbook resubscription(s) (a reconnect, or a market on a second subscription): "
                "affected books are gapped until their next snapshot, and orders resting through it are censored"
            )
        n = cap.counters.get("duplicate_subscription_messages", 0)
        if n:
            cap.warn(
                f"kalshi: {n} orderbook message(s) of markets that another subscription delivers were dropped (a "
                "market is read from one subscription at a time)"
            )
        n = cap.counters.get("duplicate_book_frames", 0)
        if n:
            cap.warn(f"kalshi: {n} duplicated orderbook frame(s) (same subscription, seq and content) were dropped")
        n = cap.counters.get("rest_orderbooks_unchecked", 0)
        if n:
            cap.warn(
                f"kalshi: {n} REST orderbook response(s) of markets the WebSocket delivers were not compared with it "
                "(the WebSocket frames have no receive times)"
            )
        derived = sorted(self._derived_orders - self._fill_order_ids)
        if derived:
            cap.warn(
                f"kalshi: {len(derived)} order(s) filled according to user_order updates without fill messages: "
                "their fills are derived from fill_count (maker / taker from which fill cost grew, at the limit price)"
            )
        return cap


def _grid(ranges: Any, where: str) -> PriceGrid | None:
    """The price grid of a `price_ranges` list (`start`, `end`, `step` dollar strings), or None when absent."""
    if not isinstance(ranges, list) or not ranges:
        return None
    try:
        bands = tuple(
            (
                price_units(r["start"], "price_ranges.start", where),
                price_units(r["end"], "price_ranges.end", where),
                price_units(r["step"], "price_ranges.step", where),
            )
            for r in ranges
        )
    except (KeyError, TypeError):
        raise QtError("QT-INPUT-002", f"{where}: price_ranges items need start, end and step") from None
    return PriceGrid(bands)


def _sub(fr: Frame, k: int, obj: Any) -> Frame:
    return Frame(obj, f"{fr.location}/{k}", fr.file_idx, fr.line_idx, k, fr.recv_ns, fr.send_ns, fr.direction)


def read_kalshi(
    paths: list[str | Path], *, account: str = "default", yes_price: str = "auto", member: str = "direct"
) -> VenueCapture:
    """Reads Kalshi captures and exports (see the module docstring)."""
    r = KalshiReader(account=account, yes_price=yes_price, member=member)
    r.read(paths)
    return r.finish()


__all__ = ["VENUE", "KalshiReader", "read_kalshi"]
