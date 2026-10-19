"""RFC 9457 problem handling for the HTTP client (SPEC 4.5): every non-2xx response becomes a `QtApiError`.

A body that is not a problem document (proxy error page, empty body) is wrapped in a synthesized `qt.problem` with
`type = "about:blank"` and `code = "http-<status>"`. The `X-Request-Id` header fills `request_id` when the body
lacks it.
"""

from __future__ import annotations

from typing import Any

import httpx

from queuetruth.errors import QtApiError

PROBLEM_TYPE_PREFIX = "https://queuetruth.dev/problems/"
_DETAIL_LIMIT = 500

_HINTS = {
    401: "check QT_API_KEY (missing, malformed or revoked key)",
    403: "the API key lacks the scope this endpoint needs",
    404: "unknown id (ids of other tenants are reported as unknown too)",
    409: "conflict with the resource's current state",
    410: "the files expired under the tenant's retention policy",
    413: "request body or upload exceeds the service limit",
    422: "the request did not validate; see problem.errors",
    429: "rate limited; retry after the Retry-After delay",
    503: "a service dependency is down; retry later",
}


def problem_slug(problem: dict[str, Any]) -> str | None:
    """The problem's slug: its `code`, or the last path segment of a queuetruth `type` URL."""
    code = problem.get("code")
    if isinstance(code, str) and code and not code.startswith("QT-"):
        return code
    ptype = problem.get("type")
    if isinstance(ptype, str) and ptype.startswith(PROBLEM_TYPE_PREFIX):
        return ptype[len(PROBLEM_TYPE_PREFIX) :] or None
    return code if isinstance(code, str) and code else None


def problem_from_response(response: httpx.Response) -> dict[str, Any]:
    """The `qt.problem` document of an error response (parsed or synthesized)."""
    request_id = response.headers.get("X-Request-Id") or ""
    body: Any = None
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict) and ("title" in body or "type" in body) and "status" in body:
        problem = dict(body)
        if not problem.get("request_id") and request_id:
            problem["request_id"] = request_id
        return problem
    try:
        text = response.text.strip()
    except (UnicodeDecodeError, httpx.ResponseNotRead):
        text = ""
    try:
        instance: str | None = response.request.url.path
    except RuntimeError:
        instance = None
    return {
        "schema": "qt.problem",
        "schema_version": 1,
        "type": "about:blank",
        "title": response.reason_phrase or "HTTP error",
        "status": response.status_code,
        "detail": text[:_DETAIL_LIMIT] or None,
        "instance": instance,
        "code": f"http-{response.status_code}",
        "request_id": request_id,
    }


def api_error(response: httpx.Response) -> QtApiError:
    """`QtApiError` for an error response, with a hint by status."""
    problem = problem_from_response(response)
    status = problem.get("status")
    hint = _HINTS.get(status if isinstance(status, int) else response.status_code)
    return QtApiError(problem, hint)


__all__ = ["PROBLEM_TYPE_PREFIX", "api_error", "problem_from_response", "problem_slug"]
