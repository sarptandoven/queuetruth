"""MCP stdio server (`queuetruth mcp serve`, SPEC 4.6) on the official `mcp` SDK 2.x low-level `Server`.

Modes: remote (default; needs `QT_API_KEY`, calls the HTTP API at `QT_API_URL` through `queuetruth.client.Client`)
lists the remote read tools, the submit tools only with `allow_submit`, then the local tools; local (`local=True`
or no API key) lists only the local read tools, which read under `QT_MCP_ROOT` and never write. Tools are listed in
the order of SPEC 4.6 and return `structuredContent` (validated against the tool's output schema) plus a short text
rendering; job files are returned as `resource_link` items, never inline. Failures are tool results with
`isError = true`, a `qt.error` document as `structuredContent` and `"<code>: <message>"` as text. Long jobs: the
installed SDK has no `io.modelcontextprotocol/tasks` extension, so submit tools return the `qt.job` handle for
polling with `qt_get_job`. No tool deletes anything or manages keys.

Zoo runs on the service (`ZOO_TOOLS`, remote mode, listed after the SPEC 4.6 tools): `qt_get_zoo` reads a zoo run's
`qt.zoo_summary`; `qt_submit_zoo` (with `allow_submit`) starts one and returns its `qt.job`.

Run mode (`allow_run=True`, `--allow-run` or `QT_MCP_ALLOW_RUN=1`; either mode): `RUN_TOOLS` are listed after those
and run shadow sampling, replay and zoo on local datasets without the service or Postgres, and `qt_local_why` runs
under the same limits (`queuetruth.mcp.local.LocalRunner`: path sandbox, child process, `RunLimits`). Their listed
input schemas accept only `{"path"}` references and no `out`. Each returns its summary document as
`structuredContent`, the output directory as text and in `_meta` (`queuetruth/out_dir`, `queuetruth/run_id`,
`queuetruth/reused`), and every output file as a `file://` `resource_link`.

Optional tool modules (`OPTIONAL_TOOL_MODULES`, e.g. `queuetruth.mcp.sandbox_tools`): in run mode only (they run
replays, like `RUN_TOOLS`), the stdio server (`serve`, so `queuetruth mcp serve --allow-run` and
`python -m queuetruth.mcp.server --allow-run`) imports each one that is installed and calls its register function with
the `QtMcpServer`. `build_server` registers none unless `tool_modules` names them, so a program that
registers a module itself (`python -m queuetruth.mcp.sandbox_tools`) does not get it twice. A module that is not
installed is skipped; one that fails to import or to register is logged and skipped, and everything its registration
changed (tools, handlers, a `tool_list` override, attributes, the instructions) is rolled back, so an optional module
never stops the server. A module already registered on the server is skipped. `QT_MCP_OPTIONAL_TOOLS=0` turns the
hook off.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import logging
import os
import re
import sys
import typing
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import Any

import anyio
import anyio.to_thread
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field, ValidationError, create_model

from queuetruth._version import __version__
from queuetruth.client import ENV_API_KEY, Client
from queuetruth.errors import QtError
from queuetruth.mcp.local import (
    ENV_ALLOW_RUN,
    MAX_LINKS,
    LocalOutputs,
    LocalRunner,
    RunLimits,
    Sandbox,
    core_why,
    env_flag,
    output_links,
)
from queuetruth.mcp.metrics import METRICS, explain
from queuetruth.mcp.tools import (
    TOOLS,
    Args,
    IdempotencyKey,
    SubmitArgs,
    ToolDef,
    _service_id,
    input_schema,
    output_schema,
)
from queuetruth.schemas import specs as sp

_tool_input_schema, _tool_output_schema = input_schema, output_schema  # `add_tool` has parameters of these names
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


#: Added to the instructions in run mode (it names only the run tools, which run mode lists in both modes).
RUN_INSTRUCTIONS = (
    "Run tools: qt_local_sample_shadows, qt_local_run_replay and qt_local_run_zoo run on local datasets "
    '({"path": ...} references under QT_MCP_ROOT, QT_MCP_DATA_ROOTS or the run root; spec.out null) and write only '
    "under the run root (QT_MCP_RUN_ROOT), at <kind>/<run_id>; the result names the directory, and the same spec "
    "returns the finished run instead of running again. Sample shadows first, then pass the sampled shadows.parquet "
    "to the zoo. qt_local_why runs under the same limits."
)
ENV_OPTIONAL_TOOLS = "QT_MCP_OPTIONAL_TOOLS"
#: Optional tool modules: (module, register function called with the `QtMcpServer`).
OPTIONAL_TOOL_MODULES: tuple[tuple[str, str], ...] = (("queuetruth.mcp.sandbox_tools", "register_sandbox_tools"),)
TOOL_NAME = re.compile(r"[A-Za-z0-9_\-]{1,64}")
#: Remote-mode text naming the zoo tools (added to the instructions of the mode that lists them).
ZOO_INSTRUCTIONS = (
    "Zoo runs: qt_get_zoo reads a zoo run's qt.zoo_summary (the L2 queue models scored against exact truth); "
    "qt_estimate takes kind zoo."
)
ZOO_SUBMIT_INSTRUCTIONS = " qt_submit_zoo starts one (poll qt_get_job, then qt_get_zoo)."
ZooId = _service_id("zoo", "zoo run")


class ZooArgs(Args):
    """`qt_get_zoo`."""

    id: ZooId


class SubmitZooSchema(Args):
    """`qt_submit_zoo`."""

    spec: sp.ZooSpec
    idempotency_key: IdempotencyKey | None = None


#: Service zoo tools (remote mode), listed after the SPEC 4.6 tools; `qt_submit_zoo` only with `allow_submit`.
ZOO_TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        "qt_get_zoo",
        "Get zoo summary",
        "qt.zoo_summary of a finished zoo run (leaderboard of L2 queue models against exact truth); error with the "
        "job state while it runs.",
        ZooArgs,
        "qt.zoo_summary",
        kind="zoo",
    ),
    ToolDef(
        "qt_submit_zoo",
        "Submit zoo run",
        "Starts a zoo job (ZooSpec with dataset_id and shadows upload_id or job_file references, out null) and "
        "returns its qt.job handle; poll qt_get_job, then qt_get_zoo.",
        SubmitArgs,
        "qt.job",
        schema_args=SubmitZooSchema,
        submit=True,
        kind="zoo",
    ),
)


class LocalRunArgs(Args):
    """Run tools at runtime (the spec is validated for the tool's kind by `LocalRunner.prepare`)."""

    spec: dict[str, Any]


def _path_only(annotation: Any) -> Any:
    """`PathRef` (or `PathRef | None`) for a reference annotation (`DatasetRef`, `FileRef`, optional), else as is."""
    args = typing.get_args(annotation)
    if sp.PathRef in args:
        return sp.PathRef | None if type(None) in args else sp.PathRef
    return annotation


def local_spec_model(spec: type[sp.SpecModel]) -> type[sp.SpecModel]:
    """`spec` as run mode accepts it: every reference a `{"path"}` and no `out` (the run root holds the outputs)."""
    fields: dict[str, Any] = {}
    for name, info in spec.model_fields.items():
        if name == "out":
            continue
        ann = _path_only(info.annotation)
        if ann is info.annotation:
            fields[name] = (info.annotation, info)
        else:
            fields[name] = (ann, Field(default=info.default) if not info.is_required() else ...)
    doc = f'{spec.__name__} for run mode: references are {{"path"}} under the allowed roots; no `out`.'
    return create_model(f"Local{spec.__name__}", __base__=sp.SpecModel, __doc__=doc, **fields)


LocalShadowSampleSpec = local_spec_model(sp.ShadowSampleSpec)
LocalReplaySpec = local_spec_model(sp.ReplaySpec)
LocalZooSpec = local_spec_model(sp.ZooSpec)


class LocalSampleSchema(Args):
    """`qt_local_sample_shadows`."""

    spec: LocalShadowSampleSpec  # type: ignore[valid-type]


class LocalReplaySchema(Args):
    """`qt_local_run_replay`."""

    spec: LocalReplaySpec  # type: ignore[valid-type]


class LocalZooSchema(Args):
    """`qt_local_run_zoo`."""

    spec: LocalZooSpec  # type: ignore[valid-type]


#: Run-mode tools, listed after the SPEC 4.6 tools.
RUN_TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        "qt_local_sample_shadows",
        "Sample shadows locally",
        "Seeded random shadow orders on a local dataset (ShadowSampleSpec, out null); writes shadows.parquet under "
        "the run root and returns qt.shadow_sample_summary. Pass the file to qt_local_run_replay or qt_local_run_zoo.",
        LocalRunArgs,
        "qt.shadow_sample_summary",
        schema_args=LocalSampleSchema,
        local=True,
        kind="shadows",
    ),
    ToolDef(
        "qt_local_run_replay",
        "Run replay locally",
        "Exact replay of a local dataset with optional shadows (ReplaySpec, out null) in a sandboxed child process; "
        "outputs under the run root; returns qt.replay_summary.",
        LocalRunArgs,
        "qt.replay_summary",
        schema_args=LocalReplaySchema,
        local=True,
        kind="replay",
    ),
    ToolDef(
        "qt_local_run_zoo",
        "Run model zoo locally",
        "Runs L2 queue models on a local dataset's L2 view and scores them against exact truth (ZooSpec, out null) "
        "in a sandboxed child process; outputs under the run root; returns qt.zoo_summary with the leaderboard.",
        LocalRunArgs,
        "qt.zoo_summary",
        schema_args=LocalZooSchema,
        local=True,
        kind="zoo",
    ),
)
RUN_TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in RUN_TOOLS)


