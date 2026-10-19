"""Validation of spec references and compute knobs for the HTTP API (SPEC 4.2, 8.8): `out` must be null;
`DatasetRef` must be a `dataset_id`, `FileRef` an `upload_id` or `job_file` (no server-side paths); every id must
belong to the caller's tenant (queries filter by `tenant_id`, and RLS makes other tenants' ids invisible too, so they
are 404 like unknown ids) and be usable: datasets `ready`, uploads `verified`, job files of `succeeded` jobs (409
otherwise). `check_compute` bounds the compute knobs (security review M4)."""

from __future__ import annotations

from typing import Any

import psycopg

from queuetruth_service.documents import fetch_one
from queuetruth_service.problems import ProblemError, not_found, validation


def check_spec(conn: psycopg.Connection, tenant_id: str, spec: dict[str, Any]) -> None:
    """Raises a problem for the first invalid reference in `spec` (a JSON-mode dump of a Spec model)."""
    if spec.get("out") is not None:
        raise validation(["body", "out"], "out must be null for the HTTP API (the worker assigns the output)")
    _walk(conn, tenant_id, spec, ["body"])


def _walk(conn: psycopg.Connection, tenant_id: str, value: Any, loc: list[str | int]) -> None:
    if isinstance(value, dict):
        keys = frozenset(value)
        if keys == {"path"}:
            raise validation(loc, "local paths are not accepted by the HTTP API; use dataset_id, upload_id or job_file")
        if keys == {"dataset_id"}:
            check_dataset(conn, tenant_id, str(value["dataset_id"]))
            return
        if keys == {"upload_id"}:
            check_upload(conn, tenant_id, str(value["upload_id"]))
            return
        if keys == {"job_file"} and isinstance(value["job_file"], dict):
            jf = value["job_file"]
            check_job_file(conn, tenant_id, str(jf.get("job_id")), str(jf.get("name")))
            return
        for k, v in value.items():
            _walk(conn, tenant_id, v, [*loc, k])
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _walk(conn, tenant_id, v, [*loc, i])


def compute_violations(value: Any, limits: dict[str, int], loc: list[str | int]) -> list[dict[str, Any]]:
    """`errors` entries for every compute knob in `value` above its limit (`bootstrap.b` = `bootstrap: {b}`)."""
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, bool):
                continue
            if isinstance(v, int) and k in limits and v > limits[k]:
                out.append({"loc": [*loc, k], "msg": f"must be <= {limits[k]} on this service"})
            elif k == "bootstrap" and isinstance(v, dict) and isinstance(v.get("b"), int) and "bootstrap.b" in limits:
                if v["b"] > limits["bootstrap.b"]:
                    out.append({"loc": [*loc, k, "b"], "msg": f"must be <= {limits['bootstrap.b']} on this service"})
                out.extend(compute_violations({kk: vv for kk, vv in v.items() if kk != "b"}, limits, [*loc, k]))
            else:
                out.extend(compute_violations(v, limits, [*loc, k]))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.extend(compute_violations(v, limits, [*loc, i]))
    return out


def check_compute(spec: dict[str, Any], limits: dict[str, int]) -> None:
    """422 `validation-error` listing every compute knob above `QT_COMPUTE_LIMITS`."""
    errors = compute_violations(spec, limits, ["body"])
    if errors:
        raise ProblemError(
            422,
            "validation-error",
            "Request validation failed",
            "QT-CONFIG-001",
            "compute parameters exceed this service's limits (QT_COMPUTE_LIMITS)",
            errors=errors,
        )


def check_dataset(conn: psycopg.Connection, tenant_id: str, ds_id: str) -> None:
    """The dataset exists for this tenant and is ready."""
    row = fetch_one(conn, "select status from datasets where id = %s and tenant_id = %s", (ds_id, tenant_id))
    if row is None or row["status"] == "deleted":
        raise not_found(f"dataset {ds_id}")
    if row["status"] != "ready":
        raise ProblemError(409, "dataset-not-ready", "Dataset not ready", "QT-USAGE-001", f"dataset is {row['status']}")


def check_upload(conn: psycopg.Connection, tenant_id: str, up_id: str) -> None:
    """The upload exists for this tenant and its sha256 was verified."""
    row = fetch_one(conn, "select status from uploads where id = %s and tenant_id = %s", (up_id, tenant_id))
    if row is None:
        raise not_found(f"upload {up_id}")
    if row["status"] != "verified":
        raise ProblemError(
            409, "upload-not-verified", "Upload not verified", "QT-USAGE-001", f"upload is {row['status']}"
        )


def check_job_succeeded(conn: psycopg.Connection, tenant_id: str, job_id: str) -> None:
    """The job exists for this tenant and succeeded."""
    row = fetch_one(conn, "select state from jobs where id = %s and tenant_id = %s", (job_id, tenant_id))
    if row is None:
        raise not_found(f"job {job_id}")
    if row["state"] != "succeeded":
        raise ProblemError(409, "job-not-succeeded", "Job not succeeded", "QT-USAGE-001", f"job is {row['state']}")


def check_job_file(conn: psycopg.Connection, tenant_id: str, job_id: str, name: str) -> None:
    """The job succeeded and has a file `name`."""
    check_job_succeeded(conn, tenant_id, job_id)
    row = fetch_one(
        conn,
        "select 1 as x from job_files where job_id = %s and tenant_id = %s and name = %s",
        (job_id, tenant_id, name),
    )
    if row is None:
        raise not_found(f"file {name} of job {job_id}")
