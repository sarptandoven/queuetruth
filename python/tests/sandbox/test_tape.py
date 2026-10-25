"""The tape: the cut rule (complete events, prefix, end of data, cuts larger than the pending cap), nesting of cuts,
compaction, the scratch dataset's manifest, the certification view, and `open_source`'s resolution, containment,
file checks and errors."""

from __future__ import annotations

import json
import os
import random
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import queuetruth as qt
from queuetruth.errors import QtError
from queuetruth.sandbox import tape as tape_mod
from queuetruth.sandbox._util import sha256_text
from queuetruth.sandbox.tape import SourceCursor, certification_view, check_partition_files, open_source
from queuetruth.sandbox.tape import Tape as ScratchTape
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.score._io import make_table
from sandbox.sbx_helpers import SYNTH, SYNTH_NASDAQ, Tape, at, prefix_rows, px, write_dataset


def _ts(t: pa.Table | None) -> list[int]:
    return [] if t is None else t.column("ts_event").cast(pa.int64()).to_pylist()


def test_cut_releases_complete_events_only(tmp_path: Path) -> None:
    t = Tape()
    t.add(at(1), "B", px(0), 5)
    # one event whose records carry two timestamps (10.0 and 10.5)
    t.event([t._rec(at(10), "A", "B", px(1), 1, 11), t._rec(at(10.5), "A", "B", px(2), 1, 12)])
    t.add(at(11), "A", px(5), 5)
    t.event([t._rec(at(12), "A", "A", px(6), 1, 13)])
    t.events[-1][-1]["flags"] = 0  # a trailing event without F_LAST (data ends inside it)
    ds = write_dataset(tmp_path / "ds", t)
    cur = SourceCursor(open_source(ds, instrument_id=1).mbo_paths(), batch_rows=2)
    assert _ts(cur.take_until(at(10.2))) == [at(1)]  # the 10.0/10.5 event is not complete by 10.2
    assert cur.take_until(at(10.4)) is None
    assert _ts(cur.take_until(at(10.5))) == [at(10), at(10.5)]
    assert _ts(cur.take_until(at(11.9))) == [at(11)]
    assert not cur.eof
    assert _ts(cur.take_until(at(12))) == [at(12)]  # end of data: the unterminated tail goes too
    assert cur.eof and cur.take_until(at(99)) is None
    assert cur.rows_released == 5 and cur.varying_events == 1 and cur.ts_decreases == 0


def test_cuts_nest_and_match_an_independent_prefix(tmp_path: Path) -> None:
    src = open_source(SYNTH, instrument_id=2, session_date="2026-06-15")
    p = src.partition
    rng = random.Random(5)
    cuts = sorted(rng.randrange(p.ts_min, p.ts_max) for _ in range(30))
    cur = SourceCursor(src.mbo_paths(), batch_rows=4096)
    n = 0
    for c in cuts:
        got = cur.take_until(c)
        n += 0 if got is None else got.num_rows
        assert n == prefix_rows(SYNTH, 2, p.session_date, c)
    one = SourceCursor(src.mbo_paths()).take_until(cuts[-1])
    assert one is not None and one.num_rows == n


def test_scratch_dataset_compacts_and_stays_valid(tmp_path: Path) -> None:
    src = open_source(SYNTH_NASDAQ, instrument_id=1, session_date="2026-06-15")
    tape = ScratchTape(src, tmp_path / "tape", compact_every=4)
    p = src.partition
    step = (p.ts_max - p.ts_min) // 23
    for k in range(23):
        tape.extend_to(p.ts_min + k * step)
        man = json.loads((tape.root / "manifest.json").read_text())
        files = man["partitions"][0]["files"] if man["partitions"] else []
        assert len(files) <= 5
        for f in files:
            assert sha256_file(tape.root / f["path"]) == f["sha256"]
        trade_parts = list((tape.root / "trades_source").rglob("part-*.parquet"))
        assert len(trade_parts) <= 5
        if files:
            shas = sorted(f["sha256"] for f in files)
            assert man["dataset_id"] == sha256_text("\n".join(shas) + "\n" + man["source_format"])
    tape.extend_to(p.ts_max + 1)
    assert tape.cursor.eof and tape.rows == p.rows
    full = pa.concat_tables(
        [
            pq.read_table(tape.root / f["path"])
            for f in json.loads((tape.root / "manifest.json").read_text())["partitions"][0]["files"]
        ]
    )
    src_tab = pq.read_table(SYNTH_NASDAQ / src.partition.files[0])
    assert full.equals(src_tab.cast(full.schema))
    with pytest.raises(QtError) as e:
        tape.extend_to(p.ts_min)
    assert e.value.code == "QT-ENGINE-001"


