"""Run mode of the MCP local tools without the engine: the path sandbox (allowed roots, symlinks, service refs), spec
checks against `RunLimits`, `RunLimits.from_env`, and the child process (`LocalRunner.spawn`): results and errors
across the process boundary, the minimal environment, unpickling refused, and the limits that kill it (timeout with
its whole process group, caller cancel, CPU seconds, file size, memory)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import anyio
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from queuetruth.errors import QtError
from queuetruth.mcp import local as qt_local
from queuetruth.mcp.local import (
    ENV_RUN_ROOT,
    LocalRunner,
    RunLimits,
    Sandbox,
    child_env,
    env_flag,
    proc_status_rss,
    run_mode_unsupported,
    sample_bound,
)

TARGETS = """
import os, subprocess, sys, time
from collections import OrderedDict


def ok(spec):
    return {"echo": spec, "env": sorted(os.environ), "cwd": os.getcwd()}


def fail(spec):
    from queuetruth.errors import QtError

    raise QtError("QT-INPUT-002", "bad input", "fix it")


def boom(spec):
    raise RuntimeError("kaboom")


def exit3(spec):
    os._exit(3)


def unpickle(spec):
    import pickle

    return {"x": str(pickle.loads(pickle.dumps(OrderedDict(a=1))))}


def sleeper(spec):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(spec["pidfile"], "w") as f:
        f.write(f"{os.getpid()} {p.pid}")
    time.sleep(120)


def spin(spec):
    while True:
        pass


def hog(spec):
    blocks = []
    while True:
        blocks.append(b"\\x01" * (32 << 20))
        time.sleep(0.02)


def big_file(spec):
    with open(spec["path"], "wb") as f:
        f.write(b"x" * spec["n"])
    return {}


def many_files(spec):
    for i in range(spec["files"]):
        with open(os.path.join(spec["dir"], f"part-{i}.bin"), "wb") as f:
            f.write(b"x" * spec["n"])
        time.sleep(0.05)
    return {}
"""


@pytest.fixture
def targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A module of child targets on sys.path (the child gets the parent's absolute sys.path entries)."""
    d = tmp_path / "targets"
    d.mkdir()
    (d / "qt_mcp_child_targets.py").write_text(TARGETS)
    monkeypatch.syspath_prepend(str(d))
    return "qt_mcp_child_targets"


def runner(tmp_path: Path, **limits) -> LocalRunner:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    return LocalRunner(Sandbox(root), RunLimits(**({"min_free_bytes": 0} | limits)))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid: int, seconds: float = 10) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


# ---- limits and flags ---------------------------------------------------------------------------------------------


def test_run_limits_from_env():
    lim = RunLimits.from_env({"QT_MCP_RUN_TIMEOUT_S": "12.5", "QT_MCP_RUN_MAX_THREADS": "2", "QT_MCP_RUN_CPU_S": " "})
    assert lim.timeout_s == 12.5 and lim.max_threads == 2 and lim.cpu_s == RunLimits().cpu_s
    assert RunLimits.from_env({"QT_MCP_RUN_MEMORY_BYTES": "0"}).memory_bytes == 0  # 0 = no memory limit
    for env in ({"QT_MCP_RUN_MAX_THREADS": "two"}, {"QT_MCP_RUN_MAX_THREADS": "0"}, {"QT_MCP_RUN_TIMEOUT_S": "-1"}):
        with pytest.raises(QtError) as e:
            RunLimits.from_env(env)
        assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError):
        RunLimits(timeout_s=float("nan"))
    with pytest.raises(QtError):
        RunLimits(max_shadows=True)  # type: ignore[arg-type]


def test_env_flag():
    assert env_flag("X", {"X": "1"}) and env_flag("X", {"X": " Yes "}) and env_flag("X", {"X": "on"})
    assert not env_flag("X", {"X": "0"}) and not env_flag("X", {}) and not env_flag("X", {"X": ""})
    assert env_flag("X", {}, default=True) and not env_flag("X", {"X": "off"}, default=True)
    with pytest.raises(QtError) as e:
        env_flag("X", {"X": "maybe"})
    assert e.value.code == "QT-CONFIG-001"


