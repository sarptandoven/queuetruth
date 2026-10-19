"""Process-level benchmark runner: warmups, interleaved repetitions, per-process rusage (wall, user, sys, peak RSS).

Each case runs as a separate process per repetition. Cases of one group are interleaved round-robin with a rotating
start position, so slow drift (thermal, background load) spreads evenly over the cases of the group and ratios
between cases can be formed per round (paired). A case's `parse` hook may return several samples per process (for
example the per-repetition `wall_s` list of a `queuetruth-core bench ...` qt.bench document); otherwise the process
wall time is the sample.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import env as envmod

__all__ = ["Case", "ProcRun", "run_group", "run_process"]


@dataclass
class ProcRun:
    """One process execution."""

    wall_s: float
    user_s: float
    sys_s: float
    maxrss_bytes: int
    returncode: int
    stdout: str
    stderr: str
    load1_before: float


@dataclass
class Case:
    """A benchmark case. `cmd(workdir)` returns argv; `work` = units of work per process (records, messages)."""

    id: str
    group: str
    cmd: Callable[[Path], list[str]]
    unit: str  # e.g. "records/s", "msg/s", "s"
    direction: str  # "higher_is_better" | "lower_is_better"
    work: float | None = None  # if set and parse is None: metric = work / wall_s
    parse: Callable[[ProcRun], dict[str, list[float]]] | None = None  # {"metric": [...], optional extras}
    setup: Callable[[Path], None] | None = None  # before every process (fresh workdir)
    cleanup: Callable[[Path], None] | None = None  # after every process
    env: dict[str, str] | None = None
    timeout_s: float = 3600.0
    target: dict | None = None  # {"op": ">=", "value": ..., "spec": "..."}
    reference: dict | None = None
    notes: list[str] = field(default_factory=list)
    skip_reason: str | None = None


def run_process(argv: list[str], cwd: Path | None, extra_env: dict | None, timeout_s: float) -> ProcRun:
    """Run argv; wait4() gives exact per-child rusage; stdout/stderr go through temp files (no pipe deadlock)."""
    full_env = dict(os.environ)
    if extra_env:
        full_env.update(extra_env)
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        l1 = envmod.load1()
        t0 = time.perf_counter()
        p = subprocess.Popen(argv, cwd=cwd, env=full_env, stdout=fo, stderr=fe)
        timer = threading.Timer(timeout_s, p.kill)
        timer.start()
        try:
            _, status, ru = os.wait4(p.pid, 0)
        finally:
            timer.cancel()
        wall = time.perf_counter() - t0
        p.returncode = os.waitstatus_to_exitcode(status)
        fo.seek(0)
        fe.seek(0)
        out, err = fo.read().decode(errors="replace"), fe.read().decode(errors="replace")
    rss = ru.ru_maxrss if sys.platform == "darwin" else ru.ru_maxrss * 1024  # macOS bytes, Linux KiB
    return ProcRun(wall, ru.ru_utime, ru.ru_stime, int(rss), p.returncode, out, err, l1)


def _wait_quiet(max_load: float | None, max_wait_s: float) -> bool:
    """Wait (bounded) for the 1-minute load to fall below max_load; returns True when quiet."""
    if max_load is None:
        return True
    deadline = time.monotonic() + max_wait_s
    while envmod.load1() > max_load:
        if time.monotonic() > deadline:
            return False
        time.sleep(2.0)
    return True


def run_group(
    cases: list[Case],
    root: Path,
    warmup: int = 1,
    repeat: int = 5,
    max_load: float | None = None,
    max_wait_s: float = 30.0,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr, flush=True),
) -> dict[str, dict]:
    """Run the cases of one group interleaved; returns {case_id: raw record} (samples per round, errors, flags)."""
    live = [c for c in cases if c.skip_reason is None]
    raw: dict[str, dict] = {
        c.id: {"rounds": [], "errors": [], "noisy_rounds": 0, "skip_reason": c.skip_reason} for c in cases
    }
    total_rounds = warmup + repeat
    for r in range(total_rounds):
        measured = r >= warmup
        k = r % len(live) if live else 0
        for c in live[k:] + live[:k]:
            quiet = _wait_quiet(max_load, max_wait_s)
            wd = root / c.id.replace("/", "_")
            wd.mkdir(parents=True, exist_ok=True)
            if c.setup:
                c.setup(wd)
            pr = run_process(c.cmd(wd), wd, c.env, c.timeout_s)
            tag = "run" if measured else "warmup"
            msg = None
            vals: dict = {}
            if pr.returncode != 0:
                msg = f"{c.id} {tag} {r}: exit {pr.returncode}: {pr.stderr.strip()[-500:]}"
            else:
                try:
                    vals = c.parse(pr) if c.parse else {"metric": [c.work / pr.wall_s] if c.work else [pr.wall_s]}
                except (ValueError, KeyError, OSError) as exc:  # output check failed: the run does not count
                    msg = f"{c.id} {tag} {r}: output check failed: {exc}"
            if c.cleanup:
                c.cleanup(wd)
            if msg is not None:
                raw[c.id]["errors"].append(msg)
                log(msg)
                continue
            log(
                f"{c.id} {tag} {r}: wall {pr.wall_s:.3f}s rss {pr.maxrss_bytes / 2**20:.0f}MiB "
                f"load1 {pr.load1_before:.2f} metric {vals.get('metric')}"
            )
            if measured:
                raw[c.id]["rounds"].append(
                    {
                        "round": r - warmup,
                        "metric": vals.get("metric", []),
                        "extra": {k2: v for k2, v in vals.items() if k2 != "metric"},
                        "wall_s": pr.wall_s,
                        "user_s": pr.user_s,
                        "sys_s": pr.sys_s,
                        "peak_rss_bytes": pr.maxrss_bytes,
                        "load1_before": pr.load1_before,
                        "quiet": quiet,
                    }
                )
                if not quiet:
                    raw[c.id]["noisy_rounds"] += 1
    return raw
