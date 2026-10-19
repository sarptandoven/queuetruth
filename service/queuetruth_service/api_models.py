"""Request bodies and response wrappers of the HTTP API that are not shared `qt.*` documents (SPEC 4.5).

Shared documents (`qt.job`, `qt.upload`, ...) come from `queuetruth.schemas.models`; specs from
`queuetruth.schemas.specs`. Pages are typed subclasses of `qt.page` so the OpenAPI document describes their items.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from queuetruth.schemas import models as m
from queuetruth.schemas import specs as sp

Hex64 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
INGEST_FORMATS = ("itch", "dbn", "etcsv", "parquet", "bitstamp", "bitfinex", "coinbase", "tardis_l2")


class Body(BaseModel):
    """Base of request bodies: unknown fields rejected."""

    model_config = ConfigDict(extra="forbid")


class KeyCreate(Body):
    """`POST /v1/keys`."""

    scopes: Annotated[list[m.Scope], Field(min_length=1, max_length=3)]
    name: Annotated[str, Field(max_length=200)] = ""


class UploadCreate(Body):
    """`POST /v1/uploads`."""

    purpose: Literal["market_data", "live_log", "sim_log", "shadow_spec", "model", "other"]
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    size_bytes: Annotated[int, Field(ge=1)]
    sha256: Hex64
    content_type: Annotated[str, Field(max_length=200)] | None = None


class CompletedPart(Body):
    """One uploaded part and the ETag the store returned."""

    part_number: Annotated[int, Field(ge=1, le=10_000)]
    etag: Annotated[str, Field(min_length=1, max_length=200)]


class UploadComplete(Body):
    """`POST /v1/uploads/{id}/complete`."""

    parts: Annotated[list[CompletedPart], Field(min_length=1, max_length=10_000)]


class IngestOptions(Body):
    """`IngestSpec` minus `inputs` and `out` (inputs are the dataset's uploads; the worker picks the output)."""

    format: Literal["itch", "dbn", "etcsv", "parquet", "bitstamp", "bitfinex", "coinbase", "tardis_l2"] | None = None
    venue: str | None = None
    source_venue: str | None = None
    symbols: list[str] | None = None
    session_date: sp.DateStr | None = None
    midnight_utc_ns: int | None = None
    tz: Literal["America/New_York", "UTC"] | None = None
    instruments: sp.UploadRef | None = None
    price_exp: Annotated[int, Field(ge=-12, le=0)] = -9
    size_exp: int | None = None
    tick_size: str | None = None
    lot_size: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    strict: bool = False


class DatasetCreate(Body):
    """`POST /v1/datasets`."""

    name: Annotated[str, Field(min_length=1, max_length=200)]
    kind: Literal["mbo", "l2"]
    format: Literal["itch", "dbn", "etcsv", "parquet", "bitstamp", "bitfinex", "coinbase", "tardis_l2"]
    venue: Annotated[str, Field(min_length=1, max_length=64)]
    upload_ids: Annotated[list[str], Field(min_length=1, max_length=1000)]
    ingest: IngestOptions | None = None


class ApiKeyCreatedResponse(m.ApiKeyCreated):
    """`qt.api_key_created`; `secret` is null when an idempotent replay returns the stored response (secrets are
    never persisted, so they are shown exactly once)."""

    secret: str | None  # type: ignore[assignment]


class Submitted(BaseModel):
    """Response of a job-creating POST."""

    id: str
    job: m.Job


class ReplayRecord(BaseModel):
    """`GET /v1/replays/{id}`."""

    id: str
    spec: dict[str, Any]
    job_id: str
    summary: m.ReplaySummary | None = None


class CalibrationRecord(BaseModel):
    """`GET /v1/calibrations/{id}`."""

    id: str
    spec: dict[str, Any]
    job_id: str
    summary: m.CalibrationResult | None = None


class ScorecardRecord(BaseModel):
    """`GET /v1/scorecards/{id}`."""

    id: str
    spec: dict[str, Any]
    job_id: str
    summary: m.ScorecardSummary | None = None


class ReportFile(BaseModel):
    """One file of a report job."""

    name: str
    bytes: int
    sha256: Hex64


class ReportRecord(BaseModel):
    """`GET /v1/reports/{id}`."""

    id: str
    job_id: str
    files: list[ReportFile]


class Tombstone(BaseModel):
    """Deletion record (ids, counts, hashes, time; no data)."""

    id: str
    resource_type: str
    resource_id: str
    deleted_at: str
    object_count: int
    sha256s: list[str]


def _page(name: str, item: Any) -> type[m.Page]:
    cls = type(name, (m.Page,), {"__annotations__": {"data": list[item]}, "__doc__": f"`qt.page` of {name[:-4]}."})
    return cls  # type: ignore[return-value]


KeyPage = _page("ApiKeyPage", m.ApiKey)
DatasetPage = _page("DatasetPage", m.Dataset)
JobPage = _page("JobPage", m.Job)
ReplayPage = _page("ReplayRecordPage", ReplayRecord)
CalibrationPage = _page("CalibrationRecordPage", CalibrationRecord)
ScorecardPage = _page("ScorecardRecordPage", ScorecardRecord)
TombstonePage = _page("TombstonePage", Tombstone)