def test_child_env_is_minimal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("QT_API_KEY", "qt_secret")
    monkeypatch.setenv("QT_DATABASE_URL_APP", "postgres://x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3")
    env = child_env("/w", 3)
    assert not any(k.startswith(("QT_", "AWS_")) for k in env)
    assert env["HOME"] == env["TMPDIR"] == "/w" and env["RAYON_NUM_THREADS"] == env["OMP_NUM_THREADS"] == "3"


# ---- sandbox ------------------------------------------------------------------------------------------------------


def test_sandbox_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, data, other = tmp_path / "root", tmp_path / "data", tmp_path / "other"
    for d in (root, data, other):
        d.mkdir()
    (root / "a.parquet").write_bytes(b"x")
    (data / "b.parquet").write_bytes(b"x")
    (other / "c.parquet").write_bytes(b"x")
    sb = Sandbox(root, data_roots=[str(data)])
    assert sb.run_root == root.resolve() / "qt-mcp-runs" and sb.roots[-1] == sb.run_root
    assert sb.resolve("a.parquet", "x") == (root / "a.parquet").resolve()
    assert sb.resolve(str(data / "b.parquet"), "x") == (data / "b.parquet").resolve()
    for bad in (str(other / "c.parquet"), "../other/c.parquet", "/etc/hosts", "../data/../other/c.parquet"):
        with pytest.raises(QtError) as e:
            sb.resolve(bad, "spec.shadows")
        assert e.value.code == "QT-CONFIG-001" and "spec.shadows" in e.value.message, bad
    (root / "link.parquet").symlink_to(other / "c.parquet")
    with pytest.raises(QtError) as e:
        sb.resolve("link.parquet", "x")
    assert e.value.code == "QT-CONFIG-001"
    (root / "inner.parquet").symlink_to(data / "b.parquet")  # a link between allowed roots is fine
    assert sb.resolve("inner.parquet", "x") == (data / "b.parquet").resolve()
    with pytest.raises(QtError) as e:
        sb.resolve("missing.parquet", "x")
    assert e.value.code == "QT-INPUT-001"
    for empty in ("", "a\x00b"):
        with pytest.raises(QtError) as e:
            sb.resolve(empty, "x")
        assert e.value.code == "QT-USAGE-001"
    ds = root / "ds" / "mbo"
    ds.mkdir(parents=True)
    with pytest.raises(QtError) as e:  # not a dataset: refused before anything under it is walked
        sb.check_tree(root / "ds", "x")
    assert e.value.code == "QT-INPUT-001" and "no manifest.json" in e.value.message
    (root / "ds" / "manifest.json").write_text("{}")
    sb.check_tree(root / "ds", "x")
    (ds / "part.parquet").symlink_to(other / "c.parquet")
    with pytest.raises(QtError) as e:
        sb.check_tree(root / "ds", "spec.dataset")
    assert e.value.code == "QT-CONFIG-001"
    monkeypatch.setenv(ENV_RUN_ROOT, str(other))
    monkeypatch.setenv("QT_MCP_DATA_ROOTS", os.pathsep.join([str(data), ""]))
    env_sb = Sandbox(root)
    assert env_sb.run_root == other.resolve() and env_sb.roots == (root.resolve(), data.resolve(), other.resolve())
    with pytest.raises(QtError) as e:
        Sandbox(root, run_root="/")
    assert e.value.code == "QT-CONFIG-001"


# ---- spec checks --------------------------------------------------------------------------------------------------


def write_shadows(path: Path, n: int) -> Path:
    pq.write_table(pa.table({"shadow_id": [f"s{i}" for i in range(n)]}), path)
    return path


@pytest.fixture
def layout(tmp_path: Path) -> tuple[LocalRunner, Path]:
    r = runner(tmp_path, max_threads=2, max_shadows=10, max_particles=200, max_bootstrap=2000, max_models=3)
    root = r.sandbox.root
    ds = root / "ds"
    (ds / "mbo").mkdir(parents=True)
    parts = [(1, 20619), (2, 20619), (1, 20620)]  # instrument 1 on two days (20619 = 2026-06-15)
    (ds / "manifest.json").write_text(
        json.dumps({"partitions": [{"instrument_id": i, "session_date": d} for i, d in parts]})
    )
    (root / "bad").mkdir()
    (root / "bad" / "manifest.json").write_text(json.dumps({"partitions": [{"instrument_id": 1}]}))
    write_shadows(root / "five.parquet", 5)
    write_shadows(root / "many.parquet", 11)
    (root / "not.parquet").write_bytes(b"nope")
    return r, root


