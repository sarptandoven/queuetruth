"""RFC 9457 problem details (`application/problem+json`, document `qt.problem`) and the edge middleware.

`EdgeMiddleware` is a pure ASGI middleware outside every FastAPI layer: it mints `X-Request-Id`, adds
`nosniff`/`no-store` and late headers from `scope["state"]["extra_headers"]` (rate limits) to every response,
enforces the 1 MiB JSON body cap (413) and turns any unhandled exception into a 500 `internal` problem without
a stack trace (Starlette's own `Exception` handler runs outside user middleware and would drop the headers).
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from queuetruth_service.db.pool import BindingRefused
from queuetruth_service.ids import ulid
from queuetruth_service.logs import request_id_var, tenant_id_var

PROBLEM_BASE = "https://queuetruth.dev/problems/"
PROBLEM_MEDIA = "application/problem+json"
MAX_JSON_BODY = 1 << 20
log = logging.getLogger("queuetruth_service.http")


class ProblemError(Exception):
    """Raised anywhere in a request to answer with a problem document."""

    def __init__(
        self,
        status: int,
        slug: str,
        title: str,
        code: str = "QT-USAGE-001",
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(title)
        self.status, self.slug, self.title, self.code = status, slug, title, code
        self.detail, self.headers, self.errors = detail, dict(headers or {}), errors


def not_found(what: str = "resource") -> ProblemError:
    """404 for unknown and other-tenant ids alike (never 403)."""
    return ProblemError(404, "not-found", "Not found", "QT-USAGE-001", f"no such {what}")


def validation(loc: list[str | int], msg: str) -> ProblemError:
    """422 `validation-error` with one `errors` entry."""
    return ProblemError(
        422, "validation-error", "Request validation failed", "QT-CONFIG-001", errors=[{"loc": loc, "msg": msg}]
    )


def problem_body(
    request_id: str,
    status: int,
    slug: str,
    title: str,
    code: str,
    detail: str | None = None,
    instance: str | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A `qt.problem` document."""
    body: dict[str, Any] = {
        "schema": "qt.problem",
        "schema_version": 1,
        "type": PROBLEM_BASE + slug,
        "title": title,
        "status": status,
        "code": code,
        "request_id": request_id,
    }
    if detail is not None:
        body["detail"] = detail
    if instance is not None:
        body["instance"] = instance
    if errors is not None:
        body["errors"] = errors
    return body


def problem_response(request_id: str, status: int, slug: str, title: str, code: str, **kw: Any) -> JSONResponse:
    """A problem as a response."""
    headers = kw.pop("headers", None)
    return JSONResponse(
        problem_body(request_id, status, slug, title, code, **kw),
        status_code=status,
        media_type=PROBLEM_MEDIA,
        headers=headers,
    )


def request_id(request: Request) -> str:
    """The request id minted by `EdgeMiddleware`."""
    return getattr(request.state, "request_id", None) or "req_unknown"


class _BodyTooLarge(Exception):
    pass


