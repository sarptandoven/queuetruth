"""Stripe-style idempotency (SPEC 8.6). `request_hash` = sha256 of method, path and the canonical JSON body (sorted
keys, no whitespace). Transaction 1 inserts an `in_progress` record and commits (concurrent duplicates then see it:
409 `idempotency-in-flight`); transaction 2 runs the handler and stores status, body and headers with the handler's
writes; the post-commit hook (enqueue) runs last. Same key + same hash replays the stored response with
`Idempotent-Replayed: true`; a different hash is 422 `idempotency-key-reused`. Handler errors (problems raised with
any 4xx status, and 5xx) roll back the handler's transaction and delete the record, so nothing was committed, nothing
is stored and the client may retry with the same key once the cause is fixed (SPEC 8.6, as ratified: storing them
would pin a transient 404/409 for 24 h with no effect to protect). A 4xx the handler *returns* as its outcome
(e.g. `upload-size-mismatch`, which commits the failed upload) is stored and replayed like a success. An
`in_progress` record older than 60 s with the same hash is treated as abandoned and taken over. Secrets (`secret`
fields) are never stored: a replay returns null. Records are pruned after 24 h by the worker."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Annotated, Any

import psycopg
from fastapi import Header, Request
from fastapi.responses import JSONResponse, Response
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from queuetruth_service.auth import Auth
from queuetruth_service.context import ctx
from queuetruth_service.db import tenant_tx
from queuetruth_service.problems import PROBLEM_MEDIA, ProblemError, validation

TAKEOVER_S = 60
IdemKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
OptIdemKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)]

#: Handler result: (status, body or None, headers, post-commit hook or None).
Outcome = tuple[int, Any, dict[str, str], Callable[[], None] | None]


def canonical_body(body: bytes) -> str:
    """Canonical JSON text of a request body (empty body = `null`)."""
    try:
        value = json.loads(body) if body else None
    except (ValueError, UnicodeDecodeError):
        return body.decode("utf-8", "replace")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_hash(method: str, path: str, body: bytes) -> str:
    """sha256 of method, path and canonical body."""
    return hashlib.sha256(f"{method}\n{path}\n{canonical_body(body)}".encode()).hexdigest()


def _redact(body: Any) -> Any:
    if isinstance(body, dict):
        return {k: (None if k == "secret" else v) for k, v in body.items()}
    return body


def _response(status: int, body: Any, headers: dict[str, str]) -> Response:
    if body is None:
        return Response(status_code=status, headers=headers)
    if isinstance(body, dict) and body.get("schema") == "qt.problem":
        return JSONResponse(body, status_code=status, headers=headers, media_type=PROBLEM_MEDIA)
    return JSONResponse(body, status_code=status, headers=headers)


async def run(
    request: Request, auth: Auth, key: str | None, execute: Callable[[psycopg.Connection], Outcome]
) -> Response:
    """Runs `execute(conn)` once per (tenant, key); without a key it just runs it in a tenant transaction."""
    body = await request.body()
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(_run_sync, request, auth, key, execute, body)


def _run_sync(
    request: Request, auth: Auth, key: str | None, execute: Callable[[psycopg.Connection], Outcome], body: bytes
) -> Response:
    pool = ctx(request).db()
    if key is None:
        with tenant_tx(pool, auth) as conn:
            status, out, headers, after = execute(conn)
        if after:
            after()
        return _response(status, out, headers)
    if len(key) > 255 or not key:
        raise validation(["header", "Idempotency-Key"], "must be 1..255 characters")
    h = request_hash(request.method, request.url.path, body)
    with tenant_tx(pool, auth) as conn:
        won = conn.execute(
            """insert into idempotency_keys as k (tenant_id, key, method, path, request_hash, state)
               values (%s, %s, %s, %s, %s, 'in_progress')
               on conflict (tenant_id, key) do update set locked_at = now()
                 where k.state = 'in_progress' and k.request_hash = excluded.request_hash
                   and k.locked_at < now() - make_interval(secs => %s)
               returning 1""",
            (auth.tenant_id, key, request.method, request.url.path, h, TAKEOVER_S),
        ).fetchone()
        prev = None
        if not won:
            prev = (
                conn.cursor(row_factory=dict_row)
                .execute("select * from idempotency_keys where tenant_id = %s and key = %s", (auth.tenant_id, key))
                .fetchone()
            )
    if not won:
        if prev is None:
            raise ProblemError(409, "idempotency-in-flight", "Idempotency key in flight", "QT-USAGE-001")
        if prev["request_hash"] != h:
            raise ProblemError(
                422,
                "idempotency-key-reused",
                "Idempotency key reused",
                "QT-USAGE-001",
                "this Idempotency-Key was used with a different request",
            )
        if prev["state"] == "in_progress":
            raise ProblemError(
                409,
                "idempotency-in-flight",
                "Idempotency key in flight",
                "QT-USAGE-001",
                "a request with this Idempotency-Key is still being processed",
            )
        headers = dict(prev["response_headers"] or {}) | {"Idempotent-Replayed": "true"}
        return _response(prev["response_status"], prev["response_body"], headers)
    try:
        with tenant_tx(pool, auth) as conn:
            status, out, headers, after = execute(conn)
            conn.execute(
                "update idempotency_keys set state = 'complete', response_status = %s, response_body = %s, "
                "response_headers = %s where tenant_id = %s and key = %s",
                (status, Jsonb(_redact(out)) if out is not None else None, Jsonb(headers), auth.tenant_id, key),
            )
    except BaseException:
        with tenant_tx(pool, auth) as conn:
            conn.execute(
                "delete from idempotency_keys where tenant_id = %s and key = %s and state = 'in_progress'",
                (auth.tenant_id, key),
            )
        raise
    if after:
        after()
    return _response(status, out, headers)
