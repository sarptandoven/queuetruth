"""Capture session: rows, gap detection, segments, persistence; the asyncio daemon against a local WebSocket server."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from livefill.capture import RECONNECT, CaptureConfig, CaptureSession, run_capture
from livefill.store import CAPTURE_SCHEMA, read_capture, read_meta

from queuetruth.errors import QtError


def order(eid: str | None, pre: str | None, oid: int = 1, ev: str = "order_created", market: str = "btcusd") -> str:
    msg: dict = {
        "data": {"id": oid, "order_type": 0, "price_str": "1.00", "amount_str": "1.0", "microtimestamp": "1000"},
        "channel": f"live_orders_{market}",
        "event": ev,
    }
    if eid is not None:
        msg["event_id"] = eid
        msg["pre_event_id"] = pre
    return json.dumps(msg)


def session(tmp_path: Path, **kw) -> CaptureSession:
    t = [1_000]

    def clock() -> int:
        t[0] += 1
        return t[0]

    cfg = CaptureConfig(out=tmp_path / "cap", markets=["btcusd", "ethusd"], **kw)
    return CaptureSession(cfg, clock=clock, mono=clock)


def test_unbroken_chain_has_one_segment(tmp_path: Path) -> None:
    s = session(tmp_path)
    s.connected()
    for k in range(5):
        assert s.handle(order(f"e{k + 1}", f"e{k}", oid=k)) != RECONNECT
    # another market keeps its own chain
    s.handle(order("x1", "x0", market="ethusd"))
    s.handle(order("x2", "x1", market="ethusd"))
    meta = s.close()
    assert meta["gaps"] == [] and len(meta["segments"]) == 1 and meta["complete"] is True
    assert meta["counts"] == {"live_orders": 7}
    t = read_capture(tmp_path / "cap")
    assert t.schema.equals(CAPTURE_SCHEMA, check_metadata=True)
    assert t.column("seq").to_pylist() == list(range(t.num_rows))
    assert t.column("kind").to_pylist()[0] == "marker"


def test_chain_break_restart_duplicate_and_missing_ids(tmp_path: Path) -> None:
    s = session(tmp_path)
    s.connected()
    s.handle(order("e1", None))  # restart at connect: counted, not a gap
    s.handle(order("e2", "e1"))
    s.handle(order("e2", "e1"))  # same message twice
    s.handle(order("e4", "e3"))  # e3 lost
    s.handle(order("e5", None))  # chain restart mid-stream
    s.handle(order(None, None))  # no event id at all
    meta = s.close()
    assert [g["kind"] for g in meta["gaps"]] == ["event_chain", "restart"]
    g = meta["gaps"][0]
    assert (g["expected_pre"], g["got_pre"], g["event_id"], g["segment_before"], g["segment_after"]) == (
        "e2",
        "e3",
        "e4",
        0,
        1,
    )
    assert meta["anomalies"]["chain_restarts_at_connect"] == 1
    assert meta["anomalies"]["duplicate_event_id"] == 1
    assert meta["anomalies"]["no_event_id"] == 1
    assert [x["opened_by"] for x in meta["segments"]] == ["start", "event_chain", "restart"]
    t = read_capture(tmp_path / "cap")
    kinds = t.column("kind").to_pylist()
    assert kinds.count("duplicate") == 1
    # the frame that revealed the gap starts the new segment
    segs = t.column("segment").to_pylist()
    eids = t.column("event_id").to_pylist()
    assert segs[eids.index("e4")] == 1 and segs[eids.index("e5")] == 2
    gaps_lines = (tmp_path / "cap" / "gaps.jsonl").read_text().splitlines()
    assert [json.loads(x)["kind"] for x in gaps_lines] == ["event_chain", "restart"]


def test_a_break_in_one_market_keeps_the_other_markets_chains(tmp_path: Path) -> None:
    """A loss that hits two markets at once (a server-side drop) is a gap in each, not only in the first."""
    s = session(tmp_path)
    s.connected()
    s.handle(order("a1", "a0", market="btcusd"))
    s.handle(order("b1", "b0", market="ethusd"))
    s.handle(order("a3", "a2", market="btcusd"))  # a2 lost
    s.handle(order("b3", "b2", market="ethusd"))  # b2 lost in the same drop
    s.handle(order("a4", "a3", market="btcusd"))
    meta = s.close()
    assert [(g["kind"], g["market"], g["expected_pre"]) for g in meta["gaps"]] == [
        ("event_chain", "btcusd", "a1"),
        ("event_chain", "ethusd", "b1"),
    ]
    assert len(meta["segments"]) == 3


def test_missing_pre_event_id_is_not_a_restart(tmp_path: Path) -> None:
    s = session(tmp_path)
    s.connected()
    s.handle(order("e1", "e0"))
    no_pre = json.loads(order("e2", "e1"))
    del no_pre["pre_event_id"]
    s.handle(json.dumps(no_pre))
    s.handle(order("e3", "e2"))
    meta = s.close()
    assert meta["gaps"] == [] and meta["anomalies"]["no_pre_event_id"] == 1


def test_rest_book_requested_before_a_gap_is_stale(tmp_path: Path) -> None:
    """A REST answer that arrives on a later connection or segment describes the market before the gap: it is kept
    as `stale_rest_book` and never seeds the new segment."""
    from livefill.check import choose_seed
    from livefill.stream import segment_streams

    def body(us: int) -> str:
        return f'{{"microtimestamp":"{us}","bids":[["1.00","1.0","42"]],"asks":[]}}'

    s = session(tmp_path)  # its clock runs at 1,001 ns, 1,002 ns, ...
    s.connected()
    requested = s.position()
    s.disconnected("bts:request_reconnect")
    s.connected()
    s.rest_book("btcusd", body(1), requested=requested)  # in flight across the reconnect
    s.rest_book("btcusd", body(1), requested=s.position())
    meta = s.close()
    assert meta["anomalies"]["stale_rest_books"] == 1 and meta["rest_snapshots"] == 1
    kinds = [r["kind"] for r in read_capture(tmp_path / "cap").to_pylist() if "rest" in r["kind"]]
    assert kinds == ["stale_rest_book", "rest_book"]
    st = segment_streams(tmp_path / "cap")[(1, "btcusd")]
    assert [x.source for x in st.snapshots] == ["rest"]
    # the backstop: a REST book stamped (1,000 ns) before its segment (after the first) began is no seed either
    assert st.segment_start_ns is not None and st.segment_start_ns > 1_000
    assert choose_seed(st, "rest") is None


def test_storage_errors_stop_the_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = session(tmp_path, flush_rows=2)
    s.connected()

    def full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(s.writer, "flush", full)
    with pytest.raises(QtError) as e:
        s.handle(order("e1", "e0"))
    assert e.value.code == "QT-INPUT-001" and "No space left" in e.value.message
    meta = s.close()
    assert meta["complete"] is False and "No space left" in meta["storage_error"]


def test_reconnect_is_a_gap_with_reason_and_chain_resets(tmp_path: Path) -> None:
    s = session(tmp_path)
    s.connected()
    s.handle(order("e1", "e0"))
    assert s.handle(json.dumps({"event": "bts:request_reconnect", "channel": "", "data": ""})) == RECONNECT
    s.disconnected("bts:request_reconnect")
    s.connected()
    s.handle(order("e9", "e8"))  # a new connection does not compare with the old chain
    s.stalled(61.0)
    s.connected()
    meta = s.close()
    assert [(g["kind"], g["reason"]) for g in meta["gaps"]] == [
        ("reconnect", "bts:request_reconnect"),
        ("reconnect", "stall: no frame for 61.0 s"),
    ]
    assert meta["reconnect_requests"] == 1 and meta["reconnects"] == 2 and meta["anomalies"]["stalls"] == 1
    assert len(meta["segments"]) == 3


def test_control_frames_bad_frames_and_clock_regressions(tmp_path: Path) -> None:
    s = session(tmp_path)
    s.connected()
    s.handle(json.dumps({"event": "bts:subscription_succeeded", "channel": "live_orders_btcusd", "data": {}}))
    s.handle(json.dumps({"event": "bts:error", "channel": "", "data": {"message": "bad"}}))
    s.handle(json.dumps({"event": "bts:heartbeat", "channel": "", "data": {}}))
    s.handle("garbage", recv_ns=10**12)
    s.handle(order("e1", "e0"), recv_ns=5)  # wall clock went backwards
    meta = s.close()
    assert meta["subscribed"] == ["live_orders_btcusd"]
    assert meta["errors"][0]["data"] == {"message": "bad"}
    assert meta["heartbeats"] == 1
    assert meta["anomalies"]["bad_frames"] == 1 and meta["anomalies"]["wall_clock_regressions"] == 1
    t = read_capture(tmp_path / "cap")
    assert "garbage" in t.column("raw").to_pylist()


def test_rest_book_rows_and_validation(tmp_path: Path) -> None:
    s = session(tmp_path)
    s.connected()
    s.rest_book("btcusd", '{"microtimestamp":"77","bids":[["1","2","3"]],"asks":[]}')
    with pytest.raises(QtError):
        s.rest_book("btcusd", '{"microtimestamp":"77","bids":[["1","2"]],"asks":[]}')
    s.rest_error("btcusd", "timeout")
    meta = s.close()
    assert meta["rest_snapshots"] == 1 and meta["rest_errors"][0]["error"] == "timeout"
    t = read_capture(tmp_path / "cap")
    row = next(r for r in t.to_pylist() if r["kind"] == "rest_book")
    assert (row["market"], row["microtimestamp"], row["channel"]) == ("btcusd", 77, "rest_order_book_btcusd")


def test_parts_rotate_and_meta_tracks_progress(tmp_path: Path) -> None:
    s = session(tmp_path, flush_rows=3)
    s.connected()
    for k in range(8):
        s.handle(order(f"e{k + 1}", f"e{k}"))
    mid = read_meta(tmp_path / "cap")
    assert mid["complete"] is False and mid["parts"] >= 2
    meta = s.close()
    parts = sorted((tmp_path / "cap" / "messages").glob("part-*.parquet"))
    assert len(parts) == meta["parts"] == 3
    assert sum(pq.read_metadata(p).num_rows for p in parts) == meta["rows"] == 9
    assert not list((tmp_path / "cap" / "messages").glob("*.tmp"))


def test_refuses_existing_capture_and_bad_config(tmp_path: Path) -> None:
    s = session(tmp_path)
    s.close()
    with pytest.raises(QtError, match="already holds a capture"):
        session(tmp_path)
    for kw in ({"markets": []}, {"markets": ["a", "a"]}, {"duration_s": 0}, {"stall_s": 5, "heartbeat_s": 10}):
        args = {"out": tmp_path / "x", "markets": ["btcusd"], **kw}
        with pytest.raises(QtError):
            CaptureConfig(**args)


def test_read_capture_rejects_foreign_parquet(tmp_path: Path) -> None:
    import pyarrow as pa

    d = tmp_path / "c" / "messages"
    d.mkdir(parents=True)
    pq.write_table(pa.table({"a": [1]}), d / "part-00000.parquet")
    with pytest.raises(QtError) as e:
        read_capture(tmp_path / "c")
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError) as e:
        read_capture(tmp_path / "missing")
    assert e.value.code == "QT-INPUT-001"
    with pytest.raises(QtError):
        read_meta(tmp_path / "c")


# -- the daemon against a local server ------------------------------------------------------------------------------


def _frames(market: str, start: int, n: int, restart: bool) -> list[str]:
    out = []
    for k in range(start, start + n):
        pre = None if (restart and k == start) else f"e{k - 1}"
        out.append(order(f"e{k}", pre, oid=k, market=market))
    return out


def test_daemon_reconnects_on_request_and_on_stall(tmp_path: Path) -> None:
    from websockets.asyncio.server import serve

    connections: list[list[str]] = []

    async def handler(ws) -> None:
        n = len(connections)
        got: list[str] = []
        connections.append(got)
        subs = 0
        while subs < 2:
            msg = json.loads(await ws.recv())
            got.append(msg["data"]["channel"])
            subs += 1
            await ws.send(json.dumps({"event": "bts:subscription_succeeded", "channel": msg["data"]["channel"]}))
        if n == 0:
            for f in _frames("btcusd", 1, 5, restart=True):
                await ws.send(f)
            await ws.send(json.dumps({"event": "bts:request_reconnect", "channel": "", "data": ""}))
            await asyncio.sleep(5)
        elif n == 1:
            for f in _frames("btcusd", 100, 3, restart=True):
                await ws.send(f)
            await asyncio.sleep(5)  # silent: the client must give up after stall_s
        else:
            for f in _frames("btcusd", 200, 2, restart=True):
                await ws.send(f)
            await asyncio.sleep(5)

    fetched: list[str] = []

    def fetch(rest_url: str, market: str) -> str:
        fetched.append(market)
        return '{"microtimestamp":"5","bids":[["1.00","1.0","42"]],"asks":[]}'

    async def main() -> dict:
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = CaptureConfig(
                out=tmp_path / "cap",
                markets=["btcusd"],
                channels=["live_orders", "live_trades"],
                url=f"ws://127.0.0.1:{port}",
                duration_s=4.0,
                stall_s=0.6,
                heartbeat_s=0.3,
                max_backoff_s=0.2,
            )
            return await run_capture(cfg, fetch=fetch, log=lambda s: None)

    meta = asyncio.run(main())
    assert len(connections) >= 3
    assert connections[0] == ["live_orders_btcusd", "live_trades_btcusd"]
    kinds = [(g["kind"], g["reason"]) for g in meta["gaps"]]
    assert kinds[0] == ("reconnect", "bts:request_reconnect")
    assert kinds[1][0] == "reconnect" and kinds[1][1].startswith("stall: no frame for")
    assert meta["anomalies"]["stalls"] >= 1 and meta["reconnect_requests"] == 1
    assert meta["counts"]["live_orders"] >= 10
    assert meta["complete"] is True and meta["gaps"] and len(meta["segments"]) == len(meta["gaps"]) + 1
    assert fetched and meta["rest_snapshots"] == len(fetched)
    t = read_capture(tmp_path / "cap")
    by_seg: dict[int, list[str]] = {}
    for r in t.to_pylist():
        if r["event"] == "order_created":
            by_seg.setdefault(r["segment"], []).append(r["event_id"])
    assert by_seg[0] == [f"e{k}" for k in range(1, 6)]
    assert by_seg[1] == ["e100", "e101", "e102"]