@pytest.mark.requires_core
def test_certification_view_replays_like_the_whole_dataset(tmp_path: Path) -> None:
    src = open_source(SYNTH_NASDAQ, instrument_id=2, session_date="2026-06-15")
    view = certification_view(src, tmp_path / "view")
    assert all(p.is_symlink() for p in (view / "mbo").rglob("*.parquet"))
    p = src.partition
    rows = [
        {
            "shadow_id": f"s{k}",
            "instrument_id": 2,
            "side": ord("B") if k % 2 else ord("A"),
            "price_mode": "touch",
            "price": None,
            "offset_ticks": k % 3 - 1,
            "qty": 1 + k % 4,
            "ts_submit": p.ts_min + (k + 1) * 61_000_000_000,
            "latency_ns": 1_000_000,
            "ts_cancel": None if k % 3 else p.ts_min + (k + 1) * 61_000_000_000 + 30_000_000_000,
            "group": None,
            "source": "user",
            "order_key": None,
        }
        for k in range(60)
    ]
    write_table(make_table("qt.shadow_spec", rows), tmp_path / "s.parquet")
    for name, ds in (("whole", SYNTH_NASDAQ), ("view", view)):
        qt.replay(
            {
                "dataset": {"path": str(ds)},
                "out": str(tmp_path / name),
                "shadows": {"path": str(tmp_path / "s.parquet")},
                "bbo": "none",
            }
        )
    for table in ("shadow_results", "shadow_fills", "queue_trajectory"):
        a = pq.read_table(tmp_path / "whole" / f"{table}.parquet")
        b = pq.read_table(tmp_path / "view" / f"{table}.parquet")
        assert a.drop_columns(["dq_flags"] if table == "shadow_results" else []).equals(
            b.drop_columns(["dq_flags"] if table == "shadow_results" else [])
        ), table
    assert pq.read_table(tmp_path / "view" / "shadow_fills.parquet").num_rows > 20


def test_open_source_resolution_and_errors(tmp_path: Path) -> None:
    assert open_source(SYNTH, symbol="QTAU6", session_date="2026-06-16").partition.session_date == 20620
    with pytest.raises(QtError) as e:
        open_source(SYNTH, instrument_id=1)
    assert e.value.code == "QT-CONFIG-001" and "session_date" in (e.value.hint or "")
    with pytest.raises(QtError) as e:
        open_source(SYNTH)
    assert "4 instrument-days" in e.value.message
    with pytest.raises(QtError) as e:
        open_source(SYNTH, instrument_id=1, symbol="QTAU6")
    assert "not both" in e.value.message
    with pytest.raises(QtError) as e:
        open_source(SYNTH, symbol="ZZZ")
    assert "available" in (e.value.hint or "")
    bad = tmp_path / "bad"
    shutil.copytree(SYNTH, bad)
    (bad / "manifest.json").write_text("{}")
    with pytest.raises(QtError) as e:
        open_source(bad, instrument_id=1, session_date="2026-06-15")
    assert e.value.code == "QT-INPUT-002"
    (bad / "manifest.json").write_text("[1]")
    with pytest.raises(QtError) as e:
        open_source(bad)
    assert e.value.code == "QT-INPUT-002"
    (bad / "manifest.json").unlink()
    with pytest.raises(QtError) as e:
        open_source(bad)
    assert e.value.code == "QT-INPUT-001"
    for mutate, code in (
        (lambda m: m["partitions"][0]["files"][0].__setitem__("path", "../x.parquet"), "QT-INPUT-002"),
        (lambda m: m["partitions"][0]["files"][0].__setitem__("path", "mbo/none.parquet"), "QT-INPUT-001"),
        (lambda m: m.__setitem__("rule_set", "bogus"), "QT-CONFIG-001"),
    ):
        man = json.loads((SYNTH / "manifest.json").read_text())
        mutate(man)
        (bad / "manifest.json").write_text(json.dumps(man))
        with pytest.raises(QtError) as e:
            open_source(bad, instrument_id=man["partitions"][0]["instrument_id"], session_date="2026-06-15")
        assert e.value.code == code
    man = json.loads((SYNTH / "manifest.json").read_text())
    (bad / "manifest.json").write_text(json.dumps(man))
    inst = pq.read_table(SYNTH / "instruments.parquet")
    write_table(inst.slice(1), bad / "instruments.parquet")
    with pytest.raises(QtError) as e:
        open_source(bad, instrument_id=1, session_date="2026-06-15")
    assert e.value.code == "QT-INPUT-002" and "qt.instruments" in e.value.message


