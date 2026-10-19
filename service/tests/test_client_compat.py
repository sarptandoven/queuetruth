"""Cross-check with `queuetruth.client.Client` (SPEC 4.5 as a client) against this service in process:
uploads through local-store part URLs, verification, datasets, submit with idempotent replay, job files with ranged
download and sha256 verification, problems as `QtApiError`."""

from __future__ import annotations

import os

import httpx
import pytest
from svc_support import seed_dataset, succeed_job, verify_uploads

pytestmark = pytest.mark.requires_postgres


class Bridge(httpx.BaseTransport):
    """Sync httpx transport that forwards to the in-process TestClient."""

    def __init__(self, tc) -> None:
        self.tc = tc

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        r = self.tc.request(
            request.method,
            str(request.url),
            content=request.read(),
            headers=dict(request.headers),
            follow_redirects=False,
        )
        return httpx.Response(r.status_code, headers=r.headers, content=r.content, request=request)


def test_client_against_service(svc, tmp_path):
    client_mod = pytest.importorskip("queuetruth.client")
    from queuetruth.errors import QtApiError
    from queuetruth.schemas import models as m

    c = client_mod.Client("http://testserver", svc.keys["A"], transport=Bridge(svc.client), max_retries=0)
    assert c.health().status == "ok"
    assert c.meta().limits.part_size_bytes == svc.settings.part_size_bytes
    assert c.tenant().id == svc.tenants["A"]
    assert c.openapi()["openapi"].startswith("3.1")
    created = c.create_key(["read"], "ci", idempotency_key="ck")
    assert created.secret
    with pytest.raises(Exception, match="shown only once"):
        c.create_key(["read"], "ci", idempotency_key="ck")
    data = os.urandom(5000)
    src = tmp_path / "fills.zip"
    src.write_bytes(data)
    up = c.upload_file(src, "live_log", idempotency_key="up1")
    assert up.status == "complete"
    verify_uploads(svc)
    assert c.wait_upload(up.id, timeout=5).status == "verified"
    ds = c.create_dataset("d", "mbo", "itch", "nasdaq", [up.id], idempotency_key="ds1")
    assert ds.status == "validating" and ds.job_id
    ready = seed_dataset(svc)
    sub = c.submit("replay", {"dataset": {"dataset_id": ready}}, idempotency_key="sub1")
    again = c.submit("replay", {"dataset": {"dataset_id": ready}}, idempotency_key="sub1")
    assert again.replayed and again.job.id == sub.job.id and sub.location == f"/v1/jobs/{sub.job.id}"
    assert c.estimate("replay", {"dataset": {"dataset_id": ready}}).records_est == 1000
    payload = os.urandom(3000)
    succeed_job(svc, sub.job.id, {"bbo.parquet": payload, "l2/date=2026-01-02/part-00000.parquet": b"l2"})
    assert c.jobs.wait(sub.job.id, timeout=5).state == "succeeded"
    paths = c.jobs.download_all(sub.job.id, tmp_path / "out")
    assert sorted(p.relative_to(tmp_path / "out").as_posix() for p in paths) == [
        "bbo.parquet",
        "l2/date=2026-01-02/part-00000.parquet",
    ]
    assert (tmp_path / "out" / "bbo.parquet").read_bytes() == payload
    rec = c.get_run("replay", sub.id)
    assert rec.job_id == sub.job.id and rec.summary is None
    assert isinstance(c.list_datasets(limit=1), m.Page) and isinstance(c.jobs.list(limit=2), m.Page)
    with pytest.raises(QtApiError) as ei:
        c.jobs.get("job_00000000000000000000000000")
    assert ei.value.status == 404 and ei.value.remote_code == "QT-USAGE-001"
    with pytest.raises(QtApiError) as ei:
        client_mod.Client("http://testserver", "qt_test_bad", transport=Bridge(svc.client), max_retries=0).tenant()
    assert ei.value.status == 401
    assert c.set_retention(10, 20).outputs_days == 20 and c.get_retention().uploads_days == 10
    assert isinstance(c.usage(), m.UsageReport)
    c.revoke_key(created.id)
