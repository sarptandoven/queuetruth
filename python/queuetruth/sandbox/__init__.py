"""Agent sandbox: a simulated exchange over replayed order-level data in which an agent's orders fill exactly as the
real queue would have filled them, with no look-ahead.

    from queuetruth.sandbox import Sandbox

    with Sandbox.open({"dataset": {"path": "synth"}, "instrument_id": 1, "session_date": "2026-06-15",
                       "start": "13:35", "end": "14:35", "latency": "1ms"}) as sb:
        obs = sb.observe()
        sb.submit("buy", 2, price="5000.25")            # day limit order
        obs = sb.advance(by="5s")                        # fills, queue position, markouts so far
        card = sb.finish(out="episode")                  # PnL after fees, fill rates, adverse selection

The agent's clock moves only forward; it sees the market and its orders up to `clock - feed_latency_ns`; every
replay the session runs reads a scratch dataset that physically ends at that cut (`queuetruth.sandbox.tape`). Each
`advance` re-runs the exact replay of the episode's prefix with the open orders (decide-then-replay windows), and
`finish` runs one full replay of every committed order and proves that its fills equal what the session reported.
The committed log (`order_log()`, `scorecard.json`'s certificate) can be re-executed by anyone with `verify`.
MCP tools: `queuetruth.mcp.sandbox_tools.register_sandbox_tools`. Design: docs/design/extensions/agent-sandbox.md.
"""

from queuetruth.sandbox.models import (
    SANDBOX_DOC_MODELS,
    SANDBOX_SPEC_MODELS,
    FeeSchedule,
    SandboxAck,
    SandboxFinish,
    SandboxObservation,
    SandboxOrderLog,
    SandboxScorecard,
    SandboxSession,
    SandboxSessionList,
    SandboxSpec,
    SandboxVerification,
    json_schemas,
    parse_sandbox_document,
)
from queuetruth.sandbox.session import RULES, Sandbox, normalize_spec
from queuetruth.sandbox.verify import verify

__all__ = [
    "RULES",
    "SANDBOX_DOC_MODELS",
    "SANDBOX_SPEC_MODELS",
    "FeeSchedule",
    "Sandbox",
    "SandboxAck",
    "SandboxFinish",
    "SandboxObservation",
    "SandboxOrderLog",
    "SandboxScorecard",
    "SandboxSession",
    "SandboxSessionList",
    "SandboxSpec",
    "SandboxVerification",
    "json_schemas",
    "normalize_spec",
    "parse_sandbox_document",
    "verify",
]
