"""Runner helpers and input resolution: safe archive extraction (path traversal, decompression guard), output
listing, content types, usage meters, the child executor's error boundary, and reference resolution into the job's
private work directory."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from svc_support import insert_job, seed_dataset, succeed_job, upload_bytes, verify_uploads

from queuetruth_service import runners
from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.executor import run_child
from queuetruth_service.worker import ClaimedJob, JobInputError, Worker


def test_zip_extraction_and_single_top_dir(tmp_path):
    z = tmp_path / "live.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("live/orders.parquet", b"o")
        f.writestr("live/events.parquet", b"e")
    root = runners.extract_archive(z, tmp_path / "x")
    assert root.name == "live" and sorted(p.name for p in root.iterdir()) == ["events.parquet", "orders.parquet"]


def test_zip_slip_rejected(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("../../escape.txt", b"x")
    with pytest.raises(runners.ArchiveError):
        runners.extract_archive(z, tmp_path / "x")
    assert not (tmp_path / "escape.txt").exists()


def test_tar_filter_and_ratio_guard(tmp_path):
    t = tmp_path / "evil.tar.gz"
    with tarfile.open(t, "w:gz") as f:
        info = tarfile.TarInfo("../../escape.txt")
        info.size = 1
        f.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(runners.ArchiveError):
        runners.extract_archive(t, tmp_path / "x")
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as f:
        f.writestr("zeros.bin", b"\0" * (5 << 20))
    with pytest.raises(runners.ArchiveError, match="decompression guard"):
        runners.extract_archive(bomb, tmp_path / "y")


def test_outputs_types_and_meters(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "x.parquet").write_bytes(b"1")
    (tmp_path / "summary.json").write_text("{}")
    assert [n for n, _ in runners.list_outputs(tmp_path)] == ["a/b/x.parquet", "summary.json"]
    assert (
        runners.content_type("r.html").startswith("text/html")
        and runners.content_type("x.bin") == "application/octet-stream"
    )
    assert runners.usage_meters("replay", {"stats": {"records": 5}, "shadows": {"n": 3}}, 12) == {
        "cpu_ms": (12.0, "ms"),
        "replay_records": (5.0, "records"),
        "shadow_orders": (3.0, "orders"),
    }
    assert runners.usage_meters("scorecard", {"n_orders": 9}, 1)["orders_scored"] == (9.0, "orders")
    cal = runners.usage_meters("calibration", {"n_shadows": 10, "held_out": {"a": {}, "b": {}}}, 1)
    assert cal["zoo_pairs"] == (20.0, "pairs")
    assert set(runners.TARGETS) >= {"replay", "calibration", "scorecard", "report", "dataset_validate"}


def test_executor_error_boundary(tmp_path, capfd):
    async def go(target):
        return await run_child(target, {}, str(tmp_path), timeout_s=60)

    r = asyncio.run(go("queuetruth_service.testing:fake_crash"))
    captured = capfd.readouterr()
    assert "RuntimeError: secret-internal-detail" not in captured.err and "RuntimeError" in captured.err
    assert r.status == "error" and r.error == {"code": "QT-ENGINE-001", "message": "internal error (RuntimeError)"}
    r = asyncio.run(go("queuetruth_service.testing:fake_fail"))
    assert r.error["code"] == "QT-DATA-001"
    r = asyncio.run(go("queuetruth_service.testing:fake_abort"))
    assert r.status == "crashed" and r.error["code"] == "QT-ENGINE-001"
    r = asyncio.run(go("queuetruth_service.runners:replay"))  # the real API stub / implementation
    assert r.status in ("ok", "error")


@pytest.mark.requires_postgres
def test_prepare_resolves_references_into_workdir(svc, tmp_path, monkeypatch):
    ds = seed_dataset(svc)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as f:
        f.writestr("canon/orders.parquet", b"o")
        f.writestr("canon/manifest.json", b"{}")
    live = upload_bytes(svc, buf.getvalue(), filename="live.zip", purpose="live_log")
    shadows = upload_bytes(svc, b"shadow-rows", filename="shadows.parquet", purpose="shadow_spec")
    verify_uploads(svc)
    prev = insert_job(svc)
    succeed_job(svc, prev, {"bbo.parquet": b"bbo", "nested/f.json": b"{}"})
    spec = {
        "live": {"upload_id": live},
        "sim": {"kind": "external", "log": {"job_file": {"job_id": prev, "name": "nested/f.json"}}},
        "dataset": {"dataset_id": ds},
        "markout_refs": ["mid"],
        "out": None,
    }

    def running(who: str, kind: str, job_spec: dict) -> ClaimedJob:
        # job transactions are bound to a running attempt (qt_bind_job), so the job must be claimed
        jid = insert_job(svc, who, kind, state="running")
        svc.db.q("update jobs set attempts = 1, spec = %s where id = %s", (json.dumps(job_spec), jid))
        return ClaimedJob(jid, svc.tenants[who], kind, job_spec, 1, kind, "res_x")

    async def go():
        pool = await make_async_pool(svc.db.worker)
        try:
            w = Worker(svc.settings, pool, svc.store, None)
            got = await w._prepare(running("A", "scorecard", spec), tmp_path)
            rep_spec = await w._prepare(running("A", "report", {"inputs": [prev]}), tmp_path / "r")
            bad = running("B", "replay", {"dataset": {"dataset_id": ds}})
            with pytest.raises(JobInputError):
                await w._prepare(bad, tmp_path / "b")  # tenant B cannot resolve tenant A's dataset
            sh = running("A", "replay", {"dataset": {"dataset_id": ds}, "shadows": {"upload_id": shadows}})
            sh_spec = await w._prepare(sh, tmp_path / "s")
            return got, rep_spec.spec, sh_spec.spec
        finally:
            await pool.close()

    (tmp_path / "r").mkdir()
    (tmp_path / "s").mkdir()
    prepared, rep_spec, sh_spec = asyncio.run(go())
    got = prepared.spec
    assert got["out"] == "out" and got["markout_refs"] == ["mid"]
    assert got["dataset"] == {"path": f"inputs/datasets/{ds}"}
    assert (tmp_path / got["dataset"]["path"] / "mbo/date=2026-01-02/instrument_id=1/part-00000.parquet").exists()
    # the archive is downloaded by the worker but extracted by the job child (security review M2)
    assert got["live"]["path"] == runners.extraction_token(0)
    assert prepared.extract == [
        {
            "token": runners.extraction_token(0),
            "archive": f"inputs/uploads/{live}/live.zip",
            "dest": f"inputs/uploads/{live}/x",
            "label": f"upload {live}",
        }
    ]
    assert not (tmp_path / f"inputs/uploads/{live}/x").exists()
    monkeypatch.chdir(tmp_path)
    child_spec = runners.apply_extractions(got, prepared.extract)
    assert child_spec["live"]["path"] == f"inputs/uploads/{live}/x/canon"
    assert (tmp_path / child_spec["live"]["path"] / "orders.parquet").read_bytes() == b"o"
    assert got["sim"]["log"] == {"path": f"inputs/jobs/{prev}/nested/f.json"}
    assert rep_spec["inputs"] == [f"inputs/jobs/{prev}"] and rep_spec["out"] == "out/report.html"
    assert (tmp_path / "r" / rep_spec["inputs"][0] / "nested" / "f.json").exists()
    assert sh_spec["shadows"] == {"path": f"inputs/uploads/{shadows}/shadows.parquet"}
    assert json.loads(json.dumps(got))  # plain JSON for the child
    assert not any(Path(p).is_absolute() for p in (got["dataset"]["path"], child_spec["live"]["path"]))
