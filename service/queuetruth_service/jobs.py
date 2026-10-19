"""Job creation and enqueueing (SPEC 8.3): the job row is inserted in the request transaction (with the idempotency
record); after commit the job id is enqueued and `enqueued_at` set (in a transaction bound to the caller's key: the
api role has no cross-tenant job functions). A lost enqueue is recovered by the sweeper."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from queuetruth_service.context import ServiceContext
from queuetruth_service.db import Binding, tenant_tx
from queuetruth_service.documents import job_doc
from queuetruth_service.ids import new_id

log = logging.getLogger("queuetruth_service.jobs")
TERMINAL = ("succeeded", "failed", "canceled", "expired")


def create_job(
    conn: psycopg.Connection,
    tenant_id: str,
    kind: str,
    spec: dict[str, Any],
    resource_type: str,
    resource_id: str,
) -> dict[str, Any]:
    """Inserts a queued job and returns its `qt.job` document."""
    row = (
        conn.cursor(row_factory=dict_row)
        .execute(
            "insert into jobs (id, tenant_id, kind, spec, resource_type, resource_id) values (%s, %s, %s, %s, %s, %s) "
            "returning *",
            (new_id("job"), tenant_id, kind, Jsonb(spec), resource_type, resource_id),
        )
        .fetchone()
    )
    assert row is not None
    return job_doc(row)


def enqueue_hook(c: ServiceContext, job_id: str, auth: Binding) -> Callable[[], None]:
    """Post-commit hook that enqueues `job_id` and then records `enqueued_at` in a transaction bound to the caller's
    key (errors are logged; the sweeper retries a lost enqueue)."""

    def hook() -> None:
        if c.enqueue is None:
            return
        try:
            c.enqueue(job_id)
        except Exception:
            log.exception("enqueue failed for %s; the sweeper will retry", job_id)
            return
        if c.pool is None:
            return
        try:
            with tenant_tx(c.pool, auth) as conn:
                conn.execute(
                    "update jobs set enqueued_at = now() where id = %s and tenant_id = %s and state = 'queued'",
                    (job_id, auth.tenant_id),
                )
        except Exception:
            log.exception("could not record the enqueue of %s; the sweeper will re-enqueue it", job_id)

    return hook
