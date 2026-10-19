"""Service gate (O-PARITY-1 HTTP arm): an ephemeral PostgreSQL 16 cluster, the real
`queuetruth-service api` (uvicorn) and `queuetruth-service worker` processes with a LocalStore, a dataset uploaded
through `queuetruth.client.Client`, a replay job submitted and waited for over HTTP, every output downloaded with its
sha256 verified, and the summary and output digests compared with a direct `qt.replay` of the same spec."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from e2e_support import SYNTH, schema_validate, strip_paths, tree_digests

pytestmark = [pytest.mark.integration, pytest.mark.requires_core, pytest.mark.requires_postgres]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Stack:
    url: str
    key: str
    root: Path


def _wait_health(url: str, procs: list[subprocess.Popen], logs: Path, timeout: float = 60) -> None:
    import httpx

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for p in procs:
            if p.poll() is not None:
                tails = {f.name: f.read_text()[-1500:] for f in sorted(logs.glob("*.log"))}
                raise AssertionError(f"service process exited early with {p.returncode}: {tails}")
        try:
            if httpx.get(f"{url}/v1/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise AssertionError("api did not become healthy")


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    pytest.importorskip("queuetruth_service")
    from queuetruth_service.admin import create_tenant
    from queuetruth_service.db.runner import migrate
    from queuetruth_service.settings import Settings
    from queuetruth_service.testing import ExternalPg, PgCluster, pg_bin, reap_stale

    root = tmp_path_factory.mktemp("svc")
    if dsn := os.environ.get("QT_PG_DSN"):
        pg: PgCluster | ExternalPg = ExternalPg(dsn).start()
    else:
        bin_dir = pg_bin()
        if bin_dir is None:
            pytest.skip("requires_postgres: initdb/pg_ctl not found")
        reap_stale(bin_dir)
        pg = PgCluster(bin_dir).start()
    db = pg.create_database()
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("QT_")},
        "QT_ENV": "dev",
        "QT_DATABASE_URL": pg.dsn(db),
        "QT_DATABASE_URL_APP": pg.dsn(db, user="qt_app"),
        "QT_DATABASE_URL_WORKER": pg.dsn(db, user="qt_worker"),
        "QT_STORE": "local",
        "QT_STORE_ROOT": str(root / "store"),
        "QT_PUBLIC_BASE_URL": url,
        "QT_WORKER_CONCURRENCY": "1",
        "QT_HEARTBEAT_S": "1",
        "QT_SWEEP_INTERVAL_S": "2",
        "QT_MAINTENANCE_INTERVAL_S": "2",
        "QT_LOG_LEVEL": "WARNING",
    }
    procs: list[subprocess.Popen] = []
    try:
        migrate(env["QT_DATABASE_URL"], env["QT_DATABASE_URL_APP"], env["QT_DATABASE_URL_WORKER"])
        old = dict(os.environ)
        os.environ.update(env)
        try:
            tenant = create_tenant(env["QT_DATABASE_URL"], Settings(), "e2e")
        finally:
            os.environ.clear()
            os.environ.update(old)
        svc_bin = str(Path(sys.executable).parent / "queuetruth-service")
        logs = {n: open(root / f"{n}.log", "w") for n in ("api", "worker")}
        procs.append(
            subprocess.Popen(
                [svc_bin, "api", "--port", str(port)], env=env, stdout=logs["api"], stderr=subprocess.STDOUT
            )
        )
        procs.append(subprocess.Popen([svc_bin, "worker"], env=env, stdout=logs["worker"], stderr=subprocess.STDOUT))
        _wait_health(url, procs, root)
        yield Stack(url, tenant["key"], root)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()
        try:
            pg.drop_database(db)
        finally:
            pg.stop()


def _tar_dataset(src: Path, dest: Path) -> Path:
    with tarfile.open(dest, "w:gz") as t:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src)
            if (p.is_file() and rel.parts[0] in ("mbo", "trades_source")) or rel.as_posix() in (
                "manifest.json",
                "instruments.parquet",
                "data_quality.json",
            ):
                t.add(p, arcname=f"synth_small/{rel.as_posix()}")
    return dest


def test_replay_job_over_http_matches_direct_run(stack: Stack, tmp_path: Path) -> None:
    import queuetruth as qt
    from queuetruth.client import Client

    c = Client(stack.url, stack.key, timeout=60)
    assert c.health().status in ("ok", "healthy", "pass")
    archive = _tar_dataset(SYNTH, tmp_path / "synth_small.tar.gz")
    up = c.upload_file(archive, "market_data")
    up = c.wait_upload(up.id, timeout=120, poll_interval=0.3)
    assert up.status == "verified", up
    ds = c.create_dataset("synth_small", "mbo", "parquet", "cme", [up.id], ingest={"venue": "cme"})
    vjob = c.jobs.wait(ds.job_id, timeout=300)
    assert vjob.state == "succeeded", vjob.error
    assert c.get_dataset(ds.id).status == "ready"

    spec = {"dataset": {"dataset_id": ds.id}, "rank_oracle": True, "bbo": "change"}
    sub = c.submit("replay", spec, idempotency_key="e2e-replay-1")
    job = c.jobs.wait(sub.job.id, timeout=300)
    assert job.state == "succeeded", job.error
    rs = c.get_run("replay", sub.id).summary
    remote = rs.model_dump(mode="json", by_alias=True, exclude_none=False) if hasattr(rs, "model_dump") else rs
    schema_validate(remote, "qt.replay_summary")
    dl = tmp_path / "remote"
    paths = c.jobs.download_all(job.id, dl)
    man = {f.name: f for f in c.jobs.files(job.id).files}
    assert paths and set(man) == {p.relative_to(dl).as_posix() for p in paths}
    for p in paths:
        f = man[p.relative_to(dl).as_posix()]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == f.sha256 and p.stat().st_size == f.bytes

    # the direct run repeats what the service did: `ingest --format parquet` of the uploaded directory, then replay
    with tarfile.open(archive) as t:
        t.extractall(tmp_path / "x", filter="data")
    qt.ingest({"format": "parquet", "inputs": [{"path": str(tmp_path / "x" / "synth_small")}], "venue": "cme",
               "out": str(tmp_path / "ds")})  # fmt: skip
    local = qt.replay({**spec, "dataset": {"path": str(tmp_path / "ds")}, "out": str(tmp_path / "local")})
    a, b = strip_paths(local.model_dump(mode="json", by_alias=True)), strip_paths(remote)
    for k in ("run_id", "files"):  # run_id hashes the dataset by its dataset_id; files carry service names
        a.pop(k, None)
        b.pop(k, None)
    diff = {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}
    assert not diff, json.dumps(diff, default=str)[:3000]
    local_d = tree_digests(tmp_path / "local", skip=("summary.json",))
    remote_d = tree_digests(dl, skip=("summary.json",))
    common = sorted(set(local_d) & set(remote_d))
    assert common, (sorted(local_d), sorted(remote_d))
    assert {k: local_d[k] for k in common} == {k: remote_d[k] for k in common}
