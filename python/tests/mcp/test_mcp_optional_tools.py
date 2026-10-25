"""The optional tool module hook: the stdio server imports `queuetruth.mcp.sandbox_tools` (a stub here) when it is
installed and calls `register_sandbox_tools(server)`, whose tools are listed after the built-in tools and callable
through the MCP client, whether they come through `QtMcpServer.add_tool` or are written into the registry directly
(the way agent-sandbox's module registers). `build_server` registers no optional module unless asked, so a program
that registers the module itself does not get it twice, and the hook skips a module already registered. A missing
module is skipped quietly, a broken one (import error, missing or failing register function) is logged and skipped
with everything its registration changed rolled back, `QT_MCP_OPTIONAL_TOOLS=0` turns the hook off, and `add_tool`
validates names, schemas and handler results."""

from __future__ import annotations

import logging
import sys
import types as pytypes
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from mcp import Client as McpClient
from pydantic import BaseModel

from queuetruth.errors import QtError
from queuetruth.mcp import server as qt_server
from queuetruth.mcp.server import OPTIONAL_TOOL_MODULES, build_server, default_tool_modules, register_optional_tools
from queuetruth.mcp.tools import TOOLS, Args, ToolDef

LOCAL = [t.name for t in TOOLS if t.local]
STUB = "queuetruth.mcp.sandbox_tools"


class EchoArgs(Args):
    """Stub tool arguments."""

    text: str
    n: int = 1


ECHO_OUT = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"echo": {"type": "string"}, "local_mode": {"type": "boolean"}},
    "required": ["echo"],
}


def stub_module(register: Any, name: str = STUB) -> pytypes.ModuleType:
    mod = pytypes.ModuleType(name)
    mod.register_sandbox_tools = register  # type: ignore[attr-defined]
    return mod


def register_echo(server: Any) -> None:
    def echo(tool: ToolDef, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, EchoArgs)
        if args.text == "fail":
            raise QtError("QT-INPUT-001", "asked to fail", "do not")
        return {"echo": args.text * args.n, "local_mode": server.local_mode}

    async def session(tool: ToolDef, args: BaseModel) -> tuple[dict[str, Any], list[Any]]:
        import mcp.types as types

        return {"echo": "async"}, [types.TextContent(text="extra block")]

    def bad(tool: ToolDef, args: BaseModel) -> Any:
        return ["not", "a", "document"]

    server.add_tool(
        ToolDef("qt_sandbox_echo", "Echo", "Echoes its text.", EchoArgs, "echo", local=True),
        echo,
        output_schema=ECHO_OUT,
    )
    server.add_tool(
        ToolDef("qt_sandbox_session", "Session", "Async handler.", EchoArgs, "echo", local=True),
        session,
        output_schema=ECHO_OUT,
        read_only=False,
    )
    server.add_tool(ToolDef("qt_sandbox_bad", "Bad", "Bad result.", EchoArgs, "echo"), bad, output_schema=ECHO_OUT)


def test_default_modules_name_the_sandbox_tools():
    assert (STUB, "register_sandbox_tools") in OPTIONAL_TOOL_MODULES


@pytest.mark.asyncio
async def test_stub_sandbox_module_registers_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, STUB, stub_module(register_echo))
    monkeypatch.setenv("QT_MCP_OPTIONAL_TOOLS", "1")
    app = build_server(local=True, root=tmp_path, tool_modules=default_tool_modules())  # what `serve` passes
    assert app.optional_modules == [STUB]
    async with McpClient(app.server) as c:
        tools = (await c.list_tools()).tools
        names = [t.name for t in tools]
        assert names == [*LOCAL, "qt_sandbox_echo", "qt_sandbox_session", "qt_sandbox_bad"]
        by = {t.name: t for t in tools}
        assert by["qt_sandbox_echo"].output_schema == ECHO_OUT
        assert by["qt_sandbox_echo"].input_schema["properties"]["text"]["type"] == "string"
        jsonschema.Draft202012Validator.check_schema(by["qt_sandbox_echo"].input_schema)
        assert by["qt_sandbox_echo"].annotations.read_only_hint is True
        assert by["qt_sandbox_session"].annotations.read_only_hint is False
        assert all(t.annotations.destructive_hint is False for t in tools)
        res = await c.call_tool("qt_sandbox_echo", {"text": "ab", "n": 2})
        assert not res.is_error and res.structured_content == {"echo": "abab", "local_mode": True}
        assert res.content[0].text == 'result document\n{"echo":"abab","local_mode":true}'
        res = await c.call_tool("qt_sandbox_session", {"text": "x"})
        assert res.structured_content == {"echo": "async"} and res.content[1].text == "extra block"
        res = await c.call_tool("qt_sandbox_echo", {"text": "fail"})
        assert res.is_error and res.structured_content["code"] == "QT-INPUT-001"
        assert res.content[0].text == "QT-INPUT-001: asked to fail (hint: do not)"
        res = await c.call_tool("qt_sandbox_echo", {"text": 3})
        assert res.is_error and res.structured_content["code"] == "QT-USAGE-001"
        res = await c.call_tool("qt_sandbox_bad", {"text": "x"})
        assert res.is_error and res.structured_content["code"] == "QT-ENGINE-001"
        assert "not a document" in res.structured_content["message"]


