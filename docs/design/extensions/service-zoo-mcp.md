# Zoo and calibration over HTTP, MCP run mode, optional MCP tool modules

Spec text for the integrator to fold into `docs/design/spec.md` (sections 4.5, 4.6, 8.2 and 8.8). Everything here
is built and tested; the tests named below are the evidence.

## 4.5 HTTP API: zoo runs

| Method & path | Scope | Body -> Response | Codes |
|---|---|---|---|
| POST `/v1/zoos/estimate` | read | ZooSpec -> qt.estimate | 200, 404, 409, 422 |
| POST `/v1/zoos` (I) | write | ZooSpec -> {id, job: qt.job} | 202 + `Location: /v1/jobs/{job_id}`, 404, 409, 422 |
| GET `/v1/zoos`, GET `/v1/zoos/{zoo_id}` | read | -> {id, spec, job_id, summary?: qt.zoo_summary} | 200, 404 |

- Ids: `zoo_<ULID>` (added to the id grammar). Job kind `zoo` (added to `qt.job.kind` and to `?kind=` of
  `GET /v1/jobs`); the job's resource is `{type: "zoo", id}`.
- References follow the other job resources: `dataset` is a `dataset_id` (ready), `shadows` and `amends` are
  `upload_id` (verified) or `job_file` (of a succeeded job) of the caller's tenant; a path or another tenant's id is
  422 or 404; `out` must be null. The spec is stored with SPEC 4.2 defaults.
- Bounds (422 `validation-error`, `QT-CONFIG-001`): the existing `QT_COMPUTE_LIMITS` knobs (`threads`, `particles`,
  `bootstrap_b`); at most 64 entries in `models`, none repeated, each matching
  `[A-Za-z0-9_.-]{1,64}(\[[A-Za-z0-9_.=;,:+-]{0,128}\])?` in full (a trailing newline is refused), and at least one;
  at most 32 entries in `horizons_ns`. The 32-horizon bound applies to calibration specs too. Every violation, compute
  and list alike, is listed in one `errors[]` (`test_compute_and_list_violations_are_reported_together`). A model
  family must be one of the engine's registry (the list behind `queuetruth zoo --list-models`, read once from
  `queuetruth._core`), so a misspelled family is 422 at submit and at estimate; parameter values (`databento_mc[k=5]`)
  are checked by the engine when the job runs, which fails with the engine's `QT-CONFIG-001` message (status 422
  problem), as the CLI reports it (`test_zoo_job_with_a_bad_model_parameter_fails_with_the_engine_code`). A service
  built without `queuetruth._core` cannot run zoo jobs and checks only the shape. The worker applies the same bounds
  again before it runs a job.
- Estimate: records and shadows as for the other kinds (manifest rows; upload size / 64 bytes). `all` expands to the
  engine's list (16 models, 2 of them `databento_mc`), and an explicit id that list already holds counts once, as the
  engine dedupes it (`test_zoo_estimate_follows_the_engine_registry`, default suite). Per shadow the work is one step
  per deterministic model, `1 + particles / 500` steps per particle model, and `(models + 1) x bootstrap_b / 1000`
  steps for the leaderboard bootstrap; `cpu = 2 x records / 4e6 + shadows x steps x 0.004 s`,
  `bytes_out = shadows x models x 512` plus `records x 24` with `write_trajectory`, `eta = cpu + 5 s`
  (`test_zoo_estimate`). The step weights come from the measurements below: over the measured cases (`all` with 1 and
  1,000 draws, one model with 10,000 draws, `databento_mc` with 10,000 particles) the estimate is 14x to 24x the
  measured time above the fixed cost, where counting (model, shadow) pairs alone gave 0.65x (10,000 draws) to 17x
  (`all`).
- Job: the worker resolves the references, sets `out`, and the child calls `queuetruth.run_zoo`; the stored summary
  is the `qt.zoo_summary` with the spec echo of the submitted spec (`out`, `threads` removed), equal to a local
  `run_zoo` of the same inputs apart from the echo. Every Parquet output (`zoo_fills`, `zoo_shadows`, `zoo_errors`,
  `zoo_predictions`, ...) is byte-identical to the local run's, and the downloaded `summary.json` differs from the
  local one only in the spec echo, which names tenant ids instead of paths (`test_real_zoo_job_equals_a_local_run`).
  Usage: `cpu_ms`, `shadow_orders` = `n_shadows`, `zoo_pairs` = `n_shadows x len(models)`. `QT_JOB_TIMEOUTS_S`
  accepts `zoo` (`test_zoo_per_kind_timeout`).