@pytest.mark.parametrize(
    ("kind", "spec", "code", "needle"),
    [
        ("zoo", {"dataset": {"path": "ds"}, "shadows": {"path": "five.parquet"}, "out": "x"}, "QT-CONFIG-001", "out"),
        (
            "zoo",
            {"dataset": {"dataset_id": "ds_1"}, "shadows": {"path": "five.parquet"}},
            "QT-CONFIG-001",
            "dataset_id",
        ),
        ("zoo", {"dataset": {"path": "ds"}, "shadows": {"upload_id": "up_1"}}, "QT-CONFIG-001", "upload_id"),
        (
            "replay",
            {"dataset": {"path": "ds"}, "shadows": {"job_file": {"job_id": "j", "name": "n"}}},
            "QT-CONFIG-001",
            "job_file",
        ),
        (
            "zoo",
            {"dataset": {"path": "ds"}, "shadows": {"path": "five.parquet"}, "threads": 3},
            "QT-CONFIG-001",
            "threads",
        ),
        (
            "zoo",
            {"dataset": {"path": "ds"}, "shadows": {"path": "five.parquet"}, "particles": 201},
            "QT-CONFIG-001",
            "particles",
        ),
        (
            "zoo",
            {"dataset": {"path": "ds"}, "shadows": {"path": "five.parquet"}, "bootstrap_b": 2001},
            "QT-CONFIG-001",
            "bootstrap_b",
        ),
        (
            "zoo",
            {"dataset": {"path": "ds"}, "shadows": {"path": "five.parquet"}, "models": list("abcd")},
            "QT-CONFIG-001",
            "models",
        ),
        (
            "zoo",
            {"dataset": {"path": "ds"}, "shadows": {"path": "five.parquet"}, "horizons_ns": list(range(33))},
            "QT-CONFIG-001",
            "horizons_ns",
        ),
        ("zoo", {"dataset": {"path": "ds"}, "shadows": {"path": "many.parquet"}}, "QT-CONFIG-001", "shadows"),
        ("replay", {"dataset": {"path": "ds"}, "shadows": {"path": "many.parquet"}}, "QT-CONFIG-001", "shadows"),
        ("zoo", {"dataset": {"path": "ds"}, "shadows": {"path": "not.parquet"}}, "QT-INPUT-002", "Parquet"),
        ("zoo", {"dataset": {"path": "ds"}, "shadows": {"path": "ds"}}, "QT-INPUT-001", "not a file"),
        ("zoo", {"dataset": {"path": "five.parquet"}, "shadows": {"path": "five.parquet"}}, "QT-INPUT-001", "dataset"),
        ("zoo", {"dataset": {"path": "ds"}}, "QT-USAGE-001", "shadows"),
        ("zoo", {"dataset": {"path": "ds"}, "shadows": {"path": "five.parquet"}, "mode": "x"}, "QT-USAGE-001", "mode"),
        ("shadows", {"dataset": {"path": "ds"}, "n": 11}, "QT-CONFIG-001", "spec.n"),
        ("shadows", {"dataset": {"path": "ds"}, "n": 4, "allocation": "per_partition"}, "QT-CONFIG-001", "spec.n"),
        (  # 5 (instrument 1, day 1) + 2 + 5 (instrument 1, day 2) = 12
            "shadows",
            {"dataset": {"path": "ds"}, "n": 2, "allocation": "per_partition", "partition_quotas": {"1": 5}},
            "QT-CONFIG-001",
            "spec.n",
        ),
        (  # an undated quota counts once per day of its instrument: 6 + 0 + 6 = 12
            "shadows",
            {"dataset": {"path": "ds"}, "n": 0, "allocation": "per_partition", "partition_quotas": {"1": 6}},
            "QT-CONFIG-001",
            "spec.n",
        ),
        (
            "shadows",
            {"dataset": {"path": "bad"}, "n": 1, "allocation": "per_partition"},
            "QT-INPUT-002",
            "manifest.json",
        ),
        ("zoo", {"dataset": {"path": "."}, "shadows": {"path": "five.parquet"}}, "QT-INPUT-001", "manifest.json"),
        ("nope", {}, "QT-USAGE-001", "unknown run kind"),
    ],
)
def test_prepare_refuses(layout, kind, spec, code, needle):
    r, _ = layout
    with pytest.raises(QtError) as e:
        r.prepare(kind, spec)
    assert e.value.code == code and needle in e.value.message, e.value


