"""Polymarket readers: the user's own CLOB WebSocket captures and saved market metadata (docs.polymarket.com, read
2026-10-24). On-chain fills are decoded by `queuetruth.pm.chain`.

Market channel (`wss://ws-subscriptions-clob.polymarket.com/ws/market`; wire frames, or the SDK's `{topic, type,
payload}` events with camelCase keys, which are mapped to the wire names):

- `book` (`asset_id`, `market`, `bids` / `asks` as `{price, size}` lists, `timestamp` ms, `hash`): the token's full
  book. `price_change` (`price_changes[]` of `{asset_id, price, size, side BUY|SELL, best_bid, best_ask}`,
  `timestamp`): new level sizes (`size` 0 removes the level), one batch per message; the stated best bid / ask
  after the change are checked against the rebuilt book. `last_trade_price` (`price`, `size`, `side`,
  `fee_rate_bps`, `transaction_hash`): a print. The docs give `side` only as BUY or SELL; it is read as the taker's
  side and checked twice: against the book (`trades_other_side_explained` counts prints that only a decrease of the
  other side explains) and, with a chain export, against each transaction's taker orders (`prints_side_mismatch`).
  `tick_size_change`, `market_resolved` (`winning_asset_id`) and `new_market` (`assets_ids`, `outcomes`) update the
  market. `best_bid_ask` is redundant and counted. Whether `price_change.size` is the level's new aggregate size (as
  read here) is not stated either; the documented example matches the `book` size of that level, and every
  message's `best_bid` / `best_ask` is compared with the rebuilt book (`bba_mismatch`).
- The canonical instrument of a market is its YES token: index 0 of Gamma's `clobTokenIds` (CTF markets) or
  `positionIds` (Polymarket V2) is YES, index 1 NO ("index 0 is YES and index 1 is NO"), also taken from
  `new_market` (`outcomes` Yes/No) or given as `markets=` mappings. Polymarket matches complementary orders (a NO
  bid at q crosses a YES ask at 1 - q), and each token's book shows the complement's orders mirrored, so one
  token's book is the whole book: the YES token's book is used when it was captured, else the NO token's book
  mirrored (NO bid q -> YES ask 1 - q, NO ask q -> YES bid 1 - q). With both captured, the top levels of the
  other token's book are compared with the mirror after every message (`complement_top_mismatch`). Prints are
  mapped the same way. The prints of the book token (else the YES token) are kept as they are, so equal prints of
  one transaction stay separate; a print on the other token is dropped as the mirror of one of them when an
  unmatched one has the same transaction hash, price, size and side (`duplicate_prints`, one for one), kept when
  none has (`complement_prints_kept`), and skipped without a transaction hash (`complement_prints_skipped`).
- A market seen with a single token and no metadata is read as its own instrument on that token (a warning says
  so); two tokens of one condition without metadata are refused (`QT-INPUT-002`).

User channel (`.../ws/user`): `order` events (`type` PLACEMENT | UPDATE | CANCELLATION, `id`, `asset_id`, `side`,
`price`, `original_size`, `size_matched`, `order_type`, `created_at` s, `timestamp` ms) and `trade` events (`id`,
`status` MATCHED .. CONFIRMED | FAILED, `taker_order_id`, `side`, `size`, `price`, `trader_side`, `owner`,
`maker_orders[]` of `{order_id, owner, maker_address, matched_amount, price, asset_id, side}`, `timestamp` ms,
`transaction_hash`) -> the canonical live log on the YES ladder. A PLACEMENT is an `ack` (match_time), an order
first seen later is a `restate` when it was created before the capture started; CANCELLATION is a `cancel_ack`.
Each trade is a fill once (its first status; fill id `<trade id>:<order id>`): for `trader_side` TAKER the taker
order fills `size` at `price`, for MAKER every `maker_orders` entry that is the user's fills its `matched_amount` at
its own price (an entry is the user's when its `owner` is the event's `owner`, or, when the event names none, when
the user channel reported the order in an `order` event; other makers of the match are never attributed). A trade
that ends FAILED is a `fill_bust`. Maker fills cost 0 ("Makers are never charged fees"); taker fees are left null
unless the chain export gives them.

Market metadata (the user's saved Gamma `GET /markets` JSON, or the rewards markets page): `conditionId`, `slug`,
`outcomes`, `clobTokenIds` / `positionIds`, `version`, `orderPriceMinTickSize`, `feesEnabled`, `feeSchedule`
(`rate`, `exponent`, `takerOnly`, `rebateRate`), `rewardsMinSize`, `rewardsMaxSpread`, `clobRewards[]`
(`rewardsDailyRate`, `startDate`, `endDate`), `negRisk`. Snake-case spellings (`condition_id`, `rewards_config`,
`rate_per_day`) are read too.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live._common import Record, dump_raw
from queuetruth.live.builder import LogBuilder
from queuetruth.pm.batches import BookMsg, TradeMsg
from queuetruth.pm.book import ASK, BID, Ladder
from queuetruth.pm.capture import Frame, iter_frames, order_inputs
from queuetruth.pm.fees import PolymarketFeeSchedule
from queuetruth.pm.rewards import PriceGrid
from queuetruth.pm.units import ONE, POLY_SIZE_EXP, PRICE_EXP, dec, ms_to_ns, price_units, req_dec, s_to_ns, size_units
from queuetruth.pm.venue import MarketInfo, VenueCapture

VENUE = "polymarket"
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")
_SPECIAL = {"conditionId": "market", "orderEventType": "type", "assetIds": "assets_ids", "expiresAt": "expiration"}


def _snake(obj: Any) -> Any:
    """SDK camelCase payload -> wire snake_case keys (recursively)."""
    if isinstance(obj, dict):
        return {_SPECIAL.get(k, _CAMEL.sub("_", k).lower()): _snake(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_snake(v) for v in obj]
    return obj


def _wire(obj: dict[str, Any]) -> dict[str, Any] | None:
    """The wire form of a frame, or None when it is not a market / user event."""
    if "event_type" in obj:
        return obj
    if obj.get("topic") in ("market", "user") and isinstance(obj.get("type"), str) and "payload" in obj:
        p = _snake(obj["payload"])
        if not isinstance(p, dict):
            return None
        out = dict(p)
        etype = obj["type"]
        if obj["topic"] == "user" and etype not in ("order", "trade"):
            return None
        if obj["topic"] == "user" and etype == "trade":
            out.setdefault("type", "TRADE")
        out["event_type"] = etype
        return out
    return None


def _list(v: Any) -> list[Any] | None:
    """A list, or a JSON-encoded list string (Gamma encodes `outcomes` and `clobTokenIds` as strings)."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            out = json.loads(v)
        except json.JSONDecodeError:
            return None
        return out if isinstance(out, list) else None
    return None


