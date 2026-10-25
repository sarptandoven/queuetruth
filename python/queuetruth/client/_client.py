"""`Client`: typed synchronous client for every `/v1` endpoint of the queuetruth service (SPEC 4.5, 8.5, 8.6).

Semantics:
- Base URL from `base_url`, else env `QT_API_URL`, else `http://127.0.0.1:8080`; API key from `api_key`, else env
  `QT_API_KEY`. The key is sent as `Authorization: Bearer <key>` to API paths only, never to presigned URLs.
- Every POST to an endpoint marked `I` in SPEC 4.5 carries an `Idempotency-Key`: the caller's key, or one generated
  per call. Retries of that call reuse the same key, so a retried submit never creates a second job.
- Retries (at most `max_retries`): transport errors, 429 (honouring `Retry-After`), 500/502/503/504 and
  409 `idempotency-in-flight`. Delays grow exponentially from `backoff_s` up to `max_backoff_s`, with jitter.
- Every non-2xx response raises `QtApiError` (code `QT-REMOTE-001`) carrying the RFC 9457 problem.
- Responses are validated against the shared Pydantic models (`queuetruth.schemas`); a non-conforming document
  raises `QtError("QT-REMOTE-001")`.
- Specs passed to `estimate`/`submit` are validated locally (`QT-CONFIG-001`) and must have `out = null`; only the
  fields the caller set are sent (the service applies the SPEC 4.2 defaults).
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote, urljoin

import httpx
from pydantic import BaseModel, ValidationError

from queuetruth._version import __version__
from queuetruth.client.problems import api_error, problem_from_response, problem_slug
from queuetruth.errors import QtApiError, QtError
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as sp

DEFAULT_API_URL = "http://127.0.0.1:8080"
ENV_API_URL = "QT_API_URL"
ENV_API_KEY = "QT_API_KEY"

#: Statuses retried for every request (with the same idempotency key for POSTs).
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
#: Terminal job states (SPEC 3.10 `qt.job.state`).
TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "canceled", "expired"})
#: Terminal upload states after completion (asynchronous sha256 verification, SPEC 8.4).
TERMINAL_UPLOAD_STATES = frozenset({"verified", "failed"})

#: Job-creating resources: kind -> (collection path, spec model, summary model or None).
RUN_KINDS: dict[str, tuple[str, type[sp.SpecModel], type[m.QtDoc] | None]] = {
    "replay": ("replays", sp.ReplaySpec, m.ReplaySummary),
    "zoo": ("zoos", sp.ZooSpec, m.ZooSummary),
    "calibration": ("calibrations", sp.CalibrationSpec, m.CalibrationResult),
    "scorecard": ("scorecards", sp.ScorecardSpec, m.ScorecardSummary),
    "report": ("reports", sp.ReportSpec, None),
}
#: Kinds with an `/estimate` endpoint and a `GET /{collection}/{id}` run record with a summary.
ESTIMATE_KINDS = ("replay", "zoo", "calibration", "scorecard")

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def new_idempotency_key() -> str:
    """A fresh random idempotency key (1..255 chars, SPEC 8.6)."""
    return f"qtc-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class Submission:
    """Response of a job-creating POST (`{id, job}`), with `replayed` = the `Idempotent-Replayed` header."""

    id: str
    job: m.Job
    replayed: bool = False
    location: str | None = None


@dataclass(frozen=True)
class RunRecord:
    """`GET /v1/{replays|calibrations|scorecards}/{id}`: `{id, spec, job_id, summary?}` with a typed summary."""

    id: str
    kind: str
    spec: dict[str, Any]
    job_id: str
    summary: m.QtDoc | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ReportRecord:
    """`GET /v1/reports/{id}`: `{id, job_id, files}`."""

    id: str
    job_id: str
    files: list[Any]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _seg(value: str) -> str:
    """One URL path segment, percent-encoded (ids never inject path components)."""
    if not isinstance(value, str) or not value:
        raise QtError("QT-USAGE-001", f"invalid id {value!r}")
    return quote(value, safe="")


def _parse(model: type[_ModelT], data: Any) -> _ModelT:
    try:
        return model.model_validate(data)
    except ValidationError as e:
        name = getattr(model, "SCHEMA", "") or model.__name__
        err = e.errors()[0] if e.errors() else {"loc": (), "msg": str(e)}
        loc = ".".join(str(p) for p in err.get("loc", ()))
        raise QtError(
            "QT-REMOTE-001",
            f"service response is not a valid {name}: {loc or '<root>'}: {err.get('msg')}",
            "client and service versions may differ",
        ) from e


def _spec_body(kind: str, spec: Any) -> dict[str, Any]:
    """Validates a spec for `kind` and returns the JSON body (fields the caller set)."""
    if kind not in RUN_KINDS:
        raise QtError("QT-USAGE-001", f"unknown job kind {kind!r} (expected one of {', '.join(RUN_KINDS)})")
    model = RUN_KINDS[kind][1]
    try:
        if isinstance(spec, model):
            obj = spec
        elif isinstance(spec, BaseModel):
            obj = model.model_validate(spec.model_dump(mode="json", exclude_unset=True))
        elif isinstance(spec, Mapping):
            obj = model.model_validate(dict(spec))
        else:
            raise QtError("QT-CONFIG-001", f"{model.__name__}: expected a mapping, got {type(spec).__name__}")
    except ValidationError as e:
        err = e.errors()[0]
        loc = ".".join(str(p) for p in err.get("loc", ()))
        raise QtError("QT-CONFIG-001", f"{model.__name__}.{loc}: {err.get('msg')}") from e
    if getattr(obj, "out", None) is not None:
        raise QtError(
            "QT-CONFIG-001",
            f"{model.__name__}.out must be null for the HTTP API",
            "the worker assigns a private output directory; download results with jobs.download_all",
        )
    return obj.model_dump(mode="json", exclude_unset=True)


def _retry_after_s(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


class Client:
    """Client for every `/v1` endpoint (idempotency keys, problems -> `QtApiError`, job waiting, downloads)."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_s: float = 0.5,
        max_backoff_s: float = 8.0,
        max_retry_after_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        user_agent: str | None = None,
    ) -> None:
        url = (base_url or os.environ.get(ENV_API_URL) or DEFAULT_API_URL).strip().rstrip("/")
        if url.endswith("/v1"):
            url = url[: -len("/v1")]
        if not url.startswith(("http://", "https://")):
            raise QtError("QT-CONFIG-001", f"{ENV_API_URL} must be an http(s) URL, got {url!r}")
        key = api_key if api_key is not None else os.environ.get(ENV_API_KEY)
        self._base_url = url
        self._api_key = key.strip() if key else None
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.max_retry_after_s = max_retry_after_s
        self._sleep = sleep
        self._clock = clock
        self._rng = random.Random()
        self.last_request_id: str | None = None
        self._meta: m.Meta | None = None
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": user_agent or f"queuetruth-client/{__version__}"},
        )
        self.jobs = Jobs(self)

    # ---- plumbing ---------------------------------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """Service root URL (without `/v1`)."""
        return self._base_url

    @property
    def has_api_key(self) -> bool:
        """True when an API key is configured."""
        return bool(self._api_key)

    def __repr__(self) -> str:
        return f"Client(base_url={self._base_url!r}, api_key={'set' if self._api_key else 'unset'})"

    def close(self) -> None:
        """Closes the connection pool."""
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def url(self, path: str) -> str:
        """Absolute URL of an API path (`/v1/...`) or of a service-relative presigned path."""
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(self._base_url + "/", path.lstrip("/"))

    def _auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            raise QtError("QT-CONFIG-001", f"{ENV_API_KEY} is not set", "create a key with `queuetruth-service admin`")
        return {"Authorization": f"Bearer {self._api_key}"}

    def _delay(self, attempt: int, response: httpx.Response | None) -> float | None:
        """Seconds to wait before retry `attempt` (0-based); None = do not retry."""
        if attempt >= self.max_retries:
            return None
        if response is not None:
            ra = _retry_after_s(response)
            if ra is not None:
                return ra if ra <= self.max_retry_after_s else None
        base = min(self.max_backoff_s, self.backoff_s * (2**attempt))
        return base * (0.5 + 0.5 * self._rng.random())

    @staticmethod
    def _retryable(response: httpx.Response) -> bool:
        if response.status_code in RETRY_STATUSES:
            return True
        if response.status_code == 409:
            return problem_slug(problem_from_response(response)) == "idempotency-in-flight"
        return False

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        params: Mapping[str, Any] | None = None,
        stream: bool = False,
        retry_statuses: bool = True,
    ) -> httpx.Response:
        """Sends one logical request with retries; returns the final response (any status). Transport failures
        after the last retry raise `QtError("QT-REMOTE-001")`."""
        clean = {k: v for k, v in (params or {}).items() if v is not None} or None
        attempt = 0
        while True:
            request = self._http.build_request(method, url, headers=dict(headers or {}), content=content, params=clean)
            try:
                response = self._http.send(request, stream=stream)
            except httpx.TransportError as e:
                delay = self._delay(attempt, None)
                if delay is None:
                    raise QtError(
                        "QT-REMOTE-001",
                        f"{method} {request.url.copy_with(query=None)} failed after {attempt + 1} attempts: "
                        f"{type(e).__name__}: {e}",
                        f"is the service reachable at {self._base_url}?",
                    ) from e
                self._sleep(delay)
                attempt += 1
                continue
            rid = response.headers.get("X-Request-Id")
            if rid:
                self.last_request_id = rid
            if stream and response.status_code >= 400:
                response.read()
            if retry_statuses and self._retryable(response):
                delay = self._delay(attempt, response)
                if delay is not None:
                    response.close()
                    self._sleep(delay)
                    attempt += 1
                    continue
            return response

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
        auth: bool = True,
    ) -> httpx.Response:
        """One API call: JSON body, auth, `Idempotency-Key` when `idempotent`; non-2xx raises `QtApiError`."""
        headers = {"Accept": "application/json, application/problem+json"}
        if auth:
            headers.update(self._auth_headers())
        content = None
        if json_body is not None:
            try:
                content = json.dumps(json_body, allow_nan=False, separators=(",", ":")).encode()
            except (TypeError, ValueError) as e:
                raise QtError("QT-CONFIG-001", f"request body is not valid JSON: {e}") from e
            headers["Content-Type"] = "application/json"
        if idempotent:
            key = idempotency_key or new_idempotency_key()
            if not 1 <= len(key) <= 255:
                raise QtError("QT-USAGE-001", "idempotency key must have 1..255 characters")
            headers["Idempotency-Key"] = key
        response = self.send(method, self.url(path), headers=headers, content=content, params=params)
        if response.status_code >= 400:
            raise api_error(response)
        return response

    def _get_json(self, path: str, params: Mapping[str, Any] | None = None, *, auth: bool = True) -> Any:
        return self._json(self.request("GET", path, params=params, auth=auth))

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as e:
            raise QtError(
                "QT-REMOTE-001", f"{response.request.method} {response.request.url.path}: response is not JSON"
            ) from e

    def _page(self, path: str, limit: int | None, starting_after: str | None, **filters: Any) -> m.Page:
        params = {"limit": limit, "starting_after": starting_after, **filters}
        return _parse(m.Page, self._get_json(path, params))

    def _iter(self, path: str, model: type[_ModelT] | None, page_size: int = 100, **filters: Any) -> Iterator[Any]:
        cursor: str | None = None
        while True:
            page = self._page(path, page_size, cursor, **filters)
            for item in page.data:
                yield _parse(model, item) if model is not None else item
            if not page.has_more or not page.next_cursor:
                return
            cursor = page.next_cursor

    # ---- service ----------------------------------------------------------------------------------------------

    def health(self) -> m.Health:
        """`GET /v1/health` (no auth). A 503 still returns the `qt.health` document (status `degraded`)."""
        response = self.send("GET", self.url("/v1/health"), headers={"Accept": "application/json"})
        if response.status_code == 503:
            try:
                return _parse(m.Health, response.json())
            except (ValueError, QtError):
                raise api_error(response) from None
        if response.status_code >= 400:
            raise api_error(response)
        return _parse(m.Health, self._json(response))

    def meta(self, *, refresh: bool = False) -> m.Meta:
        """`GET /v1/meta` (cached after the first call)."""
        if self._meta is None or refresh:
            self._meta = _parse(m.Meta, self._get_json("/v1/meta"))
        return self._meta

    def openapi(self) -> dict[str, Any]:
        """`GET /v1/openapi.json` (no auth)."""
        doc = self._get_json("/v1/openapi.json", auth=False)
        if not isinstance(doc, dict):
            raise QtError("QT-REMOTE-001", "openapi.json is not a JSON object")
        return doc

    def tenant(self) -> m.Tenant:
        """`GET /v1/tenant`."""
        return _parse(m.Tenant, self._get_json("/v1/tenant"))

    # ---- keys -------------------------------------------------------------------------------------------------

    def create_key(self, scopes: Sequence[str], name: str, *, idempotency_key: str | None = None) -> m.ApiKeyCreated:
        """`POST /v1/keys` (admin): the secret is shown once."""
        body = {"scopes": list(scopes), "name": name}
        r = self.request("POST", "/v1/keys", json_body=body, idempotent=True, idempotency_key=idempotency_key)
        doc = self._json(r)
        if isinstance(doc, dict) and doc.get("secret") is None and r.headers.get("Idempotent-Replayed") == "true":
            raise QtError(
                "QT-REMOTE-001",
                f"key {doc.get('id')} was already created with this idempotency key; its secret is shown only once",
                "revoke it and create a new key if the secret was lost",
                {"key_id": doc.get("id")},
            )
        return _parse(m.ApiKeyCreated, doc)

    def list_keys(self, *, limit: int | None = None, starting_after: str | None = None) -> m.Page:
        """`GET /v1/keys` (admin): one page of `qt.api_key`."""
        return self._page("/v1/keys", limit, starting_after)

    def revoke_key(self, key_id: str) -> None:
        """`DELETE /v1/keys/{key_id}` (admin)."""
        self.request("DELETE", f"/v1/keys/{_seg(key_id)}")

    # ---- uploads ----------------------------------------------------------------------------------------------

    def create_upload(
        self,
        purpose: str,
        filename: str,
        size_bytes: int,
        sha256: str,
        *,
        content_type: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Upload:
        """`POST /v1/uploads`: returns the upload with presigned part URLs."""
        body: dict[str, Any] = {"purpose": purpose, "filename": filename, "size_bytes": size_bytes, "sha256": sha256}
        if content_type is not None:
            body["content_type"] = content_type
        r = self.request("POST", "/v1/uploads", json_body=body, idempotent=True, idempotency_key=idempotency_key)
        return _parse(m.Upload, self._json(r))

    def get_upload(self, upload_id: str) -> m.Upload:
        """`GET /v1/uploads/{id}`."""
        return _parse(m.Upload, self._get_json(f"/v1/uploads/{_seg(upload_id)}"))

    def complete_upload(
        self, upload_id: str, parts: Sequence[Mapping[str, Any]], *, idempotency_key: str | None = None
    ) -> m.Upload:
        """`POST /v1/uploads/{id}/complete` with `[{part_number, etag}]`."""
        body = {"parts": [{"part_number": int(p["part_number"]), "etag": str(p["etag"])} for p in parts]}
        r = self.request(
            "POST",
            f"/v1/uploads/{_seg(upload_id)}/complete",
            json_body=body,
            idempotent=True,
            idempotency_key=idempotency_key,
        )
        return _parse(m.Upload, self._json(r))

    def delete_upload(self, upload_id: str) -> None:
        """`DELETE /v1/uploads/{id}`."""
        self.request("DELETE", f"/v1/uploads/{_seg(upload_id)}")

    def upload_file(
        self,
        path: str | os.PathLike[str],
        purpose: str,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        idempotency_key: str | None = None,
    ) -> m.Upload:
        """Uploads a local file: create, PUT every presigned part, complete (status `complete`; the service then
        verifies the sha256 asynchronously, see `wait_upload`)."""
        from queuetruth.client.transfer import upload_file

        return upload_file(
            self, Path(path), purpose, filename=filename, content_type=content_type, idempotency_key=idempotency_key
        )

    def wait_upload(self, upload_id: str, *, timeout: float | None = 600.0, poll_interval: float = 1.0) -> m.Upload:
        """Polls until the upload is `verified` or `failed` (sha256 check by the worker)."""
        deadline = None if timeout is None else self._clock() + timeout
        interval = poll_interval
        while True:
            up = self.get_upload(upload_id)
            if up.status in TERMINAL_UPLOAD_STATES:
                return up
            if deadline is not None and self._clock() + interval > deadline:
                raise QtError(
                    "QT-REMOTE-001",
                    f"timed out after {timeout} s waiting for upload {upload_id} (status {up.status})",
                    details={"upload_id": upload_id, "status": up.status},
                )
            self._sleep(interval)
            interval = min(interval * 1.5, 10.0)

    # ---- datasets ---------------------------------------------------------------------------------------------

    def create_dataset(
        self,
        name: str,
        kind: str,
        format: str,
        venue: str,
        upload_ids: Sequence[str],
        *,
        ingest: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> m.Dataset:
        """`POST /v1/datasets`: registers uploads as a dataset and starts its validation job (`dataset.job_id`).
        `ingest` is the IngestSpec without `inputs` and `out`."""
        body: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "format": format,
            "venue": venue,
            "upload_ids": list(upload_ids),
            "ingest": dict(ingest) if ingest is not None else None,
        }
        r = self.request("POST", "/v1/datasets", json_body=body, idempotent=True, idempotency_key=idempotency_key)
        return _parse(m.Dataset, self._json(r))

    def list_datasets(self, *, limit: int | None = None, starting_after: str | None = None) -> m.Page:
        """`GET /v1/datasets`: one page of `qt.dataset`."""
        return self._page("/v1/datasets", limit, starting_after)

    def iter_datasets(self, *, page_size: int = 100) -> Iterator[m.Dataset]:
        """Every dataset, following `next_cursor`."""
        return self._iter("/v1/datasets", m.Dataset, page_size)

    def get_dataset(self, dataset_id: str) -> m.Dataset:
        """`GET /v1/datasets/{id}`."""
        return _parse(m.Dataset, self._get_json(f"/v1/datasets/{_seg(dataset_id)}"))

    def delete_dataset(self, dataset_id: str) -> m.Job:
        """`DELETE /v1/datasets/{id}`: returns the deletion job."""
        return _parse(m.Job, self._json(self.request("DELETE", f"/v1/datasets/{_seg(dataset_id)}")))

    # ---- replays / calibrations / scorecards / reports --------------------------------------------------------

    def estimate(self, kind: str, spec: Any) -> m.Estimate:
        """`POST /v1/{replays|calibrations|scorecards}/estimate`."""
        if kind not in ESTIMATE_KINDS:
            raise QtError(
                "QT-USAGE-001", f"no estimate for kind {kind!r} (expected one of {', '.join(ESTIMATE_KINDS)})"
            )
        body = _spec_body(kind, spec)
        r = self.request("POST", f"/v1/{RUN_KINDS[kind][0]}/estimate", json_body=body)
        return _parse(m.Estimate, self._json(r))

    def submit(self, kind: str, spec: Any, *, idempotency_key: str | None = None) -> Submission:
        """`POST /v1/{replays|calibrations|scorecards|reports}`: creates the resource and its job."""
        body = _spec_body(kind, spec)
        r = self.request(
            "POST", f"/v1/{RUN_KINDS[kind][0]}", json_body=body, idempotent=True, idempotency_key=idempotency_key
        )
        doc = self._json(r)
        if not isinstance(doc, dict) or not isinstance(doc.get("id"), str) or "job" not in doc:
            raise QtError("QT-REMOTE-001", f"submit {kind}: expected {{id, job}}, got {str(doc)[:200]}")
        return Submission(
            id=doc["id"],
            job=_parse(m.Job, doc["job"]),
            replayed=r.headers.get("Idempotent-Replayed", "").lower() == "true",
            location=r.headers.get("Location"),
        )

    def get_run(self, kind: str, run_id: str) -> RunRecord:
        """`GET /v1/{replays|calibrations|scorecards}/{id}`; `summary` is typed and None until the job succeeds."""
        if kind not in ESTIMATE_KINDS:
            raise QtError("QT-USAGE-001", f"unknown run kind {kind!r} (expected one of {', '.join(ESTIMATE_KINDS)})")
        collection, _, summary_model = RUN_KINDS[kind]
        doc = self._get_json(f"/v1/{collection}/{_seg(run_id)}")
        if not isinstance(doc, dict) or not isinstance(doc.get("id"), str) or not isinstance(doc.get("job_id"), str):
            raise QtError("QT-REMOTE-001", f"{kind} record: expected {{id, spec, job_id, summary?}}")
        raw_summary = doc.get("summary")
        summary = _parse(summary_model, raw_summary) if raw_summary is not None and summary_model else None
        spec = doc.get("spec")
        return RunRecord(
            id=doc["id"],
            kind=kind,
            spec=spec if isinstance(spec, dict) else {},
            job_id=doc["job_id"],
            summary=summary,
            raw=doc,
        )

    def list_runs(self, kind: str, *, limit: int | None = None, starting_after: str | None = None) -> m.Page:
        """`GET /v1/{replays|calibrations|scorecards}`: one page of `{id, spec, job_id, summary?}`."""
        if kind not in ESTIMATE_KINDS:
            raise QtError("QT-USAGE-001", f"unknown run kind {kind!r}")
        return self._page(f"/v1/{RUN_KINDS[kind][0]}", limit, starting_after)

    def get_report(self, report_id: str) -> ReportRecord:
        """`GET /v1/reports/{id}`."""
        doc = self._get_json(f"/v1/reports/{_seg(report_id)}")
        if not isinstance(doc, dict) or not isinstance(doc.get("id"), str) or not isinstance(doc.get("job_id"), str):
            raise QtError("QT-REMOTE-001", "report record: expected {id, job_id, files}")
        files = doc.get("files")
        return ReportRecord(id=doc["id"], job_id=doc["job_id"], files=files if isinstance(files, list) else [], raw=doc)

    # ---- usage, retention, tombstones -------------------------------------------------------------------------

    def usage(self, *, from_: str | None = None, to: str | None = None, group_by: str = "meter") -> m.UsageReport:
        """`GET /v1/usage?from=&to=&group_by=`."""
        return _parse(m.UsageReport, self._get_json("/v1/usage", {"from": from_, "to": to, "group_by": group_by}))

    def get_retention(self) -> m.Retention:
        """`GET /v1/retention`."""
        return _parse(m.Retention, self._get_json("/v1/retention"))

    def set_retention(self, uploads_days: int, outputs_days: int) -> m.Retention:
        """`PUT /v1/retention` (admin)."""
        body = {"uploads_days": uploads_days, "outputs_days": outputs_days}
        return _parse(m.Retention, self._json(self.request("PUT", "/v1/retention", json_body=body)))

    def list_tombstones(self, *, limit: int | None = None, starting_after: str | None = None) -> m.Page:
        """`GET /v1/tombstones` (admin)."""
        return self._page("/v1/tombstones", limit, starting_after)


class Jobs:
    """`client.jobs`: list, get, cancel, submit, wait, files and verified downloads."""

    def __init__(self, client: Client) -> None:
        self._c = client

    def list(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
        limit: int | None = None,
        starting_after: str | None = None,
    ) -> m.Page:
        """`GET /v1/jobs?kind=&state=`: one page of `qt.job`."""
        return self._c._page("/v1/jobs", limit, starting_after, kind=kind, state=state)

    def iter(self, *, kind: str | None = None, state: str | None = None, page_size: int = 100) -> Iterator[m.Job]:
        """Every job matching the filters."""
        return self._c._iter("/v1/jobs", m.Job, page_size, kind=kind, state=state)

    def get(self, job_id: str) -> m.Job:
        """`GET /v1/jobs/{id}`."""
        return _parse(m.Job, self._c._get_json(f"/v1/jobs/{_seg(job_id)}"))

    def cancel(self, job_id: str, *, idempotency_key: str | None = None) -> m.Job:
        """`POST /v1/jobs/{id}/cancel` (409 for a terminal job)."""
        r = self._c.request("POST", f"/v1/jobs/{_seg(job_id)}/cancel", idempotent=True, idempotency_key=idempotency_key)
        return _parse(m.Job, self._c._json(r))

    def submit(self, kind: str, spec: Any, *, idempotency_key: str | None = None) -> m.Job:
        """Submits a replay/calibration/scorecard/report spec and returns its job (`queuetruth jobs submit`)."""
        return self._c.submit(kind, spec, idempotency_key=idempotency_key).job

    def wait(
        self,
        job_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
        max_poll_interval: float = 10.0,
        raise_on_failure: bool = False,
        on_update: Callable[[m.Job], None] | None = None,
    ) -> m.Job:
        """Polls `GET /v1/jobs/{id}` until a terminal state. `timeout` (s) raises `QtError("QT-REMOTE-001")`;
        `raise_on_failure` raises `QtApiError` with the job's problem for failed/canceled/expired jobs."""
        deadline = None if timeout is None else self._c._clock() + timeout
        interval = max(0.0, poll_interval)
        while True:
            job = self.get(job_id)
            if on_update is not None:
                on_update(job)
            if job.state in TERMINAL_JOB_STATES:
                if raise_on_failure and job.state != "succeeded":
                    raise job_error(job)
                return job
            if deadline is not None and self._c._clock() + interval > deadline:
                raise QtError(
                    "QT-REMOTE-001",
                    f"timed out after {timeout} s waiting for job {job_id} (state {job.state})",
                    f"`queuetruth jobs wait {job_id}` resumes waiting",
                    {"job_id": job_id, "state": job.state},
                )
            self._c._sleep(interval)
            interval = min(max_poll_interval, max(interval * 1.5, poll_interval))

    def files(self, job_id: str) -> m.FileManifest:
        """`GET /v1/jobs/{id}/files` (409 until the job succeeded, 410 after expiry)."""
        return _parse(m.FileManifest, self._c._get_json(f"/v1/jobs/{_seg(job_id)}/files"))

    def download(
        self,
        job_id: str,
        name: str,
        dest: str | os.PathLike[str],
        *,
        entry: m.ManifestFile | None = None,
    ) -> Path:
        """Downloads one job file to `dest` with ranged resume after interruptions; the size and sha256 from the
        file manifest are verified before `dest` is written (mismatch raises `QtError("QT-REMOTE-001")`)."""
        from queuetruth.client.transfer import download_job_file

        if entry is None:
            manifest = self.files(job_id)
            found = [f for f in manifest.files if f.name == name]
            if not found:
                raise QtError("QT-INPUT-001", f"job {job_id} has no file {name!r}")
            entry = found[0]
        return download_job_file(self._c, job_id, entry, Path(dest))

    def download_all(self, job_id: str, out_dir: str | os.PathLike[str]) -> list[Path]:
        """Downloads every file of a job under `out_dir` (names may contain sub-directories)."""
        from queuetruth.client.transfer import safe_join

        manifest = self.files(job_id)
        out = Path(out_dir)
        return [self.download(job_id, f.name, safe_join(out, f.name), entry=f) for f in manifest.files]


def job_error(job: m.Job) -> QtApiError:
    """`QtApiError` for a job that did not succeed (its problem, or a synthesized one)."""
    if job.error is not None:
        problem = job.error.model_dump(mode="json")
    else:
        problem = {
            "schema": "qt.problem",
            "schema_version": 1,
            "type": "about:blank",
            "title": f"job {job.state}",
            "status": 0,
            "detail": f"job {job.id} ended in state {job.state}",
            "code": f"job-{job.state}",
            "request_id": "",
        }
    return QtApiError(problem, f"job {job.id} ({job.kind}) ended in state {job.state}")


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
]
