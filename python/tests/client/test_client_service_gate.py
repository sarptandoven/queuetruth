"""Gate test (gate-2): the client against a running queuetruth service.

Needs `QT_GATE_API_URL` and `QT_GATE_API_KEY` (an admin key from `queuetruth-service admin create-tenant`) for a
locally started api + worker; `QT_GATE_DATASET_ID` (a `ready` dataset of that tenant) adds a replay job that is
waited for and whose files are downloaded with sha256 verification. Skipped with the reason when unset.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from queuetruth.client import Client
from queuetruth.errors import QtApiError
from queuetruth.schemas import models as m

URL = os.environ.get("QT_GATE_API_URL")
KEY = os.environ.get("QT_GATE_API_KEY")
DATASET = os.environ.get("QT_GATE_DATASET_ID")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (URL and KEY), reason="QT_GATE_API_URL / QT_GATE_API_KEY not set (no running service)"),
]


@pytest.fixture
def client() -> Client:
    return Client(URL, KEY, max_retries=2)


def test_service_read_endpoints_and_problems(client: Client) -> None:
    assert client.health().status in ("ok", "degraded")
    meta = client.meta()
    assert meta.api_version == "v1" and meta.limits.part_size_bytes > 0
    assert client.openapi()["openapi"].startswith("3.1")
    assert isinstance(client.tenant(), m.Tenant)
    assert isinstance(client.list_datasets(limit=1), m.Page)
    assert isinstance(client.jobs.list(limit=1), m.Page)
    with pytest.raises(QtApiError) as ei:
        client.jobs.get("job_00000000000000000000000000")
    assert ei.value.status == 404
    m.Problem.model_validate(ei.value.problem)
    with pytest.raises(QtApiError) as ei:
        Client(URL, "qt_test_invalid").tenant()
    assert ei.value.status == 401


@pytest.mark.skipif(not DATASET, reason="QT_GATE_DATASET_ID not set")
def test_service_replay_job_end_to_end(client: Client, tmp_path: Path) -> None:
    spec = {"dataset": {"dataset_id": DATASET}, "rank_oracle": True}
    first = client.submit("replay", spec, idempotency_key=f"gate-{os.getpid()}")
    again = client.submit("replay", spec, idempotency_key=f"gate-{os.getpid()}")
    assert again.replayed and again.job.id == first.job.id
    job = client.jobs.wait(first.job.id, timeout=600, raise_on_failure=True)
    assert job.state == "succeeded"
    record = client.get_run("replay", first.id)
    assert isinstance(record.summary, m.ReplaySummary)
    paths = client.jobs.download_all(job.id, tmp_path)
    assert any(p.name == "summary.json" for p in paths)
