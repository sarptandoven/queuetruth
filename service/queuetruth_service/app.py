"""FastAPI application (SPEC 4.5): `/v1` routers, problem handlers, the edge middleware and the OpenAPI 3.1 document
at `/v1/openapi.json`. `create_app()` with no arguments reads `QT_*` settings and opens its pool in the lifespan;
tests inject a pool and a store. CORS is disabled (no CORS middleware). The api refuses to start unless it connects
as the unprivileged `qt_app` role (security review H1)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from queuetruth.schemas import models as m
from queuetruth_service import __version__, logs
from queuetruth_service.context import ServiceContext
from queuetruth_service.problems import PROBLEM_MEDIA, EdgeMiddleware, install_handlers
from queuetruth_service.ratelimit import FailureThrottle
from queuetruth_service.routers import account, datasets, jobs, keys, meta, runs, uploads
from queuetruth_service.routers import store as store_router
from queuetruth_service.settings import Settings
from queuetruth_service.store import ObjectStore, make_store

PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": m.Problem, "description": desc}
    for code, desc in (
        (401, "Missing or invalid API key"),
        (403, "Insufficient scope"),
        (404, "Not found (unknown or other-tenant id)"),
        (409, "Conflict"),
        (413, "Payload too large"),
        (422, "Validation error"),
        (429, "Rate limited"),
        (500, "Internal error"),
        (503, "Dependency unavailable"),
    )
}
PUBLIC_PATHS = ("/v1/health", "/v1/openapi.json")


def create_app(
    settings: Settings | None = None,
    *,
    pool: ConnectionPool | None = None,
    store: ObjectStore | None = None,
    enqueue: Callable[[str], None] | None = None,
    connect: bool = True,
) -> FastAPI:
    """Builds the app. Without `pool`, the lifespan opens one from `QT_DATABASE_URL_APP` when `connect` is true; either
    way the startup refuses a database role that could bypass tenant isolation (`db.pool.verify_role`)."""
    s = settings or Settings()
    c = ServiceContext(
        settings=s,
        pool=pool,
        store=store or make_store(s),
        enqueue=enqueue,
        auth_throttle=FailureThrottle(*s.auth_failure_limit),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = None
        if c.pool is None and connect:
            from queuetruth_service.db.pool import make_pool
            from queuetruth_service.queue import enqueue_sync

            logs.setup(s.log_level)
            s.require("api")
            # make_pool refuses superusers, BYPASSRLS roles, schema owners and the worker role (security review H1)
            owned = make_pool(s.app_dsn, max_size=s.db_pool_max, purpose="api")
            c.pool = owned
            if c.enqueue is None:
                c.enqueue = lambda job_id: enqueue_sync(owned, job_id)
        elif c.pool is not None and connect:
            from queuetruth_service.db.pool import verify_role

            with c.pool.connection() as conn:
                verify_role(conn, "api")
        try:
            yield
        finally:
            if owned is not None:
                owned.close()
                c.pool = None

    app = FastAPI(
        title="queuetruth",
        version=__version__,
        description="queuetruth HTTP API v1 (SPEC 4.5). Errors are RFC 9457 `application/problem+json`. "
        "RateLimit headers follow draft-ietf-httpapi-ratelimit-headers-11 (draft-based).",
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        responses=PROBLEM_RESPONSES,
        lifespan=lifespan,
    )
    app.state.ctx = c
    install_handlers(app)
    for r in (meta, keys, uploads, datasets, runs, jobs, account, store_router):
        app.include_router(r.router)
    app.add_middleware(EdgeMiddleware, part_cap=s.part_size_bytes)
    install_openapi(app)
    return app


def install_openapi(app: FastAPI) -> None:
    """Problem schemas under `application/problem+json`, no FastAPI validation-error schemas, bearer/basic security."""

    def custom() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        doc = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes, servers=[{"url": "/"}]
        )
        for path, item in doc["paths"].items():
            for op in item.values():
                if not isinstance(op, dict):
                    continue
                for resp in op.get("responses", {}).values():
                    content = resp.get("content", {})
                    js = content.get("application/json")
                    if js and str(js.get("schema", {}).get("$ref", "")).endswith("/Problem"):
                        resp["content"] = {PROBLEM_MEDIA: content.pop("application/json")}
                if path in PUBLIC_PATHS or path.startswith("/v1/_store/"):
                    op["security"] = []
                    for code in ("401", "429") if path.startswith("/v1/_store/") else ("401", "403", "429"):
                        op.get("responses", {}).pop(code, None)
                else:
                    op["security"] = [{"bearer": []}, {"basic": []}]
        schemas = doc.setdefault("components", {}).setdefault("schemas", {})
        schemas.pop("HTTPValidationError", None)
        schemas.pop("ValidationError", None)
        doc["components"]["securitySchemes"] = {
            "bearer": {"type": "http", "scheme": "bearer", "description": "API key"},
            "basic": {"type": "http", "scheme": "basic", "description": "API key as user name, empty password"},
        }
        app.openapi_schema = doc
        return doc

    app.openapi = custom  # type: ignore[method-assign]


def _lazy_app() -> FastAPI:
    return create_app()


def __getattr__(name: str) -> Any:
    """`queuetruth_service.app:app` for uvicorn, built on first access from the environment."""
    if name == "app":
        global app
        app = _lazy_app()
        return app
    raise AttributeError(name)