@dataclass
class _Market:
    condition: str
    yes: str | None = None
    no: str | None = None
    symbol: str | None = None
    tick: int | None = None
    fee: PolymarketFeeSchedule | None = None
    rewards: list[dict[str, Any]] = field(default_factory=list)
    min_size: Decimal | None = None
    max_spread: Decimal | None = None
    winner: str | None = None
    resolved_ns: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Ev:
    """One parsed market-channel event (kept until YES/NO are known)."""

    kind: str
    condition: str
    asset: str
    ts: int
    recv: int
    data: Any
    where: str
    bba: tuple[int | None, int | None] | None = None


def _record(fr: Frame) -> Record:
    """A live-log record of an own-order frame; `raw` keeps the frame's JSON (the canonical log's `raw` column)."""
    return Record(
        fr.obj, dump_raw(fr.obj), fr.location, fr.file_idx, fr.line_idx * 1000 + fr.sub_idx, recv_ns=fr.recv_ns
    )


class PolymarketReader:
    """Accumulates one ingest's Polymarket inputs (see the module docstring)."""

    def __init__(self, *, account: str = "default", markets: dict[str, tuple[str, str]] | None = None) -> None:
        self.cap = VenueCapture(VENUE)
        self.cap.builder = LogBuilder(VENUE, account=account, price_exp=PRICE_EXP, size_exp=POLY_SIZE_EXP)
        self._markets: dict[str, _Market] = {}
        self._asset_cond: dict[str, str] = {}
        for cond, (yes, no) in (markets or {}).items():
            m = self._mk(cond)
            m.yes, m.no = yes, no
        self._events: list[_Ev] = []
        self._orders: dict[str, dict[str, Any]] = {}
        self._fills_seen: set[str] = set()
        self._trade_status: dict[str, str] = {}
        self._own_trades: list[tuple[dict[str, Any], Frame]] = []
        self.tx_times: dict[str, int] = {}

    def _mk(self, cond: str) -> _Market:
        m = self._markets.get(cond)
        if m is None:
            m = _Market(cond)
            self._markets[cond] = m
        return m

    def _ts(self, fr: Frame, v: Any, what: str) -> int:
        if v is not None and v != "":
            return ms_to_ns(v, what, fr.location)
        if fr.recv_ns is not None:
            return fr.recv_ns
        raise QtError("QT-INPUT-002", f"{fr.location}: event without timestamp and without capture receive time")

    def _note(self, ns: int | None) -> None:
        if ns is not None and (self.cap.first_ns is None or ns < self.cap.first_ns):
            self.cap.first_ns = ns

    # ---- inputs -------------------------------------------------------------------------------------------------

    def read(self, paths: list[str | Path]) -> None:
        """Reads WebSocket captures and metadata files (in time order, `queuetruth.pm.capture.order_inputs`)."""
        ordered = order_inputs(paths)
        self.cap.inputs.extend(ordered)
        for fr in iter_frames(list(ordered)):
            if fr.direction == "out":
                continue
            self._note(fr.recv_ns)
            obj = fr.obj
            if not isinstance(obj, dict):
                self.cap.count("non_object_frames")
                continue
            w = _wire(obj)
            if w is not None:
                self._event(w, fr)
            elif self._metadata(obj, fr):
                continue
            else:
                self.cap.count("unrecognised_frames")

    def _metadata(self, obj: dict[str, Any], fr: Frame) -> bool:
        """Gamma market objects (also inside `{"data": [...]}` pages); True when consumed."""
        if isinstance(obj.get("data"), list):
            used = False
            for k, it in enumerate(obj["data"]):
                if isinstance(it, dict):
                    sub = Frame(it, f"{fr.location}/{k}", fr.file_idx, fr.line_idx, k, fr.recv_ns)
                    used = self._metadata(it, sub) or used
            return used
        cond = obj.get("conditionId") or obj.get("condition_id")
        if not isinstance(cond, str):
            return False
        where = fr.location
        m = self._mk(cond)
        outcomes = _list(obj.get("outcomes"))
        ids = _list(obj.get("positionIds") if obj.get("version") == "v2" else obj.get("clobTokenIds"))
        if ids is None:
            ids = _list(obj.get("clobTokenIds")) or _list(obj.get("positionIds"))
        if ids is not None and len(ids) == 2:
            if outcomes is not None and [str(o).lower() for o in outcomes] != ["yes", "no"]:
                raise QtError("QT-INPUT-002", f"{where}: market {cond} outcomes {outcomes} are not [Yes, No]")
            m.yes, m.no = str(ids[0]), str(ids[1])
        m.symbol = str(obj.get("slug") or obj.get("market_slug") or m.symbol or cond)
        tick = obj.get("orderPriceMinTickSize") or obj.get("minimum_tick_size")
        if tick is not None:
            m.tick = price_units(str(tick), "tick size", where)
        fs = obj.get("feeSchedule") or obj.get("fee_schedule")
        enabled = obj.get("feesEnabled", obj.get("fees_enabled"))
        if isinstance(fs, dict):
            m.fee = PolymarketFeeSchedule(
                fees_enabled=bool(enabled) if enabled is not None else True,
                rate=req_dec(str(fs.get("rate", 0)), "feeSchedule.rate", where),
                exponent=req_dec(str(fs.get("exponent", 1)), "feeSchedule.exponent", where),
                taker_only=bool(fs.get("takerOnly", fs.get("taker_only", True))),
                rebate_rate=req_dec(str(fs.get("rebateRate", fs.get("rebate_rate", 0))), "rebateRate", where),
            )
        elif enabled is False:
            m.fee = PolymarketFeeSchedule(fees_enabled=False)
        ms = obj.get("rewardsMinSize", obj.get("rewards_min_size"))
        if ms is not None:
            m.min_size = req_dec(str(ms), "rewardsMinSize", where)
        sp = obj.get("rewardsMaxSpread", obj.get("rewards_max_spread"))
        if sp is not None:
            m.max_spread = req_dec(str(sp), "rewardsMaxSpread", where)
        for r in obj.get("clobRewards") or obj.get("clob_rewards") or obj.get("rewards_config") or []:
            rate = r.get("rewardsDailyRate", r.get("rewards_daily_rate", r.get("rate_per_day")))
            m.rewards.append(
                {
                    "daily_rate": None if rate is None else format(req_dec(str(rate), "daily rate", where), "f"),
                    "start_date": r.get("startDate", r.get("start_date")),
                    "end_date": r.get("endDate", r.get("end_date")),
                    "asset_address": r.get("assetAddress", r.get("asset_address")),
                }
            )
        for k in ("negRisk", "version", "question", "category"):
            if obj.get(k) is not None:
                m.meta[k] = obj[k]
        self.cap.count("market_metadata")
        return True

    def _event(self, w: dict[str, Any], fr: Frame) -> None:
        et = w.get("event_type")
        where = fr.location
        if et in ("order", "trade"):
            self._user(et, w, fr)
            return
        cond = w.get("market")
        if et == "price_change":
            changes = w.get("price_changes") or []
            ts = self._ts(fr, w.get("timestamp"), "timestamp")
            self._note(ts)
            by_asset: dict[str, list[dict[str, Any]]] = {}
            for c in changes:
                by_asset.setdefault(str(c.get("asset_id")), []).append(c)
            for asset, lst in by_asset.items():
                cnd = str(cond) if isinstance(cond, str) else self._asset_cond.get(asset, "")
                self._asset_cond.setdefault(asset, cnd)
                last = lst[-1]
                bb, ba = last.get("best_bid"), last.get("best_ask")
                bba = None if bb is None and ba is None else (_opt_price(bb, where), _opt_price(ba, where))
                self._events.append(_Ev("price_change", cnd, asset, ts, fr.recv_ns or ts, lst, where, bba))
            self.cap.count("price_change")
            return
        asset = w.get("asset_id")
        if et in ("book", "last_trade_price", "tick_size_change", "market_resolved", "new_market", "best_bid_ask"):
            if et == "new_market":
                ids = _list(w.get("assets_ids"))
                outs = _list(w.get("outcomes"))
                if isinstance(cond, str) and ids and outs and [str(o).lower() for o in outs] == ["yes", "no"]:
                    m = self._mk(cond)
                    m.yes, m.no = str(ids[0]), str(ids[1])
                self.cap.count("new_market")
                return
            if et == "market_resolved":
                if isinstance(cond, str) and w.get("winning_asset_id") is not None:
                    m = self._mk(cond)
                    m.winner = str(w["winning_asset_id"])
                    m.resolved_ns = self._ts(fr, w.get("timestamp"), "timestamp")
                self.cap.count("market_resolved")
                return
            if et == "best_bid_ask":
                self.cap.count("best_bid_ask")
                return
            if not isinstance(asset, str) and not isinstance(asset, int):
                raise QtError("QT-INPUT-002", f"{where}: {et} without asset_id")
            asset = str(asset)
            cnd = str(cond) if isinstance(cond, str) else self._asset_cond.get(asset, "")
            self._asset_cond.setdefault(asset, cnd)
            ts = self._ts(fr, w.get("timestamp"), "timestamp")
            self._note(ts)
            self._events.append(_Ev(et, cnd, asset, ts, fr.recv_ns or ts, w, where))
            self.cap.count(et)
            if et == "last_trade_price" and w.get("transaction_hash"):
                self.tx_times.setdefault(str(w["transaction_hash"]).lower(), ts)
            return
        self.cap.count(f"skipped_{et}")

    # ---- user channel ---------------------------------------------------------------------------------------------

    def _user(self, et: str, w: dict[str, Any], fr: Frame) -> None:
        if et == "order":
            oid = w.get("id")
            if not isinstance(oid, str):
                raise QtError("QT-INPUT-002", f"{fr.location}: order event without id")
            self._orders.setdefault(oid, {"events": []})["events"].append((w, fr))
            self._asset_cond.setdefault(str(w.get("asset_id")), str(w.get("market") or ""))
        else:
            self._own_trades.append((w, fr))
            if w.get("transaction_hash"):
                ts = self._ts(fr, w.get("timestamp"), "timestamp")
                self.tx_times.setdefault(str(w["transaction_hash"]).lower(), ts)

    # ---- finish ----------------------------------------------------------------------------------------------------

    def _canon(self, asset: str) -> tuple[str, bool] | None:
        """(condition, is_yes) of an asset, or None when unknown."""
        cond = self._asset_cond.get(asset)
        m = self._markets.get(cond) if cond else None
        if m is None:
            for c, mm in self._markets.items():
                if asset in (mm.yes, mm.no):
                    m, cond = mm, c
                    break
        if m is None or cond is None:
            return None
        if asset == m.yes:
            return cond, True
        if asset == m.no:
            return cond, False
        return None

    def finish(self) -> VenueCapture:
        """Resolves YES / NO per market and emits book messages, prints and the own-order log."""
        cap = self.cap
        assets_by_cond: dict[str, set[str]] = {}
        for ev in self._events:
            assets_by_cond.setdefault(ev.condition, set()).add(ev.asset)
        for cond, assets in assets_by_cond.items():
            m = self._mk(cond) if cond else None
            if m is not None and (m.yes is None or m.no is None):
                if len(assets) > 1:
                    raise QtError(
                        "QT-INPUT-002",
                        f"polymarket: market {cond or '?'} has events for {len(assets)} tokens but no YES / NO "
                        "mapping: pass the market's Gamma metadata (clobTokenIds / positionIds) or a COND=YES,NO map",
                    )
                (only,) = tuple(assets)
                m.yes = only
                cap.warn(
                    f"polymarket: market {cond or '?'} read on token {only[:12]}.. as its own instrument (no "
                    "metadata): settlement and prices are that token's"
                )
        book_asset: dict[str, str] = {}
        for ev in self._events:
            if ev.kind in ("book", "price_change"):
                c = self._canon(ev.asset)
                if c is not None and c[1]:
                    book_asset[c[0]] = ev.asset
        for ev in self._events:
            if ev.kind in ("book", "price_change"):
                c = self._canon(ev.asset)
                if c is not None and c[0] not in book_asset:
                    book_asset[c[0]] = ev.asset
        symbols: dict[str, str] = {}
        for cond, m in self._markets.items():
            if m.yes is None:
                continue
            sym = m.symbol or cond or m.yes
            symbols[cond] = sym
            info = cap.markets.setdefault(sym, MarketInfo(sym, VENUE, POLY_SIZE_EXP, "pUSD"))
            info.condition_id, info.yes_asset, info.no_asset = cond, m.yes, m.no
            if m.tick is not None:
                info.grid = PriceGrid.uniform(m.tick)
            info.poly_fee = m.fee
            info.poly_rewards = list(m.rewards)
            if m.min_size is not None:
                info.meta["rewards_min_size"] = format(m.min_size, "f")
            if m.max_spread is not None:
                info.meta["rewards_max_spread"] = format(m.max_spread, "f")
            if m.winner is not None:
                info.settlement = Decimal(1) if m.winner == m.yes else Decimal(0) if m.winner == m.no else None
                info.settled_ns = m.resolved_ns
            info.meta.update(m.meta)
            cap.book.setdefault(sym, [])
            cap.trades.setdefault(sym, [])
        # prints: the primary token's (the book token, else YES) as they are; the other token's only where no
        # primary print of the same transaction, price, size and side is left to mirror (multiset matching)
        primary = {cond: book_asset.get(cond) or m.yes for cond, m in self._markets.items()}
        mirrors: Counter[tuple[str, str, int, int, int]] = Counter()
        for ev in self._events:
            if ev.kind == "last_trade_price" and ev.data.get("transaction_hash"):
                c = self._canon(ev.asset)
                if c is not None and ev.asset == primary.get(c[0]):
                    t = self._print(ev, c[1])
                    mirrors[(c[0], str(t.tx_hash), t.price, t.size, t.aggressor)] += 1
        canon_books: dict[str, Ladder] = {}
        mirror_books: dict[str, Ladder] = {}
        touched: set[str] = set()
        for i, ev in enumerate(self._events):
            c = self._canon(ev.asset)
            if c is None:
                cap.count("events_unknown_market")
                continue
            cond, is_yes = c
            sym = symbols[cond]
            if ev.kind == "tick_size_change":
                new = price_units(str(ev.data.get("new_tick_size")), "new_tick_size", ev.where)
                info = cap.markets[sym]
                old = info.grid.finest if info.grid else None
                info.grid = PriceGrid.uniform(min(new, old) if old else new)
                cap.count("tick_size_changes")
            elif ev.kind == "last_trade_price":
                t = self._print(ev, is_yes)
                key = (cond, str(t.tx_hash), t.price, t.size, t.aggressor)
                if ev.asset == primary.get(cond):
                    cap.trades[sym].append(t)
                elif t.tx_hash is None:
                    cap.count("complement_prints_skipped")
                elif mirrors[key] > 0:
                    mirrors[key] -= 1
                    cap.count("duplicate_prints")
                else:
                    cap.trades[sym].append(t)
                    cap.count("complement_prints_kept")
            else:
                msg = self._book_msg(ev, is_yes)
                own_book = ev.asset == book_asset.get(cond)
                _apply(canon_books if own_book else mirror_books, cond, msg)
                touched.add(cond)
                if own_book:
                    cap.book[sym].append(msg)
                else:
                    cap.count("complement_book_messages")
            nxt = self._events[i + 1].where.split("[")[0] if i + 1 < len(self._events) else None
            if nxt != ev.where.split("[")[0]:
                for cnd in touched:
                    a, m = canon_books.get(cnd), mirror_books.get(cnd)
                    if a is not None and m is not None and len(a) and len(m):
                        if (a.best(BID), a.best(ASK)) != (m.best(BID), m.best(ASK)):
                            cap.count("complement_top_mismatch")
                touched.clear()
        self._own(symbols)
        return cap

    def _levels(self, raw: list[Any], side_is_bid: bool, is_yes: bool, where: str) -> list[tuple[int, int, int]]:
        out = []
        for lv in raw:
            p = price_units(str(lv.get("price")), "price", where)
            q = size_units(str(lv.get("size")), POLY_SIZE_EXP, "size", where)
            if is_yes:
                out.append((BID if side_is_bid else ASK, p, q))
            else:
                out.append((ASK if side_is_bid else BID, ONE - p, q))
        return out

    def _book_msg(self, ev: _Ev, is_yes: bool) -> BookMsg:
        if ev.kind == "book":
            lv = self._levels(ev.data.get("bids") or [], True, is_yes, ev.where)
            lv += self._levels(ev.data.get("asks") or [], False, is_yes, ev.where)
            return BookMsg("snapshot", ev.ts, ev.recv, lv, location=ev.where)
        levels = []
        for c in ev.data:
            side = c.get("side")
            if side not in ("BUY", "SELL"):
                raise QtError("QT-INPUT-002", f"{ev.where}: price_change side {side!r}")
            levels += self._levels([c], side == "BUY", is_yes, ev.where)
        bba = ev.bba
        if bba is not None and not is_yes:
            bb, ba = bba
            bba = (None if ba is None else ONE - ba, None if bb is None else ONE - bb)
        return BookMsg("set", ev.ts, ev.recv, levels, location=ev.where, bba=bba)

    def _print(self, ev: _Ev, is_yes: bool) -> TradeMsg:
        d = ev.data
        p = price_units(str(d.get("price")), "price", ev.where)
        q = size_units(str(d.get("size")), POLY_SIZE_EXP, "size", ev.where)
        side = d.get("side")
        if side not in ("BUY", "SELL"):
            raise QtError("QT-INPUT-002", f"{ev.where}: last_trade_price side {side!r}")
        buy_yes = (side == "BUY") == is_yes
        return TradeMsg(
            ev.ts,
            ev.recv,
            p if is_yes else ONE - p,
            q,
            BID if buy_yes else ASK,
            trade_id=None,
            tx_hash=str(d.get("transaction_hash")).lower() if d.get("transaction_hash") else None,
            fee_rate_bps=str(d.get("fee_rate_bps")) if d.get("fee_rate_bps") is not None else None,
            location=ev.where,
        )

    def _own(self, symbols: dict[str, str]) -> None:
        b = self.cap.builder
        assert b is not None
        start = self.cap.first_ns

        def canon(asset: str, side: str, price: Decimal, where: str) -> tuple[str, str, Decimal]:
            c = self._canon(asset)
            if c is None:
                raise QtError("QT-INPUT-002", f"{where}: own order on unknown token {asset}")
            cond, is_yes = c
            buy = side.upper() == "BUY"
            if is_yes:
                return symbols[cond], "buy" if buy else "sell", price
            return symbols[cond], "sell" if buy else "buy", Decimal(1) - price

        for oid, rec in self._orders.items():
            evs = rec["events"]
            w0, fr0 = evs[0]
            where = fr0.location
            sym, side, px = canon(
                str(w0.get("asset_id")), str(w0.get("side")), req_dec(w0.get("price"), "price", where), where
            )
            otype = str(w0.get("order_type") or "").upper()
            tif = {"GTC": "gtc", "GTD": "gtd", "FOK": "fok", "FAK": "ioc"}.get(otype, "other")
            qty = req_dec(w0.get("original_size"), "original_size", where)
            h = b.order(
                oid=oid,
                rec=_record(fr0),
                record=True,
                qty=qty,
                qty_text=str(w0.get("original_size")),
                px=px,
                px_text=format(px, "f"),
                instrument=sym,
                side=side,
                order_type="limit",
                tif=tif,
                post_only=None,
            )
            for k, (w, fr) in enumerate(evs):
                ts = self._ts(fr, w.get("timestamp"), "timestamp")
                common: dict[str, Any] = {"rec": _record(fr), "source": "ws", "precision": "ms", "ts_exchange": ts}
                typ = str(w.get("type") or "").upper()
                if typ == "PLACEMENT":
                    b.event(h, "ack", kind="match_time", qty=qty, px=px, **common)
                elif k == 0:
                    created = w.get("created_at")
                    created_ns = s_to_ns(created, "created_at", fr.location) if created not in (None, "") else None
                    if created_ns is not None and start is not None and created_ns < start - 1_000_000_000:
                        matched = dec(w.get("size_matched"), "size_matched", fr.location) or Decimal(0)
                        b.event(
                            h,
                            "restate",
                            kind="record_time",
                            qty=qty,
                            px=px,
                            cum=matched,
                            leaves=qty - matched,
                            reason="open before the capture started",
                            **common,
                        )
                    else:
                        b.event(h, "ack", kind="record_time", qty=qty, px=px, reason="first seen as " + typ, **common)
                if typ == "CANCELLATION":
                    b.event(h, "cancel_ack", kind="record_time", **common)
                elif typ not in ("PLACEMENT", "UPDATE"):
                    self.cap.count(f"order_event_{typ.lower() or 'untyped'}")
        for w, fr in self._own_trades:
            where = fr.location
            tid = str(w.get("id"))
            status = str(w.get("status") or "").upper()
            ts = self._ts(fr, w.get("timestamp"), "timestamp")
            owner = w.get("owner")
            fills: list[tuple[str, str, Decimal, Decimal, str]] = []
            if str(w.get("trader_side") or "").upper() == "TAKER":
                oid = w.get("taker_order_id")
                if isinstance(oid, str):
                    fills.append(
                        (
                            oid,
                            str(w.get("asset_id")),
                            req_dec(w.get("size"), "size", where),
                            req_dec(w.get("price"), "price", where),
                            str(w.get("side")),
                        )
                    )
                liq = "taker"
            else:
                for mo in w.get("maker_orders") or []:
                    # the user's own maker orders: the event's owner (API key) when it names one, else orders the
                    # user channel reported through order events (never every maker of the match)
                    mine = mo.get("owner") == owner if owner is not None else str(mo.get("order_id")) in self._orders
                    if not mine:
                        continue
                    fills.append(
                        (
                            str(mo.get("order_id")),
                            str(mo.get("asset_id")),
                            req_dec(mo.get("matched_amount"), "matched_amount", where),
                            req_dec(mo.get("price"), "price", where),
                            str(mo.get("side")),
                        )
                    )
                liq = "maker"
            for oid, asset, qty, price, side in fills:
                fid = f"{tid}:{oid}"
                sym, cside, cpx = canon(asset, side, price, where)
                h = b.order(oid=oid, instrument=sym, side=cside)
                common = {"rec": _record(fr), "source": "ws", "precision": "ms", "ts_exchange": ts}
                if status == "FAILED":
                    if fid in self._fills_seen:
                        b.event(h, "fill_bust", kind="match_time", ref_fill_id=fid, reason="trade FAILED", **common)
                    continue
                if fid in self._fills_seen:
                    self.cap.count("trade_status_updates")
                    continue
                self._fills_seen.add(fid)
                b.event(
                    h,
                    "fill",
                    kind="match_time",
                    qty=qty,
                    px=cpx,
                    fill_id=fid,
                    liquidity=liq,
                    fee=Decimal(0) if liq == "maker" else None,
                    fee_ccy="pUSD" if liq == "maker" else None,
                    reason=f"trade {status}"
                    + (f" tx {w.get('transaction_hash')}" if w.get("transaction_hash") else ""),
                    **common,
                )
                self.cap.count("own_fills")


def _apply(books: dict[str, Ladder], cond: str, msg: BookMsg) -> None:
    """Applies a canonical book message to a replica ladder (snapshot replaces, set levels overwrite)."""
    bk = books.setdefault(cond, Ladder())
    if msg.kind == "snapshot":
        bk.clear()
    for s, p, q in msg.levels:
        bk.set(s, p, max(q, 0))


def _opt_price(v: Any, where: str) -> int | None:
    if v is None or v == "":
        return None
    return price_units(str(v), "best price", where)


def read_polymarket(
    paths: list[str | Path], *, account: str = "default", markets: dict[str, tuple[str, str]] | None = None
) -> tuple[VenueCapture, dict[str, int]]:
    """Reads Polymarket captures and metadata; returns the capture and the WebSocket times of transaction hashes
    (from `last_trade_price` and own trades), used to place on-chain matches on the book's clock."""
    r = PolymarketReader(account=account, markets=markets)
    r.read(paths)
    return r.finish(), dict(r.tx_times)


__all__ = ["VENUE", "PolymarketReader", "read_polymarket"]
