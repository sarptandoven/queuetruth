"""Job queue and worker (SPEC 8.3, O-Q-1): queued -> running -> succeeded through PgQueuer; failures; cancel of
queued and running jobs; timeout; worker crash (lease expiry) requeued, failed after 3 attempts; lost enqueue
recovered by the sweeper; duplicate deliveries run once; fenced finish; cancel against a held job row lock (bounded
wait, never a hang) and the heartbeat's bounded lock wait."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from svc_support import make_svc, seed_dataset

from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.queue import PgQueuerJobQueue, enqueue_sync
from queuetruth_service.routers.jobs import CANCEL_LOCK_TIMEOUT_MS
from queuetruth_service.worker import ClaimedJob, Outcome, Worker, _Fenced

pytestmark = [pytest.mark.requires_postgres, pytest.mark.asyncio, pytest.mark.deadline(120)]
FAKES = {
    "replay": "queuetruth_service.testing:fake_replay",
    "dataset_validate": "queuetruth_service.testing:fake_ingest",
}


class Env:
    def __init__(self, svc, pool, queue, worker):
        self.svc, self.pool, self.queue, self.worker = svc, pool, queue, worker
        self.stop = asyncio.Event()
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.queue.run(self.worker.handle, concurrency=2, stop=self.stop))

    async def until(self, pred, timeout: float = 30.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            await asyncio.sleep(0.05)
        return False

    async def api(self, method: str, url: str, *, timeout: float = 30.0, **kw):
        """An API call made off the event loop, bounded by `timeout`. The TestClient blocks its calling thread until
        the response; called on this loop it would also freeze the in-loop worker, and a worker transaction that
        already holds the job row lock could then never send its COMMIT while the request waits for that lock (the
        hang this replaces). Use it for every call that may lock a row the running worker touches (cancel)."""
        return await asyncio.wait_for(asyncio.to_thread(getattr(self.svc.client, method), url, **kw), timeout)

    def state(self, job_id: str) -> str:
        return self.svc.db.q("select state from jobs where id = %s", (job_id,))[0][0]

    def job(self, job_id: str) -> dict:
        return self.svc.client.get(f"/v1/jobs/{job_id}", headers=self.svc.h()).json()


def build_worker(svc, pool, queue, targets=None, **settings_kw) -> Worker:
    s = svc.settings.model_copy(update={"lease_s": 3, "heartbeat_s": 0.2, **settings_kw})
    return Worker(s, pool, svc.store, queue, targets=FAKES | (targets or {}))


@pytest_asyncio.fixture
async def env(db, tmp_path):
    gen = make_svc(db, tmp_path)
    svc = next(gen)
    svc.client.app.state.ctx.enqueue = lambda job_id: enqueue_sync(svc.pool, job_id)
    pool = await make_async_pool(db.worker, max_size=8)
    queue = PgQueuerJobQueue(db.worker, heartbeat_timeout=timedelta(seconds=2), dequeue_timeout=timedelta(seconds=1))
    e = Env(svc, pool, queue, build_worker(svc, pool, queue))
    yield e
    e.stop.set()
    if e.task is not None:
        await asyncio.wait_for(e.task, 15)
    await queue.close()
    await pool.close()
    gen.close()


def lock_waiters(svc) -> int:
    """Sessions of the test database waiting for a lock (pg_locks and datname are visible to any role)."""
    return svc.db.q(
        "select count(*) from pg_locks l join pg_stat_activity a on a.pid = l.pid "
        "where not l.granted and a.datname = current_database()"
    )[0][0]


def submit(svc, ds: str, key: str, **extra) -> str:
    r = svc.client.post("/v1/replays", json={"dataset": {"dataset_id": ds}} | extra, headers=svc.h(Idempotency_Key=key))
    assert r.status_code == 202, r.text
    return r.json()["job"]["id"]


async def test_o_q_1_lifecycle_succeeded_files_usage_summary(env):
    svc = env.svc
    ds = seed_dataset(svc)
    job = submit(svc, ds, "life")
    assert env.job(job)["state"] == "queued"
    assert svc.db.q("select enqueued_at is not null from jobs where id = %s", (job,)) == [(True,)]
    seen: list[str] = []
    orig = env.worker.claim

    async def spy(job_id):
        claimed = await orig(job_id)
        if claimed:
            seen.append(env.state(job_id))
        return claimed

    env.worker.claim = spy
    env.start()
    assert await env.until(lambda: env.state(job) == "succeeded")
    assert seen == ["running"]
    doc = env.job(job)
    assert doc["progress"] == {"fraction": 1.0, "stage": "succeeded"}
    assert doc["started_at"] and doc["finished_at"] and doc["files_expire_at"]
    assert doc["usage"]["replay_records"] == 2000.0 and doc["usage"]["cpu_ms"] >= 0
    man = svc.client.get(f"/v1/jobs/{job}/files", headers=svc.h()).json()
    names = [f["name"] for f in man["files"]]
    assert names == ["inputs.json", "nested/data.bin"]
    got = svc.client.get(f"/v1/jobs/{job}/files/inputs.json", headers=svc.h()).json()
    assert got["inputs"] == ["manifest.json", "mbo/date=2026-01-02/instrument_id=1/part-00000.parquet"]
    assert got["cwd_is_workdir"] is True
    rp = svc.client.get("/v1/replays", headers=svc.h()).json()["data"][0]
    assert rp["summary"]["stats"]["records"] == 2000 and rp["summary"]["spec"]["dataset"] == {"dataset_id": ds}
    idents = sorted(i for (i,) in svc.db.q("select identifier from usage_events where job_id = %s", (job,)))
    assert idents == [f"{job}:cpu_ms", f"{job}:replay_records"]
    assert svc.db.q("select run_id from jobs where id = %s", (job,)) == [("0123456789abcdef",)]
    assert not list(Path("/tmp").glob("qtjob-*")) or all(
        p.stat().st_mtime < time.time() - 60 for p in Path("/tmp").glob("qtjob-*")
    )


async def test_o_q_1_failures_are_problems_without_internals(env):
    svc = env.svc
    env.worker.targets["replay"] = "queuetruth_service.testing:fake_fail"
    ds = seed_dataset(svc)
    job = submit(svc, ds, "f1")
    env.start()
    assert await env.until(lambda: env.state(job) == "failed")
    err = env.job(job)["error"]
    assert err["code"] == "QT-DATA-001" and err["status"] == 422 and err["type"].endswith("/job-failed")
    assert "qtjob-" not in err["detail"] and "/tmp" not in err["detail"]
    env.worker.targets["replay"] = "queuetruth_service.testing:fake_crash"
    job2 = submit(svc, ds, "f2")
    assert await env.until(lambda: env.state(job2) == "failed")
    err = env.job(job2)["error"]
    assert err["code"] == "QT-ENGINE-001" and "secret-internal-detail" not in json.dumps(err)
    assert svc.db.q("select count(*) from usage_events") == [(0,)]
    assert svc.db.q("select count(*) from job_files") == [(0,)]


async def test_o_q_1_cancel_queued_and_running(env):
    svc = env.svc
    ds = seed_dataset(svc)
    queued = submit(svc, ds, "cq")
    r = await env.api("post", f"/v1/jobs/{queued}/cancel", headers=svc.h(Idempotency_Key="c1"))
    assert r.status_code == 200 and r.json()["state"] == "canceled"
    env.worker.targets["replay"] = "queuetruth_service.testing:fake_sleep"
    running = submit(svc, ds, "cr", seed=60)
    env.start()
    assert await env.until(lambda: env.state(running) == "running")
    await asyncio.sleep(1.0)  # child process started
    r = await env.api("post", f"/v1/jobs/{running}/cancel", headers=svc.h(Idempotency_Key="c2"))
    assert r.status_code == 200 and r.json()["state"] == "running"
    t0 = time.monotonic()
    assert await env.until(lambda: env.state(running) == "canceled", timeout=15)
    assert time.monotonic() - t0 < 10
    assert env.state(queued) == "canceled"
    r = await env.api("post", f"/v1/jobs/{running}/cancel", headers=svc.h(Idempotency_Key="c3"))
    assert r.status_code == 409


async def _claimed(env, key: str) -> ClaimedJob:
    svc = env.svc
    svc.client.app.state.ctx.enqueue = None  # no queue delivery: the test drives the claim itself
    job = submit(svc, seed_dataset(svc), key)
    claimed = await env.worker.claim(job)
    assert claimed is not None and env.state(job) == "running"
    return claimed


async def test_cancel_waits_for_a_worker_transaction_on_the_same_loop(env):
    """Regression for the intermittent hang of `test_o_q_1_cancel_queued_and_running` (18 min): a heartbeat
    transaction on this event loop held the job row lock between its UPDATE and its COMMIT when the test called the
    cancel synchronously on the same loop; the cancel waited for the lock, the COMMIT waited for the loop. Here the
    interleaving is forced: the holder takes the row lock, the barrier sees the cancel waiting for it, and only then
    the holder commits, which needs this loop to run while the cancel is in flight."""
    svc = env.svc
    job = await _claimed(env, "hold")
    holder = await psycopg.AsyncConnection.connect(svc.db.worker)
    try:
        await holder.execute("select * from qt_heartbeat(%s, %s, %s)", (job.id, job.attempts, 30))  # lock, no COMMIT
        call = asyncio.create_task(env.api("post", f"/v1/jobs/{job.id}/cancel", headers=svc.h(Idempotency_Key="h1")))
        assert await env.until(lambda: lock_waiters(svc) >= 1, timeout=10)  # barrier: the cancel waits on the row
        assert not call.done()
        await holder.commit()
        r = await call
    finally:
        await holder.close()
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "running"
    assert svc.db.q("select cancel_requested from jobs where id = %s", (job.id,)) == [(True,)]


async def test_cancel_fails_fast_while_the_job_row_stays_locked(env):
    """A worker that stalls inside a transaction on the job row (stopped process, partition) must not pin a cancel
    forever: after `CANCEL_LOCK_TIMEOUT_MS` the cancel answers 503 with Retry-After, changes nothing and keeps no
    idempotency record, so the same key succeeds once the lock is gone."""
    svc = env.svc
    job = await _claimed(env, "stall")
    holder = await psycopg.AsyncConnection.connect(svc.db.worker)
    try:
        await holder.execute("select * from qt_heartbeat(%s, %s, %s)", (job.id, job.attempts, 30))  # never commits
        t0 = time.monotonic()
        r = await env.api("post", f"/v1/jobs/{job.id}/cancel", headers=svc.h(Idempotency_Key="s1"))
        elapsed = time.monotonic() - t0
        assert r.status_code == 503, r.text
        assert r.headers["Retry-After"] == "1" and r.json()["code"] == "QT-ENGINE-001"
        assert CANCEL_LOCK_TIMEOUT_MS / 1000 - 0.5 < elapsed < CANCEL_LOCK_TIMEOUT_MS / 1000 + 5
        assert svc.db.q("select cancel_requested from jobs where id = %s", (job.id,)) == [(False,)]
        await holder.rollback()
    finally:
        await holder.close()
    r = await env.api("post", f"/v1/jobs/{job.id}/cancel", headers=svc.h(Idempotency_Key="s1"))
    assert r.status_code == 200 and r.json()["state"] == "running"
    assert "Idempotent-Replayed" not in r.headers
    assert svc.db.q("select cancel_requested from jobs where id = %s", (job.id,)) == [(True,)]


async def test_heartbeat_waits_at_most_two_seconds_for_the_job_row(env):
    """The heartbeat runs in autocommit (no row lock held across a round trip) and gives up after 2 s behind another
    transaction on the row (`lock_timeout` on `qt_heartbeat`); its pooled connection comes back usable."""
    svc = env.svc
    job = await _claimed(env, "hb")
    with psycopg.connect(svc.db.owner) as holder:
        holder.execute("select 1 from jobs where id = %s for no key update", (job.id,))
        t0 = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await asyncio.wait_for(env.worker.heartbeat_once(job), 10)
        assert time.monotonic() - t0 < 5
        holder.rollback()
    assert await asyncio.wait_for(env.worker.heartbeat_once(job), 10) == (True, False)
    async with env.pool.connection() as conn:
        assert not conn.autocommit


async def test_job_timeout_kills_child(env):
    """The timeout is per job kind (SPEC 8.1, open conflict 9): the replay limit applies, not the global default."""
    svc = env.svc
    env.worker = build_worker(
        svc,
        env.pool,
        env.queue,
        {"replay": "queuetruth_service.testing:fake_sleep"},
        job_timeout_s=3600,
        job_timeouts_s={"replay": 1.5, "calibration": 3600},
    )
    assert env.worker.settings.job_timeout_for("replay") == 1.5
    ds = seed_dataset(svc)
    job = submit(svc, ds, "to", seed=60)
    env.start()
    assert await env.until(lambda: env.state(job) == "failed", timeout=20)
    assert env.job(job)["error"]["detail"] == "job timed out"


async def test_o_q_1_lost_enqueue_recovered_by_sweeper(env):
    svc = env.svc
    ds = seed_dataset(svc)
    svc.client.app.state.ctx.enqueue = None  # API "crashed" between commit and enqueue
    job = submit(svc, ds, "lost")
    assert svc.db.q("select enqueued_at from jobs where id = %s", (job,)) == [(None,)]
    assert await env.worker.sweep_once() == [(job, "enqueue")]
    assert await env.worker.sweep_once() == []  # enqueued_at now set
    env.start()
    assert await env.until(lambda: env.state(job) == "succeeded")


async def test_duplicate_deliveries_run_once(env):
    svc = env.svc
    ds = seed_dataset(svc)
    job = submit(svc, ds, "dup")
    for _ in range(3):
        await env.queue.enqueue(job)
    env.start()
    assert await env.until(
        lambda: (
            env.state(job) == "succeeded"
            and svc.db.q("select count(*) from pgqueuer where status in ('queued', 'picked')") == [(0,)]
        )
    )
    assert svc.db.q("select attempts from jobs where id = %s", (job,)) == [(1,)]
    assert svc.db.q("select count(*) from usage_events where job_id = %s and meter = 'replay_records'", (job,)) == [
        (1,)
    ]


async def test_fenced_finish_rejects_stale_attempt(env):
    svc = env.svc
    ds = seed_dataset(svc)
    svc.client.app.state.ctx.enqueue = None
    job = submit(svc, ds, "fence")
    first = await env.worker.claim(job)
    assert first is not None and first.attempts == 1
    assert await env.worker.claim(job) is None
    svc.db.q("update jobs set state = 'queued' where id = %s", (job,))  # lease expired + requeued
    second = await env.worker.claim(job)
    assert second is not None and second.attempts == 2
    with pytest.raises(_Fenced):
        await env.worker.finish(first, Outcome("succeeded", summary={}))
    await env.worker.finish(second, Outcome("failed", error=None))
    assert env.state(job) == "failed"


async def test_o_q_1_worker_crash_requeued_then_failed_after_3_attempts(env, db):
    svc = env.svc
    ds = seed_dataset(svc)
    svc.client.app.state.ctx.enqueue = None
    job = submit(svc, ds, "crash", seed=60)
    script = (
        "import asyncio, sys\n"
        "from queuetruth_service.db.pool import make_async_pool\n"
        "from queuetruth_service.settings import Settings\n"
        "from queuetruth_service.store.local import LocalStore\n"
        "from queuetruth_service.worker import Worker\n"
        "async def main():\n"
        "    s = Settings(store_root=sys.argv[2], lease_s=2, heartbeat_s=0.5)\n"
        "    pool = await make_async_pool(sys.argv[1])\n"
        "    w = Worker(s, pool, LocalStore(sys.argv[2], s.signing_secret), None,\n"
        "               targets={'replay': 'queuetruth_service.testing:fake_sleep'})\n"
        "    print('READY', flush=True)\n"
        "    await w.handle(sys.argv[3])\n"
        "asyncio.run(main())\n"
    )
    env_vars = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    proc = subprocess.Popen(
        [sys.executable, "-c", script, db.worker, svc.settings.store_root, job],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env_vars,
    )
    try:
        assert await env.until(lambda: env.state(job) == "running", timeout=20)
        await asyncio.sleep(1.0)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)  # the worker and its job child die together
        proc.wait(5)
    assert svc.db.q("select attempts from jobs where id = %s", (job,)) == [(1,)]
    assert await env.worker.sweep_once() == []  # lease still valid right after the kill
    assert await env.until(lambda: svc.db.q("select lease_until < now() from jobs where id = %s", (job,)) == [(True,)])
    assert await env.worker.sweep_once() == [(job, "requeue")]
    assert env.state(job) == "queued"
    # attempt 2 succeeds on a healthy worker
    env.start()
    assert await env.until(lambda: env.state(job) == "succeeded")
    assert svc.db.q("select attempts from jobs where id = %s", (job,)) == [(2,)]
    # a job whose lease expired on its 3rd attempt is failed ("worker lost")
    job3 = submit(svc, ds, "crash3")
    svc.db.q(
        "update jobs set state = 'running', attempts = 3, lease_until = now() - interval '1 second' where id = %s",
        (job3,),
    )
    assert (job3, "failed") in await env.worker.sweep_once()
    err = env.job(job3)["error"]
    assert err["title"] == "worker lost" and err["code"] == "QT-ENGINE-001"
    from queuetruth.schemas.models import Job

    Job.model_validate(env.job(job3))


async def test_claimed_job_shape():
    assert set(ClaimedJob.__dataclass_fields__) == {
        "id", "tenant_id", "kind", "spec", "attempts", "resource_type", "resource_id"
    }  # fmt: skip


async def test_pgqueuer_requires_autocommit(db):
    from pgqueuer import Queries

    async with await psycopg.AsyncConnection.connect(db.app) as c:
        with pytest.raises(RuntimeError, match="autocommit"):
            Queries.from_psycopg_connection(c)