def instructions_for(local_mode: bool, allow_run: bool = False, allow_submit: bool = False) -> str:
    """The server instructions of a mode: they name only tools that mode lists."""
    if local_mode:
        base = LOCAL_INSTRUCTIONS
        if allow_run:
            base = base.replace("read-only tools over", "read tools over").replace(
                "; they never write.", " (they never write) and the run tools below."
            )
    else:
        base = f"{INSTRUCTIONS} {ZOO_INSTRUCTIONS}" + (ZOO_SUBMIT_INSTRUCTIONS if allow_submit else "")
    return f"{base} {RUN_INSTRUCTIONS}" if allow_run else base


#: A handler returns the structured document, extra content blocks and optionally the result's `_meta`.
Handler = Callable[[ToolDef, BaseModel], Awaitable[tuple[Any, ...]]]
#: What `add_tool` accepts: a sync or async callable `(tool, args)` returning the structured document, or the
#: document and extra content blocks.
ToolHandler = Callable[[ToolDef, Any], Any]


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
    return (int(h), h) if h.isdecimal() else (1 << 62, h)


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
    if s == "qt.zoo_summary":
        line = f"zoo {doc['run_id']}: {doc['n_shadows']} shadows x {len(doc['models'])} models"
        if doc.get("n_leaderboard") is not None:
            line += f", {doc['n_leaderboard']} in the leaderboard population"
        return line
    if s == "qt.shadow_sample_summary":
        return f"sampled {doc['n']} shadows over {len(doc.get('by_instrument') or {})} instrument(s)"
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
    return f"{s or 'result'} document"


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
        allow_run: bool = False,
        run_limits: RunLimits | None = None,
        run_root: str | os.PathLike[str] | None = None,
        data_roots: Sequence[str | os.PathLike[str]] | None = None,
        tool_modules: Sequence[tuple[str, str]] = (),
    ) -> None:
        if client is None and not local and os.environ.get(ENV_API_KEY):
            client = Client()
        self.client = client
        self.local_mode = local or client is None
        self.allow_submit = allow_submit and not self.local_mode
        self.allow_run = allow_run
        self.outputs = LocalOutputs(root)
        self.runner: LocalRunner | None = None
        if allow_run:
            sandbox = Sandbox(root, data_roots=data_roots, run_root=run_root)
            self.runner = LocalRunner(sandbox, run_limits or RunLimits.from_env())
        self._why = why
        self.tools: list[ToolDef] = [
            t for t in TOOLS if (t.local or not self.local_mode) and (not t.submit or self.allow_submit)
        ]
        if not self.local_mode:
            self.tools.extend(t for t in ZOO_TOOLS if not t.submit or self.allow_submit)
        if allow_run:
            self.tools.extend(RUN_TOOLS)
        self._by_name = {t.name: t for t in self.tools}
        #: Tools that write (annotated `readOnlyHint: false`); custom schemas of added tools: name -> (in, out).
        self._writes: set[str] = set(RUN_TOOL_NAMES) if allow_run else set()
        self._schemas: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
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
            "qt_get_zoo": self._get_run,
            "qt_explain_metric": self._explain_metric,
            "qt_submit_replay": self._submit,
            "qt_submit_calibration": self._submit,
            "qt_submit_scorecard": self._submit,
            "qt_submit_report": self._submit,
            "qt_submit_zoo": self._submit,
            "qt_local_read_summary": self._local_read_summary,
            "qt_local_list_outputs": self._local_list_outputs,
            "qt_local_why": self._local_why,
            "qt_local_sample_shadows": self._local_run,
            "qt_local_run_replay": self._local_run,
            "qt_local_run_zoo": self._local_run,
        }
        self.server: Server[Any] = Server(
            SERVER_NAME,
            version=__version__,
            title="queuetruth",
            instructions=instructions_for(self.local_mode, allow_run, self.allow_submit),
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )
        #: Optional tool modules registered on this server (`register_optional_tools` skips them on a second call).
        self.optional_modules: list[str] = []
        register_optional_tools(self, tool_modules)

    # ---- registration of added tools ----------------------------------------------------------------------------

    def add_tool(
        self,
        tool: ToolDef,
        handler: ToolHandler,
        *,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        read_only: bool = True,
    ) -> None:
        """Lists `tool` after the built-in tools. `handler(tool, args)` (sync or async; `args` is the validated
        `tool.args` model) returns the structured document, or `(document, extra content blocks)`; a `QtError` it
        raises becomes an error result. The schemas default to those of `tool` (`schema_args or args`, and the
        document schema named by `tool.output`); `output_schema` is required when `tool.output` is not a `qt.*`
        document. No added tool is destructive."""
        if not TOOL_NAME.fullmatch(tool.name):
            raise ValueError(f"invalid tool name {tool.name!r}")
        if tool.name in self._by_name:
            raise ValueError(f"tool {tool.name!r} is already registered")
        if not callable(handler):
            raise TypeError(f"handler of {tool.name!r} is not callable")
        in_schema = input_schema if input_schema is not None else _tool_input_schema(tool)
        out_schema = output_schema
        if out_schema is None:
            try:
                out_schema = _tool_output_schema(tool)
            except KeyError:
                raise ValueError(f"tool {tool.name!r}: output {tool.output!r} needs an output_schema") from None
        self.tools.append(tool)
        self._by_name[tool.name] = tool
        self._schemas[tool.name] = (in_schema, out_schema)
        if not read_only:
            self._writes.add(tool.name)
        self._handlers[tool.name] = _wrap_handler(handler)

    def _snapshot(self) -> dict[str, Any]:
        """What a registration may change: the instance attributes (a `tool_list` override, a registry such as
        `sandbox_tools`), the tool containers' contents and the server instructions."""
        return {
            "attrs": dict(vars(self)),
            "tools": list(self.tools),
            "by_name": dict(self._by_name),
            "handlers": dict(self._handlers),
            "schemas": dict(self._schemas),
            "writes": set(self._writes),
            "instructions": self.server.instructions,
        }

    def _restore(self, snap: dict[str, Any]) -> None:
        for name in [n for n in vars(self) if n not in snap["attrs"]]:
            delattr(self, name)
        for name, value in snap["attrs"].items():
            setattr(self, name, value)
        self.tools[:] = snap["tools"]
        for live, saved in ((self._by_name, snap["by_name"]), (self._handlers, snap["handlers"])):
            live.clear()
            live.update(saved)
        self._schemas.clear()
        self._schemas.update(snap["schemas"])
        self._writes.clear()
        self._writes.update(snap["writes"])
        self.server.instructions = snap["instructions"]

    def _check_registry(self) -> None:
        """Every listed tool is listed once, is known by name and has a handler, and every known tool is listed
        (`ValueError` otherwise; it also raises when a schema cannot be resolved)."""
        listed = [t.name for t in self.tool_list()]
        dupes = sorted({n for n in listed if listed.count(n) > 1})
        unhandled = [n for n in listed if n not in self._handlers or n not in self._by_name]
        hidden = sorted(set(self._by_name) - set(listed))
        if dupes or unhandled or hidden:
            raise ValueError(f"tools listed twice {dupes}, without a handler {unhandled}, or not listed {hidden}")

    # ---- protocol ---------------------------------------------------------------------------------------------

    def tool_list(self) -> list[types.Tool]:
        """The listed tools, in SPEC 4.6 order."""
        out = []
        for t in self.tools:
            schemas = self._schemas.get(t.name) or (input_schema(t), output_schema(t))
            out.append(
                types.Tool(
                    name=t.name,
                    title=t.title,
                    description=t.description,
                    input_schema=schemas[0],
                    output_schema=schemas[1],
                    annotations=types.ToolAnnotations(
                        title=t.title,
                        read_only_hint=not (t.submit or t.name in self._writes),
                        destructive_hint=False,
                        idempotent_hint=not t.submit,
                        open_world_hint=False,
                    ),
                )
            )
        return out

    async def _list_tools(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=self.tool_list())

    async def _call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        tool = self._by_name.get(params.name)
        if tool is None or params.name not in self._handlers:
            raise MCPError(types.INVALID_PARAMS, f"unknown tool {params.name!r}")
        return await self.call(tool, params.arguments or {})

    async def call(self, tool: ToolDef, arguments: dict[str, Any]) -> types.CallToolResult:
        """Validates arguments, runs the handler and wraps the result or error."""
        try:
            args = tool.args.model_validate(arguments)
        except ValidationError as e:
            return error_result(QtError("QT-USAGE-001", f"{tool.name}: invalid arguments: {_first_error(e)}"))
        try:
            out = await self._handlers[tool.name](tool, args)
        except QtError as e:
            return error_result(e)
        except Exception as e:  # a tool failure must not end the session
            log.exception("tool %s failed", tool.name)
            return error_result(QtError("QT-ENGINE-001", f"{tool.name}: internal error: {type(e).__name__}: {e}"))
        doc, extra = out[0], out[1]
        meta = out[2] if len(out) > 2 else None
        content = [types.TextContent(text=render_text(doc)), *extra]
        return types.CallToolResult(content=content, structured_content=doc, meta=meta)

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
        if self.runner is not None and self._why is core_why:  # run mode: limited child, allowed roots
            return await self.runner.why(args.dataset_path, args.shadows_path, args.shadow_id), []
        fn = partial(self.outputs.why, args.dataset_path, args.shadows_path, args.shadow_id, self._why)
        return await anyio.to_thread.run_sync(fn), []

    async def _local_run(
        self, tool: ToolDef, args: Any
    ) -> tuple[dict[str, Any], list[types.ContentBlock], dict[str, Any]]:
        assert self.runner is not None and tool.kind is not None
        res = await self.runner.run(tool.kind, args.spec)
        files, total = await anyio.to_thread.run_sync(output_links, res.out, MAX_LINKS)
        state = "finished run reused" if res.cached else "new run"
        note = f"outputs: {self.runner.sandbox.rel(res.out)} ({state}; {total} file(s))"
        if total > len(files):
            note += f"; {len(files)} linked, list the rest with qt_local_list_outputs or the file system"
        links: list[types.ContentBlock] = [
            types.ResourceLink(name=name, uri=path.as_uri(), mime_type=_mime(name), size=size)
            for name, path, size in files
        ]
        meta = {"queuetruth/out_dir": str(res.out), "queuetruth/run_id": res.out.name, "queuetruth/reused": res.cached}
        return res.summary, [types.TextContent(text=note), *links], meta