- Clients: `queuetruth.client` knows the kind (`Client.estimate/submit/get_run/list_runs("zoo", ...)`,
  `client.jobs.submit("zoo", ...)`, so `queuetruth jobs submit zoo --spec ...`), the fake service of
  `queuetruth.client.testing` serves `/v1/zoos`, and the remote MCP server lists `qt_get_zoo` (and `qt_submit_zoo`
  with `--allow-submit`) after the SPEC 4.6 tools, with `qt_estimate` taking `kind: "zoo"`
  (`test_mcp_zoo_remote.py`).
- Upgrade note: `qt.job.kind` is a closed enum, so a client older than this release fails to parse a
  `GET /v1/jobs` page (`QT-REMOTE-001`) once the tenant has a zoo job.
- Calibration jobs were already a kind (`calibration`); they now have an end-to-end test through the worker whose
  stored `qt.calibration_result` equals a local `queuetruth.calibrate` apart from the echo
  (`test_real_calibration_job_equals_a_local_run`).

## 8.2 / 8.8 Database and retention

Migration `0008_zoo_jobs.sql`: the `jobs.kind` check gains `zoo`; table `zoos (id, tenant_id, job_id, spec jsonb,
summary jsonb, created_at)` with an index on `(tenant_id, id)`, forced row-level security with the `qt_current_tenant()`
policy of 0005, and DML grants to `qt_app` and `qt_worker` (`test_zoos_table_forces_rls_and_job_kind_constraint`).
Tenant isolation holds for every zoo endpoint, for references to another tenant's dataset or upload, and at the SQL
level (`test_zoo_tenant_isolation`). The retention purge clears `zoos.summary` of expired jobs like the other run
tables (`test_retention_purge_clears_zoo_summaries`).

## 4.6 MCP server: run mode

Run mode is opt-in: `--allow-run` on `python -m queuetruth.mcp.server`, or `QT_MCP_ALLOW_RUN=1` (the way to reach it
from `queuetruth mcp serve`, which has no `--allow-run` flag yet). It needs a POSIX host (process groups and
`resource`); elsewhere it is `QT-CONFIG-001` at startup (`test_run_mode_needs_posix`). It works in local and remote
mode; the tools below are listed after the SPEC 4.6 tools (and, in remote mode, the zoo tools), and the server
instructions gain one paragraph naming them.

| Tool | Input | Output | Notes |
|---|---|---|---|
| `qt_local_sample_shadows` | {spec: ShadowSampleSpec} | qt.shadow_sample_summary | writes `shadows.parquet` |
| `qt_local_run_replay` | {spec: ReplaySpec} | qt.replay_summary | |
| `qt_local_run_zoo` | {spec: ZooSpec} | qt.zoo_summary | |
| `qt_local_why` (run mode) | as SPEC 4.6 | qt.why_trace | same inputs rule and limits as the run tools |

- Listed input schemas are the spec models with every reference narrowed to `{"path"}` and without `out`
  (`LocalShadowSampleSpec`, `LocalReplaySpec`, `LocalZooSpec`), so a client is not offered references the server
  refuses (`test_run_tools_listed_only_in_run_mode`).
- Inputs are `{"path"}` references; relative paths are taken from `QT_MCP_ROOT`. Each path is resolved with realpath
  and must lie in an allowed root: `QT_MCP_ROOT`, each directory of `QT_MCP_DATA_ROOTS` (os.pathsep-separated) and the
  run root. No allowed root may be the filesystem root, the home directory or a parent of it: a stdio server started
  from `/` (as desktop MCP hosts often do) without `QT_MCP_ROOT` would otherwise allow every file, so that is
  `QT-CONFIG-001` at startup with a hint to set `QT_MCP_ROOT` (`test_run_mode_refuses_broad_roots`). A run root that
  cannot be created is `QT-CONFIG-001` at startup too (`test_unwritable_run_root_is_a_config_error`). A dataset must
  hold `manifest.json` (`QT-INPUT-001` otherwise, checked before anything under it is read, so `{"path": "."}` does not
  walk the root) and no symlink that leads out of the roots; the symlink check walks at most 100,000 entries.
  `dataset_id`, `upload_id` and `job_file` references and a non-null `out` are `QT-CONFIG-001`; a malformed spec is
  `QT-USAGE-001`; a missing input `QT-INPUT-001`.
