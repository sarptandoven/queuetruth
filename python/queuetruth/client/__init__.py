"""HTTP client for the queuetruth service (SPEC 4.5; env `QT_API_URL`, `QT_API_KEY`).

`Client` covers every `/v1` endpoint; `client.jobs` waits for jobs and downloads their files with sha256
verification. Errors from the service raise `queuetruth.QtApiError` (code `QT-REMOTE-001`, `.problem` = the
RFC 9457 `qt.problem` document). `queuetruth.client.testing.FakeService` is an in-process fake of the API for tests.
Needs the `client` extra (`pip install "queuetruth[client]"`, which provides httpx); without it, importing this package
raises an `ImportError` that says so.
"""

from __future__ import annotations

try:
    import httpx  # noqa: F401  (the optional dependency of this subpackage)
except ModuleNotFoundError as _e:
    if _e.name != "httpx":
        raise
    raise ImportError(
        'queuetruth.client needs the optional dependency httpx: pip install "queuetruth[client]"',
        name="httpx",  # the missing module (callers such as the CLI key on it)
    ) from _e

from queuetruth.client._client import (
    DEFAULT_API_URL,
    ENV_API_KEY,
    ENV_API_URL,
    ESTIMATE_KINDS,
    RUN_KINDS,
    TERMINAL_JOB_STATES,
    Client,
    Jobs,
    ReportRecord,
    RunRecord,
    Submission,
    job_error,
    new_idempotency_key,
)
from queuetruth.client.problems import problem_from_response, problem_slug

__all__ = [
    "DEFAULT_API_URL",
    "ENV_API_KEY",
    "ENV_API_URL",
    "ESTIMATE_KINDS",
    "RUN_KINDS",
    "TERMINAL_JOB_STATES",
    "Client",
    "Jobs",
    "ReportRecord",
    "RunRecord",
    "Submission",
    "job_error",
    "new_idempotency_key",
    "problem_from_response",
    "problem_slug",
]
