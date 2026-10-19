"""Pools, role checks and tenant-bound transactions (SPEC 8.2, security review H1/M3).

Every request and job transaction starts by *binding* its tenant through a SECURITY DEFINER function that checks
ownership (migration 0005): the api presents the caller's API key (`qt_authenticate(prefix, key_hash)`), the worker
the job attempt it holds (`qt_bind_job`) or, for its maintenance passes only, a tenant id (`qt_bind_tenant`). RLS
compares every row with `qt_current_tenant()`, which only returns a tenant bound that way in the current transaction;
a bare `set_config('app.tenant_id', ...)` binds nothing. Queries also filter by `tenant_id` explicitly, so RLS is not
the only control.

Pools refuse roles that would make RLS moot: a superuser, a role with BYPASSRLS (or a member of one), or a role that
owns tables or functions of the schema (`verify_role`). The api must additionally be unable to bind arbitrary
tenants (it must be `qt_app`, not `qt_worker`), and the worker must be able to claim jobs.

The API uses a synchronous `psycopg_pool.ConnectionPool` (FastAPI runs sync endpoints in a thread pool; a sync pool is
independent of event loops, which matters for test clients that start a loop per request). The worker uses an
`AsyncConnectionPool` on its single event loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from queuetruth_service.logs import tenant_id_var

Purpose = Literal["api", "worker", "any"]

ROLE_CHECK = """
select current_user::text,
  exists (select 1 from pg_roles r
           where pg_has_role(current_user, r.oid, 'MEMBER') and (r.rolsuper or r.rolbypassrls)),
  exists (select 1 from pg_class c
           where c.relnamespace = 'public'::regnamespace and pg_has_role(current_user, c.relowner, 'MEMBER')),
  exists (select 1 from pg_proc p
           where p.pronamespace = 'public'::regnamespace and pg_has_role(current_user, p.proowner, 'MEMBER')),
  coalesce(has_function_privilege(current_user, to_regprocedure('public.qt_bind_tenant(text)'), 'EXECUTE'), false),
  coalesce(has_function_privilege(current_user, to_regprocedure('public.qt_authenticate(text,text)'), 'EXECUTE'),
           false),
  coalesce(has_function_privilege(current_user, to_regprocedure('public.qt_claim_job(text,integer,integer)'),
                                  'EXECUTE'), false)
"""


class RoleError(RuntimeError):
    """The database role is not acceptable for this process (the process refuses to start)."""


class BindingRefused(Exception):
    """The database refused to bind the tenant (revoked key, lost lease, unknown tenant)."""


class Binding(Protocol):
    """Proof of the right to act for one tenant, checked by the database at the start of each transaction."""

    @property
    def tenant_id(self) -> str: ...

    def bind_query(self) -> tuple[str, tuple[Any, ...]]: ...


@dataclass(frozen=True)
class JobBinding:
    """Worker: the tenant of a job this worker holds (state running, attempt fenced)."""

    tenant_id: str
    job_id: str
    attempt: int

    def bind_query(self) -> tuple[str, tuple[Any, ...]]:
        return "select qt_bind_job(%s, %s)", (self.job_id, self.attempt)


@dataclass(frozen=True)
class TenantBinding:
    """Worker maintenance: any existing tenant (only `qt_worker` may execute `qt_bind_tenant`)."""

    tenant_id: str

    def bind_query(self) -> tuple[str, tuple[Any, ...]]:
        return "select qt_bind_tenant(%s)", (self.tenant_id,)


@dataclass(frozen=True)
class KeyBinding:
    """API: the tenant of an unrevoked API key (prefix + HMAC of the presented key)."""

    tenant_id: str
    prefix: str = field(repr=False)
    key_hash: str = field(repr=False)

    def bind_query(self) -> tuple[str, tuple[Any, ...]]:
        return "select tenant_id from qt_authenticate(%s, %s)", (self.prefix, self.key_hash)


def _check_binding(binding: Binding) -> None:
    if isinstance(binding, str) or not hasattr(binding, "bind_query"):
        raise TypeError("tenant transactions need a binding (Auth, JobBinding or TenantBinding), not a tenant id")


def _bound(row: Any, binding: Binding) -> None:
    if not row or row[0] is None or row[0] != binding.tenant_id:
        raise BindingRefused(f"tenant binding refused for {binding.tenant_id}")


def role_problems(row: tuple[Any, ...], purpose: Purpose) -> list[str]:
    """Reasons a role (one `ROLE_CHECK` row) is unacceptable for `purpose`; empty when it is fine."""
    user, privileged, owns_tables, owns_functions, can_bind_any, can_authenticate, can_claim = row
    out = []
    if privileged:
        out.append(f"role {user!r} is (or can become) a superuser or has BYPASSRLS, which turns tenant isolation off")
    if owns_tables or owns_functions:
        out.append(f"role {user!r} owns schema objects (owners can disable row-level security)")
    if purpose == "api":
        if can_bind_any:
            out.append(f"role {user!r} can bind any tenant (that is the worker role); the api must use qt_app")
        if not can_authenticate:
            out.append(f"role {user!r} cannot execute qt_authenticate (run `queuetruth-service migrate`?)")
    if purpose == "worker" and not (can_bind_any and can_claim):
        out.append(f"role {user!r} cannot claim jobs; the worker must use qt_worker (run migrations?)")
    return out


def verify_role(conn: psycopg.Connection, purpose: Purpose) -> str:
    """Raises `RoleError` unless the connection's role is an unprivileged app role fit for `purpose`."""
    row = conn.execute(ROLE_CHECK).fetchone()
    conn.rollback()
    assert row is not None
    problems = role_problems(tuple(row), purpose)
    if problems:
        raise RoleError("refusing to start: " + "; ".join(problems))
    return str(row[0])