- Outputs: `QT_MCP_RUN_ROOT` (default `<QT_MCP_ROOT>/qt-mcp-runs`) `/<kind>/<run_id>/`, where `kind` is `shadows`,
  `replay` or `zoo` and `run_id` is the SPEC 7.3 id of the resolved spec. A run writes into
  `<run root>/.staging/<kind>-<run_id>-<random>` and is renamed into place only after it succeeded within the limits;
  anything else removes the staging directory, so a directory at `<kind>/<run_id>` is always complete. When it already
  holds a valid `summary.json` for that `run_id`, the call returns it without running (the run id hashes the inputs'
  contents, so this is the same answer). A directory there whose `summary.json` does not validate (truncated, a
  symlink, another schema) is moved aside and removed before the run, so it never blocks its run id
  (`test_an_invalid_finished_directory_is_replaced`). Identical specs called together run once: the second waits for
  the first and reuses its directory (`test_identical_specs_started_together_run_once`). Staging directories left by a
  killed server (older than the run timeout plus 5 minutes) are removed at startup
  (`test_stale_staging_is_swept_at_startup`).
- Results: the summary as `structuredContent`; `_meta` with `queuetruth/out_dir` (absolute), `queuetruth/run_id`
  and `queuetruth/reused`; a text line `outputs: <dir> (new run | finished run reused; N file(s))`; and a `file://`
  `resource_link` per output file, at most 100, with `; K linked, list the rest with qt_local_list_outputs or the file
  system` when there are more (`test_link_cap_note`). Sampled shadows are always at
  `<run root>/shadows/<run_id>/shadows.parquet`.
- Each run (and run-mode why) is a fresh `python -I` child in its own session. Its environment is an allow list
  (`PATH`, locale, `TZ`) plus `HOME`/`TMPDIR` in a private directory and `RAYON_NUM_THREADS` = `OMP_NUM_THREADS` = the
  spec's `threads`: no `QT_*` variable, so no API key, reaches it. An audit hook refuses unpickling. The child's sys.path
  is set before `queuetruth` is imported, so it runs the same sources as the server.
- Limits (`RunLimits`, each from `QT_MCP_RUN_<NAME>`; a malformed value is `QT-CONFIG-001` at startup):

| Limit | Default | Enforcement |
|---|---|---|
| `TIMEOUT_S` | 900 | wall clock; the process group is SIGKILLed |
| `CPU_S` | 3600 | `RLIMIT_CPU` (SIGXCPU) |
| `MEMORY_BYTES` | 8 GiB | watchdog every 0.25 s: physical footprint on macOS, `VmRSS` on Linux, else RSS from `ps` |
| `MAX_OUTPUT_BYTES` | 8 GiB | per file `RLIMIT_FSIZE` (at most the free disk above `MIN_FREE_BYTES`; the write fails with EFBIG); the run's total by the watchdog while it runs and again after |
| `MIN_FREE_BYTES` | 2 GiB | free disk under the run root, before a run and by the watchdog while it runs |
| `MAX_THREADS` | min(4, CPUs) | spec `threads` |
| `MAX_SHADOWS` | 200,000 | shadows rows (Parquet footer) of replay and zoo; for sampling, `n`, or for `per_partition` the exact quota sum: each partition of the manifest takes its dated key `<id>_<YYYYMMDD>`, else `<id>`, else `n` |
| `MAX_PARTICLES`, `MAX_BOOTSTRAP` | 10,000 | spec `particles`, `bootstrap_b` |
| `MAX_MODELS`, `MAX_HORIZONS` | 64, 32 | zoo `models`, `horizons_ns` |
| `MAX_CONCURRENT` | 1 | runs at once; further calls wait |

  A limit hit is `QT-ENGINE-001` with a hint naming the variable; a refused spec names the knob and its variable. The
  process group is also killed when the client cancels the call (`test_cancel_kills_the_process_group`). The watchdog
  polls, so a run can overshoot the memory and output limits by what it allocates or writes in 0.25 s: a child writing
  1 MiB files every 50 ms is stopped after its total passes a 4 MiB limit
  (`test_total_output_is_bounded_while_the_run_writes`). Linux uses the watchdog too (no `RLIMIT_AS`, which counts
  address space that an allocator reserves but never touches); the `VmRSS` parser is tested
  (`test_proc_status_rss`), the kill path on Linux only through the macOS run of `test_memory_limit`. `qt_local_why`
  traces one shadow, so its shadows file is not bounded by `MAX_SHADOWS`; its work is bounded by the child's limits.
  The sampling bound equals what the engine samples on `synth_small`, where an undated quota counts once per day of
  its instrument (`test_sample_bound_equals_what_the_engine_samples`).

### Optional tool modules