_MIME = {
    ".parquet": "application/vnd.apache.parquet",
    ".json": "application/json",
    ".html": "text/html",
    ".csv": "text/csv",
    ".npz": "application/octet-stream",
    ".py": "text/x-python",
    ".txt": "text/plain",
}


def _mime(name: str) -> str:
    return _MIME.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def _wrap_handler(fn: ToolHandler) -> Handler:
    async def handler(tool: ToolDef, args: BaseModel) -> tuple[dict[str, Any], list[types.ContentBlock]]:
        if inspect.iscoroutinefunction(fn):
            out = await fn(tool, args)
        else:
            out = await anyio.to_thread.run_sync(fn, tool, args)
            if inspect.isawaitable(out):
                out = await out
        if isinstance(out, tuple) and len(out) == 2 and isinstance(out[0], dict):
            return out[0], list(out[1])
        if isinstance(out, dict):
            return out, []
        raise QtError("QT-ENGINE-001", f"{tool.name}: the handler returned {type(out).__name__}, not a document")

    return handler


def default_tool_modules(allow_run: bool = True) -> tuple[tuple[str, str], ...]:
    """The optional tool modules the stdio server registers: `OPTIONAL_TOOL_MODULES` in run mode unless
    `QT_MCP_OPTIONAL_TOOLS=0` (`QT-CONFIG-001` for a value that is not a boolean). They run replays (the agent
    sandbox), so like `RUN_TOOLS` they are never listed without run mode: `serve` passes its `allow_run`."""
    on = env_flag(ENV_OPTIONAL_TOOLS, default=True)  # validated even when run mode is off
    return OPTIONAL_TOOL_MODULES if (on and allow_run) else ()


