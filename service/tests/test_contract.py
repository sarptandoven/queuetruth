"""Contract tests (O-API-1, O-API-5): schemathesis stateless checks on every operation of the in-process ASGI app
(OpenAPI 3.1): `not_a_server_error`, `status_code_conformance`, `content_type_conformance`,
`response_schema_conformance`; and the committed `service/openapi.json` equals the generated document."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    response_schema_conformance,
    status_code_conformance,
)

from queuetruth_service.export_openapi import document, render

OPENAPI = Path(__file__).resolve().parents[1] / "openapi.json"
CHECKS = [not_a_server_error, status_code_conformance, content_type_conformance, response_schema_conformance]


def test_o_api_5_committed_openapi_matches_generated():
    assert OPENAPI.read_text() == render(), "run `make openapi` to refresh service/openapi.json"


def test_openapi_shape():
    doc = document()
    assert doc["openapi"].startswith("3.1")
    op = doc["paths"]["/v1/replays"]["post"]
    assert "application/problem+json" in op["responses"]["422"]["content"]
    assert op["responses"]["422"]["content"]["application/problem+json"]["schema"]["$ref"].endswith("/Problem")
    assert any(p["name"] == "Idempotency-Key" and p["in"] == "header" and p["required"] for p in op["parameters"])
    assert "HTTPValidationError" not in doc["components"]["schemas"]
    assert doc["paths"]["/v1/health"]["get"]["security"] == []
    assert "{name}" in "".join(doc["paths"])  # file names keep '/' (path converter)
    expected = {
        ("get", "/v1/health"), ("get", "/v1/meta"), ("get", "/v1/tenant"), ("post", "/v1/keys"), ("get", "/v1/keys"),
        ("delete", "/v1/keys/{key_id}"), ("post", "/v1/uploads"), ("get", "/v1/uploads/{upload_id}"),
        ("post", "/v1/uploads/{upload_id}/complete"), ("delete", "/v1/uploads/{upload_id}"), ("post", "/v1/datasets"),
        ("get", "/v1/datasets"), ("get", "/v1/datasets/{dataset_id}"), ("delete", "/v1/datasets/{dataset_id}"),
        ("post", "/v1/replays/estimate"), ("post", "/v1/replays"), ("get", "/v1/replays"),
        ("get", "/v1/replays/{replay_id}"), ("post", "/v1/calibrations/estimate"), ("post", "/v1/calibrations"),
        ("get", "/v1/calibrations"), ("get", "/v1/calibrations/{calibration_id}"), ("post", "/v1/scorecards/estimate"),
        ("post", "/v1/scorecards"), ("get", "/v1/scorecards"), ("get", "/v1/scorecards/{scorecard_id}"),
        ("post", "/v1/reports"), ("get", "/v1/reports/{report_id}"), ("get", "/v1/jobs"), ("get", "/v1/jobs/{job_id}"),
        ("post", "/v1/jobs/{job_id}/cancel"), ("get", "/v1/jobs/{job_id}/files"),
        ("get", "/v1/jobs/{job_id}/files/{name}"), ("get", "/v1/usage"), ("get", "/v1/retention"),
        ("put", "/v1/retention"), ("get", "/v1/tombstones"), ("put", "/v1/_store/{key}"), ("get", "/v1/_store/{key}"),
    }  # fmt: skip
    got = {(m, p) for p, item in doc["paths"].items() for m in item}
    assert expected <= got, expected - got


@pytest.fixture
def api_schema(svc):
    schema = schemathesis.openapi.from_asgi("/v1/openapi.json", svc.client.app)
    schema.svc = svc  # type: ignore[attr-defined]
    return schema


schema = schemathesis.pytest.from_fixture("api_schema")


@pytest.mark.requires_postgres
@schema.parametrize()
@settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
def test_o_api_1_schemathesis_stateless(case, svc):
    headers = {"Authorization": f"Bearer {svc.keys['A']}"}
    if case.operation.method.upper() in ("POST", "PUT", "DELETE"):
        headers["Idempotency-Key"] = json.dumps([case.operation.path, case.operation.method])[:200]
    case.call_and_validate(headers=headers, checks=CHECKS)
