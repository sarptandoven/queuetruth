# Use queuetruth from an agent (MCP)

**Goal:** let an AI agent (any MCP client) read queuetruth results, explain a fill, and, if you allow it, start jobs
on your queuetruth service.

`queuetruth mcp serve` runs an MCP server over stdio (official `mcp` Python SDK 2.x). Install it with
`pip install "queuetruth[mcp]"`. The full tool list with schemas is in the [MCP tools reference](../reference/mcp.md).

<!-- check-docs: run -->
```sh
queuetruth mcp serve --help
```

## Two modes

| | Local mode | Remote mode |
|---|---|---|
| When | `--local`, or `QT_API_KEY` is not set | `QT_API_KEY` is set and `--local` is not given |
| Reads | output directories under `QT_MCP_ROOT` (default: the server's working directory) | the HTTP API at `QT_API_URL` (default `http://127.0.0.1:8080`), plus the local tools |
| Tools | `qt_local_read_summary`, `qt_local_list_outputs`, `qt_local_why` | `qt_meta`, `qt_list_datasets`, `qt_describe_dataset`, `qt_estimate`, `qt_get_job`, `qt_list_job_files`, `qt_get_replay`, `qt_get_calibration`, `qt_get_scorecard`, `qt_explain_metric`, then the submit tools (only with `--allow-submit`), then the three local tools |
| Writes | nothing | only through submit tools, and only with `--allow-submit` |

The tool order and the mode rules are tested in `python/tests/mcp/test_mcp_server.py` (O-MCP-1:
`test_O_MCP_1_tool_order_and_schemas`, `test_O_MCP_1_submit_tools_absent_without_allow_submit`,
`test_O_MCP_1_local_mode_lists_only_local_tools`). `--allow-submit` is ignored in local mode, with a warning on
stderr. `qt_explain_metric` needs no service but is listed only in remote mode in v0.1; in local mode, read the
metric definitions in [Scorecard statistics](../concepts/scorecard.md) or the
[metric glossary](../reference/mcp.md#metric-glossary-qt_explain_metric).

In local mode every path argument is resolved against `QT_MCP_ROOT`; a path that leaves the root after resolving
symlinks is refused with `QT-CONFIG-001`. A summary is any JSON file whose top-level `schema` names a `qt.*`
document; it is validated with the shared model before it is returned. A directory is read as its summary
(`summary.json` first, then `calibration.json`, `model.json`, `report.json`, `manifest.json`, else its only `qt.*`
JSON file). `qt_local_why` runs the exact why-trace with no output file. The local tools never write
(`test_local_tools_read_and_never_write`).

## Configure your MCP client

Most clients take a JSON `mcpServers` object. Local, read-only access to a results directory:

```json
{
  "mcpServers": {
    "queuetruth": {
      "command": "queuetruth",
      "args": ["mcp", "serve", "--local"],
      "env": {"QT_MCP_ROOT": "/home/me/research/queuetruth-runs"}
    }
  }
}
```

Against a self-hosted service (see [Self-host the service](self-host-the-service.md)), with a `read` key:

```json
{
  "mcpServers": {
    "queuetruth": {
      "command": "queuetruth",
      "args": ["mcp", "serve"],
      "env": {
        "QT_API_URL": "https://queuetruth.internal",
        "QT_API_KEY": "qt_live_...",
        "QT_MCP_ROOT": "/home/me/research/queuetruth-runs"
      }
    }
  }
}
```

Use the full path of the `queuetruth` executable (for example the one in your virtual environment's `bin/`) if the
client does not inherit your `PATH`. To let the agent start jobs, add `"--allow-submit"` to `args` and give it a
`write` key; give it nothing more than it needs, since every submitted job uses your service's capacity and the
`submit` rate limit (10 per minute per key by default).

## Try it without a client

The MCP SDK can connect to the server in-process or over stdio, which is a quick way to check the setup. First make a
small run on the synthetic dataset: an exact replay of 50 sampled shadow orders, and a run of the L2 queue models on
the same orders.

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --out synth
queuetruth shadows sample --data synth --n 50 --seed 1 --out shadows.parquet
queuetruth replay --data synth --shadows shadows.parquet --out run1 --bbo none --trajectory none
queuetruth zoo --data synth --shadows shadows.parquet --out zoo1
```

Now call the local tools through an MCP client session, exactly as an agent would. The server is started as a
subprocess with the same command and environment as the client configuration above:

<!-- check-docs: run -->
```python
import os
import shutil

import anyio
from mcp import Client, StdioServerParameters

env = {k: v for k, v in os.environ.items() if k not in ("QT_API_KEY", "QT_API_URL")}
env["QT_MCP_ROOT"] = os.getcwd()
server = StdioServerParameters(command=shutil.which("queuetruth"), args=["mcp", "serve", "--local"], env=env)


async def main() -> None:
    async with Client(server) as c:
        for tool in (await c.list_tools()).tools:
            print(tool.name, "read-only" if tool.annotations.read_only_hint else "writes")
        listing = await c.call_tool("qt_local_list_outputs", {"path": "."})
        for entry in listing.structured_content["entries"]:
            print(entry["path"], entry["schema"])
        replay = await c.call_tool("qt_local_read_summary", {"path": "run1"})
        print(replay.content[0].text.splitlines()[0])
        bad = await c.call_tool("qt_local_read_summary", {"path": "../elsewhere"})
        print(bad.is_error, bad.content[0].text)


anyio.run(main)
```

Each result carries the full document as `structuredContent` and a short text rendering in `content`: a headline,
plus the compact JSON when it is shorter than 1,500 characters. A failure is not a protocol error; it is a result with
`isError` true, a `qt.error` document (`code`, `message`, `hint`) as `structuredContent` and `"<code>: <message>"`
as text (`test_errors_are_tool_results_with_qt_error`).

## An example exchange

Asked "which queue model has the smallest fill-rate bias at 10 s in `zoo1`, and is its confidence interval clear of
zero?", an agent lists the outputs, reads the zoo summary and compares the `fill_rate_bias` entries of the
`leaderboard` (keyed by horizon in ns; see [Scorecard statistics](../concepts/scorecard.md) for the definition). To
explain one order it calls `qt_local_why`. The same calls, made from Python with the in-process server:

<!-- check-docs: run -->
```python
import anyio
import pyarrow.parquet as pq
from mcp import Client

from queuetruth.mcp.server import build_server

app = build_server(local=True, root=".")
H10S = str(10_000_000_000)


async def main() -> None:
    async with Client(app.server) as c:
        zoo = (await c.call_tool("qt_local_read_summary", {"path": "zoo1"})).structured_content
        print(zoo["schema"], zoo["n_shadows"], "shadows,", len(zoo["leaderboard"]), "models")
        rows = [(r["model_id"], r["fill_rate_bias"][H10S]) for r in zoo["leaderboard"]]
        best, b = min(rows, key=lambda x: abs(x[1]["value"]))
        clear = b["ci_lo"] > 0 or b["ci_hi"] < 0
        print(f"smallest |bias| at 10 s: {best} {b['value']:+.3f} [{b['ci_lo']:+.3f}, {b['ci_hi']:+.3f}]")
        print("CI clear of zero:", clear)
        sid = pq.read_table("shadows.parquet").column("shadow_id")[0].as_py()
        args = {"dataset_path": "synth", "shadows_path": "shadows.parquet", "shadow_id": sid}
        why = await c.call_tool("qt_local_why", args)
        print(why.content[0].text.splitlines()[0])


anyio.run(main)
```

On 50 synthetic shadows the intervals are wide; this run checks the mechanics, not any model. The numbers come from
the data and the seed, so they are not quoted here.

Against a service, an agent with `--allow-submit` runs a job like this (abbreviated JSON-RPC payloads):

```text
-> tools/call qt_list_datasets {}
<- structuredContent: {"schema": "qt.page", "data": [{"schema": "qt.dataset", "id": "ds_01J...", "status": "ready", ...}], ...}
-> tools/call qt_submit_replay {"spec": {"dataset": {"dataset_id": "ds_01J..."}, "rank_oracle": true}}
<- structuredContent: {"schema": "qt.job", "id": "job_01J...", "kind": "replay", "state": "queued",
                       "progress": {"fraction": 0.0, "stage": "queued"}, "resource": {"type": "replay", "id": "rp_01J..."}, ...}
-> tools/call qt_get_job {"job_id": "job_01J..."}          (repeated until state is succeeded, failed, canceled or expired)
-> tools/call qt_get_replay {"id": "rp_01J..."}
<- structuredContent: the qt.replay_summary
-> tools/call qt_list_job_files {"job_id": "job_01J..."}
<- structuredContent: qt.file_manifest; content: one resource_link per file (presigned URL, at most 15 minutes)
```

## Long jobs: the job handle

The submit tools return the `qt.job` handle at once; the agent polls `qt_get_job` until the state is terminal, then
reads the summary (`qt_get_replay`, `qt_get_calibration`, `qt_get_scorecard`) or the files (`qt_list_job_files`).
Calling a `qt_get_*` tool while the job runs returns a `QT-REMOTE-001` error result that names the job and its
state, with the hint to keep polling. The MCP Tasks extension is not used: the installed SDK (`mcp` 2.3.0) does not
ship it, so v0.1 relies on this handle pattern. Pass `idempotency_key` to a submit tool so that a retried call does
not start a second job.

## Safety

- **No destructive tools.** No tool deletes data, revokes or creates keys, or changes retention. Every tool is
  annotated `destructiveHint: false` and `openWorldHint: false`; read tools are `readOnlyHint: true`.
- **Least privilege.** Without `--allow-submit` the agent cannot start jobs at all. Give a read-only agent a `read`
  key; a `read` key cannot submit even if the flag is set, because the service checks the scope.
- **Files stay out of the conversation.** Job files are returned as `resource_link` items with presigned URLs, never
  inline; local tools return only `qt.*` JSON documents, not Parquet data.
- **Local mode stays inside its root.** Point `QT_MCP_ROOT` at the results directory, not at your home directory.
- **Tool output is data.** Treat summaries, dataset names and error messages as data, not as instructions to the
  agent. Dataset names and job specs are written by users of your service.
- **Network.** In remote mode the server's only network client is the HTTP client for `QT_API_URL`; it returns
  presigned URLs to the agent but does not fetch them. The local tools have no network code. (This follows from the
  code in `python/queuetruth/mcp/`; no test asserts it.)