def register_optional_tools(app: QtMcpServer, modules: Sequence[tuple[str, str]] = OPTIONAL_TOOL_MODULES) -> list[str]:
    """Imports each `(module, function)` that is installed and calls `function(app)`; returns the modules that
    registered in this call (also appended to `app.optional_modules`). A module already in `app.optional_modules` is
    skipped. A missing module is skipped quietly; an import or registration failure is logged and skipped, and
    everything the failed registration changed is rolled back (`QtMcpServer._snapshot`)."""
    done: list[str] = []
    for module, func in modules:
        if module in app.optional_modules:
            log.debug("optional tool module %s is already registered", module)
            continue
        try:
            mod = importlib.import_module(module)
        except ModuleNotFoundError as e:
            if e.name is not None and (module == e.name or module.startswith(e.name + ".")):
                log.debug("optional tool module %s is not installed", module)
            else:
                log.warning("optional tool module %s not loaded: %s", module, e)
            continue
        except Exception as e:
            log.warning("optional tool module %s not loaded: %s: %s", module, type(e).__name__, e)
            continue
        register = getattr(mod, func, None)
        if not callable(register):
            log.warning("optional tool module %s has no callable %s; skipped", module, func)
            continue
        snap = app._snapshot()
        try:
            register(app)
            app._check_registry()
        except Exception:
            log.exception("optional tool module %s failed to register; skipped", module)
            app._restore(snap)
            continue
        app.optional_modules.append(module)
        done.append(module)
    return done