async def verify_role_async(conn: psycopg.AsyncConnection, purpose: Purpose) -> str:
    """Async variant of `verify_role`."""
    row = await (await conn.execute(ROLE_CHECK)).fetchone()
    await conn.rollback()
    assert row is not None
    problems = role_problems(tuple(row), purpose)
    if problems:
        raise RoleError("refusing to start: " + "; ".join(problems))
    return str(row[0])


def _configure(conn: psycopg.Connection) -> None:
    conn.execute("set application_name = 'queuetruth-api'")
    conn.commit()


def make_pool(
    dsn: str, *, max_size: int = 20, min_size: int = 1, open_: bool = True, purpose: Purpose = "api"
) -> ConnectionPool:
    """Synchronous pool for the API process; refuses privileged roles (`verify_role`)."""
    pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False, configure=_configure, timeout=10)
    if open_:
        pool.open(wait=True, timeout=10)
        try:
            with pool.connection() as conn:
                verify_role(conn, purpose)
        except BaseException:
            pool.close()
            raise
    return pool


async def make_async_pool(
    dsn: str, *, max_size: int = 10, min_size: int = 1, purpose: Purpose = "worker"
) -> AsyncConnectionPool:
    """Asynchronous pool for the worker process; refuses privileged roles (`verify_role`)."""
    pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False, timeout=10)
    await pool.open(wait=True, timeout=10)
    try:
        async with pool.connection() as conn:
            await verify_role_async(conn, purpose)
    except BaseException:
        await pool.close()
        raise
    return pool


@contextmanager
def tenant_tx(pool: ConnectionPool, binding: Binding) -> Iterator[psycopg.Connection]:
    """A transaction bound to `binding`'s tenant (committed on exit, rolled back on error); `BindingRefused` when
    the database does not accept the binding."""
    _check_binding(binding)
    tenant_id_var.set(binding.tenant_id)
    query, params = binding.bind_query()
    with pool.connection() as conn, conn.transaction():
        _bound(conn.execute(query, params).fetchone(), binding)
        yield conn


@asynccontextmanager
async def tenant_tx_async(pool: AsyncConnectionPool, binding: Binding) -> AsyncIterator[psycopg.AsyncConnection]:
    """Async variant of `tenant_tx`."""
    _check_binding(binding)
    query, params = binding.bind_query()
    async with pool.connection() as conn, conn.transaction():
        _bound(await (await conn.execute(query, params)).fetchone(), binding)
        yield conn