class EdgeMiddleware:
    """Outermost ASGI middleware (see module docstring). `/v1/_store/` bodies are capped by `part_cap` (the part
    size) instead; the store endpoint then enforces each part's exact length."""

    def __init__(self, app: Any, part_cap: int = 1 << 30) -> None:
        self.app = app
        self.part_cap = part_cap

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = f"req_{ulid()}"
        state = scope.setdefault("state", {})
        state["request_id"] = rid
        token = request_id_var.set(rid)
        tenant_token = tenant_id_var.set(None)
        started = False
        fixed = [
            (b"x-request-id", rid.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"cache-control", b"no-store"),
        ]

        async def send_wrapper(msg: dict[str, Any]) -> None:
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
                have = {k.lower() for k, _ in msg.get("headers", [])}
                late = [
                    (k.lower().encode(), str(v).encode())
                    for k, v in state.get("extra_headers", {}).items()
                    if k.lower().encode() not in have
                ]
                msg["headers"] = [h for h in msg.get("headers", []) if h[0].lower() not in dict(fixed)]
                msg["headers"] = msg["headers"] + fixed + late
            await send(msg)

        async def reply(resp: JSONResponse) -> None:
            await resp(scope, receive, send_wrapper)

        path: str = scope.get("path", "")
        cap = self.part_cap if path.startswith("/v1/_store/") else MAX_JSON_BODY
        headers = dict(scope.get("headers") or [])
        try:
            content_length = int(headers.get(b"content-length", b"-1"))
        except ValueError:
            content_length = -1
        too_large = problem_body(
            rid, 413, "payload-too-large", "Payload too large", "QT-USAGE-001", detail=f"body limit is {cap} bytes"
        )
        try:
            if content_length > cap:
                await reply(JSONResponse(too_large, status_code=413, media_type=PROBLEM_MEDIA))
                return
            seen = 0

            async def capped_receive() -> dict[str, Any]:
                nonlocal seen
                msg = await receive()
                if msg["type"] == "http.request":
                    seen += len(msg.get("body", b""))
                    if seen > cap:
                        raise _BodyTooLarge
                return msg

            await self.app(scope, capped_receive, send_wrapper)
        except _BodyTooLarge:
            if not started:
                await reply(JSONResponse(too_large, status_code=413, media_type=PROBLEM_MEDIA))
        except Exception:
            log.exception("unhandled error on %s %s", scope.get("method"), path)
            if not started:
                await reply(problem_response(rid, 500, "internal", "Internal error", "QT-ENGINE-001"))
        finally:
            request_id_var.reset(token)
            tenant_id_var.reset(tenant_token)


def install_handlers(app: FastAPI) -> None:
    """Problem responses for `ProblemError`, request validation and Starlette HTTP errors."""

    @app.exception_handler(ProblemError)
    async def _problem(request: Request, e: ProblemError) -> JSONResponse:
        return problem_response(
            request_id(request),
            e.status,
            e.slug,
            e.title,
            e.code,
            detail=e.detail,
            instance=request.url.path,
            errors=e.errors,
            headers=e.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, e: RequestValidationError) -> JSONResponse:
        errs = [
            {"loc": [p for p in x.get("loc", ()) if isinstance(p, (str, int))], "msg": str(x.get("msg"))}
            for x in e.errors()
        ]
        return problem_response(
            request_id(request),
            422,
            "validation-error",
            "Request validation failed",
            "QT-CONFIG-001",
            instance=request.url.path,
            errors=errs,
        )

    @app.exception_handler(BindingRefused)
    async def _binding(request: Request, e: BindingRefused) -> JSONResponse:
        # the key was revoked (or the tenant deleted) after authentication
        return problem_response(
            request_id(request),
            401,
            "unauthorized",
            "Unauthorized",
            "QT-USAGE-001",
            detail="missing or invalid API key",
            instance=request.url.path,
            headers={"WWW-Authenticate": 'Bearer realm="queuetruth"'},
        )

    @app.exception_handler(psycopg.DataError)
    async def _data(request: Request, e: psycopg.DataError) -> JSONResponse:
        # Values the database cannot store (NUL characters, out-of-range numbers): a client error, not a 500.
        return problem_response(
            request_id(request),
            422,
            "validation-error",
            "Request validation failed",
            "QT-CONFIG-001",
            instance=request.url.path,
            errors=[{"loc": ["body"], "msg": f"value cannot be stored ({type(e).__name__})"}],
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, e: StarletteHTTPException) -> JSONResponse:
        if e.status_code == 400:  # undecodable JSON body: a request validation error (SPEC 4.5 maps those to 422)
            return problem_response(
                request_id(request),
                422,
                "validation-error",
                "Request validation failed",
                "QT-CONFIG-001",
                instance=request.url.path,
                errors=[{"loc": ["body"], "msg": "body is not valid JSON"}],
            )
        slug = {404: "not-found", 405: "method-not-allowed"}.get(e.status_code, "http-error")
        title = {404: "Not found", 405: "Method not allowed"}.get(e.status_code, str(e.detail))
        return problem_response(
            request_id(request),
            e.status_code,
            slug,
            title,
            "QT-USAGE-001",
            instance=request.url.path,
            headers=getattr(e, "headers", None),
        )