def test_prepare_resolves_paths_and_hints(layout):
    r, root = layout
    out = r.prepare("zoo", {"dataset": {"path": "ds"}, "shadows": {"path": str(root / "five.parquet")}, "threads": 2})
    assert out.dataset.path == str((root / "ds").resolve()) and out.shadows.path == str(
        (root / "five.parquet").resolve()
    )
    assert out.out is None
    ok = r.prepare("shadows", {"dataset": {"path": "ds"}, "n": 3, "allocation": "per_partition"})  # 3 x 3 <= 10
    assert ok.n == 3
    quotas = {"1": 5}  # 5 + 0 + 5 = 10: the undated key on both days of instrument 1
    assert r.prepare(
        "shadows", {"dataset": {"path": "ds"}, "n": 0, "allocation": "per_partition", "partition_quotas": quotas}
    )
    quotas = {"1_20260615": 9, "1": 1}  # the dated key wins on its day: 9 + 0 + 1 = 10
    assert r.prepare(
        "shadows", {"dataset": {"path": "ds"}, "n": 0, "allocation": "per_partition", "partition_quotas": quotas}
    )
    with pytest.raises(QtError) as e:
        r.prepare("zoo", {"dataset": {"path": "ds"}, "shadows": {"path": "five.parquet"}, "threads": 3})
    assert e.value.hint == "set QT_MCP_RUN_MAX_THREADS to raise it"
    assert r.prepare_why("ds", "many.parquet", "s1")["shadow_id"] == "s1"  # one shadow traced: no max_shadows bound
    spec = r.prepare_why("ds", "five.parquet", "s1")
    assert spec["out"] is None and spec["shadow_id"] == "s1" and spec["dataset"]["path"] == str((root / "ds").resolve())
    with pytest.raises(QtError) as e:
        r.prepare_why("five.parquet", "five.parquet", "s1")
    assert e.value.code == "QT-INPUT-001"


# ---- the child process --------------------------------------------------------------------------------------------