def test_hook_on_in_serve_off_by_env_and_never_in_build_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, STUB, stub_module(register_echo))
    monkeypatch.delenv("QT_MCP_OPTIONAL_TOOLS")
    assert default_tool_modules() == OPTIONAL_TOOL_MODULES  # on by default in run mode
    assert default_tool_modules(allow_run=False) == ()  # the sandbox runs replays: never without run mode
    assert build_server(local=True, root=tmp_path).optional_modules == []  # build_server: only when asked
    served: list[Any] = []
    monkeypatch.setattr(qt_server.anyio, "run", lambda fn, app: served.append(app))
    monkeypatch.chdir(tmp_path)
    qt_server.serve(local=True, allow_run=False)  # `queuetruth mcp serve`: read-only, no optional modules
    assert served[0].optional_modules == [] and [t.name for t in served[0].tools] == LOCAL
    qt_server.serve(local=True, allow_run=True)  # `queuetruth mcp serve --allow-run`: the hook runs
    assert served[1].optional_modules == [STUB] and "qt_sandbox_echo" in [t.name for t in served[1].tools]
    monkeypatch.setenv("QT_MCP_OPTIONAL_TOOLS", "0")
    assert default_tool_modules() == ()
    qt_server.serve(local=True, allow_run=True)
    assert served[2].optional_modules == [] and "qt_sandbox_echo" not in [t.name for t in served[2].tools]
    monkeypatch.setenv("QT_MCP_OPTIONAL_TOOLS", "sometimes")
    with pytest.raises(QtError) as e:
        default_tool_modules()
    assert e.value.code == "QT-CONFIG-001"


