"""MCP stdio server (SPEC 4.6) through the `mcp` SDK client: O-MCP-1 against the in-process fake HTTP API."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import jsonschema
import mcp.types as types
import pytest
from mcp import Client as McpClient
from mcp import StdioServerParameters
from mcp.shared.exceptions import MCPError

from queuetruth.client.testing import FakeService
from queuetruth.mcp.metrics import METRICS
from queuetruth.mcp.server import build_server
from queuetruth.mcp.tools import TOOL_NAMES, TOOLS
from queuetruth.schemas.models import parse_document

REPO = Path(__file__).resolve().parents[3]
SCHEMAS = REPO / "schemas" / "json"
SUBMIT = {t.name for t in TOOLS if t.submit}
LOCAL = [t.name for t in TOOLS if t.local]
REMOTE_READ = [t.name for t in TOOLS if not t.local and not t.submit]


def sample(name: str) -> dict[str, Any]:
    return json.loads((REPO / "schemas" / "samples" / f"{name}.json").read_text())["sample"]


def committed_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text())


def validate(doc: dict[str, Any], name: str) -> None:
    jsonschema.Draft202012Validator(committed_schema(name)).validate(doc)
    parse_document(doc)


def tree_state(root: Path) -> dict[str, tuple[int, int, str]]:
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else ""
        out[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns, digest)
    return out


@pytest.fixture
def outputs(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "replay_out" / "l2").mkdir(parents=True)
    (root / "replay_out" / "summary.json").write_text(json.dumps(sample("qt.replay_summary"), indent=2))
    (root / "replay_out" / "data_quality.json").write_text(json.dumps(sample("qt.data_quality")))
    (root / "replay_out" / "trades.parquet").write_bytes(b"PAR1....PAR1")
    (root / "replay_out" / "notes.json").write_text('{"hello": 1}')
    (root / "calib").mkdir()
    (root / "calib" / "calibration.json").write_text(json.dumps(sample("qt.calibration_result")))
    (root / "calib" / "fit.json").write_text(json.dumps(sample("qt.calibration_result")))
    (root / "two").mkdir()
    (root / "two" / "a.json").write_text(json.dumps(sample("qt.zoo_summary")))
    (root / "two" / "b.json").write_text(json.dumps(sample("qt.feature_summary")))
    (root / "bad.json").write_text(json.dumps({**sample("qt.replay_summary"), "run_id": "nothex"}))
    (root / "dataset").mkdir()
    (root / "shadows.parquet").write_bytes(b"PAR1")
    (tmp_path / "outside.json").write_text(json.dumps(sample("qt.replay_summary")))
    os.symlink(tmp_path / "outside.json", root / "escape.json")
    return root


@pytest.fixture
def fake() -> FakeService:
    return FakeService()


def seeded(fake: FakeService) -> dict[str, str]:
    """A dataset, finished replay/calibration/scorecard runs with summaries and files, and a running replay."""
    c = fake.client()
    up = c.create_upload("market_data", "d.itch", 3, hashlib.sha256(b"abc").hexdigest())
    ds = c.create_dataset("day1", "mbo", "itch", "nasdaq", [up.id]).id
    ids = {"ds": ds}
    for kind, spec, summary in (
        ("replay", {"dataset": {"dataset_id": ds}}, "qt.replay_summary"),
        (
            "calibration",
            {"kind": "alloc", "family": "power3", "dataset": {"dataset_id": ds}, "train": {}, "test": {}},
            "qt.calibration_result",
        ),
        (
            "scorecard",
            {"live": {"upload_id": up.id}, "sim": {"kind": "exact"}, "dataset": {"dataset_id": ds}},
            "qt.scorecard_summary",
        ),
    ):
        sub = c.submit(kind, spec)
        fake.finish_job(sub.job.id, summary=sample(summary), files={"summary.json": b"{}", "trades.parquet": b"PAR1"})
        ids[kind], ids[f"{kind}_job"] = sub.id, sub.job.id
    running = c.submit("replay", {"dataset": {"dataset_id": ds}})
    fake.advance(running.job.id)
    ids["running"], ids["running_job"] = running.id, running.job.id
    return ids


async def list_names(app: Any, mode: str = "auto") -> list[str]:
    async with McpClient(app.server, mode=mode) as c:
        return [t.name for t in (await c.list_tools()).tools]


# ---- O-MCP-1: tool list, order, schemas -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_O_MCP_1_tool_order_and_schemas(fake: FakeService, outputs: Path, mode: str) -> None:
    app = build_server(client=fake.client(), allow_submit=True, root=outputs)
    async with McpClient(app.server, mode=mode) as c:
        tools = (await c.list_tools()).tools
    assert [t.name for t in tools] == list(TOOL_NAMES)
    assert TOOL_NAMES[:10] == (
        "qt_meta",
        "qt_list_datasets",
        "qt_describe_dataset",
        "qt_estimate",
        "qt_get_job",
        "qt_list_job_files",
        "qt_get_replay",
        "qt_get_calibration",
        "qt_get_scorecard",
        "qt_explain_metric",
    )
    assert TOOL_NAMES[10:] == (
        "qt_submit_replay",
        "qt_submit_calibration",
        "qt_submit_scorecard",
        "qt_submit_report",
        "qt_local_read_summary",
        "qt_local_list_outputs",
        "qt_local_why",
    )
    by_def = {t.name: t for t in TOOLS}
    for tool in tools:
        assert tool.input_schema["type"] == "object" and tool.output_schema is not None
        jsonschema.Draft202012Validator.check_schema(tool.input_schema)
        jsonschema.Draft202012Validator.check_schema(tool.output_schema)
        assert tool.output_schema["type"] == "object"
        out = by_def[tool.name].output
        if out.startswith("qt."):
            assert tool.output_schema == committed_schema(out), tool.name
        assert tool.annotations is not None and tool.annotations.destructive_hint is False
        assert tool.annotations.read_only_hint is (tool.name not in SUBMIT)
    spec_defs = {"qt_submit_replay": "ReplaySpec", "qt_submit_calibration": "CalibrationSpec"}
    schemas = {t.name: t.input_schema for t in tools}
    for name, model in spec_defs.items():
        assert schemas[name]["properties"]["spec"] == {"$ref": f"#/$defs/{model}"}
    est = schemas["qt_estimate"]["properties"]["spec"]["anyOf"]
    assert [x["$ref"] for x in est] == ["#/$defs/ReplaySpec", "#/$defs/CalibrationSpec", "#/$defs/ScorecardSpec"]


@pytest.mark.asyncio
async def test_O_MCP_1_submit_tools_absent_without_allow_submit(fake: FakeService, outputs: Path) -> None:
    names = await list_names(build_server(client=fake.client(), root=outputs))
    assert names == [n for n in TOOL_NAMES if n not in SUBMIT]
    async with McpClient(build_server(client=fake.client(), root=outputs).server) as c:
        with pytest.raises(MCPError):
            await c.call_tool("qt_submit_replay", {"spec": {}})
    assert fake.calls("POST") == []


@pytest.mark.asyncio
async def test_O_MCP_1_local_mode_lists_only_local_tools(
    fake: FakeService, outputs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await list_names(build_server(local=True, allow_submit=True, client=fake.client(), root=outputs)) == LOCAL
    # SPEC 4.6: qt_explain_metric is not marked L, so local mode does not list it
    assert LOCAL == ["qt_local_read_summary", "qt_local_list_outputs", "qt_local_why"]
    monkeypatch.delenv("QT_API_KEY", raising=False)
    keyless = build_server(allow_submit=True, root=outputs)
    assert keyless.local_mode and keyless.client is None
    assert await list_names(keyless) == LOCAL
    async with McpClient(keyless.server) as c:
        with pytest.raises(MCPError):
            await c.call_tool("qt_meta", {})


@pytest.mark.asyncio
async def test_instructions_name_only_listed_tools(fake: FakeService, outputs: Path) -> None:
    """SPEC 4.6 (open conflict 10): the server instructions of each mode point only at tools that mode lists; local
    mode does not list qt_explain_metric, so its instructions do not send the client there."""
    for app in (
        build_server(local=True, root=outputs),
        build_server(allow_submit=True, client=fake.client(), root=outputs),
    ):
        listed = set(await list_names(app))
        async with McpClient(app.server) as c:
            text = c.instructions or ""
        named = {t.name for t in TOOLS if t.name in text}
        if app.local_mode:
            assert "qt_explain_metric" not in listed
            assert set(LOCAL) <= named and named - set(LOCAL) == {"qt_explain_metric"}
            assert "need remote mode" in text  # named only to say it is not available here
        else:
            assert named <= listed and "qt_explain_metric" in named


@pytest.mark.asyncio
async def test_remote_mode_from_env_key(fake: FakeService, monkeypatch: pytest.MonkeyPatch, outputs: Path) -> None:
    monkeypatch.setenv("QT_API_KEY", "qt_test_x")
    monkeypatch.setenv("QT_API_URL", "http://qt.test")
    app = build_server(root=outputs)
    assert not app.local_mode and app.client is not None and app.client.base_url == "http://qt.test"
    app.client.close()


# ---- O-MCP-1: results validate against the schemas ------------------------------------------------------------


@pytest.mark.asyncio
async def test_O_MCP_1_remote_tool_results_validate(fake: FakeService, outputs: Path) -> None:
    ids = seeded(fake)
    app = build_server(client=fake.client(), allow_submit=True, root=outputs)
    async with McpClient(app.server) as c:

        async def ok(name: str, args: dict[str, Any], schema: str) -> types.CallToolResult:
            res = await c.call_tool(name, args)
            assert not res.is_error, res.content
            validate(res.structured_content, schema)
            assert isinstance(res.content[0], types.TextContent) and res.content[0].text
            return res

        await ok("qt_meta", {}, "qt.meta")
        page = await ok("qt_list_datasets", {"limit": 10}, "qt.page")
        assert [d["id"] for d in page.structured_content["data"]] == [ids["ds"]]
        await ok("qt_describe_dataset", {"dataset_id": ids["ds"]}, "qt.dataset")
        est = await ok("qt_estimate", {"kind": "replay", "spec": {"dataset": {"dataset_id": ids["ds"]}}}, "qt.estimate")
        assert est.structured_content["kind"] == "replay"
        await ok("qt_get_job", {"job_id": ids["replay_job"]}, "qt.job")
        files = await ok("qt_list_job_files", {"job_id": ids["replay_job"]}, "qt.file_manifest")
        links = [b for b in files.content if isinstance(b, types.ResourceLink)]
        assert [link.name for link in links] == ["summary.json", "trades.parquet"]
        assert all(link.uri.startswith("http://qt.test/v1/_store/") and link.size is not None for link in links)
        assert all("PAR1" not in b.text for b in files.content if isinstance(b, types.TextContent))
        await ok("qt_get_replay", {"id": ids["replay"]}, "qt.replay_summary")
        await ok("qt_get_calibration", {"id": ids["calibration"]}, "qt.calibration_result")
        sc = await ok("qt_get_scorecard", {"id": ids["scorecard"]}, "qt.scorecard_summary")
        assert sc.structured_content == sample("qt.scorecard_summary")
        for kind, spec in (
            ("replay", {"dataset": {"dataset_id": ids["ds"]}}),
            (
                "calibration",
                {"kind": "alloc", "family": "power", "dataset": {"dataset_id": ids["ds"]}, "train": {}, "test": {}},
            ),
            (
                "scorecard",
                {"live": {"upload_id": "up_1"}, "sim": {"kind": "exact"}, "dataset": {"dataset_id": ids["ds"]}},
            ),
            ("report", {"inputs": [ids["replay_job"]]}),
        ):
            res = await ok(f"qt_submit_{kind}", {"spec": spec, "idempotency_key": f"mcp-{kind}"}, "qt.job")
            assert res.structured_content["state"] == "queued" and res.structured_content["kind"] == kind
        assert [r.headers["Idempotency-Key"] for r in fake.calls("POST", "/v1/") if "estimate" not in r.url.path][
            -4:
        ] == [
            "mcp-replay",
            "mcp-calibration",
            "mcp-scorecard",
            "mcp-report",
        ]
        again = await ok(
            "qt_submit_replay",
            {"spec": {"dataset": {"dataset_id": ids["ds"]}}, "idempotency_key": "mcp-replay"},
            "qt.job",
        )
        assert again.structured_content["id"] == fake.runs["replay"][list(fake.runs["replay"])[-1]]["job_id"]


@pytest.mark.asyncio
async def test_O_MCP_1_explain_metric(outputs: Path) -> None:
    app = build_server(client=FakeService().client(), root=outputs)
    tool = next(t for t in (await _list(app)) if t.name == "qt_explain_metric")
    validator = jsonschema.Draft202012Validator(tool.output_schema)
    async with McpClient(app.server) as c:
        for name in METRICS:
            res = await c.call_tool("qt_explain_metric", {"name": name})
            assert not res.is_error
            validator.validate(res.structured_content)
            assert set(res.structured_content) == {"name", "definition", "formula", "proper", "notes"}
        brier = (await c.call_tool("qt_explain_metric", {"name": "brier"})).structured_content
        assert brier["proper"] is True
        assert (await c.call_tool("qt_explain_metric", {"name": "c_index_uno"})).structured_content["proper"] is False
        bad = await c.call_tool("qt_explain_metric", {"name": "sharpe"})
        assert bad.is_error and bad.structured_content["code"] == "QT-USAGE-001"


async def _list(app: Any) -> list[types.Tool]:
    async with McpClient(app.server) as c:
        return (await c.list_tools()).tools


@pytest.mark.asyncio
async def test_errors_are_tool_results_with_qt_error(fake: FakeService, outputs: Path) -> None:
    ids = seeded(fake)
    app = build_server(client=fake.client(), root=outputs)
    async with McpClient(app.server) as c:
        missing = await c.call_tool("qt_get_job", {"job_id": "job_" + "9" * 26})
        assert missing.is_error
        validate(missing.structured_content, "qt.error")
        assert missing.structured_content["code"] == "QT-REMOTE-001"
        assert missing.structured_content["details"]["problem"]["status"] == 404
        running = await c.call_tool("qt_get_replay", {"id": ids["running"]})
        assert running.is_error
        assert running.structured_content["details"] == {"job_id": ids["running_job"], "state": "running"}
        assert "qt_get_job" in running.structured_content["hint"]
        bad = await c.call_tool("qt_get_job", {"job_id": "../keys"})
        assert bad.is_error and bad.structured_content["code"] == "QT-USAGE-001"
        bad_spec = await c.call_tool(
            "qt_estimate", {"kind": "replay", "spec": {"dataset": {"dataset_id": "x"}, "out": "/x"}}
        )
        assert bad_spec.is_error and bad_spec.structured_content["code"] == "QT-CONFIG-001"
        extra = await c.call_tool("qt_meta", {"unexpected": 1})
        assert extra.is_error
        with pytest.raises(MCPError):
            await c.call_tool("qt_delete_dataset", {"dataset_id": ids["ds"]})
    assert not any(r.method == "DELETE" for r in fake.requests)


# ---- local tools ----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_tools_read_and_never_write(outputs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # hermetic: no inherited mode/root settings, cwd outside the watched tree (only `outputs` is compared)
    for var in ("QT_MCP_ROOT", "QT_API_KEY", "QT_API_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(outputs.parent)
    calls: list[dict[str, Any]] = []

    def fake_why(spec_json: str) -> str:
        spec = json.loads(spec_json)
        calls.append(spec)
        return json.dumps(sample("qt.why_trace"))

    before = tree_state(outputs)
    app = build_server(local=True, root=outputs, why=fake_why)
    async with McpClient(app.server) as c:
        listing = await c.call_tool("qt_local_list_outputs", {"path": "."})
        assert not listing.is_error
        entries = listing.structured_content["entries"]
        assert entries == [
            {"path": "bad.json", "schema": "qt.replay_summary"},
            {"path": "calib/calibration.json", "schema": "qt.calibration_result"},
            {"path": "calib/fit.json", "schema": "qt.calibration_result"},
            {"path": "replay_out/data_quality.json", "schema": "qt.data_quality"},
            {"path": "replay_out/summary.json", "schema": "qt.replay_summary"},
            {"path": "two/a.json", "schema": "qt.zoo_summary"},
            {"path": "two/b.json", "schema": "qt.feature_summary"},
        ]
        by_dir = await c.call_tool("qt_local_read_summary", {"path": "replay_out"})
        assert not by_dir.is_error and by_dir.structured_content == sample("qt.replay_summary")
        validate(by_dir.structured_content, "qt.replay_summary")
        calib = await c.call_tool("qt_local_read_summary", {"path": str(outputs / "calib")})
        assert calib.structured_content["schema"] == "qt.calibration_result"
        one = await c.call_tool("qt_local_read_summary", {"path": "two/b.json"})
        assert one.structured_content["schema"] == "qt.feature_summary"
        ambiguous = await c.call_tool("qt_local_read_summary", {"path": "two"})
        assert ambiguous.is_error and ambiguous.structured_content["code"] == "QT-INPUT-001"
        invalid = await c.call_tool("qt_local_read_summary", {"path": "bad.json"})
        assert invalid.is_error and invalid.structured_content["code"] == "QT-INPUT-002"
        not_qt = await c.call_tool("qt_local_read_summary", {"path": "replay_out/notes.json"})
        assert not_qt.is_error
        for escape in ("../outside.json", "escape.json", "/etc/hosts"):
            res = await c.call_tool("qt_local_read_summary", {"path": escape})
            assert res.is_error and res.structured_content["code"] == "QT-CONFIG-001", escape
        why = await c.call_tool(
            "qt_local_why", {"dataset_path": "dataset", "shadows_path": "shadows.parquet", "shadow_id": "s1"}
        )
        assert not why.is_error
        validate(why.structured_content, "qt.why_trace")
        from queuetruth.schemas import specs as sp

        expected = sp.WhySpec(
            dataset=sp.PathRef(path=str(outputs / "dataset")),
            shadows=sp.PathRef(path=str(outputs / "shadows.parquet")),
            shadow_id="s1",
        ).model_dump(mode="json")  # spec defaults (e.g. 1.1 exclude_order_ids) follow the model
        assert calls == [expected] and expected["out"] is None and expected["max_events"] == 5000
        escape_why = await c.call_tool(
            "qt_local_why", {"dataset_path": "..", "shadows_path": "shadows.parquet", "shadow_id": "s1"}
        )
        assert escape_why.is_error and len(calls) == 1
    assert tree_state(outputs) == before


@pytest.mark.asyncio
async def test_local_why_maps_core_errors(outputs: Path) -> None:
    from queuetruth.errors import QtError

    def failing(spec_json: str) -> str:
        raise QtError("QT-INPUT-002", "shadow s9 not found")

    app = build_server(local=True, root=outputs, why=failing)
    async with McpClient(app.server) as c:
        res = await c.call_tool(
            "qt_local_why", {"dataset_path": "dataset", "shadows_path": "shadows.parquet", "shadow_id": "s9"}
        )
    assert res.is_error and res.structured_content["code"] == "QT-INPUT-002"
    assert res.content[0].text == "QT-INPUT-002: shadow s9 not found"


@pytest.mark.asyncio
async def test_stdio_server_local_mode(outputs: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("QT_API_KEY", "QT_API_URL")}
    env["QT_MCP_ROOT"] = str(outputs)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "python"), env.get("PYTHONPATH", "")])
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "queuetruth.mcp.server", "--allow-submit"], env=env, cwd=str(outputs)
    )
    async with McpClient(params) as c:
        names = [t.name for t in (await c.list_tools()).tools]
        res = await c.call_tool("qt_local_read_summary", {"path": "replay_out/summary.json"})
    assert names == LOCAL
    assert not res.is_error and res.structured_content["schema"] == "qt.replay_summary"


# ---- gate test: real `_core.why` on the committed synthetic fixture -------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_core
@pytest.mark.asyncio
async def test_local_why_on_synth_small_gate(tmp_path: Path) -> None:
    import pyarrow as pa

    from queuetruth.schemas.arrow import SCHEMAS
    from queuetruth.schemas.io import write_table

    dataset = REPO / "tests" / "fixtures" / "synth_small"
    manifest = json.loads((dataset / "manifest.json").read_text())
    part = manifest["partitions"][0]
    shadows = tmp_path / "shadows.parquet"
    row = {
        "shadow_id": "gate1",
        "instrument_id": part["instrument_id"],
        "side": ord("B"),
        "price_mode": "touch",
        "price": None,
        "offset_ticks": 0,
        "qty": 1,
        "ts_submit": part["ts_min"] + 1_000_000_000,
        "latency_ns": 0,
        "ts_cancel": None,
        "group": None,
        "source": "user",
        "order_key": None,
    }
    write_table(pa.Table.from_pylist([row]), shadows, SCHEMAS["qt.shadow_spec"])
    before = tree_state(dataset)
    app = build_server(local=True, root="/")
    async with McpClient(app.server) as c:
        res = await c.call_tool(
            "qt_local_why", {"dataset_path": str(dataset), "shadows_path": str(shadows), "shadow_id": "gate1"}
        )
    assert not res.is_error, res.content
    validate(res.structured_content, "qt.why_trace")
    assert res.structured_content["subject"]["shadow_id"] == "gate1"
    assert tree_state(dataset) == before
