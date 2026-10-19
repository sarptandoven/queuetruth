"""Job-creating resources: `/v1/replays`, `/v1/calibrations`, `/v1/scorecards` (estimate, submit, list, get) and
`/v1/reports` (submit, get). Specs are the SPEC 4.2 models; references must be tenant-owned ids (see `refs`) and the
compute knobs (`threads`, `particles`, `bootstrap.b`, `bootstrap_b`, `perm_b`) stay within `QT_COMPUTE_LIMITS`
(422 otherwise, security review M4)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from psycopg.types.json import Jsonb

from queuetruth.schemas import models as m
from queuetruth.schemas import specs as sp
from queuetruth_service import idempotency
from queuetruth_service.api_models import (
    CalibrationPage,
    CalibrationRecord,
    ReplayPage,
    ReplayRecord,
    ReportRecord,
    ScorecardPage,
    ScorecardRecord,
    Submitted,
)
from queuetruth_service.auth import Auth, ReadAuth, SubmitAuth
from queuetruth_service.context import ctx
from queuetruth_service.db import tenant_tx
from queuetruth_service.documents import fetch_one, page, page_doc, run_doc
from queuetruth_service.estimate import estimate
from queuetruth_service.ids import new_id
from queuetruth_service.jobs import create_job, enqueue_hook
from queuetruth_service.problems import not_found as nf
from queuetruth_service.problems import validation
from queuetruth_service.refs import check_compute, check_job_succeeded, check_spec

router = APIRouter(prefix="/v1")
RunId = Annotated[str, Path(max_length=64)]
Limit = Annotated[int, Query(ge=1, le=200)]
LOCATION = {202: {"headers": {"Location": {"description": "URL of the job", "schema": {"type": "string"}}}}}


async def _submit(
    request: Request, auth: Auth, idem: str, kind: str, prefix: str, table: str, spec: dict[str, Any]
) -> Any:
    c = ctx(request)
    check_compute(spec, c.settings.compute_limits)

    def execute(conn: Any) -> idempotency.Outcome:
        if kind == "report":
            if spec.get("out") is not None:
                raise validation(["body", "out"], "out must be null for the HTTP API")
            for jid in spec.get("inputs", []):
                check_job_succeeded(conn, auth.tenant_id, str(jid))
        else:
            check_spec(conn, auth.tenant_id, spec)
        rid = new_id(prefix)
        job = create_job(conn, auth.tenant_id, kind, spec, kind, rid)
        conn.execute(
            f"insert into {table} (id, tenant_id, job_id, spec) values (%s, %s, %s, %s)",
            (rid, auth.tenant_id, job["id"], Jsonb(spec)),
        )
        body = {"id": rid, "job": job}
        return 202, body, {"Location": f"/v1/jobs/{job['id']}"}, enqueue_hook(c, job["id"], auth)

    return await idempotency.run(request, auth, idem, execute)


def _estimate(request: Request, auth: Auth, kind: str, spec: dict[str, Any]) -> Any:
    c = ctx(request)
    check_compute(spec, c.settings.compute_limits)
    with tenant_tx(c.db(), auth) as conn:
        check_spec(conn, auth.tenant_id, spec)
        return estimate(conn, auth.tenant_id, kind, spec)


def _get(request: Request, auth: Auth, table: str, rid: str) -> Any:
    with tenant_tx(ctx(request).db(), auth) as conn:
        row = fetch_one(conn, f"select * from {table} where id = %s and tenant_id = %s", (rid, auth.tenant_id))
    if row is None:
        raise nf(table[:-1])
    return run_doc(row)


def _list(request: Request, auth: Auth, table: str, limit: int, starting_after: str | None) -> Any:
    with tenant_tx(ctx(request).db(), auth) as conn:
        rows, more = page(conn, table, auth.tenant_id, limit, starting_after)
    return page_doc([run_doc(r) for r in rows], more)


def _dump(spec: Any) -> dict[str, Any]:
    return spec.model_dump(mode="json")


# ---- replays ----------------------------------------------------------------------------------------------------


@router.post("/replays/estimate", response_model=m.Estimate, summary="Estimate a replay")
def estimate_replay(request: Request, spec: sp.ReplaySpec, auth: ReadAuth) -> Any:
    """`qt.estimate`."""
    return _estimate(request, auth, "replay", _dump(spec))


@router.post("/replays", status_code=202, response_model=Submitted, responses=LOCATION, summary="Submit a replay")
async def submit_replay(request: Request, spec: sp.ReplaySpec, idem: idempotency.IdemKey, auth: SubmitAuth) -> Any:
    """`{id, job}` + `Location: /v1/jobs/{job_id}`."""
    return await _submit(request, auth, idem, "replay", "rp", "replays", _dump(spec))


@router.get("/replays", response_model=ReplayPage, summary="List replays")
def list_replays(request: Request, auth: ReadAuth, limit: Limit = 50, starting_after: str | None = None) -> Any:
    """`qt.page` of `{id, spec, job_id, summary?}`."""
    return _list(request, auth, "replays", limit, starting_after)


@router.get("/replays/{replay_id}", response_model=ReplayRecord, summary="Get a replay")
def get_replay(request: Request, replay_id: RunId, auth: ReadAuth) -> Any:
    """`{id, spec, job_id, summary?: qt.replay_summary}`."""
    return _get(request, auth, "replays", replay_id)


# ---- calibrations -----------------------------------------------------------------------------------------------


@router.post("/calibrations/estimate", response_model=m.Estimate, summary="Estimate a calibration")
def estimate_calibration(request: Request, spec: sp.CalibrationSpec, auth: ReadAuth) -> Any:
    """`qt.estimate`."""
    return _estimate(request, auth, "calibration", _dump(spec))


@router.post(
    "/calibrations", status_code=202, response_model=Submitted, responses=LOCATION, summary="Submit a calibration"
)
async def submit_calibration(
    request: Request, spec: sp.CalibrationSpec, idem: idempotency.IdemKey, auth: SubmitAuth
) -> Any:
    """`{id, job}` + `Location`."""
    return await _submit(request, auth, idem, "calibration", "cal", "calibrations", _dump(spec))


@router.get("/calibrations", response_model=CalibrationPage, summary="List calibrations")
def list_calibrations(request: Request, auth: ReadAuth, limit: Limit = 50, starting_after: str | None = None) -> Any:
    """`qt.page` of `{id, spec, job_id, summary?}`."""
    return _list(request, auth, "calibrations", limit, starting_after)


@router.get("/calibrations/{calibration_id}", response_model=CalibrationRecord, summary="Get a calibration")
def get_calibration(request: Request, calibration_id: RunId, auth: ReadAuth) -> Any:
    """`{id, spec, job_id, summary?: qt.calibration_result}`."""
    return _get(request, auth, "calibrations", calibration_id)


# ---- scorecards -------------------------------------------------------------------------------------------------


@router.post("/scorecards/estimate", response_model=m.Estimate, summary="Estimate a scorecard")
def estimate_scorecard(request: Request, spec: sp.ScorecardSpec, auth: ReadAuth) -> Any:
    """`qt.estimate`."""
    return _estimate(request, auth, "scorecard", _dump(spec))


@router.post("/scorecards", status_code=202, response_model=Submitted, responses=LOCATION, summary="Submit a scorecard")
async def submit_scorecard(
    request: Request, spec: sp.ScorecardSpec, idem: idempotency.IdemKey, auth: SubmitAuth
) -> Any:
    """`{id, job}` + `Location`."""
    return await _submit(request, auth, idem, "scorecard", "sc", "scorecards", _dump(spec))


@router.get("/scorecards", response_model=ScorecardPage, summary="List scorecards")
def list_scorecards(request: Request, auth: ReadAuth, limit: Limit = 50, starting_after: str | None = None) -> Any:
    """`qt.page` of `{id, spec, job_id, summary?}`."""
    return _list(request, auth, "scorecards", limit, starting_after)


@router.get("/scorecards/{scorecard_id}", response_model=ScorecardRecord, summary="Get a scorecard")
def get_scorecard(request: Request, scorecard_id: RunId, auth: ReadAuth) -> Any:
    """`{id, spec, job_id, summary?: qt.scorecard_summary}`."""
    return _get(request, auth, "scorecards", scorecard_id)


# ---- reports ----------------------------------------------------------------------------------------------------


@router.post("/reports", status_code=202, response_model=Submitted, responses=LOCATION, summary="Submit a report")
async def submit_report(request: Request, spec: sp.ReportSpec, idem: idempotency.IdemKey, auth: SubmitAuth) -> Any:
    """`ReportSpec` whose `inputs` are ids of succeeded jobs of this tenant."""
    return await _submit(request, auth, idem, "report", "rpt", "reports", _dump(spec))


@router.get("/reports/{report_id}", response_model=ReportRecord, summary="Get a report")
def get_report(request: Request, report_id: RunId, auth: ReadAuth) -> Any:
    """`{id, job_id, files}` (files once the job succeeded)."""
    with tenant_tx(ctx(request).db(), auth) as conn:
        row = fetch_one(conn, "select * from reports where id = %s and tenant_id = %s", (report_id, auth.tenant_id))
        if row is None:
            raise nf("report")
        files = conn.execute(
            "select name, bytes, sha256 from job_files where job_id = %s and tenant_id = %s order by name",
            (row["job_id"], auth.tenant_id),
        ).fetchall()
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "files": [{"name": n, "bytes": int(b), "sha256": s} for n, b, s in files],
    }
