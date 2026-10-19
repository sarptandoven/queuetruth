"""HTTP client against the in-process fake of the SPEC 4.5 API (`httpx.MockTransport`; no network)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from queuetruth.client import Client, new_idempotency_key, problem_slug
from queuetruth.client.testing import FAKE_API_KEY, FakeService
from queuetruth.client.transfer import safe_join
from queuetruth.errors import QtApiError, QtError
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as sp

REPO = Path(__file__).resolve().parents[3]


def sample(name: str) -> dict:
    return json.loads((REPO / "schemas" / "samples" / f"{name}.json").read_text())["sample"]


@pytest.fixture
def fake() -> FakeService:
    return FakeService()


@pytest.fixture
def client(fake: FakeService) -> Client:
    return fake.client()


def make_dataset(fake: FakeService, client: Client) -> str:
    up = client.create_upload("market_data", "day.itch", 3, hashlib.sha256(b"abc").hexdigest())
    ds = client.create_dataset("day", "mbo", "itch", "nasdaq", [up.id], ingest={"price_exp": -9})
    return ds.id


def replay_spec(ds_id: str) -> dict:
    return {"dataset": {"dataset_id": ds_id}, "rank_oracle": True}


def keys_of(fake: FakeService, method: str, prefix: str) -> list[str | None]:
    return [r.headers.get("Idempotency-Key") for r in fake.calls(method, prefix)]


# ---- idempotency and retries ---------------------------------------------------------------------------------


def test_idempotency_key_reused_on_retry_after_5xx(fake: FakeService, client: Client) -> None:
    ds = make_dataset(fake, client)
    fake.inject(status=503, method="POST", path_prefix="/v1/replays", times=2)
    sub = client.submit("replay", replay_spec(ds))
    keys = keys_of(fake, "POST", "/v1/replays")
    assert len(keys) == 3 and keys[0] and len(set(keys)) == 1
    assert len(fake.runs["replay"]) == 1 and sub.job.state == "queued"
    assert sub.location == f"/v1/jobs/{sub.job.id}" and len(fake.sleeps) == 2


def test_idempotency_key_reused_on_transport_error(fake: FakeService, client: Client) -> None:
    ds = make_dataset(fake, client)
    fake.inject(exc=httpx.ConnectError, method="POST", path_prefix="/v1/replays")
    client.submit("replay", replay_spec(ds))
    keys = keys_of(fake, "POST", "/v1/replays")
    assert len(keys) == 2 and keys[0] == keys[1]
    assert len(fake.runs["replay"]) == 1


def test_in_flight_409_is_retried_with_same_key(fake: FakeService, client: Client) -> None:
    ds = make_dataset(fake, client)
    fake.in_flight("k-1", times=2)
    sub = client.submit("replay", replay_spec(ds), idempotency_key="k-1")
    assert keys_of(fake, "POST", "/v1/replays") == ["k-1", "k-1", "k-1"]
    assert not sub.replayed


def test_same_key_same_body_replays_without_second_job(fake: FakeService, client: Client) -> None:
    ds = make_dataset(fake, client)
    first = client.submit("replay", replay_spec(ds), idempotency_key="same")
    second = client.submit("replay", replay_spec(ds), idempotency_key="same")
    assert second.replayed and not first.replayed
    assert second.id == first.id and second.job.id == first.job.id
    assert len(fake.runs["replay"]) == 1 and len([j for j in fake.jobs.values() if j["kind"] == "replay"]) == 1


def test_same_key_different_body_is_422_and_not_retried(fake: FakeService, client: Client) -> None:
    ds = make_dataset(fake, client)
    client.submit("replay", replay_spec(ds), idempotency_key="k")
    with pytest.raises(QtApiError) as ei:
        client.submit("replay", {**replay_spec(ds), "rank_oracle": False}, idempotency_key="k")
    assert ei.value.status == 422 and ei.value.remote_code == "idempotency-key-reused"
    assert len(fake.calls("POST", "/v1/replays")) == 2


def test_auto_keys_are_fresh_per_call_and_estimate_has_none(fake: FakeService, client: Client) -> None:
    ds = make_dataset(fake, client)
    client.submit("replay", replay_spec(ds))
    client.submit("replay", replay_spec(ds))
    keys = keys_of(fake, "POST", "/v1/replays")
    assert len(set(keys)) == 2 and all(keys)
    est = client.estimate("replay", replay_spec(ds))
    assert isinstance(est, m.Estimate) and est.kind == "replay"
    assert fake.calls("POST", "/v1/replays/estimate")[0].headers.get("Idempotency-Key") is None
    assert new_idempotency_key() != new_idempotency_key()


def test_retry_after_is_honoured(fake: FakeService, client: Client) -> None:
    fake.inject(status=429, path_prefix="/v1/tenant", headers={"Retry-After": "7"})
    assert client.tenant().id.startswith("ten_")
    assert fake.sleeps == [7.0]


def test_retries_exhausted_raise_problem(fake: FakeService) -> None:
    client = fake.client(max_retries=2)
    fake.inject(status=503, path_prefix="/v1/tenant", times=5)
    with pytest.raises(QtApiError) as ei:
        client.tenant()
    assert ei.value.status == 503 and ei.value.code == "QT-REMOTE-001"
    assert len(fake.calls("GET", "/v1/tenant")) == 3


def test_transport_errors_exhausted_raise_qterror(fake: FakeService) -> None:
    client = fake.client(max_retries=1)
    fake.inject(exc=httpx.ConnectError, path_prefix="/v1/meta", times=5)
    with pytest.raises(QtError) as ei:
        client.meta()
    assert not isinstance(ei.value, QtApiError)
    assert ei.value.code == "QT-REMOTE-001" and "ConnectError" in ei.value.message


# ---- problems -------------------------------------------------------------------------------------------------


def test_problem_parsing_404(fake: FakeService, client: Client) -> None:
    with pytest.raises(QtApiError) as ei:
        client.jobs.get("job_00000000000000000000009999")
    err = ei.value
    assert err.code == "QT-REMOTE-001" and err.status == 404 and err.remote_code == "not-found"
    assert err.problem["type"] == "https://queuetruth.dev/problems/not-found"
    assert err.problem["request_id"].startswith("req_") and err.hint
    m.Problem.model_validate(err.problem)
    assert str(err).startswith("QT-REMOTE-001: HTTP 404")
    assert err.to_document()["details"]["problem"] == err.problem


def test_problem_parsing_401_and_403(fake: FakeService) -> None:
    with pytest.raises(QtApiError) as ei:
        fake.client(api_key="qt_test_wrong").tenant()
    assert ei.value.status == 401 and "QT_API_KEY" in (ei.value.hint or "")
    secret = fake.client().create_key(["read"], "ro").secret
    with pytest.raises(QtApiError) as ei:
        fake.client(api_key=secret).create_upload("other", "a", 1, "0" * 64)
    assert ei.value.status == 403 and ei.value.remote_code == "forbidden"


def test_problem_parsing_422_errors(fake: FakeService, client: Client) -> None:
    with pytest.raises(QtApiError) as ei:
        client.set_retention(0, 30)
    assert ei.value.status == 422
    assert ei.value.problem["errors"] == [{"loc": ["body", "uploads_days"], "msg": "uploads_days must be in 1..365"}]


def test_non_problem_error_body_is_synthesized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>", headers={"X-Request-Id": "rid-1"})

    client = Client("http://qt.test", "k", transport=httpx.MockTransport(handler), sleep=lambda s: None, max_retries=1)
    with pytest.raises(QtApiError) as ei:
        client.tenant()
    p = ei.value.problem
    assert p["status"] == 502 and p["code"] == "http-502" and p["type"] == "about:blank"
    assert p["request_id"] == "rid-1" and "bad gateway" in p["detail"]
    assert problem_slug(p) == "http-502"
    m.Problem.model_validate(p)


def test_create_key_replay_never_returns_secret(fake: FakeService, client: Client) -> None:
    first = client.create_key(["read"], "ci", idempotency_key="key-1")
    assert first.secret
    with pytest.raises(QtError) as ei:
        client.create_key(["read"], "ci", idempotency_key="key-1")
    assert ei.value.code == "QT-REMOTE-001" and ei.value.details == {"key_id": first.id}


def test_terminal_cancel_409_is_not_retried(fake: FakeService, client: Client) -> None:
    ds = make_dataset(fake, client)
    job = client.submit("replay", replay_spec(ds)).job
    fake.finish_job(job.id)
    with pytest.raises(QtApiError) as ei:
        client.jobs.cancel(job.id)
    assert ei.value.status == 409 and len(fake.calls("POST", f"/v1/jobs/{job.id}/cancel")) == 1


# ---- job waiting ----------------------------------------------------------------------------------------------


def test_wait_polls_until_terminal(fake: FakeService, client: Client) -> None:
    fake.auto_advance = True
    fake.summaries["replay"] = sample("qt.replay_summary")
    ds = make_dataset(fake, client)
    sub = client.submit("replay", replay_spec(ds))
    seen: list[str] = []
    job = client.jobs.wait(sub.job.id, poll_interval=0.5, on_update=lambda j: seen.append(j.state))
    assert job.state == "succeeded" and seen == ["queued", "running", "succeeded"]
    assert fake.sleeps[-2:] == [0.5, 0.75]
    record = client.get_run("replay", sub.id)
    assert isinstance(record.summary, m.ReplaySummary) and record.job_id == sub.job.id


def test_wait_raise_on_failure(fake: FakeService, client: Client) -> None:
    ds = make_dataset(fake, client)
    job = client.submit("replay", replay_spec(ds)).job
    problem = {**sample("qt.problem"), "status": 500, "code": "QT-ENGINE-001", "title": "worker lost"}
    fake.finish_job(job.id, state="failed", error=problem)
    assert client.jobs.wait(job.id).state == "failed"
    with pytest.raises(QtApiError) as ei:
        client.jobs.wait(job.id, raise_on_failure=True)
    assert ei.value.problem["title"] == "worker lost"


def test_wait_timeout(fake: FakeService) -> None:
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    client = fake.client(sleep=sleep, clock=lambda: now[0])
    ds = make_dataset(fake, client)
    job = client.submit("replay", replay_spec(ds)).job
    with pytest.raises(QtError) as ei:
        client.jobs.wait(job.id, timeout=5.0, poll_interval=1.0)
    assert ei.value.code == "QT-REMOTE-001" and ei.value.details == {"job_id": job.id, "state": "queued"}
    assert now[0] <= 5.0


# ---- downloads ------------------------------------------------------------------------------------------------


def finished_job(fake: FakeService, client: Client, files: dict[str, bytes]) -> str:
    ds = make_dataset(fake, client)
    job = client.submit("replay", replay_spec(ds)).job
    fake.finish_job(job.id, files=files)
    return job.id


@pytest.mark.parametrize("redirect", [False, True])
def test_download_resumes_after_cut(fake: FakeService, client: Client, tmp_path: Path, redirect: bool) -> None:
    fake.redirect_downloads = redirect
    data = bytes(range(256)) * 40
    job_id = finished_job(fake, client, {"trades.parquet": data})
    fake.cut_next_download(1000)
    out = client.jobs.download(job_id, "trades.parquet", tmp_path / "trades.parquet")
    assert out.read_bytes() == data and not (tmp_path / "trades.parquet.part").exists()
    gets = [r for r in fake.requests if r.method == "GET" and "trades.parquet" in r.url.path]
    ranges = [r.headers.get("Range") for r in gets if (r.url.path.startswith("/v1/_store/") or not redirect)]
    assert ranges == [None, "bytes=1000-"]
    if redirect:
        store = [r for r in gets if r.url.path.startswith("/v1/_store/")]
        assert store and all("Authorization" not in r.headers for r in store)
        api = [r for r in gets if r.url.path.startswith("/v1/jobs/")]
        assert all(r.headers["Authorization"] == f"Bearer {FAKE_API_KEY}" for r in api)


def test_download_sha256_mismatch_raises(fake: FakeService, client: Client, tmp_path: Path) -> None:
    job_id = finished_job(fake, client, {"summary.json": b'{"a": 1}'})
    fake.corrupt(job_id, "summary.json")
    with pytest.raises(QtError) as ei:
        client.jobs.download(job_id, "summary.json", tmp_path / "summary.json")
    assert ei.value.code == "QT-REMOTE-001" and "sha256 mismatch" in ei.value.message
    assert not (tmp_path / "summary.json").exists() and not (tmp_path / "summary.json.part").exists()


def test_download_gives_up_after_repeated_cuts(fake: FakeService, tmp_path: Path) -> None:
    client = fake.client(max_retries=1)
    job_id = finished_job(fake, client, {"a.bin": b"x" * 100})
    fake.inject(exc=httpx.ReadError, path_prefix=f"/v1/jobs/{job_id}/files/", times=10)
    with pytest.raises(QtError) as ei:
        client.jobs.download(job_id, "a.bin", tmp_path / "a.bin")
    assert ei.value.code == "QT-REMOTE-001"


def test_download_all_with_subdirectories(fake: FakeService, client: Client, tmp_path: Path) -> None:
    files = {"summary.json": b"{}", "l2/date=2020-01-02/instrument_id=1/part-00000.parquet": b"PAR1" * 10, "e.bin": b""}
    job_id = finished_job(fake, client, files)
    paths = client.jobs.download_all(job_id, tmp_path / "out")
    got = {p.relative_to(tmp_path / "out").as_posix(): p.read_bytes() for p in paths}
    assert got == files


def test_files_before_success_is_409(fake: FakeService, client: Client) -> None:
    ds = make_dataset(fake, client)
    job = client.submit("replay", replay_spec(ds)).job
    with pytest.raises(QtApiError) as ei:
        client.jobs.files(job.id)
    assert ei.value.status == 409


@pytest.mark.parametrize("name", ["../x", "/etc/passwd", "a/../../b", "", "a\\b", "C:/x"])
def test_safe_join_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(QtError):
        safe_join(tmp_path, name)


# ---- uploads --------------------------------------------------------------------------------------------------


def test_upload_file_multipart(fake: FakeService, client: Client, tmp_path: Path) -> None:
    src = tmp_path / "orders.jsonl"
    src.write_bytes(b"0123456789" * 2 + b"xy")
    up = client.upload_file(src, "live_log")
    assert up.status == "complete" and up.size_bytes == 22 and len(up.parts) == 3
    puts = fake.calls("PUT", "/v1/_store/")
    assert len(puts) == 3 and all("Authorization" not in r.headers for r in puts)
    assert [len(r.content) for r in puts] == [8, 8, 6]
    assert client.wait_upload(up.id).status == "verified"


def test_upload_part_retry_and_url_refresh(fake: FakeService, client: Client, tmp_path: Path) -> None:
    src = tmp_path / "a.bin"
    src.write_bytes(b"abcdefgh" * 2)
    fake.inject(status=503, method="PUT", path_prefix="/v1/_store/")
    fake.inject(status=403, method="PUT", path_prefix="/v1/_store/")
    up = client.upload_file(src, "other", idempotency_key="up-1")
    assert up.status == "complete"
    assert len(fake.calls("GET", f"/v1/uploads/{up.id}")) == 1
    assert keys_of(fake, "POST", f"/v1/uploads/{up.id}/complete") == ["up-1.complete"]


def test_upload_empty_file_rejected(client: Client, tmp_path: Path) -> None:
    (tmp_path / "e").write_bytes(b"")
    with pytest.raises(QtError) as ei:
        client.upload_file(tmp_path / "e", "other")
    assert ei.value.code == "QT-INPUT-002"


# ---- specs, configuration, coverage of the remaining endpoints -----------------------------------------------


def test_spec_validated_locally(fake: FakeService, client: Client) -> None:
    with pytest.raises(QtError) as ei:
        client.submit("replay", {"dataset": {"dataset_id": "ds_x"}, "out": "/tmp/x"})
    assert ei.value.code == "QT-CONFIG-001" and "out" in ei.value.message
    with pytest.raises(QtError) as ei:
        client.submit("replay", {"dataset": {"dataset_id": "ds_x"}, "bogus": 1})
    assert ei.value.code == "QT-CONFIG-001" and "bogus" in ei.value.message
    with pytest.raises(QtError) as ei:
        client.submit("zoo", {})
    assert ei.value.code == "QT-USAGE-001"
    assert fake.calls("POST", "/v1/replays") == []


def test_spec_model_instances_send_only_set_fields(fake: FakeService, client: Client) -> None:
    ds = make_dataset(fake, client)
    client.submit("replay", sp.ReplaySpec(dataset=sp.DatasetIdRef(dataset_id=ds), depth_levels=5))
    body = json.loads(fake.calls("POST", "/v1/replays")[0].content)
    assert body == {"dataset": {"dataset_id": ds}, "depth_levels": 5}


def test_http_api_rejects_path_refs(fake: FakeService, client: Client) -> None:
    with pytest.raises(QtApiError) as ei:
        client.submit("replay", {"dataset": {"path": "/data/x"}})
    assert ei.value.status == 422


def test_env_configuration(monkeypatch: pytest.MonkeyPatch, fake: FakeService) -> None:
    monkeypatch.setenv("QT_API_URL", "http://qt.test/v1/")
    monkeypatch.setenv("QT_API_KEY", FAKE_API_KEY)
    client = Client(transport=fake.transport())
    assert client.base_url == "http://qt.test" and client.tenant().name == "fake"
    assert FAKE_API_KEY not in repr(client)
    monkeypatch.delenv("QT_API_KEY")
    keyless = Client(transport=fake.transport())
    assert keyless.health().status == "ok"
    with pytest.raises(QtError) as ei:
        keyless.tenant()
    assert ei.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError):
        Client("ftp://x")


def test_ids_are_percent_encoded(fake: FakeService, client: Client) -> None:
    with pytest.raises(QtApiError):
        client.jobs.get("../keys")
    assert fake.requests[-1].url.raw_path == b"/v1/jobs/..%2Fkeys"


def test_pagination_and_iteration(fake: FakeService, client: Client) -> None:
    for _ in range(5):
        make_dataset(fake, client)
    page = client.list_datasets(limit=2)
    assert len(page.data) == 2 and page.has_more and page.next_cursor == page.data[-1]["id"]
    names = [d.id for d in client.iter_datasets(page_size=2)]
    assert names == list(fake.datasets) and len(names) == 5


def test_every_endpoint_round_trips(fake: FakeService, client: Client) -> None:
    assert isinstance(client.health(), m.Health)
    meta = client.meta()
    assert meta.limits.part_size_bytes == 8 and client.meta() is meta
    assert client.openapi()["openapi"].startswith("3.1")
    assert client.tenant().retention.uploads_days == 30
    created = client.create_key(["read", "write"], "ci")
    assert created.secret and any(k["id"] == created.id for k in client.list_keys().data)
    client.revoke_key(created.id)
    with pytest.raises(QtApiError):
        Client("http://qt.test", created.secret, transport=fake.transport()).tenant()
    ds = make_dataset(fake, client)
    assert client.get_dataset(ds).status == "validating"
    assert client.delete_dataset(ds).kind == "deletion"
    up = client.create_upload("other", "f", 3, "0" * 64)
    assert client.get_upload(up.id).status == "pending"
    client.delete_upload(up.id)
    ds2 = make_dataset(fake, client)
    cal = {"kind": "alloc", "family": "power3", "dataset": {"dataset_id": ds2}, "train": {}, "test": {}}
    assert client.estimate("calibration", cal).kind == "calibration"
    cal_sub = client.submit("calibration", cal)
    fake.finish_job(cal_sub.job.id, summary=sample("qt.calibration_result"))
    assert isinstance(client.get_run("calibration", cal_sub.id).summary, m.CalibrationResult)
    assert len(client.list_runs("calibration").data) == 1
    sc = {"live": {"upload_id": "up_1"}, "sim": {"kind": "exact"}, "dataset": {"dataset_id": ds2}}
    sc_sub = client.submit("scorecard", sc)
    assert client.get_run("scorecard", sc_sub.id).summary is None
    rpt = client.submit("report", {"inputs": [sc_sub.job.id]})
    fake.finish_job(rpt.job.id, files={"report.html": b"<html></html>"})
    assert client.get_report(rpt.id).files == [{"name": "report.html", "bytes": 13}]
    assert client.jobs.submit("replay", replay_spec(ds2)).kind == "replay"
    assert len(client.jobs.list(kind="replay").data) == 1
    assert {j.kind for j in client.jobs.iter(state="queued")} >= {"replay", "scorecard"}
    queued = client.jobs.list(kind="replay").data[0]["id"]
    assert client.jobs.cancel(queued).state == "canceled"
    assert client.usage(group_by="day").group_by == "day"
    assert client.get_retention().outputs_days == 30
    assert client.set_retention(10, 20) == m.Retention(uploads_days=10, outputs_days=20)
    assert client.list_tombstones().data == []
