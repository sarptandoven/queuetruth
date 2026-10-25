#!/usr/bin/env python3
"""Local nightly cross-checks for queuetruth: engine cross-checks, a public-data reproduction and a benchmark, each in
its own virtual environment, with a static HTML results page and a JSON history diffed against the previous run.

Never uses GitHub Actions or any cloud runner: everything runs on this machine, from this checkout.

  bash scripts/nightly/nightly.sh run [--mode full|reduced] [--state DIR] [--only a,b | --skip a,b] ...
  bash scripts/nightly/nightly.sh render [--state DIR]             re-render the page from the history
  bash scripts/nightly/nightly.sh schedule launchd|cron [...] [-- RUN OPTIONS]
                                                                   print a launchd plist or a crontab line (installs
                                                                   nothing)
  bash scripts/nightly/nightly.sh list                             the checks and what each gates

(nightly.sh runs this file with CPython >= 3.11: Homebrew python3.13 or $QT_NIGHTLY_PYTHON.)

Checks (each in its own venv under <state>/venvs; the queuetruth side runs in a venv built from this checkout):
  hbt        hftbacktest (the version `make hbt-crosscheck` pins) vs the faithful hbt zoo models, order by order
  nt-pinned  NautilusTrader as pinned in scripts/nt_crosscheck/requirements.txt vs the faithful nt_* zoo models
  nt-rc      the newest NautilusTrader 2.x release on PyPI (release candidates included); advisory
  repro      scripts/repro/reproduce.py on the public Nasdaq ITCH subsets (pinned sha256)
  bench      benches/bench.py --suite quick, compared by benches/check.py with the last passing run (or
             --bench-baseline)

Exit codes (precedence 2 > 1 > 4 > 3 > 0): 0 ok; 1 a required check failed; 2 a required check could not run, or a
runner error (lock held, bad state, setup failed, interrupted); 3 everything passed but numbers or statuses changed
against the earlier runs of the same mode (per check); 4 only the advisory check failed or errored. run.json also
lists every condition that held in `flags`.

Outputs under --state (default <repo>/.scratch/nightly): history.json, index.html (latest run and history),
latest.json, runs/<nightly_id>/{run.json, index.html, setup/, checks/<name>/}, venvs/, wheels/, itch/.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.version_info < (3, 11):  # noqa: UP036  (the runner is started by any python3; macOS ships 3.9)
    sys.stderr.write(
        f"nightly.py needs CPython >= 3.11 (this is {sys.version.split()[0]}); run it with "
        "`bash scripts/nightly/nightly.sh` or python3.13\n"
    )
    raise SystemExit(2)

HERE = Path(__file__).resolve().parent
REPO_DEFAULT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from qtnightly import (  # noqa: E402
    EXIT_ERROR,
    EXIT_MEANING,
    EXIT_OK,
    SCHEMA_RUN,
    SCHEMA_VERSION,
)
from qtnightly import envs as E  # noqa: E402
from qtnightly import evaluate as V  # noqa: E402
from qtnightly import history as H  # noqa: E402
from qtnightly import page as P  # noqa: E402

CHECKS = ("hbt", "nt-pinned", "nt-rc", "repro", "bench")
TITLES = {
    "hbt": "hftbacktest vs faithful hbt zoo models",
    "nt-pinned": "NautilusTrader (pinned) vs faithful nt_* zoo models",
    "nt-rc": "NautilusTrader 2.x release candidate vs faithful nt_* zoo models",
    "repro": "Public Nasdaq ITCH reproduction subset",
    "bench": "Quick benchmark suite",
}
ADVISORY = {"nt-rc"}
GIB = 1 << 30
SOURCE_DIRS = ("crates", "python", "scripts", "benches", "Cargo.toml", "Cargo.lock", "pyproject.toml", "uv.lock")

# per mode: what each check runs and its timeout in seconds
MODES: dict[str, dict[str, Any]] = {
    "full": {
        "dataset_sessions": 1,
        "nt_shadows": 400,
        "nt_depth10": True,
        "nt_procs": 2,
        "hbt_shadows": 400,
        "repro_dates": ["12302019", "01302020"],
        "repro_steps": ["subset", "ingest", "replay", "headline", "calib", "hazard"],
        "bench_only": None,
        "timeouts": {"setup": 3600, "hbt": 1800, "nt-pinned": 3600, "nt-rc": 3600, "repro": 3600, "bench": 2400},
    },
    "reduced": {
        "dataset_sessions": 1,
        "nt_shadows": 60,
        "nt_depth10": False,
        "nt_procs": 1,
        "hbt_shadows": 60,
        "repro_dates": ["12302019"],
        "repro_steps": ["subset", "ingest", "replay"],
        "bench_only": r"^(core\.synth2m\.replay_book|bind\.fixture\.cli_replay)$",
        "timeouts": {"setup": 2400, "hbt": 900, "nt-pinned": 1200, "nt-rc": 1200, "repro": 1200, "bench": 900},
    },
}
DATASET_SEED = 7  # synthetic nasdaq fixture of the NautilusTrader cross-check (one session: run.py reads one tape)
NT_SEED = 42
STRIP_ENV = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONSTARTUP", "UV_PROJECT_ENVIRONMENT")


class CheckError(RuntimeError):
    """A check could not run: reported as status `error` with this message."""


class Interrupted(BaseException):  # like KeyboardInterrupt: not an Exception, so no handler eats it
    """SIGTERM, SIGHUP or SIGINT reached the runner (launchctl bootout, a killed wrapper, Ctrl-C)."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"interrupted by {signal.Signals(signum).name}")


STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def _raise_interrupted(signum: int, frame: Any) -> None:
    raise Interrupted(signum)


@contextlib.contextmanager
def stop_signals_raise():
    """While active, SIGTERM, SIGHUP and SIGINT raise `Interrupted` in the main thread, so `run_logged` kills the
    running child's process group and the run records a partial result instead of leaving an orphan behind."""
    old = {s: signal.signal(s, _raise_interrupted) for s in STOP_SIGNALS}
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def stop_group(p: subprocess.Popen, grace: float = 10.0) -> None:
    """SIGTERM the process group of `p`, then SIGKILL after `grace` seconds; stop signals are held off meanwhile."""
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, None)):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(p.pid, sig)
            try:
                p.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of every child: this one without interpreter/venv overrides (a PYTHONPATH pointing at a source
    tree would shadow the installed wheel), user site disabled."""
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env["PYTHONNOUSERSITE"] = "1"
    env.update(extra or {})
    return env


def run_logged(
    argv: list[str], log: Path, cwd: Path | None = None, timeout: float | None = None, env: dict[str, str] | None = None
) -> int:
    """Run `argv`, appending its stdout and stderr to `log`; the exit code, or 124 after `timeout` seconds. The child
    runs in its own process group, which is killed on a timeout and on any exception while waiting (`Interrupted`
    included, which is then re-raised), so no child outlives the runner."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(f"\n$ {' '.join(str(a) for a in argv)}\n")
        f.flush()
        t0 = time.monotonic()
        try:
            p = subprocess.Popen(
                [str(a) for a in argv],
                cwd=cwd,
                stdout=f,
                stderr=subprocess.STDOUT,
                env=env or child_env(),
                start_new_session=True,
            )
        except OSError as e:
            f.write(f"[nightly] cannot start: {e}\n")
            return 127
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_group(p, grace=30)
            f.write(f"[nightly] timeout after {timeout} s\n")
            return 124
        except BaseException as e:
            stop_group(p)
            f.write(f"[nightly] {e or type(e).__name__}: process group stopped\n")
            raise
        f.write(f"[nightly] exit {rc} after {time.monotonic() - t0:.1f} s\n")
        return rc


