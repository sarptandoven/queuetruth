"""Builders shared by the prediction-market kit tests: hand-made capture files, batches and L2 datasets."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from queuetruth.pm.batches import Batch, BuildStats, TradeMsg
from queuetruth.pm.book import ASK, BID
from queuetruth.pm.dataset import Instrument, L2Arrays, Series, read_l2, write_dataset

REPO = Path(__file__).resolve().parents[3]
FIX = REPO / "tests" / "fixtures" / "pm"
T0 = 1_792_764_000_000_000_000  # 2026-10-23T14:00:00Z in ns
MS = 1_000_000
S = 1_000_000_000
TICKER = "KXTEST-26OCT23-T50"
COND = "0x" + "ab" * 32
YES = "1111"
NO = "2222"


def write_jsonl(path: Path, records: Iterable[Any]) -> Path:
    """One JSON value per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def wrap(recv_ns: int, msg: Any) -> dict[str, Any]:
    """A received frame in the capture wrapper."""
    return {"recv_ns": recv_ns, "msg": msg}


# ---- Kalshi frames ------------------------------------------------------------------------------------------------


def k_subscribe(cmd_id: int, use_yes_price: bool | None = None, t: int = T0 - 50 * MS) -> list[dict[str, Any]]:
    """The subscribe command (`out`) and its `subscribed` answer for the orderbook channel (sid = cmd_id)."""
    params: dict[str, Any] = {"channels": ["orderbook_delta"], "market_ticker": TICKER}
    if use_yes_price is not None:
        params["use_yes_price"] = use_yes_price
    return [
        {"dir": "out", "local_ns": t, "msg": {"id": cmd_id, "cmd": "subscribe", "params": params}},
        wrap(t + 5 * MS, {"id": cmd_id, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": cmd_id}}),
    ]


def k_snapshot(seq: int, t: int, yes: list[tuple[str, str]], no: list[tuple[str, str]], sid: int = 1) -> dict:
    return wrap(
        t + 2 * MS,
        {
            "type": "orderbook_snapshot",
            "sid": sid,
            "seq": seq,
            "sending_ts_ms": t // MS,
            "msg": {
                "market_ticker": TICKER,
                "market_id": "m-1",
                "clock": 1,
                "yes_dollars_fp": [list(x) for x in yes],
                "no_dollars_fp": [list(x) for x in no],
            },
        },
    )


def k_delta(
    seq: int, t: int, side: str, price: str, delta: str, clock: int, cid: str | None = None, sid: int = 1
) -> dict:
    msg: dict[str, Any] = {
        "market_ticker": TICKER,
        "market_id": "m-1",
        "clock": clock,
        "price_dollars": price,
        "delta_fp": delta,
        "side": side,
        "ts_ms": t // MS,
    }
    if cid is not None:
        msg["client_order_id"] = cid
    return wrap(t + 2 * MS, {"type": "orderbook_delta", "sid": sid, "seq": seq, "msg": msg})


def k_trade(t: int, price: str, count: str, taker: str, tid: str, seq: int = 1, block: bool = False) -> dict:
    return wrap(
        t + 1 * MS,
        {
            "type": "trade",
            "sid": 2,
            "seq": seq,
            "msg": {
                "trade_id": tid,
                "market_ticker": TICKER,
                "yes_price_dollars": price,
                "count_fp": count,
                "taker_outcome_side": taker,
                "is_block_trade": block,
                "ts_ms": t // MS,
            },
        },
    )


def k_user_order(
    t: int,
    oid: str,
    cid: str,
    book_side: str,
    price: str,
    initial: str,
    fill: str,
    remaining: str,
    status: str,
    created_ms: int,
    reason: str | None = None,
    maker_cost: str = "0.000000",
    maker_fees: str | None = None,
) -> dict:
    msg = {
        "order_id": oid,
        "user_id": "u-1",
        "ticker": TICKER,
        "status": status,
        "book_side": book_side,
        "yes_price_dollars": price,
        "fill_count_fp": fill,
        "remaining_count_fp": remaining,
        "initial_count_fp": initial,
        "taker_fill_cost_dollars": "0.000000",
        "maker_fill_cost_dollars": maker_cost,
        "client_order_id": cid,
        "created_ts_ms": created_ms,
        "last_updated_ts_ms": t // MS,
    }
    if reason is not None:
        msg["last_update_reason"] = reason
    if maker_fees is not None:
        msg["maker_fees_dollars"], msg["taker_fees_dollars"] = maker_fees, "0.000000"
    return wrap(t + 3 * MS, {"type": "user_order", "sid": 3, "msg": msg})


def k_fill(t: int, tid: str, oid: str, cid: str, book_side: str, price: str, count: str, fee: str) -> dict:
    return wrap(
        t + 2 * MS,
        {
            "type": "fill",
            "sid": 4,
            "msg": {
                "trade_id": tid,
                "order_id": oid,
                "client_order_id": cid,
                "market_ticker": TICKER,
                "is_taker": False,
                "book_side": book_side,
                "yes_price_dollars": price,
                "count_fp": count,
                "fee_cost": fee,
                "ts_ms": t // MS,
            },
        },
    )


# ---- Polymarket frames --------------------------------------------------------------------------------------------


def p_book(t: int, asset: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> dict:
    return wrap(
        t + 2 * MS,
        [
            {
                "event_type": "book",
                "market": COND,
                "asset_id": asset,
                "timestamp": str(t // MS),
                "hash": "0x01",
                "bids": [{"price": p, "size": s} for p, s in bids],
                "asks": [{"price": p, "size": s} for p, s in asks],
            }
        ],
    )


def p_change(t: int, changes: list[tuple[str, str, str, str]], bba: tuple[str, str] | None = None) -> dict:
    """`changes`: (asset, price, new size, BUY | SELL)."""
    out = []
    for asset, price, size, side in changes:
        c = {"asset_id": asset, "price": price, "size": size, "side": side, "hash": "x"}
        if bba is not None:
            c["best_bid"], c["best_ask"] = bba
        out.append(c)
    return wrap(
        t + 3 * MS, {"event_type": "price_change", "market": COND, "price_changes": out, "timestamp": str(t // MS)}
    )


def p_print(t: int, asset: str, price: str, size: str, side: str, tx: str) -> dict:
    return wrap(
        t + 2 * MS,
        {
            "event_type": "last_trade_price",
            "market": COND,
            "asset_id": asset,
            "price": price,
            "size": size,
            "fee_rate_bps": "0",
            "side": side,
            "timestamp": str(t // MS),
            "transaction_hash": tx,
        },
    )


def p_order(t: int, oid: str, side: str, price: str, size: str, matched: str, typ: str, asset: str = YES) -> dict:
    return wrap(
        t + 4 * MS,
        {
            "event_type": "order",
            "id": oid,
            "owner": "key-1",
            "market": COND,
            "asset_id": asset,
            "side": side,
            "original_size": size,
            "size_matched": matched,
            "price": price,
            "type": typ,
            "order_type": "GTC",
            "created_at": str(t // S),
            "timestamp": str(t // MS),
        },
    )


def p_gamma(**over: Any) -> dict[str, Any]:
    g: dict[str, Any] = {
        "conditionId": COND,
        "slug": "test-market",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": json.dumps([YES, NO]),
        "orderPriceMinTickSize": 0.01,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.04, "exponent": 1, "takerOnly": True, "rebateRate": 0.25},
        "rewardsMinSize": 10,
        "rewardsMaxSpread": 3,
        "clobRewards": [{"rewardsDailyRate": 20, "startDate": "2026-10-01", "endDate": "2026-10-31"}],
    }
    g.update(over)
    return g


# ---- batches and datasets -----------------------------------------------------------------------------------------


def resync(ts: int, levels: list[tuple[int, int, int]]) -> Batch:
    return Batch(
        "resync", ts, ts, 0, levels=sorted(levels, key=lambda x: (x[0] != BID, -x[1] if x[0] == BID else x[1]))
    )


def depth(ts: int, changes: list[tuple[int, int, int, int]], trades: list[TradeMsg] | None = None) -> Batch:
    return Batch("depth", ts, ts, 0, changes=list(changes), trades=list(trades or []))


def trade(ts: int, price: int, size: int, aggressor: int, tx: str | None = None) -> TradeMsg:
    return TradeMsg(ts, ts, price, size, aggressor, tx_hash=tx)


def l2_dataset(root: Path, batches: list[Batch], *, size_exp: int = -2, tick: int = 100) -> L2Arrays:
    """Writes the batches (seq 1..) as a one-instrument L2 dataset and reads it back."""
    for k, b in enumerate(batches, start=1):
        b.seq = k
    ins = Instrument(1, "TEST", tick, size_exp)
    write_dataset(root, [Series(ins, batches, BuildStats())], source_format="pm_test", source_venue="kalshi", inputs=[])
    return read_l2(root, 1)


__all__ = [
    "ASK",
    "BID",
    "COND",
    "FIX",
    "MS",
    "NO",
    "REPO",
    "T0",
    "TICKER",
    "YES",
    "S",
    "depth",
    "k_delta",
    "k_fill",
    "k_snapshot",
    "k_subscribe",
    "k_trade",
    "k_user_order",
    "l2_dataset",
    "p_book",
    "p_change",
    "p_gamma",
    "p_order",
    "p_print",
    "resync",
    "trade",
    "wrap",
    "write_jsonl",
]