def test_dataset_without_event_boundaries_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    t = Tape()
    for k in range(40):
        t.add(at(1 + k), "B", px(-k), 1)
    for ev in t.events:
        ev[-1]["flags"] = 0
    ds = write_dataset(tmp_path / "ds", t)
    monkeypatch.setattr(tape_mod, "MAX_PENDING_ROWS", 10)
    cur = SourceCursor(open_source(ds, instrument_id=1).mbo_paths(), batch_rows=4)
    with pytest.raises(QtError) as e:
        cur.take_until(at(30))
    assert e.value.code == "QT-CONFIG-001" and "F_LAST" in e.value.message


def test_cursor_counts_decreasing_timestamps(tmp_path: Path) -> None:
    t = Tape()
    t.add(at(5), "B", px(0), 1)
    t.add(at(4), "B", px(0), 1)
    t.add(at(6), "B", px(0), 1)
    ds = write_dataset(tmp_path / "ds", t)
    cur = SourceCursor(open_source(ds, instrument_id=1).mbo_paths())
    assert _ts(cur.take_until(at(5.5))) == [at(5), at(4)]
    assert cur.ts_decreases == 1


def test_a_cut_larger_than_the_pending_cap_is_released_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cut that spans more records than `MAX_PENDING_ROWS` (the first cut of an afternoon episode on a liquid
    instrument, or one long advance) is still exactly the cut: the cursor releases complete events early and keeps
    reading until a record past the cut."""
    t = Tape()
    t.add(at(1), "B", px(0), 5)
    rng = random.Random(3)
    for k in range(400):
        ts = at(2 + k * 0.05)
        if k % 7 == 3:  # multi-record events
            t.event([t._rec(ts, "A", "B", px(-1 - j), 1, 50_000 + 10 * k + j) for j in range(rng.randint(2, 5))])
        else:
            t.add(ts, "A" if k % 2 else "B", px(5 + k % 3) if k % 2 else px(-(k % 3)), 1)
    t.noop(at(30))
    ds = write_dataset(tmp_path / "ds", t)
    monkeypatch.setattr(tape_mod, "MAX_PENDING_ROWS", 50)
    src = open_source(ds, instrument_id=1)
    cur = SourceCursor(src.mbo_paths(), batch_rows=16)
    tape = ScratchTape(src, tmp_path / "tape", batch_rows=16)
    n = 0
    for cut in (at(12.0), at(12.0), at(13.37), at(19.99), at(25), at(31)):
        got = cur.take_until(cut)
        n += 0 if got is None else got.num_rows
        tape.extend_to(cut)
        want = prefix_rows(ds, 1, src.partition.session_date, cut)
        assert n == want == tape.rows, cut
        assert want > 3 * 50  # every cut spans several times the cap
    assert cur.eof and tape.cursor.eof and n == src.partition.rows
    assert cur.ts_decreases == 0 and cur.varying_events == 0


def _symlink_out(path: Path, outside: Path) -> None:
    path.rename(outside)
    path.symlink_to(outside)


def test_containment_is_checked_before_a_file_is_read(tmp_path: Path) -> None:
    """In MCP mode every file is checked before it is read: a manifest, instruments table, data-quality document or
    trades directory that resolves outside the root is refused without its content reaching an error message; the
    partition's files are opened through the real paths checked at open."""
    root = tmp_path / "root"
    ds = root / "ds"
    shutil.copytree(SYNTH_NASDAQ, ds)
    seen: list[Path] = []

    def contain(p: Path) -> Path:
        real = Path(os.path.realpath(p))
        if root.resolve() not in real.parents and real != root.resolve():
            raise QtError("QT-CONFIG-001", f"{p.name!r} resolves outside the root")
        seen.append(real)
        return real

    src = open_source(ds, instrument_id=1, session_date="2026-06-15", contain=contain)
    assert all(root.resolve() in p.parents for p in src.paths) and src.trades_files
    for name in ("manifest.json", "instruments.parquet", "data_quality.json"):
        other = tmp_path / f"copy-{name}"
        shutil.copytree(ds, other)
        _symlink_out(other / name, tmp_path / f"outside-{name}")
        if name == "manifest.json":  # the content must not leak: a manifest that is not even JSON is never parsed
            (tmp_path / f"outside-{name}").write_text("secret, not JSON")
        with pytest.raises(QtError) as e:
            open_source(other, instrument_id=1, session_date="2026-06-15", contain=contain)
        assert e.value.code == "QT-CONFIG-001" and "outside" in e.value.message and "secret" not in str(e.value)
    tdir = ds / "trades_source" / "date=2026-06-15" / "instrument_id=1"
    shutil.move(tdir, tmp_path / "outside-trades")
    tdir.symlink_to(tmp_path / "outside-trades")
    with pytest.raises(QtError) as e:
        open_source(ds, instrument_id=1, session_date="2026-06-15", contain=contain)
    assert e.value.code == "QT-CONFIG-001"