def test_missing_module_is_skipped_quietly(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.DEBUG, logger="queuetruth.mcp"):
        app = build_server(local=True, root=tmp_path, tool_modules=[("queuetruth.mcp.no_such_tools", "register")])
    assert app.optional_modules == [] and [t.name for t in app.tools] == LOCAL
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_broken_modules_are_logged_and_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    pkg = tmp_path / "plugins"
    pkg.mkdir()
    (pkg / "qt_plugin_bad_import.py").write_text("import qt_dependency_that_is_not_installed\n")
    (pkg / "qt_plugin_raises.py").write_text("raise RuntimeError('boom at import')\n")
    (pkg / "qt_plugin_no_register.py").write_text("x = 1\n")
    monkeypatch.syspath_prepend(str(pkg))

    def half_then_fail(server: Any) -> None:
        server.add_tool(ToolDef("qt_half", "Half", "d", EchoArgs, "echo"), lambda t, a: {}, output_schema=ECHO_OUT)
        raise RuntimeError("register failed")

    def direct_without_handler(server: Any) -> None:  # bypasses add_tool: a tool the server cannot call
        server.tools.append(ToolDef("qt_direct", "Direct", "d", EchoArgs, "qt.why_trace"))
        server._by_name["qt_direct"] = server.tools[-1]

    def direct_unlistable(server: Any) -> None:  # bypasses add_tool: an output schema that cannot be resolved
        tool = ToolDef("qt_unlistable", "U", "d", EchoArgs, "no_such_document")
        server.tools.append(tool)
        server._by_name[tool.name] = tool
        server._handlers[tool.name] = server._handlers["qt_local_why"]

    monkeypatch.setitem(sys.modules, "qt_plugin_half", stub_module(half_then_fail, "qt_plugin_half"))
    monkeypatch.setitem(sys.modules, "qt_plugin_direct", stub_module(direct_without_handler, "qt_plugin_direct"))
    monkeypatch.setitem(sys.modules, "qt_plugin_unlistable", stub_module(direct_unlistable, "qt_plugin_unlistable"))
    monkeypatch.setitem(sys.modules, "qt_plugin_ok", stub_module(register_echo, "qt_plugin_ok"))
    modules = [
        ("qt_plugin_bad_import", "register_sandbox_tools"),
        ("qt_plugin_raises", "register_sandbox_tools"),
        ("qt_plugin_no_register", "register_sandbox_tools"),
        ("qt_plugin_half", "register_sandbox_tools"),
        ("qt_plugin_direct", "register_sandbox_tools"),
        ("qt_plugin_unlistable", "register_sandbox_tools"),
        ("qt_plugin_ok", "register_sandbox_tools"),
    ]
    with caplog.at_level(logging.WARNING, logger="queuetruth.mcp"):
        app = build_server(local=True, root=tmp_path, tool_modules=modules)
    assert app.optional_modules == ["qt_plugin_ok"]
    names = [t.name for t in app.tools]
    assert names == [*LOCAL, "qt_sandbox_echo", "qt_sandbox_session", "qt_sandbox_bad"]
    assert "qt_half" not in app._handlers and "qt_unlistable" not in app._by_name  # failed registrations rolled back
    text = caplog.text
    for needle in (
        "qt_plugin_bad_import not loaded",
        "qt_plugin_raises not loaded: RuntimeError",
        "qt_plugin_no_register has no callable register_sandbox_tools",
        "qt_plugin_half failed to register",
        "qt_plugin_direct failed to register",
        "qt_plugin_unlistable failed to register",
    ):
        assert needle in text, needle
    assert register_optional_tools(app, []) == []


def test_add_tool_validation(tmp_path: Path):
    app = build_server(local=True, root=tmp_path, tool_modules=())

    def h(t: ToolDef, a: Any) -> dict[str, Any]:
        return {}

    with pytest.raises(ValueError, match="already registered"):
        app.add_tool(ToolDef("qt_local_why", "x", "d", EchoArgs, "echo"), h, output_schema=ECHO_OUT)
    for bad in ("", "has space", "x" * 65, "semi;colon", "qt_x\n", "\nqt_x"):
        with pytest.raises(ValueError, match="invalid tool name"):
            app.add_tool(ToolDef(bad, "x", "d", EchoArgs, "echo"), h, output_schema=ECHO_OUT)
    with pytest.raises(ValueError, match="needs an output_schema"):
        app.add_tool(ToolDef("qt_x", "x", "d", EchoArgs, "not_a_document"), h)
    with pytest.raises(TypeError, match="not callable"):
        app.add_tool(ToolDef("qt_x", "x", "d", EchoArgs, "echo"), "nope", output_schema=ECHO_OUT)  # type: ignore[arg-type]
    assert [t.name for t in app.tools] == LOCAL
    app.add_tool(ToolDef("qt_x", "x", "d", EchoArgs, "qt.why_trace"), h)  # a qt.* output needs no schema
    assert app.tool_list()[-1].output_schema["title"] == "qt.why_trace"


# ---- registration that writes the registry directly (agent-sandbox's register_sandbox_tools) -------------------


DIRECT_INSTRUCTIONS = "Direct tools: qt_direct_echo echoes."


