"""MCP tools of the agent sandbox: `register_sandbox_tools(app)` adds them to a `queuetruth.mcp.server.QtMcpServer`.

Tools, in listing order (input -> output document):

- `qt_sandbox_start` {dataset_path, instrument_id or symbol, session_date, start, end, latency, jitter, feed_latency,
  ...} -> `qt.sandbox_session`: opens an episode (`queuetruth.sandbox.Sandbox`);
- `qt_sandbox_observe` {session_id} -> `qt.sandbox_observation`, read-only;
- `qt_sandbox_submit` {session_id, side, qty, type, price, peg_offset_ticks, tif, client_order_id} ->
  `qt.sandbox_ack`; a rejected order is an error result (`QT-CONFIG-001` with `details.reason`);
- `qt_sandbox_cancel` {session_id, order_id, qty} -> `qt.sandbox_ack`; `qty` cancels part of it (priority kept);
- `qt_sandbox_advance` {session_id, by or to} -> `qt.sandbox_observation`; the clock only moves forward;
- `qt_sandbox_finish` {session_id, detail, include_log} -> `qt.sandbox_finish`: the scorecard's aggregates and
  certificate without its per-order and per-fill arrays (a result of a few kilobytes however long the episode), the
  full scorecard inline with `detail = "full"`, the committed order log as an embedded JSON resource with
  `include_log`; on a server with an output directory (`out_dir`, `--out`) the full scorecard, order log and replay
  tables are written to `<out_dir>/<session_id>/` and linked;
- `qt_sandbox_list` {} -> `qt.sandbox_session_list`: the open sessions (an agent that lost its session id finds it
  here), read-only;
- `qt_sandbox_close` {session_id} -> `qt.sandbox_session_list`: ends a session without a scorecard.

Datasets are resolved against the server's root (`QT_MCP_ROOT`) like the local tools: every file and directory the
session reads is checked before it is read (after resolving symlinks it must stay inside the root, else
`QT-CONFIG-001`), and the partition's files are opened only through the real paths checked at start. The tools write
nothing under the root; each session keeps its tape and replay outputs in a private temporary directory, removed when
the session finishes, fails to finish, is closed or evicted, or the server exits. Sessions are bounded
(`max_sessions`); when the limit is reached, starting a session first evicts the sessions idle for longer than
`idle_timeout_s`. Results carry the document as `structuredContent` (validated by the declared output schema) and,
after the server's headline, a plain-text rendering with decimal prices and local times for agents that read text.
`python -m queuetruth.mcp.sandbox_tools [--root DIR] [--out DIR]` serves the local tools plus the sandbox tools over
stdio.
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import secrets
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
import anyio.to_thread
import mcp.types as types
from pydantic import BaseModel, Field

from queuetruth.errors import QtError
from queuetruth.mcp.local import ENV_ROOT, LocalOutputs
from queuetruth.mcp.tools import Args, LocalPath, ToolDef
from queuetruth.sandbox import render
from queuetruth.sandbox._util import local_time
from queuetruth.sandbox.models import (
    ClientOrderId,
    FeeSchedule,
    OrderId,
    SandboxAck,
    SandboxFinish,
    SandboxObservation,
    SandboxSession,
    SandboxSessionList,
)
from queuetruth.sandbox.scorecard import finish_summary
from queuetruth.sandbox.session import Sandbox
from queuetruth.schemas.export import DRAFT, json_schema
from queuetruth.schemas.specs import DateStr

log = logging.getLogger("queuetruth.mcp.sandbox")

SESSION_PATTERN = r"^sbx_[0-9a-f]{16}$"
SessionId = Annotated[str, Field(pattern=SESSION_PATTERN, description="session id returned by qt_sandbox_start")]
DurationArg = Annotated[
    Annotated[int, Field(ge=0, strict=True)] | Annotated[str, Field(pattern=r"^\s*[0-9]+\s*(ns|us|ms|s|m|h)\s*$")],
    Field(description="integer ns, or a number with a unit: '250us', '100ms', '1s', '5m'"),
]
TimeArg = Annotated[
    Annotated[int, Field(strict=True)]
    | Annotated[str, Field(pattern=r"^\s*[0-9]{1,2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,9})?)?\s*$")],
    Field(description="local time of day of the session ('10:30', '10:30:05.250') or absolute ns since the epoch"),
]
DecimalPrice = Annotated[
    str, Field(pattern=r"^\s*-?[0-9]+(\.[0-9]+)?\s*$", description="decimal price on the tick grid, e.g. '291.65'")
]
QtyArg = Annotated[
    Annotated[int, Field(ge=1, strict=True)] | Annotated[str, Field(pattern=r"^\s*[0-9]+(\.[0-9]+)?\s*$")],
    Field(
        description="size: an integer in the instrument's size units (shares, contracts), or a decimal string such "
        "as '0.25' for instruments with fractional sizes (size_exp < 0)"
    ),
]
DEFAULT_MAX_SESSIONS = 4
#: Sessions idle this long are evicted when a new session needs their slot.
DEFAULT_IDLE_TIMEOUT_S = 3600.0
SANDBOX_TOOL_NAMES = (
    "qt_sandbox_start",
    "qt_sandbox_observe",
    "qt_sandbox_submit",
    "qt_sandbox_cancel",
    "qt_sandbox_advance",
    "qt_sandbox_finish",
    "qt_sandbox_list",
    "qt_sandbox_close",
)
SANDBOX_INSTRUCTIONS = (
    "Sandbox tools (qt_sandbox_*): qt_sandbox_start opens a trading episode on an order-level dataset under "
    "QT_MCP_ROOT in which your orders fill exactly as the recorded FIFO queue would have filled them; then "
    "alternate qt_sandbox_submit / qt_sandbox_cancel with qt_sandbox_advance (your clock only moves forward, you "
    "only see data up to it, and you act only at the times you advance to), use qt_sandbox_observe to re-read the "
    "state, and end with qt_sandbox_finish for the scorecard summary and its certificate. qt_sandbox_list shows the "
    "open sessions, qt_sandbox_close abandons one. They write only private scratch files."
)


class StartArgs(Args):
    """`qt_sandbox_start`."""

    dataset_path: LocalPath
    instrument_id: Annotated[int, Field(ge=0)] | None = None
    symbol: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    session_date: DateStr | None = None
    start: TimeArg | None = None
    end: TimeArg | None = None
    latency: DurationArg = 0
    jitter: DurationArg = 0
    feed_latency: DurationArg = 0
    depth_levels: Annotated[int, Field(ge=1, le=50)] = 5
    max_trades: Annotated[int, Field(ge=0, le=1000)] = 20
    fees: FeeSchedule | None = None
    markout_horizons: Annotated[list[DurationArg], Field(min_length=1, max_length=16)] | None = None
    market_collar_bps: Annotated[float, Field(gt=0, le=10_000)] | None = None
    max_order_qty: Annotated[int, Field(ge=1)] | None = None
    max_position: Annotated[int, Field(ge=0)] | None = None
    max_open_orders: Annotated[int, Field(ge=1, le=10_000)] | None = None
    self_trade_prevention: Literal["reject_new", "off"] = "reject_new"
    hidden_print_policy: Literal["ignore", "behind", "behind_and_through"] | None = None
    seed: Annotated[int, Field(ge=0)] = 0


class SessionArgs(Args):
    """`qt_sandbox_observe`, `qt_sandbox_close`."""

    session_id: SessionId


class SubmitArgs(Args):
    """`qt_sandbox_submit`."""

    session_id: SessionId
    side: Literal["buy", "sell"]
    qty: QtyArg
    type: Literal["limit", "market"] = "limit"
    price: DecimalPrice | None = None
    peg_offset_ticks: (
        Annotated[
            int, Field(ge=-1000, le=1000, description="price at arrival = same-side best + k ticks (buy) / - k (sell)")
        ]
        | None
    ) = None
    tif: Literal["day", "ioc"] = "day"
    client_order_id: ClientOrderId | None = None


class CancelArgs(Args):
    """`qt_sandbox_cancel`."""

    session_id: SessionId
    order_id: OrderId
    qty: QtyArg | None = Field(default=None, description="cancel only this much of the order (priority kept)")


class AdvanceArgs(Args):
    """`qt_sandbox_advance`."""

    session_id: SessionId
    by: DurationArg | None = None
    to: TimeArg | None = None


class FinishArgs(Args):
    """`qt_sandbox_finish`."""

    session_id: SessionId
    detail: Literal["summary", "full"] = Field(
        default="summary",
        description="summary: aggregates and certificate only; full: also the whole scorecard (every order and fill)",
    )
    include_log: bool = Field(default=False, description="also return the committed order log (it grows per step)")


class ListArgs(Args):
    """`qt_sandbox_list`."""


#: The sandbox tools, in listing order, with their output document model.
SANDBOX_TOOLS: tuple[tuple[ToolDef, type[BaseModel]], ...] = (
    (
        ToolDef(
            "qt_sandbox_start",
            "Start sandbox episode",
            "Opens a trading episode on one instrument-day of a local order-level dataset (path under QT_MCP_ROOT): "
            "your orders become shadow orders in the real recorded FIFO queue and fill exactly as it would have "
            "filled them, with order-entry latency and no look-ahead. Returns the session id, the instrument, the "
            "rules and the first observation.",
            StartArgs,
            "qt.sandbox_session",
        ),
        SandboxSession,
    ),
    (
        ToolDef(
            "qt_sandbox_observe",
            "Observe sandbox",
            "The market and your orders at your clock: best quotes and depth, prints since the last step, your "
            "fills, queue positions, position and PnL. Read-only.",
            SessionArgs,
            "qt.sandbox_observation",
        ),
        SandboxObservation,
    ),
    (
        ToolDef(
            "qt_sandbox_submit",
            "Submit sandbox order",
            "Sends an order at your clock: a limit order (price as a decimal string on the tick grid, or "
            "peg_offset_ticks to price it at the touch on arrival; tif day rests, ioc takes what is there) or a "
            "market order (IOC at a collar). It arrives after the session latency and is confirmed at the next "
            "market event. A rejected order is an error result (QT-CONFIG-001, details.reason).",
            SubmitArgs,
            "qt.sandbox_ack",
        ),
        SandboxAck,
    ),
    (
        ToolDef(
            "qt_sandbox_cancel",
            "Cancel sandbox order",
            "Cancels an open day order, or only qty of it (the rest keeps its queue priority). Takes effect after "
            "the latency; fills until then stand.",
            CancelArgs,
            "qt.sandbox_ack",
        ),
        SandboxAck,
    ),
    (
        ToolDef(
            "qt_sandbox_advance",
            "Advance sandbox clock",
            "Moves your clock forward by a duration ('500ms', '5s') or to a local time ('10:31:05'), never past the "
            "episode end, and returns the new observation with the fills, queue positions and markouts it brought. "
            "You act only at the times you advance to: smaller steps react sooner and cost more.",
            AdvanceArgs,
            "qt.sandbox_observation",
        ),
        SandboxObservation,
    ),
    (
        ToolDef(
            "qt_sandbox_finish",
            "Finish sandbox episode",
            "Ends the episode: cancels open orders, runs one exact replay of every order you sent, and returns the "
            "scorecard summary (PnL after fees, fill rates, markouts, adverse selection, the proof that it equals "
            "what you were shown, the certificate). detail='full' adds every order and fill, include_log the "
            "committed order log.",
            FinishArgs,
            "qt.sandbox_finish",
        ),
        SandboxFinish,
    ),
    (
        ToolDef(
            "qt_sandbox_list",
            "List sandbox sessions",
            "The open sandbox sessions of this server (id, instrument-day, clock, orders, idle time) and its limits. "
            "Read-only.",
            ListArgs,
            "qt.sandbox_session_list",
        ),
        SandboxSessionList,
    ),
    (
        ToolDef(
            "qt_sandbox_close",
            "Close sandbox session",
            "Ends a sandbox session without a scorecard and frees its slot; returns the sessions still open.",
            SessionArgs,
            "qt.sandbox_session_list",
        ),
        SandboxSessionList,
    ),
)

_READ_ONLY = {"qt_sandbox_observe", "qt_sandbox_list"}
_DESTRUCTIVE = {"qt_sandbox_close"}
Result = tuple[dict[str, Any], list[types.ContentBlock]]


class SandboxTools:
    """Session registry and handlers of the sandbox tools."""

    def __init__(
        self,
        *,
        root: str | os.PathLike[str] | None = None,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        workdir: str | os.PathLike[str] | None = None,
        out_dir: str | os.PathLike[str] | None = None,
        idle_timeout_s: float | None = DEFAULT_IDLE_TIMEOUT_S,
    ) -> None:
        if max_sessions < 1:
            raise QtError("QT-CONFIG-001", f"max_sessions must be >= 1 (got {max_sessions})")
        if idle_timeout_s is not None and idle_timeout_s < 0:
            raise QtError("QT-CONFIG-001", f"idle_timeout_s must be >= 0 (got {idle_timeout_s})")
        self.outputs = LocalOutputs(root)
        self.max_sessions = max_sessions
        self.workdir = workdir
        self.idle_timeout_s = idle_timeout_s
        self.out_dir: Path | None = None
        if out_dir is not None:
            out = Path(out_dir).expanduser().resolve()
            if self.outputs.inside(out) is not None:
                raise QtError(
                    "QT-CONFIG-001",
                    f"the sandbox output directory {out} is inside {ENV_ROOT} ({self.outputs.root})",
                    "the tools write nothing under the dataset root: choose a directory outside it",
                )
            out.mkdir(parents=True, exist_ok=True)
            self.out_dir = out
        self.sessions: dict[str, Sandbox] = {}
        self._last_used: dict[str, float] = {}
        self._lock = threading.Lock()
        self._schemas = {t.name: {"$schema": DRAFT, **t.args.model_json_schema()} for t, _ in SANDBOX_TOOLS}
        self._outputs = {t.name: json_schema(t.output, model) for t, model in SANDBOX_TOOLS}
        self.handlers: dict[str, Callable[[ToolDef, Any], Awaitable[Result]]] = {
            "qt_sandbox_start": self._start,
            "qt_sandbox_observe": self._observe,
            "qt_sandbox_submit": self._submit,
            "qt_sandbox_cancel": self._cancel,
            "qt_sandbox_advance": self._advance,
            "qt_sandbox_finish": self._finish,
            "qt_sandbox_list": self._list,
            "qt_sandbox_close": self._close,
        }
        atexit.register(self.close_all)

    # ---- listing ------------------------------------------------------------------------------------------------

    @property
    def tool_defs(self) -> list[ToolDef]:
        """The tool definitions in listing order."""
        return [t for t, _ in SANDBOX_TOOLS]

    def tool_list(self) -> list[types.Tool]:
        """The MCP tool entries with input and output schemas and annotations."""
        return [
            types.Tool(
                name=t.name,
                title=t.title,
                description=t.description,
                input_schema=self._schemas[t.name],
                output_schema=self._outputs[t.name],
                annotations=types.ToolAnnotations(
                    title=t.title,
                    read_only_hint=t.name in _READ_ONLY,
                    destructive_hint=t.name in _DESTRUCTIVE,
                    idempotent_hint=t.name in _READ_ONLY,
                    open_world_hint=False,
                ),
            )
            for t in self.tool_defs
        ]

    # ---- sessions -----------------------------------------------------------------------------------------------

    def _get(self, session_id: str) -> Sandbox:
        with self._lock:
            sb = self.sessions.get(session_id)
            if sb is not None:
                self._last_used[session_id] = time.monotonic()
        if sb is None:
            raise QtError(
                "QT-USAGE-001",
                f"unknown or finished sandbox session {session_id!r}",
                "start one with qt_sandbox_start (qt_sandbox_list shows the open ones)",
            )
        return sb

    def _pop(self, session_id: str) -> Sandbox | None:
        with self._lock:
            self._last_used.pop(session_id, None)
            return self.sessions.pop(session_id, None)

    def _item(self, session_id: str, sb: Sandbox, now: float) -> dict[str, Any]:
        i = sb.instrument
        return {
            "session_id": session_id,
            "instrument_id": i["instrument_id"],
            "symbol": i["symbol"],
            "session_date": i["session_date"],
            "clock_ns": sb.clock_ns,
            "time_local": local_time(sb.clock_ns, i["midnight_utc_ns"]),
            "step": sb.step,
            "n_orders": len(sb.orders),
            "n_open": sum(1 for o in sb.orders if not o.final),
            "idle_s": round(max(0.0, now - self._last_used.get(session_id, now)), 3),
        }

    def list_doc(self, closed: dict[str, Any] | None = None) -> dict[str, Any]:
        """The `qt.sandbox_session_list` document of the open sessions (and of one just closed)."""
        now = time.monotonic()
        with self._lock:
            items = [self._item(sid, sb, now) for sid, sb in self.sessions.items()]
        return SandboxSessionList.model_validate(
            {
                "schema": "qt.sandbox_session_list",
                "schema_version": 1,
                "sessions": items,
                "max_sessions": self.max_sessions,
                "idle_timeout_s": self.idle_timeout_s,
                "closed": closed,
            }
        ).model_dump(mode="json")

    def _evict_idle(self) -> None:
        """Closes the sessions idle for longer than `idle_timeout_s` (called when a new session needs a slot)."""
        if self.idle_timeout_s is None:
            return
        now = time.monotonic()
        with self._lock:
            stale = [sid for sid in self.sessions if now - self._last_used.get(sid, now) > self.idle_timeout_s]
            gone = [(sid, self.sessions.pop(sid)) for sid in stale]
            for sid in stale:
                self._last_used.pop(sid, None)
        for sid, sb in gone:
            log.warning("evicting sandbox session %s: idle for more than %s s", sid, self.idle_timeout_s)
            sb.close()

    def close_all(self) -> None:
        """Closes every open session (scratch files removed)."""
        with self._lock:
            sessions, self.sessions = list(self.sessions.values()), {}
            self._last_used = {}
        for sb in sessions:
            try:
                sb.close()
            except Exception:  # cleanup at exit must not raise
                log.exception("closing a sandbox session failed")

    @staticmethod
    async def _run(fn: Callable[[], Any]) -> Any:
        return await anyio.to_thread.run_sync(fn)

    # ---- handlers -----------------------------------------------------------------------------------------------

    async def _start(self, tool: ToolDef, args: StartArgs) -> Result:
        if len(self.sessions) >= self.max_sessions:
            self._evict_idle()
        with self._lock:
            if len(self.sessions) >= self.max_sessions:
                raise QtError(
                    "QT-USAGE-001",
                    f"{len(self.sessions)} sandbox sessions are open (max {self.max_sessions})",
                    "finish one with qt_sandbox_finish or close one with qt_sandbox_close first",
                )
        path = self.outputs.resolve(args.dataset_path)
        spec: dict[str, Any] = {
            "dataset": {"path": str(path)},
            **args.model_dump(mode="json", exclude_none=True, exclude={"dataset_path"}),
        }
        session_id = "sbx_" + secrets.token_hex(8)
        sb = await self._run(
            partial(Sandbox.open, spec, session_id=session_id, workdir=self.workdir, contain=self.outputs.contain)
        )
        with self._lock:
            if len(self.sessions) >= self.max_sessions:
                sb.close()
                raise QtError(
                    "QT-USAGE-001", f"{len(self.sessions)} sandbox sessions are open (max {self.max_sessions})"
                )
            self.sessions[session_id] = sb
            self._last_used[session_id] = time.monotonic()
        doc = sb.session.model_dump(mode="json")
        return doc, [types.TextContent(text=render.session_text(doc))]

    async def _observe(self, tool: ToolDef, args: SessionArgs) -> Result:
        sb = self._get(args.session_id)
        doc = (await self._run(sb.observe)).model_dump(mode="json")
        return doc, [types.TextContent(text=render.observation_text(doc, sb.instrument, sb.end_ns))]

    async def _submit(self, tool: ToolDef, args: SubmitArgs) -> Result:
        sb = self._get(args.session_id)
        fn = partial(
            sb.submit,
            args.side,
            args.qty,
            type=args.type,
            price=None if args.price is None else args.price.strip(),
            peg_offset_ticks=args.peg_offset_ticks,
            tif=args.tif,
            client_order_id=args.client_order_id,
        )
        doc = (await self._run(fn)).model_dump(mode="json")
        return doc, [types.TextContent(text=render.ack_text(doc, sb.instrument))]

    async def _cancel(self, tool: ToolDef, args: CancelArgs) -> Result:
        sb = self._get(args.session_id)
        doc = (await self._run(partial(sb.cancel, args.order_id, args.qty))).model_dump(mode="json")
        return doc, [types.TextContent(text=render.ack_text(doc, sb.instrument))]

    async def _advance(self, tool: ToolDef, args: AdvanceArgs) -> Result:
        sb = self._get(args.session_id)
        by = args.by.strip() if isinstance(args.by, str) else args.by
        to = args.to.strip() if isinstance(args.to, str) else args.to
        doc = (await self._run(partial(sb.advance, by=by, to=to))).model_dump(mode="json")
        return doc, [types.TextContent(text=render.observation_text(doc, sb.instrument, sb.end_ns))]

    async def _finish(self, tool: ToolDef, args: FinishArgs) -> Result:
        sb = self._get(args.session_id)
        out = None if self.out_dir is None else self.out_dir / args.session_id
        try:
            card = await self._run(partial(sb.finish, out=out))
        except BaseException:
            sb.close()  # a failed finish leaves no usable session: free its slot and scratch files
            raise
        finally:
            self._pop(args.session_id)
        doc = finish_summary(
            card, session_id=args.session_id, out_dir=None if out is None else str(out), detail=args.detail
        ).model_dump(mode="json")
        extra: list[types.ContentBlock] = [types.TextContent(text=render.scorecard_text(doc))]
        if out is not None:
            for name, mime in (("scorecard.json", "application/json"), ("order_log.json", "application/json")):
                p = out / name
                extra.append(types.ResourceLink(name=name, uri=p.as_uri(), mime_type=mime, size=p.stat().st_size))
        if args.include_log:
            log_doc = sb.log.document().model_dump(mode="json")
            extra.append(
                types.EmbeddedResource(
                    resource=types.TextResourceContents(
                        uri=f"queuetruth://sandbox/{args.session_id}/order_log.json",
                        mime_type="application/json",
                        text=json.dumps(log_doc, separators=(",", ":")),
                    )
                )
            )
        return doc, extra

    async def _list(self, tool: ToolDef, args: ListArgs) -> Result:
        doc = self.list_doc()
        n = len(doc["sessions"])
        lines = [f"{n} open sandbox session(s) of at most {doc['max_sessions']}"]
        lines += [
            f"{s['session_id']}: {s['symbol']} {s['session_date']} at {s['time_local']}, step {s['step']}, "
            f"{s['n_open']} open of {s['n_orders']} orders, idle {s['idle_s']:.0f} s"
            for s in doc["sessions"]
        ]
        return doc, [types.TextContent(text="\n".join(lines))]

    async def _close(self, tool: ToolDef, args: SessionArgs) -> Result:
        sb = self._get(args.session_id)
        item = self._item(args.session_id, sb, time.monotonic())
        self._pop(args.session_id)
        await self._run(sb.close)
        doc = self.list_doc(closed=item)
        return doc, [types.TextContent(text=f"closed {args.session_id}; {len(doc['sessions'])} session(s) open")]


def register_sandbox_tools(
    app: Any,
    *,
    root: str | os.PathLike[str] | None = None,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    workdir: str | os.PathLike[str] | None = None,
    out_dir: str | os.PathLike[str] | None = None,
    idle_timeout_s: float | None = DEFAULT_IDLE_TIMEOUT_S,
) -> SandboxTools:
    """Adds the sandbox tools to a `QtMcpServer` (listed after its own tools; datasets resolved against `root`,
    default the server's `QT_MCP_ROOT`) and appends their instructions to the server's. `out_dir` (outside the root)
    receives each finished session's scorecard, order log and replay tables. Returns the registry (its `sessions`,
    `list_doc()` and `close_all()`); `app.sandbox_tools` refers to it too."""
    tools = SandboxTools(
        root=root if root is not None else app.outputs.root,
        max_sessions=max_sessions,
        workdir=workdir,
        out_dir=out_dir,
        idle_timeout_s=idle_timeout_s,
    )
    for t in tools.tool_defs:
        if t.name in app._by_name:
            raise QtError("QT-CONFIG-001", f"tool {t.name} is already registered")
        app._by_name[t.name] = t
        app._handlers[t.name] = tools.handlers[t.name]
    base: Callable[[], list[types.Tool]] = app.tool_list
    app.tool_list = lambda: [*base(), *tools.tool_list()]
    app.server.instructions = (
        f"{app.server.instructions} {SANDBOX_INSTRUCTIONS}" if app.server.instructions else SANDBOX_INSTRUCTIONS
    )
    app.sandbox_tools = tools
    return tools


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m queuetruth.mcp.sandbox_tools [--root DIR] [--out DIR] [--max-sessions N] [--idle-timeout S]`: the
    local tools plus the sandbox tools over stdio."""
    from queuetruth.mcp.server import build_server, run_stdio

    parser = argparse.ArgumentParser(prog="python -m queuetruth.mcp.sandbox_tools", description=__doc__.split("\n")[0])
    parser.add_argument("--root", default=None, help="dataset root (default QT_MCP_ROOT or the working directory)")
    parser.add_argument(
        "--out", default=None, help="write each finished session's scorecard and order log here (outside the root)"
    )
    parser.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS, help="open sessions at most")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT_S,
        help="seconds after which an idle session may be evicted to make room (negative: never)",
    )
    ns = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    app = build_server(local=True, root=ns.root)
    tools = register_sandbox_tools(
        app,
        max_sessions=ns.max_sessions,
        out_dir=ns.out,
        idle_timeout_s=None if ns.idle_timeout < 0 else ns.idle_timeout,
    )
    try:
        anyio.run(run_stdio, app)
    finally:
        tools.close_all()
    return 0


__all__ = [
    "DEFAULT_IDLE_TIMEOUT_S",
    "DEFAULT_MAX_SESSIONS",
    "SANDBOX_INSTRUCTIONS",
    "SANDBOX_TOOLS",
    "SANDBOX_TOOL_NAMES",
    "SandboxTools",
    "main",
    "register_sandbox_tools",
]

if __name__ == "__main__":
    raise SystemExit(main())
