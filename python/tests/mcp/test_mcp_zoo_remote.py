"""Zoo runs on the service through every supported client: `Client.estimate/submit/get_run/list_runs("zoo")`,
`client.jobs.submit("zoo", ...)` (what `queuetruth jobs submit zoo` calls) and the remote MCP tools `qt_estimate`
(kind zoo), `qt_submit_zoo` (only with allow_submit) and `qt_get_zoo`, against the in-process fake HTTP API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from mcp import Client as McpClient

from queuetruth.client import ESTIMATE_KINDS, RUN_KINDS
from queuetruth.client.testing import FakeService
from queuetruth.errors import QtError
from queuetruth.mcp.server import build_server
from queuetruth.schemas.models import ZooSummary

REPO = Path(__file__).resolve().parents[3]


def sample(name: str) -> dict[str, Any]:
    return json.loads((REPO / "schemas" / "samples" / f"{name}.json").read_text())["sample"]


def committed_schema(name: str) -> dict[str, Any]:
    return json.loads((REPO / "schemas" / "json" / f"{name}.schema.json").read_text())


def zoo_spec(fake: FakeService) -> dict[str, Any]:
    c = fake.client()
    data = c.create_upload("market_data", "d.itch", 3, hashlib.sha256(b"abc").hexdigest())
    ds = c.create_dataset("day1", "mbo", "itch", "nasdaq", [data.id]).id
    shadows = c.create_upload("shadow_spec", "s.parquet", 4, hashlib.sha256(b"PAR1").hexdigest())
    return {"dataset": {"dataset_id": ds}, "shadows": {"upload_id": shadows.id}, "models": ["all"]}


def test_client_knows_the_zoo_kind():
    assert RUN_KINDS["zoo"][0] == "zoos" and "zoo" in ESTIMATE_KINDS
    fake = FakeService()
    c = fake.client()
    spec = zoo_spec(fake)
    assert c.estimate("zoo", spec).kind == "zoo"
    sub = c.submit("zoo", spec, idempotency_key="zoo-1")
    assert sub.id.startswith("zoo_") and sub.job.kind == "zoo"
    assert c.get_run("zoo", sub.id).summary is None  # queued
    fake.finish_job(sub.job.id, summary=sample("qt.zoo_summary"))
    record = c.get_run("zoo", sub.id)
    assert isinstance(record.summary, ZooSummary) and record.job_id == sub.job.id
    assert [r["id"] for r in c.list_runs("zoo").data] == [sub.id]
    job = c.jobs.submit("zoo", spec)  # `queuetruth jobs submit zoo --spec ...`
    assert job.kind == "zoo" and fake.calls("POST", "/v1/zoos")
    with pytest.raises(QtError) as e:
        c.submit("zoo", {"dataset": {"dataset_id": "ds_x"}})  # ZooSpec needs shadows: refused before any request
    assert e.value.code == "QT-CONFIG-001" and "shadows" in e.value.message


@pytest.mark.asyncio
async def test_remote_mcp_zoo_tools(tmp_path: Path) -> None:
    fake = FakeService()
    spec = zoo_spec(fake)
    app = build_server(client=fake.client(), allow_submit=True, root=tmp_path)
    async with McpClient(app.server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert tools["qt_get_zoo"].output_schema == committed_schema("qt.zoo_summary")
        assert tools["qt_get_zoo"].annotations.read_only_hint is True
        assert tools["qt_submit_zoo"].annotations.read_only_hint is False
        assert tools["qt_submit_zoo"].input_schema["properties"]["spec"] == {"$ref": "#/$defs/ZooSpec"}
        est = await c.call_tool("qt_estimate", {"kind": "zoo", "spec": spec})
        assert not est.is_error and est.structured_content["kind"] == "zoo"
        sub = await c.call_tool("qt_submit_zoo", {"spec": spec, "idempotency_key": "mcp-zoo"})
        assert not sub.is_error and sub.structured_content["kind"] == "zoo"
        jsonschema.Draft202012Validator(committed_schema("qt.job")).validate(sub.structured_content)
        job_id = sub.structured_content["id"]
        zoo_id = next(iter(fake.runs["zoo"]))
        running = await c.call_tool("qt_get_zoo", {"id": zoo_id})
        assert running.is_error and running.structured_content["details"]["state"] == "queued"
        fake.finish_job(job_id, summary=sample("qt.zoo_summary"))
        done = await c.call_tool("qt_get_zoo", {"id": zoo_id})
        assert not done.is_error and done.structured_content == sample("qt.zoo_summary")
        assert done.content[0].text.startswith("zoo ")
        bad = await c.call_tool("qt_get_zoo", {"id": "rp_" + "0" * 26})
        assert bad.is_error and bad.structured_content["code"] == "QT-USAGE-001"
    assert fake.calls("POST", "/v1/zoos")[-1].headers["Idempotency-Key"] == "mcp-zoo"
    no_submit = build_server(client=fake.client(), root=tmp_path)
    names = [t.name for t in no_submit.tool_list()]
    assert "qt_get_zoo" in names and "qt_submit_zoo" not in names
    assert "qt_get_zoo" not in [t.name for t in build_server(local=True, root=tmp_path).tool_list()]
