"""`queuetruth.dataset`: open, read, verify and write a dataset on the synthetic fixtures."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from queuetruth.dataset import (
    DatasetKind,
    DatasetWriter,
    dataset_id,
    open_dataset,
)
from queuetruth.errors import QtError
from queuetruth.schemas.arrow import SCHEMAS

REPO = Path(__file__).resolve().parents[3]
SYNTH = REPO / "tests" / "fixtures" / "synth_small"


@pytest.fixture
def synth(tmp_path: Path) -> Path:
    dst = tmp_path / "synth"
    shutil.copytree(SYNTH, dst)
    return dst


def test_open_mbo_dataset() -> None:
    ds = open_dataset(SYNTH)
    assert ds.kind is DatasetKind.MBO
    assert ds.book_kind == "mbo"
    assert ds.manifest.is_dataset
    assert ds.manifest.dataset_id == json.loads((SYNTH / "manifest.json").read_text())["dataset_id"]
    assert ds.instruments and all(i.symbol for i in ds.instruments)
    assert {i.instrument_id for i in ds.instruments} == {p.instrument_id for p in ds.partitions}
    assert ds.days == sorted({p.date for p in ds.partitions})
    s = ds.sessions[0]
    assert s.open_ns is not None and s.close_ns is not None and s.open_ns < s.close_ns
    assert ds.instrument(s.instrument_id, ts=s.open_ns) == s.instrument
    assert ds.session_at(s.instrument_id, s.open_ns) is not None


def test_manifest_keeps_order_and_unknown_fields(synth: Path) -> None:
    doc = json.loads((synth / "manifest.json").read_text())
    doc["x_future_field"] = {"a": 1}
    (synth / "manifest.json").write_text(json.dumps(doc, indent=2) + "\n")
    m = open_dataset(synth).manifest
    assert list(m) == list(doc)
    assert m["x_future_field"] == {"a": 1}
    assert m.to_dict() == doc


def test_table_reads_and_filters() -> None:
    ds = open_dataset(SYNTH)
    p = ds.partitions[0]
    t = ds.table("mbo", columns=["ts_event", "price"], filter={"instrument_id": p.instrument_id})
    assert t.column_names == ["ts_event", "price"]
    assert t.num_rows == sum(q.rows or 0 for q in ds.partitions if q.instrument_id == p.instrument_id)
    whole = ds.table("qt.mbo")
    assert whole.num_rows == sum(q.rows or 0 for q in ds.partitions)
    inst = ds.table("instruments", filter={"session_date": p.date})
    assert inst.num_rows == sum(1 for s in ds.sessions if s.session_date == p.session_date)
    assert ds.table("orders").num_rows == 0  # no live tables here: empty, with the table's columns
    with pytest.raises(QtError) as e:
        ds.table("nope")
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        ds.table("mbo", columns=["no_such_column"])
    assert e.value.code == "QT-INPUT-002"


def test_table_refuses_a_file_of_another_table(synth: Path) -> None:
    ds = open_dataset(synth)
    target = synth / ds.partitions[0].files[0].path
    shutil.copy2(synth / "instruments.parquet", target)
    with pytest.raises(QtError) as e:
        open_dataset(synth).table("mbo")
    assert e.value.code == "QT-INPUT-002"
    assert "qt.instruments" in e.value.message and str(target) in e.value.message
    assert e.value.hint


def test_verify(synth: Path) -> None:
    assert open_dataset(synth).verify().ok
    f = synth / open_dataset(synth).partitions[0].files[0].path
    t = pq.read_table(f)
    pq.write_table(t.slice(0, max(t.num_rows - 1, 0)), f)
    v = open_dataset(synth).verify()
    assert not v.ok
    assert any("sha256" in x for x in v.problems) and any("rows" in x for x in v.problems)
    with pytest.raises(QtError) as e:
        v.raise_for_problems()
    assert e.value.code == "QT-INPUT-002"


def test_errors_name_the_path_and_the_fix(tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        open_dataset(tmp_path / "missing")
    assert e.value.code == "QT-INPUT-001"
    with pytest.raises(QtError) as e:
        open_dataset(tmp_path)
    assert e.value.code == "QT-INPUT-001" and str(tmp_path) in e.value.message and e.value.hint
    assert open_dataset(tmp_path, missing_ok=True).kind is DatasetKind.MBO
    (tmp_path / "manifest.json").write_text("[1]")
    with pytest.raises(QtError) as e:
        open_dataset(tmp_path)
    assert e.value.code == "QT-INPUT-002"
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "qt.dataset_manifest",
                "kind": "mbo",
                "partitions": [{"instrument_id": 1, "session_date": 1, "files": [{"path": "../x.parquet"}]}],
            }
        )
    )
    with pytest.raises(QtError) as e:
        open_dataset(tmp_path)
    assert e.value.code == "QT-INPUT-002" and "outside" in e.value.message


def test_kinds(tmp_path: Path) -> None:
    base = {"schema": "qt.dataset_manifest", "kind": "l2", "partitions": []}
    for venue, kind in (("coinbase", DatasetKind.L2), ("kalshi", DatasetKind.PREDICTION_MARKET)):
        d = tmp_path / venue
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({**base, "source_venue": venue}))
        assert open_dataset(d).kind is kind
        assert open_dataset(d).book_kind == "l2"
    for venues, kind in ((["sim:hftbacktest"], DatasetKind.SIM), (["coinbase"], DatasetKind.LIVE)):
        d = tmp_path / ("log_" + venues[0].replace(":", "_"))
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({"schema": "qt.live_manifest", "venues": venues}))
        ds = open_dataset(d)
        assert ds.kind is kind and ds.book_kind is None


def test_writer_writes_the_manifest_last_and_atomically(synth: Path, tmp_path: Path) -> None:
    src = open_dataset(synth)
    out = tmp_path / "copy"
    w = DatasetWriter(out)
    man = src.manifest.to_dict()
    for p in src.partitions:
        for f in p.files:
            t = pq.read_table(synth / f.path)
            w.write_table(f.path, t, SCHEMAS["qt.mbo"])
    w.copy(synth / "instruments.parquet", "instruments.parquet")
    assert not (out / "manifest.json").exists()
    shas = w.update_partitions(man)
    man["dataset_id"] = dataset_id(shas, man["source_format"])
    assert man["dataset_id"] == DatasetWriter.dataset_id(man)
    entry = w.finalize(man)
    assert entry["name"] == "manifest.json"
    assert not list(out.glob("*.tmp"))
    assert (out / "manifest.json").read_text() == json.dumps(man, indent=2) + "\n"
    ds = open_dataset(out)
    assert ds.verify().ok
    assert [e["name"] for e in w.entries][-1] == "manifest.json"


def test_writer_records_files_written_elsewhere(tmp_path: Path) -> None:
    w = DatasetWriter(tmp_path / "d")
    t = pa.table({"a": [1, 2, 3]})
    pq.write_table(t, tmp_path / "d" / "x.parquet")
    e = w.record("x.parquet")
    assert e["rows"] == 3 and e["bytes"] == (tmp_path / "d" / "x.parquet").stat().st_size
    assert w.entry("x.parquet") == e


def test_old_dataset_id_helpers_are_deprecated_aliases() -> None:
    from queuetruth.cert.certify import dataset_id as cert_id
    from queuetruth.data.merge import dataset_id as merge_id
    from queuetruth.dq.meta import dataset_id as dq_id

    shas = ["b" * 64, "a" * 64]
    for old in (cert_id, merge_id, dq_id):
        with pytest.warns(DeprecationWarning, match="queuetruth.dataset.dataset_id"):
            assert old(shas, "itch") == dataset_id(shas, "itch")


def test_bounded_reader_moved_without_changing_its_limits(tmp_path: Path) -> None:
    from queuetruth.dataset import MAX_RATIO, expansion_limit, read_parquet_bounded

    p = tmp_path / "t.parquet"
    pq.write_table(pa.table({"a": [1, 2, 3]}), p)
    assert read_parquet_bounded(p).num_rows == 3
    assert expansion_limit(1, 0) == MAX_RATIO


def test_instrument_units_through_the_dataset() -> None:
    from queuetruth import units

    ds = open_dataset(SYNTH)
    s = ds.sessions[0]
    got = units.instrument_units(ds, s.instrument_id, s.open_ns)
    assert got == units.instrument_units(SYNTH, s.instrument_id, s.open_ns)
    assert set(got) == {"symbol", "price_exp", "size_exp", "tick_size", "session_tz"} and got["session_tz"]
    assert units.instrument_units(ds, 999_999) == {}
