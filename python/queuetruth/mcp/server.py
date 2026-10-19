"""MCP stdio server (`queuetruth mcp serve`, SPEC 4.6) on the official `mcp` SDK 2.x low-level `Server`.

Modes: remote (default; needs `QT_API_KEY`, calls the HTTP API at `QT_API_URL` through `queuetruth.client.Client`)
lists the remote read tools, the submit tools only with `allow_submit`, then the local tools; local (`local=True`
or no API key) lists only the local read tools, which read under `QT_MCP_ROOT` and never write. Tools are listed in
the order of SPEC 4.6 and return `structuredContent` (validated against the tool's output schema) plus a short text
rendering; job files are returned as `resource_link` items, never inline. Failures are tool results with
`isError = true`, a `qt.error` document as `structuredContent` and `"<code>: <message>"` as text. Long jobs: the
installed SDK has no `io.modelcontextprotocol/tasks` extension, so submit tools return the `qt.job` handle for
polling with `qt_get_job`. No tool deletes anything or manages keys.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import Any

import anyio
import anyio.to_thread
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ValidationError

from queuetruth._version import __version__
from queuetruth.client import ENV_API_KEY, Client
from queuetruth.errors import QtError
from queuetruth.mcp.local import LocalOutputs, core_why
from queuetruth.mcp.metrics import METRICS, explain
from queuetruth.mcp.tools import TOOLS, ToolDef, input_schema, output_schema

SERVER_NAME = "queuetruth"
TEXT_JSON_LIMIT = 1500
log = logging.getLogger("queuetruth.mcp")

INSTRUCTIONS = (
    "queuetruth: exact order-book replay, queue-model zoo, calibration and sim-vs-live scorecards. Read tools return "
    "qt.* JSON documents (structuredContent). Submit tools (if listed) return a qt.job handle: poll qt_get_job until "
    "the state is terminal, then read the summary (qt_get_replay/qt_get_calibration/qt_get_scorecard) or the files "
    "(qt_list_job_files). Use qt_explain_metric for metric definitions. Local tools read output directories under "
    "QT_MCP_ROOT and never write."
)
#: Local mode lists only the SPEC 4.6 L tools, so its instructions name only those (no remote tool, no
#: qt_explain_metric: it is not marked L).
LOCAL_INSTRUCTIONS = (
    "queuetruth (local mode): read-only tools over output directories under QT_MCP_ROOT; they never write. "
    "qt_local_list_outputs lists the qt.* documents under a directory, qt_local_read_summary reads one (validated by "
    "its schema field), qt_local_why explains one shadow's outcome. Remote tools, submit tools and the metric "
    "glossary (qt_explain_metric) need remote mode (QT_API_KEY)."
)


def instructions_for(local_mode: bool) -> str:
    """The server instructions of a mode: they name only tools that mode lists."""
    return LOCAL_INSTRUCTIONS if local_mode else INSTRUCTIONS


Handler = Callable[[ToolDef, BaseModel], Awaitable[tuple[dict[str, Any], list[types.ContentBlock]]]]


def _first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    return f"{loc or '<arguments>'}: {err.get('msg')}"


def render_text(doc: dict[str, Any]) -> str:
    """Short text rendering of a tool result: one headline, plus the compact JSON when it is small."""
    schema = doc.get("schema")
    try:
        head = _headline(doc)
    except (KeyError, TypeError, ValueError, IndexError):
        head = f"{schema or 'result'} document"
    body = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
    if len(body) <= TEXT_JSON_LIMIT:
        return f"{head}\n{body}"
    return f"{head}\n(full {schema or 'result'} document in structuredContent)"


def _horizon_order(h: str) -> tuple[int, str]:
    return (int(h), h) if h.isdigit() else (1 << 62, h)


def trust_lines(doc: dict[str, Any]) -> list[str]:
    """The export verdict of a calibration: the `trust: beats_default: ...` and `trust: verdict: ...` lines,
    from the structured `trust` field (schema 1.1) when present, else from the `warnings` lines."""
    t = doc.get("trust")
    if isinstance(t, dict) and t.get("verdict"):
        out = []
        beats = t.get("beats_default") or {}
        if beats:
            out.append(
                "trust: beats_default: "
                + ",".join(f"{h}={beats[h]}" for h in sorted(beats, key=lambda x: _horizon_order(str(x))))
            )
        reasons = [str(r) for r in t.get("reasons") or []]
        out.append(f"trust: verdict: {t['verdict']}" + (f" ({', '.join(reasons)})" if reasons else ""))
        return out
    warnings = [w for w in doc.get("warnings") or [] if isinstance(w, str)]
    return [w for w in warnings if w.startswith(("trust: beats_default:", "trust: verdict:"))]


def _pct(x: Any) -> str:
    return f"{100 * x:.0f}%" if isinstance(x, int | float) else "?"


def _headline(doc: dict[str, Any]) -> str:
    s = doc.get("schema")
    if s == "qt.job":
        line = f"job {doc['id']} ({doc['kind']}): {doc['state']}, {_pct(doc['progress']['fraction'])}"
        line += f" [{doc['progress']['stage']}]"
        if doc.get("error"):
            line += f"; error {doc['error'].get('code')}: {doc['error'].get('title')}"
        return line
    if s == "qt.dataset":
        return f"dataset {doc['id']} {doc['name']!r}: {doc['kind']} {doc['format']} {doc['venue']}, {doc['status']}"
    if s == "qt.page":
        more = f", next_cursor {doc['next_cursor']}" if doc.get("has_more") else ""
        return f"{len(doc['data'])} item(s){more}"
    if s == "qt.meta":
        return f"queuetruth API {doc['api_version']}, engine {doc['engine_version']}, zero_egress={doc['zero_egress']}"
    if s == "qt.estimate":
        return (
            f"{doc['kind']} estimate: ~{doc['records_est']} records, {doc['shadows_est']} shadows, "
            f"{doc['cpu_seconds_est']:.1f} CPU-s, ETA {doc['eta_seconds']:.0f} s, {doc['cost_units']} cost units"
        )
    if s == "qt.file_manifest":
        names = ", ".join(f"{f['name']} ({f['bytes']} B)" for f in doc["files"][:10])
        extra = f" (+{len(doc['files']) - 10} more)" if len(doc["files"]) > 10 else ""
        return f"{len(doc['files'])} file(s) for job {doc['job_id']}: {names}{extra}"
    if s == "qt.replay_summary":
        st = doc["stats"]
        line = f"replay {doc['run_id']}: {st['records']} records, {st['events']} events, {st['trades']} trades"
        if doc.get("shadows"):
            line += f"; shadows {doc['shadows']['n']} ({doc['shadows']['n_filled']} filled)"
        return line + f"; data quality {doc['data_quality']['verdict']}"
    if s == "qt.calibration_result":
        est = f"{doc.get('param_name')} = {doc.get('estimate')}" if doc.get("param_name") else "no parameter"
        line = f"calibration {doc['run_id']} ({doc['kind']}/{doc['family']}): {est}, {doc['n_shadows']} shadows"
        return "\n".join([line, *trust_lines(doc)])
    if s == "qt.scorecard_summary":
        return f"scorecard {doc['run_id']}: {doc['verdict']['headline']}"
    if s == "qt.why_trace":
        out = doc["outcome"]
        return f"why-trace: {len(doc['events'])} events, end_state {out['end_state']}, filled {out['filled_qty']}"
    if s == "qt.error":
        return f"{doc['code']}: {doc['message']}" + (f" (hint: {doc['hint']})" if doc.get("hint") else "")
    if s is None and "definition" in doc:
        proper = "proper scoring rule" if doc["proper"] else "not a proper scoring rule"
        return f"{doc['name']} ({proper}): {doc['definition']} Formula: {doc['formula']}"
    if s is None and "entries" in doc:
        lines = [f"{e['path']}: {e['schema']}" for e in doc["entries"][:50]]
        return f"{len(doc['entries'])} qt.* document(s)" + ("\n" + "\n".join(lines) if lines else "")
    return f"{s} document"


def error_result(err: QtError) -> types.CallToolResult:
    """Tool error result: `qt.error` as structuredContent, `<code>: <message>` (+ hint) as text."""
    doc = err.to_document()
    return types.CallToolResult(content=[types.TextContent(text=_headline(doc))], structured_content=doc, is_error=True)


class QtMcpServer:
    """Tool registry and handlers behind the low-level MCP `Server`."""

    def __init__(
        self,
        *,
        allow_submit: bool = False,
        local: bool = False,
        client: Client | None = None,
        root: str | os.PathLike[str] | None = None,
        why: Callable[[str], str] = core_why,
    ) -> None:
        if client is None and not local and os.environ.get(ENV_API_KEY):
            client = Client()
        self.client = client
        self.local_mode = local or client is None
        self.allow_submit = allow_submit and not self.local_mode
        self.outputs = LocalOutputs(root)
        self._why = why
        self.tools: list[ToolDef] = [
            t for t in TOOLS if (t.local or not self.local_mode) and (not t.submit or self.allow_submit)
        ]
        self._by_name = {t.name: t for t in self.tools}
        self._handlers: dict[str, Handler] = {
            "qt_meta": self._meta,
            "qt_list_datasets": self._list_datasets,
            "qt_describe_dataset": self._describe_dataset,
            "qt_estimate": self._estimate,
            "qt_get_job": self._get_job,
            "qt_list_job_files": self._list_job_files,
            "qt_get_replay": self._get_run,
            "qt_get_calibration": self._get_run,
            "qt_get_scorecard": self._get_run,
            "qt_explain_metric": self._explain_metric,
            "qt_submit_replay": self._submit,
            "qt_submit_calibration": self._submit,
            "qt_submit_scorecard": self._submit,
            "qt_submit_report": self._submit,
            "qt_local_read_summary": self._local_read_summary,
            "qt_local_list_outputs": self._local_list_outputs,
            "qt_local_why": self._local_why,
        }
        self.server: Server[Any] = Server(
            SERVER_NAME,
            version=__version__,
            title="queuetruth",
            instructions=instructions_for(self.local_mode),
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    # ---- protocol ---------------------------------------------------------------------------------------------

    def tool_list(self) -> list[types.Tool]:
        """The listed tools, in SPEC 4.6 order."""
        return [
            types.Tool(
                name=t.name,
                title=t.title,
                description=t.description,
                input_schema=input_schema(t),
                output_schema=output_schema(t),
                annotations=types.ToolAnnotations(
                    title=t.title,
                    read_only_hint=not t.submit,
                    destructive_hint=False,
                    idempotent_hint=not t.submit,
                    open_world_hint=False,
                ),
            )
            for t in self.tools
        ]

    async def _list_tools(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=self.tool_list())

    async def _call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        tool = self._by_name.get(params.name)
        if tool is None:
            raise MCPError(types.INVALID_PARAMS, f"unknown tool {params.name!r}")
        return await self.call(tool, params.arguments or {})

    async def call(self, tool: ToolDef, arguments: dict[str, Any]) -> types.CallToolResult:
        """Validates arguments, runs the handler and wraps the result or error."""
        try:
            args = tool.args.model_validate(arguments)
        except ValidationError as e:
            return error_result(QtError("QT-USAGE-001", f"{tool.name}: invalid arguments: {_first_error(e)}"))
        try:
            doc, extra = await self._handlers[tool.name](tool, args)
        except QtError as e:
            return error_result(e)
        except Exception as e:  # a tool failure must not end the session
            log.exception("tool %s failed", tool.name)
            return error_result(QtError("QT-ENGINE-001", f"{tool.name}: internal error: {type(e).__name__}: {e}"))
        return types.CallToolResult(content=[types.TextContent(text=render_text(doc)), *extra], structured_content=doc)

    async def _remote(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self.client is None:
            raise QtError("QT-CONFIG-001", f"{ENV_API_KEY} is not set (local mode)")
        return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))

    # ---- remote tools -----------------------------------------------------------------------------------------

    async def _meta(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        assert self.client is not None
        meta = await self._remote(self.client.meta, refresh=True)
        return meta.model_dump(mode="json"), []

    async def _list_datasets(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        assert self.client is not None
        page = await self._remote(self.client.list_datasets, limit=args.limit, starting_after=args.starting_after)
        return page.model_dump(mode="json"), []

    async def _describe_dataset(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        assert self.client is not None
        return (await self._remote(self.client.get_dataset, args.dataset_id)).model_dump(mode="json"), []

    async def _estimate(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        assert self.client is not None
        return (await self._remote(self.client.estimate, args.kind, args.spec)).model_dump(mode="json"), []

    async def _get_job(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        assert self.client is not None
        return (await self._remote(self.client.jobs.get, args.job_id)).model_dump(mode="json"), []

    async def _list_job_files(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        assert self.client is not None
        manifest = await self._remote(self.client.jobs.files, args.job_id)
        links: list[types.ContentBlock] = [
            types.ResourceLink(
                name=f.name,
                uri=f.url,
                mime_type=f.content_type,
                size=f.bytes,
                description=f"sha256 {f.sha256}; URL expires {f.expires_at}",
            )
            for f in manifest.files
        ]
        return manifest.model_dump(mode="json"), links

    async def _get_run(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        assert self.client is not None and tool.kind is not None
        record = await self._remote(self.client.get_run, tool.kind, args.id)
        if record.summary is None:
            job = await self._remote(self.client.jobs.get, record.job_id)
            details: dict[str, Any] = {"job_id": job.id, "state": job.state}
            if job.error is not None:
                details["problem"] = job.error.model_dump(mode="json")
            raise QtError(
                "QT-REMOTE-001",
                f"{tool.kind} {args.id} has no summary: job {job.id} is {job.state}",
                "poll qt_get_job until the state is succeeded" if job.state in ("queued", "running") else None,
                details,
            )
        return record.raw["summary"], []

    async def _explain_metric(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        if args.name not in METRICS:
            raise QtError("QT-USAGE-001", f"unknown metric {args.name!r}")
        return explain(args.name), []

    async def _submit(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        assert self.client is not None and tool.kind is not None
        sub = await self._remote(self.client.submit, tool.kind, args.spec, idempotency_key=args.idempotency_key)
        return sub.job.model_dump(mode="json"), []

    # ---- local tools ------------------------------------------------------------------------------------------

    async def _local_read_summary(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        return await anyio.to_thread.run_sync(self.outputs.read_summary, args.path), []

    async def _local_list_outputs(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        return await anyio.to_thread.run_sync(self.outputs.list_outputs, args.path), []

    async def _local_why(self, tool: ToolDef, args: Any) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        fn = partial(self.outputs.why, args.dataset_path, args.shadows_path, args.shadow_id, self._why)
        return await anyio.to_thread.run_sync(fn), []


def build_server(
    *,
    allow_submit: bool = False,
    local: bool = False,
    client: Client | None = None,
    root: str | os.PathLike[str] | None = None,
    why: Callable[[str], str] = core_why,
) -> QtMcpServer:
    """The configured server (its `.server` is the MCP `Server`)."""
    return QtMcpServer(allow_submit=allow_submit, local=local, client=client, root=root, why=why)


async def run_stdio(app: QtMcpServer) -> None:
    """Serves `app` over stdin/stdout until the client disconnects."""
    async with stdio_server() as (read_stream, write_stream):
        await app.server.run(read_stream, write_stream, app.server.create_initialization_options())


def serve(*, allow_submit: bool = False, local: bool = False) -> None:
    """Runs the stdio server."""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    app = build_server(allow_submit=allow_submit, local=local)
    if allow_submit and app.local_mode:
        log.warning("--allow-submit ignored in local mode (no submit tools without %s)", ENV_API_KEY)
    try:
        anyio.run(run_stdio, app)
    finally:
        if app.client is not None:
            app.client.close()


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m queuetruth.mcp.server [--allow-submit] [--local]`."""
    parser = argparse.ArgumentParser(prog="queuetruth mcp serve", description="queuetruth MCP stdio server")
    parser.add_argument("--allow-submit", action="store_true", help="list the job-submitting tools")
    parser.add_argument("--local", action="store_true", help="only the local read tools (no HTTP API)")
    ns = parser.parse_args(argv)
    serve(allow_submit=ns.allow_submit, local=ns.local)
    return 0


__all__ = [
    "INSTRUCTIONS",
    "LOCAL_INSTRUCTIONS",
    "QtMcpServer",
    "build_server",
    "error_result",
    "instructions_for",
    "main",
    "render_text",
    "run_stdio",
    "serve",
    "trust_lines",
]

if __name__ == "__main__":
    raise SystemExit(main())
