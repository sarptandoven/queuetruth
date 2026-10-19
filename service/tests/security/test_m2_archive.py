"""M2 (security review, inverted): the decompression guard counts every member (entry cap, per-entry cost in the
ratio), checks the limits while reading headers (memory stays flat), refuses links and special files, and extraction
runs inside the resource-limited, timeout-bound, cancellable job child instead of the worker parent."""

from __future__ import annotations

import asyncio
import inspect
import json
import tarfile
import tracemalloc
import zipfile
from pathlib import Path

import pytest
from svc_support import insert_job, upload_bytes, verify_uploads

from queuetruth_service import executor, runners, worker
from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.runners import ArchiveError, extract_archive
from queuetruth_service.worker import Worker

N = 30_000


def _bomb(path: Path, n: int) -> Path:
    with tarfile.open(path, "w:gz", compresslevel=9) as t:
        d = tarfile.TarInfo("d")
        d.type = tarfile.DIRTYPE
        t.addfile(d)
        for i in range(n):
            ti = tarfile.TarInfo(f"d/{i:x}")
            ti.size = 0
            t.addfile(ti)
    return path


def test_member_count_bomb_is_refused_from_headers_with_flat_memory(tmp_path):
    arc = _bomb(tmp_path / "bomb.tgz", N)
    assert arc.stat().st_size < 16 * N
    tracemalloc.start()
    with pytest.raises(ArchiveError, match="more than 1000 entries"):
        extract_archive(arc, tmp_path / "x", max_members=1000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 2 << 20  # the reviewed version traced ~13 MB here (getmembers of all 30k members)
    assert list((tmp_path / "x").iterdir()) == []  # nothing written: the limit is checked before extraction
    assert runners.MAX_MEMBERS <= 100_000 and runners.ENTRY_OVERHEAD >= 4096
    # with the default cap the same bomb fails the ratio guard instead (each empty member costs a 4 KiB block)
    with pytest.raises(ArchiveError, match="decompression guard"):
        extract_archive(arc, tmp_path / "y")
    assert list((tmp_path / "y").iterdir()) == []


def test_empty_members_count_toward_the_ratio_and_links_are_refused(tmp_path):
    zero = tmp_path / "zeros.zip"
    with zipfile.ZipFile(zero, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(2000):
            z.writestr(f"e/{i}", b"")
    runners._guard(0, 2000, zero.stat().st_size)  # ~80 compressed bytes per entry: within the ratio
    with pytest.raises(ArchiveError, match="decompression guard"):
        runners._guard(0, 2000, 2000)  # ~1 byte per entry is not (each entry costs a 4 KiB block)
    for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
        arc = tmp_path / f"k{kind!r}.tar"
        with tarfile.open(arc, "w") as t:
            ti = tarfile.TarInfo("member")
            ti.type = kind
            ti.linkname = "/etc/passwd"
            t.addfile(ti)
        with pytest.raises(ArchiveError, match="unsupported member type"):
            extract_archive(arc, tmp_path / f"out{kind!r}")


def test_extraction_is_not_in_the_worker_parent():
    assert "extract_archive" not in inspect.getsource(worker)
    assert "to_thread(extract_archive" not in inspect.getsource(worker.Worker._download_upload)
    body = inspect.getsource(worker.Worker._body)
    assert "extract=prepared.extract" in body  # the plan travels to run_child
    assert "apply_extractions" in inspect.getsource(executor._child_entry)


@pytest.mark.requires_postgres
def test_job_child_extracts_and_guards_archives(svc, tmp_path, monkeypatch):
    def parent_must_not_extract(*a, **kw):
        raise AssertionError("the worker parent extracted an archive")

    monkeypatch.setattr(runners, "extract_archive", parent_must_not_extract)  # the child is another interpreter
    good = tmp_path / "live.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("canon/orders.parquet", b"o")
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("zeros.bin", b"\0" * (5 << 20))
    up_good = upload_bytes(svc, good.read_bytes(), filename="live.zip", purpose="live_log")
    up_bomb = upload_bytes(svc, bomb.read_bytes(), filename="bomb.zip", purpose="live_log")
    assert verify_uploads(svc) == {up_good: "verified", up_bomb: "verified"}

    async def run(up: str) -> tuple[str, str]:
        job = insert_job(svc)
        svc.db.q("update jobs set spec = %s where id = %s", (json.dumps({"live": {"upload_id": up}}), job))
        pool = await make_async_pool(svc.db.worker)
        try:
            w = Worker(svc.settings, pool, svc.store, None, targets={"replay": "queuetruth_service.testing:fake_echo"})
            assert await w.handle(job) is not None
        finally:
            await pool.close()
        return job, svc.db.q("select state from jobs where id = %s", (job,))[0][0]

    job, state = asyncio.run(run(up_good))
    assert state == "succeeded"
    key = svc.db.q("select object_key from job_files where job_id = %s and name = 'echo.json'", (job,))[0][0]
    echo = json.loads(svc.store.path(key).read_text())
    assert echo["spec"]["live"] == {"path": f"inputs/uploads/{up_good}/x/canon"}
    assert echo["listing"][f"inputs/uploads/{up_good}/x/canon"] == ["orders.parquet"]
    job, state = asyncio.run(run(up_bomb))
    assert state == "failed"
    err = svc.db.q("select error from jobs where id = %s", (job,))[0][0]
    assert err["code"] == "QT-INPUT-002" and "decompression guard" in err["detail"] and up_bomb in err["detail"]
