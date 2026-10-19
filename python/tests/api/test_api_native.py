"""`queuetruth.api` over the native core on tests/fixtures/synth_small: replay / sample / mirror from Python, run_id
parity with `queuetruth.schemas.run_id`, the GIL released during native work, error mapping, Arrow interchange,
Session, l2 npz export, from-live schedules, live why traces and the synthetic quickstart fixture."""

from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import queuetruth as qt
from queuetruth import api
from queuetruth.schemas import models as m
from queuetruth.schemas import run_id, specs
from queuetruth.schemas.arrow import ENGINE_VERSION, SCHEMAS
from queuetruth.score._io import resolve_ref

pytestmark = pytest.mark.requires_core


@pytest.fixture(scope="module")
def synth(repo_root: Path) -> Path:
    p = repo_root / "tests" / "fixtures" / "synth_small"
    if not (p / "manifest.json").exists():
        pytest.fail("tests/fixtures/synth_small is missing (run `make fixtures`)")
    return p


@pytest.fixture(scope="module")
def shadows(synth: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("api") / "shadows.parquet"
    s = qt.sample_shadows({"dataset": {"path": str(synth)}, "out": str(out), "n": 60, "seed": 4})
    assert isinstance(s, m.ShadowSampleSummary) and s.n == 60
    return out


def test_replay_from_python(synth: Path, shadows: Path, tmp_path: Path) -> None:
    s = qt.replay(
        {
            "dataset": {"path": str(synth)},
            "out": str(tmp_path / "r"),
            "shadows": {"path": str(shadows)},
            "rank_oracle": True,
        }
    )
    assert isinstance(s, m.ReplaySummary)
    assert s.stats.records > 100_000 and s.shadows is not None and s.shadows.n == 60
    assert s.rank_oracle is not None and s.rank_oracle.price_priority_violations == 0
    assert (tmp_path / "r" / "shadow_results.parquet").exists()
    on_disk = json.loads((tmp_path / "r" / "summary.json").read_text())
    assert on_disk["run_id"] == s.run_id
    # Spec models are accepted as well as dicts; the run_id does not depend on out / threads.
    spec = specs.ReplaySpec(dataset={"path": str(synth)}, shadows={"path": str(shadows)}, rank_oracle=True, threads=2)
    s2 = qt.replay(spec)
    assert s2.run_id == s.run_id


def test_run_id_parity(synth: Path, shadows: Path) -> None:
    spec = specs.ReplaySpec(dataset={"path": str(synth)}, shadows={"path": str(shadows)}, out="x", rank_oracle=True)
    doc = spec.model_dump(mode="json")
    py = run_id(doc, ENGINE_VERSION, resolve_ref)
    assert api.run_id(spec) == py
    assert api.run_id(doc) == py
    # The summary's run_id is the identity of the spec as the core read it (its echo), whatever the schema version.
    summ = qt.replay(spec.model_copy(update={"out": None}))
    assert summ.run_id == run_id(summ.spec, ENGINE_VERSION, resolve_ref)


@pytest.mark.integration  # gate test: needs a current engine build (by_placement keys must be offsets, schema IdKey)
def test_mirror_real_orders(synth: Path, tmp_path: Path) -> None:
    s = qt.mirror_real_orders({"dataset": {"path": str(synth)}, "out": str(tmp_path / "m.parquet"), "n": 40})
    assert 0 < s.n <= 40
    t = qt.read_table(str(tmp_path / "m.parquet"), "qt.shadow_spec")
    assert t.num_rows == s.n


def test_errors_are_qt_errors(synth: Path, tmp_path: Path) -> None:
    with pytest.raises(qt.QtError) as e:
        qt.replay({"dataset": {"path": str(tmp_path / "missing")}})
    assert e.value.code == "QT-INPUT-001"
    with pytest.raises(qt.QtError) as e:
        qt.replay({"dataset": {"path": str(synth)}, "bbo": "sometimes"})
    assert e.value.code == "QT-CONFIG-001" and "bbo" in e.value.message
    with pytest.raises(qt.QtError) as e:
        qt.replay({"dataset": {"path": str(synth)}, "no_such_field": 1})
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(qt.QtError) as e:
        qt.why(
            {
                "dataset": {"path": str(synth)},
                "shadows": {"path": str(synth / "truth" / "shadows.parquet")},
                "shadow_id": "x",
            }
        )
    assert e.value.code == "QT-CONFIG-001"


def test_gil_released_during_native_work(synth: Path) -> None:
    """While replays run in another thread, this thread keeps executing Python bytecode.

    The worker repeats the replay until at least 0.25 s of native work has run, so the window stays long enough to
    measure however fast one replay of the fixture is."""
    window: dict[str, float] = {}

    def work() -> None:
        window["start"] = time.perf_counter()
        while time.perf_counter() - window["start"] < 0.25:
            qt.replay({"dataset": {"path": str(synth)}, "bbo": "full", "rank_oracle": True})
        window["end"] = time.perf_counter()

    th = threading.Thread(target=work)
    ticks: list[float] = []
    th.start()
    while th.is_alive():
        ticks.append(time.perf_counter())
    th.join()
    inside = [t for t in ticks if window["start"] < t < window["end"]]
    assert window["end"] - window["start"] > 0.05
    assert len(inside) > 1000, f"only {len(inside)} Python iterations ran during the native call"


def test_two_threads_replay_concurrently(synth: Path, shadows: Path, tmp_path: Path) -> None:
    out: dict[int, m.ReplaySummary] = {}

    def work(i: int) -> None:
        out[i] = qt.replay(
            {"dataset": {"path": str(synth)}, "out": str(tmp_path / f"r{i}"), "shadows": {"path": str(shadows)}}
        )

    ths = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert set(out) == {0, 1}
    assert out[0].run_id == out[1].run_id
    assert [f.sha256 for f in out[0].files] == [f.sha256 for f in out[1].files]


def test_read_table_and_write_table(shadows: Path, tmp_path: Path) -> None:
    from queuetruth import _core

    t = qt.read_table(str(shadows))
    assert isinstance(t, pa.Table) and t.schema.metadata[b"qt.schema"] == b"qt.shadow_spec"
    assert t.schema.equals(SCHEMAS["qt.shadow_spec"], check_metadata=False)
    with pytest.raises(qt.QtError) as e:
        qt.read_table(str(shadows), "qt.mbo")
    assert e.value.code == "QT-INPUT-002"
    entry = json.loads(_core.write_table(t, str(tmp_path / "copy.parquet")))
    assert entry["rows"] == t.num_rows and len(entry["sha256"]) == 64
    assert pq.read_table(tmp_path / "copy.parquet").equals(t)
    plain = tmp_path / "plain.parquet"
    pq.write_table(pa.table({"a": [1]}), plain)
    with pytest.raises(qt.QtError):
        qt.read_table(str(plain))


def test_session(synth: Path) -> None:
    sess = qt.Session(str(synth))
    assert sess.bbo(1) is None
    sess.advance_to(1_781_540_000_000_000_000)
    b = sess.bbo(1)
    assert b is not None and b["bid"] is not None and b["ask"] is not None
    assert b["bid"][0] < b["ask"][0]
    bids, asks = sess.depth(1, 3)
    assert bids[0] == b["bid"] and asks[0] == b["ask"] and len(bids) <= 3
    assert sess.queue_ahead(1, "B", b["bid"][0]) == (b["bid"][1], b["bid"][2])
    with pytest.raises(ValueError):
        sess.queue_ahead(1, "X", 1)
    with pytest.raises(qt.QtError):
        qt.Session(str(synth), venue="nowhere")


def test_export_l2_npz(synth: Path, tmp_path: Path) -> None:
    s = qt.export(
        {
            "target": "l2_npz",
            "dataset": {"path": str(synth)},
            "instrument_id": 2,
            "session_date": "2026-06-16",
            "out": str(tmp_path / "x.npz"),
            "feed_latency_ns": 1000,
        }
    )
    assert s.target == "l2_npz" and s.files[0].rows and s.files[0].rows > 0
    data = np.load(tmp_path / "x.npz")["data"]
    assert data.dtype.names == ("ev", "exch_ts", "local_ts", "px", "qty", "order_id", "ival", "fval")
    assert data.dtype.itemsize == 64 and len(data) == s.files[0].rows
    # The in-crate zip writer: one DEFLATE member (as np.savez_compressed writes), CRC-32 verified by zipfile, fixed
    # 1980-01-01 timestamp.
    with zipfile.ZipFile(tmp_path / "x.npz") as z:
        assert z.testzip() is None and z.namelist() == ["data.npy"]
        info = z.getinfo("data.npy")
        assert info.compress_type == zipfile.ZIP_DEFLATED and info.date_time == (1980, 1, 1, 0, 0, 0)


def test_synthesize_and_bbo_from_l2_shapes(synth: Path, tmp_path: Path) -> None:
    from queuetruth import _core

    files = json.loads(
        _core.synthesize_l2(
            json.dumps({"dataset": {"path": str(synth)}, "out": str(tmp_path), "l2": {"depth_levels": 5}})
        )
    )["files"]
    assert len(files) == 4 and all(f["name"].startswith("l2/date=") for f in files)
    with pytest.raises(_core.QtCoreError) as e:
        _core.bbo_from_l2(json.dumps({"dataset": {"path": str(synth)}, "out": str(tmp_path / "b"), "threads": 1}))
    assert e.value.code == "QT-CONFIG-001"


def test_shadows_from_live_and_live_why(synth: Path, tmp_path: Path) -> None:
    live = synth / "own_orders" / "live"
    s = qt.shadows_from_live(str(live), str(tmp_path / "sched.parquet"))
    assert s.n > 0 and sum(s.by_instrument.values()) == s.n
    t = qt.read_table(str(tmp_path / "sched.parquet"), "qt.shadow_spec")
    assert t.num_rows == s.n
    key = t.column("order_key")[0].as_py()
    w = qt.why({"dataset": {"path": str(synth)}, "live": {"path": str(live)}, "order_key": key, "max_events": 20})
    assert w.subject.order_key == key and w.subject.shadow_id is not None
    assert len(w.events) <= 20
    with pytest.raises(qt.QtError):
        qt.why({"dataset": {"path": str(synth)}, "live": {"path": str(live)}, "order_key": "no:such:key"})


def test_referee_fixtures(tmp_path: Path) -> None:
    dirs = qt.referee_fixtures(str(tmp_path / "ref"))
    assert len(dirs) >= 11 and all(Path(d).is_dir() for d in dirs)


@pytest.mark.integration  # gate test: the committed fixture must be regenerated with the current testkit
def test_fixtures_synth_reproduces_committed_fixture(synth: Path, tmp_path: Path) -> None:
    s = api.fixtures_synth(str(tmp_path / "synth"))
    assert s.fixtures[0].name == "synth_small"
    assert (tmp_path / "synth" / "MANIFEST.sha256").read_text() == (synth / "MANIFEST.sha256").read_text()
    with pytest.raises(qt.QtError) as e:
        api.fixtures_synth(str(tmp_path))  # not empty and not a fixture: refused, nothing deleted
    assert e.value.code == "QT-CONFIG-001"
    assert (tmp_path / "synth" / "manifest.json").exists()


@pytest.mark.integration  # gate test: engine and testkit reference must agree on A1
def test_replay_options_exclude_order_ids_a1(synth: Path, tmp_path: Path) -> None:
    """Audit A1: excluding the own orders' venue ids reproduces own_orders/expected_results.parquet exactly."""
    own = synth / "own_orders"
    orders = pq.read_table(own / "live" / "orders.parquet")
    ids = orders.column("venue_order_id").to_pylist()
    pairs = [
        {"instrument_id": i, "order_id": int(d)}
        for i, d in zip(orders.column("instrument_id").to_pylist(), ids, strict=True)
    ]
    want = (own / "expected_results.parquet").read_bytes()
    base = {"dataset": {"path": str(synth)}, "shadows": {"path": str(own / "schedule.parquet")}, "bbo": "none"}
    for name, excl in [("bare", ids), ("pairs", pairs)]:
        s = qt.replay({**base, "out": str(tmp_path / name)}, exclude_order_ids=excl)
        assert (tmp_path / name / "shadow_results.parquet").read_bytes() == want, name
        assert any(w.startswith("exclude_order_ids:") for w in s.warnings)
    qt.replay({**base, "out": str(tmp_path / "none")})
    assert (tmp_path / "none" / "shadow_results.parquet").read_bytes() != want
    et = qt.replay({**base, "out": str(tmp_path / "et")}, fill_rule="et", hidden_print_policy="ignore")
    assert any("fill rule Et" in w for w in et.warnings)
    with pytest.raises(qt.QtError) as e:
        qt.replay(base, fill_rule="sometimes")
    assert e.value.code == "QT-CONFIG-001"


def test_fill_liquidity(tmp_path: Path, synth: Path, shadows: Path) -> None:
    # A5 as refined: only a shadow marketable on arrival takes liquidity; a resting shadow crossed later is the maker.
    assert api.fill_liquidity("crossed_at_live") == "taker"
    assert {api.fill_liquidity(r) for r in ("crossed", "queue_depleted", "trade_through")} == {"maker"}
    qt.replay({"dataset": {"path": str(synth)}, "out": str(tmp_path / "r"), "shadows": {"path": str(shadows)}})
    t = api.fill_liquidity(pq.read_table(tmp_path / "r" / "shadow_fills.parquet"))
    assert set(t.column("liquidity").to_pylist()) <= {"maker", "taker"}
    with pytest.raises(qt.QtError):
        api.fill_liquidity("nonsense")


def test_replay_options_reach_the_engine(synth: Path, tmp_path: Path) -> None:
    """Excluding the own orders changes the exact results; the exclusion count is reported."""
    own = synth / "own_orders"
    ids = pq.read_table(own / "live" / "orders.parquet").column("venue_order_id").to_pylist()
    base = {"dataset": {"path": str(synth)}, "shadows": {"path": str(own / "schedule.parquet")}, "bbo": "none"}
    s = qt.replay({**base, "out": str(tmp_path / "ex")}, exclude_order_ids=ids)
    assert any(w.startswith("exclude_order_ids:") and "matched=0 " not in w for w in s.warnings), s.warnings
    plain = qt.replay({**base, "out": str(tmp_path / "plain")})
    a = (tmp_path / "ex" / "shadow_results.parquet").read_bytes()
    assert a != (tmp_path / "plain" / "shadow_results.parquet").read_bytes()
    # open conflict 8 (OC-34): keyword options are spec fields, so they enter run_id and the spec echo
    et = qt.replay({**base, "out": str(tmp_path / "et")}, fill_rule="et")
    assert len({plain.run_id, s.run_id, et.run_id}) == 3
    assert et.spec["fill_rule"] == "et" and s.spec["exclude_order_ids"]
    same = qt.replay({**base, "out": str(tmp_path / "same"), "exclude_order_ids": s.spec["exclude_order_ids"]})
    assert same.run_id == s.run_id
    # twin-linking counters are structured fields of shadow_diagnostics (additive, default 0)
    d = s.shadow_diagnostics
    assert d is not None and d.twins_linked == 200 and d.twin_fills_with_queue_ahead >= 0
    assert plain.shadow_diagnostics is not None and plain.shadow_diagnostics.twins_linked == 0
    old = d.model_dump(mode="json")
    for k in ("twins_linked", "twin_fills_with_queue_ahead", "twin_fills_off_display"):
        old.pop(k)
    assert m.ShadowDiagnostics.model_validate(old).twin_fills_off_display == 0
