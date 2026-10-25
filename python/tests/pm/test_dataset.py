"""Batches -> a queuetruth L2 dataset and back (`queuetruth.pm.dataset`)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from pm._helpers import depth, l2_dataset, resync, trade

from queuetruth.errors import QtError
from queuetruth.pm.batches import BuildStats
from queuetruth.pm.book import ASK, BID
from queuetruth.pm.dataset import (
    DAY_NS,
    KINDS,
    Instrument,
    Series,
    feed_gaps,
    instruments,
    read_l2,
    read_manifest,
    write_dataset,
)
from queuetruth.schemas.arrow import SCHEMAS

T = 20_384 * DAY_NS + 23 * 3_600_000_000_000  # 2025-10-24 23:00 UTC


def test_rows_follow_the_tardis_conventions(tmp_path: Path) -> None:
    rows = l2_dataset(
        tmp_path,
        [
            resync(T, [(BID, 4900, 10), (BID, 4800, 3), (ASK, 5100, 10)]),
            depth(T + 5, [(BID, 4900, 10, 6), (ASK, 5100, 10, 0)], [trade(T + 5, 4900, 4, ASK)]),
        ],
    )
    kinds = [KINDS[k] for k in rows.kind]
    assert kinds == ["clear", "snapshot", "snapshot", "snapshot", "trade", "depth", "depth"]
    assert rows.sequence.tolist() == [1, 1, 1, 1, 2, 2, 2] and rows.event_id.tolist() == [1, 1, 1, 1, 2, 2, 2]
    assert rows.flags.tolist() == [0, 8, 8, 9, 0, 0, 1]  # SNAPSHOT = 8, LAST_IN_BATCH = 1
    assert rows.side[4] == BID and rows.aggressor[4] == ASK and rows.trade_qty[4] == 4  # trade rows first
    assert (rows.prev_qty[5], rows.new_qty[5], rows.prev_qty[6], rows.new_qty[6]) == (10, 6, 10, 0)
    t = pq.read_table(next(tmp_path.glob("l2/date=*/instrument_id=1/part-00000.parquet")))
    assert t.schema.equals(SCHEMAS["qt.l2"], check_metadata=False)
    assert t.column("level").to_pylist() == [255, 0, 1, 0, 255, 0, 255]  # rank after the batch; 255 = emptied


def test_partitions_by_utc_day_restate_the_book_and_the_manifest_identity(tmp_path: Path) -> None:
    hour = 3_600_000_000_000
    batches = [
        resync(T, [(BID, 4900, 10), (ASK, 5100, 10)]),
        depth(T + 2 * hour, [(BID, 4900, 10, 12)]),  # 01:00 the next day
    ]
    for k, b in enumerate(batches, start=1):
        b.seq = k
    ins = Instrument(7, "SYM", 100, -2)
    stats = BuildStats(feed_gaps=[(T + 1, T + 2, "sequence gap until the next snapshot")])
    src = tmp_path / "capture.jsonl"
    src.write_text("{}\n", encoding="utf-8")
    man, dq, files = write_dataset(
        tmp_path / "ds", [Series(ins, batches, stats)], source_format="pm_kalshi", source_venue="kalshi", inputs=[src]
    )
    assert [p["session_date"] for p in man.model_dump()["partitions"]] == [20_384, 20_385]
    rows = read_l2(tmp_path / "ds", 7)
    day2 = rows.part == 1
    assert [KINDS[k] for k in rows.kind[day2]] == ["clear", "snapshot", "snapshot", "depth"]  # restated first
    shas = sorted(f["sha256"] for f in files if f["name"].startswith("l2/"))
    assert man.dataset_id == hashlib.sha256(("\n".join(shas) + "\npm_kalshi").encode()).hexdigest()
    assert man.kind == "l2" and man.validation_status == "assumed" and man.source_venue == "kalshi"
    assert dq.verdict == "warnings" and dq.reasons == ["feed_gaps = 1"]
    assert feed_gaps(tmp_path / "ds") == {7: [(T + 1, T + 2)]}
    inst = instruments(tmp_path / "ds")
    assert [(r["instrument_id"], r["session_date"], r["tick_size"], r["size_exp"]) for r in inst] == [
        (7, 20_384, 100, -2),
        (7, 20_385, 100, -2),
    ]
    doc = json.loads((tmp_path / "ds" / "data_quality.json").read_text())
    assert doc["schema"] == "qt.data_quality" and read_manifest(tmp_path / "ds")["dataset_id"] == man.dataset_id
    assert len(read_l2(tmp_path / "ds", 99)) == 0


def test_inconsistent_batches_and_wrong_datasets_are_refused(tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        l2_dataset(tmp_path / "a", [resync(T, [(BID, 4900, 10)]), depth(T + 1, [(BID, 4900, 11, 12)])])
    assert e.value.code == "QT-ENGINE-001" and "the batch says 11" in e.value.message
    with pytest.raises(QtError) as e:
        read_manifest(tmp_path / "missing")
    assert e.value.code == "QT-INPUT-001"
    bad = tmp_path / "mbo"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"kind": "mbo", "partitions": []}), encoding="utf-8")
    with pytest.raises(QtError, match="an L2 dataset is required"):
        read_l2(bad, 1)


def test_crossed_books_make_the_verdict_unusable(tmp_path: Path) -> None:
    stats = BuildStats(crossed_books_after_last=5)
    b = [resync(T, [(BID, 4900, 1), (ASK, 5100, 1)])]
    b[0].seq = 1
    _m, dq, _f = write_dataset(
        tmp_path, [Series(Instrument(1, "X", 100, -2), b, stats)], source_format="x", source_venue="kalshi", inputs=[]
    )
    assert dq.verdict == "unusable" and dq.reasons[0].startswith("crossed_books_after_last / events")
