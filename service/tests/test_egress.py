"""Zero-egress (SPEC 8.8, O-EGRESS-1): with `QT_ZERO_EGRESS=true` a job's child process can connect only to the
database and the store endpoint; every other connection raises; the biller is forced to Noop."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Iterator

import pytest
from svc_support import insert_job

from queuetruth_service import netguard
from queuetruth_service.billing import NoopBiller, make_biller
from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.executor import run_child
from queuetruth_service.settings import Settings
from queuetruth_service.worker import Worker

TARGET = "queuetruth_service.testing:fake_connect"


def listener() -> tuple[socket.socket, int]:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)

    def accept() -> None:
        while True:
            try:
                c, _ = s.accept()
                c.close()
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    return s, int(s.getsockname()[1])


@pytest.fixture
def servers() -> Iterator[dict[str, int]]:
    socks = {name: listener() for name in ("db", "store", "other")}
    yield {name: port for name, (_, port) in socks.items()}
    for s, _ in socks.values():
        s.close()


def test_allowlist_from_dsn_and_endpoint():
    assert netguard.allowlist_from("host=db.internal port=6543 dbname=q", "http://seaweedfs:8333") == [
        ("db.internal", 6543),
        ("seaweedfs", 8333),
    ]
    assert netguard.allowlist_from("postgresql://u@pg/q", "https://s3.example") == [("pg", 5432), ("s3.example", 443)]
    assert netguard.allowlist_from("host=/tmp/sock port=5432", None) == []


def test_o_egress_1_child_only_reaches_db_and_store(servers, tmp_path):
    s = Settings(
        zero_egress=True,
        database_url_app=f"host=127.0.0.1 port={servers['db']} dbname=q user=qt_app",
        store="s3",
        s3_endpoint=f"http://127.0.0.1:{servers['store']}",
        biller="stripe",
        stripe_api_key="sk_test_FAKE",
    )
    assert isinstance(make_biller(s), NoopBiller)
    allow = netguard.allowlist_from(s.app_dsn, s.s3_endpoint)
    targets = [["127.0.0.1", servers["db"]], ["127.0.0.1", servers["store"]], ["127.0.0.1", servers["other"]]]

    async def go(allow_list):
        return await run_child(TARGET, {"targets": targets}, str(tmp_path), timeout_s=60, allow=allow_list)

    res = asyncio.run(go(allow))
    assert res.status == "ok", res.error
    r = res.summary["results"]
    assert r[f"127.0.0.1:{servers['db']}"] == "ok" and r[f"raw 127.0.0.1:{servers['db']}"] == "ok"
    assert r[f"127.0.0.1:{servers['store']}"] == "ok" and r[f"raw 127.0.0.1:{servers['store']}"] == "ok"
    assert r[f"127.0.0.1:{servers['other']}"] == "EgressBlocked"
    assert r[f"raw 127.0.0.1:{servers['other']}"] == "EgressBlocked"
    # without the guard the same child reaches everything (the test servers are real)
    open_res = asyncio.run(go(None))
    assert open_res.summary["results"][f"127.0.0.1:{servers['other']}"] == "ok"


def test_guard_blocks_udp_and_allows_unix_sockets():
    import tempfile

    short = tempfile.mkdtemp(prefix="qtng-", dir="/tmp")
    netguard.install([("127.0.0.1", 1)])
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with pytest.raises(netguard.EgressBlocked):
            u.sendto(b"x", ("127.0.0.1", 9))
        u.close()
        path = f"{short}/s.sock"
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(path)
        srv.listen(1)
        cli = socket.socket(socket.AF_UNIX)
        cli.connect(path)
        cli.close()
        srv.close()
    finally:
        netguard.uninstall()
        import shutil

        shutil.rmtree(short, ignore_errors=True)


@pytest.mark.requires_postgres
def test_o_egress_1_worker_installs_guard_in_zero_egress_mode(svc, servers):
    job = insert_job(svc)
    svc.db.q(
        "update jobs set spec = %s where id = %s",
        (json.dumps({"targets": [["127.0.0.1", servers["other"]]]}), job),
    )

    async def run(zero: bool) -> dict:
        svc.db.q("update jobs set state = 'queued' where id = %s", (job,))
        svc.db.q("delete from job_files where job_id = %s", (job,))
        pool = await make_async_pool(svc.db.worker)
        try:
            w = Worker(
                svc.settings.model_copy(update={"zero_egress": zero}), pool, svc.store, None, targets={"replay": TARGET}
            )
            assert await w.handle(job) == "succeeded"
        finally:
            await pool.close()
        key = svc.db.q("select object_key from job_files where job_id = %s and name = 'results.json'", (job,))[0][0]
        return json.loads(svc.store.path(key).read_text())

    assert asyncio.run(run(True))[f"127.0.0.1:{servers['other']}"] == "EgressBlocked"
    assert asyncio.run(run(False))[f"127.0.0.1:{servers['other']}"] == "ok"
    # the per-tenant flag has the same effect
    svc.db.q("update tenants set zero_egress = true where id = %s", (svc.tenants["A"],))
    assert asyncio.run(run(False))[f"127.0.0.1:{servers['other']}"] == "EgressBlocked"
