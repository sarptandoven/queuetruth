"""Test support: an ephemeral PostgreSQL cluster (`initdb` + `pg_ctl`, unix socket only, random port; SPEC 8.3) or
a uniquely named database on `QT_PG_DSN`, and fake job targets for the child-process executor (importable by
spawned children). Never used by the running service.

The socket directory is a short path under `/tmp` (macOS limits AF_UNIX paths to 104 bytes); `listen_addresses=''`
means no TCP listener, so clusters never collide and the developer's own server is never touched. `PG*` variables
are stripped from the environment. Each cluster records its owner pid; `reap_stale` stops clusters whose owner died.
"""

from __future__ import annotations

import atexit
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

DEFAULT_PG_BIN = "/opt/homebrew/opt/postgresql@16/bin"


def pg_bin() -> Path | None:
    """`QT_PG_BIN` (default Homebrew postgresql@16) when it has initdb and pg_ctl."""
    p = Path(os.environ.get("QT_PG_BIN", DEFAULT_PG_BIN))
    return p if (p / "initdb").exists() and (p / "pg_ctl").exists() else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    return port if port != 5432 else _free_port()


def clean_env() -> dict[str, str]:
    """Environment without `PG*` variables."""
    return {k: v for k, v in os.environ.items() if not k.startswith("PG")} | {"LC_ALL": "C", "LANG": "C"}


