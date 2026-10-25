"""Polymarket exchange events decoded from the user's own Polygon log export (`eth_getLogs` results).

Event ABIs are taken from the public contract sources (interface facts only; no contract code is used):

- CTF Exchange v1 (MIT, `Polymarket/ctf-exchange`, `src/exchange/interfaces/ITrading.sol`, `IFees.sol`):
  `OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker, uint256 makerAssetId,
  uint256 takerAssetId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee)`,
  `OrdersMatched(bytes32 indexed takerOrderHash, address indexed takerOrderMaker, uint256 makerAssetId, uint256
  takerAssetId, uint256 makerAmountFilled, uint256 takerAmountFilled)`, `OrderCancelled(bytes32 indexed
  orderHash)`, `FeeCharged(address indexed receiver, uint256 tokenId, uint256 amount)`.
- CTF Exchange v2 (`Polymarket/ctf-exchange-v2`, `ITrading.sol`, `mixins/Events.sol`; live since CLOB V2 on
  2026-04-28) and the Protocol V2 Exchange (`Polymarket/polymarket-v2-external`, `src/exchange/Exchange.sol`; same
  encoding, `PositionId` is a uint256): `OrderFilled(bytes32 indexed orderHash, address indexed maker, address
  indexed taker, uint8 side, uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee,
  bytes32 builder, bytes32 metadata)`, `OrdersMatched(bytes32 indexed takerOrderHash, address indexed
  takerOrderMaker, uint8 side, uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled)`,
  `FeeCharged(address indexed receiver, uint256 amount)`.

Semantics used by the reconstruction (from the same sources): one `matchOrders` call emits, in log order, one
`OrderFilled` per maker order in the operator's `makerOrders[]` order with `taker` = the taker order's maker wallet,
then the taker's own `OrderFilled` with `taker` = the exchange contract, then `OrdersMatched`. `makerAmountFilled`
is what the order's maker gave and `takerAmountFilled` what it received: a BUY gives collateral and receives
outcome tokens, a SELL the reverse (v1 tells the side by which asset id is 0, v2 by the `side` field). Amounts have
6 decimals; the fill price is collateral / tokens. v1 charges the fee on proceeds (tokens for a BUY, collateral for
a SELL); v2 charges collateral. A v1 `OrderFilled` with no `OrdersMatched` after it in its transaction is a direct
operator fill (`fillOrder`), tagged `operator_fill`.

Inputs: JSON (`eth_getLogs` result arrays, JSON-RPC envelopes `{"result": [...]}`, or JSON lines of logs; hex or
integer numbers; an optional `blockTimestamp`) or CSV with columns `block_number, tx_hash, log_index, address,
topic0..topic3, data` (and optional `block_time`, epoch seconds or ISO 8601). Logs flagged `removed` (reorged) are
dropped and counted; logs whose topic 0 is not an exchange event are counted and skipped.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.pm.capture import iter_frames
from queuetruth.pm.keccak import topic

V1, V2 = "v1", "v2"
SIG = {
    ("OrderFilled", V1): "OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)",
    ("OrdersMatched", V1): "OrdersMatched(bytes32,address,uint256,uint256,uint256,uint256)",
    ("OrderCancelled", V1): "OrderCancelled(bytes32)",
    ("FeeCharged", V1): "FeeCharged(address,uint256,uint256)",
    ("OrderFilled", V2): "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)",
    ("OrdersMatched", V2): "OrdersMatched(bytes32,address,uint8,uint256,uint256,uint256)",
    ("FeeCharged", V2): "FeeCharged(address,uint256)",
}
#: topic0 -> (event name, ABI version, number of indexed topics after topic0, number of data words).
TOPICS: dict[str, tuple[str, str, int, int]] = {}
_SHAPE = {
    ("OrderFilled", V1): (3, 5),
    ("OrdersMatched", V1): (2, 4),
    ("OrderCancelled", V1): (1, 0),
    ("FeeCharged", V1): (1, 2),
    ("OrderFilled", V2): (3, 7),
    ("OrdersMatched", V2): (2, 4),
    ("FeeCharged", V2): (1, 1),
}
for _k, _sig in SIG.items():
    TOPICS[topic(_sig)] = (_k[0], _k[1], *_SHAPE[_k])

#: Polygon exchange contracts: the three current ones from docs.polymarket.com/resources/contracts (read
#: 2026-10-24); the v1 CTF Exchange from the archived `Polymarket/ctf-exchange` README and the v1 Neg Risk CTF
#: Exchange from the chain-137 contract config of `Polymarket/py-clob-client` (`py_clob_client/config.py`, read
#: 2026-10-24), both settling the CLOB until CLOB V2 (2026-04-28).
KNOWN_EXCHANGES: dict[str, str] = {
    "0xe111180000d2663c0091e4f400237545b87b996b": "ctf_exchange_v2",
    "0xe2222d279d744050d28e00520010520000310f59": "neg_risk_ctf_exchange_v2",
    "0xe3333700ca9d93003f00f0f71f8515005f6c00aa": "protocol_v2_exchange",
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e": "ctf_exchange_v1",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a": "neg_risk_ctf_exchange_v1",
}


@dataclass(frozen=True)
class RawLog:
    """One log entry with numbers parsed."""

    address: str
    topics: tuple[str, ...]
    data: bytes
    block: int
    tx: str
    log_index: int
    block_ts_ns: int | None
    location: str


@dataclass(frozen=True)
class Fill:
    """One decoded `OrderFilled`: the order, its wallet, side and amounts (6-decimal integers)."""

    tx: str
    block: int
    log_index: int
    block_ts_ns: int | None
    exchange: str
    version: str
    order_hash: str
    maker: str
    taker: str
    side: str  # "BUY" | "SELL" of the order's own maker
    token_id: int
    collateral: int
    tokens: int
    fee: int
    fee_asset: str  # "collateral" | "token"
    builder: str | None = None
    metadata: str | None = None
    role: str = "maker"  # maker | taker | operator_fill
    match_idx: int | None = None
    maker_pos: int | None = None


@dataclass(frozen=True)
class Match:
    """One decoded `OrdersMatched` with its taker and maker fills (makers in operator order)."""

    tx: str
    block: int
    log_index: int
    block_ts_ns: int | None
    exchange: str
    version: str
    match_idx: int
    taker_order_hash: str
    taker_wallet: str
    taker_side: str
    taker_token_id: int
    taker_collateral: int
    taker_tokens: int
    makers: tuple[Fill, ...]
    taker_fill: Fill | None


@dataclass
class ChainDecode:
    """Decoded events and counters of one log export."""

    fills: list[Fill] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    cancels: list[tuple[str, int, int, str]] = field(default_factory=list)  # (tx, block, log_index, order_hash)
    fee_events: int = 0
    logs: int = 0
    removed: int = 0
    other_events: int = 0
    duplicate_logs: int = 0
    operator_fills: int = 0
    unknown_exchanges: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _int(v: Any, what: str, where: str) -> int:
    if isinstance(v, bool) or v is None:
        raise QtError("QT-INPUT-002", f"{where}: {what} missing or not a number: {v!r}")
    if isinstance(v, int):
        return v
    s = str(v).strip()
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except ValueError:
        raise QtError("QT-INPUT-002", f"{where}: {what} {v!r} is not an integer") from None


def _hex(v: Any, what: str, where: str, nbytes: int | None = None) -> str:
    s = str(v).strip().lower() if v is not None else ""
    if not s.startswith("0x"):
        raise QtError("QT-INPUT-002", f"{where}: {what} {v!r} is not 0x-prefixed hex")
    body = s[2:]
    if len(body) % 2 or any(c not in "0123456789abcdef" for c in body):
        raise QtError("QT-INPUT-002", f"{where}: {what} {v!r} is not hex")
    if nbytes is not None and len(body) != 2 * nbytes:
        raise QtError("QT-INPUT-002", f"{where}: {what} must be {nbytes} bytes, got {len(body) // 2}")
    return s


def _ts(v: Any, where: str) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, str) and ("T" in v or "-" in v[1:]):
        try:
            dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace(" UTC", "+00:00"))
        except ValueError:
            raise QtError("QT-INPUT-002", f"{where}: block time {v!r} is not ISO 8601") from None
        if dt.tzinfo is None:
            raise QtError("QT-INPUT-002", f"{where}: block time {v!r} has no time zone")
        return int(dt.timestamp()) * 1_000_000_000
    return _int(v, "block time", where) * 1_000_000_000


def _log_from_obj(o: Any, where: str) -> RawLog | None:
    if not isinstance(o, dict) or "topics" not in o:
        raise QtError("QT-INPUT-002", f"{where}: not an eth_getLogs entry (no topics)")
    if o.get("removed") is True:
        return None
    topics = o.get("topics")
    if not isinstance(topics, list) or not topics:
        raise QtError("QT-INPUT-002", f"{where}: topics must be a non-empty list")
    return RawLog(
        address=_hex(o.get("address"), "address", where, 20),
        topics=tuple(_hex(t, "topic", where, 32) for t in topics),
        data=bytes.fromhex(_hex(o.get("data", "0x"), "data", where)[2:]),
        block=_int(o.get("blockNumber"), "blockNumber", where),
        tx=_hex(o.get("transactionHash"), "transactionHash", where, 32),
        log_index=_int(o.get("logIndex"), "logIndex", where),
        block_ts_ns=_ts(o.get("blockTimestamp", o.get("timeStamp")), where),
        location=where,
    )


def _csv_logs(path: Path) -> tuple[list[RawLog], int]:
    """(logs, removed count) of a CSV log export."""
    if not path.is_file():
        raise QtError("QT-INPUT-001", f"{path}: input not found")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise QtError("QT-INPUT-001", f"{path}: {e}") from None
    rows = list(csv.DictReader(io.StringIO(text)))
    need = {"block_number", "tx_hash", "log_index", "address", "topic0", "data"}
    if rows and not need <= set(rows[0]):
        raise QtError("QT-INPUT-002", f"{path}: CSV log export needs columns {sorted(need)}")
    out: list[RawLog] = []
    removed = 0
    for n, r in enumerate(rows, start=2):
        if str(r.get("removed") or "").strip().lower() == "true":
            removed += 1
            continue
        topics = [r[k] for k in ("topic0", "topic1", "topic2", "topic3") if r.get(k)]
        obj = {
            "address": r["address"],
            "topics": topics,
            "data": r["data"] or "0x",
            "blockNumber": r["block_number"],
            "transactionHash": r["tx_hash"],
            "logIndex": r["log_index"],
            "blockTimestamp": r.get("block_time") or None,
        }
        lg = _log_from_obj(obj, f"{path}:{n}")
        if lg is not None:
            out.append(lg)
    return out, removed


def read_logs(paths: list[str | Path]) -> tuple[list[RawLog], int, int]:
    """(logs sorted by (block, log index), removed count, duplicate count) of every export."""
    out: list[RawLog] = []
    removed = 0
    for p in paths:
        path = Path(p)
        if path.suffix.lower() == ".csv":
            logs, rm = _csv_logs(path)
            out.extend(logs)
            removed += rm
            continue
        for fr in iter_frames([path]):
            if fr.direction != "in":
                continue
            obj = fr.obj
            items = obj.get("result") if isinstance(obj, dict) and "result" in obj else obj
            for k, item in enumerate(items if isinstance(items, list) else [items]):
                lg = _log_from_obj(item, fr.location if not isinstance(items, list) else f"{fr.location}/{k}")
                if lg is None:
                    removed += 1
                else:
                    out.append(lg)
    out.sort(key=lambda g: (g.block, g.log_index))
    dedup: list[RawLog] = []
    dups = 0
    for lg in out:
        if dedup and (dedup[-1].block, dedup[-1].log_index) == (lg.block, lg.log_index):
            if dedup[-1].tx != lg.tx or dedup[-1].data != lg.data or dedup[-1].topics != lg.topics:
                raise QtError(
                    "QT-INPUT-002",
                    f"{lg.location}: block {lg.block} log {lg.log_index} appears twice with different contents",
                )
            dups += 1
            continue
        dedup.append(lg)
    return dedup, removed, dups


def _words(data: bytes, n: int, where: str) -> list[int]:
    if len(data) != 32 * n:
        raise QtError("QT-INPUT-002", f"{where}: event data has {len(data)} bytes, expected {32 * n}")
    return [int.from_bytes(data[32 * i : 32 * i + 32], "big") for i in range(n)]


def _addr(topic_hex: str) -> str:
    return "0x" + topic_hex[-40:]


def _b32(v: int) -> str:
    return "0x" + v.to_bytes(32, "big").hex()


def _decode_fill(lg: RawLog, version: str) -> Fill:
    where = lg.location
    order_hash, maker, taker = lg.topics[1], _addr(lg.topics[2]), _addr(lg.topics[3])
    if version == V1:
        maker_asset, taker_asset, making, taking, fee = _words(lg.data, 5, where)
        if (maker_asset == 0) == (taker_asset == 0):
            raise QtError(
                "QT-INPUT-002",
                f"{where}: v1 OrderFilled with asset ids {maker_asset}/{taker_asset}: exactly one must be 0 "
                "(collateral)",
            )
        if maker_asset == 0:
            side, token, collateral, tokens, fee_asset = "BUY", taker_asset, making, taking, "token"
        else:
            side, token, collateral, tokens, fee_asset = "SELL", maker_asset, taking, making, "collateral"
        builder = metadata = None
    else:
        side_w, token, making, taking, fee, builder_w, metadata_w = _words(lg.data, 7, where)
        if side_w not in (0, 1):
            raise QtError("QT-INPUT-002", f"{where}: OrderFilled side {side_w} is not 0 (BUY) or 1 (SELL)")
        side = "BUY" if side_w == 0 else "SELL"
        collateral, tokens = (making, taking) if side == "BUY" else (taking, making)
        fee_asset = "collateral"
        builder, metadata = _b32(builder_w), _b32(metadata_w)
    role = "taker" if taker == lg.address else "maker"
    return Fill(
        tx=lg.tx,
        block=lg.block,
        log_index=lg.log_index,
        block_ts_ns=lg.block_ts_ns,
        exchange=lg.address,
        version=version,
        order_hash=order_hash,
        maker=maker,
        taker=taker,
        side=side,
        token_id=token,
        collateral=collateral,
        tokens=tokens,
        fee=fee,
        fee_asset=fee_asset,
        builder=builder,
        metadata=metadata,
        role=role,
    )


def decode(paths: list[str | Path], exchanges: Iterable[str] | None = None) -> ChainDecode:
    """Decodes every exchange event of the exports. `exchanges` restricts decoding to those contract addresses
    (default: every log whose topic 0 is an exchange event; contracts outside `KNOWN_EXCHANGES` are reported in
    `unknown_exchanges`)."""
    logs, removed, dups = read_logs(paths)
    allow = {a.lower() for a in exchanges} if exchanges is not None else None
    res = ChainDecode(logs=len(logs), removed=removed, duplicate_logs=dups)
    unknown: set[str] = set()
    by_tx: dict[str, list[tuple[RawLog, str, str]]] = {}
    tx_order: list[str] = []
    for lg in logs:
        meta = TOPICS.get(lg.topics[0])
        if meta is None or (allow is not None and lg.address not in allow):
            res.other_events += 1
            continue
        name, version, n_idx, _n_words = meta
        if len(lg.topics) != 1 + n_idx:
            raise QtError(
                "QT-INPUT-002", f"{lg.location}: {name} ({version}) needs {1 + n_idx} topics, got {len(lg.topics)}"
            )
        if lg.address not in KNOWN_EXCHANGES:
            unknown.add(lg.address)
        if lg.tx not in by_tx:
            by_tx[lg.tx] = []
            tx_order.append(lg.tx)
        by_tx[lg.tx].append((lg, name, version))
    for tx in tx_order:
        _decode_tx(by_tx[tx], res)
    res.unknown_exchanges = sorted(unknown)
    if res.unknown_exchanges:
        res.warnings.append(
            f"chain: {len(res.unknown_exchanges)} contract(s) emitting exchange events are not in the known exchange "
            f"list ({', '.join(res.unknown_exchanges[:3])}); pass exchanges= to restrict decoding"
        )
    if res.operator_fills:
        res.warnings.append(f"chain: {res.operator_fills} v1 OrderFilled without OrdersMatched (direct operator fills)")
    return res


def _decode_tx(entries: list[tuple[RawLog, str, str]], res: ChainDecode) -> None:
    pending: list[Fill] = []
    taker_fill: Fill | None = None
    match_idx = 0
    for lg, name, version in entries:
        if name == "OrderFilled":
            f = _decode_fill(lg, version)
            if f.role == "taker":
                if taker_fill is not None:
                    raise QtError(
                        "QT-INPUT-002", f"{lg.location}: two taker OrderFilled events without an OrdersMatched between"
                    )
                taker_fill = f
            else:
                pending.append(f)
        elif name == "OrdersMatched":
            where = lg.location
            if version == V1:
                maker_asset, taker_asset, making, taking = _words(lg.data, 4, where)
                if maker_asset == 0:
                    side, token, collateral, tokens = "BUY", taker_asset, making, taking
                else:
                    side, token, collateral, tokens = "SELL", maker_asset, taking, making
            else:
                side_w, token, making, taking = _words(lg.data, 4, where)
                side = "BUY" if side_w == 0 else "SELL"
                collateral, tokens = (making, taking) if side == "BUY" else (taking, making)
            taker_hash, taker_wallet = lg.topics[1], _addr(lg.topics[2])
            if taker_fill is not None and taker_fill.order_hash != taker_hash:
                raise QtError(
                    "QT-INPUT-002",
                    f"{where}: OrdersMatched taker {taker_hash} differs from the preceding taker OrderFilled "
                    f"{taker_fill.order_hash}",
                )
            makers = tuple(_with(f, match_idx, i) for i, f in enumerate(pending))
            tf = _with(taker_fill, match_idx, None) if taker_fill is not None else None
            for f in makers:
                if f.taker != taker_wallet:
                    res.warnings.append(
                        f"chain: {f.tx} log {f.log_index}: maker fill names taker {f.taker}, OrdersMatched names "
                        f"{taker_wallet}"
                    )
            res.fills.extend(makers)
            if tf is not None:
                res.fills.append(tf)
            res.matches.append(
                Match(
                    tx=lg.tx,
                    block=lg.block,
                    log_index=lg.log_index,
                    block_ts_ns=lg.block_ts_ns,
                    exchange=lg.address,
                    version=version,
                    match_idx=match_idx,
                    taker_order_hash=taker_hash,
                    taker_wallet=taker_wallet,
                    taker_side=side,
                    taker_token_id=token,
                    taker_collateral=collateral,
                    taker_tokens=tokens,
                    makers=makers,
                    taker_fill=tf,
                )
            )
            pending, taker_fill = [], None
            match_idx += 1
        elif name == "OrderCancelled":
            res.cancels.append((lg.tx, lg.block, lg.log_index, lg.topics[1]))
        elif name == "FeeCharged":
            res.fee_events += 1
    for f in pending:
        res.fills.append(_role(f, "operator_fill"))
        res.operator_fills += 1
    if taker_fill is not None:
        res.fills.append(_role(taker_fill, "operator_fill"))
        res.operator_fills += 1


def _with(f: Fill, match_idx: int, pos: int | None) -> Fill:
    return Fill(**{**f.__dict__, "match_idx": match_idx, "maker_pos": pos})


def _role(f: Fill, role: str) -> Fill:
    return Fill(**{**f.__dict__, "role": role})


__all__ = ["KNOWN_EXCHANGES", "SIG", "TOPICS", "V1", "V2", "ChainDecode", "Fill", "Match", "RawLog", "decode"]
