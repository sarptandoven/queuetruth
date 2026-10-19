"""Job transport (SPEC 8.3) on PgQueuer 1.4 in the same Postgres. PgQueuer carries only `{"job_id": ...}` to the
entrypoint `run_job`; the `jobs` table is the source of truth, and duplicate deliveries (PgQueuer re-picks, sweeper
re-enqueues, API retries) are harmless because the claim is a conditional `UPDATE ... WHERE state = 'queued'`.

PgQueuer requires `max_concurrent_tasks >= 2 * batch_size`, so the per-worker concurrency is enforced with a
semaphore; with `WORKER_CONCURRENCY=1` a worker may hold one extra picked (not started) delivery. Fairness across
tenants is enforced by the claim (`qt_claim_job` caps the running jobs of each tenant at `QT_TENANT_MAX_RUNNING`); a
deferred delivery is re-enqueued with a delay (`enqueue(job_id, delay_s)`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Protocol

import psycopg
from pgqueuer import Job, Queries, QueueManager
from pgqueuer.adapters.drivers.psycopg import SyncPsycopgDriver
from pgqueuer.domain.types import QueueExecutionMode
from pgqueuer.queries import SyncQueries
from psycopg_pool import ConnectionPool

ENTRYPOINT = "run_job"
Handler = Callable[[str], Awaitable[None]]


def payload(job_id: str) -> bytes:
    """Queue payload of a job."""
    return json.dumps({"job_id": job_id}).encode()


class JobQueue(Protocol):
    """SPEC 8.3 interface."""

    async def enqueue(self, job_id: str, delay_s: float = 0) -> None: ...
    async def run(self, handler: Handler, *, concurrency: int, stop: asyncio.Event) -> None: ...


class PgQueuerJobQueue:
    """`JobQueue` on PgQueuer (psycopg 3 driver; its connections are autocommit)."""

    def __init__(
        self,
        dsn: str,
        *,
        heartbeat_timeout: timedelta = timedelta(seconds=30),
        dequeue_timeout: timedelta = timedelta(seconds=5),
    ) -> None:
        self.dsn = dsn
        self.heartbeat_timeout = heartbeat_timeout
        self.dequeue_timeout = dequeue_timeout
        self._conn: psycopg.AsyncConnection | None = None
        self._lock = asyncio.Lock()

    async def enqueue(self, job_id: str, delay_s: float = 0) -> None:
        """Enqueues one job id (delivered no earlier than `delay_s` from now)."""
        after = timedelta(seconds=delay_s) if delay_s > 0 else None
        async with self._lock:
            if self._conn is None or self._conn.closed:
                self._conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True)
            await Queries.from_psycopg_connection(self._conn).enqueue(ENTRYPOINT, payload(job_id), execute_after=after)

    async def close(self) -> None:
        """Closes the enqueue connection."""
        if self._conn is not None:
            await self._conn.close()

    async def run(
        self,
        handler: Handler,
        *,
        concurrency: int,
        stop: asyncio.Event,
        mode: QueueExecutionMode = QueueExecutionMode.continuous,
    ) -> None:
        """Consumes deliveries until `stop` is set (or the queue drains in `drain` mode)."""
        conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True)
        qm = QueueManager(Queries.from_psycopg_connection(conn))
        sem = asyncio.Semaphore(concurrency)

        @qm.entrypoint(ENTRYPOINT)
        async def run_job(job: Job) -> None:
            job_id = json.loads(job.payload or b"{}").get("job_id")
            if not isinstance(job_id, str):
                return
            async with sem:
                await handler(job_id)

        async def watch_stop() -> None:
            await stop.wait()
            qm.shutdown.set()

        watcher = asyncio.create_task(watch_stop())
        try:
            await qm.run(
                batch_size=1,
                max_concurrent_tasks=max(2, concurrency),
                mode=mode,
                dequeue_timeout=self.dequeue_timeout,
                heartbeat_timeout=self.heartbeat_timeout,
            )
        finally:
            watcher.cancel()
            await conn.close()


def enqueue_sync(pool: ConnectionPool, job_id: str) -> None:
    """API-side enqueue after commit (sync, on an autocommit pool connection); the caller records `enqueued_at`."""
    with pool.connection() as conn:
        conn.autocommit = True
        try:
            SyncQueries(SyncPsycopgDriver(conn)).enqueue(ENTRYPOINT, payload(job_id))
        finally:
            conn.autocommit = False
