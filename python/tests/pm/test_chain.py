"""Keccak-256 and the Polymarket exchange-event decoder (`queuetruth.pm.keccak`, `queuetruth.pm.chain`)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from queuetruth.errors import QtError
from queuetruth.pm import keccak
from queuetruth.pm.chain import KNOWN_EXCHANGES, SIG, TOPICS, decode
from queuetruth.pm.keccak import keccak256, topic

EX2 = "0xe111180000d2663c0091e4f400237545b87b996b"  # CTF Exchange V2 (docs.polymarket.com/resources/contracts)
EX1 = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"  # CTF Exchange v1 (archived Polymarket/ctf-exchange README)
TOKEN = 123456789


# ---- keccak -------------------------------------------------------------------------------------------------------


def test_keccak_known_answers() -> None:
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    # ERC-20 topics every block explorer shows
    assert topic("Transfer(address,address,uint256)") == (
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )
    assert topic("Approval(address,address,uint256)") == (
        "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
    )
    # the CTF Exchange v1 topics, from the event signatures of the public contract source (ITrading.sol)
    assert topic(SIG[("OrderFilled", "v1")]) == "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
    assert topic(SIG[("OrdersMatched", "v1")]) == "0x63bf4d16b7fa898ef4c4b2b6d90fd201e9c56313b65638af6088d149d2ce956c"


def _sha3_with_this_permutation(data: bytes) -> bytes:
    """SHA3-256 = the same sponge with FIPS 202 padding 0x06: checks the permutation and absorption at every
    length against `hashlib` (the two digests differ only in the padding byte)."""
    msg = bytearray(data) + b"\x06"
    while len(msg) % keccak._RATE:
        msg.append(0)
    msg[-1] |= 0x80
    state = [0] * 25
    for off in range(0, len(msg), keccak._RATE):
        for i in range(keccak._RATE // 8):
            state[i] ^= int.from_bytes(msg[off + 8 * i : off + 8 * i + 8], "little")
        state = keccak._permute(state)
    return b"".join(state[i].to_bytes(8, "little") for i in range(4))


@pytest.mark.parametrize("n", [0, 1, 55, 135, 136, 137, 271, 272, 273, 300])
def test_keccak_sponge_matches_hashlib_sha3_at_block_boundaries(n: int) -> None:
    data = bytes((7 * i + 3) % 256 for i in range(n))
    assert _sha3_with_this_permutation(data) == hashlib.sha3_256(data).digest()


def test_every_exchange_topic_is_registered_with_its_shape() -> None:
    assert len(TOPICS) == len(SIG) == 7
    assert TOPICS[topic(SIG[("OrderFilled", "v2")])] == ("OrderFilled", "v2", 3, 7)
    assert TOPICS[topic(SIG[("FeeCharged", "v1")])] == ("FeeCharged", "v1", 1, 2)


def test_known_exchanges_cover_both_clob_generations() -> None:
    """The current exchanges (docs.polymarket.com/resources/contracts) and both v1 exchanges that settled the CLOB
    before CLOB V2 (2026-04-28): the CTF Exchange and the Neg Risk CTF Exchange of the py-clob-client config."""
    assert KNOWN_EXCHANGES[EX2] == "ctf_exchange_v2" and KNOWN_EXCHANGES[EX1] == "ctf_exchange_v1"
    assert KNOWN_EXCHANGES["0xc5d563a36ae78145c45a50134d48a1215220f80a"] == "neg_risk_ctf_exchange_v1"
    assert all(a == a.lower() and len(a) == 42 for a in KNOWN_EXCHANGES)


# ---- log builders -------------------------------------------------------------------------------------------------


def _w(v: int) -> str:
    return f"{v:064x}"


def _a(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def _h(n: int) -> str:
    return "0x" + f"{n:064x}"


WALLET_M1, WALLET_M2, WALLET_T = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20


def log(name: str, ver: str, topics: list[str], words: list[int], block: int, li: int, tx: str, ex: str = EX2) -> dict:
    return {
        "address": ex,
        "topics": [topic(SIG[(name, ver)]), *topics],
        "data": "0x" + "".join(_w(x) for x in words),
        "blockNumber": hex(block),
        "transactionHash": tx,
        "logIndex": hex(li),
        "removed": False,
        "blockTimestamp": hex(1_792_764_002),
    }


def v2_match(tx: str, block: int, li0: int, makers: list[tuple[int, str, int, int, int]], taker: tuple) -> list[dict]:
    """makers: (order hash int, wallet, side 0 BUY / 1 SELL, collateral, tokens); taker: (hash, side, coll, tok)."""
    out = []
    li = li0
    for h, wal, side, coll, tok in makers:
        making, taking = (coll, tok) if side == 0 else (tok, coll)
        out.append(
            log(
                "OrderFilled",
                "v2",
                [_h(h), _a(wal), _a(WALLET_T)],
                [side, TOKEN, making, taking, 0, 0, 0],
                block,
                li,
                tx,
            )
        )
        li += 1
    th, tside, tcoll, ttok = taker
    making, taking = (tcoll, ttok) if tside == 0 else (ttok, tcoll)
    out.append(log("FeeCharged", "v2", [_a("0x" + "fe" * 20)], [1234], block, li, tx))
    out.append(
        log(
            "OrderFilled",
            "v2",
            [_h(th), _a(WALLET_T), _a(EX2)],
            [tside, TOKEN, making, taking, 1234, 7, 9],
            block,
            li + 1,
            tx,
        )
    )
    out.append(log("OrdersMatched", "v2", [_h(th), _a(WALLET_T)], [tside, TOKEN, making, taking], block, li + 2, tx))
    return out


def write(tmp: Path, logs: list[dict[str, Any]], envelope: bool = True) -> Path:
    p = tmp / "logs.json"
    p.write_text(json.dumps({"jsonrpc": "2.0", "id": 1, "result": logs} if envelope else logs), encoding="utf-8")
    return p


TX1, TX2 = "0x" + "a1" * 32, "0x" + "a2" * 32


def test_v2_match_makers_in_operator_order_with_sides_and_prices(tmp_path: Path) -> None:
    # a taker buys 30 tokens: maker 1 sells 10 at 0.45, maker 2 sells 20 at 0.46
    logs = v2_match(
        TX1,
        100,
        0,
        [(1, WALLET_M1, 1, 4_500_000, 10_000_000), (2, WALLET_M2, 1, 9_200_000, 20_000_000)],
        (9, 0, 13_700_000, 30_000_000),
    )
    res = decode([write(tmp_path, logs)])
    assert (res.logs, res.fee_events, res.operator_fills, res.other_events) == (5, 1, 0, 0)
    (m,) = res.matches
    assert m.taker_order_hash == _h(9) and m.taker_side == "BUY" and m.taker_tokens == 30_000_000
    assert [(f.order_hash, f.maker_pos, f.role, f.side, f.collateral, f.tokens) for f in m.makers] == [
        (_h(1), 0, "maker", "SELL", 4_500_000, 10_000_000),
        (_h(2), 1, "maker", "SELL", 9_200_000, 20_000_000),
    ]
    assert m.taker_fill is not None and m.taker_fill.role == "taker" and m.taker_fill.fee == 1234
    assert m.taker_fill.builder == _h(7) and m.taker_fill.fee_asset == "collateral"
    assert [f.role for f in res.fills] == ["maker", "maker", "taker"]
    assert res.fills[0].block_ts_ns == 1_792_764_002 * 1_000_000_000
    assert res.unknown_exchanges == [] and res.warnings == []


def test_v1_sides_fee_asset_operator_fill_and_two_matches_in_one_tx(tmp_path: Path) -> None:
    def f1(h: int, wal: str, maker_asset: int, taker_asset: int, making: int, taking: int, li: int, tx: str, tkr: str):
        return log(
            "OrderFilled",
            "v1",
            [_h(h), _a(wal), _a(tkr)],
            [maker_asset, taker_asset, making, taking, 5],
            50,
            li,
            tx,
            EX1,
        )

    logs = [
        # match 0: a maker BUY (gives collateral 0 asset id), taker SELL
        f1(1, WALLET_M1, 0, TOKEN, 4_000_000, 10_000_000, 0, TX1, WALLET_T),
        f1(9, WALLET_T, TOKEN, 0, 10_000_000, 4_000_000, 1, TX1, EX1),
        log("OrdersMatched", "v1", [_h(9), _a(WALLET_T)], [TOKEN, 0, 10_000_000, 4_000_000], 50, 2, TX1, EX1),
        # match 1 in the same transaction
        f1(2, WALLET_M2, TOKEN, 0, 5_000_000, 2_500_000, 3, TX1, WALLET_T),
        f1(8, WALLET_T, 0, TOKEN, 2_500_000, 5_000_000, 4, TX1, EX1),
        log("OrdersMatched", "v1", [_h(8), _a(WALLET_T)], [0, TOKEN, 2_500_000, 5_000_000], 50, 5, TX1, EX1),
        # a direct operator fill: OrderFilled without OrdersMatched
        f1(3, WALLET_M1, 0, TOKEN, 1_000_000, 2_000_000, 6, TX2, WALLET_T),
    ]
    for lg in logs:
        lg.pop("blockTimestamp")
    res = decode([write(tmp_path, logs, envelope=False)])
    assert [m.match_idx for m in res.matches] == [0, 1]
    assert [(m.taker_side, m.makers[0].side) for m in res.matches] == [("SELL", "BUY"), ("BUY", "SELL")]
    buy = res.matches[0].makers[0]
    assert (buy.collateral, buy.tokens, buy.fee_asset) == (4_000_000, 10_000_000, "token")  # v1 fee on proceeds
    assert res.matches[1].makers[0].fee_asset == "collateral"
    op = [f for f in res.fills if f.role == "operator_fill"]
    assert len(op) == 1 and op[0].order_hash == _h(3) and res.operator_fills == 1
    assert any("without OrdersMatched" in w for w in res.warnings)
    assert res.fills[0].block_ts_ns is None


def test_removed_duplicate_unknown_and_restricted_logs(tmp_path: Path) -> None:
    logs = v2_match(TX1, 100, 0, [(1, WALLET_M1, 1, 4_500_000, 10_000_000)], (9, 0, 4_500_000, 10_000_000))
    reorged = {**logs[0], "logIndex": hex(40), "removed": True}
    dup = dict(logs[1])
    other = {**logs[0], "topics": [topic("Transfer(address,address,uint256)"), _a(WALLET_M1), _a(WALLET_T)]}
    other["logIndex"] = hex(41)
    foreign = [{**lg, "address": "0x" + "77" * 20, "transactionHash": TX2, "blockNumber": hex(101)} for lg in logs]
    res = decode([write(tmp_path, [*logs, reorged, dup, other, *foreign])])
    assert (res.removed, res.duplicate_logs, res.other_events) == (1, 1, 1)
    assert len(res.matches) == 2 and res.unknown_exchanges == ["0x" + "77" * 20]
    assert any("not in the known exchange list" in w for w in res.warnings)
    only = decode([write(tmp_path, [*logs, *foreign])], exchanges=[EX2.upper().replace("0X", "0x")])
    assert len(only.matches) == 1 and only.other_events == len(foreign) and only.unknown_exchanges == []


def test_csv_export(tmp_path: Path) -> None:
    logs = v2_match(TX1, 100, 0, [(1, WALLET_M1, 0, 4_500_000, 10_000_000)], (9, 1, 4_500_000, 10_000_000))
    rows = ["block_number,tx_hash,log_index,address,topic0,topic1,topic2,topic3,data,block_time,removed"]
    for lg in logs:
        t = lg["topics"] + [""] * (4 - len(lg["topics"]))
        rows.append(
            f"{int(lg['blockNumber'], 16)},{lg['transactionHash']},{int(lg['logIndex'], 16)},{lg['address']},"
            f"{','.join(t)},{lg['data']},2026-10-23T14:00:02Z,false"
        )
    rows.append(rows[1].replace(",false", ",true").replace(",0,", ",77,", 1))
    p = tmp_path / "logs.csv"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    res = decode([p])
    assert len(res.matches) == 1 and res.removed == 1
    assert res.fills[0].side == "BUY" and res.fills[0].block_ts_ns == 1_792_764_002 * 1_000_000_000
    bad = tmp_path / "bad.csv"
    bad.write_text("block,tx\n1,2\n", encoding="utf-8")
    with pytest.raises(QtError, match="needs columns"):
        decode([bad])


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda lg: lg.update(topics=lg["topics"][:3]), "needs 4 topics"),
        (lambda lg: lg.update(data=lg["data"][:-64]), "expected 224"),
        (lambda lg: lg.update(address="0x1234"), "must be 20 bytes"),
        (lambda lg: lg.update(transactionHash="a1" * 32), "0x-prefixed"),
        (lambda lg: lg.update(blockNumber="0xzz"), "is not an integer"),
        (lambda lg: lg.update(blockTimestamp="2026-10-23T14:00:02"), "no time zone"),
        (lambda lg: lg.update(data="0x" + _w(2) + lg["data"][66:]), "is not 0 (BUY) or 1 (SELL)"),
    ],
)
def test_malformed_logs_are_qt_input_002(tmp_path: Path, mutate: Any, message: str) -> None:
    logs = v2_match(TX1, 100, 0, [(1, WALLET_M1, 1, 4_500_000, 10_000_000)], (9, 0, 4_500_000, 10_000_000))
    mutate(logs[0])
    with pytest.raises(QtError) as e:
        decode([write(tmp_path, logs)])
    assert e.value.code == "QT-INPUT-002" and message in e.value.message


def test_inconsistent_transactions_are_refused(tmp_path: Path) -> None:
    logs = v2_match(TX1, 100, 0, [(1, WALLET_M1, 1, 4_500_000, 10_000_000)], (9, 0, 4_500_000, 10_000_000))
    logs[-1]["topics"][1] = _h(8)  # OrdersMatched names another taker
    with pytest.raises(QtError, match="differs from the preceding taker"):
        decode([write(tmp_path, logs)])
    logs = v2_match(TX1, 100, 0, [(1, WALLET_M1, 1, 4_500_000, 10_000_000)], (9, 0, 4_500_000, 10_000_000))
    clash = {**logs[0], "data": logs[0]["data"][:-1] + "1"}
    with pytest.raises(QtError, match="twice with different contents"):
        decode([write(tmp_path, [*logs, clash])])
    v1 = log("OrderFilled", "v1", [_h(1), _a(WALLET_M1), _a(WALLET_T)], [TOKEN, TOKEN, 1, 1, 0], 5, 0, TX2, EX1)
    with pytest.raises(QtError, match="exactly one must be 0"):
        decode([write(tmp_path, [v1])])


def test_fixture_matches_reconstruct_every_maker_in_operator_order(poly_run: Any) -> None:
    """The committed fixture: 65 matches, every maker order hash and size in the generator's fill order."""
    import pyarrow.parquet as pq

    rows = pq.read_table(poly_run.pm / "pm" / "chain_fills.parquet").to_pylist()
    by_tx: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_tx.setdefault(r["tx"], []).append(r)
    matches = poly_run.truth["matches"]
    assert len(matches) == poly_run.ingest.chain["matches"] == 65
    for m in matches:
        got = sorted((r for r in by_tx[m["tx"]] if r["role"] == "maker"), key=lambda r: r["maker_pos"])
        assert [(r["order_hash"], r["size"], r["own"], r["price"]) for r in got] == [
            (x["order_hash"], x["qty"], x["own"], x["price"]) for x in m["makers"]
        ]
        (tk,) = [r for r in by_tx[m["tx"]] if r["role"] == "taker"]
        assert tk["order_hash"] == m["taker_order_hash"]
    assert poly_run.ingest.chain["removed_logs"] == 1 and poly_run.ingest.chain["other_events"] == 1