def build_server(
    *,
    allow_submit: bool = False,
    local: bool = False,
    client: Client | None = None,
    root: str | os.PathLike[str] | None = None,
    why: Callable[[str], str] = core_why,
    allow_run: bool = False,
    run_limits: RunLimits | None = None,
    run_root: str | os.PathLike[str] | None = None,
    data_roots: Sequence[str | os.PathLike[str]] | None = None,
    tool_modules: Sequence[tuple[str, str]] = (),
) -> QtMcpServer:
    """The configured server (its `.server` is the MCP `Server`), with the optional tool modules `tool_modules`
    (none by default; `serve` passes `default_tool_modules()`)."""
    return QtMcpServer(
        allow_submit=allow_submit,
        local=local,
        client=client,
        root=root,
        why=why,
        allow_run=allow_run,
        run_limits=run_limits,
        run_root=run_root,
        data_roots=data_roots,
        tool_modules=tool_modules,
    )


async def run_stdio(app: QtMcpServer) -> None:
    """Serves `app` over stdin/stdout until the client disconnects."""
    async with stdio_server() as (read_stream, write_stream):
        await app.server.run(read_stream, write_stream, app.server.create_initialization_options())


def serve(*, allow_submit: bool = False, local: bool = False, allow_run: bool | None = None) -> None:
    """Runs the stdio server; `allow_run` None reads `QT_MCP_ALLOW_RUN`."""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    run = env_flag(ENV_ALLOW_RUN) if allow_run is None else allow_run
    app = build_server(
        allow_submit=allow_submit,
        local=local,
        allow_run=run,
        tool_modules=default_tool_modules(allow_run=run),
    )
    if allow_submit and app.local_mode:
        log.warning("--allow-submit ignored in local mode (no submit tools without %s)", ENV_API_KEY)
    try:
        anyio.run(run_stdio, app)
    finally:
        if app.client is not None:
            app.client.close()


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m queuetruth.mcp.server [--allow-submit] [--local] [--allow-run]`."""
    parser = argparse.ArgumentParser(prog="queuetruth mcp serve", description="queuetruth MCP stdio server")
    parser.add_argument("--allow-submit", action="store_true", help="list the job-submitting tools")
    parser.add_argument("--local", action="store_true", help="only the local read tools (no HTTP API)")
    parser.add_argument(
        "--allow-run",
        action="store_true",
        default=None,
        help=f"list the local run tools (sandboxed; writes under the run root); default from {ENV_ALLOW_RUN}",
    )
    ns = parser.parse_args(argv)
    serve(allow_submit=ns.allow_submit, local=ns.local, allow_run=ns.allow_run)
    return 0


__all__ = [
    "INSTRUCTIONS",
    "LOCAL_INSTRUCTIONS",
    "OPTIONAL_TOOL_MODULES",
    "RUN_INSTRUCTIONS",
    "RUN_TOOLS",
    "RUN_TOOL_NAMES",
    "ZOO_TOOLS",
    "QtMcpServer",
    "build_server",
    "default_tool_modules",
    "error_result",
    "instructions_for",
    "local_spec_model",
    "main",
    "register_optional_tools",
    "render_text",
    "run_stdio",
    "serve",
    "trust_lines",
]

if __name__ == "__main__":
    raise SystemExit(main())
