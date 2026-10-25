"""MCP test setup: optional tool modules (`QT_MCP_OPTIONAL_TOOLS`) are off here, so the SPEC 4.6 tool lists of the
stdio tests (which run `serve`, the one entry point that registers them) do not depend on which optional modules (for
example `queuetruth.mcp.sandbox_tools`) are installed; `build_server` registers none unless a test passes
`tool_modules`. Run mode is off unless a test turns it on."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _mcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_MCP_OPTIONAL_TOOLS", "0")
    for var in ("QT_MCP_ALLOW_RUN", "QT_MCP_RUN_ROOT", "QT_MCP_DATA_ROOTS"):
        monkeypatch.delenv(var, raising=False)
