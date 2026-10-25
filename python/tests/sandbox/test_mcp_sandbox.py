"""The sandbox MCP tools through the `mcp` SDK client: listing, schemas and annotations, a full episode whose results
validate against the declared output schemas, a finish result whose size does not grow with the episode, the
server-side output directory, listing, closing and evicting sessions, a failed finish, errors as `qt.error` tool
results, root containment, no writes under the root, the session limit, and the stdio entry point."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import jsonschema
import mcp.types as types
import pytest
from mcp import Client as McpClient
from mcp import StdioServerParameters
from mcp.shared.exceptions import MCPError

from queuetruth.errors import QtError
from queuetruth.mcp.sandbox_tools import SANDBOX_TOOL_NAMES, register_sandbox_tools
from queuetruth.mcp.server import build_server
from queuetruth.mcp.tools import TOOLS
from queuetruth.sandbox import Sandbox, verify
from queuetruth.sandbox.models import json_schemas
from sandbox.sbx_helpers import FIXTURES, REPO

pytestmark = [pytest.mark.requires_core, pytest.mark.asyncio]
LOCAL = [t.name for t in TOOLS if t.local]
START = {
    "dataset_path": "synth_small",
    "instrument_id": 1,
    "session_date": "2026-06-15",
    "start": "13:35",
    "end": "13:50",
    "latency": "1ms",
    "fees": {"maker_per_unit": -0.2, "taker_per_unit": 0.3},
    "markout_horizons": ["1s", "10s"],
}


def tree_state(root: Path) -> dict[str, tuple[int, int, str]]:
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else ""
        out[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns, digest)
    return out


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    for name in ("synth_small", "synth_small_nasdaq"):
        os.symlink(FIXTURES / name, r / name)  # inside the root by path; resolves outside: refused
    (r / "copy").mkdir()
    import shutil

    shutil.copytree(FIXTURES / "synth_small", r / "copy" / "synth_small")
    return r


def server(root: Path, **kw: Any) -> Any:
    app = build_server(local=True, root=root)
    register_sandbox_tools(app, **kw)
    return app


async def test_tools_are_listed_with_schemas_and_annotations(root: Path) -> None:
    app = server(root)
    async with McpClient(app.server) as c:
        tools = (await c.list_tools()).tools
        instructions = c.instructions or ""
    names = [t.name for t in tools]
    assert names == [*LOCAL, *SANDBOX_TOOL_NAMES]
    schemas = json_schemas()
    by = {t.name: t for t in tools}
    for name, out in (
        ("qt_sandbox_start", "qt.sandbox_session"),
        ("qt_sandbox_observe", "qt.sandbox_observation"),
        ("qt_sandbox_submit", "qt.sandbox_ack"),
        ("qt_sandbox_cancel", "qt.sandbox_ack"),
        ("qt_sandbox_advance", "qt.sandbox_observation"),
        ("qt_sandbox_finish", "qt.sandbox_finish"),
        ("qt_sandbox_list", "qt.sandbox_session_list"),
        ("qt_sandbox_close", "qt.sandbox_session_list"),
    ):
        t = by[name]
        assert t.output_schema == schemas[out]
        jsonschema.Draft202012Validator.check_schema(t.input_schema)
        assert t.input_schema["additionalProperties"] is False
        a = t.annotations
        assert a.destructive_hint is (name == "qt_sandbox_close") and a.open_world_hint is False
        assert a.read_only_hint is (name in ("qt_sandbox_observe", "qt_sandbox_list"))
    assert by["qt_sandbox_finish"].input_schema["properties"]["include_log"]["default"] is False
    assert "qt_sandbox_start" in instructions and "qt_local_why" in instructions
    with pytest.raises(QtError):
        register_sandbox_tools(app)  # twice: refused


async def test_a_full_episode_through_the_client(root: Path) -> None:
    before = tree_state(root / "copy")
    app = server(root)
    schemas = json_schemas()
    async with McpClient(app.server) as c:

        async def ok(name: str, args: dict[str, Any]) -> types.CallToolResult:
            res = await c.call_tool(name, args)
            assert not res.is_error, res.content
            jsonschema.Draft202012Validator(schemas[res.structured_content["schema"]]).validate(res.structured_content)
            assert all(isinstance(b, types.TextContent) for b in res.content[:2]) and res.content[1].text
            return res

        start = await ok("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})
        doc = start.structured_content
        sid = doc["session_id"]
        assert doc["schema"] == "qt.sandbox_session" and doc["spec"]["latency_ns"] == 1_000_000
        assert "QTAU6" in start.content[1].text and "rules:" in start.content[1].text
        bid = doc["observation"]["book"]["bid"][0]
        ack = await ok(
            "qt_sandbox_submit",
            {"session_id": sid, "side": "buy", "qty": 2, "price": "5001.00", "client_order_id": "a-1"},
        )
        assert ack.structured_content["order"]["price"] == bid and "o1 BUY 2 @ 5001.00" in ack.content[1].text
        again = await ok(
            "qt_sandbox_submit",
            {"session_id": sid, "side": "buy", "qty": 2, "price": "5001.00", "client_order_id": "a-1"},
        )
        assert again.structured_content["duplicate"] is True
        await ok("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 1, "type": "market"})
        await ok("qt_sandbox_submit", {"session_id": sid, "side": "sell", "qty": 3, "peg_offset_ticks": -1})
        stp = await c.call_tool("qt_sandbox_submit", {"session_id": sid, "side": "sell", "qty": 1, "type": "market"})
        assert stp.is_error and stp.structured_content["details"] == {"reason": "self_trade", "order_id": "o1"}
        obs = await ok("qt_sandbox_advance", {"session_id": sid, "by": "20s"})
        assert obs.structured_content["step"] == 1 and "step 1 |" in obs.content[1].text
        assert (await ok("qt_sandbox_observe", {"session_id": sid})).structured_content == obs.structured_content
        open_ = [
            o for o in obs.structured_content["orders"] if o["status"] in ("pending", "working") and o["tif"] == "day"
        ]
        if open_:
            await ok("qt_sandbox_cancel", {"session_id": sid, "order_id": open_[0]["order_id"]})
        await ok("qt_sandbox_advance", {"session_id": sid, "to": "13:40"})
        fin = await ok("qt_sandbox_finish", {"session_id": sid, "detail": "full", "include_log": True})
        summary = fin.structured_content
        card = summary["scorecard"]
        assert summary["schema"] == "qt.sandbox_finish" and summary["session_id"] == sid
        assert card["consistency"]["equal"] and card["steps"] == 2 and summary["certificate"] == card["certificate"]
        assert (summary["n_orders"], summary["n_fills"]) == (len(card["orders"]), len(card["fills"]))
        assert "consistency with one full exact replay: EQUAL" in fin.content[1].text
        (res,) = [b for b in fin.content if isinstance(b, types.EmbeddedResource)]
        assert res.resource.uri == f"queuetruth://sandbox/{sid}/order_log.json"
        log = json.loads(res.resource.text)
        assert log["root"] == card["certificate"]["log_root"]
        gone = await c.call_tool("qt_sandbox_observe", {"session_id": sid})
        assert gone.is_error and gone.structured_content["code"] == "QT-USAGE-001"
        # by default: the summary alone, no log, no per-order arrays
        s2 = (await ok("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})).structured_content[
            "session_id"
        ]
        fin2 = await ok("qt_sandbox_finish", {"session_id": s2})
        assert not any(isinstance(b, types.EmbeddedResource) for b in fin2.content)
        assert fin2.structured_content["scorecard"] is None and "orders" not in fin2.structured_content
    assert tree_state(root / "copy") == before  # nothing written under the root
    v = verify(log, dataset=root / "copy" / "synth_small", scorecard=card)
    assert v.ok, v.problems


async def test_the_finish_result_stays_small_and_the_out_directory_holds_the_rest(root: Path, tmp_path: Path) -> None:
    """A session with hundreds of orders: the default finish result is a few kilobytes (it carries no per-order or
    per-fill arrays); the full scorecard and the order log are written to the server's output directory, linked from
    the result, and verify."""
    out = tmp_path / "out"
    app = server(root, out_dir=out)
    before = tree_state(root / "copy")
    async with McpClient(app.server) as c:
        sid = (await c.call_tool("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})).structured_content[
            "session_id"
        ]
        n = 0
        for _ in range(30):
            for k in range(8):
                res = await c.call_tool(
                    "qt_sandbox_submit",
                    {"session_id": sid, "side": "buy" if k % 2 else "sell", "qty": 1, "peg_offset_ticks": -(k // 2)},
                )
                n += not res.is_error
            await c.call_tool("qt_sandbox_advance", {"session_id": sid, "by": "2s"})
        fin = await c.call_tool("qt_sandbox_finish", {"session_id": sid})
        full = await c.call_tool("qt_sandbox_list", {})
    assert not fin.is_error and n >= 200
    summary = fin.structured_content
    assert summary["n_orders"] == n and summary["out_dir"] == str((out / sid).resolve())
    size = len(json.dumps(summary)) + sum(len(b.text) for b in fin.content if isinstance(b, types.TextContent))
    assert size < 16_000, size
    card = json.loads((out / sid / "scorecard.json").read_text())
    assert len(card["orders"]) == n and len(json.dumps(card)) > 10 * size
    links = {b.name: b for b in fin.content if isinstance(b, types.ResourceLink)}
    assert set(links) == {"scorecard.json", "order_log.json"}
    assert links["order_log.json"].uri == (out / sid / "order_log.json").resolve().as_uri()
    assert {f["name"] for f in summary["files"]} >= {"order_log.json", "replay/shadow_fills.parquet"}
    v = verify(out / sid / "order_log.json", scorecard=out / sid / "scorecard.json", workdir=tmp_path)
    assert v.ok, v.problems
    assert full.structured_content["sessions"] == []
    assert tree_state(root / "copy") == before
    with pytest.raises(QtError) as e:
        server(root, out_dir=root / "copy" / "out")  # inside the root: refused
    assert e.value.code == "QT-CONFIG-001"


async def test_sessions_can_be_listed_closed_and_evicted(root: Path) -> None:
    app = server(root, max_sessions=2, idle_timeout_s=3600)
    async with McpClient(app.server) as c:
        ids = [
            (await c.call_tool("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})).structured_content[
                "session_id"
            ]
            for _ in range(2)
        ]
        await c.call_tool("qt_sandbox_submit", {"session_id": ids[0], "side": "buy", "qty": 1, "price": "5001.00"})
        listed = (await c.call_tool("qt_sandbox_list", {})).structured_content
        assert [s["session_id"] for s in listed["sessions"]] == ids and listed["max_sessions"] == 2
        assert listed["sessions"][0]["n_orders"] == 1 and listed["sessions"][0]["symbol"] == "QTAU6"
        full = await c.call_tool("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})
        assert full.is_error and "qt_sandbox_close" in (full.structured_content.get("hint") or "")
        wd = app.sandbox_tools.sessions[ids[0]].workdir
        closed = await c.call_tool("qt_sandbox_close", {"session_id": ids[0]})
        assert closed.structured_content["closed"]["session_id"] == ids[0] and not wd.exists()
        assert [s["session_id"] for s in closed.structured_content["sessions"]] == ids[1:]
        gone = await c.call_tool("qt_sandbox_observe", {"session_id": ids[0]})
        assert gone.is_error
        # a session idle for longer than the timeout is evicted when a new one needs its slot; a used one is not
        app.sandbox_tools.idle_timeout_s = 0.3
        s3 = (await c.call_tool("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})).structured_content
        await anyio.sleep(0.4)
        assert not (await c.call_tool("qt_sandbox_observe", {"session_id": s3["session_id"]})).is_error
        s4 = (await c.call_tool("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})).structured_content
        assert set(app.sandbox_tools.sessions) == {s3["session_id"], s4["session_id"]}  # ids[1] was evicted
        assert (await c.call_tool("qt_sandbox_observe", {"session_id": ids[1]})).is_error
    app.sandbox_tools.close_all()


async def test_a_failed_finish_frees_the_session(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = server(root, max_sessions=1)
    async with McpClient(app.server) as c:
        sid = (await c.call_tool("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})).structured_content[
            "session_id"
        ]
        wd = app.sandbox_tools.sessions[sid].workdir

        def broken(self: Sandbox, **kw: Any) -> Any:
            raise QtError("QT-ENGINE-001", "the certification replay failed")

        monkeypatch.setattr(Sandbox, "finish", broken)
        res = await c.call_tool("qt_sandbox_finish", {"session_id": sid})
        assert res.is_error and res.structured_content["code"] == "QT-ENGINE-001"
        assert app.sandbox_tools.sessions == {} and not wd.exists()
        monkeypatch.undo()
        again = await c.call_tool("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})
        assert not again.is_error  # the slot is free again


async def test_errors_are_tool_results(root: Path) -> None:
    app = server(root, max_sessions=1)
    async with McpClient(app.server) as c:
        sid = (await c.call_tool("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})).structured_content[
            "session_id"
        ]

        async def err(name: str, args: dict[str, Any]) -> dict[str, Any]:
            res = await c.call_tool(name, args)
            assert res.is_error, res.content
            assert res.content[0].text.startswith(res.structured_content["code"])
            return res.structured_content

        assert (await err("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 1, "price": "5001.01"}))[
            "code"
        ] == "QT-CONFIG-001"
        e = await err("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 1, "price": "5001.000000000001"})
        assert e["code"] == "QT-INPUT-004"  # finer than the price exponent
        e = await err("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": "1.5", "price": "5001"})
        assert e["code"] == "QT-INPUT-004"  # a decimal size finer than the instrument's size units
        e = await err("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": "2", "price": "5001.01"})
        assert e["code"] == "QT-CONFIG-001" and "tick grid" in e["message"]  # "2" parsed as a size, price checked
        e = await err("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 0, "price": "5001"})
        assert e["code"] == "QT-USAGE-001" and "invalid arguments" in e["message"]
        e = await err("qt_sandbox_submit", {"session_id": sid, "side": "hold", "qty": 1, "price": "5001"})
        assert e["code"] == "QT-USAGE-001"
        e = await err("qt_sandbox_cancel", {"session_id": sid, "order_id": "o7"})
        assert e["code"] == "QT-USAGE-001" and "unknown order_id" in e["message"]
        e = await err("qt_sandbox_advance", {"session_id": sid, "to": "13:00"})
        assert e["code"] == "QT-USAGE-001"
        e = await err("qt_sandbox_advance", {"session_id": sid, "by": "5"})
        assert e["code"] == "QT-USAGE-001"  # a duration without a unit fails the input schema
        e = await err("qt_sandbox_observe", {"session_id": "sbx_0000000000000000"})
        assert e["code"] == "QT-USAGE-001" and "unknown or finished" in e["message"]
        e = await err("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})
        assert e["code"] == "QT-USAGE-001" and "max 1" in e["message"]
        await c.call_tool("qt_sandbox_finish", {"session_id": sid})
        e = await err("qt_sandbox_start", {**START, "dataset_path": "synth_small"})
        assert e["code"] == "QT-CONFIG-001" and "outside QT_MCP_ROOT" in e["message"]
        e = await err("qt_sandbox_start", {**START, "dataset_path": "../elsewhere"})
        assert e["code"] == "QT-CONFIG-001"
        e = await err("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small", "instrument_id": 9})
        assert e["code"] == "QT-CONFIG-001" and "no partition" in e["message"]
        e = await err("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small", "bogus": 1})
        assert e["code"] == "QT-USAGE-001"
        with pytest.raises(MCPError):
            await c.call_tool("qt_sandbox_nope", {})
    assert app.sandbox_tools.sessions == {}


async def test_a_dataset_file_symlinked_out_of_the_root_is_refused(root: Path, tmp_path: Path) -> None:
    ds = root / "copy" / "synth_small"
    part = ds / "mbo" / "date=2026-06-15" / "instrument_id=1" / "part-00000.parquet"
    outside = tmp_path / "outside.parquet"
    part.rename(outside)
    os.symlink(outside, part)
    app = server(root)
    async with McpClient(app.server) as c:
        res = await c.call_tool("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})
    assert res.is_error and res.structured_content["code"] == "QT-CONFIG-001"
    assert "resolves outside" in res.structured_content["message"]


async def test_stdio_entry_point(root: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("QT_API_KEY", "QT_API_URL")}
    env["QT_MCP_ROOT"] = str(root)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "python"), env.get("PYTHONPATH", "")])
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "queuetruth.mcp.sandbox_tools"], env=env, cwd=str(root)
    )
    async with McpClient(params) as c:
        names = [t.name for t in (await c.list_tools()).tools]
        start = await c.call_tool("qt_sandbox_start", {**START, "dataset_path": "copy/synth_small"})
        sid = start.structured_content["session_id"]
        await c.call_tool("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 1, "price": "5001.00"})
        await c.call_tool("qt_sandbox_advance", {"session_id": sid, "by": "1m"})
        fin = await c.call_tool("qt_sandbox_finish", {"session_id": sid})
    assert names == [*LOCAL, *SANDBOX_TOOL_NAMES]
    assert not fin.is_error and fin.structured_content["consistency"]["equal"]
    assert fin.structured_content["schema"] == "qt.sandbox_finish"
