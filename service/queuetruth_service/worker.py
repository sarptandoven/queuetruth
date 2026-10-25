"""Worker (SPEC 8.3): consumes job ids from the queue, claims each job with the fenced SECURITY DEFINER functions,
heartbeats every `HEARTBEAT_S` (lease `LEASE_S`), resolves inputs into a private work directory, runs the job's
computation in a child process (`executor`), uploads outputs, and commits the terminal state together with job
files, usage events and the resource summary in one tenant transaction. A sweeper requeues expired leases (failing
after `MAX_ATTEMPTS`) and re-enqueues lost enqueues; maintenance passes verify uploads, expire abandoned uploads,
prune idempotency records, push usage and apply retention.

Duplicate deliveries are dropped by the claim; a worker whose lease was lost (heartbeat not owned) kills its child
and discards the result. Cancel sets `cancel_requested`; the heartbeat sees it and kills the child.

Row locks: the single-call lifecycle functions (claim, heartbeat, mark-enqueued, sweep) run in autocommit, so the job
row lock they take is released by the server at the end of the statement and is never held across a client round trip
(a stalled event loop between a statement and its COMMIT would otherwise keep a concurrent cancel waiting). The
heartbeat also waits at most 2 s for the row (`lock_timeout` on `qt_heartbeat`, migration 0007) and retries on the
next tick. The finish transaction hashes output files before it takes the row lock.

Security review: the worker connects as `qt_worker` (H1/M3) and every job transaction is bound to the job attempt it
holds (`JobBinding`; a stale attempt cannot write), queries filter by `tenant_id` explicitly; the claim caps each
tenant's running jobs (`QT_TENANT_MAX_RUNNING`) and a capped delivery is re-enqueued after `QT_TENANT_DEFER_S`;
compute knobs above `QT_COMPUTE_LIMITS` fail the job (M4); archives are downloaded here but extracted by the job
child (M2); each job directory lives under `QT_JOB_ROOT` with its own `tmp/` (M5).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from queuetruth.schemas import is_ref, spec_echo
from queuetruth_service import maintenance, netguard
from queuetruth_service.billing import Biller, NoopBiller
from queuetruth_service.db import BindingRefused, JobBinding, tenant_tx_async
from queuetruth_service.executor import ChildResult, run_child
from queuetruth_service.ids import new_id
from queuetruth_service.logs import job_id_var, tenant_id_var
from queuetruth_service.problems import PROBLEM_BASE
from queuetruth_service.queue import JobQueue
from queuetruth_service.refs import compute_violations
from queuetruth_service.retention import delete_dataset
from queuetruth_service.routers.runs import list_violations
from queuetruth_service.runners import TARGETS, content_type, extraction_token, is_archive, list_outputs, usage_meters
from queuetruth_service.settings import Settings
from queuetruth_service.store import ObjectStore, dataset_prefix, job_key, job_prefix, safe_filename

log = logging.getLogger("queuetruth_service.worker")
RUN_TABLES = {
    "replay": "replays",
    "calibration": "calibrations",
    "scorecard": "scorecards",
    "report": "reports",
    "zoo": "zoos",
}
CLAIM_FIELDS = ("id", "tenant_id", "kind", "spec", "attempts", "resource_type", "resource_id")


@dataclass
class ClaimedJob:
    """A job this worker owns for one attempt."""

    id: str
    tenant_id: str
    kind: str
    spec: dict[str, Any]
    attempts: int
    resource_type: str
    resource_id: str

    @property
    def binding(self) -> JobBinding:
        """The tenant binding of this attempt's transactions."""
        return JobBinding(self.tenant_id, self.id, self.attempts)


@dataclass
class Prepared:
    """Inputs resolved for the child: the spec (relative paths) and the archives the child must extract."""

    spec: dict[str, Any]
    extract: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Outcome:
    """Result of a job body."""

    state: str
    error: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    files: list[tuple[str, Path, str]] = field(default_factory=list)
    usage: dict[str, tuple[float, str]] = field(default_factory=dict)
    dataset: dict[str, Any] | None = None


class JobInputError(Exception):
    """An input reference could not be resolved (becomes a failed job, `QT-INPUT-001`)."""

    def __init__(self, message: str, code: str = "QT-INPUT-001") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Fenced(Exception):
    pass