`OPTIONAL_TOOL_MODULES = (("queuetruth.mcp.sandbox_tools", "register_sandbox_tools"),)`. The stdio server (`serve`:
`queuetruth mcp serve` and `python -m queuetruth.mcp.server`) imports each installed module and calls the function
with the `QtMcpServer`; `QT_MCP_OPTIONAL_TOOLS=0` turns that off. `build_server` registers none unless its
`tool_modules` names them, so a program that builds a server and registers a module itself (agent-sandbox's
`python -m queuetruth.mcp.sandbox_tools` and its tests) gets it once, and the hook skips a module already in
`server.optional_modules`. A module that is not installed is skipped quietly; an import error, a missing function, a
raising function, or a registration after which a listed tool is listed twice, has no handler or has an unresolvable
schema, or a callable tool is not listed, is logged and rolled back, and the server starts without it. Rollback restores
the tool registry, the instance attributes the registration added or replaced (a `tool_list` override, a
`sandbox_tools` registry) and the server instructions (`test_failed_direct_registration_is_rolled_back`). Verified
against agent-sandbox's real module by overlaying its sources: its own `test_mcp_sandbox.py` and
`test_real_sandbox_module_composes_with_the_hook` pass.

A module adds tools with `QtMcpServer.add_tool(tool: ToolDef, handler, *, input_schema=None, output_schema=None,
read_only=True)`. `handler(tool, args)` may be sync or async; `args` is the validated `tool.args` model; it returns
the structured document or `(document, extra content blocks)`, and a `QtError` it raises becomes a `qt.error` tool
result. The input schema defaults to `schema_args or args`, the output schema to the document schema named by
`tool.output` (`output_schema` is required for anything that is not a `qt.*` document). Added tools are listed after
the built-in ones and are never destructive; `read_only=False` marks a tool that writes. The module can read
`server.local_mode`, `server.allow_run` and `server.runner` to decide what to register. A module may instead write
`_by_name` and `_handlers` and override `tool_list` on the instance, as agent-sandbox's does; the hook checks and rolls
back that way of registering too (`test_direct_registration_composes_with_the_hook`).

## Example

The run tools through the MCP client on the committed synthetic dataset: sample shadows, run two zoo models on them,
and ask again (the second call reuses the finished run).

<!-- check-docs: run -->
```python
import os
import tempfile

import anyio
from mcp import Client

from queuetruth.mcp.local import RunLimits
from queuetruth.mcp.server import build_server

data = os.path.join(os.environ["QT_FIXTURES"], "synth_small")
root = tempfile.mkdtemp()


async def main() -> None:
    app = build_server(
        local=True, allow_run=True, root=root, data_roots=[data], run_limits=RunLimits(min_free_bytes=0)
    )
    async with Client(app.server) as c:
        res = await c.call_tool("qt_local_sample_shadows", {"spec": {"dataset": {"path": data}, "n": 20, "seed": 1}})
        out = res.meta["queuetruth/out_dir"]  # <run root>/shadows/<run_id>
        spec = {
            "dataset": {"path": data},
            "shadows": {"path": f"{out}/shadows.parquet"},
            "models": ["hbt_power3[n=3]", "hbt_risk_adverse"],
            "bootstrap_b": 20,
        }
        for _ in range(2):
            res = await c.call_tool("qt_local_run_zoo", {"spec": spec})
            assert not res.is_error, res.content
            print(res.content[0].text.splitlines()[0], "|", res.content[1].text)


anyio.run(main)
```

## Measurements

On the development laptop (8 cores, macOS, Low Power Mode, shared with other jobs), 2026-10-24:

- A run child costs about 1.1 s before the work starts (median of 5 spawns of a trivial target: 1.092 s; interpreter
  start plus `queuetruth` imports). On `synth_small`, sampling 20 shadows took 1.56 s through the MCP client, a
  one-model zoo on them 2.09 s, a replay 2.09 s, and the reused runs 0.01 s each.
- The memory watchdog first read RSS from `ps`. A child allocating highly compressible 32 MiB blocks ran 27.75 s
  before RSS crossed a 768 MiB limit, because macOS compresses those pages and RSS leaves them out; reading the
  physical footprint (`proc_pid_rusage`) stops it in 2.67 s (`test_memory_limit`).
- The four zoo and calibration integration tests (real jobs through PgQueuer, the worker and the child, on
  `synth_small` with 40 shadows, all 16 models) take 24.8 s together.
- Zoo cost on `synth_small` with 400 shadows (`queuetruth.run_zoo`, best of 2): one model with 1 bootstrap draw
  0.486 s (the fixed cost of the two passes); one model with 1,000 and 10,000 draws 0.800 and 2.947 s (0.25 ms per
  draw); all 16 models with 1 and 1,000 draws 1.958 and 3.419 s (1.46 ms per draw, 0.23 ms per (model, shadow) pair);
  `databento_mc[k=-0.8]` with 100, 1,000 and 10,000 particles 0.525, 0.694 and 1.863 s (0.10, 0.52 and 3.4 ms per
  pair). The estimate's step weights (`1 + particles / 500`, `(models + 1) x bootstrap_b / 1000`) follow these ratios.
