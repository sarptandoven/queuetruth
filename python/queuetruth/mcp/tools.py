"""MCP tool table (SPEC 4.6): names in the normative order, argument models, input and output JSON Schemas.

Input schemas come from Pydantic argument models (the Spec schemas of SPEC 4.2 are embedded where a tool takes a
spec); output schemas are the SPEC 3.10 document schemas exactly as written to `schemas/json` (`qt_local_read_summary`
declares the common `qt.*` envelope and validates the full document by its `schema` field; `qt_explain_metric` and
`qt_local_list_outputs` declare the shapes given in SPEC 4.6). Tools marked `local` read local output directories;
`submit` tools are listed only with `--allow-submit`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from queuetruth.mcp.metrics import METRICS, MetricExplanation
from queuetruth.schemas import specs as sp
from queuetruth.schemas.export import DRAFT, json_schema
from queuetruth.schemas.models import DOC_MODELS

_ULID = r"[0-9A-Za-z]{26}"


def _service_id(prefix: str, what: str) -> Any:
    return Annotated[str, Field(pattern=f"^{prefix}_{_ULID}$", description=f"{what} id ({prefix}_<ULID>)")]


DatasetId = _service_id("ds", "dataset")
JobId = _service_id("job", "job")
ReplayId = _service_id("rp", "replay")
CalibrationId = _service_id("cal", "calibration")
ScorecardId = _service_id("sc", "scorecard")
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=255, description="reuse to make a retry safe")]
LocalPath = Annotated[str, Field(min_length=1, description="path under QT_MCP_ROOT (relative paths from the root)")]
ShadowId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$")]
MetricName = Literal[tuple(METRICS)]  # type: ignore[valid-type]


class Args(BaseModel):
    """Base of tool arguments: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")


class NoArgs(Args):
    """No arguments."""


class ListDatasetsArgs(Args):
    """`qt_list_datasets`."""

    limit: Annotated[int, Field(ge=1, le=200)] | None = None
    starting_after: DatasetId | None = None


class DatasetArgs(Args):
    """`qt_describe_dataset`."""

    dataset_id: DatasetId


class EstimateArgs(Args):
    """`qt_estimate` (runtime: the spec is validated for `kind` by the client)."""

    kind: Literal["replay", "calibration", "scorecard"]
    spec: dict[str, Any]


class EstimateSchema(Args):
    """`qt_estimate` as listed: the spec is a ReplaySpec, CalibrationSpec or ScorecardSpec matching `kind`."""

    kind: Literal["replay", "calibration", "scorecard"]
    spec: sp.ReplaySpec | sp.CalibrationSpec | sp.ScorecardSpec


class JobArgs(Args):
    """`qt_get_job`, `qt_list_job_files`."""

    job_id: JobId


class ReplayArgs(Args):
    """`qt_get_replay`."""

    id: ReplayId


class CalibrationArgs(Args):
    """`qt_get_calibration`."""

    id: CalibrationId


class ScorecardArgs(Args):
    """`qt_get_scorecard`."""

    id: ScorecardId


class MetricArgs(Args):
    """`qt_explain_metric`."""

    name: MetricName


class SubmitArgs(Args):
    """Submit tools at runtime (the spec is validated for the tool's kind by the client)."""

    spec: dict[str, Any]
    idempotency_key: IdempotencyKey | None = None


class SubmitReplaySchema(Args):
    """`qt_submit_replay`."""

    spec: sp.ReplaySpec
    idempotency_key: IdempotencyKey | None = None


class SubmitCalibrationSchema(Args):
    """`qt_submit_calibration`."""

    spec: sp.CalibrationSpec
    idempotency_key: IdempotencyKey | None = None


class SubmitScorecardSchema(Args):
    """`qt_submit_scorecard`."""

    spec: sp.ScorecardSpec
    idempotency_key: IdempotencyKey | None = None


class SubmitReportSchema(Args):
    """`qt_submit_report` (`inputs` are job ids)."""

    spec: sp.ReportSpec
    idempotency_key: IdempotencyKey | None = None


class PathArgs(Args):
    """`qt_local_read_summary`, `qt_local_list_outputs`."""

    path: LocalPath


class LocalWhyArgs(Args):
    """`qt_local_why`."""

    dataset_path: LocalPath
    shadows_path: LocalPath
    shadow_id: ShadowId


