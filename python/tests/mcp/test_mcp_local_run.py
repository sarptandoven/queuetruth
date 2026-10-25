"""Run mode through the MCP client (in-process and over stdio): shadow sampling, replay, zoo and why on the committed
synthetic dataset without the service or Postgres. Results validate against the committed schemas and equal a
direct Python API run; a second identical call reuses the finished `<run root>/<kind>/<run_id>` directory; inputs
stay untouched; refusals (paths outside the roots, service ids, `out`, limits, timeouts) are `qt.error` tool results
that leave no output behind; run tools are listed only in run mode, with input schemas that accept only `{"path"}`
references, and name their output directory in `_meta`. A directory whose summary does not validate is replaced, and
identical specs started together run once."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import jsonschema
import mcp.types as types
import pytest
from mcp import Client as McpClient
from mcp import StdioServerParameters
from mcp.shared.exceptions import MCPError

from queuetruth.mcp import server as qt_server
from queuetruth.mcp.local import LocalRunner, RunLimits
from queuetruth.mcp.server import RUN_INSTRUCTIONS, RUN_TOOL_NAMES, build_server, instructions_for
from queuetruth.mcp.tools import TOOLS
from queuetruth.schemas.models import parse_document

REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "tests" / "fixtures" / "synth_small"
SCHEMAS = REPO / "schemas" / "json"
LOCAL = [t.name for t in TOOLS if t.local]
pytestmark = pytest.mark.requires_core


def committed(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text())


def validate(doc: dict[str, Any], name: str) -> None:
    jsonschema.Draft202012Validator(committed(name)).validate(doc)
    parse_document(doc)


def tree_state(root: Path) -> dict[str, tuple[int, int, str]]:
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else ""
        out[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns, digest)
    return out


def note_of(res: types.CallToolResult) -> str:
    return next(c.text for c in res.content if isinstance(c, types.TextContent) and c.text.startswith("outputs: "))


def links_of(res: types.CallToolResult) -> list[types.ResourceLink]:
    return [c for c in res.content if isinstance(c, types.ResourceLink)]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    if not (FIXTURE / "manifest.json").exists():
        pytest.skip("tests/fixtures/synth_small is missing (make fixtures)")
    r = tmp_path / "root"
    r.mkdir()
    return r


def run_server(root: Path, **kw: Any):
    limits = kw.pop("run_limits", RunLimits(min_free_bytes=0))
    return build_server(
        local=True, allow_run=True, root=root, data_roots=[str(FIXTURE)], run_limits=limits, tool_modules=(), **kw
    )


# ---- listing ------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_tools_listed_only_in_run_mode(root: Path) -> None:
    async with McpClient(build_server(local=True, root=root).server) as c:
        names = [t.name for t in (await c.list_tools()).tools]
        assert names == LOCAL
        with pytest.raises(MCPError):
            await c.call_tool("qt_local_run_zoo", {"spec": {}})
    app = run_server(root)
    async with McpClient(app.server) as c:
        tools = (await c.list_tools()).tools
        text = c.instructions or ""
    assert [t.name for t in tools] == [*LOCAL, *RUN_TOOL_NAMES]
    by = {t.name: t for t in tools}
    for name, spec_model, out in (
        ("qt_local_sample_shadows", "ShadowSampleSpec", "qt.shadow_sample_summary"),
        ("qt_local_run_replay", "ReplaySpec", "qt.replay_summary"),
        ("qt_local_run_zoo", "ZooSpec", "qt.zoo_summary"),
    ):
        t = by[name]
        assert t.input_schema["properties"]["spec"] == {"$ref": f"#/$defs/Local{spec_model}"}
        jsonschema.Draft202012Validator.check_schema(t.input_schema)
        props = t.input_schema["$defs"][f"Local{spec_model}"]["properties"]
        assert "out" not in props and props["dataset"] == {"$ref": "#/$defs/PathRef"}
        assert not {"DatasetIdRef", "UploadRef", "JobFileRef"} & set(t.input_schema["$defs"])
        assert t.output_schema == committed(out)
        assert t.annotations.read_only_hint is False and t.annotations.destructive_hint is False
        assert t.annotations.idempotent_hint is True
    assert by["qt_local_why"].annotations.read_only_hint is True
    # run-mode instructions name the run tools, and no tool that is not listed
    named = {n for n in [*[t.name for t in TOOLS], *RUN_TOOL_NAMES] if n in text}
    assert set(RUN_TOOL_NAMES) <= named and named - {t.name for t in tools} == {"qt_explain_metric"}
    assert RUN_INSTRUCTIONS in text and "never write." not in text
    assert instructions_for(True) == instructions_for(True, False)  # unchanged without run mode
    assert RUN_INSTRUCTIONS in instructions_for(False, True)


# ---- end to end ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sample_zoo_replay_why_end_to_end(root: Path) -> None:
    import queuetruth as qt

    before = tree_state(FIXTURE)
    app = run_server(root)
    runs = root / "qt-mcp-runs"
    async with McpClient(app.server) as c:
        # 1. shadows
        sample_spec = {"dataset": {"path": str(FIXTURE)}, "n": 30, "seed": 11}
        res = await c.call_tool("qt_local_sample_shadows", {"spec": sample_spec})
        assert not res.is_error, res.content
        validate(res.structured_content, "qt.shadow_sample_summary")
        assert res.structured_content["n"] == 30
        out_rel = note_of(res).split("outputs: ")[1].split(" (")[0]
        assert out_rel.startswith("qt-mcp-runs/shadows/") and "(new run; 2 file(s))" in note_of(res)
        meta = res.meta or {}
        assert meta["queuetruth/out_dir"] == str((root / out_rel).resolve()) and meta["queuetruth/reused"] is False
        assert meta["queuetruth/run_id"] == out_rel.rsplit("/", 1)[1]
        shadows = Path(meta["queuetruth/out_dir"]) / "shadows.parquet"  # <run root>/shadows/<run_id>/shadows.parquet
        assert {link.name for link in links_of(res)} == {"shadows.parquet", "summary.json"}
        link = next(x for x in links_of(res) if x.name == "shadows.parquet")
        assert link.uri == shadows.resolve().as_uri() and link.size == shadows.stat().st_size
        assert link.mime_type == "application/vnd.apache.parquet"
        # 2. zoo on the sampled shadows (a path inside the run root, relative to QT_MCP_ROOT)
        zoo_spec = {
            "dataset": {"path": str(FIXTURE)},
            "shadows": {"path": f"{out_rel}/shadows.parquet"},
            "models": ["hbt_power3[n=3]", "hbt_risk_adverse"],
            "bootstrap_b": 20,
        }
        z1 = await c.call_tool("qt_local_run_zoo", {"spec": zoo_spec})
        assert not z1.is_error, z1.content
        validate(z1.structured_content, "qt.zoo_summary")
        zdoc = z1.structured_content
        assert zdoc["n_shadows"] == 30 and len(zdoc["models"]) == 2 and zdoc["leaderboard"]
        assert z1.content[0].text.startswith(f"zoo {zdoc['run_id']}: 30 shadows x 2 models")
        zdir = runs / "zoo" / zdoc["run_id"]
        n_files = sum(1 for p in zdir.rglob("*") if p.is_file())
        assert {"summary.json", "zoo_predictions.parquet", "zoo_errors.parquet"} <= {p.name for p in zdir.iterdir()}
        assert note_of(z1) == f"outputs: qt-mcp-runs/zoo/{zdoc['run_id']} (new run; {n_files} file(s))"
        assert sorted(x.name for x in links_of(z1)) == sorted(p.name for p in zdir.iterdir())
        assert json.loads((zdir / "summary.json").read_text()) == zdoc
        t0 = time.monotonic()
        z2 = await c.call_tool("qt_local_run_zoo", {"spec": zoo_spec | {"threads": 2}})  # threads: same run_id
        assert time.monotonic() - t0 < 2
        assert z2.structured_content == zdoc and "finished run reused" in note_of(z2)
        assert z2.meta["queuetruth/reused"] is True and z2.meta["queuetruth/run_id"] == zdoc["run_id"]
        # 3. replay
        rp_spec = {"dataset": {"path": str(FIXTURE)}, "shadows": {"path": str(shadows)}, "rank_oracle": True}
        r1 = await c.call_tool("qt_local_run_replay", {"spec": rp_spec})
        assert not r1.is_error, r1.content
        validate(r1.structured_content, "qt.replay_summary")
        assert r1.structured_content["shadows"]["n"] == 30
        r2 = await c.call_tool("qt_local_run_replay", {"spec": rp_spec})
        assert r2.structured_content == r1.structured_content and "finished run reused" in note_of(r2)
        # 4. why on one sampled shadow, under the run limits
        import pyarrow.parquet as pq

        sid = pq.read_table(shadows).column("shadow_id")[0].as_py()
        w = await c.call_tool(
            "qt_local_why", {"dataset_path": str(FIXTURE), "shadows_path": str(shadows), "shadow_id": sid}
        )
        assert not w.is_error, w.content
        validate(w.structured_content, "qt.why_trace")
        assert w.structured_content["subject"]["shadow_id"] == sid
    # the same answers as the Python API
    local = qt.run_zoo(zoo_spec | {"shadows": {"path": str(shadows)}}).model_dump(mode="json")
    assert local["run_id"] == zdoc["run_id"]
    assert {k: v for k, v in local.items() if k != "spec"} == {k: v for k, v in zdoc.items() if k != "spec"}
    assert tree_state(FIXTURE) == before  # inputs untouched
    assert list((runs / ".staging").iterdir()) == []  # no staging leftovers
    assert sorted(p.name for p in runs.iterdir()) == [".staging", "replay", "shadows", "zoo"]


@pytest.mark.asyncio
async def test_refusals_are_tool_errors_and_write_nothing(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.parquet").write_bytes(b"PAR1")
    (root / "escape.parquet").symlink_to(outside / "s.parquet")
    app = run_server(root, run_limits=RunLimits(min_free_bytes=0, max_threads=2, max_shadows=50))
    ds = {"path": str(FIXTURE)}
    cases = [
        ("qt_local_run_zoo", {"dataset": ds, "shadows": {"path": str(outside / "s.parquet")}}, "QT-CONFIG-001"),
        ("qt_local_run_zoo", {"dataset": ds, "shadows": {"path": "escape.parquet"}}, "QT-CONFIG-001"),
        ("qt_local_run_zoo", {"dataset": {"path": "/"}, "shadows": {"path": "escape.parquet"}}, "QT-CONFIG-001"),
        ("qt_local_run_replay", {"dataset": {"dataset_id": "ds_x"}}, "QT-CONFIG-001"),
        ("qt_local_run_replay", {"dataset": ds, "out": str(root / "x")}, "QT-CONFIG-001"),
        ("qt_local_run_replay", {"dataset": ds, "threads": 3}, "QT-CONFIG-001"),
        ("qt_local_sample_shadows", {"dataset": ds, "n": 51}, "QT-CONFIG-001"),
        ("qt_local_run_replay", {"dataset": ds, "bogus": 1}, "QT-USAGE-001"),
        ("qt_local_run_zoo", {"dataset": {"path": "missing"}, "shadows": {"path": "x"}}, "QT-INPUT-001"),
    ]
    async with McpClient(app.server) as c:
        for tool, spec, code in cases:
            res = await c.call_tool(tool, {"spec": spec})
            assert res.is_error, (tool, spec)
            assert res.structured_content["schema"] == "qt.error" and res.structured_content["code"] == code, (
                spec,
                res.structured_content,
            )
        bad_args = await c.call_tool("qt_local_run_zoo", {"spec": {}, "extra": 1})
        assert bad_args.is_error and bad_args.structured_content["code"] == "QT-USAGE-001"
        why = await c.call_tool(
            "qt_local_why", {"dataset_path": str(FIXTURE), "shadows_path": str(outside / "s.parquet"), "shadow_id": "a"}
        )
        assert why.is_error and why.structured_content["code"] == "QT-CONFIG-001"
    assert not (root / "qt-mcp-runs").exists() or not any((root / "qt-mcp-runs").rglob("*.parquet"))


@pytest.mark.asyncio
async def test_outputs_never_go_through_a_symlink(root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "qt-mcp-runs").mkdir()
    (root / "qt-mcp-runs" / "shadows").symlink_to(elsewhere, target_is_directory=True)
    app = run_server(root)
    async with McpClient(app.server) as c:
        res = await c.call_tool("qt_local_sample_shadows", {"spec": {"dataset": {"path": str(FIXTURE)}, "n": 3}})
    assert res.is_error and res.structured_content["code"] == "QT-CONFIG-001"
    assert "symlink" in res.structured_content["message"] and list(elsewhere.iterdir()) == []


@pytest.mark.asyncio
async def test_timeout_leaves_no_output(root: Path) -> None:
    app = run_server(root, run_limits=RunLimits(min_free_bytes=0, timeout_s=0.2))
    async with McpClient(app.server) as c:
        res = await c.call_tool("qt_local_sample_shadows", {"spec": {"dataset": {"path": str(FIXTURE)}, "n": 5}})
    assert res.is_error and res.structured_content["code"] == "QT-ENGINE-001"
    assert "timed out after 0.2 s" in res.structured_content["message"]
    runs = root / "qt-mcp-runs"
    assert list((runs / "shadows").iterdir()) == [] and list((runs / ".staging").iterdir()) == []  # no run dir


@pytest.mark.asyncio
async def test_disk_and_output_limits(root: Path) -> None:
    spec = {"dataset": {"path": str(FIXTURE)}, "n": 5}
    app = run_server(root, run_limits=RunLimits(min_free_bytes=1 << 60))
    async with McpClient(app.server) as c:
        res = await c.call_tool("qt_local_sample_shadows", {"spec": spec})
    assert res.is_error and "bytes free under the run root" in res.structured_content["message"]
    app = run_server(root, run_limits=RunLimits(min_free_bytes=0, max_output_bytes=2000))
    async with McpClient(app.server) as c:
        res = await c.call_tool("qt_local_sample_shadows", {"spec": spec})
    assert res.is_error and res.structured_content["code"] == "QT-ENGINE-001"
    runs = root / "qt-mcp-runs"
    assert list((runs / "shadows").iterdir()) == [] and list((runs / ".staging").iterdir()) == []  # no run dir


def test_runs_wait_for_a_slot(root: Path) -> None:
    """`max_concurrent = 1`: two concurrent runs execute one after the other."""
    runner = LocalRunner(run_server(root).runner.sandbox, RunLimits(min_free_bytes=0, max_concurrent=1))
    spans: list[tuple[float, float]] = []
    real = runner.spawn

    async def timed(*a: Any, **k: Any) -> Any:
        t0 = time.monotonic()
        try:
            return await real(*a, **k)
        finally:
            spans.append((t0, time.monotonic()))

    runner.spawn = timed  # type: ignore[method-assign]

    async def go() -> None:
        async with anyio.create_task_group() as tg:
            for seed in (1, 2):
                tg.start_soon(runner.run, "shadows", {"dataset": {"path": str(FIXTURE)}, "n": 5, "seed": seed})

    anyio.run(go)
    (_, a1), (b0, _) = sorted(spans)
    assert a1 <= b0, spans
    assert len(list((root / "qt-mcp-runs" / "shadows").iterdir())) == 2


@pytest.mark.asyncio
async def test_stdio_run_mode(root: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("QT_API_KEY", "QT_API_URL")}
    env |= {
        "QT_MCP_ROOT": str(root),
        "QT_MCP_DATA_ROOTS": str(FIXTURE),
        "QT_MCP_RUN_MIN_FREE_BYTES": "0",
        "QT_MCP_OPTIONAL_TOOLS": "0",
        "PYTHONPATH": os.pathsep.join([str(REPO / "python"), env.get("PYTHONPATH", "")]),
    }
    for args, extra in ((["--local", "--allow-run"], {}), (["--local"], {"QT_MCP_ALLOW_RUN": "1"})):
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "queuetruth.mcp.server", *args], env=env | extra, cwd=str(root)
        )
        async with McpClient(params) as c:
            names = [t.name for t in (await c.list_tools()).tools]
            res = await c.call_tool("qt_local_sample_shadows", {"spec": {"dataset": {"path": "../../" + "x"}, "n": 1}})
            assert res.is_error and res.structured_content["code"] == "QT-CONFIG-001"
            res = await c.call_tool("qt_local_sample_shadows", {"spec": {"dataset": {"path": str(FIXTURE)}, "n": 4}})
            listing = await c.call_tool("qt_local_list_outputs", {"path": "qt-mcp-runs"})
        assert names == [*LOCAL, *RUN_TOOL_NAMES]
        assert not res.is_error and res.structured_content["n"] == 4
        assert [e["schema"] for e in listing.structured_content["entries"]] == ["qt.shadow_sample_summary"]


@pytest.mark.asyncio
async def test_link_cap_note(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qt_server, "MAX_LINKS", 1)
    async with McpClient(run_server(root).server) as c:
        res = await c.call_tool("qt_local_sample_shadows", {"spec": {"dataset": {"path": str(FIXTURE)}, "n": 3}})
    assert not res.is_error, res.content
    assert len(links_of(res)) == 1 and links_of(res)[0].name == "shadows.parquet"  # sorted: the first file
    assert note_of(res).endswith(
        "(new run; 2 file(s)); 1 linked, list the rest with qt_local_list_outputs or the file system"
    )


def test_an_invalid_finished_directory_is_replaced(root: Path) -> None:
    """A truncated `summary.json` (or a stray file) at `<run root>/<kind>/<run_id>` does not block that run_id."""
    runner = run_server(root).runner
    assert runner is not None
    spec = {"dataset": {"path": str(FIXTURE)}, "n": 4, "seed": 5}
    first = anyio.run(runner.run, "shadows", spec)
    assert not first.cached
    for damage in ("truncate", "file", "link"):
        dest = first.out
        if damage == "truncate":
            (dest / "summary.json").write_text("{truncated")
        elif damage == "file":
            import shutil

            shutil.rmtree(dest)
            dest.write_text("not a run")
        else:
            (dest / "summary.json").unlink()
            (dest / "summary.json").symlink_to(root / "elsewhere.json")
        assert runner.cached("shadows", dest.name) is None, damage
        again = anyio.run(runner.run, "shadows", spec)
        assert not again.cached and again.out == dest and again.summary == first.summary, damage
        assert runner.cached("shadows", dest.name) == first.summary
    assert list((root / "qt-mcp-runs" / ".staging").iterdir()) == []


def test_identical_specs_started_together_run_once(root: Path) -> None:
    runner = LocalRunner(run_server(root).runner.sandbox, RunLimits(min_free_bytes=0, max_concurrent=2))
    spawned: list[str] = []
    real = runner.spawn

    async def counted(*a: Any, **k: Any) -> Any:
        spawned.append(a[0])
        return await real(*a, **k)

    runner.spawn = counted  # type: ignore[method-assign]
    results: list[Any] = []

    async def go() -> None:
        async def one() -> None:
            results.append(await runner.run("shadows", {"dataset": {"path": str(FIXTURE)}, "n": 5, "seed": 9}))

        async with anyio.create_task_group() as tg:
            tg.start_soon(one)
            tg.start_soon(one)

    anyio.run(go)
    assert len(spawned) == 1 and sorted(r.cached for r in results) == [False, True]
    assert results[0].summary == results[1].summary and not runner._run_locks
