"""The operation table (`queuetruth.ops.table`) and the one runner (`queuetruth.runner.run`): every operation is
declared once and collected; the runner validates the spec, keeps paths inside the caller's roots, refuses the network
unless allowed, puts outputs where the caller says and returns the same document as the Python API."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel

from queuetruth.errors import QtError
from queuetruth.ops import table as optable
from queuetruth.runner import Roots, RunContext, run

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tests" / "fixtures" / "synth_small"
VERBS = ("check", "fit", "compare", "data", "atlas", "demo")
#: Fields that name where a run wrote or how long it took (they differ between two runs of one spec).
RUN_LOCAL = ("spec", "files", "timings")


def synthetic_atlas(where: Path) -> Path:
    """A synthetic atlas data API (the helper of python/tests/atlas/test_atlas_lookup.py), written under `where`."""
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[0] / "atlas" / "test_atlas_lookup.py"
    spec = importlib.util.spec_from_file_location("qt_optable_atlas_lookup", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod._write_api(where)


def same_answer(a: dict, b: dict) -> bool:
    return {k: v for k, v in a.items() if k not in RUN_LOCAL} == {k: v for k, v in b.items() if k not in RUN_LOCAL}


def test_the_table_is_collected_from_the_declaring_modules() -> None:
    ops = optable.table()
    assert list(ops) == sorted(ops)
    for verb in VERBS:
        assert verb in ops and ops[verb].tool == f"qt_{verb}"
        assert ops[verb].fn.__module__ == f"queuetruth.{verb}.operation"
    core = {"ingest", "replay", "shadows", "zoo", "calibrate", "score", "report", "why", "export", "dq"}
    assert core <= set(ops)
    for op in ops.values():
        assert issubclass(op.spec, BaseModel) and issubclass(op.document, BaseModel)
        assert op.description.endswith(".") and "\n" not in op.description
        assert op.cost in optable.COSTS
        assert op.writes == (op.out_field is not None)
        if op.out_field is not None:
            assert op.out_field in op.spec.model_fields
    # each operation is declared by exactly one module, next to the code it runs
    declared = [
        op.name for m in optable._declaring_modules() for op in getattr(__import__(m, fromlist=["x"]), "OPERATIONS", ())
    ]
    assert sorted(declared) == list(ops)
    assert optable.by_tool("qt_local_run_replay") is ops["replay"]
    assert optable.by_service_kind("calibration") is ops["calibrate"]


def test_unknown_operation_and_invalid_spec() -> None:
    with pytest.raises(QtError) as e:
        run("no_such_op", {})
    assert e.value.code == "QT-USAGE-001" and "replay" in (e.value.hint or "")
    with pytest.raises(QtError) as e:
        run("replay", {"dataset": {"path": str(DATA)}, "no_such_field": 1})
    assert e.value.code == "QT-CONFIG-001" and "ReplaySpec" in e.value.message


def test_paths_stay_inside_the_roots(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.parquet").write_bytes(b"")
    root = tmp_path / "root"
    root.mkdir()
    ctx = RunContext(roots=[root])
    with pytest.raises(QtError) as e:
        run("replay", {"dataset": {"path": str(outside)}}, context=ctx)
    assert e.value.code == "QT-CONFIG-001" and "outside the allowed roots" in e.value.message
    os.symlink(outside, root / "link")
    with pytest.raises(QtError) as e:  # a symlink that leads out is resolved first
        run("replay", {"dataset": {"path": "link"}}, context=ctx)
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        run("replay", {"dataset": {"path": str(DATA)}}, out=str(outside / "o"), context=RunContext(roots=[DATA]))
    assert e.value.code == "QT-CONFIG-001" and "spec.out" in e.value.message
    with pytest.raises(QtError) as e:
        run(
            "replay",
            {"dataset": {"dataset_id": "ds_" + "0" * 26}},
            context=RunContext(roots=[root], path_refs_only=True),
        )
    assert e.value.code == "QT-CONFIG-001" and "service ids" in e.value.message
    (root / "in.parquet").write_bytes(b"")
    roots = Roots([root])
    got = roots.refs({"a": {"path": "in.parquet"}, "b": [1]}, "spec", resolve=True, path_refs_only=True)
    assert got == {"a": {"path": str((root / "in.parquet").resolve())}, "b": [1]}
    assert roots.refs({"a": {"path": "in.parquet"}}, "spec", resolve=False, path_refs_only=True) == {
        "a": {"path": "in.parquet"}
    }


def test_network_is_refused_unless_allowed() -> None:
    with pytest.raises(QtError) as e:
        run("atlas", {"library": "nautilus"})
    assert e.value.code == "QT-CONFIG-001" and "needs the network" in e.value.message
    with pytest.raises(QtError) as e:
        run("demo", {})
    assert e.value.code == "QT-CONFIG-001"
    assert optable.get("check").uses_network(optable.validate(optable.get("check"), {"backtest": [{"path": "x"}]}))
    assert not optable.get("replay").network


def test_atlas_from_a_local_copy_matches_the_python_api(tmp_path: Path) -> None:
    atlas = synthetic_atlas(tmp_path / "atlas")
    import queuetruth as qt

    res = run("atlas", {"library": "nautilus", "source": {"path": str(atlas)}}, context=RunContext(roots=[atlas]))
    direct = qt.atlas.lookup(library="nautilus", source=str(atlas))
    assert res.document == direct.doc
    assert res.summary == direct.summary and res.files == [] and res.out is None
    with pytest.raises(QtError) as e:
        run("atlas", {"source": {"path": str(atlas)}}, out="x")
    assert e.value.code == "QT-USAGE-001"


@pytest.mark.requires_core
def test_replay_through_the_runner_is_the_python_api(tmp_path: Path) -> None:
    import queuetruth as qt

    if not (DATA / "manifest.json").exists():
        pytest.skip("tests/fixtures/synth_small is missing (make fixtures)")
    sampled = run("shadows", {"dataset": {"path": str(DATA)}, "n": 20, "seed": 5}, out=tmp_path / "s")
    shadows = tmp_path / "s" / "shadows.parquet"
    assert sampled.files == [shadows] and sampled.document["n"] == 20
    spec = {"dataset": {"path": str(DATA)}, "shadows": {"path": str(shadows)}}
    res = run("replay", spec, out=tmp_path / "r", context=RunContext(roots=[DATA, tmp_path]))
    direct = qt.replay(spec | {"out": str(tmp_path / "r2")}).model_dump(mode="json", by_alias=True)
    assert res.document["run_id"] == direct["run_id"]
    assert same_answer(res.document, direct)
    assert res.out == tmp_path / "r" and (tmp_path / "r" / "summary.json") in res.files
    assert json.loads((tmp_path / "r" / "summary.json").read_text())["run_id"] == direct["run_id"]
    assert sorted(p.name for p in res.files) == sorted(p.name for p in (tmp_path / "r2").rglob("*") if p.is_file())
    # resolve_paths: the run sees real paths, so nothing can move under it
    link = tmp_path / "data-link"
    os.symlink(DATA, link)
    res2 = run(
        "replay",
        {"dataset": {"path": str(link)}, "shadows": {"path": str(shadows)}},
        context=RunContext(roots=[DATA, tmp_path], resolve_paths=True),
    )
    assert res2.document["run_id"] == direct["run_id"]
    shutil.rmtree(tmp_path / "r2")