def test_spawn_result_env_and_errors(tmp_path: Path, targets: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("QT_API_KEY", "qt_secret_key")
    r = runner(tmp_path)

    async def go():
        res = await r.spawn(f"{targets}:ok", {"a": [1, 2]}, threads=2)
        assert res["echo"] == {"a": [1, 2]}
        assert not any(k.startswith("QT_") for k in res["env"]) and "RAYON_NUM_THREADS" in res["env"]
        assert not res["cwd"].startswith(str(r.sandbox.root))
        for target, code, needle, hint in (
            ("fail", "QT-INPUT-002", "bad input", "fix it"),
            ("boom", "QT-ENGINE-001", "RuntimeError: kaboom", None),
            ("exit3", "QT-ENGINE-001", "exited with code 3", None),
            ("unpickle", "QT-ENGINE-001", "unpickling is disabled", None),
            ("nosuch", "QT-ENGINE-001", "AttributeError", None),
        ):
            with pytest.raises(QtError) as e:
                await r.spawn(f"{targets}:{target}", {})
            assert e.value.code == code and needle in e.value.message, (target, e.value)
            assert e.value.hint == hint

    anyio.run(go)


def test_timeout_kills_the_process_group(tmp_path: Path, targets: str):
    r = runner(tmp_path, timeout_s=5)
    pidfile = tmp_path / "pids"

    async def go():
        with pytest.raises(QtError) as e:
            await r.spawn(f"{targets}:sleeper", {"pidfile": str(pidfile)})
        assert e.value.code == "QT-ENGINE-001" and "timed out after 5 s" in e.value.message

    t0 = time.monotonic()
    anyio.run(go)
    assert time.monotonic() - t0 < 30
    child, grandchild = map(int, pidfile.read_text().split())
    assert wait_dead(child) and wait_dead(grandchild)


def test_cancel_kills_the_process_group(tmp_path: Path, targets: str):
    r = runner(tmp_path)
    pidfile = tmp_path / "pids"

    async def go():
        with anyio.move_on_after(5) as scope:
            await r.spawn(f"{targets}:sleeper", {"pidfile": str(pidfile)})
        assert scope.cancelled_caught

    anyio.run(go)
    child, grandchild = map(int, pidfile.read_text().split())
    assert wait_dead(child) and wait_dead(grandchild)


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX resource limits")
def test_cpu_limit(tmp_path: Path, targets: str):
    r = runner(tmp_path, cpu_s=1, timeout_s=60)

    async def go():
        with pytest.raises(QtError) as e:
            await r.spawn(f"{targets}:spin", {})
        assert e.value.code == "QT-ENGINE-001" and "CPU limit (1 s)" in e.value.message

    anyio.run(go)


def test_output_file_size_limit(tmp_path: Path, targets: str):
    r = runner(tmp_path, max_output_bytes=1 << 20)
    big = tmp_path / "big.bin"

    async def go():
        with pytest.raises(QtError) as e:
            await r.spawn(f"{targets}:big_file", {"path": str(big), "n": 2 << 20})
        assert e.value.code == "QT-ENGINE-001" and "File too large" in e.value.message
        assert big.stat().st_size <= 1 << 20
        await r.spawn(f"{targets}:big_file", {"path": str(big), "n": 1000})  # under the limit

    anyio.run(go)


def test_memory_limit(tmp_path: Path, targets: str):
    r = runner(tmp_path, memory_bytes=768 << 20, timeout_s=60)

    async def go():
        with pytest.raises(QtError) as e:
            await r.spawn(f"{targets}:hog", {})
        assert e.value.code == "QT-ENGINE-001"
        assert "memory limit exceeded" in e.value.message  # watchdog: footprint (macOS) or VmRSS (Linux)

    anyio.run(go)


def test_total_output_is_bounded_while_the_run_writes(tmp_path: Path, targets: str):
    """Many files, each under the per-file limit: the watchdog stops the run once their total passes the limit."""
    r = runner(tmp_path, max_output_bytes=4 << 20, timeout_s=60)
    out = tmp_path / "staging"
    out.mkdir()

    async def go():
        with pytest.raises(QtError) as e:
            await r.spawn(f"{targets}:many_files", {"dir": str(out), "files": 40, "n": 1 << 20}, watch_dir=out)
        assert e.value.code == "QT-ENGINE-001" and "output limit exceeded" in e.value.message, e.value

    anyio.run(go)
    written = sum(p.stat().st_size for p in out.iterdir())
    assert 4 << 20 < written < 40 << 20  # stopped early, within a poll interval of writes past the limit


def test_free_disk_floor_is_watched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    r = runner(tmp_path, min_free_bytes=1 << 30, memory_bytes=0)
    staging = tmp_path / "w"
    staging.mkdir()
    usage = type("U", (), {"free": 5 << 30})
    monkeypatch.setattr(qt_local.shutil, "disk_usage", lambda p: usage)
    assert anyio.run(r._over_limit, os.getpid(), staging) is None
    usage.free = 1 << 20
    verdict = anyio.run(r._over_limit, os.getpid(), staging)
    assert verdict is not None and "free disk under the run root fell to" in verdict


def test_proc_status_rss():
    text = "Name:\tpython\nVmPeak:\t  900 kB\nVmRSS:\t   12345 kB\nThreads:\t4\n"
    assert proc_status_rss(text) == 12345 * 1024
    assert proc_status_rss("Name: x\n") is None and proc_status_rss("VmRSS: lots kB") is None


def test_run_mode_refuses_broad_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A stdio server started from `/` (as MCP hosts often do) without `QT_MCP_ROOT` would allow every file."""
    root = tmp_path / "root"
    root.mkdir()
    home = Path(os.path.realpath(Path.home()))
    cases = [
        {"root": "/"},
        {"root": str(home)},
        {"root": str(root), "data_roots": [str(home)]},
        {"root": str(root), "data_roots": [str(home.parent)]},
        {"root": str(root), "run_root": "/"},
    ]
    for kw in cases:
        with pytest.raises(QtError) as e:
            Sandbox(**kw)
        assert e.value.code == "QT-CONFIG-001" and "filesystem root, the home directory" in e.value.message, kw
        assert "QT_MCP_ROOT" in (e.value.hint or "")
    monkeypatch.delenv("QT_MCP_ROOT", raising=False)
    monkeypatch.chdir("/")
    with pytest.raises(QtError) as e:
        Sandbox()
    assert e.value.code == "QT-CONFIG-001"
    from queuetruth.mcp.server import build_server

    with pytest.raises(QtError) as e:  # refused at startup, before any tool call
        build_server(local=True, allow_run=True)
    assert e.value.code == "QT-CONFIG-001"
    monkeypatch.setenv("QT_MCP_ROOT", str(root))
    assert Sandbox().root == root.resolve()


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_run_root_is_a_config_error(tmp_path: Path):
    root, locked = tmp_path / "root", tmp_path / "locked"
    root.mkdir()
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(QtError) as e:
            LocalRunner(Sandbox(root, run_root=locked / "runs"), RunLimits(min_free_bytes=0))
        assert e.value.code == "QT-CONFIG-001" and "cannot create the run directory" in e.value.message
        assert e.value.hint == "set QT_MCP_RUN_ROOT to a writable directory"
    finally:
        locked.chmod(0o700)


def test_run_mode_needs_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert run_mode_unsupported() is None
    monkeypatch.delattr(os, "killpg")
    assert run_mode_unsupported() == "process groups (os.killpg)"
    root = tmp_path / "root"
    root.mkdir()
    from queuetruth.mcp.server import build_server

    with pytest.raises(QtError) as e:
        build_server(local=True, root=root, allow_run=True)
    assert e.value.code == "QT-CONFIG-001" and "os.killpg" in e.value.message
    assert [t.name for t in build_server(local=True, root=root).tools]  # read tools still work without run mode


def test_stale_staging_is_swept_at_startup(tmp_path: Path):
    r = runner(tmp_path, timeout_s=60)
    staging = r.sandbox.run_root / ".staging"
    old, fresh, trash = staging / "zoo-abc-1", staging / "zoo-def-2", staging / "trash-replay-x-3"
    for d in (old, fresh, trash):
        d.mkdir()
        (d / "f.bin").write_bytes(b"x")
    stale = time.time() - 60 - qt_local.STALE_STAGING_MARGIN_S - 10
    os.utime(old, (stale, stale))
    r2 = LocalRunner(r.sandbox, r.limits)  # a restarted server
    assert not old.exists() and not trash.exists() and fresh.exists()
    assert r2.sweep_staging() == []


@pytest.mark.requires_core
def test_sample_bound_equals_what_the_engine_samples(tmp_path: Path):
    """The per-partition bound is the engine's quota sum, not an upper bound: synth_small has instruments 1 and 2 on
    two days each, so an undated quota counts twice."""
    import queuetruth as qt

    fixture = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "synth_small"
    if not (fixture / "manifest.json").exists():
        pytest.skip("tests/fixtures/synth_small is missing (make fixtures)")
    cases = [
        {"n": 3},
        {"n": 3, "allocation": "per_partition"},
        {"n": 0, "allocation": "per_partition", "partition_quotas": {"1": 5}},
        {"n": 2, "allocation": "per_partition", "partition_quotas": {"1_20260615": 4, "2": 1}},
    ]
    expected = [3, 12, 10, 4 + 2 + 1 + 1]
    for i, (extra, want) in enumerate(zip(cases, expected, strict=True)):
        spec = {"dataset": {"path": str(fixture)}, "seed": 3, "out": str(tmp_path / f"s{i}.parquet")} | extra
        assert sample_bound(fixture, spec) == want, extra
        qt.sample_shadows(spec)
        assert pq.read_metadata(spec["out"]).num_rows == want, extra
    r = runner(tmp_path, max_shadows=10)
    r.sandbox.roots = (*r.sandbox.roots, fixture.resolve())
    quotas = {"1": 10}  # 20 shadows: refused although one quota is 10
    with pytest.raises(QtError) as e:
        r.prepare(
            "shadows",
            {"dataset": {"path": str(fixture)}, "n": 0, "allocation": "per_partition", "partition_quotas": quotas},
        )
    assert e.value.code == "QT-CONFIG-001" and "20" in e.value.message