def test_the_certification_view_keeps_the_dataset_names_and_the_paths_resolved_at_open(tmp_path: Path) -> None:
    ds = tmp_path / "ds"
    shutil.copytree(SYNTH_NASDAQ, ds)
    tdir = ds / "trades_source" / "date=2026-06-15" / "instrument_id=1"
    (part,) = sorted(tdir.glob("part-*.parquet"))
    store = tmp_path / "store" / "prints-elsewhere.parquet"
    store.parent.mkdir()
    _symlink_out(part, store)  # a source print file linked under another name
    src = open_source(ds, instrument_id=1, session_date="2026-06-15")
    assert src.trades_names == (part.name,) and src.trades_files == (store.resolve(),)
    view = certification_view(src, tmp_path / "view")
    (linked,) = (view / "trades_source" / "date=2026-06-15" / "instrument_id=1").iterdir()
    assert linked.name == part.name and Path(os.readlink(linked)) == store.resolve()
    for rel, real in zip(src.partition.files, src.mbo_files, strict=True):
        assert Path(os.readlink(view / rel)) == real


def test_partition_files_are_checked_against_the_manifest(tmp_path: Path) -> None:
    ds = tmp_path / "ds"
    shutil.copytree(SYNTH, ds)
    src = open_source(ds, instrument_id=2, session_date="2026-06-16")
    assert check_partition_files(src) == []
    f = src.mbo_files[0]
    tab = pq.read_table(f)
    write_table(tab.slice(0, tab.num_rows - 1), f, tab.schema)  # same manifest, one record fewer
    (problem,) = check_partition_files(open_source(ds, instrument_id=2, session_date="2026-06-16"))
    assert src.partition.files[0] in problem and "sha256" in problem