class OutputEntry(BaseModel):
    """One `qt.*` document found under a local path."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str
    schema_: str = Field(alias="schema")


class OutputsListing(BaseModel):
    """Output of `qt_local_list_outputs`: `{entries: [{path, schema}]}`."""

    model_config = ConfigDict(extra="forbid")

    entries: list[OutputEntry]


@dataclass(frozen=True)
class ToolDef:
    """One MCP tool."""

    name: str
    title: str
    description: str
    args: type[Args]
    output: str
    schema_args: type[Args] | None = None
    local: bool = False
    submit: bool = False
    kind: str | None = None


#: Every tool in the normative order of SPEC 4.6.
TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        "qt_meta",
        "Service metadata",
        "Service version, schema versions, limits, input formats, zero-egress.",
        NoArgs,
        "qt.meta",
    ),
    ToolDef(
        "qt_list_datasets",
        "List datasets",
        "One page of the tenant's datasets (qt.page of qt.dataset); pass next_cursor as starting_after.",
        ListDatasetsArgs,
        "qt.page",
    ),
    ToolDef(
        "qt_describe_dataset",
        "Describe dataset",
        "One dataset: status, manifest (partitions, rows, validation status) and data-quality verdict.",
        DatasetArgs,
        "qt.dataset",
    ),
    ToolDef(
        "qt_estimate",
        "Estimate job cost",
        "Records, shadows, CPU seconds, output bytes, cost units and ETA of a replay, calibration or scorecard spec "
        "(HTTP specs reference datasets by dataset_id and leave out null).",
        EstimateArgs,
        "qt.estimate",
        schema_args=EstimateSchema,
    ),
    ToolDef("qt_get_job", "Get job", "State, progress, error problem and usage of a job.", JobArgs, "qt.job"),
    ToolDef(
        "qt_list_job_files",
        "List job files",
        "Output files of a succeeded job with sizes and sha256; each file is also returned as a resource_link "
        "(presigned URL, TTL <= 15 min).",
        JobArgs,
        "qt.file_manifest",
    ),
    ToolDef(
        "qt_get_replay",
        "Get replay summary",
        "qt.replay_summary of a finished replay (error with the job state while it runs).",
        ReplayArgs,
        "qt.replay_summary",
        kind="replay",
    ),
    ToolDef(
        "qt_get_calibration",
        "Get calibration result",
        "qt.calibration_result of a finished calibration (error with the job state while it runs).",
        CalibrationArgs,
        "qt.calibration_result",
        kind="calibration",
    ),
    ToolDef(
        "qt_get_scorecard",
        "Get scorecard summary",
        "qt.scorecard_summary of a finished scorecard (error with the job state while it runs).",
        ScorecardArgs,
        "qt.scorecard_summary",
        kind="scorecard",
    ),
    ToolDef(
        "qt_explain_metric",
        "Explain metric",
        "Definition, formula and properness of a queuetruth metric (brier, logloss, fill_rate_bias, mcnemar, ...).",
        MetricArgs,
        "metric",
    ),
    ToolDef(
        "qt_submit_replay",
        "Submit replay",
        "Starts a replay job and returns its qt.job handle; poll qt_get_job, then qt_get_replay.",
        SubmitArgs,
        "qt.job",
        schema_args=SubmitReplaySchema,
        submit=True,
        kind="replay",
    ),
    ToolDef(
        "qt_submit_calibration",
        "Submit calibration",
        "Starts a calibration job and returns its qt.job handle; poll qt_get_job, then qt_get_calibration.",
        SubmitArgs,
        "qt.job",
        schema_args=SubmitCalibrationSchema,
        submit=True,
        kind="calibration",
    ),
    ToolDef(
        "qt_submit_scorecard",
        "Submit scorecard",
        "Starts a scorecard job and returns its qt.job handle; poll qt_get_job, then qt_get_scorecard.",
        SubmitArgs,
        "qt.job",
        schema_args=SubmitScorecardSchema,
        submit=True,
        kind="scorecard",
    ),
    ToolDef(
        "qt_submit_report",
        "Submit report",
        "Starts an HTML report job over finished job ids and returns its qt.job handle; files via qt_list_job_files.",
        SubmitArgs,
        "qt.job",
        schema_args=SubmitReportSchema,
        submit=True,
        kind="report",
    ),
    ToolDef(
        "qt_local_read_summary",
        "Read local summary",
        "Reads one qt.* summary JSON (a file, or the summary of an output directory) under QT_MCP_ROOT; read-only.",
        PathArgs,
        "summary",
        local=True,
    ),
    ToolDef(
        "qt_local_list_outputs",
        "List local outputs",
        "Lists the qt.* JSON documents under a directory below QT_MCP_ROOT with their schema names; read-only.",
        PathArgs,
        "outputs",
        local=True,
    ),
    ToolDef(
        "qt_local_why",
        "Explain a shadow fill",
        "Event-by-event why-trace (qt.why_trace) of one shadow order on a local dataset; read-only.",
        LocalWhyArgs,
        "qt.why_trace",
        local=True,
    ),
)

TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in TOOLS)


def summary_envelope_schema() -> dict[str, Any]:
    """Output schema of `qt_local_read_summary`: any SPEC 3.10 document (full validation by its `schema`)."""
    return {
        "$schema": DRAFT,
        "title": "qt.summary",
        "type": "object",
        "properties": {"schema": {"enum": sorted(DOC_MODELS)}, "schema_version": {"const": 1}},
        "required": ["schema", "schema_version"],
        "additionalProperties": True,
    }


@cache
def output_schema(tool: ToolDef) -> dict[str, Any]:
    """The tool's output JSON Schema."""
    if tool.output == "metric":
        return {"$schema": DRAFT, **MetricExplanation.model_json_schema()}
    if tool.output == "outputs":
        return {"$schema": DRAFT, **OutputsListing.model_json_schema(by_alias=True)}
    if tool.output == "summary":
        return summary_envelope_schema()
    return json_schema(tool.output, DOC_MODELS[tool.output])


@cache
def input_schema(tool: ToolDef) -> dict[str, Any]:
    """The tool's input JSON Schema."""
    model = tool.schema_args or tool.args
    return {"$schema": DRAFT, **model.model_json_schema()}


__all__ = ["TOOLS", "TOOL_NAMES", "OutputEntry", "OutputsListing", "ToolDef", "input_schema", "output_schema"]
