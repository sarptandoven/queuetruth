"""Segment export, ingest and the JSONL import of predecessor captures."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from lf_testkit import needs_core
from livefill import export, synth
from livefill.legacy import import_jsonl
from livefill.store import read_capture

from queuetruth.errors import QtError

REPO = Path(__file__).resolve().parents[3]
#: the RD-12 capture (JSONL layout, the directory `queuetruth ingest --format bitstamp` reads) under the repository's
#: `.realdata` (never committed); override with LIVEFILL_RD12_CAPTURE
ET_CAPTURE = Path(os.environ.get("LIVEFILL_RD12_CAPTURE", REPO / ".realdata/bitstamp/20261012T020742Z"))
RUN_REALDATA = os.environ.get("QT_REALDATA") == "1"


def test_export_writes_ingest_inputs(synth_default, tmp_path: Path) -> None:
    doc = export.export_segments(synth_default.capture, tmp_path / "exp")
    (seg,) = doc["segments"]
    d = Path(seg["dir"])
    assert seg["markets"]["synusd"]["seed_source"] == "ws" and seg["unseeded"] == []
    orders = (d / "live_orders.jsonl").read_text().splitlines()
    trades = (d / "live_trades.jsonl").read_text().splitlines()
    seeds = (d / "detail_order_book.jsonl").read_text().splitlines()
    assert len(seeds) == 1 and len(orders) == seg["lines"]["live_orders"] and len(trades) == seg["lines"]["live_trades"]
    t = read_capture(synth_default.capture)
    n_live = sum(1 for r in t.to_pylist() if r["channel"] == "live_orders_synusd" and r["event"].startswith("order_"))
    assert len(orders) == n_live
    first = json.loads(orders[0])
    assert isinstance(first["recv_ns"], int) and first["event"] in ("order_created", "order_changed", "order_deleted")
    assert json.loads((d / "capture_meta.json").read_text())["markets"] == ["synusd"]
    with pytest.raises(QtError):
        export.export_segments(synth_default.capture, tmp_path / "x", book_seed="nope")


def test_export_splits_at_gaps_and_skips_unseeded(tmp_path: Path) -> None:
    r = synth.build(tmp_path / "g", seed=3, steps=600, n_own=6, drop=(400,), reconnect_at=(450,))
    doc = export.export_segments(r.capture, tmp_path / "exp")
    segs = doc["segments"]
    assert doc["gaps"] == 2 and [s["segment"] for s in segs] == [0, 1, 2]
    # segment 1 starts at the chain break; the next snapshot seeds it
    assert all(s["dir"] for s in segs)
    seed_seqs = [s["markets"]["synusd"]["seed_seq"] for s in segs]
    assert seed_seqs == sorted(seed_seqs) and len(set(seed_seqs)) == 3


def test_export_rest_seed_line(tmp_path: Path) -> None:
    from livefill.capture import CaptureConfig, CaptureSession

    s = CaptureSession(CaptureConfig(out=tmp_path / "c", markets=["tstusd"]))
    s.connected()
    s.rest_book("tstusd", '{"microtimestamp":"1000","bids":[["1.00","1","5"]],"asks":[["1.01","1","6"]]}')
    s.handle(
        '{"data":{"id":7,"order_type":0,"price_str":"1.00","amount_str":"1","microtimestamp":"2000"},'
        '"channel":"live_orders_tstusd","event":"order_created","event_id":"a","pre_event_id":"z"}'
    )
    s.close()
    doc = export.export_segments(tmp_path / "c", tmp_path / "e", book_seed="auto")
    seed = json.loads((Path(doc["segments"][0]["dir"]) / "detail_order_book.jsonl").read_text())
    assert seed["livefill_seed"] == "rest" and seed["data"]["bids"] == [["1.00", "1", "5"]]
    ws_only = export.export_segments(tmp_path / "c", tmp_path / "w", book_seed="ws")
    assert ws_only["segments"][0]["skipped"] and ws_only["segments"][0]["unseeded"] == ["tstusd"]


def test_export_leaves_out_markets_without_events_after_their_seed(tmp_path: Path) -> None:
    from livefill.capture import CaptureConfig, CaptureSession

    s = CaptureSession(CaptureConfig(out=tmp_path / "c", markets=["aaausd", "bbbusd"], rest_snapshot=False))
    s.connected()
    for m, us in (("aaausd", 1000), ("bbbusd", 1000)):
        s.handle(
            json.dumps(
                {
                    "event": "data",
                    "channel": f"detail_order_book_{m}",
                    "data": {"microtimestamp": str(us), "bids": [["1.00", "1", "5"]], "asks": []},
                }
            )
        )
    order = {
        "data": {"id": 7, "order_type": 0, "price_str": "1.00", "amount_str": "1", "microtimestamp": "2000"},
        "channel": "live_orders_aaausd",
        "event": "order_created",
        "event_id": "a",
        "pre_event_id": "z",
    }
    s.handle(json.dumps(order))
    # bbbusd only has a message from before its seed: nothing to replay after the seed
    s.handle(json.dumps({**order, "channel": "live_orders_bbbusd", "data": {**order["data"], "microtimestamp": "500"}}))
    s.close()
    doc = export.export_segments(tmp_path / "c", tmp_path / "e", book_seed="ws")
    seg = doc["segments"][0]
    assert list(seg["markets"]) == ["aaausd"] and seg["lines"]["live_orders"] == 1
    seeds = (Path(seg["dir"]) / "detail_order_book.jsonl").read_text().splitlines()
    assert [json.loads(x)["channel"] for x in seeds] == ["detail_order_book_aaausd"]


@needs_core
def test_ingest_segment_round_trip(synth_default, tmp_path: Path) -> None:
    doc = export.export_segments(synth_default.capture, tmp_path / "exp")
    s = export.ingest_segment(doc["segments"][0]["dir"], tmp_path / "ds")
    dq = s["data_quality"]
    assert dq["records"] > 1000 and dq["crossed_books_after_last"] == 0 and dq["unknown_order_refs"] == 0
    assert dq["source_specific"].get("deleted_unknown", 0) == 0
    manifest = json.loads((tmp_path / "ds" / "manifest.json").read_text())
    assert manifest["source_format"] == "bitstamp" and manifest["source_venue"] == "bitstamp"


def test_import_jsonl_rebuilds_capture(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    o = {
        "data": {"id": 1, "order_type": 0, "price_str": "1", "amount_str": "1", "microtimestamp": "5"},
        "channel": "live_orders_tstusd",
        "event": "order_created",
        "event_id": "b",
        "pre_event_id": "a",
    }
    o2 = {**o, "event_id": "d", "pre_event_id": "c", "recv_ns": 30}
    (src / "live_orders.jsonl").write_text(
        json.dumps({**o, "recv_ns": 10}) + "\n" + json.dumps({"event": "_capture", "recv_ns": 20}) + "\n"
    )
    (src / "live_trades.jsonl").write_text(json.dumps(o2) + "\n")
    meta = import_jsonl(src, tmp_path / "cap")
    assert meta["markets"] == ["tstusd"] and [g["kind"] for g in meta["gaps"]] == ["reconnect"]
    t = read_capture(tmp_path / "cap")
    assert [r["recv_ns"] for r in t.to_pylist() if r["kind"] == "ws"] == [10, 30]
    with pytest.raises(QtError):
        import_jsonl(tmp_path / "empty", tmp_path / "cap2")
    (src / "live_trades.jsonl").write_text("{bad\n")
    with pytest.raises(QtError):
        import_jsonl(src, tmp_path / "cap3")


def test_export_never_deletes_and_refuses_a_non_empty_out(synth_default, tmp_path: Path) -> None:
    cap = Path(synth_default.capture)
    before = sorted(p.relative_to(cap.parent) for p in cap.parent.rglob("*"))
    for out in (cap, cap.parent):
        with pytest.raises(QtError) as e:
            export.export_segments(cap, out)
        assert e.value.code == "QT-CONFIG-001" and "not empty" in e.value.message
    assert sorted(p.relative_to(cap.parent) for p in cap.parent.rglob("*")) == before
    (tmp_path / "file").write_text("x")
    with pytest.raises(QtError):
        export.export_segments(cap, tmp_path / "file")
    (tmp_path / "empty").mkdir()
    assert export.export_segments(cap, tmp_path / "empty")["segments"][0]["dir"]


@pytest.mark.realdata
@needs_core
@pytest.mark.skipif(
    not (RUN_REALDATA and ET_CAPTURE.exists()),
    reason="set QT_REALDATA=1 and put the RD-12 Bitstamp capture (JSONL) at .realdata/bitstamp/20261012T020742Z "
    "(or point LIVEFILL_RD12_CAPTURE at it)",
)
def test_rd12_capture_round_trip_and_check(tmp_path: Path) -> None:
    """The RD-12 10-minute BTC/USD capture: JSONL -> Parquet -> export (seeded, as RD-12 was, by the first snapshot)
    -> ingest gives RD-12's 77,272 records; the replay from the first unambiguous snapshot (seq 278) explains every one
    of the venue's 5,407 later snapshots, with channel skew of at most 165 messages. Ranking same-millisecond orders
    by arrival instead of order id leaves 64 of them unexplained. One order stamped 2.4 ms before the seed is not in
    it; its create did arrive, so its delete is not an unknown delete."""
    from livefill.check import check_capture

    import_jsonl(ET_CAPTURE, tmp_path / "cap")
    doc = check_capture(tmp_path / "cap", book_seed="ws")
    st = doc["streams"][0]
    assert doc["verdict"] == "consistent" and doc["compared"] == 5407 and doc["unexplained"] == 0
    assert st["seed"]["seq"] == 278 and st["exact"] == 2843 and st["explained"] == 2564
    assert st["max_skew_messages"] == 165 and st["unknown_deletes"] == 0
    arrival = check_capture(tmp_path / "cap", book_seed="ws", id_ties=False)
    assert arrival["verdict"] == "inconsistent" and arrival["unexplained"] == 64
    exp = export.export_segments(tmp_path / "cap", tmp_path / "exp", book_seed="ws", clean_seed=False)
    s = export.ingest_segment(exp["segments"][0]["dir"], tmp_path / "ds")
    assert s["data_quality"]["records"] == 77_272