def capture(argv: list[str], cwd: Path | None = None) -> str:
    r = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, text=True, env=child_env(), timeout=120)
    if r.returncode != 0:
        raise CheckError(f"{' '.join(map(str, argv))}: exit {r.returncode}: {r.stderr.strip()[-500:]}")
    return r.stdout


def git_info(repo: Path) -> dict[str, Any]:
    """HEAD, branch, whether tracked files have uncommitted changes, and how many untracked files sit in the source
    directories that go into the wheel and the harnesses (read-only git commands)."""

    def g(*a: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    status = g("status", "--porcelain", "--untracked-files=normal", "--", *SOURCE_DIRS)
    lines = [] if status is None else [x for x in status.splitlines() if x]
    return {
        "head": g("rev-parse", "HEAD"),
        "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": None if status is None else any(not x.startswith("??") for x in lines),
        "untracked_sources": None if status is None else sum(1 for x in lines if x.startswith("??")),
    }


def free_gib(p: Path) -> float:
    return shutil.disk_usage(p).free / GIB


def pinned_nt_version(repo: Path) -> tuple[str, list[str]]:
    """(nautilus_trader version, the other requirement lines) of scripts/nt_crosscheck/requirements.txt."""
    req = repo / "scripts" / "nt_crosscheck" / "requirements.txt"
    version, rest = None, []
    for line in req.read_text().splitlines():
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        m = re.fullmatch(r"nautilus[_-]trader==(\S+)", s)
        if m:
            version = m.group(1)
        else:
            rest.append(s)
    if version is None:
        raise CheckError(f"{req}: no nautilus_trader== pin")
    return version, rest


def pinned_hbt_version(repo: Path) -> str:
    """The hftbacktest version `make hbt-crosscheck` installs (one pin, in the Makefile)."""
    m = re.search(r"hftbacktest==([0-9][0-9A-Za-z.]*)", (repo / "Makefile").read_text())
    if not m:
        raise CheckError("Makefile: no hftbacktest== pin in hbt-crosscheck")
    return m.group(1)


@dataclass
class Ctx:
    a: argparse.Namespace
    repo: Path
    state: Path
    run_dir: Path
    mode: dict[str, Any]
    nightly_id: str
    today: dt.date
    started: dt.datetime | None = None
    qt_py: Path | None = None
    dataset: Path | None = None
    log_path: Path | None = None

    def log(self, msg: str) -> None:
        line = f"[{iso(utcnow())}] {msg}"
        print(line, file=sys.stderr, flush=True)
        if self.log_path:
            with self.log_path.open("a") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------------------------------------------------------
# setup: the queuetruth venv (built from this checkout) and the synthetic dataset
# ---------------------------------------------------------------------------------------------------------------


def setup_qt(ctx: Ctx) -> dict[str, Any]:
    """Build (or reuse) the queuetruth venv: locked dependencies from uv.lock, then a release wheel of this checkout
    built with the venv's maturin and installed over the previous one. With --qt-python, use that interpreter."""
    sdir = ctx.run_dir / "setup"
    log = sdir / "qt-env.log"
    to = ctx.mode["timeouts"]["setup"]
    info: dict[str, Any] = {}
    if ctx.a.qt_python:
        py = Path(ctx.a.qt_python).expanduser()
        info["source"] = f"--qt-python {py}"
    else:
        uv = ctx.a.uv
        venv = ctx.state / "venvs" / "qt"
        req = ctx.state / "venvs" / "qt-requirements.txt"
        req.parent.mkdir(parents=True, exist_ok=True)
        rc = run_logged(
            [
                uv,
                "export",
                "--frozen",
                "--no-hashes",
                "--no-header",
                "--no-emit-workspace",
                "--all-extras",
                "--group",
                "dev",
                "-o",
                str(req),
            ],
            log,
            cwd=ctx.repo,
            timeout=600,
        )
        if rc != 0:
            raise CheckError(f"uv export failed (exit {rc}); see {log}")
        spec = E.EnvSpec(
            "qt",
            ctx.a.python,
            [["-r", str(req)]],
            {"uv.lock": E.sha256_file(ctx.repo / "uv.lock"), "requirements": E.sha256_file(req)},
        )
        py, rebuilt = E.ensure_env(
            venv, spec, uv, lambda argv: run_logged(argv, log, cwd=ctx.state, timeout=to), ctx.a.refresh_envs, ctx.log
        )
        info["venv_rebuilt"] = rebuilt
        wdir = sdir / "wheel"
        rc = run_logged(
            [venv / "bin" / "maturin", "build", "--release", "--locked", "-o", wdir], log, cwd=ctx.repo, timeout=to
        )
        wheels = sorted(wdir.glob("queuetruth-*.whl"))
        if rc != 0 or len(wheels) != 1:
            raise CheckError(f"maturin build failed (exit {rc}, wheels {[w.name for w in wheels]}); see {log}")
        rc = run_logged(
            [uv, "pip", "install", "--no-config", "--python", py, "--no-deps", "--reinstall", wheels[0]],
            log,
            cwd=ctx.state,
            timeout=600,
        )
        if rc != 0:
            raise CheckError(f"installing {wheels[0].name} failed; see {log}")
        info.update(wheel=wheels[0].name, wheel_sha256=E.sha256_file(wheels[0]))
        info["freeze_sha256"] = record_freeze(ctx, py, sdir / "qt-freeze.txt")
        if not ctx.a.keep_work:
            shutil.rmtree(wdir, ignore_errors=True)
    probe = (
        "import json, queuetruth, queuetruth._core as c, hashlib, pathlib; print(json.dumps({'version': "
        "queuetruth.__version__, 'file': queuetruth.__file__, 'core_sha256': "
        "hashlib.sha256(pathlib.Path(c.__file__).read_bytes()).hexdigest()}))"
    )
    try:
        got = json.loads(capture([py, "-I", "-c", probe], cwd=ctx.state))
    except (CheckError, ValueError) as e:
        raise CheckError(f"queuetruth is not importable from {py}: {e}") from e
    if not ctx.a.qt_python and not str(got["file"]).startswith(str(ctx.state)):
        raise CheckError(f"{py} imports queuetruth from {got['file']}, not from its venv")
    ctx.qt_py = py
    info.update(python=str(py), queuetruth=got["version"], core_sha256=got["core_sha256"])
    return info


def setup_dataset(ctx: Ctx) -> dict[str, Any]:
    """The synthetic one-session nasdaq dataset both engine cross-checks replay (deterministic: seed 7)."""
    out = ctx.run_dir / "data" / "synth_nasdaq"
    log = ctx.run_dir / "setup" / "dataset.log"
    rc = run_logged(
        [
            ctx.qt_py,
            "-I",
            "-m",
            "queuetruth.cli",
            "--seed",
            str(DATASET_SEED),
            "fixtures",
            "synth",
            "--out",
            out,
            "--venue",
            "nasdaq",
            "--sessions",
            str(ctx.mode["dataset_sessions"]),
            "--format",
            "json",
        ],
        log,
        cwd=ctx.state,
        timeout=600,
    )
    if rc != 0 or not (out / "manifest.json").exists():
        raise CheckError(f"fixtures synth failed (exit {rc}); see {log}")
    man = json.loads((out / "manifest.json").read_text())
    ctx.dataset = out
    return {"path": str(out), "dataset_id": man.get("dataset_id"), "partitions": len(man.get("partitions", []))}


# ---------------------------------------------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------------------------------------------


def _env_runner(ctx: Ctx, log: Path, timeout: float):
    return lambda argv: run_logged(argv, log, cwd=ctx.state, timeout=timeout)


def record_freeze(ctx: Ctx, py: Path, dest: Path) -> str:
    """`uv pip freeze` of the venv of `py` written to `dest`; its sha256 (so a transitive dependency that moved under
    the cutoff can be traced from run.json)."""
    out = capture([ctx.a.uv, "pip", "freeze", "--no-config", "--python", py], cwd=ctx.state)
    dest.write_text(out)
    return E.sha256_file(dest)


def prune_nt_venvs(ctx: Ctx, current: str) -> list[str]:
    """Delete `venvs/nt-2.*` other than `current` and the newest older release (about 330 MB each)."""
    have = []
    for p in (ctx.state / "venvs").glob("nt-2.*"):
        k = E.version_key(p.name.removeprefix("nt-"))
        if k is not None and p.is_dir():
            have.append((k, p))
    cur_k = E.version_key(current)
    older = sorted((x for x in have if cur_k is None or x[0] < cur_k), reverse=True)
    keep = {p.name for _, p in older[:1]} | {f"nt-{current}"}
    removed = []
    for _, p in have:
        if p.name not in keep:
            shutil.rmtree(p, ignore_errors=True)
            removed.append(p.name)
    return removed


def nt_env(ctx: Ctx, version: str, pinned_rest: list[str] | None, log: Path) -> Path:
    """The venv for nautilus_trader `version`: the pinned requirement file's other pins for the pinned version, else
    pyarrow, numpy and pandas (14-day cutoff, lifted for nautilus_trader only, as requirements.txt documents). No
    `--prerelease=allow`: the exact `nautilus_trader==<rc>` pin is an explicit pre-release, which uv's default allows,
    while the other packages stay on final releases."""
    venv = ctx.state / "venvs" / f"nt-{version}"
    if ctx.a.offline:
        if (venv / E.MARKER).exists() and E.venv_python(venv).exists():
            ctx.log(f"offline: reusing {venv}")
            return E.venv_python(venv)
        raise CheckError(f"--offline and no venv for nautilus_trader {version} in {venv.parent}")
    tag = host_python_tag(ctx)
    files = E.pypi_release("nautilus-trader", version)["urls"]
    target = f"nautilus_trader=={version}"
    fp: dict[str, Any] = {"nautilus_trader": version, "python_tag": tag}
    if platform.system() == "Darwin":
        entry = E.wheel_for_host(files, tag, "Darwin", platform.machine(), E.mac_major())
        if entry is None:
            raise CheckError(f"nautilus_trader {version} has no {tag} macOS {platform.machine()} wheel on PyPI")
        fp["wheel_sha256"] = entry["digests"]["sha256"]
        if entry["needs_retag"]:
            whl = E.find_or_download_wheel(
                entry, ctx.state / "wheels", [Path(d) for d in ctx.a.wheel_dirs], ctx.a.offline, ctx.log
            )
            target = str(whl)
            fp["retagged"] = whl.name
    deps = pinned_rest if pinned_rest is not None else ["pyarrow", "numpy", "pandas"]
    unhashed: list[str] = []
    if pinned_rest is None:
        unhashed = [
            "--exclude-newer",
            E.exclude_newer(ctx.today),
            "--exclude-newer-package",
            f"nautilus-trader={iso(utcnow())}",
        ]
    spec = E.EnvSpec(f"nt-{version}", ctx.a.python, [[target, *deps]], fp, unhashed)
    py, _ = E.ensure_env(
        venv, spec, ctx.a.uv, _env_runner(ctx, log, ctx.mode["timeouts"]["setup"]), ctx.a.refresh_envs, ctx.log
    )
    return py


def host_python_tag(ctx: Ctx) -> str:
    return E.python_tag(ctx.a.python, lambda argv: capture(argv))


def latest_nt2(ctx: Ctx) -> str:
    """--nt-rc, else the newest 2.x release on PyPI (pre-releases included) that has a wheel for this interpreter and
    platform, else (offline) the newest local venv."""
    if ctx.a.nt_rc != "auto":
        return ctx.a.nt_rc
    if ctx.a.offline:
        have = [p.name.removeprefix("nt-") for p in (ctx.state / "venvs").glob("nt-2.*") if (p / E.MARKER).exists()]
        have = [v for v in have if E.version_key(v)]
        if not have:
            raise CheckError("--offline and no nautilus_trader 2.x venv to reuse")
        return max(have, key=lambda v: E.version_key(v) or ())
    tag, system, machine, mac = host_python_tag(ctx), platform.system(), platform.machine(), E.mac_major()
    v = E.newest_installable(
        E.pypi_project("nautilus-trader")["releases"],
        2,
        lambda files: E.wheel_for_host(files, tag, system, machine, mac) is not None,
    )
    if v is None:
        raise CheckError(f"PyPI lists no nautilus_trader 2.x release with a {tag} {system} {machine} wheel")
    return v


def check_nt(ctx: Ctx, cdir: Path, which: str) -> dict[str, Any]:
    log = cdir / "log.txt"
    pinned, rest = pinned_nt_version(ctx.repo)
    version, pinned_rest = (pinned, rest) if which == "nt-pinned" else (latest_nt2(ctx), None)
    nt_py = nt_env(ctx, version, pinned_rest, log)
    freeze_sha = record_freeze(ctx, nt_py, cdir / "freeze.txt")
    if which == "nt-rc" and not ctx.a.offline:
        removed = prune_nt_venvs(ctx, version)
        if removed:
            ctx.log(f"pruned NautilusTrader venvs {removed}")
    work = cdir / "work"
    argv = [
        ctx.qt_py,
        "-I",
        ctx.repo / "scripts" / "nt_crosscheck" / "run.py",
        "all",
        "--work",
        work,
        "--dataset",
        ctx.dataset,
        "--n-shadows",
        str(ctx.mode["nt_shadows"]),
        "--threads",
        str(ctx.a.threads),
        "--nt-python",
        nt_py,
        "--procs",
        str(ctx.mode["nt_procs"]),
        "--seed",
        str(NT_SEED),
    ]
    if ctx.mode["nt_depth10"]:
        argv.append("--depth10")
    rc = run_logged(argv, log, cwd=ctx.repo, timeout=ctx.mode["timeouts"][which])
    rep = work / "report.json"
    if rc != 0 or not rep.exists():
        raise CheckError(f"run.py all exit {rc}" + (" (timeout)" if rc == 124 else "") + f"; see {log}")
    report = json.loads(rep.read_text())
    for name in ("report.json", "report.md"):
        shutil.copy2(work / name, cdir / name)
    return {
        **V.evaluate_nt(report, version)._asdict(),
        "versions": {"nautilus_trader": version, "freeze_sha256": freeze_sha},
        "artifacts": {"report.md": "report.md", "report.json": "report.json", "freeze.txt": "freeze.txt"},
    }


def check_hbt(ctx: Ctx, cdir: Path) -> dict[str, Any]:
    log = cdir / "log.txt"
    to = ctx.mode["timeouts"]["hbt"]
    version = pinned_hbt_version(ctx.repo)
    venv = ctx.state / "venvs" / f"hbt-{version}"
    if ctx.a.offline:
        if not (venv / E.MARKER).exists():
            raise CheckError(f"--offline and no hftbacktest venv in {venv}")
        hbt_py = E.venv_python(venv)
    else:
        spec = E.EnvSpec(
            f"hbt-{version}",
            ctx.a.python,
            [["numba", "numpy"], ["--no-deps", f"hftbacktest=={version}"]],
            {"hftbacktest": version},
            ["--exclude-newer", E.exclude_newer(ctx.today)],
        )
        hbt_py, _ = E.ensure_env(
            venv, spec, ctx.a.uv, _env_runner(ctx, log, ctx.mode["timeouts"]["setup"]), ctx.a.refresh_envs, ctx.log
        )
    freeze_sha = record_freeze(ctx, hbt_py, cdir / "freeze.txt")
    work = cdir / "work"
    me = HERE / "hbt_xcheck.py"
    rc = run_logged(
        [
            ctx.qt_py,
            "-I",
            me,
            "prepare",
            "--dataset",
            ctx.dataset,
            "--work",
            work,
            "--n-shadows",
            str(ctx.mode["hbt_shadows"]),
            "--threads",
            str(ctx.a.threads),
        ],
        log,
        cwd=ctx.repo,
        timeout=to,
    )
    if rc != 0:
        raise CheckError(f"hbt_xcheck.py prepare exit {rc}; see {log}")
    models = ",".join(json.loads((work / "prepare.json").read_text())["models"])
    rc = run_logged(
        [
            hbt_py,
            "-I",
            ctx.repo / "python" / "tests" / "zoo" / "hbt_crosscheck.py",
            work / "npz",
            "--out",
            work / "hbt_runs.json",
            "--models",
            models,
            "--exch",
            "no_partial",
            "--coarse",
        ],
        log,
        cwd=ctx.repo,
        timeout=to,
    )
    if rc != 0:
        raise CheckError(f"hbt_crosscheck.py exit {rc}" + (" (timeout)" if rc == 124 else "") + f"; see {log}")
    rc = run_logged([ctx.qt_py, "-I", me, "compare", "--work", work], log, cwd=ctx.repo, timeout=600)
    if rc not in (0, 1) or not (work / "hbt_report.json").exists():
        raise CheckError(f"hbt_xcheck.py compare exit {rc}; see {log}")
    shutil.copy2(work / "hbt_report.json", cdir / "hbt_report.json")
    return {
        **V.evaluate_hbt(json.loads((cdir / "hbt_report.json").read_text()))._asdict(),
        "versions": {"hftbacktest": version, "freeze_sha256": freeze_sha},
        "artifacts": {"hbt_report.json": "hbt_report.json", "freeze.txt": "freeze.txt"},
    }


def find_subset(dirs: list[Path], date: str, tag: str) -> Path | None:
    for d in dirs:
        p = d / f"{date}.subset.{tag}.itch50.gz"
        if p.is_file() and (d / f"{date}.subset.{tag}.manifest.json").is_file():
            return p
    return None


def pinned_subsets(repo: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """`PINNED_SUBSETS` of scripts/repro/reproduce.py, read as a literal (importing the script needs the queuetruth
    venv; this runner is standard library only)."""
    src = repo / "scripts" / "repro" / "reproduce.py"
    for node in ast.parse(src.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "PINNED_SUBSETS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise CheckError(f"{src}: no PINNED_SUBSETS literal")


def framed_sha256(p: Path) -> str:
    """sha256 of the bytes inside a gzip file (reproduce.py's `framed_sha256`)."""
    h = hashlib.sha256()
    with gzip.open(p, "rb") as g:
        for b in iter(lambda: g.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def verified_subset(dirs: list[Path], date: str, tag: str, pin: dict[str, Any] | None, log) -> Path | None:
    """The first subset of `date` in `dirs` whose framed sha256 equals the pin (any subset with a manifest when the
    date has no pin). A subset with the wrong content is skipped and logged: reproduce.py would re-stream it from
    emi.nasdaq.com without asking, which only --repro-download may allow."""
    for d in dirs:
        p = find_subset([d], date, tag)
        if p is None:
            continue
        if pin is None:
            return p
        got = framed_sha256(p)
        if got == pin["framed_sha256"]:
            return p
        log(f"ignoring {p}: framed sha256 {got} != pinned {pin['framed_sha256']}")
    return None


def repro_out_dir(ctx: Ctx, cdir: Path) -> Path:
    """Where reproduce.py writes: the check directory when it is inside the checkout, else a directory under
    `<repo>/.scratch` (reproduce.py records some artifacts relative to the checkout, which fails outside it)."""
    if cdir.resolve().is_relative_to(ctx.repo.resolve()):
        return cdir / "out"
    return ctx.repo / ".scratch" / "nightly-repro" / ctx.nightly_id


def check_repro(ctx: Ctx, cdir: Path) -> dict[str, Any]:
    log = cdir / "log.txt"
    dirs = [Path(d).expanduser().resolve() for d in ctx.a.itch_dirs]
    tag = "AAPL-MSFT-SPY-TSLA"
    dates, steps = ctx.mode["repro_dates"], ctx.mode["repro_steps"]
    pins = pinned_subsets(ctx.repo)
    found = {d: verified_subset(dirs, d, tag, pins.get((d, tag)), ctx.log) for d in dates}
    missing = [d for d, p in found.items() if p is None]
    if missing and ctx.a.offline:
        raise CheckError(
            f"--offline and public ITCH subsets {missing} ({tag}) matching the pinned sha256 are not on disk"
        )
    if missing and not ctx.a.repro_download:
        raise CheckError(
            f"public ITCH subsets {missing} ({tag}) matching the pinned sha256 not found in {[str(d) for d in dirs]}; "
            "pass --repro-download to stream them once from emi.nasdaq.com (free public data)"
        )
    # reproduce.py sees only the verified subsets (one directory of links), so it never re-streams one silently
    links = cdir / "itch"
    links.mkdir(parents=True, exist_ok=True)
    for p in found.values():
        if p is not None:
            for name in (p.name, p.name.replace(".itch50.gz", ".manifest.json")):
                (links / name).unlink(missing_ok=True)
                (links / name).symlink_to(p.parent / name)
    out = repro_out_dir(ctx, cdir)
    argv = [
        ctx.qt_py,
        "-I",
        ctx.repo / "scripts" / "repro" / "reproduce.py",
        "--out",
        out,
        "--dates",
        ",".join(dates),
        "--steps",
        ",".join(steps),
        "--itch-dirs",
        str(links),
        "--threads",
        str(ctx.a.threads),
        "--min-free-gib",
        str(ctx.a.min_free_gib),
    ]
    try:
        rc = run_logged(argv, log, cwd=ctx.repo, timeout=ctx.mode["timeouts"]["repro"])
        res = out / "results.json"
        if not res.exists():
            raise CheckError(
                f"reproduce.py exit {rc}" + (" (timeout)" if rc == 124 else "") + f", no results; see {log}"
            )
        streamed = out / "itch"
        if streamed.is_dir():  # keep streamed subsets for the next nights (verified against the pin there)
            keep = ctx.state / "itch"
            keep.mkdir(parents=True, exist_ok=True)
            for p in streamed.iterdir():
                if p.is_file():
                    shutil.move(str(p), keep / p.name)
            shutil.rmtree(streamed, ignore_errors=True)
        doc = json.loads(res.read_text())
        shutil.copy2(res, cdir / "results.json")
    finally:
        shutil.rmtree(links, ignore_errors=True)
        if out != cdir / "out" and out.exists():
            if not ctx.a.keep_work:
                shutil.rmtree(out / "work", ignore_errors=True)
            shutil.move(str(out), cdir / "out")
            with contextlib.suppress(OSError):
                out.parent.rmdir()
    expected = [f"{s}/{d}" for d in dates for s in steps if s in ("subset", "ingest", "replay", "headline", "oracle")]
    expected += [s for s in steps if s in ("calib", "hazard")]
    # reproduce.py writes paths under the checkout relative to it: scrub both spellings of the per-run directories
    roots: dict[str, str] = {}
    for d, tag in ((out, "<out>"), (links, "<itch>"), (ctx.state, "<state>")):
        roots[str(d)] = tag
        if d.is_relative_to(ctx.repo):
            roots[str(d.relative_to(ctx.repo))] = tag
    roots[str(ctx.repo)] = "<repo>"
    return {
        **V.evaluate_repro(doc, rc, expected, roots)._asdict(),
        "versions": {"dates": ",".join(dates)},
        "artifacts": {"results.json": "results.json"},
    }


def bench_shim(ctx: Ctx) -> Path:
    """A directory that looks like the checkout to benches/bench.py (which runs `<repo>/.venv/bin/python` and
    `<repo>/.venv/bin/queuetruth`) but whose `.venv` is the nightly queuetruth venv and whose scratch is ours."""
    shim = ctx.state / "bench-shim"
    if shim.exists():
        shutil.rmtree(shim)
    shim.mkdir(parents=True)
    for p in ctx.repo.iterdir():
        if p.name in (".venv", ".scratch", ".realdata", "target", ".git", ".wsdist"):
            continue
        (shim / p.name).symlink_to(p)
    (shim / ".venv").symlink_to(ctx.qt_py.parent.parent)
    return shim


BENCH_BASELINE_VERDICTS = ("no previous run", "no regression")


def bench_baseline_run(state: Path, hist: dict[str, Any], mode: str) -> tuple[str, Path] | None:
    """(nightly_id, bench.json) of the newest run of `mode` whose bench check passed with a comparable verdict ("no
    previous run" or "no regression") and whose bench.json is still on disk. A night that regressed (or ran in an
    environment check.py found not comparable) never becomes the baseline, so a slowdown keeps failing until it is
    fixed or a baseline is pinned with --bench-baseline."""
    for r in reversed(hist["runs"]):
        if r.get("mode") != mode:
            continue
        c = r.get("checks", {}).get("bench")
        if (
            not c
            or c.get("status") != "pass"
            or not any(v.endswith("/bench.json") for v in c.get("artifacts", {}).values())
        ):
            continue
        if c.get("metrics", {}).get("bench.vs_previous") not in BENCH_BASELINE_VERDICTS:
            continue
        p = state / "runs" / r["nightly_id"] / "checks" / "bench" / "bench.json"
        if p.exists():
            return r["nightly_id"], p
    return None


def bench_load(results: dict[str, Any]) -> float | None:
    """The higher 1-minute load average of bench.py's start and end (None when not recorded)."""
    env = results.get("env", {})
    vals = [v[0] for v in (env.get("load_avg_start"), env.get("load_avg_end")) if isinstance(v, list) and v]
    return max(vals) if vals else None


def check_bench(ctx: Ctx, cdir: Path, hist: dict[str, Any]) -> dict[str, Any]:
    log = cdir / "log.txt"
    to = ctx.mode["timeouts"]["bench"]
    cargo = ctx.a.cargo
    rc = run_logged(
        [cargo, "build", "--locked", "--release", "-p", "queuetruth-cli", "--bin", "queuetruth-core"],
        log,
        cwd=ctx.repo,
        timeout=to,
    )
    if rc != 0:
        raise CheckError(f"cargo build queuetruth-core exit {rc}; see {log}")
    tdir = Path(os.environ.get("CARGO_TARGET_DIR", ctx.repo / "target"))
    core = tdir / "release" / "queuetruth-core"
    shim = bench_shim(ctx)
    out = cdir / "bench.json"
    argv = [
        ctx.qt_py,
        "-I",
        shim / "benches" / "bench.py",
        "--suite",
        "quick",
        "--repo",
        shim,
        "--core",
        core,
        "--out",
        out,
    ]
    if ctx.mode["bench_only"]:
        argv += ["--only", ctx.mode["bench_only"]]
    if ctx.a.bench_max_load > 0:  # bench.py waits up to 30 s per round for the 1-minute load to fall below this
        argv += ["--max-load", str(ctx.a.bench_max_load)]
    try:
        bench_rc = run_logged(argv, log, cwd=shim, timeout=to)
    finally:
        shutil.rmtree(shim, ignore_errors=True)
    if bench_rc == 2 or not out.exists():
        raise CheckError(f"bench.py exit {bench_rc}, no results; see {log}")
    results = json.loads(out.read_text())
    load = bench_load(results)
    if ctx.a.bench_max_load > 0 and load is not None and load > ctx.a.bench_max_load:
        # a shared, saturated machine measures the neighbours, not queuetruth: the check could not run, and the night
        # never becomes a bench baseline (bench.json is kept for the record)
        raise CheckError(
            f"1-minute load {load:.1f} > --bench-max-load {ctx.a.bench_max_load:g} during the benchmark; "
            f"medians not judged (see {out})"
        )
    if ctx.a.bench_baseline:
        prev: Path | None = Path(ctx.a.bench_baseline).expanduser().resolve()
        if not prev.is_file():
            raise CheckError(f"--bench-baseline {prev} does not exist")
        label = f"--bench-baseline {prev}"
    else:
        found = bench_baseline_run(ctx.state, hist, ctx.a.mode)
        prev, label = (None, None) if found is None else (found[1], f"run {found[0]}")
    check_doc, check_rc = None, None
    arts = {"bench.json": "bench.json"}
    if prev is not None:
        cj = cdir / "bench-check.json"
        check_rc = run_logged(
            [ctx.qt_py, "-I", ctx.repo / "benches" / "check.py", "--baseline", prev, "--current", out, "--json", cj],
            log,
            cwd=ctx.repo,
            timeout=600,
        )
        if cj.exists():
            check_doc = json.loads(cj.read_text())
            arts["bench-check.json"] = "bench-check.json"
    return {
        **V.evaluate_bench(results, bench_rc, check_doc, check_rc, label)._asdict(),
        "artifacts": arts,
        "versions": {"suite": "quick" + (" (subset)" if ctx.mode["bench_only"] else "")},
    }


# ---------------------------------------------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------------------------------------------


def selected_checks(a: argparse.Namespace) -> list[str]:
    only = [c for c in (a.only or "").split(",") if c]
    skip = [c for c in (a.skip or "").split(",") if c]
    bad = sorted(set(only + skip) - set(CHECKS))
    if bad:
        raise ValueError(f"unknown checks {bad}; known: {', '.join(CHECKS)}")
    sel = [c for c in CHECKS if (not only or c in only) and c not in skip]
    if not sel:
        raise ValueError("--only/--skip leave no check to run")
    return sel


def run_check(ctx: Ctx, name: str, hist: dict[str, Any], setup_error: str | None) -> dict[str, Any]:
    cdir = ctx.run_dir / "checks" / name
    cdir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    res: dict[str, Any] = {"title": TITLES[name], "required": name not in ADVISORY, "started_at": iso(utcnow())}
    ctx.log(f"[{name}] start (free {free_gib(ctx.state):.1f} GiB)")
    try:
        if setup_error:
            raise CheckError(f"setup failed: {setup_error}")
        if free_gib(ctx.state) < ctx.a.min_free_gib:
            raise CheckError(f"free disk {free_gib(ctx.state):.1f} GiB < --min-free-gib {ctx.a.min_free_gib}")
        if name == "hbt":
            body = check_hbt(ctx, cdir)
        elif name in ("nt-pinned", "nt-rc"):
            body = check_nt(ctx, cdir, name)
        elif name == "repro":
            body = check_repro(ctx, cdir)
        else:
            body = check_bench(ctx, cdir, hist)
        res.update(body)
        res["status"] = "pass" if all(g["ok"] for g in body["gates"]) and body["gates"] else "fail"
    except (CheckError, E.EnvError) as e:
        res.update(status="error", reason=str(e))
    except Exception as e:  # a bug in a harness adapter must not lose the other checks' results
        res.update(status="error", reason=f"{type(e).__name__}: {e}", traceback=traceback.format_exc()[-3000:])
    finally:
        if not ctx.a.keep_work:
            for w in ("work", "out/work"):
                shutil.rmtree(cdir / w, ignore_errors=True)
    if (cdir / "log.txt").exists():
        res.setdefault("artifacts", {})["log"] = "log.txt"
    res["artifacts"] = {k: f"checks/{name}/{v}" for k, v in res.get("artifacts", {}).items()}
    res["wall_s"] = round(time.monotonic() - t0, 1)
    res.setdefault("gates", [])
    res.setdefault("metrics", {})
    res.setdefault("noisy", [])
    res.setdefault("provenance", {})
    ctx.log(f"[{name}] {res['status']} in {res['wall_s']} s" + (f": {res.get('reason')}" if res.get("reason") else ""))
    return res


NIGHTLY_ID = re.compile(r"\d{8}T\d{6}Z")


def prune_runs(state: Path, keep: int, current: str, protect: set[str]) -> list[str]:
    """Delete run directories beyond the newest `keep` of each mode (read from each directory's run.json; a directory
    without a readable run.json counts as its own group), never `current` and never a directory in `protect` (each
    mode's bench baseline). Independent of history.json, and only directories named like a nightly id."""
    runs_dir = state / "runs"
    if not runs_dir.is_dir() or keep < 1:
        return []
    by_mode: dict[str | None, list[str]] = {}
    for p in sorted(runs_dir.iterdir()):
        if not (p.is_dir() and NIGHTLY_ID.fullmatch(p.name)):
            continue
        try:
            mode = json.loads((p / "run.json").read_text()).get("mode")
        except (OSError, ValueError):
            mode = None
        by_mode.setdefault(mode, []).append(p.name)
    removed = []
    for ids in by_mode.values():
        for rid in ids[:-keep]:
            if rid != current and rid not in protect:
                shutil.rmtree(runs_dir / rid, ignore_errors=True)
                removed.append(rid)
    return sorted(removed)


def write_pages(state: Path, run: dict[str, Any], hist: dict[str, Any]) -> None:
    rid = run["nightly_id"]
    rdir = state / "runs" / rid
    on_disk = {p.name for p in (state / "runs").glob("*") if (p / "index.html").exists() or p.name == rid}
    if rdir.is_dir():
        (rdir / "index.html").write_text(P.render(run, hist, link_base="", runs_base="../", run_pages=on_disk))
    (state / "index.html").write_text(
        P.render(run, hist, link_base=f"runs/{rid}/", runs_base="runs/", run_pages=on_disk)
    )


def cmd_run(a: argparse.Namespace) -> int:
    repo = Path(a.repo).resolve()
    state = Path(a.state).expanduser().resolve() if a.state else repo / ".scratch" / "nightly"
    state.mkdir(parents=True, exist_ok=True)
    checks = selected_checks(a)
    with (state / ".lock").open("w") as lock_f:  # closed (and released) on every path out of the run
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"another nightly run holds {state / '.lock'}", file=sys.stderr)
            return EXIT_ERROR
        with stop_signals_raise():
            return run_locked(a, repo, state, checks)


def new_run_dir(state: Path) -> tuple[dt.datetime, str, Path]:
    """(start, nightly_id, run directory) for a run starting now; waits out the second when a run that finished
    within the same UTC second already took the id (the lock is held, so nothing else races for it)."""
    for _ in range(5):
        start = utcnow()
        nid = start.strftime("%Y%m%dT%H%M%SZ")
        run_dir = state / "runs" / nid
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            return start, nid, run_dir
        except FileExistsError:
            time.sleep(1.0)
    raise OSError(f"{state / 'runs'}: no free nightly id after 5 s")


def run_locked(a: argparse.Namespace, repo: Path, state: Path, checks: list[str]) -> int:
    """One run, with the state directory's lock held and stop signals raising `Interrupted`."""
    start, nid, run_dir = new_run_dir(state)
    if not a.itch_dirs:
        a.itch_dirs = [
            str(state / "itch"),
            str(repo / ".realdata" / "itch"),
            str(repo / ".realdata" / "repro" / "itch"),
        ]
    ctx = Ctx(
        a=a,
        repo=repo,
        state=state,
        run_dir=run_dir,
        mode=MODES[a.mode],
        nightly_id=nid,
        today=start.date(),
        started=start,
        log_path=run_dir / "runner.log",
    )
    run: dict[str, Any] = {
        "schema": SCHEMA_RUN,
        "schema_version": SCHEMA_VERSION,
        "nightly_id": nid,
        "mode": a.mode,
        "started_at": iso(start),
        "checks_selected": checks,
        "partial": checks != list(CHECKS),
        "git": git_info(repo),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "runner_python": sys.version.split()[0],
            "venv_python": a.python,
        },
        "setup": {},
        "checks": {},
    }
    runner_error = None
    history_ok = True
    try:
        hist = H.load_history(hist_path(state))
    except (OSError, ValueError) as e:
        ctx.log(f"history unreadable: {e}")
        runner_error = f"history unreadable: {e}"
        history_ok = False
        hist = H.empty_history()
    ctx.log(f"nightly {nid} mode {a.mode}: {', '.join(checks)}; state {state}")
    setup_error = runner_error
    step = "setup"
    try:
        if setup_error is None:
            for key, fn in (("qt", setup_qt), ("dataset", setup_dataset)):
                step = f"setup {key}"
                t0 = time.monotonic()
                try:
                    run["setup"][key] = {"status": "pass", **fn(ctx)}
                except (CheckError, E.EnvError) as e:
                    run["setup"][key] = {"status": "error", "reason": str(e)}
                    setup_error = f"{key}: {e}"
                run["setup"][key]["wall_s"] = round(time.monotonic() - t0, 1)
                ctx.log(f"[setup {key}] {run['setup'][key]['status']}")
                if setup_error:
                    break
        for name in checks:
            step = name
            run["checks"][name] = run_check(ctx, name, hist, setup_error)
    except Interrupted as e:
        ctx.log(f"{e} during {step}: the running process group was stopped; recording a partial run")
        run["interrupted"] = {"signal": signal.Signals(e.signum).name, "during": step}
        runner_error = runner_error or f"{e} during {step}"
    # the rest runs to completion: a second stop signal waits until the run is recorded
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        return finish_run(a, ctx, run, hist, history_ok, runner_error, setup_error)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


def finish_run(
    a: argparse.Namespace,
    ctx: Ctx,
    run: dict[str, Any],
    hist: dict[str, Any],
    history_ok: bool,
    runner_error: str | None,
    setup_error: str | None,
) -> int:
    """Diff, exit code, run.json, history, pruning and pages of a run whose checks are done (or interrupted)."""
    state, nid, run_dir = ctx.state, ctx.nightly_id, ctx.run_dir
    run["diff"] = H.diff_runs(hist["runs"], run)
    run["finished_at"] = iso(utcnow())
    run["wall_s"] = round((utcnow() - ctx.started).total_seconds(), 1)
    if runner_error:
        run["runner_error"] = runner_error
    failed_setup = runner_error is not None or setup_error is not None
    run["flags"] = H.run_flags(run["checks"], run["diff"], failed_setup)
    run["exit_code"] = H.exit_code(run["checks"], run["diff"], failed_setup)
    run["verdict"] = H.verdict(run["exit_code"], run["diff"])
    (run_dir / "run.json").write_text(json.dumps(run, indent=1, sort_keys=True) + "\n")
    if runner_error is None:
        hist = H.append_run(hist, run, a.history_keep)
        H.save_history(hist_path(state), hist)
    (state / "latest.json").write_text(json.dumps(run, indent=1, sort_keys=True) + "\n")
    if not a.keep_work:  # the synthetic dataset is regenerated every run (its dataset_id is in setup.dataset)
        shutil.rmtree(run_dir / "data", ignore_errors=True)
    if history_ok and runner_error is None:  # never delete run directories on the word of a history not loaded
        modes = {r.get("mode") for r in hist["runs"]}
        protect = {b[0] for m in modes if (b := bench_baseline_run(state, hist, m)) is not None}
        removed = prune_runs(state, a.keep_runs, nid, protect)
        if removed:
            ctx.log(f"pruned run directories {removed}")
    write_pages(state, run, hist)
    ctx.log(f"exit {run['exit_code']}: {run['verdict']}; page {state / 'index.html'}")
    return run["exit_code"]


def hist_path(state: Path) -> Path:
    return state / "history.json"


def cmd_render(a: argparse.Namespace) -> int:
    repo = Path(a.repo).resolve()
    state = Path(a.state).expanduser().resolve() if a.state else repo / ".scratch" / "nightly"
    hist = H.load_history(state / "history.json")
    if not hist["runs"]:
        print(f"no runs in {state / 'history.json'}", file=sys.stderr)
        return EXIT_ERROR
    write_pages(state, hist["runs"][-1], hist)
    print(state / "index.html")
    return EXIT_OK


def schedule_text(
    kind: str,
    repo: Path,
    state: Path,
    hour: int,
    minute: int,
    mode: str,
    label: str,
    extra: list[str] | None = None,
) -> str:
    """The launchd plist or crontab line for a nightly run of this checkout (printed; never installed). `extra` are
    further `run` options (e.g. `--itch-dirs`, `--repro-download`), passed through unchanged."""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hour must be 0-23 and minute 0-59")
    wrapper = HERE / "nightly.sh"
    args = ["run", "--mode", mode, "--state", str(state), *(extra or [])]
    if kind == "launchd":  # plistlib escapes every string; launchd needs <state> to exist before it opens the log
        doc = {
            "Label": label,
            "ProgramArguments": ["/bin/bash", str(wrapper), *args],
            "WorkingDirectory": str(repo),
            "StartCalendarInterval": {"Hour": hour, "Minute": minute},
            "StandardOutPath": str(state / "launchd.log"),
            "StandardErrorPath": str(state / "launchd.log"),
            "Nice": 10,
            "LowPriorityIO": True,
        }
        return plistlib.dumps(doc, sort_keys=False).decode()
    if kind == "cron":  # every word shell-quoted; % is a newline to cron unless escaped
        q = shlex.quote
        cmd = (
            f"mkdir -p {q(str(state))} && /bin/bash {q(str(wrapper))} {' '.join(q(x) for x in args)} "
            f">> {q(str(state / 'cron.log'))} 2>&1"
        )
        return f"{minute} {hour} * * * {cmd.replace('%', chr(92) + '%')}\n"
    raise ValueError(f"unknown schedule kind {kind!r}")


def cmd_schedule(a: argparse.Namespace) -> int:
    repo = Path(a.repo).resolve()
    state = Path(a.state).expanduser().resolve() if a.state else repo / ".scratch" / "nightly"
    extra = list(a.extra or [])
    if extra:  # fail now on a typo, not at 02:30
        bad = {"--mode", "--state", "--repo"} & {x.split("=", 1)[0] for x in extra}
        if bad:
            raise ValueError(f"pass {sorted(bad)} to schedule itself, not after --")
        build_parser().parse_args(["run", *extra])
    sys.stdout.write(schedule_text(a.kind, repo, state, a.hour, a.minute, a.mode, a.label, extra))
    return EXIT_OK


def cmd_list(a: argparse.Namespace) -> int:
    for name in CHECKS:
        role = "advisory" if name in ADVISORY else "required"
        print(f"{name:10s} {role:9s} {TITLES[name]}")
    for m, cfg in MODES.items():
        print(f"\nmode {m}: " + json.dumps({k: v for k, v in cfg.items() if k != "timeouts"}, sort_keys=True))
    return EXIT_OK


def default_python() -> str:
    for c in (os.environ.get("QT_NIGHTLY_PYTHON"), "/opt/homebrew/bin/python3.13", shutil.which("python3.13")):
        if c and Path(c).exists():
            return c
    return sys.executable


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="Exit codes: " + "; ".join(f"{k} {v}" for k, v in sorted(EXIT_MEANING.items())),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", default=str(REPO_DEFAULT), help="queuetruth checkout (default: this one)")
        p.add_argument("--state", default=None, help="state directory (default <repo>/.scratch/nightly)")

    p = sub.add_parser("run", help="run the checks")
    common(p)
    p.add_argument("--mode", choices=sorted(MODES), default="full")
    p.add_argument("--only", default=None, help=f"comma list of {','.join(CHECKS)}")
    p.add_argument("--skip", default=None, help="comma list of checks to leave out")
    p.add_argument("--python", default=default_python(), help="interpreter every venv is created from")
    p.add_argument("--uv", default=shutil.which("uv") or str(Path.home() / ".local/bin/uv"))
    p.add_argument("--cargo", default=shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo"))
    p.add_argument(
        "--qt-python",
        default=None,
        help="use this interpreter (queuetruth importable) instead of building the queuetruth venv",
    )
    p.add_argument("--nt-rc", default="auto", help="NautilusTrader 2.x version (default: newest on PyPI)")
    p.add_argument(
        "--wheel-dirs",
        default="",
        type=lambda s: [x for x in s.split(",") if x],
        help="extra directories searched for a retagged NautilusTrader wheel (sha256 checked)",
    )
    p.add_argument(
        "--itch-dirs",
        default=None,
        type=lambda s: [x for x in s.split(",") if x],
        help="directories searched for the public ITCH subsets (default <state>/itch, <repo>/.realdata/itch, "
        "<repo>/.realdata/repro/itch)",
    )
    p.add_argument(
        "--repro-download",
        action="store_true",
        help="stream missing public ITCH subsets from emi.nasdaq.com (several GB of network traffic)",
    )
    p.add_argument("--offline", action="store_true", help="no network: reuse existing venvs and wheels only")
    p.add_argument("--refresh-envs", action="store_true", help="rebuild every venv")
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--min-free-gib", type=float, default=10.0, help="a check does not start below this free disk")
    p.add_argument("--history-keep", type=int, default=90, help="runs kept in history.json")
    p.add_argument("--keep-runs", type=int, default=14, help="run directories kept on disk")
    p.add_argument("--keep-work", action="store_true", help="keep the checks' work directories")
    p.add_argument(
        "--bench-max-load",
        type=float,
        default=float(os.cpu_count() or 8),
        help="the bench check errors (not judged) when the 1-minute load average exceeds this during the benchmark "
        "(default: the number of CPUs; 0 disables)",
    )
    p.add_argument(
        "--bench-baseline",
        default=None,
        help="bench.json to compare the benchmark with (default: the newest run of this mode whose bench check "
        "passed without a regression)",
    )
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("render", help="re-render index.html from history.json")
    common(p)
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("schedule", help="print a launchd plist or a crontab line (installs nothing)")
    common(p)
    p.add_argument("kind", choices=["launchd", "cron"])
    p.add_argument("--hour", type=int, default=2)
    p.add_argument("--minute", type=int, default=30)
    p.add_argument("--mode", choices=sorted(MODES), default="full")
    p.add_argument("--label", default="io.github.queuetruth.nightly")
    p.set_defaults(fn=cmd_schedule, extra=[])

    p = sub.add_parser("list", help="the checks and the modes")
    p.set_defaults(fn=cmd_list)
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:  # `schedule ... -- RUN OPTIONS`
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1 :]
    a = build_parser().parse_args(argv)
    if extra and a.cmd != "schedule":
        print("nightly: options after -- are only for schedule", file=sys.stderr)
        return EXIT_ERROR
    a.extra = extra
    try:
        return a.fn(a)
    except (OSError, ValueError) as e:
        print(f"nightly: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR
    except Interrupted as e:
        print(f"nightly: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
