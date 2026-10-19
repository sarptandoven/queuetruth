"""In-process fake of the SPEC 4.5 HTTP API for tests (`httpx.MockTransport`; no network, no service code).

Covers every `/v1` endpoint with the contract's status codes, `qt.*` documents, RFC 9457 problems, Bearer/Basic auth
with hierarchical scopes, Stripe-style idempotency (`Idempotent-Replayed`, 422 `idempotency-key-reused`,
409 `idempotency-in-flight`), cursor pagination, presigned part uploads and file downloads (direct with `Range`, or a
302 to a presigned `/v1/_store/...` URL that rejects requests carrying an `Authorization` header). It is a contract
fake, not the service: no persistence, no rate limits, jobs advance only through the test controls (`advance`,
`finish_job`) or `auto_advance`.

Test controls: `inject(...)` queues faults (status responses or transport exceptions) for matching requests,
`cut_next_download(n)` drops the next file stream after `n` bytes, `corrupt(job_id, name)` serves different bytes
than the manifest's sha256, `in_flight(key)` marks an idempotency key as in progress.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import math
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx
from pydantic import ValidationError

from queuetruth.client._client import RUN_KINDS, Client
from queuetruth.schemas import specs as sp

FAKE_API_KEY = "qt_test_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE_ABCDEF"
FAKE_TENANT = "ten_00000000000000000000000001"
FAKE_TIME = "2026-10-18T00:00:00Z"
FAKE_EXPIRES = "2026-10-18T00:15:00Z"
PROBLEM_BASE = "https://queuetruth.dev/problems/"
SCOPE_RANK = {"read": 0, "write": 1, "admin": 2}
COLLECTION_KIND = {v[0]: k for k, v in RUN_KINDS.items()}


@dataclass
class Fault:
    """A queued fault: matches method and path prefix, fires `times` times."""

    method: str | None
    path_prefix: str | None
    status: int | None = None
    exc: type[Exception] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    times: int = 1


@dataclass
class _Stored:
    request_hash: str
    status: int
    body: Any
    headers: dict[str, str]


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class _Problem(Exception):
    def __init__(
        self,
        status: int,
        slug: str,
        title: str,
        detail: str | None = None,
        errors: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(title)
        self.status, self.slug, self.title, self.detail = status, slug, title, detail
        self.errors = errors
        self.headers = dict(headers or {})


def _validation(loc: list[Any], msg: str) -> _Problem:
    return _Problem(422, "validation-error", "Request validation failed", msg, [{"loc": loc, "msg": msg}])


class FakeService:
    """Contract fake of the queuetruth HTTP API; `handler` is an `httpx.MockTransport` handler."""

    def __init__(
        self,
        *,
        base_url: str = "http://qt.test",
        api_key: str = FAKE_API_KEY,
        scopes: tuple[str, ...] = ("admin",),
        part_size_bytes: int = 8,
        max_upload_bytes: int = 1 << 30,
        redirect_downloads: bool = False,
        auto_advance: bool = False,
    ) -> None:
        self._counter = 0
        self.base_url = base_url.rstrip("/")
        self.part_size_bytes = part_size_bytes
        self.max_upload_bytes = max_upload_bytes
        self.redirect_downloads = redirect_downloads
        self.auto_advance = auto_advance
        self.keys: dict[str, dict[str, Any]] = {}
        self._secrets: dict[str, str] = {}
        self._add_key(api_key, list(scopes))
        self.retention = {"uploads_days": 30, "outputs_days": 30}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.upload_meta: dict[str, dict[str, Any]] = {}
        self.datasets: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in RUN_KINDS}
        self.files: dict[str, dict[str, tuple[bytes, str]]] = {}
        self.corrupted: set[tuple[str, str]] = set()
        self.summaries: dict[str, dict[str, Any]] = {}
        self.idem: dict[str, _Stored] = {}
        self._in_flight: dict[str, int] = {}
        self.faults: list[Fault] = []
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self._cut_after: int | None = None
        self._req = 0
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., httpx.Response], str | None, bool]] = []
        self._build_routes()

    # ---- wiring -----------------------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        """A transport serving this fake."""
        return httpx.MockTransport(self.handler)

    def client(self, **kwargs: Any) -> Client:
        """A `Client` wired to this fake (sleeps are recorded in `self.sleeps`, never slept)."""
        kwargs.setdefault("api_key", next(iter(self._secrets)))
        kwargs.setdefault("sleep", self.sleeps.append)
        return Client(self.base_url, transport=self.transport(), **kwargs)

    def new_id(self, prefix: str) -> str:
        """Deterministic `<prefix>_<26 chars>` id."""
        self._counter += 1
        return f"{prefix}_{self._counter:026d}"

    # ---- test controls ----------------------------------------------------------------------------------------

    def inject(
        self,
        *,
        status: int | None = None,
        exc: type[Exception] | None = None,
        method: str | None = None,
        path_prefix: str | None = None,
        times: int = 1,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        """Queues a fault for the next `times` requests matching `method`/`path_prefix`."""
        self.faults.append(Fault(method, path_prefix, status, exc, dict(headers or {}), body, times))

    def in_flight(self, key: str, times: int = 1) -> None:
        """The next `times` requests with idempotency key `key` get 409 `idempotency-in-flight`."""
        self._in_flight[key] = times

    def cut_next_download(self, after_bytes: int) -> None:
        """The next file body stream raises `httpx.ReadError` after `after_bytes` bytes."""
        self._cut_after = after_bytes

    def corrupt(self, job_id: str, name: str) -> None:
        """Serves bytes for `name` that do not match its manifest sha256."""
        self.corrupted.add((job_id, name))

    def add_job_file(self, job_id: str, name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        """Attaches an output file to a job."""
        self.files.setdefault(job_id, {})[name] = (data, content_type)

    def finish_job(
        self,
        job_id: str,
        *,
        state: str = "succeeded",
        summary: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> dict[str, Any]:
        """Moves a job to a terminal state; `summary` is stored on its replay/calibration/scorecard record."""
        job = self.jobs[job_id]
        job.update(state=state, finished_at=FAKE_TIME, started_at=job["started_at"] or FAKE_TIME)
        job["progress"] = {"fraction": 1.0, "stage": state}
        if state == "succeeded":
            job["files_expire_at"] = "2026-11-17T00:00:00Z"
            job["usage"] = {"cpu_ms": 1.0}
        if error is not None:
            job["error"] = error
        for name, data in (files or {}).items():
            self.add_job_file(job_id, name, data)
        res = job["resource"]
        if summary is not None and res["type"] in self.runs:
            self.runs[res["type"]][res["id"]]["summary"] = summary
        return job

    def advance(self, job_id: str) -> dict[str, Any]:
        """queued -> running -> succeeded (with `self.summaries[kind]` when set)."""
        job = self.jobs[job_id]
        if job["state"] == "queued":
            job.update(state="running", started_at=FAKE_TIME, progress={"fraction": 0.5, "stage": "running"})
        elif job["state"] == "running":
            self.finish_job(job_id, summary=self.summaries.get(job["resource"]["type"]))
        return job

    def calls(self, method: str | None = None, path_prefix: str | None = None) -> list[httpx.Request]:
        """Recorded requests matching `method` and `path_prefix`."""
        return [
            r
            for r in self.requests
            if (method is None or r.method == method) and (path_prefix is None or r.url.path.startswith(path_prefix))
        ]

    # ---- handler ----------------------------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        """`httpx.MockTransport` entry point."""
        self.requests.append(request)
        self._req += 1
        rid = f"req_{self._req:06d}"
        path = request.url.path
        for f in self.faults:
            if (
                f.times > 0
                and (f.method in (None, request.method))
                and (f.path_prefix is None or path.startswith(f.path_prefix))
            ):
                f.times -= 1
                if f.exc is not None:
                    raise f.exc("injected fault", request=request) if issubclass(f.exc, httpx.RequestError) else f.exc()
                body = f.body or self._problem_body(_Problem(f.status or 500, "injected", "Injected fault"), path, rid)
                return httpx.Response(
                    f.status or 500,
                    json=body,
                    headers={"Content-Type": "application/problem+json", "X-Request-Id": rid, **f.headers},
                )
        self.faults = [f for f in self.faults if f.times > 0]
        try:
            response = self._dispatch(request, rid)
        except _Problem as p:
            response = httpx.Response(
                p.status,
                json=self._problem_body(p, path, rid),
                headers={"Content-Type": "application/problem+json", **p.headers},
            )
        response.headers["X-Request-Id"] = rid
        return response

    def _problem_body(self, p: _Problem, path: str, rid: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": "qt.problem",
            "schema_version": 1,
            "type": PROBLEM_BASE + p.slug,
            "title": p.title,
            "status": p.status,
            "detail": p.detail,
            "instance": path,
            "code": p.slug,
            "request_id": rid,
        }
        if p.errors is not None:
            body["errors"] = p.errors
        return body

    def _route(
        self, method: str, pattern: str, fn: Callable[..., httpx.Response], scope: str | None, idem: bool = False
    ) -> None:
        self._routes.append((method, re.compile(f"^{pattern}$"), fn, scope, idem))

    def _build_routes(self) -> None:
        r = self._route
        ident = r"([A-Za-z0-9_\-]+)"
        r("GET", "/v1/health", self._health, None)
        r("GET", "/v1/meta", self._meta, "read")
        r("GET", "/v1/openapi.json", self._openapi, None)
        r("GET", "/v1/tenant", self._tenant, "read")
        r("POST", "/v1/keys", self._create_key, "admin", True)
        r("GET", "/v1/keys", self._list_keys, "admin")
        r("DELETE", f"/v1/keys/{ident}", self._revoke_key, "admin")
        r("POST", "/v1/uploads", self._create_upload, "write", True)
        r("GET", f"/v1/uploads/{ident}", self._get_upload, "read")
        r("POST", f"/v1/uploads/{ident}/complete", self._complete_upload, "write", True)
        r("DELETE", f"/v1/uploads/{ident}", self._delete_upload, "write")
        r("POST", "/v1/datasets", self._create_dataset, "write", True)
        r("GET", "/v1/datasets", self._list_datasets, "read")
        r("GET", f"/v1/datasets/{ident}", self._get_dataset, "read")
        r("DELETE", f"/v1/datasets/{ident}", self._delete_dataset, "write")
        r("POST", "/v1/(replays|calibrations|scorecards)/estimate", self._estimate, "read")
        r("POST", "/v1/(replays|calibrations|scorecards|reports)", self._submit, "write", True)
        r("GET", "/v1/(replays|calibrations|scorecards)", self._list_runs, "read")
        r("GET", f"/v1/(replays|calibrations|scorecards)/{ident}", self._get_run, "read")
        r("GET", f"/v1/reports/{ident}", self._get_report, "read")
        r("GET", "/v1/jobs", self._list_jobs, "read")
        r("GET", f"/v1/jobs/{ident}", self._get_job, "read")
        r("POST", f"/v1/jobs/{ident}/cancel", self._cancel_job, "write", True)
        r("GET", f"/v1/jobs/{ident}/files", self._job_files, "read")
        r("GET", f"/v1/jobs/{ident}/files/(.+)", self._job_file, "read")
        r("GET", "/v1/usage", self._usage, "read")
        r("GET", "/v1/retention", self._get_retention, "read")
        r("PUT", "/v1/retention", self._put_retention, "admin")
        r("GET", "/v1/tombstones", self._tombstones, "admin")
        r("PUT", "/v1/_store/(.+)", self._store_put, None)
        r("GET", "/v1/_store/(.+)", self._store_get, None)

    def _dispatch(self, request: httpx.Request, rid: str) -> httpx.Response:
        path = request.url.path
        allowed = False
        for method, pattern, fn, scope, idem in self._routes:
            match = pattern.match(path)
            if not match:
                continue
            allowed = True
            if method != request.method:
                continue
            if scope is not None:
                self._authorize(request, scope)
            groups = [unquote(g) for g in match.groups()]
            if idem:
                return self._idempotent(request, functools.partial(fn, request, *groups))
            return fn(request, *groups)
        if allowed:
            raise _Problem(405, "method-not-allowed", "Method not allowed")
        raise _Problem(404, "not-found", "Not found")

    def _add_key(self, secret: str, scopes: list[str], name: str = "fake") -> dict[str, Any]:
        key_id = self.new_id("key")
        doc = {
            "schema": "qt.api_key",
            "schema_version": 1,
            "id": key_id,
            "prefix": secret.split("_")[2][:12] if secret.count("_") >= 2 else secret[:12],
            "scopes": scopes,
            "created_at": FAKE_TIME,
            "last_used_at": None,
            "revoked_at": None,
        }
        self.keys[key_id] = doc
        self._secrets[secret] = key_id
        return doc

    def _authorize(self, request: httpx.Request, need: str) -> None:
        header = request.headers.get("Authorization", "")
        secret = None
        if header.startswith("Bearer "):
            secret = header[len("Bearer ") :].strip()
        elif header.startswith("Basic "):
            try:
                user, _, _ = base64.b64decode(header[6:]).decode().partition(":")
                secret = user
            except (ValueError, UnicodeDecodeError):
                secret = None
        key_id = self._secrets.get(secret or "")
        if key_id is None or self.keys[key_id]["revoked_at"] is not None:
            raise _Problem(401, "unauthorized", "Missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})
        if max(SCOPE_RANK[s] for s in self.keys[key_id]["scopes"]) < SCOPE_RANK[need]:
            raise _Problem(403, "forbidden", "Insufficient scope", f"requires scope {need}")

    def _idempotent(self, request: httpx.Request, run: Callable[[], httpx.Response]) -> httpx.Response:
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise _validation(["header", "Idempotency-Key"], "Idempotency-Key header is required")
        if not 1 <= len(key) <= 255:
            raise _validation(["header", "Idempotency-Key"], "Idempotency-Key must have 1..255 characters")
        body = json.loads(request.content) if request.content else None
        request_hash = hashlib.sha256(f"{request.method}\n{request.url.path}\n{_canonical(body)}".encode()).hexdigest()
        if self._in_flight.get(key, 0) > 0:
            self._in_flight[key] -= 1
            raise _Problem(409, "idempotency-in-flight", "A request with this Idempotency-Key is in progress")
        stored = self.idem.get(key)
        if stored is not None:
            if stored.request_hash != request_hash:
                raise _Problem(422, "idempotency-key-reused", "Idempotency-Key reused with a different request")
            return httpx.Response(
                stored.status, json=stored.body, headers={**stored.headers, "Idempotent-Replayed": "true"}
            )
        response = run()
        if response.status_code < 500:
            response.read()
            stored_body = json.loads(response.content) if response.content else None
            if isinstance(stored_body, dict) and "secret" in stored_body:
                stored_body["secret"] = None  # secrets are never persisted (replays return null)
            self.idem[key] = _Stored(request_hash, response.status_code, stored_body, dict(response.headers))
        return response

    # ---- helpers ----------------------------------------------------------------------------------------------

    @staticmethod
    def _body(request: httpx.Request) -> dict[str, Any]:
        try:
            body = json.loads(request.content or b"null")
        except ValueError as e:
            raise _validation(["body"], "body is not JSON") from e
        if not isinstance(body, dict):
            raise _validation(["body"], "body must be a JSON object")
        return body

    def _page(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        params = request.url.params
        try:
            limit = int(params.get("limit", "50"))
        except ValueError as e:
            raise _validation(["query", "limit"], "limit must be an integer") from e
        if not 1 <= limit <= 200:
            raise _validation(["query", "limit"], "limit must be in 1..200")
        after = params.get("starting_after")
        ids = [i["id"] for i in items]
        start = 0
        if after:
            if after not in ids:
                raise _validation(["query", "starting_after"], "unknown cursor")
            start = ids.index(after) + 1
        chunk = items[start : start + limit]
        has_more = start + limit < len(items)
        doc = {
            "schema": "qt.page",
            "schema_version": 1,
            "data": chunk,
            "has_more": has_more,
            "next_cursor": chunk[-1]["id"] if has_more and chunk else None,
        }
        return httpx.Response(200, json=doc)

    def _store_url(self, key: str, **params: Any) -> str:
        query = "&".join(f"{k}={v}" for k, v in {**params, "exp": "1760746500", "sig": "fakesig"}.items())
        return f"{self.base_url}/v1/_store/{key}?{query}"

    def _new_job(self, kind: str, resource_type: str, resource_id: str, spec: Any = None) -> dict[str, Any]:
        job = {
            "schema": "qt.job",
            "schema_version": 1,
            "id": self.new_id("job"),
            "kind": kind,
            "state": "queued",
            "progress": {"fraction": 0.0, "stage": "queued"},
            "resource": {"type": resource_type, "id": resource_id},
            "created_at": FAKE_TIME,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "usage": {},
            "files_expire_at": None,
        }
        self.jobs[job["id"]] = job
        return job

    def _get(self, table: dict[str, dict[str, Any]], item_id: str) -> dict[str, Any]:
        if item_id not in table:
            raise _Problem(404, "not-found", "Not found", f"unknown id {item_id}")
        return table[item_id]

    # ---- endpoints --------------------------------------------------------------------------------------------

    def _health(self, request: httpx.Request) -> httpx.Response:
        doc = {"schema": "qt.health", "schema_version": 1, "status": "ok", "db": "ok", "store": "ok", "queue": "ok"}
        return httpx.Response(200, json={**doc, "version": "0.1.0"})

    def _meta(self, request: httpx.Request) -> httpx.Response:
        doc = {
            "schema": "qt.meta",
            "schema_version": 1,
            "api_version": "v1",
            "engine_version": "0.1.0",
            "schema_versions": {"qt.job": 1, "qt.replay_summary": 1},
            "limits": {
                "max_upload_bytes": self.max_upload_bytes,
                "part_size_bytes": self.part_size_bytes,
                "max_jobs_running": 2,
                "rate_limits": {"read": "120/min", "write": "30/min", "submit": "10/min"},
            },
            "formats": [
                "itch",
                "dbn",
                "etcsv",
                "parquet",
                "bitstamp",
                "bitfinex",
                "coinbase",
                "tardis_l2",
                "iex_deep_plus",
                "coinbase_level3",
                "kraken_l3",
                "hyperliquid_node",
            ],
            "zero_egress": False,
        }
        return httpx.Response(200, json=doc)

    def _openapi(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"openapi": "3.1.0", "info": {"title": "queuetruth", "version": "v1"}})

    def _tenant(self, request: httpx.Request) -> httpx.Response:
        doc = {
            "schema": "qt.tenant",
            "schema_version": 1,
            "id": FAKE_TENANT,
            "name": "fake",
            "plan": "dev",
            "zero_egress": False,
            "retention": dict(self.retention),
        }
        return httpx.Response(200, json=doc)

    def _create_key(self, request: httpx.Request) -> httpx.Response:
        body = self._body(request)
        scopes = body.get("scopes")
        if not isinstance(scopes, list) or not scopes or any(s not in SCOPE_RANK for s in scopes):
            raise _validation(["body", "scopes"], "scopes must be a non-empty list of read|write|admin")
        secret = f"qt_test_{self._counter + 1:032d}_XXXXXX"
        doc = self._add_key(secret, scopes, str(body.get("name", "")))
        return httpx.Response(201, json={**doc, "schema": "qt.api_key_created", "secret": secret})

    def _list_keys(self, request: httpx.Request) -> httpx.Response:
        return self._page(request, list(self.keys.values()))

    def _revoke_key(self, request: httpx.Request, key_id: str) -> httpx.Response:
        self._get(self.keys, key_id)["revoked_at"] = FAKE_TIME
        return httpx.Response(204)

    def _create_upload(self, request: httpx.Request) -> httpx.Response:
        body = self._body(request)
        purpose = body.get("purpose")
        if purpose not in ("market_data", "live_log", "sim_log", "shadow_spec", "model", "other"):
            raise _validation(["body", "purpose"], "invalid purpose")
        size = body.get("size_bytes")
        if not isinstance(size, int) or size <= 0:
            raise _validation(["body", "size_bytes"], "size_bytes must be a positive integer")
        sha = body.get("sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise _validation(["body", "sha256"], "sha256 must be 64 lower-case hex chars")
        if size > self.max_upload_bytes:
            raise _Problem(413, "payload-too-large", "Upload exceeds the size limit")
        up_id = self.new_id("up")
        filename = str(body.get("filename", "file"))
        n = math.ceil(size / self.part_size_bytes)
        key = f"t/{FAKE_TENANT}/uploads/{up_id}/{filename}"
        doc = {
            "schema": "qt.upload",
            "schema_version": 1,
            "id": up_id,
            "purpose": purpose,
            "filename": filename,
            "size_bytes": size,
            "sha256": sha,
            "status": "pending",
            "parts": [
                {"part_number": k, "url": self._store_url(key, part=k), "expires_at": FAKE_EXPIRES}
                for k in range(1, n + 1)
            ],
            "created_at": FAKE_TIME,
        }
        self.uploads[up_id] = doc
        self.upload_meta[up_id] = {"key": key, "parts": {}, "data": None}
        return httpx.Response(201, json=doc)

    def _get_upload(self, request: httpx.Request, up_id: str) -> httpx.Response:
        doc = self._get(self.uploads, up_id)
        if doc["status"] == "complete":
            data = self.upload_meta[up_id]["data"]
            doc["status"] = "verified" if hashlib.sha256(data).hexdigest() == doc["sha256"] else "failed"
        return httpx.Response(200, json=doc)

    def _complete_upload(self, request: httpx.Request, up_id: str) -> httpx.Response:
        doc = self._get(self.uploads, up_id)
        if doc["status"] != "pending":
            raise _Problem(409, "conflict", "Upload is not pending")
        parts = self._body(request).get("parts")
        meta = self.upload_meta[up_id]
        n = len(doc["parts"])
        if not isinstance(parts, list) or sorted(p.get("part_number") for p in parts) != list(range(1, n + 1)):
            raise _validation(["body", "parts"], f"parts must list part numbers 1..{n}")
        data = b""
        for p in sorted(parts, key=lambda p: p["part_number"]):
            stored = meta["parts"].get(p["part_number"])
            if stored is None or stored[1] != p.get("etag"):
                raise _validation(["body", "parts", p["part_number"]], "missing part or ETag mismatch")
            data += stored[0]
        meta["data"] = data
        doc["status"] = "complete"
        return httpx.Response(200, json=doc)

    def _delete_upload(self, request: httpx.Request, up_id: str) -> httpx.Response:
        self._get(self.uploads, up_id)
        del self.uploads[up_id]
        return httpx.Response(204)

    def _create_dataset(self, request: httpx.Request) -> httpx.Response:
        body = self._body(request)
        for name in ("name", "kind", "format", "venue"):
            if not isinstance(body.get(name), str):
                raise _validation(["body", name], f"{name} is required")
        if body["kind"] not in ("mbo", "l2"):
            raise _validation(["body", "kind"], "kind must be mbo or l2")
        ups = body.get("upload_ids")
        if not isinstance(ups, list) or not ups or any(u not in self.uploads for u in ups):
            raise _validation(["body", "upload_ids"], "unknown upload id")
        ds_id = self.new_id("ds")
        job = self._new_job("dataset_validate", "dataset", ds_id)
        doc = {
            "schema": "qt.dataset",
            "schema_version": 1,
            "id": ds_id,
            "name": body["name"],
            "kind": body["kind"],
            "format": body["format"],
            "venue": body["venue"],
            "status": "validating",
            "upload_ids": ups,
            "manifest": None,
            "data_quality": None,
            "created_at": FAKE_TIME,
            "job_id": job["id"],
        }
        self.datasets[ds_id] = doc
        return httpx.Response(202, json=doc, headers={"Location": f"/v1/jobs/{job['id']}"})

    def _list_datasets(self, request: httpx.Request) -> httpx.Response:
        return self._page(request, list(self.datasets.values()))

    def _get_dataset(self, request: httpx.Request, ds_id: str) -> httpx.Response:
        return httpx.Response(200, json=self._get(self.datasets, ds_id))

    def _delete_dataset(self, request: httpx.Request, ds_id: str) -> httpx.Response:
        doc = self._get(self.datasets, ds_id)
        doc["status"] = "deleted"
        return httpx.Response(202, json=self._new_job("deletion", "dataset", ds_id))

    def _validate_spec(self, kind: str, request: httpx.Request) -> dict[str, Any]:
        model = RUN_KINDS[kind][1]
        body = self._body(request)
        try:
            spec = model.model_validate(body)
        except ValidationError as e:
            errors = [{"loc": ["body", *e_["loc"]], "msg": e_["msg"]} for e_ in e.errors()]
            raise _Problem(422, "validation-error", "Request validation failed", None, errors) from e
        if getattr(spec, "out", None) is not None:
            raise _validation(["body", "out"], "out must be null")
        for name in ("dataset",):
            ref = getattr(spec, name, None)
            if ref is not None and not isinstance(ref, sp.DatasetIdRef):
                raise _validation(["body", name], "the HTTP API accepts only dataset_id references")
            if isinstance(ref, sp.DatasetIdRef) and ref.dataset_id not in self.datasets:
                raise _Problem(404, "not-found", "Not found", f"unknown dataset {ref.dataset_id}")
        return body

    def _estimate(self, request: httpx.Request, collection: str) -> httpx.Response:
        kind = COLLECTION_KIND[collection]
        self._validate_spec(kind, request)
        doc = {
            "schema": "qt.estimate",
            "schema_version": 1,
            "kind": kind,
            "records_est": 1000,
            "shadows_est": 10,
            "cpu_seconds_est": 1.5,
            "bytes_out_est": 4096,
            "cost_units": 0.25,
            "eta_seconds": 3.0,
        }
        return httpx.Response(200, json=doc)

    def _submit(self, request: httpx.Request, collection: str) -> httpx.Response:
        kind = COLLECTION_KIND[collection]
        spec = self._validate_spec(kind, request)
        prefix = {"replay": "rp", "calibration": "cal", "scorecard": "sc", "report": "rpt"}[kind]
        run_id = self.new_id(prefix)
        job = self._new_job(kind, kind, run_id, spec)
        self.runs[kind][run_id] = {"id": run_id, "spec": spec, "job_id": job["id"]}
        return httpx.Response(202, json={"id": run_id, "job": job}, headers={"Location": f"/v1/jobs/{job['id']}"})

    def _list_runs(self, request: httpx.Request, collection: str) -> httpx.Response:
        return self._page(request, [self._run_doc(r) for r in self.runs[COLLECTION_KIND[collection]].values()])

    @staticmethod
    def _run_doc(rec: dict[str, Any]) -> dict[str, Any]:
        doc = {"id": rec["id"], "spec": rec["spec"], "job_id": rec["job_id"]}
        if rec.get("summary") is not None:
            doc["summary"] = rec["summary"]
        return doc

    def _get_run(self, request: httpx.Request, collection: str, run_id: str) -> httpx.Response:
        return httpx.Response(200, json=self._run_doc(self._get(self.runs[COLLECTION_KIND[collection]], run_id)))

    def _get_report(self, request: httpx.Request, report_id: str) -> httpx.Response:
        rec = self._get(self.runs["report"], report_id)
        files = [{"name": n, "bytes": len(d)} for n, (d, _) in self.files.get(rec["job_id"], {}).items()]
        return httpx.Response(200, json={"id": rec["id"], "job_id": rec["job_id"], "files": files})

    def _list_jobs(self, request: httpx.Request) -> httpx.Response:
        kind = request.url.params.get("kind")
        state = request.url.params.get("state")
        items = [
            j
            for j in self.jobs.values()
            if (kind is None or j["kind"] == kind) and (state is None or j["state"] == state)
        ]
        return self._page(request, items)

    def _get_job(self, request: httpx.Request, job_id: str) -> httpx.Response:
        job = self._get(self.jobs, job_id)
        doc = dict(job)
        if self.auto_advance:
            self.advance(job_id)
        return httpx.Response(200, json=doc)

    def _cancel_job(self, request: httpx.Request, job_id: str) -> httpx.Response:
        job = self._get(self.jobs, job_id)
        if job["state"] in ("succeeded", "failed", "canceled", "expired"):
            raise _Problem(409, "job-terminal", "Job is in a terminal state", f"state {job['state']}")
        job.update(state="canceled", finished_at=FAKE_TIME)
        return httpx.Response(200, json=job)

    def _job_ready(self, job_id: str) -> dict[str, Any]:
        job = self._get(self.jobs, job_id)
        if job["state"] == "expired":
            raise _Problem(410, "gone", "Job files expired")
        if job["state"] != "succeeded":
            raise _Problem(409, "job-not-succeeded", "Job has not succeeded", f"state {job['state']}")
        return job

    def _job_files(self, request: httpx.Request, job_id: str) -> httpx.Response:
        self._job_ready(job_id)
        files = [
            {
                "name": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_type": ctype,
                "url": self._store_url(f"t/{FAKE_TENANT}/jobs/{job_id}/{name}"),
                "expires_at": FAKE_EXPIRES,
            }
            for name, (data, ctype) in sorted(self.files.get(job_id, {}).items())
        ]
        return httpx.Response(
            200, json={"schema": "qt.file_manifest", "schema_version": 1, "job_id": job_id, "files": files}
        )

    def _file_bytes(self, job_id: str, name: str) -> tuple[bytes, str]:
        files = self.files.get(job_id, {})
        if name not in files:
            raise _Problem(404, "not-found", "Not found", f"no file {name}")
        data, ctype = files[name]
        if (job_id, name) in self.corrupted:
            data = bytes(b ^ 0xFF for b in data[:1]) + data[1:] if data else b"x"
        return data, ctype

    def _serve(self, request: httpx.Request, data: bytes, ctype: str) -> httpx.Response:
        start = 0
        status = 200
        headers = {"Content-Type": ctype, "Accept-Ranges": "bytes"}
        rng = request.headers.get("Range")
        if rng:
            match = re.fullmatch(r"bytes=(\d+)-", rng.strip())
            if not match:
                raise _Problem(416, "range-not-satisfiable", "Unsupported Range")
            start = int(match.group(1))
            if start >= len(data):
                raise _Problem(416, "range-not-satisfiable", "Range not satisfiable")
            status = 206
            headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        payload = data[start:]
        headers["Content-Length"] = str(len(payload))
        cut = self._cut_after
        self._cut_after = None
        return httpx.Response(status, headers=headers, stream=_Stream(payload, cut, request))

    def _job_file(self, request: httpx.Request, job_id: str, name: str) -> httpx.Response:
        self._job_ready(job_id)
        data, ctype = self._file_bytes(job_id, name)
        if self.redirect_downloads:
            return httpx.Response(302, headers={"Location": self._store_url(f"t/{FAKE_TENANT}/jobs/{job_id}/{name}")})
        return self._serve(request, data, ctype)

    def _store_check(self, request: httpx.Request) -> None:
        if "Authorization" in request.headers:
            raise _Problem(400, "presigned-with-authorization", "Presigned URLs must not carry Authorization")
        if request.url.params.get("sig") != "fakesig":
            raise _Problem(403, "forbidden", "Bad signature")

    def _store_put(self, request: httpx.Request, key: str) -> httpx.Response:
        self._store_check(request)
        for up_id, meta in self.upload_meta.items():
            if meta["key"] == key:
                part = int(request.url.params.get("part", "0"))
                if not 1 <= part <= len(self.uploads[up_id]["parts"]):
                    raise _Problem(400, "bad-part", "Unknown part number")
                data = bytes(request.content)
                etag = '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'
                meta["parts"][part] = (data, etag)
                return httpx.Response(200, headers={"ETag": etag})
        raise _Problem(404, "not-found", "Not found")

    def _store_get(self, request: httpx.Request, key: str) -> httpx.Response:
        self._store_check(request)
        match = re.fullmatch(rf"t/{FAKE_TENANT}/jobs/([A-Za-z0-9_]+)/(.+)", key)
        if not match:
            raise _Problem(404, "not-found", "Not found")
        data, ctype = self._file_bytes(match.group(1), match.group(2))
        return self._serve(request, data, ctype)

    def _usage(self, request: httpx.Request) -> httpx.Response:
        group_by = request.url.params.get("group_by", "meter")
        if group_by not in ("meter", "day", "job"):
            raise _validation(["query", "group_by"], "group_by must be meter|day|job")
        doc = {
            "schema": "qt.usage_report",
            "schema_version": 1,
            "from": request.url.params.get("from", "2026-10-01T00:00:00Z"),
            "to": request.url.params.get("to", FAKE_TIME),
            "group_by": group_by,
            "rows": [{"key": "replay_records", "meter": "replay_records", "quantity": 1000.0, "unit": "records"}],
        }
        return httpx.Response(200, json=doc)

    def _get_retention(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=dict(self.retention))

    def _put_retention(self, request: httpx.Request) -> httpx.Response:
        body = self._body(request)
        for name in ("uploads_days", "outputs_days"):
            v = body.get(name)
            if not isinstance(v, int) or not 1 <= v <= 365:
                raise _validation(["body", name], f"{name} must be in 1..365")
        self.retention = {"uploads_days": body["uploads_days"], "outputs_days": body["outputs_days"]}
        return httpx.Response(200, json=dict(self.retention))

    def _tombstones(self, request: httpx.Request) -> httpx.Response:
        return self._page(request, [])


class _Stream(httpx.SyncByteStream):
    """Response body that optionally fails with `httpx.ReadError` after `cut` bytes."""

    def __init__(self, data: bytes, cut: int | None, request: httpx.Request) -> None:
        self.data, self.cut, self.request = data, cut, request

    def __iter__(self) -> Iterator[bytes]:
        if self.cut is None:
            yield self.data
            return
        yield self.data[: self.cut]
        raise httpx.ReadError("connection reset by peer (injected)", request=self.request)


__all__ = ["FAKE_API_KEY", "FAKE_TENANT", "FakeService", "Fault"]