def register_direct(server: Any, *, fail_after: bool = False) -> Any:
    """The shape of agent-sandbox's `register_sandbox_tools`: writes `_by_name`/`_handlers`, overrides `tool_list`
    on the instance, appends to the instructions, sets `server.sandbox_tools`, and refuses a second registration."""
    import mcp.types as types

    tool = ToolDef("qt_direct_echo", "Direct echo", "Echoes its text.", EchoArgs, "echo", local=True)
    if tool.name in server._by_name:
        raise QtError("QT-CONFIG-001", f"tool {tool.name} is already registered")

    async def handler(t: ToolDef, args: Any) -> tuple[dict[str, Any], list[Any]]:
        return {"echo": args.text}, []

    server._by_name[tool.name] = tool
    server._handlers[tool.name] = handler
    base = server.tool_list
    listed = types.Tool(name=tool.name, input_schema=EchoArgs.model_json_schema(), output_schema=ECHO_OUT)
    server.tool_list = lambda: [*base(), listed]
    server.server.instructions = f"{server.server.instructions} {DIRECT_INSTRUCTIONS}"
    server.sandbox_tools = object()
    if fail_after:
        raise RuntimeError("register failed after changing the server")
    return server.sandbox_tools


@pytest.mark.asyncio
async def test_direct_registration_composes_with_the_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, STUB, stub_module(register_direct))
    # `python -m queuetruth.mcp.sandbox_tools` and agent-sandbox's tests: build_server, then register explicitly
    app = build_server(local=True, root=tmp_path)
    register_direct(app)
    assert [t.name for t in app.tool_list()] == [*LOCAL, "qt_direct_echo"]
    # the hook: registers once, and a second hook call (or a module already registered) is skipped
    hooked = build_server(local=True, root=tmp_path, tool_modules=OPTIONAL_TOOL_MODULES)
    assert hooked.optional_modules == [STUB]
    assert register_optional_tools(hooked, OPTIONAL_TOOL_MODULES) == [] and hooked.optional_modules == [STUB]
    async with McpClient(hooked.server) as c:
        assert [t.name for t in (await c.list_tools()).tools] == [*LOCAL, "qt_direct_echo"]
        assert DIRECT_INSTRUCTIONS in (c.instructions or "")
        res = await c.call_tool("qt_direct_echo", {"text": "hi"})
        assert not res.is_error and res.structured_content == {"echo": "hi"}
    with pytest.raises(QtError, match="already registered"):  # an explicit second registration: the module's call
        register_direct(hooked)


def test_failed_direct_registration_is_rolled_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "qt_plugin_direct_fail", stub_module(partial_fail, "qt_plugin_direct_fail"))
    monkeypatch.setitem(sys.modules, "qt_plugin_hidden", stub_module(hidden_tool, "qt_plugin_hidden"))
    clean = build_server(local=True, root=tmp_path)
    app = build_server(
        local=True,
        root=tmp_path,
        tool_modules=[
            ("qt_plugin_direct_fail", "register_sandbox_tools"),
            ("qt_plugin_hidden", "register_sandbox_tools"),
        ],
    )
    assert app.optional_modules == []
    assert "tool_list" not in vars(app) and not hasattr(app, "sandbox_tools")
    assert [t.name for t in app.tool_list()] == LOCAL
    assert app.server.instructions == clean.server.instructions
    assert set(app._by_name) == set(clean._by_name) and set(app._handlers) == set(clean._handlers)


def partial_fail(server: Any) -> None:
    register_direct(server, fail_after=True)


def hidden_tool(server: Any) -> None:  # callable by name but never listed
    server._by_name["qt_hidden"] = ToolDef("qt_hidden", "Hidden", "d", EchoArgs, "echo")
    server._handlers["qt_hidden"] = server._handlers["qt_local_why"]


def test_real_sandbox_module_composes_with_the_hook(tmp_path: Path):
    """Once agent-sandbox is merged: the stdio server registers its tools through the hook, and its own entry point
    (build_server, then register_sandbox_tools) still works."""
    sandbox_tools = pytest.importorskip("queuetruth.mcp.sandbox_tools")
    names = list(sandbox_tools.SANDBOX_TOOL_NAMES)
    app = build_server(local=True, root=tmp_path)
    sandbox_tools.register_sandbox_tools(app)
    assert [t.name for t in app.tool_list()] == [*LOCAL, *names]
    hooked = build_server(local=True, root=tmp_path, tool_modules=OPTIONAL_TOOL_MODULES)
    assert hooked.optional_modules == ["queuetruth.mcp.sandbox_tools"]
    assert [t.name for t in hooked.tool_list()] == [*LOCAL, *names]
    assert register_optional_tools(hooked, OPTIONAL_TOOL_MODULES) == []
