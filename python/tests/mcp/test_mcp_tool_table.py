"""The MCP tools come from the operation table: every tool listed before keeps its name, arguments, schemas and hints
in every mode (`golden/tools_before.json`, written from main before the change by `snapshot` below); the run tools
are exactly the table's operations with a tool, each with the run-mode form of its spec model as input and its
document model as output; the verbs run like the other run tools and answer first; a spec that needs the network is
refused unless the server allows it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import mcp.types as types
import pytest
from mcp import Client as McpClient

from queuetruth.mcp.local import RunLimits, run_operations
from queuetruth.mcp.server import (
    OPTIONAL_TOOL_MODULES,
    RUN_TOOL_NAMES,
    build_server,
    local_spec_model,
    run_operations_in_order,
)
from queuetruth.ops.table import table
from queuetruth.runner import RunContext, run
from queuetruth.schemas.export import DRAFT, json_schema

REPO = Path(__file__).resolve().parents[3]
GOLDEN = Path(__file__).parent / "golden" / "tools_before.json"
DATA = REPO / "tests" / "fixtures" / "synth_small"
ATLAS = REPO / "tests" / "fixtures" / "atlas_synth"
VERB_TOOLS = ["qt_check", "qt_fit", "qt_compare", "qt_data", "qt_atlas", "qt_demo"]
MODES: dict[str, dict[str, Any]] = {
    "local": {"local": True},
    "local+run": {"local": True, "allow_run": True},
    "local+run+sandbox": {"local": True, "allow_run": True, "tool_modules": OPTIONAL_TOOL_MODULES},
    "remote": {"client": object()},
    "remote+submit": {"client": object(), "allow_submit": True},
    "remote+submit+run": {"client": object(), "allow_submit": True, "allow_run": True},
}


def digest(x: Any) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot(root: Path) -> dict[str, list[dict[str, Any]]]:
    """Every mode's tools: name, arguments, input schema digest (top-level title left out: a generated argument
    model may be named differently), output schema digest, read-only and idempotent hints."""
    out = {}
    for mode, kw in MODES.items():
        app = build_server(root=root, run_limits=RunLimits(min_free_bytes=0), **kw)
        out[mode] = [
            {
                "name": t.name,
                "arguments": sorted(t.input_schema.get("properties", {})),
                "required": sorted(t.input_schema.get("required", [])),
                "input_sha256": digest({k: v for k, v in t.input_schema.items() if k != "title"}),
                "output_sha256": digest(t.output_schema),
                "read_only": t.annotations.read_only_hint,
                "idempotent": t.annotations.idempotent_hint,
            }
            for t in app.tool_list()
        ]
    return out


def test_every_tool_listed_before_is_unchanged(tmp_path: Path) -> None:
    before = json.loads(GOLDEN.read_text())
    after = snapshot(tmp_path)
    assert set(after) == set(before)
    for mode, tools in before.items():
        now = {t["name"]: t for t in after[mode]}
        for t in tools:
            assert now.get(t["name"]) == t, (mode, t["name"])
        names = [t["name"] for t in tools]
        assert [n for n in now if n in names] == names, mode  # the same order
        new = [n for n in now if n not in names]
        assert new == (VERB_TOOLS if "run" in mode else []), mode


def test_run_tools_are_the_table(tmp_path: Path) -> None:
    ops = [op for op in table().values() if op.tool is not None]
    assert sorted(RUN_TOOL_NAMES) == sorted(str(op.tool) for op in ops)
    assert [op.tool for op in run_operations_in_order()] == list(RUN_TOOL_NAMES)
    assert set(run_operations()) == {op.name for op in ops}
    app = build_server(local=True, allow_run=True, root=tmp_path, run_limits=RunLimits(min_free_bytes=0))
    listed = {t.name: t for t in app.tool_list()}
    for op in ops:
        t = listed[str(op.tool)]
        assert t.title == op.title and t.description is not None and t.description.startswith(op.description)
        # input: {"spec": <the operation's spec model in its run-mode form>}
        local = local_spec_model(op.spec)
        body = local.model_json_schema()
        assert t.input_schema["$schema"] == DRAFT and t.input_schema["required"] == ["spec"]
        assert t.input_schema["properties"]["spec"] == {"$ref": f"#/$defs/{local.__name__}"}
        assert t.input_schema["$defs"][local.__name__] == {k: v for k, v in body.items() if k != "$defs"}
        for name, d in body.get("$defs", {}).items():
            assert t.input_schema["$defs"][name] == d
        assert set(body["properties"]) == set(op.spec.model_fields) - {"out"}
        # output: the operation's document model
        name = getattr(op.document, "SCHEMA", None) or f"qt.{op.name}"
        assert t.output_schema == json_schema(name, op.document)
        assert t.annotations is not None and t.annotations.read_only_hint is False
        assert t.annotations.open_world_hint is False  # the network is off by default
    on = build_server(
        local=True, allow_run=True, root=tmp_path, run_limits=RunLimits(min_free_bytes=0), allow_network=True
    )
    hints = {t.name: t.annotations.open_world_hint for t in on.tool_list() if t.annotations is not None}
    assert {n for n, h in hints.items() if h} == {str(op.tool) for op in ops if op.network}


def answer_line(res: types.CallToolResult) -> str:
    first = res.content[0]
    assert isinstance(first, types.TextContent)
    return first.text.split("\n", 1)[0]


@pytest.mark.asyncio
async def test_network_is_refused_unless_allowed(tmp_path: Path) -> None:
    app = build_server(local=True, allow_run=True, root=tmp_path, run_limits=RunLimits(min_free_bytes=0))
    async with McpClient(app.server) as c:
        for tool, spec in (("qt_atlas", {"library": "nautilus"}), ("qt_demo", {})):
            res = await c.call_tool(tool, {"spec": spec})
            assert res.is_error and res.structured_content is not None
            assert res.structured_content["code"] == "QT-CONFIG-001"
            assert "QT_MCP_ALLOW_NETWORK" in (res.structured_content.get("hint") or "")
        res = await c.call_tool("qt_atlas", {"spec": {"source": {"path": "/etc"}}})
        assert res.is_error and "outside the allowed roots" in res.structured_content["message"]
        res = await c.call_tool("qt_check", {"spec": {"backtest": [{"path": "x.csv"}], "out": "o"}})
        assert res.is_error and "spec.out must be null" in res.structured_content["message"]
    assert not (tmp_path / "qt-mcp-runs" / "atlas").exists() or not any((tmp_path / "qt-mcp-runs" / "atlas").iterdir())


@pytest.mark.asyncio
async def test_atlas_tool_answers_first_from_a_local_copy(tmp_path: Path) -> None:
    if not (ATLAS / "manifest.json").exists():
        pytest.skip("tests/fixtures/atlas_synth is missing")
    app = build_server(
        local=True, allow_run=True, root=tmp_path, data_roots=[str(ATLAS)], run_limits=RunLimits(min_free_bytes=0)
    )
    spec = {"library": "nautilus", "source": {"path": str(ATLAS)}}
    async with McpClient(app.server) as c:
        res = await c.call_tool("qt_atlas", {"spec": spec})
    assert not res.is_error, res.content
    direct = run("atlas", spec, context=RunContext(roots=[ATLAS]))
    assert res.structured_content == direct.document
    assert answer_line(res) == direct.summary
    assert res.meta is not None and res.meta["queuetruth/answer"] == direct.summary


@pytest.mark.requires_core
@pytest.mark.asyncio
async def test_check_tool_is_the_python_api(tmp_path: Path) -> None:
    if not (DATA / "manifest.json").exists():
        pytest.skip("tests/fixtures/synth_small is missing (make fixtures)")
    from check import synth_backtests as sb

    import queuetruth as qt

    root = tmp_path / "root"
    root.mkdir()
    backtest = sb.write_nautilus(root / "orders_report.csv", sb.orders(DATA))
    spec = {"backtest": [{"path": "orders_report.csv"}], "data": {"path": str(DATA)}, "window": "10s"}
    app = build_server(
        local=True, allow_run=True, root=root, data_roots=[str(DATA)], run_limits=RunLimits(min_free_bytes=0)
    )
    async with McpClient(app.server) as c:
        names = [t.name for t in (await c.list_tools()).tools]
        res = await c.call_tool("qt_check", {"spec": spec})
    assert "qt_check" in names
    assert not res.is_error, res.content
    doc = res.structured_content
    assert doc is not None and doc["schema"] == "qt.check"
    direct = qt.check(str(backtest), data=str(DATA), window="10s", out=str(tmp_path / "direct"))
    assert doc["answer"] == direct.summary == answer_line(res)
    # the same document, but for where each run wrote (its output paths, and the `why` commands that name them)
    run_local = ("out", "files", "instrument_map", "examples")
    assert {k: v for k, v in doc.items() if k not in run_local} == {
        k: v for k, v in direct.doc.items() if k not in run_local
    }
    assert res.meta is not None
    out = Path(res.meta["queuetruth/out_dir"])
    assert out.parent == root / "qt-mcp-runs" / "check" and (out / "summary.json").is_file()
    assert json.loads((out / "summary.json").read_text()) == doc
