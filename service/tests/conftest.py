"""Service test fixtures: an ephemeral PostgreSQL 16 cluster per session (or `QT_PG_DSN`), a migrated template
database cloned per test, a moto S3 server, and an in-process app with two tenants.

Every test runs under a wall-clock deadline (`QT_TEST_DEADLINE_S`, default 300 s; `@pytest.mark.deadline(s)` per
test or module; 0 disables): at the deadline SIGALRM raises `DeadlineExceeded` in the main thread (the test fails and
the run continues); if the test still has not unwound `HARD_GRACE_S` later (for example the main thread is stuck in a
call that cannot be interrupted), faulthandler dumps every thread's stack to the real stderr and exits the process, so
a hang fails CI in minutes instead of blocking it (pytest-timeout is not a dependency)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # makes svc_support importable (unique name)

import faulthandler
import os
import shutil
import signal
import socket
import subprocess
import threading
from collections.abc import Iterator

import pytest
from svc_support import Db, Svc, make_svc

from queuetruth_service.db.runner import migrate
from queuetruth_service.testing import ExternalPg, PgCluster, pg_bin, reap_stale

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DEADLINE_S = float(os.environ.get("QT_TEST_DEADLINE_S", "300"))
HARD_GRACE_S = 60.0
_real_stderr_fd: int | None = None


class DeadlineExceeded(BaseException):
    """A test ran past its deadline (a BaseException so that broad `except Exception` handlers cannot swallow it)."""


def pytest_configure(config: pytest.Config) -> None:
    global _real_stderr_fd
    config.addinivalue_line("markers", "deadline(seconds): wall-clock limit of a test (default QT_TEST_DEADLINE_S)")
    if _real_stderr_fd is None:
        _real_stderr_fd = os.dup(2)  # output capture is suspended here; per-test fd capture would swallow the dump


def pytest_unconfigure(config: pytest.Config) -> None:
    global _real_stderr_fd
    if _real_stderr_fd is not None:
        os.close(_real_stderr_fd)
        _real_stderr_fd = None


@pytest.fixture(autouse=True)
def _deadline(request: pytest.FixtureRequest) -> Iterator[None]:
    """Per-test deadline (setup, call and teardown of function fixtures; see the module docstring)."""
    marker = request.node.get_closest_marker("deadline")
    seconds = float(marker.args[0]) if marker else DEFAULT_DEADLINE_S
    if seconds <= 0 or not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return
    nodeid = request.node.nodeid

    def fire(signum: int, frame: object) -> None:
        raise DeadlineExceeded(f"{nodeid} exceeded its {seconds:g} s deadline")

    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    faulthandler.dump_traceback_later(
        seconds + HARD_GRACE_S, exit=True, file=_real_stderr_fd if _real_stderr_fd is not None else 2
    )
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        faulthandler.cancel_dump_traceback_later()


def postgres_available() -> bool:
    return bool(os.environ.get("QT_PG_DSN")) or pg_bin() is not None


def core_available() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("queuetruth._core") is not None
    except (ImportError, ValueError):
        return False


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    checks = {
        "requires_postgres": (
            postgres_available,
            "requires_postgres: initdb/pg_ctl not found at QT_PG_BIN (default /opt/homebrew/opt/postgresql@16/bin) "
            "and QT_PG_DSN unset",
        ),
        "requires_core": (core_available, "requires_core: queuetruth._core is not built (run `make develop`)"),
        "requires_docker": (docker_available, "requires_docker: Docker daemon not running (`docker info` failed)"),
    }
    cache: dict[str, bool] = {}
    for item in items:
        for name, (fn, reason) in checks.items():
            if item.get_closest_marker(name):
                if name not in cache:
                    cache[name] = fn()
                if not cache[name]:
                    item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def pg() -> Iterator[PgCluster | ExternalPg]:
    """Session cluster (initdb + pg_ctl) or the `QT_PG_DSN` server."""
    if dsn := os.environ.get("QT_PG_DSN"):
        yield ExternalPg(dsn).start()
        return
    bin_dir = pg_bin()
    if bin_dir is None:
        pytest.skip("requires_postgres: initdb/pg_ctl not found (set QT_PG_BIN or QT_PG_DSN)")
    reap_stale(bin_dir)
    cluster = PgCluster(bin_dir).start()
    yield cluster
    cluster.stop()


@pytest.fixture(scope="session")
def template_db(pg: PgCluster | ExternalPg) -> Iterator[str]:
    """A database with every migration applied, cloned by `db`."""
    name = pg.create_database()
    # role passwords come from the DSNs (QT_PG_DSN servers authenticate TCP logins; the local cluster trusts them)
    migrate(pg.dsn(name), pg.dsn(name, user="qt_app"), pg.dsn(name, user="qt_worker"))
    yield name
    pg.drop_database(name)


@pytest.fixture
def db(pg: PgCluster | ExternalPg, template_db: str) -> Iterator[Db]:
    """Fresh migrated database."""
    name = pg.create_database(template=template_db)
    yield Db(name, pg.dsn(name), pg.dsn(name, user="qt_app"), pg.dsn(name, user="qt_worker"))
    pg.drop_database(name)


@pytest.fixture
def svc(db: Db, tmp_path: Path) -> Iterator[Svc]:
    """API with unlimited rate limits."""
    yield from make_svc(db, tmp_path)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def moto_endpoint() -> Iterator[str]:
    """A moto S3 server on 127.0.0.1 (no network beyond loopback)."""
    from moto.server import ThreadedMotoServer

    port = free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()