@dataclass
class PgCluster:
    """A throwaway cluster owned by this process."""

    bin_dir: Path
    root: Path = field(init=False)
    port: int = field(init=False)
    superuser: str = "postgres"
    _started: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="qtpg-", dir="/tmp"))
        self.port = _free_port()

    @property
    def data_dir(self) -> Path:
        """Cluster data directory."""
        return self.root / "data"

    def dsn(self, dbname: str = "postgres", user: str | None = None) -> str:
        """libpq DSN over the unix socket."""
        return f"host={self.root} port={self.port} dbname={dbname} user={user or self.superuser}"

    def start(self) -> PgCluster:
        """initdb + start (about 1 s)."""
        env = clean_env()
        subprocess.run(
            [
                str(self.bin_dir / "initdb"),
                "-D",
                str(self.data_dir),
                "-U",
                self.superuser,
                "-A",
                "trust",
                "-E",
                "UTF8",
                "--locale=C",
                "--no-sync",
                "--no-instructions",
            ],
            check=True,
            capture_output=True,
            env=env,
        )
        opts = (
            f"-k {self.root} -p {self.port} -c listen_addresses='' -c fsync=off -c synchronous_commit=off "
            "-c full_page_writes=off -c max_connections=300 -c shared_buffers=32MB"
        )
        subprocess.run(
            [
                str(self.bin_dir / "pg_ctl"),
                "-D",
                str(self.data_dir),
                "-l",
                str(self.root / "pg.log"),
                "-o",
                opts,
                "-w",
                "-t",
                "30",
                "start",
            ],
            check=True,
            capture_output=True,
            env=env,
        )
        (self.root / "owner.pid").write_text(str(os.getpid()))
        self._started = True
        atexit.register(self.stop)
        return self

    def pid(self) -> int | None:
        """Postmaster pid."""
        try:
            return int((self.data_dir / "postmaster.pid").read_text().split("\n", 1)[0])
        except (OSError, ValueError):
            return None

    def stop(self) -> None:
        """Immediate stop and removal."""
        if self._started:
            self._started = False
            r = subprocess.run(
                [str(self.bin_dir / "pg_ctl"), "-D", str(self.data_dir), "-m", "immediate", "-w", "stop"],
                capture_output=True,
                env=clean_env(),
            )
            if r.returncode != 0 and (pid := self.pid()):
                try:
                    os.kill(pid, signal.SIGQUIT)
                except ProcessLookupError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def create_database(self, name: str | None = None, template: str | None = None) -> str:
        """`CREATE DATABASE` (optionally from a template); returns the name."""
        name = name or f"qt_{secrets.token_hex(4)}"
        with psycopg.connect(self.dsn(), autocommit=True) as c:
            tpl = f' TEMPLATE "{template}"' if template else ""
            c.execute(f'CREATE DATABASE "{name}"{tpl}')
        return name

    def drop_database(self, name: str) -> None:
        """`DROP DATABASE ... WITH (FORCE)`."""
        with psycopg.connect(self.dsn(), autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@dataclass
class ExternalPg:
    """`QT_PG_DSN` mode (CI `postgres:16` service): databases are created on that server and dropped afterwards."""

    base_dsn: str

    def start(self) -> ExternalPg:
        """No-op (the server already runs)."""
        return self

    def dsn(self, dbname: str = "postgres", user: str | None = None) -> str:
        """DSN for one database (and optionally another role)."""
        kw: dict[str, Any] = {"dbname": dbname}
        if user:
            kw["user"] = user
        return psycopg.conninfo.make_conninfo(self.base_dsn, **kw)

    def create_database(self, name: str | None = None, template: str | None = None) -> str:
        """`CREATE DATABASE`."""
        name = name or f"qt_test_{secrets.token_hex(4)}"
        with psycopg.connect(self.base_dsn, autocommit=True) as c:
            tpl = f' TEMPLATE "{template}"' if template else ""
            c.execute(f'CREATE DATABASE "{name}"{tpl}')
        return name

    def drop_database(self, name: str) -> None:
        """`DROP DATABASE ... WITH (FORCE)`."""
        with psycopg.connect(self.base_dsn, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    def stop(self) -> None:
        """No-op."""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap_stale(bin_dir: Path) -> int:
    """Stops and deletes `/tmp/qtpg-*` clusters whose owning process is gone."""
    n = 0
    for root in Path("/tmp").glob("qtpg-*"):
        try:
            owner = int((root / "owner.pid").read_text())
        except (OSError, ValueError):
            continue
        if _alive(owner):
            continue
        subprocess.run(
            [str(bin_dir / "pg_ctl"), "-D", str(root / "data"), "-m", "immediate", "-w", "stop"],
            capture_output=True,
            env=clean_env(),
        )
        shutil.rmtree(root, ignore_errors=True)
        n += 1
    return n


def wait_gone(pid: int, timeout: float = 5.0) -> bool:
    """True once `pid` has exited."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return False


# ---- fake child targets -----------------------------------------------------------------------------------------


def data_quality(records: int = 0) -> dict[str, Any]:
    """A clean `qt.data_quality` document."""
    zero = dict.fromkeys(
        (
            "maybe_bad_book_records",
            "bad_ts_recv_records",
            "snapshot_records",
            "pre_snapshot_rows_skipped",
            "unknown_order_refs",
            "duplicate_adds",
            "unexpected_modifies",
            "sequence_gaps",
            "sequence_regressions",
            "out_of_order_ts_event",
            "crossed_books_after_last",
            "hidden_prints",
            "off_price_executions",
        ),
        0,
    )
    return {
        "schema": "qt.data_quality",
        "schema_version": 1,
        "records": records,
        **zero,
        "feed_gaps": [],
        "source_specific": {},
        "verdict": "clean",
        "reasons": [],
    }


def replay_summary(spec: dict[str, Any], records: int, files: list[dict[str, Any]]) -> dict[str, Any]:
    """A valid `qt.replay_summary` for fakes."""
    return {
        "schema": "qt.replay_summary",
        "schema_version": 1,
        "run_id": "0123456789abcdef",
        "engine_version": "0.1.0",
        "spec": {k: v for k, v in spec.items() if k not in ("out", "threads")},
        "stats": {
            "records": records,
            "events": records,
            "trades": 0,
            "instruments": 1,
            "partitions": 1,
            "snapshot_records": 0,
        },
        "invariants": dict.fromkeys(
            (
                "cancels_exceeding_resting",
                "fills_exceeding_resting",
                "fills_unknown_order",
                "unknown_order_refs",
                "duplicate_adds",
                "unexpected_modifies",
                "crossed_books_after_last",
                "negative_spreads",
            ),
            0,
        ),
        "data_quality": data_quality(records),
        "files": files,
        "warnings": [],
    }


def fake_replay(spec: dict[str, Any]) -> dict[str, Any]:
    """Child target: lists the input dataset, writes two output files (one nested) and returns a summary."""
    import hashlib

    ds = Path(spec["dataset"]["path"])
    inputs = sorted(p.relative_to(ds).as_posix() for p in ds.rglob("*") if p.is_file())
    out = Path(spec["out"])
    (out / "nested").mkdir(parents=True, exist_ok=True)
    (out / "inputs.json").write_text(
        json.dumps({"inputs": inputs, "cwd_is_workdir": Path.cwd().name.startswith("qtjob-")})
    )
    (out / "nested" / "data.bin").write_bytes(bytes(range(256)) * 64)
    files = []
    for p in sorted(out.rglob("*")):
        if p.is_file():
            files.append(
                {
                    "name": p.relative_to(out).as_posix(),
                    "bytes": p.stat().st_size,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                }
            )
    return replay_summary(spec, records=len(inputs) * 1000, files=files)


def manifest_doc(fmt: str, venue: str, sha: str, rows: int) -> dict[str, Any]:
    """A valid `qt.dataset_manifest` with one partition file."""
    import hashlib

    return {
        "schema": "qt.dataset_manifest",
        "schema_version": 1,
        "dataset_id": hashlib.sha256((sha + "\n" + fmt).encode()).hexdigest(),
        "kind": "mbo",
        "source_format": fmt,
        "venue": venue,
        "source_venue": venue,
        "inputs": [],
        "partitions": [
            {
                "instrument_id": 1,
                "session_date": 20455,  # 2026-01-02 as days since 1970-01-01
                "files": [
                    {"path": "mbo/date=2026-01-02/instrument_id=1/part-00000.parquet", "rows": rows, "sha256": sha}
                ],
                "rows": rows,
                "ts_min": 0,
                "ts_max": 1,
            }
        ],
        "price_exp_by_instrument": {"1": -9},
        "created_by": "fake",
        "validation_status": "synthetic_only",
    }


def fake_ingest(spec: dict[str, Any]) -> dict[str, Any]:
    """Child target: copies the inputs into a dataset directory with a manifest."""
    out = Path(spec["out"])
    part = out / "mbo" / "date=2026-01-02" / "instrument_id=1"
    part.mkdir(parents=True, exist_ok=True)
    data = b"".join(Path(i["path"]).read_bytes() for i in spec["inputs"])
    (part / "part-00000.parquet").write_bytes(data)
    import hashlib

    sha = hashlib.sha256(data).hexdigest()
    manifest = manifest_doc(spec["format"], spec["venue"], sha, 3)
    dq = data_quality(3)
    (out / "manifest.json").write_text(json.dumps(manifest))
    (out / "data_quality.json").write_text(json.dumps(dq))
    return {
        "schema": "qt.ingest_summary",
        "schema_version": 1,
        "spec": {k: v for k, v in spec.items() if k != "out"},
        "manifest": manifest,
        "data_quality": dq,
        "files": [],
    }


def fake_sleep(spec: dict[str, Any]) -> dict[str, Any]:
    """Child target: sleeps `spec["seed"]` seconds (used for cancel and timeout tests)."""
    time.sleep(float(spec.get("seed", 30)))
    return replay_summary(spec, 0, [])


def fake_fail(spec: dict[str, Any]) -> dict[str, Any]:
    """Child target: raises a `QtError` with a data-quality code."""
    from queuetruth.errors import QtError

    raise QtError("QT-DATA-001", f"verdict unusable for {Path.cwd()}/inputs")


def fake_crash(spec: dict[str, Any]) -> dict[str, Any]:
    """Child target: raises an unexpected exception whose message must not leak."""
    raise RuntimeError("secret-internal-detail")


def fake_abort(spec: dict[str, Any]) -> dict[str, Any]:
    """Child target: dies without reporting (a crashed job process)."""
    os._exit(3)


def fake_connect(spec: dict[str, Any]) -> dict[str, Any]:
    """Child target: tries TCP connections to `spec["targets"]` and reports `ok` / the error type for each."""
    out = {}
    for host, port in spec["targets"]:
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                out[f"{host}:{port}"] = "ok"
        except OSError as e:
            out[f"{host}:{port}"] = type(e).__name__
        s = socket.socket()
        try:
            s.settimeout(2)
            s.connect((host, int(port)))
            out[f"raw {host}:{port}"] = "ok"
        except OSError as e:
            out[f"raw {host}:{port}"] = type(e).__name__
        finally:
            s.close()
    if spec.get("out"):
        Path(spec["out"]).mkdir(parents=True, exist_ok=True)
        (Path(spec["out"]) / "results.json").write_text(json.dumps(out, sort_keys=True))
    return {"results": out}


def fake_env(spec: dict[str, Any]) -> dict[str, Any]:
    """Child target: reports the environment a job process sees (and, on Linux, whether its initial environment block
    holds any of `spec["needles"]`)."""
    environ_block = b""
    try:
        environ_block = Path("/proc/self/environ").read_bytes()
    except OSError:
        pass
    needles = [str(n) for n in spec.get("needles", [])]
    leaked = [n for n in needles if n.encode() in environ_block]
    doc = {
        "env": dict(os.environ),
        "cwd": str(Path.cwd()),
        "cwd_mode": oct(Path.cwd().stat().st_mode & 0o777),
        "leaked_in_proc_environ": leaked,
    }
    if spec.get("out"):
        Path(spec["out"]).mkdir(parents=True, exist_ok=True)
        (Path(spec["out"]) / "env.json").write_text(json.dumps(doc, sort_keys=True))
    return doc


def fake_echo(spec: dict[str, Any]) -> dict[str, Any]:
    """Child target: writes `out/echo.json` with the resolved spec and the files under every `path` it references."""
    listing: dict[str, list[str]] = {}

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            if isinstance(v.get("path"), str):
                p = Path(v["path"])
                if p.is_dir():
                    listing[v["path"]] = sorted(q.relative_to(p).as_posix() for q in p.rglob("*") if q.is_file())
                elif p.is_file():
                    listing[v["path"]] = [p.name]
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(spec)
    out = Path(spec["out"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "echo.json").write_text(json.dumps({"spec": spec, "listing": listing}, sort_keys=True))
    return {"echo": True}


def fake_unpickle(spec: dict[str, Any]) -> dict[str, Any]:
    """Child target: unpickles `spec["payload_hex"]` (job processes must refuse to)."""
    import pickle

    pickle.loads(bytes.fromhex(spec["payload_hex"]))
    return {"unpickled": True}


def fake_marker(spec: dict[str, Any]) -> dict[str, Any]:
    """Child target: creates `spec["marker"]` (proves the child ran)."""
    Path(spec["marker"]).write_text("ran")
    return {"ran": True}
