"""One operation, three callers: a replay run through the service's job executor (a child process in a job directory,
`runners.replay`), through the MCP run tool (`qt_local_run_replay`, a sandboxed child) and through Python
(`queuetruth.runner.run` and `qt.replay`) gives the same document, because all of them call `queuetruth.runner.run`.
The service's job kinds are operations of the table, and a job's paths stay inside its work directory."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from queuetruth.ops.table import by_service_kind, table
from queuetruth_service import runners
from queuetruth_service.executor import run_child

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tests" / "fixtures" / "synth_small"
#: Fields that name where a run wrote or how long it took.
RUN_LOCAL = ("spec", "files", "timings")


def answer(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in RUN_LOCAL}


def test_service_kinds_are_operations() -> None:
    ops = table()
    kinds = {op.service_kind for op in ops.values() if op.service_kind}
    assert kinds == {"replay", "zoo", "calibration", "scorecard", "report", "ingest"}
    assert kinds <= set(runners.TARGETS)
    for kind in kinds:
        op = by_service_kind(kind)
        assert op is not None
        module, _, fn = runners.TARGETS[kind].partition(":")
        assert module == "queuetruth_service.runners" and callable(getattr(runners, fn))
    assert runners.TARGETS["dataset_validate"] == runners.TARGETS["ingest"]


def test_job_paths_stay_in_the_work_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "job"
    work.mkdir()
    monkeypatch.chdir(work)
    with pytest.raises(Exception) as e:
        runners.replay({"dataset": {"path": str(tmp_path)}, "out": "out"})
    assert getattr(e.value, "code", None) == "QT-CONFIG-001"
    with pytest.raises(Exception) as e:
        runners.replay({"dataset": {"path": "inputs/x"}, "out": "../elsewhere"})
    assert getattr(e.value, "code", None) in ("QT-CONFIG-001", "QT-INPUT-001")


@pytest.mark.requires_core
@pytest.mark.asyncio
async def test_one_replay_three_callers(tmp_path: Path) -> None:
    from mcp import Client as McpClient

    import queuetruth as qt
    from queuetruth.mcp.local import RunLimits
    from queuetruth.mcp.server import build_server
    from queuetruth.runner import RunContext, run

    if not (DATA / "manifest.json").exists():
        pytest.skip("tests/fixtures/synth_small is missing (make fixtures)")
    # Python: sample shadows, then replay through the runner and through qt.replay
    shadows = tmp_path / "shadows.parquet"
    qt.sample_shadows({"dataset": {"path": str(DATA)}, "n": 25, "seed": 7, "out": str(shadows)})
    spec = {"dataset": {"path": str(DATA)}, "shadows": {"path": str(shadows)}}
    py = run("replay", spec, out=tmp_path / "py", context=RunContext(roots=[DATA, tmp_path])).document
    api = qt.replay(spec | {"out": str(tmp_path / "api")}).model_dump(mode="json", by_alias=True)

    # the service: a job directory holding its inputs, the child executor, the job runner of kind replay
    work = tmp_path / "job"
    shutil.copytree(DATA, work / "inputs" / "datasets" / "ds")
    (work / "inputs" / "uploads").mkdir(parents=True)
    shutil.copy(shadows, work / "inputs" / "uploads" / "shadows.parquet")
    job_spec = {
        "dataset": {"path": "inputs/datasets/ds"},
        "shadows": {"path": "inputs/uploads/shadows.parquet"},
        "out": "out",
    }
    res = await run_child(runners.TARGETS["replay"], job_spec, str(work), timeout_s=300)
    assert res.status == "ok", res.error
    service = res.summary
    assert service is not None

    # MCP: the run tool in a sandbox whose roots hold the dataset and the shadows
    root = tmp_path / "mcp"
    root.mkdir()
    app = build_server(
        local=True,
        allow_run=True,
        root=root,
        data_roots=[str(DATA), str(tmp_path)],
        run_limits=RunLimits(min_free_bytes=0),
        tool_modules=(),
    )
    async with McpClient(app.server) as c:
        got = await c.call_tool("qt_local_run_replay", {"spec": spec})
    assert not got.is_error, got.content
    via_mcp = got.structured_content
    assert via_mcp is not None

    assert py["run_id"] == api["run_id"] == service["run_id"] == via_mcp["run_id"]
    assert answer(py) == answer(api) == answer(service) == answer(via_mcp)