def job_problem(job_id: str, code: str, message: str) -> dict[str, Any]:
    """`qt.problem` stored as a failed job's `error`."""
    status = 422 if code.split("-")[1] in ("USAGE", "CONFIG", "INPUT", "DATA", "GATE") else 500
    return {
        "schema": "qt.problem",
        "schema_version": 1,
        "type": PROBLEM_BASE + "job-failed",
        "title": "Job failed",
        "status": status,
        "detail": message,
        "code": code,
        "request_id": f"req_{job_id}",
    }


class Worker:
    """One worker process's job loop and background passes."""

    def __init__(
        self,
        settings: Settings,
        pool: AsyncConnectionPool,
        store: ObjectStore,
        queue: JobQueue | None = None,
        *,
        targets: dict[str, str] | None = None,
        biller: Biller | None = None,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.store = store
        self.queue = queue
        self.targets = dict(TARGETS) | dict(targets or {})
        self.biller: Biller = biller or NoopBiller()
        self._last_daily: str | None = None

    # ---- claim / heartbeat / finish -------------------------------------------------------------------------

    async def claim(self, job_id: str) -> ClaimedJob | None:
        """Claims a queued job (None: not queued any more, e.g. a duplicate delivery, or deferred because its tenant
        is at `QT_TENANT_MAX_RUNNING`; a deferred job is re-enqueued with a delay)."""
        rows = await self._call(
            "select * from qt_claim_job(%s, %s, %s)", (job_id, self.settings.lease_s, self.settings.tenant_max_running)
        )
        row = rows[0] if rows else None
        if row is None:
            return None
        if row["deferred"]:
            await self._defer(job_id)
            return None
        return ClaimedJob(**{k: row[k] for k in CLAIM_FIELDS})

    async def _defer(self, job_id: str) -> None:
        log.info("job %s deferred: its tenant is at the running-job cap", job_id)
        if self.queue is None:
            return  # the sweeper re-enqueues it
        await self.queue.enqueue(job_id, delay_s=self.settings.tenant_defer_s)
        await self._call("select qt_mark_enqueued(%s)", ([job_id],))

    async def _call(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        """Runs one statement in its own implicit transaction (autocommit): the row locks it takes are released by
        the server when the statement ends, never held while this event loop gets around to sending a COMMIT."""
        async with self.pool.connection() as conn:
            await conn.set_autocommit(True)
            try:
                return await (await conn.cursor(row_factory=dict_row).execute(query, params)).fetchall()
            finally:
                try:
                    await conn.set_autocommit(False)
                except psycopg.Error:
                    await conn.close()  # not idle (interrupted mid-statement): the pool discards it

    async def heartbeat_once(self, job: ClaimedJob) -> tuple[bool, bool]:
        """Extends the lease of this attempt; returns `(owned, cancel_requested)`. Raises `psycopg.Error` (for
        example `LockNotAvailable` after 2 s behind another transaction on the row); the caller retries."""
        rows = await self._call("select * from qt_heartbeat(%s, %s, %s)", (job.id, job.attempts, self.settings.lease_s))
        return bool(rows[0]["owned"]), bool(rows[0]["cancel_requested"])

    async def _heartbeat(self, job: ClaimedJob, cancel: asyncio.Event, lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_s)
            try:
                owned, cancel_req = await self.heartbeat_once(job)
            except psycopg.Error:
                log.warning("heartbeat failed for %s; retrying", job.id)
                continue
            if not owned:
                lost.set()
                cancel.set()
                return
            if cancel_req:
                cancel.set()

    async def _progress(self, job: ClaimedJob, fraction: float, stage: str) -> None:
        async with tenant_tx_async(self.pool, job.binding) as conn:
            await conn.execute(
                "update jobs set progress = %s where id = %s and tenant_id = %s and state = 'running' "
                "and attempts = %s",
                (Jsonb({"fraction": fraction, "stage": stage}), job.id, job.tenant_id, job.attempts),
            )

    async def handle(self, job_id: str) -> str | None:
        """Runs one delivery end to end; returns the terminal state written, or None (dropped / lease lost)."""
        job = await self.claim(job_id)
        if job is None:
            log.info("delivery for %s dropped (not queued)", job_id)
            return None
        job_id_var.set(job.id)
        tenant_id_var.set(job.tenant_id)
        cancel, lost = asyncio.Event(), asyncio.Event()
        hb = asyncio.create_task(self._heartbeat(job, cancel, lost))
        root = self.settings.job_root or None
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="qtjob-", dir=root))  # mode 0700, one per job
        (workdir / "tmp").mkdir()
        try:
            try:
                outcome = await self._body(job, workdir, cancel)
            except JobInputError as e:
                outcome = Outcome("failed", job_problem(job.id, e.code, e.message))
            except Exception as e:
                log.exception("job %s failed in the worker", job.id)
                outcome = Outcome("failed", job_problem(job.id, "QT-ENGINE-001", f"worker error ({type(e).__name__})"))
            if cancel.is_set() and not lost.is_set() and outcome.state != "succeeded":
                outcome = Outcome("canceled")
            if lost.is_set():
                log.warning("lease lost for %s attempt %s; result discarded", job.id, job.attempts)
                return None
            try:
                await self.finish(job, outcome)
            except _Fenced:
                log.warning("fenced finish rejected for %s attempt %s", job.id, job.attempts)
                return None
            return outcome.state
        finally:
            hb.cancel()
            shutil.rmtree(workdir, ignore_errors=True)

    async def finish(self, job: ClaimedJob, o: Outcome) -> None:
        """Terminal transition plus files, usage, summaries in one tenant transaction (fenced by attempt)."""
        if o.state == "succeeded":
            await self._upload_outputs(job, o)
        try:
            await self._finish_tx(job, o)
        except BindingRefused as e:
            raise _Fenced from e

    async def _finish_tx(self, job: ClaimedJob, o: Outcome) -> None:
        # sizes and digests first, off the event loop: hashing large outputs inside the transaction would hold the
        # job row lock (and stall every heartbeat of this worker) for as long as the hashing takes
        digests: list[tuple[int, str]] = []
        if o.state == "succeeded" and o.files:
            digests = await asyncio.to_thread(lambda: [(p.stat().st_size, _sha256(p)) for _, p, _ in o.files])
        async with tenant_tx_async(self.pool, job.binding) as conn:
            cur = conn.cursor(row_factory=dict_row)
            t = await (await cur.execute("select * from tenants where id = %s", (job.tenant_id,))).fetchone()
            expires = None
            if o.state == "succeeded" and t is not None:
                expires = datetime.now(UTC) + timedelta(days=int(t["retention_outputs_days"]))
            usage = {k: v[0] for k, v in o.usage.items()} if o.state == "succeeded" else {}
            ok = (
                await (
                    await conn.execute(
                        "select qt_finish_job(%s, %s, %s, %s, %s, %s)",
                        (job.id, job.attempts, o.state, Jsonb(o.error) if o.error else None, Jsonb(usage), expires),
                    )
                ).fetchone()
            )[0]
            if not ok:
                raise _Fenced
            if o.state == "succeeded":
                for (name, _, ctype), (size, digest) in zip(o.files, digests, strict=True):
                    await conn.execute(
                        "insert into job_files (job_id, tenant_id, name, bytes, sha256, content_type, object_key) "
                        "values (%s, %s, %s, %s, %s, %s, %s) on conflict (job_id, name) do update set "
                        "bytes = excluded.bytes, sha256 = excluded.sha256, content_type = excluded.content_type",
                        (
                            job.id,
                            job.tenant_id,
                            name,
                            size,
                            digest,
                            ctype,
                            job_key(job.tenant_id, job.id, name),
                        ),
                    )
                for meter, (qty, unit) in sorted(o.usage.items()):
                    await conn.execute(
                        "insert into usage_events (id, tenant_id, job_id, meter, quantity, unit, identifier) "
                        "values (%s, %s, %s, %s, %s, %s, %s) on conflict (identifier) do nothing",
                        (new_id("use"), job.tenant_id, job.id, meter, qty, unit, f"{job.id}:{meter}"),
                    )
                table = RUN_TABLES.get(job.kind)
                if table and o.summary is not None:
                    await conn.execute(
                        f"update {table} set summary = %s where id = %s and tenant_id = %s",
                        (Jsonb(o.summary), job.resource_id, job.tenant_id),
                    )
                run_id = (o.summary or {}).get("run_id")
                if isinstance(run_id, str):
                    await conn.execute(
                        "update jobs set run_id = %s where id = %s and tenant_id = %s", (run_id, job.id, job.tenant_id)
                    )
                if o.dataset is not None:
                    await conn.execute(
                        "update datasets set status = 'ready', manifest = %s, data_quality = %s "
                        "where id = %s and tenant_id = %s",
                        (
                            Jsonb(o.dataset.get("manifest")),
                            Jsonb(o.dataset.get("data_quality")),
                            job.resource_id,
                            job.tenant_id,
                        ),
                    )
            elif job.kind == "dataset_validate":
                await conn.execute(
                    "update datasets set status = 'failed' where id = %s and tenant_id = %s and status <> 'deleted'",
                    (job.resource_id, job.tenant_id),
                )

    async def _upload_outputs(self, job: ClaimedJob, o: Outcome) -> None:
        for name, path, _ in o.files:
            await asyncio.to_thread(self.store.put_file, job_key(job.tenant_id, job.id, name), path)

    # ---- job bodies -----------------------------------------------------------------------------------------

    async def _body(self, job: ClaimedJob, workdir: Path, cancel: asyncio.Event) -> Outcome:
        if job.kind == "deletion":
            await self._progress(job, 0.1, "deleting")
            async with tenant_tx_async(self.pool, job.binding) as conn:
                n = await delete_dataset(conn, self.store, job.tenant_id, job.resource_id)
            return Outcome("succeeded", usage={}, summary={"objects_deleted": n})
        if job.kind not in self.targets:
            raise JobInputError(f"unsupported job kind {job.kind}", "QT-CONFIG-001")
        await self._progress(job, 0.05, "preparing inputs")
        prepared = await self._prepare(job, workdir)
        if cancel.is_set():
            return Outcome("canceled")
        await self._progress(job, 0.1, "running")
        allow = None
        if await self._zero_egress(job):
            # the child holds no credentials; the allow list only matters for SPEC 8.8 compatibility
            allow = netguard.allowlist_from(self.settings.database_url_worker, self.store.endpoint)
        res = await run_child(
            self.targets[job.kind],
            prepared.spec,
            str(workdir),
            timeout_s=self.settings.job_timeout_for(job.kind),
            cancel=cancel,
            allow=allow,
            mem_bytes=self.settings.job_memory_bytes or None,
            extract=prepared.extract,
        )
        return await self._collect(job, workdir, res)

    async def _zero_egress(self, job: ClaimedJob) -> bool:
        if self.settings.zero_egress:
            return True
        async with tenant_tx_async(self.pool, job.binding) as conn:
            row = await (
                await conn.execute("select zero_egress from tenants where id = %s", (job.tenant_id,))
            ).fetchone()
        return bool(row and row[0])

    async def _collect(self, job: ClaimedJob, workdir: Path, res: ChildResult) -> Outcome:
        if res.status == "canceled":
            return Outcome("canceled")
        if res.status != "ok":
            err = res.error or {"code": "QT-ENGINE-001", "message": "job failed"}
            message = err.get("message", "").replace(str(workdir) + "/", "").replace(str(workdir), ".")
            return Outcome("failed", job_problem(job.id, err.get("code", "QT-ENGINE-001"), message))
        summary = dict(res.summary or {})
        if "spec" in summary and job.kind != "dataset_validate":
            summary["spec"] = spec_echo(job.spec)
        out = workdir / "out"
        usage = usage_meters(job.kind, summary, res.cpu_ms)
        if job.kind == "dataset_validate":
            ds_dir = out / "dataset"
            for name, path in list_outputs(ds_dir):
                await asyncio.to_thread(
                    self.store.put_file, dataset_prefix(job.tenant_id, job.resource_id) + name, path
                )
            manifest = _read_json(ds_dir / "manifest.json") or summary.get("manifest")
            dq = _read_json(ds_dir / "data_quality.json") or summary.get("data_quality")
            summary_path = workdir / "ingest_summary.json"
            summary_path.write_text(json.dumps(summary, sort_keys=True, separators=(",", ":")))
            files = [("ingest_summary.json", summary_path, "application/json")]
            return Outcome(
                "succeeded",
                summary=summary,
                files=files,
                usage=usage,
                dataset={"manifest": manifest, "data_quality": dq},
            )
        files = [(name, path, content_type(name)) for name, path in list_outputs(out)]
        return Outcome("succeeded", summary=summary, files=files, usage=usage)

    async def _prepare(self, job: ClaimedJob, workdir: Path) -> Prepared:
        """Resolves references to paths relative to `workdir`, plans archive extraction for the child and sets the
        output location. Compute knobs above `QT_COMPUTE_LIMITS` and zoo model or horizon lists outside the service's
        bounds (checked at submit too) fail the job (`QT-CONFIG-001`)."""
        errors = compute_violations(job.spec, self.settings.compute_limits, ["spec"])
        errors += list_violations(job.kind, job.spec, ["spec"])
        if errors:
            detail = "; ".join(f"{'.'.join(str(x) for x in e['loc'])} {e['msg']}" for e in errors)
            raise JobInputError(f"spec exceeds this service's limits: {detail}", "QT-CONFIG-001")
        plan: list[dict[str, str]] = []
        if job.kind == "report":
            spec = dict(job.spec)
            dirs = []
            for jid in spec.get("inputs", []):
                dirs.append(await self._download_job(job, str(jid), workdir))
            spec["inputs"] = dirs
            spec["out"] = "out/report.html"
            (workdir / "out").mkdir()
            return Prepared(spec, plan)
        if job.kind == "dataset_validate":
            ingest = dict(job.spec.get("ingest") or {})
            inputs = []
            # format parquet reads a dataset directory: an archived directory is extracted
            expand = ingest.get("format") == "parquet"
            for up in job.spec.get("upload_ids", []):
                inputs.append({"path": await self._download_upload(job, str(up), workdir, plan, expand=expand)})
            ingest["inputs"] = inputs
            if is_ref(ingest.get("instruments")):
                ingest["instruments"] = await self._resolve(job, ingest["instruments"], workdir, plan)
            ingest["out"] = "out/dataset"
            (workdir / "out").mkdir()
            return Prepared(ingest, plan)
        spec = await self._walk(job, dict(job.spec), workdir, plan)
        spec["out"] = "out"
        return Prepared(spec, plan)

    async def _walk(self, job: ClaimedJob, value: Any, workdir: Path, plan: list[dict[str, str]]) -> Any:
        if isinstance(value, dict):
            if is_ref(value):
                return await self._resolve(job, value, workdir, plan)
            return {k: await self._walk(job, v, workdir, plan) for k, v in value.items()}
        if isinstance(value, list):
            return [await self._walk(job, v, workdir, plan) for v in value]
        return value

    async def _resolve(
        self, job: ClaimedJob, ref: dict[str, Any], workdir: Path, plan: list[dict[str, str]]
    ) -> dict[str, str]:
        if "dataset_id" in ref:
            return {"path": await self._download_dataset(job, str(ref["dataset_id"]), workdir)}
        if "upload_id" in ref:
            return {"path": await self._download_upload(job, str(ref["upload_id"]), workdir, plan, expand=True)}
        if "job_file" in ref:
            jf = ref["job_file"] or {}
            return {"path": await self._download_job_file(job, str(jf.get("job_id")), str(jf.get("name")), workdir)}
        raise JobInputError("local paths are not accepted by the service", "QT-CONFIG-001")

    async def _download_dataset(self, job: ClaimedJob, ds_id: str, workdir: Path) -> str:
        tenant = job.tenant_id
        async with tenant_tx_async(self.pool, job.binding) as conn:
            row = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "select * from datasets where id = %s and tenant_id = %s", (ds_id, tenant)
                )
            ).fetchone()
        if row is None or row["status"] != "ready":
            raise JobInputError(f"dataset {ds_id} is not ready")
        rel = f"inputs/datasets/{ds_id}"
        prefix = dataset_prefix(tenant, ds_id)
        for key, _ in await asyncio.to_thread(self.store.list_prefix, prefix):
            await asyncio.to_thread(self.store.get_to_file, key, workdir / rel / key[len(prefix) :])
        return rel

    async def _download_upload(
        self, job: ClaimedJob, up_id: str, workdir: Path, plan: list[dict[str, str]], *, expand: bool
    ) -> str:
        """Downloads a verified upload; an archive to expand is planned for extraction by the child (its token
        stands for the extracted root)."""
        async with tenant_tx_async(self.pool, job.binding) as conn:
            row = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "select * from uploads where id = %s and tenant_id = %s", (up_id, job.tenant_id)
                )
            ).fetchone()
        if row is None or row["status"] != "verified":
            raise JobInputError(f"upload {up_id} is not verified")
        name = safe_filename(row["filename"])
        rel = Path("inputs/uploads") / up_id / name
        await asyncio.to_thread(self.store.get_to_file, row["object_key"], workdir / rel)
        if expand and is_archive(name):
            token = extraction_token(len(plan))
            plan.append(
                {
                    "token": token,
                    "archive": rel.as_posix(),
                    "dest": (Path("inputs/uploads") / up_id / "x").as_posix(),
                    "label": f"upload {up_id}",
                }
            )
            return token
        return rel.as_posix()

    async def _job_files(self, job: ClaimedJob, job_id: str) -> list[dict[str, Any]]:
        async with tenant_tx_async(self.pool, job.binding) as conn:
            cur = conn.cursor(row_factory=dict_row)
            j = await (
                await cur.execute("select state from jobs where id = %s and tenant_id = %s", (job_id, job.tenant_id))
            ).fetchone()
            if j is None or j["state"] != "succeeded":
                raise JobInputError(f"job {job_id} has no downloadable outputs")
            return await (
                await cur.execute(
                    "select * from job_files where job_id = %s and tenant_id = %s order by name",
                    (job_id, job.tenant_id),
                )
            ).fetchall()

    async def _download_job_file(self, job: ClaimedJob, job_id: str, name: str, workdir: Path) -> str:
        files = {f["name"]: f for f in await self._job_files(job, job_id)}
        if name not in files:
            raise JobInputError(f"job {job_id} has no file {name}")
        rel = Path("inputs/jobs") / job_id / name
        await asyncio.to_thread(self.store.get_to_file, files[name]["object_key"], workdir / rel)
        return rel.as_posix()

    async def _download_job(self, job: ClaimedJob, job_id: str, workdir: Path) -> str:
        rel = Path("inputs/jobs") / job_id
        prefix = job_prefix(job.tenant_id, job_id)
        for f in await self._job_files(job, job_id):
            await asyncio.to_thread(
                self.store.get_to_file, f["object_key"], workdir / rel / f["object_key"][len(prefix) :]
            )
        (workdir / rel).mkdir(parents=True, exist_ok=True)
        return rel.as_posix()

    # ---- sweeper and maintenance ----------------------------------------------------------------------------

    async def sweep_once(self, reenqueue_after_s: int = 300) -> list[tuple[str, str]]:
        """One sweeper pass; returns `[(job_id, action)]`."""
        rows = await self._call("select * from qt_sweep(%s, %s)", (self.settings.max_attempts, reenqueue_after_s))
        ids = [r["id"] for r in rows if r["action"] in ("requeue", "enqueue")]
        if ids and self.queue is not None:
            for jid in ids:
                await self.queue.enqueue(jid)
            await self._call("select qt_mark_enqueued(%s)", (ids,))
        return [(r["id"], r["action"]) for r in rows]

    async def maintenance_once(self, *, daily: bool | None = None) -> dict[str, Any]:
        """Upload verification and expiry, idempotency pruning, usage push; once per UTC day retention and the
        storage meter."""
        out: dict[str, Any] = {
            "verified": await maintenance.verify_uploads(self.pool, self.store),
            "expired_uploads": await maintenance.expire_pending_uploads(
                self.pool, self.store, self.settings.pending_upload_ttl_s
            ),
            "pruned": await maintenance.prune_idempotency(self.pool),
            "pushed": await maintenance.push_usage(self.pool, self.biller),
        }
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if daily or (daily is None and self._last_daily != today):
            out["retention"] = await maintenance.run_retention(self.pool, self.store)
            out["storage"] = await maintenance.storage_meter(self.pool, self.store, today)
            self._last_daily = today
        return out

    async def _every(self, interval: float, fn: Callable[[], Awaitable[Any]], stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await fn()
            except Exception:
                log.exception("background pass %s failed", getattr(fn, "__name__", "?"))
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _touch_health(self) -> None:
        """Touches `WORKER_HEALTH_FILE` (container health check) when configured."""
        if self.settings.worker_health_file:
            Path(self.settings.worker_health_file).touch()

    async def serve(self, stop: asyncio.Event) -> None:
        """Runs the consumer, the sweeper and maintenance until `stop` is set."""
        if self.queue is None:
            raise RuntimeError("worker needs a queue")
        await asyncio.gather(
            self.queue.run(self.handle, concurrency=self.settings.worker_concurrency, stop=stop),
            self._every(10.0, self._touch_health, stop),
            self._every(self.settings.sweep_interval_s, self.sweep_once, stop),
            self._every(self.settings.maintenance_interval_s, self.maintenance_once, stop),
        )


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
